#!/usr/bin/env python3
"""SYRD-45 end to end: prepare, cut over, verify and roll back a real tenant.

The mocked suite could not have caught what the rollout hit, because the parts
that fail are the ones a stub replaces: credentials that have to land before the
configuration names the accounts, worktrees that must not change hands while
workers are live, sessions that have to come back under a different uid, and a
rollback that has to put the filesystem back and not just the file.

This runs in a user namespace where this process is root, uses accounts that
really exist with really distinct uids, starts real tmux servers, runs real
processes under those uids, and reads every uid back from the kernel.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher
from scripts.ticket_board.server import (
    LocalRoleAuthority,
    TicketBoardEventHub,
    TicketBoardUnixServer,
)


def _distinct_accounts(count: int) -> list[str]:
    seen: dict[int, str] = {}
    for entry in pwd.getpwall():
        if 0 < entry.pw_uid < 1000 and entry.pw_uid not in seen:
            seen[entry.pw_uid] = entry.pw_name
    names = [seen[uid] for uid in sorted(seen)][:count]
    assert len(names) == count, f"need {count} distinct unprivileged accounts, found {names}"
    return names


class _RealRunner:
    """Run commands for real, translating `sudo -u` into a real identity change.

    The launcher asks for a role's account through sudo. Inside the namespace
    there is no sudo, so the same request is honoured with setpriv, which is a
    real setuid -- the processes it starts are genuinely owned by that account.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        argv = list(args)
        self.calls.append(argv)
        if argv[:2] == ["sudo", "-u"]:
            account = argv[2]
            rest = argv[4:] if len(argv) > 3 and argv[3] == "-H" else argv[3:]
            entry = pwd.getpwnam(account)
            argv = [
                "setpriv",
                f"--reuid={entry.pw_uid}",
                f"--regid={entry.pw_gid}",
                "--clear-groups",
                *rest,
            ]
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
        kwargs.setdefault("text", True)
        return subprocess.run(argv, **kwargs)


def _tenant(tmp: Path, accounts: list[str], owner: str) -> tuple[Path, dict[str, str]]:
    """A legacy tenant: every role runs as the project owner, as one really does."""
    roles = ["main", "app"]
    mapping = dict(zip(roles, accounts))
    worktrees = tmp / "worktrees"
    layout = tmp / "porter-layout.json"
    layout.write_text(json.dumps({"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}), encoding="utf-8")
    for role in roles:
        (worktrees / role).mkdir(parents=True)
        (worktrees / role / "file").write_text("work\n", encoding="utf-8")
    payload = {
        "desktop_access": {"mode": "headless"},
        "project": "porter",
        "layout": str(layout),
        "repository": str(tmp / "repo"),
        "session_dir": str(tmp / "sessions"),
        "run_as_user": owner,
        "roles": [
            {
                "role": role,
                "slot": index,
                "cli": ["codex"],
                "live_commands": ["codex"],
                "target": f"porter-{role}:0.0",
                "tmux_session": f"porter-{role}",
                "workdir": str(worktrees / role),
            }
            for index, role in enumerate(roles)
        ],
    }
    (tmp / "repo").mkdir()
    (tmp / "sessions").mkdir()
    config_path = tmp / "porter.json"
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config_path, mapping


def _write_pending(config, mapping: dict[str, str], homes: Path, config_path: Path) -> None:
    path = team_launcher.pending_identities_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    roles = {
        role: {
            "account": account,
            "home": str(homes / account),
            "worktree": next(
                r.workdir for r in config.roles if r.role == role
            ),
        }
        for role, account in mapping.items()
    }
    path.write_text(
        json.dumps(
            {
                "schema": team_launcher.PENDING_IDENTITIES_SCHEMA,
                "project": "porter",
                "owner": config.run_as_user,
                "roles": roles,
            }
        ),
        encoding="utf-8",
    )


def _owner_credentials(owner_home: Path, owner: str) -> None:
    """The owner's real credential, owned by the owner as the seeder requires."""
    entry = pwd.getpwnam(owner)
    credential = owner_home / ".codex" / "auth.json"
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text(json.dumps({"token": "owner-token"}), encoding="utf-8")
    credential.chmod(0o600)
    credential.parent.chmod(0o700)
    owner_home.chmod(0o700)
    for path in (owner_home, credential.parent, credential):
        os.chown(path, entry.pw_uid, entry.pw_gid)


def _session_launcher(mapping: dict[str, str], runner: _RealRunner, *, wrong_uid_for: str = ""):
    """Start each role's session under its own account, for real."""

    def launch(config, **_kwargs) -> int:
        for role in config.roles:
            account = mapping.get(role.role, "")
            socket = f"syrd45-{role.role}"
            runner(["tmux", "-L", socket, "kill-server"], check=False)
            command = ["tmux", "-L", socket, "new-session", "-d", "-s", role.tmux_session, "sleep 300"]
            if account and role.role != wrong_uid_for:
                command = ["sudo", "-u", account, "-H", *command]
            if runner(command).returncode != 0:
                return 1
        return 0

    return launch


def _stopper(mapping: dict[str, str], runner: _RealRunner):
    def stop(config, **_kwargs) -> int:
        for role in config.roles:
            account = mapping.get(role.role, "")
            command = ["tmux", "-L", f"syrd45-{role.role}", "kill-server"]
            if account:
                command = ["sudo", "-u", account, "-H", *command]
            runner(command, check=False)
        return 0

    return stop


def _pane_uid(role: str, mapping: dict[str, str], runner: _RealRunner) -> int | None:
    account = mapping[role]
    proc = runner(
        ["sudo", "-u", account, "-H", "tmux", "-L", f"syrd45-{role}", "display-message", "-p",
         "-t", f"porter-{role}:0.0", "#{pane_pid}"]
    )
    if proc.returncode != 0:
        return None
    return team_launcher.process_uid(int(proc.stdout.strip()))


def _identity_runner(mapping: dict[str, str], runner: _RealRunner):
    """Route the launcher's per-role probes at that role's own tmux server."""

    def call(args, **kwargs):
        argv = list(args)
        if argv[:2] == ["sudo", "-u"]:
            account = argv[2]
            rest = argv[4:] if len(argv) > 3 and argv[3] == "-H" else argv[3:]
            role = next((name for name, acct in mapping.items() if acct == account), "")
            if role and rest[:1] == ["tmux"]:
                rest = ["tmux", "-L", f"syrd45-{role}", *rest[1:]]
                argv = [*argv[:4], *rest] if argv[3] == "-H" else [*argv[:3], *rest]
        return runner(argv, **kwargs)

    return call


def _board_write_accepted(socket_path: Path, account: str, role: str) -> tuple[int, str]:
    """Ask the running board, as that account, whether it is that role.

    Forked and setuid so the connection really carries the account's uid: this
    is the check the authority table performs, and the reason a cutover that
    leaves the old processes in place is an outage.
    """
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            entry = pwd.getpwnam(account)
            os.setgid(entry.pw_gid)
            os.setuid(entry.pw_uid)
            sys.path.insert(0, str(ROOT / "tests"))
            from ticket_board_pid_identity_test import request_unix

            status, body = request_unix(socket_path, "/api/register-caller", {"role": role})
            os.write(write_fd, f"{status}\n{body}".encode("utf-8"))
        except BaseException as exc:  # noqa: BLE001 - reported through the pipe
            os.write(write_fd, f"0\n{exc}".encode("utf-8"))
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as handle:
        payload = handle.read().decode("utf-8", "replace")
    os.waitpid(child, 0)
    status, _, body = payload.partition("\n")
    return int(status or 0), body


def end_to_end(owner: str) -> None:
    assert os.geteuid() == 0, "this case must run as root inside the namespace"
    accounts = _distinct_accounts(2)
    with tempfile.TemporaryDirectory(prefix="syrd45-e2e-") as tmp:
        tmp_path = Path(tmp)
        tmp_path.chmod(0o755)
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(tmp_path / "etc-switchyard")
        homes = tmp_path / "homes"
        homes.mkdir()
        owner_home = homes / owner
        owner_home.mkdir(parents=True)
        _owner_credentials(owner_home, owner)
        config_path, mapping = _tenant(tmp_path, accounts, owner)
        # What the preparation artifact creates before any credential is seeded:
        # each role's own home, owned by that account, 0700.
        for account in accounts:
            role_home = homes / account
            role_home.mkdir()
            entry = pwd.getpwnam(account)
            os.chown(role_home, entry.pw_uid, entry.pw_gid)
            role_home.chmod(0o700)
        config = team_launcher.load_project_config("porter", config_path)
        _write_pending(config, mapping, homes, config_path)

        original_account_name = team_launcher.role_account_name
        team_launcher.role_account_name = lambda project, role: mapping.get(role, f"{project}-{role}")
        runner = _RealRunner()
        try:
            ownership_before = {
                role.workdir: os.stat(role.workdir).st_uid for role in config.roles
            }

            # The preparation artifact must not move a worktree: that would take
            # write access from a live worker before anything is quiesced.
            artifact = team_launcher.render_role_account_migration(config)
            for role in config.roles:
                assert f"chown -R" not in artifact or role.workdir not in artifact, artifact
            assert "switchyard cutover-roles porter" in artifact, artifact

            # PREPARATION, while the configuration still names no accounts.
            assert all(not role.run_as_user for role in config.roles)
            printed: list[str] = []
            team_launcher.switchyard_seed_role_credentials_command(
                config, home_base=homes, runner=runner, print_func=printed.append
            )
            for role, account in mapping.items():
                seeded = homes / account / ".codex" / "auth.json"
                assert seeded.is_file(), (role, "\n".join(printed))
                assert json.loads(seeded.read_text())["token"] == "owner-token"
                info = os.stat(seeded)
                assert info.st_uid == pwd.getpwnam(account).pw_uid, (role, info.st_uid)
                assert info.st_mode & 0o777 == 0o600, oct(info.st_mode)
                assert os.stat(seeded.parent).st_mode & 0o777 == 0o700
            assert not any("nothing to seed" in line for line in printed), printed

            # ... and nothing has changed hands yet.
            assert {
                role.workdir: os.stat(role.workdir).st_uid for role in config.roles
            } == ownership_before

            # THE CUTOVER, for real: stop, transfer, restart under the new
            # accounts, and read every uid back from the kernel.
            identity_runner = _identity_runner(mapping, runner)
            result = team_launcher.cutover_role_identities_command(
                config,
                config_path=config_path,
                runner=identity_runner,
                launcher=_session_launcher(mapping, runner),
                stopper=_stopper(mapping, runner),
                print_func=printed.append,
            )
            assert result == 0, "\n".join(printed[-12:])
            after = json.loads(config_path.read_text(encoding="utf-8"))
            assert {role["role"]: role["run_as_user"] for role in after["roles"]} == mapping, after
            for role, account in mapping.items():
                expected = pwd.getpwnam(account).pw_uid
                workdir = next(r["workdir"] for r in after["roles"] if r["role"] == role)
                assert os.stat(workdir).st_uid == expected, (role, os.stat(workdir).st_uid)
                assert _pane_uid(role, mapping, runner) == expected, role
                # Exactly one session per role: the old server was replaced, not
                # duplicated.
                listed = runner(
                    ["sudo", "-u", account, "-H", "tmux", "-L", f"syrd45-{role}", "list-sessions", "-F", "#{session_name}"]
                )
                assert listed.stdout.split() == [f"porter-{role}"], listed.stdout

            # THE BOARD: the authority table names those accounts, and the
            # processes now serving the roles are running as them, so their
            # writes are accepted -- and the project account, which is what the
            # old workers had, is not a role at all.
            sys.path.insert(0, str(ROOT / "tests"))
            from ticket_board_pid_identity_test import MemoryBoardApp, QuietNotifier, ticket_payload

            board_dir = tmp_path / "board"
            (board_dir / "frames").mkdir(parents=True)
            (board_dir / "assets").mkdir(parents=True)
            socket_path = board_dir / "board.sock"
            app = MemoryBoardApp(
                [ticket_payload("POR-1", title="Work", state="in_progress", assignee="main")],
                board_dir / "frames",
                board_dir / "assets",
            )
            authority = LocalRoleAuthority(dict(mapping))
            server = TicketBoardUnixServer(
                socket_path,
                app,
                events=TicketBoardEventHub(app),
                director_notifier=QuietNotifier(),
                role_authority=authority,
            )
            os.chmod(board_dir, 0o755)
            os.chmod(socket_path, 0o666)
            import threading

            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for role, account in mapping.items():
                    status, body = _board_write_accepted(socket_path, account, role)
                    assert status == 200, (role, status, body)
                    assert f'"role": "{role}"' in body, body
                # The identity the old workers had is not a role on this board:
                # refused at the socket, or answered 403 if it gets that far.
                status, body = _board_write_accepted(socket_path, owner, "main")
                assert status in (0, 403), (status, body)
                assert '"role": "main"' not in body, body
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            # THE ROLLBACK: one role comes back under the wrong identity.
            _stopper(mapping, runner)(config)
            config_path.write_text(
                json.dumps(
                    {**json.loads(config_path.read_text()),
                     "roles": [{k: v for k, v in role.items() if k != "run_as_user"} for role in after["roles"]]},
                    indent=2, sort_keys=True,
                ) + "\n",
                encoding="utf-8",
            )
            for path, uid in ownership_before.items():
                subprocess.run(["chown", "-R", str(uid), path], check=True)
            config = team_launcher.load_project_config("porter", config_path)
            before_bytes = config_path.read_bytes()
            result = team_launcher.cutover_role_identities_command(
                config,
                config_path=config_path,
                runner=identity_runner,
                launcher=_session_launcher(mapping, runner, wrong_uid_for="app"),
                stopper=_stopper(mapping, runner),
                print_func=printed.append,
            )
            assert result == 1, "\n".join(printed[-12:])
            assert config_path.read_bytes() == before_bytes, "the configuration was not put back"
            assert {
                role.workdir: os.stat(role.workdir).st_uid for role in config.roles
            } == ownership_before, "the worktrees were not put back"
        finally:
            team_launcher.role_account_name = original_account_name
            for role in ("main", "app"):
                subprocess.run(["tmux", "-L", f"syrd45-{role}", "kill-server"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    owner = pwd.getpwuid(os.geteuid()).pw_name
    command = [sys.executable, str(Path(__file__).resolve()), "--namespace-child", owner]
    if os.geteuid() != 0:
        command = ["unshare", "--user", "--map-auto", "--map-root-user", *command]
    subprocess.run(command, check=True)
    print("team_launcher_identity_cutover_e2e_test: ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--namespace-child":
        end_to_end(sys.argv[2])
    else:
        raise SystemExit(main())
