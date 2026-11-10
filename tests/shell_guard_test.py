#!/usr/bin/env python3
"""SYRD-753: no runtime's shell evaluates a board command's prose, and the text reaches the board byte for byte.

On MEFP (2026-11-10) Ops, a Codex role, wrote a tool call `ticket-board-write return-delivery-finding MEFP-1075
--reason "..."` whose reason quoted a command in Markdown backticks. Codex runs a shell tool call as `bash -c`, so
the shell ran the quoted command before the board client started, and left it running after the turn.

Main runs first, from a git archive. These checks run on both trees:
- **The hooks an install writes.** The real installer runs into a disposable home, as a role's provisioning runs
  it. On main, no runtime has a pre-tool hook that reads a shell command for board prose. On this tree, every
  supported runtime has: Claude Code and Codex (PreToolUse), agy (PreToolUse in its matcher group), Copilot
  (preToolUse) and Hermes (pre_tool_call). Each runs the guard staged beside the installer, not a copy in the role's
  home. Codex's activity hook keeps the trust hash an owner already approved.

On this tree only:
- **The guard as each runtime runs it.** Each installed hook command runs as its runtime runs it (`sh -c`, the
  payload on standard input), with that runtime's payload shape. The unsafe forms are refused, in the answer shape
  the runtime reads: inline double quotes, an unquoted heredoc, a here-string, a pipe, and a board command inside
  `bash -c` or `eval`. The safe forms pass.
- **The bytes.** The Director's text (backticks around a plausible pkexec command, `$()`, `$variables`, nested
  quotes, semicolons, backslashes, newlines, Unicode and leading dashes) goes through every safe form. Each is
  run through `bash -c`, as Codex runs a tool call, with stand-ins for pkexec and a marker program first on PATH.
  Nothing starts, the real parser resolves exactly that text, and a disposable board stores it. The unsafe forms
  are only ever given to the guard, never to a shell.
- **The inventory.** The guard's prose options cover every free-text option of the CLI. No Switchyard program
  runs a shell string through `shell=True`, `os.system` or `os.popen`. The `sh -c` sites are the ones the design
  note accounts for. Notices are handed to directorctl as argv.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

BEFORE = "4eda4daa9dab45170bf8dced73d84e14f9148d35"
CHECKS = 0
RUNTIMES = ("claude", "codex", "agy", "copilot", "hermes")

#: The Director's acceptance set, in one reason.
TEXT = (
    "Run `pkexec /usr/bin/cp --no-clobber /x /y` first, then `syrd753-marker tick`.\n"
    "Substitution: $(syrd753-marker sub) and $((1 + 1))\n"
    "Variables: $HOME ${PATH} $1 $$\n"
    "Nested quotes: \"say 'hi' and \\\"there\\\"\" and 'it\\'s'\n"
    "Semicolons; pipes | ands && ors || redirects > /tmp/syrd753-nope\n"
    "Backslashes: \\ \\\\ \\n \\`\n"
    "Unicode: \u2713 \u2014 \u00e9 \u6f22\u5b57\n"
    "--reason looks like a flag\n"
    "-a leading dash\n"
)


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def load_guard(tree: Path):
    path = tree / "scripts" / "ticket-board-shell-guard"
    loader = importlib.machinery.SourceFileLoader("ticket_board_shell_guard", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def clean_env(home: Path, **extra: str) -> dict[str, str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra)
    return env


# -- the hooks an install writes ------------------------------------------------------------------------------------
def install(tree: Path, home: Path) -> None:
    scripts = tree / "scripts"
    state = home / "state"
    result = subprocess.run(
        [sys.executable, str(scripts / "ticket-board-install-pane-hooks"), "install", "--home", str(home),
         "--hook-source", str(scripts / "ticket-board-pane-idle-hook"),
         "--bin-path", str(home / ".local" / "bin" / "ticket-board-pane-idle-hook")],
        env=clean_env(home, TICKET_BOARD_PROJECT="pgu", TICKET_BOARD_PANE_STATE_DIR=str(state / "pane-state"),
                      TICKET_BOARD_PANE_SESSION_DIR=str(state / "pane-sessions")),
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def _commands(value) -> list[str]:
    """Every hook command line under `value`: the `command` (Claude, Codex, agy, Hermes) or `bash` (Copilot) field."""
    if isinstance(value, dict):
        own = [value[k] for k in ("command", "bash") if isinstance(value.get(k), str)]
        return own + [c for k, v in value.items() if k not in ("command", "bash") for c in _commands(v)]
    if isinstance(value, list):
        return [c for v in value for c in _commands(v)]
    return []


def pre_tool_commands(home: Path) -> dict[str, list[str]]:
    """Each runtime's pre-tool hook commands, as the install wrote them."""
    import yaml

    claude = json.loads((home / ".claude" / "settings.json").read_text())["hooks"].get("PreToolUse", [])
    codex = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"].get("PreToolUse", [])
    agy = json.loads((home / ".gemini" / "config" / "hooks.json").read_text())
    copilot = json.loads((home / ".copilot" / "hooks" / "ticket-board-pane-state.json").read_text())["hooks"]
    hermes = (yaml.safe_load((home / ".hermes" / "config.yaml").read_text()) or {}).get("hooks") or {}
    return {
        "claude": [c for c in _commands(claude) if "ticket-board-pane-idle-hook" not in c],
        "codex": [c for c in _commands(codex) if "ticket-board-pane-idle-hook" not in c],
        "agy": _commands([group.get("PreToolUse", []) for group in agy.values() if isinstance(group, dict)]),
        "copilot": _commands(copilot.get("preToolUse", [])),
        "hermes": _commands(hermes.get("pre_tool_call", [])),
    }


def codex_activity_trust(home: Path) -> list[tuple[str, str]]:
    from scripts.ticket_board.codex_hook_trust import codex_command_hook_trust_entries

    hooks = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]
    activity = {f":pre_tool_use:{g}:{h}" for g, group in enumerate(hooks.get("PreToolUse", []))
                for h, handler in enumerate(group.get("hooks", [])) if " activity " in handler.get("command", "")}
    return [(key.split(str(home))[-1], digest) for _event, key, digest in codex_command_hook_trust_entries(home)
            if any(key.endswith(suffix) for suffix in activity)]


def installed(tree: Path, home: Path) -> dict:
    """What an install writes into `home`, an empty directory, which is removed again (both trees use one path,
    because a hook's command line names the home it was installed into)."""
    home.mkdir()
    try:
        install(tree, home)
        return {"commands": pre_tool_commands(home), "activity_trust": [list(e) for e in codex_activity_trust(home)]}
    finally:
        shutil.rmtree(home, ignore_errors=True)


# -- what the guard sees -------------------------------------------------------------------------------------------
def payload(runtime: str, command: str) -> dict:
    """The pre-tool payload each runtime sends for a shell call (their documented shapes)."""
    if runtime == "claude":
        return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    if runtime == "codex":
        return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    if runtime == "agy":
        return {"toolCall": {"name": "run_command", "args": {"CommandLine": command}}, "stepIdx": 3}
    if runtime == "copilot":
        return {"toolName": "bash", "toolArgs": json.dumps({"command": command, "description": "x"})}
    return {"hook_event_name": "pre_tool_call", "tool_name": "terminal", "tool_input": {"command": command}}


def refused(runtime: str, answer: str) -> bool:
    if not answer.strip() or answer.strip() == "{}":
        # agy reads a JSON object from every hook; the others take silence for "no decision".
        assert answer.strip() == ("{}" if runtime == "agy" else ""), (runtime, answer)
        return False
    data = json.loads(answer)
    reason = ""
    if runtime in ("claude", "codex"):
        out = data.get("hookSpecificOutput") or {}
        ok = out.get("hookEventName") == "PreToolUse" and out.get("permissionDecision") == "deny"
        reason = out.get("permissionDecisionReason", "")
    elif runtime == "copilot":
        ok, reason = data.get("permissionDecision") == "deny", data.get("permissionDecisionReason", "")
    elif runtime == "agy":
        ok, reason = data.get("decision") == "deny", data.get("reason", "")
    else:
        ok, reason = data.get("decision") == "block", data.get("reason", "")
    assert ok and "--reason - <<'EOF'" in reason and "Nothing was run" in reason, (runtime, answer)
    return True


def run_hook(runtime: str, hook_command: str, command: str, home: Path) -> str:
    request = payload(runtime, command)
    request["cwd"] = str(home)
    result = subprocess.run(["sh", "-c", hook_command], input=json.dumps(request),
                            env=clean_env(home), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (runtime, result.stderr)
    return result.stdout


def heredoc(text: str) -> str:
    assert "\nEOF\n" not in "\n" + text
    return "<<'EOF'\n" + text + "EOF"


def single_quoted(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def double_quoted(text: str) -> str:
    """What a model writes when it puts prose in double quotes: only the double quotes escaped."""
    return '"' + text.replace('"', '\\"') + '"'


def safe_forms(body_file: Path) -> dict[str, tuple[str, str]]:
    """name -> (command, the text the parser must resolve)."""
    return {
        "stdin quoted heredoc": (f"ticket-board-write add-comment PGU-1 --text - {heredoc(TEXT)}", TEXT),
        "file option, stdin heredoc": (f"ticket-board-write add-comment PGU-1 --text-file - {heredoc(TEXT)}", TEXT),
        "cat of a quoted heredoc": (f'ticket-board-write add-comment PGU-1 --text "$(cat {heredoc(TEXT)}\n)"',
                                    TEXT.rstrip("\n")),
        "file option": (f"ticket-board-write add-comment PGU-1 --text-file {body_file}", TEXT),
        "single quotes": (f"ticket-board-write add-comment PGU-1 --text {single_quoted(TEXT)}", TEXT),
        "a prepared variable": (f"T=$(cat {body_file}); ticket-board-write add-comment PGU-1 --text \"$T\"",
                                TEXT.rstrip("\n")),
        "a variable-named board command, quoted heredoc": (
            f"BOARD=ticket-board-write; \"$BOARD\" add-comment PGU-1 --text - {heredoc(TEXT)}", TEXT),
    }


def unsafe_forms() -> dict[str, str]:
    reason = double_quoted(TEXT)
    return {
        "inline double quotes (MEFP's form)": f"ticket-board-write return-delivery-finding PGU-1 --reason {reason}",
        "backticks alone (MEFP's reason)": 'ticket-board-write return-delivery-finding PGU-1 --reason '
                                           '"Run `pkexec /usr/bin/cp --no-clobber` from the checkout."',
        "a variable inside prose": 'ticket-board-write add-comment PGU-1 --text "It costs $5 under $HOME."',
        "--flag=value": f"ticket-board-write add-comment PGU-1 --text={reason}",
        "unquoted heredoc": "ticket-board-write add-comment PGU-1 --text - <<EOF\n" + TEXT + "EOF",
        "cat of an unquoted heredoc": 'ticket-board-write add-comment PGU-1 --text "$(cat <<EOF\n' + TEXT + 'EOF\n)"',
        "here-string": f"ticket-board-write add-comment PGU-1 --text - <<< {reason}",
        "pipe": f"echo {reason} | ticket-board-write add-comment PGU-1 --text-file -",
        "inside bash -c": f"bash -c {single_quoted('ticket-board-write add-comment PGU-1 --text ' + reason)}",
        "inside a double-quoted bash -c": f"bash -c {double_quoted('ticket-board-write add-comment PGU-1 --text ' + reason)}",
        "a two-stage pipe": f"echo {reason} | cat | ticket-board-write add-comment PGU-1 --text -",
        "inside eval": f"eval {single_quoted('ticket-board-write add-comment PGU-1 --text ' + reason)}",
        "directorctl message": f"directorctl send app {reason}",
        "after a wrapper, in a pipeline": f"timeout 30 ticket-board-write add-comment PGU-1 --text {reason} 2>&1 | tail -n 3",
        "blocked reason": f"ticket-board-write set-blockers PGU-1 --blocked-by PGU-2 --blocked-reason {reason}",
        # Indirection (Audit, on 4914b680): what runs is a board command, though no word of the line says so.
        "a variable-named board command": f'BOARD=ticket-board-write; "$BOARD" add-comment PGU-1 --text {reason}',
        "a command from a variable this line never sets": f'"$TBW" add-comment PGU-1 --text {reason}',
        "a command found at run time": f"$(command -v ticket-board-write) add-comment PGU-1 --text {reason}",
        "a prepared string run by bash -c": (f"S={single_quoted('ticket-board-write add-comment PGU-1 --text ' + reason)}"
                                             '; bash -c "$S"'),
        "a prepared string run by eval": (f"S={single_quoted('ticket-board-write add-comment PGU-1 --text ' + reason)}"
                                          '; eval "$S"'),
        "an unreadable bash -c string in a line naming a board command": 'export T=ticket-board-write; bash -c "$CMD"',
        "a function that runs a board command": ('function tbw { ticket-board-write "$@"; }; '
                                                 f"tbw add-comment PGU-1 --text {reason}"),
        "an alias for a board command": f"alias tbw=ticket-board-write\ntbw add-comment PGU-1 --text {reason}",
        "a script this line writes, then runs": (f"cat > s.sh <<'EOF'\nticket-board-write add-comment PGU-1 --text "
                                                 f"{reason}\nEOF\nbash s.sh"),
        "a variable-named command behind sudo": f'sudo -u ops "$B" add-comment PGU-1 --text {reason}',
        "a variable-named command in a loop": f'for i in 1; do "$B" add-comment PGU-1 --text {reason}; done',
    }


ORDINARY = (
    "ticket-board-write submit-to-audit PGU-1 --commit $(git rev-parse HEAD)",
    'B=ticket-board-write; "$B" submit-to-audit PGU-1 --commit $(git rev-parse HEAD)',
    '"$EDITOR" notes.md',
    "cat > notes.md <<'EOF'\nticket-board-write x --reason \"`pkexec`\"\nEOF",
    'bash -c "$CMD"',
    "S='ticket-board-write add-comment PGU-1 --text plain'; bash -c \"$S\"",
    'ticket-board-write add-comment "$TICKET_BOARD_TICKET_PREFIX-1" --text "Plain words; nothing to expand."',
    'echo "`date`" && git log --oneline -1',
    "grep -rn 'ticket-board-write' scripts | head",
)


# -- the bytes ------------------------------------------------------------------------------------------------------
STUB_RESOLVER = """#!/usr/bin/env python3
import json, os, sys
sys.path.insert(0, {root!r})
from scripts.ticket_board import write_cli
parser = write_cli._build_parser()
args = parser.parse_args(sys.argv[1:])
write_cli.resolve_free_text_arguments(args, parser)
with open(os.environ["G753_OUT"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"argv": sys.argv[1:], "text": getattr(args, "text", None),
                             "reason": getattr(args, "reason", None)}}) + "\\n")
"""
STUB_MARKER = "#!/bin/sh\necho \"$0 $*\" >> \"$G753_MARKS\"\nexit 1\n"


def stub_dir(base: Path) -> Path:
    stubs = base / "stubs"
    stubs.mkdir()
    (stubs / "ticket-board-write").write_text(STUB_RESOLVER.format(root=str(ROOT)))
    for name in ("pkexec", "syrd753-marker", "cp", "id", "hostname"):
        (stubs / name).write_text(STUB_MARKER)
    for path in stubs.iterdir():
        path.chmod(0o755)
    return stubs


def run_through_bash(command: str, base: Path, stubs: Path) -> tuple[list[dict], str]:
    out, marks = base / "out.jsonl", base / "marks.txt"
    for path in (out, marks):
        path.unlink(missing_ok=True)
    result = subprocess.run(["bash", "-c", command], cwd=base, capture_output=True, text=True, timeout=60,
                            env={"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(base), "LANG": "C.UTF-8",
                                 "PYTHONDONTWRITEBYTECODE": "1", "G753_OUT": str(out), "G753_MARKS": str(marks)})
    assert result.returncode == 0, (command[:80], result.stderr)
    rows = [json.loads(line) for line in out.read_text().splitlines()] if out.exists() else []
    return rows, marks.read_text() if marks.exists() else ""


def taught_in_double_quotes():
    """A prose option shown with its value inside double quotes, except the quoted-heredoc idiom."""
    flags = "|".join(sorted(f[2:] for f in load_guard(ROOT).PROSE_FLAGS))
    return re.compile(r'--(?:' + flags + r')(?:=| )\\?"(?!\$\(cat <<\'EOF\')')


def taught_inline_prose():
    """Free-form placeholders need a file or stdin; quote delimiters cannot safely wrap arbitrary replacement text."""
    flags = load_guard(ROOT).PROSE_FLAGS - {"--set", "--value", "--patch-json", "--payload-json"}
    names = "|".join(sorted(f[2:] for f in flags))
    return re.compile(r'--(?:' + names + r')(?:=| )\\?[\'"](?!\$\(cat <<\'EOF\')')


def board_round_trip(texts: dict[str, str]) -> tuple[dict[str, bool], list[str]]:
    """Each text, written through a client pinned to a disposable board, read back exactly; and every function of
    the board's schema whose source shows board prose inside double quotes."""
    import ticket_board_write_api_test as t
    from scripts.ticket_board import write_client as wc
    from temporary_cluster import temporary_cluster

    stored: dict[str, bool] = {}
    with temporary_cluster(prefix="shell-guard-", shutdown="immediate") as cluster:
        root, sock, port = cluster.root, cluster.socket_dir, cluster.port
        db = "shell_guard_test"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, (ROOT / "scripts/ticket_board/schema.sql").read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        t.seed_postgres_ticket(admin, "PGU-1", title="Prose", state="in_progress", assignee="ops")
        sources = t.psql(admin, "SELECT p.proname || ' ' || replace(p.prosrc, E'\\n', ' ') FROM pg_proc p "
                                "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'ticket_board';")
        taught = taught_inline_prose()
        hinting = sorted({line.split(" ", 1)[0] for line in sources.splitlines() if taught.search(line)})
        (root / "frames").mkdir(exist_ok=True)
        (root / "assets").mkdir(exist_ok=True)
        app = t.TicketBoardApp(root / "frames", root / "assets", project="cerulean", ticket_prefix="PGU",
                               database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE))
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            #: socket_path=None: this client can only reach the disposable board above.
            client = wc.TicketBoardWriteClient(f"http://127.0.0.1:{server.server_port}", "ops", socket_path=None,
                                               write_token=server.write_token or "")
            for name, text in texts.items():
                client.add_comment("PGU-1", text=text)
                comments = [str(c.get("text") or "") for c in (app.get_ticket("PGU-1").get("comments") or [])]
                # The board strips exactly one trailing newline from a comment body (SYRD-196).
                stored[name] = comments[-1] == (text[:-1] if text.endswith("\n") else text)
        finally:
            server.shutdown()
            thread.join(timeout=5)
    return stored, hinting


# -- the inventory --------------------------------------------------------------------------------------------------
#: Every `sh -c`/`bash -c` argv in Switchyard's Python, per file: each carries a fixed script, with positional
#: arguments or shlex-quoted trusted values (docs/syrd-753-board-text-never-reaches-a-shell.md says which).
SHELL_C_SITES = {
    "scripts/agent_cli_promotion.py": 1, "scripts/browser_inheritance.py": 5, "scripts/github_identity.py": 2,
    "scripts/owner_commands.py": 1, "scripts/presentation_layout_files.py": 1, "scripts/provider_auth_status.py": 1,
    "scripts/repository_boundary_repair.py": 1, "scripts/role_command.py": 1, "scripts/staged_role_tooling.py": 1,
    "scripts/tenant_control_helper.py": 1, "scripts/tenant_release_deploy.py": 1,
    "scripts/tenant_release_report.py": 4, "scripts/ticket_board/publication_boundary.py": 6,
    "scripts/upgrade_phases.py": 1,
}


def inventory() -> None:
    from scripts.ticket_board import write_cli

    guard = load_guard(ROOT)
    parser = write_cli._build_parser()
    free = {flag for action in parser._subparsers._group_actions[0].choices.values()
            for flag, *_ in getattr(action, "_free_text_fields", ())}
    check(len(free) >= 16 and free <= guard.PROSE_FLAGS,
          f"the guard reads every free-text option of the CLI as prose: missing {sorted(free - guard.PROSE_FLAGS)}")

    sources = {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8") for p in (ROOT / "scripts").rglob("*.py")
               if "migrations" not in p.parts}
    loose = sorted(name for name, text in sources.items()
                   if re.search(r"shell\s*=\s*True|os\.system\(|os\.popen\(|subprocess\.getoutput\(", text))
    check(loose == [], f"no Switchyard program hands a shell a string to parse: {loose}")
    sites = {name: n for name, text in sources.items() if (n := len(re.findall(r'\[\s*"(?:ba)?sh",\s*"-c"', text)))}
    check(sites == SHELL_C_SITES, f"the sh -c sites are the inventoried ones: {sites}")

    taught = taught_inline_prose()
    teaching = [p for base in ("skills", "docs/onboarding") for p in (ROOT / base).rglob("*.md")]
    teaching += [ROOT / name for name in sources if name != "scripts/ticket_board/write_cli.py"]
    inline = [f"{p.relative_to(ROOT)}:{n}" for p in teaching
              for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if taught.search(line)]
    check(inline == [], f"every skill, guide and hint feeds arbitrary prose through a file or stdin: {inline}")
    director_skill = (ROOT / "skills/switchyard-director/SKILL.md").read_text()
    check('--payload-json "$(cat <<\'EOF\'\n{"reason": "<what to fix, from the User\'s message>"}\nEOF\n)"'
          in director_skill, "the User's apostrophe in a structured payload stays inside a quoted heredoc")

    dispatch = (ROOT / "scripts/ticket_board/notification_dispatch.py").read_text()
    check('[self.directorctl_bin, "send", target, message]' in dispatch,
          "a notice is handed to directorctl as argv, never as a shell string")
    directorctl = (ROOT / "scripts/directorctl").read_text()
    check('send-keys -t "$target" -l "$payload"' in directorctl and "eval " not in directorctl,
          "directorctl types a notice as literal keys and evaluates nothing")


def script_inputs(base: Path) -> None:
    """Decision-only regressions: unsafe text is never passed to any shell or interpreter."""
    guard = load_guard(ROOT)
    unsafe = 'ticket-board-write add-comment PGU-1 --text "Run `printf harmless-marker` first."\n'
    safe = "ticket-board-write add-comment PGU-1 --text - <<'EOF'\n" + TEXT + "EOF\n"
    script = base / "existing.sh"
    script.write_text("#!/bin/sh\n" + unsafe)
    script.chmod(0o700)
    for command in ("bash existing.sh", "sh ./existing.sh", "source existing.sh", ". existing.sh", "./existing.sh",
                    f"bash {single_quoted(str(script))}", 'S=existing.sh; bash "$S"',
                    "env bash existing.sh", "bash < existing.sh"):
        check(bool(guard.problems_in(command, cwd=str(base))), f"existing/cross-call script refused: {command}")
    # This write represents the earlier tool call; the decision reads its current bytes on the later call.
    script.write_text("#!/bin/sh\n" + safe)
    check(not guard.problems_in("bash existing.sh", cwd=str(base)), "existing script with literal stdin passes")
    check(not guard.problems_in("./existing.sh", cwd=str(base)), "directly executed safe shell script passes")
    script.write_text("#!/bin/sh\n" + unsafe)
    old_path = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = str(base) + os.pathsep + old_path
        check(bool(guard.problems_in("existing.sh", cwd=str(base))), "PATH-resolved shell script is inspected")
    finally:
        os.environ["PATH"] = old_path
    search = base / "path-scripts"
    search.mkdir()
    (search / "source-me").write_text(unsafe)
    (base / "source-me").write_text("printf 'safe local file\\n'\n")
    check(bool(guard.problems_in(f"PATH={single_quoted(str(search))}; source source-me", cwd=str(base))),
          "source searches PATH even when the current directory has no matching file")
    for producer in (f"printf '%s' {single_quoted(unsafe)}", f"printf '%s\\n' {single_quoted(unsafe.rstrip())}"):
        for consumer in ("bash staged.sh", "source staged.sh", "./staged.sh"):
            command = f"{producer} > staged.sh; {consumer}"
            check(bool(guard.problems_in(command, cwd=str(base))), f"printf staging refused: {consumer}")
    check(not guard.problems_in(f"printf '%s' {single_quoted(safe)} > staged.sh; bash staged.sh", cwd=str(base)),
          "deterministic safe printf staging passes")
    check(bool(guard.problems_in("printf '%s' \"$DYNAMIC\" > staged.sh; bash staged.sh", cwd=str(base))),
          "unresolved staged script content refuses")
    for command in ('bash "$SCRIPT"', 'source "$SCRIPT"', "bash missing.sh", "bash - < missing.sh"):
        check(bool(guard.problems_in(command, cwd=str(base))), f"unresolved/unreadable shell input refuses: {command}")
    script.chmod(0)
    check(bool(guard.problems_in("bash existing.sh", cwd=str(base))), "unreadable script refuses before execution")
    script.chmod(0o700)
    linked = base / "linked.sh"
    linked.symlink_to(script)
    check(bool(guard.problems_in("bash linked.sh", cwd=str(base))), "explicit shell symlink input refuses safely")
    (base / "oversized.sh").write_bytes(b"#" * ((1 << 20) + 1))
    check(bool(guard.problems_in("bash oversized.sh", cwd=str(base))), "oversized script input refuses boundedly")
    nested = base / "nested"
    nested.mkdir()
    (nested / "input.sh").write_text(unsafe)
    check(bool(guard.problems_in("cd nested; bash input.sh", cwd=str(base))), "script cwd follows literal cd")
    check(bool(guard.problems_in('cd "$DYNAMIC"; bash input.sh', cwd=str(base))), "unresolved cd fails closed")
    check(bool(guard.problems_in(f"printf '%s' {single_quoted(unsafe)} | bash", cwd=str(base))),
          "printf script piped into shell is inspected")
    for code in (f"import os; os.system({unsafe!r})", f"import subprocess; subprocess.run({unsafe!r}, shell=True)",
                 f"import subprocess; subprocess.run(['bash', '-c', {unsafe!r}])",
                 "import subprocess; subprocess.run(['bash', 'existing.sh'])"):
        check(bool(guard.problems_in("python3 -c " + single_quoted(code), cwd=str(base))),
              "directly visible Python shell reparse refuses unsafe board prose")
    check(not guard.problems_in("python3 -c 'print(42)'", cwd=str(base)), "ordinary interpreter code passes")
    code = f"print(['bash', '-c', {unsafe!r}])"
    check(not guard.problems_in("python3 -c " + single_quoted(code), cwd=str(base)),
          "printing a shell-looking list is data rather than interpreter execution")
    code = f"require('child_process').exec({json.dumps(unsafe)})"
    check(bool(guard.problems_in("node -e " + single_quoted(code), cwd=str(base))),
          "directly visible Node shell reparse refuses board text")
    check(not (base / "harmless-marker").exists(), "decision checks never execute their script text")


def syntax_and_command_tokens(base: Path) -> None:
    """Real repository scripts and synthetic shell syntax are only inspected, never executed."""
    guard = load_guard(ROOT)
    for text in ("/home/switchyard-agent/worktree", "switchyard-example", "switchyard.json", "switchyard2",
                 "switchyard_local", "myswitchyard", "/opt/switchyard/docs"):
        check(not guard.mentions_command(text), f"ordinary path/prefixed text is not a board command: {text}")
        check(not guard.problems_in(f"printf '%s' {single_quoted(text)}; bash -c \"$CMD\""),
              f"ordinary path/prefixed text does not poison an unresolved command: {text}")
    for text in ("switchyard", "/opt/switchyard/current/switchyard", "sudo switchyard --help", "B=ticket-board-write"):
        check(guard.mentions_command(text), f"complete command token, including a path, is recognized: {text}")
    unsafe = 'ticket-board-write add-comment PGU-1 --text "Run `printf harmless-marker` first."'
    for command in (unsafe, "B=ticket-board-write; \"$B\" " + unsafe.split(" ", 1)[1],
                    unsafe.replace("ticket-board-write", "/opt/switchyard/ticket-board-write", 1),
                    'switchyard set-role-runtime p app --reason "`printf harmless-marker`"'):
        check(bool(guard.problems_in(command)), "actual board command still refuses evaluated prose")
    cases = (
        'case "$x" in alpha|beta) switchyard status ;; *) printf safe ;; esac',
        'case "$x" in (alpha) switchyard status ;& (beta) printf safe ;;& (*) printf safe ;; esac',
        'case "$x" in alpha) case "$y" in beta) switchyard status ;; *) printf safe ;; esac ;; esac',
        'X=$(case "$x" in alpha) switchyard status ;; *) printf safe ;; esac)',
        'f() { case "$x" in alpha) switchyard status ;; *) printf safe ;; esac; }; f',
        'A=(first second); A+=(third); case "$x" in alpha) switchyard status ;; esac',
    )
    for command in cases:
        try:
            parsed = guard.scan(command)
        except guard.Unreadable:
            parsed = []
        check(any(any(w.literal == "switchyard" for w in c.words) for c in parsed),
              "case/array/function syntax preserves the actual branch command")
        check(not guard.problems_in(command), f"safe case/array/function syntax passes: {command}")
    for command in (f'case "$x" in alpha) {unsafe} ;; *) printf safe ;; esac',
                    f'X=$(case "$x" in alpha) {unsafe} ;; *) printf safe ;; esac)',
                    f'f() {{ case "$x" in alpha) {unsafe} ;; esac; }}; f'):
        check(bool(guard.problems_in(command)), "unsafe board prose in a case/function branch still refuses")
    for command in ('case "$x" in alpha) switchyard status', 'switchyard status )', 'bash "$UNREADABLE"'):
        check(bool(guard.problems_in(command)), "genuinely unreadable board syntax or dynamic script still refuses")
    scripts = ("directorctl", "ensure-postgres-cluster", "install-all-repository-hooks.sh", "install-switchyard",
               "install-switchyard-prereqs", "switchyard-pane-window", "ticket-board-boardsvc-setup.sh",
               "ticket-board-notify-listener-service.sh", "ticket-board-service.sh")
    for name in scripts:
        problems = guard.problems_in("bash scripts/" + name, cwd=str(ROOT))
        check(not problems, f"safe repository script is accepted without execution: {name}: {problems}")
    migrations = list((ROOT / "scripts/ticket_board/migrations").glob("pgu1035_*.sql"))
    check([p.name for p in migrations] == ["pgu1035_syrd753_reason_file_hints.sql"],
          "the single pgu1035 migration is named for its actual reason-file hints")
    sibling = base / "sibling.sh"
    sibling.write_text(unsafe)
    entry = base / "entry.sh"
    entry.write_text('source "$(dirname "${BASH_SOURCE[0]}")/sibling.sh"\n')
    check(bool(guard.problems_in("bash " + single_quoted(str(entry)))),
          "a statically resolved sibling source still inspects and refuses its unsafe board prose")
    sibling.write_text("printf 'safe sibling\\n'\n")
    entry.write_text("B=ticket-board-write\nbash -c " + single_quoted('source "$(dirname "${BASH_SOURCE[0]}")/sibling.sh"') + "\n")
    check(bool(guard.problems_in("bash " + single_quoted(str(entry)))),
          "an inline new shell does not inherit a script path for BASH_SOURCE sibling resolution")


def visible_file_replacements(base: Path) -> None:
    """Earlier visible replacement commands must invalidate pre-call destination bytes; never run these commands."""
    guard = load_guard(ROOT)
    unsafe = 'ticket-board-write add-comment PGU-1 --text "Run `printf harmless-marker` first."\n'
    safe = "#!/bin/sh\nprintf 'safe bytes\\n'\n"
    folder = base / "replacement"
    folder.mkdir()
    (folder / "unsafe.sh").write_text(unsafe)
    (folder / "safe.sh").write_text(safe)
    (folder / "target.sh").write_text(safe)
    for operation in ("cp", "mv", "install -m 0700", "ln -f", "ln -sf"):
        for consumer in ("bash target.sh", "source target.sh", "./target.sh"):
            command = f"{operation} unsafe.sh target.sh; {consumer}"
            check(bool(guard.problems_in(command, cwd=str(folder))),
                  f"visible {operation} replaces old safe bytes before {consumer}")
        check(not guard.problems_in(f"{operation} safe.sh target.sh; bash target.sh", cwd=str(folder)),
              f"literal safe replacement by {operation} remains usable")
        check(not guard.problems_in(f"{operation} unsafe.sh target.sh", cwd=str(folder)),
              f"ordinary {operation} does not itself evaluate the copied text")
        for operands in ('"$SRC" target.sh', 'unsafe.sh "$DEST"', '"$SRC" "$DEST"'):
            check(bool(guard.problems_in(f"{operation} {operands}; bash target.sh", cwd=str(folder))),
                  f"dynamic {operation} cannot trust the destination's previous bytes: {operands}")
    check(not guard.problems_in("cp -n unsafe.sh target.sh; bash target.sh", cwd=str(folder)),
          "no-clobber copy preserves an existing safe target")
    check(bool(guard.problems_in('S=unsafe.sh; D=target.sh; env cp -- "$S" "$D"; bash "$D"', cwd=str(folder))),
          "literal assigned operands and wrappers still track replacement")
    check(bool(guard.problems_in('S=unsafe.sh; cp "$S" target.sh; S=safe.sh; bash target.sh', cwd=str(folder))),
          "a later reassignment cannot change which bytes an earlier copy used")
    check(not guard.problems_in('S=safe.sh; cp "$S" target.sh; S=unsafe.sh; bash target.sh', cwd=str(folder)),
          "a safe earlier copy is not changed by later variable values")
    check(bool(guard.problems_in('D=target.sh; cp unsafe.sh "$D"; D=other.sh; bash target.sh', cwd=str(folder))),
          "a later destination assignment cannot hide the earlier replacement")
    directory = folder / "directory"
    directory.mkdir()
    (directory / "unsafe.sh").write_text(safe)
    (directory / "safe.sh").write_text(unsafe)
    for operation in ("cp", "mv", "install", "ln"):
        check(bool(guard.problems_in(f"{operation} -t directory unsafe.sh; bash directory/unsafe.sh", cwd=str(folder))),
              f"{operation}'s literal target-directory option tracks the actual destination")
    check(bool(guard.problems_in("ln -sf ../unsafe.sh directory/target.sh; bash directory/target.sh", cwd=str(folder))),
          "relative symlink source resolves from the link's parent directory")
    check(not guard.problems_in("ln -sf unsafe.sh directory/target.sh; bash directory/target.sh", cwd=str(folder)),
          "a symlink to safe sibling bytes does not accidentally read unsafe same-named cwd bytes")
    check(bool(guard.problems_in("ln -sf safe.sh directory/target.sh; bash directory/target.sh", cwd=str(folder))),
          "an unsafe symlink sibling is detected even when its cwd namesake is safe")
    check(bool(guard.problems_in("ln -srf unsafe.sh directory/target.sh; bash directory/target.sh", cwd=str(folder))),
          "ln --relative resolves its source from the command's cwd")
    check(not guard.problems_in("ln -srf safe.sh directory/target.sh; bash directory/target.sh", cwd=str(folder)),
          "a relative-mode link to a safe source is usable")
    for command in ('cp --target-directory="$DEST" unsafe.sh; bash target.sh',
                    'cp -t"$DEST" unsafe.sh; bash target.sh', 'cp -fR directory target.sh; bash target.sh'):
        check(bool(guard.problems_in(command, cwd=str(folder))),
              "dynamic directory options and unsupported recursive replacements fail closed")
    alias = folder / "alias.sh"
    alias.symlink_to("target.sh")
    check(bool(guard.problems_in("cp unsafe.sh alias.sh; bash target.sh", cwd=str(folder))),
          "copy through an existing destination symlink invalidates its actual target")
    (folder / "directory-alias").symlink_to(directory, target_is_directory=True)
    source_directory = folder / "replacement-source"
    source_directory.mkdir()
    (source_directory / "unsafe.sh").write_text(unsafe)
    check(bool(guard.problems_in("cp -R replacement-source/. directory-alias; bash directory/unsafe.sh", cwd=str(folder))),
          "uncertain recursive writes invalidate the resolved destination directory too")
    check(bool(guard.problems_in("ln -sf safe.sh middle.sh; ln -sf middle.sh target.sh; cp unsafe.sh target.sh; "
                                "bash safe.sh", cwd=str(folder))),
          "a copy through a modeled link chain updates the actual source state")
    for operation in ("ln -f", "ln -sf"):
        check(bool(guard.problems_in(f"{operation} safe.sh target.sh; printf '%s' {single_quoted(unsafe)} > safe.sh; "
                                    "bash target.sh", cwd=str(folder))),
              "a modeled link follows a later visible write to its source")
        check(bool(guard.problems_in(f"{operation} safe.sh target.sh; printf '%s' {single_quoted(unsafe)} > target.sh; "
                                    "bash safe.sh", cwd=str(folder))),
              "a later visible write through a modeled link reaches its source")
    check(not guard.problems_in("helper() { cp \"$SRC\" \"$DEST\"; }; bash target.sh", cwd=str(folder)),
          "a function definition does not execute a file replacement at top level")
    check(bool(guard.problems_in("helper() { cp unsafe.sh target.sh; bash target.sh; }; helper", cwd=str(folder))),
          "a replacement and shell within one function body are inspected together")
    check(bool(guard.problems_in("function helper { cp unsafe.sh target.sh; bash target.sh; }; helper", cwd=str(folder))),
          "keyword-style function definitions retain their first replacement command")
    check((folder / "target.sh").read_text() == safe and (folder / "unsafe.sh").read_text() == unsafe,
          "all replacement and script commands were inspected without execution or file mutation")


def board_reach_decisions(base: Path) -> None:
    """Unrelated script syntax is not a board-text refusal; static/unknown board reach remains fail-closed."""
    guard = load_guard(ROOT)
    folder = base / "reach"
    folder.mkdir()
    unsafe = 'ticket-board-write add-comment PGU-1 --text "`printf harmless-marker`"\n'
    unrelated = {
        "dynamic-source.sh": 'source "$RUNTIME_HELPER"\nprintf safe\n',
        "unknown-program.sh": '"$PROGRAM" "$(printf harmless)"\n',
        "gui-text.sh": '"$GUI" --text "$(printf harmless)" --title "$(printf harmless)"\n',
        "missing-optional.sh": 'source /nonexistent-g753-optional-configuration\n',
        "pipe-input.sh": 'printf "$GENERATED" | bash\n',
        "expanded-input.sh": 'bash <<EOF\n$GENERATED\nEOF\n',
        "syntax.sh": 'case "$MODE" in unfinished\n',
    }
    nested = "printf safe"
    for _ in range(7):
        nested = "bash -c " + single_quoted(nested)
    unrelated["deep-inline.sh"] = nested
    for name, text in unrelated.items():
        (folder / name).write_text(text)
        check(not guard.problems_in("bash " + name, cwd=str(folder)),
              f"an unrelated inspected script's unresolved/syntax/depth detail is allowed: {name}")
    (folder / "board-source.sh").write_text('B=ticket-board-write\nsource "$RUNTIME_HELPER"\n')
    (folder / "unknown-board.sh").write_text('"$BOARD" add-comment PGU-1 --text "`printf harmless-marker`"\n')
    (folder / "actual-board.sh").write_text(unsafe)
    for name in ("board-source.sh", "unknown-board.sh", "actual-board.sh"):
        check(bool(guard.problems_in("bash " + name, cwd=str(folder))),
              f"actual or unresolved board-command reach still refuses: {name}")
    (folder / "board-depth.sh").write_text("B=ticket-board-write\n" + nested)
    check(bool(guard.problems_in("bash board-depth.sh", cwd=str(folder))),
          "an unsupported inline depth still refuses when the script has board reach")
    for name in ("pipe-input.sh", "expanded-input.sh"):
        (folder / "board-input.sh").write_text("B=ticket-board-write\n" + unrelated[name])
        check(bool(guard.problems_in("bash board-input.sh", cwd=str(folder))),
              f"an unresolved script stream still refuses when the inspected script has board reach: {name}")
    (folder / "child.sh").write_text(unsafe)
    (folder / "child.sh").chmod(0o700)
    parents = ("source child.sh", "bash child.sh", 'LIB=.\nsource "$LIB"/child.sh',
               'case "$MODE" in unfinished\nsource child.sh',
               'case "$MODE" in unfinished\n./child.sh')
    for index, text in enumerate(parents):
        (folder / "parent.sh").write_text(text)
        check(bool(guard.problems_in("bash parent.sh", cwd=str(folder))),
              f"a statically followed board subscript refuses, even after unrelated syntax: {index}")
    (folder / "child-helper").write_text("#!/bin/sh\n" + unsafe)
    (folder / "child-helper").chmod(0o700)
    (folder / "parent.sh").write_text('case "$MODE" in unfinished\nchild-helper\n')
    original_path = os.environ["PATH"]
    try:
        os.environ["PATH"] = str(folder) + os.pathsep + original_path
        check(bool(guard.problems_in("bash parent.sh", cwd=str(folder))),
              "a literal PATH shell subscript remains followed after unrelated syntax")
    finally:
        os.environ["PATH"] = original_path
    (folder / "parent.sh").write_text("PATH=" + single_quoted(str(folder) + ":/usr/bin") +
                                      '\ncase "$MODE" in unfinished\nchild-helper\n')
    check(bool(guard.problems_in("bash parent.sh", cwd=str(folder))),
          "unsupported syntax does not discard a subscript found through a prior literal PATH assignment")
    (folder / "unreadable.sh").write_text("printf safe\n")
    (folder / "unreadable.sh").chmod(0)
    (folder / "parent.sh").write_text("source unreadable.sh\n")
    check(bool(guard.problems_in("bash parent.sh", cwd=str(folder))),
          "an unreadable literal shell-file input has unknown board reach and still refuses")
    (folder / "trampoline.sh").write_text('#!/bin/sh\nexec wish "$0" -- "$@"\nsource unreadable.sh\n')
    check(not guard.problems_in("bash trampoline.sh", cwd=str(folder)),
          "a known unconditional self-interpreter trampoline does not analyze its Tcl body as shell")
    (folder / "conditional.sh").write_text('if false; then\nexec wish "$0"\nfi\nbash child.sh\n')
    check(bool(guard.problems_in("bash conditional.sh", cwd=str(folder))),
          "a conditional interpreter exec cannot hide a later supported shell input")
    (folder / "safe-board.sh").write_text("ticket-board-write add-comment PGU-1 --text 'safe'\n")
    (folder / "parent.sh").write_text('source safe-board.sh\nsource "$UNKNOWN_SCRIPT"\n')
    check(bool(guard.problems_in("bash parent.sh", cwd=str(folder))),
          "a safely quoted board subscript still makes the parent's unresolved input relevant")
    check(bool(guard.problems_in('bash "$UNKNOWN_SCRIPT"', cwd=str(folder))),
          "the model's direct dynamic shell-file input remains fail-closed")
    (folder / "a.sh").write_text("source b.sh\n")
    (folder / "b.sh").write_text("source a.sh\n")
    check(not guard.problems_in("bash a.sh", cwd=str(folder)), "a finite unrelated static cycle is bounded and allowed")
    for i in range(66):
        (folder / f"chain{i}.sh").write_text(f"source chain{i + 1}.sh\n" if i < 65 else "printf safe\n")
    check(bool(guard.problems_in("bash chain0.sh", cwd=str(folder))),
          "an oversized static graph has unknown reach and refuses at the explicit bound")
    for i in range(7):
        (folder / f"deep{i}.sh").write_text(f"source deep{i + 1}.sh\n" if i < 6 else unsafe)
    check(bool(guard.problems_in("bash deep0.sh", cwd=str(folder))),
          "static board reach is followed beyond the inline-shell nesting bound")
    for name in ("xdg-open", "checkupdates", "gitk", "pgmcrater"):
        if shutil.which(name):
            check(not guard.problems_in(name), f"the real unrelated PATH tool is allowed by command name: {name}")
        else:
            print(f"shell_guard_test: {name} unavailable; PATH-tool case not exercised")
    check(not guard._immutable_system_script(str(folder / "child.sh")),
          "a role-writable script never receives the immutable package-path exception")
    (folder / "safe.sh").write_text("printf safe\n")
    (folder / "indirect.sh").write_text("bash safe.sh\n")
    check(bool(guard.problems_in("bash indirect.sh; cp child.sh safe.sh; bash indirect.sh", cwd=str(folder))),
          "a prior safe graph visit cannot hide a later file replacement in its child")
    (folder / "repeat.sh").write_text("bash indirect.sh; cp child.sh safe.sh; bash indirect.sh\n")
    check(bool(guard.problems_in("bash repeat.sh", cwd=str(folder))),
          "an inspected parent's shared graph does not reuse stale child-file state")
    (folder / "new-target.sh").write_text('cp "$SOURCE" "$DEST"; bash absent-target.sh\n')
    check(bool(guard.problems_in("bash new-target.sh", cwd=str(folder))),
          "an unresolved visible replacement may create a currently absent literal script input")
    if shutil.which("checkupdates"):
        check(bool(guard.problems_in('sudo cp "$SOURCE" "$DEST"; checkupdates', cwd=str(folder))),
              "unknown privileged replacement does not receive the immutable package-script exception")
    check((folder / "child.sh").read_text() == unsafe, "reachability inspection never executes or changes a script")


def main() -> int:
    if "--installed" in sys.argv:
        at = sys.argv.index("--installed")
        print(json.dumps(installed(Path(sys.argv[at + 1]), Path(sys.argv[at + 2]))))
        return 0
    archive = Path(tempfile.mkdtemp(prefix="g753-main-", dir=os.environ.get("TMPDIR") or None))
    homes = Path(tempfile.mkdtemp(prefix="g753-homes-", dir=os.environ.get("TMPDIR") or None))
    try:
        subprocess.run(f"git -C {ROOT} archive {BEFORE} | tar -x -C {archive}", shell=True, check=True)
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--installed", str(archive),
                                 str(homes / "home")], capture_output=True, text=True, timeout=120, cwd=archive)
        assert result.returncode == 0, result.stderr
        before = json.loads(result.stdout.splitlines()[-1])
        main_schema = (archive / "scripts/ticket_board/schema.sql").read_text(encoding="utf-8")
        check(len(taught_in_double_quotes().findall(main_schema)) == 4,
              "main: four of the schema's own hints show a reason inside double quotes (apply_blockers, "
              "note_provider_quota, provider_hold_status, begin_provider_account_hold)")
        after = installed(ROOT, homes / "home")
    finally:
        shutil.rmtree(archive, ignore_errors=True)
        shutil.rmtree(homes, ignore_errors=True)
    check(all(commands == [] for commands in before["commands"].values()),
          f"main: no runtime has a pre-tool hook reading a shell command for board prose: {before['commands']}")

    staged = ROOT / "scripts" / "ticket-board-shell-guard"
    for runtime in RUNTIMES:
        commands = after["commands"][runtime]
        check(commands == [f"{staged} --runtime {runtime}"],
              f"{runtime}: one pre-tool hook, the guard staged beside the installer: {commands}")
    check(after["activity_trust"] == before["activity_trust"] and len(after["activity_trust"]) == 1,
          f"Codex's activity hook keeps the trust an owner approved: {before['activity_trust']} -> "
          f"{after['activity_trust']}")

    base = Path(tempfile.mkdtemp(prefix="g753-run-", dir=os.environ.get("TMPDIR") or None))
    try:
        body_file = base / "body.txt"
        body_file.write_text(TEXT, encoding="utf-8")
        script_inputs(base)
        syntax_and_command_tokens(base)
        visible_file_replacements(base)
        board_reach_decisions(base)
        safe, unsafe = safe_forms(body_file), unsafe_forms()
        (base / "safe-input.sh").write_text("#!/bin/sh\nprintf 'safe script\\n'\n")
        unsafe["existing shell input, created in an earlier call"] = "bash existing.sh"
        unsafe["executable shell script from an earlier call"] = "./existing.sh"
        for runtime in RUNTIMES:
            hook = after["commands"][runtime][0]
            for name, command in unsafe.items():
                check(refused(runtime, run_hook(runtime, hook, command, base)),
                      f"{runtime}: {name} is refused before it runs")
            for name, (command, _text) in safe.items():
                check(not refused(runtime, run_hook(runtime, hook, command, base)), f"{runtime}: {name} passes")
            for command in ORDINARY:
                check(not refused(runtime, run_hook(runtime, hook, command, base)),
                      f"{runtime}: an ordinary command passes: {command}")
            check(not refused(runtime, run_hook(runtime, hook, "bash safe-input.sh", base)),
                  f"{runtime}: the hook resolves a relative safe script in the tool's working directory")

        stubs = stub_dir(base)
        resolved: dict[str, str] = {}
        for name, (command, text) in safe.items():
            rows, marks = run_through_bash(command, base, stubs)
            check(marks == "", f"{name}: nothing in the text was run: {marks!r}")
            check(len(rows) == 1 and rows[0]["text"] == text,
                  f"{name}: the parser resolves exactly the text: {rows and rows[0]['text']!r}")
            resolved[name] = rows[0]["text"]
        rows, marks = run_through_bash(f"ticket-board-write request-dependency PGU-1 --role director --reason - "
                                       f"{heredoc(TEXT)}", base, stubs)
        check(marks == "" and rows[0]["reason"] == TEXT, f"a --reason through stdin is exact too: {rows}")
        stored, hinting = board_round_trip(resolved)
        for name, ok in stored.items():
            check(ok, f"{name}: the board stores the text byte for byte")
        check(hinting == [], f"schema hints use file/stdin for free-form prose, including apostrophes: {hinting}")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    inventory()
    print(f"shell_guard_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
