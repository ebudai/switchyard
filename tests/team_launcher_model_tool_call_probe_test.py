#!/usr/bin/env python3
"""SYRD-111: the first-run model probe has to prove a tool call, not prose.

Every Switchyard role is a tool-calling agent. The probe asked each role's model
for a one-shot completion and required `model-ok` in stdout, which answers "can
this model talk" rather than "can this model work". A model that emits tool
calls as text, or not at all, passed and then silently did nothing useful: it
reports edits it did not make, the pane stops changing, and the idle path reads
that as a worker who needs a nudge rather than a runtime that cannot work.

The probe now leaves a file holding a token that exists nowhere else -- not in
the prompt, not in the model's weights -- and asks for it back. A reply carrying
that token cannot have been written without reading the file, so echoing it is
the proof. `model-ok` stays, because an exit 0 carrying no usable content is
still a failed completion.

`test_the_built_probe_makes_a_real_cli_call_a_tool` runs the argv the launcher
builds through an installed CLI and reads the answer, because every other case
here proves only that the launcher and a fake agree with each other.
"""

from __future__ import annotations

import stat

from team_launcher_test_helpers import *


def _role(role: str, cli: str, model: str, *, model_arg: str = "--model") -> "team_launcher.RoleConfig":
    return team_launcher.RoleConfig(
        role=role, slot=0, detached=False, tmux_session=f"otto-{role}",
        target=f"otto-{role}:0.0", workdir="/srv/otto", cli=[cli], model=model,
        model_arg=model_arg, effort="", yolo=False, extra_args=[],
        resume_mode="flag", resume_flag="--resume", resume_subcommand="resume",
        fresh_session_per_ticket=False, live_commands=[cli], env={},
    )


def _config(tmp: Path, roles: list[tuple[str, str]], models: dict[str, str]) -> object:
    return load_project_config(
        "otto",
        _write_first_run_auth_config(tmp, roles=roles, role_models=models),
    )


def _validate(config, runner, owner_home: Path) -> list[object]:
    # A tenant that has been through its providers' first runs, which is the
    # only one whose models are probed at all: an unfinished first run is what
    # the CLI shows a probe instead of answering it, so the phase leaves those
    # roles alone rather than reporting their model as broken (SYRD-221).
    _mark_first_run_setup_complete(owner_home, config)
    return team_launcher.run_first_run_auth_phase(
        config,
        owner_user="otto-agent",
        owner_home=owner_home,
        validate_models=True,
        runner=runner,
    ).model_validation_failures


def test_the_prompt_asks_for_something_only_a_tool_can_fetch() -> None:
    """The token is never in the prompt, or answering it would prove nothing."""
    assert team_launcher.MODEL_PROBE_FILENAME in team_launcher.MODEL_VALIDATION_PROMPT
    assert "model-ok" in team_launcher.MODEL_VALIDATION_PROMPT

    workspace = team_launcher._ModelProbeWorkspace()
    try:
        probe_file = workspace.root / team_launcher.MODEL_PROBE_FILENAME
        assert probe_file.read_text(encoding="utf-8").strip() == workspace.token
        assert workspace.token not in team_launcher.MODEL_VALIDATION_PROMPT
        assert len(workspace.token) >= 16, workspace.token
        # Readable by the owner user, who is not necessarily this process.
        assert stat.S_IMODE(workspace.root.stat().st_mode) == 0o755
        assert stat.S_IMODE(probe_file.stat().st_mode) == 0o644
        root = workspace.root
    finally:
        workspace.remove()
    assert not root.exists()


def test_two_workspaces_never_share_a_token() -> None:
    """A token reused across runs is a token a model could have memorised."""
    tokens = set()
    for _ in range(5):
        workspace = team_launcher._ModelProbeWorkspace()
        tokens.add(workspace.token)
        workspace.remove()
    assert len(tokens) == 5, tokens


def test_each_cli_is_asked_with_the_flags_its_panes_use() -> None:
    """A probe has nobody to approve a tool call, so it carries the pane's flags.

    Taken from the launcher's own yolo table rather than restated here, because
    a probe that asks with different flags than a pane is answering a different
    question.
    """
    workspace = Path("/tmp/probe-workspace")
    for cli, model_arg, expected_tail in (
        ("claude", "--model", ["-p", team_launcher.MODEL_VALIDATION_PROMPT]),
        ("agy", "--model", ["-p", team_launcher.MODEL_VALIDATION_PROMPT]),
        ("hermes", "-m", ["-z", team_launcher.MODEL_VALIDATION_PROMPT]),
    ):
        command = team_launcher._model_validation_command(_role("r", cli, "m", model_arg=model_arg), workspace)
        assert command is not None
        expected_flags = team_launcher.YOLO_ARGS_BY_CLI[cli]
        assert command == [cli, model_arg, "m", *expected_flags, *expected_tail], command

    codex = team_launcher._model_validation_command(_role("r", "codex", "m"), workspace)
    assert codex == [
        "codex", "exec", "--skip-git-repo-check", "--model", "m",
        *team_launcher.YOLO_ARGS_BY_CLI["codex"],
        "-C", str(workspace), team_launcher.MODEL_VALIDATION_PROMPT,
    ], codex


def test_a_model_that_answers_without_reading_the_file_fails() -> None:
    """The gap the ticket names: prose that looks like success."""
    with tempfile.TemporaryDirectory(prefix="pgu-tool-call-probe.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = _config(
            tmp_path,
            [("director", "claude"), ("ops", "codex")],
            {"director": "talks-only", "ops": "gpt-5.5"},
        )
        runner = FirstRunAuthRunner(tool_blind_models={("claude", "talks-only")})
        runner.login_seen.update({"claude", "codex"})

        failures = _validate(config, runner, owner_home)

    assert [(failure.role, failure.cli, failure.model) for failure in failures] == [
        ("director", "claude", "talks-only")
    ], failures
    failure = failures[0]
    assert failure.reason == team_launcher.MODEL_PROBE_NO_TOOL_CALL_REASON, failure
    assert team_launcher.MODEL_PROBE_FILENAME in failure.reason, failure
    # Says what to change, and names the model that could not do it.
    assert "talks-only" in failure.suggestion, failure.suggestion
    assert "tool calling" in failure.suggestion, failure.suggestion
    # The role that can call a tool is not reported at all.
    assert all(item.role != "ops" for item in failures), failures


def test_the_operator_message_names_the_role_and_the_model() -> None:
    """Acceptance reads a warning line, not a dataclass."""
    with tempfile.TemporaryDirectory(prefix="pgu-tool-call-message.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = _config(tmp_path, [("director", "claude")], {"director": "talks-only"})
        runner = FirstRunAuthRunner(tool_blind_models={("claude", "talks-only")})
        runner.login_seen.add("claude")
        _mark_first_run_setup_complete(owner_home, config)
        messages: list[str] = []
        report = team_launcher.run_first_run_auth_phase(
            config, owner_user="otto-agent", owner_home=owner_home,
            validate_models=True, runner=runner, print_func=messages.append,
        )
        team_launcher.report_first_run_auth_warnings(report, print_func=messages.append)

    warning = next(line for line in messages if "model validation failed" in line)
    assert "role director" in warning, warning
    assert "model talks-only" in warning, warning
    assert "completed no tool call" in warning, warning


def test_the_three_failures_do_not_read_alike() -> None:
    """Acceptance asks for these to be distinguishable, so they are compared.

    An unauthenticated CLI never reaches model validation at all -- the auth
    phase skips its roles -- and an unknown model fails non-zero with the
    vendor's own text. A model that answered but did not act is the third, and
    it is the one that used to pass.
    """
    with tempfile.TemporaryDirectory(prefix="pgu-tool-call-distinct.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = _config(
            tmp_path,
            [("director", "claude"), ("inspector", "agy"), ("ops", "codex")],
            {"director": "talks-only", "inspector": "gemini-retired", "ops": "gpt-5.5"},
        )
        runner = FirstRunAuthRunner(
            tool_blind_models={("claude", "talks-only")},
            invalid_models={("agy", "gemini-retired"): "model gemini-retired is unavailable\n"},
            unauthenticated_clis={"codex"},
        )
        runner.login_seen.update({"claude", "agy"})
        _mark_first_run_setup_complete(owner_home, config)
        report = team_launcher.run_first_run_auth_phase(
            config, owner_user="otto-agent", owner_home=owner_home,
            validate_models=True, runner=runner,
        )

    reasons = {failure.role: failure.reason for failure in report.model_validation_failures}
    assert set(reasons) == {"director", "inspector"}, reasons
    assert reasons["director"] == team_launcher.MODEL_PROBE_NO_TOOL_CALL_REASON
    assert "gemini-retired is unavailable" in reasons["inspector"], reasons
    assert reasons["director"] != reasons["inspector"]
    # The unauthenticated CLI is reported as unauthenticated and never probed
    # for a model at all, which is the third outcome kept apart from these two.
    assert report.unauthenticated_roles == {"codex": ["ops"]}, report.unauthenticated_roles
    assert "ops" not in reasons, reasons


def test_an_empty_success_is_still_a_failed_completion() -> None:
    """The check that was already right stays right, and keeps its own reason."""
    with tempfile.TemporaryDirectory(prefix="pgu-tool-call-empty.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = _config(tmp_path, [("bulk", "hermes")], {"bulk": "openrouter/missing"})
        runner = FirstRunAuthRunner(empty_success_models={("hermes", "openrouter/missing")})
        runner.login_seen.add("hermes")

        failures = _validate(config, runner, owner_home)

    assert len(failures) == 1, failures
    assert failures[0].reason == "model probe did not confirm model-ok", failures[0]
    assert failures[0].reason != team_launcher.MODEL_PROBE_NO_TOOL_CALL_REASON


def test_the_sentinel_cannot_be_satisfied_from_stderr() -> None:
    """Kept from the original probe: the prompt itself contains the sentinel."""
    echoed = subprocess.CompletedProcess(
        ["codex"], 0, stdout="", stderr=f"model not found\n{team_launcher.MODEL_VALIDATION_PROMPT}\n"
    )
    assert team_launcher._model_validation_passed(echoed) is False
    assert team_launcher._model_probe_called_a_tool(
        subprocess.CompletedProcess(["codex"], 0, stdout="", stderr="deadbeefdeadbeef"), "deadbeefdeadbeef"
    ) is False
    assert team_launcher._model_probe_called_a_tool(
        subprocess.CompletedProcess(["codex"], 0, stdout="model-ok deadbeefdeadbeef"), "deadbeefdeadbeef"
    ) is True


def test_the_built_probe_makes_a_real_cli_call_a_tool() -> None:
    """The argv the launcher builds, through an installed CLI, for real.

    Everything above shows the launcher and a fake agreeing with each other.
    This is the case that fails if the prompt stops making a real runtime read
    the file -- a permission flag that no longer grants the read, a CLI that
    needs the path spelled differently, a prompt a model answers from memory.

    Scoped to claude because it is one model call per suite run and one is
    enough to answer the question; skipped when the CLI is absent or not
    authenticated, which is the shape the rest of this repository's
    real-runtime cases use.
    """
    cli = shutil.which("claude")
    if not cli:
        return
    status = subprocess.run(
        [cli, "auth", "status", "--json"], capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=120, check=False,
    )
    try:
        authenticated = bool(json.loads(status.stdout or "{}").get("loggedIn"))
    except json.JSONDecodeError:
        authenticated = False
    if status.returncode != 0 or not authenticated:
        return

    workspace = team_launcher._ModelProbeWorkspace()
    try:
        role = _role("director", "claude", "")
        # No model on the role, so the probe carries the account's default and
        # the case is about the prompt and the flags rather than one model id.
        command = team_launcher._model_validation_command(
            team_launcher.replace(role, model="claude-opus-5"), workspace.root
        )
        assert command is not None
        proc = subprocess.run(
            command, cwd=str(workspace.root), capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=600, check=False,
        )
        assert team_launcher._model_validation_passed(proc), (proc.returncode, proc.stdout, proc.stderr)
        assert team_launcher._model_probe_called_a_tool(proc, workspace.token), (
            workspace.token, proc.stdout, proc.stderr,
        )
    finally:
        workspace.remove()


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_model_tool_call_probe_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
