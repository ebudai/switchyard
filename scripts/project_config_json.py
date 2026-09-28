"""The project config's JSON, read into its roles, board environment and provision plan.

`_role_from_json` turns one role entry into a `RoleConfig` (with `_string_list`
and `_bool_value` checking its list and boolean fields), and
`_with_project_board_env` gives every role the board environment
(`_role_board_env`) and the staged Git template (`role_git_template_env`);
`load_project_config` calls both. `_project_board_provision_from_json` parses a
provision plan written by this release or an older one, filling fields the
document predates from a reference plan (`_plan_migration_reference`, anchored
on `_recorded_owner_home`).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-430), in their original
order. The launcher imports this module and re-exports all nine names;
`load_project_config` still calls the launcher's names, and
`scripts/privileged_runtime_plan.py`, `scripts/project_teardown.py` and
`scripts/runtime_artifact_refresh.py` still read the plan parser there.
Everything they read -- each other included, the role and config types, the
CLI tables and resume defaults, the path, command-name, user and JSON helpers,
the staging directory and the plan builder and migration -- is read through the
launcher at call time, so a suite that rebinds one there still intercepts it.
None has a definition-time default. `role_git_template_env` keeps its own
nested import. This module imports `team_launcher` only inside the functions,
when they run.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig
    from scripts.ticket_board.project_provision import ProjectBoardProvision


def _string_list(value: Any, *, field: str, role: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"role {role} {field} must be a JSON string list")
    return list(value)


def _bool_value(value: Any, *, field: str, role: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise SystemExit(f"role {role} {field} must be a JSON boolean")
    return value


def _role_from_json(project: str, raw: dict[str, Any], *, base: Path, default_workdir: Path | None) -> RoleConfig:
    from scripts import team_launcher as launcher

    role = str(raw.get("role") or "").strip()
    if not role:
        raise SystemExit("team launcher role entry missing role")
    tmux_session = str(raw.get("tmux_session") or f"{project}-{role}").strip()
    slot_raw = raw.get("slot")
    slot = None if slot_raw is None else int(slot_raw)
    cli_raw = raw.get("cli")
    if isinstance(cli_raw, str):
        cli = shlex.split(cli_raw)
    elif isinstance(cli_raw, list) and all(isinstance(item, str) for item in cli_raw):
        cli = list(cli_raw)
    else:
        raise SystemExit(f"role {role} must define cli as a string or string list")
    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict):
        raise SystemExit(f"role {role} env must be a JSON object")
    workdir_raw = raw.get("workdir")
    if workdir_raw is None:
        workdir = str(default_workdir or Path.cwd())
    else:
        workdir = str(launcher._expand_path(str(workdir_raw), base=base))
    cli_name = launcher._command_name(cli[0])
    if cli_name not in launcher.SUPPORTED_CONFIG_CLI_NAMES:
        raise SystemExit(
            f"role {role} cli {cli_name!r} is not supported; "
            f"supported clis: {', '.join(launcher.SUPPORTED_CONFIG_CLI_NAMES)}"
        )
    resume_mode = str(raw.get("resume_mode") or launcher.DEFAULT_RESUME_MODE_BY_CLI.get(cli_name, "flag")).strip()
    resume_flag = str(raw.get("resume_flag") or launcher.DEFAULT_RESUME_FLAG_BY_CLI.get(cli_name, "--resume")).strip()
    resume_subcommand = str(raw.get("resume_subcommand") or launcher.DEFAULT_RESUME_SUBCOMMAND_BY_CLI.get(cli_name, "resume")).strip()
    return launcher.RoleConfig(
        role=role,
        slot=slot,
        detached=launcher._bool_value(raw.get("detached"), field="detached", role=role),
        tmux_session=tmux_session,
        target=str(raw.get("target") or f"{tmux_session}:0.0").strip(),
        workdir=workdir,
        cli=cli,
        model=str(raw.get("model") or "").strip(),
        model_arg=str(raw.get("model_arg") or launcher.DEFAULT_MODEL_ARG_BY_CLI.get(cli_name, "--model")).strip(),
        effort=str(raw.get("effort") or "").strip(),
        yolo=launcher._bool_value(raw.get("yolo"), field="yolo", role=role),
        extra_args=launcher._string_list(raw.get("extra_args"), field="extra_args", role=role),
        resume_mode=resume_mode,
        resume_flag=resume_flag,
        resume_subcommand=resume_subcommand,
        fresh_session_per_ticket=launcher._bool_value(
            raw.get("fresh_session_per_ticket"),
            field="fresh_session_per_ticket",
            role=role,
        ),
        live_commands=launcher._string_list(raw.get("live_commands"), field="live_commands", role=role),
        env={str(key): str(value) for key, value in env_raw.items()},
        run_as_user=str(raw.get("run_as_user") or "").strip(),
        ephemeral=launcher._bool_value(raw.get("ephemeral"), field="ephemeral", role=role),
        presentation_label=str(raw.get("presentation_label") or "").strip(),
    )


def _role_board_env(config: ProjectConfig, role: RoleConfig, session_role_map: dict[str, str]) -> dict[str, str]:
    from scripts import team_launcher as launcher

    env = {
        "TICKET_BOARD_PROJECT": config.project,
        "TICKET_BOARD_PROJECT_NAME": config.project_name,
        "TICKET_BOARD_TICKET_PREFIX": config.ticket_prefix,
        "TICKET_BOARD_URL": config.board_url,
        "TICKET_BOARD_SOCKET": config.board_socket,
        "TICKET_BOARD_CALLER_ROLE": role.role,
        "TICKET_BOARD_CALLER_ROLE_MAP": json.dumps(session_role_map, sort_keys=True, separators=(",", ":")),
    }
    if config.role_state_isolation:
        env["TICKET_BOARD_PROCESS_AUTHORITY"] = "1"
    else:
        # Runtime routing follows only explicit legacy bindings.  The
        # migration renderer may synthesize canonical account names before it
        # creates them; doing that here would misclassify a shared-account PGU
        # config as partially migrated and make ordinary startup require
        # artifacts it never installed (SYRD-66).
        owner = config.run_as_user or launcher.current_user_name()
        accounts = tuple(
            (candidate.role, candidate.run_as_user)
            for candidate in config.roles
            if candidate.run_as_user and candidate.run_as_user != owner
        )
        if accounts:
            env["TICKET_BOARD_ROLE_ACCOUNTS"] = ",".join(
                f"{name}={account}" for name, account in accounts
            )
    if config.run_as_user:
        # directorctl needs the owner's name to reach the display and viewer
        # sessions, which stay in the owner's tmux server (SYRD-39).
        env["SWITCHYARD_PROJECT_OWNER"] = config.run_as_user
    if config.upstream_report_url:
        env["TICKET_BOARD_REPORT_URL"] = config.upstream_report_url
        env["TICKET_BOARD_REPORT_ORIGIN_PROJECT"] = config.project
    if config.upstream_report_token_file:
        env["TICKET_BOARD_TENANT_REPORT_TOKEN_FILE"] = config.upstream_report_token_file
    return env


def role_git_template_env(project: str) -> dict[str, str]:
    """The Git template a role's new repositories are created from, once staged.

    Implementers make their own source checkouts from their panes, with a plain
    `git clone`, and nothing Switchyard installs reached them: those clones had
    no pre-commit hook, so the size warning never fired (SYRD-257). Naming the
    root-staged template here gives every clone or init made from a role pane
    the warning-only policy before its first commit, and every worktree linked
    to it shares that clone's hooks. Only new repositories are affected, and
    only those a role makes -- this is not a global hooksPath. Unset until the
    template is staged, so a pane never points Git at a directory that is not
    there.
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import GIT_TEMPLATE_DIR_NAME

    template = Path(launcher.role_tooling_staging_dir(project)) / GIT_TEMPLATE_DIR_NAME
    if (template / "hooks" / "pre-commit").is_file():
        return {"GIT_TEMPLATE_DIR": str(template)}
    return {}


def _with_project_board_env(config: ProjectConfig, roles: list[RoleConfig]) -> list[RoleConfig]:
    from scripts import team_launcher as launcher

    session_role_map = {role.tmux_session: role.role for role in roles}
    git_env = launcher.role_git_template_env(config.project)
    return [
        replace(
            role,
            env={
                # Beneath the role's own env: a template a role's config names
                # on purpose is not overridden.
                **git_env,
                **role.env,
                **launcher._role_board_env(config, role, session_role_map),
            },
        )
        for role in roles
    ]


def _recorded_owner_home(raw: Mapping[str, Any]) -> str:
    """The owner home a plan records, or the one its board root discloses.

    Every other generated path is anchored on this, so it is established before
    a reference plan can be built and is never guessed at.
    """
    recorded = str(raw.get("owner_home") or "").strip()
    if recorded:
        return recorded
    project = str(raw.get("project") or "").strip()
    board_root = Path(str(raw.get("board_root") or "")).expanduser()
    if project and board_root.name == f"{project}-ticketboard-live":
        return str(board_root.parent)
    return ""


def _plan_migration_reference(raw: Mapping[str, Any]) -> ProjectBoardProvision | None:
    """A current plan for the same project, to take generated names from.

    Built by the same `build_plan` provisioning uses, from the identity and the
    tenant-specific choices the document already records, so a field added
    after the document was written gets the value provisioning would have given
    it rather than an invented one. Nothing here decides what root installs:
    root renders from its own baseline, and a value taken from this reference
    is one the document was missing entirely (SYRD-52).
    """
    from scripts import team_launcher as launcher

    project = str(raw.get("project") or "").strip()
    owner_user = str(raw.get("owner_user") or "").strip()
    owner_home = launcher._recorded_owner_home(raw)
    if not project or not owner_user or not owner_home:
        return None
    recorded: dict[str, Any] = {}
    for name in ("project_name", "ticket_prefix", "database"):
        value = str(raw.get(name) or "").strip()
        if value:
            recorded[name] = value
    port = raw.get("port")
    if isinstance(port, int) and not isinstance(port, bool):
        recorded["port"] = port
    try:
        return launcher.build_plan(
            project=project,
            owner_user=owner_user,
            owner_home=Path(owner_home),
            **recorded,
        )
    except SystemExit:
        return None


def _project_board_provision_from_json(
    path: Path,
    *,
    migrated: list[str] | None = None,
    supplied: Mapping[str, str] | None = None,
    document: Mapping[str, Any] | None = None,
) -> ProjectBoardProvision:
    """Parse a plan written by this release, or by an older one.

    A plan gains fields as the product does, and parsing straight into the
    current strict shape made every already provisioned tenant unupgradable the
    moment one was added. Fields the document predates are filled in first, and
    the caller is told which ones so it can report the migration (SYRD-52).

    `supplied` is what the operator gave on the command line -- `--commit-git-dir`,
    `--source-repo`. It fills a field the document is MISSING, before anything
    is judged unresolved, and never replaces one the document records: the
    tenant's recorded choices stand, and the caller applies the operator's
    values over the parsed plan afterwards exactly as before. Without it, the
    documented repair `switchyard upgrade <project> --commit-git-dir <path>`
    was refused for the one plan it exists to repair: mefp's plan predates
    `commit_git_dir`, its reference plan could not be built, and the parse gave
    up before the operator's value was ever consulted (SYRD-226).
    """
    from scripts import team_launcher as launcher

    # `document` is a plan a privileged caller already read without following
    # anything; `path` then names it for diagnostics only (SYRD-228).
    raw = dict(document) if document is not None else launcher._load_json(path)
    document = dict(raw)
    owner_home = launcher._recorded_owner_home(document)
    if owner_home:
        document.setdefault("owner_home", owner_home)
    from_operator: list[str] = []
    for name, value in (supplied or {}).items():
        if name in launcher.plan_field_names() and document.get(name) in (None, "") and value:
            document[name] = value
            from_operator.append(name)
    fields, added, unresolved = launcher.migrate_plan_document(
        document, reference=launcher._plan_migration_reference(document)
    )
    if "owner_home" in unresolved:
        raise SystemExit(
            f"switchyard: {path} is missing provision field 'owner_home' and it cannot be "
            "derived from board_root"
        )
    if "commit_git_dir" in unresolved:
        # Never regenerated, so no reference plan could ever supply it: it names
        # where this tenant's commits are verified. Advising a re-provision --
        # which would discard the tenant, its tickets and its resumable state --
        # sent operators away from the one supported repair (SYRD-226).
        raise SystemExit(
            f"switchyard: {path} is missing provision field 'commit_git_dir', which no reference "
            "plan can supply: give it with `switchyard upgrade <project> --commit-git-dir <path>`"
        )
    if unresolved:
        raise SystemExit(
            f"switchyard: {path} is missing provision field {unresolved[0]!r} and no reference "
            "plan can be built for it; re-provision the project"
        )
    if migrated is not None:
        migrated.extend(added)
        migrated.extend(f"{name} (from the command line)" for name in from_operator)
    return launcher.ProjectBoardProvision(**{name: fields[name] for name in launcher.plan_field_names()})
