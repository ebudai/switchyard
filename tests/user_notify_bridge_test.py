#!/usr/bin/env python3
"""SYRD-714: one host bridge sends the User's high-value events to a phone, safely, and touches nothing else.

Main has no bridge at all (its tree, from a git archive, has no `user_notify` and no command), so the rest runs on
this tree: the real bridge, outbox, configuration and Pushover adapter, against boards that answer
`/api/user-events` as pgu1021's reader does, a transport that records what it is handed, and the real urllib POST
against a local HTTP server. Each case on its own state directory, as root's records would be (owned by this uid).

- replay and restart: an event read again, by a restarted bridge or from a board that repeats itself, is one
  message; an attempt recorded before a crash is retried once after its backoff;
- provider answers: 429/5xx/timeout/unreachable retry with exponential backoff and give up after MAX_ATTEMPTS; other
  4xx, a redirect and a 200 without a valid receipt are dead letters, never retried; a valid receipt is sent;
- the credential never reaches the outbox, a log line, status, a URL or argv, even when the provider echoes it;
  it is read at each send, so a rotation is used at once;
- hostile titles stay one plain line; another project's ticket, a malformed key or field gets no message or link;
- an unavailable board is that tenant's problem: others are read and sent, and a failed or long-silent board is one
  outage message; a deliberately stopped one is none;
- a noisy tenant does not starve another; disable drains and records instead of sending; per-kind, per-project and
  operator filters suppress; links only to a tailnet host Serve proxies to that board, never via Funnel;
- install never writes the configuration and leaves an enabled or disabled bridge as it was;
- the bridge only ever GETs `/api/user-events`.
"""

from __future__ import annotations

import http.server
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import user_notify as bridge_mod  # noqa: E402
from scripts import user_notify_pushover as pushover  # noqa: E402
from scripts import user_notify_setup as setup  # noqa: E402
from scripts import user_notify_store as store  # noqa: E402

BEFORE = "16b8f0681008a1fbd4b4544e21216ffc0c439804"
TOKEN = "aTokenTokenTokenTokenTokenTok1"
USER = "uUserUserUserUserUserUserUser1"
TOKEN2 = "aSecondSecondSecondSecondSeco2"
BASE = "https://desk.tail1234.ts.net"
PORTS = {"alpha": (23001, "ALPHA"), "beta": (23002, "BETA"), "gamma": (23003, "GAMMA")}
RECEIPT = "647d2300-702c-4b38-8b2f-d56326ae460b"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class Boards:
    """Each project's board as pgu1021 answers GET /api/user-events, plus what root's units and systemd say."""

    def __init__(self) -> None:
        self.events: dict[str, list[dict]] = {p: [] for p in PORTS}
        self.failing: dict[str, str] = {}
        self.state: dict[str, str] = {p: "active" for p in PORTS}
        self.raw: dict[str, object] = {}
        self.asked: list[tuple[str, str]] = []

    def add(self, project: str, kind: str, ticket: str, *, detail: str = "", title: str = "", standing: bool = True,
            **override) -> dict:
        number = len(self.events[project]) + 1
        event = {"id": number, "key": f"{kind}:{ticket}:{number}:20261031T120000.{number:06d}", "kind": kind,
                 "ticket": ticket, "detail": detail or {"user_review": "user_review"}.get(kind, detail),
                 "at": "2026-10-31T12:00:00+00:00", "title": title or f"{ticket} title", "standing": standing}
        event.update(override)
        self.events[project].append(event)
        return event

    def add_notice(self, project: str, *, ticket: str = "", title: str = "Deploy finished",
                   message: str = "The release is live.", urgent: bool = False, reason: str = "",
                   tamper: dict | None = None, notice: dict | None = None) -> dict:
        from scripts.ticket_board import director_notice

        fields = {"title": title, "message": message, "urgent": urgent, "reason": reason}
        try:
            digest = director_notice.preview(project, PORTS[project][1], {**fields, "ticket": ticket})["digest"]
        except ValueError:  # text the board refuses: a board that sent it anyway gives it some digest
            digest = "c" * 64
        body = notice if notice is not None else {**fields, "requester_role": "director", "requester_pid": 4242,
                                                  "requester_generation": 3, "digest": digest, **(tamper or {})}
        event = self.add(project, "director_ad_hoc", ticket or "-", detail="urgent" if urgent else "normal",
                         title=f"{ticket} title" if ticket else "", notice=body)
        event["ticket"] = ticket
        return event

    def reader(self, project: str, path: str) -> object:
        self.asked.append((project, path))
        if project in self.failing:
            raise OSError(self.failing[project])
        if project in self.raw:
            return self.raw[project]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        after, limit = int(query["after"][0]), int(query["limit"][0])
        events = [e for e in self.events[project] if e["id"] > after][:limit]
        return {"available": True, "project": project, "latest": max([0] + [e["id"] for e in self.events[project]]),
                "events": events}

    def address(self, project: str) -> tuple[int, str]:
        return PORTS[project]


class Transport:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[pushover.Message, dict]] = []
        self.outcomes: list[pushover.Outcome] = []
        self.crash = False

    def send(self, message: pushover.Message, credential: dict) -> pushover.Outcome:
        self.sent.append((message, dict(credential)))
        if self.crash:
            raise KeyboardInterrupt("killed mid-send")
        return self.outcomes.pop(0) if self.outcomes else pushover.Outcome("sent", "delivered", RECEIPT)


def serve_status() -> dict:
    return {"Web": {f"desk.tail1234.ts.net:{PORTS['alpha'][0]}": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:23001"}}},
                    f"desk.tail1234.ts.net:{PORTS['beta'][0]}": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:23002"}}},
                    f"desk.tail1234.ts.net:{PORTS['gamma'][0]}": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:23003"}}}},
            "AllowFunnel": {f"desk.tail1234.ts.net:{PORTS['gamma'][0]}": True}}


class Host:
    def __init__(self, tmp: Path, *, configured: bool = True, enabled: bool = True) -> None:
        self.tmp = tmp
        self.paths = setup.Paths(config_dir=tmp / "etc", state_dir=tmp / "state", registry_dir=tmp / "projects",
                                 unit_dir=tmp / "units", owner_uid=os.getuid())
        self.paths.registry_dir.mkdir(parents=True)
        for project in PORTS:
            (self.paths.registry_dir / f"{project}.json").write_text("{}")
        self.clock, self.boards, self.transport, self.logs = Clock(), Boards(), Transport(), []
        if configured:
            setup.save_config(self.paths, {"enabled": enabled, "operator": "eric", "base_url": BASE})
            setup.save_credential(self.paths, [TOKEN, USER])
        self.bridge = self.new_bridge()

    def new_bridge(self, transport=None) -> bridge_mod.Bridge:
        return bridge_mod.Bridge(self.paths, outbox=store.Outbox(self.paths.outbox, clock=self.clock),
                                 reader=self.boards.reader, address=self.boards.address,
                                 board_state=lambda project: self.boards.state[project],
                                 transport=transport or self.transport, serve_status=serve_status, clock=self.clock,
                                 log=lambda **fields: self.logs.append(json.dumps(fields, sort_keys=True)))

    def rows(self, **where) -> list[dict]:
        return [dict(r) for r in self.bridge.outbox.db.execute("SELECT * FROM outbox ORDER BY seq")
                if all(dict(r)[k] == v for k, v in where.items())]

    def configure(self, **values) -> None:
        setup.save_config(self.paths, {**setup.load_config(self.paths).values, **values})


def fresh(tmp: Path, name: str, **kwargs) -> Host:
    host = Host(tmp / name, **kwargs)
    host.bridge.pass_once()  # every tenant joins at its board's latest event
    return host


# -- cases ------------------------------------------------------------------------------------------------------------
def main_has_no_bridge(tmp: Path) -> None:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", BEFORE, "scripts"], check=True,
                             capture_output=True).stdout
    names = tarfile.open(fileobj=io.BytesIO(archive)).getnames()
    check(not any(re.search(r"user_notify|switchyard-user-notify", n) for n in names),
          "main has no phone bridge, command or outbox: nothing reaches a phone")


def first_message(tmp: Path) -> None:
    host = Host(tmp / "first", configured=False)
    host.boards.add("alpha", "user_review", "ALPHA-1")
    host.bridge.pass_once()
    check(host.rows() == [] and host.transport.sent == [] and host.bridge.outbox.tenant("alpha")["after"] == 1,
          "unconfigured: each tenant joins at its latest event; nothing before it is history, nothing is sent")
    setup.save_config(host.paths, {"enabled": True, "operator": "eric", "base_url": BASE})
    setup.save_credential(host.paths, [TOKEN, USER])
    host.boards.add("alpha", "user_review", "ALPHA-2", title="Ship the <b>parser</b>")
    host.boards.add("alpha", "operator_blocker", "ALPHA-3", detail="eric")
    host.boards.add("alpha", "operation_failed", "ALPHA-4", detail="deploy-release")
    result = host.bridge.pass_once()
    sent = host.transport.sent
    check(result["sent"] == ["sent"] * 3 and len(sent) == 3, f"configured: each new event is sent once: {result}")
    message, credential = sent[0]
    check(message.title == "alpha: ALPHA-2 needs your review"
          and message.body == "ALPHA-2 Ship the <b>parser</b> -- waiting for you in User Review"
          and message.url == f"{BASE}:23001/#ticket=ALPHA-2" and message.url_title == "Open ALPHA-2"
          and message.priority == 0 and credential == {"app_token": TOKEN, "user_key": USER},
          f"project, ticket, short title, kind and a fixed action; the link is that board's own page: {message}")
    check(sent[1][0].body.endswith("blocked until operator:eric acts")
          and sent[2][0].body.endswith("privileged action deploy-release did not succeed; see the ticket"),
          f"each kind's safe action summary: {[m.body for m, _c in sent]}")
    check([r["state"] for r in host.rows()] == ["sent"] * 3 and all(r["receipt"] == RECEIPT for r in host.rows()),
          "each recorded sent, with the provider's receipt")
    check(set(host.boards.asked) <= {(p, f"/api/user-events?after={a}&limit=20") for p in PORTS for a in range(0, 10)}
          and {p for p, _path in host.boards.asked} == set(PORTS),
          f"every registered tenant is read, and only ever through GET /api/user-events: {host.boards.asked[:6]}")


def replay_and_restart(tmp: Path) -> None:
    host = fresh(tmp, "replay")
    host.boards.add("alpha", "user_review", "ALPHA-1")
    host.bridge.pass_once()
    host.bridge.outbox.close()
    host.bridge = host.new_bridge()  # a restarted service, same state directory
    host.bridge.pass_once()
    host.boards.raw["alpha"] = {"available": True, "latest": 1, "events": []}
    host.bridge.outbox.db.execute("UPDATE tenants SET after = 0 WHERE project = 'alpha'")  # a lost cursor write
    host.boards.raw.pop("alpha")
    host.bridge.pass_once()
    check(len(host.transport.sent) == 1 and len(host.rows()) == 1,
          f"read again after a restart and after a lost cursor: one event, one message: {len(host.transport.sent)}")
    # Crash after the attempt is recorded, before the provider answers: at-least-once.
    host.boards.add("alpha", "user_review", "ALPHA-2")
    host.transport.crash = True
    try:
        host.bridge.pass_once()
    except KeyboardInterrupt:
        pass
    host.transport.crash = False
    host.bridge = host.new_bridge()
    host.bridge.pass_once()
    early = len(host.transport.sent)
    host.clock.now += store.backoff(1)
    host.bridge.pass_once()
    rows = host.rows(ticket="ALPHA-2")
    check(early == 2 and len(host.transport.sent) == 3 and rows[0]["state"] == "sent" and rows[0]["attempts"] == 2,
          f"an attempt cut short by a crash is retried once, after its backoff, and then sent: {rows}")
    # A board whose record was recreated (latest below the cursor) is joined again, at its latest.
    host.boards.events["alpha"] = []
    host.boards.add("alpha", "user_review", "ALPHA-9")
    host.bridge.pass_once()
    check(host.bridge.outbox.tenant("alpha")["after"] == 1 and not host.rows(ticket="ALPHA-9"),
          "a board whose event record restarted is joined again at its latest, not replayed")


def provider_answers(tmp: Path) -> None:
    def adapter(status: int, body: object, raises: BaseException | None = None):
        posted: list = []

        def post(url: str, data: bytes, timeout: float):
            posted.append((url, urllib.parse.parse_qs(data.decode()), timeout))
            if raises:
                raise raises
            return status, body if isinstance(body, bytes) else json.dumps(body).encode()
        return pushover.Pushover(post=post), posted

    message = pushover.Message("t", "b", url=f"{BASE}:23001/#ticket=ALPHA-1", url_title="Open ALPHA-1")
    credential = {"app_token": TOKEN, "user_key": USER}
    cases = {
        "ok": (200, {"status": 1, "request": RECEIPT}, None),
        "status_true": (200, {"status": True, "request": RECEIPT}, None),
        "no_receipt": (200, {"status": 1}, None),
        "bad_receipt": (200, {"status": 1, "request": "x"}, None),
        "not_json": (200, b"<html>ok</html>", None),
        "huge": (200, b" " * (pushover.MAX_ANSWER_BYTES + 10), None),
        "400": (400, {"status": 0, "errors": [f"application token is invalid: {TOKEN}"], "request": RECEIPT}, None),
        "403": (403, {"status": 0, "errors": ["user key invalid"]}, None),
        "302": (302, b"", None),
        "429": (429, {"status": 0, "errors": ["over the limit"]}, None),
        "500": (500, b"oops", None),
        "503": (503, b"", None),
        "timeout": (0, b"", TimeoutError("timed out")),
        "refused": (0, b"", ConnectionRefusedError(111, "Connection refused")),
    }
    got = {}
    for name, (status, body, raises) in cases.items():
        transport, posted = adapter(status, body, raises)
        got[name] = transport.send(message, credential)
        if name == "ok":
            url, fields, timeout = posted[0]
            check(url == pushover.ENDPOINT and TOKEN not in url and fields["token"] == [TOKEN]
                  and fields["user"] == [USER] and fields["html"] == ["0"] and fields["ttl"] == [str(pushover.TTL_SECONDS)]
                  and fields["url"] == [message.url] and timeout == pushover.TIMEOUT_SECONDS,
                  f"HTTPS POST to the fixed endpoint, the credential in the body only, html off, a TTL: {posted}")
    check(got["ok"] == pushover.Outcome("sent", "delivered to Pushover", RECEIPT), f"a valid receipt is sent: {got['ok']}")
    for name in ("status_true", "no_receipt", "bad_receipt", "not_json", "huge"):
        check(got[name].kind == "dead" and "without a valid receipt" in got[name].detail,
              f"{name}: a 200 that is not exactly a success is a dead letter, never retried: {got[name]}")
    for name in ("400", "403", "302"):
        check(got[name].kind == "dead" and f"HTTP {name}" in got[name].detail, f"{name} is refused for good: {got[name]}")
    check(TOKEN not in got["400"].detail and "[redacted]" in got["400"].detail,
          f"a provider that echoes the token has it redacted: {got['400']}")
    for name in ("429", "500", "503", "timeout", "refused"):
        check(got[name].kind == "retry", f"{name} may pass: retried: {got[name]}")
    check(pushover.Pushover(post=lambda *a: (200, b"")).send(message, {"app_token": "short", "user_key": USER}).kind
          == "dead", "a credential not of Pushover's form is never sent")
    check(pushover.redact(f"old key {TOKEN2} here") == "old key [redacted] here"
          and pushover.redact("a short-secret-1 here", ("short-secret-1",)) == "a [redacted] here",
          "redaction covers anything shaped like a credential, a rotated-out one included, and each current secret")
    # Backoff and giving up, through the bridge.
    host = fresh(tmp, "retry")
    host.boards.add("beta", "user_review", "BETA-1")
    host.transport.outcomes = [pushover.Outcome("retry", "Pushover answered HTTP 503")] * store.MAX_ATTEMPTS
    waits = []
    for _ in range(store.MAX_ATTEMPTS):
        host.bridge.pass_once()
        row = host.rows(ticket="BETA-1")[0]
        waits.append(round(row["due"] - host.clock.now))
        host.clock.now = row["due"]
    row = host.rows(ticket="BETA-1")[0]
    check(waits[:7] == [30, 60, 120, 240, 480, 960, 1920] and row["state"] == "dead" and row["attempts"] == 8
          and row["reason"].startswith("gave up after 8 attempts"),
          f"bounded exponential backoff, then a dead letter that says why: {waits} {row['reason']}")
    host.transport.outcomes = [pushover.Outcome("retry", "Pushover answered HTTP 500")]
    host.boards.add("beta", "user_review", "BETA-2")
    host.bridge.pass_once()
    host.clock.now += store.EXPIRY_SECONDS + 1
    host.bridge.pass_once()
    row = host.rows(ticket="BETA-2")[0]
    check(row["state"] == "dead" and row["reason"].startswith("expired") and row["attempts"] == 1,
          f"a message still unsent a day after its event is a dead letter, not a late alert: {row}")


def real_post(tmp: Path) -> None:
    """The real urllib POST, against a local server: the body read, a redirect not followed, a stall cut off."""
    seen: list = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, body, self.headers.get("Content-Type")))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.end_headers()
                return
            if self.path == "/slow":
                threading.Event().wait(2.0)
            answer = json.dumps({"status": 1, "request": RECEIPT}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, body = pushover.default_post(f"{base}/ok", b"token=x", 5.0)
        check(status == 200 and json.loads(body)["request"] == RECEIPT
              and seen[0][2] == "application/x-www-form-urlencoded", f"the real POST reads the answer: {status} {seen}")
        status, _body = pushover.default_post(f"{base}/redirect", b"token=x", 5.0)
        check(status == 302 and len(seen) == 2, f"a redirect is answered as itself and never followed: {status}")
        slow = pushover.Pushover(endpoint=f"{base}/slow", timeout=0.5).send(
            pushover.Message("t", "b"), {"app_token": TOKEN, "user_key": USER})
        check(slow.kind == "retry", f"no answer within the timeout is a retry: {slow}")
    finally:
        server.shutdown()
        server.server_close()


def secrets_stay_secret(tmp: Path) -> None:
    host = fresh(tmp, "secrets")
    host.boards.add("alpha", "user_review", "ALPHA-1")
    host.boards.add("alpha", "user_review", "ALPHA-2")
    host.transport.outcomes = [pushover.Outcome("dead", f"Pushover refused the message: token {TOKEN} user {USER}"),
                               pushover.Outcome("retry", f"Pushover unreachable at {TOKEN}")]
    host.bridge.pass_once()
    status = bridge_mod.status_text(host.paths, host.bridge)
    host.bridge.outbox.db.execute("PRAGMA wal_checkpoint(FULL)")
    stored = b"".join(p.read_bytes() for p in host.paths.state_dir.iterdir() if p.is_file())
    everything = "\n".join(host.logs) + status
    check(TOKEN not in everything and USER not in everything and TOKEN.encode() not in stored
          and USER.encode() not in stored and "[redacted]" in everything,
          f"neither secret reaches a log line, status or the outbox, even when the provider says it: {host.logs[-2:]}")
    check(oct(host.paths.credential.stat().st_mode & 0o777) == "0o600"
          and oct(host.paths.outbox.stat().st_mode & 0o777) == "0o600",
          "the credential and the outbox are root's alone")
    # Rotation: the next message uses the new credential, with no restart.
    setup.save_credential(host.paths, [TOKEN2, USER])
    host.boards.add("alpha", "user_review", "ALPHA-3")
    host.bridge.pass_once()
    check(host.transport.sent[-1][1]["app_token"] == TOKEN2, "a rotated credential is used from the next message")
    # A credential file others can read, or a link, is refused; nothing is attempted, nothing is lost.
    os.chmod(host.paths.credential, 0o644)
    host.boards.add("alpha", "user_review", "ALPHA-4")
    before = len(host.transport.sent)
    host.bridge.pass_once()
    os.chmod(host.paths.credential, 0o600)
    real = host.paths.credential.with_name("real.json")
    host.paths.credential.rename(real)
    host.paths.credential.symlink_to(real)
    host.bridge.pass_once()
    row = host.rows(ticket="ALPHA-4")[0]
    check(len(host.transport.sent) == before and row["state"] == "pending" and row["attempts"] == 0
          and any("readable by nobody else" in line for line in host.logs)
          and any("cannot read" in line or "must be a regular file" in line for line in host.logs[-1:]),
          f"a loose or linked credential is not used: the message waits, no attempt spent: {host.logs[-2:]}")
    host.paths.credential.unlink()
    real.rename(host.paths.credential)
    # set-credential takes a descriptor or stdin, never an argument.
    parser = bridge_mod.build_parser()
    options = {a for action in parser._subparsers._group_actions[0].choices["set-credential"]._actions
               for a in action.option_strings}
    check(options == {"-h", "--help", "--from-fd"}, f"no command-line option can carry a secret: {options}")
    read, write = os.pipe()
    os.write(write, f"{TOKEN}\n{USER}\n".encode())
    os.close(write)
    said: list[str] = []
    with mock.patch("os.geteuid", return_value=0):
        code = bridge_mod.main(["set-credential", "--from-fd", str(read)], paths=host.paths, out=said.append)
    os.close(read)
    check(code == 0 and setup.read_credential(host.paths) == {"app_token": TOKEN, "user_key": USER}
          and TOKEN not in said[0], f"set-credential stores both from a descriptor, and does not echo them: {said}")
    code = bridge_mod.main(["set-credential", "--from-fd", "0"], paths=host.paths, out=said.append)
    check(code == 1 and "as root" in said[-1], f"only root writes it: {said[-1]}")


def hostile_text(tmp: Path) -> None:
    host = fresh(tmp, "hostile")
    nasty = "Fix\n\rPWNED\x1b[31m ‮gnp.exe⁦ <script>alert(1)</script> ​" + "x" * 5000
    host.boards.add("alpha", "user_review", "ALPHA-1", title=nasty)
    host.boards.add("alpha", "user_review", "BETA-7")  # another project's ticket, from alpha's board
    host.boards.add("alpha", "user_review", "ALPHA-2", key="user_review:ALPHA-9:3:20261031T120000.000003")
    host.boards.add("alpha", "operator_blocker", "ALPHA-3", detail="eric;rm -rf")
    host.boards.add("alpha", "user_review", "ALPHA-4/../x")
    host.bridge.pass_once()
    message = host.transport.sent[0][0]
    check(len(host.transport.sent) == 1 and "\n" not in message.body and "\r" not in message.body
          and "\x1b" not in message.body and "‮" not in message.body and "⁦" not in message.body
          and "​" not in message.body and len(message.body) <= pushover.BODY_LIMIT
          and len(message.title) <= pushover.TITLE_LIMIT and "<script>" in message.body
          and message.url == f"{BASE}:23001/#ticket=ALPHA-1",
          f"a hostile title is one plain line, cut short, its markup literal (html=0), and moves no link: {message}")
    reasons = {r["ticket"] or r["key"]: (r["state"], r["reason"]) for r in host.rows()[1:]}
    check(all(state == "suppressed" for state, _ in reasons.values()) and len(reasons) == 4
          and sum(1 for _s, why in reasons.values() if why == "not this project's ticket") == 2
          and sum(1 for _s, why in reasons.values() if why == "an event not of the expected form") == 2,
          f"another project's ticket, a key that is not its own, a detail or ticket not of the form: recorded, never "
          f"sent or linked: {reasons}")
    check(setup.link(BASE, 23001, "ALPHA", "BETA-7") == "" and setup.link(BASE, 23001, "ALPHA", "ALPHA-1#x") == ""
          and setup.link(BASE, 23001, "ALPHA", "ALPHA-12") == f"{BASE}:23001/#ticket=ALPHA-12",
          "a link is only ever to that project's own ticket")
    for answer in ("nonsense", {"latest": "1", "events": []}, {"latest": 1, "events": [{"id": 0}]},
                   {"latest": 1, "events": [{}] * 51}):
        host.boards.raw["beta"] = answer
        check(host.bridge.read_tenant("beta", setup.load_config(host.paths)) == "failed",
              f"an answer not of the form is a failed read, never a message: {str(answer)[:60]}")
    host.boards.raw.pop("beta")


def outages_and_isolation(tmp: Path) -> None:
    host = fresh(tmp, "outage")
    host.configure(outage_minutes=15)
    host.boards.failing["alpha"] = "alpha's board socket does not answer"
    host.boards.add("beta", "user_review", "BETA-1")
    host.bridge.pass_once()
    check([m.title for m, _c in host.transport.sent] == ["beta: BETA-1 needs your review"]
          and host.bridge.outbox.tenant("alpha")["failing_since"] == host.clock.now,
          "one board down: the others are read and sent as usual, and the failure is recorded")
    host.clock.now += 14 * 60
    host.bridge.pass_once()
    check(len(host.transport.sent) == 1, "a board silent for less than outage_minutes is not yet an outage")
    host.clock.now += 2 * 60
    host.bridge.pass_once()
    host.clock.now += 60
    host.bridge.pass_once()
    outage = host.transport.sent[-1][0]
    check(len(host.transport.sent) == 2 and outage.title == "alpha: board is down" and outage.priority == 1
          and "has not answered for 16 minutes" in outage.body,
          f"a running board silent past outage_minutes is one high-priority outage message: {outage}")
    del host.boards.failing["alpha"]
    host.bridge.pass_once()
    host.boards.failing["alpha"] = "again"
    host.boards.state["alpha"] = "failed"
    host.bridge.pass_once()
    check(len(host.transport.sent) == 3 and host.transport.sent[-1][0].title == "alpha: board is down",
          "after it recovers, a board whose unit has failed is a new outage at once")
    host.boards.failing["gamma"] = "stopped"
    host.boards.state["gamma"] = "inactive"
    host.bridge.pass_once()
    host.clock.now += 3600
    host.bridge.pass_once()
    check(not any(m.title.startswith("gamma") for m, _c in host.transport.sent),
          "a board stopped on purpose is not an outage")


def fairness(tmp: Path) -> None:
    host = fresh(tmp, "fair")
    for n in range(1, 61):
        host.boards.add("alpha", "user_review", f"ALPHA-{n}")
    host.bridge.pass_once()  # reads 20 of alpha's, sends 10
    host.boards.add("beta", "user_review", "BETA-1")
    host.bridge.pass_once()
    titles = [m.title for m, _c in host.transport.sent]
    check(len(titles) == 20 and "beta: BETA-1 needs your review" in titles[10:],
          f"a tenant with sixty events does not hold back another's one: {titles[10:]}")
    check(host.bridge.outbox.tenant("alpha")["after"] == 40, "each pass reads at most EVENT_LIMIT events per tenant")


def controls(tmp: Path) -> None:
    host = fresh(tmp, "controls")
    host.transport.outcomes = [pushover.Outcome("retry", "Pushover answered HTTP 503")]
    host.boards.add("alpha", "user_review", "ALPHA-1")
    host.bridge.pass_once()
    said: list[str] = []
    with mock.patch("os.geteuid", return_value=0):
        bridge_mod.main(["disable"], paths=host.paths, bridge=host.bridge, out=said.append)
    host.clock.now += 3600
    host.boards.add("alpha", "user_review", "ALPHA-2")
    host.bridge.pass_once()
    check(host.rows(ticket="ALPHA-1")[0]["state"] == "drained"
          and host.rows(ticket="ALPHA-2")[0]["reason"] == "notifications are disabled"
          and len(host.transport.sent) == 1 and "1 pending message(s) drained" in said[0],
          f"disable: what was pending is drained, new events are recorded and not sent: {said}")
    with mock.patch("os.geteuid", return_value=0):
        bridge_mod.main(["enable"], paths=host.paths, bridge=host.bridge, out=said.append)
    host.configure(events={"operation_failed": False}, projects={"beta": False})
    host.boards.add("alpha", "user_review", "ALPHA-3")
    host.boards.add("alpha", "operation_failed", "ALPHA-4", detail="deploy-release")
    host.boards.add("alpha", "operator_blocker", "ALPHA-5", detail="alice")
    host.boards.add("alpha", "user_review", "ALPHA-6", standing=False)
    host.boards.add("beta", "user_review", "BETA-1")
    host.bridge.pass_once()
    why = {r["ticket"]: (r["state"], r["reason"]) for r in host.rows()}
    check([m.title for m, _c in host.transport.sent[1:]] == ["alpha: ALPHA-3 needs your review"]
          and why["ALPHA-4"] == ("suppressed", "operation_failed notifications are off")
          and why["ALPHA-5"] == ("suppressed", "operator:alice is not the configured operator")
          and why["ALPHA-6"] == ("suppressed", "resolved before it was read")
          and why["BETA-1"] == ("suppressed", "project beta is opted out"),
          f"enabled again: per-kind, per-project and operator filters, and resolved events, each say why: {why}")
    # An opt-out made after an event was recorded still holds at send time.
    host.transport.outcomes = [pushover.Outcome("retry", "busy")]
    host.boards.add("alpha", "user_review", "ALPHA-7")
    host.bridge.pass_once()
    host.configure(projects={"alpha": False, "beta": False})
    host.clock.now += 60
    host.bridge.pass_once()
    check(host.rows(ticket="ALPHA-7")[0]["state"] == "suppressed" and len(host.transport.sent) == 3,
          f"an opt-out made while a message waits is honoured before it is sent: {host.rows(ticket='ALPHA-7')}")
    with mock.patch("os.geteuid", return_value=0):
        bridge_mod.main(["drain"], paths=host.paths, bridge=host.bridge, out=said.append)
    check("pending message(s) drained" in said[-1], f"drain: {said[-1]}")
    check(bridge_mod.main(["enable"], paths=host.paths, bridge=host.bridge, out=said.append) == 1
          and "as root" in said[-1], f"only root changes the controls: {said[-1]}")
    status = bridge_mod.status_text(host.paths, host.bridge)
    check("projects opted out: alpha, beta" in status and "operation_failed off" in status
          and "dead letters" in status and "credential: present" in status, f"status shows the boundary: {status}")


def links(tmp: Path) -> None:
    host = fresh(tmp, "links")
    host.boards.add("gamma", "user_review", "GAMMA-1")
    host.bridge.pass_once()
    check(host.transport.sent[-1][0].url == "", "a board Funnel publishes gets no link: Funnel is refused")
    no_tailscale = host.new_bridge()

    def down() -> dict:
        raise setup.SetupError("tailscale serve status failed")
    no_tailscale.serve_status = down
    host.boards.add("alpha", "user_review", "ALPHA-1")
    no_tailscale.pass_once()
    check(host.transport.sent[-1][0].url == "" and host.transport.sent[-1][0].title == "alpha: ALPHA-1 needs your review",
          "Tailscale unavailable: the message is still sent, without a link")
    check(setup.served(serve_status(), BASE, 23001) == ""
          and setup.served(serve_status(), BASE, 23003).startswith("refused: Funnel")
          and setup.served({}, BASE, 23001) == "not served: run `tailscale serve --bg --https=23001 http://127.0.0.1:23001`"
          and setup.served({"Web": {"desk.tail1234.ts.net:23001": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9"}}}}},
                           BASE, 23001).startswith("not served"),
          "only a Serve proxy to that board's own loopback port counts; the preview names the one command needed")
    for bad in ("http://desk.tail1234.ts.net", "https://example.com", "https://desk.tail1234.ts.net:443",
                "https://desk.tail1234.ts.net/board", "https://u:p@desk.tail1234.ts.net", "https://8.8.8.8",
                "https://desk.tail1234.ts.net?x=1", "https://ts.net", "https://evil.ts.net.example.com"):
        try:
            setup.base_url(bad)
            check(False, f"{bad} accepted")
        except setup.SetupError:
            check(True, "")
    check(setup.base_url("https://100.101.102.103") == "https://100.101.102.103"
          and setup.base_url("https://[fd7a:115c:a1e0::1]") == "https://[fd7a:115c:a1e0::1]"
          and setup.base_url("https://desk.tail1234.ts.net/") == BASE, "tailnet names and addresses are accepted")
    said: list[str] = []
    bridge_mod.main(["configure", "--operator", "eric", "--base-url", BASE, "--event", "project_outage=off"],
                    paths=host.paths, bridge=host.bridge, out=said.append)
    preview = "\n".join(said)
    check("preview; nothing was changed" in preview and '"project_outage": false' in preview
          and "gamma: refused: Funnel" in preview and "alpha: private board link served" in preview
          and setup.load_config(host.paths)["events"]["project_outage"] is True,
          f"configure previews the change and each project's link, and writes nothing: {preview[-400:]}")


def board_address(tmp: Path) -> None:
    from scripts.ticket_board import board_identity

    units = tmp / "address" / "units"
    units.mkdir(parents=True, mode=0o755)
    good = ('[Service]\nUser=boardsvc\nEnvironment="TICKET_BOARD_PROJECT_NAME=Alpha Board"\n'
            "Environment=TICKET_BOARD_TICKET_PREFIX=ALPHA\n"
            "ExecStart=/usr/bin/python3 -E -s -B /opt/switchyard/releases/x/scripts/ticket-board.py --host 127.0.0.1 "
            "--port 23001 --unix-socket /run/alpha-ticket-board/ticket-board.sock\n")

    def answer(text: str) -> object:
        path = units / "alpha-ticket-board.service"
        path.write_text(text)
        os.chmod(path, 0o644)
        try:
            return board_identity.unit_board_address("alpha", unit_dir=units, owner_uid=os.getuid(), anchor=tmp)
        except board_identity.BoardIdentityError as exc:
            return str(exc)
    check(answer(good) == (23001, "ALPHA"), f"the port and prefix come from root's unit: {answer(good)}")
    for name, text in (("public", good.replace("--host 127.0.0.1", "--host 0.0.0.0")),
                       ("two ports", good.replace("--port 23001", "--port 23001 --port 9")),
                       ("no prefix", good.replace("TICKET_BOARD_TICKET_PREFIX=ALPHA", "X=1")),
                       ("bad prefix", good.replace("PREFIX=ALPHA", "PREFIX=al/pha"))):
        check(isinstance(answer(text), str), f"{name}: refused, no link: {answer(text)}")
    answer(good)
    os.chmod(units / "alpha-ticket-board.service", 0o666)
    try:
        loose = board_identity.unit_board_address("alpha", unit_dir=units, owner_uid=os.getuid(), anchor=tmp)
    except board_identity.BoardIdentityError as exc:
        loose = str(exc)
    check(isinstance(loose, str), f"a unit others may write names nothing: {loose}")


def director_ad_hoc(tmp: Path) -> None:
    host = fresh(tmp, "adhoc")
    host.boards.add_notice("alpha", ticket="ALPHA-1")
    host.boards.add_notice("alpha", message="No ticket this time.")
    host.boards.add_notice("alpha", message="Urgent.", urgent=True, reason="half deployed")
    host.boards.add_notice("gamma", ticket="GAMMA-2", message="Its board is published by Funnel.")
    host.bridge.pass_once()
    sent = [m for m, _c in host.transport.sent]
    check(sorted((m.title, m.body, m.url, m.priority) for m in sent) == sorted([
        ("alpha Director: Deploy finished", "The release is live. -- ALPHA-1", f"{BASE}:23001/#ticket=ALPHA-1", 0),
        ("alpha Director: Deploy finished", "No ticket this time.", "", 0),
        ("alpha Director: Deploy finished", "Urgent.", "", 0),
        ("gamma Director: Deploy finished", "Its board is published by Funnel. -- GAMMA-2", "", 0)]),
          f"the Director's notification, attributed to the project's Director, linked only to its own ticket and with "
          f"no link without one; urgent stays normal priority unless the operator allows it: {sent}")
    # End to end: what the phone got is what the board previewed for the Director to confirm -- the same title and
    # body, a link exactly when the preview names a ticket's page, and then only that ticket's page on its board.
    from scripts.ticket_board import director_notice

    rows = host.rows(kind="director_ad_hoc")
    check(len(rows) == len(sent) == 4, f"each of the four notices was delivered once: {len(rows)} {len(sent)}")
    for row in rows:
        message = next(m for m in sent if m.body.startswith(row["message"]))
        shown = director_notice.preview(row["project"], PORTS[row["project"]][1], {
            "title": row["title"], "message": row["message"], "ticket": row["ticket"],
            "urgent": row["detail"] == "urgent", "reason": "r" if row["detail"] == "urgent" else ""})["preview"]
        port = PORTS[row["project"]][0]
        check((message.title, message.body) == (shown["title"], shown["message"])
              and (message.url == "" if shown["link"] == "none" else message.url in ("", f"{BASE}:{port}/#ticket={row['ticket']}"))
              and (shown["link"] != "none") == bool(row["ticket"]),
              f"delivered as previewed: {message} against {shown}")
    check(sum(1 for m in sent if m.url) == 1, "only the served ticket's page is linked")
    rows = host.rows(kind="director_ad_hoc")
    check(all(r["requester"] == "director pid 4242 generation 3" and re.fullmatch(r"[0-9a-f]{64}", r["digest"])
              and r["state"] == "sent" for r in rows),
          f"the ledger records who asked, the confirmed digest and the outcome: {rows[0]}")
    host.configure(director_urgent=True)
    host.boards.add_notice("alpha", message="Urgent again.", urgent=True, reason="still half deployed")
    host.bridge.pass_once()
    check(host.transport.sent[-1][0].priority == 1, "with the operator's separate permission, urgent is high priority")
    host.boards.add_notice("alpha", tamper={"message": "Something the Director never confirmed."})
    host.boards.add_notice("alpha", message="see https://evil.example")  # the board would have refused it
    host.boards.add_notice("alpha", tamper={"requester_role": "Director; rm"})
    host.boards.add_notice("beta", ticket="ALPHA-1")
    host.boards.add_notice("alpha", notice={})
    before = len(host.transport.sent)
    host.bridge.pass_once()
    why = [r["reason"] for r in host.rows(kind="director_ad_hoc")]
    check(len(host.transport.sent) == before
          and "not the notification the Director confirmed (its digest differs)" in why
          and any(w.startswith("a Director's notification the board should have refused") for w in why)
          and "a Director's notification not of the expected form" in why
          and "not this project's ticket" in why,
          f"a notice changed after its confirmation, text the board must refuse, a malformed requester, or another "
          f"board's ticket: recorded, never sent: {why}")
    host.configure(events={"director_ad_hoc": False})
    host.boards.add_notice("alpha", message="Filtered.")
    host.bridge.pass_once()
    check(host.rows(kind="director_ad_hoc")[-1]["reason"] == "director_ad_hoc notifications are off",
          "the Director's notifications have their own filter")
    host.configure(events={"director_ad_hoc": True})
    for n in range(12):
        host.boards.add_notice("gamma", message=f"Number {n}.")
    host.bridge.pass_once()
    gamma = host.rows(project="gamma")
    check(sum(r["state"] != "suppressed" for r in gamma) == bridge_mod.AD_HOC_PER_HOUR
          and gamma[-1]["reason"].startswith("more than 10 Director notifications from gamma"),
          f"past the bridge's own hourly cap, a board's Director notifications are recorded, not sent: "
          f"{[r['reason'] for r in gamma[-3:]]}")


def install_preserves(tmp: Path) -> None:
    host = Host(tmp / "install", enabled=False)
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    said: list[str] = []
    config = host.paths.config.read_bytes()
    code = setup.install(host.paths, apply=True, runner=runner, out=said.append, euid=lambda: 0)
    first = list(calls)
    calls.clear()
    code2 = setup.install(host.paths, apply=True, runner=runner, out=said.append, euid=lambda: 0)
    unit = (host.paths.unit_dir / setup.UNIT).read_text()
    check(code == code2 == 0 and first == [["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", setup.UNIT]]
          and calls == [["systemctl", "daemon-reload"], ["systemctl", "try-restart", setup.UNIT]],
          f"a first install starts the service; a reinstall restarts it only if running, enabling nothing: {calls}")
    check(host.paths.config.read_bytes() == config and setup.load_config(host.paths)["enabled"] is False,
          "install never writes the configuration: a disabled bridge stays disabled")
    check("ExecStart=/usr/bin/python3 -I -B /opt/switchyard/current/scripts/user_notify.py serve" in unit
          and "Restart=always" in unit and "tmux" not in unit and TOKEN not in unit and "ProtectHome=yes" in unit,
          f"one supervised host service, starting no pane, carrying no secret: {unit}")
    check(setup.install(host.paths, apply=True, runner=runner, out=said.append, euid=lambda: 1000) == 1,
          "only root installs it")


def test_push(tmp: Path) -> None:
    host = fresh(tmp, "testpush", enabled=False)
    said: list[str] = []
    with mock.patch("os.geteuid", return_value=0):
        code = bridge_mod.main(["test"], paths=host.paths, bridge=host.bridge, out=said.append)
    message = host.transport.sent[-1][0]
    check(code == 0 and message.title == "switchyard: test notification" and message.url == ""
          and f"receipt {RECEIPT}" in said[-1], f"a test notification goes now, even while disabled: {said}")
    host.transport.outcomes = [pushover.Outcome("retry", "Pushover answered HTTP 503")]
    with mock.patch("os.geteuid", return_value=0):
        code = bridge_mod.main(["test"], paths=host.paths, bridge=host.bridge, out=said.append)
    host.clock.now += 3600
    host.configure(enabled=True)
    host.bridge.pass_once()
    check(code == 1 and "test dead: test not retried" in said[-1] and len(host.transport.sent) == 2,
          f"a failed test says why and is not retried later: {said[-1]}")


def main() -> int:
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_"))]:
        os.environ.pop(key)
    with tempfile.TemporaryDirectory(prefix="syrd714-bridge.") as tmp:
        root = Path(tmp)
        for case in (main_has_no_bridge, first_message, replay_and_restart, provider_answers, real_post,
                     secrets_stay_secret, hostile_text, outages_and_isolation, fairness, controls, links,
                     board_address, director_ad_hoc, install_preserves, test_push):
            case(root)
    print(f"user_notify_bridge_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
