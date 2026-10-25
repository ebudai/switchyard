#!/usr/bin/env python3
"""SYRD-628: a provider cohort is scheduling capacity -- the board holds every claim to its ceiling.

MEFP runs an eight-worker pull pool split between Claude and Codex by hand, to
keep both providers' headroom. A cohort (`scheduling.cohorts`) is the claimant
pool's members of one runtime. The board refuses a claim that would put the
cohort past its ceiling -- through claim-next, the canonical claim action and
the listener's pickup alike -- while the other cohorts keep pulling. A quota
record (the Director's, or a provider signal's) lowers a ceiling for a while;
0 is exhausted.

The pool here is eight workers expanded by the real worker-pool code, started
imbalanced: two Claude, six Codex. Boards are built the production ways --
schema.sql, the real migration runner, and an upgrade from the release before
-- and driven over HTTP, as the listener, and with the real listener's pass.
Nothing here names MEFP's roles.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import psycopg  # noqa: E402

import ticket_board_write_api_test as fixture  # noqa: E402
from pull_pickup_test import Gate, proc_entry  # noqa: E402
from pull_scheduling_policy_test import pull_document  # noqa: E402
from scripts import team_launcher, worker_pool  # noqa: E402
from scripts.ticket_board import scheduling_cohorts, workflow_config  # noqa: E402
from scripts.ticket_board.notify_listener import PaneHookStateStore, TicketBoardNotifyListener  # noqa: E402
from scripts.ticket_board.peer_identity import SessionIdentity  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu1010_syrd628_provider_cohorts.sql"
FUNCTIONS = ("validate_declared_scheduling", "claim_ready_ticket", "lock_pull_assignment", "pull_cohort",
             "cohort_effective_ceiling", "cohort_holders", "cohort_claim_refusal", "require_declared_cohort",
             "set_cohort_quota", "clear_cohort_quota", "note_cohort_quota_signal", "record_cohort_change", "cohort_status",
             "begin_cohort_drain", "end_cohort_drain")
POOL = {"name": "luna", "runtime": "claude", "size": 8, "kind": "implementer", "ephemeral": True}
CODEX = [f"luna-{n}" for n in range(3, 9)]
CLAUDE = ["luna-1", "luna-2"]
COHORTS = [{"runtime": "claude", "target": 4, "ceiling": 4, "model": "claude-sonnet-5-5", "effort": "high"},
           {"runtime": "codex", "target": 4, "ceiling": 4, "model": "gpt-5.6-luna", "effort": "xhigh"}]


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def mixed_document(cohorts: list | None = None) -> dict:
    """Eight workers, two on Claude and six on Codex; main, app and ops are persistent and routed."""
    doc = pull_document(**({"cohorts": cohorts} if cohorts is not None else {}))
    doc["project"] = "pgu"
    for role in doc["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        if role["name"] in ("main", "app", "ops"):
            role["ephemeral"] = False
    doc, _ = worker_pool.expand_pool(doc, team_launcher.parse_worker_pool(POOL), project="pgu")
    next(t for t in doc["transitions"] if t["action"] == "claim")["actors"] = ["luna-1"]
    for role in doc["roles"]:
        if role["name"] in CODEX:
            role["runtime"] = "codex"
    return workflow_config.validate(doc)


def build(cluster, dbname: str, shape: str) -> str:
    admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
    fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    from schema_function_drift import migrations_from, rbac_before, schema_before
    if shape in ("upgraded", "before"):
        before = schema_before(MIGRATION)
        check("cohort_claim_refusal" not in before and "claim_ready_ticket" in before,
              "the before-schema is the previous release: pull, without cohorts")
        fixture.psql(admin, before)
    else:
        fixture.psql(admin, fixture.SCHEMA_PATH.read_text())
    fixture.create_roles(admin)
    fixture.psql(admin, rbac_before(MIGRATION) if shape in ("upgraded", "before") else fixture.RBAC_PATH.read_text())
    if shape == "migrated":
        result = subprocess.run(["bash", str(ROOT / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
        check(result.returncode == 0, f"every migration applies: {result.stderr[-1500:]}")
        fixture.psql(admin, fixture.RBAC_PATH.read_text())
    if shape == "upgraded":
        for migration in migrations_from(MIGRATION):
            fixture.psql(admin, migration.read_text())
            if migration == MIGRATION:
                fixture.psql(admin, MIGRATION.read_text())  # replayed: changes nothing
        fixture.psql(admin, fixture.RBAC_PATH.read_text())
    return admin


class Board:
    """One production-built board with the mixed pool applied, driven over HTTP and as the listener."""

    def __init__(self, cluster, dbname: str, shape: str, document: dict | None = None) -> None:
        self.label = shape
        self.admin = build(cluster, dbname, shape)
        self.app = fixture.TicketBoardApp(cluster.root / f"frames-{dbname}", cluster.root / f"assets-{dbname}",
                                          project="pgu", ticket_prefix="PGU",
                                          database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname,
                                                                        fixture.SERVICE_ROLE))
        self.server = fixture.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = self.server.write_token
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")
        self.document = document if document is not None else mixed_document(COHORTS)
        self.app.apply_workflow(self.document, expected_revision=0, dry_run=False, caller_role="director")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def sql(self, statement: str) -> str:
        return fixture.psql(self.admin, statement).strip()

    def act(self, ticket: str, operation: str, actor: str, expect: int = 200, **payload):
        return fixture.post_json(self.base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor,
                                 expect=expect)

    def board_op(self, operation: str, actor: str, expect: int = 200, **payload):
        return fixture.post_json(self.base, f"/api/tickets/actions/{operation}", payload, caller=actor, expect=expect)

    def claim_next(self, role: str, expect: int = 200):
        return self.board_op("claim_next", role, expect=expect)

    def where(self, ticket: str) -> str:
        return self.sql(f"SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id='{ticket}';")

    def admit(self, *tickets: str) -> None:
        for ticket in tickets:
            fixture.seed_postgres_ticket(self.admin, ticket, title=ticket, state="analysis", assignee="director",
                                         commit_exempt=True)
            self.act(ticket, "admit", "director")

    def release(self, ticket: str, author: str) -> None:
        """Through Audit's approval, which frees the author's serial slot."""
        self.act(ticket, "submit_to_inspection", author)
        if self.where(ticket).startswith("inspection/"):
            self.act(ticket, "inspector_sign_off", "inspector")
        self.act(ticket, "audit_sign_off", "audit", text="Audited.")

    def status(self) -> dict:
        with urllib.request.urlopen(self.base + "/api/cohorts", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def cohort(self, runtime: str) -> dict:
        return next(c for c in self.status()["cohorts"] if c["runtime"] == runtime)

    def events(self) -> list[str]:
        return self.sql("SELECT coalesce(string_agg(runtime || ':' || role || ':' || change || ':' || source || ':' || actor, ','"
                        " ORDER BY id), '') FROM ticket_board.cohort_events;").split(",")


def test_python_and_sql_agree_on_what_a_cohort_policy_may_say() -> None:
    def variant(edit) -> dict:
        doc = mixed_document(COHORTS)
        edit(doc["scheduling"])
        return doc

    cases = {
        "the declared cohorts": (variant(lambda p: None), ""),
        "not a list": (variant(lambda p: p.update(cohorts={"runtime": "claude"})), "scheduling cohorts must be a non-empty list"),
        "an empty list": (variant(lambda p: p.update(cohorts=[])), "scheduling cohorts must be a non-empty list"),
        "an unknown field": (variant(lambda p: p["cohorts"][0].update(share=0.5)), "invalid scheduling cohort"),
        "no runtime": (variant(lambda p: p["cohorts"][0].pop("runtime")), "invalid scheduling cohort"),
        "an unknown runtime": (variant(lambda p: p["cohorts"][0].update(runtime="gpt")),
                               "a scheduling cohort names a known runtime, and each runtime once"),
        "a runtime twice": (variant(lambda p: p["cohorts"][1].update(runtime="claude")),
                            "a scheduling cohort names a known runtime, and each runtime once"),
        "no target or ceiling": (variant(lambda p: [p["cohorts"][0].pop(k) for k in ("target", "ceiling")]),
                                 "a scheduling cohort needs a target or a ceiling"),
        "a target past 64": (variant(lambda p: p["cohorts"][0].update(target=65)), "scheduling cohort target must be an integer from 0 to 64"),
        "a ceiling in words": (variant(lambda p: p["cohorts"][1].update(ceiling="4")), "scheduling cohort ceiling must be an integer from 0 to 64"),
        "an empty model": (variant(lambda p: p["cohorts"][0].update(model="")), "scheduling cohort model must be a name of 1 to 120 characters"),
        "a padded effort": (variant(lambda p: p["cohorts"][1].update(effort=" xhigh")), "scheduling cohort effort must be a name of 1 to 120 characters"),
    }
    with temporary_cluster(prefix="syrd628-validate.", shutdown="immediate") as cluster:
        admin = build(cluster, "validate", "fresh")
        for name, (doc, expected) in cases.items():
            try:
                workflow_config.validate(doc)
                python = ""
            except ValueError as exc:
                python = str(exc)
            with psycopg.connect(admin, autocommit=True) as conn:
                try:
                    conn.execute("SELECT ticket_board.validate_declared_scheduling(%s::jsonb)", (json.dumps(doc),))
                    sql = ""
                except psycopg.Error as exc:
                    sql = exc.diag.message_primary or str(exc)
            check(expected in python and (expected == "") == (python == "") and sql == python,
                  f"{name}: Python and SQL give the same answer: python={python!r} sql={sql!r}")
    doc = mixed_document(COHORTS)
    check([scheduling_cohorts.cohort_of(doc, r)["runtime"] for r in ("luna-1", "luna-3")] == ["claude", "codex"]
          and scheduling_cohorts.cohort_of(doc, "main") is None and scheduling_cohorts.cohort_of(doc, "director") is None,
          "a claimant's cohort is its declared runtime's; a persistent implementer or the Director has none")


def test_main_cannot_say_it_and_does_not_hold_claims_back() -> None:
    """The release before: the policy is refused, and without it every running Codex worker claims."""
    with temporary_cluster(prefix="syrd628-before.", shutdown="immediate") as cluster:
        board = Board(cluster, "before", "before", document=mixed_document())
        try:
            with psycopg.connect(board.admin, autocommit=True) as conn:
                try:
                    conn.execute("SELECT ticket_board.validate_declared_scheduling(%s::jsonb)",
                                 (json.dumps(mixed_document(COHORTS)),))
                    refused = ""
                except psycopg.Error as exc:
                    refused = exc.diag.message_primary or ""
            check(refused == "invalid scheduling policy", f"before: a cohort policy is refused outright: {refused!r}")
            board.admit(*[f"PGU-{n}" for n in range(1, 7)])
            got = [board.claim_next(role).get("claimed") for role in CODEX]
            check(got == [True] * 6, f"before: six Codex workers hold six tickets; nothing holds a provider back: {got}")
        finally:
            board.close()


def test_the_ceiling_holds_on_every_claim_path_on_every_board_shape() -> None:
    for shape in ("fresh", "migrated", "upgraded"):
        with temporary_cluster(prefix="syrd628-board.", shutdown="immediate") as cluster:
            board = Board(cluster, f"cohort_{shape}", shape)
            label = shape
            try:
                board.admit(*[f"PGU-{n}" for n in range(1, 10)])
                got = [board.claim_next(role) for role in CODEX[:4]]
                check([g.get("claimed") for g in got] == [True] * 4,
                      f"{label}: four Codex workers claim up to the cohort's ceiling: {got}")
                refused = board.claim_next(CODEX[4])
                check(refused == {"claimed": False, "ticket": None, "reason": "ceiling",
                                  "detail": "the codex cohort holds 4 ticket(s), its ceiling is 4"},
                      f"{label}: the fifth Codex claim is refused with the ceiling, not as idle: {refused}")
                ready = board.sql("SELECT id FROM ticket_board.tickets WHERE state='ready' ORDER BY ticket_number LIMIT 1;")
                denied = str(board.act(ready, "claim", CODEX[5], expect=400))  # the database's refusal, as for claim-next
                check(board.where(ready) == "ready/unassigned" and "codex cohort holds 4" in denied,
                      f"{label}: the canonical claim action is refused the same way: {denied}")
                claude = [board.claim_next(role) for role in CLAUDE]
                check([c.get("claimed") for c in claude] == [True, True],
                      f"{label}: the Claude cohort keeps pulling while Codex is at its ceiling: {claude}")
                status = board.cohort("codex")
                check(status["effective_ceiling"] == 4 and len(status["holding"]) == 4 and status["members"] == CODEX
                      and status["telemetry"] == "static" and status["model"] == "gpt-5.6-luna",
                      f"{label}: status shows the policy, the members and who holds what: {status}")
                # Draining: Audit's approval frees a Codex worker's slot, and one more Codex claim fits.
                first = got[0]["ticket"]
                board.release(first, CODEX[0])
                again = board.claim_next(CODEX[4])
                check(again.get("claimed") is True, f"{label}: a released slot is claimable by the cohort again: {again}")
                claims = board.sql("SELECT count(*) || '/' || count(DISTINCT ticket_id) FROM ticket_board.pull_claims;")
                check(claims == "7/7", f"{label}: seven claims, seven tickets -- none claimed twice: {claims}")
            finally:
                board.close()


def test_concurrent_claims_never_pass_the_ceiling_or_share_a_ticket() -> None:
    with temporary_cluster(prefix="syrd628-race.", shutdown="immediate") as cluster:
        board = Board(cluster, "race", "fresh")
        try:
            board.board_op("set_cohort_quota", "director", runtime="codex", ceiling=2, reason="half the Codex headroom")
            board.admit(*[f"PGU-{n}" for n in range(1, 11)])
            results: dict[str, dict] = {}
            barrier = threading.Barrier(len(CODEX) + len(CLAUDE))

            def claim(role: str) -> None:
                barrier.wait()
                results[role] = board.claim_next(role)

            threads = [threading.Thread(target=claim, args=(role,)) for role in CODEX + CLAUDE]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
            codex = [results[r] for r in CODEX]
            won = [r["ticket"] for r in codex if r.get("claimed")]
            check(len(won) == 2 and all(r.get("reason") == "ceiling" for r in codex if not r.get("claimed")),
                  f"six Codex workers at once: exactly two claim, the rest are refused at the ceiling: {codex}")
            tickets = [r["ticket"] for r in results.values() if r.get("claimed")]
            check(len(tickets) == 4 and len(set(tickets)) == 4 and all(results[r].get("claimed") for r in CLAUDE),
                  f"the Claude workers claim alongside -- not 'nothing ready' with work waiting -- and no ticket is "
                  f"claimed twice: {results}")
        finally:
            board.close()


def test_quota_exhaustion_and_recovery_are_recorded_and_the_other_cohort_carries_on() -> None:
    with temporary_cluster(prefix="syrd628-quota.", shutdown="immediate") as cluster:
        board = Board(cluster, "quota", "fresh")
        try:
            board.admit(*[f"PGU-{n}" for n in range(1, 7)])
            denied = str(board.board_op("set_cohort_quota", "app", expect=403, runtime="codex", ceiling=0, reason="x"))
            check("director" in denied.lower() or "cannot" in denied, f"only the Director records quota: {denied}")
            board.board_op("set_cohort_quota", "director", runtime="codex", ceiling=0, reason="Codex usage exhausted")
            exhausted = board.claim_next(CODEX[0])
            claude = board.claim_next(CLAUDE[0])
            check(exhausted.get("reason") == "ceiling" and "ceiling is 0" in exhausted.get("detail", "")
                  and claude.get("claimed") is True,
                  f"exhausted: no Codex claim, while Claude still claims: {exhausted} {claude}")
            status = board.cohort("codex")
            check(status["effective_ceiling"] == 0 and status["quota"][0]["source"] == "operator"
                  and status["quota"][0]["reason"] == "Codex usage exhausted",
                  f"status names the limit, its source and its reason: {status}")
            board.board_op("clear_cohort_quota", "director", runtime="codex", reason="usage window reset")
            recovered = board.claim_next(CODEX[0])
            check(recovered.get("claimed") is True, f"recovered: Codex claims again: {recovered}")
            # A limit that expires ends by itself.
            board.board_op("set_cohort_quota", "director", runtime="codex", ceiling=0, reason="paused", expires_minutes=5)
            paused = board.claim_next(CODEX[1])
            board.sql("UPDATE ticket_board.cohort_quota SET expires_at = clock_timestamp() - interval '1 second';")
            resumed = board.claim_next(CODEX[1])
            check(paused.get("reason") == "ceiling" and resumed.get("claimed") is True,
                  f"an expired limit holds nothing back: {paused} {resumed}")
            # A provider signal is a renewable record under its own name; only a change is an event.
            with psycopg.connect(board.listener_url, autocommit=True) as conn:
                said = [conn.execute("SELECT ticket_board.note_cohort_quota_signal('codex', 'codex-usage', 1, '90% used', 600)").fetchone()[0]
                        for _ in range(3)]
            check(said == [True, False, False] and board.cohort("codex")["telemetry"] == "signal"
                  and board.cohort("codex")["effective_ceiling"] == 1,
                  f"a signal lowers the ceiling once, and renewing it is not news: {said} {board.cohort('codex')}")
            with psycopg.connect(fixture.conninfo(cluster.socket_dir, cluster.port, "quota", fixture.SERVICE_ROLE)) as conn:
                try:
                    conn.execute("SELECT ticket_board.note_cohort_quota_signal('codex', 'codex-usage', 0, 'forged', 600)")
                    forged = "accepted"
                except psycopg.Error as exc:
                    forged = str(exc)
            check(forged != "accepted", f"only the listener sends a provider signal: {forged}")
            check(board.events() == ["codex::quota_set:operator:director", "codex::quota_cleared:operator:director",
                                     "codex::quota_set:operator:director", "codex::signal:listener:codex-usage"],
                  f"every quota change is recorded with its source and actor: {board.events()}")
        finally:
            board.close()


def test_convergence_records_are_the_directors_and_name_a_member() -> None:
    with temporary_cluster(prefix="syrd628-record.", shutdown="immediate") as cluster:
        board = Board(cluster, "record", "fresh")
        try:
            ok = board.board_op("record_cohort_change", "director", runtime="codex", role="luna-8", kind="stop",
                                source="auto", reason="codex is 2 over target; luna-8 is idle and holds nothing",
                                detail={"phase": "intended"})
            stranger = str(board.board_op("record_cohort_change", "director", expect=400, runtime="codex", role="luna-1",
                                          kind="stop", source="auto", reason="wrong cohort"))
            persistent = str(board.board_op("record_cohort_change", "director", expect=400, runtime="codex", role="main",
                                            kind="stop", source="auto", reason="not a claimant"))
            other = str(board.board_op("record_cohort_change", "app", expect=403, runtime="codex", role="luna-8",
                                       kind="stop", source="auto", reason="not the Director"))
            check(isinstance(ok.get("result"), int) and "not a member of the codex cohort" in stranger
                  and "role main is not a member of the codex cohort" in persistent and "app" in other,
                  f"the Director records a change to a member; a non-member or another role is refused: {ok} {stranger} {other}")
            check(board.events() == ["codex:luna-8:stop:auto:director"]
                  and board.status()["events"][0]["detail"] == {"phase": "intended"},
                  f"the record keeps who, what, why and its detail: {board.status()['events']}")
            read = subprocess.run([sys.executable, str(ROOT / "scripts/ticket-board-read"), "--board-url", board.base,
                                   "cohorts"], capture_output=True, text=True, timeout=60, cwd=ROOT)
            check(read.returncode == 0 and "codex: target 4, ceiling 4, now 4 (model=gpt-5.6-luna effort=xhigh)" in read.stdout
                  and "usage telemetry: none; static limits only" in read.stdout and "luna-8 stop (auto, director)" in read.stdout,
                  f"ticket-board-read cohorts says it plainly: {read.stdout}{read.stderr}")
        finally:
            board.close()


def test_the_listener_pulls_for_the_open_cohort_and_never_calls_the_capped_one_idle() -> None:
    with temporary_cluster(prefix="syrd628-pickup.", shutdown="immediate") as cluster:
        board = Board(cluster, "pickup", "fresh")
        tmp = Path(tempfile.mkdtemp(prefix="syrd628-pickup."))
        try:
            board.board_op("set_cohort_quota", "director", runtime="codex", ceiling=0, reason="Codex exhausted")
            targets = {role["name"]: role["target"] for role in board.document["roles"] if role.get("target")}
            board.admit("PGU-1", "PGU-2", "PGU-3", "PGU-4")
            proc = tmp / "proc"
            store = PaneHookStateStore(tmp / "state")
            gate = Gate(state_store=store)
            gate.proc_root, gate.role_targets, gate.busy = proc, dict(targets), set()
            gate.role_identities = {}
            for n, role in enumerate(CLAUDE + CODEX, start=1):
                proc_entry(proc, 7200 + n, 9200 + n)
                gate.role_identities[role] = SessionIdentity(7200 + n, 9200 + n)
                store.write(targets[role], "idle", source="claude.Stop")
            sent: list = []
            listener = TicketBoardNotifyListener(
                conninfo=board.listener_url, sender=lambda target, message: sent.append((target, message)),
                activity_gate=gate.is_working, target_exists=lambda _t: True, submission_witness=lambda *_a: True,
                poll_seconds=0, logger=logging.getLogger("syrd628"))
            listener.role_targets = dict(targets)
            listener.listen_once(max_notifications=20)
            claimed = board.sql("SELECT string_agg(role, ',' ORDER BY role) FROM ticket_board.pull_claims;")
            check(claimed == "luna-1,luna-2", f"one pass claims for the two Claude workers and no Codex one: {claimed}")
            board.sql("UPDATE ticket_board.ticket_notification_state SET entered_current_state_at = "
                      "clock_timestamp() - interval '20 minutes' WHERE ticket_id IN ('PGU-3', 'PGU-4');")
            sent.clear()
            listener.listen_once(max_notifications=20)
            director = [m for t, m in sent if t == targets["director"]]
            check(not any(name in m for m in director for name in CODEX),
                  f"waiting work does not call the capped Codex workers idle: {director}")
            board.board_op("clear_cohort_quota", "director", runtime="codex", reason="Codex recovered")
            listener.listen_once(max_notifications=20)
            claimed = board.sql("SELECT count(*) FROM ticket_board.pull_claims WHERE role LIKE 'luna-%' AND role NOT IN ('luna-1', 'luna-2');")
            check(claimed == "2", f"once Codex recovers, the next pass gives it the waiting work: {claimed}")
        finally:
            board.close()
            shutil.rmtree(tmp, ignore_errors=True)


def test_zz_schema_and_migration_carry_the_same_functions() -> None:
    """Last, so a planted fault in one copy is caught by what it does before it is caught by the copy check."""
    from schema_function_drift import assert_no_drift
    assert_no_drift(*FUNCTIONS)
    check(True, "schema.sql and the migration carry the same functions")


def main() -> int:
    only = set(sys.argv[1:])  # named cases only, e.g. for a mutant that one case covers
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case) and (not only or name in only):
            case()
    print(f"provider_cohort_claims_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
