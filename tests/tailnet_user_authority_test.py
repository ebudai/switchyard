#!/usr/bin/env python3
"""SYRD-718: the User's phone, through this host's Tailscale Serve, decides the User's own UAT, and nothing else does.

A production-built board with the shipped workflow and the real HTTP server, reached three ways, each by a different
account the kernel tells apart (unshare's subuids, as tests/board_ui_delegation_test.py runs its clients):

- through a stand-in for tailscaled's Serve proxy (tests/tailscale_serve_standin.py), run as its own account, which
  the board is told is the proxy's -- root's in production;
- directly over loopback by the approved desktop account, whose root-recorded entry also names its tailnet login;
- directly over loopback by another local account, sending the headers the proxy would.

Main runs first, from a git archive of its own scripts, examples and tests, then this tree; the same steps on each:

- the approved login through the proxy: the page and /api/client-config carry the write token, the board describes
  Accept and Send to Director (and lends no Director operation), and Accept, Send to Director and Ask Director each
  happen exactly once -- a second Accept or Send is refused;
- another tailnet login (a copied notification link), a tagged device, Funnel, the host's own traffic, a page by a name
  that is neither `*.ts.net` nor a tailnet address, and the proxy's headers sent by a local account, the approved
  desktop's included: no write token, no description, and a write with the real token refused, the ticket untouched;
- a record without the login, or with a malformed one: refused;
- the desktop's own loopback page keeps its kernel-UID lending, and another account still gets none;
- a page left open while the ticket was accepted elsewhere is refused; and after the board restarts with a new write
  token, the phone's stale token is refused, /api/client-config gives the approved login the new one, and the retry
  is done, once.
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
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "a6a28fed87036abad75de52db3674e33070cd0f0"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
LOGIN = "uat-user@example.com"
PHONE = f"login:{LOGIN}@100.101.102.103"
TAILNET_NAME = "board.tail0718.ts.net"
A = "a" * 40
CHECKS = 0

#: A local account's own client: the requests it is given, sent straight to the board, and what each answered.
CLIENT = r"""
import http.client, json, os, sys
port, requests = int(sys.argv[1]), json.loads(sys.argv[2])
out = {"answers": []}
for line in open("/proc/self/uid_map"):  # this account as the host's kernel knows it
    inside, host, count = map(int, line.split())
    if inside <= os.getuid() < inside + count:
        out["host_uid"] = host + os.getuid() - inside
for method, path, headers, body in requests:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=headers)
    resp = conn.getresponse()
    out["answers"].append([resp.status, resp.read().decode("utf-8", "replace")])
print(json.dumps(out))
"""


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from board_ui_delegation_test import SCRIPTED_CLIENT
    from board_ui_user_record import trust_board_ui_users
    from temporary_cluster import temporary_cluster

    from scripts.ticket_board import uat_instructions, ui_delegation
    from scripts.ticket_board import ui_user_identity as identity

    ui_delegation.TRUST_HEADER_ROLES = False  # production: HTTP is the User's
    standin_source = Path(__file__).resolve().parent / "tailscale_serve_standin.py"  # this tree's, on either run

    def subuid(ns_uid: int, program: list[str], **popen) -> subprocess.Popen:
        return subprocess.Popen(["unshare", "--user", "--map-auto", "--map-root-user", "setpriv", f"--reuid={ns_uid}",
                                 f"--regid={ns_uid}", "--clear-groups", sys.executable, "-I", *program],
                                cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, **popen)

    def as_account(ns_uid: int, script: str, args: list[str]) -> dict:
        child = subuid(ns_uid, ["-c", script, *args])
        stdout, stderr = child.communicate(timeout=120)
        assert child.returncode == 0, stderr[-2000:]
        return json.loads(stdout.strip().splitlines()[-1])

    out: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board",
                                                                   fixture.SERVICE_ROLE))
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        def enter_review(ticket: str) -> None:
            fixture.seed_postgres_ticket(admin, ticket, title=f"Phone UAT {ticket}", state="dat", assignee="director",
                                         needs_audit=True, audit_signoff=True, needs_user_signoff=True, commit_hash=A)
            uat_instructions.perform(app, "set_uat_instructions", {"do_this": "Open the board on the phone.",
                                                                   "you_should_see": "This ticket."},
                                     caller_role="director", ticket_id=ticket)
            app.perform_workflow_action(ticket, "director_dat_sign_off", {}, caller_role="director")

        for n in range(1, 7):
            enter_review(f"PGU-{n}")
        for n in (7, 8, 9):
            fixture.seed_postgres_ticket(admin, f"PGU-{n}", title=f"Desktop {n}", state="analysis", assignee="director",
                                         commit_exempt=True)

        def ticket_row(ticket: str) -> list:
            return sql(f"SELECT state || '|' || user_signoff::text || '|' || (SELECT count(*) FROM "
                       f"ticket_board.ticket_comments c WHERE c.ticket_id = t.id AND c.who = 'user')::text "
                       f"FROM ticket_board.tickets t WHERE id = '{ticket}';").split("|")

        servers: list = []

        def start_board(port: int = 0):
            server = fixture.TicketBoardServer(("127.0.0.1", port), app, director_notifier=fixture.QuietNotifier())
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
            return server

        board = start_board()
        port = board.server_port
        registry = cluster.root / "registry"

        # -- the three accounts the kernel tells apart --------------------------------------------------------------
        trust_board_ui_users(registry, [])
        desktop_uid = as_account(1000, CLIENT, [str(port), "[]"])["host_uid"]
        stranger_uid = as_account(1001, CLIENT, [str(port), "[]"])["host_uid"]
        proxy = subuid(1500, ["-c", standin_source.read_text(), str(port)])  # by text: it cannot read this checkout
        standin_port = int(proxy.stdout.readline())
        proxy_uid = int(next(line.split()[1] for line in Path(f"/proc/{proxy.pid}/status").read_text().splitlines()
                             if line.startswith("Uid:")))
        out["accounts differ"] = len({desktop_uid, stranger_uid, proxy_uid, os.getuid()}) == 4

        def record(users: list | None = None) -> None:
            """Root's record: the desktop account, with its tailnet login; then the proxy's account named as root's."""
            trust_board_ui_users(registry, users if users is not None else [
                {"account": "board-ui-user", "uid": desktop_uid, "tailnet_login": LOGIN}])
            identity.TAILNET_PROXY_UIDS = frozenset({proxy_uid})  # production: root, as tailscaled runs

        record()
        host = f"{TAILNET_NAME}:{standin_port}"

        def via(peer: str, method: str, path: str, body: object = None, token: str = "", *, name: str = host,
                origin: str | None = None, plain: bool = False) -> list:
            """One request from a browser on the tailnet, through the stand-in, as `peer` (`plain`: serve --http)."""
            conn = http.client.HTTPConnection("127.0.0.1", standin_port, timeout=30)
            headers = {"Host": name, "X-Standin-Peer": peer, "Origin": origin or f"https://{name}",
                       "Sec-Fetch-Site": "same-origin", "X-Ticket-Board-Caller-Role": "user",
                       "Content-Type": "application/json", "X-Ticket-Board-Write-Token": token,
                       **({"X-Standin-Plain": "1"} if plain else {})}
            conn.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=headers)
            resp = conn.getresponse()
            return [resp.status, resp.read().decode("utf-8", "replace")]

        def page_token(answer: list) -> str:
            marker = "window.TICKET_BOARD_WRITE_TOKEN = "
            return json.loads(answer[1].split(marker, 1)[1].split(";", 1)[0]) if marker in answer[1] else "<no page>"

        def config_token(answer: list) -> str:
            return json.loads(answer[1]).get("write_token", "") if answer[0] == 200 else f"<{answer[0]}>"

        def described(answer: list) -> dict:
            if answer[0] != 200:
                return {"status": answer[0], "refused": answer[1].splitlines()[0][:300] if answer[1] else ""}
            body = json.loads(answer[1])
            return {"status": 200, "uat": sorted(op.get("uat_decision", "") for op in body["operations"]
                                                 if op.get("placement") == "uat_panel"),
                    "delegated": sorted(op["operation"] for op in body["operations"] if op["mode"] == "delegated"),
                    "unavailable": body.get("delegation_unavailable", "")}

        def short(answer: list) -> list:
            return [answer[0], answer[1].splitlines()[0][:300] if answer[1] else ""]

        def tried(peer: str, ticket: str, **kw) -> dict:
            """Everything a page needs to act as the User, as `peer`, and a write with the board's real token."""
            return {"page": page_token(via(peer, "GET", "/", **kw)),
                    "config": config_token(via(peer, "GET", "/api/client-config", **kw)),
                    "describe": described(via(peer, "POST", "/api/ui/delegations", {"describe": True, "ticket_id": ticket},
                                              board.write_token, **kw)),
                    "accept": short(via(peer, "POST", f"/api/tickets/{ticket}/actions/user_sign_off",
                                        {"target": "director_review"}, board.write_token, **kw)),
                    "row": ticket_row(ticket)}

        token = board.write_token
        out["write token"] = token
        # -- the approved login, through the proxy -------------------------------------------------------------------
        out["phone page"] = page_token(via(PHONE, "GET", "/"))
        out["phone config"] = config_token(via(PHONE, "GET", "/api/client-config"))
        out["phone describe"] = described(via(PHONE, "POST", "/api/ui/delegations", {"describe": True, "ticket_id": "PGU-1"},
                                              token))
        out["phone accept"] = short(via(PHONE, "POST", "/api/tickets/PGU-1/actions/user_sign_off",
                                        {"target": "director_review"}, token))
        out["phone accept again"] = short(via(PHONE, "POST", "/api/tickets/PGU-1/actions/user_sign_off",
                                              {"target": "director_review"}, token))
        out["PGU-1"] = ticket_row("PGU-1")
        out["phone send"] = short(via(PHONE, "POST", "/api/tickets/PGU-2/actions/user_send_to_director",
                                      {"target": "user_feedback", "text": "It said stopped on the phone."}, token))
        out["phone send again"] = short(via(PHONE, "POST", "/api/tickets/PGU-2/actions/user_send_to_director",
                                            {"target": "user_feedback", "text": "It said stopped on the phone."}, token))
        out["PGU-2"] = ticket_row("PGU-2")
        out["PGU-2 message"] = sql("SELECT count(*) FROM ticket_board.ticket_comments WHERE ticket_id = 'PGU-2' AND "
                                   "who = 'user' AND text LIKE '%It said stopped on the phone.%';")
        out["phone ask"] = short(via(PHONE, "POST", "/api/tickets/PGU-3/actions/ask_director",
                                     {"question": "Which terminal?"}, token))
        out["PGU-3"] = ticket_row("PGU-3")
        out["PGU-3 told"] = sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-3' "
                                "AND target_role = 'director' AND payload->>'kind' = 'uat_question';")
        # Never a Director operation over the tailnet, even for the User's own login.
        blockers = {"blocked_by": ["PGU-8"], "blocked_reason": "from the phone"}
        out["phone prepare"] = short(via(PHONE, "POST", "/api/ui/delegations", {"ticket_id": "PGU-9", "operation":
                                                                                "set_blockers", "payload": blockers,
                                                                                "prepare": True}, token))
        out["phone mint"] = short(via(PHONE, "POST", "/api/ui/delegations", {"ticket_id": "PGU-9", "operation":
                                                                             "set_blockers", "payload": blockers,
                                                                             "ticket_version": "0" * 32}, token))
        # A cross-site page on the phone: no description, as on the desktop.
        out["phone other origin"] = described(via(PHONE, "POST", "/api/ui/delegations",
                                                  {"describe": True, "ticket_id": "PGU-4"}, token,
                                                  origin="https://evil.example"))

        # -- everyone else, through the proxy or around it -----------------------------------------------------------
        out["another login"] = tried(f"login:someone-else@example.com@100.101.102.104", "PGU-4")
        out["tagged"] = tried("tagged@100.101.102.105", "PGU-4")
        out["funnel"] = tried("funnel", "PGU-4")
        out["host itself"] = tried("local", "PGU-4")
        out["another name"] = tried(PHONE, "PGU-4", name=f"board.example.com:{standin_port}")  # a rebinding site's
        out["plain http serve"] = tried(PHONE, "PGU-4", plain=True)
        # The tailnet name alone, sent straight to the board by this process (the board service's account here).
        direct = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        direct.request("GET", "/api/client-config", headers={"Host": host})
        out["tailnet name, no proxy"] = config_token([200, direct.getresponse().read().decode()])
        direct = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        direct.request("POST", "/api/tickets/PGU-4/actions/user_sign_off", body=b'{"target": "director_review"}',
                       headers={"Host": host, "X-Ticket-Board-Write-Token": token, "X-Ticket-Board-Caller-Role": "user",
                                "Content-Type": "application/json"})
        answer = direct.getresponse()
        out["tailnet name, no proxy: accept"] = short([answer.status, answer.read().decode()])
        out["tailnet address page"] = config_token(via(PHONE, "GET", "/api/client-config", name=f"100.64.0.1:{standin_port}"))
        spoof = {"Host": host, "Origin": f"https://{host}", "Sec-Fetch-Site": "same-origin",
                 "X-Forwarded-Host": host, "X-Forwarded-Proto": "https", "X-Forwarded-For": "100.101.102.103",
                 "Tailscale-User-Login": LOGIN, "Tailscale-User-Name": "uat-user",
                 "X-Ticket-Board-Caller-Role": "user", "X-Ticket-Board-Write-Token": token,
                 "Content-Type": "application/json"}
        steps = [["GET", "/", spoof, None], ["GET", "/api/client-config", spoof, None],
                 ["POST", "/api/ui/delegations", spoof, {"describe": True, "ticket_id": "PGU-4"}],
                 ["POST", "/api/tickets/PGU-4/actions/user_sign_off", spoof, {"target": "director_review"}]]
        for label, ns_uid in (("spoof: another account", 1001), ("spoof: the desktop account", 1000)):
            answers = as_account(ns_uid, CLIENT, [str(port), json.dumps(steps)])["answers"]
            out[label] = {"page": page_token(answers[0]), "config": config_token(answers[1]),
                          "describe": described(answers[2]), "accept": short(answers[3]), "row": ticket_row("PGU-4")}
        record([{"account": "board-ui-user", "uid": desktop_uid}])
        out["no login recorded"] = tried(PHONE, "PGU-4")
        record([{"account": "board-ui-user", "uid": desktop_uid, "tailnet_login": "uat user"}])
        out["malformed login recorded"] = tried(PHONE, "PGU-4")
        record([{"account": "board-ui-user", "uid": desktop_uid, "tailnet_login": LOGIN.upper()}])
        out["login recorded in capitals"] = config_token(via("login:Uat-User@example.COM@100.101.102.103", "GET",
                                                             "/api/client-config"))
        record()

        # -- the desktop's own loopback page, as before --------------------------------------------------------------
        payload = {"blocked_by": ["PGU-8"], "blocked_reason": "from the desktop"}
        desk = as_account(1000, SCRIPTED_CLIENT, [str(port), token, "PGU-7", json.dumps(payload), "{}"])
        out["desktop"] = {"cookie": desk["cookie"], "describe": desk["describe"], "act": desk["act"][0],
                          "blocked": ",".join(app.get_ticket("PGU-7")["blocked_by"])}
        other = as_account(1001, SCRIPTED_CLIENT, [str(port), token, "PGU-9", json.dumps(payload), "{}"])
        out["another account"] = {"cookie": other["cookie"], "describe": other["describe"][:2], "act": other["act"][0],
                                  "blocked": ",".join(app.get_ticket("PGU-9")["blocked_by"])}
        loop = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        loop.request("GET", "/api/client-config")
        out["loopback config"] = json.loads(loop.getresponse().read()).get("write_token", "")

        # -- a page left open, and a board restart --------------------------------------------------------------------
        out["stale page describe"] = described(via(PHONE, "POST", "/api/ui/delegations",
                                                   {"describe": True, "ticket_id": "PGU-5"}, token))
        app.perform_workflow_action("PGU-5", "user_sign_off", {}, caller_role="user")  # done on the desktop meanwhile
        out["stale page accept"] = short(via(PHONE, "POST", "/api/tickets/PGU-5/actions/user_sign_off",
                                             {"target": "director_review"}, token))
        out["PGU-5"] = ticket_row("PGU-5")
        board.shutdown()
        board.server_close()
        board = start_board(port)  # a new process's write token, on the same port behind the same proxy
        out["restarted token differs"] = board.write_token != token
        out["restart: stale token"] = short(via(PHONE, "POST", "/api/tickets/PGU-6/actions/user_sign_off",
                                                {"target": "director_review"}, token))
        out["restart: another login's config"] = config_token(via("login:someone-else@example.com@100.101.102.104",
                                                                  "GET", "/api/client-config"))
        fresh = config_token(via(PHONE, "GET", "/api/client-config"))
        out["restart: config gives the new token"] = fresh == board.write_token
        out["restart: describe"] = described(via(PHONE, "POST", "/api/ui/delegations",
                                                 {"describe": True, "ticket_id": "PGU-6"}, fresh))
        out["restart: retry"] = short(via(PHONE, "POST", "/api/tickets/PGU-6/actions/user_sign_off",
                                          {"target": "director_review"}, fresh))
        out["restart: retry again"] = short(via(PHONE, "POST", "/api/tickets/PGU-6/actions/user_sign_off",
                                                {"target": "director_review"}, fresh))
        out["PGU-6"] = ticket_row("PGU-6")
        proxy.kill()
        proxy.wait(timeout=30)
        for server in servers[-1:]:
            server.shutdown()
            server.server_close()
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path, prefix: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), prefix],
                           capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(got: dict) -> None:
    """Main: the phone is offered nothing to decide with, and anyone through the proxy is the User."""
    check(got["accounts differ"], "main: four accounts the kernel tells apart")
    check(got["phone describe"]["status"] == 403 and "own page" in got["phone describe"]["refused"],
          f"main: the phone is described nothing, so Accept and Send to Director never show: {got['phone describe']}")
    check(got["another login"]["page"] == got["write token"] and got["another login"]["accept"][0] == 200,
          f"main: a copied link's stranger gets the write token and accepts as the User: {got['another login']}")
    check(got["spoof: another account"]["page"] == got["write token"],
          f"main: and a local account sending the proxy's headers gets the token too: {got['spoof: another account']}")


def refused_whole(got: dict, label: str, why: str) -> None:
    seen = got[label]
    check(seen["page"] == "" and seen["config"] == "", f"{label}: no write token, on the page or asked for: {seen}")
    check(seen["describe"]["status"] == 403 and why in seen["describe"]["refused"],
          f"{label}: nothing described, saying why ({why}): {seen['describe']}")
    check(seen["accept"][0] == 403 and why in seen["accept"][1],
          f"{label}: and the real token does not make it the User: {seen['accept']}")
    check(seen["row"] == ["user_review", "false", "0"], f"{label}: PGU-4 untouched: {seen['row']}")


def candidate(got: dict) -> None:
    token = got["write token"]
    check(got["accounts differ"], "four accounts the kernel tells apart")
    # The approved login, through the proxy.
    check(got["phone page"] == token and got["phone config"] == token,
          "the phone's page, and /api/client-config, carry the write token")
    check(got["phone describe"] == {"status": 200, "uat": ["accept", "send"], "delegated": [],
                                    "unavailable": "the board UI lends Director operations only on the desktop, never "
                                                   "over the tailnet"},
          f"Accept and Send to Director are described, and no Director operation: {got['phone describe']}")
    check(got["phone accept"][0] == 200 and got["PGU-1"][:2] == ["director_review", "true"],
          f"Accept records the User's sign-off and moves the ticket on: {got['phone accept']} {got['PGU-1']}")
    check(got["phone accept again"][0] in (400, 403), f"once: a second Accept is refused: {got['phone accept again']}")
    check(got["phone send"][0] == 200 and got["PGU-2"][0] == "user_feedback" and got["PGU-2 message"] == "1",
          f"Send to Director records the message and moves to User Feedback: {got['phone send']} {got['PGU-2']}")
    check(got["phone send again"][0] in (400, 403) and got["PGU-2 message"] == "1",
          f"once: a second Send is refused: {got['phone send again']}")
    check(got["phone ask"][0] == 200 and got["PGU-3"][0] == "user_review" and got["PGU-3 told"] == "1",
          f"Ask Director still works, the ticket stays: {got['phone ask']} {got['PGU-3']} {got['PGU-3 told']}")
    for label in ("phone prepare", "phone mint"):
        check(got[label][0] == 403 and "never over the tailnet" in got[label][1],
              f"{label}: no Director operation over the tailnet: {got[label]}")
    check(got["phone other origin"]["status"] == 403 and "own page" in got["phone other origin"]["refused"],
          f"another site's page on the phone is described nothing: {got['phone other origin']}")
    # Everyone else.
    refused_whole(got, "another login", "someone-else@example.com is not one root recorded")
    refused_whole(got, "tagged", "named no tailnet user")
    refused_whole(got, "funnel", "Tailscale Funnel")
    refused_whole(got, "host itself", "named no tailnet user")
    refused_whole(got, "another name", "by its tailnet name (*.ts.net) or address over HTTPS")
    refused_whole(got, "plain http serve", "by its tailnet name (*.ts.net) or address over HTTPS")
    check(got["tailnet name, no proxy"] == "" and got["tailnet name, no proxy: accept"][0] == 403
          and "not this host's Tailscale Serve" in got["tailnet name, no proxy: accept"][1],
          f"the tailnet name sent straight to the board is no one's: {got['tailnet name, no proxy: accept']}")
    check(got["login recorded in capitals"] == token, "a login is the same login whatever its case, recorded or named")
    check(got["tailnet address page"] == token, "a page by the host's tailnet address, as a link may name it, is the User's")
    refused_whole(got, "spoof: another account", "not this host's Tailscale Serve")
    refused_whole(got, "spoof: the desktop account", "not this host's Tailscale Serve")
    refused_whole(got, "no login recorded", f"{LOGIN} is not one root recorded")
    refused_whole(got, "malformed login recorded", "malformed")
    # The desktop, as before.
    check(got["desktop"]["cookie"] and "set_blockers" in got["desktop"]["describe"][1] and not got["desktop"]["describe"][2]
          and got["desktop"]["act"] == 200 and got["desktop"]["blocked"] == "PGU-8",
          f"the desktop's own loopback page still lends Director operations: {got['desktop']}")
    check(not got["another account"]["cookie"] and got["another account"]["describe"] == [200, []]
          and got["another account"]["act"] != 200 and got["another account"]["blocked"] == "",
          f"another local account still gets none: {got['another account']}")
    check(got["loopback config"] == token, "a loopback page still gets the write token")
    # A stale page; a restart.
    check(got["stale page describe"]["uat"] == ["accept", "send"] and got["stale page accept"][0] in (400, 403)
          and got["PGU-5"][:2] == ["director_review", "true"],
          f"a page left open while the ticket was accepted elsewhere is refused: {got['stale page accept']}")
    check(got["restarted token differs"] and got["restart: stale token"][0] == 403
          and "write-token" in got["restart: stale token"][1].lower(),
          f"after a restart the phone's old token is refused, as a stale token: {got['restart: stale token']}")
    check(got["restart: another login's config"] == "", "and another login is given no new one")
    check(got["restart: config gives the new token"] and got["restart: describe"]["uat"] == ["accept", "send"],
          f"the approved login is given the new token, and Accept is described again: {got['restart: describe']}")
    check(got["restart: retry"][0] == 200 and got["restart: retry again"][0] in (400, 403)
          and got["PGU-6"][:2] == ["director_review", "true"],
          f"the retry is done, once: {got['restart: retry']} {got['restart: retry again']} {got['PGU-6']}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), sys.argv[3])))
        return 0
    if os.environ.get("SYRD718_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
        with tempfile.TemporaryDirectory(prefix="syrd718-before.", dir="/tmp") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before"), "s718b"))
    candidate(run(ROOT, "s718a"))
    print(f"tailnet_user_authority_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
