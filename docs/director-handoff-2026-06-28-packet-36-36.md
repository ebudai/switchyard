# Director Handoff - 2026-06-28 - Packet 36.36

**Provenance note (added 2026-07-05):** This file is handoff material from a *different, unrelated project* (internally called SRX), copied into this repo's `docs/` folder with a mechanical case-insensitive `srx`→`pgu` text replacement. It is retained only as process-pattern reference. None of the project specifics below describe this repository's actual history — this repo's real project is the procedural galaxy/point-cloud renderer in `main.cpp` (see `docs/galaxy_architecture.md`).

## Current State

The project is in the object-ownership / background-billboarding phase. The current strategic order is:

1. Establish object segmentation and permanence first.
2. Treat background as whatever remains after object extraction, not as a depth-only far-plane threshold.
3. Use background billboarding after ownership is reliable.
4. Return to surfel cloud merging and atlasing once object/background ownership is stable.

The immediate blocker is object ownership recall. The proposal masks are now source-matched and useful, but they still miss many real objects or only outline them. The next packet is intended to expand recall using model proposals plus mechanical/depth/thin-structure evidence.

## Current Packet

Packet file:

- `/home/budai/Projects/PGU/docs/packets/packet_36_36_proposal_recall_expansion.md`

Runner:

- `/home/budai/Projects/PGU/scripts/run_packet_36_36_proposal_recall_expansion.fish`

Current branch when this handoff was written:

- `packet-36-36-proposal-recall-expansion`

Current uncommitted packet work included:

- `pgu_tests/src/test_atlas_benchmark_object_permanence.cpp`
- `docs/packets/packet_36_36_proposal_recall_expansion.md`
- `scripts/run_packet_36_36_proposal_recall_expansion.fish`

The user said the packet was written and will be sent to the programmer. Do not assume it has been implemented yet until the programmer reports back.

## Packet 36.36 Intent

Create a recall-expanded object ownership diagnostic for the playground/iPhone-derived window.

Preserve the existing useful layers:

- `object_first_background_mask`
- `model_assisted_object_first_background_mask`
- `proposal_outline_overlay`
- `proposal_component_overlay`
- `mechanical_recall_candidate_mask`
- `thin_structure_candidate_mask`
- `proposal_recall_uncertain_mask`
- `proposal_component_reject_reason_mask`

Add expanded-recall layers:

- `proposal_recall_expanded_object_mask`
- `proposal_recall_expanded_background_mask`
- `proposal_recall_expansion_added_mask`
- `proposal_recall_expansion_removed_mask`
- `proposal_recall_expanded_overlay`
- `proposal_recall_expansion_reason_mask`
- `proposal_recall_expanded_uncertain_mask`
- Optional: `proposal_recall_expanded_background_prediction`
- Optional: `proposal_recall_expanded_prediction_error`

Useful metrics:

- `recall_expanded_object_fraction`
- `recall_expansion_added_fraction`
- `recall_expansion_background_removed_fraction`
- `mechanical_candidate_promoted_fraction`
- `uncertain_promoted_fraction`
- `reject_reason_promoted_fraction`
- `thin_structure_promoted_fraction`
- `recall_expanded_component_count`

Acceptance is visual first: far bench, near bench slats, trees, poles, thin structures, and people should be owned more consistently. Sky should remain background. Broad ground should not be swallowed.

## Recent Visual Findings

The user inspected the source-matched proposal views and found:

- `proposal_outline_overlay` now lines up with the correct source frame.
- The overlay is useful but still leaves much of the frame unsegmented.
- Trees are outlined but not reliably owned.
- The woman's blouse and sand/playground patches were outlined/misclassified in earlier views.
- The far bench is missed.
- The near bench has inconsistent slat segmentation.
- `proposal_component_reject_reason_mask` is very useful for understanding why candidate regions are not being accepted.
- `object_first_background_prediction_region_promoted` looks good until about frame 7, where smearing starts.

Director assessment: the idea is sound, but the recall path needs better use of existing evidence. Do not jump straight to production mask bitstream work yet.

## Important Principle

Depth should help with sky/far priors, but it should not be the primary ownership decision for background. The previous bug pattern was depth-only far pixels being treated as background even when they belonged to objects. That caused moving benches/people/playground equipment to smear through the billboard prediction.

Mechanical outline masks are not failures. They are useful boundary evidence. The missing piece is turning outline/reject/thin-structure evidence into filled, stable object ownership.

## Current Validation Assets

Host/guest shared viz mount:

- `/mnt/host_viz/`

Useful reusable host inputs:

- `/home/eric/viz/packet_36_25_far_region_interior_object_ownership_20260627_165932/input_frames`
- `/home/eric/viz/packet_36_25_far_region_interior_object_ownership_20260627_165932/input_frames/depth`

SAM2 external host env:

- Root: `/home/eric/pgu_external/sam2`
- Venv: `/home/eric/pgu_external/sam2/venv`
- Repo: `/home/eric/pgu_external/sam2/src/sam2`
- Checkpoint: `/home/eric/pgu_external/sam2/checkpoints/sam2.1_hiera_small.pt`
- Config: `configs/sam2.1/sam2.1_hiera_s.yaml`

The bare config name caused a Hydra lookup failure. Use the full config path above.

SAM2/Python notes:

- This is diagnostic/offline only.
- Production should not depend on a Python model in the decoder.
- If this approach proves useful, later work should consider ONNX/compiled encoder-side inference or bitstreamed compressed masks.
- A torchvision non-writable NumPy warning was seen; probably benign, but a later cleanup can copy arrays before tensor conversion.
- A CUDA OOM happened on a 4070 when VRAM was fragmented/stale. Freeing/rebooting fixed it.

## Workflow Rules

Programmer lanes:

- Primary/lead: usually Opus 4.8 high; use Codex 5.5 high/xhigh only for unusually hard packets.
- UI: Opus 4.6 high.
- Perf: Codex 5.4 or 5.5 high.
- Backlog may switch to Gemini.

Keep lanes in separate folders/branches. There was a recent cross-lane contamination where UI and primary work landed in one mixed commit/report. Avoid sharing a branch or working directory between primary and UI.

### Current tmux workflow, tested 2026-06-29

The user moved the standing agents into tmux. Prefer tmux routing over `wl-copy`, CLion PTY injection, or desktop SendKeys.

Known sessions after setup:

```bash
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} current=#{pane_current_path} cmd=#{pane_current_command}'
```

Expected names:

- `pgu-director`: Codex director.
- `pgu-audit`: auditor (`agy`).
- `pgu-ui`: UI programmer (`claude`).
- `pgu-backlog`: backlog programmer (`claude`).

Send prompts with:

```bash
prompt='Please do ...'
tmux set-buffer -- "$prompt"
tmux paste-buffer -t pgu-backlog:0.0
tmux send-keys -t pgu-backlog:0.0 Enter
```

For short callbacks back to the Codex/director pane, use literal text plus `C-m`; the older `paste-buffer` or trailing `Enter` forms can leave the callback line waiting for a manual Enter press:

```bash
tmux send-keys -t pgu-director:0.0 -l 'audit clean: packet NN'
tmux send-keys -t pgu-director:0.0 C-m
```

Use this for all short status callbacks (`audit clean`, `audit findings`, `backlog complete`, `ui complete`, and similar).

Capture status with:

```bash
tmux capture-pane -t pgu-backlog:0.0 -p -S -120
```

Do not use `ydotool`/desktop keystrokes unless the exact target window is known. A prior desktop-send attempt landed in the Codex/director pane because focus was on the wrong terminal. CLion PTY master writes were also unreliable for Claude/agy.

For independent backlog or UI tests, create a separate worktree from `origin/main` rather than using the active primary checkout:

```bash
git fetch origin
git worktree add -b backlog/<short-name> /home/budai/Projects/PGU-<short-name> origin/main
```

Task the worker with the dedicated worktree path in the prompt. This avoids contaminating active packet branches.

The full tmux workflow was tested with Packet 36.41:

- Worktree: `/home/budai/Projects/PGU-backlog-tmux-test`
- Branch: `backlog/uint16-frame-dimension-doc`
- Backlog task: document the `uint16_t` frame dimension limit in `FramePayload`.
- Auditor: `pgu-audit` performed a clean scoped audit.
- Branch commit: `491bc2d Document frame dimension bitstream limit`
- Main merge: `05166b5 Merge packet 36.41 backlog frame dimension docs`

This test proved the desired loop:

1. Director writes packet in the dedicated worktree.
2. Director sends prompt through tmux to the lane agent.
3. Lane agent implements and reports completion.
4. Director writes/sends audit packet through tmux to `pgu-audit`.
5. Director rules on findings. Clean audit needs no disposition.
6. Director commits branch, pushes branch, merges to `main`, and pushes `main`.

When merging a side worktree into `main`, remember the active primary packet branch may now be behind `origin/main`; the main programmer may need to merge/rebase before their final push.

Packets should be written to:

- `/home/budai/Projects/PGU/docs/packets/packet_<number>_<name>.md`

Clipboard convention:

```text
please read {link} and follow the instructions therein
```

The user strongly prefers scripts over long commands. If a host run is needed, write a fish script, make it executable, and have it print the artifact root and player command.

## Audit Rules

Audits should start clean each time:

- Do not retain old findings.
- Do not rank severity.
- Do not say what “should fix now.”
- Do not prescribe implementation unless asked.
- Report findings only.
- Auditor should read the decision log and the packet.

Only one role should run tests. Other roles should read results. Avoid full test-suite runs unless specifically justified.

Director disposition rule as of 2026-06-29:

- For clean audits, proceed normally.
- For findings, the director chooses `incorporate`, `defer`, `reject`, or `clarify`.
- Rejections require consensus with the auditor.
- The programmer should not be asked to decide whether to incorporate or defer findings.

## Repo Hygiene

Generated viz outputs are huge and should stay outside the repo:

- `/home/eric/viz`
- `/home/budai/viz`
- `/mnt/host_viz`

Avoid committing generated masks, PPMs, checkpoints, or bulky intermediate data unless explicitly requested.

Use `apply_patch` for manual edits. Do not revert changes from other lanes unless the user explicitly asks.

## Near-Term Next Steps

1. Let the programmer implement Packet 36.36.
2. Audit Packet 36.36 normally.
3. Run the 36.36 script on the host/reused playground window.
4. Inspect:
   - `proposal_recall_expanded_overlay`
   - `proposal_recall_expanded_object_mask`
   - `proposal_recall_expanded_background_mask`
   - `proposal_recall_expansion_reason_mask`
   - `proposal_recall_expanded_uncertain_mask`
5. Decide whether expanded recall is enough to feed object-first background prediction.

If recall still misses obvious objects, do not keep chasing arbitrary morphology. Consider a clearer encoder-side segmentation model path, with deterministic decoder-side mask consumption.

## Longer-Term Backlog Direction

Useful future ideas already discussed:

- Object segmentation/permanence before billboarding.
- Background atlas/billboard after object removal.
- Foreground affine-transformed billboard/object cards for planar-ish foreground regions, possibly cheaper than surfels.
- Two error layers:
  - object/statistic correction layer;
  - pixel correction layer.
- Object ID color persistence diagnostic.
- Mask temporal jacobian to catch chatter.
- Boundary-gated residual upsampling to prevent foreground/background leakage.
- Cloud merging and atlasing remain the core compression thesis, but should wait until ownership is stable.
