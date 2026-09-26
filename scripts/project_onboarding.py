"""What a project's agents are handed to start with: onboarding docs, and the board skill.

This module covers:
- **Onboarding documents.** The README, methodology and board/director guides
  a new project receives, the designer and director onboarding files, and the
  director's seeded remit. They are written at `switchyard new` and refreshed
  at upgrade. Each installed doc is stamped with the Switchyard source commit
  it came from, so an upgrade replaces only docs nobody edited; that check
  reads the doc's history at its stamped commit.
- **Director onboarding.** Whether a declared workflow's director onboarding
  has migrated (`director_onboarding_state`), and that migration
  (`migrate_declarative_director_onboarding`).
- **The generated project board skill.** Installing it for a project from the
  release it runs (`ensure_generated_project_board_skill`).

The role-prompt logic is in `scripts/workflow_manage.py`; the `board-skill`
verb is in `scripts/board_skill_cli.py`.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-289). `team_launcher`
imports this module at its top and still exports the names callers and suites
reach there. The suites patch `team_launcher._install_switchyard_onboarding_docs`,
`director_onboarding_state` and `migrate_declarative_director_onboarding`
around the launcher's own call sites. This module never imports `team_launcher`
at its top. Launcher facilities (`run_owner_correct_git`, `_chown_project_file`,
`_open_board_url`, `current_user_name`, ...) are read from
`scripts.team_launcher` when a function runs, so patches there still reach
them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from scripts.ticket_board.workflow_config import (
    DIRECTOR_ONBOARDING_MIGRATION,
    DIRECTOR_ROLE,
)

if TYPE_CHECKING:
    from scripts.team_launcher import DeclaredWorkflowPresence, ProjectConfig


SWITCHYARD_DESIGN_ONBOARDING_FILE_NAME = "DESIGNER_ONBOARDING.md"


SWITCHYARD_DIRECTOR_ONBOARDING_FILE_NAME = "DIRECTOR_ONBOARDING.md"


SWITCHYARD_ONBOARDING_DOC_NAMES = (
    "README.md",
    "adversarial-collaborative-methodology.md",
    "switchyard-board-guide.md",
    "switchyard-director-guide.md",
)


BOARD_SKILL_NAME = "switchyard-board"


BOARD_SKILL_INSTALLER_NAME = "switchyard-board-skill"


def install_generated_project_board_skill_args(
    config: ProjectConfig,
    *,
    installer: Path,
    source_commit: str = "",
    dry_run: bool = False,
) -> list[str]:
    from scripts import team_launcher as launcher

    if not config.run_as_user:
        raise ValueError("board skill install requires run_as_user")
    owner_home = launcher.home_dir_for_user(config.run_as_user) or Path("/home") / config.run_as_user
    args = [str(installer), "install", "--home", str(owner_home)]
    if source_commit and source_commit != "unknown":
        args.extend(["--source-commit", source_commit])
    if dry_run:
        args.append("--dry-run")
    if launcher.current_user_name() == config.run_as_user:
        return args
    return ["sudo", "-u", config.run_as_user, "-H", *args]


def board_skill_installer_path(
    config: ProjectConfig,
    *,
    script_path: Path,
    source_repo: Path | None = None,
) -> Path:
    """The installer that ships beside the launcher this project actually runs."""
    if source_repo is not None:
        return source_repo / "scripts" / BOARD_SKILL_INSTALLER_NAME
    launcher_path = (config.pane_launcher or script_path).expanduser()
    return launcher_path.with_name(BOARD_SKILL_INSTALLER_NAME)


def ensure_generated_project_board_skill(
    config: ProjectConfig,
    *,
    config_path: Path,
    script_path: Path,
    source_repo: Path | None = None,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None] = print,
) -> None:
    """Project the board skill into the owner's CLI skill trees.

    On launch this runs before any role session starts, so a Hermes role home
    created later in the same launch finds a shared skills directory to link.
    """
    from scripts import team_launcher as launcher

    if not config.run_as_user or not launcher._is_generated_project_layout_template(config, config_path=config_path):
        return
    installer = board_skill_installer_path(config, script_path=script_path, source_repo=source_repo)
    source_commit = _switchyard_source_commit(installer.parent.parent, runner=runner)
    args = install_generated_project_board_skill_args(
        config,
        installer=installer,
        source_commit=source_commit,
        dry_run=dry_run,
    )
    result = runner(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        # A missing or unwritable skill tree must not stop the team from
        # working; the pane still runs, it just starts without the skill.
        reason = launcher._proc_failure_reason(result, f"installer failed with exit {result.returncode}")
        print_func(
            f"warning: switchyard: could not install the {BOARD_SKILL_NAME} skill for {config.project}: {reason}"
        )
        return
    for line in str(getattr(result, "stdout", "") or "").splitlines():
        # An unchanged copy is the normal case; only report what moved.
        if line.strip() and ": current " not in line:
            print_func(f"switchyard: board skill {line.strip()}")


def director_onboarding_seed_text(project_dir: Path) -> str:
    """The director's remit, as ordinary role data rather than a special case.

    The hook used to build this at session start for the director alone, searching for
    the onboarding packet and printing paths relative to the pane's cwd. Storing it makes
    the director the same shape as every other role, at the cost of the paths being fixed
    at provisioning rather than rediscovered. It is stored as prompt text rather than as
    an `onboarding` file path because the generic path branch refuses to start when the
    file is missing, where the director previously degraded quietly.
    """
    packet = project_dir / "docs" / "onboarding"
    guide = packet / "switchyard-director-guide.md"
    return (
        f"Your director session just started fresh. Read the onboarding packet first: {packet}. "
        f"Start with {guide}."
    )


@dataclass(frozen=True)
class DirectorSeedResult:
    """What the backfill changed: the marker, the prompt, or neither."""

    marked: bool
    seeded: bool

    @property
    def changed(self) -> bool:
        return self.marked or self.seeded


def seed_director_onboarding(document, project_dir: Path):
    """Give the configured director stored onboarding when it has none.

    Idempotent, and only ever fills a gap: a director that already carries a prompt or a
    remit path is left untouched, as is every other role. The director is located through
    the workflow model's own required-role constant rather than a literal, because a
    document without that role is rejected by validation -- it is structural, not a
    tenant-chosen name.

    Returns the document and whether anything was seeded, so callers can report an
    automatic configuration change rather than making it silently.
    """
    if not document:
        return document, DirectorSeedResult(False, False)
    migrations = document.setdefault("migrations", {})
    if migrations.get(DIRECTOR_ONBOARDING_MIGRATION):
        # Already considered once. A director with nothing stored now means the operator
        # cleared it, not that this tenant predates the feature, so refilling here would
        # make `role-prompt clear director` impossible to persist.
        return document, DirectorSeedResult(False, False)
    seeded = False
    for role in document.get("roles", []):
        if role.get("name") != DIRECTOR_ROLE:
            continue
        if not role.get("onboarding_prompt") and not role.get("onboarding"):
            role["onboarding_prompt"] = director_onboarding_seed_text(project_dir)
            seeded = True
    # Marked whether or not anything was filled: a director that already had its own
    # onboarding is equally "considered". Reported separately from seeding, because a
    # tenant that needs only the marker still needs that marker persisted -- otherwise a
    # later clear would look like a legacy gap and be refilled.
    migrations[DIRECTOR_ONBOARDING_MIGRATION] = True
    return document, DirectorSeedResult(True, seeded)


def _write_switchyard_onboarding_files(
    *,
    project_name: str,
    slug: str,
    owner_user: str,
    project_dir: Path,
    artifact_path: Path,
    design_document: Path,
    director_onboarding: Path,
    include_designer: bool = True,
) -> None:
    from scripts import team_launcher as launcher

    switchyard_dir = launcher._switchyard_dir(project_dir)
    switchyard_dir.mkdir(parents=True, exist_ok=True)
    if include_designer:
        designer_onboarding = switchyard_dir / SWITCHYARD_DESIGN_ONBOARDING_FILE_NAME
        designer_onboarding.write_text(
            "\n".join(
                [
                    f"# {project_name} Switchyard Design Session",
                    "",
                    f"Working directory: {project_dir}",
                    f"Project slug: {slug}",
                    f"Owner user: {owner_user}",
                    f"Design document: {design_document}",
                    f"Project artifact: {artifact_path}",
                    "",
                    "Create or refine the design document with the user.",
                    "The board and full pane window already exist; use tickets for follow-up implementation work.",
                    "Do not create workflow stages or workflow transitions in the artifact.",
                    "This project directory is the user-visible checkout and is not reset or cleaned by launch.",
                    # An empty variable turns `rm -rf "$TARGET"/x` into `/x`, and Claude
                    # stops to ask before a recursive delete of a critical path even in
                    # bypass mode -- which stalls a pane nobody is watching (SYRD-234).
                    "Recursive deletion always names a guarded target: rm -rf -- \"${TARGET:?}\", never an unguarded \"$TARGET\".",
                    "Implementation panes run from managed role worktrees outside this directory.",
                    "The initial git remote named origin points at this local repository as a bootstrap placeholder.",
                    "Replace origin with the project's real remote before expecting fetches to detect upstream changes.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    director_onboarding.write_text(
        "\n".join(
            [
                f"# {project_name} Director Onboarding",
                "",
                f"Project directory: {project_dir}",
                f"Project artifact: {artifact_path}",
                *([f"Design document: {design_document}"] if include_designer else ["Design phase: skipped"]),
                "",
                "The privileged provisioning and full pane window were started by switchyard new.",
                "Use the live board to onboard the team and reshape stages or roles later if the user asks.",
                "Recursive deletion always names a guarded target: rm -rf -- \"${TARGET:?}\", never an unguarded \"$TARGET\".",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _switchyard_source_commit(
    source_repo: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    from scripts import team_launcher as launcher

    direct_marker = launcher._read_switchyard_release_marker(source_repo)
    if direct_marker is not None and direct_marker.marker_commit:
        return direct_marker.marker_commit
    shared_release = launcher.shared_switchyard_release_for_path(source_repo)
    if shared_release is not None and shared_release.marker_commit:
        return shared_release.marker_commit
    proc = launcher.run_owner_correct_git(
        ["git", "-C", str(source_repo), "rev-parse", "--verify", "HEAD"],
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        return "unknown"
    return str(getattr(proc, "stdout", "") or "").strip() or "unknown"


def _stamp_onboarding_doc(*, source_name: str, source_commit: str, body: str) -> str:
    return (
        f"<!-- Switchyard onboarding snapshot: source commit {source_commit}; "
        f"source docs/onboarding/{source_name}. -->\n\n"
        f"{body}"
    )


ONBOARDING_SNAPSHOT_RE = re.compile(
    r"\A<!-- Switchyard onboarding snapshot: source commit (?P<commit>[^;]+); "
    r"source docs/onboarding/(?P<source>.+?)\. -->\n\n",
    re.S,
)


def _parse_onboarding_snapshot(text: str) -> tuple[str, str] | None:
    match = ONBOARDING_SNAPSHOT_RE.match(text)
    if not match:
        return None
    return match.group("commit").strip(), match.group("source").strip()


def _onboarding_history_git_args(
    *,
    source_repo: Path,
    commit_git_dir: str | None,
    source_commit: str,
    current_source_commit: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> list[str] | None:
    from scripts import team_launcher as launcher

    direct_release = launcher._read_switchyard_release_marker(source_repo)
    shared_release = launcher.shared_switchyard_release_for_path(source_repo)
    immutable_release = bool(
        (direct_release is not None and direct_release.marker_commit)
        or (shared_release is not None and shared_release.marker_commit)
    )
    selected = str(commit_git_dir or "").strip()
    if not immutable_release or not selected:
        return ["git", "-C", str(source_repo)]

    required_commits = tuple(dict.fromkeys((source_commit, current_source_commit)))
    for raw_path in selected.split(os.pathsep):
        if not raw_path.strip():
            continue
        git_dir = Path(raw_path.strip()).expanduser()
        git_args = ["git", f"--git-dir={git_dir}"]
        for commit in required_commits:
            proc = launcher.run_owner_correct_git(
                [*git_args, "cat-file", "-e", f"{commit}^{{commit}}"],
                runner=runner,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if proc.returncode != 0:
                break
        else:
            return git_args
    return None


def _read_source_onboarding_doc_at_commit(
    *,
    source_repo: Path,
    git_args: Sequence[str] | None = None,
    source_commit: str,
    source_name: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> str | None:
    from scripts import team_launcher as launcher

    if not source_commit or source_commit == "unknown":
        return None
    selected_git_args = list(git_args) if git_args is not None else ["git", "-C", str(source_repo)]
    proc = launcher.run_owner_correct_git(
        [*selected_git_args, "show", f"{source_commit}:docs/onboarding/{source_name}"],
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return str(getattr(proc, "stdout", "") or "")


def _source_onboarding_commit_is_ancestor(
    *,
    source_repo: Path,
    git_args: Sequence[str] | None = None,
    source_commit: str,
    current_source_commit: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool | None:
    from scripts import team_launcher as launcher

    if (
        not source_commit
        or source_commit == "unknown"
        or not current_source_commit
        or current_source_commit == "unknown"
    ):
        return None
    selected_git_args = list(git_args) if git_args is not None else ["git", "-C", str(source_repo)]
    proc = launcher.run_owner_correct_git(
        [
            *selected_git_args,
            "merge-base",
            "--is-ancestor",
            source_commit,
            current_source_commit,
        ],
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def upgrade_switchyard_onboarding_docs(
    *,
    source_repo: Path,
    project_dir: Path,
    owner_user: str,
    commit_git_dir: str | None = None,
    dry_run: bool,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None],
) -> None:
    from scripts import team_launcher as launcher

    source_dir = source_repo / "docs" / "onboarding"
    target_dir = project_dir / "docs" / "onboarding"
    if not source_dir.is_dir():
        print_func(
            "warning: switchyard: onboarding docs source "
            f"{source_dir} is missing; skipped upgrading "
            f"{', '.join(SWITCHYARD_ONBOARDING_DOC_NAMES)} in {target_dir}."
        )
        return

    current_source_commit = _switchyard_source_commit(source_repo, runner=runner)
    if not dry_run:
        install = runner(
            [
                "install",
                "-d",
                "-m",
                "0755",
                "-o",
                owner_user,
                "-g",
                owner_user,
                str(target_dir.parent),
                str(target_dir),
            ]
        )
        if install.returncode != 0:
            raise SystemExit(f"switchyard: failed to create onboarding docs directory {target_dir} for {owner_user}")
        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)

    for name in SWITCHYARD_ONBOARDING_DOC_NAMES:
        source_path = source_dir / name
        target_path = target_dir / name
        try:
            current_body = source_path.read_text(encoding="utf-8")
        except OSError:
            print_func(f"switchyard: onboarding doc {name}: skipped, source missing")
            continue
        current_snapshot = _stamp_onboarding_doc(
            source_name=name,
            source_commit=current_source_commit,
            body=current_body,
        )
        if not target_path.exists():
            if not dry_run:
                target_path.write_text(current_snapshot, encoding="utf-8")
                launcher._chown_project_file(owner_user=owner_user, path=target_path, runner=runner)
            print_func(f"switchyard: onboarding doc {name}: {'would install' if dry_run else 'installed'}")
            continue
        try:
            existing = target_path.read_text(encoding="utf-8")
        except OSError as exc:
            print_func(f"switchyard: onboarding doc {name}: skipped, cannot read installed copy: {exc}")
            continue
        provenance = _parse_onboarding_snapshot(existing)
        if provenance is None:
            print_func(f"switchyard: onboarding doc {name}: skipped, no provenance header")
            continue
        source_commit, source_name = provenance
        if source_name != name:
            print_func(f"switchyard: onboarding doc {name}: skipped, provenance names {source_name}")
            continue
        history_git_args = _onboarding_history_git_args(
            source_repo=source_repo,
            commit_git_dir=commit_git_dir,
            source_commit=source_commit,
            current_source_commit=current_source_commit,
            runner=runner,
        )
        if history_git_args is None:
            print_func(f"switchyard: onboarding doc {name}: skipped, cannot verify source commit {source_commit}")
            continue
        old_body = _read_source_onboarding_doc_at_commit(
            source_repo=source_repo,
            git_args=history_git_args,
            source_commit=source_commit,
            source_name=source_name,
            runner=runner,
        )
        if old_body is None:
            print_func(f"switchyard: onboarding doc {name}: skipped, cannot verify source commit {source_commit}")
            continue
        if existing != _stamp_onboarding_doc(source_name=name, source_commit=source_commit, body=old_body):
            print_func(f"switchyard: onboarding doc {name}: skipped, edited since source commit {source_commit}")
            continue
        source_commit_is_ancestor = _source_onboarding_commit_is_ancestor(
            source_repo=source_repo,
            git_args=history_git_args,
            source_commit=source_commit,
            current_source_commit=current_source_commit,
            runner=runner,
        )
        if source_commit_is_ancestor is False:
            print_func(f"switchyard: onboarding doc {name}: skipped, installed copy is newer than this checkout")
            continue
        if source_commit_is_ancestor is None:
            print_func(
                f"switchyard: onboarding doc {name}: skipped, cannot verify ancestry from "
                f"source commit {source_commit} to release commit {current_source_commit}"
            )
            continue
        if not dry_run:
            target_path.write_text(current_snapshot, encoding="utf-8")
            launcher._chown_project_file(owner_user=owner_user, path=target_path, runner=runner)
        print_func(f"switchyard: onboarding doc {name}: {'would refresh' if dry_run else 'refreshed'}")


def _install_switchyard_onboarding_docs(
    *,
    source_repo: Path,
    project_dir: Path,
    owner_user: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None],
) -> None:
    from scripts import team_launcher as launcher

    source_dir = source_repo / "docs" / "onboarding"
    target_dir = project_dir / "docs" / "onboarding"
    if not source_dir.is_dir():
        print_func(
            "warning: switchyard: onboarding docs source "
            f"{source_dir} is missing; skipped installing "
            f"{', '.join(SWITCHYARD_ONBOARDING_DOC_NAMES)} into {target_dir}. "
            "Copy them later from docs/onboarding/ in the Switchyard source checkout."
        )
        return

    install = runner(
        [
            "install",
            "-d",
            "-m",
            "0755",
            "-o",
            owner_user,
            "-g",
            owner_user,
            str(target_dir.parent),
            str(target_dir),
        ]
    )
    if install.returncode != 0:
        raise SystemExit(f"switchyard: failed to create onboarding docs directory {target_dir} for {owner_user}")
    if not target_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)

    source_commit = _switchyard_source_commit(source_repo, runner=runner)
    copied: list[str] = []
    skipped_existing: list[str] = []
    skipped_missing: list[str] = []
    for name in SWITCHYARD_ONBOARDING_DOC_NAMES:
        source_path = source_dir / name
        target_path = target_dir / name
        if target_path.exists():
            skipped_existing.append(name)
            continue
        try:
            body = source_path.read_text(encoding="utf-8")
        except OSError:
            skipped_missing.append(name)
            continue
        target_path.write_text(
            _stamp_onboarding_doc(source_name=name, source_commit=source_commit, body=body),
            encoding="utf-8",
        )
        launcher._chown_project_file(owner_user=owner_user, path=target_path, runner=runner)
        copied.append(name)

    if copied:
        print_func(f"switchyard: installed onboarding docs into {target_dir}: {', '.join(copied)}")
    if skipped_existing:
        print_func(f"switchyard: skipped existing onboarding docs in {target_dir}: {', '.join(skipped_existing)}")
    if skipped_missing:
        print_func(
            "warning: switchyard: onboarding docs source "
            f"{source_dir} is incomplete; skipped missing {', '.join(skipped_missing)}. "
            "Copy them later from docs/onboarding/ in the Switchyard source checkout."
        )


def _document_director_onboarding_marker(document: Any) -> bool:
    if not isinstance(document, Mapping):
        return False
    migrations = document.get("migrations")
    return bool(isinstance(migrations, Mapping) and migrations.get("director_onboarding"))


def director_onboarding_state(
    config: ProjectConfig,
    *,
    config_path: Path,
    opener: Callable[[str], Any] | None = None,
    presence: DeclaredWorkflowPresence | None = None,
) -> tuple[str, str]:
    """Whether the director migration is done, and if not, what is in the way.

    Asked of the board and the projection rather than of a record anyone wrote
    about them. `migrated=False` is not completion: it is also what a board that
    predates the phase-one schema returns, and that board has to receive the
    compatibility release before the migration can be accepted at all -- the
    exact state this tenant is in (SYRD-45).

    The `not required` exit below is the one this ticket narrowed. It used to
    be reachable for any tenant whose LOCAL config carried no workflow key,
    which is every legacy tenant -- so the board was never asked and the
    "pending" answer three lines further down, which was already correct, was
    never reached (SYRD-240).
    """
    from scripts import team_launcher as launcher

    if not launcher.director_phase_required(config, config_path=config_path, presence=presence):
        return "not required", ""
    try:
        projected = _document_director_onboarding_marker(
            (launcher._load_json(config_path) or {}).get("workflow")
        )
    except (SystemExit, OSError, ValueError) as exc:
        # The projection is one of the two things "done" rests on, so a config
        # that cannot be read is not a state this can report on.
        return "unknown", f"{config_path} could not be read ({exc})"
    board_root = config.board_url.rstrip("/").removesuffix("/api/tickets").removesuffix("/api")
    open_url = opener or launcher._open_board_url
    try:
        with open_url(board_root + "/api/workflow") as response:
            payload = json.load(response)
    except Exception as exc:  # noqa: BLE001 - any failure to read is "not proven"
        return "unknown", f"the board at {board_root} could not be read ({exc})"
    document = payload.get("document") if isinstance(payload, Mapping) else None
    if document is None:
        return "pending", "the board carries no workflow document yet"
    if not _document_director_onboarding_marker(document):
        return "pending", "the board's workflow document does not carry the migration marker"
    if not projected:
        return "pending", "the local projection does not carry the migration marker"
    return "done", ""


def migrate_declarative_director_onboarding(
    config: "ProjectConfig",
    *,
    config_path: Path,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> bool:
    """Run the shared director-onboarding migration for a declarative tenant.

    Upgrade and recovery call this so an existing project is migrated by ordinary
    maintenance rather than by a manual per-tenant step. It is a no-op for a project
    with no workflow document, and idempotent for one already migrated.
    """
    from scripts import team_launcher as launcher

    if not launcher._load_json(config_path).get("workflow"):
        return False
    if dry_run:
        print_func("switchyard: would migrate the director's onboarding prompt if unset")
        return False
    if os.geteuid() == 0:
        # This is a board write that only the director may make, and the board
        # decides that from the peer's uid. Root has no role, and manufacturing
        # one would be impersonation, so the phase belongs to the director's own
        # process and root only says so (SYRD-45).
        raise SystemExit(
            "switchyard: the director onboarding migration is a director-authority board "
            "write and cannot be made by root. The director must run "
            f"`switchyard finish-upgrade {config.project}` from their own session."
        )
    import contextlib
    import io as _io

    from scripts import workflow_manage

    captured = _io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            workflow_manage.main(
                [
                    "migrate-director-onboarding",
                    "--board-url",
                    config.board_url,
                    "--config",
                    str(config_path),
                ]
            )
    except Exception as exc:
        # Deliberately fatal. The hook no longer carries a legacy director source, so
        # deploying it over an unmigrated document would leave that director with no
        # onboarding at all. Failing here stops before the release is deployed, which is
        # recoverable; succeeding and deploying anyway is not.
        raise SystemExit(
            f"switchyard: director onboarding migration failed for {config_path}: {exc}. "
            "Resolve it before deploying. Clearing the director's onboarding is not a "
            "recovery for this: it would discard configuration to get past an error."
        ) from exc
    # Report whether the document actually changed, so callers know when their loaded
    # configuration has gone stale. An already-migrated tenant changes nothing.
    try:
        report = json.loads(captured.getvalue() or "{}")
    except json.JSONDecodeError:
        return True
    if report.get("reason") == "board predates the phase-one schema":
        # Expected during rollout, and not a failure: the upgrade must still report the
        # deployment commands, because deploying phase one is what unblocks the migration.
        print_func(
            "switchyard: the running board predates this release's workflow schema, so "
            "the director onboarding migration was not attempted. Deploy the release "
            "below, then rerun switchyard upgrade to migrate this tenant."
        )
        return False
    if report.get("migrated") is False:
        return False
    print_func(
        f"switchyard: migrated the director's onboarding for {config_path}"
    )
    return True
