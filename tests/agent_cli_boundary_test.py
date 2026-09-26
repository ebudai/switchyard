#!/usr/bin/env python3
"""SYRD-295: the agent-CLI modules' boundary with the launcher they came out of.

Agent CLI discovery moved into `scripts/agent_cli_discovery.py`, and
host-wide promotion into `scripts/agent_cli_promotion.py`, unchanged. This
pins what makes that safe:

- Neither module imports the launcher at its top: the launcher imports them.
  Promotion stands on discovery, never the reverse.
- Every name callers reached as `team_launcher.<name>` is still there and is
  the very same object, whichever module is imported first.
- Launcher facilities the moved code uses are looked up on `team_launcher`
  when it runs, so a patch there reaches it: `PROC_ROOT` (patched by three
  suites), `FIRST_RUN_AUTH_STATUS_COMMANDS`, `switchyard_shared_install_root`
  and `_repo_root`.
- The vendor install table is read only in `team_launcher.py`.
  `team_launcher_missing_cli_install_hint_test` guards its readers by scanning
  that one file, so a reader anywhere else would escape that guard.

Nothing is installed, copied, promoted or run.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

#: Every moved name the launcher still exports, by the module that now holds it.
EXPORTED = {
    'scripts.agent_cli_discovery': (
        'AGENT_CLI_SCOPE_HOST_WIDE',
        'AGENT_CLI_SCOPE_CALLER_ONLY',
        'AGENT_CLI_SCOPE_ABSENT',
        'CALLER_LOCAL_BIN_SUBDIRS',
        'CALLER_LOCAL_BIN_GLOBS',
        'CALLER_PROCESS_TREE_HOPS',
        'InvokingAccount',
        'invoking_account',
        'account_can_execute',
        'caller_command_search_path',
        'caller_executable',
        'caller_aware_which',
        'AgentCliAvailability',
        'agent_cli_binary',
        'classify_agent_cli',
        'classify_selected_agent_clis',
        'agent_cli_scope_explanation',
        'AgentCliUnavailable',
    ),
    'scripts.agent_cli_promotion': (
        'AGENT_CLI_POLICY_REQUIRE_HOST_WIDE',
        'AGENT_CLI_POLICY_PROMOTE_LOCAL',
        'AGENT_CLI_POLICIES',
        '_parse_agent_cli_sources',
        'AGENT_CLI_PROMOTER_NAME',
        'AGENT_CLI_PROMOTION_LABEL',
        'agent_cli_promoter_path',
        'promote_agent_cli_through_sudo',
        'registered_tenant_agent_clis',
        'resolvable_agent_cli_promotions',
        'offer_host_wide_promotion_before_launch',
        'require_agent_clis_for_new_tenant',
        'OwnerCliVerification',
        'verify_agent_clis_for_owner',
        'AGENT_CLI_HOST_WIDE_BIN',
        'AgentCliSourceRejected',
        'AGENT_CLI_SCRIPT_SAMPLE_BYTES',
        'agent_cli_unreachable_dependencies',
        'agent_cli_detected_path_problems',
        'agent_cli_source_is_self_contained',
        'resolve_agent_cli_source',
        'promote_agent_cli_host_wide',
        'refresh_registered_agent_clis',
        '_configured_agent_clis',
    ),
}


def attempt(action):
    """Run one step and hand back what it returned or raised, so a check reports it."""
    try:
        return action(), None
    except BaseException as exc:  # noqa: BLE001 -- what escaped is the finding
        return None, exc


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_modules_import_without_the_launcher_and_in_one_direction() -> None:
    result = python(
        "import sys; import scripts.agent_cli_discovery; "
        "print('scripts.team_launcher' in sys.modules, 'scripts.agent_cli_promotion' in sys.modules); "
        "import scripts.agent_cli_promotion; print('scripts.team_launcher' in sys.modules)"
    )
    check(result.returncode == 0, f"both import on their own: {result.stderr[-600:]}")
    check(result.stdout.split() == ["False", "False", "False"],
          f"neither pulls the launcher in, and discovery does not pull promotion in: {result.stdout!r}")


def test_every_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.agent_cli_promotion", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.agent_cli_discovery", "scripts.agent_cli_promotion"),
                  ("scripts.agent_cli_discovery", "scripts.team_launcher")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t; "
            f"table = {EXPORTED!r}; "
            "print(all(getattr(t, n) is getattr(importlib.import_module(m), n) for m, ns in table.items() for n in ns))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_the_vendor_install_table_is_read_only_where_its_guard_looks() -> None:
    readers = sorted(
        path.relative_to(ROOT).as_posix() for path in (ROOT / "scripts").rglob("*.py")
        if "AGENT_CLI_INSTALL_COMMANDS" in path.read_text(encoding="utf-8")
    )
    check(readers == ["scripts/team_launcher.py"],
          f"only team_launcher.py, which the no-execution guard scans, names the install table: {readers}")


def test_discovery_reads_the_launchers_patched_proc_root_and_probe_table() -> None:
    _, escaped_import = attempt(lambda: __import__("scripts.agent_cli_discovery"))
    check(escaped_import is None, f"scripts.agent_cli_discovery imports in this process: {escaped_import!r}")
    from scripts import agent_cli_discovery, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd295-proc.") as raw:
        proc = Path(raw)
        me = proc / str(os.getpid())
        me.mkdir()
        (me / "status").write_text(f"Name:\tpython\nPPid:\t1\nUid:\t{os.getuid()}\t0\t0\t0\n", encoding="utf-8")
        (me / "environ").write_bytes(b"HOME=/x\0PATH=/caller/only/bin:/usr/bin\0")
        account = agent_cli_discovery.InvokingAccount(user="caller", uid=os.getuid(), gid=os.getgid(),
                                                      home=Path("/x"), source="test")
        saved = (team_launcher.PROC_ROOT, team_launcher.FIRST_RUN_AUTH_STATUS_COMMANDS)
        team_launcher.PROC_ROOT = proc
        team_launcher.FIRST_RUN_AUTH_STATUS_COMMANDS = {"somecli": ["some-binary", "auth", "status"]}
        try:
            path, escaped = attempt(lambda: agent_cli_discovery._caller_path_from_process_tree(account))
            binary, escaped_binary = attempt(lambda: agent_cli_discovery.agent_cli_binary("somecli"))
        finally:
            team_launcher.PROC_ROOT, team_launcher.FIRST_RUN_AUTH_STATUS_COMMANDS = saved
    check(path == "/caller/only/bin:/usr/bin",
          f"the caller's PATH was read from the launcher's patched PROC_ROOT: {path!r} {escaped!r}")
    check(binary == "some-binary",
          f"the binary came from the launcher's patched probe table: {binary!r} {escaped_binary!r}")


def test_promotion_finds_its_promoter_through_the_launcher() -> None:
    _, escaped_import = attempt(lambda: __import__("scripts.agent_cli_promotion"))
    check(escaped_import is None, f"scripts.agent_cli_promotion imports in this process: {escaped_import!r}")
    from scripts import agent_cli_promotion, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd295-promoter.") as raw:
        shared = Path(raw) / "opt"
        saved = (team_launcher.switchyard_shared_install_root, team_launcher._repo_root)
        team_launcher.switchyard_shared_install_root = lambda: shared
        team_launcher._repo_root = lambda: Path("/nowhere/checkout")
        try:
            fallback, escaped = attempt(agent_cli_promotion.agent_cli_promoter_path)
            installed = shared / "current" / "scripts" / agent_cli_promotion.AGENT_CLI_PROMOTER_NAME
            installed.parent.mkdir(parents=True)
            installed.write_text("#!/bin/sh\n", encoding="utf-8")
            chosen, escaped_chosen = attempt(agent_cli_promotion.agent_cli_promoter_path)
        finally:
            team_launcher.switchyard_shared_install_root, team_launcher._repo_root = saved
    check(fallback == Path("/nowhere/checkout/scripts") / agent_cli_promotion.AGENT_CLI_PROMOTER_NAME,
          f"with no installed release, the launcher's patched checkout supplies the promoter: {fallback} {escaped!r}")
    check(chosen == installed, f"and the launcher's patched shared install root wins when it has one: {chosen} {escaped_chosen!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"agent_cli_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
