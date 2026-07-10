#!/usr/bin/env bash
set -euo pipefail

readonly ALLOW_ENV_NAME="PGU_ALLOW_MAIN_PUSH"
readonly ALLOW_ENV_VALUE="director"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${PGU_UPDATE_HOOK_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
LOCAL_REPO_ROOT="${PGU_UPDATE_HOOK_LOCAL_REPO_ROOT:-$REPO_ROOT}"
SERVER_HOOKS_DIR="${PGU_UPDATE_HOOK_SERVER_HOOKS_DIR:-/data/git/pgu.git/hooks}"
FILE_SIZE_LIMIT="${PGU_FILE_SIZE_LINE_LIMIT:-1250}"
LOCAL_HOOKS_DIR="${PGU_UPDATE_HOOK_LOCAL_HOOKS_DIR:-}"
LOCAL_PRE_COMMIT_HOOK=""
LOCAL_WARNING_HELPER=""
SERVER_STAGE3B_DEMO_HOOK="$SERVER_HOOKS_DIR/refresh-stage3b-demo.sh"
SERVER_UPDATE_HOOK="$SERVER_HOOKS_DIR/update"
LEGACY_SERVER_SIZE_HOOK="$SERVER_HOOKS_DIR/pgu-file-size-ticket.py"

usage() {
    cat <<EOF
Usage: scripts/install-update-hook.sh

Installs the bare-repo update hook that:
  - rejects direct pushes to refs/heads/main unless director override is set
  - does NOT create push-time file-size tickets
  - auto-refreshes /tmp/pgu-stage3b-demo when main's render surface changes
and installs a local pre-commit hook that:
  - warns (without blocking) when staged source files exceed the soft line limit

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
    install -m 0755 "$REPO_ROOT/scripts/refresh-stage3b-demo.sh" "$SERVER_STAGE3B_DEMO_HOOK"
    rm -f "$LEGACY_SERVER_SIZE_HOOK"
    cat >"$SERVER_UPDATE_HOOK" <<EOF
#!/usr/bin/env bash
set -euo pipefail

readonly ALLOW_ENV_NAME="$ALLOW_ENV_NAME"
readonly ALLOW_ENV_VALUE="$ALLOW_ENV_VALUE"
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

resolve_local_hooks_dir() {
    if [[ -n "$LOCAL_HOOKS_DIR" ]]; then
        printf '%s\n' "$LOCAL_HOOKS_DIR"
        return 0
    fi
    local hooks_dir
    hooks_dir="$(git -C "$LOCAL_REPO_ROOT" rev-parse --git-path hooks)"
    if [[ "$hooks_dir" != /* ]]; then
        hooks_dir="$LOCAL_REPO_ROOT/$hooks_dir"
    fi
    printf '%s\n' "$hooks_dir"
}

write_local_pre_commit_hook() {
    local hooks_dir
    hooks_dir="$(resolve_local_hooks_dir)"
    mkdir -p "$hooks_dir"
    LOCAL_PRE_COMMIT_HOOK="$hooks_dir/pre-commit"
    LOCAL_WARNING_HELPER="$hooks_dir/pgu-warn-file-size-limit.py"
    install -m 0755 "$REPO_ROOT/scripts/warn_file_size_limit.py" "$LOCAL_WARNING_HELPER"
    cat >"$LOCAL_PRE_COMMIT_HOOK" <<EOF
#!/usr/bin/env bash
set -u

readonly FILE_SIZE_LIMIT="$FILE_SIZE_LIMIT"
readonly FILE_SIZE_HELPER="$LOCAL_WARNING_HELPER"

if [[ -x "\$FILE_SIZE_HELPER" ]]; then
    "\$FILE_SIZE_HELPER" --line-limit "\$FILE_SIZE_LIMIT" || true
fi

exit 0
EOF
    chmod 0755 "$LOCAL_PRE_COMMIT_HOOK"
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

    [[ -f "$REPO_ROOT/scripts/warn_file_size_limit.py" ]] || die "missing pre-commit warning helper script under $REPO_ROOT/scripts"
    [[ -f "$REPO_ROOT/scripts/refresh-stage3b-demo.sh" ]] || die "missing stage3b demo helper script under $REPO_ROOT/scripts"
    write_server_hook
    write_local_pre_commit_hook
    printf '[install-update-hook] server update hook: %s\n' "$SERVER_UPDATE_HOOK"
    printf '[install-update-hook] stage3b helper: %s\n' "$SERVER_STAGE3B_DEMO_HOOK"
    printf '[install-update-hook] local pre-commit hook: %s\n' "$LOCAL_PRE_COMMIT_HOOK"
    printf '[install-update-hook] local warning helper: %s\n' "$LOCAL_WARNING_HELPER"
}

main "$@"
