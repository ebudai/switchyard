#!/usr/bin/env python3
"""SYRD-113 slice A: a privileged action is a job with an owner, and its output is recorded.

On SYRD-102 a privileged command's output existed only in the User's terminal.
SYRD-112's root helper records each run in root's rollout journal, but it ran
the operation with a plain `subprocess.run`: the output went to the pane and
`stdout.log`/`stderr.log` stayed empty, and the record named no ticket, no
requesting role and no action. A timeout was recorded as an ordinary failure,
and the front door's deadline was the same as the helper's, so the two raced.

Slice A makes each run a job:
- an optional, validated `ticket=PREFIX-N` that the helper finds on the
  project's own board before anything runs, and never passes to the operation;
- the action, its operation values, the ticket, the requesting control role
  and the release commit in the record;
- output streamed to the pane as it comes, and into the record redacted,
  timestamped and bounded -- per stream and per line, without holding more than
  a line's bound -- with what was dropped counted and marked;
- a deadline the helper owns, which stops the operation's whole process group
  and records it as timed out, inside the front door's longer one.

Driven through the real catalogue, the real `rollout_journal.Attempt` in a
scratch journal root, the real streaming runner on real child processes, and
the ticket check against a real board over HTTP. It runs on main, from a git
archive in a child process, and on this tree.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "9ee705a11303e7bdd8fc80365bd82e1f3d470b2c"  # main before SYRD-113 slice A
COMMIT = "a" * 40
CHECKS = 0

# The operation each case runs, as a child: it prints, fails, floods, or hangs.
CHILD = r'''
import os, sys, time
mode = sys.argv[1]
if mode in ("ok", "fail"):
    print("starting the deploy", flush=True)
    print("API_TOKEN=supersecret-value-1", flush=True)
    print("Authorization: Bearer abcdefgh12345678", file=sys.stderr, flush=True)
    print("-----BEGIN OPENSSH PRIVATE KEY-----\nkeymaterialkeymaterial\n-----END OPENSSH PRIVATE KEY-----", flush=True)
    print("done", flush=True)
    sys.exit(0 if mode == "ok" else 3)
if mode == "flood":
    line = ("x" * 1023) + "\n"
    for _ in range(6 * 1024):
        sys.stdout.write(line)
    sys.stdout.write("y" * (3 * 1024 * 1024) + "TOKEN=hidden-at-the-cut")
    sys.stdout.flush()
    print("after the long line", file=sys.stderr, flush=True)
    sys.exit(0)
if mode == "longline":
    sys.stdout.write("before\n" + "y" * (3 * 1024 * 1024) + " API_TOKEN=hidden-past-the-cut\nafter\n")
    sys.stdout.flush()
    sys.exit(0)
if mode == "straddle":
    # The line's bound falls inside a bearer token: what precedes the cut is
    # " Bearer abcd", too short for any pattern to know for a secret.
    print("z" * (64 * 1024 - 12) + " Bearer abcdefghijklmnop", flush=True)
    sys.exit(0)
if mode == "hang":
    child = os.fork()
    if child == 0:
        time.sleep(600)
        os._exit(0)
    open(sys.argv[2], "w").write(f"{os.getpid()} {child}")
    print("waiting forever", flush=True)
    time.sleep(600)
'''


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples", "config"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def attempt_of(journal: Path, project: str) -> dict:
    """The newest attempt's directory, result and logs."""
    directory = sorted(p for p in (journal / project).iterdir() if p.is_dir())[-1]
    result = json.loads((directory / "result.json").read_text())
    index = [json.loads(line) for line in (journal / project / "index.jsonl").read_text().splitlines()]
    logs = {name: (directory / f"{name}.log").read_text() for name in ("stdout", "stderr")}
    shas = {f"{name}.log": hashlib.sha256((directory / f"{name}.log").read_bytes()).hexdigest() for name in logs}
    return {"result": result, "start": [e for e in index if e.get("event") == "start"][-1], "logs": logs, "shas": shas}


def outcome(call):
    try:
        return {"ok": True, "value": call()}
    except BaseException as exc:  # noqa: BLE001 - the refusal is the observation
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "scripts"), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from scripts.ticket_board import privileged_actions as pa
    from scripts.ticket_board import privileged_front_door as fd
    from scripts.ticket_board import privileged_helper as ph
    from scripts.ticket_board import rollout_journal as rj

    seen: dict = {}
    # The catalogue: a ticket is an optional job value, validated, for actions with a project.
    action = pa.action_for("deploy-release")
    seen["valid_ticket"] = outcome(lambda: action.validate({"project": "mefp", "commit": COMMIT, "ticket": "MEFP-113"}))
    seen["bad_tickets"] = {raw: outcome(lambda raw=raw: action.validate({"project": "mefp", "commit": COMMIT, "ticket": raw}))["ok"]
                           for raw in ("mefp-113", "MEFP-0", "MEFP-113;id", "../MEFP-1", "MEFP", "MEFP-1 x")}
    seen["projectless_ticket"] = outcome(lambda: pa.action_for("install-shared-release").validate({"commit": COMMIT, "ticket": "MEFP-1"}))["ok"]
    seen["parse_request"] = outcome(lambda: ph.parse_request(["deploy-release", "project=mefp", f"commit={COMMIT}", "ticket=MEFP-113"])[1])
    seen["deadlines"] = {"helper": ph.ACTION_TIMEOUT_SECONDS, "front_door": fd.DEFAULT_ACTION_TIMEOUT_SECONDS,
                         "grace": getattr(ph, "TERMINATE_GRACE_SECONDS", None)}

    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        tmp = Path(tmp)
        journal = tmp / "journal"
        os.environ["SWITCHYARD_ROLLOUT_JOURNAL_ROOT"] = str(journal)
        child = tmp / "child.py"
        child.write_text(CHILD)

        def run(mode: str, *, values: dict, timeout: float = 60.0, extra: list[str] = ()) -> dict:
            terminal = {"stdout": io.BytesIO(), "stderr": io.BytesIO()}
            printed: list[str] = []
            runner = ph._run_command
            if getattr(runner, "streams_output", False):
                def runner(command, *, timeout, sink=None, real=ph._run_command):
                    return real(command, timeout=timeout, sink=sink, terminal=terminal, grace=2.0)
                runner.streams_output = True
            done = outcome(lambda: ph.run_privileged_action(
                action, values, project="mefp", attempt_factory=ph._attempt_factory, runner=runner,
                command_for=lambda name, vals: [sys.executable, str(child), mode, *extra],
                timeout=timeout, print_func=printed.append,
                **({"requester_role": "director"} if "requester_role" in ph.run_privileged_action.__code__.co_varnames else {})))
            return {"done": done, "attempt": attempt_of(journal, "mefp"), "printed": printed,
                    "terminal": {k: v.getvalue().decode("utf-8", "replace") for k, v in terminal.items()}}

        job_values = {"project": "mefp", "commit": COMMIT, **({"ticket": "MEFP-113"} if seen["valid_ticket"]["ok"] else {})}
        seen["ok"] = run("ok", values=job_values)
        seen["fail"] = run("fail", values=job_values)
        seen["flood"] = run("flood", values=job_values)
        seen["longline"] = run("longline", values=job_values)
        seen["straddle"] = run("straddle", values=job_values)
        pids = tmp / "pids"
        hang = run("hang", values=job_values, timeout=3.0, extra=[str(pids)])
        alive = []
        for pid in pids.read_text().split() if pids.exists() else []:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
                alive.append(state != "Z")
            except FileNotFoundError:
                alive.append(False)
        for pid in pids.read_text().split() if pids.exists() else []:
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid), 9)  # main leaves the operation's own child running; this suite does not
        seen["hang"] = {**hang, "alive": alive}
        seen["log_text"] = rj.format_attempts("mefp", rj.attempts("mefp", root=journal), [])
        for key in ("ok", "fail", "flood", "longline", "straddle", "hang"):
            seen[key]["attempt"]["logs"] = {name: text[-4000:] if key in ("flood", "longline", "straddle") else text
                                            for name, text in seen[key]["attempt"]["logs"].items()}
            seen[key]["terminal"] = {name: text[-2000:] for name, text in seen[key]["terminal"].items()}
        directories = sorted(p for p in (journal / "mefp").iterdir() if p.is_dir())
        seen["flood_sizes"] = {name: (directories[2] / f"{name}.log").stat().st_size for name in ("stdout", "stderr")}
        longline = (directories[3] / "stdout.log").read_text().splitlines()
        seen["longline_lines"] = [line[:80] + f"...<{len(line)}>..." + line[-120:] for line in longline]
    return seen


def board_scenario(root: Path, prefix: str) -> dict:
    """The ticket check against a real board, over HTTP."""
    for extra in (str(root), str(root / "scripts"), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from ticket_board_pane_env import strip_ticket_board_pane_env

    strip_ticket_board_pane_env(os.environ)
    import legacy_workflow_equivalence_test as equivalence
    import ticket_board_write_api_test as t
    from scripts.ticket_board import privileged_helper as ph
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        board = equivalence.Board(cluster, "jobs")
        try:
            t.seed_postgres_ticket(board.admin, "MEFP-113", title="the originating ticket", state="in_progress",
                                   assignee="ops", commit_exempt=True)
            for ticket in ("MEFP-113", "MEFP-999"):
                seen[ticket] = outcome(lambda ticket=ticket: ph.require_ticket_on_board(
                    "mefp", ticket, board_get=ph._board_get, board_url=board.base))
            seen["board_down"] = outcome(lambda: ph.require_ticket_on_board(
                "mefp", "MEFP-113", board_get=ph._board_get, board_url="http://127.0.0.1:9/"))
            # The installed entry point, with only the host's own facts stood in: the installation, the
            # pane identity, the control-role check and the launcher. The ticket check, the record and the
            # streaming runner are the real ones.
            import privileged_helper_boundary_test as boundary
            from scripts.ticket_board import privileged_operations as po

            with tempfile.TemporaryDirectory(prefix="syrd113-main-") as tmp:
                journal = Path(tmp) / "journal"
                os.environ["SWITCHYARD_ROLLOUT_JOURNAL_ROOT"] = str(journal)
                child = Path(tmp) / "child.py"
                child.write_text(CHILD)
                stand_ins = dict(
                    privileged_install=boundary._StubInstall([]),
                    caller_identity=lambda *a, **k: ph.CallerIdentity(4242, 99, os.getuid()),
                    board_url_for=lambda project, **k: board.base,
                    require_registered_control_caller=lambda project, **k: "director",
                )
                with boundary.swapped(po, command_for=lambda name, values: [sys.executable, str(child), "ok"]):
                    for ticket in ("MEFP-113", "MEFP-999"):
                        out, err = io.StringIO(), io.StringIO()
                        with boundary.swapped(ph, **stand_ins), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                            code = ph.main(["deploy-release", "project=mefp", f"commit={COMMIT}", f"ticket={ticket}"])
                        attempts = sorted(p.name for p in (journal / "mefp").iterdir() if p.is_dir()) if (journal / "mefp").exists() else []
                        seen[f"main/{ticket}"] = {"code": code, "stderr": err.getvalue()[-300:], "attempts": attempts,
                                                  "job": attempt_of(journal, "mefp")["result"].get("job") if attempts else None}
        finally:
            board.close()
    return seen


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd113b-")))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd113.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    after = scenario(ROOT, "syrd113a-")
    board = board_scenario(ROOT, "syrd113c-")
    sys.path.insert(0, str(ROOT))
    from scripts.ticket_board import rollout_journal as rj

    secrets = ("supersecret", "abcdefgh12345678", "keymaterial", "hidden-past-the-cut")
    stamped = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ")

    # Main, as SYRD-112 left it.
    check(not before["valid_ticket"]["ok"] and "refusing unexpected ['ticket']" in before["valid_ticket"]["error"],
          f"main: a run cannot say which ticket it is for: {before['valid_ticket']}")
    check(all(before[k]["attempt"]["logs"] == {"stdout": "", "stderr": ""} and before[k]["attempt"]["result"].get("job") is None
              for k in ("ok", "fail", "flood")),
          "main: the record of every run has empty logs and names no action, ticket or role")
    check(before["hang"]["attempt"]["result"]["status"] == "failed" and before["hang"]["alive"] == [False, True]
          and before["deadlines"]["helper"] == before["deadlines"]["front_door"],
          f"main: a timeout is recorded as an ordinary failure, the operation's own child is left running, and the "
          f"front door's deadline is the helper's: {before['hang']['alive']} {before['deadlines']}")

    # The job's identity.
    check(after["valid_ticket"] == {"ok": True, "value": {"project": "mefp", "commit": COMMIT, "ticket": "MEFP-113"}}
          and after["parse_request"]["ok"] and not any(after["bad_tickets"].values()) and after["projectless_ticket"] is False,
          f"a ticket is an optional job value of the board's own form, refused in any other, and refused where no "
          f"project names a board to report to: {after['bad_tickets']}")
    check(board["MEFP-113"]["ok"] and not board["MEFP-999"]["ok"] and "404" in board["MEFP-999"]["error"]
          and not board["board_down"]["ok"] and "nothing was run" in board["board_down"]["error"],
          f"the helper finds the ticket on the project's own board, or refuses: {board['MEFP-999']['error']}")
    job = {"action": "deploy-release", "action_id": "org.switchyard.privileged.deploy-release",
           "arguments": {"commit": COMMIT, "project": "mefp"}, "release_commit": COMMIT,
           "requester_role": "director", "ticket": "MEFP-113"}
    check(board["main/MEFP-113"]["code"] == 0 and board["main/MEFP-113"]["job"] == job,
          f"through the installed entry point, the record names the action, its operation values (not the "
          f"ticket), the ticket, the requesting control role and the release: {board['main/MEFP-113']['job']}")
    check(board["main/MEFP-999"]["code"] == 1 and "nothing was run" in board["main/MEFP-999"]["stderr"]
          and board["main/MEFP-999"]["attempts"] == board["main/MEFP-113"]["attempts"],
          f"a ticket the board does not hold is refused before any attempt is opened: {board['main/MEFP-999']['stderr']}")

    # What a run records.
    for key, status, code in (("ok", "succeeded", 0), ("fail", "failed", 3)):
        run, result = after[key], after[key]["attempt"]["result"]
        lines = run["attempt"]["logs"]["stdout"].splitlines() + run["attempt"]["logs"]["stderr"].splitlines()
        check(run["done"] == {"ok": True, "value": code} and result["status"] == status and result["exit_status"] == code
              and result["job"] == job and run["attempt"]["start"]["job"] == job,
              f"{key}: the run's status and exit, and the job in the result and the journal's start entry: {result['status']}")
        check([stamped.sub("", line) for line in lines] == ["starting the deploy", "API_TOKEN=<redacted>",
                                                            "<redacted private key>", "done", "Authorization: <redacted>"]
              and all(stamped.match(line) for line in lines)
              and not any(secret in json.dumps(run["attempt"]) for secret in secrets),
              f"{key}: every line is recorded, timestamped and redacted before it is written: {lines}")
        check(all(result["artifacts"][name] == sha for name, sha in run["attempt"]["shas"].items())
              and result["capture"]["stdout"]["bytes"] == result["capture"]["stdout"]["recorded_bytes"]
              and not result["capture"]["stdout"]["truncated"],
              f"{key}: the recorded checksums are the files', and the capture's size is accounted: {result['capture']}")
        check("starting the deploy\n" in run["terminal"]["stdout"] and "done\n" in run["terminal"]["stdout"]
              and "Authorization: Bearer" in run["terminal"]["stderr"],
              f"{key}: the pane still shows the operation's output as it came: {run['terminal']}")
    flood = after["flood"]["attempt"]
    counts = flood["result"]["capture"]["stdout"]
    marker = flood["logs"]["stdout"].splitlines()[-1]
    check(counts["truncated"] and counts["limit"] == rj.STREAM_LIMIT_BYTES
          and counts["recorded_bytes"] <= counts["limit"]
          and after["flood_sizes"]["stdout"] == counts["recorded_bytes"] + len(marker.encode()) + 1 == counts["bytes"]
          and counts["dropped_bytes"] > 2 * 1024 * 1024
          and f"{counts['dropped_bytes']} bytes in {counts['dropped_lines']} lines after the first "
              f"{counts['recorded_bytes']} bytes were not recorded" in marker
          and flood["logs"]["stderr"].endswith("after the long line\n"),
          f"a flooded stream stops at its limit with one marker that says what was dropped, and the other stream "
          f"is unaffected: {marker} {after['flood_sizes']}")
    longline = after["longline_lines"]
    # 65,536 characters held, the last 256 of them dropped too: 65,280 kept of 3,145,758.
    check(len(longline) == 3 and longline[0].endswith(" before") and longline[2].endswith(" after")
          and "...<65370>..." in longline[1]
          and longline[1].endswith("y [switchyard: 3080478 more characters of this line were not recorded]")
          and "hidden-past-the-cut" not in json.dumps(after["longline"]["attempt"]),
          f"a line past its bound is cut, its end with it, the drop is counted, and the next line is recorded: {longline}")
    straddle = after["straddle"]["attempt"]["logs"]["stdout"]
    check(straddle.endswith("z [switchyard: 268 more characters of this line were not recorded]\n")
          and "Bearer" not in straddle and "Bearer abcd" in after["straddle"]["terminal"]["stdout"],
          f"a secret the bound falls inside is not recorded even in part: the end of what is kept goes too: "
          f"{straddle[-120:]}")
    hang = after["hang"]
    check(hang["done"] == {"ok": True, "value": 124} and hang["attempt"]["result"]["status"] == "timed_out"
          and hang["alive"] == [False, False] and stamped.sub("", hang["attempt"]["logs"]["stdout"]) == "waiting forever\n"
          and any("recorded as timed out" in line for line in hang["printed"]),
          f"at its deadline the helper stops the operation's whole process group and records it as timed out, "
          f"with what it said first: {hang['alive']} {hang['attempt']['result']['status']}")
    deadlines = after["deadlines"]
    check(deadlines["helper"] + 2 * deadlines["grace"] < deadlines["front_door"] == 900.0,
          f"the helper's deadline, with its stop, ends inside the front door's: {deadlines}")
    check("deploy-release for MEFP-113, asked by director" in after["log_text"],
          f"`switchyard rollout-log` names each job's action, ticket and requester: {after['log_text'][:300]}")
    print(f"privileged_job_capture_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
