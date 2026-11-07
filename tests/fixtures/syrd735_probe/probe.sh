#!/bin/bash
# probe.sh <outdir> <claim> [reset-in-seconds] [key]: Claude Code 2.1.318 against the fake API, offline, in a private tmux; records hooks and screens.
set -u
O=$1; CLAIM=${2:-seven_day}
mkdir -p "$O/home/.claude" "$O/work"
K=$(mktemp -d /tmp/s735k.XXXX)  # short: tmux caps a socket's path
H=$O/home; W=$O/work
RESET=$(( $(date +%s) + ${3:-86400} ))
cat > "$H/.claude/.credentials.json" <<JSON
{"claudeAiOauth": {"accessToken": "sk-ant-oat01-fake", "refreshToken": "sk-ant-ort01-fake", "expiresAt": $(( ($(date +%s) + 30*86400) * 1000 )), "scopes": ["user:inference", "user:profile", "user:sessions:claude_code"], "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}}
JSON
cat > "$H/.claude.json" <<JSON
{"hasCompletedOnboarding": true, "lastOnboardingVersion": "2.1.318", "numStartups": 5, "bypassPermissionsModeAccepted": true,
 "oauthAccount": {"accountUuid": "00000000-0000-4000-8000-000000000001", "emailAddress": "probe@example.invalid", "organizationUuid": "00000000-0000-4000-8000-000000000002", "organizationRole": "admin", "displayName": "probe", "billingType": "stripe_subscription"},
 "projects": {"$W": {"hasTrustDialogAccepted": true, "hasCompletedProjectOnboarding": true}}}
JSON
HOOK="python3 $(dirname "$0")/hookpoll.py $O/hooks.jsonl"
python3 - "$H/.claude/settings.json" "$HOOK" <<'PY'
import json, sys
events = ["SessionStart", "UserPromptSubmit", "Stop", "StopFailure", "Notification", "PreToolUse", "PostToolUse", "SubagentStop", "PreCompact", "SessionEnd"]
hook = {"hooks": [{"type": "command", "command": sys.argv[2]}]}
json.dump({"hooks": {e: [hook] for e in events}, "skipDangerousModePermissionPrompt": True}, open(sys.argv[1], "w"))
PY
cat > "$O/inner.sh" <<INNER
#!/bin/bash
ip link set lo up
python3 $(dirname "$0")/fake_api.py 18435 $RESET $CLAIM $O/api.jsonl > /dev/null 2>&1 < /dev/null &
API=\$!
sleep 0.5
export HOME=$H TERM=xterm-256color LANG=C.UTF-8 TZ=America/Toronto PATH=/usr/bin:/bin
T="tmux -S $K/t"
\$T new-session -d -s p -x 160 -y 45 -c $W "env IS_SANDBOX=1 ANTHROPIC_BASE_URL=http://127.0.0.1:18435 DISABLE_UPDATES=1 DISABLE_INSTALLATION_CHECKS=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 /opt/claude-code/bin/claude --dangerously-skip-permissions 2>$O/claude.err; sleep 600"
for i in \$(seq 1 30); do sleep 1; \$T capture-pane -p -t p > $O/screen_boot.txt; grep -q '❯\|>' $O/screen_boot.txt && break; done
sleep 3; \$T capture-pane -p -t p > $O/screen_ready.txt
\$T send-keys -t p -l 'say hi'; sleep 0.5; \$T send-keys -t p Enter
for i in 1 2 3 4 5 6 8 10 15 20; do sleep 1; \$T capture-pane -p -t p > $O/screen_after_\$i.txt; done
\$T send-keys -t p ${4:-Enter}; sleep 3; \$T capture-pane -p -e -t p > $O/screen_after_key_e.txt; \$T capture-pane -p -t p > $O/screen_after_enter.txt
sleep 3; \$T capture-pane -p -t p > $O/screen_after_enter2.txt
\$T kill-server
kill \$API
INNER
chmod +x "$O/inner.sh"
timeout 120 unshare -rn bash "$O/inner.sh"
