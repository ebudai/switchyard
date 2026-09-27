#!/usr/bin/env python3
"""SYRD-328: legacy presentation migration and the presentation config, against the launcher they came out of.

Giving a legacy tenant the desktop-account presentation, and the
presentation-config wrappers it rests on, moved into
`scripts/legacy_presentation.py` unchanged. `_gui_home` did not: it is shared
account lookup, and the moved code reads it through the launcher. This pins
what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- Every name the launcher and `pane_rebind` reach as `team_launcher.<name>` is
  still there and is the very same object, whichever module is imported
  first; every reader goes through the launcher (rule 21).
- **Seams (rule 24).** The suites rebind `presentation_section_for_roles` on
  the launcher; the launcher calls every entry point by its own name; the
  accounts, homes, desktop account, layout paths, JSON reader and writer and
  config loader are read through the launcher when a function runs; the
  presentation controller stays each function's own import (rule 27).
- **The decision is unchanged:** nothing moves when no desktop is granted,
  the desktop is the owner's, it already presents, or nothing is visible;
  too many slots, an unsafe destination or a state root that is a symlink or
  another account's is refused; a dry run writes nothing; a config naming
  another project is not written.

The desktop state root is a temporary home this account owns, the account
lookups are patched, and the config writer and loader record; no tenant,
window, grant or other account is touched.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = ('LegacyPresentationMigration', 'presentation_controller_enabled', 'presentation_section_for_roles',
            'legacy_presentation_section', 'legacy_presentation_migration', '_desktop_state_root_problem',
            'migrate_legacy_presentation', 'legacy_presentation_refusal')
READ_ELSEWHERE = {"pane_rebind": ("presentation_section_for_roles",)}
#: Measured on the baseline: the launcher's own call sites. SYRD-342 moved
#: launch_project's P7+P8, and with it the refusal's one site, to launch_phases,
#: which calls it through the launcher; the total is unchanged.
LAUNCHER_CALLS = {"legacy_presentation_refusal": 1, "migrate_legacy_presentation": 1,
                  "legacy_presentation_migration": 1, "presentation_controller_enabled": 1}
LAUNCHER_READS = ("_gui_home", "uid_for_user", "current_user_name", "_load_json", "_write_json_atomic",
                  "load_project_config", "pinned_presentation_gui_user", "presentation_gui_user",
                  "desktop_presentation_layout_path", "desktop_layout_destination_problem", "desktop_state_dir",
                  "MAX_VISIBLE_PANES_PER_WINDOW", "presentation_section_for_roles")
OWNER, DESK, PROJECT = "syrd328-owner", "syrd328-desk", "p328"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


class patched:
    """Rebind attributes of one module for one block, as the suites do."""

    def __init__(self, module: object, **values: object) -> None:
        self.module = module
        self.values = values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.module, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.module, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.module, name, value)


def tenant(*slots: int | None, detached: tuple[int, ...] = ()) -> SimpleNamespace:
    roles = [SimpleNamespace(role=f"r{index}", slot=slot, detached=index in detached)
             for index, slot in enumerate(slots)]
    return SimpleNamespace(project=PROJECT, run_as_user=OWNER, roles=roles, repository=None)


HOMES_ASKED: list[str] = []


def accounts(home: Path, uid: int | None = None) -> dict[str, object]:
    """The launcher's account and desktop lookups, answering for this account's own temporary home."""
    return dict(_gui_home=lambda user: HOMES_ASKED.append(user) or str(home),
                uid_for_user=lambda user: os.getuid() if uid is None else uid,
                pinned_presentation_gui_user=lambda config: DESK, presentation_gui_user=lambda config: DESK,
                current_user_name=lambda: OWNER)


def test_a_home_is_asked_of_the_launcher() -> None:
    # Named to run first: the walk's home has to come from the launcher before
    # anything else here relies on where it points.
    from scripts import legacy_presentation, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd328.") as raw:
        home = Path(raw) / "desk-home"
        home.mkdir()
        HOMES_ASKED.clear()
        with patched(team_launcher, **accounts(home)):
            try:
                answer = legacy_presentation._desktop_state_root_problem(DESK, PROJECT)
            except Exception as exc:  # noqa: BLE001 - the answer it failed to give is the finding
                answer = f"the state-root walk raised {exc!r} instead of answering"
    check(HOMES_ASKED and set(HOMES_ASKED) == {DESK} and answer == "",
          f"the desktop account's home is the launcher's _gui_home, as patched: {HOMES_ASKED} {answer!r}")


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.legacy_presentation; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.legacy_presentation'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.legacy_presentation", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.legacy_presentation")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.legacy_presentation as d; "
            f"print(all(getattr(t, n) is getattr(d, n) for n in {EXPORTED!r}), t._gui_home.__module__)"
        )
        check(result.stdout.strip() == "True scripts.team_launcher",
              f"{' then '.join(order)}: every moved name is the launcher's too, and _gui_home stayed: "
              f"{result.stdout}{result.stderr[-600:]}")


def test_the_readers_the_seams_and_the_functions_own_imports() -> None:
    for module, names in READ_ELSEWHERE.items():
        tree = ast.parse((ROOT / "scripts" / f"{module}.py").read_text(encoding="utf-8"))
        for name in names:
            uses = [n for n in ast.walk(tree) if (isinstance(n, ast.Attribute) and n.attr == name)
                    or (isinstance(n, ast.Name) and n.id == name)]
            through = [n for n in uses if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                       and n.value.id in ("launcher", "team_launcher")]
            check(uses and through == uses, f"{module} reads {name} through the launcher, and only there")
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    # SYRD-350 moved the upgrade's U2 phase to upgrade_phases, which calls through
    # the launcher too; its sites count with launch_phases' and the totals are unchanged.
    phases = ast.Module(body=[*ast.parse((ROOT / "scripts" / "launch_phases.py").read_text(encoding="utf-8")).body,
                              *ast.parse((ROOT / "scripts" / "upgrade_phases.py").read_text(encoding="utf-8")).body],
                        type_ignores=[])
    for name, count in LAUNCHER_CALLS.items():
        calls = [n for n in ast.walk(launcher_tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        phase_calls = [n for n in ast.walk(phases)
                       if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(calls) + len(phase_calls) == count and all(isinstance(n.func, ast.Name) for n in calls)
              and all(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                      and n.func.value.id == "launcher" for n in phase_calls),
              f"the launcher calls {name} at its {count} baseline site, by its own name there, "
              "through the launcher from launch_phases")
    moved = ast.parse((ROOT / "scripts" / "legacy_presentation.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in LAUNCHER_READS})
    check(bare == [], f"the launcher's names are read through it, never past it: {bare}")
    through = sorted({n.attr for n in ast.walk(moved) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher"
                      and n.attr == "presentation_controller"})
    check(through == [], "and the presentation controller is each function's own import")


def test_the_section_rules_agree() -> None:
    from scripts import legacy_presentation, presentation_controller

    config = tenant(0, 2, None, 3, detached=(3,))
    raw = [{"role": role.role, "slot": role.slot, "detached": role.detached} for role in config.roles]
    section = legacy_presentation.legacy_presentation_section(config)
    check(section == {"slot_count": 3, "layouts": {"default": {"0": "r0", "2": "r1"}}},
          f"the configured slots, gap kept, detached and slotless left out: {section}")
    check(legacy_presentation.presentation_section_for_roles(raw) == section,
          "and the raw-entry rule gives the same section")
    asked: list[object] = []
    with patched(presentation_controller,
                 presentation_enabled=lambda config, *, config_path: asked.append(config_path) or True):
        enabled = legacy_presentation.presentation_controller_enabled(config, config_path=Path("/nonexistent/p328"))
    check(enabled is True and asked == [Path("/nonexistent/p328")], "the wrapper asks the presentation controller")


def test_whether_and_where_a_legacy_presentation_moves() -> None:
    from scripts import legacy_presentation, presentation_controller, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd328.") as raw:
        home = Path(raw) / "desk-home"
        home.mkdir()
        config_path = Path(raw) / "p328.json"
        decide = lambda config: legacy_presentation.legacy_presentation_migration(config, config_path=config_path)
        with patched(team_launcher, **accounts(home)), \
                patched(presentation_controller, presentation_enabled=lambda config, *, config_path: False):
            with patched(team_launcher, pinned_presentation_gui_user=lambda config: ""):
                no_desktop = decide(tenant(0))
            with patched(team_launcher, pinned_presentation_gui_user=lambda config: OWNER):
                owners = decide(tenant(0))
            with patched(presentation_controller, presentation_enabled=lambda config, *, config_path: True):
                presenting = decide(tenant(0))
            invisible = decide(tenant(None))
            crowded = decide(tenant(0, 6))
            moved = decide(tenant(0, 1))
            (home / ".local").symlink_to(Path(raw))
            HOMES_ASKED.clear()
            try:
                linked = legacy_presentation._desktop_state_root_problem(DESK, PROJECT)
            except Exception as exc:  # noqa: BLE001 - reported below as the answer it failed to give
                linked = f"the state-root walk raised {exc!r} instead of answering"
            homes_asked = list(HOMES_ASKED)
            (home / ".local").unlink()
            with patched(team_launcher, uid_for_user=lambda user: os.getuid() + 1):
                foreign = legacy_presentation._desktop_state_root_problem(DESK, PROJECT)
            with patched(team_launcher, uid_for_user=lambda user: None):
                unknown = legacy_presentation._desktop_state_root_problem(DESK, PROJECT)
        expected = home / ".local" / "state" / "switchyard" / "projects" / PROJECT / f"{PROJECT}-presentation-layout.json"
    check(not no_desktop.needed and no_desktop.reason.startswith("no Wayland desktop is granted"),
          f"no desktop granted: nothing to move: {no_desktop}")
    check(not owners.needed and "is the tenant owner" in owners.reason, f"the owner's own desktop: {owners}")
    check(not presenting.needed and "already presents" in presenting.reason, f"already presenting: {presenting}")
    check(not invisible.needed and invisible.reason == "it has no visible role to present",
          f"nothing visible: {invisible}")
    check(crowded.needed and "a presentation window has 6" in crowded.refusal and crowded.destination is None,
          f"more slots than one window holds is refused: {crowded}")
    check(moved.needed and moved.refusal == "" and moved.destination == expected and moved.gui_user == DESK
          and moved.section == {"slot_count": 2, "layouts": {"default": {"0": "r0", "1": "r1"}}},
          f"otherwise it moves to the desktop account's own state directory: {moved}")
    check("2 slot(s): 0=r0, 1=r1" in moved.describe(), f"and says so: {moved.describe()}")
    check(homes_asked and set(homes_asked) == {DESK},
          f"the desktop account's home is asked of the launcher: {homes_asked} ({linked})")
    check(linked == f"{home / '.local'} is a symlink or not a directory",
          f"a symlink in the desktop account's state root is refused: {linked!r}")
    check(foreign.startswith(f"{home} is owned by uid {os.getuid()}, not by {DESK}"),
          f"so is a home another account owns: {foreign!r}")
    check(unknown == f"{DESK} is not a local account", f"and an account the host does not have: {unknown!r}")


def test_migrating_refuses_dry_runs_or_writes_and_reloads() -> None:
    from scripts import legacy_presentation, presentation_controller, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd328.") as raw:
        home = Path(raw) / "desk-home"
        home.mkdir()
        config_path = Path(raw) / "p328.json"
        written: list[tuple[dict[str, object], str]] = []
        reloaded = SimpleNamespace(reloaded=True)
        said: list[str] = []
        def no_reading(path: Path) -> dict[str, object]:
            raise AssertionError("the migration went on to read the tenant's config, as if to write it")

        seams = dict(_write_json_atomic=lambda path, payload, *, owner_user: written.append((dict(payload), owner_user)),
                     load_project_config=lambda project, path: reloaded, _load_json=no_reading)
        migrate = lambda config, **kw: legacy_presentation.migrate_legacy_presentation(
            config, config_path=config_path, print_func=said.append, **kw)
        with patched(team_launcher, **accounts(home), **seams), \
                patched(presentation_controller, presentation_enabled=lambda config, *, config_path: False):
            refused = migrate(tenant(0, 6))
            with patched(team_launcher, pinned_presentation_gui_user=lambda config: ""):
                unneeded = migrate(tenant(0))
            said_after_unneeded = len(said)
            dry = migrate(tenant(0, 1), dry_run=True)
            with patched(team_launcher, _load_json=lambda path: {"project": "someone-else"}):
                mismatch = migrate(tenant(0, 1))
            with patched(team_launcher, _load_json=lambda path: {"project": PROJECT}):
                done = migrate(tenant(0, 1))
    check(refused[1] is False and "this upgrade stops before declaring it ready. Nothing was changed." in said[0],
          f"a refused migration stops the upgrade and changes nothing: {said[0]!r}")
    check(unneeded[1] is True and said_after_unneeded == 1, "nothing needed: the upgrade goes on, silently")
    check(dry[1] is True and said[1].startswith("switchyard: would give p328 a presentation section")
          and said[1].endswith("; nothing written"), f"a dry run says what it would give: {said[1]!r}")
    check(mismatch[1] is False and "refusing to add a presentation section to it." in said[2],
          f"a config naming another project is not written: {said[2]!r}")
    check(done == (reloaded, True) and written == [(
        {"project": PROJECT, "presentation": {"slot_count": 2, "layouts": {"default": {"0": "r0", "1": "r1"}}}},
        OWNER)], f"the section is written as the owner, through the launcher's writer, then reloaded: {written}")


def test_the_fallback_window_refusal() -> None:
    from scripts import legacy_presentation, team_launcher

    output = Path("/nonexistent/syrd328/owner-state/p328-team-layout.json")
    with patched(team_launcher, presentation_gui_user=lambda config: DESK, current_user_name=lambda: OWNER):
        refused = legacy_presentation.legacy_presentation_refusal(tenant(0), output_path=output)
    with patched(team_launcher, presentation_gui_user=lambda config: OWNER):
        same = legacy_presentation.legacy_presentation_refusal(tenant(0), output_path=output)
    check(f"the layout it would be handed, {output}, is in {OWNER}'s private state" in refused
          and "Run `sudo switchyard upgrade p328` to move it there" in refused,
          f"a desktop account that cannot read the owner's layout is told why and what fixes it: {refused[:120]!r}")
    check(same == "", "the owner's own desktop needs no refusal")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"legacy_presentation_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
