"""Rebinding a tenant's panes to its migrated workflow: `switchyard rebind-workflow-panes`.

After a workflow migration, the board's roles may name runtimes, targets and
slots that no longer match the panes actually running. This module plans and
applies the reconciliation:
- `plan_pane_rebind` builds a `PaneRebind`. It compares the declared
  bindings (`PANE_BINDING_FIELDS`: runtime, target, slot) with what the
  board's runtime registrations show (`read_runtime_registrations`,
  `_registration_visibility`) and what was recorded before the migration
  (`recorded_pre_migration_panes`).
- `plan_projection_rewrites` and `_reconciled_presentation_section` produce
  the `ProjectionRewrite`s that keep the generated presentation projection in
  agreement with the new bindings.
- `rebind_review_digest` is the exact digest a preview shows and an apply
  must present again.
- `root_verified_tenant` is the root prologue: root's own plan, and the
  tenant configuration only once root has verified it.
- `switchyard_rebind_workflow_panes_command` previews by default and applies
  on request; `_build_switchyard_rebind_workflow_panes_parser` is its parser.

Reading the board's workflow state, the role pane and runtime declarations,
the presentation rule, tenant config verification and the privileged plan
records stay in `scripts/team_launcher.py`. This module reads them from there
when a function runs, so the suites' patches on the launcher reach it.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-304). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


PANE_BINDING_FIELDS = ("runtime", "target", "slot")


@dataclass(frozen=True)
class PaneRebind:
    """What rebinding a board's declared pane roles to its tenant's panes would do."""

    revision: int
    document: dict
    rebound: dict
    bindings: dict
    changes: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        from scripts.ticket_board.project_provision import workflow_document_digest

        return workflow_document_digest(self.document)

    @property
    def rebound_digest(self) -> str:
        from scripts.ticket_board.project_provision import workflow_document_digest

        return workflow_document_digest(self.rebound)


def plan_pane_rebind(
    config: ProjectConfig,
    revision: int,
    document: dict,
    *,
    intended: "Mapping[str, str] | None" = None,
    slots: "Mapping[str, int] | None" = None,
) -> PaneRebind:
    """Rebind each declared role this tenant runs a pane for, to that pane.

    The values come from the tenant's configuration through the function the
    pane registers with, never from a caller (SYRD-262). A role the tenant runs
    no pane for is left exactly as declared, and a pane whose role is not
    declared is reported rather than added: a rebind changes bindings, not
    which roles exist.
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.workflow_config import validate

    declared = {str(role.get("name")): role for role in document.get("roles") or []}
    bindings: dict[str, dict] = {}
    changes: list[str] = []
    notes: list[str] = []
    intended = dict(intended or {})
    for role in config.roles:
        pane = launcher.role_pane_declaration(role)
        if role.role in intended:
            # The operator's reviewed decision, and the only way a runtime
            # differs from the verified configuration here (SYRD-262).
            pane["runtime"] = intended[role.role]
        if role.role in (slots or {}):
            # Likewise where a pane is shown: the first migration copied the
            # example's slots into both the declaration and the configuration,
            # so neither is evidence of the tenant's layout any more.
            pane["slot"] = slots[role.role]
        current = declared.get(role.role)
        if current is None:
            notes.append(
                f"{role.role}: this tenant runs a {role.role} pane, but the declared workflow has "
                f"no {role.role} role; a rebind does not add roles"
            )
            continue
        was = {field: current.get(field) for field in PANE_BINDING_FIELDS}
        if was != pane:
            bindings[role.role] = pane
            changes.append(
                f"{role.role}: " + ", ".join(
                    f"{field} {was[field]} -> {pane[field]}"
                    for field in PANE_BINDING_FIELDS if was[field] != pane[field]
                )
            )
    for name, current in declared.items():
        if name not in {role.role for role in config.roles} and current.get("target"):
            notes.append(
                f"{name}: declared on {current.get('runtime')}/{current.get('target')} but this "
                "tenant runs no such pane; left as declared"
            )
    rebound = copy.deepcopy(document)
    for role in rebound.get("roles") or []:
        if role.get("name") in bindings:
            role.update(bindings[role["name"]])
    problems: list[str] = []
    try:
        validate(copy.deepcopy(rebound), project=config.project)
    except (ValueError, SystemExit) as exc:
        problems.append(f"the rebound workflow would not validate: {exc}")
    return PaneRebind(revision, document, rebound, bindings, tuple(changes), tuple(notes), tuple(problems))


def read_runtime_registrations(
    plan: "ProjectBoardProvision",
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[list[dict], str]:
    """Every registered pane, including those the declaration currently hides."""
    from scripts import team_launcher as launcher

    result = runner(
        ["sudo", "-u", "postgres", "psql", "-X", "-tA", "-v", "ON_ERROR_STOP=1",
         plan.admin_database_url, "-c",
         "SELECT coalesce(json_agg(json_build_object('role', role, 'runtime', runtime, "
         "'target', actual_target, 'pid', process_pid, 'start_time', process_start_time) "
         "ORDER BY role), '[]') FROM ticket_board.role_runtime_assignments"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return [], launcher._proc_failure_reason(result, f"psql exited {result.returncode}")
    try:
        return list(json.loads(result.stdout or "[]")), ""
    except json.JSONDecodeError as exc:
        return [], f"unreadable registrations: {exc}"


def _registration_visibility(rebind: PaneRebind, registrations: list[dict]) -> list[str]:
    from scripts.ticket_board.peer_identity import SessionIdentity, session_is_live

    lines = []
    by_role = {str(row.get("role")): row for row in registrations}
    for role, binding in sorted(rebind.bindings.items()):
        row = by_role.get(role)
        if row is None:
            lines.append(f"{role}: no pane is registered; it is served once one registers")
            continue
        matches = row.get("runtime") == binding["runtime"] and row.get("target") == binding["target"]
        live = session_is_live(SessionIdentity(int(row.get("pid") or 0), int(row.get("start_time") or 0)))
        state = "live" if live else "not running"
        if matches and live:
            lines.append(f"{role}: registered {row['runtime']}/{row['target']} (pid {row['pid']}, {state}) "
                         "matches, so it is served as soon as the rebind lands -- no restart")
        else:
            lines.append(f"{role}: registered {row.get('runtime')}/{row.get('target')} (pid "
                         f"{row.get('pid')}, {state}) does not match {binding['runtime']}/"
                         f"{binding['target']}; it stays unserved until that role is restarted")
    return lines


def root_verified_tenant(
    slug: str,
    *,
    registry_dir: Path | None = None,
    config_path: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> "tuple[ProjectBoardProvision, Path, ProjectConfig] | None":
    """Root's plan for `slug`, rebuilt from root's own record, and the tenant
    configuration root has verified against it -- or None, with the reasons
    printed. The same sequence `adopt-workflow` runs before trusting a tenant's
    configuration (SYRD-166, SYRD-262).
    """
    from scripts import team_launcher as launcher

    baseline = launcher.privileged_baseline_plan_path(slug)
    record, problem = launcher.read_plan_no_follow(baseline, require_root_owned=True)
    if record is None:
        print_func(f"switchyard: {problem}")
        return None
    identity = launcher.trusted_owner_identity(slug)
    if not identity.trusted:
        for objection in identity.problems:
            print_func(f"switchyard: {objection}")
        print_func(f"switchyard: root cannot establish whose installation {slug} is.")
        return None
    recorded_release = str(record.data.get("source_repo") or "").strip()
    if recorded_release:
        selected = Path(recorded_release)
    else:
        selected, release_problem = launcher._resume_source_release(None)
        if release_problem:
            print_func(f"switchyard: {release_problem}")
            return None
    plan, divergence = launcher._resume_plan_from_record(record, identity, source_repo=selected)
    if divergence:
        for objection in divergence:
            print_func(f"switchyard: {objection}")
        print_func(f"switchyard: root cannot rebuild {slug}'s plan unchanged.")
        return None
    verified, config, config_problems = launcher.verified_tenant_config(
        plan, slug, explicit=config_path, owner_uid=launcher.uid_for_user(plan.owner_user),
        registry_dir=registry_dir,
    )
    if config is None or verified is None:
        for objection in config_problems:
            print_func(f"switchyard: {objection}")
        print_func(f"switchyard: {slug}'s configuration is not one root has verified.")
        return None
    return plan, verified, config


def recorded_pre_migration_panes(config_path: Path) -> dict[str, list[str]]:
    """What the tenant's own rollback journals say each pane was before a workflow write.

    `workflow_manage apply` keeps the projection it replaced in
    `workflow-before-<revision>.json` beside the configuration. That is where
    MEFP's Codex runtimes and its four-pane slots survived the write that
    overwrote them (SYRD-262). It is the tenant's file, so this is evidence to
    show an operator, never authority: read without following links, and
    nothing is decided from it.
    """
    from scripts import team_launcher as launcher

    found: dict[str, list[str]] = {}
    for journal in sorted(config_path.parent.glob("workflow-before-*.json")):
        holder, _problem = launcher.read_plan_no_follow(journal, require_root_owned=False)
        if holder is None:
            continue
        previous = (holder.data.get("previous_files") or {}).get(str(config_path))
        try:
            roles = json.loads(previous or "{}").get("roles") or []
        except (TypeError, json.JSONDecodeError):
            continue
        for role in roles:
            if not isinstance(role, dict):
                continue
            cli = role.get("cli")
            runtime = launcher._command_name(str(cli[0])) if isinstance(cli, list) and cli else "none"
            found.setdefault(str(role.get("role")), []).append(
                f"runtime {runtime}, slot {role.get('slot')} (in {journal.name})"
            )
    return found


@dataclass(frozen=True)
class ProjectionRewrite:
    """One tenant projection file root will replace, read without following links."""

    path: Path
    document: "PlanDocument"
    body: bytes

    @property
    def changed(self) -> bool:
        return self.document.raw != self.body


def plan_projection_rewrites(
    config_path: Path, rebound: dict, *, owner_uid: int
) -> tuple[list[ProjectionRewrite], list[str]]:
    """The tenant's projection as the rebound document would generate it.

    The same `projection_files` every workflow write uses, so the launcher
    configuration, its copies of the document and the board agree afterwards.
    Only files that already exist are replaced, each read and later written by
    descriptor inside its own directory, keeping its owner and mode.
    """
    from scripts import team_launcher as launcher

    from scripts.workflow_launcher import projection_files

    rewrites: list[ProjectionRewrite] = []
    problems: list[str] = []
    try:
        projected = projection_files(config_path, rebound)
    except (KeyError, ValueError, OSError, SystemExit) as exc:
        return [], [f"the tenant projection could not be generated from the rebound workflow: {exc!r}"]
    if config_path in projected:
        projected[config_path] = _reconciled_presentation_section(projected[config_path])
    for path, text in projected.items():
        holder, problem = launcher.read_plan_no_follow(
            path, require_root_owned=False, require_owner_uids=sorted({0, owner_uid}),
            require_single_link=True, require_not_shared_writable=True,
        )
        if holder is None:
            if "does not exist" in problem:
                continue
            problems.append(problem)
            continue
        rewrites.append(ProjectionRewrite(path, holder, text.encode("utf-8")))
    # The configuration first: it is the record a restart reads, so it is
    # reconciled before the board is (SYRD-262).
    rewrites.sort(key=lambda item: item.path != config_path)
    return rewrites, problems


def _reconciled_presentation_section(text: str) -> str:
    """Re-derive a presentation section Switchyard wrote, from the slots now configured.

    The legacy presentation migration (SYRD-233) writes `slot_count` and a
    `default` layout from the slots in force at the time. On MEFP those were
    the example's, so the section went on holding its window at six slots
    after the roles were put back on 0-3 (SYRD-262). A section of exactly that
    shape -- nothing but a `default` layout -- carries nothing a person chose,
    so it is derived again by the same rule. One with any other layout is
    somebody's own and is left as it is. Either way the result is in the
    preview's diff and under its digest before anything is written.
    """
    from scripts import team_launcher as launcher

    document = json.loads(text)
    section = document.get("presentation")
    if not isinstance(section, dict):
        return text
    layouts = section.get("layouts")
    if set(section) - {"slot_count", "layouts"} or not isinstance(layouts, dict) or set(layouts) - {"default"}:
        return text
    derived = launcher.presentation_section_for_roles(document.get("roles") or [])
    if derived["slot_count"] < 1 or section == derived:
        return text
    document["presentation"] = derived
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def rebind_review_digest(rebind: "PaneRebind", rewrites: list[ProjectionRewrite]) -> str:
    """One digest over everything an apply would change, old and new."""
    return hashlib.sha256(json.dumps({
        "document": [rebind.digest, rebind.rebound_digest],
        "files": {
            str(item.path): [hashlib.sha256(item.document.raw).hexdigest(),
                             hashlib.sha256(item.body).hexdigest()]
            for item in rewrites if item.changed
        },
    }, sort_keys=True).encode("utf-8")).hexdigest()


def switchyard_rebind_workflow_panes_command(
    slug: str,
    *,
    apply: bool = False,
    expect: str = "",
    runtimes: "Mapping[str, str] | None" = None,
    slots: "Mapping[str, int] | None" = None,
    registry_dir: Path | None = None,
    config_path: Path | None = None,
    euid_getter: Callable[[], int] = os.geteuid,
    operator_resolver: Callable[[], Any] | None = None,
    board_reader: Callable[[ProjectConfig], tuple[int, dict | None, str]] | None = None,
    registrations_reader: Callable[["ProjectBoardProvision"], tuple[list[dict], str]] | None = None,
    tenant_resolver: Callable[..., Any] | None = None,
    sql_runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    journal: Any | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """Rebind a declared workflow's pane roles to the tenant's own panes (SYRD-262).

    For a board whose declaration names the wrong runtime, target or slot for
    roles its tenant runs: the board then serves those panes no assignment and
    grants their processes no authority -- the Director's included, so the
    Director cannot correct it. This is root's bounded repair, and it is not a
    Director write: nobody acts as the Director.

    The values are derived here from root's verified tenant configuration
    through the function panes register with; no caller supplies a role,
    runtime, target or slot. Only those three fields of roles that already
    exist can change. Without --apply it previews the bindings, the exact
    document diff and both digests. --apply must name the previewed digest
    with --expect, and the database refuses unless the live revision AND
    document are still the reviewed ones. The run is journaled with both
    documents, and the new revision is attributed to this repair.
    """
    from scripts import team_launcher as launcher

    import difflib

    from scripts.ticket_board.rollout_journal import Attempt, resolve_operator

    slug = launcher._validate_project_slug(slug)
    if euid_getter() != 0:
        print_func(
            f"switchyard: rebinding {slug}'s declared pane roles reads root's records and writes "
            f"through the database owner. Run: pkexec switchyard rebind-workflow-panes {slug}"
        )
        return 1
    operator = (operator_resolver or resolve_operator)()
    if getattr(operator, "source", "") != "pkexec" or not getattr(operator, "known", False):
        print_func(
            "switchyard: a rebind is an operator's decision and has to be authorized as one. "
            f"This run was elevated by {getattr(operator, 'source', None) or 'nothing that names a person'}. "
            f"Run: pkexec switchyard rebind-workflow-panes {slug}"
        )
        return 1

    resolved = (tenant_resolver or root_verified_tenant)(
        slug, registry_dir=registry_dir, config_path=config_path, print_func=print_func
    )
    if resolved is None:
        print_func(f"switchyard: refusing to rebind {slug}. Nothing was changed.")
        return 1
    plan, verified, config = resolved
    from scripts.ticket_board.workflow_config import RUNTIMES

    intended = dict(runtimes or {})
    placed = dict(slots or {})
    pane_roles = {role.role for role in config.roles}
    for role_name, slot in placed.items():
        if role_name not in pane_roles:
            print_func(f"switchyard: {role_name} is not a pane role in {verified}. Nothing was changed.")
            return 1
        if type(slot) is not int or not 0 <= slot <= 5:
            print_func(f"switchyard: slot {slot!r} for {role_name} is not a visible slot (0-5). "
                       "Nothing was changed.")
            return 1
    for role_name, runtime in intended.items():
        if role_name not in pane_roles:
            print_func(f"switchyard: {role_name} is not a pane role in {verified}. Nothing was changed.")
            return 1
        if runtime not in RUNTIMES:
            print_func(f"switchyard: {runtime!r} is not a runtime ({', '.join(sorted(RUNTIMES))}). "
                       "Nothing was changed.")
            return 1

    revision, live, board_problem = (board_reader or launcher.read_board_workflow_state)(config)
    if live is None:
        print_func(
            f"switchyard: {slug}'s board workflow could not be read"
            + (f": {board_problem}" if board_problem else ", or it runs none")
            + ". Nothing was changed."
        )
        return 1
    rebind = plan_pane_rebind(config, revision, live, intended=intended, slots=placed)
    unknown = sorted((set(intended) | set(placed)) - {str(r.get("name")) for r in live.get("roles") or []})
    if unknown:
        print_func(f"switchyard: {', '.join(unknown)} is not a declared role; a rebind does not add "
                   "roles. Nothing was changed.")
        return 1
    rewrites, file_problems = plan_projection_rewrites(
        verified, rebind.rebound, owner_uid=launcher.uid_for_user(plan.owner_user)
    )
    registrations, registration_problem = (registrations_reader or read_runtime_registrations)(plan)
    review = rebind_review_digest(rebind, rewrites)

    print_func(f"switchyard: {slug} declared pane rebind, from {verified}")
    print_func(f"  board            revision {rebind.revision}, digest {rebind.digest}")
    if intended or placed:
        recorded = recorded_pre_migration_panes(verified)
        by_role = {str(row.get("role")): row for row in registrations}
        for role in config.roles:
            if role.role not in intended and role.role not in placed:
                continue
            row = by_role.get(role.role) or {}
            decided = ", ".join(
                ([f"runtime {intended[role.role]}"] if role.role in intended else [])
                + ([f"slot {placed[role.role]}"] if role.role in placed else [])
            )
            print_func(f"  operator         {role.role}: {decided} (the operator's decision)")
            print_func(f"    evidence       configuration now: runtime {launcher.role_runtime_binding(role)[0]}, "
                       f"slot {role.slot}")
            print_func("    evidence       tenant journal before its last workflow write: "
                       + ("; ".join(recorded.get(role.role) or []) or "none recorded"))
            print_func(f"    evidence       live registration: "
                       + (f"{row.get('runtime')}/{row.get('target')} (pid {row.get('pid')})" if row else "none"))
    for line in rebind.changes or ("(the declaration already names every pane's binding)",):
        print_func(f"  changes          {line}")
    for line in rebind.notes:
        print_func(f"  unchanged        {line}")
    problems = [*rebind.problems, *file_problems]
    if registration_problem:
        problems.append(f"the live registrations could not be read: {registration_problem}")
    for line in problems:
        print_func(f"  problem          {line}")
    if problems:
        print_func("switchyard: nothing was changed.")
        return 1

    changed_files = [item for item in rewrites if item.changed]
    if not rebind.bindings and not changed_files:
        # Nothing to write is only a success when the board already serves what
        # runs. A live pane the declaration does not match is the very failure
        # this exists to repair, and reporting "nothing to do" over it is what
        # left MEFP's Director without authority after the first preview.
        declared = {str(r.get("name")): r for r in live.get("roles") or []}
        divergent = [
            f"{row.get('role')}: live pane registered {row.get('runtime')}/{row.get('target')}, "
            f"declared {declared[row['role']].get('runtime')}/{declared[row['role']].get('target')}"
            for row in registrations
            if row.get("role") in declared and row.get("role") in pane_roles
            and (row.get("runtime"), row.get("target"))
            != (declared[row["role"]].get("runtime"), declared[row["role"]].get("target"))
        ]
        if divergent:
            for line in divergent:
                print_func(f"  divergent        {line}")
            print_func(
                f"switchyard: refusing: {slug}'s declaration already equals its configuration, "
                "but live panes registered something else, so there is nothing this could write "
                "that would serve them. If those panes run what was intended, name it: "
                f"pkexec switchyard rebind-workflow-panes {slug} --runtime ROLE=RUNTIME. "
                "Nothing was changed."
            )
            return 1
        print_func(f"switchyard: {slug}'s declared pane roles already match its panes. Nothing to do.")
        return 0

    if rebind.bindings:
        before = json.dumps(rebind.document, indent=2, sort_keys=True).splitlines()
        after = json.dumps(rebind.rebound, indent=2, sort_keys=True).splitlines()
        for line in difflib.unified_diff(before, after, "declared", "rebound", lineterm="", n=2):
            print_func(f"  | {line}")
    for item in changed_files:
        for line in difflib.unified_diff(
            item.document.raw.decode("utf-8", "replace").splitlines(),
            item.body.decode("utf-8", "replace").splitlines(),
            str(item.path), f"{item.path} (reconciled)", lineterm="", n=1,
        ):
            print_func(f"  | {line}")
    print_func(f"  rebound          digest {rebind.rebound_digest}")
    for line in _registration_visibility(rebind, registrations):
        print_func(f"  registration     {line}")
    print_func(f"  review           digest {review}")
    if not apply:
        print_func(
            "switchyard: preview; nothing was written. Apply exactly this with: "
            f"pkexec switchyard rebind-workflow-panes {slug}"
            + "".join(f" --runtime {name}={value}" for name, value in sorted(intended.items()))
            + "".join(f" --slot {name}={value}" for name, value in sorted(placed.items()))
            + f" --apply --expect {review}"
        )
        return 0
    if expect != review:
        print_func(
            f"switchyard: --expect {expect or '(none)'} is not what this would write now "
            f"({review}). Review the preview and apply the digest it shows. Nothing was changed."
        )
        return 1

    attempt = journal or Attempt(
        slug,
        ["switchyard", "rebind-workflow-panes", slug,
         *(f"--runtime={name}={value}" for name, value in sorted(intended.items())),
         *(f"--slot={name}={value}" for name, value in sorted(placed.items())),
         "--apply", "--expect", expect],
        operator=operator.name,
    )
    attempt.operator = operator
    attempt.open()
    attribution = f"SYRD-262 pane rebind by {operator.name} from {verified}"
    # Rollback evidence, before anything is written: both documents, every
    # file's old and new content, and what the operator decided. The previous
    # document also stays in the board's own workflow_revisions.
    attempt.write("stdout", json.dumps({
        "revision": rebind.revision, "digest": rebind.digest, "document": rebind.document,
        "bindings": rebind.bindings, "rebound_digest": rebind.rebound_digest,
        "rebound": rebind.rebound, "attribution": attribution, "intended": intended, "slots": placed,
        "review": review,
        "files": {str(item.path): {"before": item.document.raw.decode("utf-8", "replace"),
                                   "after": item.body.decode("utf-8", "replace")}
                  for item in changed_files},
    }, sort_keys=True) + "\n")

    def write_files(items: "list[ProjectionRewrite]") -> str:
        for item in items:
            problem = launcher.write_plan_no_follow(item.document, item.body)
            if problem:
                return problem
        return ""

    # 1. The trusted tenant record first: it is what a restart reads.
    first = [item for item in changed_files if item.path == Path(verified)]
    problem = write_files(first)
    if problem:
        attempt.close(status="failed", exit_status=1, detail=problem)
        print_func(f"switchyard: {problem}. Nothing else was changed.")
        return 1
    # 2. The board, compare-and-swap on the reviewed revision and document.
    if rebind.bindings:
        result = sql_runner(
            ["sudo", "-u", "postgres", "psql", "-X", "-tA", "-v", "ON_ERROR_STOP=1",
             "-v", f"rev={int(rebind.revision)}",
             "-v", f"expected={json.dumps(rebind.document, sort_keys=True)}",
             "-v", f"bindings={json.dumps(rebind.bindings, sort_keys=True)}",
             "-v", f"why={attribution}",
             plan.admin_database_url, "-f", "-"],
            input=("SELECT ticket_board.rebind_declared_pane_roles("
                   ":rev, :'expected'::jsonb, :'bindings'::jsonb, :'why');\n"),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            reason = launcher._proc_failure_reason(result, f"psql exited {result.returncode}")
            attempt.close(status="failed", exit_status=1, detail=reason)
            print_func(
                f"switchyard: the database refused the rebind: {reason}. The board was not changed"
                + (f"; {verified} was already reconciled and the journal holds its previous content."
                   if first else ".")
            )
            return 1
    # 3. The rest of the projection, generated from the rebound document.
    problem = write_files([item for item in changed_files if item.path != Path(verified)])
    if problem:
        attempt.close(status="failed", exit_status=1, detail=problem)
        print_func(f"switchyard: the board was rebound but {problem}; rerun to finish.")
        return 1
    new_revision, now, now_problem = (board_reader or launcher.read_board_workflow_state)(config)
    from scripts.ticket_board.project_provision import workflow_document_digest

    if now is None or workflow_document_digest(now) != rebind.rebound_digest:
        detail = (f"the board now serves {workflow_document_digest(now) if now else 'nothing'}"
                  f"{(' (' + now_problem + ')') if now_problem else ''}, not {rebind.rebound_digest}")
        attempt.close(status="failed", exit_status=1, detail=detail)
        print_func(f"switchyard: {detail}.")
        return 1
    registrations, _ = (registrations_reader or read_runtime_registrations)(plan)
    for line in _registration_visibility(rebind, registrations):
        print_func(f"  registration     {line}")
    attempt.close(status="succeeded", exit_status=0, detail=f"revision {new_revision}")
    print_func(
        f"switchyard: {slug} now declares its panes at revision {new_revision} "
        f"(digest {rebind.rebound_digest}), attributed to this rebind, and {verified} agrees."
    )
    return 0


def _build_switchyard_rebind_workflow_panes_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard rebind-workflow-panes",
        description=(
            "Correct the runtime, target and slot a declared workflow names for the roles a "
            "tenant runs as panes, from root's verified tenant configuration. Nothing else in "
            "the workflow can change. Previews without --apply."
        ),
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("--apply", action="store_true", help="write the previewed rebind; requires root")
    parser.add_argument("--expect", default="", help="the review digest the preview showed")
    parser.add_argument(
        "--runtime", action="append", default=[], metavar="ROLE=RUNTIME",
        help="the runtime the operator has decided a pane role runs; shown with the evidence "
        "for it, and reconciled into the tenant's configuration before the board",
    )
    parser.add_argument(
        "--slot", action="append", default=[], metavar="ROLE=SLOT",
        help="the visible slot (0-5) the operator has decided a pane role is shown in; "
        "shown with the evidence for it and reconciled the same way",
    )
    parser.add_argument("--config-path", type=Path, default=None, help=argparse.SUPPRESS)
    return parser
