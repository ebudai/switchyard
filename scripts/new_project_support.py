"""The new-project support helpers and stage reporting `switchyard new` uses.

- `SWITCHYARD_DESIGN_FILE_NAME`, `NEW_RESULT_FILE_ENV`,
  `NEW_PROJECT_REQUIRED_ROLES` and `NEW_PROJECT_FIXED_ROLE_NAMES` are the
  shared constants; `NEW_PROJECT_STAGES` names the stages, in order.
- `_dedupe_role_cli_pairs` and `_require_new_project_roles` normalize and check
  the role plan; `print_role_plan_review` shows it before anything exists.
- `_agent_owner_user`, `_project_dir` and `_resolve_project_path` name the
  owner and the project path.
- `_write_initial_switchyard_project_artifact`,
  `_chown_switchyard_project_files` and `_prepare_first_run_auth_worktrees`
  write the first artifact, hand it to the owner and prepare the first-run
  worktrees; `_confirm_switchyard_new` asks before any of it.
- `_report_new_project_to_caller` writes the wrapper's result file as the
  wrapper's own user, never as root; `announce_new_project_presentation` says
  where the window is and asks the wrapper to open it.
- `ProvisioningStages` says which stage is running and what each one cost.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-375), in their original
order. The launcher imports this module and re-exports every name, so
`switchyard_new_command`'s phases and every suite that reaches these through
the launcher reach the same objects, the class included. Every launcher
facility these use, and every name defined here that another definition here
reads when it runs, is read from `team_launcher` when it runs, as it was, so a
patch on the launcher still intercepts. The defaults are bound when each
definition runs, as they were: `announce_new_project_presentation`'s `report`
is the function defined just above it, `ProvisioningStages`' `monotonic` is
`time.monotonic`, and the initial artifact's `implementer_roles` is
`DEFAULT_PROJECT_IMPLEMENTER_ROLES` from its own leaf,
`scripts.ticket_board.project_provision`. The standard-library names are this
module's own imports, the same objects. This module never imports
`team_launcher` at its top.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from scripts.ticket_board.project_provision import DEFAULT_PROJECT_IMPLEMENTER_ROLES

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleSelection

SWITCHYARD_DESIGN_FILE_NAME = "PROJECT_DESIGN.md"
#: Where `switchyard new`, run as root by the installed wrapper, reports the
#: project whose window the wrapper should open next as the person who asked.
#: The wrapper creates and owns the file; root only ever writes it as that
#: person (SYRD-221 UAT, test10).
NEW_RESULT_FILE_ENV = "SWITCHYARD_NEW_RESULT_FILE"
NEW_PROJECT_REQUIRED_ROLES = (
    "director",
)
NEW_PROJECT_FIXED_ROLE_NAMES = frozenset({"designer", "director", "audit"})


def _dedupe_role_cli_pairs(role_clis: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    from scripts import team_launcher as launcher

    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for role, cli in role_clis:
        normalized_role = role.strip().lower()
        if normalized_role in launcher.NEW_PROJECT_RESERVED_ROLE_NAMES:
            if normalized_role not in launcher.NEW_PROJECT_FIXED_ROLE_NAMES:
                raise SystemExit(f"role {normalized_role!r} is reserved")
        else:
            launcher._validate_new_project_implementer_role(normalized_role)
        normalized_cli = launcher._validate_new_project_cli(cli, context=f"CLI for {normalized_role}")
        if normalized_role in seen:
            continue
        seen.add(normalized_role)
        result.append((normalized_role, normalized_cli))
    return tuple(result)


def _require_new_project_roles(role_clis: Sequence[tuple[str, str]]) -> None:
    from scripts import team_launcher as launcher

    roles = {role for role, _cli in role_clis}
    missing = [role for role in launcher.NEW_PROJECT_REQUIRED_ROLES if role not in roles]
    if missing:
        raise SystemExit(f"switchyard: required roles missing: {', '.join(missing)}")


def print_role_plan_review(
    plan: Sequence[RoleSelection], *, print_func: Callable[[str], None] = print
) -> None:
    """The summary shown before anything is created.

    Every role, its runtime, its model and its effort, in one place and before
    the first account exists. A provisioning run that only reveals what it chose
    by creating it is one an operator cannot check (SYRD-115).
    """
    print_func("switchyard: roles to create:")
    for selection in plan:
        parts = [selection.cli]
        if selection.model:
            parts.append(selection.model)
        if selection.effort:
            parts.append(f"effort {selection.effort}")
        print_func(f"  {selection.role}: {' -> '.join(parts)}")


def _agent_owner_user(agent_name: str) -> str:
    agent = agent_name.strip()
    if not agent:
        raise SystemExit("switchyard: agent name cannot be empty")
    return agent if agent.endswith("-agent") else f"{agent}-agent"


def _project_dir(home_base: Path, owner_user: str, project_name: str) -> Path:
    from scripts import team_launcher as launcher

    return home_base / owner_user / "Projects" / launcher._slug_from_project_name(project_name)


def _resolve_project_path(raw_path: Path | str) -> Path:
    return Path(os.path.expandvars(str(raw_path))).expanduser().resolve(strict=False)


def _write_initial_switchyard_project_artifact(
    *,
    project_name: str,
    slug: str,
    owner_user: str,
    project_dir: Path,
    artifact_path: Path,
    design_document: Path,
    owner_shell: str,
    implementer_roles: Sequence[str] = DEFAULT_PROJECT_IMPLEMENTER_ROLES,
    role_clis: Sequence[tuple[str, str]] | None = None,
    role_models: Mapping[str, str] | None = None,
    role_efforts: Mapping[str, str] | None = None,
    include_designer: bool = True,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
    agy_credential_source: str = "",
    agy_credential_source_origin: str = "unset",
) -> None:
    from scripts import team_launcher as launcher

    resolved_audit_roles = tuple(audit_roles) if audit_roles is not None else (("audit",) if include_audit else ())
    role_overlap = set(implementer_roles) & set(resolved_audit_roles)
    if role_overlap:
        raise SystemExit(f"roles cannot be both implementers and auditors: {', '.join(sorted(role_overlap))}")
    artifact = launcher.ProjectDesignArtifact(
        project=slug,
        project_name=project_name,
        ticket_prefix=launcher.validate_ticket_prefix(slug),
        owner_user=owner_user,
        repository=project_dir,
        remote="origin",
        default_branch="main",
        worktree_policy="shared",
        design_document=design_document,
        implementer_roles=tuple(implementer_roles),
        audit_roles=resolved_audit_roles,
        role_clis=launcher._dedupe_role_cli_pairs(
            role_clis
            or launcher._default_role_cli_pairs(
                implementer_roles,
                include_designer=include_designer,
                include_audit=include_audit,
                audit_roles=resolved_audit_roles,
            )
        ),
        role_models=tuple(sorted((role_models or {}).items())),
        role_efforts=tuple(sorted((role_efforts or {}).items())),
        catalog_version=launcher.runtime_catalog.CATALOG_VERSION if (role_models or role_efforts) else 0,
        include_designer=include_designer,
        include_audit=bool(resolved_audit_roles),
        push_policy="director-main-only",
        gates=dict(launcher.PROJECT_DESIGN_DEFAULT_GATES),
        capability_grants={
            **launcher.PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS,
            "shell": owner_shell,
            "agy_credential_source": agy_credential_source,
            "agy_credential_source_origin": agy_credential_source_origin,
        },
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if include_designer and not design_document.exists():
        design_document.write_text(launcher._project_design_markdown(slug, title=project_name, body="Design in progress."), encoding="utf-8")
    launcher._write_json_atomic(artifact_path, launcher.project_design_artifact_payload(artifact))


def _chown_switchyard_project_files(
    *,
    owner_user: str,
    project_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    from scripts import team_launcher as launcher

    result = runner(["chown", "-R", f"{owner_user}:{owner_user}", str(launcher._switchyard_dir(project_dir))])
    if result.returncode != 0:
        raise SystemExit(f"switchyard: failed to assign {launcher._switchyard_dir(project_dir)} to {owner_user}")


def _prepare_first_run_auth_worktrees(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    from scripts import team_launcher as launcher

    if config.repository is None:
        return
    worktree_runner = runner
    owner_runner_anchor = config.repository or config.pane_launcher
    if config.run_as_user and owner_runner_anchor is not None and (
        config.control_repository is not None or config.pane_launcher is not None
    ):
        worktree_runner = launcher._owner_project_git_runner(
            owner_user=config.run_as_user,
            project_dir=owner_runner_anchor,
            owned_roots=launcher._control_repository_owned_roots(config),
            runner=runner,
        )
    launcher.ensure_project_worktrees(config, refresh=True, runner=worktree_runner)


def _confirm_switchyard_new(
    *,
    slug: str,
    owner_user: str,
    project_name: str,
    project_dir: Path,
    yes: bool,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
) -> None:
    from scripts import team_launcher as launcher

    print_func(f"switchyard: project name: {project_name}")
    print_func(f"switchyard: slug: {slug}")
    print_func(f"switchyard: owner user: {owner_user}")
    print_func(f"switchyard: project path: {project_dir}")
    if yes:
        return
    answer = launcher._read_prompt("Proceed? [y/N]: ", input_func=input_func).strip().lower()
    if answer not in {"y", "yes"}:
        raise SystemExit("switchyard: cancelled")


def _report_new_project_to_caller(
    path: str,
    project: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Write `project` into the wrapper's result file, as the wrapper's user.

    Root writing to a path somebody else chose is the classic way to be made to
    overwrite a file of root's choosing through a symlink. So root does not
    write it at all: a child drops to the sudo caller's uid and gid first, then
    opens the path without following a link and without creating anything, and
    writes only to a regular file that caller already owns. Whatever the path
    turns out to be, nothing is written that the caller could not have written
    themselves.
    """
    from scripts import team_launcher as launcher

    env = os.environ if environ is None else environ
    uid = launcher._int_env(env.get("SUDO_UID"))
    gid = launcher._int_env(env.get("SUDO_GID"))
    if not path or uid is None or gid is None or not launcher.PROJECT_SLUG_RE.fullmatch(project):
        return False

    def write() -> None:
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
                raise PermissionError(f"{path} is not a regular file of uid {uid}")
            os.write(fd, project.encode("ascii"))
        finally:
            os.close(fd)

    return launcher._run_as_account(uid, gid, write)


def announce_new_project_presentation(
    project: str,
    *,
    resolved_layout_mode: str,
    environ: Mapping[str, str] | None = None,
    report: Callable[..., bool] = _report_new_project_to_caller,
    print_func: Callable[[str], None] = print,
) -> None:
    """Say where the new project's window is, and arrange for it to open.

    Only a layout that opens its own window from here may say one was opened.
    The viewer does not: it is one detached tmux session, and root has no screen
    to show it on. That used to be reported as "full pane window started", and
    on test10 the command then returned with no window and no hint (SYRD-221
    UAT). When the installed wrapper is waiting, it is told which project to
    open and opens it as the person who asked; when it is not, this says how.
    """
    from scripts import team_launcher as launcher

    env = os.environ if environ is None else environ
    if resolved_layout_mode != launcher.LAYOUT_MODE_VIEWER:
        print_func(f"switchyard: full pane window started for {project}")
        return
    result_file = str(env.get(launcher.NEW_RESULT_FILE_ENV) or "")
    if result_file and report(result_file, project, environ=env):
        print_func(
            f"switchyard: every pane of {project} is up; its window opens next, in your "
            "own session"
        )
        return
    print_func(
        f"switchyard: every pane of {project} is up, but this command runs as root, "
        f"which has no screen, so it cannot open the window. Open it from your desktop "
        f"session with: switchyard {project}"
    )


class ProvisioningStages:
    """Say which stage `switchyard new` is in, and how long each one took.

    A fresh project took about a minute on a modest host and the operator could
    not tell which part was taking the time, or whether a quiet screen was a
    wait for them or a stall (SYRD-248). One line as each stage starts, marked
    when it is waiting for the operator, and one line at the end naming what
    each stage cost.
    """

    def __init__(
        self,
        names: Sequence[str],
        *,
        print_func: Callable[[str], None] = print,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.names = tuple(names)
        self.print_func = print_func
        self.monotonic = monotonic
        self.started: float | None = None
        self.current: tuple[str, float] | None = None
        self.durations: list[tuple[str, float]] = []

    def begin(self, name: str, *, waits_for_you: bool = False) -> None:
        now = self.monotonic()
        if self.started is None:
            self.started = now
        self._close(now)
        self.current = (name, now)
        index = self.names.index(name) + 1 if name in self.names else len(self.durations) + 1
        self.print_func(
            f"switchyard: [{index}/{len(self.names)}] {name}"
            + (" -- this step waits for you" if waits_for_you else "")
            + f" ({now - self.started:.1f}s in)"
        )

    def finish(self) -> None:
        now = self.monotonic()
        self._close(now)
        if self.started is None:
            return
        self.print_func(
            f"switchyard: provisioned in {now - self.started:.1f}s: "
            + ", ".join(f"{name} {seconds:.1f}s" for name, seconds in self.durations)
        )

    def _close(self, now: float) -> None:
        if self.current is not None:
            name, since = self.current
            self.durations.append((name, now - since))
            self.current = None


#: The stages `switchyard new` reports, in order.
NEW_PROJECT_STAGES = (
    "host and agent CLI checks",
    "project accounts and files",
    "database and board",
    "provider sign-in and folder trust",
    "role panes",
)
