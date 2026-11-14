"""`switchyard rebind-director`: replace a Director held by a provider safety hold with one on another runtime (SYRD-738).

When a safety refusal holds the Director's own provider account (SYRD-672), the Director cannot be asked anything --
not to approve its replacement, explain, retry or write a handoff -- and nothing may act as the Director. This is the
operator's one narrow command for that state. It extends two existing boundaries: SYRD-593's root repair while the
Director is down, and SYRD-262's root-only rebind of a declared pane role.

Who: root, elevated by pkexec for a named person (`rollout_journal.resolve_operator`), the same authority
`rebind-workflow-panes` takes. The tenant configuration is the one root has verified (`root_verified_tenant`), and the
board is read only over root's kernel-proven board socket (`board_identity.board_get_for`). The board is written only
through the database owner's connection, by root-only functions that grant nothing to any role (pgu1039).

What it reads, all before anything changes, none of it from the held account:
- the SYRD-737 recovery packet for the named hold, validated and verified against a second read of the board;
- the hold, from that packet: it must be current, and the Director's account must be the one held;
- the declared workflow and its revision, and the Director's registration;
- for each runtime not under a hold, whether it could start for the owner (the launcher's own first-run checks).

Without --apply it previews and prints a review digest. --apply --expect DIGEST does exactly what was previewed:
1. the board records the rebind (`begin_director_rebind`): hold, old identity, new runtime, operator, reason and the
   packet's provenance;
2. the old Director session is stopped, and the command waits until its processes are gone. The held CLI is never
   started, typed into or resumed;
3. its session record is copied aside as held evidence, never as a record a later start would resume, and cleared,
   so the new session starts fresh;
4. the Director's declared runtime moves (`rebind_declared_pane_roles`, compare-and-swap on the reviewed revision);
5. the launcher's projection names the new runtime;
6. the validated packet is written for the new session, which is started and proven registered on the new runtime;
7. the board finishes the rebind: the Director is sent one fixed notice naming the packet file, and the operator one
   event.

Any failure after step 1 undoes what was done, in reverse: the new session, if any, is stopped, and the workflow and
the projection go back to the held runtime. The Director is left stopped -- a rollback never starts the held account
-- so there is one authoritative Director identity: the held runtime, declared and configured, and no live session.
Work stays paused (drain), and the operator is told once. When no alternate runtime is eligible, --apply does only
that: pause and tell.

The hold itself is never cleared here: only `clear-provider-account-hold`, by the Director or the User, does.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

DIRECTOR = "director"
SCHEMA = "switchyard.director-rebind.v1"
#: The registration fields kept as the old Director's identity: identifiers, never a path's contents.
IDENTITY_FIELDS = ("runtime", "actual_target", "process_pid", "process_start_time", "process_uid", "generation",
                   "registered_at")


class RebindRefused(Exception):
    """A rebind that must not happen: said once, nothing changed."""


@dataclass(frozen=True)
class Plan:
    """What a rebind would do, decided before anything changes."""

    project: str
    hold: int
    held_runtime: str
    runtime: str
    model: str
    packet: dict
    revision: int
    document: dict
    declared: dict  # the Director's declared runtime, target and slot
    registration: dict  # its board registration, IDENTITY_FIELDS only; {} when it has none
    eligible: dict  # runtime -> [] when it could start, else why not; held runtimes are left out
    config_sha256: str
    blockers: tuple[str, ...] = ()

    @property
    def provenance(self) -> dict:
        prov = self.packet["provenance"]
        return {"schema": self.packet["schema"], "version": self.packet["version"], "project": prov["project"],
                "hold": self.hold, "generated_at": prov["generated_at"], "db_xmax": prov["db_xmax"],
                "state_digest": prov["state_digest"], "packet_digest": self.packet["packet_digest"]}

    @property
    def digest(self) -> str:
        from scripts.ticket_board.project_provision import workflow_document_digest

        review = {"schema": SCHEMA, "project": self.project, "hold": self.hold, "from": self.held_runtime,
                  "to": self.runtime, "model": self.model, "revision": self.revision,
                  "workflow": workflow_document_digest(self.document), "declared": self.declared,
                  "registration": self.registration, "config": self.config_sha256,
                  "state_digest": self.packet["provenance"]["state_digest"]}
        return "sha256:" + hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()


# -- the board, read over root's proven socket, and written through the database owner --------------------------------
def board_facts(get: Callable[[str], Any], *, project: str, hold: int) -> tuple[dict, dict, int, dict]:
    """(packet, workflow document, revision, Director registration) -- the packet validated and verified current."""
    from scripts.ticket_board import recovery_packet

    try:
        verdict = recovery_packet.fetch_verified(get, project=project, hold=hold)
    except recovery_packet.PacketError as exc:
        raise RebindRefused(f"the board's recovery packet for hold {hold} does not validate: {exc}") from None
    except Exception as exc:  # noqa: BLE001 - a 400 for a hold that is not current, an unreachable board
        raise RebindRefused(f"the board gave no recovery packet for hold {hold}: {exc}") from None
    if not verdict["current"]:
        raise RebindRefused(f"the board changed between two reads ({', '.join(verdict['changed'])}); run it again")
    workflow = get("/api/workflow") or {}
    document = workflow.get("document")
    if not isinstance(document, dict):
        raise RebindRefused("the board runs no declared workflow, so the Director's runtime cannot be rebound")
    rows = ((get("/api/runtime-assignments") or {}).get("assignments") or {})
    row = rows.get(DIRECTOR) if isinstance(rows, dict) else None
    registration = {key: row[key] for key in IDENTITY_FIELDS if isinstance(row, dict) and key in row}
    return verdict["packet"], document, int(workflow.get("revision") or 0), registration


class PsqlAdmin:
    """Root's calls into the board's root-only functions, through the database owner's connection."""

    def __init__(self, database_url: str, runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run):
        self.database_url, self.runner = database_url, runner

    def call(self, function: str, *args: tuple[Any, str]) -> Any:
        values = ["-v", "ON_ERROR_STOP=1"]
        casts = []
        for index, (value, sql_type) in enumerate(args):
            text = "" if value is None else value if isinstance(value, str) else json.dumps(value, sort_keys=True)
            values += ["-v", f"a{index}={text}"]
            casts.append(f"NULLIF(:'a{index}', '')::{sql_type}")  # an empty value is NULL; psql quotes the rest
        result = self.runner(["sudo", "-u", "postgres", "psql", "-X", "-tA", *values, self.database_url, "-f", "-"],
                             input=f"SELECT {function}({', '.join(casts)});\n", text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            said = (result.stderr or "").strip().splitlines()
            raise RuntimeError(said[-1] if said else f"psql exited {result.returncode}")
        out = (result.stdout or "").strip()
        return json.loads(out) if out else None


# -- deciding -----------------------------------------------------------------------------------------------------------
def _director(config: Any) -> Any:
    from scripts import team_launcher as launcher

    role = launcher._role_by_name(config, DIRECTOR)
    if role is None:
        raise RebindRefused(f"{config.project} has no director role")
    return role


def _declared(document: Mapping[str, Any]) -> dict:
    entry = next((r for r in document.get("roles") or [] if isinstance(r, dict) and r.get("name") == DIRECTOR), None)
    if entry is None or not entry.get("runtime"):
        raise RebindRefused("the declared workflow names no runtime for the director")
    return {"runtime": entry.get("runtime"), "target": entry.get("target"), "slot": entry.get("slot")}


def eligibility(config: Any, *, config_path: Path, held: set[str], model: str,
                runner: Callable[..., subprocess.CompletedProcess[Any]]) -> dict[str, list[str]]:
    """Each runtime not under a hold, and why the Director could not start on it ([] when it could)."""
    from scripts import role_runtime, team_launcher as launcher

    role = _director(config)
    owner = config.run_as_user or launcher.current_user_name()
    found: dict[str, list[str]] = {}
    for runtime in launcher.SUPPORTED_CONFIG_CLI_NAMES:
        if runtime in held:
            continue
        found[runtime] = [*role_runtime._readiness_blockers(config, role, runtime, runner=runner),
                          *role_runtime._verifiable_projection_blockers(config_path, DIRECTOR, runtime, model=model,
                                                                        owner_user=owner)]
    return found


def plan_rebind(config: Any, *, config_path: Path, get: Callable[[str], Any], hold: int, runtime: str,
                model: str, runner: Callable[..., subprocess.CompletedProcess[Any]],
                eligible_for: Callable[..., dict[str, list[str]]] | None = None) -> Plan:
    """Everything decided before anything changes. Raises RebindRefused when the hold is not one this answers."""
    from scripts import runtime_projection

    packet, document, revision, registration = board_facts(get, project=config.project, hold=hold)
    this = packet["hold"]
    if not this["director_held"]:
        raise RebindRefused(
            f"hold {hold} is on the {this['runtime']} account, which does not hold the Director; the Director moves "
            "another role's work itself (switchyard set-role-runtime)")
    held = {this["runtime"], *(h["runtime"] for h in packet["other_holds"])}
    declared = _declared(document)
    owner = config.run_as_user or ""
    config_text = runtime_projection.required_text(config_path, owner)
    eligible = (eligible_for or eligibility)(config, config_path=config_path, held=held, model=model, runner=runner)
    blockers: list[str] = []
    if runtime in held:
        blockers.append(f"the {runtime} account is under a safety hold"
                        + (" -- the one this rebind moves the Director off" if runtime == this["runtime"] else ""))
    elif runtime not in eligible:
        blockers.append(f"{runtime!r} is not a runtime the launcher starts")
    else:
        blockers.extend(eligible[runtime])
    if model and runtime not in held:
        blockers.extend(_model_problems(config, runtime, model, runner=runner))
    return Plan(project=config.project, hold=hold, held_runtime=this["runtime"], runtime=runtime, model=model,
                packet=packet, revision=revision, document=document, declared=declared, registration=registration,
                eligible=eligible, config_sha256=hashlib.sha256(config_text.encode()).hexdigest(),
                blockers=tuple(blockers))


def _model_problems(config: Any, runtime: str, model: str, *,
                    runner: Callable[..., subprocess.CompletedProcess[Any]]) -> list[str]:
    from scripts import team_launcher as launcher

    owner_user, owner_args = launcher._owner_catalog_args(config)
    catalog = launcher.runtime_catalog.owner_model_catalog(runtime, runner=runner, owner_args=owner_args)
    if catalog is None:
        return [f"{runtime} on {owner_user or 'the project account'} could not list its models to check {model!r}"]
    if launcher.runtime_catalog.model_absent_from(catalog, model) is not None:
        return [f"{runtime} on {owner_user or 'the project account'} does not offer {model!r}"]
    return []


def describe(plan: Plan, print_func: Callable[[str], None]) -> None:
    prov = plan.provenance
    print_func(f"switchyard: {plan.project} Director rebind, for safety hold {plan.hold} on {plan.held_runtime}")
    print_func(f"  packet           {prov['schema']} v{prov['version']}, generated {prov['generated_at']}, "
               f"snapshot xmax {prov['db_xmax']}, {prov['state_digest']}")
    reg = plan.registration
    print_func(f"  director now     declared {plan.declared['runtime']} at {plan.declared['target']} "
               f"(slot {plan.declared['slot']}); registered "
               + (f"{reg.get('runtime')} pid {reg.get('process_pid')} generation {reg.get('generation')}" if reg
                  else "nothing"))
    print_func(f"  director after   {plan.runtime}" + (f", model {plan.model}" if plan.model else ", its default model")
               + "; a fresh session, given the packet; the old session kept, unread, as held evidence")
    for runtime, why in sorted(plan.eligible.items()):
        print_func(f"  alternate        {runtime}: " + ("eligible" if not why else "; ".join(why)))
    print_func(f"  held             {plan.held_runtime}: never started, typed into or resumed; the hold stays")
    for line in plan.blockers:
        print_func(f"  problem          {line}")
    print_func(f"  review           digest {plan.digest}")


# -- doing ----------------------------------------------------------------------------------------------------------
@dataclass
class Rebind:
    """One rebind's moving parts and the record of what it did, for its undo."""

    plan: Plan
    config: Any
    config_path: Path
    admin: Any
    get: Callable[[str], Any]
    runner: Callable[..., subprocess.CompletedProcess[Any]]
    tmux_runner: Callable[..., subprocess.CompletedProcess[Any]]
    start: Callable[..., int]
    handover: Any
    pane_state_dir: Path
    print_func: Callable[[str], None] = print
    rebind_id: int = 0
    done: list[str] = field(default_factory=list)
    rebound: dict = field(default_factory=dict)
    rebound_revision: int = 0
    packet_file: str = ""
    journal: Any = None
    journal_path: Path | None = None
    updated: Any = None

    def mark(self, step: str) -> None:
        """A step that may take effect from here on: marked BEFORE it is tried, so the undo always considers it."""
        if step not in self.done:
            self.done.append(step)

    def note(self, step: str, detail: Mapping[str, Any] | None = None) -> None:
        self.mark(step)
        try:
            self.admin.call("ticket_board.note_director_rebind_step", (self.rebind_id, "bigint"), (step, "text"),
                            (dict(detail or {}), "jsonb"))
        except Exception as exc:  # noqa: BLE001 - the step happened; the journal file still has it
            self.print_func(f"switchyard: the board did not record step {step}: {exc}")


def _old_identity(plan: Plan, config: Any) -> dict:
    from scripts import owner_writes, team_launcher as launcher

    role = _director(config)
    record = launcher.role_session_dir(config, role) / launcher.session_file_name(role.target)
    owner = launcher.role_run_as_user(config, role) or config.run_as_user or launcher.current_user_name()
    raw = owner_writes.read_owner_bytes(owner, record) if owner_writes.root_writes_for(owner) else (
        record.read_bytes() if record.is_file() else None)
    kept: dict[str, Any] = {"path": str(record), "present": raw is not None}
    if raw is not None:
        kept["sha256"] = hashlib.sha256(raw).hexdigest()
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        kept.update({k: data[k] for k in ("session_id", "runtime", "cli") if isinstance(data, dict) and k in data
                     and isinstance(data[k], str)})
    return {"declared": plan.declared, "registration": plan.registration, "session_record": kept,
            "configured_runtime": launcher._role_cli_name(role)}


def _stop_old(r: Rebind) -> None:
    from scripts import runtime_handover, stopped_project_model_repair

    role = _director(r.config)
    recorded = runtime_handover.row_holder(r.plan.registration)
    holders: set = set()
    if runtime_handover.session_is_live(role, runner=r.tmux_runner):
        holders = runtime_handover.pane_holders(role, runner=r.tmux_runner, handover=r.handover, recorded=recorded)
        kill = r.tmux_runner(["tmux", "kill-session", "-t", role.tmux_session])
        if kill.returncode != 0:
            raise RuntimeError(f"could not stop {role.tmux_session} (exit {kill.returncode})")
    elif recorded is not None and r.handover.live(recorded):
        holders = {recorded}
    r.note("old_stopped", {"session": role.tmux_session, "holders": sorted(list(h) for h in holders)})
    runtime_handover.await_holders_gone(holders, role=role, replacement=r.plan.runtime, handover=r.handover,
                                        print_func=r.print_func)
    # Held evidence, never a conversation to resume: a copy of the record beside the packet, and its digest and
    # session id in the board's record. It is then cleared while the role is still configured as the held runtime,
    # so clear_role_records keeps no SYRD-622 left record that a later switch back would resume.
    evidence = _keep_evidence(r)
    stopped_project_model_repair.clear_role_records(r.config, role, pane_state_dir=r.pane_state_dir)
    r.note("session_set_aside", {"evidence": evidence})


def _keep_evidence(r: Rebind) -> str:
    """A copy of the held session's record, mode 0600, beside where the packet goes; '' when there was none."""
    from scripts import owner_writes, team_launcher as launcher

    role = _director(r.config)
    record = launcher.role_session_dir(r.config, role) / launcher.session_file_name(role.target)
    account = launcher.role_run_as_user(r.config, role) or r.config.run_as_user or launcher.current_user_name()
    path = record.with_name(f"held-session-hold-{r.plan.hold}.json")
    if owner_writes.root_writes_for(account):
        raw = owner_writes.read_owner_bytes(account, record)
        if raw is None:
            return ""
        owner_writes.write_owner_file(account, path, raw, mode=0o600)
    else:
        if not record.is_file():
            return ""
        path.write_bytes(record.read_bytes())
        path.chmod(0o600)
    return str(path)


def _rebind_board(r: Rebind, *, runtime: str, revision: int, document: dict, why: str) -> None:
    binding = {DIRECTOR: {"runtime": runtime, "target": r.plan.declared["target"], "slot": r.plan.declared["slot"]}}
    r.admin.call("ticket_board.rebind_declared_pane_roles", (revision, "bigint"), (document, "jsonb"),
                 (binding, "jsonb"), (why, "text"))


def _board_declares(r: Rebind) -> tuple[str, int, dict]:
    """(the Director's declared runtime, revision, document) as the board says now."""
    workflow = r.get("/api/workflow") or {}
    document = workflow.get("document")
    if not isinstance(document, dict):
        raise RuntimeError("the board gave no declared workflow")
    return str(_declared(document)["runtime"]), int(workflow.get("revision") or 0), document


def _configured(r: Rebind) -> str:
    """The Director's runtime as the launcher configuration names it now, read as the owner's file."""
    from scripts import runtime_projection, team_launcher as launcher

    raw = json.loads(runtime_projection.required_text(r.config_path, r.config.run_as_user or ""))
    entry = next((e for e in raw.get("roles") or [] if isinstance(e, dict) and e.get("role") == DIRECTOR), {})
    cli = entry.get("cli") if isinstance(entry.get("cli"), list) else []
    return launcher._command_name(str(cli[0])) if cli else ""


def _write_packet(r: Rebind) -> str:
    from scripts import owner_writes, team_launcher as launcher

    role = _director(r.updated)
    path = launcher.role_session_dir(r.updated, role) / f"recovery-packet-hold-{r.plan.hold}.json"
    account = launcher.role_run_as_user(r.updated, role) or r.config.run_as_user or launcher.current_user_name()
    body = json.dumps(r.plan.packet, sort_keys=True, indent=1).encode() + b"\n"
    if owner_writes.root_writes_for(account):
        owner_writes.write_owner_file(account, path, body, mode=0o600)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(0o600)
    return str(path)


def _start_new(r: Rebind) -> dict:
    from scripts import role_runtime, team_launcher as launcher

    role = _director(r.updated)
    role_runtime._start_and_prove(role, config=r.updated, pane_state_dir=r.pane_state_dir, runner=r.tmux_runner,
                                  start=r.start, handover=r.handover)
    rows = ((r.get("/api/runtime-assignments") or {}).get("assignments") or {})
    row = rows.get(DIRECTOR) if isinstance(rows, dict) else None
    if not isinstance(row, dict) or row.get("runtime") != r.plan.runtime:
        raise RuntimeError(f"the board registers the Director as {(row or {}).get('runtime') or 'nothing'}, "
                           f"not {r.plan.runtime}")
    return {key: row[key] for key in IDENTITY_FIELDS if key in row} | {"cli": launcher._role_cli_name(role)}


def apply(r: Rebind, *, operator: str, reason: str) -> int:
    """Carry out the plan. Returns 0 when the Director runs on the new runtime, else 1 having undone what it did."""
    from scripts import role_runtime, runtime_projection
    from scripts.role_runtime import RuntimeJournal, journal_path_for

    plan, p = r.plan, r.print_func
    try:
        r.rebind_id = int(r.admin.call(
            "ticket_board.begin_director_rebind", (plan.hold, "bigint"), (operator, "text"), (reason, "text"),
            (plan.runtime, "text"), (plan.model, "text"), (_old_identity(plan, r.config), "jsonb"),
            (plan.provenance, "jsonb")))
    except Exception as exc:  # noqa: BLE001 - refused before anything changed
        p(f"switchyard: the board refused the rebind: {exc}. Nothing was changed.")
        return 1
    owner = r.config.run_as_user or ""
    r.journal = RuntimeJournal(project=plan.project, role=DIRECTOR, previous_runtime=plan.held_runtime,
                               requested_runtime=plan.runtime, previous_workflow_revision=plan.revision,
                               previous_workflow_document=plan.document,
                               previous_config_bytes=runtime_projection.required_text(r.config_path, owner),
                               config_path=str(r.config_path), owner_user=owner)
    r.journal_path = journal_path_for(r.config, config_path=r.config_path, role_name=DIRECTOR)
    try:
        r.journal.write(r.journal_path)  # the prior configuration, on disk before anything changes
        _stop_old(r)
        why = f"SYRD-738 Director rebind {r.rebind_id} by {operator}: hold {plan.hold} on {plan.held_runtime}"
        # Marked before the call: a compare-and-swap that commits and then fails to answer is still undone.
        r.mark("workflow")
        _rebind_board(r, runtime=plan.runtime, revision=plan.revision, document=plan.document, why=why)
        declared, r.rebound_revision, r.rebound = _board_declares(r)
        if declared != plan.runtime:
            raise RuntimeError(f"the board declares the Director as {declared} after the rebind")
        r.note("workflow", {"revision": r.rebound_revision})
        # Marked, and journalled, before the first byte: a projection that writes and then fails is still undone. A
        # restore of files never written puts back what was already there.
        r.mark("projection")
        r.journal.record("projection")
        r.journal.write(r.journal_path)
        r.updated = runtime_projection.write_runtime_projection(
            r.config, config_path=r.config_path, role_name=DIRECTOR, runtime=plan.runtime, runner=r.runner,
            model=plan.model, document=r.rebound, journal=r.journal, journal_path=r.journal_path)
        r.note("projection", {})
        r.packet_file = _write_packet(r)
        r.note("packet_written", {"path": r.packet_file})
        r.done.append("starting")
        started = _start_new(r)
        r.note("started", started)
        result = r.admin.call("ticket_board.finish_director_rebind", (r.rebind_id, "bigint"), ("applied", "text"),
                              ({"registration": started}, "jsonb"), (r.packet_file, "text"))
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - a half-applied rebind must not look like success
        p(f"switchyard: the Director rebind failed: {exc}")
        return _undo(r, failure=str(exc), operator=operator)
    role_runtime.stopped_project_model_repair.remove_journal(r.journal_path, owner, p)
    p(f"switchyard: {plan.project}'s Director now runs {plan.runtime} (rebind {r.rebind_id}), registered as "
      f"pid {started.get('process_pid')}; it is sent the recovery packet at {r.packet_file}"
      + ("" if (result or {}).get("notice") else " -- but no ticket could carry the notice; tell it yourself")
      + f". The {plan.held_runtime} hold {plan.hold} stands until it is cleared.")
    return 0


def _undo(r: Rebind, *, failure: str, operator: str) -> int:
    """Undo in reverse, never starting the held runtime. Returns 1."""
    from scripts import runtime_handover, runtime_projection

    plan, p = r.plan, r.print_func
    undone: list[str] = []
    problems: list[str] = []

    def attempt(step: str, undo: Callable[[], None]) -> bool:
        try:
            undo()
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - every failure is reported, none raised
            problems.append(f"{step}: {exc}")
            return False
        undone.append(step)
        return True

    if "starting" in r.done:
        def stop_attempt() -> None:
            role = _director(r.updated)
            if runtime_handover.session_is_live(role, runner=r.tmux_runner):
                holders = runtime_handover.pane_holders(role, runner=r.tmux_runner, handover=r.handover)
                r.tmux_runner(["tmux", "kill-session", "-t", role.tmux_session])
                runtime_handover.await_holders_gone(holders, role=role, replacement="nothing", handover=r.handover,
                                                    print_func=p)
        attempt("new_session", stop_attempt)
    board_back = True
    if "workflow" in r.done:
        # What the board declares now decides, not what this run believes it did: the write may have committed
        # without answering.
        def put_back() -> None:
            declared, revision, document = _board_declares(r)
            if declared == plan.held_runtime:
                return  # it never moved
            if declared != plan.runtime:
                raise RuntimeError(f"the board declares the Director as {declared}, neither {plan.held_runtime} "
                                   f"nor {plan.runtime}")
            _rebind_board(r, runtime=plan.held_runtime, revision=revision, document=document,
                          why=f"SYRD-738 Director rebind {r.rebind_id} undone: {failure[:200]}")
        board_back = attempt("workflow", put_back)
    if board_back and "projection" in r.done:
        attempt("projection", lambda: runtime_projection.restore_projection(r.journal))
    # Called rolled back only once it is so: the board and the configuration both name the held runtime, and no
    # Director session runs.
    if not problems:
        def verify() -> None:
            declared, configured = _board_declares(r)[0], _configured(r)
            live = runtime_handover.session_is_live(_director(r.config), runner=r.tmux_runner)
            wrong = ([f"the board declares {declared}"] if declared != plan.held_runtime else []) + (
                [f"the configuration names {configured}"] if configured != plan.held_runtime else []) + (
                ["a Director session is live"] if live else [])
            if wrong:
                raise RuntimeError(", ".join(wrong) + f", not {plan.held_runtime} with no session")
        if attempt("verified", verify):
            undone.remove("verified")
    outcome = "needs_operator" if problems else "rolled_back"
    result = {"failure": failure[:2000], "undone": undone, "problems": problems, "director_started": False,
              "steps": r.done}
    try:
        r.admin.call("ticket_board.finish_director_rebind", (r.rebind_id, "bigint"), (outcome, "text"),
                     (result, "jsonb"), (r.packet_file or None, "text"))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"the board did not record the outcome: {exc}")
    if problems:
        if r.journal is not None and r.journal_path is not None:
            r.journal.rollback_problems = problems
            r.journal.write(r.journal_path)
        p(f"switchyard: the rebind could not be fully undone and needs an operator: {'; '.join(problems)}. "
          f"The prior state is in {r.journal_path}. The Director was not started on {plan.held_runtime}.")
        return 1
    if r.journal_path is not None and r.journal is not None and "projection" in r.done:
        from scripts import stopped_project_model_repair

        stopped_project_model_repair.remove_journal(r.journal_path, r.config.run_as_user or "", p)
    p(f"switchyard: the rebind was undone ({', '.join(undone) or 'nothing had changed'}). The Director is declared "
      f"and configured as {plan.held_runtime} again and is left stopped: its account is held. Work is paused (drain) "
      f"and the operator is told. The hold stands.")
    return 1


def unavailable(plan: Plan, *, admin: Any, operator: str, reason: str, print_func: Callable[[str], None]) -> int:
    """No eligible alternate: keep work paused and tell the operator, once per hold. Returns 1."""
    detail = {runtime: why for runtime, why in sorted(plan.eligible.items())}
    try:
        said = admin.call("ticket_board.director_rebind_unavailable", (plan.hold, "bigint"), (operator, "text"),
                          (reason, "text"), (detail, "jsonb")) or {}
    except Exception as exc:  # noqa: BLE001
        print_func(f"switchyard: the board did not record that no alternate is eligible: {exc}")
        return 1
    print_func(f"switchyard: no runtime can take {plan.project}'s Director while hold {plan.hold} stands: "
               + "; ".join(f"{runtime}: {', '.join(why)}" for runtime, why in sorted(plan.eligible.items()))
               + f". Work is paused (drain, pause {(said.get('pause') or {}).get('pause')}) and the operator "
               + ("is told." if said.get("told") else "was already told.")
               + " Make one ready (install it, or sign the project account in), then run the rebind again.")
    return 1


# -- the command ------------------------------------------------------------------------------------------------------
def command(slug: str, *, cli: str, hold: int, reason: str, model: str = "", apply_it: bool = False,
            expect: str = "", euid_getter: Callable[[], int] = os.geteuid,
            operator_resolver: Callable[[], Any] | None = None, tenant_resolver: Callable[..., Any] | None = None,
            board_get: Callable[[str], Any] | None = None, admin: Any = None,
            runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
            start: Callable[..., int] | None = None, handover: Any = None,
            print_func: Callable[[str], None] = print) -> int:
    from scripts import role_runtime, runtime_handover, team_launcher as launcher

    slug = launcher._validate_project_slug(slug)
    again = f"pkexec switchyard rebind-director {slug} --cli {cli} --hold {hold}" + (f" --model {model}" if model else "")
    if euid_getter() != 0:
        print_func(f"switchyard: replacing {slug}'s Director reads root's records and writes through the database "
                   f"owner. Run: {again} --reason-file PATH")
        return 1
    from scripts.ticket_board.rollout_journal import Attempt, resolve_operator

    operator = (operator_resolver or resolve_operator)()
    if getattr(operator, "source", "") != "pkexec" or not getattr(operator, "known", False):
        print_func("switchyard: replacing the Director is an operator's decision and has to be authorized as one. "
                   f"This run was elevated by {getattr(operator, 'source', None) or 'nothing that names a person'}. "
                   f"Run: {again} --reason-file PATH")
        return 1
    if not reason.strip():
        print_func("switchyard: a Director rebind needs a reason (--reason-file PATH). Nothing was changed.")
        return 1
    from scripts.pane_rebind import root_verified_tenant

    resolved = (tenant_resolver or root_verified_tenant)(slug, print_func=print_func)
    if resolved is None:
        print_func(f"switchyard: refusing to rebind {slug}'s Director. Nothing was changed.")
        return 1
    tenant, config_path, config = resolved
    if board_get is None:
        from scripts.ticket_board import board_identity

        proven = board_identity.board_get_for(slug)
        board_get = lambda path: proven("", path)  # noqa: E731 - root's unit names the board, not the caller
    role = _director(config)
    from scripts.process_inspection import role_process_runner_for

    tmux_runner = role_process_runner_for(config, role, runner=runner)
    try:
        plan = plan_rebind(config, config_path=Path(config_path), get=board_get, hold=hold, runtime=cli, model=model,
                           runner=runner)
    except RebindRefused as exc:
        print_func(f"switchyard: {exc}. Nothing was changed.")
        return 1
    describe(plan, print_func)
    admin = admin or PsqlAdmin(tenant.admin_database_url, runner=runner)
    if not any(not why for why in plan.eligible.values()):
        if not apply_it:
            print_func(f"switchyard: preview; no runtime is eligible. --apply would keep work paused and tell the "
                       f"operator: {again} --reason-file PATH --apply --expect {plan.digest}")
            return 1
        if expect != plan.digest:
            print_func(f"switchyard: --expect {expect or '(none)'} is not what this would do now ({plan.digest}). "
                       "Review the preview and apply the digest it shows. Nothing was changed.")
            return 1
        return unavailable(plan, admin=admin, operator=operator.name, reason=reason, print_func=print_func)
    if plan.blockers:
        print_func(f"switchyard: {cli} cannot take the Director: {'; '.join(plan.blockers)}. Nothing was changed.")
        return 1
    if not apply_it:
        print_func(f"switchyard: preview; nothing was written. Apply exactly this with: "
                   f"{again} --reason-file PATH --apply --expect {plan.digest}")
        return 0
    if expect != plan.digest:
        print_func(f"switchyard: --expect {expect or '(none)'} is not what this would do now ({plan.digest}). Review "
                   "the preview and apply the digest it shows. Nothing was changed.")
        return 1
    attempt = Attempt(slug, ["switchyard", "rebind-director", slug, f"--cli={cli}", f"--hold={hold}",
                             f"--model={model}", "--apply", "--expect", expect], operator=operator.name)
    attempt.operator = operator
    attempt.open()
    attempt.write("stdout", json.dumps({"schema": SCHEMA, "plan": {
        "hold": plan.hold, "from": plan.held_runtime, "to": plan.runtime, "model": plan.model,
        "revision": plan.revision, "document": plan.document, "declared": plan.declared,
        "registration": plan.registration, "packet": plan.provenance, "review": plan.digest},
        "reason": reason}, sort_keys=True) + "\n")
    state_dir = launcher.default_pane_state_dir_for_user(config.run_as_user or launcher.current_user_name(),
                                                         project=config.project)
    rebind = Rebind(plan=plan, config=config, config_path=Path(config_path), admin=admin, get=board_get,
                    runner=runner, tmux_runner=tmux_runner,
                    start=start or role_runtime._default_start,
                    handover=handover or runtime_handover.RuntimeHandover(
                        read_assignments=lambda _config: (((board_get("/api/runtime-assignments") or {})
                                                           .get("assignments") or {}), "")),
                    pane_state_dir=state_dir, print_func=print_func)
    status = apply(rebind, operator=operator.name, reason=reason)
    attempt.write("stdout", json.dumps({"rebind": rebind.rebind_id, "steps": rebind.done, "status": status}) + "\n")
    attempt.close(status="succeeded" if status == 0 else "failed", exit_status=status,
                  detail=f"rebind {rebind.rebind_id}: {', '.join(rebind.done)}")
    return status


def _build_switchyard_rebind_director_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard rebind-director",
        description="Replace a Director whose provider account is under a safety hold with a fresh Director on "
                    "another runtime, given the board's recovery packet. Operator only, through pkexec. Previews by "
                    "default; --apply --expect DIGEST does exactly what the preview showed. The hold is not cleared.")
    parser.add_argument("project")
    parser.add_argument("--cli", required=True, help="the runtime the Director moves to")
    parser.add_argument("--hold", required=True, type=int, help="the safety hold on the Director's account")
    parser.add_argument("--model", default="", help="the model on the new runtime (default: the runtime's own)")
    parser.add_argument("--reason-file", required=True, type=Path, help="why, recorded with the rebind")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect", default="", help="the review digest the preview printed")
    return parser


def switchyard_rebind_director_command(argv: list[str]) -> int:
    args = _build_switchyard_rebind_director_parser().parse_args(argv)
    try:
        reason = args.reason_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"switchyard: cannot read {args.reason_file}: {exc.strerror}. Nothing was changed.")
        return 1
    return command(args.project, cli=args.cli, hold=args.hold, reason=reason, model=args.model,
                   apply_it=args.apply, expect=args.expect)


def switchyard_break_glass_command(argv: list[str]) -> int:
    """`switchyard break-glass` (SYRD-739), reached through this module's launcher re-export."""
    from scripts import break_glass_role

    return break_glass_role.main(argv)
