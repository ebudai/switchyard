#!/usr/bin/env python3
"""SYRD-757: root acts as or for a role's own account only when it is one root made for that project.

SYRD-663 holds a project's owner to root's records, but each role's `run_as_user` was still read straight from the
tenant's configuration. With `role_state_isolation` false -- the loader's default for a legacy configuration -- root
took that value as the account to act as or for:

- every root upgrade repatriated the role's state from that account's home into the owner's, owned by the owner
  (`role_identity_cutover.repatriate_role_runtime_state`): another tenant's or root's `~/.codex/sessions`;
- root's worktree git ran as it (`project_worktrees._config_git_owner_rules` -> `owner_git._root_git_owner`), on
  `start` before the role-isolation gate;
- `team-launcher <p> pane detach-role` re-executed as it, or, for `root`, stayed root and wrote the configuration;
- root's launch started the role's pane, or its detached process, as it (`launch_phases` `run_as_user=` /
  `bin_user=` from `role_run_as_user`);
- root's stopped-role model repair took it as the root of the role's session records.

Here, as root in a user, mount, pid and network namespace with real uids: root's provisioning record is the
product's own, the registry entry is root's, the tenant's configuration is the one the product generates, and the
accounts are real -- the owner, another tenant with session files and a git checkout, root's own session files, a
genuine role account root made (`pie-main`, in `pie-roles`), and `pie-app`, an account of that canonical name root
never put in `pie-roles`. `sudo`, `runuser`, `setpriv` and `su` are stubs that record their argv and refuse.

- Main's code (from a git archive), each route driven directly with the configuration its loader accepts, does what
  the ticket says: the repatriation copies the other tenant's and root's session files to the owner, the git and pane
  account decisions name the other tenant, and detach-role as root rewrites the configuration.
- The candidate's real `scripts/switchyard` (`upgrade`, `start`, `set-role-runtime`) and `scripts/team-launcher`
  (`pane detach-role`), run as root, refuse every planted account at the configuration load -- before anything is
  read, copied, run or launched -- naming the role and the account; nothing in any home or in root's records changes
  and no account switch is even attempted. So does a direct `load_project_config`.
- A blank role account (the owner's) and the genuine `pie-main` are not refused, and the genuine legacy repatriation
  still converges: `pie-main`'s session file is copied to the owner and owned by it, and a second run copies nothing.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "e04db9cda240e78c5239d7768707aee21eb1b48a"  # main before SYRD-757
SLUG, NAME = "pie", "Personal Inference Engine"
OWNER, OWNER_UID = "pie-agent", 1018
OTHER, OTHER_UID = "otto-agent", 1019  # another tenant
GENUINE, GENUINE_UID = "pie-main", 1020  # the role account root made for role main
STRAY, STRAY_UID = "pie-app", 1021  # canonical for role app, but root never put it in pie-roles
SERVICE, SERVICE_UID = "pie-director", 1022  # a board service account an operator named <project>-<role>
ROLES_GID = 1600
HOME = Path("/home") / OWNER
OTHER_HOME = Path("/home") / OTHER
GENUINE_HOME = Path("/home") / GENUINE
ROOT_HOME = Path("/run/root-home")
CHECKOUT = HOME / "Projects" / "pie"
PROVISION = CHECKOUT / ".switchyard" / "provision"
OTHER_REPO = OTHER_HOME / "proj"
LEFTOVER = Path("/home/pie-designer")  # no account has this name
CALLS = Path("/stubs/calls.log")
SESSIONS = ".codex/sessions/2026/11/12"
#: role main's planted account, and why it is refused. `wrong-role`: the canonical account of another role.
PLANTED = {"foreign": OTHER, "root": "root", "uid0": "toor", "unknown": "ghost", "wrong-role": STRAY}
ENTRY_POINTS = {
    "upgrade": ["switchyard", "upgrade", SLUG],
    "start": ["switchyard", "start", SLUG],
    "set-role-runtime": ["switchyard", "set-role-runtime", SLUG, "main", "--model", ""],
    "pane detach-role": ["team-launcher", SLUG, "pane", "detach-role", "main"],
}
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def sh(*argv: str) -> None:
    subprocess.run(list(argv), check=True, capture_output=True)


def snapshot(*roots: Path) -> dict:
    """Owner, mode and content digest of everything under each root: what a refused run must not change."""
    found = {}
    for top in roots:
        for path in [top, *sorted(top.rglob("*"))]:
            info = os.lstat(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16] if stat.S_ISREG(info.st_mode) else ""
            found[str(path)] = [info.st_uid, f"{stat.S_IMODE(info.st_mode):04o}", digest]
    return found


def watched() -> dict:
    return snapshot(HOME, OTHER_HOME, GENUINE_HOME, ROOT_HOME, LEFTOVER, Path("/etc/switchyard"))


def calls() -> list[str]:
    return CALLS.read_text(encoding="utf-8").splitlines() if CALLS.exists() else []


def enter_root_owned_root(stage: Path) -> None:
    """Become root of a filesystem namespace root really owns (as `root_owner_from_record_test` does)."""
    new = stage / "newroot"
    new.mkdir()
    sh("mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", str(new))
    for directory in ("usr", "etc", "dev", "proc", "tmp", "run", "home", "opt", "stage", "var/lib/switchyard",
                      "var/tmp", "stubs/bin", "stubs/sbin"):
        (new / directory).mkdir(parents=True, exist_ok=True)
    (new / "tmp").chmod(0o1777)
    for link, target in (("bin", "usr/bin"), ("sbin", "usr/bin"), ("lib", "usr/lib"), ("lib64", "usr/lib")):
        (new / link).symlink_to(target)
    sh("mount", "--rbind", "/usr", str(new / "usr"))
    sh("mount", "--rbind", "/dev", str(new / "dev"))
    sh("mount", "--bind", str(stage), str(new / "stage"))
    os.chroot(new)
    os.chdir("/")
    sh("mount", "-t", "proc", "proc", "/proc")


def make_accounts() -> None:
    accounts = ((OWNER, OWNER_UID, HOME), (OTHER, OTHER_UID, OTHER_HOME), (GENUINE, GENUINE_UID, GENUINE_HOME),
                (STRAY, STRAY_UID, Path("/home") / STRAY), (SERVICE, SERVICE_UID, Path("/home") / SERVICE))
    Path("/etc/passwd").write_text(
        f"root:x:0:0::{ROOT_HOME}:/bin/sh\ntoor:x:0:0::{ROOT_HOME}:/bin/sh\npie-audit:x:0:0::{ROOT_HOME}:/bin/sh\n"
        "boardsvc:x:1500:1500::/nonexistent:/bin/false\n"
        + "".join(f"{name}:x:{uid}:{uid}::{home}:/bin/sh\n" for name, uid, home in accounts), encoding="utf-8")
    Path("/etc/group").write_text(
        "root:x:0:\nboardsvc:x:1500:\n" + "".join(f"{name}:x:{uid}:\n" for name, uid, _ in accounts)
        + f"pie-roles:x:{ROLES_GID}:boardsvc,{OWNER},pie-audit,{SERVICE},{GENUINE}\n", encoding="utf-8")
    os.makedirs(ROOT_HOME, mode=0o700)
    for name, uid, home in accounts:
        os.makedirs(home, mode=0o700)
        os.chown(home, uid, uid)
    # Session files the repatriation would carry: another tenant's, root's own, and the genuine role account's.
    for home, uid, label in ((OTHER_HOME, OTHER_UID, "otto-secret"), (ROOT_HOME, 0, "root-secret"),
                             (GENUINE_HOME, GENUINE_UID, "pie-main-legit")):
        target = home / SESSIONS / f"rollout-{label}.jsonl"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"secret": label}) + "\n", encoding="utf-8")
        for path in [*target.parents][: len(Path(SESSIONS).parts) + 1]:
            if path != home.parent:
                os.chown(path, uid, uid)
        os.chown(target, uid, uid)
    # A directory left at the canonical name of a role account root never made: the home lookup's fallback for
    # `pie-designer`, which has no account.
    leftover = LEFTOVER / SESSIONS / "rollout-leftover.jsonl"
    leftover.parent.mkdir(parents=True)
    leftover.write_text(json.dumps({"secret": "leftover"}) + "\n", encoding="utf-8")
    for path in [LEFTOVER, *[p for p in leftover.parents if str(p).startswith(str(LEFTOVER) + "/")], leftover]:
        os.chown(path, OTHER_UID, OTHER_UID)
    # Another tenant's checkout with an untracked file, which a root `git clean -fd` as that tenant would delete.
    OTHER_REPO.mkdir()
    (OTHER_REPO / "untracked.txt").write_text("otto's work in progress\n", encoding="utf-8")
    for path in (OTHER_REPO, OTHER_REPO / "untracked.txt"):
        os.chown(path, OTHER_UID, OTHER_UID)


def make_stubs() -> None:
    Path("/stubs/sbin/systemctl").write_text('#!/bin/sh\ncase "$*" in *is-active*) echo inactive; exit 3;; esac\nexit 0\n')
    for cli in ("tmux", "claude", "codex", "agy", "copilot", "hermes", "gemini", "konsole"):
        Path(f"/stubs/bin/{cli}").write_text(f'#!/bin/sh\necho "refused stub: {cli} $*" >&2\nexit 97\n')
    # Every way root could become another account records itself and refuses.
    for switch in ("sudo", "runuser", "setpriv", "su"):
        Path(f"/stubs/bin/{switch}").write_text(f'#!/bin/sh\necho "{switch} $*" >> {CALLS}\nexit 97\n')
    for place in (Path("/stubs/sbin"), Path("/stubs/bin")):
        for stub in place.iterdir():
            stub.chmod(0o755)


def inner(root: Path, work: Path, mode: str) -> dict:
    sh("ip", "link", "set", "lo", "up")
    enter_root_owned_root(work)
    root = Path("/stage") / root.relative_to(work)
    release = Path("/opt/sw")
    for part in ("scripts", "config", "examples", "skills", "docs"):
        if (root / part).exists():
            shutil.copytree(root / part, release / part, symlinks=True)
    sh("chmod", "-R", "a+rX", str(release))
    for place in ("/etc/switchyard", "/etc/systemd/system", "/etc/sudoers.d", "/etc/polkit-1/rules.d",
                  "/etc/tmpfiles.d"):
        Path(place).mkdir(parents=True, exist_ok=True)
    make_stubs()
    make_accounts()
    for directory in (HOME / "Projects", CHECKOUT, CHECKOUT / ".switchyard", PROVISION):
        directory.mkdir(mode=0o700)
        os.chown(directory, OWNER_UID, OWNER_UID)

    sys.path.insert(0, str(release))
    from scripts import team_launcher as launcher
    from scripts.ticket_board.project_provision import build_plan

    plan = build_plan(project=SLUG, project_name=NAME, owner_user=OWNER, owner_home=HOME, source_repo=release,
                      service_user="boardsvc", project_repository=CHECKOUT)
    installed = launcher.install_privileged_artifacts(plan, launcher.render_privileged_artifacts(plan))
    config_path = launcher.write_new_project_launcher_artifacts(plan, PROVISION, repository=CHECKOUT,
                                                                project_name=NAME, print_func=lambda *a, **k: None)
    registry = Path("/etc/switchyard/projects")
    registry.mkdir(exist_ok=True)
    (registry / f"{SLUG}.json").write_text(json.dumps({"schema": "switchyard.project-registry.v1", "slug": SLUG,
                                                        "name": NAME, "config_path": str(config_path)}, indent=2))
    (registry / f"{SLUG}.json").chmod(0o644)
    generated = json.loads(config_path.read_text(encoding="utf-8"))
    roles = {role["role"] for role in generated["roles"]}
    out: dict = {"installed": str(installed), "roles": sorted(roles), "owner": generated.get("run_as_user")}

    def legacy(main: str | None, app: str | None = None) -> None:
        """The tenant's configuration as a legacy one: isolation off, main a codex role naming `main`."""
        document = json.loads(json.dumps(generated))
        document["role_state_isolation"] = False
        for role in document["roles"]:
            if role["role"] == "main":
                role["cli"] = ["codex"]
                if main is not None:
                    role["run_as_user"] = main
            if role["role"] == "app" and app is not None:
                role["run_as_user"] = app
        config_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.chown(config_path, OWNER_UID, OWNER_UID)

    def load() -> object:
        try:
            return launcher.load_project_config(SLUG, config_path)
        except SystemExit as stop:
            return f"refused: {stop}"

    def command(argv: list[str]) -> dict:
        CALLS.write_text("", encoding="utf-8")
        CALLS.chmod(0o666)
        before = watched()
        done = subprocess.run([sys.executable, str(release / "scripts" / argv[0]), *argv[1:]], capture_output=True,
                              text=True, timeout=300, stdin=subprocess.DEVNULL,
                              env={"PATH": "/stubs/sbin:/stubs/bin:/usr/bin:/bin", "HOME": str(ROOT_HOME),
                                   "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"})
        return {"code": done.returncode, "said": (done.stdout + done.stderr)[-1500:], "unchanged": watched() == before,
                "calls": calls()}

    def repatriate() -> dict:
        """The upgrade's repatriation step, driven directly with the configuration the loader gave."""
        config = load()
        if isinstance(config, str):
            return {"loaded": config}
        CALLS.write_text("", encoding="utf-8")
        before = watched()
        try:
            done, problems = launcher.repatriate_role_runtime_state(config, config_path=config_path, dry_run=False,
                                                                     runner=subprocess.run, written={})
            said = f"repatriated={done!r} problems={problems!r}"
        except BaseException as exc:  # noqa: BLE001 - what it did is the point, whatever it raised
            said = f"{type(exc).__name__}: {exc}"
        copied = sorted(str(p.relative_to(HOME)) for p in (HOME / ".codex").rglob("*.jsonl")) \
            if (HOME / ".codex").exists() else []
        owners = {name: os.lstat(HOME / name).st_uid for name in copied}
        after = watched()
        changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
        return {"loaded": "yes", "said": said[-800:], "copied": copied, "owners": owners, "changed": changed}

    def decisions() -> dict:
        """The account decisions the git, pane and launch routes take, with the configuration the loader gave."""
        config = load()
        if isinstance(config, str):
            return {"loaded": config}
        from scripts import owner_git, project_worktrees

        role = launcher._role_by_name(config, "main")
        recorded: list[list[str]] = []
        git = owner_git.run_owner_correct_git(
            ["git", "-C", str(OTHER_REPO), "clean", "-fd"],
            runner=lambda args, **k: recorded.append(list(args)) or subprocess.CompletedProcess(args, 0, "", ""),
            owner_rules=project_worktrees._config_git_owner_rules(config))
        return {"loaded": "yes", "git": recorded or [git.stderr], "role_account": launcher.role_run_as_user(config, role),
                "pane_command": launcher.pane_command(config.project, role, config_path=config_path,
                                                      mode="attach-or-start", script_path=release / "scripts" / "team-launcher",
                                                      pane_state_dir=Path("/tmp/pane-state"), force_reload=False,
                                                      skip_launcher_check=True,
                                                      run_as_user=launcher.role_run_as_user(config, role))}

    # The repository the tenant points root's worktree git at: the other tenant's checkout, under a role whose
    # workdir is that tenant's home.
    def git_route(main: str) -> None:
        legacy(main)
        document = json.loads(config_path.read_text(encoding="utf-8"))
        for role in document["roles"]:
            if role["role"] == "main":
                role["workdir"] = str(OTHER_HOME)
        config_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.chown(config_path, OWNER_UID, OWNER_UID)

    for name, account in PLANTED.items():
        git_route(account)
        out[f"decisions/{name}"] = decisions()
        legacy(account)
        out[f"load/{name}"] = repr(load())[:600] if not isinstance(load(), str) else load()
        out[f"repatriate/{name}"] = repatriate()
        shutil.rmtree(HOME / ".codex", ignore_errors=True)
        if mode == "candidate":
            for entry, argv in ENTRY_POINTS.items():
                legacy(account)
                out[f"{entry}/{name}"] = command(argv)
        else:
            legacy(account)
            out[f"pane detach-role/{name}"] = command(ENTRY_POINTS["pane detach-role"])
    # Role app naming its own canonical name, which root never put in pie-roles.
    legacy(None, STRAY)
    out["load/stray"] = load() if isinstance(load(), str) else "loaded"

    def planted(role_name: str, account: str) -> object:
        legacy(None)
        document = json.loads(config_path.read_text(encoding="utf-8"))
        for role in document["roles"]:
            if role["role"] == role_name:
                role["run_as_user"] = account
                role["cli"] = ["codex"]
        config_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.chown(config_path, OWNER_UID, OWNER_UID)
        got = load()
        return got if isinstance(got, str) else "loaded"

    # Canonical names: one root put in pie-roles that is uid 0, and one with no account at all; and a role naming the
    # owner itself, which is the owner's.
    out["load/canonical-uid0"] = planted("audit", "pie-audit")
    out["load/canonical-missing"] = planted("designer", "pie-designer")
    out["load/owner-named"] = planted("main", OWNER)
    # The board's service account, named `<project>-<role>` by the operator and put in pie-roles by root's packet:
    # recorded by root's board unit, then by root's provisioning record. Refused, at the load and at an entry point.
    unit = Path("/etc/systemd/system") / f"{SLUG}-ticket-board.service"
    out["unit_present_at_start"] = unit.exists()
    saved_unit = unit.read_text(encoding="utf-8") if unit.exists() else None
    unit.write_text(f"[Service]\nUser={SERVICE}\nExecStart=/opt/sw/board --unix-socket /run/{SLUG}-ticket-board/s\n",
                    encoding="utf-8")
    unit.chmod(0o644)
    out["load/service-unit"] = planted("director", SERVICE)
    planted("director", SERVICE)
    out["start/service-unit"] = command(ENTRY_POINTS["start"])
    unit.unlink()
    plan_path = Path(installed) / "plan.json"
    plan_text = plan_path.read_text(encoding="utf-8")
    plan_path.write_text(json.dumps({**json.loads(plan_text), "service_user": SERVICE}, indent=2), encoding="utf-8")
    out["load/service-plan"] = planted("director", SERVICE)
    plan_path.write_text(plan_text, encoding="utf-8")
    # Neither record: root cannot tell which account runs the board, so not even the genuine role account passes.
    shutil.move(str(installed), str(installed) + ".away")
    legacy(GENUINE)
    out["load/service-unknown"] = load() if isinstance(load(), str) else "loaded"
    shutil.move(str(installed) + ".away", str(installed))
    if saved_unit is not None:
        unit.write_text(saved_unit, encoding="utf-8")
    # The canonical name with no account loads, and its repatriation reads nothing at the home lookup's fallback.
    shutil.rmtree(HOME / ".codex", ignore_errors=True)
    planted("designer", "pie-designer")
    out["repatriate/missing"] = repatriate()
    shutil.rmtree(HOME / ".codex", ignore_errors=True)
    # A blank role account is the owner's; the genuine role account root made converges.
    for name, account in (("blank", ""), ("genuine", GENUINE)):
        legacy(account)
        out[f"load/{name}"] = load() if isinstance(load(), str) else "loaded"
        if mode == "candidate":
            for entry, argv in ENTRY_POINTS.items():
                legacy(account)
                out[f"{entry}/{name}"] = command(argv)
    shutil.rmtree(HOME / ".codex", ignore_errors=True)
    legacy(GENUINE)
    out["repatriate/genuine"] = repatriate()
    legacy(GENUINE)
    out["repatriate/genuine-again"] = repatriate()
    # And the genuine account once root takes it out of pie-roles: then it is not one root made for this project.
    group = Path("/etc/group")
    group.write_text(group.read_text(encoding="utf-8").replace(f",{GENUINE}\n", "\n"), encoding="utf-8")
    legacy(GENUINE)
    out["load/genuine-out-of-group"] = load() if isinstance(load(), str) else "loaded"
    # And a project root made no roles group for at all: then it made no role accounts for it.
    group.write_text(group.read_text(encoding="utf-8").replace("pie-roles:", "pie-other:"), encoding="utf-8")
    legacy(GENUINE)
    out["load/no-roles-group"] = load() if isinstance(load(), str) else "loaded"
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "skills",
                              "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, mode: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd757.") as tmp:
        stage = Path(tmp)
        os.chmod(stage, 0o755)
        shutil.copy2(Path(__file__), stage / "suite.py")
        code = stage / "tree"
        for part in ("scripts", "config", "examples", "skills", "docs"):
            if (tree / part).exists():
                shutil.copytree(tree / part, code / part, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run(["chmod", "-R", "a+rX", str(stage), str(code)], check=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX",
                                                                         "SWITCHYARD_"))}
        child = subprocess.run(["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private",
                                "--pid", "--fork", "--mount-proc", "--net", sys.executable, str(stage / "suite.py"),
                                "--inner", str(code), tmp, mode], capture_output=True, text=True,
                               env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=3000, stdin=subprocess.DEVNULL)
        assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


def refusal(account: str, why: str) -> str:
    return f"role 'main' names {account!r} as its account, and {why}"


WHY = {
    "foreign": refusal(OTHER, f"the only account root makes for main of {SLUG} is {GENUINE!r}"),
    "root": refusal("root", "root does not act as its own tenant"),
    "uid0": refusal("toor", f"the only account root makes for main of {SLUG} is {GENUINE!r}"),
    "unknown": refusal("ghost", f"the only account root makes for main of {SLUG} is {GENUINE!r}"),
    "wrong-role": refusal(STRAY, f"the only account root makes for main of {SLUG} is {GENUINE!r}"),
}
TAIL = "Root acts only as the project's owner or a role account it made for this project. Nothing was changed."


def before(out: dict) -> None:
    for name in ("foreign", "root", "uid0"):
        got = out[f"repatriate/{name}"]
        label = "otto-secret" if name == "foreign" else "root-secret"
        check(got["loaded"] == "yes" and any(label in path for path in got["copied"])
              and all(uid == OWNER_UID for uid in got["owners"].values()),
              f"before, {name}: the loader accepts the planted account and the repatriation copies its session file "
              f"into the owner's home, owned by the owner: {got}")
    got = out["decisions/foreign"]
    check(got["loaded"] == "yes" and got["git"] and got["git"][0][:3] == ["sudo", "-u", OTHER]
          and got["role_account"] == OTHER and OTHER in got["pane_command"],
          f"before, foreign: root's worktree git, and the role's pane, run as the other tenant: {got}")
    detach = out["pane detach-role/root"]
    check(not detach["unchanged"] and not any(TAIL in detach["said"] for _ in [0]),
          f"before, root: detach-role as root stays root and rewrites the tenant's configuration: {detach}")
    check(out["load/service-unit"] == "loaded" and out["load/service-plan"] == "loaded",
          f"before: the board's service account named <project>-<role> is accepted as a role account: "
          f"{out['load/service-unit']} {out['load/service-plan']}")
    check(out["load/canonical-uid0"] == "loaded" and out["load/canonical-missing"] == "loaded",
          f"before: a canonical uid-0 account and a name with no account are accepted: {out['load/canonical-uid0']} "
          f"{out['load/canonical-missing']}")
    leftover = out["repatriate/missing"]
    check(any("rollout-leftover" in path for path in leftover["copied"]),
          f"before: for a role account with no account, the repatriation copies whatever is at /home/<name>: {leftover}")
    check(out["load/stray"] == "loaded" and out["load/genuine-out-of-group"] == "loaded",
          f"before: a canonical name root never put in the project's group is accepted: {out['load/stray']} "
          f"{out['load/genuine-out-of-group']}")
    foreign = out["pane detach-role/foreign"]
    check(any(line.startswith(f"sudo -u {OTHER}") or f" {OTHER} " in f" {line} " for line in foreign["calls"]),
          f"before, foreign: detach-role as root re-executes as the other tenant: {foreign}")


def candidate(out: dict) -> None:
    check({"main", "app"} <= set(out["roles"]) and out["owner"] == OWNER,
          f"the product's own configuration has roles main and app, and its owner: {out['roles']} {out['owner']}")
    for name, why in WHY.items():
        load = out[f"load/{name}"]
        check(isinstance(load, str) and load.startswith("refused: switchyard: ") and why in load and TAIL in load,
              f"load, main's account {name}: refused, naming the role, the account and why: {load}")
        check(out[f"decisions/{name}"] == {"loaded": out[f"decisions/{name}"]["loaded"]}
              and "refused" in out[f"decisions/{name}"]["loaded"],
              f"{name}: no git, pane or launch account decision is ever reached: {out[f'decisions/{name}']}")
        repatriated = out[f"repatriate/{name}"]
        check(set(repatriated) == {"loaded"} and why in repatriated["loaded"],
              f"{name}: the repatriation never gets a configuration to copy from: {repatriated}")
        for entry in ENTRY_POINTS:
            got = out[f"{entry}/{name}"]
            check(got["code"] != 0 and why in got["said"] and TAIL in got["said"] and got["unchanged"]
                  and got["calls"] == [] and "Traceback" not in got["said"],
                  f"{entry}, main's account {name}: refused as root before anything is read, copied, run or "
                  f"launched; nothing in any home or root's records changed; no account switch attempted: {got}")
    stray = out["load/stray"]
    check(isinstance(stray, str) and f"role 'app' names {STRAY!r} as its account, and it is not in pie-roles, the "
          f"group root adds {SLUG}'s role accounts to" in stray,
          f"role app naming its own canonical account, which root never put in pie-roles: refused: {stray}")
    uid0 = out["load/canonical-uid0"]
    check(isinstance(uid0, str) and "role 'audit' names 'pie-audit' as its account, and it is uid 0" in uid0,
          f"a canonical account root put in pie-roles that is uid 0 is refused: {uid0}")
    check(out["load/canonical-missing"] == "loaded",
          f"a canonical name with no account loads, so a legacy configuration naming one can still be upgraded: "
          f"{out['load/canonical-missing']}")
    missing = out["repatriate/missing"]
    check(missing["loaded"] == "yes" and missing["copied"] == [] and "problems=[]" in missing["said"].replace("()", "[]")
          and not any(path.startswith(str(LEFTOVER)) for path in missing["changed"]),
          f"and the repatriation reads nothing at /home/pie-designer, the home lookup's fallback for it: {missing}")
    check(out["load/owner-named"] == "loaded", f"a role naming the owner itself is the owner's: {out['load/owner-named']}")
    service = f"role 'director' names {SERVICE!r} as its account, and it is the account {SLUG}'s board service runs as"
    for source in ("unit", "plan"):
        got = out[f"load/service-{source}"]
        check(isinstance(got, str) and service in got and TAIL in got,
              f"the board's service account, recorded by root's {source}, is never a role's: {got}")
    started = out["start/service-unit"]
    check(started["code"] != 0 and service in started["said"] and started["unchanged"] and started["calls"] == [],
          f"and switchyard start refuses it before anything runs as it: {started}")
    unknown = out["load/service-unknown"]
    check(isinstance(unknown, str) and f"role 'main' names {GENUINE!r} as its account, and root's records cannot say "
          f"which account runs {SLUG}'s board (it has neither a provisioning record nor a board unit)" in unknown,
          f"a project whose board account root's records cannot name admits no role account: {unknown}")
    no_group = out["load/no-roles-group"]
    check(isinstance(no_group, str) and "and root has made no pie-roles group for pie, so it made no role accounts "
          "for it" in no_group, f"a project root made no roles group for has no role accounts: {no_group}")
    out_of_group = out["load/genuine-out-of-group"]
    check(isinstance(out_of_group, str) and "it is not in pie-roles" in out_of_group,
          f"and the genuine account, once root takes it out of pie-roles, is refused too: {out_of_group}")
    for name in ("blank", "genuine"):
        check(out[f"load/{name}"] == "loaded", f"{name}: root's load accepts it: {out[f'load/{name}']}")
        for entry in ENTRY_POINTS:
            got = out[f"{entry}/{name}"]
            check("as its account, and" not in got["said"] and "Traceback" not in got["said"],
                  f"{entry}, main's account {name}: not refused for its account; the command goes on to its own "
                  f"checks: {got}")
    genuine = out["repatriate/genuine"]
    legit = f"{SESSIONS}/rollout-pie-main-legit.jsonl"
    check(genuine["loaded"] == "yes" and genuine["copied"] == [legit]
          and genuine["owners"] == {legit: OWNER_UID} and "problems=[]" in genuine["said"].replace("()", "[]"),
          f"the genuine legacy repatriation converges: the role account's session file is now the owner's: {genuine}")
    again = out["repatriate/genuine-again"]
    check(again["copied"] == [legit] and again["changed"] == [str(PROVISION / f"{SLUG}.json")]
          and genuine["changed"][-1:] == [str(PROVISION / f"{SLUG}.json")],
          f"and running it again copies nothing more: only the configuration's binding, planted again, is dropped "
          f"again: {again}")


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]), default=str))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT if sys.argv[2] == "candidate" else tree_at(BEFORE, Path(tempfile.mkdtemp())),
                             sys.argv[2]), indent=1, default=str))
        return 0
    after = run(ROOT, "candidate")
    if os.environ.get("SYRD757_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd757-before.") as tmp:
            os.chmod(tmp, 0o755)
            before(run(tree_at(BEFORE, Path(tmp) / "before"), "before"))
    candidate(after)
    print(f"root_role_account_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
