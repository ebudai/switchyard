"""No test reads or changes this host's Switchyard state, or runs its privileged tools (SYRD-808).

Three suites reached live host state from an ordinary test run: one wrote the
real /etc/switchyard/provision/mefp after reading mefp's live unit, one ran the
real sudo and read the host's project registry, and one read the live pgu
board's unit and /proc entry. Permissions refused each write; run as root, the
first would have overwritten a live tenant's records.

A suite calls `install()` before it imports any production module and
`assert_untouched()` as its last check. From `install()` on, this process:

* refuses any open, stat, listing, access check, readlink or chdir of a path
  under HOST_ROOTS, or of another process's /proc entry (this process's own and
  its descendants' are allowed), resolving symlinks with the posix functions so
  a link from a fixture into /etc/switchyard counts too;
* refuses to start the real privileged and service tools (PRIVILEGED), queries
  included -- `systemctl show` of a live unit is exactly how a board's pid was
  found -- and any command naming a path under HOST_ROOTS. A stand-in a suite
  puts on PATH, outside the system prefixes, is not refused. Each tool is
  resolved with the PATH that will apply to it, not the caller's: through
  env(1)'s -i, -u and assignments (none left means the default path), through
  a shell -c script's assignments, `export` and `command -p`, and through
  wrappers that pass their environment on (Audit of 32f8b75b);
* refuses to start a Python child that would run without the guard: one
  given -I, -E or -S, or an environment that lost the guard's site or record.

Each refusal raises PermissionError and is also recorded, because product code
often treats an unreadable path as absent and carries on: `assert_untouched()`
fails the suite naming every recorded reach and the test it came from, and a
process that exits without calling it after a refusal exits 98.

Programs that are not Python -- a shell script a test starts, and whatever it
runs -- meet refusing shims instead: `install()` puts a directory of them first
on PATH -- ahead of every system directory, behind any stand-in a suite put
there -- one for each PRIVILEGED tool, and every child gets it the same way. A shim runs nothing: it appends its argv to the same record and
exits 126. The suite's account has NOPASSWD sudoers rules on this host, so a
stray `sudo` from a grandchild would otherwise run (Director, SYRD-808). A
program that names a tool by absolute path bypasses the shims; the guarded
suites and their fixtures name none.

The guard refuses to start as root: these suites are guard-only, never run as
root, and not for sweeps that do.

Python children inherit the guard: `install()` puts host_guard_site/ (a
sitecustomize) first on PYTHONPATH and names a shared record in
SWITCHYARD_TEST_HOST_GUARD_LOG, and every child started through subprocess,
os.system, os.exec* or os.posix_spawn* gets both, even with an `env=` of its
own. `assert_untouched()` reads that record, so a child's reach fails the
suite too. Other programs a child runs are not interposed: suites that run
shell or C tooling against host paths still belong in a sandbox that hides
these roots.
"""

from __future__ import annotations

import atexit
import builtins
import contextlib
import errno
import io
import os
import posix
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

#: Where this host keeps Switchyard's records, units, staged tooling and releases.
HOST_ROOTS = ("/etc/switchyard", "/etc/systemd/system", "/usr/local/lib/systemd/system", "/run/systemd/system",
              "/opt/switchyard", "/usr/local/lib/switchyard", "/var/lib/switchyard", "/var/lib/systemd/linger",
              "/run/switchyard", "/run/switchyard-desktop", "/run/switchyard-board-supervisor")
#: Privilege escalation, service and session control. Unlike live_boundary, queries are refused too.
PRIVILEGED = frozenset({"sudo", "pkexec", "su", "doas", "runuser", "setpriv", "systemctl", "loginctl", "machinectl",
                        "busctl", "systemd-run", "journalctl"})
#: Where the real tools live; a stand-in a suite made lives in its own fixture.
SYSTEM_PREFIXES = ("/usr/", "/bin/", "/sbin/", "/lib", "/opt/", "/etc/", "/snap/")
EXIT_UNCHECKED = 98
LOG_ENV = "SWITCHYARD_TEST_HOST_GUARD_LOG"
SHIMS_ENV = "SWITCHYARD_TEST_HOST_GUARD_SHIMS"
SHIM_EXIT = 126
SITE_DIR = str(Path(__file__).resolve().parent / "host_guard_site")

_state = {"installed": False, "asserted": False, "root": False}
_real_open = builtins.open
_real = {name: getattr(os, name) for name in ("stat", "lstat", "open", "listdir", "scandir", "access", "readlink",
                                               "chdir", "execv", "execve", "posix_spawn", "posix_spawnp")
         if hasattr(os, name)}
_real_popen_init = subprocess.Popen.__init__
_real_system = os.system


def _realpath(path: str, depth: int = 0) -> str:
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
        if (info.st_mode & 0o170000) == 0o120000 and depth < 40:
            target = posix.readlink(candidate)
            out = _realpath(target if target.startswith("/") else out.rstrip("/") + "/" + target, depth + 1)
        else:
            out = candidate
    return out


def _absolute(path: object, dir_fd: int | None = None) -> str:
    if isinstance(path, int):
        return ""  # an open descriptor was judged when it was opened
    try:
        text = os.fsdecode(path)  # type: ignore[arg-type]
    except TypeError:
        return ""
    if not text.startswith("/"):
        base = posix.readlink(f"/proc/self/fd/{dir_fd}") if dir_fd is not None else posix.getcwd()
        text = base.rstrip("/") + "/" + text
    return text


def _under(path: str, roots) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _descends_from_us(pid: int) -> bool:
    me = os.getpid()
    for _ in range(64):
        if pid == me:
            return True
        if pid <= 1:
            return False
        try:
            with _real_open(f"/proc/{pid}/stat", "rb") as handle:
                fields = handle.read().rsplit(b")", 1)[1].split()
            pid = int(fields[1])
        except (OSError, IndexError, ValueError):
            return False
    return False


def _host_path(path: str) -> str:
    """The host-state path `path` reaches, after symlinks, or ''."""
    if not path:
        return ""
    for candidate in (os.path.normpath(path), _realpath(path)):
        if _under(candidate, HOST_ROOTS):
            return candidate
        parts = candidate.split("/")
        if len(parts) > 2 and parts[1] == "proc" and parts[2].isdigit() and not _descends_from_us(int(parts[2])):
            return candidate
    return ""


def _attribution() -> str:
    frames = [f for f in traceback.extract_stack()[:-3] if "/tests/" in f.filename and not f.filename.endswith(
        "host_state_guard.py")]
    named = [f for f in frames if f.name.startswith("test_")] or frames
    where = f"{os.path.basename(named[-1].filename)}:{named[-1].lineno} {named[-1].name}" if named else "?"
    product = [f for f in traceback.extract_stack()[:-3] if "/scripts/" in f.filename]
    if product:
        where += f" via {product[-1].filename.split('/scripts/', 1)[1]}:{product[-1].lineno} {product[-1].name}"
    return where


def _refuse(what: str) -> None:
    line = f"{what} (from {_attribution()}, pid {os.getpid()}: {shlex.join(sys.argv[:3])[-120:]})"
    log = os.environ.get(LOG_ENV, "")
    if log:
        with _real_open(log, "a", encoding="utf-8") as handle:
            handle.write(line.replace("\n", " ") + "\n")
    sys.stderr.write(f"host state guard: REFUSED {line}\n")
    if os.environ.get("SWITCHYARD_TEST_HOST_GUARD_STACK"):
        sys.stderr.write("".join(traceback.format_stack(limit=24)[:-2]))
    raise PermissionError(errno.EACCES, f"host state guard: a test reached host state: {what}")


def _check_path(operation: str, path: object, dir_fd: int | None = None) -> None:
    reached = _host_path(_absolute(path, dir_fd))
    if reached:
        _refuse(f"{operation} {reached}")


def _guard_path(name: str):
    real = _real[name]

    def guarded(path=".", *args, **kwargs):
        _check_path(name, path, kwargs.get("dir_fd"))
        return real(path, *args, **kwargs)

    guarded.__wrapped__ = real  # type: ignore[attr-defined]
    return guarded


def _open(file, *args, **kwargs):
    _check_path("open", file)
    return _real_open(file, *args, **kwargs)


def _words(args) -> list[str]:
    if isinstance(args, (str, bytes)):
        text = os.fsdecode(args)
        try:
            return shlex.split(text)
        except ValueError:
            return text.split()
    try:
        return [os.fsdecode(a) for a in args]
    except TypeError:
        return []


#: Programs that run the rest of their argv with the environment they were given (their own options are skipped).
WRAPPERS = frozenset({"setsid", "nohup", "nice", "ionice", "stdbuf", "timeout", "chrt", "taskset", "unbuffer", "exec",
                      "time", "flock"})
SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash", "mksh"})
PYTHON = re.compile(r"python(\d+(\.\d+)*)?$")
#: The search path a program gets when its environment names none (glibc's execvp; a shell's default is the same).
DEFAULT_PATH = os.confstr("CS_PATH") if "CS_PATH" in os.confstr_names else os.defpath


def _resolve(word: str, env: dict) -> str:
    """The file `word` runs as, under the PATH that applies to it -- none means the default path, never the caller's."""
    if "/" in word:
        return word
    return shutil.which(word, path=env.get("PATH") or DEFAULT_PATH) or ""


def _env_options(words: list[str], at: int, env: dict) -> tuple[int, dict]:
    """env(1)'s options and assignments from `at`: what it clears, unsets and sets. Returns where its command starts."""
    env = dict(env)
    while at < len(words):
        word = words[at]
        if word in ("-i", "--ignore-environment", "-"):
            env = {}
        elif word in ("-u", "--unset") and at + 1 < len(words):
            env.pop(words[at + 1], None)
            at += 1
        elif word.startswith("--unset="):
            env.pop(word.split("=", 1)[1], None)
        elif word in ("-S", "--split-string") and at + 1 < len(words):
            words[at + 1:at + 2] = _words(words[at + 1])
        elif word in ("-C", "--chdir") and at + 1 < len(words):
            at += 1
        elif word.startswith("-"):
            pass
        elif "=" in word and not word.startswith("="):
            key, value = word.split("=", 1)
            env[key] = value
        else:
            break
        at += 1
    return at, env


def _python_guarded(words: list[str], at: int, env: dict) -> str:
    """Why the Python interpreter at `words[at]` would start without the guard, or ''."""
    for word in words[at + 1:]:
        if word in ("-c", "-m") or not word.startswith("-") or word.startswith("--"):
            break
        letters = word[1:]
        for index, letter in enumerate(letters):
            if letter in "IES":
                return f"-{letter} keeps sitecustomize from loading"
            if letter in "cm" or (letter in "WX" and index == len(letters) - 1):
                break
    path = env.get("PYTHONPATH", "").split(os.pathsep)
    if SITE_DIR not in path or not env.get(LOG_ENV):
        return "its environment does not carry the guard"
    return ""


def _shell_commands(script: str, env: dict, depth: int) -> None:
    """A shell -c script, simple command by simple command: assignments and `export` change what follows,
    `command -p` asks for the default path, and each command is judged as an argv under that environment."""
    try:
        lexer = shlex.shlex(script, posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError:
        tokens = script.split()
    env = dict(env)
    command: list[str] = []
    for token in [*tokens, ";"]:
        if token and set(token) <= set(";&|()\n"):
            if command:
                assignments = 0
                while assignments < len(command) and "=" in command[assignments] and \
                        re.match(r"[A-Za-z_][A-Za-z0-9_]*=", command[assignments]):
                    assignments += 1
                local = dict(env)
                for word in command[:assignments]:
                    key, value = word.split("=", 1)
                    local[key] = value
                rest = command[assignments:]
                if not rest:
                    env = local  # an assignment alone changes the shell's own environment
                elif rest[0] in ("export", "declare", "typeset", "readonly"):
                    for word in rest[1:]:
                        if "=" in word:
                            key, value = word.split("=", 1)
                            env[key] = value
                elif rest[0] in ("unset",):
                    for word in rest[1:]:
                        env.pop(word, None)
                elif rest[0] == "command" and len(rest) > 1 and rest[1] == "-p":
                    _check_argv(rest[2:], {**local, "PATH": DEFAULT_PATH}, depth + 1)
                else:
                    _check_argv(rest[1:] if rest[0] == "command" else rest, local, depth + 1)
            command = []
        else:
            command.append(token)


def _check_argv(words: list[str], env: dict, depth: int = 0) -> None:
    if depth > 8 or not words:
        return
    words = list(words)
    shown = shlex.join(words)
    at = 0
    while at < len(words):  # find the program, through env(1) and wrappers that pass their environment on
        name = os.path.basename(words[at])
        if name == "env":
            at, env = _env_options(words, at + 1, env)
        elif name in WRAPPERS:
            at += 1
            while at < len(words) and (words[at].startswith("-") or words[at].replace(".", "").isdigit()):
                at += 1
        else:
            break
    for word in words:
        if word.startswith("/") or word.startswith("."):
            reached = _host_path(_absolute(word))
            if reached:
                _refuse(f"exec naming {reached}: {shown}")
    for index, word in enumerate(words[at:], start=at):
        name = os.path.basename(word)
        if name in PRIVILEGED:
            found = _resolve(word, env)
            if found and _realpath(os.path.abspath(found)).startswith(SYSTEM_PREFIXES):
                _refuse(f"exec of the real {name}: {shown}")
    if at >= len(words):
        return
    program = os.path.basename(words[at])
    if PYTHON.fullmatch(program):
        why = _python_guarded(words, at, env)
        if why:
            _refuse(f"an unguarded Python child ({why}): {shown}")
    elif program in SHELLS:
        for index in range(at + 1, len(words) - 1):
            option = words[index]
            if option.startswith("-") and not option.startswith("--") and "c" in option[1:]:
                _shell_commands(words[index + 1], env, depth)
                break
            if not option.startswith("-"):
                break


def _check_command(words: list[str], environ=None) -> None:
    _check_argv(words, dict(environ if environ is not None else os.environ))


def _carried(env):
    """A child's environment with the guard's record and site added, whatever it was given."""
    if env is None:
        return None
    env = dict(env)
    for key in (LOG_ENV, SHIMS_ENV):
        if os.environ.get(key):
            env[key] = os.environ[key]
    path = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and p != SITE_DIR]
    env["PYTHONPATH"] = os.pathsep.join([SITE_DIR, *path])
    if os.environ.get(SHIMS_ENV):
        env["PATH"] = _with_shims(str(env.get("PATH", os.defpath)))
    return env


def _with_shims(search: str) -> str:
    """`search` with the shims just before its first system directory: a stand-in a suite put ahead of those still
    answers first, and the real tool never does."""
    shims = os.environ.get(SHIMS_ENV, "")
    parts = [p for p in search.split(os.pathsep) if p and p != shims]
    at = next((i for i, p in enumerate(parts) if (p.rstrip("/") + "/").startswith(SYSTEM_PREFIXES)), len(parts))
    return os.pathsep.join([*parts[:at], shims, *parts[at:]])


def _popen_init(self, args, *rest, **kwargs):  # noqa: C901
    if len(rest) >= 10:  # env passed positionally: bufsize, executable, stdin, stdout, stderr, preexec_fn,
        rest = (*rest[:9], _carried(rest[9]), *rest[10:])  # close_fds, shell, cwd, env
    elif "env" in kwargs:
        kwargs["env"] = _carried(kwargs["env"])
    words = _words(args)
    if kwargs.get("shell") or (len(rest) >= 8 and rest[7]):
        words = ["/bin/sh", "-c", args if isinstance(args, str) else shlex.join(words)]
    if kwargs.get("executable"):
        words = [os.fsdecode(kwargs["executable"]), *words[1:]]
    _check_command(words, kwargs.get("env"))
    return _real_popen_init(self, args, *rest, **kwargs)


def _system(command):
    _check_command(["/bin/sh", "-c", os.fsdecode(command)])
    return _real_system(command)


def _exec(name: str):
    real = _real[name]

    def guarded(path, args, *rest, **kwargs):
        if rest and name in ("execve", "posix_spawn", "posix_spawnp"):
            rest = (_carried(rest[0]), *rest[1:])
            env = rest[0]
        else:
            env = None
        _check_command([os.fsdecode(path), *_words(args)[1:]], env)
        return real(path, args, *rest, **kwargs)

    guarded.__wrapped__ = real  # type: ignore[attr-defined]
    return guarded


def reached() -> list[str]:
    """Every reach this run recorded: in this process, in its Python children, and by the shims."""
    log = os.environ.get(LOG_ENV, "")
    try:
        with _real_open(log, encoding="utf-8") as handle:
            return [line.rstrip("\n") for line in handle if line.strip()]
    except OSError:
        return []


def _unchecked_exit() -> None:
    found = reached()
    log = os.environ.get(LOG_ENV, "")
    if log:
        with contextlib.suppress(OSError):
            posix.unlink(log)
    shims = os.environ.get(SHIMS_ENV, "")
    if shims:
        shutil.rmtree(shims, ignore_errors=True)
    if found and not _state["asserted"]:
        sys.stderr.write("host state guard: the run reached host state and never checked it:\n"
                         + "".join(f"  {line}\n" for line in found))
        sys.stderr.flush()
        os._exit(EXIT_UNCHECKED)


def _make_shims() -> str:
    """A directory of refusing stand-ins for every PRIVILEGED tool: each records its argv in the run's record and
    exits SHIM_EXIT, running nothing."""
    directory = tempfile.mkdtemp(prefix="host-state-guard-shims.")
    for name in sorted(PRIVILEGED):
        path = os.path.join(directory, name)
        with _real_open(path, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\n'
                         '# host_state_guard (SYRD-808): a privileged tool reached from a test run. Nothing is run.\n'
                         f'printf "shim refused %s %s (pid %s, parent %s)\\n" "{name}" "$*" "$$" "$PPID" '
                         f'>> "${{{LOG_ENV}:-/dev/null}}"\n'
                         f'echo "host state guard: refused {name} $*" >&2\n'
                         f'exit {SHIM_EXIT}\n')
        os.chmod(path, 0o755)
    return directory


def install() -> None:
    """Guard this process from here on. Idempotent."""
    if _state["installed"]:
        return
    if not os.environ.get(LOG_ENV) and os.getuid() == 0:
        raise SystemExit("host state guard: this suite is guard-only and never runs as root (SYRD-808)")
    for name in ("stat", "lstat", "listdir", "scandir", "access", "readlink", "chdir"):
        if name in _real:
            setattr(os, name, _guard_path(name))
    os.open = _guard_path("open")
    builtins.open = _open
    io.open = _open
    subprocess.Popen.__init__ = _popen_init
    os.system = _system
    for name in ("execv", "execve", "posix_spawn", "posix_spawnp"):
        if name in _real:
            setattr(os, name, _exec(name))
    if not os.environ.get(LOG_ENV):  # the suite's own process: it owns the record and checks it
        handle, log = tempfile.mkstemp(prefix="host-state-guard.", suffix=".log")
        posix.close(handle)
        os.environ[LOG_ENV] = log
        os.environ[SHIMS_ENV] = _make_shims()
        _state["root"] = True
        atexit.register(_unchecked_exit)
    path = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p and p != SITE_DIR]
    os.environ["PYTHONPATH"] = os.pathsep.join([SITE_DIR, *path])
    if os.environ.get(SHIMS_ENV):
        os.environ["PATH"] = _with_shims(os.environ.get("PATH", os.defpath))
    _state["installed"] = True


def assert_untouched(suite: str) -> None:
    """The suite's last check: nothing it ran reached this host's Switchyard state or privileged tools."""
    _state["asserted"] = True
    found = reached()
    if found:
        raise AssertionError(f"FAILED CHECK: {suite} reached host state {len(found)} time(s): " + "; ".join(found[:20]))
