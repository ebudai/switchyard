#!/usr/bin/env python3
"""SYRD-288: the upstream-report module's boundary with the launcher it came out of.

The upstream report link and its credential moved into
`scripts/upstream_report.py` unchanged, and `home_dir_for_user` moved into
the leaf `scripts/host_accounts.py`. This pins what makes that safe:

- Neither module imports the launcher at its top: the launcher imports them.
  `host_accounts` imports nothing of Switchyard's at all.
- Every name callers reached as `team_launcher.<name>` is still there and is
  the very same object, whichever module is imported first. That includes
  `home_dir_for_user`, which is the default of every `home_for_user` parameter
  in the moved code and so is bound when those functions are defined.
- Launcher facilities the moved code uses are looked up on `team_launcher`
  when it runs, so a patch there reaches it.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = {
    "scripts.upstream_report": (
        "UPSTREAM_REPORT_CREDENTIAL_NAME", "UPSTREAM_REPORT_TOKEN_KEY", "_board_env_report_token",
        "_write_owner_private_file", "record_upstream_report_link", "refresh_upstream_report_credential",
        "upstream_report_board", "upstream_report_credential_path",
    ),
    "scripts.host_accounts": ("home_dir_for_user",),
}
DEFAULTED = ("upstream_report_credential_path", "upstream_report_board", "record_upstream_report_link",
             "refresh_upstream_report_credential", "_write_owner_private_file")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_modules_import_without_the_launcher() -> None:
    result = python(
        "import sys; import scripts.host_accounts; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.host_accounts')); "
        "import scripts.upstream_report; print('scripts.team_launcher' in sys.modules)"
    )
    check(result.returncode == 0, f"both import on their own: {result.stderr[-600:]}")
    check(result.stdout.split("\n")[:2] == ["[]", "False"],
          f"host_accounts is a leaf, and neither pulls the launcher in: {result.stdout!r}")


def test_every_import_order_gives_one_set_of_objects_and_one_default() -> None:
    for order in (("scripts.upstream_report", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.upstream_report"),
                  ("scripts.host_accounts", "scripts.upstream_report", "scripts.team_launcher")):
        result = python(
            "import importlib, inspect; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.upstream_report as u; "
            f"table = {EXPORTED!r}; "
            "same = all(getattr(t, n) is getattr(importlib.import_module(m), n) for m, ns in table.items() for n in ns); "
            f"defaults = all(inspect.signature(getattr(u, n)).parameters['home_for_user'].default is t.home_dir_for_user for n in {DEFAULTED!r}); "
            "print(same, defaults)"
        )
        check(result.stdout.split() == ["True", "True"],
              f"{' then '.join(order)}: one set of objects, one default: {result.stdout}{result.stderr[-600:]}")


def test_launcher_patches_reach_the_moved_code() -> None:
    from scripts import team_launcher, upstream_report

    asked: list[str] = []
    entry = type("Entry", (), {"slug": "upstream", "config_path": Path("/nonexistent/upstream.json")})()
    saved = (team_launcher._registry_project_entries, team_launcher._load_json, team_launcher.uid_for_user)
    team_launcher._registry_project_entries = lambda registry_dir: asked.append("registry") or [entry]
    team_launcher._load_json = lambda path: asked.append(f"load:{path.name}") or {
        "board_url": "http://127.0.0.1:9/", "run_as_user": "upstream-agent"}
    team_launcher.uid_for_user = lambda user: asked.append(f"uid:{user}") or os.getuid()
    try:
        slug, env_file, problem = upstream_report.upstream_report_board(
            "http://127.0.0.1:9", home_for_user=lambda user: Path("/homes") / user)
        with tempfile.TemporaryDirectory(prefix="syrd288-seam.") as raw:
            token = Path(raw) / "token"
            token.write_text("x", encoding="utf-8")
            token.chmod(0o600)
            private = upstream_report._credential_is_private(token, "someone")
    finally:
        team_launcher._registry_project_entries, team_launcher._load_json, team_launcher.uid_for_user = saved
    check((slug, env_file, problem) == ("upstream", Path("/homes/upstream-agent/.config/upstream/ticket-board.env"), ""),
          f"the board was found through the launcher's patched registry: {(slug, env_file, problem)}")
    check(asked[:2] == ["registry", "load:upstream.json"], f"and read through them: {asked}")
    check("uid:someone" in asked and private is True,
          f"the privacy check asked the launcher's patched uid lookup: {asked} {private}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"upstream_report_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
