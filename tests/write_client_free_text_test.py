#!/usr/bin/env python3
"""SYRD-196: prose reaches the board literally, and executes nothing.

On SYRD-195 a review comment was posted with its operative phrase missing,
because the author's shell executed the backticked phrase instead of passing
it. `ticket-board-write` accepted free text only as a shell argument, so the
only defence was every author remembering a quoting idiom -- a safety property
that depends on memory, which is the shape SYRD-194 exists to argue against.

Every free-text option now has two companions that cannot be interpreted:
`--<name>-file PATH` and `--<name> -`. This drives the real CLI against a real
board and asserts the stored text is byte-identical to the file -- including
backticks, dollar expansions, quotes, newlines, trailing spaces and a line that
begins with a dash, which argparse would otherwise read as a flag.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus

isolate_tmux_bus()
import ticket_board_write_api_test as t
from scripts.ticket_board import write_client as wc
from temporary_cluster import temporary_cluster

#: Everything that has ever gone wrong in a shell, in one comment body. The
#: backticked phrase is the literal one from the SYRD-195 incident.
ADVERSARIAL = (
    "Backticks: `switchyard upgrade syrd` and `id -u`\n"
    "Substitution: $(id -u) and `hostname`\n"
    "Dollars: $HOME ${PATH} $$ $(( 1 + 1 ))\n"
    "Quotes: \"double\" 'single' and a \\backslash\n"
    "A line that starts like a flag:\n"
    "--text-file /etc/passwd\n"
    "Trailing spaces follow this colon:   \n"
    "Semicolons; pipes | ampersands && redirects > /tmp/nope\n"
    "Unicode: \u2713 \u2014 \u00a9\n"
)

#: What the board stores: it strips exactly one trailing newline from a
#: comment body. Pre-existing, reasonable, and not this change's to alter --
#: named here so the test asserts the real guarantee instead of pretending
#: the byte count is unchanged end to end.
ON_BOARD = ADVERSARIAL[:-1]

SENTINEL = ROOT / "tests" / ".syrd196-should-never-exist"


def resolve(argv: list[str]):
    """Parse and resolve exactly as the CLI does, and nothing more.

    Deliberately NOT a subprocess of the real entry point. `--board-url` does
    not govern where a write goes -- an explicit board URL still resolves to a
    production socket -- and I put a stray comment on another project's live
    board finding that out. A test for text handling has no business anywhere
    near that resolution, so it exercises the parser and the resolver, which is
    what SYRD-196 changed, and then writes through a client pinned to the
    disposable board with the socket path explicitly disabled.
    """
    parser = wc._build_parser()
    args = parser.parse_args(argv)
    wc.resolve_free_text_arguments(args, parser)
    return args


def resolve_error(argv: list[str]) -> str:
    """The message argparse would print, without exiting the test."""
    parser = wc._build_parser()
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stderr(buffer):
            args = parser.parse_args(argv)
            wc.resolve_free_text_arguments(args, parser)
    except SystemExit:
        return buffer.getvalue()
    return ""


def client_for(base: str, caller: str = "ops"):
    #: socket_path=None, so this can only ever reach the disposable board above.
    return wc.TicketBoardWriteClient(base, caller, socket_path=None,
                                     write_token=t.TEST_WRITE_TOKEN or "")


def comments_of(app, ticket_id: str) -> list[str]:
    return [str(c.get("text") or "") for c in (app.get_ticket(ticket_id).get("comments") or [])]


def main() -> int:
    checks = 0
    body_path = ROOT / "tests" / ".syrd196-body.txt"
    body_path.write_text(ADVERSARIAL, encoding="utf-8")
    if SENTINEL.exists():
        SENTINEL.unlink()
    try:
        with temporary_cluster(prefix="free-text-", shutdown="immediate") as cluster:
            root, sock, port = cluster.root, cluster.socket_dir, cluster.port
            db = "write_client_free_text_test"
            admin = t.conninfo(sock, port, db)
            t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
            t.psql(admin, (ROOT / "scripts/ticket_board/schema.sql").read_text())
            t.create_roles(admin)
            t.psql(admin, t.RBAC_PATH.read_text())
            t.seed_postgres_ticket(admin, "PGU-1", title="Prose", state="in_progress", assignee="ops")
            (root / "frames").mkdir(exist_ok=True)
            (root / "assets").mkdir(exist_ok=True)
            app = t.TicketBoardApp(
                root / "frames", root / "assets", project="cerulean", ticket_prefix="PGU",
                database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE),
            )
            server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
            t.TEST_WRITE_TOKEN = server.write_token
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                client = client_for(base)

                # 1. --text-file: the bytes in the file are the bytes on the board.
                args = resolve(["add-comment", "PGU-1", "--text-file", str(body_path)])
                assert args.text == ADVERSARIAL, "resolution did not preserve the file"
                checks += 1
                client.add_comment("PGU-1", text=args.text)
                stored = comments_of(app, "PGU-1")
                exact = [c for c in stored if c == ON_BOARD]
                assert len(exact) == 1, [len(c) for c in stored]
                checks += 1

                # Byte-exact apart from the board's own single trailing-newline
                # strip, which is pre-existing behaviour and not this change's to
                # alter. Asserted precisely rather than glossed: exactly one "\n"
                # comes off the end and nothing else moves -- the trailing SPACES
                # in the middle of the body survive, which is the harder case.
                assert exact[0] + "\n" == ADVERSARIAL
                assert len(exact[0]) == len(ADVERSARIAL) - 1
                assert exact[0].encode("utf-8") == ON_BOARD.encode("utf-8")
                checks += 3

                # 2. --text - : the same content, through standard input.
                stdin_backup = sys.stdin
                sys.stdin = io.TextIOWrapper(io.BytesIO(ADVERSARIAL.encode("utf-8")), encoding="utf-8")
                try:
                    args = resolve(["add-comment", "PGU-1", "--text", "-"])
                finally:
                    sys.stdin = stdin_backup
                assert args.text == ADVERSARIAL, "stdin did not preserve the content"
                client.add_comment("PGU-1", text=args.text)
                assert len([c for c in comments_of(app, "PGU-1") if c == ON_BOARD]) == 2
                checks += 2

                # 3. Nothing executed. Every substitution above would have left a
                #    trace; the literal text is on the board instead of its output.
                on_board = [c for c in comments_of(app, "PGU-1") if c == ON_BOARD][0]
                assert "`switchyard upgrade syrd`" in on_board
                assert "$(id -u)" in on_board and "$HOME" in on_board
                assert "--text-file /etc/passwd" in on_board
                assert "Trailing spaces follow this colon:   \n" in on_board
                assert not SENTINEL.exists(), "a shell ran something"
                assert not Path("/tmp/nope").exists(), "a redirect ran"
                checks += 6

                # 4. The direct form still works, unchanged.
                args = resolve(["add-comment", "PGU-1", "--text", "plain and direct"])
                assert args.text == "plain and direct"
                client.add_comment("PGU-1", text=args.text)
                assert "plain and direct" in comments_of(app, "PGU-1")
                checks += 2

                # 5. Error behaviour, each naming the flag.
                for argv, fragment, label in (
                    (["add-comment", "PGU-1", "--text", "x", "--text-file", str(body_path)],
                     "mutually exclusive", "both forms"),
                    (["add-comment", "PGU-1", "--text-file", "/nonexistent-syrd196"],
                     "no such file", "missing file"),
                    (["add-comment", "PGU-1", "--text-file", str(ROOT)],
                     "is a directory", "directory"),
                    (["add-comment", "PGU-1", "--text", ""],
                     "must not be empty", "empty required"),
                    (["add-comment", "PGU-1"],
                     "must not be empty", "neither form"),
                ):
                    message = resolve_error(argv)
                    assert message, (label, "no error raised")
                    assert fragment in message, (label, message[-200:])
                    assert "--text" in message, (label, message[-200:])
                    checks += 3

                # 6. Empty stdin is EOF, not a hang; a required field says so.
                stdin_backup = sys.stdin
                sys.stdin = io.TextIOWrapper(io.BytesIO(b""), encoding="utf-8")
                try:
                    message = resolve_error(["add-comment", "PGU-1", "--text", "-"])
                finally:
                    sys.stdin = stdin_backup
                assert "must not be empty" in message, message[-200:]
                checks += 1

                # 7. The mechanism reached every free-text field, not just the one.
                #    A reason and a ticket body carry the same content intact.
                args = resolve(["submit-to-audit-without-commit", "PGU-1", "--reason-file", str(body_path)])
                assert args.reason == ADVERSARIAL
                args = resolve(["create-ticket", "--title", "t", "--body-file", str(body_path),
                                "--comment-text-file", str(body_path)])
                assert args.body == ADVERSARIAL and args.comment_text == ADVERSARIAL
                args = resolve(["set-blockers", "PGU-1", "--blocked-by", "PGU-2",
                                "--blocked-reason-file", str(body_path)])
                assert args.blocked_reason == ADVERSARIAL
                args = resolve(["inspector-kick-back", "PGU-1", "--recommendations-file", str(body_path)])
                assert args.recommendations == ADVERSARIAL
                checks += 5

                # 8. Every free-text option registered by the helper has both forms.
                parser = wc._build_parser()
                registered = 0
                for action in parser._subparsers._group_actions[0].choices.values():
                    for flag, dest, _req, _default in getattr(action, "_free_text_fields", ()):
                        opts = {o for a in action._actions for o in a.option_strings}
                        assert flag in opts, flag
                        assert f"{flag}-file" in opts, f"{flag}-file"
                        registered += 1
                assert registered >= 26, registered
                print(f"  free-text options with both safe forms: {registered}")
                checks += 1
            finally:
                server.shutdown()
                thread.join(timeout=5)
    finally:
        if body_path.exists():
            body_path.unlink()
        if SENTINEL.exists():
            SENTINEL.unlink()
    print(f"write_client_free_text_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
