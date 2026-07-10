#!/usr/bin/env bash
set -euo pipefail

ZERO_OID="0000000000000000000000000000000000000000"
EMPTY_TREE_OID="4b825dc642cb6eb9a060e54bf8d69288fbee4904"
RENDER_PATHS=(
    "camera_controller.h"
    ":(glob)galaxy*.h"
    ":(glob)galaxy*.inl"
    "main.cpp"
    ":(glob)renderer*.h"
    ":(glob)shaders/**"
)

usage() {
    cat <<'EOF'
Usage: scripts/refresh-stage3b-demo.sh --git-dir <bare.git> --ref <ref> --oldrev <old> --newrev <new>

Refresh the shared /tmp stage3b demo bundle when refs/heads/main receives
render-surface changes.
EOF
}

log() {
    local stamp
    stamp="$(date --iso-8601=seconds)"
    mkdir -p "$(dirname "$LOG_PATH")"
    printf '[%s] [refresh-stage3b-demo] %s\n' "$stamp" "$*" | tee -a "$LOG_PATH" >&2
}

die() {
    log "ERROR: $*"
    exit 1
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

git_bare() {
    env -u GIT_DIR -u GIT_WORK_TREE git --git-dir="$GIT_DIR" "$@"
}

git_worktree() {
    local worktree="$1"
    shift
    env -u GIT_DIR -u GIT_WORK_TREE git -C "$worktree" "$@"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --git-dir)
                GIT_DIR="$2"
                shift 2
                ;;
            --ref)
                REFNAME="$2"
                shift 2
                ;;
            --oldrev)
                OLDREV="$2"
                shift 2
                ;;
            --newrev)
                NEWREV="$2"
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "unknown argument: $1"
                ;;
        esac
    done
}

ensure_demo_checkout() {
    if [[ -e "$DEMO_SOURCE_ROOT/.git" ]]; then
        log "updating demo source checkout at $DEMO_SOURCE_ROOT -> $NEWREV"
        git_worktree "$DEMO_SOURCE_ROOT" reset --hard "$NEWREV" >/dev/null
        return
    fi
    log "creating demo source checkout at $DEMO_SOURCE_ROOT -> $NEWREV"
    mkdir -p "$(dirname "$DEMO_SOURCE_ROOT")"
    git_bare worktree add --detach "$DEMO_SOURCE_ROOT" "$NEWREV" >/dev/null
}

ensure_build_dir() {
    if [[ -f "$DEMO_BUILD_DIR/CMakeCache.txt" ]]; then
        return
    fi
    log "configuring Release build at $DEMO_BUILD_DIR"
    cmake -S "$DEMO_SOURCE_ROOT" -B "$DEMO_BUILD_DIR" -DCMAKE_BUILD_TYPE=Release >/dev/null
}

collect_render_changes() {
    local base="$OLDREV"
    if [[ "$base" == "$ZERO_OID" ]]; then
        base="$EMPTY_TREE_OID"
    fi
    mapfile -t RENDER_CHANGES < <(git_bare diff --name-only "$base" "$NEWREV" -- "${RENDER_PATHS[@]}")
}

main() {
    GIT_DIR=""
    REFNAME=""
    OLDREV=""
    NEWREV=""
    parse_args "$@"

    [[ -n "$GIT_DIR" ]] || die "--git-dir is required"
    [[ -n "$REFNAME" ]] || die "--ref is required"
    [[ -n "$OLDREV" ]] || die "--oldrev is required"
    [[ -n "$NEWREV" ]] || die "--newrev is required"

    GIT_DIR="$(cd "$GIT_DIR" && pwd)"
    DEMO_SOURCE_ROOT="${PGU_STAGE3B_DEMO_SOURCE_ROOT:-/home/agent/pgu-stage3b-demo-main}"
    DEMO_BUILD_DIR="${PGU_STAGE3B_DEMO_BUILD_DIR:-$DEMO_SOURCE_ROOT/build-release}"
    DEMO_TARGET_DIR="${PGU_STAGE3B_DEMO_TARGET_DIR:-/tmp/pgu-stage3b-demo}"
    LOG_PATH="${PGU_STAGE3B_DEMO_LOG_PATH:-$GIT_DIR/hooks/stage3b-demo-refresh.log}"
    INSTALLER_REL="${PGU_STAGE3B_DEMO_INSTALLER_REL:-scripts/install-stage3b-demo.sh}"

    require_cmd git
    require_cmd cmake

    if [[ "$REFNAME" != "refs/heads/main" ]]; then
        exit 0
    fi
    if [[ "$NEWREV" == "$ZERO_OID" ]]; then
        log "skipping deleted main ref update"
        exit 0
    fi

    collect_render_changes
    if [[ "${#RENDER_CHANGES[@]}" -eq 0 ]]; then
        log "skipping: no render-surface changes in $OLDREV..$NEWREV"
        exit 0
    fi

    log "render changes detected on main: ${RENDER_CHANGES[*]}"
    ensure_demo_checkout
    ensure_build_dir

    local installer="$DEMO_SOURCE_ROOT/$INSTALLER_REL"
    [[ -f "$installer" ]] || die "missing installer in demo source checkout: $installer"

    log "reinstalling demo bundle into $DEMO_TARGET_DIR"
    (
        cd "$DEMO_SOURCE_ROOT"
        env -u GIT_DIR -u GIT_WORK_TREE \
        INSTALL_STAGE3B_DEMO_REF="$NEWREV" \
        SOURCE_BUILD_DIR="$DEMO_BUILD_DIR" \
        "$installer" "$DEMO_TARGET_DIR"
    )

    local install_commit=""
    if [[ -f "$DEMO_TARGET_DIR/installed-from.txt" ]]; then
        install_commit="$(sed -n 's/^install_commit=//p' "$DEMO_TARGET_DIR/installed-from.txt" | head -1)"
    fi
    log "demo bundle refreshed at commit ${install_commit:-unknown}"
}

main "$@"
