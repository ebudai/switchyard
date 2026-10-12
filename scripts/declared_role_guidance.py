"""What `switchyard add-role` says on a tenant whose roles come from a declared workflow (SYRD-510).

A declared-workflow tenant's roles are the active roles of its workflow
document: the board routes and authorises from it, and the launcher config is
its projection. `add-role` writes the config and the board's built-in role
lists directly, so on such a tenant it refuses, before anything is written --
correctly, but it said only "use ticket-board-workflow apply to update
configured roles and stages". MEFP's Director, following the director skill's
`add-role` line to add overnight capacity (`luna-7`, codex, detached, not
started), was left without the supported path.

The refusal now names that path for this tenant, every command filled in and
each one a command that parses as printed:
- a pool worker: raise `worker_pool.size`, then `switchyard worker-pool <p>
  apply`, which declares the next member with the stages and transition actors
  of the role the pool is modelled on, at the pool's runtime and that
  runtime's default model and effort;
- any other role: a document entry whose `template_role` names the launcher
  entry it copies (model and effort, and arguments on the same runtime),
  applied through `ticket-board-workflow apply`;
- then, for either, the per-role preparation (worktree, hooks, trust) and a
  start, and `set-role-runtime` for its model or effort.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any


def declared_workflow_add_role_refusal(config: Any, *, config_path: Path, role_name: str) -> str:
    """The refusal for `add-role` on a declared-workflow tenant, naming what to run instead."""
    from scripts.worker_pool import prepare_role_command

    project = str(config.project)
    scripts_dir = Path(__file__).resolve().parent
    workflow = shlex.quote(str(scripts_dir / "ticket-board-workflow"))
    launcher = shlex.quote(str(scripts_dir / "team-launcher"))
    cfg = shlex.quote(str(config_path))
    pool = getattr(config, "worker_pool", None)
    lines = [
        f"switchyard: {project} runs a declared workflow: its roles are the active roles of its workflow document, "
        f"so add-role adds none to it. Nothing was changed.",
    ]
    if pool is not None:
        lines += [
            f"To add a worker to its {pool.name} pool ({pool.size} declared): raise worker_pool.size in {config_path}, "
            "then review and apply the declaration. The new worker takes the pool's next number and the stages and "
            "transition actors of the role the pool is modelled on; it runs the pool's runtime at that runtime's "
            "default model and effort until set-role-runtime below changes them, and starts each ticket with a fresh "
            "session when the pool is ephemeral:",
            f"  switchyard worker-pool {shlex.quote(project)} apply",
            f"  switchyard worker-pool {shlex.quote(project)} apply --apply",
            "then prepare the worker it names, and start it:",
            f"  {prepare_role_command(config_path, '<worker>')}",
            f"  switchyard worker-pool {shlex.quote(project)} start <worker>",
        ]
    lines += [
        f"To add {role_name} as a role of its own: add it to the workflow document with its runtime, template_role "
        "naming the role whose launcher settings it copies (model and effort, and its arguments when the runtime is "
        "the same), that role's stages and transition actors, slot null for a session without a pane, and ephemeral "
        "true for a fresh session per ticket; then review and "
        f"apply the document. `{workflow} show` prints the active document and its revision:",
        f"  {workflow} apply --config {cfg} --document <document.json> --expected-revision <revision> --dry-run",
        f"  {workflow} apply --config {cfg} --document <document.json> --expected-revision <revision> "
        "--journal <before.json>",
        "Then prepare its worktree, hooks and trust, and start it:",
        f"  {prepare_role_command(config_path, role_name)}",
        f"  {launcher} {shlex.quote(project)} pane attach-or-start {shlex.quote(role_name)} --config {cfg}",
        "To set either one's model or effort, now or later:",
        f"  switchyard set-role-runtime {shlex.quote(project)} {shlex.quote(role_name)} --cli <runtime> "
        "--model <model> --effort <effort>",
    ]
    return "\n".join(lines)
