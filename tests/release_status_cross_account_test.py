#!/usr/bin/env python3
"""SYRD-659: `switchyard release-status` for a tenant whose private tree the caller may not read.

After PIE was recovered, a read-only Director check ended in a traceback:

    switchyard release-status pie
    PermissionError: [Errno 13] Permission denied:
    '/home/pie-agent/Projects/personal_inference_engine/.switchyard/provision/pie.json'

`switchyard_release_status_command` loaded the tenant's generated configuration
first, and only its owner and root can read it.

Here, as root in a user, mount, pid and network namespace with real uids: the
tenant's home is its own, mode 0700, with the configuration the product
generates in it. The registry entry and the board's systemd unit are root's,
written the way provisioning writes them -- the unit is the product's own
rendering. A board answers on loopback at the port that unit names. The real
`scripts/switchyard release-status pie` then runs, as a child that has become
each caller: another tenant's account (the Director's case), an unprivileged
operator, the owner, and root (the `pkexec` case). A missing and a broken
configuration are run as the owner and as root, who can tell them apart. The
before-run is main's code, from a git archive.
"""

from __future__ import annotations

import http.server
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
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "cc907f2bfbeb1e470f8ec93e1cecbc925972dea8"  # main before SYRD-659
SLUG, NAME = "pie", "Personal Inference Engine"
OWNER, OWNER_UID = "pie-agent", 1018
DIRECTOR, DIRECTOR_UID = "syrd-agent", 1019  # another tenant's account
OPERATOR, OPERATOR_UID = "operator1", 1000
HOME = Path("/home") / OWNER
CHECKOUT = HOME / "Projects" / "personal_inference_engine"
PROVISION = CHECKOUT / ".switchyard" / "provision"
SHARED = "a" * 40
LIVE = "b" * 40
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def sh(*argv: str) -> None:
    subprocess.run(list(argv), check=True, capture_output=True)


def tree_modes() -> dict:
    """Owner and mode of every directory and file in the tenant's tree, to show nothing was widened."""
    found = {}
    for path in [HOME, *sorted(HOME.rglob("*"))]:
        info = os.lstat(path)
        found[str(path)] = [info.st_uid, f"{stat.S_IMODE(info.st_mode):04o}"]
    return found


def run_as(uid: int, gid: int, home: str, argv: list[str]) -> dict:
    """The real CLI, in a child that has become `uid`, with a rebuilt environment."""
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the caller's child
        os.close(reader)
        os.dup2(writer, 1)
        os.dup2(writer, 2)
        try:
            if uid:
                os.setgroups([gid])
                os.setresgid(gid, gid, gid)
                os.setresuid(uid, uid, uid)
            os.chdir("/")
            os.execve(sys.executable, [sys.executable, *argv],
                      {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8",
                       "PYTHONDONTWRITEBYTECODE": "1"})
        except BaseException as exc:  # noqa: BLE001
            os.write(2, f"could not become {uid}: {exc!r}".encode())
        os._exit(126)
    os.close(writer)
    data = b""
    while chunk := os.read(reader, 65536):
        data += chunk
    os.close(reader)
    _, status = os.waitpid(pid, 0)
    return {"code": os.waitstatus_to_exitcode(status), "said": data.decode("utf-8", "replace")}


class Board(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        body = json.dumps({"build_id": LIVE, "tickets": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return None


def inner(root: Path, work: Path) -> dict:
    sh("ip", "link", "set", "lo", "up")
    sh("mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", "/opt")
    release = Path("/opt/sw")
    for part in ("scripts", "config", "examples", "skills", "docs"):
        if (root / part).exists():
            shutil.copytree(root / part, release / part, symlinks=True)
    sh("chmod", "-R", "a+rX", str(release))  # a release is world-readable; a worktree's umask may not be
    for place in ("/run", "/home", "/etc/switchyard", "/etc/systemd/system", "/var/lib/switchyard", "/var/tmp",
                  "/usr/local/sbin", "/usr/local/lib/switchyard"):
        sh("mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", place)
    Path("/usr/local/sbin/systemctl").write_text('#!/bin/sh\ncase "$*" in *is-active*) echo active;; esac\nexit 0\n')
    Path("/usr/local/sbin/systemctl").chmod(0o755)
    passwd, group = work / "passwd", work / "group"
    accounts = ((OWNER, OWNER_UID, str(HOME)), (DIRECTOR, DIRECTOR_UID, f"/home/{DIRECTOR}"),
                (OPERATOR, OPERATOR_UID, f"/home/{OPERATOR}"))
    # boardsvc as well: the board's unit runs as it, and its reader refuses a unit naming no account.
    passwd.write_text("root:x:0:0::/run/root-home:/bin/sh\nboardsvc:x:1500:1500::/nonexistent:/bin/false\n" + "".join(
        f"{name}:x:{uid}:{uid}::{home}:/bin/sh\n" for name, uid, home in accounts), encoding="utf-8")
    group.write_text("root:x:0:\nboardsvc:x:1500:\n" + "".join(f"{name}:x:{uid}:\n" for name, uid, _ in accounts), encoding="utf-8")
    sh("mount", "--bind", str(passwd), "/etc/passwd")
    sh("mount", "--bind", str(group), "/etc/group")
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
    from scripts.ticket_board import rollout_journal
    from scripts.ticket_board.project_provision import build_plan

    # The shared release, with its marker, where `/opt/switchyard/current` names it.
    shared = Path("/opt/switchyard/releases") / SHARED
    shared.mkdir(parents=True)
    (shared / ".switchyard-release.json").write_text(json.dumps({"commit": SHARED}), encoding="utf-8")
    Path("/opt/switchyard/current").symlink_to(shared)
    # The tenant as provisioning leaves it: its generated configuration in its own tree, root's registry entry, and
    # the board's unit as the product renders it.
    plan = build_plan(project=SLUG, project_name=NAME, owner_user=OWNER, owner_home=HOME, source_repo=release,
                      service_user="boardsvc", project_repository=CHECKOUT)
    config_path = launcher.write_new_project_launcher_artifacts(plan, PROVISION, repository=CHECKOUT,
                                                                project_name=NAME, print_func=lambda *a, **k: None)
    rendered = launcher.render_privileged_artifacts(plan)
    unit = Path("/etc/systemd/system") / f"{SLUG}-ticket-board.service"
    unit.write_bytes(rendered[unit.name])
    unit.chmod(0o644)
    registry = Path("/etc/switchyard/projects")
    registry.mkdir()
    entry = registry / f"{SLUG}.json"

    def register(path: Path) -> None:
        entry.write_text(json.dumps({"schema": "switchyard.project-registry.v1", "slug": SLUG, "name": NAME,
                                     "config_path": str(path)}, indent=2), encoding="utf-8")
        entry.chmod(0o644)

    register(config_path)
    index = rollout_journal.index_path(SLUG)
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"attempt": "0001", "at": "2026-10-23T05:33:00Z", "status": "ok",
                                 "detail": "deploy finished"}) + "\n", encoding="utf-8")
    for place in (index.parent.parent, index.parent, index):
        place.chmod(0o755 if place.is_dir() else 0o644)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", plan.port), Board)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    cli = [str(release / "scripts" / "switchyard"), "release-status", SLUG]
    callers = {"director": (DIRECTOR_UID, f"/home/{DIRECTOR}"), "operator": (OPERATOR_UID, f"/home/{OPERATOR}"),
               "owner": (OWNER_UID, str(HOME)), "root": (0, "/run/root-home")}
    before = tree_modes()
    out: dict = {"config_path": str(config_path), "port": plan.port, "tree_before": before}
    for name, (uid, home) in callers.items():
        out[name] = run_as(uid, uid, home, cli)
    out["director_close"] = run_as(DIRECTOR_UID, DIRECTOR_UID, f"/home/{DIRECTOR}", [*cli, "--close"])
    out["tree_after"] = tree_modes()
    out["journal_after"] = index.read_text(encoding="utf-8")
    # A checkout that moved: the registry names a configuration that is not there.
    register(CHECKOUT.parent / "moved-away" / ".switchyard" / "provision" / f"{SLUG}.json")
    out["missing_owner"] = run_as(OWNER_UID, OWNER_UID, str(HOME), cli)
    out["missing_root"] = run_as(0, 0, "/run/root-home", cli)
    out["missing_director"] = run_as(DIRECTOR_UID, DIRECTOR_UID, f"/home/{DIRECTOR}", cli)
    register(config_path)
    # A configuration that is there and does not load.
    kept = config_path.read_bytes()
    config_path.write_text("{ not json", encoding="utf-8")
    out["broken_owner"] = run_as(OWNER_UID, OWNER_UID, str(HOME), cli)
    out["broken_root"] = run_as(0, 0, "/run/root-home", cli)
    # Well-formed JSON whose fields break the real loader: a `slot` that is an object, a `cli` that is empty (Audit of
    # a9e26d1d: the first raised an uncaught TypeError).
    for case, field, value in (("slot_object", "slot", {}), ("cli_empty", "cli", [])):
        document = json.loads(kept)
        document["roles"][0][field] = value
        config_path.write_text(json.dumps(document), encoding="utf-8")
        out[f"{case}_owner"] = run_as(OWNER_UID, OWNER_UID, str(HOME), cli)
        out[f"{case}_root"] = run_as(0, 0, "/run/root-home", cli)
    config_path.write_bytes(kept)
    server.shutdown()
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "skills",
                              "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd659.") as tmp:
        # /home is covered inside the namespace, so this suite and the tree it runs go somewhere that is not.
        stage = Path(tmp)
        os.chmod(stage, 0o755)
        shutil.copy2(Path(__file__), stage / "suite.py")
        if tree != ROOT:
            code = tree
        else:
            code = stage / "tree"
            for part in ("scripts", "config", "examples", "skills", "docs"):
                if (ROOT / part).exists():
                    shutil.copytree(ROOT / part, code / part, symlinks=True,
                                    ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run(["chmod", "-R", "a+rX", str(stage), str(code)], check=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX",
                                                                         "SWITCHYARD_"))}
        child = subprocess.run(["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private",
                                "--pid", "--fork", "--mount-proc", "--net", sys.executable, str(stage / "suite.py"),
                                "--inner", str(code), tmp], capture_output=True, text=True,
                               env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900, stdin=subprocess.DEVNULL)
        assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


def before(out: dict) -> None:
    director = out["director"]
    check(director["code"] != 0 and "PermissionError" in director["said"] and "Traceback" in director["said"],
          f"before: another tenant's account gets a traceback, as on PIE: {director}")


def candidate(out: dict) -> None:
    config_path = out["config_path"]
    full = "switchyard: pie release phase\n"
    for name in ("director", "operator"):
        result = out[name]
        said = result["said"]
        check(result["code"] == 1 and "Traceback" not in said
              and f"{config_path} is not readable from {DIRECTORS[name]}: it is in {OWNER}'s private tree." in said,
              f"{name}: the private configuration is named as not readable, with no traceback: {result}")
        check(f"shared release   {SHARED}" in said and f"live board build {LIVE}" in said
              and "board service    active; last rollout 0001 at 2026-10-23T05:33:00Z (ok): deploy finished" in said
              and said.count("(not readable from this account)") == 4,
              f"{name}: the shared release, the live build at the unit's port and root's rollout record are reported, "
              f"and the four facts only the owner or root can read are named as such: {result}")
        check(f"to compare everything, run `pkexec switchyard release-status {SLUG}`, or "
              f"`switchyard release-status {SLUG}` as {OWNER}." in said,
              f"{name}: the commands that compare everything are given exactly: {result}")
    close = out["director_close"]
    check(close["code"] == 1 and "Traceback" not in close["said"]
          and f"Closing the phase is root's: `pkexec switchyard release-status {SLUG} --close`." in close["said"],
          f"--close from another account closes nothing and names root's command: {close}")
    check(out["tree_after"] == out["tree_before"] and out["tree_before"][str(HOME)] == [OWNER_UID, "0700"]
          and out["journal_after"].count("\n") == 1,
          f"nothing was widened or written: the owner's tree keeps its owners and modes, home 0700: {out['tree_after']}")
    for name in ("owner", "root"):
        result = out[name]
        check("Traceback" not in result["said"] and result["said"].startswith(full)
              and f"live board build {LIVE}" in result["said"] and "from root-owned records only" not in result["said"],
              f"{name}: the owner and root compare everything from the configuration itself: {result}")
    for name in ("missing_owner", "missing_root"):
        said = out[name]["said"]
        check(out[name]["code"] == 1 and "Traceback" not in said and "does not exist, so the registry entry "
              f"/etc/switchyard/projects/{SLUG}.json is stale. Restore the checkout there, or have an operator remove "
              "that entry and run `sudo switchyard register <the configuration's path>`." in said
              and f"live board build {LIVE}" in said and said.count("(unknown without a usable configuration)") == 4,
              f"{name}: a registered configuration that is not there is named stale, with what fixes it, and what "
              f"depends on it is unknown rather than unreadable: {out[name]}")
    missing = out["missing_director"]
    check(missing["code"] == 1 and "Traceback" not in missing["said"] and "is not readable from" in missing["said"],
          f"another account cannot tell missing from private inside a 0700 tree, and says not readable: {missing}")
    for name in ("broken_owner", "broken_root"):
        said = out[name]["said"]
        check(out[name]["code"] == 1 and "Traceback" not in said
              and f"{config_path} is not a usable launcher configuration:" in said
              and f"It is {OWNER}'s file to repair; nothing was changed." in said and ".. It is" not in said
              and said.count("(unknown without a usable configuration)") == 4,
              f"{name}: a configuration that does not load is named, and whose it is to repair: {out[name]}")


    for case, kind in (("slot_object", "TypeError"), ("cli_empty", "IndexError")):
        for caller in ("owner", "root"):
            result = out[f"{case}_{caller}"]
            said = result["said"]
            check(result["code"] == 1 and "Traceback" not in said
                  and f"{config_path} is not a usable launcher configuration:" in said and f"({kind})." in said
                  and f"live board build {LIVE}" in said,
                  f"{case} as {caller}: a field the real loader breaks on is a diagnostic naming the failure, not a "
                  f"traceback: {result}")


DIRECTORS = {"director": DIRECTOR, "operator": OPERATOR}


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT), indent=1, default=str))
        return 0
    after = run(ROOT)
    if os.environ.get("SYRD659_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd659-before.") as tmp:
            os.chmod(tmp, 0o755)
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    print(f"release_status_cross_account_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
