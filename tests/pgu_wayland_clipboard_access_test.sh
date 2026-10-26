#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/pgu-wayland-clipboard-access.sh"
RUNBOOK="$REPO_ROOT/docs/pgu-wayland-clipboard-access.md"

[[ -x "$SCRIPT" ]] || {
    echo "FAIL: clipboard access helper must exist and be executable" >&2
    exit 1
}
bash -n "$SCRIPT"

# SYRD-687: a named ACL on the desktop owner's runtime directory makes it 0710,
# which Qt, KDE and the XDG spec refuse. The helper grants nothing any more.
if grep -Eq 'setfacl (-[a-z-]+ )*-m' "$SCRIPT"; then
    echo "FAIL: helper must not add an ACL entry anywhere" >&2
    exit 1
fi
for command in apply-acl install-eric-user-unit; do
    if output="$(PGU_WAYLAND_GUI_USER=nobody-such PGU_WAYLAND_AGENT_USER=nobody-such "$SCRIPT" "$command" 2>&1)"; then
        echo "FAIL: $command must refuse" >&2
        exit 1
    fi
    grep -q 'is retired' <<<"$output" || {
        echo "FAIL: $command must say it is retired: $output" >&2
        exit 1
    }
done
grep -q 'setfacl --no-mask -x "u:${AGENT_USER}" "$path"' "$SCRIPT" || {
    echo "FAIL: retire must take the agent's entry off without recalculating the mask" >&2
    exit 1
}
grep -q 'systemctl --user disable --now "$SERVICE_NAME"' "$SCRIPT" || {
    echo "FAIL: retire must disable the login unit that re-applied the grant" >&2
    exit 1
}
grep -q 'scripts/pgu-wayland-clipboard-access.sh retire' "$RUNBOOK" || {
    echo "FAIL: runbook must document the retire command" >&2
    exit 1
}
grep -q '0710 instead of 0700' "$RUNBOOK" || {
    echo "FAIL: runbook must say why the helper is retired" >&2
    exit 1
}

echo "pgu_wayland_clipboard_access_test: ok"
