#!/usr/bin/env python3
"""SYRD-132: the two defects first production use found in the SYRD-128 journal.

Both are mine, and both only appear when the thing is actually used.

`switchyard rollout-log syrd` crashed with `AttributeError:
'SwitchyardProjectEntry' object has no attribute 'project'`. The registry entry
identifies a tenant by `slug`; I wrote `entry.project`. Every invocation of the
read path died, so the root-owned records could be read only by finding their
directories by hand -- which is most of what the read path was for.

And both records the operator produced through the authorized pkexec path
recorded `operator` as an empty string, because the recorder read only
`SUDO_USER`. Polkit does not set that; it sets `PKEXEC_UID`. So the one field
that says WHO ran a privileged command was blank on every run that went through
polkit.

The rules these fix to, and which this file holds to:

* the read path resolves every registered tenant, from the registry entry
  itself, without loading the tenant's configuration -- reading a record has to
  keep working for a tenant whose configuration does not;
* attribution comes from the mechanism that elevated the process and from
  nowhere else, resolved through the account database;
* an id that names no account records as unknown rather than as itself, and
  nothing is ever invented from `USER` or `LOGNAME`.
"""

from __future__ import annotations

import json
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

from scripts.ticket_board.rollout_journal import (  # noqa: E402
    JOURNAL_ROOT_ENV,
    RESULT_NAME,
    Attempt,
    resolve_operator,
)

RECORDER = ROOT / "scripts" / "switchyard-record-rollout"
REGISTRY_ENV = "SWITCHYARD_PROJECT_REGISTRY_DIR"
TENANTS = ("otto", "porter", "cerulean")


def namespaces_available() -> bool:
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "true"], capture_output=True, text=True
    )
    return probe.returncode == 0


def write_registry(registry: Path) -> None:
    """A registry with several tenants, as a host with several projects has."""
    registry.mkdir(parents=True, exist_ok=True)
    for slug in TENANTS:
        config = registry / f"{slug}-config.json"
        config.write_text(
            json.dumps({"project": slug, "roles": [{"role": "main"}]}), encoding="utf-8"
        )
        (registry / f"{slug}.json").write_text(
            json.dumps(
                {
                    "schema": "switchyard.project-registry.v1",
                    "slug": slug,
                    "name": slug.title(),
                    "config_path": str(config),
                }
            ),
            encoding="utf-8",
        )


def test_the_read_path_resolves_every_registered_tenant() -> None:
    """The reported crash, and the rule it broke.

    Driven through `switchyard_main`, which is where it crashed -- calling
    `rollout_log_command` directly would never have touched the registry entry
    and so would never have failed.
    """
    import scripts.team_launcher as team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd132-registry.") as raw:
        tmp = Path(raw)
        registry = tmp / "registry"
        write_registry(registry)
        journal = tmp / "rollout"
        # One tenant has a record; the others have none. Both have to work: an
        # empty journal is a normal state, not an error.
        recorded = Attempt("otto", ["true"], root=journal)
        directory = recorded.open()
        recorded.close(status="completed", exit_status=0)

        previous = {name: os.environ.get(name) for name in (REGISTRY_ENV, JOURNAL_ROOT_ENV)}
        os.environ[REGISTRY_ENV] = str(registry)
        os.environ[JOURNAL_ROOT_ENV] = str(journal)
        try:
            from contextlib import redirect_stdout
            from io import StringIO

            for slug in TENANTS:
                shown = StringIO()
                with redirect_stdout(shown):
                    exit_status = team_launcher.switchyard_main(["rollout-log", slug])
                # A clean journal, whether it holds one attempt or none.
                assert exit_status == 0, (slug, exit_status, shown.getvalue())
                assert f"{slug} rollout journal" in shown.getvalue(), shown.getvalue()
            # The tenant with a record shows it; the others say so plainly.
            shown = StringIO()
            with redirect_stdout(shown):
                team_launcher.switchyard_main(["rollout-log", "otto"])
            assert "0001-" in shown.getvalue(), shown.getvalue()
            shown = StringIO()
            with redirect_stdout(shown):
                team_launcher.switchyard_main(["rollout-log", "porter"])
            assert "no attempts recorded" in shown.getvalue(), shown.getvalue()
            # And by name as well as by slug, since the registry accepts either.
            shown = StringIO()
            with redirect_stdout(shown):
                assert team_launcher.switchyard_main(["rollout-log", "Otto"]) == 0, shown.getvalue()
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        # The record really was the one displayed, by the path the journal is
        # keyed under: the registry slug.
        assert (Path(directory) / RESULT_NAME).is_file()
        assert Path(directory).parent.name == "otto", directory


def test_attribution_comes_only_from_what_elevated_the_process() -> None:
    """Polkit, sudo, and every way of having nobody to name."""
    me = os.getuid()
    my_name = __import__("pwd").getpwuid(me).pw_name

    polkit = resolve_operator({"PKEXEC_UID": str(me)})
    assert (polkit.name, polkit.uid, polkit.source) == (my_name, me, "pkexec"), polkit

    sudo = resolve_operator({"SUDO_USER": my_name})
    assert (sudo.name, sudo.uid, sudo.source) == (my_name, me, "sudo"), sudo

    # pkexec wins when both are present: it is what elevated this process, and
    # SUDO_USER may be left over from an outer shell.
    both = resolve_operator({"PKEXEC_UID": str(me), "SUDO_USER": "root"})
    assert (both.name, both.source) == (my_name, "pkexec"), both

    # Malformed and unresolvable ids record as unknown, and say which mechanism
    # produced something unusable rather than pretending nothing was offered.
    for value in ("nonsense", "-1", "", "  ", "99999999"):
        unresolved = resolve_operator({"PKEXEC_UID": value})
        assert not unresolved.known, (value, unresolved)
        if value.strip():
            assert unresolved.source == "pkexec:unresolved", (value, unresolved)
    gone = resolve_operator({"SUDO_USER": "no-such-account-syrd132"})
    assert not gone.known and gone.source == "sudo:unresolved", gone

    # Nothing is invented from what any caller can set.
    for environ in ({}, {"USER": "somebody"}, {"LOGNAME": "somebody"}, {"USERNAME": "somebody"}):
        nobody = resolve_operator(environ)
        assert not nobody.known and nobody.source == "", (environ, nobody)


def main() -> int:
    checks = 0
    test_the_read_path_resolves_every_registered_tenant()
    checks += 1
    test_attribution_comes_only_from_what_elevated_the_process()
    checks += 1

    if not namespaces_available():
        print(f"rollout_journal_identity_test: {checks} checks ok (namespaces unavailable)")
        return 0

    with tempfile.TemporaryDirectory(prefix="syrd132-record.") as raw:
        journal = Path(raw) / "rollout"

        def record(environment: dict[str, str]) -> dict:
            done = subprocess.run(
                ["unshare", "--user", "--map-root-user", sys.executable, str(RECORDER),
                 "otto", "--", "sh", "-c", "echo recorded"],
                capture_output=True,
                text=True,
                env={**os.environ, JOURNAL_ROOT_ENV: str(journal), **environment},
            )
            assert done.returncode == 0, done.stdout + done.stderr
            latest = sorted(
                (journal / "otto").glob("[0-9]*-*"), key=lambda path: path.name
            )[-1]
            return {
                "result": json.loads((latest / RESULT_NAME).read_text(encoding="utf-8")),
                "output": done.stdout + done.stderr,
            }

        # THE REPORTED SHAPE: a root run reached through polkit. Inside the
        # namespace this process is uid 0 and the invoking uid is the real one,
        # which is exactly what pkexec leaves behind.
        invoking = os.getuid()
        invoking_name = __import__("pwd").getpwuid(invoking).pw_name
        through_polkit = record({"PKEXEC_UID": str(invoking), "SUDO_USER": ""})
        assert through_polkit["result"]["operator"] == invoking_name, through_polkit["result"]
        assert through_polkit["result"]["operator_uid"] == invoking, through_polkit["result"]
        assert through_polkit["result"]["operator_source"] == "pkexec", through_polkit["result"]
        # And the run says so while it is still running, not only in a file
        # somebody reads afterwards.
        assert f"operator {invoking_name} (via pkexec)" in through_polkit["output"], through_polkit["output"]
        checks += 1

        # Sudo still works, unchanged.
        through_sudo = record({"SUDO_USER": invoking_name, "PKEXEC_UID": ""})
        assert through_sudo["result"]["operator"] == invoking_name, through_sudo["result"]
        assert through_sudo["result"]["operator_source"] == "sudo", through_sudo["result"]
        checks += 1

        # A polkit uid this host cannot resolve records as unknown and says
        # which mechanism offered it -- the blank that started this ticket, but
        # now with the reason attached rather than silent.
        unresolvable = record({"PKEXEC_UID": "99999999", "SUDO_USER": ""})
        assert unresolvable["result"]["operator"] == "", unresolvable["result"]
        assert unresolvable["result"]["operator_source"] == "pkexec:unresolved", unresolvable["result"]
        assert "no operator recorded" in unresolvable["output"], unresolvable["output"]
        checks += 1

        # Neither mechanism: blank, and nothing invented from the environment.
        anonymous = record({"PKEXEC_UID": "", "SUDO_USER": "", "USER": "somebody", "LOGNAME": "somebody"})
        assert anonymous["result"]["operator"] == "", anonymous["result"]
        assert anonymous["result"]["operator_source"] == "", anonymous["result"]
        assert "somebody" not in json.dumps(anonymous["result"]), anonymous["result"]
        checks += 1

        # And SYRD-128's guarantees still hold on these records.
        latest = sorted((journal / "otto").glob("[0-9]*-*"), key=lambda path: path.name)[-1]
        for name in ("stdout.log", "stderr.log", RESULT_NAME):
            assert (latest / name).stat().st_mode & 0o777 == 0o444, name
        checks += 1

    print(f"rollout_journal_identity_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
