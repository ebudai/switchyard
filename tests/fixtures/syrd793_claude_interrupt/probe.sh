#!/bin/bash
# probe.sh <outdir> <wait-seconds>: Claude Code offline; one completed turn, then a prompt interrupted by Esc while its
# request is in flight; then wait, capturing the screen, logging every hook (SYRD-793).
set -u
O=$1; WAIT=${2:-100}; D=$(cd "$(dirname "$0")" && pwd)
rm -rf "$O"; mkdir -p "$O/home/.claude" "$O/work"
K=$(mktemp -d /tmp/s793k.XXXX)
# SYRD-800: on every exit -- done, failed or timed out -- the private tmux server and its socket directory go. A
# timeout kills inner.sh's process group (the stand-in API with it) before its own kill-server, and the tmux server,
# in a session of its own, outlives it.
cleanup() {
    tmux -S "$K/t" kill-server 2>/dev/null
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
python3 - "$H/.claude/settings.json" "python3 $D/hook.py $O/hooks.jsonl" <<'PY'
import json, sys
hook = {"hooks": [{"type": "command", "command": sys.argv[2]}]}
events = ("SessionStart", "UserPromptSubmit", "Stop", "StopFailure", "Notification", "PreToolUse", "PostToolUse", "SessionEnd")
cfg = {"hooks": {e: [hook] for e in events}, "skipDangerousModePermissionPrompt": True}
cfg["hooks"]["Notification"] = [{"matcher": "", **hook}, {"matcher": "idle_prompt", **hook}]
json.dump(cfg, open(sys.argv[1], "w"))
PY
cat > "$O/inner.sh" <<INNER
#!/bin/bash
ip link set lo up
python3 $D/fake_api.py 18793 $O/api.jsonl 0 > $O/api.err 2>&1 < /dev/null &
API=\$!
sleep 0.5
export HOME=$H TERM=xterm-256color LANG=C.UTF-8 PATH=/usr/bin:/bin
T="tmux -S $K/t"
\$T new-session -d -s p -x 160 -y 45 -c $W "env IS_SANDBOX=1 ANTHROPIC_BASE_URL=http://127.0.0.1:18793 DISABLE_UPDATES=1 DISABLE_INSTALLATION_CHECKS=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 /opt/claude-code/bin/claude --model claude-opus-5-5 --dangerously-skip-permissions 2>$O/claude.err; sleep 900"
for i in \$(seq 1 30); do sleep 1; \$T capture-pane -p -t p > $O/screen_boot.txt; grep -q '❯' $O/screen_boot.txt && break; done
sleep 2
echo "\$(date +%s.%N) send first" >> $O/marks.txt
\$T send-keys -t p -l 'first: say hi'; sleep 0.3; \$T send-keys -t p Enter; sleep 5
\$T capture-pane -p -t p > $O/screen_after_first.txt
kill \$API; sleep 0.3
python3 $D/fake_api.py 18793 $O/api.jsonl 60 > $O/api2.err 2>&1 < /dev/null &
API=\$!
sleep 0.5
echo "\$(date +%s.%N) send second" >> $O/marks.txt
\$T send-keys -t p -l 'an accidental paste meant for another pane'; sleep 0.3; \$T send-keys -t p Enter; sleep 1.5
\$T capture-pane -p -e -t p > $O/screen_working.txt
echo "\$(date +%s.%N) esc" >> $O/marks.txt
\$T send-keys -t p Escape; sleep 0.4; \$T send-keys -t p Escape; sleep 0.4; \$T send-keys -t p Escape; sleep 1
\$T capture-pane -p -t p > $O/screen_after_esc.txt
for i in \$(seq 1 $((WAIT/10))); do sleep 10; \$T capture-pane -p -t p > $O/screen_wait_\$i.txt; done
echo "\$(date +%s.%N) end" >> $O/marks.txt
\$T kill-server
kill \$API
INNER
chmod +x "$O/inner.sh"
timeout $((WAIT + 120)) unshare -rn bash "$O/inner.sh"
echo "probe exit $?"
