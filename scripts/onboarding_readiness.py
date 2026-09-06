#!/usr/bin/env python3
"""Read-only readiness inventory for the Director onboarding migration (SYRD-41).

Proves, per registered tenant, whether the phase-one migration has landed, so the
legacy compatibility bridge can be removed without stranding anyone. Reads only: it
never writes a board, a config, a service, or a file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.workflow_config import (  # noqa: E402
    DIRECTOR_ONBOARDING_MIGRATION,
    DIRECTOR_ROLE,
)

# Capabilities that together mark the role holding director authority. Derived from the
# declarative document rather than assumed from a name: a tenant is free to label its
# roles, and a document where these do not land on exactly one role is reported as
# ambiguous instead of guessed at.
DIRECTOR_CAPABILITIES = frozenset({"merge", "set_blockers", "set_manually_controlled"})

READY = "ready"
UNVERIFIED = "unverified"


@dataclass(frozen=True)
class TenantReadiness:
    slug: str
    state: str
    detail: str
    director_role: str = ""
    migrated: bool = False
    deliberate_clear: bool = False

    @property
    def verified(self) -> bool:
        return self.state == READY

    def as_json(self) -> dict[str, Any]:
        """Stable shape. Deliberately carries no prompt text or tenant paths."""
        return {
            "tenant": self.slug,
            "state": self.state,
            "detail": self.detail,
            "director_role": self.director_role,
            "migrated": self.migrated,
            "deliberate_clear": self.deliberate_clear,
        }


@dataclass
class ReadinessReport:
    tenants: list[TenantReadiness] = field(default_factory=list)

    @property
    def unverified(self) -> list[TenantReadiness]:
        return [t for t in self.tenants if not t.verified]

    @property
    def ready(self) -> bool:
        return bool(self.tenants) and not self.unverified

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": "switchyard.onboarding-readiness.v1",
            "ready": self.ready,
            "tenants": [t.as_json() for t in self.tenants],
            "unverified": [t.slug for t in self.unverified],
        }


def _unverified(slug: str, detail: str, **extra: Any) -> TenantReadiness:
    return TenantReadiness(slug=slug, state=UNVERIFIED, detail=detail, **extra)


def find_director_role(document: dict[str, Any]) -> tuple[str, str]:
    """Return (role_name, error). Exactly one role must hold director authority."""
    roles = document.get("roles")
    if not isinstance(roles, list) or not roles:
        return "", "workflow document has no roles"
    holders = [
        str(role.get("name", ""))
        for role in roles
        if isinstance(role, dict)
        and DIRECTOR_CAPABILITIES.issubset(set(role.get("capabilities") or ()))
    ]
    if len(holders) == 1:
        return holders[0], ""
    if not holders:
        # The model still requires a structurally named director; say so precisely
        # rather than reporting nothing found.
        named = [r for r in roles if isinstance(r, dict) and r.get("name") == DIRECTOR_ROLE]
        if named:
            return "", (
                f"no role holds director capabilities "
                f"({', '.join(sorted(DIRECTOR_CAPABILITIES))}); "
                f"the {DIRECTOR_ROLE!r} role does not carry them"
            )
        return "", "no role holds director capabilities"
    return "", f"director capabilities are ambiguous across roles: {', '.join(sorted(holders))}"


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return None, "file is missing"
    except PermissionError:
        return None, "file is not readable by this user"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"file could not be read: {exc.__class__.__name__}"


def _board_workflow(board_url: str, *, timeout: float) -> tuple[dict[str, Any] | None, str]:
    root = board_url.rstrip("/").removesuffix("/api/tickets").removesuffix("/api")
    try:
        with urllib.request.urlopen(root + "/api/workflow", timeout=timeout) as response:
            return json.load(response), ""
    except urllib.error.URLError as exc:
        return None, f"board is unreachable: {exc.__class__.__name__}"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"board response was unusable: {exc.__class__.__name__}"


def _marked(document: dict[str, Any]) -> bool:
    migrations = document.get("migrations")
    return bool(isinstance(migrations, dict) and migrations.get(DIRECTOR_ONBOARDING_MIGRATION))


def _role_named(document: dict[str, Any], name: str) -> dict[str, Any]:
    for role in document.get("roles", []):
        if isinstance(role, dict) and role.get("name") == name:
            return role
    return {}


def assess_tenant(slug: str, config_path: Path, *, timeout: float = 5.0) -> TenantReadiness:
    """Assess one registered tenant. Performs reads only."""
    config, error = _read_json(config_path)
    if config is None:
        return _unverified(slug, f"launcher config {error}")

    document = config.get("workflow")
    if not document:
        # No declarative document: the director's remit comes from the launcher role
        # environment instead, which the bridge removal does not consult. Ready only if
        # that environment actually carries a prompt.
        for role in config.get("roles", []):
            env = (role or {}).get("env") or {}
            if env.get("TICKET_BOARD_ROLE_ONBOARDING_PROMPT"):
                return TenantReadiness(
                    slug=slug,
                    state=READY,
                    detail="not declarative; director onboarding is set in the launcher environment",
                    director_role=str(role.get("role", "")),
                    migrated=True,
                )
        return _unverified(
            slug,
            "not declarative and no role carries an onboarding prompt in its environment",
        )

    director, ambiguity = find_director_role(document)
    if ambiguity:
        return _unverified(slug, ambiguity)

    board_url = str(config.get("board_url") or "").strip()
    if not board_url:
        return _unverified(slug, "launcher config records no board url", director_role=director)
    board, error = _board_workflow(board_url, timeout=timeout)
    if board is None:
        return _unverified(slug, error, director_role=director)

    board_document = board.get("document")
    if not board_document:
        return _unverified(
            slug, "board has no active workflow configuration", director_role=director
        )

    # Schema acceptance: a board that predates phase one cannot represent the marker at
    # all, so its absence there is a schema problem rather than an unmigrated tenant.
    board_marked = _marked(board_document)
    projection_marked = _marked(document)

    if not board_marked and not projection_marked:
        return _unverified(
            slug,
            "director onboarding migration has not run; the board carries no marker",
            director_role=director,
        )
    if board_marked != projection_marked:
        return _unverified(
            slug,
            "projection is stale: the marker differs between the board and the on-disk workflow",
            director_role=director,
        )

    board_role = _role_named(board_document, director)
    disk_role = _role_named(document, director)
    if not board_role:
        return _unverified(
            slug,
            f"board document has no {director!r} role; the projection and board disagree",
            director_role=director,
        )

    has_onboarding = bool(board_role.get("onboarding_prompt") or board_role.get("onboarding"))
    disk_has_onboarding = bool(disk_role.get("onboarding_prompt") or disk_role.get("onboarding"))
    if has_onboarding != disk_has_onboarding:
        return _unverified(
            slug,
            "projection is stale: director onboarding differs between the board and disk",
            director_role=director,
        )

    if has_onboarding:
        return TenantReadiness(
            slug=slug,
            state=READY,
            detail="migrated; director onboarding is stored",
            director_role=director,
            migrated=True,
        )
    return TenantReadiness(
        slug=slug,
        state=READY,
        detail="migrated; director onboarding was deliberately cleared",
        director_role=director,
        migrated=True,
        deliberate_clear=True,
    )


def registered_tenants(registry_dir: Path | None = None, config_dir: Path | None = None):
    """Registry entries only. Never scans homes and never guesses a tenant."""
    from scripts import team_launcher as launcher

    return launcher._switchyard_entries(config_dir=config_dir, registry_dir=registry_dir)


def inventory(
    *,
    registry_dir: Path | None = None,
    config_dir: Path | None = None,
    timeout: float = 5.0,
) -> ReadinessReport:
    report = ReadinessReport()
    for entry in registered_tenants(registry_dir=registry_dir, config_dir=config_dir):
        report.tenants.append(assess_tenant(entry.slug, entry.config_path, timeout=timeout))
    return report


def render(report: ReadinessReport) -> str:
    if not report.tenants:
        return "no registered tenants found; nothing to verify"
    width = max(len(t.slug) for t in report.tenants)
    lines = []
    for tenant in sorted(report.tenants, key=lambda t: t.slug):
        mark = "ok " if tenant.verified else "!! "
        lines.append(f"{mark}{tenant.slug.ljust(width)}  {tenant.detail}")
    if report.unverified:
        lines.append("")
        lines.append(
            f"{len(report.unverified)} of {len(report.tenants)} tenants are unverified: "
            + ", ".join(t.slug for t in report.unverified)
        )
        lines.append("the director onboarding bridge must not be removed while any tenant is unverified")
    else:
        lines.append("")
        lines.append(f"all {len(report.tenants)} registered tenants are migrated and verified")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="switchyard onboarding-readiness",
        description=(
            "Report whether every registered tenant has completed the director onboarding "
            "migration. Read-only; exits nonzero while any tenant is unverified."
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit the stable JSON report")
    parser.add_argument("--registry-dir", type=Path, default=None)
    parser.add_argument("--config-dir", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    report = inventory(
        registry_dir=args.registry_dir, config_dir=args.config_dir, timeout=args.timeout
    )
    print(json.dumps(report.as_json(), indent=2, sort_keys=True) if args.json else render(report))
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
