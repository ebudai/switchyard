"""The test-run boundary: no test touches a live account's home or runtime directory (SYRD-627).

A suite run from a role pane inherits that pane's HOME, XDG directories and
TICKET_BOARD_PANE_* stores. On 2026-10-18 a host-boundary suite running as
namespace root resolved a project's session store to the Audit pane's live
store through those variables. Its fixture then chowned every ancestor of the
record it wrote, from the record up to `/`, to its stand-in owner: the live
home, .local, .local/state and the pane-session store went to subordinate UID
494715, and every Codex pane hook failed.

A suite that maps root in a user namespace calls `isolate()` before it imports
any production module:

* **Disposable environment.** For a run from a pane (HOME unset or live),
  HOME, TMPDIR and the XDG directories point into a fresh root under TMPDIR,
  and TICKET_BOARD_*, PGU_TICKET_BOARD_*, DIRECTORCTL* and TMUX* are removed.
  Every child inherits that, namespace children included, because they inherit
  the environment. A child a suite starts with an environment it prepared on
  purpose keeps it, less any variable pointing into a live root.
* **Guard.** Every Python process of the run refuses, before acting, any chown,
  chmod, rename, mkdir, unlink, link, utime, truncate or write-mode open that
  resolves under the invoking account's real home or its /run/user directory.
  Inside the test's own checkout, writes are allowed but chowns never are. A
  mutating command run through subprocess is refused the same way. Children
  load the guard through `boundary_site/sitecustomize.py` on PYTHONPATH. The
  guard resolves paths with the posix functions directly, so a suite's own
  recorder of os/pathlib lookups never sees it.
* **Sentinel.** The owner, group and mode of the home chain, every
  `*-ticket-board` store under ~/.local/state and the runtime directory are
  snapshotted at isolation and checked when the process exits. On drift the
  run fails, naming the suite and every namespace command it ran.

Each refusal and each drift is printed with the responsible command and stack.
`SWITCHYARD_TEST_BOUNDARY_LOG`, if set, receives a copy.
"""

from __future__ import annotations

import atexit
import builtins
import errno
import hashlib
import io
import itertools
import os
import posix
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

LIVE_ROOTS_ENV = "SWITCHYARD_TEST_LIVE_ROOTS"
ALLOW_ENV = "SWITCHYARD_TEST_WRITABLE"
DISPOSABLE_ENV = "SWITCHYARD_TEST_DISPOSABLE_ROOT"
LOG_ENV = "SWITCHYARD_TEST_BOUNDARY_LOG"
INVOKER_UID_ENV = "SWITCHYARD_TEST_INVOKER_UID"
SITE_DIR = Path(__file__).resolve().parent / "boundary_site"
REPOSITORY = Path(__file__).resolve().parents[1]
#: What a role pane carries that names live stores, a live tmux server or the caller's own identity.
SCRUBBED_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL", "XDG_", "TMUX")
SCRUBBED_NAMES = frozenset({"HOME", "TMPDIR"})
MUTATING_COMMANDS = frozenset({"chown", "chgrp", "chmod", "install", "mkdir", "mv", "cp", "rm", "rmdir", "ln", "touch",
                               "tee", "setfacl", "truncate", "rsync", "dd", "mkfifo", "mknod", "unlink", "shred"})
#: Their options that take a separate value which is not a path (a mode, an owner, a size, an ACL).
_MUTATING_VALUE_OPTIONS = frozenset({"-m", "--mode", "-o", "--owner", "-g", "--group", "-s", "--size", "-x", "-M",
                                     "-X", "-Z", "--context", "-S", "--suffix"})
#: Whose first operand is an owner, a group or a mode, not a path.
_SPEC_FIRST = frozenset({"chown", "chgrp", "chmod"})
#: Which write only their destination, not their sources.
_COPIERS = frozenset({"cp", "install", "ln", "rsync"})
_REDIRECT = re.compile(r"^(?:\d*|&)(?:>>?|>\||<>)(.*)$")
HARMLESS = ("/dev/null", "/dev/tty", "/dev/pts/", "/proc/self/")
#: Privilege escalation, service and session control: no test reaches the real ones (SYRD-627). A suite that
#: needs their behaviour brings a fake -- a stand-in on PATH is not refused, only what resolves into the system.
PRIVILEGED_COMMANDS = frozenset({"sudo", "pkexec", "su", "doas", "runuser", "systemctl", "loginctl", "machinectl",
                                 "busctl", "systemd-run"})
#: What these tools may still do for real: ask. Anything else -- stop, kill, terminate, start -- is refused.
QUERY_VERBS = {
    "systemctl": frozenset({"is-active", "is-enabled", "is-failed", "is-system-running", "status", "show", "cat",
                            "list-units", "list-unit-files", "list-timers", "list-sockets", "list-dependencies",
                            "get-default", "show-environment"}),
    "loginctl": frozenset({"list-sessions", "list-users", "list-seats", "show-session", "show-user", "show-seat",
                           "session-status", "user-status", "seat-status"}),
    "machinectl": frozenset({"list", "status", "show", "list-images", "show-image", "image-status"}),
    "busctl": frozenset({"list", "status", "tree", "introspect", "get-property"}),
}
#: Their options that take a separate value, so the value is not mistaken for the verb.
_VALUE_OPTIONS = frozenset({"-p", "--property", "-t", "--type", "-M", "--machine", "-H", "--host", "-n", "--lines",
                            "-o", "--output", "--root", "--state"})
#: Where the real tools live; a stand-in a suite made lives in its own fixture.
SYSTEM_PREFIXES = ("/usr/", "/bin/", "/sbin/", "/lib", "/opt/", "/etc/", "/snap/")
#: Kills by name reach whatever matches, inside the fixture or not.
BROAD_KILL_COMMANDS = frozenset({"pkill", "killall"})
RUN_START_ENV = "SWITCHYARD_TEST_RUN_START"

_roots: list[str] = []
_writable: list[str] = []
_namespace_commands: list[str] = []
_installed = False


# --------------------------------------------------------------------------
# path resolution the suites cannot observe
# --------------------------------------------------------------------------


def _realpath(path: str, depth: int = 0) -> str:
    """realpath through posix.lstat/readlink, so a suite's recorder of os.lstat and Path.resolve sees nothing."""
    parts, out = [p for p in path.split("/") if p], "/"
    for index, part in enumerate(parts):
        if part == ".":
            continue
        if part == "..":
            out = out.rsplit("/", 1)[0] or "/"
            continue
        candidate = out.rstrip("/") + "/" + part
        try:
            info = posix.lstat(candidate)
        except OSError:
            rest = parts[index + 1:]
            return candidate + ("/" + "/".join(rest) if rest else "")
        if stat.S_ISLNK(info.st_mode) and depth < 40:
            target = posix.readlink(candidate)
            out = _realpath(target if target.startswith("/") else out.rstrip("/") + "/" + target, depth + 1)
        else:
            out = candidate
    return out


def _resolve(path: object, dir_fd: int | None = None) -> str:
    try:
        if isinstance(path, int):
            return posix.readlink(f"/proc/self/fd/{path}")
        text = os.fsdecode(path)  # type: ignore[arg-type]
        if dir_fd is not None and not text.startswith("/"):
            text = posix.readlink(f"/proc/self/fd/{dir_fd}").rstrip("/") + "/" + text
        elif not text.startswith("/"):
            text = posix.getcwd().rstrip("/") + "/" + text
        parent, _, name = text.rstrip("/").rpartition("/")
        return _realpath(parent or "/").rstrip("/") + "/" + name
    except Exception:  # noqa: BLE001 - an unresolvable path is judged by nothing, not by a crash
        return ""


def _under(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


# --------------------------------------------------------------------------
# the guard
# --------------------------------------------------------------------------


def _report(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()
    log = os.environ.get(LOG_ENV, "")
    if log:
        with _real_open(log, "a") as handle:
            handle.write(text)


def _check(operation: str, path: str, *, chown: bool = False) -> None:
    if not path or path.startswith(HARMLESS) or not _under(path, _roots):
        return
    if not chown and _under(path, _writable):
        return
    _report(f"test boundary: REFUSED {operation} {path} (pid {os.getpid()}, uid {os.getuid()}, "
            f"command {shlex.join(sys.argv)})\n" + "".join(traceback.format_stack(limit=16)[:-2]))
    raise Refused(f"{operation} under a live root", [path])


_real_open = builtins.open
_real = {name: getattr(os, name) for name in ("chown", "lchown", "fchown", "chmod", "lchmod", "fchmod", "rename",
                                               "replace", "mkdir", "rmdir", "unlink", "remove", "symlink", "link",
                                               "open", "utime", "truncate") if hasattr(os, name)}
_real_popen_init = subprocess.Popen.__init__
_real_system = os.system
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _one_path(name: str, *, chown: bool = False):
    real = _real[name]

    def guarded(path, *args, **kwargs):
        _check(name, _resolve(path, kwargs.get("dir_fd")), chown=chown)
        return real(path, *args, **kwargs)

    guarded.__wrapped__ = real  # type: ignore[attr-defined]
    return guarded


def _two_paths(name: str):
    real = _real[name]

    def guarded(src, dst, *args, **kwargs):
        if name != "symlink":  # a link's target is not changed by making the link
            _check(name, _resolve(src, kwargs.get("src_dir_fd")))
        _check(name, _resolve(dst, kwargs.get("dst_dir_fd", kwargs.get("dir_fd"))))
        return real(src, dst, *args, **kwargs)

    guarded.__wrapped__ = real  # type: ignore[attr-defined]
    return guarded


def _os_open(path, flags, mode=0o777, *, dir_fd=None):
    if flags & _WRITE_FLAGS:
        _check("open", _resolve(path, dir_fd))
    return _real["open"](path, flags, mode, dir_fd=dir_fd)


def _open(file, mode="r", *args, **kwargs):
    if not isinstance(file, int) and any(c in mode for c in "wax+"):
        _check("open", _resolve(file))
    return _real_open(file, mode, *args, **kwargs)


def account_tmux_dir(uid: int | None = None) -> str:
    """The invoking account's default tmux socket directory: where its live server listens."""
    if uid is None:
        uid = int(os.environ.get(INVOKER_UID_ENV) or os.getuid())
    return f"/tmp/tmux-{uid}"


#: tmux's global options that take a value; every other leading option is a flag.
_TMUX_VALUED = frozenset({"-S", "-L", "-f", "-c", "-T"})


def tmux_socket(words: list[str], environ: dict[str, str] | os._Environ) -> str:
    """The socket a command line's tmux will reach, decided as tmux decides it, or "" when it runs no tmux.

    -S wins; then $TMUX, unless -L names another socket; then -L's name, or
    `default`, in `$TMUX_TMPDIR/tmux-<uid>`, which is /tmp's without
    TMUX_TMPDIR. Removing TMUX and TMUX_TMPDIR is therefore not isolation: it
    is the account's default socket, its live server (SYRD-627). `env` before
    tmux is read for the variables it sets or clears.
    """
    at = next((i for i, w in enumerate(words) if os.path.basename(w) == "tmux"), None)
    if at is None:
        return ""
    env = dict(environ)
    if words and os.path.basename(words[0]) == "env":
        for word in words[1:at]:
            if word in ("-i", "--ignore-environment", "-"):
                env = {}
            elif "=" in word and not word.startswith("-"):
                name, _, value = word.partition("=")
                env[name] = value
    path = label = ""
    index = at + 1
    while index < len(words) and words[index].startswith("-") and words[index] != "--":
        word = words[index]
        if word in _TMUX_VALUED:
            value = words[index + 1] if index + 1 < len(words) else ""
            path = value if word == "-S" else path
            label = value if word == "-L" else label
            index += 2
            continue
        if word[:2] == "-S" and len(word) > 2:
            path = word[2:]
        elif word[:2] == "-L" and len(word) > 2:
            label = word[2:]
        index += 1
    if path:
        return path
    if not label and env.get("TMUX"):
        return env["TMUX"].split(",")[0]
    base = (env.get("TMUX_TMPDIR") or "/tmp").rstrip("/")
    return f"{base}/tmux-{os.getuid()}/{label or 'default'}"


def _shared_tmux_dir(socket: str) -> str:
    """The host's shared default tmux directory (/tmp/tmux-<uid>, any uid) that `socket` lies in, or ''.

    The account's own is where its live server listens. Any other is one a namespace root (uid 0, 1, ...) or another
    identity would create on the host, owned on disk by the invoking or a subordinate UID -- which real root's tmux,
    or that account's, then refuses as unsafe."""
    resolved, own = _resolve(socket), _realpath(account_tmux_dir())
    if _under(resolved, [own]):
        return own
    directory = os.path.dirname(resolved)
    if os.path.dirname(directory) == _realpath("/tmp") and re.fullmatch(r"tmux-\d+", os.path.basename(directory)):
        return directory
    return ""

class Refused(PermissionError):
    """A command the boundary will not run. Raised inside the guard only: the caller sees the command fail."""

    def __init__(self, why: str, words: list[str]) -> None:
        super().__init__(errno.EACCES, f"test boundary: {why} is refused", words[0] if words else "")
        self.why = why


#: What a refused command exits with, as a shell does for a command it cannot run.
REFUSED_STATUS = 126


def _check_tmux(words: list[str], environ) -> None:
    socket = tmux_socket(words, environ)
    directory = _shared_tmux_dir(socket) if socket else ""
    if directory:
        _refuse_command(words, f"tmux on {socket}, in the host's shared tmux directory {directory}")


def _refuse_command(words: list[str], why: str) -> None:
    _report(f"test boundary: REFUSED {why}: {shlex.join(words)} (run by {shlex.join(sys.argv)})\n"
            + "".join(traceback.format_stack(limit=16)[:-2]))
    raise Refused(why, words)


def _kill_targets(arguments: list[str]) -> list[str]:
    """The targets of a `kill` command line: what follows its signal, as kill parses it."""
    args = list(arguments)
    if args and args[0] in ("-s", "-n"):
        args = args[2:]
    elif args and args[0].startswith("-") and args[0] != "--" and not args[0][1:].isdigit():
        args = args[1:]  # -TERM, -SIGKILL
    elif args and args[0].startswith("-") and args[0][1:].isdigit() and len(args) > 1:
        args = args[1:]  # -9 with a target after it: a signal number
    if args and args[0] == "--":
        args = args[1:]
    return args


_ESCALATORS = frozenset({"sudo", "doas", "pkexec", "runuser"})
_WRAPPERS = frozenset({"env", "timeout", "nice", "nohup", "setsid", "stdbuf", "unshare", "ionice", "chrt"})
#: Words a shell reads before the command itself: reserved words, and what opens a group.
_KEYWORDS = frozenset({"if", "then", "else", "elif", "while", "until", "do", "!", "{", "(", "time", "function"})
_SHELLS = frozenset({"sh", "bash", "dash", "zsh"})


def _program(words: list[str]) -> tuple[str, str, list[str]]:
    """The command a line really runs, past leading assignments, wrappers (env, timeout, ...) and their options,
    assignments and durations: its name, the word that names it, and its arguments."""
    index = 0
    while index < len(words) and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[index]) or words[index] in _KEYWORDS
                                  or re.match(r"^[A-Za-z_][\w.-]*\(\)$", words[index])
                                  or (words[index].endswith(")") and not words[index].startswith("$("))):
        index += 2 if words[index] == "function" else 1
    while index < len(words):
        name = os.path.basename(words[index])
        if name not in _WRAPPERS:
            return name, words[index], words[index + 1:]
        index += 1
        while index < len(words) and (words[index].startswith("-") or "=" in words[index]
                                      or words[index].rstrip("smhd").replace(".", "").isdigit()):
            index += 1
    return "", "", []


def _escalated(arguments: list[str]) -> list[str]:
    """The command line sudo (doas, ...) is given, past its own options."""
    index = 0
    while index < len(arguments) and arguments[index].startswith("-"):
        index += 2 if arguments[index] in ("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U") else 1
    return arguments[index:]


def _effective(words: list[str]) -> list[str]:
    """The command line that really runs, past wrappers and any sudo: [program, *arguments]."""
    for _ in range(4):
        name, word, rest = _program(words)
        if name not in _ESCALATORS:
            return [word, *rest] if word else []
        words = _escalated(rest)
    return words


def _is_real(word: str, environ) -> bool:
    """Whether `word` runs a tool installed in the system, not a stand-in a suite put on PATH."""
    path = word if "/" in word else shutil.which(word, path=(os.environ if environ is None else environ).get("PATH"))
    return bool(path) and _realpath(path).startswith(SYSTEM_PREFIXES)


def _verb(arguments: list[str]) -> str:
    """The first argument that is not an option or an option's value: systemctl's, loginctl's ... verb."""
    skip = False
    for word in arguments:
        if skip:
            skip = False
        elif word in _VALUE_OPTIONS:
            skip = True
        elif not word.startswith("-"):
            return word
    return ""


def _script_commands(script: str) -> list[tuple[list[str], str | None, bool, str]]:
    """A shell script's simple commands, in order, as words, each with the body of its heredoc if it has one,
    whether that heredoc's delimiter was quoted (its body then is not expanded), and the command's own text.

    Separators (`;`, `&`, `|`, `&&`, `||`, newlines) split commands only outside quotes, `$(...)` and backticks; a
    backslash-newline continues a line; comments are dropped; a heredoc's body is not read as commands."""
    commands: list[list] = []
    current: list[str] = []
    pending: list[tuple[int, str, bool, bool]] = []
    quote, depth, tick, index, size = "", 0, False, 0, len(script)

    def finish() -> None:
        text = "".join(current).strip()
        current.clear()
        if not text:
            return
        try:
            words = _split(text)
        except ValueError:
            return
        quoted = bool(re.search(r"<<-?\s*['\"\\\\]", text))  # a quoted delimiter: the body is not expanded
        kept, at = [], 0
        while at < len(words):
            word = words[at]
            if word.startswith("<<") and not word.startswith("<<<"):
                marker = word[2:]
                strip = marker.startswith("-")
                marker = marker[1:] if strip else marker
                if not marker and at + 1 < len(words):
                    at += 1
                    marker = words[at]
                pending.append((len(commands), marker, strip, quoted))
            else:
                kept.append(word)
            at += 1
        commands.append([kept, None, False, text])

    while index < size:
        char = script[index]
        following = script[index + 1] if index + 1 < size else ""
        if quote == "'":
            current.append(char)
            quote = "" if char == "'" else quote
            index += 1
            continue
        if quote == '"':
            current.append(char)
            if char == "\\" and following:
                current.append(following)
                index += 2
                continue
            quote = "" if char == '"' else quote
            index += 1
            continue
        if char == "\\" and following:
            if following != "\n":
                current.extend((char, following))
            index += 2
            continue
        if char in "'\"":
            quote = char
        elif char == "`":
            tick = not tick
        elif char == "$" and following == "(":
            depth += 1
            current.extend((char, following))
            index += 2
            continue
        elif char == ")" and depth:
            depth -= 1
        elif char == "#" and not depth and not tick and (not current or current[-1].isspace()):
            while index < size and script[index] != "\n":
                index += 1
            continue
        elif char in ";&|\n" and not depth and not tick:
            previous = current[-1] if current else ""
            if (char == "&" and (following == ">" or previous in "<>")) or (char == "|" and previous == ">"):
                current.append(char)
                index += 1
                continue
            finish()
            index += 2 if char in "&|" and following == char else 1
            if char == "\n" and pending:
                for slot, marker, strip, quoted in pending:
                    body = []
                    while index < size:
                        end = script.find("\n", index)
                        line = script[index:] if end < 0 else script[index:end]
                        index = size if end < 0 else end + 1
                        if (line.lstrip("\t") if strip else line) == marker:
                            break
                        body.append(line)
                    commands[slot][1], commands[slot][2] = "\n".join(body), quoted
                pending.clear()
            continue
        current.append(char)
        index += 1
    finish()
    return [(words, body, quoted, raw) for words, body, quoted, raw in commands]


def _closing(text: str, start: int) -> int:
    """The index of the `)` closing a `$(` whose body starts at `start` (or the end of `text`)."""
    depth, quote, index = 1, "", start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 1
            elif char == quote:
                quote = ""
        elif char == "\\":
            index += 1
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if not depth:
                return index
        index += 1
    return len(text)


def _spans(text: str) -> list[tuple[int, int, str]]:
    """Where the command substitutions a shell would run in `text` are -- `$(...)` and backticks, outside single
    quotes (inside double quotes too) -- as (start, end, body). Nested ones are found when each body is read."""
    spans, index, single, double = [], 0, False, False
    while index < len(text):
        char = text[index]
        if single:
            single = char != "'"
        elif char == "\\":
            index += 1
        elif char == "'" and not double:
            single = True
        elif char == '"':
            double = not double
        elif text.startswith("$((", index):
            index += 2  # arithmetic; a substitution inside it is still found
        elif text.startswith("$(", index):
            end = _closing(text, index + 2)
            spans.append((index, end + 1, text[index + 2:end]))
            index = end
        elif char == "`":
            end = index + 1
            while end < len(text) and text[end] != "`":
                end += 2 if text[end] == "\\" else 1
            spans.append((index, end + 1, text[index + 1:end].replace("\\`", "`")))
            index = end
        index += 1
    return spans


def _substitutions(text: str) -> list[str]:
    """The bodies of the command substitutions a shell would run in `text`."""
    return [body for _start, _end, body in _spans(text)]


def _split(text: str) -> list[str]:
    """`text` split into words as the shell splits them, quotes removed -- a command substitution kept whole, as
    written, inside the word it is part of."""
    spans = _spans(text)
    protected, last = [], 0
    for number, (start, end, _body) in enumerate(spans):
        protected += [text[last:start], f"\x00{number}\x00"]
        last = end
    protected.append(text[last:])
    words = shlex.split("".join(protected))
    return [re.sub(r"\x00(\d+)\x00", lambda m: text[spans[int(m.group(1))][0]:spans[int(m.group(1))][1]], w)
            for w in words]


_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
_PARAMETER = re.compile(r"\$\{(?P<braced>[A-Za-z_]\w*(?:\[0\])?)(?:(?P<op>:?[-=])(?P<default>[^}]*))?\}"
                        r"|\$(?P<bare>[A-Za-z_]\w*|0)")


def _substituted(body: str, variables: dict, cwd: str | None) -> str | None:
    """What a command substitution prints, for the few the guard can know without running anything: mktemp (a
    fresh path in its directory), `cd X && pwd`, pwd, dirname, basename, realpath and readlink -f. None otherwise."""
    here, value = cwd, None
    for command in re.split(r"\s*(?:&&|;)\s*", body.strip()):
        try:
            words = _split(command)
        except ValueError:
            return None
        if not words:
            continue
        name, arguments = words[0], [_expand(w, variables, here) for w in words[1:]]
        if any(argument is None for argument in arguments):
            return None
        operands = [a for a in arguments if not a.startswith("-")]
        if name == "cd" and operands:
            here = operands[0] if operands[0].startswith("/") else (os.path.join(here, operands[0]) if here else None)
            if here is None:
                return None
            here = os.path.normpath(here)
            value = None
        elif name == "pwd":
            value = here
        elif name in ("dirname", "basename", "realpath", "readlink") and operands:
            if name == "readlink" and "-f" not in arguments and "-e" not in arguments and "-m" not in arguments:
                return None
            path = operands[0] if operands[0].startswith("/") or name in ("dirname", "basename") \
                else (os.path.join(here, operands[0]) if here else None)
            if path is None:
                return None
            value = os.path.dirname(path) or "." if name == "dirname" else os.path.basename(path) \
                if name == "basename" else os.path.normpath(path)
        elif name == "mktemp":
            directory, template, skip = None, None, False
            for index, argument in enumerate(arguments):
                if skip:
                    skip = False
                elif argument in ("-p", "--tmpdir") and index + 1 < len(arguments):
                    directory, skip = arguments[index + 1], True
                elif argument.startswith("--tmpdir="):
                    directory = argument.split("=", 1)[1]
                elif argument == "-t":
                    directory = directory or variables.get("TMPDIR") or "/tmp"
                elif not argument.startswith("-"):
                    template = argument
            if template and "/" in template and directory is None:
                directory, template = os.path.dirname(template), os.path.basename(template)
            if directory is None:
                directory = (variables.get("TMPDIR") or "/tmp") if not template else here
            if directory is None:
                return None
            value = os.path.join(directory, (template or "tmp.XXXXXXXXXX").replace("X", "q"))
        else:
            return None
    return value


def _expand(word: str, variables: dict, cwd: str | None = None) -> str | None:
    """`word` as the shell would expand it, from what the script and its environment assigned: `~`, `~user`, `$NAME`,
    `${NAME}`, `${NAME:-default}`, and the substitutions `_substituted` can know. None when the guard cannot know
    the value -- an unset or unknowable variable, a positional parameter, any other substitution -- for the caller
    to refuse."""
    while "$(" in word or "`" in word:
        start = word.find("$(") if "$(" in word else len(word)
        tick = word.find("`")
        if 0 <= tick < start:
            end = word.find("`", tick + 1)
            end = len(word) if end < 0 else end
            body, before, after = word[tick + 1:end], word[:tick], word[end + 1:]
        else:
            end = _closing(word, start + 2)
            body, before, after = word[start + 2:end], word[:start], word[end + 1:]
        printed = _substituted(body, variables, cwd)
        if printed is None:
            return None
        word = before + printed + after
    if word.startswith("~"):
        head, slash, tail = word.partition("/")
        if head == "~":
            home = variables.get("HOME")
        else:
            try:
                home = pwd.getpwnam(head[1:]).pw_dir
            except KeyError:
                home = None
        if not home:
            return None
        word = home + slash + tail
    unknown = False

    def value(match: re.Match) -> str:
        nonlocal unknown
        name = match.group("braced") or match.group("bare")
        found = variables.get(name)
        if found is None and match.group("op"):
            found = _expand(match.group("default"), variables, cwd)
        if found is None:
            unknown = True
            return ""
        return found

    expanded = _PARAMETER.sub(value, word)
    return None if unknown or "$" in expanded or "`" in expanded else expanded


def _mutation_targets(words: list[str]) -> list[tuple[str, str]]:
    """What a command line would write: (operation, path as written) for each redirection target and each path
    operand of a mutating command, past wrappers and sudo."""
    targets, plain = [], []
    index = 0
    while index < len(words):
        match = _REDIRECT.match(words[index])
        if match and not words[index].startswith("<"):
            target = match.group(1) or (words[index + 1] if index + 1 < len(words) else "")
            index += 1 if match.group(1) else 2
            if target and not target.startswith("&"):
                targets.append(("redirect", target))
            continue
        plain.append(words[index])
        index += 1
    effective = _effective(plain)
    program = os.path.basename(effective[0]) if effective else ""
    if program in MUTATING_COMMANDS:
        operands, skip = [], False
        for word in effective[1:]:
            if skip:
                skip = False
            elif word in _MUTATING_VALUE_OPTIONS:
                skip = True
            elif program == "dd":
                if word.startswith("of="):
                    operands.append(word[3:])
            elif not word.startswith("-") or word == "-":
                operands.append(word)
        if program in _SPEC_FIRST and operands and not any(w.startswith("--reference") for w in effective):
            operands = operands[1:]
        if program in _COPIERS and not (program == "install" and "-d" in effective):
            # Only the destination is written: `-t DIR`, or the last operand. (mv also removes its sources.)
            target = next((effective[i + 1] for i, w in enumerate(effective[:-1]) if w in ("-t", "--target-directory")),
                          None) or next((w.split("=", 1)[1] for w in effective if w.startswith("--target-directory=")),
                                        None)
            operands = [target] if target else operands[-1:]
        targets += [(f"exec {program}", operand) for operand in operands if operand != "-"]
    if program == "find" and any(w in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for w in effective):
        starts = []
        for word in effective[1:]:
            if word in ("-H", "-L", "-P"):
                continue
            if word.startswith(("-", "(", "!")):
                break
            starts.append(word)
        targets += [("exec find", start) for start in starts or ["."]]
    if program == "sed" and any(w == "--in-place" or w.startswith("--in-place=")
                                or (w.startswith("-") and not w.startswith("--") and "i" in w[1:]) for w in effective[1:]):
        operands = [w for w in effective[1:] if not w.startswith("-")]
        scripted = any(w in ("-e", "-f", "--expression", "--file") for w in effective)
        targets += [("exec sed -i", operand) for operand in (operands if scripted else operands[1:])]
    if program == "xargs":
        inner = [w for w in effective[1:] if not w.startswith("-")]
        if inner and (os.path.basename(inner[0]) in MUTATING_COMMANDS or os.path.basename(inner[0]) == "find"):
            targets.append((f"exec xargs {os.path.basename(inner[0])}", "$(stdin)"))  # what it writes is not known
    return targets


def _check_mutations(words: list[str], cwd: str | None, variables: dict | None = None) -> None:
    """Refuse a command line that would write under a live root -- relative paths resolved against `cwd`.

    In a script (`variables` given) each target is expanded first; one the guard cannot resolve -- an unknown
    variable, a substitution, a relative path after an unresolvable `cd` -- is refused: the guard fails closed."""
    effective = _effective([w for w in words if not _REDIRECT.match(w)])
    program = os.path.basename(effective[0]) if effective else ""
    chown = program in {"chown", "chgrp"} or (program == "install" and any(
        w in ("-o", "-g") or w.startswith(("--owner", "--group")) for w in effective))
    for operation, target in _mutation_targets(words):
        expanded = target if variables is None and target != "$(stdin)" else _expand(target, variables or {}, cwd)
        if expanded is not None and expanded.startswith("/"):
            path = expanded
        elif expanded is not None and cwd is not None:
            path = os.path.join(cwd, expanded)
        else:
            _refuse_command(words, f"{operation} to {target}, which the guard cannot resolve")
        _check(operation, _resolve(path), chown=chown and operation != "redirect")


#: How deep scripts within scripts are read; anything deeper is refused rather than run unread.
_SCRIPT_DEPTH = 8


def _check_command(words: list[str], environ=None, depth: int = 0, functions: frozenset = frozenset(),
                   cwd: str | None = "", variables: dict | None = None) -> None:
    """Privileged and service commands, kills by name, kills of everything or of a foreign process group, and writes
    under a live root (relative to `cwd`, the current directory by default; None when a script's `cd` could not be
    followed).

    `functions` are names the enclosing shell script defines for itself, which are not the system's tools;
    `variables` what it has assigned (None outside a script: an argv is not expanded)."""
    cwd = os.getcwd() if cwd == "" else cwd
    _check_mutations(words, cwd, variables)
    name, word, rest = _program(words)
    real = word not in functions and _is_real(word, environ)
    if name in PRIVILEGED_COMMANDS and _verb(rest) not in QUERY_VERBS.get(name, ()) and real:
        _refuse_command(words, f"the real {name}")
    if name in BROAD_KILL_COMMANDS and real:
        _refuse_command(words, f"{name}, which kills by name")
    if name in _ESCALATORS:
        # A stand-in sudo runs what it is given: that command is checked as if run directly.
        inner = _escalated(rest)
        _check_tmux(inner, os.environ if environ is None else environ)
        _check_nested(inner, environ, depth, functions, cwd, variables)
    if name == "kill":
        for target in _kill_targets(rest):
            if target.startswith("-") and target[1:].isdigit() and (target == "-1" or not _ours(int(target[1:]))):
                _refuse_command(words, "a kill of every process or of a process group not this run's")
    script = source = None
    if name in _SHELLS:
        # `sh -n` reads a script without running any of it: nothing to refuse.
        options = rest[:rest.index("-c")] if "-c" in rest else list(itertools.takewhile(lambda w: w.startswith("-"), rest))
        noexec = any(w.startswith("-") and not w.startswith("--") and "n" in w[1:] for w in options)
        if "-c" in rest and not noexec:
            script = rest[rest.index("-c") + 1] if rest.index("-c") + 1 < len(rest) else ""
        elif not noexec and len(rest) > len(options):
            script, source = _script_file(rest[len(options)], cwd, variables)
    elif name in (".", "source") and rest:
        script, source = _script_file(rest[0], cwd, variables)
    elif name == "eval":
        script = " ".join(rest)
    if script is not None:
        if depth >= _SCRIPT_DEPTH:
            _refuse_command(words, "a script nested too deeply to read")
        _check_script(script, environ, depth + 1, functions, cwd, variables, source)


def _check_nested(words, environ, depth, functions, cwd, variables) -> None:
    if depth >= _SCRIPT_DEPTH:
        _refuse_command(words, "a command nested too deeply to read")
    _check_command(words, environ, depth + 1, functions, cwd, variables)


def _script_file(path: str, cwd: str | None, variables: dict | None) -> tuple[str | None, str | None]:
    """The text and path of a script a shell is given to run as a file, when the guard can read it."""
    expanded = path if variables is None else _expand(path, variables, cwd)
    if expanded is None:
        return None, None
    if not expanded.startswith("/"):
        if cwd is None:
            return None, None
        expanded = os.path.join(cwd, expanded)
    try:
        with _real_open(expanded, encoding="utf-8", errors="replace") as handle:
            return handle.read(1 << 20), expanded
    except OSError:
        return None, None


_DECLARATIONS = frozenset({"export", "local", "readonly", "declare", "typeset"})


def _check_script(script: str, environ, depth: int, functions: frozenset, cwd: str | None,
                  variables: dict | None, source: str | None = None) -> None:
    """Every command of a shell script, in order, as the shell would expand it: what it assigns, where it `cd`s,
    the commands in its substitutions, and a heredoc a shell reads or that the shell expands."""
    defined = functions | set(re.findall(r"(?:^|[\s;&|{(])(?:function\s+)?([A-Za-z_][\w.-]*)\s*\(\)", script)) \
        | set(re.findall(r"\bfunction\s+([A-Za-z_][\w.-]*)", script))
    defined = frozenset(defined)
    known = dict(os.environ if environ is None else environ) if variables is None else dict(variables)
    if source:
        known.update({"0": source, "BASH_SOURCE": source, "BASH_SOURCE[0]": source})
    here, groups = cwd, []

    def one(inner: list[str], body: str | None, quoted: bool) -> None:
        nonlocal here
        if inner[:1] == ["case"]:
            inner = inner[inner.index("in") + 1:] if "in" in inner else []
            inner = list(itertools.dropwhile(lambda w: w.endswith(")") and not w.startswith("$("), inner))
        if inner and all(_ASSIGNMENT.match(w) for w in inner):
            for assignment in inner:
                key, value = _ASSIGNMENT.match(assignment).groups()
                known[key] = _expand(value, known, here)
            return
        if inner[:1] and inner[0] in _DECLARATIONS:
            for assignment in inner[1:]:
                match = _ASSIGNMENT.match(assignment)
                if match:
                    known[match.group(1)] = _expand(match.group(2), known, here)
            return
        if inner[:1] in (["for"], ["read"], ["select"]) and len(inner) > 1:
            for key in (inner[1:2] if inner[0] != "read" else [w for w in inner[1:] if not w.startswith("-")]):
                known[key] = None  # a value the script takes at run time: unknown
            return
        if inner[:1] == ["cd"]:
            target = _expand(inner[1], known, here) if len(inner) > 1 else known.get("HOME")
            if target is None or (here is None and not target.startswith("/")):
                here = None  # where later relative paths lead is no longer known
            else:
                here = target if target.startswith("/") else os.path.join(here, target)
            return
        if inner:
            _check_tmux(inner, os.environ if environ is None else environ)
            _check_nested(inner, environ, depth, defined, here, known)
        if body is not None:
            reader = _effective(inner)
            if reader and os.path.basename(reader[0]) in _SHELLS and "-c" not in reader:
                _check_nested([os.path.basename(reader[0]), "-c", body], environ, depth, defined, here, known)
            elif not quoted:
                for nested in _substitutions(body):
                    _check_nested(["sh", "-c", nested], environ, depth, defined, here, known)

    for inner, body, quoted, raw in _script_commands(script):
        for nested in _substitutions(raw):
            _check_nested(["sh", "-c", nested], environ, depth, defined, here, known)
        # A `cd` inside ( ... ) or { ... } holds until the group closes.
        inner = list(itertools.dropwhile(lambda w: w in _KEYWORDS and w not in ("(", "{"), inner))
        while inner[:1] in (["("], ["{"]):
            groups.append(here)
            inner = list(itertools.dropwhile(lambda w: w in _KEYWORDS and w not in ("(", "{"), inner[1:]))
        closes = 0
        while inner and inner[-1] in (")", "}"):
            closes += 1
            inner = inner[:-1]
        one(inner, body, quoted)
        for _ in range(closes):
            here = groups.pop() if groups else here


def _stat_fields(pid: int) -> list[str]:
    try:
        with _real_open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return []


def _ours(pid: int) -> bool:
    """Whether a process belongs to this run: a descendant of it, or started since it isolated itself."""
    fields = _stat_fields(pid)
    if not fields:
        return True  # nothing there to harm
    start = os.environ.get(RUN_START_ENV, "")
    if start.isdigit() and int(fields[19]) >= int(start):
        return True
    me, cursor = os.getpid(), pid
    for _ in range(64):
        if cursor == me:
            return True
        parent = _stat_fields(cursor)
        if not parent or int(parent[1]) <= 1:
            return False
        cursor = int(parent[1])
    return False


_real_kill = os.kill
_real_killpg = os.killpg


def _kill(pid, sig):
    if sig != 0 and (pid == -1 or (pid < -1 and not _ours(-pid))):
        _refuse_command(["kill", f"-{sig}", str(pid)], "kill of every process or of a process group not this run's")
    return _real_kill(pid, sig)  # (a refusal raises Refused, a PermissionError: what kill raises for EPERM)


def _killpg(pgid, sig):
    if sig != 0 and not _ours(pgid):
        _refuse_command(["killpg", str(pgid), str(sig)], "a signal to a process group not this run's")
    return _real_killpg(pgid, sig)


def _popen_init(self, args, *rest, **kwargs):
    argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
    words = [os.fsdecode(a) if isinstance(a, (str, bytes, os.PathLike)) else str(a) for a in argv]
    environ = kwargs.get("env")
    cwd = os.fsdecode(kwargs["cwd"]) if kwargs.get("cwd") is not None else os.getcwd()
    try:
        if len(words) == 1 and kwargs.get("shell"):
            # Every command of the script, not only its first.
            _check_command(["sh", "-c", words[0]], environ, cwd=cwd)
            try:
                words = shlex.split(words[0])
            except ValueError:
                pass
            _check_tmux(words, os.environ if environ is None else environ)
        else:
            _check_tmux(words, os.environ if environ is None else environ)
            _check_command(words, environ, cwd=cwd)
    except Refused as refused:
        # The command fails as one that cannot run would, its reason on its stderr: a suite that copes with a tool
        # failing copes with this, and the tool itself never runs.
        script = f"echo {shlex.quote(f'test boundary: refused {refused.why}')} >&2; exit {REFUSED_STATUS}"
        kwargs.pop("executable", None)
        return _real_popen_init(self, script if kwargs.get("shell") else ["/bin/sh", "-c", script], *rest, **kwargs)
    if words and os.path.basename(words[0]) == "unshare":
        _namespace_commands.append(shlex.join(words))
    return _real_popen_init(self, args, *rest, **kwargs)


def _system(command):
    try:
        words = shlex.split(os.fsdecode(command))
    except ValueError:
        words = []
    try:
        _check_tmux(words, os.environ)
        _check_command(["sh", "-c", os.fsdecode(command)])
    except Refused:
        return REFUSED_STATUS << 8  # the wait status of a command that exited 126
    return _real_system(command)


def install_guard(roots: list[str], writable: list[str]) -> None:
    """Refuse every change under `roots` (writes allowed, chowns never, under `writable`). Idempotent."""
    global _installed
    _roots[:] = [_realpath(r) for r in roots if r]
    _writable[:] = [_realpath(w) for w in writable if w]
    if _installed:
        return
    for name in ("chown", "lchown", "fchown"):
        if name in _real:
            setattr(os, name, _one_path(name, chown=True))
    for name in ("chmod", "lchmod", "fchmod", "mkdir", "rmdir", "unlink", "remove", "utime", "truncate"):
        if name in _real:
            setattr(os, name, _one_path(name))
    for name in ("rename", "replace", "symlink", "link"):
        setattr(os, name, _two_paths(name))
    os.open = _os_open
    builtins.open = _open
    io.open = _open
    subprocess.Popen.__init__ = _popen_init
    os.system = _system
    os.kill = _kill
    os.killpg = _killpg
    _installed = True


def install_guard_from_env() -> None:
    """In a child: the roots the isolating process recorded."""
    roots = [r for r in os.environ.get(LIVE_ROOTS_ENV, "").split(":") if r]
    if roots:
        install_guard(roots, [w for w in os.environ.get(ALLOW_ENV, "").split(":") if w])


# --------------------------------------------------------------------------
# the sentinel
# --------------------------------------------------------------------------


def _stores(root: str) -> list[str]:
    """Every entry of each project ticket-board store directly under `root`, recursively."""
    try:
        names = sorted(n for n in posix.listdir(root) if n.endswith("-ticket-board"))
    except OSError:
        return []
    paths, stack = [], [f"{root}/{name}" for name in reversed(names)]
    while stack:
        path = stack.pop()
        paths.append(path)
        try:
            if stat.S_ISDIR(posix.lstat(path).st_mode):
                stack.extend(f"{path}/{child}" for child in sorted(posix.listdir(path), reverse=True))
        except OSError:
            continue
    return paths


def _entries(home: str, runtime: str) -> tuple[list[str], list[str]]:
    """The chain (the home's ancestors of the stores, and the runtime directory) and every entry of the project
    ticket-board stores under ~/.local/state and the runtime directory."""
    chain = [home, f"{home}/.local", f"{home}/.local/state", runtime]
    return chain, _stores(f"{home}/.local/state") + _stores(runtime)


def _digest(path: str, size: int) -> str:
    if size > 1 << 20:
        return ""
    try:
        descriptor = posix.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return ""
    try:
        digest = hashlib.sha256()
        while chunk := posix.read(descriptor, 1 << 16):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        posix.close(descriptor)


def snapshot(home: str, runtime: str) -> dict[str, tuple]:
    """Each entry's identity. The chain: owner, group, mode and inode (live activity changes a home's times). A store
    entry: also its type, size, change time and, for a file, its contents."""
    out = {}
    chain, stores = _entries(home, runtime)
    for path in chain:
        try:
            info = posix.lstat(path)
        except OSError:
            continue
        out[path] = (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_ino)
    for path in stores:
        try:
            info = posix.lstat(path)
        except OSError:
            continue
        kind = "d" if stat.S_ISDIR(info.st_mode) else "f" if stat.S_ISREG(info.st_mode) else "l" \
            if stat.S_ISLNK(info.st_mode) else "o"
        content = _digest(path, info.st_size) if kind == "f" else ""
        if kind == "l":
            try:
                content = posix.readlink(path)
            except OSError:
                content = ""
        out[path] = (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_ino, kind,
                     info.st_size if kind != "d" else 0, info.st_ctime_ns if kind != "d" else 0, content)
    return out


#: The records a live pane's own hooks keep rewriting in its project's store, relative to that store: its pane
#: state, its session record (and what supersedes it), its resume log and its provider state -- each named for the
#: role whose directory it is in.
_LIVE_RECORD = r"(?:pane-state/{p}-[\w.-]+_\d+\.\d+\.json" \
               r"|pane-sessions/{p}-[\w.-]+_\d+\.\d+\.json(?:\.superseded(?:\.\d+)?)?" \
               r"|pane-sessions/roles/(?P<role>[\w.-]+)/{p}-(?P=role)_\d+\.\d+\.(?:json(?:\.superseded(?:\.\d+)?)?|resume\.log)" \
               r"|pane-sessions/roles/(?P<owner>[\w.-]+)/(?P=owner)\.provider-state\.json)"


def live_stores(environ) -> list[str]:
    """The ticket-board stores the live pane a run was started from writes in: from its pane variables, before
    they are removed. None when the run was not started from a pane."""
    stores = []
    for name in ("TICKET_BOARD_PANE_SESSION_DIR", "TICKET_BOARD_PANE_STATE_DIR"):
        value = environ.get(name, "")
        match = re.match(r"^(/.+?/([\w.-]+)-ticket-board)(?:/|$)", value)
        if match:
            stores.append(match.group(1))
    return sorted(set(stores))


def _live_record(path: str, stores) -> bool:
    for store in stores:
        slug = re.escape(os.path.basename(store)[:-len("-ticket-board")])
        if path.startswith(store + "/") and re.fullmatch(_LIVE_RECORD.format(p=slug), path[len(store) + 1:]):
            return True
    return False


def drift(before: dict[str, tuple], after: dict[str, tuple], uid: int, live: tuple | list = ()) -> list[str]:
    """Every change to what was there -- owner, group, mode, identity, size, change time, contents -- every deletion
    and every addition, whoever owns it.

    The one exception is the live pane's own records in its own project's store (`live`, from `live_stores`), which
    its hooks rewrite while a suite runs: those may change contents or be added, never owner, group or mode, and never
    disappear."""
    problems = []
    for path in sorted(before):
        if path not in after:
            problems.append(f"{path}: deleted")
        elif after[path] != before[path]:
            ownership = after[path][:3] != before[path][:3]
            if ownership or not _live_record(path, live):
                problems.append(f"{path}: {_describe(before[path], after[path])}")
    for path in sorted(set(after) - set(before)):
        if after[path][0] != uid or not _live_record(path, live):
            problems.append(f"{path}: new, owned by {after[path][0]}:{after[path][1]}")
    return problems


def _describe(old: tuple, new: tuple) -> str:
    names = ("owner", "group", "mode", "inode", "type", "size", "ctime", "contents")
    return ", ".join(f"{name} {old[i]!r} -> {new[i]!r}" if name != "contents" else "contents changed"
                     for i, name in enumerate(names) if i < len(old) and i < len(new) and old[i] != new[i])


def _start_ticks(pid: int) -> str:
    try:
        with _real_open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        return ""


def tmux_snapshot(tmux_dir: str) -> dict[str, int]:
    """Each tmux server listening in `tmux_dir`, by socket name: the inode of its listening socket.

    Read from the kernel's table of listening sockets, never by contacting a server, and never from the files: tmux
    leaves its socket file behind when the server exits. A server that stops drops out; one that restarts changes
    inode."""
    out = {}
    try:
        with _real_open("/proc/net/unix") as handle:
            lines = handle.read().splitlines()[1:]
    except OSError:
        return out
    prefix = tmux_dir.rstrip("/") + "/"
    for line in lines:
        fields = line.split(None, 7)
        if len(fields) == 8 and fields[7].startswith(prefix) and "/" not in fields[7][len(prefix):] \
                and int(fields[3], 16) & 0x10000:  # __SO_ACCEPTCON: listening
            out[fields[7][len(prefix):]] = int(fields[6])
    return out

class Sentinel:
    def __init__(self, home: str, runtime: str, uid: int, *, tmux: str = "", tmux_dir: str | None = None,
                 live: tuple | list = ()) -> None:
        self.home, self.runtime, self.uid, self.live = home, runtime, uid, tuple(live)
        self.before = snapshot(home, runtime)
        self.tmux_dir = tmux_dir if tmux_dir is not None else account_tmux_dir(uid)
        self.sockets = tmux_snapshot(self.tmux_dir)
        # The live server the run was started from, when it was started from one: its socket, pid and start.
        socket, _, rest = tmux.partition(",")
        pid = rest.split(",")[0] if rest else ""
        self.server = (socket, int(pid), _start_ticks(int(pid))) if socket and pid.isdigit() else None

    def problems(self) -> list[str]:
        problems = drift(self.before, snapshot(self.home, self.runtime), self.uid, self.live)
        now = tmux_snapshot(self.tmux_dir)
        problems += [f"{self.tmux_dir}/{name}: tmux server {'gone' if name not in now else 'replaced'}"
                     for name, inode in sorted(self.sockets.items()) if now.get(name) != inode]
        if self.server is not None:
            socket, pid, ticks = self.server
            if not ticks or _start_ticks(pid) != ticks:
                problems.append(f"tmux server {pid} on {socket}, which this run was started from, is gone")
        return problems


def _verify_at_exit(sentinel: Sentinel) -> None:
    problems = sentinel.problems()
    for _ in range(5):
        if problems:
            break
        # A server told to stop exits, and removes its socket, after the client that told it has returned.
        time.sleep(0.2)
        problems = sentinel.problems()
    if not problems:
        return
    _report("test boundary: LIVE DRIFT while running " + shlex.join(sys.argv) + "\n"
            + "".join(f"  {p}\n" for p in problems[:50])
            + "".join(f"  namespace command: {c}\n" for c in _namespace_commands))
    sys.stdout.flush()
    os._exit(97)


def _remove(root: Path) -> None:
    """Remove the disposable root; give back first anything a namespace child left owned by a subordinate id."""
    try:
        shutil.rmtree(root)
        return
    except OSError:
        pass
    if shutil.which("unshare"):
        subprocess.run(["unshare", "--user", "--map-auto", "--map-root-user", "chown", "-R", "-h", "0:0", str(root)],
                       capture_output=True, check=False)
    shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# isolation
# --------------------------------------------------------------------------


def isolate() -> Path | None:
    """Put this run behind the boundary; returns its disposable root, or None when it made none.

    A run from a pane -- HOME unset or live -- gets the whole treatment: a
    disposable environment, the guard and the sentinel. An environment a suite
    prepared on purpose for a child it starts (HOME already a fixture's) is kept
    as given: only a variable pointing into a live root is removed, and the guard
    is installed. The live roots come from the account database, never from the
    environment, so a fixture's HOME is never mistaken for one.
    """
    existing = os.environ.get(DISPOSABLE_ENV, "")
    if existing:
        install_guard_from_env()
        return Path(existing)
    uid = os.getuid()
    os.environ.setdefault(INVOKER_UID_ENV, str(uid))
    # Processes started from here on are this run's: a signal to their group is the run's own business.
    os.environ.setdefault(RUN_START_ENV, (_stat_fields(os.getpid()) or [""] * 20)[19])
    live_tmux = os.environ.get("TMUX", "")
    live = live_stores(os.environ)  # read before the pane variables are removed
    home = pwd.getpwuid(uid).pw_dir
    runtime = f"/run/user/{uid}"
    roots = [home, runtime]
    resolved = [_realpath(r) for r in roots]
    inherited = os.environ.get("HOME", "")
    if inherited and not _under(_realpath(inherited), resolved):
        for name in list(os.environ):
            value = os.environ[name]
            if (name in SCRUBBED_NAMES or name.startswith(SCRUBBED_PREFIXES)) and value.startswith("/") \
                    and _under(_realpath(value), resolved):
                del os.environ[name]
        # And never the account's own tmux: TMUX naming it is dropped, and a TMUX_TMPDIR is always set, since
        # without one tmux falls back to the account's default socket (SYRD-627).
        if os.environ.get("TMUX", "").startswith(account_tmux_dir(uid) + "/"):
            del os.environ["TMUX"]
        os.environ.pop("TMUX_PANE", None)
        tmux_tmpdir = os.environ.get("TMUX_TMPDIR", "")
        if not tmux_tmpdir or _under(_realpath(tmux_tmpdir), resolved) or tmux_tmpdir.rstrip("/") == "/tmp":
            private = tempfile.mkdtemp(prefix="sytx.")
            os.environ["TMUX_TMPDIR"] = private
            atexit.register(shutil.rmtree, private, True)
        install_guard(roots, [str(REPOSITORY)])
        return None
    parent = os.environ.get("TMPDIR") or "/tmp"
    if _under(_realpath(parent), resolved):
        parent = "/tmp"  # a TMPDIR under a live root is no place for a disposable one
    # Short names: a PostgreSQL socket path made under it must stay within the 107 bytes a Unix socket allows.
    root = Path(tempfile.mkdtemp(prefix="sytb.", dir=parent))
    names = {"HOME": "h", "TMPDIR": "t", "XDG_CONFIG_HOME": "c", "XDG_STATE_HOME": "s", "XDG_CACHE_HOME": "k",
             "XDG_DATA_HOME": "d", "XDG_RUNTIME_DIR": "r", "TMUX_TMPDIR": "x"}
    for leaf in names.values():
        (root / leaf).mkdir(mode=0o700)
    # TMPDIR stands in for /tmp, which other identities a suite runs (a namespace's stand-in owner) can reach: the
    # root is traversable and TMPDIR sticky and open, as /tmp is. The home, state and runtime leaves stay private.
    os.chmod(root, 0o711)
    os.chmod(root / names["TMPDIR"], 0o1777)
    for name in list(os.environ):
        if name in SCRUBBED_NAMES or name.startswith(SCRUBBED_PREFIXES):
            del os.environ[name]
    writable = [str(REPOSITORY)]
    os.environ.update({name: str(root / leaf) for name, leaf in names.items()})
    os.environ.update({
        LIVE_ROOTS_ENV: ":".join(roots), ALLOW_ENV: ":".join(writable), DISPOSABLE_ENV: str(root),
        "PYTHONPATH": os.pathsep.join([str(SITE_DIR), *filter(None, [os.environ.get("PYTHONPATH", "")])]),
    })
    for name, value in (("GIT_AUTHOR_NAME", "Switchyard Test"), ("GIT_AUTHOR_EMAIL", "test@switchyard.invalid"),
                        ("GIT_COMMITTER_NAME", "Switchyard Test"), ("GIT_COMMITTER_EMAIL", "test@switchyard.invalid")):
        os.environ.setdefault(name, value)
    tempfile.tempdir = None
    install_guard(roots, writable)
    sentinel = Sentinel(home, runtime, uid, tmux=live_tmux, live=live)
    atexit.register(_remove, root)
    atexit.register(_verify_at_exit, sentinel)  # atexit runs last-registered first: verify, then remove
    return root

def main(argv: list[str]) -> int:
    """`live_boundary.py --run -- <command>`: run a non-Python suite behind the same boundary (its shell suites)."""
    if len(argv) < 3 or argv[0] != "--run" or argv[1] != "--":
        sys.stderr.write("usage: live_boundary.py --run -- <command> [args...]\n")
        return 2
    isolate()
    return subprocess.run(argv[2:], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
