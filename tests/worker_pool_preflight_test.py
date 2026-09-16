#!/usr/bin/env python3
"""SYRD-37: a declared pool of interchangeable workers, and what it would cost.

The milestone is a reusable capability, so nothing here is about the project
that asked for it: a pool is a name, a runtime and a size that a tenant
declares, and the same code answers for one worker or sixty-four.

This suite covers the two halves that exist so far -- the declaration, and the
preflight that says what bringing it up would change and what would stop it.
The preflight reads only, which is the property most worth pinning: an operator
asked to upgrade a live tenant is owed the whole list before anything moves.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher  # noqa: E402

CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def write_config(tmp_path: Path, *, pool: dict | None, roles: list[tuple[str, str]]) -> Path:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"Orientation": "Horizontal", "Widgets": []}), encoding="utf-8"
    )
    repository = tmp_path / "repo"
    repository.mkdir(exist_ok=True)
    payload = {
        "project": "stellaris",
        "ticket_prefix": "STL",
        "layout": str(layout),
        "repository": str(repository),
        "run_as_user": team_launcher.current_user_name(),
        "session_dir": str(tmp_path / "state" / "sessions"),
        "board_url": "http://127.0.0.1:26623",
        "board_socket": "/run/stellaris/board.sock",
        "roles": [
            {
                "role": role,
                "slot": index,
                "target": f"stellaris-{role}:0.0",
                "tmux_session": f"stellaris-{role}",
                "workdir": str(tmp_path / "worktrees" / role),
                "cli": [cli],
            }
            for index, (role, cli) in enumerate(roles)
        ],
    }
    if pool is not None:
        payload["worker_pool"] = pool
    for role, _cli in roles:
        (tmp_path / "worktrees" / role).mkdir(parents=True, exist_ok=True)
    path = tmp_path / "stellaris.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load(tmp_path: Path, **kwargs):
    return team_launcher.load_project_config("stellaris", write_config(tmp_path, **kwargs))


POOL = {"name": "impl", "runtime": "hermes", "size": 8, "kind": "implementer"}
PERSISTENT = [("director", "codex"), ("audit", "claude"), ("main", "codex"), ("ops", "codex")]


def ready_runner(*, installed: bool = True, authenticated: bool = True):
    """A CLI that is installed and logged in, unless a case says otherwise."""

    def runner(args, **_kwargs):
        command = [str(part) for part in args]
        if command[-2:-1] == ["-c"] and command[-1].startswith("command -v "):
            return subprocess.CompletedProcess(
                command, 0 if installed else 1, stdout="/usr/bin/hermes\n" if installed else ""
            )
        if command[-2:] == ["config", "check"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="\N{CHECK MARK} OPENROUTER_API_KEY\n" if authenticated else "no keys\n",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    return runner


def declared_board(*roles: str) -> dict:
    return {"revision": 4, "document": {"roles": [{"name": role} for role in roles]}}


# --------------------------------------------------------------------------
# the declaration
# --------------------------------------------------------------------------


def test_a_pool_is_a_name_a_runtime_and_a_size() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd37-declare.") as tmp:
        config = load(Path(tmp), pool=POOL, roles=PERSISTENT)

    pool = config.worker_pool
    check(pool is not None, "the project declares one")
    check(pool.members == tuple(f"impl-{index}" for index in range(1, 9)), f"members: {pool.members}")
    check(pool.ephemeral is True, "a pool worker starts each ticket clean unless told otherwise")
    check(pool.presentation == "on-demand", "and holds no permanent pane by default")
    check(
        [role.role for role in config.roles] == [role for role, _cli in PERSISTENT],
        "declaring a pool adds no roles by itself; the persistent ones are untouched",
    )


def test_a_project_without_a_pool_is_unchanged() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd37-none.") as tmp:
        config = load(Path(tmp), pool=None, roles=PERSISTENT)
    check(config.worker_pool is None, "no pool is declared")
    findings = team_launcher.worker_pool_preflight(config, runner=ready_runner())
    check(
        len(findings) == 1 and "declares no worker pool" in findings[0].detail,
        f"and the preflight says so rather than inventing one: {findings}",
    )


def test_a_pool_the_system_could_not_run_is_refused_when_it_is_read() -> None:
    """Refusals happen where the configuration is read, not at first use."""
    cases = {
        "size": {"name": "impl", "runtime": "hermes", "size": 0},
        "huge": {"name": "impl", "runtime": "hermes", "size": 65},
        "name": {"name": "Impl Pool", "runtime": "hermes", "size": 2},
        "runtime": {"name": "impl", "runtime": "", "size": 2},
        "presentation": {"name": "impl", "runtime": "hermes", "size": 2, "presentation": "maybe"},
        "unknown": {"name": "impl", "runtime": "hermes", "size": 2, "colour": "green"},
    }
    for label, pool in cases.items():
        with tempfile.TemporaryDirectory(prefix=f"syrd37-bad-{label}.") as tmp:
            try:
                load(Path(tmp), pool=pool, roles=PERSISTENT)
            except SystemExit as exc:
                check("worker_pool" in str(exc), f"{label}: the refusal names the field: {exc}")
            else:
                raise AssertionError(f"{label}: {pool} was accepted")


def test_the_size_and_runtime_are_the_tenant_s_own() -> None:
    """Nothing about eight, or Hermes, or this project is written into the code."""
    with tempfile.TemporaryDirectory(prefix="syrd37-other.") as tmp:
        config = load(
            Path(tmp),
            pool={"name": "review", "runtime": "codex", "size": 2, "kind": "auditor",
                  "presentation": "attached", "ephemeral": False},
            roles=PERSISTENT,
        )
    pool = config.worker_pool
    check(pool.members == ("review-1", "review-2"), f"a pool of two: {pool.members}")
    check(pool.kind == "auditor" and pool.runtime == "codex", "of whatever kind and runtime")
    check(pool.ephemeral is False and pool.presentation == "attached", "with the tenant's policy")


# --------------------------------------------------------------------------
# the preflight
# --------------------------------------------------------------------------


def test_the_preflight_says_what_would_change_and_changes_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd37-preflight.") as tmp:
        tmp_path = Path(tmp)
        config_path = write_config(tmp_path, pool=POOL, roles=PERSISTENT)
        before = config_path.read_text(encoding="utf-8")
        config = team_launcher.load_project_config("stellaris", config_path)
        findings = team_launcher.worker_pool_preflight(
            config,
            owner_home=tmp_path / "home",
            board_workflow=declared_board("director", "audit", "main", "ops"),
            runner=ready_runner(),
        )
        after = config_path.read_text(encoding="utf-8")
        listed = sorted(path.name for path in tmp_path.iterdir())

    check(before == after, "the configuration is untouched")
    check(
        "state" not in listed,
        f"and nothing was created beside it: {listed}",
    )
    detail = " ".join(finding.detail for finding in findings)
    check("8 worker role(s) would be added" in detail, f"it names what it would add: {detail}")
    check("impl-1" in detail and "impl-8" in detail, "and which identities those are")
    check(
        any("not done here" == finding.subject for finding in findings),
        "and says plainly that it created nothing",
    )


def test_the_board_not_knowing_the_workers_is_a_blocker() -> None:
    """A worker nobody can route to or notify is not a worker."""
    with tempfile.TemporaryDirectory(prefix="syrd37-board.") as tmp:
        tmp_path = Path(tmp)
        config = load(tmp_path, pool=POOL, roles=PERSISTENT)
        legacy = team_launcher.worker_pool_preflight(
            config, owner_home=tmp_path / "home", board_workflow=None, runner=ready_runner()
        )
        partial = team_launcher.worker_pool_preflight(
            config,
            owner_home=tmp_path / "home",
            board_workflow=declared_board("director", "impl-1", "impl-2"),
            runner=ready_runner(),
        )
        complete = team_launcher.worker_pool_preflight(
            config,
            owner_home=tmp_path / "home",
            board_workflow=declared_board(*(f"impl-{index}" for index in range(1, 9))),
            runner=ready_runner(),
        )

    check(
        any(f.blocking and "built-in workflow" in f.detail for f in legacy),
        f"a board with no declared workflow blocks: {[f.detail for f in legacy if f.blocking]}",
    )
    unregistered = [f for f in partial if f.blocking and f.subject == "board"]
    check(unregistered, "so does one that knows only some of them")
    check(
        "impl-3" in unregistered[0].detail and "impl-1" not in unregistered[0].detail,
        f"naming exactly the ones it does not know: {unregistered[0].detail}",
    )
    check(
        not any(f.blocking and f.subject == "board" for f in complete),
        "and a board that knows every worker is no blocker at all",
    )


def test_the_runtime_must_be_installed_and_authenticated_for_the_owner() -> None:
    """Learned from SYRD-191: a CLI that is not logged in opens its first run."""
    with tempfile.TemporaryDirectory(prefix="syrd37-runtime.") as tmp:
        tmp_path = Path(tmp)
        config = load(tmp_path, pool=POOL, roles=PERSISTENT)
        board = declared_board(*(f"impl-{index}" for index in range(1, 9)))
        missing = team_launcher.worker_pool_preflight(
            config, owner_home=tmp_path / "home", board_workflow=board,
            runner=ready_runner(installed=False),
        )
        unauthenticated = team_launcher.worker_pool_preflight(
            config, owner_home=tmp_path / "home", board_workflow=board,
            runner=ready_runner(authenticated=False),
        )
        ready = team_launcher.worker_pool_preflight(
            config, owner_home=tmp_path / "home", board_workflow=board, runner=ready_runner(),
        )

    check(
        any(f.blocking and "not installed" in f.detail for f in missing),
        f"an absent runtime blocks, with how to install it: {[f.detail for f in missing]}",
    )
    check(
        any(f.blocking and "first run" in f.detail for f in unauthenticated),
        "an unauthenticated one blocks, saying what every worker would open instead",
    )
    check(not any(f.blocking for f in ready), f"and a ready runtime blocks nothing: {ready}")


def test_a_worker_name_somebody_else_already_uses_is_a_blocker() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd37-collide.") as tmp:
        tmp_path = Path(tmp)
        config = load(
            tmp_path,
            pool={"name": "impl", "runtime": "hermes", "size": 2},
            roles=[*PERSISTENT, ("impl-1", "codex")],
        )
        findings = team_launcher.worker_pool_preflight(
            config, owner_home=tmp_path / "home",
            board_workflow=declared_board("impl-1", "impl-2"), runner=ready_runner(),
        )

    collision = [f for f in findings if f.blocking and f.subject == "role names"]
    check(collision, f"the collision blocks: {[f.detail for f in findings]}")
    check("impl-1" in collision[0].detail, "naming the role it would have taken over")


def test_a_pool_already_declared_is_not_reported_as_new_work() -> None:
    """Idempotent: running the preflight again after the pool exists is quiet."""
    with tempfile.TemporaryDirectory(prefix="syrd37-again.") as tmp:
        tmp_path = Path(tmp)
        config = load(
            tmp_path,
            pool={"name": "impl", "runtime": "hermes", "size": 2},
            roles=[*PERSISTENT, ("impl-1", "hermes"), ("impl-2", "hermes")],
        )
        findings = team_launcher.worker_pool_preflight(
            config, owner_home=tmp_path / "home",
            board_workflow=declared_board("impl-1", "impl-2"), runner=ready_runner(),
        )

    check(not any(f.blocking for f in findings), f"nothing blocks: {[f.detail for f in findings]}")
    check(
        any("every worker is already declared" in f.detail for f in findings),
        f"and it says there is nothing to add: {[f.detail for f in findings]}",
    )


def test_the_report_separates_blockers_from_changes() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd37-report.") as tmp:
        tmp_path = Path(tmp)
        config = load(tmp_path, pool=POOL, roles=PERSISTENT)
        findings = team_launcher.worker_pool_preflight(
            config, owner_home=tmp_path / "home", board_workflow=None,
            runner=ready_runner(authenticated=False),
        )
        lines = team_launcher.format_worker_pool_preflight(config, findings)

    check(lines[0].startswith("switchyard: worker pool preflight for stellaris:"), lines[0])
    check("2 blocker(s)" in lines[0], f"the count is in the first line: {lines[0]}")
    check(sum(1 for line in lines if "BLOCKER" in line) == 2, f"and marked on each: {lines}")
    check(
        lines[-1].startswith("switchyard: nothing was changed."),
        f"and the last word is that nothing happened: {lines[-1]}",
    )


def test_the_command_is_discoverable_and_says_it_only_reads() -> None:
    check("worker-pool" in team_launcher.SWITCHYARD_COMMANDS, "the command is registered")
    check("worker-pool" in team_launcher.switchyard_help_text(), "and listed in the help")
    help_text = " ".join(team_launcher._build_switchyard_worker_pool_parser().format_help().split())
    for promised in ("Reads only", "no role, account, worktree, board registration or session"):
        check(promised in help_text, f"the help promises it: {promised!r} in {help_text}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"worker_pool_preflight_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
