#!/usr/bin/env python3
"""End-to-end role-prompt writes against a live board (SYRD-36).

Drives the same entry point `switchyard role-prompt` forwards to, against a real
board and real projection files, rather than calling the mutation helper directly.
"""

from __future__ import annotations

import contextlib
import copy
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
                config_path = launcher_dir / "cerulean.json"  # noqa: E501
                config_path.write_text(
                    json.dumps(
                        {
                            "project": "cerulean",
                            "layout": "layout.json",
                            # A role entry is required for switchyard to recognise this as
                            # a project config, which the public command's resolution needs.
                            "roles": [{"role": "ops"}],
                            "board_url": base,
                            # Distinct per-role workdirs; without this every role would
                            # default to the same directory and the config is rejected.
                            "worktree_base": str(root / "worktrees"),
                            "repository": str(project_dir),
                        }
                    ),
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
                assert repeat["reason"] == "already migrated"
                assert _board_document(base)["revision"] == revision_before

                # It fills only the director; no unrelated role acquires a prompt.
                seeded_roles = sorted(
                    r["name"]
                    for r in _board_document(base)["document"]["roles"]
                    if r.get("onboarding_prompt")
                )
                assert seeded_roles == ["director", "ops"], seeded_roles

                # --- upgrading a tenant whose board predates phase one --------
                # The running board is made to validate with main's pre-feature schema,
                # which is the release actually deployed today. It rejects the new fields,
                # so the migration must detect that and stage the deployment rather than
                # attempt a write the board cannot accept.
                import importlib.util as _ilu
                import subprocess as _sp

                old_schema = _sp.run(
                    ["git", "show", "origin/main:scripts/ticket_board/workflow_config.py"],
                    cwd=str(ROOT), capture_output=True, text=True,
                )
                if old_schema.returncode == 0:
                    old_path = root / "old_workflow_config.py"
                    old_path.write_text(old_schema.stdout, encoding="utf-8")
                    old_spec = _ilu.spec_from_file_location("old_wc", old_path)
                    old_module = _ilu.module_from_spec(old_spec)
                    old_spec.loader.exec_module(old_module)

                    # Put the tenant back into the unmigrated state a real pre-phase-one
                    # board would be in, while the new validator is still active.
                    unmigrated = copy.deepcopy(_board_document(base)["document"])
                    unmigrated.pop("migrations", None)
                    for role in unmigrated["roles"]:
                        if role["name"] == "director":
                            role.pop("onboarding_prompt", None)
                            role["onboarding"] = None
                    t.post_json(
                        base,
                        "/api/tickets/actions/configure_workflow",
                        {"document": unmigrated,
                         "expected_revision": _board_document(base)["revision"]},
                        caller="director",
                    )
                    assert "migrations" not in _board_document(base)["document"]

                    wc = importlib.import_module("scripts.ticket_board.workflow_config")
                    current_validate = wc.validate
                    revision_before_probe = _board_document(base)["revision"]
                    wc.validate = old_module.validate  # the board now speaks the old schema
                    try:
                        staged = _run(
                            wm,
                            ["migrate-director-onboarding", *base_args,
                             "--journal", str(launcher_dir / "journal-oldboard.json")],
                        )
                    finally:
                        wc.validate = current_validate

                    assert staged["migrated"] is False, staged
                    assert staged["reason"] == wm.PHASE_ONE_BOARD_REQUIRED, staged
                    assert "deploy the phase-one board release" in staged["action_required"]
                    # Nothing was written to a board that could not accept it.
                    assert _board_document(base)["revision"] == revision_before_probe
                    assert not (launcher_dir / "journal-oldboard.json").exists()

                # --- board committed, projection failed, then retried ---------
                # configure_workflow commits before the projection is written, so a
                # failure between them leaves the board ahead of the files. The retry
                # must repair the projection rather than report success over a stale one.
                original_apply = wm.apply_files
                wm.apply_files = lambda _files: (_ for _ in ()).throw(OSError("disk full"))
                try:
                    wm.main(
                        [
                            "set-role-prompt", "--role", "audit", "--prompt", "audit remit",
                            *base_args, "--journal", str(launcher_dir / "journal-partial.json"),
                        ]
                    )
                    raise AssertionError("expected the projection failure to surface")
                except RuntimeError as exc:
                    assert "local projection pending" in str(exc), exc
                finally:
                    wm.apply_files = original_apply

                # The board took the change; the projection did not.
                audit_role = next(
                    r for r in _board_document(base)["document"]["roles"] if r["name"] == "audit"
                )
                assert audit_role["onboarding_prompt"] == "audit remit"
                on_disk = json.loads(workflow_json.read_text())
                assert not next(
                    r for r in on_disk["roles"] if r["name"] == "audit"
                ).get("onboarding_prompt"), "projection should still be stale here"

                # The journal is the recovery evidence and must already be the tenant's,
                # not left behind by the step that failed.
                partial_journal = launcher_dir / "journal-partial.json"
                assert partial_journal.exists()
                assert partial_journal.stat().st_uid == os.getuid()

                # Retrying the migration repairs the projection.
                repaired = _run(wm, ["migrate-director-onboarding", *base_args])
                assert repaired["projection_reconciled"] is True, repaired
                on_disk = json.loads(workflow_json.read_text())
                assert next(
                    r for r in on_disk["roles"] if r["name"] == "audit"
                )["onboarding_prompt"] == "audit remit"

                # And a second retry has nothing left to repair.
                settled = _run(wm, ["migrate-director-onboarding", *base_args])
                assert settled["projection_reconciled"] is False, settled

                # --- the public command --------------------------------------
                # Everything above drives the entry point the command forwards to; this
                # drives `switchyard role-prompt` itself, including project resolution
                # and the board URL taken from the tenant's own config.
                import contextlib as _contextlib

                launcher_module = importlib.import_module("scripts.team_launcher")
                previous_config_dir = launcher_module.DEFAULT_CONFIG_DIR
                launcher_module.DEFAULT_CONFIG_DIR = launcher_dir
                try:
                    assert "role-prompt" in launcher_module.SWITCHYARD_UNPRIVILEGED_COMMANDS
                    assert "role-prompt" not in launcher_module.SWITCHYARD_PRIVILEGED_COMMANDS

                    # Compare against what the board actually holds now rather than the
                    # value set at the top; earlier sections have changed it since.
                    expected_now = _ops_role(_board_document(base)["document"])["onboarding_prompt"]
                    out = io.StringIO()
                    with _contextlib.redirect_stdout(out):
                        assert (
                            launcher_module.switchyard_main(
                                ["role-prompt", "show", "ops", "--project", "cerulean"]
                            )
                            == 0
                        )
                    assert json.loads(out.getvalue())["onboarding_prompt"] == expected_now

                    public_prompt = "Set through the public command.\n\nSecond line.\n"
                    with _contextlib.redirect_stdout(io.StringIO()):
                        assert (
                            launcher_module.switchyard_main(
                                [
                                    "role-prompt", "set", "ops",
                                    "--project", "cerulean",
                                    "--prompt", public_prompt,
                                ]
                            )
                            == 0
                        )
                    assert _ops_role(_board_document(base)["document"])["onboarding_prompt"] == public_prompt

                    with _contextlib.redirect_stdout(io.StringIO()):
                        assert (
                            launcher_module.switchyard_main(
                                ["role-prompt", "clear", "ops", "--project", "cerulean"]
                            )
                            == 0
                        )
                    assert "onboarding_prompt" not in _ops_role(_board_document(base)["document"])

                    # Restore the prompt so the assertions below still describe reality.
                    _run(wm, ["set-role-prompt", "--role", "ops", "--prompt", PROMPT, *base_args])
                finally:
                    launcher_module.DEFAULT_CONFIG_DIR = previous_config_dir

                # --- tenant ownership ----------------------------------------
                # The projection is written by the invoking tenant, so it must remain
                # owned by that tenant rather than by anything the write path escalated to.
                for written in (config_path, workflow_json):
                    assert written.stat().st_uid == os.getuid(), written
                    assert written.stat().st_gid == os.getgid(), written

                # --- clear ----------------------------------------------------
                _run(wm, ["clear-role-prompt", "--role", "ops", *base_args])
                ops = _ops_role(_board_document(base)["document"])
                assert "onboarding_prompt" not in ops, ops
                # Clearing the prompt leaves the packaged path in place: the generic
                # fallback, not a wipe.
                assert ops["onboarding"] == "/srv/cerulean/docs/onboarding/ops.md"
                assert "onboarding_prompt" not in _ops_role(json.loads(workflow_json.read_text()))

                # --- a customised director is marked, not skipped -------------
                # Nothing needs seeding here, but the marker still must be persisted:
                # without it a later clear looks like a legacy gap and gets refilled.
                # Reproduced on a fresh document so this is the customised-tenant path.
                custom = copy.deepcopy(_board_document(base)["document"])
                for role in custom["roles"]:
                    if role["name"] == "director":
                        role["onboarding_prompt"] = "director's own words"
                custom.pop("migrations", None)
                t.post_json(
                    base,
                    "/api/tickets/actions/configure_workflow",
                    {"document": custom, "expected_revision": _board_document(base)["revision"]},
                    caller="director",
                )
                assert "migrations" not in _board_document(base)["document"]

                # Distinct journals: these writes land on revisions the earlier ones
                # already journaled, and reusing a journal path is refused by design.
                marked = _run(
                    wm,
                    ["migrate-director-onboarding", *base_args,
                     "--journal", str(launcher_dir / "journal-marker.json")],
                )
                assert marked.get("migrated") is not False, marked
                persisted = _board_document(base)["document"]
                assert persisted.get("migrations", {}).get("director_onboarding") is True, persisted
                assert _director()["onboarding_prompt"] == "director's own words", _director()

                # Clearing immediately after that migration must stick.
                with _contextlib.redirect_stdout(io.StringIO()):
                    wm.main(["clear-role-prompt", "--role", "director", *base_args])
                _run(
                    wm,
                    ["set-role-prompt", "--role", "ops", "--prompt", "another write", *base_args,
                     "--journal", str(launcher_dir / "journal-after-custom-clear.json")],
                )
                assert not _director().get("onboarding_prompt"), (
                    "a customised director's clear was undone because the marker was dropped"
                )

                # --- clearing the director must persist -----------------------
                # The migration runs on every write, so without durable state it would
                # treat a deliberately cleared director as unmigrated and refill it on
                # the very next operation. Reproduced exactly: clear, then perform an
                # unrelated write, then look again.
                with _contextlib.redirect_stdout(io.StringIO()):
                    wm.main(["clear-role-prompt", "--role", "director", *base_args])
                assert not _director().get("onboarding_prompt"), _director()

                _run(
                    wm,
                    ["set-role-prompt", "--role", "ops", "--prompt", "unrelated", *base_args,
                     "--journal", str(launcher_dir / "journal-after-clear.json")],
                )
                assert not _director().get("onboarding_prompt"), (
                    "a cleared director prompt was restored by the shared write tail"
                )

                # And it stays cleared across the explicit migration too.
                repeat_after_clear = _run(wm, ["migrate-director-onboarding", *base_args])
                assert repeat_after_clear["migrated"] is False, repeat_after_clear
                assert not _director().get("onboarding_prompt"), _director()
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
