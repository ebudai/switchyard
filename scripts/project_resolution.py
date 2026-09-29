"""Project discovery and resolution: which projects exist, and which one a selection names.

`_project_entries` reads the launcher configs in the config directory and
`_registry_project_entries` the registered tenants in the registry, and
`_switchyard_entries` merges them, keeping the first entry seen for a slug --
the launcher config's, which are read first -- and sorts them by name.
`_resolve_switchyard_project` finds the one a selection names --
by slug or by name, then by the slugs a name would derive
(`_project_name_selector_slugs`) -- or refuses: an empty or ambiguous
selection, a first word that is a project of its own, and, for a tenant whose
provisioning stopped part-way (`partial_provision_record`), how to resume it
(`_resume_provision_hint`).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-454). The launcher
imports this module and re-exports all seven names, so every launcher caller
and every module that reads them through the launcher still reaches the
launcher's names. Everything they read when they run -- each other included,
the config directory, registry directory and schema, the JSON reader, the slug
validator and derivations, the entry type and the privileged baseline plan
path -- is read through the launcher, so a suite that rebinds one there still
intercepts it. This module imports `team_launcher` only inside the functions,
when they run.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.team_launcher import SwitchyardProjectEntry


def _project_name_selector_slugs(name: str) -> set[str]:
    from scripts import team_launcher as launcher

    selectors: set[str] = set()
    for derive in (launcher._slug_from_project_name, launcher._legacy_dash_slug_from_project_name):
        try:
            selectors.add(derive(name).casefold())
        except SystemExit:
            continue
    return selectors


def _project_entries(config_dir: Path | None = None) -> list[SwitchyardProjectEntry]:
    from scripts import team_launcher as launcher

    config_dir = config_dir or launcher.DEFAULT_CONFIG_DIR
    entries: list[SwitchyardProjectEntry] = []
    try:
        paths = sorted(path for path in config_dir.iterdir() if path.is_file() and path.suffix == ".json")
    except OSError:
        return []
    for path in paths:
        try:
            raw = launcher._load_json(path)
        except (OSError, json.JSONDecodeError, SystemExit):
            continue
        roles_raw = raw.get("roles")
        if not isinstance(roles_raw, list) or not roles_raw:
            continue
        try:
            slug = launcher._validate_project_slug(str(raw.get("project") or path.stem))
        except SystemExit as exc:
            print(f"warning: switchyard: skipping {path}: {exc}", file=sys.stderr)
            continue
        name = str(raw.get("project_name") or raw.get("name") or slug).strip() or slug
        entries.append(launcher.SwitchyardProjectEntry(slug=slug, name=name, config_path=path))
    return entries


def _registry_project_entries(registry_dir: Path | None = None) -> list[SwitchyardProjectEntry]:
    from scripts import team_launcher as launcher

    registry_dir = registry_dir or launcher.switchyard_registry_dir()
    entries: list[SwitchyardProjectEntry] = []
    try:
        paths = sorted(path for path in registry_dir.iterdir() if path.is_file() and path.suffix == ".json")
    except OSError:
        return []
    for path in paths:
        try:
            raw = launcher._load_json(path)
        except (OSError, json.JSONDecodeError, SystemExit):
            continue
        if str(raw.get("schema") or "") != launcher.SWITCHYARD_REGISTRY_SCHEMA:
            continue
        try:
            slug = launcher._validate_project_slug(str(raw.get("slug") or ""))
        except SystemExit as exc:
            print(f"warning: switchyard: skipping {path}: {exc}", file=sys.stderr)
            continue
        name = str(raw.get("name") or slug).strip() or slug
        config_path_raw = str(raw.get("config_path") or "").strip()
        if not slug or not config_path_raw:
            continue
        entries.append(launcher.SwitchyardProjectEntry(slug=slug, name=name, config_path=Path(config_path_raw).expanduser()))
    return entries


def _switchyard_entries(
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
) -> list[SwitchyardProjectEntry]:
    from scripts import team_launcher as launcher

    entries: dict[str, SwitchyardProjectEntry] = {}
    for entry in [*launcher._project_entries(config_dir), *launcher._registry_project_entries(registry_dir)]:
        key = entry.slug.casefold()
        entries.setdefault(key, entry)
    return sorted(entries.values(), key=lambda entry: (entry.name.casefold(), entry.slug.casefold()))


def partial_provision_record(slug: str) -> Path | None:
    """Root's own record of a project whose provisioning did not finish.

    The one place a partial installation can be recognised from. The tenant's
    own directory cannot answer this -- it is writable by the account every
    role runs as, and a project that never reached registration has no registry
    entry to check either (SYRD-147).
    """
    from scripts import team_launcher as launcher

    baseline = launcher.privileged_baseline_plan_path(slug)
    try:
        info = os.stat(baseline, follow_symlinks=False)
    except OSError:
        return None
    return baseline if stat.S_ISREG(info.st_mode) else None


def _resume_provision_hint(slug: str) -> str:
    """What to say about a project that is not registered but was started."""
    from scripts import team_launcher as launcher

    if launcher.partial_provision_record(slug) is None:
        return ""
    return (
        f"switchyard: {slug!r} is not registered, but root holds a provisioning record for it: "
        f"its `switchyard new` stopped before registration. Resume it with "
        f"`sudo switchyard resume-provision {slug}`."
    )


def _resolve_switchyard_project(
    selection: str,
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
) -> SwitchyardProjectEntry:
    from scripts import team_launcher as launcher

    wanted = selection.strip().casefold()
    if not wanted:
        raise SystemExit("switchyard: project name cannot be empty")
    entries = launcher._switchyard_entries(config_dir=config_dir, registry_dir=registry_dir)
    exact = [
        entry
        for entry in entries
        if entry.name.casefold() == wanted or entry.slug.casefold() == wanted
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise SystemExit(f"switchyard: project selector {selection!r} is ambiguous")
    fallback = [entry for entry in entries if wanted in launcher._project_name_selector_slugs(entry.name)]
    if len(fallback) == 1:
        return fallback[0]
    words = selection.split()
    if len(words) > 1:
        first = words[0].casefold()
        first_matches = [
            entry
            for entry in entries
            if entry.slug.casefold() == first or entry.name.casefold() == first
        ]
        if len(first_matches) == 1:
            raise SystemExit(
                f"switchyard: {words[0]!r} is a project; did you mean `switchyard {first_matches[0].slug}`? "
                "A bare project name starts or attaches it."
            )
    # A project that never reached registration is unknown to every ordinary
    # command, and the installation is still there: the account, its
    # repository, its journal and its exported release. Saying only "unknown"
    # sent a live recovery looking for a workaround, so the one supported way
    # back is named here, where the failure is (SYRD-147).
    hint = launcher._resume_provision_hint(selection.strip())
    if hint:
        raise SystemExit(hint)
    raise SystemExit(f"switchyard: unknown project {selection!r}")
