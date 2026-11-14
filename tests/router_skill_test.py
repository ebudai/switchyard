#!/usr/bin/env python3
"""SYRD-770: an ephemeral Router routes from its skill and its project's current prompt, not its launch prompt.

The board starts each ticket on an ephemeral role with a cleared conversation, so a Router's onboarding prompt,
given only at launch, was gone from its second ticket on: it routed blind, and `role-prompt set router` never reached
it. Now:
- `switchyard-router`, a canonical skill like `switchyard-librarian`, carries the generic remit, and
  `switchyard-board` tells a router caller to load it (it used to say "stop here");
- the skill reads the project's Router prompt (what each implementer owns) from the board's declared workflow at
  every ticket.

What a model makes of the text cannot be run here. What can be: every command the skill gives, taken from the skill
itself, run as a fresh session would run it, in an environment with no onboarding prompt at all, against a real board
whose Router is declared `ephemeral`. Over two tickets, with the Director's prompt changed between them, each
session's routing follows the prompt current when it read it. A Router whose prompt cannot be read returns the ticket.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import router_role_command_test as rrc  # noqa: E402 - its real-board fixture
import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board import board_skill  # noqa: E402
from scripts.ticket_board.router_setup import ROUTER_PROMPT, with_router  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

SKILL = ROOT / "skills" / "switchyard-router" / "SKILL.md"
BOARD = ROOT / "skills" / "switchyard-board" / "SKILL.md"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def flat(text: str) -> str:
    return " ".join(text.split())


def bash_blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", text, re.S)


# -- the skill, its projection and its pointers -------------------------------------------------------------------
def test_the_skill_is_canonical_and_the_routers_alone() -> None:
    check(board_skill.ROUTER_SKILL in board_skill.CANONICAL_SKILLS and board_skill.ROUTER_SKILL.role_only == "router",
          "switchyard-router is a canonical skill, scoped to the router role")
    check([s.name for s in board_skill.skills_for_role("router")] == ["switchyard-board", "switchyard-router"],
          "the Router is pointed at the shared skill and its own")
    check([s.name for s in board_skill.skills_for_role("ROUTER")] == ["switchyard-board", "switchyard-router"],
          "whatever the case of its name")
    for name in ("director", "librarian", "main", "app", "ops", "audit", "router-ish", ""):
        check(board_skill.ROUTER_SKILL not in board_skill.skills_for_role(name), f"{name!r} is not pointed at it")
    with tempfile.TemporaryDirectory(prefix="syrd770-skill.") as home:
        paths = dict(board_skill.adapter_paths(Path(home), board_skill.ROUTER_SKILL))
        check(sorted(paths) == sorted(runtime for runtime, _ in board_skill.SKILL_ADAPTERS)
              and all(str(p).endswith("switchyard-router/SKILL.md") for p in paths.values()),
              f"it is installed for every supported CLI: {paths}")
        done = subprocess.run([sys.executable, str(ROOT / "scripts/switchyard-board-skill"), "install", "--home", home],
                              capture_output=True, text=True, timeout=120)
        check(done.returncode == 0 and all(p.exists() and p.read_text().startswith("---\nname: switchyard-router\n")
                                           for p in paths.values()),
              f"and the real installer puts it in every CLI's tree: {done.stdout[-400:]} {done.stderr[-400:]}")
        body = SKILL.read_text(encoding="utf-8")
        check(all(p.read_text().startswith(body.rstrip("\n")) for p in paths.values()),
              "each installed copy is the canonical body, with only its provenance footer added")


def test_the_skill_checks_the_role_before_anything_else() -> None:
    text = SKILL.read_text(encoding="utf-8")
    check(text.startswith("---\nname: switchyard-router\n"), "the frontmatter names it")
    description = rrc_frontmatter(text)["description"]
    check("ONLY when TICKET_BOARD_CALLER_ROLE is exactly router" in description and "do not follow it" in description,
          f"its description scopes it to the Router, for runtimes that read only that: {description!r}")
    head = text.split("## ", 2)[1]
    check('echo "$TICKET_BOARD_CALLER_ROLE"' in head and "If that does not print exactly `router`, stop" in head
          and "switchyard-board" in head, "its first section is the role check, and where to go if it fails")


def rrc_frontmatter(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.split("---\n", 2)[1].splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "\t")):
            fields[key.strip()] = value.strip().strip('"')
    return fields


def test_the_skill_carries_the_remit_and_nothing_else() -> None:
    text = flat(SKILL.read_text(encoding="utf-8"))
    for words in ("ticket-board-read ticket <ID> --json", "ticket-board-write router-assign <ID> --assignee <owner> "
                  "--revision <revision>", "ticket-board-write router-return <ID> --reason-file PATH --revision "
                  "<revision>", "`assignable`", "`scopes`", "eligible: false", "`revision`",
                  "`router-assign` is a capability, not a transition: it never appears in the ticket's "
                  "`workflow_actions`", "A justified return costs far less than a wrong assignment",
                  "A stale revision means", "Stop and wait for the next notification", "poll the board",
                  "fresh conversation"):
        check(words in text, f"the skill says {words!r}")
    raw = SKILL.read_text(encoding="utf-8")
    listed = raw.split("Return the ticket to the Director when:", 1)[1].split("\n\n", 2)[1]
    bullets = [flat(b) for b in listed.split("\n- ") if b.strip()]
    for reason in ("not confident", "signals conflict", "spans projects", "missing, inactive or not in `assignable`",
                   "first deliverable is not implementation", "Router prompt could not be read"):
        check(any(reason in b for b in bullets), f"the skill lists {reason!r} as a reason to return: {bullets}")
    lowered = text.lower()
    named = re.findall(r"parent|blocker|depend|order|priorit|gate|flag|hold|comment|edit|reassign|route\b", lowered)
    check(not named, f"like its prompt, it names no other operation Router could suggest or take: {named}")
    forbidden = {"ticket id": r"\b[A-Z]{2,}-\d", "port or host": r"127\.0\.0\.1|localhost|:\d{4,5}\b",
                 "home path": r"/home/|~/\.", "a project's owner": r"\b(main|app|ops) (owns|=)"}
    found = {label: re.findall(pattern, SKILL.read_text(encoding="utf-8")) for label, pattern in forbidden.items()}
    check(not any(found.values()), f"it is project-neutral, and carries no ticket id: {found}")


def test_the_skill_reads_the_current_prompt_from_the_declared_workflow() -> None:
    text = SKILL.read_text(encoding="utf-8")
    reads = [b for b in bash_blocks(text) if "/api/workflow" in b]
    check(len(reads) == 1 and "$TICKET_BOARD_URL" in reads[0] and "TICKET_BOARD_CALLER_ROLE" in reads[0]
          and "onboarding_prompt" in reads[0], f"one command reads the role's prompt from the declared workflow: {reads}")
    check("at the start of every ticket" in flat(text) and "role-prompt set" in text,
          "and it is read at every ticket, so the Director's change reaches the next one")


def test_the_shared_skill_and_the_prompt_point_the_router_at_it() -> None:
    board = BOARD.read_text(encoding="utf-8")
    head = board[:board.index("## What the environment already tells you")]
    check("If `TICKET_BOARD_CALLER_ROLE` is `router`, load `switchyard-router`" in flat(head)
          and "stop here" not in head.split("`librarian`")[0],
          "switchyard-board sends a router caller to its skill, as it does the Librarian, instead of turning it away")
    check("switchyard-router skill" in ROUTER_PROMPT and "declared workflow" in ROUTER_PROMPT,
          "the stored prompt points Router at the skill, for a session that starts with it")
    director = flat((ROOT / "skills/switchyard-director/SKILL.md").read_text(encoding="utf-8"))
    check("`switchyard-router` skill" in director and "role-prompt set router" in director and "ephemeral" in director,
          "and the Director is told where the ownership map goes")


# -- a fresh session, following only the skill, on a real board ---------------------------------------------------
class Session:
    """One fresh Router conversation: the skill's commands, run in an environment carrying no onboarding prompt."""

    def __init__(self, board: rrc.Board) -> None:
        self.env = {"PATH": f"{ROOT / 'scripts'}:{os.environ.get('PATH', '/usr/bin:/bin')}", "LANG": "C.UTF-8",
                    "HOME": os.environ.get("HOME", "/tmp"), "TICKET_BOARD_URL": board.base,
                    "TICKET_BOARD_CALLER_ROLE": "router", "TICKET_BOARD_WRITE_TOKEN": board.server.write_token}
        check("TICKET_BOARD_ROLE_ONBOARDING_PROMPT" not in self.env, "the session starts with no onboarding prompt")
        self.blocks = bash_blocks(SKILL.read_text(encoding="utf-8"))

    def run(self, script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=self.env,
                              stdin=subprocess.DEVNULL)

    def step(self, needle: str) -> str:
        found = [b for b in self.blocks if needle in b]
        check(len(found) == 1, f"the skill has one command block with {needle!r}: {found}")
        return found[0]

    def route(self, ticket: str, scratch: Path) -> str:
        """Steps 1-3 of the skill, as written: what this session read as its prompt, then its one write."""
        prompt = self.run(self.step("/api/workflow"))
        check(prompt.returncode == 0, f"step 1 runs as written: {prompt.stderr[-300:]}")
        read = self.run(self.step("ticket-board-read ticket <ID> --json").replace("<ID>", ticket))
        check(read.returncode == 0, f"step 2 runs as written: {read.stderr[-300:]}")
        revision = json.loads(read.stdout)["routing"]["revision"]
        writes = self.step("router-assign").splitlines()
        owner = re.search(r"everything goes to (\w+)", prompt.stdout)
        if owner:  # the project's prompt names the owner: assign, with the revision read
            line = next(w for w in writes if "router-assign" in w)
            command = line.replace("<ID>", ticket).replace("<owner>", owner.group(1)).replace("<revision>", revision)
        else:  # no prompt to route from: return it, saying why
            reason = scratch / f"{ticket}.reason"
            reason.write_text("This project's Router prompt could not be read, so I cannot tell who owns this.\n")
            line = next(w for w in writes if "router-return" in w)
            command = line.replace("<ID>", ticket).replace("PATH", str(reason)).replace("<revision>", revision)
        done = self.run(command)
        check(done.returncode == 0, f"step 3 runs as written: {command}: {done.stderr[-300:]}")
        return prompt.stdout


def set_router_prompt(board: rrc.Board, prompt: str | None) -> None:
    """What `switchyard role-prompt set|clear router` writes: the declared workflow with the role's prompt changed."""
    revision = int(t.psql(board.admin, "SELECT revision FROM ticket_board.workflow_configuration;"))
    document = board.document()
    for role in document["roles"]:
        if role["name"] == "router":
            if prompt is None:
                role.pop("onboarding_prompt", None)
            else:
                role["onboarding_prompt"] = prompt
    t.post_json(board.base, "/api/tickets/actions/configure_workflow",
                {"document": document, "expected_revision": revision, "dry_run": False}, caller="director")


def exercise_an_ephemeral_router(cluster, board: rrc.Board) -> None:
    t.TEST_WRITE_TOKEN = board.server.write_token
    t.post_json(board.base, "/api/tickets/actions/configure_workflow",
                {"document": validate(copy.deepcopy(rrc.SHIPPED)), "expected_revision": 0, "dry_run": False},
                caller="director")
    revision = int(t.psql(board.admin, "SELECT revision FROM ticket_board.workflow_configuration;"))
    document = with_router(board.document(), runtime="claude")
    for role in document["roles"]:
        if role["name"] == "router":
            role["ephemeral"] = True
    t.post_json(board.base, "/api/tickets/actions/configure_workflow",
                {"document": document, "expected_revision": revision, "dry_run": False}, caller="director")
    router = rrc.role(board.document(), "router")
    check(router.get("ephemeral") is True, "the Router is declared ephemeral: a fresh conversation per ticket")

    def where(ticket: str) -> str:
        return t.psql(board.admin, f"SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id = '{ticket}';")

    from scripts.ticket_board.peer_identity import read_process
    sleeper = subprocess.Popen(["sleep", "3600"])
    tickets = ("PGU-971", "PGU-972", "PGU-973")
    try:
        t.psql(board.admin, "INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, "
                            "session_dir, process_pid, process_start_time, process_uid) VALUES ('router', "
                            f"'{router['runtime']}', '{router['target']}', '/w/router', '/s/router', {sleeper.pid}, "
                            f"{read_process(sleeper.pid).start_time}, {os.getuid()});")
        for ticket in tickets:
            t.seed_postgres_ticket(board.admin, ticket, title=ticket, state="analysis", assignee="director",
                                   commit_exempt=True)
            env = {**Session(board).env, "TICKET_BOARD_CALLER_ROLE": "director"}
            routed = subprocess.run([str(ROOT / "scripts/ticket-board-write"), "route", ticket, "--state",
                                     "in_progress"], capture_output=True, text=True, timeout=60, env=env,
                                    stdin=subprocess.DEVNULL)
            check(routed.returncode == 0 and where(ticket) == "in_progress/router",
                  f"{ticket} is the Router's: {routed.stderr[-300:]} {where(ticket)}")
    finally:
        sleeper.kill()
        sleeper.wait()

    first = ROUTER_PROMPT + "\n\nOwnership on this project: everything goes to ops."
    second = ROUTER_PROMPT + "\n\nOwnership on this project: everything goes to app."
    with tempfile.TemporaryDirectory(prefix="syrd770-scratch.") as scratch:
        set_router_prompt(board, first)
        said = Session(board).route("PGU-971", Path(scratch))
        check(said.strip() == first.strip(), f"ticket one's fresh session read the current prompt: {said[-120:]!r}")
        check(where("PGU-971") == "in_progress/ops", f"and routed by it: {where('PGU-971')}")

        set_router_prompt(board, second)  # the Director's `role-prompt set router`, between tickets
        said = Session(board).route("PGU-972", Path(scratch))
        check(said.strip() == second.strip(), f"ticket two's fresh session read the changed prompt: {said[-120:]!r}")
        check(where("PGU-972") == "in_progress/app", f"and routed by the change, not the first: {where('PGU-972')}")

        set_router_prompt(board, None)
        said = Session(board).route("PGU-973", Path(scratch))
        check(said.strip() == "", f"with no prompt to read, the skill's command prints nothing: {said!r}")
        check(where("PGU-973") == "analysis/director",
              f"and the session returns the ticket to the Director rather than guess: {where('PGU-973')}")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    with temporary_cluster(prefix="syrd770-", shutdown="immediate") as cluster:
        board = rrc.Board(cluster, "router_skill", migrate=True)
        try:
            exercise_an_ephemeral_router(cluster, board)
        finally:
            board.close()
    print(f"router_skill_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
