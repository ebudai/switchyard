#!/usr/bin/env python3
"""SYRD-327: making a project's desktop policy real, against the launcher it came out of.

Verifying a project's desktop policy at launch and installing and recording it
at `new` and `upgrade` moved into `scripts/project_desktop.py` unchanged. This
pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- Every name the launcher and the modules that read through it reach as
  `team_launcher.<name>` is still there and is the very same object,
  whichever module is imported first; every reference goes through the
  launcher (rule 21).
- **Seams (rule 24).** The suites patch both entry points on the launcher;
  the launcher calls them by its own names, and `configure_project_desktop`
  reaches `prepare_project_desktop` through the launcher, as rebound. The JSON
  reader and writer, the owner command and the current user are read there
  when a function runs; the desktop-access helpers stay each function's own
  import (rule 27).
- **The helper is still the one beside it.** The default verifier and
  installer is `scripts/desktop_access.py`, as it was from the launcher.
- **What it does is unchanged:** every role's desktop variables are replaced
  by the verified ones and unset otherwise; verification runs as the tenant
  through the runner given; installing happens only when asked; a failed
  configure undoes its install and puts the previous policy back.

`desktop_access` is patched to record what it is asked, the runner and the
JSON writer are recorders, and paths are temporary: no grant, desktop, sudo
or tenant is touched.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = ("DESKTOP_ENV_KEYS", "configure_project_desktop", "prepare_project_desktop")
READ_ELSEWHERE = {
    "first_run_auth": ("prepare_project_desktop", "DESKTOP_ENV_KEYS"),
    "presentation_controller": ("prepare_project_desktop",),
    "workflow_launcher": ("prepare_project_desktop",),
    "desktop_policy": ("configure_project_desktop",),
}
#: Measured on the baseline: the launcher's own call sites. SYRD-340 moved one
#: prepare_project_desktop site, in launch_project's P5 reload branch, to
#: launch_phases, which calls it through the launcher; the totals are unchanged.
LAUNCHER_CALLS = {"prepare_project_desktop": 6, "configure_project_desktop": 2}
LAUNCHER_READS = ("prepare_project_desktop", "configure_project_desktop", "_load_json", "_owner_command_args",
                  "_write_json_atomic", "current_user_name")
WAYLAND = {"mode": "wayland", "gui_user": "syrd327-desk", "tag": "new"}
PREVIOUS = {"mode": "wayland", "gui_user": "syrd327-desk", "tag": "previous"}


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


@dataclass(frozen=True)
class Role:
    role: str
    env: dict[str, str] = field(default_factory=dict)
    unset_env: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tenant:
    project: str
    run_as_user: str
    desktop_access: object
    roles: list[Role]


class Desktop:
    """What the launcher's desktop-access module is asked, and what it answers."""

    def __init__(self, fail_install: object = None) -> None:
        self.calls: list[tuple[str, object, object]] = []
        self.fail_install = fail_install

    def validate_policy(self, raw: object, *, project: str, tenant: str) -> dict[str, object]:
        self.calls.append(("validate", raw, tenant))
        return dict(raw) if isinstance(raw, dict) else {"mode": "headless"}

    def install(self, policy: dict[str, object], *, helper: Path) -> None:
        self.calls.append(("install", policy.get("tag"), helper))
        if self.fail_install is not None and policy.get("tag") == "new":
            raise self.fail_install

    def uninstall(self, policy: dict[str, object]) -> None:
        self.calls.append(("uninstall", policy.get("tag"), None))


def tenant(policy: object) -> Tenant:
    return Tenant("p327", "syrd327-owner", policy,
                  [Role("main", {"DISPLAY": ":0", "WAYLAND_DISPLAY": "old", "KEEP": "1"})])


def verifier(stdout: str = '{"WAYLAND_DISPLAY": "wayland-9", "XDG_RUNTIME_DIR": "/run/user/1327"}', code: int = 0):
    ran: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        ran.append(list(args))
        return subprocess.CompletedProcess(args, code, stdout, "syrd327 denied" if code else "")
    return run, ran


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.project_desktop; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.project_desktop'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.project_desktop", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.project_desktop")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.project_desktop as d; "
            f"print(all(getattr(t, n) is getattr(d, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_every_reader_reaches_its_names_through_the_launcher() -> None:
    for module, names in READ_ELSEWHERE.items():
        tree = ast.parse((ROOT / "scripts" / f"{module}.py").read_text(encoding="utf-8"))
        for name in names:
            check(name in EXPORTED, f"{name} is exported")
            uses = [n for n in ast.walk(tree) if (isinstance(n, ast.Attribute) and n.attr == name)
                    or (isinstance(n, ast.Name) and n.id == name)]
            through = [n for n in uses if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                       and n.value.id in ("launcher", "team_launcher")]
            check(uses and through == uses, f"{module} reads {name} through the launcher, and only there")


def test_the_seams_and_the_functions_own_imports() -> None:
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    # SYRD-350 moved the upgrade's U2 phase to upgrade_phases, which calls through
    # the launcher too; its sites count with launch_phases' and the totals are unchanged.
    # SYRD-426 moved new_project_command, one of configure_project_desktop's two callers, to
    # scripts/new_project_command.py, which calls through the launcher too; its site counts here and the totals are unchanged.
    moved_caller = ROOT / "scripts" / "new_project_command.py"
    moved_launch = ROOT / "scripts" / "project_launch.py"
    phases = ast.Module(body=[*ast.parse((ROOT / "scripts" / "launch_phases.py").read_text(encoding="utf-8")).body,
                              *ast.parse((ROOT / "scripts" / "upgrade_phases.py").read_text(encoding="utf-8")).body,
                              *(ast.parse(moved_caller.read_text(encoding="utf-8")).body if moved_caller.exists() else []),
                              # SYRD-429 moved launch_project, one of prepare_project_desktop's callers, to
                              # scripts/project_launch.py, which calls through the launcher; the totals are unchanged.
                              *(ast.parse(moved_launch.read_text(encoding="utf-8")).body if moved_launch.exists() else [])],
                        type_ignores=[])
    for name, count in LAUNCHER_CALLS.items():
        calls = [n for n in ast.walk(launcher_tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        phase_calls = [n for n in ast.walk(phases)
                       if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(calls) + len(phase_calls) == count and all(isinstance(n.func, ast.Name) for n in calls)
              and all(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                      and n.func.value.id == "launcher" for n in phase_calls),
              f"{name} is called at its {count} baseline sites: by the launcher's own patchable name there, "
              "through the launcher from launch_phases")
    moved = ast.parse((ROOT / "scripts" / "project_desktop.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in LAUNCHER_READS})
    check(bare == [], f"the seams and the launcher's helpers are read through it, never past it: {bare}")
    through = sorted({n.attr for n in ast.walk(moved) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr == "desktop_access"})
    check(through == [], "and the desktop-access helpers are each function's own import")


def test_prepare_verifies_as_the_tenant_and_replaces_the_desktop_environment() -> None:
    from scripts import desktop_access, project_desktop, team_launcher

    fake = Desktop()
    owner: list[str] = []
    run, ran = verifier()
    failing, _ = verifier(stdout="", code=1)
    with patched(desktop_access, validate_policy=fake.validate_policy, install=fake.install,
                 uninstall=fake.uninstall), \
            patched(team_launcher, _owner_command_args=lambda who, args: owner.append(who) or ["as", who, *args]):
        headless = project_desktop.prepare_project_desktop(tenant({"mode": "headless"}), runner=run)
        verified_for_headless = len(ran)
        wayland = project_desktop.prepare_project_desktop(tenant(WAYLAND), runner=run)
        installed = project_desktop.prepare_project_desktop(tenant(WAYLAND), install=True, runner=run)
        try:
            project_desktop.prepare_project_desktop(tenant(WAYLAND), runner=failing)
            refused = ""
        except SystemExit as exc:
            refused = str(exc)
    helper = ROOT / "scripts" / "desktop_access.py"
    keys = project_desktop.DESKTOP_ENV_KEYS
    check(keys == ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"),
          f"the desktop variables are unchanged: {keys}")
    check(headless.roles[0].env == {"KEEP": "1"} and headless.roles[0].unset_env == keys and verified_for_headless == 0,
          f"headless: the desktop variables are removed and unset, and nothing is verified: {headless.roles[0]}")
    check(wayland.roles[0].env == {"KEEP": "1", "WAYLAND_DISPLAY": "wayland-9", "XDG_RUNTIME_DIR": "/run/user/1327"}
          and wayland.desktop_access == WAYLAND,
          f"wayland: each role gets exactly the verified environment: {wayland.roles[0].env}")
    check(ran[0][:5] == ["as", "syrd327-owner", sys.executable, str(helper), "verify"] and owner[0] == "syrd327-owner",
          f"verified as the tenant, through the launcher's owner command and the runner given: {ran[0][:5]}")
    installs = [call for call in fake.calls if call[0] == "install"]
    check(installs == [("install", "new", helper)] and installed.roles[0].env == wayland.roles[0].env,
          f"installed only when asked, with the helper beside it: {installs}")
    check(refused == "switchyard: desktop setup incomplete before launch: syrd327 denied",
          f"a failed verification stops the launch and says why: {refused!r}")


def no_runner(args: list[str], **kwargs: object) -> object:
    raise AssertionError(f"configure ran {args!r}: the launcher's patched prepare was bypassed")


def test_configure_installs_prepares_records_and_puts_back_on_failure() -> None:
    from scripts import desktop_access, project_desktop, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd327.") as raw:
        config_path = Path(raw) / "p327.json"
        written: list[tuple[str, object, str]] = []
        prepared: list[object] = []

        def write(path: Path, payload: object, *, owner_user: str) -> None:
            written.append((path.name, payload, owner_user))

        def prepare(config: Tenant, **kwargs: object) -> Tenant:
            prepared.append(config.desktop_access)
            return config

        seams = dict(_load_json=lambda path: {"project": "p327"}, _write_json_atomic=write,
                     prepare_project_desktop=prepare)
        fake = Desktop()
        out = io.StringIO()
        with patched(desktop_access, validate_policy=fake.validate_policy, install=fake.install,
                     uninstall=fake.uninstall), patched(team_launcher, **seams), contextlib.redirect_stdout(out):
            dry = project_desktop.configure_project_desktop(tenant(PREVIOUS), config_path=config_path,
                                                            policy_path=Path("headless"), dry_run=True)
            dry_calls = [call[0] for call in fake.calls]
            configured = project_desktop.configure_project_desktop(tenant(PREVIOUS), config_path=config_path,
                                                                   policy_path=None, runner=no_runner)
        check(dry.desktop_access == PREVIOUS and dry_calls == ["validate"] and "no access changed" in out.getvalue(),
              f"a dry run validates, says what it would record, and changes nothing: {dry_calls}")
        check(prepared == [PREVIOUS] and written == [
            ("p327.json", {"project": "p327", "desktop_access": PREVIOUS}, "syrd327-owner"),
            ("desktop-policy.json", PREVIOUS, "syrd327-owner")] and configured.desktop_access == PREVIOUS,
              f"the unchanged policy is prepared through the launcher's seam and then recorded: {written}")

        def fails_to_prepare(config: Tenant, **kwargs: object) -> Tenant:
            raise SystemExit("syrd327 prepare failed")

        fake = Desktop()
        written.clear()
        with patched(desktop_access, validate_policy=fake.validate_policy, install=fake.install,
                     uninstall=fake.uninstall), \
                patched(team_launcher, **{**seams, "prepare_project_desktop": fails_to_prepare}):
            new_policy = Path(raw) / "new.json"
            new_policy.write_text(json.dumps(WAYLAND), encoding="utf-8")
            with patched(team_launcher, _load_json=lambda path: dict(WAYLAND) if path == new_policy else {}):
                try:
                    project_desktop.configure_project_desktop(tenant(PREVIOUS), config_path=config_path,
                                                              policy_path=new_policy, runner=no_runner)
                    failed = ""
                except SystemExit as exc:
                    failed = str(exc)
        order = [(call[0], call[1]) for call in fake.calls if call[0] != "validate"]
        check(failed == "syrd327 prepare failed" and written == [],
              f"a failed prepare is raised as it was and nothing is recorded: {failed!r}")
        check(order == [("uninstall", "previous"), ("install", "new"), ("uninstall", "new"), ("install", "previous")],
              f"the replaced policy comes off, the new one goes on, and on failure the old one is put back: {order}")

        fake = Desktop(fail_install=desktop_access.DesktopAccessError("syrd327 cannot reach the session"))
        with patched(desktop_access, validate_policy=fake.validate_policy, install=fake.install,
                     uninstall=fake.uninstall), patched(team_launcher, **seams):
            try:
                project_desktop.configure_project_desktop(tenant(None), config_path=config_path,
                                                          policy_path=None, runner=no_runner)
                untouched = "no install was attempted"
            except SystemExit as exc:
                untouched = str(exc)
            with patched(team_launcher, _load_json=lambda path: dict(WAYLAND)):
                try:
                    project_desktop.configure_project_desktop(tenant(None), config_path=config_path,
                                                              policy_path=Path(raw) / "new.json", runner=no_runner)
                    explained = ""
                except SystemExit as exc:
                    explained = str(exc)
    check(untouched == "no install was attempted", "a headless tenant with nothing new installs nothing")
    check(explained.startswith("switchyard: p327's desktop policy could not be installed: syrd327 cannot reach the "
                               "session. Nothing was recorded"),
          f"an install that cannot reach the desktop is explained, not dumped: {explained!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"project_desktop_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
