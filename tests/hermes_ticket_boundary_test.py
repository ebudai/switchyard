#!/usr/bin/env python3
"""SYRD-693: the board clears a Hermes pane into a ticket only when the Hermes running there started closed.

Audit (on 8c31c512) found that a workflow apply makes a running role ephemeral without restarting it, and a
Hermes `/clear` keeps the agent and the memory settings it started with. So the check reads what the pane's own
process started from. Here it runs against a real tmux server of its own (`TMUX_TMPDIR`, with the user bus
isolated) whose pane is started the way the launcher starts a Hermes role, `env ... HERMES_HOME=<home> <cli>`,
and against that pane's real process in /proc. The homes are written by Switchyard's own config writers.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

from scripts import hermes_role_config  # noqa: E402
from scripts.ticket_board import hermes_ticket_boundary as boundary  # noqa: E402

CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def role(name: str, **flags: bool) -> SimpleNamespace:
    return SimpleNamespace(role=name, effort="", hermes_local=None, ephemeral=flags.get("ephemeral", False),
                           fresh_session_per_ticket=flags.get("fresh", False))


def prepared(root: Path, name: str, **flags: bool) -> Path:
    """A role's Hermes home, as Switchyard prepares it for a cloud Hermes role with no effort."""
    shared, home = root / "shared", root / "homes" / name
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "config.yaml").write_text("model:\n  default: glm-5.3\nmemory:\n  memory_enabled: true\n", encoding="utf-8")
    home.mkdir(parents=True)
    (home / "config.yaml").symlink_to(shared / "config.yaml")  # as a Hermes role's home starts
    hermes_role_config.prepare_hermes_role_config(role(name, **flags), hermes_home=home, shared_home=shared)
    return home


def pane(name: str, home: Path) -> str:
    """A pane started as the launcher starts a Hermes role: one shell string, `env ... HERMES_HOME=<home>`, here
    with a stand-in for the CLI. The home is quoted, as the launcher's shlex.join quotes it."""
    import shlex

    command = shlex.join(["env", "-u", "TICKET_BOARD_PANE_SESSION_ID", f"HERMES_HOME={home}", "sleep", "120"])
    subprocess.run(["tmux", "new-session", "-d", "-s", name, command], check=True)
    return f"{name}:0.0"


def test_ephemeral_or_fresh_closes_the_home_and_persistent_does_not() -> None:
    root = Path(tempfile.mkdtemp(prefix="syrd693b-"))
    try:
        for name, flags, closed in (("workflow-ephemeral", {"ephemeral": True}, True),
                                    ("launcher-fresh", {"fresh": True}, True),
                                    ("persistent", {}, False)):
            home = prepared(root, name, **flags)
            text = (home / "config.yaml").read_text(encoding="utf-8") if not (home / "config.yaml").is_symlink() else ""
            check((boundary.TICKET_BOUNDARY_MARK in text.splitlines()[:boundary.MARK_LINES]) == closed,
                  f"{name}: the config carries the closed mark exactly when the board clears the role: {text[:400]}")
            check((home / "config.yaml").is_symlink() == (not closed),
                  f"{name}: a persistent role keeps the shared config, an ephemeral one gets its own")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_check_reads_what_the_running_pane_started_from() -> None:
    root = Path(tempfile.mkdtemp(prefix="syrd693 b-"))  # a space in every path, as shlex must quote it
    os.environ["TMUX_TMPDIR"] = str(root / "tmux")
    os.environ.pop("TMUX", None)
    (root / "tmux").mkdir(mode=0o700)
    isolate_tmux_bus(base=root)
    try:
        closed = prepared(root, "closed", ephemeral=True)
        target = pane("closed", closed)
        time.sleep(0.5)
        check(boundary.open_reason(target) == "", f"a pane started on a closed home may be cleared: "
                                                  f"{boundary.open_reason(target)}")

        persistent = prepared(root, "persistent")
        target = pane("persistent", persistent)
        time.sleep(0.5)
        said = boundary.open_reason(target)
        check("leaves Hermes's memory and session search on" in said and boundary.REMEDY in said,
              f"a pane started persistent, then declared ephemeral, is held with the remedy: {said}")

        # The workflow made it ephemeral and the home was prepared again, but the running Hermes predates that.
        late = root / "homes" / "persistent" / "config.yaml"
        late.unlink()
        hermes_role_config.prepare_hermes_role_config(role("persistent", ephemeral=True), hermes_home=late.parent,
                                                      shared_home=root / "shared")
        future = time.time() + 30
        os.utime(late, (future, future))
        said = boundary.open_reason(target)
        check("was written after" in said and "has not read it" in said,
              f"a closed config written after the pane started is not what its Hermes loaded: {said}")

        bare = pane("bare", root)
        subprocess.run(["tmux", "respawn-pane", "-k", "-t", bare, "sleep 120"], check=True)
        said = boundary.open_reason(bare)
        check("names no HERMES_HOME" in said, f"a pane whose start names no home is held: {said}")
        said = boundary.open_reason("nowhere:0.0")
        check(said.startswith("tmux did not say what nowhere:0.0 runs"), f"a missing pane is held: {said}")
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True, check=False)
        shutil.rmtree(root, ignore_errors=True)


def cutover_tenant(t, record: str):
    """The fixture's `app` as a cloud Hermes role the workflow made ephemeral (`ephemeral` alone, as projected),
    whose home still holds the open config of its persistent days, and a recorded conversation from then."""
    import json

    from scripts import team_launcher

    entry = next(role for role in t.raw["roles"] if role["role"] == "app")
    entry.pop("hermes_local", None)
    entry.update(model="glm-5.3", ephemeral=True, fresh_session_per_ticket=False)
    t.save()
    home, store = t.role_home(), t.store()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").unlink(missing_ok=True)
    (home / "config.yaml").symlink_to(t.shared / "config.yaml")
    store.mkdir(parents=True, exist_ok=True)
    path = store / team_launcher.session_file_name(t.role().target)
    path.write_text(json.dumps({"target": t.role().target, "session_id": record}), encoding="utf-8")
    return path


def test_the_start_that_closes_the_boundary_never_resumes_what_came_before() -> None:
    """Audit (on b308740b): a restart the hold demands must not resume the conversation held open."""
    import contextlib
    import io
    import json

    import hermes_local_profile_test as fixture

    from scripts import role_pane_entry, role_sessions, team_launcher
    from scripts.resume_history import read_resume_history

    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(list(args))
        failed = "has-session" in args or "new-session" in args  # no pane yet; the start itself is stood in
        return subprocess.CompletedProcess(args, 1 if failed else 0, "", "")

    def starts(t) -> list[str]:
        return [c[-1] for c in calls if "new-session" in c]

    with fixture.tenant(roles=("app",)) as (t, _server):
        path = cutover_tenant(t, "20261101_open_era")
        with contextlib.redirect_stderr(io.StringIO()) as said:
            role_sessions._start_role_session(t.role(), session_dir=t.store(), pane_state_dir=t.root / "panes",
                                              prefer_resume=True, seed_source="syrd693", runner=runner)
        events = [e["event"] for e in read_resume_history(t.role().target, t.store())]
        check("ticket_boundary_cutover" in events and "predates its closed ticket boundary" in said.getvalue(),
              f"the cutover start is decided fresh, and says why: {events} {said.getvalue()[-300:]}")
        check(team_launcher.session_id_for_role(t.role(), t.store()) == ""
              and json.loads(path.with_name(path.name + ".superseded").read_text())["session_id"] == "20261101_open_era",
              "the open-era conversation is set aside, recoverable, not resumed")
        check(starts(t) and "--resume" not in starts(t)[-1] and boundary.TICKET_BOUNDARY_MARK in
              (t.role_home() / "config.yaml").read_text(encoding="utf-8"),
              f"its pane starts fresh, on the closed home: {starts(t)[-1:]}")

        # Closed since: a later start may resume a conversation that began closed.
        path.write_text(json.dumps({"target": t.role().target, "session_id": "20261102_closed_era"}), encoding="utf-8")
        calls.clear()
        with contextlib.redirect_stderr(io.StringIO()):
            role_sessions._start_role_session(t.role(), session_dir=t.store(), pane_state_dir=t.root / "panes",
                                              prefer_resume=True, seed_source="syrd693", runner=runner)
        events = [e["event"] for e in read_resume_history(t.role().target, t.store())]
        check(events.count("ticket_boundary_cutover") == 1, f"only the start that closed it was forced fresh: {events}")

    # A persistent Hermes role is never a cutover: its restart resumes as before.
    with fixture.tenant(roles=("app",)) as (t, _server):
        cutover_tenant(t, "20261101_persistent")
        entry = next(role for role in t.raw["roles"] if role["role"] == "app")
        entry["ephemeral"] = False
        t.save()
        with contextlib.redirect_stderr(io.StringIO()):
            role_sessions._start_role_session(t.role(), session_dir=t.store(), pane_state_dir=t.root / "panes",
                                              prefer_resume=True, seed_source="syrd693", runner=runner)
        events = [e["event"] for e in read_resume_history(t.role().target, t.store())]
        check("ticket_boundary_cutover" not in events, f"a persistent role's restart is not forced fresh: {events}")

    # The same through attach-role, the other start that resumes.
    with fixture.tenant(roles=("app",)) as (t, _server):
        entry = next(role for role in t.raw["roles"] if role["role"] == "app")
        entry["detached"] = True
        entry.pop("slot", None)
        path = cutover_tenant(t, "20261101_open_era")
        calls.clear()
        with contextlib.redirect_stderr(io.StringIO()):
            role_pane_entry.attach_role_to_slot(t.config(), config_path=t.config_path, role_name="app", slot=1,
                                                session_dir=t.store(), pane_state_dir=t.root / "panes", runner=runner,
                                                print_func=lambda line: None)
        events = [e["event"] for e in read_resume_history(t.role().target, t.store())]
        check("ticket_boundary_cutover" in events and starts(t) and "--resume" not in starts(t)[-1],
              f"attach-role's start that closes the boundary is fresh too: {events} {starts(t)[-1:]}")
        check(path.with_name(path.name + ".superseded").exists(), "and sets the open-era conversation aside")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"hermes_ticket_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
