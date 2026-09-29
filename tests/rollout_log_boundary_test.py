#!/usr/bin/env python3
"""SYRD-439: the rollout log command, against the launcher it came out of.

`rollout_log_command` -- `switchyard rollout-log`, reading a project's rollout
journal -- moved unchanged into `scripts/rollout_log.py`; the launcher
re-exports it. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads no other Switchyard module -- not the journal, never the
  launcher; its defaults are the same, `print` the builtin.
- **Seams (rule 24):** it reads nothing from the launcher, as before. What it
  reads when it runs is the journal module, imported inside it exactly as
  before, so a suite that rebinds a journal function or `RESULT_NAME` there
  still reaches it: the stand-in cases below do.
- **Caller:** `switchyard_main` calls it by the launcher's name with the
  resolved project's slug; no other production module reads it.
- **The behaviour is the baseline's:** a synthetic journal with nothing
  recorded, one or two attempts, the latest or a named attempt, `--output`, an
  unknown attempt, an interrupted attempt, a missing result, a broken chain, an
  unreadable index line, output and a result that are not UTF-8, an attempt
  that recorded no directory; refusals and malformed records from the journal;
  and `switchyard rollout-log` through `switchyard_main`. `GOLDEN` below was
  produced by running the BASELINE launcher's own definition over the very
  cases embedded here (`gold439.py`), not typed; it is byte-identical under
  `env -i`, in a normal role pane, with another HOME, USER and COLUMNS, and
  under umask 077.

Nothing reaches the host: every journal is written into a test-owned tree
that `SWITCHYARD_ROLLOUT_JOURNAL_ROOT` points at, so the live journal is never
read. Spawns, every exec, signals, account and group lookups and socket
connections are refused for each case.
"""

from __future__ import annotations

import ast
import grp
import json
import os
import pwd
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import rollout_log as m  # noqa: E402

from launcher_main_view import launcher_body  # noqa: E402

CHECKS = 0
MOVED = ('rollout_log_command',)
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals (none).
SEAMS = {
}
#: Measured on the baseline launcher: every launcher definition outside it that names it, and how often.
DISPATCH = {'switchyard_main': {'rollout_log_command': 1}}
#: Measured on the baseline: every production module that reads it, and how (none).
READERS = {}
#: The BASELINE's own behaviour for the cases below (`gold439.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'journal: no journal at all': {'result': 0, 'calls': [], 'printed': ['switchyard: p439 rollout journal\n  no attempts recorded', 'switchyard: nothing has been recorded for p439 under TMP/journal/p439']},
    'journal: an empty index': {'result': 0, 'calls': [], 'printed': ['switchyard: p439 rollout journal\n  no attempts recorded', 'switchyard: nothing has been recorded for p439 under TMP/journal/p439']},
    'journal: one completed attempt': {'result': 0, 'calls': [], 'printed': ['switchyard: p439 rollout journal\n  0001-deploy  2026-01-01T00:00:00+00:00 completed (exit 0)\n      TMP/journal/p439/0001-deploy']},
    'journal: one completed attempt, with its output': {'result': 0, 'calls': [], 'printed': ['switchyard: p439 rollout journal\n  0001-deploy  2026-01-01T00:00:00+00:00 completed (exit 0)\n      TMP/journal/p439/0001-deploy', '{"status": "completed"}', '--- stdout.log ---', 'did it', '--- stderr.log ---', 'a warning']},
    'journal: the named attempt': {'result': 0, 'calls': [], 'printed': ['first result']},
    'journal: the named attempt, with its output': {'result': 0, 'calls': [], 'printed': ['first result', '--- stdout.log ---', 'one']},
    'journal: the latest of two, with its output': {'result': 0, 'calls': [], 'printed': ['switchyard: p439 rollout journal\n  0001-deploy  2026-01-01T00:00:00+00:00 failed (exit 3)\n      TMP/journal/p439/0001-deploy\n  0002-deploy  2026-01-02T00:00:00+00:00 completed (exit 0)\n      TMP/journal/p439/0002-deploy', 'second result', '--- stdout.log ---', 'two', '--- stderr.log ---', 'two err']},
    'journal: an attempt that is not recorded': {'result': 1, 'calls': [], 'printed': ['switchyard: p439 has no recorded attempt 0009-deploy']},
    'journal: an attempt that is not recorded, with output': {'result': 1, 'calls': [], 'printed': ['switchyard: p439 has no recorded attempt 0009-deploy']},
    'journal: an interrupted attempt': {'result': 0, 'calls': [], 'printed': ["switchyard: p439 rollout journal\n  0001-deploy  2026-01-01T00:00:00+00:00 interrupted\n      TMP/journal/p439/0001-deploy\n      result.json is missing from this attempt's directory", 'switchyard: attempt 0001-deploy recorded no result; it started at 2026-01-01T00:00:00+00:00 and never completed', '--- stdout.log ---', 'partial']},
    'journal: an interrupted attempt, named': {'result': 0, 'calls': [], 'printed': ['switchyard: attempt 0001-deploy recorded no result; it started at 2026-01-01T00:00:00+00:00 and never completed']},
    'journal: a finished attempt whose result is gone': {'result': 0, 'calls': [], 'printed': ["switchyard: p439 rollout journal\n  0001-deploy  2026-01-01T00:00:00+00:00 completed (exit 0)\n      TMP/journal/p439/0001-deploy\n      result.json is missing from this attempt's directory", 'switchyard: attempt 0001-deploy recorded no result; it started at 2026-01-01T00:00:00+00:00 and never completed', '--- stderr.log ---', 'only err']},
    'journal: a result that is a directory': {'result': 0, 'calls': [], 'printed': ["switchyard: p439 rollout journal\n  0001-deploy  2026-01-01T00:00:00+00:00 completed (exit 0)\n      TMP/journal/p439/0001-deploy\n      result.json is missing from this attempt's directory", 'switchyard: attempt 0001-deploy recorded no result; it started at 2026-01-01T00:00:00+00:00 and never completed']},
    'journal: output that is not UTF-8': {'result': 0, 'calls': [], 'printed': ['switchyard: p439 rollout journal\n  0001-deploy  2026-01-01T00:00:00+00:00 completed (exit 0)\n      TMP/journal/p439/0001-deploy', 'ok', '--- stdout.log ---', 'caf�', '--- stderr.log ---', 'fine']},
    'journal: a result that is not UTF-8': {'result': {'raised': 'UnicodeDecodeError', 'message': "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"}, 'calls': [], 'printed': ['switchyard: p439 rollout journal\n  0001-deploy  2026-01-01T00:00:00+00:00 completed (exit 0)\n      TMP/journal/p439/0001-deploy']},
    'journal: a broken chain': {'result': 1, 'calls': [], 'printed': ['switchyard: p439 rollout journal\n  0001-deploy  2026-01-01T00:00:00+00:00 completed (exit 0)\n      TMP/journal/p439/0001-deploy\n  journal integrity:\n    entry 2 (0001-deploy finish) follows <digest>, but the entry before it hashes to <digest>']},
    'journal: a broken chain, the attempt named': {'result': 1, 'calls': [], 'printed': ['{"status": "completed"}']},
    'journal: a broken chain, with output': {'result': 1, 'calls': [], 'printed': ['switchyard: p439 rollout journal\n  0001-deploy  2026-01-01T00:00:00+00:00 completed (exit 0)\n      TMP/journal/p439/0001-deploy\n  journal integrity:\n    entry 2 (0001-deploy finish) follows <digest>, but the entry before it hashes to <digest>', '{"status": "completed"}', '--- stdout.log ---', 'did it', '--- stderr.log ---', 'a warning']},
    'journal: an unreadable index line': {'result': 1, 'calls': [], 'printed': ['switchyard: p439 rollout journal\n  0001-deploy  2026-01-01T00:00:00+00:00 completed (exit 0)\n      TMP/journal/p439/0001-deploy\n  journal integrity:\n    entry 3 is not readable as JSON', '{"status": "completed"}', '--- stdout.log ---', 'did it', '--- stderr.log ---', 'a warning']},
    'journal: only an unreadable index line': {'result': 0, 'calls': [], 'printed': ['switchyard: p439 rollout journal\n  no attempts recorded\n  journal integrity:\n    entry 1 is not readable as JSON', 'switchyard: nothing has been recorded for p439 under TMP/journal/p439']},
    'journal: an attempt that recorded no directory, a result where the command runs': {'result': 0, 'calls': [], 'printed': ['switchyard: p439 rollout journal\n  0001-deploy  2026-01-01T00:00:00+00:00 completed (exit 0)', "the working directory's result", '--- stdout.log ---', 'cwd out']},
    'journal: an attempt that recorded no directory, nothing where the command runs': {'result': 0, 'calls': [], 'printed': ["switchyard: p439 rollout journal\n  0001-deploy  2026-01-01T00:00:00+00:00 completed (exit 0)\n      result.json is missing from this attempt's directory", 'switchyard: attempt 0001-deploy recorded no result; it started at 2026-01-01T00:00:00+00:00 and never completed']},
    'stand-in: the journal functions are read when it runs': {'result': 0, 'calls': [['attempts', ['p439'], {}], ['verify_index', ['p439'], {}], ['format_attempts', ['p439', [{'attempt': 'A1', 'directory': 'TMP/a1', 'started_at': 'T0'}], []], {}]], 'printed': ['FORMATTED p439 1 []', 'R', '--- stdout.log ---', 'O']},
    'stand-in: the result name is read when it runs': {'result': 0, 'calls': [['attempts', ['p439'], {}], ['verify_index', ['p439'], {}]], 'printed': ['this one']},
    'stand-in: nothing recorded, problems found': {'result': 0, 'calls': [['attempts', ['p439'], {}], ['verify_index', ['p439'], {}], ['format_attempts', ['p439', [], ['entry 1 is broken']], {}], ['project_journal_dir', ['p439'], {}]], 'printed': ["FORMATTED p439 0 ['entry 1 is broken']", 'switchyard: nothing has been recorded for p439 under TMP/journal-dir/p439']},
    'stand-in: a record without a start time': {'result': 0, 'calls': [['attempts', ['p439'], {}], ['verify_index', ['p439'], {}]], 'printed': ['switchyard: attempt A1 recorded no result; it started at ? and never completed']},
    'stand-in: a record whose directory is null': {'result': 0, 'calls': [['attempts', ['p439'], {}], ['verify_index', ['p439'], {}]], 'printed': ['switchyard: attempt A1 recorded no result; it started at T0 and never completed']},
    'stand-in: a record without an attempt id': {'result': {'raised': 'KeyError', 'message': "'attempt'"}, 'calls': [['attempts', ['p439'], {}], ['verify_index', ['p439'], {}]], 'printed': []},
    'stand-in: the latest record without an attempt id': {'result': {'raised': 'KeyError', 'message': "'attempt'"}, 'calls': [['attempts', ['p439'], {}], ['verify_index', ['p439'], {}], ['format_attempts', ['p439', [{'attempt': 'A0'}, {'directory': 'TMP/a1'}], []], {}]], 'printed': ['FORMATTED p439 2 []']},
    'stand-in: records that name the attempt twice': {'result': 1, 'calls': [['attempts', ['p439'], {}], ['verify_index', ['p439'], {}]], 'printed': ['FIRST']},
    'stand-in: the attempts read refused': {'result': {'raised': 'PermissionError', 'message': 'syrd439 attempts refused'}, 'calls': [['attempts', ['p439'], {}]], 'printed': []},
    'stand-in: the integrity check refused': {'result': {'raised': 'PermissionError', 'message': 'syrd439 verify_index refused'}, 'calls': [['attempts', ['p439'], {}], ['verify_index', ['p439'], {}]], 'printed': []},
    'stand-in: the formatter fails': {'result': {'raised': 'ValueError', 'message': 'syrd439 format_attempts refused'}, 'calls': [['attempts', ['p439'], {}], ['verify_index', ['p439'], {}], ['format_attempts', ['p439', [], []], {}]], 'printed': []},
    'stand-in: a result that is a dangling link': {'result': 0, 'calls': [['attempts', ['p439'], {}], ['verify_index', ['p439'], {}]], 'printed': ['switchyard: attempt A1 recorded no result; it started at T0 and never completed']},
    'main: switchyard rollout-log': {'result': 0, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['P439'], {}], ['rollout_log_command', ['p439'], {'attempt': '', 'output': False}]], 'printed': ['switchyard: p439 rollout journal\n  0001-deploy  2026-01-01T00:00:00+00:00 completed (exit 0)\n      TMP/journal/p439/0001-deploy']},
    'main: switchyard rollout-log --attempt --output': {'result': 0, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['P439'], {}], ['rollout_log_command', ['p439'], {'attempt': '0001-deploy', 'output': True}]], 'printed': ['first result', '--- stdout.log ---', 'one']},
    'main: switchyard rollout-log, the case of the command ignored': {'result': 0, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p439'], {}], ['rollout_log_command', ['p439'], {'attempt': '', 'output': False}]], 'printed': ['switchyard: p439 rollout journal\n  no attempts recorded', 'switchyard: nothing has been recorded for p439 under TMP/journal/p439']},
    'main: switchyard rollout-log, the project unknown': {'result': {'raised': 'SystemExit', 'message': "switchyard: unknown project 'nobody'"}, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['nobody'], {}]], 'printed': []},
    'main: switchyard rollout-log, no project': {'result': {'raised': 'SystemExit', 'message': '2'}, 'calls': [['report_installed_release_version', [], {}]], 'printed': []},
    'the default printer is print': {'result': 0, 'calls': [], 'printed': ['switchyard: p439 rollout journal', '  0001-deploy  2026-01-01T00:00:00+00:00 completed (exit 0)', '      TMP/journal/p439/0001-deploy', '']},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold439.py` (which ran them on the baseline) ------------------------------------
# A case reads a synthetic rollout journal. Either the journal is real -- written by the case, hash chain included, in
# a test-owned tree that `SWITCHYARD_ROLLOUT_JOURNAL_ROOT` points at, and read by the real journal functions -- or the
# journal functions are stand-ins on `scripts.ticket_board.rollout_journal` (the module the command imports them from
# when it runs), for records and refusals a real journal cannot produce. Either way the root is test-owned, so the live
# journal under /var/lib is never read. `switchyard rollout-log` runs through `switchyard_main` with the project lookup
# and the release notice stood in on the launcher. Recorded, in order: every stood-in call with its arguments,
# everything printed, and the exit status or the exact exception.
DONE = {"status": "completed", "exit_status": 0}
ONE = [{"id": "0001-deploy", "finish": DONE, "result": '{"status": "completed"}\n\n', "stdout": "did it\n", "stderr": "a warning\n"}]
TWO = [{"id": "0001-deploy", "finish": {"status": "failed", "exit_status": 3}, "result": "first result", "stdout": "one\n"},
       {"id": "0002-deploy", "finish": DONE, "result": "second result", "stdout": "two\n", "stderr": "two err\n"}]
JOURNALS = {
    "no journal at all": {"journal": None},
    "an empty index": {"journal": []},
    "one completed attempt": {"journal": ONE},
    "one completed attempt, with its output": {"journal": ONE, "kw": {"output": True}},
    "the named attempt": {"journal": TWO, "kw": {"attempt": "0001-deploy"}},
    "the named attempt, with its output": {"journal": TWO, "kw": {"attempt": "0001-deploy", "output": True}},
    "the latest of two, with its output": {"journal": TWO, "kw": {"output": True}},
    "an attempt that is not recorded": {"journal": TWO, "kw": {"attempt": "0009-deploy"}},
    "an attempt that is not recorded, with output": {"journal": TWO, "kw": {"attempt": "0009-deploy", "output": True}},
    "an interrupted attempt": {"journal": [{"id": "0001-deploy", "stdout": "partial\n"}], "kw": {"output": True}},
    "an interrupted attempt, named": {"journal": [{"id": "0001-deploy"}], "kw": {"attempt": "0001-deploy"}},
    "a finished attempt whose result is gone": {"journal": [{"id": "0001-deploy", "finish": DONE, "stderr": "only err\n"}], "kw": {"output": True}},
    "a result that is a directory": {"journal": [{"id": "0001-deploy", "finish": DONE, "result": "DIR", "stdout": "DIR"}], "kw": {"output": True}},
    "output that is not UTF-8": {"journal": [{"id": "0001-deploy", "finish": DONE, "result": "ok", "stdout": b"caf\xe9\n", "stderr": "fine\n"}], "kw": {"output": True}},
    "a result that is not UTF-8": {"journal": [{"id": "0001-deploy", "finish": DONE, "result": b"\xff\xfe", "stdout": "x\n"}], "kw": {"output": True}},
    "a broken chain": {"journal": ONE, "tamper": "chain"},
    "a broken chain, the attempt named": {"journal": ONE, "tamper": "chain", "kw": {"attempt": "0001-deploy"}},
    "a broken chain, with output": {"journal": ONE, "tamper": "chain", "kw": {"output": True}},
    "an unreadable index line": {"journal": ONE, "tamper": "malformed", "kw": {"output": True}},
    "only an unreadable index line": {"journal": [], "tamper": "malformed"},
    "an attempt that recorded no directory, a result where the command runs": {"journal": [{"id": "0001-deploy", "finish": DONE, "nodir": True}],
                                                                              "cwd": {"result.json": "the working directory's result", "stdout.log": "cwd out\n"},
                                                                              "kw": {"output": True}},
    "an attempt that recorded no directory, nothing where the command runs": {"journal": [{"id": "0001-deploy", "finish": DONE, "nodir": True}], "kw": {"output": True}},
}
STANDS = {
    "the journal functions are read when it runs": {"records": [{"attempt": "A1", "directory": "@/a1", "started_at": "T0"}], "problems": [], "kw": {"output": True},
                                                    "files": {"a1/result.json": "R", "a1/stdout.log": "O"}},
    "the result name is read when it runs": {"records": [{"attempt": "A1", "directory": "@/a1"}], "problems": [], "rebind": {"RESULT_NAME": "other.json"}, "kw": {"attempt": "A1"},
                                            "files": {"a1/result.json": "not this", "a1/other.json": "this one"}},
    "nothing recorded, problems found": {"records": [], "problems": ["entry 1 is broken"]},
    "a record without a start time": {"records": [{"attempt": "A1", "directory": "@/a1"}], "problems": [], "kw": {"attempt": "A1"}},
    "a record whose directory is null": {"records": [{"attempt": "A1", "directory": None, "started_at": "T0"}], "problems": [], "kw": {"attempt": "A1", "output": True}},
    "a record without an attempt id": {"records": [{"directory": "@/a1"}], "problems": [], "kw": {"attempt": "A1"}},
    "the latest record without an attempt id": {"records": [{"attempt": "A0"}, {"directory": "@/a1"}], "problems": [], "kw": {"output": True}},
    "records that name the attempt twice": {"records": [{"attempt": "A1", "directory": "@/first"}, {"attempt": "A1", "directory": "@/second"}], "problems": ["p"],
                                           "kw": {"attempt": "A1"}, "files": {"first/result.json": "FIRST", "second/result.json": "SECOND"}},
    "the attempts read refused": {"raise": {"attempts": "PermissionError"}},
    "the integrity check refused": {"raise": {"verify_index": "PermissionError"}},
    "the formatter fails": {"records": [], "problems": [], "raise": {"format_attempts": "ValueError"}},
    "a result that is a dangling link": {"records": [{"attempt": "A1", "directory": "@/a1", "started_at": "T0"}], "problems": [], "kw": {"attempt": "A1", "output": True},
                                         "files": {"a1/result.json": "LINK", "a1/stdout.log": "LINK"}},
}
MAIN = {
    "switchyard rollout-log": {"argv": ["rollout-log", "P439"], "journal": ONE},
    "switchyard rollout-log --attempt --output": {"argv": ["rollout-log", "P439", "--attempt", "0001-deploy", "--output"], "journal": TWO},
    "switchyard rollout-log, the case of the command ignored": {"argv": ["ROLLOUT-LOG", "p439"], "journal": []},
    "switchyard rollout-log, the project unknown": {"argv": ["rollout-log", "nobody"], "journal": ONE},
    "switchyard rollout-log, no project": {"argv": ["rollout-log"], "journal": ONE},
}
CASES = {
    **{f"journal: {k}": {"call": "journal", **v} for k, v in JOURNALS.items()},
    **{f"stand-in: {k}": {"call": "stand", **v} for k, v in STANDS.items()},
    **{f"main: {k}": {"call": "main", **v} for k, v in MAIN.items()},
    "the default printer is print": {"call": "journal", "journal": ONE, "default_print": True},
}
FUNCTIONS = ("rollout_log_command",)


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s command, in a fresh test-owned tree that the journal root points at."""
    import contextlib, hashlib, io, json, os, re, shutil, tempfile
    from pathlib import Path as _P
    from types import SimpleNamespace
    from scripts.ticket_board import rollout_journal as rj
    calls: list = []
    printed: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd439-")).resolve()
    root = tmp / "journal"

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, _P):
            return "PATH " + norm(str(value))
        if isinstance(value, str):
            # The index lines carry the test-owned tree's path, so the chain's digests differ from run to run.
            return re.sub(r"\b[0-9a-f]{12}\b", "<digest>", value.replace(str(tmp), "TMP"))
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    def put(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        if content == "DIR":
            path.mkdir()
        elif content == "LINK":
            path.symlink_to(path.parent / "nowhere")
        else:
            path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))

    def write_journal(project, attempts):
        entries, previous = [], ""
        for n, a in enumerate(attempts):
            directory = root / project / a["id"]
            directory.mkdir(parents=True)
            start = {"schema": "switchyard.rollout-journal.v1", "attempt": a["id"], "event": "start", "at": f"2026-01-0{n + 1}T00:00:00+00:00",
                     "command": ["sh", "-c", "true"], "target_commit": "c" * 40, **({} if a.get("nodir") else {"directory": str(directory)})}
            entries.append(start)
            if a.get("finish"):
                entries.append({"schema": "switchyard.rollout-journal.v1", "attempt": a["id"], "event": "finish", "at": f"2026-01-0{n + 1}T00:05:00+00:00", **a["finish"]})
            for key, name in (("result", "result.json"), ("stdout", "stdout.log"), ("stderr", "stderr.log")):
                if a.get(key) is not None:
                    put(directory / name, a[key])
        lines = []
        for e in entries:
            e["previous"] = previous
            line = json.dumps(e, sort_keys=True, separators=(",", ":"))
            previous = hashlib.sha256(line.encode("utf-8")).hexdigest()
            lines.append(line)
        if spec.get("tamper") == "chain":
            lines[0] = lines[0].replace('"sh"', '"bash"')
        if spec.get("tamper") == "malformed":
            lines.insert(len(lines), "{not json")
        (root / project).mkdir(parents=True, exist_ok=True)
        (root / project / "index.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    journal_names = ("attempts", "verify_index", "format_attempts", "project_journal_dir", "RESULT_NAME")
    saved_rj = {n: getattr(rj, n) for n in journal_names}
    saved_t = {n: getattr(t, n) for n in ("rollout_log_command", "_resolve_switchyard_project", "report_installed_release_version")}
    saved_env = os.environ.get(rj.JOURNAL_ROOT_ENV)
    saved_cwd = os.getcwd()
    try:
        os.environ[rj.JOURNAL_ROOT_ENV] = str(root)
        command = getattr(holder, "rollout_log_command")
        workdir = tmp / "cwd"
        workdir.mkdir()
        for name, content in spec.get("cwd", {}).items():
            put(workdir / name, content)
        os.chdir(workdir)
        if spec.get("journal") is not None:
            write_journal("p439", spec["journal"])
        if spec["call"] == "stand":
            def stand(name, answer):
                def f(*args, **kwargs):
                    note(name, *args, **kwargs)
                    if name in spec.get("raise", {}):
                        raise {"PermissionError": PermissionError, "ValueError": ValueError}[spec["raise"][name]](f"syrd439 {name} refused")
                    return answer(*args)
                return f
            records = [{k: (v.replace("@", str(tmp)) if isinstance(v, str) else v) for k, v in r.items()} for r in spec.get("records", [])]
            for name, content in spec.get("files", {}).items():
                put(tmp / name, content)
            rj.attempts = stand("attempts", lambda project: records)
            rj.verify_index = stand("verify_index", lambda project: list(spec.get("problems", [])))
            rj.format_attempts = stand("format_attempts", lambda project, recs, problems: f"FORMATTED {project} {len(list(recs))} {list(problems)}")
            rj.project_journal_dir = stand("project_journal_dir", lambda project: tmp / "journal-dir" / project)
            for name, value in spec.get("rebind", {}).items():
                setattr(rj, name, value)
        kw = dict(spec.get("kw", {}))
        try:
            if spec["call"] == "main":
                def resolve(selection, **kwargs):
                    note("_resolve_switchyard_project", selection, **kwargs)
                    if selection.casefold() != "p439":
                        raise SystemExit(f"switchyard: unknown project {selection!r}")
                    return SimpleNamespace(slug="p439", name="P439", config_path=tmp / "p439.json")

                def through(project, **kwargs):
                    note("rollout_log_command", project, **kwargs)
                    return command(project, print_func=lambda s: printed.append(norm(s)), **kwargs)
                t._resolve_switchyard_project = resolve
                t.report_installed_release_version = lambda: note("report_installed_release_version")
                t.rollout_log_command = through
                with contextlib.redirect_stderr(io.StringIO()):
                    got = t.switchyard_main(list(spec["argv"]))
            elif spec.get("default_print"):
                shown = io.StringIO()
                with contextlib.redirect_stdout(shown):
                    got = command("p439", **kw)
                printed.extend(norm(shown.getvalue()).split("\n"))
            else:
                got = command("p439", print_func=lambda s: printed.append(norm(s)), **kw)
            result = norm(got)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            result = {"raised": type(exc).__name__, "message": norm(str(exc))}
        return {"result": result, "calls": calls, "printed": printed}
    finally:
        os.chdir(saved_cwd)
        for n, v in saved_rj.items():
            setattr(rj, n, v)
        for n, v in saved_t.items():
            setattr(t, n, v)
        if saved_env is None:
            os.environ.pop(rj.JOURNAL_ROOT_ENV, None)
        else:
            os.environ[rj.JOURNAL_ROOT_ENV] = saved_env
        shutil.rmtree(tmp)
# ----------------------------------------------------------------------------------------------------------------------


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


class patched:
    def __init__(self, target: object, **values: object) -> None:
        self.target, self.values = target, values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.target, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.target, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.target, name, value)


def refuse(what: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{what} was called: {args} {kwargs}")
    return refused


EXECS = ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe")


class contained:
    """Spawns, every exec, signals, connections and real account/group lookups refused."""

    def __enter__(self) -> None:
        self.parts = [patched(subprocess, run=refuse("subprocess.run"), Popen=refuse("subprocess.Popen")),
                      patched(os, kill=refuse("os.kill"), system=refuse("os.system"), **{name: refuse(f"os.{name}") for name in EXECS}),
                      patched(pwd, getpwnam=refuse("pwd.getpwnam"), getpwuid=refuse("pwd.getpwuid")),
                      patched(grp, getgrgid=refuse("grp.getgrgid"), getgrnam=refuse("grp.getgrnam")),
                      patched(socket.socket, connect=refuse("socket.connect"), connect_ex=refuse("socket.connect_ex"))]
        for part in self.parts:
            part.__enter__()

    def __exit__(self, *exc: object) -> None:
        for part in reversed(self.parts):
            part.__exit__(*exc)


def run(holder: object, spec: dict) -> dict:
    with contained():
        return json.loads(json.dumps(run_case(t, holder, spec, REACHED)))


def test_the_guard_itself_refuses_a_spawn_an_exec_a_lookup_and_a_connection() -> None:
    attempts = [lambda: subprocess.run(["true"]), lambda: subprocess.Popen(["true"]), lambda: os.kill(os.getpid(), 0), lambda: os.system("true"),
                lambda: pwd.getpwuid(0), lambda: grp.getgrgid(0), lambda: socket.socket().connect(("127.0.0.1", 9)),
                *(lambda name=name: getattr(os, name)("true", ["true"]) for name in EXECS)]
    for attempt in attempts:
        with contained():
            try:
                attempt()
            except AssertionError as exc:
                refused = " was called: " in str(exc)
            else:
                refused = False
        check(refused, "the guard refuses a spawn, every exec, a signal, an account or group lookup and a connection")


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_only_where_its_defaults_come_from() -> None:
    result = python("import sys, scripts.rollout_log as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module -- not the journal, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.rollout_log", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.rollout_log")):
        result = python("import builtins, importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.rollout_log as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "[f'{k}={v.default!r}' for k, v in inspect.signature(m.rollout_log_command).parameters.items() "
                        "if v.default is not inspect.Parameter.empty and v.default is not builtins.print], "
                        "inspect.signature(m.rollout_log_command).parameters['print_func'].default is builtins.print, "
                        "not hasattr(m, 'launcher') and not hasattr(m, 'team_launcher') and not hasattr(m, 'attempts') and not hasattr(m, 'RESULT_NAME'))")
        check(result.stdout.strip() == "True [\"attempt=''\", 'output=False'] True True",
              f"{' then '.join(order)}: one object, the same defaults, the builtin printer, and nothing of the journal bound at load: {result.stdout}{result.stderr[-600:]}")
    import pathlib
    check(m.Path is Path is pathlib.Path and t.Path is m.Path, "the one standard-library name is the module's own, the very object the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "rollout_log.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through = sorted({x.attr for x in ast.walk(node) if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher"})
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        check(SEAMS.get(name, {}) == {} and through == []
              and imports == ["from scripts.ticket_board.rollout_journal import RESULT_NAME, attempts, format_attempts, project_journal_dir, verify_index"]
              and ast.unparse(node.body[first]) == imports[0],
              f"{name}: reads nothing from the launcher, as before, and keeps its one call-time import of the journal, first thing: {through} {imports}")
        journal = {"RESULT_NAME", "attempts", "format_attempts", "project_journal_dir", "verify_index"}
        stores = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)} & journal)
        check(stores == [], f"{name}: nothing rebinds a journal name it imports: {stores}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "from pathlib import Path"] and not [n for n in tree.body if isinstance(n, ast.If)],
          f"the standard library only, nothing for annotations: {top}")
    runtime = [ast.unparse(x) for x in ast.walk(tree) if isinstance(x, (ast.Import, ast.ImportFrom)) and "team_launcher" in ast.unparse(x)]
    check(runtime == [], f"the launcher is never imported: {runtime}")
    names = [n.name if isinstance(n, (ast.FunctionDef, ast.ClassDef)) else ast.unparse(n)[:40] for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the one function, and nothing else: {names}")


def test_the_launcher_reexports_the_command_and_its_dispatcher_reaches_it_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.rollout_log"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the command, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read it")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"switchyard_main", "_resolve_switchyard_project", "_build_switchyard_rollout_log_parser",
                                        "report_installed_release_version", "publication_status_command"} <= defined | exported,
          "the launcher defines none of it, and keeps its dispatcher and the dispatcher's neighbours, its own or re-exported")
    uses: dict = {}
    for fn in launcher_body(ROOT, tree):
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"switchyard_main calls it by its launcher global, exactly as often as before: {uses}")
    past = sorted(ast.unparse(x) for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr in MOVED)
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(past == [] and loose == [], f"and nothing reaches past the launcher's name, or reads it at module level: {past} {loose}")
    check(READERS == {}, f"no other production module reads it: {READERS}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        for holder in (m, t):
            got = run(holder, spec)
            check(got == GOLDEN[label], f"{label} ({holder.__name__}): the baseline's answer, every call in order: {got}")


def test_the_rules_hold_in_the_measured_record() -> None:
    def result(label):
        return GOLDEN[label]["result"]

    def shown(label):
        return GOLDEN[label]["printed"]

    def steps(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    summary = "switchyard: p439 rollout journal\n  no attempts recorded"
    nothing = "switchyard: nothing has been recorded for p439 under TMP/journal/p439"
    check(shown("journal: no journal at all") == shown("journal: an empty index") == [summary, nothing]
          and result("journal: no journal at all") == result("journal: an empty index") == 0
          and result("journal: only an unreadable index line") == 0 and shown("journal: only an unreadable index line")[1] == nothing
          and result("stand-in: nothing recorded, problems found") == 0,
          "nothing recorded: the summary, where the journal would be, and 0 -- even when the index has problems")
    check(result("journal: one completed attempt") == 0 and len(shown("journal: one completed attempt")) == 1
          and result("journal: a broken chain") == 1 and "journal integrity:" in shown("journal: a broken chain")[0]
          and result("journal: a broken chain, the attempt named") == 1 and result("journal: an unreadable index line") == 1,
          "by default the summary alone; the exit status is 1 whenever the index has problems, whatever else is shown")
    check(shown("journal: the latest of two, with its output")[1:] == ["second result", "--- stdout.log ---", "two", "--- stderr.log ---", "two err"]
          and shown("journal: the named attempt") == ["first result"]
          and shown("journal: the named attempt, with its output") == ["first result", "--- stdout.log ---", "one"],
          "--output alone shows the latest attempt after the summary; a named attempt is shown alone; each log only if present, stdout first")
    check(shown("journal: an attempt that is not recorded") == shown("journal: an attempt that is not recorded, with output") == ["switchyard: p439 has no recorded attempt 0009-deploy"]
          and result("journal: an attempt that is not recorded") == 1,
          "an attempt that is not recorded is refused, word for word, with 1")
    check(shown("journal: one completed attempt, with its output")[1] == '{"status": "completed"}'
          and shown("journal: an interrupted attempt, named") == ["switchyard: attempt 0001-deploy recorded no result; it started at 2026-01-01T00:00:00+00:00 and never completed"]
          and shown("stand-in: a record without a start time") == ["switchyard: attempt A1 recorded no result; it started at ? and never completed"]
          and all(result(k) == 0 and "recorded no result" in " ".join(shown(k)) for k in ("journal: a finished attempt whose result is gone", "journal: a result that is a directory",
                                                                                       "stand-in: a result that is a dangling link", "stand-in: a record whose directory is null")),
          "the result is shown trimmed; with no result file it says when the attempt started ('?' if unknown), and that is not an error")
    check(shown("journal: output that is not UTF-8")[3] == "caf�"
          and result("journal: a result that is not UTF-8") == {"raised": "UnicodeDecodeError", "message": "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"},
          "a log that is not UTF-8 is shown with replacements; a result that is not UTF-8 raises")
    check(shown("journal: an attempt that recorded no directory, a result where the command runs")[1:] == ["the working directory's result", "--- stdout.log ---", "cwd out"]
          and "recorded no result" in shown("journal: an attempt that recorded no directory, nothing where the command runs")[1],
          "an attempt that recorded no directory is read relative to where the command runs (the baseline's behaviour, kept)")
    check(steps("stand-in: the journal functions are read when it runs") == ["attempts", "verify_index", "format_attempts"]
          and steps("stand-in: nothing recorded, problems found") == ["attempts", "verify_index", "format_attempts", "project_journal_dir"]
          and shown("stand-in: the result name is read when it runs") == ["this one"]
          and shown("stand-in: records that name the attempt twice") == ["FIRST"],
          "the journal's functions and result name are the journal module's when it runs; the first record naming the attempt wins")
    check(result("stand-in: the attempts read refused")["raised"] == result("stand-in: the integrity check refused")["raised"] == "PermissionError"
          and steps("stand-in: the attempts read refused") == ["attempts"]
          and result("stand-in: the formatter fails")["raised"] == "ValueError"
          and result("stand-in: a record without an attempt id") == result("stand-in: the latest record without an attempt id") == {"raised": "KeyError", "message": "'attempt'"},
          "a refusal or a malformed record is raised, never swallowed")
    check(GOLDEN["main: switchyard rollout-log --attempt --output"]["calls"][1:] == [["_resolve_switchyard_project", ["P439"], {}], ["rollout_log_command", ["p439"], {"attempt": "0001-deploy", "output": True}]]
          and GOLDEN["main: switchyard rollout-log"]["calls"][2] == ["rollout_log_command", ["p439"], {"attempt": "", "output": False}]
          and result("main: switchyard rollout-log, the case of the command ignored") == 0
          and steps("main: switchyard rollout-log, the project unknown") == ["report_installed_release_version", "_resolve_switchyard_project"]
          and result("main: switchyard rollout-log, no project") == {"raised": "SystemExit", "message": "2"},
          "switchyard rollout-log: the project resolved, the command called by the launcher's name with the entry's slug, --attempt and --output")
    check(shown("the default printer is print")[:3] == ["switchyard: p439 rollout journal", "  0001-deploy  2026-01-01T00:00:00+00:00 completed (exit 0)", "      TMP/journal/p439/0001-deploy"],
          "the default printer is print")


def test_every_launcher_seam_is_reached() -> None:
    # It reads nothing from the launcher; what it reaches when it runs is the journal module, stood in there for the
    # refusals and malformed records, and the dispatcher's project lookup and release notice, stood in on the launcher.
    check(SEAMS == {} and {"attempts", "verify_index", "format_attempts", "project_journal_dir", "_resolve_switchyard_project",
                           "report_installed_release_version", "rollout_log_command"} <= REACHED,
          f"no launcher seam to read, and every stood-in step reached: {sorted(REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_command_and_its_dispatcher_reaches_it_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"rollout_log_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
