"""Who refreshes the board's trusted commit cache: the account that owns it, never the board service (SYRD-872).

The cache belongs to the tenant account. Its config, hooks and refs are the owner's to write, and the board service
runs as one account, `boardsvc`, shared by every tenant's board. A `git fetch` run as `boardsvc` into that cache runs
the owner's `reference-transaction` hook and follows the owner's `remote.origin.url`, so a service that could write the
cache would hand any tenant code execution as the account that serves every board. The service's boundary therefore
stays read-only by design (ProtectHome=read-only, `boardsvc:rX` on the store alone), and the refresh is the owner's:

- **The service asks.** When it meets a commit it does not know, it sends `refresh:<token>` (a fresh random token, and
  nothing a caller chose) on CHANNEL, from a connection of its own that is closed again before it waits, so no
  transaction or lock is held while it does. It then re-reads the cache for a bounded time, scaled from the listener's
  last measured fetch (`wait_bound`), for the token among the ones the listener says it served.
- **The listener answers, as the owner.** Its worker thread LISTENs on CHANNEL with a connection of its own, so the
  listener's delivery loop never delays it. An ask must be exactly the service's form, or it is dropped. Asks are
  coalesced: one fetch at a time, at most one per FETCH_INTERVAL_SECONDS, and every ask that arrived before a fetch
  started is served by that fetch, so a flood of unknown commits is a handful of fetches, not a flood. The fetch is
  `commit_cache.refresh_commit_repos` unchanged: the integration lock, the Publisher-mark refspecs, the pinned
  safe.directory, the transport limits. It then records the tokens it served, and how long the fetch took, in SERVED
  in each cache's git directory.
- **No listener, no wait past the bound.** A listener that never served this cache has left no measurement, and the
  service waits only MIN_WAIT_SECONDS. A listener down or crash-looping leaves the last measurement, and the wait is
  bounded by it. Either way the caller refuses the commit plainly, naming the publication path that brings it in.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from . import commit_cache
from .commit_cache_reader import cache_reader

#: The channel the service asks on and the listener's refresh worker LISTENs to.
CHANNEL = "ticket_board_commit_refresh"
#: The only ask the listener accepts: the service's own form, a random token and nothing else.
ASK = re.compile(r"refresh:([0-9a-f]{32})")
#: The listener's record, in each cache's git directory, of the asks it served and how long its last fetch took.
SERVED = "switchyard-refresh-served.json"
#: How many served tokens the record keeps, newest last. A waiting service needs only its own, seconds old.
SERVED_KEPT = 256
#: The record is the owner's file; the service reads no more of it than this.
SERVED_MAX_BYTES = 64 * 1024
#: Asks held for the next fetch; past this, an ask is dropped, and that service refuses within its bound.
PENDING_MAX = 256
#: The fewest seconds between two fetches of the caches, however many asks arrive.
FETCH_INTERVAL_SECONDS = 2.0
#: How long the worker gathers asks once the first arrives, so a burst becomes one fetch.
GATHER_SECONDS = 0.25
#: The service's wait: WAIT_FACTOR times the listener's last measured fetch, plus the gather and the interval;
#: never shorter than MIN_WAIT_SECONDS, and never as long as the write client's own request timeout (10 s,
#: `TicketBoardWriteClient.timeout`), so a role is never told its submission failed while the board goes on to
#: make it. A fetch slower than that still finishes, and the next submission finds the commit.
MIN_WAIT_SECONDS = 4.0
WAIT_FACTOR = 3.0
MAX_WAIT_SECONDS = 8.0
POLL_SECONDS = 0.1


# -- the service's side -------------------------------------------------------------------------------------------

def notify(database_url: str | None, payload: str) -> None:
    """Send one ask from a connection of the service's own, committed and closed before the caller waits."""
    import psycopg

    with psycopg.connect(database_url or "", autocommit=True, connect_timeout=5) as conn:
        conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, payload))


def served(commit_git_dirs: Iterable[Path]) -> tuple[dict[str, bool], float | None]:
    """The tokens the listener says it served, with whether that fetch succeeded, and its last measured fetch."""
    tokens: dict[str, bool] = {}
    measured: float | None = None
    for commit_git_dir in commit_git_dirs:
        record = _read_record(commit_git_dir)
        seconds = record.get("fetch_seconds")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and 0 <= seconds <= 3600:
            measured = max(measured or 0.0, float(seconds))
        for entry in record.get("served") or []:
            if (isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str)
                    and ASK.fullmatch(f"refresh:{entry[0]}") and isinstance(entry[1], bool)):
                tokens[entry[0]] = tokens.get(entry[0], False) or entry[1]
    return tokens, measured


def wait_bound(measured: float | None) -> float:
    """How long the service waits for its ask: scaled from the listener's last measured fetch, within the limits."""
    if measured is None:
        return MIN_WAIT_SECONDS
    return min(MAX_WAIT_SECONDS, max(MIN_WAIT_SECONDS, WAIT_FACTOR * measured + GATHER_SECONDS + FETCH_INTERVAL_SECONDS))


#: The commit this thread already asked about before opening a transaction (`ask_first`), so the validation inside
#: that transaction does not wait a second time.
_ASKED = threading.local()


def ask_first(app: Any, patch: dict[str, Any]) -> None:
    """Before a transaction that will validate `patch`'s commit, ask about a commit the cache lacks, so the wait
    happens while nothing is held. Never refuses: the transaction's own checks still refuse in their own order."""
    value = patch.get("commit_hash")
    _ASKED.value = ""
    if not (isinstance(value, str) and "state" in patch and re.fullmatch(r"[0-9A-Fa-f]{7,40}", value.strip())):
        return
    value, dirs = value.strip(), tuple(app.commit_git_dirs)
    if not commit_cache.resolve_known_commit(dirs, value)[0]:
        ask(app, value)
        if not commit_cache.resolve_known_commit(dirs, value)[0]:
            _ASKED.value = value


def ask(app: Any, value: str = "", *, clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None) -> bool:
    """Ask the tenant's listener to refresh the cache, and wait, bounded, for its answer.

    True when a fetch the listener ran after this ask succeeded: the caller then reads the cache again. False when
    the ask could not be sent, the listener's fetch failed, or no answer came within the bound. Nothing is held
    while waiting: the ask's connection is closed before the first read of the record. False at once for `value`
    when this thread asked about it already, before its transaction (`ask_first`).
    """
    if value and getattr(_ASKED, "value", "") == value:
        _ASKED.value = ""
        return False
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    dirs = tuple(Path(path) for path in (getattr(app, "commit_git_dirs", None) or ()))
    if not dirs:
        return False
    token = secrets.token_hex(16)
    deadline = clock() + wait_bound(served(dirs)[1])
    try:
        (getattr(app, "commit_refresh_notify", None) or notify)(getattr(app, "database_url", None), f"refresh:{token}")
    except Exception:  # noqa: BLE001 - an ask that cannot be sent is an unknown commit, refused in the caller's words
        return False
    while True:
        answers = served(dirs)[0]
        if token in answers:
            return answers[token]
        if clock() >= deadline:
            return False
        sleep(POLL_SECONDS)


# -- the listener's side, as the owner ----------------------------------------------------------------------------

class Coalescer:
    """The asks waiting for the next fetch: only the service's form, at most PENDING_MAX, gathered for
    GATHER_SECONDS after the first, and one fetch per FETCH_INTERVAL_SECONDS however many arrive."""

    def __init__(self, interval: float = FETCH_INTERVAL_SECONDS):
        self.interval = interval
        self.pending: list[str] = []
        self.first_at = 0.0
        self.last_start: float | None = None

    def ask(self, payload: Any, now: float = 0.0) -> bool:
        found = ASK.fullmatch(payload) if isinstance(payload, str) else None
        if found is None or len(self.pending) >= PENDING_MAX:
            return False
        if not self.pending:
            self.first_at = now
        if found.group(1) not in self.pending:
            self.pending.append(found.group(1))
        return True

    def ready_at(self) -> float:
        ready = self.first_at + GATHER_SECONDS
        return ready if self.last_start is None else max(ready, self.last_start + self.interval)

    def wait(self, now: float, idle: float) -> float:
        """How long to listen before the next fetch could start: `idle` with nothing pending."""
        return max(0.0, self.ready_at() - now) if self.pending else idle

    def due(self, now: float) -> bool:
        return bool(self.pending) and now + 1e-6 >= self.ready_at()

    def take(self, now: float) -> list[str]:
        tokens, self.pending, self.last_start = self.pending, [], now
        return tokens


def record(commit_git_dirs: Iterable[Path], tokens: list[str], ok: bool, seconds: float) -> None:
    """Add the tokens a fetch served, and its measured duration, to each cache's record. Written whole and renamed
    into place, readable by the service through the store's default ACL; a cache that cannot be written is left."""
    for commit_git_dir in commit_git_dirs:
        try:
            git_dir = cache_reader(commit_git_dir).git_dir
        except (ValueError, PermissionError):
            continue
        kept = [entry for entry in _read_record(commit_git_dir).get("served") or []
                if isinstance(entry, list) and len(entry) == 2 and entry[0] not in tokens]
        body = json.dumps({"fetch_seconds": round(seconds, 3),
                           "served": (kept + [[token, ok] for token in tokens])[-SERVED_KEPT:]}).encode()
        temporary = Path(git_dir) / f".{SERVED}.{os.getpid()}.{threading.get_ident()}"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            os.replace(temporary, Path(git_dir) / SERVED)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def refresh(commit_git_dirs: tuple[Path, ...], tokens: list[str], *, clock: Callable[[], float] = time.monotonic,
            fetch: Callable[[tuple[Path, ...]], bool] | None = None) -> bool:
    """One owner-side fetch of every cache, timed, and recorded as serving `tokens`."""
    started = clock()
    ok = (fetch or commit_cache.refresh_commit_repos)(commit_git_dirs)
    record(commit_git_dirs, tokens, ok, clock() - started)
    return ok


def serve(conn: Any, commit_git_dirs: tuple[Path, ...], *, stop: threading.Event,
          clock: Callable[[], float] = time.monotonic, fetch: Callable[[tuple[Path, ...]], bool] | None = None,
          idle: float = 5.0) -> None:
    """LISTEN on CHANNEL and serve asks until `stop`. One fetch first, so the service has a measurement to scale
    its wait by and the cache starts current."""
    conn.execute(f"LISTEN {CHANNEL}")
    refresh(commit_git_dirs, [], clock=clock, fetch=fetch)
    asks = Coalescer()
    asks.last_start = clock()
    while not stop.is_set():
        # Idle, the first ask wakes the worker at once; then it gathers what follows for the rest of the window.
        for notice in conn.notifies(timeout=asks.wait(clock(), idle), stop_after=None if asks.pending else 1):
            asks.ask(getattr(notice, "payload", None), clock())
        if asks.due(clock()):
            refresh(commit_git_dirs, asks.take(clock()), clock=clock, fetch=fetch)


def run(listener: Any) -> None:
    """The listener's refresh worker: reconnects as the listener does, and gives up only when the listener stops."""
    from .publisher import Handoff, board_commit_repositories

    while not listener.stop_event.is_set():
        try:
            dirs = tuple(Path(path) for path in board_commit_repositories())
            with listener.connector(listener.conninfo, **listener._connector_kwargs()) as conn:
                listener.logger.info("commit refresh: LISTEN %s for %s", CHANNEL, ", ".join(map(str, dirs)))
                serve(conn, dirs, stop=listener.stop_event)
        except Handoff as exc:
            listener.logger.warning("commit refresh: %s", exc)
        except Exception as exc:  # noqa: BLE001 - a lost connection or a failed read; reconnect like the listener
            listener.logger.warning("commit refresh disconnected: %s", exc)
        listener.stop_event.wait(listener.reconnect_seconds)


def start(listener: Any) -> threading.Thread:
    worker = threading.Thread(target=run, args=(listener,), name="commit-refresh", daemon=True)
    worker.start()
    return worker


def _read_record(commit_git_dir: Path) -> dict[str, Any]:
    try:
        path = Path(cache_reader(commit_git_dir).git_dir) / SERVED
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except (OSError, ValueError):
        return {}
    try:  # non-blocking: the owner's file, and a FIFO in its place must not hang the service
        with os.fdopen(fd, "rb") as handle:
            body = handle.read(SERVED_MAX_BYTES + 1)
    except OSError:
        return {}
    if len(body) > SERVED_MAX_BYTES:
        return {}
    try:
        value = json.loads(body)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
