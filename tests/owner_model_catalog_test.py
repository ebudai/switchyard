#!/usr/bin/env python3
"""SYRD-250: a model list belongs to an account, not to a host.

The User's live `test2` screenshot: five panes up, and the Antigravity audit
pane at its prompt saying

    model gemini-3.7-flash-high is not recognized as a known model or custom
    model in settings. Ignoring the flag.

So that role was running on some model, and not the one in the tenant plan.

The slug is not universally wrong -- `agy models` on the development account
lists it. That is the whole point: `agy models` run by the operator and run by
`test2-agent` are different lists, and the one that decides is the owner's,
because that is the account the role runs as. SYRD-115 asked whichever account
happened to be typing, which on a `switchyard new` is the operator.

Two things follow, and both are here: ask the owner, and check what is already
configured against the owner's own answer before starting a pane for it. The
check is free -- one `agy models`, no prompt, no token, no capability probe --
which is what makes it different from the probe SYRD-246 removed.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher  # noqa: E402
from scripts.ticket_board import runtime_catalog  # noqa: E402
from team_launcher_test_helpers import (  # noqa: E402
    FirstRunAuthRunner,
    _mark_first_run_setup_complete,
    _write_first_run_auth_config,
    load_project_config,
)

CHECKS = 0

#: The slug in the screenshot, and what the tenant owner's `agy` actually lists.
CONFIGURED = "gemini-3.7-flash-high"
OWNER_LISTS = ["gemini-3.7-pro", "gemini-3.5-flash"]


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


class TwoAccounts:
    """`agy models` answers differently depending on who asks.

    Exactly the situation the ticket describes: the operator's own account lists
    the configured slug, and the tenant owner's does not.
    """

    def __init__(self, *, owner: str = "test2-agent", authenticated: bool = True) -> None:
        self.owner = owner
        self.authenticated = authenticated
        #: Every `agy models`, whoever asked.
        self.asked_as: list[str] = []
        #: Only the ones read as a model CATALOG. `agy models` is also how the
        #: phase tests whether `agy` is signed in at all, so a raw count of
        #: calls cannot tell the check this ticket adds from the one that was
        #: always there. `model_catalog` passes `capture_output`; the auth
        #: probe passes `stdout`/`timeout`. If that ever stops being true these
        #: cases go red rather than quietly counting nothing.
        self.catalog_reads: list[str] = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        if argv[-2:] != ["agy", "models"]:
            return subprocess.CompletedProcess(argv, 0, stdout="")
        as_owner = argv[:2] == ["sudo", "-u"] and argv[2] == self.owner
        who = self.owner if as_owner else "operator"
        self.asked_as.append(who)
        if "capture_output" in kwargs:
            self.catalog_reads.append(who)
        if not self.authenticated:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not logged in")
        listed = OWNER_LISTS if as_owner else [CONFIGURED, *OWNER_LISTS]
        return subprocess.CompletedProcess(
            argv, 0, stdout="".join(f"{slug}  a description\n" for slug in listed)
        )


def test_the_catalog_is_read_in_the_owners_context_not_the_callers() -> None:
    """The root cause, as a difference between two answers."""
    runner = TwoAccounts()
    prefix = team_launcher._owner_command_env_args(
        "test2-agent", Path("/home/test2-agent"), []
    )

    as_caller = runtime_catalog.model_catalog("agy", runner=runner)
    as_owner = runtime_catalog.model_catalog("agy", runner=runner, owner_args=prefix)

    check(CONFIGURED in [c.value for c in as_caller.choices],
          "the operator's own account lists the configured slug")
    check(CONFIGURED not in [c.value for c in as_owner.choices],
          f"and the owner's does not: {[c.value for c in as_owner.choices]}")
    check(runner.asked_as == ["operator", "test2-agent"],
          f"and the second question was asked as the owner: {runner.asked_as}")


def test_a_model_the_owner_does_not_offer_is_reported_not_rewritten() -> None:
    runner = TwoAccounts()
    prefix = team_launcher._owner_command_env_args(
        "test2-agent", Path("/home/test2-agent"), []
    )
    owned = runtime_catalog.owner_model_catalog("agy", runner=runner, owner_args=prefix)
    mismatch = runtime_catalog.model_absent_from(owned, CONFIGURED)

    check(mismatch is not None, "the mismatch is detected")
    check([c.value for c in mismatch.choices] == OWNER_LISTS,
          f"and carries what the owner does offer: {[c.value for c in mismatch.choices]}")


def test_a_model_the_owner_does_offer_is_not_complained_about() -> None:
    runner = TwoAccounts()
    prefix = team_launcher._owner_command_env_args(
        "test2-agent", Path("/home/test2-agent"), []
    )
    owned = runtime_catalog.owner_model_catalog("agy", runner=runner, owner_args=prefix)
    check(
        runtime_catalog.model_absent_from(owned, "gemini-3.7-pro") is None,
        "a configured model the owner lists passes silently",
    )
    check(
        runtime_catalog.model_absent_from(owned, "") is None,
        "and a role with no model configured has nothing to contradict",
    )


def test_a_runtime_that_cannot_enumerate_never_contradicts_a_configured_value() -> None:
    """"Do not claim all four CLIs expose a machine-readable catalog."

    Claude and Codex enumerate nothing. A recorded table is a starting point for
    choosing, never evidence that somebody's configured model is wrong.
    """
    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(list(argv), 0, stdout="")

    for runtime in ("claude", "codex", "hermes"):
        owned = runtime_catalog.owner_model_catalog(runtime, runner=runner)
        check(owned is None, f"{runtime} produces no list of its own")
        check(
            runtime_catalog.model_absent_from(owned, "something-nobody-recorded") is None,
            f"so {runtime} cannot contradict anything",
        )


def test_a_list_that_could_not_be_read_contradicts_nothing_either() -> None:
    """An unauthenticated or broken `agy` is not evidence of a bad model."""
    def refuses(argv, **_kwargs):
        return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="not logged in")

    owned = runtime_catalog.owner_model_catalog("agy", runner=refuses)
    check(owned is None, "a failed listing is not a list")
    check(
        runtime_catalog.model_absent_from(owned, CONFIGURED) is None,
        "and the recorded table it falls back to refuses nobody",
    )


def test_a_recorded_catalog_says_it_is_unverified() -> None:
    """Provenance, in the words an operator reads."""
    recorded = runtime_catalog.model_catalog("claude")
    live_runner = lambda argv, **_kw: subprocess.CompletedProcess(  # noqa: E731
        list(argv), 0, stdout="gemini-3.7-pro x\n"
    )
    live = runtime_catalog.model_catalog("agy", runner=live_runner)

    check("UNVERIFIED" in recorded.note, f"a recorded list says so: {recorded.note}")
    check("not checked against this account" in recorded.note, f"{recorded.note}")
    check(recorded.enumerable is False, "and cannot be used to refuse anything")
    check("in this account" in live.note, f"a live list says whose it is: {live.note}")
    check(live.enumerable is True, "and is the only kind that can contradict a value")


def test_the_agy_slug_carries_its_own_effort_so_none_is_asked_for() -> None:
    """"the Antigravity slug already encodes effort"."""
    check(runtime_catalog.runtime_takes_effort("agy") is False,
          "agy is not asked for a separate effort level")
    check(runtime_catalog.effort_catalog("agy").choices == (),
          "because there is nothing it could render")
    for runtime in ("claude", "codex", "hermes"):
        check(runtime_catalog.runtime_takes_effort(runtime),
              f"{runtime} does render one, so it is offered")


def test_the_launch_refuses_the_role_and_changes_nothing() -> None:
    """The pane does not start, and the configured value is left alone."""
    report = team_launcher.FirstRunAuthReport(
        {}, [], [], {},
        [("audit", "agy", CONFIGURED, tuple(OWNER_LISTS))],
        owner_user="test2-agent",
    )
    said: list[str] = []
    stopped = team_launcher.stop_before_launch_for_unknown_models(
        report, print_func=said.append
    )

    check(stopped is True, "the launch stops rather than opening that pane")
    message = said[0]
    check("not starting audit" in message, f"naming the role: {message}")
    check(CONFIGURED in message, f"and the model it was configured for: {message}")
    check("test2-agent" in message, f"and whose account was asked: {message}")
    check("gemini-3.7-pro" in message, f"and what that account does offer: {message}")
    check("nothing has been changed for you" in message,
          f"and that nothing was rewritten: {message}")
    check("set-role-runtime" in message, f"and how to change it: {message}")


def test_provisioning_offers_the_owners_models_not_the_operators() -> None:
    """The other half: the list an operator picks from during `switchyard new`.

    Flagging a bad value at launch is the safety net. Offering the right list in
    the first place is the fix, and it is the same account question.
    """
    runner = TwoAccounts()
    shown: list[str] = []
    asked: list[str] = []

    def answer(prompt: str) -> str:
        asked.append(prompt)
        if prompt.startswith("Include"):
            return "n"
        if "implementer" in prompt:
            return "1"
        if "runtime" in prompt:
            return "3"  # agy
        if "model" in prompt:
            return "1"  # whatever the owner's list offers first
        return ""

    plan = team_launcher._prompt_switchyard_role_plan(
        runner=runner,
        owner_user="test2-agent",
        owner_home=Path("/home/test2-agent"),
        input_func=answer,
        print_func=shown.append,
    )

    offered = "\n".join(shown)
    check("test2-agent" in runner.asked_as,
          f"the catalog was read as the owner: {runner.asked_as}")
    check("operator" not in runner.asked_as,
          f"and never as whoever is typing: {runner.asked_as}")
    check(CONFIGURED not in offered,
          "so the slug the owner does not have is never offered")
    check("gemini-3.7-pro" in offered, f"and the owner's own models are: {offered}")
    check(plan[0].model in OWNER_LISTS, f"and what was chosen is one of them: {plan[0].model}")
    check(plan[0].effort == "", "agy is asked for no separate effort level")


def test_a_report_with_no_mismatch_stops_nothing() -> None:
    report = team_launcher.FirstRunAuthReport({}, [], [], {}, [])
    check(team_launcher.stop_before_launch_for_unknown_models(
        report, print_func=lambda _line: None) is False,
        "an ordinary launch is untouched")


class Tenant(FirstRunAuthRunner, TwoAccounts):
    """A tenant whose `agy` answers as `test2-agent`, and as nobody else."""

    def __init__(self, *, authenticated: bool = True) -> None:
        FirstRunAuthRunner.__init__(self, authenticated_after_login=authenticated)
        TwoAccounts.__init__(self, owner="test2-agent", authenticated=authenticated)
        self.login_seen.update({"claude", "agy"})

    def __call__(self, argv, **kwargs):
        if list(argv)[-2:] == ["agy", "models"]:
            return TwoAccounts.__call__(self, argv, **kwargs)
        return FirstRunAuthRunner.__call__(self, argv, **kwargs)


def run_phase(roles, role_models, *, authenticated: bool = True):
    """`run_first_run_auth_phase` for a tenant owned by `test2-agent`."""
    with tempfile.TemporaryDirectory(prefix="syrd250-phase.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "test2-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path, roles=roles, role_models=role_models
            ),
        )
        _mark_first_run_setup_complete(owner_home, config)
        runner = Tenant(authenticated=authenticated)
        report = team_launcher.run_first_run_auth_phase(
            config, owner_user="test2-agent", owner_home=owner_home,
            runner=runner, print_func=lambda _line: None,
        )
    return report, runner


def test_the_phase_asks_the_owner_and_reports_the_mismatch() -> None:
    """End to end: the ticket's own situation, through the real phase.

    Two `agy` roles, one on the slug from the screenshot and one on a model the
    owner does list, so the case says which role is flagged rather than only
    that something was.
    """
    report, runner = run_phase(
        roles=[("director", "claude"), ("audit", "agy"), ("app", "agy")],
        role_models={
            "director": "claude-opus-5",
            "audit": CONFIGURED,
            "app": "gemini-3.7-pro",
        },
    )

    mismatched = [entry[0] for entry in report.unknown_model_roles]
    check(mismatched == ["audit"],
          f"the audit role is flagged and the one on a listed model is not: "
          f"{report.unknown_model_roles}")
    _role, cli, model, available = report.unknown_model_roles[0]
    check((cli, model) == ("agy", CONFIGURED), f"{report.unknown_model_roles}")
    check(list(available) == OWNER_LISTS, f"carrying the owner's own list: {available}")
    check(report.owner_user == "test2-agent",
          f"and the report names whose account said so: {report.owner_user!r}")
    check(set(runner.asked_as) == {"test2-agent"},
          f"and `agy` was only ever asked as the owner: {runner.asked_as}")


def test_a_team_that_configured_no_models_is_not_asked_about_any() -> None:
    """Nobody chose a model, so there is nothing for a catalog to contradict.

    And no list is fetched for it either: the read exists to check a configured
    value, so with no configured value it is not work worth doing.
    """
    report, runner = run_phase(roles=[("audit", "agy"), ("app", "agy")], role_models={})

    check(report.unknown_model_roles == [], f"nothing is flagged: {report.unknown_model_roles}")
    check(runner.catalog_reads == [],
          f"and the owner's list is not fetched at all: {runner.catalog_reads}")
    check(runner.asked_as != [],
          "though `agy` was still asked whether it is signed in, as it always was")


def test_an_account_that_cannot_answer_is_not_asked_what_it_offers() -> None:
    """An unauthenticated `agy` has no opinion about a model.

    Its list would fail to read and contradict nobody, so the result is the
    same either way -- but the roles are already being reported as
    unauthenticated, and asking an account that just told us it cannot answer
    is a sudo and a Node start spent to learn nothing.
    """
    report, runner = run_phase(
        roles=[("audit", "agy")],
        role_models={"audit": CONFIGURED},
        authenticated=False,
    )

    check("agy" in report.unauthenticated_roles,
          f"the phase reports the real problem: {report.unauthenticated_roles}")
    check(report.unknown_model_roles == [],
          f"and does not also blame the model: {report.unknown_model_roles}")
    check(runner.catalog_reads == [],
          f"having never asked for a list: {runner.catalog_reads}")


def test_the_owners_list_is_read_once_however_many_roles_share_a_runtime() -> None:
    """The cost of the check does not grow with the size of the team.

    Stated as a comparison rather than a number, because `agy models` is also
    how the phase tests whether `agy` is authenticated at all -- so the count
    includes a call this ticket did not add, and only the DIFFERENCE between
    one role and three is the thing being pinned. Each of these is a sudo and a
    Node start; paying per role is the launch delay SYRD-246 just removed.
    """
    _one, one_role = run_phase(
        roles=[("audit", "agy")],
        role_models={"audit": CONFIGURED},
    )
    _three, three_roles = run_phase(
        roles=[("audit", "agy"), ("app", "agy"), ("ops", "agy")],
        role_models={"audit": CONFIGURED, "app": CONFIGURED, "ops": "gemini-3.7-pro"},
    )

    check(one_role.catalog_reads == ["test2-agent"],
          f"one role reads the owner's list once: {one_role.catalog_reads}")
    check(three_roles.catalog_reads == ["test2-agent"],
          f"and so do three: {three_roles.catalog_reads}")
    check(len(_three.unknown_model_roles) == 2,
          f"while still flagging both misconfigured roles: {_three.unknown_model_roles}")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"owner_model_catalog_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
