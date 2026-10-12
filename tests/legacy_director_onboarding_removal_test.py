#!/usr/bin/env python3
"""SYRD-40: the legacy Director onboarding bridge is gone, and no tenant is stranded by it.

The phase-one bridge answered a director's fresh start with a packet lookup
when its tenant had no stored onboarding and no migration marker. Every
declarative tenant now carries the marker; the one that could not is the
bundled PGU, non-declarative by design, whose director relied on the bridge.
PGU's built-in launcher config now carries the canonical Director prompt in
its director's environment, so the readiness inventory verifies it, and the
real pane command delivers that prompt through the real hook. The hook's
role-specific branch, its helpers and the projected marker are removed.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import onboarding_readiness as readiness  # noqa: E402
from scripts import role_command, team_launcher  # noqa: E402

PGU = ROOT / "config" / "team-launcher" / "pgu.json"
HOOK = ROOT / "scripts" / "ticket-board-pane-idle-hook"
PROMPT_KEY = "TICKET_BOARD_ROLE_ONBOARDING_PROMPT"
REMOVED = (
    "_display_path",
    "_onboarding_packet_exists",
    "_candidate_project_roots",
    "_onboarding_project_root",
    "_director_startup_onboarding_context",
    "_director_onboarding_migrated",
    "DIRECTOR_ROLE",
    "ONBOARDING_PACKET_DIR",
    "DIRECTOR_ONBOARDING_GUIDE",
    "SWITCHYARD_PROJECT_DIR_NAME",
)
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def pgu_raw() -> dict:
    return json.loads(PGU.read_text(encoding="utf-8"))


def test_the_built_in_pgu_config_passes_the_readiness_inventory() -> None:
    result = readiness.assess_tenant("pgu", PGU)
    check(result.verified and result.director_role == "director" and result.migrated
          and "not declarative" in result.detail,
          f"PGU is ready through its launcher environment: {result}")
    with tempfile.TemporaryDirectory(prefix="syrd40-registry.") as raw:
        report = readiness.inventory(registry_dir=Path(raw), config_dir=PGU.parent)
    check([t.slug for t in report.tenants] == ["pgu"] and report.ready,
          f"the inventory over the release's own config directory is ready: {report.as_json()}")


def test_pgu_carries_the_canonical_prompt_and_stays_what_it_was() -> None:
    raw = pgu_raw()
    director = next(role for role in raw["roles"] if role["role"] == "director")
    expected = team_launcher.director_onboarding_seed_text(Path(raw["repository"]))
    check(director.get("env") == {PROMPT_KEY: expected},
          f"the director's prompt is the provisioning seed for PGU's own repository: {director.get('env')}")
    check(all(PROMPT_KEY not in (role.get("env") or {}) for role in raw["roles"] if role["role"] != "director"),
          "no other role is given the director's prompt")
    check("workflow" not in raw and raw["layout"] == "pgu-konsole-layout.json",
          "PGU stays non-declarative, on its own layout")
    check([(role["role"], role.get("slot"), role["target"]) for role in raw["roles"]] == [
        ("director", 2, "pgu-director:0.0"), ("main", 1, "pgu-main:0.0"), ("app", 4, "pgu-app:0.0"),
        ("research", None, "pgu-research:0.0"), ("ops", 5, "pgu-ops:0.0"), ("audit", 3, "pgu-audit:0.0"),
        ("inspector", 0, "pgu-inspector:0.0"),
    ], "its seven roles, slots and targets are unchanged")


def _split_env_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """`env -u K ... K=V ... cmd` -> (the env prefix, the command)."""
    check(argv[0] == "env", f"the pane command starts with its env prefix: {argv[:3]}")
    index = 1
    while index < len(argv):
        if argv[index] == "-u":
            index += 2
        elif "=" in argv[index] and not argv[index].startswith("/"):
            index += 1
        else:
            break
    return argv[:index], argv[index:]


def test_the_real_pane_command_delivers_the_prompt_through_the_real_hook() -> None:
    config = team_launcher.load_project_config("pgu", PGU)
    director = next(role for role in config.roles if role.role == "director")
    expected = team_launcher.director_onboarding_seed_text(Path(pgu_raw()["repository"]))
    with tempfile.TemporaryDirectory(prefix="syrd40-pane.") as raw:
        tmp = Path(raw)
        argv = role_command.cli_command_for_role(director, session_dir=tmp / "sessions", pane_state_dir=tmp / "state")
        prefix, command = _split_env_command(argv)
        check(f"{PROMPT_KEY}={expected}" in prefix and command[:1] == ["claude"],
              f"the director's pane is launched with the prompt in its environment: {command[:2]}")
        # The same prefix, with the board's addresses pointed at nothing, runs the hook
        # the CLI would run at a fresh start.
        sandboxed = [
            f"{item.split('=', 1)[0]}={tmp / 'no-board.sock'}" if item.startswith("TICKET_BOARD_SOCKET=")
            else "TICKET_BOARD_URL=http://127.0.0.1:9" if item.startswith("TICKET_BOARD_URL=")
            else item
            for item in prefix
        ]
        ran = subprocess.run(
            [*sandboxed, "HOME=" + str(tmp), sys.executable, "-B", str(HOOK), "idle", "--source", "claude.SessionStart",
             "--state-dir", str(tmp / "state"), "--session-dir", str(tmp / "sessions")],
            input=json.dumps({"source": "startup", "session_id": "syrd40-session"}),
            capture_output=True, text=True, timeout=60, cwd=tmp,
        )
        check(ran.returncode == 0, f"the hook ran: {ran.stderr}")
        output = json.loads(ran.stdout or "null")
        context = (output or {}).get("hookSpecificOutput", {}).get("additionalContext")
        check(context == f"Your director session started.\n\n{expected}",
              f"PGU's director hears the canonical prompt at a fresh start: {ran.stdout!r}")


def test_the_hook_carries_no_role_specific_onboarding() -> None:
    source = HOOK.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assigned = {target.id for node in tree.body if isinstance(node, ast.Assign)
                for target in node.targets if isinstance(target, ast.Name)}
    check(not (set(REMOVED) & (names | defined | assigned)),
          f"the bridge's helpers and constants are gone: {sorted(set(REMOVED) & (names | defined | assigned))}")
    start = next(node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "_session_start_additional_context_output")
    literals = {node.value for node in ast.walk(start) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    check("director" not in literals and "TICKET_BOARD_ROLE_ONBOARDING_MIGRATED" not in literals
          and "TICKET_BOARD_ROLE_ONBOARDING_MIGRATED" not in source,
          "the session-start answer names no role and reads no migration marker")


def test_an_unmigrated_director_with_a_packet_everywhere_hears_nothing() -> None:
    """What the bridge used to answer: no prompt, no path, no marker, a packet in every root."""
    with tempfile.TemporaryDirectory(prefix="syrd40-packet.") as raw:
        root = Path(raw)
        (root / "docs" / "onboarding").mkdir(parents=True)
        (root / "docs" / "onboarding" / "switchyard-director-guide.md").write_text("guide\n", encoding="utf-8")
        (root / ".switchyard").mkdir()
        env = {
            "PATH": "/usr/bin:/bin", "HOME": str(root), "TICKET_BOARD_PROJECT": "porter",
            "TICKET_BOARD_PANE_TARGET": "porter-director:0.0", "TICKET_BOARD_URL": "http://127.0.0.1:9",
            "SWITCHYARD_PROJECT_DIR": str(root), "TICKET_BOARD_PROJECT_DIR": str(root),
            "PGU_TICKET_BOARD_PROJECT_DIR": str(root),
            "SWITCHYARD_DIRECTOR_ONBOARDING": str(root / ".switchyard" / "DIRECTOR_ONBOARDING.md"),
        }
        ran = subprocess.run(
            [sys.executable, "-B", str(HOOK), "idle", "--source", "claude.SessionStart",
             "--state-dir", str(root / "state"), "--session-dir", str(root / "sessions")],
            input=json.dumps({"source": "startup", "session_id": "syrd40-bare"}),
            capture_output=True, text=True, timeout=60, cwd=root, env=env,
        )
    check(ran.returncode == 0 and ran.stdout.strip() == "",
          f"no packet lookup answers for the director any more: {ran.stdout!r} {ran.stderr!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"legacy_director_onboarding_removal_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
