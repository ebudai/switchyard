"""Run a real provider CLI so that everything it starts is gone before its home is removed (SYRD-524).

The real-Codex folder-trust cases (SYRD-279) started Codex under a tmux server
in a throwaway CODEX_HOME. Codex daemonises an app-server, which left its
process group and outlived `tmux kill-server`; repeated runs left twelve of
them running from deleted homes. And its plugin clone could still be writing
into the home while `TemporaryDirectory` removed it: `OSError: [Errno 39]
Directory not empty`.

`ContainedHome` runs each CLI invocation through `bounded_run.run_bounded`,
which makes this process a child subreaper: anything the invocation starts --
the tmux server, the CLI, a daemon that setsid()s or double-forks away, a clone
it spawns -- is reparented here when its parent exits, and is stopped (SIGTERM,
then SIGKILL) and reaped within the run's cleanup budget. Ownership is ancestry:
only this process's own descendants can be reparented to it, so nothing is ever
chosen by a path or a name. Children this process already had before a run are
left alone.

The home is removed only when every run reported clean and no process of this
user is running from inside it. Otherwise closing raises `Unclean`, names what
is left, and keeps the home: removing it under a live process is the race this
exists to prevent. Where this process cannot become a subreaper, nothing is run.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

import bounded_run


class Unclean(AssertionError):
    """A contained run could not be proved finished, so its home was kept."""


def processes_under(root: Path | str) -> list[tuple[int, str]]:
    """This user's processes whose executable or working directory is inside `root`.

    A check on the result, not a way of choosing what to stop: stopping is
    done by ancestry alone. `root` is a directory this process created, so no
    other test's or tenant's process can match it.
    """
    base = os.path.realpath(root)
    uid = os.getuid()
    found: list[tuple[int, str]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        for link in ("exe", "cwd"):
            try:
                target = os.readlink(entry / link).removesuffix(" (deleted)")
            except OSError:
                continue
            if target == base or target.startswith(base + os.sep):
                found.append((int(entry.name), target))
                break
    return found


def containment_available() -> bool:
    """Whether descendants that leave their process group can be found and stopped here."""
    return bounded_run._become_subreaper() and bounded_run._own_children() is not None


class ContainedHome:
    """A temporary directory whose CLI runs are all contained, removed only when nothing is left."""

    def __init__(self, prefix: str, *, grace: float = bounded_run.DEFAULT_GRACE_SECONDS) -> None:
        self.root = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
        self.grace = grace
        self.outcomes: list[bounded_run.Outcome] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout: float,
        label: str = "",
        input_text: str | None = None,
    ) -> bounded_run.Outcome:
        if not containment_available():
            raise Unclean(
                "cannot contain a real CLI here: this process cannot become a child subreaper, "
                f"so a daemon it started would go unseen; nothing was run ({label or argv[0]})"
            )
        outcome = bounded_run.run_bounded(
            argv, timeout=timeout, cwd=self.root, env=env, input_text=input_text,
            label=label or " ".join(argv[:2]), grace=self.grace,
        )
        self.outcomes.append(outcome)
        return outcome

    def problems(self) -> list[str]:
        found = [f"{outcome.label}: {outcome.reason}" for outcome in self.outcomes if not outcome.clean]
        left = processes_under(self.root)
        if left:
            found.append(f"still running from inside {self.root}: {left}")
        return found

    def close(self) -> None:
        problems = self.problems()
        if problems:
            raise Unclean(
                f"kept {self.root}, because removing it under a live process is the race this prevents: "
                + "; ".join(problems)
            )
        shutil.rmtree(self.root)

    def __enter__(self) -> "ContainedHome":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
