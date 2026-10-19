#!/usr/bin/env python3
"""SYRD-598: the User's confirmed board-UI action may use the control role's operation permission, for that one action.

Every case runs on a production-built board (schema.sql, migrations, rbac.sql),
the real HTTP server and the real app, declared and legacy. The before-run is
BEFORE (main when the ticket was cut), in a child process built from a git
archive: there the board UI has no way to act for the Director except by naming
`director` in a header.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

import live_boundary  # noqa: E402

live_boundary.isolate()  # SYRD-627: no live home, pane store or runtime directory, here or in a child

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "e986c4964f2aa8f1fa10a11c0bc3f8135bfc4190"  # main when SYRD-598 was cut
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0
#: The approved board UI user's uid in the page-and-grant cases: no account on any host has it.
BULK_UI_UID = 4242424


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def board_cases(root: Path, declared: bool) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from temporary_cluster import temporary_cluster

    try:  # the board as production serves it: the shared fixture stands in for role panes over HTTP
        from scripts.ticket_board import ui_delegation
        ui_delegation.TRUST_HEADER_ROLES = False
    except ImportError:  # BEFORE has none
        pass
    from scripts.ticket_board.write_client import UnixHTTPConnection

    seen: dict = {}
    with temporary_cluster(prefix="u598", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="u598-sock.") as sock_dir:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        if declared:
            app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")
        try:  # an approved board UI user, as provisioning records one; this process's requests are that User's page
            from board_ui_user_record import trust_board_ui_users
            trust_board_ui_users(Path(sock_dir) / "registry", [{"account": "board-ui-user", "uid": BULK_UI_UID}],
                                 peer_uid=BULK_UI_UID)
        except ImportError:  # BEFORE has no board UI confirmations
            pass
        servers = []

        def start_server():
            server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
            return server

        server = start_server()
        # The Director's own pane: the board's Unix socket, where the kernel says who is calling.
        socket_path = Path(sock_dir) / "board.sock"
        unix = fixture.TicketBoardUnixServer(socket_path, app, events=fixture.TicketBoardEventHub(app),
                                             director_notifier=fixture.QuietNotifier(),
                                             role_authority=fixture.local_role_authority_as("director"))
        threading.Thread(target=unix.serve_forever, daemon=True).start()

        def director_pane(ticket, operation, payload):
            conn = UnixHTTPConnection(str(socket_path), 30)
            conn.request("POST", f"/api/tickets/{ticket}/actions/{operation}", body=json.dumps(payload).encode(),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            return resp.status

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        for n in range(1, 8):
            fixture.seed_postgres_ticket(admin, f"PGU-{n}", title=f"Ticket {n}", state="analysis", assignee="director",
                                         commit_exempt=True)

        def request(method, path, body=None, headers=None, srv=None):
            srv = srv or server
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=30)
            data = json.dumps(body).encode() if body is not None else None
            hdrs = dict(headers or {})
            if data is not None:
                hdrs.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            text = resp.read().decode("utf-8", "replace")
            return resp.status, text, resp.getheader("Set-Cookie") or ""

        def page_session(srv=None):
            status, _page, cookie = request("GET", "/", srv=srv)
            return status, cookie

        status, cookie = page_session()
        seen["cookie"] = [status, cookie.split("=", 1)[0], "HttpOnly" in cookie, "SameSite=Strict" in cookie]
        session = cookie.split(";", 1)[0]
        origin = f"http://127.0.0.1:{server.server_port}"
        token = {"X-Ticket-Board-Write-Token": server.write_token}
        as_user = dict(token, **{"X-Ticket-Board-Caller-Role": "user"})
        ui = dict(as_user, Cookie=session, Origin=origin, **{"Sec-Fetch-Site": "same-origin"})

        def short(result):
            return [result[0], result[1].splitlines()[0][:140] if result[1] else ""]

        def prepare(ticket, operation, payload, headers=ui):
            return request("POST", "/api/ui/delegations",
                           {"ticket_id": ticket, "operation": operation, "payload": payload, "prepare": True}, headers)

        def mint(ticket, operation, payload, headers=ui, version=None):
            if version is None:
                prepared = prepare(ticket, operation, payload)
                if prepared[0] != 200:
                    return prepared[0], prepared[1], None
                version = json.loads(prepared[1])["ticket_version"]
            status, text, _ = request("POST", "/api/ui/delegations",
                                      {"ticket_id": ticket, "operation": operation, "payload": payload, "ticket_version": version},
                                      headers)
            return status, text, (json.loads(text)["token"] if status == 200 else None)

        def act(ticket, operation, payload, confirmation, headers=ui, srv=None):
            hdrs = dict(headers, **({"X-Ticket-Board-UI-Delegation": confirmation} if confirmation else {}))
            return short(request("POST", f"/api/tickets/{ticket}/actions/{operation}", payload, hdrs, srv=srv))

        def state(ticket):
            return sql(f"SELECT state || '/' || assignee || '/' || manually_controlled FROM ticket_board.tickets WHERE id = '{ticket}';")

        blockers = {"blocked_by": ["PGU-7"], "blocked_reason": "waits on seven"}
        route = ({"target": "in_progress", "assignee": "main"} if declared else {"state": "in_progress", "assignee": "main"})
        # The same payload as the User, through the API, without a confirmation: refused.
        seen["user_alone"] = [act("PGU-1", "set_blockers", blockers, None, as_user)[0],
                              act("PGU-1", "route", route, None, as_user)[0]]
        # A role named in an HTTP header proves nothing: anyone who reaches the port can read its write token.
        forged = dict(token, **{"X-Ticket-Board-Caller-Role": "director"})
        seen["forged_director_header"] = [act("PGU-6", "route", route, None, forged),
                                          act("PGU-6", "set_blockers", blockers, None, forged)]
        seen["forged_inspector_header"] = act("PGU-6", "add_comment", {"text": "hello"}, None,
                                              dict(token, **{"X-Ticket-Board-Caller-Role": "inspector"}))
        # The Director's own pane is unchanged.
        seen["director_unchanged"] = director_pane("PGU-6", "set_manually_controlled", {"manually_controlled": True})
        seen["prepare"] = short(prepare("PGU-1", "set_blockers", blockers))
        if seen["prepare"][0] != 200:  # BEFORE has no board UI confirmations at all
            return seen
        detail = json.loads(prepare("PGU-1", "set_blockers", blockers)[1])
        seen["prepare_detail"] = {k: detail[k] for k in ("ticket_id", "operation", "authority", "state", "assignee")}
        # Bound to one ticket, operation, payload, session and caller; single use.
        status, _text, confirmation = mint("PGU-1", "set_blockers", blockers)
        refusals = {
            "another ticket": act("PGU-2", "set_blockers", blockers, confirmation),
            "altered payload": act("PGU-1", "set_blockers", dict(blockers, blocked_reason="something else"), confirmation),
            "another operation": act("PGU-1", "set_manually_controlled", {"manually_controlled": True}, confirmation),
            "no origin": act("PGU-1", "set_blockers", blockers, confirmation, {k: v for k, v in ui.items() if k != "Origin"}),
            "forged origin": act("PGU-1", "set_blockers", blockers, confirmation, dict(ui, Origin="http://evil.example")),
            # A rebound name: the page's Origin agrees with the Host it sent, and neither is this board.
            "forged host": act("PGU-1", "set_blockers", blockers, confirmation,
                               dict(ui, Host="evil.example", Origin="http://evil.example")),
            "cross-site fetch": act("PGU-1", "set_blockers", blockers, confirmation, dict(ui, **{"Sec-Fetch-Site": "cross-site"})),
            "no session": act("PGU-1", "set_blockers", blockers, confirmation, {k: v for k, v in ui.items() if k != "Cookie"}),
            "forged session": act("PGU-1", "set_blockers", blockers, confirmation, dict(ui, Cookie="tb_ui_session=" + "x" * 43)),
            "as director": act("PGU-1", "set_blockers", blockers, confirmation, dict(ui, **{"X-Ticket-Board-Caller-Role": "director"})),
            "no write token": act("PGU-1", "set_blockers", blockers, confirmation, {k: v for k, v in ui.items() if "Token" not in k}),
        }
        refusals["used"] = act("PGU-1", "set_blockers", blockers, confirmation)
        refusals["replay"] = act("PGU-1", "set_blockers", blockers, confirmation)
        seen["used_state"] = json.loads(request("GET", "/api/tickets/PGU-1")[1]).get("blocked_by")
        # Expired, stale, and after a restart.
        _s, _t, expired = mint("PGU-2", "set_blockers", blockers)
        sql("UPDATE ticket_board.ui_delegations SET expires_at = clock_timestamp() - interval '1 second' WHERE consumed_at IS NULL;")
        refusals["expired"] = act("PGU-2", "set_blockers", blockers, expired)
        _s, _t, stale = mint("PGU-2", "set_blockers", blockers)
        director_pane("PGU-2", "set_manually_controlled", {"manually_controlled": True})
        refusals["stale ticket"] = act("PGU-2", "set_blockers", blockers, stale)
        refusals["stale version at mint"] = short(mint("PGU-3", "set_blockers", blockers, version="0" * 32))
        if declared:
            # A grant for a move the ticket's stage no longer offers: refused before anything runs, for that reason.
            fixture.seed_postgres_ticket(admin, "PGU-8", title="Ticket 8", state="analysis", assignee="director",
                                         commit_exempt=True)
            _s, _t, for_triage = mint("PGU-8", "route", route)
            director_pane("PGU-8", "route", route)
            seen["no longer lendable"] = act("PGU-8", "route", route, for_triage)
        _s, _t, before_restart = mint("PGU-3", "set_blockers", blockers)
        restarted = start_server()
        refusals["after a restart"] = act("PGU-3", "set_blockers", blockers, before_restart,
                                          dict(ui, Origin=f"http://127.0.0.1:{restarted.server_port}",
                                               **{"X-Ticket-Board-Write-Token": restarted.write_token}), srv=restarted)
        # A live grant opens only the action it names, checked before anything runs: measure_size runs git
        # before its first write, so a set_blockers grant must not reach it, and the grant stays unspent.
        _s, _t, for_blockers = mint("PGU-3", "set_blockers", blockers)
        refusals["another operation, before it runs"] = act("PGU-3", "measure_size", {"commit": "HEAD"}, for_blockers)
        seen["grant unspent"] = sql("SELECT count(*) FROM ticket_board.ui_delegations WHERE consumed_at IS NULL "
                                    "AND ticket_id = 'PGU-3' AND operation = 'set_blockers' AND expires_at > now();")
        # A grant is minted for the version the User was shown, never for whatever the ticket is now.
        seen["minted without the version shown"] = short(request(
            "POST", "/api/ui/delegations", {"ticket_id": "PGU-2", "operation": "set_blockers", "payload": blockers}, ui))
        # One kept-alive connection serves its requests on one thread. A confirmation a refused request left
        # unspent (the board refuses a reason with nothing to wait on before the action writes) must not be
        # spent by the next request on that connection -- here a confirmation for another ticket, whose minting
        # writes as the User without passing through an action.
        note_only = {"blocked_by": [], "blocked_reason": "a note alone"}
        _s, _t, unspent = mint("PGU-2", "set_blockers", note_only)
        version1 = json.loads(prepare("PGU-1", "set_blockers", blockers)[1])["ticket_version"]
        kept = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
        kept_alive = []
        for path, body, extra in (("/api/tickets/PGU-2/actions/set_blockers", note_only,
                                   {"X-Ticket-Board-UI-Delegation": unspent or ""}),
                                  ("/api/ui/delegations", {"ticket_id": "PGU-1", "operation": "set_blockers",
                                                           "payload": blockers, "ticket_version": version1}, {})):
            kept.request("POST", path, body=json.dumps(body).encode(),
                         headers=dict(ui, **extra, **{"Content-Type": "application/json"}))
            response = kept.getresponse()
            kept_alive.append([response.status, response.read().decode("utf-8", "replace").splitlines()[0][:100]])
        kept.close()
        seen["kept alive"] = kept_alive + [sql(
            "SELECT count(*) FROM ticket_board.ui_delegations WHERE ticket_id = 'PGU-2' AND consumed_at IS NULL "
            "AND payload->>'blocked_reason' = 'a note alone';")]
        # Never lent: a workflow bypass, the review gates, and anything not the User's to confirm.
        refusals["force_move"] = short(mint("PGU-3", "force_move", {"state": "done", "assignee": "unassigned"}))
        refusals["gates"] = short(mint("PGU-3", "set_workflow_flags", {"needs_audit": False}))
        refusals["director mints"] = short(mint("PGU-3", "set_blockers", blockers, dict(ui, **{"X-Ticket-Board-Caller-Role": "director"})))
        refusals["minted without a session"] = short(mint("PGU-3", "set_blockers", blockers,
                                                          {k: v for k, v in ui.items() if k != "Cookie"}))
        seen["refusals"] = refusals
        # What a confirmation does do.
        done = {}
        for ticket, operation, payload in (
                ("PGU-4", "set_blockers", blockers),
                ("PGU-4", "set_manually_controlled", {"manually_controlled": True}),
                ("PGU-4", "set_manually_controlled", {"manually_controlled": False}),
                ("PGU-5", "route", route),
                ("PGU-5", "reassign", {"assignee": "app", "reason": "the User moves it to App"}),
                ("PGU-3", "cancel", {"reason": "the User cancels it"} if not declared else {"text": "the User cancels it"})):
            status, text, confirmation = mint(ticket, operation, payload)
            done[f"{operation} {ticket} {json.dumps(payload, sort_keys=True)[:30]}"] = (
                (lambda r: r[0] if r[0] == 200 else r)(act(ticket, operation, payload, confirmation))
                if confirmation else [status, text.splitlines()[0][:120]])
        seen["done"] = done
        # A withdrawal lowers the one approval it names, with why; a merge is bound to both tickets.
        fixture.psql(admin, "SET session_replication_role = replica; "
                            "UPDATE ticket_board.tickets SET audit_signoff = true WHERE id = 'PGU-6';")
        withdrawal = {"signoff": "audit_signoff", "reason": "the User withdraws the audit sign-off"}
        status, text, confirmation = mint("PGU-6", "withdraw_signoff", withdrawal)
        seen["withdrawn"] = [act("PGU-6", "withdraw_signoff", withdrawal, confirmation)[0] if confirmation else [status, text[:120]],
                             sql("SELECT audit_signoff FROM ticket_board.tickets WHERE id = 'PGU-6';")]
        seen["withdrawal_refusals"] = {
            "not given": short(mint("PGU-6", "withdraw_signoff", withdrawal)),
            "no reason": short(mint("PGU-6", "withdraw_signoff", {"signoff": "audit_signoff", "reason": " "})),
            "a gate": short(mint("PGU-6", "withdraw_signoff", {"signoff": "needs_audit", "reason": "lower the gate"})),
        }
        merge = {"target_id": "PGU-7"}
        status, text, confirmation = mint("PGU-6", "merge", merge)
        seen["merged"] = [act("PGU-6", "merge", merge, confirmation)[0] if confirmation else [status, text[:120]],
                          state("PGU-6").split("/")[0]]
        version7 = json.loads(prepare("PGU-7", "merge", {"target_id": "PGU-6"})[1])["ticket_version"]
        seen["merge_refusals"] = {"into itself": short(mint("PGU-7", "merge", {"target_id": "PGU-7"})),
                                  # past the page's preview, the database refuses it as well
                                  "into nothing": short(mint("PGU-7", "merge", {"target_id": "PGU-999"}, version=version7))}
        seen["merge_preview_of_nothing"] = short(prepare("PGU-7", "merge", {"target_id": "PGU-999"}))
        # A notice is named one way, and what the grant was checked against is what is dismissed.
        notices = {t: sql("INSERT INTO ticket_board.ticket_notification_queue (ticket_id, kind, target_role, message, "
                          f"payload, dedupe_key) VALUES ('{t}', 'transition', 'ops', '{t} for ops', '{{}}', 'syrd598:{t}') "
                          "RETURNING id;").splitlines()[0] for t in ("PGU-1", "PGU-2")}
        by_key = {"ticket_id": "PGU-1", "target_role": "ops", "kind": "transition", "reason": "the User dismisses it"}
        status, text, confirmation = mint("PGU-1", "dismiss_notification", by_key)
        seen["dismissal"] = {
            "both selectors": short(mint("PGU-1", "dismiss_notification", dict(by_key, notification_id=int(notices["PGU-2"])))),
            "both, director's pane": director_pane("PGU-1", "dismiss_notification",
                                                   dict(by_key, notification_id=int(notices["PGU-2"]))),
            "by key": act("PGU-1", "dismiss_notification", by_key, confirmation)[0] if confirmation else [status, text[:120]],
            "left": sql("SELECT string_agg(ticket_id, ',' ORDER BY ticket_id) FROM ticket_board.ticket_notification_queue "
                        "WHERE dedupe_key LIKE 'syrd598:%';")}
        seen["withdrawal_comment"] = sql("SELECT who || ': ' || text FROM ticket_board.ticket_comments "
                                         "WHERE ticket_id = 'PGU-6' AND text LIKE 'Withdrew %';")
        seen["states"] = {t: state(t) for t in ("PGU-3", "PGU-4", "PGU-5")}
        # The record: the User decided, the board UI lent the control role's permission.
        seen["comments"] = json.loads(sql(
            "SELECT coalesce(json_agg(json_build_array(ticket_id, who, left(text, 400)) ORDER BY id), '[]')::text "
            "FROM ticket_board.ticket_comments WHERE ticket_id IN ('PGU-4', 'PGU-5') AND text LIKE 'Through the board UI%';"))
        seen["authors"] = sorted(set(json.loads(sql(
            "SELECT coalesce(json_agg(DISTINCT who), '[]')::text FROM ticket_board.ticket_comments "
            "WHERE ticket_id IN ('PGU-3', 'PGU-4', 'PGU-5');"))))
        seen["notices"] = json.loads(sql(
            "SELECT coalesce(json_agg(json_build_array(target_role, left(message, 160)) ORDER BY id), '[]')::text "
            "FROM ticket_board.ticket_notification_queue WHERE ticket_id IN ('PGU-4', 'PGU-5') AND message LIKE '%board UI%';"))
        seen["journal"] = json.loads(sql(
            "SELECT coalesce(json_agg(json_build_array(ticket_id, operation, decided_by, authority, consumed_at IS NOT NULL) "
            "ORDER BY id), '[]')::text FROM ticket_board.ui_delegations WHERE ticket_id IN ('PGU-4', 'PGU-5');"))
        for srv in [*servers, unix]:
            srv.shutdown()
            srv.server_close()
    return seen


def database_cases(root: Path) -> dict:
    """The database on its own: a grant authorizes the User for its own operation, in its own transaction, only."""
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import secrets

    import psycopg
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    with temporary_cluster(prefix="d598", shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")
        fixture.seed_postgres_ticket(admin, "PGU-1", title="One", state="analysis", assignee="director", commit_exempt=True)
        fixture.seed_postgres_ticket(admin, "PGU-2", title="Two", state="analysis", assignee="director", commit_exempt=True)
        service = fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE)
        listener = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")

        def run(url, caller, statements):
            try:
                with psycopg.connect(url) as conn:
                    with conn.transaction():
                        conn.execute("SELECT set_config('ticket_board.caller_role', %s, true)", (caller,))
                        for statement, params in statements:
                            conn.execute(statement, params)
                return "ok"
            except psycopg.Error as exc:
                return str(exc).splitlines()[0][:120]

        def version(ticket):
            with psycopg.connect(service) as conn:
                value = conn.execute("SELECT ticket_board.ui_ticket_version(%s)", (ticket,)).fetchone()[0]
            return value.decode() if isinstance(value, bytes) else value

        session = secrets.token_urlsafe(32)
        payload = json.dumps({"blocked_by": ["PGU-2"], "blocked_reason": "waits"})
        mint = "SELECT ticket_board.mint_ui_delegation(%s, %s, %s, %s, %s::jsonb, %s, 120)"
        consume = "SELECT ticket_board.consume_ui_delegation(%s, %s, %s, %s, %s::jsonb)"
        set_blockers = ("SELECT ticket_board.set_blockers('PGU-1', ARRAY['PGU-2'], 'waits')", ())
        hold = ("SELECT ticket_board.set_manually_controlled('PGU-1', true)", ())
        token = secrets.token_urlsafe(32)
        seen["listener mints"] = run(listener, "user", [(mint, (token, session, "PGU-1", "set_blockers", payload, version("PGU-1")))])
        seen["mint"] = run(service, "user", [(mint, (token, session, "PGU-1", "set_blockers", payload, version("PGU-1")))])
        # Its operation only: in the transaction that consumed it, another Director-only operation is still refused.
        seen["other operation, same transaction"] = run(service, "user", [
            (consume, (token, session, "PGU-1", "set_blockers", payload)), hold])
        token2 = secrets.token_urlsafe(32)
        run(service, "user", [(mint, (token2, session, "PGU-1", "set_blockers", payload, version("PGU-1")))])
        seen["consumed, then its operation"] = run(service, "user", [
            (consume, (token2, session, "PGU-1", "set_blockers", payload)), set_blockers])
        # In a later transaction, the consumed grant authorizes nothing.
        seen["a later transaction"] = run(service, "user", [set_blockers])
        # Its decider only: a role the transaction turns into after the User consumed it is lent nothing.
        token4 = secrets.token_urlsafe(32)
        run(service, "user", [(mint, (token4, session, "PGU-1", "set_blockers", payload, version("PGU-1")))])
        seen["another caller, same transaction"] = run(service, "user", [
            (consume, (token4, session, "PGU-1", "set_blockers", payload)),
            ("SELECT set_config('ticket_board.caller_role', 'ops', true)", ()), set_blockers])
        token3 = secrets.token_urlsafe(32)
        run(service, "user", [(mint, (token3, session, "PGU-2", "set_blockers", payload, version("PGU-2")))])
        seen["another session"] = run(service, "user", [(consume, (token3, secrets.token_urlsafe(32), "PGU-2", "set_blockers", payload))])
        seen["director consumes"] = run(service, "director", [(consume, (token3, session, "PGU-2", "set_blockers", payload))])
        seen["names a commit"] = run(service, "user", [(mint, (secrets.token_urlsafe(32), session, "PGU-2", "route",
                                                               json.dumps({"target": "in_progress", "commit_hash": "a" * 40}),
                                                               version("PGU-2")))])
        # require_actor and require_publication_control cannot know their object; the object acted on must be
        # the one the consumed grant was confirmed for, checked in the transaction that consumed it: the grant's
        # ticket's, and the very notice (by id or by key) or request, decided as confirmed. Grants for PGU-1.
        def insert(statement):
            return fixture.psql(admin, statement).splitlines()[0]

        def notice(t, role, kind):
            return insert("INSERT INTO ticket_board.ticket_notification_queue (ticket_id, kind, target_role, message, "
                          f"payload, dedupe_key) VALUES ('{t}', '{kind}', '{role}', '{t} for {role}', '{{}}', "
                          f"'syrd598:{t}:{role}:{kind}') RETURNING id;")

        def request(t, by):
            return insert("INSERT INTO ticket_board.publication_requests (ticket_id, requested_by, ref, commit_hash, "
                          f"bundle_path) VALUES ('{t}', '{by}', 'roles/{by}/{t.lower()}', '{'b' * 40}', '/tmp/{t}.bundle') "
                          "RETURNING id;")

        notices = {"ops": notice("PGU-1", "ops", "transition"), "app": notice("PGU-1", "app", "transition"),
                   "kind": notice("PGU-1", "ops", "publication"), "PGU-2": notice("PGU-2", "ops", "transition")}
        requests = {"own": request("PGU-1", "app"), "same ticket": request("PGU-1", "main"), "PGU-2": request("PGU-2", "app")}

        def granted(operation, payload, then):
            grant = secrets.token_urlsafe(32)
            minted = run(service, "user", [(mint, (grant, session, "PGU-1", operation, json.dumps(payload), version("PGU-1")))])
            return minted if minted != "ok" else run(service, "user", [
                (consume, (grant, session, "PGU-1", operation, json.dumps(payload))), (then, ())])

        def left():
            return fixture.psql(admin, (
                "SELECT (SELECT string_agg(ticket_id || ':' || target_role || ':' || kind, ',' ORDER BY ticket_id, "
                "target_role, kind) FROM ticket_board.ticket_notification_queue WHERE dedupe_key LIKE 'syrd598:%') || ' ' "
                "|| (SELECT string_agg(ticket_id || ':' || requested_by || '=' || state, ',' ORDER BY ticket_id, "
                "requested_by) FROM ticket_board.publication_requests);")).strip()

        by_key = {"ticket_id": "PGU-1", "target_role": "ops", "kind": "transition", "reason": "r"}
        by_id = {"notification_id": int(notices["ops"]), "reason": "r"}
        rejected = {"request_id": int(requests["own"]), "outcome": "rejected", "detail": "no"}
        dismiss = "SELECT ticket_board.dismiss_notification({}, 'r')".format
        by_key_sql = "SELECT ticket_board.dismiss_notification_by_key('PGU-1', '{}', '{}', 'r')".format
        resolve = "SELECT ticket_board.resolve_publication({}, '{}', 'no', '{}')".format
        seen["bound objects"] = {
            "both selectors": run(service, "user", [(mint, (secrets.token_urlsafe(32), session, "PGU-1", "dismiss_notification",
                                                            json.dumps(dict(by_key, notification_id=int(notices["PGU-2"]))),
                                                            version("PGU-1")))]),
            "another ticket's notice": granted("dismiss_notification", by_key, dismiss(notices["PGU-2"])),
            "same ticket, another id": granted("dismiss_notification", by_id, dismiss(notices["app"])),
            "same ticket, another role": granted("dismiss_notification", by_key, by_key_sql("app", "transition")),
            "same ticket, another kind": granted("dismiss_notification", by_key, by_key_sql("ops", "publication")),
            "another ticket's request": granted("resolve_publication", rejected, resolve(requests["PGU-2"], "rejected", "")),
            "same ticket, another request": granted("resolve_publication", rejected,
                                                    resolve(requests["same ticket"], "rejected", "")),
            "same request, another outcome": granted("resolve_publication", rejected,
                                                     resolve(requests["own"], "published", "b" * 40)),
            "after the refusals": left(),
            "its own notice, by id": granted("dismiss_notification", by_id, dismiss(notices["ops"])),
            "its own notice, by key": granted("dismiss_notification", dict(by_key, target_role="APP ", kind=" Transition"),
                                              by_key_sql("app", "transition")),
            "its own request": granted("resolve_publication", rejected, resolve(requests["own"], "rejected", "")),
            "left": left()}

        # require_actor cannot know the ticket either, and other functions check the same action names: the
        # permission is lent only to the operation's own function, which enters the grant with the ticket it acts
        # on. A grant for PGU-1 lends nothing for another ticket, another destination or another function.
        fixture.seed_postgres_ticket(admin, "PGU-9", title="Nine", state="analysis", assignee="director", commit_exempt=True)

        def tickets():
            return fixture.psql(admin, "SELECT string_agg(id || ':' || state || ':' || manually_controlled, ' ' ORDER BY id) "
                                       "FROM ticket_board.tickets WHERE id IN ('PGU-1', 'PGU-2', 'PGU-9');").strip()

        before = tickets()
        hold = {"manually_controlled": True}
        into_two = {"target_id": "PGU-2"}
        seen["entered only"] = {
            "hold another ticket": granted("set_manually_controlled", hold,
                                           "SELECT ticket_board.set_manually_controlled('PGU-2', true)"),
            "block another ticket": granted("set_blockers", {"blocked_by": ["PGU-9"], "blocked_reason": "waits"},
                                            "SELECT ticket_board.set_blockers('PGU-2', ARRAY['PGU-9'], 'waits')"),
            "merge another ticket": granted("merge", into_two, "SELECT ticket_board.merge('PGU-2', 'PGU-1')"),
            "merge into another": granted("merge", into_two, "SELECT ticket_board.merge('PGU-1', 'PGU-9')"),
            "a size exception": granted("merge", into_two,
                                        "SELECT ticket_board.approve_size_exception('PGU-1', 'x.py', 'r', false, false)"),
            "size review": granted("merge", into_two,
                                   f"SELECT ticket_board.enable_size_review('{'a' * 40}', '[]', '[]', false)"),
            "a snooze": granted("set_manually_controlled", hold,
                                "SELECT ticket_board.snooze_reminders(ARRAY['PGU-1'], now() + interval '1 hour', 'r', false)"),
            "clearing a snooze": granted("set_manually_controlled", hold,
                                         "SELECT ticket_board.clear_reminder_snooze(1, ARRAY['PGU-1'], 'r', false)"),
            "a release, then blockers": granted("release_external_blocker", {"ref": "x:1", "reason": "r"},
                                                "SELECT ticket_board.set_blockers('PGU-1', ARRAY['PGU-9'], 'waits')"),
            # Entered for its own call only: after the hold, the same transaction's snooze is lent nothing.
            "a snooze after its own hold": granted("set_manually_controlled", hold, (
                "SELECT ticket_board.set_manually_controlled('PGU-1', true), "
                "ticket_board.snooze_reminders(ARRAY['PGU-1'], now() + interval '1 hour', 'r', false)")),
            "unchanged": tickets() == before, "before": before,
            "its own ticket": granted("set_manually_controlled", hold,
                                      "SELECT ticket_board.set_manually_controlled('PGU-1', true)"),
            "held": tickets().split()[0]}
        # Releasing an external blocker and recovering a stalled ticket enter their grants the same way.
        release = {"ref": "syrd:SYRD-1", "reason": "released upstream", "evidence": "https://example.invalid/1"}
        released = "SELECT ticket_board.release_external_blocker('{}', 'syrd:SYRD-1', 'released upstream', 'https://example.invalid/1')"
        recover = "SELECT ticket_board.recover_stalled_ticket('{}', 'r')"
        seen["entered only"].update({
            "an external blocker": run(service, "director", [(
                "SELECT ticket_board.set_blockers('PGU-1', ARRAY['syrd:SYRD-1'], 'Waiting on SYRD-1.')", ())]),
            "release another ticket's": granted("release_external_blocker", release, released.format("PGU-2")),
            "release its own": granted("release_external_blocker", release, released.format("PGU-1")),
            "recover, no grant": run(service, "user", [(recover.format("PGU-1"), ())]),
            "recover another ticket": granted("recover_stalled_ticket", {"reason": "r"}, recover.format("PGU-2")),
            "recover its own": granted("recover_stalled_ticket", {"reason": "r"}, recover.format("PGU-1"))})
        # The checks that know the ticket lend in the consuming transaction, to its decider, only.
        moved = {"assignee": "director", "reason": "r"}
        reassign = ("SELECT ticket_board.reassign('PGU-1', 'director', 'r')", ())
        consumed_earlier = granted("reassign", moved, "SELECT 1")
        token5 = secrets.token_urlsafe(32)
        run(service, "user", [(mint, (token5, session, "PGU-1", "reassign", json.dumps(moved), version("PGU-1")))])
        seen["entered only"].update({
            "reassign, its own": granted("reassign", moved, reassign[0]),  # lent: past the check, to its own
            "reassign, a later transaction": [consumed_earlier, run(service, "user", [reassign])],
            "reassign, another caller": run(service, "user", [
                (consume, (token5, session, "PGU-1", "reassign", json.dumps(moved))),
                ("SELECT set_config('ticket_board.caller_role', 'ops', true)", ()), reassign]),
            "reassign on a blockers grant": granted("set_blockers", {"blocked_by": ["PGU-2"], "blocked_reason": "waits"},
                                                    reassign[0])})
        # The grant consumed in this transaction is the one entered, even when a later-minted one was consumed
        # before it: A (PGU-1) minted, then B (PGU-2); B consumed first, then A, and A's action is A's.
        release_hold = {"manually_controlled": False}
        token_a, token_b = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        run(service, "user", [(mint, (token_a, session, "PGU-1", "set_manually_controlled", json.dumps(release_hold),
                                      version("PGU-1")))])
        run(service, "user", [(mint, (token_b, session, "PGU-2", "set_manually_controlled", json.dumps(hold), version("PGU-2")))])
        run(service, "user", [(consume, (token_b, session, "PGU-2", "set_manually_controlled", json.dumps(hold)))])
        seen["entered only"]["consumed out of order"] = run(service, "user", [
            (consume, (token_a, session, "PGU-1", "set_manually_controlled", json.dumps(release_hold))),
            ("SELECT ticket_board.set_manually_controlled('PGU-1', false)", ())])
        # The User's grant binds the User's action only: the Director deciding in the same transaction, on the
        # Director's own authority, is not held to it.
        token6 = secrets.token_urlsafe(32)
        run(service, "user", [(mint, (token6, session, "PGU-1", "resolve_publication", json.dumps(rejected), version("PGU-1")))])
        seen["entered only"]["the director's own decision"] = run(service, "user", [
            (consume, (token6, session, "PGU-1", "resolve_publication", json.dumps(rejected))),
            ("SELECT set_config('ticket_board.caller_role', 'director', true)", ()),
            (resolve(requests["same ticket"], "rejected", ""), ())])

        def lendable(op):
            with psycopg.connect(service) as conn:
                return conn.execute("SELECT ticket_board.ui_delegable('PGU-2', %s)", (op,)).fetchone()[0]

        # Never an approval or a relay of another role's decision, whatever a workflow lets the control role
        # do. The validator refuses a sign-off to a role named director, but not to a control role named
        # otherwise; this plants one, asks, and takes it back.
        fixture.seed_postgres_ticket(admin, "PGU-3", title="Three", state="audit", assignee="audit", commit_exempt=True)
        fixture.seed_postgres_ticket(admin, "PGU-4", title="Four", state="user_review", assignee="user",
                                     needs_user_signoff=True, commit_exempt=True)
        planted = json.dumps([{"from": "audit", "to": "dat", "action": "control_approves", "label": "Approve",
                               "actors": ["director"], "primitive": "approve", "require_commit": False}])
        seen["never lent"] = fixture.psql(admin, (
            "BEGIN; UPDATE ticket_board.workflow_configuration SET document = jsonb_set(document, '{transitions}', "
            f"(document->'transitions') || '{planted}'::jsonb) WHERE singleton; "
            "SELECT ticket_board.ui_delegable('PGU-3', 'control_approves') || '/' || "
            "ticket_board.ui_delegable('PGU-4', 'relay_user_kick_back'); ROLLBACK;")).strip().splitlines()
        # The withdrawal window lowers the one sign-off it names, and raises nothing -- that one included.
        try:  # the table is the definer functions' own; its triggers judge whoever writes
            seen["window raises"] = fixture.psql(admin, (
                "BEGIN; SELECT set_config('ticket_board.caller_role', 'director', true); "
                "SELECT set_config('ticket_board.signoff_withdrawal', 'PGU-3:audit_signoff', true); "
                "UPDATE ticket_board.tickets SET audit_signoff = true, "
                "workflow_flags = workflow_flags || '{\"audit_signoff\": true}'::jsonb WHERE id = 'PGU-3'; ROLLBACK;"))
        except AssertionError as exc:
            seen["window raises"] = str(exc).splitlines()[0][:160]

        seen["lendable"] = {op: lendable(op)
                            for op in ("route", "set_blockers", "reassign", "dismiss_notification", "merge",
                                       "withdraw_signoff", "force_move", "override_move", "set_workflow_flags",
                                       "configure_workflow", "approve_size_exception", "director_edit")}
        functions = ("mint_ui_delegation(text, text, text, text, jsonb, text, integer)",
                     "consume_ui_delegation(text, text, text, text, jsonb)", "ui_delegated_authority(text, text)",
                     "ui_delegable(text, text)", "ui_ticket_version(text)")
        seen["grantees"] = {f.split("(")[0]: fixture.psql(admin, (
            "SELECT coalesce(string_agg(DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END, '+'), '') "
            f"FROM pg_proc p, aclexplode(p.proacl) a WHERE p.oid = 'ticket_board.{f}'::regprocedure "
            "AND a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner;")).strip() for f in functions}
        patched = ("require_actor(text[], text)", "require_workflow_transition_actor(text, text, text)",
                   "perform_workflow_action(text, text, jsonb)", "reassign(text, text, text)",
                   "recover_stalled_ticket(text, text)", "require_publication_control()",
                   "notify_ticket_owner_in_place_change(text, text)", "dismiss_notification(bigint, text)",
                   "publication_request_ui_delegation_ticket()", "ui_delegation_for(text, text, text)",
                   "enter_ui_delegation(text, text, text)", "leave_ui_delegation()", "ui_entered_authority(text)",
                   "set_manually_controlled(text, boolean)", "set_blockers(text, text[], text)",
                   "release_external_blocker(text, text, text, text)", "merge(text, text)",
                   "dismiss_notification_by_key(text, text, text, text)") + functions
        digest = (" || ".join(f"md5(pg_get_functiondef('ticket_board.{f}'::regprocedure))" for f in patched)
                  + " || (SELECT md5(string_agg(attname || ' ' || format_type(atttypid, atttypmod), ', ' ORDER BY attnum)) "
                    "FROM pg_attribute WHERE attrelid = 'ticket_board.ui_delegations'::regclass AND attnum > 0 AND NOT attisdropped)"
                    " || (SELECT md5(pg_get_triggerdef(oid)) FROM pg_trigger "
                    "WHERE tgname = 'publication_requests_ui_delegation_ticket')")
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
        fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
        fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
        seen["fresh_equals_migrated"] = fixture.psql(fresh, f"SELECT {digest};").strip() == fixture.psql(admin, f"SELECT {digest};").strip()
    return seen


# A scripted client: stdlib only, run under other accounts. It fetches the page, asks what it may do, and
# tries to prepare, mint and act -- everything a process that reaches the port can do for itself.
SCRIPTED_CLIENT = r"""
import http.client, json, sys
port, write_token, ticket, payload, given = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4]), json.loads(sys.argv[5])
origin = "http://127.0.0.1:" + port
def call(method, path, body=None, extra=None):
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=30)
    headers = {"X-Ticket-Board-Write-Token": write_token, "X-Ticket-Board-Caller-Role": "user", "Origin": origin,
               "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json", **(extra or {})}
    conn.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.read().decode("utf-8", "replace"), resp.getheader("Set-Cookie") or ""
import os
out = {}
for line in open("/proc/self/uid_map"):  # this account as the host's kernel knows it
    inside, host, count = map(int, line.split())
    if inside <= os.getuid() < inside + count:
        out["host_uid"] = host + os.getuid() - inside
_s, _b, cookie = call("GET", "/")
out["cookie"] = bool(cookie)
session = {"Cookie": cookie.split(";", 1)[0] if cookie else given.get("cookie", "")}
status, text, _c = call("POST", "/api/ui/delegations", {"describe": True, "ticket_id": ticket}, session)
described = json.loads(text) if status == 200 else {}
out["describe"] = [status, sorted(op["operation"] for op in described.get("operations", []) if op["mode"] == "delegated"),
                   described.get("delegation_unavailable", "")]
status, text, _c = call("POST", "/api/ui/delegations", {"ticket_id": ticket, "operation": "set_blockers", "payload": payload,
                                                         "prepare": True}, session)
out["prepare"] = [status, text.splitlines()[0][:220] if text else ""]
version = json.loads(text)["ticket_version"] if status == 200 else "0" * 32
def mint(version):
    status, text, _c = call("POST", "/api/ui/delegations", {"ticket_id": ticket, "operation": "set_blockers",
                                                             "payload": payload, "ticket_version": version}, session)
    out["mint"] = [status, text.splitlines()[0][:220] if text else ""]
    return json.loads(text)["token"] if status == 200 else ""
grant = mint(version) or given.get("grant", "")
status, text, _c = call("POST", "/api/tickets/" + ticket + "/actions/set_blockers", payload,
                        dict(session, **{"X-Ticket-Board-UI-Delegation": grant}))
out["act"] = [status, text.splitlines()[0][:220] if text else ""]
# A grant left live for the ticket as it is now: what another account might carry.
status, text, _c = call("POST", "/api/ui/delegations", {"ticket_id": ticket, "operation": "set_blockers", "payload": payload,
                                                         "prepare": True}, session)
out["spare"] = {"cookie": session["Cookie"], "grant": mint(json.loads(text)["ticket_version"]) if status == 200 else ""}
print(json.dumps(out))
"""


def identity_cases(root: Path) -> dict:
    """Who may be the User, by the kernel's account of the connection and root's record of approved users:
    the scripted client run as the approved account and as another, and each way the record or the
    kernel's answer can fail -- every one of which turns the board UI's lending off."""
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import pwd
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board
    from board_ui_user_record import trust_board_ui_users
    from temporary_cluster import temporary_cluster

    from scripts.ticket_board import ui_user_identity as identity

    def as_account(ns_uid: int, args: list[str]) -> tuple[int, dict]:
        """Run the scripted client as a mapped subuid; returns its kernel uid and what it saw."""
        command = ["unshare", "--user", "--map-auto", "--map-root-user", "setpriv", f"--reuid={ns_uid}",
                   f"--regid={ns_uid}", "--clear-groups", sys.executable, "-I", "-c", SCRIPTED_CLIENT, *args]
        done = subprocess.run(command, cwd="/", capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr[-2000:]
        seen_by = json.loads(done.stdout.strip().splitlines()[-1])
        return int(seen_by.get("host_uid", -1)), seen_by

    seen: dict = {}
    with temporary_cluster(prefix="i598", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="i598-reg.") as reg:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        for n in (1, 2, 3):
            fixture.seed_postgres_ticket(admin, f"PGU-{n}", title=f"Ticket {n}", state="analysis", assignee="director",
                                         commit_exempt=True)
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = str(server.server_port)
        registry = Path(reg) / "projects"

        def client(ns_uid: int, ticket: str, given: dict | None = None) -> tuple[int, dict]:
            payload = {"blocked_by": ["PGU-3"], "blocked_reason": f"{ticket} waits on three"}
            return as_account(ns_uid, [port, server.write_token, ticket, json.dumps(payload), json.dumps(given or {})])

        # The kernel uid the approved account will have; root's record then names exactly it.
        trust_board_ui_users(registry, [])
        user_uid, _probe = client(1000, "PGU-1")
        seen["user_uid_is_another_account"] = user_uid not in (-1, os.getuid())
        user = {"account": "board-ui-user", "uid": user_uid}
        trust_board_ui_users(registry, [user])
        _uid, seen["the user"] = client(1000, "PGU-1")
        seen["the user blocked"] = ",".join(app.get_ticket("PGU-1")["blocked_by"])
        # Another account, carrying the User's own session cookie and a live grant the User minted.
        other_uid, seen["another account"] = client(1001, "PGU-1", seen["the user"]["spare"])
        seen["other_uid_is_another_account"] = other_uid not in (-1, os.getuid(), user_uid)
        seen["the user's grant unspent"] = fixture.psql(admin, (
            "SELECT count(*) FROM ticket_board.ui_delegations WHERE consumed_at IS NULL AND ticket_id = 'PGU-1';")).strip()

        def refused_for(note: str, users: list, **kwargs) -> None:
            """The approved account's own client, with root's record or the board in a state that must refuse."""
            trust_board_ui_users(registry, users, **kwargs)
            _uid, out = client(1000, "PGU-2")
            seen[note] = {"cookie": out["cookie"], "describe": out["describe"][2], "prepare": out["prepare"],
                          "act": out["act"][0]}

        # Accounts that are never a board UI user, beside the approved one: the record is refused whole.
        me = pwd.getpwuid(os.getuid()).pw_name
        refused_for("the project account recorded", [user, {"account": me, "uid": os.getuid()}],
                    excluded={"tenant_user": me})
        refused_for("root recorded", [user, {"account": "root", "uid": 0}])
        refused_for("a role account recorded", [user], excluded={"role_accounts": ["board-ui-user"]})
        # The tenant's EnvironmentFile can override the unit's environment: it may add exclusions, never lift root's.
        os.environ["TICKET_BOARD_TENANT_USER"] = "nobody"
        refused_for("root's exclusion against the environment", [user], excluded={"tenant_user": "board-ui-user"})
        os.environ.pop("TICKET_BOARD_TENANT_USER")
        # Nothing recorded; an account that no longer has the recorded uid; a record not root's alone.
        refused_for("none recorded", [])
        refused_for("no longer as recorded", [{"account": "board-ui-user", "uid": user_uid}],
                    accounts={"board-ui-user": user_uid + 7})
        refused_for("a record others can write", [user], mode=0o664)
        trust_board_ui_users(registry, [user])
        identity.RECORD_PATH = None  # a unit rendered before this ticket: no --board-ui-record
        _uid, out = client(1000, "PGU-2")
        seen["no record given"] = {"cookie": out["cookie"], "describe": out["describe"][2], "prepare": out["prepare"]}
        trust_board_ui_users(registry, [user])
        identity.TRUSTED_OWNER_UIDS = frozenset({0})  # production's rule: this test's own record is not root's
        _uid, out = client(1000, "PGU-2")
        seen["a record root does not own"] = {"cookie": out["cookie"], "describe": out["describe"][2], "prepare": out["prepare"]}
        # The kernel's answer fails closed: not loopback, no table, more than one owner.
        trust_board_ui_users(registry, [user])

        class Conn:
            def __init__(self, server, client):
                self.server, self.client = server, client

            def getsockname(self):
                return self.server

            def getpeername(self):
                return self.client

        table = Path(reg) / "tcp"
        row = "   0: {local} {remote} 01 00000000:00000000 00:00000000 00000000  {uid}        0 1 1 0000000000000000\n"
        local, remote = "0100007F:A000", "0100007F:1F90"  # 127.0.0.1:40960 -> 127.0.0.1:8080
        header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        table.write_text(header + row.format(local=local, remote=remote, uid=user_uid))
        loopback = Conn(("127.0.0.1", 8080), ("127.0.0.1", 40960))
        seen["peer: one owner"] = identity.tcp_peer_uid(loopback, (table,))
        # Off loopback, even a row the kernel lists for those endpoints (10.0.0.9:40960 -> 10.0.0.5:8080) names nobody.
        table.write_text(header + row.format(local="0900000A:A000", remote="0500000A:1F90", uid=user_uid))
        seen["peer: not loopback"] = identity.tcp_peer_uid(Conn(("10.0.0.5", 8080), ("10.0.0.9", 40960)), (table,))
        seen["peer: no table"] = identity.tcp_peer_uid(loopback, (Path(reg) / "missing",))
        table.write_text(header + row.format(local=local, remote=remote, uid=user_uid)
                         + row.format(local=local, remote=remote, uid=os.getuid()))
        seen["peer: two owners"] = identity.tcp_peer_uid(loopback, (table,))
        server.shutdown()
        server.server_close()
    return seen


def scenario(root: Path) -> dict:
    out = {"declared": board_cases(root, True), "legacy": board_cases(root, False)}
    if (root / "scripts/ticket_board/ui_delegation.py").exists():
        out["database"] = database_cases(root)
    if (root / "scripts/ticket_board/ui_user_identity.py").exists():
        out["identity"] = identity_cases(root)
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2])), default=str))
        return 0
    cached = Path(os.environ["SYRD598_BEFORE_JSON"]) if os.environ.get("SYRD598_BEFORE_JSON") else None
    if cached is not None and cached.is_file():
        before = json.loads(cached.read_text())
    else:
        with tempfile.TemporaryDirectory(prefix="syrd598.") as tmp:
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario",
                                    str(tree_at(BEFORE, Path(tmp) / "before"))],
                                   capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900)
            assert child.returncode == 0, child.stderr[-3000:]
            before = json.loads(child.stdout.strip().splitlines()[-1])
        if cached is not None:
            cached.write_text(json.dumps(before))
    after = json.loads(json.dumps(scenario(ROOT), default=str))
    print("OBSERVED " + json.dumps({"before": before, "after": after}), file=sys.stderr)

    for kind in ("declared", "legacy"):
        b, a = before[kind], after[kind]
        check(b["prepare"][0] == 404 and b["user_alone"] == [403, 403] and b["director_unchanged"] == 200
              and [r[0] for r in b["forged_director_header"]] == [200, 200],
              f"{kind}, main: the board UI has no confirmation path, and a request that names `director` in a header "
              f"is obeyed: {b}")
        check([r[0] for r in a["forged_director_header"]] == [403, 403] and a["forged_inspector_header"][0] == 403
              and all("acts as the User" in r[1] for r in [*a["forged_director_header"], a["forged_inspector_header"]]),
              f"{kind}: a role named in an HTTP header is refused, director or any other: {a['forged_director_header']}, "
              f"{a['forged_inspector_header']}")
        check(a["cookie"] == [200, "tb_ui_session", True, True],
              f"{kind}: the board page carries an HttpOnly, SameSite=Strict session: {a['cookie']}")
        check(a["user_alone"] == [403, 403] and a["director_unchanged"] == 200,
              f"{kind}: the same payload as the User without a confirmation is refused; the Director's pane works: "
              f"{a['user_alone']}, {a['director_unchanged']}")
        check(a["prepare_detail"] == {"ticket_id": "PGU-1", "operation": "set_blockers", "authority": "director",
                                      "state": "analysis", "assignee": "director"},
              f"{kind}: the confirmation shows the concrete ticket, action and current state: {a['prepare_detail']}")
        r = a["refusals"]
        check(r["used"][0] == 200 and a["used_state"] == ["PGU-7"],
              f"{kind}: the confirmed action is done once: {r['used']}, {a['used_state']}")
        refused = {name: result for name, result in r.items() if name != "used" and result[0] != 403}
        check(not refused, f"{kind}: every forged, replayed, rebound, expired, stale or unlendable request is refused "
                           f"with a 403: {refused}")
        check("already used" in r["replay"][1] and "was for set_blockers on PGU-1" in r["another ticket"][1]
              and "changed since it was confirmed" in r["altered payload"][1] and "expired" in r["expired"][1]
              and "changed since it was confirmed" in r["stale ticket"][1] and "reload" in r["after a restart"][1]
              and "cannot lend force_move" in r["force_move"][1]
              and r["another operation, before it runs"][1] == "this board UI confirmation was for set_blockers on "
                                                                "PGU-3, not measure_size on PGU-3"
              and int(a["grant unspent"]) >= 1,
              f"{kind}: each refusal says why, plainly: {r}, unspent {a['grant unspent']}")
        if kind == "declared":
            check(a["no longer lendable"] == [403, "the board UI can no longer lend route for PGU-8"],
                  f"{kind}: a grant for a move the ticket's stage no longer offers is refused for that reason, before "
                  f"anything runs: {a['no longer lendable']}")
        check(a["minted without the version shown"] == [400, "a board UI confirmation needs the ticket version it was shown"],
              f"{kind}: a grant is minted only for the version the User was shown: {a['minted without the version shown']}")
        check(a["kept alive"][0][0] == 400 and a["kept alive"][1][0] == 200 and a["kept alive"][2] == "1",
              f"{kind}: a confirmation one request left unspent is not spent by the next on the same connection: "
              f"{a['kept alive']}")
        check(all(result == 200 for result in a["done"].values()),
              f"{kind}: route, reassign, blockers, manual control and cancel are done when the User confirms them: {a['done']}")
        check(a["states"]["PGU-5"] == "in_progress/app/false" and a["states"]["PGU-4"].endswith("/false")
              and a["states"]["PGU-3"].startswith("cancelled/"),
              f"{kind}: each took effect: {a['states']}")
        check(len(a["comments"]) == 5 and all(c[1] == "user" and "using the operation permission of director" in c[2]
                                              for c in a["comments"]) and "director" not in a["authors"],
              f"{kind}: the history says the User decided and the board UI lent the Director's operation permission; "
              f"nothing is recorded as the Director's: {a['comments']}, {a['authors']}")
        check(a["withdrawn"] == [200, "f"] and a["withdrawal_comment"] == "user: Withdrew audit_signoff: the User withdraws "
              "the audit sign-off" and all(r[0] == 403 for r in a["withdrawal_refusals"].values()),
              f"{kind}: a withdrawal lowers the one named approval, with why, as the User; one not given, without a "
              f"reason, or of a gate is refused: {a['withdrawn']}, {a['withdrawal_comment']}, {a['withdrawal_refusals']}")
        check(a["merged"][0] == 200 and a["merged"][1] in ("done", "cancelled")
              and all(r == [403, "a merge names another ticket to merge PGU-7 into"] for r in a["merge_refusals"].values())
              and a["merge_preview_of_nothing"] == [404, "ticket not found: PGU-999"],
              f"{kind}: a merge is bound to both tickets; into itself or into nothing is refused: {a['merged']}, "
              f"{a['merge_refusals']}, {a['merge_preview_of_nothing']}")
        n = a["dismissal"]
        check(n["both selectors"][0] == 403 and "one, not both" in n["both selectors"][1] and n["both, director's pane"] == 400
              and n["by key"] == 200 and n["left"] == "PGU-2",
              f"{kind}: a notice is named one way, at the mint and on the Director's own pane, so the notice a "
              f"confirmation was checked against is the one dismissed; another ticket's stays: {n}")
        check(len(a["journal"]) == 5 and all(row[2:] == ["user", "director", True] for row in a["journal"]),
              f"{kind}: each confirmation is journalled, decided by the User with the Director's permission: {a['journal']}")
        check(all("changed by user (through the board UI" in n[1] for n in a["notices"]),
              f"{kind}: notices name the User and the board UI, never the Director: {a['notices']}")
    i = after["identity"]
    check(i["user_uid_is_another_account"] and i["other_uid_is_another_account"],
          f"the scripted clients ran as accounts other than this process's, by the kernel's account: {i}")
    check(i["the user"]["cookie"] and i["the user"]["describe"][1] and i["the user"]["prepare"][0] == 200
          and i["the user"]["mint"][0] == 200 and i["the user"]["act"][0] == 200 and i["the user blocked"] == "PGU-3",
          f"the approved board UI user's own account is given a session and can confirm a Director operation: "
          f"{i['the user']}, {i['the user blocked']}")
    another = i["another account"]
    check(not another["cookie"] and another["describe"][0] == 200 and another["describe"][1] == []
          and "not an approved board UI user" in another["describe"][2]
          and another["prepare"][0] == 403 and "this connection belongs to" in another["prepare"][1]
          and another["mint"][0] == 403 and another["act"][0] == 403 and "not an approved board UI user" in another["act"][1]
          and i["the user's grant unspent"] == "1",
          f"another account -- a scripted client that fetches the page, invents the same-origin headers and even "
          f"carries the User's cookie and live grant -- gets no session, is offered nothing, and every prepare, mint "
          f"and action is refused: {another}, unspent {i['the user\'s grant unspent']}")
    for note, said in (("the project account recorded", "never a board UI user"), ("root recorded", "which is root"),
                       ("a role account recorded", "which is a role account"),
                       ("root's exclusion against the environment", "which is the project account"),
                       ("no record given", "started without its project record"),
                       ("none recorded", "no approved board UI user is recorded"),
                       ("no longer as recorded", "no longer exists as recorded"),
                       ("a record others can write", "is writable by others"),
                       ("a record root does not own", "is not owned by root")):
        r = i[note]
        check(not r["cookie"] and said in r["describe"] and "an operator must configure" in r["describe"]
              and r["prepare"][0] == 403 and said in r["prepare"][1],
              f"{note}: the board UI lends nothing, even to the approved account, and says why and who can fix it: {r}")
    check(i["peer: one owner"] == i["the user"]["host_uid"] and i["peer: not loopback"] is None
          and i["peer: no table"] is None and i["peer: two owners"] is None,
          f"the kernel's answer names one loopback owner or nothing: {i['peer: one owner']}, {i['peer: not loopback']}, "
          f"{i['peer: no table']}, {i['peer: two owners']}")
    d = after["database"]
    check(d["listener mints"].startswith("permission denied") and d["mint"] == "ok"
          and "cannot call set_manually_controlled" in d["other operation, same transaction"]
          and d["consumed, then its operation"] == "ok" and "cannot call set_blockers" in d["a later transaction"]
          and "ops cannot call set_blockers" in d["another caller, same transaction"],
          f"the database lends the permission for the grant's own operation, in the transaction that consumed it, "
          f"only: {d}")
    check("false/false" in d["never lent"] and "changes only that sign-off" in d["window raises"],
          f"no approval or relay is ever lent, and the withdrawal window raises nothing: {d['never lent']}, "
          f"{d['window raises']}")
    check("another board session" in d["another session"] and "only for the User" in d["director consumes"]
          and "names no commit" in d["names a commit"],
          f"a grant is the User's and its session's, and names no commit: {d}")
    b = d["bound objects"]
    refused = ("another ticket's notice", "same ticket, another id", "same ticket, another role",
               "same ticket, another kind", "another ticket's request", "same ticket, another request",
               "same request, another outcome")
    untouched = "PGU-1:app:transition,PGU-1:ops:publication,PGU-1:ops:transition,PGU-2:ops:transition " \
                "PGU-1:app=requested,PGU-1:main=requested,PGU-2:app=requested"
    check("one, not both" in b["both selectors"]
          and "is PGU-2's, not the ticket" in b["another ticket's notice"] and "notification" in b["another ticket's notice"]
          and all("is not the notice this board UI confirmation was for" in b[k] for k in refused[1:4])
          and "is PGU-2's, not the ticket" in b["another ticket's request"] and "publication request" in b["another ticket's request"]
          and all("is not the decision this board UI confirmation was for" in b[k] for k in refused[5:])
          and b["after the refusals"] == untouched
          and b["its own notice, by id"] == b["its own notice, by key"] == b["its own request"] == "ok"
          and b["left"] == "PGU-1:ops:publication,PGU-2:ops:transition "
                           "PGU-1:app=rejected,PGU-1:main=requested,PGU-2:app=requested",
          f"a grant dismisses or resolves only the notice or publication request it was confirmed for, decided as "
          f"confirmed, in the database, whatever the HTTP layer passes: {b}")
    e = d["entered only"]
    check(all("is PGU-2's, not the ticket this board UI confirmation was for" in e[k]
              for k in ("hold another ticket", "block another ticket", "merge another ticket"))
          and "merging into PGU-9 is not what this board UI confirmation was for" in e["merge into another"]
          and "role user cannot call merge" in e["a size exception"] and "role user cannot call merge" in e["size review"]
          and "role user cannot call set_manually_controlled" in e["a snooze"]
          and "role user cannot call set_manually_controlled" in e["clearing a snooze"]
          and "role user cannot call set_blockers" in e["a release, then blockers"]
          and "role user cannot call set_manually_controlled" in e["a snooze after its own hold"]
          and e["unchanged"] is True and e["its own ticket"] == "ok" and e["held"] == "PGU-1:analysis:true"
          and "is PGU-2's, not the ticket" in e["release another ticket's"] and e["an external blocker"] == "ok"
          and e["release its own"] == "ok" and "cannot call recover_stalled_ticket" in e["recover, no grant"]
          and "is PGU-2's, not the ticket" in e["recover another ticket"]
          and "nothing to recover" in e["recover its own"]  # lent: past the authority check, to its own
          and "already assigned to director" in e["reassign, its own"]
          and e["reassign, a later transaction"][0] == "ok"
          and "role user cannot call reassign" in e["reassign, a later transaction"][1]
          and "role ops cannot call reassign" in e["reassign, another caller"]
          and "role user cannot call reassign" in e["reassign on a blockers grant"]
          and e["the director's own decision"] == "ok" and e["consumed out of order"] == "ok",
          f"a grant lends the control role's permission to its own operation's function, for its own ticket and "
          f"destination, and never to another function that checks the same action name: {e}")
    check(d["lendable"] == {"route": True, "set_blockers": True, "reassign": True, "dismiss_notification": True,
                            "merge": True, "withdraw_signoff": True, "force_move": False, "override_move": False,
                            "set_workflow_flags": False, "configure_workflow": False, "approve_size_exception": False,
                            "director_edit": False},
          f"only operations with a meaningful UI control are lent, never a bypass, a gate or the workflow: {d['lendable']}")
    check(all(v == "ticket_board_service" for k, v in d["grantees"].items() if k != "ui_delegated_authority")
          and d["grantees"]["ui_delegated_authority"] == "" and d["fresh_equals_migrated"] is True,
          f"only the board service mints or consumes; schema.sql installs the same: {d['grantees']}, {d['fresh_equals_migrated']}")
    print(f"board_ui_delegation_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
