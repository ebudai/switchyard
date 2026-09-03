# PGU-894 Deploy-Source Git Enumeration

Repository-wide scan used:

```bash
find scripts -type f -print0 | xargs -0 rg -n "git(-| |$)|git_source|SOURCE_REPO|DEPLOY_REF"
```

## User-Facing Switchyard Commands

- `switchyard new`
  - Reachable path: `scripts/ticket_board/project_provision.py` renders `operator-commands.sh`, which runs `scripts/ticket-board-service.sh deploy`.
  - Release-source risk: yes. When Switchyard is installed from `/opt/switchyard/releases/<sha>`, the generated command runs the service script from that exported release and passes the same release as `SOURCE_REPO`.
  - Correct behavior: deploy the tenant board from the exported release's `.switchyard-release.json` commit without invoking `git -C "$SOURCE_REPO"`. A non-release, non-checkout source must fail once on the source error.

- `switchyard upgrade`
  - Reachable path: `scripts/team_launcher.py` reports a tenant release update command. If the invoking Switchyard is a shared release, it already renders a clone-first command using the bare repo as the source for `ticket-board-service.sh deploy`.
  - Release-source risk: covered by PGU-891; retained.

- `switchyard status`
  - Reachable path: `scripts/team_launcher.py` reads release markers and uses owner-correct git probes for checkouts.
  - Release-source risk: no deploy mutation. Shared-release paths report marker state rather than probing git.

- `switchyard stop`
  - Reachable path: tmux/system process management only.
  - Release-source risk: none.

- `switchyard add-role`
  - Reachable path: project config/layout updates and pane launch. No board release export from `SOURCE_REPO`.
  - Release-source risk: none for ticket-board deploy.

- Board deploy by hand
  - Reachable path: `scripts/ticket-board-service.sh install|deploy|deploy-restart`.
  - Release-source risk: yes. The script must support both a clean git checkout and an exported Switchyard release.
  - Correct behavior: git checkout sources still fetch/archive `DEPLOY_REF`; exported release sources copy the release tree and use the release marker commit.

- Notify-listener deploy by hand
  - Reachable path: `scripts/ticket-board-notify-listener-service.sh install|deploy|deploy-restart`.
  - Release-source risk: yes. It has the same deploy-source shape as the board service.
  - Correct behavior: same as the board service.

## Non-Deploy Git Use

- `scripts/install-switchyard`
  - Purpose: installs a shared Switchyard release from a source checkout.
  - Release-source risk: intentionally requires a git checkout; it is the producer of exported releases, not a consumer during `switchyard new`.

- `scripts/team_launcher.py`
  - Purpose: launcher freshness, tenant release reporting, project repository initialization, model/auth checks, and owner-correct git operations.
  - Release-source risk: PGU-891 covered the release-source launcher paths. The remaining git calls are against launcher checkouts, project repositories, control repositories, or bare repositories with explicit path arguments.

- `scripts/ticket_board/app.py`, `scripts/ticket_board/write_client.py`, `scripts/ticket_board/worktree_cleanup.py`, `scripts/repository_hooks.py`, `scripts/report_file_size_limit.py`, and `scripts/refresh-stage3b-demo.sh`
  - Purpose: commit verification, write-client submit checks, cleanup planning, hook installation/reporting, and demo refresh.
  - Release-source risk: none. These do not receive the Switchyard deploy source used by `new`.
