#!/usr/bin/env python3
"""SYRD-655 / SYRD-661: root makes a tenant's pane-state directory in the tenant's own runtime directory.

Live on PIE (`resume-provision`, cross-account) and on SYRD-642's headless
clean host (role panes at launch), the same refusal:

    tenant_paths: refused: /run/user/1018/pie-ticket-board cannot be made without a race:
    there is no directory only root can change on its filesystem to make it in first.

`/run/user/<uid>` is the owner's own tmpfs, mode 0700. Root's no-race rule
(SYRD-605) stages what it makes in a directory only root can change on the
destination's filesystem, and that filesystem has none. The repair made the
directories by hand, `pie-agent:pie-agent` 0700.

Here, as root in a user and mount namespace with real uids: `/run` is a tmpfs,
`/run/user/1018` is its own tmpfs owned by uid 1018, mode 0700, as logind
mounts it, and `/etc/passwd` names the accounts. The real
`ensure_owner_state_dirs` runs with the real subprocess runner, so the helper
argv it builds is executed; `owner_writes.ensure_owner_dirs` is the in-process
path. No host account, home or runtime directory is touched. The before-run is
main's code, from a git archive.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "f505808f24373c81517c7a13ea434ebd8a571bf8"  # main before SYRD-655
OWNER, OWNER_UID, OTHER, OTHER_UID = "pie-agent", 1018, "nbr-agent", 1019
RUNTIME = f"/run/user/{OWNER_UID}"
STATE = f"{RUNTIME}/pie-ticket-board"
PANE_STATE = f"{STATE}/pane-state"
HOME = "/run/pie-home"
SESSIONS = f"{HOME}/.local/state/pie-ticket-board/pane-sessions"
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def sh(*argv: str) -> None:
    subprocess.run(list(argv), check=True)


def shape(path: str) -> list | None:
    """Owner, group and mode, or None when nothing is there; a link says so."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(status.st_mode):
        return ["link", os.readlink(path)]
    return [status.st_uid, status.st_gid, f"{stat.S_IMODE(status.st_mode):04o}"]


def runtime(mode: int = 0o700, uid: int = OWNER_UID) -> None:
    """The owner's runtime directory, fresh: its own tmpfs, as logind mounts it."""
    subprocess.run(["umount", RUNTIME], capture_output=True)
    os.makedirs(RUNTIME, exist_ok=True)
    sh("mount", "-t", "tmpfs", "-o", f"mode={mode:04o},uid={uid},gid={uid}", "tmpfs", RUNTIME)


def inner(root: Path, work: Path) -> dict:
    sys.path.insert(0, str(root))
    sh("mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", "/run")
    os.makedirs("/run/user", mode=0o755)
    other_runtime = f"/run/user/{OTHER_UID}"
    os.makedirs(other_runtime)
    sh("mount", "-t", "tmpfs", "-o", f"mode=0700,uid={OTHER_UID},gid={OTHER_UID}", "tmpfs", other_runtime)
    passwd, group = work / "passwd", work / "group"
    passwd.write_text(f"root:x:0:0::/root:/bin/sh\n{OWNER}:x:{OWNER_UID}:{OWNER_UID}::{HOME}:/bin/sh\n"
                      f"{OTHER}:x:{OTHER_UID}:{OTHER_UID}::/run/nbr-home:/bin/sh\n", encoding="utf-8")
    group.write_text(f"root:x:0:\n{OWNER}:x:{OWNER_UID}:\n{OTHER}:x:{OTHER_UID}:\n", encoding="utf-8")
    sh("mount", "--bind", str(passwd), "/etc/passwd")
    sh("mount", "--bind", str(group), "/etc/group")
    os.makedirs(HOME)
    os.chown(HOME, OWNER_UID, OWNER_UID)
    os.chmod(HOME, 0o750)

    from scripts import owner_writes
    from scripts import team_launcher as launcher

    config = SimpleNamespace(run_as_user=OWNER, session_dir=Path(SESSIONS), project="pie")

    def recover() -> str:
        """The root recovery shape: the launcher gives the owner its session and pane-state directories."""
        said: list[str] = []

        def runner(argv, **kwargs):
            done = subprocess.run(argv, capture_output=True, text=True, **kwargs)
            said.append(done.stderr.strip())
            return done

        try:
            launcher.ensure_owner_state_dirs(config, pane_state_dir=Path(PANE_STATE), runner=runner)
        except SystemExit as stop:
            return " | ".join([*filter(None, said), str(stop)])
        return ""

    def leftovers() -> list[str]:
        return sorted(name for place in ("/run", "/run/user", RUNTIME) if os.path.isdir(place)
                      for name in os.listdir(place) if name.startswith(".switchyard-new-"))

    out: dict = {"whoami": os.geteuid()}
    runtime()
    out["first"] = {"refused": recover(), "state": shape(STATE), "pane_state": shape(PANE_STATE),
                    "sessions": shape(SESSIONS), "leftovers": leftovers()}
    out["again"] = {"refused": recover(), "state": shape(STATE), "pane_state": shape(PANE_STATE),
                    "sessions": shape(SESSIONS), "leftovers": leftovers()}
    try:
        owner_writes.ensure_owner_dirs(OWNER, [Path(f"{STATE}/listener")], mode=0o700)
        out["in_process"] = {"refused": "", "made": shape(f"{STATE}/listener")}
    except SystemExit as stop:
        out["in_process"] = {"refused": str(stop), "made": shape(f"{STATE}/listener")}

    # A link where the state directory goes, to the neighbour's own runtime directory.
    runtime()
    foreign = f"{other_runtime}/target"
    os.makedirs(foreign)
    os.chown(foreign, OTHER_UID, OTHER_UID)
    os.chmod(foreign, 0o700)
    os.symlink(foreign, STATE)
    before = shape(foreign), sorted(os.listdir(foreign))
    out["link"] = {"refused": recover(), "state": shape(STATE), "foreign": [shape(foreign), sorted(os.listdir(foreign))],
                   "foreign_before": list(before), "leftovers": leftovers()}
    # Not the owner's own: open to its group, or owned by somebody else.
    runtime(mode=0o750)
    out["loose"] = {"refused": recover(), "state": shape(STATE)}
    runtime(uid=OTHER_UID)
    out["foreign_owned"] = {"refused": recover(), "state": shape(STATE)}
    # The runtime directory is the owner's, but anybody may change /run/user above it.
    runtime()
    os.chmod("/run/user", 0o1777)
    out["open_parent"] = {"refused": recover(), "state": shape(STATE)}
    os.chmod("/run/user", 0o755)
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd655.") as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
        child = subprocess.run(["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private",
                                sys.executable, str(Path(__file__).resolve()), "--inner", str(root), tmp],
                               capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=600)
        assert child.returncode == 0, child.stderr[-3000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


OWNED = [OWNER_UID, OWNER_UID, "0700"]


def before(out: dict) -> None:
    first = out["first"]
    check("cannot be made without a race" in first["refused"] and first["pane_state"] is None,
          f"before: root's recovery refuses the pane-state directory in the owner's runtime directory, as on PIE: {first}")


def candidate(out: dict) -> None:
    check(out["whoami"] == 0, f"the recovery runs as root: euid {out['whoami']}")
    first = out["first"]
    check(first["refused"] == "" and first["state"] == OWNED and first["pane_state"] == OWNED
          and first["sessions"] == OWNED and first["leftovers"] == [],
          f"root's recovery gives the owner its directories in its own runtime directory, owner:owner 0700, and "
          f"its session directory in its home, leaving nothing staged behind: {first}")
    again = out["again"]
    check(again == first, f"a second recovery changes nothing and refuses nothing: {again}")
    check(out["in_process"] == {"refused": "", "made": OWNED},
          f"the in-process path does the same: {out['in_process']}")
    link = out["link"]
    check("symbolic link" in link["refused"] and link["state"][0] == "link"
          and link["foreign"] == link["foreign_before"] and link["leftovers"] == [],
          f"a link at the state directory is refused, and the neighbour's directory it names is untouched: {link}")
    for name, why in (("loose", "mode 0750"), ("foreign_owned", f"owner {OTHER_UID}")):
        case = out[name]
        check(f"is not uid {OWNER_UID}'s own runtime directory" in case["refused"] and why in case["refused"]
              and case["state"] is None,
              f"a runtime directory that is not the owner's own ({name}) is refused before anything is made: {case}")
    parent = out["open_parent"]
    check("/run/user is not a directory only root can change" in parent["refused"] and parent["state"] is None,
          f"a /run/user anybody can change is refused before anything is made: {parent}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT), indent=1, default=str))
        return 0
    after = run(ROOT)
    if os.environ.get("SYRD655_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd655-before.") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    print(f"owner_runtime_dir_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
