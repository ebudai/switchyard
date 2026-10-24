#!/usr/bin/env python3
"""SYRD-664: root acts in the account and trees its own record names, not the ones the tenant's plan.json names.

`_plan_data_from_config` reads `<provision>/plan.json`, the tenant's own copy of
the plan, and root took three of its fields as roots: `owner_home`, the home it
provisions a GitHub identity in and installs the listener unit under;
`board_root`, where it captures and restores a release pointer; and
`owner_user`, whose record it writes on a unit install. A tenant could point all
three somewhere else.

Here, as root in a user, mount, pid and network namespace with real uids,
chrooted into a root the namespace's root owns (so root's records can be proven
root's): root's provisioning record is the product's own, the registry entry is
root's, and the tenant's configuration and plan are the product's, in the
owner's 0700 home. The tenant's plan is then rewritten to name another tenant's
home and board, and `root` or that tenant as the owner, and the real
`scripts/switchyard` runs as root for the identity phase (`upgrade`) and the unit
install (`add-role`, `set-vcs-close-role`); the release-pointer and unit
captures and restores the identity cutover rolls back with are called as root.
The other tenant's tree is byte-identical afterwards. A legacy tenant (no plan
of root's) and the untouched plan are controls. The before-run is main's code.
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
BEFORE = "b730cc6b78bbb4ffe705a2c488efed8552f68103"  # main before SYRD-664
SLUG, NAME = "pie", "Personal Inference Engine"
OWNER, OWNER_UID = "pie-agent", 1018
OTHER, OTHER_UID = "otto-agent", 1019  # another tenant's owner
HOME = Path("/home") / OWNER
CHECKOUT = HOME / "Projects" / "pie"
PROVISION = CHECKOUT / ".switchyard" / "provision"
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


def enter_root_owned_root(stage: Path) -> None:
    """Become root of a filesystem namespace root really owns.

    In a user namespace `/` and `/etc` belong to the overflow uid, so no path can
    be proven root-controlled and root's record could never name an owner. So
    the root here is a tmpfs this namespace's root owns: `/etc` and every
    directory on the way to root's records are its own, the host's `/usr` is
    bound in read-only for the interpreter, and the stage (this suite and the
    code it runs) is bound at `/stage`.
    """
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


def inner(root: Path, work: Path) -> dict:
    sh("ip", "link", "set", "lo", "up")
    enter_root_owned_root(work)
    root, work = Path("/stage") / root.relative_to(work), Path("/stage")
    release = Path("/opt/sw")
    for part in ("scripts", "config", "examples", "skills", "docs"):
        if (root / part).exists():
            shutil.copytree(root / part, release / part, symlinks=True)
    sh("chmod", "-R", "a+rX", str(release))
    for place in ("/etc/switchyard", "/etc/systemd/system", "/etc/sudoers.d", "/etc/polkit-1/rules.d",
                  "/etc/tmpfiles.d", "/usr/local/lib/switchyard"):
        if not place.startswith("/usr/"):
            Path(place).mkdir(parents=True, exist_ok=True)
    # No systemd and no provider may run: a quiet systemctl, refusing tmux and provider CLIs, first on PATH.
    Path("/stubs/sbin/systemctl").write_text('#!/bin/sh\ncase "$*" in *is-active*) echo inactive; exit 3;; esac\nexit 0\n')
    for cli in ("tmux", "claude", "codex", "agy", "copilot", "hermes", "gemini", "konsole"):
        Path(f"/stubs/bin/{cli}").write_text(f'#!/bin/sh\necho "refused stub: {cli} $*" >&2\nexit 97\n')
    for place in (Path("/stubs/sbin"), Path("/stubs/bin")):
        for stub in place.iterdir():
            stub.chmod(0o755)
    passwd, group = Path("/etc/passwd"), Path("/etc/group")
    accounts = ((OWNER, OWNER_UID, str(HOME)), (OTHER, OTHER_UID, f"/home/{OTHER}"))
    passwd.write_text("root:x:0:0::/run/root-home:/bin/sh\ntoor:x:0:0::/run/root-home:/bin/sh\nboardsvc:x:1500:1500::/nonexistent:/bin/false\n" + "".join(
        f"{name}:x:{uid}:{uid}::{home}:/bin/sh\n" for name, uid, home in accounts), encoding="utf-8")
    group.write_text("root:x:0:\nboardsvc:x:1500:\n" + "".join(f"{name}:x:{uid}:\n" for name, uid, _ in accounts),
                     encoding="utf-8")
    os.makedirs("/run/root-home", mode=0o700)
    for name, uid, home in accounts:
        os.makedirs(home, mode=0o700)
        os.chown(home, uid, uid)
        os.chmod(home, 0o700)
    for directory in (HOME / "Projects", CHECKOUT, CHECKOUT / ".switchyard", PROVISION):
        directory.mkdir(mode=0o700)
        os.chown(directory, OWNER_UID, OWNER_UID)

    sys.path.insert(0, str(release))
    from scripts import team_launcher as launcher
    from scripts.ticket_board.project_provision import build_plan

    plan = build_plan(project=SLUG, project_name=NAME, owner_user=OWNER, owner_home=HOME, source_repo=release,
                      service_user="boardsvc", project_repository=CHECKOUT)
    # Root's record, as provisioning installs it; then the tenant's configuration, as `switchyard new` writes it.
    installed = launcher.install_privileged_artifacts(plan, launcher.render_privileged_artifacts(plan))
    config_path = launcher.write_new_project_launcher_artifacts(plan, PROVISION, repository=CHECKOUT,
                                                                project_name=NAME, print_func=lambda *a, **k: None)
    registry = Path("/etc/switchyard/projects")
    registry.mkdir(exist_ok=True)
    (registry / f"{SLUG}.json").write_text(json.dumps({"schema": "switchyard.project-registry.v1", "slug": SLUG,
                                                        "name": NAME, "config_path": str(config_path)}, indent=2))
    (registry / f"{SLUG}.json").chmod(0o644)
    generated = json.loads(config_path.read_text(encoding="utf-8"))
    out: dict = {"installed": str(installed), "identity": repr(launcher.trusted_owner_identity(SLUG)),
                 "config_path_registered": str(config_path)}

    def with_owner(value: str | None) -> None:
        document = dict(generated)
        if value is None:
            document.pop("run_as_user", None)
        else:
            document["run_as_user"] = value
        config_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.chown(config_path, OWNER_UID, OWNER_UID)

    def switchyard(argv: list[str]) -> dict:
        done = subprocess.run([sys.executable, str(release / "scripts" / "switchyard"), *argv], capture_output=True,
                              text=True, timeout=300, stdin=subprocess.DEVNULL,
                              env={"PATH": "/stubs/sbin:/stubs/bin:/usr/bin:/bin", "HOME": "/run/root-home",
                                   "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"})
        return {"code": done.returncode, "said": (done.stdout + done.stderr)[-1500:]}

    # The tenant's own copy of the plan, as `switchyard new` leaves it beside its configuration.
    tenant_plan = PROVISION / "plan.json"
    if not tenant_plan.exists():
        tenant_plan.write_text((installed / "plan.json").read_text(encoding="utf-8"), encoding="utf-8")
        os.chown(tenant_plan, OWNER_UID, OWNER_UID)
    as_written = tenant_plan.read_bytes()
    with_owner(OWNER)
    # Another tenant's tree: its home, an .ssh with a key, a board root with a release pointer, a user unit.
    other = Path(f"/home/{OTHER}")
    for directory in (other / ".ssh", other / "board" / "releases" / "r1", other / ".config" / "systemd" / "user"):
        directory.mkdir(parents=True, exist_ok=True)
    (other / ".ssh" / "id_ed25519").write_text("other's key\n")
    (other / ".ssh" / "id_ed25519").chmod(0o600)
    (other / "board" / "current").symlink_to("releases/r1")
    (other / ".config" / "systemd" / "user" / f"{SLUG}-ticket-board-notify-listener.service").write_text("[Unit]\n")
    for path in (other, *other.rglob("*")):
        os.lchown(path, OTHER_UID, OTHER_UID)

    def with_plan(**fields: object) -> None:
        document = json.loads(as_written)
        document.update(fields)
        tenant_plan.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.chown(tenant_plan, OWNER_UID, OWNER_UID)

    def as_root(call) -> str:
        try:
            return f"returned {call()!r}"[:300]
        except SystemExit as stop:
            return f"refused: {stop}"
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"[:300]

    forged = {"owner_home": {"owner_home": str(other)},
              "board_root": {"board_root": str(other / "board")},
              "owner_root": {"owner_user": "root"},
              "owner_other": {"owner_user": OTHER}}
    before = snapshot(other)
    for name, fields in forged.items():
        with_plan(**fields)
        for verb, argv in (("upgrade", ["upgrade", SLUG, "--desktop-policy", "headless"]), ("add-role", ["add-role", SLUG, "qa", "--no-start"]),
                           ("set-vcs-close-role", ["set-vcs-close-role", SLUG, "director"])):
            out[f"{verb}/{name}"] = switchyard(argv)
        config = launcher.load_project_config(SLUG, config_path)
        out[f"capture_units/{name}"] = as_root(lambda: launcher.capture_installed_units(config, config_path=config_path))
        out[f"capture_pointer/{name}"] = as_root(lambda: launcher.capture_release_pointer(config, config_path=config_path))
        out[f"restore_units/{name}"] = as_root(lambda: launcher.restore_installed_units(
            config, {}, runner=subprocess.run, config_path=config_path, listener_state="inactive"))
        out[f"owner_home/{name}"] = as_root(lambda: str(launcher._tenant_owner_home(config, config_path)))
    out["foreign_unchanged"] = snapshot(other) == before
    out["foreign_after"] = {k: v for k, v in snapshot(other).items() if before.get(k) != v}

    # A plan that leaves board_root out, with the configuration's pane launcher in the other tenant's tree: root's
    # record names the board root, not the configuration.
    document = json.loads(as_written)
    document.pop("board_root", None)
    tenant_plan.write_text(json.dumps(document, indent=2), encoding="utf-8")
    os.chown(tenant_plan, OWNER_UID, OWNER_UID)
    planted = dict(generated, run_as_user=OWNER, pane_launcher=str(other / "board" / "current" / "scripts" / "team-launcher"))
    config_path.write_text(json.dumps(planted, indent=2), encoding="utf-8")
    os.chown(config_path, OWNER_UID, OWNER_UID)
    config = launcher.load_project_config(SLUG, config_path)
    out["omitted_board_root"] = as_root(lambda: str(launcher._tenant_board_root_from_config_or_plan(config, config_path)))
    # No plan at all: what the configuration implies is held to root's record just the same.
    tenant_plan.unlink()
    out["no_plan_board_root"] = as_root(lambda: str(launcher._tenant_board_root_from_config_or_plan(config, config_path)))
    # Nobody but root is asked: the owner reading its own documents is given them as they are.
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the owner's child
        os.close(reader)
        try:
            os.setgroups([OWNER_UID])
            os.setresgid(OWNER_UID, OWNER_UID, OWNER_UID)
            os.setresuid(OWNER_UID, OWNER_UID, OWNER_UID)
            said = as_root(lambda: str(launcher._tenant_board_root_from_config_or_plan(config, config_path)))
        except BaseException as exc:  # noqa: BLE001
            said = f"{type(exc).__name__}: {exc}"
        os.write(writer, said.encode())
        os._exit(0)
    os.close(writer)
    owner_said = b""
    while chunk := os.read(reader, 65536):
        owner_said += chunk
    os.close(reader)
    os.waitpid(pid, 0)
    out["owner_reads_its_own"] = owner_said.decode()
    with_owner(OWNER)
    # Root's record handed to the tenant says nothing.
    with_plan()
    os.chown(installed / "plan.json", OWNER_UID, OWNER_UID)
    out["record_not_roots"] = as_root(lambda: str(launcher._tenant_owner_home(config, config_path)))
    os.chown(installed / "plan.json", 0, 0)

    # Controls: the plan as written; and a legacy tenant, root holding no plan of its own.
    with_plan()
    out["control"] = switchyard(["upgrade", SLUG, "--dry-run", "--desktop-policy", "headless"])
    config = launcher.load_project_config(SLUG, config_path)
    out["control_owner_home"] = as_root(lambda: str(launcher._tenant_owner_home(config, config_path)))
    shutil.move(str(installed), str(installed) + ".away")
    with_plan(owner_home=str(other))
    out["legacy_owner_home"] = as_root(lambda: str(launcher._tenant_owner_home(config, config_path)))
    with_plan(board_root=str(other / "board"))
    out["legacy_board_root"] = as_root(lambda: str(launcher._tenant_board_root_from_config_or_plan(config, config_path)))
    with_plan(board_root=str(HOME / f"{SLUG}-ticketboard-live"))
    out["legacy_own_board_root"] = as_root(lambda: str(launcher._tenant_board_root_from_config_or_plan(config, config_path)))
    # The plan kept, board_root alone dropped, and the configuration's pane launcher in the other tenant's tree: what
    # the configuration implies is held to the owner's roots like a named value (Audit of 4ebc856b).
    document = json.loads(as_written)
    document.pop("board_root", None)
    tenant_plan.write_text(json.dumps(document, indent=2), encoding="utf-8")
    os.chown(tenant_plan, OWNER_UID, OWNER_UID)
    for where, launcher_path in (("foreign", other / "board" / "current" / "scripts" / "team-launcher"),
                                 ("own", HOME / f"{SLUG}-ticketboard-live" / "current" / "scripts" / "team-launcher")):
        config_path.write_text(json.dumps(dict(generated, run_as_user=OWNER, pane_launcher=str(launcher_path)), indent=2),
                               encoding="utf-8")
        os.chown(config_path, OWNER_UID, OWNER_UID)
        config = launcher.load_project_config(SLUG, config_path)
        before_kept = snapshot(other)
        out[f"kept_plan/{where}/board_root"] = as_root(
            lambda: str(launcher._tenant_board_root_from_config_or_plan(config, config_path)))
        out[f"kept_plan/{where}/capture_pointer"] = as_root(
            lambda: launcher.capture_release_pointer(config, config_path=config_path))
        out[f"kept_plan/{where}/capture_units"] = as_root(
            lambda: launcher.capture_installed_units(config, config_path=config_path))
        out[f"kept_plan/{where}/restore_units"] = as_root(lambda: launcher.restore_installed_units(
            config, {}, runner=subprocess.run, config_path=config_path, listener_state="inactive"))
        for verb, argv in (("add-role", ["add-role", SLUG, "qa", "--no-start"]),
                           ("set-vcs-close-role", ["set-vcs-close-role", SLUG, "director"])):
            out[f"kept_plan/{where}/{verb}"] = switchyard(argv)
        out[f"kept_plan/{where}/foreign_unchanged"] = snapshot(other) == before_kept
    with_owner(OWNER)
    shutil.move(str(installed) + ".away", str(installed))
    with_plan()
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "skills",
                              "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd664.") as tmp:
        stage = Path(tmp)
        os.chmod(stage, 0o755)
        shutil.copy2(Path(__file__), stage / "suite.py")
        code = stage / "tree"  # under the stage either way, which is all the namespace's root can see of the host
        for part in ("scripts", "config", "examples", "skills", "docs"):
            if (tree / part).exists():
                shutil.copytree(tree / part, code / part, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run(["chmod", "-R", "a+rX", str(stage), str(code)], check=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX",
                                                                         "SWITCHYARD_"))}
        child = subprocess.run(["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private",
                                "--pid", "--fork", "--mount-proc", "--net", sys.executable, str(stage / "suite.py"),
                                "--inner", str(code), tmp], capture_output=True, text=True,
                               env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=3000, stdin=subprocess.DEVNULL)
        assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


REFUSED = "root acts only on what its record names. Nothing was changed."
FORGED = ("owner_home", "board_root", "owner_root", "owner_other")


def before(out: dict) -> None:
    accepted = [key for key in out if key.split("/")[0] in ("capture_pointer", "owner_home") and "/" in key
                and not out[key].startswith("refused")]
    check(len(accepted) == 2 * len(FORGED),
          f"before: root takes the tenant plan's owner home and board root as its own: {accepted}")


def candidate(out: dict) -> None:
    for name in FORGED:
        for verb in ("upgrade", "add-role", "set-vcs-close-role"):
            result = out[f"{verb}/{name}"]
            check(result["code"] != 0 and REFUSED in result["said"] and "Traceback" not in result["said"],
                  f"{verb}, a plan naming another {name}: refused by root before anything is written: {result}")
        for step in ("capture_units", "capture_pointer", "restore_units", "owner_home"):
            said = out[f"{step}/{name}"]
            check(said.startswith("refused: ") and REFUSED in said,
                  f"{step} (the identity cutover's roll-back), a plan naming another {name}: refused: {said}")
    check(out["foreign_unchanged"], f"the other tenant's tree is byte-identical: {out['foreign_after']}")
    control = out["control"]
    check(REFUSED not in control["said"] and "Traceback" not in control["said"]
          and out["control_owner_home"] == f"returned {str(HOME)!r}",
          f"the plan as provisioned passes, and its owner home is root's record's: {control} {out['control_owner_home']}")
    check(out["omitted_board_root"] == f"returned {str(HOME / f'{SLUG}-ticketboard-live')!r}",
          f"a plan leaving board_root out gets root's record's, not the configuration's pane launcher: "
          f"{out['omitted_board_root']}")
    check(out["no_plan_board_root"].startswith("refused: ") and REFUSED in out["no_plan_board_root"],
          f"with no plan, a board root the configuration implies in another tenant's tree is refused: "
          f"{out['no_plan_board_root']}")
    check(out["owner_reads_its_own"] == f"returned {str(Path(f'/home/{OTHER}') / 'board')!r}",
          f"the owner, unprivileged, is given what its own documents say: {out['owner_reads_its_own']}")
    check(out["record_not_roots"].startswith("refused: ") and "cannot say whose project it is" in out["record_not_roots"],
          f"root's record handed to the tenant says nothing: {out['record_not_roots']}")
    check(out["legacy_owner_home"].startswith("refused: ") and REFUSED in out["legacy_owner_home"],
          f"a legacy tenant's plan naming another home than its owner's passwd home is refused: {out['legacy_owner_home']}")
    check(out["legacy_board_root"].startswith("refused: ") and "outside" in out["legacy_board_root"],
          f"and a board root outside the owner's own roots: {out['legacy_board_root']}")
    check(out["legacy_own_board_root"] == f"returned {str(HOME / f'{SLUG}-ticketboard-live')!r}",
          f"while one inside the owner's home is taken: {out['legacy_own_board_root']}")
    implied = "configuration's pane launcher implies board_root"
    for step in ("board_root", "capture_pointer", "capture_units", "restore_units"):
        said = out[f"kept_plan/foreign/{step}"]
        check(said.startswith("refused: ") and implied in said and "outside" in said,
              f"{step}, a kept plan without board_root and a pane launcher in another tenant's tree: refused: {said}")
    for verb in ("add-role", "set-vcs-close-role"):
        result = out[f"kept_plan/foreign/{verb}"]
        check(result["code"] != 0 and implied in result["said"] and "Traceback" not in result["said"],
              f"{verb}, the same kept plan: refused before anything is written: {result}")
    check(out["kept_plan/foreign/foreign_unchanged"], "and the other tenant's tree is byte-identical")
    check(out["kept_plan/own/board_root"] == f"returned {str(HOME / f'{SLUG}-ticketboard-live')!r}"
          and out["kept_plan/own/foreign_unchanged"],
          f"the same kept plan with the pane launcher in the owner's own home takes it: {out['kept_plan/own/board_root']}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT), indent=1, default=str))
        return 0
    after = run(ROOT)
    if os.environ.get("SYRD664_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd664-before.") as tmp:
            os.chmod(tmp, 0o755)
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    print(f"root_plan_identity_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
