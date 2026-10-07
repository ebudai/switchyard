"""A runtime switch's tenant projection: every file that names the role's runtime (SYRD-558).

A workflow-driven tenant carries the declared workflow in its launcher config,
`workflow.json`, `plan.json` and `<project>.project.json`, and every copy names
each role's runtime. `scripts.role_runtime` used to rewrite the role's launcher
entry alone, so after a switch the board and the entry said the new runtime and
every copy the old one -- Otto's main and uiux, hand-edited back into line.
These compute the whole projection by the rule `workflow apply` uses
(`workflow_launcher.projection_files`), write it config-last with every file's
previous bytes journalled first, restore it on rollback, and find a projection
the board's declaration has left behind. `role_runtime` imports them; they reach
back into it only when they run.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping


def runtime_projection_files(
    config_path: Path,
    *,
    role_name: str,
    runtime: str,
    document: Mapping[str, Any] | None,
    model: str | None = None,
    effort: str | None = None,
) -> dict[Path, str]:
    """Every tenant file the switch writes, as it will read: computed whole before any is written.

    The role's launcher entry is rewritten by `project_role_runtime`. A
    workflow-driven tenant -- one whose config carries the declared workflow --
    also carries that document in its config, `workflow.json`, `plan.json` and
    the project artifact, and every copy names the role's runtime. Writing the
    entry alone left those naming the old runtime while the board declared the
    new one: Otto's main and uiux, hand-edited back into line (SYRD-558). So the
    whole projection is computed from the document the switch applies, by the
    same rule `workflow apply` uses, over the rewritten entry.
    """
    from scripts import role_runtime as rr

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    roles = raw.get("roles")
    if not isinstance(roles, list):
        raise rr.RoleRuntimeRefusal(f"switchyard: {config_path} must define a roles list")
    for entry in roles:
        if not isinstance(entry, dict) or entry.get("role") != role_name:
            continue
        rr.project_role_runtime(entry, runtime=runtime, model=model, **rr._effort_kwargs(effort))
        break
    else:
        raise rr.RoleRuntimeRefusal(f"switchyard: {config_path} has no role {role_name!r}")
    if not isinstance(raw.get("workflow"), Mapping) or document is None:
        return {config_path: json.dumps(raw, indent=2, sort_keys=True) + "\n"}
    from scripts.workflow_launcher import projection_files

    try:
        files = projection_files(config_path, dict(document), raw=raw)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise rr.RoleRuntimeRefusal(
            f"switchyard: cannot project the declared workflow into {config_path.parent}: {exc}"
        ) from exc
    # The launcher config last: it is what says which runtime the role runs, so
    # a write interrupted part-way still reads as a switch not yet made, and the
    # same command finishes it instead of calling it done.
    return {**{path: text for path, text in files.items() if path != config_path}, config_path: files[config_path]}


def declared_projection(config_path: Path, document: Mapping[str, Any]) -> dict[Path, str]:
    """A workflow-driven tenant's projection of the board's document, as `workflow apply` writes it; {} for any other."""
    from scripts.workflow_launcher import projection_files

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw.get("workflow"), Mapping):
            return {}
        files = projection_files(config_path, dict(document), raw=raw)
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return {**{path: text for path, text in files.items() if path != config_path}, config_path: files[config_path]}


def stale_projection(files: Mapping[Path, str]) -> dict[Path, str]:
    """The files whose content differs from what they should hold -- as data, so formatting alone is never stale."""
    def differs(path: Path, text: str) -> bool:
        try:
            return json.loads(path.read_text(encoding="utf-8")) != json.loads(text)
        except (OSError, ValueError):
            return True

    return {path: text for path, text in files.items() if differs(path, text)}


def write_runtime_projection(
    config: Any,
    *,
    config_path: Path,
    role_name: str,
    runtime: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    model: str | None = None,
    effort: str | None = None,
    document: Mapping[str, Any] | None = None,
    journal: Any = None,
    journal_path: Path | None = None,
) -> Any:
    """Point the tenant's projection at the new runtime -- every file that names it -- leaving everything else."""
    from scripts import team_launcher
    from scripts.workflow_manage import apply_files

    files = runtime_projection_files(
        config_path, role_name=role_name, runtime=runtime, document=document, model=model, effort=effort
    )
    if journal is not None:
        # Recorded before the first byte is written, so a crash between files
        # leaves a journal that names every one to put back.
        journal.previous_projection = {
            str(path): path.read_text(encoding="utf-8") if path.exists() else None for path in files
        }
        if journal_path is not None:
            journal.write(journal_path)
    apply_files(files)
    for path in files:
        team_launcher.ensure_owner_file(config, path, runner=runner)
    return team_launcher.load_project_config(config.project, config_path)


def restore_projection(journal: Any) -> None:
    from scripts.workflow_manage import atomic

    previous = journal.previous_projection or {journal.config_path: journal.previous_config_bytes}
    for name, content in previous.items():
        path = Path(name)
        if content is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, content)
