#!/usr/bin/env python3
"""SYRD-69 regressions for project-account/process-bound role authority."""

from __future__ import annotations

import sys
import json
import os
import pwd
import subprocess
import tempfile
import threading
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.peer_identity import SessionIdentity
from scripts.ticket_board.project_provision import build_plan, render_board_unit
from scripts.ticket_board.server import (
    CallerIdentityError,
    PeerCredentials,
    ProcessRoleAuthority,
)
from scripts.ticket_board.workflow_config import validate
from scripts import presentation_controller, role_runtime, team_launcher


class FakeApp:
    def __init__(self) -> None:
        self.assignments = {
            (701, 11001, 4242): {
                "role": "director",
                "runtime": "claude",
                "actual_target": "demo-director-recovery:0.0",
            }
        }

    def runtime_assignment_for_process(self, pid: int, start_time: int, uid: int):
        return self.assignments.get((pid, start_time, uid))


def test_registered_pane_gets_role_but_sibling_process_cannot_claim_it() -> None:
    sessions = {
        9001: SessionIdentity(701, 11001),
        9002: SessionIdentity(702, 11002),
    }
    authority = ProcessRoleAuthority(
        FakeApp(),
        "demo-agent",
        resolve_uid=lambda _account: 4242,
        resolve_session=sessions.get,
        session_live=lambda _identity: True,
    )
    assert authority.role_for_peer(PeerCredentials(9001, 4242, 4242)) == "director"
    try:
        authority.role_for_peer(PeerCredentials(9002, 4242, 4242))
    except CallerIdentityError as exc:
        assert "no PostgreSQL role assignment" in str(exc)
    else:
        raise AssertionError("a sibling process inherited director authority from the shared uid")


def test_pid_reuse_and_another_project_uid_fail_closed() -> None:
    authority = ProcessRoleAuthority(
        FakeApp(),
        "demo-agent",
        resolve_uid=lambda _account: 4242,
        resolve_session=lambda _pid: SessionIdentity(701, 99999),
        session_live=lambda _identity: True,
    )
    for peer in (PeerCredentials(9001, 4242, 4242), PeerCredentials(9001, 5252, 5252)):
        try:
            authority.role_for_peer(peer)
        except CallerIdentityError:
            pass
        else:
            raise AssertionError(f"unregistered identity was authorized: {peer}")


def test_fresh_plan_creates_no_role_accounts_or_role_account_unit_contract() -> None:
    plan = build_plan(project="demo", owner_user="demo-agent")
    unit = render_board_unit(plan)
    assert plan.role_accounts == ()
    assert plan.roles_group == "demo-agent"
    assert "TICKET_BOARD_ROLE_ACCOUNTS" not in unit
    assert "Group=demo-agent" in unit
    assert "Environment=TICKET_BOARD_TENANT_USER=demo-agent" in unit


def test_runtime_schema_commits_routing_and_process_identity_together() -> None:
    sql = (Path(__file__).parents[1] / "scripts/ticket_board/schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS ticket_board.role_runtime_assignments" in sql
    assert "CREATE TABLE IF NOT EXISTS ticket_board.role_runtime_assignment_history" in sql
    assert "UNIQUE(process_pid, process_start_time, process_uid)" in sql
    function = sql.split("CREATE OR REPLACE FUNCTION ticket_board.register_role_runtime", 1)[1]
    assert "actual_target=EXCLUDED.actual_target" in function
    assert "process_pid=EXCLUDED.process_pid" in function
    assert "generation=assignment.generation + 1" in function
    assert "WHERE assignment.generation = p_expected_generation" in function
    assert "configured->>'target' IS DISTINCT FROM p_actual_target" in function
    assert "INSERT INTO ticket_board.role_runtime_assignment_history" in function
    assert "NOT EXISTS (SELECT FROM ticket_board.workflow_configuration)" in function
    assert "normalized_role=ANY(owner_roles)" in function


def test_replacement_target_is_allowed_only_inside_the_project_namespace() -> None:
    document = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
    for role in document["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "demo-", 1)
    document["project"] = "demo"
    next(role for role in document["roles"] if role["name"] == "director")["target"] = "demo-recovery-7:0.0"
    assert validate(document, project="demo")["roles"][0]["target"] == "demo-recovery-7:0.0"
    next(role for role in document["roles"] if role["name"] == "director")["target"] = "other-recovery-7:0.0"
    try:
        validate(document, project="demo")
    except ValueError as exc:
        assert "selected project" in str(exc)
    else:
        raise AssertionError("cross-project replacement target was accepted")


def test_pane_launch_registers_before_execing_cli() -> None:
    role = team_launcher.RoleConfig(
        role="app", slot=0, detached=False, tmux_session="demo-app",
        target="demo-app:0.0", workdir="/srv/demo/app", cli=["codex"],
        model="", model_arg="--model", effort="", yolo=False, extra_args=[],
        resume_mode="subcommand", resume_flag="--resume", resume_subcommand="resume",
        fresh_session_per_ticket=False, live_commands=["codex"],
        env={
            "TICKET_BOARD_SOCKET": "/run/demo-ticket-board/ticket-board.sock",
            "TICKET_BOARD_PROCESS_AUTHORITY": "1",
        },
    )
    command = team_launcher.cli_command_for_role(
        role, session_dir=Path("/srv/demo/state/roles/app")
    )
    assert command[0] == "env"
    register = next(
        index for index, value in enumerate(command)
        if value.endswith("ticket-board-register-runtime")
    )
    assert command[register + 1 : register + 7] == [
        "--socket", "/run/demo-ticket-board/ticket-board.sock",
        "--role", "app", "--runtime", "codex",
    ]
    assert command[-1] == "codex"


def _project_account_config(root: Path) -> tuple[team_launcher.ProjectConfig, Path]:
    worktree = root / "director"
    worktree.mkdir()
    layout = root / "layout.json"
    layout.write_text("{}\n")
    path = root / "demo.json"
    path.write_text(json.dumps({
        "project": "demo",
        "run_as_user": pwd.getpwuid(__import__("os").geteuid()).pw_name,
        "role_state_isolation": True,
        "layout": str(layout),
        "board_url": "http://127.0.0.1:29999",
        "board_socket": str(root / "board.sock"),
        "session_dir": str(root / "sessions"),
        "roles": [{
            "role": "director", "slot": 0, "cli": ["claude"],
            "target": "demo-director:0.0", "workdir": str(worktree),
        }],
    }) + "\n")
    return team_launcher.load_project_config("demo", path), path


class JsonResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_presentation_resolves_replacement_target_from_atomic_assignment() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config, _path = _project_account_config(Path(tmp))
        payload = json.dumps({
            "project": "demo",
            "authority_mode": "process",
            "assignments": {
                "director": {
                    "runtime": "claude",
                    "actual_target": "demo-director-recovery-7:0.0",
                }
            },
        }).encode()
        resolved = presentation_controller.runtime_assignment_config(
            config, opener=lambda _url: JsonResponse(payload)
        )
        assert resolved.roles[0].target == "demo-director-recovery-7:0.0"
        assert resolved.roles[0].tmux_session == "demo-director-recovery-7"


def test_mixed_version_board_refuses_before_launch_mutates_anything() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config, path = _project_account_config(root)
        calls = []
        original_probe = team_launcher.process_authority_board_compatibility
        team_launcher.process_authority_board_compatibility = lambda _config: (
            False, "HTTP 404 from old board"
        )
        try:
            output: list[str] = []
            result = team_launcher.launch_project(
                config,
                config_path=path,
                mode="attach-or-start",
                script_path=ROOT / "scripts/team-launcher",
                runner=lambda args, **_kwargs: calls.append(args),
                print_func=output.append,
            )
        finally:
            team_launcher.process_authority_board_compatibility = original_probe
        assert result == 1
        assert calls == []
        assert "before changing local state" in output[0]


def test_board_compatibility_probe_requires_process_authority_mode() -> None:
    class Response:
        def __init__(self, payload: dict) -> None:
            self.status = 200
            self._body = json.dumps(payload).encode()

        def read(self) -> bytes:
            return self._body

    class Connection:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def request(self, method: str, path: str) -> None:
            assert (method, path) == ("GET", "/api/runtime-assignments")

        def getresponse(self) -> Response:
            return Response(self.payload)

        def close(self) -> None:
            pass

    with tempfile.TemporaryDirectory() as tmp:
        config, _path = _project_account_config(Path(tmp))
        for mode, expected in (("legacy_uid", False), ("process", True)):
            ready, _reason = team_launcher.process_authority_board_compatibility(
                config,
                connection_factory=lambda _socket, _timeout, mode=mode: Connection({
                    "project": "demo", "authority_mode": mode, "assignments": {},
                }),
            )
            assert ready is expected


def test_runtime_switch_uses_the_roles_private_session_store() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config, _path = _project_account_config(Path(tmp))
        role = config.roles[0]
        seen: dict[str, Path] = {}
        original_start = team_launcher.ensure_visible_role_session_for_viewer
        team_launcher.ensure_visible_role_session_for_viewer = (
            lambda _role, **kwargs: seen.setdefault("session_dir", kwargs["session_dir"]) and 0
        )
        try:
            assert role_runtime._default_start(
                role,
                config=config,
                pane_state_dir=Path(tmp) / "pane-state",
                runner=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
            ) == 0
        finally:
            team_launcher.ensure_visible_role_session_for_viewer = original_start
        assert seen["session_dir"] == config.session_dir / "roles/director"


def test_director_control_resolves_logical_roles_and_never_uses_legacy_sudo() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            assert self.path == "/api/runtime-assignments/director"
            body = json.dumps({
                "project": "demo",
                "authority_mode": "process",
                "assignment": {
                    "role": "director",
                    "runtime": "claude",
                    "actual_target": "demo-director-recovery-7:0.0",
                },
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "tmux.log"
            tmux = root / "tmux"
            tmux.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {log}\n"
                "printf 'assigned pane\\n'\n"
            )
            tmux.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{root}:{os.environ['PATH']}",
                "TICKET_BOARD_PROJECT": "demo",
                "TICKET_BOARD_PROCESS_AUTHORITY": "1",
                "TICKET_BOARD_URL": f"http://127.0.0.1:{server.server_port}",
            }
            proc = subprocess.run(
                [str(ROOT / "scripts/directorctl"), "capture", "director", "1"],
                env=env, text=True, capture_output=True,
            )
            assert proc.returncode == 0, proc.stderr
            assert proc.stdout.strip() == "assigned pane"
            invocation = log.read_text()
            assert "demo-director-recovery-7:0.0" in invocation
            assert "sudo" not in invocation
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_eight_ephemeral_workers_need_no_accounts_and_keep_private_state_paths() -> None:
    owner = pwd.getpwuid(__import__("os").geteuid()).pw_name
    workers = [f"worker{index}" for index in range(8)]
    plan = build_plan(
        project="demo", owner_user=owner, implementer_roles=workers,
        include_designer=False, include_audit=False,
    )
    payload = team_launcher._new_project_launcher_config_payload(
        plan,
        repository=Path("/srv/demo"),
        implementer_roles=workers,
        role_clis=[("director", "claude"), *((role, "hermes") for role in workers)],
        include_designer=False,
        include_audit=False,
    )
    assert all("run_as_user" not in role for role in payload["roles"])
    assert sum(bool(role.get("detached")) for role in payload["roles"]) == 3
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        payload["layout"] = str(root / "layout.json")
        payload["repository"] = str(root / "repo")
        payload.pop("worktree_base", None)
        payload.pop("control_repository", None)
        payload["session_dir"] = str(root / "sessions")
        for role in payload["roles"]:
            role["workdir"] = str(root / "worktrees" / role["role"])
        (root / "repo").mkdir()
        path = root / "demo.json"
        path.write_text(json.dumps(payload))
        config = team_launcher.load_project_config("demo", path)
        state_paths = {team_launcher.role_session_dir(config, role) for role in config.roles}
        assert len(state_paths) == len(config.roles)
        assert {team_launcher.role_run_as_user(config, role) for role in config.roles} == {owner}


def _legacy_config(root: Path) -> tuple[team_launcher.ProjectConfig, Path]:
    owner = pwd.getpwuid(0 if __import__("os").geteuid() == 0 else __import__("os").geteuid()).pw_name
    layout = root / "layout.json"
    layout.write_text("{}\n")
    worktree = root / "director"
    worktree.mkdir()
    path = root / "demo.json"
    path.write_text(json.dumps({
        "project": "demo",
        "layout": str(layout),
        "session_dir": str(root / "sessions"),
        "run_as_user": owner,
        "roles": [{
            "role": "director", "slot": 0, "cli": ["claude"],
            "target": "demo-director:0.0", "workdir": str(worktree),
            "run_as_user": "demo-director",
        }],
    }) + "\n")
    return team_launcher.load_project_config("demo", path), path


def test_legacy_account_binding_is_removed_only_after_live_pane_is_gone() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config, path = _legacy_config(Path(tmp))
        assert team_launcher.role_run_as_user(config, config.roles[0]) == "demo-director"

        def live_runner(args, **_kwargs):
            return subprocess.CompletedProcess(args, 0)

        changed, problems = team_launcher.repatriate_role_runtime_state(
            config, config_path=path, runner=live_runner
        )
        assert not changed and problems
        assert json.loads(path.read_text())["roles"][0]["run_as_user"] == "demo-director"

        def stopped_runner(args, **_kwargs):
            return subprocess.CompletedProcess(args, 1 if "has-session" in args else 0)

        changed, problems = team_launcher.repatriate_role_runtime_state(
            config, config_path=path, runner=stopped_runner
        )
        assert changed and not problems
        migrated = json.loads(path.read_text())
        assert migrated["role_state_isolation"] is True
        assert "run_as_user" not in migrated["roles"][0]
        upgraded = team_launcher.load_project_config("demo", path)
        assert team_launcher.role_run_as_user(upgraded, upgraded.roles[0]) == upgraded.run_as_user


def test_shared_account_session_layout_also_refuses_a_live_migration() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config, path = _legacy_config(root)
        payload = json.loads(path.read_text())
        payload["roles"][0].pop("run_as_user")
        path.write_text(json.dumps(payload) + "\n")
        config = team_launcher.load_project_config("demo", path)

        def live_runner(args, **_kwargs):
            return subprocess.CompletedProcess(args, 0)

        changed, problems = team_launcher.repatriate_role_runtime_state(
            config, config_path=path, runner=live_runner
        )
        assert not changed and problems
        assert not json.loads(path.read_text()).get("role_state_isolation", False)


def test_claude_director_context_is_repatriated_before_binding_is_removed() -> None:
    owner = pwd.getpwuid(__import__("os").geteuid()).pw_name
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        owner_home = root / "owner-home"
        legacy_home = root / "director-home"
        worktree = root / "worktrees" / "director"
        worktree.mkdir(parents=True)
        owner_sessions = owner_home / ".local/state/demo-ticket-board/pane-sessions"
        legacy_sessions = legacy_home / ".local/state/demo-ticket-board/pane-sessions"
        legacy_sessions.mkdir(parents=True)
        config_path = root / "demo.json"
        config_path.write_text(json.dumps({
            "project": "demo", "run_as_user": owner,
            "layout": str(root / "layout.json"),
            "session_dir": str(owner_sessions),
            "roles": [{
                "role": "director", "slot": 0, "cli": ["claude"],
                "target": "demo-director:0.0", "workdir": str(worktree),
                "run_as_user": "demo-director",
            }],
        }) + "\n")
        (root / "layout.json").write_text("{}\n")
        session_id = "11111111-2222-3333-4444-555555555555"
        transcript = (
            team_launcher._claude_project_dir_for_workdir(str(worktree), home=legacy_home)
            / f"{session_id}.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"type":"summary"}\n')
        record_path = legacy_sessions / team_launcher.session_file_name("demo-director:0.0")
        record_path.write_text(json.dumps({
            "target": "demo-director:0.0", "session_id": session_id,
            "payload": {"transcript_path": str(transcript)},
        }) + "\n")

        original_home = team_launcher.home_dir_for_user
        original_account_sessions = team_launcher.account_session_dir
        team_launcher.home_dir_for_user = lambda account: (
            owner_home if account == owner else legacy_home if account == "demo-director" else None
        )
        team_launcher.account_session_dir = lambda account, project: (
            legacy_sessions if account == "demo-director" else owner_sessions
        )
        try:
            config = team_launcher.load_project_config("demo", config_path)

            def stopped_runner(args, **_kwargs):
                return subprocess.CompletedProcess(args, 1 if "has-session" in args else 0)

            changed, problems = team_launcher.repatriate_role_runtime_state(
                config, config_path=config_path, runner=stopped_runner
            )
        finally:
            team_launcher.home_dir_for_user = original_home
            team_launcher.account_session_dir = original_account_sessions

        assert changed and not problems
        target_record = owner_sessions / "roles/director" / record_path.name
        migrated = json.loads(target_record.read_text())
        target_transcript = Path(migrated["payload"]["transcript_path"])
        assert migrated["session_id"] == session_id
        assert target_transcript == owner_home / transcript.relative_to(legacy_home)
        assert target_transcript.read_text() == transcript.read_text()
        assert transcript.exists(), "legacy context is copied, never deleted"


def test_codex_director_context_is_repatriated_before_binding_is_removed() -> None:
    owner = pwd.getpwuid(os.geteuid()).pw_name
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        owner_home = root / "owner-home"
        legacy_home = root / "director-home"
        worktree = root / "worktrees/director"
        worktree.mkdir(parents=True)
        owner_sessions = owner_home / ".local/state/demo-ticket-board/pane-sessions"
        legacy_sessions = legacy_home / ".local/state/demo-ticket-board/pane-sessions"
        legacy_sessions.mkdir(parents=True)
        config_path = root / "demo.json"
        config_path.write_text(json.dumps({
            "project": "demo", "run_as_user": owner,
            "layout": str(root / "layout.json"), "session_dir": str(owner_sessions),
            "roles": [{
                "role": "director", "slot": 0, "cli": ["codex"],
                "target": "demo-director:0.0", "workdir": str(worktree),
                "run_as_user": "demo-director",
            }],
        }) + "\n")
        (root / "layout.json").write_text("{}\n")
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        transcript = legacy_home / team_launcher.CODEX_SESSIONS_DIR_NAME / "2026/09" / (
            f"rollout-{session_id}.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"type":"session_meta"}\n')
        record_path = legacy_sessions / team_launcher.session_file_name("demo-director:0.0")
        record_path.write_text(json.dumps({
            "target": "demo-director:0.0", "session_id": session_id,
            "payload": {"transcript_path": str(transcript)},
        }) + "\n")

        original_home = team_launcher.home_dir_for_user
        original_account_sessions = team_launcher.account_session_dir
        team_launcher.home_dir_for_user = lambda account: (
            owner_home if account == owner else legacy_home if account == "demo-director" else None
        )
        team_launcher.account_session_dir = lambda account, project: (
            legacy_sessions if account == "demo-director" else owner_sessions
        )
        try:
            config = team_launcher.load_project_config("demo", config_path)
            stopped = lambda args, **_kwargs: subprocess.CompletedProcess(
                args, 1 if "has-session" in args else 0
            )
            changed, problems = team_launcher.repatriate_role_runtime_state(
                config, config_path=config_path, runner=stopped
            )
        finally:
            team_launcher.home_dir_for_user = original_home
            team_launcher.account_session_dir = original_account_sessions

        assert changed and not problems
        migrated = json.loads((owner_sessions / "roles/director" / record_path.name).read_text())
        target_transcript = Path(migrated["payload"]["transcript_path"])
        assert target_transcript == owner_home / transcript.relative_to(legacy_home)
        assert target_transcript.read_text() == transcript.read_text()
        assert transcript.exists(), "legacy Codex context is copied, never deleted"


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print("ticket_board_process_authority_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
