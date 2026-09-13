#!/usr/bin/env python3
"""SYRD-127: the deployer has to be able to READ what it is told to compare.

On SYRD-126 the third operator attempt installed the reviewed units, ran
daemon-reload, and was still refused: the release sequence handed the deployer
/etc/switchyard/provision/syrd/syrd-ticket-board.service, the deployer runs as
the project account through runuser, and that directory is root-only. `[[ -f ]]`
on an unreadable path is false, so a present, correct, byte-identical unit read
as ABSENT and the release was rolled back.

The answer is not to open root's directory. Root publishes a COPY of the
reviewed unit beside the other root-owned bytes a role account is meant to read,
and the handoff names the copy -- which keeps the comparison real. Pointing the
handoff at the INSTALLED unit would also be readable and would make the gate
vacuous: the two sides would be the same file, `cmp` could never differ, and a
release whose unit genuinely differs would sail through the one check written to
stop it. That was done once as a stopgap, so it is refused explicitly here.

The gate is bash, so it is driven as bash: the real function, with only the two
facts it cannot compute in a test -- where the installed unit is, and what
systemd says about reloading -- supplied by the harness.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

SERVICE_SCRIPT = ROOT / "scripts" / "ticket-board-service.sh"
PROJECT = "otto"

REVIEWED_UNIT = """[Unit]
Description=otto Ticket Board

[Service]
User=boardsvc
Group=otto-roles
SupplementaryGroups=otto-roles
NoNewPrivileges=true
ProtectSystem=full
ExecStart=/usr/bin/python3 /srv/otto/current/scripts/ticket-board.py --port 8873
Environment=TICKET_BOARD_PROJECT=otto

[Install]
WantedBy=multi-user.target
"""

#: The same unit with one sensitive directive changed -- the shape of drift the
#: gate exists to catch, rather than a cosmetic difference.
DRIFTED_UNIT = REVIEWED_UNIT.replace("User=boardsvc", "User=root")
#: And one where only the environment moved, which is classified differently.
ENVIRONMENT_DRIFT = REVIEWED_UNIT.replace(
    "Environment=TICKET_BOARD_PROJECT=otto", "Environment=TICKET_BOARD_PROJECT=otto-two"
)

HARNESS = r"""
set -uo pipefail
source "$SERVICE_SCRIPT"

# The two facts a test cannot compute: where systemd's copy is, and what systemd
# says about it. Everything else in the gate is the real function.
system_unit_file_path() { printf '%s\n' "$INSTALLED_UNIT"; }
system_unit_needs_daemon_reload() { [[ "$NEEDS_RELOAD" == yes ]]; }
log() { printf '%s\n' "$*"; }

assert_system_unit_reload_not_required_for_release "$RELEASE_DIR"
printf 'GATE PASSED\n'
"""


def run_gate(
    *,
    candidate: Path | str,
    installed: Path,
    release_dir: Path,
    needs_reload: bool = False,
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "SERVICE_SCRIPT": str(SERVICE_SCRIPT),
        "INSTALLED_UNIT": str(installed),
        "NEEDS_RELOAD": "yes" if needs_reload else "no",
        "RELEASE_DIR": str(release_dir),
        "TICKET_BOARD_PROVISIONED_SYSTEM_UNIT": str(candidate),
        "TICKET_BOARD_PROJECT": PROJECT,
        "PROJECT_SLUG": PROJECT,
    }
    return subprocess.run(
        ["bash", "-c", HARNESS], text=True, capture_output=True, env=environment
    )


def make_tree(tmp: Path) -> tuple[Path, Path, Path]:
    """A root-only provisioning directory, a readable copy, and the installed unit."""
    provision = tmp / "etc-switchyard" / "provision" / PROJECT
    provision.mkdir(parents=True)
    provisioned = provision / f"{PROJECT}-ticket-board.service"
    provisioned.write_text(REVIEWED_UNIT, encoding="utf-8")

    staging = tmp / "usr-local" / "lib" / "switchyard" / PROJECT
    staging.mkdir(parents=True)
    readable = staging / f"{PROJECT}-ticket-board.service"
    readable.write_text(REVIEWED_UNIT, encoding="utf-8")

    installed_dir = tmp / "etc" / "systemd" / "system"
    installed_dir.mkdir(parents=True)
    installed = installed_dir / f"{PROJECT}-ticket-board.service"
    installed.write_text(REVIEWED_UNIT, encoding="utf-8")

    # The live shape: root's own directory, readable by nobody else. As a
    # non-root account this test cannot traverse it either, which is exactly the
    # condition that produced the refusal.
    provision.chmod(0o700 if os.geteuid() == 0 else 0o000)
    return provisioned, readable, installed


def main() -> int:
    checks = 0
    assert os.geteuid() != 0, "this case is about what a NON-root deployer can read"
    with tempfile.TemporaryDirectory(prefix="syrd127-") as raw:
        tmp = Path(raw)
        release_dir = tmp / "release"
        release_dir.mkdir()
        provisioned, readable, installed = make_tree(tmp)
        try:
            # 1. THE REPORTED FAILURE. Root's own path, from an account that
            #    cannot traverse the directory: the unit is right there and
            #    byte-identical, and the gate refuses the release anyway.
            refused = run_gate(candidate=provisioned, installed=installed, release_dir=release_dir)
            assert refused.returncode != 0, refused.stdout
            assert "contains no production system unit" in refused.stdout + refused.stderr, refused
            assert provisioned.name in refused.stdout + refused.stderr, refused
            checks += 1

            # 2. THE READABLE COPY, same bytes, same tenant, same moment: the
            #    operator installed the reviewed unit and reloaded, so the gate
            #    passes and the deploy proceeds.
            passed = run_gate(candidate=readable, installed=installed, release_dir=release_dir)
            assert passed.returncode == 0, passed.stdout + passed.stderr
            assert "GATE PASSED" in passed.stdout, passed.stdout
            checks += 1

            # 3. AND IT IS NOT VACUOUS. Genuine drift in a sensitive directive
            #    is still refused, named, and classified.
            installed.write_text(DRIFTED_UNIT, encoding="utf-8")
            drifted = run_gate(candidate=readable, installed=installed, release_dir=release_dir)
            output = drifted.stdout + drifted.stderr
            assert drifted.returncode != 0, drifted.stdout
            assert "sensitive unit drift" in output, output
            assert "User" in output, output
            assert "daemon-reload" in output, output
            checks += 1

            # Environment-only drift is refused too, and says which kind it is:
            # the classification is what tells an operator how alarmed to be.
            installed.write_text(ENVIRONMENT_DRIFT, encoding="utf-8")
            environment_drift = run_gate(
                candidate=readable, installed=installed, release_dir=release_dir
            )
            output = environment_drift.stdout + environment_drift.stderr
            assert environment_drift.returncode != 0, environment_drift.stdout
            assert "Environment-only drift" in output, output
            checks += 1

            # 4. NEEDDAEMONRELOAD. Units match, systemd has not reloaded, and
            #    reloading is deliberately outside the board's polkit grant.
            installed.write_text(REVIEWED_UNIT, encoding="utf-8")
            stale = run_gate(
                candidate=readable, installed=installed, release_dir=release_dir, needs_reload=True
            )
            output = stale.stdout + stale.stderr
            assert stale.returncode != 0, stale.stdout
            assert "NeedDaemonReload=yes" in output, output
            checks += 1

            # 5. THE STOPGAP, REFUSED. Handing the deployer the installed unit
            #    is readable and proves nothing: both sides would be one file.
            vacuous = run_gate(candidate=installed, installed=installed, release_dir=release_dir)
            output = vacuous.stdout + vacuous.stderr
            assert vacuous.returncode != 0, vacuous.stdout
            assert "comparing them proves nothing" in output, output
            checks += 1

            # ... including by a path that only resolves to the same file.
            link = tmp / "etc" / "systemd" / "system" / "linked.service"
            link.symlink_to(installed)
            through_link = run_gate(candidate=link, installed=installed, release_dir=release_dir)
            output = through_link.stdout + through_link.stderr
            assert through_link.returncode != 0, through_link.stdout
            assert "comparing them proves nothing" in output, output
            checks += 1

            # 6. A MISSING COPY still refuses rather than falling back to the
            #    generic render: an unreviewed unit must never be installed.
            readable.unlink()
            absent = run_gate(candidate=readable, installed=installed, release_dir=release_dir)
            output = absent.stdout + absent.stderr
            assert absent.returncode != 0, absent.stdout
            assert "contains no production system unit" in output, output
            assert "diagnostic only and must not be installed" in output, output
            checks += 1
        finally:
            # Leave the tree removable.
            provisioned.parent.chmod(0o700)

    print(f"readable_system_unit_proof_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
