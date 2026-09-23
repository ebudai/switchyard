#!/usr/bin/env python3
"""`switchyard status` reads, and reading needs no root.

Live evidence from the restored MEFP Director pane after the legacy cutover:
`switchyard status` still required root. It reports and changes nothing, and
after a cutover the tenant's generated configuration lives in an account the
desktop operator is not -- so the ask became a password prompt for a read, on
the operator's own host (SYRD-241).

Three things escalated, and all three are asserted here: the installed
wrapper's verb classification, the scoped dispatch's owner-or-root crossing,
and the unscoped listing's outright re-exec. What this account may not read is
named as unavailable, with the remedy, beside the root-owned records that are
readable by design.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board.rollout_journal import JOURNAL_ROOT_ENV  # noqa: E402
from standalone_test_runner import run_module_tests  # noqa: E402

ME = pwd.getpwuid(os.geteuid()).pw_name
#: Anything a tenant's own document might hold. A status must never print it.
SECRET = "tenant-board-token-syrd241"


class Host:
    """A registry of tenants, one readable and one closed to this account."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="syrd241."))
        self.registry = self.tmp / "registry"
        self.registry.mkdir()
        self.configs = self.tmp / "configs"
        self.configs.mkdir()
        self.journal_root = self.tmp / "rollout"
        self.journal_root.mkdir()

    def tenant(self, slug: str, *, name: str = "", readable: bool = True,
               malformed: bool = False) -> Path:
        home = self.tmp / "homes" / f"{slug}-owner"
        provision = home / "provision"
        provision.mkdir(parents=True)
        config_path = provision / f"{slug}.json"
        layout = self.tmp / "layout.json"
        if not layout.exists():
            layout.write_text(
                '{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8"
            )
        document = {
            "desktop_access": {"mode": "headless"},
            "project": slug,
            "project_name": name or slug.title(),
            "run_as_user": ME,
            "layout": str(layout),
            "repository": str(home / "repo"),
            "board_url": f"http://127.0.0.1:2333{len(slug)}/",
            "roles": [
                {"role": "director", "slot": 0, "target": f"{slug}-director:0.0",
                 "tmux_session": f"{slug}-director", "cli": ["claude"]},
            ],
        }
        if readable and not malformed:
            config_path.write_text(json.dumps(document), encoding="utf-8")
        else:
            # A document this account is not meant to read, carrying what such
            # a document carries.
            document["notes"] = SECRET
            body = json.dumps(document)
            config_path.write_text(body if not malformed else body[:40] + SECRET, encoding="utf-8")
        (self.registry / f"{slug}.json").write_text(
            json.dumps({"schema": launcher.SWITCHYARD_REGISTRY_SCHEMA, "slug": slug,
                        "name": name or slug.title(), "config_path": str(config_path)}),
            encoding="utf-8",
        )
        if not readable:
            # What a tenant's private home looks like from another account.
            config_path.chmod(0o000)
        return config_path

    def journal(self, slug: str, record: dict) -> Path:
        directory = self.journal_root / slug
        directory.mkdir(parents=True, exist_ok=True)
        index = directory / "index.jsonl"
        with index.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return index

    def status(self, project: str = "", **kwargs) -> tuple[int, str]:
        lines: list[str] = []
        # The journal root through its own documented seam, so the command runs
        # exactly as it ships.
        kwargs.pop("journal_root", None)
        previous = os.environ.get(JOURNAL_ROOT_ENV)
        os.environ[JOURNAL_ROOT_ENV] = str(self.journal_root)
        try:
            code = self._status(project, lines, **kwargs)
        finally:
            if previous is None:
                os.environ.pop(JOURNAL_ROOT_ENV, None)
            else:
                os.environ[JOURNAL_ROOT_ENV] = previous
        return code, "\n".join(lines)

    def _status(self, project: str, lines: list[str], **kwargs) -> int:
        return launcher.switchyard_status_command(
            config_dir=self.configs,
            registry_dir=self.registry,
            project=project,
            owner_tmux_reader=kwargs.pop("owner_tmux_reader", lambda _c: (set(), "")),
            assignments_reader=kwargs.pop("assignments_reader", lambda _c: ({}, "")),
            source_repo=self.tmp / "missing-source",
            switchyard_install_path=self.tmp / "missing-switchyard",
            print_func=lines.append,
            **kwargs,
        )

    def close(self) -> None:
        for path in self.tmp.rglob("*"):
            if path.is_file():
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)


def test_the_installed_wrapper_asks_for_no_root_to_read() -> None:
    """The live symptom: the trampoline classified `status` as privileged."""
    for argv in (["status"], ["status", "mefp"], ["status", "--json"]):
        assert not launcher.switchyard_invocation_requires_root(argv), argv
    assert "status" in launcher.SWITCHYARD_UNPRIVILEGED_COMMANDS
    # And the verbs that really do change the host still say so.
    for argv in (["upgrade", "mefp"], ["new"], ["teardown", "mefp"]):
        assert launcher.switchyard_invocation_requires_root(argv), argv


class _NeverEscalates:
    """Fails the case if anything on the status path reaches for root."""

    def __init__(self) -> None:
        self.calls: list = []

    def __enter__(self):
        self.previous = (
            launcher._switchyard_exec_with_root,
            launcher._switchyard_cross_account,
        )

        def refuse(*args, **kwargs):
            self.calls.append(args)
            raise AssertionError(f"status escalated: {args!r}")

        launcher._switchyard_exec_with_root = refuse
        launcher._switchyard_cross_account = refuse
        return self

    def __exit__(self, *exc):
        (launcher._switchyard_exec_with_root, launcher._switchyard_cross_account) = self.previous


def _dispatch(host: Host, argv: list[str]) -> int:
    """The real CLI dispatch, with this fixture's registry."""
    previous = os.environ.get(launcher.SWITCHYARD_REGISTRY_DIR_ENV)
    os.environ[launcher.SWITCHYARD_REGISTRY_DIR_ENV] = str(host.registry)
    try:
        with _NeverEscalates():
            return launcher.switchyard_main(argv)
    finally:
        if previous is None:
            os.environ.pop(launcher.SWITCHYARD_REGISTRY_DIR_ENV, None)
        else:
            os.environ[launcher.SWITCHYARD_REGISTRY_DIR_ENV] = previous


def test_the_unscoped_listing_never_re_execs_under_root() -> None:
    host = Host()
    try:
        host.tenant("porter")
        assert _dispatch(host, ["status"]) == 0
    finally:
        host.close()


def test_a_scoped_status_never_crosses_to_the_owner() -> None:
    """Scoped status used to load the config, which crosses accounts to do it."""
    host = Host()
    try:
        host.tenant("porter", readable=False)
        assert _dispatch(host, ["status", "porter"]) == 0
    finally:
        host.close()


def test_a_current_tenant_is_reported_as_before() -> None:
    host = Host()
    try:
        host.tenant("porter", name="Porter")
        code, said = host.status()
        assert code == 0, said
        assert "Porter" in said and "porter" in said, said
        assert "unavailable" not in said, said
    finally:
        host.close()


def test_an_upgraded_legacy_tenant_is_reported_from_root_owned_records() -> None:
    """Its configuration moved into an account this one is not."""
    host = Host()
    try:
        host.tenant("mefp", name="Morfane", readable=False)
        host.journal("mefp", {"attempt": "0023-20260923T024729Z", "at": "2026-09-23T02:47:29Z",
                              "status": "ok", "event": "finish",
                              "detail": "tenant-control start through the bridge (exit 0)"})
        code, said = host.status(
            runner=lambda args, **kw: subprocess.CompletedProcess(args, 0),
            journal_root=host.journal_root,
        )
        assert code == 0, said
        assert "mefp" in said, said
        assert "pane and viewer state is unavailable" in said, said
        assert "from root-owned records" in said, said
        assert "0023-20260923T024729Z" in said, said
        assert "board service active" in said, said
        assert f"to see the rest, run `switchyard status mefp`" in said, said
        assert "sudo switchyard status mefp" in said, said
        assert SECRET not in said, said
    finally:
        host.close()


def test_a_malformed_configuration_is_named_without_quoting_it() -> None:
    """Readable and unusable is still one row, and still no file contents."""
    host = Host()
    try:
        host.tenant("broken", malformed=True)
        code, said = host.status(journal_root=host.journal_root)
        assert code == 0, said
        assert "broken" in said, said
        assert "unavailable" in said, said
        assert SECRET not in said, said
    finally:
        host.close()


def test_an_inaccessible_optional_detail_is_named_and_not_invented() -> None:
    """No journal, and a systemctl that cannot answer: still a status, still exit 0."""
    host = Host()
    try:
        host.tenant("mefp", readable=False)

        def runner(args, **kwargs):
            raise OSError("systemctl is not on this host")

        code, said = host.status(runner=runner, journal_root=host.journal_root)
        assert code == 0, said
        assert "pane and viewer state is unavailable" in said, said
        assert "to see the rest" in said, said
        # Nothing claimed about what could not be read.
        assert "board service" not in said, said
        assert "last rollout" not in said, said
    finally:
        host.close()


def test_a_caller_with_no_tenant_authority_still_gets_the_listing() -> None:
    """A tenant whose whole directory is closed: one row, not a failed command."""
    host = Host()
    try:
        config_path = host.tenant("closed", readable=False)
        config_path.parent.chmod(0o000)
        try:
            code, said = host.status(journal_root=host.journal_root)
            assert code == 0, said
            assert "closed" in said, said
            assert "unavailable" in said, said
            assert SECRET not in said, said
        finally:
            config_path.parent.chmod(0o755)
    finally:
        host.close()


def test_a_release_detail_that_cannot_be_derived_is_one_unavailable_row() -> None:
    """An optional field, not the command: the status must survive it."""
    host = Host()
    try:
        host.tenant("porter")
        config = launcher.load_project_config("porter", host.registry.parent / "homes"
                                              / "porter-owner" / "provision" / "porter.json")
        original = launcher.tenant_release_status

        def explode(*_a, **_k):
            raise SystemExit("switchyard: cannot determine tenant owner for porter")

        launcher.tenant_release_status = explode
        try:
            row = launcher._runtime_release_copy_status(
                config, source_repo=host.tmp, runner=lambda *a, **k: None
            )
        finally:
            launcher.tenant_release_status = original
        assert row is not None
        assert row.status.startswith("unavailable: "), row.status
        assert "cannot determine tenant owner" in row.status, row.status
    finally:
        host.close()


def test_the_root_owned_facts_are_read_from_root_owned_places_only() -> None:
    host = Host()
    try:
        host.journal("mefp", {"attempt": "0001", "at": "2026-09-23T00:00:00Z", "status": "ok",
                              "detail": "installed units"})
        host.journal("mefp", {"attempt": "0002", "at": "2026-09-23T01:00:00Z", "status": "failed",
                              "detail": "deploy-restart"})
        active, said = launcher.root_recorded_tenant_facts(
            "mefp", runner=lambda args, **kw: subprocess.CompletedProcess(args, 0),
            journal_root=host.journal_root,
        )
        assert active is True
        assert "0002" in said and "failed" in said and "deploy-restart" in said, said

        # Inactive is a real answer; anything else is no answer at all.
        inactive, _ = launcher.root_recorded_tenant_facts(
            "mefp", runner=lambda args, **kw: subprocess.CompletedProcess(args, 3),
            journal_root=host.journal_root,
        )
        assert inactive is False
        unknown, _ = launcher.root_recorded_tenant_facts(
            "mefp", runner=lambda args, **kw: subprocess.CompletedProcess(args, 4),
            journal_root=host.journal_root,
        )
        assert unknown is None
        # A tenant with no journal at all is simply no line.
        _active, none_said = launcher.root_recorded_tenant_facts(
            "nothing-here", runner=lambda args, **kw: subprocess.CompletedProcess(args, 0),
            journal_root=host.journal_root,
        )
        assert none_said == "", none_said
    finally:
        host.close()


def test_the_unit_it_asks_about_is_the_one_provisioning_renders() -> None:
    """Derived from the slug, because the tenant's own document is unreadable."""
    from scripts.ticket_board.project_provision import build_plan

    asked: list[list[str]] = []
    launcher.root_recorded_tenant_facts(
        "porter",
        runner=lambda args, **kw: asked.append(list(args)) or subprocess.CompletedProcess(args, 0),
        journal_root=Path("/nonexistent-syrd241"),
    )
    plan = build_plan(project="porter", project_name="Porter", owner_user=ME,
                      owner_home=Path("/home/porter-owner"), source_repo=ROOT)
    assert asked and asked[0][-1] == plan.board_unit, (asked, plan.board_unit)
    assert "sudo" not in asked[0] and "pkexec" not in asked[0], asked


def main() -> int:
    run_module_tests(globals())
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"readonly_status_without_root_test: {count} tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
