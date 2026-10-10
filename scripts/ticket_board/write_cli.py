"""The `ticket-board-write` command line: its parser, free text and dispatch.

`write_client` owns the client, its endpoint resolution and every request it
sends; this module owns only turning argv into one client call and printing the
answer. Everything it takes from the client module is read through that module
at call time, so a caller that patches `write_client` is still observed here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    from . import workflow_relays, write_client
except ImportError:  # pragma: no cover - `python write_client.py` runs outside the package
    import workflow_relays
    import write_client


def _add_user_acceptance_relay(client: Any, *, apply: bool, expected_revision: int | None) -> dict[str, Any]:
    """Preview, or apply, the one transition that lets the Director relay the User's acceptance (SYRD-535).

    Read-only by default: the live document and revision, the transition that
    would be added and nothing else, the digests before and after, and the
    board's own validation of the result as a dry run. `--apply` takes the
    revision the preview showed, so what is applied is what was reviewed; the
    board refuses it if the workflow has moved since.
    """
    # One endpoint for the read, the dry run and the apply, resolved once.
    client = client.pinned_to_resolved_endpoint()
    current = client.read_workflow()
    document, revision = current.get("document"), current.get("revision")
    if not isinstance(document, dict) or type(revision) is not int:
        raise write_client.TicketBoardWriteError("this board runs no declared workflow, so there is nothing to add a relay to")
    result: dict[str, Any] = {
        "endpoint": client.workflow_endpoint,
        "revision": revision,
        "before_digest": workflow_relays.document_digest(document),
        "rejection": workflow_relays.rejection_relay_finding(document),
        "applied": False,
    }
    updated, relay, reason = workflow_relays.with_user_acceptance_relay(document)
    result["reason"] = reason
    result["added"] = relay
    if updated is None:
        return result
    result["after_digest"] = workflow_relays.document_digest(updated)
    if apply:
        if expected_revision != revision:
            raise write_client.TicketBoardWriteError(
                f"the workflow is at revision {revision}, not the {expected_revision} you reviewed; preview it again"
            )
    validated = client.configure_workflow(updated, expected_revision=revision, dry_run=True, same_endpoint=True)
    result["validated"] = bool(validated.get("dry_run"))
    if not apply:
        result["apply_with"] = f"ticket-board-write add-user-acceptance-relay --apply --expected-revision {revision}"
        return result
    applied = client.configure_workflow(updated, expected_revision=revision, dry_run=False, same_endpoint=True)
    result.update(applied=True, revision_after=applied.get("revision"))
    return result


def not_connected_for_reports(environ: Any) -> str:
    """The refusal a pane with no report credential sees, naming the one narrow step (SYRD-548).

    Never a full upgrade: connecting a tenant for reports writes its report URL,
    its credential and nothing else, so the remedy must not cost it every other
    pending upgrade phase.
    """
    project = str(environ.get("TICKET_BOARD_PROJECT") or "").strip() or "<project>"
    return (
        f"file-report: {project} is not connected to an upstream board for reports: this pane has no report "
        "credential (TICKET_BOARD_TENANT_REPORT_TOKEN_FILE is unset). An operator connects it with one narrow "
        f"step that runs no other upgrade phase: `sudo switchyard upgrade {project} --only upstream-report "
        "--upstream-report-url <upstream-board-url>`; the next launch of each pane carries it. Nothing was filed."
    )


def _records_report_request(response: dict[str, Any]) -> bool:
    """Whether the board wrote a report's Backlog request on the ticket it created (SYRD-548)."""
    ticket = response.get("ticket") if isinstance(response, dict) else None
    comments = ticket.get("comments") if isinstance(ticket, dict) else None
    return any(
        isinstance(c, dict) and c.get("who") == "ticket_board_service" and "requested Backlog" in str(c.get("text", ""))
        for c in comments or []
    )


def _ticket_from_response(response: dict[str, Any]) -> dict[str, Any]:
    ticket = response.get("ticket")
    if not isinstance(ticket, dict):
        raise write_client.TicketBoardWriteError("ticket board response did not include ticket")
    return ticket


#: Free-text fields carry prose -- comment bodies, reasons, ticket bodies,
#: implementation notes. Prose in this project routinely contains backticks
#: (`function_name()`), dollar signs, quotes and newlines, and passing it as a
#: shell argument means the author's shell gets a vote on what reaches the
#: board. On SYRD-195 it took one: a review comment was posted with its
#: operative phrase missing, because the shell had executed the backticked
#: phrase instead of passing it (SYRD-196).
#:
#: So every free-text option gains two companions that cannot be interpreted:
#: `--<name>-file PATH` reads the bytes from a file, and `--<name> -` reads
#: them from standard input. The direct `--<name> VALUE` form is unchanged, so
#: nothing that works today stops working.
#:
#: Content is preserved exactly. No stripping, no newline normalisation, no
#: shell, no `eval`: the bytes in the file are the bytes the board stores.
FREE_TEXT_STDIN = "-"


def add_free_text_argument(parser, flag, *, required=False, default="", help=None):
    """Register `--flag`, `--flag-file`, and the resolution rule for both.

    `required` is enforced after parsing rather than by argparse, because either
    form may satisfy it and argparse can only require one option at a time.
    """
    dest = flag.lstrip("-").replace("-", "_")
    direct_help = help or f"{dest.replace('_', ' ')} text"
    parser.add_argument(
        flag,
        dest=dest,
        default=None,
        help=f"{direct_help}. Use {FREE_TEXT_STDIN} to read it from standard input.",
    )
    parser.add_argument(
        f"{flag}-file",
        dest=f"{dest}_file",
        default=None,
        metavar="PATH",
        help=(
            f"read {dest.replace('_', ' ')} from PATH ({FREE_TEXT_STDIN} for standard input), "
            "so backticks, dollar signs, quotes, newlines and leading dashes reach "
            "the board literally"
        ),
    )
    existing = list(getattr(parser, "_free_text_fields", ()))
    existing.append((flag, dest, required, default))
    parser._free_text_fields = existing
    parser.set_defaults(_free_text_fields=existing)


def _read_text_stream(stream, parser, flag):
    data = stream.buffer.read() if hasattr(stream, "buffer") else stream.read()
    if isinstance(data, bytes):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            parser.error(f"{flag}: standard input is not valid UTF-8 ({exc})")
    return data


def _read_free_text(source, parser, flag):
    import sys as _sys

    if source == FREE_TEXT_STDIN:
        if _sys.stdin is None:
            parser.error(f"{flag}: asked to read standard input, but there is none")
        if _sys.stdin.isatty():
            # Reading a terminal here would hang with no output, which reads as
            # the command having silently stopped.
            parser.error(
                f"{flag}: standard input is a terminal; redirect a file or a heredoc into it"
            )
        return _read_text_stream(_sys.stdin, parser, flag)
    try:
        with open(source, "rb") as handle:
            return handle.read().decode("utf-8")
    except FileNotFoundError:
        parser.error(f"{flag}: no such file: {source}")
    except IsADirectoryError:
        parser.error(f"{flag}: is a directory: {source}")
    except PermissionError:
        parser.error(f"{flag}: cannot read: {source}")
    except UnicodeDecodeError as exc:
        parser.error(f"{flag}: {source} is not valid UTF-8 ({exc})")


def resolve_free_text_arguments(args, parser):
    """Turn --flag / --flag-file into the single value the dispatch already reads.

    Resolved onto the same attribute the direct flag would have set, so every
    call site downstream is untouched.
    """
    for flag, dest, required, default in getattr(args, "_free_text_fields", ()):
        direct = getattr(args, dest, None)
        path = getattr(args, f"{dest}_file", None)
        if direct is not None and path is not None:
            parser.error(f"{flag} and {flag}-file are mutually exclusive; give one")
        if path is not None:
            text = _read_free_text(path, parser, flag)
        elif direct is not None:
            text = _read_free_text(direct, parser, flag) if direct == FREE_TEXT_STDIN else direct
        else:
            text = default
        if required and not text.strip():
            parser.error(f"{flag} (or {flag}-file) is required and must not be empty")
        setattr(args, dest, text)
    return args


def _build_parser() -> argparse.ArgumentParser:
    caller_default = write_client.default_caller_role()
    caller_default_display = caller_default or "unset"
    parser = argparse.ArgumentParser(description="Write tickets through the board action API.")
    parser.add_argument(
        "--board-url",
        default=None,
        help=(
            "Board root or /api/tickets URL. Supplying it is authoritative: the write "
            "goes there or fails, and never falls back to a socket belonging to "
            "another project (SYRD-198). Default: TICKET_BOARD_URL."
        ),
    )
    parser.add_argument(
        "--socket",
        dest="socket_path",
        default=None,
        help=(
            "Unix-domain board socket for local pane writes. Defaults to TICKET_BOARD_SOCKET "
            "(or legacy PGU_TICKET_BOARD_SOCKET), "
            f"or {write_client.DEFAULT_BOARD_SOCKET} when it exists and --board-url is the default."
        ),
    )
    parser.add_argument(
        "--caller-role",
        default=caller_default,
        help=(
            "Value for X-Ticket-Board-Caller-Role "
            "(required unless TICKET_BOARD_CALLER_ROLE or legacy PGU_TICKET_BOARD_CALLER_ROLE is set; "
            f"currently {caller_default_display})"
        ),
    )
    parser.add_argument(
        "--report-token",
        default=write_client.DEFAULT_REPORT_TOKEN,
        help="Tenant report token for file-report. Default: TICKET_BOARD_TENANT_REPORT_TOKEN or TICKET_BOARD_REPORT_TOKEN.",
    )
    parser.add_argument(
        "--report-token-file",
        default=write_client.DEFAULT_REPORT_TOKEN_FILE,
        help=(
            "EnvironmentFile-style file containing TICKET_BOARD_TENANT_REPORT_TOKEN for file-report. "
            "Default: TICKET_BOARD_TENANT_REPORT_TOKEN_FILE or TICKET_BOARD_REPORT_TOKEN_FILE."
        ),
    )
    parser.add_argument(
        "--report-board-url",
        default=write_client.DEFAULT_REPORT_BOARD_URL,
        help="Target board URL for file-report. Default: TICKET_BOARD_REPORT_URL or PGU_TICKET_BOARD_REPORT_URL; falls back to --board-url.",
    )
    parser.add_argument(
        "--write-token",
        default=write_client.DEFAULT_WRITE_TOKEN,
        help="HTTP write token for TCP board action routes. Default: TICKET_BOARD_WRITE_TOKEN or legacy PGU_TICKET_BOARD_WRITE_TOKEN.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "verify-caller",
        help="ask the board which role this process is, over the local socket; changes nothing",
    )
    workflow_action = subparsers.add_parser("workflow-action")
    workflow_action.add_argument("ticket_id")
    workflow_action.add_argument("action")
    workflow_action.add_argument("--payload-json", default="{}")
    relay = subparsers.add_parser(
        "add-user-acceptance-relay",
        help=(
            "preview (default) or apply the one transition that lets the Director relay the User's "
            "acceptance, copied from the User's own sign-off; changes nothing else"
        ),
    )
    relay.add_argument("--apply", action="store_true", help="apply it; requires --expected-revision from the preview")
    relay.add_argument("--expected-revision", type=int, default=None, help="the revision the preview showed")
    workflow_flags = subparsers.add_parser("set-workflow-flags")
    workflow_flags.add_argument("ticket_id")
    workflow_flags.add_argument("--patch-json", required=True)

    create = subparsers.add_parser("create-ticket")
    create.add_argument("--title", required=True)
    add_free_text_argument(create, "--body", required=True)
    create.add_argument("--assignee", default=None, help="omit to let the board place the ticket")
    create.add_argument("--state", default="analysis")
    create.add_argument("--parent-id", default="")
    create.add_argument("--draft", action="store_true")
    create.add_argument("--screenshot")
    create.add_argument("--blocked-by", action="append", default=[])
    add_free_text_argument(create, "--blocked-reason", default="")
    add_free_text_argument(create, "--implementation", default="")
    add_free_text_argument(create, "--audit-prompt", default="")
    add_free_text_argument(create, "--comment-text", default="")
    create.add_argument("--needs-inspection", action="store_true")
    create.add_argument("--needs-audit", action=argparse.BooleanOptionalAction, default=True)
    create.add_argument("--needs-user-signoff", action="store_true")

    file_bug = subparsers.add_parser("file-bug")
    file_bug.add_argument("--title", required=True)
    add_free_text_argument(file_bug, "--body", required=True)
    file_bug.add_argument("--source-ticket-id", required=True)
    file_bug.add_argument("--assignee", default="unassigned")
    file_bug.add_argument("--needs-audit", action=argparse.BooleanOptionalAction, default=True)

    file_report = subparsers.add_parser("file-report")
    file_report.add_argument("--title", required=True)
    add_free_text_argument(file_report, "--body", required=True)
    file_report.add_argument("--origin-project", default=write_client.DEFAULT_REPORT_ORIGIN_PROJECT)
    file_report.add_argument("--external-source-ref", default="")
    file_report.add_argument(
        "--defer",
        action="store_true",
        help=(
            "ask for the report to land in Backlog (deferred) rather than Triage. A request: the "
            "board's report-intake policy decides, and the request is written on the ticket (SYRD-548)"
        ),
    )
    report_intake = subparsers.add_parser(
        "set-report-intake",
        help="Director: where tenant reports that ask for Backlog land -- backlog, or triage (the default) (SYRD-548)",
    )
    report_intake.add_argument("--backlog-requests", required=True, choices=("backlog", "triage"))
    add_free_text_argument(report_intake, "--reason", required=True, help="why this board takes reports this way")

    route = subparsers.add_parser("route")
    route.add_argument("ticket_id")
    route.add_argument("--state", required=True)
    route.add_argument("--assignee", required=True)

    reassign = subparsers.add_parser("reassign")
    reassign.add_argument("ticket_id")
    reassign.add_argument("--assignee", required=True)
    add_free_text_argument(reassign, "--reason", required=True)

    request_publication = subparsers.add_parser("request-publication")
    request_publication.add_argument("ticket_id")
    request_publication.add_argument("--ref", required=True)
    request_publication.add_argument("--commit", required=True)
    request_publication.add_argument("--bundle", required=True)

    resolve_publication = subparsers.add_parser("resolve-publication")
    resolve_publication.add_argument("ticket_id")
    resolve_publication.add_argument("--request-id", required=True, type=int)
    resolve_publication.add_argument("--outcome", required=True, choices=("published", "rejected"))
    add_free_text_argument(resolve_publication, "--reason", default="")

    director_edit = subparsers.add_parser("director-edit")
    director_edit.add_argument("ticket_id")
    director_edit.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE")
    add_free_text_argument(director_edit, "--reason", required=True)

    force_move = subparsers.add_parser("force-move")
    force_move.add_argument("ticket_id")
    force_move.add_argument("--state", required=True)
    force_move.add_argument("--assignee", required=True)
    force_move.add_argument("--suppress-notification", action="store_true")

    override_move = subparsers.add_parser("override-move")
    override_move.add_argument("ticket_id")
    override_move.add_argument("--state", required=True)
    override_move.add_argument("--assignee", required=True)
    override_move.add_argument("--no-notify", action="store_true")

    for name in ("start-work", "inspector-sign-off", "user-sign-off", "release-draft", "defer"):
        sub = subparsers.add_parser(name)
        sub.add_argument("ticket_id")

    start_task = subparsers.add_parser("start-task")
    start_task.add_argument("ticket_id")
    add_free_text_argument(start_task, "--text", default="")

    complete_task = subparsers.add_parser("complete-task")
    complete_task.add_argument("ticket_id")
    add_free_text_argument(complete_task, "--text", required=True)

    audit_sign = subparsers.add_parser("audit-sign-off")
    audit_sign.add_argument("ticket_id")
    add_free_text_argument(audit_sign, "--text", required=True)

    submit = subparsers.add_parser("submit-to-audit")
    submit.add_argument("ticket_id")
    submit.add_argument("--commit-hash", default="")

    submit_no_commit = subparsers.add_parser("submit-to-audit-without-commit")
    submit_no_commit.add_argument("ticket_id")
    add_free_text_argument(submit_no_commit, "--reason", required=True)

    submit_inspection = subparsers.add_parser("submit-to-inspection")
    submit_inspection.add_argument("ticket_id")
    submit_inspection.add_argument("--commit-hash", default="")

    implementer_kick = subparsers.add_parser("implementer-kick-back")
    implementer_kick.add_argument("ticket_id")
    add_free_text_argument(implementer_kick, "--reason", required=True)

    request_exempt = subparsers.add_parser("request-commit-exempt")
    request_exempt.add_argument("ticket_id")
    add_free_text_argument(request_exempt, "--reason", required=True)

    recover_stalled = subparsers.add_parser(
        "recover-stalled-ticket",
        help="take the transition a stalled ticket's owner did not take, to its next required gate",
    )
    recover_stalled.add_argument("ticket_id")
    add_free_text_argument(recover_stalled, "--reason", required=True, help="why the recovery is being made")
    request_dependency = subparsers.add_parser(
        "request-dependency",
        help="record why this work is waiting and hand it to the role it waits on, in one action",
    )
    request_dependency.add_argument("ticket_id")
    request_dependency.add_argument("--role", required=True, help="the role this work waits on")
    add_free_text_argument(request_dependency, "--reason", required=True, help="what they have to do, in their words")
    snooze = subparsers.add_parser(
        "snooze-reminders",
        help="defer the named tickets' optional reminders until a deadline; previews unless --apply (SYRD-537)",
    )
    snooze.add_argument("--ticket", action="append", required=True, dest="tickets", help="a ticket to snooze; repeat")
    snooze.add_argument("--until", required=True, help="deadline, ISO 8601 with an offset, e.g. 2026-10-03T07:00:00-04:00")
    add_free_text_argument(snooze, "--reason", required=True, help="why these reminders can wait")
    snooze.add_argument("--apply", action="store_true", help="create the snooze; without it nothing is written")
    subparsers.add_parser(
        "claim-next",
        help="under a pull policy, claim your own next ready ticket; held work is returned instead (SYRD-539)",
    )
    clear_snooze = subparsers.add_parser(
        "clear-reminder-snooze",
        help="end a reminder snooze early, for named tickets or the whole batch; previews unless --apply",
    )
    clear_snooze.add_argument("--batch", required=True, type=int)
    clear_snooze.add_argument("--ticket", action="append", default=[], dest="tickets", help="only this ticket; repeat")
    add_free_text_argument(clear_snooze, "--reason", required=True, help="why ordinary reminders resume now")
    clear_snooze.add_argument("--apply", action="store_true", help="clear it; without it nothing is written")
    size_exception = subparsers.add_parser(
        "approve-size-exception",
        help="Director: approve one open size finding at its measured size; previews unless --apply (SYRD-541)",
    )
    size_exception.add_argument("ticket_id")
    size_exception.add_argument("--path", required=True, help="the file the finding is about")
    add_free_text_argument(size_exception, "--rationale", required=True, help="why this growth is justified")
    size_exception.add_argument("--standing", action="store_true",
                                help="the file's allowance on later tickets too, not this ticket only")
    size_exception.add_argument("--apply", action="store_true", help="record it; without it nothing is written")
    measure_size = subparsers.add_parser(
        "measure-size",
        help="Director: measure an integration commit for a ticket before pushing main; moves nothing (SYRD-541)",
    )
    measure_size.add_argument("ticket_id")
    measure_size.add_argument("--commit", required=True, help="the integration commit, published where the board sees it")
    enable_size = subparsers.add_parser(
        "enable-size-review",
        help="Director: turn the size review on, recording the baseline inventory; previews unless --apply",
    )
    enable_size.add_argument("--baseline", default="", help="baseline commit (default: the board's main)")
    enable_size.add_argument("--carried-file", default="",
                             help="JSON list of reviewed exceptions to carry forward (path, ceiling, "
                                  "reviewed_commit, reviewed_base, rationale, approved_by)")
    enable_size.add_argument("--apply", action="store_true", help="enable it; without it nothing is written")
    release_external = subparsers.add_parser(
        "release-external-blocker",
        help="end a wait on another board's work, explicitly and with why (SYRD-270)",
    )
    release_external.add_argument("ticket_id")
    release_external.add_argument("--ref", required=True, help="the external blocker, as project:PREFIX-N")
    add_free_text_argument(release_external, "--reason", required=True, help="what happened that ends the wait")
    release_external.add_argument(
        "--commit",
        default="",
        help="a commit this board must itself recognise before the wait may end",
    )
    await_role = subparsers.add_parser("await-role")
    await_role.add_argument("ticket_id")
    await_role.add_argument("--role", required=True)

    clear_awaiting = subparsers.add_parser("clear-awaiting-role")
    clear_awaiting.add_argument("ticket_id")

    audit_kick = subparsers.add_parser("audit-kick-back")
    audit_kick.add_argument("ticket_id")
    add_free_text_argument(audit_kick, "--reason", required=True)
    audit_kick.add_argument(
        "--target-assignee",
        default="",
        help="optional confirmation: under a declared workflow a kick-back returns the ticket to its recorded implementer, so this must name that implementer; to hand it to someone else, the Director reassigns after the kick-back",
    )

    dat_sign = subparsers.add_parser("director-dat-sign-off")
    dat_sign.add_argument("ticket_id")
    add_free_text_argument(dat_sign, "--text", default="")

    dat_kick = subparsers.add_parser("director-dat-kick-back")
    dat_kick.add_argument("ticket_id")
    add_free_text_argument(dat_kick, "--reason", required=True)
    dat_kick.add_argument(
        "--target-assignee",
        default="",
        help="optional confirmation: under a declared workflow a kick-back returns the ticket to its recorded implementer, so this must name that implementer; to hand it to someone else, the Director reassigns after the kick-back",
    )

    for name in ("user-reopen", "cancel"):
        sub = subparsers.add_parser(name)
        sub.add_argument("ticket_id")
        add_free_text_argument(sub, "--reason", required=True)

    inspector_kick = subparsers.add_parser("inspector-kick-back")
    inspector_kick.add_argument("ticket_id")
    add_free_text_argument(inspector_kick, "--recommendations", required=True)
    inspector_kick.add_argument(
        "--target-assignee",
        default="",
        help="optional confirmation: under a declared workflow a kick-back returns the ticket to its recorded implementer, so this must name that implementer; to hand it to someone else, the Director reassigns after the kick-back",
    )

    done = subparsers.add_parser("mark-done")
    done.add_argument("ticket_id")
    done.add_argument("--commit-hash", default="")

    manual = subparsers.add_parser("set-manually-controlled")
    manual.add_argument("ticket_id")
    manual.add_argument("--value", action=argparse.BooleanOptionalAction, default=True)

    blockers = subparsers.add_parser("set-blockers")
    blockers.add_argument("ticket_id")
    blockers.add_argument(
        "--blocked-by",
        action="append",
        required=True,
        help=(
            "a ticket on this board (PREFIX-N), or work on another board as project:PREFIX-N; "
            "an external blocker never resolves by itself -- see release-external-blocker"
        ),
    )
    add_free_text_argument(blockers, "--blocked-reason", required=True)

    comment = subparsers.add_parser("add-comment")
    comment.add_argument("ticket_id")
    add_free_text_argument(comment, "--text", required=True)
    comment.add_argument("--urgent", action="store_true")

    edit_fields = subparsers.add_parser("edit-fields")
    edit_fields.add_argument("ticket_id")
    edit_fields.add_argument("--json", required=True, help="JSON object of fields accepted by the edit_fields action")

    merge = subparsers.add_parser("merge")
    merge.add_argument("ticket_id")
    merge.add_argument("--target-id", required=True)

    dismiss_notification = subparsers.add_parser("dismiss-notification")
    dismiss_notification.add_argument("notification_id", type=int, nargs="?")
    dismiss_notification.add_argument("--ticket-id", default="")
    dismiss_notification.add_argument("--target-role", default="")
    dismiss_notification.add_argument("--kind", default="transition")
    add_free_text_argument(dismiss_notification, "--reason", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        parser = _build_parser()
        args = parser.parse_args(argv)
        resolve_free_text_arguments(args, parser)
        command = args.command.replace("-", "_")
        if command != "file_report" and not args.caller_role.strip():
            raise write_client.TicketBoardWriteError(
                "ticket board caller role required; pass --caller-role or set TICKET_BOARD_CALLER_ROLE"
            )
        resolved_url, resolved_socket = write_client.resolve_endpoint(args.board_url, args.socket_path)
        args.board_url, args.socket_path = resolved_url, resolved_socket
        # The CLI knows what was SUPPLIED; the client cannot. Saying "no socket"
        # out loud is what stops the library rediscovering one (SYRD-198).
        socket_disabled = resolved_socket is None
        client = write_client.TicketBoardWriteClient(
            args.board_url,
            args.caller_role,
            socket_path=args.socket_path,
            socket_disabled=socket_disabled,
            report_token=args.report_token,
            write_token=args.write_token,
            report_board_url=args.report_board_url,
            report_token_file=args.report_token_file,
        )
        if command == "verify_caller":
            response = client.verify_caller()
        elif command == "create_ticket":
            response = client.create_ticket(
                title=args.title,
                body=args.body,
                screenshot=args.screenshot,
                assignee=args.assignee,
                state="draft" if args.draft else args.state,
                parent_id=args.parent_id,
                blocked_by=args.blocked_by,
                blocked_reason=args.blocked_reason,
                implementation=args.implementation,
                audit_prompt=args.audit_prompt,
                needs_inspection=args.needs_inspection,
                needs_audit=args.needs_audit,
                needs_user_signoff=args.needs_user_signoff,
                comment_text=args.comment_text,
            )
        elif command == "file_bug":
            response = client.file_bug(
                title=args.title,
                body=args.body,
                source_ticket_id=args.source_ticket_id,
                assignee=args.assignee,
                needs_audit=args.needs_audit,
            )
        elif command == "file_report":
            if not (args.report_token.strip() or args.report_token_file.strip()):
                raise write_client.TicketBoardWriteError(not_connected_for_reports(os.environ))
            if not args.origin_project.strip():
                raise write_client.TicketBoardWriteError("file-report requires --origin-project or TICKET_BOARD_REPORT_ORIGIN_PROJECT")
            response = client.file_report(
                title=args.title,
                body=args.body,
                origin_project=args.origin_project,
                external_source_ref=args.external_source_ref,
                requested_stage="backlog" if args.defer else "",
            )
            if args.defer and not _records_report_request(response):
                print(
                    "file-report: the report was filed, but this board did not record the Backlog request; "
                    "it predates SYRD-548. It is in Triage like any report.",
                    file=sys.stderr,
                )
        elif command == "set_report_intake":
            response = client.set_report_intake(backlog_requests=args.backlog_requests, reason=args.reason)
        elif command == "route":
            response = client.route(args.ticket_id, state=args.state, assignee=args.assignee)
        elif command == "reassign":
            response = client.reassign(args.ticket_id, assignee=args.assignee, reason=args.reason)
        elif command == "director_edit":
            patch: dict[str, Any] = {}
            for assignment in args.set:
                if "=" not in assignment:
                    raise SystemExit(f"--set expects FIELD=VALUE, got {assignment!r}")
                key, _, raw = assignment.partition("=")
                key = key.strip()
                # Typed the way the field is: a gate is a boolean and a title is
                # not, and the database refuses a value of the wrong shape
                # rather than coercing it.
                if raw in ("true", "false"):
                    patch[key] = raw == "true"
                else:
                    patch[key] = raw
            response = client.director_edit(
                args.ticket_id, patch=patch, reason=args.reason
            )
        elif command == "request_publication":
            response = client.request_publication(
                args.ticket_id, ref=args.ref, commit=args.commit, bundle=args.bundle
            )
        elif command == "resolve_publication":
            response = client.resolve_publication(
                args.ticket_id,
                request_id=args.request_id,
                outcome=args.outcome,
                detail=args.reason,
            )
        elif command == "force_move":
            response = client.force_move(
                args.ticket_id,
                state=args.state,
                assignee=args.assignee,
                suppress_notification=args.suppress_notification,
            )
        elif command == "override_move":
            response = client.override_move(
                args.ticket_id,
                state=args.state,
                assignee=args.assignee,
                notify=not args.no_notify,
            )
        elif command == "workflow_action":
            response = client.workflow_action(args.ticket_id, args.action, json.loads(args.payload_json))
        elif command == "add_user_acceptance_relay":
            if args.apply and args.expected_revision is None:
                raise write_client.TicketBoardWriteError("--apply needs --expected-revision, from the preview")
            response = _add_user_acceptance_relay(client, apply=args.apply, expected_revision=args.expected_revision)
        elif command == "set_workflow_flags":
            response = client.set_workflow_flags(args.ticket_id, json.loads(args.patch_json))
        elif command == "submit_to_audit":
            response = client.submit_to_audit(args.ticket_id, commit_hash=args.commit_hash)
        elif command == "submit_to_audit_without_commit":
            response = client.submit_to_audit_without_commit(args.ticket_id, reason=args.reason)
        elif command == "submit_to_inspection":
            response = client.submit_to_inspection(args.ticket_id, commit_hash=args.commit_hash)
        elif command == "implementer_kick_back":
            response = client.implementer_kick_back(args.ticket_id, reason=args.reason)
        elif command == "request_commit_exempt":
            response = client.request_commit_exempt(args.ticket_id, reason=args.reason)
        elif command == "start_task":
            response = client.start_task(args.ticket_id, text=args.text)
        elif command == "complete_task":
            response = client.complete_task(args.ticket_id, text=args.text)
        elif command == "recover_stalled_ticket":
            response = client.recover_stalled_ticket(args.ticket_id, reason=args.reason)
        elif command == "release_external_blocker":
            response = client.release_external_blocker(
                args.ticket_id, ref=args.ref, reason=args.reason, commit=args.commit
            )
        elif command == "claim_next":
            response = client.claim_next()
        elif command == "snooze_reminders":
            response = client.snooze_reminders(args.tickets, until=args.until, reason=args.reason, apply=args.apply)
        elif command == "clear_reminder_snooze":
            response = client.clear_reminder_snooze(
                args.batch, tickets=args.tickets, reason=args.reason, apply=args.apply
            )
        elif command == "approve_size_exception":
            response = client.approve_size_exception(
                args.ticket_id, path=args.path, rationale=args.rationale, standing=args.standing, apply=args.apply
            )
        elif command == "measure_size":
            response = client.measure_size(args.ticket_id, commit=args.commit)
        elif command == "enable_size_review":
            carried = json.loads(Path(args.carried_file).read_text(encoding="utf-8")) if args.carried_file else []
            response = client.enable_size_review(baseline=args.baseline, carried=carried, apply=args.apply)
        elif command == "request_dependency":
            response = client.request_dependency(
                args.ticket_id, role=args.role, reason=args.reason
            )
        elif command == "await_role":
            response = client.await_role(args.ticket_id, role=args.role)
        elif command == "clear_awaiting_role":
            response = client.clear_awaiting_role(args.ticket_id)
        elif command == "audit_sign_off":
            response = client.audit_sign_off(args.ticket_id, text=args.text)
        elif command == "audit_kick_back":
            response = client.audit_kick_back(args.ticket_id, reason=args.reason, target_assignee=args.target_assignee)
        elif command == "director_dat_sign_off":
            response = client.director_dat_sign_off(args.ticket_id, text=args.text)
        elif command == "director_dat_kick_back":
            response = client.director_dat_kick_back(args.ticket_id, reason=args.reason, target_assignee=args.target_assignee)
        elif command in {"user_reopen", "cancel"}:
            response = getattr(client, command)(args.ticket_id, reason=args.reason)
        elif command == "inspector_kick_back":
            response = client.inspector_kick_back(
                args.ticket_id,
                recommendations=args.recommendations,
                target_assignee=args.target_assignee,
            )
        elif command == "mark_done":
            response = client.mark_done(args.ticket_id, commit_hash=args.commit_hash)
        elif command == "set_manually_controlled":
            response = client.set_manually_controlled(args.ticket_id, args.value)
        elif command == "set_blockers":
            response = client.set_blockers(args.ticket_id, blocked_by=args.blocked_by, blocked_reason=args.blocked_reason)
        elif command == "add_comment":
            response = client.add_comment(args.ticket_id, text=args.text, urgent=args.urgent)
        elif command == "edit_fields":
            patch = json.loads(args.json)
            if not isinstance(patch, dict):
                raise write_client.TicketBoardWriteError("edit-fields --json must be a JSON object")
            response = client.edit_fields(args.ticket_id, patch)
        elif command == "merge":
            response = client.merge(args.ticket_id, target_id=args.target_id)
        elif command == "dismiss_notification":
            response = client.dismiss_notification(
                args.notification_id,
                ticket_id=args.ticket_id,
                target_role=args.target_role,
                kind=args.kind,
                reason=args.reason,
            )
        else:
            response = getattr(client, command)(args.ticket_id)
    except json.JSONDecodeError as exc:
        print(f"invalid edit-fields --json: {exc}", file=sys.stderr)
        return 1
    except write_client.TicketBoardWriteError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if command in {"merge", "dismiss_notification", "verify_caller", "add_user_acceptance_relay",
                   "snooze_reminders", "clear_reminder_snooze", "claim_next", "approve_size_exception",
                   "enable_size_review", "measure_size", "set_report_intake"}:
        print(json.dumps(response))
    else:
        print(json.dumps(_ticket_from_response(response)))
    return 0
