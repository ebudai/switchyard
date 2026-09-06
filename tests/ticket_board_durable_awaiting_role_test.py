#!/usr/bin/env python3
"""SYRD-15: real PostgreSQL queue/RBAC + production listener with an idle sender seam."""
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

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'scripts/ticket_board/migrations/pgu920_syrd15_durable_awaiting_role.sql'


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

    def listener(sender):
        return TicketBoardNotifyListener(conninfo=admin.replace('user=ticket_board_service', ''),
            sender=sender, activity_gate=lambda target: False, target_exists=lambda target: True)

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
        server = 'syrd15-' + str(os.getpid())
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
            subprocess.run([tmux, '-L', server, 'kill-server'], check=False, capture_output=True)
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
    role('director', "SELECT ticket_board.clear_awaiting_role('PGU-1');")
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

    role('director', "SELECT ticket_board.clear_awaiting_role('PGU-1');")
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
    role('director', "SELECT ticket_board.clear_awaiting_role('PGU-1');")
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


def run(svc: str, admin: str) -> None:
    checks(svc, admin)
    fresh = fixture.psql(admin, "SELECT pg_get_functiondef('ticket_board.set_awaiting_role(text,text)'::regprocedure)")
    # Upgrade the actual reviewed pre-change schema, with a pre-existing wait.
    baseline = subprocess.check_output(['git', 'show', '8651f78634174afbea55f33d2d4546a10ddb0005:scripts/ticket_board/schema.sql'], cwd=ROOT, text=True)
    fixture.psql(admin, 'DROP SCHEMA ticket_board CASCADE;\n'+baseline)
    fixture.psql(admin, (ROOT/'scripts/ticket_board/rbac.sql').read_text())
    fixture.insert_ticket(admin, 'PGU-90', state='in_progress', assignee='app')
    fixture.as_role(svc, 'app', "SELECT ticket_board.set_awaiting_role('PGU-90', 'director')")
    fixture.psql(admin, MIGRATION.read_text())
    assert fixture.psql(admin, "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE kind='awaiting_role'").strip() == '4'
    fixture.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue WHERE kind='awaiting_role'")
    fixture.psql(admin, MIGRATION.read_text())
    assert fixture.psql(admin, "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE kind='awaiting_role'").strip() == '0'
    # The opt-in foundation generalizes only recipient/stage lookup; compare
    # current fresh schema after both reviewed incremental layers.
    fixture.psql(admin, (ROOT / "scripts/ticket_board/migrations/pgu921_syrd11_declarative_workflow.sql").read_text())
    assert fixture.psql(admin, "SELECT pg_get_functiondef('ticket_board.set_awaiting_role(text,text)'::regprocedure)") == fresh
    fixture.psql(admin, "DELETE FROM ticket_board.tickets")
    checks(svc, admin)
    print('durable awaiting-role: fresh + upgrade + repeat migration + PostgreSQL/listener checks passed')


if __name__ == '__main__':
    if fixture.missing_prerequisite():
        raise SystemExit(fixture.missing_prerequisite())
    fixture.run_checks = run
    raise SystemExit(fixture.main())
