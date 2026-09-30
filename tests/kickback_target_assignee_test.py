#!/usr/bin/env python3
"""SYRD-307: the kick-back commands' options mean what the declared workflow does.

`director-dat-kick-back --target-assignee luna-2` was advertised and refused
"unknown workflow action payload field" on MEFP, three times over (SYRD-307,
427, 455, 483), while the reason-only kick-back returned the ticket to its
recorded implementer. The declared executor ignores any requested assignee on
a `return`: a kick-back always goes back to the implementer. So on a declared
board a target cannot be chosen -- and a reviewer choosing one would be new
authority. `--target-assignee` is now a confirmation: accepted when it names
exactly where the return goes, refused before anything changes when it does
not. `inspector-kick-back` could not be used at all: its `recommendations`
were not in the declared schema either; they are its reason now.

Every board is built as production builds one (companion roles, schema.sql,
the real ticket-board-migrate, rbac.sql, a declared workflow) and driven
through the real `ticket-board-write` CLI and HTTP server. The reproduction
runs the code and SQL from before this change, in a child process.
"""

from __future__ import annotations

import contextlib
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
BEFORE = "8e031b773858b1a5046d7b70ab22cf207713bc4c"  # main before SYRD-307
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
COMMIT = "6d4ee1aa99147e8118f59e637be02b660d62d064"
KICKBACKS = (
    # command, stage it leaves, who may run it, its text option
    ("audit-kick-back", "audit", "audit", "--reason"),
    ("director-dat-kick-back", "dat", "director", "--reason"),
    ("inspector-kick-back", "inspection", "inspector", "--recommendations"),
)
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_board_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def scenario(root: Path, prefix: str) -> dict:
    """Every kick-back, each way, from the tree at `root` -- its code and its SQL."""
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import copy

    import ticket_board_write_api_test as t
    from scripts.ticket_board import write_cli
    from temporary_cluster import temporary_cluster
    from workflow_document_eras import before_relaying

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        db = "kickbacks"
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
        app._resolve_known_commit = lambda value: (COMMIT, [])
        doc = copy.deepcopy(before_relaying())
        doc["project"] = "cerulean"
        doc.setdefault("reassign", {})
        doc.setdefault("remove_stages", [])
        with app._pg_connect() as conn:
            app._pg_set_caller_role(conn, "director")
            conn.execute("SELECT set_config('ticket_board.project','cerulean',false)")
            conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(doc),))
            conn.commit()
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cli(role: str, *argv: str) -> tuple[int, str]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                try:
                    code = write_cli.main(["--board-url", f"http://127.0.0.1:{server.server_port}",
                                           "--caller-role", role, f"--write-token={server.write_token}", *argv])
                except SystemExit as exc:
                    code = int(exc.code or 0)
            return code, out.getvalue()

        def reviewed(ticket: str, stage: str) -> None:
            """Implemented by ops, submitted, and waiting in `stage`."""
            t.seed_postgres_ticket(admin, ticket, title=ticket, state="in_progress", assignee="ops",
                                   needs_inspection=(stage == "inspection"), needs_user_signoff=(stage == "dat"))
            app.perform_workflow_action(ticket, "submit_to_audit", {"commit_hash": COMMIT}, caller_role="ops")
            if stage == "dat":
                app.perform_workflow_action(ticket, "audit_sign_off", {"text": "approved"}, caller_role="audit")
            here = app.get_ticket(ticket)
            assert here["state"] == stage, (ticket, here["state"])

        number = 30
        try:
            for command, stage, role, text_option in KICKBACKS:
                for label, extra in (("confirm", ("--target-assignee", " Ops ")),
                                     ("elsewhere", ("--target-assignee", "main")),
                                     ("reason-only", ())):
                    number += 1
                    ticket = f"PGU-{number}"
                    reviewed(ticket, stage)
                    code, output = cli(role, command, ticket, text_option, f"{label}: fix it", *extra)
                    after = app.get_ticket(ticket)
                    seen[f"{command} {label}"] = {
                        "code": code, "output": output[-400:], "state": after["state"], "assignee": after["assignee"],
                        "audit_signoff": after["audit_signoff"],
                        "comment": next((c["text"] for c in reversed(after["comments"]) if f"{label}: fix it" in c["text"]), ""),
                    }
                    # Park it, so the implementer is free for the next case.
                    app.perform_workflow_action(ticket, "defer", {}, caller_role="director")
            # A transition that is not a return has no target to name.
            number += 1
            reviewed(f"PGU-{number}", "audit")
            try:
                app.perform_workflow_action(f"PGU-{number}", "audit_sign_off", {"text": "ok", "target_assignee": "ops"},
                                            caller_role="audit")
                seen["non-return"] = "accepted"
            except Exception as exc:  # noqa: BLE001
                seen["non-return"] = str(exc)
            seen["non-return state"] = app.get_ticket(f"PGU-{number}")["state"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    clean_board_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd307b-")))
        return 0

    with tempfile.TemporaryDirectory(prefix="syrd307.") as tmp:
        root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root)],
                               capture_output=True, text=True, env={**clean_board_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-2000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    for command, stage, _role, _option in KICKBACKS:
        confirm = before[f"{command} confirm"]
        check(confirm["code"] != 0 and "unknown workflow action payload field" in confirm["output"]
              and confirm["state"] == stage,
              f"reproduced: {command} --target-assignee was refused, nothing moved: {confirm}")
    inspector = before["inspector-kick-back reason-only"]
    check(inspector["code"] != 0 and "unknown workflow action payload field" in inspector["output"],
          f"reproduced: inspector-kick-back could not be used at all: {inspector}")

    after = scenario(ROOT, "syrd307a-")
    for command, stage, _role, _option in KICKBACKS:
        confirm, elsewhere, plain = (after[f"{command} {label}"] for label in ("confirm", "elsewhere", "reason-only"))
        check(confirm["code"] == 0 and (confirm["state"], confirm["assignee"]) == ("in_progress", "ops"),
              f"{command} --target-assignee naming the recorded implementer returns it there: {confirm}")
        check(elsewhere["code"] != 0 and "returns" in elsewhere["output"] and "recorded implementer, ops" in elsewhere["output"]
              and "cannot send it to main" in elsewhere["output"] and elsewhere["state"] == stage,
              f"{command} --target-assignee naming anyone else is refused, nothing moved: {elsewhere}")
        check(plain["code"] == 0 and (plain["state"], plain["assignee"]) == ("in_progress", "ops"),
              f"{command} without a target still returns to the implementer: {plain}")
        check(confirm["comment"] and plain["comment"], f"{command}'s text is recorded: {confirm['comment']!r}")
    check(after["director-dat-kick-back confirm"]["audit_signoff"] is False,
          "the DAT return still clears the Audit approval it no longer stands on")
    check("takes no target assignee" in after["non-return"] and after["non-return state"] == "audit",
          f"an action that is not a return takes no target, and nothing moves: {after['non-return']!r}")
    print(f"kickback_target_assignee_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
