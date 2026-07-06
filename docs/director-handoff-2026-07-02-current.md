# Director Handoff — PGU Current State

**Provenance note (added 2026-07-05):** This file is handoff material from a *different, unrelated project* (internally called SRX), copied into this repo's `docs/` folder with a mechanical case-insensitive `srx`→`pgu` text replacement. It is retained only as process-pattern reference. None of the project specifics below describe this repository's actual history — this repo's real project is the procedural galaxy/point-cloud renderer in `main.cpp` (see `docs/galaxy_architecture.md`).

**Date**: 2026-07-02  
**Repo**: `/home/budai/Projects/PGU`  
**Primary concern**: current director context is slow; start a fresh director soon.

## Current Repo State

As of this handoff, the local checkout is on:

```text
docs-cuda-teardown-investigation-notes
```

Important refs:

- `origin/main`: `435b94e` — `Extract motion field comparison helper`
- local `main`: `cae9d18` — `Merge packet 36.161 segment-stable review runner`
- `packet-36-161-panoptic-segment-stable-review-runner`: `239ceb2`
- `origin/docs-cuda-teardown-investigation-notes`: `bcb0cfe`

Important nuance: `docs-cuda-teardown-investigation-notes` was pushed from a checkout that already contained local Packet 36.161 merge commit `cae9d18`. Do not blindly merge or fast-forward it into `origin/main` without deciding how to land Packet 36.161 and the CUDA notes.

## Packet 36.161

Status:

- Implemented by main.
- Audit clean.
- Merged into local `main` as `cae9d18`.
- Not yet pushed to `origin/main` at the time this handoff was written.

What it did:

- Added `docs/packets/packet_36_161_panoptic_segment_stable_review_runner.md`.
- Improved `scripts/check_latest_visual_review.fish` so it finds segment-stable panoptic artifacts by contents, not only old 36.36 names.
- Added `PGU_PLAYER_AUTO_CLOSE_MS` passthrough to the visual review scripts.
- Added durable segment-stable summary output to `scripts/run_packet_36_96_guest_panoptic_guard_review.fish`.

The next director should decide whether to push local `main` to `origin/main` before assigning more mainline packets.

## CUDA Teardown Investigation Notes

Status:

- Recorded in `docs/todo.md`.
- Committed and pushed on branch `docs-cuda-teardown-investigation-notes`.
- Commit: `bcb0cfe`.
- Not intentionally merged into `origin/main`.

Content:

- Static-inspection rule-outs for the CUDA-visible teardown heap corruption.
- Leading hypothesis: async CUDA kernel execution fault is only surfacing at synchronize, creating a sticky context error and later teardown corruption.
- Host-side instrumentation recipe: reproduce under `compute-sanitizer --tool memcheck`, then `synccheck`, then host ASAN if sanitizer is clean.
- Separate latent item: GPU copy-back currently bounds reads by destination size; safe under current invariants, but should be hardened by exposing/clamping source buffer lengths.

Recommended next main/GPU packet:

```text
CUDA rasterizer diagnostics and copy-back invariant hardening
```

Scope should be diagnostic and opt-in:

- Add `PGU_CUDA_DIAGNOSTICS=1`.
- Label checks after each kernel launch.
- Optionally synchronize after each kernel only in diagnostics mode.
- Log GPU allocation/capacity sizes.
- Assert/copy-back source and destination lengths.
- Add a host script for normal and `compute-sanitizer` runs.
- Do not change Vulkan/CPU behavior.

## Active Team / tmux

Expected sessions:

- `pgu-director`: director
- `pgu-main`: main programmer
- `pgu-audit`: auditor
- `pgu-backlog`: backlog programmer
- `pgu-perf`: perf programmer
- `pgu-ui`: currently may be absent

Use:

```bash
scripts/directorctl status
tmux capture-pane -t pgu-main:0.0 -p -S -80
tmux capture-pane -t pgu-audit:0.0 -p -S -80
```

Prompt delivery note:

- Prefer `scripts/directorctl`.
- For raw tmux, use delayed `C-m` after paste/text send.
- If text lands but does not submit, send one additional `C-m` or `Enter`.
- Do not poll lanes; fire-and-forget prompts plus explicit callbacks.

## Current Workflow Rules To Preserve

- `docs/implementer-report.md` is a current-packet report only. Replace, do not append.
- `docs/todo.md` is pending work only. Remove completed todos; do not move them into a completed section.
- Auditors report findings only. Director decides incorporate/defer/reject/clarify.
- Auditor should not re-run expensive encodes/artifact generation unless the implementer artifact is stale, missing, or suspect.
- Perf/artifact work must check disk pressure first. Do not prune artifacts unattended unless the user approved exact names.
- Use separate branches/worktrees for concurrent lanes.

## Likely Next Decisions

1. Push or otherwise land local `main` containing Packet 36.161.
2. Decide whether to merge the CUDA notes branch into main as documentation, or fold the notes into the next CUDA diagnostics packet.
3. Prepare the CUDA diagnostics packet for main/GPU work.
4. Only after that, resume panoptic/static-dynamic mainline packets from the 36.xx sequence.

## Useful Commands

```bash
git status --short --branch
git log --oneline --decorate -8
git branch --all --contains bcb0cfe
scripts/directorctl status
scripts/report_viz_disk_usage.fish
```

Segment-stable review:

```bash
PGU_PLAYER_AUTO_CLOSE_MS=0 scripts/check_latest_visual_review.fish
```

Generate/review with cached inputs if needed:

```bash
scripts/run_packet_36_96_guest_panoptic_guard_review.fish
```

Avoid running the full 16-frame diagnostic just to audit script changes.
