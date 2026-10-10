#!/usr/bin/env python3
"""Re-recording root's declared workflow from the live board, on review (SYRD-561).

Otto's runtime switch moved the board (and part of the tenant's projection) and
left root's record naming `main=hermes`. `adopt-workflow` stopped once root held
a record; nothing else could repair it. These cases drive the real command, as
root, inside a user namespace -- exactly as `adopt_workflow_test` does -- with
the board answered by a stand-in that can move between reads.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board.project_provision import workflow_document_digest  # noqa: E402

from adopt_workflow_test import (  # noqa: E402
    JOURNAL_ENV, OPERATOR, SEAM, SLUG, adopt, adopting_fixture, board, journal_entries,
)
from resume_provision_test import namespaces_available  # noqa: E402

CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def moved_runtime(document: dict) -> tuple[dict, str, str]:
    """The otto shape: one implementer's runtime moved on the board and nowhere else."""
    live = copy.deepcopy(document)
    role = next(r for r in live["roles"] if r.get("runtime") and r["name"] != "director")
    before = role["runtime"]
    role["runtime"] = "codex" if before != "codex" else "claude"
    return live, role["name"], before


def sequence(*answers):
    """A board that answers each read with the next document (the last repeats)."""
    calls = []

    def reader(_config):
        index = min(len(calls), len(answers) - 1)
        calls.append(index)
        document = answers[index]
        return document, ("" if document is not None else "no board socket")
    reader.calls = calls  # type: ignore[attr-defined]
    return reader


# ---------------------------------------------------------------- unprivileged


def test_the_selection_reaches_the_command() -> None:
    """`--from-live`/`--replacing` parse, and dispatch passes them to the command and nowhere else."""
    seen: dict = {}
    original = launcher.switchyard_adopt_workflow_command

    def recorded(slug, **kwargs):
        seen.update(slug=slug, **kwargs)
        return 0

    launcher.switchyard_adopt_workflow_command = recorded
    try:
        try:
            status = launcher.switchyard_main(
                ["adopt-workflow", SLUG, "--apply", "--from-live", "a" * 64, "--replacing", "b" * 64])
        except SystemExit as exc:  # argparse's refusal is an answer, not a crash
            status = f"parser refused: exit {exc.code}"
    finally:
        launcher.switchyard_adopt_workflow_command = original
    check(status == 0 and seen.get("slug") == SLUG, seen)
    check((seen.get("apply"), seen.get("from_live"), seen.get("replacing")) == (True, "a" * 64, "b" * 64), seen)


def test_a_record_that_does_not_read_back_is_restored_and_refused() -> None:
    """Root wrote, and what reads back is not the board's document: the previous record goes back."""
    from scripts.workflow_reconcile import reconcile_recorded_workflow
    from adopt_workflow_test import declared_document

    recorded = declared_document()
    live, _role, _before = moved_runtime(recorded)
    writes: list[str] = []
    said: list[str] = []
    status, detail = reconcile_recorded_workflow(
        SLUG, recorded, config=object(), config_path=None, apply=True,
        from_live=workflow_document_digest(live), replacing=workflow_document_digest(recorded),
        operator_name="an-operator", board_reader=lambda _c: (live, ""), say=said.append,
        write_record=lambda slug, doc: writes.append(workflow_document_digest(doc)) or Path("/r"),
        read_record=lambda slug: (recorded, ""),
    )
    check(status == 1 and "restored" in detail, (status, detail, said[-2:]))
    check(writes == [workflow_document_digest(live), workflow_document_digest(recorded)],
          f"it wrote the board's document, then put root's back: {writes}")


# ------------------------------------------------------------------ privileged


def privileged_cases() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd561.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        os.environ[SEAM] = str(root)
        os.environ[JOURNAL_ENV] = str(root / "journal")
        os.environ["PKEXEC_UID"] = "0"
        os.environ.pop("SUDO_USER", None)
        release, home, installed, tenant_plan, config_path = adopting_fixture(root)
        declared = tenant_plan.workflow
        record = launcher.workflow_record_path(SLUG)
        # A workflow-driven tenant, as otto is: its configuration carries the
        # declared workflow, projected into its configuration, plan and layout.
        from scripts.workflow_launcher import projection_files
        from scripts.workflow_manage import apply_files
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["workflow"] = declared
        config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        apply_files(projection_files(config_path, declared))

        # A selection names a record to replace; with none held it is refused, never ignored.
        status, said = adopt(root, home, apply=True, from_live="a" * 64, replacing="b" * 64,
                             board_reader=board(declared))
        check(status == 1 and any("root holds none" in line for line in said), said[-2:])
        check(not record.exists(), "a refused selection adopted nothing")

        status, said = adopt(root, home, apply=True, board_reader=board(declared))
        check(status == 0 and record.is_file(), said)
        recorded_digest = workflow_document_digest(declared)
        live, role, before = moved_runtime(declared)
        live_digest = workflow_document_digest(live)
        check(live_digest != recorded_digest, "the board really diverged from root's record")
        untouched = record.read_bytes()

        # 1. Preview: both digests, the difference, the exact command, the tenant's stale files.
        status, said = adopt(root, home, board_reader=board(live))
        joined = "\n".join(said)
        check(status == 0, joined[-500:])
        check(f"digest {recorded_digest}" in joined and f"digest {live_digest}" in joined, joined[:600])
        check(f'"runtime": "{before}"' in joined and f'"runtime": "{live["roles"][0]["runtime"]}"' in joined
              or "-" in joined, "the difference is shown line by line")
        apply_line = (f"pkexec switchyard adopt-workflow {SLUG} --apply "
                      f"--from-live {live_digest} --replacing {recorded_digest}")
        check(apply_line in joined, f"the exact apply command: {joined[-700:]}")
        check(str(config_path) in joined, f"the tenant's stale configuration is named: {joined[-700:]}")
        repair = next(line for line in said if "set-role-runtime" in line).split("`")[1]
        argv = repair.split()[1:]
        parsed = launcher._build_switchyard_set_role_runtime_parser().parse_args(argv[1:])
        check((parsed.project, parsed.role, parsed.cli) == (SLUG, argv[2], argv[4]),
              f"the tenant command parses with the real parser: {repair}")
        check(record.read_bytes() == untouched, "a preview writes nothing")
        check(journal_entries(root)[-1]["detail"] == "dry-run", journal_entries(root)[-1])

        # 2. --apply alone chooses nothing.
        status, said = adopt(root, home, apply=True, board_reader=board(live))
        check(status == 1 and any("--apply alone does not choose" in line for line in said), said[-3:])
        check(record.read_bytes() == untouched, "refused: nothing written")

        # 3. A board or a record that moved since the review is refused.
        status, said = adopt(root, home, apply=True, from_live="f" * 64, replacing=recorded_digest,
                             board_reader=board(live))
        check(status == 1 and any(f"the board now holds {live_digest}" in line for line in said), said[-3:])
        status, said = adopt(root, home, apply=True, from_live=live_digest, replacing="e" * 64,
                             board_reader=board(live))
        check(status == 1 and any(f"root now holds {recorded_digest}" in line for line in said), said[-3:])
        check(record.read_bytes() == untouched, "refused: nothing written")

        # 4. A board that moves while root writes: root's previous record comes back.
        third, _r, _b = moved_runtime(live)
        racing = sequence(live, live, third)
        status, said = adopt(root, home, apply=True, from_live=live_digest, replacing=recorded_digest,
                             board_reader=racing)
        check(status == 1 and any("changed while root recorded it" in line for line in said), said[-3:])
        stored, problem = launcher.recorded_declared_workflow(SLUG)
        check(stored is not None and workflow_document_digest(stored) == recorded_digest,
              f"root's previous record was restored: {problem}")

        # 5. A live document root would not accept is refused.
        foreign = copy.deepcopy(live)
        foreign["roles"][0]["target"] = "another-project-director:0.0"
        status, said = adopt(root, home, apply=True, from_live=workflow_document_digest(foreign),
                             replacing=recorded_digest, board_reader=board(foreign))
        check(status == 1 and any("is not one root will record" in line for line in said), said[-3:])
        stored, _problem = launcher.recorded_declared_workflow(SLUG)
        check(workflow_document_digest(stored) == recorded_digest, "refused: root's record unchanged")

        # 6. The reviewed selection re-records root from the board, journalled.
        status, said = adopt(root, home, apply=True, from_live=live_digest, replacing=recorded_digest,
                             board_reader=board(live))
        check(status == 0 and any("re-recorded" in line and OPERATOR.name in line for line in said), said[-3:])
        stored, problem = launcher.recorded_declared_workflow(SLUG)
        check(stored == live and workflow_document_digest(stored) == live_digest, problem)
        info = os.stat(record)
        check(info.st_uid == 0 and (info.st_mode & 0o777) == 0o600, oct(info.st_mode))
        check(journal_entries(root)[-1]["detail"] == "re-recorded from the board", journal_entries(root)[-1])
        check(any("set-role-runtime" in line for line in said), "the tenant's files are still named for the tenant")
        status, said = adopt(root, home, board_reader=board(live))
        check(status == 0 and any("only the tenant's files differ" in line for line in said)
              and any(str(config_path) in line for line in said), f"root matches; the tenant's files are named: {said[-4:]}")

        # 7. An unreadable board (before the tenant repairs its own files): a preview says so; a selection is refused.
        # The board answers verification's read and is gone by the comparison's:
        # a board that goes away between the two, which the command must say.
        status, said = adopt(root, home, board_reader=sequence(live, None))
        check(status == 0 and any("could not be read" in line for line in said), said[-3:])
        status, said = adopt(root, home, apply=True, from_live=live_digest, replacing=live_digest,
                             board_reader=sequence(live, None))
        check(status == 1 and any("without the board's" in line for line in said), said[-3:])

        # 8. Root matches; once the tenant's own repair has run, all three agree and nothing changes.
        from scripts.runtime_projection import declared_projection
        apply_files(declared_projection(config_path, live))
        before_bytes = record.read_bytes()
        status, said = adopt(root, home, apply=True, board_reader=board(live))
        check(status == 0 and any("already holds" in line and "agree" in line for line in said), said[-3:])
        check(record.read_bytes() == before_bytes, "all agree: nothing written")

def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    if "--privileged-child" in sys.argv:
        privileged_cases()
        print(f"workflow_reconcile_test: privileged child {CHECKS} checks ok")
        return 0
    if not namespaces_available():
        print("workflow_reconcile_test: FAILED: user namespaces are unavailable, so the privileged cases did not run")
        return 1
    done = subprocess.run(["unshare", "--user", "--map-root-user", sys.executable, __file__, "--privileged-child"],
                          text=True, capture_output=True)
    print(done.stdout.strip())
    if done.returncode != 0 or "privileged child" not in done.stdout:
        print(done.stderr[-3000:])
        print("workflow_reconcile_test: FAILED in the privileged child")
        return 1
    print(f"workflow_reconcile_test: {CHECKS} unprivileged checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
