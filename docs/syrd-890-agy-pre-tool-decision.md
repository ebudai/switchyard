# SYRD-890: agy 1.5.17 denies every tool call whose PreToolUse answer has no decision

## What happened

On ocstab (2026-11-27, release f05a1b37), a Researcher on agy 1.5.17 with `gemini-4-hydrogen-high` had every
`read_file` and `run_command` call refused, from its first turn and again after a reboot:

    Encountered error in tool execution: tool call denied by pre-tool hook:

The reason was empty. The role could not read its delivery or its skill, and about two hours went by with no work.
The Director moved it to Claude.

## Cause, measured offline

agy 1.5.17 requires a `decision` on every PreToolUse answer, and treats an answer without one as a deny with no reason.
Switchyard's shell guard (`scripts/ticket-board-shell-guard`, SYRD-753) answered an allowed agy call with `{}`. agy
1.5.1, where SYRD-753 measured the contract, accepted that.

- **agy's own contract**, embedded in the 1.5.17 binary (its hooks documentation): `decision` is a required string,
  one of `allow`, `deny`, `ask` or `force_ask`. Its changelog has an older line about "safely handling empty decision
  strings" instead of failing on an unknown decision. Neither says what an empty decision now does, so it was
  measured.
- **The reproduction** is `tests/fixtures/syrd890_probe`, built on SYRD-756's rig. It runs the host's real
  `/usr/bin/agy` 1.5.17 in a private user and network namespace (loopback only), with a throwaway HOME, a fake account
  and a throwaway CA. The real hook installer and this tree's guard are installed, behind a wrapper that logs each
  call. A stand-in model asks for one `run_command` (`echo PROBE-TOOL-RAN`), and agy's next model request carries the
  tool result. Results:
  - the guard as shipped answers `{}`, and agy returns "tool call denied by pre-tool hook: " with an empty reason,
    exactly ocstab's symptom;
  - the same guard with that answer changed to `{"decision": "allow"}`: the command runs and prints `PROBE-TOOL-RAN`.
- **Why no earlier probe saw a hook run.** agy runs hooks only when the server's experiments turn them on. The stand-in
  must answer `listExperiments` with `{"flags": [{"name": "enable-customization-kind-hooks", "boolValue": true},
  {"name": "enable-hook-status", "boolValue": true}]}`. Answered `{}`, agy loads `hooks.json` ("loaded 1 named
  hooks") and runs none of them. That is why SYRD-756 recorded that agy "ran no hook offline", and why only an
  account whose experiments are on sees the denial.
- **Not the hook's environment.** The guard ran: it was called once per tool call, with agy's documented payload, and
  printed `{}` with exit 0. Its logic was right. Only its answer's shape was wrong for this agy.

## The fix

The guard answers an allowed agy call with `{"decision": "allow"}` in place of `{}`. A refusal is unchanged:
`{"decision": "deny", "reason": ...}`. A payload the guard cannot read is still allowed, now explicitly. The agy roles
already run with `--dangerously-skip-permissions`, so `allow` asks agy to do what it was doing. agy 1.5.7 already
keeps an admin-enforced review in force over a hook's approval.

The guard stays at 1250 lines; the change is in place. The other agy hooks (SessionStart, PreInvocation,
PostInvocation, Stop) are not tool decisions, and agy's contract takes `{}` from them. They are unchanged.

## So it is caught next time

- **Release smoke** (`scripts/release_smoke.py`, SYRD-854) runs the release's own guard as agy runs it, right after
  the import check: an ordinary `run_command`, a `read_file`, and board prose the shell would expand. Each answer
  must carry the decision agy reads: `allow`, or `deny` with a reason. Otherwise the release is NOT READY, before
  any board is built. f05a1b37's guard is NOT READY there.
- **The canary**, `tests/agy_pre_tool_hook_canary_test.py`, runs the installed agy through the reproduction above.
  - It checks that an ordinary command runs.
  - It checks that refused board prose is denied with the guard's reason handed to the model.
  - It fails if agy ran no hook, since then nothing was proved.
  - agy updates as a package, outside Switchyard's deploys, so this is the check to run on the host after an agy
    update. Where there is no agy or no user and network namespace (inside the sweep sandbox, for one), it says SKIP
    and passes.
  - With the shipped guard it fails with ocstab's symptom: "agy 1.5.17 ran the ordinary command the guard passed:
    ... tool call denied by pre-tool hook: ".

## Other agy panes on this host

- Every tenant's agy roles run the same `/usr/bin/agy` (1.5.17, installed 2026-11-26 06:21), and the same guard
  answer from their release's staged guard.
- Running agy panes on 2026-11-27 (read-only `ps`):
  - ocstab's Researcher session `92eae1b3` (ocstab-agent), the one kept for a return;
  - syrd's inspector (switchyard-agent, `syrd-inspector:0.0`). Its `~/.gemini/config/hooks.json` runs f05a1b37's
    guard. It has had no input since its 09:29 start, so its logs record no tool call and no denial.
- Each will be denied the same way on its first tool call while its account's experiments turn hooks on, as
  ocstab's did.
- The other tenants' launcher configs are not readable from this account, so their agy roles could not be listed.
- After the fix is installed and deployed, `switchyard upgrade` re-runs the hook installer for each tenant
  (`refresh_role_pane_hooks`). That repoints `hooks.json` at the new release's guard. A running agy pane loaded its
  hooks at start, so it takes the fix when it restarts.

## Pinned by

- `tests/shell_guard_test.py`: an allowed agy answer is exactly `{"decision": "allow"}`, never `{}` or silence.
- `tests/release_smoke_test.py`: HEAD's smoke says it checked agy's guard answers. HEAD with f05a1b37's `{}` answer
  is NOT READY, naming the call and the missing decision, before any board is built.
- `tests/agy_pre_tool_hook_canary_test.py`, on a host with agy: the real agy, as above.
