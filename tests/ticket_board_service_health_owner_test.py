#!/usr/bin/env python3
"""SYRD-516: board health lives in a file the service script sources, and behaves as before.

`scripts/ticket-board-service-health.sh` owns the release canary and the checks
of the restarted live service. `scripts/ticket-board-service.sh` keeps the
configuration, the plumbing and the orchestration, and sources the health file
from beside itself. This drives the real scripts from a copy of the two files:

  * a whole copy runs, and a copy missing the health file fails loudly before
    any command, so a deploy can never proceed without its canary;
  * the canary's own lifecycle: start, check, stop, clean up, report;
  * the deploy's order around it: canary before activation, rollback after a
    failed live check, and a canary failure that stops before activation;
  * functions a caller redefines after sourcing, in either file, are the ones
    that run, and sourcing keeps the service script's shell options.

Every host-facing program the scripts name (systemctl, sudo, pkexec, loginctl,
systemd-run, journalctl) is a refusing recorder first on PATH, and every board
is a fake on 127.0.0.1 or a temporary Unix socket.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "scripts" / "ticket-board-service.sh"
HEALTH = ROOT / "scripts" / "ticket-board-service-health.sh"
SOURCE_LINE = 'source "$(dirname "${BASH_SOURCE[0]}")/ticket-board-service-health.sh"'
HEALTH_FUNCTIONS = (
    "smoke_check_url",
    "smoke_check_http",
    "verify_live_build_id",
    "verify_live_commit_repositories",
    "verify_local_socket_available",
    "verify_http_write_token_required",
    "verify_post_deploy_system_runtime",
    "free_tcp_port",
    "start_canary_direct",
    "stop_canary_direct",
    "systemd_env_value",
    "write_canary_env_file",
    "start_canary_systemd",
    "stop_canary_systemd",
    "run_release_canary",
)
REFUSED = ("systemctl", "sudo", "pkexec", "loginctl", "systemd-run", "journalctl")

#: The release's board, for the canary: serves /api/board, or refuses to start.
FAKE_RELEASE_BOARD = '''#!/usr/bin/env python3
import argparse, json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
if os.environ.get("FAKE_CANARY_FAIL") == "1":
    print("fake release board: refusing to start", file=sys.stderr)
    raise SystemExit(3)
parser = argparse.ArgumentParser()
for flag in ("--host", "--unix-socket", "--frames", "--assets"):
    parser.add_argument(flag, default="")
parser.add_argument("--port", type=int, required=True)
args = parser.parse_args()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"build_id": "canary"}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
ThreadingHTTPServer((args.host or "127.0.0.1", args.port), Handler).serve_forever()
'''

#: Every step of deploy_restart_service outside board health, as recorders.
ORCHESTRATION_RECORDERS = '''
record() { echo "STEP $*"; }
assert_user_manager_identity() { record assert_user_manager_identity; }
deploy_export_release() { printf "abc123\\t%s\\n" "$FAKE_RELEASE"; }
current_release_dir() { echo /previous/release; }
resolved_service_scope() { echo user; }
stop_listener_for_upgrade() { record stop_listener; }
apply_database_migrations_for_release() { record migrate; }
activate_release() { record activate "$1"; }
verify_current_release_sha() { record verify_current "$1"; }
restart_live_service() { record restart "$1"; }
start_listener_after_upgrade() { record start_listener; }
verify_listener_pane_state_authority() { record listener_authority; }
rollback_live_service() { record rollback "$1" "$2"; }
'''


class LiveBoard(ThreadingHTTPServer):
    """The restarted live board, with a chosen build id and no managed commit cache."""

    def __init__(self, build_id: str) -> None:
        self.build_id = build_id
        super().__init__(("127.0.0.1", 0), LiveHandler)


class LiveHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        payload = {"build_id": self.server.build_id} if self.path.startswith("/api/board") else {}
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Fixture:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.tree = tmp / "tree"
        (self.tree / "scripts").mkdir(parents=True)
        shutil.copy2(SERVICE, self.tree / "scripts" / SERVICE.name)
        shutil.copy2(HEALTH, self.tree / "scripts" / HEALTH.name)
        self.lone = tmp / "lone"
        (self.lone / "scripts").mkdir(parents=True)
        shutil.copy2(SERVICE, self.lone / "scripts" / SERVICE.name)
        self.stubs = tmp / "stubs"
        self.stubs.mkdir()
        self.refusals = tmp / "refused.log"
        for name in REFUSED:
            stub = self.stubs / name
            stub.write_text(
                "#!/bin/bash\n"
                f"printf '%s %s\\n' {name} \"$*\" >>{self.refusals}\n"
                f"echo 'refused {name}' >&2\n"
                "exit 97\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
        self.release = tmp / "release"
        (self.release / "scripts").mkdir(parents=True)
        board = self.release / "scripts" / "ticket-board.py"
        board.write_text(FAKE_RELEASE_BOARD, encoding="utf-8")
        board.chmod(0o755)
        self.owner = tmp / "owner"
        self.owner.mkdir()
        self.scratch = tmp / "t"
        self.scratch.mkdir()
        # The user unit the commit-cache check reads; it names no managed cache.
        (tmp / "units").mkdir()
        (tmp / "units" / "syrd516-ticket-board.service").write_text(
            "[Service]\nExecStart=/usr/bin/python3 ticket-board.py\n", encoding="utf-8")

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.stubs}:/usr/bin:/bin",
            "HOME": str(self.owner),
            "LANG": "C.UTF-8",
            "TMPDIR": str(self.scratch),
            "TICKET_BOARD_PROJECT": "syrd516",
            "TICKET_BOARD_OWNER_HOME": str(self.owner),
            "TICKET_BOARD_COMMIT_GIT_DIR": str(self.tmp / "commit.git"),
            "TICKET_BOARD_PYTHON": "/usr/bin/python3",
            "TICKET_BOARD_SYSTEM_UNIT_PATH": str(self.tmp / "absent.service"),
            "UNIT_DIR": str(self.tmp / "units"),
            "BOARD_UNIX_SOCKET": str(self.tmp / "board.sock"),
            "BOARD_SMOKE_TIMEOUT_SECONDS": "2",
            "BOARD_CANARY_TIMEOUT_SECONDS": "2",
            "BOARD_CANARY_USER": os.environ.get("USER") or subprocess.run(
                ["id", "-un"], capture_output=True, text=True, check=True).stdout.strip(),
            "FAKE_RELEASE": str(self.release),
        }
        env.update(extra)
        return env

    def run(self, argv: list[str], **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, env=self.env(**extra), capture_output=True, text=True, timeout=60)

    def sourced(self, body: str, **extra: str) -> subprocess.CompletedProcess[str]:
        script = self.tree / "scripts" / SERVICE.name
        return self.run(["bash", "-c", f"source {script}\n{body}"], **extra)

    def refused(self) -> list[str]:
        return self.refusals.read_text(encoding="utf-8").splitlines() if self.refusals.exists() else []

    def leftovers(self) -> list[str]:
        return sorted(path.name for path in self.scratch.iterdir())


def top_level_functions(path: Path) -> list[str]:
    return re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) *\{", path.read_text(encoding="utf-8"), flags=re.M)


def test_ownership_and_files() -> None:
    assert top_level_functions(HEALTH) == list(HEALTH_FUNCTIONS), top_level_functions(HEALTH)
    assert not set(top_level_functions(SERVICE)) & set(HEALTH_FUNCTIONS), "a health function is defined twice"
    service_lines = SERVICE.read_text(encoding="utf-8").splitlines()
    assert service_lines.count(SOURCE_LINE) == 1, "the service script must source the health file exactly once"
    at = service_lines.index(SOURCE_LINE)
    before = [line for line in service_lines[:at] if re.match(r"^[A-Za-z_]\w*\(\) *\{", line)]
    after = [line for line in service_lines[at:] if re.match(r"^[A-Za-z_]\w*\(\) *\{", line)]
    assert before and after, "the health file must be sourced between function definitions, before the main guard"
    assert before[-1].startswith("ensure_database_roles()") and after[0].startswith("render_unit()"), (before[-1], after[0])
    assert at < service_lines.index('if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then'), "sourced after main runs"
    health_text = HEALTH.read_text(encoding="utf-8")
    assert not health_text.startswith("#!"), "the health file is sourced, never run"
    assert not HEALTH.stat().st_mode & 0o111, oct(HEALTH.stat().st_mode)
    assert SERVICE.stat().st_mode & 0o111, "the service script must stay executable"
    for path in (SERVICE, HEALTH):
        check = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert check.returncode == 0, (path, check.stderr)


def test_whole_copy_runs_and_lone_copy_fails_before_any_command(fx: Fixture) -> None:
    whole = fx.run([str(fx.tree / "scripts" / SERVICE.name), "render-unit"])
    assert whole.returncode == 0 and "[Service]" in whole.stdout, whole.stderr
    for command in ("render-unit", "deploy-restart", "start"):
        lone = fx.run([str(fx.lone / "scripts" / SERVICE.name), command])
        assert lone.returncode != 0, (command, lone.stdout)
        assert lone.stdout == "", (command, lone.stdout)
        assert "ticket-board-service-health.sh: No such file or directory" in lone.stderr, (command, lone.stderr)
    assert fx.refused() == [], fx.refused()
    assert not (fx.owner / "syrd516-ticketboard-live").exists(), "a lone copy created release state"


def test_canary_lifecycle(fx: Fixture) -> None:
    passed = fx.sourced(f"run_release_canary {fx.release} user")
    assert passed.returncode == 0, passed.stderr
    assert f"canary-pass for {fx.release}" in passed.stderr, passed.stderr
    assert fx.leftovers() == [], fx.leftovers()
    # The canary board is gone once the check returns, not merely unreferenced.
    port = int(re.search(r" on port (\d+)$", passed.stderr, flags=re.M).group(1))
    with socket.socket() as probe:
        probe.settimeout(2)
        assert probe.connect_ex(("127.0.0.1", port)) != 0, f"the canary is still serving on port {port}"

    failed = fx.sourced(f"run_release_canary {fx.release} user", FAKE_CANARY_FAIL="1")
    assert failed.returncode == 1, failed.stderr
    lines = failed.stderr.splitlines()
    assert "[ticket-board-service] canary log follows:" in lines, lines
    assert "[ticket-board-service] canary: fake release board: refusing to start" in lines, lines
    assert lines[-1] == (
        "[ticket-board-service] ERROR: canary failed; leaving "
        f"{fx.owner}/syrd516-ticketboard-live/current unchanged"
    ), lines[-1]
    assert fx.leftovers() == [], fx.leftovers()

    skipped = fx.sourced(f"run_release_canary {fx.release} user", TICKET_BOARD_SKIP_CANARY="1")
    assert skipped.returncode == 0 and "skipping deploy canary" in skipped.stderr, skipped.stderr
    assert fx.refused() == [], fx.refused()


def test_redefinitions_after_sourcing_are_the_ones_that_run(fx: Fixture) -> None:
    # A plumbing function the health file calls, redefined by the caller.
    body = (
        'systemctl_system() { echo "systemctl_system $*" >>"$TMPDIR/../calls"; [[ "$1" != start ]]; }\n'
        f"run_release_canary {fx.release} system"
    )
    systemd = fx.sourced(body, BOARD_CANARY_USER="boardsvc")
    calls = (fx.tmp / "calls").read_text(encoding="utf-8").splitlines()
    assert calls == [
        "systemctl_system stop syrd516-ticket-board-canary.service",
        "systemctl_system start syrd516-ticket-board-canary.service",
        "systemctl_system status syrd516-ticket-board-canary.service --no-pager -l",
        "systemctl_system stop syrd516-ticket-board-canary.service",
    ], calls
    assert systemd.returncode == 1 and "canary failed" in systemd.stderr, systemd.stderr
    env_file = fx.owner / "syrd516-ticketboard-live" / "canary.env"
    assert f'BOARD_CANARY_RELEASE_DIR="{fx.release}"' in env_file.read_text(encoding="utf-8")
    assert env_file.stat().st_mode & 0o777 == 0o644, oct(env_file.stat().st_mode)
    # A health function redefined by the caller.
    smoke = fx.sourced('smoke_check_url() { echo "redefined $1 $2"; }\nsmoke_check_http', BOARD_PORT="4321")
    assert smoke.stdout == "redefined http://127.0.0.1:4321/api/board 2\n", smoke.stdout
    assert fx.refused() == [], fx.refused()
    options = fx.sourced('echo "$-"; shopt -po errexit nounset pipefail')
    assert options.stdout.splitlines()[1:] == ["set -o errexit", "set -o nounset", "set -o pipefail"], options.stdout


def deploy(fx: Fixture, build_id: str | None, **extra: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    server = None
    port = free_port()
    if build_id is not None:
        server = LiveBoard(build_id)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = fx.sourced(ORCHESTRATION_RECORDERS + "deploy_restart_service", BOARD_PORT=str(port), **extra)
    finally:
        if server is not None:
            server.shutdown()
    return result, [line for line in result.stdout.splitlines() if line.startswith("STEP ")]


def test_deploy_order_around_the_health_checks(fx: Fixture) -> None:
    ok, steps = deploy(fx, "abc123")
    assert ok.returncode == 0, ok.stderr
    assert steps == [
        "STEP assert_user_manager_identity", "STEP stop_listener", "STEP migrate",
        f"STEP activate {fx.release}", "STEP verify_current abc123", "STEP restart user",
        "STEP start_listener", "STEP listener_authority",
    ], steps
    assert ok.stderr.index("canary-pass") < ok.stderr.index("live build-id verification passed for abc123"), ok.stderr

    for label, build_id in (("wrong build", "zzz"), ("live board down", None)):
        failed, steps = deploy(fx, build_id)
        assert failed.returncode == 1, (label, failed.stderr)
        assert steps == [
            "STEP assert_user_manager_identity", "STEP stop_listener", "STEP migrate",
            f"STEP activate {fx.release}", "STEP verify_current abc123", "STEP restart user",
            "STEP rollback user /previous/release", "STEP start_listener",
        ], (label, steps)

    broken, steps = deploy(fx, "abc123", FAKE_CANARY_FAIL="1")
    assert broken.returncode == 1 and "canary failed" in broken.stderr, broken.stderr
    assert steps == ["STEP assert_user_manager_identity", "STEP stop_listener", "STEP migrate"], steps
    assert fx.leftovers() == [], fx.leftovers()
    assert fx.refused() == [], fx.refused()


def main() -> int:
    test_ownership_and_files()
    checks = 1
    for case in (
        test_whole_copy_runs_and_lone_copy_fails_before_any_command,
        test_canary_lifecycle,
        test_redefinitions_after_sourcing_are_the_ones_that_run,
        test_deploy_order_around_the_health_checks,
    ):
        # A short base keeps every Unix socket path under the 108-byte limit.
        with tempfile.TemporaryDirectory(prefix="s516.", dir="/tmp") as tmpdir:
            case(Fixture(Path(tmpdir)))
        checks += 1
    print(f"ticket_board_service_health_owner_test: {checks} cases ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
