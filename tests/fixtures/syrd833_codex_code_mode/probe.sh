#!/bin/bash
# probe.sh <outdir> <cmdfile>: real Codex offline, as a pane's root, runs one shell tool call: the command in <cmdfile>
# (SYRD-833). The private tmux server, the stand-in API and the socket directory go on every exit.
set -u
O=$1; CF=$2; D=$(cd "$(dirname "$0")" && pwd)
rm -rf "$O"; mkdir -p "$O/codex" "$O/work" "$O/home"
K=$(mktemp -d /tmp/s833.XXXX)
cleanup() {
    tmux -S "$K/t" kill-server 2>/dev/null
    [ -s "$O/api.pid" ] && kill "$(cat "$O/api.pid")" 2>/dev/null
    # Codex daemonises an app-server that outlives the pane (it leaked twice before --no-daemon): end anything that
    # names this run's directory -- as its program, never merely in its arguments, which would also match the shell
    # that started this probe.
    for p in /proc/[0-9]*; do
        exe=$(tr '\0' '\n' < "$p/cmdline" 2>/dev/null | head -1)
        case "$exe" in "$O/"*) kill -KILL "${p#/proc/}" 2>/dev/null ;; esac
    done
    rm -rf "$K"
}
trap cleanup EXIT
W=$O/work
cat > "$O/codex/config.toml" <<TOML
model = "gpt-6-sol"
model_provider = "mock"
approval_policy = "never"
sandbox_mode = "danger-full-access"
check_for_update_on_startup = false

[model_providers.mock]
name = "mock"
base_url = "http://127.0.0.1:18833/v1"
wire_api = "responses"
env_key = "MOCK_KEY"

[projects."$W"]
trust_level = "trusted"
TOML
cat > "$O/inner.sh" <<INNER
#!/bin/bash
ip link set lo up
PROBE_CMD_FILE=$CF python3 $D/fake_responses.py 18833 $O/api.jsonl > $O/api.err 2>&1 < /dev/null &
echo \$! > $O/api.pid
sleep 0.5
export HOME=$O/home CODEX_HOME=$O/codex MOCK_KEY=sk-placeholder-never-sent TERM=xterm-256color LANG=C.UTF-8 PATH=/usr/bin:/bin
printf 'sk-placeholder-never-sent\n' | codex login --with-api-key > $O/login.txt 2>&1
T="tmux -S $K/t"
\$T new-session -d -s p -x 160 -y 45 -c $W "exec codex --no-daemon --dangerously-bypass-approvals-and-sandbox -C $W 2> $O/codex.err"
for i in \$(seq 1 40); do sleep 1; \$T capture-pane -p -t p > $O/screen_boot.txt; grep -q 'OpenAI Codex' $O/screen_boot.txt && break; done
sleep 3
\$T send-keys -t p -l 'run it'; sleep 0.3; \$T send-keys -t p Enter
for i in \$(seq 1 40); do sleep 1; [ -s $O/look.json ] && break; done
sleep 1
\$T capture-pane -p -t p > $O/screen_end.txt
kill \$(cat $O/api.pid) 2>/dev/null
INNER
timeout 120 unshare -rn bash "$O/inner.sh"
