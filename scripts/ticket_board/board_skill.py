"""Project the one canonical Switchyard board skill into each CLI's skill tree.

Claude, Codex, agy and Hermes all read the same ``SKILL.md`` package shape, but
each discovers it in a different directory.  One source file is therefore
installed into four locations rather than maintained as four bodies.

Installed copies carry a provenance footer naming the Switchyard commit they
came from.  That footer is what marks a copy as Switchyard-managed: a managed
copy is refreshed on upgrade, and a file we did not write is never touched.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

SKILL_NAME = "switchyard-board"
CANONICAL_RELATIVE_SOURCE = Path("skills") / SKILL_NAME / "SKILL.md"
RELEASE_MARKER_NAME = ".switchyard-release.json"
UNKNOWN_COMMIT = "unknown"

#: One entry per supported agent CLI: the runtime name and the skills root it
#: scans, relative to the runtime owner's home directory.
SKILL_ADAPTERS: tuple[tuple[str, Path], ...] = (
    ("claude", Path(".claude") / "skills"),
    ("codex", Path(".codex") / "skills"),
    ("agy", Path(".gemini") / "config" / "skills"),
    ("hermes", Path(".hermes") / "skills"),
)

PROVENANCE_RE = re.compile(
    r"\n<!-- Switchyard board skill snapshot: source commit (?P<commit>[^;]+); "
    r"source (?P<source>[^ ]+)\. -->\n\Z"
)


def adapter_paths(home: Path) -> tuple[tuple[str, Path], ...]:
    """The installed SKILL.md path for each supported runtime under ``home``."""
    home = Path(home).expanduser()
    return tuple((runtime, home / root / SKILL_NAME / "SKILL.md") for runtime, root in SKILL_ADAPTERS)


def stamp_skill(body: str, *, source_commit: str, source: str = str(CANONICAL_RELATIVE_SOURCE)) -> str:
    """Append the provenance footer that marks a copy as Switchyard-managed.

    The footer goes after the body, never before it: every runtime requires the
    YAML frontmatter to be the first thing in the file.
    """
    return (
        f"{body.rstrip(chr(10))}\n"
        f"\n<!-- Switchyard board skill snapshot: source commit {source_commit}; source {source}. -->\n"
    )


def parse_provenance(text: str) -> tuple[str, str] | None:
    """Return ``(commit, source)`` for a managed copy, or None for anything else."""
    match = PROVENANCE_RE.search(text)
    if not match:
        return None
    return match.group("commit").strip(), match.group("source").strip()


def source_root_for(source: Path) -> Path:
    """The Switchyard tree a canonical skill path belongs to."""
    source = Path(source).expanduser()
    parents = source.parents
    # <root>/skills/<skill-name>/SKILL.md
    if len(parents) >= 3 and parents[1].name == CANONICAL_RELATIVE_SOURCE.parts[0]:
        return parents[2]
    return source.parent


def _release_marker_commit(root: Path) -> str:
    """The immutable release commit stamped on ``root``, if it is a release tree."""
    try:
        payload = json.loads((root / RELEASE_MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("commit") or "").strip()


def _commit_carries_source(
    root: Path,
    commit: str,
    body: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    """True when ``commit`` really contains the body we are about to install."""
    proc = runner(
        ["git", "-C", str(root), "show", f"{commit}:{CANONICAL_RELATIVE_SOURCE.as_posix()}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return False
    return str(getattr(proc, "stdout", "") or "") == body


def resolve_source_commit(
    source: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    """Provenance for ``source``: its immutable release marker, else its checkout.

    A release tree is an export of one commit, so its marker is the answer. A
    working checkout is not: `HEAD` there can be any commit, including one that
    predates this file entirely. Stamping it anyway would produce a copy whose
    footer names a release that does not contain the body it carries, so the
    commit is claimed only when it actually holds these bytes.

    Callers that already know the release commit -- the launcher does, and it
    resolves it the same way for onboarding docs -- should pass it in rather
    than rely on this fallback.
    """
    source = Path(source).expanduser()
    root = source_root_for(source)
    marker_commit = _release_marker_commit(root)
    if marker_commit:
        return marker_commit
    proc = runner(
        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return UNKNOWN_COMMIT
    commit = str(getattr(proc, "stdout", "") or "").strip()
    if not commit:
        return UNKNOWN_COMMIT
    try:
        body = source.read_text(encoding="utf-8")
    except OSError:
        return UNKNOWN_COMMIT
    if not _commit_carries_source(root, commit, body, runner=runner):
        return UNKNOWN_COMMIT
    return commit


@dataclass(frozen=True)
class AdapterResult:
    runtime: str
    path: Path
    action: str  # installed | refreshed | current | unmanaged | failed
    detail: str = ""

    def describe(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"{self.runtime}: {self.action} {self.path}{suffix}"


def _install_one(
    *,
    runtime: str,
    path: Path,
    desired: str,
    force: bool,
    dry_run: bool,
) -> AdapterResult:
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        return AdapterResult(runtime, path, "failed", f"cannot read installed copy: {exc}")

    if existing is not None:
        if existing == desired:
            return AdapterResult(runtime, path, "current")
        if parse_provenance(existing) is None and not force:
            return AdapterResult(
                runtime,
                path,
                "unmanaged",
                "left alone; it has no Switchyard provenance footer",
            )
    action = "installed" if existing is None else "refreshed"
    if dry_run:
        return AdapterResult(runtime, path, "would install" if existing is None else "would refresh")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.switchyard.tmp")
        temporary.write_text(desired, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        return AdapterResult(runtime, path, "failed", str(exc))
    return AdapterResult(runtime, path, action)


def install_board_skill(
    *,
    home: Path,
    source: Path,
    source_commit: str = "",
    force: bool = False,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[AdapterResult]:
    """Project the canonical skill into every supported runtime under ``home``."""
    source = Path(source).expanduser()
    body = source.read_text(encoding="utf-8")
    commit = source_commit.strip() or resolve_source_commit(source, runner=runner)
    desired = stamp_skill(body, source_commit=commit)
    return [
        _install_one(runtime=runtime, path=path, desired=desired, force=force, dry_run=dry_run)
        for runtime, path in adapter_paths(home)
    ]


def verify_board_skill(*, home: Path, expected_commit: str = "") -> list[AdapterResult]:
    """Report, per runtime, whether a managed copy of the skill is discoverable."""
    results: list[AdapterResult] = []
    for runtime, path in adapter_paths(home):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            results.append(AdapterResult(runtime, path, "missing", str(exc)))
            continue
        provenance = parse_provenance(text)
        if provenance is None:
            results.append(AdapterResult(runtime, path, "unmanaged", "no Switchyard provenance footer"))
            continue
        commit, _source = provenance
        if commit == UNKNOWN_COMMIT:
            results.append(
                AdapterResult(
                    runtime,
                    path,
                    "unprovenanced",
                    "installed from a tree that could not name the commit carrying this body",
                )
            )
            continue
        if expected_commit and commit != expected_commit:
            results.append(AdapterResult(runtime, path, "stale", f"installed from {commit}"))
            continue
        results.append(AdapterResult(runtime, path, "present", commit))
    return results


def failed_runtimes(results: Iterable[AdapterResult]) -> list[str]:
    return [
        result.runtime
        for result in results
        if result.action in {"failed", "missing", "stale", "unmanaged", "unprovenanced"}
    ]


def default_source(tree_root: Path) -> Path:
    return Path(tree_root).expanduser() / CANONICAL_RELATIVE_SOURCE


def install_command(
    *,
    home: Path,
    source: Path,
    source_commit: str = "",
    force: bool = False,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
    error_func: Callable[[str], None] = print,
) -> int:
    source = Path(source).expanduser()
    if not source.is_file():
        error_func(f"switchyard: canonical board skill {source} is missing")
        return 1
    commit = source_commit.strip() or resolve_source_commit(source)
    results = install_board_skill(
        home=home, source=source, source_commit=commit, force=force, dry_run=dry_run
    )
    for result in results:
        print_func(result.describe())
    if commit == UNKNOWN_COMMIT:
        # Loud, because an unprovenanced copy is exactly what must not be
        # mistaken for evidence that a release shipped this skill.
        error_func(
            f"switchyard: warning: no release commit carries {source}; installed copies are "
            "stamped 'unknown' and verify will report them unprovenanced"
        )
    failures = [result for result in results if result.action == "failed"]
    if failures:
        error_func("switchyard: could not install " + ", ".join(result.runtime for result in failures))
        return 1
    return 0


def verify_command(
    *,
    home: Path,
    source_commit: str = "",
    print_func: Callable[[str], None] = print,
    error_func: Callable[[str], None] = print,
) -> int:
    results = verify_board_skill(home=home, expected_commit=source_commit)
    for result in results:
        print_func(result.describe())
    unusable = failed_runtimes(results)
    if unusable:
        error_func(
            f"switchyard: {SKILL_NAME} is not usable for "
            + ", ".join(unusable)
            + "; re-run `switchyard board-skill install`"
        )
        return 1
    return 0

