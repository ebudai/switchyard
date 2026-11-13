#!/usr/bin/env python3
"""SYRD-767: a restarted role's display slots come back while another role is stopped, and `team-launcher --dry-run
pane ...` changes nothing.

1. On syrd, with Audit stopped, every other restarted role printed "its display slots could not be reconnected
   (SystemExit: switchyard: no live runtime assignment for configured role(s): audit)": the reconnection resolved
   every configured role from the board. Here the board's runtime assignments are served over loopback HTTP, with
   one role absent; the reconnection of another role re-attaches its slot, and only the absent role's own
   reconnection is refused, naming it alone.
2. `team-launcher syrd --dry-run pane attach-or-start audit` started a real Audit session. Here the real
   `team-launcher` is run for every pane mode with --dry-run, on an isolated tmux server: each is refused, says so,
   and no tmux server or session exists afterwards.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def serve_assignments(assignments: dict) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            if self.path != "/api/runtime-assignments":
                self.send_error(404)
                return
            body = json.dumps({"project": "p767", "authority_mode": "process", "assignments": assignments}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def project(tmp: Path, *, board_url: str) -> Path:
    for role in ("librarian", "audit"):
        (tmp / role).mkdir(exist_ok=True)
    doc = {"project": "p767", "board_url": board_url, "board_socket": str(tmp / "board.sock"),
           "role_state_isolation": True, "session_dir": str(tmp / "sessions"), "desktop_access": {"mode": "headless"},
           "roles": [{"role": role, "slot": slot, "workdir": str(tmp / role), "cli": ["claude"]}
                     for slot, role in enumerate(("librarian", "audit"))]}
    path = tmp / "p767.json"
    path.write_text(json.dumps(doc))
    return path


def test_a_stopped_role_does_not_keep_another_roles_slots_unshown(tmp: Path) -> None:
    from scripts import presentation_controller as pc
    from scripts import team_launcher as launcher

    live = {"librarian": {"role": "librarian", "runtime": "claude", "actual_target": "p767-librarian:0.0",
                          "process_pid": 4242, "process_start_time": 1, "generation": 3}}
    server = serve_assignments(live)  # audit has no live assignment: it is stopped
    try:
        path = project(tmp, board_url=f"http://127.0.0.1:{server.server_port}")
        config = launcher.load_project_config("p767", path)
        shown: list[tuple[int, str, str]] = []
        slots = {"librarian": (0,), "audit": (1,)}
        with mock.patch.object(pc, "slots_showing_role", lambda c, *, role_name, **_k: slots[role_name]), \
                mock.patch.object(pc, "_presentation_client_ttys", lambda *a, **k: set()), \
                mock.patch.object(pc, "_reconcile_viewer_observers", lambda *a, **k: None), \
                mock.patch.object(pc, "_tmux_runner", lambda c, runner: runner), \
                mock.patch.object(pc, "_configure_display_session",
                                  lambda c, slot, name, **_k: shown.append(
                                      (slot, name, launcher._role_by_name(c, name).tmux_session))):
            try:
                got = pc.reconnect_role_slots(config, config_path=path, role_name="librarian", state_path=tmp / "s.json")
            except SystemExit as exc:
                got = f"refused: {exc}"
            check(got == (0,) and shown == [(0, "librarian", "p767-librarian")],
                  f"librarian's slot is re-attached to its live session while audit is stopped: {got} {shown}")
            try:
                refused = pc.reconnect_role_slots(config, config_path=path, role_name="audit", state_path=tmp / "s.json")
            except SystemExit as exc:
                refused = str(exc)
            check(refused == "switchyard: no live runtime assignment for configured role(s): audit"
                  and shown == [(0, "librarian", "p767-librarian")],
                  f"audit's own reconnection is refused, naming it alone, and attaches nothing: {refused!r} {shown}")
    finally:
        server.shutdown()
        server.server_close()


def test_a_pane_dry_run_changes_nothing(tmp: Path) -> None:
    path = project(tmp, board_url="http://127.0.0.1:1/")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("TMUX", "TICKET_BOARD_", "PGU_", "CLAUDE", "SWITCHYARD_"))}
    env["TMUX_TMPDIR"] = str(tmp / "tmux")
    (tmp / "tmux").mkdir()
    socket = tmp / "tmux" / f"tmux-{os.getuid()}" / "default"
    bindir = tmp / "bin"
    bindir.mkdir()
    started = tmp / "cli-started"
    # Were the CLI ever run, it would say so here: a dry run must never get this far.
    (bindir / "claude").write_text(f"#!/bin/sh\ntouch {started}\nexec sleep 600\n")
    (bindir / "claude").chmod(0o755)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    for mode, extra in (("attach-or-start", []), ("start", []), ("reload", []), ("attach", []),
                        ("attach-role", ["--slot", "0"]), ("detach-role", [])):
        run = subprocess.run([sys.executable, str(ROOT / "scripts" / "team-launcher"), "p767", "--dry-run", "pane", mode,
                              "audit", "--config", str(path), "--skip-launcher-check", *extra],
                             capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=60)
        said = run.stdout + run.stderr
        sessions = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True, env=env)
        check(run.returncode != 0 and f"--dry-run is refused for `pane {mode}`" in said and "nothing was changed" in said,
              f"pane {mode} --dry-run is refused and says so (exit {run.returncode}): {said[-400:]}")
        check(sessions.returncode != 0 and not socket.exists() and not started.exists(),
              f"pane {mode} --dry-run started no tmux server, session or CLI: {sessions.stdout!r} {socket.exists()}")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            with tempfile.TemporaryDirectory(prefix="s767.") as tmp:
                case(Path(tmp))
    print(f"role_restart_slots_and_pane_dry_run_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
