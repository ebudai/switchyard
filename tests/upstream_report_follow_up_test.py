#!/usr/bin/env python3
"""SYRD-677: a reporting tenant can follow up on its own upstream report, and on nothing else.

On 2026-10-25 the pie Director filed SYRD-673 and SYRD-674; within the hour each
needed a word from the reporter -- a better approach superseding one, a caveat on
the other -- and the report credential could only create.

Production-built boards: `syrd`, the upstream, which takes reports, and the
tenants `pie` and `otto`. The real `ticket-board-write` runs as a pie pane would,
and requests are also sent straight to the board, as any client could:

* pie is given the board's report secret bound to its project; with it, it files
  reports from pie only, and the board's shared token still files as before;
* `report-follow-up` is a preview that sends nothing; the same command with
  `--submit-upstream --expect <digest>` adds exactly that follow-up once, as
  `reporter:pie`, tells the Director once, and changes nothing else on the
  ticket -- a withdrawal or supersession included, and on a finished report too;
* the digest binds the board, the ticket, the project, the report's source
  reference, the kind and the text, and the board recomputes it;
* refused, saying why: a ticket that does not exist, a local ticket, another
  tenant's report, a report whose source reference moved, the shared token, a
  rotated binding key, no credential, and a field it may not set; a credential
  minted from the shared secret every tenant once held; a project named
  in the request is never the authority;
* a report filed before this change is followed up after the upgrade, its
  provenance recorded from what it was filed with, and fixed;
* the read CLI and the board page show the comment as an external reporter's;
* a tenant is given the bound credential only by a board that says it reads it,
  derived from the board's own binding key, which no tenant is ever given and
  root's `--only accept-reports` mints for a board that has none;
* no token appears in any output.

The before-run is main's code, from a git archive, with the steps main has.
"""

from __future__ import annotations

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
CHECKS = 0
SECRET = "syrd-report-secret-must-never-print"
#: The board's own binding key (SYRD-677): never given to a tenant.
KEY = "syrd-binding-key-must-never-print"
FOLLOW_UP = "/api/tickets/actions/report_follow_up"


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenario(root: Path, before_root: Path | None) -> dict:
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as t
    from temporary_cluster import temporary_cluster

    has = (root / "scripts/ticket_board/report_follow_up.py").is_file()
    if has:
        from scripts.ticket_board import report_follow_up as rf

    def bound(project: str) -> str:
        return rf.bound_token(KEY, project) if has else f"syrdr1.{project}.{'0' * 64}"

    def build(cluster, db: str, tree: Path) -> str:
        admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(admin, (tree / "scripts/ticket_board/schema.sql").read_text())
        if t.psql(admin, "SELECT count(*) FROM pg_roles WHERE rolname = 'director';") == "0":
            t.create_roles(admin)
        migrate(admin, tree)
        return admin

    def migrate(admin: str, tree: Path, *, upgrade: bool = False) -> None:
        """A new board: rbac.sql (which creates the roles), the migrations, rbac.sql. An upgrade, as a deploy runs
        it: every pending migration, then rbac.sql."""
        if not upgrade:
            t.psql(admin, (tree / "scripts/ticket_board/rbac.sql").read_text())
        done = subprocess.run(["bash", str(tree / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                              env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
        assert done.returncode == 0, done.stderr[-800:]
        t.psql(admin, (tree / "scripts/ticket_board/rbac.sql").read_text())

    servers: list = []

    def serve(cluster, db: str, project: str, prefix: str, secret: str = SECRET, key: str = KEY):
        app = t.TicketBoardApp(cluster.root / f"{db}-frames", cluster.root / f"{db}-assets", project=project,
                               ticket_prefix=prefix,
                               database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE))
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier(),
                                     report_token=secret or None, **({"report_binding_key": key} if has else {}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return app, server, f"http://127.0.0.1:{server.server_port}"

    def post(base: str, path: str, payload: dict, token: str | None) -> tuple[int, str]:
        import urllib.error
        import urllib.request
        headers = {"Content-Type": "application/json", **({t.REPORT_TOKEN_HEADER: token} if token else {})}
        request = urllib.request.Request(base + path, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    seen: dict = {}
    outputs: list[str] = []
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    with temporary_cluster(prefix="syrd677-", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd677-pane.") as pane:
        admin = build(cluster, "syrd", root)
        app, server, base = serve(cluster, "syrd", "syrd", "SYRD")
        tenant_admin = build(cluster, "pie", root)
        _tenant_app, tenant, tenant_base = serve(cluster, "pie", "pie", "PIE", secret="")

        def sql(statement: str) -> str:
            return t.psql(admin, statement).strip()

        def credential(name: str, token: str) -> str:
            path = Path(pane) / f"{name}.env"
            path.write_text(f"TICKET_BOARD_TENANT_REPORT_TOKEN={token}\n")
            path.chmod(0o600)
            return str(path)

        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": pane,
               "TICKET_BOARD_URL": tenant_base, "TICKET_BOARD_PROJECT": "pie", "TICKET_BOARD_CALLER_ROLE": "director",
               "TICKET_BOARD_WRITE_TOKEN": tenant.write_token, "TICKET_BOARD_REPORT_URL": base,
               "TICKET_BOARD_REPORT_ORIGIN_PROJECT": "pie",
               "TICKET_BOARD_TENANT_REPORT_TOKEN_FILE": credential("pie", bound("pie"))}

        def cli(*args: str, stdin: str | None = None, **changes: str) -> subprocess.CompletedProcess[str]:
            done = subprocess.run([str(root / "scripts/ticket-board-write"), *args], capture_output=True, text=True,
                                  timeout=60, env={**env, **changes}, input=stdin if stdin is not None else "")
            outputs.append(done.stdout + done.stderr)
            return done

        def digest_of(done: subprocess.CompletedProcess[str]) -> str:
            return next((line.split()[-1] for line in done.stdout.splitlines() if line.strip().startswith("digest:")), "")

        def file_report(ref: str, *, origin: str = "pie", **changes: str) -> tuple[str, subprocess.CompletedProcess]:
            args = ["file-report", "--title", f"Report {ref}", "--body", "What failed.", "--external-source-ref", ref,
                    "--origin-project", origin]
            preview = cli(*args, **changes)
            done = cli(*args, "--submit-upstream", "--expect", digest_of(preview), **changes)
            ticket = json.loads(done.stdout).get("id", "") if done.returncode == 0 else ""  # it prints the ticket
            return ticket, done

        def row(ticket: str) -> str:
            return sql(f"SELECT row_to_json(x)::text FROM ticket_board.tickets x WHERE id = '{ticket}';")

        def comments(ticket: str) -> list[str]:
            return json.loads(sql("SELECT coalesce(json_agg(who || ': ' || text ORDER BY id), '[]') FROM "
                                  f"ticket_board.ticket_comments WHERE ticket_id = '{ticket}';"))

        def notices(ticket: str) -> list[str]:
            return json.loads(sql("SELECT coalesce(json_agg(target_role || ' ' || kind || ' ' || (payload->>'kind') "
                                  "ORDER BY id), '[]') FROM ticket_board.ticket_notification_queue "
                                  f"WHERE ticket_id = '{ticket}';"))

        # -- filing: the bound credential files from its own project; the shared token as before ------------------
        first, done = file_report("pie:PIE-5")
        seen["filed_bound"] = [done.returncode, first, sql(f"SELECT origin_project FROM ticket_board.tickets "
                                                           f"WHERE id = '{first}';") if first else ""]
        if has and not first:  # nothing after this can be asked without the report pie filed
            for item in servers:
                item.shutdown()
                item.server_close()
            return {**seen, "leaked": []}
        _, done = file_report("pie:PIE-6", origin="otto")
        seen["filed_as_other"] = [done.returncode, (done.stdout + done.stderr)[-200:]]
        shared = credential("shared", SECRET)
        legacy, done = file_report("pie:PIE-7", TICKET_BOARD_TENANT_REPORT_TOKEN_FILE=shared)
        seen["filed_shared"] = [done.returncode, bool(legacy)]

        # -- a follow-up: previewed, then exactly that, once ------------------------------------------------------
        text = "A later finding supersedes this: the approach in PIE-9 is preferable.\n`keep this literal` $HOME"
        follow = ["report-follow-up", "--ticket", first, "--kind", "supersession", "--text-file", "-"]
        before_row, before_comments, before_notices = row(first), comments(first), notices(first)
        preview = cli(*follow, stdin=text)
        digest = digest_of(preview)
        seen["preview"] = [preview.returncode, preview.stdout, preview.stderr[-300:]]
        seen["no_such_command"] = "invalid choice: 'report-follow-up'" in preview.stderr
        seen["preview_wrote"] = comments(first) != before_comments or notices(first) != before_notices
        sent = cli(*follow, "--submit-upstream", "--expect", digest, stdin=text)
        seen["sent"] = [sent.returncode, sent.stdout[-300:], sent.stderr[-300:]]
        seen["comments"] = comments(first)[len(before_comments):]
        seen["notices"] = notices(first)[len(before_notices):]
        seen["row_unchanged"] = row(first) == before_row
        again = cli(*follow, "--submit-upstream", "--expect", digest, stdin=text)
        seen["replayed"] = [again.returncode, again.stderr[-200:], len(comments(first)) - len(before_comments),
                            len(notices(first)) - len(before_notices)]
        changed = cli(*follow, "--submit-upstream", "--expect", digest, stdin=text + " (edited)")
        seen["changed"] = [changed.returncode, changed.stderr[-200:]]

        if has:
            destination = {"project": "syrd", "ticket_prefix": "SYRD"}

            def payload(ticket: str, project: str, ref: str, words: str = "A note.", kind: str = "comment") -> dict:
                return {"ticket_id": ticket, "kind": kind, "text": words, "confirmation": rf.follow_up_digest(
                    destination=destination, ticket_id=ticket, origin_project=project, external_source_ref=ref,
                    kind=kind, text=words)}

            # Two at once, the same confirmation: one comment.
            race = payload(first, "pie", "pie:PIE-5", "Sent twice at once.")
            results: list = []
            threads = [threading.Thread(target=lambda: results.append(post(base, FOLLOW_UP, race, bound("pie"))))
                       for _ in range(3)]
            [x.start() for x in threads]
            [x.join() for x in threads]
            seen["race"] = [sorted(code for code, _ in results),
                            sum("Sent twice at once." in c for c in comments(first))]

            # The database refuses on its own too: another project, a local ticket, any writer but the service.
            def as_service(statement: str, role: str = t.SERVICE_ROLE) -> str:
                try:
                    t.psql(t.conninfo(cluster.socket_dir, cluster.port, "syrd", role), statement)
                    return "allowed"
                except AssertionError as exc:
                    return " ".join(str(exc).split())[:400] or "refused"

            confirmation = "a" * 24
            seen["database"] = [
                as_service(f"SELECT * FROM ticket_board.append_report_follow_up('{first}', 'otto', 'pie:PIE-5', "
                           f"'comment', 'From the database.', '{confirmation}');"),
                as_service("SELECT * FROM ticket_board.append_report_follow_up('SYRD-1', 'pie', 'pie:PIE-5', "
                           f"'comment', 'From the database.', '{'b' * 24}');", role="ticket_board_listener"),
            ]
            # The Director's notice is the Director's to be delivered.
            from scripts.ticket_board import notify_listener as nl
            import psycopg
            listener_url = t.conninfo(cluster.socket_dir, cluster.port, "syrd", "ticket_board_listener")
            notice = sql(f"SELECT payload::text FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{first}' "
                         "AND payload->>'kind' = 'report_follow_up' LIMIT 1;")
            held = nl.TicketBoardNotifyListener(conninfo=listener_url, project="syrd", sender=lambda *_a: None,
                                                activity_gate=lambda _t: True, target_exists=lambda _t: True,
                                                poll_seconds=0)
            with psycopg.connect(listener_url, autocommit=True) as conn:
                held.refresh_workflow(conn)
                seen["deliverable"] = [held.eligibility._notification_is_current(conn, first, role, notice)
                                       for role in ("director", "ops")]
            # A project named in the pane's environment is not the authority either: the credential's is.
            named = cli("report-follow-up", "--ticket", first, "--text", "Hi.", TICKET_BOARD_REPORT_ORIGIN_PROJECT="otto")
            seen["named_env"] = [named.returncode, "as:          reporter:pie" in named.stdout]

            # The board page says whose it is, in a real browser.
            def page_label(board: str, ticket: str) -> list:
                from playwright.sync_api import sync_playwright
                launch: dict = {"headless": True}
                if Path("/opt/google/chrome/chrome").is_file():
                    launch["executable_path"] = "/opt/google/chrome/chrome"
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(**launch)
                    page = browser.new_page(viewport={"width": 1280, "height": 1000})
                    page.goto(board + "/", wait_until="domcontentloaded")
                    page.locator(".card", has=page.locator(".card-id", has_text=ticket)).first.click()
                    page.locator(".comment strong").first.wait_for(timeout=10000)
                    reporter = page.locator("[data-comment-origin='external-reporter'] strong")
                    local = page.locator(".comment:not([data-comment-origin]) strong")
                    labels = [reporter.first.inner_text() if reporter.count() else "no external reporter's comment shown",
                              local.first.inner_text() if local.count() else ""]
                    browser.close()
                return labels

            sql(f"SELECT ticket_board.append_ticket_comment('{first}', 'director', 'A local note.');")
            seen["page_before"] = page_label(base, first)

            # Refused, each saying why.
            local = "SYRD-50"
            t.seed_postgres_ticket(admin, local, title="A local ticket", state="analysis", assignee="director",
                                   commit_exempt=True)
            other, _ = file_report("otto:OTTO-1", origin="otto",
                                   TICKET_BOARD_TENANT_REPORT_TOKEN_FILE=credential("otto", bound("otto")),
                                   TICKET_BOARD_REPORT_ORIGIN_PROJECT="otto", TICKET_BOARD_PROJECT="otto")
            moved, _ = file_report("pie:PIE-8")
            sql(f"UPDATE ticket_board.tickets SET external_source_ref = 'pie:PIE-80' WHERE id = '{moved}';")
            refusals = {
                "absent": post(base, FOLLOW_UP, payload("SYRD-99", "pie", ""), bound("pie")),
                "local": post(base, FOLLOW_UP, payload(local, "pie", ""), bound("pie")),
                "another tenant's": post(base, FOLLOW_UP, payload(other, "pie", "otto:OTTO-1"), bound("pie")),
                "as the other tenant": post(base, FOLLOW_UP, payload(first, "otto", "pie:PIE-5"), bound("otto")),
                # Authority before the digest: another tenant's report is refused for that, whatever it confirms.
                "as the other tenant, any confirmation": post(base, FOLLOW_UP, {**payload(first, "otto", "pie:PIE-5"),
                                                                                "confirmation": "c" * 24}, bound("otto")),
                "source moved": post(base, FOLLOW_UP, payload(moved, "pie", "pie:PIE-80"), bound("pie")),
                "shared token": post(base, FOLLOW_UP, payload(first, "pie", "pie:PIE-5"), SECRET),
                "no credential": post(base, FOLLOW_UP, payload(first, "pie", "pie:PIE-5"), None),
                "forged credential": post(base, FOLLOW_UP, payload(first, "pie", "pie:PIE-5"),
                                          "syrdr1.pie." + "0" * 64),
                # Audit's case: a tenant that kept the shared secret every tenant was once given cannot make another
                # project's credential from it.
                "minted from the shared secret": post(base, FOLLOW_UP, payload(first, "pie", "pie:PIE-5"),
                                                      rf.bound_token(SECRET, "pie")),
                "minted for otto from the shared secret": post(base, FOLLOW_UP, payload(other, "otto", "otto:OTTO-1"),
                                                               rf.bound_token(SECRET, "otto")),
                "a field": post(base, FOLLOW_UP, {**payload(first, "pie", "pie:PIE-5"), "state": "done"}, bound("pie")),
                "a named project": post(base, FOLLOW_UP, {**payload(first, "pie", "pie:PIE-5"), "origin_project": "pie"},
                                        bound("pie")),
                "wrong confirmation": post(base, FOLLOW_UP, {**payload(first, "pie", "pie:PIE-5"), "text": "Other."},
                                           bound("pie")),
                "too long": post(base, FOLLOW_UP, payload(first, "pie", "pie:PIE-5", "x" * 8001), bound("pie")),
            }
            seen["refusals"] = {name: [code, body[:160]] for name, (code, body) in refusals.items()}
            seen["moved_cli"] = [cli("report-follow-up", "--ticket", moved, "--text", "Hi.").returncode]
            moved_preview = cli("report-follow-up", "--ticket", moved, "--text", "Hi.")
            moved_sent = cli("report-follow-up", "--ticket", moved, "--text", "Hi.", "--submit-upstream", "--expect",
                             digest_of(moved_preview))
            seen["moved_cli"] = [moved_sent.returncode, (moved_sent.stdout + moved_sent.stderr)[-200:]]
            seen["local_cli"] = [cli("report-follow-up", "--ticket", local, "--text", "Hi.").returncode,
                                 outputs[-1][-200:]]
            seen["shared_cli"] = [cli("report-follow-up", "--ticket", first, "--text", "Hi.",
                                      TICKET_BOARD_TENANT_REPORT_TOKEN_FILE=shared).returncode, outputs[-1][-200:]]
            seen["provenance"] = [sql(f"SELECT origin_project || ' ' || external_source_ref || ' ' || recorded_from "
                                      f"FROM ticket_board.report_provenance WHERE ticket_id = '{moved}';")]
            try:
                sql(f"UPDATE ticket_board.report_provenance SET origin_project = 'otto' WHERE ticket_id = '{first}';")
                seen["provenance"].append("changed")
            except AssertionError as exc:
                seen["provenance"].append("refused" if "fixed when it is filed" in str(exc) else str(exc)[-200:])

            # A finished report takes a follow-up; a withdrawal is a request, and nothing moves.
            sql(f"SET session_replication_role = replica; UPDATE ticket_board.tickets SET state = 'done' "
                f"WHERE id = '{first}'; SET session_replication_role = origin;")
            done_row = row(first)
            withdraw = ["report-follow-up", "--ticket", first, "--kind", "withdrawal_request", "--text",
                        "Please withdraw: the operator chose this at creation."]
            sent = cli(*withdraw, "--submit-upstream", "--expect", digest_of(cli(*withdraw)))
            seen["terminal"] = [sent.returncode, comments(first)[-1], row(first) == done_row]
            # Its notice reaches the Director although the report is finished.
            withdrawn = sql(f"SELECT payload::text FROM ticket_board.ticket_notification_queue WHERE ticket_id = "
                            f"'{first}' AND payload->>'follow_up' = 'withdrawal_request' LIMIT 1;")
            with psycopg.connect(listener_url, autocommit=True) as conn:
                held.refresh_workflow(conn)
                seen["terminal"].append(held.eligibility._notification_is_current(conn, first, "director", withdrawn))

            # A rotated binding key: every credential derived from the old one stops.
            server.report_binding_key = "a-rotated-binding-key"
            seen["rotated"] = post(base, FOLLOW_UP, payload(first, "pie", "pie:PIE-5", "After rotation."), bound("pie"))
            server.report_binding_key = KEY

            # The read CLI and the page say whose it is.
            read = subprocess.run([str(root / "scripts/ticket-board-read"), "comments", first], capture_output=True,
                                  text=True, timeout=60, env={**env, "TICKET_BOARD_URL": base})
            seen["read"] = [line for line in read.stdout.splitlines() if "reporter:pie" in line][:1]

            # A tenant is given the bound credential only by a board that says it reads it.
            from scripts import upstream_report as ur
            _same_app, same, same_base = serve(cluster, "syrd", "syrd", "SYRD", key=SECRET)
            _keyless_app, _keyless, keyless_base = serve(cluster, "syrd", "syrd", "SYRD", key="")
            seen["advertised"] = [ur._board_takes_bound_credentials(base), ur._board_takes_bound_credentials(
                "http://127.0.0.1:1"), ur._board_takes_bound_credentials(keyless_base),
                ur._board_takes_bound_credentials(same_base)]
            # A binding key that is the report secret is no key: a token derived from it opens nothing.
            seen["same_key"] = post(same_base, FOLLOW_UP, payload(first, "pie", "pie:PIE-5", "Same key."),
                                    rf.bound_token(SECRET, "pie"))[0]
            # What the refresh writes into a tenant's credential file (upstream_report_credential_test's sandbox).
            import upstream_report_credential_test as uc
            from scripts import team_launcher
            with tempfile.TemporaryDirectory(prefix="syrd677-refresh.") as raw:
                tmp = Path(raw)
                registry, board_env, board_owner, resolver = uc._host(tmp)
                board_env.parent.mkdir(parents=True, exist_ok=True)
                board_env.write_text(f"TICKET_BOARD_TENANT_REPORT_TOKEN={SECRET}\nOTHER=kept\n", encoding="utf-8")
                home, _ = uc._sandbox_home(tmp)
                written = home / ".config" / "mefp" / "upstream-report.env"
                config, _ = uc._tenant(tmp, upstream_url=uc.UPSTREAM, token_file=str(written))

                def refresh(takes: bool) -> list:
                    printed: list[str] = []
                    problems = team_launcher.refresh_upstream_report_credential(
                        config, registry_dir=registry, home_for_user=resolver, print_func=printed.append,
                        takes_bound=lambda _url: takes)
                    return [problems, written.read_text(encoding="utf-8").strip().split("=", 1)[-1], printed]

                # A board provisioned before SYRD-677 has no binding key: its tenants keep the secret.
                keyless = refresh(True)
                # `--only accept-reports` gives it one, once, keeping the rest of its file.
                printed: list[str] = []
                ensured = [ur.ensure_report_binding_key(board_env, board_owner, slug="syrd", home_for_user=resolver,
                                                        print_func=printed.append) for _ in range(2)]
                env_text = board_env.read_text(encoding="utf-8")
                key_line = [line for line in env_text.splitlines() if line.startswith("TICKET_BOARD_REPORT_BINDING_KEY=")]
                minted = key_line[0].split("=", 1)[1] if key_line else ""
                given, again, older = refresh(True), refresh(True), refresh(False)
                seen["ensured"] = [ensured, len(key_line), bool(minted) and minted != SECRET,
                                   f"TICKET_BOARD_TENANT_REPORT_TOKEN={SECRET}" in env_text and "OTHER=kept" in env_text,
                                   oct(board_env.stat().st_mode & 0o777), any("already holds" in p for p in printed)]
                seen["refreshed"] = [keyless[1] == SECRET, given[0], given[1] == rf.bound_token(minted, "mefp"),
                                     minted not in given[1], SECRET not in given[1],
                                     any("bound to mefp" in line for line in given[2]),
                                     any("already holds" in line for line in again[2]), older[1] == SECRET]
            # Root's `--only accept-reports` gives the board that takes reports its key: the declaration itself.
            from types import SimpleNamespace

            from scripts import upstream_report_sinks as sinks
            with tempfile.TemporaryDirectory(prefix="syrd677-accept.") as raw:
                tmp = Path(raw)
                registry, board_env, _board_owner, resolver = uc._host(tmp, board_url=base, slug="syrd")
                board_env.parent.mkdir(parents=True, exist_ok=True)
                board_env.write_text(f"TICKET_BOARD_TENANT_REPORT_TOKEN={SECRET}\n", encoding="utf-8")
                printed = []
                code = sinks.declare_report_board(SimpleNamespace(project="syrd"), accept=True, registry_dir=registry,
                                                  home_for_user=resolver, print_func=printed.append)
                seen["declared"] = [code, "TICKET_BOARD_REPORT_BINDING_KEY=" in board_env.read_text(encoding="utf-8"),
                                    any("project-bound report credentials" in line for line in printed)]
            seen["credential"] = [rf.credential_project(SECRET, KEY, bound("pie")),
                                  rf.credential_project(SECRET, KEY, SECRET),
                                  rf.credential_project(SECRET, KEY, "syrdr1.pie." + "0" * 64),
                                  rf.credential_project(SECRET, "other", bound("pie")),
                                  rf.credential_project(SECRET, KEY, rf.bound_token(SECRET, "pie")),
                                  rf.credential_project(SECRET, SECRET, rf.bound_token(SECRET, "pie"))]

            # A report filed before this change, followed up after the upgrade.
            if before_root is not None:
                old = build(cluster, "upgraded", before_root)
                with_service = t.conninfo(cluster.socket_dir, cluster.port, "upgraded", t.SERVICE_ROLE)
                t.psql(with_service, "SELECT ticket_board.file_report('Filed before', 'Body.', 'pie', 'pie:PIE-1');")
                migrate(old, root, upgrade=True)
                up_app, up_server, up_base = serve(cluster, "upgraded", "syrd", "SYRD")
                filed = t.psql(old, "SELECT id FROM ticket_board.tickets WHERE title = 'Filed before';").strip()
                code, body = post(up_base, FOLLOW_UP, payload(filed, "pie", "pie:PIE-1", "After the upgrade."),
                                  bound("pie"))
                seen["upgraded"] = [code, t.psql(old, "SELECT recorded_from FROM ticket_board.report_provenance "
                                                      f"WHERE ticket_id = '{filed}';").strip(),
                                    t.psql(old, "SELECT who FROM ticket_board.ticket_comments WHERE ticket_id = "
                                                f"'{filed}' ORDER BY id DESC LIMIT 1;").strip()]
        else:
            seen["direct"] = post(base, FOLLOW_UP, {"ticket_id": first or "SYRD-1", "kind": "comment", "text": "Hi.",
                                                    "confirmation": "0" * 24}, bound("pie"))
            seen["direct_shared"] = post(base, FOLLOW_UP, {"ticket_id": first or "SYRD-1", "kind": "comment",
                                                           "text": "Hi.", "confirmation": "0" * 24}, SECRET)
        for item in servers:
            item.shutdown()
            item.server_close()
    seen["leaked"] = [name for name, token in (("secret", SECRET), ("key", KEY), ("pie", bound("pie")),
                                                ("otto", bound("otto")))
                      if any(token in output for output in outputs)]
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests",
                              "skills", "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def base_commit() -> str:
    """Main as it stood before this ticket: the commit before the one that added this test."""
    found = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--",
                            "tests/upstream_report_follow_up_test.py"], check=True, capture_output=True,
                           text=True).stdout.split()
    assert found, "this test is not committed; the run of main needs the commit that added it"
    return found[-1] + "^"


def run(root: Path, before_root: Path | None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX", "PG"))}
    args = [sys.executable, str(Path(__file__).resolve()), "--scenario", str(root)]
    if before_root is not None:
        args.append(str(before_root))
    child = subprocess.run(args, capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"},
                           timeout=900)
    assert child.returncode == 0, child.stderr[-3000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(s: dict) -> None:
    """Main: the report credential creates, and a reporter can add nothing to its report."""
    print("OBSERVED main " + json.dumps(s), file=sys.stderr)
    check(s["filed_shared"] == [0, True], f"main: the board's shared token files a report: {s['filed_shared']}")
    check(s["preview"][0] != 0 and s["no_such_command"],
          f"main: there is no follow-up to preview: {s['preview']}")
    check(s["direct"][0] != 201 and s["direct_shared"][0] != 201,
          f"main: the board takes no follow-up, with any credential: {s['direct']} {s['direct_shared']}")
    check(s["filed_bound"][0] != 0, f"main: a credential bound to a project is no credential: {s['filed_bound']}")


def candidate(s: dict) -> None:
    print("OBSERVED candidate " + json.dumps(s), file=sys.stderr)
    check(s["filed_bound"][0] == 0 and s["filed_bound"][2] == "pie",
          f"pie's bound credential files a report from pie: {s['filed_bound']}")
    check(s["filed_as_other"][0] != 0 and "pie's" in s["filed_as_other"][1],
          f"and from no other project: {s['filed_as_other']}")
    check(s["filed_shared"] == [0, True], f"the board's shared token still files, as before: {s['filed_shared']}")
    check(s["preview"][0] == 0 and "nothing has been sent" in s["preview"][1] and "supersession" in s["preview"][1]
          and "reporter:pie" in s["preview"][1] and "`keep this literal` $HOME" in s["preview"][1]
          and not s["preview_wrote"],
          f"report-follow-up alone is a preview that sends nothing and shows exactly what would be: {s['preview']}")
    check(s["sent"][0] == 0 and len(s["comments"]) == 1 and s["comments"][0].startswith(
              "reporter:pie: The reporter says a later finding supersedes this report: A later finding")
          and s["comments"][0].endswith("`keep this literal` $HOME"),
          f"submitted: one comment, the reporter's, the text literal: {s['sent']} {s['comments']}")
    check(s["notices"] == ["director ticket_update report_follow_up"],
          f"the Director is told once: {s['notices']}")
    check(s["row_unchanged"], "and nothing else on the ticket changed")
    check(s["replayed"][0] == 0 and "already added" in s["replayed"][1] and s["replayed"][2:] == [1, 1],
          f"the same follow-up again adds nothing and tells no one: {s['replayed']}")
    check(s["changed"][0] != 0 and "not the follow-up that was previewed" in s["changed"][1],
          f"a changed text is not the previewed follow-up: {s['changed']}")
    check(s["race"] == [[200, 200, 201], 1], f"three at once with one confirmation: one comment: {s['race']}")
    r = s["refusals"]
    expected = {"absent": (404, "there is no ticket SYRD-99"), "local": (403, "was not filed as a report"),
                "another tenant's": (403, "filed from another project"),
                "as the other tenant": (403, "filed from another project"),
                "as the other tenant, any confirmation": (403, "filed from another project; this credential is otto's"),
                "source moved": (403, "source reference was changed on this board after it was filed"), "shared token": (403, "bound to it"),
                "no credential": (403, "missing or invalid"), "forged credential": (403, "missing or invalid"),
                "minted from the shared secret": (403, "missing or invalid"),
                "minted for otto from the shared secret": (403, "missing or invalid"),
                "a field": (403, "cannot set: state"), "a named project": (403, "cannot set: origin_project"),
                "wrong confirmation": (400, "does not match this follow-up"), "too long": (400, "limited to 8000")}
    for name, (code, words) in expected.items():
        check(r[name][0] == code and words in r[name][1], f"refused, {name}: {r[name]}")
    check("filed from another project" in s["database"][0]
          and ("permission denied" in s["database"][1] or "only database writer" in s["database"][1]),
          f"the database refuses another project's follow-up and any writer but the service: {s['database']}")
    check(s["deliverable"] == [True, False], f"the notice is the Director's to be delivered, and no one else's: "
                                             f"{s['deliverable']}")
    check(s["named_env"] == [0, True], f"a project named in the environment is not the authority: {s['named_env']}")
    check(s["moved_cli"][0] != 0 and "source reference was changed" in s["moved_cli"][1],
          f"the CLI too, for a report whose source reference moved: {s['moved_cli']}")
    check(s["local_cli"][0] != 0 and "not filed as a report" in s["local_cli"][1],
          f"a local ticket stops at the preview: {s['local_cli']}")
    check(s["shared_cli"][0] != 0 and "shared token" in s["shared_cli"][1],
          f"so does the shared token, naming the repair: {s['shared_cli']}")
    check(s["provenance"] == ["pie pie:PIE-8 filing", "refused"],
          f"provenance is what the report was filed with, and is fixed: {s['provenance']}")
    check(s["terminal"][0] == 0 and s["terminal"][1].startswith(
              "reporter:pie: The reporter asks to withdraw this report: Please withdraw") and s["terminal"][2:] == [True, True],
          f"a finished report takes a follow-up; a withdrawal is a request, and nothing moves: {s['terminal']}")
    check(s["rotated"][0] == 403, f"a rotated binding key stops every credential derived from the old one: "
                                  f"{s['rotated']}")
    check(s["page_before"] == ["Reporter (pie, external)", "director"],
          f"the board page labels the reporter's comment as an external reporter's, and no other: {s['page_before']}")
    check(s["read"] and "reporter:pie (external reporter):" in s["read"][0],
          f"the read CLI says it is an external reporter's: {s['read']}")
    check(s["advertised"] == [True, False, False, False],
          f"a board says it reads a bound credential only with a binding key that is not its report secret; no answer "
          f"is no: {s['advertised']}")
    check(s["declared"] == [0, True, True],
          f"root's --only accept-reports gives the board its binding key, and says so: {s['declared']}")
    check(s["same_key"] == 403, f"a binding key that is the report secret is no key: {s['same_key']}")
    check(s["ensured"] == [["", ""], 1, True, True, "0o600", True],
          f"--only accept-reports gives a board its binding key once, a new secret, keeping the rest of its file, "
          f"0600: {s['ensured']}")
    check(s["refreshed"] == [True, [], True, True, True, True, True, True],
          f"the refresh gives a tenant a credential derived from the board's binding key and its project -- neither "
          f"the key nor the secret -- once, when its board reads one and has a key, and the secret otherwise: "
          f"{s['refreshed']}")
    check(s["credential"] == ["pie", "", None, None, None, None],
          f"a bound credential names its project, the shared one none; a forged one, one from another key, one "
          f"derived from the shared secret, or with the secret as the key: nothing: {s['credential']}")
    check(s["upgraded"] == [201, "backfill", "reporter:pie"],
          f"a report filed before the upgrade is followed up after it: {s['upgraded']}")
    check(s["leaked"] == [], f"no token in any output: {s['leaked']}")


def main() -> int:
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--scenario":
        before_root = Path(sys.argv[3]) if len(sys.argv) == 4 else None
        print(json.dumps(scenario(Path(sys.argv[2]), before_root), default=str))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd677-before.") as tmp:
        main_tree = tree_at(base_commit(), Path(tmp) / "before")
        before(run(main_tree, None))
        candidate(run(ROOT, main_tree))
    print(f"upstream_report_follow_up_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
