#!/usr/bin/env python3

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from importlib.machinery import SourceFileLoader
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ticket-board-verify-resume"


def load_module():
    loader = SourceFileLoader("ticket_board_verify_resume", str(SCRIPT))
    spec = importlib.util.spec_from_loader("ticket_board_verify_resume", loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BoardHandler(http.server.BaseHTTPRequestHandler):
    build_id = "abcdef123456"
    ticket_count = 3

    def do_GET(self) -> None:
        if self.path != "/api/board":
            self.send_response(404)
            self.end_headers()
            return
        payload = {
            "build_id": self.build_id,
            "tickets": [{"id": f"TEST-{idx}"} for idx in range(self.ticket_count)],
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ThreadedServer:
    def __enter__(self):
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), BoardHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address
        self.url = f"http://{host}:{port}"
        return self

    def __exit__(self, *_exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def make_launcher_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test User"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    marker = path / "README"
    marker.write_text("launcher\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return path


def make_fixture(root: Path, *, boot_id: str, boot_time: int) -> dict[str, Path]:
    runtime_sessions = root / "run" / "ticket-board" / "pane-sessions"
    durable_sessions = root / "state" / "ticket-board" / "pane-sessions"
    pane_state = root / "run" / "ticket-board" / "pane-state"
    current_parent = root / "live" / "releases"
    current_target = current_parent / BoardHandler.build_id
    current_target.mkdir(parents=True)
    current_link = root / "live" / "current"
    current_link.parent.mkdir(parents=True, exist_ok=True)
    current_link.symlink_to(current_target)
    frame_dir = root / "frames"
    frame_dir.mkdir()
    tmux_file = root / "tmux.txt"
    tmux_file.write_text("demo-ops\ndemo-inspector\n", encoding="utf-8")
    tmux_panes_file = root / "tmux-panes.json"
    tmux_panes_file.write_text(
        json.dumps(
            {
                "panes": {
                    "demo-ops:0.0": {
                        "target": "demo-ops:0.0",
                        "pane_pid": 1200,
                        "argvs": [["bash"], ["codex", "resume", "ops-session", "--model", "gpt-test"]],
                    },
                    "demo-inspector:0.0": {
                        "target": "demo-inspector:0.0",
                        "pane_pid": 1300,
                        "argvs": [["bash"], ["agy", "--conversation", "inspector-session"]],
                    },
                }
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    boot_id_file = root / "boot_id"
    boot_time_file = root / "boot_time"
    launcher_repo = make_launcher_repo(root / "launcher")
    boot_id_file.write_text(boot_id, encoding="utf-8")
    boot_time_file.write_text(str(boot_time), encoding="utf-8")
    config = root / "config.json"
    config.write_text(
        json.dumps(
            {
                "project": "demo",
                "roles": [
                    {
                        "role": "ops",
                        "target": "demo-ops:0.0",
                        "tmux_session": "demo-ops",
                        "cli": ["codex"],
                    },
                    {
                        "role": "inspector",
                        "target": "demo-inspector:0.0",
                        "tmux_session": "demo-inspector",
                        "cli": ["agy"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    for target, session_id in {
        "demo-ops:0.0": "ops-session",
        "demo-inspector:0.0": "inspector-session",
    }.items():
        file_name = target.replace(":", "_") + ".json"
        write_json(runtime_sessions / file_name, {"target": target, "session_id": session_id})
        write_json(durable_sessions / file_name, {"target": target, "session_id": session_id})
        write_json(pane_state / file_name, {"target": target, "state": "idle", "updated_at": boot_time + 10})
        os.utime(runtime_sessions / file_name, (boot_time + 10, boot_time + 10))
        os.utime(pane_state / file_name, (boot_time + 10, boot_time + 10))
    return {
        "runtime_sessions": runtime_sessions,
        "durable_sessions": durable_sessions,
        "pane_state": pane_state,
        "current": current_link,
        "frame_dir": frame_dir,
        "tmux_file": tmux_file,
        "tmux_panes_file": tmux_panes_file,
        "boot_id_file": boot_id_file,
        "boot_time_file": boot_time_file,
        "config": config,
        "launcher_repo": launcher_repo,
    }


def build_args(module, paths: dict[str, Path], url: str):
    return module._resolve_args(
        module._build_parser().parse_args(
            [
                "--snapshot",
                str(paths["snapshot"]),
                "--project",
                "demo",
                "--owner-user",
                "agent",
                "--runtime-user",
                "agent",
                "--uid",
                "1001",
                "--config",
                str(paths["config"]),
                "--board-url",
                url,
                "--runtime-session-dir",
                str(paths["runtime_sessions"]),
                "--durable-session-dir",
                str(paths["durable_sessions"]),
                "--pane-state-dir",
                str(paths["pane_state"]),
                "--current-release",
                str(paths["current"]),
                "--launcher-repo",
                str(paths["launcher_repo"]),
                "--frame-dir",
                str(paths["frame_dir"]),
                "--tmux-sessions-file",
                str(paths["tmux_file"]),
                "--tmux-panes-file",
                str(paths["tmux_panes_file"]),
                "--boot-id-file",
                str(paths["boot_id_file"]),
                "--boot-time-file",
                str(paths["boot_time_file"]),
            ]
        )
    )


@contextmanager
def collect_good_pair(module):
    with tempfile.TemporaryDirectory(prefix="resume-verify.") as tmp, ThreadedServer() as server:
        root = Path(tmp)
        paths = make_fixture(root, boot_id="before-boot", boot_time=1000)
        paths["snapshot"] = root / "snapshot.json"
        args = build_args(module, paths, server.url)
        before = module.collect_snapshot(args)
        module._write_json(paths["snapshot"], before)

        paths["boot_id_file"].write_text("after-boot", encoding="utf-8")
        paths["boot_time_file"].write_text("2000", encoding="utf-8")
        for item in paths["runtime_sessions"].glob("*.json"):
            os.utime(item, (2010, 2010))
        for item in paths["pane_state"].glob("*.json"):
            os.utime(item, (2010, 2010))

        after = module.collect_snapshot(args)
        yield paths, before, after


def failed_checks(module, before: dict[str, object], after: dict[str, object]) -> dict[str, str]:
    checks = module.verify_snapshot(before, after)
    return {check.name: check.detail for check in checks if not check.ok}


def assert_check_fails(module, before: dict[str, object], after: dict[str, object], name: str) -> None:
    failed = failed_checks(module, before, after)
    assert name in failed, failed


def test_snapshot_and_verify_pass_after_real_boot_id_change() -> None:
    module = load_module()
    with collect_good_pair(module) as (_paths, before, after):
        checks = module.verify_snapshot(before, after)
        assert all(check.ok for check in checks), [(check.name, check.detail) for check in checks if not check.ok]


def test_real_reboot_observed_fails_without_boot_id_change() -> None:
    module = load_module()
    with collect_good_pair(module) as (_paths, before, after):
        after["boot"]["boot_id"] = before["boot"]["boot_id"]
        assert_check_fails(module, before, after, "real_reboot_observed")


def test_legacy_runtime_session_files_are_not_required_after_reboot() -> None:
    module = load_module()
    with collect_good_pair(module) as (_paths, before, after):
        after["runtime_sessions"]["files"] = {}
        checks = module.verify_snapshot(before, after)
        failed = {check.name: check.detail for check in checks if not check.ok}
        assert "runtime_session_tmpfs_wiped_or_recreated" not in {check.name for check in checks}
        assert "durable_sessions_survive" not in failed
        assert "durable_session_records_retained" not in failed
        assert "pane_resume_attempt_passed_to_cli" not in failed
        assert "roles_resume_recorded_sessions" not in {check.name for check in checks}


def test_durable_sessions_survive_fails_when_record_missing() -> None:
    module = load_module()
    with collect_good_pair(module) as (_paths, before, after):
        after["durable_sessions"]["files"].pop("demo-ops_0.0.json")
        assert_check_fails(module, before, after, "durable_sessions_survive")


def test_durable_session_records_retained_fails_when_a_session_id_changes() -> None:
    module = load_module()
    with collect_good_pair(module) as (_paths, before, after):
        after["durable_sessions"]["files"]["demo-ops_0.0.json"]["session_id"] = "cold-session"
        assert_check_fails(module, before, after, "durable_session_records_retained")


def test_pane_resume_attempt_passed_to_cli_fails_when_argv_lacks_recorded_id() -> None:
    module = load_module()
    with collect_good_pair(module) as (_paths, before, after):
        after["tmux"]["panes"]["demo-inspector:0.0"]["argvs"] = [["agy", "--conversation", "fresh-session"]]
        assert_check_fails(module, before, after, "pane_resume_attempt_passed_to_cli")


def test_pane_resume_attempt_passed_to_cli_states_attempt_not_outcome() -> None:
    module = load_module()
    with collect_good_pair(module) as (_paths, before, after):
        checks = module.verify_snapshot(before, after)
        details = {check.name: check.detail for check in checks}
        assert "roles_resume_recorded_sessions" not in details
        detail = details["pane_resume_attempt_passed_to_cli"]
        assert "attempt only" in detail
        assert "not CLI context retention/outcome" in detail


def test_codex_agy_pane_state_seeded_fails_when_hook_state_missing() -> None:
    module = load_module()
    with collect_good_pair(module) as (_paths, before, after):
        after["pane_state"]["files"].pop("demo-inspector_0.0.json")
        assert_check_fails(module, before, after, "codex_agy_pane_state_seeded")


def test_codex_agy_pane_state_seeded_fails_when_state_predates_boot() -> None:
    module = load_module()
    with collect_good_pair(module) as (_paths, before, after):
        first_record = next(iter(after["pane_state"]["files"].values()))
        first_record["mtime"] = after["boot"]["boot_time"] - 1
        assert_check_fails(module, before, after, "codex_agy_pane_state_seeded")


def test_frame_dir_recreated_fails_when_frame_dir_is_missing() -> None:
    module = load_module()
    with collect_good_pair(module) as (_paths, before, after):
        after["frame_dir"]["exists"] = False
        assert_check_fails(module, before, after, "frame_dir_recreated")


def test_board_build_matches_current_release_fails_on_build_mismatch() -> None:
    module = load_module()
    with collect_good_pair(module) as (_paths, before, after):
        after["board"]["build_id"] = "different-release"
        assert_check_fails(module, before, after, "board_build_matches_current_release")


def test_tmux_sessions_restored_fails_when_configured_session_missing() -> None:
    module = load_module()
    with collect_good_pair(module) as (_paths, before, after):
        after["tmux"]["sessions"] = ["demo-ops"]
        assert_check_fails(module, before, after, "tmux_sessions_restored")


def test_launcher_checkout_not_stale_fails_when_checkout_is_behind() -> None:
    module = load_module()
    with collect_good_pair(module) as (_paths, before, after):
        after["launcher_checkout"]["behind"] = 2
        assert_check_fails(module, before, after, "launcher_checkout_not_stale")


def test_verify_fails_when_a_recorded_session_id_changes() -> None:
    module = load_module()
    with collect_good_pair(module) as (_paths, before, after):
        after["durable_sessions"]["files"]["demo-ops_0.0.json"]["session_id"] = "cold-session"
        failed = failed_checks(module, before, after)
        assert "durable_sessions_survive" in failed
        assert "durable_session_records_retained" in failed


if __name__ == "__main__":
    test_snapshot_and_verify_pass_after_real_boot_id_change()
    test_real_reboot_observed_fails_without_boot_id_change()
    test_legacy_runtime_session_files_are_not_required_after_reboot()
    test_durable_sessions_survive_fails_when_record_missing()
    test_durable_session_records_retained_fails_when_a_session_id_changes()
    test_pane_resume_attempt_passed_to_cli_fails_when_argv_lacks_recorded_id()
    test_pane_resume_attempt_passed_to_cli_states_attempt_not_outcome()
    test_codex_agy_pane_state_seeded_fails_when_hook_state_missing()
    test_codex_agy_pane_state_seeded_fails_when_state_predates_boot()
    test_frame_dir_recreated_fails_when_frame_dir_is_missing()
    test_board_build_matches_current_release_fails_on_build_mismatch()
    test_tmux_sessions_restored_fails_when_configured_session_missing()
    test_launcher_checkout_not_stale_fails_when_checkout_is_behind()
    test_verify_fails_when_a_recorded_session_id_changes()
    print("ticket_board_verify_resume_test: ok")
