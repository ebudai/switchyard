#!/usr/bin/env python3
"""SYRD-310: session records and seeding's boundary with the launcher they came out of.

A role's recorded session -- which one to resume, whether its hooks reported,
the launch report, and seeding a new session store -- moved into
`scripts/session_records.py` unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top; the
  launcher imports it at its top.
- Every name the launcher's launch, session start and resume preflight, the
  five modules that read through the launcher, and the suites reach as
  `team_launcher.<name>` is still there and is the very same object,
  whichever module is imported first.
- **Def-time identities hold.** The reporters' timeout and poll defaults are
  the very objects `launch_project` and `switchyard new` default to.
- **The launcher's seams are still reached.** The suites patch
  `report_launch_session_records` on the launcher, and the launch still calls
  it there, at both baseline sites. They patch `DEFAULT_SESSION_DIR` there,
  and seeding reads it there when it runs, and writes through the launcher's
  private writers.

Everything here runs on temporary directories; nothing reads or writes a real
session store.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    'clear_session_record_for_role',
    'LAUNCH_SESSION_RECORD_POLL_SECONDS',
    'launch_session_record_statuses',
    'LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS',
    'LaunchSessionRecordStatus',
    'pane_launch_outcome_source_for_role',
    'pane_runtime_hook_source_for_role',
    'pane_state_file_name',
    'report_launch_session_records',
    'seed_default_session_dir_from_legacy_sources',
    'seed_session_dir_from_legacy_sources',
    'session_file_name',
    'session_id_for_role',
    '_session_payload_model_for_role',
    '_session_record_for_role',
    'superseded_session_id_for_role',
)

#: What other Switchyard modules read through the launcher.
READ_ELSEWHERE = {
    "scripts/role_credentials.py": ("session_file_name",),
    "scripts/role_identity_cutover.py": ("session_file_name", "session_id_for_role", "_session_record_for_role"),
    "scripts/presentation_controller.py": ("session_id_for_role",),
    "scripts/role_command.py": ("session_id_for_role",),
    "scripts/role_runtime.py": ("clear_session_record_for_role",),
}


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.session_records; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.session_records'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.session_records", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.session_records")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.session_records as s; "
            f"print(all(getattr(t, n) is getattr(s, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_what_other_modules_read_is_exported() -> None:
    for path, names in READ_ELSEWHERE.items():
        text = (ROOT / path).read_text(encoding="utf-8")
        for name in names:
            check(name in text and name in EXPORTED, f"{path} reads {name}, and the launcher exports it")


def test_the_timeout_and_poll_defaults_are_one_object() -> None:
    from scripts import session_records, team_launcher

    for reporter in (session_records.launch_session_record_statuses, session_records.report_launch_session_records):
        params = inspect.signature(reporter).parameters
        check(params["timeout_seconds"].default is team_launcher.LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS
              and params["poll_seconds"].default is team_launcher.LAUNCH_SESSION_RECORD_POLL_SECONDS,
              f"{reporter.__name__} defaults to the launcher's own timeout and poll")
    for stayed in (team_launcher.launch_project, team_launcher.switchyard_new_command):
        params = inspect.signature(stayed).parameters
        check(params["session_record_timeout"].default is session_records.LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS
              and params["session_record_poll"].default is session_records.LAUNCH_SESSION_RECORD_POLL_SECONDS,
              f"and {stayed.__name__} to the same objects")


def test_the_patched_reporter_is_still_called_by_the_launchers_name() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "report_launch_session_records"]
    check(len(calls) == 2 and all(isinstance(n.func, ast.Name) for n in calls),
          "the launch and `switchyard new` call it at their two baseline sites, by the launcher's patchable name")
    moved = ast.parse((ROOT / "scripts" / "session_records.py").read_text(encoding="utf-8"))
    bare = [n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            and n.id in ("DEFAULT_SESSION_DIR", "role_session_dir")]
    check(bare == [], f"the moved code reads the launcher's session directory only through it: {bare}")


def test_seeding_reads_and_writes_through_the_launcher_when_it_runs() -> None:
    from scripts import session_records, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd310.") as raw:
        root = Path(raw)
        store, elsewhere, legacy = root / "sessions", root / "elsewhere", root / "legacy"
        legacy.mkdir()
        (legacy / "p310-main.json").write_text(json.dumps({"target": "p310-main", "session_id": "syrd-310"}),
                                               encoding="utf-8")
        written: list[Path] = []
        ensured: list[Path] = []
        saved = (team_launcher.DEFAULT_SESSION_DIR, team_launcher._write_private_json_atomic,
                 team_launcher._ensure_private_dir, session_records.DEFAULT_LEGACY_RUNTIME_SESSION_DIR,
                 session_records.DEFAULT_INTERIM_SESSION_BACKUP_DIR)
        team_launcher.DEFAULT_SESSION_DIR = store
        team_launcher._write_private_json_atomic = lambda path, payload: written.append(path)
        team_launcher._ensure_private_dir = lambda path: ensured.append(path)
        session_records.DEFAULT_LEGACY_RUNTIME_SESSION_DIR = legacy
        session_records.DEFAULT_INTERIM_SESSION_BACKUP_DIR = root / "no-interim"
        try:
            not_default = session_records.seed_default_session_dir_from_legacy_sources(elsewhere)
            seeded = session_records.seed_default_session_dir_from_legacy_sources(store)
        finally:
            (team_launcher.DEFAULT_SESSION_DIR, team_launcher._write_private_json_atomic,
             team_launcher._ensure_private_dir, session_records.DEFAULT_LEGACY_RUNTIME_SESSION_DIR,
             session_records.DEFAULT_INTERIM_SESSION_BACKUP_DIR) = saved
        target = store / session_records.session_file_name("p310-main")
        check(not_default == [], "a store that is not the launcher's default is not seeded")
        check(seeded == [target] and written == [target],
              f"the default store is seeded through the launcher's private writer: {seeded!r} {written!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"session_records_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
