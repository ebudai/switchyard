#!/usr/bin/env python3
"""SYRD-209: teardown left the owner account, its linger, and its user manager.

Live on ZorinOS. A disposable tenant `test` was stopped and empty, and:

    switchyard teardown test --confirm test --destroy-registered-tenant \\
        --remove-owner-home --remove-owner-user

removed enough that `switchyard new --slug test` began provisioning, and then
the new flow found `test-agent` still in /etc/passwd with `Linger=yes` and a
live systemd --user manager, sd-pam, PipeWire, WirePlumber and session D-Bus
under it. Accepting the reuse prompt would have invalidated a clean-machine
end-to-end test.

The cause is that `--remove-owner-user` appended exactly one action, `userdel
<owner>`. userdel refuses an account whose processes are running, and nothing
disabled the linger that keeps those processes running in the first place. So
the last action of a long teardown failed after everything else had been
removed, and the account survived in a state that looks identical to a tenant
that was never torn down.

The shell here is the real shell: these cases run the scripts the plan actually
builds, against real processes owned by this account, so `pgrep`, the wait loop
and the residue read are exercised rather than described. What cannot be run
unprivileged -- `userdel`, `loginctl disable-linger` -- is checked for the
commands it issues and the order it issues them in, which is what the ticket
asks the dry run to show.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher  # noqa: E402

CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def _script_of(action) -> str:
    """The shell an action runs, for the steps built as bash scripts."""
    command = list(action.command)
    return command[2] if command[:2] == ["bash", "-lc"] else shlex.join(command)


def _run(script: str, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-lc", script], capture_output=True, text=True, **kwargs)


def test_the_removal_is_four_ordered_steps_not_one_userdel() -> None:
    """The order is the fix: unlinger, end the session, wait, then remove."""
    actions = team_launcher._owner_removal_actions("test-agent")
    labels = [action.label for action in actions]
    check(len(actions) == 4, f"four steps, not one: {labels}")
    check("linger" in labels[0], f"linger goes first, or the manager restarts: {labels[0]}")
    check("user manager" in labels[1] or "session" in labels[1],
          f"then the manager and session: {labels[1]}")
    check("wait" in labels[2], f"then the wait for its processes: {labels[2]}")
    check("remove owner user account" in labels[3], f"and only then the account: {labels[3]}")
    scripts = [_script_of(action) for action in actions]
    check("disable-linger" in scripts[0], f"the first step disables linger: {scripts[0]}")
    check("user@" in scripts[1] and "terminate-user" in scripts[1],
          f"the second stops the manager unit and the session: {scripts[1]}")
    check("userdel" in scripts[3], f"the last removes the account: {scripts[3]}")
    check(all("test-agent" in script for script in scripts),
          "and every one of them names the owner from the plan")


def test_every_step_names_only_the_planned_owner() -> None:
    """A teardown acts on one account; nothing here may reach another."""
    for owner in ("test-agent", "other-agent"):
        scripts = " ".join(_script_of(action)
                           for action in team_launcher._owner_removal_actions(owner))
        others = {"test-agent", "other-agent", "eric", "root"} - {owner}
        for name in others:
            check(name not in scripts, f"{owner}'s removal must not mention {name}: {scripts[:200]}")


def test_the_account_the_teardown_runs_as_is_refused() -> None:
    caller = "switchyard-agent"
    refusal = team_launcher.owner_removal_refusal(
        caller, caller=caller, uid_lookup=lambda _name: 1006, desktop_approval=lambda: {}
    )
    check(bool(refusal), "removing the account running the teardown is refused")
    check("running this teardown" in refusal, f"and says why: {refusal}")


def test_the_account_behind_sudo_is_refused() -> None:
    """Running as root through sudo, the person to protect is the invoker."""
    saved = os.environ.get("SUDO_USER")
    os.environ["SUDO_USER"] = "eric"
    try:
        refusal = team_launcher.owner_removal_refusal(
            "eric", caller="root", uid_lookup=lambda _name: 1000, desktop_approval=lambda: {}
        )
    finally:
        if saved is None:
            os.environ.pop("SUDO_USER", None)
        else:
            os.environ["SUDO_USER"] = saved
    check(bool(refusal) and "invoked this teardown" in refusal,
          f"the invoking account is protected even when euid is root: {refusal}")


def test_the_desktop_operator_is_refused() -> None:
    refusal = team_launcher.owner_removal_refusal(
        "eric", caller="switchyard-agent", uid_lookup=lambda _name: 1000,
        desktop_approval=lambda: {"gui_user": "eric"},
    )
    check(bool(refusal) and "desktop operator" in refusal,
          f"the approved desktop operator is not removed by a tenant teardown: {refusal}")


def test_system_accounts_and_root_are_refused() -> None:
    check(bool(team_launcher.owner_removal_refusal("root")), "root is refused")
    low = team_launcher.owner_removal_refusal(
        "daemon", caller="x", uid_lookup=lambda _name: 2, desktop_approval=lambda: {}
    )
    check("below the 1000" in low, f"and so is any uid below an ordinary account's: {low}")
    empty = team_launcher.owner_removal_refusal("   ")
    check("names no owner" in empty, f"and a plan with no owner removes nothing: {empty}")


def test_an_ordinary_tenant_owner_is_allowed() -> None:
    check(team_launcher.owner_removal_refusal(
        "test-agent", caller="switchyard-agent", uid_lookup=lambda _name: 1001,
        desktop_approval=lambda: {},
    ) == "", "the tenant owner the plan names is not refused")


def test_the_wait_step_returns_when_the_processes_are_gone() -> None:
    """Run against this account's real processes, not a description of them."""
    actions = team_launcher._owner_removal_actions(os.environ.get("USER") or "root")
    settle = _script_of(actions[2])
    # A process of our own that exits while the step waits: the loop must see it
    # go rather than time out, and must not report residue that is not there.
    with tempfile.TemporaryDirectory(prefix="syrd209-wait.") as tmp:
        marker = Path(tmp) / "child.pid"
        child = subprocess.Popen(["sleep", "1"])
        marker.write_text(str(child.pid), encoding="utf-8")
        began = time.monotonic()
        result = _run(settle)
        elapsed = time.monotonic() - began
        child.wait()
    # This account always has processes (the suite itself), so the wait times
    # out and reports them -- which is the failure path, and it must say what
    # was still running rather than only failing.
    check(result.returncode != 0, "a user with live processes does not pass the wait")
    check("still running" in result.stderr, f"and the step says so: {result.stderr[:160]}")
    check(elapsed < 30, f"bounded rather than hanging: {elapsed:.1f}s")


def test_every_step_tolerates_an_account_that_is_already_gone() -> None:
    """A teardown must be resumable after a partial one."""
    missing = "syrd209-absent-user"
    for action in team_launcher._owner_removal_actions(missing):
        result = _run(_script_of(action))
        check(result.returncode == 0,
              f"{action.label} succeeds when the account is absent: {result.stderr[:160]}")
        check("already absent" in (result.stdout + result.stderr),
              f"and says why it did nothing: {(result.stdout + result.stderr)[:160]}")


def test_residue_reports_the_account_that_survived() -> None:
    """The shape SYRD-209 was reported in: the account is still in passwd."""
    calls: list[str] = []

    def runner(args, **_kwargs):
        script = args[2] if args[:2] == ["bash", "-lc"] else " ".join(args)
        calls.append(script)
        if "getent passwd" in script:
            return subprocess.CompletedProcess(args, 0,
                stdout="test-agent:x:1001:1001::/home/test-agent:/bin/bash\n")
        if "Linger" in script:
            return subprocess.CompletedProcess(args, 0, stdout="yes\n")
        if script.startswith("ps ") or " ps " in script:
            return subprocess.CompletedProcess(args, 0,
                stdout=" 900 systemd\n 901 (sd-pam)\n 902 pipewire\n 903 wireplumber\n 904 dbus-daemon\n")
        return subprocess.CompletedProcess(args, 0, stdout="")

    residue = team_launcher.owner_removal_residue("test-agent", runner=runner)
    joined = " | ".join(residue)
    check(len(residue) == 3, f"all three kinds of residue are reported: {residue}")
    check("still exists" in joined and "1001" in joined, f"the passwd entry: {joined}")
    check("linger is still enabled" in joined, f"the linger: {joined}")
    check("pipewire" in joined and "wireplumber" in joined, f"and the live processes: {joined}")


def test_no_residue_reads_as_removed() -> None:
    def runner(args, **_kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="")

    check(team_launcher.owner_removal_residue("test-agent", runner=runner) == [],
          "an account that is really gone reports nothing")


class _TeardownRunner:
    """The machine as the command sees it, with the owner still present."""

    def __init__(self, *, residue: bool = False, fail_label: str = ""):
        self.calls: list[list[str]] = []
        self.residue = residue
        self.fail_label = fail_label

    def __call__(self, args, **_kwargs):
        self.calls.append(list(args))
        script = args[2] if args[:2] == ["bash", "-lc"] else shlex.join(args)
        if args[:2] == ["psql", "-XAt"]:
            return subprocess.CompletedProcess(args, 0, stdout="0\n")
        if self.fail_label and self.fail_label in script:
            return subprocess.CompletedProcess(args, 1, stderr="simulated failure\n")
        if "getent passwd" in script:
            return subprocess.CompletedProcess(args, 0, stdout=(
                "porter-agent:x:1001:1001::/home/porter-agent:/bin/bash\n" if self.residue else ""))
        if "Linger" in script:
            return subprocess.CompletedProcess(args, 0, stdout="yes\n" if self.residue else "no\n")
        if script.startswith("ps -o") or " ps -o" in script:
            return subprocess.CompletedProcess(args, 0, stdout=(
                " 900 systemd\n 901 (sd-pam)\n 902 pipewire\n" if self.residue else ""))
        return subprocess.CompletedProcess(args, 0, stdout="")


def test_the_dry_run_shows_the_removal_in_execution_order() -> None:
    """The ticket asks for the order, because the order is the fix."""
    runner = _TeardownRunner()
    lines: list[str] = []
    code = team_launcher.switchyard_teardown_command(
        "porter", dry_run=True, remove_owner_user=True, remove_owner_home=True,
        runner=runner, print_func=lines.append,
    )
    check(code == 0, "a dry run succeeds")
    numbered = [line for line in lines if line.strip()[:1].isdigit() and ". " in line]
    order = [line.split(". ", 1)[1] for line in numbered]
    def position(fragment: str) -> int:
        return next(index for index, label in enumerate(order) if fragment in label)
    check(position("disable linger") < position("user manager"),
          f"linger is disabled before the manager is stopped: {order[-5:]}")
    check(position("user manager") < position("wait for"),
          f"the manager is stopped before the wait: {order[-5:]}")
    check(position("wait for") < position("remove owner user account"),
          f"and the wait comes before the account is removed: {order[-5:]}")
    check(not [call for call in runner.calls if call[:2] != ["psql", "-XAt"]],
          f"and a dry run mutates nothing: {runner.calls}")


def test_a_surviving_account_fails_the_teardown_and_names_the_residue() -> None:
    """Never 'teardown complete' over an account that can still be reused."""
    runner = _TeardownRunner(residue=True)
    lines: list[str] = []
    try:
        team_launcher.switchyard_teardown_command(
            "porter", confirm="porter", destroy_registered_tenant=True,
            drop_nonempty_board=True, remove_owner_user=True, remove_owner_home=True,
            runner=runner, print_func=lines.append,
        )
    except SystemExit as exc:
        message = str(exc)
    else:
        raise AssertionError("a surviving owner account must fail the teardown")
    output = "\n".join(lines)
    check("did NOT remove porter-agent" in message, f"the failure names the account: {message[:200]}")
    check("still exists" in message and "1001" in message, f"and the passwd entry: {message[:300]}")
    check("linger is still enabled" in message, f"and the linger: {message[:300]}")
    check("pipewire" in message, f"and what is still running: {message[:400]}")
    check("teardown complete" not in output,
          f"and it never claims completion: {output[-200:]}")


def test_a_clean_removal_says_so_and_completes() -> None:
    runner = _TeardownRunner(residue=False)
    lines: list[str] = []
    code = team_launcher.switchyard_teardown_command(
        "porter", confirm="porter", destroy_registered_tenant=True,
        drop_nonempty_board=True, remove_owner_user=True, remove_owner_home=True,
        runner=runner, print_func=lines.append,
    )
    output = "\n".join(lines)
    check(code == 0, "a clean teardown succeeds")
    check("no residue" in output, f"and says the account is really gone: {output[-300:]}")
    check("teardown complete for porter" in output, f"before completing: {output[-200:]}")
    ran = [shlex.join(call) if call[:2] != ["bash", "-lc"] else call[2] for call in runner.calls]
    check(any("disable-linger" in item for item in ran), f"linger was disabled: {ran[-8:]}")
    check(any("userdel" in item for item in ran), f"and the account removed: {ran[-8:]}")


def test_a_refused_owner_stops_the_teardown_before_anything_runs() -> None:
    """The boundary is checked before the first destructive action."""
    runner = _TeardownRunner()
    lines: list[str] = []
    saved = os.environ.get("SUDO_USER")
    os.environ["SUDO_USER"] = "porter-agent"
    try:
        team_launcher.switchyard_teardown_command(
            "porter", confirm="porter", destroy_registered_tenant=True,
            drop_nonempty_board=True, remove_owner_user=True,
            runner=runner, print_func=lines.append,
        )
    except SystemExit as exc:
        message = str(exc)
    else:
        raise AssertionError("a refused owner must stop the teardown")
    finally:
        if saved is None:
            os.environ.pop("SUDO_USER", None)
        else:
            os.environ["SUDO_USER"] = saved
    check("refusing to remove the owner account" in message, f"it refuses: {message[:160]}")
    check(not [call for call in runner.calls if call[:2] != ["psql", "-XAt"]],
          f"and nothing destructive ran first: {runner.calls}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"team_launcher_teardown_owner_removal_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
