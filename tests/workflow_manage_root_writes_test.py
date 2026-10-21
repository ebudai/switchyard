#!/usr/bin/env python3
"""SYRD-623: root does not make the Director's workflow writes.

`ticket-board-workflow` -- `switchyard role-prompt`, `design-stage`,
`router-role` and `worker-pool ... rollback` reach it too -- read and wrote the
tenant's provision directory by path whenever an operator ran it under sudo:
the launcher config, the projection's staleness check, the rollback journal
(written before the board, which refuses root, was asked), the projection
itself and its rollback. It has no trusted record of whose tree that is.

Now every operation that writes the projection, the journal or the onboarding
migration refuses effective uid 0 before anything else -- even for a dry run --
and says to run it from the Director's own session. The privileged half runs
each one as root in a user namespace, under a recorder of every pathname lookup
or change in a planted tenant tree (which must see none) and a stand-in board
(which must not be asked), and proves the tree unchanged byte for byte. The
read-only commands still run as root, and the unprivileged Director still
applies, rolls back and migrates as before.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import copy
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import live_boundary  # noqa: E402

live_boundary.isolate()  # SYRD-627: no live home, pane store or runtime directory, here or in a child

from scripts import workflow_manage as wm  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402

SHIPPED = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
SECRET = "https://token@example.invalid/secret"
WRITES = ("apply", "rollback", "set-role-prompt", "clear-role-prompt", "migrate-director-onboarding",
          "add-design-stage", "enable-router", "disable-router",
          "enable-documentation-gate", "disable-documentation-gate")  # SYRD-624
READS = ("show", "show-role-prompt", "validate", "prepare-role")
CHECKS = 0


def check(condition: object, message: object) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def refusal(operation: str) -> str:
    return (
        f"ticket-board-workflow: {operation} writes the tenant's workflow projection and rollback "
        "journal, so root does not run it. Run the same command from the project's registered "
        "Director session, unprivileged, not under sudo or pkexec; the onboarding migration is "
        "`switchyard finish-upgrade <project>` there."
    )


def snapshot(top: Path) -> list[tuple]:
    seen = []
    for item in [top, *sorted(top.rglob("*"))]:
        status = item.lstat()
        if stat.S_ISLNK(status.st_mode):
            seen.append((str(item), status.st_uid, status.st_gid, "link", os.readlink(item)))
            continue
        xattrs = sorted((name, os.getxattr(item, name)) for name in os.listxattr(item) if name.startswith("user."))
        acl = subprocess.run(["getfacl", "-cpn", str(item)], capture_output=True, text=True).stdout
        body = item.read_bytes() if stat.S_ISREG(status.st_mode) else b""
        seen.append((str(item), status.st_uid, status.st_gid, stat.S_IMODE(status.st_mode), acl, xattrs, body))
    return seen


@contextlib.contextmanager
def lookups(top: Path):
    """Every pathname lookup or change of something under `top` -- by `pathlib` or `os` -- while this holds."""
    seen: list[str] = []
    prefix = str(top).rstrip("/") + "/"

    def note(*targets):
        for target in targets:
            text = os.fsdecode(target) if isinstance(target, (str, bytes, os.PathLike)) else ""
            if text.startswith(prefix):
                seen.append(text)

    patches = []
    for holder, name in ((Path, "exists"), (Path, "is_dir"), (Path, "is_file"), (Path, "is_symlink"), (Path, "stat"),
                         (Path, "lstat"), (Path, "resolve"), (Path, "read_text"), (Path, "read_bytes"),
                         (Path, "write_text"), (Path, "write_bytes"), (Path, "open"), (Path, "mkdir"),
                         (Path, "touch"), (Path, "unlink"), (Path, "iterdir"), (os, "stat"), (os, "lstat"),
                         (os, "access"), (os, "readlink"), (os.path, "exists"), (os.path, "lexists"),
                         (os.path, "isdir"), (os.path, "isfile"), (os.path, "islink"), (os.path, "realpath"),
                         (builtins, "open"), (io, "open"), (os, "open"), (os, "unlink"), (os, "remove"),
                         (os, "rename"), (os, "replace"), (os, "mkdir"), (os, "makedirs"), (os, "rmdir"),
                         (os, "chmod"), (os, "chown"), (os, "lchown"), (os, "symlink"), (os, "link"),
                         (os, "listdir"), (os, "scandir")):
        real = getattr(holder, name)

        def spy(*args, __real=real, __name=name, **kwargs):
            note(*(args[:2] if __name in ("rename", "replace", "symlink", "link") else args[:1]))
            return __real(*args, **kwargs)

        patches.append((holder, name, real))
        setattr(holder, name, spy)
    try:
        yield seen
    finally:
        for holder, name, real in patches:
            setattr(holder, name, real)


class Board:
    """The board's workflow endpoint and its write client, held in memory; every request is recorded."""

    def __init__(self, document: dict | None, revision: int = 1) -> None:
        self.document, self.revision, self.calls = document, revision, []

    def urlopen(self, url, *args, **kwargs):
        self.calls.append(("read", str(url)))
        payload = json.dumps({"revision": self.revision, "document": self.document}).encode()
        return contextlib.nullcontext(io.BytesIO(payload))

    def client(self, **kwargs):
        board = self

        class Client:
            def configure_workflow(self, document, *, expected_revision, dry_run=False):
                board.calls.append(("configure", dry_run))
                if expected_revision != board.revision:
                    raise RuntimeError(f"revision {board.revision}, not {expected_revision}")
                validated = validate(copy.deepcopy(document))
                if not dry_run:
                    board.document, board.revision = validated, board.revision + 1
                return {"revision": board.revision}

        self.calls.append(("client", sorted(kwargs)))
        return Client()


@contextlib.contextmanager
def standing_in(board: Board):
    saved = urllib.request.urlopen, wm.TicketBoardWriteClient
    urllib.request.urlopen, wm.TicketBoardWriteClient = board.urlopen, board.client
    try:
        yield board
    finally:
        urllib.request.urlopen, wm.TicketBoardWriteClient = saved


def tenant(base: Path) -> tuple[Path, Path]:
    """A provisioned tenant: its launcher config, its project artifact and its project directory."""
    provision, project = base / "provision", base / "project"
    (provision / "launcher").mkdir(parents=True)
    (project / "docs" / "onboarding").mkdir(parents=True)
    (provision / "cerulean.project.json").write_text(json.dumps({"project": {"repository": str(project)}}))
    config = provision / "launcher" / "cerulean.json"
    config.write_text(json.dumps({
        "project": "cerulean", "layout": "layout.json", "board_url": "http://127.0.0.1:9",
        "roles": [{"role": "ops", "cli": ["/nonexistent/claude"], "slot": 0}],
        "worktree_base": str(base / "worktrees"), "repository": str(project),
    }))
    return config, project


def run_main(argv: list[str]) -> tuple[object, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = wm.main(argv)
        except SystemExit as exc:
            code = exc.code
        except Exception as exc:  # a fault is a failed check, not a crash
            code = f"{type(exc).__name__}: {exc}"
    return code, out.getvalue(), err.getvalue()


def operations() -> set[str]:
    """The operations `main` accepts, read from its own parser."""
    class Seen(Exception):
        pass

    held: list[set[str]] = []
    real = argparse.ArgumentParser.parse_args

    def capture(self, *args, **kwargs):
        held.append({choice for action in self._actions for choice in (action.choices or ())})
        raise Seen

    argparse.ArgumentParser.parse_args = capture
    try:
        wm.main(["show"])
    except Seen:
        pass
    finally:
        argparse.ArgumentParser.parse_args = real
    return held[0]


def test_every_operation_is_either_refused_to_root_or_read_only() -> None:
    check(operations() == set(WRITES) | set(READS), sorted(operations()))
    check(set(getattr(wm, "TENANT_WRITES", ())) == set(WRITES), "exactly the writes are refused to root")


def director_cases() -> None:
    """The tenant's Director, unprivileged as it runs: every write still lands, journalled."""
    assert os.geteuid() != 0
    with tempfile.TemporaryDirectory(prefix="syrd623.") as tmp:
        config, _project = tenant(Path(tmp))
        board = Board(validate(copy.deepcopy(SHIPPED)))
        common = ["--config", str(config), "--board-url", "http://127.0.0.1:9"]
        with standing_in(board):
            code, out, err = run_main(["set-role-prompt", "--role", "ops", "--prompt", "Mind the yard.", *common])
            check(code == 0, (code, err[-600:]))
            ops = next(r for r in board.document["roles"] if r["name"] == "ops")
            check(board.revision == 2 and ops.get("onboarding_prompt") == "Mind the yard.", board.document["roles"])
            projected = json.loads((config.parent / "workflow.json").read_text())
            check(projected == board.document, "the projection is the board's document")
            journal = Path(json.loads(out)["journal"])
            check(journal == config.parent / "workflow-before-1.json" and journal.is_file(), out)

            before = (config.parent / "workflow.json").read_bytes()
            code, out, err = run_main(["clear-role-prompt", "--role", "ops", "--dry-run", *common])
            check(code == 0 and "Mind the yard." not in out and board.revision == 2, (code, err[-400:]))
            check((config.parent / "workflow.json").read_bytes() == before, "a dry run writes nothing")

            code, out, err = run_main(["rollback", "--journal", str(journal), *common])
            check(code == 0, (code, err[-600:]))
            ops = next(r for r in board.document["roles"] if r["name"] == "ops")
            check(board.revision == 3 and "onboarding_prompt" not in ops, ops)

            code, out, err = run_main(["migrate-director-onboarding", *common])
            check(code == 0, (code, err[-600:]))
            director = next(r for r in board.document["roles"] if r["name"] == "director")
            check(board.document["migrations"] and (director.get("onboarding_prompt") or director.get("onboarding")),
                  director)

            document = Path(tmp) / "document.json"
            document.write_text(json.dumps(board.document))
            code, out, err = run_main(["apply", "--document", str(document), "--journal",
                                       str(Path(tmp) / "fresh.json"), *common])
            check(code == 0 and json.loads(out)["journal"] == str(Path(tmp) / "fresh.json"), (code, err[-600:]))


def planted(base: Path) -> dict[str, Path]:
    """A tenant whose projection and default journal are links to a foreign, root-only file."""
    config, _project = tenant(base)
    foreign = base / "foreign"
    foreign.mkdir(mode=0o700)
    (foreign / "config.json").write_text(json.dumps({"project": "cerulean", "board_url": SECRET}))
    os.setxattr(foreign / "config.json", "user.mark", b"foreign")
    (config.parent / "workflow.json").symlink_to(foreign / "config.json")
    (config.parent / "workflow-before-1.json").symlink_to(foreign / "config.json")
    paths = {"config": config, "document": base / "document.json", "prompt": base / "prompt.txt",
             "journal": base / "provision" / "journal.json", "saved": base / "provision" / "saved.json"}
    paths["document"].write_text(json.dumps(SHIPPED))
    paths["prompt"].write_text("Mind the yard.")
    # A saved journal, as an apply leaves it, for rollback to read.
    paths["saved"].write_text(json.dumps({"previous": {"revision": 0, "document": validate(copy.deepcopy(SHIPPED))},
                                          "desired": None, "previous_files": {}}))
    return paths


def root_cases() -> None:
    """Root, for each write and its dry run: refused before any tenant path or the board is touched."""
    assert os.geteuid() == 0
    with tempfile.TemporaryDirectory(prefix="syrd623.") as tmp:
        for operation in WRITES:
            for dry in ([], ["--dry-run"]):
                label = " ".join([operation, *dry])
                base = Path(tmp) / label.replace(" ", "_")
                base.mkdir()
                paths = planted(base)
                before = snapshot(base)
                board = Board(validate(copy.deepcopy(SHIPPED)))
                argv = [operation, "--config", str(paths["config"]), "--board-url", "http://127.0.0.1:9",
                        "--document", str(paths["document"]), "--rollback-document", str(paths["document"]),
                        "--role", "director", "--prompt-file", str(paths["prompt"]),
                        "--journal", str(paths["saved" if operation == "rollback" else "journal"]),
                        "--runtime", "claude", *dry]
                with standing_in(board), lookups(base) as seen:
                    code, out, err = run_main(argv)
                check(code == refusal(operation), f"{label}: {code!r}")
                check(seen == [], f"{label}: root looked up {seen[:5]}")
                check(board.calls == [], f"{label}: root asked the board {board.calls[:3]}")
                check(SECRET not in out + err + str(code), f"{label}: printed the foreign file")
                check(snapshot(base) == before, f"{label}: the tree changed")

        for operation in READS:
            check(wm.refuse_root_write(operation) is None, f"{operation} is not refused")
        board = Board(validate(copy.deepcopy(SHIPPED)))
        with standing_in(board):
            code, out, _err = run_main(["show", "--board-url", "http://127.0.0.1:9"])
        check(code == 0 and json.loads(out)["revision"] == 1, "show still runs as root")

        # The entry point an operator types, as a process of its own.
        base = Path(tmp) / "entry"
        base.mkdir()
        paths = planted(base)
        before = snapshot(base)
        done = subprocess.run([sys.executable, str(ROOT / "scripts" / "ticket-board-workflow"), "apply",
                               "--config", str(paths["config"]), "--document", str(paths["document"]),
                               "--board-url", "http://127.0.0.1:9"], capture_output=True, text=True)
        check(done.returncode != 0 and refusal("apply") in done.stderr, (done.returncode, done.stderr[-600:]))
        check(snapshot(base) == before, "the entry point changed the tree")


def main() -> int:
    if "--privileged-child" in sys.argv:
        root_cases()
        print(f"workflow_manage_root_writes_test: privileged child ran {CHECKS} checks")
        return 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    director_cases()
    done = subprocess.run(["unshare", "--user", "--map-root-user", sys.executable, __file__, "--privileged-child"],
                          capture_output=True, text=True)
    print(done.stdout.strip())
    if done.returncode != 0 or "privileged child" not in done.stdout:
        print(done.stderr[-3000:])
        print("workflow_manage_root_writes_test: FAILED in the privileged child")
        return 1
    print(f"workflow_manage_root_writes_test: {CHECKS} unprivileged checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
