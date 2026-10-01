#!/usr/bin/env python3
"""SYRD-536: a Director's declared return out of User Review is not refused for want of the User's sign-off.

MEFP-233 sat in user_review with audit_signoff=true and user_signoff=false. Its
workflow_actions advertised the Director's route to analysis, and the board
refused it: "stage signoff required". The guard treated every move out of a
review stage that was not a `return`, a `reopen` or a park as forward, so the
Director could not hand the work back without the User accepting it first.

Now a move whose destination is where that stage already sends work back --
its own `return`/`reopen` destinations -- and which is not on the stage's
declared forward path (approval destinations and gate skips) nor terminal, is a
return: no sign-off is needed and none is granted, cleared or forged, and a
blocker does not stop it. Everything else is unchanged, and what the board
advertises is what it will do.

Production-built board (companion roles, schema.sql, the real
ticket-board-migrate, rbac.sql) with a MEFP-shaped declared workflow -- the
User signs off or reopens to analysis; the Director routes user_review to
analysis or to audit -- driven through the real app. The reproduction runs
main's code and SQL in a child, from a git worktree.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "a8d3be93f5f667cdc3f5414b72aa9b8977e6e01b"  # main before SYRD-536
COMMIT = "6d4ee1aa99147e8118f59e637be02b660d62d064"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


GRANTEES = ("SELECT coalesce(string_agg(DISTINCT coalesce(r.rolname, 'PUBLIC'), ','), '') "
            "FROM pg_proc p CROSS JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
            "LEFT JOIN pg_roles r ON r.oid=a.grantee "
            "WHERE p.proname='declared_review_return' AND a.privilege_type='EXECUTE';")


def move(frm: str, to: str, action: str, actors: list[str]) -> dict:
    return {"from": frm, "to": to, "action": action, "label": action, "actors": actors, "primitive": "move",
            "owner_scoped": False, "allow_no_code": False, "clear_signoffs": [], "require_commit": False,
            "require_reason": False, "relays_decision_of": None}


def mefp_review(document: dict) -> dict:
    """MEFP's user_review: the User signs off or reopens; the Director routes back to analysis or audit.

    Plus one Director move FORWARD to director_review, which nothing should let
    past the missing sign-off.
    """
    doc = copy.deepcopy(document)
    doc["transitions"] = [t for t in doc["transitions"] if t.get("action") != "user_kick_back"]
    doc["transitions"] += [
        {**move("user_review", "analysis", "user_reopen", ["user"]), "primitive": "reopen", "label": "Reopen"},
        move("user_review", "analysis", "route", ["director"]),
        move("user_review", "audit", "route", ["director"]),
        move("user_review", "director_review", "promote", ["director"]),
    ]
    return doc


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as t
    from scripts.ticket_board import workflow_config
    from temporary_cluster import temporary_cluster
    from workflow_document_eras import before_relaying

    seen: dict = {}

    def build(cluster, db: str, *, migrate: bool) -> str:
        admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(admin, """
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_service') THEN
        CREATE ROLE ticket_board_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        CREATE ROLE ticket_board_listener LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END $$;
""")
        try:
            t.create_roles(admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        t.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
        if migrate:
            runner = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")],
                                    env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, capture_output=True, text=True)
            assert runner.returncode == 0, runner.stderr
            # Before rbac.sql: the window in which a new function is PUBLIC unless revoked.
            seen[f"{db} grantees before rbac"] = t.psql(admin, GRANTEES).strip()
        t.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        return admin

    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = build(cluster, "board", migrate=True)
        app = t.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="cerulean", ticket_prefix="PGU",
                               database_url=t.conninfo(cluster.socket_dir, cluster.port, "board", t.SERVICE_ROLE))
        doc = mefp_review(before_relaying())
        doc["project"] = "cerulean"
        doc.setdefault("reassign", {})
        doc.setdefault("remove_stages", [])
        app.apply_workflow(doc, expected_revision=0, dry_run=False, caller_role="director")
        cfg = app.workflow_configuration()

        def ticket(tid: str) -> None:
            # MEFP-233: User Review, audited, the User has not signed off.
            t.seed_postgres_ticket(admin, tid, title=tid, state="user_review", assignee="user", commit_hash=COMMIT,
                                   needs_audit=True, audit_signoff=True, needs_user_signoff=True, user_signoff=False)

        def state(tid: str) -> dict:
            got = app.get_ticket(tid)
            return {k: got[k] for k in ("state", "assignee", "audit_signoff", "user_signoff", "needs_user_signoff", "commit_hash")} | {
                "advertised": sorted(f"{a['action']}->{a['to']}" for a in got.get("workflow_actions", []))}

        def act(tid: str, action: str, target: str, role: str = "director") -> str:
            try:
                app.perform_workflow_action(tid, action, {"target": target}, caller_role=role)
                return "ok"
            except Exception as exc:  # noqa: BLE001
                return f"{type(exc).__name__}: {str(exc).splitlines()[0]}"

        ticket("PGU-1")
        seen["before"] = state("PGU-1")
        seen["route to analysis"] = act("PGU-1", "route", "analysis")
        seen["after route to analysis"] = state("PGU-1")
        ticket("PGU-2")
        seen["route to audit"] = act("PGU-2", "route", "audit")
        seen["forward promote"] = act("PGU-2", "promote", "director_review")
        seen["not the director"] = act("PGU-2", "route", "analysis", role="ops")
        seen["unconfigured"] = act("PGU-2", "route", "in_progress")
        seen["cancel unsigned"] = act("PGU-2", "cancel", "cancelled")
        seen["after refusals"] = state("PGU-2")
        # A blocker stops promotion, not a return.
        t.seed_postgres_ticket(admin, "PGU-9", title="PGU-9", state="analysis", assignee="director")
        for tid in ("PGU-3", "PGU-4"):
            ticket(tid)
            app.update_ticket(tid, {"blocked_by": ["PGU-9"], "blocked_reason": "waits on PGU-9"}, caller_role="director")
        seen["blocked return"] = act("PGU-3", "route", "analysis")
        seen["blocked forward"] = act("PGU-4", "promote", "director_review")
        # The User's own decision is untouched by any of this.
        ticket("PGU-5")
        seen["user signs off"] = act("PGU-5", "user_sign_off", "director_review", role="user")
        seen["after user sign-off"] = state("PGU-5")
        # The Python mirror agrees with the database on every pair this declaration has.
        pairs = sorted({(tr["from"], tr["to"]) for tr in cfg["transitions"]} |
                       {(s["name"], d["name"]) for s in cfg["stages"] for d in cfg["stages"]})
        try:
            sql = json.loads(t.psql(admin, "SELECT coalesce(jsonb_agg(jsonb_build_array(s, d, "
                                           "ticket_board.declared_review_return(ticket_board.declared_workflow(), s, d))), '[]')::text "
                                           f"FROM jsonb_to_recordset('{json.dumps([{'s': a, 'd': b} for a, b in pairs])}'::jsonb) AS x(s text, d text);"))
            seen["agreement"] = [[a, b, v, workflow_config.declared_review_return(cfg, a, b)] for a, b, v in sql
                                 if v != workflow_config.declared_review_return(cfg, a, b)]
            seen["sql returns"] = sorted(f"{a}->{b}" for a, b, v in sql if v)
        except Exception as exc:  # noqa: BLE001 -- main has no such function
            seen["agreement"] = f"{type(exc).__name__}"
        # Fresh schema and migrated board install the same guard; nobody but the owner may call the helper.
        fresh = build(cluster, "fresh", migrate=False)
        definitions = "SELECT coalesce(string_agg(md5(pg_get_functiondef(p.oid)), ',' ORDER BY p.proname), '') FROM pg_proc p " \
                      "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='ticket_board' " \
                      "AND p.proname IN ('enforce_declared_ticket_update','declared_review_return');"
        seen["fresh equals migrated"] = t.psql(fresh, definitions).strip() == t.psql(admin, definitions).strip()
        seen["helper grantees"] = t.psql(admin, GRANTEES).strip()
        # A reopen that points FORWARD -- legal to declare -- is not a return.
        odd = copy.deepcopy(cfg)
        for tr in odd["transitions"]:
            if tr["action"] == "user_reopen":
                tr["to"] = "director_review"
        try:
            seen["forward reopen"] = [workflow_config.declared_review_return(odd, "user_review", "director_review"),
                                      t.psql(admin, "SELECT ticket_board.declared_review_return("
                                                    f"'{json.dumps(odd)}'::jsonb, 'user_review', 'director_review');").strip()]
        except Exception as exc:  # noqa: BLE001 -- main has neither
            seen["forward reopen"] = f"{type(exc).__name__}"
        seen["helper owner"] = t.psql(admin, "SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname='declared_review_return';").strip()
        # Audit's case: a stage with NO sign-off whose ordinary forward route
        # shares its destination with an incidental reopen. That is not a return
        # out of review, and a blocker still stops it.
        shadow = copy.deepcopy(cfg)
        shadow["transitions"].append({**move("analysis", "in_progress", "analysis_reopen_shadow", ["director"]),
                                      "primitive": "reopen"})
        revision = int(t.psql(admin, "SELECT coalesce(max(revision), 0) FROM ticket_board.workflow_configuration;").strip())
        app.apply_workflow(shadow, expected_revision=revision, dry_run=False, caller_role="director")
        t.seed_postgres_ticket(admin, "PGU-6", title="PGU-6", state="analysis", assignee="director")
        app.update_ticket("PGU-6", {"blocked_by": ["PGU-9"], "blocked_reason": "wait"}, caller_role="director")
        try:
            app.perform_workflow_action("PGU-6", "route", {"target": "in_progress", "assignee": "app"}, caller_role="director")
            seen["shadow route"] = "ok"
        except Exception as exc:  # noqa: BLE001
            seen["shadow route"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        seen["shadow ticket"] = app.get_ticket("PGU-6")["state"]
        try:
            live = app.workflow_configuration()
            seen["shadow helper"] = [workflow_config.declared_review_return(live, "analysis", "in_progress"),
                                     t.psql(admin, "SELECT ticket_board.declared_review_return("
                                                   "ticket_board.declared_workflow(), 'analysis', 'in_progress');").strip()]
            # And no stage without a sign-off is ever a review source, in either copy.
            pairs = [(a["name"], b["name"]) for a in live["stages"] for b in live["stages"] if not a.get("signoff")]
            seen["non-review returns"] = [f"{a}->{b}" for a, b in pairs if workflow_config.declared_review_return(live, a, b)] + \
                json.loads(t.psql(admin, "SELECT coalesce(jsonb_agg(s || '->' || d), '[]')::text FROM jsonb_to_recordset("
                                         f"'{json.dumps([{'s': a, 'd': b} for a, b in pairs])}'::jsonb) AS x(s text, d text) "
                                         "WHERE ticket_board.declared_review_return(ticket_board.declared_workflow(), s, d);"))
        except Exception as exc:  # noqa: BLE001 -- main has no such function
            seen["shadow helper"] = seen["non-review returns"] = f"{type(exc).__name__}"
    return seen


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print("RESULT " + json.dumps(scenario(Path(sys.argv[2]), "syrd536b-")))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd536-before.") as raw:
        before_tree = Path(raw) / "tree"
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", "-q", str(before_tree), BEFORE], check=True)
        try:
            (before_tree / "tests" / Path(__file__).name).write_text(Path(__file__).read_text(encoding="utf-8"), encoding="utf-8")
            proc = subprocess.run([sys.executable, str(before_tree / "tests" / Path(__file__).name), "--scenario", str(before_tree)],
                                  text=True, capture_output=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        finally:
            subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(before_tree)], check=False)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        assert proc.returncode == 0 and line, proc.stdout[-3000:] + proc.stderr[-3000:]
        before = json.loads(line[len("RESULT "):])
    check("stage signoff required" in before["route to analysis"] and before["after route to analysis"]["state"] == "user_review",
          f"reproduced: the advertised route back to analysis is refused for the User's missing sign-off: {before['route to analysis']}")
    check("route->analysis" in before["before"]["advertised"] and "route->audit" in before["before"]["advertised"],
          f"reproduced: while both routes are advertised: {before['before']['advertised']}")

    now = scenario(ROOT, "syrd536a-")
    back = now["after route to analysis"]
    check(now["route to analysis"] == "ok" and back["state"] == "analysis",
          f"the Director's declared return to analysis goes through: {now['route to analysis']} {back}")
    check(back["user_signoff"] is False and back["needs_user_signoff"] is True and back["audit_signoff"] is True
          and back["commit_hash"] == COMMIT,
          f"and grants, clears and forges nothing: no User sign-off, the gate stays, the audit and candidate ride along: {back}")
    check("stage signoff required" in now["route to audit"], f"a move to audit is not a declared return here, so it still needs the sign-off: {now['route to audit']}")
    check("stage signoff required" in now["forward promote"], f"a forward move still needs it: {now['forward promote']}")
    check("actor cannot perform workflow action" in now["not the director"]
          and "unknown or ambiguous workflow action" in now["unconfigured"],
          f"actors and the declaration still decide who may move where: {now['not the director']} / {now['unconfigured']}")
    check(now["after refusals"]["state"] == "user_review" and now["after refusals"]["user_signoff"] is False,
          f"and the refusals change nothing: {now['after refusals']}")
    check(now["blocked return"] == "ok" and "unresolved blocker prevents forward promotion" in now["blocked forward"],
          f"a blocker does not stop handing work back, and still stops promotion: {now['blocked return']} / {now['blocked forward']}")
    check(now["user signs off"] == "ok" and now["after user sign-off"]["state"] == "director_review" and now["after user sign-off"]["user_signoff"],
          f"the User's own sign-off is unchanged: {now['after user sign-off']}")
    check("stage signoff required" in before["cancel unsigned"] and "stage signoff required" in now["cancel unsigned"],
          f"cancelling an unsigned User Review was refused before and still is: {before['cancel unsigned']} / {now['cancel unsigned']}")
    advertised = now["before"]["advertised"]
    check("route->analysis" in advertised and "route->audit" not in advertised and "promote->director_review" not in advertised
          and "cancel->cancelled" not in advertised and "cancel->cancelled" in before["before"]["advertised"],
          f"what is advertised is what the board will do: {advertised}")
    check(now["agreement"] == [] and now["sql returns"] and all(r.endswith("->analysis") or r.endswith("->in_progress") for r in now["sql returns"]),
          f"the Python mirror and the database agree on every pair, and only returns to correction qualify: {now['agreement']} {now['sql returns']}")
    check(now["forward reopen"] == [False, "f"],
          f"a reopen declared into the stage's forward path is no return, in Python and in the database: {now['forward reopen']}")
    check(now["board grantees before rbac"] == now["helper owner"],
          f"no PUBLIC grant before rbac.sql either -- from the runner's revoke; pgu969's own is equivalent there: "
          f"{now['board grantees before rbac']}")
    check(now["fresh equals migrated"] is True, "a fresh schema and the migrated board install the same guard and helper")
    check(now["helper grantees"] == now["helper owner"] and "PUBLIC" not in now["helper grantees"],
          f"only the owner may call the helper on a board as the runner migrates and rbac.sql grants it -- "
          f"the runner's own revoke (SYRD-530) is what this proves, not pgu969's: {now['helper grantees']} (owner {now['helper owner']})")
    check("unresolved blocker prevents forward promotion" in now["shadow route"] and now["shadow ticket"] == "analysis",
          f"Audit's case: a blocked forward route out of a stage with no sign-off is still refused, "
          f"whatever reopen shares its destination: {now['shadow route']} ({now['shadow ticket']})")
    check(now["shadow helper"] == [False, "f"] and now["non-review returns"] == [],
          f"no stage without a sign-off is a review source, in Python or the database: {now['shadow helper']} {now['non-review returns']}")
    print(f"declared_review_return_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
