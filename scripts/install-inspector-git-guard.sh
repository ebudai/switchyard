#!/usr/bin/env bash
set -euo pipefail

readonly INSPECTOR_TARGET_PATTERN="*-inspector*"
readonly BLOCK_MESSAGE="Inspector role cannot commit or push code. Code-level findings go through inspector-kick-back with a written reason; main/ops implement."
readonly ALLOW_ENV_NAME="ALLOW_MAIN_PUSH"
readonly LEGACY_ALLOW_ENV_NAME="PGU_ALLOW_MAIN_PUSH"
readonly ALLOW_ENV_VALUE="director"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${INSPECTOR_GIT_GUARD_REPO_ROOT:-${PGU_INSPECTOR_GIT_GUARD_REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}}"
GLOBAL_HOOKS_DIR="${GLOBAL_GIT_HOOKS_DIR:-${PGU_GLOBAL_GIT_HOOKS_DIR:-$HOME/.config/ticket-board/git-hooks}}"
FILE_SIZE_LIMIT="${FILE_SIZE_LINE_LIMIT:-${PGU_FILE_SIZE_LINE_LIMIT:-1250}}"

usage() {
    cat <<EOF
Usage: scripts/install-inspector-git-guard.sh

Installs global client-side git hooks for the agent user. The hooks block
pre-commit and pre-push when TICKET_BOARD_PANE_TARGET matches ${INSPECTOR_TARGET_PATTERN},
while preserving normal behavior for every other pane.
EOF
}

die() {
    printf '[install-inspector-git-guard] ERROR: %s\n' "$*" >&2
    exit 1
}

write_pre_commit_hook() {
    local hook_path="$GLOBAL_HOOKS_DIR/pre-commit"
    local warning_helper="$GLOBAL_HOOKS_DIR/warn-file-size-limit.py"
    install -m 0755 "$REPO_ROOT/scripts/warn_file_size_limit.py" "$warning_helper"
    cat >"$hook_path" <<EOF
#!/usr/bin/env bash
set -u

readonly INSPECTOR_TARGET_PATTERN="$INSPECTOR_TARGET_PATTERN"
readonly BLOCK_MESSAGE="$BLOCK_MESSAGE"
readonly FILE_SIZE_LIMIT="$FILE_SIZE_LIMIT"
readonly FILE_SIZE_HELPER="$warning_helper"

pane_target="\${TICKET_BOARD_PANE_TARGET:-\${PGU_PANE_TARGET:-}}"
case "\$pane_target" in
    \$INSPECTOR_TARGET_PATTERN)
        printf '%s\n' "\$BLOCK_MESSAGE" >&2
        exit 1
        ;;
esac

if [[ -x "\$FILE_SIZE_HELPER" ]]; then
    "\$FILE_SIZE_HELPER" --line-limit "\$FILE_SIZE_LIMIT" || true
fi

exit 0
EOF
    chmod 0755 "$hook_path"
}

write_pre_push_hook() {
    local hook_path="$GLOBAL_HOOKS_DIR/pre-push"
    cat >"$hook_path" <<EOF
#!/usr/bin/env bash
set -euo pipefail

readonly INSPECTOR_TARGET_PATTERN="$INSPECTOR_TARGET_PATTERN"
readonly BLOCK_MESSAGE="$BLOCK_MESSAGE"
readonly ALLOW_ENV_NAME="$ALLOW_ENV_NAME"
readonly LEGACY_ALLOW_ENV_NAME="$LEGACY_ALLOW_ENV_NAME"
readonly ALLOW_ENV_VALUE="$ALLOW_ENV_VALUE"

pane_target="\${TICKET_BOARD_PANE_TARGET:-\${PGU_PANE_TARGET:-}}"
case "\$pane_target" in
    \$INSPECTOR_TARGET_PATTERN)
        printf '%s\n' "\$BLOCK_MESSAGE" >&2
        exit 1
        ;;
esac

if [[ "\${!ALLOW_ENV_NAME:-}" == "\$ALLOW_ENV_VALUE" || "\${!LEGACY_ALLOW_ENV_NAME:-}" == "\$ALLOW_ENV_VALUE" ]]; then
    exit 0
fi

while read -r _local_ref _local_oid remote_ref _remote_oid; do
    if [[ "\$remote_ref" == "refs/heads/main" ]]; then
        cat >&2 <<MSG
[pgu main guard] Direct pushes to origin/main are blocked.
Push your feature branch instead and advance the ticket to director_review.
Director-only override:
  $ALLOW_ENV_NAME=$ALLOW_ENV_VALUE git push origin HEAD:main
  $LEGACY_ALLOW_ENV_NAME=$ALLOW_ENV_VALUE git push origin HEAD:main  # legacy pgu deployments
MSG
        exit 1
    fi
done

exit 0
EOF
    chmod 0755 "$hook_path"
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

    [[ -f "$REPO_ROOT/scripts/warn_file_size_limit.py" ]] || die "missing warning helper under $REPO_ROOT/scripts"
    mkdir -p "$GLOBAL_HOOKS_DIR"
    write_pre_commit_hook
    write_pre_push_hook
    git config --global core.hooksPath "$GLOBAL_HOOKS_DIR"
    printf '[install-inspector-git-guard] global hooks path: %s\n' "$GLOBAL_HOOKS_DIR"
    printf '[install-inspector-git-guard] pre-commit: %s\n' "$GLOBAL_HOOKS_DIR/pre-commit"
    printf '[install-inspector-git-guard] pre-push: %s\n' "$GLOBAL_HOOKS_DIR/pre-push"
}

main "$@"
