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
start_listener_after_upgrade() { :; }
apply_database_migrations_for_release() { :; }
run_release_canary() { log "canary-pass for /opt/switchyard/releases/$SHA"; }
activate_release() { :; }
verify_current_release_sha() { :; }
restart_live_service() { :; }
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
    if (( SMOKES == 1 )); then say_and_fail http-smoke; else return 0; fi
}
verify_live_build_id() { say_and_fail live-build-id; }
verify_live_commit_repositories() { say_and_fail commit-repositories; }
verify_listener_pane_state_authority() { say_and_fail listener-pane-state-authority; }
verify_post_deploy_system_runtime() { say_and_fail system-runtime; }
mkdir -p "$BOARD_ROOT/releases/previous"

deploy_restart_service
"""


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def deploy(failing: str, tmp: Path) -> subprocess.CompletedProcess[str]:
    env = {**{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")},
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
