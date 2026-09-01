#!/usr/bin/env python3
"""Hermes session-store isolation regression tests."""

from __future__ import annotations

from dataclasses import replace

from team_launcher_test_helpers import *


def _hermes_role(role: str, *, home: Path) -> team_launcher.RoleConfig:
    return team_launcher.RoleConfig(
        role=role,
        slot=0,
        detached=False,
        tmux_session=f"porter-{role}",
        target=f"porter-{role}:0.0",
        workdir=str(home / "project"),
        cli=["hermes"],
        model="z-ai/glm-4.6",
        model_arg="-m",
        effort="",
        yolo=False,
        extra_args=[],
        resume_mode="flag",
        resume_flag="--resume",
        resume_subcommand="resume",
        live_commands=["hermes"],
        env={},
    )


def _env_value(command: list[str], name: str) -> str:
    prefix = f"{name}="
    matches = [entry[len(prefix):] for entry in _env_entries(command) if entry.startswith(prefix)]
    assert len(matches) == 1, (name, command)
    return matches[0]


def test_hermes_role_uses_private_home_and_shared_auth_config() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-hermes-home-isolation.") as tmp:
        owner_home = Path(tmp) / "home" / "porter-agent"
        shared_home = owner_home / ".hermes"
        shared_home.mkdir(parents=True)
        for name, content in {
            ".env": "OPENROUTER_API_KEY=secret\n",
            "auth.json": "{}\n",
            "config.yaml": "hooks: {}\n",
            "shell-hooks-allowlist.json": "[]\n",
            "state.db": "worker-a-private-session-content\n",
        }.items():
            (shared_home / name).write_text(content, encoding="utf-8")
        (shared_home / "skills").mkdir()
        session_dir = owner_home / ".local" / "state" / "porter-ticket-board" / "pane-sessions"
        role = _hermes_role("bulk", home=owner_home)

        hermes_home = team_launcher.prepare_hermes_home_for_role(role, session_dir=session_dir)

        assert hermes_home == team_launcher.hermes_home_for_role(role, session_dir=session_dir)
        assert hermes_home is not None
        assert hermes_home.is_dir()
        assert oct(hermes_home.stat().st_mode & 0o777) == "0o700"
        for name in (".env", "auth.json", "config.yaml", "shell-hooks-allowlist.json", "skills"):
            link = hermes_home / name
            assert link.is_symlink(), name
            assert link.resolve(strict=True) == shared_home / name
        assert not (hermes_home / "state.db").exists()
        assert not (hermes_home / "sessions").exists()
        assert not (hermes_home / "memories").exists()


def test_hermes_home_env_is_role_local_and_other_clis_are_unchanged() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-hermes-home-env.") as tmp:
        owner_home = Path(tmp) / "home" / "porter-agent"
        session_dir = owner_home / ".local" / "state" / "porter-ticket-board" / "pane-sessions"
        hermes_role = _hermes_role("bulk", home=owner_home)
        hermes_role = replace(hermes_role, env={"HERMES_HOME": str(owner_home / ".hermes")})
        codex_role = replace(
            hermes_role,
            role="ops",
            tmux_session="porter-ops",
            target="porter-ops:0.0",
            cli=["codex"],
            model_arg="--model",
            live_commands=["codex"],
            env={},
        )

        hermes_command = cli_command_for_role(hermes_role, session_dir=session_dir)
        codex_command = cli_command_for_role(codex_role, session_dir=session_dir)

        assert _env_value(hermes_command, "HERMES_HOME") == str(
            team_launcher.hermes_home_for_role(hermes_role, session_dir=session_dir)
        )
        assert not any(entry.startswith("HERMES_HOME=") for entry in _env_entries(codex_command))
        assert _command_tail(codex_command)[:2] == ["codex", "--model"]
        assert "--resume" not in _command_tail(codex_command)


def test_hermes_session_search_cannot_read_another_role_database_after_isolation() -> None:
    hermes_python = Path("/opt/hermes/venv/bin/python")
    if not hermes_python.exists():
        print("team_launcher_hermes_session_isolation_test: skipped session_search probe; missing Hermes venv")
        return
    with tempfile.TemporaryDirectory(prefix="pgu-hermes-session-search.") as tmp:
        owner_home = Path(tmp) / "home" / "porter-agent"
        shared_home = owner_home / ".hermes"
        shared_home.mkdir(parents=True)
        session_dir = owner_home / ".local" / "state" / "porter-ticket-board" / "pane-sessions"
        worker_a = _hermes_role("worker-a", home=owner_home)
        worker_b = _hermes_role("worker-b", home=owner_home)
        worker_a_home = team_launcher.prepare_hermes_home_for_role(worker_a, session_dir=session_dir)
        worker_b_home = team_launcher.prepare_hermes_home_for_role(worker_b, session_dir=session_dir)
        assert worker_a_home is not None
        assert worker_b_home is not None

        probe = subprocess.run(
            [
                str(hermes_python),
                "-",
                str(shared_home),
                str(worker_a_home),
                str(worker_b_home),
            ],
            input=r'''
import json
import sys
from pathlib import Path

from hermes_state import SessionDB
from tools.session_search_tool import session_search

shared_home = Path(sys.argv[1])
worker_a_home = Path(sys.argv[2])
worker_b_home = Path(sys.argv[3])
sentinel = "pgu839-session-search-worker-a-only"

shared_db = SessionDB(db_path=shared_home / "state.db")
shared_db.create_session("worker-a-shared-session", "cli")
shared_db.append_message("worker-a-shared-session", "user", f"secret {sentinel}")
shared_result = json.loads(session_search(query=sentinel, limit=3, db=shared_db))

worker_a_db = SessionDB(db_path=worker_a_home / "state.db")
worker_a_db.create_session("worker-a-isolated-session", "cli")
worker_a_db.append_message("worker-a-isolated-session", "user", f"secret {sentinel}")
worker_b_db = SessionDB(db_path=worker_b_home / "state.db")
worker_b_db.create_session("worker-b-isolated-session", "cli")
isolated_result = json.loads(session_search(query=sentinel, limit=3, db=worker_b_db))

print(json.dumps({"shared": shared_result, "isolated": isolated_result}, sort_keys=True))
''',
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert probe.returncode == 0, probe.stderr
        result = json.loads(probe.stdout)
        assert "pgu839-session-search-worker-a-only" not in (worker_b_home / "state.db").read_text(errors="ignore")

    assert result["shared"]["success"] is True
    assert result["shared"]["count"] >= 1
    assert result["isolated"]["success"] is True
    assert result["isolated"]["count"] == 0


def test_hermes_start_prepares_isolated_home_and_preserves_fresh_session_behavior() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-hermes-start.") as tmp:
        owner_home = Path(tmp) / "home" / "porter-agent"
        shared_home = owner_home / ".hermes"
        shared_home.mkdir(parents=True)
        (shared_home / "config.yaml").write_text("hooks: {}\n", encoding="utf-8")
        session_dir = owner_home / ".local" / "state" / "porter-ticket-board" / "pane-sessions"
        session_dir.mkdir(parents=True)
        role = _hermes_role("bulk", home=owner_home)
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": "previous-hermes-session"}) + "\n",
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={role.tmux_session}, current_commands={role.target: "hermes"})
        stderr = StringIO()

        with redirect_stderr(stderr):
            assert (
                run_role_pane(
                    role,
                    mode="reload",
                    session_dir=session_dir,
                    pane_state_dir=owner_home / ".local" / "state" / "porter-ticket-board" / "pane-state",
                    runner=runner,
                )
                == 0
            )

        new_session = next(call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "porter-bulk"])
        hermes_home = team_launcher.hermes_home_for_role(role, session_dir=session_dir)
        assert hermes_home.is_dir()
        assert (hermes_home / "config.yaml").resolve(strict=True) == shared_home / "config.yaml"
        assert f"HERMES_HOME={hermes_home}" in new_session[-1]
        assert "previous-hermes-session" not in new_session[-1]
        assert "--resume" not in new_session[-1]
        assert "uses hermes, which has no dependable reset" in stderr.getvalue()


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_hermes_session_isolation_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
