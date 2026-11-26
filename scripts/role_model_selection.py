"""The model and effort a role chose, kept across every restart Switchyard makes (SYRD-579).

Claude's `/model` picker says "Your pick becomes the default for new sessions"
and saves the pick in the account's own settings. Switchyard launches every
role with `--model <configured>` and `--effort <configured>`, and a flag beats
a saved pick. So the first restart after a pick put the role back on what
`switchyard new` had written, whatever the role had been running. Live on Otto,
an upgrade and start restarted every Claude role on `claude-opus-5` after the
operator had picked Opus 5.5 in each, and ops resumed the right conversation
on the old model.

What a role last ran on is in its own conversation. Claude records `model` and
`effort` on every assistant turn of the transcript its session record names
(`last_turn`). `role_model_drift` compares that with the role's configuration,
counting only turns after the configuration's own `model_selected_at`, the time
a choice was last recorded here.

Nothing here picks between the two. A role that drifted is not restarted on
either until somebody says which one it keeps:
- `switchyard upgrade` lists the drift on a dry run and refuses before anything
  changes, unless `--role-models running|configured` is given
  (`hold_role_selections_for_upgrade`). It then checks that its own phases left
  every role's model and effort as they were
  (`role_selections_after_upgrade`).
- A launch that would start or reload a drifted role refuses
  (`refuse_restart_on_another_model`).
- `switchyard reconcile-role-models` records the choice and restarts only the
  live roles whose process does not run it, through the ordinary reload, which
  resumes the recorded conversation (`reconcile_role_models_command`).

A configuration naming a model Switchyard once gave as its default
(`runtime_catalog.RETIRED_DEFAULT_MODELS`: `claude-opus-5`, SYRD-886) is a
stale default, not a choice, where three things hold: the tenant was made when
it was still the default (its artifact's `catalog_version` is older than
`RETIRED_DEFAULT_SINCE_CATALOG`, or it has none), nothing recorded it as a
choice (no `model_selected_at`), and no upgrade has moved the tenant before
(`retired_defaults_moved`). `switchyard upgrade` moves each such role to the
replacement once, listing it on a dry run first (`retired_default_moves`), and
changes nothing else about it. A role it moved carries `retired_default_moved`,
and its session running the retired model is no drift until it has run the
replacement since: the process from before the move takes it at its next
ordinary restart. Every explicit pick, and any other drift, is held as above.

Launcher facilities are read from `scripts.team_launcher` when a function runs.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig

#: When a role's model or effort was last chosen through Switchyard: turns its
#: conversation ran before then were already answered.
SELECTED_AT_KEY = "model_selected_at"
#: The retired defaults an upgrade has already moved this tenant off, at the configuration's top (SYRD-886).
TENANT_MOVED_KEY = "retired_defaults_moved"
#: On a role an upgrade moved: {"from": the retired default, "at": when}. A later recorded choice clears it.
ROLE_MOVED_KEY = "retired_default_moved"
KEEP_RUNNING = "running"
KEEP_CONFIGURED = "configured"
KEEP_CHOICES = (KEEP_RUNNING, KEEP_CONFIGURED)
#: How far back from its end a transcript is read for its last turn.
TRANSCRIPT_READ_LIMIT = 64 * 1024 * 1024
_CHUNK = 1024 * 1024
#: What may be taken from a transcript and printed: an identifier, never text.
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}")
_EFFORT = re.compile(r"[a-z]{1,16}")


@dataclass(frozen=True)
class LastTurn:
    model: str
    effort: str
    at: str


@dataclass(frozen=True)
class RoleDrift:
    role: str
    configured_model: str
    configured_effort: str
    running: LastTurn
    model_differs: bool
    effort_differs: bool

    def describe(self) -> str:
        changes = []
        if self.model_differs:
            changes.append(f"model {self.configured_model} is configured, its session last ran {self.running.model}")
        if self.effort_differs:
            changes.append(f"effort {self.configured_effort} is configured, its session last ran {self.running.effort}")
        return f"{self.role}: " + "; ".join(changes) + f" (last turn {self.running.at or 'undated'})"

    def running_selection(self) -> tuple[str | None, str | None]:
        """What `--keep running` records: only the fields that differ."""
        return (
            self.running.model if self.model_differs else None,
            self.running.effort if self.effort_differs else None,
        )


def _parse_time(text: object) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)


def _assistant_turn(raw: bytes) -> LastTurn | None:
    if b'"assistant"' not in raw:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    # A subagent's turns run on whatever it was given, not on the role's choice.
    if not isinstance(record, dict) or record.get("type") != "assistant" or record.get("isSidechain"):
        return None
    message = record.get("message")
    model = message.get("model") if isinstance(message, dict) else None
    # "<synthetic>" marks a turn Claude wrote itself, with no model behind it.
    if not isinstance(model, str) or not _MODEL_ID.fullmatch(model):
        return None
    effort = record.get("effort")
    return LastTurn(
        model=model,
        effort=effort if isinstance(effort, str) and _EFFORT.fullmatch(effort) else "",
        at=str(record.get("timestamp") or ""),
    )


def last_turn(transcript: Path, *, after: datetime | None = None) -> LastTurn | None:
    """The model and effort of the transcript's last assistant turn, if it ran after `after`.

    Read backwards from the end, without following a link: a transcript can be
    hundreds of megabytes and its last turn is near its end, and root reads it
    during an upgrade from a directory the tenant owns.
    """
    try:
        descriptor = os.open(transcript, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None
        end, carried, read = info.st_size, b"", 0
        while end > 0 and read < TRANSCRIPT_READ_LIMIT:
            start = max(0, end - min(_CHUNK, TRANSCRIPT_READ_LIMIT - read))
            handle.seek(start)
            lines = (handle.read(end - start) + carried).split(b"\n")
            read, end = read + (end - start), start
            # The first piece may be the tail of a line that began earlier.
            carried = lines.pop(0) if start > 0 else b""
            for raw in reversed(lines):
                turn = _assistant_turn(raw)
                if turn is None:
                    continue
                if after is not None:
                    when = _parse_time(turn.at)
                    if when is None or when <= after:
                        return None
                return turn
    return None


def ran_since(transcript: Path, model: str, after: datetime) -> bool:
    """Whether the transcript has an assistant turn on `model` that ran after `after`.

    Read backwards like `last_turn`, and only as far as the first turn at or before `after`.
    """
    try:
        descriptor = os.open(transcript, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return False
        end, carried, read = info.st_size, b"", 0
        while end > 0 and read < TRANSCRIPT_READ_LIMIT:
            start = max(0, end - min(_CHUNK, TRANSCRIPT_READ_LIMIT - read))
            handle.seek(start)
            lines = (handle.read(end - start) + carried).split(b"\n")
            read, end = read + (end - start), start
            carried = lines.pop(0) if start > 0 else b""
            for raw in reversed(lines):
                turn = _assistant_turn(raw)
                if turn is None:
                    continue
                when = _parse_time(turn.at)
                if when is None or when <= after:
                    return False
                if same_model(model, turn.model):
                    return True
    return False


def same_model(configured: str, running: str) -> bool:
    """Whether a configured name means the model a turn ran on.

    A transcript names the model itself ("claude-opus-5-5"); a configuration may
    name an alias ("opus", "opus[1m]") that Claude resolves to the newest model
    of that family, which is still the configured choice.
    """
    want = configured.split("[", 1)[0].strip().lower()
    got = running.split("[", 1)[0].strip().lower()
    if want == got:
        return True
    if want and not any(character.isdigit() for character in want):
        return want in got.split("-")
    return False


def _selected_at(config_path: Path) -> dict[str, datetime]:
    from scripts import team_launcher as launcher

    raw, _problem = launcher.read_tenant_document_no_follow(config_path, what="the project configuration")
    stamps: dict[str, datetime] = {}
    for entry in (raw or {}).get("roles") or []:
        if isinstance(entry, dict):
            when = _parse_time(entry.get(SELECTED_AT_KEY)) if entry.get(SELECTED_AT_KEY) else None
            if when is not None:
                stamps[str(entry.get("role") or "")] = when
    return stamps


def _moved(config_path: Path) -> dict[str, tuple[str, datetime]]:
    """Each role an upgrade moved off a retired default: (the retired model, when)."""
    from scripts import team_launcher as launcher

    raw, _problem = launcher.read_tenant_document_no_follow(config_path, what="the project configuration")
    moved: dict[str, tuple[str, datetime]] = {}
    for entry in (raw or {}).get("roles") or []:
        record = entry.get(ROLE_MOVED_KEY) if isinstance(entry, dict) else None
        if isinstance(record, dict) and isinstance(record.get("from"), str):
            when = _parse_time(record.get("at"))
            if when is not None:
                moved[str(entry.get("role") or "")] = (record["from"], when)
    return moved


def role_model_drift(
    config: ProjectConfig,
    *,
    config_path: Path,
    roles: Sequence[RoleConfig] | None = None,
    moving: Mapping[str, tuple[str, str]] | None = None,
) -> list[RoleDrift]:
    """Claude roles whose own session last ran a model or effort other than the configured one.

    `moving` names roles an upgrade is about to move off a retired default, (old, new), judged as moved now.
    """
    from scripts import team_launcher as launcher
    from scripts.provider_resume import claude_session_store_exists

    stamps = _selected_at(config_path)
    now = datetime.now(timezone.utc)
    moved = {**_moved(config_path), **{name: (old, now) for name, (old, _new) in (moving or {}).items()}}
    drifts: list[RoleDrift] = []
    for role in config.roles if roles is None else roles:
        if launcher._role_cli_name(role) != "claude" or launcher._uses_fresh_session_per_ticket(role):
            continue
        configured_model, configured_effort = (role.model or "").strip(), (role.effort or "").strip()
        # Nothing on the command line, so the provider's own choice already stands.
        if not configured_model and not configured_effort:
            continue
        session_dir = launcher.role_session_dir(config, role)
        session_id = launcher.session_id_for_role(role, session_dir)
        if not session_id:
            continue
        found, location = claude_session_store_exists(
            role, session_id, session_dir=session_dir, record=launcher._session_record_for_role(role, session_dir)
        )
        turn = last_turn(Path(location), after=stamps.get(role.role)) if found else None
        if turn is None:
            continue
        model_differs = bool(configured_model) and not same_model(configured_model, turn.model)
        # A role an upgrade moved off a retired default whose process from before still runs it takes the
        # replacement at its next restart -- until it has run the replacement since, after which the retired
        # model is a pick like any other (SYRD-886).
        if model_differs and role.role in moved:
            old, at = moved[role.role]
            model_differs = not (
                turn.model == old
                and launcher.runtime_catalog.retired_default_replacement("claude", old) == configured_model
                and not ran_since(Path(location), configured_model, at)
            )
        effort_differs = bool(configured_effort and turn.effort) and configured_effort != turn.effort
        if model_differs or effort_differs:
            drifts.append(
                RoleDrift(role.role, configured_model, configured_effort, turn, model_differs, effort_differs)
            )
    return drifts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def record_role_selections(
    config: ProjectConfig,
    *,
    config_path: Path,
    selections: Mapping[str, tuple[str | None, str | None]],
    now: str = "",
) -> None:
    """Write each role's chosen model and effort (None keeps the field) and when it was chosen.

    The project artifact's `role_models` and `role_efforts`, which recorded what
    `switchyard new` was given, follow where they name the role. A recorded
    choice ends a retired default's move record (SYRD-886).
    """
    from scripts import team_launcher as launcher

    when = now or _now()
    owner = config.run_as_user or ""
    raw, problem = launcher.read_tenant_document_no_follow(config_path, what=f"{config.project}'s configuration")
    if raw is None:
        raise SystemExit(f"switchyard: {problem}. Nothing was changed.")
    entries = {str(entry.get("role")): entry for entry in raw.get("roles") or [] if isinstance(entry, dict)}
    missing = sorted(set(selections) - set(entries))
    if missing:
        raise SystemExit(f"switchyard: {config_path} has no role {', '.join(missing)}. Nothing was changed.")
    for role_name, (model, effort) in selections.items():
        for key, value in (("model", model), ("effort", effort)):
            if value is not None:
                entries[role_name].pop(key, None)
                if value:
                    entries[role_name][key] = value
        entries[role_name][SELECTED_AT_KEY] = when
        entries[role_name].pop(ROLE_MOVED_KEY, None)
    launcher._write_json_atomic(config_path, raw, owner_user=owner)
    artifact_path = config_path.parent.parent / f"{config.project}.project.json"
    artifact, _problem = (
        launcher.read_tenant_document_no_follow(artifact_path, what=f"{config.project}'s project artifact")
        if artifact_path.is_file() else (None, "")
    )
    project = (artifact or {}).get("project")
    if not isinstance(project, dict):
        return
    changed = False
    for role_name, chosen in selections.items():
        for index, key in enumerate(("role_models", "role_efforts")):
            table = project.get(key)
            if isinstance(table, dict) and role_name in table and chosen[index] is not None:
                table[role_name] = chosen[index]
                changed = True
    if changed:
        launcher._write_json_atomic(artifact_path, artifact, owner_user=owner)


def _remedy(project: str, prefix: str) -> list[str]:
    return [
        f"{prefix}   keep what each one ran last: switchyard reconcile-role-models {project} --keep running",
        f"{prefix}   or put each back on its configuration: "
        f"switchyard reconcile-role-models {project} --keep configured",
    ]


def refuse_restart_on_another_model(
    config: ProjectConfig,
    *,
    config_path: Path,
    mode: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None] = print,
) -> bool:
    """Whether a launch must stop because a role it would (re)start last ran on another model.

    `start` restarts only the roles that are down, so a running role keeps
    whatever it runs; `reload` restarts them all.
    """
    from scripts import team_launcher as launcher

    if mode == "attach":
        return False
    drifts = role_model_drift(config, config_path=config_path)
    if drifts and mode != "reload":
        running = {role.role for role in launcher._running_project_roles(config, runner=runner)}
        drifts = [drift for drift in drifts if drift.role not in running]
    if not drifts:
        return False
    for drift in drifts:
        print_func(f"team-launcher: not restarting {drift.describe()}")
    print_func(
        f"team-launcher: refusing to launch {config.project}: the flags would replace what "
        "those roles chose. Nothing was started. Say which each keeps:"
    )
    for line in _remedy(config.project, "team-launcher:"):
        print_func(line)
    return True


def _role_selections(config_path: Path) -> dict[str, tuple[str, str, str]]:
    from scripts import team_launcher as launcher

    raw, _problem = launcher.read_tenant_document_no_follow(config_path, what="the project configuration")
    selections: dict[str, tuple[str, str, str]] = {}
    for entry in (raw or {}).get("roles") or []:
        if isinstance(entry, dict):
            cli = entry.get("cli")
            selections[str(entry.get("role") or "")] = (
                str(cli[0]) if isinstance(cli, list) and cli else "",
                str(entry.get("model") or ""),
                str(entry.get("effort") or ""),
            )
    return selections


def _tenant_retired_state(config_path: Path) -> tuple[dict[str, Any], int, dict[str, Any] | None, Path]:
    """The configuration, its artifact's catalog version (0 for none), the artifact, and its path."""
    from scripts import team_launcher as launcher

    raw, problem = launcher.read_tenant_document_no_follow(config_path, what="the project configuration")
    if raw is None:
        raise SystemExit(f"switchyard: {problem}. Nothing was changed.")
    artifact_path = config_path.parent.parent / f"{raw.get('project')}.project.json"
    artifact, _problem = (launcher.read_tenant_document_no_follow(artifact_path, what="the project artifact")
                          if artifact_path.is_file() else (None, ""))
    project = (artifact or {}).get("project")
    version = project.get("catalog_version") if isinstance(project, dict) else 0
    return raw, version if isinstance(version, int) and not isinstance(version, bool) else 0, artifact, artifact_path


def retired_default_moves(config_path: Path) -> dict[str, tuple[str, str]]:
    """Each role still on a stale retired default, and the model that replaced it (SYRD-886).

    Stale only when the tenant was made while it was the default (`RETIRED_DEFAULT_SINCE_CATALOG`), no upgrade has
    moved this tenant off it before (`TENANT_MOVED_KEY`), and nothing recorded it as the role's choice
    (`model_selected_at`). Anything else is somebody's pick, kept.
    """
    from scripts import team_launcher as launcher

    catalog = launcher.runtime_catalog
    raw, version, _artifact, _path = _tenant_retired_state(config_path)
    done = raw.get(TENANT_MOVED_KEY)
    done = set(done) if isinstance(done, list) else set()
    moves: dict[str, tuple[str, str]] = {}
    for entry in raw.get("roles") or []:
        if not isinstance(entry, dict) or entry.get(SELECTED_AT_KEY):
            continue
        cli = entry.get("cli")
        model = str(entry.get("model") or "").strip()
        replacement = catalog.retired_default_replacement(str(cli[0]) if isinstance(cli, list) and cli else "", model)
        if replacement and model not in done and version < catalog.RETIRED_DEFAULT_SINCE_CATALOG.get(model, 0):
            moves[str(entry.get("role") or "")] = (model, replacement)
    return moves


class HeldRoleSelections(dict):
    """Each role's (cli, model, effort) before an upgrade, and the retired-default moves it makes once it succeeds.

    A dict, so every reader of the selections reads them as before. `moves` is written only by
    `role_selections_after_upgrade`, after the upgrade's phases succeeded and only when `apply` (not a dry run): a
    failed upgrade leaves the tenant's configuration as it was (SYRD-886).
    """

    moves: Mapping[str, tuple[str, str]] = {}
    apply: bool = False


def _held(config_path: Path, moves: Mapping[str, tuple[str, str]], *, apply: bool) -> HeldRoleSelections:
    held = HeldRoleSelections(_role_selections(config_path))
    held.moves, held.apply = dict(moves), apply
    return held


def _record_retired_moves(config: ProjectConfig | None, *, config_path: Path, moves: Mapping[str, tuple[str, str]]) -> bool:
    """Move each role to its replacement, model only, mark the tenant moved, and say whether anything was written.

    `model_selected_at` is left alone: the move is nobody's choice, so turns before it still count as drift.
    """
    from scripts import team_launcher as launcher

    raw, _version, artifact, artifact_path = _tenant_retired_state(config_path)
    retired = sorted(model for table in launcher.runtime_catalog.RETIRED_DEFAULT_MODELS.values() for model in table)
    done = raw.get(TENANT_MOVED_KEY) if isinstance(raw.get(TENANT_MOVED_KEY), list) else []
    if not moves and set(retired) <= set(done):
        return False
    when = _now()
    for entry in raw.get("roles") or []:
        name = str(entry.get("role") or "") if isinstance(entry, dict) else ""
        if name in moves:
            entry["model"] = moves[name][1]
            entry[ROLE_MOVED_KEY] = {"from": moves[name][0], "at": when}
    raw[TENANT_MOVED_KEY] = sorted(set(done) | set(retired))
    owner = str(raw.get("run_as_user") or "") if config is None else (config.run_as_user or "")
    launcher._write_json_atomic(config_path, raw, owner_user=owner)
    table = (artifact or {}).get("project", {}).get("role_models") if isinstance((artifact or {}).get("project"), dict) else None
    if isinstance(table, dict) and any(table.get(name) == old for name, (old, _new) in moves.items()):
        for name, (old, new) in moves.items():
            if table.get(name) == old:
                table[name] = new
        launcher._write_json_atomic(artifact_path, artifact, owner_user=owner)
    return True


def _move_text(project: str, role_name: str, old: str, new: str) -> str:
    return (f"{project}'s {role_name} from {old}, a retired default, to {new}; its effort, runtime and conversation "
            "stay as they are, and nothing restarts it for this")


def hold_role_selections_for_upgrade(
    config: ProjectConfig,
    *,
    config_path: Path,
    keep: str = "",
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> dict[str, tuple[str, str, str]] | None:
    """Before an upgrade changes anything: settle any drift, then each role's (cli, model, effort).

    None means the upgrade must not go on. A drift with nothing said about it
    is refused, because every later phase that restarts a role would settle it
    by putting the configured model back.

    A role on a stale retired default is judged as if already on its
    replacement. The selections returned carry the moves, which
    `role_selections_after_upgrade` writes once the upgrade has succeeded; the
    tenant is then marked so it never moves again (SYRD-886). The dry run, and a
    refusal, list what would move.
    """
    moves = retired_default_moves(config_path)
    judged = replace(config, roles=tuple(
        replace(role, model=moves[role.role][1]) if role.role in moves else role for role in config.roles))
    drifts = role_model_drift(judged, config_path=config_path, moving=moves)
    refused = bool(drifts and not keep)
    if dry_run or refused:
        for role_name, (old, new) in moves.items():
            print_func(f"switchyard: would move {_move_text(config.project, role_name, old, new)}")
    for drift in drifts:
        print_func(f"switchyard: {drift.describe()}")
    if drifts and not keep:
        if dry_run:
            print_func(
                f"switchyard: an upgrade of {config.project} would refuse here: restarting those roles "
                "would replace what they chose. Name which each keeps with --role-models running|configured."
            )
            return _held(config_path, moves, apply=False)
        print_func(
            f"switchyard: refusing to upgrade {config.project}: restarting those roles would replace "
            "what they chose. Nothing was stopped or written. Re-run with --role-models running to keep "
            "what each ran last, or --role-models configured to keep the configuration, or first:"
        )
        for line in _remedy(config.project, "switchyard:"):
            print_func(line)
        return None
    if drifts:
        # Keeping the configuration of a role about to move keeps what it moves to: the retired default is no choice.
        selections = {
            drift.role: drift.running_selection() if keep == KEEP_RUNNING
            else ((moves[drift.role][1], None) if drift.role in moves else (None, None))
            for drift in drifts
        }
        verb = "would record" if dry_run else "recorded"
        for role_name, (model, effort) in selections.items():
            kept = " and ".join(f"{name} {value}" for name, value in (("model", model), ("effort", effort)) if value)
            print_func(f"switchyard: {verb} {role_name}'s {kept or 'configured model and effort'} as its choice")
        if not dry_run:
            record_role_selections(judged, config_path=config_path, selections=selections)
    return _held(config_path, moves, apply=not dry_run)


def role_selections_after_upgrade(
    before: Mapping[str, tuple[str, str, str]],
    *,
    config_path: Path,
    code: int,
    print_func: Callable[[str], None] = print,
) -> int:
    """The upgrade's exit code, or 1 when its phases changed a role's model or effort.

    A role whose runtime changed was asked to: the model belongs to the runtime.
    """
    changed = 0
    for role_name, (cli, model, effort) in _role_selections(config_path).items():
        was = before.get(role_name)
        if was is None or was[0] != cli:
            continue
        for name, old, new in (("model", was[1], model), ("effort", was[2], effort)):
            if old != new:
                changed += 1
                print_func(
                    f"switchyard: the upgrade changed {role_name}'s {name} from {old or 'unset'} to "
                    f"{new or 'unset'}, which nothing asked it to do"
                )
    result = 1 if changed else code
    # Only now, after every phase succeeded: a failed upgrade leaves the configuration as it was (SYRD-886).
    if isinstance(before, HeldRoleSelections) and before.apply:
        moves = retired_default_moves(config_path) if result == 0 else before.moves
        project = str(_tenant_retired_state(config_path)[0].get("project") or config_path.stem)
        if result == 0:
            _record_retired_moves(None, config_path=config_path, moves=moves)
        for role_name, (old, new) in moves.items():
            print_func(f"switchyard: {'moved' if result == 0 else 'did not move'} "
                       f"{_move_text(project, role_name, old, new)}"
                       + ("" if result == 0 else ": the upgrade did not succeed, so the next one moves it"))
    return result


def reconcile_role_models_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    roles: Sequence[str] = (),
    keep: str = KEEP_RUNNING,
    model: str | None = None,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    """Record which model each role keeps, and restart only the live roles not running it.

    `--keep running` records what each drifted role's session last ran;
    `--keep configured` records the configuration as chosen; `--model` names a
    model for the named roles outright. A restart is the ordinary reload, which
    resumes the recorded conversation. A role that is down takes the choice at
    its next start, and a busy one is left running and named.
    """
    from scripts import team_launcher as launcher
    from scripts import role_runtime
    from scripts.model_validation import _validate_model_identifier

    known = {role.role: role for role in config.roles}
    unknown = sorted(set(roles) - set(known))
    if unknown:
        raise SystemExit(f"switchyard: {config.project} has no role {', '.join(unknown)}. Nothing was changed.")
    if keep not in KEEP_CHOICES:
        raise SystemExit(f"switchyard: --keep takes {' or '.join(KEEP_CHOICES)}. Nothing was changed.")
    targets = [known[name] for name in roles] if roles else list(config.roles)
    if model is not None:
        if not roles:
            raise SystemExit("switchyard: --model names a model for the roles given with --role. Nothing was changed.")
        try:
            model = _validate_model_identifier(model)
        except ValueError as exc:
            raise SystemExit(f"switchyard: --model {model!r}: {exc}. Nothing was changed.") from exc
        selections: dict[str, tuple[str | None, str | None]] = {role.role: (model, None) for role in targets}
    else:
        drifts = role_model_drift(config, config_path=config_path, roles=targets)
        for drift in drifts:
            print_func(f"switchyard: {drift.describe()}")
        selections = {
            drift.role: drift.running_selection() if keep == KEEP_RUNNING else (None, None) for drift in drifts
        }
    for role_name, chosen in selections.items():
        kept = " and ".join(f"{name} {value}" for name, value in zip(("model", "effort"), chosen) if value)
        print_func(f"switchyard: {'would record' if dry_run else 'recorded'} {role_name}'s "
                   f"{kept or 'configured model and effort'} as its choice")
    if selections and not dry_run:
        record_role_selections(config, config_path=config_path, selections=selections)
        config = launcher.load_project_config(config.project, config_path)
    pane_state_dir = launcher.default_pane_state_dir_for_user(
        config.run_as_user or launcher.current_user_name(), project=config.project
    )
    named = {target.role for target in targets}
    restarted = failed = 0
    for role in (role for role in config.roles if role.role in named):
        # What it runs from now on: a dry run has recorded nothing yet.
        wanted = selections.get(role.role, (None, None))[0] or role.model
        if not wanted:
            continue
        role_runner = launcher.role_process_runner_for(config, role, runner=runner)
        alive = role_runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if alive.returncode != 0:
            continue
        session_dir = launcher.role_session_dir(config, role)
        live = launcher.live_model_for_role(role, session_dir=session_dir, runner=role_runner)
        # A session that chose another model in the provider keeps it until it restarts.
        back_to_configured = role.role in selections and keep == KEEP_CONFIGURED and model is None
        if (same_model(wanted, live) if live else role.role not in selections) and not back_to_configured:
            continue
        if dry_run:
            print_func(
                f"switchyard: would restart {role.role} on {wanted}; "
                "its recorded conversation resumes where it still exists"
            )
            continue
        if role_runtime._is_busy(role, pane_state_dir=pane_state_dir, runner=role_runner):
            print_func(
                f"switchyard: {role.role} is busy, so it was left running; it takes {wanted} at its next "
                "restart, or run this again when it is idle"
            )
            continue
        result = launcher.ensure_visible_role_session_for_viewer(
            role, mode="reload", session_dir=session_dir, pane_state_dir=pane_state_dir,
            bin_user=config.run_as_user, runner=role_runner,
        )
        now_running = (
            launcher.live_model_for_role(role, session_dir=session_dir, runner=role_runner) if result == 0 else ""
        )
        if result != 0 or not same_model(wanted, now_running):
            failed += 1
            print_func(
                f"switchyard: {role.role} did not come back on {wanted} "
                f"(exit {result}, running {now_running or 'nothing'})"
            )
            continue
        restarted += 1
        print_func(
            f"switchyard: restarted {role.role} on {now_running}; "
            "its recorded conversation resumes where it still exists"
        )
    if not (selections or restarted or failed):
        print_func(
            f"switchyard: every named role of {config.project} ran its configured model last; nothing to reconcile"
        )
    return 1 if failed else 0
