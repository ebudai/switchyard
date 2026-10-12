#!/usr/bin/env python3
"""SYRD-585: clearing a reminder snooze applies what its preview showed.

`clear-reminder-snooze --apply` failed inside the board whenever the clear
released a batch's last snoozed ticket:

    column reference "reason" is ambiguous

clear_reminder_snooze held the supplied reason in a variable named `reason`,
which is also a reminder_snooze_batches column, and the batch close named it.
SYRD-537's own test cleared one ticket of five, which never reaches the close.

Every scenario runs on a board built the production way (companion roles,
schema.sql, the real ticket-board-migrate, rbac.sql, a declared workflow), with
the real write CLI talking to the real board server over its Unix socket:

* from main before this change -- code and SQL, in a child from a git archive --
  previews succeed, a clear that leaves a ticket snoozed applies, and every
  clear that ends a batch is refused with the ambiguity, writing nothing;
* from this tree: a whole batch, one ticket of a batch and then the rest each
  apply exactly what the preview listed and record the reason; previews and
  every refused or failing request write nothing, a fault in the batch close
  undoes the members' release with it, and a change of stage, owner or
  candidate still ends a member's snooze by itself.
"""

from __future__ import annotations

import contextlib
import copy
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
BEFORE = "f23e7098146c6ea34a335fd31cf99037eee027ad"  # main before SYRD-585
COMMIT = "6d4ee1aa99147e8118f59e637be02b660d62d064"
OTHER_COMMIT = "9c1d0e2f3a4b5c6d7e8f90a1b2c3d4e5f6a7b8c9"
TICKETS = tuple(f"PGU-{n}" for n in range(1, 10))
REASON = ("The recorded App pane hold ended and its held ticket notices were dropped; "
          "ordinary reminders may resume.")
AMBIGUOUS = 'column reference "reason" is ambiguous'
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


def scenario(root: Path, prefix: str) -> dict:
    """Everything observable, from the tree at `root` (its code and its SQL)."""
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as t
    from scripts.ticket_board import write_cli
    from temporary_cluster import temporary_cluster
    from workflow_document_eras import before_relaying

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = t.conninfo(cluster.socket_dir, cluster.port, "board")
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "board"])
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
        t.create_roles(admin)
        t.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
        runner = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")],
                                env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin},
                                capture_output=True, text=True)
        assert runner.returncode == 0, runner.stderr
        t.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        app = t.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="cerulean",
                               ticket_prefix="PGU",
                               database_url=t.conninfo(cluster.socket_dir, cluster.port, "board", t.SERVICE_ROLE))
        doc = copy.deepcopy(before_relaying())
        doc["project"] = "cerulean"
        doc.setdefault("reassign", {})
        doc.setdefault("remove_stages", [])
        app.apply_workflow(doc, expected_revision=0, dry_run=False, caller_role="director")
        for tid in TICKETS:
            if tid == "PGU-7":  # in a stage with more than one owner, so only its owner can change
                t.seed_postgres_ticket(admin, tid, title=tid, state="in_progress", assignee="app")
                continue
            t.seed_postgres_ticket(admin, tid, title=tid, state="director_review", assignee="director",
                                   commit_hash=COMMIT, audit_signoff=True, needs_user_signoff=True, user_signoff=True)

        sock = cluster.root / "board.sock"
        servers: dict = {}

        def cli(role: str, *args: str) -> tuple[int, str, str]:
            """The real `ticket-board-write`, as `role`, against the real server."""
            if role not in servers:
                from scripts.ticket_board.board_notifications import TicketBoardEventHub
                from scripts.ticket_board.server import TicketBoardUnixServer
                for srv, events in servers.values():
                    srv.shutdown()
                    srv.server_close()
                    events.close()
                servers.clear()
                events = TicketBoardEventHub(app)
                srv = TicketBoardUnixServer(sock, app, events=events, director_notifier=t.QuietNotifier(),
                                            role_authority=t.local_role_authority_as(role))
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                servers[role] = (srv, events)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = write_cli.main(["--socket", str(sock), "--caller-role", role, *args])
                except SystemExit as exc:  # argparse's answer is an answer, not a crash
                    code = int(exc.code or 0)
            return code, out.getvalue(), err.getvalue()

        def clear(*args: str, role: str = "director") -> tuple[int, str, str]:
            return cli(role, "clear-reminder-snooze", *args)

        def scalar(sql: str) -> str:
            return t.psql(admin, sql).strip()

        def state() -> str:
            """Every snooze row and the notification queue, byte for byte."""
            return scalar(
                "SELECT md5(coalesce((SELECT string_agg(to_jsonb(b)::text, '|' ORDER BY id) "
                "FROM ticket_board.reminder_snooze_batches b), '') || '#' || "
                "coalesce((SELECT string_agg(to_jsonb(m)::text, '|' ORDER BY batch_id, ticket_id) "
                "FROM ticket_board.reminder_snooze_members m), '') || '#' || "
                "(SELECT count(*) FROM ticket_board.ticket_notification_queue)::text);")

        def rows() -> dict:
            return json.loads(scalar(
                "SELECT jsonb_build_object("
                "'batches', (SELECT coalesce(jsonb_agg(jsonb_build_array(id, state, closed_by, close_reason, "
                "closed_at IS NOT NULL) ORDER BY id), '[]') FROM ticket_board.reminder_snooze_batches), "
                "'members', (SELECT coalesce(jsonb_agg(jsonb_build_array(batch_id, ticket_id, released_by, "
                "release_reason) ORDER BY batch_id, position), '[]') FROM ticket_board.reminder_snooze_members))::text;"))

        def snoozed(*tids: str) -> dict:
            return {tid: scalar(f"SELECT ticket_board.ticket_reminders_snoozed('{tid}', clock_timestamp());") == "t"
                    for tid in tids}

        until = scalar("SELECT to_char((clock_timestamp() + interval '12 hours') AT TIME ZONE 'UTC', "
                       "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"');")

        def snooze(*tids: str) -> int:
            named = [arg for tid in tids for arg in ("--ticket", tid)]
            return cli("director", "snooze-reminders", *named, "--until", until,
                       "--reason", "overnight Final Sign-Off batch", "--apply")[0]

        seen["snoozes"] = [snooze("PGU-1", "PGU-2"), snooze("PGU-3", "PGU-4", "PGU-5"),
                           snooze("PGU-6", "PGU-7", "PGU-8")]
        seen["snoozed_at_start"] = snoozed(*TICKETS)
        start = state()

        # Previews: the exact scope, and nothing written.
        seen["preview_whole"] = clear("--batch", "1", "--reason", REASON)
        seen["preview_one"] = clear("--batch", "2", "--ticket", "PGU-3", "--reason", REASON)
        seen["preview_rest"] = clear("--batch", "2", "--ticket", "PGU-4", "--ticket", "pgu-5", "--reason", REASON)
        seen["preview_writes_nothing"] = state() == start

        # Requests that must be refused, each writing nothing.
        refused = {
            "blank reason": ("--batch", "1", "--reason", "   "),
            "ticket not in the batch": ("--batch", "2", "--ticket", "PGU-1", "--reason", REASON),
            "one ticket in it, one not": ("--batch", "2", "--ticket", "PGU-3", "--ticket", "PGU-1", "--reason", REASON),
            "no such batch": ("--batch", "99", "--reason", REASON),
        }
        seen["refused"] = {name: clear(*args, "--apply") for name, args in refused.items()}
        seen["refused_app"] = clear("--batch", "1", "--reason", REASON, "--apply", role="app")
        seen["refused_sql_blank"] = ""
        try:
            with app._pg_connect() as conn:
                app._pg_set_caller_role(conn, "director")
                conn.execute("SELECT ticket_board.clear_reminder_snooze(1, ARRAY[]::text[], '  ', true)")
        except Exception as exc:  # noqa: BLE001 -- the database's refusal is the answer
            seen["refused_sql_blank"] = str(exc).splitlines()[0]
        seen["refusals_write_nothing"] = state() == start

        # A fault in the batch close, after the members' release: nothing of it stays.
        t.psql(admin, """
CREATE FUNCTION ticket_board.syrd585_fault() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'syrd585 injected batch-close fault'; END $$;
CREATE TRIGGER syrd585_fault BEFORE UPDATE ON ticket_board.reminder_snooze_batches
    FOR EACH ROW EXECUTE FUNCTION ticket_board.syrd585_fault();
""")
        seen["faulted"] = clear("--batch", "1", "--reason", REASON, "--apply")
        seen["fault_writes_nothing"] = state() == start
        t.psql(admin, "DROP TRIGGER syrd585_fault ON ticket_board.reminder_snooze_batches; "
                      "DROP FUNCTION ticket_board.syrd585_fault();")

        # The whole batch.
        before_whole = state()
        seen["clear_whole"] = clear("--batch", "1", "--reason", REASON, "--apply")
        seen["whole_wrote"] = state() != before_whole
        seen["after_whole"] = rows()
        seen["snoozed_after_whole"] = snoozed(*TICKETS)
        after_whole = state()
        seen["clear_again"] = clear("--batch", "1", "--reason", REASON, "--apply")
        seen["again_writes_nothing"] = state() == after_whole

        # One ticket of a multi-ticket batch, then the rest by name.
        seen["clear_one"] = clear("--batch", "2", "--ticket", "PGU-3", "--reason", REASON, "--apply")
        seen["after_one"] = rows()
        seen["snoozed_after_one"] = snoozed(*TICKETS)
        seen["clear_rest"] = clear("--batch", "2", "--ticket", "PGU-4", "--ticket", "pgu-5",
                                   "--reason", "the rest of the batch", "--apply")
        seen["after_rest"] = rows()
        seen["snoozed_after_rest"] = snoozed(*TICKETS)

        # Automatic invalidation: a change of stage, owner or candidate ends that
        # member's snooze by itself -- no clear involved.
        app.force_move_ticket("PGU-6", "analysis", "director", caller_role="director")
        app.reassign_ticket("PGU-7", "ops", reason="SYRD-585 owner change", caller_role="director")
        # No board operation moves a candidate without moving its stage, so the
        # candidate alone is changed by the admin, past the workflow's triggers:
        # the snapshot comparison is what is under test, not a writer.
        t.psql(admin, "BEGIN; SET LOCAL session_replication_role = replica; "
                      f"UPDATE ticket_board.tickets SET commit_hash = '{OTHER_COMMIT}' WHERE id = 'PGU-8'; COMMIT;")
        seen["invalidated"] = {tid: [app.get_ticket(tid).get("reminder_snooze") or {}, snoozed(tid)[tid]]
                               for tid in ("PGU-6", "PGU-7", "PGU-8")}
        seen["control"] = [app.get_ticket("PGU-9").get("reminder_snooze"), snoozed("PGU-9")["PGU-9"]]
        # And a batch whose members all changed -- SYRD-113's -- still clears whole.
        seen["clear_invalidated"] = clear("--batch", "3", "--reason", REASON, "--apply")
        seen["after_invalidated"] = rows()

        # A board installed from schema.sql alone -- what a new board gets before
        # any migration -- clears a whole batch the same way.
        fresh = t.conninfo(cluster.socket_dir, cluster.port, "fresh")
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
        t.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
        t.psql(fresh, (root / "scripts/ticket_board/rbac.sql").read_text())
        t.seed_postgres_ticket(fresh, "PGU-1", title="PGU-1", state="director_review", assignee="director",
                               commit_hash=COMMIT)
        try:
            seen["fresh_clear"] = t.psql(fresh, (
                "SET ROLE ticket_board_service; BEGIN; SELECT set_config('ticket_board.caller_role', 'director', true); "
                "SELECT ticket_board.snooze_reminders(ARRAY['PGU-1'], clock_timestamp() + interval '1 hour', "
                "'overnight', true) IS NOT NULL; COMMIT; "
                "BEGIN; SELECT set_config('ticket_board.caller_role', 'director', true); "
                "SELECT ticket_board.clear_reminder_snooze(1, ARRAY[]::text[], 'fresh board', true) ->> 'applied'; COMMIT; "
                "SELECT state || ':' || close_reason FROM ticket_board.reminder_snooze_batches;")).split()
        except AssertionError as exc:
            seen["fresh_clear"] = [str(exc)[-600:]]

        for srv, events in servers.values():
            srv.shutdown()
            srv.server_close()
            events.close()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def answer(result: tuple[int, str, str]) -> dict:
    code, out, _err = result
    return json.loads(out) if code == 0 else {}


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd585b-")))
        return 0

    with tempfile.TemporaryDirectory(prefix="syrd585.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])

    # Main: the previews work, and every clear that ends a batch is refused inside the board.
    check(before["snoozes"] == [0, 0, 0] and answer(before["preview_whole"]).get("releases") == ["PGU-1", "PGU-2"],
          f"before: the batches are set and the preview lists the release: {before['snoozes']} {before['preview_whole']}")
    check(before["clear_whole"][0] != 0 and AMBIGUOUS in before["clear_whole"][2]
          and not before["whole_wrote"] and before["snoozed_after_whole"]["PGU-1"],
          f"before: the whole-batch apply fails on the ambiguous reference and changes nothing: {before['clear_whole']}")
    check(before["clear_one"][0] == 0 and before["clear_rest"][0] != 0 and AMBIGUOUS in before["clear_rest"][2]
          and before["clear_invalidated"][0] != 0 and AMBIGUOUS in before["clear_invalidated"][2],
          f"before: one ticket of three clears, but the last ones never can, nor a changed batch: "
          f"{before['clear_one'][0]} {before['clear_rest'][2][-300:]} {before['clear_invalidated'][2][-300:]}")

    after = scenario(ROOT, "syrd585a-")
    check(after["snoozes"] == [0, 0, 0] and all(after["snoozed_at_start"][tid] for tid in TICKETS[:8])
          and not after["snoozed_at_start"]["PGU-9"],
          f"the probe arrives: three batches over eight tickets, the ninth a control: {after['snoozed_at_start']}")

    # Previews.
    whole, one, rest = (answer(after[k]) for k in ("preview_whole", "preview_one", "preview_rest"))
    check(whole.get("applied") is False and whole["releases"] == ["PGU-1", "PGU-2"] and whole["remaining"] == []
          and whole["refusals"] == [] and whole["reason"] == REASON,
          f"the whole-batch preview lists both tickets and the reason: {after['preview_whole']}")
    check(one["releases"] == ["PGU-3"] and one["remaining"] == ["PGU-4", "PGU-5"]
          and rest["releases"] == ["PGU-4", "PGU-5"] and rest["remaining"] == ["PGU-3"],
          f"a named preview lists exactly its tickets: {one} {rest}")
    check(after["preview_writes_nothing"], "previews write nothing: every snooze row and the queue are unchanged")

    # Refusals.
    expected = {"blank reason": "--reason (or --reason-file) is required", "ticket not in the batch": "PGU-1",
                "one ticket in it, one not": "not snoozed in batch 2", "no such batch": "batch 99 not found"}
    for name, needle in expected.items():
        code, _out, err = after["refused"][name]
        check(code != 0 and needle in err, f"{name}: refused: {after['refused'][name]}")
    check(after["refused_app"][0] != 0 and "app cannot call clear_reminder_snooze" in after["refused_app"][2],
          f"App is refused: {after['refused_app']}")
    check("a reason is required" in after["refused_sql_blank"],
          f"the database itself refuses a blank reason: {after['refused_sql_blank']!r}")
    check(after["refusals_write_nothing"], "and no refused request changes a snooze or a batch")
    check(after["faulted"][0] != 0 and "syrd585 injected batch-close fault" in after["faulted"][2]
          and after["fault_writes_nothing"],
          f"a fault in the batch close undoes the members' release with it: {after['faulted']}")

    # The whole batch.
    applied = answer(after["clear_whole"])
    check(after["clear_whole"][0] == 0 and applied["applied"] is True and applied["by"] == "director"
          and applied["releases"] == whole["releases"] and applied["remaining"] == [] and applied["reason"] == REASON,
          f"the whole-batch apply does exactly what its preview showed: {after['clear_whole']}")
    check(after["after_whole"]["batches"][0] == [1, "cleared", "director", REASON, True]
          and after["after_whole"]["members"][:2] == [[1, "PGU-1", "director", f"cleared: {REASON}"],
                                                      [1, "PGU-2", "director", f"cleared: {REASON}"]],
          f"the batch is closed with the supplied reason, and both members released: {after['after_whole']}")
    check(not after["snoozed_after_whole"]["PGU-1"] and not after["snoozed_after_whole"]["PGU-2"]
          and all(after["snoozed_after_whole"][tid] for tid in TICKETS[2:8]),
          f"their reminders resume; the other batches' stay snoozed: {after['snoozed_after_whole']}")
    check(after["clear_again"][0] != 0 and "batch 1 is already cleared" in after["clear_again"][2]
          and after["again_writes_nothing"],
          f"clearing it again is refused and writes nothing: {after['clear_again']}")

    # One ticket of three, then the rest.
    check(after["clear_one"][0] == 0 and answer(after["clear_one"])["releases"] == ["PGU-3"]
          and after["after_one"]["batches"][1] == [2, "snoozed", None, None, False]
          and [m[3] for m in after["after_one"]["members"][2:5]] == [f"cleared: {REASON}", None, None]
          and [after["snoozed_after_one"][tid] for tid in ("PGU-3", "PGU-4", "PGU-5")] == [False, True, True],
          f"one ticket of a multi-ticket batch: released alone, the batch stays open: {after['after_one']}")
    check(after["clear_rest"][0] == 0 and answer(after["clear_rest"])["releases"] == ["PGU-4", "PGU-5"]
          and after["after_rest"]["batches"][1] == [2, "cleared", "director", "the rest of the batch", True]
          and [m[3] for m in after["after_rest"]["members"][2:5]]
          == [f"cleared: {REASON}", "cleared: the rest of the batch", "cleared: the rest of the batch"]
          and not any(after["snoozed_after_rest"][tid] for tid in ("PGU-3", "PGU-4", "PGU-5")),
          f"releasing the last ones closes the batch with their reason: {after['after_rest']}")

    # Invalidation, untouched by any clear.
    changed = {tid: (json_["status"], json_.get("changed"), live) for tid, (json_, live) in after["invalidated"].items()}
    check(changed == {"PGU-6": ("invalidated", ["state"], False), "PGU-7": ("invalidated", ["assignee"], False),
                      "PGU-8": ("invalidated", ["commit_hash"], False)},
          f"a change of stage, owner or candidate ends that member's snooze by itself: {changed}")
    check(after["control"] == [None, False], f"and an unsnoozed ticket says nothing: {after['control']}")
    check(after["clear_invalidated"][0] == 0 and answer(after["clear_invalidated"])["releases"] == ["PGU-6", "PGU-7", "PGU-8"]
          and after["after_invalidated"]["batches"][2] == [3, "cleared", "director", REASON, True],
          f"a batch whose members all changed still clears whole: {after['clear_invalidated']}")
    check(any(AMBIGUOUS in line for line in before["fresh_clear"]),
          f"before: a board from schema.sql alone fails the same way: {before['fresh_clear']}")
    check(" ".join(after["fresh_clear"]) == "SET BEGIN director t COMMIT BEGIN director true COMMIT cleared:fresh board",
          f"a board from schema.sql alone clears a whole batch too: {after['fresh_clear']}")
    print(f"reminder_snooze_clear_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
