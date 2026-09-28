"""`switchyard register`: check a project config can be registered, refuse a collision, record it.

`_registered_project_collision` names the registered project a new slug or
name would collide with; `_check_switchyard_registration_available` refuses
with that; `_register_switchyard_project` loads the config, checks it, and
writes its root-owned, world-readable registry entry; and
`switchyard_register_command` is the verb.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-433), in their original
order. The launcher imports this module and re-exports all four names;
`switchyard_main` still dispatches `register` to the launcher's name, and
`scripts/new_project_phases.py` and `scripts/resume_provision_command.py` still
read the check and the registration there. Everything they read -- each other
included, the registry directory, schema and agent-CLI key, the JSON reader,
the config loader, the slug check, the registry listing and the configured-CLI
reader -- is read through the launcher at call time, so a suite that rebinds
one there still intercepts it. The one definition-time default is `print`.
This module imports `team_launcher` only inside the functions, when they run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable


def _registered_project_collision(
    *,
    slug: str,
    name: str,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    skip_config_path: Path | None = None,
) -> str:
    from scripts import team_launcher as launcher

    skip_resolved = skip_config_path.expanduser().resolve(strict=False) if skip_config_path is not None else None
    for entry in launcher._switchyard_entries(config_dir=config_dir, registry_dir=registry_dir):
        if skip_resolved is not None and entry.config_path.expanduser().resolve(strict=False) == skip_resolved:
            continue
        if entry.slug.casefold() == slug.casefold():
            return (
                f"switchyard: project slug {slug!r} is already registered to "
                f"{entry.name!r} at {entry.config_path}"
            )
        if entry.name.casefold() == name.casefold():
            return (
                f"switchyard: project name {name!r} is already registered as "
                f"{entry.slug!r} at {entry.config_path}"
            )
    return ""


def _check_switchyard_registration_available(
    *,
    slug: str,
    name: str,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    skip_config_path: Path | None = None,
) -> None:
    from scripts import team_launcher as launcher

    collision = launcher._registered_project_collision(
        slug=slug,
        name=name,
        config_dir=config_dir,
        registry_dir=registry_dir,
        skip_config_path=skip_config_path,
    )
    if collision:
        raise SystemExit(collision)


def _register_switchyard_project(
    config_path: Path,
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
) -> Path:
    from scripts import team_launcher as launcher

    resolved_config_path = config_path.expanduser().resolve(strict=False)
    raw = launcher._load_json(resolved_config_path)
    raw_slug = str(raw.get("project") or resolved_config_path.stem).strip()
    if not raw_slug:
        raise SystemExit(f"switchyard: cannot register {resolved_config_path}: project slug is empty")
    slug = launcher._validate_project_slug(raw_slug)
    config = launcher.load_project_config(slug, resolved_config_path)
    name = str(raw.get("project_name") or raw.get("name") or slug).strip() or slug
    registry_dir = registry_dir or launcher.switchyard_registry_dir()
    registry_path = registry_dir / f"{slug}.json"
    launcher._check_switchyard_registration_available(
        slug=slug,
        name=name,
        config_dir=config_dir,
        registry_dir=registry_dir,
        skip_config_path=resolved_config_path,
    )
    if registry_path.exists():
        raise SystemExit(f"switchyard: registry entry {registry_path} already exists; refusing to overwrite")
    payload = {
        "schema": launcher.SWITCHYARD_REGISTRY_SCHEMA,
        "slug": slug,
        "name": name,
        "config_path": str(resolved_config_path),
        # Recorded here because this is the last moment the configuration and a
        # root-owned, world-readable file are both in reach: afterwards the
        # configuration is under the owner's home, and a launch cannot read it
        # from the operator's side of the boundary. Without it a launch has to
        # guess which CLIs a tenant uses, and guessing "all of them" asked the
        # `test` tenant to promote a Hermes no role of its uses (SYRD-220).
        launcher.SWITCHYARD_REGISTRY_AGENT_CLIS_KEY: launcher._configured_agent_clis(config),
    }
    try:
        registry_dir.mkdir(parents=True, exist_ok=True)
        registry_dir.parent.chmod(0o755)
        registry_dir.chmod(0o755)
        registry_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        registry_path.chmod(0o644)
    except OSError as exc:
        raise SystemExit(f"switchyard: failed to register project {slug!r} in {registry_dir}: {exc}") from exc
    return registry_path


def switchyard_register_command(
    config_path: Path,
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    from scripts import team_launcher as launcher

    registry_path = launcher._register_switchyard_project(config_path, config_dir=config_dir, registry_dir=registry_dir)
    print_func(f"switchyard: registered {config_path.expanduser().resolve(strict=False)} at {registry_path}")
    return 0
