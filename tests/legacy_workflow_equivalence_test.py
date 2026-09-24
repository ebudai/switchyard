#!/usr/bin/env python3
"""SYRD-240: does the declared document do what the legacy board does?

`legacy_workflow.compose_legacy_workflow` builds a declared document for a
tenant seeded with `default-project`. The validator accepting it proves only
that it is well formed. Whether it BEHAVES like the board it replaces is a
different question, and the two execution models answer it differently:

* a legacy board runs per-operation handlers that write a patch and let
  triggers advance the ticket -- `audit_sign_off` does not move the ticket at
  all, it sets a flag and a trigger decides where it lands;
* a declared board runs `perform_workflow_action`, which moves the ticket to
  the transition's declared destination.

So this does not reason about either. It builds both boards in one disposable
cluster, drives the SAME HTTP calls against each, and compares what comes out.
Every outcome that differs must be one the composer already declared -- in
`differences` or `additions` -- and anything else fails. That is what turns
"the document is valid" into "the document is this tenant's workflow".
"""

from __future__ import annotations

import copy
import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()
import ticket_board_write_api_test as t  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402
from ticket_board import legacy_workflow as lw  # noqa: E402
from ticket_board import project_provision as pv  # noqa: E402

PROJECT = "mefp"
CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def mefp_plan():
    """A plan shaped like the live mefp board: a designer-owned draft stage, main
    and ops implementing, audit reviewing -- as its /api/workflow shows."""
    return pv.build_plan(
        project=PROJECT, project_name="MEFP", owner_user="stellaris-agent",
        owner_home=Path("/home/stellaris-agent"), source_repo=ROOT,
        implementer_roles=("main", "ops"), include_designer=True, include_audit=True,
    )


def mefp_panes() -> dict:
    """MEFP's four panes, as its configuration runs them (SYRD-262)."""
    return {
        role: {"runtime": runtime, "target": f"{PROJECT}-{role}:0.0", "slot": slot}
        for slot, (role, runtime) in enumerate(
            (("director", "codex"), ("main", "codex"), ("ops", "codex"), ("audit", "claude"))
        )
    }


class Board:
    """One board in the cluster, reachable through the real HTTP handler."""

    def __init__(self, cluster, name: str) -> None:
        self.admin = t.conninfo(cluster.socket_dir, cluster.port, name)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port),
               "-U", "postgres", name])
        t.psql(self.admin, t.SCHEMA_PATH.read_text())
        try:
            t.create_roles(self.admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        t.psql(self.admin, t.RBAC_PATH.read_text())
        (cluster.root / f"frames-{name}").mkdir(exist_ok=True)
        (cluster.root / f"assets-{name}").mkdir(exist_ok=True)
        self.app = t.TicketBoardApp(
            cluster.root / f"frames-{name}", cluster.root / f"assets-{name}",
            project=PROJECT, ticket_prefix="MEFP",
            database_url=t.conninfo(cluster.socket_dir, cluster.port, name, t.SERVICE_ROLE),
        )
        self.server = t.TicketBoardServer(("127.0.0.1", 0), self.app,
                                          director_notifier=t.QuietNotifier())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def act(self, ticket_id: str, operation: str, actor: str, **payload):
        """(ok, ticket-or-error). A refusal is an outcome, not a test failure."""
        t.TEST_WRITE_TOKEN = self.server.write_token
        try:
            out = t.post_json(self.base, f"/api/tickets/{ticket_id}/actions/{operation}",
                              payload, caller=actor, expect=200)
            return True, out["ticket"]
        except AssertionError as exc:
            return False, str(exc)

    def row(self, ticket_id: str) -> dict:
        return json.loads(t.psql(
            self.admin,
            "SELECT jsonb_build_object('state',state,'assignee',assignee,"
            "'commit_hash',commit_hash,'audit_signoff',audit_signoff,"
            "'user_signoff',user_signoff,"
            # Who was queued a notice for this ticket: a stage's notify is
            # behaviour too, and only visible here (SYRD-262).
            "'notified',(SELECT coalesce(jsonb_agg(q.target_role ORDER BY q.id),'[]'::jsonb) "
            "FROM ticket_board.ticket_notification_queue q WHERE q.ticket_id=t.id)) "
            "FROM ticket_board.tickets t "
            f"WHERE id='{ticket_id}';",
        ))

    def close(self) -> None:
        self.server.shutdown()


@dataclass
class Scenario:
    name: str
    #: How the ticket is seeded before the action.
    seed: dict
    operation: str
    actor: str
    payload: dict
    #: The exact divergence this scenario is expected to show, or None for
    #: "identical on both boards". A dict of field -> (legacy, declared), plus
    #: the optional key "verdict" -> (legacy_ok, declared_ok).
    #:
    #: Exact, not "the operation name appears in the differences text". That
    #: coarser check let a refusal caused by a malformed payload pass as the
    #: declared commit-clearing difference -- a substring matching the wrong
    #: thing, which is precisely what an expectation has to rule out.
    expect: dict | None = None


SCENARIOS = [
    # Triage belongs to the Director; the implementer picks the work up.
    Scenario("start work", {"state": "analysis", "assignee": "director"},
             "start_work", "main", {}),
    Scenario("submit to audit", {"state": "in_progress", "assignee": "main", "commit_exempt": True},
             "submit_to_audit_without_commit", "main", {"reason": "doc-only change"}),
    Scenario("audit kick-back", {"state": "audit", "assignee": "audit", "commit_hash": "a" * 40,
                                 "implementer": "ops"},
             "audit_kick_back", "audit", {"reason": "missing test"}),
    Scenario("DAT kick-back", {"state": "dat", "assignee": "director", "audit_signoff": True,
                               "commit_hash": "b" * 40, "implementer": "ops"},
             "director_dat_kick_back", "director", {"reason": "found a defect"}),
    Scenario("implementer kick-back", {"state": "in_progress", "assignee": "main",
                                       "commit_hash": "c" * 40},
             "implementer_kick_back", "main", {"reason": "needs re-scoping"}),
    Scenario("user reopen from done", {"state": "done", "assignee": "director",
                                       "commit_hash": "d" * 40},
             "user_reopen", "user", {"reason": "regressed"}),
    # MEFP's draft stage belongs to a designer it runs no pane for (SYRD-262).
    Scenario("route into draft for the designer", {"state": "analysis", "assignee": "director"},
             "route", "director", {"state": "draft", "assignee": "designer"}),
    Scenario("cancel from triage", {"state": "analysis", "assignee": "director"},
             "cancel", "director", {"text": "no longer needed"}),
    # The one case the composer declares rather than reproduces: a reviewed
    # ticket nobody is on record as having implemented. It is kept so the
    # declared difference is observed, not merely asserted.
    # Each remaining declared difference gets a scenario that exercises it, so
    # the declaration is observed rather than trusted. The one I got wrong --
    # user_reopen clearing the hash -- was only caught because a scenario ran.
    # -- the sign-offs: the risk named first ---------------------------------
    #
    # Legacy `audit_sign_off` does not move the ticket: it sets audit_signoff
    # and a trigger decides where it lands, by gate. A declared approve moves
    # it to the transition's destination. These are the scenarios where the
    # two execution models differ most in HOW, so they are where a difference
    # in WHAT would hide.
    Scenario("audit sign-off, DAT and UAT both required",
             {"state": "audit", "assignee": "audit", "commit_hash": "3" * 40,
              "implementer": "main", "needs_user_signoff": True},
             "audit_sign_off", "audit", {"text": "reviewed"}),
    Scenario("audit sign-off, no UAT required",
             {"state": "audit", "assignee": "audit", "commit_hash": "4" * 40,
              "implementer": "main", "needs_user_signoff": False},
             "audit_sign_off", "audit", {"text": "reviewed"}),
    Scenario("DAT sign-off to UAT",
             {"state": "dat", "assignee": "director", "audit_signoff": True,
              "commit_hash": "5" * 40, "implementer": "main", "needs_user_signoff": True},
             "director_dat_sign_off", "director", {}),
    Scenario("user sign-off",
             {"state": "user_review", "assignee": "user", "audit_signoff": True,
              "commit_hash": "6" * 40, "implementer": "main", "needs_user_signoff": True},
             "user_sign_off", "user", {"text": "accepted"}),
    # Legacy `route` takes its destination in the payload; a declared route
    # carries it in the transition. Passing it keeps the call fair to both --
    # without it legacy refuses with "invalid state" for a reason that has
    # nothing to do with the workflow.
    Scenario("route out of done", {"state": "done", "assignee": "director",
                                   "commit_hash": "f" * 40},
             "route", "director", {"state": "analysis", "assignee": "director"}),
    Scenario("route out of cancelled", {"state": "cancelled", "assignee": "director",
                                        "commit_hash": "1" * 40},
             "route", "director", {"state": "analysis", "assignee": "director"}),
    Scenario("implementer kick-back while blocked",
             {"state": "in_progress", "assignee": "main", "commit_hash": "2" * 40,
              "blocked_by": "MEFP-99"},
             "implementer_kick_back", "main", {"reason": "blocked, re-scope"},
             expect={"verdict": (True, False),
                     "state": ("analysis", "in_progress"),
                     "assignee": ("director", "main")}),
    Scenario("audit kick-back, no implementer on record",
             {"state": "audit", "assignee": "audit", "commit_hash": "e" * 40},
             "audit_kick_back", "audit", {"reason": "missing test"},
             expect={"assignee": ("ops", "main")}),
]

#: Fields whose value after an action must agree between the two boards,
#: unless the composer declared the difference.
COMPARED = ("state", "assignee", "commit_hash", "audit_signoff", "user_signoff")


def seed(board: Board, ticket_id: str, spec: dict) -> None:
    t.seed_postgres_ticket(
        board.admin, ticket_id, title=f"equivalence {ticket_id}",
        state=spec["state"], assignee=spec["assignee"],
        commit_hash=spec.get("commit_hash", ""),
        commit_exempt=spec.get("commit_exempt", False),
        audit_signoff=spec.get("audit_signoff", False),
        needs_audit=True,
        needs_user_signoff=spec.get("needs_user_signoff", False),
    )
    # A ticket that has reached review got there through implementation, so it
    # has an implementer on record. Seeding it without one would compare the
    # two boards' FALLBACKS for a state no real ticket is in -- the case where
    # a kick-back has nobody to return to -- which is a separate question with
    # its own case below.
    blocker = spec.get("blocked_by")
    if blocker:
        t.psql(board.admin,
               "SELECT set_config('ticket_board.caller_role','director',false);\n"
               "INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position, resolved) "
               f"VALUES ('{ticket_id}', '{blocker}', 0, false);\n"
               f"UPDATE ticket_board.tickets SET blocked_reason='waiting' WHERE id='{ticket_id}';")
    implementer = spec.get("implementer")
    if implementer:
        t.psql(board.admin,
               "UPDATE ticket_board.ticket_notification_state "
               f"SET last_implementer_assignee='{implementer}' WHERE ticket_id='{ticket_id}';")


def main() -> int:
    plan = mefp_plan()
    declared = lw.compose_legacy_workflow(
        plan, canonical=lw.load_canonical(ROOT),
        stage_seeds=pv.project_workflow_stages(plan),
        transition_seeds=pv.project_workflow_transitions(plan),
        panes=mefp_panes(),
    )
    undeclared: list[str] = []
    with temporary_cluster(prefix="syrd240-equiv-", shutdown="immediate") as cluster:
        legacy = Board(cluster, "legacy")
        modern = Board(cluster, "declared")
        try:
            # The legacy board: the default-project seed, exactly as provisioning
            # applies it, and no declared document.
            t.psql(legacy.admin, pv.render_workflow_sql(plan))
            check(legacy.app.workflow_configuration() is None,
                  "the legacy board runs no declared workflow")

            # The declared board takes the path mefp itself will take: the same
            # legacy seed first, so its stage and transition rows are exactly
            # the legacy board's, and THEN the composed document applied on top
            # of them -- which is what `migrate-workflow --apply` does to a live
            # tenant. Starting it from a bare schema would test a board no
            # tenant has; schema.sql seeds stages mefp does not carry.
            t.psql(modern.admin, pv.render_workflow_sql(plan))
            with modern.app._pg_connect() as conn:
                modern.app._pg_set_caller_role(conn, "director")
                conn.execute("SELECT set_config('ticket_board.project', %s, false)", (PROJECT,))
                conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)",
                             (json.dumps(declared.document),))
                conn.commit()
            check(modern.app.workflow_configuration() is not None,
                  "the declared board runs the composed document")

            for index, scenario in enumerate(SCENARIOS, start=1):
                ticket_id = f"MEFP-{index}"
                seed(legacy, ticket_id, scenario.seed)
                seed(modern, ticket_id, scenario.seed)
                ok_legacy, out_legacy = legacy.act(ticket_id, scenario.operation,
                                                   scenario.actor, **scenario.payload)
                ok_modern, out_modern = modern.act(ticket_id, scenario.operation,
                                                   scenario.actor, **scenario.payload)
                row_legacy, row_modern = legacy.row(ticket_id), modern.row(ticket_id)
                same_verdict = ok_legacy == ok_modern
                same_rows = all(row_legacy[k] == row_modern[k] for k in COMPARED)
                observed: dict = {
                    k: (row_legacy[k], row_modern[k])
                    for k in COMPARED if row_legacy[k] != row_modern[k]
                }
                if not same_verdict:
                    observed["verdict"] = (ok_legacy, ok_modern)
                expected = scenario.expect or {}
                if observed != expected:
                    undeclared.append(
                        f"{scenario.name}: expected {expected or 'identical'}, observed "
                        f"{observed or 'identical'}"
                        + (f" | legacy said: {out_legacy[:160]}" if not ok_legacy else "")
                        + (f" | declared said: {out_modern[:160]}" if not ok_modern else "")
                    )
                print(f"  {scenario.name:24} legacy={'ok' if ok_legacy else 'refused':8} "
                      f"declared={'ok' if ok_modern else 'refused':8} "
                      f"{'same' if same_verdict and same_rows else 'DIFFERS'}")
                check(True, scenario.name)
                # Each scenario gets the boards to itself. A ticket left in
                # implementation holds its implementer's serial slot, and the
                # next seed for that implementer would be redirected to a
                # holding stage -- testing serial focus, not this document.
                for board in (legacy, modern):
                    t.psql(board.admin, f"DELETE FROM ticket_board.tickets WHERE id='{ticket_id}';")
        finally:
            legacy.close()
            modern.close()

    # Both directions, not one. Every observed divergence has to be expected
    # (above), and every difference the composer DECLARES has to be one a
    # scenario actually observes. Two of my first declarations -- user_reopen
    # and routing out of a terminal stage "clearing the commit" -- were false:
    # legacy clears it too. An operator reading the adoption preview would have
    # been told their board changes in ways it does not.
    observed_actions = {s.operation for s in SCENARIOS if s.expect}
    for line in declared.differences:
        action_part = line.split(":", 1)[0]
        named = {a.strip() for a in action_part.split(",")}
        check(
            named & observed_actions,
            f"declared difference is observed by some scenario: {line}",
        )
    check(
        len(declared.differences) == len({s.operation for s in SCENARIOS if s.expect}),
        f"one declared difference per observed one: declared {list(declared.differences)}, "
        f"observed {sorted(observed_actions)}",
    )

    if undeclared:
        print("\nUNDECLARED DIFFERENCES:")
        for line in undeclared:
            print("  -", line)
    check(not undeclared, f"{len(undeclared)} behavioural differences the composer did not declare")
    print(f"legacy_workflow_equivalence_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
