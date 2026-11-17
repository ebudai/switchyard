#!/usr/bin/env python3
"""SYRD-832: every tmux session target Switchyard builds in scripts/ is exact-match.

tmux resolves a target session by exact name and, failing that, by PREFIX. On mefp, 2026-11-17, `mefp-audit` was
absent and `mefp-audit-2` answered for it: audit never started, and restart-role read audit-2's pane as audit's. So
in scripts/, a tmux argv's `-t` value must be one tmux cannot widen:
* a call to `exact_tmux_target(...)` (scripts/ticket_board/tmux_target.py), or the presentation code's
  `_exact_tmux_target(...)`, which delegates to it;
* a literal or f-string that starts with `=`, or names an id (`%pane`, `@window`, `$session`) or a token (`{last}`).
The client commands, whose `-t` names a client, not a session (refresh-client, detach-client, suspend-client,
lock-client), are not session targets. Shell scripts are held to the same rule line by line: `-t "=$session"`.
"""

from __future__ import annotations

import ast
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

#: tmux commands that take a -t target: a session, a window or a pane, each resolved through its session name.
SUBCOMMANDS = frozenset({
    "attach", "attach-session", "capture-pane", "choose-tree", "clear-history", "copy-mode", "display",
    "display-message", "has", "has-session", "if-shell", "join-pane", "kill-pane", "kill-session", "kill-window",
    "last-window", "link-window", "list-clients", "list-panes", "list-windows", "lsp", "move-window", "new-session",
    "new-window", "next-window", "paste-buffer", "pipe-pane", "previous-window", "rename-session", "rename-window",
    "resize-pane", "respawn-pane", "respawn-window", "run-shell", "select-layout", "select-pane", "select-window",
    "send-keys", "send", "set", "set-environment", "set-hook", "set-option", "set-window-option", "setw",
    "show-environment", "show-options", "show-option", "show", "show-window-options", "split-window", "swap-pane",
    "switch-client", "unlink-window",
    # Commands the first list missed, and tmux's own aliases: a tmux argv is checked whatever its command (below),
    # so these only name the command in a finding.
    "resize-window", "resizew", "resize-pane", "resizep", "swap-window", "swapw", "break-pane", "breakp",
    "display-popup", "popup", "display-panes", "displayp", "split-window", "splitw", "new-window", "neww",
    "kill-pane", "killp", "kill-window", "killw", "select-pane", "selectp", "select-window", "selectw",
    "capture-pane", "capturep", "respawn-pane", "respawnp", "list-panes", "list-windows", "lsw",
    "send-keys", "send", "set-window-option", "setw", "show-options", "show", "rename-window", "renamew",
})
#: Their -t is a client (a tty), not a session.
CLIENT_COMMANDS = frozenset({"refresh-client", "detach-client", "suspend-client", "lock-client"})
EXACT_CALLS = frozenset({"exact_tmux_target", "_exact_tmux_target"})
EXACT_LITERAL = ("=", "%", "@", "$", "{")


def _names_tmux(node: ast.AST) -> bool:
    """`"tmux"`, a path to it, or a name that holds it (`tmux`, `REAL_TMUX`, `[*tmux, ...]`)."""
    if isinstance(node, ast.Starred):
        node = node.value
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == "tmux" or node.value.endswith("/tmux")
    name = getattr(node, "id", None) or getattr(node, "attr", None) or ""
    return "tmux" in name.lower()


def _is_exact(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        func = node.func
        return (getattr(func, "id", None) or getattr(func, "attr", None)) in EXACT_CALLS
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.startswith(EXACT_LITERAL)
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        return isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.startswith(EXACT_LITERAL)
    return False


def python_findings(source: str, path: Path) -> list[tuple[Path, int, str]]:
    found = []
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        elts = node.elts
        literals = [e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None for e in elts]
        # Fail closed (Audit, SYRD-832): any argv that runs tmux is checked, whatever its command; a list without tmux in
        # it is still checked when it names a tmux command (an argv built for a runner that adds tmux itself).
        if not (any(_names_tmux(e) for e in elts) or any(v in SUBCOMMANDS or v in CLIENT_COMMANDS for v in literals)):
            continue  # not a tmux argv (ssh-keygen -t, timeout -t ...)
        for index, value in enumerate(literals[:-1]):
            if value != "-t":
                continue
            command = next((v for v in reversed(literals[:index]) if v in SUBCOMMANDS or v in CLIENT_COMMANDS), None)
            if command in CLIENT_COMMANDS or _is_exact(elts[index + 1]):
                continue
            found.append((path, elts[index + 1].lineno, f"`tmux {command or '?'} -t` is not exact-match: "
                                                         f"{ast.unparse(elts[index + 1])[:80]}"))
    return found


#: In shell a leading `$` is a variable, never tmux's session-id prefix.
SHELL_EXACT = ("=", "%", "@", "{")
SHELL_TARGET = re.compile(r"""-t\s+(?P<value>"[^"]*"|'[^']*'|\S+)""")


def shell_findings(source: str, path: Path) -> list[tuple[Path, int, str]]:
    found = []
    for number, line in enumerate(source.splitlines(), 1):
        if "tmux" not in line or line.lstrip().startswith("#"):
            continue
        for match in SHELL_TARGET.finditer(line):
            value = match.group("value").strip("\"'")
            if not value.startswith(SHELL_EXACT) and not value.startswith("$(exact_tmux_target "):
                found.append((path, number, f"tmux -t is not exact-match: {match.group(0)[:80]}"))
    return found


def is_python(path: Path) -> bool:
    if path.suffix == ".py":
        return True
    try:
        first = path.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
    except OSError:
        return False
    return path.suffix == "" and first.startswith("#!") and "python" in first


def is_shell(path: Path) -> bool:
    try:
        first = path.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
    except OSError:
        return False
    return path.suffix in ("", ".sh") and first.startswith("#!") and ("bash" in first or first.endswith("/sh"))


def lint(root: Path = SCRIPTS) -> list[tuple[Path, int, str]]:
    found: list[tuple[Path, int, str]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "migrations" not in p.parts):
        if is_python(path):
            found += python_findings(path.read_text(encoding="utf-8"), path)
        elif is_shell(path):
            found += shell_findings(path.read_text(encoding="utf-8"), path)
    return found


def test_the_rule_on_examples() -> None:
    good = '''import subprocess
subprocess.run(["tmux", "has-session", "-t", exact_tmux_target(session)])
subprocess.run(["tmux", "send-keys", "-t", f"={session}:0.0", "Enter"])
subprocess.run(["tmux", "capture-pane", "-p", "-t", "%12"])
subprocess.run(["tmux", "refresh-client", "-f", flags, "-t", tty])
subprocess.run(["ssh-keygen", "-q", "-t", "ed25519"])
'''
    bad = '''import subprocess
subprocess.run(["tmux", "has-session", "-t", session])
subprocess.run(["tmux", "send-keys", "-t", f"{session}:0.0", "Enter"])
subprocess.run(["tmux", "kill-session", "-t", "mefp-audit"])
subprocess.run(["tmux", "resize-window", "-t", f"{session}:0", "-x", "80"])
subprocess.run([*tmux, "some-future-command", "-t", session])
'''
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "good.py").write_text(good)
        (root / "bad.py").write_text(bad)
        (root / "good.sh").write_text('#!/bin/bash\ntmux send-keys -t "=$target" Enter\ntmux capture-pane -p -t "%3"\n'
                                      'role_tmux "$t" send-keys -t "$(exact_tmux_target "$t")" Enter\n')
        (root / "bad.sh").write_text('#!/bin/bash\ntmux send-keys -t "$target" Enter\n')
        found = lint(root)
    assert sorted((p.name, n) for p, n, _ in found) == [("bad.py", 2), ("bad.py", 3), ("bad.py", 4), ("bad.py", 5),
                                                        ("bad.py", 6), ("bad.sh", 2)], found


def test_the_helper() -> None:
    sys.path.insert(0, str(ROOT))
    from scripts.ticket_board.tmux_target import exact_tmux_target as exact

    cases = {"mefp-audit": "=mefp-audit:", "mefp-audit:0.0": "=mefp-audit:0.0", "mefp-audit:": "=mefp-audit:",
             "=mefp-audit": "=mefp-audit", "%12": "%12", "@3": "@3", "$1": "$1", "{last}": "{last}", "": "",
             ":0.1": ":0.1"}
    wrong = {given: exact(given) for given, want in cases.items() if exact(given) != want}
    assert not wrong, wrong


def test_scripts_build_only_exact_session_targets() -> None:
    found = lint()
    assert not found, "\n".join(f"{p.relative_to(ROOT)}:{n}: {why}" for p, n, why in found)


def main() -> int:
    test_the_rule_on_examples()
    test_the_helper()
    test_scripts_build_only_exact_session_targets()
    print("tmux_exact_target_lint_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
