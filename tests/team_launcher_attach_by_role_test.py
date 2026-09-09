#!/usr/bin/env python3
"""SYRD-76: an operator attaches to a role by project and role name.

During the partial per-role-account migration `tmux ls` under the project owner
showed only `<project>-display-N` proxy sessions while the worker lived on
another Unix user's tmux server. Reaching a role then meant knowing a slot
number, an account, or an internal session name -- none of which an operator
should have to hold. `switchyard attach <project> <role>` resolves all three
from the registered assignment.

The registered row is the resolver, not the launcher projection, for the same
reason presentation uses it: a replacement worker may sit on a recovery target,
and the operator still names the role.
"""

from __future__ import annotations

import fcntl
import shutil
import struct
import termios

from team_launcher_test_helpers import *
from scripts import presentation_controller

PROJECT = "porter"
ROLES = ("director", "app")
# Captured at import, before `run_team_launcher_tests` replaces it with an
# identity function for the whole module. These launcher suites have no board
# to ask, so that stub is right for every case but the one below -- which is
# about the resolution itself and has to drive the real thing.
_REAL_RUNTIME_ASSIGNMENT_CONFIG = presentation_controller.runtime_assignment_config


def _config(root: Path, *, project: str = PROJECT, run_as_user: str = "", role_accounts: bool = False):
    layout = root / "layout.json"
    layout.write_text(
        json.dumps(team_launcher._new_project_layout_payload(2), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    roles = []
    for slot, role in enumerate(ROLES):
        entry = {
            "role": role,
            "slot": slot,
            "tmux_session": f"{project}-{role}",
            "target": f"{project}-{role}:0.0",
            "workdir": str(root / role),
            "cli": ["codex"],
            "live_commands": ["codex"],
        }
        if role_accounts:
            entry["run_as_user"] = f"{project}-{role}"
        roles.append(entry)
        (root / role).mkdir(exist_ok=True)
    payload = {
        "project": project,
        "layout": str(layout),
        "session_dir": str(root / "sessions"),
        "presentation": {"slot_count": 2, "layouts": {"default": {"0": "director", "1": "app"}}},
        "roles": roles,
    }
    if run_as_user:
        payload["run_as_user"] = run_as_user
    path = root / f"{project}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return load_project_config(project, path)


class _Tenant:
    """A runner whose roles are live, recording every tmux call it is given."""

    def __init__(self, *, live: set[str] | None = None, project: str = PROJECT):
        self.project = project
        self.live = {f"{project}-{role}" for role in ROLES} if live is None else set(live)
        self.calls: list[list[str]] = []
        self.clients: dict[str, list[str]] = {}

    def __call__(self, args, **kwargs):
        argv = list(args)
        self.calls.append(argv)
        # Presentation reaches a role account's server through `sudo -u <acct>
        # -H tmux ...`. A runner that only understands bare tmux argv reports
        # every cross-account role as absent, which is the state in which this
        # command would never lock or attach anything.
        if argv[:2] == ["sudo", "-u"]:
            argv = argv[4:] if argv[3:4] == ["-H"] else argv[3:]
        if argv[:1] != ["tmux"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        target = ""
        if "-t" in argv:
            target = argv[argv.index("-t") + 1].lstrip("=").split(":", 1)[0]
        if argv[1:2] == ["has-session"]:
            return subprocess.CompletedProcess(argv, 0 if target in self.live else 1, "", "can't find session")
        if argv[1:2] == ["display-message"]:
            return subprocess.CompletedProcess(argv, 0, "0\n", "")
        if argv[1:2] == ["list-clients"]:
            return subprocess.CompletedProcess(argv, 0, "\n".join(self.clients.get(target, [])), "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def _attach(config, role_name, *, runner, **kwargs):
    """Run the command with the attach itself captured instead of performed."""
    captured: list[list[str]] = []
    printed: list[str] = []
    result = presentation_controller.attach_role_command(
        config,
        role_name=role_name,
        runner=runner,
        attacher=lambda argv: captured.append(argv) or kwargs.get("exit_code", 0),
        print_func=printed.append,
        **{k: v for k, v in kwargs.items() if k != "exit_code"},
    )
    return result, captured, "\n".join(printed)


def test_an_operator_attaches_by_role_name_alone() -> None:
    """The whole ask: a project and a role, nothing internal."""
    with tempfile.TemporaryDirectory(prefix="attach-by-name.") as tmp:
        config = _config(Path(tmp))
        tenant = _Tenant()

        result, captured, printed = _attach(config, "app", runner=tenant)

        assert result == 0, printed
        assert len(captured) == 1, captured
        argv = captured[0]
        # tmux refuses to nest without this, and an operator may well already
        # be inside tmux when they run the command.
        assert argv[:2] == ["env", "TMUX="], argv
        assert argv[2] == "tmux", argv
        assert argv[3] == "attach", argv
        assert argv[-2:] == ["-t", "=porter-app"], argv
        assert "app" in printed and "detached" in printed, printed


def test_the_session_comes_from_the_registered_assignment() -> None:
    """A replacement worker is reached by the role's name, not by convention.

    The launcher projection still says `porter-app`; the board says the live
    worker is somewhere else. Attaching must follow the board, which is the row
    notifications and write authority already resolve from.
    """
    with tempfile.TemporaryDirectory(prefix="attach-registered.") as tmp:
        root = Path(tmp)
        config = team_launcher.replace(_config(root), role_state_isolation=True)
        recovery = "porter-app-r2:0.0"
        payload = {
            "project": PROJECT,
            "authority_mode": "process",
            "assignments": {
                "director": {"actual_target": "porter-director:0.0", "runtime": "codex"},
                "app": {"actual_target": recovery, "runtime": "codex"},
            },
        }
        tenant = _Tenant(live={"porter-director", "porter-app-r2"})

        stub = presentation_controller.runtime_assignment_config
        presentation_controller.runtime_assignment_config = lambda cfg: (
            _REAL_RUNTIME_ASSIGNMENT_CONFIG(cfg, opener=lambda _url: _JsonResponse(payload))
        )
        try:
            result, captured, printed = _attach(config, "app", runner=tenant)
        finally:
            presentation_controller.runtime_assignment_config = stub

        assert result == 0, printed
        assert captured[0][-2:] == ["-t", "=porter-app-r2"], captured[0]
        # And the operator never had to see that name.
        assert "porter-app-r2" not in printed, printed


class _JsonResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return json.dumps(self._payload).encode("utf-8")


def test_crossing_accounts_goes_through_the_role_control_grant_and_locks_it() -> None:
    """A caller who is not the worker's account must not get a shell as it.

    The grant permits tmux as that account and nothing else, and the session's
    prefix and key table are taken away first -- otherwise prefix-c in the
    attached client is a shell in the worker's account, which is exactly the
    privileged parent this command exists to avoid.
    """
    with tempfile.TemporaryDirectory(prefix="attach-cross-account.") as tmp:
        config = _config(Path(tmp), run_as_user="porter-owner", role_accounts=True)
        tenant = _Tenant()

        _result, captured, _printed = _attach(config, "app", runner=tenant)

        argv = captured[0]
        assert argv[:3] == ["env", "TMUX=", "/usr/bin/sudo"], argv
        assert argv[3:7] == ["-n", "-u", "porter-app", "/usr/bin/tmux"], argv
        locks = [call for call in tenant.calls if "set-option" in call]
        locked = {(call[-2], call[-1]) for call in locks}
        assert set(presentation_controller.display_lock_options()) <= locked, locks
        # Every lock is aimed at the worker's own session, not the operator's.
        assert all("=porter-app:" in call for call in locks), locks
        # And it reached that session as the account that owns it, not as this
        # caller, who by construction cannot set options on it.
        assert all(call[:3] == ["sudo", "-u", "porter-app"] for call in locks), locks


def test_a_same_account_role_is_reached_directly_and_not_locked() -> None:
    """Nothing is taken away from a session the caller already owns outright."""
    with tempfile.TemporaryDirectory(prefix="attach-same-account.") as tmp:
        config = _config(Path(tmp))
        tenant = _Tenant()

        _result, captured, _printed = _attach(config, "app", runner=tenant)

        assert "/usr/bin/sudo" not in captured[0], captured[0]
        assert [call for call in tenant.calls if "set-option" in call] == [], tenant.calls


def test_a_second_watcher_does_not_resize_what_the_first_one_shows() -> None:
    """A display slot already showing the role must keep its geometry (SYRD-27)."""
    with tempfile.TemporaryDirectory(prefix="attach-observer.") as tmp:
        config = _config(Path(tmp))

        alone = _Tenant()
        _r, captured_alone, _p = _attach(config, "app", runner=alone)

        watched = _Tenant()
        watched.clients["porter-app"] = ["/dev/pts/40"]
        _r, captured_watched, _p = _attach(config, "app", runner=watched)

        flags_alone = captured_alone[0][captured_alone[0].index("-f") + 1]
        flags_watched = captured_watched[0][captured_watched[0].index("-f") + 1]
        assert "ignore-size" not in flags_alone, flags_alone
        assert "ignore-size" in flags_watched, flags_watched


def test_the_listing_names_roles_and_nothing_internal() -> None:
    """Slot numbers, accounts and session names stay out of the operator's way."""
    with tempfile.TemporaryDirectory(prefix="attach-listing.") as tmp:
        config = _config(Path(tmp), run_as_user="porter-owner", role_accounts=True)
        tenant = _Tenant(live={"porter-director"})
        printed: list[str] = []

        result = presentation_controller.attach_role_command(
            config, role_name=None, runner=tenant, print_func=printed.append
        )
        output = "\n".join(printed)

        assert result == 0, output
        for role in ROLES:
            assert role in output, output
        assert "director" in output and "live" in output, output
        assert "app" in output and "missing" in output, output
        assert "slot" not in output.casefold(), output
        assert "porter-app" not in output, output
        assert "porter-director:0.0" not in output, output


def test_an_unknown_role_is_named_back_with_the_ones_that_exist() -> None:
    with tempfile.TemporaryDirectory(prefix="attach-unknown-role.") as tmp:
        config = _config(Path(tmp))
        tenant = _Tenant()

        try:
            _attach(config, "auditt", runner=tenant)
        except SystemExit as exc:
            message = str(exc)
        else:
            raise AssertionError("an unknown role attached")

        assert "auditt" in message, message
        assert "director" in message and "app" in message, message
        # Nothing was attached on the way to finding out.
        assert not [call for call in tenant.calls if "attach" in call], tenant.calls


def test_a_role_that_is_not_running_says_so_by_name() -> None:
    with tempfile.TemporaryDirectory(prefix="attach-not-running.") as tmp:
        config = _config(Path(tmp))
        tenant = _Tenant(live={"porter-director"})

        try:
            _attach(config, "app", runner=tenant)
        except SystemExit as exc:
            message = str(exc)
        else:
            raise AssertionError("a missing worker attached")

        assert "app" in message and "missing" in message, message
        assert "recover app" in message, message


def test_a_failed_attach_is_reported_as_a_failure() -> None:
    """tmux exiting nonzero is not a detach, and must not read as one."""
    with tempfile.TemporaryDirectory(prefix="attach-failed.") as tmp:
        config = _config(Path(tmp))
        tenant = _Tenant()

        try:
            _attach(config, "app", runner=tenant, exit_code=1)
        except SystemExit as exc:
            message = str(exc)
        else:
            raise AssertionError("a failed attach reported success")

        assert "app" in message and "exit 1" in message, message


def test_a_role_pointing_outside_the_project_is_refused() -> None:
    """Attaching by name must not become a way to reach another tenant."""
    with tempfile.TemporaryDirectory(prefix="attach-foreign.") as tmp:
        root = Path(tmp)
        config = _config(root)
        foreign = team_launcher.replace(
            config.roles[1], tmux_session="other-app", target="other-app:0.0"
        )
        config = team_launcher.replace(config, roles=[config.roles[0], foreign])
        tenant = _Tenant(live={"porter-director", "other-app"})

        try:
            _attach(config, "app", runner=tenant)
        except SystemExit as exc:
            message = str(exc)
        else:
            raise AssertionError("a foreign session attached")

        assert "app" in message and "other-app" in message, message
        assert not [call for call in tenant.calls if "attach" in call], tenant.calls


def test_attach_is_never_escalated() -> None:
    """Escalating it would put back the privileged parent it exists to remove."""
    assert "attach" in team_launcher.SWITCHYARD_COMMANDS
    assert "attach" in team_launcher.SWITCHYARD_UNPRIVILEGED_COMMANDS
    assert "attach" not in team_launcher.SWITCHYARD_PRIVILEGED_COMMANDS
    for argv in (["attach", PROJECT], ["attach", PROJECT, "app"]):
        assert team_launcher.switchyard_invocation_requires_root(argv) is False, argv


def test_a_real_terminal_attaches_to_a_real_worker_and_returns_on_detach() -> None:
    """The end of the ticket, against a real tmux rather than a description.

    A real worker session; a real client attached by the exact argv this
    command builds, unmodified -- an isolated `TMUX_TMPDIR` is what keeps it
    off this host's own server, so nothing about the argv has to be rewritten
    to make it testable. Detached from outside the way a human presses
    Ctrl-b d, and the command returns 0 with the worker still running.
    """
    if shutil.which("tmux") is None:
        return
    with tempfile.TemporaryDirectory(prefix="attach-real-tmux.") as tmp:
        root = Path(tmp)
        tmux_tmp = root / "tmux"
        tmux_tmp.mkdir(mode=0o700)
        config = _config(root)
        env = dict(os.environ)
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        env["TMUX_TMPDIR"] = str(tmux_tmp)
        # A client started under TERM=dumb exits with "open terminal failed"
        # before it can be the client this case is about.
        env["TERM"] = "xterm-256color"

        def tmux(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
            return subprocess.run(["tmux", *args], env=env, text=True, **kwargs)

        def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
            call_env = dict(env)
            call_env.update(kwargs.pop("env", None) or {})
            return subprocess.run(list(args), env=call_env, **kwargs)

        try:
            tmux("new-session", "-d", "-s", "porter-app", "-x", "80", "-y", "24",
                 "sh", "-c", "exec sleep 600", check=True)
            worker_pid = tmux("display-message", "-p", "-t", "=porter-app:0.0", "#{pane_pid}",
                              stdout=subprocess.PIPE).stdout.strip()
            assert worker_pid, "the worker never started"

            printed: list[str] = []
            attached: list[str] = []

            def attacher(argv: list[str]) -> int:
                # The built argv verbatim, on a pty of its own and with no shell
                # between: a shell here would be both unlike the command (which
                # runs the argv directly) and actively misleading -- zsh reads
                # the exact-target `=porter-app` as equals-expansion.
                master, slave = os.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
                client = subprocess.Popen(
                    argv, env=env, stdin=slave, stdout=slave, stderr=slave,
                    start_new_session=True,
                )
                os.close(slave)
                deadline = time.time() + 20
                while time.time() < deadline and not attached:
                    listing = tmux("list-clients", "-t", "=porter-app", "-F", "#{client_tty}",
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    attached.extend(
                        line.strip() for line in (listing.stdout or "").splitlines() if line.strip()
                    )
                    if not attached:
                        time.sleep(0.25)
                assert attached, "no client ever attached to the worker"
                tmux("detach-client", "-t", attached[0])
                try:
                    return client.wait(timeout=20)
                finally:
                    os.close(master)

            result = presentation_controller.attach_role_command(
                config, role_name="app", runner=runner, attacher=attacher, print_func=printed.append
            )
            output = "\n".join(printed)

            assert result == 0, output
            assert "detached from porter role app" in output, output
            # A detach is not a stop: the worker is the same process it was.
            still = tmux("display-message", "-p", "-t", "=porter-app:0.0", "#{pane_pid}",
                         stdout=subprocess.PIPE).stdout.strip()
            assert still == worker_pid, (still, worker_pid)
        finally:
            subprocess.run(["tmux", "kill-server"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_attach_by_role_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
