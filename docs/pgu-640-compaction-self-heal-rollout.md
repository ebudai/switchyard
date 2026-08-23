# PGU-640 Compaction Self-Heal Rollout

This rollout installs the merged PGU-557/PGU-640 pane hook and restarts only
the Gemini-backed inspector pane. The hook binary path is shared by Claude,
Codex, and Gemini panes, so replacing it changes hook behavior for all panes on
their next hook event.

Run this only with Eric/director approval:

```bash
sudo -iu agent bash <<'EOF'
set -euo pipefail
cd /home/agent/Projects/pgu
git fetch origin main
git switch main
git pull --ff-only origin main

# Capture the pre-rollout hook binary once. -n is intentional: rerunning this
# block must not overwrite the known-good rollback point with the new binary.
cp -pn /home/agent/.local/bin/pgu-ticket-board-pane-idle-hook \
  /home/agent/.local/bin/pgu-ticket-board-pane-idle-hook.pre-640

# Keep the legacy hook path because all live configs currently reference it.
scripts/ticket-board-install-pane-hooks install \
  --home /home/agent \
  --bin-path /home/agent/.local/bin/pgu-ticket-board-pane-idle-hook

# Sanity-check that Gemini SessionStart no longer suppresses hook stdout.
rg -n 'gemini.SessionStart|>/dev/null|printf' /home/agent/.gemini/config/hooks.json

# Restart only the Gemini-backed inspector pane. If this refuses because the live
# command does not match the configured CLI, stop and inspect before using --force.
scripts/team-launcher pgu pane reload inspector --config config/team-launcher/pgu.json
EOF
```

Rollback if the hook binary breaks SessionStart handling before a code revert can
be deployed:

```bash
sudo -iu agent cp -p \
  /home/agent/.local/bin/pgu-ticket-board-pane-idle-hook.pre-640 \
  /home/agent/.local/bin/pgu-ticket-board-pane-idle-hook
```

Acceptance order after Eric runs the block:

1. Trigger SessionStart on a Claude pane and confirm the session starts normally.
2. Trigger SessionStart on a Codex pane and confirm the session starts normally.
3. Test consumption with an in-progress ticket assigned to the tested pane. Trigger
   a real compaction/resume, then ask: `What ticket are you working on? Reply
   exactly NO PRIOR CONTEXT if you do not know.`

Passing requires the pane to name the injected ticket without being given the
ticket id in the prompt.
