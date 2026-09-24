#!/usr/bin/env python3
"""Fresh provisioning says where its time goes, and stops waiting for nothing.

Measured on a disposable 2 vCPU / 4 GB Arch VM, `switchyard new` at faafe96
spent about 27 seconds of its own after the last question, and the operator
had no way to tell which part (SYRD-248). Two of those waits could never end
early:

- the session-record check always ran its whole 10 seconds for Codex panes,
  because interactive Codex defers its hook until the first prompt -- the
  report already treated that as expected and said nothing, but the wait
  still spent the full timeout finding it out;
- a presentation whose panes had all failed to start waited the whole 90
  seconds for runtimes that could not register.

These cases hold the waits to what they are for, and the stage report to
what it says.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from team_launcher_test_helpers import *  # noqa: F401,F403
from scripts import presentation_controller, team_launcher
from scripts.team_launcher import load_project_config

import runtime_registration_wait_test as registration  # noqa: E402


def _tenant(tmp: Path, roles: list[dict]):
    layout = tmp / "layout.json"
    layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
    (tmp / "repo").mkdir()
    (tmp / "sessions").mkdir()
    pane_state_dir = tmp / "pane-state"
    pane_state_dir.mkdir()
    config_path = tmp / "porter.json"
    config_path.write_text(json.dumps({
        "desktop_access": {"mode": "headless"},
        "project": "porter",
        "layout": str(layout),
        "repository": str(tmp / "repo"),
        "session_dir": str(tmp / "sessions"),
        "roles": roles,
    }) + "\n", encoding="utf-8")
    return load_project_config("porter", config_path), pane_state_dir


def _started_fresh(config, pane_state_dir: Path) -> float:
    """Every pane as the launcher leaves a fresh start: its outcome, no record."""
    since = time.time()
    for role in config.roles:
        team_launcher.seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source="team_launcher.start")
    return since


CODEX_ROLES = [
    {"role": "main", "slot": 0, "cli": ["codex"], "target": "porter-main:0.0"},
    {"role": "app", "slot": 1, "cli": ["codex"], "target": "porter-app:0.0"},
    {"role": "ops", "slot": 2, "cli": ["codex"], "target": "porter-ops:0.0"},
]


def test_fresh_codex_panes_do_not_hold_the_launch_for_the_whole_timeout() -> None:
    """The measured 10 seconds: every pane accounted for, so only the grace remains."""
    with tempfile.TemporaryDirectory(prefix="syrd248-codex.") as tmp:
        config, pane_state_dir = _tenant(Path(tmp), CODEX_ROLES)
        since = _started_fresh(config, pane_state_dir)
        started = time.monotonic()
        statuses = team_launcher.launch_session_record_statuses(
            config,
            timeout_seconds=10.0,
            poll_seconds=0.05,
            fallback_grace_seconds=0.5,
            pane_state_dir=pane_state_dir,
            pane_state_updated_since=since,
        )
        waited = time.monotonic() - started
    assert waited < 3.0, f"still waited {waited:.1f}s for records that cannot come"
    # The grace is still honoured: a hook that is merely slow gets its chance.
    assert waited >= 0.45, f"returned before the grace: {waited:.2f}s"
    assert all(status.pane_state_source == "team_launcher.start" for status in statuses)
    assert all(not status.found for status in statuses)


def test_the_report_still_says_what_it_said_after_the_shorter_wait() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd248-report.") as tmp:
        config, pane_state_dir = _tenant(Path(tmp), CODEX_ROLES)
        since = _started_fresh(config, pane_state_dir)
        messages: list[str] = []
        started = time.monotonic()
        team_launcher.report_launch_session_records(
            config,
            timeout_seconds=10.0,
            poll_seconds=0.05,
            pane_state_dir=pane_state_dir,
            pane_state_updated_since=since,
            print_func=messages.append,
        )
        waited = time.monotonic() - started
    assert waited < 5.0, waited
    warnings = [m for m in messages if m.startswith("warning: switchyard:")]
    assert len(warnings) == 1 and "codex runtime hook did not report for 3 pane(s)" in warnings[0], messages
    assert not any("session record missing" in m for m in messages), messages


def test_a_pane_that_can_record_is_still_waited_for_until_its_timeout() -> None:
    """Only the case that cannot answer is cut short; everything else is unchanged."""
    roles = [
        {"role": "main", "slot": 0, "cli": ["codex"], "target": "porter-main:0.0"},
        {"role": "director", "slot": 1, "cli": ["claude"], "target": "porter-director:0.0"},
    ]
    with tempfile.TemporaryDirectory(prefix="syrd248-claude.") as tmp:
        config, pane_state_dir = _tenant(Path(tmp), roles)
        since = _started_fresh(config, pane_state_dir)
        started = time.monotonic()
        statuses = team_launcher.launch_session_record_statuses(
            config,
            timeout_seconds=1.5,
            poll_seconds=0.05,
            fallback_grace_seconds=0.2,
            pane_state_dir=pane_state_dir,
            pane_state_updated_since=since,
        )
        waited = time.monotonic() - started
    assert waited >= 1.45, f"a Claude pane with no record yet was abandoned after {waited:.2f}s"
    assert {status.role for status in statuses} == {"main", "director"}


def test_a_pane_with_no_launch_outcome_is_not_counted_as_settled() -> None:
    """No launcher outcome means the pane has not reported at all: keep waiting."""
    with tempfile.TemporaryDirectory(prefix="syrd248-unreported.") as tmp:
        config, pane_state_dir = _tenant(Path(tmp), CODEX_ROLES)
        since = time.time()  # nothing seeded
        started = time.monotonic()
        team_launcher.launch_session_record_statuses(
            config,
            timeout_seconds=1.0,
            poll_seconds=0.05,
            fallback_grace_seconds=0.1,
            pane_state_dir=pane_state_dir,
            pane_state_updated_since=since,
        )
        waited = time.monotonic() - started
    assert waited >= 0.95, waited


def test_panes_that_never_started_are_not_waited_for() -> None:
    """The measured 90 seconds: nothing that failed to start can register."""
    project = replace(registration.config(), role_state_isolation=True)
    clock = registration.Clock()
    try:
        presentation_controller.runtime_assignment_config(
            project,
            opener=registration.opener_for(set()),
            wait_seconds=90.0,
            poll_seconds=2.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            unstarted=tuple(registration.ROLE_NAMES),
        )
    except SystemExit as exc:
        said = str(exc)
        assert "no live runtime assignment for configured role(s)" in said, said
        assert "their panes did not start" in said, said
        assert clock.slept == [], clock.slept
        return
    raise AssertionError("roles that never started were resolved")


def test_a_started_role_is_still_waited_for_beside_one_that_failed() -> None:
    project = replace(registration.config(), role_state_isolation=True)
    clock = registration.Clock()
    names = set(registration.ROLE_NAMES)
    failed, late = sorted(names)[0], sorted(names)[1]
    try:
        presentation_controller.runtime_assignment_config(
            project,
            opener=registration.opener_for(names - {failed, late}, names - {failed}),
            wait_seconds=90.0,
            poll_seconds=2.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            unstarted=(failed,),
        )
    except SystemExit as exc:
        # The late one arrived; the failed one is named, promptly after that.
        assert failed in str(exc) and "did not start" in str(exc), exc
        assert clock.slept == [2.0], clock.slept
        return
    raise AssertionError("a role whose pane failed was reported as assigned")


def test_the_stage_report_names_each_stage_and_what_it_cost() -> None:
    ticks = iter([100.0, 100.5, 108.0, 108.0, 120.0, 121.0])
    said: list[str] = []
    stages = team_launcher.ProvisioningStages(
        team_launcher.NEW_PROJECT_STAGES, print_func=said.append, monotonic=lambda: next(ticks)
    )
    stages.begin("host and agent CLI checks")
    stages.begin("project accounts and files")
    stages.begin("database and board")
    stages.begin("provider sign-in and folder trust", waits_for_you=True)
    stages.begin("role panes")
    stages.finish()
    assert said[0] == "switchyard: [1/5] host and agent CLI checks (0.0s in)", said
    assert said[2] == "switchyard: [3/5] database and board (8.0s in)", said
    assert said[3].startswith("switchyard: [4/5] provider sign-in and folder trust -- this step waits for you"), said
    assert said[-1] == (
        "switchyard: provisioned in 21.0s: host and agent CLI checks 0.5s, project accounts and files 7.5s, "
        "database and board 0.0s, provider sign-in and folder trust 12.0s, role panes 1.0s"
    ), said[-1]


def test_switchyard_new_reports_every_stage_in_order() -> None:
    """The real command's source: each stage is begun, in order, and finished on success."""
    import inspect

    body = inspect.getsource(team_launcher.switchyard_new_command)
    positions = [body.index(f'stages.begin("{name}"') for name in team_launcher.NEW_PROJECT_STAGES]
    assert positions == sorted(positions), positions
    assert body.index("stages.begin(\"database and board\")") < body.index("result = new_project_command(")
    assert body.index("stages.begin(\"provider sign-in and folder trust\", waits_for_you=True)") < body.index(
        "run_first_run_auth_phase("
    )
    assert body.rstrip().endswith("stages.finish()\n    return 0") or "stages.finish()\n    return 0" in body


def main() -> int:
    from standalone_test_runner import run_module_tests

    run_module_tests(globals())
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"provisioning_stage_timing_test: {count} tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
