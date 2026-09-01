#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="${SWITCHYARD_HOOK_SOURCE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
INSTALLER="$SOURCE_ROOT/scripts/repository_hooks.py"
BARE_ROOT="${SWITCHYARD_BARE_REPO_ROOT:-/data/git}"
REGISTRY_DIR="${SWITCHYARD_PROJECT_REGISTRY_DIR:-/etc/switchyard/projects}"
SHARED_CHECKOUTS="${SWITCHYARD_SHARED_CHECKOUTS:-/home/agent/Projects/pgu:/home/eric/Projects/pgu}"
PLATFORM_WORKFLOW_REPOSITORIES="${SWITCHYARD_PLATFORM_WORKFLOW_REPOSITORIES:-$BARE_ROOT/pgu.git:$BARE_ROOT/switchyard.git}"
DRY_RUN=0

usage() {
    cat <<'EOF'
Usage: scripts/install-all-repository-hooks.sh [--dry-run]

Installs the main-branch guard only for platform and registered Switchyard
workflows, and installs warning-only pre-commit policy in working checkouts.
Bare repositories without a Switchyard workflow are reported and left without
a blocking guard. Requires root for production paths. Environment overrides
exist for isolated tests.
EOF
}

case "${1:-}" in
    "") ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'install-all-repository-hooks: unknown argument: %s\n' "$1" >&2; exit 2 ;;
esac

if [[ "$DRY_RUN" -ne 1 && "$EUID" -ne 0 && "${SWITCHYARD_HOOK_INSTALL_ALLOW_NON_ROOT:-}" != "1" ]]; then
    echo "install-all-repository-hooks: production rollout requires root" >&2
    exit 1
fi
[[ -x "$INSTALLER" ]] || { echo "install-all-repository-hooks: missing $INSTALLER" >&2; exit 1; }

run_install() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf 'would run:'
        printf ' %q' "$@"
        printf '\n'
        return
    fi
    "$@"
}

shopt -s nullglob
declare -A workflow_repository=()
IFS=: read -r -a platform_repositories <<<"$PLATFORM_WORKFLOW_REPOSITORIES"
for repository in "${platform_repositories[@]}"; do
    [[ -n "$repository" ]] || continue
    workflow_repository["$(readlink -m "$repository")"]=1
done

for repository in "$BARE_ROOT"/*.git; do
    name="$(basename "$repository" .git)"
    project="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-')"
    if [[ -n "${workflow_repository[$(readlink -m "$repository")]:-}" ]]; then
        printf 'MAIN GUARD (platform workflow): %s\n' "$repository"
        run_install "$INSTALLER" --source-root "$SOURCE_ROOT" --project "$project" \
            --repository "$repository" --server-only
    else
        printf 'NO MAIN GUARD (no Switchyard workflow): %s\n' "$repository"
    fi
done

IFS=: read -r -a shared_repositories <<<"$SHARED_CHECKOUTS"
for repository in "${shared_repositories[@]}"; do
    [[ -n "$repository" && -e "$repository/.git" ]] || continue
    printf 'FILE-SIZE WARNING ONLY: %s\n' "$repository"
    run_install "$INSTALLER" --source-root "$SOURCE_ROOT" --project pgu \
        --repository "$repository" --local-only
done

for registry in "$REGISTRY_DIR"/*.json; do
    config_path="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["config_path"])' "$registry")"
    printf 'REGISTERED WORKFLOW (warning + main guard): %s -> %s\n' "$registry" "$config_path"
    run_install "$INSTALLER" --source-root "$SOURCE_ROOT" --project-config "$config_path"
done
