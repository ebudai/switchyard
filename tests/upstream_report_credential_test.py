#!/usr/bin/env python3
"""SYRD-238: the report-only credential a tenant files upstream with.

A board compares a submitted report token against one configured string, and
that string is minted when the board's own environment file is first written.
A cutover writes a new one, so every credential handed out before it stops
working -- silently, because the only thing that notices is a tenant trying to
report that something is wrong.

MEFP's copy was from 2026-08-31 and its Director could not file at all. Nothing
in the tree had ever written that credential: it was an operator-supplied path,
so there was nothing to refresh.
"""

from __future__ import annotations

import json
import os
import signal
import stat
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
UPSTREAM = "http://127.0.0.1:23326"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def _sandbox_home(tmp: Path):
    """The ONLY home any case here may resolve.

    The first version of this suite took the owner's home from
    `home_dir_for_user`, which on a host is correct and in a test is a live
    credential file: it wrote test tokens into the running board's own
    `~/.config/syrd/ticket-board.env`. Every case now injects this instead, and
    `test_the_shipped_default_reads_the_real_passwd_home` is the one place the
    real resolver is named at all (SYRD-238).
    """
    home = tmp / "sandbox-home"
    home.mkdir(parents=True, exist_ok=True)

    def resolver(_user: str):
        return home

    return home, resolver


def _host(tmp: Path, *, board_url: str = UPSTREAM):
    """A registry, an upstream project's artifact, and its board credential."""
    owner = team_launcher.current_user_name()
    home, resolver = _sandbox_home(tmp)
    registry = tmp / "registry"
    registry.mkdir(parents=True, exist_ok=True)
    artifact = tmp / "upstream.json"
    artifact.write_text(
        json.dumps({"project": "syrd", "board_url": board_url, "run_as_user": owner}),
        encoding="utf-8",
    )
    (registry / "syrd.json").write_text(
        json.dumps({
            "schema": "switchyard.project-registry.v1",
            "slug": "syrd", "name": "Switchyard", "config_path": str(artifact),
        }),
        encoding="utf-8",
    )
    board_env = home / ".config" / "syrd" / "ticket-board.env"
    return registry, board_env, owner, resolver


def _tenant(tmp: Path, *, upstream_url: str = "", token_file: str = ""):
    """A tenant whose home is inside the sandbox, so nothing real is written."""
    owner = team_launcher.current_user_name()
    layout = tmp / "layout.json"
    layout.write_text(
        json.dumps({"Orientation": "Horizontal", "Widgets": [
            {"Command": "", "SessionRestoreId": i, "WorkingDirectory": ""} for i in range(2)]}),
        encoding="utf-8",
    )
    (tmp / "repo").mkdir(exist_ok=True)
    payload = {
        "project": "mefp",
        "project_name": "Morfane's Epic Fix Patch",
        "layout": str(layout),
        "repository": str(tmp / "repo"),
        "run_as_user": owner,
        "session_dir": str(tmp / "state" / "pane-sessions"),
        "role_state_isolation": True,
        "desktop_access": {"mode": "headless"},
        "roles": [
            {"role": role, "slot": index, "cli": ["claude"], "live_commands": ["claude"],
             "target": f"mefp-{role}:0.0", "tmux_session": f"mefp-{role}"}
            for index, role in enumerate(("director", "main"))
        ],
    }
    if upstream_url:
        payload["upstream_report_url"] = upstream_url
    if token_file:
        payload["upstream_report_token_file"] = token_file
    path = tmp / "mefp.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return team_launcher.load_project_config("mefp", path), path


# --------------------------------------------------------------------------
# Finding the board a tenant reports to
# --------------------------------------------------------------------------


def test_the_upstream_board_is_resolved_from_the_host_registry() -> None:
    """Not configured a second time: the upstream board is a project here."""
    with tempfile.TemporaryDirectory(prefix="syrd238-resolve.") as raw:
        tmp = Path(raw)
        registry, board_env, _owner, resolver = _host(tmp)
        project, path, problem = team_launcher.upstream_report_board(
            UPSTREAM, registry_dir=registry, home_for_user=resolver
        )
    check(problem == "", f"a registered board resolves: {problem}")
    check(project == "syrd", f"naming the project that serves it: {project}")
    check(path == board_env, f"and its own credential file: {path}")


def test_a_url_nothing_serves_is_named_rather_than_guessed() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd238-unknown.") as raw:
        tmp = Path(raw)
        registry, _board_env, _owner, resolver = _host(tmp)
        _project, _path, problem = team_launcher.upstream_report_board(
            "http://127.0.0.1:9999", registry_dir=registry, home_for_user=resolver
        )
    check("no project registered on this host serves" in problem, problem)
    check("9999" in problem, f"and says which URL: {problem}")


def test_a_trailing_slash_is_the_same_board() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd238-slash.") as raw:
        tmp = Path(raw)
        registry, _board_env, _owner, resolver = _host(tmp)
        project, _path, problem = team_launcher.upstream_report_board(
            UPSTREAM + "/", registry_dir=registry, home_for_user=resolver
        )
    check(problem == "" and project == "syrd", f"{project!r} {problem!r}")


# --------------------------------------------------------------------------
# Reading only the one value that may travel
# --------------------------------------------------------------------------


def test_only_the_report_token_is_read_from_a_board_environment() -> None:
    """A write token sitting beside it must not become a tenant's credential."""
    with tempfile.TemporaryDirectory(prefix="syrd238-readenv.") as raw:
        env = Path(raw) / "ticket-board.env"
        env.write_text(
            "# a board environment\n"
            "TICKET_BOARD_WRITE_TOKEN=write-token-must-not-travel\n"
            "TICKET_BOARD_TENANT_REPORT_TOKEN=report-token-abc\n"
            "TICKET_BOARD_SOMETHING_ELSE=x\n",
            encoding="utf-8",
        )
        token, problem = team_launcher._board_env_report_token(env)
    check(problem == "", problem)
    check(token == "report-token-abc", f"the report token, and only it: {token!r}")


def test_a_board_environment_without_a_report_token_is_reported() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd238-noenv.") as raw:
        env = Path(raw) / "ticket-board.env"
        env.write_text("TICKET_BOARD_WRITE_TOKEN=only-a-write-token\n", encoding="utf-8")
        token, problem = team_launcher._board_env_report_token(env)
        check(token == "" and "no TICKET_BOARD_TENANT_REPORT_TOKEN" in problem, f"{token!r} {problem}")

        empty = Path(raw) / "empty.env"
        empty.write_text("TICKET_BOARD_TENANT_REPORT_TOKEN=\n", encoding="utf-8")
        token, problem = team_launcher._board_env_report_token(empty)
        check(token == "" and "empty" in problem, f"{token!r} {problem}")

        token, problem = team_launcher._board_env_report_token(Path(raw) / "absent.env")
        check(token == "" and "cannot be read" in problem, f"{token!r} {problem}")


# --------------------------------------------------------------------------
# Writing it where only the tenant can read it
# --------------------------------------------------------------------------


def _refresh(config, registry: Path, resolver, printed: list[str], dry_run: bool = False):
    return team_launcher.refresh_upstream_report_credential(
        config, dry_run=dry_run, registry_dir=registry,
        home_for_user=resolver, print_func=printed.append,
    )


def test_the_credential_is_written_private_and_owned_by_the_tenant() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd238-write.") as raw:
        tmp = Path(raw)
        registry, board_env, owner, resolver = _host(tmp)
        board_env.parent.mkdir(parents=True, exist_ok=True)
        board_env.write_text("TICKET_BOARD_TENANT_REPORT_TOKEN=rotated-token-1\n", encoding="utf-8")
        home, _ = _sandbox_home(tmp)
        destination = home / ".config" / "mefp" / "upstream-report.env"
        config, _path = _tenant(tmp, upstream_url=UPSTREAM, token_file=str(destination))
        printed: list[str] = []

        problems = _refresh(config, registry, resolver, printed)
        check(problems == [], f"written without complaint: {problems}")
        info = destination.lstat()
        check(stat.S_IMODE(info.st_mode) == 0o600, f"0600: {oct(stat.S_IMODE(info.st_mode))}")
        check(info.st_uid == os.getuid(), f"owned by the tenant: {info.st_uid}")
        body = destination.read_text(encoding="utf-8")
        check(
            body.strip() == "TICKET_BOARD_TENANT_REPORT_TOKEN=rotated-token-1",
            f"it holds the report token and nothing else: {body!r}",
        )
        check("WRITE_TOKEN" not in body, f"no write authority travelled: {body!r}")

        # Idempotent: the same upgrade run twice changes nothing.
        before = destination.stat().st_mtime_ns
        printed.clear()
        problems = _refresh(config, registry, resolver, printed)
        check(problems == [], f"a second run is quiet: {problems}")
        check(destination.stat().st_mtime_ns == before, "and does not rewrite the file")
        check(any("already holds" in line for line in printed), f"saying so: {printed}")

        # A rotated upstream token is taken, which is the whole ticket.
        board_env.write_text("TICKET_BOARD_TENANT_REPORT_TOKEN=rotated-token-2\n", encoding="utf-8")
        printed.clear()
        problems = _refresh(config, registry, resolver, printed)
        check(problems == [], f"the rotation is taken: {problems}")
        check(
            destination.read_text(encoding="utf-8").strip().endswith("rotated-token-2"),
            destination.read_text(encoding="utf-8"),
        )


def test_a_dry_run_says_what_it_would_write_and_writes_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd238-dry.") as raw:
        tmp = Path(raw)
        registry, board_env, owner, resolver = _host(tmp)
        board_env.parent.mkdir(parents=True, exist_ok=True)
        board_env.write_text("TICKET_BOARD_TENANT_REPORT_TOKEN=abc\n", encoding="utf-8")
        home, _ = _sandbox_home(tmp)
        destination = home / ".config" / "mefp" / "never-written.env"
        config, _path = _tenant(tmp, upstream_url=UPSTREAM, token_file=str(destination))
        printed: list[str] = []
        problems = _refresh(config, registry, resolver, printed, dry_run=True)
    check(problems == [], f"{problems}")
    check(not destination.exists(), "nothing was written")
    check(any("would give" in line for line in printed), f"but it said what it would do: {printed}")


def test_a_tenant_with_no_upstream_board_is_left_alone() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd238-none.") as raw:
        tmp = Path(raw)
        registry, _board_env, _owner, resolver = _host(tmp)
        config, _path = _tenant(tmp)
        printed: list[str] = []
        problems = _refresh(config, registry, resolver, printed)
    check(problems == [], f"no upstream board is not a problem: {problems}")
    check(printed == [], f"and nothing is said about it: {printed}")


def test_an_unreachable_upstream_credential_is_reported_not_invented() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd238-missing.") as raw:
        tmp = Path(raw)
        registry, _board_env, _owner, resolver = _host(tmp)
        home, _ = _sandbox_home(tmp)
        destination = home / ".config" / "mefp" / "credential.env"
        config, _path = _tenant(tmp, upstream_url=UPSTREAM, token_file=str(destination))
        printed: list[str] = []
        problems = _refresh(config, registry, resolver, printed)
    check(len(problems) == 1, f"the failure is reported: {problems}")
    check("cannot be read" in problems[0] or "no TICKET_BOARD" in problems[0], problems[0])
    check(not destination.exists(), "and no credential is invented")


def test_a_destination_outside_the_tenants_home_is_refused() -> None:
    """Root is writing this. It goes in the account's own tree or nowhere."""
    with tempfile.TemporaryDirectory(prefix="syrd238-outside.") as raw:
        tmp = Path(raw)
        home, resolver = _sandbox_home(tmp)
        outside = tmp / "outside.env"
        problem = team_launcher._write_owner_private_file(
            outside, "TICKET_BOARD_TENANT_REPORT_TOKEN=x\n",
            owner_user=team_launcher.current_user_name(), home_for_user=resolver,
        )
    check("is not inside" in problem, f"refused by path: {problem}")
    check(not outside.exists(), "and nothing was written there")


# --------------------------------------------------------------------------
# Recording it, so the panes carry it
# --------------------------------------------------------------------------


def test_the_link_is_recorded_and_the_next_launch_carries_all_three() -> None:
    """The acceptance shape: a pane that needs no flags.

    `_role_board_env` has emitted these for a while, gated on two config keys
    that could only be supplied to `switchyard new`. A tenant provisioned
    before that, or one whose board moved, had no way to acquire them.
    """
    with tempfile.TemporaryDirectory(prefix="syrd238-record.") as raw:
        tmp = Path(raw)
        config, path = _tenant(tmp)
        check(
            "TICKET_BOARD_REPORT_URL" not in config.roles[0].env,
            "before: the pane carries no report URL",
        )
        printed: list[str] = []
        _home, resolver = _sandbox_home(tmp)
        updated, problems = team_launcher.record_upstream_report_link(
            config, config_path=path, upstream_report_url=UPSTREAM,
            home_for_user=resolver, print_func=printed.append,
        )
        check(problems == [], f"recorded: {problems}")
        reloaded = team_launcher.load_project_config("mefp", path)
        env = reloaded.roles[0].env

    check(env.get("TICKET_BOARD_REPORT_URL") == UPSTREAM, f"the URL reaches the pane: {env.get('TICKET_BOARD_REPORT_URL')}")
    check(env.get("TICKET_BOARD_REPORT_ORIGIN_PROJECT") == "mefp", f"and the origin project: {env.get('TICKET_BOARD_REPORT_ORIGIN_PROJECT')}")
    check(
        env.get("TICKET_BOARD_TENANT_REPORT_TOKEN_FILE", "").endswith("upstream-report.env"),
        f"and the credential path: {env.get('TICKET_BOARD_TENANT_REPORT_TOKEN_FILE')}",
    )
    check(
        "TICKET_BOARD_TENANT_REPORT_TOKEN" not in env,
        "the token itself never goes in the pane environment",
    )
    check(updated.upstream_report_url == UPSTREAM, "and the caller gets the updated config back")


def test_the_recorded_path_defaults_to_the_tenants_own_config_directory() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd238-default.") as raw:
        tmp = Path(raw)
        config, path = _tenant(tmp)
        home, resolver = _sandbox_home(tmp)
        team_launcher.record_upstream_report_link(
            config, config_path=path, upstream_report_url=UPSTREAM,
            home_for_user=resolver, print_func=lambda _l: None,
        )
        recorded = json.loads(path.read_text(encoding="utf-8"))
    check(
        recorded["upstream_report_token_file"] == str(home / ".config" / "mefp" / "upstream-report.env"),
        f"the same shape provisioning uses: {recorded['upstream_report_token_file']}",
    )


def test_recording_the_same_link_twice_writes_once() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd238-idempotent.") as raw:
        tmp = Path(raw)
        config, path = _tenant(tmp)
        _home, resolver = _sandbox_home(tmp)
        updated, _ = team_launcher.record_upstream_report_link(
            config, config_path=path, upstream_report_url=UPSTREAM,
            home_for_user=resolver, print_func=lambda _l: None,
        )
        before = path.stat().st_mtime_ns
        printed: list[str] = []
        team_launcher.record_upstream_report_link(
            updated, config_path=path, upstream_report_url=UPSTREAM,
            home_for_user=resolver, print_func=printed.append,
        )
        check(
            path.stat().st_mtime_ns == before,
            "the second run does not rewrite the configuration",
        )
        check(printed == [], f"and says nothing: {printed}")


def test_the_token_is_never_written_into_the_configuration() -> None:
    """Every role can read that file; `load_project_config` refuses a token in it."""
    with tempfile.TemporaryDirectory(prefix="syrd238-inline.") as raw:
        tmp = Path(raw)
        config, path = _tenant(tmp)
        raw_config = json.loads(path.read_text(encoding="utf-8"))
        raw_config["upstream_report_token"] = "should-not-survive"
        path.write_text(json.dumps(raw_config), encoding="utf-8")
        _home, resolver = _sandbox_home(tmp)
        team_launcher.record_upstream_report_link(
            config, config_path=path, upstream_report_url=UPSTREAM,
            home_for_user=resolver, print_func=lambda _l: None,
        )
        recorded = json.loads(path.read_text(encoding="utf-8"))
    check("upstream_report_token" not in recorded, f"the inline token is removed: {sorted(recorded)}")
    check("tenant_report_token" not in recorded, "and so is the legacy spelling")


def _load_cutover():
    import importlib.machinery, importlib.util

    path = ROOT / "tests" / "team_launcher_upgrade_cutover_test.py"
    loader = importlib.machinery.SourceFileLoader("cutover_for_syrd238", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_the_upgrade_itself_records_and_refreshes() -> None:
    """The call site, not just the functions.

    A refresh nothing calls is a tenant that still cannot file a report. This
    drives the real `upgrade_project_command` through the cutover suite's own
    harness and watches for both halves.
    """
    cutover = _load_cutover()
    calls: list[str] = []
    saved_record = team_launcher.record_upstream_report_link
    saved_refresh = team_launcher.refresh_upstream_report_credential

    def record(config, **kwargs):
        calls.append(f"record:{kwargs.get('upstream_report_url', '')}")
        return config, []

    def refresh(config, **kwargs):
        calls.append("refresh")
        return []

    with tempfile.TemporaryDirectory(prefix="syrd238-callsite.") as tmp:
        config_path, _ = cutover._declarative_tenant(Path(tmp), accounts=True)
        cutover._mark_projection_migrated(config_path)
        try:
            team_launcher.record_upstream_report_link = record
            team_launcher.refresh_upstream_report_credential = refresh
            with cutover._RunningTenant(config_path, account_uid=os.getuid()) as tenant:
                tenant.live = False
                result, output, _migrations = cutover._upgrade(
                    config_path, as_root=True, exists=set(),
                    runner=tenant.runner(), board=cutover._board_with_marker(True),
                )
        finally:
            team_launcher.record_upstream_report_link = saved_record
            team_launcher.refresh_upstream_report_credential = saved_refresh

    check(result == 0, f"the upgrade completes: {output[-500:]}")
    check("refresh" in calls, f"the upgrade refreshes the credential: {calls}")
    check(any(c.startswith("record:") for c in calls), f"and records the link: {calls}")


def test_a_report_credential_problem_does_not_fail_the_upgrade() -> None:
    """A tenant that cannot be given a credential still gets its upgrade.

    The credential matters, but an upgrade that refuses to finish over it would
    leave the tenant on the old release as well as unable to report -- strictly
    worse than saying so and carrying on.
    """
    cutover = _load_cutover()
    saved = team_launcher.refresh_upstream_report_credential

    def refuse(config, **kwargs):
        return ["mefp cannot be given syrd's report credential: nothing to read"]

    with tempfile.TemporaryDirectory(prefix="syrd238-callsite-fail.") as tmp:
        config_path, _ = cutover._declarative_tenant(Path(tmp), accounts=True)
        cutover._mark_projection_migrated(config_path)
        try:
            team_launcher.refresh_upstream_report_credential = refuse
            with cutover._RunningTenant(config_path, account_uid=os.getuid()) as tenant:
                tenant.live = False
                result, output, _migrations = cutover._upgrade(
                    config_path, as_root=True, exists=set(),
                    runner=tenant.runner(), board=cutover._board_with_marker(True),
                )
        finally:
            team_launcher.refresh_upstream_report_credential = saved

    check(result == 0, f"the upgrade still completes: {result}")
    check("nothing to read" in output, f"and says what went wrong: {output[-400:]}")
    check(
        "keeps the report credential it had" in output,
        f"and what that means for the tenant: {output[-400:]}",
    )


def test_the_shipped_default_reads_the_real_passwd_home() -> None:
    """Every case above injects a sandbox home. This names the real one once.

    A seam that every test replaces is a seam whose shipped value nothing
    checks -- and here the shipped value is what puts a credential inside the
    tenant's own tree rather than somewhere a test invented. It is asserted by
    identity rather than by calling it, because calling it is what wrote into a
    live board's credential the first time (SYRD-238).
    """
    import inspect

    for function in (
        team_launcher.upstream_report_credential_path,
        team_launcher.upstream_report_board,
        team_launcher.refresh_upstream_report_credential,
        team_launcher.record_upstream_report_link,
        team_launcher._write_owner_private_file,
    ):
        default = inspect.signature(function).parameters["home_for_user"].default
        check(
            default is team_launcher.home_dir_for_user,
            f"{function.__name__} resolves a real home by default: {default!r}",
        )


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("upstream_report_credential_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(300)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"upstream_report_credential_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
