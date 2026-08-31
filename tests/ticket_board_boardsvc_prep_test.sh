#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/ticket-board-boardsvc-setup.sh"
UNIT="$REPO_ROOT/deploy/systemd/pgu-ticket-board.service.boardsvc"
CANARY_UNIT="$REPO_ROOT/deploy/systemd/pgu-ticket-board-canary.service"
TMPFILES="$REPO_ROOT/deploy/tmpfiles/pgu-ticket-board.conf"
POLKIT_RULE="$REPO_ROOT/deploy/polkit/49-pgu-board-deploy.rules"
RUNBOOK="$REPO_ROOT/docs/ticket-board-boardsvc-peer-auth-runbook.md"
TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

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
grep -q 'setfacl -m "u:\$SERVICE_USER:--x" "\$asset_parent"' "$SCRIPT" || {
    echo "FAIL: setup script does not grant traverse access to the asset parent directory" >&2
    exit 1
}
grep -q 'grant_write_acl "\$FRAME_ROOT"' "$SCRIPT" || {
    echo "FAIL: setup script does not grant shared frame directory write access" >&2
    exit 1
}
grep -q '49-pgu-board-deploy.rules' "$SCRIPT" || {
    echo "FAIL: setup command plan does not install the board deploy polkit rule" >&2
    exit 1
}
"$SCRIPT" --print-root-commands >"$TMPDIR_T/root-commands.out"
grep -q '^set -euo pipefail$' "$TMPDIR_T/root-commands.out" || {
    echo "FAIL: printed root commands must stop on the first failed step" >&2
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
grep -q '^RuntimeDirectory=pgu-ticket-board$' "$UNIT" || {
    echo "FAIL: proposed unit does not request a service-owned runtime directory" >&2
    exit 1
}
grep -q -- '--unix-socket /run/pgu-ticket-board/ticket-board.sock' "$UNIT" || {
    echo "FAIL: proposed unit does not bind the pane socket in the runtime directory" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_SOCKET=/run/pgu-ticket-board/ticket-board.sock$' "$UNIT" || {
    echo "FAIL: proposed unit does not export the runtime socket path" >&2
    exit 1
}
grep -q '^EnvironmentFile=-/home/agent/.config/pgu/ticket-board.env$' "$UNIT" || {
    echo "FAIL: proposed unit does not load the tenant report token env file" >&2
    exit 1
}
grep -q '^Environment=PGUSER=ticket_board_service$' "$UNIT" || {
    echo "FAIL: proposed unit does not select ticket_board_service" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_DATABASE_URL=postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_service$' "$UNIT" || {
    echo "FAIL: proposed unit does not bake the service database URL" >&2
    exit 1
}
grep -q '^Environment=HOME=/home/agent$' "$UNIT" || {
    echo "FAIL: proposed unit does not set a stable HOME for remaining expanduser paths" >&2
    exit 1
}
if grep -q '^ExecStartPre=/bin/mkdir -p /tmp/pgu-frames$' "$UNIT"; then
    echo "FAIL: proposed sandboxed unit must not pretend ExecStartPre can create ReadWritePaths targets" >&2
    exit 1
fi
if grep -q '^ExecStartPre=/bin/chmod 1777 /tmp/pgu-frames$' "$UNIT"; then
    echo "FAIL: proposed sandboxed unit must not carry dead frame-directory chmod pre-start hooks" >&2
    exit 1
fi
retired_backend_flag='--store''-backend'
if grep -q -- "$retired_backend_flag" "$UNIT"; then
    echo "FAIL: proposed unit should not pass the removed backend selector option" >&2
    exit 1
fi
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
[[ -f "$TMPFILES" ]] || {
    echo "FAIL: tmpfiles definition is missing" >&2
    exit 1
}
grep -q '^d /tmp/pgu-frames 1777 root root -$' "$TMPFILES" || {
    echo "FAIL: tmpfiles definition does not recreate the shared frame directory on boot" >&2
    exit 1
}
[[ -f "$POLKIT_RULE" ]] || {
    echo "FAIL: board deploy polkit rule is missing" >&2
    exit 1
}
grep -q 'unit === "pgu-ticket-board-canary.service"' "$POLKIT_RULE" || {
    echo "FAIL: board deploy polkit rule does not cover the fixed deploy canary unit" >&2
    exit 1
}
if grep -q 'pgu-ticket-board-canary-' "$POLKIT_RULE"; then
    echo "FAIL: board deploy polkit rule must not cover caller-defined transient canary units" >&2
    exit 1
fi
[[ -f "$CANARY_UNIT" ]] || {
    echo "FAIL: fixed deploy canary systemd unit is missing" >&2
    exit 1
}
grep -q '^User=boardsvc$' "$CANARY_UNIT" || {
    echo "FAIL: fixed canary unit does not run as boardsvc" >&2
    exit 1
}
grep -q '^EnvironmentFile=/home/agent/pgu-ticketboard-live/canary.env$' "$CANARY_UNIT" || {
    echo "FAIL: fixed canary unit does not read the deploy-written environment file" >&2
    exit 1
}
grep -q '^ReadWritePaths=/tmp$' "$CANARY_UNIT" || {
    echo "FAIL: fixed canary unit does not limit writable paths to /tmp" >&2
    exit 1
}
grep -q '^NoNewPrivileges=true$' "$CANARY_UNIT" || {
    echo "FAIL: fixed canary unit does not pin NoNewPrivileges" >&2
    exit 1
}
if grep -Eq '^\[Install\]$|^WantedBy=' "$CANARY_UNIT"; then
    echo "FAIL: fixed canary unit must not be enable-able at boot" >&2
    exit 1
fi
if grep -Eq '%i|systemd-run|pgu-ticket-board-canary-' "$CANARY_UNIT"; then
    echo "FAIL: fixed canary unit must not interpolate instance names or use transient units" >&2
    exit 1
fi

grep -q 'parser.add_argument("--assets"' "$REPO_ROOT/scripts/ticket_board/cli.py" || {
    echo "FAIL: CLI does not expose an explicit assets directory" >&2
    exit 1
}
HOME=/nonexistent python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.ticket_board.app import TicketBoardApp

with TemporaryDirectory(prefix="boardsvc-path-resolution.") as tmp:
    root = Path(tmp)
    frames = root / "frames"
    assets = root / "assets"
    app = TicketBoardApp(frames, assets, database_url="")
    assert str(app.frame_dir) == str(frames.resolve()), app.frame_dir
    assert str(app.asset_dir) == str(assets.resolve()), app.asset_dir
    assert "/nonexistent" not in str(app.frame_dir)
    assert "/nonexistent" not in str(app.asset_dir)
    assert getattr(app, "store_backend") == "postgres"
PY
grep -q 'PGU-197 is a prep-only package' "$RUNBOOK" || {
    echo "FAIL: runbook does not mark the package prep-only" >&2
    exit 1
}
grep -q 'This package targets the PGU-198/PGU-209 single-writer board' "$RUNBOOK" || {
    echo "FAIL: runbook does not document DB-backed ticket-store boundary" >&2
    exit 1
}
grep -q 'tmpfiles' "$RUNBOOK" || {
    echo "FAIL: runbook does not document tmpfiles-based frame directory recreation" >&2
    exit 1
}
grep -q '49-pgu-board-deploy.rules' "$RUNBOOK" || {
    echo "FAIL: runbook does not document the board deploy polkit rule" >&2
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
grep -q 'data\["store_backend"\] == "postgres"' "$RUNBOOK" || {
    echo "FAIL: runbook does not verify the board is using the Postgres backend" >&2
    exit 1
}
grep -q 'Roll Back' "$RUNBOOK" || {
    echo "FAIL: runbook lacks rollback steps" >&2
    exit 1
}

echo "ticket_board_boardsvc_prep_test: ok"
