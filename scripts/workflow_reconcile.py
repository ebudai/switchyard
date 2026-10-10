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

Root does not write the tenant's files. They are the tenant's, in directories
the tenant controls, and a root process writing through those follows whatever
the tenant has placed there. The tenant already has a supported repair -- a
runtime switch to the runtime a role already has brings a stale projection in
line from the board's document (SYRD-558) -- so the exact command is named
instead. Reading the tenant's files to say which are stale names files; it
never shows their content.
"""

from __future__ import annotations

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
) -> tuple[int, str]:
    """Preview, or on review re-record, root's workflow from the board. Returns (exit status, journal detail)."""
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
    try:
        # A check, not a transform: the record must carry the board's exact
        # document, so that the digest root holds is the digest the board runs.
        validate(dict(live), project=slug)
    except Exception as exc:  # noqa: BLE001 - a document root would not accept is an answer
        say(f"switchyard: the board's document is not one root will record for {slug}: {exc}. "
            f"{REFUSED_NOTHING_CHANGED}")
        tenant_advice()
        return 1, "refused: live document invalid"
    command = (f"pkexec switchyard adopt-workflow {slug} --apply "
               f"--from-live {live_digest} --replacing {root_digest}")
    if not (apply and selecting):
        say("switchyard: re-recording root's workflow from the board is an operator's decision. Having "
            f"reviewed the difference above, run: `{command}`")
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

    path = write_record(slug, live)
    stored, stored_problem = read_record(slug)
    if stored is None or workflow_document_digest(stored) != live_digest:
        write_record(slug, recorded)
        say(f"switchyard: the record at {path} did not read back as the board's document "
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
        f"(digest {root_digest} -> {live_digest}, by {operator_name})")
    tenant_advice()
    return 0, "re-recorded from the board"
