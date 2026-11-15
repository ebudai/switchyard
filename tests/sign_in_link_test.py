#!/usr/bin/env python3
"""SYRD-782: the provider's sign-in link is printed by Switchyard as one clickable line, and the agent opens no browser.

On test11 the agent's Firefox opened for the sign-in, behind the User's terminal (KWin refuses to raise a window the
User has not asked for), and closed with the step: never seen. The User chose this instead: the agent opens nothing,
and Switchyard prints the URL the CLI shows as one unbroken OSC 8 line the User clicks in their own terminal.

The CLI outputs below are the shapes measured on this host: `claude auth login` (2.1.318), `codex login`, `copilot
login`, and agy 1.5.1's full-screen sign-in on the alternate screen, with made-up URLs of the same form.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scripts import sign_in_link as sil  # noqa: E402
from scripts.provider_session import run_provider_first_run_session  # noqa: E402

CHECKS = 0
CLAUDE_URL = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a&response_type=code&redirect_uri="
              "https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=user%3Ainference&state=AbC-123_x")
CODEX_URL = ("https://auth.openai.com/oauth/authorize?response_type=code&client_id=app_X&redirect_uri="
             "http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback&state=Zz9")
COPILOT_URL = "https://github.com/login/oauth/authorize?client_id=Ov23&redirect_uri=http%3A%2F%2F127.0.0.1%3A36935"
AGY_URL = ("https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=1071.apps.googleusercontent.com"
           "&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&response_type=code&state=pNq")

CLAUDE_OUT = ("Opening browser to sign in…\nIf the browser didn't open, visit: "
              f"\x1b]8;;{CLAUDE_URL}\x07\x1b[94m{CLAUDE_URL}\x1b[39m\x1b]8;;\x07\nPaste code here if prompted > ")
CODEX_OUT = ("Starting local login server on http://localhost:1455.\nIf your browser did not open, navigate to this URL "
             f"to authenticate:\n\n{CODEX_URL}\n\nOn a remote or headless machine? Use `codex login --device-auth`.\n")
COPILOT_OUT = (f"Opening your browser to authenticate...\nIf it doesn't open automatically, visit:\n{COPILOT_URL}\n"
               "Waiting for authorization...\n")
# Full screen: the URL hard-broken over rows (its first row looks like a whole URL), then an OSC 8 "Click here".
AGY_OUT = ("\x1b[?1049h\x1b[H\x1b[2JCopy and paste the URL or click on the link below:\r\n"
           + "\r\n".join(AGY_URL[i:i + 60] for i in range(0, len(AGY_URL), 60))
           + f"\r\n\x1b]8;;{AGY_URL}\x1b\\→ Click here to authenticate\x1b]8;;\x1b\\\r\n"
           "If you aren't automatically redirected, paste the authorization code below:\r\n")


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def fed(output: str, step: int, **kwargs):
    watcher = sil.SignInLink(cli="claude", owner_user="test11-agent")
    pieces = []
    for at in range(0, len(output), step):
        pieces += watcher.feed(output[at:at + step], **kwargs)
    return watcher, pieces


def test_each_line_mode_sign_in_gets_one_link_after_its_url_line() -> None:
    for name, output, url, before in (("claude auth login", CLAUDE_OUT, CLAUDE_URL, "Paste code here"),
                                      ("codex login", CODEX_OUT, CODEX_URL, "On a remote"),
                                      ("copilot login", COPILOT_OUT, COPILOT_URL, "Waiting for authorization")):
        for step in (1, 7, 64, len(output)):  # however the reads cut it
            watcher, pieces = fed(output, step)
            ours = [text for text, mine in pieces if mine]
            check(watcher.url == url, f"{name}/{step}: the whole URL is found: {watcher.url!r}")
            check(len(ours) == 1 and sil.link(url) in ours[0], f"{name}/{step}: printed once, as the link: {ours}")
            check("".join(text for text, mine in pieces if not mine) == output,
                  f"{name}/{step}: the CLI's own output is passed on unchanged")
            shown = "".join(text for text, _ in pieces)
            check(shown.index(sil.link(url)) < shown.index(before),
                  f"{name}/{step}: after the line with the URL, before what follows it")
            check(shown[:shown.index(ours[0])].endswith("\n"),
                  f"{name}/{step}: never inside that line, which the CLI ends first: {shown[:shown.index(ours[0])][-40:]!r}")
        link_line = [line for line in ours[0].split("\r\n") if url in line]
        check(len(link_line) == 1 and link_line[0] == sil.link(url) and "\n" not in sil.link(url),
              f"{name}: the URL is one unbroken line, its own text as the hyperlink's: {link_line}")
        check(sil.link(url) == f"\x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\", f"{name}: an OSC 8 link around the URL itself")
        check("Browser didn't open" not in ours[0] and "paste back here any code the page shows" in ours[0]
              and "test11-agent" in ours[0] and "your own browser" in ours[0],
              f"{name}: it says to click it and paste back any code, never 'Browser didn't open': {ours[0]!r}")
    # codex prints http://localhost first: only an https sign-in URL is a sign-in link.
    watcher, _ = fed(CODEX_OUT, 5)
    check("localhost" not in watcher.url.split("?")[0], f"the local login server is not the link: {watcher.url}")


def test_a_full_screen_sign_in_gets_its_link_in_rows_the_cli_cannot_draw_on() -> None:
    reserved: list[int] = []
    watcher, pieces = fed(AGY_OUT, 9, size=(40, 120), reserve=lambda n: reserved.append(n) or True)
    ours = [text for text, mine in pieces if mine]
    check(watcher.url == AGY_URL, f"the OSC 8 target is the URL, not the first broken row: {watcher.url!r}")
    check(reserved == [watcher.rows_needed(120)] and watcher.reserved == reserved[0],
          f"the CLI's terminal is made shorter by the rows the link needs: {reserved}")
    top = 40 - reserved[0] + 1
    check(len(ours) == 1 and ours[0].startswith("\x1b7") and ours[0].endswith("\x1b8")
          and f"\x1b[{top};1H" in ours[0] and sil.link(AGY_URL) in ours[0]
          and all(f"\x1b[{row};1H" not in ours[0] for row in range(1, top)),
          f"drawn in the bottom rows only, the CLI's cursor kept: {ours!r}")
    check("".join(text for text, mine in pieces if not mine) == AGY_OUT, "the CLI's own output passes unchanged")
    check(f"\x1b[1;{top - 1}r" in ours[0] and watcher.release() == "\x1b7\x1b[r\x1b8",
          "the rows above are the scroll region, so the CLI's newlines never reach the link; given back after")
    redraw = watcher.feed("\x1b[H\x1b[2Jnew frame", size=(40, 120))
    check([mine for _, mine in redraw] == [False, True] and sil.link(AGY_URL) in redraw[1][0],
          f"a screen clear by the CLI is followed by the link again: {redraw}")
    quiet = watcher.feed("more of the same frame", size=(40, 120))
    check(quiet == [("more of the same frame", False)], f"and nothing is redrawn otherwise: {quiet}")
    resized = watcher.feed("frame", size=(30, 100))
    check(len(resized) == 2 and "\x1b[" + str(30 - watcher.reserved + 1) + ";1H" in resized[1][0],
          f"a resized window gets the link at its new bottom: {resized}")
    asked: list[int] = []
    watcher.feed("frame", size=(40, 60), reserve=lambda n: asked.append(n) or True)
    check(asked == [watcher.rows_needed(60)] and watcher.reserved == asked[0] > reserved[0],
          f"a narrower window keeps more rows for the same URL: {asked}")
    split = watcher.feed("\x1b[2J\x1b[", size=(40, 120))
    check(split == [("\x1b[2J\x1b[", False)], f"never written into the middle of the CLI's escape sequence: {split}")
    later = watcher.feed("1;1Hframe", size=(40, 120))
    check(len(later) == 2 and sil.link(AGY_URL) in later[1][0],
          f"and that clear is still answered at the next clean boundary: {later}")
    osc = watcher.feed("\x1b[2Jtext\x1b]", size=(40, 120))
    check(osc == [("\x1b[2Jtext\x1b]", False)], f"nor after a hyperlink's opening `ESC ]` (a real-terminal replay): {osc}")
    check(sil.SignInLink(cli="x", owner_user="y").release() == "", "a step that reserved nothing gives nothing back")
    # Nowhere to put it: no terminal size, or no way to shorten the CLI's terminal.
    for kwargs in ({}, {"size": (40, 120)}, {"size": (40, 120), "reserve": lambda n: False}):
        watcher, pieces = fed(AGY_OUT, 50, **kwargs)
        check(not [t for t, mine in pieces if mine] and sil.link(AGY_URL) in watcher.leftover(),
              f"{kwargs}: nothing is drawn over the CLI; the link is said once its screen is gone")


def test_nothing_the_agent_prints_reaches_the_terminal_as_a_control_sequence() -> None:
    for label, output in (
            ("a plain URL with a title sequence glued on", "visit:\nhttps://x.example/a\x1b]0;pwned\x07b\nnext\n"),
            ("an OSC 8 target that is not https", "visit: \x1b]8;;javascript:alert(1)\x07go\x1b]8;;\x07\nnext\n"),
            ("an http URL", "visit:\nhttp://x.example/login\nnext\n"),
            ("a URL with a space where a host should be", "visit:\nhttps:// x.example\nnext\n"),
            ("a URL too long to be one", "visit:\nhttps://x.example/" + "a" * 5000 + "\nnext\n")):
        watcher, pieces = fed(output, 3)
        ours = "".join(t for t, mine in pieces if mine)
        check(all(c not in watcher.url for c in "\x1b\x07\r\n \"<>"), f"{label}: {watcher.url!r}")
        check(not watcher.url or watcher.url == "https://x.example/a", f"{label}: only the URL itself: {watcher.url!r}")
        check("pwned" not in ours and "javascript" not in ours, f"{label}: nothing else is printed: {ours!r}")
    check(sil.printable_url(CLAUDE_URL) == CLAUDE_URL and not sil.printable_url("https://a.example/\x1b")
          and not sil.printable_url("ftp://a.example/") and not sil.printable_url(""), "the printable-URL rule")


def test_the_agents_own_browser_launch_is_turned_off() -> None:
    owner = ["sudo", "-u", "test11-agent", "env", "HOME=/home/test11-agent", "PATH=/home/test11-agent/bin:/usr/bin",
             "TERM=xterm-256color", "claude", "auth", "login"]
    args, kwargs = sil.no_browser_transform(owner, {"cwd": "/x"})
    directory = sil.no_browser_dir()
    check(kwargs == {"cwd": "/x"} and args[:4] == owner[:4] and args[-3:] == ["claude", "auth", "login"],
          f"the command is unchanged around it: {args}")
    check("BROWSER=true" in args and f"PATH={directory}:/home/test11-agent/bin:/usr/bin" in args,
          f"BROWSER=true, and the no-op opener first on the command's own PATH: {args}")
    info = os.stat(directory)
    opener = Path(directory) / "xdg-open"
    check(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o755
          and opener.stat().st_uid == os.getuid() and stat.S_IMODE(opener.stat().st_mode) == 0o755,
          "made by whoever runs Switchyard, readable by the agent, writable by nobody else")
    ran = subprocess.run([str(opener), "https://example.com"], capture_output=True, text=True)
    check(ran.returncode == 0 and ran.stdout == ran.stderr == "", "and it opens nothing, silently")
    # With the desktop branch's own transform first (`-u` options straight after `env`), `env` still parses it.
    desktop = ["env", "-u", "DISPLAY", "-u", "WAYLAND_DISPLAY", "WAYLAND_DISPLAY=/run/user/1000/wayland-1",
               "HOME=/tmp", "PATH=/usr/bin:/bin", "sh", "-c", 'printf "%s|%s" "$BROWSER" "$(command -v xdg-open)"']
    args, _ = sil.no_browser_transform(desktop, {})
    ran = subprocess.run(args, capture_output=True, text=True)
    check(ran.returncode == 0 and ran.stdout == f"true|{opener}",
          f"the CLI sees BROWSER=true, and xdg-open is the no-op: {ran.stdout!r} {ran.stderr!r}")
    check(sil.no_browser_dir() == directory, "one directory per process")


class _ScriptedSession:
    """A provider that prints `chunks`, one per read, then exits."""

    def __init__(self, chunks, *, size=None) -> None:
        self.chunks = list(chunks)
        self.size = size
        self.reserved: list[int] = []

    def poll(self):
        return 0 if not self.chunks else None

    def read(self):
        return self.chunks.pop(0) if self.chunks else ""

    def write(self, _text):
        pass

    def terminate(self):
        self.chunks = []

    def wait(self, timeout=None):
        return 0

    def outer_size(self):
        return self.size

    def reserve_rows(self, count):
        self.reserved.append(count)
        return True


def _watch(chunks, *, size=None, link=True):
    written: list[str] = []
    said: list[str] = []
    session = _ScriptedSession(chunks, size=size)
    watcher = sil.SignInLink(cli="claude", owner_user="test11-agent") if link else None
    run_provider_first_run_session(
        cli="claude", args=["claude", "auth", "login"], kwargs={}, is_complete=lambda: False, watching="sign-in",
        session_factory=lambda *_a, **_k: session, output_write=written.append, sleep=lambda _s: None,
        purpose="sign-in", print_func=said.append, **({"sign_in_link": watcher} if link else {}))
    return "".join(written), said, session


def test_the_watched_sign_in_step_prints_the_link_in_the_users_terminal() -> None:
    shown, _said, _session = _watch([CLAUDE_OUT[:70], CLAUDE_OUT[70:200], CLAUDE_OUT[200:]])
    check(shown.count(sil.link(CLAUDE_URL)) == 1 and shown.index(sil.link(CLAUDE_URL)) < shown.index("Paste code"),
          f"the step's own output carries the link once, before the code prompt: {shown!r}")
    plain, _said, _session = _watch([CLAUDE_OUT], link=False)
    check(sil.link(CLAUDE_URL) not in plain, "a step without a watcher prints only what the CLI did")
    full, said, session = _watch([AGY_OUT[:120], AGY_OUT[120:]], size=(40, 120))
    check(session.reserved and sil.link(AGY_URL) in full and "\x1b7" in full,
          f"a full-screen sign-in gets its reserved rows through the session: {session.reserved}")
    check(full.rstrip("\x1b[H\x1b[2J").endswith("\x1b7\x1b[r\x1b8") or "\x1b7\x1b[r\x1b8" in full[-40:],
          f"and the whole terminal is the scroll region again when the step ends: {full[-60:]!r}")
    _full, said, _session = _watch([AGY_OUT])
    check(any(sil.link(AGY_URL) in line for line in said),
          f"and with nowhere to draw it, the link is said once the CLI's screen is gone: {said}")


def test_a_real_pty_keeps_the_reserved_rows_from_the_provider_across_a_resize() -> None:
    import fcntl
    import struct
    import termios
    from scripts import provider_session

    def pty_size(session):
        rows, cols, _x, _y = struct.unpack("HHHH", fcntl.ioctl(session._master, termios.TIOCGWINSZ, b"\0" * 8))
        return rows, cols

    real = provider_session.PtyForegroundSession._outer_size
    sizes = [(40, 120)]
    provider_session.PtyForegroundSession._outer_size = staticmethod(lambda: sizes[-1])
    session = provider_session.PtyForegroundSession(["sleep", "5"])
    try:
        check(pty_size(session) == (40, 120) and session.outer_size() == (40, 120), "the provider starts at full size")
        check(session.reserve_rows(9) and pty_size(session) == (31, 120), f"then 9 rows shorter: {pty_size(session)}")
        sizes.append((30, 100))
        session.sync_window_size()
        check(pty_size(session) == (21, 100) and session.outer_size() == (30, 100),
              f"and a resized window keeps the rows kept: {pty_size(session)}")
        check(not session.reserve_rows(30), "never the whole terminal")
    finally:
        provider_session.PtyForegroundSession._outer_size = real
        session.terminate()
        session.wait(timeout=5)
        session.close()


def test_the_first_run_phase_signs_in_first_with_no_browser_of_the_agents() -> None:
    from team_launcher_test_helpers import FirstRunAuthRunner, _write_first_run_auth_config
    from scripts import team_launcher
    from scripts.team_launcher import load_project_config

    class Raw(FirstRunAuthRunner):
        def __init__(self) -> None:
            super().__init__()
            self.raw: list[list[str]] = []

        def __call__(self, args, **kwargs):
            self.raw.append([str(a) for a in args])
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="syrd782.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config("otto", _write_first_run_auth_config(
            tmp_path, roles=[("director", "claude"), ("ops", "codex"), ("inspector", "agy")]))
        runner = Raw()
        team_launcher.run_first_run_auth_phase(config, owner_user="otto-agent", owner_home=owner_home, runner=runner,
                                               print_func=lambda _line: None)
    interactive = [call for call in runner.raw if call[-1:] in (["claude"], ["agy"], ["codex"], ["login"])
                   and "status" not in call and "models" not in call]
    check(len(interactive) >= 4, f"the sign-in, first-run and trust steps ran: {interactive}")
    allowed = {"HOME", "PATH", "TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "BROWSER",
               *team_launcher.DESKTOP_ENV_KEYS}
    for call in interactive:
        assigned = [part.split("=", 1)[0] for part in call[call.index("env") + 1:] if "=" in part
                    and not part.startswith("-")]
        check("BROWSER=true" in call, f"every step that takes the terminal opens no browser of the agent's: {call}")
        check(set(assigned) <= allowed, f"and carries nothing outside the allow-list: {sorted(set(assigned) - allowed)}")
        path = next(part for part in call if part.startswith("PATH="))
        check(path.split("=", 1)[1].split(":")[0] == sil.no_browser_dir(), f"the no-op opener comes first: {path}")
    order = [" ".join(call[call.index("env") + 1:][-3:]) for call in interactive]
    first_login = next(i for i, call in enumerate(interactive) if call[-2:] == ["auth", "login"])
    first_run = next(i for i, call in enumerate(interactive) if call[-1:] == ["claude"])
    check(first_login < first_run, f"Claude's sign-in comes before its own first run: {order}")
    for signed_in in (True, False):
        with tempfile.TemporaryDirectory(prefix="syrd782.") as tmp:
            tmp_path = Path(tmp)
            owner_home = tmp_path / "home" / "otto-agent"
            owner_home.mkdir(parents=True)
            config = load_project_config("otto", _write_first_run_auth_config(tmp_path, roles=[("director", "claude")]))
            said: list[str] = []
            team_launcher.run_first_run_auth_phase(
                config, owner_user="otto-agent", owner_home=owner_home,
                runner=FirstRunAuthRunner(authenticated_after_login=signed_in), print_func=said.append)
        told = [line for line in said if "will now run in this terminal as otto-agent. Answer" in line]
        check(told and (("it is already signed in" in told[0]) == signed_in) and (("a sign-in" in told[0]) != signed_in),
              f"signed in={signed_in}: the first run is announced as asking for a sign-in only when it will: {told}")


def test_each_watched_sign_in_step_is_given_its_link_and_no_other_step_is() -> None:
    from team_launcher_test_helpers import FirstRunAuthRunner, _write_first_run_auth_config
    from scripts import team_launcher
    from scripts.team_launcher import load_project_config

    given: list[tuple[str, object]] = []
    real = team_launcher.run_provider_first_run_session

    def watched(**kwargs):
        given.append((kwargs.get("purpose"), kwargs.get("sign_in_link")))
        return True

    team_launcher.run_provider_first_run_session = watched
    try:
        with tempfile.TemporaryDirectory(prefix="syrd782.") as tmp:
            tmp_path = Path(tmp)
            owner_home = tmp_path / "home" / "otto-agent"
            owner_home.mkdir(parents=True)
            config = load_project_config("otto", _write_first_run_auth_config(tmp_path, roles=[("ops", "codex")]))
            # Probes through the fake; the steps a person sits in front of through the watched session.
            team_launcher.run_first_run_auth_phase(config, owner_user="otto-agent", owner_home=owner_home,
                                                   runner=FirstRunAuthRunner(), foreground_runner=None,
                                                   print_func=lambda _line: None)
    finally:
        team_launcher.run_provider_first_run_session = real
    sign_ins = [link for purpose, link in given if purpose == "sign-in"]
    others = [link for purpose, link in given if purpose != "sign-in"]
    check(len(sign_ins) == 1 and isinstance(sign_ins[0], sil.SignInLink) and sign_ins[0].cli == "codex"
          and sign_ins[0].owner_user == "otto-agent", f"the sign-in step is watched with its link: {given}")
    check(others and all(link is None for link in others), f"and no other step prints one: {given}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"sign_in_link_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
