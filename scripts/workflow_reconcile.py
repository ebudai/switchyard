"""Re-recording root's declared workflow from the live board, on review (SYRD-561).

A tenant's declared workflow lives in three places: the board's live document,
the tenant's projection of it (its launcher configuration, plan and layout),
and root's own record (`/etc/switchyard/provision/<project>/workflow.json`).
Otto's runtime switch moved the board and part of the tenant's projection and
left root's record naming `main=hermes`, and nothing could repair it:
`adopt-workflow` stops once root holds a record, `migrate-workflow` installs
onto a board that has none, and the upgrade's own checks fail closed on the
mismatch -- correctly, and with no way forward.

This is that way forward, run as `adopt-workflow`'s answer when root already
holds a record:

* **Preview** (always): root's digest, the board's digest, every line where the
  two differ, and which of the tenant's files the board's document would
  rewrite. Nothing is written.
* **Apply** (`--apply --from-live <digest> --replacing <digest>`): root's record
  becomes the board's document. Only that selection exists -- the live
  declaration, which the board already validated and enforces -- so an
  unreviewed tenant document is never adopted. Both digests are compared
  first, as the operator reviewed them: a board or a record that moved since
  the preview is refused. The document is validated for this project, written
  through root's staged writer, read back, and the board is read once more; a
  board that changed while root wrote is answered by restoring root's previous
  record. No upgrade phase runs.

`--add-role <role>` (SYRD-562) takes only one role from the board -- its
definition and its place in stage owners and transition actors -- onto root's
record, for a role the tenant added with `workflow apply` after root recorded
its workflow, without adopting any other difference the board carries. The
apply is the same compare-and-swap, read-back and board re-read.

Root does not write the tenant's files. They are the tenant's, in directories
the tenant controls, and a root process writing through those follows whatever
the tenant has placed there. The tenant already has a supported repair -- a
runtime switch to the runtime a role already has brings a stale projection in
line from the board's document (SYRD-558) -- so the exact command is named
instead. Reading the tenant's files to say which are stale names files; it
never shows their content.
"""

from __future__ import annotations

import copy
from difflib import unified_diff
from pathlib import Path
from typing import Any, Callable, Mapping

REFUSED_NOTHING_CHANGED = "Nothing was changed."


def record_difference(recorded: Mapping[str, Any], live: Mapping[str, Any]) -> list[str]:
    """Where root's record and the board's document disagree, line by line."""
    from scripts.workflow_adoption import _canonical_workflow

    return list(unified_diff(
        _canonical_workflow(recorded).splitlines(),
        _canonical_workflow(live).splitlines(),
        fromfile="root's recorded workflow",
        tofile="the board's declared workflow",
        lineterm="",
    ))


def missing_roles(recorded: Mapping[str, Any], live: Mapping[str, Any]) -> list[str]:
    """Roles the board declares that root's record does not, in the board's order (SYRD-562)."""
    held = {r.get("name") for r in recorded.get("roles") or []}
    return [r["name"] for r in live.get("roles") or [] if r.get("name") and r["name"] not in held]


def _insert_like(names: list[str], role: str, live_names: list[str]) -> list[str]:
    """`names` with `role` added where the board's list has it, relative to the names both share."""
    if role in names:
        return names
    after = None
    for name in live_names[: live_names.index(role)]:
        if name in names:
            after = name
    position = names.index(after) + 1 if after is not None else 0
    return [*names[:position], role, *names[position:]]


def with_role_added(recorded: Mapping[str, Any], live: Mapping[str, Any], role: str) -> tuple[dict, list[str]]:
    """Root's record plus one role, exactly as the board declares it, and nothing else (SYRD-562).

    The role's own definition, and its name in the stage owners and transition
    actors where the board lists it -- the additive references, which only give
    this role a place. Scalar references (a stage's notify role, `reassign`,
    the queue) are never copied: changing one would replace policy that has
    nothing to do with adding a role. Whatever of the board's this leaves out
    is returned, so the operator sees it.
    """
    definition = next((r for r in live.get("roles") or [] if r.get("name") == role), None)
    if definition is None:
        raise ValueError(f"the board's declared workflow has no role {role!r}")
    if any(r.get("name") == role for r in recorded.get("roles") or []):
        raise ValueError(f"root's record already declares role {role!r}")
    merged = copy.deepcopy(dict(recorded))
    live_roles = [r.get("name") for r in live.get("roles") or []]
    merged["roles"] = [copy.deepcopy(definition) if r == role else next(x for x in merged["roles"] if x.get("name") == r)
                       for r in _insert_like([x.get("name") for x in merged["roles"]], role, live_roles)]
    left_out: list[str] = []
    live_stages = {s.get("name"): s for s in live.get("stages") or []}
    for stage in merged.get("stages") or []:
        other = live_stages.get(stage.get("name")) or {}
        if role in (other.get("owners") or []):
            stage["owners"] = _insert_like(list(stage.get("owners") or []), role, list(other["owners"]))
        if (other.get("notify") or {}).get("role") == role and (stage.get("notify") or {}).get("role") != role:
            left_out.append(f"stage {stage.get('name')}: the board notifies {role}; root's record keeps its own notify role")
    key = lambda t: (t.get("from"), t.get("to"), t.get("action"))
    live_transitions = {key(t): t for t in live.get("transitions") or []}
    for transition in merged.get("transitions") or []:
        other = live_transitions.get(key(transition)) or {}
        if role in (other.get("actors") or []):
            transition["actors"] = _insert_like(list(transition.get("actors") or []), role, list(other["actors"]))
    for name, target in (live.get("reassign") or {}).items():
        if target == role and (merged.get("reassign") or {}).get(name) != role:
            left_out.append(f"reassign {name}: the board sends it to {role}; root's record keeps its own")
    if (live.get("queue") or {}).get("assignee") == role and (merged.get("queue") or {}).get("assignee") != role:
        left_out.append(f"queue: the board queues for {role}; root's record keeps its own")
    return merged, left_out


def stale_tenant_files(config_path: Path | None, live: Mapping[str, Any]) -> list[str]:
    """The tenant's files the board's document would rewrite, by name; [] when none or unknowable."""
    if config_path is None:
        return []
    from scripts.runtime_projection import declared_projection, stale_projection

    return sorted(str(path) for path in stale_projection(declared_projection(config_path, live)))


def tenant_repair_command(slug: str, live: Mapping[str, Any]) -> str:
    """The tenant's own supported repair: a switch to the runtime a role already has (SYRD-558)."""
    role = next((r for r in live.get("roles") or [] if r.get("runtime") and r.get("name") != "director"), None) \
        or next((r for r in live.get("roles") or [] if r.get("runtime")), None)
    if role is None:
        return ""
    return f"switchyard set-role-runtime {slug} {role['name']} --cli {role['runtime']}"


def reconcile_recorded_workflow(
    slug: str,
    recorded: Mapping[str, Any],
    *,
    config: Any,
    config_path: Path | None,
    apply: bool,
    from_live: str,
    replacing: str,
    operator_name: str,
    board_reader: Callable[[Any], tuple[dict | None, str]] | None,
    say: Callable[[str], None],
    write_record: Callable[[str, Mapping[str, Any]], Path],
    read_record: Callable[[str], tuple[dict | None, str]],
    add_role: str = "",
) -> tuple[int, str]:
    """Preview, or on review re-record, root's workflow from the board. Returns (exit status, journal detail).

    With `add_role`, only that role is taken from the board (SYRD-562): its
    definition and its place in stage owners and transition actors, added to
    root's record and nothing else, so a role added after adoption reaches root
    without replacing unrelated policy the two disagree on.
    """
    from scripts.ticket_board.project_provision import workflow_document_digest
    from scripts.ticket_board.workflow_config import validate

    if board_reader is None:
        from scripts import team_launcher as launcher

        board_reader = launcher.read_board_declared_workflow
    root_digest = workflow_document_digest(recorded)
    selecting = bool(from_live or replacing)
    live, problem = board_reader(config)
    if live is None:
        say(f"switchyard: root holds {slug}'s declared workflow (digest {root_digest}), and the board's "
            f"could not be read to compare it: {problem}")
        if selecting:
            say(f"switchyard: refusing to re-record root's workflow without the board's. {REFUSED_NOTHING_CHANGED}")
            return 1, "refused: board unreadable"
        say(f"switchyard: {REFUSED_NOTHING_CHANGED}")
        return 0, "board unreadable"
    live_digest = workflow_document_digest(live)
    stale = stale_tenant_files(config_path, live)
    repair = tenant_repair_command(slug, live)

    def tenant_advice() -> None:
        if stale:
            say(f"switchyard: {len(stale)} of the tenant's file(s) do not match the board's document:")
            for path in stale:
                say(f"    {path}")
            say(f"switchyard: root does not write the tenant's files; as {slug}'s Director run: `{repair}` "
                "-- it rewrites them from the board's document")

    if root_digest == live_digest and not stale:
        say(f"switchyard: root already holds {slug}'s declared workflow, and the board and the tenant's "
            f"files agree with it (digest {root_digest}). {REFUSED_NOTHING_CHANGED}")
        return 0, "all agree"
    say(f"switchyard: root's recorded workflow for {slug}: digest {root_digest}")
    say(f"switchyard: the board's declared workflow:      digest {live_digest}")
    if root_digest == live_digest:
        say("switchyard: root's record matches the board; only the tenant's files differ.")
        tenant_advice()
        if selecting:
            say(f"switchyard: there is nothing to re-record in root. {REFUSED_NOTHING_CHANGED}")
            return 1, "refused: root already matches"
        return 0, "tenant files stale"

    say("switchyard: they differ:")
    for line in record_difference(recorded, live):
        say(f"    {line}")
    absent = missing_roles(recorded, live)
    if add_role:
        try:
            target, left_out = with_role_added(recorded, live, add_role)
            validate(dict(target), project=slug)
        except Exception as exc:  # noqa: BLE001 - a role root cannot add this way is an answer
            say(f"switchyard: refusing to add role {add_role} to root's record: {exc}. {REFUSED_NOTHING_CHANGED}")
            tenant_advice()
            return 1, "refused: role cannot be added"
        target_digest = workflow_document_digest(target)
        say(f"switchyard: adding only role {add_role} makes root's record (digest {target_digest}):")
        from scripts.workflow_adoption import _canonical_workflow

        for line in unified_diff(_canonical_workflow(recorded).splitlines(), _canonical_workflow(target).splitlines(),
                                 fromfile="root's recorded workflow", tofile=f"with role {add_role} added", lineterm=""):
            say(f"    {line}")
        for line in left_out:
            say(f"switchyard: not taken from the board: {line}")
        if target_digest != live_digest:
            say("switchyard: every other difference above stays as root's record has it.")
        command = (f"pkexec switchyard adopt-workflow {slug} --apply --add-role {add_role} "
                   f"--from-live {live_digest} --replacing {root_digest}")
        done_detail = f"role {add_role} added from the board"
    else:
        try:
            # A check, not a transform: the record must carry the board's exact
            # document, so that the digest root holds is the digest the board runs.
            validate(dict(live), project=slug)
        except Exception as exc:  # noqa: BLE001 - a document root would not accept is an answer
            say(f"switchyard: the board's document is not one root will record for {slug}: {exc}. "
                f"{REFUSED_NOTHING_CHANGED}")
            tenant_advice()
            return 1, "refused: live document invalid"
        target, target_digest = live, live_digest
        command = (f"pkexec switchyard adopt-workflow {slug} --apply "
                   f"--from-live {live_digest} --replacing {root_digest}")
        done_detail = "re-recorded from the board"
    if not (apply and selecting):
        say("switchyard: re-recording root's workflow from the board is an operator's decision. Having "
            f"reviewed the difference above, run: `{command}`")
        if absent and not add_role:
            say(f"switchyard: the board declares role(s) root's record does not: {', '.join(absent)}. To add "
                "only a role, keeping every other line of root's record, review and run:")
            for name in absent:
                say(f"    pkexec switchyard adopt-workflow {slug} --add-role {name}")
        tenant_advice()
        if apply:
            say(f"switchyard: --apply alone does not choose a document. {REFUSED_NOTHING_CHANGED}")
            return 1, "refused: no selection"
        return 0, "dry-run"
    if from_live != live_digest or replacing != root_digest:
        moved = []
        if from_live != live_digest:
            moved.append(f"the board now holds {live_digest}, not the {from_live or 'unnamed'} reviewed")
        if replacing != root_digest:
            moved.append(f"root now holds {root_digest}, not the {replacing or 'unnamed'} being replaced")
        say(f"switchyard: refusing: {'; '.join(moved)}. Review the difference above again. "
            f"{REFUSED_NOTHING_CHANGED}")
        return 1, "refused: digest moved"

    path = write_record(slug, target)
    stored, stored_problem = read_record(slug)
    if stored is None or workflow_document_digest(stored) != target_digest:
        write_record(slug, recorded)
        say(f"switchyard: the record at {path} did not read back as written "
            f"({stored_problem or 'a different digest'}); root's previous record was restored.")
        return 1, "record did not read back; restored"
    after, after_problem = board_reader(config)
    if after is None or workflow_document_digest(after) != live_digest:
        write_record(slug, recorded)
        now = workflow_document_digest(after) if after is not None else f"unreadable ({after_problem})"
        say(f"switchyard: the board changed while root recorded it (now {now}); root's previous record "
            f"(digest {root_digest}) was restored. Review it again. {REFUSED_NOTHING_CHANGED}")
        return 1, "board moved during the write; restored"
    say(f"switchyard: re-recorded {slug}'s declared workflow at {path} from the board "
        f"(digest {root_digest} -> {target_digest}{f', role {add_role} only' if add_role else ''}, by {operator_name})")
    tenant_advice()
    return 0, done_detail
