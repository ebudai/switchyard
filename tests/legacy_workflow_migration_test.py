#!/usr/bin/env python3
"""SYRD-240: a legacy upgrade must not declare the Director migration unnecessary.

The live evidence, from the restored MEFP Director pane after an upgrade and a
clean role restart: identity restored, `/api/workflow` still `null`, the pane
still on provisioning-scaffold onboarding, and the upgrade having reported the
Director phase as *not required*.

The last of those is the defect. `director_phase_required` asked the tenant's
own launcher config whether it carried a `workflow` key and answered "no" for
every tenant provisioned before declarative workflows existed -- so the board
was never asked, and the "pending" answer three lines further down, which was
already correct, was never reached.

The three tenant shapes the ticket names appear here by name: an already
migrated tenant, a legacy tenant with a null workflow, and a partially
migrated tenant whose scaffold remains.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

# Root's provision directory is overridable precisely so a suite can exercise
# the real branch without reading the host's /etc. Set before the import that
# reads it, and pointed at a directory that does not exist, so "root holds no
# recorded workflow" is a real answer rather than a mocked one.
os.environ.setdefault(
    "SWITCHYARD_PRIVILEGED_PROVISION_ROOT", "/nonexistent/switchyard-syrd240"
)

import team_launcher as tl  # noqa: E402

CHECKS = 0

def _example_document(project: str = "porter") -> dict:
    """A real declared workflow: the example this repository ships, re-projected.

    Deliberately not a hand-built stub. `plan_workflow_migration` runs the real
    `workflow_config.validate`, so a document invented for the test would prove
    the validator rejects inventions rather than proving anything about this
    migration. The same helper `adopt_workflow_test` uses, for the same reason.
    """
    raw = json.loads((ROOT / "examples" / "workflows" / "inspection.json").read_text())
    source = str(raw.get("project") or "")
    document = json.loads(json.dumps(raw).replace(f'"{source}-', f'"{project}-'))
    document["project"] = project
    return document


MIGRATED_DOCUMENT = {**_example_document(), "migrations": {"director_onboarding": True}}
UNMIGRATED_DOCUMENT = {**_example_document(), "migrations": {}}


def as_the_board_stores_it(document: dict) -> dict:
    """What a board actually holds: the document after validation, not before.

    `apply_declared_workflow` stores what `validate` returned, and validation
    fills defaults -- so comparing a raw fixture with a live document would
    report a difference that no real board could ever have. The migration
    compares digests, so this distinction is the whole of its idempotence.
    """
    from ticket_board.workflow_config import validate

    return validate(json.loads(json.dumps(document)), project="porter")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Tenant:
    """A launcher config on disk, with the plan beside it the real code reads."""

    def __init__(self, raw: str, *, workflow=None, plan_workflow=None, seed=""):
        self.root = Path(raw)
        (self.root / "d").mkdir(exist_ok=True)
        (self.root / "m").mkdir(exist_ok=True)
        payload = {
            "project": "porter",
            "board_url": "http://127.0.0.1:1/",
            "roles": [
                {"role": "director", "cli": "claude", "slot": 0, "workdir": str(self.root / "d")},
                {"role": "main", "cli": "claude", "slot": 1, "workdir": str(self.root / "m")},
            ],
        }
        if workflow is not None:
            payload["workflow"] = workflow
        self.config_path = self.root / "porter.json"
        self.config_path.write_text(json.dumps(payload), encoding="utf-8")
        plan: dict = {"project": "porter"}
        if plan_workflow is not None:
            plan["workflow"] = plan_workflow
        if seed:
            plan["workflow_seed"] = seed
        (self.root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        self.config = tl.load_project_config("porter", self.config_path)

    def presence(self, board):
        return tl.declared_workflow_presence(
            self.config, config_path=self.config_path, board_reader=board
        )


def legacy_board(_config):
    """What `read_board_declared_workflow` returns for the live mefp board."""
    return None, "the board is running no declared workflow"


def migrated_board(_config):
    return dict(MIGRATED_DOCUMENT), ""


def unreachable_board(_config):
    return None, "the board's workflow could not be read: [Errno 111] Connection refused"


class root_holds:
    """Make `recorded_declared_workflow` answer with a document, for one block.

    Root's real record lives under a directory this suite cannot write, and
    the point of these cases is what the migration does with a document root
    vouches for -- not how it got there, which `adopt_workflow_test` covers.
    """

    def __init__(self, document: dict) -> None:
        self.document = document

    def __enter__(self):
        self.previous = tl.recorded_declared_workflow
        tl.recorded_declared_workflow = lambda project: (json.loads(json.dumps(self.document)), "")
        return self

    def __exit__(self, *exc):
        tl.recorded_declared_workflow = self.previous
        return False


def tenant(**kwargs):
    class _Ctx:
        def __enter__(self):
            self.tmp = tempfile.TemporaryDirectory(prefix="syrd240.")
            return Tenant(self.tmp.__enter__(), **kwargs)

        def __exit__(self, *exc):
            return self.tmp.__exit__(*exc)

    return _Ctx()


# -- the three tenant shapes the ticket names --------------------------------


def test_a_legacy_tenant_with_a_null_workflow_is_not_declared_exempt() -> None:
    """The regression itself, in one case.

    Nothing declares a workflow for this tenant -- not its config, not root,
    not its plan -- which is exactly what "provisioned before declarative
    workflows" means. That used to read as "this tenant has no Director
    phase". It now reads as a tenant whose board is running none.
    """
    with tenant() as t:
        presence = t.presence(legacy_board)
        check(not presence.config_declares, "its local config carries no workflow")
        check(not presence.root_records, "and root holds no record")
        check(not presence.plan_declares, "and its plan declares none")
        check(presence.board_runs_none, "and the board is running none")
        check(
            presence.legacy_without_workflow,
            "so it is the legacy condition this ticket is about",
        )
        check(
            tl.director_phase_required(
                t.config, config_path=t.config_path, presence=presence
            ),
            "the Director phase is required, not 'not required'",
        )


def test_an_already_migrated_tenant_is_done_and_not_migrated_again() -> None:
    with tenant(workflow=MIGRATED_DOCUMENT) as t:
        presence = t.presence(migrated_board)
        check(presence.board_document, "the board runs a document")
        check(not presence.legacy_without_workflow, "so nothing is legacy about it")
        state, reason = tl.director_onboarding_state(
            t.config,
            config_path=t.config_path,
            presence=presence,
            opener=_opener({"document": MIGRATED_DOCUMENT}),
        )
        check(state == "done", f"{state}: {reason}")


def test_a_partially_migrated_tenant_whose_scaffold_remains_is_pending() -> None:
    """The board carries a document, but not the migration marker.

    This is the tenant whose workflow landed and whose Director is still being
    served the scaffold, because the pane hook's scaffold branch is gated on
    the marker rather than on the document.
    """
    with tenant(workflow=UNMIGRATED_DOCUMENT) as t:
        presence = t.presence(lambda _c: (dict(UNMIGRATED_DOCUMENT), ""))
        check(
            tl.director_phase_required(
                t.config, config_path=t.config_path, presence=presence
            ),
            "the phase applies",
        )
        state, reason = tl.director_onboarding_state(
            t.config,
            config_path=t.config_path,
            presence=presence,
            opener=_opener({"document": UNMIGRATED_DOCUMENT}),
        )
        check(state == "pending", f"{state}: {reason}")
        check("migration marker" in reason, reason)

    # And the mirror of it: the board carries the marker, the local projection
    # does not, so a restarted pane would still read the scaffold env.
    with tenant(workflow=UNMIGRATED_DOCUMENT) as t:
        presence = t.presence(migrated_board)
        state, reason = tl.director_onboarding_state(
            t.config,
            config_path=t.config_path,
            presence=presence,
            opener=_opener({"document": MIGRATED_DOCUMENT}),
        )
        check(state == "pending", f"{state}: {reason}")
        check("local projection" in reason, reason)


def _opener(payload):
    """A stand-in for `_open_board_url`, which its caller passes to `json.load`."""
    import io

    def open_url(_url):
        return io.StringIO(json.dumps(payload))

    return open_url


# -- what must not regress ---------------------------------------------------


def test_a_tenant_that_declares_no_workflow_by_design_stays_exempt() -> None:
    """`pgu` keeps the workflow seeded by schema.sql and declares none.

    It has nothing to adopt and no scaffold to migrate away from, so the
    detection must leave it alone. Without this, the fix for a legacy tenant
    would invent a Director phase for a tenant that correctly has none.
    """
    with tenant(seed=tl.NON_DECLARATIVE_WORKFLOW_SEED) as t:
        presence = t.presence(legacy_board)
        check(presence.non_declarative_by_design, "the plan says so")
        check(
            not presence.legacy_without_workflow,
            "so a board with no document is its design, not its regression",
        )
        check(
            not tl.director_phase_required(
                t.config, config_path=t.config_path, presence=presence
            ),
            "and the Director phase stays not required",
        )


def test_an_unreachable_board_is_unknown_rather_than_legacy() -> None:
    """Reporting "cannot ask" as "no workflow" would turn a blip into a migration."""
    with tenant() as t:
        presence = t.presence(unreachable_board)
        check(not presence.board_runs_none, "a board that could not be read said nothing")
        check(not presence.legacy_without_workflow, "so no migration is inferred from it")


def test_an_unreadable_config_is_not_an_exempt_tenant() -> None:
    """The `except SystemExit: return False` this replaces.

    An unreadable config used to give exactly the same answer as a tenant with
    no Director phase -- the same wrong answer for a completely different
    reason, and one nobody could act on.
    """
    with tenant() as t:
        t.config_path.write_text("{ this is not json", encoding="utf-8")
        presence = t.presence(migrated_board)
        check(presence.config_unreadable != "", f"it is reported: {presence.config_unreadable}")
        check(
            tl.director_phase_required(
                t.config, config_path=t.config_path, presence=presence
            ),
            "and it does not read as exempt",
        )
        state, reason = tl.director_onboarding_state(
            t.config,
            config_path=t.config_path,
            presence=presence,
            opener=_opener({"document": MIGRATED_DOCUMENT}),
        )
        check(state == "unknown", f"{state}: {reason}")
        check("could not be read" in reason, reason)

    # And the case where the unreadable config is the ONLY thing that can say
    # so: nothing declares a workflow, and the board cannot be asked either.
    # "We cannot tell" must not collapse into "this tenant needs nothing".
    with tenant() as t:
        t.config_path.write_text("{ this is not json", encoding="utf-8")
        presence = t.presence(unreachable_board)
        check(not presence.declared_somewhere, "no source declares a workflow")
        check(not presence.legacy_without_workflow, "and the board said nothing either")
        check(
            tl.director_phase_required(
                t.config, config_path=t.config_path, presence=presence
            ),
            "the unreadable config alone keeps the phase required",
        )


# -- the release phase must not close over it --------------------------------


def test_the_release_phase_refuses_to_close_over_a_board_with_no_workflow() -> None:
    """Every other check can pass and this still be wrong.

    The release can be deployed, the board can be serving exactly what was
    deployed, and the tenant can still have no declared workflow at all --
    which is how mefp reached "upgraded, done" with its Director on the
    scaffold.
    """
    aligned = tl.ReleaseAlignment(
        project="porter",
        deployed_release="a" * 40,
        live_build="a" * 40,
        board_runs_declared_workflow=False,
        legacy_without_workflow=True,
    )
    refusals = aligned.close_refusals()
    check(bool(refusals), "it refuses")
    joined = " ".join(refusals)
    check("no declared workflow" in joined, joined)
    check("provisioning-scaffold onboarding" in joined, f"and says why it matters: {joined}")
    check("migrate-workflow porter" in joined, f"and what to run: {joined}")

    # The same alignment with a workflow installed has nothing to say.
    fine = tl.ReleaseAlignment(
        project="porter",
        deployed_release="a" * 40,
        live_build="a" * 40,
        board_runs_declared_workflow=True,
    )
    check(fine.close_refusals() == [], f"a migrated tenant closes: {fine.close_refusals()}")


def test_the_release_report_names_the_missing_workflow_as_a_fact() -> None:
    """One line per fact is this report's whole design; the workflow is a fact."""
    lines = tl.format_release_alignment(
        tl.ReleaseAlignment(
            project="porter",
            deployed_release="a" * 40,
            live_build="a" * 40,
            board_runs_declared_workflow=False,
            legacy_without_workflow=True,
        )
    )
    body = "\n".join(lines)
    check("declared workflow" in body, body)
    check("NONE" in body, f"and it is visible rather than summarised: {body}")


# -- the bounded migration ---------------------------------------------------


def test_the_migration_refuses_when_root_vouches_for_no_document() -> None:
    """It installs only what an operator already decided, never the tenant's copy.

    The tenant's `plan.json` is the file the account every role runs as can
    write, and the document decides which roles exist and what each may call.
    Adopting it is `adopt-workflow`, which is operator-authorized and
    journalled; this installs the result of that decision and nothing else.
    """
    with tenant(plan_workflow=MIGRATED_DOCUMENT) as t:
        migration = tl.plan_workflow_migration(
            t.config,
            config_path=t.config_path,
            board_reader=lambda _c: (0, None, ""),
        )
        check(not migration.installable, "it refuses")
        joined = " ".join(migration.problems)
        check("root holds no declared workflow" in joined, joined)
        check("adopt-workflow porter" in joined, f"and names the decision: {joined}")
        check("--despite-board" in joined, f"including the legacy form of it: {joined}")


class provision_root:
    """Point root's provision root at a scratch directory, for one block.

    The handoff lives beside it. Through this documented seam, "root's" files
    are the caller's own (expected_privileged_uid), so both halves run here.
    """

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="syrd253-root.")
        self.previous = os.environ.get("SWITCHYARD_PRIVILEGED_PROVISION_ROOT")
        root = Path(self.tmp.name) / "provision"
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(root)
        return root

    def __exit__(self, *exc):
        if self.previous is None:
            os.environ.pop("SWITCHYARD_PRIVILEGED_PROVISION_ROOT", None)
        else:
            os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = self.previous
        self.tmp.cleanup()
        return False


def migrate(t, *, apply, board, euid=0):
    printed: list[str] = []
    with root_holds(MIGRATED_DOCUMENT):
        code = tl.switchyard_migrate_workflow_command(
            "porter", apply=apply, config_path=t.config_path, board_reader=board,
            euid_getter=lambda: euid, print_func=printed.append,
        )
    return code, "\n".join(printed)


def test_the_migration_is_a_no_op_when_the_board_already_runs_it() -> None:
    """Idempotence, at the layer that decides whether to do anything at all."""
    with tenant() as t, provision_root():
        stored = as_the_board_stores_it(MIGRATED_DOCUMENT)
        with root_holds(MIGRATED_DOCUMENT):
            migration = tl.plan_workflow_migration(
                t.config, config_path=t.config_path,
                board_reader=lambda _c: (7, dict(stored), ""),
            )
        check(migration.already_installed, "it recognises the document is already in force")
        check(not migration.installable, "so there is nothing to install")
        code, report = migrate(t, apply=True, board=lambda _c: (7, dict(stored), ""))
        check(code == 0, f"a rerun succeeds without doing anything: {code}")
        check("already running exactly this workflow" in report, report)
        check(not tl.workflow_handoff_path("porter").exists(), "and hands nothing over")


def test_the_migration_will_not_overwrite_a_different_live_workflow() -> None:
    """This installs onto a board that has none. Changing one is a different verb."""
    with tenant() as t:
        with root_holds(MIGRATED_DOCUMENT):
            migration = tl.plan_workflow_migration(
                t.config,
                config_path=t.config_path,
                board_reader=lambda _c: (3, as_the_board_stores_it(UNMIGRATED_DOCUMENT), ""),
            )
        check(not migration.installable, "it refuses")
        joined = " ".join(migration.problems)
        check("already running a declared workflow" in joined, joined)


def test_a_dry_run_reports_what_would_be_installed_and_writes_nothing() -> None:
    with tenant() as t, provision_root():
        code, report = migrate(t, apply=False, board=lambda _c: (0, None, ""))
        check(code == 0, f"{code}: {report}")
        check(not tl.workflow_handoff_path("porter").exists(), "nothing was handed over")
        check("running NONE" in report, f"the board's state: {report}")
        check("source" in report, f"where the document came from: {report}")
        check("not reversible" in report, f"and the one-way door is stated: {report}")
        check("--apply" in report, f"and how to do it: {report}")


def test_root_hands_the_reviewed_workflow_over_and_writes_nothing_to_the_board() -> None:
    """SYRD-253: configuring a workflow is the director's write, never root's.

    The live retry died on `caller_role must be non-empty` because root was
    making a director-only write. Root has no director process to make it
    with, and must not borrow a role header or a token to fake one.
    """
    with tenant() as t, provision_root():
        reads: list = []

        def board(_config):
            reads.append(1)
            return 5, None, ""

        code, report = migrate(t, apply=True, board=board)
        check(code == 0, f"{code}: {report}")
        check(len(reads) == 1, f"root read the board once and never came back to write: {reads}")
        path = tl.workflow_handoff_path("porter")
        info = path.lstat()
        check(stat.S_IMODE(info.st_mode) == 0o644, f"readable, and writable only by root: {oct(info.st_mode)}")
        check(stat.S_IMODE(path.parent.lstat().st_mode) == 0o755, "in a directory only root writes")
        handed = json.loads(path.read_text())
        check(handed["board_revision"] == 5, f"pinned to the revision it read: {handed['board_revision']}")
        check(handed["project"] == "porter", f"{handed['project']}")
        check("switchyard finish-upgrade porter" in report, f"and names the director's step: {report}")
        check("nothing was written to the board" in report, report)

        read, problem = tl.read_workflow_handoff("porter", config_path=t.config_path)
        check(read is not None, f"which the director can read back: {problem}")
        check(read.reviewed_digest == handed["reviewed_digest"], "with the digest root showed")


def test_the_director_refuses_a_handoff_it_cannot_trust() -> None:
    """Every way the file could not be root's, or not what root showed."""
    import copy as _copy

    with tenant() as t, provision_root():
        migrate(t, apply=True, board=lambda _c: (0, None, ""))
        path = tl.workflow_handoff_path("porter")
        original = path.read_text()

        def refused(label, expect):
            read, problem = tl.read_workflow_handoff("porter", config_path=t.config_path)
            check(read is None, f"{label}: it was accepted")
            check(expect in problem, f"{label}, for the right reason: {problem}")

        edited = json.loads(original)
        edited["document"] = _copy.deepcopy(edited["document"])
        edited["document"]["roles"][0]["label"] = "Somebody Else"
        path.write_text(json.dumps(edited))
        refused("an edited document", "does not match its own digest")

        moved = json.loads(original)
        moved["effective_digest"] = "0" * 64
        path.write_text(json.dumps(moved))
        refused("a document that would no longer be written as shown", "not the")

        path.write_text(original)
        path.chmod(0o664)
        refused("a file its group could rewrite", "group or beyond can write")
        path.chmod(0o644)

        link = path.with_name("second-name.json")
        os.link(path, link)
        refused("a file with a second name", "links")
        link.unlink()

        path.rename(path.with_name("real.json"))
        path.symlink_to(path.with_name("real.json"))
        refused("a symlink", "symlink")


def test_the_director_install_does_nothing_when_there_is_nothing_to_do() -> None:
    with tenant() as t, provision_root():
        stored = as_the_board_stores_it(MIGRATED_DOCUMENT)
        said: list[str] = []
        # A handoff is present: a board that already runs a workflow must be
        # left alone because it runs one, not because nothing was handed over.
        migrate(t, apply=True, board=lambda _c: (0, None, ""))
        check(
            tl.install_handed_off_workflow(
                t.config, config_path=t.config_path, caller_role="director",
                board_reader=lambda _c: (4, dict(stored), ""), print_func=said.append,
            ) is None,
            "a board already running a workflow is left alone",
        )
        from scripts import workflow_manage

        attempted: list = []
        previous = workflow_manage.main
        workflow_manage.main = lambda args: attempted.append(args) or 0
        said.clear()
        try:
            unreadable = tl.install_handed_off_workflow(
                t.config, config_path=t.config_path, caller_role="director",
                board_reader=lambda _c: (0, None, "refused"), print_func=said.append,
            )
        finally:
            workflow_manage.main = previous
        check(unreadable is False, "with something to install, a board that cannot be read is not read as empty")
        check("could not be read: refused" in " ".join(said), f"and says so: {said}")
        check(attempted == [], f"and writes nothing on an unknown: {attempted}")
        tl.workflow_handoff_path("porter").unlink()
        reads: list = []
        check(
            tl.install_handed_off_workflow(
                t.config, config_path=t.config_path, caller_role="director",
                board_reader=lambda _c: reads.append(1) or (0, None, "refused"),
                print_func=said.append,
            ) is None,
            "and with no handoff there is nothing to install",
        )
        check(reads == [], "and the board is not even asked, so an unreachable one changes nothing")


def test_a_write_the_board_does_not_show_is_not_reported_as_installed() -> None:
    """The writer returning is not the board running the workflow."""
    from scripts import workflow_manage

    other = as_the_board_stores_it(UNMIGRATED_DOCUMENT)
    for after, expect in (
        ((0, None, ""), "still reports no declared workflow"),
        ((9, dict(other), ""), "root handed over"),
    ):
        with tenant() as t, provision_root():
            migrate(t, apply=True, board=lambda _c: (0, None, ""))
            reads: list = []
            argv: list = []

            def board(_config):
                reads.append(1)
                return (0, None, "") if len(reads) == 1 else after

            said: list[str] = []
            previous = workflow_manage.main
            workflow_manage.main = lambda args: argv.append(list(args)) or 0
            try:
                result = tl.install_handed_off_workflow(
                    t.config, config_path=t.config_path, caller_role="director",
                    board_reader=board, print_func=said.append,
                )
            finally:
                workflow_manage.main = previous
            check(result is False, f"{after}: {result}")
            check(expect in " ".join(said), " ".join(said))
            command = argv[0]
            check(command[command.index("--socket") + 1] == t.config.board_socket,
                  f"the write was pinned to the board's socket: {command}")
            check(command[command.index("--expected-revision") + 1] == "0",
                  f"and to the revision it read: {command}")


def test_root_will_not_hand_over_through_a_directory_others_can_write() -> None:
    with tenant() as t, provision_root():
        directory = tl.workflow_handoff_path("porter").parent
        directory.mkdir(parents=True)
        directory.chmod(0o775)
        code, report = migrate(t, apply=True, board=lambda _c: (0, None, ""))
        check(code == 1, f"{code}: {report}")
        check("only root may write where the director reads" in report, report)
        check(not tl.workflow_handoff_path("porter").exists(), "and nothing was handed over")


def test_the_director_install_writes_only_over_the_socket() -> None:
    """A config with no socket gets a refusal, never a TCP write."""
    import dataclasses

    with tenant() as t, provision_root():
        migrate(t, apply=True, board=lambda _c: (0, None, ""))
        said: list[str] = []
        no_socket = dataclasses.replace(t.config, board_socket="")
        result = tl.install_handed_off_workflow(
            no_socket, config_path=t.config_path, caller_role="director",
            board_reader=lambda _c: (0, None, ""), print_func=said.append,
        )
        check(result is False, f"{result}: {said}")
        check("only made over the socket" in " ".join(said), " ".join(said))


class patched:
    """Replace team_launcher attributes for one block, and put them back."""

    def __init__(self, **replacements):
        self.replacements = replacements

    def __enter__(self):
        self.previous = {name: getattr(tl, name) for name in self.replacements}
        for name, value in self.replacements.items():
            setattr(tl, name, value)
        return self

    def __exit__(self, *exc):
        for name, value in self.previous.items():
            setattr(tl, name, value)
        return False


def test_finish_upgrade_installs_the_handoff_before_migrating_onboarding() -> None:
    """The director's command is where the first write happens, and a refusal stops it."""
    calls: list[str] = []
    common = dict(
        resolve_pinned_upgrade_source=lambda config, **_k: (None, None, None, ""),
        control_role_name=lambda config, **_k: ("director", ""),
        migrate_declarative_director_onboarding=lambda *a, **k: calls.append("onboarding") or False,
        director_onboarding_state=lambda *a, **k: ("pending", "stop here"),
        record_upgrade_phase=lambda *a, **k: None,
    )
    for outcome, expected_code, expected_calls in (
        (False, 1, ["install"]),
        (None, 1, ["install", "onboarding"]),
        (True, 1, ["install", "onboarding"]),
    ):
        calls.clear()
        seen: dict = {}

        def install(config, *, config_path, caller_role, print_func, **_k):
            calls.append("install")
            seen["caller_role"] = caller_role
            return outcome

        with tenant() as t, patched(install_handed_off_workflow=install, **common):
            code = tl.finish_upgrade_command(
                t.config, config_path=t.config_path, print_func=lambda _line: None
            )
        check(code == expected_code, f"{outcome}: {code}")
        check(calls == expected_calls, f"install {outcome} -> {calls}")
        check(seen["caller_role"] == "director", f"as the control role: {seen}")


def test_an_unprivileged_caller_is_told_how_this_is_run() -> None:
    with tenant() as t, provision_root():
        code, report = migrate(t, apply=True, board=lambda _c: (0, None, ""), euid=1000)
        check(code == 1, f"{code}")
        check(not tl.workflow_handoff_path("porter").exists(), "and nothing was handed over")
        check("pkexec switchyard migrate-workflow porter --apply" in report, report)


def test_the_command_is_registered_and_parses_what_it_claims() -> None:
    check("migrate-workflow" in tl.SWITCHYARD_COMMANDS, "the command exists")
    check(
        "migrate-workflow" in tl.SWITCHYARD_PRIVILEGED_COMMANDS,
        "and is privileged: it writes the root-owned handoff the director trusts",
    )
    parsed = tl._build_switchyard_migrate_workflow_parser().parse_args(["porter", "--apply"])
    check(parsed.project == "porter" and parsed.apply is True, f"{parsed}")
    check(
        tl._build_switchyard_migrate_workflow_parser().parse_args(["porter"]).apply is False,
        "and a bare invocation is a dry run",
    )


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("legacy_workflow_migration_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(120)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"legacy_workflow_migration_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
