#!/usr/bin/env python3
"""SYRD-91: Switchyard's Codex panes must start with a readable scrollback.

Codex draws its transcript through the TUI's own renderer by default. The wheel
in that pane cycles input history instead of scrolling the terminal, and a
response longer than the pane never reaches the terminal's scrollback at all --
measured on codex-cli 0.153.4 by asking each of two real panes for sixty
numbered lines and reading `tmux capture-pane -S -200` back: without the setting
the scrollback held none of them, with it, all sixty. Claude and Gemini panes do
not do this and must not be touched by the repair.

The setting is applied as a launch-time `-c` override rather than written into
the owner's `~/.codex/config.toml`, because that file is also read by every
codex the human starts for themselves, and the ticket asks for the
Switchyard-managed runtimes and only those.
"""

from __future__ import annotations

from team_launcher_test_helpers import *

RAW_OUTPUT_KEY = "tui.raw_output_mode"
RAW_OUTPUT_SETTING = f"{RAW_OUTPUT_KEY}=true"


def _role_payload(role: str, cli: list[str], *, slot: int = 0, workdir: str = "", **extra) -> dict:
    payload = {"role": role, "slot": slot, "cli": cli, "target": f"porter-{role}:0.0", **extra}
    if workdir:
        payload["workdir"] = workdir
    return payload


def _project(tmp: Path, roles: list[dict]) -> "team_launcher.ProjectConfig":
    layout_path = tmp / "layout.json"
    layout_path.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
    config_path = tmp / "porter.json"
    config_path.write_text(
        json.dumps(
            {
                "desktop_access": {"mode": "headless"},
                "project": "porter",
                "layout": str(layout_path),
                "roles": roles,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return load_project_config("porter", config_path)


def _command_for(config, role_name: str, *, session_dir: Path, resume: bool = False) -> list[str]:
    role = next(role for role in config.roles if role.role == role_name)
    return cli_command_for_role(role, session_dir=session_dir, resume=resume)


def _setting_positions(command: list[str], setting_prefix: str) -> list[int]:
    """Where a `-c <key>=...` override sits, by the index of its value."""
    return [
        index
        for index, token in enumerate(command)
        if token.startswith(setting_prefix) and index and command[index - 1] == "-c"
    ]


def test_a_codex_role_launches_in_raw_output_mode() -> None:
    """The reported failure: a Codex pane whose earlier output cannot be scrolled to.

    The stored project declares nothing about it. That is the point: the pane
    gets the setting from the launcher at start, so an already-provisioned
    tenant has it on its next launch with nothing regenerated and nothing for
    the operator to toggle.
    """
    with tempfile.TemporaryDirectory(prefix="codex-raw.") as tmp:
        tmp_path = Path(tmp)
        config = _project(tmp_path, [_role_payload("ops", ["codex"])])
        assert RAW_OUTPUT_KEY not in (tmp_path / "porter.json").read_text(encoding="utf-8")

        command = _command_for(config, "ops", session_dir=tmp_path / "sessions")

        assert _setting_positions(command, RAW_OUTPUT_KEY) == [command.index(RAW_OUTPUT_SETTING)], command
        assert command[command.index(RAW_OUTPUT_SETTING) - 1] == "-c", command


def test_claude_and_gemini_roles_are_left_alone() -> None:
    """The scope line in the ticket: only the runtime with the problem changes."""
    with tempfile.TemporaryDirectory(prefix="codex-raw-scope.") as tmp:
        tmp_path = Path(tmp)
        config = _project(
            tmp_path,
            [
                _role_payload("director", ["claude"], slot=0, workdir=str(tmp_path / "director")),
                _role_payload("inspector", ["agy"], slot=1, workdir=str(tmp_path / "inspector")),
                _role_payload("main", ["hermes"], slot=2, workdir=str(tmp_path / "main")),
                _role_payload("ops", ["codex"], slot=3, workdir=str(tmp_path / "ops")),
            ],
        )
        session_dir = tmp_path / "sessions"

        for role_name in ("director", "inspector", "main"):
            command = _command_for(config, role_name, session_dir=session_dir)
            assert RAW_OUTPUT_KEY not in " ".join(command), (role_name, command)
        assert RAW_OUTPUT_SETTING in _command_for(config, "ops", session_dir=session_dir)


def test_the_setting_does_not_disturb_the_rest_of_the_codex_command() -> None:
    """Model, effort, yolo, startup flags and the role's own arguments all survive."""
    with tempfile.TemporaryDirectory(prefix="codex-raw-intact.") as tmp:
        tmp_path = Path(tmp)
        config = _project(
            tmp_path,
            [
                _role_payload(
                    "ops",
                    ["codex"],
                    model="gpt-5.5",
                    effort="high",
                    yolo=True,
                    extra_args=["--search"],
                )
            ],
        )
        command = _command_for(config, "ops", session_dir=tmp_path / "sessions")
        tail = _command_tail(command)

        assert tail == [
            "codex",
            "--model",
            "gpt-5.5",
            "-c",
            "reasoning_effort=high",
            "-c",
            RAW_OUTPUT_SETTING,
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "--search",
        ], tail
        # The effort override keeps its own `-c`; the two are separate settings
        # and neither replaces the other.
        assert _setting_positions(command, "reasoning_effort"), command


def test_an_operator_who_states_the_setting_themselves_still_decides_it() -> None:
    """`extra_args` come last and codex resolves repeated `-c` last-wins."""
    with tempfile.TemporaryDirectory(prefix="codex-raw-override.") as tmp:
        tmp_path = Path(tmp)
        config = _project(
            tmp_path,
            [_role_payload("ops", ["codex"], extra_args=["-c", f"{RAW_OUTPUT_KEY}=false"])],
        )
        command = _command_for(config, "ops", session_dir=tmp_path / "sessions")

        positions = _setting_positions(command, RAW_OUTPUT_KEY)
        assert len(positions) == 2, command
        assert command[positions[-1]] == f"{RAW_OUTPUT_KEY}=false", command


def test_the_setting_survives_a_resumed_session() -> None:
    """A restart resumes through a subcommand, and must not lose the setting."""
    with tempfile.TemporaryDirectory(prefix="codex-raw-resume.") as tmp:
        tmp_path = Path(tmp)
        config = _project(tmp_path, [_role_payload("ops", ["codex"])])
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        role = next(role for role in config.roles if role.role == "ops")
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": "ops-session"}) + "\n", encoding="utf-8"
        )
        rollout = session_dir.parent / ".codex" / "sessions"
        rollout.mkdir(parents=True)
        (rollout / "rollout-2026-09-09T00-00-00-ops-session.jsonl").write_text("{}\n", encoding="utf-8")

        command = _command_for(config, "ops", session_dir=session_dir, resume=True)
        tail = _command_tail(command)

        assert tail[:3] == ["codex", "resume", "ops-session"], tail
        assert RAW_OUTPUT_SETTING in tail, tail


def test_the_launcher_names_a_key_codex_actually_reads() -> None:
    """A key codex does not know is a setting that silently does nothing.

    The key is taken from the launcher's own table, not from this file, and put
    to the installed binary: `-c` parses the value as TOML into the same config
    that `~/.codex/config.toml` feeds, so an unknown key is accepted in silence
    while a known one of the wrong type is rejected by name. That asymmetry is
    what makes this a check rather than a restatement.
    """
    codex = shutil.which("codex")
    if not codex:
        return
    args = team_launcher.RAW_OUTPUT_ARGS_BY_CLI["codex"]
    assert args[0] == "-c" and len(args) == 2, args
    key, _, value = args[1].partition("=")

    def probe(setting: str) -> str:
        result = subprocess.run(
            [codex, "-c", setting, "exec", "--skip-git-repo-check"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=180,
        )
        return f"{result.stdout}\n{result.stderr}"

    # Codex knows this key and it is a boolean, so the wrong type is named back.
    rejected = probe(f"{key}=12345")
    assert key in rejected and "expected a boolean" in rejected, rejected
    # And the value the launcher actually sends is one it accepts.
    accepted = probe(f"{key}={value}")
    assert "expected a boolean" not in accepted, accepted


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_codex_raw_output_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
