#!/usr/bin/env python3
"""SYRD-634: GitHub Copilot CLI is a runtime everywhere one is named, and the other four are as they were.

Offline, and needs no Copilot: every Copilot fact used here is a recording of
the real 1.0.101 CLI in tests/fixtures/copilot-1.0.101 (SOURCE.md says how each
was made). The disposable run of the real CLI is copilot_end_to_end_test; the
board's own SQL boundaries are copilot_runtime_board_test.

* every allowlist, menu, parser and table names `copilot`, and the other
  runtimes' entries are unchanged;
* argv comes from the installed CLI's own --help: `--model` and
  `--reasoning-effort <level>` only when it lists them, an honest line when it
  does not, and never another runtime's flags;
* screens, hooks, the hook installer, trust, resume, the sign-in probe and
  host promotion each behave as the recordings say Copilot does.
"""

from __future__ import annotations

import contextlib
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

for _key in [key for key in os.environ
             if key.startswith(("TICKET_BOARD_", "PGU_", "COPILOT_")) or key in ("TMUX", "TMUX_PANE")]:
    del os.environ[_key]
ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

from scripts import agent_cli_promotion, copilot_runtime, model_validation, provider_auth_status  # noqa: E402
from scripts import role_command, runtime_arguments  # noqa: E402
from scripts import switchyard_parsers  # noqa: E402
from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board import board_skill, notification_session_clear, provider_prompt  # noqa: E402
from scripts.ticket_board import runtime_catalog, session_context, workflow_config  # noqa: E402
from scripts.ticket_board import pane_activity_gate, pane_state  # noqa: E402
from scripts import provider_screen  # noqa: E402
from provider_store_fixture import transcript_for  # SYRD-638: a session as its provider files it

FIXTURES = ROOT / "tests" / "fixtures" / "copilot-1.0.101"
HOOK = ROOT / "scripts" / "ticket-board-pane-idle-hook"
INSTALLER = ROOT / "scripts" / "ticket-board-install-pane-hooks"
PROMOTER = ROOT / "scripts" / "switchyard-promote-agent-cli"
OTHERS = ("agy", "claude", "codex", "hermes")
TARGET = "pgu-main:0.0"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def load_script(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


HOOK_MODULE = load_script(HOOK, "pane_idle_hook_634")
PROMOTER_MODULE = load_script(PROMOTER, "promote_agent_cli_634")
sys.path.append(str(ROOT / "scripts"))  # the installer imports ticket_board as its own sibling
INSTALLER_MODULE = load_script(INSTALLER, "install_pane_hooks_634")
HELP = (FIXTURES / "help.txt").read_text(encoding="utf-8")
CAPABILITIES = copilot_runtime.parse_help(HELP, source="fixture --help")


def screen(name: str) -> str:
    return (FIXTURES / "screens" / f"{name}.txt").read_text(encoding="utf-8")


def hook_events(name: str) -> list[tuple[str, dict]]:
    lines = (FIXTURES / "hooks" / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    return [(entry["event"], entry["payload"]) for entry in map(json.loads, lines)]


@contextlib.contextmanager
def capabilities(found):
    saved = copilot_runtime.installed_capabilities
    copilot_runtime.installed_capabilities = lambda *_args, **_kwargs: found
    try:
        yield
    finally:
        copilot_runtime.installed_capabilities = saved


def role(cli: str, *, name: str = "main", **fields):
    raw = {"role": name, "cli": [cli], "model": "m-1", "effort": "high", "yolo": True, **fields}
    return launcher._role_from_json("pgu", raw, base=Path("/tmp"), default_workdir=Path("/tmp"))


def argv_tail(built: list[str], cli: str) -> list[str]:
    return built[built.index(cli):]


def build(role_config, *, resume: bool = False, session_dir: Path | None = None) -> tuple[list[str], str]:
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            built = role_command.cli_command_for_role(role_config, session_dir=session_dir or Path("/tmp/none"),
                                                      resume=resume)
    except SystemExit as exc:  # a refusal is an answer, and a check names it
        return ["<refused>"], f"{err.getvalue()}{exc}"
    return argv_tail(built, role_config.cli[0]), err.getvalue()


def test_every_allowlist_names_copilot_and_keeps_the_others() -> None:
    five = {*OTHERS, "copilot"}
    check(launcher.SUPPORTED_CONFIG_CLI_NAMES == ("agy", "claude", "codex", "hermes", "copilot"),
          launcher.SUPPORTED_CONFIG_CLI_NAMES)
    check(set(launcher.KNOWN_LIVE_CLI_NAMES) == five, launcher.KNOWN_LIVE_CLI_NAMES)
    check(workflow_config.RUNTIMES == five, workflow_config.RUNTIMES)
    check([c.value for c in runtime_catalog.RUNTIMES] == ["claude", "codex", "agy", "hermes", "copilot"],
          runtime_catalog.RUNTIMES)
    check(launcher.SUPPORTED_NEW_PROJECT_CLIS == ("claude", "codex", "agy", "hermes", "copilot"),
          launcher.SUPPORTED_NEW_PROJECT_CLIS)
    check(PROMOTER_MODULE.PROMOTABLE_CLIS == ("agy", "claude", "codex", "hermes", "copilot")
          and PROMOTER_MODULE.VERSION_PREFIXES == {"copilot": "GitHub Copilot CLI "},
          "the root promoter's own list takes copilot, and only by its version line")
    check(notification_session_clear.SESSION_CLEAR_COMMANDS == {r: "/clear" for r in five},
          notification_session_clear.SESSION_CLEAR_COMMANDS)
    check(session_context.RESUME_COMMANDS == {"claude": "/resume {session_id}", "copilot": "/resume {session_id}"},
          session_context.RESUME_COMMANDS)
    check(dict(board_skill.SKILL_ADAPTERS)["copilot"] == Path(".copilot/skills")
          and {r for r, _ in board_skill.SKILL_ADAPTERS} == five, board_skill.SKILL_ADAPTERS)
    check(launcher.FIRST_RUN_AUTH_STATUS_COMMANDS["copilot"] == ["copilot", "--no-auto-update"]
          and launcher.FIRST_RUN_AUTH_LOGIN_COMMANDS["copilot"] == ["copilot", "login"]
          and launcher.AGENT_CLI_INSTALL_COMMANDS["copilot"] == "curl -fsSL https://gh.io/copilot-install | bash",
          "first run probes, signs in and names the install for copilot")
    check(launcher.FIRST_RUN_TRUST_CLIS == frozenset({"agy", "claude", "codex", "copilot"}), launcher.FIRST_RUN_TRUST_CLIS)
    check(launcher.ROLE_CREDENTIAL_ARTIFACTS["copilot"] == (), "no Copilot credential crosses into a role account")
    for build_parser in (switchyard_parsers._build_switchyard_add_role_parser,
                         switchyard_parsers._build_switchyard_set_role_runtime_parser):
        parsed = build_parser().parse_args(["proj", "main", "--cli", "copilot"])
        check(parsed.cli == "copilot", f"{build_parser.__name__} takes --cli copilot")
    refused = []
    for runtime in (*OTHERS, "copilot", "gh-copilot"):
        document = _example_document()
        next(entry for entry in document["roles"] if entry["name"] == "main")["runtime"] = runtime
        try:
            workflow_config.validate(document, project="pgu")
        except Exception as exc:  # noqa: BLE001 - the refusal is the answer
            refused.append((runtime, str(exc)))
    check([runtime for runtime, _ in refused] == ["gh-copilot"] and "invalid runtime/target: main" in refused[0][1],
          f"workflow validation accepts the five runtimes and nothing else: {refused}")


def _example_document() -> dict:
    document = json.loads((ROOT / "examples" / "workflows" / "inspection.json").read_text(encoding="utf-8"))
    document["project"] = "pgu"
    for entry in document["roles"]:
        if entry.get("target"):
            entry["target"] = entry["target"].replace("cerulean-", "pgu-", 1)
    return document


def test_help_parsing_and_the_recorded_catalog() -> None:
    check(CAPABILITIES.model_flag and CAPABILITIES.resume_flag, CAPABILITIES)
    check(CAPABILITIES.effort_levels == ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
          CAPABILITIES.effort_levels)
    catalog = copilot_runtime.parse_model_catalog((FIXTURES / "help-config.txt").read_text(encoding="utf-8"))
    check(catalog[:2] == ("claude-sonnet-5.5", "claude-sonnet-5") and len(catalog) == 31 and "kimi-k3" in catalog,
          catalog)
    recorded = runtime_catalog.model_catalog("copilot")
    check(recorded.provenance == runtime_catalog.PROVENANCE_RECORDED and not recorded.enumerable
          and recorded.note.startswith("UNVERIFIED"), f"Copilot's list is recorded and never claimed live: {recorded}")
    check({c.value for c in runtime_catalog.RECORDED_MODELS["copilot"]} <= set(catalog),
          "every recorded Copilot model is in 1.0.101's own catalog")
    check(runtime_catalog.runtime_takes_effort("copilot"), "an effort level is asked for a copilot role")
    check(copilot_runtime.parse_help("Usage: copilot\n").effort_levels == ()
          and not copilot_runtime.parse_help("Usage: copilot\n").model_flag, "a help without the flags says so")


def test_argv_comes_from_the_installed_help_and_carries_no_foreign_flag() -> None:
    with capabilities(CAPABILITIES):
        tail, said = build(role("copilot"))
    check(tail == ["copilot", "--model", "m-1", "--reasoning-effort", "high", "--allow-all"] and not said,
          f"copilot's own flags, from its help: {tail} {said!r}")
    foreign = {flag for cli in OTHERS for flag in role_command.YOLO_ARGS_BY_CLI[cli]}
    foreign |= {"--effort", "-c", "--reasoning", "--accept-hooks", "--pass-session-id", "-m", "--conversation"}
    check(not foreign & set(tail), f"no other runtime's flag: {foreign & set(tail)}")
    with capabilities(CAPABILITIES):
        tail, said = build(role("copilot", effort="ultra"))
    check(tail == ["copilot", "--model", "m-1", "--allow-all"]
          and "effort 'ultra' is not passed" in said and "none, minimal, low, medium, high, xhigh, max" in said,
          f"an effort this copilot does not list is omitted, and said: {tail} {said!r}")
    with capabilities(None):
        tail, said = build(role("copilot"))
    check(tail == ["copilot", "--allow-all"]
          and "model 'm-1' is not passed: the installed copilot's --help could not be read" in said
          and "effort 'high' is not passed: the installed copilot's --help could not be read" in said,
          f"an unreadable help passes neither --model nor --reasoning-effort, and says why for each: {tail} {said!r}")
    check(copilot_runtime.model_args("m-1", "--model", None) == ([], "model 'm-1' is not passed: the installed "
          "copilot's --help could not be read, so whether it takes --model is unknown")
          and copilot_runtime.model_args("m-1", "--model", CAPABILITIES) == (["--model", "m-1"], "")
          and copilot_runtime.model_args("", "--model", None) == ([], ""),
          "the model rule on its own: unknown help fails closed, a listed --model passes, no model says nothing")
    bare = copilot_runtime.parse_help("Usage: copilot [options]\n", source="old --help")
    with capabilities(bare):
        tail, said = build(role("copilot"))
    check(tail == ["copilot", "--allow-all"] and "has no --model" in said and "has no --reasoning-effort" in said,
          f"a copilot without --model or --reasoning-effort gets neither: {tail} {said!r}")
    with capabilities(CAPABILITIES):
        tail, _ = build(role("copilot", model="", effort="", yolo=False))
    check(tail == ["copilot"], f"nothing configured, nothing passed: {tail}")
    # The other four, exactly as they were built before Copilot existed.
    expected = {
        "agy": ["agy", "--model", "m-1", "--dangerously-skip-permissions"],
        "claude": ["claude", "--model", "m-1", "--effort", "high", "--dangerously-skip-permissions"],
        "codex": ["codex", "--model", "m-1", "-c", 'model_reasoning_effort="high"',
                  "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
        "hermes": ["hermes", "-m", "m-1", "--yolo", "--accept-hooks", "--pass-session-id"],
    }
    calls = []
    with capabilities(None):
        saved = copilot_runtime.installed_capabilities
        copilot_runtime.installed_capabilities = lambda *a, **k: calls.append(a) or None
        try:
            for cli, want in expected.items():
                tail, said = build(role(cli))
                check(tail == want and not said, f"{cli} is built as before: {tail} {said!r}")
        finally:
            copilot_runtime.installed_capabilities = saved
    check(calls == [], f"no other runtime asks copilot's help: {calls}")


def test_resume_argv_and_the_preflight() -> None:
    with tempfile.TemporaryDirectory(prefix="cp634-resume.") as tmp:
        home = Path(tmp)
        sessions = home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
        sessions.mkdir(parents=True)
        sid = "46ffe999-8bcb-4d01-aaa1-60a34e9d0954"
        (sessions / "pgu-main_0.0.json").write_text(json.dumps({"target": TARGET, "session_id": sid}))
        copilot_role = role("copilot")
        with capabilities(CAPABILITIES):
            tail, _ = build(copilot_role, resume=True, session_dir=sessions)
        check(tail[:3] == ["copilot", "--resume", sid], f"a resume names the recorded session: {tail}")
        ok, why = launcher._resume_preflight_allows_attempt(copilot_role, sid, session_dir=sessions)
        check(not ok and f"{home}/.copilot/session-state/{sid}/events.jsonl" in why and "starting fresh" in why,
              f"a session Copilot does not hold is reported, not passed: {why}")
        transcript = home / ".copilot" / "session-state" / sid / "events.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(transcript_for(copilot_role))  # SYRD-638: as Copilot files it
        ok, why = launcher._resume_preflight_allows_attempt(copilot_role, sid, session_dir=sessions)
        check(ok and not why, f"Copilot's own transcript lets the resume go ahead: {why}")
        moved = replace(copilot_role, env={**copilot_role.env, "COPILOT_HOME": str(home / "elsewhere")})
        ok, why = launcher._resume_preflight_allows_attempt(moved, sid, session_dir=sessions)
        check(not ok and str(home / "elsewhere" / "session-state") in why, f"COPILOT_HOME is where it looks: {why}")
        outside = home / ".copilot" / "x" / "events.jsonl"
        outside.parent.mkdir(parents=True)
        outside.write_text("{}\n")
        for bad in ("../x", "", "..", "."):
            check(not copilot_runtime.session_store_exists(home, bad)[0], f"no id names a file outside the store: {bad!r}")


def test_screens_are_named_from_the_recordings() -> None:
    expected = {"trust": "trust", "ready": "ready", "reply": "ready", "cleared": "ready", "working": "working",
                "quota-retrying": "working", "permission": "permission", "unauthenticated": "login",
                "not-logged-in-prompt": "login", "login-account": "account_picker", "resume-unknown-id": "fatal"}
    for name, state in expected.items():
        check(copilot_runtime.classify_screen(screen(name)) == state, f"{name} reads as {state}")
    check(copilot_runtime.classify_screen("") == "unknown", "an empty screen is not ready")
    kinds = {name: provider_prompt.open_prompt(screen(name).splitlines()) for name in ("trust", "permission", "login-account")}
    check(getattr(kinds["trust"], "kind", None) == "folder_trust" and len(kinds["trust"].options) == 3, kinds["trust"])
    check(getattr(kinds["login-account"], "kind", None) == "sign_in", kinds["login-account"])
    check(kinds["permission"] is not None and kinds["permission"].options[0] == "Yes", kinds["permission"])
    for name in ("ready", "working", "reply", "unauthenticated"):
        check(provider_prompt.open_prompt(screen(name).splitlines()) is None, f"{name} asks no menu question")
    check(provider_screen.provider_screen_is_at_ordinary_prompt(screen("ready")), "ready is the ordinary prompt")
    check(not provider_screen.provider_screen_is_at_ordinary_prompt(screen("unauthenticated")),
          "a box with no sign-in under it is not a finished first run")


class Hooks:
    """The real hook program, fed what Copilot 1.0.101 sent, as the installer wires each event."""

    def __init__(self, root: Path, board_port: int | None = None) -> None:
        self.root = root
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "TICKET_BOARD_PROJECT": "pgu",
                    "TICKET_BOARD_PANE_TARGET": TARGET, "GH_TOKEN": "gho_not_a_real_token_634",
                    "COPILOT_PROVIDER_API_KEY": "sk-not-a-real-key-634",
                    "TICKET_BOARD_URL": f"http://127.0.0.1:{board_port or 9}"}
        self.outputs: list[str] = []

    def fire(self, event: str, payload: dict) -> str:
        state = INSTALLER_MODULE.COPILOT_HOOK_EVENTS[event]
        argv = [sys.executable, "-B", str(HOOK), state, "--source", f"copilot.{event}",
                "--state-dir", str(self.root / "state"), "--session-dir", str(self.root / "sessions")]
        if event == "sessionStart":
            argv.append("--record-session")
        done = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True, env=self.env, timeout=60)
        check(done.returncode == 0, f"{event} ran: {done.stderr}")
        check(isinstance(json.loads(done.stdout), dict) and len(done.stdout.strip().splitlines()) == 1,
              f"{event} printed one JSON document: {done.stdout!r}")
        self.outputs.append(done.stdout)
        return done.stdout

    def replay(self, name: str) -> None:
        for event, payload in hook_events(name):
            if event in INSTALLER_MODULE.COPILOT_HOOK_EVENTS:
                self.fire(event, payload)

    def state(self) -> dict:
        return json.loads((self.root / "state" / "pgu-main_0.0.json").read_text(encoding="utf-8"))

    def record(self) -> dict:
        return json.loads((self.root / "sessions" / "pgu-main_0.0.json").read_text(encoding="utf-8"))

    def files(self) -> str:
        return "".join(path.read_text(encoding="utf-8") for path in self.root.rglob("*.json*"))


def board_stub() -> http.server.ThreadingHTTPServer:
    class Board(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({"workflow": {"stages": [{"name": "in_progress", "terminal": False,
                                                        "kind": "implementation", "owners": ["main"]}]},
                               "tickets": [{"id": "PGU-7", "title": "Seven", "state": "in_progress",
                                            "assignee": "main"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Board)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_hooks_drive_pane_state_from_the_recorded_events() -> None:
    with tempfile.TemporaryDirectory(prefix="cp634-hooks.") as tmp:
        hooks = Hooks(Path(tmp))
        events = hook_events("turn")
        hooks.fire(*events[0])
        start = hooks.state()
        check(start["state"] == "idle" and start["last_session_start"]["source"] == "startup"
              and hooks.record()["session_id"] == events[0][1]["sessionId"],
              f"a new session is a startup, recorded: {start}")
        hooks.fire(*events[1])
        busy = hooks.state()
        check(busy["state"] == "busy" and busy["source"] == "copilot.userPromptSubmitted" and busy.get("turn_started_at"),
              f"a prompt starts a turn: {busy}")
        check(pane_state.TURN_START_EVENTS >= {"userPromptSubmitted"}
              and "userPromptSubmitted" in HOOK_MODULE.TURN_START_EVENTS, "both turn-start lists know it")
        hooks.fire(*events[2])
        end = hooks.state()
        check(end["state"] == "idle" and end["source"] in pane_activity_gate.IDLE_TURN_END_SOURCES
              and (end.get("last_checkout") or {}).get("cwd") == events[2][1]["cwd"],
              f"agentStop ends the turn idle, with the checkout it ended in: {end}")
        hooks.fire("notification", {"sessionId": events[2][1]["sessionId"], "notification_type": "agent_idle",
                                    "message": "m"})
        check(hooks.state()["state"] == "idle" and hooks.state()["source"] == "copilot.agentStop",
              f"a notification that is not a permission prompt changes nothing: {hooks.state()}")
        hooks.fire(*events[3])
        check(hooks.state()["source"] == "copilot.sessionEnd" and hooks.state()["state"] == "idle", hooks.state())
        check("gho_not_a_real_token_634" not in hooks.files() + "".join(hooks.outputs)
              and "sk-not-a-real-key-634" not in hooks.files() + "".join(hooks.outputs),
              "no token from the hook's environment reaches a state file, a record or Copilot")

    with tempfile.TemporaryDirectory(prefix="cp634-hooks.") as tmp:
        hooks = Hooks(Path(tmp))
        hooks.replay("permission-then-escape")
        blocked = hooks.state()
        check(blocked["state"] == "idle" and blocked["source"] == "copilot.sessionEnd",
              f"the session's end closes it: {blocked}")
        hooks = Hooks(Path(tmp) / "again")
        for event, payload in hook_events("permission-then-escape")[:5]:
            if event in INSTALLER_MODULE.COPILOT_HOOK_EVENTS:
                hooks.fire(event, payload)
        blocked = hooks.state()
        check(blocked["state"] == "blocked" and blocked["source"] in pane_activity_gate.PERMISSION_PROMPT_BLOCK_SOURCES,
              f"a permission prompt blocks the pane, as one the board knows: {blocked}")
        hooks.fire("notification", {"sessionId": "x", "notification_type": "idle_prompt", "message": "m"})
        check(hooks.state()["state"] == "blocked", "any other notification is news, not state")

    with tempfile.TemporaryDirectory(prefix="cp634-hooks.") as tmp:
        hooks = Hooks(Path(tmp))
        hooks.replay("quota-429")
        limited = hooks.state()
        check(limited["state"] == "busy" and limited["source"] == "copilot.errorOccurred.capacity",
              f"a retried 429 is capacity, and busy: {limited}")
        hooks.fire("errorOccurred", {"sessionId": "x", "recoverable": True, "errorContext": "model_call",
                                     "error": {"message": "socket hang up"}})
        check(hooks.state()["source"] == "copilot.errorOccurred.retrying" and hooks.state()["state"] == "busy",
              hooks.state())
        hooks.fire("errorOccurred", {"sessionId": "x", "recoverable": False, "errorContext": "model_call",
                                     "error": {"message": "fatal"}})
        check(hooks.state()["source"] == "copilot.errorOccurred" and hooks.state()["state"] == "idle"
              and hooks.state()["source"] not in pane_activity_gate.TRUSTED_IDLE_SOURCES,
              f"an error Copilot gives up on is an idle the gate still checks: {hooks.state()}")

    # Outside a managed pane the hook writes nothing and still answers Copilot.
    with tempfile.TemporaryDirectory(prefix="cp634-hooks.") as tmp:
        env = {"PATH": "/usr/bin:/bin", "HOME": tmp}
        done = subprocess.run([sys.executable, "-B", str(HOOK), "idle", "--source", "copilot.agentStop"],
                              input="{}", capture_output=True, text=True, env=env, timeout=60)
        check(done.returncode == 0 and done.stdout.strip() == "{}" and not list(Path(tmp).iterdir()),
              f"an unmanaged Copilot gets `{{}}` and nothing is written: {done.stdout!r}")


def test_a_clear_is_told_apart_from_a_launch() -> None:
    with tempfile.TemporaryDirectory(prefix="cp634-clear.") as tmp:
        state_dir = Path(tmp)
        same = (4242, 99)
        saved = HOOK_MODULE._pane_root_identity
        try:
            HOOK_MODULE._pane_root_identity = lambda _pid, *_a: same
            first = HOOK_MODULE._copilot_event(state_dir, TARGET, "copilot.sessionStart", "idle",
                                               {"sessionId": "s-1", "source": "new"})
            check(first[2]["source"] == "startup", f"a pane's first new session is its launch: {first}")
            HOOK_MODULE._write_state(state_dir, TARGET, "idle", source="copilot.sessionStart",
                                     session_start={"session_id": "s-1", "source": "startup", "pane_pid": same[0],
                                                    "pane_start_time": same[1]})
            second = HOOK_MODULE._copilot_event(state_dir, TARGET, "copilot.sessionStart", "idle",
                                                {"sessionId": "s-2", "source": "new"})
            check(second[2]["source"] == "clear" and second[2]["copilot_source"] == "new",
                  f"a new session from the same provider is a /clear: {second}")
            again = HOOK_MODULE._copilot_event(state_dir, TARGET, "copilot.sessionStart", "idle",
                                               {"sessionId": "s-1", "source": "new"})
            check(again[2]["source"] == "startup", "the same session announced again is not a clear")
            HOOK_MODULE._pane_root_identity = lambda _pid, *_a: (4243, 100)
            other = HOOK_MODULE._copilot_event(state_dir, TARGET, "copilot.sessionStart", "idle",
                                               {"sessionId": "s-3", "source": "new"})
            check(other[2]["source"] == "startup", f"a restarted provider's new session is a launch: {other}")
            HOOK_MODULE._pane_root_identity = lambda _pid, *_a: None
            unknown = HOOK_MODULE._copilot_event(state_dir, TARGET, "copilot.sessionStart", "idle",
                                                 {"sessionId": "s-4", "source": "new"})
            check(unknown[2]["source"] == "startup", "with no provider identity nothing is called a clear")
            resumed = HOOK_MODULE._copilot_event(state_dir, TARGET, "copilot.sessionStart", "idle",
                                                 {"sessionId": "s-1", "source": "resume"})
            check(resumed[2]["source"] == "resume", "a resume stays a resume")
        finally:
            HOOK_MODULE._pane_root_identity = saved


def test_a_resumed_session_is_told_its_ticket_in_copilots_own_shape() -> None:
    server = board_stub()
    try:
        with tempfile.TemporaryDirectory(prefix="cp634-context.") as tmp:
            hooks = Hooks(Path(tmp), server.server_port)
            events = hook_events("resume-then-clear")
            said = json.loads(hooks.fire(*events[0]))
            check(set(said) == {"additionalContext"} and "PGU-7" in said["additionalContext"],
                  f"a resumed session hears its in-progress ticket as top-level additionalContext: {said}")
            check(hooks.state()["last_session_start"]["source"] == "resume", hooks.state())
        with tempfile.TemporaryDirectory(prefix="cp634-context.") as tmp:
            said = json.loads(Hooks(Path(tmp), server.server_port).fire(*hook_events("turn")[0]))
            check(said == {}, f"a fresh start with no onboarding is told nothing: {said}")
    finally:
        server.shutdown()


def test_the_installer_adds_a_file_of_its_own_and_touches_nothing_else() -> None:
    with tempfile.TemporaryDirectory(prefix="cp634-install.") as tmp:
        home = Path(tmp) / "home"
        hooks_dir = home / ".copilot" / "hooks"
        hooks_dir.mkdir(parents=True)
        mine = hooks_dir / "mine.json"
        mine.write_text('{"version": 1, "hooks": {"agentStop": [{"type": "command", "bash": "echo mine"}]}}\n')
        settings = home / ".copilot" / "settings.json"
        settings.write_text('// owner\n{"disableAllHooks": true, "hooks": {"sessionStart": []}}\n')
        config = home / ".copilot" / "config.json"
        config.write_text('// managed\n{"trustedFolders": ["/w"]}\n')
        before = {path: path.read_bytes() for path in (mine, settings, config)}
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "TICKET_BOARD_PROJECT": "pgu",
               "TICKET_BOARD_PANE_STATE_DIR": str(Path(tmp) / "state"),
               "TICKET_BOARD_PANE_SESSION_DIR": str(Path(tmp) / "sessions")}
        runs = []
        for _ in range(2):
            runs.append(subprocess.run([sys.executable, "-B", str(INSTALLER), "install", "--home", str(home),
                                        "--bin-path", str(home / ".local/bin/ticket-board-pane-idle-hook")],
                                       capture_output=True, text=True, env=env, timeout=120))
        check(all(run.returncode == 0 for run in runs), [run.stderr for run in runs])
        check(all(path.read_bytes() == data for path, data in before.items()),
              "the owner's hook file, settings and config are byte for byte as they were")
        ours = hooks_dir / "ticket-board-pane-state.json"
        check(ours.is_file(), f"the installer wrote Copilot's hook file: {sorted(p.name for p in hooks_dir.iterdir())}")
        document = json.loads(ours.read_text())
        check(document["version"] == 1 and set(document["hooks"]) == set(INSTALLER_MODULE.COPILOT_HOOK_EVENTS),
              document)
        for event, entries in document["hooks"].items():
            check(len(entries) == 1 and entries[0]["type"] == "command" and f"copilot.{event}" in entries[0]["bash"]
                  and str(home / ".local/bin/ticket-board-pane-idle-hook") in entries[0]["bash"],
                  f"{event} runs the installed hook as copilot.{event}: {entries}")
        check("--record-session" in document["hooks"]["sessionStart"][0]["bash"], "sessionStart records the session")
        check(sorted(p.name for p in hooks_dir.iterdir()) == ["mine.json", "ticket-board-pane-state.json"],
              "one file of ours beside the owner's")
        check("disableAllHooks" in runs[0].stderr, f"an owner who disabled hooks is told: {runs[0].stderr}")


def test_folder_trust_is_read_from_copilots_config() -> None:
    with tempfile.TemporaryDirectory(prefix="cp634-trust.") as tmp:
        home = Path(tmp)
        (home / ".copilot").mkdir()
        (home / ".copilot" / "config.json").write_text(
            "// User settings belong in settings.json.\n// This file is managed automatically.\n"
            '{\n  "firstLaunchAt": "x",\n  "trustedFolders": ["/srv/work", "/home/o/repo/"]\n}\n')
        check(copilot_runtime.workdir_is_trusted("/srv/work", home), "a listed folder is trusted")
        check(copilot_runtime.workdir_is_trusted("/home/o/repo/sub", home), "a folder under a listed one is trusted")
        check(not copilot_runtime.workdir_is_trusted("/srv/worker", home), "a name that only starts the same is not")
        check(launcher._workdir_is_trusted("copilot", owner_home=home, workdir=Path("/srv/work")),
              "the launcher asks Copilot's own list")
        check(not copilot_runtime.workdir_is_trusted("/srv/work", home / "nobody"), "no config, no trust")
        check(not launcher._workdir_is_trusted("copilot", owner_home=home, workdir=Path("/srv/other")),
              "and the launcher does not call an unlisted folder trusted")
        from scripts import provider_runtime_state
        before = provider_runtime_state.provider_state_generation("copilot", owner_home=home)
        (home / ".copilot" / "config.json").write_text('{"trustedFolders": ["/srv/work"]}\n')
        after = provider_runtime_state.provider_state_generation("copilot", owner_home=home)
        check(before != after and after == provider_runtime_state.provider_state_generation("copilot", owner_home=home),
              "trusting or untrusting a folder is a new provider generation, and nothing else is")


def test_the_sign_in_probe_reads_the_screen_as_the_owner() -> None:
    calls = []

    def runner(answer: str, installed: int = 0):
        def run(args, **kwargs):
            calls.append(list(args))
            if args[-1].startswith("command -v"):
                return subprocess.CompletedProcess(args, installed, "", "")
            return subprocess.CompletedProcess(args, 0 if answer == "ready" else 1, json.dumps({"state": answer}) + "\n", "")
        return run

    home = Path("/tmp/owner-home")
    status = provider_auth_status._cli_auth_status("copilot", owner_user="o", owner_home=home, runner=runner("ready"))
    probe = calls[0]
    check(status == "authenticated" and probe[-7:] == [sys.executable, "-I", str(ROOT / "scripts/copilot_runtime.py"),
                                                        "screen-status", "--", "copilot", "--no-auto-update"],
          f"the owner runs the screen probe on copilot: {status} {probe}")
    check(provider_auth_status._cli_auth_status("copilot", owner_user="o", owner_home=home, runner=runner("login"))
          == "unauthenticated", "a sign-in screen is unauthenticated")
    check(provider_auth_status._cli_auth_status("copilot", owner_user="o", owner_home=home, runner=runner("exited", 1))
          == "not_installed", "a copilot that is not there is not installed")
    for answer in ("trust", "timeout", "fatal", "account_picker"):
        check(provider_auth_status._cli_auth_status("copilot", owner_user="o", owner_home=home, runner=runner(answer))
              == "unauthenticated", f"{answer} is never taken for signed in")

    def lying(answer: str):
        return lambda args, **_k: subprocess.CompletedProcess(args, 0, json.dumps({"state": answer}) + "\n", "")
    for answer in ("trust", "timeout", "exited"):
        check(not provider_auth_status._cli_auth_probe_passed("copilot", lying(answer)(["x"])),
              f"only the ready screen is signed in, whatever the exit status says: {answer}")


def stand_in(path: Path, startup: str, *, on_model: str = "", answer_after: int = 0, later: str = "",
             later_after: int = 0, seen: Path | None = None) -> Path:
    """A stand-in copilot for the screen probe: draws a recorded screen, maybe a line later, and answers `/model`
    with another recorded screen, only once `answer_after` seconds have passed (as a loading catalog does)."""
    lines = ["#!/bin/sh"]
    if seen is not None:
        lines.append(f"printf '%s %s\\n' \"$COPILOT_ALLOW_ALL\" \"$PWD\" >> {seen}")
    lines.append(f"cat {FIXTURES / 'screens' / startup}")
    if later:
        lines.append(f"( sleep {later_after}; printf '\\033[H%s' '{later}' ) &")
    lines.append("start=$(date +%s)")
    lines.append("while IFS= read -r line; do case \"$line\" in */model*)")
    if on_model:
        lines.append(f"  [ $(( $(date +%s) - start )) -ge {answer_after} ] && "
                     f"{{ printf '\\033[2J\\033[H'; cat {FIXTURES / 'screens' / on_model}; }};;")
    else:
        lines.append("  :;;")
    lines.append("esac; done")
    lines.append("exec sleep 30")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o755)
    return path


def probe(fake: Path) -> tuple[int, dict]:
    done = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/copilot_runtime.py"), "screen-status", "--", str(fake)],
                          capture_output=True, text=True, timeout=180, env={**os.environ, "PATH": "/usr/bin:/bin"})
    return done.returncode, json.loads(done.stdout)


def test_the_screen_probe_against_a_stand_in_copilot() -> None:
    if shutil.which("tmux") is None:
        print("copilot_runtime_test: screen probe skipped, no tmux")
        return
    with tempfile.TemporaryDirectory(prefix="cp634-probe.", dir="/tmp") as tmp:
        root = Path(tmp)
        seen = root / "seen"
        stand_in(root / "copilot-late", "ready.txt", later="Please use /login to sign in to use Copilot", later_after=2, seen=seen)
        stand_in(root / "copilot-unauthenticated", "unauthenticated.txt", seen=seen)
        stand_in(root / "copilot-ready", "ready.txt", on_model="model-picker-allowed.txt", seen=seen)
        stand_in(root / "copilot-resume-unknown-id", "resume-unknown-id.txt", seen=seen)
        stand_in(root / "copilot-trust", "trust.txt", seen=seen)
        fake = root / "copilot-quits"
        fake.write_text("#!/bin/sh\necho 'something else'\n")
        fake.chmod(0o755)
        os.environ["TMUX_TMPDIR"] = tmp
        results = {}
        for name in ("unauthenticated", "ready", "resume-unknown-id", "trust", "quits", "late"):
            code, answer = probe(root / f"copilot-{name}")
            results[name] = (code, answer["state"])
        check(results == {"unauthenticated": (1, "login"), "ready": (0, "ready"), "resume-unknown-id": (1, "fatal"),
                          "trust": (1, "trust"), "quits": (1, "exited"), "late": (1, "login")}, results)
        lines = seen.read_text().splitlines()
        check(len(lines) == 5 and all(line.startswith("true /tmp/copilot-status-cwd-") for line in lines),
              f"each probe ran in an empty folder of its own, trusted for that run only: {lines}")
        check(not any(Path(line.split(" ", 1)[1]).exists() for line in lines), "and the folder is gone afterwards")


def test_promotion_takes_the_native_executable_or_refuses() -> None:
    with tempfile.TemporaryDirectory(prefix="cp634-promote.") as tmp:
        modules = Path(tmp) / "lib" / "node_modules" / "@github"
        loader = modules / "copilot" / "npm-loader.js"
        loader.parent.mkdir(parents=True)
        loader.write_text("#!/usr/bin/env node\nimport('x')\n")
        loader.chmod(0o755)
        link = Path(tmp) / "bin" / "copilot"
        link.parent.mkdir()
        link.symlink_to(loader)
        try:
            agent_cli_promotion.resolve_agent_cli_source("copilot", link)
        except agent_cli_promotion.AgentCliSourceRejected as exc:
            refusal = str(exc)
        else:
            refusal = ""
        check("is a Node script" in refusal and "npm install -g @github/copilot" in refusal
              and "Nothing has been created" in refusal, f"a loader with no runtime beside it is refused: {refusal}")
        native = modules / "copilot-linux-x64" / "copilot"
        native.parent.mkdir()
        native.write_bytes(b"\x7fELF fake")
        native.chmod(0o755)
        import platform
        if platform.system() == "Linux" and platform.machine() in ("x86_64", "amd64"):
            try:
                promoted = agent_cli_promotion.resolve_agent_cli_source("copilot", link)
            except agent_cli_promotion.AgentCliSourceRejected as exc:
                promoted = exc
            check(promoted == native, f"the self-contained platform executable is what is promoted, never the loader: {promoted}")
        check(agent_cli_promotion.resolve_agent_cli_source("copilot", native) == native, "a native copilot is itself")
        check(agent_cli_promotion.resolve_agent_cli_source("codex", link) == loader.resolve(),
              "no other runtime is given Copilot's rule")

        impostor = Path(tmp) / "impostor"
        impostor.write_text("#!/bin/sh\necho 'copilot 9.9 (not GitHub)'\n")
        impostor.chmod(0o755)
        bin_dir = Path(tmp) / "usr-local-bin"
        try:
            agent_cli_promotion.promote_agent_cli_host_wide("copilot", impostor, bin_dir=bin_dir,
                                                            chown=lambda *_a: None, print_func=lambda _m: None)
        except agent_cli_promotion.AgentCliUnavailable as exc:
            refused = str(exc)
        else:
            refused = ""
        check("not the standalone GitHub Copilot CLI" in refused and not (bin_dir / "copilot").exists(),
              f"a `copilot` that is not GitHub's is not installed: {refused}")


def test_a_launched_copilot_is_not_waited_on_for_a_record_it_defers() -> None:
    from scripts import session_records

    launched = session_records.LaunchSessionRecordStatus(role="main", target=TARGET, session_id="",
                                                         pane_state_source="team_launcher.started")
    deferred = {cli: session_records._runtime_hook_deferred_until_activity(role(cli), launched) for cli in (*OTHERS, "copilot")}
    check(deferred == {"agy": False, "claude": False, "codex": True, "hermes": False, "copilot": True},
          f"Copilot, like Codex, says nothing until its first prompt: {deferred}")
    check(not session_records._expects_launch_runtime_hook_probe(role("copilot")),
          "and is not warned about as a Codex hook that did not report")


def test_model_probe_and_runtime_switches() -> None:
    with capabilities(None), contextlib.redirect_stderr(io.StringIO()):
        unknown = model_validation._model_validation_command(role("copilot"))
    check(unknown is None, f"with no readable help there is no --model to validate, so no probe: {unknown}")
    with capabilities(CAPABILITIES):
        command = model_validation._model_validation_command(role("copilot"))
    check(command is not None and command[:6] == ["copilot", "--model", "m-1", "--reasoning-effort", "high", "--allow-all"]
          and command[6] == "-p", f"the model probe is copilot's own one-shot prompt: {command}")
    entry = {"cli": ["codex"], "extra_args": ["-c", 'model_reasoning_effort="high"',
                                               "--dangerously-bypass-approvals-and-sandbox"]}
    notes = runtime_arguments.reconcile(entry, runtime="copilot", previous_runtime="codex")
    check(entry.get("effort") == "high" and entry.get("yolo") is True and entry["extra_args"] == [],
          f"codex's effort and bypass become copilot's own: {entry} {notes}")
    entry = {"cli": ["copilot"], "extra_args": ["--reasoning-effort", "low", "--allow-all"]}
    runtime_arguments.reconcile(entry, runtime="claude", previous_runtime="copilot")
    check(entry.get("effort") == "low" and entry.get("yolo") is True and entry["extra_args"] == [],
          f"copilot's flags move to claude as intent, not as flags: {entry}")
    entry = {"cli": ["copilot"], "extra_args": ["--reasoning-effort", "low"]}
    runtime_arguments.reconcile(entry, runtime="copilot", previous_runtime="copilot")
    check(entry["extra_args"] == ["--reasoning-effort", "low"] and "effort" not in entry,
          f"copilot's own effort flag stays on copilot, as claude's own --effort does: {entry}")
    entry = {"cli": ["copilot"], "effort": "high", "extra_args": ["--reasoning-effort", "low"]}
    try:
        runtime_arguments.reconcile(entry, runtime="copilot", previous_runtime="copilot")
    except runtime_arguments.ArgumentRefusal as exc:
        refusal = str(exc)
    else:
        refusal = ""
    check("records effort 'high'" in refusal, f"and one that disagrees with the recorded effort is refused: {refusal}")


def test_a_role_with_its_own_copilot_home_is_served_there() -> None:
    """COPILOT_HOME moves hooks, skills, trust and sessions together (measured on 1.0.101), so everything follows it."""
    import types

    from scripts import first_run_setup, pane_hooks, project_onboarding
    from scripts.ticket_board import board_skill as skills

    with tempfile.TemporaryDirectory(prefix="cp634-home.") as tmp:
        home, work = Path(tmp) / "home", Path(tmp) / "work"
        work.mkdir(parents=True)
        custom = Path(tmp) / "custom"
        check(copilot_runtime.copilot_home(home) == home / ".copilot"
              and copilot_runtime.copilot_home(home, {"COPILOT_HOME": "~/alt"}, work) == work / "~" / "alt"
              and copilot_runtime.copilot_home(home, {"COPILOT_HOME": str(custom)}) == custom
              and copilot_runtime.copilot_home(home, {"COPILOT_HOME": "rel"}, work) == work / "rel"
              and copilot_runtime.copilot_home(home, {"COPILOT_HOME": "  "}) == home / ".copilot",
              "the root is $COPILOT_HOME taken literally, as Copilot does: a relative one, ~ included, is under the worktree")

        copilot_role = role("copilot", workdir=str(work), env={"COPILOT_HOME": str(custom)})
        plain = role("copilot", workdir=str(work))
        config = types.SimpleNamespace(roles=[copilot_role, plain, role("claude", env={"COPILOT_HOME": "/elsewhere"})],
                                       role_state_isolation=True, run_as_user="owner634")
        args = pane_hooks._copilot_home_args(config, "owner634", home)
        check(args == ["--copilot-home", str(custom)],
              f"only a Copilot role's own COPILOT_HOME is added for its account: {args}")
        check(pane_hooks._copilot_home_args(types.SimpleNamespace(roles=[plain], role_state_isolation=True,
                                                                  run_as_user="owner634"), "owner634", home) == [],
              "a tenant whose Copilot roles use the default adds nothing")
        saved = launcher.current_user_name, launcher.home_dir_for_user
        launcher.current_user_name, launcher.home_dir_for_user = (lambda: "owner634"), (lambda _user: home)
        try:
            skill_args = project_onboarding.install_generated_project_board_skill_args(
                config, installer=Path("/x/switchyard-board-skill"))
        finally:
            launcher.current_user_name, launcher.home_dir_for_user = saved
        check(skill_args[-2:] == ["--copilot-home", str(custom)], f"the skill installer is told the root: {skill_args}")

        custom.mkdir()
        (custom / "settings.json").write_text('{"disableAllHooks": true}\n')
        mine = custom / "hooks" / "mine.json"
        mine.parent.mkdir()
        mine.write_text('{"version": 1, "hooks": {}}\n')
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "TICKET_BOARD_PROJECT": "pgu",
               "COPILOT_HOME": str(Path(tmp) / "from-env"),
               "TICKET_BOARD_PANE_STATE_DIR": str(Path(tmp) / "state"),
               "TICKET_BOARD_PANE_SESSION_DIR": str(Path(tmp) / "sessions")}
        done = subprocess.run([sys.executable, "-B", str(INSTALLER), "install", "--home", str(home),
                               "--bin-path", str(home / ".local/bin/ticket-board-pane-idle-hook"), *args],
                              capture_output=True, text=True, env=env, timeout=120)
        check(done.returncode == 0, done.stderr)
        roots = [home / ".copilot", Path(tmp) / "from-env", custom]
        check(all((root / "hooks" / INSTALLER_MODULE.COPILOT_HOOK_FILE).is_file() for root in roots),
              f"Copilot's hooks are in the default root, the installer's own $COPILOT_HOME and the role's: "
              f"{[str(root) for root in roots if not (root / 'hooks' / INSTALLER_MODULE.COPILOT_HOOK_FILE).is_file()]}")
        check(mine.read_text() == '{"version": 1, "hooks": {}}\n' and str(custom / "settings.json") in done.stderr,
              f"the owner's hooks in that root are untouched, and its disableAllHooks is reported there: {done.stderr}")
        bad = subprocess.run([sys.executable, "-B", str(INSTALLER), "install", "--home", str(home), "--bin-path",
                              str(home / ".local/bin/ticket-board-pane-idle-hook"), "--copilot-home", "relative"],
                             capture_output=True, text=True, env=env, timeout=120, cwd=tmp)
        check(bad.returncode != 0 and "must be an absolute path" in bad.stderr, f"a relative root is refused: {bad.stderr}")

        source = ROOT / "skills" / "switchyard-board" / "SKILL.md"
        results = skills.install_board_skill(home=home, source=source, source_commit="c0ffee634", copilot_homes=[custom])
        check((custom / "skills" / "switchyard-board" / "SKILL.md").is_file()
              and (home / ".copilot" / "skills" / "switchyard-board" / "SKILL.md").is_file()
              and [r.runtime for r in results].count("copilot") == 2,
              f"the skill is in the role's root as well as ~/.copilot: {[(r.runtime, str(r.path)) for r in results]}")
        check(len(skills.verify_board_skill(home=home, copilot_homes=[custom])) == 6, "and verify checks it there")

        check(first_run_setup._first_run_trust_command(copilot_role) == ["env", f"COPILOT_HOME={custom}", "copilot"]
              and first_run_setup._first_run_trust_command(plain) == ["copilot"]
              and first_run_setup._first_run_trust_command(role("claude", env={"COPILOT_HOME": "/x"})) == ["claude"],
              "the trust step runs Copilot with the role's own COPILOT_HOME, and only Copilot")
        check(launcher._trust_kwargs("copilot", copilot_role) == {"env": copilot_role.env}
              and launcher._trust_kwargs("copilot", plain) == {} and launcher._trust_kwargs("claude", copilot_role) == {}
              and launcher._trust_kwargs("copilot", None) == {},
              "a trust check is given the role's environment only for a Copilot role that sets COPILOT_HOME")
        (custom / "config.json").write_text(f'// managed\n{{"trustedFolders": ["{work}"]}}\n')
        check(launcher._workdir_is_trusted("copilot", owner_home=home, workdir=work, **launcher._trust_kwargs("copilot", copilot_role))
              and not launcher._workdir_is_trusted("copilot", owner_home=home, workdir=work),
              "trust is read from the role's own root, and the default root says nothing about it")

        def runner(args, **_kwargs):
            answer = '{"state": "ready"}\n' if "screen-status" in args else ""
            return subprocess.CompletedProcess(args, 0, answer, "")

        (custom / "config.json").unlink()
        shared = role("copilot", name="app", workdir=str(work), env={"COPILOT_HOME": str(custom)})
        first_run = types.SimpleNamespace(roles=[copilot_role, plain, shared], run_as_user="owner634", desktop_access=None)
        manifest = launcher.build_first_run_setup_manifest(first_run, owner_user="owner634", owner_home=home, runner=runner)
        steps = sorted((s.roles, s.copilot_home, s.command) for s in manifest.folder_trust_steps)
        check(steps == sorted([(("main", "app"), str(custom), ("env", f"COPILOT_HOME={custom}", "copilot")),
                               (("main",), "", ("copilot",))]),
              f"one trust step per worktree and Copilot root, run with that root: {steps}")
        ran = []

        def answered(**kwargs):
            ran.append(kwargs["command"])
            if "env" in kwargs["command"]:  # the person answers "Yes, and remember" in the root it ran with
                (custom / "config.json").write_text(f'{{"trustedFolders": ["{work}"]}}\n')
            return kwargs["is_complete"]()

        saved_until = launcher._run_owner_cli_until
        launcher._run_owner_cli_until = answered
        try:
            report = launcher.run_first_run_auth_phase(first_run, owner_user="owner634", owner_home=home, runner=runner,
                                                       print_func=lambda _m: None)
        finally:
            launcher._run_owner_cli_until = saved_until
        check([cmd[0] for cmd in ran] == ["env", "copilot"] or [cmd[0] for cmd in ran] == ["copilot", "env"],
              f"both trust steps ran: {ran}")
        check([(cli, name) for cli, name, _w in report.untrusted_roles] == [("copilot", "main")],
              f"the root that was answered is done, read from that root; the default one was not answered: {report.untrusted_roles}")

        # The runtime switch's own readiness check asks the role's root too.
        from scripts import role_runtime

        me = launcher.current_user_name()
        saved_checks = launcher._cli_auth_status, launcher._owner_home_for_auth
        launcher._cli_auth_status = lambda *_a, **_k: "authenticated"
        launcher._owner_home_for_auth = lambda *_a, **_k: home
        try:
            switch = types.SimpleNamespace(run_as_user=me)
            trusted = role_runtime._readiness_blockers(switch, copilot_role, "copilot", runner=runner)
            untrusted = role_runtime._readiness_blockers(switch, plain, "copilot", runner=runner)
        finally:
            launcher._cli_auth_status, launcher._owner_home_for_auth = saved_checks
        check(trusted == () and untrusted == (f"copilot does not trust {work} yet",),
              f"a runtime switch reads the role's own root: {trusted} / {untrusted}")

        # Both other trust checks pass the role's own trust arguments (they need a running pool to reach).
        import ast
        for module in ("worker_pool", "worker_pool_command"):
            tree = ast.parse((ROOT / "scripts" / f"{module}.py").read_text(encoding="utf-8"))
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and ast.unparse(node.func).endswith("_workdir_is_trusted")]
            check(len(calls) == 1 and [ast.unparse(k.value).split("(")[0] for k in calls[0].keywords if k.arg is None]
                  == ["launcher._trust_kwargs"], f"{module} gives its trust check the role's own arguments")

        # Hook installation, on launch and on refresh, is told the role's root.
        from scripts import pane_hooks as hooks_module

        tenant = types.SimpleNamespace(roles=[copilot_role, plain], role_state_isolation=True, run_as_user="owner634",
                                       project="pgu", session_dir=Path(tmp) / "sessions", pane_launcher=None)
        saved_ids = launcher.current_user_name, launcher.home_dir_for_user, launcher.uid_for_user
        launcher.current_user_name, launcher.home_dir_for_user = (lambda: "owner634"), (lambda _user: home)
        launcher.uid_for_user = lambda _user: 4634
        sent = []
        try:
            launched = hooks_module.install_generated_project_pane_hooks_args(
                tenant, script_path=Path("/x/team-launcher"), pane_state_dir=Path(tmp) / "state")
            hooks_module.refresh_role_pane_hooks(
                tenant, staging_root=Path(tmp) / "staging", runner=lambda args, **_k: sent.append(args)
                or subprocess.CompletedProcess(args, 0, "", ""), print_func=lambda _m: None)
        finally:
            launcher.current_user_name, launcher.home_dir_for_user, launcher.uid_for_user = saved_ids
        check(launched[-2:] == ["--copilot-home", str(custom)] and len(sent) == 1
              and sent[0][-2:] == ["--copilot-home", str(custom)],
              f"the launch install and the refresh both name the role's root: {launched[-2:]} {sent}")

        cli_home = Path(tmp) / "cli-home"
        cli = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "board_skill_cli.py"), "install", "--home",
                              str(cli_home), "--copilot-home", str(cli_home / "root"), "--source-commit", "c0ffee634"],
                             capture_output=True, text=True, timeout=120, env={"PATH": "/usr/bin:/bin", "HOME": str(cli_home)})
        check(cli.returncode == 0 and (cli_home / "root" / "skills" / "switchyard-board" / "SKILL.md").is_file(),
              f"`switchyard board-skill install --copilot-home` writes the skill there: {cli.stderr[-400:]}")

        sid = "46ffe999-8bcb-4d01-aaa1-60a34e9d0954"
        transcript = custom / "session-state" / sid / "events.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(transcript_for(copilot_role))  # SYRD-638: as Copilot files it
        sessions = home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
        sessions.mkdir(parents=True)
        ok, why = launcher._resume_preflight_allows_attempt(copilot_role, sid, session_dir=sessions)
        check(ok and not why, f"a session in the role's root resumes: {why}")
        relative = role("copilot", workdir=str(work), env={"COPILOT_HOME": "rel-root"})
        moved = work / "rel-root" / "session-state" / sid / "events.jsonl"
        moved.parent.mkdir(parents=True)
        moved.write_text(transcript_for(relative))
        ok, why = launcher._resume_preflight_allows_attempt(relative, sid, session_dir=sessions)
        check(ok and not why, f"a relative COPILOT_HOME is the worktree's, as Copilot reads it: {why}")


def test_a_copilot_pane_prepares_its_own_root_before_it_starts() -> None:
    """A role given COPILOT_HOME after its account was made still starts with Switchyard's hooks and skills there."""
    import types

    from scripts.ticket_board import provision_role_accounts as accounts

    with tempfile.TemporaryDirectory(prefix="cp634-prestart.", dir="/tmp") as tmp:
        tmp_path = Path(tmp)
        home, work = tmp_path / "home", tmp_path / "work"
        work.mkdir(parents=True)
        (home / ".local" / "bin").mkdir(parents=True)
        staged = tmp_path / "control" / "pgu"
        staged.mkdir(parents=True)
        for name in ("ticket-board-install-pane-hooks", "switchyard-board-skill"):
            (staged / name).symlink_to(ROOT / "scripts" / name)
        homed = role("copilot", workdir=str(work), env={"COPILOT_HOME": str(tmp_path / "root"), "TICKET_BOARD_PROJECT": "pgu"})
        relative = role("copilot", workdir=str(work), env={"COPILOT_HOME": "rel", "TICKET_BOARD_PROJECT": "pgu"})
        saved = os.environ.get("SWITCHYARD_TENANT_CONTROL_ROOT")
        os.environ["SWITCHYARD_TENANT_CONTROL_ROOT"] = str(tmp_path / "control")
        try:
            with capabilities(CAPABILITIES):
                built = role_command.cli_command_for_role(homed, session_dir=tmp_path / "sessions")
                plain = role_command.cli_command_for_role(role("copilot", env={"TICKET_BOARD_PROJECT": "pgu"}),
                                                          session_dir=tmp_path / "sessions")
                other = role_command.cli_command_for_role(role("claude", env={"COPILOT_HOME": "/x", "TICKET_BOARD_PROJECT": "pgu"}),
                                                          session_dir=tmp_path / "sessions")
                rel = role_command.copilot_root_prestart(relative, ["copilot"], "pgu")
        finally:
            if saved is None:
                os.environ.pop("SWITCHYARD_TENANT_CONTROL_ROOT", None)
            else:
                os.environ["SWITCHYARD_TENANT_CONTROL_ROOT"] = saved
        check("switchyard-copilot-root" in built, f"a Copilot role with COPILOT_HOME prepares it before it starts: {built}")
        at = built.index("sh")
        check(built[at:at + 7] == ["sh", "-c", role_command.COPILOT_ROOT_PRESTART, "switchyard-copilot-root",
                                   str(staged / "ticket-board-install-pane-hooks"), str(staged / "switchyard-board-skill"),
                                   str(tmp_path / "root")] and built[at + 7] == "copilot",
              f"the pane prepares the role's root from the staged tooling, then runs copilot: {built[at:at + 9]}")
        check("sh" not in plain and "sh" not in other and plain[plain.index("copilot") - 1] != "switchyard-copilot-root",
              "a Copilot role without COPILOT_HOME, and any other runtime, start exactly as before")
        check(rel[6] == str(work / "rel"), f"a relative root is the worktree's, as Copilot reads it: {rel[6]}")
        check(role_command.copilot_root_prestart(homed, ["copilot"], "") == ["copilot"], "with no tenant, nothing is added")

        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "TICKET_BOARD_PROJECT": "pgu",
               "TICKET_BOARD_PANE_STATE_DIR": str(tmp_path / "state"), "TICKET_BOARD_PANE_SESSION_DIR": str(tmp_path / "sessions")}
        ran = subprocess.run([*built[at:at + 7], "sh", "-c", 'echo started "$1"', "copilot", "--model"],
                             capture_output=True, text=True, env=env, cwd=work, timeout=120)
        hooks = tmp_path / "root" / "hooks" / INSTALLER_MODULE.COPILOT_HOOK_FILE
        skill = tmp_path / "root" / "skills" / "switchyard-board" / "SKILL.md"
        check(ran.returncode == 0 and ran.stdout.strip() == "started --model" and ran.stderr == "",
              f"the prepared pane execs the CLI with its own arguments: {ran.stdout!r} {ran.stderr!r}")
        check(hooks.is_file() and skill.is_file() and str(tmp_path / "state") in hooks.read_text(),
              "the hook file, pointing at the pane's own state, and the board skill are in the role's root")
        check(sorted(p.name for p in home.iterdir()) == [".local"] and not list((home / ".local" / "bin").iterdir()),
              f"nothing else of the account is written: {sorted(str(p) for p in home.rglob('*'))}")
        broken = subprocess.run(["sh", "-c", role_command.COPILOT_ROOT_PRESTART, "switchyard-copilot-root",
                                 str(tmp_path / "missing-installer"), str(tmp_path / "missing-skill"), str(tmp_path / "root2"),
                                 "echo", "still started"], capture_output=True, text=True, env=env, cwd=work, timeout=60)
        check(broken.returncode == 0 and broken.stdout.strip() == "still started"
              and "could not put Copilot hooks in" in broken.stderr and "could not put the board skills in" in broken.stderr,
              f"a tenant without the staged tooling is told so, and the pane still starts: {broken.stderr!r}")

        only = subprocess.run([sys.executable, "-B", str(INSTALLER), "install-copilot", "--home", str(home),
                               "--bin-path", str(home / ".local/bin/ticket-board-pane-idle-hook"),
                               "--copilot-home", str(tmp_path / "only")], capture_output=True, text=True, env=env, timeout=120)
        check(only.returncode == 0 and (tmp_path / "only" / "hooks" / INSTALLER_MODULE.COPILOT_HOOK_FILE).is_file()
              and not (home / ".copilot").exists() and not (home / ".claude").exists(),
              f"install-copilot writes Copilot's file in the named root and nothing else: {only.stderr}")

        # The operator commands for a role account carry the role's root too, for add-role and for the migration.
        commands = accounts.role_runtime_commands(project="pgu", runtime_directory="pgu-ticket-board",
                                                  board_current="/b", roles_group="pgu-roles", account="pgu-main",
                                                  home="/home/pgu-main", worktree="", copilot_homes=["/srv/root"])
        installs = [line for line in commands if " install --home " in line]
        check(len(installs) == 2 and all(line.endswith(" --copilot-home '/srv/root'") for line in installs),
              f"both installers in the role-account commands are told the root: {installs}")
        check(not any("--copilot-home" in line for line in accounts.role_runtime_commands(
                  project="pgu", runtime_directory="pgu-ticket-board", board_current="/b", roles_group="pgu-roles",
                  account="pgu-main", home="/home/pgu-main", worktree="")),
              "without a root the commands are exactly as before")
        saved_home = launcher.home_dir_for_user
        launcher.home_dir_for_user = lambda _user: Path("/home/pgu-main")
        try:
            check(launcher.role_copilot_homes(homed, "pgu-main") == [str(tmp_path / "root")]
                  and launcher.role_copilot_homes(role("copilot"), "pgu-main") == []
                  and launcher.role_copilot_homes(role("claude", env={"COPILOT_HOME": "/x"}), "pgu-main") == [],
                  "the add-role and migration callers get the role's root, and only a Copilot role's")
        finally:
            launcher.home_dir_for_user = saved_home
        import ast
        for module, call in (("project_role_add", "role_account_commands"), ("role_account_migration", "role_runtime_commands")):
            tree = ast.parse((ROOT / "scripts" / f"{module}.py").read_text(encoding="utf-8"))
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and ast.unparse(node.func) == call]
            check(len(calls) == 1 and any(k.arg is None and "launcher.role_copilot_homes(" in ast.unparse(k.value)
                                          for k in calls[0].keywords),
                  f"{module} passes the role's own Copilot roots to {call}")
        packet = accounts.role_account_commands("pgu", "main", "owner", "svc", role_accounts=[("main", "pgu-main")],
                                                copilot_homes=["/srv/root"])
        check(packet.count("--copilot-home '/srv/root'") == 2, "add-role's operator commands carry it")
        del types


def test_an_account_refused_its_models_is_not_ready() -> None:
    """SYRD-634 DAT: signed in, the input drawn, and every model refused by Copilot policy -- that is not ready."""
    import types

    denied = "Model catalog failed to load: Error: 403 access denied by Copilot policy"
    for name, state in (("model-catalog-denied-auto", "model_access"), ("model-catalog-denied-auto-prompt", "model_access"),
                        ("model-catalog-denied-model", "model_access"), ("model-catalog-allowed", "ready")):
        check(copilot_runtime.classify_screen(screen(name)) == state, f"{name} reads as {state}")
    check(copilot_runtime.classify_screen(screen("model-catalog-denied-model-prompt")) in {"model_access", "login"},
          "a configured model's refused call is never ready either")
    check(copilot_runtime.classify_screen(screen("model-catalog-denied-model-prompt").replace(
              "Model catalog failed to load", "x")) == "login", "the refused call alone is a sign-in problem")
    check(copilot_runtime.access_detail(screen("model-catalog-denied-model")) == denied
          and copilot_runtime.access_detail(screen("model-catalog-allowed")) == "",
          "the refusal is quoted as Copilot printed it, and only when there is one")
    check("Copilot policy" in screen("model-catalog-allowed") and copilot_runtime.classify_screen(screen("model-catalog-allowed")) == "ready",
          "an unrelated policy notice on a working account is not a refusal")

    if shutil.which("tmux") is not None:
        with tempfile.TemporaryDirectory(prefix="cp634-denied.", dir="/tmp") as tmp:
            root = Path(tmp)
            os.environ["TMUX_TMPDIR"] = tmp
            fakes = {
                # The refusal drawn at startup, before anything is asked.
                "denied": stand_in(root / "c1", "model-catalog-denied-auto.txt"),
                # The refusal drawn only after the input, past the old fixed settle window.
                "late": stand_in(root / "c2", "model-catalog-allowed.txt", later=denied, later_after=9),
                # Nothing at startup; `/model` answers with the refusal (the fast recording).
                "asked": stand_in(root / "c3", "model-catalog-allowed.txt", on_model="model-picker-denied.txt"),
                # A catalog that loads slowly: `/model` opens nothing until 6 s, then the picker.
                "slow": stand_in(root / "c4", "model-catalog-allowed.txt", on_model="model-picker-allowed.txt",
                                 answer_after=6),
                "allowed": stand_in(root / "c5", "model-catalog-allowed.txt", on_model="model-picker-allowed.txt"),
            }
            answers = {name: probe(fake) for name, fake in fakes.items()}
            check(answers == {"denied": (1, {"state": "model_access", "detail": denied}),
                              "late": (1, {"state": "model_access", "detail": denied}),
                              # The recording carries the startup line above the picker's answer; the first is quoted.
                              "asked": (1, {"state": "model_access", "detail": denied}),
                              "slow": (0, {"state": "ready"}),
                              "allowed": (0, {"state": "ready"})},
                  f"only the open model picker is ready; a refusal, early, late or asked for, is not: {answers}")
            silent = stand_in(root / "c6", "model-catalog-allowed.txt")
            state, _screen = copilot_runtime.screen_status([str(silent)], cwd=tmp, timeout=12)
            check(state == "model_unconfirmed",
                  f"an input whose picker never opens is never taken as ready, however long it stays quiet: {state}")

    calls = []

    def probed(answer: dict):
        def run(args, **_kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 1, json.dumps(answer) + "\n", "")
        return run

    status = provider_auth_status._cli_auth_status("copilot", owner_user="o", owner_home=Path("/h"),
                                                   runner=probed({"state": "model_access", "detail": denied}))
    check(status == "access_denied" and len(calls) == 1, f"a refused account is access_denied, not missing: {status} {calls}")
    unconfirmed = provider_auth_status._cli_auth_status("copilot", owner_user="o", owner_home=Path("/h"),
                                                        runner=probed({"state": "model_unconfirmed"}))
    check(unconfirmed == "access_unconfirmed"
          and "could not confirm that o may use Copilot's models" in launcher.auth_blocker_text("copilot", unconfirmed, "o"),
          f"a picker that never opened is its own not-ready status, said as such: {unconfirmed}")
    text = launcher.auth_blocker_text("copilot", status, "syrd-agent", "copilot login")
    check(denied in text and "signed in for syrd-agent" in text and "an account whose policy allows the CLI (copilot login)" in text
          and launcher.auth_blocker_text("codex", "unauthenticated", "o", "codex login") == "codex is not logged in for o; run: codex login",
          f"the blocker says what is wrong, and the other providers' words are unchanged: {text}")

    from scripts import role_runtime

    saved = launcher._cli_auth_status, launcher._owner_home_for_auth, launcher._workdir_is_trusted
    launcher._cli_auth_status = lambda *_a, **_k: "access_denied"
    launcher._owner_home_for_auth = lambda *_a, **_k: Path("/h")
    launcher._workdir_is_trusted = lambda *_a, **_k: True
    try:
        blockers = role_runtime._readiness_blockers(types.SimpleNamespace(run_as_user=launcher.current_user_name()),
                                                    role("copilot"), "copilot", runner=None)
    finally:
        launcher._cli_auth_status, launcher._owner_home_for_auth, launcher._workdir_is_trusted = saved
    check(len(blockers) == 1 and denied in blockers[0], f"a runtime switch to a refused account is refused: {blockers}")

    with tempfile.TemporaryDirectory(prefix="cp634-denied-run.") as tmp:
        home = Path(tmp)
        first_run = types.SimpleNamespace(roles=[role("copilot", workdir=tmp)], run_as_user="owner634", desktop_access=None)
        (home / ".copilot").mkdir()
        (home / ".copilot" / "config.json").write_text(json.dumps({"trustedFolders": [tmp]}))
        ran = []

        def signed_in_again(**kwargs):
            ran.append(kwargs["command"])
            return kwargs["is_complete"]()

        saved_until = launcher._run_owner_cli_until
        launcher._run_owner_cli_until = signed_in_again
        try:
            report = launcher.run_first_run_auth_phase(first_run, owner_user="owner634", owner_home=home,
                                                       runner=probed({"state": "model_access", "detail": denied}),
                                                       print_func=lambda _m: None)
        finally:
            launcher._run_owner_cli_until = saved_until
        said = []
        stopped = launcher.stop_before_launch_for_unauthenticated_providers(report, print_func=said.append)
        warned = []
        launcher.report_first_run_auth_warnings(report, print_func=warned.append)
        check(ran == [["copilot", "login"]], f"the person is given Copilot's own sign-in, to choose another account: {ran}")
        check(stopped and report.unauthenticated_roles == {"copilot": ["main"]} and denied in said[0]
              and "sign-in not finished" not in said[0] and any(denied in line for line in warned),
              f"and no role starts, with the refusal in the stop and the warning: {said} {warned}")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"copilot_runtime_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
