#!/usr/bin/env python3
"""SYRD-669, in a real browser: a board page kept open across a board restart keeps its User actions.

A board process mints its write token, and its board UI sessions, when it
starts, so a restart replaces both under a page that stays open. Here the real
server is stopped and a new one started on the same port with another token,
under a production-built board, while Chromium keeps the real page open:

* a fresh page's UAT panel offers Accept and Send to Director, and every write
  the page sends names the User as its caller;
* after a restart the next redraw of the open ticket asks /api/client-config for
  the new token and still offers both, with no reload, and what the User was
  typing is still there;
* a restart between discovery and Accept: Accept is done, after one refused try
  with the old token and one with the new;
* a Director operation the User confirms (a lent operation): after a restart its
  confirmation is prepared, and after a restart between its grant and its
  request a new grant is minted under the new process's session and used once;
* a new build behind the restart still leaves the page to refresh; a write that
  meets a new build behind the same token, or is refused on a new build, still
  reloads the page and is sent once after it;
* a token still refused after the refresh is not tried again;
* no token is kept in the page's storage or written to its console, and every
  write goes to the board's own origin.

The before-run is main's code, from a git archive, with the same steps.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = 0
A = "a" * 40
TOKENS = {name: f"syrd669-token-{name}-" + name * 24 for name in "ABCDEFGH"}
DRAFT = "It said stopped after the restart."
COMMENT = "A comment I have not sent yet."
STALE = "missing or invalid X-Ticket-Board-Write-Token"


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenario(root: Path) -> dict:
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from playwright.sync_api import sync_playwright
    from temporary_cluster import temporary_cluster

    from scripts.ticket_board import uat_instructions, ui_delegation

    ui_delegation.TRUST_HEADER_ROLES = False  # production: HTTP is the User's
    seen: dict = {}
    with temporary_cluster(prefix="b669", shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board",
                                                                   fixture.SERVICE_ROLE))
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        for ticket in ("PGU-1", "PGU-4"):
            fixture.seed_postgres_ticket(admin, ticket, title=f"Try it {ticket}", state="dat", assignee="director",
                                         needs_audit=True, audit_signoff=True, needs_user_signoff=True, commit_hash=A)
            uat_instructions.perform(app, "set_uat_instructions", {"do_this": "Open the board.", "you_should_see": "It."},
                                     caller_role="director", ticket_id=ticket)
            app.perform_workflow_action(ticket, "director_dat_sign_off", {}, caller_role="director")
        for ticket, title in (("PGU-2", "Route me after a restart"), ("PGU-3", "Route me across a restart")):
            fixture.seed_postgres_ticket(admin, ticket, title=title, state="analysis", assignee="director",
                                         commit_exempt=True)
        seen["arranged"] = sql("SELECT string_agg(id || ':' || state, ',' ORDER BY id) FROM ticket_board.tickets;")

        live: dict = {}
        database_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX", "PG"))}

        def start(token: str, build: str = "") -> None:
            """The board process: stopped, as a restart stops it, and a new one on the same port."""
            port = live["port"] if live else 0
            if live:
                live["server"].kill()
                live["server"].wait(timeout=30)
            server = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--serve", str(root), database_url,
                 str(cluster.root), str(port), token, build],
                stdout=subprocess.PIPE, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
            ready = server.stdout.readline().split()
            assert ready[:1] == ["serving"], f"the board process did not start: {ready}"
            live.update(server=server, port=int(ready[1]))

        start(TOKENS["A"])
        origin = f"http://127.0.0.1:{live['port']}"
        writes: list[dict] = []
        console: list[str] = []
        errors: list[str] = []
        try:
            with sync_playwright() as playwright:
                launch: dict = {"headless": True}
                if Path("/opt/google/chrome/chrome").is_file():
                    launch["executable_path"] = "/opt/google/chrome/chrome"
                browser = playwright.chromium.launch(**launch)
                page = browser.new_page(viewport={"width": 1280, "height": 1000})
                page.on("console", lambda message: console.append(message.text))
                page.on("pageerror", lambda error: errors.append(f"{error} @ {len(writes)} {getattr(error, 'stack', '')}"))

                def recorded(response) -> None:
                    request = response.request
                    if request.method != "POST":
                        return
                    headers = request.headers
                    try:
                        body = json.loads(request.post_data or "{}")
                    except ValueError:
                        body = {}
                    # A board UI request is a description, a confirmation's preparation, or its grant.
                    kind = "describe" if body.get("describe") else "prepare" if body.get("prepare") else "mint"
                    path = request.url.removeprefix(origin)
                    writes.append({"url": request.url, "path": f"{path} {kind}" if path == "/api/ui/delegations" else path,
                                   "token": headers.get("x-ticket-board-write-token", ""),
                                   "role": headers.get("x-ticket-board-caller-role", ""),
                                   "grant": bool(headers.get("x-ticket-board-ui-delegation")),
                                   "status": response.status})
                page.on("response", recorded)

                def since(mark: int, path: str) -> list[str]:
                    page.wait_for_timeout(500)  # the last response's event, after the database already shows its work
                    named = {v: k for k, v in TOKENS.items()}
                    return [f"{named.get(w['token'], '?')}{w['status']}" for w in writes[mark:] if w["path"] == path]

                def wait_for(predicate, seconds: float = 15.0) -> bool:
                    end = time.monotonic() + seconds
                    while time.monotonic() < end:
                        if predicate():
                            return True
                        page.wait_for_timeout(100)
                    return predicate()

                def open_ticket(ticket: str, marker: str) -> None:
                    page.keyboard.press("Escape")
                    page.locator(".card", has=page.locator(".card-id", has_text=ticket)).first.click()
                    page.locator(marker).wait_for(timeout=10000)

                def note_text(selector: str) -> str:
                    return " | ".join(page.locator(selector).all_inner_texts())

                page.goto(origin + "/", wait_until="domcontentloaded")
                page.locator(".card-id", has_text="PGU-1").first.wait_for(timeout=10000)
                page.evaluate("window.__syrd669 = 'the same page'")

                # -- a fresh page -----------------------------------------------------------------------------
                panel = "[data-uat-panel='PGU-1']"
                open_ticket("PGU-1", panel)
                seen["fresh_buttons"] = wait_for(lambda: page.locator(f"{panel} [data-uat-decision='accept']").count() == 1
                                                 and page.locator(f"{panel} [data-uat-decision='send']").count() == 1)
                seen["fresh_note"] = note_text(f"{panel} .soft-note")
                seen["fresh_describe"] = since(0, "/api/ui/delegations describe")
                if not seen["fresh_buttons"]:  # nothing after this can be asked of a page that never had them
                    seen.update(writes=writes, console=console, errors=errors, origin=origin)
                    return seen

                # -- restarted: the next redraw recovers the token --------------------------------------------
                page.locator(f"{panel} [data-uat-message]").fill(DRAFT)
                page.locator("textarea[placeholder='Add a comment or bounce-back note']").fill(COMMENT)
                page.locator(f"{panel} [data-uat-message]").evaluate("el => { el.dataset.drawn = 'before'; }")
                mark = len(writes)
                start(TOKENS["B"])
                sql("SELECT ticket_board.append_ticket_comment('PGU-2', 'director', $c$Another ticket changed.$c$);")
                seen["redrawn"] = wait_for(lambda: page.locator(f"{panel} [data-uat-message]").count() == 1
                                           and page.locator(f"{panel} [data-uat-message]").get_attribute("data-drawn") is None,
                                           seconds=30)
                seen["restart_buttons"] = wait_for(
                    lambda: page.locator(f"{panel} [data-uat-decision='accept']").count() == 1
                    and page.locator(f"{panel} [data-uat-decision='send']").count() == 1
                    or bool(note_text(f"{panel} .soft-note")))
                seen["restart_note"] = note_text(f"{panel} .soft-note")
                seen["restart_describe"] = since(mark, "/api/ui/delegations describe")
                seen["same_page"] = page.evaluate("window.__syrd669 || ''")
                seen["draft_kept"] = page.locator(f"{panel} [data-uat-message]").input_value()
                seen["comment_kept"] = page.locator("textarea[placeholder='Add a comment or bounce-back note']").input_value()

                # -- restarted between discovery and Accept -----------------------------------------------------
                if page.locator(f"{panel} [data-uat-decision='accept']").count() == 0:
                    # The before-run: only a reload brings the buttons back.
                    page.reload(wait_until="domcontentloaded")
                    page.locator(".card-id", has_text="PGU-1").first.wait_for(timeout=10000)
                    open_ticket("PGU-1", f"{panel} [data-uat-decision='accept']")
                mark = len(writes)
                start(TOKENS["C"])
                page.locator(f"{panel} [data-uat-decision='accept']").click()
                page.locator("[data-board-ui-dialog='user_sign_off'] [data-board-ui-confirm]").click()
                seen["accepted"] = wait_for(
                    lambda: sql("SELECT user_signoff FROM ticket_board.tickets WHERE id='PGU-1';") == "t")
                seen["accept_writes"] = since(mark, "/api/tickets/PGU-1/actions/user_sign_off")

                # -- a lent operation, restarted after its button was drawn ---------------------------------------
                lent = "[data-board-ui-actions='PGU-2'] [data-board-ui-op='route']"
                open_ticket("PGU-2", "[data-board-ui-actions='PGU-2']")
                seen["lent_offered"] = wait_for(lambda: page.locator(lent).count() == 1, seconds=10)
                seen["lent_offered_note"] = note_text("[data-board-ui-actions='PGU-2'] .soft-note")
                mark = len(writes)
                start(TOKENS["D"])
                seen["lent_error"] = "route was not offered"
                if seen["lent_offered"]:
                    page.locator(lent).click()
                    dialog = page.locator("[data-board-ui-dialog='route']")
                    dialog.locator("[data-board-ui-field='assignee']").select_option("main")
                    dialog.locator("[data-board-ui-review]").click()
                    wait_for(lambda: dialog.locator("[data-board-ui-summary]").count() == 1
                             or bool(dialog.locator("[data-board-ui-error]").inner_text()), seconds=10)
                    seen["lent_error"] = dialog.locator("[data-board-ui-error]").inner_text()
                    if dialog.locator("[data-board-ui-confirm]").count():
                        dialog.locator("[data-board-ui-confirm]").click()
                    else:
                        dialog.locator("[data-board-ui-cancel]").click()
                seen["lent_routed"] = wait_for(
                    lambda: sql("SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id='PGU-2';")
                    == "in_progress/main", seconds=10)
                seen["lent_prepare"] = since(mark, "/api/ui/delegations prepare")
                seen["lent_mint"] = since(mark, "/api/ui/delegations mint")
                seen["lent_action"] = since(mark, "/api/tickets/PGU-2/actions/route")

                # -- a lent operation, restarted between its grant and its request --------------------------------
                page.reload(wait_until="domcontentloaded")  # the page and its session as fresh as they come
                page.locator(".card-id", has_text="PGU-3").first.wait_for(timeout=10000)
                open_ticket("PGU-3", "[data-board-ui-actions='PGU-3'] [data-board-ui-op='route']")
                restarted = []

                def restart_first(route) -> None:
                    if not restarted:
                        restarted.append(True)
                        start(TOKENS["E"])
                    route.continue_()

                page.route("**/api/tickets/PGU-3/actions/route", restart_first)
                mark = len(writes)
                page.locator("[data-board-ui-actions='PGU-3'] [data-board-ui-op='route']").click()
                dialog = page.locator("[data-board-ui-dialog='route']")
                dialog.locator("[data-board-ui-field='assignee']").select_option("app")
                dialog.locator("[data-board-ui-review]").click()
                dialog.locator("[data-board-ui-confirm]").click()
                seen["crossed_routed"] = wait_for(
                    lambda: sql("SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id='PGU-3';")
                    == "in_progress/app", seconds=10)
                page.wait_for_timeout(500)
                seen["crossed_prepare"] = since(mark, "/api/ui/delegations prepare")
                seen["crossed_mint"] = since(mark, "/api/ui/delegations mint")
                seen["crossed_action"] = since(mark, "/api/tickets/PGU-3/actions/route")
                seen["crossed_grants"] = sql(
                    "SELECT count(*) || ' grants, ' || count(DISTINCT session_sha256) || ' sessions, '"
                    " || count(consumed_txid) || ' used' FROM ticket_board.ui_delegations WHERE ticket_id = 'PGU-3';")
                seen["crossed_status"] = page.locator("#createStatus").inner_text()
                page.unroute("**/api/tickets/PGU-3/actions/route")

                # -- a new build behind the restart -----------------------------------------------------------
                panel = "[data-uat-panel='PGU-4']"
                open_ticket("PGU-4", panel)
                seen["build_buttons_before"] = wait_for(
                    lambda: page.locator(f"{panel} [data-uat-decision='accept']").count() == 1, seconds=5)
                mark = len(writes)
                start(TOKENS["F"], build="syrd669-next-build")
                sql("SELECT ticket_board.append_ticket_comment('PGU-2', 'director', $c$And again.$c$);")
                wait_for(lambda: page.evaluate("state.refreshRequired === true"), seconds=30)
                seen["build_refresh_required"] = page.evaluate("state.refreshRequired === true")
                seen["build_describe"] = since(mark, "/api/ui/delegations describe")

                # -- the full refresh, as before: a write finds a new build behind the same token ---------------------
                def comment(text: str) -> None:
                    page.locator("textarea[placeholder='Add a comment or bounce-back note']").fill(text)
                    page.locator(".detail-modal").get_by_role("button", name="Add Comment").click()

                def comments(text: str) -> str:
                    return sql(f"SELECT count(*) || ' by ' || coalesce(string_agg(DISTINCT who, ','), 'nobody') FROM "
                               f"ticket_board.ticket_comments WHERE ticket_id = 'PGU-4' AND text = $c${text}$c$;")

                def reloaded(text: str) -> bool:
                    return wait_for(lambda: comments(text).startswith("1 ") and page.evaluate(
                        "typeof window.__syrd669 === 'undefined'") and page.locator(".card-id").count() > 0)

                page.evaluate("window.__syrd669 = 'the same page'")
                mark = len(writes)
                start(TOKENS["F"], build="syrd669-build-3")
                comment("Sent across a new build.")
                seen["build_replayed"] = reloaded("Sent across a new build.")
                seen["build_comment"] = comments("Sent across a new build.")
                seen["build_writes"] = since(mark, "/api/tickets/PGU-4/actions/add_comment")
                page.locator(".card-id", has_text="PGU-4").first.wait_for(timeout=10000)

                # -- and a write refused on a new build is replayed after the reload ----------------------------------
                # The ticket open, so the page does not refresh itself first, and no description asking for the new
                # token first: the write meets the new build and the new token itself.
                open_ticket("PGU-4", panel)
                page.evaluate("window.__syrd669 = 'the same page'")
                page.route("**/api/ui/delegations", lambda route: route.abort())
                start(TOKENS["H"], build="syrd669-build-4")
                refusals: list[bool] = []

                def refuse_first(route) -> None:
                    if refusals:
                        route.continue_()
                        return
                    refusals.append(True)
                    route.fulfill(status=500, content_type="text/plain", body="temporarily refused")

                page.route("**/api/tickets/PGU-4/actions/add_comment", refuse_first)
                mark = len(writes)
                comment("Replayed after a refusal.")
                seen["refusal_replayed"] = reloaded("Replayed after a refusal.")
                seen["refusal_comment"] = comments("Replayed after a refusal.")
                seen["refusal_writes"] = since(mark, "/api/tickets/PGU-4/actions/add_comment")
                page.unroute("**/api/tickets/PGU-4/actions/add_comment")
                page.unroute("**/api/ui/delegations")
                page.locator(".card-id", has_text="PGU-4").first.wait_for(timeout=10000)

                # -- once, at most: a token that is still refused after the refresh is not tried again -------------
                page.route("**/api/client-config", lambda route: route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"build_id": "syrd669-next-build", "write_token": TOKENS["G"]})))
                mark = len(writes)
                seen["once_refused"] = page.evaluate("""async (stale) => {
                    setTicketBoardWriteToken(stale);  // the last process's token, as a page that missed a restart holds
                    try { await boardUiRequest({ describe: true, ticket_id: 'PGU-4' }); return 'described'; }
                    catch (failure) { return failure.message; }
                }""", TOKENS["E"])
                seen["once_describe"] = since(mark, "/api/ui/delegations describe")
                page.unroute("**/api/client-config")

                # -- what the page kept -----------------------------------------------------------------------
                seen["storage"] = page.evaluate("JSON.stringify([{...localStorage}, {...sessionStorage}])")
                seen["console"] = console
                seen["errors"] = errors
                seen["writes"] = writes
                seen["origin"] = origin
                browser.close()
        finally:
            live["server"].kill()
            live["server"].wait(timeout=30)
    return seen


def serve(root: Path, database_url: str, work: Path, port: int, token: str, build: str) -> None:
    """One board process: the real app and server, with a write token of its own, as production starts one."""
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from board_ui_user_record import trust_board_ui_users

    from scripts.ticket_board import ui_delegation

    ui_delegation.TRUST_HEADER_ROLES = False
    trust_board_ui_users(work / "registry", [{"account": "board-ui-user", "uid": 4242424}], peer_uid=4242424)
    app = fixture.TicketBoardApp(work / "frames", work / "assets", project="pgu", ticket_prefix="PGU",
                                 database_url=database_url)
    server = fixture.TicketBoardServer(("127.0.0.1", port), app, director_notifier=fixture.QuietNotifier(),
                                       write_token=token)
    if build:
        server.build_id = build
    print(f"serving {server.server_port}", flush=True)
    server.serve_forever()


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def base_commit() -> str:
    """Main as it stood before this ticket: the commit before the one that added this test."""
    found = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--",
                            "tests/board_write_token_rotation_browser_test.py"],
                           check=True, capture_output=True, text=True).stdout.split()
    assert found, "this test is not committed; the run of main needs the commit that added it"
    return found[-1] + "^"


def run(root: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX", "PG"))}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root)],
                           capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900)
    assert child.returncode == 0, child.stderr[-3000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def recovered(seq: list[str], old: str, new: str) -> bool:
    """Every request with the old token refused, each once, and none after the first with the new token, which
    all succeeded; at least one of each: what describing a ticket again and again after a restart looks like."""
    if not seq or seq[0] != f"{old}403" or f"{new}200" not in seq:
        return False
    first_new = seq.index(f"{new}200")
    return (set(seq[:first_new]) == {f"{old}403"} and set(seq[first_new:]) == {f"{new}200"}
            and seq.count(f"{old}403") <= seq.count(f"{new}200"))


def before(s: dict) -> None:
    """Main: the open page loses the User's decisions and the lent operations at a restart."""
    print("OBSERVED main " + json.dumps({k: v for k, v in s.items() if k not in ("writes", "console")}), file=sys.stderr)
    check(s["fresh_buttons"] and set(s["fresh_describe"]) == {"A200"}, f"main: a fresh page offers both: {s}")
    check(STALE in s["restart_note"] and set(s["restart_describe"]) == {"A403"},
          f"main: after a restart the panel loses Accept and Send to Director, refused with the old token and never "
          f"asking for the new one: {s['restart_note']!r} {s['restart_describe']}")
    check(STALE in s["lent_error"] and not s["lent_routed"] and set(s["lent_prepare"]) == {"C403"},
          f"main: a lent operation's confirmation is refused after a restart: {s['lent_error']!r} {s['lent_prepare']}")


def candidate(s: dict) -> None:
    print("OBSERVED candidate " + json.dumps({k: v for k, v in s.items() if k not in ("writes", "console")}),
          file=sys.stderr)
    check(s["arranged"] == "PGU-1:user_review,PGU-2:analysis,PGU-3:analysis,PGU-4:user_review",
          f"arranged: two tickets in front of the User, two for a lent operation: {s['arranged']}")
    check(s["fresh_buttons"] and set(s["fresh_describe"]) == {"A200"} and not s["fresh_note"],
          f"a fresh page offers Accept and Send to Director: {s['fresh_describe']} {s['fresh_note']!r}")
    check(s["writes"] and all(w["role"] == "user" for w in s["writes"]),
          f"every write the page sends names the User as its caller: "
          f"{sorted({(w['path'], w['role']) for w in s['writes'] if w['role'] != 'user'})}")
    check(s["redrawn"] and s["restart_buttons"] and not s["restart_note"],
          f"after a restart the redrawn panel still offers Accept and Send to Director: {s['restart_note']!r}")
    check(recovered(s["restart_describe"], "A", "B"),
          f"each description refused for the old token is asked once more, with the new token from "
          f"/api/client-config: {s['restart_describe']}")
    check(s["same_page"] == "the same page", "with no reload")
    check(s["draft_kept"] == DRAFT and s["comment_kept"] == COMMENT,
          f"and what the User was typing is still there: {s['draft_kept']!r}, {s['comment_kept']!r}")
    check(s["accepted"] and s["accept_writes"] == ["B403", "C200"],
          f"a restart between discovery and Accept: Accept is done, sent once more with the new token: "
          f"{s['accept_writes']}")
    check(s["lent_offered"] and not s["lent_offered_note"],
          f"the lent operations are offered after the restarts before: {s['lent_offered_note']!r}")
    check(s["lent_routed"] and not s["lent_error"] and s["lent_prepare"] == ["C403", "D200"]
          and s["lent_mint"] == ["D200"] and s["lent_action"] == ["D200"],
          f"a lent operation after a restart: its confirmation prepared with the new token and a session from the "
          f"new process, its grant minted, the action done: {s['lent_error']!r} {s['lent_prepare']} "
          f"{s['lent_mint']} {s['lent_action']}")
    check(s["crossed_routed"] and s["crossed_prepare"] == ["D200"] and s["crossed_mint"] == ["D200", "E200"]
          and s["crossed_action"] == ["D403", "E200"],
          f"restarted between its grant and its request: refused once, a new grant minted, done once: "
          f"{s['crossed_prepare']} {s['crossed_mint']} {s['crossed_action']} {s['crossed_status']!r}")
    check(s["crossed_grants"] == "2 grants, 2 sessions, 1 used",
          f"the new grant is the new session's, and only it is spent: {s['crossed_grants']}")
    check(s["build_refresh_required"] and recovered(s["build_describe"], "E", "F"),
          f"a new build behind the restart: the token recovered, and the page still left to refresh: "
          f"{s['build_describe']}")
    check(s["build_replayed"] and s["build_comment"] == "1 by user" and s["build_writes"] == ["F200"],
          f"a write that finds a new build behind the same token reloads the page and is sent after it, once, as the "
          f"User: {s['build_comment']} {s['build_writes']}")
    check(s["refusal_replayed"] and s["refusal_comment"] == "1 by user" and s["refusal_writes"] == ["H500", "H200"],
          f"a write refused on a new build is sent again after the reload, once: {s['refusal_comment']} "
          f"{s['refusal_writes']}")
    check(STALE in s["once_refused"] and s["once_describe"] == ["E403", "G403"],
          f"a token still refused after the refresh is not tried again: {s['once_describe']} {s['once_refused']!r}")
    tokens = TOKENS.values()
    check(not any(token in s["storage"] for token in tokens),
          f"no token is kept in the page's storage: {s['storage'][:300]}")
    check(not any(token in line for token in tokens for line in s["console"]),
          f"or written to its console: {s['console']}")
    check(all(w["url"].startswith(s["origin"] + "/") for w in s["writes"]),
          f"every write goes to the board's own origin: {sorted({w['url'] for w in s['writes']})}")
    # The board's own reload, asked by an event as the board stopped, fails while no board is there to answer.
    check([e for e in s["errors"] if not (e.startswith("Failed to fetch") and "at loadBoard " in e)] == [],
          f"no page errors: {s['errors']}")


def main() -> int:
    if len(sys.argv) == 8 and sys.argv[1] == "--serve":
        serve(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), int(sys.argv[5]), sys.argv[6], sys.argv[7])
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2])), default=str))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd669-before.") as tmp:
        before(run(tree_at(base_commit(), Path(tmp) / "before")))
    candidate(run(ROOT))
    print(f"board_write_token_rotation_browser_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
