#!/usr/bin/env python3
"""What a constrained provisioning question IS, separately from how it is asked.

Switchyard's provisioning prompts grew one at a time, and each one carried its
own idea of what a valid answer looked like. A CLI was chosen by typing its name
after reading the alternatives out of the prompt text -- `claude/codex/agy/hermes`
-- roles arrived as a comma-separated string, and a model was an identifier the
operator had to remember. Every one of those values has a known, finite set, and
the set was nowhere an operator could see it (SYRD-115).

This module is that set, declared. It holds no input, no output and no terminal:
a `Field` says what may be answered and what an answer means, and
`terminal_select` is the single place that asks. The separation is what lets the
same declaration drive `switchyard new`, an artifact, `add-role` and a runtime
switch without any of them keeping a second list of the choices.

Two distinctions run through everything here.

**A value is not a label.** `Choice.value` is the stable identifier that reaches
the generated artifact and the launcher configuration; `Choice.label` is for the
person reading the screen and may be reworded whenever it reads badly. Nothing
stores a label, so nothing breaks when one changes.

**Not offered is not invalid.** A project configured last year may name a model
its runtime no longer advertises. That is a fact about the catalog, not an error
in the project, so such a value is carried into the choices as an unlisted entry
and kept unless the operator deliberately replaces it.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Callable, Mapping, Sequence

#: The kinds of question this schema can express. Deliberately few: every
#: provisioning field measured for SYRD-115 is one of these, and a kind that
#: exists for one caller is a hard-coded choice list wearing a costume.
KIND_SINGLE = "single"
KIND_MULTI = "multi"
KIND_BOOL = "bool"
KIND_TEXT = "text"

KINDS = (KIND_SINGLE, KIND_MULTI, KIND_BOOL, KIND_TEXT)

#: What an operator types to reach a value no catalog offers. An explicit,
#: visible entry rather than "type anything and we will take it": the ticket
#: asks for a deliberate Custom action, and a free-text fallback that triggers
#: by accident is the opposite of deliberate.
CUSTOM_VALUE = "__custom__"


class SchemaError(ValueError):
    """A field that could not be built. Raised where it is declared, not asked."""


@dataclass(frozen=True)
class Choice:
    """One selectable option: what is stored, and what is shown.

    `unlisted` marks a value the catalog no longer advertises but a project
    already holds. It is shown, it is selectable, and it is kept -- what it must
    never be is silently dropped, because that would rewrite a working project
    from a catalog change.
    """

    value: str
    label: str = ""
    description: str = ""
    unlisted: bool = False

    def __post_init__(self) -> None:
        if not str(self.value).strip():
            raise SchemaError("a choice must have a value")

    @property
    def display(self) -> str:
        """What a person reads. Falls back to the value when no label is given."""
        return self.label.strip() or self.value


#: A catalog that depends on earlier answers -- the models of the runtime just
#: chosen, the effort levels that runtime supports. Called with the answers so
#: far, so a changed runtime recomputes rather than remembering.
ChoiceSource = Callable[[Mapping[str, Any]], Sequence[Choice]]


@dataclass(frozen=True)
class Field:
    """One constrained question.

    `choices` is either a fixed sequence or a callable of the answers so far.
    The callable form is what makes a dependent question honest: it is evaluated
    at the moment the question is asked, so an earlier answer that changes takes
    its dependents with it instead of leaving a stale selection behind.
    """

    name: str
    kind: str
    title: str
    choices: Sequence[Choice] | ChoiceSource = ()
    default: Any = None
    description: str = ""
    allow_custom: bool = False
    custom_title: str = "Something else"
    required: bool = True
    validate: Callable[[str], str] | None = None
    #: Names of earlier fields this one is computed from. Documentation for a
    #: reader and a checkable claim for `Schema`, which refuses a dependency on
    #: a field that is not asked first.
    depends_on: Sequence[str] = ()
    secret: bool = False

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise SchemaError(f"{self.name}: unknown kind {self.kind!r}; expected one of {KINDS}")
        if not str(self.name).strip():
            raise SchemaError("a field must have a name")
        if self.secret and self.kind != KIND_TEXT:
            # A secret has no finite set worth listing, and listing it is how a
            # secret ends up on a screen and in a scrollback (SYRD-115).
            raise SchemaError(f"{self.name}: a secret cannot be a selectable choice")
        if self.kind in {KIND_SINGLE, KIND_MULTI} and not self.choices:
            raise SchemaError(f"{self.name}: a {self.kind} field needs choices")
        if self.kind in {KIND_BOOL, KIND_TEXT} and self.choices:
            raise SchemaError(f"{self.name}: a {self.kind} field cannot carry choices")

    def choices_for(self, answers: Mapping[str, Any] | None = None) -> tuple[Choice, ...]:
        """This field's options, given what has been answered so far."""
        source = self.choices
        if callable(source):
            source = source(dict(answers or {}))
        resolved = tuple(source)
        seen: set[str] = set()
        for choice in resolved:
            if choice.value in seen:
                raise SchemaError(f"{self.name}: duplicate choice value {choice.value!r}")
            seen.add(choice.value)
        return resolved


def with_existing_value(
    choices: Sequence[Choice], configured: str, *, note: str = "already configured"
) -> tuple[Choice, ...]:
    """Make sure a project's own value is among its options.

    The case this exists for: a model recorded a year ago that the runtime has
    stopped advertising. Dropping it would offer an operator every option except
    the one their project uses, and quietly re-point the project at whatever
    they picked instead. It is placed first, because the value a project already
    holds is the one Enter should keep.
    """
    wanted = str(configured or "").strip()
    if not wanted:
        return tuple(choices)
    for choice in choices:
        if choice.value == wanted:
            return (choice, *(other for other in choices if other is not choice))
    return (Choice(value=wanted, label=f"{wanted} ({note})", unlisted=True), *choices)


@dataclass
class Schema:
    """An ordered set of fields, asked in order.

    Ordered because dependence is real: a model catalog cannot be listed before
    its runtime is known. The order is declared here rather than discovered by
    whoever asks, so every caller asks the same questions in the same sequence.
    """

    fields: Sequence[Field] = dataclass_field(default_factory=tuple)

    def __post_init__(self) -> None:
        names: set[str] = set()
        for entry in self.fields:
            if entry.name in names:
                raise SchemaError(f"duplicate field {entry.name!r}")
            for dependency in entry.depends_on:
                if dependency not in names:
                    # Asked before it is answered, which would mean a dependent
                    # catalog computed from a value nobody has chosen yet.
                    raise SchemaError(
                        f"{entry.name}: depends on {dependency!r}, which is not asked before it"
                    )
            names.add(entry.name)

    def field(self, name: str) -> Field:
        for entry in self.fields:
            if entry.name == name:
                return entry
        raise KeyError(name)

    def dependents_of(self, name: str) -> tuple[str, ...]:
        """Which fields have to be asked again when `name` changes."""
        return tuple(entry.name for entry in self.fields if name in entry.depends_on)


def review_lines(schema: Schema, answers: Mapping[str, Any]) -> list[str]:
    """The summary shown before anything is created.

    Every constrained answer, in the order it was asked, as the label the
    operator saw and the value that will be written. Both, because a label is
    what they recognise and a value is what the artifact records -- and a review
    that shows only one of them cannot be checked against either.

    A secret field is named and never shown.
    """
    lines: list[str] = []
    for entry in schema.fields:
        if entry.name not in answers:
            continue
        answer = answers[entry.name]
        if entry.secret:
            lines.append(f"{entry.title}: (not shown)")
            continue
        if entry.kind == KIND_BOOL:
            lines.append(f"{entry.title}: {'yes' if answer else 'no'}")
            continue
        if entry.kind == KIND_TEXT:
            lines.append(f"{entry.title}: {answer}")
            continue
        catalog = {choice.value: choice for choice in entry.choices_for(answers)}
        values = list(answer) if entry.kind == KIND_MULTI else [answer]
        shown = []
        for value in values:
            choice = catalog.get(value)
            if choice is None or choice.display == value:
                shown.append(str(value))
            else:
                shown.append(f"{choice.display} ({value})")
        lines.append(f"{entry.title}: {', '.join(shown) if shown else '(none)'}")
    return lines
