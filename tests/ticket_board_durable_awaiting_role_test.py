#!/usr/bin/env python3
"""SYRD-15: real PostgreSQL queue/RBAC + production listener with an idle sender seam.

SYRD-805: both boards are built as production builds them -- schema.sql, then the release's migration runner and
rbac.sql; the baseline by its own release -- and the owner's "wait cleared" wake (SYRD-797) is checked: held a moment,
and dropped when a wait stands again by the time it is due."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import shlex
import shutil
import tempfile
import time
from unittest.mock import patch
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ticket_board_awaiting_role_survives_comment_test as fixture
from scripts.ticket_board.notify_listener import TicketBoardNotifyListener, ROLE_TO_TARGET, DirectorctlSender
import psycopg
from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402
from tmux_socket_cleanup import end_isolated_tmux_server, own_isolated_tmux_server  # noqa: E402 - SYRD-823
isolate_tmux_bus()  # SYRD-814: the tmux servers it starts get a user bus of their own, never the invoker's

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'scripts/ticket_board/migrations/pgu920_syrd15_durable_awaiting_role.sql'
#: The schema pgu920 was reviewed against, and the grants it ran with (SYRD-15).
BASELINE = '8651f78634174afbea55f33d2d4546a10ddb0005'


def check(condition: object, detail: str) -> None:
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def migrate(admin: str, release: Path = ROOT) -> None:
    """A deploy of `release` as production makes one (database_admin): its runner applies every migration the board
    has not recorded, then its rbac.sql. SYRD-805: the upgrade below applied today's rbac.sql to the baseline with only
    pgu920 and pgu921, and was red from the first later grant on (3c73b822's pgu923, 2026-09-06)."""
    done = subprocess.run(['bash', str(release / 'scripts/ticket-board-migrate')], capture_output=True, text=True,
                          env={**os.environ, 'TICKET_BOARD_ADMIN_DATABASE_URL': admin}, timeout=300)
    check(done.returncode == 0, f"{release.name}'s migrations apply: {done.stderr[-700:]!r}")
    fixture.psql(admin, (release / 'scripts/ticket_board/rbac.sql').read_text())


def baseline_release(into: Path) -> Path:
    """The baseline's own scripts, as its release held them: its schema.sql, runner, migrations and rbac.sql."""
    archive = subprocess.run(['git', 'archive', BASELINE, 'scripts'], cwd=ROOT, capture_output=True, check=True).stdout
    subprocess.run(['tar', '-x', '-C', str(into)], input=archive, check=True)
    return into


def checks(svc: str, admin: str) -> None:
    def sql(query: str) -> str:
        return fixture.psql(admin, query).strip()

    def role(actor: str, query: str) -> str:
        return fixture.as_role(svc, actor, query)

    def request(target: str = 'director') -> None:
        role('research', f"SELECT ticket_board.set_awaiting_role('PGU-1', '{target}');")

    def count() -> int:
        return int(sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE kind='awaiting_role'"))

    def due(step: int) -> None:
        sql(f"UPDATE ticket_board.ticket_notification_queue SET next_attempt_at=clock_timestamp() WHERE kind='awaiting_role' AND (payload->>'step')::int={step}")

    def clear() -> None:
        role('director', "SELECT ticket_board.clear_awaiting_role('PGU-1');")
        # The owner is told its wait was cleared (SYRD-697); what follows is about the wait's own queued steps.
        assert sql("SELECT string_agg(target_role || ':' || (message LIKE '%the wait on director was cleared%')::text, ',') "
                   "FROM ticket_board.ticket_notification_queue WHERE kind = 'ticket_update'") == 'research:true'
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE kind <> 'awaiting_role'")

    def listener(sender):
        return TicketBoardNotifyListener(conninfo=admin.replace('user=ticket_board_service', ''),
            sender=sender, activity_gate=lambda target: False, target_exists=lambda target: True,
            # Delivered needs the recipient to have taken it (SYRD-268): the
            # fixture's pane takes what it is sent.
            submission_witness=lambda target, _since: any(t == target for t, _m in sent))

    def process(worker) -> int:
        # Separate connections and objects exercise persisted delivery/retry after restart.
        with psycopg.connect(admin + ' user=ticket_board_listener', autocommit=True) as conn:
            return worker.process_due_notifications(conn)

    sql((ROOT / 'scripts/ticket_board/rbac.sql').read_text())
    fixture.insert_ticket(admin, 'PGU-1', state='in_progress', assignee='research')
    sql('DELETE FROM ticket_board.ticket_notification_queue')
    with psycopg.connect(admin, autocommit=True) as wake:
        wake.execute('LISTEN ticket_board_state_transition')
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: request(), range(6)))
        assert next(wake.notifies(timeout=1, stop_after=1)).channel == 'ticket_board_state_transition'
    assert count() == 4, 'concurrent requests must schedule exactly one handoff'
    since = sql("SELECT awaiting_since_at FROM ticket_board.ticket_notification_state WHERE ticket_id='PGU-1'")
    assert sql("SELECT string_agg(extract(epoch FROM (q.next_attempt_at-ns.awaiting_since_at))::int::text, ',' ORDER BY q.next_attempt_at) FROM ticket_board.ticket_notification_queue q JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id=q.ticket_id") == '0,300,900,1800'

    sent = []
    def fail(target, message):
        raise OSError('simulated missed delivery')
    assert process(listener(fail)) == 0
    assert sql("SELECT attempts || ':' || (last_error IS NOT NULL)::text FROM ticket_board.ticket_notification_queue WHERE payload->>'step'='1'") == '1:true'
    due(1)
    # Real isolated idle tmux pane, production directorctl sender, no live sessions.
    tmux = shutil.which('tmux')
    assert tmux, 'tmux is required for the delivery acceptance check'
    with tempfile.TemporaryDirectory(prefix='syrd15-pane-') as tmp:
        pane = Path(tmp)
        server = own_isolated_tmux_server('syrd15-' + str(os.getpid()))
        target = ROLE_TO_TARGET['director']
        received = pane/'received.txt'
        receiver = pane/'receiver.sh'
        receiver.write_text('#!/bin/sh\nwhile IFS= read -r line; do printf "%s\\n" "$line" >> ' + shlex.quote(str(received)) + '; printf "received:%s\\n" "$line"; done\n')
        receiver.chmod(0o755)
        wrapper = pane/'tmux'
        wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(tmux) + ' -L ' + shlex.quote(server) + ' "$@"\n')
        wrapper.chmod(0o755)
        subprocess.run([tmux, '-L', server, 'new-session', '-d', '-s', target.split(':')[0], str(receiver)], check=True)
        try:
            with patch.dict(os.environ, {'PATH': str(pane)+os.pathsep+os.environ['PATH'], 'DIRECTORCTL_ENTER_DELAY':'0'}):
                installed = pane/'directorctl'
                subprocess.run(['bash', str(ROOT/'scripts/install-directorctl')], env={**os.environ, 'DIRECTORCTL_INSTALL_PATH':str(installed)}, check=True, capture_output=True)
                sender = DirectorctlSender(str(installed))
                def deliver(target, message):
                    sender(target, message)
                    sent.append((target, message))
                assert process(listener(deliver)) == 1
            for _ in range(50):
                if received.exists() and 'New handoff.' in received.read_text():
                    break
                time.sleep(0.02)
            assert 'PGU-1' in received.read_text() and 'New handoff.' in received.read_text()
        finally:
            end_isolated_tmux_server(server)  # the server, and the socket tmux leaves behind
    assert sent[0][0] == ROLE_TO_TARGET['director'] and 'New handoff.' in sent[0][1]
    assert fixture.awaiting(admin, 'PGU-1') == 'director', 'ACK is not resolution'
    assert count() == 3
    request()
    assert count() == 3 and sql("SELECT awaiting_since_at FROM ticket_board.ticket_notification_state WHERE ticket_id='PGU-1'") == since
    role('director', "SELECT ticket_board.add_comment('PGU-1', 'Seen, still blocked');")
    sql("DELETE FROM ticket_board.ticket_notification_queue WHERE kind <> 'awaiting_role'")
    due(2)
    assert process(listener(lambda t, m: sent.append((t, m)))) == 1
    assert fixture.awaiting(admin, 'PGU-1') == 'director'
    clear()
    sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at=clock_timestamp()")
    assert process(listener(lambda t, m: sent.append((t, m)))) == 0 and count() == 0

    # A cleared wait must not lend its queued steps to a later wait for the
    # same role. Keep both generations persisted so only their identity can
    # distinguish them.
    request('director')
    wait_a_ids = [
        int(value)
        for value in sql(
            "SELECT string_agg(id::text, ',' ORDER BY id) "
            "FROM ticket_board.ticket_notification_queue WHERE kind='awaiting_role'"
        ).split(',')
    ]
    assert len(wait_a_ids) == 4
    wait_a_id_list = ','.join(str(value) for value in wait_a_ids)
    wait_a_generation = sql(
        "SELECT min(payload->>'awaiting_since_at') "
        f"FROM ticket_board.ticket_notification_queue WHERE id IN ({wait_a_id_list})"
    )
    assert int(sql(
        "SELECT count(DISTINCT payload->>'awaiting_since_at') "
        f"FROM ticket_board.ticket_notification_queue WHERE id IN ({wait_a_id_list})"
    )) == 1
    assert sql(
        "SELECT string_agg(payload->>'step', ',' ORDER BY (payload->>'step')::int) "
        f"FROM ticket_board.ticket_notification_queue WHERE id IN ({wait_a_id_list})"
    ) == '1,2,3,4'

    clear()
    request('director')
    wait_b_ids = [
        int(value)
        for value in sql(
            "SELECT string_agg(id::text, ',' ORDER BY id) "
            "FROM ticket_board.ticket_notification_queue "
            f"WHERE kind='awaiting_role' AND id NOT IN ({wait_a_id_list})"
        ).split(',')
    ]
    assert len(wait_b_ids) == 4
    wait_b_id_list = ','.join(str(value) for value in wait_b_ids)
    wait_b_generation = sql(
        "SELECT min(payload->>'awaiting_since_at') "
        f"FROM ticket_board.ticket_notification_queue WHERE id IN ({wait_b_id_list})"
    )
    assert int(sql(
        "SELECT count(DISTINCT payload->>'awaiting_since_at') "
        f"FROM ticket_board.ticket_notification_queue WHERE id IN ({wait_b_id_list})"
    )) == 1
    assert wait_a_generation != wait_b_generation
    assert sql(
        "SELECT bool_and((payload->>'awaiting_since_at')::timestamptz = "
        "(SELECT awaiting_since_at FROM ticket_board.ticket_notification_state WHERE ticket_id='PGU-1')) "
        f"FROM ticket_board.ticket_notification_queue WHERE id IN ({wait_b_id_list})"
    ) == 't'
    sql(
        "UPDATE ticket_board.ticket_notification_queue "
        "SET next_attempt_at=clock_timestamp()+interval '1 day' "
        f"WHERE id IN ({wait_b_id_list}); "
        "UPDATE ticket_board.ticket_notification_queue "
        "SET next_attempt_at=clock_timestamp() "
        f"WHERE id IN ({wait_a_id_list})"
    )
    assert int(sql(
        "SELECT count(*) FROM ticket_board.ticket_notification_queue q "
        "JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id=q.ticket_id "
        "JOIN ticket_board.tickets t ON t.id=q.ticket_id "
        f"WHERE q.id IN ({wait_a_id_list}) "
        "AND q.payload->>'awaiting_role'=ns.awaiting_role "
        "AND q.target_role='director' "
        "AND t.state IN ('in_progress','inspection','audit') "
        "AND q.next_attempt_at <= clock_timestamp() "
        "AND q.claimed_at IS NULL AND q.dead_lettered_at IS NULL "
        "AND (q.payload->>'expires_at')::timestamptz > clock_timestamp()"
    )) == 4
    before = len(sent)
    stale_deliveries = process(listener(lambda t, m: sent.append((t, m))))
    assert stale_deliveries == 0, f'{stale_deliveries} row(s) from cleared wait A were delivered against wait B'
    assert len(sent) == before
    assert int(sql(
        "SELECT count(*) FROM ticket_board.ticket_notification_queue "
        f"WHERE id IN ({wait_a_id_list})"
    )) == 0
    assert int(sql(
        "SELECT count(*) FROM ticket_board.ticket_notification_queue "
        f"WHERE id IN ({wait_b_id_list})"
    )) == 4

    sql(
        "UPDATE ticket_board.ticket_notification_queue "
        "SET next_attempt_at=clock_timestamp() "
        f"WHERE id IN ({wait_b_id_list}) AND payload->>'step'='1'"
    )
    assert process(listener(lambda t, m: sent.append((t, m)))) == 1
    assert len(sent) == before + 1
    assert sent[-1][0] == ROLE_TO_TARGET['director'] and 'New handoff.' in sent[-1][1]
    clear()
    sql(
        "UPDATE ticket_board.ticket_notification_queue "
        "SET next_attempt_at=clock_timestamp() "
        f"WHERE id IN ({wait_b_id_list})"
    )
    assert process(listener(lambda t, m: sent.append((t, m)))) == 0 and count() == 0

    # Retarget then have the awaited role act while the listener probes the pane.
    request('ops')
    request('director')
    sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at=clock_timestamp()")
    worker = listener(lambda t, m: sent.append((t, m)))
    acted = False
    def probe(target):
        nonlocal acted
        if not acted:
            acted = True
            role('director', "SELECT ticket_board.edit_fields('PGU-1', '{\"title\":\"Decision supplied\"}'::jsonb)")
            sql("DELETE FROM ticket_board.ticket_notification_queue WHERE kind <> 'awaiting_role'")
        return False
    worker.activity_gate = probe
    assert process(worker) == 0 and count() == 0
    assert fixture.awaiting(admin, 'PGU-1') == ''

    # State/assignee changes and terminal resolution suppress every pending step.
    for change in ["SELECT ticket_board.route('PGU-1', 'in_progress', 'ops')",
                   "SELECT ticket_board.route('PGU-1', 'audit', 'audit')",
                   "SELECT ticket_board.cancel('PGU-1', 'Resolved fixture')"]:
        request()
        role('director', change)
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE kind <> 'awaiting_role'")
        sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at=clock_timestamp()")
        assert process(listener(lambda t, m: sent.append((t, m)))) == 0 and count() == 0

    # Expired windows don't burst after downtime; final escalation targets director.
    role('director', "SELECT ticket_board.route('PGU-1', 'analysis', 'director')")
    role('director', "SELECT ticket_board.route('PGU-1', 'in_progress', 'research')")
    sql("DELETE FROM ticket_board.ticket_notification_queue")
    request('ops')
    sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at=clock_timestamp(), payload=jsonb_set(payload, '{expires_at}', to_jsonb(clock_timestamp()-interval '1 second')) WHERE payload->>'step' <> '4'")
    due(4)
    before = len(sent)
    assert process(listener(lambda t, m: sent.append((t, m)))) == 1
    assert len(sent) == before+1 and sent[-1][0] == ROLE_TO_TARGET['director']
    assert 'escalated' in sent[-1][1]
    request('ops')
    assert count() == 0, 'same unresolved request must not recreate ACKed schedule'
    assert fixture.awaiting(admin, 'PGU-1') == 'ops'

    # Crash after claim: the next process reclaims after the existing lease timeout.
    request('director')
    sql("DELETE FROM ticket_board.ticket_notification_queue WHERE payload->>'step' <> '1'")
    with psycopg.connect(admin + ' user=ticket_board_listener', autocommit=True) as conn:
        assert conn.execute('SELECT * FROM ticket_board.claim_notification()').fetchone()
    sql("UPDATE ticket_board.ticket_notification_queue SET claimed_at=clock_timestamp()-interval '3 minutes'")
    assert process(listener(lambda t, m: sent.append((t, m)))) == 1
    request('ops')
    # All deliveries terminate at four hours, even if delivery never succeeded.
    sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at=clock_timestamp(), payload=jsonb_set(payload, '{expires_at}', to_jsonb(clock_timestamp()-interval '1 second'))")
    assert process(listener(lambda t, m: sent.append((t, m)))) == 0 and count() == 0

    # No privileged entry point is exposed by the migration.
    for query in ["SELECT ticket_board.set_awaiting_role('PGU-1', 'research')",
                  "SELECT ticket_board.set_awaiting_role('PGU-1', 'bogus')",
                  "SELECT ticket_board.enqueue_awaiting_role_handoff('PGU-1')"]:
        try:
            role('research', query)
        except AssertionError:
            pass
        else:
            raise AssertionError('unauthorized/invalid call succeeded: '+query)
    # An outer transaction rollback leaves neither state nor notification behind.
    role('director', "SELECT ticket_board.clear_awaiting_role('PGU-1')")
    role('research', "BEGIN; SELECT ticket_board.set_awaiting_role('PGU-1', 'director'); ROLLBACK;")
    assert count() == 0 and fixture.awaiting(admin, 'PGU-1') == ''

    # SYRD-797 (pgu1045; notification_eligibility): the owner's "wait cleared" wake is held a moment, so the awaited
    # role's comment can follow it; and when it falls due while the owner has asked again, it says nothing false: it
    # is dropped, not delivered.
    request()
    role('director', "SELECT ticket_board.clear_awaiting_role('PGU-1');")
    held = float(sql("SELECT coalesce(extract(epoch FROM max(next_attempt_at) - clock_timestamp()), -1) FROM "
                     "ticket_board.ticket_notification_queue WHERE kind = 'ticket_update' AND target_role = 'research' "
                     "AND message LIKE '%the wait on director was cleared%'"))
    check(25 < held <= 30, f"the owner's 'wait cleared' wake is held a moment, not sent at once: due in {held:.1f}s")
    request()  # the owner asks again before that wake is due
    sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() + interval '1 hour' "
        "WHERE kind = 'awaiting_role'")
    sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() WHERE kind = 'ticket_update'")
    told: list[str] = []
    process(listener(lambda t, m: told.append(m)))
    left = sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE kind = 'ticket_update'")
    check([m for m in told if 'was cleared' in m] == [] and left == '0',
          f"a 'wait cleared' wake due while a wait stands again is dropped, not delivered: told {told}, {left} left")
    role('director', "SELECT ticket_board.clear_awaiting_role('PGU-1');")
    sql("DELETE FROM ticket_board.ticket_notification_queue")


def run(svc: str, admin: str) -> None:
    migrate(admin)  # a fresh board as production builds one: schema.sql, then every migration and rbac.sql
    checks(svc, admin)
    fresh = fixture.psql(admin, "SELECT pg_get_functiondef('ticket_board.set_awaiting_role(text,text)'::regprocedure)")
    # Upgrade the actual reviewed pre-change board, as its release built it -- its schema.sql, every migration it
    # shipped (recorded, as its runner records them) and its grants -- with a pre-existing wait. A schema.sql alone
    # records no migration, and today's runner would then apply them all from the first, pgu589's `\ir ../schema.sql`
    # among them: today's schema over the baseline's tables.
    with tempfile.TemporaryDirectory(prefix='syrd805-baseline.') as raw:
        baseline = baseline_release(Path(raw))
        fixture.psql(admin, 'DROP SCHEMA ticket_board CASCADE;\n'
                     + (baseline / 'scripts/ticket_board/schema.sql').read_text())
        migrate(admin, baseline)
    fixture.insert_ticket(admin, 'PGU-90', state='in_progress', assignee='app')
    fixture.as_role(svc, 'app', "SELECT ticket_board.set_awaiting_role('PGU-90', 'director')")
    fixture.psql(admin, MIGRATION.read_text())
    assert fixture.psql(admin, "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE kind='awaiting_role'").strip() == '4'
    fixture.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue WHERE kind='awaiting_role'")
    fixture.psql(admin, MIGRATION.read_text())
    assert fixture.psql(admin, "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE kind='awaiting_role'").strip() == '0'
    # Then the rest of the way, as a deploy goes: every later migration, then today's rbac.sql. The upgraded board
    # ends where a fresh one starts.
    migrate(admin)
    upgraded = fixture.psql(admin, "SELECT pg_get_functiondef('ticket_board.set_awaiting_role(text,text)'::regprocedure)")
    check(upgraded == fresh, "upgraded from the baseline, set_awaiting_role is the fresh board's")
    fixture.psql(admin, "DELETE FROM ticket_board.tickets")
    checks(svc, admin)
    print('durable awaiting-role: fresh + upgrade + repeat migration + PostgreSQL/listener checks passed')


if __name__ == '__main__':
    if fixture.missing_prerequisite():
        raise SystemExit(fixture.missing_prerequisite())
    fixture.run_checks = run
    raise SystemExit(fixture.main())
