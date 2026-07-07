#!/usr/bin/env bash
# Opens one Konsole window per team tmux session, each running a plain,
# non-nested `docker exec -it pgu-team tmux attach`. No dashboard, no nested
# tmux -- avoids the mouse-focus/prefix-key collisions that made the single
# nested-tmux dashboard unusable. Tile the windows yourself (KWin quick-tile
# or drag-to-edge) the way you did in the VM.
set -euo pipefail

readonly CONTAINER="pgu-team"
# watchdog isn't launched by entrypoint.sh (it's ops's job to start it via
# scripts/startwatchdog.fish) -- its window may show "session not found"
# until ops has done that; just re-run this script or attach manually after.
readonly ROLES=(director main ui audit ops watchdog)

if ! command -v konsole >/dev/null 2>&1; then
    echo "error: konsole not found on this host -- this script only knows how to launch konsole windows" >&2
    echo "run manually instead: docker exec -it $CONTAINER tmux attach -t pgu-<role>" >&2
    exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "error: container '$CONTAINER' is not running" >&2
    exit 1
fi

for role in "${ROLES[@]}"; do
    session="pgu-$role"
    konsole --title "$session" -e bash -c "docker exec -it $CONTAINER tmux attach -t $session; exec bash" &
done

echo "Opened ${#ROLES[@]} windows: ${ROLES[*]}"
