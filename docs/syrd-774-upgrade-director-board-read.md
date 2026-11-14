# SYRD-774: `switchyard upgrade`, as root, reads the Director phase from the board root's unit names

A follow-up to SYRD-769, found by Audit in its round-2 review. It is the same class as SYRD-769's two gaps.

## The reader
`upgrade_phases` asks `project_onboarding.director_onboarding_state` whether the Director's onboarding migration is
done, and records the answer as the `director` phase in root's upgrade record.

It read `/api/workflow` from the tenant configuration's `board_url` (`launcher._open_board_url`). A tenant could point
that at a board it controls, and so choose the phase root records. On failure it printed the exception's raw text,
which can include a board's status line or reason phrase.

Now:
- it asks through `board_identity.board_opener(config, opener)`, as SYRD-769's URL readers do. As root that is the
  socket root's unit names, answered (`SO_PEERCRED`) by the unit's service account in its main process, and the fixed
  route `/api/workflow`; nothing of the tenant's URL is asked. Not as root, the configured URL is used, as before;
- its failure text goes through `phone_text.clean_text`.

## The survey: other root-run board reads in `switchyard upgrade`
I traced `upgrade_phases`' call graph four levels deep, through every function it calls on the launcher and every
function those call, looking for reads of `config.board_url` or `config.board_socket`, `_open_board_url`, `urlopen` or
`UnixHTTPConnection`:

| path | why it is safe |
|---|---|
| `director_onboarding_state` | fixed here |
| `director_phase_required`, `upgrade_phase_report` → `declared_workflow_presence` → `read_board_declared_workflow` | SYRD-769's socket reader: as root, root's unit socket |
| `migrate_declarative_director_onboarding` | as root it refuses before any board read (a director-authority write) |
| `report_tenant_release_upgrade` → `tenant_release_status` | reads root's plan and the release on disk; its `board_socket` is copied from root's plan, not read |
| `_plan_data_from_config`, `load_project_config` → `_role_board_env` | copy the address into plan data or a pane's environment; nothing is connected to |
| `role_restart` (not called by `upgrade`) | as root it re-runs as the owner through `runuser` (noted on the ticket) |

## Tests
`tests/upgrade_director_board_read_test.py` uses SYRD-769's rig:
- **as root:** the unit's board is asked `/api/workflow` once, nothing the configuration names is connected to, and the
  unit's marked document decides `done`;
- **not root:** the configured URL is asked, as before;
- **a tenant URL** whose path or query points elsewhere still has root ask exactly `/api/workflow`;
- **another process answering** the unit's socket gives `unknown`, saying why;
- **an escape-laden status line or reason phrase** prints inert, as root and not.
