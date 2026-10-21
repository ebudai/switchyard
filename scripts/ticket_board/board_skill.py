"""Project Switchyard's canonical skills into each CLI's skill tree.

Claude, Codex, agy and Hermes all read the same ``SKILL.md`` package shape, but
each discovers it in a different directory.  One source file per skill is
therefore installed into four locations rather than maintained as four bodies.

Three skills ship: ``switchyard-board``, which every role needs,
``switchyard-director``, a Director-only overlay on it, and
``switchyard-librarian``, the optional Librarian's overlay (SYRD-624).  Role
panes commonly share one Unix home, so every file lands in the same trees; which
skill a session is *told* to load is the role-scoped part, and the board's own
authorization is what actually stops one role acting like another.

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
from typing import Any, Callable, Iterable, Sequence

SKILLS_DIR_NAME = "skills"
RELEASE_MARKER_NAME = ".switchyard-release.json"
UNKNOWN_COMMIT = "unknown"
DIRECTOR_ROLE = "director"


@dataclass(frozen=True)
class CanonicalSkill:
    """One skill body, and who is told to load it."""

    name: str
    director_only: bool = False
    #: The one role, besides none, that is told to load it (SYRD-624); empty means every role.
    role_only: str = ""

    @property
    def relative_source(self) -> Path:
        return Path(SKILLS_DIR_NAME) / self.name / "SKILL.md"

    def installed_path(self, home: Path, root: Path) -> Path:
        return Path(home).expanduser() / root / self.name / "SKILL.md"


BOARD_SKILL = CanonicalSkill("switchyard-board")
DIRECTOR_SKILL = CanonicalSkill("switchyard-director", director_only=True)
LIBRARIAN_SKILL = CanonicalSkill("switchyard-librarian", role_only="librarian")
CANONICAL_SKILLS: tuple[CanonicalSkill, ...] = (BOARD_SKILL, DIRECTOR_SKILL, LIBRARIAN_SKILL)

SKILL_NAME = BOARD_SKILL.name
CANONICAL_RELATIVE_SOURCE = BOARD_SKILL.relative_source


def skills_for_role(role: str) -> tuple[CanonicalSkill, ...]:
    """The skills a session in ``role`` should be told to load, in order.

    Every role gets the shared body; only a Director is pointed at its overlay,
    and only the Librarian at its own. This is a usability boundary, not a
    security one -- an overlay is readable in a shared home either way, and the
    board refuses privileged operations from any other caller regardless of what
    a session has read.
    """
    name = role.strip().lower()
    return tuple(skill for skill in CANONICAL_SKILLS
                 if (name == DIRECTOR_ROLE or not skill.director_only)
                 and (not skill.role_only or name == skill.role_only))

#: One entry per supported agent CLI: the runtime name and the skills root it
#: scans, relative to the runtime owner's home directory.
SKILL_ADAPTERS: tuple[tuple[str, Path], ...] = (
    ("claude", Path(".claude") / "skills"),
    ("codex", Path(".codex") / "skills"),
    ("agy", Path(".gemini") / "config" / "skills"),
    ("hermes", Path(".hermes") / "skills"),
    # GitHub Copilot CLI's personal skills (`copilot help skill`, 1.0.101).
    ("copilot", Path(".copilot") / "skills"),
)

# "board skill" is the wording the first release shipped. Copies installed by
# it are still ours, so they are still recognised -- and refreshed to the
# skill-neutral wording -- rather than being mistaken for somebody's own file.
PROVENANCE_RE = re.compile(
    r"\n<!-- Switchyard (?:board )?skill snapshot: source commit (?P<commit>[^;]+); "
    r"source (?P<source>[^ ]+)\. -->\n\Z"
)


def adapter_paths(
    home: Path, skill: CanonicalSkill = BOARD_SKILL, copilot_homes: Sequence[Path] = ()
) -> tuple[tuple[str, Path], ...]:
    """The installed SKILL.md path for each supported runtime under ``home``.

    `copilot_homes` are COPILOT_HOME roots a role runs with: Copilot 1.0.101
    reads personal skills only from `$COPILOT_HOME/skills` when that is set
    (SYRD-634), so each one gets a copy as well as `~/.copilot/skills`.
    """
    paths = [(runtime, skill.installed_path(home, root)) for runtime, root in SKILL_ADAPTERS]
    for copilot_home in copilot_homes:
        path = skill.installed_path(home, Path(copilot_home) / "skills")
        if ("copilot", path) not in paths:
            paths.append(("copilot", path))
    return tuple(paths)


def stamp_skill(body: str, *, source_commit: str, source: str = str(BOARD_SKILL.relative_source)) -> str:
    """Append the provenance footer that marks a copy as Switchyard-managed.

    The footer goes after the body, never before it: every runtime requires the
    YAML frontmatter to be the first thing in the file.
    """
    return (
        f"{body.rstrip(chr(10))}\n"
        f"\n<!-- Switchyard skill snapshot: source commit {source_commit}; source {source}. -->\n"
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
    if len(parents) >= 3 and parents[1].name == SKILLS_DIR_NAME:
        return parents[2]
    return source.parent


def skill_for_source(source: Path) -> CanonicalSkill:
    """Which canonical skill a source path is, by the directory that names it."""
    name = Path(source).expanduser().parent.name
    for skill in CANONICAL_SKILLS:
        if skill.name == name:
            return skill
    return CanonicalSkill(name)


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
    relative_source: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    """True when ``commit`` really contains the body we are about to install."""
    proc = runner(
        ["git", "-C", str(root), "show", f"{commit}:{relative_source.as_posix()}"],
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
    if not _commit_carries_source(
        root, commit, body, relative_source=skill_for_source(source).relative_source, runner=runner
    ):
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
    copilot_homes: Sequence[Path] = (),
    copilot_only: bool = False,
) -> list[AdapterResult]:
    """Project one canonical skill into every supported runtime under ``home``.

    `copilot_only` writes only the `copilot_homes` copies: what a Copilot pane
    does for its own COPILOT_HOME before it starts (SYRD-634).
    """
    source = Path(source).expanduser()
    skill = skill_for_source(source)
    body = source.read_text(encoding="utf-8")
    commit = source_commit.strip() or resolve_source_commit(source, runner=runner)
    desired = stamp_skill(body, source_commit=commit, source=str(skill.relative_source))
    return [
        _install_one(runtime=runtime, path=path, desired=desired, force=force, dry_run=dry_run)
        for runtime, path in adapter_paths(home, skill, copilot_homes)[len(SKILL_ADAPTERS) if copilot_only else 0:]
    ]


def verify_board_skill(
    *, home: Path, expected_commit: str = "", skill: CanonicalSkill = BOARD_SKILL, copilot_homes: Sequence[Path] = ()
) -> list[AdapterResult]:
    """Report, per runtime, whether a managed copy of the skill is discoverable."""
    results: list[AdapterResult] = []
    for runtime, path in adapter_paths(home, skill, copilot_homes):
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


def default_source(tree_root: Path, skill: CanonicalSkill = BOARD_SKILL) -> Path:
    return Path(tree_root).expanduser() / skill.relative_source


def canonical_sources(tree_root: Path) -> tuple[tuple[CanonicalSkill, Path], ...]:
    return tuple((skill, default_source(tree_root, skill)) for skill in CANONICAL_SKILLS)


def effective_source_commit(
    source: Path,
    supplied: str = "",
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    """The commit that may honestly be stamped on the body at ``source``.

    A caller that supplies a commit does not get to assert it. The launcher, for
    one, resolves a release commit the same way it does for onboarding docs, and
    over a working checkout that resolution is just `rev-parse HEAD` -- which
    says nothing about whether HEAD carries this body. Validating here rather
    than at each call site means every path into the installer gets the same
    guarantee.

    A release export is self-describing and immutable, so its marker wins over
    anything a caller passes.
    """
    source = Path(source).expanduser()
    root = source_root_for(source)
    marker_commit = _release_marker_commit(root)
    if marker_commit:
        return marker_commit
    supplied = supplied.strip()
    if not supplied:
        return resolve_source_commit(source, runner=runner)
    try:
        body = source.read_text(encoding="utf-8")
    except OSError:
        return UNKNOWN_COMMIT
    if not _commit_carries_source(
        root, supplied, body, relative_source=skill_for_source(source).relative_source, runner=runner
    ):
        return UNKNOWN_COMMIT
    return supplied


def _sources_to_install(source: Path | None, tree_root: Path) -> tuple[tuple[CanonicalSkill, Path], ...]:
    if source is None:
        return canonical_sources(tree_root)
    source = Path(source).expanduser()
    return ((skill_for_source(source), source),)


def install_command(
    *,
    home: Path,
    tree_root: Path,
    source: Path | None = None,
    source_commit: str = "",
    force: bool = False,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
    error_func: Callable[[str], None] = print,
    copilot_homes: Sequence[Path] = (),
    copilot_only: bool = False,
) -> int:
    """Install every canonical skill, or just the one named by ``source``."""
    selected = _sources_to_install(source, tree_root)
    exit_code = 0
    for skill, path in selected:
        if not path.is_file():
            error_func(f"switchyard: canonical skill {path} is missing")
            exit_code = 1
            continue
        commit = effective_source_commit(path, source_commit)
        results = install_board_skill(
            home=home, source=path, source_commit=commit, force=force, dry_run=dry_run, copilot_homes=copilot_homes,
            copilot_only=copilot_only,
        )
        for result in results:
            print_func(f"{skill.name} {result.describe()}")
        if commit == UNKNOWN_COMMIT:
            # Loud, because an unprovenanced copy is exactly what must not be
            # mistaken for evidence that a release shipped this skill.
            error_func(
                f"switchyard: warning: no release commit carries {path}; installed copies are "
                "stamped 'unknown' and verify will report them unprovenanced"
            )
        failures = [result for result in results if result.action == "failed"]
        if failures:
            error_func(
                f"switchyard: could not install {skill.name} for "
                + ", ".join(result.runtime for result in failures)
            )
            exit_code = 1
    return exit_code


def verify_command(
    *,
    home: Path,
    source_commit: str = "",
    print_func: Callable[[str], None] = print,
    error_func: Callable[[str], None] = print,
    copilot_homes: Sequence[Path] = (),
) -> int:
    exit_code = 0
    for skill in CANONICAL_SKILLS:
        results = verify_board_skill(home=home, expected_commit=source_commit, skill=skill, copilot_homes=copilot_homes)
        for result in results:
            print_func(f"{skill.name} {result.describe()}")
        unusable = failed_runtimes(results)
        if unusable:
            error_func(
                f"switchyard: {skill.name} is not usable for "
                + ", ".join(unusable)
                + "; re-run `switchyard board-skill install`"
            )
            exit_code = 1
    return exit_code
