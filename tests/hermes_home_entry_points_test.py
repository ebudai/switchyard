#!/usr/bin/env python3
"""SYRD-563: every entry point runs a Hermes role from the same HERMES_HOME.

Otto (2026-10-07, release 2951c5c9): after `switchyard present otto recover`, a
Hermes role ran from `~/.local/state/otto-ticket-board/hermes-homes/<pane>` --
empty memories -- instead of its live `.../pane-sessions/roles/hermes-homes/<pane>`.
`hermes_home_for_role` follows the session directory its caller holds, and
recover passed the project-wide `config.session_dir` where launch and start pass
the role's own store. The display's "resumable" probe and the launch-status
record read that project-wide store too, and the credential seeding wrote
Hermes's key file into the project-wide tree.

The tenant here uses role-state isolation, with DIFFERENT marker state in the
project-wide (legacy) home and the role's own (current) home, and a different
session record in each store. Every entry point is driven through its own code
-- recover through `switchyard_present_command` -- with only its process start
recorded; each must resolve the current home and never read the legacy one.
The one way legacy state moves is the explicit cutover, once, without
overwriting. Nothing here starts a provider or touches a real home.
"""

from __future__ import annotations

import ast
import json
import os
import pwd
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import team_launcher_presentation_test as presentation_fixture  # noqa: E402
from scripts import role_credentials, role_identity_cutover, session_records, team_launcher  # noqa: E402
from scripts import presentation_controller, presentation_display_session  # noqa: E402

CHECKS = 0
ME = pwd.getpwuid(os.geteuid()).pw_name


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Tenant:
    """A Hermes `app` role with state in both trees: which one an entry point reads is visible."""

    def __init__(self, root: Path, *, isolated: bool = True, session_dir: Path | None = None) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.config_path = presentation_fixture._write_presentation_config(root)
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.home = root / "home" / ME
        raw["session_dir"] = str(session_dir or self.home / ".local" / "state" / "porter-ticket-board" / "pane-sessions")
        raw["role_state_isolation"] = isolated
        raw["desktop_access"] = {"mode": "headless"}
        for role in raw["roles"]:
            if role["role"] == "app":
                role.update(cli=["hermes"], live_commands=["hermes"])
        self.config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.config = team_launcher.load_project_config("porter", self.config_path)
        self.role = next(role for role in self.config.roles if role.role == "app")
        self.legacy_store = self.config.session_dir
        self.current_store = team_launcher.role_session_dir(self.config, self.role)
        self.legacy_home = team_launcher.hermes_home_for_role(self.role, session_dir=self.legacy_store)
        self.current_home = team_launcher.role_hermes_home(self.config, self.role)
        for home, marker in ((self.legacy_home, "legacy"), (self.current_home, "current")):
            (home / "memories").mkdir(parents=True, exist_ok=True)
            (home / "memories" / "MARKER").write_text(marker, encoding="utf-8")
        for store, session in ((self.legacy_store, "legacy-session"), (self.current_store, "current-session")):
            store.mkdir(parents=True, exist_ok=True)
            (store / team_launcher.session_file_name(self.role.target)).write_text(
                json.dumps({"target": self.role.target, "session_id": session}), encoding="utf-8")

    def marker_of(self, session_dir: Path) -> str:
        """Which tree an entry point that holds `session_dir` runs Hermes from."""
        home = team_launcher.hermes_home_for_role(self.role, session_dir=session_dir)
        return read(home / "memories" / "MARKER") or "no state at all"


def read(path: Path) -> str | None:
    """A file's text, or None: a missing file is a failed check, never a crash."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def sandboxed(case):
    def run() -> None:
        with tempfile.TemporaryDirectory(prefix="syrd563.") as tmp:
            saved = {key: os.environ.get(key) for key in ("HOME", "TICKET_BOARD_CALLER_ROLE", "TICKET_BOARD_PROJECT",
                                                         "TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR")}
            os.environ["HOME"] = tmp  # nothing here may reach the real home
            for key in ("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR"):
                os.environ.pop(key, None)
            try:
                case(Path(tmp))
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
    run.__name__ = case.__name__
    return run


@sandboxed
def test_the_two_trees_differ_so_the_cases_mean_something(tmp: Path) -> None:
    tenant = Tenant(tmp)
    check(tenant.legacy_home != tenant.current_home
          and tenant.current_home == tenant.current_store.parent / "hermes-homes" / "porter-app_0.0"
          and tenant.legacy_home == tenant.legacy_store.parent / "hermes-homes" / "porter-app_0.0",
          f"current {tenant.current_home}, legacy {tenant.legacy_home}")


@sandboxed
def test_present_recover_runs_the_role_from_its_own_home(tmp: Path) -> None:
    """Otto's entry point: the recovery start is handed the role's own store."""
    tenant = Tenant(tmp)
    args = team_launcher._build_switchyard_present_parser().parse_args(["porter", "recover", "app"])
    starts: list[Path] = []
    saved = (team_launcher.prepare_project_desktop, team_launcher.ensure_visible_role_session_for_viewer,
             team_launcher.run_switchyard_launch_first_run_auth)
    saved_assignments = presentation_controller.runtime_assignment_config
    # The board's runtime assignments are read over HTTP; stood in for, unchanged.
    presentation_controller.runtime_assignment_config = lambda config, **_kw: config
    os.environ.update(presentation_fixture.DIRECTOR_ENV)
    team_launcher.prepare_project_desktop = lambda candidate, **_kw: candidate
    team_launcher.ensure_visible_role_session_for_viewer = lambda role, **kw: starts.append(kw["session_dir"]) or 0
    team_launcher.run_switchyard_launch_first_run_auth = lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("consent"))
    try:
        with redirect_stdout(StringIO()):
            result = team_launcher.switchyard_present_command(
                tenant.config, config_path=tenant.config_path, args=args,
                runner=presentation_fixture.AttachingPresentationRunner())
    finally:
        (team_launcher.prepare_project_desktop, team_launcher.ensure_visible_role_session_for_viewer,
         team_launcher.run_switchyard_launch_first_run_auth) = saved
        presentation_controller.runtime_assignment_config = saved_assignments
    check(result == 0 and len(starts) == 1, f"one recovery start: {result} {starts}")
    check(starts[0] == tenant.current_store and tenant.marker_of(starts[0]) == "current",
          f"recover runs Hermes from the current home, not the legacy one: {starts[0]} -> {tenant.marker_of(starts[0])}")


@sandboxed
def test_the_command_every_start_execs_names_the_current_home(tmp: Path) -> None:
    """Launch, start, the runtime switch and recover all start a role through this command."""
    tenant = Tenant(tmp)
    command = team_launcher.cli_command_for_role(tenant.role, session_dir=tenant.current_store,
                                                 pane_state_dir=tmp / "pane-state", resume=True)
    joined = " ".join(command)
    check(f"HERMES_HOME={tenant.current_home}" in joined and str(tenant.legacy_home) not in joined
          and "current-session" in joined and "legacy-session" not in joined,
          f"the started process gets the current home and the current session: {joined}")


@sandboxed
def test_present_and_launch_status_read_the_role_store(tmp: Path) -> None:
    tenant = Tenant(tmp)
    (tenant.current_store / team_launcher.session_file_name(tenant.role.target)).unlink()

    def runner(args, **_kw):
        return subprocess.CompletedProcess(args, 0, stdout="0\n", stderr="")

    status = presentation_display_session._role_status(tenant.config, "app", runner=runner)
    check(status["resumable"] is False,
          f"present reports resumable from the role's own store, not the legacy record beside it: {status}")
    statuses = session_records.report_launch_session_records(
        tenant.config, [tenant.role], attached_roles=[tenant.role], print_func=lambda _t: None)
    check([s.session_id for s in statuses] == [""],
          f"and launch status does not report the legacy session as this role's: {statuses}")


@sandboxed
def test_credentials_are_seeded_into_the_home_the_role_runs_from(tmp: Path) -> None:
    tenant = Tenant(tmp)
    artifact = role_credentials.ROLE_CREDENTIAL_ARTIFACTS["hermes"][0]
    base, relative = role_credentials._role_credential_target(tenant.config, tenant.role, artifact, role_home=tenant.home)
    check(base / relative == tenant.current_home / Path(artifact.relative_path).name,
          f"the key file lands in the current home: {base / relative}")
    plain = Tenant(tmp / "plain", isolated=False)
    base, relative = role_credentials._role_credential_target(plain.config, plain.role, artifact, role_home=plain.home)
    check(base / relative == plain.legacy_home / Path(artifact.relative_path).name,
          "and a tenant without isolation keeps the project-wide home it always ran from")
    outside = Tenant(tmp / "outside", session_dir=tmp / "outside" / "sessions")
    base, relative = role_credentials._role_credential_target(outside.config, outside.role, artifact, role_home=outside.home)
    check(base / relative == outside.current_home / Path(artifact.relative_path).name,
          f"a session store outside any home still seeds the home the role runs from: {base / relative}")


def test_no_entry_point_hands_a_role_start_the_project_wide_store() -> None:
    """Every call that starts or describes a role names its session store through role_session_dir."""
    starters = {"ensure_visible_role_session_for_viewer", "run_detached_role", "run_role_pane", "attach_role_to_slot",
                "cli_command_for_role", "prepare_hermes_home_for_role", "hermes_home_for_role", "session_id_for_role"}
    offenders = []
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name not in starters:
                continue
            given = [kw.value for kw in node.keywords if kw.arg == "session_dir"] + list(node.args[1:2])
            for value in given:
                if isinstance(value, ast.Attribute) and value.attr == "session_dir":
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {ast.unparse(node)[:90]}")
    allowed = {"scripts/role_identity_cutover.py"}  # the explicit migration reads the project-wide tree on purpose
    check([o for o in offenders if o.split(":")[0] not in allowed] == [],
          f"no entry point passes a project-wide store: {offenders}")


@sandboxed
def test_the_cutover_moves_legacy_hermes_state_once_and_never_back(tmp: Path) -> None:
    """The one explicit, one-way migration: copied without overwriting; the legacy tree left as it was."""
    tenant = Tenant(tmp, isolated=False)
    target_home = team_launcher.hermes_home_for_role(tenant.role, session_dir=tenant.legacy_store / "roles" / "app")
    check(not target_home.exists(), "precondition: the role has no home of its own yet")
    (tenant.legacy_home / "memories" / "MARKER").write_text("legacy", encoding="utf-8")  # one tree before isolation
    for name, text in (("state.db", "legacy-db"), ("state.db-wal", "legacy-wal")):
        (tenant.legacy_home / name).write_text(text, encoding="utf-8")

    def runner(args, **_kw):  # every role is stopped: tmux has no session
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

    os.environ.update(presentation_fixture.DIRECTOR_ENV)
    saved = team_launcher._resume_preflight_allows_attempt
    team_launcher._resume_preflight_allows_attempt = lambda *_a, **_kw: (True, "")
    try:
        changed, problems = role_identity_cutover.repatriate_role_runtime_state(
            tenant.config, config_path=tenant.config_path, runner=runner)
        check(not problems, problems)
        check(read(target_home / "memories" / "MARKER") == "legacy"
              and read(target_home / "state.db") == "legacy-db" and read(target_home / "state.db-wal") == "legacy-wal",
              "the legacy memories and the whole database move into the role's own home")
        check((tenant.legacy_home / "memories" / "MARKER").read_text() == "legacy", "and the legacy tree is left as it was")
        (target_home / "memories" / "MARKER").write_text("worked-since", encoding="utf-8")
        role_identity_cutover.repatriate_role_runtime_state(tenant.config, config_path=tenant.config_path, runner=runner)
        check((target_home / "memories" / "MARKER").read_text() == "worked-since",
              "one way: a later run never overwrites the role's own state with the legacy tree's")
    finally:
        team_launcher._resume_preflight_allows_attempt = saved


@sandboxed
def test_a_database_is_never_split_across_two_homes(tmp: Path) -> None:
    tenant = Tenant(tmp, isolated=False)
    target_home = team_launcher.hermes_home_for_role(tenant.role, session_dir=tenant.legacy_store / "roles" / "app")
    (tenant.legacy_home / "state.db").write_text("legacy-db", encoding="utf-8")
    (tenant.legacy_home / "state.db-wal").write_text("legacy-wal", encoding="utf-8")
    target_home.mkdir(parents=True)
    (target_home / "state.db").write_text("own-db", encoding="utf-8")
    role_identity_cutover._copy_hermes_home(tenant.legacy_home, target_home)
    check((target_home / "state.db").read_text() == "own-db" and not (target_home / "state.db-wal").exists(),
          "a role that already has a database never gets another database's write-ahead log beside it")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"hermes_home_entry_points_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
