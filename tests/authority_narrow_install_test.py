#!/usr/bin/env python3
"""SYRD-93 live acceptance: install the boundary without moving anything else.

The host that needs the integration command runs a board and a shared release
that are AHEAD of the audited candidate on a different line of history. Handing
it the candidate as a whole release would replace an accepted board, generated
artifacts, hooks, skills and staged tooling with older ones -- a downgrade
dressed as a bootstrap.

So the narrow installer puts in four files and one sudo rule. This starts from
exactly that awkward state -- staged tooling from a newer, divergent release --
runs the real program, and asserts that everything except the boundary is
byte-for-byte where it was.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPT = ROOT / "scripts" / "switchyard-install-authority"
AUTHORITY = (
    "switchyard-publish-ref",
    "switchyard-integrate-main",
    "switchyard-integrate",
    "switchyard_publication_authority.py",
)
#: What a tenant already has staged, from a release this candidate knows nothing
#: about: newer tools, newer board clients, a newer marker.
DIVERGENT = {
    "ticket-board-write": b"#!/bin/sh\n# from the newer divergent release\n",
    "ticket-board-read": b"#!/bin/sh\n# from the newer divergent release\n",
    "directorctl": b"#!/bin/sh\n# from the newer divergent release\n",
    "ticket-board-pane-idle-hook": b"#!/bin/sh\n# from the newer divergent release\n",
    "switchyard-request-publication": b"#!/bin/sh\n# from the newer divergent release\n",
    "board_skill_cli.py": b"# from the newer divergent release\n",
}


def sandbox(root: Path) -> dict[str, Path]:
    """A tenant staged from a newer release, and root's copy of the candidate."""
    staging_root = root / "usr-local-lib-switchyard"
    staging = staging_root / "demo"
    staging.mkdir(parents=True)
    for name, body in DIVERGENT.items():
        (staging / name).write_bytes(body)
        (staging / name).chmod(0o755)
    (staging / ".switchyard-release.json").write_text(
        json.dumps({"commit": "d" * 40, "source_ref": "d" * 40}), encoding="utf-8"
    )
    (staging / "ticket_board").mkdir()
    (staging / "ticket_board" / "app.py").write_bytes(b"# newer board package\n")
    (staging / "skills").mkdir()
    (staging / "skills" / "switchyard-board.md").write_bytes(b"# newer skill\n")

    # Root's own checkout of the audited commit: the whole repository, because
    # that is what the bundle import produces.
    source = root / "bootstrap-src"
    (source / "scripts").mkdir(parents=True)
    for name in (*AUTHORITY, "switchyard-install-authority"):
        (source / "scripts" / name).write_bytes((ROOT / "scripts" / name).read_bytes())
        (source / "scripts" / name).chmod(0o755)
    (source / "scripts" / "ticket_board").symlink_to(ROOT / "scripts" / "ticket_board")
    (source / ".switchyard-release.json").write_text(
        json.dumps({"commit": "c" * 40}), encoding="utf-8"
    )

    sudoers = root / "sudoers.d"
    sudoers.mkdir()
    visudo = root / "visudo"
    visudo.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    visudo.chmod(0o755)
    return {"staging_root": staging_root, "staging": staging, "source": source,
            "sudoers": sudoers, "visudo": visudo, "provision": root / "provision"}


def environment(paths: dict[str, Path], *, visudo: Path | None = None) -> dict[str, str]:
    return {
        **os.environ,
        "SWITCHYARD_AUTHORITY_STAGING_ROOT": str(paths["staging_root"]),
        "SWITCHYARD_SUDOERS_ROOT": str(paths["sudoers"]),
        "SWITCHYARD_PRIVILEGED_PROVISION_ROOT": str(paths["provision"]),
        "SWITCHYARD_AUTHORITY_VISUDO": str(visudo or paths["visudo"]),
    }


def run(paths: dict[str, Path], *args: str, visudo: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(paths["source"] / "scripts" / "switchyard-install-authority"),
         "--project", "demo", "--owner", "demo-agent", *args],
        text=True, capture_output=True, env=environment(paths, visudo=visudo),
    )


def snapshot(staging: Path) -> dict[str, tuple[bytes, int]]:
    seen: dict[str, tuple[bytes, int]] = {}
    for path in sorted(staging.rglob("*")):
        if path.is_file():
            seen[str(path.relative_to(staging))] = (path.read_bytes(), path.stat().st_mode)
    return seen


def test_it_installs_the_boundary_and_leaves_the_divergent_release_alone() -> None:
    with tempfile.TemporaryDirectory() as raw:
        paths = sandbox(Path(raw))
        before = snapshot(paths["staging"])

        result = run(paths)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout.strip().splitlines()[-1])
        assert report["from_commit"] == "c" * 40, report

        after = snapshot(paths["staging"])
        # Exactly the boundary appeared, and nothing else moved: not the board
        # clients, not the hook, not the package, not the skills, and not the
        # marker naming the release this tenant actually runs.
        assert set(after) - set(before) == set(AUTHORITY), sorted(set(after) - set(before))
        for name, state in before.items():
            assert after[name] == state, f"{name} was changed by a narrow install"
        assert json.loads((paths["staging"] / ".switchyard-release.json").read_text())["commit"] == "d" * 40

        for name in AUTHORITY:
            installed = paths["staging"] / name
            assert installed.read_bytes() == (ROOT / "scripts" / name).read_bytes(), name
            expected = 0o755 if not name.endswith(".py") else 0o644
            assert installed.stat().st_mode & 0o777 == expected, (name, oct(installed.stat().st_mode))

        rule = (paths["sudoers"] / "48-demo-publish").read_text()
        assert rule.count("NOPASSWD:") == 2, rule
        assert "switchyard-publish-ref" in rule and "switchyard-integrate-main" in rule, rule


def test_running_it_again_changes_nothing_and_keeps_one_way_back() -> None:
    with tempfile.TemporaryDirectory() as raw:
        paths = sandbox(Path(raw))
        assert run(paths).returncode == 0
        first = snapshot(paths["staging"])
        record = json.loads((paths["provision"] / "demo" / "authority-install.json").read_text())

        assert run(paths).returncode == 0
        assert snapshot(paths["staging"]) == first
        # The second install must not record ITSELF as the way back, or the
        # rollback would restore the boundary it just replaced.
        again = json.loads((paths["provision"] / "demo" / "authority-install.json").read_text())
        assert again["previously_present"] == record["previously_present"] or all(
            again["previously_present"][name] for name in AUTHORITY
        ), (record["previously_present"], again["previously_present"])


def test_the_way_back_restores_exactly_what_was_there() -> None:
    with tempfile.TemporaryDirectory() as raw:
        paths = sandbox(Path(raw))
        # This tenant already had an older publisher, and no grant at all.
        older = b"#!/usr/bin/env python3\n# the release before the boundary\n"
        (paths["staging"] / "switchyard-publish-ref").write_bytes(older)
        (paths["staging"] / "switchyard-publish-ref").chmod(0o755)
        before = snapshot(paths["staging"])

        assert run(paths).returncode == 0
        assert (paths["staging"] / "switchyard-publish-ref").read_bytes() != older
        assert (paths["sudoers"] / "48-demo-publish").is_file()

        undone = run(paths, "--rollback")
        assert undone.returncode == 0, undone.stdout + undone.stderr
        assert snapshot(paths["staging"]) == before, "the rollback did not restore the tenant"
        # The grant was absent before, so it is absent again.
        assert not (paths["sudoers"] / "48-demo-publish").exists()
        assert not (paths["provision"] / "demo" / "authority-install.json").exists()


def test_a_grant_that_does_not_validate_installs_nothing() -> None:
    with tempfile.TemporaryDirectory() as raw:
        paths = sandbox(Path(raw))
        refusing = Path(raw) / "visudo-refuses"
        refusing.write_text("#!/bin/sh\necho 'parse error' >&2\nexit 1\n", encoding="utf-8")
        refusing.chmod(0o755)

        result = run(paths, visudo=refusing)
        assert result.returncode != 0, result.stdout
        assert "did not validate" in result.stdout + result.stderr
        assert not (paths["sudoers"] / "48-demo-publish").exists()
        assert not (paths["sudoers"] / "48-demo-publish.staged").exists()


def test_it_refuses_to_run_from_a_path_the_caller_does_not_control() -> None:
    """A copy in a role-writable directory must not be what installs a grant."""
    with tempfile.TemporaryDirectory() as raw:
        paths = sandbox(Path(raw))
        loose = Path(raw) / "loose"
        (loose / "scripts").mkdir(parents=True)
        for name in AUTHORITY:
            (loose / "scripts" / name).write_bytes((ROOT / "scripts" / name).read_bytes())
        (loose / "scripts" / "switchyard-install-authority").write_bytes(SCRIPT.read_bytes())
        (loose / "scripts" / "ticket_board").symlink_to(ROOT / "scripts" / "ticket_board")
        (loose / "scripts").chmod(0o777)

        result = subprocess.run(
            ["python3", str(loose / "scripts" / "switchyard-install-authority"),
             "--project", "demo", "--owner", "demo-agent"],
            text=True, capture_output=True, env=environment(paths),
        )
        assert result.returncode != 0, result.stdout
        assert "does not control" in result.stdout + result.stderr
        assert not (paths["staging"] / "switchyard-integrate-main").exists()
        assert not (paths["sudoers"] / "48-demo-publish").exists()


def test_a_release_without_the_boundary_is_refused_before_anything_moves() -> None:
    with tempfile.TemporaryDirectory() as raw:
        paths = sandbox(Path(raw))
        (paths["source"] / "scripts" / "switchyard-integrate-main").unlink()
        before = snapshot(paths["staging"])

        result = run(paths)
        assert result.returncode != 0, result.stdout
        assert "not the audited authority release" in result.stdout + result.stderr
        assert snapshot(paths["staging"]) == before
        assert not (paths["sudoers"] / "48-demo-publish").exists()


def test_the_dry_run_says_what_it_would_touch_and_touches_nothing() -> None:
    with tempfile.TemporaryDirectory() as raw:
        paths = sandbox(Path(raw))
        before = snapshot(paths["staging"])
        result = run(paths, "--dry-run")
        assert result.returncode == 0, result.stderr
        plan = json.loads(result.stdout.strip().splitlines()[-1])
        assert sorted(Path(p).name for p in plan["would_install"]) == sorted(AUTHORITY), plan
        assert "the board release" in plan["leaves_alone"], plan
        assert snapshot(paths["staging"]) == before
        assert not (paths["sudoers"] / "48-demo-publish").exists()


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"authority_narrow_install_test: {len(tests)} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
