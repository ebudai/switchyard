# SYRD-693: the live local-Qwen canary, and what it decides

SYRD-690 asks whether the local Qwen3.8 model, through Hermes, is safe and useful as a Switchyard worker.
SYRD-691 built the service and SYRD-692 the role profile. This ticket ran the real thing once, in a bounded
window, and fixed what that run and its preparation found.

**The decision:** local Qwen is safe for **attended, operator-started work windows**, on **one role at a
time**, for **small, bounded tasks** (a single ticket's board read, a short edit and test in a small
repository, a summary). It is not for control or gate roles (Director, Audit, Librarian, Inspector), nor for
an always-on implementer, nor for work on a repository the size of Switchyard's.

## How it was run

- **When:** 2026-11-01, 22:01:26–22:28:33 EST (27 minutes of the 120 approved). Eric approved, then ran
  `/tmp/syrd-693-canary-approved.sh` (sha256 `bbc2dbf7…`, which checks the payload's `b79530da…`). The
  log is `/var/log/syrd693-canary-20261101T220126.log`.
- **The root side:** it installed nothing. It checked that `preview` and the installed record both say
  packet `9158bf5e…`, then ran `local-model start`. On request it ran `stop`, `start` and finally `done`,
  and it ended stopped: `inactive/dead`, `Result=success`, `NRestarts=0`, and only kwin on the GPU.
- **The model side:** the installed Hermes 0.15.2 ran as main's own account, with a role home written by
  SYRD-692's `write_role_home`. Every start went through the real pane check (`hermes_local_profile.py check`).
  Hermes ran in a bwrap sandbox with no network except two relays:
  - a loopback relay to `127.0.0.1:17431`;
  - a GET-only relay to the board, which answered 405 to anything else.

  So nothing could leave the host, and the canary had no way to write to the board.
- **Not done:** no tenant role was added, no cloud credit was spent, and no repository material left the
  host. The Director's choice was that the existing launcher suites stand for fresh-per-ticket and
  same-parent routing, which do not depend on the CLI.

## What answered

| Check | Result |
|---|---|
| Model | `/v1/models`: exactly `Qwen/Qwen3.8-27B`, `n_ctx` 65,536 (`n_ctx_train` 262,144), Q4_K - Medium, 26,895,998,464 parameters |
| Artifact | `/props` `model_path`: `/var/lib/switchyard/local-models/ggml-org/Qwen3.8-27B-GGUF/a316ea527fde…/Qwen3.8-27B-Q4_K_M.gguf`, as the record pins it |
| Server | `build_info` and each reply's `system_fingerprint`: `b11803-a37cc4610`. The `llama-server` binary's sha256 `ec45d805…` matches the record |
| Process | The unit's cgroup holds the supervisor and `llama-server` with the approved command line: `--host 127.0.0.1 --port 17431 --ctx-size 65536 --n-gpu-layers 24 … --no-slots --cors-origins https://cors.invalid` |
| Endpoint | `ss`: listening on `127.0.0.1:17431` only. One slot (`total_slots` 1) |
| Start check | Passes with the service up. With it stopped, it refuses in 0.04 s with the remedy and "nothing falls back to a hosted model" |
| Hermes | The main model and every auxiliary task (title generation included) went to `custom` at `http://127.0.0.1:17431/v1`. Hermes's log shows no other provider |

## The tasks

Each ran as `hermes -z`, through the check, in the sandbox.

| Task | Time | Tool calls | Result |
|---|---|---|---|
| Board read: `ticket-board-read ticket SYRD-693 --json`, answer `{state, assignee, awaiting_role}` | 76 s | 1 (`terminal`) | Exactly the board's values |
| Edit and test: a failing `median` in a fixture repository; fix `stats.py` only, rerun, report as JSON | 132 s | 4 (`terminal`, `read_file`, `patch`, `terminal`) | Only `stats.py` changed; the suite passes when rerun by hand; the report `{"tests_run": 3, "failures": 0}` is true |
| Nothing to do: run the tests, change nothing if they pass, reply `NO_CHANGE` | 43 s | 1 (`terminal`) | No change, `NO_CHANGE` |

Every tool call was a structured `tool_calls` entry whose arguments are a JSON string. No assistant message
held text that imitates a tool call (`<tool_call>`, `<function=`, a quoted tool name, a tool fence).

## The session lifecycle

These ran in interactive Hermes in a private tmux, which is how a pane runs it.

- **Continuation:** a codeword, then a question about it in the same conversation: answered in 42 s.
- **Stop and resume:** the pane was ended, then started with `--resume 20261101_220733_393a82`. Hermes said
  "Resumed session … (3 user messages, 8 total messages)" and recalled a fact given only in that
  conversation, in 8 s, from the cached prefix.
- **Fresh start, as the board does it:** `/clear now`, then the ticket message 0.5 s later. A new session
  started, the message arrived, and the conversation-only fact was gone (`NONE`).
- **The boundary leaked through Hermes's memory** (fixed below, defect 4). A fact Hermes chose to save with its
  own `memory` tool came back in the new session, with no tool call. Hermes's memory (`memories/MEMORY.md` in
  the role's home) and its `session_search` over earlier sessions outlive `/clear`.

## Board hooks, live (SYRD-690)

SYRD-693's canary ran Hermes from a sandbox home without Switchyard's hooks. SYRD-690's Audit asked for the
real pane command with them, so one more approved window ran (2026-11-02, 18:07:14–18:10:38 EST; log
`/var/log/syrd690-window-20261102T180714.log`). It installed the corrected packet `6f787b3c…` (one account of its RAM;
`docs/local-model-service.md`) without downloading anything, served for 3.5 minutes, and ended stopped (`NRestarts=0`, only kwin on the GPU).

**Setup.** Nothing touched a live tenant, board or Hermes home. SYRD's own shared `config.yaml` was
byte-identical before and after.
- **Hooks:** Switchyard's own `ticket-board-install-pane-hooks install` wrote a disposable shared Hermes home,
  whose hooks name scratch state and session directories.
- **Tenant:** a disposable one with one local-Qwen Hermes role.
- **Start:** the real `role_sessions._start_role_session`, which ran the start check, prepared the home and
  started the real pane command: `… hermes_local_profile.py check … -- hermes -m Qwen/Qwen3.8-27B --yolo
  --accept-hooks --pass-session-id`. tmux ran on a private socket.

**What the board would read.** The pane's state file, `ticket-board-pane-idle-hook`'s, at every change:

| When | State | Source |
|---|---|---|
| launcher start | idle | the launcher's seed |
| first turn, 5.6 s later | busy | `hermes.pre_llm_call` |
| its answer, 54.7 s later | idle | `hermes.post_llm_call` |
| `/exit` | idle | `hermes.on_session_end`, then `hermes.on_session_finalize` |
| launcher start, resuming | idle | the launcher's seed; its command carried `--resume 20261102_180821_19317c`, the session the `on_session_start` hook had recorded |
| second turn | busy, then idle 16 s later | `hermes.pre_llm_call`, `hermes.post_llm_call` |

**The conversation.** The first turn asked the model to remember `CANARY-690` and it answered `OK`. After the
stop and the resume, asked which word, it answered `CANARY-690`. It was one session throughout
(`20261102_180821_19317c`: 4 messages, 2 model calls).

**Off the host: nothing.**
- Hermes's TCP peers, sampled through both turns, were only `127.0.0.1:17431`.
- Its log names no endpoint but `http://127.0.0.1:17431/v1`.
- It made no lazy install.
- Its update check was answered from the home (below).

**Found by this canary's dry run, and fixed.** Run outside a sandbox, Hermes 0.15.2 asked PyPI for a newer
release at its start. A local role's home now answers that check, so the role asks nothing off the host
(SYRD-692 note).

## Failure, load and recovery

- **Endpoint stopped mid-session** (`local-model stop` by the window script): the next turn made 3 attempts
  in 22.7 s, then "API call failed after 3 retries: Connection error". Hermes's "trying fallback" found
  nothing to try, because the profile's fallback list is empty. Hermes stayed at its prompt: no loop, no exit,
  no other provider.
- **Recovery:** `local-model start` loaded in 1.4 s (`model loaded`, with the model in the page cache; a
  first load after a reboot reads 19 GB from disk). The same Hermes session carried on.
- **Cancellation:** Ctrl-C during a long answer. Within 0.5 s `requests_processing` was 0 and generation
  stopped (no counter moved in the next 10 s). Hermes said "Operation interrupted" and stayed up.
- **Two roles at once:** two Hermes edit tasks plus a direct request 20 s later. The slot ran one request at
  a time with up to 2 waiting (`requests_deferred` 2). Both roles finished correctly, in 360 s and 404 s
  against 132 s alone. The direct request waited 70 s. No request failed. Hermes's own background title
  generation, which shares the slot, timed out twice and was dropped.
- **Over the limit:** an 80K-token prompt was refused at once with HTTP 400 `exceed_context_size_error`
  (`n_ctx` 65,536). Nothing was truncated silently.

## Measurements

| What | Measured |
|---|---|
| Load | `answered` 2 s after `start`; `model loaded` 1.37 s into the restart (model in page cache) |
| First token | 0.3–0.6 s on a short prompt. With thinking on, the first answer token comes after 8–9 s of reasoning |
| Generation | 5.2 tokens/s on a short context, 3.3 at 39K tokens, 2.65 at 61K |
| Prefill | 522 tokens/s at 39K; 422 at 61K (22.5K new on a 38K cached prefix) |
| Usable context | 65,536 tokens. A 60,867-token prompt answered correctly. Hermes's own system prompt and tool schemas take about 10.2K of it on every request (10,249 cached tokens on the board task's first call) |
| VRAM | `llama-server` peak 8,612 MiB. GPU total peak 8,823 MiB of 12,282, with kwin (10 MiB) and briefly another tenant's job (156 MiB) |
| Host RAM | Unit cgroup peak 9.15 GiB (the mapped model counts as page cache), no swap, against `MemoryHigh` 28G |
| CPU | Load average 9 on the 32-thread host while generating. CPU pressure `some avg10` at most 0.21%, mean 0.01%. The desktop was never starved |
| GPU utilisation | About 25% while generating: the CPU half of the model sets the pace |

## Defects found, and fixed in this ticket

1. **A local role started while the service is stopped lost its conversation.** The service stops itself
   after 15 idle minutes, so this is the usual case. The pane's check exits at once. `role_sessions` took
   that for a failed resume, set the session record aside, and tried a fresh start, which was refused too.
   - **Fix:** `role_sessions._start_role_session` asks `hermes_local_profile.start_refusal` first. A local
     role whose service does not serve its profile is refused before any session decision. The record is
     kept and `local_model_unavailable` goes into the pane's resume history.
2. **A bare `/clear` swallowed the next ticket.** Hermes 0.15.2 answers `/clear` with a confirmation menu
   (1/2/3, 120 s), even under `--yolo`. The board types the ticket 0.5 s after the clear, into that menu, and
   it was lost. When the menu timed out, Hermes printed "Goodbye!" and exited, ending the pane. This affects
   every Hermes role that starts each ticket fresh.
   - **Fix:** the board types `/clear now` to Hermes (`SESSION_CLEAR_COMMANDS`; `/new now` since SYRD-727). `now` is Hermes's own skip
     token.
3. **Hermes `pip install`ed from PyPI as it started.** Its lazy installer tried its text-to-speech packages
   every ~8 s (18 attempts in one run against the scripted stand-in) and held the first turn back 47 s
   offline. The live run, with the fix, made none. On a networked host that is
   off-host traffic outside the model path.
   - **Fix:** a local role's config sets `security.allow_lazy_installs: false`.

4. **An ephemeral Hermes role carried the last ticket through Hermes's memory.** This is any Hermes role,
   local or cloud, that the board clears into each ticket (the workflow's `ephemeral`, or the launcher's
   `fresh_session_per_ticket`). `/clear` empties the conversation, but Hermes puts `memories/MEMORY.md` and
   `USER.md` into every new session's prompt, and `session_search` reads every earlier session. Audit kicked
   back three candidates on this:
   - the first for leaving it open;
   - the second for reading only `fresh_session_per_ticket` and for running panes;
   - the third because a same-parent continuation skipped the check, and a restart could resume the
     open-era conversation.
   - **The home:** such a role always gets Switchyard's private `config.yaml`. It sets
     `memory.memory_enabled`, `memory.user_profile_enabled` and the memory provider off, adds `memory` and
     `session_search` to `agent.disabled_toolsets` (`hermes_role_config.close_ticket_boundary`), and carries
     `TICKET_BOUNDARY_MARK`. The owner's other settings stay, and persistent roles keep Hermes's memory.
   - **A resumed launch:** a launch that resumes a ticket the role already holds is prepared from the role's
     declaration.
   - **A running pane:** Hermes reads its config once, when it starts, and a `/clear` keeps that agent. A
     workflow apply can make a running role ephemeral without restarting it. So before clearing a Hermes
     pane into a ticket, the board (`hermes_ticket_boundary.open_reason`) reads:
     - the HERMES_HOME in the pane's start command;
     - whether that home's `config.yaml` carries the mark;
     - whether it was written before the pane's process started.

     Otherwise it holds the hand-off (`hermes_ticket_boundary_open`, with the reason) until the role is
     restarted. The ticket waits; nothing is typed. It asks before any same-parent continuation (SYRD-626),
     too, since carrying a pane on does not close it.
   - **The cutover:** the start that first closes a role's boundary, because its home's config was not yet
     marked, never resumes. The conversation it would have resumed was held with memory and search open, so
     it is set aside (recoverable, `ticket_boundary_cutover` in the pane's resume history), on both the
     launcher's start and attach-role. Every later start resumes only a conversation that began closed,
     which a continuation may then carry on. Persistent roles resume as before.
   - **Proof, with the installed Hermes and the scripted stand-in model:** a fresh-per-ticket home with
     `PLUM-42` in its `MEMORY.md` sent no trace of it to the model and offered 14 tools, without `memory` or
     `session_search`. A persistent home sent it, and offered both (16 tools). The board-side check is
     tested against a real tmux pane started as the launcher starts one.
   - **Not covered:** skills Hermes writes go to the owner's shared skills, as for every role. Those are
     shared tooling by design, not a conversation.

## What it decides

**Which roles and workloads**
- **Yes:** one implementer-class role, started by the operator for a working session the operator attends:
  - small bounded tickets: a single ticket's board read, a short edit and test in a small repository, a
    summary or classification;
  - work where 2–7 minutes a task is acceptable and nothing leaves the host.
- **No:** Director, Audit, Librarian and Inspector, whose judgement gates other work.
- **No:** an always-on board worker. The service stops after 15 idle minutes and only root starts it, so a
  role left waiting finds it stopped. Its next turn fails in about 23 s; the ticket waits and nothing loops.
  A non-root wake path would be a new privileged boundary and is not part of this ticket.
- **No:** this repository's own implementation work.
  - The usable window is 65,536 tokens, and Hermes takes about 10K of it.
  - `scripts/team_launcher.py` alone is 92.5 KB, roughly 25K tokens.
  - The Claude and Codex workers on this board routinely carry far longer contexts.

**Concurrency:** one local role per host. The service has one slot.
- A second role works, but both run about three times slower.
- Hermes's own background requests then time out.
- The board's own deadlines (stall reminders) were not designed around 6-minute turns.

## Reproducing

The harness lives outside the repository: the sandbox, relays, sampler and driver.
- **The root script** is quoted on the ticket.
- **The unprivileged side** needs only the installed Hermes, `bwrap` and the service. Every step can be
  rerun in another approved window.
- **The offline half** is in `tests/hermes_local_profile_test.py`:
  - a start while the service is stopped;
  - the board's `/clear` command, read by the installed Hermes;
  - lazy installs, read by the installed Hermes.
