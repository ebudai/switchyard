#!/usr/bin/env python3
"""The one place Switchyard asks a constrained question.

`prompt_schema` says what may be answered. This asks, and nothing else does: the
point of SYRD-115 is that `switchyard new`, an artifact, `add-role` and a runtime
switch stop keeping four ideas of what the choices are and four ideas of what a
valid answer looks like.

The interaction is the one Hermes already had and the rest did not: print the
options, number them, mark the first as the default, and let Enter take it. What
is added here is what a large catalog needs -- a filter and paging, so that
choosing a model is reading rather than remembering -- and what a real
configuration needs: a deliberate custom entry, and an existing value the
catalog has stopped advertising kept rather than dropped.

Three rules hold everywhere in this module.

**Nothing invalid is returned.** Every path out of here is a value from the
choices, a validated custom value, or an exception. A caller cannot be handed
something to write into an artifact that was never offered.

**A scripted run never becomes interactive.** With `interactive=False` this
never reads input. It takes the answer it was given, or the default, and refuses
by naming the field when it has neither -- because the alternative is a
provisioning script that hangs on a prompt nobody is there to answer.

**Only the injected pair does I/O.** `input_func` and `print_func`, the same
seam the rest of the launcher's prompts use, so every one of these interactions
is drivable by a test.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from scripts.ticket_board.prompt_schema import (
    CUSTOM_VALUE,
    KIND_BOOL,
    KIND_MULTI,
    KIND_SINGLE,
    KIND_TEXT,
    Choice,
    Field,
    Schema,
)

#: How many options are shown at once before the list pages. Chosen to fit a
#: terminal beside its prompt and its hint rather than to be a round number.
PAGE_SIZE = 12

#: How many times a question is re-asked before it gives up. The launcher's
#: existing prompts use the same bound; an infinite retry loop in a provisioning
#: run is a hang with extra steps.
MAX_ATTEMPTS = 5


class Cancelled(Exception):
    """The operator asked to stop. Raised, never swallowed: cancelling a
    provisioning question means the run does not continue with a guess."""


class NoAnswer(Exception):
    """A non-interactive run reached a question it has no answer for."""


def _hint(field: Field, choices: Sequence[Choice], *, paged: bool) -> str:
    parts = ["number"]
    if paged:
        parts.append("text to filter")
    parts.append("Enter for the default")
    if field.kind == KIND_MULTI:
        parts.insert(1, "several numbers")
    parts.append("'cancel' to stop")
    return "  (" + ", ".join(parts) + ")"


def _render(
    choices: Sequence[Choice],
    *,
    default_values: Sequence[str],
    print_func: Callable[[str], None],
    offset: int,
    total: int,
) -> None:
    for index, choice in enumerate(choices, start=offset + 1):
        marks = []
        if choice.value in default_values:
            marks.append("default")
        if choice.unlisted:
            # Said out loud, so that keeping it is a choice and not an accident.
            marks.append("not currently offered")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        description = f" -- {choice.description}" if choice.description else ""
        print_func(f"  {index}) {choice.display}{description}{suffix}")
    shown = offset + len(choices)
    if shown < total:
        print_func(f"  ... {total - shown} more; type part of a name to filter, or 'more'")


def _matching(choices: Sequence[Choice], needle: str) -> tuple[Choice, ...]:
    folded = needle.casefold()
    return tuple(
        choice for choice in choices
        if folded in choice.value.casefold() or folded in choice.display.casefold()
    )


def _resolve_token(token: str, choices: Sequence[Choice]) -> Choice | None:
    """One typed token to one choice, or nothing.

    A number indexes the list as printed. Anything else has to identify exactly
    one option: its stored value, its label, or a prefix of either that is not
    shared. An ambiguous prefix resolves to nothing rather than to the first
    match, because guessing here writes the wrong identifier into a project.
    """
    cleaned = token.strip()
    if not cleaned:
        return None
    if cleaned.isdigit():
        # A number is the index that was printed beside the entry -- but only
        # when it is written the way that index is written. `042` is not how a
        # list position is typed; it is somebody filtering a catalog of
        # `vendor/model-042`, and reading it as position 42 would quietly
        # select a different model than the one they were looking at.
        index = int(cleaned)
        if cleaned == str(index) and 1 <= index <= len(choices):
            return choices[index - 1]
        return None
    folded = cleaned.casefold()
    for choice in choices:
        if folded in {choice.value.casefold(), choice.display.casefold()}:
            return choice
    prefixed = [
        choice for choice in choices
        if choice.value.casefold().startswith(folded)
        or choice.display.casefold().startswith(folded)
    ]
    return prefixed[0] if len(prefixed) == 1 else None


def _custom_value(
    field: Field,
    *,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
) -> str:
    """Take a value no catalog offers, and validate it before accepting it.

    Deliberate by construction: this is only reached by choosing the custom
    entry. The field's own validator decides, so "custom" never means
    "unchecked" -- an unvalidated custom value is how an identifier that cannot
    work reaches a generated artifact.
    """
    for _attempt in range(MAX_ATTEMPTS):
        raw = input_func(f"{field.title} (exact value): ").strip()
        if raw.casefold() == "cancel":
            raise Cancelled(field.name)
        if not raw:
            print_func("switchyard: a custom value cannot be empty")
            continue
        if field.validate is None:
            return raw
        try:
            return field.validate(raw)
        except (ValueError, SystemExit) as exc:
            print_func(f"switchyard: {exc}")
    raise Cancelled(field.name)


def select_one(
    field: Field,
    answers: Mapping[str, Any] | None = None,
    *,
    preset: Any = None,
    interactive: bool = True,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> str:
    """Choose exactly one value for `field`."""
    choices = field.choices_for(answers)
    if not choices and not field.allow_custom:
        raise NoAnswer(f"{field.name}: nothing to choose from")
    default = _default_value(field, choices)

    if preset is not None and str(preset).strip():
        return _accept_preset(field, choices, str(preset).strip())
    if not interactive:
        if default:
            return default
        raise NoAnswer(
            f"switchyard: {field.title} has no default and was not supplied; "
            f"pass it rather than relying on a prompt"
        )
    return _ask_one(
        field, choices, default=default, input_func=input_func, print_func=print_func
    )


def _default_value(field: Field, choices: Sequence[Choice]) -> str:
    """The value Enter takes: the declared default, else the first option.

    "The first item is the default" is the rule the ticket asks for, and making
    it the fallback rather than a separate convention means a field that
    declares nothing still has a visible, pressable default.
    """
    if field.default is not None and str(field.default).strip():
        wanted = str(field.default).strip()
        if any(choice.value == wanted for choice in choices):
            return wanted
    return choices[0].value if choices else ""


def _accept_preset(field: Field, choices: Sequence[Choice], preset: str) -> str:
    """A value that came from a flag or an artifact, checked the same way.

    Scripted input is not trusted input. It is checked against the same choices
    a person would have been shown, so an automation cannot write a value into a
    project that a human would have been refused -- except through the field's
    own custom validator, which is the declared way to say "this one is mine".
    """
    for choice in choices:
        if choice.value == preset:
            return choice.value
    if field.allow_custom:
        if field.validate is None:
            return preset
        return field.validate(preset)
    offered = ", ".join(choice.value for choice in choices)
    raise NoAnswer(f"switchyard: {preset!r} is not one of {field.title}'s choices: {offered}")


def _ask_one(
    field: Field,
    choices: Sequence[Choice],
    *,
    default: str,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
) -> str:
    visible = tuple(choices)
    offset = 0
    for _attempt in range(MAX_ATTEMPTS):
        page = visible[offset:offset + PAGE_SIZE]
        paged = len(visible) > PAGE_SIZE
        print_func(f"{field.title}:")
        if field.description:
            print_func(f"  {field.description}")
        _render(
            page, default_values=[default], print_func=print_func,
            offset=offset, total=len(visible),
        )
        if field.allow_custom:
            print_func(f"  c) {field.custom_title}")
        print_func(_hint(field, visible, paged=paged))
        raw = input_func(f"{field.title} [{_display_of(default, choices)}]: ").strip()
        if not raw:
            return default
        folded = raw.casefold()
        if folded == "cancel":
            raise Cancelled(field.name)
        if folded == "more" and offset + PAGE_SIZE < len(visible):
            offset += PAGE_SIZE
            continue
        if field.allow_custom and folded in {"c", "custom"}:
            return _custom_value(field, input_func=input_func, print_func=print_func)
        # Resolved against everything visible, never against the page alone:
        # `_render` numbers from `offset + 1`, so on the second page the numbers
        # an operator can see are 13, 14, 15. Indexing the page by those would
        # land twelve entries further on -- picking a different model than the
        # one that was read off the screen.
        chosen = _resolve_token(raw, visible)
        if chosen is not None:
            return chosen.value
        narrowed = _matching(choices, raw)
        if narrowed:
            # Typed text that is not an answer is a filter. This is what makes a
            # long catalog usable without anybody recalling an exact identifier.
            visible = narrowed
            offset = 0
            if default not in {choice.value for choice in narrowed}:
                # The default follows the list being shown. "The first item is
                # the default" has to be true of what is on the screen, or
                # filtering to the one model somebody wants and pressing Enter
                # selects a different one entirely -- which is how a filter
                # becomes a way to choose the wrong thing.
                default = narrowed[0].value
            if len(narrowed) == 1:
                print_func(f"switchyard: one match: {narrowed[0].display}")
            continue
        print_func(f"switchyard: no choice matches {raw!r}")
        visible = tuple(choices)
        offset = 0
        default = _default_value(field, choices)
    raise Cancelled(field.name)


def _display_of(value: str, choices: Sequence[Choice]) -> str:
    for choice in choices:
        if choice.value == value:
            return choice.display
    return value


def select_many(
    field: Field,
    answers: Mapping[str, Any] | None = None,
    *,
    preset: Sequence[str] | None = None,
    interactive: bool = True,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> tuple[str, ...]:
    """Choose any number of values for `field`, as numbers rather than prose.

    The flow this replaces read roles as one comma-separated string, so a typo
    in the middle of it was a role nobody asked for and a role nobody noticed
    was missing.
    """
    choices = field.choices_for(answers)
    defaults = _default_values(field, choices)
    if preset is not None:
        return tuple(_accept_preset(field, choices, str(value).strip()) for value in preset)
    if not interactive:
        if defaults or not field.required:
            return tuple(defaults)
        raise NoAnswer(
            f"switchyard: {field.title} has no default and was not supplied; "
            f"pass it rather than relying on a prompt"
        )

    #: What the operator has actually chosen so far, as opposed to what they
    #: would get by pressing Enter. Adding a role of your own is a choice, so
    #: from then on Enter confirms THAT -- carrying the defaults along as well
    #: would hand somebody two roles they never asked for, which is the same
    #: silent-extra-role failure the comma-separated line used to produce.
    custom: list[str] = []
    for _attempt in range(MAX_ATTEMPTS):
        standing = custom or defaults
        print_func(f"{field.title}:")
        if field.description:
            print_func(f"  {field.description}")
        _render(
            choices, default_values=standing, print_func=print_func,
            offset=0, total=len(choices),
        )
        if field.allow_custom:
            print_func(f"  c) {field.custom_title}")
        print_func(_hint(field, choices, paged=False))
        raw = input_func(f"{field.title} [{', '.join(standing) or 'none'}]: ").strip()
        if not raw:
            picked = tuple(standing)
            if picked or not field.required:
                return picked
            print_func(f"switchyard: {field.title} needs at least one choice")
            continue
        if raw.casefold() == "cancel":
            raise Cancelled(field.name)
        tokens = [token for token in raw.replace(",", " ").split() if token]
        if field.allow_custom and len(tokens) == 1 and tokens[0].casefold() in {"c", "custom"}:
            custom.append(_custom_value(field, input_func=input_func, print_func=print_func))
            print_func(
                f"switchyard: added {custom[-1]}; the selection is now "
                f"{', '.join(custom)}. Add more, pick from the list, or press Enter"
            )
            continue
        resolved: list[str] = []
        unknown: list[str] = []
        for token in tokens:
            choice = _resolve_token(token, choices)
            if choice is None:
                unknown.append(token)
            elif choice.value not in resolved:
                resolved.append(choice.value)
        if unknown:
            print_func(f"switchyard: no choice matches {', '.join(repr(t) for t in unknown)}")
            continue
        picked = tuple(resolved) + tuple(value for value in custom if value not in resolved)
        if not picked and field.required:
            print_func(f"switchyard: {field.title} needs at least one choice")
            continue
        return picked
    raise Cancelled(field.name)


def _default_values(field: Field, choices: Sequence[Choice]) -> list[str]:
    if field.default:
        wanted = [str(value) for value in field.default]
        return [choice.value for choice in choices if choice.value in wanted]
    return []


def ask_bool(
    field: Field,
    *,
    preset: Any = None,
    interactive: bool = True,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> bool:
    default = bool(field.default)
    if preset is not None:
        return bool(preset)
    if not interactive:
        return default
    shown = "Y/n" if default else "y/N"
    for _attempt in range(MAX_ATTEMPTS):
        raw = input_func(f"{field.title} [{shown}]: ").strip().casefold()
        if not raw:
            return default
        if raw == "cancel":
            raise Cancelled(field.name)
        if raw in {"y", "yes", "true", "1"}:
            return True
        if raw in {"n", "no", "false", "0"}:
            return False
        print_func("switchyard: answer yes or no")
    raise Cancelled(field.name)


def ask_text(
    field: Field,
    *,
    preset: Any = None,
    interactive: bool = True,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> str:
    """An open value: a name, a path. Validated, never enumerated.

    A secret is a text field and is asked here, which is the only place it can
    be: it is never a choice, never printed back, and never carried into a
    review line.
    """
    default = "" if field.default is None else str(field.default)
    if preset is not None and str(preset).strip():
        raw = str(preset).strip()
        return field.validate(raw) if field.validate else raw
    if not interactive:
        if default or not field.required:
            return default
        raise NoAnswer(f"switchyard: {field.title} was not supplied")
    suffix = f" [{default}]" if default and not field.secret else ""
    for _attempt in range(MAX_ATTEMPTS):
        raw = input_func(f"{field.title}{suffix}: ").strip()
        if not raw and default:
            return default
        if raw.casefold() == "cancel" and not field.secret:
            raise Cancelled(field.name)
        if not raw:
            if not field.required:
                return ""
            print_func(f"switchyard: {field.title} is required")
            continue
        if field.validate is None:
            return raw
        try:
            return field.validate(raw)
        except (ValueError, SystemExit) as exc:
            print_func(f"switchyard: {exc}")
    raise Cancelled(field.name)


def ask(
    schema: Schema,
    *,
    presets: Mapping[str, Any] | None = None,
    interactive: bool = True,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Ask a whole schema, in order, and return the answers.

    Dependent fields are computed as they are reached rather than up front, so
    a catalog that depends on an earlier answer is built from the answer that
    was actually given.
    """
    given = dict(presets or {})
    answers: dict[str, Any] = {}
    for entry in schema.fields:
        preset = given.get(entry.name)
        if entry.kind == KIND_SINGLE:
            answers[entry.name] = select_one(
                entry, answers, preset=preset, interactive=interactive,
                input_func=input_func, print_func=print_func,
            )
        elif entry.kind == KIND_MULTI:
            answers[entry.name] = select_many(
                entry, answers, preset=preset, interactive=interactive,
                input_func=input_func, print_func=print_func,
            )
        elif entry.kind == KIND_BOOL:
            answers[entry.name] = ask_bool(
                entry, preset=preset, interactive=interactive,
                input_func=input_func, print_func=print_func,
            )
        elif entry.kind == KIND_TEXT:
            answers[entry.name] = ask_text(
                entry, preset=preset, interactive=interactive,
                input_func=input_func, print_func=print_func,
            )
    return answers
