#!/usr/bin/env python3
"""SYRD-548: connecting a tenant for reports is one narrow step, not an upgrade.

Otto had two Switchyard bugs ready and could not file them: `switchyard new`
never offered the report URL, and the documented remedy was a full `upgrade`,
which would have run the identities phase its Director had chosen not to run --
and whose release phase otto's dry run could not even complete.

`switchyard upgrade <project> --only upstream-report` writes the report link in
the tenant's configuration (which is what puts the report variables in every
pane) and the report-only credential, 0600 and the tenant's own -- and nothing
else: every upgrade phase is fenced to fail here, and the whole sandbox tree is
compared before and after. `switchyard new --upstream-report-url` connects a
project before its first launch. A pane with no credential is told the narrow
step, never an upgrade.

Every home is the sandbox's (SYRD-238 wrote test tokens into a live board's
credential once); the owner is the account running the suite.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import team_launcher as launcher  # noqa: E402
from scripts import upstream_report  # noqa: E402
from upstream_report_credential_test import UPSTREAM, _host, _tenant  # noqa: E402

CHECKS = 0
TOKEN = "report-token-one"
PHASES = ("_pin_upgrade_source", "_recover_upgrade_state", "_refresh_upgrade_artifacts", "_stage_upgrade_tooling",
          "_upgrade_identities_and_accounts", "_finish_upgrade")


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tree(root: Path) -> dict[str, tuple]:
    """Every path under the sandbox: its kind, mode, owner and content digest."""
    seen: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else ""
        seen[str(path.relative_to(root))] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_uid, digest)
    return seen


@contextlib.contextmanager
def host(tmp: Path, *, token: str = TOKEN):
    """A registry holding the upstream board and the tenant, a sandbox home, and every phase fenced."""
    registry, board_env, owner, resolver = _host(tmp)
    board_env.parent.mkdir(parents=True, exist_ok=True)
    # The board's own environment; with no report token it still holds the write token.
    board_env.write_text((f"TICKET_BOARD_TENANT_REPORT_TOKEN={token}\n" if token else "")
                         + "TICKET_BOARD_WRITE_TOKEN=never-copied\n", encoding="utf-8")
    config, path = _tenant(tmp)
    (registry / "mefp.json").write_text(json.dumps({
        "schema": "switchyard.project-registry.v1", "slug": "mefp", "name": "MEFP", "config_path": str(path),
        "owner_user": owner}), encoding="utf-8")

    def fenced(name):
        def phase(*_args, **_kwargs):
            raise AssertionError(f"the upgrade phase {name} ran")
        return phase

    defaults = upstream_report.connect_upstream_report.__kwdefaults__
    previous = defaults["home_for_user"]
    defaults["home_for_user"] = resolver
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(launcher, "switchyard_registry_dir", lambda: registry))
            for name in PHASES:
                stack.enter_context(patch.object(launcher, name, fenced(name)))
            yield SimpleNamespace(registry=registry, board_env=board_env, config_path=path, resolver=resolver,
                                  credential=resolver(owner) / ".config" / "mefp" / "upstream-report.env")
    finally:
        defaults["home_for_user"] = previous


def upgrade_only(*extra: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            status = launcher.switchyard_main(["upgrade", "mefp", "--only", "upstream-report", *extra])
        except SystemExit as exc:  # argparse's refusal is an answer
            status = f"exit {exc.code}"
    return status, out.getvalue()


# ------------------------------------------------------------------ the narrow step


def test_the_narrow_step_writes_the_link_and_the_credential_and_nothing_else() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd548-connect.") as raw:
        tmp = Path(raw)
        with host(tmp) as h:
            before_config = json.loads(h.config_path.read_text())
            before = tree(tmp)
            status, said = upgrade_only("--upstream-report-url", UPSTREAM)
            check(status == 0 and "no upgrade phase ran" in said, said)
            after = tree(tmp)
            changed = sorted(k for k in after if before.get(k) != after[k])
            rel_config = str(h.config_path.relative_to(tmp))
            rel_credential = str(h.credential.relative_to(tmp))
            # The credential's directory is created on the way, and the config replaced in place.
            created_dirs = sorted(k for k in changed if after[k][0] == stat.S_IFDIR and k not in before)
            check(set(changed) - set(created_dirs) == {rel_config, rel_credential},
                  f"only the configuration and the credential changed: {changed}")
            check(all(rel_credential.startswith(d + "/") for d in created_dirs), created_dirs)
            check(not [k for k in before if k not in after], "nothing was removed")
            after_config = json.loads(h.config_path.read_text())
            check({k for k in set(before_config) | set(after_config) if before_config.get(k) != after_config.get(k)}
                  == {"upstream_report_url", "upstream_report_token_file"}, after_config)
            info = h.credential.lstat()
            check(stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.getuid(), oct(info.st_mode))
            check(h.credential.read_text() == f"TICKET_BOARD_TENANT_REPORT_TOKEN={TOKEN}\n",
                  "only the report token, never the board's write token")
            env = launcher.load_project_config("mefp", h.config_path).roles[0].env
            check((env.get("TICKET_BOARD_REPORT_URL"), env.get("TICKET_BOARD_REPORT_ORIGIN_PROJECT"),
                   env.get("TICKET_BOARD_TENANT_REPORT_TOKEN_FILE")) == (UPSTREAM, "mefp", str(h.credential)), env)

            again = tree(tmp)
            status, said = upgrade_only("--upstream-report-url", UPSTREAM)
            check(status == 0 and tree(tmp) == again, f"run twice, it writes once: {said}")

            # A rotated board token is fetched again without naming the URL a second time.
            h.board_env.write_text("TICKET_BOARD_TENANT_REPORT_TOKEN=report-token-two\n", encoding="utf-8")
            config_bytes = h.config_path.read_bytes()
            status, said = upgrade_only()
            check(status == 0 and h.credential.read_text() == "TICKET_BOARD_TENANT_REPORT_TOKEN=report-token-two\n",
                  said)
            check(h.config_path.read_bytes() == config_bytes, "the recorded link is reused, not rewritten")


def test_a_refusal_writes_nothing() -> None:
    cases = (
        (dict(), ("--upstream-report-url", "http://127.0.0.1:1"), "no project registered on this host serves"),
        (dict(token=""), ("--upstream-report-url", UPSTREAM), "has no TICKET_BOARD_TENANT_REPORT_TOKEN"),
        (dict(), (), "records no upstream report board"),
    )
    for host_kwargs, argv, expected in cases:
        with tempfile.TemporaryDirectory(prefix="syrd548-refuse.") as raw:
            tmp = Path(raw)
            with host(tmp, **host_kwargs):
                before = tree(tmp)
                status, said = upgrade_only(*argv)
                check(status == 1 and expected in said and "no upgrade phase ran" in said, (argv, said))
                check(tree(tmp) == before, f"refused before anything was written: {argv}")


def test_a_dry_run_says_what_it_would_write() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd548-dry.") as raw:
        tmp = Path(raw)
        with host(tmp):
            before = tree(tmp)
            status, said = upgrade_only("--upstream-report-url", UPSTREAM, "--dry-run")
            check(status == 0 and tree(tmp) == before, said)
            check("would record mefp's upstream report board" in said and "would give mefp" in said, said)


def test_only_takes_the_one_step_it_names() -> None:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            launcher._build_switchyard_upgrade_parser().parse_args(["mefp", "--only", "identities"])
            status = 0
        except SystemExit as exc:
            status = exc.code
    check(status == 2 and "invalid choice" in err.getvalue(), err.getvalue())
    args = launcher._build_switchyard_upgrade_parser().parse_args(["mefp"])
    check(args.only == "", "without --only, upgrade is the upgrade")


# ------------------------------------------------------------------ switchyard new


def test_new_passes_the_report_url_through() -> None:
    seen: dict = {}
    original = launcher.switchyard_new_command
    launcher.switchyard_new_command = lambda **kwargs: seen.update(kwargs) or 0
    try:
        try:
            status = launcher.switchyard_main(["new", "--slug", "mefp", "--upstream-report-url", UPSTREAM,
                                               "--upstream-report-token-file", "/x/report.env"])
        except SystemExit as exc:
            status = f"parser refused: exit {exc.code}"
    finally:
        launcher.switchyard_new_command = original
    check(status == 0 and (seen.get("upstream_report_url"), seen.get("upstream_report_token_file"))
          == (UPSTREAM, "/x/report.env"), seen)


def new_until_sign_in(h, tmp: Path, **kwargs) -> tuple[object, list[str]]:
    """switchyard_new_command with its provisioning phases stood in, stopped at sign-in.

    The project's board is provisioned (the fixture tenant); what reaches sign-in
    -- and so the first launch -- is what this asks about.
    """
    reached: dict = {}
    said: list[str] = []
    choices = SimpleNamespace(**{name: None for name in (
        "agy_source_origin", "artifact_path", "design_document", "director_onboarding", "first_run_runner",
        "include_audit", "include_designer", "owner_shell", "project_dir",
        "resolved_agy_credential_source", "resolved_project_name", "runner", "selected_audit_roles",
        "selected_desktop_policy", "selected_implementer_roles", "selected_role_clis", "selected_role_efforts",
        "selected_role_models")}, resolved_slug="mefp", owner_user="mefp-agent", stages=SimpleNamespace(begin=lambda _name: None))
    preflight = SimpleNamespace(effective_source_repo=None, precheck_plan=None, selected_role_clis=None,
                                worktree_branch=None)

    def sign_in(**sign_in_kwargs):
        reached["config"] = sign_in_kwargs["config"]
        return 7

    # P3 runs for real; only what it calls to provision the board stands in, and
    # its configuration is the fixture tenant's, where P3 reads it.
    with patch.object(launcher, "_resolve_new_project_choices", lambda **_k: choices), \
            patch.object(launcher, "_check_new_project_preflight", lambda **_k: preflight), \
            patch.object(launcher, "_prepare_new_project_accounts",
                         lambda **_k: SimpleNamespace(provision_dir=h.config_path.parent)), \
            patch.object(launcher, "new_project_command", lambda *_a, **_k: 0), \
            patch.object(launcher, "_commit_project_git_changes", lambda **_k: None), \
            patch.object(launcher, "prepare_project_desktop", lambda config, **_k: config), \
            patch.object(launcher, "_register_switchyard_project", lambda *_a, **_k: None), \
            patch.object(launcher, "_prepare_first_run_auth_worktrees", lambda *_a, **_k: None), \
            patch.object(launcher, "_run_new_project_sign_in", sign_in):
        status = launcher.switchyard_new_command(registry_dir=h.registry, print_func=said.append, **kwargs)
    check(status == 7, f"it went on to sign-in: {status} {said}")
    return reached["config"], said


def test_new_connects_before_the_first_launch() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd548-new.") as raw:
        tmp = Path(raw)
        with host(tmp) as h:
            config, said = new_until_sign_in(h, tmp, upstream_report_url=UPSTREAM)
            # What sign-in and the first launch are handed, each role's pane environment included.
            check(config.upstream_report_url == UPSTREAM, said)
            check(all(role.env.get("TICKET_BOARD_REPORT_URL") == UPSTREAM
                      and role.env.get("TICKET_BOARD_TENANT_REPORT_TOKEN_FILE") == str(h.credential)
                      for role in config.roles), [role.env.get("TICKET_BOARD_REPORT_URL") for role in config.roles])
            check(launcher.load_project_config("mefp", h.config_path).roles[0].env.get("TICKET_BOARD_REPORT_URL")
                  == UPSTREAM, "the configuration the panes launch from carries it")
            check(h.credential.read_text() == f"TICKET_BOARD_TENANT_REPORT_TOKEN={TOKEN}\n", said)

        with host(Path(tempfile.mkdtemp(dir=raw))) as h:
            config, said = new_until_sign_in(h, tmp, upstream_report_url="http://127.0.0.1:1")
            check(not config.upstream_report_url, "a failed connection does not undo the project")
            named = next((line for line in said if "is not connected for reports" in line and "`" in line), "")
            check(named, f"the narrow step is named: {said[-3:]}")
            remedy = named.split("`")[1]
            argv = shlex.split(remedy)
            check(argv[:3] == ["sudo", "switchyard", "upgrade"], remedy)
            args = launcher._build_switchyard_upgrade_parser().parse_args(argv[3:])
            check((args.project, args.only, args.upstream_report_url) == ("mefp", "upstream-report",
                                                                          "http://127.0.0.1:1"), remedy)

        with host(Path(tempfile.mkdtemp(dir=raw))) as h:
            before = tree(Path(h.config_path).parent)
            config, _said = new_until_sign_in(h, tmp)
            check(not config.upstream_report_url and tree(Path(h.config_path).parent) == before,
                  "without the flag, new connects nothing")


# ------------------------------------------------------------------ the pane's refusal


def test_a_pane_without_a_credential_is_told_the_narrow_step() -> None:
    from scripts.ticket_board import write_cli, write_client

    env = {"TICKET_BOARD_URL": "http://127.0.0.1:9", "TICKET_BOARD_PROJECT": "otto",
           "TICKET_BOARD_REPORT_ORIGIN_PROJECT": "otto"}
    out, err = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, env, clear=False):
        for name in ("TICKET_BOARD_TENANT_REPORT_TOKEN", "TICKET_BOARD_REPORT_TOKEN",
                     "TICKET_BOARD_TENANT_REPORT_TOKEN_FILE", "TICKET_BOARD_REPORT_TOKEN_FILE",
                     "TICKET_BOARD_REPORT_URL", "PGU_TICKET_BOARD_REPORT_URL"):
            os.environ.pop(name, None)
        importlib.reload(write_client)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                status = write_cli.main(["file-report", "--title", "a Switchyard bug", "--body", "details"])
            except Exception as exc:  # noqa: BLE001 - a crash is an answer, and the wrong one
                status = f"raised {exc!r}"
    importlib.reload(write_client)
    message = err.getvalue()
    check(status == 1 and "Nothing was filed" in message and "otto is not connected" in message, message)
    remedy = message.split("`")[1]
    argv = shlex.split(remedy)
    check(argv[:3] == ["sudo", "switchyard", "upgrade"], remedy)
    args = launcher._build_switchyard_upgrade_parser().parse_args(argv[3:])
    check((args.project, args.only) == ("otto", "upstream-report") and args.upstream_report_url, remedy)
    check("--only" in argv, "never a full upgrade")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"upstream_report_connect_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
