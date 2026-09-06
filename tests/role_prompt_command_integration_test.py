#!/usr/bin/env python3
"""End-to-end role-prompt writes against a live board (SYRD-36).

Drives the same entry point `switchyard role-prompt` forwards to, against a real
board and real projection files, rather than calling the mutation helper directly.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402

PROMPT = "Ops remit.\n\n- Verify staging first.\n- Never deploy on Friday.\n"


def rejected(fn, reason=""):
    """Return the refusal text.

    argparse refusals raise SystemExit(2) and put the message on stderr rather than in
    the exception, so both are captured and searched.
    """
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            fn()
    except (Exception, SystemExit) as exc:
        message = f"{exc}\n{stderr.getvalue()}"
        if reason:
            assert reason in message, f"{reason!r} not in {message!r}"
        return message
    raise AssertionError(f"expected rejection containing {reason!r}")


def _run(wm, argv: list[str]) -> dict:
    """Run one workflow_manage operation and return its JSON output."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert wm.main(argv) == 0
    return json.loads(buffer.getvalue())


def _board_document(base: str) -> dict:
    import urllib.request

    with urllib.request.urlopen(base + "/api/workflow") as response:
        return json.load(response)


def _ops_role(document: dict) -> dict:
    return next(r for r in document["roles"] if r["name"] == "ops")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="role-prompt-db-") as tmp:
        root = Path(tmp)
        data = root / "data"
        sock = root / "socket"
        sock.mkdir()
        port = t.free_port()
        db = "role_prompt_test"
        admin = t.conninfo(sock, port, db)
        t.run(["initdb", "-D", str(data), "-A", "trust", "--no-locale", "--username=postgres"])
        t.run(
            [
                "pg_ctl", "-D", str(data),
                "-o", f"-k {sock} -p {port} -h ''",
                "-l", str(root / "postgres.log"), "-w", "start",
            ]
        )
        try:
            t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
            t.psql(admin, t.SCHEMA_PATH.read_text())
            t.create_roles(admin)
            t.psql(admin, t.RBAC_PATH.read_text())

            app = t.TicketBoardApp(
                root / "frames",
                root / "assets",
                project="cerulean",
                ticket_prefix="PGU",
                database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE),
            )
            document = validate(
                json.loads((ROOT / "examples/workflows/inspection.json").read_text())
            )
            server = t.TicketBoardServer(
                ("127.0.0.1", 0), app, director_notifier=t.QuietNotifier()
            )
            t.TEST_WRITE_TOKEN = server.write_token
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                t.post_json(
                    base,
                    "/api/tickets/actions/configure_workflow",
                    {"document": document, "expected_revision": 0},
                    caller="director",
                )

                launcher_dir = root / "launcher"
                launcher_dir.mkdir()
                # The projection resolves the project artifact one level above the
                # launcher config; the migration reads the project directory from it.
                project_dir = root / "project"
                (project_dir / "docs" / "onboarding").mkdir(parents=True)
                (root / "cerulean.project.json").write_text(
                    json.dumps({"project": {"repository": str(project_dir)}}), encoding="utf-8"
                )
                config_path = launcher_dir / "cerulean.json"
                config_path.write_text(
                    json.dumps({"project": "cerulean", "layout": "layout.json", "roles": []}),
                    encoding="utf-8",
                )
                workflow_json = launcher_dir / "workflow.json"

                os.environ["TICKET_BOARD_WRITE_TOKEN"] = server.write_token
                os.environ["TICKET_BOARD_CALLER_ROLE"] = "director"
                write_client = importlib.import_module("scripts.ticket_board.write_client")
                importlib.reload(write_client)
                wm = importlib.reload(importlib.import_module("scripts.workflow_manage"))

                base_args = ["--board-url", base, "--config", str(config_path)]

                # The shipped example leaves the director without stored onboarding,
                # which is the state every existing tenant starts in.
                def _director() -> dict:
                    return next(
                        r for r in _board_document(base)["document"]["roles"]
                        if r["name"] == "director"
                    )

                assert not _director().get("onboarding_prompt"), _director()

                # --- set ------------------------------------------------------
                before = _board_document(base)["revision"]
                result = _run(
                    wm, ["set-role-prompt", "--role", "ops", "--prompt", PROMPT, *base_args]
                )
                after = _board_document(base)
                assert result["revision"] > before, (before, result)
                assert _ops_role(after["document"])["onboarding_prompt"] == PROMPT
                # The projection on disk carries it too, written atomically.
                assert _ops_role(json.loads(workflow_json.read_text()))["onboarding_prompt"] == PROMPT
                assert Path(result["journal"]).exists(), "no rollback journal was written"

                # --- show -----------------------------------------------------
                shown = _run(wm, ["show-role-prompt", "--role", "ops", "--board-url", base])
                assert shown["onboarding_prompt"] == PROMPT
                assert shown["role"] == "ops"

                # --- unknown role --------------------------------------------
                rejected(
                    lambda: wm.main(["show-role-prompt", "--role", "nope", "--board-url", base]),
                    "unknown role: nope",
                )

                # --- optimistic concurrency ----------------------------------
                # A stale expected revision must lose rather than overwrite.
                stale = _board_document(base)["revision"] - 1
                rejected(
                    lambda: wm.main(
                        [
                            "set-role-prompt", "--role", "ops", "--prompt", "racing write",
                            "--expected-revision", str(stale), *base_args,
                        ]
                    )
                )
                assert _ops_role(_board_document(base)["document"])["onboarding_prompt"] == PROMPT

                # --- authorization -------------------------------------------
                # The board enforces director-only configuration writes; a role that is
                # not the director must be refused rather than quietly succeeding.
                os.environ["TICKET_BOARD_CALLER_ROLE"] = "main"
                write_client = importlib.reload(write_client)
                wm_as_main = importlib.reload(importlib.import_module("scripts.workflow_manage"))
                message = rejected(
                    lambda: wm_as_main.main(
                        ["set-role-prompt", "--role", "ops", "--prompt", "not mine", *base_args]
                    )
                )
                # The board's own words, so this cannot pass on an unrelated failure such as a
                # bad token or an unreachable board.
                assert "only director may configure workflow" in message.lower(), message
                assert _ops_role(_board_document(base)["document"])["onboarding_prompt"] == PROMPT

                os.environ["TICKET_BOARD_CALLER_ROLE"] = "director"
                importlib.reload(write_client)
                wm = importlib.reload(importlib.import_module("scripts.workflow_manage"))

                # --- an existing path-based configuration survives ------------
                # Upgrading a role that already uses the packaged remit file must not
                # discard that path when a prompt is added or removed.
                path_doc = json.loads(json.dumps(_board_document(base)["document"]))
                _ops_role(path_doc)["onboarding"] = "/srv/cerulean/docs/onboarding/ops.md"
                t.post_json(
                    base,
                    "/api/tickets/actions/configure_workflow",
                    {"document": path_doc, "expected_revision": _board_document(base)["revision"]},
                    caller="director",
                )
                _run(wm, ["set-role-prompt", "--role", "ops", "--prompt", "newer remit", *base_args])
                ops = _ops_role(_board_document(base)["document"])
                assert ops["onboarding"] == "/srv/cerulean/docs/onboarding/ops.md"
                assert ops["onboarding_prompt"] == "newer remit"

                # --- director migration --------------------------------------
                # Migration rides on ordinary use: the first write above already filled
                # the director, so no tenant needs a manual step.
                assert str(project_dir) in _director()["onboarding_prompt"], _director()

                # The explicit operation is therefore idempotent here: nothing to do, no
                # revision spent, no journal written.
                revision_before = _board_document(base)["revision"]
                repeat = _run(wm, ["migrate-director-onboarding", *base_args])
                assert repeat["migrated"] is False, repeat
                assert repeat["reason"] == "director onboarding already stored"
                assert _board_document(base)["revision"] == revision_before

                # It fills only the director; no unrelated role acquires a prompt.
                seeded_roles = sorted(
                    r["name"]
                    for r in _board_document(base)["document"]["roles"]
                    if r.get("onboarding_prompt")
                )
                assert seeded_roles == ["director", "ops"], seeded_roles

                # --- clear ----------------------------------------------------
                _run(wm, ["clear-role-prompt", "--role", "ops", *base_args])
                ops = _ops_role(_board_document(base)["document"])
                assert "onboarding_prompt" not in ops, ops
                # Clearing the prompt leaves the packaged path in place: the generic
                # fallback, not a wipe.
                assert ops["onboarding"] == "/srv/cerulean/docs/onboarding/ops.md"
                assert "onboarding_prompt" not in _ops_role(json.loads(workflow_json.read_text()))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
        finally:
            t.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"])

    print("role_prompt_command_integration_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
