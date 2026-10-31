#!/usr/bin/env python3
"""One host bridge that sends the User's high-value board events to a phone (SYRD-714).

`switchyard-user-notify.service` runs `serve` as root: one process for every registered tenant, never one per tenant.
Each pass, for every project root has registered (`/etc/switchyard/projects`), in an order that rotates:

1. **Read** at most EVENT_LIMIT events after its cursor from `GET /api/user-events`, over the board's own Unix
   socket as root's unit names it and the kernel proves (`board_identity.board_get_for`). The board records those
   events itself, from triggers, in the same transaction as the change (pgu1021); this reads that record and nothing
   else, so it never sees, acknowledges, suppresses or changes a role pane's notification. A board that does not
   answer is that tenant's problem alone: the others are read as usual.
2. **Record** each event in the outbox (`user_notify_store`) under its stable key, as `pending` or as `suppressed`
   with why: notifications disabled, the project or the kind opted out, another operator's blocker, or already
   resolved when read. Every field is checked first; an event whose ticket is not of that project's prefix is
   suppressed, never linked. A Director's ad hoc notification (`director_ad_hoc`) is checked again as the board
   checked it, and against the digest of the preview the Director confirmed, and at most AD_HOC_PER_HOUR an hour
   from one board are sent; it goes at high priority only when the operator's `director_urgent` allows it.
3. **Notice an outage**: a board whose unit has `failed`, or that is running and has not answered for
   `outage_minutes`, is one `project_outage` event per outage.
4. **Send** what is due, fairly across tenants (`Outbox.due`), through the configured transport
   (`user_notify_pushover`), reading the credential at each send. A provider or Tailscale outage only delays the
   phone: nothing here writes to a board, so board work never waits on it.

A push is advisory. Opening, dismissing or receiving it signs nothing off, acknowledges nothing and authorizes
nothing: the link opens the board's own page, which decides what the person there may do.

Commands (writes are root's): `status`, `configure [...] [--apply]`, `enable`, `disable`, `drain`, `test`,
`set-credential [--from-fd N]`, `install [--apply]`, `render-unit`, `pass`, `serve`. See docs/user-notifications.md.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

if __name__ == "__main__" and not __package__:
    # Run as a program -- `python3 -I -B .../scripts/user_notify.py` -- so the release root goes on the path and
    # every module is imported, once, from that exact release (as board_supervisor does).
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.dont_write_bytecode = True

from scripts import board_supervisor_spec  # noqa: E402
from scripts import user_notify_pushover as pushover  # noqa: E402
from scripts import user_notify_setup as setup  # noqa: E402
from scripts import user_notify_store as store  # noqa: E402
from scripts.ticket_board import board_identity, director_notice, phone_text  # noqa: E402
from scripts.ticket_board import privileged_operations  # noqa: E402,F401 -- board_identity's trusted-release check

EVENT_LIMIT = 20
SEND_PER_PASS = 10
PASS_SECONDS = 30.0
SERVE_CHECK_SECONDS = 300.0
ANSWER_LIMIT = 64 << 10
HOST_PROJECT = "_host"
BOARD_KINDS = ("user_review", "operator_blocker", "operation_failed", "director_ad_hoc")
#: A board's own limit is 5 an hour; past this many in an hour the bridge sends no more from that board.
AD_HOC_PER_HOUR = 10
DETAIL = re.compile(r"[a-z0-9_-]{0,64}")
STAMP = re.compile(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}")


class Refused(RuntimeError):
    pass


def default_reader(project: str, path: str) -> Any:
    return board_identity.board_get_for(project)("", path, ANSWER_LIMIT)


def default_board_state(project: str, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                        supervisor: board_supervisor_spec.Paths | None = None) -> str:
    """`failed`, `active` or anything else systemd (or the board supervisor, for a supervised board) says."""
    supervisor = supervisor or board_supervisor_spec.Paths()
    if os.path.lexists(supervisor.tenant_record(project)):
        try:
            tenants = json.loads(supervisor.status_path().read_text()).get("tenants") or []
        except (OSError, ValueError, AttributeError):
            return "unknown"
        state = next((t.get("state") for t in tenants if isinstance(t, dict) and t.get("project") == project), "")
        return {"failed": "failed", "running": "active"}.get(str(state), str(state) or "unknown")
    result = runner(["systemctl", "show", "-p", "ActiveState", "--value", board_identity.unit_name(project)],
                    capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def log_line(**fields: Any) -> None:
    print(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fields}, sort_keys=True), flush=True)


def checked_events(answer: Any, project: str, prefix: str, after: int) -> tuple[int, list[dict[str, Any]]]:
    """(latest, events) from a board's answer, every field checked; raises Refused for an answer not of that form."""
    if not isinstance(answer, dict) or type(answer.get("latest")) is not int or answer["latest"] < 0:
        raise Refused("the board's user-events answer is not of the expected form")
    events = answer.get("events")
    if not isinstance(events, list) or len(events) > 50:
        raise Refused("the board's user-events answer is not of the expected form")
    checked = []
    for event in events:
        if not isinstance(event, dict) or type(event.get("id")) is not int or event["id"] <= after:
            raise Refused("the board's user-events answer holds an event out of order")
        kind, ticket, key = event.get("kind"), event.get("ticket"), event.get("key")
        detail = event.get("detail")
        stamp = key.rsplit(":", 1)[-1] if isinstance(key, str) else ""
        good = (kind in BOARD_KINDS and isinstance(ticket, str) and isinstance(detail, str)
                and DETAIL.fullmatch(detail) and isinstance(key, str) and len(key) <= 200
                and key == f"{kind}:{ticket or '-'}:{event['id']}:{stamp}" and STAMP.fullmatch(stamp)
                and type(event.get("standing")) is bool and (bool(ticket) or kind == "director_ad_hoc"))
        foreign = good and bool(ticket) and not re.fullmatch(rf"{re.escape(prefix)}-[0-9]{{1,9}}", ticket)
        notice = ad_hoc_notice(event, project, prefix) if good and not foreign and kind == "director_ad_hoc" else {}
        problem = ("an event not of the expected form" if not good else "not this project's ticket" if foreign
                   else notice.get("problem", ""))
        checked.append({"id": event["id"], "key": key if good else f"malformed:{event['id']}",
                        "kind": kind if good else "malformed", "ticket": ticket if good and not foreign else "",
                        "detail": detail if good else "", "standing": bool(event.get("standing")),
                        "title": notice.get("title") or (
                            pushover.clean_text(event.get("title"), pushover.TEXT_LIMIT) if good else ""),
                        "message": notice.get("message", ""), "requester": notice.get("requester", ""),
                        "digest": notice.get("digest", ""), "problem": problem})
    return answer["latest"], checked


def ad_hoc_notice(event: dict[str, Any], project: str, prefix: str) -> dict[str, str]:
    """A Director's notice from the board, checked as the board checked it, and against the digest the Director
    confirmed; 'problem' says why it is not sent."""
    notice = event.get("notice")
    if not isinstance(notice, dict):
        return {"problem": "a Director's notification with no notice"}
    role, pid, generation = notice.get("requester_role"), notice.get("requester_pid"), notice.get("requester_generation")
    if not (isinstance(role, str) and setup.NAME.fullmatch(role) and type(pid) is int and type(generation) is int
            and type(notice.get("urgent")) is bool and notice["urgent"] == (event["detail"] == "urgent")):
        return {"problem": "a Director's notification not of the expected form"}
    fields = {k: notice.get(k) for k in ("title", "message", "urgent", "reason")}
    try:
        shown = director_notice.preview(project, prefix, {**fields, "ticket": event["ticket"]})
    except ValueError as exc:
        return {"problem": f"a Director's notification the board should have refused ({exc})"}
    if shown["digest"] != notice.get("digest"):
        return {"problem": "not the notification the Director confirmed (its digest differs)"}
    return {"title": fields["title"], "message": fields["message"], "digest": shown["digest"],
            "requester": f"{role} pid {pid} generation {generation}", "problem": ""}


def allowed(config: setup.Config, project: str, kind: str, detail: str = "") -> str:
    """'' when this event may go to the phone now; otherwise why not."""
    if config.problem:
        return config.problem
    if not config.ready:
        return "not configured (operator and base_url)"
    if not config["enabled"]:
        return "notifications are disabled"
    if config["projects"].get(project) is False:
        return f"project {project} is opted out"
    if kind in config["events"] and not config["events"][kind]:
        return f"{kind} notifications are off"
    if kind == "operator_blocker" and detail != config["operator"]:
        return f"operator:{detail} is not the configured operator"
    return ""


class Bridge:
    def __init__(self, paths: setup.Paths, *, outbox: store.Outbox | None = None,
                 reader: Callable[[str, str], Any] = default_reader,
                 address: Callable[[str], tuple[int, str]] = board_identity.unit_board_address,
                 board_state: Callable[[str], str] = default_board_state,
                 transport: Any = None, serve_status: Callable[[], dict[str, Any]] = setup.serve_status,
                 clock: Callable[[], float] = time.time, log: Callable[..., None] = log_line) -> None:
        self.paths, self.reader, self.address, self.board_state = paths, reader, address, board_state
        self.outbox = outbox or store.Outbox(paths.outbox, clock=clock)
        self.transport = transport or pushover.Pushover()
        self.serve_status, self.clock, self.log = serve_status, clock, log
        self.turn = 0
        self._served: tuple[float, dict[str, Any] | str] | None = None

    # -- reading ------------------------------------------------------------------------------------------------------
    def pass_once(self) -> dict[str, Any]:
        config = setup.load_config(self.paths)
        projects = setup.registered(self.paths)
        if projects:
            start = self.turn % len(projects)
            projects = projects[start:] + projects[:start]
        self.turn += 1
        read = {project: self.read_tenant(project, config) for project in projects}
        self.outbox.expire()
        sent = self.send_due(config) if not allowed(config, HOST_PROJECT, "test") else []
        return {"read": read, "sent": sent}

    def read_tenant(self, project: str, config: setup.Config) -> str:
        try:
            port, prefix = self.address(project)
            after = self.outbox.tenant(project)["after"]
            answer = self.reader(project, f"/api/user-events?after={after or 0}&limit={EVENT_LIMIT}")
            if answer is None or (isinstance(answer, dict) and answer.get("available") is False):
                self.outbox.ingest(project, 0, [], lambda _e: ("suppressed", ""),
                                   error="its board release predates user events (SYRD-714); upgrade it")
                return "predates"
            latest, events = checked_events(answer, project, prefix, after or 0)
        except Exception as exc:  # one tenant's failure is its own: report it and read the next
            reason = pushover.redact(pushover.clean_text(exc, 300))
            since = self.outbox.note_failure(project, reason)
            self.log(event="read_failed", project=project, detail=reason)
            self.notice_outage(project, config, since)
            return "failed"

        def decide(event: dict[str, Any]) -> tuple[str, str]:
            if event["problem"]:
                return "suppressed", event["problem"]
            why = allowed(config, project, event["kind"], event["detail"])
            if why:
                return "suppressed", why
            if event["kind"] == "director_ad_hoc" and self.outbox.recorded_since(
                    project, "director_ad_hoc", self.clock() - 3600) >= AD_HOC_PER_HOUR:
                return "suppressed", f"more than {AD_HOC_PER_HOUR} Director notifications from {project} this hour"
            return ("pending", "") if event["standing"] else ("suppressed", "resolved before it was read")

        added = self.outbox.ingest(project, latest, events, decide)
        if added:
            self.log(event="recorded", project=project, count=added)
        return "ok"

    def notice_outage(self, project: str, config: setup.Config, since: float) -> None:
        state = self.board_state(project)
        minutes = int((self.clock() - since) // 60)
        if state != "failed" and not (state in ("active", "activating", "reloading")
                                      and minutes >= config["outage_minutes"]):
            return
        why = allowed(config, project, "project_outage")
        seq = self.outbox.add(project, f"project_outage:{project}:{int(since)}", "project_outage",
                              detail=str(minutes), state="suppressed" if why else "pending", reason=why)
        if seq is not None:
            self.log(event="outage", project=project, board_state=state, minutes=minutes)

    # -- sending ------------------------------------------------------------------------------------------------------
    def served(self, config: setup.Config) -> dict[str, Any] | str:
        now = self.clock()
        if self._served is None or now - self._served[0] >= SERVE_CHECK_SECONDS:
            try:
                self._served = (now, self.serve_status())
            except Exception as exc:  # no Tailscale: messages go without links
                self._served = (now, f"tailscale serve status unavailable: {pushover.clean_text(exc, 200)}")
        return self._served[1]

    def link_for(self, config: setup.Config, project: str, ticket: str) -> tuple[str, str]:
        """(the link, '') or ('', why there is none)."""
        if project == HOST_PROJECT or not config["base_url"]:
            return "", "no private board link"
        try:
            port, prefix = self.address(project)
        except Exception as exc:
            return "", pushover.clean_text(exc, 200)
        status = self.served(config)
        why = status if isinstance(status, str) else setup.served(status, config["base_url"], port)
        target = setup.link(config["base_url"], port, prefix, ticket)
        return (target, "") if not why and target else ("", why or "not this project's ticket")

    def send_due(self, config: setup.Config) -> list[str]:
        try:
            credential = setup.read_credential(self.paths)
        except setup.SetupError as exc:
            self.log(event="not_sending", detail=str(exc))
            return []
        return [self.send(row, config, credential) for row in self.outbox.due(SEND_PER_PASS)]

    def send(self, row: dict[str, Any], config: setup.Config, credential: dict[str, str]) -> str:
        why = "" if row["kind"] == "test" else allowed(config, row["project"], row["kind"], row["detail"])
        if why:
            return self.outbox.finish(row["seq"], "suppressed", f"not sent: {why}")
        target, _missing = self.link_for(config, row["project"], row["ticket"])
        if row["kind"] == "director_ad_hoc" and not row["ticket"]:
            target = ""  # as its preview said (director_notice.preview: link "none"): a Director's note links a ticket
        urgent = row["kind"] == "director_ad_hoc" and row["detail"] == "urgent" and config["director_urgent"]
        message = pushover.message_for({**row, "project": "switchyard" if row["project"] == HOST_PROJECT
                                        else row["project"]}, link=target, urgent=urgent)
        attempt = self.outbox.attempt(row["seq"])
        outcome = self.transport.send(message, credential)
        detail = pushover.redact(outcome.detail, tuple(credential.values()))
        state = self.outbox.finish(row["seq"], outcome.kind, detail, outcome.receipt)
        self.log(event="send", project=row["project"], key=row["key"], kind=row["kind"], attempt=attempt,
                 outcome=outcome.kind, state=state, detail=detail, receipt=outcome.receipt, linked=bool(target))
        return state


# -- commands -------------------------------------------------------------------------------------------------------
def _root() -> None:
    if os.geteuid() != 0:
        raise Refused("this changes root's notification state: run it as root (sudo)")


def status_text(paths: setup.Paths, bridge: Bridge) -> str:
    config = setup.load_config(paths)
    values = config.values
    lines = [f"switchyard-user-notify: {'enabled' if values['enabled'] else 'disabled'}; provider {values['provider']}; "
             f"operator {values['operator'] or '(none)'}; base {values['base_url'] or '(none)'}"]
    if config.problem:
        lines.append(f"  {config.problem}")
    try:
        setup.read_credential(paths)
        lines.append(f"credential: present ({paths.credential})")
    except setup.SetupError as exc:
        lines.append(f"credential: {exc}")
    lines.append("events: " + ", ".join(f"{k} {'on' if v else 'off'}" for k, v in values["events"].items())
                 + f"; outage after {values['outage_minutes']} min")
    muted = sorted(p for p, on in values["projects"].items() if not on)
    lines.append(f"projects opted out: {', '.join(muted) or 'none'}")
    lines.append("outbox: " + ", ".join(f"{state} {n}" for state, n in bridge.outbox.counts().items()))
    known = {t["project"]: t for t in bridge.outbox.tenants()}
    lines.append("tenants:")
    for project in setup.registered(paths):
        tenant = known.get(project) or {}
        health = (f"failing since {time.strftime('%Y-%m-%d %H:%M', time.localtime(tenant['failing_since']))}"
                  if tenant.get("failing_since") else "ok" if tenant.get("ok_at") else "not read yet")
        _link, why = bridge.link_for(config, project, "")
        lines.append(f"  {project}: cursor {tenant.get('after', '-')}, {health}"
                     + (f" ({tenant['error']})" if tenant.get("error") else "")
                     + f"; link: {'private board link served' if not why else why}")
    dead = bridge.outbox.recent("dead")
    lines.append(f"dead letters ({len(dead)} latest):" if dead else "dead letters: none")
    lines += [f"  #{r['seq']} {r['project']} {r['ticket'] or '-'} {r['kind']}: {r['reason']}" for r in dead]
    return "\n".join(lines)


def configure(paths: setup.Paths, args: argparse.Namespace, bridge: Bridge, out: Callable[[str], None]) -> int:
    current = setup.load_config(paths)
    values = json.loads(json.dumps(current.values))
    for name in ("provider", "operator", "base_url"):
        if getattr(args, name) is not None:
            values[name] = getattr(args, name)
    if args.outage_minutes is not None:
        values["outage_minutes"] = args.outage_minutes
    for table, settings in (("events", args.event), ("projects", args.project)):
        for setting in settings or []:
            name, _, value = setting.partition("=")
            if value not in ("on", "off"):
                raise Refused(f"--{table[:-1]} takes NAME=on or NAME=off, not {setting!r}")
            values[table][name] = value == "on"
    values = setup.validate(values)
    out(("switchyard-user-notify: configuration" + ("" if args.apply else " (preview; nothing was changed)") + "\n"
         + json.dumps(values, indent=2, sort_keys=True)))
    preview = setup.Config(values)
    for project in setup.registered(paths):
        _link, why = bridge.link_for(preview, project, "")
        out(f"  {project}: {'private board link served' if not why else why}")
    if args.apply:
        _root()
        setup.save_config(paths, values)
        out(f"written: {paths.config}")
    return 0


def set_enabled(paths: setup.Paths, bridge: Bridge, enabled: bool, out: Callable[[str], None]) -> int:
    _root()
    values = setup.load_config(paths).values
    setup.save_config(paths, {**values, "enabled": enabled})
    drained = 0 if enabled else bridge.outbox.drain("drained: notifications were disabled")
    out(f"switchyard-user-notify: {'enabled' if enabled else 'disabled'}"
        + ("" if enabled else f"; {drained} pending message(s) drained, and new events are recorded, not sent"))
    return 0


def send_test(paths: setup.Paths, bridge: Bridge, out: Callable[[str], None]) -> int:
    _root()
    config = setup.load_config(paths)
    credential = setup.read_credential(paths)
    seq = bridge.outbox.add(HOST_PROJECT, f"test:{time.time_ns()}", "test", state="pending")
    state = bridge.send(bridge.outbox.row(seq), config, credential)
    if state == "pending":  # a test is asked for now, or not at all: never retried behind the operator's back
        state = bridge.outbox.finish(seq, "dead", f"test not retried: {bridge.outbox.row(seq)['reason']}")
    row = bridge.outbox.row(seq)
    out(f"switchyard-user-notify: test {state}: {row['reason']}" + (f" (receipt {row['receipt']})" if row["receipt"] else ""))
    return 0 if state == "sent" else 1


def set_credential(paths: setup.Paths, args: argparse.Namespace, out: Callable[[str], None]) -> int:
    _root()
    if args.from_fd is not None:
        with os.fdopen(args.from_fd, "r", encoding="utf-8", closefd=False) as source:
            lines = source.read(4096).splitlines()
    elif sys.stdin.isatty():
        lines = [getpass.getpass("Pushover application token: "), getpass.getpass("Pushover user key: ")]
    else:
        lines = sys.stdin.read(4096).splitlines()
    setup.save_credential(paths, lines)
    out(f"switchyard-user-notify: credential written to {paths.credential} (root only); "
        "it is used from the next message, with no restart")
    return 0


def serve(bridge: Bridge) -> int:
    stopping: list[int] = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda number, _frame: stopping.append(number))
    bridge.log(event="started", pid=os.getpid())
    while not stopping:
        try:
            bridge.pass_once()
        except Exception as exc:  # a pass that fails is logged, and the next one runs
            bridge.log(event="pass_failed", detail=pushover.redact(pushover.clean_text(exc, 300)))
        deadline = time.monotonic() + PASS_SECONDS
        while not stopping and time.monotonic() < deadline:
            time.sleep(1.0)
    bridge.log(event="stopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard-user-notify",
                                     description="Send the User's high-value board events to a phone (SYRD-714).")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="configuration, credential presence, tenants, outbox and dead letters")
    configure_parser = commands.add_parser("configure", help="preview a configuration change; --apply writes it")
    configure_parser.add_argument("--provider", choices=setup.PROVIDERS)
    configure_parser.add_argument("--operator", help="whose operator:<name> blockers are sent")
    configure_parser.add_argument("--base-url", dest="base_url", help="https://<machine>.<tailnet>.ts.net")
    configure_parser.add_argument("--event", action="append", metavar="KIND=on|off", help=", ".join(setup.KINDS))
    configure_parser.add_argument("--project", action="append", metavar="SLUG=on|off")
    configure_parser.add_argument("--outage-minutes", dest="outage_minutes", type=int)
    configure_parser.add_argument("--apply", action="store_true")
    for name, text in (("enable", "start sending"), ("disable", "stop sending, and drain what is pending"),
                       ("drain", "drop every pending message without sending it"),
                       ("test", "send one test notification now"), ("render-unit", "print the service unit"),
                       ("pass", "run one bridge pass"), ("serve", "run the bridge (the service does this)")):
        commands.add_parser(name, help=text)
    credential = commands.add_parser("set-credential", help="store or rotate the Pushover token and user key: "
                                                            "two lines on stdin, a prompt, or --from-fd")
    credential.add_argument("--from-fd", dest="from_fd", type=int)
    install = commands.add_parser("install", help="write and start the service; --apply runs it")
    install.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None, *, paths: setup.Paths | None = None, bridge: Bridge | None = None,
         out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    paths = paths or setup.Paths()
    if args.command == "render-unit":
        out(setup.render_unit().rstrip("\n"))
        return 0
    if args.command == "install":
        return setup.install(paths, apply=args.apply, out=out)
    try:
        if args.command == "set-credential":
            return set_credential(paths, args, out)
        bridge = bridge or Bridge(paths)
        if args.command == "status":
            out(status_text(paths, bridge))
            return 0
        if args.command == "configure":
            return configure(paths, args, bridge, out)
        if args.command in ("enable", "disable"):
            return set_enabled(paths, bridge, args.command == "enable", out)
        if args.command == "drain":
            _root()
            out(f"switchyard-user-notify: {bridge.outbox.drain('drained by the operator')} pending message(s) drained")
            return 0
        if args.command == "test":
            return send_test(paths, bridge, out)
        if args.command == "pass":
            out(json.dumps(bridge.pass_once(), sort_keys=True))
            return 0
        return serve(bridge)
    except (Refused, setup.SetupError, OSError) as exc:
        out(f"switchyard-user-notify: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
