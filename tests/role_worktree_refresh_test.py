#!/usr/bin/env python3
"""SYRD-555: a launch never discards a role worktree's work.

Otto, 2026-10-04 21:03:15: after a restore every role session was down, so the
launch refreshed all six worktrees with `reset --hard origin/main` and
`clean -fdx`. Two roles had checked out their packet branches; the reset moved
those branches, and two finished packets (21 and 56 files) left them. `-x` also
deleted every `local.properties`. The only safeguard was a warning.

Every case here runs the launcher's own refresh against real git: a project's
repository, its bare control repository and its role worktrees on disk, built by
`ensure_control_role_worktrees` itself, and then changed the way otto's roles
had changed them. Nothing is stood in for but the owner home the fixture lives in.
"""

from __future__ import annotations

import contextlib
import io
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from control_repository_placeholder_test import tenant  # noqa: E402
from team_launcher_test_helpers import _commit_all, _run_git, team_launcher  # noqa: E402

CHECKS = 0
IDENTITY = ["-c", "user.email=role@example.invalid", "-c", "user.name=Role"]


def real_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    """Git, for real, exactly as the launcher asked for it."""
    return subprocess.run(args, **kwargs)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        return real_runner(args, **kwargs)


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def git(worktree: Path, *args: str) -> str:
    return _run_git(["git", *IDENTITY, "-C", str(worktree), *args]).stdout.strip()


class Tenant:
    """A control-repository tenant with main and ops worktrees made by the launcher, and an ignore rule for local config."""

    def __init__(self, tmp: Path) -> None:
        self.config, self.config_path, self.repo, self.control = tenant(tmp)
        (self.repo / ".gitignore").write_text("local.properties\nbuild/\n", encoding="utf-8")
        _commit_all(self.repo, "ignore local configuration")
        first = team_launcher.ensure_control_role_worktrees(self.config, refresh=True, runner=real_runner)
        assert first.ok, first.failed_roles
        self.wt = {role.role: Path(role.workdir) for role in self.config.roles}

    def advance(self) -> str:
        """A new commit on the ref, so a refresh has somewhere to go."""
        (self.repo / "tracked.txt").write_text(f"moved on {len(git(self.repo, 'log', '--oneline').splitlines())}\n", encoding="utf-8")
        _commit_all(self.repo, "the ref moves on")
        return git(self.repo, "rev-parse", "HEAD")

    def refresh(self, discard: frozenset[str] = frozenset(), runner=real_runner):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = team_launcher.ensure_control_role_worktrees(self.config, refresh=True, runner=runner, discard=discard)
        return result, stderr.getvalue()

    def head(self, role: str) -> str:
        return git(self.wt[role], "rev-parse", "HEAD")


def case(fn):
    def run() -> None:
        with tempfile.TemporaryDirectory(prefix="syrd555.") as tmp:
            fn(Tenant(Path(tmp)))
    run.__name__ = fn.__name__
    return run


@case
def test_a_branch_ahead_of_the_ref_is_kept_and_the_other_roles_still_refresh(t: Tenant) -> None:
    git(t.wt["main"], "checkout", "-b", "pack/8-template-engine")
    (t.wt["main"] / "engine.txt").write_text("the packet\n", encoding="utf-8")
    git(t.wt["main"], "add", "engine.txt")
    git(t.wt["main"], "commit", "-m", "packet 8")
    packet = t.head("main")
    new_ref = t.advance()
    result, said = t.refresh()
    check(t.head("main") == packet and git(t.wt["main"], "symbolic-ref", "--short", "HEAD") == "pack/8-template-engine"
          and git(t.wt["main"], "rev-parse", "pack/8-template-engine") == packet,
          f"the ahead branch and HEAD are unchanged: {t.head('main')} vs {packet}")
    check((t.wt["main"] / "engine.txt").read_text() == "the packet\n", "and its work is on disk")
    check("main" in result.kept_roles and "main" not in result.failed_roles
          and "branch pack/8-template-engine has 1 commit not in origin/main" in result.kept_roles["main"]
          and "branch pack/8-template-engine has 1 commit not in origin/main" in said,
          f"the refusal names the branch and the count, and is said: {result.kept_roles} / {said}")
    check(t.head("ops") == new_ref and result.ok, f"ops, clean at the ref, is refreshed as before: {t.head('ops')} vs {new_ref}")


@case
def test_untracked_files_refuse_and_ignored_local_configuration_is_never_deleted(t: Tenant) -> None:
    ops = t.wt["ops"]
    (ops / "notes.txt").write_text("an untracked draft\n", encoding="utf-8")
    (ops / "local.properties").write_text("sdk.dir=/opt/android\n", encoding="utf-8")
    (ops / "build").mkdir()
    (ops / "build" / "cache.bin").write_text("built\n", encoding="utf-8")
    before = t.head("ops")
    t.advance()
    result, _ = t.refresh()
    check(t.head("ops") == before and (ops / "notes.txt").exists() and (ops / "local.properties").exists(),
          f"without the override nothing is removed and HEAD stays: {result.kept_roles}")
    check("1 untracked path: notes.txt" in result.kept_roles.get("ops", "")
          and "local.properties" not in result.kept_roles.get("ops", ""),
          f"the untracked file is named; an ignored one is not work to refuse over: {result.kept_roles}")
    remedy = result.kept_roles["ops"].split("run `", 1)[1].split("`", 1)[0]
    words = shlex.split(remedy)
    parsed = team_launcher._build_switchyard_start_parser().parse_args(words[2:])
    check(words[:2] == ["switchyard", "start"] and parsed.project == ["testing"]
          and parsed.discard_worktree_changes == ["ops"],
          f"the printed remedy is a command the real parser takes: {remedy} -> {parsed}")

    result, _ = t.refresh(discard=frozenset({"ops"}))
    check(result.ok and not result.kept_roles and t.head("ops") == git(t.repo, "rev-parse", "HEAD")
          and not (ops / "notes.txt").exists(),
          f"with the override the untracked file goes and HEAD is at the ref: {result}")
    check((ops / "local.properties").exists() and (ops / "local.properties").read_text() == "sdk.dir=/opt/android\n"
          and (ops / "build" / "cache.bin").exists(),
          "and ignored local configuration and build caches are still there")


@case
def test_tracked_changes_refuse_and_the_override_discards_only_them(t: Tenant) -> None:
    ops = t.wt["ops"]
    (ops / "tracked.txt").write_text("an edit in progress\n", encoding="utf-8")
    t.advance()
    result, _ = t.refresh()
    check((ops / "tracked.txt").read_text() == "an edit in progress\n"
          and "1 tracked change: tracked.txt" in result.kept_roles.get("ops", ""),
          f"a tracked edit is kept and named: {result.kept_roles}")
    result, _ = t.refresh(discard=frozenset({"ops"}))
    check(result.ok and (ops / "tracked.txt").read_text() == (t.repo / "tracked.txt").read_text(),
          f"the explicit override discards it: {result}")


@case
def test_the_override_moves_head_off_an_ahead_branch_and_the_branch_keeps_its_commit(t: Tenant) -> None:
    git(t.wt["main"], "checkout", "-b", "pack/5b1-dusk-theme-today")
    (t.wt["main"] / "theme.txt").write_text("dusk\n", encoding="utf-8")
    git(t.wt["main"], "add", "theme.txt")
    git(t.wt["main"], "commit", "-m", "packet 5b1")
    packet = t.head("main")
    new_ref = t.advance()
    result, _ = t.refresh(discard=frozenset({"main"}))
    check(result.ok and t.head("main") == new_ref, f"HEAD is refreshed to the ref: {result}")
    detached = subprocess.run(["git", "-C", str(t.wt["main"]), "symbolic-ref", "-q", "HEAD"], capture_output=True)
    check(detached.returncode == 1 and git(t.wt["main"], "rev-parse", "pack/5b1-dusk-theme-today") == packet,
          "by detaching: the branch still holds its commit")


@case
def test_commits_on_no_branch_are_never_discarded_even_with_the_override(t: Tenant) -> None:
    (t.wt["main"] / "orphan.txt").write_text("work on a detached HEAD\n", encoding="utf-8")
    git(t.wt["main"], "add", "orphan.txt")
    git(t.wt["main"], "commit", "-m", "detached work")
    orphan = t.head("main")
    t.advance()
    for discard in (frozenset(), frozenset({"main"})):
        result, _ = t.refresh(discard=discard)
        check(t.head("main") == orphan and "1 commit on no branch and not in origin/main" in result.kept_roles.get("main", "")
              and "branch <name>" in result.kept_roles["main"],
              f"kept, override or not ({sorted(discard)}), with how to keep it: {result.kept_roles}")
    git(t.wt["main"], "branch", "keep/detached-work")
    result, _ = t.refresh()
    check(result.ok and not result.kept_roles and git(t.wt["main"], "rev-parse", "keep/detached-work") == orphan,
          f"once it has a branch, the refresh goes ahead and the commit stays on that branch: {result}")


@case
def test_a_clean_worktree_at_the_ref_refreshes_by_moving_head_alone(t: Tenant) -> None:
    new_ref = t.advance()
    recording = RecordingRunner()
    result, said = t.refresh(runner=recording)
    flat = [" ".join(map(str, call)) for call in recording.calls]
    check(result.ok and not result.kept_roles and t.head("main") == new_ref and t.head("ops") == new_ref and not said,
          f"both refresh, quietly: {result} {said!r}")
    check(not any(" reset " in call or " clean " in call for call in flat)
          and sum(" checkout --detach origin/main" in call for call in flat) == 2,
          f"by `checkout --detach`, never reset or clean: {flat}")


@case
def test_the_launch_carries_kept_worktrees_to_its_last_word(t: Tenant) -> None:
    (t.wt["ops"] / "notes.txt").write_text("draft\n", encoding="utf-8")
    t.advance()
    with contextlib.redirect_stderr(io.StringIO()):
        prepared = team_launcher._prepare_project_worktrees_for_launch(t.config, running_roles=[], runner=real_runner)
    check(set(prepared.kept_roles) == {"ops"} and prepared.ok, f"launch preparation keeps ops and fails nobody: {prepared}")
    lines: list[str] = []
    team_launcher.report_kept_worktrees(t.config, prepared.kept_roles, print_func=lines.append)
    check(lines[0] == "team-launcher: 1 role worktree of testing kept as they were, not refreshed, so no work was lost:"
          and lines[1].startswith("team-launcher:   ops: 1 untracked path: notes.txt"),
          f"the summary: {lines}")
    quiet: list[str] = []
    team_launcher.report_kept_worktrees(t.config, {}, print_func=quiet.append)
    check(quiet == [], "and nothing at all when nothing was kept")
    with contextlib.redirect_stderr(io.StringIO()):
        named = team_launcher._prepare_project_worktrees_for_launch(t.config, running_roles=[], runner=real_runner,
                                                                    discard=frozenset({"ops"}))
    check(named.ok and not named.kept_roles and not (t.wt["ops"] / "notes.txt").exists(),
          f"with nothing running, the operator's set still reaches the worktrees: {named}")


@case
def test_a_worktree_that_cannot_be_read_is_not_refreshed(t: Tenant) -> None:
    """Git answering without a count is not a count of zero: the refresh is refused, and the role does not start."""
    ops = next(role for role in t.config.roles if role.role == "ops")
    before = t.head("ops")
    t.advance()

    def no_count(args, **kwargs):
        if "rev-list" in args:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return real_runner(args, **kwargs)

    try:
        from scripts.project_worktrees import role_worktree_refresh_refusal
        outcome = role_worktree_refresh_refusal(t.config, ops, runner=no_count)
    except Exception as exc:  # a crash is not the refusal this asks for
        outcome = ("raised", repr(exc))
    check(outcome[0] == "failed" and "could not count commits not in origin/main" in outcome[1],
          f"an unreadable count refuses: {outcome}")
    result, _ = t.refresh(runner=no_count)
    check("ops" in result.failed_roles and t.head("ops") == before, f"and the worktree is left where it was: {result}")


@case
def test_with_a_role_running_only_stopped_ones_are_read_and_the_discard_set_reaches_them(t: Tenant) -> None:
    main_role = next(role for role in t.config.roles if role.role == "main")
    (t.wt["main"] / "live-edit.txt").write_text("the live role's work\n", encoding="utf-8")
    (t.wt["ops"] / "notes.txt").write_text("draft\n", encoding="utf-8")
    new_ref = t.advance()
    main_before = t.head("main")
    with contextlib.redirect_stderr(io.StringIO()):
        kept = team_launcher._prepare_project_worktrees_for_launch(t.config, running_roles=[main_role], runner=real_runner)
    check(set(kept.kept_roles) == {"ops"} and kept.ok and t.head("main") == main_before,
          f"the running role is not touched; the stopped one is kept and carried out: {kept}")
    with contextlib.redirect_stderr(io.StringIO()):
        discarded = team_launcher._prepare_project_worktrees_for_launch(
            t.config, running_roles=[main_role], runner=real_runner, discard=frozenset({"ops"}))
    check(discarded.ok and not discarded.kept_roles and t.head("ops") == new_ref and not (t.wt["ops"] / "notes.txt").exists()
          and (t.wt["main"] / "live-edit.txt").exists() and t.head("main") == main_before,
          f"the named stopped role is discarded and refreshed; the live one still is not: {discarded}")


def test_the_preparation_phase_hands_the_discard_set_to_the_worktrees_and_carries_what_was_kept() -> None:
    """P5 with the launch guard's own stand-ins: the operator's set goes down, the kept roles come back up."""
    import launch_phases_boundary_test as guard
    from scripts import launch_phases

    prep = guard.Preparation(running=("alpha",), failed={})
    prep.worktree_result = type(prep.worktree_result)(failed_roles={}, kept_roles={"beta": "1 untracked path: x"})
    cfg = guard.p5_config(control=True)
    with guard.patched(team_launcher, **prep.names(cfg)), contextlib.redirect_stderr(io.StringIO()):
        result = launch_phases._prepare_launch(
            cfg, allow_stale_launcher=False, config_path=guard.CONFIG_PATH, dry_run=False,
            effective_pane_state_dir=guard.PANES, mode="attach-or-start", no_launcher_self_deploy=False,
            owner_home=guard.OWNER_HOME, pane_script_path=guard.PANE_SCRIPT, print_func=lambda _line: None,
            runner=guard.caller_runner, worktree_runner=guard.worktree_runner, discard_worktree_changes=frozenset({"beta"}))
    worktrees = next(entry for entry in prep.log if entry[0] == "worktrees")
    check(worktrees[3] == frozenset({"beta"}), f"the worktree step is given the operator's set: {worktrees}")
    check(result.exit_code is None and result.kept_worktrees == {"beta": "1 untracked path: x"},
          f"and the phase hands the kept roles on: {result}")


def test_launch_project_ends_with_the_summary_and_hands_the_discard_set_down() -> None:
    """The orchestration, with each phase stood in: P5 gets the flag; the summary follows P9's report."""
    from scripts import launch_phases

    config = team_launcher.ProjectConfig.__new__(team_launcher.ProjectConfig)
    object.__setattr__(config, "project", "testing")
    object.__setattr__(config, "roles", [type("R", (), {"role": "ops"})()])
    object.__setattr__(config, "role_state_isolation", False)
    seen: dict = {}
    order: list[str] = []
    setup = type("S", (), dict(worktree_runner=None, role_process_runner=None, delegate_role_sessions_to_owner=False,
                               effective_pane_state_dir=Path("/nonexistent"), output_path=Path("/nonexistent"),
                               window_title="t", should_assign_layout_owner=False, pane_script_path=Path("/nonexistent")))()
    stand_ins = {
        "_launch_runners_and_paths": lambda c, **k: setup,
        "_prepare_launch": lambda c, **k: (seen.update(k), launch_phases.LaunchPreparation(
            exit_code=None, config=c, failed_roles={}, running_roles=[], reconcile_home=None, unreconciled_roles=set(),
            kept_worktrees={"ops": "1 untracked path: notes.txt"}))[1],
        "_write_layout_and_plan": lambda c, **k: None,
        "_start_workers_and_present": lambda c, **k: team_launcher.WorkerStartup(
            worker_start_exit_code=0, launch_started_at=0.0, launch_started_ns=0, resolved_layout_mode="auto"),
        "_report_launch": lambda c, **k: (order.append("report"), 0)[1],
    }
    saved = {name: getattr(team_launcher, name) for name in stand_ins}
    for name, value in stand_ins.items():
        setattr(team_launcher, name, value)
    try:
        printed: list[str] = []
        code = team_launcher.launch_project(config, config_path=Path("/nonexistent"), mode="attach", script_path=Path("/x"),
                                            dry_run=True, print_func=lambda line: (order.append("summary"), printed.append(line)),
                                            discard_worktree_changes=frozenset({"ops"}))
        check(code == 0 and seen.get("discard_worktree_changes") == frozenset({"ops"}),
              f"P5 is handed the operator's discard set: {seen.get('discard_worktree_changes')}")
        check(order[:2] == ["report", "summary"] and "ops: 1 untracked path: notes.txt" in printed[-1],
              f"and the launch's last lines are the kept worktrees: {order} {printed}")
        try:
            team_launcher.launch_project(config, config_path=Path("/nonexistent"), mode="attach", script_path=Path("/x"),
                                         dry_run=True, discard_worktree_changes=frozenset({"opps"}))
        except SystemExit as exc:
            check("testing has no role opps" in str(exc) and "nothing was discarded" in str(exc), str(exc))
        else:
            check(False, "a role the project does not have must stop the launch")
    finally:
        for name, value in saved.items():
            setattr(team_launcher, name, value)


def test_switchyard_start_passes_the_flag_to_the_launch() -> None:
    from scripts import switchyard_dispatch

    asked: dict = {}
    entry = type("E", (), {"slug": "testing", "config_path": Path("/nonexistent/testing.json")})()
    stand_ins = {
        "_resolve_switchyard_project": lambda project: entry,
        "_load_switchyard_project_config_for_command": lambda e, argv: "config",
        "resume_tenant": lambda config, config_path: [],
        "prepare_project_desktop": lambda config: config,
        "launch_project": lambda config, **k: (asked.update(k), 0)[1],
    }
    saved = {name: getattr(team_launcher, name) for name in stand_ins}
    for name, value in stand_ins.items():
        setattr(team_launcher, name, value)
    try:
        code = switchyard_dispatch.switchyard_main(["start", "testing", "--discard-worktree-changes", "ops",
                                                    "--discard-worktree-changes", "main"])
        check(code == 0 and asked.get("discard_worktree_changes") == frozenset({"ops", "main"}),
              f"both named roles reach the launch: {asked}")
        asked.clear()
        switchyard_dispatch.switchyard_main(["start", "testing"])
        check(asked.get("discard_worktree_changes") == frozenset(), f"and none unless named: {asked}")
    finally:
        for name, value in saved.items():
            setattr(team_launcher, name, value)


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"role_worktree_refresh_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
