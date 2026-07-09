#!/usr/bin/env bash
set -euo pipefail

readonly ALLOW_ENV_NAME="PGU_ALLOW_MAIN_PUSH"
readonly ALLOW_ENV_VALUE="director"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${PGU_UPDATE_HOOK_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
SERVER_HOOKS_DIR="${PGU_UPDATE_HOOK_SERVER_HOOKS_DIR:-/data/git/pgu.git/hooks}"
BOARD_URL="${PGU_FILE_SIZE_BOARD_URL:-http://127.0.0.1:8770/api/tickets}"
FILE_SIZE_DEDUPE_DIR="${PGU_FILE_SIZE_HOOK_DEDUPE_DIR:-/tmp/pgu-file-size-ticket-dedupe}"
FILE_SIZE_LIMIT="${PGU_FILE_SIZE_LINE_LIMIT:-1250}"
SERVER_SIZE_HOOK="$SERVER_HOOKS_DIR/pgu-file-size-ticket.py"
SERVER_UPDATE_HOOK="$SERVER_HOOKS_DIR/update"

usage() {
    cat <<EOF
Usage: scripts/install-update-hook.sh

Installs the bare-repo update hook that:
  - rejects direct pushes to refs/heads/main unless director override is set
  - auto-creates one ticket per unique file+line-count when a pushed commit
    introduces or grows a tracked source file past the soft line limit

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
    cat >"$SERVER_UPDATE_HOOK" <<EOF
#!/usr/bin/env bash
set -euo pipefail

readonly ALLOW_ENV_NAME="$ALLOW_ENV_NAME"
readonly ALLOW_ENV_VALUE="$ALLOW_ENV_VALUE"
readonly BOARD_URL="$BOARD_URL"
readonly FILE_SIZE_DEDUPE_DIR="$FILE_SIZE_DEDUPE_DIR"
readonly FILE_SIZE_LIMIT="$FILE_SIZE_LIMIT"
readonly SIZE_HOOK="$SERVER_SIZE_HOOK"

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
        --dedupe-dir "\$FILE_SIZE_DEDUPE_DIR" \
        --line-limit "\$FILE_SIZE_LIMIT" \
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

    [[ -f "$REPO_ROOT/scripts/report_file_size_limit.py" ]] || die "missing helper script under $REPO_ROOT/scripts"
    write_server_hook
    printf '[install-update-hook] server update hook: %s\n' "$SERVER_UPDATE_HOOK"
    printf '[install-update-hook] file-size helper: %s\n' "$SERVER_SIZE_HOOK"
}

main "$@"
