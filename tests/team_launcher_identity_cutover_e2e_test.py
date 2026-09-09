#!/usr/bin/env python3
"""End-to-end coverage for repatriating a legacy dedicated-account tenant.

This runs in a user namespace where the test process is root, starts real tmux
servers under distinct legacy accounts, and reads pane ownership back from the
kernel. The audited contract is fail-closed while those panes are live, then a
checkpointed move to the single project account without deleting old accounts.
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
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

# Every pane this suite opens would otherwise ask the live tenant's systemd
# user manager for a transient scope, however private its tmux socket is
# (SYRD-55).
from tmux_bus_isolation import isolate_tmux_bus

isolate_tmux_bus()

from scripts import team_launcher


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
        self.deploys: list[str] = []
        self.listener_calls: list[str] = []

    # Socket PATHS, not names: /tmp/tmux-<uid> belongs to the host and tmux
    # refuses it inside the namespace, and using it would also reach the real
    # server of the account this test borrows.
    SOCKET_DIR = Path("/tmp")
    OWNER_SOCKET = "syrd45-owner"

    board_socket: Path | None = None
    mapping: dict[str, str] | None = None
    release_pointer: Path | None = None
    release_target: Path | None = None
    # The owner's user listener: a real unit file on disk, and a state this
    # models because a user manager is not available in a namespace.
    listener_active = True

    @classmethod
    def socket_for(cls, name: str) -> str:
        return str(cls.SOCKET_DIR / f"{name}.sock")

    @staticmethod
    def _isolated(argv: list[str]) -> list[str]:
        """Never touch a real tmux server: this test runs on its own sockets."""
        if argv[:1] == ["tmux"] and "-S" not in argv:
            return ["tmux", "-S", _RealRunner.socket_for(_RealRunner.OWNER_SOCKET), *argv[1:]]
        return argv

    def __call__(self, args, **kwargs):
        argv = list(args)
        self.calls.append(argv)
        if argv[:1] == ["install"] and len(argv) >= 3:
            # `install -D` creates the destination directory.
            Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(argv[-2], argv[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")
        joined = " ".join(argv)
        if "notify-listener" in joined and "systemctl --user" in joined:
            self.listener_calls.append(joined)
            if "is-active" in joined:
                return subprocess.CompletedProcess(
                    argv, 0 if self.listener_active else 3,
                    "active\n" if self.listener_active else "inactive\n", "",
                )
            if "stop" in joined:
                self.listener_active = False
                return subprocess.CompletedProcess(argv, 0, "", "")
            self.listener_active = True
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "deploy-restart" in joined and self.release_pointer is not None:
            # What deploy-restart does to the release pointer, done for real so
            # the transaction's capture and restore are exercised.
            staged = self.release_pointer.with_name(".current.new")
            if staged.exists() or os.path.islink(staged):
                staged.unlink()
            os.symlink(self.release_target, staged)
            os.replace(staged, self.release_pointer)
            self.deploys.append(str(self.release_target))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["systemctl", "is-active"]:
            # No systemd in a user namespace; the board this case verifies
            # against is the one it started itself.
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        if argv[:1] == ["systemctl"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["sudo", "-u"]:
            account = argv[2]
            rest = argv[4:] if len(argv) > 3 and argv[3] == "-H" else argv[3:]
            entry = pwd.getpwnam(account)
            argv = [
                "setpriv",
                f"--reuid={entry.pw_uid}",
                f"--regid={entry.pw_gid}",
                "--clear-groups",
                "env",
                f"HOME={entry.pw_dir}",
                f"USER={account}",
                f"LOGNAME={account}",
                "SHELL=/bin/sh",
                *self._isolated(rest),
            ]
        else:
            argv = self._isolated(argv)
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
        kwargs.setdefault("text", True)
        return subprocess.run(argv, **kwargs)


def _tenant(tmp: Path, accounts: list[str], owner: str) -> tuple[Path, dict[str, str]]:
    """A legacy tenant whose roles still name dedicated Unix accounts."""
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
        # The director phase is complete for this tenant: the cutover refuses to
        # run before it, because the board still authorizes the account the
        # director makes that write with.
        "workflow": {"roles": [], "migrations": {"director_onboarding": True}},
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
                "run_as_user": mapping[role],
            }
            for index, role in enumerate(roles)
        ],
    }
    (tmp / "repo").mkdir()
    (tmp / "sessions").mkdir()
    board_root = tmp / "ticketboard-live"
    (board_root / "releases" / "old" / "scripts").mkdir(parents=True)
    (board_root / "releases" / "new" / "scripts").mkdir(parents=True)
    os.symlink(board_root / "releases" / "old", board_root / "current")
    (tmp / "plan.json").write_text(
        json.dumps(
            {
                "project": "porter",
                "socket_path": str(tmp / "board" / "board.sock"),
                "board_root": str(board_root),
                "owner_user": owner,
                "owner_home": str(tmp / "homes" / owner),
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp / "porter.json"
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config_path, mapping


def _session_launcher(mapping: dict[str, str], runner: _RealRunner, *, wrong_uid_for: str = ""):
    """Start each role's session under its own account, for real."""

    def launch(config, **_kwargs) -> int:
        for role in config.roles:
            account = mapping.get(role.role, "")
            socket = _RealRunner.socket_for(f"syrd45-{role.role}")
            runner(["tmux", "-S", socket, "kill-server"], check=False)
            command = [
                "tmux", "-S", socket, "new-session", "-d", "-s", role.tmux_session,
                "-c", str(role.workdir), "sleep 120",
            ]
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
            command = ["tmux", "-S", _RealRunner.socket_for(f"syrd45-{role.role}"), "kill-server"]
            if account:
                command = ["sudo", "-u", account, "-H", *command]
            runner(command, check=False)
        return 0

    return stop


def _pane_uid(role: str, mapping: dict[str, str], runner: _RealRunner) -> int | None:
    account = mapping[role]
    proc = runner(
        ["sudo", "-u", account, "-H", "tmux", "-S", _RealRunner.socket_for(f"syrd45-{role}"),
         "display-message", "-p", "-t", f"porter-{role}:0.0", "#{pane_pid}"]
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
                rest = ["tmux", "-S", _RealRunner.socket_for(f"syrd45-{role}"), *rest[1:]]
                argv = [*argv[:4], *rest] if argv[3] == "-H" else [*argv[:3], *rest]
        return runner(argv, **kwargs)

    return call


def end_to_end(owner: str) -> None:
    """Exercise the SYRD-69 checkpoint and project-account contract for real."""
    assert os.geteuid() == 0, "this case must run as root inside the namespace"
    accounts = _distinct_accounts(2)
    with tempfile.TemporaryDirectory(prefix="syrd69-e2e-") as tmp:
        tmp_path = Path(tmp)
        tmp_path.chmod(0o755)
        sockets = tmp_path / "sockets"
        sockets.mkdir()
        sockets.chmod(0o777)
        _RealRunner.SOCKET_DIR = sockets

        config_path, mapping = _tenant(tmp_path, accounts, owner)
        config = team_launcher.load_project_config("porter", config_path)
        runner = _RealRunner()
        identity_runner = _identity_runner(mapping, runner)
        ownership_before = {
            role.workdir: os.stat(role.workdir).st_uid for role in config.roles
        }

        try:
            assert {role.role: role.run_as_user for role in config.roles} == mapping
            assert _session_launcher(mapping, runner)(config) == 0
            for role, account in mapping.items():
                expected_uid = pwd.getpwnam(account).pw_uid
                probe = None
                for _attempt in range(20):
                    probe = runner(
                        [
                            "sudo", "-u", account, "-H", "tmux", "-S",
                            _RealRunner.socket_for(f"syrd45-{role}"),
                            "display-message", "-p", "-t", f"porter-{role}:0.0",
                            "#{pane_pid}",
                        ]
                    )
                    if probe.returncode == 0:
                        break
                    time.sleep(0.05)
                assert probe is not None and probe.returncode == 0, (
                    role, account, probe.stderr if probe is not None else "no probe"
                )
                assert team_launcher.process_uid(int(probe.stdout.strip())) == expected_uid

            # No mutation is permitted while any legacy role pane is live.
            before = config_path.read_bytes()
            changed, problems = team_launcher.repatriate_role_runtime_state(
                config,
                config_path=config_path,
                runner=identity_runner,
            )
            assert changed is False
            assert problems and all(
                "checkpoint before repatriation" in problem for problem in problems
            ), problems
            assert config_path.read_bytes() == before
            assert {
                role.workdir: os.stat(role.workdir).st_uid for role in config.roles
            } == ownership_before
            for role, account in mapping.items():
                assert _pane_uid(role, mapping, runner) == pwd.getpwnam(account).pw_uid

            # Once checkpointed, state and worktrees return to the project
            # account while the compatibility accounts remain untouched.
            assert _stopper(mapping, runner)(config) == 0
            changed, problems = team_launcher.repatriate_role_runtime_state(
                config,
                config_path=config_path,
                runner=identity_runner,
            )
            assert changed is True and problems == [], problems
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            assert payload["role_state_isolation"] is True, payload
            assert all("run_as_user" not in role for role in payload["roles"]), payload
            owner_uid = pwd.getpwnam(owner).pw_uid
            for role in payload["roles"]:
                assert os.stat(role["workdir"]).st_uid == owner_uid, role
            for account in accounts:
                assert pwd.getpwnam(account).pw_name == account

            # Relaunched role panes share the project account. Kernel process
            # ownership, rather than a per-role Unix-account declaration, is
            # the authority fact this end-to-end case proves.
            modern = team_launcher.load_project_config("porter", config_path)
            for role in modern.roles:
                command = [
                    "sudo",
                    "-u",
                    owner,
                    "-H",
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    role.tmux_session,
                    "sleep 120",
                ]
                assert runner(command).returncode == 0, command
            listed = runner(
                [
                    "sudo",
                    "-u",
                    owner,
                    "-H",
                    "tmux",
                    "list-sessions",
                    "-F",
                    "#{session_name}",
                ]
            )
            assert listed.returncode == 0, listed.stderr
            session_names = listed.stdout.split()
            for role in modern.roles:
                assert session_names.count(role.tmux_session) == 1, session_names
                pane = runner(
                    [
                        "sudo",
                        "-u",
                        owner,
                        "-H",
                        "tmux",
                        "display-message",
                        "-p",
                        "-t",
                        role.target,
                        "#{pane_pid}",
                    ]
                )
                assert pane.returncode == 0, pane.stderr
                assert team_launcher.process_uid(int(pane.stdout.strip())) == owner_uid

            changed, problems = team_launcher.repatriate_role_runtime_state(
                modern,
                config_path=config_path,
                runner=identity_runner,
            )
            assert changed is False and problems == [], problems
        finally:
            _stopper(mapping, runner)(config)
            for role in mapping:
                runner(
                    [
                        "tmux",
                        "-S",
                        _RealRunner.socket_for(f"syrd45-{role}"),
                        "kill-server",
                    ],
                    check=False,
                )
            runner(["sudo", "-u", owner, "-H", "tmux", "kill-server"], check=False)
            time.sleep(0.3)
            leaked = [
                line
                for line in subprocess.run(
                    ["ps", "-eo", "args"], capture_output=True, text=True
                ).stdout.splitlines()
                if str(_RealRunner.SOCKET_DIR) in line
            ]
            assert not leaked, leaked


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
