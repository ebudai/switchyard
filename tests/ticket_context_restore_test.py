#!/usr/bin/env python3
"""SYRD-540: a pulled ticket comes back to its author in its own conversation, proven -- or is parked.

The real listener on disposable boards built through schema.sql, rbac.sql and
(for the parity shapes) the real migrations. The provider is a recording pane:
it does what docs/pgu-816-clear-sessionstart-evidence.md recorded Claude and
Codex doing -- a `/clear` starts a new session announced by SessionStart with
`source: clear` -- and answers `/resume <id>` with SessionStart(resume, id), or
with a wrong id, a wrong source, another provider process, another worktree,
or nothing; each announcement either at once or deferred to the next prompt.
Its hook state is written by the real pane hook's own writer (stamped as the
hook stamps it: provider process and checkout), and the gate reading it is the
real one. Fresh context is only for a genuinely new ticket: nothing here may
clear, rebase or hand over returned work that is not proven back.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import logging
import re
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

# Hermetic in any pane: the board, listener and gate modules read the project, its pane targets and the
# board's address from these at IMPORT time, so a real role pane's own settings (TICKET_BOARD_PROJECT=syrd,
# its socket, its tmux) would otherwise judge these fixture boards as another project's. Dropped before
# anything below is imported, for this process only.
for _key in [key for key in os.environ
             if key.startswith(("TICKET_BOARD_", "PGU_", "SWITCHYARD_")) or key in ("TMUX", "TMUX_PANE")]:
    del os.environ[_key]

import ticket_board_write_api_test as fixture  # noqa: E402
from pull_claim_postgres_test import build  # noqa: E402
from pull_pickup_test import TARGETS, Gate, proc_entry  # noqa: E402
from pull_scheduling_policy_test import pull_document  # noqa: E402
from scripts.ticket_board import session_context  # noqa: E402
from scripts.ticket_board.notify_listener import PaneHookStateStore, TicketBoardNotifyListener  # noqa: E402
from scripts.ticket_board.peer_identity import SessionIdentity  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
HOOK = ROOT / "scripts" / "ticket-board-pane-idle-hook"


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def _hook():
    loader = importlib.machinery.SourceFileLoader("pane_idle_hook_540", str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


HOOK_MODULE = _hook()
ROLE_BY_TARGET = {target: role for role, target in TARGETS.items()}
WORKTREE = {role: f"/work/{role}" for role in TARGETS}
PUBLISHED = "a" * 40             # PGU-1's published commit
LEFT = "b" * 40                  # the author's HEAD when it left PGU-1 (PGU-2's clear)
NEWER = "c" * 40                 # where its PGU-2 work moved HEAD
IDENTITY = (4200, 6200)
RESUME_ANSWERS = {
    "ok": lambda role, wanted: ((wanted, "resume"), {}),
    "mismatch": lambda role, wanted: ((f"{role}-other", "resume"), {}),
    "wrong_source": lambda role, wanted: ((wanted, "startup"), {}),
    "other_process": lambda role, wanted: ((wanted, "resume"), {"identity": (9999, 1)}),
    "other_worktree": lambda role, wanted: ((wanted, "resume"), {"cwd": "/work/elsewhere"}),
    "no_worktree": lambda role, wanted: ((wanted, "resume"), {"checkout": False}),
    "silent": lambda role, wanted: None,
}


class Panes:
    """Recording provider panes: what each was sent, and the SessionStarts they announce."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.sent: list[tuple[str, str]] = []
        self.mode = "immediate"          # or "deferred": announce at the next prompt
        self.resume = "ok"               # a key of RESUME_ANSWERS
        self.clear_announces = True
        self.head = LEFT                 # the author's checkout
        self.identity = IDENTITY
        self.prepare = "comply"          # or "ignore", or "elsewhere": what the author does with the board's preparation request
        self.turn_cwd: dict[str, str] = {}  # a worktree other than the role's own, as a turn end reports it
        self.no_checkout_on: set[str] = set()  # sessions whose SessionStart reports no checkout
        self.pending: dict[str, tuple] = {}
        self.counter = 0

    def session_start(self, role: str, session_id: str, source: str, *, identity: tuple | None = None,
                      cwd: str | None = None, checkout: bool = True) -> None:
        pid, start = identity or self.identity
        record = {"session_id": session_id, "source": source, "at": time.time(), "pane_pid": pid, "pane_start_time": start,
                  "checkout": ({"cwd": cwd or WORKTREE[role], "branch": f"{role}-topic", "head": self.head}
                               if checkout and session_id not in self.no_checkout_on else {})}
        HOOK_MODULE._write_state(self.state_dir, TARGETS[role], "idle", source="claude.SessionStart", session_start=record)

    def turn_end(self, role: str) -> None:
        """What the hook records as a turn ends: the checkout git reports (SYRD-540)."""
        HOOK_MODULE._write_state(self.state_dir, TARGETS[role], "idle", source="claude.Stop",
                                 checkout={"cwd": self.turn_cwd.get(role, WORKTREE[role]), "branch": f"{role}-topic", "head": self.head,
                                           "at": time.time()})

    def __call__(self, target: str, text: str) -> None:
        self.sent.append((target, text))
        role = ROLE_BY_TARGET.get(target)
        if role is None or role == "director":
            return
        announce = None
        if text == "/clear" and self.clear_announces:
            self.counter += 1
            announce = ((f"{role}-s{self.counter}", "clear"), {})
        elif text.startswith("/resume "):
            announce = RESUME_ANSWERS[self.resume](role, text.split(" ", 1)[1])
        elif not text.startswith("/"):
            if role in self.pending:
                args, kwargs = self.pending.pop(role)
                self.session_start(role, *args, **kwargs)
            if text.startswith("Board: before") and self.prepare in ("comply", "elsewhere"):
                self.head = re.search(r"Return HEAD to (\w+)", text).group(1)
                if self.prepare == "elsewhere":  # the right commit, checked out in another worktree
                    self.turn_cwd[role] = "/work/elsewhere"
            self.turn_end(role)  # every prompt is a turn, and every turn ends with the checkout recorded
        if announce:
            if self.mode == "immediate":
                self.session_start(role, *announce[0], **announce[1])
            else:
                self.pending[role] = announce

    def to(self, role: str) -> list[str]:
        return [text for target, text in self.sent if target == TARGETS[role]]


def scenario(shape: str, *, author: str, mode: str, resume: str, clear_announces: bool = True,
             restart: str = "", restart_head: str = "", recover: bool = False, head: str = NEWER, published: bool = True,
             prepare: str = "comply", no_checkout_on: tuple = ()) -> dict:
    """The MEFP canary on one board: author claims T1, then T2 after T1's Audit; T1's Final Sign-Off
    kickback waits pinned; once T2 passes Audit, T1 comes back to the author. A Director comment on T1
    lands while the hand-off is pending, and the listener restarts across it."""
    session_context.BIND_SECONDS = 1.5
    session_context.CONFIRM_SECONDS = 1.5
    session_context.RESUME_WAIT_SECONDS = 0.6
    session_context.PREPARE_SECONDS = 1.5
    with temporary_cluster(prefix="syrd540-ctx.", shutdown="immediate") as cluster:
        dbname = f"ctx_{shape}"
        admin = build(cluster, dbname, shape)
        tmp = Path(tempfile.mkdtemp(prefix="syrd540-ctx."))
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            def sql(statement: str) -> str:
                return fixture.psql(admin, statement).strip()

            def act(ticket: str, operation: str, actor: str, **payload):
                return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor)

            def where(ticket: str) -> str:
                return sql(f"SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id='{ticket}';")

            cfg = pull_document()
            cfg["project"] = "pgu"
            for role in cfg["roles"]:
                if role.get("target"):
                    role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")

            state_dir = tmp / "state"
            panes = Panes(state_dir)
            panes.mode, panes.resume, panes.clear_announces, panes.prepare = mode, resume, clear_announces, prepare
            panes.no_checkout_on = set(no_checkout_on)
            gate = Gate(state_store=PaneHookStateStore(state_dir))
            gate.proc_root = tmp / "proc"
            gate.role_targets = dict(TARGETS)
            gate.busy = set()
            proc_entry(gate.proc_root, *IDENTITY)
            gate.role_identities = {author: SessionIdentity(*IDENTITY)}
            panes.session_start(author, f"{author}-launch", "startup")

            def listener() -> TicketBoardNotifyListener:
                made = TicketBoardNotifyListener(
                    conninfo=fixture.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener"),
                    sender=panes, activity_gate=gate.is_working, target_exists=lambda _t: True,
                    submission_witness=lambda *_a: True, poll_seconds=0, session_clear_settle_seconds=0,
                    project="pgu", logger=logging.getLogger("syrd540"))
                made.role_targets = dict(TARGETS)
                return made

            current = listener()

            def settle(passes: int = 6) -> None:
                for _ in range(passes):
                    current.listen_once(max_notifications=10)
                    time.sleep(0.25)

            def contexts() -> dict:
                return json.loads(sql("SELECT coalesce(json_object_agg(ticket_id || '/' || role, "
                                      "json_build_object('session', session_id, 'state', state)), '{}')::text "
                                      "FROM ticket_board.ticket_role_contexts;"))

            def restores() -> list:
                return json.loads(sql("SELECT coalesce(json_agg(json_build_object('session', session_id, 'outcome', outcome, "
                                      "'reason', reason, 'why', detail->>'reason', 'checkout', checkout_state, "
                                      "'checkout_why', checkout_detail->>'reason') ORDER BY id), '[]')::text "
                                      "FROM ticket_board.ticket_context_restores;"))

            def submit_and_pass_audit(ticket: str) -> None:
                sql(f"SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET commit_exempt = true WHERE id='{ticket}';")
                act(ticket, "submit_to_inspection", author)
                if where(ticket).startswith("inspection/"):
                    act(ticket, "inspector_sign_off", "inspector")
                act(ticket, "audit_sign_off", "audit", text="Audited.")

            for ticket in ("PGU-1", "PGU-2"):
                fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
                act(ticket, "admit", "director")
            settle()
            first = contexts().get(f"PGU-1/{author}")
            # PGU-1's own topic, as its publication recorded it.
            if published:
                sql("INSERT INTO ticket_board.publication_requests(ticket_id, requested_by, ref, commit_hash, bundle_path, state) "
                    f"VALUES ('PGU-1', '{author}', 'roles/{author}/pgu-1', '{PUBLISHED}', '/tmp/pgu-1.bundle', 'published');")
            submit_and_pass_audit("PGU-1")
            settle()
            second = contexts().get(f"PGU-2/{author}")
            panes.head = head  # where the author's PGU-2 work leaves its worktree
            panes.turn_end(author)
            for stage, step, actor in (("dat", "director_dat_sign_off", "director"), ("user_review", "user_sign_off", "user")):
                if where("PGU-1").startswith(stage + "/"):
                    act("PGU-1", step, actor, text="Accepted.")
            act("PGU-1", "director_kick_back", "director", reason="Final Sign-Off found a gap.")
            pinned = where("PGU-1")
            before_return = len(panes.to(author))
            submit_and_pass_audit("PGU-2")
            current.listen_once(max_notifications=10)  # the claim; its hand-off now waits on the restore
            act("PGU-1", "add_comment", "director", text="Director note on PGU-1.")
            current = listener()  # a restarted listener picks the work up where it was
            settle(10)
            time.sleep(session_context.CONFIRM_SECONDS + 0.3)
            settle(4)
            after = panes.to(author)[before_return:]
            seen: dict = {}
            if recover:
                # The Director's way out, through moves it already has: defer, then admit again.
                seen["parked_where"] = where("PGU-1")
                panes.resume, panes.prepare, mark = "ok", "comply", len(panes.to(author))
                act("PGU-1", "defer", "director", reason="Restore failed; retrying after the fix.")
                act("PGU-1", "route", "director")
                act("PGU-1", "admit", "director")
                settle(10)
                seen["recovered"] = panes.to(author)[mark:]
            if restart:
                # The provider restarts mid-ticket (a new process) and comes back in its launch session.
                count = len(panes.to(author))
                panes.identity = (4300, 6300)
                proc_entry(gate.proc_root, *panes.identity)
                panes.resume = restart
                if restart_head:
                    panes.head = restart_head  # the worktree moved while the provider was down
                panes.session_start(author, f"{author}-launch", "resume")
                act("PGU-1", "add_comment", "director", text="Director note after the restart.")
                settle(3)
                seen["before_registration"] = panes.to(author)[count:]
                gate.role_identities[author] = SessionIdentity(*panes.identity)  # the new process registers
                settle(6)
                time.sleep(session_context.CONFIRM_SECONDS + 0.3)
                settle(4)
                seen["restart"] = panes.to(author)[count:]
            director = [text for target, text in panes.sent if target == TARGETS["director"] and "conversation" in text]
            from urllib.request import urlopen
            with urlopen(f"{base}/api/reservations", timeout=10) as response:
                seen["advertised"] = json.loads(response.read())["pull_queue"].get("context_restore")
            revision = int(sql("SELECT revision FROM ticket_board.workflow_configuration;"))
            seen["previewed"] = app.apply_workflow(cfg, expected_revision=revision, dry_run=True,
                                                   caller_role="director")["reservation_changes"].get("context_restore")
            return {**seen, "sent": panes.sent, "first": first, "second": second, "pinned": pinned, "after": after,
                    "restores": restores(), "director": director, "where": where("PGU-1"), "contexts": contexts(),
                    "parked": sql(f"SELECT ticket_board.ticket_context_parked('PGU-1', '{author}');") == "t",
                    "traces": int(sql("SELECT count(*) FROM ticket_board.notification_trace WHERE event='enqueue' "
                                      "AND detail->>'dedupe_key' LIKE 'ticket-context-%';"))}
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(tmp, ignore_errors=True)


def handed_over(texts: list[str]) -> list[int]:
    """Where PGU-1's work reached the pane: its assignment, or any notice about it."""
    return [i for i, text in enumerate(texts) if text.startswith("PGU-1 --")]


CONFIRMED_READY = {"session": "app-s1", "outcome": "confirmed", "reason": "rework", "why": None, "checkout": "ready",
                   "checkout_why": None}


def test_the_canary_resumes_the_saved_conversation_and_hands_over_only_then() -> None:
    for mode in ("immediate", "deferred"):
        seen = scenario("fresh", author="app", mode=mode, resume="ok")
        label = f"claude, SessionStart {mode}"
        check(seen["first"] == {"session": "app-s1", "state": "bound"} and seen["second"] == {"session": "app-s2", "state": "bound"},
              f"{label}: each new ticket is bound to the session its own clear started: {seen['first']} {seen['second']}")
        check(seen["pinned"] == "ready/unassigned", f"{label}: the Final Sign-Off rework waits for its busy author: {seen['pinned']}")
        after = seen["after"]
        work = handed_over(after)
        resumed = [i for i, text in enumerate(after) if text == "/resume app-s1"]
        prepared = [i for i, text in enumerate(after) if text.startswith("Board: before PGU-1 is handed back")]
        restored = [i for i, text in enumerate(after) if text.startswith("Board: your saved conversation app-s1 for PGU-1 is restored")]
        check(resumed and prepared and restored and work and resumed[0] < prepared[0] < restored[0] < work[0]
              and "/clear" not in after,
              f"{label}: resume and its proof, then the checkout made ready, then the work -- never a clear: {after}")
        check(len(resumed) == 1 and len(prepared) == 1, f"{label}: one resume, one preparation request: {after}")
        ask = after[prepared[0]]
        check(f"checkout {PUBLISHED} (its last publication roles/app/pgu-1)" in ask and f"it is now at {NEWER}" in ask
              and "never reset, clean or drop a stash" in ask and "Do nothing else for PGU-1 yet" in ask,
              f"{label}: the preparation names the ticket's own commit and its source, where the worktree is, and asks for "
              f"nothing destructive and no work: {ask}")
        probes = [i for i, text in enumerate(after) if text == session_context.CONFIRM_PROMPT]
        if mode == "deferred":
            check(len(probes) == 1 and resumed[0] < probes[0] < prepared[0]
                  and "PGU" not in session_context.CONFIRM_PROMPT,
                  f"{label}: one confirmation prompt, naming no ticket and no work, draws the deferred SessionStart out: {after}")
        else:
            check(not probes, f"{label}: an immediate SessionStart needs no prompt: {after}")
        check(any(text.startswith("PGU-1 --") and "Implementation" in text for text in after)
              and any(text.startswith("PGU-1 --") and "new comment" in text for text in after),
              f"{label}: the assignment and the comment that waited are both delivered once it is ready: {after}")
        check(seen["restores"] == [CONFIRMED_READY],
              f"{label}: the conversation proof and the checkout readiness are both recorded: {seen['restores']}")
        check(seen["where"] == "in_progress/app" and not seen["director"] and seen["traces"] == 0 and not seen["parked"],
              f"{label}: a restore made ready is quiet: {seen['where']} {seen['director']}")
        check(after[restored[0]].endswith(f"worktree /work/app is at PGU-1's checkout {PUBLISHED} (its last publication roles/app/pgu-1)."),
              f"{label}: the hand-off says what was proved, nothing more: {after[restored[0]]}")


def test_a_checkout_already_at_the_ticket_topic_needs_no_preparation() -> None:
    seen = scenario("fresh", author="app", mode="immediate", resume="ok", head=PUBLISHED)
    after = seen["after"]
    check(not any(text.startswith("Board: before") for text in after) and handed_over(after)
          and seen["restores"] == [CONFIRMED_READY],
          f"a worktree already at the ticket's commit is ready at once, with no preparation request: {after}")


def test_a_checkout_never_made_ready_parks_and_can_be_recovered() -> None:
    seen = scenario("fresh", author="app", mode="immediate", resume="ok", prepare="ignore", recover=True)
    after = seen["after"]
    check(len([text for text in after if text.startswith("Board: before")]) == 1 and not handed_over(after)
          and not any(text.startswith("Board: your saved conversation") for text in after),
          f"an author that does not return its worktree is asked once and handed nothing: {after}")
    check(seen["parked_where"] == "in_progress/app" and len(seen["director"]) == 1
          and f"still at {NEWER}, not {PUBLISHED} in /work/app" in seen["director"][0] and "nothing in its worktree was changed" in seen["director"][0],
          f"it parks with ONE Director exception that names both commits and that the board changed nothing: {seen['director']}")
    recovered = seen["recovered"]
    order = [next((i for i, text in enumerate(recovered) if test(text)), -1) for test in (
        lambda text: text == "/resume app-s1", lambda text: text.startswith("Board: before"),
        lambda text: text.startswith("Board: your saved conversation"), lambda text: text.startswith("PGU-1 --"))]
    check(-1 not in order and order == sorted(order) and "/clear" not in recovered,
          f"defer and re-admit runs it again from the conversation up, and the work follows the ready checkout: {recovered}")
    check([(r["outcome"], r["checkout"]) for r in seen["restores"]] == [("confirmed", "failed"), ("confirmed", "ready")],
          f"each attempt records its own conversation proof and checkout outcome: {seen['restores']}")


def test_the_right_commit_in_another_worktree_is_not_ready() -> None:
    seen = scenario("fresh", author="app", mode="immediate", resume="ok", prepare="elsewhere")
    check(not handed_over(seen["after"]) and seen["parked"] and len(seen["director"]) == 1
          and [r["checkout"] for r in seen["restores"]] == ["failed"],
          f"HEAD at the ticket's commit but reported from another worktree proves nothing: it parks: {seen['after']} {seen['director']}")
    check(f"still at {PUBLISHED} in /work/elsewhere, not {PUBLISHED} in /work/app" in seen["director"][0],
          f"and the exception says which worktree it saw: {seen['director']}")


def test_without_a_publication_the_left_checkout_is_the_one_expected() -> None:
    seen = scenario("fresh", author="app", mode="immediate", resume="ok", published=False)
    ask = next((text for text in seen["after"] if text.startswith("Board: before")), "")
    check(f"checkout {LEFT} (the checkout recorded when it was left, branch app-topic)" in ask and handed_over(seen["after"])
          and seen["restores"] == [CONFIRMED_READY],
          f"with no publication, the checkout the author left PGU-1 at is required -- recorded, not invented: {ask}")
    none = scenario("fresh", author="app", mode="immediate", resume="ok", published=False, no_checkout_on=("app-s2",))
    check(not handed_over(none["after"]) and not any(text.startswith("Board: before") for text in none["after"])
          and none["parked"] and len(none["director"]) == 1 and "no checkout is recorded for it" in none["director"][0],
          f"with neither, nothing is guessed: it parks once, saying so: {none['director']} {none['after']}")


def assert_parked(seen: dict, label: str, why: str, *, resumes: int) -> None:
    after = seen["after"]
    check(not handed_over(after) and "/clear" not in after,
          f"{label}: no clear and no work -- neither the assignment nor the waiting comment -- reaches the pane: {after}")
    check(len([text for text in after if text.startswith("/resume")]) == resumes,
          f"{label}: {resumes} resume(s) sent, never retried: {after}")
    check(seen["parked"] and seen["where"] == "in_progress/app" if label.startswith("claude") else seen["parked"],
          f"{label}: the hand-off is parked: {seen['where']} parked={seen['parked']}")
    check(seen["traces"] == 1 and len(seen["director"]) == 1 and why in seen["director"][0]
          and "defer" in seen["director"][0] and "is kept" in seen["director"][0],
          f"{label}: ONE actionable Director exception with the reason and the way out: {seen['director']}")
    check(not any("was not restored" in text or "fresh conversation" in text for text in after),
          f"{label}: the author is never told to carry on fresh: {after}")


def test_an_unproven_resume_parks_and_keeps_the_saved_conversation() -> None:
    for resume, why in (("mismatch", "the pane reported session app-other (resume) instead"),
                        ("silent", "no SessionStart confirmed the resume"),
                        ("wrong_source", "session app-s1 started with source 'startup', not 'resume'"),
                        ("other_process", "another provider process"),
                        ("other_worktree", "not the ticket's worktree /work/app"),
                        ("no_worktree", "no worktree evidence: the resumed SessionStart reported none")):
        seen = scenario("fresh", author="app", mode="immediate", resume=resume)
        assert_parked(seen, f"claude, {resume}", why, resumes=1)
        check(seen["contexts"].get("PGU-1/app") == {"session": "app-s1", "state": "bound"},
              f"{resume}: PGU-1's saved binding is kept: {seen['contexts']}")
        check([r["outcome"] for r in seen["restores"]] == ["failed"] and why in (seen["restores"][0]["why"] or ""),
              f"{resume}: one failed attempt, with its reason: {seen['restores']}")
        if resume == "silent":
            check(seen["after"].count(session_context.CONFIRM_PROMPT) == 1,
                  f"silent: exactly one confirmation prompt, then the bounded failure: {seen['after']}")
    unbound = scenario("fresh", author="app", mode="immediate", resume="ok", no_checkout_on=("app-s1",))
    assert_parked(unbound, "claude, binding without a worktree", "no worktree evidence: the ticket's binding records none", resumes=1)
    neither = scenario("fresh", author="app", mode="immediate", resume="no_worktree", no_checkout_on=("app-s1",))
    assert_parked(neither, "claude, no worktree on either side", "no worktree evidence", resumes=1)
    check([r["outcome"] for r in neither["restores"]] == ["failed"],
          f"no worktree on either side is no proof of the conversation either: {neither['restores']}")


def test_a_runtime_without_a_verified_resume_parks() -> None:
    seen = scenario("fresh", author="main", mode="immediate", resume="ok")
    after = seen["after"]
    check(not any(text.startswith("/resume") for text in after) and "/clear" not in after and not handed_over(after),
          f"codex: no resume, no clear, no work: {after}")
    check(seen["parked"] and seen["traces"] == 1 and len(seen["director"]) == 1
          and "codex has no verified in-session resume" in seen["director"][0],
          f"codex: parked, ONE Director exception naming the limit: {seen['director']}")
    check(seen["contexts"].get("PGU-1/main") == {"session": "main-s1", "state": "bound"},
          f"codex: PGU-1's saved conversation main-s1 is kept, never rebound: {seen['contexts']}")


def test_the_limits_are_advertised_before_anyone_relies_on_them() -> None:
    seen = scenario("fresh", author="main", mode="immediate", resume="ok")
    for label, shown in (("the pull status", seen["advertised"]), ("the workflow preview", seen["previewed"])):
        check(sorted(shown or {}) == ["app", "main", "ops"]
              and shown["app"].startswith("automatic: claude") and shown["ops"].startswith("automatic: claude")
              and shown["main"].startswith("not automatic: codex") and "parks for the Director" in shown["main"],
              f"{label} says, per claimant, whether returned rework is restored automatically: {shown}")


def test_no_proof_is_never_a_binding_and_its_rework_parks() -> None:
    seen = scenario("fresh", author="app", mode="immediate", resume="ok", clear_announces=False)
    check(seen["contexts"].get("PGU-1/app") == {"session": None, "state": "unconfirmed"}
          and seen["contexts"].get("PGU-2/app") == {"session": None, "state": "unconfirmed"},
          f"a clear no SessionStart answers binds nothing, whatever the pane's newest record says: {seen['contexts']}")
    after = seen["after"]
    check(not any(text.startswith("/resume") for text in after) and "/clear" not in after and not handed_over(after),
          f"its rework is not 'restored' from a guess, nor started fresh: {after}")
    check(seen["parked"] and seen["traces"] == 3,
          f"the Director is told once per unconfirmed ticket and once for the parked rework: {seen['director']}")


def test_the_director_recovers_a_parked_ticket_with_moves_it_already_has() -> None:
    seen = scenario("fresh", author="app", mode="immediate", resume="silent", recover=True)
    check(seen["parked_where"] == "in_progress/app", f"parked in place first: {seen['parked_where']}")
    recovered = seen["recovered"]
    work = handed_over(recovered)
    check(recovered.count("/resume app-s1") == 1 and work and recovered.index("/resume app-s1") < work[0]
          and "/clear" not in recovered,
          f"defer and re-admit retries the restore once, and the work follows the proof: {recovered}")
    check([r["outcome"] for r in seen["restores"]] == ["failed", "confirmed"] and not seen["parked"],
          f"the retry is a new attempt, confirmed; the ticket is no longer parked: {seen['restores']}")


def test_parity_across_board_shapes() -> None:
    for shape in ("migrated", "upgraded"):
        seen = scenario(shape, author="app", mode="immediate", resume="ok")
        check(seen["restores"] == [CONFIRMED_READY] and handed_over(seen["after"]),
              f"{shape}: the canary holds on this board shape too: {seen['restores']}")


def test_a_restarted_provider_is_put_back_once_and_held_until_then() -> None:
    seen = scenario("fresh", author="app", mode="deferred", resume="ok", restart="ok")
    after = seen["restart"]
    resumed = [i for i, text in enumerate(after) if text == "/resume app-s1"]
    told = [i for i, text in enumerate(after) if "after its provider restarted" in text and "Continue PGU-1." in text]
    note = [i for i, text in enumerate(after) if text.startswith("PGU-1 --") and "new comment" in text]
    check(len(resumed) == 1 and told and note and resumed[0] < told[0] and resumed[0] < note[0],
          f"the new process is resumed into PGU-1's conversation once; nothing about PGU-1 reaches it before that: {after}")
    check(not seen["before_registration"],
          f"nothing is sent to the new process -- no resume, no notice -- before it is registered: {seen['before_registration']}")
    check([r["outcome"] for r in seen["restores"]] == ["confirmed", "confirmed"]
          and [r["reason"] for r in seen["restores"]] == ["rework", "restart"],
          f"both restores confirmed by SessionStart from the registered process: {seen['restores']}")
    failed = scenario("fresh", author="app", mode="immediate", resume="ok", restart="mismatch")
    after = failed["restart"]
    check(after.count("/resume app-s1") == 1 and not handed_over(after)
          and failed["parked"] and failed["contexts"].get("PGU-1/app") == {"session": "app-s1", "state": "bound"},
          f"a restart repair that is disproved parks too, keeping the binding, the note held back: {after}")
    moved = scenario("fresh", author="app", mode="immediate", resume="ok", restart="ok", restart_head=NEWER)
    after = moved["restart"]
    check(after.count("/resume app-s1") == 1 and not any("Continue PGU-1" in text for text in after) and not handed_over(after)
          and moved["parked"] and [(r["reason"], r["outcome"], r["checkout"]) for r in moved["restores"]][-1] == ("restart", "confirmed", "failed"),
          f"a restart whose worktree is no longer where its last turn ended is not told to continue: it parks: {after} {moved['restores']}")


FIXTURE_PANE_ROOT, FIXTURE_PANE_START = 700, 7007
#: Runs the hook's main() with its /proc reads pointed at a fixture made for this child's own pid.
#: `pane`: hook -> 700 (claude) -> 600 (tmux: server), so the pane root is 700. `none`: hook -> 1, no tmux at all.
HOOK_IN_PROC_FIXTURE = r"""
import importlib.machinery, importlib.util, json, os, sys
from pathlib import Path
hook, proc, chain, argv = sys.argv[1], Path(sys.argv[2]), sys.argv[3], json.loads(sys.argv[4])
def entry(pid, comm, ppid, start):
    (proc / str(pid)).mkdir(parents=True, exist_ok=True)
    (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} " + "0 " * 17 + f"{start} 0\n")
if chain == "pane":
    entry(os.getpid(), "python3", 700, 9001); entry(700, "claude", 600, 7007); entry(600, "tmux: server", 1, 6006)
else:
    entry(os.getpid(), "python3", 1, 9001)
loader = importlib.machinery.SourceFileLoader("pane_idle_hook_fixture", hook)
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
# The walk's /proc is a default bound at definition time: point every reader at the fixture.
for name in ("_proc_stat", "_pane_root_identity", "_background_work", "_session_start_record"):
    function = getattr(module, name)
    if function.__defaults__ and "proc_root" in function.__code__.co_varnames:
        function.__defaults__ = tuple(proc if value == module.PROC_ROOT else value for value in function.__defaults__)
    if function.__kwdefaults__ and "proc_root" in function.__kwdefaults__:
        function.__kwdefaults__ = {**function.__kwdefaults__, "proc_root": proc}
module.PROC_ROOT = proc
raise SystemExit(module.main(argv))
"""


def test_the_real_hook_records_the_session_start_and_withholds_work_from_a_resume_under_pull() -> None:
    """The hook's own main(), with /proc as a fixture, against a recording board: what SessionStart records,
    that later writes keep it, and that under a pull policy a resumed conversation is not handed the ticket."""
    import http.server
    import subprocess
    tmp = Path(tempfile.mkdtemp(prefix="syrd540-hook."))
    board = {"workflow": {"stages": [{"name": "in_progress", "terminal": False, "kind": "implementation", "owners": ["app"]}]},
             "tickets": [{"id": "PGU-7", "title": "Seven", "state": "in_progress", "assignee": "app"}]}

    class Board(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps(board).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Board)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        repo = tmp / "worktree"
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo)]
        repo.mkdir()
        subprocess.run([*git, "init", "-q", "-b", "pgu-7-topic"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "c"], check=True)
        head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        state = tmp / "state" / "pgu-app_0.0.json"
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "TICKET_BOARD_PROJECT": "pgu",
               "TICKET_BOARD_URL": f"http://127.0.0.1:{server.server_port}"}

        def run(state_name: str, source: str, payload: dict | None, chain: str = "none") -> tuple[dict, str]:
            """The hook's own main(), in a child whose /proc is a fixture: the pane-root walk sees exactly `chain`,
            never the real ancestry this suite happens to run under (a role pane under tmux, or none)."""
            argv = [state_name, "--source", source, "--target", "pgu-app:0.0", "--state-dir", str(tmp / "state"),
                    "--session-dir", str(tmp / "sessions"), "--stdin-timeout", "2"]
            done = subprocess.run([sys.executable, "-B", "-c", HOOK_IN_PROC_FIXTURE, str(HOOK), str(tmp / f"proc-{chain}"),
                                   chain, json.dumps(argv)],
                                  input=json.dumps(payload) if payload else "", text=True, check=True, timeout=60,
                                  capture_output=True, env=env, cwd=str(tmp))
            return json.loads(state.read_text()), done.stdout

        def start(source: str, chain: str = "none") -> tuple[dict, str]:
            return run("idle", "claude.SessionStart", {"session_id": "s-new", "source": source, "cwd": str(repo),
                                                       "hook_event_name": "SessionStart"}, chain)

        proved, _ = start("clear", chain="pane")
        record = proved.get("last_session_start") or {}
        check((record.get("pane_pid"), record.get("pane_start_time")) == (FIXTURE_PANE_ROOT, FIXTURE_PANE_START),
              f"a SessionStart under a pane root (its parent the tmux server) is stamped with that process: {proved}")
        started, said = start("clear")
        record = started.get("last_session_start") or {}
        check(record.get("session_id") == "s-new" and record.get("source") == "clear"
              and record.get("checkout") == {"cwd": str(repo), "branch": "pgu-7-topic", "head": head}
              and "pane_pid" not in record and "pane_start_time" not in record,
              f"the SessionStart is recorded with the checkout git reports, and no process it cannot prove: {started}")
        for state_name, source, payload in (("busy", "claude.UserPromptSubmit", {"session_id": "s-new"}),
                                            ("idle", "claude.Stop", {"session_id": "s-new", "background_tasks": [],
                                                                     "cwd": str(repo)})):
            later, _ = run(state_name, source, payload)
            check(later.get("last_session_start") == started["last_session_start"],
                  f"{source} keeps the latest SessionStart: {later}")
        ended = (later.get("last_checkout") or {})
        check({key: ended.get(key) for key in ("cwd", "branch", "head")} == {"cwd": str(repo), "branch": "pgu-7-topic", "head": head}
              and float(ended.get("at") or 0) > 0,
              f"a turn end records the checkout git reports, with when: {later}")
        kept, _ = run("busy", "claude.UserPromptSubmit", {"session_id": "s-new"})
        check(kept.get("last_checkout") == later.get("last_checkout"), f"and the next write keeps it: {kept}")
        check("PGU-7" in said, f"without a pull policy a clear is told its active work, as before: {said!r}")
        _, said = start("resume")
        check("PGU-7" in said, f"without a pull policy a resume is told its active work, as before: {said!r}")
        board["workflow"]["scheduling"] = {"mode": "pull", "ready_stage": "ready", "release_after": "audit"}
        _, said = start("resume")
        check("PGU-7" not in said and "ACTIVE" not in said,
              f"under pull a resumed conversation is handed nothing until the board has proven it: {said!r}")
        _, said = start("clear")
        check("PGU-7" in said, f"under pull the clear for a new ticket still starts it: {said!r}")
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_binding_needs_a_fresh_clear_of_a_session_nobody_else_holds() -> None:
    """The two proof rules on their own, against the real SQL: no stale start, no relabelled session."""
    import psycopg
    from types import SimpleNamespace
    with temporary_cluster(prefix="syrd540-bind.", shutdown="immediate") as cluster:
        admin = build(cluster, "bind", "fresh")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "bind", fixture.SERVICE_ROLE))
        cfg = pull_document()
        cfg["project"] = "pgu"
        for role in cfg["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
        for ticket in ("PGU-1", "PGU-2", "PGU-3"):
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="in_progress", assignee="app", commit_exempt=True)
        tmp = Path(tempfile.mkdtemp(prefix="syrd540-bind."))
        try:
            gate = Gate(state_store=PaneHookStateStore(tmp / "state"))
            gate.role_targets = dict(TARGETS)
            gate.role_identities = {"app": SessionIdentity(11, 12)}
            listener = SimpleNamespace(role_runtimes={"app": "claude"}, sender=lambda *_a: None, logger=logging.getLogger("bind"))

            def start(session_id: str, at: float, identity: tuple = (11, 12)) -> None:
                HOOK_MODULE._write_state(tmp / "state", TARGETS["app"], "idle", source="claude.SessionStart",
                                         session_start={"session_id": session_id, "source": "clear", "at": at,
                                                        "pane_pid": identity[0], "pane_start_time": identity[1]})

            with psycopg.connect(fixture.conninfo(cluster.socket_dir, cluster.port, "bind", "ticket_board_listener"),
                                 autocommit=True) as conn:
                def clear(ticket: str) -> None:
                    conn.execute("SELECT ticket_board.record_role_session_clear(%s, 'app')", (ticket,))

                def bound(ticket: str) -> str:
                    row = conn.execute("SELECT coalesce(session_id, state) FROM ticket_board.ticket_role_contexts "
                                       "WHERE ticket_id=%s AND role='app'", (ticket,)).fetchone()
                    value = row[0] if row else "-"
                    return value.decode() if isinstance(value, bytes) else value

                # A clear-sourced start from a minute before the clear is not this clear's.
                start("s-stale", time.time() - 60)
                clear("PGU-1")
                session_context.bind_pass(listener, conn, gate, time.time())
                check(bound("PGU-1") == "-", f"a stale SessionStart binds nothing: {bound('PGU-1')}")
                start("s-1", time.time())
                session_context.bind_pass(listener, conn, gate, time.time())
                check(bound("PGU-1") == "s-1", f"the clear's own SessionStart binds: {bound('PGU-1')}")
                # A second clear within the skew, the pane still showing PGU-1's session.
                clear("PGU-2")
                session_context.bind_pass(listener, conn, gate, time.time())
                check(bound("PGU-2") == "-", f"a session already proven another ticket's is never relabelled: {bound('PGU-2')}")
                session_context.bind_pass(listener, conn, gate, time.time() + session_context.BIND_SECONDS + 1)
                check(bound("PGU-2") == "unconfirmed", f"and without its own proof it is recorded unconfirmed: {bound('PGU-2')}")
                # A fresh clear-sourced start, but announced by another provider process than the registered one.
                clear("PGU-3")
                start("s-3", time.time(), identity=(77, 78))
                session_context.bind_pass(listener, conn, gate, time.time())
                check(bound("PGU-3") == "-", f"a SessionStart from another process binds nothing: {bound('PGU-3')}")
                # Two passes judging the same open attempt from one stale read: the prompt goes once.
                row = conn.execute("SELECT ticket_board.open_ticket_context_restore('PGU-1', 'app', 's-1', 'rework', '{}'::jsonb)").fetchone()
                attempt = session_context._json(row[0])["attempt"]
                prompts: list = []
                listener.sender = lambda _target, text: prompts.append(text)
                for _ in range(2):
                    check(session_context.judge(listener, conn, gate, attempt, None, time.time() + 100) == "pending",
                          "an unanswered resume is still pending at its prompt")
                check(prompts == [session_context.CONFIRM_PROMPT],
                      f"the confirmation prompt is recorded once, so a stale read never sends a second: {prompts}")
                # The same for the checkout preparation: a confirmed attempt, judged twice from one stale read.
                conn.execute("SELECT ticket_board.resolve_ticket_context_restore(%s, 'confirmed', '{}'::jsonb)", (attempt["id"],))
                row = conn.execute("SELECT to_jsonb(r) FROM ticket_board.ticket_context_restores r WHERE id=%s", (attempt["id"],)).fetchone()
                confirmed = session_context._json(row[0])
                HOOK_MODULE._write_state(tmp / "state", TARGETS["app"], "idle", source="claude.Stop",
                                         checkout={"cwd": "/work/app", "head": NEWER, "at": time.time()})
                status = {"topic": {"commit": PUBLISHED, "ref": "roles/app/pgu-1"},
                          "context": {"evidence": {"session_start": {"checkout": {"cwd": "/work/app"}}}}}
                prompts.clear()
                for _ in range(2):
                    check(session_context.checkout_ready(listener, conn, gate, confirmed, status, time.time()) == "wait",
                          "a worktree elsewhere waits for its preparation")
                check(len(prompts) == 1 and prompts[0].startswith("Board: before PGU-1"),
                      f"the preparation request is recorded once, so a stale read never sends a second: {prompts}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"ticket_context_restore_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
