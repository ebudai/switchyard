#!/usr/bin/env python3
"""SYRD-191: one provider login, and every role that uses it comes up ready.

During SYRD-146 UAT the User authenticated twice as the tenant owner -- once for
Claude, once for Codex -- and was then shown five panes, every one of them
asking to sign in or run first-run onboarding.

Read off the live tenant afterwards, the owner's login was exactly where it
should be:

    /home/testing-agent/.claude/.credentials.json  0600  2026-09-16 16:23:27
    /home/testing-agent/.codex/auth.json           0600  2026-09-16 16:23:40

and the five panes the User was looking at had been running since

    pid 2715930 (designer, claude)  started 2026-09-15 20:12:34

-- a day earlier, from a start when neither file existed. Every pane ran its
CLI once, at that moment, and a process cannot read a credential written after
it started. The launch treated those sessions as "already running" and
presented them, so two successful logins looked like five failed ones.

A role whose provider was authenticated during THIS run is therefore not
already running for the purposes of this launch: its session is ended and
started again, which is the ordinary path for a role that was not running, and
the new process reads the credentials that now exist. Which roles those are
comes from the declared role/provider data -- the same table the login step
itself is built from -- never from a list of role names.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from team_launcher_test_helpers import (  # noqa: E402
    FirstRunAuthRunner,
    _write_first_run_auth_config,
    load_project_config,
    team_launcher,
)

CHECKS = 0

#: The tenant the User ran: three Claude roles, two Codex roles, one account.
UAT_ROLES = [
    ("designer", "claude"),
    ("director", "claude"),
    ("audit", "claude"),
    ("main", "codex"),
    ("ops", "codex"),
]


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def uat_config(tmp_path: Path):
    return load_project_config(
        "otto", _write_first_run_auth_config(tmp_path, roles=UAT_ROLES)
    )


def test_one_login_per_provider_covers_every_role_that_uses_it() -> None:
    """Acceptance 5, from declared role/provider data rather than role names."""
    with tempfile.TemporaryDirectory(prefix="syrd191-manifest.") as tmp:
        tmp_path = Path(tmp)
        config = uat_config(tmp_path)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        runner = FirstRunAuthRunner()
        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=lambda _line: None,
        )

    check(
        report.authenticated_now == {
            "claude": ["designer", "director", "audit"],
            "codex": ["main", "ops"],
        },
        f"one login each, covering the roles configured for it: {report.authenticated_now}",
    )
    check(
        report.roles_awaiting_restart == ("designer", "director", "audit", "main", "ops"),
        f"and every one of them is a role that cannot have it yet: {report.roles_awaiting_restart}",
    )
    check(report.unauthenticated_roles == {}, f"nothing was left unauthenticated: {report}")
    check(
        not any("credential" in str(call).casefold() or "token" in str(call).casefold()
                for call in runner.calls),
        "no credential or token is named in anything this runs",
    )


def test_a_provider_that_was_already_authenticated_restarts_nobody() -> None:
    """The narrowness: this only speaks about logins THIS run performed."""
    with tempfile.TemporaryDirectory(prefix="syrd191-already.") as tmp:
        tmp_path = Path(tmp)
        config = uat_config(tmp_path)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        runner = FirstRunAuthRunner()
        # Both providers already answer "logged in", so the manifest asks for
        # no login at all -- which is every ordinary start after the first.
        runner.login_seen.update({"claude", "codex"})
        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=lambda _line: None,
        )

    check(report.authenticated_now == {}, f"no login was performed: {report.authenticated_now}")
    check(report.roles_awaiting_restart == (), "so no running role is disturbed")


def test_a_token_refresh_is_not_a_login() -> None:
    """A provider rewriting its own credential file must restart nothing.

    Codex refreshes `auth.json` on its own schedule. Deciding from the file's
    mtime -- "the runtime started before the credentials changed" -- would
    restart every live pane a few hours into a working day. The signal is the
    login step this run actually ran, which a refresh never produces.
    """
    with tempfile.TemporaryDirectory(prefix="syrd191-refresh.") as tmp:
        tmp_path = Path(tmp)
        config = uat_config(tmp_path)
        owner_home = tmp_path / "home" / "otto-agent"
        (owner_home / ".codex").mkdir(parents=True)
        credential = owner_home / ".codex" / "auth.json"
        credential.write_text(json.dumps({"auth_mode": "chatgpt"}) + "\n", encoding="utf-8")
        runner = FirstRunAuthRunner()
        runner.login_seen.update({"claude", "codex"})
        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=lambda _line: None,
        )
        # The file changes under us, the way a refresh changes it.
        credential.write_text(json.dumps({"auth_mode": "chatgpt", "refreshed": True}) + "\n", encoding="utf-8")

    check(report.roles_awaiting_restart == (), "a refreshed credential restarts nobody")


class SessionRunner:
    """A tmux that records what was asked of it, through whatever runs it.

    The launcher sends a role's tmux through the owner boundary when the
    project account is not this process, so the command arrives wrapped in `sh
    -c ... sudo -u <owner> ...`. This looks for the operation rather than for a
    literal argv, so the assertions below are about what was asked, not about
    which wrapper asked it.
    """

    def __init__(self, *, unkillable: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.killed: list[str] = []
        self.unkillable = set(unkillable or ())

    def __call__(self, args, **_kwargs):
        command = [str(part) for part in args]
        self.calls.append(command)
        if "kill-session" in command:
            session = command[-1]
            if session in self.unkillable:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="no such session\n")
            self.killed.append(session)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_a_runtime_that_predates_the_login_is_restarted_not_presented() -> None:
    """The live sequence: five roles up since yesterday, two logins today."""
    with tempfile.TemporaryDirectory(prefix="syrd191-restart.") as tmp:
        config = uat_config(Path(tmp))
        runner = SessionRunner()
        said: list[str] = []
        kept = team_launcher._drop_roles_started_before_their_login(
            config,
            list(config.roles),
            restart_roles=("designer", "director", "audit", "main", "ops"),
            runner=runner,
            print_func=said.append,
        )

    check(kept == [], f"not one of them is presented as it is: {[role.role for role in kept]}")
    check(
        sorted(runner.killed) == sorted(f"otto-{role}" for role, _cli in UAT_ROLES),
        f"each stale session is ended so the launch starts it again: {runner.killed}",
    )
    check(
        any("restarting" in line and "provider login" in line for line in said),
        f"and the reason is said out loud: {said}",
    )


def test_roles_whose_provider_was_not_logged_in_are_left_alone() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd191-narrow.") as tmp:
        config = uat_config(Path(tmp))
        runner = SessionRunner()
        kept = team_launcher._drop_roles_started_before_their_login(
            config,
            list(config.roles),
            restart_roles=("main", "ops"),  # only codex was logged in this run
            runner=runner,
            print_func=lambda _line: None,
        )

    check(
        [role.role for role in kept] == ["designer", "director", "audit"],
        f"the Claude roles keep running: {[role.role for role in kept]}",
    )
    check(sorted(runner.killed) == ["otto-main", "otto-ops"], f"only the Codex ones end: {runner.killed}")


def test_a_session_that_cannot_be_ended_is_reported_rather_than_claimed() -> None:
    """Partial failure is loud, and the ticket is not left looking repaired."""
    with tempfile.TemporaryDirectory(prefix="syrd191-stuck.") as tmp:
        config = uat_config(Path(tmp))
        runner = SessionRunner(unkillable={"otto-audit"})
        said: list[str] = []
        kept = team_launcher._drop_roles_started_before_their_login(
            config,
            list(config.roles),
            restart_roles=tuple(role for role, _cli in UAT_ROLES),
            runner=runner,
            print_func=said.append,
        )

    check([role.role for role in kept] == ["audit"], f"the one that survived is kept: {kept}")
    check(
        any("could not be ended" in line and "audit" in line for line in said),
        f"and named, with what it will keep showing: {said}",
    )
    check(
        all("audit" not in line for line in said if "restarting" in line),
        f"it is not counted among the restarted: {said}",
    )


def test_nothing_restarts_when_no_login_happened() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd191-quiet.") as tmp:
        config = uat_config(Path(tmp))
        runner = SessionRunner()
        said: list[str] = []
        kept = team_launcher._drop_roles_started_before_their_login(
            config,
            list(config.roles),
            restart_roles=(),
            runner=runner,
            print_func=said.append,
        )

    check(len(kept) == len(config.roles), "every running role is presented as it is")
    check(runner.killed == [] and said == [], f"and nothing is ended or said: {runner.killed} {said}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"first_run_login_inheritance_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
