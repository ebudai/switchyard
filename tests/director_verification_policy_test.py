#!/usr/bin/env python3
"""The Director's verification policy, and the carriers that deliver it (SYRD-47).

Audit already runs the suite and records the result. Re-running the same suite
against an unchanged commit buys nothing, so the policy is that an independently
recorded Audit result is evidence for the exact commit it names -- with identity
checked first, focused checks where the evidence leaves a gap, and an honest split
between what the Director ran and what it reused.

The policy lives in two hand-written bodies: the portable skill every Director CLI
loads, and the onboarding guide a provisioned project carries. This module pins the
policy in both (they drift apart silently otherwise) and then pushes the real bodies
through the machinery that delivers them: skill packaging into all four supported
CLI trees, fresh provisioning of the onboarding packet, and upgrade refresh.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from standalone_test_runner import run_module_tests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher  # noqa: E402
from scripts.ticket_board import board_skill  # noqa: E402

SKILL = ROOT / board_skill.DIRECTOR_SKILL.relative_source
GUIDE_NAME = "switchyard-director-guide.md"
GUIDE = ROOT / "docs" / "onboarding" / GUIDE_NAME

# The policy, as the behaviour each carrier has to state. Regexes rather than exact
# sentences: the two bodies are written for different readers and must not be forced
# into one wording, but neither may lose a clause. Case-insensitive because the skill
# names the role ("Audit") where the guide names the activity ("audit").
POLICY = (
    (
        "recorded audit evidence covers the commit it names",
        r"independently recorded [Aa]udit result is evidence for the exact commit",
    ),
    (
        "identity is checked before the evidence is trusted",
        r"commit [Aa]udit recorded is the commit you are integrating",
    ),
    ("focused checks where the evidence leaves a gap", r"focused check"),
    ("re-run trigger: the tree moved", r"tree changed after [Aa]udit recorded its result"),
    (
        "re-run trigger: the recorded run is not good enough",
        r"recorded environment or result is inadequate",
    ),
    ("re-run trigger: a focused check contradicts it", r"contradicts their evidence"),
    ("re-run trigger: policy demands the run", r"repository policy requires"),
    ("a merge gets a smoke check, not the whole suite", r"integration smoke check"),
    (
        "reused evidence is reported as reused",
        r"what you ran yourself and what you reused",
    ),
)

# The rule SYRD-47 retires. Pinned so a well-meaning edit cannot quietly restore the
# duplicated full-suite run that this ticket exists to stop.
RETIRED = r"[Rr]un the tests yourself,\s+(?:at that commit|on the merged tree)"

CARRIERS = (("canonical skill", SKILL), ("onboarding guide", GUIDE))


def _flat(text: str) -> str:
    """One line, so a policy clause is found wherever the prose happens to wrap."""
    return re.sub(r"\s+", " ", text)


def _missing(text: str) -> list[str]:
    flat = _flat(text)
    return [label for label, pattern in POLICY if not re.search(pattern, flat)]


# --- the policy, in the bodies people actually read ---------------------------


def test_both_carriers_state_the_whole_policy() -> None:
    for label, path in CARRIERS:
        assert path.is_file(), f"{label} is missing at {path}"
        assert _missing(path.read_text(encoding="utf-8")) == [], (
            f"{label} ({path.name}) does not state: {_missing(path.read_text(encoding='utf-8'))}"
        )


def test_neither_carrier_still_demands_the_duplicate_full_run() -> None:
    for label, path in CARRIERS:
        text = path.read_text(encoding="utf-8")
        assert not re.search(RETIRED, _flat(text)), f"{label} still requires re-running the whole suite"


def test_the_reuse_permission_is_bounded_by_the_rerun_triggers() -> None:
    """Permission and its limits must sit together; a reader stopping early still gets both.

    A carrier that grants reuse in one section and states the exceptions pages away
    reads as unconditional. Both bodies keep them within a few paragraphs.
    """
    for label, path in CARRIERS:
        flat = _flat(path.read_text(encoding="utf-8"))
        grant = re.search(POLICY[0][1], flat).start()
        limit = re.search(POLICY[3][1], flat).start()
        assert 0 < limit - grant <= 1200, (
            f"{label}: the re-run triggers are {limit - grant} characters from the permission"
        )


def test_the_guide_keeps_the_independent_mutation_it_always_required() -> None:
    """Relaxing the re-run is not relaxing review: the Director's own check stays."""
    text = GUIDE.read_text(encoding="utf-8")
    assert "Kill a mutant of your own" in text
    assert "Confirm the mutation applied" in text
    assert "Confirm the mutated line actually runs" in text
    assert "Prefer a semantic mutation" in text
    assert "__pycache__" in text
    assert "Killing a mutant of your own" in SKILL.read_text(encoding="utf-8")


def test_the_skill_still_requires_ancestry_checks_in_both_directions() -> None:
    """Reused evidence is about a commit, so the commit checks matter more, not less."""
    text = SKILL.read_text(encoding="utf-8")
    assert "git merge-base --is-ancestor origin/main <commit>" in text
    assert "git merge-base --is-ancestor <commit> origin/main" in text


# --- the carriers that deliver it ---------------------------------------------


def _release_tree(root: Path, commit: str) -> Path:
    """A release-shaped export of this checkout's skills, naming ``commit``."""
    tree = root / f"release-{commit}"
    for skill in board_skill.CANONICAL_SKILLS:
        source = tree / skill.relative_source
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes((ROOT / skill.relative_source).read_bytes())
    (tree / board_skill.RELEASE_MARKER_NAME).write_text(
        json.dumps({"commit": commit}) + "\n", encoding="utf-8"
    )
    return tree


def test_the_policy_reaches_every_supported_cli_skill_tree() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd47-skill-install.") as tmp:
        home = Path(tmp)
        source = board_skill.default_source(
            _release_tree(home, "c0ffee"), board_skill.DIRECTOR_SKILL
        )
        results = board_skill.install_board_skill(home=home, source=source)

        # The runtimes are named rather than read back from the adapter table: the
        # claim is that this policy reaches every CLI a Director may be running,
        # which a table that quietly lost one would still satisfy.
        assert {result.runtime for result in results} == {"claude", "codex", "agy", "hermes"}
        assert [result.action for result in results] == ["installed"] * 4
        for runtime, path in board_skill.adapter_paths(home, board_skill.DIRECTOR_SKILL):
            assert _missing(path.read_text(encoding="utf-8")) == [], runtime


def test_upgrade_refreshes_a_director_skill_that_predates_the_policy() -> None:
    """An installed copy carrying the retired rule is replaced, in every runtime."""
    with tempfile.TemporaryDirectory(prefix="syrd47-skill-upgrade.") as tmp:
        home = Path(tmp)
        stale_tree = _release_tree(home, "0ldc0de")
        stale_source = board_skill.default_source(stale_tree, board_skill.DIRECTOR_SKILL)
        stale_source.write_text(
            "---\nname: switchyard-director\n---\n\n"
            "Run the tests yourself, at that commit, on the merged tree.\n",
            encoding="utf-8",
        )
        board_skill.install_board_skill(home=home, source=stale_source)
        for _runtime, path in board_skill.adapter_paths(home, board_skill.DIRECTOR_SKILL):
            assert re.search(RETIRED, _flat(path.read_text(encoding="utf-8")))

        current = board_skill.default_source(
            _release_tree(home, "n3wc0de"), board_skill.DIRECTOR_SKILL
        )
        results = board_skill.install_board_skill(home=home, source=current)

        assert [result.action for result in results] == ["refreshed"] * 4
        for runtime, path in board_skill.adapter_paths(home, board_skill.DIRECTOR_SKILL):
            text = path.read_text(encoding="utf-8")
            assert not re.search(RETIRED, _flat(text)), runtime
            assert _missing(text) == [], runtime


def _docs_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Just enough of the privileged helpers to run the doc paths unprivileged."""
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
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def _source_repo(root: Path) -> Path:
    """An empty source checkout shaped like the Switchyard tree."""
    repo = root / "source-repo"
    (repo / "docs" / "onboarding").mkdir(parents=True)
    _git("init", "-b", "main", str(repo))
    return repo


def _commit_docs(repo: Path, guide_body: str) -> str:
    """Write the onboarding packet with ``guide_body`` and commit it. Returns the commit."""
    docs = repo / "docs" / "onboarding"
    for name in team_launcher.SWITCHYARD_ONBOARDING_DOC_NAMES:
        body = guide_body if name == GUIDE_NAME else f"# {name}\n\nPlaceholder.\n"
        docs.joinpath(name).write_text(body, encoding="utf-8")
    _git("-C", str(repo), "add", "docs")
    _git(
        "-C",
        str(repo),
        "-c",
        "user.name=Switchyard Test",
        "-c",
        "user.email=switchyard-test@example.invalid",
        "commit",
        "-m",
        "onboarding docs",
    )
    return _git("-C", str(repo), "rev-parse", "HEAD")


def test_fresh_provisioning_installs_the_guide_carrying_the_policy() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd47-provision.") as tmp:
        root = Path(tmp)
        project = root / "project"
        project.mkdir()
        repo = _source_repo(root)
        commit = _commit_docs(repo, GUIDE.read_text(encoding="utf-8"))
        output: list[str] = []

        team_launcher._install_switchyard_onboarding_docs(
            source_repo=repo,
            project_dir=project,
            owner_user=team_launcher.current_user_name(),
            runner=_docs_runner,
            print_func=output.append,
        )

        installed = (project / "docs" / "onboarding" / GUIDE_NAME).read_text(encoding="utf-8")
        assert _missing(installed) == []
        assert not re.search(RETIRED, _flat(installed))
        assert f"source commit {commit}" in installed


def test_upgrade_refreshes_a_provisioned_guide_that_predates_the_policy() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd47-upgrade.") as tmp:
        root = Path(tmp)
        project = root / "project"
        project.mkdir()
        owner = team_launcher.current_user_name()
        stale_body = (
            "# Director guide\n\n"
            "**Run the tests yourself, on the merged tree.** Not audit's.\n"
        )
        repo = _source_repo(root)
        _commit_docs(repo, stale_body)

        # The tenant as it stands before the upgrade: provisioned from the old source.
        team_launcher._install_switchyard_onboarding_docs(
            source_repo=repo,
            project_dir=project,
            owner_user=owner,
            runner=_docs_runner,
            print_func=lambda _line: None,
        )
        installed_path = project / "docs" / "onboarding" / GUIDE_NAME
        assert re.search(RETIRED, _flat(installed_path.read_text(encoding="utf-8")))

        # The release that carries SYRD-47, on top of the commit the tenant has.
        current_commit = _commit_docs(repo, GUIDE.read_text(encoding="utf-8"))

        output: list[str] = []
        team_launcher.upgrade_switchyard_onboarding_docs(
            source_repo=repo,
            project_dir=project,
            owner_user=owner,
            dry_run=False,
            runner=_docs_runner,
            print_func=output.append,
        )

        refreshed = installed_path.read_text(encoding="utf-8")
        assert any(f"onboarding doc {GUIDE_NAME}: refreshed" in line for line in output), output
        assert _missing(refreshed) == []
        assert not re.search(RETIRED, _flat(refreshed))
        assert f"source commit {current_commit}" in refreshed


def main() -> int:
    run_module_tests(globals())
    print("director_verification_policy_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
