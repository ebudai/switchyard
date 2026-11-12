#!/usr/bin/env python3
"""SYRD-763: `new` refuses root, or any uid-0 account, as a project's owner, where the owner is decided.

Every root branch that writes as or for a project's owner is chosen by `owner_writes.root_writes_for(owner)`, which
is False when the owner is root: with `root` as the owner, each of them takes its by-path code as root (the
SYRD-663 class, at `new`, where root holds no record yet). A `--from` design artifact names the owner and can be
tenant-authored; nothing on the new-project path refused a uid-0 owner.

Here, as root in a user, mount, pid and network namespace: the real `scripts/switchyard new` and `scripts/team-launcher
<p> new` run as root with the owner named `root`, and `toor` (another name for uid 0) -- from a `--from` artifact,
from `--agent-name`, and from `--owner-user`. Main's code (from a git archive) does not refuse any of them at the
owner: `switchyard new` offers root as an existing owner user to reuse (with `--allow-existing-owner-user` it would
go on), and `team-launcher new` goes on to its host precheck. The candidate refuses each before anything is created, naming the account, with `/etc`, `/home`, `/srv` and
`/var/lib` byte for byte unchanged; an ordinary owner is not refused by it.
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
BEFORE = "6914f5faf0c4b627e25570990838bec955256fd4"  # main before SYRD-763
SLUG = "pie"
DESIGNS = Path("/srv/designs")
PROJECT = Path("/srv/pie")
REFUSAL = "as the project's owner, an account with uid 0; a project is owned by an unprivileged account, never root."
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
    for owner in ("root", "toor", "pie-agent"):
        named = json.loads(json.dumps(document))
        named["project"]["owner_user"] = owner
        (DESIGNS / f"{owner}.project.json").write_text(json.dumps(named, indent=2), encoding="utf-8")

    env = {"PATH": "/stubs/sbin:/stubs/bin:/usr/bin:/bin", "HOME": "/run/root-home", "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1"}
    switchyard = [sys.executable, str(release / "scripts" / "switchyard"), "new"]
    team_launcher = [sys.executable, str(release / "scripts" / "team-launcher"), SLUG, "new"]
    routes = {}
    for owner in ("root", "toor", "pie-agent"):
        artifact = str(DESIGNS / f"{owner}.project.json")
        routes[f"switchyard --from/{owner}"] = [*switchyard, "--from", artifact, "--yes", "--headless",
                                                "--no-upstream-report"]
        routes[f"switchyard --agent-name/{owner}"] = [*switchyard, "--slug", SLUG, "--project-name", "Pie",
                                                      "--agent-name", owner, "--project-path", str(PROJECT), "--yes",
                                                      "--headless", "--no-upstream-report"]
        routes[f"team-launcher --from/{owner}"] = [*team_launcher, "--from", artifact]
        routes[f"team-launcher --owner-user/{owner}"] = [*team_launcher, "--owner-user", owner, "--repository",
                                                         str(PROJECT)]
    out: dict = {}
    for name, argv in routes.items():
        before = snapshot(Path("/etc"), Path("/home"), Path("/srv"), Path("/var/lib"))
        done = subprocess.run(argv, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, env=env,
                              cwd="/")
        out[name] = {"code": done.returncode, "said": (done.stdout + done.stderr)[-900:],
                     "unchanged": snapshot(Path("/etc"), Path("/home"), Path("/srv"), Path("/var/lib")) == before}
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "skills",
                              "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, mode: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd763.") as tmp:
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


UID0_ROUTES = [f"{entry}/{owner}" for entry in ("switchyard --from", "switchyard --agent-name", "team-launcher --from",
                                                "team-launcher --owner-user") for owner in ("root", "toor")]


def before(out: dict) -> None:
    accepted = [name for name in UID0_ROUTES if REFUSAL not in out[name]["said"]]
    check(accepted == UID0_ROUTES, f"before: no entry refuses root or toor as the owner: {accepted}")
    for name in UID0_ROUTES:
        owner = name.rsplit("/", 1)[1]
        said = out[name]["said"]
        past = (f"owner user {owner} (uid 0" in said and "already exists" in said) if name.startswith("switchyard") \
            else "no PostgreSQL server is installed" in said
        check(past, f"before, {name}: the uid-0 owner gets past where it is decided -- switchyard offers it as an "
                    f"existing owner to reuse, team-launcher goes on to the host precheck: {out[name]}")


def candidate(out: dict) -> None:
    for name in UID0_ROUTES:
        got = out[name]
        owner = name.rsplit("/", 1)[1]
        check(got["code"] != 0 and f"names {owner!r} {REFUSAL}" in got["said"] and "Nothing was created." in got["said"]
              and got["unchanged"] and "Traceback" not in got["said"],
              f"{name}: refused where the owner is decided, naming the account, before anything is created: {got}")
    for entry in ("switchyard --from", "switchyard --agent-name", "team-launcher --from", "team-launcher --owner-user"):
        got = out[f"{entry}/pie-agent"]
        check(REFUSAL not in got["said"], f"{entry}, an ordinary owner: not refused by it: {got}")


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]), default=str))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--dump":
        tree = ROOT if sys.argv[2] == "candidate" else tree_at(BEFORE, Path(tempfile.mkdtemp()))
        print(json.dumps(run(tree, sys.argv[2]), indent=1, default=str))
        return 0
    after = run(ROOT, "candidate")
    if os.environ.get("SYRD763_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd763-before.") as tmp:
            os.chmod(tmp, 0o755)
            before(run(tree_at(BEFORE, Path(tmp) / "before"), "before"))
    candidate(after)
    print(f"new_uid0_owner_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
