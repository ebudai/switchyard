#!/bin/bash
# probe.sh <outdir> <cmdfile>: Claude Code offline, as a pane's root, runs one Bash tool call: the command in
# <cmdfile> (SYRD-801). The private tmux server and its socket directory go on every exit.
set -u
O=$1; CF=$2; D=$(cd "$(dirname "$0")" && pwd)
rm -rf "$O"; mkdir -p "$O/home/.claude" "$O/work"
K=$(mktemp -d /tmp/s801.XXXX)
# The stand-in API runs in its own namespaces and outlives inner.sh: the end of inner.sh stops it, and so does this
# trap when inner.sh fails before its end (Audit of e282897f); the tmux server is in a session of its own.
cleanup() {
    tmux -S "$K/t" kill-server 2>/dev/null
    [ -s "$O/api.pid" ] && kill "$(cat "$O/api.pid")" 2>/dev/null
    rm -rf "$K"
}
trap cleanup EXIT
H=$O/home; W=$O/work
cat > "$H/.claude/.credentials.json" <<JSON
{"claudeAiOauth": {"accessToken": "sk-ant-oat01-fake", "refreshToken": "sk-ant-ort01-fake", "expiresAt": $(( ($(date +%s) + 30*86400) * 1000 )), "scopes": ["user:inference", "user:profile", "user:sessions:claude_code"], "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}}
JSON
cat > "$H/.claude.json" <<JSON
{"hasCompletedOnboarding": true, "lastOnboardingVersion": "2.1.318", "numStartups": 5, "bypassPermissionsModeAccepted": true,
 "oauthAccount": {"accountUuid": "00000000-0000-4000-8000-000000000001", "emailAddress": "probe@example.invalid", "organizationUuid": "00000000-0000-4000-8000-000000000002", "organizationRole": "admin", "displayName": "probe", "billingType": "stripe_subscription"},
 "projects": {"$W": {"hasTrustDialogAccepted": true, "hasCompletedProjectOnboarding": true}}}
JSON
echo '{"skipDangerousModePermissionPrompt": true}' > "$H/.claude/settings.json"
cat > "$O/inner.sh" <<INNER
#!/bin/bash
ip link set lo up
PROBE_CMD_FILE=$CF python3 $D/fake_api.py 18801 $O/api.jsonl 0 > $O/api.err 2>&1 < /dev/null &
API=\$!
echo \$API > $O/api.pid
sleep 0.5
export HOME=$H TERM=xterm-256color LANG=C.UTF-8 PATH=/usr/bin:/bin
T="tmux -S $K/t"
\$T new-session -d -s p -x 160 -y 45 -c $W "exec env IS_SANDBOX=1 ANTHROPIC_BASE_URL=http://127.0.0.1:18801 DISABLE_UPDATES=1 DISABLE_INSTALLATION_CHECKS=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 /opt/claude-code/bin/claude --model claude-opus-5-5 --dangerously-skip-permissions"
for i in \$(seq 1 30); do sleep 1; \$T capture-pane -p -t p > $O/screen_boot.txt; grep -q '❯' $O/screen_boot.txt && break; done
sleep 2
\$T send-keys -t p -l 'run it'; sleep 0.3; \$T send-keys -t p Enter
for i in \$(seq 1 30); do sleep 1; [ -s $O/look.json ] && break; done
sleep 1
\$T capture-pane -p -t p > $O/screen_end.txt
kill \$API
INNER
timeout 90 unshare -rn bash "$O/inner.sh"
