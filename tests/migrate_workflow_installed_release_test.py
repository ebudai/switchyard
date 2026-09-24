#!/usr/bin/env python3
"""SYRD-253: a legacy tenant's first workflow, installed across the real caller boundary.

Live MEFP cutover failed twice on the way to its first declared workflow:

  1. the root command spawned `workflow_manage.py` by filename from the
     installed release, which could not import its own package and, with that
     fixed, never called main();
  2. called properly, the write died on `caller_role must be non-empty`:
     root was making a write only the director may make.

The second is not a missing environment variable. On a tenant's socket the
board takes the caller's role from the connecting PROCESS -- the live pane
PostgreSQL registered for that role -- and never from a header. Root has no
such process, and a role header plus a write token supplied from root would be
the impersonation that boundary exists to refuse. (The first version of this
test did exactly that, which is how it passed while the live retry failed.)

So the work is split along the boundary:

  * root (`migrate-workflow --apply`, uid 0) verifies the document it vouches
    for and hands it over, root-owned and read-only, and writes no board state;
  * the director's own session (`finish-upgrade`) installs it over the socket.

Everything here is real and nothing is given a token or a role variable: the
release is a stand-alone copy of this tree run with only its root importable;
root's half runs as uid 0 in a user namespace; the board enforces process
authority on its socket; and the panes are an isolated tmux server's, with
their shells registered in PostgreSQL exactly as the launcher registers them.
The same write is then attempted by a process that is not a pane, by a pane
registered as `main` that claims to be the director, and by the director's
pane -- and only the last may land.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ticket_board_pane_env import strip_ticket_board_pane_env  # noqa: E402

strip_ticket_board_pane_env(os.environ)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.peer_identity import read_process  # noqa: E402
from scripts.ticket_board.project_provision import (  # noqa: E402
    WORKFLOW_RECORD_NAME,
    privileged_provision_dir,
    workflow_document_digest,
)
from scripts.ticket_board.server import ProcessRoleAuthority, TicketBoardUnixServer  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

PROJECT = "porter"
CHECKS = 0

# What the `switchyard` entry point does before importing anything: the
# release root, and only that, on the path. `-I` keeps the test's own path and
# every PYTHON* variable out.
ROOT_DRIVER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.team_launcher import switchyard_migrate_workflow_command
raise SystemExit(switchyard_migrate_workflow_command(
    sys.argv[2], apply=sys.argv[3] == "apply", config_path=Path(sys.argv[4])
))
"""

# The director's step, as `finish-upgrade` takes it. The role it names only
# satisfies the client; on the socket the board ignores it.
DIRECTOR_DRIVER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.team_launcher import install_handed_off_workflow, load_project_config
config_path = Path(sys.argv[2])
config = load_project_config(sys.argv[3], config_path)
result = install_handed_off_workflow(config, config_path=config_path, caller_role=sys.argv[4])
raise SystemExit(0 if result is True else 1)
"""


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def reviewed_document() -> dict:
    """The shipped example, re-projected, with the director's onboarding unfilled."""
    raw = json.loads((ROOT / "examples" / "workflows" / "inspection.json").read_text())
    source = str(raw.get("project") or "")
    document = json.loads(json.dumps(raw).replace(f'"{source}-', f'"{PROJECT}-'))
    document["project"] = PROJECT
    document["migrations"] = {}
    for role in document["roles"]:
        if role["name"] == "director":
            role.pop("onboarding_prompt", None)
            role["onboarding"] = None
    return document


def install_release(destination: Path) -> Path:
    """Every tracked file, as it is on disk now, in a directory of its own."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8").split("\0")
    for name in filter(None, listed):
        source = ROOT / name
        if source.is_file():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return destination


def live_workflow(base: str) -> dict:
    with urllib.request.urlopen(base + "/api/workflow", timeout=10) as response:
        return json.load(response)


def namespaces_available() -> bool:
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "true"], capture_output=True, check=False
    )
    return probe.returncode == 0


class Tmux:
    """A tmux server of our own. Never the caller's: -S names its socket, and
    TMUX is absent from its environment, so no command here can reach a live one."""

    def __init__(self, socket: Path, env: dict[str, str]) -> None:
        self.socket, self.env = socket, env
        check("TMUX" not in env, "the test's tmux can never address a live server")

    def run(self, *args: str) -> str:
        done = subprocess.run(
            ["tmux", "-S", str(self.socket), "-f", "/dev/null", *args],
            env=self.env, capture_output=True, text=True, check=True, timeout=30,
        )
        return done.stdout.strip()

    def pane(self, name: str, command: str) -> int:
        self.run("new-session", "-d", "-s", name, "-x", "80", "-y", "24", command)
        return int(self.run("display-message", "-p", "-t", f"{name}:0.0", "#{pane_pid}"))

    def kill(self) -> None:
        subprocess.run(
            ["tmux", "-S", str(self.socket), "kill-server"],
            env=self.env, capture_output=True, check=False,
        )


def wait_for(path: Path, seconds: float = 120) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{path} never appeared")
        time.sleep(0.1)


def main() -> int:
    if not namespaces_available():
        # Said, not skipped quietly: root's half cannot run without uid 0.
        print("migrate_workflow_installed_release_test: user namespaces unavailable")
        return 1
    account = pwd.getpwuid(os.getuid()).pw_name
    with temporary_cluster(prefix="syrd253-", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd253-") as raw:
        scratch = Path(raw)
        release = install_release(scratch / "opt" / "releases" / "candidate")

        # -- running the file by name works, rather than exiting 0 unrun ---------
        by_name = subprocess.run(
            [sys.executable, "-I", str(release / "scripts" / "workflow_manage.py"), "--help"],
            cwd="/", env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=False,
        )
        check(by_name.returncode == 0, by_name.stdout + by_name.stderr)
        check("usage: workflow_manage.py" in by_name.stdout, f"main() ran: {by_name.stderr}")

        # -- a board that takes roles from processes, as a tenant's does --------
        db = "syrd253"
        admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(admin, t.SCHEMA_PATH.read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        app = t.TicketBoardApp(
            scratch / "frames", scratch / "assets", project=PROJECT, ticket_prefix="POR",
            database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
        )
        http = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        socket_path = scratch / "run" / "board.sock"
        unix = TicketBoardUnixServer(
            socket_path, app, events=http.events, director_notifier=t.QuietNotifier(),
            role_authority=ProcessRoleAuthority(app, account),
        )
        for server in (http, unix):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{http.server_port}"
        tmux: Tmux | None = None
        try:
            check(live_workflow(base).get("document") is None, "a legacy board runs no workflow")

            # The tenant: launcher config, and the project the backfill reads.
            project_dir = scratch / "project"
            (project_dir / "docs" / "onboarding").mkdir(parents=True)
            (scratch / "tenant").mkdir()
            (scratch / "tenant" / f"{PROJECT}.project.json").write_text(
                json.dumps({"project": {"repository": str(project_dir)}}), encoding="utf-8"
            )
            config_dir = scratch / "tenant" / "launcher"
            config_dir.mkdir()
            config_path = config_dir / f"{PROJECT}.json"
            config_path.write_text(json.dumps({
                "project": PROJECT,
                "layout": "layout.json",
                "board_url": base,
                "board_socket": str(socket_path),
                "repository": str(project_dir),
                "worktree_base": str(scratch / "worktrees"),
                "roles": [
                    {"role": "director", "cli": "claude", "slot": 0,
                     "workdir": str(scratch / "worktrees" / "director")},
                    {"role": "main", "cli": "claude", "slot": 1,
                     "workdir": str(scratch / "worktrees" / "main")},
                ],
            }), encoding="utf-8")

            # Root's record, as `adopt-workflow` writes it.
            provision_root = scratch / "etc" / "provision"
            record = privileged_provision_dir(PROJECT, root=provision_root) / WORKFLOW_RECORD_NAME
            record.parent.mkdir(parents=True)
            document = reviewed_document()
            record.write_text(json.dumps({
                "project": PROJECT, "digest": workflow_document_digest(document), "document": document,
            }, sort_keys=True), encoding="utf-8")

            # Deliberately no TICKET_BOARD_WRITE_TOKEN and no TICKET_BOARD_CALLER_ROLE
            # anywhere below: nothing may authorize itself by saying so.
            env = {
                k: v for k, v in os.environ.items()
                if k in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
            }
            env.update({
                "PATH": "/usr/bin:/bin", "HOME": str(scratch), "LANG": "C.UTF-8",
                "SWITCHYARD_PRIVILEGED_PROVISION_ROOT": str(provision_root),
            })

            def as_root(action: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["unshare", "--user", "--map-root-user", sys.executable, "-I", "-c",
                     ROOT_DRIVER, str(release), PROJECT, action, str(config_path)],
                    env=env, capture_output=True, text=True, timeout=300, check=False,
                )

            # -- root: preview, then hand over; no board write either way ------
            preview = as_root("preview")
            said = preview.stdout + preview.stderr
            check(preview.returncode == 0, said)
            reviewed = next(l.split()[-1] for l in said.splitlines() if l.strip().startswith("document "))
            writes = [l.split()[1] for l in said.splitlines() if l.strip().startswith("writes ")]
            check(len(writes) == 1 and writes[0] != reviewed, f"both digests are shown: {said}")
            effective = writes[0]

            handed = as_root("apply")
            said = handed.stdout + handed.stderr
            check(handed.returncode == 0, f"root hands it over: {said}")
            check("switchyard finish-upgrade porter" in said, f"and names the director's step: {said}")
            check(live_workflow(base)["document"] is None, "root wrote nothing to the board")
            check(live_workflow(base)["revision"] == 0, "not even a revision")

            # A file, not `-c`: the pane runs it through `sh -c '...'`, and a
            # program quoted into that is a program nobody can read.
            driver = scratch / "director_step.py"
            driver.write_text(DIRECTOR_DRIVER, encoding="utf-8")

            def director_step(stamp: Path) -> str:
                return (
                    f"{sys.executable} -I {driver} {release} {config_path} {PROJECT} director"
                    f" >{stamp}.out 2>&1; echo $? >{stamp}.rc"
                )

            # -- a process that is not a pane -----------------------------------
            outside = subprocess.run(
                [sys.executable, "-I", "-c", DIRECTOR_DRIVER, str(release), str(config_path),
                 PROJECT, "director"],
                env=env, capture_output=True, text=True, timeout=300, check=False,
            )
            said = outside.stdout + outside.stderr
            check(outside.returncode == 1, f"a process that is not a pane is refused: {said}")
            # Which of the board's two process-authority refusals depends on
            # whether this suite itself runs under some tmux: with no pane in
            # its ancestry it is not a pane at all; inside one, it is a pane
            # nobody registered. Either way the board, not the client, said no.
            check(
                "live launcher pane" in said or "pane is not registered" in said,
                f"by the board, for that reason: {said}",
            )
            check(live_workflow(base)["revision"] == 0, "and nothing was written")

            # -- the panes: the launcher's registration, for real processes ------
            tmux = Tmux(scratch / "tmux.sock", env)
            gates = {role: scratch / f"go-{role}" for role in ("director", "main")}
            stamps = {role: scratch / f"ran-{role}" for role in ("director", "main")}
            for role in ("director", "main"):
                pane_pid = tmux.pane(
                    f"{PROJECT}-{role}",
                    f"sh -c 'while [ ! -e {gates[role]} ]; do sleep 0.1; done; "
                    f"{director_step(stamps[role])}; sleep 600'",
                )
                started = read_process(pane_pid)
                check(started is not None, f"the {role} pane is running")
                app.register_runtime_assignment(
                    role=role, runtime="claude", target=f"{PROJECT}-{role}:0.0",
                    worktree=str(scratch / "worktrees" / role),
                    session_dir=str(scratch / "sessions" / role),
                    process_pid=pane_pid, process_start_time=started.start_time,
                    process_uid=os.getuid(), expected_generation=0,
                )

            # -- a pane registered as main, claiming to be the director ----------
            gates["main"].touch()
            wait_for(Path(f"{stamps['main']}.rc"))
            said = Path(f"{stamps['main']}.out").read_text()
            check(Path(f"{stamps['main']}.rc").read_text().strip() == "1", f"main is refused: {said}")
            check("this pane is registered as main, not director" in said,
                  f"because the board knows it is main, whatever it claims: {said}")
            check(live_workflow(base)["revision"] == 0, "and nothing was written")

            # -- the director's own pane -----------------------------------------
            gates["director"].touch()
            wait_for(Path(f"{stamps['director']}.rc"))
            said = Path(f"{stamps['director']}.out").read_text()
            check(Path(f"{stamps['director']}.rc").read_text().strip() == "0",
                  f"the director installs it: {said}")
            live = live_workflow(base)
            check(live["document"] is not None, f"the board runs a workflow: {live}")
            check(workflow_document_digest(live["document"]) == effective,
                  f"exactly the digest root showed: {workflow_document_digest(live['document'])} vs {effective}")
            director = next(r for r in live["document"]["roles"] if r["name"] == "director")
            check(bool(director.get("onboarding_prompt")), f"the director has onboarding: {director}")
            check(live["document"]["migrations"].get("director_onboarding") is True,
                  "and the marker the pane hook reads")
            projected = json.loads((config_dir / "workflow.json").read_text())
            check(workflow_document_digest(projected) == effective,
                  "the tenant's projection was rewritten, by the director's own account")
            revision = live["revision"]

            # -- root's rerun: done, and idempotent -------------------------------
            again = as_root("apply")
            said = again.stdout + again.stderr
            check(again.returncode == 0, said)
            check("already running exactly this workflow" in said, f"a no-op: {said}")
            check(live_workflow(base)["revision"] == revision, "with no new revision")
        finally:
            if tmux is not None:
                tmux.kill()
            for server in (http, unix):
                server.shutdown()
                server.server_close()
    print(f"migrate_workflow_installed_release_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
