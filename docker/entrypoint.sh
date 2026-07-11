#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/eric/Projects/pgu"
BARE_REPO="/data/git/pgu.git"

# Working copy is container-local and ephemeral by design (only /data/git and
# ~/.claude are bind-mounted) -- recreate it fresh each container start.
if [ ! -d "$REPO_DIR/.git" ]; then
    git clone "$BARE_REPO" "$REPO_DIR"
fi

cd "$REPO_DIR"
git fetch origin --quiet
git checkout main --quiet
git merge --ff-only origin/main --quiet || true

ROLES_FILE="$REPO_DIR/docker/roles.json"
# watchdog is NOT a Claude role -- scripts/director_watchdog.py is a plain
# mechanical script, no model to configure. ops starts it (scripts/startwatchdog.fish),
# which creates its own pgu-watchdog tmux session -- don't pre-create one here.
ROLES="director main app audit ops research"

new_pane() {
    local session="$1"
    tmux new-session -d -s "$session" -c "$REPO_DIR"
}

for role in $ROLES; do
    new_pane "pgu-$role"
done

# Each role's model comes from docker/roles.json so different roles can run
# different Claude models/versions without touching this script. Director
# resumes its saved session (continuity is the point); the rest start fresh,
# standing, ready for directorctl to task them per packet.
for role in $ROLES; do
    model="$(jq -r ".roles[\"$role\"].model" "$ROLES_FILE")"
    if [ -z "$model" ] || [ "$model" = "null" ]; then
        echo "docker/roles.json: no model configured for role '$role'" >&2
        exit 1
    fi

    resume_id="$(jq -r ".roles[\"$role\"].resume_session_id // empty" "$ROLES_FILE")"
    if [ "$role" = "director" ] && [ -n "${DIRECTOR_SESSION_ID+x}" ]; then
        resume_id="$DIRECTOR_SESSION_ID"
    fi

    cmd="PGU_TICKET_BOARD_CALLER_ROLE=$role claude --model $model --dangerously-skip-permissions"
    if [ -n "$resume_id" ]; then
        cmd="$cmd --resume $resume_id"
    fi

    tmux send-keys -t "pgu-$role:0.0" -l "$cmd"
    tmux send-keys -t "pgu-$role:0.0" Enter
done

echo "Team container ready. Sessions: $(tmux list-sessions -F '#{session_name}' | tr '\n' ' ')"
exec tail -f /dev/null
