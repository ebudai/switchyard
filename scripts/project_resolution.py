"""Project discovery and resolution: which projects exist, and which one a selection names.

`_project_entries` reads the launcher configs in the config directory and
`_registry_project_entries` the registered tenants in the registry, and
`_switchyard_entries` merges them and sorts them by name. The registry is
authoritative (SYRD-709): a registered tenant is kept whatever the config
directory -- a release's own bundled `config/team-launcher` by default -- holds,
and a config there is dropped when the registry claims its slug or its name,
or holds an entry file for its slug that cannot be used
(`_registry_claims`). A config nobody registered is kept as before.
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
when they run. `_registry_claims` (SYRD-709) is this module's own and is called
directly, not re-exported; it reads the registry directory, schema, JSON reader
and slug validator through the launcher like the rest.
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


def _regular_file(path: Path) -> bool:
    """Whether `path` itself is a regular file, never looking through a link (SYRD-709).

    A registry entry is a regular file root wrote: a symlink, broken or not, is
    not one, and is never read through -- what it points at is whatever the link
    names, not root's record.
    """
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _registry_project_entries(registry_dir: Path | None = None) -> list[SwitchyardProjectEntry]:
    from scripts import team_launcher as launcher

    registry_dir = registry_dir or launcher.switchyard_registry_dir()
    entries: list[SwitchyardProjectEntry] = []
    try:
        paths = sorted(path for path in registry_dir.iterdir() if path.suffix == ".json" and _regular_file(path))
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


def _registry_claims(registry_dir: Path | None = None) -> dict[str, str]:
    """The slugs the registry holds an entry file for that cannot be used, casefolded, each with why (SYRD-709).

    `switchyard register` writes a tenant's entry as `<slug>.json`, so an entry
    claims its own name's slug even when it cannot be read, and the slug it
    names when that is valid. Every `.json` entry is looked at, never through a
    link: one that is not a regular file -- a symlink, broken or not, a
    directory -- or cannot be examined claims its name as surely as one that is
    not JSON (Audit). The key `""` means the registry directory exists but
    cannot be listed: then nothing can be known not to be registered. A
    registry that does not exist claims nothing.
    """
    from scripts import team_launcher as launcher

    registry_dir = registry_dir or launcher.switchyard_registry_dir()
    try:
        paths = sorted(path for path in registry_dir.iterdir() if path.suffix == ".json")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        return {"": f"the registry {registry_dir} cannot be read ({exc.strerror or exc})"}
    claims: dict[str, str] = {}
    for path in paths:
        claimed = [path.stem.casefold()]
        why = ""
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                why = "is not a regular file"  # nor is it read through: `_regular_file` keeps it out of the entries too
        except OSError as exc:
            why = f"cannot be examined ({exc.strerror or exc})"
        if not why:
            try:
                raw = launcher._load_json(path)
            except (OSError, json.JSONDecodeError, SystemExit) as exc:
                why = f"cannot be read ({exc})"
        if not why and str(raw.get("schema") or "") != launcher.SWITCHYARD_REGISTRY_SCHEMA:
            why = f"is not a {launcher.SWITCHYARD_REGISTRY_SCHEMA} record"
        if not why:
            try:
                slug = launcher._validate_project_slug(str(raw.get("slug") or ""))
            except SystemExit as exc:
                why = f"names no valid slug ({exc})"
            else:
                if str(raw.get("config_path") or "").strip():
                    continue  # usable: `_registry_project_entries` lists it
                claimed.append(slug.casefold())
                why = "names no configuration"
        for slug in claimed:
            claims.setdefault(slug, f"its registry entry {path} {why}")
    return claims


def _switchyard_entries(
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
) -> list[SwitchyardProjectEntry]:
    from scripts import team_launcher as launcher

    configured = launcher._project_entries(config_dir)
    registered = launcher._registry_project_entries(registry_dir)
    claims = _registry_claims(registry_dir)
    entries: dict[str, SwitchyardProjectEntry] = {}
    for entry in registered:
        if entry.slug.casefold() in claims:
            continue  # the registry also holds an unusable entry for it: refused, so never offered (Audit)
        entries.setdefault(entry.slug.casefold(), entry)
    # The registry is authoritative for every slug and name it holds (SYRD-709): a config of the same slug or name
    # never shadows it, and one whose slug the registry claims but cannot use is not offered in its place. With a
    # registry that cannot be listed, nothing is known not to be registered, so no config is offered at all.
    names = {entry.name.casefold() for entry in registered}
    for entry in configured:
        if "" in claims or entry.slug.casefold() in entries or entry.slug.casefold() in claims:
            continue
        if entry.name.casefold() in names:
            continue
        entries.setdefault(entry.slug.casefold(), entry)
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
    # A registry entry that exists for this slug but cannot be used is refused, never answered with another
    # configuration of the same slug -- a release's bundled one included (SYRD-709).
    claims = _registry_claims(registry_dir)
    if wanted in claims:
        raise SystemExit(f"switchyard: project {selection.strip()!r} is registered, but {claims[wanted]}; "
                         "refusing to use any other configuration for it. Repair or remove that entry as root.")
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
    if "" in claims:
        raise SystemExit(f"switchyard: cannot resolve {selection!r}: {claims['']}, so no project can be known "
                         "not to be registered")
    raise SystemExit(f"switchyard: unknown project {selection!r}")
