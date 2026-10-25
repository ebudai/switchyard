#!/usr/bin/env python3
"""SYRD-678: a designer may also own Implementation work, and Router may be told it takes design work only.

pie wanted its design review run through the ordinary loop: the designer is
assigned the ticket in Implementation, submits it to Audit, Audit's kick-back
returns it to the designer, and Audit's sign-off moves it on. The board refused:
the validator requires every Implementation owner to be of kind `implementer`,
and making the designer one would end its being the designer.

The cases run on boards built the production way (companion roles, schema.sql,
the real migrate runner, rbac.sql): the shipped inspection workflow with an
active designer owning Draft, made an Implementation owner by
`with_designer_implementation`, with and without Router. Writes go through the
board's HTTP API as each role, the real `ticket-board-write`, or the service's
own connection where the database alone is the question; the real notify
listener delivers.

- **The designer stays the designer:** owns Draft, releases drafts, keeps its
  kind, presentation and onboarding.
- **And implements:** the Director assigns it Implementation work; it holds the
  serial slot, submits with a commit and without one, Audit's kick-back returns
  to it alone, and Audit's sign-off moves the work on.
- **Ordinary implementers and single-kind documents are unchanged.**
- **Router:** the designer's routing scope is on the ticket's `routing`; Router
  cannot give it unflagged work, by HTTP or in the database; the Director can.
- **Refused:** malformed or contradictory declarations, a draft role owning
  Implementation without declaring it, a scoped claimant under pull scheduling.

It runs on main, from a git archive in a child process, and on this tree.
"""

from __future__ import annotations

import ast
import copy
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = 0
A, B = "a" * 40, "b" * 40
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
FLAG = "design_work"


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def with_designer(cfg: dict) -> dict:
    """An active designer owning Draft, releasing drafts, with a pane."""
    cfg = copy.deepcopy(cfg)
    for role in cfg["roles"]:
        if role["name"] == "designer":
            role["active"] = True
            role["onboarding_prompt"] = "You are the designer. You own the design."
    for stage in cfg["stages"]:
        if stage["name"] == "draft":
            stage["owners"] = ["designer"]
    for tr in cfg["transitions"]:
        if tr["action"] == "release_draft":
            tr["actors"] = ["designer", "director"]
    cfg["reassign"] = {}
    return cfg


def by_hand(cfg: dict) -> dict:
    """What pie wrote by hand: the designer added to Implementation's owners and moves, with no declaration."""
    cfg = copy.deepcopy(cfg)
    impl = next(s for s in cfg["stages"] if s["kind"] == "implementation")
    owners = set(impl["owners"])
    impl["owners"].append("designer")
    for tr in cfg["transitions"]:
        if (tr["from"] == impl["name"] or tr["to"] == impl["name"]) and owners <= set(tr["actors"]):
            tr["actors"].append("designer")
    return cfg


def scenario(root: Path, prefix: str, before_root: Path | None = None) -> dict:
    for extra in (str(root / "tests"), str(root)):
        if extra in sys.path:
            sys.path.remove(extra)
        sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    from router_role_board_test import Board, build_board, example, fixture, temporary_cluster
    from scripts.ticket_board import workflow_config

    try:
        from scripts.ticket_board.designer_implementation import with_designer_implementation
    except ImportError:  # main has no such helper: build the same document by hand
        with_designer_implementation = None

    def short(answer: dict) -> str:
        if "ok" in answer:
            return "ok"
        text = answer.get("refused", "")
        try:
            text = str(ast.literal_eval(text)[2])
        except (ValueError, SyntaxError, IndexError, TypeError):
            pass
        return " ".join(text.split("\nCONTEXT:")[0].split())[:600]

    def designed(router: bool = False) -> dict:
        cfg = with_designer(example(False))
        if with_designer_implementation is not None:
            cfg = with_designer_implementation(cfg, routing_flag=FLAG if router else None)
        else:
            cfg = by_hand(cfg)
        if router:
            from scripts.ticket_board.router_setup import with_router
            cfg = with_router(cfg, runtime="claude")
        return cfg

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        boards: list = []

        def board(name: str, document: dict):
            b = Board(cluster, name, document)
            b.app._resolve_known_commit = lambda value: (value.lower(), [])
            boards.append(b)
            return b

        def delivered(b) -> list:
            b.busy.clear()
            return sorted(t for t, m in b.one_pass() if m != "/clear")

        def flags(b, ticket: str) -> dict:
            return json.loads(b.sql("SELECT json_build_object('audit', audit_signoff, 'user', user_signoff, "
                                    f"'commit', commit_hash, 'exempt', commit_exempt)::text FROM ticket_board.tickets WHERE id='{ticket}';"))

        def cli(b, role: str, *args: str) -> str:
            env = {"PATH": "/usr/bin:/bin", "HOME": str(cluster.root), "LANG": "C.UTF-8", "TICKET_BOARD_URL": b.base,
                   "TICKET_BOARD_WRITE_TOKEN": b.server.write_token, "TICKET_BOARD_CALLER_ROLE": role}
            done = subprocess.run([sys.executable, str(root / "scripts" / "ticket-board-write"), *args], env=env,
                                  cwd=str(cluster.root), capture_output=True, text=True, timeout=120, check=False)
            return "ok" if done.returncode == 0 else " ".join((done.stdout + done.stderr).split())[-600:]

        try:
            # --- The document: refused on main, valid here. ---
            try:
                document = designed()
                workflow_config.validate(copy.deepcopy(document))
                seen["document"] = "ok"
            except ValueError as refused:
                seen["document"] = str(refused)[:300]
                return seen
            designer = next(r for r in document["roles"] if r["name"] == "designer")
            seen["designer_role"] = {k: designer.get(k) for k in ("kind", "additional_kinds", "routing_scope",
                                                                    "onboarding_prompt", "presentation_label")}
            seen["designer_label"] = [workflow_config.role_presentation_label(designer),
                                      workflow_config.role_presentation_label(next(
                                          r_ for r_ in with_designer(example(False))["roles"] if r_["name"] == "designer"))]
            seen["draft_owners"] = next(s["owners"] for s in document["stages"] if s["name"] == "draft")
            seen["moves"] = sorted(t["action"] for t in document["transitions"] if "designer" in t["actors"])
            plain = example(False)
            seen["plain_unchanged"] = workflow_config.validate(copy.deepcopy(plain)) == workflow_config.validate(
                copy.deepcopy(workflow_config.validate(copy.deepcopy(plain))))

            try:
                b = board("board", document)
                seen["board"] = "ok"
            except Exception as refused:  # noqa: BLE001 -- a board that will not take the document is the answer
                seen["board"] = " ".join(str(refused).split())[:300]
                return seen
            delivered(b)

            # --- Still the designer: Draft and its release. ---
            fixture.seed_postgres_ticket(b.admin, "PGU-1", title="Design set", state="draft", assignee="designer")
            seen["release"] = [short(b.act("PGU-1", "release_draft", "designer")), b.where("PGU-1")]

            # --- And an implementer: assigned, reserved, told; a commit to Audit and back; then the sign-off. ---
            seen["assign"] = [short(b.act("PGU-1", "route", "director", state="in_progress", assignee="designer")),
                              b.where("PGU-1"), b.reservations().get("designer"), delivered(b)]
            seen["submit"] = [short(b.act("PGU-1", "submit_to_audit", "designer", commit_hash=A)), b.where("PGU-1"),
                              b.sql("SELECT last_implementer_assignee FROM ticket_board.ticket_notification_state "
                                    "WHERE ticket_id='PGU-1';"), delivered(b)]
            seen["status"] = b.ticket("PGU-1").get("audit_return")
            seen["kick"] = [cli(b, "audit", "audit-kick-back", "PGU-1", "--reason", "B1: no oracle.",
                                "--target-assignee", "designer"),
                            b.where("PGU-1"), b.reservations().get("designer"), delivered(b), flags(b, "PGU-1")]
            seen["resubmit"] = [short(b.act("PGU-1", "submit_to_audit", "designer", commit_hash=B)), b.where("PGU-1"),
                                short(b.act("PGU-1", "audit_sign_off", "audit", text="Accepted.")), b.where("PGU-1"),
                                b.reservations().get("designer")]
            b.act("PGU-1", "defer", "director")  # done with it: a review still holds its author's slot
            # ... and the same with no code.
            fixture.seed_postgres_ticket(b.admin, "PGU-2", title="Design decision", state="analysis", assignee="director")
            seen["nocode"] = [short(b.act("PGU-2", "route", "director", state="in_progress", assignee="designer")),
                              short(b.act("PGU-2", "submit_to_audit_without_commit", "designer", reason="A decision, no code.")),
                              b.where("PGU-2"),
                              short(b.act("PGU-2", "audit_kick_back", "audit", reason="Say why.")), b.where("PGU-2"),
                              delivered(b),
                              short(b.act("PGU-2", "submit_to_audit_without_commit", "designer", reason="Why, said.")),
                              short(b.act("PGU-2", "audit_sign_off", "audit", text="Accepted.")), b.where("PGU-2")]
            b.act("PGU-2", "defer", "director")

            # --- An ordinary implementer, alongside, is unchanged. ---
            fixture.seed_postgres_ticket(b.admin, "PGU-3", title="Parser", state="in_progress", assignee="app")
            seen["app"] = [short(b.act("PGU-3", "submit_to_audit", "app", commit_hash=A)),
                           short(b.act("PGU-3", "audit_kick_back", "audit", reason="Tests.")), b.where("PGU-3"),
                           delivered(b), b.reservations().get("app")]

            # --- The workflow takes the designer back off Implementation. ---
            fixture.seed_postgres_ticket(b.admin, "PGU-4", title="Held", state="analysis", assignee="director")
            b.act("PGU-4", "route", "director", state="in_progress", assignee="designer")
            revision = int(b.sql("SELECT revision FROM ticket_board.workflow_configuration WHERE singleton;"))
            off = with_designer(example(False))
            try:
                b.app.apply_workflow(off, expected_revision=revision, dry_run=False, caller_role="director")
                seen["off_while_held"] = "ok"
            except Exception as refused:  # noqa: BLE001 -- the refusal is the answer
                seen["off_while_held"] = " ".join(str(refused).split())[:300]
            for ticket in ("PGU-3", "PGU-4"):
                b.act(ticket, "defer", "director")
            revision = int(b.sql("SELECT revision FROM ticket_board.workflow_configuration WHERE singleton;"))
            b.app.apply_workflow(off, expected_revision=revision, dry_run=False, caller_role="director")
            fixture.seed_postgres_ticket(b.admin, "PGU-5", title="After", state="analysis", assignee="director")
            seen["off_assign"] = [short(b.act("PGU-5", "route", "director", state="in_progress", assignee="designer")),
                                  b.where("PGU-5")]
            fixture.seed_postgres_ticket(b.admin, "PGU-6", title="Still drafting", state="draft", assignee="designer")
            seen["off_draft"] = [short(b.act("PGU-6", "release_draft", "designer")), b.where("PGU-6")]

            # --- Router: the designer's scope. ---
            r = board("router", designed(router=True))
            delivered(r)
            for ticket in ("PGU-1", "PGU-2"):
                fixture.seed_postgres_ticket(r.admin, ticket, title=ticket, state="analysis", assignee="director")
                r.act(ticket, "route", "director", state="in_progress")
            routing = r.ticket("PGU-1").get("routing") or {}
            seen["routing"] = {"assignable": routing.get("assignable"), "scopes": routing.get("scopes")}
            answer = r.act("PGU-1", "router_assign", "router", assignee="designer", revision=str(routing.get("revision")))
            seen["router_unflagged"] = short(answer)
            try:  # the board refuses it itself, before the database is asked
                seen["router_unflagged_status"] = ast.literal_eval(answer.get("refused", ""))[1]
            except (ValueError, SyntaxError, IndexError, TypeError):
                seen["router_unflagged_status"] = None
            seen["router_unflagged_sql"] = r.as_router_sql(
                f"SELECT ticket_board.router_assign('PGU-1', 'designer', '{routing.get('revision')}');")
            seen["router_unflagged_where"] = r.where("PGU-1")
            seen["flag"] = short(r.act("PGU-1", "set_workflow_flags", "director", **{FLAG: True}))
            routing = r.ticket("PGU-1").get("routing") or {}
            seen["routing_flagged"] = {"assignable": routing.get("assignable"), "scopes": routing.get("scopes")}
            seen["router_flagged"] = [short(r.act("PGU-1", "router_assign", "router", assignee="designer",
                                                  revision=str(routing.get("revision")))), r.where("PGU-1")]
            routing = r.ticket("PGU-2").get("routing") or {}
            seen["router_other"] = [short(r.act("PGU-2", "router_assign", "router", assignee="app",
                                                revision=str(routing.get("revision")))), r.where("PGU-2")]
            fixture.seed_postgres_ticket(r.admin, "PGU-3", title="Unflagged", state="analysis", assignee="director")
            seen["director_direct"] = [short(r.act("PGU-3", "route", "director", state="in_progress", assignee="designer")),
                                       r.where("PGU-3")]

            # --- What the document may not say, in Python and in the database alike. ---
            import psycopg

            def database(function: str, doc: dict) -> str:
                """The stored-document validator's own answer, on the database alone."""
                try:
                    with psycopg.connect(r.admin, autocommit=True) as conn:
                        conn.execute(f"SELECT ticket_board.{function}(%s::jsonb)", (json.dumps(doc),))
                    return "accepted"
                except psycopg.Error as refused:
                    return str(refused).splitlines()[0][:200]

            def bad(change, router: bool = True) -> dict:
                doc = designed(router=router)
                roles = {x["name"]: x for x in doc["roles"]}
                change(doc, roles)
                try:
                    workflow_config.validate(copy.deepcopy(doc))
                    python = "accepted"
                except ValueError as refused:
                    python = str(refused)[:160]
                except Exception as broke:  # noqa: BLE001 -- a refusal must be the validator's, not a crash
                    python = f"crashed: {type(broke).__name__}: {broke}"[:160]
                return {"python": python, "database": database("validate_declared_workflow", doc)}

            cases = {
                "reviewer also implements": lambda d, r_: (r_["audit"].update(additional_kinds=["implementer"])),
                "implementer also implements": lambda d, r_: r_["main"].update(additional_kinds=["implementer"]),
                "designer also reviews": lambda d, r_: r_["designer"].update(additional_kinds=["reviewer"]),
                "empty": lambda d, r_: r_["designer"].update(additional_kinds=[]),
                "twice": lambda d, r_: r_["designer"].update(additional_kinds=["implementer", "implementer"]),
                "a string": lambda d, r_: r_["designer"].update(additional_kinds="implementer"),
                "undeclared flag": lambda d, r_: r_["designer"].update(routing_scope={"flag": "nowhere"}),
                "signoff flag": lambda d, r_: r_["designer"].update(routing_scope={"flag": "audit_signoff"}),
                "scope on a reviewer": lambda d, r_: r_["audit"].update(routing_scope={"flag": FLAG}),
                "scope with more": lambda d, r_: r_["designer"].update(routing_scope={"flag": FLAG, "only": True}),
                "inactive owner": lambda d, r_: r_["designer"].update(active=False, slot=None),
            }
            # Structured values where kinds or a flag name belong (Audit's B1: [{}] crashed workflow_config).
            for value in ({}, None, 1, [{}], [[]], [None], [1], ["implementer", {}], {"implementer": True}):
                cases[f"additional_kinds {json.dumps(value)}"] = (
                    lambda d, r_, v=value: r_["designer"].update(additional_kinds=copy.deepcopy(v)))
            for value in ({}, [], None, FLAG, {"flag": {}}, {"flag": []}, {"flag": None}, {"flag": 1}, {"flag": [FLAG]}):
                cases[f"routing_scope {json.dumps(value)}"] = (
                    lambda d, r_, v=value: r_["designer"].update(routing_scope=copy.deepcopy(v)))
            seen["bad"] = {name: bad(change) for name, change in cases.items()}
            seen["bad"]["implicit"] = bad(lambda d, r_: r_["designer"].pop("additional_kinds", None), router=False)

            # --- Pull scheduling: a claimant is ephemeral; a scoped one would claim anything. ---
            sys.path.insert(0, str(root / "tests"))
            from pull_scheduling_policy_test import pull_document
            from scripts.ticket_board.workflow_config import pull_claimant_pool

            def pulled(ephemeral: bool, scoped: bool) -> dict:
                doc = with_designer(pull_document())
                doc["project"] = "pgu"
                for role in doc["roles"]:
                    if role.get("target"):
                        role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
                if with_designer_implementation is None:
                    return {"python": "no helper"}
                doc = with_designer_implementation(doc)
                designer_ = next(x for x in doc["roles"] if x["name"] == "designer")
                designer_["ephemeral"] = ephemeral
                if scoped:
                    doc["flags"][FLAG] = {"kind": "gate", "default": False}
                    designer_["routing_scope"] = {"flag": FLAG}
                try:
                    python = pull_claimant_pool(workflow_config.validate(copy.deepcopy(doc)))
                except ValueError as refused:
                    python = str(refused)[:200]
                pool = r.sql("SELECT coalesce(json_agg(p), '[]')::text FROM ticket_board.pull_claimant_pool('"
                             + json.dumps(doc).replace("'", "''") + "'::jsonb) p;")
                return {"python": python, "database": json.loads(pool),
                        "scheduling": database("validate_declared_scheduling", doc)}

            seen["pull"] = {"persistent": pulled(False, False), "ephemeral": pulled(True, False),
                            "scoped": pulled(True, True)}

            # --- Fresh, migrated and upgraded boards install the same functions. ---
            shape = ("SELECT coalesce(json_agg(json_build_array(p.oid::regprocedure::text, md5(pg_get_functiondef(p.oid)), "
                     "pg_get_userbyid(p.proowner), coalesce(p.proacl::text, '')) ORDER BY p.oid::regprocedure::text), '[]')::text "
                     "FROM pg_proc p WHERE p.pronamespace = 'ticket_board'::regnamespace AND p.proname IN ("
                     "'role_has_kind', 'validate_declared_workflow', 'validate_declared_notification_roles', "
                     "'scheduling_claim_transitions', 'pull_claimant_pool', 'validate_declared_scheduling', "
                     "'router_assignable_roles', 'router_scope_refusal', 'router_assign', 'ticket_routing');")
            seen["migrated_shape"] = json.loads(b.sql(shape))
            fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
            fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
            fixture.psql(fresh, (root / "scripts/ticket_board/rbac.sql").read_text())
            seen["fresh_shape"] = json.loads(fixture.psql(fresh, shape).strip())
            if before_root is not None:
                u_admin = build_board(cluster, "upgraded", before=before_root)
                migrate_from = __import__("router_role_board_test").migrate
                migrate_from(u_admin)
                seen["upgraded_shape"] = json.loads(fixture.psql(u_admin, shape).strip())
                # The upgraded board takes the designer's document and runs the loop.
                u = Board.__new__(Board)
                u.admin = u_admin
                u.service = fixture.conninfo(cluster.socket_dir, cluster.port, "upgraded", fixture.SERVICE_ROLE)
                u.app = fixture.TicketBoardApp(cluster.root / "u-frames", cluster.root / "u-assets", project="pgu",
                                               ticket_prefix="PGU", database_url=u.service)
                u.app._resolve_known_commit = lambda value: (value.lower(), [])
                u.app.apply_workflow(designed(), expected_revision=0, dry_run=False, caller_role="director")
                import threading
                u.server = fixture.TicketBoardServer(("127.0.0.1", 0), u.app, director_notifier=fixture.QuietNotifier())
                threading.Thread(target=u.server.serve_forever, daemon=True).start()
                u.base = f"http://127.0.0.1:{u.server.server_port}"
                u.sleepers = []
                boards.append(u)
                fixture.seed_postgres_ticket(u_admin, "PGU-9", title="Upgraded", state="analysis", assignee="director")
                seen["upgraded_loop"] = [short(u.act("PGU-9", "route", "director", state="in_progress", assignee="designer")),
                                         short(u.act("PGU-9", "submit_to_audit", "designer", commit_hash=A)),
                                         short(u.act("PGU-9", "audit_kick_back", "audit", reason="Again.")),
                                         u.where("PGU-9")]
        finally:
            for each in boards:
                each.close()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples", "config"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def base_commit() -> str:
    """Main as it stood before this ticket: the commit before the one that added its migration."""
    found = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--",
                            "scripts/ticket_board/migrations/pgu1014_syrd678_designer_implementer.sql"],
                           check=True, capture_output=True, text=True).stdout.split()
    assert found, "pgu1014 is not committed; the run of main needs the commit that added it"
    return found[-1] + "^"


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd678b-")))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd678.") as tmp:
        before_root = tree_at(base_commit(), Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
        after = scenario(ROOT, "syrd678a-", before_root)

    check(before["document"] == "implementation owners must be implementers",
          f"main: the designer cannot own Implementation: {before['document']}")

    check(after["document"] == "ok" and after.get("board") == "ok",
          f"this tree takes the document, in workflow_config and on the board: {after['document']} {after.get('board')}")

    # The designer stays the designer.
    role = after["designer_role"]
    check(after["document"] == "ok" and role["kind"] == "draft" and role["additional_kinds"] == ["implementer"]
          and role["routing_scope"] is None and role["onboarding_prompt"] == "You are the designer. You own the design."
          and role["presentation_label"] is None and after["designer_label"] == ["Designer", "Designer"]
          and after["draft_owners"] == ["designer"],
          f"the helper keeps the designer's kind, onboarding and presentation, and its Draft: {role} "
          f"{after['designer_label']} {after['draft_owners']}")
    check(after["moves"] == ["release_draft", "submit_to_audit", "submit_to_audit_without_commit", "submit_to_inspection"],
          f"it joins every move its fellow owners take out of Implementation, and keeps releasing drafts: {after['moves']}")
    check(after["plain_unchanged"] is True, "a document that declares neither field validates to itself")
    check(after["release"] == ["ok", ["analysis", "director"]], f"it releases its draft: {after['release']}")

    # And implements.
    check(after["assign"] == ["ok", ["in_progress", "designer"], "PGU-1", ["pgu-designer:0.0"]],
          f"the Director assigns it Implementation work; it holds the slot and is told: {after['assign']}")
    check(after["submit"] == ["ok", ["audit", "audit"], "designer", ["pgu-audit:0.0"]],
          f"it submits a commit to Audit, and is remembered as the implementer: {after['submit']}")
    status = after["status"] or {}
    check((status.get("stage"), status.get("assignee"), status.get("valid")) == ("in_progress", "designer", True),
          f"as the producer of the revision Audit reviews (SYRD-673): {status}")
    check(after["kick"][:4] == ["ok", ["in_progress", "designer"], "PGU-1", ["pgu-designer:0.0"]]
          and after["kick"][4] == {"audit": False, "user": False, "commit": "", "exempt": False},
          f"Audit's kick-back returns it to the designer alone, slot and all: {after['kick']}")
    check(after["resubmit"][:4] == ["ok", ["audit", "audit"], "ok", ["director_review", "director"]]
          and after["resubmit"][4] == "PGU-1",
          f"resubmitted, Audit's sign-off moves it on to the Director's review, which holds its author as any "
          f"review does: {after['resubmit']}")
    check(after["nocode"] == ["ok", "ok", ["audit", "audit"], "ok", ["in_progress", "designer"], ["pgu-designer:0.0"],
                              "ok", "ok", ["director_review", "director"]],
          f"and the same without code: {after['nocode']}")
    check(after["app"] == ["ok", "ok", ["in_progress", "app"], ["pgu-app:0.0"], "PGU-3"],
          f"an ordinary implementer beside it is unchanged: {after['app']}")

    # Ownership removed.
    check("orphan live ticket ownership" in after["off_while_held"]
          and after["off_assign"] == ["ok", ["in_progress", "main"]]
          and after["off_draft"] == ["ok", ["analysis", "director"]],
          f"taken off Implementation, a route naming it lands on the stage's first owner as for any non-owner, and it "
          f"is still the designer: {after['off_while_held']} "
          f"{after['off_assign']} {after['off_draft']}")

    # Router.
    check(after["routing"] == {"assignable": ["app", "main", "ops"],
                               "scopes": {"designer": {"flag": FLAG, "eligible": False}}},
          f"Router sees the designer's scope, and an unflagged ticket is not assignable to it: {after['routing']}")
    told = f"router gives designer only work flagged {FLAG}, and PGU-1 is not"
    check(told in after["router_unflagged"] and told in after["router_unflagged_sql"].get("refused", "")
          and after["router_unflagged_status"] == 403 and after["router_unflagged_where"] == ["in_progress", "router"],
          f"Router cannot give it unflagged work, by HTTP or in the database: {after['router_unflagged']} "
          f"{after['router_unflagged_sql']}")
    check(after["flag"] == "ok" and after["routing_flagged"] == {
              "assignable": ["app", "designer", "main", "ops"], "scopes": {"designer": {"flag": FLAG, "eligible": True}}}
          and after["router_flagged"] == ["ok", ["in_progress", "designer"]],
          f"flagged by the Director, Router may: {after['routing_flagged']} {after['router_flagged']}")
    check(after["router_other"] == ["ok", ["in_progress", "app"]]
          and after["director_direct"] == ["ok", ["in_progress", "designer"]],
          f"other owners are unaffected, and the Director assigns the designer anything: {after['router_other']} "
          f"{after['director_direct']}")

    # What may not be declared.
    for name, outcome in after["bad"].items():
        check(outcome["python"] != "accepted" and not outcome["python"].startswith("crashed")
              and outcome["database"] != "accepted",
              f"{name}: refused by workflow_config, as a refusal, and by the database: {outcome}")
    check(after["bad"]["implicit"]["python"] == "implementation owners must be implementers"
          and "implementation owner must be implementer" in after["bad"]["implicit"]["database"],
          f"a draft role does not own Implementation unless it says so: {after['bad']['implicit']}")
    pull = after["pull"]
    check(pull["persistent"]["python"] == pull["persistent"]["database"] == ["app", "main", "ops"]
          and pull["ephemeral"]["python"] == pull["ephemeral"]["database"] == ["app", "designer", "main", "ops"]
          and "routing_scope" in pull["scoped"]["python"] and "routing_scope" in pull["scoped"]["scheduling"],
          f"under pull scheduling the designer claims only when ephemeral, and a scoped one may not: {pull}")

    check(after["upgraded_loop"] == ["ok", "ok", "ok", ["in_progress", "designer"]],
          f"upgraded from main, the board runs the loop: {after['upgraded_loop']}")
    names = sorted({row[0].split("(")[0] for row in after["migrated_shape"]})
    check(after["migrated_shape"] == after["fresh_shape"] == after["upgraded_shape"] and len(names) == 10,
          f"schema.sql and rbac.sql install what the migration does, fresh and upgraded from main: {names}")
    print(f"designer_implementer_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
