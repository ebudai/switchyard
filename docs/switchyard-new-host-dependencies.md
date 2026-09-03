# switchyard new host dependencies

`switchyard new` is a privileged provisioning command. It should create the
project-specific state it owns and fail early, with a named remedy, when the
host is missing a prerequisite it cannot create safely.

This inventory was added for PGU-896 after a fresh Zorin install exposed that
the development host already had a `boardsvc` account created by hand. The
fresh host did not, so the first `setfacl -m u:boardsvc:...` failed with an
opaque `setfacl` parse error.

## Inventory

| Dependency | Required for | What creates it now |
| --- | --- | --- |
| `python3` | Switchyard, board service, migration runner, hooks | `scripts/install-switchyard-prereqs` installs the distro Python package. |
| `git` | Source checkout validation, control repositories, project worktrees | `scripts/install-switchyard-prereqs`. |
| `tmux` | Runtime panes and notification delivery targets | `scripts/install-switchyard-prereqs`. |
| `curl` | Generated board health check | `scripts/install-switchyard-prereqs`. |
| `psql` client | Database, schema, migration, workflow and RBAC setup | `scripts/install-switchyard-prereqs` via PostgreSQL client packages. |
| PostgreSQL server and the `postgres` OS user | Project database creation and schema application | Distro PostgreSQL installation/cluster setup; generated commands fail at the `sudo -u postgres psql` step if the server or admin path is unavailable. |
| shared Python venv at `/opt/switchyard/venv`, with `psycopg` and Pillow | Board service and notify listener interpreter; `DEFAULT_SHARED_PYTHON` is baked into the generated `ExecStart` of both units when it exists | `scripts/install-switchyard-prereqs` creates the venv and installs `psycopg`; Pillow comes from `python3-pil` / `python-pillow`. Distro `python3` alone is NOT sufficient -- a board started on it dies on `import psycopg` or `from PIL import Image` (PGU-882). |
| `setfacl`/ACL support | Granting board-service traversal and asset/frame access | `scripts/install-switchyard-prereqs` installs the `acl` package; the filesystem must support ACLs. |
| root/sudo authority | User creation, systemd unit installation, tmpfiles, polkit, database admin steps | The operator runs `sudo switchyard new`; `switchyard new` fails before mutation when it is not root. |
| board service OS user, default `boardsvc` | Systemd `User=`, owner-home traversal ACLs, asset/frame ACLs | `switchyard new` now ensures the system account exists before creating the project owner. Generated `operator-commands.sh` also creates it if absent before any ACL grant. |
| project owner OS user | Owns the project checkout, config, user service, hook/session state | `switchyard new` creates it when absent, enables linger, and validates the owner can run `sh -lc` against the project directory. Existing users are not modified. |
| owner linger and user systemd bus | Notify-listener user service | Created users get `loginctl enable-linger`; existing users must already have linger or provisioning fails with that remedy. |
| `/opt/switchyard` or another Switchyard release/source path | Shared command/runtime source used by generated project panes and board deploy | `scripts/install-switchyard` installs the shared release. Source checkouts or exported releases are accepted; arbitrary directories fail preflight. |
| `/etc/switchyard` registry | Discovering projects for `switchyard <project>` and status commands | `scripts/install-switchyard` creates the global config area; `switchyard new` registers each project. |
| systemd unit/tmpfiles/polkit paths | Running and managing the project board service | Generated `operator-commands.sh` installs per-project files under `/etc/systemd/system`, `/etc/tmpfiles.d`, and `/etc/polkit-1/rules.d`. |
| project database roles `ticket_board_service` and `ticket_board_listener` | Board write path and notify listener DB access | Generated `<project>-database.sql` creates them idempotently before creating the database. |
| PostgreSQL peer-auth mapping from OS user `boardsvc` to DB role `ticket_board_service` for the project database | Lets the systemd board service connect over the local PostgreSQL socket with `PGUSER=ticket_board_service` while running as `boardsvc` | `switchyard new` invokes `scripts/ticket-board-boardsvc-setup.sh --apply-peer-auth` before project owner/repo mutation. Generated `operator-commands.sh` runs the same helper before database creation and before the board health check. The helper installs a reachable, idempotent pg_hba rule before Debian's `local all all` catch-all: PostgreSQL 16+ uses an `include_if_exists` file, older clusters fall back to direct insertion. It also installs the pg_ident map and reloads PostgreSQL. This is a relationship between the OS user, DB role, DB name, ident map, and PostgreSQL config files; the role existing by itself is insufficient. |
| project database and `ticket_board` schema | Board storage | Generated commands create the database, apply `schema.sql`, run migrations, seed workflow, then apply `rbac.sql`. |
| project board release root | Deployed board code | Generated commands create the root and run `scripts/ticket-board-service.sh deploy`. |
| asset and frame directories | Durable attachments and frame uploads | Generated commands create these directories and grant `boardsvc` access when board-service traversal is enabled. |
| project repository | User-owned project checkout | `switchyard new` creates and initializes the repository unless told not to; lower-level `team-launcher new` requires one and fails preflight if it is absent. |

## Development-host state observed

On the PGU development host, these items already existed before this fix:

- `boardsvc:x:944:944::/nonexistent:/usr/sbin/nologin`
- `/opt/switchyard`
- `/etc/switchyard`
- `/data/git/switchyard.git`
- live board units such as `pgu-ticket-board.service` and `otto-ticket-board.service`
- tmpfiles snippets under `/etc/tmpfiles.d/*ticket-board.conf`
- live board paths such as `/home/agent/pgu-ticketboard-live`,
  `/home/agent/.claude/pgu-tickets-assets`, and `/tmp/pgu-frames`

Fresh machines must not depend on those historical artifacts. The service user
is now provisioned by `switchyard new`; the other shared install and package
dependencies are created by the installer/prereq scripts or reported by existing
preflight and generated-command failures.
