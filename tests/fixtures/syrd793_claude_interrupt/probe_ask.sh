#!/bin/bash
# probe_ask.sh <outdir>: Claude Code offline; the stand-in API answers "ask me" with an AskUserQuestion tool call;
# captures the question screen, the cursor, and every hook (SYRD-793 round 2).
set -u
O=$1; D=$(cd "$(dirname "$0")" && pwd)
rm -rf "$O"; mkdir -p "$O/home/.claude" "$O/work"
K=$(mktemp -d /tmp/s793q.XXXX)
H=$O/home; W=$O/work
cat > "$H/.claude/.credentials.json" <<JSON
{"claudeAiOauth": {"accessToken": "sk-ant-oat01-fake", "refreshToken": "sk-ant-ort01-fake", "expiresAt": $(( ($(date +%s) + 30*86400) * 1000 )), "scopes": ["user:inference", "user:profile", "user:sessions:claude_code"], "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}}
JSON
cat > "$H/.claude.json" <<JSON
{"hasCompletedOnboarding": true, "lastOnboardingVersion": "2.1.318", "numStartups": 5, "bypassPermissionsModeAccepted": true,
 "oauthAccount": {"accountUuid": "00000000-0000-4000-8000-000000000001", "emailAddress": "probe@example.invalid", "organizationUuid": "00000000-0000-4000-8000-000000000002", "organizationRole": "admin", "displayName": "probe", "billingType": "stripe_subscription"},
 "projects": {"$W": {"hasTrustDialogAccepted": true, "hasCompletedProjectOnboarding": true}}}
JSON
python3 - "$H/.claude/settings.json" "python3 $D/hook.py $O/hooks.jsonl" <<'PY'
import json, sys
hook = {"hooks": [{"type": "command", "command": sys.argv[2]}]}
events = ("SessionStart", "UserPromptSubmit", "Stop", "StopFailure", "Notification", "PreToolUse", "PostToolUse")
json.dump({"hooks": {e: [hook] for e in events}, "skipDangerousModePermissionPrompt": True}, open(sys.argv[1], "w"))
PY
cat > "$O/inner.sh" <<INNER
#!/bin/bash
ip link set lo up
python3 $D/fake_api_ask.py 18794 $O/api.jsonl 0 > $O/api.err 2>&1 < /dev/null &
API=\$!
sleep 0.5
export HOME=$H TERM=xterm-256color LANG=C.UTF-8 PATH=/usr/bin:/bin
T="tmux -S $K/t"
\$T new-session -d -s p -x 160 -y 45 -c $W "env IS_SANDBOX=1 ANTHROPIC_BASE_URL=http://127.0.0.1:18794 DISABLE_UPDATES=1 DISABLE_INSTALLATION_CHECKS=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 /opt/claude-code/bin/claude --model claude-opus-5-5 --dangerously-skip-permissions 2>$O/claude.err; sleep 900"
for i in \$(seq 1 30); do sleep 1; \$T capture-pane -p -t p > $O/screen_boot.txt; grep -q '❯' $O/screen_boot.txt && break; done
sleep 2
\$T send-keys -t p -l 'ask me'; sleep 0.3; \$T send-keys -t p Enter; sleep 6
\$T capture-pane -p -J -t p > $O/screen_question.txt
\$T display-message -p -t p '#{cursor_x} #{cursor_y} #{pane_height}' > $O/cursor.txt
sleep 3
\$T capture-pane -p -J -t p > $O/screen_question2.txt
\$T kill-server
kill \$API
INNER
chmod +x "$O/inner.sh"
timeout 90 unshare -rn bash "$O/inner.sh"
echo "probe exit $?"
