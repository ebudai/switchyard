#!/usr/bin/env python3
"""SYRD-198: the endpoint an operator names is the endpoint written to.

While implementing SYRD-196 I ran the write client with an explicit, genuinely
dead `--board-url http://127.0.0.1:45678` and an empty `--socket`. It selected
`/run/pgu-ticket-board/ticket-board.sock` and posted a comment to PGU-1 from
syrd. Two defects, both provable without a network:

  * explicitness was decided by comparing the VALUE against a default that was
    itself read from the environment, so when the environment named the same URL
    the caller passed, an explicit flag became indistinguishable from no flag;
  * and the fallbacks named a PROJECT -- `http://127.0.0.1:8770` and
    `/run/pgu-ticket-board/ticket-board.sock` -- so on a multi-tenant host a
    scrubbed environment aimed every project's client at pgu.

The safety property is therefore about RESOLUTION, and is tested without
touching any board: across every environment a caller could have, no
combination of flags may resolve to a production socket the caller did not
name. The live tenants are then counted before and after to show that holds in
practice too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Sockets that belong to a running tenant on this host. Resolution must never
#: choose one of these unless the caller said its name.
PRODUCTION_SOCKETS = tuple(
    str(p) for p in Path("/run").glob("*-ticket-board/ticket-board.sock")
) + ("/tmp/pgu-ticket-board.sock",)

#: `effective_socket_path` exists both before and after this change, so a probe
#: through it fails on BEHAVIOUR rather than on a missing function. Reverting
#: write_client.py alone makes the incident case below return the pgu socket.
CLIENT_PROBE = '''
import json, sys
sys.path.insert(0, %r)
from scripts.ticket_board import write_client as wc
argv = json.loads(sys.argv[1])
client = wc.TicketBoardWriteClient(argv["board_url"], "ops", socket_path=argv["socket_path"])
print(json.dumps({"socket": client.effective_socket_path}))
'''

PROBE = '''
import json, sys
sys.path.insert(0, %r)
from scripts.ticket_board import write_client as wc
url, sock = None, None
argv = json.loads(sys.argv[1])
try:
    url, sock = wc.resolve_endpoint(argv.get("board_url"), argv.get("socket_path"))
    print(json.dumps({"ok": True, "url": url, "socket": sock}))
except wc.TicketBoardWriteError as exc:
    print(json.dumps({"ok": False, "error": str(exc)}))
'''


def resolve_under(env: dict, board_url=None, socket_path=None) -> dict:
    done = subprocess.run(
        [sys.executable, "-c", PROBE % str(ROOT),
         json.dumps({"board_url": board_url, "socket_path": socket_path})],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **env},
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def client_socket_under(env: dict, board_url, socket_path) -> str | None:
    done = subprocess.run(
        [sys.executable, "-c", CLIENT_PROBE % str(ROOT),
         json.dumps({"board_url": board_url, "socket_path": socket_path})],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **env},
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])["socket"]


def tenant_comment_counts() -> dict:
    counts = {}
    for port in (23326, 8770, 25310, 20740, 26623):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board", timeout=3) as r:
                project = json.load(r).get("project") or str(port)
        except Exception:
            continue
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tickets", timeout=5) as r:
                tickets = json.load(r)
            rows = tickets if isinstance(tickets, list) else tickets.get("tickets", [])
            counts[project] = sum(len(t.get("comments") or []) for t in rows)
        except Exception:
            counts[project] = None
    return counts


ENVIRONMENTS = {
    "scrubbed": {},
    "url names the dead port": {"TICKET_BOARD_URL": "http://127.0.0.1:45678"},
    "syrd pane": {"TICKET_BOARD_URL": "http://127.0.0.1:23326",
                  "TICKET_BOARD_SOCKET": "/run/syrd-ticket-board/ticket-board.sock"},
    "legacy pgu names": {"PGU_TICKET_BOARD_URL": "http://127.0.0.1:8770",
                         "PGU_TICKET_BOARD_SOCKET": "/run/pgu-ticket-board/ticket-board.sock"},
}

DEAD = "http://127.0.0.1:45678"


def main() -> int:
    checks = 0
    before = tenant_comment_counts()
    print(f"  live tenants seen: {sorted(before)}")

    # 1. THE INCIDENT, through the client surface that actually chose pgu on the
    #    day. First, deliberately: `effective_socket_path` exists before and after
    #    this change, so reverting write_client.py fails HERE, on behaviour,
    #    rather than later on a function that did not exist yet.
    for label, env in ENVIRONMENTS.items():
        for socket_path, how in ((None, "omitted"), ("", "emptied")):
            chosen = client_socket_under(env, DEAD, socket_path)
            assert chosen not in PRODUCTION_SOCKETS, (
                f"{label} / socket {how}: an explicit board URL chose {chosen}"
            )
            assert chosen is None, (label, how, chosen)
            checks += 2
    print("  client surface never chooses a production socket for an explicit URL")

    # 1c. The same property through the new resolver, under every environment.
    for label, env in ENVIRONMENTS.items():
        for socket_path, how in ((None, "omitted"), ("", "emptied")):
            got = resolve_under(env, board_url=DEAD, socket_path=socket_path)
            assert got["ok"], (label, how, got)
            assert got["url"] == DEAD, (label, how, got)
            assert got["socket"] is None, (label, how, got)
            checks += 1
    print(f"  explicit URL never yields a socket: {len(ENVIRONMENTS) * 2} combinations")

    # 2. No combination may resolve to a production socket the caller did not
    #    name. This is the property, stated directly.
    for label, env in ENVIRONMENTS.items():
        for board_url in (None, DEAD):
            for socket_path in (None, ""):
                got = resolve_under(env, board_url=board_url, socket_path=socket_path)
                chosen = got.get("socket")
                if chosen in PRODUCTION_SOCKETS:
                    named = env.get("TICKET_BOARD_SOCKET") or env.get("PGU_TICKET_BOARD_SOCKET")
                    assert chosen == named, (label, board_url, socket_path, chosen, named)
                checks += 1

    # 3. Nothing named anywhere fails closed, rather than guessing a project.
    got = resolve_under({}, board_url=None, socket_path=None)
    assert not got["ok"], got
    assert "no ticket board endpoint" in got["error"], got
    assert "pgu" not in got["error"].lower(), "the error must not name a project"
    checks += 3

    # 4. An explicitly emptied socket means no socket even when the environment
    #    offers one -- "I said none" must beat "one is available".
    got = resolve_under(ENVIRONMENTS["syrd pane"], board_url=DEAD, socket_path="")
    assert got["socket"] is None, got
    checks += 1

    # 5. A pane still works: environment names both, caller names neither.
    got = resolve_under(ENVIRONMENTS["syrd pane"])
    assert got["ok"] and got["socket"] == "/run/syrd-ticket-board/ticket-board.sock", got
    assert got["url"] == "http://127.0.0.1:23326", got
    checks += 2

    # 6. An explicit socket is honoured as given, including a disposable one.
    got = resolve_under({}, socket_path="/tmp/syrd198-disposable.sock")
    assert got["ok"] and got["socket"] == "/tmp/syrd198-disposable.sock", got
    checks += 1

    # 7. Nothing was written to any tenant while proving the above.
    after = tenant_comment_counts()
    assert after == before, {"before": before, "after": after}
    print(f"  live tenants unchanged: {sorted(after)}")
    checks += 1

    print(f"endpoint_authority_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
