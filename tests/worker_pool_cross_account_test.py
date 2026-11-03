#!/usr/bin/env python3
"""SYRD-731: `switchyard worker-pool <project> status` for a tenant whose private tree the caller may not read.

SYRD-688's User acceptance check, from the registered desktop operator, under release 0209240a:

    switchyard worker-pool mefp status
    PermissionError: [Errno 13] Permission denied:
    '/home/stellaris-agent/stellaris-bugfix/.switchyard/provision/mefp.json'

`switchyard_worker_pool_command` loaded the tenant's generated configuration first, and only its owner and root
can read it. This is SYRD-659's case for `release-status`, for the pool verbs.

Here, as root in a user, mount, pid and network namespace with real uids, MEFP's shape exactly: the owner
`stellaris-agent`'s home is mode 0700, with the configuration the product generates at
`stellaris-bugfix/.switchyard/provision/mefp.json`. The registry entry is root's and carries the `board_ui` block
MEFP's does, naming the desktop operator `eric`. The board's unit is root's, as the product renders it. A board
answers on loopback at the port that unit names, with a pull pool of ephemeral `luna-N` workers: one holds a ticket
and has a live registered process, one is registered to a process that has exited, one is idle with nothing
registered, and one is retired. The real `scripts/switchyard worker-pool mefp ...` then runs as a child that has
become each caller. The before-run is main's code, from a git archive.

One thing differs from a host. `/` is not root's inside a user namespace, so root's unit reader
(`board_identity._read_unit`) starts its walk at root's unit directory, a tmpfs root owns here, instead of at `/`.
Everything below that walk is the product's own.
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
BEFORE = "0209240afb9192814c4226f88c484c720f475d40"  # main before SYRD-731, the release the User ran
SLUG, NAME = "mefp", "Morfane's Epic Fix Patch"
OWNER, OWNER_UID = "stellaris-agent", 1002
OPERATOR, OPERATOR_UID = "eric", 1000  # the registered desktop operator
DIRECTOR, DIRECTOR_UID = "syrd-agent", 1019  # another tenant's account
HOME = Path("/home") / OWNER
CHECKOUT = HOME / "stellaris-bugfix"
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


def start_time(pid: int) -> int:
    """The kernel's start time for a process, field 22 of its stat, as the listener reads it."""
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return int(raw[raw.rindex(")") + 2:].split()[19])


def luna(index: int, *, active: bool = True) -> dict:
    return {"name": f"luna-{index}", "label": f"Luna {index}", "kind": "implementer", "active": active,
            "serial": True, "ephemeral": True, "slot": None, "runtime": "claude" if index < 3 else "codex",
            "target": f"mefp-luna-{index}:0.0", "capabilities": ["add_comment"], "template_role": "luna-1"}


def board_answers(live_pid: int, gone_pid: int, gone_start: int) -> dict:
    """What MEFP's board answers, cut down to a pull pool of four lunas."""
    document = {
        "schema": "switchyard.workflow.v1", "project": SLUG,
        "roles": [
            {"name": "director", "kind": "system", "active": True, "runtime": "codex", "slot": 0},
            {"name": "main", "kind": "implementer", "active": True, "runtime": "codex", "slot": 2},
            {"name": "audit", "kind": "reviewer", "active": True, "runtime": "claude", "slot": 1},
            luna(1), luna(2), luna(3), luna(4, active=False),
        ],
        "stages": [{"name": "in_progress", "kind": "implementation", "owners": ["main", "luna-1", "luna-2", "luna-3"]},
                   {"name": "audit", "kind": "review", "owners": ["audit"]}],
        "scheduling": {"mode": "pull", "ready_stage": "pull_ready_source_first", "release_after": "audit"},
    }
    registration = {"role": "luna-2", "runtime": "claude", "actual_target": "mefp-luna-2:0.0",
                    "process_pid": live_pid, "process_start_time": start_time(live_pid), "process_uid": OWNER_UID}
    return {
        "/api/workflow": {"revision": 7, "document": document},
        "/api/board": {"tickets": [{"id": "MEFP-41", "title": "a fix", "state": "in_progress", "assignee": "luna-2"},
                                   {"id": "MEFP-40", "title": "done", "state": "done", "assignee": "luna-1"}]},
        "/api/reservations": {"project": SLUG, "reservations": {
            "main": None, "luna-1": None, "luna-3": None,
            "luna-2": {"ticket": "MEFP-41", "state": "in_progress", "assignee": "luna-2"}}},
        "/api/runtime-assignments": {"project": SLUG, "assignments": {
            "luna-2": registration,
            "luna-3": {**registration, "role": "luna-3", "actual_target": "mefp-luna-3:0.0", "process_pid": gone_pid,
                       "process_start_time": gone_start}}},
    }


ASKED: list[tuple[str, str]] = []
ANSWERS: dict = {}


class Board(http.server.BaseHTTPRequestHandler):
    def answer(self, method: str) -> None:
        ASKED.append((method, self.path))
        found = ANSWERS.get(self.path) if method == "GET" else None
        body = json.dumps(found).encode()
        self.send_response(200 if found is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        self.answer("GET")

    def do_POST(self) -> None:  # noqa: N802
        self.answer("POST")

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
    # Nothing here may start, stop or ask a service; a call to systemctl or tmux is recorded, and says no.
    calls = Path("/run/calls")
    calls.mkdir(mode=0o777)
    calls.chmod(0o1777)
    for tool in ("systemctl", "tmux", "sudo", "runuser", "claude", "codex"):
        Path(f"/usr/local/sbin/{tool}").write_text(f'#!/bin/sh\necho "{tool} $*" >> /run/calls/$(id -u)\nexit 1\n')
        Path(f"/usr/local/sbin/{tool}").chmod(0o755)
    passwd, group = work / "passwd", work / "group"
    accounts = ((OWNER, OWNER_UID, str(HOME)), (OPERATOR, OPERATOR_UID, f"/home/{OPERATOR}"),
                (DIRECTOR, DIRECTOR_UID, f"/home/{DIRECTOR}"))
    passwd.write_text("root:x:0:0::/run/root-home:/bin/sh\nboardsvc:x:1500:1500::/nonexistent:/bin/false\n" + "".join(
        f"{name}:x:{uid}:{uid}::{home}:/bin/sh\n" for name, uid, home in accounts), encoding="utf-8")
    group.write_text("root:x:0:\nboardsvc:x:1500:\n" + "".join(f"{name}:x:{uid}:\n" for name, uid, _ in accounts),
                     encoding="utf-8")
    sh("mount", "--bind", str(passwd), "/etc/passwd")
    sh("mount", "--bind", str(group), "/etc/group")
    os.makedirs("/run/root-home", mode=0o700)
    for name, uid, home in accounts:
        os.makedirs(home, mode=0o700)
        os.chown(home, uid, uid)
        os.chmod(home, 0o700)
    for directory in (CHECKOUT, CHECKOUT / ".switchyard", PROVISION):
        directory.mkdir(mode=0o700)
        os.chown(directory, OWNER_UID, OWNER_UID)

    sys.path.insert(0, str(release))
    from scripts import team_launcher as launcher
    from scripts.ticket_board.project_provision import build_plan

    plan = build_plan(project=SLUG, project_name=NAME, owner_user=OWNER, owner_home=HOME, source_repo=release,
                      service_user="boardsvc", project_repository=CHECKOUT)
    config_path = launcher.write_new_project_launcher_artifacts(plan, PROVISION, repository=CHECKOUT,
                                                                project_name=NAME, print_func=lambda *a, **k: None)
    # MEFP declares its pool in that configuration; the operator never reads it, the owner and root do.
    generated = json.loads(config_path.read_text(encoding="utf-8"))
    generated["worker_pool"] = {"name": "luna", "runtime": "claude", "size": 3}
    config_path.write_text(json.dumps(generated, indent=2), encoding="utf-8")
    rendered = launcher.render_privileged_artifacts(plan)
    unit = Path("/etc/systemd/system") / f"{SLUG}-ticket-board.service"
    unit.write_bytes(rendered[unit.name])
    unit.chmod(0o644)
    registry = Path("/etc/switchyard/projects")
    registry.mkdir()
    entry = registry / f"{SLUG}.json"

    def register(path: Path) -> None:
        # MEFP's entry, field for field: the configuration's path, and the board UI record naming the operator.
        entry.write_text(json.dumps({
            "agent_clis": ["claude", "codex"],
            "board_ui": {"recorded_at": "2026-10-19T02:19:22Z", "recorded_by": OPERATOR, "role_accounts": [],
                         "roles_group": f"{SLUG}-roles", "service_user": "boardsvc",
                         "source": "the host's approved desktop owner", "tenant_user": OWNER,
                         "users": [{"account": OPERATOR, "uid": OPERATOR_UID}]},
            "config_path": str(path), "name": NAME, "schema": "switchyard.project-registry.v1", "slug": SLUG,
        }, indent=2, sort_keys=True), encoding="utf-8")
        entry.chmod(0o644)

    register(config_path)
    # luna-2's process: live, the owner's. luna-3's: one that ran and exited.
    live = subprocess.Popen(["sleep", "600"], user=OWNER_UID, group=OWNER_UID, extra_groups=[])
    gone = subprocess.Popen(["sleep", "0.1"], user=OWNER_UID, group=OWNER_UID, extra_groups=[])
    gone_start = start_time(gone.pid)
    gone.wait()
    ANSWERS.update(board_answers(live.pid, gone.pid, gone_start))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", plan.port), Board)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    launch = Path("/opt/run-cli.py")
    launch.write_text("import runpy, sys\nfrom pathlib import Path\nsys.path.insert(0, %r)\n"
                      "from scripts.ticket_board import board_identity\n"
                      "board_identity.ANCHOR = Path('/etc/systemd/system')\n"
                      "sys.argv = sys.argv[1:]\nrunpy.run_path(sys.argv[0], run_name='__main__')\n" % str(release))
    launch.chmod(0o644)
    cli = [str(launch), str(release / "scripts" / "switchyard"), "worker-pool", SLUG]
    out: dict = {"config_path": str(config_path), "port": plan.port, "live_pid": live.pid, "gone_pid": gone.pid}
    # The owner and root first: their readiness check may write the owner's own caches. Then the tree is taken, and
    # nothing the accounts outside the tenant run may change it.
    for name, (uid, home) in {"owner": (OWNER_UID, str(HOME)), "root": (0, "/run/root-home")}.items():
        out[name] = run_as(uid, uid, home, [*cli, "status"])
    ASKED.clear()
    out["tree_before"] = tree_modes()
    for name, (uid, home) in {"operator": (OPERATOR_UID, f"/home/{OPERATOR}"),
                              "director": (DIRECTOR_UID, f"/home/{DIRECTOR}")}.items():
        out[name] = run_as(uid, uid, home, [*cli, "status"])
    out["operator_list"] = run_as(OPERATOR_UID, OPERATOR_UID, f"/home/{OPERATOR}", [*cli, "list"])
    out["status_asked"] = list(ASKED)
    ASKED.clear()
    # The verbs that act, or need what only the configuration says, as the operator.
    for verb in (["stop", "luna-2"], ["start", "luna-1"], ["apply", "--apply"], ["preflight"], ["cohorts"]):
        out["operator_" + verb[0]] = run_as(OPERATOR_UID, OPERATOR_UID, f"/home/{OPERATOR}", [*cli, *verb])
    out["acting_asked"] = list(ASKED)
    out["tree_after"] = tree_modes()
    # A checkout that moved: the registry names a configuration that is not there.
    register(CHECKOUT.parent / "moved-away" / ".switchyard" / "provision" / f"{SLUG}.json")
    out["missing_owner"] = run_as(OWNER_UID, OWNER_UID, str(HOME), [*cli, "status"])
    out["missing_root"] = run_as(0, 0, "/run/root-home", [*cli, "status"])
    out["missing_operator"] = run_as(OPERATOR_UID, OPERATOR_UID, f"/home/{OPERATOR}", [*cli, "status"])
    register(config_path)
    # A configuration that is there and does not load.
    kept = config_path.read_bytes()
    config_path.write_text("{ not json", encoding="utf-8")
    out["broken_owner"] = run_as(OWNER_UID, OWNER_UID, str(HOME), [*cli, "status"])
    out["broken_owner_stop"] = run_as(OWNER_UID, OWNER_UID, str(HOME), [*cli, "stop", "luna-2"])
    config_path.write_bytes(kept)
    # A board that says it is another project's, then one that is down.
    ANSWERS["/api/reservations"] = {**ANSWERS["/api/reservations"], "project": "otto"}
    out["impostor_operator"] = run_as(OPERATOR_UID, OPERATOR_UID, f"/home/{OPERATOR}", [*cli, "status"])
    server.shutdown()
    server.server_close()
    out["down_operator"] = run_as(OPERATOR_UID, OPERATOR_UID, f"/home/{OPERATOR}", [*cli, "status"])
    # Root's unit gone: the board cannot be found from root's records.
    unit.unlink()
    out["no_unit_operator"] = run_as(OPERATOR_UID, OPERATOR_UID, f"/home/{OPERATOR}", [*cli, "status"])
    live.kill()
    live.wait()
    out["calls"] = {path.name: path.read_text() for path in calls.iterdir()}
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "skills",
                              "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd731.") as tmp:
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
    operator = out["operator"]
    check(operator["code"] != 0 and "PermissionError" in operator["said"] and "Traceback" in operator["said"],
          f"before: the desktop operator gets a traceback, as in SYRD-688's acceptance check: {operator}")


def candidate(out: dict) -> None:
    config_path = out["config_path"]
    base = f"http://127.0.0.1:{out['port']}"
    for name, account in (("operator", OPERATOR), ("director", DIRECTOR)):
        result = out[name]
        said = result["said"]
        check(result["code"] == 0 and "Traceback" not in said
              and f"{config_path} is not readable from {account}: it is in {OWNER}'s private tree." in said,
              f"{name}: status works, and names the private configuration as not readable: {result}")
        check(f"switchyard: {SLUG} worker pool, from its board at {base} (the port root's unit names) only" in said,
              f"{name}: it reads the board root's unit names, and says so: {result}")
        check("switchyard: luna: 3 worker(s) declared on the board, 1 retired; 1 running, 2 stopped; "
              "idle ephemeral workers: 2 stopped, 0 running" in said,
              f"{name}: pool membership, and whether the idle ephemeral workers are stopped: {result}")
        check(f"  luna-2 (mefp-luna-2:0.0): running: process {out['live_pid']} is registered with the board; "
              "holding MEFP-41; serial slot held by MEFP-41 (in_progress, assigned to luna-2); claims ready work"
              in said, f"{name}: the worker holding work, its live process and its slot: {result}")
        check("  luna-1 (mefp-luna-1:0.0): stopped: no process is registered with the board; holding nothing; "
              "serial slot free; claims ready work" in said, f"{name}: the idle worker is stopped: {result}")
        check(f"  luna-3 (mefp-luna-3:0.0): stopped: its registered process is gone (pid {out['gone_pid']} no longer "
              "exists); holding nothing; serial slot free" in said,
              f"{name}: a registration whose process exited is not taken for a running worker: {result}")
        check("luna-4" not in said and "main (" not in said and "audit (" not in said,
              f"{name}: only live pool members are listed: {result}")
        check(f"is not read from this account; `switchyard worker-pool {SLUG} status` as {OWNER} reports it." in said,
              f"{name}: what it cannot read is named, with who can: {result}")
    check(out["operator_list"]["said"] == out["operator"]["said"] and out["operator_list"]["code"] == 0,
          f"list is the same report: {out['operator_list']}")
    check(out["status_asked"] and all(method == "GET" for method, _path in out["status_asked"])
          and {path for _method, path in out["status_asked"]}
          == {"/api/workflow", "/api/board", "/api/reservations", "/api/runtime-assignments"},
          f"it only reads the board, and only those four answers: {out['status_asked']}")
    for verb, shown in (("stop", "stop luna-2"), ("start", "start luna-1"), ("apply", "apply"),
                        ("preflight", "preflight"), ("cohorts", "cohorts")):
        result = out["operator_" + verb]
        check(result["code"] == 1 and "Traceback" not in result["said"]
              and f"switchyard: worker-pool {verb} needs {SLUG}'s own configuration, so nothing was read or changed; "
              f"run `switchyard worker-pool {SLUG} {shown}` as {OWNER}." in result["said"],
              f"operator {verb}: refused before it acts, naming who can run it: {result}")
    check(out["acting_asked"] == [], f"no acting verb reached the board: {out['acting_asked']}")
    check(out["tree_after"] == out["tree_before"] and out["tree_before"][str(HOME)] == [OWNER_UID, "0700"]
          and out["tree_before"][str(config_path)][0] == OWNER_UID,
          f"nothing was widened: the owner's tree keeps its owners and modes, home 0700: {out['tree_after']}")
    check(not any(caller in out["calls"] for caller in (str(OPERATOR_UID), str(DIRECTOR_UID))),
          f"neither account outside the tenant ran systemctl, tmux, sudo, runuser or a provider CLI: {out['calls']}")
    for name in ("owner", "root"):
        said = out[name]["said"]
        check(out[name]["code"] == 0 and "Traceback" not in said and "from its board at" not in said
              and "not readable from" not in said and "switchyard: luna: 3 worker(s), 0 ready, 0 running;" in said
              and "  luna-2 (mefp-luna-2:0.0): not ready, stopped, holding MEFP-41;" in said,
              f"{name}: the owner and root load the configuration itself, with its readiness, as before: {out[name]}")
    for name in ("missing_owner", "missing_root"):
        said = out[name]["said"]
        check(out[name]["code"] == 1 and "Traceback" not in said and "does not exist, so the registry entry "
              f"/etc/switchyard/projects/{SLUG}.json is stale. Restore the checkout there, or have an operator remove "
              "that entry and run `sudo switchyard register <the configuration's path>`." in said
              and "luna: 3 worker(s) declared on the board" in said
              and "is not read without a usable configuration." in said,
              f"{name}: a missing configuration is named stale, with its fix, and the board is still reported: "
              f"{out[name]}")
    missing = out["missing_operator"]
    check(missing["code"] == 0 and "Traceback" not in missing["said"] and "is not readable from" in missing["said"],
          f"the operator cannot tell missing from private inside a 0700 tree, and says not readable: {missing}")
    broken = out["broken_owner"]
    check(broken["code"] == 1 and "Traceback" not in broken["said"]
          and f"{config_path} is not a usable launcher configuration:" in broken["said"]
          and f"It is {OWNER}'s file to repair; nothing was changed." in broken["said"]
          and "luna: 3 worker(s) declared on the board" in broken["said"],
          f"a configuration that does not load is named, whose it is, and the board is reported: {broken}")
    stop = out["broken_owner_stop"]
    check(stop["code"] == 1 and "Traceback" not in stop["said"]
          and stop["said"].rstrip().endswith(f"worker-pool stop needs {SLUG}'s own configuration, so nothing was read "
                                             "or changed."),
          f"stop with a broken configuration refuses, with no command to run as someone else: {stop}")
    impostor = out["impostor_operator"]
    check(impostor["code"] == 1 and "Traceback" not in impostor["said"]
          and f"says it is otto's; its pool is not reported." in impostor["said"]
          and "luna-2 (" not in impostor["said"],
          f"a board that says it is another project's is not reported as this one's: {impostor}")
    down = out["down_operator"]
    check(down["code"] == 1 and "Traceback" not in down["said"]
          and f"{SLUG}'s board at {base} did not answer /api/workflow with a workflow document; is it running?"
          in down["said"], f"a board that is down is said, not a traceback: {down}")
    no_unit = out["no_unit_operator"]
    check(no_unit["code"] == 1 and "Traceback" not in no_unit["said"]
          and f"{SLUG}'s board cannot be found from root's records, so its pool is not reported:" in no_unit["said"],
          f"without root's unit, no port is guessed: {no_unit}")


def malformed_board_answers() -> None:
    """A board that answers oddly is reported as far as it answered, never a traceback: in-process, through the
    reader's own seams, with a private configuration."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts import worker_pool_command as w

    good = board_answers(os.getpid(), 0, 0)
    good["/api/runtime-assignments"]["assignments"].pop("luna-3")
    private = PermissionError(13, "Permission denied")

    def status(answers: dict) -> tuple[int, str]:
        lines: list[str] = []
        code = w.worker_pool_without_configuration(
            SLUG, action="status", config_path=Path("/nonexistent/mefp.json"), problem=private,
            address=lambda _slug: (26623, "MEFP"), board_get=lambda url: answers.get(url.split("26623", 1)[1]),
            print_func=lines.append)
        return code, "\n".join(lines)

    cases = {
        "reservations naming a slot with a string": {**good, "/api/reservations": {
            "project": SLUG, "reservations": {"luna-1": "MEFP-9", "luna-2": None, "luna-3": None}}},
        "tickets that are not a list": {**good, "/api/board": {"tickets": "MEFP-41"}},
        "a ticket that is not an object": {**good, "/api/board": {"tickets": ["MEFP-41"]}},
        "assignments that are not an object": {**good, "/api/runtime-assignments": {"project": SLUG,
                                                                                    "assignments": []}},
        "roles that are not objects": {**good, "/api/workflow": {"document": {
            **good["/api/workflow"]["document"], "roles": ["luna-1", 7, *good["/api/workflow"]["document"]["roles"]]}}},
        "scheduling with stages that are not objects": {**good, "/api/workflow": {"document": {
            **good["/api/workflow"]["document"], "stages": ["in_progress"]}}},
    }
    for name, answers in cases.items():
        try:
            code, said = status(answers)
        except Exception as exc:  # noqa: BLE001 - the failure under test
            raise AssertionError(f"{name}: raised {exc!r}") from None
        check(code == 0 and "luna: 3 worker(s) declared on the board" in said,
              f"{name}: the pool is still reported: {said}")
    _code, said = status(cases["reservations naming a slot with a string"])
    check("  luna-1 (mefp-luna-1:0.0): stopped: no process is registered with the board; holding nothing; "
          "serial slot unknown: the board does not count it as an implementer" in said
          and "idle ephemeral workers: 1 stopped" in said,
          f"a slot answered with a string is unknown, and not counted idle: {said}")
    _code, said = status(cases["tickets that are not a list"])
    check(said.count("holding unknown: the board did not report its tickets") == 3,
          f"tickets the board did not give as objects are unknown, not nothing: {said}")
    _code, said = status(cases["assignments that are not an object"])
    check("process state unknown" in said and said.count("process unknown:") == 3 and " stopped:" not in said,
          f"no assignments is not every worker stopped: {said}")
    for answers, expected in (({}, "is it running?"), ({"/api/workflow": [1]}, "is it running?"),
                              ({"/api/workflow": {"document": None}}, "it runs the built-in workflow"),
                              ({"/api/workflow": {"document": {"roles": [{"name": "main"}]}}},
                               "declares no pool worker")):
        code, said = status(answers)
        check(code == 1 and expected in said, f"a board with no pool to report says why: {answers} {said}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT), indent=1, default=str))
        return 0
    malformed_board_answers()
    after = run(ROOT)
    if os.environ.get("SYRD731_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd731-before.") as tmp:
            os.chmod(tmp, 0o755)
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    print(f"worker_pool_cross_account_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
