#!/usr/bin/env python3
"""SYRD-283: a Director's own upgrade dry run, and the root check it cannot make.

MEFP's Director ran `switchyard upgrade mefp --deploy-ref <sha> --dry-run` as the
project account. An unprivileged upgrade is supported -- it prepares the
privileged rerun -- but the display-bridge check read the tenant-control sudoers
rule, which is root's 0440 file, and reported "cannot be read" as

    mefp's presentation window would open with every tab refused

That is a finding it never made. It now says the rule is unverified from here,
still judges the grant it can read, does not declare the tenant ready, and
names the boundary command that makes the same check as root.

That command is new: `preview-upgrade` (and its applying twin,
`upgrade-tenant-release`), catalogued like `deploy-release` -- a project slug and
a full commit, nothing else, run by the root-owned helper only for the
registered control pane. Proved here through the helper's REAL caller check,
against this test's own live process, for the Director and for a same-uid
sibling.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import privileged_helper_boundary_test as boundary  # noqa: E402
from scripts import team_launcher as tl  # noqa: E402
from scripts.ticket_board import polkit_preflight  # noqa: E402
from scripts.ticket_board import privileged_actions as pa  # noqa: E402
from scripts.ticket_board import privileged_front_door as door  # noqa: E402
from scripts.ticket_board import privileged_helper as ph  # noqa: E402
from scripts.ticket_board import privileged_operations as po  # noqa: E402
from scripts.ticket_board import tenant_upgrade_rule  # noqa: E402
from scripts.ticket_board.peer_identity import read_process  # noqa: E402
from scripts.ticket_board.project_provision import (  # noqa: E402
    TENANT_CONTROL_GRANT_NAME,
    tenant_control_grant_document,
    tenant_control_sudoers_document,
)

CHECKS = 0
COMMIT = "5f55e16be53a7a1a097ea4e9083009d905bbd996"
PROJECT = "mefp"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


# -- the false refusal -------------------------------------------------------


def test_a_root_only_rule_is_unverified_to_a_non_root_reader_not_refused() -> None:
    if os.geteuid() == 0:
        print("director_upgrade_boundary_test: running as root; the unprivileged case is not observable")
        return
    # The real thing first: /etc/sudoers is root's 0440 file, as the tenant rule is.
    verdict, detail = tl.sudoers_rule_state(Path("/etc/sudoers"), expected="x")
    check(verdict == "unverified", f"root's own rule, read by a tenant: {verdict} {detail}")
    check("only be read by root" in detail, detail)


def bridge(tmp: Path, *, rule_mode: int | None, rule_body: str | None = None):
    """A tenant whose window crosses to the desktop account, and its root files, sandboxed."""
    config_path = tmp / f"{PROJECT}.json"
    config_path.write_text(json.dumps({
        "project": PROJECT, "board_url": "http://127.0.0.1:1/", "run_as_user": "stellaris-agent",
        "roles": [{"role": "director", "cli": ["codex"], "slot": 0, "workdir": str(tmp / "d")}],
    }), encoding="utf-8")
    config = tl.load_project_config(PROJECT, config_path)
    grant_root = tmp / "grants"
    (grant_root / PROJECT).mkdir(parents=True)
    grant = grant_root / PROJECT / TENANT_CONTROL_GRANT_NAME
    grant.write_text(tenant_control_grant_document(
        project=PROJECT, owner_user="stellaris-agent", control_user="eric") + "\n", encoding="utf-8")
    grant.chmod(0o644)
    sudoers = tmp / "sudoers"
    sudoers.mkdir()
    if rule_mode is not None:
        rule = sudoers / f"49-{PROJECT}-tenant-control"
        rule.write_text(rule_body if rule_body is not None
                        else tenant_control_sudoers_document(PROJECT, "eric") + "\n", encoding="utf-8")
        rule.chmod(rule_mode)
    return config, {"grant_root": grant_root, "sudoers_dir": sudoers, "grant_owner_uid": os.getuid(),
                    "grant_owner_gid": os.getgid()}


def test_the_upgrade_says_unverified_and_names_the_root_check() -> None:
    if os.geteuid() == 0:
        return
    with tempfile.TemporaryDirectory(prefix="syrd283-unverified.") as raw:
        tmp = Path(raw)
        # Mode 0: this account cannot read it, exactly as the tenant cannot
        # read root's 0440 rule. The grant beside it is right.
        config, where = bridge(tmp, rule_mode=0)
        said: list[str] = []
        root_check = tl._privileged_upgrade_check_command(PROJECT, COMMIT)
        # The tenant's own dry run: a note, not a NOT READY verdict, naming what makes the check (SYRD-825).
        ok = tl.ensure_display_bridge(config, gui_user="eric", dry_run=True, print_func=said.append,
                                      root_check=root_check, **where)
        text = "\n".join(said)
        upgrade = f"switchyard privileged-action {PROJECT} upgrade-tenant-release commit={COMMIT}"
        check(ok is True and text.startswith("switchyard: note: "),
              f"a dry run does not hold a rule only root can read against readiness: {text}")
        check("every tab refused" not in text and "does not declare the tenant ready" not in text
              and "Its grant is correct" in text and "only be read by root" in text,
              f"and says why it could not check it, without a refusal: {text}")
        check(f"`{upgrade}`" in text and f"`{root_check}`" in text,
              f"it names the upgrade that checks it as root, and the read-only root check: {text}")
        # A real run is another thing: it would have to install the rule, and cannot read it.
        said.clear()
        ran: list[object] = []
        ok = tl.ensure_display_bridge(config, gui_user="eric", dry_run=False, print_func=said.append,
                                      runner=lambda *a, **k: ran.append(a), root_check=root_check, **where)
        text = "\n".join(said)
        check(ok is False and ran == [], f"an unverified bridge stops a real run, before running anything: {text}")
        check("could not be verified from here" in text and root_check in text and "Nothing was changed" in text,
              f"and names the root check: {text}")
        (tmp / "sudoers" / f"49-{PROJECT}-tenant-control").chmod(0o600)


def test_a_rule_that_is_actually_wrong_is_still_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd283-wrong.") as raw:
        config, where = bridge(Path(raw), rule_mode=0o664)
        said: list[str] = []
        ok = tl.ensure_display_bridge(config, gui_user="eric", dry_run=True, print_func=said.append, **where)
        check(ok is False and "every tab refused" in "\n".join(said),
              f"a group-writable rule is a real refusal: {said}")


def test_the_named_root_check_parses_and_validates_as_it_is_printed() -> None:
    printed = tl._privileged_upgrade_check_command(PROJECT, COMMIT)
    argv = shlex.split(printed)
    check(argv[:2] == ["switchyard", "privileged-action"], printed)
    args = tl._build_switchyard_privileged_action_parser().parse_args(argv[2:])
    values = dict(item.split("=", 1) for item in args.values)
    action = pa.action_for(args.action)
    validated = action.validate({"project": args.project, **values})
    check(po.command_for(action.name, validated)[-1] == "--dry-run", f"and it is the dry run: {validated}")
    check("<release commit>" in tl._privileged_upgrade_check_command(PROJECT, "origin/main"),
          "a ref that is not a full commit is not passed off as one")


# -- the two catalogued actions ----------------------------------------------


def test_the_actions_run_the_pinned_upgrade_and_its_dry_run() -> None:
    values = {"project": PROJECT, "commit": COMMIT}
    check(po.command_for("preview-upgrade", pa.action_for("preview-upgrade").validate(values))
          == [po.LAUNCHER, "upgrade", PROJECT, "--deploy-ref", COMMIT, "--dry-run"],
          "preview is the pinned dry run, always")
    check(po.command_for("upgrade-tenant-release", pa.action_for("upgrade-tenant-release").validate(values))
          == [po.LAUNCHER, "upgrade", PROJECT, "--deploy-ref", COMMIT],
          "and its twin is the same upgrade, applied")
    for name in ("preview-upgrade", "upgrade-tenant-release"):
        action = pa.action_for(name)
        check(action.authentication == pa.ALLOW_ACTIVE, f"{name} is the control pane's, unprompted")
        for bad in ({"project": PROJECT, "commit": "5f55e16"},
                    {"project": PROJECT, "commit": COMMIT, "dry_run": "0"},
                    {"project": "../mefp", "commit": COMMIT}):
            try:
                action.validate(bad)
                check(False, f"{name} accepted {bad}")
            except pa.ArgumentError:
                check(True, "refused")


def run_helper(action: str, registered_pid: int, *, values: dict | None = None,
               caller=None) -> tuple[int, list, str]:
    """ph.main with its REAL caller check, against this test's own live process."""
    me = read_process(os.getpid())
    listing = boundary._registered(registered_pid, me.start_time if registered_pid == os.getpid() else 7,
                                   os.getuid())
    board_get, _asked = boundary._board(boundary.WORKFLOW, listing)
    ran: list = []

    def runner(command, *, timeout):
        ran.append(list(command))
        return boundary.Completed(0)

    buffer = io.StringIO()
    with boundary.swapped(
        ph,
        privileged_install=boundary._StubInstall([]),
        caller_identity=caller or (lambda *a, **k: ph.CallerIdentity(os.getpid(), me.start_time, os.getuid())),
        board_url_for=lambda project, **k: "http://127.0.0.1:1/",
        _board_get=board_get,
        # The board is the test's own: proving which board root's unit names is board_identity's suite, and here
        # it read this host's live mefp unit and process (SYRD-694, SYRD-825).
        authorizing_board=lambda action, project, **k: (board_get, "http://127.0.0.1:1/"),
        _attempt_factory=lambda project, command, **kw: boundary.FakeAttempt(project, command, log=[], **kw),
        _run_command=runner,
    ):
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            values = {"project": PROJECT, "commit": COMMIT} if values is None else values
            code = ph.main([action, *(f"{key}={value}" for key, value in values.items())])
    return code, ran, buffer.getvalue()


def test_the_registered_director_runs_the_preview_and_a_sibling_cannot() -> None:
    code, ran, said = run_helper("preview-upgrade", registered_pid=os.getpid())
    check(code == 0, f"the registered control pane is allowed: {said}")
    check(ran == [[po.LAUNCHER, "upgrade", PROJECT, "--deploy-ref", COMMIT, "--dry-run"]],
          f"and exactly the pinned dry run ran: {ran}")
    for action in ("preview-upgrade", "upgrade-tenant-release"):
        code, ran, said = run_helper(action, registered_pid=os.getpid() + 100000)
        check(code == 1 and not ran, f"{action}: a same-uid process that is not the Director runs nothing: {said}")
        check("registered process" in said, said)


def _detached_proc(root: Path) -> Path:
    """A /proc of one shell run from a service, with no tmux pane anywhere above it."""
    for pid, ppid, comm in ((4100, 4000, "sh"), (4000, 1, "systemd")):
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} " + "0 " * 17 + "4242\n", encoding="utf-8")
    return root


def test_the_tenant_upgrade_rule_widens_who_reaches_the_helper_never_who_it_acts_for() -> None:
    """SYRD-825: the rule answers YES on the owner account, the program and the action alone, so any process of
    that account reaches the helper without a prompt. The helper's own caller check is what decides."""
    body = tenant_upgrade_rule.render(PROJECT, "stellaris-agent").split("polkit.addRule", 1)[1]
    check(body.count("polkit.Result.YES") == 1 and "subject.active" not in body and "subject.local" not in body,
          f"the rule asks nothing about the session or the pane: {body}")
    real_caller = ph.caller_identity  # the helper's own, before run_helper stands the caller in
    for action in tenant_upgrade_rule.TENANT_UPGRADE_ACTIONS:
        values = {"project": PROJECT} if action == "upgrade-tenant" else {"project": PROJECT, "commit": COMMIT}
        code, ran, said = run_helper(action, registered_pid=os.getpid() + 100000, values=values)
        check(code == 1 and not ran and "registered process" in said,
              f"{action}: another pane of the owner account runs nothing, and is told why: {said}")
        with tempfile.TemporaryDirectory(prefix="syrd825-proc.") as raw:
            proc = _detached_proc(Path(raw))
            code, ran, said = run_helper(action, registered_pid=os.getpid(), values=values,
                                         caller=lambda *a, **k: real_caller(4100, proc_root=proc, uid=os.getuid()))
        check(code == 1 and not ran and "cannot establish which pane" in said,
              f"{action}: a detached shell of the owner account runs nothing, and is told why: {said}")


# -- a host whose installed boundary predates the actions --------------------


def test_a_stale_installed_policy_is_named_with_the_way_to_install_the_action() -> None:
    def plan(policy_text: str):
        return door.plan_action(
            PROJECT, "preview-upgrade", {"commit": COMMIT},
            verify=lambda *a, **k: [],
            read_policy=lambda _path: policy_text,
            identity=lambda: ph.CallerIdentity(1, 2, 3),
            decide=lambda action_id, **k: polkit_preflight.PolkitDecision(
                polkit_preflight.AUTHORIZED, action_id),
        )

    stale = plan('<action id="org.switchyard.privileged.upgrade-tenant">')
    joined = " ".join(stale.problems)
    check("predates preview-upgrade" in joined, f"the missing action is named: {stale.problems}")
    check(f"switchyard privileged-action {PROJECT} upgrade-tenant" in joined and "install-shared-release" in joined,
          f"with the chain that installs it: {joined}")
    check(not stale.ready, "and nothing is asked of polkit for an action it cannot know")
    current = plan('<action id="org.switchyard.privileged.preview-upgrade">')
    check(not any("predates" in p for p in current.problems), f"a current policy is fine: {current.problems}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"director_upgrade_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
