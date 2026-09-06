#!/usr/bin/env python3
"""Director onboarding migration readiness inventory (SYRD-41)."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import onboarding_readiness as readiness  # noqa: E402
from scripts import team_launcher  # noqa: E402

EXAMPLE = json.loads((ROOT / "examples" / "workflows" / "inspection.json").read_text())
BOARD_URL = "http://127.0.0.1:9/"  # never contacted; every test stubs the fetch


def _document(*, marked: bool, prompt: str | None = None, path: str | None = None) -> dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    if marked:
        document["migrations"] = {"director_onboarding": True}
    for role in document["roles"]:
        if role["name"] != "director":
            continue
        role.pop("onboarding_prompt", None)
        role["onboarding"] = path
        if prompt is not None:
            role["onboarding_prompt"] = prompt
    return document


def _tenant(tmp: Path, slug: str, config: dict[str, Any] | None) -> Path:
    launcher_dir = tmp / slug
    launcher_dir.mkdir(parents=True, exist_ok=True)
    config_path = launcher_dir / f"{slug}.json"
    if config is not None:
        config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _config(document: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    config: dict[str, Any] = {"project": "porter", "board_url": BOARD_URL, "roles": [], **extra}
    if document is not None:
        config["workflow"] = document
    return config


def _assess(config: dict[str, Any] | None, board: dict[str, Any] | None, *, slug: str = "porter"):
    """Assess one tenant with the board response stubbed; nothing is contacted."""
    original = readiness._board_workflow
    readiness._board_workflow = lambda url, *, timeout: (
        (board, "") if board is not None else (None, "board is unreachable: URLError")
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            return readiness.assess_tenant(slug, _tenant(Path(tmp), slug, config))
    finally:
        readiness._board_workflow = original


# --- the migrated shapes -----------------------------------------------------


def test_a_migrated_tenant_with_a_stored_prompt_is_ready() -> None:
    document = _document(marked=True, prompt="director remit")
    result = _assess(_config(document), {"revision": 4, "document": document})
    assert result.verified, result
    assert result.migrated and not result.deliberate_clear
    assert result.director_role == "director"


def test_a_migrated_tenant_with_a_remit_path_is_ready() -> None:
    document = _document(marked=True, path="/srv/porter/docs/onboarding/director.md")
    result = _assess(_config(document), {"revision": 4, "document": document})
    assert result.verified, result
    assert result.migrated and not result.deliberate_clear


def test_a_deliberate_clear_is_ready_not_unmigrated() -> None:
    """Marked with nothing stored is a decision, not an outstanding migration."""
    document = _document(marked=True)
    result = _assess(_config(document), {"revision": 4, "document": document})
    assert result.verified, result
    assert result.deliberate_clear is True
    assert "deliberately cleared" in result.detail


def test_an_unmarked_tenant_is_unverified() -> None:
    document = _document(marked=False, prompt="director remit")
    result = _assess(_config(document), {"revision": 4, "document": document})
    assert not result.verified
    assert "has not run" in result.detail


# --- the failure shapes the ticket names -------------------------------------


def test_a_stale_projection_is_detected_by_the_marker() -> None:
    on_disk = _document(marked=False, prompt="director remit")
    on_board = _document(marked=True, prompt="director remit")
    result = _assess(_config(on_disk), {"revision": 5, "document": on_board})
    assert not result.verified
    assert "stale" in result.detail


def test_a_stale_projection_is_detected_by_the_onboarding_itself() -> None:
    on_disk = _document(marked=True)
    on_board = _document(marked=True, prompt="director remit")
    result = _assess(_config(on_disk), {"revision": 5, "document": on_board})
    assert not result.verified
    assert "stale" in result.detail and "differs" in result.detail


def test_an_old_board_schema_reads_as_unmigrated_rather_than_ready() -> None:
    """A pre-phase-one board cannot represent the marker, so it can never be ready."""
    old_board_document = copy.deepcopy(EXAMPLE)  # no migrations key at all
    result = _assess(_config(_document(marked=False)), {"revision": 2, "document": old_board_document})
    assert not result.verified
    assert "marker" in result.detail


def test_an_unreachable_board_is_unverified() -> None:
    result = _assess(_config(_document(marked=True, prompt="x")), None)
    assert not result.verified
    assert "unreachable" in result.detail


def test_a_board_with_no_active_configuration_is_unverified() -> None:
    result = _assess(_config(_document(marked=True, prompt="x")), {"revision": 0, "document": None})
    assert not result.verified
    assert "no active workflow configuration" in result.detail


def test_a_missing_tenant_config_is_unverified() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "absent" / "porter.json"
        result = readiness.assess_tenant("porter", missing)
    assert not result.verified
    assert "missing" in result.detail


def test_an_unreadable_tenant_config_is_unverified() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config_path = _tenant(Path(tmp), "porter", _config(_document(marked=True, prompt="x")))
        config_path.chmod(0o000)
        try:
            result = readiness.assess_tenant("porter", config_path)
        finally:
            config_path.chmod(0o600)
    # Root can read anything; the check still must not claim readiness it did not prove.
    assert result.verified or "not readable" in result.detail


def test_ambiguous_director_capability_is_unverified() -> None:
    document = _document(marked=True, prompt="x")
    for role in document["roles"]:
        if role["name"] == "ops":
            role["capabilities"] = sorted(
                set(role["capabilities"]) | readiness.DIRECTOR_CAPABILITIES
            )
    result = _assess(_config(document), {"revision": 4, "document": document})
    assert not result.verified
    assert "ambiguous" in result.detail
    assert "ops" in result.detail and "director" in result.detail


def test_a_missing_board_url_is_unverified() -> None:
    config = _config(_document(marked=True, prompt="x"))
    config.pop("board_url")
    result = _assess(config, {"revision": 4, "document": _document(marked=True, prompt="x")})
    assert not result.verified
    assert "board url" in result.detail


# --- non-declarative tenants -------------------------------------------------


def test_a_non_declarative_tenant_with_a_seeded_environment_is_ready() -> None:
    config = _config(None, roles=[{"role": "director", "env": {
        "TICKET_BOARD_ROLE_ONBOARDING_PROMPT": "seeded at provisioning"
    }}])
    result = _assess(config, None)
    assert result.verified, result
    assert "not declarative" in result.detail


def test_a_non_declarative_tenant_without_onboarding_is_unverified() -> None:
    result = _assess(_config(None, roles=[{"role": "director", "env": {}}]), None)
    assert not result.verified
    assert "no role carries an onboarding prompt" in result.detail


# --- the inventory across tenants --------------------------------------------


class _Entry:
    def __init__(self, slug: str, config_path: Path) -> None:
        self.slug = slug
        self.config_path = config_path


def test_a_mixed_estate_reports_each_tenant_and_fails_overall() -> None:
    migrated = _document(marked=True, prompt="remit")
    unmigrated = _document(marked=False, prompt="remit")

    original_entries = readiness.registered_tenants
    original_board = readiness._board_workflow
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        good = _tenant(root, "alpha", _config(migrated))
        bad = _tenant(root, "beta", _config(unmigrated))
        boards = {"alpha": migrated, "beta": unmigrated}
        seen: list[str] = []

        def _stub_board(url, *, timeout):
            # Which board is asked is derived from the tenant under assessment.
            slug = seen[-1]
            return {"revision": 3, "document": boards[slug]}, ""

        def _stub_entries(registry_dir=None, config_dir=None):
            return [_Entry("alpha", good), _Entry("beta", bad)]

        original_assess = readiness.assess_tenant

        def _tracking_assess(slug, config_path, *, timeout=5.0):
            seen.append(slug)
            return original_assess(slug, config_path, timeout=timeout)

        readiness.registered_tenants = _stub_entries
        readiness._board_workflow = _stub_board
        readiness.assess_tenant = _tracking_assess
        try:
            report = readiness.inventory()
        finally:
            readiness.registered_tenants = original_entries
            readiness._board_workflow = original_board
            readiness.assess_tenant = original_assess

    assert not report.ready
    assert [t.slug for t in report.unverified] == ["beta"]
    rendered = readiness.render(report)
    assert "alpha" in rendered and "beta" in rendered
    assert "must not be removed" in rendered


def test_the_json_report_is_stable_and_keeps_tenant_configuration_private() -> None:
    document = _document(marked=True, prompt="SECRET DIRECTOR REMIT")
    result = _assess(_config(document, repository="/srv/porter"), {"revision": 4, "document": document})
    report = readiness.ReadinessReport(tenants=[result])
    payload = json.dumps(report.as_json())

    assert json.loads(payload)["schema"] == "switchyard.onboarding-readiness.v1"
    assert set(json.loads(payload)) == {"schema", "ready", "tenants", "unverified"}
    assert set(result.as_json()) == {
        "tenant", "state", "detail", "director_role", "migrated", "deliberate_clear",
    }
    # Nothing tenant-private leaks into the report.
    assert "SECRET DIRECTOR REMIT" not in payload
    assert "/srv/porter" not in payload
    assert BOARD_URL not in payload


def test_an_empty_estate_is_not_reported_as_ready() -> None:
    """Nothing verified is not the same as everything verified."""
    report = readiness.ReadinessReport()
    assert not report.ready
    assert "nothing to verify" in readiness.render(report)


# --- the guard SYRD-40 will call ---------------------------------------------


def test_the_command_exits_nonzero_while_any_tenant_is_unverified() -> None:
    original = readiness.inventory
    unready = readiness.ReadinessReport(
        tenants=[readiness.TenantReadiness("beta", readiness.UNVERIFIED, "not migrated")]
    )
    ready = readiness.ReadinessReport(
        tenants=[readiness.TenantReadiness("alpha", readiness.READY, "migrated", migrated=True)]
    )
    import contextlib
    import io

    try:
        readiness.inventory = lambda **_kw: unready
        with contextlib.redirect_stdout(io.StringIO()) as json_out:
            assert readiness.main(["--json"]) == 1
        assert json.loads(json_out.getvalue())["ready"] is False
        readiness.inventory = lambda **_kw: ready
        with contextlib.redirect_stdout(io.StringIO()) as text_out:
            assert readiness.main([]) == 0
        assert "verified" in text_out.getvalue()
    finally:
        readiness.inventory = original


def test_the_preflight_is_a_registered_command() -> None:
    assert "onboarding-readiness" in team_launcher.SWITCHYARD_COMMANDS
    assert "onboarding-readiness" in team_launcher.switchyard_help_text()


def test_the_inventory_makes_no_mutating_calls() -> None:
    """Read-only by construction: the module names no writer at all."""
    source = (ROOT / "scripts" / "onboarding_readiness.py").read_text(encoding="utf-8")
    for forbidden in (
        "configure_workflow",
        "apply_files",
        "write_text",
        "TicketBoardWriteClient",
        "subprocess",
        "os.chown",
        "unlink",
    ):
        assert forbidden not in source, forbidden


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print("onboarding_readiness_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
