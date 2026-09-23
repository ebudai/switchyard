#!/usr/bin/env python3
"""SYRD-234: the hook that keeps a bypass pane off Claude's permission prompt.

Claude Code 2.1.278 keeps one confirmation that no permission mode skips: a
recursive removal whose target is a critical path. A Switchyard role pane is
launched with `--dangerously-skip-permissions` and has nobody to press Enter, so
it stops there until a person notices.

The live case at the bottom is the one that decides whether any of this works:
it runs the installed Claude against the real circuit breaker, with and without
the hook, in a disposable directory.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# The cutover harness this borrows imports its siblings by bare name, so the
# suite must work when it is not sys.path[0] -- as under a per-case runner.
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher  # noqa: E402
from scripts.ticket_board import project_provision  # noqa: E402

CHECKS = 0
HOOK_NAME = "ticket-board-claude-permission-hook"
INSTALLER_NAME = "ticket-board-install-pane-hooks"
HOOK = ROOT / "scripts" / HOOK_NAME
ALLOW = {
    "hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "allow"},
    }
}


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def _load(path: Path, name: str) -> Any:
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def run_hook(payload: str) -> tuple[str, str, int]:
    """The helper as Claude runs it: a program on stdin, JSON on stdout."""
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True, timeout=30
    )
    return proc.stdout, proc.stderr, proc.returncode


# --------------------------------------------------------------------------
# What the helper decides
# --------------------------------------------------------------------------


def test_a_bypass_session_is_allowed_with_exactly_the_documented_payload() -> None:
    stdout, _stderr, code = run_hook(
        json.dumps({
            "hook_event_name": "PermissionRequest",
            "permission_mode": "bypassPermissions",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf ."},
        })
    )
    check(code == 0, f"the helper exits 0: {code}")
    check(json.loads(stdout) == ALLOW, f"and allows with the documented schema: {stdout!r}")
    # Nothing else may be on stdout: Claude parses this.
    check(stdout.strip() == json.dumps(ALLOW), f"and prints nothing else: {stdout!r}")


def test_every_other_mode_keeps_claudes_own_permission_flow() -> None:
    """The narrow part. A hook that answers in `default` would be a back door."""
    for mode in ("default", "plan", "acceptEdits", "auto", "dontAsk", "manual", ""):
        stdout, _stderr, code = run_hook(
            json.dumps({"hook_event_name": "PermissionRequest", "permission_mode": mode})
        )
        check(code == 0, f"{mode!r} exits 0: {code}")
        check(stdout.strip() == "", f"{mode!r} gets no decision, not a denial: {stdout!r}")


def test_anything_it_cannot_understand_fails_closed() -> None:
    """Fail closed here means "no decision", which leaves Claude asking."""
    cases = {
        "not json at all": "bypassPermissions",
        "": "empty input",
        "[]": "a list, not an object",
        '"bypassPermissions"': "a bare string",
        "null": "null",
        json.dumps({"permission_mode": "bypassPermissions"}): "no hook_event_name",
        json.dumps({"hook_event_name": "PreToolUse", "permission_mode": "bypassPermissions"}):
            "a different event",
        json.dumps({"hook_event_name": "PermissionRequest"}): "no mode",
        json.dumps({"hook_event_name": "PermissionRequest", "permission_mode": None}): "null mode",
        json.dumps({"hook_event_name": "PermissionRequest", "permission_mode": True}): "a bool mode",
        json.dumps({"hook_event_name": "PermissionRequest", "permission_mode": ["bypassPermissions"]}):
            "a list mode",
        json.dumps({"hook_event_name": "PermissionRequest",
                    "permission_mode": {"mode": "bypassPermissions"}}): "an object mode",
    }
    for payload, why in cases.items():
        stdout, _stderr, code = run_hook(payload)
        check(code == 0, f"{why} still exits 0: {code}")
        check(stdout.strip() == "", f"{why} decides nothing: {stdout!r}")


def test_the_mode_is_read_only_from_the_input_claude_hands_it() -> None:
    """It cannot be talked into bypass by its surroundings.

    The point of reading the mode from the hook input is that the input is
    Claude's own statement about the session. An environment variable or an
    argument is the caller's statement about it, and the caller here is
    whatever the role is running.
    """
    env = dict(os.environ)
    env.update({
        "CLAUDE_PERMISSION_MODE": "bypassPermissions",
        "PERMISSION_MODE": "bypassPermissions",
        "CLAUDE_CODE_PERMISSION_MODE": "bypassPermissions",
    })
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"hook_event_name": "PermissionRequest", "permission_mode": "default"}),
        capture_output=True, text=True, env=env, timeout=30,
    )
    check(proc.stdout.strip() == "", f"an environment claiming bypass decides nothing: {proc.stdout!r}")

    proc = subprocess.run(
        [sys.executable, str(HOOK), "--permission-mode", "bypassPermissions"],
        input=json.dumps({"hook_event_name": "PermissionRequest", "permission_mode": "default"}),
        capture_output=True, text=True, timeout=30,
    )
    check(proc.stdout.strip() == "", f"and neither does an argument saying so: {proc.stdout!r}")


def test_an_enormous_input_is_refused_rather_than_parsed() -> None:
    payload = json.dumps({
        "hook_event_name": "PermissionRequest",
        "permission_mode": "bypassPermissions",
        "tool_input": {"command": "x" * (5 * 1024 * 1024)},
    })
    stdout, _stderr, code = run_hook(payload)
    check(code == 0, f"exits 0: {code}")
    check(stdout.strip() == "", "an input past the cap decides nothing")


# --------------------------------------------------------------------------
# What the installer writes
# --------------------------------------------------------------------------


def _install(home: Path, *, staged: Path | None = None, extra: list[str] | None = None) -> dict:
    installer = (staged / INSTALLER_NAME) if staged else (ROOT / "scripts" / INSTALLER_NAME)
    args = [
        sys.executable, str(installer), "install",
        "--home", str(home),
        "--bin-path", str(home / ".local" / "bin" / "ticket-board-pane-idle-hook"),
        "--hook-source", str(ROOT / "scripts" / "ticket-board-pane-idle-hook"),
        *(extra or []),
    ]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _entries(config: dict, event: str) -> list[dict]:
    return list(config.get("hooks", {}).get(event, []))


def test_the_permission_hook_is_registered_for_every_tool() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd234-install.") as raw:
        config = _install(Path(raw))
    entries = _entries(config, "PermissionRequest")
    check(len(entries) == 1, f"exactly one managed PermissionRequest entry: {entries}")
    entry = entries[0]
    check(
        "matcher" not in entry,
        "registered with no matcher, so a bypass pane cannot stall on any tool's "
        f"prompt rather than only Bash's: {entry}",
    )
    command = entry["hooks"][0]["command"]
    check(command.endswith(HOOK_NAME), f"and it runs the helper: {command}")


def test_the_helper_it_points_at_is_the_staged_one_not_a_copy_in_the_home() -> None:
    """A role that could rewrite the helper could widen its own session."""
    with tempfile.TemporaryDirectory(prefix="syrd234-staged.") as raw:
        tmp = Path(raw)
        staged = tmp / "staging"
        staged.mkdir()
        for name in (INSTALLER_NAME, HOOK_NAME, "ticket-board-pane-idle-hook"):
            shutil.copy2(ROOT / "scripts" / name, staged / name)
        # Staging copies the package tree beside the entry points; without it
        # the installer cannot import its own helpers.
        shutil.copytree(ROOT / "scripts" / "ticket_board", staged / "ticket_board")
        home = tmp / "home"
        home.mkdir()
        config = _install(home, staged=staged)
        command = _entries(config, "PermissionRequest")[0]["hooks"][0]["command"]
    check(
        command == str(staged / HOOK_NAME),
        f"the registered path is the staged helper beside the installer: {command}",
    )
    check(str(home) not in command, f"and nothing under the role's own home: {command}")


def test_the_installer_refuses_when_the_helper_is_missing() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd234-nohelper.") as raw:
        tmp = Path(raw)
        staged = tmp / "staging"
        staged.mkdir()
        for name in (INSTALLER_NAME, "ticket-board-pane-idle-hook"):
            shutil.copy2(ROOT / "scripts" / name, staged / name)
        shutil.copytree(ROOT / "scripts" / "ticket_board", staged / "ticket_board")
        home = tmp / "home"
        home.mkdir()
        proc = subprocess.run(
            [sys.executable, str(staged / INSTALLER_NAME), "install",
             "--home", str(home),
             "--bin-path", str(home / ".local" / "bin" / "ticket-board-pane-idle-hook"),
             "--hook-source", str(ROOT / "scripts" / "ticket-board-pane-idle-hook")],
            capture_output=True, text=True, timeout=120,
        )
    check(proc.returncode != 0, "a bundle without the helper is refused, not silently skipped")
    check(HOOK_NAME in proc.stderr, f"and says what is missing: {proc.stderr[-200:]}")
    check(
        not (home / ".claude" / "settings.json").exists(),
        "and writes no settings claiming a hook that is not there",
    )


def test_the_permission_prompt_notification_does_not_replace_the_idle_one() -> None:
    """Both matchers, one merge.

    `_merge_hook_entries` drops every managed entry for an event before it
    appends, so registering the second Notification matcher in its own call
    silently removed the first. That is how this was written the first time.
    """
    with tempfile.TemporaryDirectory(prefix="syrd234-notify.") as raw:
        config = _install(Path(raw))
    matchers = sorted(entry.get("matcher", "") for entry in _entries(config, "Notification"))
    check(
        matchers == ["idle_prompt", "permission_prompt"],
        f"both notification matchers survive: {matchers}",
    )
    by_matcher = {e.get("matcher", ""): e["hooks"][0]["command"] for e in _entries(config, "Notification")}
    check(
        "blocked" in by_matcher["permission_prompt"]
        and "claude.Notification.permission_prompt" in by_matcher["permission_prompt"],
        f"a prompt nobody answered marks the pane blocked: {by_matcher['permission_prompt']}",
    )
    check("idle" in by_matcher["idle_prompt"], by_matcher["idle_prompt"])


def test_installing_twice_changes_nothing_and_keeps_foreign_hooks() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd234-idempotent.") as raw:
        home = Path(raw)
        settings = home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        foreign = {
            "hooks": {
                "PermissionRequest": [
                    {"matcher": "Edit", "hooks": [{"type": "command", "command": "/opt/mine/review.sh"}]}
                ],
                "Notification": [
                    {"hooks": [{"type": "command", "command": "/opt/mine/notify.sh"}]}
                ],
            },
            "env": {"SOMETHING": "kept"},
        }
        settings.write_text(json.dumps(foreign), encoding="utf-8")
        first = _install(home)
        second = _install(home)

    check(first == second, "a second install is a no-op")
    commands = [e["hooks"][0]["command"] for e in _entries(second, "PermissionRequest")]
    check(
        "/opt/mine/review.sh" in commands,
        f"somebody else's PermissionRequest hook is still there: {commands}",
    )
    check(
        sum(1 for c in commands if c.endswith(HOOK_NAME)) == 1,
        f"and ours appears exactly once, not once per install: {commands}",
    )
    notify = [e["hooks"][0]["command"] for e in _entries(second, "Notification")]
    check("/opt/mine/notify.sh" in notify, f"foreign Notification hook kept: {notify}")
    check(second.get("env") == {"SOMETHING": "kept"}, "and unrelated settings are untouched")


# --------------------------------------------------------------------------
# Staged as role tooling, and refreshed for tenants that already exist
# --------------------------------------------------------------------------


def test_the_helper_is_staged_with_the_rest_of_the_role_tooling() -> None:
    check(
        HOOK_NAME in project_provision.ROLE_STAGED_EXECUTABLES,
        "the helper is staged role tooling, not something a role installs for itself",
    )
    rendered = "\n".join(project_provision.role_tooling_staging_commands("demo", "/opt/switchyard/releases/abc"))
    check(HOOK_NAME in rendered, "and the staging script installs it")
    for line in rendered.splitlines():
        if HOOK_NAME in line and "install -m" in line:
            check(
                "-o root -g root" in line and "0755" in line,
                f"root-owned and executable: {line.strip()}",
            )
            break
    else:  # pragma: no cover - the assertion above is the point
        check(False, f"no install line for the helper: {rendered[:400]}")


def test_an_upgrade_refreshes_the_hooks_of_a_tenant_that_already_exists() -> None:
    """Staging refreshes the tooling; nothing refreshed what points at it.

    The registrations were written when the account was created and never
    again, so a tenant taking this release would have the helper staged and
    unreferenced -- still stalling on the prompt it answers.
    """
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")

    with tempfile.TemporaryDirectory(prefix="syrd234-upgrade.") as raw:
        tmp = Path(raw)
        config, _path = _tenant(tmp)
        printed: list[str] = []
        problems = team_launcher.refresh_role_pane_hooks(
            config, staging_root=tmp / "staging", runner=runner, print_func=printed.append
        )
    check(problems == [], f"nothing to report for a healthy tenant: {problems}")
    check(len(calls) == 1, f"one account, one refresh: {calls}")
    argv = calls[0]
    check(INSTALLER_NAME in " ".join(argv), f"it runs the installer: {argv}")
    check("install" in argv, f"with the install subcommand: {argv}")
    staged_installer = str((tmp / "staging" / config.project / INSTALLER_NAME))
    check(
        staged_installer in argv,
        f"the STAGED installer, not a checkout copy: {argv}",
    )
    check("--home" in argv, f"pointed at the account's home: {argv}")
    check("\n".join(printed).count("refreshed") == 1, f"and says so once: {printed}")


def test_the_refresh_reports_an_account_it_could_not_reach() -> None:
    def failing(args, **kwargs):
        return subprocess.CompletedProcess(list(args), 1, stdout="", stderr="sudo: no such user")

    with tempfile.TemporaryDirectory(prefix="syrd234-upgrade-fail.") as raw:
        tmp = Path(raw)
        config, _path = _tenant(tmp)
        problems = team_launcher.refresh_role_pane_hooks(
            config, staging_root=tmp / "staging", runner=failing, print_func=lambda _l: None
        )
    check(len(problems) == 1, f"the failure is reported: {problems}")
    check("no such user" in problems[0], f"with what went wrong: {problems[0]}")


def test_a_dry_run_says_what_it_would_refresh_and_runs_nothing() -> None:
    calls: list[list[str]] = []

    def runner(args, **kwargs):  # pragma: no cover - must not be reached
        calls.append(list(args))
        return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")

    with tempfile.TemporaryDirectory(prefix="syrd234-dry.") as raw:
        tmp = Path(raw)
        config, _path = _tenant(tmp)
        printed: list[str] = []
        problems = team_launcher.refresh_role_pane_hooks(
            config, staging_root=tmp / "staging", dry_run=True,
            runner=runner, print_func=printed.append,
        )
    check(problems == [], f"a dry run reports no problems: {problems}")
    check(calls == [], f"and runs nothing: {calls}")
    check(any("would refresh" in line for line in printed), f"but says what it would do: {printed}")


def test_the_upgrade_itself_calls_the_refresh() -> None:
    """The call site, not just the function.

    A refresh nothing calls is a tenant that still stalls. This drives the real
    `upgrade_project_command` through the cutover suite's own harness and
    watches for the call.
    """
    cutover = _load(ROOT / "tests" / "team_launcher_upgrade_cutover_test.py", "cutover_for_syrd234")
    calls: list[str] = []
    saved = team_launcher.refresh_role_pane_hooks

    def spy(config, **kwargs):
        calls.append(config.project)
        return []

    with tempfile.TemporaryDirectory(prefix="syrd234-upgrade-callsite.") as tmp:
        config_path, _ = cutover._declarative_tenant(Path(tmp), accounts=True)
        cutover._mark_projection_migrated(config_path)
        try:
            team_launcher.refresh_role_pane_hooks = spy
            with cutover._RunningTenant(config_path, account_uid=os.getuid()) as tenant:
                tenant.live = False
                result, output, _migrations = cutover._upgrade(
                    config_path, as_root=True, exists=set(),
                    runner=tenant.runner(), board=cutover._board_with_marker(True),
                )
        finally:
            team_launcher.refresh_role_pane_hooks = saved

    check(result == 0, f"the upgrade completes: {output[-600:]}")
    check(calls, "and it refreshed the tenant's pane hooks")


def test_a_refresh_that_fails_stops_the_upgrade_before_readiness() -> None:
    """A tenant whose hooks did not land must not be reported ready."""
    cutover = _load(ROOT / "tests" / "team_launcher_upgrade_cutover_test.py", "cutover_for_syrd234b")
    saved = team_launcher.refresh_role_pane_hooks

    def refuse(config, **kwargs):
        return [f"could not refresh {config.project}'s pane hooks for someone"]

    with tempfile.TemporaryDirectory(prefix="syrd234-upgrade-refuse.") as tmp:
        config_path, _ = cutover._declarative_tenant(Path(tmp), accounts=True)
        cutover._mark_projection_migrated(config_path)
        try:
            team_launcher.refresh_role_pane_hooks = refuse
            with cutover._RunningTenant(config_path, account_uid=os.getuid()) as tenant:
                tenant.live = False
                result, output, _migrations = cutover._upgrade(
                    config_path, as_root=True, exists=set(),
                    runner=tenant.runner(), board=cutover._board_with_marker(True),
                )
        finally:
            team_launcher.refresh_role_pane_hooks = saved

    check(result == 1, f"the upgrade stops: {result}")
    check("could not refresh" in output, f"saying what failed: {output[-400:]}")
    check(
        "without the hooks this release stages" in output,
        f"and what it means for the roles: {output[-400:]}",
    )


def _tenant(tmp: Path):
    """A tenant whose roles all run as the project account, as they do now."""
    layout = tmp / "layout.json"
    layout.write_text(json.dumps({"Orientation": "Horizontal", "Widgets": [
        {"Command": "", "SessionRestoreId": i, "WorkingDirectory": ""} for i in range(2)]}), encoding="utf-8")
    (tmp / "repo").mkdir(exist_ok=True)
    path = tmp / "demo.json"
    path.write_text(json.dumps({
        "project": "demo",
        "project_name": "Demo",
        "layout": str(layout),
        "repository": str(tmp / "repo"),
        "run_as_user": team_launcher.current_user_name(),
        "session_dir": str(tmp / "state" / "pane-sessions"),
        "role_state_isolation": True,
        "desktop_access": {"mode": "headless"},
        "roles": [
            {"role": role, "slot": index, "cli": ["claude"], "live_commands": ["claude"],
             "target": f"demo-{role}:0.0", "tmux_session": f"demo-{role}"}
            for index, role in enumerate(("director", "main"))
        ],
    }, indent=2), encoding="utf-8")
    return team_launcher.load_project_config("demo", path), path


# --------------------------------------------------------------------------
# What the onboarding tells a role to write
# --------------------------------------------------------------------------


def test_onboarding_requires_a_guarded_recursive_delete() -> None:
    guide = (ROOT / "docs" / "onboarding" / "switchyard-board-guide.md").read_text(encoding="utf-8")
    check('${TARGET:?' in guide, "the role-facing guide names the guard")
    check(
        "rm -rf --" in guide and '"${TARGET:?' in guide,
        "as a quoted, guarded, end-of-options delete",
    )
    check(
        "bypass" in guide.lower() and "confirmation" in guide.lower(),
        "and says why it matters here: the prompt is not skipped by bypass permissions",
    )

    with tempfile.TemporaryDirectory(prefix="syrd234-onboarding.") as raw:
        tmp = Path(raw)
        project_dir = tmp / "project"
        project_dir.mkdir()
        team_launcher._write_switchyard_onboarding_files(
            project_name="Demo", slug="demo", owner_user="demo",
            project_dir=project_dir,
            artifact_path=project_dir / "artifact.json",
            design_document=project_dir / "design.md",
            director_onboarding=project_dir / ".switchyard" / "DIRECTOR_ONBOARDING.md",
        )
        written = [
            (project_dir / ".switchyard" / "DESIGNER_ONBOARDING.md").read_text(encoding="utf-8"),
            (project_dir / ".switchyard" / "DIRECTOR_ONBOARDING.md").read_text(encoding="utf-8"),
        ]
    for text in written:
        check('${TARGET:?}' in text, f"generated onboarding carries the guard: {text[-200:]}")


# --------------------------------------------------------------------------
# A pane stopped on a prompt is waiting, and the Director hears about it
# --------------------------------------------------------------------------


def _listener_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    from ticket_board import notify_listener  # noqa: PLC0415

    return notify_listener


def _blocked_pane(state_dir: Path, target: str, *, state: str, source: str, ago: float = 600.0):
    import time as _time

    state_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in target)
    (state_dir / f"{safe}.json").write_text(
        json.dumps({
            "target": target, "state": state, "source": source,
            "updated_at": _time.time() - ago,
        }),
        encoding="utf-8",
    )


def _real_gate(state_dir: Path, role_targets: dict[str, str]):
    """A real PaneActivityGate over a real state directory.

    Not a stand-in: the reader belongs to the gate because the gate is what
    holds pane state, and a fake with the right attribute names is exactly how
    an earlier version of this test passed while the listener could not reach
    it at all.
    """
    notify_listener = _listener_module()
    gate = notify_listener.PaneActivityGate(
        state_store=notify_listener.PaneHookStateStore(state_dir)
    )
    gate.role_targets = dict(role_targets)
    return gate


class _FakeSelf:
    """Stands in for the listener's construction, not for any of its logic.

    `activity_gate` is a BOUND METHOD of a real gate, because that is what the
    listener holds and how it reaches the gate -- `__self__` off the bound
    method. A fake that exposed `state_store` directly would hide the wiring.
    """

    def __init__(self, gate, grace=120):
        self.activity_gate = gate.is_working
        self.permission_prompt_grace_seconds = grace
        self.logged: list[str] = []

        class _Logger:
            def __init__(self, sink):
                self._sink = sink

            def info(self, msg, *args):
                self._sink.append(msg % args if args else msg)

            warning = info

        self.logger = _Logger(self.logged)


def test_only_a_pane_stopped_on_a_prompt_counts_as_waiting() -> None:
    notify_listener = _listener_module()
    with tempfile.TemporaryDirectory(prefix="syrd234-waits.") as raw:
        state_dir = Path(raw)
        _blocked_pane(state_dir, "syrd-main:0.0", state="blocked",
                      source="claude.Notification.permission_prompt")
        _blocked_pane(state_dir, "syrd-ops:0.0", state="blocked", source="codex.PermissionRequest")
        # Blocked for some other reason, busy, and idle: none of them is this.
        _blocked_pane(state_dir, "syrd-app:0.0", state="blocked", source="claude.Stop")
        _blocked_pane(state_dir, "syrd-audit:0.0", state="busy",
                      source="claude.Notification.permission_prompt")
        _blocked_pane(state_dir, "syrd-director:0.0", state="idle", source="claude.Stop")
        gate = _real_gate(state_dir, {
            "main": "syrd-main:0.0", "ops": "syrd-ops:0.0", "app": "syrd-app:0.0",
            "audit": "syrd-audit:0.0", "director": "syrd-director:0.0",
        })
        waiting = gate.permission_prompt_waits()

    check(sorted(waiting) == ["main", "ops"], f"only the panes on a prompt: {waiting}")
    check(
        all(value.endswith("+00:00") for value in waiting.values()),
        f"reported as instants the board can compare: {waiting}",
    )


def test_the_permission_prompt_source_is_never_treated_as_idle() -> None:
    """Delivering into a stopped pane would type at a confirmation dialog."""
    notify_listener = _listener_module()
    for source in notify_listener.PERMISSION_PROMPT_BLOCK_SOURCES:
        check(
            source not in notify_listener.TRUSTED_IDLE_SOURCES,
            f"{source} is not a trusted idle source",
        )
    check(
        "claude.Notification.permission_prompt" in notify_listener.PERMISSION_PROMPT_BLOCK_SOURCES,
        "the Claude prompt source is one of them",
    )


def test_the_director_is_told_with_the_role_and_how_long_it_has_waited() -> None:
    notify_listener = _listener_module()
    calls: list[tuple] = []

    class _Result:
        def fetchone(self):
            return (1,)

    class _Conn:
        def execute(self, sql, params=None):
            calls.append((sql, params))
            return _Result()

    with tempfile.TemporaryDirectory(prefix="syrd234-escalate.") as raw:
        state_dir = Path(raw)
        _blocked_pane(state_dir, "syrd-main:0.0", state="blocked",
                      source="claude.Notification.permission_prompt")
        fake = _FakeSelf(_real_gate(state_dir, {"main": "syrd-main:0.0"}))
        enqueued = notify_listener.TicketBoardNotifyListener._process_permission_prompt_waits(
            fake, _Conn()
        )

    check(enqueued == 1, f"the generator reports what it enqueued: {enqueued}")
    check(len(calls) == 1, f"one call: {calls}")
    sql, params = calls[0]
    check("notify_permission_prompt_waits" in sql, f"the permission-prompt generator: {sql}")
    payload = json.loads(params[0])
    check(list(payload) == ["main"], f"naming the waiting role: {payload}")
    check(params[1] == "120 seconds", f"and the grace it waited: {params[1]}")
    check(
        any("permission prompt" in line for line in fake.logged),
        f"and it says so in the log: {fake.logged}",
    )


def test_nothing_is_said_when_no_pane_is_waiting() -> None:
    notify_listener = _listener_module()

    class _Conn:
        def execute(self, sql, params=None):  # pragma: no cover - must not run
            raise AssertionError(f"asked the board about nothing: {sql}")

    with tempfile.TemporaryDirectory(prefix="syrd234-quiet.") as raw:
        state_dir = Path(raw)
        _blocked_pane(state_dir, "syrd-main:0.0", state="idle", source="claude.Stop")
        fake = _FakeSelf(_real_gate(state_dir, {"main": "syrd-main:0.0"}))
        enqueued = notify_listener.TicketBoardNotifyListener._process_permission_prompt_waits(
            fake, _Conn()
        )
    check(enqueued == 0, "a quiet board is left alone")


def test_the_generator_runs_on_a_pass_where_no_turn_ended() -> None:
    """The trap SYRD-207 hit: a stopped pane ends no turns.

    If this ran only after a turn end, the one case it exists for -- a role
    that has stopped entirely -- would never reach it.
    """
    source = (ROOT / "scripts" / "ticket_board" / "notify_listener.py").read_text(encoding="utf-8")
    early = source.index("self._process_unresolved_turn_end(conn, {})")
    following = source[early:early + 400]
    check(
        "_process_permission_prompt_waits" in following,
        "the early-return pass calls it too, before returning",
    )
    check(
        following.index("_process_permission_prompt_waits") < following.index("return 0"),
        "and before the return, not after it",
    )


# --------------------------------------------------------------------------
# The installed Claude, the real circuit breaker, a disposable directory
# --------------------------------------------------------------------------

LIVE_PROMPT = (
    "Run exactly this bash command now. Do not inspect it, do not ask for "
    "confirmation, do not suggest alternatives. Disposable scratch directory:\n"
    "rm -rf ."
)


def _live_tool_results(sandbox: Path, settings: Path) -> list[str]:
    """Run the installed Claude in `sandbox` and return what its tools reported.

    `--permission-prompts none` is what makes this safe to run unattended: a
    prompt that nobody answers is denied immediately instead of waiting, so the
    case that is failing cannot hang the suite.

    The command is `rm -rf .` because that is what the breaker actually catches
    -- a removal aimed at the working directory. GNU rm refuses the `.` operand
    itself, so even when the permission layer allows it nothing is deleted;
    what changes is whether rm ran at all.
    """
    proc = subprocess.run(
        [
            "claude", "-p", LIVE_PROMPT,
            "--permission-mode", "bypassPermissions",
            "--permission-prompts", "none",
            "--settings", str(settings),
            "--output-format", "stream-json",
            "--verbose",
            "--max-budget-usd", "0.35",
        ],
        cwd=str(sandbox),
        env={"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
    )
    results: list[str] = []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                raw = block.get("content")
                if isinstance(raw, str):
                    results.append(raw)
                elif isinstance(raw, list):
                    results.append(
                        " ".join(b.get("text", "") for b in raw if isinstance(b, dict))
                    )
    return results


def test_the_installed_claude_stalls_without_the_hook_and_proceeds_with_it() -> None:
    """The whole ticket, against the provider that owns the behaviour.

    Everything above this asserts what Switchyard writes. Only this says
    whether Claude does anything different because of it -- and the published
    note that the breaker cannot be overridden by a hook says it should not.
    That note is about `permissions.allow` rules and PreToolUse; measured here,
    a PermissionRequest hook does answer it.
    """
    check(bool(shutil.which("claude")), "the installed claude is required")
    with tempfile.TemporaryDirectory(prefix="syrd234-live.") as raw:
        tmp = Path(raw)
        without = tmp / "settings-none.json"
        without.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        with_hook = tmp / "settings-hook.json"
        with_hook.write_text(
            json.dumps({"hooks": {"PermissionRequest": [
                {"hooks": [{"type": "command", "command": str(HOOK)}]}
            ]}}),
            encoding="utf-8",
        )
        outcomes = {}
        for name, settings in (("without", without), ("with", with_hook)):
            sandbox = tmp / f"sandbox-{name}"
            (sandbox / "sub").mkdir(parents=True)
            outcomes[name] = _live_tool_results(sandbox, settings)
            # The sandbox is disposable and nothing outside it is named.
            check(sandbox.exists(), f"the {name} sandbox is still there: rm refuses '.'")

    for name, results in outcomes.items():
        # A run where the model talked instead of running the command proves
        # nothing either way, so it is a failure rather than a quiet pass.
        check(
            bool(results),
            f"the {name}-hook run actually attempted the command: {results}",
        )

    without_text = "\n".join(outcomes["without"])
    with_text = "\n".join(outcomes["with"])
    check(
        "denied" in without_text.lower(),
        f"without the hook the breaker stops it -- this is the live stall: {without_text[:200]!r}",
    )
    check(
        "refusing to remove" in with_text,
        "with the hook the command reaches rm, which declines '.' on its own "
        f"terms: {with_text[:200]!r}",
    )
    check(
        "denied" not in with_text.lower(),
        f"and the permission layer did not stop it: {with_text[:200]!r}",
    )


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("claude_permission_hook_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(600)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"claude_permission_hook_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
