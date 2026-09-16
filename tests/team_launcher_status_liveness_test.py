#!/usr/bin/env python3
"""SYRD-170: `switchyard list` called a healthy project stopped.

SYRD-169 replaced argv-marker liveness in privileged recovery and left the same
search in the status path, which I said so at the time and this is the ticket
for it. A long-running role CLI has exec'd past its environment wrapper, so
`TICKET_BOARD_PANE_TARGET=<target>` is no longer in its argv and every pane of a
healthy long-running project reads as stopped.

The status path now rests on the same two durable sources the privileged path
does -- the tenant owner's own tmux server, and the board's runtime assignments
checked against /proc by pid, start time and uid -- through the same
`pane_liveness`, so there is one definition of "this pane is up" rather than two.

A listing walks every registered project as whoever typed it, so it asks
non-interactively and with a deadline: one tenant that cannot answer is one
unknown row, not a hung terminal and not an exception that ends the listing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher  # noqa: E402

CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def tenant(root: Path, slug: str, *, roles: list[str], owner: str = "tenant-agent") -> Path:
    """One registered project, written the way the registry and loader expect."""
    layout = root / f"{slug}-layout.json"
    layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
    config_path = root / "configs" / f"{slug}.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({
            "desktop_access": {"mode": "headless"},
            "project": slug,
            "project_name": slug.title(),
            "run_as_user": owner,
            "layout": str(layout),
            "repository": str(root / f"{slug}-repo"),
            "roles": [
                {"role": name, "slot": index, "target": f"{slug}-{name}:0.0",
                 "tmux_session": f"{slug}-{name}", "cli": ["claude"]}
                for index, name in enumerate(roles)
            ],
        }) + "\n",
        encoding="utf-8",
    )
    registry = root / "registry"
    registry.mkdir(exist_ok=True)
    (registry / f"{slug}.json").write_text(
        json.dumps({
            "schema": launcher.SWITCHYARD_REGISTRY_SCHEMA,
            "slug": slug, "name": slug.title(), "config_path": str(config_path),
        }) + "\n",
        encoding="utf-8",
    )
    return config_path


def statuses(root: Path, **kwargs):
    return launcher.switchyard_project_statuses(
        config_dir=root / "configs", registry_dir=root / "registry", **kwargs
    )


def assignment(target: str, pid: int, start: int, uid: int) -> dict:
    return {"actual_target": target, "process_pid": pid,
            "process_start_time": start, "process_uid": uid}


def test_a_long_running_execed_pane_is_running_not_stopped() -> None:
    """The live shape. Its argv no longer names the target; it is still up."""
    with tempfile.TemporaryDirectory(prefix="syrd170-live.") as tmp:
        root = Path(tmp)
        tenant(root, "atlas", roles=["director"])
        mine = __import__("os").getpid()
        started = launcher.process_start_ticks(mine)
        uid = launcher.process_owner_uid(mine)
        with patch.object(launcher, "uid_for_user", lambda _name: uid):
            rows = statuses(
                root,
                owner_tmux_reader=lambda _config: ({"atlas-director:0.0"}, ""),
                assignments_reader=lambda _config: (
                    {"director": assignment("atlas-director:0.0", mine, started, uid)}, ""
                ),
            )
    check(len(rows) == 1 and rows[0].state == "running", f"a live project is running: {rows}")
    check(rows[0].panes_up == 1 and rows[0].panes_total == 1, f"and counted: {rows}")

    # And what the old proof would have said about the same pane.
    role = SimpleNamespace(role="director", target="atlas-director:0.0")
    check(not launcher._role_has_pane_process(role, ["/usr/bin/claude --resume"]),
          "the argv search could not see it, which is the whole defect")


def test_a_stale_assignment_does_not_count_as_a_pane() -> None:
    """The board's row has to still be true of the machine."""
    with tempfile.TemporaryDirectory(prefix="syrd170-stale.") as tmp:
        root = Path(tmp)
        tenant(root, "atlas", roles=["director"])
        with patch.object(launcher, "uid_for_user", lambda _name: 1006):
            rows = statuses(
                root,
                owner_tmux_reader=lambda _config: ({"atlas-director:0.0"}, ""),
                # A pid that is not running at all: the row outlived the process.
                assignments_reader=lambda _config: (
                    {"director": assignment("atlas-director:0.0", 2 ** 22 - 1, 1, 1006)}, ""
                ),
            )
    check(rows[0].panes_up == 0, f"a stale row is not a pane: {rows}")
    check(rows[0].state == "stopped", f"and the project is stopped: {rows}")


def test_one_unreachable_tenant_does_not_take_the_listing_with_it() -> None:
    """Explicitly unknown for that tenant, and the others still answer."""
    with tempfile.TemporaryDirectory(prefix="syrd170-many.") as tmp:
        root = Path(tmp)
        tenant(root, "atlas", roles=["director"])
        tenant(root, "borea", roles=["director", "ops"])

        def tmux_for(config):
            if config.project == "atlas":
                return set(), "the owner's tmux server could not be asked: sudo: a password is required"
            return {"borea-director:0.0", "borea-ops:0.0"}, ""

        with patch.object(launcher, "uid_for_user", lambda _name: 1006):
            rows = {row.slug: row for row in statuses(
                root, owner_tmux_reader=tmux_for, assignments_reader=lambda _config: ({}, ""),
            )}
    check(set(rows) == {"atlas", "borea"}, f"every tenant still has a row: {rows}")
    check(rows["atlas"].state == "unknown", f"the one that could not answer says so: {rows['atlas']}")
    check(rows["atlas"].panes_up is None, f"rather than claiming zero: {rows['atlas']}")
    check("password is required" in (rows["atlas"].error or ""), f"with the reason: {rows['atlas']}")
    check(rows["borea"].state == "running" and rows["borea"].panes_up == 2,
          f"and the reachable one is unaffected: {rows['borea']}")


def test_an_unreachable_board_degrades_the_row_without_erasing_the_panes() -> None:
    """tmux saw the panes. The board not answering is worth saying, not inventing."""
    with tempfile.TemporaryDirectory(prefix="syrd170-board.") as tmp:
        root = Path(tmp)
        tenant(root, "atlas", roles=["director"])
        with patch.object(launcher, "uid_for_user", lambda _name: 1006):
            rows = statuses(
                root,
                owner_tmux_reader=lambda _config: ({"atlas-director:0.0"}, ""),
                assignments_reader=lambda _config: ({}, "the board's runtime assignments could not be read: refused"),
            )
    check(rows[0].panes_up == 1, f"the pane tmux saw is still counted: {rows}")
    check("could not be read" in (rows[0].error or ""), f"and the degradation is stated: {rows}")


def test_the_probe_is_bounded_and_never_waits_for_a_password() -> None:
    """A listing must not hang, whoever runs it and whatever a tenant is doing."""
    with tempfile.TemporaryDirectory(prefix="syrd170-bounded.") as tmp:
        root = Path(tmp)
        tenant(root, "atlas", roles=["director"])
        seen: list[tuple[list[str], object]] = []

        def hanging(args, **kwargs):
            seen.append((list(args), kwargs.get("timeout")))
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout") or 0)

        began = time.monotonic()
        with patch.object(launcher, "current_user_name", lambda: "somebody-else"):
            with patch.object(launcher, "uid_for_user", lambda _name: 1006):
                rows = statuses(root, runner=hanging, probe_timeout_seconds=0.25)
        elapsed = time.monotonic() - began
    check(rows[0].state == "unknown", f"a tenant that does not answer is unknown: {rows}")
    check("did not answer within" in (rows[0].error or ""), f"and says it timed out: {rows}")
    check(elapsed < 5, f"without waiting on it: {elapsed:.2f}s")
    argv, timeout = seen[0]
    check(timeout == 0.25, f"the deadline is passed to the probe: {timeout}")
    check(argv[0] == "sudo" and "-n" in argv, f"and sudo cannot prompt: {argv}")
    check("root" not in argv, f"nor is root's own tmux server asked: {argv}")


def test_the_status_path_uses_the_privileged_definition_of_live() -> None:
    """One definition, not two: the status row is computed by `pane_liveness`.

    Asserted by making that function say something only it could say, and
    watching the row follow it.
    """
    calls: list[str] = []

    def only_ops_is_live(config, role, **kwargs):
        calls.append(role.role)
        return launcher.PaneLiveness(role.role, role.role == "ops", "fixture verdict")

    with tempfile.TemporaryDirectory(prefix="syrd170-shared.") as tmp:
        root = Path(tmp)
        tenant(root, "borea", roles=["director", "ops"])
        with patch.object(launcher, "pane_liveness", only_ops_is_live):
            with patch.object(launcher, "uid_for_user", lambda _name: 1006):
                rows = statuses(
                    root,
                    owner_tmux_reader=lambda _config: ({"borea-director:0.0", "borea-ops:0.0"}, ""),
                    assignments_reader=lambda _config: ({}, ""),
                )
    check(sorted(calls) == ["director", "ops"], f"every role goes through it: {calls}")
    check(rows[0].panes_up == 1, f"and the count is its verdict: {rows}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"team_launcher_status_liveness_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
