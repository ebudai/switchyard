"""`switchyard rollout-log`: reading a project's rollout journal.

`rollout_log_command` prints a project's journal -- every recorded attempt and
whether the index verifies -- or, for one attempt (the latest when `--output`
is asked for without one), what that attempt recorded as its result and,
with `--output`, its captured stdout and stderr. It reads only and needs no
privilege.

An attempt whose record names no absolute directory is refused: nothing is
read for it, least of all from the working directory (SYRD-441).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-439). The launcher
imports this module and re-exports the name; `switchyard_main` still calls the
launcher's name. It reads nothing from the launcher: the journal functions are
imported from `scripts.ticket_board.rollout_journal` when it runs, exactly as
before, and its one default, `print`, is the builtin.
"""

from __future__ import annotations


def rollout_log_command(
    project: str, *, attempt: str = "", output: bool = False, print_func=print
) -> int:
    """Show the journal, or one attempt of it. Reads only; needs no privilege."""
    from scripts.ticket_board.rollout_journal import (
        RESULT_NAME,
        attempt_directory,
        attempts,
        format_attempts,
        project_journal_dir,
        unusable_directory_reason,
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
    directory = attempt_directory(selected)
    if directory is None:
        # Never Path(""): that is the working directory, and its files are not
        # this attempt's (SYRD-441).
        print_func(
            f"switchyard: attempt {attempt} {unusable_directory_reason(selected)}, "
            "so it has no result or output to show"
        )
        return 1
    result = directory / RESULT_NAME
    if result.is_file():
        print_func(result.read_text(encoding="utf-8").rstrip())
    else:
        print_func(
            f"switchyard: attempt {attempt} recorded no result; it started at "
            f"{selected.get('started_at', '?')} and never completed"
        )
    if output:
        for name in ("stdout.log", "stderr.log"):
            path = directory / name
            if path.is_file():
                print_func(f"--- {name} ---")
                print_func(path.read_text(encoding="utf-8", errors="replace").rstrip())
    return 1 if problems else 0
