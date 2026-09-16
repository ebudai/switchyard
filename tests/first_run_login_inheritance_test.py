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
    _mark_first_run_setup_complete,
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


def test_the_provider_first_run_is_asked_once_for_every_role_that_uses_it() -> None:
    """Acceptance: shared onboarding is collected once, not once per role.

    Claude's theme and welcome flow belong to the owner's account. Three roles
    use Claude on the UAT tenant, and the live `.claude.json` carried a valid
    `oauthAccount` with neither `hasCompletedOnboarding` nor a `theme` -- so
    every one of those panes opened it.
    """
    with tempfile.TemporaryDirectory(prefix="syrd191-setup.") as tmp:
        tmp_path = Path(tmp)
        config = uat_config(tmp_path)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        runner = FirstRunAuthRunner()
        runner.login_seen.update({"claude", "codex"})
        manifest = team_launcher.build_first_run_setup_manifest(
            config, owner_user="otto-agent", owner_home=owner_home, runner=runner
        )

    setup = manifest.provider_setup_steps
    check(len(setup) == 1, f"one step, not one per role: {setup}")
    check(setup[0].cli == "claude", f"and only for the provider that needs it: {setup[0].cli}")
    check(
        setup[0].roles == ("designer", "director", "audit"),
        f"named for every role that uses it: {setup[0].roles}",
    )
    check(
        all(step.cli != "codex" for step in setup),
        "a provider with no account-wide first run is not asked for one",
    )


def test_an_account_that_has_already_done_it_is_asked_for_nothing() -> None:
    """Acceptance: already-complete state is skipped, and stays skipped."""
    with tempfile.TemporaryDirectory(prefix="syrd191-idempotent.") as tmp:
        tmp_path = Path(tmp)
        config = uat_config(tmp_path)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        _mark_first_run_setup_complete(owner_home, config)
        runner = FirstRunAuthRunner()
        runner.login_seen.update({"claude", "codex"})
        first = team_launcher.build_first_run_setup_manifest(
            config, owner_user="otto-agent", owner_home=owner_home, runner=runner
        )
        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=lambda _line: None,
        )
        second = team_launcher.build_first_run_setup_manifest(
            config, owner_user="otto-agent", owner_home=owner_home, runner=runner
        )

    check(first.provider_setup_steps == [] and first.folder_trust_steps == [],
          f"nothing is offered: {first.provider_setup_steps} {first.folder_trust_steps}")
    check(not first.has_steps, "and the manifest is silent")
    check(report.incomplete_provider_setup == [] and report.untrusted_roles == [],
          f"the phase reports nothing outstanding: {report}")
    check(second.provider_setup_steps == [] and second.folder_trust_steps == [],
          "and running it again asks for nothing either")


def test_a_first_run_that_did_not_complete_is_reported_not_assumed() -> None:
    """Switchyard asks the CLI to run its setup and then looks again.

    It never writes that state itself and never answers a security prompt on
    the owner's behalf, so "the step ran" is not "the step worked": if the
    account still has not completed it, every pane using that provider will
    open it, and that is said here rather than discovered there.
    """
    with tempfile.TemporaryDirectory(prefix="syrd191-unfinished.") as tmp:
        tmp_path = Path(tmp)
        config = uat_config(tmp_path)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        runner = FirstRunAuthRunner()
        runner.login_seen.update({"claude", "codex"})
        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=lambda _line: None,
        )
        warnings: list[str] = []
        team_launcher.report_first_run_auth_warnings(report, print_func=warnings.append)

    check(
        report.incomplete_provider_setup == [("claude", ["designer", "director", "audit"])],
        f"the provider and its roles are named: {report.incomplete_provider_setup}",
    )
    check(report.has_warnings, "and the report counts as one worth printing")
    check(
        any("first run is still not complete" in line and "designer, director, audit" in line
            for line in warnings),
        f"the warning says which panes will open it: {warnings}",
    )
    check(
        not (owner_home / ".claude.json").exists(),
        "and nothing was written into the account's state to make it look done",
    )


def test_roles_that_share_a_worktree_share_one_trust_action() -> None:
    """Acceptance: one trust action per distinct required worktree."""
    with tempfile.TemporaryDirectory(prefix="syrd191-shared-tree.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_first_run_auth_config(
            tmp_path, roles=[("designer", "claude"), ("director", "claude"), ("audit", "claude")]
        )
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        # The one workdir roles may legitimately share is the project
        # repository itself, which is what the loader allows and what a tenant
        # with several roles reading one checkout really looks like.
        shared = raw["repository"]
        for role in raw["roles"]:
            if role["role"] in {"designer", "director"}:
                role["workdir"] = shared
        config_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
        config = load_project_config("otto", config_path)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        runner = FirstRunAuthRunner()
        runner.login_seen.add("claude")
        manifest = team_launcher.build_first_run_setup_manifest(
            config, owner_user="otto-agent", owner_home=owner_home, runner=runner
        )

    trust = manifest.folder_trust_steps
    check(len(trust) == 2, f"two distinct worktrees, two actions: {[str(s.workdir) for s in trust]}")
    check(
        trust[0].roles == ("designer", "director"),
        f"and the shared one names both roles it covers: {trust[0].roles}",
    )
    check(trust[1].roles == ("audit",), f"the separate tree keeps its own: {trust[1].roles}")
    printed = team_launcher._format_first_run_setup_manifest(manifest)
    check(
        any("roles designer, director" in line and "folder trust" in line for line in printed),
        f"the manifest advertises it as one step for two roles: {printed}",
    )


def test_the_setup_step_says_it_will_ask_for_a_sign_in() -> None:
    """Measured on this host, not assumed: completing Claude's first run signs in again.

    With a valid `.claude/.credentials.json` and a `.claude.json` carrying an
    `oauthAccount` but no onboarding marker -- the live testing tenant's exact
    shape -- `claude auth status --json` reports `loggedIn: true`, and an
    interactive `claude` runs its welcome flow: theme, then an OAuth sign-in it
    asks for anyway. That is the vendor's flow and Switchyard cannot suppress
    it without writing the marker itself, which it will not do.

    So the manifest must not promise a run with no login in it. It says what
    the step costs, before the step takes the terminal.
    """
    with tempfile.TemporaryDirectory(prefix="syrd191-honest.") as tmp:
        tmp_path = Path(tmp)
        config = uat_config(tmp_path)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        runner = FirstRunAuthRunner()
        runner.login_seen.update({"claude", "codex"})
        messages: list[str] = []
        team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )

    advertised = [line for line in messages if "provider setup claude" in line]
    check(advertised, f"the step is advertised: {messages}")
    check(
        "sign in again even when the account already holds valid credentials" in advertised[0],
        f"and says a sign-in is part of it: {advertised[0]}",
    )
    instruction = [line for line in messages if "will now run in this terminal" in line]
    check(instruction, f"the operator is told before it takes the terminal: {messages}")
    check("/exit" in instruction[0], f"and told how to hand it back: {instruction[0]}")
    check(
        "once for the account, not once per role" in instruction[0],
        f"and what it buys: {instruction[0]}",
    )
    trust_instructions = [line for line in messages if "so it can be trusted once for" in line]
    check(len(trust_instructions) == 3, f"each trust step says the same: {trust_instructions}")
    check(all("/exit" in line for line in trust_instructions), "including how to leave it")


class MeasuredClaudeRunner(FirstRunAuthRunner):
    """A Claude that behaves the way the real one was measured to behave.

    `claude auth status --json` answers from the credential file. An
    interactive `claude` answers from `.claude.json`: with no onboarding marker
    it runs the welcome flow -- theme, then a sign-in it asks for even though
    the credential is valid -- and with the marker it goes straight to the
    per-directory trust prompt. Both halves were measured on this host against
    the real CLI before this was written (SYRD-191).
    """

    def __init__(self, owner_home: Path, *, completes_setup: bool) -> None:
        super().__init__()
        self.owner_home = owner_home
        self.completes_setup = completes_setup
        self.login_seen.update({"claude", "codex"})
        self.signed_in_again = 0
        self.trusted: list[str] = []

    def __call__(self, args, **kwargs):
        command = [str(part) for part in args]
        if command[-1:] == ["claude"]:
            marked = json.loads((self.owner_home / ".claude.json").read_text()) \
                if (self.owner_home / ".claude.json").is_file() else {}
            if not marked.get("hasCompletedOnboarding"):
                # The welcome flow. It signs in again regardless of the
                # credential, and only then records anything.
                self.signed_in_again += 1
                if self.completes_setup:
                    marked["hasCompletedOnboarding"] = True
                    marked["theme"] = "dark"
                    (self.owner_home / ".claude.json").write_text(json.dumps(marked), encoding="utf-8")
            else:
                # Past the welcome flow, an interactive run in an untrusted
                # directory asks the trust question and records the answer.
                cwd = str(kwargs.get("cwd") or "")
                self.trusted.append(cwd)
                marked.setdefault("projects", {})[cwd] = {"hasTrustDialogAccepted": True}
                (self.owner_home / ".claude.json").write_text(json.dumps(marked), encoding="utf-8")
        return super().__call__(args, **kwargs)


def test_the_measured_vendor_flow_is_reported_rather_than_promised_away() -> None:
    """The live failure, in the shape it really has.

    The rollout plan claimed the preserved credential would make the run
    login-free. It did not: the welcome flow asks anyway. This asserts the
    product says so, and that once the account has been through it once,
    nothing asks again.
    """
    with tempfile.TemporaryDirectory(prefix="syrd191-measured.") as tmp:
        tmp_path = Path(tmp)
        config = uat_config(tmp_path)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        # The tenant's exact shape: a valid credential, an account record with
        # no onboarding marker, nothing trusted.
        (owner_home / ".claude").mkdir()
        (owner_home / ".claude" / ".credentials.json").write_text("{}\n", encoding="utf-8")
        (owner_home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "someone@example.test"}, "projects": {}}),
            encoding="utf-8",
        )
        runner = MeasuredClaudeRunner(owner_home, completes_setup=True)
        messages: list[str] = []
        first = team_launcher.run_first_run_auth_phase(
            config, owner_user="otto-agent", owner_home=owner_home,
            runner=runner, print_func=messages.append,
        )
        second_messages: list[str] = []
        second = team_launcher.run_first_run_auth_phase(
            config, owner_user="otto-agent", owner_home=owner_home,
            runner=runner, print_func=second_messages.append,
        )

    check(
        first.authenticated_now == {},
        f"no login step is offered -- the credential is valid: {first.authenticated_now}",
    )
    check(runner.signed_in_again == 1, f"and the welcome flow signs in anyway, once: {runner.signed_in_again}")
    check(
        any("asks to sign in again even when the account already holds valid credentials" in line
            for line in messages),
        f"which the manifest said in advance: {messages}",
    )
    check(first.incomplete_provider_setup == [], "the account finished it, so nothing is outstanding")
    check(
        [line for line in second_messages if line.startswith("switchyard: provider setup ")] == [],
        f"and the second run asks for no setup at all: {second_messages}",
    )
    check(
        [line for line in second_messages if line.startswith("switchyard: folder trust ")] == [],
        f"nor for any trust: {second_messages}",
    )
    check(not second_messages or "0 login step(s), 0 provider setup step(s), 0 folder trust step(s)"
          in second_messages[0], f"the manifest is empty: {second_messages[:1]}")
    check(runner.signed_in_again == 1, "no second sign-in")
    check(
        sorted(runner.trusted) == sorted(str(Path(role.workdir)) for role in config.roles
                                         if role.cli[:1] == ["claude"]),
        f"the trust prompts ran in each Claude worktree: {runner.trusted}",
    )
    check(second.untrusted_roles == [], f"and nothing is left untrusted: {second.untrusted_roles}")


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
