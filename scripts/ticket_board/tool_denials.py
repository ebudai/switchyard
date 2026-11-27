"""An agy role whose tool calls are all being denied: the Director told once, whatever the cause (SYRD-891).

On ocstab (2026-11-27) a Researcher on agy 1.5.17 had every `read_file` and `run_command` refused -- "tool call denied
by pre-tool hook: ", with no reason -- for two hours on OCSTAB-4, and nobody was told: its turns ended, its hooks said
idle, and nothing that watches a pane looks at what a tool call came back with. SYRD-890 found that incident's cause
(agy 1.5.17 reads a pre-tool answer with no decision as a deny). This is the detection, for any cause: agy's own
permission layer, a hook, or an expired login.

What agy does with a denied call, measured on the installed 1.5.17 (tests/fixtures/syrd890_probe): no PostToolUse hook
runs for it, but its transcript -- `transcriptPath`, which every hook payload names and the pane's SessionStart record
keeps -- has the call (`PLANNER_RESPONSE` with `tool_calls`) and then one step per call: `status: DONE` when it ran,
`status: ERROR` with agy's `error` when it did not. agy's own words for a refusal all say "denied" (a pre-tool hook's,
"tool call denied with reason", "User denied permission ...", "Action denied: ...", auto-denied commands).

Each listener pass, for every role the board has registered on agy: the role's session record (`<session_dir>/<target
>.json`, written by the pane's own SessionStart hook) names the transcript; its tail is read; a trailing run of at
least `THRESHOLD` refused calls, with nothing run since, is reported to the board (`note_tool_denials`), which keeps
one record per role and run -- the conversation and the step the run began at -- and queues one notice to the Director
naming the role, its ticket, how many calls and since when, agy's last refusal word for word -- one line, without
control characters (SYRD-893) -- and the likely cause:
- a pre-tool hook denying with no reason -- what agy 1.5.17 does to an answer with no decision (SYRD-890); first when it
  is there, since it is evidence about the call itself (SYRD-893);
- agy's own log saying the login lacked an OAuth scope (the 403 `fetch_config` line ocstab's log had) -- sign agy in
  again as the tenant owner; on ocstab it was there too, and the re-login did not help;
- agy's own permission layer -- the role runs without `--dangerously-skip-permissions`;
- otherwise agy's refusal as it is.
A call that runs ends the run. Nothing here asks agy or the provider anything, or changes any file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .report_closure import plain

#: Refused calls in a row, with none run since, before the Director is told: more than a model's retry of one call.
THRESHOLD = 3
#: How much of a transcript's end is read: enough for many turns, never the whole of a long conversation.
TAIL_BYTES = 1 << 20
#: agy's words when it refuses a tool call, whoever refused it.
DENIED = re.compile(r"\bdenied\b", re.IGNORECASE)
#: agy's own log when the login lacks a scope it needs (ocstab, 2026-11-27, every session start before the re-login).
SCOPE_WORDS = ("lacked OAuth scope", "insufficient authentication scopes")
TRANSCRIPT_NAMES = ("transcript_full.jsonl", "transcript.jsonl")
ERROR_CHARS = 300


def _tail(path: Path, size: int = TAIL_BYTES) -> list[str]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        end = handle.tell()
        handle.seek(max(0, end - size))
        data = handle.read()
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if end > size else lines  # the first line of a cut is partial


def streak(lines: Iterable[str]) -> dict[str, Any] | None:
    """The trailing run of refused tool calls in a transcript's lines, or None when the last call ran (or none was
    made). {"denied", "ran_before", "first_step", "first_at", "last_at", "last_error"}."""
    outcomes: list[tuple[Any, str, bool, str]] = []
    pending = 0
    for line in lines:
        try:
            step = json.loads(line)
        except ValueError:
            continue
        if not isinstance(step, dict):
            continue
        kind, status = step.get("type"), step.get("status")
        if kind == "PLANNER_RESPONSE":
            calls = step.get("tool_calls")
            pending = len(calls) if isinstance(calls, list) else 0
            continue
        if kind == "USER_INPUT":
            pending = 0
            continue
        if pending and status in ("DONE", "ERROR"):
            error = str(step.get("error") or "")
            outcomes.append((step.get("step_index"), str(step.get("created_at") or ""),
                             status == "ERROR" and bool(DENIED.search(error)), error))
            pending -= 1
    run: list[tuple[Any, str, bool, str]] = []
    for outcome in reversed(outcomes):
        if not outcome[2]:
            break
        run.insert(0, outcome)
    if not run:
        return None
    return {"denied": len(run), "ran_before": len(outcomes) - len(run), "first_step": run[0][0],
            "first_at": run[0][1], "last_at": run[-1][1], "last_error": run[-1][3][:ERROR_CHARS]}


def scope_line(transcript: Path) -> str:
    """agy's own log line saying the login lacked a scope, from the newest log of the home the transcript is in."""
    home = next((parent for parent in transcript.parents if parent.name == ".gemini"), None)
    if home is None:
        return ""
    logs = sorted((home / "antigravity-cli" / "log").glob("cli-*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return ""
    for line in reversed(_tail(logs[-1])):
        if any(word in line for word in SCOPE_WORDS):
            return line.strip()[:ERROR_CHARS]
    return ""


def one_line(text: str) -> str:
    """`text` as one line of a notice: no control characters (`plain`, as report_closure's notices are), and every run
    of whitespace, newlines and tabs included, one space (SYRD-893). agy's refusal is partly shaped by the role's own
    command. Whitespace is split first, so a tab or newline between two words stays a space between them."""
    return " ".join(plain(" ".join(text.split())).split())


def suspects(seen: dict[str, Any], scope: str) -> list[str]:
    """The likely causes, most likely first, each with what to do.

    A pre-tool hook's denial with no reason is evidence about the refused call itself, so it comes first; agy's 403
    scope line is a warning at session start, so it comes after it -- on ocstab both were there, and the re-login did
    not help (SYRD-893). With no such denial, the login comes first."""
    error = str(seen.get("last_error") or "")
    found = []
    login = (f"agy's own log says its login lacked an OAuth scope ({scope}): sign agy in again as the tenant's owner, "
             "then restart the role") if scope else ""
    empty_hook = error.rstrip().endswith("denied by pre-tool hook:")
    if empty_hook:
        found.append("a pre-tool hook denied with no reason, which is what agy 1.5.17 does to an answer with no "
                     "decision (SYRD-890): check that the role's ~/.gemini/config/hooks.json runs a release with that "
                     "fix (switchyard upgrade), then restart the role")
    if login:
        found.append(login)
    if not empty_hook and ("pre-tool hook" in error or "denied with reason" in error):
        found.append("a pre-tool hook refused it, for the reason quoted")
    if re.search(r"denied permission|auto-denied|Action denied", error, re.IGNORECASE):
        found.append("agy's own permission layer refused it: the role should run with --dangerously-skip-permissions")
    return found or ["a cause Switchyard does not recognise: agy's refusal is quoted as it is"]


def observe(session_dir: str, target: str, cache: dict | None = None) -> dict[str, Any] | None:
    """The role's run of refused calls, from its own session record's transcript, when it is long enough to report.
    `cache` keeps each answer against the record's and the transcript's mtime and size, so an unchanged transcript is
    not read again on the next pass."""
    safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in target)
    path = Path(session_dir) / f"{safe}.json"
    try:
        record_stat = path.stat()
        record = json.loads(path.read_text(encoding="utf-8"))
        payload = record.get("payload") if isinstance(record, dict) else None
        transcript = Path(str((payload or {}).get("transcriptPath") or ""))
        conversation = str(record.get("session_id") or "").strip()
        if transcript.name not in TRANSCRIPT_NAMES or not conversation or not transcript.is_file():
            return None
        transcript_stat = transcript.stat()
        key = (record_stat.st_mtime_ns, record_stat.st_size, transcript_stat.st_mtime_ns, transcript_stat.st_size)
        if cache is not None and cache.get(str(path), (None,))[0] == key:
            return cache[str(path)][1]
        seen = streak(_tail(transcript))
    except (OSError, ValueError, AttributeError):
        return None
    answer = _answer(seen, conversation, transcript)
    if cache is not None:
        cache[str(path)] = (key, answer)
    return answer


def _answer(seen: dict[str, Any] | None, conversation: str, transcript: Path) -> dict[str, Any] | None:
    if seen is None or seen["denied"] < THRESHOLD:
        return None
    try:
        scope = one_line(scope_line(transcript))
    except OSError:
        scope = ""
    seen = {**seen, "last_error": one_line(str(seen.get("last_error") or ""))}
    return {**seen, "conversation": conversation, "episode": f"{conversation}:{seen['first_step']}",
            "suspects": suspects(seen, scope)}


def _text(value: Any) -> str:
    """A column as text: a board whose encoding is SQL_ASCII returns bytes."""
    return bytes(value).decode("utf-8", "replace") if isinstance(value, (bytes, bytearray, memoryview)) else str(value or "")


def _present(listener: Any, conn: Any) -> bool:
    """Whether this board has the record yet: a listener can be newer than its board's migrations."""
    present = getattr(listener, "_tool_denials_present", None)
    if present is None:
        row = conn.execute("SELECT to_regprocedure('ticket_board.note_tool_denials(jsonb)') IS NOT NULL AS present"
                           ).fetchone()
        present = bool(row and (row["present"] if isinstance(row, dict) else row[0]))
        listener._tool_denials_present = present
    return present


def run(listener: Any, conn: Any) -> int:
    """One pass; returns how many notices the board queued. Never stops the listener. A board whose declared workflow
    runs no role on agy is asked nothing."""
    runtimes = getattr(listener, "role_runtimes", None)
    if not isinstance(runtimes, dict) or "agy" not in runtimes.values():
        return 0
    try:
        if not _present(listener, conn):
            return 0
        rows = conn.execute("SELECT role, actual_target, session_dir FROM ticket_board.role_runtime_assignments "
                            "WHERE runtime = 'agy' ORDER BY role").fetchall()
        report = {}
        for row in rows:
            role, target, session_dir = (row["role"], row["actual_target"], row["session_dir"]) \
                if isinstance(row, dict) else (row[0], row[1], row[2])
            seen = observe(_text(session_dir), _text(target), listener.__dict__.setdefault("_tool_denials_cache", {}))
            if seen is not None:
                report[_text(role)] = seen
        if not report:
            return 0
        row = conn.execute("SELECT ticket_board.note_tool_denials(%s::jsonb)", (json.dumps(report, sort_keys=True),)
                           ).fetchone()
        queued = int((next(iter(row.values())) if isinstance(row, dict) else row[0]) or 0) if row else 0
    except Exception as exc:  # logged; the notices this pass delivers matter more
        listener.logger.warning("Tool-denial pass failed: %s", exc)
        return 0
    if queued:
        listener.logger.info("Told the Director of %s agy role(s) whose tool calls are all denied: %s", queued,
                             sorted(report))
    return queued
