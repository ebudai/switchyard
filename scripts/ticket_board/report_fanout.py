"""One report to one report ancestor, or to each, and its follow-ups: previewed, then confirmed once (SYRD-721).

`file-report --to <project>` files to one member of the reporting project's
ancestry (`report_ancestry`), and `--to all` to each, nearest first. The
SYRD-602 two steps hold for the whole set: the preview writes nothing and names
every destination and how many tickets will be created; the submission needs
the one digest that binds the complete target set and each destination's own
confirmation. Each destination still checks its own confirmation itself, as
before. Without `--to`, `file-report` files to the immediate parent exactly as
it always did. Ancestry is never permission: nothing here files without that
digest, and no report fans out because ancestry exists.

**Cross-board writes are not one transaction.** So the logical report has a
stable report group, `rg-<20 hex>` from what the report says (not where it
goes), sent to each board that records one (`report_groups` in its client
configuration) and bound into its confirmation. Each board already turns a
replayed confirmation into the ticket it created (pgu998), and the reporting
project keeps a receipt per destination (`receipts_dir`): a confirmed retry
skips the boards that took the report and files only on the rest, and a board
that took it but whose answer was lost answers the retry with that same ticket.
A partial result is said exactly, and the command fails.

`report-follow-up --group <rg> --to all|<project>` follows up the tickets that
group filed, one or all, with the same preview, single confirmation and
idempotent partial retry. A destination is read from its receipt -- its board
and the credential it was filed with -- so a report filed before its parent
changed can still be followed up while that credential is held and the board
answers as itself.

No token is printed, recorded in a receipt, or put in a preview: a receipt
names the credential's file, never its contents.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from . import report_ancestry as ra
from . import report_confirmation as rc
from . import report_follow_up as rf
from .write_client import TicketBoardWriteError, _normalize_api_url

RECEIPTS_ENV = "TICKET_BOARD_REPORT_RECEIPTS_DIR"
RECEIPT_SCHEMA = "switchyard.report-receipt.v1"
FANOUT_VERSION = "syrd721-fanout-v1"
ALL = "all"


def report_group(*, source_project: str, title: str, body: str, requested_stage: str,
                 external_source_ref: str) -> str:
    """The logical report's identity: what it says and who says it, not where it goes, so filing it to one
    ancestor and later to all of them is one group, and the first board answers the second time with its ticket."""
    canonical = json.dumps({"source_project": source_project, "title": title, "body": body,
                            "requested_stage": requested_stage, "external_source_ref": external_source_ref},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "rg-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def fanout_digest(kind: str, subject: str, destinations: list["Destination"]) -> str:
    """The one confirmation for exactly these destinations, in this order, each with its own confirmation."""
    canonical = json.dumps({"version": FANOUT_VERSION, "kind": kind, "subject": subject,
                            "destinations": [[d.link.project, d.ticket_prefix, d.digest] for d in destinations]},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:rc.DIGEST_CHARS]


# ------------------------------------------------------------------ receipts


def receipts_dir(project: str, environ: Mapping[str, str] = os.environ) -> Path:
    """Where the reporting project keeps its receipts: owner-only, beside its other state."""
    named = str(environ.get(RECEIPTS_ENV) or "").strip()
    if named:
        return Path(named).expanduser()
    state = str(environ.get("XDG_STATE_HOME") or "").strip() or str(Path(environ.get("HOME") or "~") / ".local/state")
    return Path(state).expanduser() / "switchyard" / project / "report-receipts"


def load_receipt(directory: Path, group: str) -> dict[str, Any]:
    try:
        held = json.loads((directory / f"{group}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) and held.get("schema") == RECEIPT_SCHEMA and held.get("group") == group else {}


def save_receipt(directory: Path, receipt: dict[str, Any]) -> str:
    """Write a receipt atomically, owner-only. "" or why not: a receipt that cannot be kept is said, never fatal,
    since every board still answers a retry with the ticket it already created."""
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        staged = directory / f".{receipt['group']}.{os.getpid()}.tmp"
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")
        staged.replace(directory / f"{receipt['group']}.json")
    except OSError as exc:
        return f"its receipt could not be kept in {directory} ({exc.strerror or exc})"
    return ""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------------------------------------------------ destinations


@dataclass(frozen=True)
class Destination:
    position: int
    link: ra.Link
    identity: ra.Identity
    credential: Path
    #: What the credential is (bound to which project), never its value.
    credential_state: str
    digest: str = ""
    ticket: dict | None = None

    @property
    def ticket_prefix(self) -> str:
        return self.identity.ticket_prefix or self.link.ticket_prefix

    @property
    def label(self) -> str:
        return f"{self.link.name} ({self.link.project}, tickets {self.ticket_prefix}-N)"


def resolve_targets(spec: str, links: list[ra.Link], *, what: str) -> list[tuple[int, ra.Link]]:
    """`--to`'s destinations among `links`: all of them, or the one named by project, ticket prefix or name."""
    numbered = list(enumerate(links, start=1))
    wanted = spec.strip()
    if wanted.casefold() == ALL:
        return numbered
    matches = [(n, link) for n, link in numbered
               if wanted.casefold() in (link.project.casefold(), link.name.casefold())
               or (link.ticket_prefix and wanted.casefold() == link.ticket_prefix.casefold())]
    listed = ", ".join(f"{link.name} ({link.project})" for link in links) or "none"
    if len(matches) > 1:
        raise TicketBoardWriteError(f"{what}: --to {wanted} names more than one of {listed}; name it by project. "
                                    "Nothing was sent.")
    if not matches:
        raise TicketBoardWriteError(f"{what}: --to {wanted} is not one of this project's report ancestors "
                                    f"({listed}); a report goes only to a verified ancestor. Nothing was sent.")
    return matches


def prepare(project: str, targets: list[tuple[int, ra.Link]], *, credentials: Callable[[int, ra.Link], Path | None],
            what: str, ask: Callable[[str], tuple[dict, str]] | None = None, bound: bool = False) -> list[Destination]:
    """Every destination checked before anything is sent: it answers as the project root recorded, and this project
    holds a usable credential of its own for it. All refusals are said together, with nothing sent."""
    prepared, problems = [], []
    for position, link in targets:
        identity = ra.verify(link, ask=ask)
        path = credentials(position, link)
        usable, said = ra.credential_state(path, project)
        if bound and usable and not said.startswith("bound"):
            usable, said = False, f"{said} -- a follow-up needs the credential bound to {project}"
        if not identity.verified:
            problems.append(identity.problem)
        if not usable:
            problems.append(f"{link.name} ({link.project}): {project} holds no usable credential for its board "
                            f"({said}); an operator gives it one with `sudo switchyard upgrade {project} --only "
                            "upstream-report`")
        if path is not None:  # a destination with any problem is refused below, with nothing sent
            prepared.append(Destination(position, link, identity, path, said))
    if problems:
        raise TicketBoardWriteError(f"{what}: " + "; ".join(problems) + ". Nothing was sent.")
    return prepared


def _recorded_credential(entry: dict[str, Any]) -> Path | None:
    named = str(entry.get("credential_file") or "").strip()
    return Path(named) if named else None


def _client_for(client: Any, destination: Destination) -> Any:
    """The write client for one destination: its board and its own credential, never another board's."""
    return replace(client, report_board_url=destination.link.url, report_token="",
                   report_token_file=str(destination.credential))


def _chain_or_refuse(project: str, environ: Mapping[str, str], what: str) -> list[ra.Link]:
    links, problem = ra.chain(project, ra.registry_dir(environ))
    if problem:
        raise TicketBoardWriteError(f"{what}: {project}'s report ancestry cannot be used: {problem}. Nothing was sent.")
    if not links:
        raise TicketBoardWriteError(f"{what}: root's records name no report parent for {project}, so --to has no "
                                    "ancestor to name; without --to, file-report files where this pane's "
                                    "environment says. Nothing was sent.")
    return links


def _summary(kind: str, results: list[dict[str, Any]]) -> tuple[str, int]:
    failed = sum(1 for result in results if result["outcome"] == "failed")
    lines = [f"{kind}: {len(results) - failed} of {len(results)} destination(s) took it" + (
        f"; {failed} FAILED" if failed else "")]
    for result in results:
        said = {"created": f"created {result.get('ticket_id')}",
                "added": f"follow-up added to {result.get('ticket_id')}",
                "already": f"already there as {result.get('ticket_id')} (nothing new)",
                "failed": f"FAILED: {result.get('error')}"}[result["outcome"]]
        lines.append(f"  {result['name']} ({result['project']}): {said}")
    return "\n".join(lines), failed


# ------------------------------------------------------------------ file-report --to


def file_report_to(args: Any, client: Any, environ: Mapping[str, str], *,
                   ask: Callable[[str], tuple[dict, str]] | None = None) -> dict[str, Any] | None:
    """Preview (None) or file one report to the named ancestor or to each, after one exact confirmation."""
    what = "file-report"
    project = args.origin_project.strip()
    if not project:
        raise TicketBoardWriteError("file-report requires --origin-project or TICKET_BOARD_REPORT_ORIGIN_PROJECT")
    if args.expect and not args.submit_upstream:
        raise TicketBoardWriteError("file-report: --expect only confirms a submission; add --submit-upstream, or "
                                    "leave both off to preview. Nothing was filed.")
    links = _chain_or_refuse(project, environ, what)
    token_file = args.report_token_file.strip()
    destinations = prepare(project, resolve_targets(args.to, links, what=what), what=what, ask=ask,
                           credentials=lambda n, link: ra.credential_path(link, immediate=n == 1, token_file=token_file))
    requested = "backlog" if args.defer else ""
    group = report_group(source_project=project, title=args.title, body=args.body, requested_stage=requested,
                         external_source_ref=args.external_source_ref)
    destinations = [replace(d, digest=rc.report_digest(
        destination={"project": d.link.project, "ticket_prefix": d.ticket_prefix}, source_project=project,
        title=args.title, body=args.body, requested_stage=requested, external_source_ref=args.external_source_ref,
        report_group=group if d.identity.report_groups else "")) for d in destinations]
    digest = fanout_digest("file_report", group, destinations)
    directory = receipts_dir(project, environ)
    receipt = load_receipt(directory, group) or {"schema": RECEIPT_SCHEMA, "group": group, "source_project": project,
                                                 "title": args.title, "destinations": {}, "follow_ups": {}}
    filed = {d.link.project: receipt["destinations"].get(d.link.project, {}) for d in destinations}
    if not args.submit_upstream:
        print(_file_preview(args, project, requested, group, destinations, filed, digest))
        return None
    if args.expect.strip() != digest:
        raise TicketBoardWriteError(
            "file-report --submit-upstream needs --expect <digest> from this report's preview. Nothing was filed."
            if not args.expect else
            f"file-report: this is not the report that was previewed: its text, its destinations, or a destination's "
            f"identity changed (its digest is now {digest}). Preview it again. Nothing was filed.")
    results = []
    for d in destinations:
        held = filed[d.link.project]
        base = {"project": d.link.project, "name": d.link.name, "ticket_prefix": d.ticket_prefix}
        # The group is what the report says, so a ticket a board already holds for it is this report, whatever that
        # board's release added to the confirmation since.
        if held.get("ticket_id"):
            results.append({**base, "outcome": "already", "ticket_id": held["ticket_id"]})
            continue
        payload = {"title": args.title, "body": args.body, "origin_project": project,
                   "external_source_ref": args.external_source_ref, "confirmation": d.digest,
                   **({"requested_stage": requested} if requested else {}),
                   **({"report_group": group} if d.identity.report_groups else {})}
        entry = {"url": d.link.url, "name": d.link.name, "ticket_prefix": d.ticket_prefix,
                 "credential_file": str(d.credential), "digest": d.digest, "group_recorded": d.identity.report_groups}
        try:
            response = _client_for(client, d)._post_report(payload)
        except (TicketBoardWriteError, OSError, ValueError) as exc:
            receipt["destinations"][d.link.project] = {**held, **entry, "failed_at": _now(), "error": str(exc)[:500]}
            results.append({**base, "outcome": "failed", "error": str(exc)[:500]})
        else:
            ticket = response.get("ticket") if isinstance(response.get("ticket"), dict) else response
            ticket_id = str(ticket.get("id") or "")
            receipt["destinations"][d.link.project] = {**entry, "ticket_id": ticket_id, "filed_at": _now()}
            results.append({**base, "outcome": "already" if response.get("replayed") else "created",
                            "ticket_id": ticket_id})
        kept = save_receipt(directory, receipt)  # after each board, so a crash between boards loses no receipt
        if kept:
            print(f"file-report: WARNING: {kept}; each board still answers a retry with the ticket it created.",
                  file=sys.stderr)
    said, failed = _summary("file-report", results)
    print(said, file=sys.stderr)
    if failed:
        raise TicketBoardWriteError(
            f"file-report: {failed} of {len(results)} destination(s) did not take report group {group}. Run the same "
            f"command again with --submit-upstream --expect {digest}: it files only where the report is missing, and "
            "a board that took it answers with its ticket. Nothing was filed twice.")
    return {"report_group": group, "destinations": results}


def _file_preview(args: Any, project: str, requested: str, group: str, destinations: list[Destination],
                  filed: dict[str, dict], digest: str) -> str:
    fresh = [d for d in destinations if not filed[d.link.project].get("ticket_id")]
    names = ", ".join(d.link.name for d in destinations)
    target = (f"All ancestors ({names})" if len(destinations) > 1 else destinations[0].link.name)
    flat = " ".join(args.body.split())
    lines = [
        "file-report PREVIEW: nothing has been filed.",
        f"Destination: {target}. This would create {len(fresh)} NEW ticket{'s' if len(fresh) != 1 else ''} on "
        + ("ANOTHER project's board:" if len(destinations) == 1 else
           "OTHER projects' boards, one per board, nearest first:"),
    ]
    for d in destinations:
        held = filed[d.link.project]
        state = (f"already filed as {held['ticket_id']}: not filed again" if d not in fresh else "a new ticket")
        lines.append(f"  {d.position}. {d.label}: {state}; identity verified; credential {d.credential_state}"
                     + ("" if d.identity.report_groups else "; this board records no report group (older release)"))
    lines += [
        f"  from:         project {project}, caller role {args.caller_role or '<none>'}",
        f"  title:        {args.title}",
        f"  state:        {'Backlog (deferred), as a request' if requested else 'Triage'}",
        f"  source ref:   {args.external_source_ref or '<none>'}",
        f"  body:         {len(args.body)} characters: {flat if len(flat) <= 300 else flat[:297] + '...'}",
        f"  report group: {group} (each board's ticket carries it)",
        f"  digest:       {digest} (binds every destination above and this exact report)",
        "File to more than one ancestor only when the same report genuinely concerns each; otherwise choose one.",
        f"To file exactly this, run the same command again with: --submit-upstream --expect {digest}",
    ]
    return "\n".join(lines)


def keep_single_receipt(environ: Mapping[str, str], *, project: str, group: str, title: str, destination: dict,
                        board_url: str, credential_file: str, digest: str, response: dict) -> None:
    """The receipt of a report filed without --to, exactly as before and with no group sent, so `--to all` later
    files it only where it is missing."""
    directory = receipts_dir(project, environ)
    receipt = load_receipt(directory, group) or {"schema": RECEIPT_SCHEMA, "group": group, "source_project": project,
                                                 "title": title, "destinations": {}, "follow_ups": {}}
    ticket = response.get("ticket") if isinstance(response.get("ticket"), dict) else response
    receipt["destinations"][destination["project"]] = {
        "url": board_url.rstrip("/"), "name": str(destination.get("project_name") or destination["project"]),
        "ticket_prefix": destination["ticket_prefix"], "credential_file": credential_file, "digest": digest,
        "group_recorded": False, "ticket_id": str(ticket.get("id") or ""), "filed_at": _now()}
    kept = save_receipt(directory, receipt)
    if kept:
        print(f"file-report: WARNING: {kept}", file=sys.stderr)


# ------------------------------------------------------------------ report-follow-up --to


def report_follow_up_to(args: Any, client: Any, environ: Mapping[str, str], *,
                        ask: Callable[[str], tuple[dict, str]] | None = None) -> dict[str, Any] | None:
    """Preview (None) or send one follow-up to one filed destination or to each, after one exact confirmation."""
    what = "report-follow-up"
    if args.expect and not args.submit_upstream:
        raise TicketBoardWriteError("report-follow-up: --expect only confirms a submission; add --submit-upstream, "
                                    "or leave both off to preview. Nothing was sent.")
    project = str(environ.get("TICKET_BOARD_REPORT_ORIGIN_PROJECT") or environ.get("TICKET_BOARD_PROJECT") or "")
    project = project.strip()
    if not project:
        raise TicketBoardWriteError("report-follow-up --to needs this pane's project "
                                    "(TICKET_BOARD_REPORT_ORIGIN_PROJECT). Nothing was sent.")
    group = (args.group or "").strip()
    if group:
        receipt = load_receipt(receipts_dir(project, environ), group)
        held = {slug: entry for slug, entry in (receipt.get("destinations") or {}).items() if entry.get("ticket_id")}
        if not held:
            raise TicketBoardWriteError(f"report-follow-up: {project} holds no receipt of report group {group} on any "
                                        "board. Nothing was sent.")
        links = [ra.Link(slug, str(entry.get("name") or slug), str(entry["url"]), str(entry.get("ticket_prefix") or ""),
                         "receipt") for slug, entry in held.items()]
        targets = resolve_targets(args.to, links, what=what)
        tickets = {link.project: str(held[link.project]["ticket_id"]) for _n, link in targets}
        destinations = prepare(project, targets, what=what, ask=ask, bound=True,
                               credentials=lambda _n, link: _recorded_credential(held[link.project]))
    else:
        if not args.ticket or args.to.strip().casefold() == ALL:
            raise TicketBoardWriteError("report-follow-up: --to all follows up a report group (--group <rg>, from "
                                        "file-report's result); a ticket ID belongs to one board, so name that board "
                                        "with --to <project> --ticket <ID>. Nothing was sent.")
        links = _chain_or_refuse(project, environ, what)
        token_file = args.report_token_file.strip()
        targets = resolve_targets(args.to, links, what=what)
        tickets = {link.project: args.ticket.strip().upper() for _n, link in targets}
        destinations = prepare(project, targets, what=what, ask=ask, bound=True, credentials=lambda n, link:
                               ra.credential_path(link, immediate=n == 1, token_file=token_file))
    ready = []
    for d in destinations:
        try:
            ticket = rf.read_report(_normalize_api_url(d.link.url), tickets[d.link.project], timeout=client.timeout)
        except LookupError as exc:
            raise TicketBoardWriteError(f"{exc} ({d.link.name})") from exc
        origin = str(ticket.get("origin_project") or "").strip()
        if origin != project:
            raise TicketBoardWriteError(
                f"report-follow-up: {ticket['id']} on {d.link.name} was "
                f"{'filed from ' + origin if origin else 'not filed as a report'}; a follow-up is only for a report "
                f"{project} filed. Nothing was sent.")
        ready.append(replace(d, ticket=ticket, digest=rf.follow_up_digest(
            destination={"project": d.link.project, "ticket_prefix": d.ticket_prefix}, ticket_id=str(ticket["id"]),
            origin_project=project, external_source_ref=str(ticket.get("external_source_ref") or "").strip(),
            kind=args.kind, text=args.text)))
    digest = fanout_digest("report_follow_up", group or args.ticket.strip().upper(), ready)
    if not args.submit_upstream:
        print(_follow_up_preview(args, project, group, ready, digest))
        return None
    if args.expect.strip() != digest:
        raise TicketBoardWriteError(
            "report-follow-up --submit-upstream needs --expect <digest> from this follow-up's preview. Nothing was sent."
            if not args.expect else
            f"report-follow-up: this is not the follow-up that was previewed: its kind, text, tickets or destinations "
            f"changed (its digest is now {digest}). Preview it again. Nothing was sent.")
    results = []
    for d in ready:
        base = {"project": d.link.project, "name": d.link.name, "ticket_id": d.ticket["id"]}
        try:
            response = _client_for(client, d)._post_report(
                {"ticket_id": str(d.ticket["id"]), "kind": args.kind, "text": args.text, "confirmation": d.digest},
                action="report_follow_up")
        except (TicketBoardWriteError, OSError, ValueError) as exc:
            results.append({**base, "outcome": "failed", "error": str(exc)[:500]})
        else:
            results.append({**base, "outcome": "already" if response.get("replayed") else "added"})
    if group:
        directory = receipts_dir(project, environ)
        receipt = load_receipt(directory, group)
        receipt.setdefault("follow_ups", {})[digest] = {"kind": args.kind, "at": _now(), "results": results}
        kept = save_receipt(directory, receipt)
        if kept:
            print(f"report-follow-up: WARNING: {kept}", file=sys.stderr)
    said, failed = _summary("report-follow-up", results)
    print(said, file=sys.stderr)
    if failed:
        raise TicketBoardWriteError(
            f"report-follow-up: {failed} of {len(results)} destination(s) did not take it. Run the same command again "
            f"with --submit-upstream --expect {digest}: a board that took it answers that it already has it, and adds "
            "nothing.")
    return {"report_group": group, "destinations": results}


def _follow_up_preview(args: Any, project: str, group: str, ready: list[Destination], digest: str) -> str:
    lines = [
        "Report follow-up PREVIEW: nothing has been sent.",
        ("This would add one follow-up to this report" if len(ready) == 1 else
         f"This would add one follow-up to each of these {len(ready)} reports")
        + (f" of report group {group}" if group else "") + ("," if len(ready) == 1 else ", nearest first:"),
    ]
    for d in ready:
        ticket = d.ticket or {}
        lines.append(f"  {d.position}. {ticket.get('id')} on {d.label}: {ticket.get('title', '')} "
                     f"({ticket.get('state', '')}); credential {d.credential_state}")
    lines += [
        f"  kind:   {args.kind} ({rf.KINDS[args.kind]})",
        f"  as:     reporter:{project}",
        "  text:", *[f"    {line}" for line in args.text.splitlines() or [""]],
        "Each is added as a comment and that board's Director is told; it changes nothing else on any ticket.",
        f"digest: {digest}",
        f"To send exactly this, run the same command with: --submit-upstream --expect {digest}",
    ]
    return "\n".join(lines)
