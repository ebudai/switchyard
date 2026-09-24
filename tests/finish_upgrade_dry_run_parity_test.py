#!/usr/bin/env python3
"""SYRD-254: `finish-upgrade --dry-run` previews what apply will do, including how it ends.

Live MEFP: the dry run said only

    switchyard: would migrate mefp's declarative director onboarding as stellaris-agent

and exited 0. Apply then made the one-way initial workflow write and exited 1,
because it could not resolve its release from origin/main -- a read-only check
the dry run never reached, because it returned before it.

A dry run is only worth running if its verdict is apply's verdict. So it now
walks the same steps: every read-only check is performed for real (the pinned
source, the handoff, the per-role cutover, the owner's release root, and the
release resolution), every write is only described, and a step apply would
fail on fails the dry run too -- before the Director takes the step that
cannot be undone.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from team_launcher_test_helpers import FakeRunner, team_launcher  # noqa: E402
import team_launcher_upgrade_cutover_test as cutover  # noqa: E402

CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tree_digest(*roots: Path) -> dict[str, str]:
    """Every file under the given roots, by content: what a dry run may not change."""
    seen: dict[str, str] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                seen[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return seen


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"},
    ).stdout.strip()


class Tenant:
    """A declarative tenant whose director's board writes are recorded, not made."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        board = cutover._deployed_release(tmp, "porter", "a" * 40)
        self.config_path, _ = cutover._declarative_tenant(tmp, board_root=board)
        self.config = team_launcher.load_project_config("porter", self.config_path)
        self.writes: list[str] = []

    def run(self, *, dry_run: bool, source_repo: Path, deploy_ref: str) -> tuple[int, str]:
        printed: list[str] = []
        saved = {
            name: getattr(team_launcher, name)
            for name in ("migrate_declarative_director_onboarding", "director_onboarding_state",
                         "record_upgrade_phase", "record_release_phase_from_status",
                         "_open_board_url")
        }
        team_launcher.migrate_declarative_director_onboarding = (
            lambda *a, **k: self.writes.append("board: director onboarding") or True
        )
        team_launcher.director_onboarding_state = lambda *a, **k: ("done", "")
        team_launcher.record_upgrade_phase = (
            lambda *a, **k: self.writes.append(f"phase: {k.get('phase')}")
        )
        team_launcher.record_release_phase_from_status = (
            lambda *a, **k: self.writes.append("phase: release")
        )
        team_launcher._open_board_url = cutover._board_with_marker(True)
        try:
            code = team_launcher.finish_upgrade_command(
                self.config, config_path=self.config_path, dry_run=dry_run,
                source_repo=source_repo, deploy_ref=deploy_ref,
                runner=FakeRunner(), print_func=printed.append,
            )
        finally:
            for name, value in saved.items():
                setattr(team_launcher, name, value)
        return code, "\n".join(printed)


def test_the_live_shape_a_release_that_will_not_resolve() -> None:
    """MEFP: apply fails on its release, so the dry run must fail on it first."""
    with tempfile.TemporaryDirectory(prefix="syrd254-unresolved.") as raw:
        tmp = Path(raw)
        source = tmp / "source"
        source.mkdir()
        git("init", "-q", cwd=source)  # no origin/main in it, as on the live host
        tenant = Tenant(tmp)

        before = tree_digest(tmp)
        code, said = tenant.run(dry_run=True, source_repo=source, deploy_ref="origin/main")
        check(tree_digest(tmp) == before, "a dry run changes no file")
        check(tenant.writes == [], f"and makes no write: {tenant.writes}")
        check(code != 0, f"it fails, as apply will: {said}")
        check("origin/main" in said and "cannot" in said, f"naming the release it cannot resolve: {said}")
        check("would migrate porter's declarative director onboarding" in said,
              f"and still shows what it would have done: {said}")

        code, said = tenant.run(dry_run=False, source_repo=source, deploy_ref="origin/main")
        check(code != 0, f"which is what apply does: {said}")
        check("release phase did not complete" in said, said)


def test_a_release_that_resolves_previews_clean_and_names_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd254-resolved.") as raw:
        tmp = Path(raw)
        source = tmp / "source"
        source.mkdir()
        git("init", "-q", "-b", "trunk", cwd=source)
        (source / "README").write_text("r\n")
        git("add", "README", cwd=source)
        git("commit", "-q", "-m", "r", cwd=source)
        target = git("rev-parse", "HEAD", cwd=source)
        tenant = Tenant(tmp)

        before = tree_digest(tmp)
        code, said = tenant.run(dry_run=True, source_repo=source, deploy_ref="trunk")
        check(tree_digest(tmp) == before, "a dry run changes no file")
        check(tenant.writes == [], f"and makes no write: {tenant.writes}")
        check(code == 0, f"it passes: {said}")
        check(target in said, f"naming the release apply would report: {said}")

        code, said = tenant.run(dry_run=False, source_repo=source, deploy_ref="trunk")
        check(code == 0, f"and apply agrees: {said}")


def test_the_dry_run_verdict_is_apply_verdict_for_a_blocked_release() -> None:
    """Resolved, but no safe deploy can be produced: the units are incomplete."""
    with tempfile.TemporaryDirectory(prefix="syrd254-blocked.") as raw:
        tmp = Path(raw)
        source = tmp / "source"
        source.mkdir()
        git("init", "-q", "-b", "trunk", cwd=source)
        (source / "README").write_text("r\n")
        git("add", "README", cwd=source)
        git("commit", "-q", "-m", "r", cwd=source)
        tenant = Tenant(tmp)
        privileged = team_launcher.privileged_provision_dir(
            "porter", root=team_launcher.switchyard_privileged_provision_root()
        )
        for unit in (*privileged.glob("porter-ticket-board*.service"),
                     *tenant.config_path.parent.glob("porter-ticket-board*.service")):
            unit.unlink()

        dry, dry_said = tenant.run(dry_run=True, source_repo=source, deploy_ref="trunk")
        real, real_said = tenant.run(dry_run=False, source_repo=source, deploy_ref="trunk")
        check((dry != 0) == (real != 0), f"same verdict: dry {dry} / apply {real}\n{dry_said}\n--\n{real_said}")
        check("units are incomplete" in dry_said, f"for the same reason: {dry_said}")


def test_the_dry_run_previews_the_one_way_workflow_install() -> None:
    """The handoff is checked exactly as apply checks it, and not written."""
    import json

    from scripts import workflow_manage
    from scripts.ticket_board.project_provision import workflow_document_digest

    with tempfile.TemporaryDirectory(prefix="syrd254-handoff.") as raw:
        tmp = Path(raw)
        source = tmp / "source"
        source.mkdir()
        git("init", "-q", "-b", "trunk", cwd=source)
        (source / "README").write_text("r\n")
        git("add", "README", cwd=source)
        git("commit", "-q", "-m", "r", cwd=source)
        tenant = Tenant(tmp)
        document = json.loads((ROOT / "examples" / "workflows" / "inspection.json").read_text())
        effective = workflow_document_digest(
            team_launcher.effective_workflow_document(document, tenant.config_path)
        )
        team_launcher.publish_workflow_handoff("porter", team_launcher.WorkflowMigration(
            project="porter", document=document, digest=workflow_document_digest(document),
            effective_digest=effective, board_revision=0,
        ))
        attempted: list = []
        saved = (team_launcher.read_board_workflow_state, workflow_manage.main)
        team_launcher.read_board_workflow_state = lambda _config, **_k: (0, None, "")
        workflow_manage.main = lambda args: attempted.append(args) or 0
        try:
            before = tree_digest(tmp)
            code, said = tenant.run(dry_run=True, source_repo=source, deploy_ref="trunk")
            check(tree_digest(tmp) == before, "a dry run changes no file")
            check(attempted == [] and tenant.writes == [], f"and makes no write: {attempted} {tenant.writes}")
            check(code == 0, f"{said}")
            check("would install porter's handed-off workflow" in said and effective in said,
                  f"the one-way step is named, with the digest it would write: {said}")

            path = team_launcher.workflow_handoff_path("porter")
            edited = json.loads(path.read_text())
            edited["document"]["roles"][0]["label"] = "Somebody Else"
            path.write_text(json.dumps(edited))
            code, said = tenant.run(dry_run=True, source_repo=source, deploy_ref="trunk")
            check(code != 0, f"a handoff apply would refuse fails the dry run: {said}")
            check("handed-off workflow would be refused" in said, said)
            check(attempted == [], "still without a write")
        finally:
            team_launcher.read_board_workflow_state, workflow_manage.main = saved


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"finish_upgrade_dry_run_parity_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
