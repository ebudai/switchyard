"""The JSON objects `ticket-board-write` takes, and the prose merged into a workflow action's payload (SYRD-817).

`workflow-action --payload-json` is the only way to take a declared action with no subcommand of its own, and its
payload often carries a reason of several paragraphs. The JSON options are free-text options
(`write_cli.add_free_text_argument`): each has a `-file PATH` companion, and `-` reads standard input, so prose never
has to sit inline in a shell command, which the shell guard refuses (SYRD-753). A payload's single prose field can
instead come from `--reason`/`--text` (each with `-file`), merged in here; giving a field both ways is refused
rather than one silently winning.
"""

from __future__ import annotations

import json
from typing import Any

try:
    from . import write_client
except ImportError:  # pragma: no cover - run outside the package, as write_cli is
    import write_client

#: `workflow-action --help`'s closing lines: the form to copy, with the prose where the shell cannot reach it.
WORKFLOW_ACTION_EPILOG = """\
A payload's prose goes through a file or standard input, fed by a quoted heredoc, never inline:
  ticket-board-write workflow-action <id> <action> --reason-file - <<'EOF'
  What to fix, with `backticks`, $dollars and "quotes" kept exactly as written.
  EOF
Other fields stay in --payload-json, the prose merged in beside them:
  ticket-board-write workflow-action <id> <action> --payload-json '{"commit_hash": "<sha>"}' --reason-file - <<'EOF'
  ...
  EOF
A whole payload can come the same way: --payload-json-file PATH, or - for standard input.
A field given both in --payload-json and by its own option is refused."""

#: The prose fields a workflow action's payload may take from their own option.
PAYLOAD_TEXT_FIELDS = ("reason", "text")


def json_object(text: str, flag: str) -> dict[str, Any]:
    """`text` as a JSON object, or an error naming the option it came from."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise write_client.TicketBoardWriteError(f"{flag}: not valid JSON ({exc})") from None
    if not isinstance(value, dict):
        raise write_client.TicketBoardWriteError(f"{flag} must be a JSON object, not {type(value).__name__}")
    return value


def workflow_payload(args: Any) -> dict[str, Any]:
    """`--payload-json` (or its file), with each prose field given by its own option merged in."""
    payload = json_object(args.payload_json, "--payload-json")
    for field in PAYLOAD_TEXT_FIELDS:
        text = getattr(args, field, None)
        if text is None:
            continue
        if field in payload:
            raise write_client.TicketBoardWriteError(
                f"--{field} and a {field!r} field in --payload-json: give the {field} once")
        payload[field] = text
    return payload


def one_standard_input(args: Any, parser: Any) -> None:
    """Refuse two options reading standard input: the second would silently get nothing."""
    readers = [flag for flag, dest, *_ in getattr(args, "_free_text_fields", ())
               if "-" in (getattr(args, dest, None), getattr(args, f"{dest}_file", None))]
    if len(readers) > 1:
        parser.error(f"{' and '.join(readers)} both read standard input; give at most one of them as -")
