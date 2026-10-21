#!/usr/bin/env python3
"""SYRD-597: the Director yields after routing, and watches nothing nonstop.

Directors repeatedly spent their model quota capturing worker panes and
re-reading unchanged tickets while waiting for progress. The board already
reports that progress, so the rule is: after routing, yield; read one pane or
ticket only for a named, bounded reason.

The rule lives in hand-written bodies: the Director skill every Director CLI
loads, the Director guide a provisioned project carries, and short reminders in
the board guide and the shared board skill. This module pins the rule in each,
then pushes the real bodies through what delivers them -- the board skill
installer, run as the launcher runs it, into all four CLI trees, and onboarding
provisioning and upgrade -- and checks every installed copy is its source.

Nothing here adds a runtime check: a polling loop to police polling would be
the thing the rule forbids.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from standalone_test_runner import run_module_tests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import project_onboarding, team_launcher  # noqa: E402
from scripts.ticket_board import board_skill  # noqa: E402

DIRECTOR_SKILL = ROOT / board_skill.DIRECTOR_SKILL.relative_source
BOARD_SKILL = ROOT / board_skill.BOARD_SKILL.relative_source
ONBOARDING = ROOT / "docs" / "onboarding"
GUIDE_NAME = "switchyard-director-guide.md"
BOARD_GUIDE_NAME = "switchyard-board-guide.md"
GUIDE = ONBOARDING / GUIDE_NAME
BOARD_GUIDE = ONBOARDING / BOARD_GUIDE_NAME
README = ONBOARDING / "README.md"

SKILL_HEADING = "## Yield after routing: quota-safe monitoring"
GUIDE_HEADING = "## After routing, yield: quota-safe monitoring"

# What a Director carrier has to state. Patterns rather than sentences: the skill
# and the guide are written for different readers, but neither may lose a clause.
RULE = (
    ("yield after routing", r"after (?:you route work|routing), yield"),
    ("silence after routing is normal", r"silence[^.]{0,40}\bnormal"),
    ("the board reports handoffs", r"handoff"),
    ("the board escalates each stall once", r"(?:once per stall|escalation per stall)"),
    ("the board names leftover processes", r"leftover background process"),
    ("one read answers what needs the Director", r"`ticket-board-read director`"),
    ("rationale: quota is shared capacity", r"quota is (?:a )?shared operational"),
    ("rationale: coordination stops with capacity idle", r"implementer still has capacity"),
    ("no continuous capture", r"(?:capture panes continuously|continuous pane capture)"),
    ("no fleet-wide sweep", r"sweep(?:ing)? every pane or terminal"),
    ("no re-reading unchanged tickets", r"re-read(?:ing)? a ticket nothing has told you changed"),
    ("no tight or indefinite status loop", r"`watch`, `while true`, a repeated `sleep` and check"),
    ("no background poll without a deadline", r"background poll[^.;]{0,30}without an explicit deadline"),
    (
        "watching never substitutes for notifications",
        r"(?:substitute for notifications|in place of the notifications)",
    ),
    ("a failed notification is a defect to file", r"file it"),
    ("a ticket is read when something needs it", r"read a ticket when a notification, a handoff or a decision"),
    # Each of the three is its own list item, so one cannot hide inside another.
    ("bounded check: the condition", r"- (?:\*\*)?the condition(?:\*\*)? you are waiting for;"),
    ("bounded check: the deadline", r"- (?:\*\*)?the deadline(?:\*\*)?;"),
    ("bounded check: the stop rule", r"- (?:\*\*)?the stop rule(?:\*\*)?: stop "),
    ("bounded check: one target, not the fleet", r"one pane or ticket concerned rather than the fleet"),
    ("a background check has an end", r"one process, a deadline,\s+and an end"),
)

# The only reasons for one targeted pane read, in order.
REASONS = (
    ("a notification-delivery failure", r"delivery failure"),
    ("a suspected stuck process", r"stuck process"),
    ("an explicit status request", r"explicit status request"),
    ("a bounded incident investigation", r"bounded incident investigation"),
)

# Lines the ticket retires: each taught a pane read with no reason and no bound.
RETIRED = (
    (DIRECTOR_SKILL, r"1\. Read the pane with `directorctl capture` before concluding anything\."),
    (DIRECTOR_SKILL, r"reads a pane; it is read-only and is the right\s+way to see what a pane is doing\."),
    (BOARD_GUIDE, r"is the right way to see what a pane is doing\.\n"),
)

CARRIERS = (("Director skill", DIRECTOR_SKILL, SKILL_HEADING), ("Director guide", GUIDE, GUIDE_HEADING))


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _missing(text: str) -> list[str]:
    flat = _flat(text)
    return [label for label, pattern in RULE if not re.search(pattern, flat, flags=re.IGNORECASE)]


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end >= 0 else len(text)]


def _headings(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("## ")]


# --- the rule, in the bodies people read --------------------------------------


def test_each_director_carrier_states_the_whole_rule_in_its_own_section() -> None:
    for label, path, heading in CARRIERS:
        text = path.read_text(encoding="utf-8")
        assert heading in _headings(text), f"{label} has no {heading!r} section"
        assert _missing(_section(text, heading)) == [], (
            f"{label}'s section does not state: {_missing(_section(text, heading))}"
        )


def test_each_director_carrier_lists_exactly_the_four_reasons_for_a_pane_read() -> None:
    for label, path, heading in CARRIERS:
        section = _section(path.read_text(encoding="utf-8"), heading)
        items = re.findall(r"^(\d+)\. (.*?)(?=^\d+\. |^\S|\Z)", section, flags=re.MULTILINE | re.DOTALL)
        assert [number for number, _ in items] == ["1", "2", "3", "4"], f"{label}: {items}"
        for (number, body), (reason, pattern) in zip(items, REASONS):
            assert re.search(pattern, _flat(body), flags=re.IGNORECASE), f"{label} item {number} is not {reason}: {body}"


def test_the_skill_section_sits_with_pane_control_before_notification_recovery() -> None:
    headings = _headings(DIRECTOR_SKILL.read_text(encoding="utf-8"))
    at = headings.index(SKILL_HEADING)
    assert headings[at - 1] == "## Roles, sessions and visible panes", headings
    assert headings.index("## Notification recovery") > at, headings


def test_the_skill_ties_every_capture_it_teaches_to_a_reason() -> None:
    text = DIRECTOR_SKILL.read_text(encoding="utf-8")
    roles = _flat(_section(text, "## Roles, sessions and visible panes"))
    bullet = re.search(r"`directorctl capture <target>`.{0,160}?- never to watch", roles)
    assert bullet and "Yield after routing" in bullet.group(0), roles
    recovery = _flat(_section(text, "## Notification recovery"))
    step = re.search(r"1\. (.*?) 2\. ", recovery)
    assert step, recovery
    step = step.group(1)
    assert "that one pane" in step and "once" in step and "not licence to watch" in step, step
    assert "Notification recovery" in _flat(_section(text, SKILL_HEADING))
    description = re.search(r"^description: (.*)$", text, flags=re.MULTILINE)
    assert description, "the skill has no description line"
    description = description.group(1)
    assert "quota-safe monitoring" in description and len(description) <= 1024, description


def test_the_guide_section_follows_routing_and_names_its_watching_failure_mode() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    headings = _headings(text)
    assert headings[headings.index(GUIDE_HEADING) - 1] == "## Routing and holding", headings
    assert "SYRD-597" in _section(text, GUIDE_HEADING)
    failures = _flat(_section(text, "## Failure modes to watch for in yourself"))
    assert "**Watching instead of waiting.**" in failures and "After routing, yield" in failures, failures
    # The guide's cross-reference must name a heading the skill really has.
    assert "When a pane did not get told" in _flat(_section(text, GUIDE_HEADING))
    assert "When a pane did not get told" in DIRECTOR_SKILL.read_text(encoding="utf-8")


def test_the_reading_order_sends_a_new_director_to_the_section() -> None:
    readme = README.read_text(encoding="utf-8")
    entry = _flat(readme[readme.index(f"3. **[{GUIDE_NAME}]") : readme.index("## Start here")])
    assert "**After routing, yield**" in entry and "targeted pane or ticket check" in entry, entry
    assert "Host health the board depends on" in entry, entry


def test_the_shared_guidance_carries_the_short_reminder() -> None:
    board_guide = _flat(_section(BOARD_GUIDE.read_text(encoding="utf-8"), "## Talking to panes: directorctl"))
    assert "**Capture one pane for a reason, never to watch.**" in board_guide, board_guide
    assert "**After routing, yield**" in board_guide and "**After routing, yield**" in GUIDE.read_text(encoding="utf-8")
    skill = _flat(_section(BOARD_SKILL.read_text(encoding="utf-8"), "## Background commands"))
    reminder = re.search(r"\*\*Do not poll to watch progress\.\*\*(.*)", skill)
    assert reminder, f"the board skill's background-command rule has no polling reminder: {skill}"
    reminder = reminder.group(1)
    for clause in ("unchanged ticket", "model quota", "deadline", "stop rule", "check the one thing concerned"):
        assert clause in reminder, (clause, reminder)


def test_no_carrier_keeps_an_unbounded_pane_read() -> None:
    for path, pattern in RETIRED:
        assert not re.search(pattern, path.read_text(encoding="utf-8")), f"{path.name} still says {pattern!r}"


# --- what delivers it -----------------------------------------------------------


def _release(root: Path, commit: str) -> Path:
    """A deployed tree: this checkout's scripts and skills, exported as release ``commit``."""
    tree = root / f"release-{commit}"
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(ROOT / "scripts", tree / "scripts", ignore=ignore)
    shutil.copytree(ROOT / "skills", tree / "skills", ignore=ignore)
    (tree / board_skill.RELEASE_MARKER_NAME).write_text(json.dumps({"commit": commit}) + "\n", encoding="utf-8")
    return tree


def _installer(release: Path, command: str, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """The installer that ships in ``release``, run with the argv the launcher builds (minus its sudo hop)."""
    return subprocess.run(
        [sys.executable, str(release / "scripts" / project_onboarding.BOARD_SKILL_INSTALLER_NAME), command,
         "--home", str(home), *args],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def _expected(skill: board_skill.CanonicalSkill, commit: str) -> str:
    body = (ROOT / skill.relative_source).read_text(encoding="utf-8")
    return board_skill.stamp_skill(body, source_commit=commit, source=str(skill.relative_source))


def test_the_installer_puts_both_skills_in_every_cli_tree_as_their_sources() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd597-skill.") as tmp:
        home = Path(tmp) / "home"
        release = _release(Path(tmp), "c0ffee597")
        result = _installer(release, "install", home, "--source-commit", "c0ffee597")
        assert result.returncode == 0 and result.stderr == "", result.stdout + result.stderr
        for skill in (board_skill.BOARD_SKILL, board_skill.DIRECTOR_SKILL):
            paths = board_skill.adapter_paths(home, skill)
            assert {runtime for runtime, _ in paths} == {"claude", "codex", "agy", "hermes", "copilot"}
            for runtime, path in paths:
                assert path.is_file(), f"{skill.name} was not installed for {runtime} at {path}"
                assert path.read_text(encoding="utf-8") == _expected(skill, "c0ffee597"), (skill.name, runtime)
                assert result.stdout.count(f"{skill.name} {runtime}: installed {path}") == 1, result.stdout
        for runtime, path in board_skill.adapter_paths(home, board_skill.DIRECTOR_SKILL):
            assert _missing(_section(path.read_text(encoding="utf-8"), SKILL_HEADING)) == [], runtime
        verified = _installer(release, "verify", home, "--source-commit", "c0ffee597")
        assert verified.returncode == 0, verified.stdout + verified.stderr


def test_an_upgrade_refreshes_skills_that_still_teach_unbounded_watching() -> None:
    stale = {
        board_skill.DIRECTOR_SKILL: "---\nname: switchyard-director\n---\n\n"
        "1. Read the pane with `directorctl capture` before concluding anything.\n",
        board_skill.BOARD_SKILL: "---\nname: switchyard-board\n---\n\n## Background commands\n\nGive it an end.\n",
    }
    with tempfile.TemporaryDirectory(prefix="syrd597-skill-upgrade.") as tmp:
        home = Path(tmp) / "home"
        release = _release(Path(tmp), "n3w597")
        for skill, body in stale.items():
            for _runtime, path in board_skill.adapter_paths(home, skill):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    board_skill.stamp_skill(body, source_commit="0ld597", source=str(skill.relative_source)),
                    encoding="utf-8",
                )

        result = _installer(release, "install", home, "--source-commit", "n3w597")

        assert result.returncode == 0, result.stdout + result.stderr
        for skill in stale:
            for runtime, path in board_skill.adapter_paths(home, skill):
                assert f"{skill.name} {runtime}: refreshed {path}" in result.stdout, result.stdout
                assert path.read_text(encoding="utf-8") == _expected(skill, "n3w597"), (skill.name, runtime)
        again = _installer(release, "install", home, "--source-commit", "n3w597")
        # Every canonical skill for every CLI: five CLIs since SYRD-634's Copilot, three skills since SYRD-624's Librarian.
        assert again.returncode == 0 and again.stdout.count(": current ") == (
            len(board_skill.SKILL_ADAPTERS) * len(board_skill.CANONICAL_SKILLS)), again.stdout


def _docs_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Git for real; the privileged `install -d` helper as a plain mkdir."""
    if args[:1] == ["git"]:
        run_kwargs = dict(kwargs)
        run_kwargs.setdefault("text", True)
        run_kwargs.setdefault("stdout", subprocess.PIPE)
        run_kwargs.setdefault("stderr", subprocess.PIPE)
        return subprocess.run(args, **run_kwargs)  # type: ignore[arg-type]
    if args[:1] == ["install"]:
        for raw_path in args[args.index("-g") + 2 :]:
            Path(raw_path).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args, 0)
    return subprocess.CompletedProcess(args, 0)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Switchyard Test", "-c", "user.email=switchyard-test@example.invalid", *args],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def _commit_packet(repo: Path, bodies: dict[str, str]) -> str:
    docs = repo / "docs" / "onboarding"
    docs.mkdir(parents=True, exist_ok=True)
    for name, body in bodies.items():
        docs.joinpath(name).write_text(body, encoding="utf-8")
    _git("-C", str(repo), "add", "docs")
    _git("-C", str(repo), "commit", "-q", "-m", "onboarding packet")
    return _git("-C", str(repo), "rev-parse", "HEAD")


def _current_packet() -> dict[str, str]:
    return {name: (ONBOARDING / name).read_text(encoding="utf-8") for name in team_launcher.SWITCHYARD_ONBOARDING_DOC_NAMES}


def _stamped(name: str, commit: str, body: str) -> str:
    return (
        f"<!-- Switchyard onboarding snapshot: source commit {commit}; source docs/onboarding/{name}. -->\n\n{body}"
    )


def test_provisioning_installs_the_packet_as_its_source() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd597-provision.") as tmp:
        root = Path(tmp)
        project, repo = root / "project", root / "source-repo"
        project.mkdir()
        _git("init", "-q", "-b", "main", str(repo))
        commit = _commit_packet(repo, _current_packet())
        output: list[str] = []

        team_launcher._install_switchyard_onboarding_docs(
            source_repo=repo,
            project_dir=project,
            owner_user=team_launcher.current_user_name(),
            runner=_docs_runner,
            print_func=output.append,
        )

        for name, body in _current_packet().items():
            installed = (project / "docs" / "onboarding" / name).read_text(encoding="utf-8")
            assert installed == _stamped(name, commit, body), name
        guide = (project / "docs" / "onboarding" / GUIDE_NAME).read_text(encoding="utf-8")
        assert _missing(_section(guide, GUIDE_HEADING)) == []
        # The fresh Director's stored remit sends it to exactly that installed guide.
        seed = team_launcher.director_onboarding_seed_text(project)
        assert str(project / "docs" / "onboarding" / GUIDE_NAME) in seed, seed


def test_upgrade_refreshes_a_packet_that_predates_the_rule() -> None:
    old = dict(_current_packet())
    old[GUIDE_NAME] = "# Switchyard Director Guide\n\n**Speculating instead of reading.** Read the pane.\n"
    old[BOARD_GUIDE_NAME] = "# Board guide\n\n- **`capture` is read-only** and is the right way to see what a pane is doing.\n"
    old["README.md"] = "# Onboarding\n\n3. **[switchyard-director-guide.md](x)**\n\n## Start here\n"
    with tempfile.TemporaryDirectory(prefix="syrd597-upgrade.") as tmp:
        root = Path(tmp)
        project, repo = root / "project", root / "source-repo"
        project.mkdir()
        owner = team_launcher.current_user_name()
        _git("init", "-q", "-b", "main", str(repo))
        _commit_packet(repo, old)
        team_launcher._install_switchyard_onboarding_docs(
            source_repo=repo, project_dir=project, owner_user=owner, runner=_docs_runner, print_func=lambda _l: None
        )
        assert GUIDE_HEADING not in (project / "docs" / "onboarding" / GUIDE_NAME).read_text(encoding="utf-8")

        commit = _commit_packet(repo, _current_packet())
        output: list[str] = []
        team_launcher.upgrade_switchyard_onboarding_docs(
            source_repo=repo, project_dir=project, owner_user=owner, dry_run=False,
            runner=_docs_runner, print_func=output.append,
        )

        for name in (GUIDE_NAME, BOARD_GUIDE_NAME, "README.md"):
            assert any(f"onboarding doc {name}: refreshed" in line for line in output), (name, output)
        for name, body in _current_packet().items():
            installed = (project / "docs" / "onboarding" / name).read_text(encoding="utf-8")
            if name in (GUIDE_NAME, BOARD_GUIDE_NAME, "README.md"):
                assert installed == _stamped(name, commit, body), name


def main() -> int:
    run_module_tests(globals())
    print("director_quota_safe_monitoring_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
