#!/usr/bin/env python3
"""SYRD-579: a role keeps the model it chose across every restart Switchyard makes.

Live on Otto (2026-10-10): the operator had picked Opus 5.5 in each Claude role
with Claude's own `/model`, which saves the pick as the account's default. The
upgrade to a025c35 and the start after it restarted every Claude role with
`--model claude-opus-5 --effort high` from the configuration `switchyard new`
wrote, and ops resumed the right conversation on the old model. Measured on
Claude Code 2.1.294 in a sealed home: the flag beats the saved pick, on a fresh
start and on `--resume`.

Each case runs in a child process against a tenant built in a scratch home:
- four Claude roles on `claude-opus-5`, the template's model. director, audit
  and ops last ran `claude-opus-5-5`, a model newer than the template, in
  transcripts shaped as Claude writes them;
- research, whose recorded session is missing, so its start falls back to a
  fresh one;
- one Codex role, which never counts.
The before-run is main's code from a git archive, given the same tenant.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "a025c35befb42bbff0b2b17e3f67888f46996daa"  # main before SYRD-579, the release Otto runs
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:  # the label again, last, so a truncated tail still says which check failed
        raise AssertionError(f"{detail}\nFAILED CHECK: {re.split(r': [\[{(]', detail, maxsplit=1)[0]}")
    CHECKS += 1


# Run in the child, as `python3 -c`, from the tree under test. argv: tree, scratch, case.
CHILD = r'''
import contextlib, io, json, os, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
tree, scratch, case = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(tree))
os.chdir(tree)
from scripts import team_launcher as launcher

PROJECT = "otto"
HOME = scratch / "home"
PROVISION = HOME / "Projects" / "otto_scheduler" / ".switchyard" / "provision"
CONFIG = PROVISION / "otto.json"
SESSIONS = HOME / ".local" / "state" / "switchyard" / "projects" / "otto" / "pane-sessions"
CLAUDE = {"director": "a1", "audit": "b2", "ops": "c3", "research": "d4"}
OLD, NEW = "claude-opus-5", "claude-opus-5-5"


def line(**record):
    return json.dumps(record) + "\n"


def turn(model, effort, at, **extra):
    # The keys Claude Code 2.1.294 writes on an assistant record.
    return line(type="assistant", isSidechain=False, effort=effort, timestamp=at, sessionId="x", uuid=at,
                message={"id": "m", "model": model, "role": "assistant", "content": [], "type": "message"}, **extra)


def build():
    PROVISION.mkdir(parents=True, exist_ok=True)
    (PROVISION / "otto-konsole-layout.json").write_text(json.dumps(launcher._new_project_layout_payload(5)) + "\n")
    roles = []
    for slot, name in enumerate([*CLAUDE, "main"]):
        cli = "codex" if name == "main" else "claude"
        roles.append({"cli": [cli], "live_commands": [cli], "role": name, "slot": slot, "yolo": True,
                      "model": "gpt-5.6-sol" if cli == "codex" else OLD, "effort": "high",
                      "target": f"otto-{name}:0.0", "tmux_session": f"otto-{name}",
                      "workdir": str(HOME / "worktrees" / name)})
        (HOME / "worktrees" / name).mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({
        "project": PROJECT, "layout": "otto-konsole-layout.json", "roles": roles, "run_as_user": launcher.current_user_name(),
        "session_dir": str(SESSIONS), "role_state_isolation": True, "desktop_access": {"mode": "headless"},
    }, indent=2) + "\n")
    (PROVISION.parent.parent / ".switchyard").mkdir(exist_ok=True)
    (PROVISION.parent / "otto.project.json").write_text(json.dumps(
        {"project": {"role_models": {name: OLD for name in CLAUDE}, "role_efforts": {name: "high" for name in CLAUDE}}}) + "\n")
    config = launcher.load_project_config(PROJECT, CONFIG)
    for role in config.roles:
        if role.role not in CLAUDE:
            continue
        sid = CLAUDE[role.role]
        store = launcher.role_session_dir(config, role)
        store.mkdir(parents=True, exist_ok=True)
        transcript = launcher._claude_project_dir_for_workdir(role.workdir, home=HOME) / f"{sid}.jsonl"
        payload = {"session_id": sid}
        if role.role in ("director", "audit"):
            payload["transcript_path"] = str(transcript)  # recorded by the hook; ops is found by its workdir
        (store / launcher.session_file_name(role.target)).write_text(json.dumps(
            {"target": role.target, "session_id": sid, "source": "claude.SessionStart", "payload": payload}) + "\n")
        if role.role == "research":
            continue  # the recorded conversation is gone: its start falls back to a fresh session
        transcript.parent.mkdir(parents=True, exist_ok=True)
        effort = "medium" if role.role == "director" else "high"
        transcript.write_text(
            line(type="user", cwd=role.workdir, message={"role": "user", "content": "hi"},  # SYRD-638: where it ran
                 timestamp="2026-10-10T10:00:00.000Z")
            + turn(OLD, "high", "2026-10-10T10:00:05.000Z")
            + line(type="system", subtype="local_command", content="Set model to Opus 5.5 and saved as your default for new sessions",
                   timestamp="2026-10-10T10:20:00.000Z")
            + turn(NEW, effort, "2026-10-10T10:30:00.000Z")
            # Claude's own and a subagent's turns run on what they were given, not on the role's pick.
            + turn("<synthetic>", "", "2026-10-10T10:31:00.000Z")
            + line(type="assistant", isSidechain=True, effort="low", timestamp="2026-10-10T10:32:00.000Z",
                   message={"model": "claude-haiku-5-5"})
            + line(type="user", message={"role": "user", "content": "ok"}, timestamp="2026-10-10T10:33:00.000Z")
        )
    return launcher.load_project_config(PROJECT, CONFIG)


class PastGate(Exception):
    pass


live = {}  # role -> the model its running process was started with; absent means stopped
calls = []


def runner(args, **kwargs):
    argv = [str(a) for a in args]
    calls.append(argv)
    if "has-session" in argv:
        target = argv[argv.index("-t") + 1].split(":")[0].removeprefix("=")
        return subprocess.CompletedProcess(argv, 0 if target.removeprefix("otto-") in live else 1, "", "")
    return subprocess.CompletedProcess(argv, 0, "", "")


def started(name):
    return any("new-session" in argv and f"otto-{name}" in " ".join(argv) for argv in calls)


def commands(config):
    out = {}
    for role in config.roles:
        if role.role not in CLAUDE:
            continue
        store = launcher.role_session_dir(config, role)
        resumable, _why = launcher._resume_preflight_allows_attempt(role, launcher.session_id_for_role(role, store), session_dir=store)
        command = launcher.cli_command_for_role(role, session_dir=store, resume=resumable)
        tail = command[len(command) - 1 - command[::-1].index(role.cli[0]):]  # the provider's own argv, last
        out[role.role] = " ".join(tail[:7])
    return out


def launch(config, mode="start"):
    launcher.process_authority_board_compatibility = lambda *_a, **_k: (True, "")
    launcher.migrate_declarative_director_onboarding = lambda *_a, **_k: False
    # The process table is this host's; whether a pane runs its CLI is the fixture's to say.
    launcher.live_command_matches_role = lambda role, **_k: role.role in live
    def past(*_a, **_k):
        raise PastGate
    launcher._launch_runners_and_paths = past
    printed = io.StringIO()
    try:
        with contextlib.redirect_stdout(printed):
            code = launcher.launch_project(config, config_path=CONFIG, mode=mode, script_path=tree / "scripts" / "team-launcher",
                                           runner=runner, print_func=print)
    except PastGate:
        code = "past the gate"
    return {"code": code, "printed": printed.getvalue().splitlines(), "started": [n for n in [*CLAUDE, "main"] if started(n)]}


upgrade_seen = {}
UPGRADE = launcher.upgrade_project_command  # the real one, whatever a case later puts in front of it


def upgrade(config, **kwargs):
    reached = []
    def pin(*_a, **_k):
        reached.append("pin")
        raise PastGate
    launcher._pin_upgrade_source = pin
    printed = io.StringIO()
    try:
        with contextlib.redirect_stdout(printed):
            code = UPGRADE(config, config_path=CONFIG, runner=runner, print_func=print, **kwargs)
    except PastGate:
        code = "past the gate"
    upgrade_seen.update(code=code, printed=printed.getvalue().splitlines(), reached=reached, config=json.loads(CONFIG.read_text()))
    return dict(upgrade_seen)


def models(raw=None):
    raw = raw or json.loads(CONFIG.read_text())
    return {r["role"]: [r.get("model"), r.get("effort"), bool(r.get("model_selected_at"))] for r in raw["roles"]}


config = build()
result = {"commands_before": commands(config)}
if case == "before":
    result["launch"] = launch(config)
    result["upgrade"] = upgrade(config)
elif case == "drift":
    from scripts import role_model_selection as rms
    result["drift"] = [d.describe() for d in rms.role_model_drift(config, config_path=CONFIG)]
    result["launch"] = launch(config)
    calls.clear()
    live.update({"ops": NEW})
    result["launch_ops_live"] = launch(config)
    result["reload_ops_live"] = launch(config, mode="reload")
    result["upgrade"] = upgrade(config)
    result["upgrade_dry"] = upgrade(config, dry_run=True)
    result["models_untouched"] = models()
elif case in ("reconcile_running", "reconcile_configured"):
    from scripts import role_model_selection as rms
    keep = case.removeprefix("reconcile_")
    remedy = next(l for l in launch(config)["printed"] if f"--keep {keep}" in l).split("switchyard ", 1)[1]
    printed = io.StringIO()
    launcher._resolve_switchyard_project = lambda project: SimpleNamespace(slug=project, config_path=CONFIG)
    launcher._load_switchyard_project_config_for_command = lambda entry, argv: launcher.load_project_config(PROJECT, entry.config_path)
    calls.clear()
    with contextlib.redirect_stdout(printed):
        result["reconcile_code"] = launcher.switchyard_main(remedy.split())  # the printed remedy, through the real parser
    result["reconcile_printed"] = printed.getvalue().splitlines()
    result["remedy"] = remedy
    result["models"] = models()
    result["artifact"] = json.loads((PROVISION.parent / "otto.project.json").read_text())["project"]
    config = launcher.load_project_config(PROJECT, CONFIG)
    result["commands_after"] = commands(config)
    result["drift_after"] = [d.describe() for d in rms.role_model_drift(config, config_path=CONFIG)]
    result["started_by_reconcile"] = [n for n in [*CLAUDE, "main"] if started(n)]
    result["launch_after"] = launch(config)
    result["upgrade_after"] = upgrade(config)
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        result["again_code"] = launcher.switchyard_main(remedy.split())
    result["again_printed"] = printed.getvalue().splitlines()
    # A pick made after the choice was recorded counts again.
    transcript = launcher._claude_project_dir_for_workdir(str(HOME / "worktrees" / "audit"), home=HOME) / "b2.jsonl"
    with transcript.open("a") as handle:
        handle.write(turn("claude-sonnet-5-5", "high", "2099-01-01T00:00:00.000Z"))
    result["drift_later_pick"] = [d.describe() for d in rms.role_model_drift(config, config_path=CONFIG)]
    # What finish-upgrade, adopt-workflow and every workflow apply do to the tenant's configuration.
    from scripts import workflow_launcher
    document = {**json.loads((tree / "examples" / "workflows" / "inspection.json").read_text().replace("cerulean-", "otto-")), "project": PROJECT}
    projected = workflow_launcher.project_roles(json.loads(CONFIG.read_text()), document)
    result["projected"] = {r["role"]: [r.get("model"), r.get("effort"), bool(r.get("model_selected_at"))]
                           for r in [*projected["roles"], *projected["retired_workflow_roles"].values()]}
elif case == "reconcile_live":
    from scripts import role_model_selection as rms, role_runtime
    # After a restart that put them back on the template: director, ops and research run --model claude-opus-5.
    live.update({"director": OLD, "ops": OLD, "research": OLD})
    busy = {"director"}
    restarts = []
    launcher.live_model_for_role = lambda role, **_k: live.get(role.role, "")
    role_runtime._is_busy = lambda role, **_k: role.role in busy
    def reload(role, *, mode, session_dir, **_k):
        restarts.append([role.role, mode, role.model, launcher.session_id_for_role(role, session_dir)])
        live[role.role] = role.model
        return 0
    launcher.ensure_visible_role_session_for_viewer = reload
    out = {}
    for name, kwargs in (
        ("dry", {"dry_run": True}),
        ("running", {}),
        ("research", {"roles": ["research"], "model": NEW}),
    ):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = launcher.reconcile_role_models_command(config, config_path=CONFIG, runner=runner, print_func=print, **kwargs)
        out[name] = {"code": code, "printed": printed.getvalue().splitlines(), "restarts": list(restarts), "models": models()}
        restarts.clear()
        config = launcher.load_project_config(PROJECT, CONFIG)
    # A restart that comes back on something else is a failure, not a success.
    busy.clear()
    launcher.ensure_visible_role_session_for_viewer = lambda role, **_k: 0
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = launcher.reconcile_role_models_command(config, config_path=CONFIG, runner=runner, print_func=print)
    out["stays_old"] = {"code": code, "printed": printed.getvalue().splitlines()}
    result["live"] = out
    result["commands_after"] = commands(config)
elif case == "configured_live":
    from scripts import role_runtime
    live.update({"director": OLD, "audit": OLD})  # running the configured flag, but each session picked 5.5
    restarts = []
    launcher.live_model_for_role = lambda role, **_k: live.get(role.role, "")
    role_runtime._is_busy = lambda role, **_k: False
    launcher.ensure_visible_role_session_for_viewer = lambda role, *, mode, **_k: restarts.append([role.role, mode, role.model]) or 0
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = launcher.reconcile_role_models_command(config, config_path=CONFIG, roles=["director"], keep="configured",
                                                      runner=runner, print_func=print)
    result["configured_live"] = {"code": code, "printed": printed.getvalue().splitlines(), "restarts": restarts, "models": models()}
elif case == "upgrade_flags":
    # Through switchyard's own parser and dispatch, as an operator types it.
    launcher._resolve_switchyard_project = lambda project: SimpleNamespace(slug=project, config_path=CONFIG)
    launcher._load_switchyard_project_config_for_command = lambda entry, argv: launcher.load_project_config(PROJECT, entry.config_path)
    launcher.upgrade_project_command = lambda cfg, **kwargs: upgrade(cfg, role_models=kwargs["role_models"]) and 0
    with contextlib.redirect_stdout(io.StringIO()):
        launcher.switchyard_main(["upgrade", PROJECT, "--role-models", "running"])
    result["running"] = dict(upgrade_seen)
    result["running_models"] = models(result["running"]["config"])
    build()
    result["configured"] = upgrade(launcher.load_project_config(PROJECT, CONFIG), role_models="configured")
    result["configured_models"] = models(result["configured"]["config"])
elif case == "after_phases":
    # The whole command, its phases stood in for: what they do to the configuration is what is checked after them.
    def phases(rewrite=None, runtime=None):
        build()
        cfg = launcher.load_project_config(PROJECT, CONFIG)
        launcher.UpgradeSourcePinned = launcher.UpgradeStateReady = SimpleNamespace
        launcher.UpgradeToolingStaged = launcher.UpgradeIdentitiesDone = SimpleNamespace
        launcher._pin_upgrade_source = lambda *_a, **_k: SimpleNamespace(
            desktop_choice=None, deploy_ref_chosen=False, source_repo=None, commit_git_dir=None, deploy_ref="x")
        launcher._recover_upgrade_state = lambda c, **_k: SimpleNamespace(source_repo=None, effective_source_repo=tree, config=c, cutover=None,
                                                                   document=None)
        def refresh(c, **_k):
            raw = json.loads(CONFIG.read_text())
            for entry in raw["roles"]:
                if rewrite and entry["role"] in rewrite:
                    entry["model"] = rewrite[entry["role"]]
                if runtime and entry["role"] in runtime:
                    entry["cli"], entry["model"] = [runtime[entry["role"]]], "gpt-5.6-sol"
            CONFIG.write_text(json.dumps(raw) + "\n")
            return c
        launcher._refresh_upgrade_artifacts = refresh
        launcher._stage_upgrade_tooling = lambda *_a, **_k: SimpleNamespace(trusted_release_root=None, publication_detail="")
        launcher._upgrade_identities_and_accounts = lambda c, **_k: SimpleNamespace(config=c, release_report_config=c)
        launcher._finish_upgrade = lambda *_a, **_k: 0
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = launcher.upgrade_project_command(cfg, config_path=CONFIG, role_models="running", runner=runner, print_func=print)
        return {"code": code, "printed": [l for l in printed.getvalue().splitlines() if "upgrade changed" in l]}
    result["clean"] = phases()
    result["template_back"] = phases(rewrite={"director": OLD, "ops": OLD})
    result["runtime_changed"] = phases(runtime={"audit": "codex"})
else:
    raise SystemExit(f"unknown case {case}")
print("RESULT " + json.dumps(result))
'''


def run_child(tree: Path, case: str) -> dict:
    stubs = Path(tempfile.mkdtemp(prefix="syrd579-stubs-"))
    for name in ("claude", "codex", "agy", "konsole", "hermes", "gemini", "tmux", "sudo", "pkexec", "systemctl"):
        stub = stubs / name
        stub.write_text("#!/bin/sh\necho \"refused: $0 must not run in this test\" >&2\nexit 97\n")
        stub.chmod(0o755)
    with tempfile.TemporaryDirectory(prefix="syrd579-") as scratch:
        env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(Path(scratch) / "home"), "LANG": "C.UTF-8",
               "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("LOGNAME", "")}
        proc = subprocess.run([sys.executable, "-c", CHILD, str(tree), scratch, case],
                              capture_output=True, text=True, env=env, timeout=300)
    found = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    check(proc.returncode == 0 and found, f"{case} on {tree.name}: the child reported: {proc.stdout[-2000:]}{proc.stderr[-3000:]}")
    return json.loads(found[-1].removeprefix("RESULT "))


def before_tree(scratch: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", BEFORE], capture_output=True, check=True).stdout
    target = scratch / "before"
    target.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as handle:
        handle.extractall(target, filter="data")
    return target


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd579-before-") as scratch:
        before = run_child(before_tree(Path(scratch)), "before")
    resumed = "claude --resume c3 --model claude-opus-5 --effort high"
    check(before["commands_before"]["ops"].startswith(resumed)
          and before["commands_before"]["director"].startswith("claude --resume a1 --model claude-opus-5"),
          f"main: ops resumes its conversation with --model claude-opus-5, although it last ran claude-opus-5-5: {before['commands_before']}")
    check(before["launch"]["code"] == "past the gate" and before["upgrade"]["reached"] == ["pin"],
          f"main: a start and an upgrade both go on over the three roles that chose claude-opus-5-5: {before['launch']} {before['upgrade']}")

    drift = run_child(ROOT, "drift")
    check(drift["drift"] == [
        "director: model claude-opus-5 is configured, its session last ran claude-opus-5-5; effort high is configured, "
        "its session last ran medium (last turn 2026-10-10T10:30:00.000Z)",
        "audit: model claude-opus-5 is configured, its session last ran claude-opus-5-5 (last turn 2026-10-10T10:30:00.000Z)",
        "ops: model claude-opus-5 is configured, its session last ran claude-opus-5-5 (last turn 2026-10-10T10:30:00.000Z)",
    ], f"drift: three roles, from their own last turns, past the synthetic and subagent turns after them; research and main do not count: {drift['drift']}")
    launch = drift["launch"]
    check(launch["code"] == 1 and launch["started"] == []
          and "team-launcher: refusing to launch otto: the flags would replace what those roles chose. Nothing was started. "
              "Say which each keeps:" in launch["printed"],
          f"start refuses before anything starts: {launch}")
    check(drift["launch_ops_live"]["code"] == 1 and not any("ops:" in line for line in drift["launch_ops_live"]["printed"])
          and drift["reload_ops_live"]["code"] == 1 and any("not restarting ops:" in line for line in drift["reload_ops_live"]["printed"]),
          f"a start leaves a running ops alone and names only what it would start; a reload restarts ops, so it names it: "
          f"{drift['launch_ops_live']} {drift['reload_ops_live']}")
    check(drift["upgrade"]["code"] == 1 and drift["upgrade"]["reached"] == []
          and "switchyard: refusing to upgrade otto: restarting those roles would replace what they chose. Nothing was stopped "
              "or written. Re-run with --role-models running to keep what each ran last, or --role-models configured to keep "
              "the configuration, or first:" in drift["upgrade"]["printed"],
          f"upgrade refuses before its first phase: {drift['upgrade']}")
    check(drift["upgrade_dry"]["reached"] == ["pin"]
          and any(line.startswith("switchyard: an upgrade of otto would refuse here") for line in drift["upgrade_dry"]["printed"])
          and sum(line.startswith(("switchyard: director:", "switchyard: audit:", "switchyard: ops:")) for line in drift["upgrade_dry"]["printed"]) == 3,
          f"a dry run lists each role's change and what apply would do, and previews the rest: {drift['upgrade_dry']}")
    check(drift["models_untouched"] == {name: ["claude-opus-5", "high", False] for name in ("director", "audit", "ops", "research")}
          | {"main": ["gpt-5.6-sol", "high", False]},
          f"refusing wrote nothing: {drift['models_untouched']}")

    for keep in ("running", "configured"):
        done = run_child(ROOT, f"reconcile_{keep}")
        new = "claude-opus-5-5" if keep == "running" else "claude-opus-5"
        check(done["reconcile_code"] == 0 and done["remedy"] == f"reconcile-role-models otto --keep {keep}"
              and done["started_by_reconcile"] == [],
              f"{keep}: the remedy the refusal printed runs through switchyard's own parser, and starts nothing that was down: {done}")
        check(done["models"] == {"director": [new, "medium" if keep == "running" else "high", True], "audit": [new, "high", True],
                                 "ops": [new, "high", True], "research": ["claude-opus-5", "high", False], "main": ["gpt-5.6-sol", "high", False]}
              and done["artifact"]["role_models"] == {"director": new, "audit": new, "ops": new, "research": "claude-opus-5"},
              f"{keep}: the three roles that drifted are recorded, with when, in the configuration and the project artifact: "
              f"{done['models']} {done['artifact']}")
        effort = "medium" if keep == "running" else "high"
        check(done["commands_after"] == {
            "director": f"claude --resume a1 --model {new} --effort {effort}", "audit": f"claude --resume b2 --model {new} --effort high",
            "ops": f"claude --resume c3 --model {new} --effort high",
            "research": "claude --model claude-opus-5 --effort high --dangerously-skip-permissions"},
              f"{keep}: the next start resumes each conversation on the kept model, and research, whose session is missing, "
              f"starts fresh on its own: {done['commands_after']}")
        check(done["drift_after"] == [] and done["launch_after"]["code"] == "past the gate"
              and done["upgrade_after"]["reached"] == ["pin"] and done["upgrade_after"]["printed"] == []
              and done["again_code"] == 0 and done["again_printed"] == [
                  "switchyard: every named role of otto ran its configured model last; nothing to reconcile"],
              f"{keep}: then a start and an upgrade go on, and running it again finds nothing: {done['launch_after']} {done['again_printed']}")
        check(done["drift_later_pick"] == [f"audit: model {new} is configured, its session last ran claude-sonnet-5-5 "
                                           "(last turn 2099-01-01T00:00:00.000Z)"],
              f"{keep}: a pick made after the choice was recorded counts again: {done['drift_later_pick']}")
        check({role: done["projected"][role] for role in ("director", "audit", "ops", "research")}
              == {role: done["models"][role] for role in ("director", "audit", "ops", "research")},
              f"{keep}: a workflow projection -- finish-upgrade's, adopt-workflow's, every apply's -- carries model, effort and "
              f"when they were chosen: {done['projected']}")

    live = run_child(ROOT, "reconcile_live")["live"]
    check(live["dry"]["restarts"] == [] and live["dry"]["models"]["ops"] == ["claude-opus-5", "high", False]
          and "switchyard: would restart ops on claude-opus-5-5; its recorded conversation resumes where it still exists" in live["dry"]["printed"]
          and "switchyard: would restart director on claude-opus-5-5; its recorded conversation resumes where it still exists" in live["dry"]["printed"]
          and not any("research" in line for line in live["dry"]["printed"]),
          f"a dry run names what it would record and restart, and changes nothing: {live['dry']}")
    check(live["running"]["code"] == 0 and live["running"]["restarts"] == [["ops", "reload", "claude-opus-5-5", "c3"]]
          and "switchyard: director is busy, so it was left running; it takes claude-opus-5-5 at its next restart, "
              "or run this again when it is idle" in live["running"]["printed"],
          f"only the live, idle role whose process runs another model restarts, by the ordinary reload, which resumes its "
          f"recorded session; busy director is left and named, audit is down, research had nothing to change: {live['running']}")
    check(live["research"]["restarts"] == [["research", "reload", "claude-opus-5-5", "d4"]]
          and live["research"]["models"]["research"] == ["claude-opus-5-5", "high", True],
          f"--model names a model outright for a role with no transcript to read: {live['research']}")
    check(live["stays_old"] == {"code": 1, "printed": [
        "switchyard: director did not come back on claude-opus-5-5 (exit 0, running claude-opus-5)"]},
          f"a restart that comes back on another model is a failure: {live['stays_old']}")
    configured = run_child(ROOT, "configured_live")["configured_live"]
    check(configured["code"] == 0 and configured["restarts"] == [["director", "reload", "claude-opus-5"]]
          and configured["models"]["director"] == ["claude-opus-5", "high", True] and configured["models"]["audit"][2] is False,
          f"--keep configured restarts a live session that picked another model in place, so it is back on its "
          f"configuration; --role keeps it to that role: {configured}")

    flags = run_child(ROOT, "upgrade_flags")
    check(flags["running"]["reached"] == ["pin"] and flags["running_models"]["ops"] == ["claude-opus-5-5", "high", True]
          and flags["running_models"]["director"] == ["claude-opus-5-5", "medium", True]
          and flags["configured"]["reached"] == ["pin"] and flags["configured_models"]["ops"] == ["claude-opus-5", "high", True],
          f"--role-models running records what each ran and goes on; configured records the configuration as chosen: {flags}")
    phases = run_child(ROOT, "after_phases")
    check(phases["clean"] == {"code": 0, "printed": []} and phases["runtime_changed"] == {"code": 0, "printed": []}
          and phases["template_back"] == {"code": 1, "printed": [
              "switchyard: the upgrade changed director's model from claude-opus-5-5 to claude-opus-5, which nothing asked it to do",
              "switchyard: the upgrade changed ops's model from claude-opus-5-5 to claude-opus-5, which nothing asked it to do"]},
          f"after its phases the upgrade fails on a role model one of them put back, and on nothing else: {phases}")
    in_process()
    print(f"role_model_selection_test: {CHECKS} checks ok")
    return 0


def in_process() -> None:
    sys.path.insert(0, str(ROOT))
    from scripts import role_model_selection as rms

    def turn(model: str, at: str, **extra: object) -> str:
        return json.dumps({"type": "assistant", "isSidechain": False, "effort": "high", "timestamp": at,
                           "message": {"model": model}, **extra}) + "\n"

    with tempfile.TemporaryDirectory(prefix="syrd579-turns-") as scratch:
        path = Path(scratch) / "t.jsonl"
        path.write_text(turn("claude-opus-5", "2026-10-10T10:00:00Z") + json.dumps({"type": "user", "pad": "x" * 5000}) + "\n"
                        + turn("claude-opus-5-5", "2026-10-10T11:00:00Z") + json.dumps({"type": "user"}) + "\n")
        whole = rms.last_turn(path)
        chunk, rms._CHUNK = rms._CHUNK, 7
        try:
            small = rms.last_turn(path)
        finally:
            rms._CHUNK = chunk
        check(whole == small == rms.LastTurn("claude-opus-5-5", "high", "2026-10-10T11:00:00Z"),
              f"the last turn, read in pieces smaller than a line: {whole} {small}")
        after = rms._parse_time("2026-10-10T11:00:00Z")
        check(rms.last_turn(path, after=after) is None
              and rms.last_turn(path, after=rms._parse_time("2026-10-10T10:59:59.999Z")) == whole,
              "a turn counts only when it ran after the recorded choice")
        limit, rms.TRANSCRIPT_READ_LIMIT = rms.TRANSCRIPT_READ_LIMIT, 8
        try:
            check(rms.last_turn(path) is None, "a transcript is read back no further than the limit")
        finally:
            rms.TRANSCRIPT_READ_LIMIT = limit
        link = Path(scratch) / "link.jsonl"
        link.symlink_to(path)
        check(rms.last_turn(link) is None and rms.last_turn(Path(scratch) / "absent.jsonl") is None,
              "a link is not followed, and a missing transcript is no turn")
        path.write_text(turn("claude-opus-5-5 && echo", "2026-10-10T11:00:00Z") + turn("$(id)", "2026-10-10T11:00:00Z"))
        check(rms.last_turn(path) is None, "nothing but a model identifier is taken from a transcript")
    check(rms.same_model("opus", "claude-opus-5-5") and rms.same_model("opus[1m]", "claude-opus-5-5")
          and rms.same_model("claude-opus-5-5[1m]", "claude-opus-5-5") and not rms.same_model("sonnet", "claude-opus-5-5")
          and not rms.same_model("claude-opus-5", "claude-opus-5-5"),
          "an alias names its family; a versioned name names one model")


if __name__ == "__main__":
    raise SystemExit(main())
