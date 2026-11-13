#!/usr/bin/env python3
"""SYRD-765: `new` refuses an owner that is not a plain account name, where the owner is decided.

SYRD-763 refuses root or any uid-0 account as a new project's owner, by name and by `pwd`. But an all-digit owner such
as "0" names no account `pwd` can find, while `chown`, `install -o` and systemd's `User=` read it as a uid; and nothing
on the new-project path checked the owner against `OWNER_USER_NAME_RE` (`^[a-z_][a-z0-9_-]{0,31}$`) -- not the `--from`
artifact's `owner_user`, nor `--agent-name`, nor `--owner-user`.

Here, as root in a user, mount, pid and network namespace (as `new_uid0_owner_test` does): the real `scripts/switchyard
new` and `scripts/team-launcher <p> new` run as root with the owner "0", "1000", "a/b", "..", "Pie" and a 33-character
name, from a `--from` artifact, from `--agent-name`, and from `--owner-user`. Main's code (from a git archive) refuses
none of them where the owner is decided. The candidate refuses each there, naming the value and where it came from,
with `/etc`, `/home`, `/srv` and `/var/lib` byte for byte unchanged; an ordinary owner is not refused by it.
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
BEFORE = "48c426a73af8ef92e960dc11331091039758a6fc"  # main before SYRD-765
SLUG = "pie"
DESIGNS = Path("/srv/designs")
PROJECT = Path("/srv/pie")
REFUSAL = ("as the project's owner, which is not a plain account name (a lowercase letter or _, then up to 31 lowercase "
           "letters, digits, _ or -).")
BAD = ("0", "1000", "a/b", "..", "Pie", "a" * 33)
GOOD = "pie-agent"
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
    found = {}
    for top in roots:
        for path in [top, *sorted(top.rglob("*"))]:
            info = os.lstat(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16] if stat.S_ISREG(info.st_mode) else ""
            found[str(path)] = [info.st_uid, f"{stat.S_IMODE(info.st_mode):04o}", digest]
    return found


def enter_root_owned_root(stage: Path) -> None:
    """Become root of a filesystem namespace root really owns (as `root_owner_from_record_test` does)."""
    new = stage / "newroot"
    new.mkdir()
    sh("mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", str(new))
    for directory in ("usr", "etc", "dev", "proc", "tmp", "run", "home", "opt", "stage", "var/lib/switchyard",
                      "var/tmp", "srv", "stubs/bin", "stubs/sbin"):
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


def inner(root: Path, work: Path, mode: str) -> dict:
    sh("ip", "link", "set", "lo", "up")
    enter_root_owned_root(work)
    root = Path("/stage") / root.relative_to(work)
    release = Path("/opt/sw")
    for part in ("scripts", "config", "examples", "skills", "docs"):
        if (root / part).exists():
            shutil.copytree(root / part, release / part, symlinks=True)
    sh("chmod", "-R", "a+rX", str(release))
    for place in ("/etc/switchyard", "/etc/systemd/system", "/etc/sudoers.d", "/etc/polkit-1/rules.d", "/etc/tmpfiles.d"):
        Path(place).mkdir(parents=True, exist_ok=True)
    Path("/stubs/sbin/systemctl").write_text('#!/bin/sh\ncase "$*" in *is-active*) echo inactive; exit 3;; esac\nexit 0\n')
    for cli in ("tmux", "claude", "codex", "agy", "copilot", "hermes", "konsole", "sudo", "runuser", "useradd",
                "loginctl", "psql", "git"):
        Path(f"/stubs/bin/{cli}").write_text(f'#!/bin/sh\necho "refused stub: {cli} $*" >&2\nexit 97\n')
    for place in (Path("/stubs/sbin"), Path("/stubs/bin")):
        for stub in place.iterdir():
            stub.chmod(0o755)
    Path("/etc/passwd").write_text("root:x:0:0::/run/root-home:/bin/sh\ntoor:x:0:0::/run/root-home:/bin/sh\n"
                                   "boardsvc:x:1500:1500::/nonexistent:/bin/false\n"
                                   "pie-agent:x:1018:1018::/home/pie-agent:/bin/bash\n", encoding="utf-8")
    Path("/etc/group").write_text("root:x:0:\nboardsvc:x:1500:\npie-agent:x:1018:\n", encoding="utf-8")
    os.makedirs("/run/root-home", mode=0o700)
    os.makedirs("/home/pie-agent", mode=0o700)
    os.chown("/home/pie-agent", 1018, 1018)
    DESIGNS.mkdir(parents=True)

    sys.path.insert(0, str(release))
    from scripts import team_launcher as launcher

    # A design artifact as the design session emits it, with the owner each case names.
    # Written as root's own file (the writer is no decision point), then each case's owner set in it.
    launcher._write_initial_switchyard_project_artifact(
        project_name="Pie", slug=SLUG, owner_user="root", project_dir=PROJECT,
        artifact_path=DESIGNS / "template.project.json", design_document=DESIGNS / "PROJECT_DESIGN.md",
        owner_shell="bash", include_designer=False)
    document = json.loads((DESIGNS / "template.project.json").read_text(encoding="utf-8"))
    for index, owner in enumerate((*BAD, GOOD)):
        named = json.loads(json.dumps(document))
        named["project"]["owner_user"] = owner
        (DESIGNS / f"owner{index}.project.json").write_text(json.dumps(named, indent=2), encoding="utf-8")

    env = {"PATH": "/stubs/sbin:/stubs/bin:/usr/bin:/bin", "HOME": "/run/root-home", "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1"}
    switchyard = [sys.executable, str(release / "scripts" / "switchyard"), "new"]
    team_launcher = [sys.executable, str(release / "scripts" / "team-launcher"), SLUG, "new"]
    routes = {}
    for index, owner in enumerate((*BAD, GOOD)):
        artifact = str(DESIGNS / f"owner{index}.project.json")
        routes[f"switchyard --from|{owner}"] = [*switchyard, "--from", artifact, "--yes", "--headless",
                                                "--no-upstream-report"]
        routes[f"switchyard --agent-name|{owner}"] = [*switchyard, "--slug", SLUG, "--project-name", "Pie",
                                                      "--agent-name", owner, "--project-path", str(PROJECT), "--yes",
                                                      "--headless", "--no-upstream-report"]
        routes[f"team-launcher --from|{owner}"] = [*team_launcher, "--from", artifact]
        routes[f"team-launcher --owner-user|{owner}"] = [*team_launcher, "--owner-user", owner, "--repository",
                                                         str(PROJECT)]
    out: dict = {}
    for name, argv in routes.items():
        before = snapshot(Path("/etc"), Path("/home"), Path("/srv"), Path("/var/lib"))
        done = subprocess.run(argv, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, env=env,
                              cwd="/")
        after = snapshot(Path("/etc"), Path("/home"), Path("/srv"), Path("/var/lib"))
        out[name] = {"code": done.returncode, "said": (done.stdout + done.stderr)[-900:], "unchanged": after == before,
                     "changed": sorted(path for path in {*before, *after} if before.get(path) != after.get(path))}
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "skills",
                              "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, mode: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd765.") as tmp:
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
                               env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=1800, stdin=subprocess.DEVNULL)
        assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


ENTRIES = ("switchyard --from", "switchyard --agent-name", "team-launcher --from", "team-launcher --owner-user")
BAD_ROUTES = [f"{entry}|{owner}" for entry in ENTRIES for owner in BAD]


def before(out: dict) -> None:
    refused = [name for name in BAD_ROUTES if REFUSAL in out[name]["said"]]
    check(not refused, f"before: no entry refuses a name that is not a plain account name: {refused}")
    for name in BAD_ROUTES:
        owner, said = name.split("|", 1)[1], out[name]["said"]
        # switchyard goes on with the owner (its summary names it); team-launcher goes on to its host precheck, or to
        # the provisioning plan's own, looser account-name rule (which takes "0", "1000", "Pie" and 33 characters).
        past = f"switchyard: owner user: {owner}\n" in said if name.startswith("switchyard") else (
            "no PostgreSQL server is installed" in said or "owner user must look like a local Unix account name" in said)
        check(past, f"before, {name}: the owner gets past where it is decided: {out[name]}")
    first = out["switchyard --from|0"]
    check("/etc/switchyard/provision/pie/upstream-report.json" in first["changed"],
          f"before, switchyard --from with the owner '0': root records the project's report decision for it: {first}")


def candidate(out: dict) -> None:
    for name in BAD_ROUTES:
        got = out[name]
        owner = name.split("|", 1)[1]
        check(got["code"] != 0 and f"names {owner!r} {REFUSAL}" in got["said"] and "Nothing was created." in got["said"]
              and got["unchanged"] and "Traceback" not in got["said"],
              f"{name}: refused where the owner is decided, naming the value, before anything is created: {got}")
        source = "/srv/designs/owner" if "--from" in name else ("the agent user" if "agent-name" in name else "--owner-user")
        check(f"switchyard: {source}" in got["said"], f"{name}: the refusal names where the owner came from: {got}")
    for entry in ENTRIES:
        got = out[f"{entry}|{GOOD}"]
        check(REFUSAL not in got["said"] and "uid 0" not in got["said"],
              f"{entry}, an ordinary owner: not refused by it: {got}")


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]), default=str))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--dump":
        tree = ROOT if sys.argv[2] == "candidate" else tree_at(BEFORE, Path(tempfile.mkdtemp()))
        print(json.dumps(run(tree, sys.argv[2]), indent=1, default=str))
        return 0
    after = run(ROOT, "candidate")
    if os.environ.get("SYRD765_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd765-before.") as tmp:
            os.chmod(tmp, 0o755)
            before(run(tree_at(BEFORE, Path(tmp) / "before"), "before"))
    candidate(after)
    print(f"new_owner_name_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
