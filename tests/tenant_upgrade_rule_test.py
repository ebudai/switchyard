#!/usr/bin/env python3
"""SYRD-825: a tenant's own upgrade actions are prompt-free for its owner, and for nothing and nobody else.

- The rule names one account, as JSON string data, and refuses at render any name that is not a plain account name.
- It lists exactly the catalogue's tenant upgrade actions, every one of them `allow_active`, and none of the
  shared-release or database-auth actions; and only through the root-owned helper.
- Provisioning renders and installs it beside the deploy rule, completion asks for it, teardown removes it.
- The root upgrade installs it, confirms it, and replaces one that differs, never through a link; a tenant's own run
  says what it can and cannot tell. The root half runs as root of a user namespace of this test's own.
"""

from __future__ import annotations

import live_boundary  # noqa: E402

live_boundary.isolate()  # SYRD-627: no live home, pane store or runtime directory, here or in a child
import json  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import team_launcher  # noqa: E402
from scripts.ticket_board import privileged_actions as pa  # noqa: E402
from scripts.ticket_board import privileged_install  # noqa: E402
from scripts.ticket_board import project_provision as provision  # noqa: E402
from scripts.ticket_board import tenant_upgrade_rule as rule  # noqa: E402

PROJECT, OWNER = "sapi", "sapi-agent"
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def _plan():
    return provision.build_plan(project=PROJECT, owner_user=OWNER, control_user="eric", source_repo=Path("/srv/s"))


def test_the_rule_names_one_account_four_actions_and_the_helper() -> None:
    text = rule.render(PROJECT, OWNER)
    listed = [f"{pa.ACTION_NAMESPACE}.{name}" for name in rule.TENANT_UPGRADE_ACTIONS]
    check(f"subject.user !== {json.dumps(OWNER)}" in text and text.count("subject.user") == 1,
          f"the owner is compared as a JSON string, once: {text}")
    check(f'action.lookup("program") !== {json.dumps(str(privileged_install.helper_path()))}' in text,
          f"only through the root-owned helper: {text}")
    check(f"{json.dumps(listed)}.indexOf(action.id) < 0" in text and text.count("polkit.Result.YES") == 1,
          f"exactly the four actions, one YES: {text}")
    check(all(pa.action_for(name).authentication == pa.ALLOW_ACTIVE for name in rule.TENANT_UPGRADE_ACTIONS),
          "every action it lists is already the control pane's, unprompted, in an active session")
    held = [action.name for action in pa.CATALOGUE if action.authentication != pa.ALLOW_ACTIVE]
    check(held and not any(f".{name}\"" in text for name in held),
          f"no action that asks an administrator is listed: {held}")


def test_a_name_that_could_leave_its_string_is_refused_at_render() -> None:
    for bad in ('sapi"agent', "sapi\\agent", "sapi'agent", "sapi\nagent", "Sapi-agent", "", "a" * 33,
                '"); return polkit.Result.YES; ("'):
        try:
            rule.render(PROJECT, bad)
            refused = False
        except ValueError:
            refused = True
        check(refused, f"{bad!r} is refused at render, never written into a rule")


def test_provisioning_installs_it_completion_asks_for_it_teardown_removes_it() -> None:
    plan = _plan()
    name = rule.rule_name(PROJECT)
    check(provision.polkit_rule_names(plan) == (plan.polkit_name, name)
          and name in provision.privileged_artifact_names(plan),
          f"it is one of the tenant's root artifacts, beside the deploy rule: {provision.privileged_artifact_names(plan)}")
    rendered = provision.render_polkit_rules(plan)
    check(rendered[name] == rule.render(PROJECT, OWNER) and rendered[plan.polkit_name] == provision.render_polkit_rule(plan),
          "rendered from the plan, as the root upgrade renders it")
    with tempfile.TemporaryDirectory(prefix="syrd825-artifacts.") as raw:
        provision.write_artifacts(plan, Path(raw))
        check((Path(raw) / name).read_text() == rule.render(PROJECT, OWNER), "write_artifacts writes it")
    installs = [line for line in provision.render_operator_commands(plan).splitlines()
                if line.endswith(f"'/etc/polkit-1/rules.d/{name}'")]
    check(len(installs) == 1 and installs[0].startswith("sudo install -m 0644 "),
          f"the operator packet installs it, 0644 like the deploy rule: {installs}")
    missing = team_launcher.privileged_packet_completion(
        plan, exists=lambda path: path.name != name, system_unit_active=lambda _u: True,
        owner_unit_active=lambda _u: True, board_answers=lambda: True)
    check(not missing.done and len(missing.problems) == 1
          and missing.problems[0].startswith(f"the polkit rule {name} is not installed"),
          f"a packet that did not install it is not finished, and says so: {missing.problems}")
    actions = team_launcher._switchyard_teardown_actions(
        plan, registry_path=Path("/tmp/registry"), home_base=Path("/home"), remove_owner_home=False,
        remove_owner_user=False)
    check(f"rm -f /etc/polkit-1/rules.d/{name}" in {" ".join(action.command) for action in actions},
          "teardown removes it")


def test_the_tenant_says_what_it_can_tell() -> None:
    if os.geteuid() == 0:
        print("tenant_upgrade_rule_test: running as root; the tenant's view is not observable")
        return
    with tempfile.TemporaryDirectory(prefix="syrd825-tenant.") as raw:
        rules = Path(raw) / "rules.d"
        said = rule.ensure(PROJECT, OWNER, rules_dir=rules, dry_run=False)
        check(f"this host has no {rules}" in said, f"no rules directory, nothing written: {said}")
        rules.mkdir()
        path = rules / rule.rule_name(PROJECT)
        said = rule.ensure(PROJECT, OWNER, rules_dir=rules, dry_run=True)
        check(said.startswith("switchyard: would install") and not path.exists(), f"a dry run writes nothing: {said}")
        said = rule.ensure(PROJECT, OWNER, rules_dir=rules, dry_run=False)
        check("only root can write it" in said and "upgrade-tenant-release" in said and not path.exists(),
              f"a tenant's own run writes nothing, and names the root upgrade: {said}")
        path.write_text("x")
        path.chmod(0)
        said = rule.ensure(PROJECT, OWNER, rules_dir=rules, dry_run=True)
        check(said.startswith("switchyard: note: ") and "can only be read by root" in said,
              f"a rule only root can read is a note, naming the root upgrade: {said}")
        path.chmod(0o600)
        check("names no owner account" in rule.ensure(PROJECT, "", rules_dir=rules, dry_run=True),
              "a tenant with no owner gets no rule")


def root_child(rules: Path) -> dict:
    """As root of this test's own user namespace: install, confirm, repair, refuse a link."""
    out: dict = {}
    path = rules / rule.rule_name(PROJECT)
    out["install"] = rule.ensure(PROJECT, OWNER, rules_dir=rules, dry_run=False)
    info = os.stat(path)
    out["installed"] = [info.st_uid, stat.S_IMODE(info.st_mode), path.read_text() == rule.render(PROJECT, OWNER)]
    out["again"] = rule.ensure(PROJECT, OWNER, rules_dir=rules, dry_run=False)
    path.write_text(path.read_text() + "// tampered\n")
    out["tampered"] = rule.ensure(PROJECT, OWNER, rules_dir=rules, dry_run=False)
    path.chmod(0o666)
    out["widened"] = rule.ensure(PROJECT, OWNER, rules_dir=rules, dry_run=False)
    out["after"] = [stat.S_IMODE(os.stat(path).st_mode), path.read_text() == rule.render(PROJECT, OWNER)]
    elsewhere = rules.parent / "elsewhere"
    elsewhere.write_text("not a rule\n")
    path.unlink()
    path.symlink_to(elsewhere)
    out["link"] = rule.ensure(PROJECT, OWNER, rules_dir=rules, dry_run=False)
    out["elsewhere"] = elsewhere.read_text()
    return out


def test_the_root_upgrade_installs_confirms_and_repairs_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd825-root.") as raw:
        rules = Path(raw) / "rules.d"
        rules.mkdir()
        done = subprocess.run(["unshare", "--user", "--map-root-user", sys.executable, str(Path(__file__).resolve()),
                               "--root-child", str(rules)], capture_output=True, text=True, timeout=120)
        check(done.returncode == 0, f"the namespace child ran: {done.stderr[-1500:]}")
        out = json.loads(done.stdout.strip().splitlines()[-1])
    check(out["install"].startswith("switchyard: installed") and out["installed"] == [0, 0o644, True],
          f"root installs it, root's and 0644, with the rendered bytes: {out}")
    check(out["again"].startswith(f"switchyard: {PROJECT}'s tenant-upgrade polkit rule is in place"),
          f"and confirms it on the next upgrade: {out['again']}")
    check(out["tampered"].startswith("switchyard: replaced") and out["widened"].startswith("switchyard: replaced")
          and out["after"] == [0o644, True], f"a rule with other bytes, or a wider mode, is replaced: {out}")
    check("is a symlink" in out["link"] and out["elsewhere"] == "not a rule\n",
          f"and nothing is written through a link: {out}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--root-child":
        print(json.dumps(root_child(Path(sys.argv[2]))))
        return 0
    for name, value in list(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"tenant_upgrade_rule_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
