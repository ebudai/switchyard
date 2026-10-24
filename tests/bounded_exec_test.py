#!/usr/bin/env python3
"""SYRD-616: `switchyard-bounded` stops what it was told to stop, and only that.

Real processes throughout: the entry script is run as a role would run it, in
a session of its own, with the pane-state directory a pane is given. Each
check names a process by a unique `sleep` length, so "nothing left" is read
from /proc, not assumed.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "switchyard-bounded"
CHECKS = 0
sys.path.insert(0, str(ROOT))


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def running(marker: str) -> list[int]:
    """Live processes whose command line holds `marker`."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            continue
        if marker in argv and state != "Z" and "bounded_exec_test" not in argv:
            found.append(int(entry.name))
    return found


def bounded(args: list[str], state_dir: Path, *, timeout: float = 60) -> tuple[int, str, str, float]:
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(WRAPPER), *args], capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "TICKET_BOARD_PANE_STATE_DIR": str(state_dir)}, start_new_session=True)
    return proc.returncode, proc.stdout, proc.stderr, time.monotonic() - started


def main() -> int:
    from scripts.ticket_board import execution_lease as lease

    with tempfile.TemporaryDirectory(prefix="syrd616b.") as tmp:
        state = Path(tmp) / "pane-state"
        state.mkdir()
        leases = state / "leases"

        # 1. A silent infinite loop under a hard deadline.
        code, _out, err, took = bounded(["--deadline", "2s", "--grace", "1", "--", "bash", "-c",
                                         "while :; do :; done # silent-616"], state)
        check(code == 124 and "timeout after" in err and "SIGINT" in err and took < 2 + 3 + 2
              and not running("silent-616"),
              f"a silent loop is stopped at its deadline, reported as a timeout, and nothing is left: {code} {took:.1f}s {err}")

        # 2. A loop that writes output without end: the output is bounded, the verdict a timeout.
        code, out, err, took = bounded(["--deadline", "2s", "--grace", "1", "--output-limit", "4096", "--", "bash", "-c",
                                        "while :; do echo chatty-616; done"], state)
        check(code == 124 and "bytes omitted" in out and len(out) < 6000 and out.startswith("chatty-616")
              and took < 2 + 3 + 2 and not running("chatty-616"),
              f"a loop that never stops talking is stopped too; its output is kept as a bounded head and tail: {code} {took:.1f}s {len(out)}")

        # 3. A nested child that ignores SIGINT, and one that left the group and the session.
        nested = ("bash -c 'trap \"\" INT; exec sleep 1000.6161' & setsid sleep 1000.6162 & "
                  "trap '' INT; while :; do sleep 1000.6163; done")
        code, _out, err, took = bounded(["--deadline", "2s", "--grace", "1", "--", "bash", "-c", nested], state)
        check(code == 124 and "SIGTERM" in err and took < 2 + 3 + 2
              and not running("1000.6161") and not running("1000.6162") and not running("1000.6163"),
              f"a child that ignores SIGINT gets SIGTERM, an escaped one is found and stopped, nothing is left: "
              f"{code} {took:.1f}s {err} {running('1000.616')}")

        # 4. An ordinary command: its own output and exit status, untouched.
        code, out, err, took = bounded(["--deadline", "30s", "--", "bash", "-c", "echo out-616; echo err-616 >&2; exit 7"],
                                       state)
        check(code == 7 and out == "out-616\n" and err.startswith("err-616\n") and "exit 7" in err and took < 10,
              f"an ordinary command keeps its output and its exit status: {code} {out!r} {err!r}")

        # 5. A declared budget stops nothing, and the board can read it while it runs.
        proc = subprocess.Popen([sys.executable, str(WRAPPER), "--expect", "1s", "--label", "quiet test", "--", "bash", "-c",
                                 "sleep 3; echo slow-616"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env={**os.environ, "TICKET_BOARD_PANE_STATE_DIR": str(state)}, start_new_session=True)
        time.sleep(1.0)
        session = os.getsid(proc.pid)
        declared = lease.declaration(leases, session, lease.start_ticks(session), now=time.time())
        out, err = proc.communicate(timeout=30)
        check(proc.returncode == 0 and out == "slow-616\n" and declared is not None and declared["source"] == "declared"
              and declared["hard"] is False and declared["label"] == "quiet test"
              and not list(leases.glob(f"{session}-*.json")),
              f"--expect stops nothing; its budget is declared while it runs and withdrawn after: {proc.returncode} {declared}")

        # 6. A hard deadline is declared as hard.
        proc = subprocess.Popen([sys.executable, str(WRAPPER), "--deadline", "20s", "--", "sleep", "2"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env={**os.environ, "TICKET_BOARD_PANE_STATE_DIR": str(state)}, start_new_session=True)
        time.sleep(0.8)
        session = os.getsid(proc.pid)
        declared = lease.declaration(leases, session, lease.start_ticks(session), now=time.time())
        proc.wait(timeout=30)
        check(declared is not None and declared["hard"] is True and declared["source"] == "hard"
              and 15 < declared["deadline"] - time.time() < 20,
              f"--deadline is declared as a hard deadline the wrapper enforces: {declared}")

        # 7. A whole-run budget is shared, and once spent nothing more starts.
        made = Path(tmp) / "third-ran"
        first = bounded(["--run", "suite", "--total", "3s", "--", "sleep", "1"], state)
        second = bounded(["--run", "suite", "--total", "3s", "--grace", "0.5", "--", "sleep", "30"], state)
        third = bounded(["--run", "suite", "--total", "3s", "--", "touch", str(made)], state)
        check(first[0] == 0 and second[0] == 124 and second[3] < 5 and third[0] == 124 and "not started" in third[2]
              and not made.exists(),
              f"a run's commands share its whole-run budget, and once it is spent the next is refused: "
              f"{first[0]} {second[0]} {second[3]:.1f}s {third[0]} {third[2]!r}")

        # 8. The Director's interrupt: a signal to the wrapper, and the board's marker naming who and why.
        proc = subprocess.Popen([sys.executable, str(WRAPPER), "--expect", "1h", "--grace", "1", "--", "bash", "-c",
                                 "trap '' INT; sleep 1000.6164"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env={**os.environ, "TICKET_BOARD_PANE_STATE_DIR": str(state)}, start_new_session=True)
        time.sleep(1.0)
        session = os.getsid(proc.pid)
        lease.mark_interrupt(leases, session, lease.start_ticks(session), actor="director", reason="it looped on PGU-1")
        os.killpg(proc.pid, signal.SIGINT)
        out, err = proc.communicate(timeout=30)
        check(proc.returncode == 130 and "interrupted by the director: it looped on PGU-1" in err
              and not running("1000.6164"),
              f"an interrupt stops the command's group and says who asked and why: {proc.returncode} {err!r}")

        # 9. Nothing outside the command's own group is signalled: a sibling in the caller's session lives on.
        script = Path(tmp) / "caller.sh"
        script.write_text(f"sleep 1000.6165 &\nsibling=$!\n{sys.executable} {WRAPPER} --deadline 1s --grace 0.5 -- "
                          f"sleep 1000.6166\necho \"wrapper=$?\"\nkill -0 $sibling && echo sibling-alive\nkill $sibling\n")
        caller = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=30,
                                env={**os.environ, "TICKET_BOARD_PANE_STATE_DIR": str(state)}, start_new_session=True)
        check("wrapper=124" in caller.stdout and "sibling-alive" in caller.stdout and not running("1000.6166"),
              f"a timeout stops only the command's group; a sibling in the caller's session is untouched: {caller.stdout!r}")

    # Pure parts. Only what is provably safe is kept: no rule that looks for secrets can be complete.
    here = str(Path(__file__).resolve())
    summaries = {
        "flags and values": lease.sanitize_command(["curl", "-H", "x", "--token", "abc123", "PASSWORD=hunter2",
                                                    "https://u:p4ss@host/x", "--api-key=k1", here]),
        "a header": lease.sanitize_command(["curl", "-H", "Authorization: Bearer alpha beta", "https://host/x"]),
        "a JSON config": lease.sanitize_command(["tool", "--config", '{"password":"alpha beta"}']),
        "a whole script": lease.sanitize_command(["bash", "-c", 'curl -H "Authorization: Bearer x" u && sleep 1']),
        "an attached value": lease.sanitize_command(["mysql", "-pS3cret", "-u", "root"]),
        "a path, relative": lease.sanitize_command(["/usr/bin/python3", "tests/bounded_exec_test.py", "-k", "x"],
                                                   cwd=str(ROOT)),
        "a path as an option's value": lease.sanitize_command(["tool", f"--password={here}", f"--token={here}",
                                                               f"--config={here}"]),
    }
    check(summaries == {
        "flags and values": f"curl -H {lease.MASK} --token {lease.MASK} PASSWORD={lease.MASK} {lease.MASK} "
                            f"--api-key={lease.MASK} {here}",
        "a header": f"curl -H {lease.MASK} {lease.MASK}",
        "a JSON config": f"tool --config {lease.MASK}",
        "a whole script": f"bash -c {lease.MASK}",
        "an attached value": f"mysql -p{lease.MASK} -u {lease.MASK}",
        "a path, relative": f"python3 tests/bounded_exec_test.py -k {lease.MASK}",
        "a path as an option's value": f"tool --password={lease.MASK} --token={lease.MASK} --config={lease.MASK}"},
          f"a summary keeps the program, option names and paths that exist, and masks every value however it is "
          f"spelled: {summaries}")
    check(lease.sanitize_command(summaries["a whole script"].split()) == summaries["a whole script"],
          "a summary read back is summarized to itself")
    long = lease.sanitize_command(["x"] + ["--y"] * 200)
    with tempfile.TemporaryDirectory(prefix="syrd616a.") as tmp:
        accented = Path(tmp) / "caf\u00e9-616.txt"  # a path that exists is kept, so its name reaches the summary
        accented.write_text("")
        accents = lease.sanitize_command(["cat", str(accented)])
    check(len(long) == lease.COMMAND_CHARS and long.endswith("...") and long.isascii()
          and accents == f"cat {str(accented).replace(chr(0xe9), '?')}",
          f"a summary is bounded and plain ASCII, so no board encoding can cut a character in half: {accents!r}")
    check([lease.parse_duration(text) for text in ("90", "90s", "20m", "1.5h", "1d")] == [90, 90, 1200, 5400, 86400],
          "durations read as coreutils timeout's")
    with tempfile.TemporaryDirectory(prefix="syrd616c.") as tmp:
        lease.declare(Path(tmp), 4242, 7, deadline=time.time() + 30 * 86400, hard=False, command="x")
        capped = lease.declaration(Path(tmp), 4242, 7, now=time.time(), environ={lease.MAX_DECLARED_ENV: "2h"})
        check(capped is not None and capped["deadline"] - time.time() < 2 * 3600 + 5,
              f"a role's declared budget is capped: {capped}")
    print(f"bounded_exec_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
