#!/usr/bin/env python3
"""SYRD-326: deciding a project's desktop policy, against the launcher it came out of.

The desktop policy decision -- at `switchyard new`, at recovery and at
upgrade -- moved into `scripts/desktop_policy.py` unchanged. This pins what
makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- Every name the launcher and the suites reach as `team_launcher.<name>` is
  still there and is the very same object, whichever module is imported first.
- **The launcher's names are read when a function runs:** the host approval
  records, the desktop installer, the policy loader, the prompts and the
  current user, several of them patched by the suites; the launcher calls each
  entry point by its own name. The desktop-access helpers stay each function's
  own imports (rule 27).
- **The decision is unchanged:** a policy file, then `--headless`, then the
  recorded approval, then asking; `--yes` never grants; an ambiguous host is
  never guessed; recovery takes the desktop from the host's record, never from
  the tenant; an upgrade never assumes a policy.

The approval records, the installer, the owner lookups and the answers are
fakes or files in this test's own temporary directory; no desktop, logind,
grant or /etc file is touched.
"""

from __future__ import annotations

import ast
import json
import os
import pwd
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    'DESKTOP_FROM_CHOSEN_HEADLESS', 'DESKTOP_FROM_HEADLESS_OPTION', 'DESKTOP_FROM_HOST_APPROVAL',
    'DESKTOP_FROM_NEW_APPROVAL', 'DESKTOP_FROM_POLICY_FILE', 'approved_desktop_policy', '_desktop_host_is_headless',
    'install_recovered_desktop_access', '_prompt_choice', '_resolve_desktop_policy',
    'upgrade_desktop_policy_decision',
)
#: Measured on the baseline: the launcher's call sites that stayed.
LAUNCHER_CALLS = {"_resolve_desktop_policy": 1, "install_recovered_desktop_access": 1,
                  "upgrade_desktop_policy_decision": 1}
LAUNCHER_READS = ("read_host_desktop_approval", "write_host_desktop_approval", "configure_project_desktop",
                  "current_user_name", "_load_json", "_prompt_bool", "_read_prompt")
OWN_IMPORTS = ("DesktopAccessError", "active_wayland_owners", "generated_policy", "resolve_gui_owner", "desktop")
ME = pwd.getpwuid(os.getuid()).pw_name
PROJECT, TENANT = "p326", "p326-agent"


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
class Tenant:
    """The fields of a project configuration the recovery reads and replaces."""

    project: str
    run_as_user: str
    desktop_access: Any = None


def answers(*replies: str):
    queue = list(replies)

    def reply(prompt: str) -> str:
        if not queue:
            raise AssertionError(f"the decision asked a question it had no business asking: {prompt!r}")
        return queue.pop(0)
    return reply


def resolve(**kwargs: object) -> tuple[object, str, list[str]]:
    from scripts import desktop_policy

    said: list[str] = []
    options = dict(desktop_policy=None, headless=False, gui_user="", project=PROJECT, tenant=TENANT, yes=False,
                   input_func=answers(), print_func=said.append, owner_resolver=lambda preferred: ME,
                   owners_lister=lambda: [ME])
    options.update(kwargs)
    try:
        policy, source = desktop_policy._resolve_desktop_policy(**options)
    except SystemExit as exc:
        return None, str(exc), said
    return policy, source, said


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.desktop_policy; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.desktop_policy'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.desktop_policy", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.desktop_policy")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.desktop_policy as d; "
            f"print(all(getattr(t, n) is getattr(d, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_the_launchers_names_and_the_functions_own_imports() -> None:
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    # SYRD-351 moved the upgrade's call into upgrade_phases, and SYRD-366 moved the
    # resume-provision finish step's call into resume_provision_command; both
    # read it through the launcher.
    moved_trees = [ast.parse((ROOT / "scripts" / name).read_text(encoding="utf-8"))
                   for name in ("upgrade_phases.py", "resume_provision_command.py") if (ROOT / "scripts" / name).exists()]
    for name, count in LAUNCHER_CALLS.items():
        calls = [n for n in ast.walk(launcher_tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        moved = [n for tree in moved_trees for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(calls) + len(moved) == count and all(isinstance(n.func, ast.Name) for n in calls)
              and all(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                      and n.func.value.id == "launcher" for n in moved),
              f"the launcher calls {name} at its {count} baseline site, by its own name or through the launcher")
    moved = ast.parse((ROOT / "scripts" / "desktop_policy.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in LAUNCHER_READS})
    check(bare == [], f"the launcher's names are read through it, never past it: {bare}")
    through = sorted({n.attr for n in ast.walk(moved) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in OWN_IMPORTS})
    check(through == [], f"and the desktop-access helpers are the functions' own imports: {through}")


def test_the_new_project_decision_in_its_order() -> None:
    from scripts import team_launcher
    from scripts.desktop_access import DesktopAccessError

    with tempfile.TemporaryDirectory(prefix="syrd326.") as raw:
        policy_file = Path(raw) / "policy.json"
        policy_file.write_text(json.dumps({"mode": "headless", "from": "syrd326 file"}), encoding="utf-8")
        from_file = resolve(desktop_policy=policy_file)
    both = resolve(desktop_policy=Path("headless"), headless=True)
    headless_option = resolve(desktop_policy=Path("headless"))
    headless_flag = resolve(headless=True)

    def ambiguous(preferred: str) -> str:
        raise DesktopAccessError("syrd326: two desktops")

    ambiguity = resolve(owner_resolver=ambiguous, owners_lister=lambda: [ME, "syrd326-other"])
    ambiguity_yes = resolve(owner_resolver=ambiguous, owners_lister=lambda: [], yes=True)
    chose_headless = resolve(owner_resolver=ambiguous, owners_lister=lambda: [], input_func=answers("y"))
    declined = resolve(owner_resolver=ambiguous, owners_lister=lambda: [], input_func=answers("n"))

    approvals: list[object] = []
    record = {"gui_user": ME, "approved_by": "syrd326-approver", "reference": "SYRD-326 fixture"}
    with patched(team_launcher, read_host_desktop_approval=lambda path=None: approvals.append(path) or record):
        recorded = resolve(settings_path=Path("/nonexistent/syrd326/approval.json"))
    with patched(team_launcher, read_host_desktop_approval=lambda path=None: {}):
        yes_refused = resolve(yes=True)
        chose_headless_again = resolve(input_func=answers("2"))
    written: list[tuple[str, str]] = []
    reads = iter([{}, {"gui_user": ME, "approved_by": ME, "reference": "fresh"}])
    with patched(team_launcher, read_host_desktop_approval=lambda path=None: next(reads),
                 write_host_desktop_approval=lambda owner, *, settings_path=None, confirmed_by="": (
                     written.append((owner, confirmed_by)), Path("/nonexistent/syrd326/approval.json"))[1]):
        new_approval = resolve(input_func=answers(""))

    check(from_file[1] == "policy_file" and from_file[0] == {"mode": "headless", "from": "syrd326 file"},
          f"an operator's policy file comes first, read by the launcher's loader: {from_file}")
    check(both[0] is None and "not both" in both[1], f"--headless with --desktop-policy is refused: {both}")
    check(headless_option[:2] == ({"mode": "headless"}, "headless_option") and headless_flag[1] == "headless_option",
          f"then --headless: {headless_option} {headless_flag}")
    check(ambiguity[0] is None and ambiguity[1] == "switchyard: syrd326: two desktops",
          f"an ambiguous host stops, even interactively, and is never guessed: {ambiguity}")
    check(ambiguity_yes[0] is None and ambiguity_yes[1] == "switchyard: syrd326: two desktops",
          f"and --yes stops on a headless host rather than choose for anyone: {ambiguity_yes}")
    check(chose_headless[:2] == ({"mode": "headless"}, "chosen_headless")
          and declined[0] is None and "headless was declined" in declined[1],
          f"a host with no desktop offers headless, and declining it provisions nothing: {chose_headless} {declined}")
    check(recorded[1] == "host_approval" and recorded[0]["gui_user"] == ME
          and recorded[0]["consent"]["by"] == "syrd326-approver"
          and approvals == [Path("/nonexistent/syrd326/approval.json")],
          f"a recorded approval for this owner is used, through the launcher's reader: {recorded[:2]} {approvals}")
    check(yes_refused[0] is None and "--yes does not grant desktop access" in yes_refused[1],
          f"--yes never grants access the host has not approved: {yes_refused}")
    check(chose_headless_again[:2] == ({"mode": "headless"}, "chosen_headless"),
          f"the person may choose headless: {chose_headless_again}")
    check(new_approval[1] == "new_approval" and written == [(ME, ME)] and new_approval[0]["gui_user"] == ME,
          f"or record an approval, through the launcher's writer: {new_approval[:2]} {written}")


def test_recovery_takes_the_desktop_from_the_hosts_record() -> None:
    from scripts import desktop_policy, team_launcher
    from scripts.desktop_access import generated_policy

    plan = SimpleNamespace(project=PROJECT, owner_user=TENANT)
    declared = generated_policy(project=PROJECT, tenant=TENANT, gui_user=ME, approved_by=ME, reference="declared")
    other = generated_policy(project=PROJECT, tenant=TENANT, gui_user="syrd326-other", approved_by="x",
                             reference="other")
    record = {"gui_user": ME, "approved_by": "syrd326-approver", "approved_at": "2026-09-27T00:00:00Z",
              "reference": "SYRD-326 host record"}
    asked: list[object] = []
    with patched(team_launcher, read_host_desktop_approval=lambda path=None: asked.append(path) or record):
        headless = desktop_policy.approved_desktop_policy(plan, Tenant(PROJECT, TENANT, {"mode": "headless"}))
        generated = desktop_policy.approved_desktop_policy(plan, Tenant(PROJECT, TENANT),
                                                           approval_path=Path("/nonexistent/syrd326/a.json"))
        kept = desktop_policy.approved_desktop_policy(plan, Tenant(PROJECT, TENANT, declared))
        mismatch = desktop_policy.approved_desktop_policy(plan, Tenant(PROJECT, TENANT, other))
    with patched(team_launcher, read_host_desktop_approval=lambda path=None: {}):
        nothing = desktop_policy.approved_desktop_policy(plan, Tenant(PROJECT, TENANT))
        unapproved = desktop_policy.approved_desktop_policy(plan, Tenant(PROJECT, TENANT, declared))
    check(headless == (None, []) and nothing == (None, []), "headless, or nothing asked and nothing approved: no policy")
    check(generated[0] is not None and generated[1] == [] and generated[0]["gui_user"] == ME and generated[0]["consent"]["reference"]
          == "SYRD-326 host record" and asked[0] == Path("/nonexistent/syrd326/a.json"),
          f"nothing declared: the policy is generated from the host's record: {generated}")
    check(kept[1] == [] and kept[0]["gui_user"] == ME, "a declared policy naming the recorded desktop is kept")
    check(mismatch[0] is None and "is not the tenant's to answer" in mismatch[1][0],
          f"a tenant naming another desktop is refused: {mismatch}")
    check(unapproved[0] is None and "has no recorded desktop approval to grant it from" in unapproved[1][0],
          f"a tenant asking with no record behind it is refused: {unapproved}")


def test_recovery_installs_through_the_installer_it_is_given() -> None:
    from scripts import desktop_policy, team_launcher

    plan = SimpleNamespace(project=PROJECT, owner_user=TENANT)
    record = {"gui_user": ME, "approved_by": ME, "reference": "SYRD-326"}
    installed: list[tuple[object, dict[str, object]]] = []
    said: list[str] = []

    def installer(config: Tenant, **kwargs: object) -> Tenant:
        installed.append((config.desktop_access, dict(kwargs)))
        return config

    def refusing(config: Tenant, **kwargs: object) -> Tenant:
        raise SystemExit("syrd326 install refused")

    with tempfile.TemporaryDirectory(prefix="syrd326.") as raw:
        release = Path(raw) / "release"
        (release / "scripts").mkdir(parents=True)
        (release / "scripts" / "desktop_access.py").write_text("# syrd326 helper\n", encoding="utf-8")
        with patched(team_launcher, read_host_desktop_approval=lambda path=None: record):
            done = desktop_policy.install_recovered_desktop_access(
                plan, Tenant(PROJECT, TENANT), Path(raw) / "p326.json", source_release=release, installer=installer,
                print_func=said.append)
            with patched(team_launcher, configure_project_desktop=installer):
                default = desktop_policy.install_recovered_desktop_access(
                    plan, Tenant(PROJECT, TENANT), Path(raw) / "p326.json", print_func=said.append)
            failed = desktop_policy.install_recovered_desktop_access(
                plan, Tenant(PROJECT, TENANT), Path(raw) / "p326.json", installer=refusing, print_func=said.append)
        with patched(team_launcher, read_host_desktop_approval=lambda path=None: {}):
            headless = desktop_policy.install_recovered_desktop_access(
                plan, Tenant(PROJECT, TENANT, {"mode": "headless"}), Path(raw) / "p326.json", installer=refusing,
                print_func=said.append)
        helper = release / "scripts" / "desktop_access.py"
    check(done[1] is True and isinstance(done[0].desktop_access, dict) and done[0].desktop_access.get("gui_user") == ME
          and installed and installed[0][1]["helper"] == helper
          and installed[0][1]["config_path"].name == "p326.json",
          f"the policy is installed through the installer given, with the release's helper: {installed[:1]}")
    check(default[1] is True and len(installed) == 2 and installed[1][1]["helper"] is None,
          "without one, it is the launcher's configure_project_desktop, as rebound")
    check(failed == (None, False) and "desktop access could not be installed: syrd326 install refused" in said[-1],
          f"a failed install says so and reports failure: {failed}")
    check(headless[1] is True and headless[0].desktop_access == {"mode": "headless"},
          "a headless tenant needs nothing installed")


def test_an_upgrade_never_assumes_a_policy() -> None:
    from scripts import desktop_policy, team_launcher

    with patched(team_launcher, current_user_name=lambda: "syrd326-me"):
        none = desktop_policy.upgrade_desktop_policy_decision(Tenant(PROJECT, ""), None)
    headless = desktop_policy.upgrade_desktop_policy_decision(Tenant(PROJECT, TENANT), Path("headless"))
    with tempfile.TemporaryDirectory(prefix="syrd326.") as raw:
        wrong = Path(raw) / "wrong.json"
        wrong.write_text(json.dumps({"mode": "wayland", "project": "other"}), encoding="utf-8")
        invalid = desktop_policy.upgrade_desktop_policy_decision(Tenant(PROJECT, TENANT), wrong)
    check(none[0] is None and "no desktop policy, and every role launch needs one" in none[1][0]
          and "tenant 'syrd326-me'" in none[1][2], f"no policy: an operator has to choose, for this tenant: {none}")
    check(headless == ({"mode": "headless", "project": PROJECT, "tenant_user": TENANT}, []),
          f"headless is a choice the operator names: {headless}")
    check(invalid[0] is None and invalid[1][0].startswith(f"the policy supplied with --desktop-policy ({wrong})"),
          f"a policy for another project is refused: {invalid}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"desktop_policy_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
