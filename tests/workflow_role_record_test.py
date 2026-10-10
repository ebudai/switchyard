#!/usr/bin/env python3
"""A role added after adoption reaches root's workflow record, and only that role (SYRD-562).

Otto added `uiux` (display slot 5) with a reviewed `workflow apply` after root had
recorded its declared workflow, so root's record -- which upgrade and recovery
read -- never had it. The tenant cannot write root's record; an operator now
takes exactly that role from the board with `adopt-workflow --add-role`.

The end-to-end case runs a real board on a temporary PostgreSQL cluster, adds
the role with the tenant's own `workflow apply`, and drives the real
`adopt-workflow` as root inside a user namespace, reading the real board over
HTTP. It then asks the upgrade's and recovery's own readers what they see.
"""

from __future__ import annotations

import contextlib
import copy
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board.project_provision import workflow_document_digest  # noqa: E402
from scripts.workflow_reconcile import missing_roles, record_difference, with_role_added  # noqa: E402

from adopt_workflow_test import (  # noqa: E402
    JOURNAL_ENV, OPERATOR, SEAM, SLUG, adopt, adopting_fixture, declared_document, journal_entries,
)
from resume_provision_test import namespaces_available  # noqa: E402

CHECKS = 0
ROLE = "uiux"
BOARD_ENV = "SYRD562_BOARD_URL"
TOKEN_ENV = "SYRD562_WRITE_TOKEN"
STATE_ENV = "SYRD562_STATE"


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def otto_shape() -> dict:
    """The declared workflow as root recorded it, with display slot 5 free (otto ran ops headless)."""
    document = declared_document()
    next(r for r in document["roles"] if r["name"] == "ops")["slot"] = None
    return document


def with_uiux(document: dict, *, slot: int = 5, runtime: str = "codex") -> dict:
    """The document as the tenant's reviewed apply leaves it: one more implementer, given a place."""
    added = copy.deepcopy(document)
    ops = next(r for r in added["roles"] if r["name"] == "ops")
    role = {**copy.deepcopy(ops), "name": ROLE, "label": "UI/UX", "runtime": runtime,
            "target": f"{document['project']}-{ROLE}:0.0", "slot": slot}
    added["roles"].insert(added["roles"].index(ops) + 1, role)
    for stage in added["stages"]:
        if "ops" in stage.get("owners") or []:
            stage["owners"].insert(stage["owners"].index("ops") + 1, ROLE)
    for transition in added["transitions"]:
        if "ops" in transition.get("actors") or []:
            transition["actors"].insert(transition["actors"].index("ops") + 1, ROLE)
    return added


def seat(document: dict, role: str) -> tuple:
    found = next((r for r in document.get("roles") or [] if r.get("name") == role), None)
    return None if found is None else (found.get("name"), found.get("runtime"), found.get("slot"), found.get("target"))


# ---------------------------------------------------------------- unprivileged


def test_only_the_role_and_its_places_are_taken() -> None:
    recorded = declared_document()
    live = with_uiux(recorded)
    # Unrelated policy the board and root disagree on: none of it may come along.
    main = next(r for r in live["roles"] if r["name"] == "main")
    main["runtime"] = "claude" if main["runtime"] != "claude" else "codex"
    live["reassign"] = {"draft": ROLE}
    in_progress = next(s for s in live["stages"] if s["name"] == "in_progress")
    in_progress["notify"] = {"kind": "role", "role": ROLE}
    check(missing_roles(recorded, live) == [ROLE], missing_roles(recorded, live))

    merged, left_out = with_role_added(recorded, live, ROLE)
    check(seat(merged, ROLE) == seat(live, ROLE), "the role exactly as the board declares it")
    check([r["name"] for r in merged["roles"]] == [r["name"] for r in live["roles"]], "in the board's order")
    check(seat(merged, "main") == seat(recorded, "main"), "main keeps root's runtime")
    check(merged["reassign"] == recorded["reassign"] and merged["queue"] == recorded["queue"], merged["reassign"])
    merged_stage = next(s for s in merged["stages"] if s["name"] == "in_progress")
    check(merged_stage["owners"] == in_progress["owners"], merged_stage["owners"])
    check(merged_stage["notify"] == next(s for s in recorded["stages"] if s["name"] == "in_progress")["notify"],
          "a stage's notify role is policy, not a place")
    gained = [t for t in merged["transitions"] if ROLE in t["actors"]]
    check(gained and len(gained) == len([t for t in live["transitions"] if ROLE in t["actors"]]), len(gained))
    check(any("reassign draft" in line for line in left_out) and any("stage in_progress" in line for line in left_out),
          f"what was not taken is said: {left_out}")
    # Remove the role again and nothing else of root's record moved.
    stripped = copy.deepcopy(merged)
    stripped["roles"] = [r for r in stripped["roles"] if r["name"] != ROLE]
    for stage in stripped["stages"]:
        stage["owners"] = [o for o in stage["owners"] if o != ROLE]
    for transition in stripped["transitions"]:
        transition["actors"] = [a for a in transition["actors"] if a != ROLE]
    check(stripped == recorded, "every other line of root's record is root's")
    check(recorded == declared_document(), "root's record itself was not modified")


def test_a_role_that_cannot_be_added_is_refused() -> None:
    recorded = declared_document()
    for role, live, expected in (("nope", with_uiux(recorded), "has no role"),
                                 ("ops", with_uiux(recorded), "already declares")):
        try:
            with_role_added(recorded, live, role)
        except Exception as exc:  # noqa: BLE001 - any other failure is the defect, said as one
            check(isinstance(exc, ValueError) and expected in str(exc), repr(exc))
        else:
            check(False, f"{role} was added")


def reconcile(recorded: dict, live: dict, **kwargs) -> tuple[int, str, list[str], list[str]]:
    from scripts.workflow_reconcile import reconcile_recorded_workflow

    said: list[str] = []
    writes: list[str] = []
    status, detail = reconcile_recorded_workflow(
        SLUG, recorded, config=object(), config_path=None, operator_name="an-operator",
        board_reader=lambda _c: (live, ""), say=said.append,
        write_record=lambda slug, doc: writes.append(workflow_document_digest(doc)) or Path("/r"),
        read_record=lambda slug: (recorded, ""), **{"apply": False, "from_live": "", "replacing": "", **kwargs})
    return status, detail, said, writes


def test_the_preview_says_what_it_did_not_take() -> None:
    recorded = otto_shape()
    live = with_uiux(recorded)
    live["reassign"] = {"draft": ROLE}
    status, detail, said, writes = reconcile(recorded, live, add_role=ROLE)
    check(status == 0 and detail == "dry-run" and not writes, (status, detail))
    check(any(line.startswith("switchyard: not taken from the board: reassign draft") for line in said), said[-6:])
    check(any("every other difference above stays" in line for line in said), said[-6:])


def test_a_role_whose_slot_root_still_gives_another_is_refused() -> None:
    """The board moved ops off slot 5 and gave it to the new role; root's record still seats ops there."""
    recorded = declared_document()
    live = with_uiux(otto_shape())
    status, detail, said, writes = reconcile(
        recorded, live, add_role=ROLE, apply=True,
        from_live=workflow_document_digest(live), replacing=workflow_document_digest(recorded))
    check(status == 1 and detail == "refused: role cannot be added" and not writes, (status, detail, said[-2:]))
    check(any("visible slots must be unique" in line for line in said), said[-2:])


def test_workflow_apply_names_the_operator_step_for_an_added_role() -> None:
    from scripts import workflow_manage as wm

    recorded = declared_document()
    err = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(err):
        config = Path(tmp) / "c.json"
        config.write_text(json.dumps({"project": SLUG}), encoding="utf-8")
        check(wm._report_roles_root_lacks(config, recorded, recorded) == [], "no role added: nothing said")
        check(wm._report_roles_root_lacks(config, None, recorded) == [], "first activation is adoption's business")
        check(wm._report_roles_root_lacks(config, recorded, with_uiux(recorded)) == [ROLE], err.getvalue())
    check(f"`pkexec switchyard adopt-workflow {SLUG} --add-role {ROLE}`" in err.getvalue(), err.getvalue())


def test_the_role_reaches_the_command() -> None:
    seen: dict = {}
    original = launcher.switchyard_adopt_workflow_command

    def recorded(slug, **kwargs):
        seen.update(slug=slug, **kwargs)
        return 0

    launcher.switchyard_adopt_workflow_command = recorded
    try:
        try:
            status = launcher.switchyard_main(["adopt-workflow", SLUG, "--add-role", ROLE])
        except SystemExit as exc:  # argparse's refusal is an answer, not a crash
            status = f"parser refused: exit {exc.code}"
    finally:
        launcher.switchyard_adopt_workflow_command = original
    check(status == 0 and seen.get("add_role") == ROLE and seen.get("apply") is False, seen)


# ------------------------------------------------------------------ privileged


def http_board(base: str):
    def reader(_config):
        try:
            with urllib.request.urlopen(base + "/api/workflow", timeout=10) as response:
                return json.load(response)["document"], ""
        except OSError as exc:
            return None, f"the board could not be read: {exc}"
    return reader


def http_board_state(base: str):
    def reader(_config):
        with urllib.request.urlopen(base + "/api/workflow", timeout=10) as response:
            state = json.load(response)
        return state["revision"], state["document"], ""
    return reader


def privileged_cases() -> None:
    base = os.environ[BOARD_ENV]
    root = Path(os.environ[STATE_ENV])
    os.environ[SEAM] = str(root)
    os.environ[JOURNAL_ENV] = str(root / "journal")
    os.environ["PKEXEC_UID"] = "0"
    os.environ.pop("SUDO_USER", None)
    release, home, installed, tenant_plan, config_path = adopting_fixture(root)
    record = launcher.workflow_record_path(SLUG)
    from scripts.workflow_launcher import projection_files
    from scripts.workflow_manage import apply_files

    board = http_board(base)
    adopted = board(None)[0]
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["workflow"] = adopted
    config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    apply_files(projection_files(config_path, adopted))
    # A role names a record to change; with none held it is refused, never ignored.
    status, said = adopt(root, home, apply=True, add_role=ROLE, board_reader=board)
    check(status == 1 and any("root holds none" in line for line in said) and not record.exists(), said[-2:])
    status, said = adopt(root, home, apply=True, board_reader=board)
    check(status == 0 and record.is_file(), said[-3:])
    recorded, _problem = launcher.recorded_declared_workflow(SLUG)
    check(seat(recorded, ROLE) is None, "root's record starts without the role")

    # The tenant adds the role with its own reviewed apply, unprivileged as it runs.
    os.environ["TICKET_BOARD_WRITE_TOKEN"] = os.environ[TOKEN_ENV]
    os.environ["TICKET_BOARD_CALLER_ROLE"] = "director"
    importlib.reload(importlib.import_module("scripts.ticket_board.write_client"))
    wm = importlib.reload(importlib.import_module("scripts.workflow_manage"))
    document = root / "with-uiux.json"
    document.write_text(json.dumps(with_uiux(adopted)), encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            patch.object(wm, "_hand_projection_to_tenant", lambda *_paths: None):
        # The hand-over is a no-op for the unprivileged tenant this stands in for.
        check(wm.main(["apply", "--document", str(document), "--config", str(config_path),
                       "--board-url", base]) == 0, err.getvalue()[-800:])
    live = board(None)[0]
    check(seat(live, ROLE) is not None, "the board runs the added role")
    check(f"adopt-workflow {SLUG} --add-role {ROLE}" in err.getvalue(),
          f"apply names the operator's step: {err.getvalue()[-600:]}")
    untouched = record.read_bytes()
    root_digest, live_digest = workflow_document_digest(recorded), workflow_document_digest(live)

    # Before the fix, upgrade and recovery would read a record with no such role.
    migration = launcher.plan_workflow_migration(
        launcher.load_project_config(SLUG, config_path), config_path=config_path, board_reader=http_board_state(base))
    check(seat(migration.document or {}, ROLE) is None, "upgrade reads root's record, which lacks it")

    # 1. Plain preview: the role root lacks, and the command that adds only it.
    status, said = adopt(root, home, board_reader=board)
    joined = "\n".join(said)
    check(status == 0 and f"pkexec switchyard adopt-workflow {SLUG} --add-role {ROLE}" in joined, joined[-800:])
    check(record.read_bytes() == untouched, "a preview writes nothing")

    # 2. The role's own preview: what root's record becomes, and the exact apply.
    status, said = adopt(root, home, add_role=ROLE, board_reader=board)
    joined = "\n".join(said)
    command = (f"pkexec switchyard adopt-workflow {SLUG} --apply --add-role {ROLE} "
               f"--from-live {live_digest} --replacing {root_digest}")
    check(status == 0 and command in joined, joined[-900:])
    check(f'+    "name": "{ROLE}",' in joined or f'"name": "{ROLE}"' in joined, "the added role is shown")
    check(record.read_bytes() == untouched and journal_entries(root)[-1]["detail"] == "dry-run",
          journal_entries(root)[-1])

    # 3. Refusals: --apply alone, a role the board does not run, one root has, a moved board.
    for kwargs, expected in (
            (dict(apply=True, add_role=ROLE), "--apply alone does not choose"),
            (dict(apply=True, add_role="nope", from_live=live_digest, replacing=root_digest), "has no role"),
            (dict(apply=True, add_role="ops", from_live=live_digest, replacing=root_digest), "already declares"),
            (dict(apply=True, add_role=ROLE, from_live="f" * 64, replacing=root_digest), "the board now holds"),
            (dict(apply=True, add_role=ROLE, from_live=live_digest, replacing="e" * 64), "root now holds")):
        status, said = adopt(root, home, board_reader=board, **kwargs)
        check(status == 1 and any(expected in line for line in said), (kwargs, said[-3:]))
        check(record.read_bytes() == untouched, f"refused, nothing written: {kwargs}")

    # 4. The reviewed apply: root's record gains the role, journalled with the operator.
    status, said = adopt(root, home, apply=True, add_role=ROLE, from_live=live_digest, replacing=root_digest,
                         board_reader=board)
    check(status == 0 and any(f"role {ROLE} only" in line and OPERATOR.name in line for line in said), said[-3:])
    stored, problem = launcher.recorded_declared_workflow(SLUG)
    check(stored is not None and seat(stored, ROLE) == seat(live, ROLE), problem)
    info = os.stat(record)
    check(info.st_uid == 0 and (info.st_mode & 0o777) == 0o600, oct(info.st_mode))
    check(journal_entries(root)[-1]["detail"] == f"role {ROLE} added from the board", journal_entries(root)[-1])
    expected, _left = with_role_added(recorded, live, ROLE)
    check(stored == expected, "root's record is its previous record plus the role, nothing else")

    # 5. Root, tenant and board now name the same role, runtime and slot -- and so do
    # the upgrade's and recovery's own readers.
    tenant = json.loads(config_path.read_text(encoding="utf-8"))["workflow"]
    migration = launcher.plan_workflow_migration(
        launcher.load_project_config(SLUG, config_path), config_path=config_path, board_reader=http_board_state(base))
    from scripts.root_plan_reconstruction import plan_workflow_from_root
    rebuilt, problem = plan_workflow_from_root(tenant_plan, declares_workflow=True)
    seats = {"root": seat(stored, ROLE), "tenant": seat(tenant, ROLE), "board": seat(board(None)[0], ROLE),
             "upgrade": seat(migration.document or {}, ROLE), "recovery": seat(rebuilt.workflow or {}, ROLE)}
    check(len(set(seats.values())) == 1 and seats["root"] == (ROLE, "codex", 5, f"{SLUG}-{ROLE}:0.0"), seats)
    check(not problem, problem)
    # Measured: adopting from the board leaves root's record in the board's form,
    # so with the role added the two are one document and upgrade has nothing to do.
    check(workflow_document_digest(stored) == live_digest and record_difference(stored, live) == [],
          record_difference(stored, live)[:20])
    check(migration.already_installed and not migration.problems, migration.problems)

    # 6. Asked again, nothing is missing and nothing is written.
    before_bytes = record.read_bytes()
    status, said = adopt(root, home, add_role=ROLE, board_reader=board)
    check(status == 0 and any("agree" in line for line in said), said[-3:])
    check(record.read_bytes() == before_bytes, "all agree: nothing written")

    # 7. A role added together with an unrelated change: only the role reaches root.
    current = board(None)[0]
    second = copy.deepcopy(current)
    qa = {**copy.deepcopy(seat_role(current, ROLE)), "name": "qa", "label": "QA", "slot": None,
          "target": f"{SLUG}-qa:0.0"}
    second["roles"].append(qa)
    main_role = seat_role(second, "main")
    moved_from = main_role["runtime"]
    main_role["runtime"] = "claude" if moved_from != "claude" else "codex"
    document.write_text(json.dumps(second), encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
            patch.object(wm, "_hand_projection_to_tenant", lambda *_paths: None):
        check(wm.main(["apply", "--document", str(document), "--config", str(config_path),
                       "--board-url", base]) == 0, "the tenant's second apply")
    live2 = board(None)[0]
    held, _problem = launcher.recorded_declared_workflow(SLUG)
    held_digest, live2_digest = workflow_document_digest(held), workflow_document_digest(live2)
    status, said = adopt(root, home, add_role="qa", board_reader=board)
    joined = "\n".join(said)
    check(status == 0 and "every other difference above stays" in joined, joined[-600:])
    status, said = adopt(root, home, apply=True, add_role="qa", from_live=live2_digest, replacing=held_digest,
                         board_reader=board)
    check(status == 0, said[-3:])
    stored2, _problem = launcher.recorded_declared_workflow(SLUG)
    check(seat(stored2, "qa") == seat(live2, "qa"), "qa reached root")
    check(seat_role(stored2, "main")["runtime"] == moved_from, "main keeps the runtime root reviewed")
    check(seat(stored2, ROLE) == seat(live2, ROLE), "the earlier role is still there")
    # What is left between root and the board is exactly the unrelated change.
    remaining = [line for line in record_difference(stored2, live2) if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    check(sorted(remaining) == sorted([f'-      "runtime": "{moved_from}",', f'+      "runtime": "{main_role["runtime"]}",']),
          remaining)
    migration = launcher.plan_workflow_migration(
        launcher.load_project_config(SLUG, config_path), config_path=config_path, board_reader=http_board_state(base))
    check(seat(migration.document or {}, "qa") == seat(live2, "qa"), "upgrade reads the role")
    check(any("different digest" in problem for problem in migration.problems),
          f"and still fails closed on the change nobody reviewed: {migration.problems}")


def seat_role(document: dict, role: str) -> dict:
    return next(r for r in document["roles"] if r["name"] == role)


def run_board(run_child) -> int:
    import ticket_board_write_api_test as t
    from scripts.ticket_board.workflow_config import validate
    from temporary_cluster import temporary_cluster

    with temporary_cluster(prefix="syrd562-db-", shutdown="immediate") as cluster:
        sock, port, db = cluster.socket_dir, cluster.port, "syrd562"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, t.SCHEMA_PATH.read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        app = t.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project=SLUG,
                               ticket_prefix="TST", database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE))
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        t.TEST_WRITE_TOKEN = server.write_token
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            t.post_json(base, "/api/tickets/actions/configure_workflow",
                        {"document": validate(otto_shape(), project=SLUG), "expected_revision": 0},
                        caller="director")
            return run_child(base, server.write_token)
        finally:
            server.shutdown()
            server.server_close()


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    if "--privileged-child" in sys.argv:
        privileged_cases()
        print(f"workflow_role_record_test: privileged child {CHECKS} checks ok")
        return 0
    if not namespaces_available():
        print("workflow_role_record_test: FAILED: user namespaces are unavailable, so the privileged cases did not run")
        return 1

    def run_child(base: str, token: str) -> int:
        with tempfile.TemporaryDirectory(prefix="syrd562.") as tmp:
            Path(tmp).chmod(0o755)
            env = {**os.environ, BOARD_ENV: base, TOKEN_ENV: token, STATE_ENV: tmp}
            done = subprocess.run(["unshare", "--user", "--map-root-user", sys.executable, __file__,
                                   "--privileged-child"], text=True, capture_output=True, env=env)
        print(done.stdout.strip())
        if done.returncode != 0 or "privileged child" not in done.stdout:
            print(done.stderr[-3000:])
            print("workflow_role_record_test: FAILED in the privileged child")
            return 1
        return 0

    if run_board(run_child) != 0:
        return 1
    print(f"workflow_role_record_test: {CHECKS} unprivileged checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
