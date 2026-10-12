#!/usr/bin/env python3
"""SYRD-481: a one-shot presentation handoff is used once, by the launch it was made for.

The tenant control bridge hands its owner-side child a pipe on descriptor 3 and
names it in `SWITCHYARD_PRESENTATION_HANDOFF_FD`, with the caller in
`SWITCHYARD_TENANT_CONTROL_CALLER`. That launch starts the tmux server, the
server keeps its environment, and every role pane inherited both. On MEFP a
Director pane's later `switchyard present mefp bootstrap` wrote to a descriptor
3 that was long closed: "could not hand mefp's presentation window to eric:
[Errno 9] Bad file descriptor". A descriptor reused for something else would
have been written to.

Pinned here:

- **The receiving boundary,** in an isolated child process for each case, for
  both writers (`_hand_off_desktop_half` and
  `hand_presentation_back_to_the_caller`):
  - a closed descriptor, a regular file, a pipe's read end, another pipe and a
    value that is not a number are each refused with the supported
    desktop-account command, nothing is written, and it is never EBADF;
  - the bridge's own pipe, named by identity or by an older bridge without
    one, still carries the handoff;
  - no handoff variables means no handoff, as before.
- **Authority is not relaxed:** with a stale handoff, `_launch_separate` stops
  before any layout is written or window launched; without one, the ordinary
  cross-account refusal is unchanged.
- **Role panes omit the one-shot variables:** a real tmux server whose
  environment holds all three runs a role's real command prefix, and the pane
  sees none of them -- the same prefix keeps everything else.
- **The bridge names its pipe:** `switchyard-tenant-control` puts the pipe's
  `st_dev:st_ino` in the child's environment beside the descriptor.

Nothing reaches the host: no window, no Konsole, no live tmux server (a private
socket in a temporary directory, with `TMUX` stripped), no board.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import presentation_reconnect as reconnect  # noqa: E402
from scripts import presentation_window_launch as window_launch  # noqa: E402
from scripts import team_launcher as launcher  # noqa: E402

CHECKS = 0
FAILURES: list[str] = []
FD_ENV = "SWITCHYARD_PRESENTATION_HANDOFF_FD"
PIPE_ENV = "SWITCHYARD_PRESENTATION_HANDOFF_PIPE"
CALLER_ENV = "SWITCHYARD_TENANT_CONTROL_CALLER"
ROLES = ("inspector", "director", "audit", "main", "app", "ops")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(detail)
        print(f"FAIL: {detail}")


def write_config(tmp: Path, project: str = "porter"):
    leaf = lambda index: {"Command": "", "SessionRestoreId": index, "Title": "", "WorkingDirectory": ""}
    row = lambda first: {"Orientation": "Horizontal", "Widgets": [leaf(first + n) for n in range(3)]}
    layout = tmp / "layout.json"
    layout.write_text(json.dumps({"Orientation": "Vertical", "Widgets": [row(0), row(3)]}) + "\n", encoding="utf-8")
    for role in ROLES:
        (tmp / "work" / role).mkdir(parents=True, exist_ok=True)
    path = tmp / f"{project}.json"
    path.write_text(json.dumps({
        "desktop_access": {"mode": "headless"}, "project": project, "project_name": "Porter Team", "layout": str(layout),
        "roles": [{"role": role, "slot": slot, "cli": ["claude"], "target": f"{project}-{role}:0.0",
                   "workdir": str(tmp / "work" / role)} for slot, role in enumerate(ROLES)],
    }) + "\n", encoding="utf-8")
    return launcher.load_project_config(project, path), path


# --- the receiving boundary, one isolated child per case ----------------------------------------------------------------

#: What descriptor 3 is in the child, and what the environment says about it.
CASES = {
    "closed": {"fd3": "closed", "pipe_env": "none", "caller": "eric"},
    "a regular file": {"fd3": "file", "pipe_env": "none", "caller": "eric"},
    "a pipe's read end": {"fd3": "read end", "pipe_env": "none", "caller": "eric"},
    "another pipe": {"fd3": "pipe", "pipe_env": "other", "caller": "eric"},
    "not a number": {"fd3": "pipe", "pipe_env": "match", "caller": "eric", "fd_env": "three"},
    "the bridge's pipe": {"fd3": "pipe", "pipe_env": "match", "caller": "eric"},
    "an older bridge's pipe": {"fd3": "pipe", "pipe_env": "none", "caller": "eric"},
    "a handoff fixture with no caller": {"fd3": "pipe", "pipe_env": "match", "caller": ""},
    "no handoff": {"fd3": "pipe", "pipe_env": "none", "caller": "", "fd_env": ""},
}
REFUSED = {"closed": "its handoff descriptor 3 is not open",
           "a regular file": "its handoff descriptor 3 is not the write end of a pipe",
           "a pipe's read end": "its handoff descriptor 3 is not the write end of a pipe",
           "another pipe": "its handoff descriptor 3 is not the pipe the bridge opened",
           "not a number": "SWITCHYARD_PRESENTATION_HANDOFF_FD='three' names no descriptor"}


def child(writer: str, case: str, workdir: str) -> None:
    """Runs in its own process: arrange descriptor 3, call the writer, report what happened."""
    spec = CASES[case]
    tmp = Path(workdir)
    config, _path = write_config(tmp)
    for name in (FD_ENV, PIPE_ENV, CALLER_ENV):
        os.environ.pop(name, None)
    try:
        os.close(3)
    except OSError:
        pass
    read_end = None
    sink = tmp / "fd3-file"
    if spec["fd3"] == "file":
        opened = fcntl.fcntl(os.open(sink, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), fcntl.F_DUPFD, 10)
        os.dup2(opened, 3)
    elif spec["fd3"] in ("pipe", "read end"):
        # Both ends well clear of 3: with 3 closed, os.pipe() hands it out.
        r0, w0 = os.pipe()
        r, w = fcntl.fcntl(r0, fcntl.F_DUPFD, 10), fcntl.fcntl(w0, fcntl.F_DUPFD, 10)
        os.close(r0)
        os.close(w0)
        os.dup2(w if spec["fd3"] == "pipe" else r, 3)
        os.close(w if spec["fd3"] == "pipe" else r)
        read_end = r if spec["fd3"] == "pipe" else None
        os.set_blocking(read_end, False) if read_end is not None else None
    if spec.get("fd_env", "3"):
        os.environ[FD_ENV] = spec.get("fd_env", "3")
    if spec["caller"]:
        os.environ[CALLER_ENV] = spec["caller"]
    if spec["pipe_env"] == "match":
        info = os.fstat(3)
        os.environ[PIPE_ENV] = f"{info.st_dev}:{info.st_ino}"
    elif spec["pipe_env"] == "other":
        os.environ[PIPE_ENV] = "1:2"
    outcome: dict[str, object] = {}
    try:
        if writer == "launch":
            outcome["returned"] = window_launch._hand_off_desktop_half(config, {"slot_count": 6}, gui_user="eric")
        else:
            outcome["returned"] = reconnect.hand_presentation_back_to_the_caller(
                config, slot_count=6, pane_program=Path("/opt/switchyard/current/switchyard-pane-window"),
                slot_titles=[f"Porter -- {r}" for r in ROLES], print_func=lambda _t: None)
    except SystemExit as exc:
        outcome["refused"] = str(exc.code)
    except BaseException as exc:  # noqa: BLE001 -- an answer to report
        outcome["raised"] = f"{type(exc).__name__}: {exc}"
    written = b""
    if read_end is not None:
        try:
            written = os.read(read_end, 65536)
        except BlockingIOError:
            written = b""
    outcome["pipe_got"] = written.decode("utf-8", "replace")
    outcome["file_got"] = sink.read_text() if sink.exists() else None
    try:
        os.fstat(3)
        outcome["fd3_open_after"] = True
    except OSError:
        outcome["fd3_open_after"] = False
    print("RESULT " + json.dumps(outcome))


def run_child(writer: str, case: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd481-") as tmp:
        env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "LANG": "C.UTF-8"}
        done = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--child", writer, case, tmp],
                              env=env, capture_output=True, text=True, timeout=120, close_fds=True)
    line = next((l for l in done.stdout.splitlines() if l.startswith("RESULT ")), None)
    if line is None:
        return {"child_failed": f"rc={done.returncode} {done.stdout[-400:]} {done.stderr[-800:]}"}
    return json.loads(line[len("RESULT "):])


def test_the_receiving_boundary() -> None:
    for writer, who in (("launch", "eric"), ("reconnect", "the desktop account")):
        for case in CASES:
            got = run_child(writer, case)
            label = f"{writer}: {case}"
            check("child_failed" not in got, f"{label}: the isolated child ran: {got}")
            if case in REFUSED:
                refusal = got.get("refused", "")
                check(refusal == (f"switchyard: not handing porter's presentation window to {who}: {REFUSED[case]}. "
                                  "A presentation handoff belongs to the one launch the tenant control bridge started, "
                                  f"and this is not it. Run `sudo switchyard porter` from {who}'s own session to open the window."),
                      f"{label}: refused with the supported desktop-account command: {got}")
                check("Bad file descriptor" not in json.dumps(got) and "Errno" not in json.dumps(got), f"{label}: never EBADF: {got}")
                check(got.get("pipe_got") == "" and got.get("file_got") in (None, ""),
                      f"{label}: nothing is written to whatever descriptor 3 is: {got}")
            elif case == "no handoff":
                check(got.get("returned") is False and got.get("pipe_got") == "", f"{label}: no handoff variables, no handoff: {got}")
            else:
                payload = json.loads(got.get("pipe_got") or "null")
                check(got.get("returned") is True and isinstance(payload, dict)
                      and payload.get("schema") == "switchyard.presentation-handoff.v2" and payload.get("project") == "porter"
                      and payload.get("slot_count") == 6 and got.get("fd3_open_after") is False,
                      f"{label}: the handoff is written to the bridge's pipe, once, and the descriptor closed: {got}")


# --- authority is not relaxed ---------------------------------------------------------------------------------------------


class Stood:
    def __init__(self, **values):
        self.values, self.saved = values, {}

    def __enter__(self):
        for name, value in self.values.items():
            self.saved[name] = getattr(launcher, name)
            setattr(launcher, name, value)

    def __exit__(self, *exc):
        for name, value in self.saved.items():
            setattr(launcher, name, value)


def launch_separate(env: dict[str, str]) -> tuple[str, list[str]]:
    """`_launch_separate` for a project whose window belongs to eric, every host step stood in."""
    steps: list[str] = []
    saved = {k: os.environ.get(k) for k in (FD_ENV, PIPE_ENV, CALLER_ENV)}
    with tempfile.TemporaryDirectory(prefix="syrd481-launch-") as tmp:
        config, path = write_config(Path(tmp))
        try:
            for k in saved:
                os.environ.pop(k, None)
            os.environ.update(env)

            def step(name, answer=None):
                def f(*args, **kwargs):
                    steps.append(name)
                    return answer
                return f
            with Stood(presentation_gui_user=lambda _config: "eric",
                       presentation_pane_program_problem=step("pane program check", ""),
                       display_bridge_launch_problem=step("display bridge check", ""),
                       desktop_presentation_layout_path=step("layout path", Path(tmp) / "layout-out.json"),
                       write_desktop_layout=step("write layout", "eric's state directory is not writable by this account"),
                       launch_konsole_window=step("launch konsole", 0)):
                try:
                    window_launch._launch_separate(config, {"slot_count": 6}, config_path=path, runner=step("runner"), process_launcher=None)
                    message = "returned"
                except SystemExit as exc:
                    message = str(exc.code)
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
    return message, steps


def test_authority_is_not_relaxed() -> None:
    message, steps = launch_separate({FD_ENV: "3", CALLER_ENV: "eric"} if not _fd3_open() else {FD_ENV: "987", CALLER_ENV: "eric"})
    check(message.startswith("switchyard: not handing porter's presentation window to eric: its handoff descriptor")
          and "Run `sudo switchyard porter` from eric's own session" in message and steps == [],
          f"a stale handoff stops before any check, layout or window: {message} {steps}")
    message, steps = launch_separate({})
    check(message == ("switchyard: refusing to open porter's presentation window: eric's state directory is not writable by this account. "
                      "A terminal handed a layout it cannot read aborts before anything appears; run `sudo switchyard porter` "
                      "from eric's own session, which can.")
          and steps == ["pane program check", "display bridge check", "layout path", "write layout"],
          f"with no handoff, the ordinary cross-account refusal is unchanged, and no window is launched: {message} {steps}")


def _fd3_open() -> bool:
    try:
        os.fstat(3)
        return True
    except OSError:
        return False


# --- role panes -----------------------------------------------------------------------------------------------------------


ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def role_prefix(argv: list[str]) -> list[str]:
    """The `env -u ... KEY=VALUE` prefix of a role command, without the program it runs."""
    assert argv[0] == "env", argv[:3]
    i = 1
    while argv[i] == "-u":
        i += 2
    while ASSIGNMENT.match(argv[i]):
        i += 1
    return argv[:i]


def test_role_panes_omit_the_one_shot_variables() -> None:
    tmux = shutil.which("tmux")
    with tempfile.TemporaryDirectory(prefix="syrd481-tmux-") as tmp:
        root = Path(tmp)
        config, _path = write_config(root)
        role = next(r for r in config.roles if r.role == "director")
        argv = launcher.cli_command_for_role(role, session_dir=root / "sessions", pane_state_dir=root / "pane-state")
        prefix = role_prefix([str(a) for a in argv])
        unset = {prefix[i + 1] for i in range(1, len(prefix)) if prefix[i] == "-u"}
        check({FD_ENV, PIPE_ENV, CALLER_ENV} <= unset, f"a role's command unsets all three one-shot variables: {sorted(unset)}")
        check(set(launcher.PANE_TARGET_ENV_KEYS) <= unset, "and still every pane identity variable")
        if not tmux:
            check(False, "tmux is installed, so the pane case can run")
            return
        record = root / "pane-env.json"
        probe = [sys.executable, "-c", f"import json, os; json.dump(dict(os.environ), open({str(record)!r}, 'w'))"]
        socket_path = root / "tmux.sock"
        server_env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C.UTF-8", "TMUX_TMPDIR": str(root),
                      FD_ENV: "3", PIPE_ENV: "8:12345", CALLER_ENV: "eric", "SYRD481_ORDINARY": "kept"}
        assert "TMUX" not in server_env
        try:
            started = subprocess.run([tmux, "-S", str(socket_path), "-f", "/dev/null", "new-session", "-d", "-s", "syrd481",
                                      launcher._quote_command([*prefix, *probe]) + "; sleep 30"],
                                     env=server_env, capture_output=True, text=True, timeout=30)
            check(started.returncode == 0, f"precondition: a private tmux server started: {started.stderr}")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not record.exists():
                time.sleep(0.1)
            shown = subprocess.run([tmux, "-S", str(socket_path), "show-environment", "-g"], env=server_env,
                                   capture_output=True, text=True, timeout=30).stdout
            check(f"{FD_ENV}=3" in shown and f"{CALLER_ENV}=eric" in shown,
                  "precondition: the server's own environment holds the handoff, as the bridged launch's did")
            pane = json.loads(record.read_text()) if record.exists() else {}
            check(pane != {} and not {FD_ENV, PIPE_ENV, CALLER_ENV} & set(pane),
                  f"the role's pane sees none of the one-shot variables: {sorted(k for k in pane if 'SWITCHYARD' in k)}")
            check(pane.get("SYRD481_ORDINARY") == "kept", "and keeps the rest of the server's environment")
        finally:
            subprocess.run([tmux, "-S", str(socket_path), "kill-server"], env=server_env, capture_output=True, timeout=30)


# --- the bridge -----------------------------------------------------------------------------------------------------------


def test_the_bridge_names_its_pipe() -> None:
    text = (ROOT / "scripts" / "switchyard-tenant-control").read_text(encoding="utf-8")
    check('HANDOFF_PIPE_ENV = "SWITCHYARD_PRESENTATION_HANDOFF_PIPE"' in text
          and re.search(r"read_fd, write_fd = os\.pipe\(\)\n(?:.*\n){0,6}?\s+piped = os\.fstat\(write_fd\)\n"
                        r"\s+environ\[HANDOFF_PIPE_ENV\] = f\"\{piped\.st_dev\}:\{piped\.st_ino\}\"", text) is not None,
          "the bridge names the pipe it hands over, by the write end's own identity, before it forks")
    check(getattr(launcher, "PRESENTATION_HANDOFF_PIPE_ENV", None) == PIPE_ENV == getattr(reconnect, "PRESENTATION_HANDOFF_PIPE_ENV", None),
          "and the owner half reads the same variable")


def main() -> int:
    for name in sorted(n for n, v in globals().items() if n.startswith("test_") and callable(v)):
        globals()[name]()
    if FAILURES:
        print(f"presentation_handoff_stale_fd_test: {len(FAILURES)} of {CHECKS} checks FAILED")
        return 1
    print(f"presentation_handoff_stale_fd_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--child":
        child(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        raise SystemExit(main())
