#!/usr/bin/env python3
"""SYRD-211: a registered tenant whose control helper is missing repairs itself.

Live Zorin UAT on release c5c5215 could not launch a preserved tenant at all:

    sudo: /usr/local/lib/switchyard/test/switchyard-tenant-control: command not found

The tenant was real, its grant was real and its sudoers rule was real -- only the
staged executable the rule names was absent, because provisioning had been
interrupted after writing them. The launcher trusted the registered path and handed
it to sudo, so sudo's own message became the launch result and nothing downstream --
including SYRD-210's CLI gate -- ever ran.

What must hold now:

* absence and hostility are different answers. Absence is re-staged from the current
  release through the recorded privileged path and the launch resumes; a wrong
  owner, a writable file, a symlink in the path, or a grant naming another project
  is refused with an exact reason and nothing is run in its place;
* the repair is scoped to one tenant, chosen by a slug that cannot aim it elsewhere;
* it is idempotent: a second launch verifies and runs no privileged step;
* it leaves a record, under its own label, that an operator can find afterwards.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher  # noqa: E402

CHECKS = 0


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


def _tenant(root: Path, project: str = "test") -> Path:
    directory = root / project
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _stage(directory: Path, *, mode: int = 0o755) -> Path:
    helper = directory / "switchyard-tenant-control"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(mode)
    return helper


def _grant(project: str = "test") -> dict[str, str]:
    return {"project": project, "authorized_user": launcher.current_user_name()}


class _Repairs:
    """A runner that performs the repair the way privileged staging would."""

    def __init__(self, directory: Path, *, succeed: bool = True, stages: bool = True) -> None:
        self.directory = directory
        self.succeed = succeed
        self.stages = stages
        self.commands: list[str] = []
        self.lines: list[str] = []

    def __call__(self, args, **_kwargs):
        self.commands.append(" ".join(args))
        if self.stages:
            _stage(self.directory)
        return subprocess.CompletedProcess(args, 0 if self.succeed else 3)

    def say(self, line: str) -> None:
        self.lines.append(line)


def test_a_missing_helper_is_absent_not_hostile() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd211-absent.") as tmp:
        root = Path(tmp)
        _tenant(root)
        state = launcher.tenant_control_helper_state("test", grant=_grant(), root=root)
        check(not state.present, "the helper is not there")
        check(state.reasons == (), f"and nothing is wrong with it: {state.reasons}")
        check(state.repairable, "so it is the one shape a release can put right")
        check(not state.usable, "but it is not usable as it stands")


def test_a_missing_helper_is_repaired_and_the_launch_resumes() -> None:
    """The reported failure, end to end, through the real bridge function."""
    with tempfile.TemporaryDirectory(prefix="syrd211-repair.") as tmp:
        root = Path(tmp)
        directory = _tenant(root)
        repairs = _Repairs(directory)
        launcher.ensure_tenant_control_helper(
            "test", grant=_grant(), root=root, runner=repairs, print_func=repairs.say
        )
        check(len(repairs.commands) == 1, f"exactly one privileged step ran: {repairs.commands}")
        state = launcher.tenant_control_helper_state("test", grant=_grant(), root=root)
        check(state.usable, f"and the helper is usable afterwards: {state.reasons}")
        check(any("repairing it from" in line for line in repairs.lines),
              f"the operator is told before it happens: {repairs.lines}")
        check(any("repaired at" in line for line in repairs.lines),
              f"and told what the result was: {repairs.lines}")


def test_the_repair_is_recorded_under_its_own_label() -> None:
    command = launcher.tenant_control_repair_command("test")
    check("switchyard-record-rollout" in command,
          f"the repair runs through the rollout recorder: {command[:120]}")
    check(launcher.TENANT_CONTROL_REPAIR_LABEL in command,
          f"under a label that distinguishes it from provisioning: {command[:160]}")
    check(" test " in f" {command} ", "and names the tenant it is for")


def test_the_repair_touches_no_other_tenant() -> None:
    command = launcher.tenant_control_repair_command("test")
    check("/usr/local/lib/switchyard/test" in command,
          "the staging directory is this tenant's")
    others = [token for token in command.split()
              if "/usr/local/lib/switchyard/" in token
              and "/usr/local/lib/switchyard/test" not in token]
    check(others == [], f"and no other tenant's path appears anywhere in it: {others}")


def test_a_slug_that_could_aim_elsewhere_is_refused_before_any_filesystem_call() -> None:
    """The slug comes from argv, so it chooses the directory this would repair."""
    with tempfile.TemporaryDirectory(prefix="syrd211-slug.") as tmp:
        root = Path(tmp)
        for slug in ("../other", "a/b", "", ".", "..", "other\\b"):
            state = launcher.tenant_control_helper_state(slug, root=root)
            check(bool(state.reasons), f"{slug!r} is refused: {state.reasons}")
            check(not state.repairable, f"and is never treated as a repairable absence: {slug!r}")


def test_a_valid_helper_costs_no_privileged_step() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd211-idempotent.") as tmp:
        root = Path(tmp)
        directory = _tenant(root)
        _stage(directory)
        calls: list[object] = []

        def never(args, **_kwargs):
            calls.append(args)
            raise AssertionError("a usable helper must not be rewritten")

        launcher.ensure_tenant_control_helper(
            "test", grant=_grant(), root=root, runner=never, print_func=lambda _l: None
        )
        check(calls == [], "a second launch runs nothing privileged")


def test_a_repair_is_idempotent_across_two_launches() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd211-rerun.") as tmp:
        root = Path(tmp)
        directory = _tenant(root)
        repairs = _Repairs(directory)
        launcher.ensure_tenant_control_helper(
            "test", grant=_grant(), root=root, runner=repairs, print_func=repairs.say
        )
        first = len(repairs.commands)
        launcher.ensure_tenant_control_helper(
            "test", grant=_grant(), root=root, runner=repairs, print_func=repairs.say
        )
        check(first == 1, f"the first launch repaired once: {repairs.commands}")
        check(len(repairs.commands) == 1,
              f"and the second reused it without rewriting: {repairs.commands}")


def test_hostile_helper_shapes_are_refused_rather_than_repaired() -> None:
    """None of these is something a re-stage should paper over."""

    def refuse(_args, **_kwargs):
        raise AssertionError("a hostile helper must never be repaired")

    cases = []
    with tempfile.TemporaryDirectory(prefix="syrd211-hostile.") as tmp:
        root = Path(tmp)

        directory = _tenant(root, "writable")
        _stage(directory, mode=0o777)
        cases.append(("writable", "group- or world-writable", {}))

        directory = _tenant(root, "notexec")
        _stage(directory, mode=0o644)
        cases.append(("notexec", "not executable", {}))

        directory = _tenant(root, "symlink")
        real = directory / "elsewhere"
        real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        real.chmod(0o755)
        (directory / "switchyard-tenant-control").symlink_to(real)
        cases.append(("symlink", "symlink", {}))

        directory = _tenant(root, "notregular")
        (directory / "switchyard-tenant-control").mkdir()
        cases.append(("notregular", "not a regular file", {}))

        directory = _tenant(root, "wrongowner")
        _stage(directory)
        # Naming the entitled uid rather than a literal 0 is what lets this be
        # exercised without a root-owned sandbox -- the same seam the staging
        # verifier uses (SYRD-62).
        cases.append(("wrongowner", "owned by", {"owner_uid": os.getuid() + 1}))

        for project, expected, extra in cases:
            state = launcher.tenant_control_helper_state(project, root=root, **extra)
            check(state.present, f"{project}: something is there")
            check(any(expected in reason for reason in state.reasons),
                  f"{project}: refused for the right reason: {state.reasons}")
            check(not state.repairable, f"{project}: and not treated as repairable")
            try:
                launcher.ensure_tenant_control_helper(
                    project, root=root, runner=refuse, print_func=lambda _l: None, **extra
                )
            except SystemExit as exc:
                message = str(exc)
                check("refusing to run" in message, f"{project}: the refusal says so: {message[:80]}")
                check(expected in message, f"{project}: and names the reason: {message[:160]}")
                check("in its place" in message,
                      f"{project}: and promises no fallback: {message[:200]}")
            else:
                raise AssertionError(f"{project}: a hostile helper must stop the launch")


def test_a_grant_naming_another_project_is_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd211-grant.") as tmp:
        root = Path(tmp)
        directory = _tenant(root)
        _stage(directory)
        state = launcher.tenant_control_helper_state(
            "test", grant={"project": "other", "authorized_user": "someone"}, root=root
        )
        check(bool(state.reasons), "configuration disagreement is caught")
        check(any("records project other" in reason for reason in state.reasons),
              f"and says exactly what disagrees: {state.reasons}")


def test_a_grant_without_a_project_key_is_not_a_disagreement() -> None:
    """Absent is not the same fact as different.

    `_tenant_control_grant` already refuses a grant whose project is not this
    one and hands back an empty mapping, so a missing key here means the grant
    was not read -- not that it names somebody else. Reading it as a
    disagreement refuses every correctly provisioned tenant whose caller passed
    a grant that simply does not carry the key.
    """
    with tempfile.TemporaryDirectory(prefix="syrd211-nokey.") as tmp:
        root = Path(tmp)
        directory = _tenant(root)
        _stage(directory)
        for grant in ({}, {"authorized_user": "someone"}, {"project": ""}):
            state = launcher.tenant_control_helper_state("test", grant=grant, root=root)
            check(state.usable, f"a grant of {grant!r} leaves the helper usable: {state.reasons}")


def test_a_repair_that_fails_stops_the_launch_and_points_at_the_journal() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd211-failed.") as tmp:
        root = Path(tmp)
        directory = _tenant(root)
        repairs = _Repairs(directory, succeed=False, stages=False)
        try:
            launcher.ensure_tenant_control_helper(
                "test", grant=_grant(), root=root, runner=repairs, print_func=repairs.say
            )
        except SystemExit as exc:
            message = str(exc)
            check("failed" in message, f"the failure is reported: {message}")
            check("rollout journal" in message,
                  f"and says where the attempt is recorded: {message}")
        else:
            raise AssertionError("a failed repair must not continue into sudo")


def test_a_repair_that_reports_success_but_stages_nothing_is_caught() -> None:
    """The second verification is the whole reason for the first."""
    with tempfile.TemporaryDirectory(prefix="syrd211-lying.") as tmp:
        root = Path(tmp)
        directory = _tenant(root)
        repairs = _Repairs(directory, succeed=True, stages=False)
        try:
            launcher.ensure_tenant_control_helper(
                "test", grant=_grant(), root=root, runner=repairs, print_func=repairs.say
            )
        except SystemExit as exc:
            check("still unusable after repair" in str(exc),
                  f"a repair that changed nothing is not success: {exc}")
        else:
            raise AssertionError("an unverified repair must not reach sudo")


def test_the_bridge_verifies_before_it_asks_root_to_run_anything() -> None:
    """Ordering: the gate must precede the sudo invocation, not follow it."""
    with tempfile.TemporaryDirectory(prefix="syrd211-order.") as tmp:
        root = Path(tmp)
        directory = _tenant(root)
        _stage(directory)
        seen: list[list[str]] = []

        def runner(args, **_kwargs):
            seen.append(list(args))
            return subprocess.CompletedProcess(args, 0)

        original = launcher.TENANT_CONTROL_ROOT
        launcher.TENANT_CONTROL_ROOT = root
        try:
            launcher._switchyard_exec_through_tenant_control(
                "test", "status", grant=_grant(), runner=runner,
                print_func=lambda _l: None,
            )
        except SystemExit:
            pass
        finally:
            launcher.TENANT_CONTROL_ROOT = original
        check(seen, "the bridge ran something")
        check(any("switchyard-tenant-control" in " ".join(args) for args in seen),
              f"and it reached the helper once it verified: {seen}")


def test_the_reported_failure_no_longer_reaches_sudo() -> None:
    """The exact SYRD-211 shape: registered, authorized, and not staged."""
    with tempfile.TemporaryDirectory(prefix="syrd211-reported.") as tmp:
        root = Path(tmp)
        directory = _tenant(root)
        invoked: list[list[str]] = []

        def runner(args, **_kwargs):
            invoked.append(list(args))
            if any("record-rollout" in token or "install -d" in token for token in args):
                _stage(directory)
                return subprocess.CompletedProcess(args, 0)
            return subprocess.CompletedProcess(args, 0)

        original = launcher.TENANT_CONTROL_ROOT
        launcher.TENANT_CONTROL_ROOT = root
        try:
            launcher._switchyard_exec_through_tenant_control(
                "test", "status", grant=_grant(), runner=runner, print_func=lambda _l: None
            )
        except SystemExit as exc:
            check(int(exc.code or 0) == 0, f"the launch continued rather than failing: {exc.code}")
        finally:
            launcher.TENANT_CONTROL_ROOT = original
        helper_calls = [args for args in invoked
                        if args[:1] == ["sudo"] and "switchyard-tenant-control" in " ".join(args)]
        check(helper_calls, f"the helper was finally invoked, not reported missing: {invoked}")
        check(launcher.tenant_control_helper_state("test", root=root).usable,
              "and the tenant is left with a usable helper")


def main() -> int:
    failures = 0
    for name, value in sorted(globals().items()):
        if not (name.startswith("test_") and callable(value)):
            continue
        try:
            value()
        except BaseException as exc:  # noqa: BLE001 - the runner names what escaped
            failures += 1
            print(f"FAILED {name}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"tenant_control_helper_repair_test: {failures} failed")
        return 1
    print(f"tenant_control_helper_repair_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
