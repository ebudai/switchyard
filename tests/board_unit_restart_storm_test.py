#!/usr/bin/env python3
"""SYRD-552: a database outage is not a restart storm, and not a log flood.

Every board unit had `Wants=postgresql.service` with `Restart=on-failure` and
`RestartSec=2`. Each start of a board -- each automatic restart included --
queued another PostgreSQL start, and seven boards did that every two seconds.
A board that cannot read its database also logged a two-line warning for every
one-second reconciliation scan, into the journal, for the whole outage.

Pinned here, per the Director's decision on the ticket:

- **The units:** the generated board unit, pgu's static unit and the deploy
  script's own render are all ordered after PostgreSQL and pull in nothing
  (no `Wants=`, `Requires=`, `BindsTo=` or `Requisite=` naming it). They
  restart no faster than every 30 seconds, and five starts in five minutes
  leave them failed. The arithmetic is checked: five starts at that floor fit
  inside the interval, so the limit is really reached and the unit ends in a
  failed state instead of flapping.
- **The report:** a failing scan is reported once, on one line, then at most
  once a minute while scans keep failing, and a reported failure's recovery
  once -- driven through the real watch loop for ten simulated minutes of
  outage and two of flapping.
- **The real board** started against an unreachable database stays up and
  logs no scan warning in its first seconds (main logged one a second).
- **An upgrade re-renders** an existing tenant's board unit: the real
  `refresh_generated_project_runtime_artifacts` replaces an old unit with the
  new settings, root's staged bytes carry them, and nothing is restarted.

Nothing reaches the host: the real board runs with its own socket, frames and
assets in a temporary directory and a database socket directory that has no
server; the upgrade runs the unprivileged branch with a recording runner.
"""

from __future__ import annotations

import logging
import os
import pwd
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board import board_notifications as notifications  # noqa: E402
from scripts.ticket_board.project_provision import build_plan, write_artifacts  # noqa: E402
from scripts.ticket_board.provision_board_service import render_board_unit  # noqa: E402

CHECKS = 0
FAILURES: list[str] = []
FLOOR_SECONDS = 30
INTERVAL_SECONDS = 300
BURST = 5
PULLS = ("Wants", "Requires", "BindsTo", "Requisite", "Upholds", "PartOf")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(detail)
        print(f"FAIL: {detail}")


def sections(text: str) -> dict[str, list[tuple[str, str]]]:
    """A unit file's directives by section, in order, repeats kept."""
    found: dict[str, list[tuple[str, str]]] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            found.setdefault(current, [])
            continue
        key, _, value = line.partition("=")
        found.setdefault(current, []).append((key.strip(), value.strip()))
    return found


def values(unit: dict[str, list[tuple[str, str]]], section: str, key: str) -> list[str]:
    return [v for k, v in unit.get(section, []) if k == key]


def seconds(value: str) -> float:
    """A systemd time span, in the forms these units use."""
    total, rest = 0.0, value.replace(" ", "")
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)(min|ms|s|h)?", rest):
        total += float(number) * {"": 1, "s": 1, "ms": 0.001, "min": 60, "h": 3600}[unit]
    return total


def fixture_plan(root: Path):
    owner = pwd.getpwuid(os.geteuid()).pw_name
    return build_plan(project="porter", project_name="Porter", owner_user=owner,
                      owner_home=Path(pwd.getpwnam(owner).pw_dir), source_repo=ROOT)


def shell_board_unit() -> str:
    """The deploy script's own board unit render, as written in it."""
    text = (ROOT / "scripts" / "ticket-board-service.sh").read_text(encoding="utf-8")
    body = text.split("render_unit() {\n    cat <<EOF\n", 1)[1].split("\nEOF\n", 1)[0]
    return body + "\n"


def assert_unit_settles(label: str, text: str) -> None:
    unit = sections(text)
    pulled = [f"{k}={v}" for section in unit.values() for k, v in section if k in PULLS and "postgresql" in v]
    check(pulled == [], f"{label}: nothing pulls PostgreSQL in, so no start or restart of the board queues a start of it: {pulled}")
    if label != "deploy script render":
        check(any("postgresql.service" in v.split() for v in values(unit, "Unit", "After")),
              f"{label}: still ordered after PostgreSQL when both start together: {values(unit, 'Unit', 'After')}")
    check(values(unit, "Service", "Restart") == ["on-failure"] and [seconds(v) for v in values(unit, "Service", "RestartSec")] == [FLOOR_SECONDS],
          f"{label}: restarts on failure, no faster than every {FLOOR_SECONDS}s: "
          f"{values(unit, 'Service', 'Restart')} {values(unit, 'Service', 'RestartSec')}")
    interval = [seconds(v) for v in values(unit, "Unit", "StartLimitIntervalSec")]
    burst = values(unit, "Unit", "StartLimitBurst")
    check(interval == [INTERVAL_SECONDS] and burst == [str(BURST)],
          f"{label}: an explicit start limit in [Unit], {BURST} starts in {INTERVAL_SECONDS}s: {interval} {burst}")
    stray = [k for k, _ in unit.get("Service", []) if k.startswith("StartLimit")]
    check(stray == [], f"{label}: no start limit in [Service], where it would not mean what it says: {stray}")
    # The limit must be reachable at that floor, or the unit retries forever at
    # the floor instead of settling in a failed state.
    if interval and burst:
        check((int(burst[0]) - 1) * FLOOR_SECONDS < interval[0],
              f"{label}: {burst[0]} starts at a {FLOOR_SECONDS}s floor fit inside {interval[0]}s, so the limit ends in a failed state")


def test_the_units() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd552-") as tmp:
        assert_unit_settles("generated board unit", render_board_unit(fixture_plan(Path(tmp))))
    assert_unit_settles("pgu static unit", (ROOT / "deploy" / "systemd" / "pgu-ticket-board.service.boardsvc").read_text(encoding="utf-8"))
    assert_unit_settles("deploy script render", shell_board_unit())


# --- the report -------------------------------------------------------------------------------------------------------


class Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class OutageApp:
    """A store whose scans fail, with a connection error's own line breaks, or answer."""

    def __init__(self, initial_ok: bool = True) -> None:
        self.initial_ok = initial_ok
        self.script: list[bool] = []

    def store_signature(self):
        ok = self.script.pop(0) if self.script else self.initial_ok
        if not ok:
            raise RuntimeError('connection is bad: connection to server on socket "/run/postgresql/.s.PGSQL.5432" '
                               "failed: No such file or directory\n\tIs the server running locally?")
        return (("a", 1),)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Stop:
    """The watch loop's stop event: each wait advances the clock, and it ends after `count` waits."""

    def __init__(self, clock: Clock, count: int) -> None:
        self.clock, self.count = clock, count

    def wait(self, timeout: float) -> bool:
        if self.count == 0:
            return True
        self.count -= 1
        self.clock.now += timeout
        return False

    def set(self) -> None:
        self.count = 0


def hub_over(app: OutageApp, clock: Clock, records: Records) -> notifications.TicketBoardEventHub:
    logging.getLogger(notifications.LOGGER.name).addHandler(records)
    hub = notifications.TicketBoardEventHub(app, scan_interval_seconds=1.0)
    hub.close()
    # Onto the simulated clock; a failed first read was reported "now".
    hub._clock = clock
    if getattr(hub, "_last_failure_report", None) is not None:
        hub._last_failure_report = clock()
    return hub


def per_minute(stamps: list[float]) -> int:
    return max((sum(1 for t in stamps if start <= t < start + 60) for start in stamps), default=0)


def test_an_outage_is_reported_once_a_minute() -> None:
    logger = logging.getLogger(notifications.LOGGER.name)
    for initial_ok in (True, False):
        label = "board up first" if initial_ok else "database down from the start"
        clock, records = Clock(), Records()
        app = OutageApp(initial_ok=initial_ok)
        try:
            hub = hub_over(app, clock, records)
            stamps: list[float] = []
            seen = len(records.lines)
            app.script = [False] * 600
            hub._stop_event = Stop(clock, 600)
            # Record when each line was written, scan by scan.
            original = hub._record_scan_failure

            def failing(exc, _original=original):
                nonlocal seen
                _original(exc)
                stamps.extend([clock.now] * (len(records.lines) - seen))
                seen = len(records.lines)
            hub._record_scan_failure = failing
            hub._watch_loop()
            outage = records.lines[:]
            app.script = [True]
            hub._stop_event = Stop(clock, 1)
            hub._watch_loop()
            after = records.lines[len(outage):]
        finally:
            logger.removeHandler(records)
        scan_lines = [line for line in outage if line.startswith("board reconciliation scan")]
        check(all("\n" not in line for line in records.lines), f"{label}: every report is one line: {records.lines[:3]}")
        expected = 10
        check(len(scan_lines) == expected and per_minute(stamps) <= 1,
              f"{label}: ten minutes of failed scans, one a second, report {expected} times, never twice in a minute: "
              f"{len(scan_lines)} {per_minute(stamps)} {scan_lines[:2]}")
        if initial_ok:
            check(scan_lines[0].startswith("board reconciliation scan failed: connection is bad")
                  and scan_lines[0].endswith("retrying every 1s, reported at most once every 60s while it fails"),
                  f"{label}: the first report says what failed and how it is retried: {scan_lines[:1]}")
        else:
            check(outage[0] == "board reconciliation could not read its initial state",
                  f"{label}: the failed first read is the first report: {outage[:1]}")
        check(all(re.fullmatch(r"board reconciliation scan still failing after \d+ attempts: connection is bad: .*", line) for line in scan_lines[0 if not initial_ok else 1:]),
              f"{label}: the later reports count the attempts: {scan_lines[1:3]}")
        attempts = 600 + (0 if initial_ok else 1)
        check(after == [f"board reconciliation scan recovered after {attempts} failed attempts"],
              f"{label}: its recovery is reported once: {after}")
        check(hub.degraded_reason == "" and hub._consecutive_failures == 0, f"{label}: and the board is healthy again")


def test_flapping_is_one_report_a_minute_too() -> None:
    logger = logging.getLogger(notifications.LOGGER.name)
    clock, records = Clock(), Records()
    app = OutageApp()
    try:
        hub = hub_over(app, clock, records)
        app.script = [False, True] * 60
        hub._stop_event = Stop(clock, 120)
        hub._watch_loop()
    finally:
        logger.removeHandler(records)
    failed = [line for line in records.lines if "scan failed" in line or "still failing" in line]
    recovered = [line for line in records.lines if "recovered" in line]
    check(len(failed) == 2 and len(recovered) == 2,
          f"two minutes of a scan failing every other second: two failure reports and two recoveries, not sixty of each: {len(failed)} {len(recovered)}")


# --- the real board ---------------------------------------------------------------------------------------------------


def test_the_real_board_stays_up_and_quiet() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd552-board-") as tmp:
        root = Path(tmp)
        nopg = root / "no-postgres"
        nopg.mkdir()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1",
               "TICKET_BOARD_PROJECT": "porter", "TICKET_BOARD_SOCKET": str(root / "board.sock")}
        log = root / "board.log"
        with log.open("w") as out:
            child = subprocess.Popen(
                [sys.executable, "-B", str(ROOT / "scripts" / "ticket-board.py"), "--host", "127.0.0.1", "--port", "0",
                 "--unix-socket", str(root / "board.sock"), "--frames", str(root / "frames"), "--assets", str(root / "assets"),
                 "--database", f"postgresql:///porter?host={nopg}&user=porter"],
                cwd=root, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline and child.poll() is None:
                    time.sleep(0.2)
                alive = child.poll() is None
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
        text = log.read_text(errors="replace")
        check("listening on http://127.0.0.1:" in text and "could not read its initial state" in text,
              f"precondition: the board came up and could not reach the database: {text[-600:]}")
        check(alive, f"a board whose database is unreachable keeps serving rather than exiting into a restart: rc={child.returncode} {text[-400:]}")
        scans = [line for line in text.splitlines() if line.startswith("board reconciliation scan")]
        check(scans == [], f"and its first eight seconds of failed scans add no line to the log (main: one a second): {scans[:3]}")


# --- the upgrade ------------------------------------------------------------------------------------------------------


OLD = {"StartLimitIntervalSec=5min\nStartLimitBurst=5\n": "Wants=postgresql.service\n", "RestartSec=30s\n": "RestartSec=2\n"}


def test_an_upgrade_rerenders_an_existing_board_unit() -> None:
    saved = os.environ.get("SWITCHYARD_PRIVILEGED_PROVISION_ROOT")
    with tempfile.TemporaryDirectory(prefix="syrd552-upgrade-") as tmp:
        root = Path(tmp)
        provision, repository = root / "provision", root / "repository"
        provision.mkdir()
        repository.mkdir()
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(root / "etc-switchyard")
        try:
            plan = fixture_plan(root)
            write_artifacts(plan, provision, enable_owner_linger=False)
            config_path = launcher.write_new_project_launcher_artifacts(plan, provision, repository=repository, print_func=lambda _t: None)
            unit_path = provision / plan.board_unit
            old = unit_path.read_text(encoding="utf-8")
            for new, before in OLD.items():
                check(old.count(new) == 1, f"precondition: the freshly rendered unit carries {new.strip()!r}")
                old = old.replace(new, before)
            unit_path.write_text(old, encoding="utf-8")
            config = launcher.load_project_config("porter", config_path)
            calls: list[list[str]] = []

            def runner(args, **kwargs):
                calls.append([str(a) for a in args])
                return subprocess.CompletedProcess(args, 0, "", "")

            outcome = launcher.refresh_generated_project_runtime_artifacts(config, config_path=config_path, runner=runner, print_func=lambda _t: None)
            refreshed = unit_path.read_text(encoding="utf-8")
            check(outcome.changed and plan.board_unit in outcome.message,
                  f"the upgrade sees the old board unit and refreshes it: {outcome}")
            check(refreshed == render_board_unit(plan) and "Wants=postgresql.service" not in refreshed and "RestartSec=30s" in refreshed,
                  "the existing tenant's board unit is re-rendered with the new settings")
            assert_unit_settles("re-rendered tenant unit", refreshed)
            staged = launcher.render_privileged_artifacts(plan)
            check(staged.get(plan.board_unit, b"").decode() == render_board_unit(plan),
                  "what root stages and installs for that tenant is the same render")
            touched = [c for c in calls if any(word in c for word in ("systemctl", "restart", "start", "daemon-reload"))]
            check(touched == [], f"and nothing was started, restarted or reloaded: {touched}")
            again = launcher.refresh_generated_project_runtime_artifacts(config, config_path=config_path, runner=runner, print_func=lambda _t: None)
            check(not again.changed and "already current" in again.message, f"a second upgrade has nothing to do: {again}")
        finally:
            if saved is None:
                os.environ.pop("SWITCHYARD_PRIVILEGED_PROVISION_ROOT", None)
            else:
                os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = saved


def main() -> int:
    for name in sorted(n for n, v in globals().items() if n.startswith("test_") and callable(v)):
        globals()[name]()
    if FAILURES:
        print(f"board_unit_restart_storm_test: {len(FAILURES)} of {CHECKS} checks FAILED")
        return 1
    print(f"board_unit_restart_storm_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
