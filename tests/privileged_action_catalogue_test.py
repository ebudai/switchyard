#!/usr/bin/env python3
"""SYRD-112: what may be asked for, and what may never be.

The failure this comes from was reached through a generic `pkexec` invocation,
and the fallback was another sudo command handed through chat. Both are
surfaces where the *command* is the payload. Here the payload is a name from a
fixed table plus values that have to parse, and the authorization surface has
nowhere to put a program, a path, a command string or an environment.
"""

from __future__ import annotations

import signal
import sys
import xml.dom.minidom
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import privileged_actions as pa  # noqa: E402

CHECKS = 0
HELPER = "/usr/local/lib/switchyard/switchyard-privileged-helper"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def test_every_catalogued_action_is_fully_declared() -> None:
    check(bool(pa.CATALOGUE), "the catalogue is not empty")
    for action in pa.CATALOGUE:
        check(action.action_id.startswith(pa.ACTION_NAMESPACE + "."), action.action_id)
        check(action.summary.strip() != "", f"{action.name} says what it does")
        check(action.message.strip() != "", f"{action.name} says what is being allowed")
        check(
            action.authentication in {pa.ALLOW_ACTIVE, pa.AUTH_ADMIN},
            f"{action.name} declares its authentication: {action.authentication}",
        )
        check(
            all(callable(validate) for _name, validate in action.arguments),
            f"{action.name}'s arguments are typed",
        )


def test_an_action_nobody_catalogued_is_refused_by_name() -> None:
    for name in ("", "run", "deploy-release; rm -rf /", "../deploy-release", "DEPLOY-RELEASE"):
        try:
            pa.action_for(name)
            check(False, f"{name!r} was accepted")
        except pa.ArgumentError as exc:
            check("not a catalogued action" in str(exc), str(exc))
            check("known actions:" in str(exc), f"and says what is available: {exc}")


def test_a_release_is_pinned_to_an_exact_commit() -> None:
    """A ref names whatever it points at when it is read, which is not what was approved."""
    action = pa.action_for("deploy-release")
    check(
        action.validate({"project": "mefp", "commit": COMMIT})["commit"] == COMMIT,
        "a full sha is accepted",
    )
    for bad in ("HEAD", "origin/main", "v1.2.3", COMMIT[:7], COMMIT[:39], COMMIT + "0",
                COMMIT.upper(), "", "  " + COMMIT):
        try:
            action.validate({"project": "mefp", "commit": bad})
            check(False, f"{bad!r} was accepted as a commit")
        except pa.ArgumentError as exc:
            check("content-addressed" in str(exc), str(exc))


def test_a_project_is_a_slug_and_never_a_path() -> None:
    action = pa.action_for("upgrade-tenant")
    check(action.validate({"project": "mefp"})["project"] == "mefp", "a slug is accepted")
    for bad in ("../etc", "/etc/passwd", "mefp/../syrd", "mefp;reboot", "mefp x", "Mefp",
                "-mefp", "", "m" * 33, "mefp\n", "mefp\0"):
        try:
            action.validate({"project": bad})
            check(False, f"{bad!r} was accepted as a project")
        except pa.ArgumentError as exc:
            check("slug" in str(exc), str(exc))


def test_nothing_undeclared_can_ride_along() -> None:
    """An extra argument is how a payload gets in. There is nowhere to put one."""
    action = pa.action_for("deploy-release")
    for extra in ("env", "PATH", "command", "--", "argv", "helper", "source"):
        try:
            action.validate({"project": "mefp", "commit": COMMIT, extra: "anything"})
            check(False, f"{extra!r} rode along")
        except pa.ArgumentError as exc:
            check("refusing unexpected" in str(exc), str(exc))
            check(extra in str(exc), f"and names it: {exc}")


def test_a_missing_or_mistyped_value_is_refused_whole() -> None:
    action = pa.action_for("deploy-release")
    for values in ({}, {"project": "mefp"}, {"commit": COMMIT}):
        try:
            action.validate(values)
            check(False, f"{values} was accepted")
        except pa.ArgumentError as exc:
            check("requires" in str(exc), str(exc))
    try:
        action.validate({"project": "mefp", "commit": None})
        check(False, "a non-string was accepted")
    except pa.ArgumentError as exc:
        check("must be text" in str(exc), str(exc))


def test_no_catalogued_action_accepts_a_path_or_a_command() -> None:
    """The property that makes the surface bounded, asserted over the table.

    Not "no action happens to take one today" -- nothing in the catalogue may
    ever take an argument whose value is a program, a path, a command or an
    environment, because polkit would then be authorizing whatever the caller
    put there.
    """
    forbidden = {"path", "command", "argv", "env", "environment", "script",
                 "executable", "shell", "source", "url", "file"}
    for action in pa.CATALOGUE:
        names = {name for name, _ in action.arguments}
        overlap = names & forbidden
        check(not overlap, f"{action.name} takes {overlap}, which is a payload")
        for name, validate in action.arguments:
            for attempt in ("/bin/sh", "; reboot", "$(id)", "../../etc/passwd", "a b"):
                try:
                    validate(attempt)
                    check(False, f"{action.name}.{name} accepted {attempt!r}")
                except pa.ArgumentError:
                    pass
    check(True, "every catalogued argument refuses a path, a command and an injection")


def test_the_policy_binds_every_action_to_one_fixed_helper() -> None:
    policy = pa.render_policy(HELPER)
    document = xml.dom.minidom.parseString(policy)
    actions = document.getElementsByTagName("action")
    check(len(actions) == len(pa.CATALOGUE), f"one entry per action: {len(actions)}")
    for element in actions:
        annotations = {
            node.getAttribute("key"): node.firstChild.data
            for node in element.getElementsByTagName("annotate")
        }
        check(
            annotations.get("org.freedesktop.policykit.exec.path") == HELPER,
            f"{element.getAttribute('id')} runs the fixed helper: {annotations}",
        )
        defaults = element.getElementsByTagName("defaults")[0]
        for tag in ("allow_any", "allow_inactive"):
            value = defaults.getElementsByTagName(tag)[0].firstChild.data
            check(value == "no", f"{element.getAttribute('id')} {tag}={value}")


def test_each_action_declares_its_own_authentication_in_the_policy() -> None:
    policy = pa.render_policy(HELPER)
    document = xml.dom.minidom.parseString(policy)
    rendered = {
        element.getAttribute("id"):
            element.getElementsByTagName("allow_active")[0].firstChild.data
        for element in document.getElementsByTagName("action")
    }
    for action in pa.CATALOGUE:
        check(
            rendered[action.action_id] == action.authentication,
            f"{action.name}: catalogue says {action.authentication}, policy says "
            f"{rendered[action.action_id]}",
        )
    check(
        rendered[f"{pa.ACTION_NAMESPACE}.install-shared-release"] == pa.AUTH_ADMIN,
        "a host-wide install asks a human",
    )
    check(
        rendered[f"{pa.ACTION_NAMESPACE}.deploy-release"] == pa.ALLOW_ACTIVE,
        "and a tenant deployment, whose caller is already proven, does not",
    )


def test_the_policy_survives_a_helper_path_with_xml_in_it() -> None:
    policy = pa.render_policy('/opt/sw & "x"/helper')
    document = xml.dom.minidom.parseString(policy)
    path = (
        document.getElementsByTagName("annotate")[0].firstChild.data
    )
    check(path == '/opt/sw & "x"/helper', f"escaped and read back intact: {path!r}")


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("privileged_action_catalogue_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(120)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"privileged_action_catalogue_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
