# The local model service (SYRD-691)

One opt-in, host-wide inference service that serves the exact model `Qwen/Qwen3.8-27B` to Hermes roles over
loopback. There is one service for the whole host, never one per tenant or role. Nothing is downloaded or
installed until the operator approves a packet, and no project launch starts it.

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
switchyard local-model status                        # unit state, why it last stopped, model, load
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
- **Starting:** no `[Install]` section, so it never starts at boot. Only `start` starts it.
- **Context:** 65,536 tokens, since Hermes refuses a window below 64,000 (SYRD-692), and `install` refuses
  one that small. The q8_0 cache is about 2 GiB. The 6 full-attention layers among the 24 on the GPU hold
  0.8 GiB of it, and the rest is in host RAM.
- **Limits:** `MemoryHigh=28G`, `MemoryMax=32G`, no swap, `CPUWeight=20`, `Nice=10`, one request at a time
  (a second waits).
- **Restarts:** on failure, at most two starts in 15 minutes. A model that cannot load stops instead of
  restart-looping.
- **Sandbox:** `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, and only the NVIDIA devices are allowed.

`status` says why the service last stopped, in bounded lines:
- it ran out of memory;
- it failed twice and was given up on;
- llama-server's exit status;
- a load that timed out.

When it is running, `status` also says whether the endpoint is still loading, answers as exactly the
pinned model, and how many requests are running and waiting.

## Tuning, re-pinning, removal

- **Tuning:** `--gpu-layers` (16–40, default 24) and `--threads` (4–16, default 12) may be changed by
  re-running `install` with the same digest, then `restart`. They are measured on the real card. Fewer
  GPU layers leave more VRAM to the desktop; a start that does not fit fails once and says so. Nothing
  else changes without a new packet.
- **Re-pinning:** a newer Qwen or llama.cpp is a new pin in `scripts/local_model_service.py`, a new packet
  and a new approval. It is never drift.
- **Removal:** `sudo switchyard local-model stop`, then `sudo switchyard local-model remove`. That stops the
  service, then deletes the unit, the engine, the model and the record, and leaves every other file on the
  host as it was.

Hermes roles are pointed at the endpoint in SYRD-692; the live Hermes canary is SYRD-693.
