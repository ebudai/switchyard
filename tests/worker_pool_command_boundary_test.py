#!/usr/bin/env python3
"""SYRD-286: the worker-pool module's boundary with the launcher it came out of.

The worker-pool declaration and verb moved out of `scripts/team_launcher.py`
into `scripts/worker_pool_command.py`, unchanged. Two things make that safe, and
they are what this pins:

- The module never imports the launcher at its top. The launcher imports it,
  so a top-level import back would be a cycle that half-initialises one of
  the two, depending on which a caller happened to import first.
- Every name that callers and tests reached as `team_launcher.<name>` is still
  there and is the very same object. And the launcher facilities the module
  uses are looked up on `team_launcher` when it runs, so a patch on the
  launcher -- the suites patch `current_user_name` and `_owner_home_for_auth`
  -- still reaches it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

#: Every name the launcher defined for the pool before the move.
MOVED_PUBLIC = (
    "WORKER_POOL_ACTIONS",
    "WORKER_POOL_MAX_SIZE",
    "WORKER_POOL_MEMBER_SEPARATOR",
    "WORKER_POOL_ONBOARDING_PROMPT_MAX_CHARS",
    "WorkerPool",
    "WorkerPoolFinding",
    "_build_switchyard_worker_pool_parser",
    "format_worker_pool_preflight",
    "parse_worker_pool",
    "switchyard_worker_pool_command",
    "worker_pool_member_role",
    "worker_pool_preflight",
)


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def test_the_module_imports_without_the_launcher() -> None:
    probe = (
        "import sys; import scripts.worker_pool_command as w; "
        "print('scripts.team_launcher' in sys.modules, w.parse_worker_pool.__module__)"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"}, check=False)
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.split() == ["False", "scripts.worker_pool_command"],
          f"and without pulling the launcher in: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for first, second in (("scripts.worker_pool_command", "scripts.team_launcher"),
                          ("scripts.team_launcher", "scripts.worker_pool_command")):
        probe = (
            f"import importlib; a = importlib.import_module({first!r}); b = importlib.import_module({second!r}); "
            "import scripts.team_launcher as t, scripts.worker_pool_command as w; "
            f"print(all(getattr(t, n) is getattr(w, n) for n in {MOVED_PUBLIC!r}))"
        )
        result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                                env={"PATH": "/usr/bin:/bin"}, check=False)
        check(result.stdout.strip() == "True",
              f"{first} then {second}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_a_patch_on_the_launcher_reaches_the_moved_preflight() -> None:
    import json

    from scripts import team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd286-seam.") as raw:
        tmp = Path(raw)
        path = tmp / "porter.json"
        path.write_text(json.dumps({
            "project": "porter", "board_url": "http://127.0.0.1:1/",
            "roles": [{"role": "director", "cli": ["claude"], "slot": 0, "workdir": str(tmp / "d")}],
            "worker_pool": {"name": "worker", "runtime": "claude", "size": 1},
        }), encoding="utf-8")
        config = team_launcher.load_project_config("porter", path)
        asked: list[str] = []
        saved = (team_launcher.current_user_name, team_launcher._owner_home_for_auth)
        team_launcher.current_user_name = lambda: asked.append("current_user_name") or "seam-owner"
        team_launcher._owner_home_for_auth = lambda user: asked.append(f"home:{user}") or tmp / "home"
        try:
            findings = team_launcher.worker_pool_preflight(
                config, runner=lambda args, **k: subprocess.CompletedProcess(args, 0, "", ""))
        finally:
            team_launcher.current_user_name, team_launcher._owner_home_for_auth = saved
        check(bool(findings), f"the preflight ran to its findings: {findings}")
        check("current_user_name" in asked and "home:seam-owner" in asked,
              f"the moved preflight asked the launcher's patched facilities: {asked}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"worker_pool_command_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
