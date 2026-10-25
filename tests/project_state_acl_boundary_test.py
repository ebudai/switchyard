#!/usr/bin/env python3
"""SYRD-682: no launch or presentation write lowers the board service's traverse to a tenant's control repository.

A tenant keeps its control repository in `~/.local/state/switchyard/projects/<slug>/control.git` -- PIE, and on the
same host SAPI and TEST3 -- beside its presentation state and layouts. `repair-boundary` gives the board service a
named ACL traverse on that directory. A chmod of the directory to 0700 sets the ACL mask to nothing: the named entry
stays listed and stops working, and every commit-verifying transition failed with "commit_hash verification
repository not found", which named a missing repository for what was a permission. SYRD-656 left the presentation
state and the role-runtime journal off it; the presentation layout, written by the owner when the desktop account is
the project's owner, still made the directory 0700 on every window it opened.

Here, as root in a user and mount namespace with real uids and an ACL-capable tmpfs: two tenants, each with a real
bare control repository holding a commit, the boundary put in place by `repair-boundary`'s own apply core (the
boundary phase of the packet `render_operator_commands` renders, every statement through `_run_helper`), and a third
tenant installed without any ACL. Then two launches' writes each -- the presentation state, a role-runtime journal and
the presentation layout for the owner's own desktop, at the path `desktop_presentation_layout_path` gives -- once as
root for the owner and once as the owner. The directory's mode and ACL must be exactly what the repair left, the
boundary closed, and the board service must still resolve the commit through `_validate_commit_hash`, as itself.

Then the edges: a directory not made yet is made the owner's alone, 0700; a link put where root would write is
refused and what it points at is left as it was; and, with the mask zeroed as main's layout write zeroed it, the board
says the repository cannot be reached, names the directory it may not search and the repair, and an absent repository
is still "not found". Running that repair's own apply core restores the traverse. The before-run is main's code.
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
BEFORE = "fd06cac8c4d548d0a4a9cf6c893df778804e2fa7"  # main before SYRD-682
SERVICE, SERVICE_UID = "boardsvc", 1020
#: (slug, owner, uid, boundary): a tenant `repair-boundary` closed; one then repaired by hand as PIE's owner did after
#: the mask was zeroed (0700, then `setfacl -m m::x`: the directory 0710, group and other closed); and one installed
#: without any ACL.
TENANTS = (("pie", "pie-agent", 1018, "repaired"), ("sapi", "sapi-agent", 1016, "by hand"),
           ("test3", "test3-agent", 1017, ""))
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def sh(*argv: str, env: dict | None = None) -> str:
    return subprocess.run(list(argv), check=True, capture_output=True, text=True, env=env).stdout.strip()


def acl(path: Path) -> list[str]:
    done = subprocess.run(["getfacl", "-n", "-p", "-E", str(path)], capture_output=True, text=True)
    return sorted(line for line in done.stdout.splitlines() if line and not line.startswith("#"))


def home(owner: str) -> Path:
    return Path("/run/h") / owner


def state(slug: str, owner: str) -> Path:
    return home(owner) / ".local" / "state" / "switchyard" / "projects" / slug


def as_account(uid: int, operation) -> str:
    """`operation()` in a child that is `uid` alone; what it returned, or the exception it raised, as text."""
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the account's child
        os.close(reader)
        try:
            os.setgroups([uid])
            os.setresgid(uid, uid, uid)
            os.setresuid(uid, uid, uid)
            said = json.dumps(["ok", operation()], default=str)
        except BaseException as exc:  # noqa: BLE001
            said = json.dumps(["raised", f"{type(exc).__name__}: {exc}"])
        os.write(writer, said.encode())
        os._exit(0)
    os.close(writer)
    chunks = []
    while chunk := os.read(reader, 65536):
        chunks.append(chunk)
    os.close(reader)
    os.waitpid(pid, 0)
    kind, value = json.loads(b"".join(chunks).decode())
    return value if kind == "ok" else f"RAISED {value}"


def inner(root: Path, work: Path) -> dict:
    sys.path.insert(0, str(root))
    sh("mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", "/run")
    passwd, group = work / "passwd", work / "group"
    passwd.write_text("root:x:0:0::/root:/bin/sh\n" + "".join(
        f"{owner}:x:{uid}:{uid}::{home(owner)}:/bin/sh\n" for _slug, owner, uid, _b in TENANTS)
        + f"{SERVICE}:x:{SERVICE_UID}:{SERVICE_UID}::/run/boardsvc:/bin/sh\n", encoding="utf-8")
    group.write_text("root:x:0:\n" + "".join(f"{owner}:x:{uid}:{SERVICE}\n" for _slug, owner, uid, _b in TENANTS)
                     + f"{SERVICE}:x:{SERVICE_UID}:\n", encoding="utf-8")
    sh("mount", "--bind", str(passwd), "/etc/passwd")
    sh("mount", "--bind", str(group), "/etc/group")
    os.makedirs("/run/h", mode=0o755)

    from scripts import presentation_state_document as presentation
    from scripts import role_runtime
    from scripts import team_launcher as launcher
    from scripts.repository_boundary_repair import _run_helper
    from scripts.ticket_board import commit_cache
    from scripts.ticket_board import project_provision as provision
    from scripts.ticket_board.app import TicketBoardApp
    from scripts.ticket_board.provision_operator_packet import render_operator_commands
    from scripts.ticket_board.provision_repository_boundary import (repository_boundary_phase,
                                                                     repository_boundary_statements)
    from scripts.ticket_board.provision_tenant_paths import TENANT_PATHS_PREFIX

    git_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
    out: dict = {"tenants": {}}
    for slug, owner, uid, boundary in TENANTS:
        directory = state(slug, owner)
        for made in (home(owner), *reversed(directory.parents[: len(directory.relative_to(home(owner)).parts) - 1]),
                     directory):
            made.mkdir(mode=0o700, exist_ok=True)
            os.chown(made, uid, uid)
            os.chmod(made, 0o700)
        control = directory / "control.git"
        sh("git", "init", "-q", "--bare", str(control))
        tree = sh("git", f"--git-dir={control}", "hash-object", "-t", "tree", "-w", "/dev/null")
        commit = sh("git", f"--git-dir={control}", "commit-tree", tree, "-m", slug, env=git_env)
        sh("git", f"--git-dir={control}", "update-ref", "refs/heads/main", commit)
        sh("chown", "-R", f"{uid}:{uid}", str(control))
        config_path = directory / f"{slug}.json"
        config_path.write_text("{}\n", encoding="utf-8")
        os.chown(config_path, uid, uid)
        plan = provision.build_plan(project=slug, owner_user=owner, owner_home=home(owner), commit_git_dir=control)

        def problems(plan=plan) -> list[str]:
            return [str(p) for p in launcher.repository_boundary_problems(plan, runner=subprocess.run)]

        def board_resolves(control=control, commit=commit, slug=slug):
            """The board's own refusal or answer for this commit, asked as the board service."""
            def ask():
                app = TicketBoardApp.__new__(TicketBoardApp)
                app.commit_git_dirs, app.project = (control,), slug
                try:
                    return app._validate_commit_hash(commit[:12])
                except ValueError as refusal:
                    return f"REFUSED {refusal}"
            return as_account(SERVICE_UID, ask)

        def observed(label: str, directory=directory, problems=problems) -> dict:
            status = os.stat(directory)
            return {"label": label, "problems": problems(), "mode": f"{stat.S_IMODE(status.st_mode):04o}",
                    "acl": acl(directory)}

        def repair(plan=plan) -> dict:
            phase, phase_problem = repository_boundary_phase(render_operator_commands(plan))
            refusals = [r for r in (_run_helper(s, TENANT_PATHS_PREFIX, preview=False, planned=set(),
                                                say=lambda _line: None)
                                    for s in repository_boundary_statements(phase)) if r]
            return {"phase_problem": phase_problem, "statements": len(phase), "refusals": refusals}

        record: dict = {"commit": commit, "control": str(control), "state": str(directory),
                        "before_repair": observed("before repair")}
        if boundary:
            record["repair"] = {**repair(), **observed("after repair")}
        if boundary == "by hand":
            as_account(uid, lambda directory=directory: (os.chmod(directory, 0o700),
                                                         sh("setfacl", "-m", "m::x", str(directory))))
        record["start"] = observed("start")
        record["board_start"] = board_resolves()
        config = SimpleNamespace(run_as_user=owner, project=slug, repository=None)
        state_path = presentation.presentation_state_path(config, config_path=config_path)
        journal_path = role_runtime.journal_path_for(config, config_path=config_path, role_name="ops")
        layout_path = launcher.desktop_presentation_layout_path(config, config_path=config_path, gui_user=owner)
        record["paths"] = [str(state_path), str(journal_path), str(layout_path)]

        def launch_writes(n: int, state_path=state_path, journal_path=journal_path, layout_path=layout_path,
                          config=config, config_path=config_path, slug=slug, owner=owner) -> str:
            """One launch's writes into the project state directory, as the launch and the window make them."""
            with presentation._locked_state(state_path, config=config, runner=subprocess.run):
                presentation._write_state(config, state_path, {"schema": "test", "launch": n, "slots": []},
                                          runner=subprocess.run)
            role_runtime.RuntimeJournal(project=slug, role="ops", owner_user=owner, config_path=str(config_path),
                                        steps_applied=[f"launch {n}"]).write(journal_path)
            return launcher.write_desktop_layout(layout_path, {"launch": n}, gui_user=owner, runner=subprocess.run,
                                                 project=slug)

        for n in (1, 2):  # an ordinary launch, then the relaunch after a cold boot
            try:
                said = launch_writes(n)
            except BaseException as exc:  # noqa: BLE001
                said = f"RAISED {exc!r}"
            record[f"launch{n}_root"] = {"said": said, **observed(f"launch {n} as root for {owner}")}
            record[f"launch{n}_owner"] = {"said": as_account(uid, lambda n=n: launch_writes(n)),
                                          **observed(f"launch {n} as {owner}")}
        record["board_after"] = board_resolves()
        record["written"] = {name: [os.stat(path).st_uid, f"{stat.S_IMODE(os.stat(path).st_mode):04o}",
                                    json.loads(path.read_text()).get("launch",
                                                                     json.loads(path.read_text()).get("steps_applied"))]
                             for name, path in (("state", state_path), ("journal", journal_path),
                                                ("layout", layout_path))}
        if boundary:
            # Main's layout write, as the owner: the directory 0700, and the traverse goes with the mask.
            as_account(uid, lambda directory=directory: os.chmod(directory, 0o700))
            record["zeroed"] = observed("mask zeroed")
            record["board_zeroed"] = board_resolves()
            # A board configured with a link to the repository: the directory named is the one the link leads into.
            link = Path("/run/links") / f"{slug}.git"
            link.parent.mkdir(mode=0o755, exist_ok=True)
            link.symlink_to(control)
            record["board_zeroed_link"] = as_account(SERVICE_UID, lambda link=link, slug=slug: (
                commit_cache.missing_repository_refusal([link], project=slug)))
            record["recovery"] = {**repair(), **observed("after the repair again")}
            record["board_recovered"] = board_resolves()
            absent = directory / "absent.git"  # beside it, in a directory the board may search
            record["board_absent"] = as_account(SERVICE_UID, lambda absent=absent, slug=slug: (
                commit_cache.missing_repository_refusal([absent], project=slug)))
        out["tenants"][slug] = record

    # A project whose state directory is not made yet: the layout writer makes it the owner's alone, either way.
    slug, owner, uid, _b = TENANTS[0]
    fresh: dict = {}
    for name, who in (("root", 0), ("owner", uid)):
        path = state(f"fresh-{name}", owner) / f"fresh-{name}-presentation-layout.json"

        def write(path=path, name=name, owner=owner) -> str:
            return launcher.write_desktop_layout(path, {"fresh": name}, gui_user=owner, runner=subprocess.run,
                                                 project=f"fresh-{name}")
        said = write() if who == 0 else as_account(who, write)
        fresh[name] = {"said": said, "dir": [os.stat(path.parent).st_uid, f"{stat.S_IMODE(os.stat(path.parent).st_mode):04o}",
                                             acl(path.parent)] if path.parent.exists() else None,
                       "file": [os.stat(path).st_uid, f"{stat.S_IMODE(os.stat(path).st_mode):04o}"]
                       if path.exists() else None}
    out["fresh"] = fresh

    # A link where root would write: the owner's project state directory replaced by a link to a root-owned
    # directory carrying its own ACL. Root's layout and presentation-state writes are refused, and what the link
    # leads to keeps its mode and ACL and gains no file.
    target = Path("/run/elsewhere")
    target.mkdir(mode=0o700)
    sh("setfacl", "-m", f"u:{SERVICE}:--x,m::--x", str(target))
    linked = state("linked", owner)
    linked.symlink_to(target)
    os.lchown(linked, uid, uid)
    linked_config = SimpleNamespace(run_as_user=owner, project="linked", repository=None)
    link: dict = {"before": [f"{stat.S_IMODE(os.stat(target).st_mode):04o}", acl(target)]}
    link["layout"] = launcher.write_desktop_layout(linked / "linked-presentation-layout.json", {"x": 1},
                                                   gui_user=owner, runner=subprocess.run, project="linked")
    try:
        presentation._write_state(linked_config, linked / "presentation.json", {"schema": "test", "slots": []},
                                  runner=subprocess.run)
        link["state"] = ""
    except BaseException as exc:  # noqa: BLE001
        link["state"] = f"RAISED {exc}"
    link["after"] = [f"{stat.S_IMODE(os.stat(target).st_mode):04o}", acl(target)]
    link["files"] = sorted(p.name for p in target.iterdir())
    out["link"] = link
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd682.") as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
        child = subprocess.run(["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private",
                                sys.executable, str(Path(__file__).resolve()), "--inner", str(root), tmp],
                               capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=600)
        assert child.returncode == 0, child.stderr[-3000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


STEPS = ("launch1_root", "launch1_owner", "launch2_root", "launch2_owner")


def before(out: dict) -> None:
    pie = out["tenants"]["pie"]
    check(pie["repair"]["problems"] == [] and pie["launch1_root"]["problems"] == [],
          f"before: repair-boundary closes the boundary, and root's writes for the owner keep it: {pie['launch1_root']}")
    broken = pie["launch1_owner"]
    check(broken["problems"] and broken["mode"] == "0700" and "mask::---" in broken["acl"],
          f"before: the owner's own presentation layout sets the shared directory 0700 and the ACL mask to nothing, "
          f"and the board service can no longer reach control.git: {broken}")
    check(pie["board_after"].startswith(f"REFUSED commit_hash verification repository not found: {pie['control']}"),
          f"before: the board calls the repository it may not reach missing: {pie['board_after']}")


def candidate(out: dict) -> None:
    for slug, owner, uid, boundary in TENANTS:
        record = out["tenants"][slug]
        start = record["start"]
        if boundary:
            repair = record["repair"]
            check(record["before_repair"]["problems"] and repair["phase_problem"] == "" and repair["refusals"] == []
                  and repair["problems"] == [],
                  f"{slug}: the boundary starts open, and repair-boundary's own apply closes it: "
                  f"{record['before_repair']} {repair}")
            granted = [line for line in start["acl"] if line.startswith(f"user:{SERVICE_UID}:")]
            check(granted and "x" in granted[0] and any(line.startswith("mask::") and "x" in line
                                                       for line in start["acl"]) and start["problems"] == []
                  and (boundary != "by hand" or (start["mode"] == "0710" and "other::---" in start["acl"])),
                  f"{slug}: with a named traverse for the board service, effective under the mask ({boundary}): "
                  f"{start}")
            check(record["board_start"] == record["commit"],
                  f"{slug}: the board service resolves the commit through the traverse: {record['board_start']}")
        else:
            check(start["mode"] == "0700" and not any(line.startswith(("user:1", "mask::")) for line in start["acl"]),
                  f"{slug}: installed without an ACL, 0700: {start}")
        for name in STEPS:
            step = record[name]
            check(step["said"] == "" and step["problems"] == start["problems"] and step["mode"] == start["mode"]
                  and step["acl"] == start["acl"],
                  f"{step['label']}: every write succeeds, and the project state directory keeps the mode and ACL it "
                  f"had, so the boundary is as it was: {step}")
        check(record["written"] == {"state": [uid, "0600", 2], "journal": [uid, "0600", ["launch 2"]],
                                    "layout": [uid, "0600", 2]},
              f"{slug}: the state, the journal and the layout are written, the owner's, 0600: {record['written']}")
        if not boundary:
            continue
        check(record["board_after"] == record["commit"],
              f"{slug}: after two launches the board still resolves the commit: {record['board_after']}")
        zeroed = record["zeroed"]
        check(zeroed["problems"] and zeroed["mode"] == "0700" and "mask::---" in zeroed["acl"],
              f"{slug}: a chmod to 0700, as main's layout write made, zeroes the mask: {zeroed}")
        expected = (f"REFUSED commit_hash verification repository cannot be reached: {record['control']} (this "
                    f"board's account, {SERVICE}, may not search {record['state']}). This is a permission problem, "
                    "not a missing repository.")
        check(record["board_zeroed"].startswith(expected)
              and f"`pkexec switchyard repair-boundary {slug}` shows the repair, and `--apply` makes it."
              in record["board_zeroed"],
              f"{slug}: the board says the repository cannot be reached, names the directory it may not search, and "
              f"the repair: {record['board_zeroed']}")
        check(record["board_zeroed_link"].startswith(
                  f"commit_hash verification repository cannot be reached: /run/links/{slug}.git (this board's account, "
                  f"{SERVICE}, may not search {record['state']})."),
              f"{slug}: configured through a link, it names the directory the link leads into: "
              f"{record['board_zeroed_link']}")
        check(record["board_absent"] == f"commit_hash verification repository not found: {record['state']}/absent.git",
              f"{slug}: a repository that is not there is still not found: {record['board_absent']}")
        recovery = record["recovery"]
        check(recovery["refusals"] == [] and recovery["problems"] == [] and recovery["acl"] == record["repair"]["acl"]
              and recovery["mode"] == record["repair"]["mode"] and record["board_recovered"] == record["commit"],
              f"{slug}: that repair restores the traverse exactly, and the board resolves the commit again: "
              f"{recovery} {record['board_recovered']}")
    fresh = out["fresh"]
    owner_uid = TENANTS[0][2]
    for name in ("root", "owner"):
        made = fresh[name]
        check(made["said"] == "" and made["dir"] is not None and made["dir"][:2] == [owner_uid, "0700"]
              and not any(line.startswith(("user:1", "mask::")) for line in made["dir"][2])
              and made["file"] == [owner_uid, "0600"],
              f"a state directory not made yet is made by the layout write, as {name}, the owner's alone, 0700, with "
              f"the layout 0600: {made}")
    link = out["link"]
    check("symlink" in link["layout"] and link["state"].startswith("RAISED ") and "refusing" in link["state"],
          f"a link where root would write: root's layout and state writes are refused: {link}")
    check(link["after"] == link["before"] and link["files"] == [],
          f"and the directory the link leads to keeps its mode and ACL and gains nothing: {link}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT), indent=1, default=str))
        return 0
    after = run(ROOT)
    if os.environ.get("SYRD682_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd682-before.") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    print(f"project_state_acl_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
