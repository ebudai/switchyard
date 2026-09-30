"""Which of a role's stored arguments belong to its runtime, and what the others mean (SYRD-533).

A role's launcher entry carries arguments of its own: flags after the program
in `cli`, and `extra_args`. A runtime switch changed only the program, so a
role moved from Codex to Claude kept Codex's `-c model_reasoning_effort="high"`.
Claude reads `-c` as `--continue` and the assignment as a prompt: MEFP's luna-6
was started as `claude --model claude-sonnet-5-5 --dangerously-skip-permissions
-c model_reasoning_effort="high"` with `effort` unset, while MEFP's main, a
native Claude role, carries none of it.

`reconcile` rewrites one entry, in place, for the runtime it is going to run:

* an effort level written in another runtime's syntax becomes the entry's
  `effort`, which each runtime's adapter renders its own way;
* another runtime's approval-bypass flags become `yolo`, which is rendered the
  same way;
* another runtime's startup flags, which are derived from the runtime anyway,
  are dropped, as is a `model_arg` that was only the runtime being left's
  default;
* anything that is the new runtime's own -- Codex's `-c key=value` on a Codex
  role, Claude's standalone `-c` on a Claude role -- and anything not
  recognised at all stays exactly where it was.

What cannot be translated is refused, with the argument and where it is:
another runtime's configuration with no meaning here (a Codex `-c key=value`
for any key but the effort level), a standalone `-c` that means Claude's
`--continue` on a runtime without it, or an effort level that disagrees with
the one the entry already records. Discarding any of those silently would run
the role as something nobody chose.
"""

from __future__ import annotations

import re
from typing import Any

from scripts.role_command import DEFAULT_MODEL_ARG_BY_CLI, STARTUP_ARGS_BY_CLI, YOLO_ARGS_BY_CLI

#: A Codex `-c` value: a TOML assignment to a dotted key.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*=")
_CODEX_EFFORT_KEY = "model_reasoning_effort"
#: The effort flag of each runtime that takes one as a flag of its own.
_EFFORT_FLAG = {"claude": "--effort", "hermes": "--reasoning"}


class ArgumentRefusal(ValueError):
    """An argument that cannot be carried to the new runtime or translated."""


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _where(field: str, index: int) -> str:
    return f"{field}[{index}]"


def reconcile(
    entry: dict[str, Any], *, runtime: str, previous_runtime: str = "", effort: str | None = None
) -> list[str]:
    """Rewrite `entry`'s arguments for `runtime`; return what changed, one line each.

    `effort` is an explicit choice (SYRD-534): every effort setting in the
    arguments -- the runtime's own included, since a later duplicate would
    override it -- is removed and the entry records exactly that level; `""`
    clears it, leaving the runtime's default. None decides nothing: the
    arguments are reconciled as they are, and the runtime's own effort
    argument disagreeing with the recorded one is refused, because only an
    explicit choice can say which of the two was meant.

    Raises ArgumentRefusal, naming the argument, when something cannot be
    translated. `entry` is left unchanged in that case.
    """
    explicit = effort is not None
    own_efforts: list[tuple[str, str]] = []
    notes: list[str] = []
    efforts: list[tuple[str, str]] = []
    wants_yolo = False
    rewritten: dict[str, list[str]] = {}
    own_yolo = set(YOLO_ARGS_BY_CLI.get(runtime, ()))
    foreign_yolo = {flag for cli, flags in YOLO_ARGS_BY_CLI.items() if cli != runtime for flag in flags} - own_yolo
    own_startup = set(STARTUP_ARGS_BY_CLI.get(runtime, ()))
    foreign_startup = {flag for cli, flags in STARTUP_ARGS_BY_CLI.items() if cli != runtime for flag in flags} - own_startup

    cli = entry.get("cli")
    fields = [
        ("cli", list(cli[1:]) if isinstance(cli, list) else []),
        ("extra_args", list(entry.get("extra_args") or []) if isinstance(entry.get("extra_args"), list) else []),
    ]
    for field, args in fields:
        kept: list[str] = []
        offset = 1 if field == "cli" else 0
        index = 0
        while index < len(args):
            token = str(args[index])
            where = _where(field, index + offset)
            following = str(args[index + 1]) if index + 1 < len(args) else ""
            # Codex configuration: `-c key=value`, `--config key=value`, `--config=key=value`.
            assignment = ""
            width = 1
            if token in ("-c", "--config") and _ASSIGNMENT.match(following):
                assignment, width = following, 2
            elif token.startswith("--config=") and _ASSIGNMENT.match(token[len("--config="):]):
                assignment = token[len("--config="):]
            if assignment:
                key, _, value = assignment.partition("=")
                shown = ' '.join(map(str, args[index:index + width]))
                if runtime == "codex":
                    if key == _CODEX_EFFORT_KEY:
                        if explicit:
                            notes.append(f"{where}: codex's own effort `{shown}` is replaced by the chosen effort")
                        else:
                            own_efforts.append((_unquote(value), f"{where} `{shown}`"))
                            kept.extend(str(arg) for arg in args[index:index + width])
                    else:
                        kept.extend(str(arg) for arg in args[index:index + width])
                else:
                    if key != _CODEX_EFFORT_KEY:
                        raise ArgumentRefusal(
                            f"{where} is Codex configuration `{' '.join(map(str, args[index:index + width]))}`, "
                            f"which has no {runtime} equivalent; remove it or move the role back to codex"
                        )
                    efforts.append((_unquote(value), f"{where} `{' '.join(map(str, args[index:index + width]))}`"))
                    notes.append(f"{where}: Codex effort `{' '.join(map(str, args[index:index + width]))}` becomes effort")
                index += width
                continue
            if token in ("-c", "--continue") and runtime != "claude":
                # Not an assignment, so not Codex's: Claude's `--continue`.
                raise ArgumentRefusal(
                    f"{where} is `{token}` with no assignment after it -- Claude's --continue -- which {runtime} "
                    "does not have; remove it, or keep the role on claude"
                )
            # Another runtime's effort flag.
            flag_runtime = next((cli_name for cli_name, flag in _EFFORT_FLAG.items()
                                 if token == flag or token.startswith(flag + "=")), "")
            if flag_runtime and flag_runtime == runtime:
                flag = _EFFORT_FLAG[flag_runtime]
                # The level must be there before anything is consumed: a bare
                # flag followed by another option does not own that option.
                valueless = (not following or following.startswith("-")) if token == flag else not token[len(flag) + 1:]
                if valueless:
                    if not explicit:
                        raise ArgumentRefusal(
                            f"{where} is `{token}` with no level after it"
                            + (f" (`{following}` is another option)" if token == flag and following else "")
                            + "; pass --effort to choose one"
                        )
                    notes.append(f"{where}: {runtime}'s own effort `{token}`, which names no level, is replaced by the chosen effort")
                    index += 1
                    continue
                width = 2 if token == flag else 1
                value = following if token == flag else token[len(flag) + 1:]
                shown = ' '.join(map(str, args[index:index + width]))
                if explicit:
                    notes.append(f"{where}: {runtime}'s own effort `{shown}` is replaced by the chosen effort")
                else:
                    own_efforts.append((_unquote(value), f"{where} `{shown}`"))
                    kept.extend(str(arg) for arg in args[index:index + width])
                index += width
                continue
            if flag_runtime and flag_runtime != runtime:
                flag = _EFFORT_FLAG[flag_runtime]
                if token == flag:
                    if not following or following.startswith("-"):
                        raise ArgumentRefusal(f"{where} is `{flag}` with no level after it")
                    value, width = following, 2
                else:
                    value, width = token[len(flag) + 1:], 1
                    if not value:
                        raise ArgumentRefusal(f"{where} is `{token}` with no level after it")
                efforts.append((_unquote(value), f"{where} `{' '.join(map(str, args[index:index + width]))}`"))
                notes.append(f"{where}: {flag_runtime} effort `{' '.join(map(str, args[index:index + width]))}` becomes effort")
                index += width
                continue
            if token in foreign_yolo:
                wants_yolo = True
                notes.append(f"{where}: another runtime's approval bypass `{token}` becomes yolo")
                index += 1
                continue
            if token in foreign_startup:
                notes.append(f"{where}: another runtime's startup flag `{token}` is dropped; {runtime} derives its own")
                index += 1
                continue
            kept.append(token)
            index += 1
        rewritten[field] = kept

    recorded = str(entry.get("effort") or "").strip()
    if explicit:
        chosen = effort.strip()
        if chosen != recorded:
            notes.append(f"effort `{recorded or 'unset'}` becomes `{chosen or 'unset'}`")
        # The choice decides; what the arguments said no longer matters.
        efforts = [(chosen, "the chosen effort")] if chosen else []
        own_efforts = []
    # Without a choice, every level the arguments name must agree -- the
    # runtime's own carriers with each other and with translated ones -- or the
    # last duplicate would silently win.
    named = own_efforts + efforts
    if len({value for value, _ in named}) > 1:
        raise ArgumentRefusal(
            "conflicting effort levels in the role's arguments: "
            + "; ".join(f"{where} says {value!r}" for value, where in named)
            + "; pass --effort to choose one"
        )
    stale = [(value, where) for value, where in own_efforts if value != recorded]
    if stale and recorded:
        raise ArgumentRefusal(
            f"the role records effort {recorded!r} but its own argument " + "; ".join(
                f"{where} says {value!r}" for value, where in stale) + "; pass --effort to choose one"
        )
    values = sorted({value for value, _ in efforts})
    if len(values) > 1:
        raise ArgumentRefusal("conflicting effort levels in the role's arguments: "
                              + "; ".join(f"{where} = {value!r}" for value, where in efforts))
    effort_value = values[0] if values else ""
    if effort_value and recorded and recorded != effort_value and not explicit:
        raise ArgumentRefusal(
            f"the role records effort {recorded!r} but {efforts[0][1]} says {effort_value!r}; "
            "pass --effort to choose one"
        )

    # Only now, when nothing is refused, is the entry touched.
    if isinstance(cli, list) and cli:
        entry["cli"] = [cli[0], *rewritten["cli"]]
    if "extra_args" in entry or rewritten["extra_args"]:
        entry["extra_args"] = rewritten["extra_args"]
    if explicit:
        if effort.strip():
            entry["effort"] = effort.strip()
        else:
            entry.pop("effort", None)
    elif effort_value and not recorded:
        entry["effort"] = effort_value
    if wants_yolo and not entry.get("yolo"):
        entry["yolo"] = True
    model_arg = str(entry.get("model_arg") or "")
    if previous_runtime and previous_runtime != runtime and model_arg:
        if model_arg == DEFAULT_MODEL_ARG_BY_CLI.get(previous_runtime) and model_arg != DEFAULT_MODEL_ARG_BY_CLI.get(runtime, "--model"):
            entry.pop("model_arg", None)
            notes.append(f"model_arg `{model_arg}` was {previous_runtime}'s default and is dropped; {runtime} uses its own")
    return notes
