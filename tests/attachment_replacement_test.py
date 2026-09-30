#!/usr/bin/env python3
"""SYRD-519: replacing a ticket's screenshots works on a ticket that has some.

`ticket_board.edit_fields` replaced attachments with one statement: sibling
CTEs that read the old rows, DELETEd them and INSERTed the new list.
PostgreSQL finishes an unreferenced data-modifying CTE after the main query,
so the INSERT met the old rows and every replacement on a populated ticket
failed with `ticket_attachments_pkey` "Key (ticket_id, position)=(<id>, 0)
already exists" -- even resending the same list. Only an empty ticket or a
clear to [] worked.

Every board here is built as production builds one (companion roles,
schema.sql, the real ticket-board-migrate, rbac.sql) -- one legacy, one with
a declared workflow -- and edited through the real `TicketBoardApp`. The
reproduction runs the code and SQL from before this change, in a child
process.
"""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "a13b5c15108fdbf8fe3692e25c214b2d375e783d"  # main before SYRD-519
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CROP = {"crop": {"x": 1, "y": 1, "width": 2, "height": 2}}
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
    """Every replacement, on both kinds of board, from the tree at `root` -- its code and its SQL."""
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    from PIL import Image

    import ticket_board_write_api_test as t
    from temporary_cluster import temporary_cluster
    from workflow_document_eras import before_relaying

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        for kind in ("legacy", "declared"):
            admin = t.conninfo(cluster.socket_dir, cluster.port, kind)
            t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", kind])
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
            frames, assets = cluster.root / f"frames-{kind}", cluster.root / f"assets-{kind}"
            frames.mkdir()
            assets.mkdir()
            for shade, name in enumerate("abc"):
                Image.new("RGB", (4, 4), (60 * shade, 80, 120)).save(frames / f"{name}.png")
            app = t.TicketBoardApp(frames, assets, project="cerulean", ticket_prefix="PGU",
                                   database_url=t.conninfo(cluster.socket_dir, cluster.port, kind, t.SERVICE_ROLE))
            if kind == "declared":
                doc = copy.deepcopy(before_relaying())
                doc["project"] = "cerulean"
                doc.setdefault("reassign", {})
                doc.setdefault("remove_stages", [])
                with app._pg_connect() as conn:
                    app._pg_set_caller_role(conn, "director")
                    conn.execute("SELECT set_config('ticket_board.project','cerulean',false)")
                    conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(doc),))
                    conn.commit()

            def state(ticket: str) -> dict:
                rows = json.loads(t.psql(admin, (
                    "SELECT coalesce(jsonb_agg(jsonb_build_array(position, path, is_primary, source_field, metadata) "
                    f"ORDER BY position), '[]')::text FROM ticket_board.ticket_attachments WHERE ticket_id = '{ticket}';")))
                column = t.psql(admin, f"SELECT coalesce(screenshot, '') FROM ticket_board.tickets WHERE id = '{ticket}';").strip()
                return {"rows": rows, "screenshot": column, "screenshots": app.get_ticket(ticket)["screenshots"]}

            def attempt(ticket: str, patch: dict, role: str | None = "app") -> str:
                try:
                    app.update_ticket(ticket, patch, caller_role=role)
                    return "ok"
                except Exception as exc:  # noqa: BLE001
                    return f"{type(exc).__name__}: {str(exc).splitlines()[0]}"

            frame = {name: str(frames / f"{name}.png") for name in "abc"}
            here: dict = {}
            # An empty ticket takes its first screenshots.
            t.seed_postgres_ticket(admin, "PGU-1", title="one", state="in_progress", assignee="app")
            here["first"] = attempt("PGU-1", {"screenshots": [frame["a"], frame["b"]]})
            here["after first"] = state("PGU-1")
            kept_a, kept_b = (here["after first"]["screenshots"] + ["", ""])[:2]
            if kept_a:
                t.psql(admin, f"UPDATE ticket_board.ticket_attachments SET metadata = '{json.dumps(CROP)}'::jsonb "
                              f"WHERE ticket_id = 'PGU-1' AND path = '{kept_a}';")
            populated = state("PGU-1")
            # The reported case: replace a populated ticket's screenshots --
            # reorder the kept ones, drop one, add one.
            here["replace"] = attempt("PGU-1", {"screenshots": [frame["c"], kept_a]})
            here["after replace"] = state("PGU-1")
            here["kept b file"] = Path(kept_b).exists() if kept_b else None
            # Resending exactly what is there.
            current = state("PGU-1")
            here["resend"] = attempt("PGU-1", {"screenshots": current["screenshots"]})
            here["after resend"] = state("PGU-1")
            # Called with the list alone, the function itself makes the new first the ticket's screenshot.
            try:
                with app._pg_connect() as conn:
                    app._pg_set_caller_role(conn, "app")
                    conn.execute("SELECT ticket_board.edit_fields(%s, %s::jsonb)",
                                 ("PGU-1", json.dumps({"screenshots": current["screenshots"][::-1]})))
                    conn.commit()
                here["list alone result"] = "ok"
            except Exception as exc:  # noqa: BLE001
                here["list alone result"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            here["list alone"] = state("PGU-1")
            # A replacement refused by a later step of the same update leaves
            # everything as it was: the whole update is one transaction.
            before_refusal = state("PGU-1")
            here["later refusal"] = attempt("PGU-1", {"screenshots": [frame["b"]], "comment": {"who": "", "text": "x"}})
            here["after later refusal"] = state("PGU-1") == before_refusal
            # A failure inside the replacement itself, after its DELETE, undoes the DELETE.
            paths = before_refusal["screenshots"]
            try:
                with app._pg_connect() as conn:
                    app._pg_set_caller_role(conn, "app")
                    conn.execute("SELECT ticket_board.edit_fields(%s, %s::jsonb)",
                                 ("PGU-1", json.dumps({"screenshots": [paths[-1], paths[-1]]})))
                    conn.commit()
                here["inner failure"] = "ok"
            except Exception as exc:  # noqa: BLE001
                here["inner failure"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            here["after inner failure"] = state("PGU-1") == before_refusal
            # A caller the function does not admit is refused, and nothing changes.
            here["stranger"] = attempt("PGU-1", {"screenshots": [frame["a"]]}, role="stranger")
            here["after stranger"] = state("PGU-1") == before_refusal
            # Clearing, then an empty ticket's clear, still work.
            here["clear"] = attempt("PGU-1", {"screenshots": []})
            here["after clear"] = state("PGU-1")
            t.seed_postgres_ticket(admin, "PGU-2", title="two", state="in_progress", assignee="app")
            here["empty clear"] = attempt("PGU-2", {"screenshots": []})
            here["populated"] = populated
            seen[kind] = here
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
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd519b-")))
        return 0

    with tempfile.TemporaryDirectory(prefix="syrd519.") as tmp:
        root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root)],
                               capture_output=True, text=True, env={**clean_board_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-2000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    for kind in ("legacy", "declared"):
        was = before[kind]
        check(was["first"] == "ok" and len(was["after first"]["rows"]) == 2,
              f"before, {kind}: an empty ticket took its screenshots: {was['first']}")
        for step in ("replace", "resend"):
            check("ticket_attachments_pkey" in was[step], f"reproduced, {kind}: {step} on a populated ticket collided: {was[step]}")
        check(was["after replace"] == was["populated"], f"before, {kind}: and left the prior rows as they were")

    after = scenario(ROOT, "syrd519a-")
    for kind in ("legacy", "declared"):
        now = after[kind]
        first = now["after first"]
        check(now["first"] == "ok" and [r[0] for r in first["rows"]] == [0, 1]
              and [r[2] for r in first["rows"]] == [True, False] and first["screenshot"] == first["screenshots"][0],
              f"{kind}: an empty ticket takes its screenshots in order, the first primary: {now['first']} {first}")
        kept_a = now["populated"]["screenshots"][0]
        replaced = now["after replace"]
        check(now["replace"] == "ok", f"{kind}: replacing a populated ticket's screenshots succeeds: {now['replace']}")
        check([r[0] for r in replaced["rows"]] == [0, 1] and [r[2] for r in replaced["rows"]] == [True, False]
              and {r[3] for r in replaced["rows"]} == {"screenshots"},
              f"{kind}: positions are contiguous from 0 and only the first is primary: {replaced['rows']}")
        check(replaced["rows"][1][1] == kept_a and replaced["rows"][1][4] == CROP,
              f"{kind}: a kept attachment moves to its new position with its metadata: {replaced['rows']}")
        check(replaced["rows"][0][1] != kept_a and replaced["rows"][0][1].endswith(".png") and replaced["rows"][0][4] == {},
              f"{kind}: a new attachment starts with no metadata: {replaced['rows'][0]}")
        check(replaced["screenshot"] == replaced["rows"][0][1] and replaced["screenshots"] == [r[1] for r in replaced["rows"]],
              f"{kind}: the ticket's screenshot is the new first, and the list reads back in order: {replaced}")
        check(now["resend"] == "ok" and now["after resend"] == replaced,
              f"{kind}: resending the same list changes nothing: {now['resend']}")
        alone = now["list alone"]
        check(now["list alone result"] == "ok" and [r[1] for r in alone["rows"]] == replaced["screenshots"][::-1] and alone["screenshot"] == alone["rows"][0][1]
              and alone["rows"][1][4] == {} and alone["rows"][0][4] == CROP,
              f"{kind}: given only the list, edit_fields reorders it, metadata following, and refreshes the ticket's screenshot: {alone}")
        check(now["later refusal"] == "ValueError: comment requires an author" and now["after later refusal"] is True,
              f"{kind}: a refusal later in the update rolls the replacement back: {now['later refusal']}")
        check(now["inner failure"].startswith("UniqueViolation: ") and "ticket_attachments_ticket_id_path_key" in now["inner failure"] and now["after inner failure"] is True,
              f"{kind}: a failure inside the replacement restores what its DELETE removed: {now['inner failure']}")
        check(now["stranger"].startswith("InsufficientPrivilege: ") and "stranger" in now["stranger"] and now["after stranger"] is True,
              f"{kind}: a caller edit_fields does not admit is refused, nothing changes: {now['stranger']}")
        check(now["clear"] == "ok" and now["after clear"]["rows"] == [] and now["after clear"]["screenshot"] == ""
              and now["empty clear"] == "ok",
              f"{kind}: clearing, and clearing an empty ticket, still work: {now['clear']} {now['after clear']}")
    print(f"attachment_replacement_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
