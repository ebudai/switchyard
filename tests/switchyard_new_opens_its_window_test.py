#!/usr/bin/env python3
"""`switchyard new` has to end with the project's window open.

Live UAT on test10 at 803f9fe: first-run setup finished cleanly, all six role
sessions started, the command printed "switchyard: full pane window started for
test10" -- and returned with no window. `new` needs root, so the installed
wrapper replaced itself with `sudo ... new`. Root has no screen: in the viewer
layout the presentation is one detached tmux session, and a window for it is
only ever opened by the person's own process, after the control bridge hands
it back. After `new` there was no such process left (SYRD-221 UAT).

Both halves are driven here: the wrapper exactly as install-switchyard renders
it, with stub `sudo` and stub target standing in for root and the launcher; and
the root side's report back to it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher  # noqa: E402
from standalone_test_runner import run_module_tests  # noqa: E402

GENERATOR = ROOT / "scripts" / "install-switchyard"


# --- the wrapper -----------------------------------------------------------


def _render_wrapper(tmp: Path, target: Path) -> Path:
    """The whole wrapper, dispatch included, rendered by BASH.

    Through the same unquoted heredoc the generator uses: `$...` expands and
    backticks execute there, so a Python rendering would show text the
    installed file never contains (SYRD-204).
    """
    lines = GENERATOR.read_text(encoding="utf-8").splitlines()
    opening = next(i for i, line in enumerate(lines) if line.strip() == 'cat >"$tmp" <<EOF')
    closing = next(i for i in range(opening + 1, len(lines)) if lines[i].strip() == "EOF")
    body = "\n".join(lines[opening + 1 : closing])
    script = tmp / "render.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "quote() { printf '%q' \"$1\"; }\n"
        f"default_target={target}\n"
        'help_text="HELP"\nversion_text="VERSION"\nrecovery_command="RECOVERY"\n'
        f"cat <<EOF\n{body}\nEOF\n",
        encoding="utf-8",
    )
    done = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    wrapper = tmp / "switchyard"
    wrapper.write_text(done.stdout, encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


#: Stands in for sudo: `-n -v` succeeds, and anything else is run as given --
#: as this user, which is all the wrapper's side of the contract needs.
STUB_SUDO = """#!/usr/bin/env bash
if [[ "$1" == "-n" && "$2" == "-v" ]]; then exit 0; fi
if [[ "$1" == "-n" ]]; then shift; fi
echo "SUDO $*" >> "$LOG"
exec "$@"
"""


def _stub_target(tmp: Path, new_behaviour: str) -> Path:
    """The launcher: classifies `new` as root, runs `new`, and opens projects."""
    target = tmp / "target"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "--switchyard-wrapper-requires-root" ]]; then\n'
        '  if [[ "$2" == "new" ]]; then echo requires-root; else echo no-root; fi; exit 0\n'
        "fi\n"
        'if [[ "$1" == "new" ]]; then\n'
        '  echo "NEW uid=$(id -u) result=${SWITCHYARD_NEW_RESULT_FILE:-}" >> "$LOG"\n'
        f"  {new_behaviour}\n"
        "fi\n"
        'echo "OPEN $*" >> "$LOG"\n',
        encoding="utf-8",
    )
    target.chmod(0o755)
    return target


def _run_new(new_behaviour: str, *argv: str) -> tuple[int, str, str]:
    with tempfile.TemporaryDirectory(prefix="syrd221-new.") as tmp:
        root = Path(tmp)
        log = root / "log"
        log.touch()
        sudo = root / "sudo"
        sudo.write_text(STUB_SUDO, encoding="utf-8")
        sudo.chmod(0o755)
        target = _stub_target(root, new_behaviour)
        wrapper = _render_wrapper(root, target)
        scratch = root / "tmp"
        scratch.mkdir()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root),
            "LOG": str(log),
            "TMPDIR": str(scratch),
            "SWITCHYARD_SUDO_BIN": str(sudo),
        }
        done = subprocess.run(
            [str(wrapper), "new", *argv], capture_output=True, text=True, env=env, timeout=30
        )
        leftovers = sorted(p.name for p in scratch.iterdir())
        assert not leftovers, f"the result file was left behind: {leftovers}"
        return done.returncode, log.read_text(encoding="utf-8"), done.stderr


def test_a_new_project_s_window_is_opened_as_the_person_who_asked() -> None:
    """test10, fixed: `new` finishes as root, then the project is opened here."""
    code, log, stderr = _run_new('printf test10 > "$SWITCHYARD_NEW_RESULT_FILE"; exit 0')
    assert code == 0, (code, log, stderr)
    lines = log.splitlines()
    assert any(line.startswith("SUDO env ") and " new" in line for line in lines), log
    assert "OPEN test10" in lines, f"the new project was not opened afterwards: {log!r}"
    # And the opening is NOT through sudo: it is this process, as this person,
    # which is what can reach the screen.
    assert not any(line.startswith("SUDO") and "test10" in line and " new" not in line
                   for line in lines), log
    assert lines.index("OPEN test10") > next(
        i for i, line in enumerate(lines) if line.startswith("NEW ")
    ), log


def test_a_failed_new_opens_nothing_and_keeps_its_status() -> None:
    code, log, _ = _run_new('printf test10 > "$SWITCHYARD_NEW_RESULT_FILE"; exit 3')
    assert code == 3, (code, log)
    assert "OPEN" not in log, log


def test_a_new_that_reports_nothing_opens_nothing() -> None:
    """A deferred launch, or a layout that opened its own window."""
    code, log, _ = _run_new("exit 0")
    assert code == 0, (code, log)
    assert "OPEN" not in log, log


def test_a_reported_name_that_is_not_a_project_is_not_run() -> None:
    for bad in ("../etc", "Test10", "a b", "-rf", "x" * 41):
        code, log, stderr = _run_new(f"printf %s '{bad}' > \"$SWITCHYARD_NEW_RESULT_FILE\"; exit 0")
        assert code != 0, (bad, code, log)
        assert "OPEN" not in log, (bad, log)
        assert "switchyard <project>" in stderr, (bad, stderr)


def test_the_result_file_is_this_person_s_own() -> None:
    """Root is handed a file the wrapper made, private to the person who asked."""
    code, log, _ = _run_new(
        'stat -c "MODE %a OWNER %u" "$SWITCHYARD_NEW_RESULT_FILE" >> "$LOG"; exit 0'
    )
    assert code == 0, (code, log)
    assert f"MODE 600 OWNER {os.getuid()}" in log, log


def test_every_other_root_verb_still_replaces_the_wrapper() -> None:
    """Only `new` changed shape; nothing else gains a continuation."""
    with tempfile.TemporaryDirectory(prefix="syrd221-root.") as tmp:
        root = Path(tmp)
        log = root / "log"
        log.touch()
        sudo = root / "sudo"
        sudo.write_text(STUB_SUDO, encoding="utf-8")
        sudo.chmod(0o755)
        target = root / "target"
        target.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "--switchyard-wrapper-requires-root" ]]; then echo requires-root; exit 0; fi\n'
            'echo "RAN $* result=${SWITCHYARD_NEW_RESULT_FILE:-none}" >> "$LOG"\n',
            encoding="utf-8",
        )
        target.chmod(0o755)
        wrapper = _render_wrapper(root, target)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root),
               "LOG": str(log), "SWITCHYARD_SUDO_BIN": str(sudo)}
        done = subprocess.run([str(wrapper), "upgrade"], capture_output=True, text=True, env=env)
        assert done.returncode == 0, done.stderr
        assert "RAN upgrade result=none" in log.read_text(encoding="utf-8"), log.read_text()


# --- the root side -----------------------------------------------------------


def _own_ids() -> dict[str, str]:
    return {"SUDO_UID": str(os.getuid()), "SUDO_GID": str(os.getgid())}


def test_the_project_is_reported_into_the_callers_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = Path(tmp) / "result"
        result.write_text("")
        assert team_launcher._report_new_project_to_caller(str(result), "test10", environ=_own_ids())
        assert result.read_text() == "test10"


def test_a_link_is_not_followed() -> None:
    """The file is the caller's to choose, so a link to elsewhere must not be written."""
    with tempfile.TemporaryDirectory() as tmp:
        elsewhere = Path(tmp) / "elsewhere"
        elsewhere.write_text("untouched")
        link = Path(tmp) / "result"
        link.symlink_to(elsewhere)
        assert not team_launcher._report_new_project_to_caller(str(link), "test10", environ=_own_ids())
        assert elsewhere.read_text() == "untouched"


def test_nothing_is_created() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "missing"
        assert not team_launcher._report_new_project_to_caller(str(missing), "test10", environ=_own_ids())
        assert not missing.exists()


def test_it_is_never_written_as_anyone_but_the_caller() -> None:
    """Unprivileged, it cannot become another user, so it must refuse rather than write as itself."""
    with tempfile.TemporaryDirectory() as tmp:
        result = Path(tmp) / "result"
        result.write_text("")
        other = {"SUDO_UID": str(os.getuid() + 1), "SUDO_GID": str(os.getgid())}
        if os.geteuid() != 0:
            assert not team_launcher._report_new_project_to_caller(str(result), "test10", environ=other)
            assert result.read_text() == ""


def test_only_a_project_name_is_ever_reported() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = Path(tmp) / "result"
        result.write_text("")
        for bad in ("../etc", "Test10", "", "a b"):
            assert not team_launcher._report_new_project_to_caller(str(result), bad, environ=_own_ids())
        assert result.read_text() == ""


def _announce(mode: str, environ: dict[str, str], report=None) -> tuple[list[str], list[tuple]]:
    said: list[str] = []
    calls: list[tuple] = []

    def recording(path, project, *, environ):
        calls.append((path, project))
        return True if report is None else report

    team_launcher.announce_new_project_presentation(
        "test10", resolved_layout_mode=mode, environ=environ, report=recording,
        print_func=said.append,
    )
    return said, calls


def test_a_viewer_launch_asks_the_wrapper_to_open_it() -> None:
    said, calls = _announce(team_launcher.LAYOUT_MODE_VIEWER,
                            {team_launcher.NEW_RESULT_FILE_ENV: "/tmp/r", **_own_ids()})
    assert calls == [("/tmp/r", "test10")], calls
    assert any("its window opens next" in line for line in said), said
    assert not any("full pane window started" in line for line in said), said


def test_a_viewer_launch_with_no_wrapper_says_how_to_open_it() -> None:
    """Never "window started" when none was: test10's command said exactly that."""
    for environ, report in (({}, None), ({team_launcher.NEW_RESULT_FILE_ENV: "/tmp/r"}, False)):
        said, _calls = _announce(team_launcher.LAYOUT_MODE_VIEWER, environ, report)
        assert not any("full pane window started" in line for line in said), said
        assert any("switchyard test10" in line and "cannot open the window" in line
                   for line in said), said


def test_a_layout_that_opens_its_own_window_is_left_as_it_was() -> None:
    said, calls = _announce(team_launcher.LAYOUT_MODE_SEPARATE,
                            {team_launcher.NEW_RESULT_FILE_ENV: "/tmp/r"})
    assert calls == [], "a layout that opened its own window would get a second one"
    assert said == ["switchyard: full pane window started for test10"], said


def main() -> int:
    run_module_tests(globals())
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"switchyard_new_opens_its_window_test: {count} tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
