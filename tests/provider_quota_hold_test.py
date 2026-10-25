#!/usr/bin/env python3
"""SYRD-644: a provider out of quota gets one bounded hold, from fresh evidence only.

A role whose provider account has exhausted its usage quota cannot work, and
until now every path kept telling it and the Director so: delivery retried,
unresolved-turn prompts and idle nudges went to the role, escalations to the
Director -- on stale text, again and again. Now the listener takes the
provider's own evidence, tied to the turn it answered, and holds the role:

* Claude Code: its StopFailure hook (the real hook program, fed the payload
  Claude Code 2.1.288 sends and a transcript carrying the rejected quota, as
  measured in tests/fixtures/syrd644_provider_quota_evidence.json);
* Codex: the measured quota screens, read by the real gate through tmux.

This runs on a production-built board, with the real listener, gate and hook
files; only tmux and the providers are simulated. Production's grace (120 s)
and recheck (30 min) are scaled to seconds through their environment, and
reset times are a few seconds ahead, so the clock runs for real. The cases:

* Claude with a reset time: one hold, one Director notice, nothing to the role
  or about it while it lasts -- not a hand-over, not a repair prompt, not an
  escalation -- however many passes and turn ends go by. After reset plus
  grace: released, exactly one attempt, the old failure still in the record
  and not renewing it; the attempt fails afresh, and only that renews it; a
  normal turn end then ends it.
* Codex with no reset time: a bounded recheck, not an indefinite hold; the old
  quota line on the screen after the release does not renew it, a new turn's
  does.
* Two Claude roles on one account: two holds, one Director notice.
* Ended at once by a provider switch (a new registration), a new provider
  session, and an operator clear; a listener restart keeps a current hold and
  cannot resurrect a lapsed one.
* Not quota: overloaded, an authentication failure, Codex's capacity error and
  its API key's out-of-credit line hold nothing.
* Only the registered process's evidence, by pid AND start time: a StopFailure
  record or a Codex turn from another pid, or from the same pid started at
  another time (a reused pid), is not looked at, and the board refuses such
  evidence too. A replacement registered while its predecessor's quota failure
  -- a Claude record, or a Codex turn and its quota line -- is still there is
  not held for it. A hold naming another start is replaced.
* Claude's time-only reset already past in its zone is the next day's.
* Pull pickup (SYRD-539's rig): a held worker, registered, live and idle, is
  neither claimed for nor named idle capacity, and is claimed for once the hold
  ends.
* Status: who, which provider, the evidence time, the reset or recheck time,
  and how to clear it -- the provider's own words, no prompt.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import codex_capacity_retry_test as c594  # noqa: E402
import ticket_board_write_api_test as fixture  # noqa: E402
from assigned_implementation_queue_test import build_board, workflow  # noqa: E402
from scripts.ticket_board import notify_listener as nl  # noqa: E402
from scripts.ticket_board import provider_quota  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
BEFORE = "d2c80f23c452a010faf811ee8f10d4daa331c46e"  # main before SYRD-644
EVIDENCE = json.loads((ROOT / "tests/fixtures/syrd644_provider_quota_evidence.json").read_text())
HOOK = ROOT / "scripts/ticket-board-pane-idle-hook"
GRACE, RECHECK = 1.0, 4.0
TARGETS = {"director": "pgu-director:0.0", "main": "pgu-main:0.0", "app": "pgu-app:0.0", "ops": "pgu-ops:0.0"}
RUNTIMES = {"director": "claude", "main": "codex", "app": "claude", "ops": "claude"}


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


#: A pane: a process the board registers, under a process named `tmux: server`, as tmux runs one (blocked_pane_alert_test).
TMUX_SERVER = """
import json, os, signal, subprocess, sys, time
open('/proc/self/comm', 'w').write('tmux: server')
pane = subprocess.Popen(json.loads(sys.argv[1]), start_new_session=True)
open(sys.argv[2], 'w').write(str(pane.pid))
signal.signal(signal.SIGTERM, lambda *_: (pane.terminate(), sys.exit(0)))
while True:
    try:
        while os.waitpid(-1, os.WNOHANG)[0]: pass
    except ChildProcessError: pass
    time.sleep(0.1)
"""
#: The provider in that pane: it runs the commands it is handed, as its own children -- its hooks.
PANE = """
import json, subprocess, sys, time
from pathlib import Path
ctl = Path(sys.argv[1])
while True:
    for request in sorted(ctl.glob('run-*.json')):
        job = json.loads(request.read_text())
        request.unlink()
        done = subprocess.run(job['argv'], input=job['stdin'], text=True, capture_output=True, env=job['env'])
        request.with_suffix('.part').write_text(json.dumps([done.returncode, done.stderr[-2000:]]))
        request.with_suffix('.part').replace(request.with_suffix('.done'))  # whole, or not there
    time.sleep(0.02)
"""


class Provider:
    """One provider generation of a pane: the registered process, and the hooks it runs."""

    def __init__(self, root: Path) -> None:
        self.ctl = Path(tempfile.mkdtemp(prefix="pane.", dir=root))
        self.server = subprocess.Popen([sys.executable, "-c", TMUX_SERVER, json.dumps([sys.executable, "-c", PANE, str(self.ctl)]),
                                        str(self.ctl / "pid")])
        end = time.time() + 10
        while not (self.ctl / "pid").exists() or not (self.ctl / "pid").read_text().strip():
            assert time.time() < end, "the pane never started"
            time.sleep(0.02)
        self.pid = int((self.ctl / "pid").read_text())
        self.ticks = int(Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        self.started = time.time()
        self.runs = 0

    def run(self, argv: list[str], stdin: str, env: dict[str, str]) -> None:
        self.runs += 1
        request = self.ctl / f"run-{self.runs:04d}.json"
        tmp = request.with_suffix(".tmp")
        tmp.write_text(json.dumps({"argv": argv, "stdin": stdin, "env": env}))
        tmp.replace(request)
        done = request.with_suffix(".done")
        end = time.time() + 30
        while not done.exists():
            assert time.time() < end, f"the pane never ran {argv}"
            time.sleep(0.02)
        code, err = json.loads(done.read_text())
        assert code == 0, err

    def stop(self) -> None:
        self.server.terminate()
        self.server.wait()


def codex_screen(name: str, failures: int = 1) -> dict:
    """The measured screen, with `failures` failed turns drawn one under another, as Codex draws them
    (each turn's prompt, then its error: SYRD-594's capacity-twice capture)."""
    rows = EVIDENCE["codex"]["screens"][name]["rows"]
    turn = next(i for i, row in enumerate(rows) if row.startswith("› say hi"))
    prompt = max(i for i, row in enumerate(rows) if row.startswith("› Ask Codex"))
    block = [row for row in rows[turn:prompt] if row.strip()] + [""]
    drawn = rows[:turn] + block * failures + rows[prompt:prompt + 4]
    drawn = drawn[-len(rows):] if len(drawn) > len(rows) else drawn + [""] * (len(rows) - len(drawn))
    y = max(i for i, row in enumerate(drawn) if row.startswith("› Ask Codex"))
    return {"rows": drawn, "joined": drawn, "cursor": [2, y]}


class Rig:
    def __init__(self, cluster, state_dir: Path, session_dir: Path) -> None:
        self.admin = build_board(fixture, cluster, ROOT, "board")
        self.app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                          database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        self.server = fixture.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = self.server.write_token
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        self.app.apply_workflow(workflow(ROOT), expected_revision=0, dry_run=False, caller_role="director")
        self.state_dir, self.session_dir = state_dir, session_dir
        self.store = nl.PaneHookStateStore(state_dir)
        shown = c594.screens()
        self.root = state_dir.parent
        self.providers = {role: Provider(self.root) for role in TARGETS}
        self.spawned = list(self.providers.values())  # every generation, stopped at close
        self.panes = {role: c594.Pane(shown["director"] if RUNTIMES[role] == "claude" else codex_screen("plus-none"),
                                      p.pid, p.started) for role, p in self.providers.items()}
        self.panes["main"].screen = shown["recovered"]
        self.tmux, self.table = c594.fake_tmux({TARGETS[r]: pane for r, pane in self.panes.items()})
        for role in TARGETS:
            self.register(role)
            self.store.write(TARGETS[role], "idle", source=f"{RUNTIMES[role]}.Stop", now=time.time() - 600)
        self.sends: list[tuple[float, str, str]] = []
        self.codex_failures = 0
        self.down: set[str] = set()  # panes the listener cannot reach: no attempt can go to them
        self.mode = {"app": "ok", "ops": "ok", "main": "ok"}
        self.gate = self.new_gate()

    # -- the providers ---------------------------------------------------------------------------------------------
    def register(self, role: str, provider: Provider | None = None) -> int:
        if provider is not None:
            self.providers[role] = provider
            self.spawned.append(provider)
            self.panes[role].pid, self.panes[role].started = provider.pid, provider.started
        p = self.providers[role]
        previous = self.sql(f"SELECT coalesce((SELECT generation FROM ticket_board.role_runtime_assignments WHERE role = '{role}'), 0);")
        return int(self.sql(
            f"SELECT generation FROM ticket_board.register_role_runtime('{role}', '{RUNTIMES[role]}', '{TARGETS[role]}', '/tmp', "
            f"'/tmp', {p.pid}, {p.ticks}, {os.getuid()}, {previous});"))

    def claude_failure(self, role: str, error: str = "rate_limit", *, reset_in: float | None = 4.0, session: str = "s-1",
                       quota: bool = True, resets_at: int | None = None) -> None:
        """Claude Code's StopFailure, through the real hook program: its payload, and the transcript it points at."""
        record = dict(EVIDENCE["claude"]["subscription_records"][0])
        reset = resets_at if resets_at is not None else int(time.time() + reset_in) if reset_in else None
        record["quotaLimits"] = ({**record["quotaLimits"], "resetsAt": reset}
                                 if quota else None)
        transcript = self.state_dir.parent / f"{role}-{time.time_ns()}.jsonl"
        transcript.write_text(json.dumps({"type": "user", "message": {"content": "PRIVATE PROMPT"}}) + "\n" + json.dumps(record) + "\n")
        payload = {**EVIDENCE["claude"]["stopfailure_payload_api_key"], "error": error, "session_id": session,
                   "transcript_path": str(transcript), "last_assistant_message": record["message"]["content"][0]["text"]}
        # Run by the pane's own provider, as Claude Code runs its hooks: the record names that process.
        self.providers[role].run([sys.executable, str(HOOK), "busy", "--source", "claude.StopFailure", "--target", TARGETS[role],
                                  "--state-dir", str(self.state_dir), "--session-dir", str(self.session_dir)],
                                 json.dumps(payload), {"PATH": os.environ["PATH"], "HOME": str(self.state_dir.parent)})

    def turn(self, role: str, *, at: float | None = None) -> None:
        """A Codex turn starting, through the real hook program run by the pane's provider: the hook stamps the turn
        with that process. `at` backdates it."""
        self.providers[role].run([sys.executable, str(HOOK), "busy", "--source", "codex.UserPromptSubmit", "--target", TARGETS[role],
                                  "--state-dir", str(self.state_dir), "--session-dir", str(self.session_dir)],
                                 json.dumps({"session_id": "c-1", "hook_event_name": "UserPromptSubmit"}),
                                 {"PATH": os.environ["PATH"], "HOME": str(self.state_dir.parent)})
        path = self.state_dir / (TARGETS[role].replace(":", "_") + ".json")
        state = json.loads(path.read_text())
        assert state.get("turn_process", {}).get("pane_pid") == self.providers[role].pid, f"the hook stamped the turn: {state}"
        if at is not None:
            state["turn_started_at"] = state["updated_at"] = at
            path.write_text(json.dumps(state))

    def sender(self, target: str, message: str) -> dict:
        role = next(r for r, t in TARGETS.items() if t == target)
        self.sends.append((time.monotonic(), role, message))
        if role == "director":
            self.store.write(target, "busy", source="claude.UserPromptSubmit")
            self.store.write(target, "idle", source="claude.Stop")
            return {}
        if role == "main":
            self.turn(role)
        else:
            self.store.write(target, "busy", source=f"{RUNTIMES[role]}.UserPromptSubmit")
        if self.mode[role] == "ok":
            if role == "main":
                self.panes[role].screen = c594.screens()["recovered"]
            self.store.write(target, "idle", source=f"{RUNTIMES[role]}.Stop")
        elif role == "main":  # the provider refuses this new turn: Codex draws one more quota line, and runs no Stop hook
            self.codex_failures += 1
            self.panes[role].screen = codex_screen(self.mode[role], self.codex_failures)
        else:
            self.claude_failure(role, reset_in=self.mode[role] if isinstance(self.mode[role], float) else 4.0)
        return {}

    # -- the board and the listener --------------------------------------------------------------------------------
    def new_gate(self):
        return nl.PaneActivityGate(state_store=self.store, cursor_position_runner=self.tmux, capture_pane_runner=self.tmux,
                                   pane_pid_runner=self.tmux, process_table_reader=self.table, sleeper=lambda _s: None,
                                   # Production's 120 s stale-Codex rewrite, scaled like the rest: a Codex turn that
                                   # ended on an error says busy until it.
                                   director_startup_hold_seconds=0, stale_codex_busy_hook_seconds=1, role_runtimes=RUNTIMES)

    def passes(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            listener = nl.TicketBoardNotifyListener(
                conninfo=self.listener_url, project="pgu", sender=self.sender, activity_gate=self.gate.is_working,
                target_exists=lambda target: target in TARGETS.values() and target not in {TARGETS[r] for r in self.down},
                poll_seconds=0,
                submission_confirm_seconds=0.2, submission_poll_seconds=0.05, idle_stall_grace_seconds=1.0)
            listener.unresolved_turn_grace_seconds = 2
            listener.delivery_submitter = lambda _target: None
            listener.listen_once(max_notifications=100)
            time.sleep(0.25)

    def sql(self, statement: str) -> str:
        return fixture.psql(self.admin, statement).strip()

    def act(self, ticket: str, action: str, caller: str = "director", expect: int = 200, **body):
        return fixture.post_json(self.base, f"/api/tickets/{ticket}/actions/{action}", body, caller=caller, expect=expect)

    def op(self, operation: str, caller: str = "director", expect: int = 200, **body):
        return fixture.post_json(self.base, f"/api/tickets/actions/{operation}", body, caller=caller, expect=expect)

    def note(self, role: str, **evidence) -> dict:
        """note_provider_quota as the listener calls it, with the role's registration unless `evidence` overrides it."""
        registration = json.loads(self.sql(
            "SELECT row_to_json(a)::text FROM (SELECT runtime, generation, process_pid AS pid, process_start_time AS start_time "
            f"FROM ticket_board.role_runtime_assignments WHERE role = '{role}') a;"))
        body = json.dumps({**registration, "source": "claude.StopFailure", "message": "", **evidence}).replace("'", "''")
        return json.loads(self.sql(f"SET ROLE ticket_board_listener;\n"
                                   f"SELECT ticket_board.note_provider_quota('{role}', '{body}'::jsonb)::text;").splitlines()[-1])

    def hold(self, role: str) -> dict:
        row = self.sql(f"SELECT coalesce(row_to_json(h)::text, '{{}}') FROM (SELECT * FROM ticket_board.provider_quota_holds "
                       f"WHERE role = '{role}' ORDER BY id DESC LIMIT 1) h;")
        return json.loads(row or "{}")

    def status(self) -> dict:
        with urllib.request.urlopen(self.base + "/api/provider-holds", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def to(self, role: str, since: float) -> list[str]:
        return [m for at, r, m in self.sends if r == role and at >= since]

    def about(self, role: str, since: float) -> list[str]:
        return [m for at, r, m in self.sends if r == "director" and at >= since and role in m]

    def start(self, ticket: str, role: str) -> None:
        fixture.seed_postgres_ticket(self.admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
        self.act(ticket, "route", state="in_progress", assignee=role)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        for provider in self.spawned:
            provider.stop()


def test_parsing_the_providers_own_words() -> None:
    import datetime
    for record in EVIDENCE["claude"]["subscription_records"]:
        observed = datetime.datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).timestamp()
        text = record["message"]["content"][0]["text"]
        found = provider_quota.classify_claude("rate_limit", record["quotaLimits"], text, observed=observed)
        check(found["kind"] == "quota" and found["reset_at"] == record["quotaLimits"]["resetsAt"]
              and provider_quota.parse_claude_reset(text, observed) == record["quotaLimits"]["resetsAt"],
              f"Claude's {record['quotaLimits']['rateLimitType']} limit: the reset in its words, in its zone, is resetsAt: {found}")
        bare = provider_quota.classify_claude("rate_limit", {"status": "rejected"}, text, observed=observed)
        check(bare["reset_at"] == record["quotaLimits"]["resetsAt"], f"without resetsAt the text still gives it: {bare}")
    api = EVIDENCE["claude"]["transcript_error_api_key"]
    check(provider_quota.classify_claude("rate_limit", None, api["message"]["content"][0]["text"], observed=0)["kind"] == "rate_limit"
          and provider_quota.classify_claude("overloaded", None, "", observed=0)["kind"] == "capacity"
          and provider_quota.classify_claude("authentication_failed", None, "", observed=0)["kind"] == "auth"
          and provider_quota.classify_claude("billing_error", None, "", observed=0)["kind"] == "billing"
          and provider_quota.classify_claude("server_error", None, "", observed=0)["kind"] == "server",
          "a throttle without a rejected quota, an overload, an auth or billing failure, a server error: not quota")
    tz = EVIDENCE["codex"]["tz"]
    for name, screen in EVIDENCE["codex"]["screens"].items():
        found = provider_quota.codex_quota(codex_screen(name)["rows"], codex_screen(name)["cursor"][1], zone=tz)
        want = None if screen["resets_at"] is None else screen["resets_at"] - screen["resets_at"] % 60
        check(found and found["kind"] == "quota" and found["reset_at"] == want,
              f"Codex {name}: wrapped or not, the reset in the pane's zone, or none for 'try again later': {found}")
    capacity = c594.screens()["capacity"]
    billing = provider_quota.codex_quota(c594.screens()["quota"]["rows"], c594.screens()["quota"]["cursor"][1], zone=tz)
    check(provider_quota.codex_quota(capacity["rows"], capacity["cursor"][1], zone=tz) is None
          and billing["kind"] == "billing" and billing["message"].startswith("■ Quota exceeded.") and "count" not in billing,
          f"Codex's capacity error is not quota, and neither is its API key's 'Quota exceeded', credit no clock restores: {billing}")
    utc = provider_quota.codex_quota(codex_screen("pro-at")["rows"], codex_screen("pro-at")["cursor"][1], zone="UTC")["reset_at"]
    toronto = provider_quota.codex_quota(codex_screen("pro-at")["rows"], codex_screen("pro-at")["cursor"][1], zone=tz)["reset_at"]
    check(toronto - utc == 4 * 3600, f"the same words mean the zone's time: Toronto is UTC+4h later in October: {toronto - utc}")
    # A time-only reset already past in the provider's zone is the next day's; one still ahead is today's.
    from zoneinfo import ZoneInfo
    zone = ZoneInfo("America/Toronto")
    said = "You've hit your session limit · resets 10:20pm (America/Toronto)"
    late, early = (datetime.datetime(2026, 10, 24, h, 0, tzinfo=zone).timestamp() for h in (23, 21))
    check(provider_quota.parse_claude_reset(said, late) == datetime.datetime(2026, 10, 25, 22, 20, tzinfo=zone).timestamp()
          and provider_quota.parse_claude_reset(said, early) == datetime.datetime(2026, 10, 24, 22, 20, tzinfo=zone).timestamp(),
          "Claude's '10:20pm' seen at 11pm is tomorrow's 10:20pm, and seen at 9pm today's")
    # The hook's record is evidence only from the process the board registered: its pid and its start time.
    from types import SimpleNamespace
    from scripts.ticket_board import quota_hold
    record = {"source": "claude.StopFailure", "error": "rate_limit", "observed_at": 1792900000.0, "session_id": "s-1",
              "quota": {"status": "rejected", "resetsAt": 1792910000, "rateLimitType": "five_hour"}, "message": said,
              "pane_pid": 4242, "pane_start_time": 222}
    state = SimpleNamespace(provider_error=record, state="idle", source="claude.StopFailure", turn_started_at=None, last_session_start={})
    registered = {"runtime": "claude", "generation": 3, "pid": 4242, "start_time": 222}
    own = quota_hold.evidence_for(None, "app", TARGETS["app"], state, registered)
    other = quota_hold.evidence_for(None, "app", TARGETS["app"], state, dict(registered, pid=4243))
    reused = quota_hold.evidence_for(None, "app", TARGETS["app"], state, dict(registered, start_time=111))
    unnamed = quota_hold.evidence_for(None, "app", TARGETS["app"], SimpleNamespace(
        **{**vars(state), "provider_error": {k: v for k, v in record.items() if k != "pane_start_time"}}), registered)
    check(own and own["reset_at"] == provider_quota.iso(1792910000) and own["generation"] == 3 and own["start_time"] == 222
          and other is None and reused is None and unnamed is None,
          f"a StopFailure record from the registered pane process is evidence; another pid's, the same pid started at "
          f"another time (reused), or one naming no start, is not: {own} / {other} / {reused} / {unnamed}")
    shown = codex_screen("plus-none")
    gate = SimpleNamespace(cursor_position_runner=lambda *_a, **_k: SimpleNamespace(stdout=f"2 {shown['cursor'][1]} 40"),
                           capture_pane_runner=lambda *_a, **_k: SimpleNamespace(stdout="\n".join(shown["rows"])))
    turn = SimpleNamespace(provider_error=None, state="busy", source="codex.UserPromptSubmit", turn_started_at=1792900000.0,
                           last_session_start={}, turn_process={"pane_pid": 4242, "pane_start_time": 222})
    codex = dict(registered, runtime="codex")
    taken = quota_hold.evidence_for(gate, "main", TARGETS["main"], turn, codex)
    reused = quota_hold.evidence_for(gate, "main", TARGETS["main"], SimpleNamespace(
        **{**vars(turn), "turn_process": {"pane_pid": 4242, "pane_start_time": 111}}), codex)
    unnamed = quota_hold.evidence_for(gate, "main", TARGETS["main"], SimpleNamespace(**{**vars(turn), "turn_process": None}), codex)
    check(taken and taken["source"] == "codex.screen" and taken["start_time"] == 222 and reused is None and unnamed is None,
          f"the quota screen after a Codex turn the registered process took is evidence; after one the same pid started "
          f"at another time took, or one no process was named for, it is not: {taken} / {reused} / {unnamed}")


def test_a_held_worker_is_neither_pulled_for_nor_idle_capacity() -> None:
    """SYRD-539's pickup rig: an idle, registered worker whose provider is held is not claimed for, nor named idle."""
    import logging
    import shutil
    from pull_claim_postgres_test import build
    from pull_pickup_test import Gate, proc_entry
    from pull_scheduling_policy_test import pull_document
    from scripts.ticket_board.peer_identity import SessionIdentity

    targets = {role: f"pgu-{role}:0.0" for role in ("main", "app", "ops", "director", "audit", "inspector", "user")}
    with temporary_cluster(prefix="s644-pickup.", shutdown="immediate") as cluster, tempfile.TemporaryDirectory(prefix="syrd644-pickup.") as tmp:
        admin = build(cluster, "pickup", "fresh")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "pickup", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        provider = subprocess.Popen(["sleep", "600"])
        try:
            def sql(statement: str) -> str:
                return fixture.psql(admin, statement).strip()

            cfg = pull_document()
            cfg["project"] = "pgu"
            for role in cfg["roles"]:
                if role.get("target"):
                    role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
            fixture.seed_postgres_ticket(admin, "PGU-1", title="PGU-1", state="analysis", assignee="director", commit_exempt=True)
            fixture.post_json(base, "/api/tickets/PGU-1/actions/admit", {}, caller="director")
            proc, store = Path(tmp) / "proc", nl.PaneHookStateStore(Path(tmp) / "state")
            gate = Gate(state_store=store)
            gate.proc_root, gate.role_targets, gate.busy = proc, dict(targets), set()
            # app alone could take it: registered, live, idle since a minute ago. main and ops have no process.
            ticks = int(Path(f"/proc/{provider.pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
            proc_entry(proc, provider.pid, ticks)
            gate.role_identities = {"app": SessionIdentity(provider.pid, ticks)}
            generation = sql(f"SELECT generation FROM ticket_board.register_role_runtime('app', 'claude', '{targets['app']}', "
                             f"'/tmp', '/tmp', {provider.pid}, {ticks}, {os.getuid()}, 0);")
            store.write(targets["app"], "idle", source="claude.Stop", now=time.time() - 60)
            sql("INSERT INTO ticket_board.provider_quota_holds (role, runtime, generation, pid, process_start, state, evidence, "
                f"evidence_at, deadline) VALUES ('app', 'claude', {generation}, {provider.pid}, {ticks}, 'held', '{{}}', "
                "clock_timestamp(), clock_timestamp() + interval '1 hour');")
            sql("UPDATE ticket_board.ticket_notification_state SET entered_current_state_at = clock_timestamp() - interval '20 minutes' "
                "WHERE ticket_id = 'PGU-1';")
            sent: list = []
            listener = nl.TicketBoardNotifyListener(
                conninfo=fixture.conninfo(cluster.socket_dir, cluster.port, "pickup", "ticket_board_listener"),
                sender=lambda target, message: sent.append((target, message)),
                activity_gate=gate.is_working, target_exists=lambda _t: True, submission_witness=lambda *_a: True,
                poll_seconds=0, logger=logging.getLogger("syrd644-pickup"))
            listener.role_targets = dict(targets)
            listener.listen_once(max_notifications=listener.delivered_count + 5)
            listener.listen_once(max_notifications=listener.delivered_count + 5)
            where = sql("SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id = 'PGU-1';")
            idle_named = [m for t, m in sent if t == targets["director"] and "app" in m.split("claiming it:")[-1]]
            check(where == "ready/unassigned" and sql("SELECT count(*) FROM ticket_board.pull_claims;") == "0" and not idle_named
                  and gate.role_identities.get("app") == SessionIdentity(provider.pid, ticks),
                  f"held: app, registered and live, is not claimed for, and no alert names it as an idle worker: {where} {sent}")
            sql("UPDATE ticket_board.provider_quota_holds SET state = 'ended', ended_at = clock_timestamp(), end_reason = 'test' "
                "WHERE role = 'app';")
            listener.listen_once(max_notifications=listener.delivered_count + 5)
            where = sql("SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id = 'PGU-1';")
            check(where == "in_progress/app", f"the hold ended, the same worker is claimed for at once: {where}")
        finally:
            provider.kill()
            provider.wait()
            server.shutdown()
            server.server_close()


def test_holds_from_fresh_evidence_only() -> None:
    os.environ["TICKET_BOARD_QUOTA_HOLD_GRACE_SECONDS"] = str(GRACE)
    os.environ["TICKET_BOARD_QUOTA_HOLD_RECHECK_SECONDS"] = str(RECHECK)
    with temporary_cluster(prefix="s644", shutdown="immediate") as cluster, tempfile.TemporaryDirectory(prefix="syrd644.") as tmp:
        state_dir, session_dir = Path(tmp) / "state", Path(tmp) / "sessions"
        state_dir.mkdir(), session_dir.mkdir()
        rig = Rig(cluster, state_dir, session_dir)
        try:
            # --- Claude, with a reset time -------------------------------------------------------------------
            rig.start("PGU-1", "app")
            rig.mode["app"] = 7.0  # out of quota: the hand-over's turn fails, resetting 7 s from then
            t0 = time.monotonic()
            rig.passes(1.5)
            hold = rig.hold("app")
            check(hold.get("state") == "held" and hold["evidence"]["source"] == "claude.StopFailure"
                  and hold["evidence"]["limit"] == "five_hour" and hold["reset_at"],
                  f"the failed hand-over's StopFailure begins a hold, with the provider's reset: {hold}")
            notices = rig.about("app", t0)
            check(len(notices) == 1 and "out of quota" in notices[0] and "clear-provider-hold app" in notices[0]
                  and "PRIVATE PROMPT" not in notices[0],
                  f"one Director notice, saying until when and how to clear, never a prompt: {notices}")
            # While it lasts: a comment on the ticket, turn ends, passes -- nothing reaches the role or is said about it.
            mark = time.monotonic()
            rig.act("PGU-1", "add_comment", text="Director: any news?")
            rig.store.write(TARGETS["app"], "idle", source="claude.StopFailure")  # the record as the hook left it
            rig.passes(2.0)
            check(rig.to("app", mark) == [] and rig.about("app", mark) == []
                  and rig.sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE target_role = 'app' "
                              "AND last_error LIKE 'provider_quota_hold%';") != "0",
                  f"held: the comment's notice waits (requeued, not dropped), and nobody is nudged: "
                  f"{rig.to('app', mark)} {rig.about('app', mark)}")
            check(rig.sql("SELECT ticket_board.ticket_turn_is_resolved('PGU-1', clock_timestamp());") == "t",
                  "the board's one question for every reminder and escalation: app's turn is not news while it is held")
            # Only newer evidence from the registered process counts.
            import datetime
            began = datetime.datetime.fromisoformat(rig.hold("app")["evidence_at"])
            older = rig.note("app", observed_at=(began - datetime.timedelta(seconds=60)).isoformat(), turn="stopfailure:older")
            foreign = rig.note("app", observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                               turn="stopfailure:foreign", pid=1)
            reused = rig.note("app", observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                              turn="stopfailure:reused", start_time=1)
            check(older.get("stale") is True and "ignored" in foreign and "ignored" in reused
                  and rig.hold("app").get("renewals") == 0 and len(rig.hold("app").get("history", [])) == 1,
                  f"evidence older than the hold's, or from another process -- another pid, or the same pid started at "
                  f"another time -- changes nothing: {older} {foreign} {reused}")
            # An alert about app already queued when its hold began: dropped at delivery, as the hold's notice said it all.
            rig.sql("SELECT ticket_board.enqueue_notification('PGU-1', 'escalation', 'director', "
                    "'app was reminded about PGU-1 (in in_progress) and still has not advanced it -- may be stuck.', "
                    "jsonb_build_object('kind', 'escalation', 'id', 'PGU-1', 'state', 'in_progress', 'assignee', 'app', "
                    "'target_role', 'director'), 'syrd644-queued-before-the-hold');")
            mark = time.monotonic()
            rig.passes(1.0)
            check(rig.about("app", mark) == [] and rig.sql(
                      "SELECT count(*) FROM ticket_board.notification_trace WHERE event = 'drop' "
                      "AND busy_reason = 'provider_quota_hold' AND detail->>'role' = 'app';") == "1",
                  f"an escalation about the held role is dropped, by the hold: {rig.about('app', mark)}")
            # Reset plus grace: released; exactly one attempt; it fails afresh, which alone renews the hold.
            rig.mode["app"] = 6.0
            mark = time.monotonic()
            rig.passes(max(t0 + 7.0 + GRACE + 2.0 - time.monotonic(), 1.0))
            hold = rig.hold("app")
            sent = rig.to("app", mark)
            check(len(sent) == 1 and hold.get("state") == "held" and hold.get("renewals") == 1,
                  f"after reset plus grace: one attempt, which failed afresh and renewed the hold: {sent} {hold.get('state')} "
                  f"{hold.get('renewals')} {[h['event'] for h in hold.get('history', [])]}")
            # The stale record cannot renew a released hold: release it with no attempt possible (the pane busy).
            rig.down.add("app")  # unreachable: no attempt can go
            assert (rig.store.read(TARGETS["app"]).provider_error or {}).get("error") == "rate_limit", "the hook's record is there"
            rig.sql("UPDATE ticket_board.provider_quota_holds SET deadline = clock_timestamp() WHERE role = 'app' AND state = 'held';")
            rig.passes(1.0)
            stale = rig.hold("app")
            check(stale.get("state") == "released" and stale.get("renewals") == 1,
                  f"released, and the failure already in the record does not renew it: {stale.get('state')} {stale.get('renewals')}")
            window = rig.sql("SELECT concat_ws(' ', ticket_board.role_provider_hold_is_current('app', clock_timestamp()), "
                             "ticket_board.role_provider_hold_is_current('app', clock_timestamp() + interval '11 minutes'), "
                             "ticket_board.role_provider_hold_until('app', clock_timestamp()) IS NULL);")
            check(window == "t f t",
                  f"released: the attempt's window still counts for reminders, for ten minutes at most; deliveries go: {window}")
            # The provider is back; a normal turn end ends the hold.
            rig.mode["app"] = "ok"
            rig.down.discard("app")
            rig.store.write(TARGETS["app"], "idle", source="claude.Stop")
            rig.passes(0.8)
            check(rig.hold("app").get("end_reason") == "recovered", f"a normal turn end ends it: {rig.hold('app')}")

            # --- Codex, with no reset time: a bounded recheck --------------------------------------------------
            rig.start("PGU-2", "main")
            rig.mode["main"] = "plus-none"
            mark = time.monotonic()
            rig.passes(1.5)
            hold = rig.hold("main")
            check(hold.get("state") == "held" and hold["evidence"]["source"] == "codex.screen" and hold.get("reset_at") is None,
                  f"the quota line ending the screen after the hand-over's turn holds Codex, with no reset given: {hold}")
            # Nothing queued for main now: at the recheck it gets one recheck notice, and fails again on a new turn.
            mark = time.monotonic()
            rig.passes(RECHECK + 2.5)
            renewed = rig.hold("main")
            sent = rig.to("main", mark)
            check(len(sent) == 1 and "quota should have reset" in sent[0] and renewed.get("renewals") == 1,
                  f"the bounded recheck: one recheck notice, its new turn's quota line renews: {sent} {renewed.get('renewals')}")
            from scripts.ticket_board.notification_activity_hold import SELF_REMINDER_KINDS
            kinds = rig.sql("SELECT string_agg(DISTINCT kind, ',') FROM ticket_board.notification_trace WHERE event = 'enqueue' "
                            "AND detail->>'dedupe_key' LIKE 'provider-quota-recheck:%';")
            check(kinds == "ticket_update" and kinds not in SELF_REMINDER_KINDS,
                  f"the recheck is an update, which a busy pane delays and never discards as a stale self-reminder: {kinds}")
            # An old line is not new, even under a new turn the screen has not drawn yet: released, it stays released.
            rig.down.add("main")  # unreachable: no attempt goes
            rig.sql("UPDATE ticket_board.provider_quota_holds SET deadline = clock_timestamp() WHERE role = 'main' AND state = 'held';")
            rig.passes(0.6)
            rig.turn("main")  # a new turn id; the same screen
            rig.passes(1.0)
            check(rig.hold("main").get("state") == "released",
                  f"the old quota line still on the screen does not renew it: {rig.hold('main').get('state')} "
                  f"{[(h['event'], h.get('turn')) for h in rig.hold('main').get('history', [])]} {rig.to('main', mark)}")
            # A turn begun before the release and refused after it was refused before the reset: it renews nothing either.
            released_at = datetime.datetime.fromisoformat(rig.hold("main")["released_at"]).timestamp()
            rig.turn("main", at=released_at - 0.5)
            rig.codex_failures += 1
            rig.panes["main"].screen = codex_screen("plus-none", rig.codex_failures)
            rig.passes(1.0)
            check(rig.hold("main").get("state") == "released",
                  f"a quota line drawn after the release, for a turn begun before it, does not renew: {rig.hold('main')}")
            rig.down.discard("main")

            # --- ended at once: a provider switch, a new session, an operator clear ---------------------------
            rig.mode["main"] = "plus-none"
            rig.codex_failures += 1  # a new turn the provider refuses: one more quota line
            rig.panes["main"].screen = codex_screen("plus-none", rig.codex_failures)
            rig.turn("main")
            # Before any pass looks, the gate's rewrite of a Codex turn that ran no Stop (production's 120 s): the turn,
            # and the process that took it, survive it.
            rig.store.write(TARGETS["main"], "idle", source="listener.stale_codex_busy_recovery")
            rig.passes(1.0)
            check(rig.hold("main").get("state") == "held",
                  f"a newly drawn quota line, on a new turn, holds again -- read after the listener's rewrite: {rig.hold('main')}")
            rig.mode["main"] = "ok"  # the switch is to a provider that works
            rig.panes["main"].screen = c594.screens()["recovered"]  # the new process offers no quota line, nor a turn end
            rig.register("main", Provider(rig.root))  # set-role-runtime / a restart: a new registration
            window = rig.sql("SELECT concat_ws(' ', ticket_board.role_provider_hold_is_current('main', clock_timestamp()), "
                             "ticket_board.role_provider_hold_until('main', clock_timestamp()) IS NULL);")
            check(window == "f t", f"the old generation's hold stops counting the moment the new one registers: {window}")
            rig.passes(0.8)
            check(rig.hold("main").get("end_reason") == "replaced",
                  f"a new registration ends the hold: {rig.hold('main').get('end_reason')} "
                  f"{rig.sql('SELECT string_agg(id || chr(32) || state || chr(32) || generation || chr(32) || coalesce(end_reason, chr(45)), chr(124) ORDER BY id) FROM ticket_board.provider_quota_holds WHERE role = chr(109)||chr(97)||chr(105)||chr(110);')} "
                  f"gen={rig.sql('SELECT generation FROM ticket_board.role_runtime_assignments WHERE role = chr(109)||chr(97)||chr(105)||chr(110);')}")
            # A replacement registered before any pass saw its predecessor's last answer (the SessionStart race): that
            # answer -- its turn unended, its quota line still ending the screen -- is not the new process's.
            rig.down.add("main")  # no attempt goes: the screen stays as the old process left it
            rig.turn("main")
            rig.codex_failures += 1
            rig.panes["main"].screen = codex_screen("plus-none", rig.codex_failures)
            count = "SELECT count(*) FROM ticket_board.provider_quota_holds WHERE role = 'main';"
            holds, old = rig.sql(count), rig.providers["main"]
            rig.register("main", Provider(rig.root))
            old.stop()
            rig.passes(0.8)
            check(rig.sql(count) == holds and rig.hold("main").get("end_reason") == "replaced",
                  f"the old process's unended turn and quota line, still there, begin no hold for its replacement: "
                  f"{rig.sql(count)} vs {holds} {rig.hold('main')}")
            rig.down.discard("main")
            rig.panes["main"].screen = c594.screens()["recovered"]
            # The same for Claude: the old process's StopFailure record, still beside the pane, is not the new one's.
            rig.claude_failure("ops", reset_in=30.0)
            old = rig.providers["ops"]
            rig.register("ops", Provider(rig.root))
            old.stop()
            rig.passes(0.8)
            check(rig.hold("ops") == {} and (rig.store.read(TARGETS["ops"]).provider_error or {}).get("pane_pid") == old.pid,
                  f"a replacement registered beside its predecessor's quota failure is not held for it: {rig.hold('ops')}")
            rig.start("PGU-3", "ops")
            rig.mode["ops"] = 30.0
            rig.passes(1.2)
            check(rig.hold("ops").get("state") == "held", f"ops held: {rig.hold('ops')}")
            state = json.loads((state_dir / "pgu-ops_0.0.json").read_text())
            state["last_session_start"] = {"session_id": "s-new", "source": "claude.SessionStart.clear", "at": time.time()}
            (state_dir / "pgu-ops_0.0.json").write_text(json.dumps(state))
            rig.passes(0.8)
            check(rig.hold("ops").get("end_reason") == "new_session", f"a new provider session ends it: {rig.hold('ops')}")

            # --- two Claude roles on one account: two holds, one notice ----------------------------------------
            mark = time.monotonic()
            reset = int(time.time() + 30.0)
            rig.claude_failure("ops", resets_at=reset, session="s-new")
            rig.store.write(TARGETS["app"], "busy", source="claude.UserPromptSubmit")
            rig.claude_failure("app", resets_at=reset)
            # Same reset second for both: the provider's reset is the account's.
            rig.passes(1.2)
            notices = [m for at, r, m in rig.sends if r == "director" and at >= mark and "out of quota" in m]
            held = {r: rig.hold(r).get("state") for r in ("app", "ops")}
            check(held == {"app": "held", "ops": "held"} and len(notices) == 1,
                  f"each role held on its own evidence, the Director told once: {held} {notices} "
                  f"{[(r, rig.hold(r).get('notified'), rig.hold(r).get('joined_hold'), rig.hold(r).get('id')) for r in ('app', 'ops')]} "
                  f"{[m[:90] for at, r, m in rig.sends if r == 'director' and at >= mark]} "
                  f"{rig.sql('SELECT string_agg(notification_id || chr(32) || kind || chr(32) || event || chr(32) || coalesce(busy_reason, chr(45)), chr(124) ORDER BY id) FROM ticket_board.notification_trace WHERE target_role = chr(100)||chr(105)||chr(114)||chr(101)||chr(99)||chr(116)||chr(111)||chr(114) AND ts > clock_timestamp() - make_interval(secs => 10);')}")
            joined = [rig.hold(r).get("joined_hold") for r in ("app", "ops")]
            check(sum(1 for j in joined if j) == 1, f"the second joins the first's notice: {joined}")

            # --- status, and an operator clear ---------------------------------------------------------------
            status = rig.status()["holds"]
            current = [h for h in status if h["current"]]
            check({h["role"] for h in current} == {"app", "ops"}
                  and all(h["runtime"] == "claude" and h["evidence_at"] and h["deadline"] and h["reset_at"]
                          and h["clear"] == f'ticket-board-write clear-provider-hold {h["role"]} --reason "..."'
                          and "PRIVATE PROMPT" not in json.dumps(h) for h in current),
                  f"status: who, which provider, evidence time, reset, and the clear command; no prompt: {current}")
            read = subprocess.run([sys.executable, str(ROOT / "scripts/ticket-board-read"), "--board-url", rig.base, "holds"],
                                  capture_output=True, text=True, timeout=60)
            check(read.returncode == 0 and "app (claude): held until" in read.stdout and "clear: ticket-board-write clear-provider-hold app" in read.stdout,
                  f"ticket-board-read holds says it plainly: {read.stdout}{read.stderr}")
            denied = str(rig.op("clear_provider_hold", caller="app", expect=403, role="ops", reason="x"))
            rig.down.add("ops")  # nothing reaches ops for now: the only evidence left is the record the clear answered
            rig.op("clear_provider_hold", role="ops", reason="the account was topped up")
            check("app cannot call clear_provider_hold" in denied
                  and rig.hold("ops").get("end_reason") == "cleared: the account was topped up",
                  f"only the Director clears, and the reason is kept: {denied} {rig.hold('ops').get('end_reason')}")

            # --- a listener restart keeps a current hold, and cannot resurrect a lapsed one ---------------------
            rig.gate = rig.new_gate()
            rig.passes(0.6)
            check(rig.hold("app").get("state") == "held", "a new listener, the same current hold")
            assert (rig.store.read(TARGETS["ops"]).provider_error or {}).get("error") == "rate_limit", "ops's old record is there"
            check(rig.hold("ops").get("end_reason") == "cleared: the account was topped up",
                  f"the clear sticks: the failure it answered, still in ops's record, begins no new hold: {rig.hold('ops')}")
            rig.mode["ops"] = "ok"
            rig.down.discard("ops")
            # The listener is down; app's provider refuses again, then the deadline passes.
            rig.claude_failure("app", reset_in=30.0)
            time.sleep(0.05)
            rig.sql("UPDATE ticket_board.provider_quota_holds SET deadline = clock_timestamp() "
                    "WHERE role = 'app' AND state = 'held';")
            rig.down.add("app")
            rig.gate = rig.new_gate()
            rig.passes(0.8)
            check(rig.hold("app").get("state") == "released" and rig.hold("app").get("renewals") == 0,
                  f"a listener back after the deadline releases it, and a failure from before it does not bring it back: "
                  f"{rig.hold('app')}")
            rig.down.discard("app")

            # --- not quota: nothing is held -------------------------------------------------------------------
            before = rig.sql("SELECT count(*) FROM ticket_board.provider_quota_holds;")
            rig.store.write(TARGETS["ops"], "busy", source="claude.UserPromptSubmit")
            rig.claude_failure("ops", "overloaded", quota=False, session="s-new")
            rig.passes(0.6)
            rig.claude_failure("ops", "authentication_failed", quota=False, session="s-new")
            rig.passes(0.6)
            ops_state = rig.store.read(TARGETS["ops"])
            check(rig.sql("SELECT count(*) FROM ticket_board.provider_quota_holds;") == before
                  and ops_state.state == "busy" and ops_state.provider_error["error"] == "authentication_failed",
                  f"an overload or an auth failure holds nothing and leaves the pane's state as it was: {ops_state}")

            # --- a runtime reassignment ends what is left of app's hold -------------------------------------------
            check(rig.hold("app").get("state") == "released", f"app's hold is still current: {rig.hold('app')}")
            rig.sql("UPDATE ticket_board.role_runtime_assignments SET runtime = 'codex' WHERE role = 'app';")
            rig.passes(0.6)
            check(rig.hold("app").get("end_reason") == "reassigned", f"a runtime reassignment ends it: {rig.hold('app')}")

            # --- a deadline passes with no evidence left to look at: the pass still releases it --------------------------
            rig.mode["ops"] = 30.0
            rig.store.write(TARGETS["ops"], "busy", source="claude.UserPromptSubmit")
            rig.claude_failure("ops", reset_in=30.0, session="s-new")
            rig.passes(0.6)
            check(rig.hold("ops").get("state") == "held", f"ops held again, on a new failure: {rig.hold('ops')}")
            rig.down.add("ops")  # no attempt goes
            rig.store.write(TARGETS["ops"], "busy", source="claude.UserPromptSubmit")  # someone typed: the record is gone
            rig.sql("UPDATE ticket_board.provider_quota_holds SET deadline = clock_timestamp() WHERE role = 'ops' AND state = 'held';")
            rig.passes(0.6)
            check(rig.hold("ops").get("state") == "released", f"released at its deadline without any evidence: {rig.hold('ops')}")
            # A hold names its process by pid and start time too: one naming another start is not the registered
            # process's, whether evidence or a pass finds it first.
            stamp = "UPDATE ticket_board.provider_quota_holds SET process_start = process_start + 1 WHERE role = 'ops' AND state <> 'ended';"
            rig.sql(stamp)
            first = rig.hold("ops")["id"]
            began = rig.note("ops", observed_at=(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=1)).isoformat(),
                             turn="stopfailure:after-the-replacement")
            ended = rig.sql(f"SELECT end_reason FROM ticket_board.provider_quota_holds WHERE id = {first};")
            check(began.get("began") is True and ended == "replaced",
                  f"evidence finds a hold naming another process start: that hold is replaced, and a new one begins: {began} {ended}")
            rig.sql(stamp)
            rig.passes(0.6)
            check(rig.hold("ops").get("end_reason") == "replaced", f"so does a pass: {rig.hold('ops')}")
        finally:
            rig.close()


OUTAGE_SCENARIO = r"""
import json, os, subprocess, sys, tempfile, threading, time
from pathlib import Path
root, fixtures = Path(sys.argv[1]), Path(sys.argv[2])
sys.path[:0] = [str(root), str(root / "tests")]
from tmux_bus_isolation import isolate_tmux_bus
isolate_tmux_bus()
import codex_capacity_retry_test as c594
import ticket_board_write_api_test as fixture
from assigned_implementation_queue_test import build_board, workflow
from scripts.ticket_board import notify_listener as nl
from temporary_cluster import temporary_cluster
os.environ.update({"TICKET_BOARD_QUOTA_HOLD_GRACE_SECONDS": "1", "TICKET_BOARD_QUOTA_HOLD_RECHECK_SECONDS": "60"})
evidence = json.loads((fixtures / "syrd644_provider_quota_evidence.json").read_text())
rows = evidence["codex"]["screens"]["plus-none"]["rows"]
quota = {"rows": rows, "joined": rows, "cursor": [2, max(i for i, r in enumerate(rows) if r.startswith("\u203a Ask Codex"))]}
W, D = "pgu-main:0.0", "pgu-director:0.0"
with temporary_cluster(prefix="s644o", shutdown="immediate") as cluster, tempfile.TemporaryDirectory() as tmp:
    admin = build_board(fixture, cluster, root, "board")
    app = fixture.TicketBoardApp(cluster.root / "f", cluster.root / "a", project="pgu", ticket_prefix="PGU",
                                 database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
    server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
    fixture.TEST_WRITE_TOKEN = server.write_token
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")
    procs = {r: subprocess.Popen(["sleep", "300"]) for r in ("main", "director")}
    ticks = {r: int(Path(f"/proc/{p.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]) for r, p in procs.items()}
    def reg(role, runtime, target):
        fixture.psql(admin, f"SELECT ticket_board.register_role_runtime('{role}', '{runtime}', '{target}', '/tmp', '/tmp', {procs[role].pid}, {ticks[role]}, {os.getuid()}, 0);")
    reg("main", "codex", W); reg("director", "claude", D)
    store = nl.PaneHookStateStore(Path(tmp))
    shown = c594.screens()
    panes = {W: c594.Pane(shown["recovered"], procs["main"].pid, time.time()), D: c594.Pane(shown["director"], procs["director"].pid, time.time())}
    tmux, table = c594.fake_tmux(panes)
    store.write(W, "idle", source="codex.Stop", now=time.time() - 600); store.write(D, "idle", source="claude.Stop", now=time.time() - 600)
    sends = []
    def sender(target, message):
        sends.append(("main" if target == W else "director", message))
        if target == D:
            store.write(D, "busy", source="claude.UserPromptSubmit"); store.write(D, "idle", source="claude.Stop"); return {}
        store.write(W, "busy", source="codex.UserPromptSubmit"); panes[W].screen = quota  # every turn: out of quota
        state = json.loads((Path(tmp) / "pgu-main_0.0.json").read_text())  # stamped with the process that took it, as the hook does
        state["turn_process"] = {"pane_pid": procs["main"].pid, "pane_start_time": ticks["main"]}
        (Path(tmp) / "pgu-main_0.0.json").write_text(json.dumps(state))
        return {}
    gate = nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux, pane_pid_runner=tmux,
                               process_table_reader=table, sleeper=lambda _s: None, director_startup_hold_seconds=0,
                               stale_codex_busy_hook_seconds=1, role_runtimes={"main": "codex", "director": "claude"})
    fixture.seed_postgres_ticket(admin, "PGU-9", title="PGU-9", state="analysis", assignee="director", commit_exempt=True)
    fixture.post_json(base, "/api/tickets/PGU-9/actions/route", {"state": "in_progress", "assignee": "main"}, caller="director")
    end = time.monotonic() + 12
    commented = False
    while time.monotonic() < end:
        if not commented and time.monotonic() > end - 7:  # mid-outage, the Director asks on the ticket
            fixture.post_json(base, "/api/tickets/PGU-9/actions/add_comment", {"text": "Director: status?"}, caller="director")
            commented = True
        listener = nl.TicketBoardNotifyListener(conninfo=fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener"),
            project="pgu", sender=sender, activity_gate=gate.is_working, target_exists=lambda t: True, poll_seconds=0,
            submission_confirm_seconds=0.2, submission_poll_seconds=0.05, idle_stall_grace_seconds=1.0)
        listener.unresolved_turn_grace_seconds = 2
        listener.delivery_submitter = lambda _t: None
        listener.listen_once(max_notifications=100)
        time.sleep(0.25)
    server.shutdown(); server.server_close()
    for p in procs.values(): p.kill()
    resolved = fixture.psql(admin, "SELECT ticket_board.ticket_turn_is_resolved('PGU-9', clock_timestamp())::text;").strip()
    waiting = fixture.psql(admin, "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE target_role = 'main';").strip()
    print(json.dumps({"to_role": [m[:100] for r, m in sends if r == "main"], "to_director": [m[:100] for r, m in sends if r == "director"],
                      "resolved": resolved, "waiting_for_role": int(waiting)}))
"""


def outage(root: Path) -> dict:
    """A Codex role out of quota for 12 s of passes, every turn refused, and a Director comment mid-way: what reaches
    it, what waits for it, whether its ticket's turn reads resolved, and what the Director hears."""
    result = subprocess.run([sys.executable, "-c", OUTAGE_SCENARIO, str(root), str(ROOT / "tests/fixtures")],
                            capture_output=True, text=True, timeout=400, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_main_nags_an_unavailable_provider_and_this_tree_holds_it() -> None:
    import io
    import tarfile
    with tempfile.TemporaryDirectory(prefix="syrd644-before.") as tmp:
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", BEFORE, "scripts", "tests", "examples"],
                                 check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(tmp, filter="data")
        before = outage(Path(tmp))
    after = outage(ROOT)
    check(len(before["to_role"]) == 2 and "new comment" in before["to_role"][1] and before["resolved"] == "false"
          and before["waiting_for_role"] == 0,
          f"main: the Director's comment mid-outage is delivered into a provider that refuses every turn, and the "
          f"ticket's turn reads unresolved -- what lets every reminder path nag it on its schedule: {before}")
    check(after["to_role"] == [after["to_role"][0]] and after["waiting_for_role"] >= 1 and after["resolved"] == "true"
          and len(after["to_director"]) == 1 and "out of quota" in after["to_director"][0],
          f"this tree: one hand-over, then the hold -- the comment waits for it, the turn reads resolved, and the "
          f"Director is told once: {after}")


def main() -> int:
    only = set(sys.argv[1:])
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case) and (not only or name in only):
            case()
    print(f"provider_quota_hold_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
