#!/usr/bin/env python3
"""SYRD-718, in a real browser: the User decides UAT from a phone, through the board's private Tailscale link.

A production-built board with the shipped workflow and the real server, behind a stand-in for this host's Tailscale
Serve (tests/tailscale_serve_standin.py) that terminates TLS for a `*.ts.net` name as tailscaled does; Chromium, at a
phone's size, resolves that name to the stand-in and opens the notification's link, `https://<name>/#ticket=<id>`.
Main runs first, from a git archive of its own scripts, examples and tests, then this tree; the same steps on each:

- the link opens the exact ticket in User Review, with Ask Director, Accept and Send to Director;
- Accept records the User's sign-off and moves the ticket on, once; Send to Director records the message and moves the
  ticket to User Feedback, once;
- the board restarts with a new write token while the page is open: the page's stale token is refused, it asks
  /api/client-config for the new one, and Accept is done, once, without a reload;
- the same link opened as another tailnet login gets no write token, and no Accept or Send to Director.

On main the phone was offered Ask Director alone: "Accept and Send to Director are unavailable".
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "a6a28fed87036abad75de52db3674e33070cd0f0"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
LOGIN = "uat-user@example.com"
PHONE = f"login:{LOGIN}@100.101.102.103"
STRANGER = "login:someone-else@example.com@100.101.102.104"
TAILNET_NAME = "board.tail0718.ts.net"
A = "a" * 40
CHECKS = 0


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
    from board_ui_user_record import trust_board_ui_users
    from playwright.sync_api import sync_playwright
    from temporary_cluster import temporary_cluster

    from scripts.ticket_board import uat_instructions, ui_delegation
    from scripts.ticket_board import ui_user_identity as identity

    ui_delegation.TRUST_HEADER_ROLES = False  # production: HTTP is the User's
    # This tree's stand-in on either run, by its path: this tree's tests directory on sys.path would bring this tree's
    # fixtures, and with them its scripts.
    spec = importlib.util.spec_from_file_location("tailscale_serve_standin",
                                                  Path(__file__).resolve().parent / "tailscale_serve_standin.py")
    standin_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(standin_module)
    Standin = standin_module.Standin
    out: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="b718-tls.") as tls:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board",
                                                                   fixture.SERVICE_ROLE))
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        for n in range(1, 5):
            ticket = f"PGU-{n}"
            fixture.seed_postgres_ticket(admin, ticket, title=f"Phone UAT {ticket}", state="dat", assignee="director",
                                         needs_audit=True, audit_signoff=True, needs_user_signoff=True, commit_hash=A)
            uat_instructions.perform(app, "set_uat_instructions", {"do_this": "Open the link on the phone.",
                                                                   "you_should_see": "This ticket."},
                                     caller_role="director", ticket_id=ticket)
            app.perform_workflow_action(ticket, "director_dat_sign_off", {}, caller_role="director")

        def row(ticket: str) -> list:
            return sql(f"SELECT state || '|' || user_signoff::text || '|' || (SELECT count(*) FROM "
                       f"ticket_board.ticket_comments c WHERE c.ticket_id = t.id AND c.who = 'user')::text "
                       f"FROM ticket_board.tickets t WHERE id = '{ticket}';").split("|")

        # The desktop account is someone else: a request from this process is never the desktop's.
        trust_board_ui_users(cluster.root / "registry", [{"account": "board-ui-user", "uid": 4242424,
                                                          "tailnet_login": LOGIN}])
        identity.TAILNET_PROXY_UIDS = frozenset({os.getuid()})  # the stand-in is this process: production's root
        cert, key = Path(tls) / "cert.pem", Path(tls) / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj",
                        f"/CN={TAILNET_NAME}", "-addext", f"subjectAltName=DNS:{TAILNET_NAME}", "-keyout", str(key),
                        "-out", str(cert)], check=True, capture_output=True, timeout=60)
        servers: list = []

        def start_board(port: int = 0):
            server = fixture.TicketBoardServer(("127.0.0.1", port), app, director_notifier=fixture.QuietNotifier())
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
            return server

        board = start_board()
        standin = Standin(board.server_port, peer=PHONE, cert=str(cert), key=str(key)).start()
        link = f"https://{TAILNET_NAME}:{standin.server_address[1]}/#ticket="
        try:
            with sync_playwright() as playwright:
                launch: dict = {"headless": True, "args": [f"--host-resolver-rules=MAP {TAILNET_NAME} 127.0.0.1"]}
                if Path("/opt/google/chrome/chrome").is_file():
                    launch["executable_path"] = "/opt/google/chrome/chrome"
                browser = playwright.chromium.launch(**launch)
                phone = {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True,
                         "ignore_https_errors": True}  # the stand-in's certificate is its own, not Let's Encrypt's
                page = browser.new_context(**phone).new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                seen: list = []
                page.on("response", lambda r: seen.append([r.request.method, r.url.split("/", 3)[-1], r.status]))

                def wait_for(predicate, seconds: float = 10.0) -> bool:
                    end = time.monotonic() + seconds
                    while time.monotonic() < end:
                        if predicate():
                            return True
                        page.wait_for_timeout(100)
                    return predicate()

                def panel(ticket: str) -> dict:
                    """The UAT panel of the ticket the link opened, once the board has said what the User may do."""
                    selector = f"[data-uat-panel='{ticket}']"
                    page.locator(selector).wait_for(timeout=15000)
                    wait_for(lambda: page.locator(f"{selector} [data-uat-decision], {selector} .soft-note").count() > 0)
                    return {"open": page.locator(".detail-modal [data-uat-panel]").get_attribute("data-uat-panel"),
                            "ask": page.locator(f"{selector} .uat-ask").count(),
                            "decisions": page.locator(f"{selector} [data-uat-decision]").evaluate_all(
                                "els => els.map(e => e.getAttribute('data-uat-decision'))"),
                            "note": " ".join(page.locator(f"{selector} .soft-note").all_text_contents())[:300]}

                def confirm() -> None:
                    dialog = page.locator(".board-ui-dialog")
                    dialog.wait_for(timeout=5000)
                    dialog.locator("[data-board-ui-confirm]").click()

                page.goto(link + "PGU-1", wait_until="domcontentloaded")
                out["PGU-1 panel"] = panel("PGU-1")
                out["page token"] = page.evaluate("ticketBoardWriteToken()") == board.write_token
                if out["PGU-1 panel"]["decisions"]:
                    page.locator("[data-uat-panel='PGU-1'] [data-uat-decision='accept']").tap()
                    confirm()
                    wait_for(lambda: row("PGU-1")[1] == "true")
                out["PGU-1"] = row("PGU-1")
                page.evaluate("window.location.hash = '#ticket=PGU-2'")
                out["PGU-2 panel"] = panel("PGU-2")
                if out["PGU-2 panel"]["decisions"]:
                    page.locator("[data-uat-panel='PGU-2'] [data-uat-message]").fill("Stopped after a restart, on the phone.")
                    page.locator("[data-uat-panel='PGU-2'] [data-uat-decision='send']").tap()
                    confirm()
                    wait_for(lambda: row("PGU-2")[0] == "user_feedback")
                out["PGU-2"] = row("PGU-2")
                out["PGU-2 message"] = sql("SELECT count(*) FROM ticket_board.ticket_comments WHERE ticket_id = 'PGU-2' "
                                           "AND who = 'user' AND text LIKE '%Stopped after a restart, on the phone.%';")
                # A restart while the page is open: a new process, a new write token, the same port.
                page.evaluate("window.location.hash = '#ticket=PGU-3'")
                out["PGU-3 panel"] = panel("PGU-3")
                old = board.write_token
                board.shutdown()
                board.server_close()
                board = start_board(standin.board_port)
                out["restart: new token"] = board.write_token != old
                del seen[:]
                if out["PGU-3 panel"]["decisions"]:
                    page.locator("[data-uat-panel='PGU-3'] [data-uat-decision='accept']").tap()
                    confirm()
                    wait_for(lambda: row("PGU-3")[1] == "true")
                out["PGU-3"] = row("PGU-3")
                refused = next((i for i, (method, _p, status) in enumerate(seen) if method == "POST" and status == 403), None)
                out["restart: stale token refused, then asked"] = refused is not None and any(
                    path == "api/client-config" for _m, path, _s in seen[refused:])
                out["restart: page holds the new token"] = page.evaluate("ticketBoardWriteToken()") == board.write_token
                out["errors"] = errors
                # The same link, as someone else on the tailnet.
                standin.peer = STRANGER
                other = browser.new_context(**phone).new_page()
                page = other
                other.goto(link + "PGU-4", wait_until="domcontentloaded")
                out["stranger panel"] = panel("PGU-4")
                out["stranger token"] = other.evaluate("ticketBoardWriteToken()")
                out["PGU-4"] = row("PGU-4")
                browser.close()
        finally:
            standin.stop()
            servers[-1].shutdown()
            servers[-1].server_close()
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
    check(got["PGU-1 panel"]["open"] == "PGU-1" and got["PGU-1 panel"]["ask"] == 1,
          f"main: the link opens the ticket, with Ask Director: {got['PGU-1 panel']}")
    check(got["PGU-1 panel"]["decisions"] == [] and "Accept and Send to Director are unavailable" in got["PGU-1 panel"]["note"],
          f"main: but no Accept or Send to Director: {got['PGU-1 panel']}")
    check(got["PGU-1"] == ["user_review", "false", "0"], f"main: so the User cannot accept from the phone: {got['PGU-1']}")


def candidate(got: dict) -> None:
    check(got["PGU-1 panel"] == {"open": "PGU-1", "ask": 1, "decisions": ["accept", "send"], "note": ""},
          f"the link opens the exact ticket, with Ask Director, Accept and Send to Director: {got['PGU-1 panel']}")
    check(got["page token"], "the phone's page carries the write token")
    check(got["PGU-1"] == ["director_review", "true", "0"], f"Accept records the sign-off and moves on, once: {got['PGU-1']}")
    check(got["PGU-2 panel"]["decisions"] == ["accept", "send"], f"a second link, by hash: {got['PGU-2 panel']}")
    check(got["PGU-2"][0] == "user_feedback" and got["PGU-2 message"] == "1",
          f"Send to Director records the message and moves to User Feedback, once: {got['PGU-2']}")
    check(got["restart: new token"] and got["restart: stale token refused, then asked"],
          "after a restart the page's stale token is refused, and it asks /api/client-config for the new one")
    check(got["restart: page holds the new token"] and got["PGU-3"] == ["director_review", "true", "0"],
          f"and Accept is done, once, without a reload: {got['PGU-3']}")
    check(got["errors"] == [], f"no page errors: {got['errors']}")
    check(got["stranger panel"]["decisions"] == [] and "not one root recorded" in got["stranger panel"]["note"]
          and got["stranger token"] == "",
          f"another tailnet login, on the same link: no token, no Accept or Send: {got['stranger panel']}")
    check(got["PGU-4"] == ["user_review", "false", "0"], f"and the ticket is untouched: {got['PGU-4']}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), sys.argv[3])))
        return 0
    if os.environ.get("SYRD718_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
        with tempfile.TemporaryDirectory(prefix="syrd718-browser-before.", dir="/tmp") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before"), "b718b"))
    candidate(run(ROOT, "b718a"))
    print(f"tailnet_uat_browser_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
