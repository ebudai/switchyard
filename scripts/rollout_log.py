"""`switchyard rollout-log`: reading a project's rollout journal.

`rollout_log_command` prints a project's journal -- every recorded attempt and
whether the index verifies -- or, for one attempt (the latest when `--output`
is asked for without one), what that attempt recorded as its result and,
with `--output`, its captured stdout and stderr. It reads only and needs no
privilege.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-439). The launcher
imports this module and re-exports the name; `switchyard_main` still calls the
launcher's name. It reads nothing from the launcher: the journal functions are
imported from `scripts.ticket_board.rollout_journal` when it runs, exactly as
before, and its one default, `print`, is the builtin.
"""

from __future__ import annotations

from pathlib import Path


def rollout_log_command(
    project: str, *, attempt: str = "", output: bool = False, print_func=print
) -> int:
    """Show the journal, or one attempt of it. Reads only; needs no privilege."""
    from scripts.ticket_board.rollout_journal import (
        RESULT_NAME,
        attempts,
        format_attempts,
        project_journal_dir,
        verify_index,
    )

    records = attempts(project)
    problems = verify_index(project)
    if not attempt:
        print_func(format_attempts(project, records, problems))
        if not records:
            print_func(
                f"switchyard: nothing has been recorded for {project} under "
                f"{project_journal_dir(project)}"
            )
            return 0
        attempt = records[-1]["attempt"]
        if not output:
            return 1 if problems else 0
    selected = next((record for record in records if record["attempt"] == attempt), None)
    if selected is None:
        print_func(f"switchyard: {project} has no recorded attempt {attempt}")
        return 1
    directory = Path(selected.get("directory") or "")
    result = directory / RESULT_NAME if directory else None
    if result is not None and result.is_file():
        print_func(result.read_text(encoding="utf-8").rstrip())
    else:
        print_func(
            f"switchyard: attempt {attempt} recorded no result; it started at "
            f"{selected.get('started_at', '?')} and never completed"
        )
    if output and directory:
        for name in ("stdout.log", "stderr.log"):
            path = directory / name
            if path.is_file():
                print_func(f"--- {name} ---")
                print_func(path.read_text(encoding="utf-8", errors="replace").rstrip())
    return 1 if problems else 0
