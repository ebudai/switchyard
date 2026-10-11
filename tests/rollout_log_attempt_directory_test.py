#!/usr/bin/env python3
"""SYRD-441: `switchyard rollout-log` reads nothing for an attempt that names no directory.

An attempt's record comes from the journal index: its start entry carries the
directory the writer created, and `attempts()` makes an attempt out of a finish
entry alone. So an index whose start entry was lost, garbled or edited yields an
attempt with no directory. The command turned that into `Path("")` -- the
working directory -- and printed whatever `result.json`, `stdout.log` and
`stderr.log` it found there as that attempt's. A relative directory had the same
defect, and a non-string one crashed.

Each journal here is written by the real `Attempt` writer into a test-owned tree
that `SWITCHYARD_ROLLOUT_JOURNAL_ROOT` points at -- the live journal is never
read -- and then its index is made malformed the way an index can be. The
command runs from a working directory full of decoy files, under an audit hook
that records every file opened. Pinned:

- **Nothing outside a recorded attempt directory is read.** No decoy is shown,
  and the only files opened are the index and the files of attempt directories
  the index really records.
- **The refusal:** asked for such an attempt, by name or as the latest with
  `--output`, the command says what the record lacks and exits 1. The summary
  says the same in place of the directory line, and `result_present` is false
  whatever the working directory holds.
- **Valid attempts read as before,** beside a malformed one too.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import rollout_log  # noqa: E402
from scripts.ticket_board import rollout_journal as rj  # noqa: E402

PROJECT = "p441"
CHECKS = 0
FAILURES: list[str] = []
DECOYS = {"result.json": "DECOY result", "stdout.log": "DECOY out\n", "stderr.log": "DECOY err\n"}

OPENED: list[str] | None = None


def _audit(event: str, args: tuple) -> None:
    if OPENED is not None and event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        OPENED.append(os.path.abspath(os.fsdecode(args[0])))


sys.addaudithook(_audit)


def check(condition: bool, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(detail)
        print(f"FAIL: {detail}")


class Journal:
    """A test-owned journal root, a decoy working directory, and the command run between them."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="syrd441-")).resolve()
        self.root = self.tmp / "journal"
        self.cwd = self.tmp / "cwd"
        self.cwd.mkdir()
        for name, text in DECOYS.items():
            (self.cwd / name).write_text(text, encoding="utf-8")
        self.saved_env = os.environ.get(rj.JOURNAL_ROOT_ENV)
        self.saved_cwd = os.getcwd()
        os.environ[rj.JOURNAL_ROOT_ENV] = str(self.root)
        os.chdir(self.cwd)

    def close(self) -> None:
        os.chdir(self.saved_cwd)
        if self.saved_env is None:
            os.environ.pop(rj.JOURNAL_ROOT_ENV, None)
        else:
            os.environ[rj.JOURNAL_ROOT_ENV] = self.saved_env
        for path in self.tmp.rglob("*"):
            if not path.is_symlink():
                os.chmod(path, 0o755 if path.is_dir() else 0o644)
        shutil.rmtree(self.tmp)

    @property
    def index(self) -> Path:
        return rj.index_path(PROJECT, root=self.root)

    def record(self, *, result_out: str = "", finish: bool = True) -> str:
        """One attempt through the real writer; its id."""
        attempt = rj.Attempt(PROJECT, ["sh", "-c", "true"], root=self.root, target_commit="c" * 40, operator="tester")
        attempt.open()
        if result_out:
            attempt.write("stdout", result_out)
            attempt.write("stderr", "err of " + result_out)
        if finish:
            attempt.close(status="completed", exit_status=0)
        return attempt.attempt

    def entries(self) -> list[str]:
        return self.index.read_text(encoding="utf-8").splitlines()

    def rewrite(self, lines: list[str], *, rechain: bool) -> None:
        """Put these index lines back; with `rechain`, recompute the hashes so the index still verifies."""
        if rechain:
            previous, rebuilt = "", []
            for line in lines:
                entry = json.loads(line)
                entry["previous"] = previous
                line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
                previous = hashlib.sha256(line.encode("utf-8")).hexdigest()
                rebuilt.append(line)
            lines = rebuilt
        os.chmod(self.index, 0o644)
        self.index.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def edit_start(self, attempt: str, change, *, rechain: bool = True) -> None:
        lines = []
        for line in self.entries():
            entry = json.loads(line)
            if entry.get("attempt") == attempt and entry.get("event") == "start":
                change(entry)
                line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
            lines.append(line)
        self.rewrite(lines, rechain=rechain)

    def run(self, **kw: object) -> tuple[object, list[str], list[str]]:
        """The command, from the decoy directory: its exit status or exception, what it printed, what it opened."""
        global OPENED
        printed: list[str] = []
        OPENED = []
        try:
            got: object = rollout_log.rollout_log_command(PROJECT, print_func=printed.append, **kw)
        except Exception as exc:  # noqa: BLE001 -- a crash is an answer to report, not to hide
            got = f"raised {type(exc).__name__}: {exc}"
        finally:
            opened, OPENED = OPENED, None
        return got, printed, opened


def listed(journal: Journal) -> list[dict] | str:
    """`attempts()` for the journal, or the exception it raised (the baseline raised for a non-string directory)."""
    try:
        return rj.attempts(PROJECT, root=journal.root)
    except Exception as exc:  # noqa: BLE001
        return f"raised {type(exc).__name__}: {exc}"


def outside(opened: list[str], allowed: list[Path]) -> list[str]:
    """Opened files that are not one of `allowed` or inside an allowed directory."""
    roots = [str(p) for p in allowed]
    return [p for p in opened if not any(p == r or p.startswith(r + os.sep) for r in roots)]


# The ways an attempt comes to name no directory it can be read from. Each turns the one recorded attempt's start entry
# into the malformed shape; `rechain` says whether its hashes are recomputed, so the index verifies clean anyway.
def _drop(key):
    return lambda entry: entry.pop(key)


def _set(value):
    return lambda entry: entry.__setitem__("directory", value)


SHAPES = {
    "a start entry without a directory": (_drop("directory"), "recorded no directory"),
    "an empty directory": (_set(""), "recorded no directory"),
    "a null directory": (_set(None), "recorded no directory"),
    "a directory that is a number": (_set(7), "recorded the directory 7, which is not an absolute path"),
    "a directory that is a list": (_set(["x"]), "recorded the directory ['x'], which is not an absolute path"),
    "the directory '.'": (_set("."), "recorded the directory '.', which is not an absolute path"),
    "a relative directory": (_set("rel/dir"), "recorded the directory 'rel/dir', which is not an absolute path"),
}


def shown_decoy(printed: list[str]) -> bool:
    return "DECOY" in "\n".join(printed)


def test_a_malformed_start_entry_reads_nothing() -> None:
    for label, (change, reason) in SHAPES.items():
        journal = Journal()
        try:
            attempt = journal.record(result_out="real output\n")
            real_directory = journal.root / PROJECT / attempt
            # The relative shape gets decoys where it points, too.
            (journal.cwd / "rel" / "dir").mkdir(parents=True)
            for name, text in DECOYS.items():
                (journal.cwd / "rel" / "dir" / name).write_text(text, encoding="utf-8")
            journal.edit_start(attempt, change)
            check(rj.verify_index(PROJECT, root=journal.root) == [], f"{label}: precondition, the recomputed index verifies")
            records = listed(journal)
            check(isinstance(records, list) and [r["attempt"] for r in records] == [attempt] and records[0]["result_present"] is False,
                  f"{label}: the attempt is listed, and its result is not present whatever the working directory holds: {records}")
            refusal = f"switchyard: attempt {attempt} {reason}, so it has no result or output to show"
            for kw in ({"attempt": attempt}, {"attempt": attempt, "output": True}, {"output": True}):
                got, printed, opened = journal.run(**kw)
                check(not shown_decoy(printed), f"{label} {kw}: no decoy from the working directory is shown: {printed}")
                check(outside(opened, [journal.index]) == [] and str(real_directory) not in " ".join(opened),
                      f"{label} {kw}: only the index is opened -- nothing where the command runs, nothing the record does not name: {opened}")
                check(got == 1 and printed[-1:] == [refusal],
                      f"{label} {kw}: refused with 1, saying what the record lacks: {got} {printed}")
            got, printed, opened = journal.run()
            check(got == 0 and len(printed) == 1 and printed[0].splitlines()[2:] == [f"      {reason}; nothing is read for it"],
                  f"{label}: the summary says the same in place of the directory line, and a clean index still exits 0: {got} {printed}")
            check(outside(opened, [journal.index]) == [], f"{label}: the summary opens only the index: {opened}")
        finally:
            journal.close()


def test_a_lost_or_garbled_start_entry_reads_nothing() -> None:
    # The realistic way: an index that lost the start line (truncated, deleted by hand) or holds it garbled. The finish
    # entry alone still makes the attempt; the chain is NOT recomputed, so the index reports a problem too.
    for label, damage in (("the start entry lost", lambda lines: lines[1:]),
                          ("the start entry garbled", lambda lines: ["{not json"] + lines[1:])):
        journal = Journal()
        try:
            attempt = journal.record(result_out="real output\n")
            journal.rewrite(damage(journal.entries()), rechain=False)
            check(rj.verify_index(PROJECT, root=journal.root) != [], f"{label}: precondition, the index reports the damage")
            records = listed(journal)
            check(isinstance(records, list) and [(r["attempt"], r["status"], r["directory"], r["result_present"]) for r in records] == [(attempt, "completed", "", False)],
                  f"{label}: a completed attempt with no directory and no result present: {records}")
            for kw in ({"attempt": attempt, "output": True}, {"output": True}):
                got, printed, opened = journal.run(**kw)
                check(got == 1 and not shown_decoy(printed)
                      and printed[-1:] == [f"switchyard: attempt {attempt} recorded no directory, so it has no result or output to show"],
                      f"{label} {kw}: refused with 1, and no decoy shown: {got} {printed}")
                check(outside(opened, [journal.index]) == [], f"{label} {kw}: only the index is opened: {opened}")
            got, printed, _ = journal.run()
            check(got == 1 and printed and "recorded no directory; nothing is read for it" in printed[0] and "journal integrity:" in printed[0],
                  f"{label}: the summary names the missing directory and the integrity problem, and exits 1 for the problem: {got} {printed}")
        finally:
            journal.close()


def test_valid_attempts_read_as_before() -> None:
    journal = Journal()
    try:
        first = journal.record(result_out="first output\n")
        second = journal.record(result_out="second output\n")
        directories = [journal.root / PROJECT / a for a in (first, second)]
        records = rj.attempts(PROJECT, root=journal.root)
        check([r["result_present"] for r in records] == [True, True] and [r["directory"] for r in records] == [str(d) for d in directories],
              f"each recorded directory, its result present: {records}")
        got, printed, opened = journal.run(attempt=first, output=True)
        result = json.loads(printed[0])
        check(got == 0 and result["attempt"] == first and result["status"] == "completed"
              and printed[1:] == ["--- stdout.log ---", "first output", "--- stderr.log ---", "err of first output"],
              f"a named attempt: its own result, then its stdout and stderr: {got} {printed}")
        check(sorted(set(opened) - {str(journal.index)}) == sorted(str(directories[0] / n) for n in ("result.json", "stderr.log", "stdout.log")),
              f"and it opens the index and exactly those three files of its own directory: {opened}")
        got, printed, opened = journal.run(output=True)
        summary = printed[0].splitlines()
        check(got == 0 and summary[2] == f"      {directories[0]}" and summary[4] == f"      {directories[1]}"
              and json.loads(printed[1])["attempt"] == second and printed[2:4] == ["--- stdout.log ---", "second output"]
              and not shown_decoy(printed) and outside(opened, [journal.index, directories[1]]) == [],
              f"--output alone: the summary with each directory, then the latest attempt's own files: {got} {printed} {opened}")
        interrupted = journal.record(result_out="partial\n", finish=False)
        got, printed, _ = journal.run(attempt=interrupted, output=True)
        check(got == 0 and json.loads(printed[0])["status"] == "running" and printed[1:3] == ["--- stdout.log ---", "partial"],
              f"an attempt still running shows its live result and partial output, as before: {got} {printed}")
    finally:
        journal.close()


def test_a_valid_attempt_beside_a_malformed_one() -> None:
    journal = Journal()
    try:
        good = journal.record(result_out="good output\n")
        bad = journal.record(result_out="bad output\n")
        journal.edit_start(bad, _drop("directory"))
        got, printed, opened = journal.run(attempt=good, output=True)
        check(got == 0 and json.loads(printed[0])["attempt"] == good and "good output" in printed
              and outside(opened, [journal.index, journal.root / PROJECT / good]) == [],
              f"the valid attempt still reads from its own directory: {got} {printed} {opened}")
        got, printed, _ = journal.run()
        summary = printed[0].splitlines()
        check(got == 0 and summary[1:] == [summary[1], f"      {journal.root / PROJECT / good}", summary[3], "      recorded no directory; nothing is read for it"]
              and good in summary[1] and bad in summary[3],
              f"the summary lists both, each as it is recorded: {printed}")
    finally:
        journal.close()


def test_the_formatter_on_its_own() -> None:
    # A record built without `attempts()` -- no result_present -- keeps its directory line, as before.
    text = rj.format_attempts(PROJECT, [{"attempt": "A1", "directory": "/x/a1", "status": "completed", "exit_status": 0}], [])
    check(text.splitlines()[2:] == ["      /x/a1"], f"an absolute directory is listed as recorded: {text}")
    text = rj.format_attempts(PROJECT, [{"attempt": "A1", "status": "completed", "exit_status": 0}], [])
    check(text.splitlines()[2:] == ["      recorded no directory; nothing is read for it"], f"a record with none says so: {text}")


def test_the_live_journal_is_never_the_one_read() -> None:
    journal = Journal()
    try:
        check(rj.journal_root() == journal.root and not str(journal.root).startswith(rj.DEFAULT_JOURNAL_ROOT),
              f"inside a case the journal root is the test-owned tree: {rj.journal_root()}")
    finally:
        journal.close()


def main() -> int:
    for name in sorted(n for n, v in globals().items() if n.startswith("test_") and callable(v)):
        globals()[name]()
    if FAILURES:
        print(f"rollout_log_attempt_directory_test: {len(FAILURES)} of {CHECKS} checks FAILED")
        return 1
    print(f"rollout_log_attempt_directory_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
