#!/bin/bash
# How tests/fixtures/syrd734_hermes_0.15.2_turn.json was captured (SYRD-734). Run as:
#   unshare -rn bash probe.sh <repo> <outdir>
# Loopback only: real Hermes in a private tmux, a notice typed as directorctl types it, no model called.
set -u
D=$(cd "$(dirname "$0")" && pwd); REPO=$1; OUT=$2
ip link set lo up
T=$(mktemp -d /tmp/h734.XXXXXX); mkdir -p $T/home/.hermes $T/sock; chmod 700 $T/sock
python3 $D/fake_llm.py 18734 $OUT/server.log & SRV=$!
python3 $D/config.py $REPO > $T/home/.hermes/config.yaml
SOCK=$T/sock/s
tmux -S $SOCK -f /dev/null new-session -d -x 150 -y 45 -s r "env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=$T/home HERMES_HOME=$T/home/.hermes TERM=xterm-256color LANG=C.UTF-8 hermes --yolo"
snap() { # name
  tmux -S $SOCK display-message -p -t r "#{cursor_x} #{cursor_y}" > $OUT/$1.cursor
  tmux -S $SOCK capture-pane -p -t r > $OUT/$1.txt
}
for i in $(seq 1 120); do
  sleep 0.5; tmux -S $SOCK capture-pane -p -t r | grep -q "❯" && break
done
sleep 2; snap idle
# typed_long was this run with MSG set to the long notice and nothing after `snap typed`.
tmux -S $SOCK send-keys -t r -l "${MSG:-MEFP-748 -- Hermes/Qwen canary entered Cancelled}"
sleep 0.3; snap typed
tmux -S $SOCK send-keys -t r Enter
for t in 0.2 0.5 1 2 4 8; do sleep $t; snap after_$t; done
tmux -S $SOCK kill-server; kill $SRV
