#!/usr/bin/env python3
"""SYRD-91: a Codex pane keeps Codex's own rendering, and the wheel still scrolls.

An earlier repair on this ticket sent `-c tui.raw_output_mode=true` to every
Switchyard-managed Codex role. Live acceptance rejected it: raw output prints
Markdown fences and emphasis literally, drops the colour and the `>`/`*` glyphs
that separate a prompt from a reply, and repaints often enough that a
terminal-side selection does not survive the mouse button coming up. Measured
side by side on codex-cli 0.153.4 with one prompt asking for a heading, a
bullet list and a fenced `bash` block:

    raw:    `## Demo`, `- One`, and a literal ```bash fence, no SGR attributes
            anywhere in the body of the reply
    native: the heading carried `ESC[0;1m`, the code block arrived indented
            with no fence, the prompt was marked `ESC[1;2m>` and the reply
            `ESC[2m*`

So the override is gone and these cases hold the pane to the launcher adding
nothing of its own. The property that makes the wheel reach earlier responses
is not an override at all: codex runs inline and does not capture the mouse, so
the `WheelUpPane` binding tmux ships takes its `copy-mode -e` branch rather than
forwarding the event, and the `mouse on` the launcher already sets on every role
session is what carries it. `test_a_real_codex_pane_scrolls_rather_than_typing`
puts that to a real codex under a real tmux and is the case that fails if a
future Codex takes the alternate screen or grabs the mouse.
"""

from __future__ import annotations

import contextlib
import fcntl
import pty
import re
import select
import signal
import struct
import termios

from team_launcher_test_helpers import *

from tmux_socket_cleanup import cleanup_dead_isolated_tmux_socket, run_isolated_tmux

RAW_OUTPUT_KEY = "tui.raw_output_mode"
#: SGR wheel-up at column 20, row 10, the encoding tmux asks its terminal for.
SGR_WHEEL_UP = b"\x1b[<64;20;10M"


@contextlib.contextmanager
def _environment(**values: str):
    """Run a block with these variables set, and put the old ones back."""
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, was in previous.items():
            if was is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = was


def _role_payload(role: str, cli: list[str], *, slot: int = 0, workdir: str = "", **extra) -> dict:
    payload = {"role": role, "slot": slot, "cli": cli, "target": f"porter-{role}:0.0", **extra}
    if workdir:
        payload["workdir"] = workdir
    return payload


def _project(tmp: Path, roles: list[dict]) -> "team_launcher.ProjectConfig":
    layout_path = tmp / "layout.json"
    layout_path.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
    config_path = tmp / "porter.json"
    config_path.write_text(
        json.dumps(
            {
                "desktop_access": {"mode": "headless"},
                "project": "porter",
                "layout": str(layout_path),
                "roles": roles,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return load_project_config("porter", config_path)


def _command_for(config, role_name: str, *, session_dir: Path, resume: bool = False) -> list[str]:
    role = next(role for role in config.roles if role.role == role_name)
    return cli_command_for_role(role, session_dir=session_dir, resume=resume)


def test_a_codex_pane_is_launched_with_no_output_mode_override() -> None:
    """The rejected repair, stated as the thing that must not come back.

    Nothing in the stored project asks for it either, so this is also what says
    a tenant provisioned while the override was live loses it on its next start
    with nothing regenerated.
    """
    with tempfile.TemporaryDirectory(prefix="codex-render.") as tmp:
        tmp_path = Path(tmp)
        config = _project(tmp_path, [_role_payload("ops", ["codex"])])

        command = _command_for(config, "ops", session_dir=tmp_path / "sessions")

        assert RAW_OUTPUT_KEY not in " ".join(command), command
        assert _command_tail(command) == ["codex"], command
        assert RAW_OUTPUT_KEY not in (tmp_path / "porter.json").read_text(encoding="utf-8")


def test_no_runtime_carries_a_presentation_override() -> None:
    """Scope, read the other way round: the repair removed something from Codex
    and must not have quietly moved it onto a neighbour."""
    with tempfile.TemporaryDirectory(prefix="codex-render-scope.") as tmp:
        tmp_path = Path(tmp)
        config = _project(
            tmp_path,
            [
                _role_payload("director", ["claude"], slot=0, workdir=str(tmp_path / "director")),
                _role_payload("inspector", ["agy"], slot=1, workdir=str(tmp_path / "inspector")),
                _role_payload("main", ["hermes"], slot=2, workdir=str(tmp_path / "main")),
                _role_payload("ops", ["codex"], slot=3, workdir=str(tmp_path / "ops")),
            ],
        )
        session_dir = tmp_path / "sessions"

        for role_name, expected in (
            ("director", ["claude"]),
            ("inspector", ["agy"]),
            ("main", ["hermes", "--accept-hooks", "--pass-session-id"]),
            ("ops", ["codex"]),
        ):
            tail = _command_tail(_command_for(config, role_name, session_dir=session_dir))
            assert tail == expected, (role_name, tail)


def test_the_rest_of_the_codex_command_is_untouched() -> None:
    """Model, effort, yolo, startup flags and the role's own arguments survive.

    The effort override is the one `-c` a Codex role is still meant to get, and
    removing a neighbouring `-c` is an easy way to take it with you.
    """
    with tempfile.TemporaryDirectory(prefix="codex-render-intact.") as tmp:
        tmp_path = Path(tmp)
        config = _project(
            tmp_path,
            [
                _role_payload(
                    "ops",
                    ["codex"],
                    model="gpt-5.5",
                    effort="high",
                    yolo=True,
                    extra_args=["--search"],
                )
            ],
        )

        tail = _command_tail(_command_for(config, "ops", session_dir=tmp_path / "sessions"))

        assert tail == [
            "codex",
            "--model",
            "gpt-5.5",
            "-c",
            "reasoning_effort=high",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "--search",
        ], tail


def test_an_operator_can_still_ask_for_raw_output_themselves() -> None:
    """Removing the forced setting is not removing the choice.

    A role that wants it says so in `extra_args`, and those are the last thing
    on the command line.
    """
    with tempfile.TemporaryDirectory(prefix="codex-render-optin.") as tmp:
        tmp_path = Path(tmp)
        config = _project(
            tmp_path,
            [_role_payload("ops", ["codex"], extra_args=["-c", f"{RAW_OUTPUT_KEY}=true"])],
        )

        tail = _command_tail(_command_for(config, "ops", session_dir=tmp_path / "sessions"))

        assert tail == ["codex", "-c", f"{RAW_OUTPUT_KEY}=true"], tail


def test_a_resumed_codex_session_is_not_given_one_either() -> None:
    """A restart resumes through a subcommand, which is a second assembly path."""
    with tempfile.TemporaryDirectory(prefix="codex-render-resume.") as tmp:
        tmp_path = Path(tmp)
        config = _project(tmp_path, [_role_payload("ops", ["codex"])])
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        role = next(role for role in config.roles if role.role == "ops")
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": "ops-session"}) + "\n", encoding="utf-8"
        )
        rollout = session_dir.parent / ".codex" / "sessions"
        rollout.mkdir(parents=True)
        (rollout / "rollout-2026-09-09T00-00-00-ops-session.jsonl").write_text("{}\n", encoding="utf-8")

        tail = _command_tail(_command_for(config, "ops", session_dir=session_dir, resume=True))

        assert tail == ["codex", "resume", "ops-session"], tail


def test_starting_a_codex_role_writes_nothing_into_a_codex_home() -> None:
    """The other half of the scope line: a human's own `codex` is left alone.

    The rejected repair was careful about this and the replacement must not be
    less careful -- the easy way to "restore rendering" is to write the owner's
    `~/.codex/config.toml`, which every codex that person starts for themselves
    then reads. Nothing here may touch it, so the whole tree is fingerprinted
    around the launch the launcher would perform.
    """
    with tempfile.TemporaryDirectory(prefix="codex-render-home.") as tmp:
        tmp_path = Path(tmp)
        codex_home = tmp_path / "home" / ".codex"
        codex_home.mkdir(parents=True)
        config_toml = codex_home / "config.toml"
        config_toml.write_text('model = "gpt-5.5"\n[tui]\ntheme = "dark"\n', encoding="utf-8")

        def fingerprint() -> dict[str, str]:
            return {
                str(path.relative_to(codex_home)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(codex_home.rglob("*"))
                if path.is_file()
            }

        before = fingerprint()
        config = _project(tmp_path, [_role_payload("ops", ["codex"], workdir=str(tmp_path))])
        role = next(item for item in config.roles if item.role == "ops")
        # The tree under test has to be the one the launcher would find, or a
        # write into the real `~/.codex` passes this by writing somewhere else.
        with _environment(HOME=str(codex_home.parent), CODEX_HOME=str(codex_home)):
            command = cli_command_for_role(role, session_dir=tmp_path / "sessions")
            team_launcher.tmux_new_session_args(role, session_dir=tmp_path / "sessions")

        assert fingerprint() == before
        assert str(codex_home) not in " ".join(command), command
        assert config_toml.read_text(encoding="utf-8") == 'model = "gpt-5.5"\n[tui]\ntheme = "dark"\n'


def test_the_launcher_source_sends_no_tui_override() -> None:
    """The table and its builder are gone, not merely unwired.

    A table left behind with no caller is a repair that reappears the next time
    somebody wires it back up by accident.
    """
    text = (ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")
    assert "RAW_OUTPUT_ARGS_BY_CLI" not in text
    assert "raw_output_args_for_role" not in text
    assert RAW_OUTPUT_KEY not in text


def _pane_format(server: str, target: str, spec: str) -> str:
    proc = run_isolated_tmux(
        server,
        ["display-message", "-p", "-t", target, spec],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=20,
    )
    return str(proc.stdout or "").strip()


def _pane_process_tree(server: str, target: str) -> set[str]:
    """Every program running under the pane, read from the process table.

    `#{pane_current_command}` names the pane's foreground process group
    leader, which is the shell whenever the pane's shell does not replace
    itself with its argument -- and this account's login shell, fish, does
    not. tmux takes `default-shell` from the invoker's `$SHELL`, so that
    format would make this case pass or fail on who ran it. What the case
    needs to know is whether codex is running under the pane, so the tree
    below `#{pane_pid}` is walked instead (SYRD-91 audit kick-back).
    """
    pane_pid = _pane_format(server, target, "#{pane_pid}")
    if not pane_pid.isdigit():
        return set()
    names: dict[int, str] = {}
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            names[int(entry.name)] = (entry / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        # The second field is the executable name in parentheses and may itself
        # contain spaces, so the fields after it are counted from the last `)`.
        after = stat[stat.rfind(")") + 1 :].split()
        if len(after) < 2:
            continue
        children.setdefault(int(after[1]), []).append(int(entry.name))
    found: set[str] = set()
    seen: set[int] = set()
    stack = [int(pane_pid)]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if pid in names:
            found.add(names[pid])
        stack.extend(children.get(pid, []))
    return found


def _wheel_up_through_an_attached_client(server: str, session: str, notches: int = 3) -> None:
    """Deliver a real wheel event the way a terminal attached to the pane does.

    tmux reads mouse events from the terminal its client is attached to, so
    nothing short of a client on a pty can produce one: `send-keys` sends keys,
    which is the outcome under test, not the input to it.
    """
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child execs
        os.environ["TERM"] = "xterm-256color"
        os.execvp("tmux", ["tmux", "-L", server, "attach", "-t", session])
        os._exit(1)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 43, 106, 0, 0))
        deadline = time.time() + 5
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    os.read(fd, 65536)
                except OSError:
                    break
        for _ in range(notches):
            os.write(fd, SGR_WHEEL_UP)
            time.sleep(0.3)
        time.sleep(1.0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)


def test_a_real_codex_pane_scrolls_rather_than_typing() -> None:
    """The reported symptom, put to the installed codex under a real tmux.

    `WheelUpPane` forwards the event to the program when the pane holds the
    alternate screen or has asked for the mouse, and tmux turns a forwarded
    wheel into arrow keys -- which in a Codex composer walks input history,
    which is what the ticket reported. It enters copy mode otherwise. So the
    two pane flags decide between scrolling and typing, and they are read here
    off a real codex started from the launcher's own argv, with the session
    options the launcher gives a role session.
    """
    if not shutil.which("codex") or not shutil.which("tmux"):
        return
    server = f"syrd91-{os.getpid()}"
    cleanup_dead_isolated_tmux_socket(server)
    with tempfile.TemporaryDirectory(prefix="codex-render-live.") as tmp:
        tmp_path = Path(tmp)
        config = _project(tmp_path, [_role_payload("ops", ["codex"], workdir=str(tmp_path))])
        role = next(item for item in config.roles if item.role == "ops")
        session = role.tmux_session
        # A codex of its own, so this never reads or writes the owner's.
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        argv = cli_command_for_role(role, session_dir=tmp_path / "sessions")
        started = run_isolated_tmux(
            server,
            [
                "new-session", "-d", "-x", "106", "-y", "43", "-s", session, "-c", str(tmp_path),
                team_launcher._quote_command(["env", f"CODEX_HOME={codex_home}", *argv]),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        if started.returncode != 0:
            return
        try:
            for option in (
                team_launcher.tmux_set_mouse_args(session),
                team_launcher.tmux_set_history_limit_args(session),
            ):
                run_isolated_tmux(
                    server, option[1:], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20
                )
            target = f"{session}:0.0"
            deadline = time.time() + 60
            running: set[str] = set()
            while time.time() < deadline:
                running = _pane_process_tree(server, target)
                if "codex" in running:
                    break
                time.sleep(0.5)
            assert "codex" in running, (
                sorted(running),
                _pane_format(server, target, "#{pane_current_command}"),
                run_isolated_tmux(
                    server, ["capture-pane", "-p", "-t", target],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=20,
                ).stdout,
            )

            flags = _pane_format(server, target, "#{alternate_on},#{mouse_any_flag}")
            assert flags == "0,0", flags

            _wheel_up_through_an_attached_client(server, session)

            assert _pane_format(server, target, "#{pane_mode}") == "copy-mode", _pane_format(
                server, target, "#{pane_mode}"
            )
            assert int(_pane_format(server, target, "#{scroll_position}") or 0) > 0
        finally:
            run_isolated_tmux(
                server,
                ["kill-session", "-t", session],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_codex_rendered_scrollback_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
