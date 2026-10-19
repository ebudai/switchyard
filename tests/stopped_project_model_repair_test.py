#!/usr/bin/env python3
"""SYRD-593: the repair the model gate prints runs while the project is stopped.

Otto would not start: its designer was configured for a model the owner's
`agy` no longer listed, so the launch gate refused every role and printed

    switchyard set-role-runtime otto designer --cli agy --model <offered>

which only the Director could run -- and the Director was one of the roles the
gate had just declined to start.

This follows that printed line, exactly, from the stopped project to a launch
that passes the gate. It runs as real identities in a user namespace
(`unshare --map-auto --map-root-user --mount --net --pid`), with a namespace
`/etc/passwd` naming the desktop operator (`deskop`, 1000) and the tenant's
owner (`ottoowner`, 1019):

1. the owner's launch (`switchyard_main(["otto"])`, the bare launch the bridge
   runs) is refused by the real gate, which prints the remedy;
2. the operator types that remedy into the shipped `scripts/switchyard`, which
   crosses to root through `sudo`, as it does for anyone not the owner;
3. root runs what sudo was asked to run, and the role's model is repaired:
   checked against the owner's own catalog, with the board left unwritten;
4. the owner's launch is run again and passes the gate.

The tenant is workflow-driven, as Otto is: the declared workflow (the shipped
example, every launched role on agy) is in its config, `workflow.json`,
`plan.json` and the project artifact, and every copy is still the board's after
the repair.

Then the limits: root is refused while the Director or the role itself has a
tmux session, while tmux cannot say, for a model the owner does not offer or a
catalog that cannot be read, and for anything but the model. Root's no-op does
not bring a stale copy of the declaration into line, and a journal root writes
is the owner's. The owner's own account is still refused without the
Director. Main (`BEFORE`) runs step 3 and refuses it, which is the outage.

Stand-ins: `agy` (it lists models, and answers differently for the owner, as
on the live host), `sudo` (as root it switches account with `setpriv`; as
anyone else it records what root was asked to run), and the board (an HTTP
server that serves the workflow and records any write). tmux, the accounts,
file ownership and the launcher are real. The launch's systemd resume, its
desktop step and the pane start are recorded rather than run.
"""

from __future__ import annotations

import http.server
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import io
import tempfile
import threading
from pathlib import Path

import live_boundary  # noqa: E402

live_boundary.isolate()  # SYRD-627: no live home, pane store or runtime directory, here or in a child

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "c9fd9c6350751e32446281bc1abde79a39503d5b"  # main before SYRD-593
PROJECT = "otto"
OPERATOR, OWNER = ("deskop", 1000), ("ottoowner", 1019)
CONFIGURED = "gemini-3.7-flash-high"
OWNER_OFFERS = ["gemini-4-argon-medium", "gemini-3.5-flash"]
DIRECTOR_MODEL = "gemini-3.5-flash"
FIXTURE = Path("/usr/local/lib/fixture")
HOME = Path("/home") / OWNER[0]
PROVISION = HOME / "Projects" / PROJECT / ".switchyard" / "provision"
CONFIG = PROVISION / f"{PROJECT}.json"
#: Every other tenant copy of the declared workflow: workflow.json, plan.json, the project artifact.
COPIES = (PROVISION / "workflow.json", PROVISION / "plan.json", PROVISION.parent / f"{PROJECT}.project.json")
PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

failures: list[str] = []
CHECKS = 0


def check(condition: object, message: str) -> None:
    global CHECKS
    CHECKS += 1
    print(("ok   " if condition else "FAIL ") + message, flush=True)
    if not condition:
        failures.append(message)


STUB_AGY = f"""#!/bin/sh
[ "$1" = models ] || {{ echo "fixture agy: unexpected $*" >&2; exit 2; }}
[ -e {FIXTURE}/agy-broken ] && {{ echo "agy: could not reach the model service" >&2; exit 1; }}
if [ "$(id -u)" = {OWNER[1]} ]; then list="{' '.join(OWNER_OFFERS)}"; else list="{CONFIGURED} {' '.join(OWNER_OFFERS)}"; fi
for model in $list; do printf '%s  a model\\n' "$model"; done
"""

STUB_SUDO = f"""#!/bin/sh
# sudo [-n] [-H] [-v] [-u USER] command...
user=root
while [ $# -gt 0 ]; do
  case "$1" in
    -n|-H) shift ;;
    -v) exit 0 ;;
    -u) user=$2; shift 2 ;;
    --) shift; break ;;
    -*) echo "fixture sudo: unexpected option $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
if [ "$(id -u)" != 0 ]; then
  {{ id -un; for word in "$@"; do printf '%s\\n' "$word"; done; }} > {FIXTURE}/sudo/request
  exit 0
fi
[ "$user" = root ] && exec "$@"
uid=$(getent passwd "$user" | cut -d: -f3); gid=$(getent passwd "$user" | cut -d: -f4)
home=$(getent passwd "$user" | cut -d: -f6)
exec setpriv --reuid="$uid" --regid="$gid" --init-groups env -i HOME="$home" USER="$user" LOGNAME="$user" PATH={PATH} "$@"
"""

#: The owner's launch, through the real dispatcher. Only what reaches systemd,
#: the desktop and the panes is recorded instead of run.
LAUNCH = r"""
import json, sys
sys.path[:0] = [sys.argv[1]]
from scripts import team_launcher as launcher
launched = []
launcher.resume_tenant = lambda config, config_path=None: []
launcher.prepare_project_desktop = lambda config, **_kwargs: config
def launch_project(config, **_kwargs):
    launched.append({role.role: role.model for role in config.roles})
    return 0
launcher.launch_project = launch_project
code = launcher.switchyard_main([sys.argv[2]])
print("LAUNCH " + json.dumps({"code": code, "launched": launched}))
"""


#: The board's declared workflow: the shipped example, every launched role on agy and the designer an
#: active implementer. Set by the namespace run once it can import the tree.
DOCUMENT: dict = {}


def workflow_document(tree: Path) -> dict:
    from scripts.ticket_board.workflow_config import validate

    document = json.loads((tree / "examples" / "workflows" / "inspection.json").read_text(encoding="utf-8"))
    document["project"] = PROJECT
    for role in document["roles"]:
        if role["name"] == "designer":
            role.update(kind="implementer", active=True)
        if role.get("runtime"):
            role.update(runtime="agy", target=role["target"].replace("cerulean-", f"{PROJECT}-", 1))
    return validate(document, project=PROJECT)


class Board(http.server.BaseHTTPRequestHandler):
    writes: list[str] = []

    def do_GET(self) -> None:
        if self.path == "/api/workflow":
            body = json.dumps({"document": DOCUMENT, "revision": 3}).encode()
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write(self) -> None:
        Board.writes.append(f"{self.command} {self.path}")
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_PATCH = do_DELETE = _write

    def log_message(self, *_args) -> None:
        pass


# ---------------------------------------------------------------- inside the namespace


def as_user(user: tuple[str, int], argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    name, uid = user
    home = "/root" if uid == 0 else f"/home/{name}"
    environ = {"HOME": home, "USER": name, "LOGNAME": name, "PATH": PATH, "LANG": "C.UTF-8", **(env or {})}
    prefix = [] if uid == 0 else ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--init-groups"]
    return subprocess.run([*prefix, "env", "-i", *(f"{k}={v}" for k, v in environ.items()), *argv],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, cwd="/")


def setup(tree: Path, board_port: int) -> bytes:
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    for target, mode in (("/home", "0755"), ("/tmp", "1777"), ("/run", "0755"), ("/usr/local", "0755"),
                         ("/etc/switchyard", "0755")):
        Path(target).mkdir(parents=True, exist_ok=True)
        subprocess.run(["mount", "-t", "tmpfs", "-o", f"mode={mode}", "tmpfs", target], check=True)
    etc = Path(tempfile.mkdtemp(prefix="etc."))
    (etc / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/sh\nnobody:x:65534:65534:nobody:/:/usr/bin/nologin\n"
        f"{OPERATOR[0]}:x:{OPERATOR[1]}:{OPERATOR[1]}::/home/{OPERATOR[0]}:/bin/sh\n"
        f"{OWNER[0]}:x:{OWNER[1]}:{OWNER[1]}::/home/{OWNER[0]}:/bin/sh\n", encoding="utf-8")
    (etc / "group").write_text(
        "root:x:0:\nnobody:x:65534:\n"
        f"{OPERATOR[0]}:x:{OPERATOR[1]}:\n{OWNER[0]}:x:{OWNER[1]}:\n", encoding="utf-8")
    for name in ("passwd", "group"):
        (etc / name).chmod(0o644)
        subprocess.run(["mount", "--bind", str(etc / name), f"/etc/{name}"], check=True)
    bin_dir = Path("/usr/local/bin")
    bin_dir.mkdir()
    for name, body in (("agy", STUB_AGY), ("sudo", STUB_SUDO)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    (FIXTURE / "sudo").mkdir(parents=True)
    (FIXTURE / "sudo").chmod(0o1777)

    for user in (OPERATOR, OWNER):
        home = Path("/home") / user[0]
        home.mkdir()
        os.chown(home, user[1], user[1])
        home.chmod(0o750)
    # The owner's project, as provisioning and `workflow apply` leave a workflow-driven
    # tenant like Otto: the declared workflow in its config, workflow.json, plan.json and
    # the project artifact, and the designer configured for a model the owner does not list.
    sys.path[:0] = [str(tree / "tests"), str(tree)]
    from team_launcher_test_helpers import _mark_first_run_setup_complete, _write_first_run_auth_config  # noqa: E402
    from scripts import team_launcher, workflow_manage  # noqa: E402
    from scripts.workflow_launcher import projection_files  # noqa: E402

    DOCUMENT.update(workflow_document(tree))
    launched = [role["name"] for role in DOCUMENT["roles"] if role.get("runtime")]
    PROVISION.mkdir(parents=True)
    written = _write_first_run_auth_config(
        PROVISION, roles=[(name, "agy") for name in launched], owner_user=OWNER[0],
        role_models={"director": DIRECTOR_MODEL, "designer": CONFIGURED},
    )
    raw = json.loads(written.read_text(encoding="utf-8"))
    raw["board_url"] = f"http://127.0.0.1:{board_port}"
    raw["session_dir"] = str(HOME / "sessions")
    check(written == CONFIG, f"the fixture configuration is where the registry will say: {written}")
    CONFIG.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for path in COPIES[1:3]:
        path.write_text(json.dumps({"project": PROJECT, "workflow": DOCUMENT}) + "\n", encoding="utf-8")
    workflow_manage.apply_files(projection_files(CONFIG, DOCUMENT))
    check(all(path.exists() for path in COPIES) and "workflow" in json.loads(CONFIG.read_text(encoding="utf-8")),
          "the tenant is workflow-driven, every copy of the declaration in place")
    _mark_first_run_setup_complete(HOME, team_launcher.load_project_config(PROJECT, CONFIG), clis={"agy"})
    subprocess.run(["chown", "-R", f"{OWNER[1]}:{OWNER[1]}", str(HOME)], check=True)
    CONFIG.chmod(0o640)
    registry = Path("/etc/switchyard/projects")
    registry.mkdir(parents=True)
    (registry / f"{PROJECT}.json").write_text(json.dumps({
        "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA, "slug": PROJECT, "name": PROJECT,
        "config_path": str(CONFIG),
    }) + "\n", encoding="utf-8")
    return snapshot()


def snapshot() -> bytes:
    """The configuration and every copy of the declaration, byte for byte."""
    return b"\0".join(path.read_bytes() for path in (CONFIG, *COPIES))


def designer_model() -> str:
    roles = json.loads(CONFIG.read_text(encoding="utf-8"))["roles"]
    return next(str(role.get("model") or "") for role in roles if role["role"] == "designer")


def launch(tree: Path) -> tuple[dict, str]:
    ran = as_user(OWNER, [sys.executable, "-B", "-c", LAUNCH, str(tree), PROJECT])
    said = ran.stdout + ran.stderr
    line = next((line for line in ran.stdout.splitlines() if line.startswith("LAUNCH ")), "")
    return (json.loads(line[len("LAUNCH "):]) if line else {"code": None, "launched": []}), said


def as_root_via_sudo(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """What sudo hands root: its own reset environment, naming the operator."""
    return as_user(("root", 0), command, env={"SUDO_USER": OPERATOR[0], "SUDO_UID": str(OPERATOR[1]),
                                               "SUDO_GID": str(OPERATOR[1]), **(env or {})})


def owner_tmux(*argv: str) -> subprocess.CompletedProcess:
    return as_user(OWNER, ["tmux", *argv])


def in_process_switch(tree: Path, **kwargs) -> str:
    """`switch_role_runtime` itself, as root, with the real runner: the authority past the command's own checks."""
    from scripts import role_runtime, team_launcher

    config = team_launcher.load_project_config(PROJECT, CONFIG)
    try:
        role_runtime.switch_role_runtime(config, config_path=CONFIG, role_name="designer", runtime="agy",
                                         environ={}, print_func=lambda _line: None, **kwargs)
    except SystemExit as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - a fault reached past the refusals is reported, not raised
        return f"(raised {exc!r})"
    return "(no refusal)"


def inner(base: Path, tree: Path, before: Path | None) -> int:
    # The copies live under the host's /tmp, which the namespace replaces with
    # its own (the owner's tmux socket goes there), so they are seen at /mnt.
    subprocess.run(["mount", "--bind", str(base), "/mnt"], check=True)
    tree = Path("/mnt") / tree.relative_to(base)
    before = Path("/mnt") / before.relative_to(base) if before is not None else None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Board)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original = setup(tree, server.server_port)
    wrapper = str(tree / "scripts" / "switchyard")

    # 1. The stopped project's launch, refused by the gate.
    result, said = launch(tree)
    check(result["code"] == 1 and result["launched"] == [], f"the gate refuses the launch before any role starts: {result}")
    check(f"not starting designer: agy on {OWNER[0]} does not offer {CONFIGURED!r}" in said, f"naming the role: {said[-600:]}")
    remedy_line = next((line.split("repair it with: ", 1)[1] for line in said.splitlines() if "repair it with: " in line), "")
    remedy = shlex.split(remedy_line)
    check(remedy == ["switchyard", "set-role-runtime", PROJECT, "designer", "--cli", "agy", "--model", OWNER_OFFERS[0]],
          f"the printed repair names the owner's first offered model: {remedy_line!r}")
    check(snapshot() == original, "and the gate changed nothing")
    if not remedy:
        print(json.dumps({"failures": failures, "checks": CHECKS}))
        return 1

    # 2. The operator types it. Not the owner, so the command crosses to root through sudo.
    request = FIXTURE / "sudo" / "request"
    typed = as_user(OPERATOR, [sys.executable, "-B", wrapper, *remedy[1:]])
    asked = request.read_text(encoding="utf-8").splitlines() if request.exists() else []
    check(asked[:1] == [OPERATOR[0]] and asked[1:] == [wrapper, *remedy[1:]],
          f"the operator's command asks sudo to run exactly the printed repair as root: {asked} "
          f"(exit {typed.returncode}: {(typed.stdout + typed.stderr)[-400:]})")
    check(snapshot() == original, "and the operator's own account changed nothing")

    # Main, given the same thing to run as root, refuses it: the outage.
    if before is not None:
        old = as_root_via_sudo([sys.executable, "-B", str(before / "scripts" / "switchyard"), *remedy[1:]])
        check(old.returncode != 0 and "requires TICKET_BOARD_CALLER_ROLE=director" in old.stdout + old.stderr,
              f"main refuses that repair as root, which is the outage: {(old.stdout + old.stderr)[-300:]}")
        check(snapshot() == original, "and changes nothing")
    else:
        check(False, f"main {BEFORE} could be read to run the same repair on")

    # Root while the Director has a session: refused.
    owner_tmux("new-session", "-d", "-s", f"{PROJECT}-director", "sleep 600")
    ran = as_root_via_sudo([sys.executable, "-B", *asked[1:]])
    out = ran.stdout + ran.stderr
    # The designer's session is plainly absent on that same server ("can't find session").
    check(ran.returncode != 0 and f"director is running ({PROJECT}-director)" in out and "could not tell" not in out,
          f"root is refused while the Director runs: {out[-300:]}")
    check(snapshot() == original, "and nothing changed")
    owner_tmux("kill-server")

    # Root while the role itself has a session: refused.
    owner_tmux("new-session", "-d", "-s", f"{PROJECT}-designer", "sleep 600")
    ran = as_root_via_sudo([sys.executable, "-B", *asked[1:]])
    out = ran.stdout + ran.stderr
    check(ran.returncode != 0 and f"designer is running ({PROJECT}-designer)" in out and "could not tell" not in out,
          f"root is refused while the role itself runs: {out[-300:]}")
    check(snapshot() == original, "and nothing changed")
    owner_tmux("kill-server")

    # tmux cannot answer: the owner's socket directory is not the owner's. Not knowing is not stopped.
    socket_dir = Path(f"/tmp/tmux-{OWNER[1]}")
    shutil.rmtree(socket_dir, ignore_errors=True)
    socket_dir.mkdir(mode=0o700)
    ran = as_root_via_sudo([sys.executable, "-B", *asked[1:]])
    check(ran.returncode != 0 and "could not tell whether director is running" in ran.stdout + ran.stderr,
          f"root is refused when tmux cannot say: {(ran.stdout + ran.stderr)[-300:]}")
    check(snapshot() == original, "and nothing changed")
    socket_dir.rmdir()

    # Nor when a server's socket is there and the account cannot open it.
    socket_dir.mkdir(mode=0o700)
    os.chown(socket_dir, OWNER[1], OWNER[1])
    server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_socket.bind(str(socket_dir / "default"))
    (socket_dir / "default").chmod(0)
    ran = as_root_via_sudo([sys.executable, "-B", *asked[1:]])
    out = ran.stdout + ran.stderr
    check(ran.returncode != 0 and "could not tell whether director is running" in out and "Permission denied" in out,
          f"root is refused when tmux cannot open the account's socket: {out[-300:]}")
    check(snapshot() == original, "and nothing changed")
    server_socket.close()
    shutil.rmtree(socket_dir)

    # The owner's own account, without the Director: refused, and told the route.
    ran = as_user(OWNER, [sys.executable, "-B", wrapper, *remedy[1:]])
    out = ran.stdout + ran.stderr
    check(ran.returncode != 0 and "requires TICKET_BOARD_CALLER_ROLE=director" in out and "root may repair" in out,
          f"the owner's account is not the Director: {out[-300:]}")
    check(snapshot() == original, "and nothing changed")

    # Anything but the model, as root: refused.
    ran = as_root_via_sudo([sys.executable, "-B", wrapper, "set-role-runtime", PROJECT, "designer", "--cli", "codex",
                            "--model", "gpt-5.5"])
    check(ran.returncode != 0 and "changes designer's runtime, which only the Director may do" in ran.stdout + ran.stderr,
          f"root may not change the runtime: {(ran.stdout + ran.stderr)[-300:]}")
    check(snapshot() == original, "and nothing changed")
    said = in_process_switch(tree, model=OWNER_OFFERS[0], effort="high")
    check("effort or arguments, which only the Director may do" in said, f"nor the effort: {said}")
    said = in_process_switch(tree, model=OWNER_OFFERS[0], force=True, reason="because")
    check("--force interrupts a working role" in said, f"nor force past a busy role: {said}")
    said = in_process_switch(tree, model=CONFIGURED)
    check(f"does not offer {CONFIGURED!r}" in said and OWNER_OFFERS[0] in said,
          f"nor set a model the owner's account does not offer: {said}")
    (FIXTURE / "agy-broken").write_text("", encoding="utf-8")
    said = in_process_switch(tree, model=OWNER_OFFERS[0])
    check("could not list its models" in said, f"nor one that cannot be checked: {said}")
    (FIXTURE / "agy-broken").unlink()
    said = in_process_switch(tree, model="", dry_run=True)
    check(said == "(no refusal)", f"the runtime's own default, the gate's other printed repair, is allowed: {said}")
    check(snapshot() == original and Board.writes == [], f"none of that changed anything: {Board.writes}")

    # Root's no-op leaves a stale copy of the declaration alone: bringing copies into line
    # with the board is the Director's repair, not a model's.
    plan_bytes = COPIES[1].read_bytes()
    plan = json.loads(plan_bytes)
    next(role for role in plan["workflow"]["roles"] if role["name"] == "app")["runtime"] = "codex"
    COPIES[1].write_text(json.dumps(plan) + "\n", encoding="utf-8")
    stale = snapshot()
    ran = as_root_via_sudo([sys.executable, "-B", wrapper, "set-role-runtime", PROJECT, "director", "--cli", "agy",
                            "--model", DIRECTOR_MODEL])
    check(ran.returncode == 0 and "nothing to change" in ran.stdout and snapshot() == stale,
          f"root's no-op rewrites no stale copy: {(ran.stdout + ran.stderr)[-300:]}")
    COPIES[1].write_bytes(plan_bytes)
    check(snapshot() == original, "(the copy put back)")

    # A journal root writes is the owner's, as the files it describes are.
    from scripts.role_runtime import RuntimeJournal

    journal = HOME / ".journal-probe.json"
    RuntimeJournal(project=PROJECT, role="designer", config_path=str(CONFIG)).write(journal)
    check(journal.stat().st_uid == OWNER[1], f"a journal root writes belongs to the owner: uid {journal.stat().st_uid}")
    journal.unlink()

    # 3. Root runs what sudo was asked to run. The account's last tmux server died and left
    # its socket behind ("no server running"), which is stopped, not unknown.
    socket_dir.mkdir(mode=0o700, exist_ok=True)  # every probe as the owner makes it
    stale_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale_socket.bind(str(socket_dir / "default"))
    stale_socket.close()
    subprocess.run(["chown", "-R", f"{OWNER[1]}:{OWNER[1]}", str(socket_dir)], check=True)
    check("no server running on" in owner_tmux("has-session", "-t", f"{PROJECT}-director").stderr,
          "(the owner's tmux finds a dead server's socket)")
    ran = as_root_via_sudo([sys.executable, "-B", *asked[1:]], env={"TICKET_BOARD_CALLER_ROLE": "app"})
    check(ran.returncode == 0, f"root's repair succeeds while the project is stopped: {(ran.stdout + ran.stderr)[-500:]}")
    check(designer_model() == OWNER_OFFERS[0], f"designer now names the offered model: {designer_model()!r}")
    after = json.loads(CONFIG.read_text(encoding="utf-8"))
    before_roles = json.loads(original.split(b"\0")[0])["roles"]
    check([r for r in after["roles"] if r["role"] != "designer"] == [r for r in before_roles if r["role"] != "designer"],
          "and no other role changed")
    from scripts.workflow_launcher import project_roles

    check(after["roles"] == project_roles(after, after["workflow"])["roles"] and after["workflow"] == DOCUMENT
          and all(json.loads(copy.read_text(encoding="utf-8")).get("workflow", json.loads(copy.read_text(encoding="utf-8")))
                  == DOCUMENT for copy in COPIES),
          "every copy of the declared workflow is still the board's, and the roles are what it projects")
    check(Board.writes == [], f"the board was not written: {Board.writes}")
    not_owned = [str(path) for path in HOME.rglob("*") if path.lstat().st_uid != OWNER[1]]
    check(not not_owned, f"everything root touched in the owner's home is still the owner's: {not_owned}")
    check(owner_tmux("has-session", "-t", f"{PROJECT}-designer").returncode != 0, "and no role was started")

    # 4. The launch passes the gate.
    result, said = launch(tree)
    expected = {role["role"]: role.get("model") or "" for role in after["roles"]}
    check(result["code"] == 0 and result["launched"] == [expected] and expected["designer"] == OWNER_OFFERS[0]
          and expected["director"] == DIRECTOR_MODEL,
          f"the project launches, the designer on the model chosen: {result} {said[-400:] if result['code'] else ''}")
    check("not starting" not in said, "and the gate says nothing")

    server.shutdown()
    print(json.dumps({"failures": failures, "checks": CHECKS}))
    return 0


def main() -> int:
    if "--inner" in sys.argv:
        index = sys.argv.index("--inner")
        before = Path(sys.argv[index + 3]) if sys.argv[index + 3] != "-" else None
        return inner(Path(sys.argv[index + 1]), Path(sys.argv[index + 2]), before)
    base = Path(tempfile.mkdtemp(prefix="syrd593."))
    try:
        base.chmod(0o755)
        tree = base / "tree"
        for part in ("scripts", "tests", "examples"):
            shutil.copytree(ROOT / part, tree / part, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(Path(__file__), base / "stopped_project_model_repair_test.py")
        before = base / "before"
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", BEFORE, "scripts"], capture_output=True)
        if archive.returncode == 0:
            before.mkdir()
            tarfile.open(fileobj=io.BytesIO(archive.stdout)).extractall(before, filter="tar")
        subprocess.run(["chmod", "-R", "a+rX", str(base)], check=True)
        ran = subprocess.run(
            ["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private", "--net", "--pid",
             "--fork", "--mount-proc", sys.executable, "-B", str(base / "stopped_project_model_repair_test.py"),
             "--inner", str(base), str(tree), str(before) if before.exists() else "-"],
            capture_output=True, text=True, timeout=600)
        print(ran.stdout)
        if ran.stderr.strip():
            print(ran.stderr)
        result = {}
        for line in reversed(ran.stdout.splitlines()):
            if line.startswith("{\"failures\""):
                result = json.loads(line)
                break
        if not result:
            print("FAIL the namespace run reported no result")
            return 1
        if result["failures"] or ran.returncode:
            print(f"stopped_project_model_repair_test: {len(result['failures'])} of {result['checks']} checks failed")
            return 1
        print(f"stopped_project_model_repair_test: {result['checks']} checks ok")
        return 0
    finally:
        subprocess.run(["unshare", "--map-auto", "--map-root-user", sys.executable, "-c",
                        "import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)", str(base)])


if __name__ == "__main__":
    raise SystemExit(main())
