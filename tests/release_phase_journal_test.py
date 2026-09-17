#!/usr/bin/env python3
"""SYRD-117: a successful deployment closes the release phase, and nothing else does.

The reported failure, from SYRD-102: an operator ran the checksum-pinned
upgrade, deployed the board separately, ran `switchyard finish-upgrade`, and got
exit 0 with `release: done` in the tenant-readable journal while root's
authoritative journal still said `ready`. Two things caused it, and both are
covered here.

The first is that *nothing ever wrote* `done` after a real deployment. The only
place the release phase was closed was the case where the identities
transaction had already switched the release, so an operator who actually
deployed left the phase where it was forever.

The second is that an unprivileged command could write a phase at all.
`finish-upgrade` refuses to run as root by design -- it makes a
director-authority board write -- so every phase it recorded went into the
tenant copy alone, and that copy became the only record claiming a completion.

The fix is a phase that is *re-proved from the running board* rather than
inferred from a previous command's exit status, and a tenant copy that is a
projection instead of a claimant. These cases drive the real functions against
a real filesystem; the board is the one thing stubbed, because the point is
what the code believes about a board, not urllib.
"""

from __future__ import annotations

import json
import os
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
PROJECT = "porter"
DEPLOYED = "a" * 40
OTHER = "b" * 40
SHARED = "c" * 40


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class FakeBoard:
    """A board that reports one build id, or refuses to answer.

    It answers by path, because the code asks it two different questions and a
    stub that returned the same body to both would let a case pass on a reading
    the real board would never have given.
    """

    def __init__(self, build_id: str | None, *, migrated: bool = True) -> None:
        self.build_id = build_id
        self.migrated = migrated
        self.reads = 0

    def __call__(self, url: str):
        if url.endswith("/api/workflow"):
            body = {"document": {"roles": [], "migrations": {"director_onboarding": self.migrated}}}
        else:
            self.reads += 1
            if self.build_id is None:
                raise OSError("connection refused")
            body = {"build_id": self.build_id, "tickets": []}
        payload = json.dumps(body).encode("utf-8")
        board = self

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self):
                return payload

        del board
        return _Response()


class Tenant:
    """One disposable tenant: a config, a board root, and root's sandbox."""

    def __init__(self, tmp: Path, *, deployed: str | None = DEPLOYED) -> None:
        self.tmp = tmp
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(tmp / "etc-switchyard")
        os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = str(tmp / "opt-switchyard")
        shared = tmp / "opt-switchyard" / "releases" / SHARED
        shared.mkdir(parents=True, exist_ok=True)
        (shared / team_launcher.SWITCHYARD_RELEASE_MARKER_NAME).write_text(
            json.dumps({"commit": SHARED, "source_ref": SHARED, "source_repo": str(shared)}),
            encoding="utf-8",
        )
        current = tmp / "opt-switchyard" / "current"
        if not current.exists():
            current.symlink_to(shared)

        self.board_root = tmp / "board"
        if deployed is not None:
            release = self.board_root / "releases" / deployed
            release.mkdir(parents=True, exist_ok=True)
            (release / ".pgu-deploy-sha").write_text(deployed + "\n", encoding="utf-8")
            (self.board_root / "current").symlink_to(release)
        else:
            self.board_root.mkdir(parents=True, exist_ok=True)

        provision = tmp / "tenant" / ".switchyard" / "provision"
        provision.mkdir(parents=True, exist_ok=True)
        layout = provision / "layout.json"
        layout.write_text(json.dumps({"Orientation": "Horizontal", "Widgets": []}), encoding="utf-8")
        (provision / "plan.json").write_text(
            json.dumps(
                {
                    "project": PROJECT,
                    "board_root": str(self.board_root),
                    "owner_user": team_launcher.current_user_name(),
                    "owner_home": str(tmp / "tenant"),
                }
            ),
            encoding="utf-8",
        )
        self.config_path = provision / f"{PROJECT}.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "project": PROJECT,
                    "ticket_prefix": "PRT",
                    "layout": str(layout),
                    "repository": str(tmp / "tenant"),
                    "run_as_user": team_launcher.current_user_name(),
                    "session_dir": str(tmp / "state" / "sessions"),
                    "board_url": "http://127.0.0.1:29117",
                    "board_socket": str(tmp / "board.sock"),
                    "roles": [
                        {
                            "role": "director",
                            "slot": 0,
                            "target": f"{PROJECT}-director:0.0",
                            "tmux_session": f"{PROJECT}-director",
                            "workdir": str(tmp / "tenant"),
                            "cli": ["codex"],
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.config = team_launcher.load_project_config(PROJECT, self.config_path)

    def declare_workflow(self) -> None:
        """Give the tenant a control role, which `finish-upgrade` requires."""
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        payload["workflow"] = {
            "roles": [
                {
                    "name": "director",
                    "active": True,
                    "capabilities": ["add_comment", "set_manually_controlled", "merge", "set_blockers"],
                }
            ],
            "migrations": {"director_onboarding": True},
        }
        self.config_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.config = team_launcher.load_project_config(PROJECT, self.config_path)

    def pin(self, commit: str) -> None:
        """Root's record of the release this upgrade was pinned to."""
        path = team_launcher.privileged_upgrade_source_path(self.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": team_launcher.UPGRADE_SOURCE_SCHEMA,
                    "project": PROJECT,
                    "source_repo": str(self.tmp),
                    "commit_git_dir": "",
                    "deploy_ref": commit,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o600)

    def trusted(self) -> dict:
        return team_launcher.read_upgrade_journal(
            self.config, config_path=self.config_path, trusted=True
        )

    def tenant(self) -> dict:
        return team_launcher.read_upgrade_journal(self.config, config_path=self.config_path)

    def trusted_release(self) -> str:
        return team_launcher.upgrade_phase_state(self.trusted(), "release")

    def tenant_release(self) -> str:
        return team_launcher.upgrade_phase_state(self.tenant(), "release")


def as_root(function, *args, **kwargs):
    """Run one call with the code believing it is root.

    The privileged provision root is already a sandbox; what is being modelled
    here is the branch, not the uid, and the ownership calls inside it are
    already tolerant of a host that cannot set them.
    """
    original = team_launcher.os.geteuid
    try:
        team_launcher.os.geteuid = lambda: 0
        return function(*args, **kwargs)
    finally:
        team_launcher.os.geteuid = original


def tenant_in(tmp: str, **kwargs) -> Tenant:
    return Tenant(Path(tmp), **kwargs)


# --------------------------------------------------------------------------
# 1. A successful deployment closes the phase, and says what it proved
# --------------------------------------------------------------------------


def test_closing_records_done_against_the_build_the_board_reports() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd117-done.") as tmp:
        tenant = tenant_in(tmp)
        tenant.pin(DEPLOYED)
        board = FakeBoard(DEPLOYED)
        printed: list[str] = []
        result = as_root(
            team_launcher.close_release_phase,
            tenant.config,
            config_path=tenant.config_path,
            opener=board,
            print_func=printed.append,
        )
        output = "\n".join(printed)
        check(result == 0, output)
        check(tenant.trusted_release() == "done", f"root's journal closes it: {tenant.trusted()}")
        detail = tenant.trusted()["phases"]["release"]["detail"]
        check(DEPLOYED in detail and "live board build" in detail, detail)
        check(board.reads >= 1, "and it asked the running board rather than a record")
        check(
            "deployed, restarted or rolled back" in output,
            f"and says what it did not do: {output}",
        )
        # The tenant copy is republished from root's, so both now agree.
        check(tenant.tenant_release() == "done", f"the tenant copy follows: {tenant.tenant()}")


# --------------------------------------------------------------------------
# 2. A board that is not serving what was deployed does not close it
# --------------------------------------------------------------------------


def test_a_board_serving_another_build_is_refused_with_the_reason() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd117-mismatch.") as tmp:
        tenant = tenant_in(tmp)
        printed: list[str] = []
        result = as_root(
            team_launcher.close_release_phase,
            tenant.config,
            config_path=tenant.config_path,
            opener=FakeBoard(OTHER),
            print_func=printed.append,
        )
        output = "\n".join(printed)
        check(result == 1, output)
        check("not serving what was deployed" in output, output)
        check(
            tenant.trusted_release() == "blocked",
            f"and the refusal is recorded: {tenant.trusted()}",
        )
        detail = tenant.trusted()["phases"]["release"]["detail"]
        check(OTHER in detail, "with the build it actually found: " + detail)


def test_a_board_that_cannot_be_read_does_not_close_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd117-unreachable.") as tmp:
        tenant = tenant_in(tmp)
        printed: list[str] = []
        result = as_root(
            team_launcher.close_release_phase,
            tenant.config,
            config_path=tenant.config_path,
            opener=FakeBoard(None),
            print_func=printed.append,
        )
        output = "\n".join(printed)
        check(result == 1, output)
        check("did not report a build id" in output, output)
        check(tenant.trusted_release() == "blocked", tenant.trusted())


def test_a_deploy_that_landed_on_another_release_than_the_pin_is_refused() -> None:
    # The rollback case as the machine shows it afterwards: the board came back
    # up on the release it had before, so what is deployed is not what this
    # upgrade was pinned to. Closing here would claim a deploy that was undone.
    with tempfile.TemporaryDirectory(prefix="syrd117-rollback.") as tmp:
        tenant = tenant_in(tmp)
        tenant.pin(OTHER)
        printed: list[str] = []
        result = as_root(
            team_launcher.close_release_phase,
            tenant.config,
            config_path=tenant.config_path,
            opener=FakeBoard(DEPLOYED),
            print_func=printed.append,
        )
        output = "\n".join(printed)
        check(result == 1, output)
        check("pinned to" in output and "did not happen" in output, output)
        check(tenant.trusted_release() == "blocked", tenant.trusted())


def test_a_tenant_with_nothing_deployed_is_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd117-nothing.") as tmp:
        tenant = tenant_in(tmp, deployed=None)
        printed: list[str] = []
        result = as_root(
            team_launcher.close_release_phase,
            tenant.config,
            config_path=tenant.config_path,
            opener=FakeBoard(DEPLOYED),
            print_func=printed.append,
        )
        output = "\n".join(printed)
        check(result == 1, output)
        check("names no deployed release" in output, output)


# --------------------------------------------------------------------------
# 3. The privilege boundary
# --------------------------------------------------------------------------


def test_an_unprivileged_close_writes_nothing_and_names_who_can() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd117-unprivileged.") as tmp:
        tenant = tenant_in(tmp)
        tenant.pin(DEPLOYED)
        board = FakeBoard(DEPLOYED)
        printed: list[str] = []
        result = team_launcher.close_release_phase(
            tenant.config, config_path=tenant.config_path, opener=board, print_func=printed.append
        )
        output = "\n".join(printed)
        check(result == 1, output)
        check(tenant.trusted_release() == "", f"nothing was recorded: {tenant.trusted()}")
        check(board.reads == 0, "and the board was not even asked, because it could not matter")
        check("release-status" in output and "--close" in output, output)


def test_an_unprivileged_phase_write_is_an_observation_not_a_phase() -> None:
    # The exact SYRD-102 mechanism. `finish-upgrade` is unprivileged by design,
    # so the phase it recorded went into the tenant copy alone and became the
    # only record claiming the deployment had finished.
    with tempfile.TemporaryDirectory(prefix="syrd117-observation.") as tmp:
        tenant = tenant_in(tmp)
        team_launcher.record_upgrade_phase(
            tenant.config,
            config_path=tenant.config_path,
            phase="release",
            state="done",
            detail="the director saw a deployed board",
        )
        journal = tenant.tenant()
    check(
        team_launcher.upgrade_phase_state(journal, "release") == "",
        f"an unprivileged caller writes no phase: {journal}",
    )
    observation = team_launcher.upgrade_phase_observation(journal, "release")
    check(observation.get("state") == "done", f"it writes an observation: {observation}")
    check(
        observation.get("observed_by") == team_launcher.current_user_name(),
        f"naming who saw it: {observation}",
    )
    check(tenant.trusted_release() == "", "and root's journal is untouched")


def test_a_forged_tenant_completion_is_not_a_completion() -> None:
    # The tenant owns its own copy and can write anything into it. What stops a
    # forgery is that nothing reads it to decide, and that root's next write
    # republishes the file from root's own record.
    with tempfile.TemporaryDirectory(prefix="syrd117-forged.") as tmp:
        tenant = tenant_in(tmp)
        tenant.pin(DEPLOYED)
        forged = tenant.tenant()
        forged["phases"]["release"] = {"state": "done", "owner": "operator", "at": "", "detail": "forged"}
        path = team_launcher.upgrade_journal_path(tenant.config, config_path=tenant.config_path)
        path.write_text(json.dumps(forged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        check(tenant.tenant_release() == "done", "the forgery is in the tenant file")
        check(tenant.trusted_release() == "", "and root's journal still says nothing")

        alignment = as_root(
            team_launcher.release_alignment,
            tenant.config,
            config_path=tenant.config_path,
            opener=FakeBoard(OTHER),
        )
        check(alignment.trusted_release_state == "", "the trusted reading ignores it")
        check(alignment.diverged, "and the divergence is reported rather than resolved in its favour")
        check(
            bool(alignment.close_refusals()),
            "and it does not let the phase be closed: " + str(alignment.close_refusals()),
        )

        # Root's next write republishes the file from root's own record, so the
        # forgery does not survive the first authoritative statement about it.
        as_root(
            team_launcher.record_upgrade_phase,
            tenant.config,
            config_path=tenant.config_path,
            phase="release",
            state="ready",
            detail="",
        )
        check(tenant.tenant_release() == "ready", f"the projection replaces it: {tenant.tenant()}")


def test_the_projection_carries_roots_phases_and_only_roots_phases() -> None:
    # The forgery that matters most is one about a phase root has never spoken
    # on: nothing overwrites it entry-by-entry, so the projection has to be a
    # replacement rather than a merge, or a tenant's claim outlives every
    # authoritative write that does not happen to name the same phase.
    with tempfile.TemporaryDirectory(prefix="syrd117-projection.") as tmp:
        tenant = tenant_in(tmp)
        forged = tenant.tenant()
        forged["phases"]["accounts"] = {"state": "done", "owner": "operator", "at": "", "detail": "forged"}
        forged["phases"]["release"] = {"state": "done", "owner": "operator", "at": "", "detail": "forged"}
        team_launcher.upgrade_journal_path(tenant.config, config_path=tenant.config_path).write_text(
            json.dumps(forged, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        # Root speaks about ONE phase. The other forged phase must not survive
        # it just because root said nothing about that one.
        as_root(
            team_launcher.record_upgrade_phase,
            tenant.config,
            config_path=tenant.config_path,
            phase="release",
            state="ready",
            detail="",
        )
        projected = tenant.tenant()
        check(
            set(projected["phases"]) == {"release"},
            f"only what root wrote survives: {projected['phases']}",
        )
        check(
            team_launcher.upgrade_phase_state(projected, "accounts") == "",
            f"the forged phase root never wrote is gone: {projected['phases']}",
        )
        check(projected["phases_written_by"] == "root", projected)


def test_an_observation_answered_by_root_is_dropped() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd117-answered.") as tmp:
        tenant = tenant_in(tmp)
        # The director observes the release phase, then root records it.
        team_launcher.record_upgrade_phase(
            tenant.config, config_path=tenant.config_path,
            phase="release", state="done", detail="saw a deployed board",
        )
        team_launcher.record_upgrade_phase(
            tenant.config, config_path=tenant.config_path,
            phase="director", state="done", detail="migrated",
        )
        check(
            team_launcher.upgrade_phase_observation(tenant.tenant(), "release").get("state") == "done",
            "the observation is there first",
        )
        as_root(
            team_launcher.record_upgrade_phase,
            tenant.config,
            config_path=tenant.config_path,
            phase="release",
            state="ready",
            detail="",
        )
        journal = tenant.tenant()
    check(
        not team_launcher.upgrade_phase_observation(journal, "release"),
        f"root answered it, so the note goes: {journal}",
    )
    check(
        team_launcher.upgrade_phase_observation(journal, "director").get("state") == "done",
        f"and a note about a phase root has not spoken on stays: {journal}",
    )


# --------------------------------------------------------------------------
# 4. The read-only comparison
# --------------------------------------------------------------------------


def test_the_status_view_compares_all_four_records() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd117-status.") as tmp:
        tenant = tenant_in(tmp)
        tenant.pin(DEPLOYED)
        alignment = as_root(
            team_launcher.release_alignment,
            tenant.config,
            config_path=tenant.config_path,
            opener=FakeBoard(DEPLOYED),
        )
        rendered = "\n".join(team_launcher.format_release_alignment(alignment))
    check(alignment.shared_release == SHARED, alignment.shared_release)
    check(alignment.deployed_release == DEPLOYED, alignment.deployed_release)
    check(alignment.live_build == DEPLOYED, alignment.live_build)
    check(alignment.pinned_release == DEPLOYED, alignment.pinned_release)
    for label in ("shared release", "deployed release", "live board build", "trusted journal", "tenant journal"):
        check(label in rendered, f"{label!r} is in the view: {rendered}")
    check(
        alignment.diverged,
        "a deployed board whose phase is unrecorded is a divergence, not a quiet success",
    )
    check("--close" in rendered, "and the view names the action that resolves it: " + rendered)


def test_the_status_view_is_honest_when_it_cannot_read_roots_journal() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd117-unreadable.") as tmp:
        tenant = tenant_in(tmp)
        trusted_path = team_launcher.privileged_upgrade_journal_path(tenant.config)
        trusted_path.parent.mkdir(parents=True, exist_ok=True)
        trusted_path.write_text("{}", encoding="utf-8")
        os.chmod(trusted_path, 0o000)
        try:
            alignment = team_launcher.release_alignment(
                tenant.config, config_path=tenant.config_path, opener=FakeBoard(DEPLOYED)
            )
            rendered = "\n".join(team_launcher.format_release_alignment(alignment))
        finally:
            os.chmod(trusted_path, 0o600)
    check(not alignment.trusted_readable, "it knows it could not read root's journal")
    check("not readable from this account" in rendered, rendered)
    check(
        not alignment.diverged,
        "and it does not call a reading it could not make a divergence",
    )


# --------------------------------------------------------------------------
# 5. Preparation-only success, and the director's observation
# --------------------------------------------------------------------------


def test_preparation_only_success_says_what_exit_zero_means() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd117-prepared.") as tmp:
        tenant = tenant_in(tmp)
        as_root(
            team_launcher.record_upgrade_phase,
            tenant.config,
            config_path=tenant.config_path,
            phase="release",
            state="ready",
            detail="",
        )
        lines = team_launcher.outstanding_release_phase_report(
            tenant.config, config_path=tenant.config_path, journal=tenant.trusted()
        )
    rendered = "\n".join(lines)
    check("exit 0 here means" in rendered, rendered)
    check("artifacts are prepared" in rendered, rendered)
    check("release phase is ready" in rendered, rendered)
    check(
        f"pkexec switchyard release-status {PROJECT} --close" in rendered,
        "and names the exact next command: " + rendered,
    )


def test_a_closed_phase_reports_the_deployment_rather_than_a_next_step() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd117-closed.") as tmp:
        tenant = tenant_in(tmp)
        as_root(
            team_launcher.record_upgrade_phase,
            tenant.config,
            config_path=tenant.config_path,
            phase="release",
            state="done",
            detail="",
        )
        rendered = "\n".join(
            team_launcher.outstanding_release_phase_report(
                tenant.config, config_path=tenant.config_path, journal=tenant.trusted()
            )
        )
    check("release phase is closed" in rendered, rendered)
    check("--close" not in rendered, "and asks for nothing further: " + rendered)


def test_the_director_reports_divergence_and_never_claims_to_have_closed_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd117-director.") as tmp:
        tenant = tenant_in(tmp)
        tenant.pin(DEPLOYED)
        as_root(
            team_launcher.record_upgrade_phase,
            tenant.config,
            config_path=tenant.config_path,
            phase="release",
            state="ready",
            detail="",
        )
        rendered = "\n".join(
            team_launcher.director_release_divergence_report(
                tenant.config, config_path=tenant.config_path, opener=FakeBoard(DEPLOYED)
            )
        )
    check("does not close" in rendered, rendered)
    check("observation beside the phases" in rendered, rendered)
    check("recorded ready" in rendered, "it names the authoritative state: " + rendered)
    check(f"release-status {PROJECT} --close" in rendered, "and the supported action: " + rendered)


def test_finish_upgrade_prints_the_divergence_and_claims_no_closure() -> None:
    """The real command, not the helper: the report has to reach the director.

    A helper nobody calls is the same defect wearing a better name, and the
    reported symptom was precisely an operator reading a command's output and
    believing it (SYRD-117).
    """
    with tempfile.TemporaryDirectory(prefix="syrd117-finish.") as tmp:
        tenant = tenant_in(tmp)
        tenant.declare_workflow()
        tenant.pin(DEPLOYED)
        as_root(
            team_launcher.record_upgrade_phase,
            tenant.config,
            config_path=tenant.config_path,
            phase="release",
            state="ready",
            detail="",
        )
        printed: list[str] = []
        original_opener = team_launcher._open_board_url
        original_migrate = team_launcher.migrate_declarative_director_onboarding
        try:
            team_launcher._open_board_url = FakeBoard(DEPLOYED)
            team_launcher.migrate_declarative_director_onboarding = lambda config, **kwargs: True
            team_launcher.finish_upgrade_command(
                tenant.config,
                config_path=tenant.config_path,
                runner=lambda *args, **kwargs: _completed(args),
                print_func=printed.append,
            )
        finally:
            team_launcher._open_board_url = original_opener
            team_launcher.migrate_declarative_director_onboarding = original_migrate
        output = "\n".join(printed)
        check("does not close" in output, f"it disclaims the closure: {output}")
        check(
            f"release-status {PROJECT} --close" in output,
            f"and names who can and how: {output}",
        )
        check(
            tenant.trusted_release() == "ready",
            f"and root's journal is where it was: {tenant.trusted()}",
        )
        check(
            team_launcher.upgrade_phase_observation(tenant.tenant(), "release").get("state") != "",
            f"what it saw is an observation: {tenant.tenant()}",
        )


def _completed(args):
    import subprocess

    return subprocess.CompletedProcess(list(args), 0, "", "")


def test_the_director_says_nothing_is_outstanding_when_the_phase_is_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd117-director-done.") as tmp:
        tenant = tenant_in(tmp)
        tenant.pin(DEPLOYED)
        as_root(
            team_launcher.record_upgrade_phase,
            tenant.config,
            config_path=tenant.config_path,
            phase="release",
            state="done",
            detail="",
        )
        rendered = "\n".join(
            team_launcher.director_release_divergence_report(
                tenant.config, config_path=tenant.config_path, opener=FakeBoard(DEPLOYED)
            )
        )
    check("nothing is outstanding" in rendered, rendered)
    check("--close" not in rendered, rendered)


# --------------------------------------------------------------------------
# 6. Legacy divergence: a deployed release left `ready` by an older Switchyard
# --------------------------------------------------------------------------


def test_a_legacy_ready_over_a_matching_deployment_reconciles_without_redeploying() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd117-legacy.") as tmp:
        tenant = tenant_in(tmp)
        tenant.pin(DEPLOYED)
        # The state this ticket found on a live host: the board is deployed and
        # serving, and the phase was never closed because nothing ever closed it.
        as_root(
            team_launcher.record_upgrade_phase,
            tenant.config,
            config_path=tenant.config_path,
            phase="release",
            state="ready",
            detail="",
        )
        before = sorted(path.name for path in (tenant.board_root / "releases").iterdir())
        current_before = os.readlink(tenant.board_root / "current")

        printed: list[str] = []
        result = as_root(
            team_launcher.close_release_phase,
            tenant.config,
            config_path=tenant.config_path,
            opener=FakeBoard(DEPLOYED),
            print_func=printed.append,
        )
        after = sorted(path.name for path in (tenant.board_root / "releases").iterdir())
        check(result == 0, "\n".join(printed))
        check(tenant.trusted_release() == "done", tenant.trusted())
        check(before == after, f"no release was added or removed: {before} -> {after}")
        check(
            current_before == os.readlink(tenant.board_root / "current"),
            "and the deployed release was not switched",
        )


# --------------------------------------------------------------------------
# 7. The command surface
# --------------------------------------------------------------------------


def test_the_command_is_registered_and_says_what_it_does_not_do() -> None:
    check("release-status" in team_launcher.SWITCHYARD_COMMANDS, "the command is registered")
    check("release-status" in team_launcher.switchyard_help_text(), "and listed in the help")
    help_text = " ".join(
        team_launcher._build_switchyard_release_status_parser().format_help().split()
    )
    for promised in ("Reads only", "deploys nothing, restarts nothing and rolls back nothing"):
        check(promised in help_text, f"the help promises it: {promised!r} in {help_text}")
    parsed = team_launcher._build_switchyard_release_status_parser().parse_args([PROJECT])
    check(parsed.close is False, "and the bare command is the read-only one")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"release_phase_journal_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
