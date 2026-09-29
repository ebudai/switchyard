"""How the launcher composes a command's environment and PATH.

`_owner_home_bin_dirs` is the owner's own bin directories (a configured one,
else `bin` and `.local/bin` under the home). `_prepend_paths` puts directories
at the front of a PATH in the given order, dropping empty entries and any
earlier copy of each; `_prepend_path` does it for one. `_env_prefix` renders
variables as `KEY=value` arguments, the pane target first and the rest sorted;
`_env_unset_prefix` renders `-u KEY` for each key in order.
`_terminal_presentation_env` carries the non-blank terminal presentation
variables (`TERMINAL_PRESENTATION_ENV_KEYS`) across the owner boundary, and
`_pane_identity_scrubbed_env` is an environment without the pane identity
variables.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-460). The launcher
imports this module and re-exports every name, so every module that reads them
through the launcher still reaches the launcher's names. What they read of the
launcher -- each other included, the bin-directory variables, `_env_first` and
the pane identity keys -- is read through it when they run, so a patch there
still reaches them. This module imports `team_launcher` only inside the
functions that need it, when they run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Sequence


def _owner_home_bin_dirs(owner_home: Path) -> list[str]:
    from scripts import team_launcher as launcher

    configured = launcher._env_first(launcher.USER_BIN_ENV, launcher.LEGACY_USER_BIN_ENV)
    if configured:
        return [str(Path(configured).expanduser())]
    home = owner_home.expanduser()
    return [str(home / "bin"), str(home / ".local" / "bin")]


def _env_prefix(env: dict[str, str]) -> list[str]:
    return [
        f"{key}={value}"
        for key, value in sorted(env.items(), key=lambda item: (item[0] != "TICKET_BOARD_PANE_TARGET", item[0]))
    ]


def _env_unset_prefix(keys: Sequence[str]) -> list[str]:
    result: list[str] = []
    for key in keys:
        result.extend(["-u", key])
    return result


def _prepend_path(path_value: str, directory: str) -> str:
    from scripts import team_launcher as launcher

    return launcher._prepend_paths(path_value, [directory])


def _prepend_paths(path_value: str, directories: Sequence[str]) -> str:
    parts = [part for part in path_value.split(":") if part]
    for directory in reversed([item for item in directories if item]):
        parts = [part for part in parts if part != directory]
        parts.insert(0, directory)
    return ":".join(parts)


#: What a terminal needs to keep looking like itself across the owner boundary.
#: `sudo` resets the environment, and a CLI that cannot see TERM or COLORTERM
#: draws its first run in monochrome -- which is what the User was shown
#: (SYRD-191).
TERMINAL_PRESENTATION_ENV_KEYS = ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION")


def _terminal_presentation_env(source: Mapping[str, str] | None = None) -> list[str]:
    from scripts import team_launcher as launcher

    environ = os.environ if source is None else source
    return [
        f"{key}={environ[key]}"
        for key in launcher.TERMINAL_PRESENTATION_ENV_KEYS
        if str(environ.get(key) or "").strip()
    ]


def _pane_identity_scrubbed_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    from scripts import team_launcher as launcher

    env = dict(os.environ if source is None else source)
    for key in launcher.PROBE_IDENTITY_ENV_KEYS:
        env.pop(key, None)
    return env
