#!/usr/bin/env python3
"""SYRD-564: a recorded Hermes session id is passed to `--resume` only if the role's own home holds it.

Otto (2026-10-07, release 2951c5c9): a recovered Hermes pane started with
`--resume <Oct 4 id>` that its home's state.db did not have. Hermes printed
"Session not found", kept running and answered every prompt the same way, while
the launcher saw the `hermes` process and called the resume verified.
`hermes --resume` looks the id up in `<HERMES_HOME>/state.db`'s sessions table
(SessionDB.get_session); the launcher's resume preflight had no Hermes branch.

Every case starts the role through the real start path (`run_role_pane`, which
the launch, start, recover and runtime-switch paths all reach), with tmux
recorded, from an isolated role store whose home and project-wide home are
real directories. The databases are written by Hermes's own SessionDB where
the installed Hermes is used, offline; nothing starts a provider.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile

from team_launcher_hermes_session_isolation_test import _hermes_role
from team_launcher_test_helpers import *  # noqa: F403 - the launcher suites' shared fakes

CHECKS = 0
HERMES_PYTHON = Path("/opt/hermes/venv/bin/python3")


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def sessions_db(home: Path, *ids: str) -> None:
    """A state.db with Hermes's sessions table holding `ids` (the columns `get_session` reads)."""
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "state.db") as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL)")
        connection.executemany("INSERT OR IGNORE INTO sessions(id, source) VALUES (?, 'cli')", [(i,) for i in ids])


class Pane:
    """An isolated Hermes role: its own store under roles/, its own home, and the project-wide home beside it."""

    def __init__(self, tmp: Path) -> None:
        self.owner_home = tmp / "home" / "porter-agent"
        (self.owner_home / ".hermes").mkdir(parents=True)
        self.project_store = self.owner_home / ".local" / "state" / "porter-ticket-board" / "pane-sessions"
        self.store = self.project_store / "roles" / "bulk"
        self.store.mkdir(parents=True)
        self.role = _hermes_role("bulk", home=self.owner_home)
        self.home = team_launcher.hermes_home_for_role(self.role, session_dir=self.store)
        self.legacy_home = team_launcher.hermes_home_for_role(self.role, session_dir=self.project_store)
        self.record = self.store / session_file_name(self.role.target)

    def record_session(self, session_id: str) -> None:
        self.record.write_text(json.dumps({"target": self.role.target, "session_id": session_id}) + "\n", encoding="utf-8")

    def start(self) -> tuple[int, str, str]:
        runner = FakeRunner(existing_sessions={self.role.tmux_session}, current_commands={self.role.target: "hermes"})
        stderr = StringIO()
        with redirect_stderr(stderr):
            code = run_role_pane(self.role, mode="reload", session_dir=self.store,
                                 pane_state_dir=self.owner_home / "pane-state", runner=runner)
        started = next(call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "porter-bulk"])
        return code, started[-1], stderr.getvalue()


def case(fn):
    def run() -> None:
        with tempfile.TemporaryDirectory(prefix="syrd564.") as tmp:
            fn(Pane(Path(tmp)))
    run.__name__ = fn.__name__
    return run


@case
def test_a_session_the_home_holds_is_resumed(pane: Pane) -> None:
    sessions_db(pane.home, "kept-session")
    pane.record_session("kept-session")
    code, command, said = pane.start()
    check(code == 0 and "--resume kept-session" in command and f"HERMES_HOME={pane.home}" in command,
          f"a session in the role's own home is resumed from it: {command}")
    check(pane.record.exists() and "kept-session" in pane.record.read_text() and "not passing" not in said,
          f"and its record is kept: {said}")


@case
def test_a_stale_id_starts_fresh_and_says_where_it_looked(pane: Pane) -> None:
    sessions_db(pane.home, "some-other-session")
    pane.record_session("oct-4-session")
    code, command, said = pane.start()
    check(code == 0 and "--resume" not in command and f"HERMES_HOME={pane.home}" in command,
          f"an id the home does not hold is never passed: {command}")
    check(f"recorded hermes session oct-4-session for bulk is not in {pane.home / 'state.db'}" in said
          and "Session not found" in said,
          f"and the reason names the database it checked: {said}")
    superseded = pane.record.with_name(pane.record.name + ".superseded")
    check(not pane.record.exists() and "oct-4-session" in superseded.read_text(),
          "the record is set aside, so the next start does not try it again")


@case
def test_an_id_only_the_project_wide_home_holds_is_not_resumed_from_there(pane: Pane) -> None:
    sessions_db(pane.home, "role-session")
    sessions_db(pane.legacy_home, "oct-4-session")
    pane.record_session("oct-4-session")
    code, command, said = pane.start()
    check(code == 0 and "--resume" not in command and str(pane.legacy_home) not in command,
          f"neither the id nor the project-wide home is used: {command}")
    check(f"it exists only in the project-wide home {pane.legacy_home}, which this role no longer runs from" in said,
          f"and the message says exactly where the session is: {said}")


@case
def test_a_home_without_a_database_or_an_unreadable_one_confirms_nothing(pane: Pane) -> None:
    pane.record_session("oct-4-session")
    code, command, said = pane.start()
    check(code == 0 and "--resume" not in command and "is not in" in said, f"no database, no resume: {said}")
    pane.home.mkdir(parents=True, exist_ok=True)
    (pane.home / "state.db").write_text("not a database", encoding="utf-8")
    pane.record_session("oct-4-session")
    code, command, said = pane.start()
    check(code == 0 and "--resume" not in command and "cannot be confirmed" in said and "could not be read" in said,
          f"an unreadable database is no confirmation either: {said}")


@case
def test_the_preflight_agrees_with_hermes_own_session_store(pane: Pane) -> None:
    """Written by the installed Hermes's SessionDB, offline: what it can resume, the preflight lets through."""
    check(HERMES_PYTHON.exists(), f"Hermes is installed at {HERMES_PYTHON}")
    pane.home.mkdir(parents=True, exist_ok=True)
    script = ("import sys; from pathlib import Path; from hermes_state import SessionDB; "
              "db = SessionDB(db_path=Path(sys.argv[1])); db.create_session('hermes-made', source='cli'); "
              "print('ok', db.get_session('hermes-made') is not None, db.get_session('never-made') is None)")
    done = subprocess.run([str(HERMES_PYTHON), "-I", "-c", script, str(pane.home / "state.db")], capture_output=True,
                          text=True, timeout=120, env={"PATH": "/usr/bin:/bin", "HOME": str(pane.owner_home),
                                                       "HERMES_HOME": str(pane.home)})
    check(done.returncode == 0 and done.stdout.strip().endswith("ok True True"), f"Hermes wrote its store: {done.stderr[-600:]}")
    allows = team_launcher._resume_preflight_allows_attempt
    check(allows(pane.role, "hermes-made", session_dir=pane.store) == (True, ""),
          "a session Hermes itself can resume passes")
    check(allows(pane.role, "never-made", session_dir=pane.store)[0] is False, "and one it cannot does not")


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"hermes_resume_preflight_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
