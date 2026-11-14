#!/usr/bin/env python3
"""SYRD-739: the operator's break-glass command and the board's argument shapes, through every seam.

The board here is a stand-in for root's reads (a real SYRD-737 packet, built and validated by recovery_packet) and
for the root-only grant functions, keeping the grant's state the way pgu1040 does. tmux is role_runtime_test's stand-
in. The production board's own functions and route are proved in break_glass_live_test.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import break_glass_role as bgr, team_launcher  # noqa: E402
from scripts.ticket_board import break_glass as bg, recovery_packet as rp  # noqa: E402

import recovery_packet_test as rpt  # noqa: E402
from role_runtime_test import RuntimeRunner, _write_config  # noqa: E402

CHECKS = 0
HOLD = 4
REASON = "isolate the refused task while the Director's claude account is held"


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


class Board:
    """Root's reads and the grant functions, with pgu1040's states."""

    def __init__(self, *, refuse_open: str = "", end_state: str | None = None) -> None:
        self.packet = rp.build(rpt.raw(project="porter"))
        self.calls: list[tuple[str, tuple]] = []
        self.grant: dict = {}
        self.refuse_open = refuse_open
        self.end_state = end_state  # what the role's request leaves the grant as, when the stand-in role acts

    def get(self, path: str) -> Any:
        if path.startswith(rp.PATH):
            return copy.deepcopy(self.packet)
        if path == "/api/workflow":
            return {"document": {"roles": [{"name": "director", "runtime": "claude"}]}, "revision": 7}
        if path == "/api/runtime-assignments":
            return {"assignments": {}}
        if path == bg.PATH:
            return {"grants": [dict(self.grant)] if self.grant else []}
        raise AssertionError(path)

    def call(self, function: str, *args: tuple[Any, str]) -> Any:
        name, values = function.removeprefix("ticket_board."), tuple(v for v, _t in args)
        self.calls.append((name, values))
        if name == "open_break_glass":
            if self.refuse_open:
                raise RuntimeError(self.refuse_open)
            self.grant = {"grant": 7, "state": "open", "attempted": None, "completed": None}
            return 7
        if name == "end_break_glass":
            if self.grant["state"] in ("open", "operating"):
                self.grant["state"] = "ended" if self.grant["state"] == "operating" else values[1]
            return dict(self.grant)
        if name == "hand_back_break_glass":
            return {"handed_back": False, "route": "rebind-director", "director_hold": HOLD}
        raise AssertionError(name)

    def names(self) -> list[str]:
        return [n for n, _v in self.calls]


class Tmux(RuntimeRunner):
    """new-session starts the role; a stand-in role may end the grant on its first poll."""

    def __init__(self, board: Board, *, act: str | None = None, fail_start: bool = False, exits: bool = False):
        super().__init__(live=set())
        self.board, self.act, self.fail, self.exits = board, act, fail_start, exits
        self.started: list[list[str]] = []
        self.snapshot: dict[str, list[str]] = {}

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = [str(a) for a in args]
        self.calls.append(command)
        if command[:1] == ["rm"]:
            return subprocess.run(command, capture_output=True, text=True)
        if command[:2] == ["tmux", "new-session"]:
            self.started.append(command)
            root = Path(command[command.index("break-glass") + 1]).parent
            self.snapshot = {d: sorted(str(p.relative_to(root / d)) for p in (root / d).rglob("*") if p.is_file())
                             for d in ("home", "work")}
            self.snapshot["root"] = sorted(p.name for p in root.iterdir())
            self.snapshot["files"] = {str(p.relative_to(root)): p.read_text() for p in root.rglob("*") if p.is_file()}
            if self.fail:
                return subprocess.CompletedProcess(command, 3)
            if not self.exits:
                self.live.add(command[command.index("-s") + 1])
            if self.act:
                self.board.grant.update(state=self.act, attempted={"operation": "isolate", "args": {"ticket": "PGU-2"}},
                                        completed={"ticket": "PGU-2"})
            return subprocess.CompletedProcess(command, 0)
        if command[:2] == ["tmux", "has-session"]:
            return subprocess.CompletedProcess(command, 0 if command[-1] in self.live else 1)
        if command[:2] == ["tmux", "kill-session"]:
            self.live.discard(command[-1])
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(command, 0)


class Operator:
    def __init__(self, source: str = "pkexec", known: bool = True) -> None:
        self.source, self.known, self.name = source, known, "eric"


class Journal:
    last: "Journal | None" = None

    def __init__(self, project: str, argv: list[str], operator: str = "") -> None:
        self.argv, self.lines, self.closed = argv, [], None
        Journal.last = self

    def open(self) -> Path:
        return Path("/nonexistent")

    def write(self, stream: str, text: str) -> None:
        self.lines.append(json.loads(text))

    def close(self, *, status: str, exit_status: int | None, detail: str = "") -> dict:
        self.closed = (status, exit_status)
        return {}


class Tenant:
    admin_database_url = "postgresql:///porter_ticket_board"


class Case:
    def __init__(self, *, board: Board | None = None, problems: list[str] | None = None, **tmux: Any) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="syrd739-")
        self.root = Path(self.tmp.name)
        self.config_path = _write_config(self.root)
        self.config = team_launcher.load_project_config("porter", self.config_path)
        self.board = board or Board()
        self.tmux = Tmux(self.board, **tmux)
        self.problems = problems or []
        self.tool_problems: list[str] = []
        self.printed: list[str] = []
        self.owner_home = self.root / "owner-home"
        for relative, body in ((".codex/auth.json", "codex-credential"), (".claude/.credentials.json", "claude-credential")):
            (self.owner_home / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.owner_home / relative).write_text(body)
        self.clock = [0.0]

    def close(self) -> None:
        self.tmp.cleanup()

    def run(self, *, cli: str = "codex", allow: list[str] | None = None, ttl: int = 600, apply_it: bool = False,
            expect: str = "", euid: int = 0, operator: Operator | None = None, reason: str = REASON,
            interrupt_after: int | None = None) -> int:
        from scripts.ticket_board import rollout_journal

        polls = [0]

        def sleep(_seconds: float) -> None:
            polls[0] += 1
            self.clock[0] += bgr.POLL_SECONDS
            if interrupt_after is not None and polls[0] >= interrupt_after:
                raise KeyboardInterrupt

        original, home = rollout_journal.Attempt, team_launcher._owner_home_for_auth
        rollout_journal.Attempt = Journal  # type: ignore[assignment]
        team_launcher._owner_home_for_auth = lambda _o, *a, **k: self.owner_home  # type: ignore[assignment]
        try:
            return bgr.command(
                "porter", cli=cli, hold=HOLD, allow=allow or ["isolate", "record"], reason=reason, ttl=ttl,
                apply_it=apply_it, expect=expect, euid_getter=lambda: euid,
                operator_resolver=lambda: operator or Operator(),
                tenant_resolver=lambda slug, print_func=print: (Tenant(), self.config_path, self.config),
                board_get=self.board.get, admin=self.board, problems_for=lambda *a, **k: list(self.problems),
                tools_for=lambda *a, **k: {"bwrap": "/usr/bin/bwrap", "cli": "/opt/codex-pkg/bin/codex", "binds": [],
                                           "problems": list(self.tool_problems)},
                print_func=self.printed.append, tmux_runner=self.tmux, scratch_root=self.root / "scratch",
                board_url="http://127.0.0.1:1", client=Path("/release/scripts/ticket_board/break_glass.py"),
                sleep=sleep, monotonic=lambda: self.clock[0], token_factory=lambda: "T" * 43)
        finally:
            rollout_journal.Attempt = original  # type: ignore[assignment]
            team_launcher._owner_home_for_auth = home  # type: ignore[assignment]

    def digest(self) -> str:
        return next(l for l in reversed(self.printed) if "review           digest" in l).rsplit(" ", 1)[-1]

    def text(self) -> str:
        return "\n".join(self.printed)

    def scratch(self) -> Path:
        return self.root / "scratch" / "break-glass-7"


def test_the_argument_shapes_are_exact() -> None:
    good = {"record": {"ticket": "PGU-2", "text": "x"}, "isolate": {"ticket": "PGU-2"},
            "reassign": {"ticket": "PGU-2", "assignee": "main", "reason": "r"},
            "unblock": {"ticket": "PGU-2", "blocker": "operator:eric"}}
    for operation, args in good.items():
        check(bg.checked_args(operation, args) == args, f"{operation}: its shape passes")
    for operation, args, said in (("isolate", {"ticket": "PGU-2", "extra": "x"}, "takes exactly"),
                                  ("isolate", {"ticket": "pgu-2"}, "is not a ticket"),
                                  ("reassign", {"ticket": "PGU-2", "assignee": "Main; rm", "reason": "r"}, "is not a assignee"),
                                  ("record", {"ticket": "PGU-2", "text": "x" * 2001}, "longer than 2000"),
                                  ("record", {"ticket": "PGU-2", "text": "  "}, "non-empty"),
                                  ("merge", {}, "not a break-glass operation")):
        try:
            bg.checked_args(operation, args)
            got = ""
        except ValueError as exc:
            got = str(exc)
        check(said in got, f"{operation} {args}: refused ({got})")
    check(bg.digest("T" * 43) == __import__("hashlib").sha256(b"T" * 43).hexdigest(), "the board keeps the sha256")


def test_only_a_named_operator_through_pkexec_may_start_one() -> None:
    for kwargs, said in (({"euid": 1000}, "root's to start"), ({"operator": Operator(source="sudo")}, "elevated by sudo"),
                         ({"operator": Operator(known=False)}, "has to be authorized"), ({"reason": " "}, "needs a reason")):
        case = Case()
        try:
            status = case.run(apply_it=True, expect="x", **kwargs)
            check(status == 1 and case.board.calls == [] and case.tmux.calls == [] and said in case.text(),
                  f"{kwargs}: refused before anything: {case.text()}")
        finally:
            case.close()


def test_the_preview_and_its_refusals() -> None:
    case = Case()
    try:
        check(case.run() == 0 and case.board.calls == [] and case.tmux.calls == [],
              f"a preview writes and starts nothing: {case.board.calls}")
        text = case.text()
        check(f"safety hold {HOLD}" in text and "one operation of isolate, record (or abort)" in text
              and "only its own credential" in text and "routed to rebind-director" in text
              and case.board.packet["provenance"]["state_digest"] in text and f"--apply --expect {case.digest()}" in text,
              f"the preview says what it would do, and how to apply exactly that: {text}")
        first = case.digest()
        case.run(allow=["record", "isolate"])
        check(case.digest() == first, "the same plan previews to the same digest, whatever the --allow order")
        case.run(allow=["record"])
        check(case.digest() != first, "another operation set is another digest")
        status = case.run(apply_it=True, expect=first + "0")
        check(status == 1 and case.board.calls == [] and "is not what this would do now" in case.text(),
              "a digest that is not the preview's applies nothing")
    finally:
        case.close()
    for kwargs, said in (({"allow": ["merge"]}, "--allow takes one or more of"), ({"ttl": 30}, "--ttl is 60 to 3600")):
        case = Case()
        try:
            check(case.run(**kwargs) == 1 and said in case.text() and case.board.calls == [], f"{kwargs}: {case.text()}")
        finally:
            case.close()
    case = Case(problems=["codex is not signed in for porter (unauthenticated)"])
    try:
        check(case.run() == 1 and "cannot take a break-glass role" in case.text() and case.board.calls == [],
              f"a runtime that cannot run refuses: {case.text()}")
    finally:
        case.close()


def test_the_runtime_must_not_be_held_and_must_take_a_first_instruction() -> None:
    case = Case()
    try:
        seen: list[str] = []
        original = team_launcher._cli_auth_status
        team_launcher._cli_auth_status = lambda cli, **k: seen.append(cli) or {"codex": "authenticated",  # type: ignore
                                                                              "claude": "unauthenticated"}.get(cli, "not_installed")
        team_launcher_home = team_launcher._owner_home_for_auth
        team_launcher._owner_home_for_auth = lambda *_a, **_k: case.owner_home  # type: ignore[assignment]
        try:
            held = bgr.runtime_problems(case.config, "claude", {"claude"}, runner=lambda *a, **k: None)
            agy = bgr.runtime_problems(case.config, "agy", {"claude"}, runner=lambda *a, **k: None)
            codex = bgr.runtime_problems(case.config, "codex", {"claude"}, runner=lambda *a, **k: None)
        finally:
            team_launcher._cli_auth_status = original  # type: ignore[assignment]
            team_launcher._owner_home_for_auth = team_launcher_home  # type: ignore[assignment]
        check(held == ["the claude account is under a safety hold"] and "first instruction" in agy[0] and codex == []
              and seen == ["codex"], f"a held account is never probed, agy has no first instruction: {held} {agy} {seen}")
    finally:
        case.close()


def test_a_completed_operation_ends_everything_and_routes_the_operator() -> None:
    case = Case(act="completed")
    try:
        case.run()
        status = case.run(apply_it=True, expect=case.digest())
        check(status == 0 and case.board.names() == ["open_break_glass", "end_break_glass", "hand_back_break_glass"],
              f"open, end, hand back: {case.board.names()} {case.text()[-300:]}")
        opened = case.board.calls[0][1]
        check(opened[:5] == (HOLD, "eric", REASON, "codex", "{isolate,record}") and opened[5] == 600
              and opened[6] == bg.digest("T" * 43) and "T" * 43 not in json.dumps(opened)
              and opened[7]["state_digest"] == case.board.packet["provenance"]["state_digest"],
              f"the board gets the token's hash, never the token: {opened}")
        start = case.tmux.started[0]
        cut = start.index("break-glass")
        sandbox = start[cut + 2:]
        token_file = Path(start[cut + 1])
        def pair(flag: str, *values: str) -> bool:
            return any(sandbox[i:i + 1 + len(values)] == [flag, *values] for i in range(len(sandbox)))
        check(start[:5] == ["tmux", "new-session", "-d", "-s", "porter-break-glass"]
              and start[cut - 2:cut] == ["-c", 'exec 9<"$1" && rm -f -- "$1" && exec "${@:2}"']
              and sandbox[0] == "/usr/bin/bwrap" and pair("--tmpfs", "/home") and pair("--clearenv")
              and pair("--file", "9", bgr.SANDBOX_TOKEN) and pair("--chdir", bgr.SANDBOX_WORK)
              and pair("--ro-bind", "/release/scripts/ticket_board/break_glass.py", bgr.SANDBOX_CLIENT)
              and sandbox[-2] == "/opt/codex-pkg/bin/codex" and "grant 7" in sandbox[-1]
              and "Do not run anything else" in sandbox[-1] and "T" * 43 not in json.dumps(start),
              f"the token file is opened and deleted, then the sandbox runs the CLI with one fixed instruction: {start}")
        seen = case.tmux.snapshot
        check(seen["home"] == [".codex/auth.json", ".codex/config.toml"]
              and seen["files"]["home/.codex/auth.json"] == "codex-credential"
              and f'[projects."{bgr.SANDBOX_WORK}"]' in seen["files"]["home/.codex/config.toml"]
              and seen["work"] == ["INSTRUCTIONS.md", "recovery-packet.json"]
              and json.loads(seen["files"]["work/recovery-packet.json"]) == case.board.packet
              and f"python3 {bgr.SANDBOX_CLIENT} isolate --ticket" in seen["files"]["work/INSTRUCTIONS.md"]
              and "reassign" not in seen["files"]["work/INSTRUCTIONS.md"]
              and seen["files"]["token"].strip() == "T" * 43,
              f"its scratch: only codex's credential and trust, the packet, its instructions, and the token: {seen}")
        check(not case.scratch().exists() and "porter-break-glass" in case.tmux.sessions_killed()
              and case.board.calls[1][1] == (7, "aborted", "board") and not token_file.exists(),
              f"the session is stopped and the whole scratch, credential copy and token, removed: {case.board.calls[1]}")
        check("grant 7 completed: isolate" in case.text() and "not handed back" in case.text()
              and "rebind-director porter --cli <runtime> --hold 4" in case.text(),
              f"the operator is told the operation and, the Director being held, routed to a full replacement: "
              f"{case.text()[-400:]}")
        journal = Journal.last
        check(journal.closed == ("succeeded", 0) and [next(k for k in ("opened", "started", "ended", "handback") if k in e)
                                                      for e in journal.lines[1:]] == ["opened", "started", "ended", "handback"]
              and journal.lines[0]["plan"]["digest"] == case.digest()
              and journal.lines[3]["scratch_removed"] is True,
              f"root's journal: the plan, the grant, its start, its end and the hand-back: {journal.lines}")
    finally:
        case.close()


def test_every_other_end_stops_the_session_and_ends_the_grant() -> None:
    for kwargs, run_kwargs, end, said in (
            ({"act": "refused"}, {}, (7, "aborted", "board"), "grant 7 refused"),
            ({"exits": True}, {}, (7, "aborted", "session_ended"), "grant 7 aborted: no operation"),
            ({}, {"ttl": 60}, (7, "expired", "expiry"), "grant 7 expired: no operation"),
            ({}, {"interrupt_after": 2}, (7, "aborted", "operator"), "grant 7 aborted: no operation"),
            ({"fail_start": True}, {}, (7, "aborted", "operator"), "grant 7 aborted: no operation")):
        case = Case(**kwargs)
        try:
            case.run(**run_kwargs)
            status = case.run(apply_it=True, expect=case.digest(), **run_kwargs)
            check(status == 1 and case.board.calls[1][1] == end and "porter-break-glass" in case.tmux.sessions_killed()
                  and not case.scratch().exists() and said in case.text()
                  and case.board.names()[-1] == "hand_back_break_glass",
                  f"{kwargs} {run_kwargs}: the session stopped, the token deleted, the grant ended {end}: "
                  f"{case.board.calls} {case.text()[-300:]}")
        finally:
            case.close()


def test_a_runtime_without_its_sandbox_is_refused() -> None:
    case = Case()
    try:
        case.tool_problems = ["bubblewrap (bwrap) is not installed for the project account; the role runs only in its sandbox"]
        check(case.run() == 1 and "runs only in its sandbox" in case.text() and case.board.calls == [],
              f"no sandbox, no role: {case.text()[-200:]}")
    finally:
        case.close()


STANDIN = """#!/usr/bin/python3
import json, os, subprocess, sys
PROBES = json.loads(%(probes)r)
OPS = json.loads(%(ops)r)
def reach(p):
    try:
        with open(p) as f:
            return "READ " + f.read(200)
    except OSError as e:
        return "DENIED " + type(e).__name__
report = {"uid": os.getuid(), "home": os.environ.get("HOME"), "cwd": os.getcwd(), "env": sorted(os.environ),
          "argv": sys.argv[1:], "listing": {d: sorted(os.listdir(d)) for d in ("/home", "/tmp", "/run")},
          "probes": {name: reach(p) for name, p in PROBES.items()},
          "own": {"packet": reach(%(work)r + "/recovery-packet.json"), "instructions": reach(%(work)r + "/INSTRUCTIONS.md"),
                  "token": reach(os.environ.get("TICKET_BOARD_BREAK_GLASS_TOKEN_FILE", ""))},
          "ops": []}
for op in OPS:
    done = subprocess.run(["python3", %(client)r, *op], capture_output=True, text=True)
    report["ops"].append({"op": op, "rc": done.returncode, "out": done.stdout[-600:], "err": done.stderr[-300:]})
with open(os.path.join(os.environ["HOME"], "report.json"), "w") as f:
    json.dump(report, f)
"""


def standin_cli(path: Path, probes: dict[str, str], *, operations: list[list[str]] = (), package: bool = True) -> None:
    """A stand-in for the role's CLI, installed as a package: what it can reach from inside the role's sandbox, and the
    operations it makes there with the client, reported in $HOME/report.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if package:
        (path.parent.parent / "package.json").write_text("{}")
    path.write_text(STANDIN % {"probes": json.dumps(probes), "ops": json.dumps([list(o) for o in operations]),
                               "work": bgr.SANDBOX_WORK, "client": bgr.SANDBOX_CLIENT})
    path.chmod(0o755)


def test_the_role_reaches_only_its_own_world_on_the_real_start_path() -> None:
    """The real argv start_command builds, run without its tmux prefix: bash, then bwrap, then a stand-in CLI that
    tries the project account's credentials, a Director transcript, another provider's key and another grant's token."""
    case = Case()
    try:
        owner = case.owner_home
        planted = {"held_credential": owner / ".claude/.credentials.json",
                   "director_transcript": owner / ".claude/projects/-home-o-worktrees-director/held.jsonl",
                   "other_provider_key": owner / ".config/other-provider/key",
                   "other_grant_token": case.root / "scratch" / "break-glass-6" / "token",
                   "board_write_token": case.root / ".switchyard" / "board-write-token"}
        for name, path in planted.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"SECRET-{name}")
        cli = case.root / "install" / "codex-pkg" / "bin" / "codex"
        standin_cli(cli, {name: str(p) for name, p in planted.items()})
        tools = bgr.resolve_tools(case.config, str(cli), runner=subprocess.run)
        original = team_launcher._owner_home_for_auth
        team_launcher._owner_home_for_auth = lambda *_a, **_k: owner  # type: ignore[assignment]
        try:
            scratch = bgr.build_scratch(case.config, case.root / "scratch" / "break-glass-7", grant=7, runtime="codex",
                                        packet=case.board.packet, token="T" * 43, allow=["isolate"], expires="soon",
                                        client=ROOT / "scripts/ticket_board/break_glass.py")
        finally:
            team_launcher._owner_home_for_auth = original  # type: ignore[assignment]
        argv = bgr.start_command("porter-break-glass", scratch, tools, runtime="codex", board_url="http://127.0.0.1:9",
                                 client=ROOT / "scripts/ticket_board/break_glass.py", instruction="the first instruction")
        done = subprocess.run(argv[argv.index("/bin/bash"):], capture_output=True, text=True, timeout=60)
        made = scratch["home"] / "report.json"  # no report is a sandbox that did not run: a failed check, not a crash
        report = json.loads(made.read_text()) if made.exists() else {"probes": {}, "own": {}, "listing": {}, "env": ["?"]}
        check(done.returncode == 0 and made.exists() and tools["binds"] == [str(cli)]
              and report.get("uid") == __import__("os").getuid(),
              f"the sandbox ran the stand-in from its own install tree: {done.returncode} {done.stderr[-300:]} {tools}")
        check(report["probes"] and all(said.startswith("DENIED") for said in report["probes"].values()),
              f"the held credential, the Director's transcript, another provider's key, another grant's token and the "
              f"board's write token do not exist in the role's world: {report['probes']}")
        check(report["own"].get("packet", "").startswith("READ {") and report["own"]["instructions"].startswith("READ # Break-glass")
              and report["own"]["token"] == "READ " + "T" * 43 + "\n" and report["home"] == bgr.SANDBOX_HOME
              and report["cwd"] == bgr.SANDBOX_WORK and report["listing"]["/home"] == ["break-glass"]
              # only the path down to the CLI's own file, whatever TMPDIR is
              and report["listing"]["/tmp"] == ([case.root.relative_to("/tmp").parts[0]]
                                                if case.root.is_relative_to("/tmp") else [])
              and report["argv"] == ["the first instruction"],
              f"it reads its packet, its instructions and its own token, in a /home with nothing else: {report['own']} "
              f"{report['listing']}")
        check(set(report["env"]) <= {"HOME", "PATH", "TERM", "LANG", "TICKET_BOARD_URL", "TICKET_BOARD_BREAK_GLASS_TOKEN_FILE",
                                     "PWD", "SHLVL", "_", "LC_CTYPE"},
              f"its environment holds nothing it was not given: {report['env']}")
        check(not scratch["token"].exists(), "the token file is gone from disk once the sandbox started")
    finally:
        case.close()


def test_only_a_program_or_its_npm_package_is_bound_never_a_home() -> None:
    home = "/home/o"
    for path, want in (("/home/o/.local/share/claude/versions/2.1.318", "/home/o/.local/share/claude/versions/2.1.318"),
                       ("/home/o/.nvm/versions/node/v22.11.0/bin/node", "/home/o/.nvm/versions/node/v22.11.0/bin/node"),
                       ("/home/o/codex", "/home/o/codex"),
                       ("/home/o/.npm-global/lib/node_modules/@openai/codex/bin/codex.js",
                        "/home/o/.npm-global/lib/node_modules/@openai/codex"),
                       ("/home/o/.npm-global/lib/node_modules/somecli/bin/x.js", "/home/o/.npm-global/lib/node_modules/somecli"),
                       ("/usr/bin/claude", None), ("/opt/codex/bin/codex", None)):
        check(bgr._install_root(path) == want, f"{path}: binds {bgr._install_root(path)}, not {want}")
    check(bgr._unconfined([home], ["/", "/home", home]) and bgr._unconfined(["/home"], ["/", "/home", home])
          and not bgr._unconfined(["/home/o/codex", "/home/o/.npm-global/lib/node_modules/somecli"], ["/", "/home", home]),
          "a bind that is or holds the home, /home or / is refused; a program or its package is not")


def test_a_cli_that_cannot_be_confined_is_refused() -> None:
    case = Case()
    try:
        home = case.root / "node_modules" / "owner"  # a home that is itself an npm package's directory
        cli = home / "bin" / "codex"
        standin_cli(cli, {}, package=False)
        original = team_launcher._owner_home_for_auth
        team_launcher._owner_home_for_auth = lambda *_a, **_k: home  # type: ignore[assignment]
        try:
            tools = bgr.resolve_tools(case.config, str(cli), runner=subprocess.run)
        finally:
            team_launcher._owner_home_for_auth = original  # type: ignore[assignment]
        check(tools.get("problems") and "cannot confine" in tools["problems"][0] and "binds" not in tools,
              f"a bind that would hold the project account's home refuses the role: {tools}")
    finally:
        case.close()


def test_a_native_install_beside_a_home_package_json_stays_confined_on_the_real_start_path() -> None:
    """Audit's shape: Claude's native layout (~/.local/share/claude/versions/X, one file) with a package.json in the
    home, as `npm install` there leaves. Run through the real argv: the home's credential and transcript are DENIED."""
    case = Case()
    try:
        owner = case.owner_home
        (owner / "package.json").write_text("{}")
        planted = {"held_credential": owner / ".claude/.credentials.json",
                   "director_transcript": owner / ".claude/projects/-home-o-worktrees-director/held.jsonl"}
        for name, path in planted.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"SECRET-{name}")
        cli = owner / ".local/share/claude/versions/2.1.318"
        standin_cli(cli, {name: str(p) for name, p in planted.items()}, package=False)
        original = team_launcher._owner_home_for_auth
        team_launcher._owner_home_for_auth = lambda *_a, **_k: owner  # type: ignore[assignment]
        try:
            tools = bgr.resolve_tools(case.config, str(cli), runner=subprocess.run)
            scratch = bgr.build_scratch(case.config, case.root / "scratch" / "break-glass-8", grant=8, runtime="codex",
                                        packet=case.board.packet, token="U" * 43, allow=["record"], expires="soon",
                                        client=ROOT / "scripts/ticket_board/break_glass.py")
        finally:
            team_launcher._owner_home_for_auth = original  # type: ignore[assignment]
        check(tools.get("binds") == [str(cli)] and not tools.get("problems"),
              f"Claude's native layout binds only the program's own file, a home package.json notwithstanding: {tools}")
        argv = bgr.start_command("porter-break-glass", scratch, tools, runtime="codex", board_url="http://127.0.0.1:9",
                                 client=ROOT / "scripts/ticket_board/break_glass.py", instruction="go")
        done = subprocess.run(argv[argv.index("/bin/bash"):], capture_output=True, text=True, timeout=60)
        made = scratch["home"] / "report.json"
        report = json.loads(made.read_text()) if made.exists() else {"probes": {}}
        check(done.returncode == 0 and report["probes"]
              and all(said.startswith("DENIED") for said in report["probes"].values()),
              f"only the program's own file is bound, and the home stays out: {tools} {report['probes']} "
              f"{done.stderr[-200:]}")
    finally:
        case.close()


def test_a_refused_grant_starts_nothing() -> None:
    case = Case(board=Board(refuse_open="ERROR:  another break-glass grant stands"))
    try:
        case.run()
        status = case.run(apply_it=True, expect=case.digest())
        check(status == 1 and case.tmux.started == [] and "refused the grant" in case.text()
              and not (case.root / "scratch").exists(), f"nothing is built or started: {case.text()[-200:]}")
    finally:
        case.close()


def test_a_missing_credential_is_not_replaced_by_another() -> None:
    case = Case()
    try:
        (case.owner_home / ".codex/auth.json").unlink()
        case.run()
        status = case.run(apply_it=True, expect=case.digest())
        check(status == 1 and case.tmux.started == [] and case.board.calls[1][1][1] == "aborted"
              and "codex's credential .codex/auth.json is not in" in case.text(),
              f"no session starts without its own credential; the grant is ended: {case.text()[-300:]}")
    finally:
        case.close()


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"break_glass_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
