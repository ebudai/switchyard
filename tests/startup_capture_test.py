#!/usr/bin/env python3
"""SYRD-532: a role whose command exits during startup leaves its exit status and last lines.

MEFP's luna-6 exited straight after its runtime switch; the start said only
"did not leave a live mefp-luna-6 session", because tmux closed the pane and
its output with it. This drives the real `_start_role_session` and the real
start verifier against a private tmux server -- set SYRD532_TMUX to test
another tmux build (Zorin runs 3.2a) -- and checks both halves: what a failure
now keeps, and that a start which succeeds is exactly what it was.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher  # noqa: E402
from scripts import startup_capture  # noqa: E402
from scripts.provider_resume import _detached_launch_verified  # noqa: E402

CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def role(name: str, cli: list[str], live: list[str], home: Path) -> team_launcher.RoleConfig:
    return team_launcher.RoleConfig(
        role=name, slot=0, detached=True, tmux_session=f"s532-{name}", target=f"s532-{name}:0.0",
        workdir=str(home), cli=cli, model="", model_arg="--model", effort="", yolo=False, extra_args=[],
        resume_mode="flag", resume_flag="--resume", resume_subcommand="resume",
        fresh_session_per_ticket=False, live_commands=live, env={},
    )


def main() -> int:
    real_tmux = os.environ.get("SYRD532_TMUX") or shutil.which("tmux")
    check(bool(real_tmux), "a tmux binary is available")
    version = subprocess.run([real_tmux, "-V"], capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="s532.", dir="/tmp") as scratch:
        scratch = Path(scratch)
        socket = scratch / "tmux.sock"
        bin_dir = scratch / "bin"
        bin_dir.mkdir()
        # Every `tmux` the launcher runs reaches this private server, never an ambient one.
        (bin_dir / "tmux").write_text(f'#!/bin/sh\nexec "{real_tmux}" -S "{socket}" -f /dev/null "$@"\n')
        (bin_dir / "tmux").chmod(0o755)
        saved_env = {key: os.environ.get(key) for key in ("PATH", "TMUX", "SHELL")}
        os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
        os.environ.pop("TMUX", None)
        os.environ["SHELL"] = "/bin/sh"
        sessions = scratch / "sessions"
        panes = scratch / "panes"
        messages: list[str] = []
        try:
            def start(r: team_launcher.RoleConfig) -> tuple[int, str]:
                import contextlib
                import io
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = team_launcher._start_role_session(
                        r, session_dir=sessions, pane_state_dir=panes, prefer_resume=False, seed_source="syrd532",
                        post_start_verifier=lambda: _detached_launch_verified(r), startup_capture=True)
                return code, err.getvalue()

            def exists(r: team_launcher.RoleConfig) -> bool:
                return subprocess.run(["tmux", "has-session", "-t", r.tmux_session], capture_output=True).returncode == 0

            def capture_of(r: team_launcher.RoleConfig) -> Path:
                return startup_capture.capture_path(sessions, team_launcher.session_file_name(r.target))

            def disarmed_then_exits_quietly(r: team_launcher.RoleConfig) -> tuple[bool, str]:
                """What an unarmed session does when its command exits later: it goes, and nothing is kept.

                Read from behaviour, not from `show-hooks`, which lists nothing
                here even while the hook is armed.
                """
                remain = subprocess.run(["tmux", "show-window-options", "-t", r.tmux_session, "-v", "remain-on-exit"],
                                        capture_output=True, text=True).stdout.strip()
                size_before = capture_of(r).stat().st_size
                pid = subprocess.run(["tmux", "display-message", "-p", "-t", r.tmux_session, "#{pane_pid}"],
                                     capture_output=True, text=True).stdout.strip()
                os.kill(int(pid), 15)
                for _ in range(30):
                    if not exists(r):
                        break
                    time.sleep(0.1)
                quiet = remain in ("", "off") and not exists(r) and capture_of(r).stat().st_size == size_before == 0
                return quiet, f"remain-on-exit={remain!r} present={exists(r)} captured={capture_of(r).stat().st_size}"

            # 1. A command that fails at once, as luna-6's did.
            failing = role("fails", ["/bin/sh", "-c", "echo to-stdout; echo syrd532-refused-option >&2; exit 3"], ["sh"], scratch)
            code, said = start(failing)
            messages.append(said)
            check(code == 1, f"{version}: a start whose command exits still fails: {code}")
            check("did not leave a live s532-fails session" in said, f"{version}: with the same message as before: {said!r}")
            check("command exited with status 3 during startup" in said, f"{version}: and now its exit status: {said!r}")
            check(str(capture_of(failing)) in said and "readable by this account only" in said,
                  f"{version}: and where its output is, not the output itself: {said!r}")
            check("syrd532-refused-option" not in said, f"{version}: the output stays in the file")
            kept = capture_of(failing).read_text(errors="replace")
            check("syrd532-refused-option" in kept and "to-stdout" in kept, f"{version}: the file holds what it printed: {kept!r}")
            check(stat.S_IMODE(capture_of(failing).stat().st_mode) == 0o600, f"{version}: readable by this account only")
            check(len(kept.encode()) <= startup_capture.CAPTURE_READ_LIMIT, f"{version}: bounded")
            check(not exists(failing), f"{version}: and the session is gone, as a dead start's always was")

            # 2. A command that exits 0 at once is not a start either.
            quits = role("quits", ["/bin/sh", "-c", "exit 0"], ["sh"], scratch)
            code, said = start(quits)
            check(code == 1 and "exited with status 0 during startup" in said, f"{version}: an instant clean exit fails too: {said!r}")

            # 3. A command that stays up starts exactly as before.
            lives = role("lives", ["sleep", "300"], ["sleep"], scratch)
            code, said = start(lives)
            check(code == 0 and exists(lives), f"{version}: a live command starts: {code} {said!r}")
            check("during startup" not in said, f"{version}: with no startup report")
            quiet, detail = disarmed_then_exits_quietly(lives)
            check(quiet, f"{version}: once verified it is disarmed: a later exit removes it and keeps nothing: {detail}")

            # 5. A resume whose command dies, then its fresh fallback dies too: each is reported.
            def record(r: team_launcher.RoleConfig) -> None:
                import json
                sessions.mkdir(parents=True, exist_ok=True)
                (sessions / team_launcher.session_file_name(r.target)).write_text(
                    json.dumps({"target": r.target, "session_id": "syrd532-session"}))

            def resume(r: team_launcher.RoleConfig) -> tuple[int, str]:
                import contextlib
                import io
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = team_launcher._start_role_session(
                        r, session_dir=sessions, pane_state_dir=panes, prefer_resume=True, seed_source="syrd532",
                        post_start_verifier=lambda: _detached_launch_verified(r), startup_capture=True)
                return code, err.getvalue()

            dies_twice = role("resumefails", ["/bin/sh", "-c", "echo resume-refused >&2; exit 4"], ["sh"], scratch)
            record(dies_twice)
            code, said = resume(dies_twice)
            check(code == 1 and "resume failed for resumefails" in said and "after resume fallback" in said,
                  f"{version}: a dead resume falls back and the fallback fails, as before: {code} {said!r}")
            check(said.count("command exited with status 4 during startup") == 2,
                  f"{version}: each dead attempt is reported: {said!r}")
            check("resume-refused" in capture_of(dies_twice).read_text(errors="replace"),
                  f"{version}: and the last one's lines are kept")
            check(not exists(dies_twice), f"{version}: leaving no session")

            # 6. A resume that stays up is disarmed like a fresh start.
            # `--resume <id>` is appended; a shell script takes it as $0/$1 and ignores it.
            resumes = role("resumelives", ["/bin/sh", "-c", "exec sleep 300"], ["sleep"], scratch)
            record(resumes)
            code, said = resume(resumes)
            check(code == 0 and exists(resumes) and "during startup" not in said,
                  f"{version}: a live resume starts and reports nothing: {code} {said!r}")
            quiet, detail = disarmed_then_exits_quietly(resumes)
            check(quiet, f"{version}: and is disarmed like a fresh start: {detail}")

            # 7. The path `worker-pool start` takes -- run_detached_role -- arms itself.
            from scripts.role_pane_entry import run_detached_role
            import contextlib
            import io
            detached = role("detached", ["/bin/sh", "-c", "echo detached-refused >&2; exit 6"], ["sh"], scratch)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = run_detached_role(detached, mode="attach-or-start", session_dir=sessions, pane_state_dir=panes)
            check(code == 1 and "exited with status 6 during startup" in err.getvalue(),
                  f"{version}: worker-pool's detached start reports the exit: {code} {err.getvalue()!r}")

            # 8. Every other start is byte-for-byte what it was: no arming, no extra call.
            calls: list[list[str]] = []
            quiet_role = role("unarmed", ["/bin/sh", "-c", "exit 7"], ["sh"], scratch)

            def recording(args, **kwargs):
                calls.append(list(args))
                return subprocess.run(args, **kwargs)

            with contextlib.redirect_stderr(io.StringIO()) as unarmed_err:
                code = team_launcher._start_role_session(
                    quiet_role, session_dir=sessions, pane_state_dir=panes, prefer_resume=False, seed_source="syrd532",
                    post_start_verifier=lambda: _detached_launch_verified(quiet_role), runner=recording)
            new_sessions = [c for c in calls if c[:2] == ["tmux", "new-session"]]
            check(new_sessions == [team_launcher.tmux_new_session_args(quiet_role, session_dir=sessions, pane_state_dir=panes)],
                  f"{version}: an unarmed start runs exactly the argv it always did: {new_sessions!r}")
            check(not any(";" in c or "set-hook" in c for c in calls), f"{version}: and nothing else: {calls!r}")
            check(code == 1 and "during startup" not in unarmed_err.getvalue(), f"{version}: and reports as before")

            # 4. A path tmux cannot be handed safely: no capture, and the start is what it was.
            odd = scratch / "o'dd"
            odd_role = role("odd", ["/bin/sh", "-c", "exit 5"], ["sh"], scratch)
            check(startup_capture.prepare(startup_capture.capture_path(odd, "x")) is None,
                  "a path with a quote is not armed")
            code, said = team_launcher._start_role_session(
                odd_role, session_dir=odd, pane_state_dir=panes, prefer_resume=False, seed_source="syrd532",
                post_start_verifier=lambda: _detached_launch_verified(odd_role), startup_capture=True), ""
            check(code == 1 and not exists(odd_role), f"{version}: an unarmed failing start still fails, and leaves nothing")
        finally:
            subprocess.run(["tmux", "kill-server"], capture_output=True)
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    print(f"startup_capture_test: {CHECKS} checks ok ({version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
