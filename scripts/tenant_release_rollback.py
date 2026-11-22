"""One tenant back to an earlier release it already ran, never the host's (SYRD-855).

`switchyard deploy-release <project> --commit <sha> --rollback [--any-staged]`, root only. It is what the catalogued
`rollback-release` (the control pane, pre-authorized) and `select-shared-release` (an administrator, who
authenticates) ask root to run.

On 2026-11-22 syrd needed to go back from bceacabf to cdc3a741, and every route refused: `deploy-release` because
root's pin named bceacabf, `preview-upgrade` because the recorded source was bceacabf's installed release, the
operator's `upgrade --source-repo` because the launcher running it was bceacabf's (an upgrade stages only the
release it runs), and `select-shared-release` because no caller could be both a pane and a person. Each of those
is right for an upgrade. A rollback is not an upgrade: everything it needs is already on the host.

So it changes only what the tenant was moved by, back to what it ran before:

- **only a release already staged for it**: its board release directory `<board root>/releases/<sha>`, marked with
  that commit, and root's installed release of the same commit, which must be able to run under root's exact units;
- **the control pane's form goes back one release**: the board release root's note of the tenant's last upgrade
  recorded as the one it replaced, while the tenant still serves the release that upgrade went to.
  `--any-staged` (an administrator's `select-shared-release`, or the operator's own sudo) takes any staged release;
- **migrations stay applied**: each migration the database has that the older release does not carry must be
  declared safe for older code (`ticket_board/schema_compatibility.py`), and any condition it holds under must be
  true on the tenant's database, read by root as the cluster's account. Otherwise nothing changes;
- then root's pin names that release, and `deploy-release` deploys it as it deploys any prepared release: the
  listener stopped, root's exact units, the tenant's own deploy-restart with its health gates and rollback, the
  listener started, and the phase closed only against what the running board reports. A deploy that fails puts the
  pin back;
- after the board serves it, the role tooling and pane hooks are restaged from that release.

The host's shared release, its privileged boundary and every other tenant are not touched.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable

#: The deploy sha marker `ticket-board-service.sh` writes into each board release it exports.
BOARD_RELEASE_MARKER = ".pgu-deploy-sha"
PSQL_TIMEOUT_SECONDS = 60


def staged_board_release_problem(board_root: Path, commit: str) -> str:
    """Why `<board root>/releases/<commit>` is not that release staged for this tenant, or ""."""
    release = board_root / "releases" / commit
    try:
        info = os.lstat(release)
    except OSError:
        return (f"{release} does not exist: the tenant never had {commit} staged as a board release, so there is "
                "nothing on this host to roll back to")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return f"{release} is not a board release directory"
    try:
        marked = (release / BOARD_RELEASE_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        marked = ""
    if marked != commit:
        return f"{release} records {marked or 'no release'}, not {commit}"
    return ""


def previous_release_problem(note_path: Path, *, current: str, commit: str) -> str:
    """Why `commit` is not the release root recorded this tenant's current board release as replacing, or ""."""
    try:
        info = os.lstat(note_path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
            raise OSError("not root's own record")
        note = json.loads(note_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        note = {}
    note = note if isinstance(note, dict) else {}
    went_to = str(note.get("upgrading_to") or "").strip()
    came_from = str(note.get("previous_board_commit") or "").strip()
    if went_to == current and came_from == commit:
        return ""
    if not went_to:
        recorded = f"root holds no note of its last upgrade ({note_path})"
    else:
        recorded = (f"root's note of its last upgrade records {came_from or 'no board release'} replaced by "
                    f"{went_to}")
    return (f"{commit} is not the release this tenant's current board release ({current or 'none'}) replaced: "
            f"{recorded}. The control pane's rollback goes back one release only; an administrator chooses any "
            "staged release with `select-shared-release`")


def _psql(database: str, sql: str, *, runner: Callable[..., Any]) -> tuple[str | None, str]:
    """One query, as the cluster's own account, on the local socket: (its output, or None, and why not)."""
    from scripts.ticket_board import database_admin

    command = ["runuser", "-u", database_admin.POSTGRES_ACCOUNT, "--", "env", "-i", "PATH=/usr/bin:/bin",
               "LANG=C.UTF-8", "psql", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1",
               database_admin.admin_url(database), "-c", sql]
    try:
        done = runner(command, cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      text=True, timeout=PSQL_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if getattr(done, "returncode", 1) != 0:
        return None, (str(getattr(done, "stderr", "") or "").strip() or "no output")[-300:]
    return str(getattr(done, "stdout", "") or ""), ""


def schema_problems(database: str, release: Path, *, runner: Callable[..., Any],
                    print_func: Callable[[str], None]) -> list[str]:
    """Why `release`'s code cannot run against `database`'s applied migrations; empty when it can."""
    from scripts.ticket_board import database_admin, schema_compatibility

    applied, why = _psql(database, "SELECT name FROM ticket_board.schema_migrations", runner=runner)
    if applied is None:
        return [f"the migrations {database} has applied could not be read ({why})"]
    try:
        carried = [path.name for path in (release / database_admin.MIGRATIONS).iterdir()]
    except OSError as exc:
        return [f"the migrations {release} carries could not be read ({exc.strerror})"]
    newer = schema_compatibility.crossed(applied.split(), carried)

    answers: dict[str, bool | None] = {}

    def holds(expression: str) -> bool | None:
        if expression not in answers:
            answer, _why = _psql(database, f"SELECT coalesce(({expression}), false)::text", runner=runner)
            answers[expression] = {"true": True, "false": False}.get((answer or "").strip())
        return answers[expression]

    problems = schema_compatibility.refusals(newer, holds=holds)
    if not problems and newer:
        print_func(f"switchyard: {len(newer)} migration(s) newer than {release.name} stay applied, each declared "
                   f"safe for older code: {', '.join(newer)}")
    return problems


def roll_back(
    config: Any,
    *,
    config_path: Path,
    commit: str,
    any_staged: bool,
    deploy: Callable[[], int],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None],
) -> int:
    """Check, pin, deploy, restage -- or refuse, having changed nothing. `deploy` is `deploy-release` for `commit`."""
    from scripts import team_launcher as launcher
    from scripts import tenant_release_deploy
    from scripts.ticket_board import database_admin, privileged_operations

    project = config.project

    def refuse(reason: str) -> int:
        print_func(f"switchyard: {reason}")
        print_func(f"switchyard: nothing of {project}'s was rolled back, deployed or recorded.")
        return 1

    board_root = launcher._tenant_board_root_from_config_or_plan(config, config_path)
    if board_root is None:
        return refuse(f"{project} has no board root to roll back")
    current = launcher._current_tenant_release(board_root)[1]
    if current == commit:
        return refuse(f"{project}'s board already serves {commit}: there is nothing to roll back")
    staged = staged_board_release_problem(board_root, commit)
    if staged:
        return refuse(staged)
    if not any_staged:
        previous = previous_release_problem(launcher.release_rollback_path(project), current=current, commit=commit)
        if previous:
            return refuse(previous)
    try:
        release = Path(privileged_operations.trusted_release_root(commit))
    except privileged_operations.NoTrustedSource as exc:
        return refuse(str(exc))
    if not tenant_release_deploy.exact_release_runtime(release):
        return refuse(f"root's release {commit} predates exact root-release board units (SYRD-699), so its board "
                      "cannot run under them")
    unit = tenant_release_deploy.SYSTEM_UNIT_DIR / launcher._board_system_unit(config)
    try:
        unit_bytes = tenant_release_deploy._installed_unit_bytes(unit)
        if unit_bytes is None:
            raise RuntimeError(f"{unit} is not installed")
        database = database_admin.database_from_unit(unit_bytes.decode("utf-8"))
    except (RuntimeError, ValueError, database_admin.DatabaseAdminError) as exc:
        return refuse(f"root cannot name {project}'s database from its board unit: {exc}")
    schema = schema_problems(database, release, runner=runner, print_func=print_func)
    if schema:
        for problem in schema:
            print_func(f"switchyard: {problem}")
        return refuse(f"{commit}'s board and listener are not known to run against {project}'s database, whose "
                      "migrations are kept: roll forward instead, or declare each migration above once it is "
                      "checked against older code")
    previous_pin = launcher.read_upgrade_source(config)
    pinned = launcher.record_upgrade_source(config, source_repo=release,
                                            commit_git_dir=previous_pin.get("commit_git_dir") or None,
                                            deploy_ref=commit)
    if pinned:
        return refuse("; ".join(pinned))
    print_func(f"switchyard: rolling {project} back from {current or 'no release'} to {commit}, staged for it at "
               f"{board_root / 'releases' / commit}; root's pin names it now, and its board is deployed next.")
    code = deploy()
    if code != 0 and launcher._current_tenant_release(board_root)[1] == commit:
        print_func(f"switchyard: the rollback's deploy did not complete (exit {code}), and {project}'s board release "
                   f"link names {commit}, so root's pin keeps naming it; its role tooling was not changed.")
        return code
    if code != 0:
        restored = (launcher.record_upgrade_source(
            config, source_repo=Path(previous_pin["source_repo"]) if previous_pin.get("source_repo") else None,
            commit_git_dir=previous_pin.get("commit_git_dir") or None, deploy_ref=previous_pin.get("deploy_ref", ""))
            if previous_pin.get("deploy_ref") else ["there was no pin before"])
        print_func(f"switchyard: the rollback's deploy did not complete (exit {code}); "
                   + (f"root's pin could not be put back ({'; '.join(restored)})" if restored
                      else f"root's pin names {previous_pin['deploy_ref']} again")
                   + f", and {project}'s role tooling was not changed.")
        return code
    tooling = launcher.refresh_staged_role_tooling(config, release_root=release, runner=runner, print_func=print_func)
    hooks = [] if tooling else launcher.refresh_role_pane_hooks(config, runner=runner, print_func=print_func)
    if tooling or hooks:
        for problem in tooling + hooks:
            print_func(f"switchyard: {problem}")
        print_func(f"switchyard: {project}'s board and listener run {commit}, but its role tooling was not restaged "
                   "from it; run this rollback again to restage it.")
        return 1
    print_func(f"switchyard: {project} is rolled back to {commit}: its board and listener run it, its role tooling "
               "and pane hooks are that release's, and its migrations stayed applied. The host's shared release is "
               "unchanged. Panes are not restarted.")
    return 0
