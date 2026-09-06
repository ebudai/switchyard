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
mkdir -p "$SOURCE_REPO/deploy/systemd" "$SOURCE_REPO/scripts"
printf '#!/usr/bin/env python3\nprint("board")\n' >"$SOURCE_REPO/scripts/ticket-board.py"
cat >"$SOURCE_REPO/deploy/systemd/pgu-ticket-board.service.boardsvc" <<'EOF'
[Unit]
Description=PGU Ticket Board Test boardsvc Unit

[Service]
Type=simple
User=boardsvc
WorkingDirectory=/home/agent/pgu-ticketboard-live/current
RuntimeDirectory=pgu-ticket-board
ExecStart=/usr/bin/python3 /home/agent/pgu-ticketboard-live/current/scripts/ticket-board.py --host 127.0.0.1 --port 8770 --unix-socket /run/pgu-ticket-board/ticket-board.sock --frames /tmp/pgu-frames --assets /home/agent/.claude/pgu-tickets-assets
Environment=TICKET_BOARD_PROJECT=pgu
Environment=TICKET_BOARD_SOCKET=/run/pgu-ticket-board/ticket-board.sock
Environment=TICKET_BOARD_DATABASE_URL=postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_service

[Install]
WantedBy=multi-user.target
EOF
chmod +x "$SOURCE_REPO/scripts/ticket-board.py"
git -C "$SOURCE_REPO" add deploy/systemd/pgu-ticket-board.service.boardsvc scripts/ticket-board.py
git -C "$SOURCE_REPO" commit -m "seed" >/dev/null

BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy >/dev/null
deployed_sha="$(git -C "$SOURCE_REPO" rev-parse --verify HEAD^{commit})"

[[ -L "$DEPLOY_ROOT/current" ]] || {
    echo "FAIL: current deploy link missing" >&2
    exit 1
}
[[ "$(cat "$DEPLOY_ROOT/current/.pgu-deploy-sha")" == "$deployed_sha" ]] || {
    echo "FAIL: deploy did not write/activate the expected commit marker" >&2
    exit 1
}
[[ -x "$DEPLOY_ROOT/current/scripts/ticket-board.py" ]] || {
    echo "FAIL: exported board script missing from current deploy" >&2
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
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy >/dev/null
[[ "$(cat "$RELEASE_DEPLOY_ROOT/current/.pgu-deploy-sha")" == "$deployed_sha" ]] || {
    echo "FAIL: release-source deploy did not use the Switchyard release marker commit" >&2
    exit 1
}
[[ -x "$RELEASE_DEPLOY_ROOT/current/scripts/ticket-board.py" ]] || {
    echo "FAIL: release-source deploy did not copy the exported board script" >&2
    exit 1
}
if [[ -d "$RELEASE_DEPLOY_ROOT/current/.git" ]]; then
    echo "FAIL: release-source deploy copied git metadata" >&2
    exit 1
fi

printf 'stale\n' >"$DEPLOY_ROOT/releases/$deployed_sha/.pgu-deploy-sha"
if BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy >"$TMPDIR_T/marker.out" 2>"$TMPDIR_T/marker.err"; then
    echo "FAIL: deploy should fail when an existing release has the wrong commit marker" >&2
    exit 1
fi
grep -q 'sha marker mismatch' "$TMPDIR_T/marker.err" || {
    echo "FAIL: stale release marker failure was not reported clearly" >&2
    cat "$TMPDIR_T/marker.err" >&2
    exit 1
}
printf '%s\n' "$deployed_sha" >"$DEPLOY_ROOT/releases/$deployed_sha/.pgu-deploy-sha"

git -C "$SOURCE_REPO" remote add origin "$TMPDIR_T/missing-origin"
if BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy >"$TMPDIR_T/fetch.out" 2>"$TMPDIR_T/fetch.err"; then
    echo "FAIL: deploy should fail loudly when git fetch origin fails" >&2
    exit 1
fi
grep -q 'failed to fetch origin' "$TMPDIR_T/fetch.err" || {
    echo "FAIL: fetch failure did not produce a clear stale-deploy error" >&2
    cat "$TMPDIR_T/fetch.err" >&2
    exit 1
}
git -C "$SOURCE_REPO" remote remove origin

printf '#!/usr/bin/env python3\nprint("board safe directory")\n' >"$SOURCE_REPO/scripts/ticket-board.py"
git -C "$SOURCE_REPO" add scripts/ticket-board.py
git -C "$SOURCE_REPO" commit -m "safe-directory deploy" >/dev/null
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
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy >/dev/null
safe_deployed_sha="$(git -C "$SOURCE_REPO" rev-parse --verify HEAD^{commit})"
[[ "$(cat "$DEPLOY_ROOT/current/.pgu-deploy-sha")" == "$safe_deployed_sha" ]] || {
    echo "FAIL: deploy did not succeed through the safe.directory-scoped git wrapper" >&2
    exit 1
}

OWNER_DEPLOY_TMP="$TMPDIR_T/owner-deploy"
OWNER_ORIGIN="$OWNER_DEPLOY_TMP/origin.git"
OWNER_SOURCE="$OWNER_DEPLOY_TMP/source"
OWNER_UPDATER="$OWNER_DEPLOY_TMP/updater"
OWNER_DEPLOY_ROOT="$OWNER_DEPLOY_TMP/live"
mkdir -p "$OWNER_DEPLOY_TMP"
git init --bare "$OWNER_ORIGIN" >/dev/null
git -C "$OWNER_ORIGIN" symbolic-ref HEAD refs/heads/main
git clone "$OWNER_ORIGIN" "$OWNER_SOURCE" >/dev/null 2>&1
git -C "$OWNER_SOURCE" config user.name Test
git -C "$OWNER_SOURCE" config user.email test@example.com
mkdir -p "$OWNER_SOURCE/deploy/systemd" "$OWNER_SOURCE/scripts"
printf '#!/usr/bin/env python3\nprint("owner board")\n' >"$OWNER_SOURCE/scripts/ticket-board.py"
cat >"$OWNER_SOURCE/deploy/systemd/pgu-ticket-board.service.boardsvc" <<'EOF'
[Unit]
Description=PGU Ticket Board Test boardsvc Unit

[Service]
Type=simple
User=boardsvc
WorkingDirectory=/home/agent/pgu-ticketboard-live/current
RuntimeDirectory=pgu-ticket-board
ExecStart=/usr/bin/python3 /home/agent/pgu-ticketboard-live/current/scripts/ticket-board.py --host 127.0.0.1 --port 8770 --unix-socket /run/pgu-ticket-board/ticket-board.sock --frames /tmp/pgu-frames --assets /home/agent/.claude/pgu-tickets-assets
Environment=TICKET_BOARD_PROJECT=pgu
Environment=TICKET_BOARD_SOCKET=/run/pgu-ticket-board/ticket-board.sock
Environment=TICKET_BOARD_DATABASE_URL=postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_service

[Install]
WantedBy=multi-user.target
EOF
chmod +x "$OWNER_SOURCE/scripts/ticket-board.py"
git -C "$OWNER_SOURCE" add deploy/systemd/pgu-ticket-board.service.boardsvc scripts/ticket-board.py
git -C "$OWNER_SOURCE" commit -m "seed owner deploy" >/dev/null
PGU_ALLOW_MAIN_PUSH=director git -C "$OWNER_SOURCE" push origin HEAD:main >/dev/null
git clone "$OWNER_ORIGIN" "$OWNER_UPDATER" >/dev/null 2>&1
git -C "$OWNER_UPDATER" config user.name Test
git -C "$OWNER_UPDATER" config user.email test@example.com
printf '#!/usr/bin/env python3\nprint("owner board updated")\n' >"$OWNER_UPDATER/scripts/ticket-board.py"
git -C "$OWNER_UPDATER" add scripts/ticket-board.py
git -C "$OWNER_UPDATER" commit -m "remote owner deploy update" >/dev/null
PGU_ALLOW_MAIN_PUSH=director git -C "$OWNER_UPDATER" push origin HEAD:main >/dev/null
SOURCE_OWNER="$(stat -c '%U' "$OWNER_SOURCE")"
FAKE_ROOT_BIN="$OWNER_DEPLOY_TMP/fake-root-bin"
FAKE_ROOT_LOG="$OWNER_DEPLOY_TMP/fake-sudo.log"
mkdir -p "$FAKE_ROOT_BIN"
cat >"$FAKE_ROOT_BIN/id" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
    -u) printf '0\n' ;;
    -un) printf 'root\n' ;;
    *) /usr/bin/id "$@" ;;
esac
EOF
cat >"$FAKE_ROOT_BIN/sudo" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_ROOT_LOG:?}"
if [[ "${1:-}" == "-u" ]]; then
    shift 2
fi
if [[ "${1:-}" == "-H" ]]; then
    shift
fi
exec "$@"
EOF
chmod +x "$FAKE_ROOT_BIN/id" "$FAKE_ROOT_BIN/sudo"
PATH="$FAKE_ROOT_BIN:/usr/bin:/bin" \
FAKE_ROOT_LOG="$FAKE_ROOT_LOG" \
BOARD_ROOT="$OWNER_DEPLOY_ROOT" SOURCE_REPO="$OWNER_SOURCE" DEPLOY_REF=origin/main TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy >/dev/null
grep -q -- "-u $SOURCE_OWNER -H git -C $OWNER_SOURCE fetch origin" "$FAKE_ROOT_LOG" || {
    echo "FAIL: privileged deploy did not fetch SOURCE_REPO as the checkout owner" >&2
    cat "$FAKE_ROOT_LOG" >&2
    exit 1
}
git -C "$OWNER_SOURCE" pull --ff-only >/dev/null
if find "$OWNER_SOURCE" ! -user "$SOURCE_OWNER" -print -quit | grep -q .; then
    echo "FAIL: privileged deploy left files not owned by the source repo owner" >&2
    find "$OWNER_SOURCE" ! -user "$SOURCE_OWNER" -print >&2
    exit 1
fi
if grep -Eq 'chown[[:space:]].*-R.*SOURCE_REPO|chown[[:space:]].*SOURCE_REPO.*-R' "$REPO_ROOT/scripts/ticket-board-service.sh"; then
    echo "FAIL: deploy script must not recursively chown SOURCE_REPO" >&2
    exit 1
fi

if BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$TMPDIR_T/absent-source" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy >"$TMPDIR_T/missing-source.out" 2>"$TMPDIR_T/missing-source.err"; then
    echo "FAIL: deploy should fail when SOURCE_REPO path is absent" >&2
    exit 1
fi
grep -q 'missing source repo path' "$TMPDIR_T/missing-source.err" || {
    echo "FAIL: missing SOURCE_REPO path error was not distinct from git readability failures" >&2
    cat "$TMPDIR_T/missing-source.err" >&2
    exit 1
}

NON_GIT_SOURCE="$TMPDIR_T/not-git-not-release"
mkdir -p "$NON_GIT_SOURCE/scripts"
if BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$NON_GIT_SOURCE" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy >"$TMPDIR_T/non-git.out" 2>"$TMPDIR_T/non-git.err"; then
    echo "FAIL: deploy should fail when SOURCE_REPO is neither a git checkout nor a Switchyard release" >&2
    exit 1
fi
grep -q 'source repo is not a readable git checkout or Switchyard release' "$TMPDIR_T/non-git.err" || {
    echo "FAIL: non-git release-source error did not name the primary source problem" >&2
    cat "$TMPDIR_T/non-git.err" >&2
    exit 1
}
if grep -Eq 'ln:|missing deploy sha marker|deploy export did not return a release path' "$TMPDIR_T/non-git.err"; then
    echo "FAIL: source repo fatal cascaded into derived deploy errors" >&2
    cat "$TMPDIR_T/non-git.err" >&2
    exit 1
fi

BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" render-unit >"$UNIT_DIR/service.unit"

grep -q "WorkingDirectory=$DEPLOY_ROOT/current" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not point to deploy current link" >&2
    exit 1
}
grep -q "ExecStart=/usr/bin/python3 $DEPLOY_ROOT/current/scripts/ticket-board.py" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit ExecStart did not point to deploy current script" >&2
    exit 1
}
TICKET_BOARD_PYTHON=/opt/switchyard/venv/bin/python \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" render-unit >"$UNIT_DIR/service-venv.unit"

grep -q "ExecStart=/opt/switchyard/venv/bin/python $DEPLOY_ROOT/current/scripts/ticket-board.py" "$UNIT_DIR/service-venv.unit" || {
    echo "FAIL: unit ExecStart did not honor TICKET_BOARD_PYTHON for the shared venv" >&2
    exit 1
}
grep -q '^ExecStartPre=/bin/mkdir -p /tmp/pgu-frames$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not recreate the frame directory before start" >&2
    exit 1
}
grep -q '^ExecStartPre=/bin/chmod 1777 /tmp/pgu-frames$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not restore shared frame directory permissions before start" >&2
    exit 1
}
grep -q '^RuntimeDirectory=pgu-ticket-board$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not request a service-owned runtime directory" >&2
    exit 1
}
grep -q -- "--unix-socket /run/pgu-ticket-board/ticket-board.sock" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not expose the local pane Unix socket" >&2
    exit 1
}
grep -q -- "--frames /tmp/pgu-frames" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not pin the shared frame directory explicitly" >&2
    exit 1
}
retired_backend_flag='--store''-backend'
if grep -q -- "$retired_backend_flag" "$UNIT_DIR/service.unit"; then
    echo "FAIL: rendered unit should not pass the removed backend selector option" >&2
    exit 1
fi
grep -q '^Environment=TICKET_BOARD_PROJECT=pgu$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not export TICKET_BOARD_PROJECT" >&2
    exit 1
}
grep -q '^EnvironmentFile=-/home/agent/.config/pgu/ticket-board.env$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not load the tenant report token env file" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_SOCKET=/run/pgu-ticket-board/ticket-board.sock$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not export TICKET_BOARD_SOCKET" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_COMMIT_GIT_DIR=/data/git/switchyard.git:/data/git/pgu.git$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not export the default commit verification repository" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_DATABASE_URL=postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_service$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not bake the service database URL" >&2
    exit 1
}
grep -q 'ensure_database_roles' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: install path does not ensure ticket-board service roles" >&2
    exit 1
}
grep -q 'apply_database_migrations' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service deploy path does not apply ticket-board migrations" >&2
    exit 1
}
grep -q 'ensure-migrations)' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service script does not expose ensure-migrations for bootstrap testing" >&2
    exit 1
}
grep -q 'TICKET_BOARD_ADMIN_DATABASE_URL="$BOARD_ADMIN_DATABASE_URL" "$migration_runner"' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service migration runner does not use the privileged admin database URL for the selected release" >&2
    exit 1
}
grep -q 'BOARD_ADMIN_DATABASE_URL=".*user=postgres' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service installer admin connection does not default to user=postgres" >&2
    exit 1
}
grep -q 'psql -X -v ON_ERROR_STOP=1 "$BOARD_ADMIN_DATABASE_URL" -f "$RBAC_SQL"' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service installer does not apply RBAC SQL with psql" >&2
    exit 1
}
grep -q 'must connect as a PostgreSQL role with CREATEROLE' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service installer lacks a clear RBAC privilege error" >&2
    exit 1
}
grep -q 'smoke_check_http' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service script lacks a post-start smoke check" >&2
    exit 1
}
grep -q 'urllib.request.urlopen(url, timeout=1.0)' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service smoke check does not perform an HTTP request" >&2
    exit 1
}
grep -q 'resolved_service_scope' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service script does not resolve the live service scope" >&2
    exit 1
}
grep -q 'systemctl_system restart "$SERVICE_NAME"' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: deploy-restart does not restart the system service when it is live" >&2
    exit 1
}
grep -q 'systemctl_user restart "$SERVICE_NAME"' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: deploy-restart no longer supports the user-unit path" >&2
    exit 1
}
grep -q 'quiesce_user_shadow_unit' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: system-scope deploy does not quiesce the shadow user unit" >&2
    exit 1
}
grep -q 'ensure-roles)' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service script does not expose ensure-roles for bootstrap testing" >&2
    exit 1
}

TICKET_BOARD_DATABASE_URL='postgresql:///custom_board?host=/tmp/pgu-pg&user=ticket_board_service' \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" render-unit >"$UNIT_DIR/service-custom-db.unit"

grep -q '^Environment=TICKET_BOARD_DATABASE_URL=postgresql:///custom_board?host=/tmp/pgu-pg&user=ticket_board_service$' "$UNIT_DIR/service-custom-db.unit" || {
    echo "FAIL: unit did not bake caller-provided TICKET_BOARD_DATABASE_URL" >&2
    exit 1
}

TICKET_BOARD_COMMIT_GIT_DIR="$TMPDIR_T/project-control.git" \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" render-unit >"$UNIT_DIR/service-custom-commit-repo.unit"

grep -q "^Environment=TICKET_BOARD_COMMIT_GIT_DIR=$TMPDIR_T/project-control.git$" "$UNIT_DIR/service-custom-commit-repo.unit" || {
    echo "FAIL: unit did not bake caller-provided TICKET_BOARD_COMMIT_GIT_DIR" >&2
    exit 1
}

TICKET_BOARD_PROJECT=porter \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" render-unit >"$UNIT_DIR/service-porter.unit"

grep -q '^RuntimeDirectory=porter-ticket-board$' "$UNIT_DIR/service-porter.unit" || {
    echo "FAIL: generic project unit did not parameterize RuntimeDirectory" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_SOCKET=/run/porter-ticket-board/ticket-board.sock$' "$UNIT_DIR/service-porter.unit" || {
    echo "FAIL: generic project unit did not parameterize socket env" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_DATABASE_URL=postgresql:///porter_ticket_board?host=/var/run/postgresql&user=ticket_board_service$' "$UNIT_DIR/service-porter.unit" || {
    echo "FAIL: generic project unit did not parameterize database URL" >&2
    exit 1
}
grep -q "^Environment=TICKET_BOARD_COMMIT_GIT_DIR=$HOME/.local/state/switchyard/projects/porter/control.git$" "$UNIT_DIR/service-porter.unit" || {
    echo "FAIL: generic project unit did not parameterize commit verification repository" >&2
    exit 1
}

TICKET_BOARD_PROJECT=mefp \
HOME=/home/stellaris-agent \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" render-unit >"$UNIT_DIR/service-mefp.unit"

grep -q '^Environment=TICKET_BOARD_COMMIT_GIT_DIR=/data/git/stellaris-fixpatch.git$' "$UNIT_DIR/service-mefp.unit" || {
    echo "FAIL: mefp unit did not use the live legacy commit verification repository" >&2
    exit 1
}

TICKET_BOARD_PROJECT=otto \
HOME=/home/otto-agent \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" render-unit >"$UNIT_DIR/service-otto.unit"

grep -q '^Environment=TICKET_BOARD_COMMIT_GIT_DIR=/data/git/otto_scheduler.git$' "$UNIT_DIR/service-otto.unit" || {
    echo "FAIL: otto unit did not use the live legacy commit verification repository" >&2
    exit 1
}

TICKET_BOARD_PROJECT=porter \
HOME=/home/porter-agent \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" render-unit >"$UNIT_DIR/service-porter-home.unit"

grep -q '^Environment=TICKET_BOARD_COMMIT_GIT_DIR=/home/porter-agent/.local/state/switchyard/projects/porter/control.git$' "$UNIT_DIR/service-porter-home.unit" || {
    echo "FAIL: generic project unit did not default to the owner's provisioned control repo" >&2
    exit 1
}

MOCKDIR="$TMPDIR_T/mockbin"
LOGFILE="$TMPDIR_T/service-scope.log"
HASH_RECORD="$TMPDIR_T/system-unit.sha256"
SYSTEM_UNIT_PATH="$TMPDIR_T/systemd/pgu-ticket-board.service"
mkdir -p "$MOCKDIR" "$(dirname "$SYSTEM_UNIT_PATH")"
: >"$LOGFILE"

cat >"$MOCKDIR/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--user" ]]; then
    shift
    printf 'user:%s\n' "$*" >>"$TICKET_BOARD_SERVICE_TEST_LOG"
    exit 0
fi
printf 'system:%s\n' "$*" >>"$TICKET_BOARD_SERVICE_TEST_LOG"
if [[ "${1:-}" == "show" && "${2:-}" == "pgu-ticket-board.service" && "${3:-}" == "-p" && "${4:-}" == "FragmentPath" && "${5:-}" == "--value" ]]; then
    printf '%s\n' "$TICKET_BOARD_SYSTEM_UNIT_PATH"
fi
if [[ "${1:-}" == "show" && "${2:-}" == "pgu-ticket-board.service" && "${3:-}" == "-p" && "${4:-}" == "NeedDaemonReload" && "${5:-}" == "--value" ]]; then
    printf '%s\n' "${TICKET_BOARD_TEST_NEED_DAEMON_RELOAD:-no}"
fi
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

cp "$DEPLOY_ROOT/current/deploy/systemd/pgu-ticket-board.service.boardsvc" "$SYSTEM_UNIT_PATH"
sha256sum "$SYSTEM_UNIT_PATH" | awk '{print $1}' >"$HASH_RECORD"

PATH="$MOCKDIR:/usr/bin:/bin" \
TICKET_BOARD_SERVICE_TEST_LOG="$LOGFILE" \
TICKET_BOARD_SYSTEM_UNIT_PATH="$SYSTEM_UNIT_PATH" \
TICKET_BOARD_SYSTEM_UNIT_HASH_RECORD="$HASH_RECORD" \
TICKET_BOARD_SKIP_POST_DEPLOY_SOCKET_VERIFY=1 \
TICKET_BOARD_POLKIT_APPROVAL_USER=user \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy-restart >/dev/null

grep -q '^system:show pgu-ticket-board.service -p FragmentPath --value$' "$LOGFILE" || {
    echo "FAIL: deploy-restart did not inspect the system unit fragment path" >&2
    cat "$LOGFILE" >&2
    exit 1
}
if grep -q '^system:daemon-reload$' "$LOGFILE"; then
    echo "FAIL: code-only deploy-restart should not reload the system service manager" >&2
    cat "$LOGFILE" >&2
    exit 1
fi
grep -q '^system:restart pgu-ticket-board.service$' "$LOGFILE" || {
    echo "FAIL: deploy-restart did not restart the live system unit" >&2
    cat "$LOGFILE" >&2
    exit 1
}
grep -q '^system:stop pgu-ticket-board-canary.service$' "$LOGFILE" || {
    echo "FAIL: deploy-restart did not stop the fixed canary unit" >&2
    cat "$LOGFILE" >&2
    exit 1
}
grep -q '^system:start pgu-ticket-board-canary.service$' "$LOGFILE" || {
    echo "FAIL: deploy-restart did not start the fixed canary unit" >&2
    cat "$LOGFILE" >&2
    exit 1
}
if grep -q '^user:restart pgu-ticket-board.service$' "$LOGFILE"; then
    echo "FAIL: deploy-restart incorrectly restarted the shadow user unit" >&2
    cat "$LOGFILE" >&2
    exit 1
fi
if grep -q 'sudo' "$LOGFILE"; then
    echo "FAIL: deploy-restart should use plain systemctl, not sudo" >&2
    cat "$LOGFILE" >&2
    exit 1
fi
[[ -s "$HASH_RECORD" ]] || {
    echo "FAIL: deploy-restart did not record the live system unit hash" >&2
    exit 1
}

current_release_before_reload_required="$(readlink -f "$DEPLOY_ROOT/current")"
: >"$LOGFILE"

if PATH="$MOCKDIR:/usr/bin:/bin" \
    TICKET_BOARD_SERVICE_TEST_LOG="$LOGFILE" \
    TICKET_BOARD_SYSTEM_UNIT_PATH="$SYSTEM_UNIT_PATH" \
    TICKET_BOARD_SYSTEM_UNIT_HASH_RECORD="$HASH_RECORD" \
    TICKET_BOARD_SKIP_POST_DEPLOY_SOCKET_VERIFY=1 \
    TICKET_BOARD_TEST_NEED_DAEMON_RELOAD=yes \
    TICKET_BOARD_POLKIT_APPROVAL_USER=user \
    BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
        "$REPO_ROOT/scripts/ticket-board-service.sh" deploy-restart >"$TMPDIR_T/reload-required.out" 2>"$TMPDIR_T/reload-required.err"; then
    echo "FAIL: deploy-restart should fail loudly when systemd reports NeedDaemonReload=yes" >&2
    exit 1
fi
grep -q 'NeedDaemonReload=yes' "$TMPDIR_T/reload-required.err" || {
    echo "FAIL: daemon-reload-required failure did not explain the systemd state" >&2
    cat "$TMPDIR_T/reload-required.err" >&2
    exit 1
}
if grep -q '^system:daemon-reload$' "$LOGFILE"; then
    echo "FAIL: deploy-restart must not attempt daemon-reload outside the board-unit polkit grant" >&2
    cat "$LOGFILE" >&2
    exit 1
fi
if grep -q '^system:restart pgu-ticket-board.service$' "$LOGFILE"; then
    echo "FAIL: deploy-restart should not restart while daemon-reload is required" >&2
    cat "$LOGFILE" >&2
    exit 1
fi
if grep -q '^system:start pgu-ticket-board-canary.service$' "$LOGFILE"; then
    echo "FAIL: deploy-restart should fail before canary when daemon-reload is required" >&2
    cat "$LOGFILE" >&2
    exit 1
fi
[[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$current_release_before_reload_required" ]] || {
    echo "FAIL: deploy-restart should not change current when daemon-reload is required" >&2
    exit 1
}

candidate_unit="$DEPLOY_ROOT/current/deploy/systemd/pgu-ticket-board.service.boardsvc"
candidate_unit_resolved="$(readlink -f "$candidate_unit")"
sed \
    -e 's/^Environment=TICKET_BOARD_PROJECT=pgu$/Environment=PGU_TICKET_BOARD_PROJECT=pgu/' \
    -e 's|^Environment=TICKET_BOARD_SOCKET=/run/pgu-ticket-board/ticket-board.sock$|Environment=PGU_TICKET_BOARD_SOCKET=/run/pgu-ticket-board/ticket-board.sock|' \
    "$candidate_unit" >"$SYSTEM_UNIT_PATH"
sha256sum "$SYSTEM_UNIT_PATH" | awk '{print $1}' >"$HASH_RECORD"
: >"$LOGFILE"

if PATH="$MOCKDIR:/usr/bin:/bin" \
    TICKET_BOARD_SERVICE_TEST_LOG="$LOGFILE" \
    TICKET_BOARD_SYSTEM_UNIT_PATH="$SYSTEM_UNIT_PATH" \
    TICKET_BOARD_SYSTEM_UNIT_HASH_RECORD="$HASH_RECORD" \
    TICKET_BOARD_SKIP_POST_DEPLOY_SOCKET_VERIFY=1 \
    TICKET_BOARD_POLKIT_APPROVAL_USER=user \
    BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
        "$REPO_ROOT/scripts/ticket-board-service.sh" deploy-restart >"$TMPDIR_T/unit-changed.out" 2>"$TMPDIR_T/unit-changed.err"; then
    echo "FAIL: deploy-restart should fail loudly when the candidate system unit has Environment-only drift" >&2
    exit 1
fi
grep -q 'candidate system unit .* differs from installed' "$TMPDIR_T/unit-changed.err" || {
    echo "FAIL: changed-unit failure did not explain that daemon-reload is operator-owned" >&2
    cat "$TMPDIR_T/unit-changed.err" >&2
    exit 1
}
grep -Fq "candidate system unit path: $candidate_unit_resolved" "$TMPDIR_T/unit-changed.err" || {
    echo "FAIL: changed-unit failure did not name the actual candidate unit path" >&2
    cat "$TMPDIR_T/unit-changed.err" >&2
    exit 1
}
grep -Fq "installed system unit path: $SYSTEM_UNIT_PATH" "$TMPDIR_T/unit-changed.err" || {
    echo "FAIL: changed-unit failure did not name the installed unit path" >&2
    cat "$TMPDIR_T/unit-changed.err" >&2
    exit 1
}
grep -q 'system unit drift classification: Environment-only drift' "$TMPDIR_T/unit-changed.err" || {
    echo "FAIL: Environment-only drift was not classified distinctly" >&2
    cat "$TMPDIR_T/unit-changed.err" >&2
    exit 1
}
grep -q -- '-Environment=PGU_TICKET_BOARD_SOCKET=/run/pgu-ticket-board/ticket-board.sock' "$TMPDIR_T/unit-changed.err" || {
    echo "FAIL: Environment-only drift diff did not show the installed socket line" >&2
    cat "$TMPDIR_T/unit-changed.err" >&2
    exit 1
}
grep -q -- '+Environment=TICKET_BOARD_SOCKET=/run/pgu-ticket-board/ticket-board.sock' "$TMPDIR_T/unit-changed.err" || {
    echo "FAIL: Environment-only drift diff did not show the candidate socket line" >&2
    cat "$TMPDIR_T/unit-changed.err" >&2
    exit 1
}
grep -q -- '-Environment=PGU_TICKET_BOARD_PROJECT=pgu' "$TMPDIR_T/unit-changed.err" || {
    echo "FAIL: Environment-only drift diff did not show the installed project line" >&2
    cat "$TMPDIR_T/unit-changed.err" >&2
    exit 1
}
grep -q -- '+Environment=TICKET_BOARD_PROJECT=pgu' "$TMPDIR_T/unit-changed.err" || {
    echo "FAIL: Environment-only drift diff did not show the candidate project line" >&2
    cat "$TMPDIR_T/unit-changed.err" >&2
    exit 1
}
grep -q '^Environment=PGU_TICKET_BOARD_SOCKET=/run/pgu-ticket-board/ticket-board.sock$' "$SYSTEM_UNIT_PATH" || {
    echo "FAIL: deploy-restart should not install a changed unit it cannot reload" >&2
    cat "$SYSTEM_UNIT_PATH" >&2
    exit 1
}
if grep -q '^system:daemon-reload$' "$LOGFILE"; then
    echo "FAIL: changed-unit deploy-restart must not attempt daemon-reload" >&2
    cat "$LOGFILE" >&2
    exit 1
fi
if grep -q '^system:restart pgu-ticket-board.service$' "$LOGFILE"; then
    echo "FAIL: changed-unit deploy-restart should not restart with a stale systemd definition" >&2
    cat "$LOGFILE" >&2
    exit 1
fi
if grep -q '^system:start pgu-ticket-board-canary.service$' "$LOGFILE"; then
    echo "FAIL: changed-unit deploy-restart should fail before canary" >&2
    cat "$LOGFILE" >&2
    exit 1
fi

sed -e 's/^User=boardsvc$/User=root/' "$candidate_unit" >"$SYSTEM_UNIT_PATH"
sha256sum "$SYSTEM_UNIT_PATH" | awk '{print $1}' >"$HASH_RECORD"
: >"$LOGFILE"

if PATH="$MOCKDIR:/usr/bin:/bin" \
    TICKET_BOARD_SERVICE_TEST_LOG="$LOGFILE" \
    TICKET_BOARD_SYSTEM_UNIT_PATH="$SYSTEM_UNIT_PATH" \
    TICKET_BOARD_SYSTEM_UNIT_HASH_RECORD="$HASH_RECORD" \
    TICKET_BOARD_SKIP_POST_DEPLOY_SOCKET_VERIFY=1 \
    TICKET_BOARD_POLKIT_APPROVAL_USER=user \
    BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
        "$REPO_ROOT/scripts/ticket-board-service.sh" deploy-restart >"$TMPDIR_T/user-changed.out" 2>"$TMPDIR_T/user-changed.err"; then
    echo "FAIL: deploy-restart should fail loudly when the candidate system unit has User= drift" >&2
    exit 1
fi
grep -q 'system unit drift classification: sensitive unit drift: User' "$TMPDIR_T/user-changed.err" || {
    echo "FAIL: User= drift was not called out distinctly" >&2
    cat "$TMPDIR_T/user-changed.err" >&2
    exit 1
}
grep -q -- '-User=root' "$TMPDIR_T/user-changed.err" || {
    echo "FAIL: User= drift diff did not show the installed User line" >&2
    cat "$TMPDIR_T/user-changed.err" >&2
    exit 1
}
grep -q -- '+User=boardsvc' "$TMPDIR_T/user-changed.err" || {
    echo "FAIL: User= drift diff did not show the candidate User line" >&2
    cat "$TMPDIR_T/user-changed.err" >&2
    exit 1
}
grep -q '^User=root$' "$SYSTEM_UNIT_PATH" || {
    echo "FAIL: deploy-restart should not install a User= drift unit it cannot reload" >&2
    cat "$SYSTEM_UNIT_PATH" >&2
    exit 1
}
if grep -q '^system:daemon-reload$' "$LOGFILE"; then
    echo "FAIL: User= drift deploy-restart must not attempt daemon-reload" >&2
    cat "$LOGFILE" >&2
    exit 1
fi
if grep -q '^system:restart pgu-ticket-board.service$' "$LOGFILE"; then
    echo "FAIL: User= drift deploy-restart should not restart with a stale systemd definition" >&2
    cat "$LOGFILE" >&2
    exit 1
fi
if grep -q '^system:start pgu-ticket-board-canary.service$' "$LOGFILE"; then
    echo "FAIL: User= drift deploy-restart should fail before canary" >&2
    cat "$LOGFILE" >&2
    exit 1
fi

missing_candidate_path_resolved="$(dirname "$candidate_unit_resolved")/otto-ticket-board.service.boardsvc"
cp "$candidate_unit" "$SYSTEM_UNIT_PATH"
sha256sum "$SYSTEM_UNIT_PATH" | awk '{print $1}' >"$HASH_RECORD"
: >"$LOGFILE"

if PATH="$MOCKDIR:/usr/bin:/bin" \
    TICKET_BOARD_SERVICE_TEST_LOG="$LOGFILE" \
    TICKET_BOARD_SYSTEM_UNIT_PATH="$SYSTEM_UNIT_PATH" \
    TICKET_BOARD_SYSTEM_UNIT_HASH_RECORD="$HASH_RECORD" \
    TICKET_BOARD_SKIP_POST_DEPLOY_SOCKET_VERIFY=1 \
    TICKET_BOARD_POLKIT_APPROVAL_USER=user \
    TICKET_BOARD_PROJECT=otto \
    BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
        "$REPO_ROOT/scripts/ticket-board-service.sh" deploy-restart >"$TMPDIR_T/missing-candidate.out" 2>"$TMPDIR_T/missing-candidate.err"; then
    echo "FAIL: deploy-restart should fail loudly when a tenant release has no production unit" >&2
    exit 1
fi
grep -Fq "release contains no production system unit at expected path: $missing_candidate_path_resolved" "$TMPDIR_T/missing-candidate.err" || {
    echo "FAIL: missing production unit failure did not name the expected path" >&2
    cat "$TMPDIR_T/missing-candidate.err" >&2
    exit 1
}
grep -q 'generic render below is diagnostic only and must not be installed' "$TMPDIR_T/missing-candidate.err" || {
    echo "FAIL: missing production unit failure did not mark the generic render as diagnostic-only" >&2
    cat "$TMPDIR_T/missing-candidate.err" >&2
    exit 1
}
if grep -q 'candidate system unit path:' "$TMPDIR_T/missing-candidate.err"; then
    echo "FAIL: missing production unit fallback should not label the generic render as a candidate" >&2
    cat "$TMPDIR_T/missing-candidate.err" >&2
    exit 1
fi
if grep -q 'install the candidate unit from /tmp' "$TMPDIR_T/missing-candidate.err"; then
    echo "FAIL: missing production unit fallback must not instruct operators to install a temporary render" >&2
    cat "$TMPDIR_T/missing-candidate.err" >&2
    exit 1
fi
grep -q 'system unit drift classification: sensitive unit drift: ExecStart, ExecStartPre, RuntimeDirectory, User, WorkingDirectory' "$TMPDIR_T/missing-candidate.err" || {
    echo "FAIL: missing production unit classification should be a clean comma-separated sensitive list" >&2
    cat "$TMPDIR_T/missing-candidate.err" >&2
    exit 1
}
grep -q -- '-User=boardsvc' "$TMPDIR_T/missing-candidate.err" || {
    echo "FAIL: missing production unit diff did not show installed production-only User line" >&2
    cat "$TMPDIR_T/missing-candidate.err" >&2
    exit 1
}
if grep -q '^system:daemon-reload$' "$LOGFILE"; then
    echo "FAIL: missing production unit deploy-restart must not attempt daemon-reload" >&2
    cat "$LOGFILE" >&2
    exit 1
fi
if grep -q '^system:restart otto-ticket-board.service$' "$LOGFILE"; then
    echo "FAIL: missing production unit deploy-restart should not restart with a generic rendered unit" >&2
    cat "$LOGFILE" >&2
    exit 1
fi

echo "ticket_board_service_deploy_test: ok"
