#!/usr/bin/env python3
"""SYRD-575: a fresh host's first project stages root's artifacts without a later upgrade.

Fresh Arch VM acceptance (2026-10-10, main 84cedf1) printed, at the first
`switchyard new`:

    switchyard: could not stage uat574 privileged artifacts for root: [Errno 2]
    No such file or directory: '/etc/switchyard/provision'. Run `switchyard
    upgrade uat574` as root before installing its units.

/etc/switchyard did not exist yet, and the provision directory's walk created
only from the provision root down.

Every case runs the real staging -- `install_privileged_artifacts` through the
real default root, /etc/switchyard/provision, with no override -- as root in a
fresh user and mount namespace whose /etc is an empty root-owned tmpfs: the
fresh host's shape, without touching this host's /etc. The before-run is main's
code, from a git archive.
"""

from __future__ import annotations

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
BEFORE = "c99bfb856be5fa73410e5cba69b9e21db4d71a39"  # main before SYRD-575
PROJECT = "uat575"
CASES = ("fresh", "fresh_umask_077", "existing_safe", "symlinked_parent", "writable_parent", "foreign_parent",
         "file_parent", "repeated", "interrupted_retry", "swapped_while_made", "fresh_under_unsafe_etc")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:  # the label again, last, so a truncated tail still says which check failed
        raise AssertionError(f"{detail}\nFAILED CHECK: {re.split(r': [\[{(]', detail, maxsplit=1)[0]}")
    CHECKS += 1


# --------------------------------------------------------------------------
# One case, as root in its namespace (see `run_case`)
# --------------------------------------------------------------------------


def tree(top: Path) -> dict:
    """Everything under `top`: type, mode, owner, and a file's bytes."""
    seen = {}
    for path in sorted([top, *top.rglob("*")]):
        info = path.lstat()
        kind = "link" if stat.S_ISLNK(info.st_mode) else "dir" if stat.S_ISDIR(info.st_mode) else "file"
        entry = [kind, f"{stat.S_IMODE(info.st_mode):04o}", info.st_uid]
        if kind == "file":
            entry.append(path.read_bytes().decode())
        if kind == "link":
            entry.append(os.readlink(path))
        seen[str(path)] = entry
    return seen


def case(name: str, code: Path) -> dict:
    sys.path.insert(0, str(code))
    from types import SimpleNamespace

    from scripts import team_launcher as launcher

    etc = Path("/etc")
    assert os.geteuid() == 0 and list(etc.iterdir()) == [], "not a fresh namespace /etc"
    assert launcher.switchyard_privileged_provision_root() == Path("/etc/switchyard/provision"), "not the real default"
    plan = SimpleNamespace(project=PROJECT)
    rendered = {"plan.json": b'{"project": "uat575"}\n', "install-units.sh": b"#!/bin/bash\necho units\n"}

    def stage(body: dict = rendered) -> str:
        try:
            return f"staged {launcher.install_privileged_artifacts(plan, body)}"
        except SystemExit as exc:
            return f"refused: {exc}"
        except OSError as exc:
            return f"failed: {exc}"

    seen: dict = {}
    if name == "fresh":
        seen["result"] = stage()
    elif name == "fresh_umask_077":
        os.umask(0o077)
        seen["result"] = stage()
    elif name == "existing_safe":
        (etc / "switchyard" / "projects").mkdir(parents=True)
        (etc / "switchyard" / "projects" / "older.json").write_text('{"project": "older"}\n')
        (etc / "switchyard" / "provision" / "older").mkdir(parents=True, mode=0o700)
        (etc / "switchyard" / "provision" / "older" / "plan.json").write_text("older's plan\n")
        os.chmod(etc / "switchyard" / "provision" / "older" / "plan.json", 0o600)
        for directory in (etc / "switchyard", etc / "switchyard" / "projects", etc / "switchyard" / "provision"):
            os.chmod(directory, 0o755)
        os.chmod(etc / "switchyard" / "provision" / "older", 0o700)
        seen["before"] = tree(etc)
        seen["result"] = stage()
    elif name == "symlinked_parent":
        (etc / "elsewhere").mkdir()
        (etc / "switchyard").symlink_to(etc / "elsewhere")
        seen["before"] = tree(etc)
        seen["result"] = stage()
    elif name == "writable_parent":
        (etc / "switchyard").mkdir()
        os.chmod(etc / "switchyard", 0o777)
        seen["before"] = tree(etc)
        seen["result"] = stage()
    elif name == "foreign_parent":
        # A directory root does not own: a host directory bound in, which this
        # namespace sees as owned by the overflow uid.
        (etc / "switchyard").mkdir()
        foreign = Path(os.environ["SYRD575_FOREIGN"])
        subprocess.run(["mount", "--bind", str(foreign), str(etc / "switchyard")], check=True)
        seen["owner"] = (etc / "switchyard").lstat().st_uid
        seen["before"] = sorted(os.listdir(etc / "switchyard"))
        seen["result"] = stage()
        seen["after"] = sorted(os.listdir(etc / "switchyard"))
    elif name == "file_parent":
        (etc / "switchyard").write_text("not a directory\n")
        seen["before"] = tree(etc)
        seen["result"] = stage()
    elif name == "repeated":
        seen["first"] = stage()
        seen["first_tree"] = tree(etc)
        seen["result"] = stage({**rendered, "plan.json": b'{"project": "uat575", "revision": 2}\n'})
    elif name == "fresh_under_unsafe_etc":
        # A fresh host whose /etc anyone can write: refused at /etc, before
        # /etc/switchyard -- which does not exist -- is made.
        os.chmod(etc, 0o777)
        seen["before"] = tree(etc)
        seen["result"] = stage()
    elif name == "swapped_while_made":
        # Between the judging and the making, /etc/switchyard becomes a link
        # to somewhere else: what was made is read back, and is not a directory.
        (etc / "elsewhere").mkdir()
        real_mkdir = Path.mkdir

        def swaps(path, *args, **kwargs):
            if Path(path) == etc / "switchyard":
                (etc / "switchyard").symlink_to(etc / "elsewhere")
                return None
            return real_mkdir(path, *args, **kwargs)

        Path.mkdir = swaps
        try:
            seen["result"] = stage()
        finally:
            Path.mkdir = real_mkdir
    elif name == "interrupted_retry":
        real_mkdir = Path.mkdir

        def dies_at_the_root(path, *args, **kwargs):
            if Path(path) == etc / "switchyard" / "provision":
                raise OSError(28, "No space left on device")
            return real_mkdir(path, *args, **kwargs)

        Path.mkdir = dies_at_the_root
        try:
            seen["first"] = stage()
        finally:
            Path.mkdir = real_mkdir
        seen["between"] = tree(etc)
        seen["result"] = stage()
    if name != "foreign_parent":  # that one holds a host directory, read by its own listing above
        seen["tree"] = tree(etc)
    return seen


def run_case(name: str, code: Path, foreign: Path) -> dict:
    """`case` as root in a new user and mount namespace whose /etc is an empty tmpfs."""
    script = (f"mount -t tmpfs -o mode=0755 none /etc && exec {sys.executable} {Path(__file__).resolve()} "
              f"--case {name} {code}")
    child = subprocess.run(["unshare", "--user", "--map-root-user", "--mount", "sh", "-c", script],
                           capture_output=True, text=True, timeout=300,
                           env={**os.environ, "SYRD575_FOREIGN": str(foreign), "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, f"{name}: {child.stderr[-2000:]}"
    return json.loads(child.stdout.strip().splitlines()[-1])


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def dry_runs_never_stage() -> list[str]:
    """The `switchyard new` cases its boundary suite records: which of them reached the staging."""
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    import new_project_command_boundary_test as boundary

    golden = next(value for name, value in vars(boundary).items() if name.isupper() and isinstance(value, dict)
                  and any(str(key).startswith("new: ") for key in value))
    return sorted(label for label, recorded in golden.items()
                  if any(call[0] == "install_privileged_artifacts" for call in recorded.get("calls", [])))


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--case":
        print(json.dumps(case(sys.argv[2], Path(sys.argv[3]))))
        return 0
    if shutil.which("unshare") is None or subprocess.run(
            ["unshare", "--user", "--map-root-user", "--mount", "true"], capture_output=True).returncode != 0:
        raise SystemExit("root_staging_parent_test: needs unprivileged user and mount namespaces")
    with tempfile.TemporaryDirectory(prefix="syrd575.") as tmp:
        foreign = Path("/usr/share/licenses")  # root's on the host; nobody's inside the namespace
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        before = {name: run_case(name, before_root, foreign) for name in ("fresh", "symlinked_parent", "writable_parent")}
        after = {name: run_case(name, ROOT, foreign) for name in CASES}

    target = f"/etc/switchyard/provision/{PROJECT}"
    staged_tree = {
        "/etc": ["dir", "0755", 0],
        "/etc/switchyard": ["dir", "0755", 0],
        "/etc/switchyard/provision": ["dir", "0755", 0],
        target: ["dir", "0700", 0],
        f"{target}/install-units.sh": ["file", "0700", 0, "#!/bin/bash\necho units\n"],
        f"{target}/plan.json": ["file", "0600", 0, '{"project": "uat575"}\n'],
    }

    # main: the live failure, word for word, and two unsafe parents it used.
    check(before["fresh"]["result"] == "failed: [Errno 2] No such file or directory: '/etc/switchyard/provision'"
          and before["fresh"]["tree"] == {"/etc": ["dir", "0755", 0]},
          f"before: a fresh host cannot stage, exactly as the VM printed: {before['fresh']}")
    check(before["symlinked_parent"]["result"] == f"staged {target}"
          and f"/etc/elsewhere/provision/{PROJECT}/plan.json" in before["symlinked_parent"]["tree"],
          f"before: a symlinked /etc/switchyard was followed, and root's plan written where it points: "
          f"{before['symlinked_parent']['result']}")
    check(before["writable_parent"]["result"] == f"staged {target}",
          f"before: root staged under a world-writable /etc/switchyard: {before['writable_parent']['result']}")

    # A genuinely absent /etc/switchyard: made, root's, and the artifacts staged.
    check(after["fresh"]["result"] == f"staged {target}" and after["fresh"]["tree"] == staged_tree,
          f"a fresh host stages its first project: /etc/switchyard and the root made 0755 and root's, the project "
          f"root-only: {after['fresh']}")
    unsafe_etc = after["fresh_under_unsafe_etc"]
    check(unsafe_etc["result"] == "refused: switchyard: /etc can be written by others (mode 0777), so it cannot hold "
                                  "root's provisioning directory /etc/switchyard/provision: what root keeps under it "
                                  "could be replaced. Nothing was written."
          and unsafe_etc["tree"] == unsafe_etc["before"] == {"/etc": ["dir", "0777", 0]},
          f"judged before anything is made: a fresh host under an unsafe /etc gets no /etc/switchyard: {unsafe_etc}")
    check(after["fresh_umask_077"]["result"] == f"staged {target}" and after["fresh_umask_077"]["tree"] == staged_tree,
          f"under umask 077 too: the parents are 0755 from the start, so other tenants can still read "
          f"/etc/switchyard/projects: {after['fresh_umask_077']['tree']}")

    # An existing safe tree is kept exactly, and the new project staged beside it.
    kept = after["existing_safe"]
    check(kept["result"] == f"staged {target}"
          and {path: entry for path, entry in kept["tree"].items() if path in kept["before"]} == kept["before"]
          and set(kept["tree"]) - set(kept["before"]) == {target, f"{target}/install-units.sh", f"{target}/plan.json"},
          f"an existing /etc/switchyard and its projects, provision root and another project's plan are untouched: {kept}")

    # Unsafe parents are refused before anything is made.
    for name, why in (("symlinked_parent", "is a symlink"), ("writable_parent", "can be written by others (mode 0777)"),
                      ("file_parent", "is not a directory")):
        refused = after[name]
        check(refused["result"] == f"refused: switchyard: /etc/switchyard {why}, so it cannot hold root's provisioning "
                                   "directory /etc/switchyard/provision: what root keeps under it could be replaced. "
                                   "Nothing was written." and refused["tree"] == refused["before"],
              f"{name}: refused, and nothing written -- not through the link, not inside: {refused}")
    foreign = after["foreign_parent"]
    check(foreign["owner"] == 65534 and foreign["result"].startswith(
              "refused: switchyard: /etc/switchyard is owned by uid 65534 rather than by root")
          and foreign["after"] == foreign["before"],
          f"a parent root does not own is refused, and nothing written in it: {foreign}")

    swapped = after["swapped_while_made"]
    check(swapped["result"] == "refused: switchyard: /etc/switchyard changed while it was being made. "
                               "Nothing more was written." and swapped["tree"] == {
              "/etc": ["dir", "0755", 0], "/etc/elsewhere": ["dir", "0755", 0],
              "/etc/switchyard": ["link", "0777", 0, "/etc/elsewhere"]},
          f"a parent swapped for a link as it is made is caught by the read-back, and nothing goes through it: {swapped}")

    # Repeated, and interrupted then retried.
    again = after["repeated"]
    check(again["first"] == again["result"] == f"staged {target}"
          and {k: v for k, v in again["tree"].items() if not k.endswith("plan.json")}
          == {k: v for k, v in again["first_tree"].items() if not k.endswith("plan.json")}
          and again["tree"][f"{target}/plan.json"] == ["file", "0600", 0, '{"project": "uat575", "revision": 2}\n'],
          f"staging again changes nothing but what was rendered anew: {again}")
    interrupted = after["interrupted_retry"]
    check(interrupted["first"] == "failed: [Errno 28] No space left on device"
          and interrupted["between"] == {"/etc": ["dir", "0755", 0], "/etc/switchyard": ["dir", "0755", 0]}
          and interrupted["result"] == f"staged {target}" and interrupted["tree"] == staged_tree,
          f"interrupted after /etc/switchyard was made: the retry finds it safe and finishes: {interrupted}")

    # Dry runs never reach the staging at all; only an executing root run does.
    check(dry_runs_never_stage() == ["new: execute as root, staged", "new: execute as root, staging fails"],
          f"of the recorded `switchyard new` runs, only the executing root ones stage: {dry_runs_never_stage()}")
    print(f"root_staging_parent_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
