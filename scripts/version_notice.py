"""Report version divergence as fact, without changing anything.

Two questions get asked here, and they are different in kind.

`./install` exports whatever is checked out; it does not fetch. A checkout that
sits behind its upstream therefore installs older code and says nothing, which
is how a user ends up running a version whose bug they have already been told
was fixed. That is worth reporting -- but it is not worth refusing over, and it
is not a defect. Someone pinning a version that works for them, avoiding a
regression, or reproducing a bug report is doing the thing a distributed tool is
supposed to let them do. So this reports the fact and proceeds. It never pulls,
fetches, reinstalls, or reaches the network.

The second question is not a version choice at all. `switchyard` runs from the
installed release, so pulling a checkout does not change what runs; the only
symptom is a bug the user has already been told is fixed, and the conclusion
they reasonably draw is that it was not. That is a mismatch between two things
they believe are the same, and telling them about it restores a choice rather
than removing one.

Silence is deliberate and durable: someone who has pinned a version must be able
to stop hearing about it, or they learn to ignore everything this tool prints.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

#: Set to 0/false/no to silence both notices for one invocation.
NOTICE_ENV = "SWITCHYARD_VERSION_NOTICE"
#: `git config switchyard.versionNotice false`, read from the checkout itself, so
#: the silence belongs to the checkout that was deliberately pinned and survives
#: every later invocation. Named in the notice so it is discoverable at the
#: moment somebody wants it.
NOTICE_CONFIG_KEY = "switchyard.versionNotice"
RELEASE_MARKER_NAME = ".switchyard-release.json"

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]


def _run(args: Sequence[str], *, runner: Runner) -> tuple[int, str]:
    proc = runner(list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return proc.returncode, str(getattr(proc, "stdout", "") or "").strip()


def _falsey(value: str) -> bool:
    return value.strip().casefold() in {"0", "false", "no", "off"}


def notices_are_silenced(
    repo: Path | None,
    *,
    environ: dict[str, str],
    runner: Runner = subprocess.run,
) -> bool:
    """Whether this checkout, or this invocation, has asked not to be told."""
    configured = environ.get(NOTICE_ENV)
    if configured is not None and _falsey(configured):
        return True
    if repo is None:
        return False
    code, value = _run(
        ["git", "-C", str(repo), "config", "--get", NOTICE_CONFIG_KEY], runner=runner
    )
    return code == 0 and _falsey(value)


@dataclass(frozen=True)
class CheckoutDivergence:
    """What is true about a checkout relative to the upstream it tracks."""

    # Why no comparison could be made, when none could. Not an error: an unknown
    # state is not a diverged one, and neither is a reason to stop.
    unknown: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0

    @property
    def level(self) -> bool:
        return not self.unknown and not self.ahead and not self.behind


def describe_checkout(
    repo: Path, *, runner: Runner = subprocess.run
) -> CheckoutDivergence:
    """Compare HEAD with its tracked upstream using only refs already on disk.

    Every command here reads the local object store. `@{upstream}` resolves the
    remote-tracking ref that the last fetch left behind, which is what makes
    this answerable without the network -- and what makes the answer as old as
    that fetch, which the notice says out loud.
    """
    code, _ = _run(["git", "-C", str(repo), "rev-parse", "--git-dir"], runner=runner)
    if code != 0:
        return CheckoutDivergence(unknown=f"{repo} is not a git checkout")
    code, branch = _run(
        ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"], runner=runner
    )
    if code != 0 or not branch:
        return CheckoutDivergence(unknown="HEAD is detached, so it tracks no branch")
    code, upstream = _run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        runner=runner,
    )
    if code != 0 or not upstream:
        return CheckoutDivergence(unknown=f"branch {branch} tracks no upstream")
    code, counts = _run(
        ["git", "-C", str(repo), "rev-list", "--left-right", "--count", f"HEAD...{upstream}"],
        runner=runner,
    )
    if code != 0:
        return CheckoutDivergence(
            unknown=f"{upstream} is not present locally; nothing has been fetched for it"
        )
    parts = counts.split()
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return CheckoutDivergence(unknown=f"could not count commits against {upstream}")
    return CheckoutDivergence(upstream=upstream, ahead=int(parts[0]), behind=int(parts[1]))


def checkout_notice_lines(
    repo: Path,
    *,
    environ: dict[str, str],
    runner: Runner = subprocess.run,
) -> list[str]:
    """What `./install` should say about the checkout it is about to install."""
    if notices_are_silenced(repo, environ=environ, runner=runner):
        return []
    divergence = describe_checkout(repo, runner=runner)
    if divergence.level:
        # The common case, and a fresh clone's case. Saying "you are up to date"
        # here would be the one place this could be wrong -- nothing has been
        # fetched -- and it would be noise on every install besides.
        return []
    if divergence.unknown:
        return [
            f"install: version check inconclusive: {divergence.unknown}.",
            "install: installing this checkout as-is.",
        ]
    parts = []
    if divergence.behind:
        parts.append(f"{divergence.behind} commit(s) behind")
    if divergence.ahead:
        parts.append(f"{divergence.ahead} commit(s) ahead of")
    return [
        f"install: this checkout is {' and '.join(parts)} {divergence.upstream} "
        "as of your last fetch.",
        "install: it installs what is checked out here; it does not fetch or pull.",
        f"install: silence this: git -C {repo} config {NOTICE_CONFIG_KEY} false",
    ]


def read_release_marker(root: Path) -> dict[str, str]:
    try:
        payload = json.loads((root / RELEASE_MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {key: str(value) for key, value in payload.items() if isinstance(value, str)}


def release_notice_lines(
    root: Path,
    *,
    environ: dict[str, str],
    runner: Runner = subprocess.run,
) -> list[str]:
    """What the running command should say when the release lags its source.

    Silence is the answer to every question this cannot settle. A release does
    not depend on its source checkout surviving, so a source that is gone, moved
    or no longer a git repository is an ordinary state and not a finding. The
    only thing reported is the one the user cannot see and did not choose: the
    release is strictly older than the checkout it was built from, which is what
    "I pulled and the bug is still there" actually is.
    """
    marker = read_release_marker(root)
    release_commit = marker.get("commit", "").strip()
    source_repo = marker.get("source_repo", "").strip()
    source_ref = marker.get("source_ref", "").strip() or "HEAD"
    if not release_commit or not source_repo:
        return []
    source = Path(source_repo)
    if not source.is_dir():
        # A fast path, not a guard: the `rev-parse --git-dir` below answers the
        # same for a missing source, verified across every shape a recorded path
        # can have. It is kept because a release commonly outlives its checkout,
        # and this is on the way to every command -- two processes not spawned.
        return []
    if notices_are_silenced(source, environ=environ, runner=runner):
        return []
    code, _ = _run(["git", "-C", str(source), "rev-parse", "--git-dir"], runner=runner)
    if code != 0:
        return []
    code, checkout_commit = _run(
        ["git", "-C", str(source), "rev-parse", f"{source_ref}^{{commit}}"], runner=runner
    )
    if code != 0 or not checkout_commit or checkout_commit == release_commit:
        return []
    code, _ = _run(
        ["git", "-C", str(source), "cat-file", "-e", f"{release_commit}^{{commit}}"], runner=runner
    )
    if code != 0:
        # The checkout does not contain the release's commit at all. That is a
        # different repository, a rewritten history or a pruned object -- not
        # evidence that the release is behind, so it is not reported as such.
        return []
    code, _ = _run(
        ["git", "-C", str(source), "merge-base", "--is-ancestor", release_commit, checkout_commit],
        runner=runner,
    )
    if code != 0:
        # Diverged, or the release is ahead. Neither is the reported failure, and
        # guessing at either would be the false warning this must not produce.
        return []
    return [
        f"switchyard: the installed release is {release_commit[:12]}, "
        f"older than {source} at {checkout_commit[:12]}.",
        "switchyard: pulling updates a checkout; it does not update what is installed. "
        "Run `sudo ./install` from that checkout to update it.",
        f"switchyard: silence this: git -C {source} config {NOTICE_CONFIG_KEY} false",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """`scripts/version_notice.py --checkout <path>`, for the bash installer.

    Always exits 0. A version notice reports; it does not decide.
    """
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(prog="version_notice")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--checkout", help="report how this checkout compares to its upstream")
    group.add_argument("--release", help="report whether this installed release lags its source")
    args = parser.parse_args(list(argv) if argv is not None else None)
    environ = dict(os.environ)
    lines = (
        checkout_notice_lines(Path(args.checkout), environ=environ)
        if args.checkout
        else release_notice_lines(Path(args.release), environ=environ)
    )
    for line in lines:
        print(line, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
