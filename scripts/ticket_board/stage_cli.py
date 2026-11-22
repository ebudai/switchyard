"""ticket-board-write's commands for the opt-in stages: the documentation gate's decisions (SYRD-624), the research
stage's brief and moves (SYRD-842), and the plan gate's deliverables, plan tickets and scope changes (SYRD-843).

Each research move is the declared transition of the same name, so the board
checks the actor, the owner, the stage and the reason, and research_finding
checks a findings commit, before anything changes. This module only names them
and builds their payloads; `write_cli` registers the parsers and dispatches.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable

try:
    from . import documentation_gate, read_client, research_stage, write_client
except ImportError:  # pragma: no cover - `python write_client.py` runs outside the package
    import documentation_gate  # type: ignore[no-redef]
    import read_client  # type: ignore[no-redef]
    import research_stage  # type: ignore[no-redef]
    import write_client  # type: ignore[no-redef]

#: command (dashes as underscores) -> the declared transition it takes, for the research moves.
RESEARCH_MOVES = {
    "start_research": research_stage.START,
    "submit_finding": research_stage.SUBMIT,
    "submit_finding_without_commit": research_stage.SUBMIT_NO_CODE,
    "research_hand_back": research_stage.HAND_BACK,
    "research_sign_off": research_stage.SIGN_OFF,
    "research_kick_back": research_stage.KICK_BACK,
    "return_finding": research_stage.RETURN,
    "close_on_finding": research_stage.CLOSE,
}
#: research-brief, and set-research-brief, its name as the board's operation.
RESEARCH_COMMANDS = frozenset({"research_brief", "set_research_brief", *RESEARCH_MOVES})
#: The plan gate's commands; set-ticket-deliverable is set-deliverable under the board's operation name.
PLAN_COMMANDS = frozenset({"set_deliverable", "set_ticket_deliverable", "request_scope_change", "plan_ticket"})


def documentation_decision(client: Any, args: argparse.Namespace, command: str) -> dict[str, Any]:
    """SYRD-624: one documentation decision, as the declared transition it is."""
    action = getattr(documentation_gate, {"documentation_sign_off": "SIGN_OFF", "documentation_no_change": "NO_CHANGE",
                                          "documentation_kick_back": "KICK_BACK"}[command])
    if command != "documentation_sign_off":
        return client.workflow_action(args.ticket_id, action, {"reason": args.reason})
    ref = (args.ref or "").strip()
    if not ref:  # the ticket's documentation ref for this role, derived exactly as the board derives it
        ticket = read_client.TicketBoardReadClient(args.board_url).get_ticket(args.ticket_id)
        ref = documentation_gate.candidate_ref(args.caller_role, args.ticket_id, str(ticket.get("commit_hash") or ""))
    payload = {"commit_hash": args.commit_hash, "ref": ref}
    if args.note:
        payload["text"] = args.note
    return client.workflow_action(args.ticket_id, action, payload)


def _brief_arguments(parser: argparse.ArgumentParser, free_text: Callable[..., Any], *, required: bool) -> None:
    free_text(parser, "--question", required=required, default=None,
              help="the one question the research answers")
    parser.add_argument("--timebox", required=required, default=None,
                        help="how long the research may take before you are told, e.g. 90m, 4h, 2d (15m to 30d)")
    free_text(parser, "--usable-answer", required=required, default=None,
              help="what a usable answer looks like: the form and detail that would let you decide")


def add_research_parsers(subparsers: Any, free_text: Callable[..., Any]) -> None:
    """The research brief and the research stage's moves (SYRD-842)."""
    brief = subparsers.add_parser(
        "research-brief", aliases=["set-research-brief"], help="Director: write a ticket's research brief -- question, timebox, usable answer -- "
                               "which makes it a research ticket (its needs_research gate goes on)")
    brief.add_argument("ticket_id")
    _brief_arguments(brief, free_text, required=True)
    start = subparsers.add_parser("start-research", help="Director: move a research ticket from triage to Research")
    start.add_argument("ticket_id")
    submit = subparsers.add_parser(
        "submit-finding", help="Researcher: submit a published findings commit -- one file, the ticket's own under "
                               "the research directory")
    submit.add_argument("ticket_id")
    submit.add_argument("--commit", dest="commit_hash", required=True, help="the findings commit")
    free_text(submit, "--note", default="", help="an optional note kept after the board's record")
    for name, who, what, helped in (
            ("submit-finding-without-commit", "Researcher", "submit a finding with nothing to keep",
             "the finding: question, evidence, answer, confidence, recommendation"),
            ("research-hand-back", "Researcher", "hand the ticket back: the question is not researchable as written",
             "why, and what would make it researchable"),
            ("research-kick-back", "Audit", "return a finding to the researcher: its evidence is missing or does "
             "not back its claims", "what is missing"),
            ("return-finding", "Director", "return a finding in triage to the researcher", "what is missing"),
            ("close-on-finding", "Director", "close a research ticket as done on its finding", "the decision")):
        move = subparsers.add_parser(name, help=f"{who}: {what}")
        move.add_argument("ticket_id")
        free_text(move, "--reason", required=True, help=helped)
    sign = subparsers.add_parser("research-sign-off",
                                 help="Audit: the finding's claims are backed by what was run or cited")
    sign.add_argument("ticket_id")


def add_create_arguments(create: argparse.ArgumentParser, free_text: Callable[..., Any]) -> None:
    """create-ticket --research with its brief, and the plan gate's --deliverable, --unplanned-reason and --plan
    (Director only: the board refuses anyone else)."""
    create.add_argument("--research", action="store_true",
                        help="a research ticket: needs --question, --timebox and --usable-answer, its brief")
    _brief_arguments(create, free_text, required=False)
    create.add_argument("--deliverable", default=None,
                        help="plan-gated boards: the deliverable of the approved plan this ticket serves")
    free_text(create, "--unplanned-reason", default=None,
              help="plan-gated boards: why this ticket serves no deliverable (a bug, an ops one-off, a report)")
    create.add_argument("--plan", action="store_true",
                        help="plan-gated boards: a plan ticket, the only way the plan changes; it needs the User's "
                             "acceptance before it is closed")


def add_plan_parsers(subparsers: Any, free_text: Callable[..., Any]) -> None:
    """The plan gate's commands (SYRD-843)."""
    deliverable = subparsers.add_parser(
        "set-deliverable", aliases=["set-ticket-deliverable"],
        help="Director: name the deliverable of the approved plan a ticket serves, or that it is unplanned and why")
    deliverable.add_argument("ticket_id")
    choice = deliverable.add_mutually_exclusive_group(required=True)
    choice.add_argument("--deliverable", help="the deliverable's id in the approved plan, e.g. D1.2")
    choice.add_argument("--unplanned-reason", help="why the ticket serves no deliverable")
    choice.add_argument("--unplanned-reason-file", help="read the unplanned reason from PATH (- for standard input)")
    scope = subparsers.add_parser(
        "request-scope-change", help="Director: the plan does not cover this ticket -- open (or extend) a plan ticket "
                                     "for the plan owner, and make this ticket wait on it")
    scope.add_argument("ticket_id")
    free_text(scope, "--reason", required=True, help="what the plan is missing")
    planned = subparsers.add_parser("plan-ticket", help="Director: mark a ticket as a plan ticket (or not, with --off)")
    planned.add_argument("ticket_id")
    planned.add_argument("--off", action="store_true", help="it is not a plan ticket after all")


def _unplanned(args: argparse.Namespace) -> str:
    path = getattr(args, "unplanned_reason_file", None)
    if path:
        import sys
        from pathlib import Path

        return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return str(getattr(args, "unplanned_reason", None) or "")


def _refused(message: str) -> Exception:
    return write_client.TicketBoardWriteError(message)


def check_create(args: argparse.Namespace) -> None:
    """create-ticket's research brief, checked before anything is created: whole with --research, absent without; and
    at most one of --deliverable and --unplanned-reason, neither on a plan ticket."""
    if args.deliverable and str(args.unplanned_reason or "").strip():
        raise _refused("a ticket serves a deliverable, or is unplanned with a reason: give one of the two")
    if args.plan and (args.deliverable or str(args.unplanned_reason or "").strip()):
        raise _refused("a plan ticket changes the plan itself, so it serves no deliverable and is not unplanned")
    given = [flag for flag, value in (("--question", args.question), ("--timebox", args.timebox),
                                      ("--usable-answer", args.usable_answer)) if value not in (None, "")]
    if not args.research:
        if given:
            raise _refused(f"{', '.join(given)} belong to a research ticket; add --research")
        return
    missing = [flag for flag in ("--question", "--timebox", "--usable-answer") if flag not in given]
    if missing:
        raise _refused(f"a research ticket states its question, a timebox and what a usable answer looks like: "
                       f"give {', '.join(missing)}")
    try:
        research_stage.timebox_seconds(args.timebox)
    except ValueError as exc:
        raise _refused(str(exc)) from None


def _brief(client: Any, ticket_id: str, args: argparse.Namespace) -> dict[str, Any]:
    return client.set_research_brief(ticket_id, question=args.question,
                                     timebox=research_stage.timebox_seconds(args.timebox), usable_answer=args.usable_answer)


def after_create(client: Any, args: argparse.Namespace, response: dict[str, Any]) -> dict[str, Any]:
    """With --research, the new ticket's brief, which turns its needs_research gate on; with --deliverable or
    --unplanned-reason, its place in the plan; with --plan, its mark as a plan ticket."""
    ticket = response.get("ticket") if isinstance(response.get("ticket"), dict) else response
    ticket_id = str(ticket.get("id") or "")
    steps = []
    if args.research:
        steps.append(("its research brief", f"research-brief {ticket_id}", lambda: _brief(client, ticket_id, args)))
    if args.deliverable or str(args.unplanned_reason or "").strip():
        steps.append(("its deliverable", f"set-deliverable {ticket_id}", lambda: client.set_ticket_deliverable(
            ticket_id, deliverable=args.deliverable or "", unplanned_reason=str(args.unplanned_reason or ""))))
    if args.plan:
        steps.append(("its plan ticket mark", f"plan-ticket {ticket_id}", lambda: client.set_workflow_flags(
            ticket_id, {"plan_change": True, "needs_user_signoff": True, "needs_audit": True})))
    for what, command, step in steps:
        try:
            step()
        except write_client.TicketBoardWriteError as exc:
            raise _refused(f"{ticket_id} was created, but {what} was refused: {exc}. Set it with "
                           f"ticket-board-write {command}") from None
    return response


def plan_command(client: Any, args: argparse.Namespace, command: str) -> dict[str, Any]:
    """One plan gate command."""
    if command in ("set_deliverable", "set_ticket_deliverable"):
        client.set_ticket_deliverable(args.ticket_id, deliverable=args.deliverable or "",
                                      unplanned_reason=_unplanned(args))
    elif command == "request_scope_change":
        client.request_scope_change(args.ticket_id, reason=args.reason)
    else:
        client.set_workflow_flags(args.ticket_id, {"plan_change": not args.off, **({} if args.off else
                                                                                   {"needs_user_signoff": True,
                                                                                    "needs_audit": True})})
    return {"ticket": read_client.TicketBoardReadClient(args.board_url).get_ticket(args.ticket_id)}


def research_command(client: Any, args: argparse.Namespace, command: str) -> dict[str, Any]:
    """One research command: the brief, or the declared move of the same name."""
    if command in ("research_brief", "set_research_brief"):
        try:
            research_stage.timebox_seconds(args.timebox)
        except ValueError as exc:
            raise _refused(str(exc)) from None
        _brief(client, args.ticket_id, args)
        return {"ticket": read_client.TicketBoardReadClient(args.board_url).get_ticket(args.ticket_id)}
    payload: dict[str, Any] = {}
    if command == "submit_finding":
        payload["commit_hash"] = args.commit_hash
        if args.note:
            payload["text"] = args.note
    elif getattr(args, "reason", None) is not None:
        payload["reason"] = args.reason
    return client.workflow_action(args.ticket_id, RESEARCH_MOVES[command], payload)
