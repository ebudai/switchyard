#!/usr/bin/env python3
"""SYRD-95: the listener has to read the directory the panes write to.

The notification listener decides whether a role is busy from per-pane hook
state on disk. Provisioning pointed its unit at the board service's
RuntimeDirectory instead of the owner's runtime directory, so it read a
directory no hook ever wrote to -- and systemd erases that directory whenever
the board service restarts, which is how a board deploy silently took delivery
away. Every pane then read as busy/no_hook_state, every notification deferred,
and nothing that checks liveness noticed: the process was up, its socket was
up, and the board's runtime assignments were complete.

Covers the five things that had to change: what provisioning renders, how a
deploy moves the listener across a migration, what a mismatched directory does,
that a restart keeps and then delivers pending work, and that delivery fails
closed while a process check would have passed.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from ticket_board_pane_env import strip_ticket_board_pane_env  # noqa: E402

strip_ticket_board_pane_env(os.environ)

from scripts.ticket_board.notify_listener import (  # noqa: E402
    PaneActivityGate,
    PaneHookStateStore,
    TicketBoardNotifyListener,
    pane_state_authority,
    registered_pane_targets,
)
from scripts.ticket_board.project_provision import (  # noqa: E402
    build_plan,
    render_listener_unit,
)

LISTENER_CLI = ROOT / "scripts" / "ticket-board-notify-listener"
SERVICE_SCRIPT = ROOT / "scripts" / "ticket-board-service.sh"


def unit_environment(unit_text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in unit_text.splitlines():
        if line.startswith("Environment="):
            key, _, value = line[len("Environment=") :].partition("=")
            values[key.strip().strip('"')] = value.strip().strip('"')
    return values


# --- 1. what provisioning renders ------------------------------------------


def single_account_plan():
    """One project account for every role: this tenant's model (SYRD-69)."""
    return build_plan(project="stellaris", owner_user="stellaris-agent", port=8871)


def role_account_plan():
    """Roles running as their own accounts: the model SYRD-39 was built for."""
    return dataclasses.replace(
        single_account_plan(),
        role_accounts=(("ops", "stellaris-ops"), ("audit", "stellaris-audit")),
    )


def test_provisioning_points_the_listener_at_the_directory_the_hooks_write_to() -> None:
    plan = single_account_plan()
    unit = render_listener_unit(plan)
    configured = unit_environment(unit)["TICKET_BOARD_PANE_STATE_DIR"]

    # %t is the runtime-directory specifier, and this unit is installed under
    # the owner's systemd/user, where %t is XDG_RUNTIME_DIR -- the same
    # directory the hook installer is handed below.
    assert configured == f"%t/{plan.runtime_directory}/pane-state", configured
    assert "WantedBy=default.target" in unit, unit

    # The old value, which this model must no longer produce. /run/<runtime> is
    # the board service's RuntimeDirectory: a directory the listener can read,
    # no hook writes to, and systemd deletes and recreates when the board
    # restarts.
    assert f"/run/{plan.runtime_directory}/pane-state" not in unit, unit

    # Nothing tenant-specific is baked in: no uid, no home, no absolute runtime
    # path. The project appears only as this plan's own slug.
    assert "/run/user/" not in configured, configured
    assert str(plan.owner_home) not in configured, configured


def test_roles_with_their_own_accounts_keep_the_shared_aggregation_path() -> None:
    """The fix is not "always %t"; it is "whatever the hooks actually use".

    A role account cannot write the owner's runtime directory and the listener
    cannot read each role's own, so those tenants aggregate in the board's
    runtime directory on purpose (SYRD-39). Read from the launcher rather than
    restated here, because the launcher is what puts the hooks there.
    """
    from scripts.team_launcher import shared_pane_state_dir

    plan = role_account_plan()
    configured = unit_environment(render_listener_unit(plan))["TICKET_BOARD_PANE_STATE_DIR"]
    assert configured == str(shared_pane_state_dir(plan.project)), configured
    assert "%t" not in configured, configured


def test_the_hook_installer_and_the_listener_are_given_one_directory() -> None:
    """Both halves derived from the owner's runtime directory, not restated."""
    plan = single_account_plan()
    from scripts.ticket_board.project_provision import render_operator_commands

    script = render_operator_commands(plan)
    assert 'owner_runtime_dir="/run/user/$owner_uid"' in script, "the installer stopped deriving the runtime dir"
    hook_dir = f'TICKET_BOARD_PANE_STATE_DIR="$owner_runtime_dir/{plan.runtime_directory}/pane-state"'
    assert hook_dir in script, hook_dir
    # The listener's %t and the installer's $owner_runtime_dir are the same
    # directory for the same account, and the tail of the path is shared, so
    # the two cannot drift apart by editing one of them.
    listener_dir = unit_environment(render_listener_unit(plan))["TICKET_BOARD_PANE_STATE_DIR"]
    assert listener_dir.endswith(f"/{plan.runtime_directory}/pane-state"), listener_dir
    assert hook_dir.endswith(f'/{plan.runtime_directory}/pane-state"'), hook_dir


def test_the_listener_follows_the_same_rule_the_launcher_applies_to_panes() -> None:
    """One rule, checked against the code that actually places the hooks.

    team_launcher.role_pane_state_dir decides where a pane's hooks write. If
    provisioning ever disagrees with it again, the listener goes back to
    reading a directory nobody writes to, which is the whole of SYRD-95.
    """
    from scripts import team_launcher

    for plan, expected in (
        (role_account_plan(), str(team_launcher.shared_pane_state_dir("stellaris"))),
        (single_account_plan(), f"%t/{single_account_plan().runtime_directory}/pane-state"),
    ):
        configured = unit_environment(render_listener_unit(plan))["TICKET_BOARD_PANE_STATE_DIR"]
        assert configured == expected, (plan.role_accounts, configured, expected)

    # And the single-account expansion is the owner's runtime directory, which
    # is what default_pane_state_dir_for_user computes for that same account.
    owner_form = team_launcher.default_pane_state_dir_for_user  # named so a rename is visible here
    assert callable(owner_form)


# --- 2. how a deploy moves the listener across a migration -----------------


DEPLOY_ORDER_HARNESS = r"""
set -euo pipefail
source "$SERVICE_SCRIPT"

record() { printf '%s\n' "$1" >>"$ORDER_FILE"; }

deploy_export_release() { printf '%s\t%s\n' "deadbeef" "$BOARD_ROOT/releases/deadbeef"; }
current_release_dir() { printf '%s\n' "$BOARD_ROOT/releases/previous"; }
resolved_service_scope() { printf 'system\n'; }
assert_system_unit_reload_not_required_for_release() { record assert-no-reload; }
apply_database_migrations_for_release() { record migrate; return $MIGRATE_RESULT; }
run_release_canary() { record canary; return $CANARY_RESULT; }
activate_release() { record activate; }
verify_current_release_sha() { record verify-sha; }
restart_live_service() { record restart-board; }
smoke_check_http() { record smoke; return $SMOKE_RESULT; }
verify_live_build_id() { record build-id; }
verify_post_deploy_system_runtime() { record post-runtime; }
rollback_live_service() { record rollback; }
verify_listener_pane_state_authority() { record verify-pane-state; return $PANE_STATE_RESULT; }
listener_is_installed() { return 0; }
log() { :; }
systemctl_user() {
    case "$*" in
        "is-active --quiet $LISTENER_SERVICE_NAME") return 0 ;;
        "stop $LISTENER_SERVICE_NAME") record stop-listener ;;
        "start $LISTENER_SERVICE_NAME") record start-listener; return $LISTENER_START_RESULT ;;
        *) : ;;
    esac
}

# Called plainly, on its own line. Wrapping it in `... || something` would put
# it in a context where errexit does not apply inside the function, so a failing
# migration would return instead of ending the shell -- and this file would then
# be testing a control flow the real deploy does not have.
deploy_restart_service
"""


class DeployRun:
    def __init__(self, order: list[str], status: int) -> None:
        self.order = order
        self.status = status

    def __repr__(self) -> str:  # shown when an assertion fails
        return f"DeployRun(status={self.status}, order={self.order})"

    def __contains__(self, item: str) -> bool:
        return item in self.order

    def index(self, item: str) -> int:
        return self.order.index(item)


def deploy_call_order(
    *,
    smoke_result: int = 0,
    pane_state_result: int = 0,
    migrate_result: int = 0,
    canary_result: int = 0,
    listener_start_result: int = 0,
) -> DeployRun:
    """Run the real deploy_restart_service with its collaborators recorded."""
    with tempfile.TemporaryDirectory(prefix="syrd95-deploy.") as tmp:
        order_file = Path(tmp) / "order"
        order_file.touch()
        env = {
            **{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")},
            "SERVICE_SCRIPT": str(SERVICE_SCRIPT),
            "ORDER_FILE": str(order_file),
            "SMOKE_RESULT": str(smoke_result),
            "PANE_STATE_RESULT": str(pane_state_result),
            "MIGRATE_RESULT": str(migrate_result),
            "CANARY_RESULT": str(canary_result),
            "LISTENER_START_RESULT": str(listener_start_result),
            "TICKET_BOARD_PROJECT": "stellaris",
            "TICKET_BOARD_COMMIT_GIT_DIR": str(Path(tmp) / "source.git"),
            "TICKET_BOARD_OWNER_HOME": tmp,
            "BOARD_ROOT": str(Path(tmp) / "live"),
        }
        proc = subprocess.run(
            ["bash", "-c", DEPLOY_ORDER_HARNESS], env=env, capture_output=True, text=True, check=False
        )
        return DeployRun(order_file.read_text(encoding="utf-8").split(), proc.returncode)


def test_the_deploy_takes_the_listener_out_of_the_way_before_it_migrates() -> None:
    """The crash that started this: an old listener reading a new document.

    A listener still running the previous release met workflow vocabulary it
    did not know and exited. It cannot meet it while stopped, and nothing is
    lost by stopping it, because pending notifications are database rows.
    """
    order = deploy_call_order()
    assert "stop-listener" in order, order
    assert order.index("stop-listener") < order.index("migrate"), order
    # Back on the deployed release, after the board it talks to is up.
    assert order.index("activate") < order.index("start-listener"), order
    assert order.index("restart-board") < order.index("start-listener"), order
    # And then checked, not merely started.
    assert order.index("start-listener") < order.index("verify-pane-state"), order
    assert "rollback" not in order, order


def test_a_listener_that_cannot_see_pane_state_rolls_the_deploy_back() -> None:
    order = deploy_call_order(pane_state_result=1)
    assert order.index("verify-pane-state") < order.index("rollback"), order
    assert "post-runtime" not in order, order


def test_a_failed_smoke_check_still_brings_the_listener_back() -> None:
    """Rolling back the board must not leave delivery stopped."""
    order = deploy_call_order(smoke_result=1)
    assert "rollback" in order, order
    assert "start-listener" in order, order
    assert order.index("rollback") < order.index("start-listener"), order


def test_a_failed_migration_does_not_leave_the_listener_stopped() -> None:
    """The path no branch covers: errexit ends the shell mid-deploy.

    A migration that fails takes the whole script down where it stands. Every
    explicit restart in this function is downstream of that point, so without a
    trap the tenant is left with the listener stopped and no notification
    delivery at all -- a worse outage than the one being deployed against.
    """
    run = deploy_call_order(migrate_result=17)
    assert run.status == 17, run
    assert run.index("stop-listener") < run.index("migrate"), run
    assert "start-listener" in run, run
    assert run.index("migrate") < run.index("start-listener"), run
    # It got no further, so nothing downstream ran.
    assert "activate" not in run, run
    assert "restart-board" not in run, run


def test_a_failed_canary_does_not_leave_the_listener_stopped() -> None:
    """The same hole, one step later, before the release is ever activated."""
    run = deploy_call_order(canary_result=9)
    assert run.status == 9, run
    assert run.index("canary") < run.index("start-listener"), run
    assert "activate" not in run, run


def test_a_listener_that_will_not_start_is_reported_not_swallowed() -> None:
    """The deploy's own failure stays the reported one.

    The restart runs while the shell is already exiting. If it fails, that is
    worth saying out loud, but replacing the exit status would hide why the
    deploy stopped in the first place.
    """
    run = deploy_call_order(migrate_result=17, listener_start_result=1)
    assert run.status == 17, run
    assert "start-listener" in run, run


# --- 3. a mismatched directory ---------------------------------------------


def test_a_directory_no_pane_writes_to_is_a_failure_not_an_empty_result() -> None:
    targets = ("stellaris-audit:0.0", "stellaris-ops:0.0")
    with tempfile.TemporaryDirectory(prefix="syrd95-mismatch.") as tmp:
        store = PaneHookStateStore(tmp)

        mismatched = pane_state_authority(targets, store)
        assert mismatched.ok is False, mismatched
        assert str(store.state_dir) in mismatched.describe()
        assert "none of the 2 registered roles" in mismatched.describe(), mismatched.describe()

        # One pane writing is enough to prove the two halves agree; a pane that
        # has not run a hook yet is not a disagreement.
        store.write("stellaris-audit:0.0", "idle", source="codex.Stop", now=100.0)
        agreeing = pane_state_authority(targets, store)
        assert agreeing.ok is True, agreeing
        assert "stellaris-ops:0.0" in agreeing.describe(), agreeing.describe()

        # A board with nobody registered has nobody to serve.
        assert pane_state_authority((), store).ok is True


def test_the_check_reads_the_roles_the_board_says_are_live() -> None:
    payload = {
        "project": "stellaris",
        "assignments": {
            "audit": {"role": "audit", "actual_target": "stellaris-audit:0.0"},
            "ops": {"role": "ops", "actual_target": "stellaris-ops:0.0"},
        },
    }
    assert registered_pane_targets(payload) == ("stellaris-audit:0.0", "stellaris-ops:0.0")
    assert registered_pane_targets({}) == ()
    assert registered_pane_targets({"assignments": {}}) == ()


def test_the_command_line_check_exits_nonzero_on_a_mismatch() -> None:
    """The form a deploy actually calls."""
    payload = {"assignments": {"audit": {"actual_target": "stellaris-audit:0.0"}}}
    with tempfile.TemporaryDirectory(prefix="syrd95-cli.") as tmp:
        assignments = Path(tmp) / "assignments.json"
        assignments.write_text(json.dumps(payload), encoding="utf-8")
        state_dir = Path(tmp) / "pane-state"
        state_dir.mkdir()

        def check() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable,
                    str(LISTENER_CLI),
                    "--verify-pane-state-authority",
                    "--assignments-json",
                    str(assignments),
                    "--pane-state-dir",
                    str(state_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        failed = check()
        assert failed.returncode == 1, failed.stdout + failed.stderr
        assert "none of the 1 registered roles" in failed.stdout, failed.stdout

        PaneHookStateStore(state_dir).write("stellaris-audit:0.0", "idle", source="codex.Stop")
        passed = check()
        assert passed.returncode == 0, passed.stdout + passed.stderr
        assert "1 of 1 registered roles" in passed.stdout, passed.stdout


# --- 4 and 5. a real queue, a real gate, a real database -------------------


SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
PANE_ROLES = ("director", "user", "ops", "app", "audit", "inspector", "perf", "research", "main")


def psql(conninfo: str, sql: str) -> str:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conninfo],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip()


def ticket_source(ticket_id: str, title: str, state: str, assignee: str) -> str:
    payload = {
        "id": ticket_id,
        "title": title,
        "body": "",
        "state": state,
        "assignee": assignee,
        "comments": [],
        "created": "2026-09-09T00:00:00+00:00",
        "updated": "2026-09-09T00:00:00+00:00",
    }
    return json.dumps(payload, sort_keys=True).replace("'", "''")


def gate_for(state_dir: Path) -> PaneActivityGate:
    """The production gate, reading real files, with tmux stubbed out.

    Only the tmux probes are faked; the decision under test is the one made
    from hook state on disk, which is the half that broke.
    """

    def cursor(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout="2 23 24\n")

    def capture(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout="")

    return PaneActivityGate(
        state_store=PaneHookStateStore(state_dir),
        cursor_position_runner=cursor,
        capture_pane_runner=capture,
    )


def queued_ids(conninfo: str) -> list[str]:
    rows = psql(conninfo, "SELECT id::text FROM ticket_board.ticket_notification_queue ORDER BY id;")
    return rows.split() if rows else []


def run_database_checks(cluster) -> None:
    dbname = "syrd95_listener_authority"
    admin = f"host={cluster.socket_dir} port={cluster.port} dbname={dbname} user=postgres"
    listener_conninfo = f"host={cluster.socket_dir} port={cluster.port} dbname={dbname} user=ticket_board_listener"
    subprocess.run(
        ["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname],
        check=True,
        capture_output=True,
    )
    psql(admin, SCHEMA_PATH.read_text(encoding="utf-8"))
    psql(admin, "\n".join(f'CREATE ROLE "{role}" LOGIN;' for role in PANE_ROLES))
    psql(admin, RBAC_PATH.read_text(encoding="utf-8"))
    psql(
        admin,
        f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    'PGU-950', 'Waiting on a listener', '', 'backlog', 'ops', '',
    '2026-09-09T00:00:00+00:00', '2026-09-09T00:00:00+00:00',
    '{ticket_source("PGU-950", "Waiting on a listener", "backlog", "ops")}'::jsonb
);
UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-950';
UPDATE ticket_board.ticket_notification_queue
SET next_attempt_at = clock_timestamp() - interval '10 seconds';
""",
    )
    pending = queued_ids(admin)
    assert len(pending) == 1, pending
    notification_id = pending[0]

    with tempfile.TemporaryDirectory(prefix="syrd95-live.") as tmp:
        # The directory the deploy left behind: readable, and empty, because no
        # hook writes here.
        wrong_dir = Path(tmp) / "board-runtime" / "pane-state"
        wrong_dir.mkdir(parents=True)
        right_dir = Path(tmp) / "user-runtime" / "pane-state"
        right_dir.mkdir(parents=True)

        # --- fails closed, and a liveness check would not have noticed -------
        sent: list[tuple[str, str]] = []
        stalled = TicketBoardNotifyListener(
            conninfo=listener_conninfo,
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate_for(wrong_dir).is_working,
            target_exists=lambda _target: True,
            poll_seconds=0,
        )
        stalled.listen_once(max_notifications=1)
        assert sent == [], sent
        assert queued_ids(admin) == [notification_id], queued_ids(admin)
        determination = psql(
            admin,
            "SELECT coalesce(pane_busy_determination,'')||'/'||coalesce(busy_reason,'') "
            f"FROM ticket_board.notification_trace WHERE notification_id = {notification_id} "
            "AND busy_reason IS NOT NULL ORDER BY id DESC LIMIT 1;",
        )
        assert determination == "busy/no_hook_state", determination

        # Everything a liveness check looks at is fine here: the process ran,
        # it reached the database, it claimed the row and it put it back. Only
        # the directory comparison can tell that this is a stall.
        assert pane_state_authority(("pgu-ops:0.0",), PaneHookStateStore(wrong_dir)).ok is False
        assert pane_state_authority(("pgu-ops:0.0",), PaneHookStateStore(right_dir)).ok is False

        # --- a restart rediscovers the hook state and delivers what was held --
        #
        # The pane wrote its state all along, to the directory the hooks use.
        # Nothing re-enqueues the notification: it is the same row, still in
        # the queue, picked up by the listener that comes back.
        PaneHookStateStore(right_dir).write("pgu-ops:0.0", "idle", source="codex.Stop")
        assert queued_ids(admin) == [notification_id], queued_ids(admin)
        psql(
            admin,
            "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() - interval '10 seconds';",
        )
        restarted = TicketBoardNotifyListener(
            conninfo=listener_conninfo,
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate_for(right_dir).is_working,
            target_exists=lambda _target: True,
            poll_seconds=0,
        )
        assert restarted.listen_once(max_notifications=1) == 1
        assert len(sent) == 1 and sent[0][0] == "pgu-ops:0.0", sent
        assert "PGU-950" in sent[0][1], sent
        assert queued_ids(admin) == [], queued_ids(admin)
        assert pane_state_authority(("pgu-ops:0.0",), PaneHookStateStore(right_dir)).ok is True

        # --- and a busy pane is still not interrupted ------------------------
        psql(
            admin,
            """
SELECT ticket_board.enqueue_notification(
    'PGU-950',
    'transition',
    'ops',
    'PGU-950 -- Waiting on a listener needs you',
    jsonb_build_object(
        'kind', 'transition',
        'id', 'PGU-950',
        'new_state', 'in_progress',
        'assignee', 'ops',
        'target_role', 'ops',
        'message', 'PGU-950 -- Waiting on a listener needs you'
    ),
    'syrd95-busy-pane-must-not-be-interrupted'
);
UPDATE ticket_board.ticket_notification_queue
SET next_attempt_at = clock_timestamp() - interval '10 seconds';
""",
        )
        PaneHookStateStore(right_dir).write("pgu-ops:0.0", "busy", source="codex.PreInvocation")
        before = queued_ids(admin)
        assert len(before) == 1, before
        busy_listener = TicketBoardNotifyListener(
            conninfo=listener_conninfo,
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate_for(right_dir).is_working,
            target_exists=lambda _target: True,
            poll_seconds=0,
        )
        busy_listener.listen_once(max_notifications=1)
        assert len(sent) == 1, sent
        assert queued_ids(admin) == before, (before, queued_ids(admin))
        # Held for the right reason: a pane that said it was working, not a
        # directory that could not answer.
        held = psql(
            admin,
            "SELECT coalesce(busy_reason,'') FROM ticket_board.notification_trace "
            f"WHERE notification_id = {before[0]} AND busy_reason IS NOT NULL ORDER BY id DESC LIMIT 1;",
        )
        assert held and held != "no_hook_state", held


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    from temporary_cluster import temporary_cluster

    with temporary_cluster(prefix="syrd95-listener-", shutdown="immediate") as cluster:
        run_database_checks(cluster)
    print(f"ticket_board_listener_pane_state_authority_test: ok ({len(tests)} tests + database checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
