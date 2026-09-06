#!/usr/bin/env python3
"""Every committed board mutation reaches an open client without a reload.

Two independent failures produced the stale board this covers, and each is
pinned separately here, because fixing either alone leaves the other invisible:

- a declared workflow transition committed without notifying the event hub, so
  nothing was pushed at all;
- the hub's reconciler compared a signature that could not see a transition, so
  it could not cover for a missed push -- from this server or any other writer.

The board, project, socket, port and stages all come from the fixture workflow
document rather than from constants here.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

# The server reads its role tables from the environment at import time, so a
# pane's own board configuration would otherwise decide what this fixture is
# allowed to do. Cleared before the import rather than worked around after it.
for _leaked in (
    "TICKET_BOARD_OPERATION_ALLOWED_ROLES",
    "PGU_TICKET_BOARD_OPERATION_ALLOWED_ROLES",
    "TICKET_BOARD_ROLE_ACCOUNTS",
    "TICKET_BOARD_URL",
    "PGU_TICKET_BOARD_URL",
    "TICKET_BOARD_SOCKET",
    "PGU_TICKET_BOARD_SOCKET",
    "TICKET_BOARD_CALLER_ROLE",
    "PGU_TICKET_BOARD_CALLER_ROLE",
):
    os.environ.pop(_leaked, None)

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.write_client import TicketBoardWriteClient  # noqa: E402

WORKFLOW_PATH = ROOT / "examples" / "workflows" / "inspection.json"
EVENT_TIMEOUT_SECONDS = 10.0
# The stream is quiet between scenarios and the server keepalive is 15s, so a
# socket timeout shorter than that would kill the reader during a quiet stretch
# and every later assertion would fail for the wrong reason.
STREAM_SOCKET_TIMEOUT_SECONDS = 120.0
# The reconciler polls; give it a few intervals before calling it broken.
RECONCILE_TIMEOUT_SECONDS = 6.0


class EventStream:
    """An open SSE client, reading frames on its own thread."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.frames: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._stop = threading.Event()
        self.reader_error: BaseException | None = None
        self._response = urllib.request.urlopen(
            f"{base_url}/events", timeout=STREAM_SOCKET_TIMEOUT_SECONDS
        )
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        event = ""
        try:
            for raw in self._response:
                if self._stop.is_set():
                    return
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("event: "):
                    event = line.removeprefix("event: ").strip()
                elif line.startswith("data: "):
                    payload = line.removeprefix("data: ").strip()
                    try:
                        self.frames.put((event, json.loads(payload)))
                    except json.JSONDecodeError:
                        self.frames.put((event, {"raw": payload}))
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            if not self._stop.is_set():
                # A reader that dies quietly turns every later assertion into a
                # timeout with the wrong explanation.
                self.reader_error = exc
            return

    def await_event(self, name: str, *, timeout: float = EVENT_TIMEOUT_SECONDS) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.reader_error is not None:
                raise AssertionError(f"the event stream stopped reading: {self.reader_error!r}")
            try:
                event, payload = self.frames.get(timeout=0.2)
            except queue.Empty:
                continue
            if event == name:
                return payload
        raise AssertionError(f"no {name!r} event within {timeout}s; an open board would still be stale")

    def drain(self) -> None:
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        self._stop.set()
        try:
            self._response.close()
        except Exception:  # noqa: BLE001
            pass
        self._thread.join(timeout=2.0)


def _board_state(base_url: str, ticket_id: str) -> str:
    with urllib.request.urlopen(f"{base_url}/api/board", timeout=10) as response:
        board = json.load(response)
    for ticket in board["tickets"]:
        if ticket["id"] == ticket_id:
            return str(ticket["state"])
    raise AssertionError(f"{ticket_id} is not in the board snapshot")


def _await(predicate: Callable[[], bool], *, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(message)


def main() -> int:  # noqa: C901 - one integration scenario, read top to bottom
    # The unprivileged Director path must work without Polkit or sudo, so the
    # whole exercise runs as this user and says so if it is root.
    assert os.geteuid() != 0, "run this as an unprivileged user; the path under test must not need root"

    document = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    project = document["project"]
    # One declared director transition, taken from the document rather than
    # named here, and a fresh ticket per scenario so every write is a legal move
    # from that ticket's own stage.
    stage_owners = {stage["name"]: set(stage.get("owners") or []) for stage in document["stages"]}
    implementers = [
        role["name"] for role in document["roles"]
        if role.get("kind") == "implementer" and role.get("active")
    ]
    origin = "analysis"
    director_moves = [
        item for item in document["transitions"]
        if item["from"] == origin and "director" in item["actors"]
    ]
    # The headline case: a Director hand-off to an implementer.
    handoff = next(item for item in director_moves if stage_owners.get(item["to"], set()) & set(implementers))
    # The rest only need *a* committed transition. One that does not hand work
    # to an implementer avoids serial focus queueing a second ticket back to the
    # backlog, which is correct board behaviour and not what these assert.
    quiet = next(
        item for item in director_moves
        if item is not handoff and not (stage_owners.get(item["to"], set()) & set(implementers))
    )
    destination = handoff["to"]
    quiet_destination = quiet["to"]
    pushed, reconnected_ticket, out_of_band, first_rapid, second_rapid = (
        "PGU-4", "PGU-5", "PGU-6", "PGU-7", "PGU-8",
    )
    tickets = (pushed, reconnected_ticket, out_of_band, first_rapid, second_rapid)

    with tempfile.TemporaryDirectory(prefix="ticket-board-live-sync.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        frames = root / "frames"
        assets = root / "assets"
        for directory in (socket_dir, frames, assets):
            directory.mkdir()
        port = t.free_port()
        dbname = "ticket_board_live_sync_test"
        admin_conn = t.conninfo(socket_dir, port, dbname)
        t.run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", "--username=postgres"])
        try:
            t.run(
                ["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"],
                capture=False,
            )
            t.run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", dbname])
            t.psql(admin_conn, t.SCHEMA_PATH.read_text(encoding="utf-8"))
            t.create_roles(admin_conn)
            t.psql(admin_conn, t.RBAC_PATH.read_text(encoding="utf-8"))
            for seeded in tickets:
                t.seed_postgres_ticket(
                    admin_conn, seeded, title=f"Live sync {seeded}", state=origin, assignee="director"
                )

            service_conn = t.conninfo(socket_dir, port, dbname, t.SERVICE_ROLE)
            app = t.TicketBoardApp(frames, assets, project=project, ticket_prefix="PGU", database_url=service_conn)
            app.apply_workflow(document, expected_revision=0, dry_run=False, caller_role="director")
            assert app.workflow_configuration() is not None, "the fixture must exercise the declared path"

            # Deliberately long, so scenarios 1, 2 and 4 prove the push path
            # itself: with a fast reconciler in the same hub, a missing push is
            # invisible because polling covers for it within a scan.
            hub = t.TicketBoardEventHub(app, scan_interval_seconds=30.0)
            server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier(), events=hub)
            t.TEST_WRITE_TOKEN = server.write_token
            unix_socket = root / "board.sock"
            unix_server = t.TicketBoardUnixServer(
                unix_socket,
                app,
                events=hub,
                director_notifier=t.QuietNotifier(),
                # The socket resolves the caller from the kernel-supplied uid
                # against the board's own account table, so the fixture states
                # that this account is the Director rather than the client
                # claiming it (SYRD-39).
                role_authority=t.local_role_authority_as("director"),
            )
            threads = [
                threading.Thread(target=server.serve_forever, daemon=True),
                threading.Thread(target=unix_server.serve_forever, daemon=True),
            ]
            for thread in threads:
                thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            # The Director's own path: the tenant Unix socket, no token, no root.
            director = TicketBoardWriteClient(
                board_url=base, caller_role="director", socket_path=str(unix_socket)
            )
            try:
                # 1. A transition over the socket reaches an already-open client.
                stream = EventStream(base)
                assert stream.await_event("version")["build_id"] == server.build_id
                stream.await_event("board")
                stream.drain()

                director.workflow_action(
                    pushed, handoff["action"], {"state": destination, "assignee": implementers[0]}
                )

                stream.await_event("board")
                assert _board_state(base, pushed) == destination

                # 2. A mutation during a disconnect is reconciled on reconnect.
                stream.close()
                director.workflow_action(reconnected_ticket, quiet["action"], {})
                reconnected = EventStream(base)
                reconnected.await_event("version")
                reconnected.await_event("board")
                assert _board_state(base, reconnected_ticket) == quiet_destination

                # 3. A writer this server never saw. Nothing can push for it, so
                #    only the reconciler can catch it -- and it cannot if the
                #    signature ignores the columns a transition moves. Its own
                #    hub, so this asserts polling rather than the push above.
                elsewhere = t.TicketBoardApp(
                    frames, assets, project=project, ticket_prefix="PGU", database_url=service_conn
                )
                watcher_hub = t.TicketBoardEventHub(app, scan_interval_seconds=0.2)
                watcher = t.TicketBoardServer(
                    ("127.0.0.1", 0), app, director_notifier=t.QuietNotifier(), events=watcher_hub
                )
                watcher_thread = threading.Thread(target=watcher.serve_forever, daemon=True)
                watcher_thread.start()
                try:
                    watching = EventStream(f"http://127.0.0.1:{watcher.server_port}")
                    watching.await_event("version")
                    watching.await_event("board")
                    watching.drain()
                    elsewhere.perform_workflow_action(
                        out_of_band, quiet["action"], {}, caller_role="director"
                    )
                    watching.await_event("board", timeout=RECONCILE_TIMEOUT_SECONDS)
                    assert _board_state(base, out_of_band) == quiet_destination
                    watching.close()
                finally:
                    watcher.shutdown()
                    watcher.server_close()
                    watcher_hub.close()
                    watcher_thread.join(timeout=5)

                # 4. Back-to-back mutations may coalesce into one notification,
                #    but the final state must not be missed.
                reconnected.drain()
                director.workflow_action(first_rapid, quiet["action"], {})
                director.workflow_action(second_rapid, quiet["action"], {})
                reconnected.await_event("board")
                _await(
                    lambda: (
                        _board_state(base, first_rapid) == quiet_destination
                        and _board_state(base, second_rapid) == quiet_destination
                    ),
                    timeout=EVENT_TIMEOUT_SECONDS,
                    message="a coalesced notification lost one of two rapid mutations",
                )
                reconnected.close()
            finally:
                server.shutdown()
                server.server_close()
                unix_server.shutdown()
                unix_server.server_close()
                hub.close()
                for thread in threads:
                    thread.join(timeout=5)
        finally:
            t.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], capture=False)

    print("ticket_board_live_sync_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
