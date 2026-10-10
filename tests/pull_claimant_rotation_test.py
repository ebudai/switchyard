#!/usr/bin/env python3
"""SYRD-573: after a rotation, the running replacements claim, and the stopped workers are not called idle.

MEFP (2026-10-07) enabled pull with its claim transition naming the eight
workers then running, later stopped two of them and started two other declared
workers. The list was not touched: the replacements were refused ("role ...
cannot claim ready work") and a stopped worker with no provider process was
reported as an idle claimant. Who may claim is now read from the role
declarations -- every active, ephemeral implementer that owns the
implementation stage -- by every path: claim-next, the canonical action, the
listener's pickup, the idle notice and worker-pool status.

The same rotation is built here on a bench expanded by the real worker-pool
code, with the claim list frozen at the first two workers, and run on boards
built the way production builds them (schema.sql, the real migration runner, an
upgrade from the previous release), with ready work and Audit-held reservations
in flight. Nothing here names MEFP's roles.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import psycopg  # noqa: E402

import ticket_board_write_api_test as fixture  # noqa: E402
from pull_pickup_test import Gate, proc_entry  # noqa: E402
from pull_scheduling_policy_test import pull_document  # noqa: E402
from scripts import team_launcher, worker_pool  # noqa: E402
from scripts.ticket_board import workflow_config  # noqa: E402
from scripts.ticket_board.notify_listener import PaneHookStateStore, TicketBoardNotifyListener  # noqa: E402
from scripts.ticket_board.peer_identity import SessionIdentity  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu982_syrd573_pull_claimant_pool.sql"
FUNCTIONS = ("pull_claimant_pool", "transition_allows_actor", "enforce_declared_ticket_update",
             "perform_workflow_action_as", "lock_pull_assignment", "claim_ready_ticket", "notify_pull_idle_capacity")
BENCH = {"name": "bench", "runtime": "claude", "size": 4, "kind": "implementer", "ephemeral": True}


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def rotated_document() -> dict:
    """A bench of four whose claim list still names the two that ran when pull was enabled.

    main and ops are persistent implementers (not ephemeral), routed by the
    Director, as a tenant's long-lived roles are; app and the bench pull.
    """
    doc = pull_document()
    doc["project"] = "pgu"
    for role in doc["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        if role["name"] in ("main", "ops"):
            role["ephemeral"] = False
    doc, _ = worker_pool.expand_pool(doc, team_launcher.parse_worker_pool(BENCH), project="pgu")
    next(t for t in doc["transitions"] if t["action"] == "claim")["actors"] = ["app", "bench-1", "bench-2"]
    for role in doc["roles"]:
        if role["name"] in ("bench-3", "bench-4"):
            role["runtime"] = "codex"  # rotated onto another provider, as MEFP's replacements were
    return workflow_config.validate(doc)


def build(cluster, dbname: str, shape: str) -> str:
    admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
    fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    from schema_function_drift import migrations_from, rbac_before, schema_before
    if shape == "upgraded":
        before = schema_before(MIGRATION)
        check("pull_claimant_pool" not in before and "claim_ready_ticket" in before,
              "the before-schema is the previous release: pull, without the claimant pool")
        fixture.psql(admin, before)
    else:
        fixture.psql(admin, fixture.SCHEMA_PATH.read_text())
    fixture.create_roles(admin)
    fixture.psql(admin, rbac_before(MIGRATION) if shape == "upgraded" else fixture.RBAC_PATH.read_text())
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
    """One production-built board with the rotated document applied, driven over HTTP and as the listener."""

    def __init__(self, cluster, dbname: str, shape: str) -> None:
        self.label = shape
        self.admin = build(cluster, dbname, shape)
        self.dbname = dbname
        self.cluster = cluster
        self.app = fixture.TicketBoardApp(cluster.root / f"frames-{dbname}", cluster.root / f"assets-{dbname}",
                                          project="pgu", ticket_prefix="PGU",
                                          database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname,
                                                                        fixture.SERVICE_ROLE))
        self.server = fixture.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = self.server.write_token
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")
        self.document = rotated_document()
        self.app.apply_workflow(self.document, expected_revision=0, dry_run=False, caller_role="director")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def sql(self, statement: str) -> str:
        return fixture.psql(self.admin, statement).strip()

    def act(self, ticket: str, operation: str, actor: str, expect: int = 200, **payload):
        return fixture.post_json(self.base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor,
                                 expect=expect)

    def claim_next(self, role: str, expect: int = 200):
        return fixture.post_json(self.base, "/api/tickets/actions/claim_next", {}, caller=role, expect=expect)

    def where(self, ticket: str) -> str:
        return self.sql(f"SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id='{ticket}';")

    def admit(self, *tickets: str) -> None:
        for ticket in tickets:
            fixture.seed_postgres_ticket(self.admin, ticket, title=ticket, state="analysis", assignee="director",
                                         commit_exempt=True)
            self.act(ticket, "admit", "director")

    def to_audit(self, ticket: str, author: str) -> None:
        self.act(ticket, "submit_to_inspection", author)
        if self.where(ticket).startswith("inspection/"):
            self.act(ticket, "inspector_sign_off", "inspector")
        check(self.where(ticket).startswith("audit/"), f"{self.label}: {ticket} reached Audit: {self.where(ticket)}")


def test_the_claimant_rule_is_one_rule_in_python_and_sql() -> None:
    """The board's pull_claimant_pool / transition_allows_actor and workflow_config's agree, clause by clause."""
    base = rotated_document()
    claim = next(t for t in base["transitions"] if t["action"] == "claim")
    start = next(t for t in base["transitions"] if t["action"] == "director_start")

    def variant(change) -> dict:
        doc = json.loads(json.dumps(base))
        change(doc)
        return doc

    def role(doc: dict, name: str) -> dict:
        return next(r for r in doc["roles"] if r["name"] == name)

    cases = {
        "rotated": (base, ["app", "bench-1", "bench-2", "bench-3", "bench-4"]),
        "no pull policy": (variant(lambda d: d.pop("scheduling")), []),
        "a retired worker": (variant(lambda d: role(d, "bench-4").update(active=False)),
                             ["app", "bench-1", "bench-2", "bench-3"]),
        "a persistent worker": (variant(lambda d: role(d, "bench-3").update(ephemeral=False)),
                                ["app", "bench-1", "bench-2", "bench-4"]),
        "ephemeral absent": (variant(lambda d: role(d, "bench-3").pop("ephemeral")), ["app", "bench-1", "bench-2", "bench-4"]),
        "not an implementer": (variant(lambda d: role(d, "bench-2").update(kind="reviewer")),
                               ["app", "bench-1", "bench-3", "bench-4"]),
        "owns no implementation": (variant(lambda d: [s["owners"].remove("bench-1") for s in d["stages"]
                                                      if "bench-1" in s["owners"]]),
                                   ["app", "bench-2", "bench-3", "bench-4"]),
    }
    with temporary_cluster(prefix="syrd573-rule.", shutdown="immediate") as cluster:
        admin = build(cluster, "rule", "fresh")
        with psycopg.connect(admin, autocommit=True) as conn:
            for label, (doc, expected) in cases.items():
                python = workflow_config.pull_claimant_pool(doc)
                board = conn.execute("SELECT coalesce(json_agg(n), '[]') FROM ticket_board.pull_claimant_pool(%s::jsonb) n",
                                     (json.dumps(doc),)).fetchone()[0]
                check(python == expected and board == expected, f"{label}: python {python}, board {board}, want {expected}")
                for tr in (claim, start):
                    for actor in ("app", "bench-3", "main", "ops", "director", "bench-9"):
                        mine = workflow_config.transition_allows_actor(doc, tr, actor)
                        theirs = conn.execute("SELECT ticket_board.transition_allows_actor(%s::jsonb, %s::jsonb, %s)",
                                              (json.dumps(doc), json.dumps(tr), actor)).fetchone()[0]
                        check(mine is theirs, f"{label}: {tr['action']} by {actor}: python {mine}, board {theirs}")
    check(workflow_config.transition_allows_actor(base, claim, "bench-3")
          and not workflow_config.transition_allows_actor(base, claim, "ops")
          and not workflow_config.transition_allows_actor(base, start, "bench-3")
          and workflow_config.transition_allows_actor(base, start, "director"),
          "a replacement may take the claim and nothing else it was not given; a persistent implementer may not claim")
    from scripts.ticket_board.pull_queue import context_restore
    check(sorted(context_restore(base)) == ["app", "bench-1", "bench-2", "bench-3", "bench-4"],
          f"the queue's per-claimant rework report covers the same pool: {sorted(context_restore(base))}")


def test_a_rotated_bench_claims_through_every_path_on_every_board_shape() -> None:
    for shape in ("fresh", "migrated", "upgraded"):
        with temporary_cluster(prefix="syrd573-board.", shutdown="immediate") as cluster:
            board = Board(cluster, f"rot_{shape}", shape)
            label = shape
            try:
                board.admit("PGU-1", "PGU-2", "PGU-3", "PGU-4")
                # claim-next from a replacement the frozen list omits: claimed, as itself.
                got = board.claim_next("bench-3")
                check(got.get("claimed") is True and got.get("ticket") == "PGU-1" and board.where("PGU-1") == "in_progress/bench-3",
                      f"{label}: a running replacement claims with claim-next: {got} {board.where('PGU-1')}")
                # The canonical claim action, by the other replacement: lands on it, not on the stage's default owner.
                board.act("PGU-2", "claim", "bench-4")
                check(board.where("PGU-2") == "in_progress/bench-4",
                      f"{label}: the canonical claim by a replacement lands on it: {board.where('PGU-2')}")
                check(board.sql("SELECT string_agg(role || ':' || via, ',' ORDER BY id) FROM ticket_board.pull_claims;")
                      == "bench-3:self", f"{label}: claim-next is recorded as the worker's own")
                # A persistent implementer, routed by the Director, still cannot pull: by either path.
                refused = str(board.claim_next("ops", expect=400))
                check("ops cannot claim ready work" in refused, f"{label}: a persistent implementer cannot claim-next: {refused}")
                refused = str(board.act("PGU-3", "claim", "ops", expect=403))
                check(board.where("PGU-3") == "ready/unassigned", f"{label}: nor take the claim action: {refused}")
                # Serial reservation: one ticket per worker, and an Audit-held one holds until Audit approves it.
                again = board.claim_next("bench-3")
                check(again == {"claimed": False, "ticket": "PGU-1", "reason": "holding"},
                      f"{label}: a worker holding work claims nothing more: {again}")
                board.to_audit("PGU-1", "bench-3")
                held = board.claim_next("bench-3")
                check(held == {"claimed": False, "ticket": "PGU-1", "reason": "holding"} and board.where("PGU-3") == "ready/unassigned",
                      f"{label}: submitted to Audit, it still holds its author: {held}")
                board.act("PGU-1", "audit_sign_off", "audit", text="Audited.")
                freed = board.claim_next("bench-3")
                check(freed.get("claimed") is True and freed.get("ticket") == "PGU-3",
                      f"{label}: Audit's approval frees it, and it claims the next: {freed}")
                # The idle notice believes only declared claimants, and only free ones.
                board.sql("UPDATE ticket_board.ticket_notification_state SET entered_current_state_at = "
                          "clock_timestamp() - interval '20 minutes' WHERE ticket_id='PGU-4';")
                idle = {"ops": "idle, nothing claimable", "bench-2": "no hook state: readiness unknown",
                        "bench-4": "idle, nothing claimable", "ghost": "idle, nothing claimable"}
                with psycopg.connect(board.listener_url, autocommit=True) as conn:
                    sent = conn.execute("SELECT ticket_board.notify_pull_idle_capacity(%s::jsonb, clock_timestamp())",
                                        (json.dumps(idle),)).fetchone()[0]
                notice = board.sql("SELECT payload FROM ticket_board.ticket_notification_queue WHERE ticket_id='PGU-4' "
                                   "AND target_role='director' ORDER BY id DESC LIMIT 1;")
                check(sent == 1 and json.loads(notice)["idle"] == {"bench-2": "no hook state: readiness unknown"},
                      f"{label}: only a free declared claimant is named -- not a persistent role, a busy worker or an unknown name: {notice}")
            finally:
                board.close()


def test_the_listener_claims_for_running_replacements_and_never_names_the_stopped() -> None:
    with temporary_cluster(prefix="syrd573-pickup.", shutdown="immediate") as cluster:
        board = Board(cluster, "pickup", "fresh")
        tmp = Path(tempfile.mkdtemp(prefix="syrd573-pickup."))
        try:
            targets = {role["name"]: role["target"] for role in board.document["roles"] if role.get("target")}
            board.admit("PGU-1", "PGU-2", "PGU-3")
            proc = tmp / "proc"
            store = PaneHookStateStore(tmp / "state")
            gate = Gate(state_store=store)
            gate.proc_root, gate.role_targets, gate.busy = proc, dict(targets), set()
            # Running: the two replacements and main (persistent). Of the frozen list, bench-1 and
            # bench-2 have no registered provider process, and app's registered one has exited.
            gate.role_identities = {"app": SessionIdentity(7199, 9099)}
            for n, role in enumerate(("bench-3", "bench-4", "main"), start=1):
                proc_entry(proc, 7100 + n, 9000 + n)
                gate.role_identities[role] = SessionIdentity(7100 + n, 9000 + n)
                store.write(targets[role], "idle", source="claude.Stop")
            sent: list = []
            listener = TicketBoardNotifyListener(
                conninfo=board.listener_url, sender=lambda target, message: sent.append((target, message)),
                activity_gate=gate.is_working, target_exists=lambda _t: True, submission_witness=lambda *_a: True,
                poll_seconds=0, logger=__import__("logging").getLogger("syrd573"))
            listener.role_targets = dict(targets)
            listener.listen_once(max_notifications=10)
            claimed = board.sql("SELECT string_agg(ticket_id || ':' || role || ':' || via, ',' ORDER BY ticket_id) FROM ticket_board.pull_claims;")
            check(claimed == "PGU-1:bench-3:listener,PGU-2:bench-4:listener" and board.where("PGU-3") == "ready/unassigned",
                  f"one pass claims for each running replacement, and no more than run: {claimed}; PGU-3 {board.where('PGU-3')}")
            # PGU-3 waits past the interval: every running claimant is busy, and the stopped ones are not capacity.
            board.sql("UPDATE ticket_board.ticket_notification_state SET entered_current_state_at = "
                      "clock_timestamp() - interval '20 minutes' WHERE ticket_id='PGU-3';")
            sent.clear()
            listener.listen_once(max_notifications=10)
            director = [m for t, m in sent if t == targets["director"]]
            minted = ("SELECT count(*) FROM ticket_board.notification_trace WHERE event='enqueue' "
                      "AND detail->>'dedupe_key' LIKE 'pull-idle:PGU-3:%';")
            check(not director and board.sql(minted) == "0" and board.where("PGU-3") == "ready/unassigned",
                  f"no notice calls a stopped worker idle, and a persistent one never claims: {director}")
            # A worker that IS running but whose readiness is unknown is still named, as unknown.
            proc_entry(proc, 7110, 9010)
            gate.role_identities["bench-2"] = SessionIdentity(7110, 9010)
            listener.listen_once(max_notifications=10)
            listener.listen_once(max_notifications=10)
            director = [m for t, m in sent if t == targets["director"]]
            check(len(director) == 1 and "bench-2 (no hook state: readiness unknown)" in director[0]
                  and not any(name in director[0].split("claiming it:")[1] for name in ("app", "bench-1", "main"))
                  and board.sql(minted) == "1",
                  f"once, naming the running worker with unknown readiness and no stopped one: {director}")
        finally:
            board.close()
            shutil.rmtree(tmp, ignore_errors=True)


def test_worker_pool_status_judges_each_worker_by_its_own_runtime_and_says_who_claims() -> None:
    import worker_pool_lifecycle_test as lifecycle
    from scripts.ticket_board import board_skill

    pool = team_launcher.parse_worker_pool(BENCH)
    asked: list[tuple[str, str]] = []
    launcher = sys.modules["scripts.team_launcher"]
    saved = (launcher._cli_auth_status, launcher._workdir_is_trusted, board_skill.verify_board_skill,
             launcher._owner_home_for_auth)
    launcher._cli_auth_status = lambda runtime, **_kw: (asked.append(("auth", runtime)),
                                                        "authenticated" if runtime == "codex" else "unauthenticated")[1]
    launcher._workdir_is_trusted = lambda runtime, **_kw: (asked.append(("trust", runtime)), runtime == "codex")[1]
    board_skill.verify_board_skill = lambda **_kw: [SimpleNamespace(runtime="codex", action="present"),
                                                    SimpleNamespace(runtime="claude", action="missing")]

    def runner(args, **_kwargs):
        command = [str(part) for part in args]
        if "has-session" in command:
            return subprocess.CompletedProcess(command, 0)
        raise AssertionError(f"readiness asked the host something it should not: {command}")

    try:
        with tempfile.TemporaryDirectory(prefix="syrd573-status.") as tmp:
            tmp_path = Path(tmp)
            members = ["bench-1", "bench-3", "bench-4"]
            config, _ = lifecycle.config_with_pool(tmp_path, pool=BENCH, workers=["bench-1", "bench-2", "bench-3", "bench-4"])
            launcher._owner_home_for_auth = lambda _owner: tmp_path / "home"
            for name in ("bench-1", "bench-2", "bench-3", "bench-4"):
                (tmp_path / "worktrees" / name).mkdir(parents=True)
            document = rotated_document()
            for role in document["roles"]:
                role.setdefault("onboarding_prompt", "Take one ticket.")
                if role["name"] == "bench-4":
                    role["ephemeral"] = False  # kept on, but as a persistent worker: routed, not pulling
            free = {name: None for name in members}
            states = {state.role: state for state in worker_pool.worker_readiness(
                config, pool, document=document, owner_home=tmp_path / "home", members=members,
                reservations=free, runner=runner)}
            check(("auth", "codex") in asked and ("trust", "codex") in asked and ("auth", "claude") in asked,
                  f"each worker is asked about its own runtime: {asked}")
            check(states["bench-3"].ready and states["bench-3"].can_claim is True
                  and "claims ready work" in states["bench-3"].describe(),
                  f"the rotated Codex worker is ready and can claim: {states['bench-3'].describe()}")
            check(not states["bench-1"].ready and states["bench-1"].runtime == "unauthenticated",
                  f"the Claude worker is judged by Claude: {states['bench-1'].describe()}")
            check(states["bench-4"].can_take_work is True and states["bench-4"].can_claim is False
                  and "not a pull claimant" in states["bench-4"].describe(),
                  f"a free persistent worker can be routed work but does not claim: {states['bench-4'].describe()}")
            # The command an operator runs: the capacity line counts who can claim, by the same rule.
            said: list[str] = []
            code = team_launcher.switchyard_worker_pool_command(
                lifecycle.PROJECT, action="status", config_dir=tmp_path, registry_dir=tmp_path / "registry",
                board_reader=lambda _config: {"document": document},
                board_snapshot_reader=lambda _config: {"tickets": [], "reservations": {f"bench-{n}": None for n in range(1, 5)}},
                runner=runner, print_func=said.append)
            check(code == 0 and "4 worker(s), 2 ready, 4 running; 2 can take a ticket now, 0 held by a ticket, "
                  "1 can claim ready work" in said[0], f"status counts who can claim: {said}")
            plain = json.loads(json.dumps(document))
            plain.pop("scheduling")
            states = worker_pool.worker_readiness(config, pool, document=plain, owner_home=tmp_path / "home",
                                                  members=["bench-3"], reservations=free, runner=runner)
            check(states[0].claimant is None and "claim" not in states[0].describe(),
                  f"a board that does not pull says nothing about claiming: {states[0].describe()}")
    finally:
        (launcher._cli_auth_status, launcher._workdir_is_trusted, board_skill.verify_board_skill,
         launcher._owner_home_for_auth) = saved


def test_zz_schema_and_migration_carry_the_same_functions() -> None:
    """Last, so a planted fault in one copy is caught by what it does before it is caught by the copy check."""
    from schema_function_drift import assert_no_drift
    assert_no_drift(*FUNCTIONS)
    check(True, "schema.sql and the migration carry the same functions")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"pull_claimant_rotation_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
