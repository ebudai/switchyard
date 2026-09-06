#!/usr/bin/env bash
set -euo pipefail

# PGU compatibility is explicit; shared runtime code no longer selects this home.
export TICKET_BOARD_OWNER_HOME=/home/agent

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

SOURCE_REPO="$TMPDIR_T/source"
DEPLOY_ROOT="$TMPDIR_T/live"
UNIT_DIR="$TMPDIR_T/unit"
mkdir -p "$UNIT_DIR"

git init "$SOURCE_REPO" >/dev/null
git -C "$SOURCE_REPO" config user.name Test
git -C "$SOURCE_REPO" config user.email test@example.com
mkdir -p "$SOURCE_REPO/scripts"
printf '#!/usr/bin/env python3\nprint("listener")\n' >"$SOURCE_REPO/scripts/ticket-board-notify-listener"
chmod +x "$SOURCE_REPO/scripts/ticket-board-notify-listener"
git -C "$SOURCE_REPO" add scripts/ticket-board-notify-listener
git -C "$SOURCE_REPO" commit -m "seed" >/dev/null

BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" deploy >/dev/null
deployed_sha="$(git -C "$SOURCE_REPO" rev-parse --verify HEAD^{commit})"

[[ -L "$DEPLOY_ROOT/current" ]] || {
    echo "FAIL: current deploy link missing" >&2
    exit 1
}
[[ "$(cat "$DEPLOY_ROOT/current/.pgu-deploy-sha")" == "$deployed_sha" ]] || {
    echo "FAIL: listener deploy did not write/activate the expected commit marker" >&2
    exit 1
}
[[ -x "$DEPLOY_ROOT/current/scripts/ticket-board-notify-listener" ]] || {
    echo "FAIL: exported listener script missing from current deploy" >&2
    exit 1
}
if [[ -d "$DEPLOY_ROOT/current/.git" ]]; then
    echo "FAIL: deploy root should be an export, not a git checkout" >&2
    exit 1
fi

SWITCHYARD_RELEASE_SOURCE="$TMPDIR_T/switchyard-release"
RELEASE_DEPLOY_ROOT="$TMPDIR_T/release-live"
mkdir -p "$SWITCHYARD_RELEASE_SOURCE"
git -C "$SOURCE_REPO" archive "$deployed_sha" | tar -x -C "$SWITCHYARD_RELEASE_SOURCE"
printf '{"commit":"%s"}\n' "$deployed_sha" >"$SWITCHYARD_RELEASE_SOURCE/.switchyard-release.json"
NO_GIT_BIN="$TMPDIR_T/no-git-bin"
mkdir -p "$NO_GIT_BIN"
cat >"$NO_GIT_BIN/git" <<'EOF'
#!/usr/bin/env bash
echo "FAIL: git must not be invoked for an exported Switchyard release source" >&2
exit 99
EOF
chmod +x "$NO_GIT_BIN/git"
PATH="$NO_GIT_BIN:/usr/bin:/bin" \
BOARD_ROOT="$RELEASE_DEPLOY_ROOT" SOURCE_REPO="$SWITCHYARD_RELEASE_SOURCE" DEPLOY_REF=origin/main TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" deploy >/dev/null
[[ "$(cat "$RELEASE_DEPLOY_ROOT/current/.pgu-deploy-sha")" == "$deployed_sha" ]] || {
    echo "FAIL: listener release-source deploy did not use the Switchyard release marker commit" >&2
    exit 1
}
[[ -x "$RELEASE_DEPLOY_ROOT/current/scripts/ticket-board-notify-listener" ]] || {
    echo "FAIL: listener release-source deploy did not copy the exported listener script" >&2
    exit 1
}
if [[ -d "$RELEASE_DEPLOY_ROOT/current/.git" ]]; then
    echo "FAIL: listener release-source deploy copied git metadata" >&2
    exit 1
fi

printf 'stale\n' >"$DEPLOY_ROOT/releases/$deployed_sha/.pgu-deploy-sha"
if BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" deploy >"$TMPDIR_T/marker.out" 2>"$TMPDIR_T/marker.err"; then
    echo "FAIL: listener deploy should fail when an existing release has the wrong commit marker" >&2
    exit 1
fi
grep -q 'sha marker mismatch' "$TMPDIR_T/marker.err" || {
    echo "FAIL: listener stale release marker failure was not reported clearly" >&2
    cat "$TMPDIR_T/marker.err" >&2
    exit 1
}
printf '%s\n' "$deployed_sha" >"$DEPLOY_ROOT/releases/$deployed_sha/.pgu-deploy-sha"

git -C "$SOURCE_REPO" remote add origin "$TMPDIR_T/missing-origin"
if BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" deploy >"$TMPDIR_T/fetch.out" 2>"$TMPDIR_T/fetch.err"; then
    echo "FAIL: listener deploy should fail loudly when git fetch origin fails" >&2
    exit 1
fi
grep -q 'failed to fetch origin' "$TMPDIR_T/fetch.err" || {
    echo "FAIL: listener fetch failure did not produce a clear stale-deploy error" >&2
    cat "$TMPDIR_T/fetch.err" >&2
    exit 1
}
git -C "$SOURCE_REPO" remote remove origin

printf '#!/usr/bin/env python3\nprint("listener safe directory")\n' >"$SOURCE_REPO/scripts/ticket-board-notify-listener"
git -C "$SOURCE_REPO" add scripts/ticket-board-notify-listener
git -C "$SOURCE_REPO" commit -m "safe-directory listener deploy" >/dev/null
SAFE_GIT_DIR="$TMPDIR_T/safe-git-bin"
mkdir -p "$SAFE_GIT_DIR"
REAL_GIT="$(command -v git)"
cat >"$SAFE_GIT_DIR/git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
required="safe.directory=$SOURCE_REPO_SAFE_DIRECTORY"
for arg in "$@"; do
    if [[ "$arg" == "$required" ]]; then
        exec "$REAL_GIT" "$@"
    fi
done
echo "fatal: detected dubious ownership in repository at '$SOURCE_REPO_SAFE_DIRECTORY'" >&2
exit 128
EOF
chmod +x "$SAFE_GIT_DIR/git"
PATH="$SAFE_GIT_DIR:/usr/bin:/bin" \
REAL_GIT="$REAL_GIT" \
SOURCE_REPO_SAFE_DIRECTORY="$SOURCE_REPO" \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" deploy >/dev/null
safe_deployed_sha="$(git -C "$SOURCE_REPO" rev-parse --verify HEAD^{commit})"
[[ "$(cat "$DEPLOY_ROOT/current/.pgu-deploy-sha")" == "$safe_deployed_sha" ]] || {
    echo "FAIL: listener deploy did not succeed through the safe.directory-scoped git wrapper" >&2
    exit 1
}
grep -q 'sudo -u "$owner" -H git -C "$SOURCE_REPO"' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" || {
    echo "FAIL: listener deploy git helper does not delegate privileged SOURCE_REPO git to the checkout owner" >&2
    exit 1
}
if grep -Eq 'chown[[:space:]].*-R.*SOURCE_REPO|chown[[:space:]].*SOURCE_REPO.*-R' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh"; then
    echo "FAIL: listener deploy script must not recursively chown SOURCE_REPO" >&2
    exit 1
fi

if BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$TMPDIR_T/absent-source" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" deploy >"$TMPDIR_T/missing-source.out" 2>"$TMPDIR_T/missing-source.err"; then
    echo "FAIL: listener deploy should fail when SOURCE_REPO path is absent" >&2
    exit 1
fi
grep -q 'missing source repo path' "$TMPDIR_T/missing-source.err" || {
    echo "FAIL: listener missing SOURCE_REPO path error was not distinct from git readability failures" >&2
    cat "$TMPDIR_T/missing-source.err" >&2
    exit 1
}

NON_GIT_SOURCE="$TMPDIR_T/not-git-not-release"
mkdir -p "$NON_GIT_SOURCE/scripts"
if BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$NON_GIT_SOURCE" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" deploy >"$TMPDIR_T/non-git.out" 2>"$TMPDIR_T/non-git.err"; then
    echo "FAIL: listener deploy should fail when SOURCE_REPO is neither a git checkout nor a Switchyard release" >&2
    exit 1
fi
grep -q 'source repo is not a readable git checkout or Switchyard release' "$TMPDIR_T/non-git.err" || {
    echo "FAIL: listener non-git release-source error did not name the primary source problem" >&2
    cat "$TMPDIR_T/non-git.err" >&2
    exit 1
}
if grep -Eq 'ln:|missing deploy sha marker' "$TMPDIR_T/non-git.err"; then
    echo "FAIL: listener source repo fatal cascaded into derived deploy errors" >&2
    cat "$TMPDIR_T/non-git.err" >&2
    exit 1
fi

BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" render-unit >"$UNIT_DIR/service.unit"

grep -q "WorkingDirectory=$DEPLOY_ROOT/current" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not point to deploy current link" >&2
    exit 1
}
grep -q "ExecStart=/usr/bin/python3 $DEPLOY_ROOT/current/scripts/ticket-board-notify-listener" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit ExecStart did not point to deployed listener script" >&2
    exit 1
}
TICKET_BOARD_PYTHON=/opt/switchyard/venv/bin/python \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" render-unit >"$UNIT_DIR/service-venv.unit"

grep -q "ExecStart=/opt/switchyard/venv/bin/python $DEPLOY_ROOT/current/scripts/ticket-board-notify-listener" "$UNIT_DIR/service-venv.unit" || {
    echo "FAIL: listener unit ExecStart did not honor TICKET_BOARD_PYTHON for the shared venv" >&2
    exit 1
}
grep -q '^Environment=PGUSER=ticket_board_listener$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not select ticket_board_listener by default" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_DATABASE_URL=postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_listener$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: listener unit did not bake the shared database URL" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_NOTIFY_DATABASE_URL=postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_listener$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: listener unit did not bake the notify database URL" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_PROJECT=pgu$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not set TICKET_BOARD_PROJECT" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_PANE_STATE_DIR=%t/pgu-ticket-board/pane-state$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not set the shared pane hook state directory" >&2
    exit 1
}
grep -q '^Restart=always$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: listener unit should always restart after drops/crashes" >&2
    exit 1
}
if grep -q '^RuntimeMaxSec=' "$UNIT_DIR/service.unit"; then
    echo "FAIL: queue listener should not rely on RuntimeMaxSec recycle" >&2
    exit 1
fi
grep -q 'ensure_database_roles' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" || {
    echo "FAIL: listener install path does not ensure ticket-board service roles" >&2
    exit 1
}
grep -q 'apply_database_migrations' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" || {
    echo "FAIL: listener deploy path does not apply ticket-board migrations" >&2
    exit 1
}
grep -q 'ensure-migrations)' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" || {
    echo "FAIL: listener script does not expose ensure-migrations for bootstrap testing" >&2
    exit 1
}
grep -q 'TICKET_BOARD_ADMIN_DATABASE_URL="$BOARD_ADMIN_DATABASE_URL" "$MIGRATION_RUNNER"' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" || {
    echo "FAIL: listener migration runner does not use the privileged admin database URL" >&2
    exit 1
}
grep -q 'BOARD_ADMIN_DATABASE_URL=".*user=postgres' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" || {
    echo "FAIL: listener installer admin connection does not default to user=postgres" >&2
    exit 1
}
grep -q 'psql -X -v ON_ERROR_STOP=1 "$BOARD_ADMIN_DATABASE_URL" -f "$RBAC_SQL"' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" || {
    echo "FAIL: listener installer does not apply RBAC SQL with psql" >&2
    exit 1
}
grep -q 'must connect as a PostgreSQL role with CREATEROLE' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" || {
    echo "FAIL: listener installer lacks a clear RBAC privilege error" >&2
    exit 1
}
grep -q 'ensure-roles)' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" || {
    echo "FAIL: listener script does not expose ensure-roles for bootstrap testing" >&2
    exit 1
}

TICKET_BOARD_NOTIFY_DATABASE_URL='postgresql:///custom_board?host=/tmp/pgu-pg&user=ticket_board_listener' \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" render-unit >"$UNIT_DIR/listener-custom-db.unit"

grep -q '^Environment=TICKET_BOARD_NOTIFY_DATABASE_URL=postgresql:///custom_board?host=/tmp/pgu-pg&user=ticket_board_listener$' "$UNIT_DIR/listener-custom-db.unit" || {
    echo "FAIL: listener unit did not bake caller-provided TICKET_BOARD_NOTIFY_DATABASE_URL" >&2
    exit 1
}

TICKET_BOARD_DATABASE_URL='postgresql:///shared_board?host=/tmp/pgu-pg&user=ticket_board_listener' \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" render-unit >"$UNIT_DIR/listener-shared-db.unit"

grep -q '^Environment=TICKET_BOARD_DATABASE_URL=postgresql:///shared_board?host=/tmp/pgu-pg&user=ticket_board_listener$' "$UNIT_DIR/listener-shared-db.unit" || {
    echo "FAIL: listener unit did not honor caller-provided TICKET_BOARD_DATABASE_URL" >&2
    exit 1
}

TICKET_BOARD_PROJECT=porter \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" render-unit >"$UNIT_DIR/listener-porter.unit"

grep -q '^Environment=PGDATABASE=porter_ticket_board$' "$UNIT_DIR/listener-porter.unit" || {
    echo "FAIL: generic project listener unit did not parameterize PGDATABASE" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_PANE_STATE_DIR=%t/porter-ticket-board/pane-state$' "$UNIT_DIR/listener-porter.unit" || {
    echo "FAIL: generic project listener unit did not parameterize pane state directory" >&2
    exit 1
}

TICKET_BOARD_DATABASE_URL='postgresql:///shared_board?host=/tmp/pgu-pg&user=ticket_board_listener' python3 - <<'PY'
from scripts.ticket_board.notify_listener import database_url_from_environment

assert database_url_from_environment() == "postgresql:///shared_board?host=/tmp/pgu-pg&user=ticket_board_listener"
PY

echo "ticket_board_notify_listener_service_test: ok"
