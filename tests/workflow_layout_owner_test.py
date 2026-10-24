#!/usr/bin/env python3
"""SYRD-657: a declared-workflow tenant's Konsole layout is the tenant's, readable by its first ordinary start.

Fresh declared-workflow provisioning on PIE left `pie-konsole-layout.json`
`root:root` 0600 while the configuration and the desktop policy were
`pie-agent`'s. The privileged paths read through descriptors and never
noticed; the first start through the tenant-control bridge, dropped to
`pie-agent`, crashed on `PermissionError` opening the layout.
`write_new_project_launcher_artifacts` rewrote the layout for the workflow as a
root record and nothing handed it over; `upgrade_generated_project_config`
could not heal it, because it read the layout first.

Here, as root in a user and mount namespace with real uids: the owner's home
and provision directory on a tmpfs, `/etc/passwd` naming the accounts. The real
artifact generation runs as root with a declared workflow; the configuration
and the layout are then read as the owner, in a child that has become it.
Then historical layouts -- root's 0600 record, a link to somebody else's file,
a root file that is not a generated layout -- go through the real
`upgrade_generated_project_layout` as root, dry run and apply, and are read as
the owner again. The before-run is main's code, from a git archive.
"""

from __future__ import annotations

import io
import json
import shutil
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "478a516f63e88e20ce1aaf82885e8700c405b26d"  # main before SYRD-657
OWNER, OWNER_UID = "pie-agent", 1018
OTHER_UID = 1019  # a sibling account's uid, mapped in the namespace
HOME = Path("/home") / OWNER
PROJECT = HOME / "Projects" / "pie"
PROVISION = PROJECT / ".switchyard" / "provision"
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def sh(*argv: str) -> None:
    subprocess.run(list(argv), check=True, capture_output=True)


def shape(path: Path) -> list | None:
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(status.st_mode):
        return ["link", os.readlink(path)]
    return [status.st_uid, f"{stat.S_IMODE(status.st_mode):04o}", stat.S_ISREG(status.st_mode)]


def as_owner(paths: list[Path]) -> dict:
    """What the owner itself can read of each file: parsed JSON's type, or the error."""
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the owner's child
        os.close(reader)
        found = {}
        try:
            os.setgroups([OWNER_UID])
            os.setresgid(OWNER_UID, OWNER_UID, OWNER_UID)
            os.setresuid(OWNER_UID, OWNER_UID, OWNER_UID)
            for path in paths:
                try:
                    found[path.name] = type(json.loads(path.read_text(encoding="utf-8"))).__name__
                except OSError as exc:
                    found[path.name] = f"{type(exc).__name__}: {exc.strerror}"
        except BaseException as exc:  # noqa: BLE001
            found["child"] = repr(exc)
        os.write(writer, json.dumps(found).encode())
        os._exit(0)
    os.close(writer)
    data = b""
    while chunk := os.read(reader, 65536):
        data += chunk
    os.close(reader)
    os.waitpid(pid, 0)
    return json.loads(data.decode())


HUMAN, HUMAN_UID = "operator1", 1000


def bridge_start(release: Path, config_path: Path, *, staged: list[str]) -> dict:
    """`switchyard-tenant-control pie start` by the recorded human, the real bridge, as far as this host lets it go.

    Only the bridge's proof that its launcher path is root's alone stands in: inside a user namespace `/` is not
    root's, so no path can pass it. The grant, the caller from SUDO_UID, the owner drop, the environment and the
    exec of the owner's own `switchyard pie` are the bridge's. The owner's start then reads its configuration and
    layout and reaches the first-run sign-in, where the refusing provider stubs stop it.
    """
    subprocess.run(["sh", "-c", "set -e" + chr(10) + chr(10).join(c.replace("sudo ", "") for c in staged)],
                   check=True, capture_output=True)
    Path("/etc/switchyard/projects").mkdir()
    Path("/etc/switchyard/projects/pie.json").write_text(json.dumps(
        {"schema": "switchyard.project-registry.v1", "slug": "pie", "name": "pie", "config_path": str(config_path)}))
    grant = Path("/usr/local/lib/switchyard/pie")
    grant.mkdir(exist_ok=True)
    (grant / "control-grant.json").write_text(json.dumps(
        {"project": "pie", "owner": OWNER, "authorized_user": HUMAN, "launcher": str(release / "scripts/switchyard")}))
    driver = Path("/run/bridge.py")
    driver.write_text(
        "import importlib.machinery, importlib.util, sys\nfrom pathlib import Path\n"
        f"loader = importlib.machinery.SourceFileLoader('bridge', '{release}/scripts/switchyard-tenant-control')\n"
        "bridge = importlib.util.module_from_spec(importlib.util.spec_from_loader('bridge', loader))\n"
        "loader.exec_module(bridge)\n"
        "bridge.resolve_root_controlled = lambda declared, depth=0: Path(declared)\n"
        "raise SystemExit(bridge.main(sys.argv[1:]))\n")
    out_path, err_path = Path("/run/bridge.out"), Path("/run/bridge.err")
    with out_path.open("w") as out, err_path.open("w") as err:
        proc = subprocess.Popen([sys.executable, str(driver), "pie", "start"], stdout=out, stderr=err,
                                stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "SUDO_UID": str(HUMAN_UID),
                                                               "SUDO_USER": HUMAN, "LANG": "C.UTF-8"})
        try:
            code = proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = "timeout"
    said = out_path.read_text() + err_path.read_text()
    return {"code": code, "requires_root": "command requires root" in said, "permission": "PermissionError" in said,
            "signin": "will now run in this terminal as pie-agent" in said, "refused_stub": "refused stub:" in said,
            "tail": said[-600:]}


def as_owner_call(release: Path, config_path: Path) -> dict:
    """The owner's launch reading its layout first, as `launch_project` does before anything starts."""
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the owner's child
        os.close(reader)
        found: dict = {}
        try:
            os.setgroups([OWNER_UID])
            os.setresgid(OWNER_UID, OWNER_UID, OWNER_UID)
            os.setresuid(OWNER_UID, OWNER_UID, OWNER_UID)
            from scripts import team_launcher as launcher

            config = launcher.load_project_config("pie", config_path)
            found["message"] = launcher.upgrade_generated_project_layout(config, config_path=config_path,
                                                                         runner=subprocess.run).message
        except BaseException as exc:  # noqa: BLE001
            found["error"] = f"{type(exc).__name__}: {exc}"
        os.write(writer, json.dumps(found).encode())
        os._exit(0)
    os.close(writer)
    data = b""
    while chunk := os.read(reader, 65536):
        data += chunk
    os.close(reader)
    os.waitpid(pid, 0)
    return json.loads(data.decode())


def declared_document(root: Path, project: str) -> dict:
    raw = (root / "examples" / "workflows" / "inspection.json").read_text(encoding="utf-8")
    source = json.loads(raw)["project"]
    return json.loads(raw.replace(f'"{source}-', f'"{project}-').replace(f'"project": "{source}"', f'"project": "{project}"'))


def inner(root: Path, work: Path) -> dict:
    sys.path.insert(0, str(root))
    # A release root only root controls, from this tree; then everything a start touches, private to this namespace.
    sh("mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", "/opt")
    release = Path("/opt/sw")
    for part in ("scripts", "config", "examples", "skills", "docs"):
        if (root / part).exists():
            shutil.copytree(root / part, release / part, symlinks=True)
    sh("chmod", "-R", "a+rX", str(release))  # a release is world-readable; a worktree's umask may not be
    for place in ("/run", "/home", "/usr/local/lib/switchyard", "/etc/switchyard", "/var/tmp", "/usr/share/polkit-1",
                  "/usr/local/sbin", "/usr/local/bin"):
        sh("mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", place)
    # No systemd as PID 1 and no provider may run: a recording systemctl, and refusing provider CLIs.
    Path("/usr/local/sbin/systemctl").write_text('#!/bin/sh\ncase "$*" in *is-active*) echo active;; esac\nexit 0\n')
    for cli in ("claude", "codex", "agy", "copilot", "hermes", "gemini"):
        Path(f"/usr/local/bin/{cli}").write_text(f'#!/bin/sh\necho "refused stub: {cli} $*" >&2\nexit 97\n')
    for stub in Path("/usr/local/sbin").iterdir():
        stub.chmod(0o755)
    for stub in Path("/usr/local/bin").iterdir():
        stub.chmod(0o755)
    passwd, group = work / "passwd", work / "group"
    passwd.write_text(f"root:x:0:0::/root:/bin/sh\n{OWNER}:x:{OWNER_UID}:{OWNER_UID}::{HOME}:/bin/sh\n"
                      f"{HUMAN}:x:{HUMAN_UID}:{HUMAN_UID}::/home/{HUMAN}:/bin/sh\n", encoding="utf-8")
    group.write_text(f"root:x:0:\n{OWNER}:x:{OWNER_UID}:\n{HUMAN}:x:{HUMAN_UID}:\n", encoding="utf-8")
    sh("mount", "--bind", str(passwd), "/etc/passwd")
    sh("mount", "--bind", str(group), "/etc/group")
    for directory in (HOME, HOME / "Projects", PROJECT, PROJECT / ".switchyard", PROVISION):
        directory.mkdir(mode=0o755)
        os.chown(directory, OWNER_UID, OWNER_UID)

    sys.path.remove(str(root))
    sys.path.insert(0, str(release))
    from scripts import team_launcher as launcher
    from scripts.ticket_board import project_provision as provision
    from scripts.ticket_board.provision_role_tooling import role_tooling_staging_commands

    out: dict = {"euid": os.geteuid()}
    # As `switchyard new` runs it: the desktop policy chosen first, then the artifacts, then the desktop step.
    policy = PROVISION / "desktop-policy.json"
    policy.write_text(json.dumps({"mode": "headless", "project": "pie", "tenant_user": OWNER}), encoding="utf-8")
    os.chown(policy, OWNER_UID, OWNER_UID)
    os.chmod(policy, 0o600)
    plan = provision.build_plan(project="pie", owner_user=OWNER, owner_home=HOME, workflow=declared_document(root, "pie"))
    config_path = launcher.write_new_project_launcher_artifacts(plan, PROVISION, repository=PROJECT, project_name="pie",
                                                               print_func=lambda *_a: None)
    launcher.prepare_project_desktop(launcher.load_project_config("pie", config_path), runner=subprocess.run)
    layout = PROVISION / "pie-konsole-layout.json"
    out["fresh"] = {"config": shape(config_path), "layout": shape(layout), "as_owner": as_owner([config_path, layout])}
    out["bridge_start"] = bridge_start(release, config_path, staged=role_tooling_staging_commands("pie", str(release)))
    out["launch_layout_read"] = as_owner_call(release, config_path)
    config = launcher.load_project_config("pie", config_path)
    out["layout_is_config_layout"] = str(config.layout) == str(layout)
    generated = layout.read_bytes()

    def upgrade(dry_run: bool) -> str:
        try:
            return launcher.upgrade_generated_project_layout(config, config_path=config_path, dry_run=dry_run,
                                                             runner=subprocess.run).message
        except SystemExit as stop:
            return f"SystemExit: {stop}"

    # A historical layout: the root record 0600 earlier releases left.
    layout.write_bytes(generated)
    os.chown(layout, 0, 0)
    os.chmod(layout, 0o600)
    out["historical"] = {"before": shape(layout), "as_owner_before": as_owner([layout]),
                         "dry_run": upgrade(True), "after_dry_run": shape(layout),
                         "apply": upgrade(False), "after": shape(layout), "as_owner": as_owner([layout]),
                         "again": upgrade(False), "content_kept": layout.read_bytes() == generated}
    # A link where the layout goes, to a file of root's elsewhere.
    foreign = Path("/run/foreign.json")
    foreign.write_bytes(generated)
    os.chmod(foreign, 0o600)
    layout.unlink()
    layout.symlink_to(foreign)
    out["link"] = {"message": upgrade(False), "layout": shape(layout), "foreign": shape(foreign)}
    # A root file at the name that is not a layout this release generates.
    layout.unlink()
    layout.write_text('{"not": "a generated layout"}\n', encoding="utf-8")
    os.chown(layout, 0, 0)
    os.chmod(layout, 0o600)
    out["not_generated"] = {"message": upgrade(False), "layout": shape(layout)}
    # A generated layout another account owns, at the name: its contents do not show root made it (Audit of 88c86394).
    layout.unlink()
    layout.write_bytes(generated)
    os.chown(layout, OTHER_UID, OTHER_UID)
    os.chmod(layout, 0o600)
    out["foreign_owned"] = {"dry_run": upgrade(True), "after_dry_run": shape(layout), "apply": upgrade(False),
                            "after": shape(layout), "content_kept": layout.read_bytes() == generated}
    # The claim itself, judging every content generated: ownership alone decides (main has no claim to call).
    claim = getattr(launcher, "claim_owner_file", None)
    if claim is not None:
        def claimed() -> list:
            return [claim(OWNER, layout, accept=lambda raw: True, mode=0o644, apply=True)[1], shape(layout)]

        out["claim_foreign"] = claimed()
        os.chown(layout, OWNER_UID, OWNER_UID)
        os.chmod(layout, 0o640)
        out["claim_owners"] = claimed()
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests", "skills", "docs"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path) -> dict:
    """The scenario on a copy of `root` outside /home, which the namespace covers with the owner's own home."""
    import shutil

    with tempfile.TemporaryDirectory(prefix="syrd657.") as tmp:
        tree = Path(tmp) / "tree"
        for part in ("scripts", "config", "examples", "tests", "skills", "docs"):
            shutil.copytree(root / part, tree / part, symlinks=True)
        shutil.copy(Path(__file__).resolve(), tree / "tests" / Path(__file__).name)  # this scenario, on either tree
        work = Path(tmp) / "work"
        work.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
        child = subprocess.run(["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private",
                                "--pid", "--fork", "--mount-proc", "--net", sys.executable, str(tree / "tests" / Path(__file__).name), "--inner", str(tree),
                                str(work)],
                               capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=600)
        assert child.returncode == 0, child.stderr[-3000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


def before(out: dict) -> None:
    fresh = out["fresh"]
    check(fresh["layout"][:2] == [0, "0600"] and fresh["as_owner"]["pie-konsole-layout.json"].startswith("PermissionError"),
          f"before: fresh workflow provisioning leaves the layout root's, 0600, and the owner cannot open it: {fresh}")
    bridge = out["bridge_start"]
    check(bridge["code"] != 0 and (bridge["requires_root"] or bridge["permission"]) and not bridge["signin"],
          f"before: the owner's start through the bridge cannot read what provisioning left it, and stops at once: {bridge}")
    historical = out["historical"]
    check(historical["after"][:2] == [0, "0600"] and historical["as_owner"]["pie-konsole-layout.json"].startswith("PermissionError"),
          f"before: an upgrade leaves a historical root layout root's, and the owner still cannot open it: {historical}")


def candidate(out: dict) -> None:
    fresh = out["fresh"]
    check(out["euid"] == 0 and out["layout_is_config_layout"],
          f"the artifacts are generated as root, and the configuration names this layout: {out['euid']}")
    check(fresh["layout"] == [OWNER_UID, "0644", True] and fresh["config"] == [OWNER_UID, "0600", True]
          and fresh["as_owner"] == {"pie.json": "dict", "pie-konsole-layout.json": "dict"},
          f"fresh workflow provisioning, desktop policy included, leaves the layout and the configuration regular files "
          f"of the owner's -- 0644 and 0600 -- and the owner reads both: {fresh}")
    bridge = out["bridge_start"]
    check(not bridge["requires_root"] and not bridge["permission"] and bridge["signin"] and bridge["refused_stub"]
          and "not starting any role yet" in bridge["tail"],
          f"the owner's start through the real tenant-control bridge reads its configuration and layout and goes on to "
          f"the first-run sign-in, where the refusing provider stubs stop it: {bridge}")
    check(out["launch_layout_read"] == {"message": "switchyard: pie layout template is already current"},
          f"the owner's launch reads its layout first, as launch_project does, and finds it current: "
          f"{out['launch_layout_read']}")
    historical = out["historical"]
    check(historical["before"] == [0, "0600", True] and historical["as_owner_before"]["pie-konsole-layout.json"].startswith("PermissionError")
          and "is root's and can be handed to pie-agent" in historical["dry_run"] and historical["after_dry_run"] == historical["before"],
          f"an upgrade's dry run names a historical root layout and changes nothing: {historical}")
    check(f"handed generated layout template {PROVISION / 'pie-konsole-layout.json'} to {OWNER}, 0644" in historical["apply"]
          and historical["after"] == [OWNER_UID, "0644", True] and historical["content_kept"]
          and historical["as_owner"] == {"pie-konsole-layout.json": "dict"},
          f"the upgrade hands it to the owner, 0644, contents unchanged, and the owner reads it: {historical}")
    check("already current" in historical["again"], f"a second upgrade changes nothing: {historical['again']}")
    link = out["link"]
    check(("leaving it unchanged" in link["message"] or "symbolic link" in link["message"])
          and "handed" not in link["message"] and link["layout"][0] == "link" and link["foreign"] == [0, "0600", True],
          f"a link at the layout is left alone, never followed into, and what it names is untouched: {link}")
    foreign = out["foreign_owned"]
    check(foreign["after_dry_run"] == [OTHER_UID, "0600", True] and foreign["after"] == [OTHER_UID, "0600", True]
          and foreign["content_kept"] and "handed" not in foreign["dry_run"] + foreign["apply"]
          and "can be handed" not in foreign["dry_run"],
          f"a generated layout another account owns is neither named for handover nor handed, dry run or apply: "
          f"its owner, mode and contents are unchanged: {foreign}")
    check(out["claim_foreign"] == ["it is uid 1019's, not root's, so root leaves it", [OTHER_UID, "0600", True]],
          f"the claim leaves another account's file alone even when its contents are taken: {out['claim_foreign']}")
    check(out["claim_owners"] == ["", [OWNER_UID, "0640", True]],
          f"the claim answers that the owner's own file is already the owner's, and leaves its mode: {out['claim_owners']}")
    other = out["not_generated"]
    check("leaving it unchanged" in other["message"] and "handed" not in other["message"]
          and other["layout"] == [0, "0600", True],
          f"a root file at the name that is not a generated layout is left as it is, and said so: {other}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT), indent=1, default=str))
        return 0
    after = run(ROOT)
    if os.environ.get("SYRD657_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd657-before.") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    print(f"workflow_layout_owner_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
