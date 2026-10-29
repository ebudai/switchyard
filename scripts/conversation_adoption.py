"""A control role resumes a legacy conversation of its own only by an explicit adoption (SYRD-694).

MEFP's Director conversation was begun before dedicated role worktrees existed, so Codex files it under the
project repository rather than the Director's worktree. SYRD-638 rightly refuses to resume it: a conversation
filed under another workspace is foreign, and nothing in a record says whose it is. The User restored it with
Codex's in-pane `/resume` (All conversations), and the next start set it aside again and began fresh.

This is the one narrow way past that refusal, and it changes nothing about any other conversation:

- `switchyard adopt-conversation <project> <role> <session-id>` previews. It names the provider, where the
  provider holds the conversation and the workspace it files it under, the role's worktree, the evidence for
  each check, every state change, and a digest. It changes nothing.
- `... --confirm <digest>` checks everything again, and if the preview still has that digest asks for the
  catalogued privileged action `adopt-conversation` with the role, the conversation and that digest. Its root
  helper proves the caller is that role's registered control pane; root then computes the plan again, as the
  tenant account, with the shared release's code (`--plan-json`), and records that plan's own values only if it
  is clean and has that digest (`conversation_adoption_record`). Every role shares the tenant account, so a
  record in the tenant's own store could be any role's; root's cannot. Provider content is never touched.
- `... --rollback` previews, and `--rollback --confirm <digest>` asks for `withdraw-conversation-adoption`,
  which removes root's record and nothing else. The conversation is kept; the next start refuses it again.

What is checked before anything is asked for:
- the role is the project's control role, on a runtime that resumes from a provider store;
- the provider's store holds the conversation, and files it under exactly one workspace;
- that workspace is the trusted project repository (`repository` in the project's configuration), or an
  earlier worktree of this same role. That is proven by another conversation this role owns
  (`conversation-owners`), which the provider files there. An adopted conversation proves nothing, so
  adoptions cannot be chained;
- it is not any other role's worktree, nor where another role's owned conversation ran. Any other directory,
  or a path the provider does not record, is refused;
- no role owns the conversation but this one, and no other role's pane recorded it (SYRD-638's own checks);
- it is not live: no process of this account names it, and no running pane's record names it;
- neither the conversation nor the role has an adoption already.

At the role's next start (`install_adopted_record`), while the conversation has no owner record yet, the role's
record is pointed at it; the record it had is kept as `.superseded`. SYRD-638's `judge` accepts that exact
conversation for that exact project, role, pane, runtime and worktree, from exactly the adopted workspace, with proof
`adopted`, only while root's record says so; every other check still applies. Its first resume claims it
(`how: adopted`), so a role that later moves on is not pulled back. `switchyard status` says the conversation is
adopted and where it began, rather than claiming it began in the worktree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from scripts import conversation_adoption_record as root_record

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig

SCHEMA = root_record.PLAN_SCHEMA
#: The `source` of the record a start points at the adopted conversation.
ADOPTED_SOURCE = "switchyard.adopt-conversation"
#: Provider conversation ids are UUID-shaped. Anything else -- a glob character, a path -- is refused before it
#: reaches a store lookup.
SESSION_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z-]{7,127}$")
#: How many owned conversations are read for an earlier-worktree proof.
MAX_OWNED = 256


@dataclass
class Plan:
    """What an adoption, or its rollback, would do, and every reason it may not."""

    action: str
    project: str
    role: str
    target: str = ""
    runtime: str = ""
    session_id: str = ""
    location: str = ""
    workspace: str = ""
    source: str = ""
    worktree: str = ""
    record: str = ""
    recorded_now: str = ""
    evidence: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    #: What a rollback removes: root's record as it stands.
    existing: dict[str, Any] | None = None

    @property
    def body(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "action": self.action, "project": self.project, "role": self.role,
                "target": self.target, "runtime": self.runtime, "session_id": self.session_id,
                "location": self.location, "workspace": self.workspace, "source": self.source,
                "worktree": self.worktree, "record": self.record, "recorded_now": self.recorded_now,
                "existing": self.existing}

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @property
    def action_values(self) -> dict[str, str]:
        """The catalogued privileged action's typed values: the role, the conversation, and for an adoption the
        digest root must find when it computes this plan again."""
        values = {"role": self.role, "conversation": self.session_id}
        return values if self.action == "rollback" else {**values, "digest": self.digest}

    @property
    def record_values(self) -> dict[str, str]:
        """What root records when it finds this plan again: workspace and worktree as the sha256 of each path."""
        return {"target": self.target, "runtime": self.runtime, "workspace_sha256": path_digest(self.workspace),
                "worktree_sha256": path_digest(self.worktree)}

    def as_json(self) -> str:
        """The plan as root's adopt-conversation operation reads it from the tenant's own run."""
        return json.dumps({"schema": SCHEMA, "project": self.project, "role": self.role, "session_id": self.session_id,
                           "digest": self.digest, "problems": self.problems,
                           "values": self.record_values if not self.problems else {}}, sort_keys=True)


def _lexical(path: str) -> str:
    """A provider's recorded workspace as written, normalized but never resolved: a link made since proves nothing."""
    return os.path.normpath(path) if os.path.isabs(path) else ""


def _forms(path: str | Path | None) -> set[str]:
    """A configured path as written and as it resolves now; empty for none."""
    if not path:
        return set()
    return {os.path.normpath(str(path)), os.path.realpath(str(path))}


def path_digest(path: str) -> str:
    """How root's record binds a directory: the sha256 of its normalized path, so no path reaches root."""
    return hashlib.sha256(os.path.normpath(path).encode("utf-8")).hexdigest()


def _role(config: ProjectConfig, name: str) -> RoleConfig | None:
    return next((role for role in config.roles if role.role == name), None)


def _directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def adopted(role: RoleConfig, session_dir: Path, session_id: str, runtime: str,
            workspaces: tuple[str, ...]) -> dict[str, Any] | None:
    """Root's adoption that makes this conversation, filed under `workspaces`, this role's; None when there is none.

    It must be root's own record (`conversation_adoption_record.read`), for exactly this role, pane, runtime and
    worktree, and the provider must still file the conversation under exactly the workspace that was adopted: both
    directories are bound by the sha256 of their normalized paths.
    """
    if not SESSION_ID.fullmatch(session_id or ""):
        return None
    record = root_record.read(session_id)
    if record is None:
        return None
    if (record.get("project"), record.get("role"), record.get("target"), record.get("runtime")) != (
            _project_of(role), role.role, role.target, runtime):
        return None
    if record.get("worktree_sha256") not in {path_digest(form) for form in _forms(role.workdir)}:
        return None
    lexical = {_lexical(workspace) for workspace in workspaces}
    if len(lexical) != 1 or "" in lexical or path_digest(lexical.pop()) != record.get("workspace_sha256"):
        return None
    return record


def _project_of(role: RoleConfig) -> str:
    """The project a loaded role belongs to, as its configuration gives it (`TICKET_BOARD_PROJECT`)."""
    return str((role.env or {}).get("TICKET_BOARD_PROJECT") or "")


def _adoptions_for(config: ProjectConfig) -> list[dict[str, Any]]:
    return [record for record in root_record.listing() if record.get("project") == config.project]


# --------------------------------------------------------------------------
# The checks.
# --------------------------------------------------------------------------


def _owned(config: ProjectConfig, session_dir_for: Callable[[RoleConfig], Path], exclude: str) -> list[tuple[RoleConfig, str, dict]]:
    """Conversations the project's owner records give to a role, as (role, id, owner record), adopted ones excluded."""
    from scripts import session_provenance

    roles = {role.role: role for role in config.roles}
    if not config.roles:
        return []
    owners = session_provenance._project_store(config.roles[0], session_dir_for(config.roles[0])) / session_provenance.OWNERS_DIR
    if not _directory(owners):
        return []
    found = []
    try:
        entries = sorted(owners.iterdir())[:MAX_OWNED]
    except OSError:
        return []
    for path in entries:
        if path.name == exclude or not SESSION_ID.fullmatch(path.name):
            continue
        raw = session_provenance._read_bounded(path)
        try:
            owner = json.loads(raw.decode("utf-8")) if raw is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(owner, dict) and owner.get("role") in roles and owner.get("how") != "adopted":
            found.append((roles[owner["role"]], path.name, owner))
    return found


def approved_source(config: ProjectConfig, role: RoleConfig, workspace: str, *,
                    session_dir_for: Callable[[RoleConfig], Path], session_id: str) -> tuple[str, str]:
    """(why `workspace` is an approved legacy workspace of this role, "") or ("", why it is not)."""
    from scripts import session_provenance

    if not workspace:
        return "", "it is not an absolute path, so nothing shows where it is"
    for other in config.roles:
        if other.role != role.role and workspace in _forms(other.workdir):
            return "", f"it is {other.role}'s worktree"
    earlier = ""
    for owner_role, owned_id, _owner in _owned(config, session_dir_for, session_id):
        runtime = session_provenance.role_runtime(owner_role)
        if not runtime:
            continue
        held, location, workspaces = session_provenance._workspaces(
            owner_role, owned_id, None, session_dir_for(owner_role), runtime)
        if not held or not workspaces or workspace not in {_lexical(w) for w in workspaces}:
            continue
        if owner_role.role != role.role:
            return "", f"{owner_role.role} ran there: its conversation {owned_id} is filed there ({location})"
        earlier = earlier or (f"an earlier worktree of {role.role}: {role.role}'s own conversation {owned_id} "
                              f"(owner record {session_provenance.OWNERS_DIR}/{owned_id}) is filed there ({location})")
    if workspace in _forms(config.repository):
        return f"the project repository ({config.repository}), from {config.project}'s configuration", ""
    if earlier:
        return earlier, ""
    return "", (f"it is neither {config.project}'s project repository ({config.repository or 'none configured'}) "
                f"nor a worktree any conversation of {role.role}'s shows it ran in")


def _ancestors(proc: Path) -> set[int]:
    """This process and every process above it: the command asking, and root's run of it, name the id too."""
    found, pid = set(), os.getpid()
    while pid > 1 and pid not in found:
        found.add(pid)
        try:
            raw = (proc / str(pid) / "stat").read_text(encoding="utf-8")
            pid = int(raw[raw.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return found


def _process_naming(session_id: str, *, proc: Path) -> str:
    """A process of this account, other than this one and those above it, whose command line names the conversation."""
    uid = os.getuid()
    try:
        entries = [entry for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return ""
    above = _ancestors(proc) | {os.getpid()}
    for entry in entries:
        if int(entry.name) in above:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(session_id.encode() in arg for arg in argv):
            return f"process {entry.name} ({os.path.basename(argv[0].decode('utf-8', 'replace'))}) names it"
    return ""


def live_where(config: ProjectConfig, session_id: str, *, session_dir_for: Callable[[RoleConfig], Path],
               runner: Callable[..., Any], proc: Path) -> str:
    """Where the conversation is live now, or "": a process naming it, or a running pane whose record names it."""
    from scripts import team_launcher as launcher

    if found := _process_naming(session_id, proc=proc):
        return found
    for other in config.roles:
        if launcher.session_id_for_role(other, session_dir_for(other)) != session_id:
            continue
        if runner(launcher.tmux_has_session_args(other), capture_output=True, text=True).returncode == 0:
            return f"{other.role}'s pane {other.tmux_session} is running and its record names it"
    return ""


def plan_adoption(config: ProjectConfig, role_name: str, session_id: str, *, config_path: Path | None,
                  session_dir_for: Callable[[RoleConfig], Path], runner: Callable[..., Any],
                  proc: Path = Path("/proc")) -> Plan:
    """What adopting `session_id` for `role_name` would do; `problems` empty when it may."""
    from scripts import session_provenance
    from scripts import team_launcher as launcher

    plan = Plan("adopt", config.project, role_name, session_id=session_id)
    role = _role(config, role_name)
    if not SESSION_ID.fullmatch(session_id):
        plan.problems.append(f"{session_id!r} is not a conversation id")
    if role is None:
        plan.problems.append(f"{config.project} has no role {role_name!r}")
    if plan.problems:
        return plan
    control, reason = launcher.control_role_name(config, config_path=config_path)
    if role.role != control:
        plan.problems.append(f"only {config.project}'s control role ({control or reason}) adopts a legacy conversation")
    account = launcher.role_run_as_user(config, role)
    if account and account != config.run_as_user:
        plan.problems.append(f"{role.role} runs as {account}, not {config.project}'s account {config.run_as_user}")
        return plan
    session_dir = session_dir_for(role)
    runtime = session_provenance.role_runtime(role)
    plan.target, plan.runtime, plan.worktree = role.target, runtime, os.path.normpath(role.workdir)
    if not runtime:
        plan.problems.append(f"{role.role} keeps its sessions in its own home, so it has nothing to adopt")
        return plan
    held, location, workspaces = session_provenance._workspaces(role, session_id, None, session_dir, runtime)
    plan.location = location
    if not held:
        plan.problems.append(f"{runtime} holds no conversation {session_id} for {config.project} (looked at {location})")
        return plan
    plan.evidence.append(f"{runtime} holds it at {location}")
    if not workspaces:
        plan.problems.append(f"{runtime} records no workspace for it, so nothing shows where it ran")
        return plan
    if len(set(workspaces)) != 1:
        plan.problems.append(f"{runtime} files it under {len(set(workspaces))} workspaces ({', '.join(workspaces)}); "
                             "only a conversation filed under one may be adopted")
        return plan
    plan.workspace = _lexical(workspaces[0])
    plan.evidence.append(f"{runtime} files it under {workspaces[0]}")
    if any(session_provenance._same(w, role.workdir) for w in workspaces):
        plan.problems.append(f"{runtime} already files it under {role.role}'s worktree {role.workdir}, so a normal "
                             "start resumes it; there is nothing to adopt")
        return plan
    plan.source, why_not = approved_source(config, role, plan.workspace, session_dir_for=session_dir_for,
                                           session_id=session_id)
    if why_not:
        plan.problems.append(f"{workspaces[0]} is not an approved legacy workspace of {role.role}: {why_not}")
    else:
        plan.evidence.append(f"{workspaces[0]} is {plan.source}")
    owner = session_provenance.owner_of(role, session_dir, session_id)
    if owner and owner != role.role:
        plan.problems.append(f"it is {owner}'s ({session_provenance.OWNERS_DIR}/{session_id})")
    else:
        plan.evidence.append(f"no other role owns it ({'owned by ' + owner if owner else 'no owner record'})")
    if elsewhere := session_provenance.named_elsewhere(role, session_dir, session_id):
        plan.problems.append(f"another role recorded it: {elsewhere}")
    else:
        plan.evidence.append("no other role's records or history name it")
    if peers := getattr(role, "workdir_peers", ()):
        plan.problems.append(f"{role.role}'s worktree is also {', '.join(peers)}'s, so no start could say whose it is")
    if live := live_where(config, session_id, session_dir_for=session_dir_for, runner=runner, proc=proc):
        plan.problems.append(f"it is live: {live}")
    else:
        plan.evidence.append("it is not live: no process of this account and no running pane's record names it")
    plan.record = str(root_record.ROOT / f"{session_id}.json")
    if (existing := root_record.read(session_id)) is not None:
        plan.problems.append(f"it is adopted already ({plan.record}, for {existing.get('project')}'s "
                             f"{existing.get('role')})")
    for other in _adoptions_for(config):
        if other.get("role") == role.role and other.get("session_id") != session_id:
            plan.problems.append(f"{role.role} has adopted {other.get('session_id')} already; roll that back first")
    plan.recorded_now = launcher.session_id_for_role(role, session_dir)
    if plan.problems:
        return plan
    record_path = session_dir / launcher.session_file_name(role.target)
    plan.changes = [
        f"root writes {plan.record}, through the catalogued privileged action adopt-conversation, once its helper "
        f"has proven this runs in {role.role}'s registered pane: {role.role} ({role.target}) may resume {runtime} "
        f"conversation {session_id}, filed under {plan.workspace}, from its worktree {plan.worktree}",
        f"at {role.role}'s next start, while {session_provenance.OWNERS_DIR}/{session_id} does not exist: "
        f"{record_path} is pointed at {session_id}"
        + (f"; the record it holds now ({plan.recorded_now}) is kept as {record_path.name}.superseded"
           if plan.recorded_now else ""),
        f"at its first resume, {session_provenance.OWNERS_DIR}/{session_id} records it as {role.role}'s "
        "(how: adopted)",
        f"nothing else: {runtime}'s conversation and every other role's records are not touched",
    ]
    return plan


def plan_rollback(config: ProjectConfig, role_name: str, session_id: str) -> Plan:
    plan = Plan("rollback", config.project, role_name, session_id=session_id)
    role = _role(config, role_name)
    if role is None or not SESSION_ID.fullmatch(session_id):
        plan.problems.append(f"{config.project} has no role {role_name!r}" if role is None
                             else f"{session_id!r} is not a conversation id")
        return plan
    plan.record, plan.target = str(root_record.ROOT / f"{session_id}.json"), role.target
    record = root_record.read(session_id)
    if record is None:
        plan.problems.append(f"root has no adoption of {session_id} ({plan.record})")
        return plan
    if (record.get("project"), record.get("role")) != (config.project, role.role):
        plan.problems.append(f"{plan.record} adopts it for {record.get('project')}'s {record.get('role')}, not "
                             f"{config.project}'s {role.role}")
        return plan
    plan.existing = record
    plan.changes = [
        f"root removes {plan.record}, through the catalogued privileged action withdraw-conversation-adoption, and "
        "nothing else",
        f"the conversation is kept; {role.role}'s next start refuses it again, as for any conversation filed "
        f"under another workspace (SYRD-638)",
    ]
    return plan


def render(plan: Plan) -> list[str]:
    verb = "adopting" if plan.action == "adopt" else "rolling back the adoption of"
    lines = [f"switchyard: {verb} {plan.session_id} for {plan.project}'s {plan.role} -- preview (nothing has changed)"]
    if plan.action == "adopt" and plan.runtime:
        lines += [f"  provider:  {plan.runtime}, at {plan.location or '-'}",
                  f"  source:    {plan.workspace or '-'}" + (f" ({plan.source})" if plan.source else ""),
                  f"  target:    {plan.role} ({plan.target}), worktree {plan.worktree}"]
    lines += [f"  evidence:  {item}" for item in plan.evidence]
    if plan.existing:
        lines.append(f"  record:    {json.dumps(plan.existing, sort_keys=True)}")
    lines += [f"  changes:   {item}" for item in plan.changes]
    lines += [f"  refused:   {item}" for item in plan.problems]
    if not plan.problems:
        lines.append(f"switchyard: to {'apply' if plan.action == 'adopt' else 'roll back'} exactly this, run from "
                     f"{plan.role}'s own pane: `switchyard adopt-conversation {plan.project} {plan.role} "
                     f"{plan.session_id}{' --rollback' if plan.action == 'rollback' else ''} --confirm {plan.digest}`")
    return lines


# --------------------------------------------------------------------------
# The role's next start.
# --------------------------------------------------------------------------


def install_adopted_record(role: RoleConfig, session_dir: Path, entry: str) -> str:
    """At a start: point the role's record at its adopted conversation until its first resume; the id, or ""."""
    from scripts import session_provenance
    from scripts import team_launcher as launcher
    from scripts.role_sessions import _decided

    for record in root_record.listing():
        if (record.get("project"), record.get("role"), record.get("target")) != (_project_of(role), role.role,
                                                                                  role.target):
            continue
        session_id = str(record.get("session_id") or "")
        if session_provenance.owner_of(role, session_dir, session_id):
            continue  # resumed already, or owned by somebody: the record is the role's own business from here
        previous = launcher.session_id_for_role(role, session_dir)
        if previous == session_id:
            return session_id
        launcher.clear_session_record_for_role(role, session_dir)
        name = launcher.session_file_name(role.target)
        tmp = session_dir / f".{name}.{os.getpid()}.adopted"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"target": role.target, "session_id": session_id, "updated_at": time.time(),
                                         "source": ADOPTED_SOURCE, "payload": None}, sort_keys=True) + "\n")
            os.link(tmp, session_dir / name, follow_symlinks=False)  # never over a record written meanwhile
        except OSError as exc:
            print(f"team-launcher: could not point {role.role}'s record at adopted {session_id}: {exc}", file=sys.stderr)
            return ""
        finally:
            tmp.unlink(missing_ok=True)
        _decided(role, session_dir, entry, "adoption_installed",
                 f"team-launcher: {role.role}'s record now names {session_id}, adopted by root's record "
                 f"{root_record.ROOT / (session_id + '.json')}"
                 + (f"; the record that named {previous} is kept as superseded" if previous else ""), session_id)
        return session_id
    return ""


# --------------------------------------------------------------------------
# What status says.
# --------------------------------------------------------------------------


def status_lines(config: ProjectConfig) -> list[str]:
    """One line per adopted conversation: it is adopted, from where, and whether a start has resumed it yet."""
    from scripts import session_provenance
    from scripts import team_launcher as launcher

    lines = []
    for record in _adoptions_for(config):
        role = _role(config, str(record.get("role") or ""))
        session_id = str(record.get("session_id") or "")
        session_dir = launcher.role_session_dir(config, role) if role is not None else None
        resumed = role is not None and session_provenance.owner_of(role, session_dir, session_id) == role.role
        began = "outside its worktree"
        if role is not None and (runtime := session_provenance.role_runtime(role)):
            _held, _location, workspaces = session_provenance._workspaces(role, session_id, None, session_dir, runtime)
            if workspaces and path_digest(_lexical(workspaces[0]) or "/") == record.get("workspace_sha256"):
                began = f"in {workspaces[0]}"
        lines.append(
            f"switchyard: {config.project}'s {record.get('role')} resumes {record.get('runtime')} conversation "
            f"{session_id} by adoption (SYRD-694): it began {began}, not in "
            f"{record.get('role')}'s worktree{' ' + role.workdir if role is not None else ''}; "
            + ("resumed since" if resumed else "its next start resumes it")
            + f" ({root_record.ROOT / (session_id + '.json')})")
    return lines


# --------------------------------------------------------------------------
# The command.
# --------------------------------------------------------------------------


def _build_switchyard_adopt_conversation_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard adopt-conversation",
        description=("Adopt a legacy conversation of the control role's, begun outside its worktree, so its normal "
                     "start resumes it. Without --confirm it previews and changes nothing."))
    parser.add_argument("project")
    parser.add_argument("role")
    parser.add_argument("session_id", metavar="session-id")
    parser.add_argument("--rollback", action="store_true", help="remove root's adoption record, and nothing else")
    parser.add_argument("--confirm", default="", metavar="DIGEST", help="apply exactly the preview with this digest")
    parser.add_argument("--plan-json", action="store_true",
                        help="print the plan as one JSON line and change nothing: what root's adopt-conversation "
                             "operation computes again, as this account, before it records anything")
    return parser


def _privileged_action(project: str, action: str, values: dict[str, str], *, print_func: Callable[[str], None]) -> int:
    from scripts.ticket_board.privileged_front_door import privileged_action_command

    return privileged_action_command(project, action, values, print_func=print_func)


def adopt_conversation_command(config: ProjectConfig, *, config_path: Path | None, role: str, session_id: str,
                               rollback: bool = False, confirm: str = "", plan_json: bool = False,
                               runner: Callable[..., Any] | None = None, proc: Path = Path("/proc"),
                               privileged: Callable[..., int] = _privileged_action,
                               print_func: Callable[[str], None] = print) -> int:
    import subprocess

    from scripts import team_launcher as launcher

    runner = runner or subprocess.run

    def session_dir_for(member: RoleConfig) -> Path:
        return launcher.role_session_dir(config, member)

    plan = (plan_rollback(config, role, session_id) if rollback else
            plan_adoption(config, role, session_id, config_path=config_path, session_dir_for=session_dir_for,
                          runner=runner, proc=proc))
    if plan_json:
        print_func(plan.as_json())
        return 1 if plan.problems else 0
    if not confirm or plan.problems:
        for line in render(plan):
            print_func(line)
        return 1 if plan.problems or confirm else 0
    if confirm != plan.digest:
        print_func(f"switchyard: --confirm {confirm} is not this preview's digest {plan.digest}: something changed "
                   "since the preview, or it was another one. Nothing was changed; preview again")
        return 1
    action = "withdraw-conversation-adoption" if rollback else "adopt-conversation"
    print_func(f"switchyard: asking for {action}; its helper proves this is {role}'s registered pane, and root "
               "computes this plan again and checks its digest, before root changes anything")
    result = privileged(config.project, action, plan.action_values, print_func=print_func)
    if result != 0:
        print_func(f"switchyard: {action} did not complete (exit {result}); its output above says why")
        return result
    if rollback:
        print_func(f"switchyard: root removed {plan.record}. {session_id} is kept; {role}'s next start refuses it again")
    else:
        print_func(f"switchyard: root recorded {plan.record}. {role}'s next start resumes {session_id}. To undo: "
                   f"`switchyard adopt-conversation {config.project} {role} {session_id} --rollback`")
    return 0
