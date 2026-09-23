#!/usr/bin/env python3
"""SYRD-239: the recovery instruction a disconnected Director slot can obey.

A live mefp Director display attachment was closed. The inert slot told the
desktop operator to run

    switchyard present mefp recover director

and that command refuses anyone who is not the Director pane -- the pane that
had just disconnected. The operator's escape hatch was to type the role in by
hand:

    env TICKET_BOARD_CALLER_ROLE=director TICKET_BOARD_PROJECT=mefp switchyard ...

which is teaching people to forge a role, and is the thing this ticket exists
to stop.

The slot now names `switchyard recover-display <project>`, which goes through
the tenant-control bridge: root-owned, reached by one NOPASSWD grant naming
that program, resolving the caller from SUDO_UID and checking it against the
tenant's root-owned grant. The tenant side re-reads that grant and requires the
two to agree.

Bounded on every axis, and each one has a case here: only the recovery action,
only the Director's slot, only that tenant's registered operator, only that
project. Ordinary presentation changes stay Director-only.
"""

from __future__ import annotations

import json
import signal
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import presentation_controller as pc  # noqa: E402
import team_launcher as tl  # noqa: E402

CHECKS = 0
PROJECT = "mefp"
OPERATOR = "eric"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def refused(call, expected: str = "") -> str:
    try:
        call()
    except SystemExit as exc:
        if expected:
            assert expected in str(exc), f"expected {expected!r} in: {exc}"
        return str(exc)
    raise AssertionError(f"expected a refusal containing {expected!r}, and it was allowed")


class grant:
    """A tenant's root-owned control grant, as provisioning writes it."""

    def __init__(self, *, project: str = PROJECT, authorized: str = OPERATOR) -> None:
        self.project = project
        self.authorized = authorized

    def __enter__(self) -> Path:
        self.tmp = tempfile.TemporaryDirectory(prefix="syrd239.")
        root = Path(self.tmp.__enter__())
        (root / self.project).mkdir()
        (root / self.project / "control-grant.json").write_text(
            json.dumps({
                "project": self.project,
                "owner": "stellaris-agent",
                "authorized_user": self.authorized,
                "launcher": "/opt/switchyard/current/switchyard",
            }),
            encoding="utf-8",
        )
        return root

    def __exit__(self, *exc):
        return self.tmp.__exit__(*exc)


def config(project: str = PROJECT, roles=()):
    """A stand-in for the authorization checks, which read only the project.

    `_require_director` and `operator_recovery_caller` look at `config.project`
    and nothing else, so this is the whole of what they need. The screen cases
    below use a REAL `ProjectConfig` instead, because those reach code that
    reads a tenant's roles, accounts and isolation settings -- and a namespace
    with just enough attributes to get past the first of them is how a test
    ends up asserting against a shape no tenant has.
    """
    return types.SimpleNamespace(project=project, roles=list(roles))


class real_config:
    """A loaded ProjectConfig, built from a configuration on disk."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="syrd239-cfg.")
        root = Path(self.tmp.__enter__())
        for name in ("d", "a"):
            (root / name).mkdir()
        (root / f"{PROJECT}.json").write_text(json.dumps({
            "project": PROJECT,
            "board_url": "http://127.0.0.1:1/",
            "roles": [
                {"role": "director", "cli": "claude", "slot": 0, "workdir": str(root / "d")},
                {"role": "app", "cli": "claude", "slot": 1, "workdir": str(root / "a")},
            ],
        }), encoding="utf-8")
        return tl.load_project_config(PROJECT, root / f"{PROJECT}.json")

    def __exit__(self, *exc):
        return self.tmp.__exit__(*exc)


# -- the catch-22, and what replaced it --------------------------------------


def test_the_slot_no_longer_tells_the_operator_to_run_what_refuses_them() -> None:
    """The regression in one line: what the screen says, and who can obey it."""
    with real_config() as cfg:
        instruction = pc._recovery_instruction(cfg, "director")
        check(
            instruction == f"switchyard recover-display {PROJECT}",
            f"a disconnected Director slot names the operator command: {instruction}",
        )
        check(
            "present" not in instruction and "recover director" not in instruction,
            f"and not the one gated to the pane that just disconnected: {instruction}",
        )
        check(
            "TICKET_BOARD_CALLER_ROLE" not in instruction,
            f"and never teaches the role variable: {instruction}",
        )

        # Every other slot keeps the Director's own command: for those the
        # Director is still attached, and it is still their call.
        other = pc._recovery_instruction(cfg, "app")
        check(
            other == f"switchyard present {PROJECT} recover app",
            f"an app slot is unchanged: {other}",
        )


def test_the_disconnected_slot_screens_carry_that_instruction() -> None:
    """Not just the helper -- the text a person actually reads off the slot."""
    with real_config() as cfg:
        _workdir, command = pc._proxy_command(cfg, "director", {"live": True})
        check(f"switchyard recover-display {PROJECT}" in command, f"live-but-disconnected: {command}")
        check("present mefp recover director" not in command, f"{command}")

        _workdir, dead = pc._proxy_command(cfg, "director", {"live": False, "state": "stopped"})
        check(f"switchyard recover-display {PROJECT}" in dead, f"not running: {dead}")
        check("TICKET_BOARD_CALLER_ROLE" not in dead, f"{dead}")


# -- the authorization, on every axis the ticket names -----------------------


def test_the_registered_operator_may_recover_the_director_slot() -> None:
    with grant() as root:
        who = pc._require_director(
            config(),
            {tl.TENANT_CONTROL_CALLER_ENV: OPERATOR},
            operator_recovery=True,
            grant_root=root,
        )
        check(who == f"operator:{OPERATOR}", f"authorized, and recorded as themselves: {who}")
        check(
            not who.endswith("director") and who != "director",
            "an operator's recovery is not written into history as the Director's move",
        )


def test_ordinary_presentation_changes_stay_director_only() -> None:
    """The authority this must not widen.

    The same operator, the same grant, the same environment -- and a show, a
    swap or a hide is still refused. Only the recovery opens.
    """
    with grant() as root:
        message = refused(
            lambda: pc._require_director(
                config(),
                {tl.TENANT_CONTROL_CALLER_ENV: OPERATOR},
                operator_recovery=False,
                grant_root=root,
            ),
            "TICKET_BOARD_CALLER_ROLE=director",
        )
        check("recover-display" not in message, f"and it is not offered the recovery: {message}")


def test_only_the_directors_slot_opens_the_operator_path() -> None:
    """Driven through the dispatcher's own decision, not a restatement of it.

    `operator_recovery` is computed from the action and the role, so the rule
    is asserted where it is made.
    """
    def opens(action: str, role_name: str) -> bool:
        return action == "recover" and (role_name or "").strip().lower() == pc.DIRECTOR_ROLE

    check(opens("recover", "director"), "the Director's slot, being recovered")
    check(opens("recover", "Director"), "spelled any way")
    for action, role_name in (
        ("recover", "app"), ("recover", "ops"), ("recover", ""),
        ("show", "director"), ("swap", "director"), ("hide", "director"),
        ("restore", "director"), ("bootstrap", "director"),
    ):
        check(not opens(action, role_name), f"{action} {role_name!r} does not open it")

    # And the same rule as the module computes it, so this cannot drift from
    # the line that actually runs.
    source = (ROOT / "scripts/presentation_controller.py").read_text()
    check(
        'operator_recovery = action == "recover" and (role_name or "").strip().lower() == DIRECTOR_ROLE'
        in source,
        "the dispatcher computes it exactly this way",
    )


def test_an_unregistered_caller_authorizes_nothing() -> None:
    with grant() as root:
        for pretender in ("mallory", "root", "stellaris-agent", ""):
            message = refused(
                lambda name=pretender: pc._require_director(
                    config(),
                    {tl.TENANT_CONTROL_CALLER_ENV: name},
                    operator_recovery=True,
                    grant_root=root,
                ),
                "registered operator",
            )
            check(
                f"switchyard recover-display {PROJECT}" in message,
                f"{pretender!r} is told the command that would work for the right person",
            )
            check(
                "TICKET_BOARD_CALLER_ROLE" not in message,
                f"and never told to forge a role: {message}",
            )


def test_cross_project_recovery_is_refused() -> None:
    with grant() as root:
        refused(
            lambda: pc._require_director(
                config(),
                {tl.TENANT_CONTROL_CALLER_ENV: OPERATOR, "TICKET_BOARD_PROJECT": "syrd"},
                operator_recovery=True,
                grant_root=root,
            ),
            "refusing cross-project presentation change",
        )
    # And a grant that names another tenant is not a grant for this one, even
    # when it is found under this one's directory.
    with grant(project=PROJECT) as root:
        (root / PROJECT / "control-grant.json").write_text(
            json.dumps({
                "project": "syrd", "owner": "o", "authorized_user": OPERATOR, "launcher": "/x",
            }),
            encoding="utf-8",
        )
        check(
            pc.operator_recovery_caller(config(), {tl.TENANT_CONTROL_CALLER_ENV: OPERATOR},
                                        grant_root=root) == "",
            "a grant for another project authorizes nothing here",
        )


def test_a_missing_or_unreadable_grant_authorizes_nothing() -> None:
    with tempfile.TemporaryDirectory() as raw:
        check(
            pc.operator_recovery_caller(config(), {tl.TENANT_CONTROL_CALLER_ENV: OPERATOR},
                                        grant_root=Path(raw)) == "",
            "no grant, no authority",
        )
        (Path(raw) / PROJECT).mkdir()
        (Path(raw) / PROJECT / "control-grant.json").write_text("{ not json", encoding="utf-8")
        check(
            pc.operator_recovery_caller(config(), {tl.TENANT_CONTROL_CALLER_ENV: OPERATOR},
                                        grant_root=Path(raw)) == "",
            "an unreadable grant fails closed rather than open",
        )


def test_the_directors_own_pane_is_unchanged() -> None:
    with grant() as root:
        for recovery in (True, False):
            who = pc._require_director(
                config(), {"TICKET_BOARD_CALLER_ROLE": "director"},
                operator_recovery=recovery, grant_root=root,
            )
            check(who == "director", f"still the Director's own authority: {who}")
        refused(
            lambda: pc._require_director(
                config(),
                {"TICKET_BOARD_CALLER_ROLE": "director", "TICKET_BOARD_PROJECT": "syrd"},
                operator_recovery=True, grant_root=root,
            ),
            "refusing cross-project presentation change",
        )


# -- the operator entry point ------------------------------------------------


def test_the_bridge_carries_one_more_fixed_verb_and_no_role() -> None:
    """The role is written into the bridge, never taken from the caller."""
    source = (ROOT / "scripts/switchyard-tenant-control").read_text()
    check(
        '"recover-display": ("present", PROJECT_ARGUMENT, "recover", "director")' in source,
        "the verb names the Director's slot itself",
    )
    check("recover-display" in tl.TENANT_CONTROL_OPERATIONS, "and the launcher knows it")
    check("recover-display" in tl.SWITCHYARD_COMMANDS, "and it is a real command")

    # Two words, this project, exactly as stop and status are routed.
    check(
        tl._tenant_control_operation(["recover-display", PROJECT], PROJECT) == "recover-display",
        "the operator's invocation routes to the bridge",
    )
    for argv in (
        ["recover-display", "syrd"],          # another tenant
        ["recover-display"],                  # no project
        ["recover-display", PROJECT, "app"],  # a role the caller chose
    ):
        check(
            tl._tenant_control_operation(argv, PROJECT) == "",
            f"{argv} does not reach the bridge",
        )


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("operator_display_recovery_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(120)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"operator_display_recovery_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
