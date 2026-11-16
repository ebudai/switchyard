#!/usr/bin/env python3
"""SYRD-817: every JSON option of `ticket-board-write`, and a workflow action's prose, reach the board from a file or
standard input.

`workflow-action --payload-json` is the only way to take a declared action with no subcommand of its own, and took its
payload only inline: `-` failed as invalid JSON, so a reason of several paragraphs had to sit in the shell command, which
the shell guard refuses (SYRD-753). Now `--payload-json`, `--patch-json` and `edit-fields --json` each take a file
(`-file PATH`) or standard input (`-`), and a payload's `reason` or `text` can come from its own option, merged in.

Each form runs the real `ticket-board-write` as a child process, its stdin a pipe as a heredoc gives it, against a
production-built board (schema.sql, roles, rbac.sql, every migration; the real HTTP server), and the board's own record
is read back: the text it stores is the text that was written, byte for byte. Each refusal leaves the ticket unchanged.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import user_review_serial_release_test as urs  # noqa: E402 - its production-built board and helpers

CHECKS = 0
#: What a shell would mangle, inline: substitutions, variables, both quotes, a leading-dash line, paragraphs.
PROSE = ("Kick it back: `write_cli.main()` drops $HOME and $(hostname) -- \"double\" and 'single' quotes.\n\n"
         "--reason-file /etc/passwd\nUnicode ✓ — and trailing spaces   \n")


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def prose(tag: str) -> str:
    return f"[{tag}] {PROSE}"


def json_text(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


class Cli:
    """`ticket-board-write`, as a pane runs it, at one board."""

    def __init__(self, board, scratch: Path) -> None:
        self.board, self.scratch, self.files = board, scratch, 0

    def file(self, text: str) -> str:
        self.files += 1
        path = self.scratch / f"in-{self.files}"
        path.write_bytes(text.encode("utf-8"))
        return str(path)

    def __call__(self, role: str, *argv: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        command = [sys.executable, str(ROOT / "scripts" / "ticket-board-write"), "--board-url", self.board.base,
                   f"--write-token={self.board.server.write_token}", "--caller-role", role, *argv]
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.scratch), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        if stdin is None:
            return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=60)
        return subprocess.run(command, input=stdin.encode("utf-8"), capture_output=True, env=env, timeout=60)


def said(done: subprocess.CompletedProcess) -> str:
    out = done.stdout + done.stderr
    return (out.decode("utf-8", "replace") if isinstance(out, bytes) else out).strip()[-400:]


def test_every_form_delivers_exactly() -> None:
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from temporary_cluster import temporary_cluster

    # UTF-8, as a production board's cluster is: prose is not ASCII, and jsonb refuses \u escapes under SQL_ASCII.
    with temporary_cluster(prefix="syrd817-", shutdown="immediate", initdb_args=("--username=postgres", "--encoding=UTF8")) as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd817.") as raw:
        admin = urs.build_board(fixture, cluster, ROOT, "board")
        board = urs.Board(fixture, cluster, admin, "board", "a")
        cli = Cli(board, Path(raw))
        try:
            board.app.apply_workflow(urs.workflow(ROOT), expected_revision=0, dry_run=False, caller_role="director")
            T = "PGU-1"
            fixture.seed_postgres_ticket(admin, T, title=T, state="analysis", assignee="director", commit_exempt=True)
            board.act(T, "route", "director", state="in_progress", assignee="main")

            def ticket() -> dict:
                return board.ticket_json(T)

            def last_comment() -> dict:
                return (ticket().get("comments") or [{}])[-1]

            # request_dependency: a declared action taking a role and a reason, so the prose sits beside a field.
            waits = {
                "--payload-json - (stdin)": (("--payload-json", "-"), json_text({"role": "director", "reason": prose("a")}), "a"),
                "--payload-json-file PATH": (("--payload-json-file", cli.file(json_text({"role": "director", "reason": prose("b")}))), None, "b"),
                "--payload-json-file - (stdin)": (("--payload-json-file", "-"), json_text({"role": "director", "reason": prose("c")}), "c"),
                "--reason-file - beside --payload-json": (("--payload-json", '{"role": "director"}', "--reason-file", "-"), prose("d"), "d"),
                "--reason-file PATH": (("--payload-json", '{"role": "director"}', "--reason-file", cli.file(prose("e"))), None, "e"),
                "--reason - (stdin)": (("--payload-json", '{"role": "director"}', "--reason", "-"), prose("f"), "f"),
            }
            for form, (argv, stdin, tag) in waits.items():
                done = cli("main", "workflow-action", T, "request_dependency", *argv, stdin=stdin)
                state = ticket()
                check(done.returncode == 0 and state.get("awaiting_role") == "director"
                      and last_comment().get("text") == prose(tag) and last_comment().get("who") == "main",
                      f"{form}: the wait is recorded, and its reason stored exactly: rc={done.returncode} "
                      f"awaiting={state.get('awaiting_role')!r} stored={last_comment().get('text')!r} {said(done)}")
                board.act(T, "clear_awaiting_role", "director")

            for form, argv, stdin, tag in (("--text-file PATH", ("--text-file", cli.file(prose("g"))), None, "g"),
                                           ("--text - (stdin)", ("--text", "-"), prose("h"), "h")):
                done = cli("director", "workflow-action", T, "add_comment", *argv, stdin=stdin)
                stored = last_comment().get("text")
                # The board keeps a comment as it keeps any (its trailing whitespace trimmed): the same text through
                # the long-standing `add-comment --text-file` is the measure.
                cli("director", "add-comment", T, "--text-file", cli.file(prose(tag)))
                check(done.returncode == 0 and stored == last_comment().get("text") and stored.startswith(f"[{tag}] Kick")
                      and "$(hostname)" in stored and "\n\n--reason-file /etc/passwd\n" in stored,
                      f"{form}: the comment is stored as add-comment --text-file stores it: {stored!r} "
                      f"vs {last_comment().get('text')!r} {said(done)}")

            for form, argv, stdin, tag in (
                    ("edit-fields --json-file PATH", ("--json-file", cli.file(json_text({"implementation": prose("i")}))), None, "i"),
                    ("edit-fields --json-file - (stdin)", ("--json-file", "-"), json_text({"implementation": prose("j")}), "j"),
                    ("edit-fields --json - (stdin)", ("--json", "-"), json_text({"implementation": prose("k")}), "k")):
                done = cli("director", "edit-fields", T, *argv, stdin=stdin)
                check(done.returncode == 0 and ticket().get("implementation") == prose(tag),
                      f"{form}: the field is stored exactly: {ticket().get('implementation')!r} {said(done)}")

            for form, argv, stdin, value in (
                    ("set-workflow-flags --patch-json-file - (stdin)", ("--patch-json-file", "-"), '{"needs_user_signoff": true}', True),
                    ("set-workflow-flags --patch-json-file PATH", ("--patch-json-file", cli.file('{"needs_user_signoff": false}')), None, False)):
                done = cli("director", "set-workflow-flags", T, *argv, stdin=stdin)
                check(done.returncode == 0 and ticket().get("needs_user_signoff") is value,
                      f"{form}: the flag is set: {ticket().get('needs_user_signoff')!r} {said(done)}")

            # Refusals: each says why, and the ticket is as it was.
            before = ticket()
            refusals = {
                "both payload forms": (("workflow-action", T, "request_dependency", "--payload-json", "{}",
                                        "--payload-json-file", cli.file("{}")), None, "mutually exclusive"),
                "a reason given twice": (("workflow-action", T, "request_dependency", "--payload-json",
                                          json_text({"role": "director", "reason": "in the JSON"}), "--reason-file", "-"),
                                         "and by --reason-file", "give the reason once"),
                "two readers of standard input": (("workflow-action", T, "request_dependency", "--payload-json", "-",
                                                   "--reason", "-"), '{"role": "director"}', "both read standard input"),
                "both edit-fields forms": (("edit-fields", T, "--json", "{}", "--json-file", cli.file("{}")), None,
                                           "mutually exclusive"),
                "a payload that is not an object": (("workflow-action", T, "request_dependency", "--payload-json-file",
                                                     cli.file("[1]")), None, "must be a JSON object"),
                "a payload that is not JSON": (("workflow-action", T, "request_dependency", "--payload-json-file", "-"),
                                               prose("not json"), "--payload-json: not valid JSON"),
            }
            for form, (argv, stdin, why) in refusals.items():
                done = cli("main", *argv, stdin=stdin)
                after = ticket()
                check(done.returncode != 0 and why in said(done)
                      and after.get("comments") == before.get("comments") and after.get("awaiting_role") == before.get("awaiting_role"),
                      f"{form}: refused ({why!r}), and nothing written: rc={done.returncode} {said(done)}")
        finally:
            board.close()


def test_help_teaches_the_heredoc() -> None:
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "ticket-board-write"), "workflow-action", "--help"],
                          capture_output=True, text=True, timeout=60, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    check(done.returncode == 0 and "--reason-file - <<'EOF'" in done.stdout and "--payload-json-file" in done.stdout
          and '"$(' not in done.stdout, f"workflow-action --help shows the quoted heredoc, never a substitution: {done.stdout}")


def test_the_guard_reads_the_new_options() -> None:
    """The shell guard reads `edit-fields --json` as prose, its file form as safe, and what the skill teaches passes."""
    import re

    from shell_guard_test import load_guard

    guard = load_guard(ROOT)
    heredoc = "\n`id -u` and $HOME\nEOF\n"
    refused = {
        "edit-fields --json with a command substitution": 'ticket-board-write edit-fields PGU-1 --json "$(date)"',
        "edit-fields --json-file - from an unquoted heredoc": "ticket-board-write edit-fields PGU-1 --json-file - <<EOF" + heredoc,
        "workflow-action --reason-file - from an unquoted heredoc":
            "ticket-board-write workflow-action PGU-1 a --reason-file - <<EOF" + heredoc,
        # `switchyard`'s --json is a switch: it must not take the next option as its value and skip reading it.
        "switchyard --json, then a substituted --reason": 'switchyard roles --json --reason "$(date)"',
    }
    for form, command in refused.items():
        check(guard.problems_in(command), f"refused: {form}")
    allowed = {
        "edit-fields --json-file - from a quoted heredoc": "ticket-board-write edit-fields PGU-1 --json-file - <<'EOF'" + heredoc,
        "workflow-action --reason-file - from a quoted heredoc":
            "ticket-board-write workflow-action PGU-1 a --payload-json '{\"role\": \"director\"}' --reason-file - <<'EOF'" + heredoc,
        "switchyard --json alone": "switchyard roles --json",
    }
    for form, command in allowed.items():
        check(not guard.problems_in(command), f"allowed: {form}: {guard.problems_in(command)}")
    for name in ("skills/switchyard-director/SKILL.md", "docs/onboarding/switchyard-director-guide.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        taught = re.findall(r"^ticket-board-write workflow-action <id> \S+ \\\n  [^\n]*<<'EOF'[^\n]*\n.*?\nEOF\n", text, re.M | re.S)
        check(len(taught) >= 3, f"{name} teaches the heredoc form: {len(taught)} blocks")
        for block in taught:
            runnable = re.sub(r"<(id|the commit the ticket carries)>", "x", block.replace("\\\n", ""))
            check(not guard.problems_in(runnable), f"{name}: the taught block passes the guard: {block!r} "
                                                   f"{guard.problems_in(runnable)}")


def main() -> int:
    test_help_teaches_the_heredoc()
    test_the_guard_reads_the_new_options()
    test_every_form_delivers_exactly()
    print(f"write_payload_files_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
