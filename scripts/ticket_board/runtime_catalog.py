#!/usr/bin/env python3
"""Where the option lists come from, and how fresh each one is.

The choices an operator is shown have to come from somewhere nameable. Before
SYRD-115 they came from three different places at once: a runtime was a tuple
declared twice (`SUPPORTED_CONFIG_CLI_NAMES` and `workflow_config.RUNTIMES`), a
model was whatever string somebody typed, and an effort level was a free-form
field rendered four different ways. Only one runtime could enumerate anything:
`agy models`. Claude and Codex enumerate nothing, and Hermes enumerates only
into its own interactive picker.

So this module says, per runtime, three things:

* **what can be asked** -- models, effort levels;
* **where the answer came from** -- a live command, or the recorded table below;
* **how much to trust it** -- `Catalog.provenance`, which is shown to the
  operator rather than implied.

Two honesty rules hold here, and they are the reason this is data rather than
code scattered through prompts.

**A recorded catalog is a record, not a claim about the vendor.** Vendor model
names drift; the launcher already says so of its install commands ("These
strings WILL drift as vendors change their installers, and that is the accepted
trade"). Everything in `RECORDED_MODELS` is sourced below, one line per entry,
from something in this repository. Nothing here was invented to fill the list
out, and the custom entry exists for exactly the case this table is wrong.

**Empty is a legitimate answer.** Hermes has no recorded models because this
repository has never held one: it authenticates by handing the terminal to
`hermes model`, its own picker. An empty catalog offers the custom path and says
why, which is honest; inventing plausible identifiers would not be.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from scripts.ticket_board.prompt_schema import Choice

#: Bumped when the recorded tables below change, so a project can record which
#: version of this data it was configured against.
CATALOG_VERSION = 1

#: When the recorded tables were last checked against the repository. A date an
#: operator can compare against, in the shape the launcher already uses for
#: vendor strings it knows will drift.
CATALOG_RECORDED = "2026-09-23"

PROVENANCE_RECORDED = "recorded"
PROVENANCE_LIVE = "live"
PROVENANCE_EMPTY = "none recorded"


@dataclass(frozen=True)
class Catalog:
    """Options, and where they came from."""

    choices: tuple[Choice, ...]
    provenance: str
    detail: str = ""

    @property
    def enumerable(self) -> bool:
        """Whether this list came from the account that will use it.

        Only a live list can be checked against; a recorded one is a starting
        point that has never been shown to this account. Nothing may be refused
        on the strength of a table (SYRD-250).
        """
        return self.provenance == PROVENANCE_LIVE

    @property
    def note(self) -> str:
        """One line an operator can read before choosing from this list."""
        if self.provenance == PROVENANCE_LIVE:
            return f"listed by {self.detail} in this account"
        if self.provenance == PROVENANCE_EMPTY:
            return self.detail
        return (
            f"UNVERIFIED: recorded {CATALOG_RECORDED} (catalog v{CATALOG_VERSION}) and not "
            f"checked against this account; {self.detail}"
        ).rstrip("; ")


#: The runtimes a role can be given. The set is `SUPPORTED_CONFIG_CLI_NAMES`;
#: the descriptions are what the launcher already does with each one, so an
#: operator choosing a runtime is told what choosing it means.
RUNTIMES: tuple[Choice, ...] = (
    Choice("claude", "Claude Code", "takes an --effort flag"),
    Choice("codex", "Codex", "takes reasoning_effort as config"),
    Choice("agy", "Antigravity", "can list its own models; takes no effort level"),
    Choice("hermes", "Hermes", "chooses its model in its own picker"),
)

#: Models this repository has actually been configured with, per runtime.
#:
#: Sources, one per entry, all in-tree:
#:   claude/codex/agy -- `config/team-launcher/pgu.json`, the live launcher
#:     configuration: claude-opus-5 (director, research, audit), gpt-5.6-sol
#:     (main), gpt-5.5 (app, ops), gemini-3.7-flash-high (inspector).
#:   agy -- `tests/team_launcher_test_helpers.py`'s agy catalog, the shape
#:     `agy models` returns: gemini-3.7-flash-high, gemini-3.7-pro.
#:   hermes -- nothing. See the module docstring.
#:
#: This is a fallback. Where a runtime can enumerate its own models, that is
#: asked first and this is what answers when it cannot be reached.
RECORDED_MODELS: dict[str, tuple[Choice, ...]] = {
    "claude": (
        Choice("claude-opus-5", "Opus 5", "configured for director, research and audit"),
    ),
    "codex": (
        Choice("gpt-5.6-sol", "GPT-5.6 Sol", "configured for main"),
        Choice("gpt-5.5", "GPT-5.5", "configured for app and ops"),
    ),
    "agy": (
        Choice("gemini-3.7-flash-high", "Gemini 3.7 Flash High", "configured for inspector"),
        Choice("gemini-3.7-pro", "Gemini 3.7 Pro"),
    ),
    "hermes": (),
}

#: Effort levels, per runtime, and what rendering each runtime gives them.
#:
#: `agy` is absent on purpose rather than empty: `EFFORT_STYLE_BY_CLI["agy"]` is
#: None, so an effort level is dropped before it reaches the command line. A
#: question whose answer is discarded should not be asked, which is why the
#: schema skips this field for a runtime that is not in this table.
#:
#: `high` is the level every role in `config/team-launcher/pgu.json` uses and is
#: therefore the default. `medium` and `low` are offered as the adjacent levels;
#: a runtime that names its levels differently is reached through the custom
#: entry, which is why that entry is always available here.
RECORDED_EFFORT: dict[str, tuple[Choice, ...]] = {
    "claude": (
        Choice("high", "high", "rendered as --effort high"),
        Choice("medium", "medium"),
        Choice("low", "low"),
    ),
    "codex": (
        Choice("high", "high", "rendered as -c reasoning_effort=high"),
        Choice("medium", "medium"),
        Choice("low", "low"),
    ),
    "hermes": (
        Choice("high", "high", "rendered as --reasoning high"),
        Choice("medium", "medium"),
        Choice("low", "low"),
    ),
}

#: How a runtime lists its own models, where it can. Only `agy` can: the
#: launcher already parses this exact output to suggest a replacement model
#: after a validation failure.
LIVE_MODEL_COMMANDS: dict[str, tuple[str, ...]] = {
    "agy": ("agy", "models"),
}


def runtime_takes_effort(runtime: str) -> bool:
    """Whether an effort level means anything to this runtime."""
    return runtime in RECORDED_EFFORT


def parse_model_ids(stdout: str) -> tuple[str, ...]:
    """The first token of each non-empty line, in order, without repeats.

    The same reading `_model_failure_suggestion` already applies to `agy
    models`: a catalog line begins with the identifier and continues with prose
    nobody should have to retype.
    """
    seen: dict[str, None] = {}
    for line in stdout.splitlines():
        token = line.strip().split()[:1]
        if token:
            seen.setdefault(token[0], None)
    return tuple(seen)


def model_catalog(
    runtime: str,
    *,
    configured: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    owner_args: Sequence[str] = (),
) -> Catalog:
    """The models to offer for `runtime`, and where the list came from.

    Live first where a runtime can enumerate, because a recorded table is out of
    date the moment a vendor ships. A live list that cannot be obtained -- no
    runner, a non-zero exit, nothing parseable -- falls back to the recorded
    table rather than to nothing, so an offline host still chooses from a list
    instead of from memory.
    """
    command = LIVE_MODEL_COMMANDS.get(runtime)
    if command and runner is not None:
        argv = [*owner_args, *command]
        try:
            proc = runner(argv, capture_output=True, text=True, check=False)
        except OSError:
            proc = None
        if proc is not None and getattr(proc, "returncode", 1) == 0:
            ids = parse_model_ids(str(getattr(proc, "stdout", "") or ""))
            if ids:
                return Catalog(
                    tuple(Choice(value) for value in ids),
                    PROVENANCE_LIVE,
                    detail=" ".join(command),
                )
    recorded = RECORDED_MODELS.get(runtime, ())
    if not recorded:
        return Catalog(
            (),
            PROVENANCE_EMPTY,
            detail=(
                f"switchyard has no recorded models for {runtime}; it chooses its model in "
                f"its own picker, so name one here only if you know it"
            ),
        )
    detail = "run this on a host that can reach the runtime to list its own" if command else ""
    return Catalog(recorded, PROVENANCE_RECORDED, detail=detail)


def owner_model_catalog(
    runtime: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    owner_args: Sequence[str] = (),
) -> Catalog | None:
    """The list `runtime` itself produced in the owner's account, or None.

    None means "this account has not told us anything", which covers a runtime
    that cannot enumerate at all, a runner that was not supplied, and a call
    that failed or returned nothing parseable. It is deliberately not the same
    value as an empty list: a recorded table describes a vendor, not an
    account, and nothing here may be used to contradict a configured model.

    One call answers for every role on that runtime, so callers read it once
    per CLI. It costs one `agy models`: no prompt, no token, no capability
    probe -- the difference between this and the probe SYRD-246 removed.
    """
    if runtime not in LIVE_MODEL_COMMANDS:
        return None
    found = model_catalog(runtime, runner=runner, owner_args=owner_args)
    return found if found.enumerable else None


def model_absent_from(catalog: Catalog | None, model: str) -> Catalog | None:
    """`catalog`, when it exists and does not contain `model`; else None.

    Returns the catalog rather than a bool so the caller can say what the
    account does offer without asking twice. Nothing is substituted for the
    missing value: `test2`'s audit role was configured for a slug its owner
    does not list, and picking a replacement here would be the silent rewrite
    SYRD-250 forbids -- the value may well be right and the account simply not
    set up yet.
    """
    wanted = str(model or "").strip()
    if catalog is None or not wanted:
        return None
    if any(choice.value == wanted for choice in catalog.choices):
        return None
    return catalog


def effort_catalog(runtime: str) -> Catalog:
    """The effort levels to offer for `runtime`."""
    recorded = RECORDED_EFFORT.get(runtime, ())
    if not recorded:
        return Catalog((), PROVENANCE_EMPTY, detail=f"{runtime} does not take an effort level")
    return Catalog(recorded, PROVENANCE_RECORDED)
