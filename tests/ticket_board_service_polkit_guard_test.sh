#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

SOURCE_REPO="$TMPDIR_T/source"
DEPLOY_ROOT="$TMPDIR_T/live"
SYSTEM_UNIT_PATH="$TMPDIR_T/systemd/pgu-ticket-board.service"
MOCKDIR="$TMPDIR_T/mockbin"
LOGFILE="$TMPDIR_T/systemctl.log"
mkdir -p "$MOCKDIR" "$(dirname "$SYSTEM_UNIT_PATH")"
: >"$LOGFILE"
export TICKET_BOARD_POLKIT_APPROVAL_USER=user

git init "$SOURCE_REPO" >/dev/null
git -C "$SOURCE_REPO" config user.name Test
git -C "$SOURCE_REPO" config user.email test@example.com
mkdir -p "$SOURCE_REPO/scripts"
printf '#!/usr/bin/env python3\nprint("board")\n' >"$SOURCE_REPO/scripts/ticket-board.py"
chmod +x "$SOURCE_REPO/scripts/ticket-board.py"
git -C "$SOURCE_REPO" add scripts/ticket-board.py
git -C "$SOURCE_REPO" commit -m "seed" >/dev/null

touch "$SYSTEM_UNIT_PATH"

cat >"$MOCKDIR/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$TICKET_BOARD_SERVICE_TEST_LOG"
exit 0
EOF
chmod +x "$MOCKDIR/systemctl"

cat >"$MOCKDIR/systemd-run" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
echo "systemd-run must not be used for the board deploy canary: $*" >&2
exit 98
EOF
chmod +x "$MOCKDIR/systemd-run"

cat >"$MOCKDIR/loginctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "list-sessions" && "${2:-}" == "--no-legend" ]]; then
    printf '3 1000 user seat0\n'
    exit 0
fi
if [[ "${1:-}" == "show-session" && "${3:-}" == "-p" && "${5:-}" == "--value" ]]; then
    case "${4:-}" in
        Active)
            printf 'yes\n'
            ;;
        Remote)
            printf 'no\n'
            ;;
        Type)
            printf 'wayland\n'
            ;;
        *)
            exit 1
            ;;
    esac
    exit 0
fi
exit 1
EOF
chmod +x "$MOCKDIR/loginctl"

cat >"$MOCKDIR/python3" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-" ]]; then
    cat >/dev/null
    exit 0
fi
exec /usr/bin/python3 "$@"
EOF
chmod +x "$MOCKDIR/python3"

if PATH="$MOCKDIR:/usr/bin:/bin" \
    TICKET_BOARD_SERVICE_TEST_LOG="$LOGFILE" \
    BOARD_ROOT="$DEPLOY_ROOT" \
    SOURCE_REPO="$SOURCE_REPO" \
    DEPLOY_REF=HEAD \
    TICKET_BOARD_SKIP_MIGRATIONS=1 \
    TICKET_BOARD_SKIP_CANARY=1 \
    TICKET_BOARD_SYSTEM_UNIT_PATH="$SYSTEM_UNIT_PATH" \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy-restart >"$TMPDIR_T/out" 2>"$TMPDIR_T/err"; then
    :
else
    echo "FAIL: initial deploy-restart with active graphical session should seed the system unit hash" >&2
    cat "$TMPDIR_T/err" >&2 || true
    exit 1
fi

printf '#!/usr/bin/env python3\nprint("board v2")\n' >"$SOURCE_REPO/scripts/ticket-board.py"
git -C "$SOURCE_REPO" add scripts/ticket-board.py
git -C "$SOURCE_REPO" commit -m "code-only" >/dev/null

cat >"$MOCKDIR/loginctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "list-sessions" && "${2:-}" == "--no-legend" ]]; then
    printf '1 1001 agent \n'
    exit 0
fi
if [[ "${1:-}" == "show-session" ]]; then
    exit 1
fi
echo "unexpected loginctl call: $*" >&2
exit 1
EOF
chmod +x "$MOCKDIR/loginctl"

cat >"$MOCKDIR/timeout" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
for arg in "$@"; do
    if [[ "$arg" == "pgu-ticket-board.service" ]]; then
        echo "timeout wrapper should not be used for whitelisted board-unit action: $*" >&2
        exit 98
    fi
done
exec /usr/bin/timeout "$@"
EOF
chmod +x "$MOCKDIR/timeout"

: >"$LOGFILE"
if ! PATH="$MOCKDIR:/usr/bin:/bin" \
    TICKET_BOARD_SERVICE_TEST_LOG="$LOGFILE" \
    BOARD_ROOT="$DEPLOY_ROOT" \
    SOURCE_REPO="$SOURCE_REPO" \
    DEPLOY_REF=HEAD \
    TICKET_BOARD_SKIP_MIGRATIONS=1 \
    TICKET_BOARD_SKIP_CANARY=1 \
    TICKET_BOARD_SYSTEM_UNIT_PATH="$SYSTEM_UNIT_PATH" \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy-restart >"$TMPDIR_T/out-headless" 2>"$TMPDIR_T/err-headless"; then
    echo "FAIL: code-only deploy-restart should use the board-unit polkit whitelist without an active graphical session" >&2
    cat "$TMPDIR_T/err-headless" >&2 || true
    exit 1
fi

grep -q '^restart pgu-ticket-board.service$' "$LOGFILE" || {
    echo "FAIL: headless code-only deploy did not attempt the plain systemctl restart" >&2
    cat "$LOGFILE" >&2 || true
    exit 1
}
grep -q '^show pgu-ticket-board.service -p FragmentPath --value$' "$LOGFILE" || {
    echo "FAIL: headless code-only deploy did not inspect the system unit" >&2
    cat "$LOGFILE" >&2 || true
    exit 1
}

printf '#!/usr/bin/env python3\nprint("board v2 canary")\n' >"$SOURCE_REPO/scripts/ticket-board.py"
git -C "$SOURCE_REPO" add scripts/ticket-board.py
git -C "$SOURCE_REPO" commit -m "code-only canary" >/dev/null

: >"$LOGFILE"
if ! PATH="$MOCKDIR:/usr/bin:/bin" \
    TICKET_BOARD_SERVICE_TEST_LOG="$LOGFILE" \
    BOARD_ROOT="$DEPLOY_ROOT" \
    SOURCE_REPO="$SOURCE_REPO" \
    DEPLOY_REF=HEAD \
    TICKET_BOARD_SKIP_MIGRATIONS=1 \
    TICKET_BOARD_SYSTEM_UNIT_PATH="$SYSTEM_UNIT_PATH" \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy-restart >"$TMPDIR_T/out-canary" 2>"$TMPDIR_T/err-canary"; then
    echo "FAIL: headless deploy-restart should run the canary through the board-deploy polkit rule" >&2
    cat "$TMPDIR_T/err-canary" >&2 || true
    exit 1
fi

if grep -q 'skipping deploy canary' "$TMPDIR_T/err-canary"; then
    echo "FAIL: headless deploy-restart must not skip the canary" >&2
    cat "$TMPDIR_T/err-canary" >&2 || true
    exit 1
fi
grep -q '^start pgu-ticket-board-canary.service$' "$LOGFILE" || {
    echo "FAIL: headless deploy-restart did not start the fixed canary unit" >&2
    cat "$LOGFILE" >&2 || true
    exit 1
}
grep -q '^stop pgu-ticket-board-canary.service$' "$LOGFILE" || {
    echo "FAIL: headless deploy-restart did not stop the fixed canary unit" >&2
    cat "$LOGFILE" >&2 || true
    exit 1
}
grep -q '^restart pgu-ticket-board.service$' "$LOGFILE" || {
    echo "FAIL: headless deploy-restart did not restart the live board after canary pass" >&2
    cat "$LOGFILE" >&2 || true
    exit 1
}

(
    # Predicate-level regression guard: only exact board-unit actions may skip
    # the interactive polkit path.
    # shellcheck source=/dev/null
    source "$REPO_ROOT/scripts/ticket-board-service.sh"
    is_board_service_systemctl_action restart pgu-ticket-board.service || {
        echo "FAIL: exact board-unit restart should match the whitelist predicate" >&2
        exit 1
    }
    if is_board_service_systemctl_action restart unrelated.service; then
        echo "FAIL: restart of a different unit must not match the whitelist predicate" >&2
        exit 1
    fi
    if is_board_service_systemctl_action restart pgu-ticket-board.service --no-block; then
        echo "FAIL: board-unit restart with extra args must not match the whitelist predicate" >&2
        exit 1
    fi
    is_board_service_systemctl_action start pgu-ticket-board-canary.service || {
        echo "FAIL: exact canary start should match the whitelist predicate" >&2
        exit 1
    }
    is_board_service_systemctl_action stop pgu-ticket-board-canary.service || {
        echo "FAIL: exact canary stop should match the whitelist predicate" >&2
        exit 1
    }
    is_board_service_systemctl_action status pgu-ticket-board-canary.service --no-pager -l || {
        echo "FAIL: exact canary status should match the whitelist predicate" >&2
        exit 1
    }
    if is_board_service_systemctl_action restart pgu-ticket-board-canary.service; then
        echo "FAIL: canary restart must not match the whitelist predicate" >&2
        exit 1
    fi
    if is_board_service_systemctl_action stop pgu-ticket-board-canary.service --no-block; then
        echo "FAIL: canary stop with extra args must not match the whitelist predicate" >&2
        exit 1
    fi
    if is_board_service_systemctl_action start pgu-ticket-board-canary-12345; then
        echo "FAIL: dynamic canary instance starts must not match the whitelist predicate" >&2
        exit 1
    fi
    if is_board_service_systemctl_action stop pgu-ticket-board-canary-12345; then
        echo "FAIL: dynamic canary instance names must not match the whitelist predicate" >&2
        exit 1
    fi
    if is_board_service_systemctl_action daemon-reload; then
        echo "FAIL: daemon-reload must not match the board-unit whitelist predicate" >&2
        exit 1
    fi
)

POLKIT_RULE="$REPO_ROOT/deploy/polkit/49-pgu-board-deploy.rules"
[[ -f "$POLKIT_RULE" ]] || {
    echo "FAIL: board deploy polkit rule artifact is missing" >&2
    exit 1
}
grep -q 'subject.user !== "agent"' "$POLKIT_RULE" || {
    echo "FAIL: polkit rule is not scoped to the agent user" >&2
    exit 1
}
grep -q 'unit === "pgu-ticket-board.service"' "$POLKIT_RULE" || {
    echo "FAIL: polkit rule does not authorize the live board unit" >&2
    exit 1
}
grep -q 'unit === "pgu-ticket-board-canary.service"' "$POLKIT_RULE" || {
    echo "FAIL: polkit rule does not authorize the fixed deploy canary unit" >&2
    exit 1
}
grep -q 'verb === "restart"' "$POLKIT_RULE" || {
    echo "FAIL: polkit rule does not authorize live board restarts" >&2
    exit 1
}
grep -q 'verb === "start" || verb === "stop"' "$POLKIT_RULE" || {
    echo "FAIL: polkit rule does not authorize canary start/stop only" >&2
    exit 1
}
if grep -Eq 'pgu-ticket-board-canary-|StartTransientUnit|systemd-run' "$POLKIT_RULE"; then
    echo "FAIL: polkit rule must not authorize caller-defined transient canary units" >&2
    exit 1
fi
python3 - "$POLKIT_RULE" <<'PY'
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
first_yes = text.find("polkit.Result.YES")
guard_user = text.find('subject.user !== "agent"')
guard_action = text.find('action.id !== "org.freedesktop.systemd1.manage-units"')
unit_lookup = text.find('var unit = action.lookup("unit");')
live_unit = text.find('unit === "pgu-ticket-board.service"')
canary_unit = text.find('unit === "pgu-ticket-board-canary.service"')
final_not_handled = text.rfind("return polkit.Result.NOT_HANDLED;")
if min(first_yes, guard_user, guard_action, unit_lookup, live_unit, canary_unit, final_not_handled) < 0:
    raise SystemExit("missing expected polkit decision structure")
if not (guard_user < guard_action < unit_lookup < live_unit < canary_unit < final_not_handled):
    raise SystemExit("polkit rule guards must precede exact fixed-unit checks")
if first_yes < live_unit:
    raise SystemExit("polkit grants before matching an exact fixed unit")
if text.count("polkit.Result.YES") != 2:
    raise SystemExit("polkit rule should contain exactly two positive grant sites")
if "return polkit.Result.YES;" in text[:live_unit]:
    raise SystemExit("blanket grant appears before fixed-unit checks")
required_unit_checks = {
    '    if (unit === "pgu-ticket-board.service") {',
    '    if (unit === "pgu-ticket-board-canary.service") {',
}
lines = set(text.splitlines())
missing_unit_checks = required_unit_checks - lines
if missing_unit_checks:
    raise SystemExit(f"unit checks must be exact literals: {sorted(missing_unit_checks)}")
canary_block = text[canary_unit:final_not_handled]
if 'verb === "restart"' in canary_block:
    raise SystemExit("canary unit must not authorize restart")
if 'verb === "start" || verb === "stop"' not in canary_block:
    raise SystemExit("canary unit should authorize only start/stop")
PY

cat >"$MOCKDIR/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$TICKET_BOARD_SERVICE_TEST_LOG"
if [[ "${1:-}" == "restart" && "${2:-}" == "pgu-ticket-board.service" ]]; then
    echo "Interactive authentication required." >&2
    exit 1
fi
exit 0
EOF
chmod +x "$MOCKDIR/systemctl"

cat >"$MOCKDIR/timeout" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
echo "timeout wrapper should not be used for whitelisted board-unit action: $*" >&2
exit 98
EOF
chmod +x "$MOCKDIR/timeout"
: >"$LOGFILE"

printf '#!/usr/bin/env python3\nprint("board v3")\n' >"$SOURCE_REPO/scripts/ticket-board.py"
git -C "$SOURCE_REPO" add scripts/ticket-board.py
git -C "$SOURCE_REPO" commit -m "code-only auth failure" >/dev/null

if PATH="$MOCKDIR:/usr/bin:/bin" \
    TICKET_BOARD_SERVICE_TEST_LOG="$LOGFILE" \
    BOARD_ROOT="$DEPLOY_ROOT" \
    SOURCE_REPO="$SOURCE_REPO" \
    DEPLOY_REF=HEAD \
    TICKET_BOARD_SKIP_MIGRATIONS=1 \
    TICKET_BOARD_SKIP_CANARY=1 \
    TICKET_BOARD_SYSTEM_UNIT_PATH="$SYSTEM_UNIT_PATH" \
    TICKET_BOARD_POLKIT_TIMEOUT_SECONDS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy-restart >"$TMPDIR_T/out-auth" 2>"$TMPDIR_T/err-auth"; then
    echo "FAIL: deploy-restart should fail when the board-unit polkit whitelist is absent" >&2
    exit 1
fi

grep -q "Interactive authentication required" "$TMPDIR_T/err-auth" || {
    echo "FAIL: missing real systemctl authorization error when whitelist is absent" >&2
    cat "$TMPDIR_T/err-auth" >&2 || true
    exit 1
}
grep -q '^restart pgu-ticket-board.service$' "$LOGFILE" || {
    echo "FAIL: auth failure test never attempted the plain systemctl restart" >&2
    cat "$LOGFILE" >&2 || true
    exit 1
}

echo "ticket_board_service_polkit_guard_test: ok"
