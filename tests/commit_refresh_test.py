#!/usr/bin/env python3
"""SYRD-872: the board's trusted commit cache is refreshed by the account that owns it, never by the board service.

- The service only asks: the exact form `refresh:<token>`, from a connection it closes before waiting, then a bounded
  wait scaled from the listener's last measured fetch. No listener: the caller refuses within that bound.
- The listener's worker accepts only that form, and coalesces: one fetch per interval, every ask that arrived
  before it started served by it, at most PENDING_MAX held.
- SYRD-876: a listed repository the owner cannot lock is skipped: the refresh runs no git in it and changes nothing
  there, and the tenant's own cache is still refreshed.
- SYRD-878: a listed repository another account owns (pgu's read-only `/data/git/switchyard.git`, eric's) is skipped
  by its owner, before its lock: even with a lock file the refresh could take, no git runs in it.
- The generated boundary stays read-only: the board unit's ProtectHome/ProtectSystem/NoNewPrivileges and
  ReadWritePaths, the store grant `rX`, and nothing in the service calls the fetch.

`answer_asks` is the in-process listener other suites use for an app with no database.
"""

from __future__ import annotations

import ast
import json
import os
import pwd
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import commit_cache, commit_refresh  # noqa: E402

TOKEN = "0123456789abcdef0123456789abcdef"
CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def answer_asks(app, *, fetch=None) -> list[str]:
    """Answer the app's asks in process, as the tenant's listener would: the owner's refresh, recorded."""
    asked: list[str] = []

    def notify(_database_url, payload: str) -> None:
        found = commit_refresh.ASK.fullmatch(payload)
        assert found, f"the service asked in a form the listener refuses: {payload!r}"
        asked.append(found.group(1))
        commit_refresh.refresh(tuple(app.commit_git_dirs), [found.group(1)], fetch=fetch)

    app.commit_refresh_notify = notify
    return asked


def git(*args: str, cwd: Path | None = None) -> str:
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


class Repos:
    """A public origin with one commit, a trusted cache cloned from it, and a second commit only origin has."""

    def __init__(self, root: Path):
        self.origin, self.cache, self.work = root / "origin.git", root / "cache.git", root / "work"
        git("init", "-q", "--bare", str(self.origin))
        git("init", "-q", "-b", "main", str(self.work))
        (self.work / "a").write_text("a\n")
        git("add", "a", cwd=self.work)
        git("commit", "-qm", "a", cwd=self.work)
        git("push", "-q", str(self.origin), "main", cwd=self.work)
        git("clone", "-q", "--bare", str(self.origin), str(self.cache))
        git("--git-dir", str(self.cache), "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        (self.work / "b").write_text("b\n")
        git("add", "b", cwd=self.work)
        git("commit", "-qm", "b", cwd=self.work)
        git("push", "-q", str(self.origin), "main", cwd=self.work)
        self.b = git("rev-parse", "HEAD", cwd=self.work)

    def knows(self, commit: str) -> bool:
        return subprocess.run(["git", "--git-dir", str(self.cache), "cat-file", "-e", f"{commit}^{{commit}}"],
                              capture_output=True).returncode == 0


class App:
    def __init__(self, dirs, notify=None):
        self.commit_git_dirs = tuple(dirs)
        self.database_url = "dbname=never-reached"
        if notify is not None:
            self.commit_refresh_notify = notify


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Notice:
    def __init__(self, payload):
        self.payload = payload


class Conn:
    """A LISTEN connection: each notifies() call hands over the next batch, advancing the clock by its timeout."""

    def __init__(self, clock: Clock, batches: list[list], stop: threading.Event):
        self.clock, self.batches, self.stop = clock, list(batches), stop
        self.executed: list[str] = []
        self.timeouts: list[float] = []
        self.stops: list[int | None] = []
        self.empty = 0

    def execute(self, statement: str, *_):
        self.executed.append(statement)

    def notifies(self, *, timeout: float, stop_after: int | None = None):
        self.timeouts.append(timeout)
        self.stops.append(stop_after)
        if not (stop_after and self.batches and self.batches[0]):
            self.clock.now += timeout  # an ask already there wakes a stop_after wait at once
        if not self.batches:
            self.empty += 1
            if self.empty >= 3:  # quiet for three waits running: the test is over
                self.stop.set()
            return iter(())
        self.empty = 0
        batch = self.batches.pop(0)
        if stop_after and len(batch) > stop_after:  # as psycopg does: the rest arrive for the next call
            batch, rest = batch[:stop_after], batch[stop_after:]
            self.batches.insert(0, rest)
        return iter([Notice(payload) for payload in batch])


def test_the_listener_accepts_only_the_services_form() -> None:
    asks = commit_refresh.Coalescer()
    refused = ["refresh:" + TOKEN[:-1], "refresh:" + TOKEN.upper(), "refresh:" + TOKEN + "0", "refresh:" + TOKEN + "\n",
               " refresh:" + TOKEN, "refresh: " + TOKEN, "a" * 40, '{"refresh": "%s"}' % TOKEN, "refresh:" + "g" * 32,
               "fetch:" + TOKEN, "refresh:" + "a" * 40, "", None, 5, b"refresh:" + TOKEN.encode()]
    for payload in refused:
        check(asks.ask(payload) is False, f"the listener refuses an ask that is not the service's form: {payload!r}")
    check(asks.pending == [], f"and holds nothing for them: {asks.pending}")
    check(asks.ask("refresh:" + TOKEN) is True and asks.pending == [TOKEN], "the service's own form is accepted")
    check(asks.ask("refresh:" + TOKEN) is True and asks.pending == [TOKEN], "a repeated token is held once")
    sent: list[str] = []
    with tempfile.TemporaryDirectory(prefix="syrd872-form.") as tmp:
        repos = Repos(Path(tmp))
        clock = Clock()
        commit_refresh.ask(App((repos.cache,), lambda _url, payload: sent.append(payload)), clock=clock, sleep=clock.sleep)
    check(len(sent) == 1 and commit_refresh.ASK.fullmatch(sent[0]) is not None,
          f"what the service sends is exactly that form, once: {sent}")


def test_a_flood_of_asks_is_a_handful_of_fetches() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd872-flood.") as tmp:
        repos = Repos(Path(tmp))
        clock, stop, fetches = Clock(), threading.Event(), []

        snapshots: list[set[str]] = []

        def fetch(dirs):
            fetches.append(clock.now)
            snapshots.append(set(commit_refresh.served(dirs)[0]))
            clock.now += 0.5
            return True

        flood = [f"refresh:{n:032x}" for n in range(commit_refresh.PENDING_MAX + 300)]
        stream = [[f"refresh:{10**6 + 20 * b + n:032x}" for n in range(20)] for b in range(10)]
        conn = Conn(clock, [[], flood, ["junk"] * 50, *stream], stop)
        commit_refresh.serve(conn, (repos.cache,), stop=stop, clock=clock, fetch=fetch, idle=5.0)
        check(conn.executed == [f"LISTEN {commit_refresh.CHANNEL}"], f"the worker LISTENs on its channel: {conn.executed}")
        check(conn.stops[1] == 1 and conn.timeouts[2] == commit_refresh.GATHER_SECONDS and conn.stops[2] is None,
              f"idle, the first ask wakes the worker; it then gathers the burst for {commit_refresh.GATHER_SECONDS}s "
              f"only: {conn.timeouts[:4]} {conn.stops[:4]}")
        check(len(fetches) == 2 + len(stream),
              f"one fetch at start, ONE for {len(flood)} asks gathered together, none for junk, and one per "
              f"window for a steady stream of asks: {len(fetches)} fetches")
        gaps = [b - a for a, b in zip(fetches, fetches[1:])]
        check(all(gap >= commit_refresh.FETCH_INTERVAL_SECONDS for gap in gaps),
              f"never two fetches within {commit_refresh.FETCH_INTERVAL_SECONDS}s, however fast asks arrive: {gaps}")
        answered, measured = commit_refresh.served((repos.cache,))
        after_flood = snapshots[2]
        check(all(f"{n:032x}" in after_flood for n in range(commit_refresh.PENDING_MAX)),
              "every ask held for the flood's fetch is recorded as served by it")
        check(not any(f"{n:032x}" in after_flood for n in range(commit_refresh.PENDING_MAX, len(flood))),
              "asks past PENDING_MAX are dropped, not queued for more fetches")
        check(all(p.split(":")[1] in answered for batch in stream for p in batch), "every streamed ask is served")
        check(measured == 0.5, f"each fetch's duration is recorded for the service's bound: {measured}")


def test_an_ask_arriving_during_a_fetch_is_served_by_the_next_one() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd872-during.") as tmp:
        repos = Repos(Path(tmp))
        clock, stop, served_at_fetch = Clock(), threading.Event(), []
        conn = Conn(clock, [["refresh:" + "1" * 32]], stop)

        def fetch(dirs):
            served_at_fetch.append(set(commit_refresh.served(dirs)[0]))
            if len(served_at_fetch) == 2:
                conn.batches.append(["refresh:" + "2" * 32])  # arrives while this fetch runs
            return True

        commit_refresh.serve(conn, (repos.cache,), stop=stop, clock=clock, fetch=fetch)
        answered = commit_refresh.served((repos.cache,))[0]
        check(len(served_at_fetch) == 3 and "2" * 32 in answered and "2" * 32 not in served_at_fetch[2],
              f"the ask that arrived during a fetch is served by a later fetch, not credited to that one: "
              f"{len(served_at_fetch)} fetches, {sorted(answered)}")


def test_the_service_waits_bounded() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd872-wait.") as tmp:
        repos = Repos(Path(tmp))
        clock = Clock()
        check(commit_refresh.ask(App((repos.cache,), lambda *_: None), clock=clock, sleep=clock.sleep) is False,
              "no listener ever served this cache: the ask goes unanswered")
        check(abs(sum(clock.slept) - commit_refresh.MIN_WAIT_SECONDS) <= commit_refresh.POLL_SECONDS,
              f"and the wait is the minimum, with no measurement to scale: {sum(clock.slept):.2f}s")

        def down(*_):
            raise OSError("connection refused")
        clock = Clock()
        check(commit_refresh.ask(App((repos.cache,), down), clock=clock, sleep=clock.sleep) is False and not clock.slept,
              "an ask that cannot be sent refuses at once")
        commit_refresh.record((repos.cache,), [], True, 4.0)
        clock = Clock()
        check(commit_refresh.ask(App((repos.cache,), lambda *_: None), clock=clock, sleep=clock.sleep) is False,
              "a listener that measured a fetch and then went down leaves the ask unanswered")
        expected = commit_refresh.wait_bound(4.0)
        check(abs(sum(clock.slept) - expected) <= commit_refresh.POLL_SECONDS and expected > commit_refresh.MIN_WAIT_SECONDS,
              f"and the wait is scaled from that measured fetch: {sum(clock.slept):.2f}s for {expected:.2f}s")
        check(commit_refresh.wait_bound(None) == commit_refresh.MIN_WAIT_SECONDS
              and commit_refresh.wait_bound(0.01) == commit_refresh.MIN_WAIT_SECONDS
              and commit_refresh.wait_bound(10**4) == commit_refresh.MAX_WAIT_SECONDS
              and commit_refresh.wait_bound(1.0) == 3 * 1.0 + commit_refresh.GATHER_SECONDS + commit_refresh.FETCH_INTERVAL_SECONDS,
              "the bound: the minimum unmeasured, a fixed most, 3x the measured fetch plus slack between")
        from scripts.ticket_board.write_client import TicketBoardWriteClient
        check(commit_refresh.MAX_WAIT_SECONDS + 1.0 < TicketBoardWriteClient.timeout,
              f"the longest wait ends well inside the write client's request timeout ({TicketBoardWriteClient.timeout}s), "
              "so a role is never told a submission failed that the board then makes")
        failed = Clock()

        def failing(_url, payload):
            commit_refresh.record((repos.cache,), [payload.split(":")[1]], False, 0.1)
        check(commit_refresh.ask(App((repos.cache,), failing), clock=failed, sleep=failed.sleep) is False
              and not failed.slept, "a listener whose fetch failed answers at once, and the caller refuses")


def test_the_owners_record_is_read_with_care() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd872-record.") as tmp:
        repos = Repos(Path(tmp))
        record = repos.cache / commit_refresh.SERVED
        record.write_text('{"fetch_seconds": true, "served": [["%s", "yes"], ["%s"], [5, true], "x", ["%s", true]]}'
                          % ("1" * 32, "2" * 32, "3" * 32))
        check(commit_refresh.served((repos.cache,)) == ({"3" * 32: True}, None),
              f"only well-formed entries count, and a boolean is no duration: {commit_refresh.served((repos.cache,))}")
        for seconds in (-1, 10**5, "2"):
            record.write_text('{"fetch_seconds": %s, "served": []}' % json.dumps(seconds))
            check(commit_refresh.served((repos.cache,))[1] is None, f"a duration of {seconds!r} is not a measurement")
        head = '{"fetch_seconds": 1, "served": [["%s", true]], "pad": "' % ("4" * 32)
        body = head + "x" * (commit_refresh.SERVED_MAX_BYTES + 1 - len(head) - 2) + '"}'
        record.write_text(body + "\n" * 100)  # its first SERVED_MAX_BYTES + 1 bytes are a whole record
        check(len(body) == commit_refresh.SERVED_MAX_BYTES + 1 and commit_refresh.served((repos.cache,)) == ({}, None),
              "a record past SERVED_MAX_BYTES is not read, even when what fits is a whole record")
        record.unlink()
        os.mkfifo(record)
        answer: list = []
        reader = threading.Thread(target=lambda: answer.append(commit_refresh.served((repos.cache,))), daemon=True)
        reader.start()
        reader.join(timeout=5)
        check(not reader.is_alive() and answer == [({}, None)],
              f"a FIFO in the record's place neither hangs the service nor answers it: {answer}")
        record.unlink()
        record.symlink_to(Path(tmp) / "elsewhere.json")
        (Path(tmp) / "elsewhere.json").write_text('{"fetch_seconds": 1, "served": [["%s", true]]}' % ("5" * 32))
        check(commit_refresh.served((repos.cache,)) == ({}, None), "a link in the record's place is not followed")


def test_answered_asks_bring_the_commit_in_as_the_owner() -> None:
    from scripts.ticket_board.app import TicketBoardApp

    with tempfile.TemporaryDirectory(prefix="syrd872-app.") as tmp:
        repos = Repos(Path(tmp))
        app = TicketBoardApp(frame_dir=Path(tmp) / "f", asset_dir=Path(tmp) / "a", commit_git_dir=str(repos.cache),
                             database_url="")
        check(not repos.knows(repos.b), "the cache does not know the pushed commit yet")
        asked = answer_asks(app)
        check(app._validate_commit_hash(repos.b[:12]) == repos.b and len(asked) == 1 and repos.knows(repos.b),
              f"the board asks once and resolves the commit the listener's refresh brought in: {asked}")
        check(app._validate_commit_hash(repos.b) == repos.b and len(asked) == 1, "a known commit asks nothing")
        app.commit_refresh_notify = lambda *_: None
        try:
            app._validate_commit_hash("f" * 40)
        except ValueError as exc:
            check("switchyard-publish-candidate" in str(exc) and "listener did not bring it in" in str(exc),
                  f"an unanswered ask is refused naming the publication path: {exc}")
        else:
            raise AssertionError("an unknown commit was accepted")


def test_a_legacy_update_waits_before_its_transaction() -> None:
    from scripts.ticket_board.app import TicketBoardApp

    class Opened(Exception):
        pass

    with tempfile.TemporaryDirectory(prefix="syrd872-legacy.") as tmp:
        repos = Repos(Path(tmp))
        app = TicketBoardApp(frame_dir=Path(tmp) / "f", asset_dir=Path(tmp) / "a", commit_git_dir=str(repos.cache),
                             database_url="")
        asked: list[str] = []
        app.commit_refresh_notify = lambda _url, payload: asked.append(payload)
        at_open: list[int] = []

        def connect():
            at_open.append(len(asked))
            raise Opened()
        app._pg_connect = connect
        clock = Clock()
        real_sleep, commit_refresh.time.sleep = commit_refresh.time.sleep, clock.sleep
        real_clock, commit_refresh.time.monotonic = commit_refresh.time.monotonic, clock
        try:
            for patch in ({"state": "audit", "commit_hash": "e" * 40}, {"commit_hash": "e" * 40},
                          {"state": "audit", "commit_hash": repos.b}):
                try:
                    app._pg_update_ticket("PGU-1", patch, caller_role="app")
                except Opened:
                    pass
        finally:
            commit_refresh.time.sleep, commit_refresh.time.monotonic = real_sleep, real_clock
        check(at_open == [1, 1, 2], f"a legacy update with a state and an unknown commit asks before its transaction "
                                    f"opens; one with no state asks nothing: asks at each open {at_open}")
        check(getattr(commit_refresh._ASKED, "value", "") == repos.b,
              "an unknown commit still unknown after the ask is remembered for this thread")
        check(commit_refresh.ask(app, repos.b) is False and len(asked) == 2,
              "so the validation inside the transaction refuses it without asking, or waiting, again")
        check(commit_refresh.ask(app, repos.b, clock=clock, sleep=clock.sleep) is False and len(asked) == 3,
              "and only once: the next validation asks as usual")
        answer_asks(app)
        commit_refresh.ask_first(app, {"state": "audit", "commit_hash": repos.b})
        check(repos.knows(repos.b) and getattr(commit_refresh._ASKED, "value", "") == "",
              "a commit the listener brought in leaves nothing remembered: the transaction resolves it locally")


def test_a_refresh_deletes_no_ref_whatever_the_cache_is_configured_to_fetch() -> None:
    """mefp's cache fetches `+refs/*:refs/*`. The refresh names its own refspec and never prunes, so the
    `refs/remotes/origin/<ref>` that publication wrote -- which the board's proofs read -- survives it."""
    from scripts.ticket_board.app import TicketBoardApp

    class Row:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    for refspec in ("+refs/*:refs/*", "+refs/heads/*:refs/remotes/origin/*"):
        with tempfile.TemporaryDirectory(prefix="syrd872-refspec.") as tmp:
            repos = Repos(Path(tmp))
            git("--git-dir", str(repos.cache), "config", "remote.origin.fetch", refspec)
            git("push", "-q", str(repos.origin), f"{repos.b}:refs/heads/roles/main/x", cwd=repos.work)
            # As pane-side publication writes it: the one ref, from the project's own copy, into the tracking ref.
            git("--git-dir", str(repos.cache), "fetch", "-q", str(repos.origin),
                "+refs/heads/roles/main/x:refs/remotes/origin/roles/main/x")
            # Published once, then deleted on origin: what the board proved stays provable.
            git("push", "-q", str(repos.origin), f"{repos.b}:refs/heads/roles/main/merged", cwd=repos.work)
            git("--git-dir", str(repos.cache), "fetch", "-q", str(repos.origin),
                "+refs/heads/roles/main/merged:refs/remotes/origin/roles/main/merged")
            git("push", "-q", str(repos.origin), ":refs/heads/roles/main/merged", cwd=repos.work)
            # As filing a publication request leaves it: a local branch origin never had.
            git("--git-dir", str(repos.cache), "update-ref", "refs/heads/roles/main/asked", repos.b)
            listed = ["git", "--git-dir", str(repos.cache), "for-each-ref", "--format=%(refname) %(objectname)"]
            before = set(subprocess.run(listed, capture_output=True, text=True, check=True).stdout.splitlines())
            check(commit_refresh.refresh((repos.cache,), [TOKEN]) is True, f"{refspec}: the owner's refresh runs")
            after = set(subprocess.run(listed, capture_output=True, text=True, check=True).stdout.splitlines())
            check(before <= after, f"{refspec}: the refresh deletes and rewinds no ref: lost {sorted(before - after)}")
            check(commit_cache.published_ref_commit((repos.cache,), "roles/main/x") == repos.b,
                  f"{refspec}: the board still reads the published ref publication wrote")
            check(f"refs/remotes/origin/main {repos.b}" in after and not any(
                  line.startswith(("refs/heads/roles/main/x ", "refs/tags/")) for line in after - before),
                  f"{refspec}: what it brings in goes to refs/remotes/origin and nowhere else: {sorted(after - before)}")
            app = TicketBoardApp(frame_dir=Path(tmp) / "f", asset_dir=Path(tmp) / "a",
                                 commit_git_dir=str(repos.cache), database_url="")

            class Conn:
                def execute(self, *_):
                    return Row({"ref": "roles/main/x", "commit_hash": repos.b, "state": "requested"})
            check(app._prove_publication(Conn(), 1) == repos.b,
                  f"{refspec}: and the publication proof still holds after the listener's refresh")


def test_the_service_never_fetches_and_runs_no_hook() -> None:
    callers = []
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "refresh_commit_repos":
                callers.append(path.relative_to(ROOT).as_posix())
            if isinstance(node, ast.ImportFrom) and any(alias.name == "refresh_commit_repos" for alias in node.names):
                callers.append(path.relative_to(ROOT).as_posix())
    check(sorted(set(callers)) == ["scripts/ticket_board/commit_refresh.py"],
          f"only the listener's refresh worker calls the fetch: {sorted(set(callers))}")
    with tempfile.TemporaryDirectory(prefix="syrd872-owner-config.") as tmp:
        repos = Repos(Path(tmp))
        # What a fetch runs from the owner's files even under the reader's prefix (core.hooksPath=/dev/null, no
        # credential helper): an alternate the owner names, and the command the owner sets to advertise its refs.
        ran = Path(tmp) / "owner-command-ran"
        git("init", "-q", "--bare", str(Path(tmp) / "alternate.git"))
        (repos.cache / "objects" / "info" / "alternates").write_text(f"{Path(tmp) / 'alternate.git' / 'objects'}\n")
        git("--git-dir", str(repos.cache), "config", "core.alternateRefsCommand", f'sh -c "echo ran >> {ran}; true"')
        clock = Clock()
        original = subprocess.run

        def no_git(argv, *args, **kwargs):
            raise AssertionError(f"the service's ask ran {argv}")
        subprocess.run = no_git
        try:
            commit_refresh.ask(App((repos.cache,), lambda *_: None), clock=clock, sleep=clock.sleep)
        finally:
            subprocess.run = original
        check(not ran.exists() and not repos.knows(repos.b), "the service's ask runs no git: no owner command, no fetch")
        commit_refresh.refresh((repos.cache,), [TOKEN])
        check(ran.exists() and repos.knows(repos.b),
              "the fetch is the owner's, and it does run the owner's configured command: why it must never be the "
              "shared board account's")


def test_the_generated_boundary_stays_read_only() -> None:
    from scripts.ticket_board.project_provision import build_plan
    from scripts.ticket_board.provision_board_service import render_board_unit
    from scripts.ticket_board.provision_path_confinement import commit_store_read_commands

    owner = pwd.getpwuid(os.geteuid()).pw_name
    plan = build_plan(project="porter", project_name="Porter", owner_user=owner,
                      owner_home=Path(pwd.getpwnam(owner).pw_dir), source_repo=ROOT)
    for label, unit in (("generated", render_board_unit(plan)),):
        lines = unit.splitlines()
        for kept in ("NoNewPrivileges=true", "ProtectSystem=full", "ProtectHome=read-only"):
            check(kept in lines, f"the {label} board unit keeps {kept}")
        writable = [line for line in lines if line.startswith("ReadWritePaths=")]
        check(len(writable) == 1 and len(writable[0].split("=", 1)[1].split()) == 2
              and "commit" not in writable[0] and ".git" not in writable[0],
              f"the {label} board unit writes only its assets and frames, never the commit cache: {writable}")
    home = "/home/porter"
    grants = commit_store_read_commands(owner_home=home, service_user="boardsvc", commit_git_dir=f"{home}/cache/x.git")
    check(any("u:boardsvc:rX" in line for line in grants) and any("d:u:boardsvc:rX" in line for line in grants)
          and not any("w" in part.split(":")[-1] for line in grants for part in line.split() if "boardsvc:" in part),
          f"the store grant stays read-only, with traverse-only ancestors: {grants}")


def test_a_listed_repository_the_owner_cannot_lock_is_never_fetched() -> None:
    """SYRD-876: a listed repository whose integration lock the listener's account can neither create nor open is
    skipped: no git command of the refresh names it, nothing in it changes, and the tenant's own cache beside it is
    still refreshed. Since SYRD-878 another account's repository is skipped earlier, by its owner (the next test);
    this holds the lock's own refusal, for a repository this account owns. Two ways the lock is out of reach: a
    directory the account may not write (root writes anywhere, so it is checked only when this runs as another
    account), and a lock path that is a symlink, which the lock never follows."""

    def ways(foreign: Path):
        if os.geteuid() != 0:
            yield "read-only", lambda: foreign.chmod(0o555), lambda: foreign.chmod(0o755)
        lock = foreign / commit_cache.INTEGRATION_LOCK
        yield "symlinked lock", lambda: lock.symlink_to(foreign / "nowhere"), lambda: lock.unlink()

    names = (["read-only"] if os.geteuid() != 0 else []) + ["symlinked lock"]
    for order in ("cache first", "foreign first"):
        for name in names:
            with tempfile.TemporaryDirectory(prefix="syrd876-foreign.") as tmp:
                repos = Repos(Path(tmp))
                foreign = Path(tmp) / "foreign.git"
                git("clone", "-q", "--bare", str(repos.origin), str(foreign))
                git("--git-dir", str(foreign), "update-ref", "refs/heads/main", f"{repos.b}~1")  # as when last pushed
                git("--git-dir", str(foreign), "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
                close, reopen = next((c, o) for way, c, o in ways(foreign) if way == name)
                listed = ["git", "--git-dir", str(foreign), "for-each-ref", "--format=%(refname) %(objectname)"]
                before = subprocess.run(listed, capture_output=True, text=True, check=True).stdout
                entries = sorted(p.name for p in foreign.iterdir())
                dirs = (repos.cache, foreign) if order == "cache first" else (foreign, repos.cache)
                calls: list[list[str]] = []
                real_run = commit_cache.subprocess.run

                def recording(cmd, *args, **kwargs):
                    calls.append([str(part) for part in cmd])
                    return real_run(cmd, *args, **kwargs)

                close()
                commit_cache.subprocess.run = recording
                try:
                    refreshed = commit_cache.refresh_commit_repos(dirs)
                finally:
                    commit_cache.subprocess.run = real_run
                    reopen()
                touching = [cmd for cmd in calls if any(str(foreign) in part for part in cmd)]
                check(touching == [], f"{name}, {order}: the refresh runs no git in the repository it cannot lock: "
                                      f"{touching}")
                after = subprocess.run(listed, capture_output=True, text=True, check=True).stdout
                check(after == before and sorted(p.name for p in foreign.iterdir()) == entries,
                      f"{name}, {order}: nothing in it changes, not a ref, not a file")
                check(refreshed is True and repos.knows(repos.b),
                      f"{name}, {order}: the tenant's own cache beside it is still refreshed")


def test_a_repository_another_account_owns_is_never_touched() -> None:
    """SYRD-878: the refresh runs only in a repository the running account owns. pgu lists `/data/git/switchyard.git`,
    eric's, after its own cache, and a lock file there that the tenant could open would otherwise let the refresh take
    the lock and run `symbolic-ref` and `fetch` there, as the tenant, under eric's config. The foreign repository is
    real, with a lock file anyone may open, and the `os.stat` seam reports another owner for it alone -- the same
    whether or not this runs as root."""
    for order in ("cache first", "foreign first"):
        with tempfile.TemporaryDirectory(prefix="syrd878-owner.") as tmp:
            repos = Repos(Path(tmp))
            foreign = Path(tmp) / "foreign.git"
            git("clone", "-q", "--bare", str(repos.origin), str(foreign))
            git("--git-dir", str(foreign), "update-ref", "refs/heads/main", f"{repos.b}~1")  # as when last pushed
            git("--git-dir", str(foreign), "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
            lock = foreign / commit_cache.INTEGRATION_LOCK
            lock.write_text("")
            lock.chmod(0o666)  # readable and writable by anyone: the lock alone would not stop the refresh
            listed = ["git", "--git-dir", str(foreign), "for-each-ref", "--format=%(refname) %(objectname)"]
            before = subprocess.run(listed, capture_output=True, text=True, check=True).stdout
            entries = sorted(p.name for p in foreign.iterdir())
            dirs = (repos.cache, foreign) if order == "cache first" else (foreign, repos.cache)
            calls: list[list[str]] = []
            real_run, real_stat = commit_cache.subprocess.run, commit_cache.os.stat

            def recording(cmd, *args, **kwargs):
                calls.append([str(part) for part in cmd])
                return real_run(cmd, *args, **kwargs)

            def owned_elsewhere(path, *args, **kwargs):
                found = real_stat(path, *args, **kwargs)
                if Path(path) == foreign:
                    values = list(found)
                    values[4] = os.geteuid() + 1  # st_uid: another account
                    return os.stat_result(values)
                return found

            commit_cache.subprocess.run, commit_cache.os.stat = recording, owned_elsewhere
            try:
                refreshed = commit_cache.refresh_commit_repos(dirs)
            finally:
                commit_cache.subprocess.run, commit_cache.os.stat = real_run, real_stat
            touching = [cmd for cmd in calls if any(str(foreign) in part for part in cmd)]
            check(touching == [], f"{order}: no git runs in another account's repository, though its lock file was "
                                  f"there to take: {touching}")
            after = subprocess.run(listed, capture_output=True, text=True, check=True).stdout
            check(after == before and sorted(p.name for p in foreign.iterdir()) == entries,
                  f"{order}: nothing in it changes, not a ref, not a file")
            check(refreshed is True and repos.knows(repos.b),
                  f"{order}: the tenant's own cache beside it is still refreshed")


def test_the_listener_starts_its_refresh_worker() -> None:
    text = (ROOT / "scripts" / "ticket_board" / "notify_listener.py").read_text(encoding="utf-8")
    main = text.split("\ndef main(", 1)[1]
    check("commit_refresh.start(listener)" in main.split("listener.run_forever()", 1)[0],
          "the listener's main starts the refresh worker before it runs")


def main() -> int:
    for name, value in list(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"commit_refresh_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
