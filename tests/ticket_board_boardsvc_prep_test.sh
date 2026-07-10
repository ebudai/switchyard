#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/ticket-board-boardsvc-setup.sh"
UNIT="$REPO_ROOT/deploy/systemd/pgu-ticket-board.service.boardsvc"
RUNBOOK="$REPO_ROOT/docs/ticket-board-boardsvc-peer-auth-runbook.md"

[[ -x "$SCRIPT" ]] || {
    echo "FAIL: setup script is missing or not executable" >&2
    exit 1
}
bash -n "$SCRIPT"

grep -q 'useradd -r -M -d /nonexistent -s /usr/sbin/nologin' "$SCRIPT" || {
    echo "FAIL: setup script does not create a non-login system user" >&2
    exit 1
}
grep -q 'peer map=\$PG_IDENT_MAP' "$SCRIPT" || {
    echo "FAIL: setup script does not configure peer auth with an ident map" >&2
    exit 1
}
grep -q '\$PG_IDENT_MAP   \$SERVICE_USER   \$SERVICE_ROLE' "$SCRIPT" || {
    echo "FAIL: setup script does not map boardsvc to ticket_board_service" >&2
    exit 1
}
grep -q 'setfacl -R -m "u:\$SERVICE_USER:rx"' "$SCRIPT" || {
    echo "FAIL: setup script does not grant deploy-tree ACL access" >&2
    exit 1
}
grep -q 'grant_write_acl "\$ASSET_ROOT"' "$SCRIPT" || {
    echo "FAIL: setup script does not grant asset directory write access" >&2
    exit 1
}
grep -q 'grant_write_acl "\$FRAME_ROOT"' "$SCRIPT" || {
    echo "FAIL: setup script does not grant shared frame directory write access" >&2
    exit 1
}
if grep -q 'pgu-tickets"' "$SCRIPT"; then
    echo "FAIL: setup script should not grant boardsvc access to the JSON ticket store" >&2
    exit 1
fi
grep -q 'find "\$path" -type d -exec setfacl -m "d:u:\$SERVICE_USER:rx"' "$SCRIPT" || {
    echo "FAIL: setup script does not set default ACLs only on directories" >&2
    exit 1
}

grep -q '^User=boardsvc$' "$UNIT" || {
    echo "FAIL: proposed unit does not run as boardsvc" >&2
    exit 1
}
grep -q '^Environment=PGUSER=ticket_board_service$' "$UNIT" || {
    echo "FAIL: proposed unit does not select ticket_board_service" >&2
    exit 1
}
grep -q -- '--frames /tmp/pgu-frames --assets /home/agent/.claude/pgu-tickets-assets' "$UNIT" || {
    echo "FAIL: proposed unit does not use explicit shared frames/assets paths" >&2
    exit 1
}
grep -q '^ReadWritePaths=/home/agent/.claude/pgu-tickets-assets /tmp/pgu-frames$' "$UNIT" || {
    echo "FAIL: proposed unit does not limit writable paths to assets and frames" >&2
    exit 1
}
if grep -q '^PrivateTmp=true$' "$UNIT"; then
    echo "FAIL: proposed unit must not isolate /tmp/pgu-frames with PrivateTmp" >&2
    exit 1
fi
if grep -Eq 'PASSWORD|PGPASSWORD|postgresql://[^ ]*:' "$UNIT"; then
    echo "FAIL: proposed unit appears to contain a password-bearing credential" >&2
    exit 1
fi

grep -q 'parser.add_argument("--assets"' "$REPO_ROOT/scripts/ticket_board/cli.py" || {
    echo "FAIL: CLI does not expose an explicit assets directory" >&2
    exit 1
}
grep -q 'PGU-197 is a prep-only package' "$RUNBOOK" || {
    echo "FAIL: runbook does not mark the package prep-only" >&2
    exit 1
}
grep -q 'This package targets the post-cutover DB-backed board' "$RUNBOOK" || {
    echo "FAIL: runbook does not document DB-backed ticket-store boundary" >&2
    exit 1
}
grep -q 'Do not add an ACL for `/home/agent/.claude/pgu-tickets`' "$RUNBOOK" || {
    echo "FAIL: runbook does not document DB-backed ticket-store boundary" >&2
    exit 1
}
grep -q 'Verify' "$RUNBOOK" || {
    echo "FAIL: runbook lacks verification steps" >&2
    exit 1
}
grep -q 'EXPECTED_TICKETS=' "$RUNBOOK" || {
    echo "FAIL: runbook lacks real ticket-count verification" >&2
    exit 1
}
grep -q 'Roll Back' "$RUNBOOK" || {
    echo "FAIL: runbook lacks rollback steps" >&2
    exit 1
}

echo "ticket_board_boardsvc_prep_test: ok"
