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

The project artifact is part of the projection on every tenant, workflow-driven
or not, wherever it records the role: its `role_clis`, `role_models` and
`role_efforts`, and the Researcher's, Librarian's and Router's own blocks
(`artifact_role_records`). `set-role-runtime --model` on ocstab's Researcher
rewrote the launcher entry and left `project.research` on the model the role was
made with, and the Director hand-edited it (SYRD-899). A switch to what the role
already runs repairs an artifact left behind (`noop_repair`), and a dry run says
where the role is recorded (`recorded_where`).
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
    owner_user: str = "",
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
    from scripts.workflow_launcher import _tenant_json

    raw = _tenant_json(config_path, owner_user)
    if raw is None:
        raise FileNotFoundError(2, "No such file or directory", str(config_path))
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
        files = {config_path: json.dumps(raw, indent=2, sort_keys=True) + "\n"}
    else:
        from scripts.workflow_launcher import projection_files

        try:
            files = projection_files(config_path, dict(document), raw=raw, owner_user=owner_user)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise rr.RoleRuntimeRefusal(
                f"switchyard: cannot project the declared workflow into {config_path.parent}: {exc}"
            ) from exc
    # The project artifact too, wherever it records the role, on any tenant: the entry alone left the artifact naming
    # the model a Researcher was made with after `set-role-runtime --model` chose another (SYRD-899).
    files.update(artifact_role_records(config_path, raw, role_name=role_name, runtime=runtime, owner_user=owner_user,
                                       held=files))
    return config_last(files, config_path)


def config_last(files: Mapping[Path, str], config_path: Path) -> dict[Path, str]:
    """The launcher config last: it is what says which runtime the role runs, so a write interrupted part-way still
    reads as a switch not yet made, and the same command finishes it instead of calling it done."""
    tail = {config_path: files[config_path]} if config_path in files else {}
    return {**{path: text for path, text in files.items() if path != config_path}, **tail}


#: Where a project artifact describes a role `switchyard new` adds of its own, and what marks the block as that role's:
#: `project.research` with `"stage": "researcher"`, `project.documentation` with `"gate": "librarian"`, and
#: `project.router` with `"enabled": true` (SYRD-899).
ARTIFACT_ROLE_BLOCKS: dict[str, tuple[str, str, object]] = {
    "researcher": ("research", "stage", "researcher"),
    "librarian": ("documentation", "gate", "librarian"),
    "router": ("router", "enabled", True),
}

#: The artifact's per-role tables, and which of the role's settings each records.
ARTIFACT_ROLE_TABLES = (("role_clis", "runtime"), ("role_models", "model"), ("role_efforts", "effort"))


def artifact_role_fields(project: Mapping[str, Any], role_name: str) -> list[str]:
    """The fields of an artifact's `project` that record `role_name`'s runtime, model or effort."""
    fields = [f"project.{key}" for key, _setting in ARTIFACT_ROLE_TABLES
              if isinstance(project.get(key), Mapping) and role_name in project[key]]
    block = ARTIFACT_ROLE_BLOCKS.get(role_name)
    if block is not None and isinstance(project.get(block[0]), Mapping) and project[block[0]].get(block[1]) == block[2]:
        fields.append(f"project.{block[0]}")
    return fields


def _artifact(config_path: Path, raw: Mapping[str, Any], owner_user: str,
              held: Mapping[Path, str] | None = None) -> tuple[Path, dict[str, Any] | None]:
    """The project artifact beside the config -- as `held` already projects it, else as it is -- or None."""
    from scripts import role_runtime as rr
    from scripts.workflow_launcher import _tenant_json

    path = config_path.parent.parent / f'{raw.get("project")}.project.json'
    try:
        text = (held or {}).get(path)
        artifact = json.loads(text) if text is not None else _tenant_json(path, owner_user)
    except (OSError, ValueError) as exc:
        raise rr.RoleRuntimeRefusal(f"switchyard: cannot read {path}, which records the role's model: {exc}") from exc
    return path, artifact if isinstance(artifact, dict) and isinstance(artifact.get("project"), dict) else None


def artifact_role_records(
    config_path: Path,
    raw: Mapping[str, Any],
    *,
    role_name: str,
    runtime: str,
    owner_user: str = "",
    held: Mapping[Path, str] | None = None,
) -> dict[Path, str]:
    """The project artifact recording the role's runtime, model and effort as its launcher entry in `raw` has them.

    {} when there is no artifact, or it records none of the three for this role. `switchyard new --from` makes a
    tenant again from the artifact, and the Researcher's, Librarian's and Router's own blocks give those roles their
    model there (`record_researcher_runtime` and its neighbours), so an artifact left on the old model is a model
    the next tenant made from it starts on. A setting the entry does not carry is left out of the role's block --
    the runtime's default, as on the entry -- and is an empty string in a table, which the artifact reads as unset.
    """
    path, artifact = _artifact(config_path, raw, owner_user, held)
    if artifact is None:
        return {}
    project = artifact["project"]
    fields = artifact_role_fields(project, role_name)
    if not fields:
        return {}
    entry = next(item for item in raw.get("roles") or [] if isinstance(item, dict) and item.get("role") == role_name)
    values = {"runtime": runtime, "model": str(entry.get("model") or ""), "effort": str(entry.get("effort") or "")}
    for key, setting in ARTIFACT_ROLE_TABLES:
        if f"project.{key}" in fields:
            project[key][role_name] = values[setting]
    block = ARTIFACT_ROLE_BLOCKS.get(role_name)
    if block is not None and f"project.{block[0]}" in fields:
        record = project[block[0]]
        for setting, value in values.items():
            record.pop(setting, None)
            if value:
                record[setting] = value
    return {path: json.dumps(artifact, indent=2, sort_keys=True) + "\n"}


def recorded_where(config_path: Path, role_name: str, *, owner_user: str = "") -> list[str]:
    """Each file that records the role's model, and where in it, as `set-role-runtime --dry-run` says it."""
    from scripts.workflow_launcher import _tenant_json

    raw = _tenant_json(config_path, owner_user) or {}
    places = [f"{config_path} (roles: {role_name})"]
    if not raw.get("project"):
        return places
    path, artifact = _artifact(config_path, raw, owner_user)
    fields = artifact_role_fields(artifact["project"], role_name) if artifact is not None else []
    if fields:
        places.append(f"{path} ({', '.join(fields)})")
    return places


def noop_repair(config_path: Path, document: Mapping[str, Any] | None, *, role_name: str, runtime: str,
                declared: bool) -> dict[Path, str]:
    """What a switch to what the role already runs rewrites, without a restart: the tenant's copies of the board's
    declaration (SYRD-558), when the board declares the role, and the artifact's record of the role (SYRD-899) --
    each only where it differs."""
    files = declared_projection(config_path, document) if declared and document is not None else {}
    try:
        raw = json.loads(files[config_path]) if config_path in files else json.loads(config_path.read_text("utf-8"))
    except (OSError, ValueError):
        return stale_projection(files)
    files.update(artifact_role_records(config_path, raw, role_name=role_name, runtime=runtime, held=files))
    return stale_projection(config_last(files, config_path))


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
    from scripts.workflow_manage import apply_files, prior_text

    # As root for the owner, each file is read and replaced by descriptor from the owner's root, so nothing root can
    # read through a link the tenant left is carried into the journal or the projection (SYRD-612).
    owner = config.run_as_user or team_launcher.current_user_name()
    files = runtime_projection_files(
        config_path, role_name=role_name, runtime=runtime, document=document, model=model, effort=effort,
        owner_user=owner,
    )
    # Before anything is written: the role as root will hold it, from the file as re-read above, whose identity must
    # still be the one root's checked load held (SYRD-762).
    projected = (_with_projected_role(config, config_path=config_path, role_name=role_name, text=files[config_path])
                 if team_launcher.root_writes_for(owner) else None)
    if journal is not None:
        # Recorded before the first byte is written, so a crash between files
        # leaves a journal that names every one to put back.
        journal.previous_projection = {str(path): prior_text(path, owner) for path in files}
        if journal_path is not None:
            journal.write(journal_path)
    apply_files(files, owner_user=owner)
    for path in files:
        team_launcher.ensure_owner_file(config, path, runner=runner)
    if projected is not None:
        return projected
    return team_launcher.load_project_config(config.project, config_path)


#: A role's fields root acts on as or for an account, which a runtime projection must leave as root's load held them.
PROJECTION_IDENTITY = ("role", "run_as_user", "workdir", "tmux_session", "target", "slot", "detached", "ephemeral")


def _with_projected_role(config: Any, *, config_path: Path, role_name: str, text: str) -> Any:
    """The configuration root holds, with the role as the projection it just wrote names it.

    Root does not read the tenant's configuration back through the tenant's tree, nor resolve the role directories
    it names as `load_project_config` does to compare them: the role's entry is parsed from what root wrote, by the
    loader's own rule, and the rest is the configuration root already held (SYRD-612).
    """
    from dataclasses import replace

    from scripts import team_launcher

    entry = next(item for item in json.loads(text)["roles"] if isinstance(item, dict) and item.get("role") == role_name)
    held = team_launcher._role_by_name(config, role_name)
    role = team_launcher._role_from_json(config.project, entry, base=config_path.parent, default_workdir=Path(held.workdir))
    # A runtime switch changes the runtime and nothing else. The tenant can rewrite its file between root's checked
    # load and this re-read, and every field root acts on as or for an account -- whose account, which tree, which
    # session -- would otherwise come from that rewrite, unchecked (SYRD-757's check is the load's). So each must be
    # what the load held, or nothing is written (SYRD-762).
    changed = [name for name in PROJECTION_IDENTITY if getattr(role, name) != getattr(held, name)]
    if changed:
        from scripts import role_runtime as rr

        raise rr.RoleRuntimeRefusal(
            f"switchyard: {config_path} now gives {role_name} another {', '.join(changed)} than root read for this "
            "switch; a runtime switch changes only the runtime. Nothing was written.")
    role = team_launcher._with_project_board_env(config, [role])[0]
    return replace(config, roles=[role if item.role == role_name else item for item in config.roles])


def projection_text(path: Path, owner_user: str) -> str | None:
    """A tenant file as it is now, or None: by descriptor as root for the owner (`workflow_manage.prior_text`)."""
    from scripts.workflow_manage import prior_text

    return prior_text(path, owner_user)


def required_text(path: Path, owner_user: str) -> str:
    """`projection_text`, refusing a file that is not there: one read away is never taken for an empty one (SYRD-612)."""
    from scripts import role_runtime as rr

    text = projection_text(path, owner_user)
    if text is None:
        raise rr.RoleRuntimeRefusal(f"switchyard: nothing is at {path} to read")
    return text


def restore_projection(journal: Any) -> None:
    """Put back what the journal recorded: as root for its owner, by descriptor, never through a link (SYRD-612)."""
    from scripts.workflow_manage import atomic, remove

    owner = getattr(journal, "owner_user", "")
    previous = journal.previous_projection or {journal.config_path: journal.previous_config_bytes}
    for name, content in previous.items():
        path = Path(name)
        if content is None:
            remove(path, owner)
        else:
            atomic(path, content, owner_user=owner)
