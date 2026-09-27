#!/usr/bin/env python3
"""SYRD-323: the display bridge's verdict and install, against the launcher they came out of.

Whether a crossing presentation window's tabs can reach the tenant's display
sessions, and installing the bridge when they cannot, moved into
`scripts/display_bridge.py` unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- Every name the launcher and the presentation controller reach as
  `team_launcher.<name>` is still there and is the very same object,
  whichever module is imported first; every reference goes through the
  launcher (rule 21).
- **Seams (rule 24).** The suites rebind `display_bridge_launch_problem` on the
  launcher; the module reaches `display_bridge_state` and the current user
  through the launcher when it runs.
- **The privileged names stay the function's own.** `display_bridge_state`
  imports the grant name, the privileged root and the rule's text from
  `project_provision` where it uses them; the launcher's own
  `TENANT_CONTROL_ROOT` -- a different object the suites rebind -- is never
  what it reads (SYRD-323 proof step 7).
- **The verdicts are unchanged:** not needed, install, refuse (never
  overwriting what an untrusted account could have put there), unverified
  (the rule is root's to read), present; and the install goes through the
  runner given and is read back.

Every grant and rule here is a file this test writes in its own temporary
directory, owned by this account, which is passed in as the owner the check
expects; no sudo, visudo, /etc or real grant is touched.
"""

from __future__ import annotations

import ast
import json
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

EXPORTED = ('SUDOERS_RULE_MODE', 'display_bridge_launch_problem', 'display_bridge_state', 'DisplayBridgeState',
            'ensure_display_bridge', 'sudoers_rule_state')
READ_ELSEWHERE = {"presentation_controller": ("display_bridge_launch_problem",)}
PROVISION_NAMES = ("TENANT_CONTROL_GRANT_NAME", "TENANT_CONTROL_ROOT", "tenant_control_commands",
                   "tenant_control_sudoers_document")
PROJECT, OWNER, DESK = "p323", "syrd323-owner", "syrd323-desk"
UID, GID = os.getuid(), os.getgid()


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


class Bridge:
    """A grant root and a sudoers directory this test owns."""

    def __init__(self, root: Path) -> None:
        from scripts.ticket_board.project_provision import TENANT_CONTROL_GRANT_NAME, tenant_control_sudoers_document

        self.grants = root / "grants"
        self.sudoers = root / "sudoers.d"
        self.sudoers.mkdir()
        self.grant = self.grants / PROJECT / TENANT_CONTROL_GRANT_NAME
        self.rule = self.sudoers / f"49-{PROJECT}-tenant-control"
        self.expected = tenant_control_sudoers_document(PROJECT, DESK) + "\n"

    def write_grant(self, mode: int = 0o644, **changes: str) -> None:
        payload = {"project": PROJECT, "owner": OWNER, "authorized_user": DESK, **changes}
        self.grant.parent.mkdir(parents=True, exist_ok=True)
        self.grant.write_text(json.dumps(payload), encoding="utf-8")
        self.grant.chmod(mode)

    def write_rule(self, text: str | None = None, mode: int = 0o440) -> None:
        if self.rule.exists() or self.rule.is_symlink():
            self.rule.chmod(0o600) if not self.rule.is_symlink() else None
            self.rule.unlink()
        self.rule.write_text(self.expected if text is None else text, encoding="utf-8")
        self.rule.chmod(mode)

    def state(self, gui_user: str = DESK, owner: str = OWNER, grant_owner_uid: int = UID):
        from scripts import display_bridge

        return display_bridge.display_bridge_state(
            SimpleNamespace(project=PROJECT, run_as_user=owner), gui_user=gui_user, grant_root=self.grants,
            sudoers_dir=self.sudoers, grant_owner_uid=grant_owner_uid, grant_owner_gid=GID)


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.display_bridge; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.display_bridge'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.display_bridge", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.display_bridge")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.display_bridge as d; "
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


def test_the_seams_and_the_privileged_names_are_read_where_they_were() -> None:
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(launcher_tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "ensure_display_bridge"]
    # SYRD-350 moved the upgrade's U2 phase, holding that site, to upgrade_phases,
    # which calls it through the launcher; the total is unchanged.
    phases = ast.parse((ROOT / "scripts" / "upgrade_phases.py").read_text(encoding="utf-8"))
    phase_calls = [n for n in ast.walk(phases)
                   if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "ensure_display_bridge"]
    check(len(calls) + len(phase_calls) == 1 and all(isinstance(n.func, ast.Name) for n in calls)
          and all(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                  and n.func.value.id == "launcher" for n in phase_calls),
          "the upgrade calls ensure_display_bridge at its one baseline site, by the launcher's name, "
          "or through the launcher from upgrade_phases")
    moved = ast.parse((ROOT / "scripts" / "display_bridge.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in ("display_bridge_state", "display_bridge_launch_problem", "current_user_name")})
    check(bare == [], f"the module reaches the verdict and the current user through the launcher: {bare}")
    through = sorted({n.attr for n in ast.walk(moved) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in PROVISION_NAMES})
    check(through == [], f"and the privileged names are its own imports, never the launcher's: {through}")


def test_the_launchers_own_root_is_never_the_one_read() -> None:
    from scripts import display_bridge, team_launcher
    from scripts.ticket_board import project_provision

    config = SimpleNamespace(project="syrd323-no-such-project", run_as_user=OWNER)
    with patched(team_launcher, TENANT_CONTROL_ROOT=Path("/nonexistent/syrd323/launcher-root")):
        state = display_bridge.display_bridge_state(config, gui_user=DESK,
                                                    sudoers_dir=Path("/nonexistent/syrd323/sudoers"))
    expected = Path(project_provision.TENANT_CONTROL_ROOT) / config.project / project_provision.TENANT_CONTROL_GRANT_NAME
    check(state.action == "install" and state.detail.startswith(f"{DESK} may attach this project's display tabs: {expected} and "),
          f"with no grant root given, the grant is looked for under project_provision's root: {state.detail}")


def test_the_verdicts() -> None:
    from scripts import display_bridge, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd323.") as raw:
        bridge = Bridge(Path(raw))
        verdicts: dict[str, object] = {}
        verdicts["same account"] = bridge.state(gui_user=OWNER).action
        with patched(team_launcher, current_user_name=lambda: DESK):
            verdicts["no owner, the caller's own"] = bridge.state(owner="").action
        missing = bridge.state()
        bridge.write_grant(mode=0o666)
        verdicts["writable grant"] = bridge.state()
        bridge.write_grant()
        verdicts["grant of another owner"] = bridge.state(grant_owner_uid=UID + 1)
        bridge.write_grant(project="other")
        verdicts["other project"] = bridge.state()
        bridge.write_grant(owner="someone")
        verdicts["other owner"] = bridge.state()
        bridge.write_grant(authorized_user="someone")
        verdicts["other user"] = bridge.state()
        bridge.write_grant()
        verdicts["no rule"] = bridge.state()
        bridge.write_rule(text="# drifted\n")
        verdicts["drifted"] = bridge.state()
        bridge.write_rule(mode=0o400)
        verdicts["mode"] = bridge.state()
        bridge.write_rule(mode=0o664)
        verdicts["writable rule"] = bridge.state()
        bridge.write_rule()
        verdicts["present"] = bridge.state()
        other_owner = display_bridge.sudoers_rule_state(bridge.rule, expected=bridge.expected, root_uid=UID + 1)
        bridge.rule.unlink()
        bridge.rule.symlink_to(bridge.sudoers / "elsewhere")
        verdicts["symlink"] = bridge.state()
        target_written = (bridge.sudoers / "elsewhere").exists()
        bridge.rule.unlink()
        bridge.write_rule(mode=0o000)
        unreadable = bridge.state() if os.geteuid() != 0 else None
        bridge.rule.chmod(0o600)
    check(verdicts["same account"] == "not needed" and verdicts["no owner, the caller's own"] == "not needed",
          "one account owning the window and the sessions needs no bridge, the launcher's current user included")
    check(missing.action == "install" and missing.commands and str(bridge.grant) in missing.detail,
          f"no grant: the bridge is owed, with the commands that install it: {missing.detail}")
    for name, needle in (("writable grant", "is not a root-owned, root-writable file"),
                         ("grant of another owner", "is not a root-owned, root-writable file"),
                         ("other project", "names a different project"),
                         ("other owner", "names owner 'someone', and this tenant's owner is syrd323-owner"),
                         ("other user", "authorizes someone, and the desktop policy names syrd323-desk")):
        check(verdicts[name].action == "refuse" and needle in verdicts[name].detail,
              f"{name}: refused, never taken over: {verdicts[name]}")
    check(verdicts["no rule"].action == "install" and verdicts["no rule"].detail == missing.detail,
          f"a right grant with no rule: install: {verdicts['no rule']}")
    check(verdicts["drifted"].action == "install" and verdicts["drifted"].detail.endswith("(its text has drifted)"),
          f"root's rule with drifted text is ours to normalize: {verdicts['drifted']}")
    check(verdicts["mode"].action == "install" and "mode 0400" in verdicts["mode"].detail,
          f"and so is one in the wrong mode: {verdicts['mode']}")
    check(verdicts["writable rule"].action == "refuse" and "mode 0664" in verdicts["writable rule"].detail,
          f"a rule another account could rewrite is refused, not replaced: {verdicts['writable rule']}")
    check(verdicts["present"].action == "present", f"the exact bytes in the exact mode: {verdicts['present']}")
    check(verdicts["symlink"].action == "refuse" and "is a symlink" in verdicts["symlink"].detail and not target_written,
          f"a symlinked rule is refused and nothing is written through it: {verdicts['symlink']}")
    if unreadable is not None:
        check(unreadable.action == "unverified" and "can only be read by root" in unreadable.detail,
              f"a rule only root can read is neither a pass nor a refusal: {unreadable}")
    check(other_owner[0] == "refuse" and f"not by uid {UID + 1}" in other_owner[1],
          f"a rule owned by anyone but the expected owner is refused: {other_owner}")


def test_the_upgrade_installs_through_its_runner_and_reads_back() -> None:
    from scripts import display_bridge

    config = SimpleNamespace(project=PROJECT, run_as_user=OWNER)
    with tempfile.TemporaryDirectory(prefix="syrd323.") as raw:
        bridge = Bridge(Path(raw))
        where = dict(grant_root=bridge.grants, sudoers_dir=bridge.sudoers, grant_owner_uid=UID, grant_owner_gid=GID)
        said: list[str] = []
        ran: list[list[str]] = []

        def refuse(args: list[str], **kwargs: object) -> object:
            raise AssertionError(f"nothing may run here: {args!r}")

        dry = display_bridge.ensure_display_bridge(config, gui_user=DESK, dry_run=True, runner=refuse,
                                                   print_func=said.append, **where)
        dry_wrote = bridge.grant.exists()

        def installs(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            ran.append(list(args))
            bridge.write_grant()
            bridge.write_rule()
            return subprocess.CompletedProcess(args, 0, "", "")

        installed = display_bridge.ensure_display_bridge(config, gui_user=DESK, runner=installs,
                                                         print_func=said.append, **where)
        already = display_bridge.ensure_display_bridge(config, gui_user=DESK, runner=refuse,
                                                       print_func=said.append, **where)
        said_after_already = len(said)
        bridge.write_rule(text="# drifted\n")

        def exits_zero_writes_nothing(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, "", "")

        hollow = display_bridge.ensure_display_bridge(config, gui_user=DESK, runner=exits_zero_writes_nothing,
                                                      print_func=said.append, **where)
        failing = display_bridge.ensure_display_bridge(
            config, gui_user=DESK, print_func=said.append,
            runner=lambda args, **kwargs: subprocess.CompletedProcess(args, 3, "", "syrd323 denied"), **where)
        bridge.write_grant(authorized_user="someone")
        refused = display_bridge.ensure_display_bridge(config, gui_user=DESK, runner=refuse, print_func=said.append,
                                                       **where)
        bridge.write_grant()
        bridge.write_rule(mode=0o000)
        unverified = (display_bridge.ensure_display_bridge(config, gui_user=DESK, runner=refuse, print_func=said.append,
                                                           root_check="syrd323 check", **where)
                      if os.geteuid() != 0 else False)
        bridge.rule.chmod(0o600)
    check(dry is True and not dry_wrote and said[0].startswith("switchyard: would install the display bridge so "),
          f"a dry run says what it would install and writes nothing: {said[:1]}")
    check(installed is True and len(ran) == 1 and ran[0][:2] == ["sh", "-euc"]
          and said[1].startswith("switchyard: installed the display bridge so "),
          f"the install runs the commands through the runner given, and is read back as present: {ran} {said[1]}")
    check(already is True and said_after_already == 2, "a present bridge runs nothing and says nothing")
    check(hollow is False and f"installed {PROJECT}'s display bridge, but it does not read back as present: " in said[2]
          and said[2].endswith("(its text has drifted)"),
          f"an install that exits 0 and leaves the rule wrong is not trusted: {said[2]}")
    check(failing is False and said[3].startswith(f"switchyard: could not install {PROJECT}'s display bridge for {DESK} "
                                                  "(exit 3): syrd323 denied"), f"a failed install says why: {said[3]}")
    check(refused is False and "every tab refused" in said[4] and said[4].endswith("Nothing was changed."),
          f"a refused bridge stops the upgrade and changes nothing: {said[4]}")
    if os.geteuid() != 0:
        check(unverified is False and "syrd323 check" in said[5] and "could not be verified from here" in said[5],
              f"an unreadable rule sends the check to root's boundary: {said[5]}")


def test_a_launch_judges_the_half_it_can_see() -> None:
    from scripts import display_bridge, team_launcher

    config = SimpleNamespace(project=PROJECT, run_as_user=OWNER)
    with tempfile.TemporaryDirectory(prefix="syrd323.") as raw:
        bridge = Bridge(Path(raw))
        where = dict(grant_root=bridge.grants, grant_owner_uid=UID)
        missing = display_bridge.display_bridge_launch_problem(config, gui_user=DESK, **where)
        bridge.write_grant(authorized_user="someone")
        refused = display_bridge.display_bridge_launch_problem(config, gui_user=DESK, **where)
        bridge.write_grant()
        right = display_bridge.display_bridge_launch_problem(config, gui_user=DESK, **where)
        with patched(team_launcher, display_bridge_state=lambda config, **kwargs: display_bridge.DisplayBridgeState(
                "refuse", "syrd323 patched verdict")):
            rebound = display_bridge.display_bridge_launch_problem(config, gui_user=DESK, **where)
    check(missing == (f"no display bridge is installed for {DESK}: {bridge.grant} does not exist, so "
                      "`sudo -n switchyard-display-attach` in each tab asks for a password and exits"),
          f"a missing grant means every tab asks for a password: {missing!r}")
    check("authorizes someone" in refused, f"a grant naming somebody else is the problem: {refused!r}")
    check(right == "", "a right grant is not second-guessed over a rule this process cannot read")
    check(rebound == "syrd323 patched verdict", "the verdict is the launcher's display_bridge_state, as rebound")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"display_bridge_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
