# PGU-640 Compaction Self-Heal Rollout

This rollout installs the merged PGU-557/PGU-640 pane hook. The hook binary path
is shared by Claude, Codex, and Gemini panes, so replacing it changes hook
behavior for all panes on their next hook event.

Run this only with Eric/director approval:

From Eric's account, this fish-safe command updates the checkout and runs the
operator script as `agent`:

```bash
sudo -iu agent bash -lc 'set -euo pipefail; cd /home/agent/Projects/pgu; git fetch origin main; git switch --detach origin/main; scripts/pgu-640-compaction-self-heal-rollout --apply'
```

From an `agent` shell, no sudo wrapper is needed:

```bash
bash -lc 'set -euo pipefail; cd /home/agent/Projects/pgu; git fetch origin main; git switch --detach origin/main; scripts/pgu-640-compaction-self-heal-rollout --apply'
```

Rollback if the hook binary breaks SessionStart handling before a code revert can
be deployed:

```bash
sudo -iu agent cp -p \
  /home/agent/.local/bin/pgu-ticket-board-pane-idle-hook.pre-640 \
  /home/agent/.local/bin/pgu-ticket-board-pane-idle-hook
```

Codex trust is deliberately manual. The install changes the exact hook command
strings, so Eric must re-trust the changed Codex hooks through Codex's own
`/hooks` flow after the script runs. Keep `--dangerously-bypass-hook-trust`
through this rollout; removing that flag is a separate change after trust is
verified.

Acceptance order after Eric runs the script and re-trusts Codex hooks:

1. Trigger SessionStart on a Claude pane and confirm the session starts normally.
2. Trigger SessionStart on a Codex pane and confirm the session starts normally.
3. Test consumption with an in-progress ticket assigned to the tested pane. Trigger
   a real compaction/resume, then ask: `What ticket are you working on? Reply
   exactly NO PRIOR CONTEXT if you do not know.`

Passing requires the pane to name the injected ticket without being given the
ticket id in the prompt.

A cold restart is not an acceptance proof. The hook emits additionalContext only
when SessionStart reports `compact` or `resume` and the pane's role currently has
an active `in_progress` ticket.

## Sandbox Runtime Proofs

Claude was proven by the director in a sandbox: a `SessionStart` hook emitting
`hookSpecificOutput.additionalContext` completed normally and the model used the
injected ticket id, while the no-hook control replied `NO PRIOR CONTEXT`.

Codex was proven in a disposable `CODEX_HOME` with copied auth/config and a
stub `SessionStart` hook. The invocation used `codex exec --ephemeral` with
`TMUX` and `TMUX_PANE` cleared, so it did not touch live pane config, live hook
state, or live sessions.

Results:

1. Tolerance: with the hook emitting `hookSpecificOutput.additionalContext`,
   Codex exited with rc=0 and replied `TOLERANCE_OK`; the hook marker confirmed
   `SessionStart` ran.
2. Consumption: with the hook injecting a hidden token, Codex replied
   `CODEX_HOOK_CONTEXT_OK`.
3. Control: with the same invocation shape and no hook, Codex replied
   `NO PRIOR CONTEXT`.

This proves Codex tolerates and consumes `additionalContext` in sandbox. It does
not replace the controlled live rollout above; Eric still owns the live hook
install and live acceptance check. No restart is performed by the rollout
script: the hook binary change applies to every pane immediately on its next
hook event, while Gemini and Codex pick up their new hook config on their next
natural restart.
