#!/bin/bash
# probe.sh <outdir> [tree]: Claude Code against the fake API (SYRD-788), offline in a private user+net namespace and tmux:
# a prompt, then /clear, then a ticket line; every request body and hook payload is logged. With a tree, its real
# ticket-board-pane-idle-hook answers SessionStart for a Router pane that was launched resuming a session.
set -u
O=$1; TREE=${2:-}; D=$(cd "$(dirname "$0")" && pwd)
rm -rf "$O"; mkdir -p "$O/home/.claude" "$O/work"
K=$(mktemp -d /tmp/s788k.XXXX)
H=$O/home; W=$O/work
cat > "$H/.claude/.credentials.json" <<JSON
{"claudeAiOauth": {"accessToken": "sk-ant-oat01-fake", "refreshToken": "sk-ant-ort01-fake", "expiresAt": $(( ($(date +%s) + 30*86400) * 1000 )), "scopes": ["user:inference", "user:profile", "user:sessions:claude_code"], "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}}
JSON
cat > "$H/.claude.json" <<JSON
{"hasCompletedOnboarding": true, "lastOnboardingVersion": "2.1.318", "numStartups": 5, "bypassPermissionsModeAccepted": true,
 "oauthAccount": {"accountUuid": "00000000-0000-4000-8000-000000000001", "emailAddress": "probe@example.invalid", "organizationUuid": "00000000-0000-4000-8000-000000000002", "organizationRole": "admin", "displayName": "probe", "billingType": "stripe_subscription"},
 "projects": {"$W": {"hasTrustDialogAccepted": true, "hasCompletedProjectOnboarding": true}}}
JSON
mkdir -p "$O/state" "$O/sessions"
if [ -n "$TREE" ]; then START="$TREE/scripts/ticket-board-pane-idle-hook idle --source claude.SessionStart --state-dir $O/state --session-dir $O/sessions --record-session"; else START="python3 $D/hook.py $O/hooks.jsonl"; fi
python3 - "$H/.claude/settings.json" "$START" "python3 $D/hook.py $O/hooks.jsonl" <<'PY'
import json, sys
start = {"hooks": [{"type": "command", "command": sys.argv[2]}]}
log = {"hooks": [{"type": "command", "command": sys.argv[3]}]}
json.dump({"hooks": {"SessionStart": [start], "UserPromptSubmit": [log], "Stop": [log]}, "skipDangerousModePermissionPrompt": True}, open(sys.argv[1], "w"))
PY
cat > "$O/inner.sh" <<INNER
#!/bin/bash
ip link set lo up
python3 $D/fake_api.py 18788 $O/api.jsonl > $O/api.err 2>&1 < /dev/null &
API=\$!
sleep 0.5
export HOME=$H TERM=xterm-256color LANG=C.UTF-8 PATH=/usr/bin:/bin
T="tmux -S $K/t"
\$T new-session -d -s p -x 160 -y 45 -c $W "env IS_SANDBOX=1 TICKET_BOARD_PANE_TARGET=p788-router:0.0 TICKET_BOARD_PROJECT=p788 TICKET_BOARD_PANE_SESSION_ID=0e5dba90-0000-4000-8000-000000000788 TICKET_BOARD_URL=http://127.0.0.1:9 TICKET_BOARD_ROLE_ONBOARDING_PROMPT=ROUTER-PROMPT-788:route,never-build ANTHROPIC_BASE_URL=http://127.0.0.1:18788 DISABLE_UPDATES=1 DISABLE_INSTALLATION_CHECKS=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 /opt/claude-code/bin/claude --model claude-haiku-5-5 --dangerously-skip-permissions 2>$O/claude.err; sleep 600"
for i in \$(seq 1 30); do sleep 1; \$T capture-pane -p -t p > $O/screen_boot.txt; grep -q '❯' $O/screen_boot.txt && break; done
sleep 2
send() { \$T send-keys -t p -l "\$1"; sleep 0.5; \$T send-keys -t p Enter; sleep \$2; \$T capture-pane -p -t p > $O/screen_\$3.txt; }
send 'first ticket: say hi' 6 first
send '/clear' 4 clear
send 'SYRD-785 -- Aborting switchyard new takes many Ctrl-C presses entered Implementation' 6 ticket
\$T kill-server
kill \$API
INNER
chmod +x "$O/inner.sh"
timeout 150 unshare -rn bash "$O/inner.sh"
echo "probe exit $?"
