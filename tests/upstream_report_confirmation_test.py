#!/usr/bin/env python3
"""SYRD-602: a tenant agent cannot create a ticket on another project's board in one step.

Two production-built boards: `syrd` (the upstream, which accepts reports with
its report token) and `mefp` (the tenant). The real `ticket-board-write` runs
as a MEFP pane would, with MEFP's own board, the upstream report URL and a
report-token file in its environment:

- one `file-report` call is a preview: it reads the upstream's public identity,
  prints both projects, the report and a digest, and writes nothing;
- the digest is bound to the destination, the source project, the title, the
  body, the requested state and the source reference: change any of them and
  the submission is refused, by the CLI and by the upstream board itself;
- the exact submission creates one ticket with MEFP's attribution, and a
  replay (or two at once) creates no second;
- a pre-SYRD-602 one-step request straight to the board is refused;
- missing, unreachable or self-pointing configuration stops at the preview;
- local operations never contact the upstream board;
- the token appears in no output, and the board's UI offers no report path;
- the board skill and the tenant guide say all of this.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()
import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board import report_confirmation as rc  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

REPORT = "/api/tickets/actions/file_report"
TOKEN = "syrd-report-token-must-never-print"
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def post(base: str, payload: dict, *, token: str | None = TOKEN) -> tuple[int, str]:
    """A report request as any client could send it: the status and body, judged by the checks, not here."""
    import urllib.error
    import urllib.request
    headers = {"Content-Type": "application/json", **({t.REPORT_TOKEN_HEADER: token} if token else {})}
    request = urllib.request.Request(base + REPORT, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class Board:
    def __init__(self, cluster, db: str, project: str, prefix: str, *, report_token: str = "") -> None:
        self.admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(self.admin, (ROOT / "scripts/ticket_board/schema.sql").read_text())
        if t.psql(self.admin, "SELECT count(*) FROM pg_roles WHERE rolname = 'director';") == "0":
            t.create_roles(self.admin)
        t.psql(self.admin, (ROOT / "scripts/ticket_board/rbac.sql").read_text())
        done = subprocess.run(["bash", str(ROOT / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                              env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": self.admin}, timeout=300)
        assert done.returncode == 0, done.stderr[-800:]
        t.psql(self.admin, (ROOT / "scripts/ticket_board/rbac.sql").read_text())
        self.app = t.TicketBoardApp(cluster.root / f"{db}-frames", cluster.root / f"{db}-assets", project=project,
                                    ticket_prefix=prefix,
                                    database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE))
        self.server = t.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=t.QuietNotifier(),
                                          report_token=report_token or None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tickets(self) -> list[dict]:
        return json.loads(t.psql(self.admin, "SELECT coalesce(json_agg(json_build_object('id', id, 'title', title, "
                                             "'state', state, 'origin', origin_project, 'ref', external_source_ref) "
                                             "ORDER BY id), '[]') FROM ticket_board.tickets;"))

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Recorder(http.server.BaseHTTPRequestHandler):
    """An upstream that only records: any request reaching it is a contact."""
    seen: list[str] = []

    def _record(self) -> None:
        Recorder.seen.append(f"{self.command} {self.path}")
        self.send_response(500)
        self.end_headers()

    do_GET = do_POST = _record

    def log_message(self, *args) -> None:
        pass


def scenario() -> dict:
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    seen: dict = {}
    with temporary_cluster(prefix="syrd602-", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd602-pane.") as pane:
        upstream = Board(cluster, "syrd", "syrd", "SYRD", report_token=TOKEN)
        tenant = Board(cluster, "mefp", "mefp", "MEFP")
        other = Board(cluster, "otto", "otto", "OTTO", report_token=TOKEN)
        recorder = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        threading.Thread(target=recorder.serve_forever, daemon=True).start()
        try:
            token_file = Path(pane) / "upstream-report.env"
            token_file.write_text(f"TICKET_BOARD_TENANT_REPORT_TOKEN={TOKEN}\n")
            token_file.chmod(0o600)
            pane_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": pane,
                        "TICKET_BOARD_URL": tenant.base, "TICKET_BOARD_PROJECT": "mefp",
                        "TICKET_BOARD_CALLER_ROLE": "director", "TICKET_BOARD_WRITE_TOKEN": tenant.server.write_token,
                        "TICKET_BOARD_REPORT_URL": upstream.base, "TICKET_BOARD_REPORT_ORIGIN_PROJECT": "mefp",
                        "TICKET_BOARD_TENANT_REPORT_TOKEN_FILE": str(token_file)}
            outputs: list[str] = []

            def cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
                done = subprocess.run([str(ROOT / "scripts/ticket-board-write"), *args], capture_output=True, text=True,
                                      timeout=60, env=env or pane_env, stdin=subprocess.DEVNULL)
                outputs.append(done.stdout + done.stderr)
                return done

            report = ["file-report", "--title", "Board refuses a valid report", "--body",
                      "The board refused it.\nSteps: one, two.", "--external-source-ref", "mefp:MEFP-12"]

            # 1. One call is a preview: no write upstream, a bounded preview, a digest.
            before = upstream.tickets()
            preview = cli(*report)
            seen["preview"] = [preview.returncode, preview.stdout, preview.stderr]
            seen["preview_wrote"] = upstream.tickets() != before
            digest = next((line.split()[-1] for line in preview.stdout.splitlines() if line.strip().startswith("digest:")), "")
            seen["digest"] = digest

            # 2. The digest is bound to every part of the report and to the destination.
            def submit(*changed: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
                return cli(*changed, "--submit-upstream", "--expect", digest, env=env)

            variants = {
                "title": [*report[:2], "Another title", *report[3:]],
                "body": [*report[:4], "A different body", *report[5:]],
                "requested state": [*report, "--defer"],
                "source ref": [*report[:6], "mefp:MEFP-13"],
                "source project": [*report, "--origin-project", "otto"],
            }
            seen["changed"] = {name: submit(*args) for name, args in variants.items()}
            seen["changed_destination"] = submit(*report, env={**pane_env, "TICKET_BOARD_REPORT_URL": other.base})
            seen["submit_needs_expect"] = cli(*report, "--submit-upstream")
            seen["expect_needs_submit"] = cli(*report, "--expect", digest)
            seen["after_refusals"] = upstream.tickets() == before and other.tickets() == []

            # The board checks it again itself: a one-step request, a borrowed digest, another board's digest.
            body = {"title": "Board refuses a valid report", "body": "The board refused it.\nSteps: one, two.",
                    "origin_project": "mefp", "external_source_ref": "mefp:MEFP-12"}
            seen["server_one_step"] = post(upstream.base, body, token=TOKEN)
            seen["server_borrowed"] = post(upstream.base, {**body, "title": "Other",
                                                                                 "confirmation": digest}, token=TOKEN)
            otto_digest = rc.report_digest(destination={"project": "otto", "ticket_prefix": "OTTO"}, source_project="mefp",
                                           title=body["title"], body=body["body"], external_source_ref=body["external_source_ref"])
            seen["server_other_board"] = post(upstream.base, {**body, "confirmation": otto_digest}, token=TOKEN)
            seen["server_state"] = post(upstream.base, {**body, "state": "done",
                                                                              "confirmation": digest}, token=TOKEN)
            seen["server_no_token"] = post(upstream.base, {**body, "confirmation": digest}, token=None)
            seen["after_server_refusals"] = upstream.tickets() == before

            # 3. The exact submission creates one ticket, attributed; replays create nothing more.
            filed = submit(*report)
            seen["filed"] = [filed.returncode, filed.stderr[-300:]]
            seen["filed_tickets"] = upstream.tickets()
            replay = submit(*report)
            seen["replay"] = [replay.returncode, replay.stdout, replay.stderr]
            seen["server_replay"] = post(upstream.base, {**body, "confirmation": digest}, token=TOKEN)
            seen["after_replays"] = upstream.tickets()
            # Two identical first submissions at once still make one.
            racing = [*report[:2], "Racing report", *report[3:]]
            race_digest = rc.report_digest(destination={"project": "syrd", "ticket_prefix": "SYRD"}, source_project="mefp",
                                           title="Racing report", body=report[4], external_source_ref=report[6])
            results: list[int] = []
            threads = [threading.Thread(target=lambda: results.append(
                cli(*racing, "--submit-upstream", "--expect", race_digest).returncode)) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            seen["race"] = [sorted(results), [x["title"] for x in upstream.tickets()].count("Racing report")]

            # Existing deliberate reports keep working: the Backlog request, confirmed.
            deferred = [*report[:2], "Deferred report", *report[3:], "--defer"]
            deferred_preview = cli(*deferred)
            deferred_digest = next((line.split()[-1] for line in deferred_preview.stdout.splitlines()
                                    if line.strip().startswith("digest:")), "")
            seen["deferred_preview"] = deferred_preview.stdout
            seen["deferred"] = cli(*deferred, "--submit-upstream", "--expect", deferred_digest).returncode
            seen["deferred_comment"] = t.psql(upstream.admin, "SELECT string_agg(text, ' | ') FROM ticket_board.ticket_comments "
                                                              "c JOIN ticket_board.tickets x ON x.id = c.ticket_id "
                                                              "WHERE x.title = 'Deferred report';")

            # 4. Configuration that cannot work stops at the preview, creating nothing.
            count = len(upstream.tickets())
            closed = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
            dead_url = f"http://127.0.0.1:{closed.server_port}"
            closed.server_close()
            seen["no_url"] = cli(*report, env={k: v for k, v in pane_env.items() if k != "TICKET_BOARD_REPORT_URL"})
            # The credential is read during the preview: a file that is missing, unreadable or empty is refused
            # there, naming the file and never a token.
            missing = Path(pane) / "missing.env"
            unreadable = Path(pane) / "unreadable.env"
            unreadable.write_text(f"TICKET_BOARD_TENANT_REPORT_TOKEN={TOKEN}\n")
            unreadable.chmod(0)
            empty = Path(pane) / "empty.env"
            empty.write_text("# no token here\nTICKET_BOARD_TENANT_REPORT_TOKEN=\n")
            seen["bad_credentials"] = {name: cli(*report, env={**pane_env, "TICKET_BOARD_TENANT_REPORT_TOKEN_FILE": str(path)})
                                       for name, path in (("missing", missing), ("unreadable", unreadable), ("empty", empty))}
            seen["no_token"] = cli(*report, env={k: v for k, v in pane_env.items()
                                                 if k != "TICKET_BOARD_TENANT_REPORT_TOKEN_FILE"})
            seen["unreachable"] = cli(*report, env={**pane_env, "TICKET_BOARD_REPORT_URL": dead_url})
            seen["own_board"] = cli(*report, env={**pane_env, "TICKET_BOARD_REPORT_URL": tenant.base})
            seen["after_config"] = len(upstream.tickets()) == count and tenant.tickets() == []

            # 5. Local operations never contact the upstream board.
            local_env = {**pane_env, "TICKET_BOARD_REPORT_URL": f"http://127.0.0.1:{recorder.server_port}"}
            created = cli("create-ticket", "--title", "Local work", "--body", "ours", env=local_env)
            local_id = json.loads(created.stdout)["id"] if created.returncode == 0 else ""
            seen["local_ops"] = [created.returncode,
                                 cli("add-comment", local_id, "--text", "a note", env=local_env).returncode,
                                 cli("route", local_id, "--state", "in_progress", "--assignee", "main", env=local_env).returncode]
            seen["local_where"] = [(x["id"], x["state"]) for x in tenant.tickets()]
            seen["recorder"] = list(Recorder.seen)

            # 6. No one-call path, whoever claims to be calling: no helper fills in a digest, and the ordinary
            # client's unconfirmed submission -- with any reporter name in it -- is refused by the board.
            from scripts.ticket_board.write_client import TicketBoardWriteClient
            seen["no_helper"] = [name for name in dir(rc) if "machine" in name.lower() or "auto" in name.lower()]
            client = TicketBoardWriteClient(board_url=tenant.base, caller_role="director", report_board_url=upstream.base,
                                            report_token_file=str(token_file))
            try:
                client.file_report(title="Listener crashed", body="trace\n\nFiled by mefp-health.",
                                   origin_project="mefp", external_source_ref="machine:mefp-health")
                seen["one_call"] = "filed"
            except Exception as exc:
                seen["one_call"] = str(exc)
            seen["one_call_created"] = any(x["title"] == "Listener crashed" for x in upstream.tickets())
            seen["outputs"] = outputs
        finally:
            recorder.shutdown()
            for board in (upstream, tenant, other):
                board.close()
    return seen


def verify(seen: dict) -> None:
    code, out, err = seen["preview"]
    check(code == 0 and not seen["preview_wrote"], f"one call files nothing: {code} {err[-200:]}")
    for words in ("PREVIEW: nothing has been filed", "a NEW ticket on ANOTHER project's board", "(syrd), tickets SYRD-N",
                  "from:        project mefp, caller role director", "title:       Board refuses a valid report",
                  "state:       Triage", "source ref:  mefp:MEFP-12", "attachments: none",
                  "body:        2 line(s), 38 characters: The board refused it. Steps: one, two.",
                  "only for a Switchyard platform defect", "ticket-board-write create-ticket",
                  f"--submit-upstream --expect {seen['digest']}"):
        check(words in out, f"the preview says {words!r}: {out}")
    check(len(seen["digest"]) == 24, f"a digest: {seen['digest']}")
    for name, done in seen["changed"].items():
        check(done.returncode != 0 and "this is not the report that was previewed" in done.stderr,
              f"a changed {name} is refused: {done.stderr[-200:]}")
    check(seen["changed_destination"].returncode != 0
          and "this is not the report that was previewed" in seen["changed_destination"].stderr,
          f"another destination is refused: {seen['changed_destination'].stderr[-200:]}")
    check("needs --expect" in seen["submit_needs_expect"].stderr and "--expect only confirms a submission"
          in seen["expect_needs_submit"].stderr, "submission and digest come together, or not at all")
    check(seen["after_refusals"], "and nothing reached either board")
    expected = {"server_one_step": "file_report needs a confirmed preview",
                "server_borrowed": "confirmation does not match this report on this board",
                "server_other_board": "confirmation does not match this report on this board",
                "server_state": "file_report cannot set: state",
                "server_no_token": "missing or invalid X-Ticket-Board-Report-Token"}
    check(all(seen[k][0] == 403 and words in seen[k][1] for k, words in expected.items()) and seen["after_server_refusals"],
          f"the upstream board refuses a one-step report, a borrowed digest and another board's digest itself: "
          f"{ {k: (seen[k][0], seen[k][1][:100]) for k in expected} } {seen['after_server_refusals']}")
    filed = [x for x in seen["filed_tickets"] if x["title"] == "Board refuses a valid report"]
    check(seen["filed"][0] == 0 and len(filed) == 1 and filed[0]["origin"] == "mefp" and filed[0]["ref"] == "mefp:MEFP-12"
          and filed[0]["state"] == "analysis", f"the exact submission files one, attributed: {seen['filed']} {filed}")
    check(seen["replay"][0] == 0 and "already filed as SYRD-" in seen["replay"][2]
          and seen["server_replay"][0] == 200 and json.loads(seen["server_replay"][1]).get("replayed") is True
          and seen["after_replays"] == seen["filed_tickets"],
          f"replays create nothing: {seen['replay'][2][-200:]}")
    check(seen["race"] == [[0, 0, 0], 1], f"three identical submissions at once file one: {seen['race']}")
    check("Backlog (deferred)" in seen["deferred_preview"] and seen["deferred"] == 0
          and "requested Backlog" in (seen["deferred_comment"] or ""),
          f"a deferred report still works once confirmed: {seen['deferred_comment']}")
    check(seen["no_url"].returncode != 0 and "is not connected to an upstream board" in seen["no_url"].stderr
          and seen["no_token"].returncode != 0 and "is not connected to an upstream board" in seen["no_token"].stderr,
          f"missing configuration stops at the preview: {seen['no_url'].stderr[-150:]}")
    words = {"missing": "cannot read report token file", "unreadable": "cannot read report token file",
             "empty": "holds no report token"}
    for name, done in seen["bad_credentials"].items():
        check(done.returncode != 0 and words[name] in done.stderr and "Nothing was filed" in done.stderr
              and "PREVIEW" not in done.stdout and "digest:" not in done.stdout,
              f"a {name} credential file is refused at the preview, with no preview or digest: {done.stderr[-200:]}")
    check(seen["unreachable"].returncode != 0 and "cannot read the upstream board" in seen["unreachable"].stderr
          and "Nothing was filed" in seen["unreachable"].stderr, f"so does an unreachable one: {seen['unreachable'].stderr[-200:]}")
    check(seen["own_board"].returncode != 0 and "the report board is mefp's own board" in seen["own_board"].stderr,
          f"and a report board that is this project's own: {seen['own_board'].stderr[-200:]}")
    check(seen["after_config"], "none of them created anything anywhere")
    check(seen["local_ops"] == [0, 0, 0] and seen["recorder"] == [] and seen["local_where"] == [("MEFP-1", "in_progress")],
          f"local create, comment and route stay local and never contact the upstream: {seen['local_ops']} {seen['recorder']}")
    check(seen["no_helper"] == [] and "file_report needs a confirmed preview" in seen["one_call"]
          and not seen["one_call_created"],
          f"there is no one-call path, whatever reporter name a caller uses: {seen['no_helper']} {seen['one_call'][:160]}")
    check(not any(TOKEN in text for text in seen["outputs"]), "the report token is in no output, error or preview")


def test_the_board_ui_offers_no_report_path() -> None:
    ui = "".join(p.read_text() for p in (ROOT / "scripts/ticket_board").glob("frontend*.py"))
    check("file_report" not in ui and "Report-Token" not in ui and "report_token" not in ui,
          "the board page creates tickets on its own board only; it has no upstream report path")


def test_the_skill_and_tenant_guide_say_when_and_how() -> None:
    skill = " ".join((ROOT / "skills/switchyard-board/SKILL.md").read_text().split())
    guide = " ".join((ROOT / "docs/onboarding/switchyard-board-guide.md").read_text().split())
    for name, text in (("skill", skill), ("guide", guide)):
        check("only for a Switchyard platform defect" in text and "create-ticket" in text
              and "--submit-upstream --expect <digest>" in text, f"the {name} says what file-report is for and how")
    check("No configured token, URL or flag is standing permission" in skill
          and "Nothing replaces the per-report digest: not a configured URL, not a token, not a flag" in guide,
          "neither treats configuration as consent")


def main() -> int:
    test_the_board_ui_offers_no_report_path()
    test_the_skill_and_tenant_guide_say_when_and_how()
    verify(scenario())
    print(f"upstream_report_confirmation_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
