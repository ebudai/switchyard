"""GitHub Copilot CLI as a Switchyard runtime: what is specific to it, in one place (SYRD-634).

The runtime is the standalone `copilot` executable (`@github/copilot`, GitHub's
installer, or Homebrew), never the retired `gh copilot` extension. Everything
below was measured on 1.0.101 and is pinned by the recordings in
tests/fixtures/copilot-1.0.101/ (SOURCE.md says how they were made):

* **State** lives in `$COPILOT_HOME`, else `~/.copilot`. A session is
  `session-state/<id>/` with its transcript at `events.jsonl`. Folder trust is
  `trustedFolders` in `config.json`, a file the CLI writes with a leading
  `//` comment line.
* **Capabilities** are read from the installed binary's own `--help`, which
  needs no network and no login. It says whether `--model` exists and which
  `--reasoning-effort` levels this version takes, so argv never assumes a flag
  the installed version lacks.
* **Models** are listed by `copilot help config` under `model`: this version's
  built-in catalog, not the account's entitlements. The CLI also accepts names
  outside it, with a warning.
* **Screens** are classified from recorded text: folder trust, the /login
  prompt and its account picker, a tool permission prompt, the Working status
  line, the ready input, and a fatal start (an unknown --resume id, or a loader
  with no platform package).
* **Install shape.** npm's `copilot` is `npm-loader.js`, a Node script that runs
  the platform package's self-contained native executable
  (`@github/copilot-<platform>-<arch>/copilot`). Promotion therefore promotes
  that executable, never the loader, which needs Node and its node_modules
  beside it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

COPILOT_HOME_ENV = "COPILOT_HOME"
SESSION_STATE_DIR = "session-state"
TRANSCRIPT_NAME = "events.jsonl"
#: What a person runs to sign in; `copilot login` is the CLI's own OAuth flow.
LOGIN_COMMAND = ("copilot", "login")
#: Environment tokens Copilot itself prefers to a stored login, in its order.
TOKEN_ENV = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
HELP_TIMEOUT_SECONDS = 15.0
#: The first line of `copilot --version` from the standalone CLI: "GitHub Copilot CLI 1.0.101."
VERSION_PREFIX = "GitHub Copilot CLI "


def copilot_home(home: Path, env: Mapping[str, str] | None = None, workdir: str | Path | None = None) -> Path:
    """Where Copilot keeps everything for this environment: $COPILOT_HOME, else <home>/.copilot.

    Measured on 1.0.101 (SYRD-634 Audit): with COPILOT_HOME set, the hooks,
    personal skills, trustedFolders and sessions are all read there, and
    ~/.copilot is not consulted. Copilot expands nothing in the value: a
    relative one, `~/x` included, is a directory under the folder it runs in
    (it wrote `work/~/x/`), so it is taken from the role's worktree.
    """
    configured = str((env or {}).get(COPILOT_HOME_ENV) or "").strip()
    if not configured:
        return Path(home) / ".copilot"
    path = Path(configured)
    return path if path.is_absolute() else Path(workdir or home) / path


def configured_homes(home: Path, roles: Iterable[tuple[Mapping[str, str], str | Path]]) -> list[Path]:
    """The Copilot homes a set of (env, workdir) roles configure other than the account default, in order."""
    found: list[Path] = []
    for env, workdir in roles:
        if str((env or {}).get(COPILOT_HOME_ENV) or "").strip():
            path = copilot_home(home, env, workdir)
            if path != Path(home) / ".copilot" and path not in found:
                found.append(path)
    return found


def session_transcript(home: Path, session_id: str, env: Mapping[str, str] | None = None,
                       workdir: str | Path | None = None) -> Path:
    return copilot_home(home, env, workdir) / SESSION_STATE_DIR / session_id / TRANSCRIPT_NAME


def session_store_exists(home: Path, session_id: str, env: Mapping[str, str] | None = None,
                         workdir: str | Path | None = None) -> tuple[bool, str]:
    """Whether Copilot holds this session, as a resume of it needs: its transcript file."""
    if not session_id or "/" in session_id or session_id in {".", ".."}:
        return False, str(copilot_home(home, env, workdir) / SESSION_STATE_DIR)
    path = session_transcript(home, session_id, env, workdir)
    return path.is_file(), str(path)


def read_config(home: Path, env: Mapping[str, str] | None = None, workdir: str | Path | None = None) -> dict[str, Any]:
    """`config.json`, which Copilot writes with `//` comment lines; {} when absent or unreadable."""
    try:
        text = (copilot_home(home, env, workdir) / "config.json").read_text(encoding="utf-8")
        parsed = json.loads("\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//")))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def workdir_is_trusted(workdir: str | Path, home: Path, env: Mapping[str, str] | None = None) -> bool:
    """Whether Copilot will open `workdir` without asking: it, or a folder above it, is in trustedFolders."""
    folders = read_config(home, env, workdir).get("trustedFolders")
    if not isinstance(folders, list):
        return False
    wanted = Path(os.path.abspath(Path(workdir).expanduser()))
    for folder in folders:
        if not isinstance(folder, str) or not folder.strip():
            continue
        trusted = Path(os.path.abspath(Path(folder).expanduser()))
        if wanted == trusted or trusted in wanted.parents:
            return True
    return False


@dataclass(frozen=True)
class Capabilities:
    """What the installed CLI's own help says it takes."""

    model_flag: bool
    effort_levels: tuple[str, ...]
    resume_flag: bool
    source: str


_EFFORT_LINE = re.compile(r"--reasoning-effort\s+<level>.*?\[possible values:\s*([^\]]+)\]", re.S)


def parse_help(text: str, *, source: str = "copilot --help") -> Capabilities:
    effort = _EFFORT_LINE.search(text)
    levels = tuple(level.strip() for level in effort.group(1).split(",") if level.strip()) if effort else ()
    return Capabilities(model_flag=bool(re.search(r"^\s+--model <", text, re.M)), effort_levels=levels,
                        resume_flag=bool(re.search(r"--resume \[", text)), source=source)


_CACHE: dict[tuple[str, int, int], Capabilities | None] = {}


def installed_capabilities(binary: str, *, runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
                           env: Mapping[str, str] | None = None) -> Capabilities | None:
    """The capabilities of the copilot this launch runs, or None when its help cannot be read.

    Cached per executable path, size and mtime: an upgraded binary is asked again.
    """
    import shutil

    resolved = shutil.which(binary, path=(env or os.environ).get("PATH")) if os.sep not in binary else binary
    if not resolved:
        return None
    try:
        info = os.stat(resolved)
    except OSError:
        return None
    key = (os.path.realpath(resolved), info.st_size, int(info.st_mtime))
    if runner is None and key in _CACHE:
        return _CACHE[key]
    try:
        # The rest of the environment as it is: the native binary unpacks itself
        # under $HOME/.cache on first run and exits 1 without a HOME to do it in.
        done = (runner or subprocess.run)([resolved, "--help"], capture_output=True, text=True,
                                          timeout=HELP_TIMEOUT_SECONDS, env={**os.environ, **(env or {})})
    except (OSError, subprocess.SubprocessError):
        found = None
    else:
        found = parse_help(done.stdout, source=f"{resolved} --help") if done.returncode == 0 and done.stdout else None
    if runner is None:
        _CACHE[key] = found
    return found


def model_args(model: str, flag: str, capabilities: Capabilities | None) -> tuple[list[str], str]:
    """`<flag> <model>` when this copilot is known to take --model; otherwise nothing, and why.

    Fails closed: a help that cannot be read says nothing about --model, so the
    model is not passed rather than assumed (SYRD-634 Audit).
    """
    if not model:
        return [], ""
    if capabilities is None:
        return [], (f"model {model!r} is not passed: the installed copilot's --help could not be read, so "
                    "whether it takes --model is unknown")
    if not capabilities.model_flag:
        return [], f"model {model!r} is not passed: the installed copilot has no --model ({capabilities.source})"
    return [flag, model], ""


def effort_args(level: str, capabilities: Capabilities | None) -> tuple[list[str], str]:
    """`--reasoning-effort <level>` when this copilot takes that level; otherwise nothing, and why."""
    if not level:
        return [], ""
    if capabilities is None:
        return [], (f"effort {level!r} is not passed: the installed copilot's --help could not be read, so "
                    "whether it takes --reasoning-effort is unknown")
    if not capabilities.effort_levels:
        return [], f"effort {level!r} is not passed: the installed copilot has no --reasoning-effort"
    if level not in capabilities.effort_levels:
        return [], (f"effort {level!r} is not passed: the installed copilot takes --reasoning-effort "
                    f"{', '.join(capabilities.effort_levels)}")
    return ["--reasoning-effort", level], ""


def parse_model_catalog(help_config: str) -> tuple[str, ...]:
    """The quoted names under the `model` setting of `copilot help config`, in order."""
    match = re.search(r"^\s*`model`:.*?\n((?:\s+- \"[^\"]+\"\n?)+)", help_config, re.M)
    return tuple(re.findall(r'- "([^"]+)"', match.group(1))) if match else ()


#: Screen states, from the recordings. Checked in this order: a dialog drawn
#: over the ready input is what the person must answer first.
SCREEN_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fatal", ("error: no session, task, or name matched", "github copilot cli: no platform package found",
               "error: invalid command format")),
    ("trust", ("confirm folder trust", "do you trust the files in this folder?")),
    ("account_picker", ("what account do you want to log into?",)),
    ("permission", ("do you want to run this command?", "do you want to allow", "do you want to edit")),
    ("login", ("please use /login to sign in", "you must be logged in to send messages")),
)
_WORKING = re.compile(r"^\s*[◎◉○●◌◍]\s+Working\b", re.M)
_READY_STATUS = re.compile(r"·\s*/ commands\s*·")


def classify_screen(text: str) -> str:
    """One of fatal, trust, account_picker, permission, login, working, ready, unknown."""
    lowered = text.lower()
    for state, markers in SCREEN_MARKERS:
        if any(marker in lowered for marker in markers):
            return state
    if _WORKING.search(text):
        return "working"
    if _READY_STATUS.search(text) and re.search(r"^❯\s*$", text, re.M):
        return "ready"
    return "unknown"


def native_executable(resolved: Path) -> Path | None:
    """The self-contained executable behind npm's `copilot` loader, or None when `resolved` is not that loader.

    npm installs `@github/copilot/npm-loader.js` and, beside it, the platform
    package `@github/copilot-<platform>[musl]-<arch>` holding `copilot`.
    """
    resolved = Path(resolved)
    if resolved.name != "npm-loader.js" or resolved.parent.name != "copilot" or resolved.parent.parent.name != "@github":
        return None
    import platform

    machine = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower(), "")
    system = {"linux": "linux", "darwin": "darwin"}.get(platform.system().lower(), "")
    if not machine or not system:
        return None
    scope = resolved.parent.parent
    for package in ((f"copilot-{system}musl-{machine}", f"copilot-{system}-{machine}") if system == "linux"
                    else (f"copilot-{system}-{machine}",)):
        for root in (scope, resolved.parent / "node_modules" / "@github"):
            candidate = root / package / "copilot"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


#: How long the status probe waits for Copilot to draw something it can name,
#: and how long a ready screen must stay ready: the sign-in notice is drawn
#: after the input box (tests/fixtures/copilot-1.0.101/screens/unauthenticated.txt).
STATUS_PROBE_TIMEOUT_SECONDS = 60.0
STATUS_PROBE_SETTLE_SECONDS = 4.0
STATUS_PROBE_POLL_SECONDS = 0.3


def screen_status(argv: list[str], *, cwd: str, timeout: float = STATUS_PROBE_TIMEOUT_SECONDS,
                  settle: float = STATUS_PROBE_SETTLE_SECONDS, extra_env: Mapping[str, str] | None = None
                  ) -> tuple[str, str]:
    """Start interactive Copilot in a private tmux server, name its first settled screen, and end it.

    Copilot has no non-interactive sign-in check, and asking a model would spend
    a request, so this reads what a person would see: (state, screen), where
    state is a `classify_screen` state, `exited` when the CLI ended first, or
    `timeout`. No prompt is ever typed, so no session or hook event starts.
    """
    import shlex
    import tempfile
    import time

    env = {key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}}
    env.update(extra_env or {})
    with tempfile.TemporaryDirectory(prefix="copilot-status-") as scratch:
        tmux = ["tmux", "-S", os.path.join(scratch, "tmux.sock"), "-f", "/dev/null"]
        started = subprocess.run([*tmux, "new-session", "-d", "-x", "160", "-y", "45", "-c", cwd,
                                  f"{shlex.join(argv)}; sleep 2"], env=env, capture_output=True, text=True)
        if started.returncode != 0:
            return "exited", started.stderr
        screen, ready_since = "", None
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                shot = subprocess.run([*tmux, "capture-pane", "-p", "-t", "0"], env=env, capture_output=True, text=True)
                if shot.returncode != 0:
                    return "exited", screen
                screen = shot.stdout
                state = classify_screen(screen)
                if state == "ready":
                    ready_since = ready_since or time.monotonic()
                    if time.monotonic() - ready_since >= settle:
                        return state, screen
                elif state != "unknown":
                    return state, screen
                else:
                    ready_since = None
                time.sleep(STATUS_PROBE_POLL_SECONDS)
            return "timeout", screen
        finally:
            subprocess.run([*tmux, "kill-server"], env=env, capture_output=True)


def screen_status_command(copilot_argv: list[str]) -> list[str]:
    """The owner-side command that runs `screen_status` on `copilot_argv`: this file, by its installed path."""
    return [sys.executable, "-I", str(Path(__file__).resolve()), "screen-status", "--", *copilot_argv]


def main(argv: list[str]) -> int:
    """`copilot_runtime.py screen-status -- <copilot argv...>`: one JSON line, {"state": ...}; exit 0 only when ready.

    Run as the project owner by the first-run probe, with `-I` and nothing but
    the standard library, so it needs no checkout on the owner's path.
    """
    if len(argv) < 3 or argv[0] != "screen-status" or argv[1] != "--":
        print("usage: copilot_runtime.py screen-status -- <copilot argv...>", file=sys.stderr)
        return 2
    import tempfile

    # An empty folder of its own, which COPILOT_ALLOW_ALL trusts for this run
    # only: folder trust is asked before sign-in is shown, and the owner's own
    # trust answers are neither given nor recorded (measured, 1.0.101: nothing
    # is added to trustedFolders).
    with tempfile.TemporaryDirectory(prefix="copilot-status-cwd-") as cwd:
        state, _screen = screen_status(argv[2:], cwd=cwd, extra_env={"COPILOT_ALLOW_ALL": "true"})
    # The screen is not printed: it can show the account or folder names.
    print(json.dumps({"state": state}))
    return 0 if state == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
