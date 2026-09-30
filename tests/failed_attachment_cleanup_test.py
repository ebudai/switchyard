#!/usr/bin/env python3
"""SYRD-520: a failed attachment operation removes the files it created, and only those.

Attaching a frame copies it into the asset directory before the database
write. When the update was then refused -- by a later step, by the database's
role check, by a bad path further down the list, by a failed copy -- the
database rolled back but the copies stayed, one more orphan per attempt. A
refused create and a refused crop did the same. And a copy picked its name
from the clock and saved over whatever was there, so a name collision
overwrote someone else's file.

Now each operation creates its files exclusively and records them by inode;
if anything raises before its transaction commits (the commit included),
exactly those files are removed. Pre-existing, reused and uploaded assets are
never touched; neither is anything a concurrent operation wrote, anything
after the commit, or a file replaced since. When a removal fails, the
original error is still what the caller gets, and the files left are named on
it and in the log.

Boards are built as production builds them (companion roles, schema.sql, the
real ticket-board-migrate, rbac.sql; a declared workflow for the crop), and
driven through the real `TicketBoardApp`. The reproduction runs the code and
SQL from before this change, in a child process.
"""

from __future__ import annotations

import copy
import io
import json
import logging
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "aabb9ae09cbfc2a92f01e228cd66585173025296"  # main before SYRD-520
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
NOT_YOURS = b"someone else's file"
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
    """Every operation, failed and not, from the tree at `root` -- its code and its SQL."""
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    from PIL import Image

    import ticket_board_write_api_test as t
    from scripts.ticket_board import attachment_store
    from temporary_cluster import temporary_cluster
    from workflow_document_eras import before_relaying

    # Warnings this suite provokes on purpose go to the handler that asserts
    # them, not to stderr.
    logging.getLogger("scripts.ticket_board.new_asset_files").addHandler(logging.NullHandler())
    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = t.conninfo(cluster.socket_dir, cluster.port, "b")
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "b"])
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
        frames, assets = cluster.root / "frames", cluster.root / "assets"
        frames.mkdir()
        assets.mkdir()
        for shade, name in enumerate("abcd"):
            Image.new("RGB", (8, 8), (60 * shade, 80, 120)).save(frames / f"{name}.png")
        app = t.TicketBoardApp(frames, assets, project="cerulean", ticket_prefix="PGU",
                               database_url=t.conninfo(cluster.socket_dir, cluster.port, "b", t.SERVICE_ROLE))
        for ticket in ("PGU-1", "PGU-3"):
            t.seed_postgres_ticket(admin, ticket, title=ticket, state="in_progress", assignee="app")
        frame = {name: str(frames / f"{name}.png") for name in "abcd"}

        def listing() -> dict[str, bytes]:
            return {p.name: p.read_bytes() for p in sorted(assets.iterdir())}

        def attempt(fn) -> str:
            try:
                fn()
                return "ok"
            except Exception as exc:  # noqa: BLE001
                notes = " | ".join(getattr(exc, "__notes__", []))
                return f"{type(exc).__name__}: {str(exc).splitlines()[0]}" + (f" || {notes}" if notes else "")

        def case(name: str, fn) -> None:
            before = listing()
            result = attempt(fn)
            after = listing()
            seen[name] = {
                "result": result,
                "new": sorted(set(after) - set(before)),
                "gone": sorted(set(before) - set(after)),
                "changed": sorted(n for n in set(before) & set(after) if before[n] != after[n]),
            }

        def refused_update(paths: list[str]) -> None:
            app.update_ticket("PGU-1", {"screenshots": paths, "comment": {"who": "", "text": "x"}}, caller_role="app")

        # Success: the copy is kept and referenced.
        case("success", lambda: app.update_ticket("PGU-1", {"screenshots": [frame["a"]]}, caller_role="app"))
        kept = app.get_ticket("PGU-1")["screenshots"][0]
        seen["kept mode"] = stat.S_IMODE(os.stat(kept).st_mode) if Path(kept).is_file() else None
        seen["umask mode"] = 0o666 & ~_umask()
        # Refusals after materialization, and failures during it.
        case("refused later in the update", lambda: refused_update([kept, frame["b"], frame["c"]]))
        case("refused by the database role check",
             lambda: app.update_ticket("PGU-1", {"screenshots": [kept, frame["b"]]}, caller_role="stranger"))
        case("bad path after a copy",
             lambda: app.update_ticket("PGU-1", {"screenshots": [frame["b"], str(frames / "missing.png")]}, caller_role="app"))
        case("refused create", lambda: app.create_ticket_record(
            title="t", body="b", screenshot=None, screenshots=[frame["c"]], assignee="director", state="analysis",
            blocked_by=[], implementation="", audit_prompt="", audit_signoff=False, needs_user_signoff=False,
            user_signoff=False, comments=[{"who": "stranger", "text": "x", "ts": "2026-09-30T00:00:00+00:00"}],
            caller_role="director"))
        # Reuse: an attached asset and an upload are used in place -- never copied, consumed or removed.
        upload = app.save_uploaded_image((frames / "d.png").read_bytes(), original_filename="up.png")["path"]
        case("reuse, refused", lambda: refused_update([kept, upload]))
        case("reuse, kept", lambda: app.update_ticket("PGU-1", {"screenshots": [kept, upload]}, caller_role="app"))
        # A copy that fails part-way through writing.
        real_save = Image.Image.save

        def failing_save(self, fp, *args, **kwargs):
            real_save(self, fp, *args, **kwargs)
            raise OSError("disk full (simulated, after the write)")

        Image.Image.save = failing_save
        try:
            case("copy fails mid-write", lambda: app.update_ticket("PGU-1", {"screenshots": [kept, frame["b"]]}, caller_role="app"))
        finally:
            Image.Image.save = real_save
        # A name another file already has: it is neither overwritten nor removed.
        clock = [1790000000000000000]

        def ticking() -> int:
            clock[0] += 1
            return clock[0]

        taken = assets / f"PGU-1-{clock[0] + 1}.png"
        taken.write_bytes(NOT_YOURS)
        real_ns = attachment_store.time.time_ns
        attachment_store.time.time_ns = ticking
        try:
            case("destination name taken, refused", lambda: refused_update([kept, frame["c"]]))
        finally:
            attachment_store.time.time_ns = real_ns
        seen["taken file"] = taken.read_bytes().decode(errors="replace") if taken.exists() else None
        # A concurrent operation's files survive this one's failure.
        real_call = app._pg_call
        state = {"fired": False}

        def interleaved(conn, sql, params):
            if not state["fired"] and "edit_fields" in sql and "PGU-1" in str(params):
                state["fired"] = True
                app._pg_call = real_call
                app.update_ticket("PGU-3", {"screenshots": [frame["d"]]}, caller_role="app")
            return real_call(conn, sql, params)

        app._pg_call = interleaved
        try:
            case("concurrent success during a failure", lambda: refused_update([kept, frame["b"]]))
        finally:
            app._pg_call = real_call
        seen["concurrent ticket"] = [Path(p).name for p in app.get_ticket("PGU-3")["screenshots"]]
        # The commit itself fails: a deferred constraint that raises at COMMIT,
        # armed only for this case. The transaction is gone; so are its files.
        t.psql(admin, """
CREATE TABLE ticket_board.syrd520_fail_commit (armed boolean);
CREATE FUNCTION ticket_board.syrd520_fail_commit() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM ticket_board.syrd520_fail_commit) THEN
        RAISE EXCEPTION 'commit refused (simulated)';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER syrd520_fail_commit AFTER INSERT ON ticket_board.ticket_attachments
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ticket_board.syrd520_fail_commit();
INSERT INTO ticket_board.syrd520_fail_commit VALUES (true);
""")
        try:
            case("commit fails", lambda: app.update_ticket("PGU-1", {"screenshots": [kept, frame["b"]]}, caller_role="app"))
        finally:
            t.psql(admin, "DELETE FROM ticket_board.syrd520_fail_commit;")
        # After a commit, nothing removes its files -- not even an error closing the connection.
        real_connect = app._pg_connect

        class FailsOnClose:
            def __init__(self, conn):
                self.conn = conn

            def __enter__(self):
                return self.conn.__enter__()

            def __exit__(self, *exc):
                self.conn.__exit__(*exc)
                raise OSError("connection close failed (simulated, after the commit)")

        app._pg_connect = lambda: FailsOnClose(real_connect())
        try:
            case("error after the commit", lambda: app.update_ticket("PGU-1", {"screenshots": [kept, frame["b"]]}, caller_role="app"))
        finally:
            app._pg_connect = real_connect
        seen["after-commit ticket"] = [Path(p).name for p in app.get_ticket("PGU-1")["screenshots"]]
        app.update_ticket("PGU-1", {"screenshots": [kept]}, caller_role="app")
        # A removal that fails: the original error stands, and the file left is named.
        real_unlink = os.unlink
        logged: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: logged.append(record.getMessage())
        logging.getLogger("scripts.ticket_board.new_asset_files").addHandler(handler)

        def refusing_unlink(path, *args, **kwargs):
            if str(path).startswith("PGU-1-"):
                raise PermissionError(13, "Permission denied")
            return real_unlink(path, *args, **kwargs)

        os.unlink = refusing_unlink
        try:
            case("cleanup fails", lambda: refused_update([kept, frame["c"]]))
        finally:
            os.unlink = real_unlink
            logging.getLogger("scripts.ticket_board.new_asset_files").removeHandler(handler)
        seen["cleanup log"] = logged
        for name in seen["cleanup fails"]["new"]:
            (assets / name).unlink()
        # A file replaced after it was created is not the operation's any more.
        def replacing_call(conn, sql, params):
            if "edit_fields" in sql and "PGU-1" in str(params):
                app._pg_call = real_call
                for name in sorted(set(p.name for p in assets.iterdir()) - state["before replace"]):
                    (assets / name).unlink()
                    (assets / name).write_bytes(NOT_YOURS)
            return real_call(conn, sql, params)

        state["before replace"] = set(p.name for p in assets.iterdir())
        app._pg_call = replacing_call
        try:
            case("replaced since created", lambda: refused_update([kept, frame["b"]]))
        finally:
            app._pg_call = real_call
        # A crop refused on a declared board (which checks the caller's role).
        doc = copy.deepcopy(before_relaying())
        doc["project"] = "cerulean"
        doc.setdefault("reassign", {})
        doc.setdefault("remove_stages", [])
        with app._pg_connect() as conn:
            app._pg_set_caller_role(conn, "director")
            conn.execute("SELECT set_config('ticket_board.project','cerulean',false)")
            conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(doc),))
            conn.commit()
        crop = {"x": 0, "y": 0, "w": 2, "h": 2}
        case("refused crop", lambda: app.crop_attachment("PGU-1", source_path=kept, rect=crop, feedback_number=1,
                                                         caller_role="stranger"))
        case("crop", lambda: app.crop_attachment("PGU-1", source_path=kept, rect=crop, feedback_number=1,
                                                 caller_role="director"))
        seen["crop attached"] = [Path(p).name for p in app.get_ticket("PGU-1")["screenshots"]]
        seen["final ticket"] = [Path(p).name for p in app.get_ticket("PGU-1")["screenshots"]]
        seen["kept exists"] = Path(kept).is_file()
        seen["upload exists"] = Path(upload).is_file()
    return seen


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def consumed_upload(root: Path) -> dict:
    """An upload copied out of the asset directory is removed only when the operation is kept."""
    sys.path.insert(0, str(root))
    from PIL import Image

    from scripts.ticket_board import attachment_store, new_asset_files

    out = {}
    with tempfile.TemporaryDirectory(prefix="syrd520u.") as tmp:
        assets = Path(tmp).resolve()
        for outcome in ("discard", "keep"):
            upload = assets / f"upload_{outcome}.png"
            Image.new("RGB", (4, 4), (1, 2, 3)).save(upload)
            files = new_asset_files.NewAssetFiles()
            copied = Path(attachment_store.copy_attachment(str(upload), "PGU-9", assets, assets, files))
            left = files.discard() if outcome == "discard" else files.keep()
            out[outcome] = {"upload": upload.exists(), "copy": copied.exists(), "left": left}
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    clean_board_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd520b-")))
        return 0

    with tempfile.TemporaryDirectory(prefix="syrd520.") as tmp:
        root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root)],
                               capture_output=True, text=True, env={**clean_board_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-2000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    for name in ("refused later in the update", "refused by the database role check", "bad path after a copy",
                 "refused create", "copy fails mid-write", "refused crop"):
        check(before[name]["result"] != "ok" and before[name]["new"],
              f"reproduced: {name} left files behind: {before[name]}")
    check(before["taken file"] != NOT_YOURS.decode() and before["destination name taken, refused"]["changed"],
          f"reproduced: a copy saved over a file that was already there: {before['destination name taken, refused']}")

    now = scenario(ROOT, "syrd520a-")
    refusals = {
        "refused later in the update": "ValueError: comment requires an author",
        "refused by the database role check": "InsufficientPrivilege: role stranger cannot call edit_fields",
        "bad path after a copy": "FileNotFoundError: screenshot not found:",
        "refused create": "InsufficientPrivilege: role stranger cannot call add_comment",
        "copy fails mid-write": "OSError: disk full (simulated, after the write)",
        "destination name taken, refused": "ValueError: comment requires an author",
        "concurrent success during a failure": "ValueError: comment requires an author",
        "reuse, refused": "ValueError: comment requires an author",
        "refused crop": "InsufficientPrivilege: invalid configured caller role: stranger",
    }
    for name, refusal in refusals.items():
        check(now[name]["result"].startswith(refusal), f"{name}: the original refusal is what the caller gets: {now[name]['result']}")
        check(now[name]["gone"] == [] and now[name]["changed"] == [],
              f"{name}: nothing that was there is removed or changed: {now[name]}")
    for name in ("refused later in the update", "refused by the database role check", "bad path after a copy",
                 "refused create", "copy fails mid-write", "destination name taken, refused", "reuse, refused", "refused crop"):
        check(now[name]["new"] == [], f"{name}: the files it created are gone: {now[name]}")
    check(now["success"] == {"result": "ok", "new": now["success"]["new"], "gone": [], "changed": []}
          and len(now["success"]["new"]) == 1 and now["kept exists"],
          f"a successful attachment keeps its copy: {now['success']}")
    check(now["kept mode"] == now["umask mode"], f"a copy is created with the mode a plain write gives: {oct(now['kept mode'])}")
    check(now["reuse, kept"] == {"result": "ok", "new": [], "gone": [], "changed": []} and now["upload exists"],
          f"reused and uploaded assets are used in place, never copied or consumed: {now['reuse, kept']}")
    check(now["taken file"] == NOT_YOURS.decode(), f"a file that already had the name is untouched: {now['taken file']!r}")
    concurrent = now["concurrent success during a failure"]
    check(len(concurrent["new"]) == 1 and concurrent["new"][0].startswith("PGU-3-")
          and now["concurrent ticket"] == concurrent["new"],
          f"a concurrent operation's file, committed meanwhile, survives this one's failure: {concurrent} {now['concurrent ticket']}")
    check("commit refused (simulated)" in now["commit fails"]["result"] and now["commit fails"]["new"] == [],
          f"when the commit itself fails, its files go too: {now['commit fails']}")
    after_commit = now["error after the commit"]
    check(after_commit["result"].startswith("OSError: connection close failed") and len(after_commit["new"]) == 1
          and after_commit["new"][0] in now["after-commit ticket"],
          f"after a commit nothing removes its files, even when a later step raises: {after_commit} {now['after-commit ticket']}")
    failed = now["cleanup fails"]
    check(failed["result"].startswith("ValueError: comment requires an author || ") and "left in place" in failed["result"]
          and len(failed["new"]) == 1 and failed["new"][0] in failed["result"] and "Permission denied" in failed["result"],
          f"a removal that fails keeps the original error and names the file left: {failed}")
    check(len(now["cleanup log"]) == 1 and failed["new"][0] in now["cleanup log"][0],
          f"and logs it: {now['cleanup log']}")
    replaced = now["replaced since created"]
    check(replaced["result"].startswith("ValueError: comment requires an author || ")
          and "no longer the file this operation created" in replaced["result"] and len(replaced["new"]) == 1,
          f"a file replaced after it was created is not removed, and is named: {replaced}")
    check(now["crop"]["result"] == "ok" and len(now["crop"]["new"]) == 1 and now["crop"]["new"][0] in now["crop attached"],
          f"a crop that is kept keeps its file: {now['crop']}")

    upload = consumed_upload(ROOT)
    check(upload["discard"] == {"upload": True, "copy": False, "left": []},
          f"a failed operation does not consume the upload it copied: {upload}")
    check(upload["keep"] == {"upload": False, "copy": True, "left": None},
          f"a kept one consumes it, as before: {upload}")
    print(f"failed_attachment_cleanup_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
