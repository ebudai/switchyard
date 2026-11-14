# SYRD-773: a rolled-back deploy names the check that failed, and the journal keeps all of it

## What happened
On 2026-11-13 a syrd `deploy-release` rolled back with exit 3. Its only account of why was the first 400 characters of
the deploy's stderr: `rolled back: deploying 48c426a7... failed (exit 3): [ticket-board-service] skipping ...
[ticket-board-service] canary-pass for /opt/switchyard/releases/48c426a73af8ef92`. It stopped mid-path.

Root's rollout journal had the same cut text. It records what the privileged action prints, and
`deploy_release_in_transaction` piped the deploy's own streams and printed only that problem. Five checks after the
restart can each roll the deploy back. Which one failed, and what it said, was lost, and the tenant account cannot read
the board's systemd journal.

## Now
`deploy-restart` (`ticket-board-service.sh`) names the check that failed. Each of the five calls `rollback_live_service`
with its name. The checks are:
- `http-smoke`;
- `live-build-id`;
- `commit-repositories`;
- `listener-pane-state-authority`;
- `system-runtime`.

Before rolling back, it writes one fixed line, `[ticket-board-service] post-restart-check-failed: <name>`. The check's
own output is just above that line. The rollback's messages then name the check:
- `post-restart check <name> failed; rolling back current to <previous>`;
- then `rollback-pass; restored <previous> after <name> failed`. If the rollback's own smoke check fails, the deploy
  dies saying so.

`deploy_release_in_transaction` hands a failed deploy to `scripts/deploy_failure_report.deploy_failure`, which does two
things:
1. **It prints every line the deploy wrote**, stderr then stdout, as `switchyard:   [stderr] ...`. Root's rollout
   journal records what the privileged action prints, so it now keeps the deploy's whole account, within the journal's
   own 4 MB stream and 64 KB line limits. Each line is shown as plain text (`phone_text.clean_text`), because the deploy
   repeats what the board answered.
2. **It returns a one-line problem that names the check:** `rolled back: deploying <sha> failed (exit 3): the
   post-restart check <name> failed; its full output is in root's rollout journal (switchyard rollout-log <project>
   --output)`. A failure that names no check, such as an earlier step, is quoted from its **end**, where the failure
   is, with the same pointer.

## Tests
`tests/deploy_failed_check_report_test.py` covers each of the five checks failing in turn:
- **The real script:** the real `deploy_restart_service` and `rollback_live_service` run, sourced, with the earlier
  steps stubbed.
- **What it writes:** it exits 3 and names the check, as does the rollback's result.
- **The journal:** the output goes into a real rollout-journal job attempt. The problem names the check, and the
  journal holds the check's whole message (over 2,000 characters), uncut and in order, with nothing truncated.
- **The list of checks:** every rollback in `deploy_restart_service` names one of the five, in order.
- **No check named:** the output is quoted from its end.
- **Plain text:** a board's escape characters are printed as plain text.
