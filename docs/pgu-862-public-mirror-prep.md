# PGU-862 Public Mirror And Fresh-Machine Prep

> Historical preparation record. SYRD-6 supersedes the mirror-only policy and
> local recovery URL below: GitHub is now canonical. See
> [the director handoff](switchyard-director-handoff.md) for the current workflow
> and [installation guidance](team-launcher.md#installing-the-entry-point) for recovery.

This note covers the layer below PGU-795's first-run manifest. PGU-795 tells
an operator which interactive account/model/trust steps are coming after
Switchyard can run; this note lists what a fresh machine needs before those
steps can succeed.

The director already scanned the repository and history for credentials and
cleared it for public mirroring. This document does not re-run or replace that
scan.

## Production Changes

- `scripts/install-all-repository-hooks.sh` no longer has a site-specific
  default for `SWITCHYARD_SHARED_CHECKOUTS`. The default is now empty, so a
  fresh machine does not try to install warning-only hooks into
  `/home/agent/Projects/pgu` or `/home/eric/Projects/pgu`.
- The shared-checkout override remains supported. Set
  `SWITCHYARD_SHARED_CHECKOUTS=/path/to/checkout:/path/to/other/checkout` when
  a host has explicit shared working checkouts that should receive the
  warning-only local hooks.
- The same script still discovers platform workflow bare repositories under
  `SWITCHYARD_BARE_REPO_ROOT` and registered project configs under
  `SWITCHYARD_PROJECT_REGISTRY_DIR`; those are the production rollout inputs.
- `docker/README.md` now marks the Docker setup as an unsupported historical
  path. The directory is retained for history, but should not be used for a
  fresh Switchyard install or public-mirror setup guidance.

## Fresh-Machine Prerequisites

These are derived from the installer, launcher, board service, provisioning,
and hook-management code.

### Base Shell Tools

- `bash` is required by `scripts/install-switchyard`,
  `scripts/install-all-repository-hooks.sh`, `scripts/ticket-board-service.sh`,
  and `scripts/ticket-board-notify-listener-service.sh`. Without it, the scripts
  cannot be executed by their shebangs.
- Core Unix tools used directly include `install`, `chmod`, `mv`, `find`,
  `ln`, `mktemp`, `tar`, `stat`, `readlink`, `awk`, `grep`, `sed`, `tee`,
  `touch`, and `tail`. Missing tools fail at the shell command that invokes
  them, usually as `command not found` or a non-zero shell exit.
- `curl` is used by generated project provisioning and board service smoke
  checks. A missing or failing `curl` breaks the final health check at
  `curl -fsS http://127.0.0.1:<port>/api/board`.

### Git And Source Repository

- `git` is required for installs, deploys, project initialization, hook
  rollout, commit verification, and worktree cleanup.
- `scripts/install-switchyard --apply` fails with
  `source repository is not a git checkout: <path>` if the source checkout is
  not a readable Git checkout.
- The same installer fails with
  `source switchyard is not executable: <path>/switchyard` if the checkout does
  not contain an executable `switchyard` entry point.
- Board service deploys fetch and resolve a deploy ref. If fetch fails, the
  service script prints
  `failed to fetch origin in <repo>; refusing stale deploy of <ref>`. If the ref
  cannot be resolved, it prints `failed to resolve <ref> in <repo>`.
- `switchyard new` initializes project repositories unless `--no-git-init` is
  used. Existing project directories with unusable Git metadata, no initial
  commit, or `--no-git-init` without a valid existing commit are rejected with
  explicit `switchyard: project path ...` messages.

### Sudo And Privileged Install Path

- Installing the shared `switchyard` command uses `sudo` by default and targets
  `/opt/switchyard` plus `/usr/local/bin/switchyard`.
- If a non-root user invokes a root-required wrapper command and cannot use
  sudo, the wrapper prints:
  `switchyard: command requires root: <command>` and
  `switchyard: sudo is unavailable for this user or shell; run it as a sudo-capable human or ask an operator`.
- When installing under `/usr` paths, `scripts/install-switchyard --apply`
  checks sudo's `secure_path`. If the install directory is absent, it fails with
  `<install-dir> is not in sudo secure_path`.
- `switchyard new` must run as root or through the wrapper's sudo path. Direct
  non-root execution fails with
  `switchyard: new requires sudo; re-run as \`sudo ./switchyard new\``.

### Systemd, Polkit, Linger, And Runtime Dirs

- The ticket board service is a system service and the notify listener is a
  user service. The provisioning code emits `systemctl`, `systemctl --user`,
  `loginctl enable-linger`, and `systemd-tmpfiles --create` commands.
- `scripts/ticket-board-service.sh` uses polkit for bounded system-service
  deploy/restart actions. If no interactive approval path is available, it
  fails with messages such as
  `system service action requires polkit approval from <user>'s active graphical session`,
  `systemctl <args> timed out waiting for polkit approval`, or
  `could not reach an interactive polkit approval flow`.
- User-service setup requires an active user systemd bus. The notify listener
  installer tries `loginctl enable-linger <user>` and then fails with
  `systemd user bus unavailable at /run/user/<uid>/bus` if the bus never
  appears.
- `switchyard new` refuses to modify an existing owner user whose linger is not
  already enabled:
  `switchyard: existing user '<user>' does not have linger enabled; switchyard refuses to modify an existing owner user`.
- If Switchyard creates the owner user itself and the system commands fail, it
  reports `switchyard: failed to create user '<user>'` or
  `switchyard: failed to enable linger for created user '<user>'`.

### PostgreSQL

- The board stores tickets in PostgreSQL and generated units use the local
  socket at `/var/run/postgresql`.
- `scripts/ticket-board-migrate` requires the `psql` client. If absent, it
  fails with `[ticket-board-migrate] ERROR: psql is required to apply ticket-board migrations`.
- Board and notify listener role setup also requires `psql`; without it,
  service scripts print `psql is required to ensure ticket-board service roles`.
- Role setup must connect as a PostgreSQL role with `CREATEROLE`, defaulting to
  the local `postgres` role. If that fails, the service scripts print:
  `failed to ensure ticket-board database roles using TICKET_BOARD_ADMIN_DATABASE_URL. This must connect as a PostgreSQL role with CREATEROLE`.
- Tenant provisioning emits commands such as
  `sudo -u postgres psql -X -v ON_ERROR_STOP=1`, so a host without a running
  PostgreSQL service, local socket, or usable admin role fails during database
  creation/migration/RBAC setup.
- At runtime, the Python ticket store requires `psycopg` 3.3 or newer and below
  4. If it is missing, the application raises
  `postgres ticket store requires psycopg3; install the 'psycopg' Python package`.

### Python And Runtime Dependencies

- The Python tooling assumes `/usr/bin/python3` in generated service units and
  `python3` on `PATH` in shell scripts.
- Several Python modules import `tomllib`, so the practical Python baseline is
  Python 3.11 or newer unless the code is changed to use a backport. On older
  Python versions, those modules fail during import with
  `ModuleNotFoundError: No module named 'tomllib'`.
- The Python board dependency file is `scripts/ticket_board/requirements.txt`
  and currently requires `psycopg>=3.3,<4`.
- `scripts/ticket-board-install-pane-hooks` can optionally preserve existing
  Hermes YAML configuration. If that path needs YAML preservation and PyYAML is
  missing, it fails with
  `PyYAML is required to preserve existing Hermes config: <path>`.

### Filesystem Permissions And ACLs

- Project provisioning creates board roots, asset directories, frame
  directories, owner config directories, systemd unit files, tmpfiles snippets,
  and polkit rules with `sudo install`.
- It grants the board service traversal and asset access using `setfacl`. A
  fresh machine therefore needs ACL tooling installed and a filesystem that
  supports the requested ACLs. If not, the generated provisioning command fails
  at the `sudo setfacl ...` step.
- The generated board service uses `ProtectHome=read-only` and
  `ReadWritePaths=<asset_dir> <frame_dir>`, so the provisioned paths must match
  the service unit and ACL grants.

### Ports, Sockets, And Existing State

- `switchyard new` performs a precheck before provisioning. It rejects common
  partial-install states with one combined message:
  `team-launcher: new project precheck failed:` followed by bullets such as
  `target user '<user>' does not exist`, `project repository <path> does not exist`,
  `database '<db>' already exists but <unit> is not installed`,
  `socket <path> already exists but <unit> is not installed`,
  `port <port> is already in use but <unit> is not installed`, or
  `<unit> is installed but database '<db>' does not exist`.
- Generated tenant boards default to a deterministic localhost port and verify
  with `curl -fsS http://127.0.0.1:<port>/api/board`, so the port must be free
  or owned by the expected board unit.

### Pane Sessions And GUI Launching

- `tmux` is required for role sessions and the viewer layout. Missing or failing
  tmux calls surface as failed `tmux ...` commands; several paths also print
  role-specific messages such as `tmux session <name> does not exist` or
  `failed to stop <role>: <session>: <reason>`.
- `konsole` is required only for the KDE/separate-window layout path. The
  launcher builds `konsole --layout <project>-konsole-layout.json`; if Konsole
  is not installed, that launch path fails at process start. The base
  `scripts/install-switchyard-prereqs` package set intentionally omits Konsole
  and prints a KDE-only install line instead. On Zorin 18.1 / Ubuntu 24.04
  noble, `apt-get install --dry-run konsole` listed 141 packages.
- A non-GUI or non-KDE environment can use the tmux viewer/headless paths, but
  still needs tmux.
- `python-is-python3` is not required: Switchyard invokes `python3`, and the
  bare `python` package name appears only in the pacman package set.

### Agent CLIs, Auth, Trust, And Models

- At least one configured role CLI must be installed for a useful project run.
  Supported first-run auth checks are:
  `agy models`, `claude auth status --json`, `codex login status`, and
  `hermes config check`.
- Missing CLIs are reported in the first-run manifest as
  `switchyard: missing CLI <cli>: install <cli> for owner user <owner>; affected roles: ...`.
- Auth setup is intentionally interactive and belongs to PGU-795's layer. The
  commands advertised by the code are `agy`, `claude auth login`,
  `codex login`, and `hermes model`.
- Folder trust is also interactive and role/workdir specific. The manifest
  reports it as `switchyard: folder trust <cli>: role <role> at <workdir>; ...`.
- Codex hook trust can require an explicit `/hooks` approval. The manifest says
  `codex hook trust needs <n> approval(s) ... run /hooks once in any Codex pane...`.
- Model probes can fail with `model probe failed with exit <code>` or
  `model probe did not confirm model-ok`.

### Optional TypeScript Board Port

- `ticket-board-ts/deno.json` defines an experimental Deno-based board path.
  That path needs Deno and npm dependencies `postgres@3` and `zod@3`.
- The current production Python board does not require Deno.

## Site-Specific References Intentionally Left

These references remain so Eric is not surprised by grep results in the public
mirror.

- `docker/Dockerfile`, `docker/README.md`, and `docker/entrypoint.sh`: retained
  as unsupported historical Docker/container material, now explicitly marked as
  not fresh-install guidance.
- PGU deployment/runbook material with local host paths remains in:
  `config/team-launcher/pgu.json`,
  `deploy/systemd/pgu-ticket-board-canary.service`,
  `deploy/systemd/pgu-ticket-board.service.boardsvc`,
  `scripts/install-directorctl`,
  `scripts/ticket-board-boardsvc-setup.sh`,
  `scripts/ticket-board-notify-listener-service.sh`,
  `scripts/ticket-board-service.sh`,
  `scripts/ticket_board/README.md`,
  `scripts/ticket_board/notify_listener.py`, and
  `scripts/ticket_board/server.py`. These are still PGU host/deploy paths, not
  generic public install defaults.
- Local recovery and policy examples under `/data/git` remain in
  `scripts/install-switchyard`, `scripts/install-update-hook.sh`,
  `scripts/report_file_size_limit.py`, `scripts/team_launcher.py`, and
  `scripts/ticket_board/app.py`. They document or implement the current
  operator-host topology and were not changed without a separate recovery-path
  design.
- Documentation examples and historical handoffs remain in
  `docs/director-handoff-2026-06-28-packet-36-36.md`,
  `docs/director-handoff-2026-07-02-current.md`,
  `docs/otto-milestone-1-hand-standup.md`,
  `docs/pgu-640-compaction-self-heal-rollout.md`,
  `docs/pgu-779-notify-listener-flake.md`,
  `docs/pgu-816-clear-sessionstart-evidence.md`,
  `docs/pgu-818-switchyard-shared-install-proposal.md`,
  `docs/pgu-841-switchyard-shared-install-cutover.md`,
  `docs/pgu-wayland-clipboard-access.md`,
  `docs/switchyard-project-setup-design.md`,
  `docs/tailscale-resolved-nm-runbook.md`,
  `docs/team-launcher.md`,
  `docs/ticket-board-boardsvc-peer-auth-runbook.md`,
  `docs/ticket-board-project-provisioning.md`,
  `docs/ticket-board-service.md`, and `docs/validator_audit.md`.
- Tests intentionally retain site/project fixture names such as `/home/eric`,
  `/home/agent`, `/data/git`, `otto-agent`, and `stellaris-agent` where they
  verify migration, prompt, path, or provisioning behavior. These are examples
  and regressions, not executed fresh-machine defaults.

## Not Fixed

- The unsupported Docker directory was not removed because it still documents
  the abandoned container topology. It now has an explicit warning instead.
- PGU-specific deployment scripts and systemd units were not generalized. They
  remain load-bearing for the current PGU host and should be handled by a
  separate deployment-template cleanup, not by this public-mirror prep.
- The installed wrapper's recovery command still points at the current local
  bare-repo topology (`/data/git/switchyard.git`). A public-mirror-friendly
  recovery URL needs an operator decision about the canonical public remote.
- Historical docs and fixture-heavy tests were not rewritten because the ticket
  explicitly allowed examples to remain when listed.
