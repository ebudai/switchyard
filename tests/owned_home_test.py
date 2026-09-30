#!/usr/bin/env python3
"""The containment the presentation and first-run suites run under, proved on its own (SYRD-343).

Nothing here may touch a real account's home, a real runtime directory or a
real process, and each case says how it knows: the guard refuses before the
syscall, and every case checks afterwards that what it would have made does
not exist.
"""

from __future__ import annotations

import json
import os
import pwd
import secrets
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import team_launcher as launcher  # noqa: E402

import owned_home  # noqa: E402
from owned_home import HostGuard, OwnedAccounts, Refused, contained  # noqa: E402

CHECKS = 0
REAL_HOME = Path(owned_home._REAL_PWD["getpwuid"](os.getuid()).pw_dir)


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def _remove_own_probe(path: Path) -> None:
    """Only ever this test's own randomly named probe: if a broken guard let it be made, it goes."""
    if path.is_dir() and not path.is_symlink():
        path.rmdir()
    elif path.exists() or path.is_symlink():
        path.unlink()


def _bridge():
    import types

    module = types.ModuleType("tenant_control_under_test")
    module.__dict__["__name__"] = "tenant_control_under_test"
    source = (ROOT / "scripts" / "switchyard-tenant-control").read_text(encoding="utf-8")
    exec(compile(source, "switchyard-tenant-control", "exec"), module.__dict__)  # noqa: S102
    return module


def test_the_bridge_publishes_where_the_caller_consumes_and_both_are_owned() -> None:
    """Root's real `publish_handoff` and the caller's real reader agree -- on an owned path."""
    real_destination = REAL_HOME / ".local/state/switchyard/projects/syrd343probe/syrd343probe-presentation-handoff.json"
    check(not real_destination.exists(), f"a leftover exists before the case: {real_destination}")

    @contained
    def case() -> None:
        me = launcher.current_user_name()
        check(me == owned_home.ME, f"the caller keeps its real name: {me}")
        payload = launcher.render_presentation_handoff(
            "syrd343probe", slot_count=1, pane_program=owned_home.root_pinned_program(),
            slot_titles=["Probe"], window_title="Probe",
        )
        _bridge().publish_handoff("syrd343probe", json.dumps(payload), caller=me)
        consumed_at = launcher.presentation_handoff_path("syrd343probe", me)
        owned_home.assert_owned(consumed_at)
        check(consumed_at.is_file(), f"root published where the caller reads: {consumed_at}")
        written = json.loads(consumed_at.read_text(encoding="utf-8"))
        validated, problem = launcher.validated_presentation_handoff(
            written, project="syrd343probe", runner=owned_home.plain_acl_runner)
        check(not problem and validated["slot_count"] == 1, (problem, validated))

    case()
    check(not real_destination.exists(), f"the real home was written: {real_destination}")


def test_nothing_depends_on_a_host_s_installed_helper() -> None:
    """The pinned program is one every host has, and its ACL is read without a process."""
    with HostGuard() as guard:
        program = owned_home.root_pinned_program()
        reasons = launcher.untrusted_root_executable_reasons(program, owner_uid=0, runner=owned_home.plain_acl_runner)
    check(reasons == [], f"root's own program passes root pinning: {reasons}")
    check(guard.attempts == [], f"and nothing was spawned or read from a home: {guard.attempts}")


def test_the_sandbox_goes_even_when_the_case_fails() -> None:
    seen: list[Path] = []

    @contained
    def failing() -> None:
        home = Path(pwd.getpwnam("somebody").pw_dir)
        seen.append(home)
        (home / "left-behind").write_text("x", encoding="utf-8")
        raise AssertionError("a case that fails half way")

    try:
        failing()
        raised = ""
    except AssertionError as exc:
        raised = str(exc)
    check(raised == "a case that fails half way", f"the case's own failure is what surfaces: {raised!r}")
    check(seen and not seen[0].exists(), f"its sandbox was removed anyway: {seen}")


def test_a_real_path_this_account_could_write_is_refused_before_anything_happens() -> None:
    """Not a permission boundary: this account owns its home, and is refused all the same."""
    probe = REAL_HOME / ".local" / "state" / f"syrd343-probe-{secrets.token_hex(4)}"
    check(os.access(REAL_HOME, os.W_OK), "this account can write its own home, so a refusal is the guard's")
    # A file that is really there, so a read the guard let through would succeed.
    existing = next(entry for entry in sorted(REAL_HOME.iterdir()) if entry.is_file() and not entry.is_symlink())
    attempts = []
    try:
        with HostGuard() as guard:
            for label, attempt in (
                ("write", lambda: probe.write_text("x", encoding="utf-8")),
                ("mkdir", lambda: probe.mkdir(parents=True)),
                ("os.open", lambda: os.close(os.open(probe, os.O_WRONLY | os.O_CREAT, 0o600))),
                ("read", lambda: existing.read_bytes()),
                ("process", lambda: __import__("subprocess").run(["true"], check=False)),
                ("runtime socket", lambda: __import__("socket").socket(1).connect(f"/run/user/{os.getuid()}/bus")),
            ):
                try:
                    attempt()
                except Refused:
                    attempts.append(label)
                    continue
                raise AssertionError(f"the guard let a {label} through")
        check(attempts == ["write", "mkdir", "os.open", "read", "process", "runtime socket"], attempts)
        check(len(guard.attempts) == 6, guard.attempts)
        check(not probe.exists(), f"nothing was created: {probe}")
    finally:
        # Whatever failed, and wherever: a broken guard's probe does not stay.
        _remove_own_probe(probe)


def test_owned_accounts_answer_every_lookup_inside_the_sandbox_and_restore_them() -> None:
    real = {name: getattr(pwd, name) for name in ("getpwnam", "getpwuid", "getpwall")}
    real_runtime = launcher.runtime_dir_for_uid
    with tempfile.TemporaryDirectory(prefix="syrd343-accounts.") as tmp:
        with OwnedAccounts(Path(tmp)):
            for user in ("eric", owned_home.ME, "no-such-account-anywhere"):
                entry = pwd.getpwnam(user)
                check(Path(entry.pw_dir).is_relative_to(tmp) and entry.pw_uid == os.getuid(), (user, entry))
            check(pwd.getpwuid(os.getuid()).pw_name == owned_home.ME, "this uid keeps its real name")
            check(launcher._gui_home("eric").startswith(tmp), launcher._gui_home("eric"))
            check(str(launcher.desktop_state_dir("syrd", "eric")).startswith(tmp), "the consumer's path is owned")
            check(str(launcher.runtime_dir_for_uid(os.getuid())).startswith(tmp), "and so is the runtime dir")
    check({name: getattr(pwd, name) for name in real} == real, "every lookup is restored")
    check(launcher.runtime_dir_for_uid is real_runtime, "and so is the runtime dir")


def test_a_refusal_the_case_swallowed_still_fails_the_case() -> None:
    """Product code that catches a failure must not turn an escape into a pass."""
    target = REAL_HOME / ".local" / "state" / f"syrd343-swallowed-{secrets.token_hex(4)}"

    @contained
    def swallowing() -> None:
        try:
            target.write_text("x", encoding="utf-8")
        except Exception:  # noqa: BLE001 - exactly what a careless caller would do
            pass

    try:
        swallowing()
        failed = ""
    except Refused as exc:
        failed = str(exc)
    try:
        check("reached past its sandbox" in failed and str(target) in failed, f"the escape was not reported: {failed!r}")
        check(not target.exists(), f"and nothing was written: {target}")
    finally:
        _remove_own_probe(target)


def test_a_bridge_descriptor_the_runner_inherited_is_not_the_case_s() -> None:
    """A pane launched through the bridge hands its window back on an fd; a case must never write to it."""
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    names = owned_home.bridge_environment()
    saved = {name: os.environ.get(name) for name in names}
    os.environ[launcher.PRESENTATION_HANDOFF_FD_ENV] = str(write_fd)
    os.environ[launcher.TENANT_CONTROL_CALLER_ENV] = "somebody"
    seen: dict[str, str | None] = {}

    handed: list[object] = []

    @contained
    def launching_case() -> None:
        from first_run_login_inheritance_test import uat_config

        seen.update({name: os.environ.get(name) for name in names})
        with tempfile.TemporaryDirectory(prefix="syrd343-tenant.") as tenant:
            # The real hand-back the first-run launch case reaches: bridged, it
            # writes the window's payload to the handoff descriptor.
            handed.append(launcher.hand_presentation_back_to_the_caller(
                uat_config(Path(tenant)), slot_count=1, window_title="Probe", layout=launcher.LAYOUT_MODE_VIEWER,
                slot_titles=["Probe"], pane_program=owned_home.root_pinned_program(), print_func=lambda _l: None,
            ))

    try:
        launching_case()
        check(all(value is None for value in seen.values()), f"the inherited bridge context reached the case: {seen}")
        check(handed == [False], f"so the case's launch was not a bridged one: {handed}")
        try:
            leaked = os.read(read_fd, 4096)
        except BlockingIOError:
            leaked = b""
        check(leaked == b"", f"something was written to the runner's own descriptor: {leaked[:80]!r}")
        check(os.environ.get(launcher.PRESENTATION_HANDOFF_FD_ENV) == str(write_fd), "and it is restored afterwards")
    finally:
        os.close(read_fd)
        os.close(write_fd)
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_assert_owned_refuses_a_real_home() -> None:
    try:
        owned_home.assert_owned(REAL_HOME / ".local" / "state")
        refused = False
    except Refused:
        refused = True
    check(refused, "a path in the real home is not an owned test path")


def main() -> int:
    for name, case in list(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"owned_home_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
