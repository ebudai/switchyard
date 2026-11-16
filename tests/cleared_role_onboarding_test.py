#!/usr/bin/env python3
"""SYRD-788: an ephemeral role's cleared session hears its role before its ticket.

With Router set to `ephemeral: true`, the board cleared its session for SYRD-785 and delivered only "SYRD-785 -- ...
entered Implementation". The session got no Router prompt: the SessionStart hook gave the onboarding prompt only to a
`startup`, and only when the launch resumed nothing -- a cleared session (`source: "clear"`) of a pane launched
resuming one got nothing. Haiku read the line as an assignment, and implemented the ticket.

Now:
* the pane hook gives a cleared session its role's onboarding, then any active work, whatever the launch resumed;
* the listener's first notice after a clear, to a role whose job is not to build what the notice names (Router,
  the Librarian), starts with who it is and the skill to load -- the one thing agy and Hermes, which run no
  SessionStart at a clear, are told. An implementer's notice is its assignment and stays verbatim (SYRD-106).

Measured on a real Claude Code (2.1.318), offline: a fake API logs each request; the tree's real pane hook answers
SessionStart for a Router pane launched resuming a session; after `/clear` the ticket line's request carries the
Router prompt. Skipped, saying so, where Claude Code, tmux or an unprivileged user namespace is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from ticket_board_pane_env import strip_ticket_board_pane_env  # noqa: E402

strip_ticket_board_pane_env(os.environ)  # before the imports: the listener reads its project at import
for _key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
    os.environ.pop(_key)  # a role pane's own board, too: run from one, this is still the rig's project

import ephemeral_role_sessions_test as e135  # noqa: E402 - SYRD-135's listener rig
import ticket_board_notify_listener_test as lt  # noqa: E402
from ticket_board_no_repeated_skill_reminder_test import load_hook  # noqa: E402
from scripts.ticket_board.notification_session_clear import cleared_preface  # noqa: E402
from scripts.ticket_board.router_setup import with_router  # noqa: E402

CHECKS = 0
PROMPT = "ROUTER-PROMPT-788: route each ticket with one router-assign; never build it."
PROBE = ROOT / "tests" / "fixtures" / "syrd788_probe" / "probe.sh"
CLAUDE = Path("/opt/claude-code/bin/claude")


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


# -- the listener: what a cleared Router is sent ----------------------------------------------------------------------
def router_document() -> dict:
    cfg = with_router(e135.document(), runtime="claude")
    for role in cfg["roles"]:
        if role["name"] == "router":
            role["ephemeral"] = True
            role["target"] = "pgu-router:0.0"
    return cfg


class Rows(lt.FakeResult):
    def fetchall(self):
        return list(self.rows)


class RouterConnection(e135.WorkflowConnection):
    """SYRD-135's connection, with Router's live runtime row: the listener routes by those rows (SYRD-713)."""

    def execute(self, statement, params=None):
        if "role_runtime_assignments" in str(statement):
            return Rows([("router", "pgu-router:0.0", "claude"), ("ops", "pgu-ops:0.0", "codex")])
        return super().execute(statement, params)


def test_a_cleared_router_is_told_its_role_before_the_notice() -> None:
    notice = "PGU-788 -- Aborting switchyard new takes many Ctrl-C presses entered Implementation"
    comment = "PGU-788 -- a comment"
    sent: list[tuple[str, str]] = []
    conn = RouterConnection([lt.queue_row(1, "PGU-788", target_role="router", message=notice),
                                    lt.queue_row(2, "PGU-788", kind="comment", target_role="router", message=comment)],
                                   cfg=router_document())
    with lt.TemporaryStateDir() as tmp_path:
        delivered = e135.drive(conn, tmp_path=tmp_path, idle_targets=("pgu-router:0.0",), sent=sent, max_notifications=2)
    preface = cleared_preface("router")
    check(delivered == 2 and sent[:2] == [("pgu-router:0.0", "/clear"), ("pgu-router:0.0", f"{preface}\n\n{notice}")],
          f"the clear, then who it is and its skill, then the ticket's line: {sent}")
    check(preface.startswith("You are the router role, and your session was just cleared") and "switchyard-router" in preface,
          f"the preface names the role and the skill that carries its remit: {preface}")
    check(sent[2:] == [("pgu-router:0.0", comment)], f"only the first notice after the clear carries it: {sent[2:]}")


def test_an_implementers_notice_stays_its_assignment() -> None:
    sent: list[tuple[str, str]] = []
    conn = e135.WorkflowConnection([lt.queue_row(1, "PGU-501")], cfg=e135.document(ephemeral=("ops",)))
    with lt.TemporaryStateDir() as tmp_path:
        e135.drive(conn, tmp_path=tmp_path, sent=sent)
    check(sent == [("pgu-ops:0.0", "/clear"), ("pgu-ops:0.0", "New ticket for you: PGU-501 -- Queue")],
          f"an ephemeral implementer is cleared and handed its ticket verbatim (SYRD-106): {sent}")
    check("switchyard-librarian" in cleared_preface("librarian") and cleared_preface("ops") == "",
          "the Librarian is told its skill too; an implementer is told nothing extra")


# -- the hook: what a cleared session hears at SessionStart -----------------------------------------------------------
def session_start(source: str, *, role: str = "router", **environ: str):
    hook = load_hook()
    return hook, hook._session_start_additional_context_output(
        target=f"porter-{role}:0.0", payload={"source": source},
        environ={"TICKET_BOARD_CALLER_ROLE_MAP": json.dumps({f"porter-{role}": role}),
                 "TICKET_BOARD_URL": "http://127.0.0.1:9", **environ},
        resume_session_id=environ.get("TICKET_BOARD_PANE_SESSION_ID", ""))


def context(output) -> str:
    return "" if output is None else str(output["hookSpecificOutput"]["additionalContext"])


def test_a_cleared_session_hears_its_onboarding() -> None:
    hook, output = session_start("clear", TICKET_BOARD_ROLE_ONBOARDING_PROMPT=PROMPT,
                                 TICKET_BOARD_PANE_SESSION_ID="0e5dba90-launch-resumed")
    check(context(output) == f"Your router session was cleared for its next ticket.\n\n{PROMPT}",
          f"a cleared session of a pane launched resuming a session hears its role's onboarding: {context(output)!r}")
    original = hook._active_in_progress_ticket_for_role
    try:
        hook._active_in_progress_ticket_for_role = lambda *_a, **_k: {"id": "PGU-9", "title": "Work", "state": "in_progress"}
        text = context(hook._session_start_additional_context_output(
            target="porter-ops:0.0", payload={"source": "clear"},
            environ={"TICKET_BOARD_CALLER_ROLE_MAP": json.dumps({"porter-ops": "ops"}),
                     "TICKET_BOARD_URL": "http://127.0.0.1:9", "TICKET_BOARD_ROLE_ONBOARDING_PROMPT": "OPS-REMIT"}))
    finally:
        hook._active_in_progress_ticket_for_role = original
    check(text.startswith("Your ops session was cleared for its next ticket.\n\nOPS-REMIT\n\n")
          and "You have ACTIVE work: PGU-9 -- Work" in text,
          f"its onboarding first, then its active work, as a cleared pane was told before (PGU-816): {text!r}")


def test_other_starts_are_as_they_were() -> None:
    _hook, output = session_start("startup", TICKET_BOARD_ROLE_ONBOARDING_PROMPT=PROMPT)
    check(context(output) == f"Your router session started.\n\n{PROMPT}", f"a fresh start: {context(output)!r}")
    _hook, output = session_start("startup", TICKET_BOARD_ROLE_ONBOARDING_PROMPT=PROMPT,
                                  TICKET_BOARD_PANE_SESSION_ID="resumed")
    check(output is None, f"a launch that resumed its conversation keeps what it had: {context(output)!r}")
    _hook, output = session_start("resume", TICKET_BOARD_ROLE_ONBOARDING_PROMPT=PROMPT)
    check(PROMPT not in context(output), f"a resumed session is not re-onboarded: {context(output)!r}")
    _hook, output = session_start("clear")
    check(output is None, "a cleared session with no onboarding and no work: nothing")


# -- a real Claude Code, offline ------------------------------------------------------------------------------------
def probe_leftovers() -> set[str]:
    """What a probe run could leave: its socket directories, and processes naming them or its stand-in API."""
    import glob

    found = {f"dir {path}" for path in glob.glob("/tmp/s788k.*")}
    ps = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True).stdout
    found |= {f"process {line.split(None, 1)[0]}" for line in ps.splitlines()
              if "/tmp/s788k." in line or "fake_api.py 18788" in line}
    return found


def test_a_real_claude_session_hears_the_prompt_after_clear() -> None:
    missing = [name for name, ok in (("Claude Code", CLAUDE.exists()), ("tmux", shutil.which("tmux")),
                                     ("unshare", shutil.which("unshare"))) if not ok]
    if not missing and subprocess.run(["unshare", "-rn", "true"], capture_output=True).returncode != 0:
        missing.append("an unprivileged user namespace")
    if missing:
        print(f"cleared_role_onboarding_test: the real Claude Code probe is skipped: no {', '.join(missing)}")
        return
    leftovers = probe_leftovers()
    with tempfile.TemporaryDirectory(prefix="syrd788.") as raw:
        out = Path(raw) / "out"
        env = {"PATH": "/usr/bin:/bin", "HOME": raw, "LANG": "C.UTF-8"}
        subprocess.run(["bash", str(PROBE), str(out), str(ROOT)], env=env, capture_output=True, text=True, timeout=200)
        left = probe_leftovers() - leftovers
        check(not left, f"the probe leaves nothing behind: no socket directory, tmux server or stand-in API: {left}")
        # SYRD-800: cut off by its timeout mid-run, before inner.sh's own kill-server, it still cleans up after itself.
        subprocess.run(["bash", str(PROBE), str(Path(raw) / "cut"), str(ROOT)], env={**env, "SYRD788_PROBE_TIMEOUT": "4"},
                       capture_output=True, text=True, timeout=200)
        left = probe_leftovers() - leftovers
        check(not left, f"and so does a run its timeout cuts off: {left}")
        requests = []
        for line in (out / "api.jsonl").read_text().splitlines() if (out / "api.jsonl").exists() else []:
            row = json.loads(line)
            if "count_tokens" not in row["p"]:
                requests.append(json.loads(row["body"]))

    def texts(body: dict) -> list[tuple[str, str]]:
        found = []
        for message in body["messages"]:
            content = message["content"]
            for item in ([{"type": "text", "text": content}] if isinstance(content, str) else content):
                if item.get("type") == "text":
                    found.append((message["role"], item["text"]))
        return found

    check(len(requests) == 2 and any("entered Implementation" in text for _r, text in texts(requests[1])),
          f"the probe reached Claude: a request before the clear and the ticket line's after it: {len(requests)}")
    after = " ".join(text for _r, text in texts(requests[1]))
    check("Your router session was cleared for its next ticket." in after and "ROUTER-PROMPT-788" in after,
          "the ticket line's request after /clear carries the Router prompt the hook gave")
    check(not any("ROUTER-PROMPT-788" in text for _r, text in texts(requests[0])),
          "and the launch that resumed a session was not re-onboarded at its start, as before")


def main() -> int:
    for test in (test_a_cleared_router_is_told_its_role_before_the_notice, test_an_implementers_notice_stays_its_assignment,
                 test_a_cleared_session_hears_its_onboarding, test_other_starts_are_as_they_were,
                 test_a_real_claude_session_hears_the_prompt_after_clear):
        test()
    print(f"cleared_role_onboarding_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
