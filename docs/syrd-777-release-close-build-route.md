# SYRD-777: a release close reads the build id from `/api/build`, at any board size

On 2026-11-15 syrd deployed `95600e79` and served it, but `switchyard privileged-action syrd deploy-release` could not
close the release phase. It recorded the phase **blocked**: "the running board did not report a build id (... syrd's
board answered /api/board with more than 8388608 bytes)".

The cause was `release_alignment._live_board_build_id`. It read the board's whole `/api/board` just to take
`build_id`. Since SYRD-769, root reads it through `board_identity.board_opener`, whose answers are capped at 8 MiB
(`MAX_ANSWER_BYTES`). syrd's `/api/board` is 26.7 MB, nearly all of it tickets, so every root close of syrd failed.

## What changed

- `_live_board_build_id` asks `/api/build`: the board's build id alone, the same size on any board (SYRD-703). Root
  still asks the board root's unit names, proven by the kernel, on one fixed route, and shows the answer through
  `clean_text`. The 8 MiB cap is unchanged.
- Only text counts as a build id. A number, for example, is now "reported no build id", where before it was turned
  into the string `"7"`.
- `ROOT_READ_ROUTES` is now `/api/build`, `/api/workflow` and `/api/runtime-assignments`, and `/api/board` is no longer
  on it. The build id was the only root read of `/api/board` (see the survey below). As root, the opener now refuses
  `/api/board` by name on a board of any size. A reader that asks for it fails its first test, rather than only on a
  board large enough to pass the cap.
- A board that answers `/api/build` with 404 is refused, and the whole board is not asked instead. Every board
  release since SYRD-703 (2026-10-29) serves `/api/build`. A tenant that does not run one also fails to match the
  shared release, so its close is refused either way.
- Not as root, the reader asks `/api/build` of the configured `board_url`, as it asked `/api/board` before.

## Survey: every root reader of a tenant board

None of the other reads below grows with the board's tickets.

| reader | through | route |
|---|---|---|
| `release_alignment._live_board_build_id` | `board_opener` | `/api/build` (was `/api/board`) |
| `board_authority_preflight` (both), `provider_runtime_state` (both) | `board_connection_factory` | `/api/runtime-assignments` |
| `board_workflow_readers` (both) | `board_connection_factory` | `/api/workflow` |
| `presentation_runtime_assignments`, `presentation_role_recovery` | `board_opener` | `/api/runtime-assignments`, `/api/workflow` |
| `project_onboarding.director_onboarding_state` | `board_opener` | `/api/workflow` |
| `board_get_for`'s own build check (SYRD-703) | `board_get_for` | `/api/build` |
| `privileged_helper` | `board_get_for` | `/api/workflow`, `/api/runtime-assignments/<role>`, `/api/tickets/<id>` (one ticket) |
| `director_rebind`, `break_glass_role` | `board_get_for` | `/api/workflow`, `/api/runtime-assignments` |
| `project_status` | `board_get_for` | `/api/work-pause` |
| `user_notify` | `board_get_for` | `/api/user-events?after=…&limit=…` (bounded by its limit) |

The declared workflow and the runtime assignments grow with roles, not tickets. A root reader that needs ticket data
asks for the ticket it needs, as `privileged_helper` does with `/api/tickets/<id>`.

## Tests

`tests/release_close_build_route_test.py` runs the real close (`team_launcher.close_release_phase`, as root, with no
opener) against `release_phase_journal_test`'s tenant and SYRD-769's rig. Every board's `/api/board` is over the cap.

- The phase closes `done` against the board's build, and only `/api/build` of the unit's board is asked.
- These `/api/build` answers refuse and record the phase blocked, as plain text:
  - another build;
  - no build id, a number, or a non-mapping;
  - an escape-laden build id;
  - a 404.
- Not as root, the configured URL's `/api/build` is asked.
- As root, `/api/board` is refused.

`root_board_readers_test`, `release_alignment_boundary_test` and `deploy_release_board_readiness_test` now expect the
`/api/build` route.
