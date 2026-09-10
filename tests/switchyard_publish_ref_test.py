#!/usr/bin/env python3
"""Security regressions for the privileged ref publisher.

The program under test is the only thing on a Switchyard host that may push.
Every role runs as one Unix account, so it cannot decide anything from the uid
that invoked it: it asks the kernel which pane it is running under, and asks the
board whether that exact process is the registered control-role runtime.

Everything here runs unprivileged, with the root-owned locations redirected into
a temporary tree and the process ancestry supplied as a fake /proc. Git is real,
the remote is a real repository, and the pushes really happen.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "switchyard-publish-ref"

TMUX_PID = 900001
PANE_PID = 900002
OTHER_PANE_PID = 900003
PANE_START = 4242
OTHER_START = 5353


def load_module():
    loader = importlib.machinery.SourceFileLoader("switchyard_publish_ref", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


publisher = load_module()


def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    assert result.returncode == 0, (args, result.stderr)
    return result


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_proc(root: Path, pid: int, *, ppid: int, start: int, comm: str, uid: int) -> None:
    """One entry shaped like the kernel's, so the real parser is what runs."""
    directory = root / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(start)]
    (directory / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n", encoding="utf-8")
    (directory / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n", encoding="utf-8")


class Board:
    """The board's read surface, as the publisher actually consumes it."""

    def __init__(self, *, roles: list[dict], assignments: dict, requests: list[dict],
                 authority_mode: str = "process") -> None:
        self.roles = roles
        self.assignments = assignments
        self.requests = requests
        self.authority_mode = authority_mode
        board = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401 - quiet
                return

            def do_GET(self):  # noqa: N802 - http.server API
                path = self.path
                if path == "/api/workflow":
                    payload = {"revision": 3, "document": {"roles": board.roles}}
                elif path.startswith("/api/runtime-assignments/"):
                    role = path.rsplit("/", 1)[-1]
                    assignment = board.assignments.get(role)
                    if assignment is None:
                        self.send_response(404)
                        self.end_headers()
                        self.wfile.write(b"runtime assignment not found")
                        return
                    payload = {"authority_mode": board.authority_mode, "assignment": assignment}
                elif path.startswith("/api/publications"):
                    payload = {"requests": board.requests}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_port

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class Host:
    """A whole fake host: root-owned data, a board, a remote, and a request."""

    def __init__(self, root: Path, *, project: str = "syrd", role: str = "ops",
                 ref: str = "ops/syrd-92-defer-backlog") -> None:
        self.root = root
        self.project = project
        self.role = role
        self.ref = ref
        uid, gid = os.getuid(), os.getgid()

        self.owner_home = root / "owner"
        self.repository = self.owner_home / "Projects" / "switchyard"
        self.config_path = self.repository / ".switchyard" / "provision" / f"{project}.json"
        self.registry = root / "etc" / "switchyard" / "projects"
        self.units = root / "etc" / "systemd" / "system"
        self.grants = root / "etc" / "switchyard" / "publish"
        self.staging = root / "var" / "lib" / "switchyard" / "publish"
        self.proc = root / "proc"
        self.cache = root / "owner" / "source-cache.git"
        self.remote = root / "remote.git"
        self.work = root / "work"

        self.repository.mkdir(parents=True, exist_ok=True)
        write_json(self.config_path, {
            "project": project,
            "repository": str(self.repository),
            "worktree_remote": "origin",
        })
        write_json(self.registry / f"{project}.json", {
            "schema": publisher.PROJECT_REGISTRY_SCHEMA,
            "slug": project,
            "name": "Switchyard",
            "config_path": str(self.config_path),
        })

        # A real remote, a real cache, and a real commit to publish.
        git("init", "--bare", "-q", str(self.remote))
        git("init", "--bare", "-q", str(self.cache))
        self.work.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "-b", "trunk", str(self.work))
        git("config", "user.email", "role@example.invalid", cwd=self.work)
        git("config", "user.name", "Role", cwd=self.work)
        (self.work / "file.txt").write_text("work\n", encoding="utf-8")
        git("add", "file.txt", cwd=self.work)
        git("commit", "-q", "-m", "role work", cwd=self.work)
        git("branch", "-f", ref, "HEAD", cwd=self.work)
        self.commit = git("rev-parse", "HEAD", cwd=self.work).stdout.strip()
        self.bundle = root / "outbox" / "PGU-1.bundle"
        self.bundle.parent.mkdir(parents=True, exist_ok=True)
        git("bundle", "create", str(self.bundle), ref, cwd=self.work)

        # Root-owned data, redirected here: the unit that names the cache and
        # the board, and the grant that names the one credential.
        self.units.mkdir(parents=True, exist_ok=True)
        self.grants.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        self.identity = self.grants / f"{project}-publish-key"
        self.identity.write_text("not a real key\n", encoding="utf-8")
        self.identity.chmod(0o600)

        write_proc(self.proc, TMUX_PID, ppid=1, start=1, comm="tmux: server", uid=uid)
        write_proc(self.proc, PANE_PID, ppid=TMUX_PID, start=PANE_START, comm="bash", uid=uid)
        write_proc(self.proc, OTHER_PANE_PID, ppid=TMUX_PID, start=OTHER_START, comm="bash", uid=uid)
        write_proc(self.proc, os.getpid(), ppid=PANE_PID, start=7, comm="python3", uid=uid)

        self.board = Board(
            roles=[
                {"name": "director", "active": True,
                 "capabilities": ["merge", "set_manually_controlled", "resolve_publication"]},
                {"name": role, "active": True, "capabilities": ["request_publication"]},
            ],
            assignments={
                "director": {
                    "role": "director", "process_pid": PANE_PID,
                    "process_start_time": PANE_START, "process_uid": uid,
                },
                role: {
                    "role": role, "process_pid": OTHER_PANE_PID,
                    "process_start_time": OTHER_START, "process_uid": uid,
                },
            },
            requests=[{
                "id": 7, "ticket_id": "PGU-1", "requested_by": role, "ref": ref,
                "commit_hash": self.commit, "bundle_path": str(self.bundle), "state": "requested",
            }],
        )
        self.write_unit()
        self.write_grant()
        self.uid, self.gid = uid, gid

    def write_unit(self, *, cache: str | None = None, extra: str = "") -> None:
        (self.units / f"{self.project}-ticket-board.service").write_text(
            "[Service]\n"
            f"ExecStart=/usr/bin/python3 /board/ticket-board.py --host 127.0.0.1 "
            f"--port {self.board.port} --unix-socket /run/board.sock\n"
            f"Environment=TICKET_BOARD_COMMIT_GIT_DIR={cache if cache is not None else self.cache}\n"
            f"{extra}",
            encoding="utf-8",
        )

    def write_grant(self, **overrides) -> None:
        document = {
            "schema": publisher.PUBLISH_GRANT_SCHEMA,
            "project": self.project,
            "identity_file": str(self.identity),
            "remote": str(self.remote),
        }
        document.update(overrides)
        write_json(self.grants / f"{self.project}.json", document)

    def environment(self) -> dict[str, str]:
        return {
            publisher.REGISTRY_TEST_ROOT_ENV: str(self.registry),
            publisher.BOARD_UNIT_TEST_DIR_ENV: str(self.units),
            publisher.PUBLISH_GRANT_TEST_DIR_ENV: str(self.grants),
            publisher.STAGING_TEST_ROOT_ENV: str(self.staging),
            publisher.PROC_ROOT_ENV: str(self.proc),
            publisher.OWNER_TEST_HOME_ENV: str(self.owner_home),
        }

    def publish(self, *, request: int = 7, under: int = PANE_PID) -> subprocess.CompletedProcess[str]:
        """Run the real program, as a process whose ancestry the test controls.

        The publisher asks the kernel which pane it is under, so a test that
        wants to say "this ran from the Director's pane" has to say it about the
        publisher's own process. The shim writes its own entry into the fake
        /proc and then execs, which keeps the pid it just described.
        """
        shim = (
            "import os, sys\n"
            "root, pane = sys.argv[1], int(sys.argv[2])\n"
            "d = os.path.join(root, str(os.getpid()))\n"
            "os.makedirs(d, exist_ok=True)\n"
            "fields = ['S', str(pane)] + ['0'] * 17 + ['7']\n"
            "open(os.path.join(d, 'stat'), 'w').write('%d (python3) ' % os.getpid() + ' '.join(fields) + '\\n')\n"
            "open(os.path.join(d, 'status'), 'w').write('Uid:\\t%d\\t%d\\t%d\\t%d\\n' % ((os.getuid(),) * 4))\n"
            "os.execv(sys.executable, [sys.executable] + sys.argv[3:])\n"
        )
        return subprocess.run(
            [
                "python3", "-c", shim, str(self.proc), str(under),
                str(SCRIPT), "--project", self.project, "--request", str(request),
            ],
            text=True,
            capture_output=True,
            env={**os.environ, **self.environment()},
        )

    def close(self) -> None:
        self.board.close()


def refusal(result: subprocess.CompletedProcess[str], expected: str) -> None:
    assert result.returncode != 0, result.stdout
    combined = result.stdout + result.stderr
    assert expected in combined, (expected, combined)


def remote_ref(host: Host) -> str:
    listed = subprocess.run(
        ["git", "--git-dir", str(host.remote), "rev-parse", f"refs/heads/{host.ref}"],
        text=True, capture_output=True,
    )
    return listed.stdout.strip() if listed.returncode == 0 else ""


def test_the_registered_control_process_publishes_and_the_cache_is_refreshed() -> None:
    """The whole flow, from the ask on the board to a submittable commit."""
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        try:
            result = host.publish()
            assert result.returncode == 0, result.stderr
            report = json.loads(result.stdout.strip().splitlines()[-1])
            assert report["published"] is True and report["ref"] == host.ref, report
            assert report["commit"] == host.commit, report
            # The exact ref, on the actual remote.
            assert remote_ref(host) == host.commit
            # And in the cache the board verifies commit_hash against, which is
            # what makes the work submittable rather than merely pushed.
            cached = subprocess.run(
                ["git", "--git-dir", str(host.cache), "rev-parse",
                 f"refs/remotes/origin/{host.ref}^{{commit}}"],
                text=True, capture_output=True,
            )
            assert cached.stdout.strip() == host.commit, cached

            # Running it again for the same request is safe: same ref, same
            # commit, no error, so an interrupted publication can be retried.
            again = host.publish()
            assert again.returncode == 0, again.stderr
            assert remote_ref(host) == host.commit
        finally:
            host.close()


def test_a_sibling_role_under_the_same_uid_is_refused() -> None:
    """The defect this exists for: one account, so the uid proves nothing."""
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        try:
            # Run from the implementer's pane instead of the control role's:
            # the same account, the same program, a different pane.
            result = host.publish(under=OTHER_PANE_PID)
            refusal(result, "only director's registered process may publish")
            assert remote_ref(host) == "", "a refused caller must not have pushed"
        finally:
            host.close()


def test_an_unregistered_or_replaced_process_is_refused() -> None:
    """A row can outlive the process it names; a start time is not reused."""
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        try:
            host.board.assignments["director"]["process_start_time"] = PANE_START + 1
            refusal(host.publish(), "only director's registered process may publish")
            assert remote_ref(host) == ""

            host.board.assignments.pop("director")
            refusal(host.publish(), "no registered runtime for director")

            # And a caller with no pane at all -- detached, or run by a service
            # -- has no identity to check, so it is refused rather than guessed.
            host.board.assignments["director"] = {
                "role": "director", "process_pid": PANE_PID,
                "process_start_time": PANE_START, "process_uid": host.uid,
            }
            refusal(host.publish(under=1), "cannot establish which pane this was run from")
            assert remote_ref(host) == ""
        finally:
            host.close()


def test_a_board_without_process_authority_cannot_authorize_a_publication() -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        try:
            host.board.authority_mode = "legacy_uid"
            refusal(host.publish(), "does not run on process authority")
            assert remote_ref(host) == ""
        finally:
            host.close()


def test_the_control_role_is_derived_from_capabilities_and_never_from_a_name() -> None:
    """A project whose controller is not called 'director' works the same.

    And a document with no controller yields no controller. There is no
    fallback: this program holds the only push credential on the host, so
    "nobody is declared" must mean "nobody may publish", never "whoever is
    called director may".
    """
    document = {"roles": [
        {"name": "steward", "active": True,
         "capabilities": ["merge", "set_manually_controlled"]},
        {"name": "ops", "active": True, "capabilities": ["request_publication"]},
    ]}
    assert publisher.control_role_of(document) == "steward"
    for empty in (
        {"roles": [{"name": "steward", "active": False,
                    "capabilities": ["merge", "set_manually_controlled"]}]},
        {"roles": [{"name": "director", "active": True, "capabilities": ["add_comment"]}]},
        {"roles": []},
        {},
        {"roles": "not a list"},
    ):
        assert publisher.control_role_of(empty) == "", empty


def test_a_board_with_no_declared_controller_publishes_nothing() -> None:
    """The exact shape the Director reproduced: an empty document used to push.

    control_role_of() fell back to `director`, so a board whose workflow
    declared no capability-derived controller -- absent, empty or malformed --
    became name-based push authority at the one boundary holding the root
    credential. A runtime row registered under that name was then enough.
    """
    for label, roles in (
        ("no roles at all", []),
        ("a director in name only", [
            {"name": "director", "active": True, "capabilities": ["add_comment"]},
        ]),
        ("an inactive controller", [
            {"name": "director", "active": False,
             "capabilities": ["merge", "set_manually_controlled"]},
        ]),
    ):
        with tempfile.TemporaryDirectory() as raw:
            host = Host(Path(raw))
            try:
                host.board.roles = roles
                # The runtime row is exactly the one a real board would have for
                # the Director's own pane, and the caller really is that pane.
                host.board.assignments = {
                    "director": {
                        "role": "director", "process_pid": PANE_PID,
                        "process_start_time": PANE_START, "process_uid": host.uid,
                    },
                }
                result = host.publish()
                refusal(result, "declares no role with control authority")
                assert remote_ref(host) == "", (label, "a refused publication must not have pushed")
            finally:
                host.close()


def test_a_project_whose_controller_is_not_called_director_publishes_the_same() -> None:
    """Authority is the capabilities, not the name -- through the whole flow.

    A project names its own roles. If the publisher looked for `director` it
    would refuse the controller of every project that calls it something else,
    and would accept a role that merely borrowed the name.
    """
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        try:
            host.board.roles = [
                {"name": "steward", "active": True,
                 "capabilities": ["merge", "set_manually_controlled", "resolve_publication"]},
                # Named like the controller of this repo's own project, and
                # holding none of its authority.
                {"name": "director", "active": True, "capabilities": ["add_comment"]},
                {"name": host.role, "active": True, "capabilities": ["request_publication"]},
            ]
            host.board.assignments = {
                "steward": {
                    "role": "steward", "process_pid": PANE_PID,
                    "process_start_time": PANE_START, "process_uid": host.uid,
                },
                "director": {
                    "role": "director", "process_pid": OTHER_PANE_PID,
                    "process_start_time": OTHER_START, "process_uid": host.uid,
                },
            }
            # The role that merely has the name cannot publish.
            refusal(host.publish(under=OTHER_PANE_PID), "only steward's registered process may publish")
            assert remote_ref(host) == ""

            # The role that holds the capabilities can.
            result = host.publish()
            assert result.returncode == 0, result.stderr
            report = json.loads(result.stdout.strip().splitlines()[-1])
            assert report["control_role"] == "steward", report
            assert remote_ref(host) == host.commit
        finally:
            host.close()


def test_only_the_request_on_the_board_decides_what_is_published() -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        try:
            # A request naming another role's namespace is refused even though
            # the board handed it over: the rule is asked again here.
            host.board.requests[0]["ref"] = "app/somebody-elses"
            refusal(host.publish(), "is not in ops's namespace")
            host.board.requests[0]["ref"] = "main"
            refusal(host.publish(), "collides with an integration branch")
            host.board.requests[0]["ref"] = host.ref

            # A record whose commit is not what the bundle carries is refused
            # rather than published as whatever the bundle happens to contain.
            host.board.requests[0]["commit_hash"] = "c" * 40
            refusal(host.publish(), "publish the request the implementer actually filed")
            host.board.requests[0]["commit_hash"] = host.commit

            # And a request id that is not open publishes nothing.
            refusal(host.publish(request=99), "is not open on this board")
            assert remote_ref(host) == ""
        finally:
            host.close()


def test_the_bundle_must_belong_to_the_project_account() -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        try:
            link = host.root / "outbox" / "link.bundle"
            link.symlink_to(host.bundle)
            host.board.requests[0]["bundle_path"] = str(link)
            refusal(host.publish(), "must be a regular file, not a link")

            loose = host.root / "outbox" / "loose.bundle"
            loose.write_bytes(host.bundle.read_bytes())
            loose.chmod(0o666)
            host.board.requests[0]["bundle_path"] = str(loose)
            refusal(host.publish(), "writable by an account outside the project")

            empty = host.root / "outbox" / "empty.bundle"
            empty.write_text("not a bundle\n", encoding="utf-8")
            host.board.requests[0]["bundle_path"] = str(empty)
            refusal(host.publish(), "bundle did not verify")
            assert remote_ref(host) == ""
        finally:
            host.close()


def test_the_push_credential_must_be_unreadable_by_the_project_account() -> None:
    """A key every role can read is not a boundary, and is refused as one."""
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        try:
            host.identity.chmod(0o640)
            refusal(host.publish(), "a credential the project account can read")
            host.identity.chmod(0o600)

            host.identity.unlink()
            refusal(host.publish(), "cannot read publish identity")
            host.identity.write_text("not a real key\n", encoding="utf-8")
            host.identity.chmod(0o600)

            (host.grants / f"{host.project}.json").unlink()
            refusal(host.publish(), "no publish grant for syrd")
            assert remote_ref(host) == ""
        finally:
            host.close()


def test_the_remote_is_pinned_in_root_owned_data() -> None:
    """The checkout's remote is role-writable; the grant's is not."""
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        try:
            host.write_grant(remote="")
            refusal(host.publish(), "publish grant names no remote")
            host.write_grant(remote="ext::sh -c 'touch /tmp/pwned'")
            refusal(host.publish(), "would run a command rather than name a repository")
            assert remote_ref(host) == ""
        finally:
            host.close()


def test_the_board_unit_is_where_the_cache_and_the_board_come_from() -> None:
    """Root owns the unit; the project configuration is role-writable."""
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        try:
            unit = host.units / f"{host.project}-ticket-board.service"
            unit.write_text(
                "[Service]\nExecStart=/usr/bin/python3 board.py --host 127.0.0.1\n"
                f"Environment=TICKET_BOARD_COMMIT_GIT_DIR={host.cache}\n",
                encoding="utf-8",
            )
            refusal(host.publish(), "does not say which port its board reads on")

            host.write_unit(cache="")
            refusal(host.publish(), "the trusted commit cache is not configured")

            host.write_unit()
            unit.chmod(0o664)
            refusal(host.publish(), "writable by an untrusted account")
            unit.chmod(0o644)

            # The cache the unit names has to be the project account's own bare
            # repository, not a path a role could have replaced.
            host.write_unit(cache=str(host.root / "missing-cache.git"))
            refusal(host.publish(), "cannot inspect configured commit cache")
            assert remote_ref(host) == ""
        finally:
            host.close()


def test_registered_configuration_must_stay_inside_the_owner_tree() -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        try:
            outside = host.root / "elsewhere" / "syrd.json"
            write_json(outside, {"project": host.project, "repository": str(host.repository)})
            write_json(host.registry / f"{host.project}.json", {
                "schema": publisher.PROJECT_REGISTRY_SCHEMA,
                "slug": host.project,
                "name": "Switchyard",
                "config_path": str(outside),
            })
            refusal(host.publish(), "escapes")

            write_json(host.registry / f"{host.project}.json", {
                "schema": "switchyard.project-registry.v0",
                "slug": host.project,
                "name": "Switchyard",
                "config_path": str(host.config_path),
            })
            refusal(host.publish(), "unsupported schema")

            host.registry.chmod(0o775)
            refusal(host.publish(), "writable by an untrusted account")
            host.registry.chmod(0o755)
            assert remote_ref(host) == ""
        finally:
            host.close()


def test_the_cache_refresh_takes_only_this_ref_and_refuses_a_moved_one() -> None:
    """Two roles publishing at once touch different refs, and neither is guessed."""
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        try:
            assert host.publish().returncode == 0

            staging = host.staging / f"{host.project}.git"
            other = "app/other-work"
            git("--git-dir", str(staging), "branch", other, host.commit)
            problem = publisher.refresh_commit_cache(
                host.cache, staging, other, "d" * 40,
                owner_uid=host.uid, owner_gid=host.gid, sleep=lambda _: None,
            )
            assert problem is not None and "not " + "d" * 40 in problem, problem
            # The publication that did land is untouched by the one that did not.
            resolved = subprocess.run(
                ["git", "--git-dir", str(host.cache), "rev-parse",
                 f"refs/remotes/origin/{host.ref}^{{commit}}"],
                text=True, capture_output=True,
            )
            assert resolved.stdout.strip() == host.commit, resolved
        finally:
            host.close()


def test_a_publication_that_cannot_be_verified_is_not_reported_as_ready() -> None:
    """Pushed is not the same as submittable, and the difference is said out loud."""
    with tempfile.TemporaryDirectory() as raw:
        host = Host(Path(raw))
        try:
            # The cache is the project account's, and a cache that cannot be
            # fetched into leaves work that is published and not submittable.
            host.cache.chmod(0o555)
            result = host.publish()
            assert result.returncode != 0
            assert remote_ref(host) == host.commit, "the push itself did land"
            assert "NOT ready to submit" in result.stderr, result.stderr
            assert "re-running this for the same request is safe" in result.stderr.lower(), result.stderr
        finally:
            host.cache.chmod(0o755)
            host.close()


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"switchyard_publish_ref_test: {len(tests)} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
