#!/usr/bin/env python3
"""SYRD-521: an explicit unassigned request is honoured or refused -- never quietly handed to a worker.

MEFP's audited importer ran `create-ticket --draft --assignee unassigned` for
inert intake. The Draft stage is owned by `designer` and notifies its assignee,
and a declared board's insert gives every new draft-kind ticket to the stage's
sole owner: MEFP-129 was created owned by the designer, the designer was
notified, and acted on it before release. The CLI's own default was
`unassigned` too, so "explicit" and "omitted" could not even be told apart.

Now an omitted assignee means "let the board place it" (ordinary Draft
ownership, unchanged), and an explicit `unassigned` into a stage that would
not keep it is refused before anything is written or sent, naming the stage
where inert intake works. Every board here is built as production builds one
(companion roles, schema.sql, the real ticket-board-migrate, rbac.sql, a
declared workflow) and driven through the real `ticket-board-write` CLI and
HTTP server; the reproduction runs the code and SQL from before this change.
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
BEFORE = "262fd5cefb6db22c6e2c5e322ae16601eec2ffe2"  # main before SYRD-521
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_board_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def document(before_relaying, *, draft_owners: list[str]) -> dict:
    """The shipped workflow; with ["designer"], MEFP's shape: an active designer owns Draft."""
    doc = copy.deepcopy(before_relaying())
    doc["project"] = "cerulean"
    doc.setdefault("reassign", {})
    doc.setdefault("remove_stages", [])
    for stage in doc["stages"]:
        if stage["name"] == "draft":
            stage["owners"] = list(draft_owners)
    # The shipped document hands existing drafts to the Director; hand them to
    # the new sole owner instead, or to nobody when Draft has no single owner.
    doc["reassign"] = {k: v for k, v in doc["reassign"].items() if k != "draft"}
    if len(draft_owners) == 1:
        doc["reassign"]["draft"] = draft_owners[0]
    if "designer" in draft_owners:
        for role in doc["roles"]:
            if role["name"] == "designer":
                role["active"] = True
        for transition in doc["transitions"]:
            if transition["from"] == "draft" and transition["action"] == "release_draft":
                transition["actors"] = sorted(set(transition["actors"]) | {"designer"})
    return doc


def scenario(root: Path, prefix: str, draft_owners: list[str]) -> dict:
    """Observations from the tree at `root` -- its code and its SQL."""
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
        db = "intake"
        admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
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
        try:
            t.create_roles(admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        t.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
        runner = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")],
                                env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin},
                                capture_output=True, text=True)
        assert runner.returncode == 0, runner.stderr
        t.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        app = t.TicketBoardApp(
            cluster.root / "frames", cluster.root / "assets", project="cerulean", ticket_prefix="PGU",
            database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
        )
        with app._pg_connect() as conn:
            app._pg_set_caller_role(conn, "director")
            conn.execute("SELECT set_config('ticket_board.project','cerulean',false)")
            conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)",
                         (json.dumps(document(before_relaying, draft_owners=draft_owners)),))
            conn.commit()
        t.seed_postgres_ticket(admin, "PGU-6", title="Parent", state="analysis", assignee="director")
        t.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue;")
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cli(*argv: str) -> tuple[int, str]:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = write_cli.main(["--board-url", f"http://127.0.0.1:{server.server_port}",
                                           "--caller-role", "director", f"--write-token={server.write_token}",
                                           *argv])
                except SystemExit as exc:
                    code = int(exc.code or 0)
            return code, out.getvalue() + err.getvalue()

        def tickets() -> list[dict]:
            raw = t.psql(admin, "SELECT coalesce(jsonb_agg(jsonb_build_object('id', id, 'state', state, "
                                "'assignee', assignee, 'parent', parent_id, 'audit', needs_audit, "
                                "'user', needs_user_signoff) ORDER BY ticket_number), '[]')::text "
                                "FROM ticket_board.tickets WHERE id <> 'PGU-6';")
            return json.loads(raw)

        def notices() -> list[str]:
            raw = t.psql(admin, "SELECT coalesce(jsonb_agg(ticket_id || ':' || kind || ':' || target_role "
                                "ORDER BY id), '[]')::text FROM ticket_board.ticket_notification_queue;")
            return json.loads(raw)

        def reserved(role: str) -> str:
            return t.psql(admin, f"SELECT coalesce(ticket_board.ticket_current_reserved_ticket('{role}'), '-');").strip()

        common = ("--title", "Lead", "--body", "x", "--parent-id", "PGU-6", "--needs-user-signoff")
        try:
            # The importer's request, exactly.
            code, output = cli("create-ticket", "--draft", "--assignee", "unassigned", *common)
            seen["explicit"] = {"code": code, "output": output[-600:], "tickets": tickets(), "notices": notices()}
            t.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue;")
            before = {row["id"] for row in tickets()}
            # Omitted: the board places it.
            code, output = cli("create-ticket", "--draft", *common)
            made = [row for row in tickets() if row["id"] not in before]
            seen["omitted"] = {"code": code, "tickets": made, "notices": notices()}
            t.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue;")
            # Inert intake, where the workflow provides it.
            before = {row["id"] for row in tickets()}
            code, output = cli("create-ticket", "--state", "backlog", "--assignee", "unassigned", *common)
            made = [row for row in tickets() if row["id"] not in before]
            seen["backlog"] = {"code": code, "output": output[-300:], "tickets": made, "notices": notices(),
                               "reserved": {role: reserved(role) for role in ("designer", "director", "ops")}}
            # The web form's draft request: no assignee field at all.
            before = {row["id"] for row in tickets()}
            form = {"title": "From the form", "body": "x", "initial_state": "draft", "needs_user_signoff": False,
                    "needs_inspection": False, "needs_audit": True, "regression": False}
            try:
                from scripts.ticket_board.write_client import TicketBoardWriteClient

                TicketBoardWriteClient(f"http://127.0.0.1:{server.server_port}", caller_role="director",
                                       write_token=server.write_token)._post(
                    "/actions/create_ticket", form, caller_role="director")
                seen["form"] = [row for row in tickets() if row["id"] not in before]
            except Exception as exc:  # noqa: BLE001
                seen["form"] = f"REFUSED {exc}"
            # The web form's ordinary request: Analysis, with its select's
            # explicit "unassigned" -- the refusal is for draft-kind stages only.
            before = {row["id"] for row in tickets()}
            analysis_form = {**form, "title": "Analysis from the form", "initial_state": "analysis",
                             "assignee": "unassigned"}
            try:
                TicketBoardWriteClient(f"http://127.0.0.1:{server.server_port}", caller_role="director",
                                       write_token=server.write_token)._post(
                    "/actions/create_ticket", analysis_form, caller_role="director")
                seen["form_analysis"] = [row for row in tickets() if row["id"] not in before]
            except Exception as exc:  # noqa: BLE001
                seen["form_analysis"] = f"REFUSED {exc}"
            # Ordinary release of the omitted-owner draft.
            if seen["omitted"]["tickets"]:
                draft_id = seen["omitted"]["tickets"][0]["id"]
                released = app.perform_workflow_action(draft_id, "release_draft", {}, caller_role="director")
                seen["released"] = [released["state"], released["assignee"]]
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    return seen


def check_served_form() -> None:
    """Run the page's own createTicket under node and read the payload it posts."""
    import shutil

    if not shutil.which("node"):
        raise SystemExit("explicit_unassigned_draft_test: node is required")
    sys.path.insert(0, str(ROOT))
    from scripts.ticket_board.frontend import HTML

    start = HTML.index("async function createTicket(")
    depth = 0
    for index in range(HTML.index("{", start), len(HTML)):
        if HTML[index] == "{":
            depth += 1
        elif HTML[index] == "}":
            depth -= 1
            if depth == 0:
                break
    source = HTML[start:index + 1]
    program = """
const field = (value) => ({ value, checked: false });
const titleInput = field('t'), bodyInput = field('b'), assigneeInput = field('unassigned');
const createDraftInput = field(), createBacklogInput = field(), needsUserInput = field();
const needsInspectionInput = field(), needsAuditInput = field(), createRegressionInput = field();
const state = { pendingCreateScreenshots: [] };
const posted = [];
async function postTicketAction(path, payload) { posted.push(payload); return { ticket: { id: 'X-1' } }; }
function renderCreatePreview() {} function setCreateStatus() {} async function requestBoardReload() {}
%s
(async () => {
  createDraftInput.checked = true; await createTicket();
  createDraftInput.checked = false; await createTicket();
  process.stdout.write(JSON.stringify(posted));
})();
""" % source
    proc = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    draft, analysis = json.loads(proc.stdout)
    check(draft["initial_state"] == "draft" and "assignee" not in draft,
          f"the served form sends a draft without an assignee: {draft}")
    check(analysis["initial_state"] == "analysis" and analysis.get("assignee") == "unassigned",
          f"and anything else with the one chosen: {analysis}")


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    clean_board_env()
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd521b-", json.loads(sys.argv[3]))))
        return 0

    with tempfile.TemporaryDirectory(prefix="syrd521.") as tmp:
        root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), '["designer"]'],
                               capture_output=True, text=True,
                               env={**clean_board_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-2000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    made = before["explicit"]["tickets"]
    check(before["explicit"]["code"] == 0 and len(made) == 1 and made[0]["state"] == "draft"
          and made[0]["assignee"] == "designer",
          f"reproduced: an explicit unassigned draft was created owned by the designer: {before['explicit']}")
    check(f"{made[0]['id']}:transition:designer" in before["explicit"]["notices"],
          f"reproduced: and the designer was told: {before['explicit']['notices']}")

    # This tree, MEFP's shape.
    after = scenario(ROOT, "syrd521a-", ["designer"])
    explicit = after["explicit"]
    check(explicit["code"] != 0 and "would not stay unassigned" in explicit["output"]
          and "designer" in explicit["output"] and "backlog" in explicit["output"],
          f"the importer's request is refused, naming the owner and the inert stage: {explicit['output']!r}")
    check(explicit["tickets"] == [] and explicit["notices"] == [],
          f"before anything is written or sent: {explicit['tickets']} {explicit['notices']}")
    omitted = after["omitted"]
    check(omitted["code"] == 0 and len(omitted["tickets"]) == 1
          and omitted["tickets"][0]["state"] == "draft" and omitted["tickets"][0]["assignee"] == "designer",
          f"leaving the assignee out is ordinary Draft ownership, unchanged: {omitted}")
    check(omitted["tickets"][0]["parent"] == "PGU-6" and omitted["tickets"][0]["user"] is True,
          f"with its parent and gates: {omitted['tickets']}")
    check(after["released"] == ["analysis", "director"], f"and releases as ever: {after['released']}")
    backlog = after["backlog"]
    check(backlog["code"] == 0 and len(backlog["tickets"]) == 1
          and (backlog["tickets"][0]["state"], backlog["tickets"][0]["assignee"]) == ("backlog", "unassigned"),
          f"inert intake in backlog stays unassigned: {backlog}")
    check(backlog["tickets"][0]["parent"] == "PGU-6" and backlog["tickets"][0]["audit"] is True
          and backlog["tickets"][0]["user"] is True, f"with its parent and both review gates: {backlog['tickets']}")
    check(backlog["notices"] == [] and set(backlog["reserved"].values()) == {"-"},
          f"and nobody is told or reserved: {backlog['notices']} {backlog['reserved']}")
    check(isinstance(after["form"], list) and len(after["form"]) == 1 and after["form"][0]["assignee"] == "designer",
          f"the web form's draft request (no assignee field) still works: {after['form']}")

    check(isinstance(after["form_analysis"], list) and len(after["form_analysis"]) == 1
          and after["form_analysis"][0]["state"] == "analysis",
          f"an ordinary Analysis request with an explicit unassigned is untouched: {after['form_analysis']}")
    check_served_form()

    # The shipped workflow: nobody called designer, Draft owned by the Director.
    shipped = scenario(ROOT, "syrd521s-", ["director"])
    check(shipped["explicit"]["code"] != 0 and "director" in shipped["explicit"]["output"]
          and shipped["explicit"]["tickets"] == [],
          f"without a designer the same request is refused, naming the Director: {shipped['explicit']['output']!r}")
    # A Draft with no single owner keeps what it is given.
    unowned = scenario(ROOT, "syrd521u-", [])
    check(unowned["explicit"]["code"] == 0 and len(unowned["explicit"]["tickets"]) == 1
          and unowned["explicit"]["tickets"][0]["assignee"] == "unassigned" and unowned["explicit"]["notices"] == [],
          f"where Draft has no single owner an explicit unassigned is honoured: {unowned['explicit']}")
    print(f"explicit_unassigned_draft_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
