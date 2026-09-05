#!/usr/bin/env python3
"""Hermes session-store isolation regression tests."""

from __future__ import annotations

import inspect

from dataclasses import replace

from team_launcher_test_helpers import *


OBSERVED_HERMES_HOME_ENTRIES = frozenset(
    {
        ".env",
        ".hermes_history",
        ".skills_prompt_snapshot.json",
        ".update_check",
        "SOUL.md",
        "audio_cache",
        "auth.json",
        "auth.lock",
        "bin",
        "cache",
        "config.yaml",
        "cron",
        "hooks",
        "image_cache",
        "logs",
        "memories",
        "models_dev_cache.json",
        "pairing",
        "sandboxes",
        "sessions",
        "shell-hooks-allowlist.json",
        "shell-hooks-allowlist.json.lock",
        "skills",
        "state.db",
    }
)


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
        fresh_session_per_ticket=False,
        live_commands=["hermes"],
        env={},
    )


def _env_value(command: list[str], name: str) -> str:
    prefix = f"{name}="
    matches = [entry[len(prefix):] for entry in _env_entries(command) if entry.startswith(prefix)]
    assert len(matches) == 1, (name, command)
    return matches[0]


def _classified_hermes_home_entries() -> set[str]:
    return set(team_launcher.HERMES_SHARED_HOME_ENTRIES) | set(team_launcher.HERMES_PRIVATE_HOME_ENTRIES)


def test_every_observed_hermes_home_entry_is_classified_shared_or_private() -> None:
    classified = _classified_hermes_home_entries()
    assert not (set(team_launcher.HERMES_SHARED_HOME_ENTRIES) & set(team_launcher.HERMES_PRIVATE_HOME_ENTRIES))
    assert OBSERVED_HERMES_HOME_ENTRIES <= classified
    live_home = Path.home() / ".hermes"
    if live_home.exists():
        live_entries = {path.name for path in live_home.iterdir()}
        assert not (live_entries - classified), sorted(live_entries - classified)


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
        for name in (
            ".env",
            "auth.json",
            "auth.lock",
            "config.yaml",
            "shell-hooks-allowlist.json",
            "shell-hooks-allowlist.json.lock",
            "skills",
        ):
            link = hermes_home / name
            assert link.is_symlink(), name
            assert link.resolve(strict=True) == shared_home / name
        assert (shared_home / "auth.lock").is_file()
        assert (shared_home / "shell-hooks-allowlist.json.lock").is_file()
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

        def run_hermes_home_probe(hermes_home: Path, source: str) -> dict[str, object]:
            env = {
                **os.environ,
                "HOME": str(owner_home),
                "HERMES_HOME": str(hermes_home),
            }
            proc = subprocess.run(
                [str(hermes_python), "-"],
                input=source,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert proc.returncode == 0, proc.stderr
            return json.loads(proc.stdout)

        write_script = r'''
import json

from hermes_state import SessionDB

sentinel = "pgu839-session-search-worker-a-only"
session_id = "worker-a-session"
db = SessionDB()
db.create_session(session_id, "cli")
db.append_message(session_id, "user", f"secret {sentinel}")
print(json.dumps({"db_path": str(db.db_path)}))
'''
        search_script = r'''
import json

from hermes_state import SessionDB
from tools.session_search_tool import session_search

sentinel = "pgu839-session-search-worker-a-only"
db = SessionDB()
result = json.loads(session_search(query=sentinel, limit=3))
print(json.dumps({"db_path": str(db.db_path), "result": result}, sort_keys=True))
'''

        shared_write = run_hermes_home_probe(shared_home, write_script)
        shared_search = run_hermes_home_probe(shared_home, search_script)
        worker_a_write = run_hermes_home_probe(worker_a_home, write_script)
        worker_b_search = run_hermes_home_probe(worker_b_home, search_script)
        assert shared_write["db_path"] == str(shared_home / "state.db")
        assert shared_search["db_path"] == str(shared_home / "state.db")
        assert worker_a_write["db_path"] == str(worker_a_home / "state.db")
        assert worker_b_search["db_path"] == str(worker_b_home / "state.db")
        assert "pgu839-session-search-worker-a-only" not in (worker_b_home / "state.db").read_text(errors="ignore")

    assert shared_search["result"]["success"] is True
    assert shared_search["result"]["count"] >= 1
    assert worker_b_search["result"]["success"] is True
    assert worker_b_search["result"]["count"] == 0


def test_fresh_session_decision_is_not_cli_name_based() -> None:
    source = inspect.getsource(team_launcher._uses_fresh_session_per_ticket)
    assert "hermes" not in source
    assert "fresh_session_per_ticket" in source


def test_hermes_start_prepares_isolated_home_and_resumes_by_default() -> None:
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
        assert "--resume previous-hermes-session" in new_session[-1]
        assert "uses hermes, which has no dependable reset" not in stderr.getvalue()


def test_configured_fresh_session_per_ticket_starts_fresh_for_hermes() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-hermes-fresh-opt-in.") as tmp:
        owner_home = Path(tmp) / "home" / "porter-agent"
        shared_home = owner_home / ".hermes"
        shared_home.mkdir(parents=True)
        (shared_home / "config.yaml").write_text("hooks: {}\n", encoding="utf-8")
        session_dir = owner_home / ".local" / "state" / "porter-ticket-board" / "pane-sessions"
        session_dir.mkdir(parents=True)
        role = replace(_hermes_role("bulk", home=owner_home), fresh_session_per_ticket=True)
        session_path = session_dir / session_file_name(role.target)
        session_path.write_text(
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
        assert not session_path.exists()
        sidecar = json.loads((session_dir / f"{session_file_name(role.target)}.superseded").read_text(encoding="utf-8"))
        assert sidecar["session_id"] == "previous-hermes-session"
        assert "previous-hermes-session" not in new_session[-1]
        assert "--resume" not in new_session[-1]
        assert "configured for fresh sessions per ticket" in stderr.getvalue()


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_hermes_session_isolation_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
