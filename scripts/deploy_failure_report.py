"""What a failed tenant board deploy says, and where it says it (SYRD-773).

`deploy_release_in_transaction` runs the tenant release's `deploy-restart`. When that fails -- exit 3 is a deploy its
own rollback undid -- the problem it returned quoted the first 400 characters of the deploy's stderr and nothing more.
Which check after the restart failed comes last in that output, so it was lost, even in root's rollout journal, which
records what the privileged action printed and never saw the deploy's own streams.

`deploy_failure` prints every line the deploy wrote, which the journal keeps whole, and returns a one-line problem
naming the check that failed, read from deploy-restart's own `post-restart-check-failed: NAME` line. Without one it
quotes the end of the output, where the failure is, and says where the rest is.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Callable

#: The checks deploy-restart runs after restarting the board, each rolling back with exit 3 when it fails.
POST_RESTART_CHECKS = ("http-smoke", "live-build-id", "commit-repositories", "listener-pane-state-authority",
                       "system-runtime")
#: How long one printed line of the deploy's output may be; the rollout journal's own line limit is 64K.
LINE_CHARS = 16000

#: The line deploy-restart writes before rolling back, naming the post-restart check that failed (SYRD-773).
FAILED_CHECK = re.compile(r"^\[ticket-board-service\] post-restart-check-failed: ([a-z][a-z0-9-]{0,63})$", re.M)
#: How much of the deploy's own output the one-line problem quotes: its end, where the failure is.
PROBLEM_TAIL_CHARS = 400
#: The line deploy-restart writes when the rollback after a failed check could not run, or did not pass its own smoke
#: check: the board may not be answering at all (SYRD-775).
ROLLBACK_FAILED_LINE = re.compile(r"^\[ticket-board-service\] post-restart-rollback-failed: ([a-z][a-z0-9-]{0,63})$",
                                  re.M)
#: The problem's opening when that happened, as ROLLED_BACK ("rolled back: ") opens one whose rollback passed.
ROLLBACK_FAILED = "rollback failed: "
#: deploy-restart's own error lines, the last of which says why the rollback failed.
ERROR_LINE = re.compile(r"^\[ticket-board-service\] ERROR: (.*)$", re.M)
#: What deploy-restart logs when its rollback finished; without it, after a failed check, the rollback did not.
ROLLBACK_PASSED = "[ticket-board-service] rollback-pass; "
#: deploy-restart's own fixed lines, which say what happened but never why.
MARKER_LINE = re.compile(r"^\[ticket-board-service\] (post-restart-check-failed|post-restart-rollback-failed): ")


def deploy_failure(project: str, target_sha: str, result: subprocess.CompletedProcess[Any], *,
                   print_func: Callable[[str], None] = print) -> str:
    """A failed deploy: its whole output printed, which root's rollout journal records, and the one-line problem.

    SYRD-113: deploy-restart's 3 is a deploy its rollback undid (tenant_release_deploy.ROLLED_BACK_EXIT), and the
    problem says so. SYRD-773: it used to quote the first 400 characters of the deploy's stderr and nothing else, so
    which check after the restart failed -- its output comes last -- was lost even in the journal. Now every line the
    deploy wrote is printed first, so the journal keeps it whole; the problem names the check that failed, from
    deploy-restart's own line, and otherwise quotes the end of the output, not its start.
    """
    from scripts.ticket_board.phone_text import clean_text

    stderr, stdout = str(result.stderr or ""), str(result.stdout or "")
    print_func(f"switchyard: deploying {target_sha} failed (exit {result.returncode}); its output, in full:")
    for name, text in (("stderr", stderr), ("stdout", stdout)):
        for line in text.splitlines():
            # Plain text, one line each: the deploy repeats what the board answered (its build id, its smoke page).
            print_func(f"switchyard:   [{name}] {clean_text(line, LINE_CHARS)}")
    failed = FAILED_CHECK.findall(stderr)
    where = f"its full output is in root's rollout journal (switchyard rollout-log {project} --output)"
    unfinished = bool(failed) and ROLLBACK_PASSED not in stderr and result.returncode != 3
    if ROLLBACK_FAILED_LINE.search(stderr) or unfinished:
        # SYRD-775: the one outcome where the board may not be answering at all, said plainly. A rollback that did
        # not finish -- a check named, no rollback-pass, not the rolled-back exit -- failed, whatever stopped it: its
        # own smoke check, nothing to roll back to, or a restart that failed under `set -e` (Audit of 17417a04).
        check = (ROLLBACK_FAILED_LINE.findall(stderr) or failed)[-1]
        # Only what the rollback itself said, never the failed check's own (earlier) error line.
        after = stderr[stderr.rfind(f"post-restart-check-failed: {check}"):].splitlines()[1:]
        said = [line for line in after if line.strip() and not MARKER_LINE.match(line)]
        errors = [m.group(1) for m in map(ERROR_LINE.match, said) if m]
        if errors:
            last = clean_text(errors[-1], PROBLEM_TAIL_CHARS)
            how = ("there was no previous release to roll back to" if "no previous release" in last
                   else "the rollback did not pass its smoke check" if "did not pass its smoke check" in last
                   else "the rollback did not finish")
        else:
            last = clean_text(said[-1] if said else "no output", PROBLEM_TAIL_CHARS)
            how = "the rollback stopped before its smoke check"
        return (f"{ROLLBACK_FAILED}deploying {target_sha} failed (exit {result.returncode}): the post-restart check "
                f"{check} failed and {how}: the board may not be answering ({last}); {where}")
    if failed:
        said = f"the post-restart check {failed[-1]} failed; {where}"
    else:
        text = clean_text(stderr.strip() or stdout.strip() or "no output", len(stderr) + len(stdout) + 16)
        tail = text if len(text) <= PROBLEM_TAIL_CHARS else "..." + text[-PROBLEM_TAIL_CHARS:]
        said = f"{tail} ({where})" if text != "no output" else text
    return (("rolled back: " if result.returncode == 3 else "")
            + f"deploying {target_sha} failed (exit {result.returncode}): {said}")
