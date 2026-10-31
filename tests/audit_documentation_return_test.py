#!/usr/bin/env python3
"""SYRD-719: Audit's kick-back tells the work's substantive origin from the Librarian's documentation layer.

SYRD-673 recorded one producer per revision. The Librarian's documentation
commit became the producer, so every Audit finding on that combined candidate --
code, tests, configuration, security -- went back to the Librarian. The board
now records the two layers apart, and Audit chooses: `audit_kick_back` returns
to the substantive origin, `audit_documentation_kick_back` returns a
documentation-only finding to the Documentation stage and owner the ticket
passed.

The cases run on boards built the production way (companion roles, schema.sql,
the real migrate runner, rbac.sql) with the shipped inspection workflow, an
active designer owning Draft and the Librarian's documentation gate. Writes go
through the board's HTTP API as that role, the real `ticket-board-write`, or, for
the Librarian's commit (whose git checks are SYRD-624's), the board service's
own connection. The real notify listener delivers.

- Design, Implementation, and Implementation through Documentation with no
  change: the ordinary kick-back returns to the designer or implementer.
- The Librarian's commit: the ordinary kick-back returns to the implementer,
  owing Documentation again, and the resubmission passes the gate again; the
  documentation return goes to the Librarian on the audited candidate, round
  after round, keeping the implementer as the origin under it.
- Refusals that move nothing: no documentation stage, the gate off, a
  documentation pass on another revision, a spoofed target or stage, a
  Librarian no longer active, the gate removed while the ticket is in Audit.
- An upgrade from main: the Librarian's commit recorded as the producer becomes
  the documentation layer, its origin unrecorded and the Director told once;
  the workflow gains the return exactly as composed; schema.sql, the migration
  and the upgrade install the same objects.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu1022_syrd719_audit_return_layers.sql"
CHECKS = 0
A, B, C, D = "a" * 40, "b" * 40, "c" * 40, "d" * 40
DOCS = "audit_documentation_kick_back"


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def before_root(into: Path) -> Path:
    """Main as it stood before this ticket: the commit before the one that added pgu1022, or HEAD before it is."""
    def git(*args: str) -> bytes:
        return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True).stdout
    adding = git("log", "--format=%H", "--diff-filter=A", "--", str(MIGRATION.relative_to(ROOT))).decode().split()
    with tarfile.open(fileobj=io.BytesIO(git("archive", f"{adding[-1]}^" if adding else "HEAD", "scripts"))) as tar:
        tar.extractall(into, filter="data")
    check(not (into / "scripts/ticket_board/migrations" / MIGRATION.name).exists(), "the before tree predates pgu1022")
    return into


def main() -> int:
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    for extra in (str(ROOT / "tests"), str(ROOT)):
        sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    from audit_return_test import design_loop, example
    from router_role_board_test import Board, build_board, fixture, migrate, temporary_cluster
    from scripts.ticket_board import read_client
    from scripts.ticket_board.documentation_gate import with_documentation_gate, without_documentation_gate

    def short(answer: dict) -> str:
        if "ok" in answer:
            return "ok"
        text = answer.get("refused", "")
        try:
            text = str(ast.literal_eval(text)[2])
        except (ValueError, SyntaxError, IndexError, TypeError):
            pass
        return " ".join(text.split("\nCONTEXT:")[0].split())[:900]

    with tempfile.TemporaryDirectory(prefix="syrd719.") as tmp, \
            temporary_cluster(prefix="syrd719-", shutdown="immediate") as cluster:
        boards: list = []

        def must(what: str, step):
            """A step every case needs: its refusal is a failed check, never a crash."""
            try:
                return step()
            except Exception as exc:  # noqa: BLE001
                check(False, f"{what}: {type(exc).__name__}: {str(exc)[:600]}")

        def board(name: str, document: dict):
            b = must(f"the {name} board takes its workflow", lambda: Board(cluster, name, document))
            b.app._resolve_known_commit = lambda value: (value.lower(), [])  # this board's copy knows every commit
            boards.append(b)
            return b

        def cli(b, role: str, *args: str) -> str:
            env = {"PATH": "/usr/bin:/bin", "HOME": str(cluster.root), "LANG": "C.UTF-8", "TICKET_BOARD_URL": b.base,
                   "TICKET_BOARD_WRITE_TOKEN": b.server.write_token, "TICKET_BOARD_CALLER_ROLE": role}
            done = subprocess.run([sys.executable, str(ROOT / "scripts" / "ticket-board-write"), *args], env=env,
                                  cwd=str(cluster.root), capture_output=True, text=True, timeout=120, check=False)
            return "ok" if done.returncode == 0 else " ".join((done.stdout + done.stderr).split("CONTEXT:")[0].split())[-900:]

        def state(b, ticket: str) -> dict:
            return json.loads(b.sql(
                "SELECT json_build_object('where', state || '/' || assignee, 'commit', commit_hash, "
                "'audit', audit_signoff, 'user', user_signoff, 'inspector', inspector_signoff, "
                "'documentation', coalesce((workflow_flags->>'documentation_signoff')::boolean, false), "
                "'gate', coalesce((workflow_flags->>'needs_documentation')::boolean, false), "
                "'comments', (SELECT count(*) FROM ticket_board.ticket_comments c WHERE c.ticket_id = t.id), "
                "'queued', (SELECT count(*) FROM ticket_board.ticket_notification_queue q WHERE q.ticket_id = t.id))::text "
                f"FROM ticket_board.tickets t WHERE id = '{ticket}';"))

        def status(b, ticket: str) -> dict:
            return b.ticket(ticket).get("audit_return") or {}

        def offered(b, ticket: str) -> dict:
            return {a["action"]: {k: a.get(k) for k in ("to", "returns_to", "refused") if k in a}
                    for a in b.ticket(ticket).get("workflow_actions") or [] if a["action"] in ("audit_kick_back", DOCS)}

        def delivered(b) -> list:
            b.busy.clear()
            return sorted(target for target, message in b.one_pass() if message != "/clear")

        def librarian(b, ticket: str, action: str, **payload) -> str:
            """The Librarian's decision as the database takes it (SYRD-624's git checks pass only for a published ref)."""
            return short(b.as_role_sql("librarian", f"SELECT ticket_board.perform_workflow_action('{ticket}', '{action}', "
                                                    f"'{json.dumps(payload)}');"))

        def submitted(b, ticket: str, owner: str, commit: str, *, gate: bool = True) -> str:
            fixture.seed_postgres_ticket(b.admin, ticket, title=f"Case {ticket}", state="in_progress", assignee=owner)
            if gate:
                b.act(ticket, "set_workflow_flags", "director", needs_documentation=True)
            return short(b.act(ticket, "submit_to_audit", owner, commit_hash=commit))

        def finished(b, ticket: str) -> None:
            """The case is over: closed, so it holds no owner's slot and owes no review a later workflow drops."""
            answer = [short(b.act(ticket, "defer", "director")), short(b.act(ticket, "cancel", "director", reason="case done"))]
            answer = "ok" if answer == ["ok", "ok"] else answer
            check(answer == "ok", f"{ticket} closed to free its owner: {answer} {state(b, ticket)}")

        try:
            b = board("board", must("the design loop composes", lambda: design_loop(ROOT)))
            delivered(b)
            declared = [t for t in b.app.workflow_configuration()["transitions"] if t["action"] == DOCS]
            check(declared == [{"from": "audit", "to": "documentation", "action": DOCS,
                                "label": "Return documentation-only findings", "actors": ["audit"], "primitive": "return",
                                "owner_scoped": True, "require_commit": False, "require_reason": True,
                                "clear_signoffs": ["audit_signoff", "documentation_signoff", "user_signoff"],
                                "allow_no_code": False, "relays_decision_of": None}],
                  f"the gate declares Audit's documentation return, out of Audit, by Audit: {declared}")

            # --- Design to Audit: the ordinary kick-back is the designer's (SYRD-673), and there is no documentation layer.
            fixture.seed_postgres_ticket(b.admin, "PGU-1", title="Design", state="draft", assignee="designer")
            check(short(b.act("PGU-1", "submit_design", "designer")) == "ok", "the designer submits the design to Audit")
            delivered(b)
            got = status(b, "PGU-1")
            check(got.get("valid") is True and (got.get("stage"), got.get("assignee")) == ("draft", "designer")
                  and (got.get("documentation") or {}).get("valid") is False
                  and "no record that it passed through documentation" in got["documentation"]["problem"],
                  f"a design is the designer's, and never passed Documentation: {got}")
            before = state(b, "PGU-1")
            answer = short(b.act("PGU-1", DOCS, "audit", reason="The README is stale."))
            check(answer.startswith("PGU-1 cannot be returned to Documentation: the board has no record that it passed "
                                    "through documentation") and "Nothing was moved." in answer
                  and "goes back to its substantive origin with audit-kick-back" in answer and state(b, "PGU-1") == before,
                  f"so a documentation return is refused, moving nothing: {answer}")
            check(short(b.act("PGU-1", "audit_kick_back", "audit", reason="Fix B1.")) == "ok"
                  and state(b, "PGU-1")["where"] == "draft/designer" and delivered(b) == ["pgu-designer:0.0"],
                  f"the ordinary kick-back returns to Draft/designer, telling only them: {state(b, 'PGU-1')}")
            finished(b, "PGU-1")

            # --- Implementation to Audit, the gate off: the implementer; Documentation was never passed.
            check(submitted(b, "PGU-2", "app", A, gate=False) == "ok" and state(b, "PGU-2")["where"] == "audit/audit",
                  "with the gate off, a submission skips Documentation")
            delivered(b)
            before = state(b, "PGU-2")
            answer = cli(b, "audit", "audit-documentation-kick-back", "PGU-2", "--reason", "Docs.")
            check("PGU-2 cannot be returned to Documentation: the board has no record that it passed" in answer
                  and state(b, "PGU-2") == before and offered(b, "PGU-2")[DOCS].get("returns_to", "absent") is None,
                  f"a ticket that skipped Documentation has no documentation return: {answer}")
            check(cli(b, "audit", "audit-kick-back", "PGU-2", "--reason", "Fix it.") == "ok"
                  and state(b, "PGU-2")["where"] == "in_progress/app" and delivered(b) == ["pgu-app:0.0"]
                  and b.reservations().get("app") == "PGU-2",
                  f"the ordinary kick-back returns it to app, holding app's slot: {state(b, 'PGU-2')}")
            finished(b, "PGU-2")

            # --- Through Documentation with no change: the implementer; the documentation return is the Librarian's.
            check(submitted(b, "PGU-3", "ops", A) == "ok" and state(b, "PGU-3")["where"] == "documentation/librarian",
                  "the gate on, a submission enters Documentation")
            check(librarian(b, "PGU-3", "documentation_no_change", reason="Nothing to say.") == "ok",
                  "the Librarian finds nothing to change")
            delivered(b)
            got = replayed = status(b, "PGU-3")
            docs = got.get("documentation") or {}
            check(got.get("valid") is True and (got.get("stage"), got.get("assignee"), got.get("commit")) == ("in_progress", "ops", A)
                  and docs.get("valid") is True and (docs.get("stage"), docs.get("assignee"), docs.get("by")) ==
                  ("documentation", "librarian", "documentation_no_change") and docs.get("commit") == docs.get("base") == A,
                  f"no change: the origin is ops, and Documentation was passed with the same revision: {got}")
            check(offered(b, "PGU-3") == {"audit_kick_back": {"to": "in_progress", "returns_to": {"stage": "in_progress", "assignee": "ops"}},
                                          DOCS: {"to": "documentation", "returns_to": {"stage": "documentation", "assignee": "librarian"}}},
                  f"Audit is offered both, each naming where it goes: {offered(b, 'PGU-3')}")
            check(short(b.act("PGU-3", DOCS, "audit", reason="Mention the new flag in docs/operator.md.")) == "ok"
                  and state(b, "PGU-3")["where"] == "documentation/librarian" and state(b, "PGU-3")["commit"] == A
                  and delivered(b) == ["pgu-librarian:0.0"],
                  f"a documentation return gives the Librarian the audited candidate, and wakes only the Librarian: "
                  f"{state(b, 'PGU-3')}")
            check(librarian(b, "PGU-3", "documentation_no_change", reason="Already there.") == "ok", "and it passes again")
            delivered(b)
            check(short(b.act("PGU-3", "audit_kick_back", "audit", reason="Handle the empty file.")) == "ok"
                  and state(b, "PGU-3")["where"] == "in_progress/ops" and delivered(b) == ["pgu-ops:0.0"],
                  f"the ordinary kick-back still returns to ops: {state(b, 'PGU-3')}")
            # Resubmitted unchanged with the gate off, it skips Documentation: the earlier pass is not this round's.
            check(short(b.act("PGU-3", "set_workflow_flags", "director", needs_documentation=False)) == "ok"
                  and short(b.act("PGU-3", "submit_to_audit", "ops", commit_hash=A)) == "ok"
                  and state(b, "PGU-3")["where"] == "audit/audit", f"resubmitted past Documentation: {state(b, 'PGU-3')}")
            got = status(b, "PGU-3")
            check(got.get("valid") is True and (got.get("documentation") or {}).get("valid") is False
                  and short(b.act("PGU-3", DOCS, "audit", reason="Docs.")).startswith("PGU-3 cannot be returned to Documentation"),
                  f"a new submission ends the earlier documentation layer, even for the same commit: {got}")
            finished(b, "PGU-3")

            # --- The Librarian's commit, then a substantive (or mixed) finding: back to the implementer, not the Librarian.
            check(submitted(b, "PGU-4", "ops", A) == "ok" and librarian(b, "PGU-4", "documentation_sign_off", commit_hash=B) == "ok"
                  and state(b, "PGU-4")["where"] == "audit/audit" and state(b, "PGU-4")["commit"] == B,
                  f"the Librarian's commit is the candidate Audit reviews: {state(b, 'PGU-4')}")
            delivered(b)
            got = status(b, "PGU-4")
            docs = got.get("documentation") or {}
            check(got.get("valid") is True and (got.get("stage"), got.get("assignee"), got.get("action"), got.get("commit"))
                  == ("in_progress", "ops", "submit_to_audit", A)
                  and docs.get("valid") is True and (docs.get("stage"), docs.get("assignee"), docs.get("commit"), docs.get("base"))
                  == ("documentation", "librarian", B, A),
                  f"two layers: ops's work (A) is the origin, the Librarian's commit (B) the documentation layer: {got}")
            text = read_client.format_ticket(b.ticket("PGU-4"))
            check("Kick-back returns to:\n  in_progress/ops (by submit_to_audit)" in text
                  and "Documentation return (audit-documentation-kick-back) goes to:\n  documentation/librarian "
                      f"(by documentation_sign_off: commit {B} on {A})" in text,
                  f"the ticket's text names both destinations: {text}")
            check(offered(b, "PGU-4")["audit_kick_back"]["returns_to"] == {"stage": "in_progress", "assignee": "ops"}
                  and offered(b, "PGU-4")[DOCS]["returns_to"] == {"stage": "documentation", "assignee": "librarian"},
                  f"and so do its workflow actions: {offered(b, 'PGU-4')}")
            # Spoofing: a confirmation naming anyone else, a stage the action does not go to, the wrong actor.
            before = state(b, "PGU-4")
            spoof = {"kick_librarian": cli(b, "audit", "audit-kick-back", "PGU-4", "--reason", "x", "--target-assignee", "librarian"),
                     "docs_ops": cli(b, "audit", "audit-documentation-kick-back", "PGU-4", "--reason", "x", "--target-assignee", "ops"),
                     "kick_to_docs": short(b.act("PGU-4", "audit_kick_back", "audit", reason="x", state="documentation")),
                     "docs_to_impl": short(b.act("PGU-4", DOCS, "audit", reason="x", state="in_progress")),
                     "docs_by_librarian": short(b.act("PGU-4", DOCS, "librarian", reason="x")),
                     "docs_no_reason": short(b.act("PGU-4", DOCS, "audit")),
                     "set_docs": short(b.act("PGU-4", "set_audit_return", "director", stage="documentation",
                                             assignee="librarian", reason="r"))}
            check("audit_kick_back returns PGU-4 to the producer of its revision, ops; it cannot send it to librarian" in spoof["kick_librarian"]
                  and f"{DOCS} returns PGU-4 to the Documentation owner it passed, librarian; it cannot send it to ops" in spoof["docs_ops"]
                  and "ambiguous" in spoof["kick_to_docs"] and "ambiguous" in spoof["docs_to_impl"]
                  and spoof["docs_by_librarian"] != "ok" and "reason required" in spoof["docs_no_reason"]
                  and "documentation is a review stage, which produces no substantive work" in spoof["set_docs"]
                  and state(b, "PGU-4") == before and status(b, "PGU-4") == got,
                  f"no target, stage or actor but the recorded one is taken, and nothing changes: {spoof}")
            check(short(b.act("PGU-4", "audit_kick_back", "audit", reason="The parser drops the last line; and the guide.",
                              assignee="librarian")) == "ok",
                  "a mixed finding goes back with the ordinary kick-back (an assignee in the payload changes nothing)")
            returned = state(b, "PGU-4")
            check(returned["where"] == "in_progress/ops" and returned["commit"] == "" and returned["gate"] is True
                  and (returned["audit"], returned["user"], returned["documentation"]) == (False, False, False)
                  and delivered(b) == ["pgu-ops:0.0"] and b.reservations().get("ops") == "PGU-4",
                  f"to ops, the candidate cleared, Audit's and Documentation's sign-offs owed, only ops told: {returned}")
            check(short(b.act("PGU-4", "submit_to_audit", "ops", commit_hash=A)) == "ok"
                  and state(b, "PGU-4")["where"] == "documentation/librarian",
                  f"the resubmission -- even of the same commit -- passes the documentation gate again: {state(b, 'PGU-4')}")
            finished(b, "PGU-4")

            # --- The Librarian's commit, then documentation-only findings, round after round.
            check(submitted(b, "PGU-5", "app", A) == "ok" and librarian(b, "PGU-5", "documentation_sign_off", commit_hash=B) == "ok",
                  "the Librarian's commit reaches Audit")
            # The Director turns the gate off meanwhile: the pass is still on record.
            check(short(b.act("PGU-5", "set_workflow_flags", "director", needs_documentation=False)) == "ok", "the gate off")
            delivered(b)
            rounds = []
            for n, (reason, decision, commit) in enumerate((("docs/operator.md: the example is wrong.", "documentation_sign_off", C),
                                                            ("docs/operator.md: still says --force.", "documentation_no_change", None),
                                                            ("README: link is dead.", "documentation_sign_off", D)), 1):
                kick = (cli(b, "audit", "audit-documentation-kick-back", "PGU-5", "--reason", reason, "--target-assignee", "librarian")
                        if n == 1 else short(b.act("PGU-5", DOCS, "audit", reason=reason)))
                back = state(b, "PGU-5")
                told = delivered(b)
                payload = {"commit_hash": commit} if commit else {"reason": "Fixed already."}
                passed = librarian(b, "PGU-5", decision, **payload)
                delivered(b)
                rounds.append({"kick": kick, "back": back, "told": told, "passed": passed, "status": status(b, "PGU-5")})
            for n, (done, layer) in enumerate(zip(rounds, (B, C, C)), 1):
                check(done["kick"] == "ok" and done["back"]["where"] == "documentation/librarian" and done["back"]["commit"] == layer
                      and (done["back"]["audit"], done["back"]["documentation"]) == (False, False)
                      and done["told"] == ["pgu-librarian:0.0"] and done["passed"] == "ok",
                      f"round {n}: back to the Librarian on the audited candidate ({layer}), its sign-off and Audit's owed: {done}")
            got = rounds[-1]["status"]
            check((got.get("stage"), got.get("assignee"), got.get("commit")) == ("in_progress", "app", A) and got.get("valid") is True
                  and ((got.get("documentation") or {}).get("commit"), (got.get("documentation") or {}).get("base")) == (D, A),
                  f"after every round the origin under the layer is still app's A: {got}")
            check(short(b.act("PGU-5", "audit_kick_back", "audit", reason="And the code.")) == "ok"
                  and state(b, "PGU-5")["where"] == "in_progress/app" and delivered(b) == ["pgu-app:0.0"],
                  f"so the ordinary kick-back still returns to app: {state(b, 'PGU-5')}")
            finished(b, "PGU-5")

            # --- A documentation pass on another revision: an edit after it ends the layer.
            check(submitted(b, "PGU-6", "app", A) == "ok" and librarian(b, "PGU-6", "documentation_sign_off", commit_hash=B) == "ok"
                  and short(b.act("PGU-6", "director_edit", "director", reason="no code after all", patch={"commit_exempt": True})) == "ok",
                  "the Director's edit makes a new revision in Audit")
            before = state(b, "PGU-6")
            answer = short(b.act("PGU-6", DOCS, "audit", reason="Docs."))
            check(answer.startswith("PGU-6 cannot be returned to Documentation: the board has no record that it passed "
                                    f"through documentation with the revision it carries ({B})")
                  and short(b.act("PGU-6", "audit_kick_back", "audit", reason="x")).startswith("PGU-6 cannot be kicked back")
                  and state(b, "PGU-6") == before,
                  f"neither layer is known for a revision nobody known made: {answer}")
            finished(b, "PGU-6")

            # --- The workflow changes under tickets in Audit.
            for ticket in ("PGU-7", "PGU-8"):
                check(submitted(b, ticket, "app" if ticket == "PGU-7" else "ops", A) == "ok"
                      and librarian(b, ticket, "documentation_sign_off", commit_hash=B) == "ok", f"{ticket} reaches Audit")
            delivered(b)
            # A record that does not match -- written by older code, or by hand -- is never trusted.
            tampered = {}
            for column, value in (("documentation_commit", C), ("documentation_stage", "inspection")):
                b.sql(f"UPDATE ticket_board.ticket_audit_returns SET {column} = '{value}' WHERE ticket_id = 'PGU-8';")
                tampered[column] = (status(b, "PGU-8").get("documentation") or {}).get("problem")
                b.sql(f"UPDATE ticket_board.ticket_audit_returns SET {column} = "
                      f"'{B if column == 'documentation_commit' else 'documentation'}' WHERE ticket_id = 'PGU-8';")
            check(tampered == {"documentation_commit": "the board has no record that it passed through documentation with "
                                                       f"the revision it carries ({B})",
                               "documentation_stage": "it passed through inspection, not documentation, where this return goes"}
                  and (status(b, "PGU-8").get("documentation") or {}).get("valid") is True,
                  f"a documentation layer for another revision, or recorded in another stage, is refused: {tampered}")
            revision = int(b.sql("SELECT revision FROM ticket_board.workflow_configuration WHERE singleton;"))
            plain = must("the gate comes off", lambda: without_documentation_gate(b.app.workflow_configuration()))
            must("the board takes it", lambda: b.app.apply_workflow(plain, expected_revision=revision, dry_run=False,
                                                                     caller_role="director"))
            before = state(b, "PGU-7")
            gone = {"offered": offered(b, "PGU-7"), "docs": short(b.act("PGU-7", DOCS, "audit", reason="Docs.")),
                    "unchanged": state(b, "PGU-7") == before}
            check(DOCS not in gone["offered"] and gone["docs"] != "ok" and gone["unchanged"]
                  and not any(t["action"] == DOCS for t in plain["transitions"])
                  and gone["offered"]["audit_kick_back"]["returns_to"] == {"stage": "in_progress", "assignee": "app"},
                  f"the gate removed, there is no documentation return, and the origin stands: {gone}")
            check(short(b.act("PGU-7", "audit_kick_back", "audit", reason="Redo.")) == "ok"
                  and state(b, "PGU-7")["where"] == "in_progress/app", f"the ordinary kick-back works: {state(b, 'PGU-7')}")
            finished(b, "PGU-7")
            director_gate = must("the gate goes back on, the Director deciding", lambda: with_documentation_gate(plain))
            revision = int(b.sql("SELECT revision FROM ticket_board.workflow_configuration WHERE singleton;"))
            must("the board takes it", lambda: b.app.apply_workflow(director_gate, expected_revision=revision,
                                                                     dry_run=False, caller_role="director"))
            got = status(b, "PGU-8")
            before = state(b, "PGU-8")
            answer = short(b.act("PGU-8", DOCS, "audit", reason="Docs."))
            check((got.get("documentation") or {}).get("problem") == "librarian is not an active role in the workflow"
                  and answer.startswith("PGU-8 cannot be returned to Documentation: librarian is not an active role")
                  and state(b, "PGU-8") == before,
                  f"a Librarian no longer active is not returned to, and nothing moves: {got} {answer}")
            check(short(b.act("PGU-8", "audit_kick_back", "audit", reason="Redo.")) == "ok"
                  and state(b, "PGU-8")["where"] == "in_progress/ops", f"the origin is unchanged: {state(b, 'PGU-8')}")
            finished(b, "PGU-8")

            # --- A board without the gate: no documentation return is declared or offered.
            n = board("nogate", example(ROOT))
            fixture.seed_postgres_ticket(n.admin, "PGU-9", title="Plain", state="in_progress", assignee="app")
            check(short(n.act("PGU-9", "submit_to_audit", "app", commit_hash=A)) == "ok"
                  and DOCS not in offered(n, "PGU-9") and short(n.act("PGU-9", DOCS, "audit", reason="x")) != "ok"
                  and (status(n, "PGU-9").get("documentation"), status(n, "PGU-9").get("assignee")) == (None, "app")
                  and n.where("PGU-9") == ["audit", "audit"],
                  f"without the gate there is nothing to return documentation to: {status(n, 'PGU-9')}")

            # --- An upgrade from main, with tickets in flight.
            u_admin = build_board(cluster, "upgraded", before=before_root(Path(tmp) / "before"))
            u = Board.__new__(Board)
            u.admin = u_admin
            u.service = fixture.conninfo(cluster.socket_dir, cluster.port, "upgraded", fixture.SERVICE_ROLE)
            composed = must("the gate composes", lambda: with_documentation_gate(
                example(ROOT), librarian={"runtime": "claude", "template_role": "main"}))
            older = {**composed, "transitions": [t for t in composed["transitions"] if t["action"] != DOCS]}
            with psycopg.connect(u.service, autocommit=False) as conn:  # as main applied it: main's own validation
                conn.execute("SELECT set_config('ticket_board.caller_role', 'director', true), "
                             "set_config('ticket_board.project', 'pgu', true)")
                conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb, 0)", (json.dumps(older),))
                conn.commit()

            def as_role(role: str, statement: str) -> str:
                return short(Board.as_role_sql(u, role, statement))

            def act(ticket: str, action: str, role: str, payload: dict) -> str:
                return as_role(role, f"SELECT ticket_board.perform_workflow_action('{ticket}', '{action}', '{json.dumps(payload)}');")

            walk = {}
            for ticket, owner, decision in (("PGU-50", "app", "documentation_sign_off"), ("PGU-51", "ops", "documentation_no_change"),
                                            ("PGU-52", "main", "documentation_sign_off")):
                fixture.seed_postgres_ticket(u_admin, ticket, title=f"In flight {ticket}", state="in_progress", assignee=owner)
                walk[ticket] = [as_role("director", f"SELECT ticket_board.set_declared_flags('{ticket}', "
                                                    "'{\"needs_documentation\": true}'::jsonb);"),
                                act(ticket, "submit_to_audit", owner, {"commit_hash": A}),
                                act(ticket, decision, "librarian", {"commit_hash": B} if decision.endswith("off") else {"reason": "r"})]
            walk["PGU-52"].append(act("PGU-52", "audit_kick_back", "audit", {"reason": "Fix the parser."}))
            main_52 = Board.where(u, "PGU-52")
            check(all(step == "ok" for steps in walk.values() for step in steps) and main_52 == ["documentation", "librarian"],
                  f"main: Audit's finding on the Librarian's commit goes to the Librarian: {walk} {main_52}")
            must("this tree's migrations upgrade main's board", lambda: migrate(u_admin))
            u.app = fixture.TicketBoardApp(cluster.root / "u-frames", cluster.root / "u-assets", project="pgu",
                                           ticket_prefix="PGU", database_url=u.service)
            u.app._resolve_known_commit = lambda value: (value.lower(), [])
            u.server = fixture.TicketBoardServer(("127.0.0.1", 0), u.app, director_notifier=fixture.QuietNotifier())
            threading.Thread(target=u.server.serve_forever, daemon=True).start()
            u.base = f"http://127.0.0.1:{u.server.server_port}"
            u.sleepers = []
            boards.append(u)
            stored = u.app.workflow_configuration()
            check(stored == composed and [t for t in stored["transitions"] if t["action"] == DOCS]
                  == [t for t in composed["transitions"] if t["action"] == DOCS],
                  f"the upgrade declares Audit's documentation return exactly as the gate composes it: "
                  f"{[t for t in stored['transitions'] if t['action'] == DOCS]}")
            s50, s51 = status(u, "PGU-50"), status(u, "PGU-51")
            check(s50.get("valid") is False
                  and s50.get("problem") == f"its revision ({B}) is the documentation commit librarian made by "
                                            "documentation_sign_off, and the board has no record of who produced the work "
                                            "under it (unknown)"
                  and (s50.get("documentation") or {}).get("valid") is True
                  and ((s50.get("documentation") or {}).get("assignee"), (s50.get("documentation") or {}).get("commit"))
                  == ("librarian", B) and read_client.needs_director(u.ticket("PGU-50")),
                  f"the Librarian's commit recorded as the producer becomes the documentation layer; the work under it "
                  f"is unrecorded, nothing guessed, and the Director's queue lists it: {s50}")
            check((s51.get("valid"), s51.get("stage"), s51.get("assignee"), s51.get("commit")) == (True, "in_progress", "ops", A)
                  and (s51.get("documentation") or {}).get("valid") is False,
                  f"an origin recorded before the upgrade stands; a pass main never recorded is not invented: {s51}")
            notices = json.loads(u.sql(
                "SELECT coalesce(json_agg(json_build_array(ticket_id, target_role, payload->>'kind', message) ORDER BY id), "
                "'[]') FROM ticket_board.ticket_notification_queue WHERE dedupe_key LIKE 'audit_return_layers:%';"))
            check(len(notices) == 1 and notices[0][:3] == ["PGU-50", "director", "audit_return_missing"]
                  and "nothing was guessed and nothing was moved" in notices[0][3],
                  f"the Director is told once, for the ticket in Audit only: {notices}")
            before = state(u, "PGU-50")
            answer = short(u.act("PGU-50", "audit_kick_back", "audit", reason="Fix it."))
            check(answer.startswith("PGU-50 cannot be kicked back: its revision") and state(u, "PGU-50") == before,
                  f"its ordinary kick-back waits for the Director's record, moving nothing: {answer}")
            check(short(u.act("PGU-50", "set_audit_return", "director", stage="in_progress", assignee="app",
                              reason="App submitted it before the upgrade.")) == "ok"
                  and (status(u, "PGU-50").get("valid"), status(u, "PGU-50").get("commit")) == (True, B)
                  and (status(u, "PGU-50").get("documentation") or {}).get("valid") is True
                  and short(u.act("PGU-50", "audit_kick_back", "audit", reason="Fix it.")) == "ok"
                  and u.where("PGU-50") == ["in_progress", "app"],
                  f"recorded, it returns to app, and the documentation layer stood beside it: {status(u, 'PGU-50')}")
            # Main returned PGU-52 to the Librarian; its next commit layers on that one, and the origin is the Director's to record.
            check(act("PGU-52", "documentation_sign_off", "librarian", {"commit_hash": C}) == "ok"
                  and (status(u, "PGU-52").get("documentation") or {}).get("base") == B
                  and status(u, "PGU-52").get("valid") is False
                  and short(u.act("PGU-52", "set_audit_return", "director", stage="in_progress", assignee="main", reason="r")) == "ok"
                  and status(u, "PGU-52").get("commit") == B
                  and short(u.act("PGU-52", "audit_kick_back", "audit", reason="x")) == "ok" and u.where("PGU-52") == ["in_progress", "main"],
                  f"a round begun on main finishes on the record: {status(u, 'PGU-52')}")
            # Replay: PGU-3 walked the same way on this tree, and recorded the same origin the upgrade kept.
            keys = ("valid", "stage", "assignee", "action", "commit")
            check({k: replayed.get(k) for k in keys} == {k: s51.get(k) for k in keys},
                  f"replayed here, the walk records the origin the upgrade kept: {replayed} {s51}")

            # schema.sql and rbac.sql install what the migrations do.
            shape = ("SELECT coalesce(json_agg(json_build_array(p.oid::regprocedure::text, md5(pg_get_functiondef(p.oid)), "
                     "pg_get_userbyid(p.proowner), coalesce(p.proacl::text, '')) ORDER BY p.oid::regprocedure::text), '[]')::text "
                     "FROM pg_proc p WHERE p.pronamespace = 'ticket_board'::regnamespace AND p.proname IN ("
                     "'enforce_declared_ticket_update', 'validate_declared_workflow', 'audit_return_transition', "
                     "'audit_documentation_return_transition', 'documentation_pass_action', 'substantive_origin_problem', "
                     "'audit_return_stage', 'producing_holder', 'audit_return_problem', 'record_audit_return', "
                     "'audit_return_status', 'set_audit_return');")
            table = ("SELECT json_build_array((SELECT json_agg(attname::text || ':' || format_type(atttypid, atttypmod) "
                     "ORDER BY attnum) FROM pg_attribute WHERE attrelid = 'ticket_board.ticket_audit_returns'::regclass "
                     "AND attnum > 0 AND NOT attisdropped), (SELECT relacl::text FROM pg_class WHERE oid = "
                     "'ticket_board.ticket_audit_returns'::regclass))::text;")
            fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
            fixture.psql(fresh, (ROOT / "scripts/ticket_board/schema.sql").read_text())
            fixture.psql(fresh, (ROOT / "scripts/ticket_board/rbac.sql").read_text())
            shapes = [[json.loads(fixture.psql(url, shape).strip()), fixture.psql(url, table).strip()]
                      for url in (b.admin, u_admin, fresh)]
            check(shapes[0] == shapes[1] == shapes[2] and len(shapes[0][0]) == 12,
                  f"schema.sql and rbac.sql install what the migrations do, fresh and upgraded from main: "
                  f"{[row[0] for row in shapes[0][0]]}")
        finally:
            for each in boards:
                each.close()
    print(f"audit_documentation_return_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
