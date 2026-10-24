#!/usr/bin/env python3
"""SYRD-663: root acts for a tenant only as the owner root's own record names.

Every root branch SYRD-608..614 added -- a write by descriptor as the owner, a
handover, a bounded chown -- is chosen by `owner_writes.root_writes_for(owner)`,
and `owner` was the tenant configuration's own `run_as_user`. A blank value, or
`root`, made the owner root, so each of those branches took its by-path one as
root; another tenant's account would have aimed them at that tenant's roots.
Root's own record of the owner was never asked.

Here, as root in a user, mount, pid and network namespace with real uids: root's
provisioning record is the product's own (`render_privileged_artifacts` and
`install_privileged_artifacts`, under `/etc/switchyard/provision`), the
registry entry is root's, and the tenant's configuration is the one the product
generates, in the owner's 0700 home. The real `scripts/switchyard` then runs as
root for each entry point -- `upgrade`, `start`, `stop`, `add-role`,
`set-vcs-close-role` -- with the configuration's `run_as_user` blank, `root`,
and another tenant's account, and once correct. Every refused run is shown to
leave the owner's tree and root's records exactly as they were. A configuration
changed between two loads is refused at the second, and a project root holds no
record for is refused before its configuration is read. The before-run is
main's code, from a git archive.
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
BEFORE = "62c8eef30ef786f233b360fa38f55c0de06e70f3"  # main before SYRD-663
SLUG, NAME = "pie", "Personal Inference Engine"
OWNER, OWNER_UID = "pie-agent", 1018
OTHER, OTHER_UID = "otto-agent", 1019  # another tenant's owner
HOME = Path("/home") / OWNER
CHECKOUT = HOME / "Projects" / "pie"
PROVISION = CHECKOUT / ".switchyard" / "provision"
ENTRY_POINTS = {
    "upgrade": ["upgrade", SLUG],
    "start": ["start", SLUG],
    "stop": ["stop", SLUG],
    "add-role": ["add-role", SLUG, "qa", "--no-start"],
    "set-vcs-close-role": ["set-vcs-close-role", SLUG, "director"],
}
VALUES = {"blank": "", "root": "root", "uid0": "toor", "foreign": OTHER}  # toor: another name for uid 0
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

    for value_name, value in (*VALUES.items(), ("absent", None)):
        for entry, argv in ENTRY_POINTS.items():
            with_owner(value)
            before = snapshot(HOME, Path("/etc/switchyard"))
            result = switchyard(argv)
            result["unchanged"] = snapshot(HOME, Path("/etc/switchyard")) == before
            out[f"{entry}/{value_name}"] = result
    with_owner(OWNER)
    out["control"] = switchyard(["upgrade", SLUG, "--dry-run"])

    # Between two loads in one command: the second load refuses what the first accepted.
    loads = {}
    try:
        loads["first"] = launcher.load_project_config(SLUG, config_path).run_as_user
    except SystemExit as stop:
        loads["first"] = f"refused: {stop}"
    with_owner(OTHER)
    try:
        loads["second"] = launcher.load_project_config(SLUG, config_path).run_as_user
    except SystemExit as stop:
        loads["second"] = f"refused: {stop}"
    out["loads"] = loads

    # Nobody but root is asked: the owner loads its own configuration, run_as_user blank and all, as before. Without a
    # control repository, which the loader requires a named account for whoever loads it.
    unasked = {key: value for key, value in generated.items() if key not in ("control_repository", "worktree_base")}
    config_path.write_text(json.dumps({**unasked, "run_as_user": ""}, indent=2), encoding="utf-8")
    os.chown(config_path, OWNER_UID, OWNER_UID)
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the owner's child
        os.close(reader)
        try:
            os.setgroups([OWNER_UID])
            os.setresgid(OWNER_UID, OWNER_UID, OWNER_UID)
            os.setresuid(OWNER_UID, OWNER_UID, OWNER_UID)
            said = f"loaded as {launcher.load_project_config(SLUG, config_path).project}"
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
    out["owner_loads_its_own"] = owner_said.decode()
    # Root's record must be root's: one the tenant was handed is no record.
    with_owner(OWNER)
    baseline = installed / "plan.json"
    os.chown(baseline, OWNER_UID, OWNER_UID)
    out["record_not_roots"] = switchyard(["upgrade", SLUG])
    try:  # and as root loads it outside any command entry
        out["record_not_roots_load"] = f"loaded as {launcher.load_project_config(SLUG, config_path).run_as_user}"
    except SystemExit as stop:
        out["record_not_roots_load"] = str(stop)
    os.chown(baseline, 0, 0)

    # A legacy tenant: registered, but root holds no plan for it. Root's registry entry names where its configuration
    # is, and the owner is the account whose home holds it.
    shutil.move(str(installed), str(installed) + ".away")
    # The catalogued deploy action starts its own root process, which has no command entry of its own: the argv the
    # privileged front door builds, run against this release (Audit of f905e90f).
    from scripts.ticket_board import privileged_operations

    action = privileged_operations._deploy_release({"project": SLUG, "commit": "c" * 40})
    out["deploy_argv"] = action[1:]
    for value_name, value in (*VALUES.items(), ("owner", OWNER)):
        with_owner(value)
        before = snapshot(HOME, Path("/etc/switchyard"))
        result = switchyard(action[1:])
        result["unchanged"] = snapshot(HOME, Path("/etc/switchyard")) == before
        out[f"deploy/{value_name}"] = result
    # Root's entry names exactly this file, but no account owns its project directory: a record of this tenant that
    # names nobody, so whatever the configuration names is refused -- the owner and another tenant's account included
    # (Audit of 9bcafe2f).
    os.chown(CHECKOUT, 0, 0)
    for value_name, value in (*VALUES.items(), ("owner", OWNER)):
        with_owner(value)
        before = snapshot(HOME, Path("/etc/switchyard"))
        result = switchyard(action[1:])
        result["unchanged"] = snapshot(HOME, Path("/etc/switchyard")) == before
        out[f"deploy_unowned_home/{value_name}"] = result
    os.chown(CHECKOUT, OWNER_UID, OWNER_UID)
    with_owner(OWNER)
    out["legacy_owner"] = switchyard(["upgrade", SLUG, "--dry-run"])
    with_owner(OTHER)
    before = snapshot(HOME, Path("/etc/switchyard"))
    out["legacy_foreign"] = switchyard(["upgrade", SLUG])
    out["legacy_foreign"]["unchanged"] = snapshot(HOME, Path("/etc/switchyard")) == before
    with_owner(OWNER)
    # A project directory root registered outside the owner's home, the owner's own (`new --project-path`, SYRD-607):
    # its owner is the account that owns it, as SYRD-608 trusts it.
    outside = Path("/srv/pie")
    for directory in (outside.parent, outside, outside / ".switchyard", outside / ".switchyard" / "provision"):
        directory.mkdir(exist_ok=True)
    for directory in (outside, outside / ".switchyard", outside / ".switchyard" / "provision"):
        os.chown(directory, OWNER_UID, OWNER_UID)
    moved = outside / ".switchyard" / "provision" / f"{SLUG}.json"
    shutil.copy2(config_path, moved)
    registered = (registry / f"{SLUG}.json").read_text(encoding="utf-8")
    (registry / f"{SLUG}.json").write_text(registered.replace(str(config_path), str(moved)), encoding="utf-8")
    for value_name, value in (("owner", OWNER), ("foreign", OTHER)):
        document = json.loads(moved.read_text(encoding="utf-8"))
        moved.write_text(json.dumps({**document, "run_as_user": value}), encoding="utf-8")
        os.chown(moved, OWNER_UID, OWNER_UID)
        out[f"outside_home/{value_name}"] = switchyard(action[1:])
    # A link where the registered project directory is, however its owner is set, is no project directory.
    linked = Path("/srv/pie-link")
    linked.symlink_to(outside)
    os.lchown(linked, OWNER_UID, OWNER_UID)
    (registry / f"{SLUG}.json").write_text(registered.replace(str(config_path), str(linked / ".switchyard" / "provision"
                                                                                         / f"{SLUG}.json")), encoding="utf-8")
    document = json.loads(moved.read_text(encoding="utf-8"))
    moved.write_text(json.dumps({**document, "run_as_user": OWNER}), encoding="utf-8")
    out["outside_home/link"] = switchyard(["upgrade", SLUG])  # at the entry, which asks before anything is read
    linked.unlink()
    (registry / f"{SLUG}.json").write_text(registered, encoding="utf-8")
    # A project directory the kernel says is root's names nobody.
    os.chown(CHECKOUT, 0, 0)
    out["legacy_home_not_owners"] = switchyard(["upgrade", SLUG])
    os.chown(CHECKOUT, OWNER_UID, OWNER_UID)
    # A registry entry the tenant could have written is no record.
    entry = registry / f"{SLUG}.json"
    os.chown(entry, OWNER_UID, OWNER_UID)
    out["legacy_entry_not_roots"] = switchyard(["upgrade", SLUG])
    os.chown(entry, 0, 0)
    # A configuration other than the one root registered is not what that record speaks for: one under the same slug in
    # the release's own configuration directory, which the entry finds first.
    stray = release / "config" / "team-launcher" / f"{SLUG}.json"
    stray.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_path, stray)
    out["legacy_other_path"] = switchyard(["upgrade", SLUG])
    out["legacy_other_path"]["stray"] = str(stray)
    # Loaded outside any command, that copy is not refused for a record that is not about it -- a fixture under a
    # live tenant's slug is not checked against the live tenant's registry entry.
    try:
        out["legacy_other_path_load"] = f"loaded as {launcher.load_project_config(SLUG, stray).run_as_user}"
    except SystemExit as stop:
        out["legacy_other_path_load"] = str(stop)
    stray.unlink()
    shutil.move(str(installed) + ".away", str(installed))
    with_owner(OWNER)
    # A project root holds no record of at all, found in the release's own configuration directory: refused before
    # its configuration is read.
    unrecorded = dict(generated, project="qux")
    (release / "config" / "team-launcher").mkdir(parents=True, exist_ok=True)
    (release / "config" / "team-launcher" / "qux.json").write_text(json.dumps(unrecorded), encoding="utf-8")
    out["no_record"] = switchyard(["upgrade", "qux"])
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "skills",
                              "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd663.") as tmp:
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


REFUSED = {
    "blank": f"{SLUG}'s configuration names no run_as_user, so it does not say whose project this is",
    "absent": f"{SLUG}'s configuration names no run_as_user, so it does not say whose project this is",
    "root": f"{SLUG}'s configuration names 'root' as its owner; root does not act as its own tenant",
    "uid0": f"{SLUG}'s configuration names 'toor' as its owner, and root provisioned it for {OWNER!r}",
    "foreign": f"{SLUG}'s configuration names {OTHER!r} as its owner, and root provisioned it for {OWNER!r}",
}


def before_deploy(out: dict) -> None:
    accepted = [name for name in VALUES if not any(text in out[f"deploy/{name}"]["said"] for text in REFUSED.values())]
    check(accepted == list(VALUES),
          f"before: the catalogued deploy's root process accepts a legacy tenant naming no owner, root, or another: {accepted}")


def before(out: dict) -> None:
    before_deploy(out)
    accepted = [key for key in out if key.split("/")[0] in ENTRY_POINTS
                and not any(text in out[key]["said"] for text in REFUSED.values())]
    check(len(accepted) == len(ENTRY_POINTS) * len(REFUSED),
          f"before: no entry point refuses a configuration naming no owner, root, or another tenant: {accepted}")


def candidate(out: dict) -> None:
    check("problems=()" in out["identity"] and f"owner_user='{OWNER}'" in out["identity"],
          f"root's own record names the owner, and the host agrees: {out['identity']}")
    for value_name, refusal in REFUSED.items():
        for entry in ENTRY_POINTS:
            result = out[f"{entry}/{value_name}"]
            check(result["code"] != 0 and refusal in result["said"] and "Nothing was changed." in result["said"]
                  and "Traceback" not in result["said"] and result["unchanged"],
                  f"{entry}, run_as_user {value_name}: refused by root before anything is written, and nothing in the "
                  f"owner's tree or root's records changed: {result}")
    control = out["control"]
    check(not any(text in control["said"] for text in REFUSED.values()) and "provisioning record" not in control["said"]
          and "Traceback" not in control["said"] and f"{SLUG} has no desktop policy, and every role launch needs one"
          in control["said"],
          f"the owner root's record names passes, and the upgrade's dry run goes on to its own checks: {control}")
    loads = out["loads"]
    check(loads["first"] == OWNER and loads["second"].startswith("refused: ") and REFUSED["foreign"] in loads["second"],
          f"a configuration changed between two loads in one command is refused at the second: {loads}")
    check(out["deploy_argv"] == ["deploy-release", SLUG, "--commit", "c" * 40],
          f"the deploy action's own argv is what runs: {out['deploy_argv']}")
    for value_name in VALUES:
        result = out[f"deploy/{value_name}"]
        check(result["code"] != 0 and REFUSED[value_name] in result["said"] and "Nothing was changed." in result["said"]
              and result["unchanged"] and "Traceback" not in result["said"],
              f"deploy, legacy tenant, run_as_user {value_name}: the catalogued action's own root process refuses before "
              f"any deployment step, and nothing changed: {result}")
    for value_name in (*VALUES, "owner"):
        unowned = out[f"deploy_unowned_home/{value_name}"]
        expected = REFUSED[value_name] if value_name in ("blank", "root") else (
            f"no account owns {CHECKOUT}, the project directory of the configuration root registered for {SLUG}")
        check(unowned["code"] != 0 and expected in unowned["said"] and "Nothing was changed." in unowned["said"]
              and unowned["unchanged"],
              f"deploy, a registered configuration in a home nobody owns, run_as_user {value_name}: refused before any "
              f"deployment step, nothing changed: {unowned}")
    outside = out["outside_home/owner"]
    check(not any(text in outside["said"] for text in REFUSED.values()) and "no account owns" not in outside["said"]
          and f"{SLUG} was prepared for no recorded release" in outside["said"],
          f"a registered project outside the owner's home, the owner's own, names that owner: {outside}")
    check(REFUSED["foreign"] in out["outside_home/foreign"]["said"],
          f"and another tenant's owner is refused there: {out['outside_home/foreign']}")
    link = out["outside_home/link"]
    check(link["code"] != 0 and "no account owns /srv/pie-link, the project directory" in link["said"],
          f"a link at the registered project directory, owned by the owner, names nobody, before anything is read: {link}")
    owner = out["deploy/owner"]
    check(not any(text in owner["said"] for text in REFUSED.values()) and "Traceback" not in owner["said"]
          and f"{SLUG} was prepared for no recorded release, not {'c' * 40}" in owner["said"],
          f"and the owner root's records name goes on to the deploy's own pin check: {owner}")
    legacy = out["legacy_owner"]
    check(not any(text in legacy["said"] for text in REFUSED.values()) and "Traceback" not in legacy["said"]
          and f"{SLUG} has no desktop policy, and every role launch needs one" in legacy["said"],
          f"a tenant root holds no plan for is owned by the account whose home holds its registered configuration, "
          f"and that owner passes: {legacy}")
    foreign = out["legacy_foreign"]
    check(foreign["code"] != 0 and REFUSED["foreign"] in foreign["said"] and foreign["unchanged"],
          f"and another tenant's owner is refused there too, before anything is written: {foreign}")
    check(out["owner_loads_its_own"] == "loaded as pie",
          f"the owner loads its own configuration as before, whatever it names: {out['owner_loads_its_own']}")
    forged = out["record_not_roots"]
    check(forged["code"] != 0 and f"root's provisioning record for {SLUG} cannot say whose project it is:" in forged["said"],
          f"a provisioning record root does not own says nothing about the owner: {forged}")
    check(f"root's provisioning record for {SLUG} cannot say whose project it is:" in out["record_not_roots_load"],
          f"nor to a load outside any command: {out['record_not_roots_load']}")
    home = out["legacy_home_not_owners"]
    check(home["code"] != 0 and f"no account owns {CHECKOUT}, the project directory" in home["said"],
          f"a project directory the kernel says is root's names no owner: {home}")
    entry = out["legacy_entry_not_roots"]
    check(entry["code"] != 0 and f"root's registry entry for {SLUG} cannot say whose project it is:" in entry["said"],
          f"a registry entry root does not own is no record: {entry}")
    other = out["legacy_other_path"]
    check(other["code"] != 0 and f"root's registry entry for {SLUG} names {out['config_path_registered']}, not "
          f"{other['stray']}" in other["said"],
          f"a configuration other than the registered one is refused before it is read: {other}")
    check(out["legacy_other_path_load"] == f"loaded as {OWNER}",
          f"and loaded outside a command, a record that is not about that file does not refuse it: {out['legacy_other_path_load']}")
    missing = out["no_record"]
    check(missing["code"] != 0 and "root holds no record of qux that says whose project it is" in missing["said"]
          and "Traceback" not in missing["said"],
          f"a project root holds no record of at all is refused before its configuration is read: {missing}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT), indent=1, default=str))
        return 0
    after = run(ROOT)
    if os.environ.get("SYRD663_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd663-before.") as tmp:
            os.chmod(tmp, 0o755)
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    print(f"root_owner_from_record_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
