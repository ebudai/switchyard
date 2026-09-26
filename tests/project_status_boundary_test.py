#!/usr/bin/env python3
"""SYRD-301: the status verbs' boundary with the launcher they came out of.

`switchyard status` and `switchyard release-status` moved into
`scripts/project_status.py` unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top; the
  launcher imports it at its top.
- Every name the launcher's dispatch and the suites reach as
  `team_launcher.<name>` is still there and is the very same object, whichever
  module is imported first.
- **The patched verb is still the one dispatched.** A suite replaces
  `team_launcher.switchyard_status_command`; `switchyard status` must reach
  that replacement, not the moved definition behind it.
- **The launcher facilities are read when a status runs,** so a patch on the
  launcher is what the status reports.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    'SwitchyardProjectStatus',
    '_build_switchyard_release_status_parser',
    '_build_switchyard_status_parser',
    'close_release_phase',
    'format_release_alignment',
    'root_recorded_tenant_facts',
    '_runtime_release_copy_status',
    'switchyard_project_statuses',
    'switchyard_release_status_command',
    'switchyard_runtime_copy_statuses',
    'switchyard_status_command',
)


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.project_status; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.project_status'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]", f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.project_status", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.project_status")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.project_status as s; "
            f"print(all(getattr(t, n) is getattr(s, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_switchyard_status_dispatches_to_the_patched_verb() -> None:
    from scripts import project_status, team_launcher

    calls: list[dict[str, object]] = []

    def live_listing(**kwargs: object) -> None:
        # A dispatch that went past the patch would read this host's tenants;
        # the real verb's first step is this listing, so stop it there.
        raise AssertionError("switchyard status ran the moved verb, not the launcher's patched one")

    saved = (team_launcher.switchyard_status_command, project_status.switchyard_project_statuses)
    team_launcher.switchyard_status_command = lambda **kwargs: calls.append(kwargs) or 37
    project_status.switchyard_project_statuses = live_listing
    try:
        code = team_launcher.switchyard_main(["status", "--json"])
    finally:
        team_launcher.switchyard_status_command, project_status.switchyard_project_statuses = saved
    check(code == 37 and calls == [{"json_output": True}],
          f"`switchyard status --json` reached the launcher's patched verb: {code!r} {calls!r}")


def test_a_status_reads_the_launcher_when_it_runs() -> None:
    from scripts import project_status, team_launcher

    entry = SimpleNamespace(name="syrd-301", slug="syrd-301-no-such-project",
                            config_path=Path("/nonexistent/syrd-301.json"))
    saved = (team_launcher._switchyard_entries, team_launcher.load_project_config)

    def refuse(slug: str, path: Path) -> None:
        raise OSError(f"syrd-301 refused {slug}")

    team_launcher._switchyard_entries = lambda **kwargs: [entry]
    team_launcher.load_project_config = refuse
    try:
        statuses = project_status.switchyard_project_statuses()
    finally:
        team_launcher._switchyard_entries, team_launcher.load_project_config = saved
    check([(s.slug, s.state, s.error) for s in statuses]
          == [("syrd-301-no-such-project", "unknown", "syrd-301 refused syrd-301-no-such-project")],
          f"the listing and the config load were the launcher's patched ones: {statuses!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"project_status_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
