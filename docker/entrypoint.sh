#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/eric/Projects/pgu"
BARE_REPO="/data/git/pgu.git"
DIRECTOR_SESSION_ID="${DIRECTOR_SESSION_ID:-916d2445-6e1c-45d3-a04d-9b9299f7d0f7}"

# Working copy is container-local and ephemeral by design (only /data/git and
# ~/.claude are bind-mounted) -- recreate it fresh each container start.
if [ ! -d "$REPO_DIR/.git" ]; then
    git clone "$BARE_REPO" "$REPO_DIR"
fi

cd "$REPO_DIR"
git fetch origin --quiet
git checkout main --quiet
git merge --ff-only origin/main --quiet || true

CLAUDE="claude --dangerously-skip-permissions"

new_pane() {
    local session="$1"
    tmux new-session -d -s "$session" -c "$REPO_DIR"
}

new_pane pgu-director
new_pane pgu-main
new_pane pgu-audit
new_pane pgu-ui
new_pane pgu-ops
new_pane pgu-watchdog

# Director resumes its saved session (continuity is the point); the rest
# start fresh, standing, ready for directorctl to task them per packet.
tmux send-keys -t pgu-director:0.0 -l "$CLAUDE --resume $DIRECTOR_SESSION_ID"
tmux send-keys -t pgu-director:0.0 Enter

for session in pgu-main pgu-audit pgu-ui pgu-ops pgu-watchdog; do
    tmux send-keys -t "$session:0.0" -l "$CLAUDE"
    tmux send-keys -t "$session:0.0" Enter
done

echo "Team container ready. Sessions: $(tmux list-sessions -F '#{session_name}' | tr '\n' ' ')"
exec tail -f /dev/null
