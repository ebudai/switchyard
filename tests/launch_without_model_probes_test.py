#!/usr/bin/env python3
"""SYRD-246: an ordinary launch asks no model to prove anything.

Live test17, on the published SYRD-245 build. Claude's first run and every
folder trust were complete. Switchyard then checked designer, director and
audit (claude-opus-5), then main and ops (gpt-5.6-sol, Codex 0.152.1) -- and
returned to the shell with no panes. Main's Codex probe exited 0 without
printing `model-ok`; ops exited 0 and twice answered without reading
`switchyard-model-probe.txt`.

Every model on that team can call tools. What failed was the probe, not the
model: a single non-deterministic request per role, retried once, up to 180
seconds an attempt, on the critical path of a first launch -- and a refusal to
launch at all when a capable model happened to answer in prose.

SYRD-244 gave that probe better evidence and SYRD-245 bounded its wait. Neither
is a reason to keep it in front of a launch. So it is gone from the normal path,
and these cases are what says so: an ordinary `switchyard new` makes no live
model request at all, a model that answers without the token no longer stops
anything, and the run says plainly that nothing checked the models rather than
letting silence imply it did.

What is deliberately NOT removed: the cheap certain checks. A CLI the owner
cannot run, or an account that is not signed in, still stops a launch before it
opens panes onto something unusable.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher  # noqa: E402
from team_launcher_test_helpers import (  # noqa: E402
    FirstRunAuthRunner,
    _mark_first_run_setup_complete,
    _write_first_run_auth_config,
    load_project_config,
)

CHECKS = 0

#: test17's team: three Claude roles and two Codex ones, every model configured.
TEST17_ROLES = [
    ("designer", "claude"),
    ("director", "claude"),
    ("audit", "claude"),
    ("main", "codex"),
    ("ops", "codex"),
]
TEST17_MODELS = {
    "designer": "claude-opus-5",
    "director": "claude-opus-5",
    "audit": "claude-opus-5",
    "main": "gpt-5.6-sol",
    "ops": "gpt-5.6-sol",
}


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


def probe_calls(runner: FirstRunAuthRunner) -> list[list[str]]:
    """Every call that asked a model to do something, however it was spelled.

    Matched on the prompt rather than on a flag, because the flag differs per
    runtime -- `-p` for Claude and agy, `exec` for Codex, `-z` for Hermes -- and
    the thing this ticket removes is the REQUEST, not one spelling of it.
    """
    return [
        call for call in runner.calls
        if team_launcher.MODEL_VALIDATION_PROMPT in call
    ]


def test17_tenant(tmp_path: Path):
    config_path = _write_first_run_auth_config(
        tmp_path, roles=TEST17_ROLES, role_models=TEST17_MODELS
    )
    return load_project_config("otto", config_path)


def test_an_ordinary_first_run_phase_makes_no_live_model_request() -> None:
    """The removal itself, at the phase every launch goes through."""
    with tempfile.TemporaryDirectory(prefix="syrd246-phase.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = test17_tenant(tmp_path)
        _mark_first_run_setup_complete(owner_home, config)
        runner = FirstRunAuthRunner(authenticated_after_login=True)
        runner.login_seen.update({"claude", "codex"})
        said: list[str] = []
        report = team_launcher.run_first_run_auth_phase(
            config, owner_user="otto-agent", owner_home=owner_home,
            runner=runner, print_func=said.append,
        )

    check(probe_calls(runner) == [],
          f"no model was asked anything: {probe_calls(runner)}")
    check(report.model_validation_failures == [],
          f"and nothing is reported as a model failure: {report.model_validation_failures}")
    check(not any("checking" in line and "model" in line for line in said),
          f"and nothing claims to be checking one: {said}")


def test_a_model_that_answers_without_the_token_no_longer_stops_a_launch() -> None:
    """The test17 failure, made impossible.

    A model that replies in prose instead of reading the file used to produce a
    `ModelValidationFailure`, and `switchyard new` returned 1 on it. Here the
    same tool-blind model is configured and the phase reports nothing to stop
    for.
    """
    with tempfile.TemporaryDirectory(prefix="syrd246-toolblind.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = test17_tenant(tmp_path)
        _mark_first_run_setup_complete(owner_home, config)
        # Exactly ops' behaviour on test17: exits 0, answers, never reads the file.
        runner = FirstRunAuthRunner(
            authenticated_after_login=True,
            tool_blind_models={("codex", "gpt-5.6-sol"), ("claude", "claude-opus-5")},
        )
        runner.login_seen.update({"claude", "codex"})
        report = team_launcher.run_first_run_auth_phase(
            config, owner_user="otto-agent", owner_home=owner_home,
            runner=runner, print_func=lambda _line: None,
        )

    check(report.model_validation_failures == [],
          f"a tool-blind answer is not a launch-stopping failure: "
          f"{report.model_validation_failures}")
    check(probe_calls(runner) == [],
          "because nothing asked it in the first place")


def test_the_run_says_the_models_were_not_checked() -> None:
    """"Do not silently claim model capability was verified when no probe ran"."""
    with tempfile.TemporaryDirectory(prefix="syrd246-notice.") as tmp:
        tmp_path = Path(tmp)
        config = test17_tenant(Path(tmp))
        said: list[str] = []
        team_launcher.report_models_were_not_probed(config, print_func=said.append)

    check(len(said) == 1, f"said once, not once per role: {said}")
    notice = said[0]
    check("NOT checked" in notice, f"and says so plainly: {notice}")
    check("switchyard validate-models otto" in notice,
          f"naming the command that does ask: {notice}")
    check("own pane" in notice,
          f"and where a real rejection will show up: {notice}")


def test_a_tenant_with_no_configured_model_is_told_nothing() -> None:
    """A notice about a diagnostic nobody needs is just noise."""
    with tempfile.TemporaryDirectory(prefix="syrd246-nomodel.") as tmp:
        tmp_path = Path(tmp)
        config = load_project_config(
            "otto", _write_first_run_auth_config(tmp_path, roles=[("director", "claude")])
        )
        said: list[str] = []
        team_launcher.report_models_were_not_probed(config, print_func=said.append)

    check(said == [], f"nothing configured, nothing to say: {said}")


def test_the_explicit_diagnostic_still_asks() -> None:
    """"Keep `switchyard validate-models` as an explicit diagnostic if useful"."""
    with tempfile.TemporaryDirectory(prefix="syrd246-explicit.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = test17_tenant(tmp_path)
        _mark_first_run_setup_complete(owner_home, config)
        runner = FirstRunAuthRunner(authenticated_after_login=True)
        runner.login_seen.update({"claude", "codex"})
        team_launcher.run_first_run_auth_phase(
            config, owner_user="otto-agent", owner_home=owner_home,
            validate_models=True, runner=runner, print_func=lambda _line: None,
        )

    asked = probe_calls(runner)
    check(asked, "asked on purpose, the models are still probed")
    check(len(asked) == len(TEST17_ROLES),
          f"one request per configured role: {len(asked)}")


def test_an_unusable_cli_still_stops_the_launch() -> None:
    """"retain cheap executable/authentication checks".

    The gate that remains is the one that costs nothing and is never wrong: a
    CLI the owner cannot run, and an account that is not signed in.
    """
    with tempfile.TemporaryDirectory(prefix="syrd246-missing.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = test17_tenant(tmp_path)
        _mark_first_run_setup_complete(owner_home, config)
        runner = FirstRunAuthRunner(
            authenticated_after_login=True, missing_clis={"codex"}
        )
        runner.login_seen.add("claude")
        report = team_launcher.run_first_run_auth_phase(
            config, owner_user="otto-agent", owner_home=owner_home,
            runner=runner, print_func=lambda _line: None,
        )
        stopped = team_launcher.stop_before_launch_for_missing_owner_clis(
            report, print_func=lambda _line: None
        )

    check(report.missing_cli_roles.get("codex") == ["main", "ops"],
          f"the roles whose CLI is absent are named: {report.missing_cli_roles}")
    check(stopped is True, "and the launch stops before opening a pane onto it")
    check(probe_calls(runner) == [],
          "without having asked any model anything")


def test_an_unauthenticated_provider_still_stops_the_launch() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd246-unauth.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = test17_tenant(tmp_path)
        _mark_first_run_setup_complete(owner_home, config)
        runner = FirstRunAuthRunner(
            authenticated_after_login=False, unauthenticated_clis={"codex"}
        )
        report = team_launcher.run_first_run_auth_phase(
            config, owner_user="otto-agent", owner_home=owner_home,
            runner=runner, print_func=lambda _line: None,
        )
        stopped = team_launcher.stop_before_launch_for_unauthenticated_providers(
            report, print_func=lambda _line: None
        )

    check(report.unauthenticated_roles, f"the unsigned-in provider is reported: {report}")
    check(stopped is True, "and the launch stops rather than opening a pane onto it")


def test_no_normal_launch_path_asks_for_model_validation() -> None:
    """The boundary, held where a caller could quietly put it back.

    `switchyard new` and `switchyard <slug>` both reach the same phase, whose
    default is now not to probe. This reads the shipped call sites rather than
    trusting that default: the only place that may turn it on is the explicit
    diagnostic command.
    """
    source = (ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")
    enabling = [
        line.strip() for line in source.splitlines() if "validate_models=True" in line
    ]
    check(len(enabling) == 1,
          f"exactly one caller asks for model validation: {enabling}")
    command = source[source.index("def switchyard_validate_models_command("):]
    command = command[: command.index("\ndef ", 1)]
    check("validate_models=True" in command,
          "and it is `switchyard validate-models`, the explicit diagnostic")


def test_the_new_command_says_it_before_it_launches() -> None:
    """The notice is on the shipped path, and in the right place on it.

    Read from the source rather than by driving `switchyard new`, deliberately:
    that command resolves the owner's home from the passwd database, so a test
    that ran it far enough to reach this line would be writing into a real
    `/home/<owner>`. The weakness is that this checks position rather than
    behaviour; the behaviour of the line itself is checked above.
    """
    source = (ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")
    body = source[source.index("def switchyard_new_command("):]
    body = body[: body.index("\ndef ", 1)]
    check("report_models_were_not_probed(" in body,
          "the launch says the models were not checked")
    told = body.index("report_models_were_not_probed(")
    gate = body.index("stop_before_launch_for_unauthenticated_providers(")
    launched = body.index("launch_result = 0 if launch_deferred else launch_project(")
    check(gate < told < launched,
          "after the checks that can stop a launch, and before the panes open")
    check("model_validation_failures" not in body,
          "and nothing in this command stops a launch over a model probe")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"launch_without_model_probes_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
