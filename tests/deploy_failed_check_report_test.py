#!/usr/bin/env python3
"""SYRD-773: a rolled-back deploy names the post-restart check that failed, and root's journal keeps all it said.

On 2026-11-13 a syrd deploy rolled back with exit 3. Its only account of why was the first 400 characters of the
deploy's stderr, cut mid-path inside the canary's lines. Root's rollout journal had the same cut text: it records what
the privileged action prints, and the action printed only that problem. deploy-restart has five checks after the
restart that each roll back with exit 3 -- the HTTP smoke check, the live build id, the commit repositories, the
listener's pane-state authority and the system runtime -- and which one failed, and what it said, was gone.

Here, for each of the five in turn:
* the real `deploy_restart_service` (ticket-board-service.sh, sourced) runs with every step before the checks stubbed,
  the chosen check failing with a long message whose end is unique to it, and the real `rollback_live_service` and
  `log`. It exits 3, writes `post-restart-check-failed: <check>` and the rollback's result naming it;
* that real output goes through `deploy_failure` -- what `deploy_release_in_transaction` now calls -- with its printed
  lines written into a real rollout-journal attempt, captured as a privileged job is;
* the problem names the check, and the journal's stdout.log holds the check's whole message, uncut, the rollback's
  result, and is not truncated.
Also: output with no check named is quoted from its end, not its start.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import deploy_failure_report  # noqa: E402
from scripts.ticket_board import rollout_journal  # noqa: E402

SERVICE_SCRIPT = ROOT / "scripts" / "ticket-board-service.sh"
CHECKS = 0
SHA = "48c426a73af8ef92" + "0" * 24

HARNESS = r"""
set -euo pipefail
source "$SERVICE_SCRIPT"

deploy_export_release() { printf '%s\t%s\n' "$SHA" "$BOARD_ROOT/releases/$SHA"; }
current_release_dir() { printf '%s\n' "$BOARD_ROOT/releases/previous"; }
resolved_service_scope() { printf 'system\n'; }
assert_user_manager_identity() { :; }
assert_system_unit_reload_not_required_for_release() { :; }
stop_listener_for_upgrade() { :; }
start_listener_after_upgrade() {
    # The listener failing to come back after a rollback that passed: exit 1, but the board is back (SYRD-775).
    if [[ "${LISTENER_START:-pass}" == fail ]]; then die "could not restart the listener after deploy"; fi
}
apply_database_migrations_for_release() { :; }
run_release_canary() { log "canary-pass for /opt/switchyard/releases/$SHA"; }
activate_release() { :; }
verify_current_release_sha() { :; }
RESTARTS=0
restart_live_service() {
    RESTARTS=$((RESTARTS + 1))
    # SYRD-775: the rollback's own restart -- the second -- failing when asked to, as systemctl does, with no ERROR line.
    if (( RESTARTS == 2 )) && [[ "${ROLLBACK_RESTART:-pass}" == fail ]]; then
        printf 'Job for syrd-ticket-board.service failed because the control process exited with error code.\n' >&2
        return 1
    fi
}
# The chosen check fails, saying a long message whose end is its own; the rollback's own smoke check passes.
say_and_fail() {
    local name="$1"
    if [[ "$FAILING" == "$name" ]]; then
        printf '[ticket-board-service] ERROR: %s %s\n' "$(printf 'detail-%.0s' $(seq 1 300))" "END-OF-$name" >&2
        return 1
    fi
    return 0
}
SMOKES=0
smoke_check_http() {
    SMOKES=$((SMOKES + 1))
    if (( SMOKES == 1 )); then say_and_fail http-smoke; return $?; fi
    # SYRD-775: the rollback's own smoke check, failing when asked to.
    if [[ "${ROLLBACK_SMOKE:-pass}" == fail ]]; then
        printf '[ticket-board-service] ERROR: smoke check: HTTP 502 from the restored board \033]0;owned\007\n' >&2
        return 1
    fi
    return 0
}
verify_live_build_id() { say_and_fail live-build-id; }
verify_live_commit_repositories() { say_and_fail commit-repositories; }
verify_listener_pane_state_authority() { say_and_fail listener-pane-state-authority; }
verify_post_deploy_system_runtime() { say_and_fail system-runtime; }
[[ "${NO_PREVIOUS:-}" == 1 ]] || mkdir -p "$BOARD_ROOT/releases/previous"

deploy_restart_service
"""


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def deploy(failing: str, tmp: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {**{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")}, **extra,
           "SERVICE_SCRIPT": str(SERVICE_SCRIPT), "SHA": SHA, "FAILING": failing, "BOARD_ROOT": str(tmp / "live"),
           "TICKET_BOARD_PROJECT": "syrd", "TICKET_BOARD_COMMIT_GIT_DIR": str(tmp / "source.git"),
           "TICKET_BOARD_OWNER_HOME": str(tmp)}
    return subprocess.run(["bash", "-c", HARNESS], env=env, capture_output=True, text=True, timeout=60)


def journalled(result: subprocess.CompletedProcess[str], tmp: Path) -> tuple[str, str, dict]:
    """The deploy's failure as the privileged helper records it: printed lines into a job's attempt."""
    attempt = rollout_journal.Attempt("syrd", ["switchyard", "privileged-action", "syrd", "deploy-release"],
                                      root=tmp / "journal", operator="test", group=None)
    attempt.describe_job({"action": "deploy-release", "values": {"commit": SHA}})
    attempt.open()
    problem = deploy_failure_report.deploy_failure("syrd", SHA, result,
                                                   print_func=lambda line: attempt.write("stdout", line + "\n"))
    attempt.write("stdout", f"switchyard: {problem}\n")
    record = attempt.close(status="rolled_back", exit_status=3)
    return problem, (attempt.directory / "stdout.log").read_text(encoding="utf-8"), record


def test_each_post_restart_check_is_named_and_kept_whole() -> None:
    for name in deploy_failure_report.POST_RESTART_CHECKS:
        with tempfile.TemporaryDirectory(prefix="syrd773.") as tmp:
            result = deploy(name, Path(tmp))
            check(result.returncode == 3, f"{name}: the deploy rolls back with exit 3: {result.returncode} "
                                          f"{result.stderr[-600:]}")
            check(f"[ticket-board-service] post-restart-check-failed: {name}\n" in result.stderr
                  and f"rollback-pass; restored {tmp}/live/releases/previous after {name} failed" in result.stderr,
                  f"{name}: deploy-restart names the check, and the rollback's result names it too: {result.stderr[-600:]}")
            problem, log, record = journalled(result, Path(tmp))
            check(problem == f"rolled back: deploying {SHA} failed (exit 3): the post-restart check {name} failed; "
                             "its full output is in root's rollout journal (switchyard rollout-log syrd --output)",
                  f"{name}: the deploy's problem names the check, and where the rest is: {problem!r}")
            message = next((line for line in result.stderr.splitlines() if f"END-OF-{name}" in line), "")
            check(len(message) > 2000 and message in log,
                  f"{name}: the journal holds the check's whole message, uncut ({len(message)} characters)")
            check(f"post-restart-check-failed: {name}" in log and f"after {name} failed" in log
                  and f"canary-pass for /opt/switchyard/releases/{SHA}" in log and f"post-restart check {name} failed" in log,
                  f"{name}: and the canary, the failure and the rollback's result, in order: {log[-800:]}")
            check(log.index(f"END-OF-{name}") < log.index(f"post-restart-check-failed: {name}")
                  < log.index(f"after {name} failed"), f"{name}: in the order they happened")
            captured = (record.get("capture") or {}).get("stdout") or {}
            check(captured.get("truncated") is False and "output truncated" not in log,
                  f"{name}: nothing of it was cut: {captured}")


def test_a_rollback_that_failed_says_so() -> None:
    """SYRD-775: the rollback's own smoke check failing, or nothing to roll back to, is said plainly."""
    cases = [({"ROLLBACK_SMOKE": "fail"}, "the rollback did not pass its smoke check",
              "rollback after {check} failed: {tmp}/live/releases/previous did not pass its smoke check"),
             ({"NO_PREVIOUS": "1"}, "there was no previous release to roll back to",
              "post-restart check {check} failed and no previous release is available for rollback")]
    for extra, how, error in cases:
        for name in ("http-smoke", "listener-pane-state-authority"):
            with tempfile.TemporaryDirectory(prefix="syrd775.") as tmp:
                result = deploy(name, Path(tmp), **extra)
                check(result.returncode == 1 and f"post-restart-rollback-failed: {name}\n" in result.stderr
                      and "rollback-pass" not in result.stderr,
                      f"{name}, {how}: deploy-restart exits 1, naming the failed rollback: {result.returncode} "
                      f"{result.stderr[-500:]}")
                problem, log, _record = journalled(result, Path(tmp))
                said = error.format(check=name, tmp=tmp)
                check(problem == f"rollback failed: deploying {SHA} failed (exit 1): the post-restart check {name} failed "
                                 f"and {how}: the board may not be answering ({said}); its full output is in root's "
                                 "rollout journal (switchyard rollout-log syrd --output)",
                      f"{name}, {how}: the problem says the rollback failed, with deploy-restart's last error: {problem!r}")
                check("\x1b" not in problem and "\x07" not in problem and f"post-restart-rollback-failed: {name}" in log,
                      f"{name}, {how}: plain text, and the journal has it all")


def test_a_rollback_whose_restart_failed_says_so() -> None:
    """Audit of 17417a04: the rollback's restart failing ends deploy-restart under `set -e`, with neither the
    rollback-failed line nor rollback-pass. It is a rollback that failed, and the failed check's own error line is
    never passed off as the reason."""
    for name in ("live-build-id", "listener-pane-state-authority"):
        with tempfile.TemporaryDirectory(prefix="syrd775-restart.") as tmp:
            result = deploy(name, Path(tmp), ROLLBACK_RESTART="fail")
            check(result.returncode == 1 and "rollback-pass" not in result.stderr
                  and "post-restart-rollback-failed" not in result.stderr,
                  f"{name}: deploy-restart stops at the rollback's restart, saying neither: {result.returncode} "
                  f"{result.stderr[-400:]}")
            problem, _log, _record = journalled(result, Path(tmp))
            check(problem == f"rollback failed: deploying {SHA} failed (exit 1): the post-restart check {name} failed and "
                             "the rollback stopped before its smoke check: the board may not be answering (Job for "
                             "syrd-ticket-board.service failed because the control process exited with error code.); its "
                             "full output is in root's rollout journal (switchyard rollout-log syrd --output)",
                  f"{name}: the problem says the rollback failed, quoting the rollback's last line, not the check's: "
                  f"{problem!r}")
            check(f"END-OF-{name}" not in problem, f"{name}: the failed check's own error is not passed off as the reason")


def test_a_rollback_that_passed_is_not_called_failed() -> None:
    """A rollback that passed (rollback-pass) and a listener that then would not start exits 1 too: the board is back,
    so the problem is an ordinary failed check, not a failed rollback."""
    with tempfile.TemporaryDirectory(prefix="syrd775-listener.") as tmp:
        result = deploy("http-smoke", Path(tmp), LISTENER_START="fail")
        check(result.returncode == 1 and "rollback-pass" in result.stderr,
              f"the rollback passed, then the listener did not start: {result.returncode} {result.stderr[-300:]}")
        problem, _log, _record = journalled(result, Path(tmp))
        check(problem == f"deploying {SHA} failed (exit 1): the post-restart check http-smoke failed; its full output is "
                         "in root's rollout journal (switchyard rollout-log syrd --output)",
              f"it is not said to be a failed rollback: {problem!r}")


def test_the_last_error_line_is_quoted_as_plain_text() -> None:
    stderr = ("[ticket-board-service] post-restart-check-failed: live-build-id\n"
              "[ticket-board-service] post-restart-rollback-failed: live-build-id\n"
              "[ticket-board-service] ERROR: the board said \x1b]0;owned\x07 nothing\n")
    problem = deploy_failure_report.deploy_failure("syrd", SHA, SimpleNamespace(returncode=1, stderr=stderr, stdout=""),
                                                   print_func=lambda _line: None)
    check(problem.startswith("rollback failed: ") and "(the board said ]0;owned nothing)" in problem
          and "\x1b" not in problem and "\x07" not in problem,
          f"deploy-restart's last error line is quoted as plain text: {problem!r}")


def test_the_deploy_summary_says_the_board_may_not_be_answering() -> None:
    """SYRD-775: `deploy-release`'s summary, through the real tenant_release_deploy, for a deploy whose rollback failed."""
    sys.path[:0] = [str(ROOT / "tests")]
    import catalogued_deploy_release_test as host_test

    host_test.install_test_dispatch()
    original = host_test.Host.__call__
    # One run of every deploy-release scenario (about 150 s), with Audit's case: the rollback's restart failing under
    # `set -e`. The summary reads only the problem's `rollback failed:` opening, which every failed rollback's has.
    for extra, how in (({"ROLLBACK_RESTART": "fail"}, "the rollback stopped before its smoke check"),):
        with tempfile.TemporaryDirectory(prefix="syrd775-real.") as tmp:
            real = deploy("live-build-id", Path(tmp), **extra)
        check(real.returncode == 1, f"{how}: the real deploy-restart's failed rollback: {real.stderr[-300:]}")

        def with_real_output(self, argv, real=real, **kwargs):
            result = original(self, argv, **kwargs)
            if "deploy-restart" in " ".join(str(part) for part in argv) and self.outcome["deploy"] == "fail":
                result.returncode, result.stderr = 1, real.stderr  # what deploy-restart says when its rollback failed
            return result

        host_test.Host.__call__ = with_real_output
        try:
            seen = host_test.scenarios(ROOT)
        finally:
            host_test.Host.__call__ = original
        summary_checks(host_test, seen, how)


def summary_checks(host_test, seen: dict, how: str) -> None:
    failed = seen["deploy fails, board unchanged"]
    said = failed["said"]
    check("switchyard: rollback failed: deploying" in said and f"{how}: the board may not be answering" in said,
          f"{how}: the deploy's problem says the rollback failed: {said[-900:]}")
    check(f"the board's release link names {host_test.OLD} again, but the board did not pass its smoke check on it, so "
          "it may not be answering" in said and f"the board still names {host_test.OLD}" not in said,
          f"and its summary does not read as the board being back to normal: {said[-900:]}")
    check(failed["code"] == 1 and failed["release"] == "blocked",
          f"it is a failure, not a rollback: exit {failed['code']}, phase {failed['release']}")
    plain = seen["deploy fails, board moved"]["said"]
    check("may not be answering" not in plain, "a deploy whose output says nothing of a failed rollback is not said so")


def test_the_five_are_the_checks_deploy_restart_runs() -> None:
    script = SERVICE_SCRIPT.read_text(encoding="utf-8")
    body = script[script.index("deploy_restart_service() {"):script.index("\nrestart_live_service() {")]
    named = [line.split()[-1] for line in body.splitlines() if "rollback_live_service" in line]
    check(named == list(deploy_failure_report.POST_RESTART_CHECKS),
          f"every rollback in deploy-restart names its check, the five this module knows, in order: {named}")


def test_unnamed_output_is_quoted_from_its_end() -> None:
    said: list[str] = []
    text = "start-of-output " + "x" * 900 + " the-real-failure-at-the-end"
    problem = deploy_failure_report.deploy_failure(
        "syrd", SHA, SimpleNamespace(returncode=1, stderr=text + "\n", stdout=""), print_func=said.append)
    check(problem.startswith(f"deploying {SHA} failed (exit 1): ...") and "the-real-failure-at-the-end" in problem
          and "start-of-output" not in problem and "switchyard rollout-log syrd --output" in problem,
          f"a failure that names no check is quoted from its end, with where the rest is: {problem!r}")
    check(said[1:] == [f"switchyard:   [stderr] {text}"], f"and all of it is printed: {said}")
    escaped = deploy_failure_report.deploy_failure(
        "syrd", SHA, SimpleNamespace(returncode=3, stderr="board said \x1b]0;owned\x07 build\n", stdout=""),
        print_func=said.append)
    check("\x1b" not in "".join(said) + escaped and "\x07" not in "".join(said) + escaped,
          f"what the board answered is printed as plain text: {said[-1]!r} {escaped!r}")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"deploy_failed_check_report_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
