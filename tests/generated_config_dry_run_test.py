#!/usr/bin/env python3
"""SYRD-418: a dry run of the generated config upgrade writes nothing and hands nothing to the owner.

Audit of SYRD-417 found that `upgrade_generated_project_config(dry_run=True)`
wrote `TICKET_BOARD_DIRECTORCTL` into a generated tenant configuration, and
handed it to its owner, while returning `changed=False`, whenever a role's
`directorctl` pin was stale: the final write was not conditioned on the dry run.

Each scenario is one test-owned generated tenant -- its configuration and a
current layout template in a provision directory, every role pinned nowhere, so
the stale `directorctl` pin is the only change -- driven through the real
`upgrade_generated_project_config` in a child that is root in its own user and
network namespace (`unshare -rn`). There the shipped `ensure_owner_file` really
reaches its ownership step for a tenant owned by another account, and the
runner it is given records what it would run. The no-follow read, the atomic
writer and the owner step are the launcher's own; only the four seams that
read the host -- the board root, the shared pane launcher, the durable session
directory and the user-runtime check -- stand in, pointing into the tree.

The same scenarios run on main `BEFORE`, from a `git archive`, to show the
defect they pin, and on this tree.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "05c6ab435ebc640d8a1c0a6cbbf09091bb513bef"
CHECKS = 0
OWNER = "syrd418-owner"

#: Runs inside `unshare -rn`, as namespace root: argv is the tree to import, the scratch directory, and the scenario.
CHILD = r'''
import json, os, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
tree, tmp, scenario = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(tree))
from scripts import team_launcher as launcher
import pwd
# The owner is a stand-in account whose home is the scratch directory, owned by namespace root (SYRD-608: root
# writes for an owner by descriptor from that home, and refuses a path outside it).
_getpwnam = pwd.getpwnam
sys.path.insert(0, str(tree / "tests"))
try:
    import owner_home_sandbox  # noqa: F401 -- root stages its writes in a private directory, where this tree has one
except ImportError:
    pass
pwd.getpwnam = lambda name: (pwd.struct_passwd((name, "x", 0, 0, "", str(tmp), "/bin/sh"))
                             if name == "''' + OWNER + r'''" else _getpwnam(name))

provision = tmp / "provision"
provision.mkdir(mode=0o755)
roles = [SimpleNamespace(role=name, detached=False, env={}) for name in ("director", "worker")]
config = SimpleNamespace(project="p418", project_name="", run_as_user="''' + OWNER + r'''", roles=roles, layout=None,
                         pane_launcher=tmp / "opt" / "current" / "scripts" / "team-launcher",
                         session_dir=tmp / "durable" / "''' + OWNER + r'''")
config.layout = provision / launcher.presentation_layout_markers(config)[0]
config.layout.write_text(json.dumps(launcher._new_project_layout_payload(len(roles))), encoding="utf-8")
config_path = provision / "p418.json"
document = {"project": "p418", "roles": [{"role": r.role, "env": {"KEEP": "1"}} for r in roles], "layout": str(config.layout)}
target = provision / "real.json" if scenario == "dry run, linked" else config_path
target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
if target != config_path:
    os.symlink(target.name, config_path)
launcher._tenant_board_root_from_config_or_plan = lambda c, p=None: tmp / "board"
launcher.switchyard_shared_pane_launcher = lambda root=None: tmp / "opt" / "current" / "scripts" / "team-launcher"
launcher._new_project_session_dir = lambda project, user: str(tmp / "durable" / user)
launcher.session_dir_uses_user_runtime = lambda d, u: False


def state():
    seen = {}
    for path in sorted(provision.iterdir()):
        info = os.lstat(path)
        seen[path.name] = {"bytes": os.readlink(path) if os.path.islink(path) else path.read_text(encoding="utf-8"),
                           "inode": info.st_ino, "mtime_ns": info.st_mtime_ns, "mode": info.st_mode, "uid": info.st_uid}
    return seen


ran = []
def runner(args, **kwargs):
    ran.append(list(args))
    return subprocess.CompletedProcess(args, 0, "", "")


before = state()
result = launcher.upgrade_generated_project_config(config, config_path=config_path, dry_run=scenario.startswith("dry run"), runner=runner)
print(json.dumps({"euid": os.geteuid(), "caller": launcher.current_user_name(), "changed": result.changed, "message": result.message,
                  "ran": ran, "before": before, "after": state(), "pin": str(tmp / "board" / "current" / "scripts" / "directorctl"),
                  "config": str(config_path), "layout": config.layout.name}))
'''


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def scenario(tree: Path, name: str) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="syrd418-"))
    try:
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(tmp), "PYTHONDONTWRITEBYTECODE": "1"}
        done = subprocess.run(["unshare", "-rn", sys.executable, "-B", "-c", CHILD, str(tree), str(tmp), name],
                              env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        check(done.returncode == 0, f"{name}: the child ran ({done.returncode}): {done.stderr[-1500:]}")
        got = json.loads(done.stdout.strip().splitlines()[-1])
        check(got["euid"] == 0 and got["caller"] == "root", f"{name}: the child is namespace root, so the owner step is live: {got['euid']} {got['caller']}")
        return got
    finally:
        shutil.rmtree(tmp)


def roles_pinned(got: dict) -> list[str | None]:
    document = json.loads(got["after"]["p418.json"]["bytes"])
    return [role["env"].get("TICKET_BOARD_DIRECTORCTL") for role in document["roles"]]


def chown(got: dict) -> list[str]:
    """The ownership step `ensure_owner_file` took for this tenant's config on main `BEFORE`."""
    return ["chown", f"{OWNER}:{OWNER}", got["config"]]


def handoff(got: dict) -> list[str]:
    """The ownership step it takes now: the descriptor helper, rooted at the owner's home (SYRD-608)."""
    home = str(Path(got["config"]).parent.parent)
    return [sys.executable, "-I", str(ROOT / "scripts" / "tenant_paths.py"), "--root", home, "attrs", "--owner", OWNER,
            "--group", OWNER, got["config"]]


def test_main_wrote_the_pin_in_a_dry_run() -> None:
    if os.environ.get("SYRD418_MUTATION_SKIP_BEFORE"):
        return
    base = Path(tempfile.mkdtemp(prefix="syrd418-before-"))
    try:
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", BEFORE], capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(base)], input=archive.stdout, check=True)
        got = scenario(base, "dry run")
        check(got["changed"] is False and got["after"]["p418.json"]["bytes"] != got["before"]["p418.json"]["bytes"]
              and roles_pinned(got) == [got["pin"]] * 2 and got["ran"] == [chown(got)],
              f"main {BEFORE[:8]}: a dry run reported changed=False, yet wrote the pin and handed the config to its owner: {got['ran']}")
    finally:
        shutil.rmtree(base)


def test_a_directorctl_only_dry_run_leaves_the_config_and_its_owner_alone() -> None:
    got = scenario(ROOT, "dry run")
    check(got["changed"] is False and got["message"].endswith(f"pane directorctl can be pinned to {got['pin']}"),
          f"the dry run reports the stale pin, as before: {got['message']}")
    check(got["after"] == got["before"],
          f"the config and layout keep their bytes, inode, mtime, mode and owner, and nothing is added beside them: {got['after']}")
    check(got["ran"] == [], f"no ownership step ran: {got['ran']}")


def test_the_upgrade_still_writes_the_pin_and_hands_it_to_the_owner() -> None:
    got = scenario(ROOT, "apply")
    check(got["changed"] is True and got["message"].endswith(f"pinned pane directorctl to {got['pin']}"), got["message"])
    check(roles_pinned(got) == [got["pin"]] * 2, f"every role is pinned: {roles_pinned(got)}")
    kept = json.loads(got["after"]["p418.json"]["bytes"])["roles"]
    check(all(role["env"]["KEEP"] == "1" for role in kept), "and keeps its own env")
    check(got["ran"] == [handoff(got)], f"and the config, only, is handed to its owner: {got['ran']}")
    check(got["after"][got["layout"]] == got["before"][got["layout"]], "the current layout template is not touched")


def test_a_dry_run_still_refuses_a_linked_config() -> None:
    got = scenario(ROOT, "dry run, linked")
    check(got["changed"] is False and got["message"].startswith("switchyard: refusing to read p418's generated configuration")
          and got["message"].endswith("Nothing was changed."), f"the no-follow refusal stands in a dry run: {got['message']}")
    check(got["after"] == got["before"] and got["ran"] == [], "and nothing is written or handed over")


def main() -> int:
    for name, test in list(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
    print(f"generated_config_dry_run_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
