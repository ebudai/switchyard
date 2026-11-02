# The local model service (SYRD-691)

One opt-in, host-wide inference service that serves the exact model `Qwen/Qwen3.8-27B` to Hermes roles over
loopback. There is one service for the whole host, never one per tenant or role. Nothing is downloaded or
installed until the operator approves a packet, and no project launch starts it.

**While it runs it holds about 8.3 GiB of the GPU's memory (8,546 MiB measured) and 3 GiB of RAM, whether or not
anything is using it.** It stops itself once it has been idle for 15 minutes, and never while a request runs.
`sudo switchyard local-model stop` releases it at once. `switchyard local-model status` says what holds GPU
memory now.

## What it is, and why this shape

- **Model:** `Qwen/Qwen3.8-27B` at upstream revision `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0`
  (Apache-2.0). It is served from the llama.cpp project's own conversion,
  `ggml-org/Qwen3.8-27B-GGUF` at revision `a316ea527fde32c12487be7574e2db5ac2ad2a7e`:
  `Qwen3.8-27B-Q4_K_M.gguf`, 18,973,870,528 bytes, sha256 `c600de03…a747`.
- **Engine:** llama.cpp release `b11803` (commit `a37cc46104eb…`), the official Linux CUDA 13.4 x64
  build. It uses the host's own CUDA 13.4 runtime and cuBLAS. No pacman package, pip package or
  container image is installed.
- **Why not vLLM or SGLang:** they keep weights in VRAM. BF16 is 55.6 GB and the official FP8 checkpoint
  30.9 GB, and with CPU offload every generated token would cross PCIe with about 19 GB, about one token a
  second on a 12 GiB card.
- **Why llama.cpp:** it runs the layers it cannot fit on the CPU, reading them from RAM. On the qualifying
  host (RTX 4070 12 GiB with about 1.8 GiB held by the desktop, Ryzen 9 9950X, about 63 GB/s of measured
  read bandwidth), Q4_K_M puts about 7 GB on the GPU and reads about 12 GB from RAM per token. The estimate
  is 4–5 tokens a second. Qwen3.8 thinks by default, so one turn can take minutes. A 24 GB card would hold
  the whole Q4_K_M with its cache.
- **Tool calls and reasoning:** llama-server parses Qwen3.8's reasoning into `reasoning_content` and its
  tool calls into structured `tool_calls` (`--jinja --reasoning-format deepseek`).
  `switchyard local-model probe` proves this against the running server. It fails a server that only
  writes tool markup as text, that calls a tool it was not offered, or that returns arguments that are not
  a JSON string.

## Commands

```bash
switchyard local-model preview                       # the approval packet; changes nothing
sudo switchyard local-model install --approve <digest>   # download, verify, install; starts nothing
sudo switchyard local-model start | stop | restart
sudo switchyard local-model qualify                  # start, wait for the load, probe, stop what it started
switchyard local-model status                        # unit state, why it last stopped, GPU memory, model, load, idle stop
switchyard local-model probe                         # one real structured tool call, as Hermes makes it
switchyard local-model logs                          # the unit's last journal lines (journal readers only)
sudo switchyard local-model remove                   # everything it installed, and nothing else
```

`preview` prints every fact that decides what is downloaded, installed and run:
- sources, revisions, sizes and digests;
- paths, the unit and its identity, the port and the full server command;
- limits, sizing, network use and rollback;
- the GPU's current use and the free disk.

It ends with the packet's digest. `install` refuses any `--approve` that is not that digest, so what is
installed is exactly what was approved. `install` and `remove` are root's, and each refuses otherwise. The
rest run as the caller.

## What it installs, and where

| What | Where |
|---|---|
| engine | `/opt/switchyard/local-models/llama.cpp/b11803/` (and the release archive beside it) |
| model | `/var/lib/switchyard/local-models/ggml-org/Qwen3.8-27B-GGUF/<revision>/Qwen3.8-27B-Q4_K_M.gguf` |
| record | `/var/lib/switchyard/local-models/service.json`: the approved packet's digest, the model and engine pins, the exact `llama-server` path and its sha256, the settings, when it was installed |
| supervisor | `/opt/switchyard/local-models/local-model-supervisor.py`: the service's main process, a copy of `scripts/local_model_supervisor.py` whose sha256 is in the packet |
| unit | `/etc/systemd/system/switchyard-local-model.service` |

Every directory is created root-owned and writable by root alone. A symlink anywhere on the way, or a
directory anybody else could write, is refused before anything is written or deleted. Downloads resume.
A file is kept only if its size and sha256 match its pin; otherwise it is discarded. Running `install`
again changes nothing.

## The service

- **Identity:** systemd `DynamicUser` `switchyard-local-model`, with no login and no home.
- **Network:** listens on `http://127.0.0.1:17431/v1` only, with `IPAddressAllow=localhost`. The service
  can reach nothing but loopback.
- **Browsers:** llama-server reflects any web page's origin by default, so a page open on this host could
  drive the model and read its answers. The service allows no origin (`--cors-origins
  https://cors.invalid`, `--no-cors-credentials`), since nothing that uses it is a browser. `/slots` is
  off (`--no-slots`); load comes from `/metrics`.
- **Starting:** no `[Install]` section, so it never starts at boot. Only `start` and `qualify` start it. No
  launcher, project start, unit, timer or hook names it, and the Hermes start check of a local role only reads
  `/v1/models` and `/props`. A project that does not use the endpoint never starts it or keeps it running.
- **Context:** 65,536 tokens, since Hermes refuses a window below 64,000 (SYRD-692), and `install` refuses
  one that small. The q8_0 cache is about 2 GiB. The 6 full-attention layers among the 24 on the GPU hold
  0.8 GiB of it, and the rest is in host RAM.
- **Limits:** `MemoryHigh=28G`, `MemoryMax=32G`, no swap, `CPUWeight=20`, `Nice=10`, one request at a time
  (a second waits).
- **Restarts:** on failure, at most two starts in 15 minutes. A model that cannot load stops instead of
  restart-looping.
- **Sandbox:** `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, and only the NVIDIA devices are allowed.

## GPU memory and the idle stop

The unit's main process is a small supervisor that runs llama-server and watches what llama-server itself
counts on `/metrics`:
- **Activity** is a request running or waiting (`requests_processing`, `requests_deferred`), or any work
  counter (`prompt_tokens_total`, `n_decode_total` and the other `llamacpp:*_total`) that moved since the last
  reading. It reads every 30 seconds.
- **Idle for the window** (15 minutes by default, `--idle-minutes` 5–120 at install): it ends llama-server and
  exits with status 75. The unit counts that as a clean stop (`SuccessExitStatus=75`), so it is not
  restarted. The GPU and host memory are released, and the CUDA context with them.
- **Never during a request:** a request that thinks for twenty minutes is running the whole time. Just before
  stopping, the supervisor reads the metrics once more and stays up if anything moved.
- **What is not activity:** reads that do no inference (`/health`, `/v1/models`, `/props`, `/metrics`), so
  `status` and the Hermes start check never keep it alive.
- **A consumer that is gone:** a qualification run, a test, or a role that crashed or was abandoned stops making
  requests, and the service stops one window later. Consumers are counted by the work the server does for them,
  not by registering, so an abandoned registration cannot hold it.
- **An endpoint that does not answer** is not activity either, so a server that never loads, hangs or stops
  answering is also stopped one window after anything was last seen moving.
- **`systemctl stop`** (`KillMode=mixed`) signals the supervisor alone, which passes SIGTERM to llama-server
  once.
- **A crash:** if llama-server exits on its own, the supervisor exits with its status, so `Restart=on-failure`
  and the two-starts limit apply as before.

The engine's own `--sleep-idle-seconds` is not used: it unloads the weights but keeps the process, and its CUDA
context, on the GPU, and any stray request loads the model again.

A Hermes role that finds the service stopped is refused at its start check, with the command that starts it.
Starting it is root's.

`status` says why the service last stopped, in bounded lines:
- it stopped itself after its idle minutes;
- it ran out of memory;
- it failed twice and was given up on;
- llama-server's exit status;
- a load that timed out.

It also says what holds GPU memory now, as `nvidia-smi` reports it: this service's processes (from the unit's
cgroup), with how many MiB and what releases them. If a `llama-server` outside the service holds GPU memory, it
names that one as well; the idle stop does not reach it. When it is running, `status` also says:
- whether the endpoint is still loading;
- whether it answers as exactly the pinned model;
- how many requests are running and waiting;
- how long it has been idle, and when it will stop itself.

A service installed before the idle stop existed is named as such. `start`, `restart` and `qualify` refuse it
until the current packet is approved and installed. `stop` always works.

## Qualification and UAT

Every qualification or acceptance run ends with the service stopped, unless it was already running for
someone else:

```bash
sudo switchyard local-model qualify
```

`qualify` starts the service if it is not running and waits up to 330 s for the model to load. Then it runs the
structured-tool probe and prints `status`, with the GPU memory it holds. Then it stops what it started, whether
the probe passed, failed or never got to run. A failed stop fails the qualification and prints the command to
run. A service that was already running is probed and left running, and its idle stop still bounds it.

A procedure that starts the service by hand (`start`, then `probe` or anything else) ends with
`sudo switchyard local-model stop`, and then checks `switchyard local-model status` for
`GPU: this service holds no GPU memory`.

## Tuning, re-pinning, removal

- **Tuning:** `--gpu-layers` (16–40, default 24), `--threads` (4–16, default 12) and `--idle-minutes` (5–120,
  default 15) may be changed by re-running `install` with the same digest, then `restart`. They are measured on the real card. Fewer
  GPU layers leave more VRAM to the desktop; a start that does not fit fails once and says so. Nothing
  else changes without a new packet.
- **Re-pinning:** a newer Qwen or llama.cpp is a new pin in `scripts/local_model_service.py`, a new packet
  and a new approval. It is never drift.
- **Removal:** `sudo switchyard local-model stop`, then `sudo switchyard local-model remove`. That stops the
  service, then deletes the unit, the engine, the supervisor, the model and the record, and leaves every other
  file on the host as it was. If the service does not stop, `remove` deletes nothing and says so, so the
  service is never left running without its unit.

## Running a role on it (SYRD-693)

The live canary (`docs/syrd-693-local-qwen-canary.md`) decides how the service is used.

**Which roles.**
- **Yes:** one implementer-class Hermes role, for small bounded tasks: a single ticket's board read, a short
  edit and test in a small repository, a summary.
- **No:** Director, Audit, Librarian or Inspector.
- **No:** an always-on worker.
- **No:** work on a repository whose files fill the window. The usable window is 65,536 tokens, and Hermes's
  own prompt and tools take about 10K of every request.

**One local role per host.** The service has one slot. A second role works, but both run about three times
slower, and Hermes's own background requests time out.

**Attended windows.** The service stops itself after 15 idle minutes and only root starts it, so a local role
works while the operator is there:

```bash
sudo switchyard local-model start        # loads in seconds when the model is in the page cache
switchyard local-model status            # serving, the GPU memory it holds, when it stops itself
switchyard start <project>               # or attach the local role; its pane checks the endpoint first
# ... the role works its tickets ...
sudo switchyard local-model stop         # or let the idle stop release it 15 minutes after the last request
switchyard local-model status            # "GPU: this service holds no GPU memory"
```

**When the service is stopped.**
- **A role started then** is refused before its pane starts, with this remedy. Its conversation's record is
  kept, so the next start resumes it.
- **A role already running** fails its next turn after three attempts, about 23 seconds, and waits at its
  prompt. Nothing retries forever and nothing falls back to a hosted model. Start the service, and the same
  conversation carries on.

**What to expect, measured on the qualifying host.**
- **GPU:** about 8.6 GiB while it runs (8,612 MiB peak).
- **Host memory:** 9.2 GiB peak in the unit, most of it the mapped model.
- **CPU pressure:** negligible.
- **Generation:** about 5 tokens a second, falling to 2.7 at 60K tokens of context.
- **Turn times:** a one-call task takes about a minute, a four-call edit-and-test about two, each roughly three
  times longer with a second role.

**Choosing and leaving it.** `switchyard add-role … --local-qwen` and
`switchyard set-role-runtime … --local-qwen`, and leaving it again, are in
[SYRD-692](syrd-692-hermes-local-profile.md) and `docs/role-runtime.md`. A re-pin of the service (above) needs
each local role re-pinned the same way.
