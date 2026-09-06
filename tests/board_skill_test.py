#!/usr/bin/env python3
"""Regression coverage for the portable Switchyard board skill."""

from __future__ import annotations

import importlib.machinery
import json
import os
import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from standalone_test_runner import run_module_tests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import board_skill  # noqa: E402
from scripts.ticket_board import notify_listener  # noqa: E402

INSTALLER = ROOT / "scripts" / "switchyard-board-skill"
PUBLIC_SWITCHYARD = ROOT / "scripts" / "switchyard"
CANONICAL = ROOT / board_skill.CANONICAL_RELATIVE_SOURCE
PANE_HOOK = ROOT / "scripts" / "ticket-board-pane-idle-hook"


def _load_pane_hook():
    loader = importlib.machinery.SourceFileLoader("syrd35_pane_hook", str(PANE_HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _run_installer(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(INSTALLER), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "a SKILL.md must open with YAML frontmatter"
    body = text.split("---\n", 2)[1]
    fields: dict[str, str] = {}
    for line in body.splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def test_canonical_skill_is_the_only_body_and_names_its_own_directory() -> None:
    assert CANONICAL.is_file()
    fields = _frontmatter(CANONICAL.read_text(encoding="utf-8"))
    assert fields["name"] == board_skill.SKILL_NAME == CANONICAL.parent.name
    assert fields["description"]
    others = [
        path
        for path in ROOT.rglob("SKILL.md")
        if ".git" not in path.parts and path != CANONICAL
    ]
    assert others == [], f"a second skill body would drift from the canonical one: {others}"


def test_canonical_skill_hardcodes_no_tenant_detail() -> None:
    text = CANONICAL.read_text(encoding="utf-8")
    forbidden = {
        "project prefix": r"\bPGU-\d|\bSYRD-\d|\bpgu\b",
        "port or host": r"127\.0\.0\.1|localhost|:\d{4,5}\b",
        "home path": r"/home/|~/\.",
        "database": r"postgresql://|ticket_board_service",
        "named stage sequence": r"in_progress|director_review|user_review\b",
        "obsolete command": r"eric-sign-off|eric-reopen|needs-eric-signoff",
    }
    found = {label: re.findall(pattern, text) for label, pattern in forbidden.items()}
    assert not any(found.values()), f"skill hardcodes tenant detail: { {k: v for k, v in found.items() if v} }"
    # It has to teach discovery instead.
    for variable in (
        "TICKET_BOARD_URL",
        "TICKET_BOARD_SOCKET",
        "TICKET_BOARD_CALLER_ROLE",
        "TICKET_BOARD_TICKET_PREFIX",
    ):
        assert variable in text
    assert "workflow_actions" in text


def test_every_supported_cli_has_a_skill_adapter() -> None:
    from scripts import team_launcher

    covered = {runtime for runtime, _root in board_skill.SKILL_ADAPTERS}
    assert covered == set(team_launcher.SUPPORTED_CONFIG_CLI_NAMES)
    assert covered == set(team_launcher.SUPPORTED_NEW_PROJECT_CLIS)


def test_adapter_paths_are_the_runtime_discovery_locations() -> None:
    home = Path("/nowhere")
    assert dict(board_skill.adapter_paths(home)) == {
        "claude": home / ".claude" / "skills" / "switchyard-board" / "SKILL.md",
        "codex": home / ".codex" / "skills" / "switchyard-board" / "SKILL.md",
        "agy": home / ".gemini" / "config" / "skills" / "switchyard-board" / "SKILL.md",
        "hermes": home / ".hermes" / "skills" / "switchyard-board" / "SKILL.md",
    }


def test_provenance_footer_never_displaces_the_frontmatter() -> None:
    stamped = board_skill.stamp_skill(CANONICAL.read_text(encoding="utf-8"), source_commit="deadbeef")
    assert stamped.startswith("---\n")
    assert board_skill.parse_provenance(stamped) == ("deadbeef", str(board_skill.CANONICAL_RELATIVE_SOURCE))
    assert board_skill.parse_provenance(CANONICAL.read_text(encoding="utf-8")) is None
    # Stamping is deterministic, so an unchanged source reinstalls as a no-op.
    assert board_skill.stamp_skill(stamped.removesuffix(stamped[stamped.index("\n<!-- Switchyard"):]),
                                   source_commit="deadbeef") == stamped


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid"},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _skill_checkout(root: Path, body: str) -> Path:
    source = root / board_skill.CANONICAL_RELATIVE_SOURCE
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(body, encoding="utf-8")
    return source


def test_recovery_runs_through_the_public_switchyard_command() -> None:
    """The documented recovery path must not depend on a pane's own PATH.

    Only `switchyard` is installed host-wide; `switchyard-board-skill` lives in
    the deployed tree's scripts directory, which a pane happens to have on PATH
    and an operator's shell does not.
    """
    from scripts import team_launcher

    assert "board-skill" in team_launcher.SWITCHYARD_COMMANDS
    assert not team_launcher.switchyard_invocation_requires_root(["board-skill", "verify"])
    assert "board-skill" not in team_launcher.SWITCHYARD_PRIVILEGED_COMMANDS

    with tempfile.TemporaryDirectory(prefix="switchyard-board-skill-public.") as tmp:
        home = Path(tmp)
        public = subprocess.run(
            [sys.executable, str(PUBLIC_SWITCHYARD), "board-skill", "install", "--home", str(home)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert public.returncode == 0, public.stderr
        assert public.stdout.count(": installed ") == 4
        verified = subprocess.run(
            [sys.executable, str(PUBLIC_SWITCHYARD), "board-skill", "verify", "--home", str(home)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert verified.returncode == 0, verified.stderr
        assert verified.stdout.count(": present ") == 4


def test_a_checkout_only_claims_a_commit_that_carries_this_body() -> None:
    """The provenance footer must never name a release that lacks the skill."""
    with tempfile.TemporaryDirectory(prefix="switchyard-board-skill-provenance.") as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        (repo / "unrelated.txt").write_text("seed\n", encoding="utf-8")
        _git(repo, "add", "unrelated.txt")
        _git(repo, "commit", "-q", "-m", "before the skill existed")
        before = _git(repo, "rev-parse", "HEAD")

        # Written but not committed: HEAD does not carry it.
        source = _skill_checkout(repo, "---\nname: switchyard-board\n---\n\nBody.\n")
        assert board_skill.resolve_source_commit(source) == board_skill.UNKNOWN_COMMIT

        _git(repo, "add", str(board_skill.CANONICAL_RELATIVE_SOURCE))
        _git(repo, "commit", "-q", "-m", "add the skill")
        carrying = _git(repo, "rev-parse", "HEAD")
        assert carrying != before
        assert board_skill.resolve_source_commit(source) == carrying

        # Edited since that commit: the commit no longer describes these bytes.
        source.write_text("---\nname: switchyard-board\n---\n\nEdited.\n", encoding="utf-8")
        assert board_skill.resolve_source_commit(source) == board_skill.UNKNOWN_COMMIT


def test_a_release_export_is_trusted_through_its_marker() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-board-skill-release.") as tmp:
        release = Path(tmp) / "release"
        source = _skill_checkout(release, "---\nname: switchyard-board\n---\n\nBody.\n")
        # A release tree is a git-less export plus the marker, so the marker is
        # the only thing that can name the commit.
        (release / board_skill.RELEASE_MARKER_NAME).write_text(
            json.dumps({"commit": "a" * 40}) + "\n", encoding="utf-8"
        )
        assert board_skill.resolve_source_commit(source) == "a" * 40


def test_an_unprovenanced_copy_is_reported_rather_than_counted_as_installed() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-board-skill-unknown.") as tmp:
        home = Path(tmp)
        result = _run_installer(
            "install", "--home", str(home), "--source-commit", board_skill.UNKNOWN_COMMIT
        )
        assert result.returncode == 0
        assert "warning" in result.stderr and "unprovenanced" in result.stderr

        verified = _run_installer("verify", "--home", str(home))
        assert verified.returncode == 1
        assert verified.stdout.count(": unprovenanced ") == 4
        assert board_skill.failed_runtimes(board_skill.verify_board_skill(home=home)) == [
            runtime for runtime, _root in board_skill.SKILL_ADAPTERS
        ]


def test_install_projects_one_identical_body_into_every_runtime() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-board-skill.") as tmp:
        home = Path(tmp)
        first = _run_installer("install", "--home", str(home), "--source-commit", "c0ffee")
        assert first.returncode == 0, first.stderr
        assert first.stdout.count(": installed ") == 4

        bodies = {path.read_text(encoding="utf-8") for _runtime, path in board_skill.adapter_paths(home)}
        assert len(bodies) == 1
        installed = bodies.pop()
        assert board_skill.parse_provenance(installed) == ("c0ffee", str(board_skill.CANONICAL_RELATIVE_SOURCE))
        assert installed.startswith(CANONICAL.read_text(encoding="utf-8").rstrip("\n"))

        verified = _run_installer("verify", "--home", str(home), "--source-commit", "c0ffee")
        assert verified.returncode == 0, verified.stderr
        assert verified.stdout.count(": present ") == 4

        again = _run_installer("install", "--home", str(home), "--source-commit", "c0ffee")
        assert again.returncode == 0
        assert again.stdout.count(": current ") == 4, again.stdout


def test_upgrade_refreshes_managed_copies_and_reports_stale_ones() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-board-skill-upgrade.") as tmp:
        home = Path(tmp)
        assert _run_installer("install", "--home", str(home), "--source-commit", "old").returncode == 0
        stale = _run_installer("verify", "--home", str(home), "--source-commit", "new")
        assert stale.returncode == 1
        assert stale.stdout.count(": stale ") == 4

        planned = _run_installer("install", "--home", str(home), "--source-commit", "new", "--dry-run")
        assert planned.stdout.count(": would refresh ") == 4
        assert board_skill.parse_provenance(
            dict(board_skill.adapter_paths(home))["claude"].read_text(encoding="utf-8")
        ) == ("old", str(board_skill.CANONICAL_RELATIVE_SOURCE))

        assert _run_installer("install", "--home", str(home), "--source-commit", "new").returncode == 0
        assert _run_installer("verify", "--home", str(home), "--source-commit", "new").returncode == 0


def test_a_skill_switchyard_did_not_write_is_never_overwritten() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-board-skill-foreign.") as tmp:
        home = Path(tmp)
        codex = dict(board_skill.adapter_paths(home))["codex"]
        codex.parent.mkdir(parents=True)
        codex.write_text("---\nname: switchyard-board\n---\n\nHand written.\n", encoding="utf-8")

        result = _run_installer("install", "--home", str(home), "--source-commit", "c0ffee")
        assert result.returncode == 0
        assert ": unmanaged " in result.stdout
        assert codex.read_text(encoding="utf-8") == "---\nname: switchyard-board\n---\n\nHand written.\n"

        # It is reported rather than silently tolerated.
        verified = _run_installer("verify", "--home", str(home))
        assert verified.returncode == 1
        assert "codex" in verified.stderr
        assert "switchyard board-skill install" in verified.stderr

        forced = _run_installer("install", "--home", str(home), "--source-commit", "c0ffee", "--force")
        assert forced.returncode == 0
        assert board_skill.parse_provenance(codex.read_text(encoding="utf-8")) is not None


def test_every_pointer_names_the_skill_that_is_actually_installed() -> None:
    """A rename must not leave a session chasing a skill by an old name.

    The pane hook is deliberately import-free -- it runs inside every CLI's hook
    path -- so its copy of the name is pinned here instead of shared.
    """
    hook = _load_pane_hook()
    assert hook.BOARD_SKILL_NAME == board_skill.SKILL_NAME
    assert board_skill.SKILL_NAME in hook.BOARD_SKILL_INSTRUCTION
    assert notify_listener.BOARD_SKILL_NAME == board_skill.SKILL_NAME
    assert board_skill.SKILL_NAME in notify_listener.BOARD_SKILL_INSTRUCTION


def test_only_real_handoffs_carry_the_skill_pointer() -> None:
    message = "SYRD-1 -- Title entered Implementation"
    handed_off = notify_listener.with_board_skill_instruction(message, kind="transition")
    assert handed_off.startswith(message)
    assert notify_listener.BOARD_SKILL_NAME in handed_off
    # The pointer, not the body.
    assert len(handed_off) - len(message) < 120
    for quiet in ("nudge", "idle_reminder", "escalation", "awaiting_role"):
        assert notify_listener.with_board_skill_instruction(message, kind=quiet) == message
    assert notify_listener.with_board_skill_instruction(handed_off, kind="transition") == handed_off


def test_every_session_start_context_names_the_skill() -> None:
    hook = _load_pane_hook()
    with tempfile.TemporaryDirectory(prefix="switchyard-board-skill-hook.") as tmp:
        remit = Path(tmp) / "app.md"
        remit.write_text("# Role remit\n", encoding="utf-8")
        environ = {
            "TICKET_BOARD_ROLE_ONBOARDING": str(remit),
            "TICKET_BOARD_CALLER_ROLE_MAP": '{"porter-app": "app"}',
        }
        output = hook._session_start_additional_context_output(
            target="porter-app:0.0",
            payload={"source": hook.SESSION_START_FRESH_SOURCE},
            environ=environ,
        )
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context.startswith(hook.BOARD_SKILL_INSTRUCTION)
    assert "Read your role remit" in context
    assert hook._with_board_skill_instruction(context) == context


def test_launcher_installs_the_skill_as_the_project_owner() -> None:
    from scripts import team_launcher

    with tempfile.TemporaryDirectory(prefix="switchyard-board-skill-launch.") as tmp:
        root = Path(tmp)
        layout = root / "layout.json"
        layout.write_text(
            json.dumps(team_launcher._new_project_layout_payload(1), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = root / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "run_as_user": "porter-agent",
                    "session_dir": str(root / "sessions"),
                    "roles": [
                        {
                            "role": "director",
                            "slot": 0,
                            "target": "porter-director:0.0",
                            "tmux_session": "porter-director",
                            "workdir": str(root),
                            "cli": ["claude"],
                        }
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = team_launcher.load_project_config("porter", config_path)
    args = team_launcher.install_generated_project_board_skill_args(
        config,
        installer=Path("/opt/switchyard/current/scripts/switchyard-board-skill"),
        source_commit="c0ffee",
    )
    assert args[:4] == ["sudo", "-u", "porter-agent", "-H"]
    assert args[4].endswith("switchyard-board-skill")
    assert "install" in args and "--source-commit" in args and "c0ffee" in args
    assert "--home" in args


if __name__ == "__main__":
    run_module_tests(globals())
    print("board_skill_test: ok")
