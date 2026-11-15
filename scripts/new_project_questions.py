"""`switchyard new` asks each decision it needs with its other questions, instead of refusing (SYRD-778).

A decision `new` cannot take for the operator -- which of several declared boards takes its reports, which of several
signed-in desktops it may use -- used to stop the run with "name one with --flag, then run this again". The User:
"I don't want to have to add parameters like that ... please fold all that into the questions that are posed with the
others." Each is now a question asked in the same sequence as the project name, slug, agent user and path, before
anything is created:

- `can_ask` is whether anybody is there to answer: an injected reader is a scripted operator; the real `input` only
  on a terminal. A run nobody can answer -- no terminal and no `--yes` -- keeps the refusal, with its flag, because
  there is nobody to ask;
- `ask_choice` asks one of a named few, by number or by name. Its default, when it has one, is shown in brackets and
  taken by Enter; a question with no safe default has none, and Enter alone answers nothing.

A flag on the command line answers its question, so it is not asked; `--yes` takes each question's default where it
has one, and keeps the refusal where it has none.
"""

from __future__ import annotations

import builtins
import sys
from typing import Callable, Sequence


def can_ask(input_func: Callable[[str], str]) -> bool:
    """Whether a question asked through `input_func` can be answered: an injected reader can, the real `input` only
    when standard input is a terminal."""
    if input_func is not builtins.input:
        return True
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, ValueError):  # a closed or replaced stream answers nothing
        return False


def ask_choice(
    label: str,
    *,
    options: Sequence[tuple[str, str]],
    default: str = "",
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
) -> str:
    """One of `options` (name, description), by its number or its name. With a `default`, Enter takes it and the
    prompt shows it in brackets; with none, Enter alone is not an answer."""
    from scripts import team_launcher as launcher

    names = [name for name, _description in options]
    if default and default not in names:
        raise ValueError(f"default {default!r} is not one of {names}")
    for index, (name, description) in enumerate(options, start=1):
        marker = " [default]" if name == default else ""
        print_func(f"  {index}) {name}: {description}{marker}")
    suffix = f" [{default}]" if default else ""
    for _attempt in range(launcher.SWITCHYARD_PROMPT_MAX_ATTEMPTS):
        raw = launcher._read_prompt(f"{label}{suffix}: ", input_func=input_func).strip()
        if not raw and default:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(names):
            return names[int(raw) - 1]
        if raw in names or raw.lower() in names:
            return raw if raw in names else raw.lower()
        print_func("answer with the number or the name of one of the choices above"
                   + ("" if default else "; this question has no default"))
    raise SystemExit(f"switchyard: too many invalid answers for {label}")


def report_board_asker(
    slug: str,
    *,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
) -> Callable[[str, Sequence[tuple[str, str]], str], str] | None:
    """The `ask` `upstream_report_sinks.choose_report_board` takes for `slug`, or None when nobody can answer."""
    if not can_ask(input_func):
        return None

    def ask(why: str, options: Sequence[tuple[str, str]], default: str) -> str:
        print_func(f"switchyard: {slug} needs a decision about Switchyard reports: {why}.")
        return ask_choice("Switchyard reports board", options=options, default=default, input_func=input_func,
                          print_func=print_func)

    return ask
