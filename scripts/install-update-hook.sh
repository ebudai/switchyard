#!/usr/bin/env bash
set -euo pipefail

readonly ALLOW_ENV_NAME="PGU_ALLOW_MAIN_PUSH"
readonly ALLOW_ENV_VALUE="director"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${PGU_UPDATE_HOOK_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
SERVER_HOOKS_DIR="${PGU_UPDATE_HOOK_SERVER_HOOKS_DIR:-/data/git/pgu.git/hooks}"
BOARD_URL="${PGU_FILE_SIZE_BOARD_URL:-http://127.0.0.1:8770/api/tickets}"
FILE_SIZE_STATE_DIR="${PGU_FILE_SIZE_HOOK_STATE_DIR:-$SERVER_HOOKS_DIR/file-size-ticket-state}"
FILE_SIZE_LIMIT="${PGU_FILE_SIZE_LINE_LIMIT:-1250}"
FILE_SIZE_EMIT_CAP="${PGU_FILE_SIZE_HOOK_EMIT_CAP:-8}"
SERVER_SIZE_HOOK="$SERVER_HOOKS_DIR/pgu-file-size-ticket.py"
SERVER_STAGE3B_DEMO_HOOK="$SERVER_HOOKS_DIR/refresh-stage3b-demo.sh"
SERVER_UPDATE_HOOK="$SERVER_HOOKS_DIR/update"

usage() {
    cat <<EOF
Usage: scripts/install-update-hook.sh

Installs the bare-repo update hook that:
  - rejects direct pushes to refs/heads/main unless director override is set
  - auto-creates at most one persistent ticket per oversized source file,
    evaluating only the changed files at the pushed tip revision
  - auto-refreshes /tmp/pgu-stage3b-demo when main's render surface changes

Director override:
  $ALLOW_ENV_NAME=$ALLOW_ENV_VALUE git push origin HEAD:main
EOF
}

die() {
    printf '[install-update-hook] ERROR: %s\n' "$*" >&2
    exit 1
}

write_server_hook() {
    mkdir -p "$SERVER_HOOKS_DIR"
    install -m 0755 "$REPO_ROOT/scripts/report_file_size_limit.py" "$SERVER_SIZE_HOOK"
    install -m 0755 "$REPO_ROOT/scripts/refresh-stage3b-demo.sh" "$SERVER_STAGE3B_DEMO_HOOK"
    cat >"$SERVER_UPDATE_HOOK" <<EOF
#!/usr/bin/env bash
set -euo pipefail

readonly ALLOW_ENV_NAME="$ALLOW_ENV_NAME"
readonly ALLOW_ENV_VALUE="$ALLOW_ENV_VALUE"
readonly BOARD_URL="$BOARD_URL"
readonly FILE_SIZE_STATE_DIR="$FILE_SIZE_STATE_DIR"
readonly FILE_SIZE_LIMIT="$FILE_SIZE_LIMIT"
readonly FILE_SIZE_EMIT_CAP="$FILE_SIZE_EMIT_CAP"
readonly SIZE_HOOK="$SERVER_SIZE_HOOK"
readonly STAGE3B_DEMO_HOOK="$SERVER_STAGE3B_DEMO_HOOK"

refname="\${1:-}"
oldrev="\${2:-}"
newrev="\${3:-}"

if [[ "\$refname" == "refs/heads/main" && "\${!ALLOW_ENV_NAME:-}" != "\$ALLOW_ENV_VALUE" ]]; then
    cat >&2 <<MSG
[pgu main guard] Push rejected: refs/heads/main is director-only.
Push your feature branch and report via the ticket board.
Director override:
  $ALLOW_ENV_NAME=$ALLOW_ENV_VALUE git push origin HEAD:main
MSG
    exit 1
fi

if [[ -x "\$SIZE_HOOK" && "\$newrev" != "0000000000000000000000000000000000000000" ]]; then
    "\$SIZE_HOOK" \
        --git-dir "\$(pwd)" \
        --ref "\$refname" \
        --oldrev "\$oldrev" \
        --newrev "\$newrev" \
        --board-url "\$BOARD_URL" \
        --state-dir "\$FILE_SIZE_STATE_DIR" \
        --line-limit "\$FILE_SIZE_LIMIT" \
        --emit-cap "\$FILE_SIZE_EMIT_CAP" \
        || true
fi

if [[ -x "\$STAGE3B_DEMO_HOOK" ]]; then
    "\$STAGE3B_DEMO_HOOK" \
        --git-dir "\$(pwd)" \
        --ref "\$refname" \
        --oldrev "\$oldrev" \
        --newrev "\$newrev" \
        || true
fi

exit 0
EOF
    chmod 0755 "$SERVER_UPDATE_HOOK"
}

main() {
    case "${1:-}" in
        -h|--help)
            usage
            exit 0
            ;;
        "")
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac

    [[ -f "$REPO_ROOT/scripts/report_file_size_limit.py" ]] || die "missing file-size helper script under $REPO_ROOT/scripts"
    [[ -f "$REPO_ROOT/scripts/refresh-stage3b-demo.sh" ]] || die "missing stage3b demo helper script under $REPO_ROOT/scripts"
    write_server_hook
    printf '[install-update-hook] server update hook: %s\n' "$SERVER_UPDATE_HOOK"
    printf '[install-update-hook] file-size helper: %s\n' "$SERVER_SIZE_HOOK"
    printf '[install-update-hook] stage3b helper: %s\n' "$SERVER_STAGE3B_DEMO_HOOK"
}

main "$@"
