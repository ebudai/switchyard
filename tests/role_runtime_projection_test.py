#!/usr/bin/env python3
"""SYRD-558: a runtime switch moves every tenant copy of the declared workflow, or none of them.

Otto (2026-10-07, release 2951c5c9, which already held SYRD-486): after
`set-role-runtime` moved main and uiux to Codex, the board's revision 43 said
codex while the tenant's own `workflow.roles[].runtime` still said hermes, and
the operator hand-edited it back into line. The switch rewrote the role's
launcher entry and nothing else, but a workflow-driven tenant carries the
declared workflow in its config, `workflow.json`, `plan.json` and the project
artifact -- and `prepare_role` refuses a config whose roles disagree with its
own embedded workflow. The neighbouring suites never saw it: their tenant has
no embedded workflow at all.

The tenant here is the neighbouring suite's, made workflow-driven the way
`workflow apply` makes one -- the real `projection_files` written by the real
`apply_files` -- with a plan and a project artifact beside it. The switch is the
supported path (`switch_role_runtime`), against the strict board and the real
start path and verifier those suites use; tmux is stood in for and every
"provider" is a copy of `sleep`. No live tenant is touched.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import role_runtime_consistency_test as harness  # noqa: E402
from scripts import role_runtime, workflow_manage  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from scripts.workflow_launcher import project_roles, projection_files  # noqa: E402

CHECKS = 0
EXAMPLE = ROOT / "examples" / "workflows" / "inspection.json"


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def declared(runtime: str = "claude") -> dict:
    """The shipped example for project porter, its audit role on `runtime`."""
    doc = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    doc["project"] = "porter"
    for role in doc["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "porter-", 1)
        if role["name"] == "audit":
            role["runtime"] = runtime
    return validate(doc, project="porter")


class Tenant:
    """A workflow-driven tenant, projected as `workflow apply` projects one."""

    def __init__(self, tmp: Path, *, runtime: str = "claude") -> None:
        self.tmp = tmp
        self.config_path = harness.base._write_config(tmp)
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        (tmp / "repository").mkdir()
        raw.update(repository=str(tmp / "repository"), worktree_base=str(tmp / "worktrees"))
        self.config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.plan = self.config_path.parent / "plan.json"
        self.artifact = self.config_path.parent.parent / "porter.project.json"
        self.workflow = self.config_path.parent / "workflow.json"
        document = declared(runtime)
        for path in (self.plan, self.artifact):
            path.write_text(json.dumps({"project": "porter", "workflow": document}) + "\n", encoding="utf-8")
        workflow_manage.apply_files(projection_files(self.config_path, document))
        self.board = harness.StrictBoard(document=json.loads(json.dumps(document)))

    def files(self) -> dict[str, str]:
        """Every tenant file the projection writes, byte for byte."""
        return {str(path): path.read_text(encoding="utf-8") for path in sorted(self.tmp.rglob("*.json"))
                if "pane-state" not in path.parts and not path.name.startswith("role-runtime-")}

    def runtimes(self) -> dict[str, str]:
        """What each place a tenant records the audit role's runtime says."""
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))

        def in_doc(doc: dict) -> str:
            return next(role["runtime"] for role in doc["roles"] if role["name"] == "audit")

        return {
            "launcher entry": harness._entry(self.config_path)["cli"][0],
            "config workflow": in_doc(raw["workflow"]),
            "workflow.json": in_doc(json.loads(self.workflow.read_text(encoding="utf-8"))),
            "plan.json": in_doc(json.loads(self.plan.read_text(encoding="utf-8"))["workflow"]),
            "project artifact": in_doc(json.loads(self.artifact.read_text(encoding="utf-8"))["workflow"]),
            "board": self.board.runtime_of("audit"),
        }

    def preparable(self) -> bool:
        """`prepare_role`'s own refusal: the roles are what the embedded workflow projects."""
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        return raw["roles"] == project_roles(raw, raw["workflow"])["roles"]

    def set_copies(self, runtime: str) -> None:
        """The tenant's copies of the declaration naming `runtime`, the launcher and board left alone."""
        for path, key in ((self.config_path, "workflow"), (self.plan, "workflow"), (self.artifact, "workflow"), (self.workflow, None)):
            data = json.loads(path.read_text(encoding="utf-8"))
            doc = data[key] if key else data
            for role in doc["roles"]:
                if role["name"] == "audit":
                    role["runtime"] = runtime
            path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def host(self, name: str = "host", **kwargs) -> harness.Host:
        (self.tmp / name).mkdir()
        return harness.Host(self.tmp / name, self.config_path, **kwargs)

    def switch(self, host: harness.Host, runtime: str, **kwargs):
        return harness._switch(self.config_path, host, self.board, runtime, **kwargs)


ALL_CODEX = {"launcher entry": "codex", "config workflow": "codex", "workflow.json": "codex", "plan.json": "codex",
             "project artifact": "codex", "board": "codex"}


def sandbox(case):
    def run() -> None:
        with tempfile.TemporaryDirectory(prefix="syrd558.") as tmp:
            previous = os.environ.get("HOME")
            os.environ["HOME"] = tmp  # nothing here may reach the real home
            try:
                case(Path(tmp))
            finally:
                if previous is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = previous
    run.__name__ = case.__name__
    return run


@sandbox
def test_a_switch_moves_every_tenant_copy_with_the_board(tmp: Path) -> None:
    tenant = Tenant(tmp)
    host = tenant.host()
    try:
        result, refused, said = tenant.switch(host, "codex", model="gpt-5.5")
        check(result is not None and result.live_session_changed, (refused, said))
        check(tenant.runtimes() == ALL_CODEX, f"the launcher, every workflow copy and the board agree: {tenant.runtimes()}")
        check(harness._entry(tenant.config_path).get("model") == "gpt-5.5",
              f"what the switch rewrote in the role's entry survives the projection: {harness._entry(tenant.config_path)}")
        check(tenant.preparable(), "and prepare_role's own check holds: the roles are what the embedded workflow projects")
        raw = json.loads(tenant.config_path.read_text(encoding="utf-8"))
        check(raw["workflow"] == validate(tenant.board.document, project="porter"),
              "the embedded declaration is the board's, nothing else of it changed")
        check(host.running("porter-audit") == "codex", f"and the worker is codex: {host.running('porter-audit')}")
    finally:
        host.close()


@sandbox
def test_otto_a_tenant_whose_copies_already_differ_is_brought_whole(tmp: Path) -> None:
    """The ticket's reproduction: workflow.roles[].runtime differs from roles[].cli before the switch."""
    tenant = Tenant(tmp)
    tenant.set_copies("hermes")
    check(tenant.runtimes()["config workflow"] == "hermes" and tenant.runtimes()["launcher entry"] == "claude",
          "precondition: the copies name hermes, the launcher claude")
    host = tenant.host()
    try:
        result, refused, said = tenant.switch(host, "codex")
        check(result is not None and tenant.runtimes() == ALL_CODEX and tenant.preparable(),
              f"one supported switch leaves no copy behind: {tenant.runtimes()} {refused}")
    finally:
        host.close()


@sandbox
def test_otto_now_the_same_command_repairs_stale_copies_without_a_restart(tmp: Path) -> None:
    """Otto after its switch: board and launcher say codex, the copies still hermes."""
    tenant = Tenant(tmp, runtime="codex")
    entry = json.loads(tenant.config_path.read_text(encoding="utf-8"))
    for role in entry["roles"]:
        if role["role"] == "audit":
            role.update(cli=["codex"], live_commands=["codex"])
    tenant.config_path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tenant.set_copies("hermes")
    host = tenant.host()
    try:
        before = tenant.files()
        result, refused, _ = tenant.switch(host, "codex", dry_run=True)
        check(result is not None and not result.projection_written and len(result.projection_repaired) == 4
              and "run without --dry-run" in result.describe() and tenant.files() == before,
              f"--dry-run names the four stale files and writes nothing: {result and result.describe()} {refused}")
        calls = len(host.calls)
        result, refused, _ = tenant.switch(host, "codex")
        check(result is not None and result.projection_written and not result.live_session_changed
              and "did not match the board's declared workflow and was brought in line" in result.describe(),
              f"the same command brings them in line: {result and result.describe()} {refused}")
        check(tenant.runtimes() == ALL_CODEX and tenant.preparable(), f"every copy agrees: {tenant.runtimes()}")
        check(not [c for c in host.calls[calls:] if c[:2] in (["tmux", "kill-session"], ["tmux", "new-session"])],
              "and no worker was stopped or started for a file repair")
        result, _, _ = tenant.switch(host, "codex")
        check(result is not None and result.describe().endswith("nothing to change"), "then there is nothing to change")
        # Another writer's formatting is not staleness: the same data, laid out differently, is left alone.
        tenant.plan.write_text(json.dumps(json.loads(tenant.plan.read_text(encoding="utf-8")), indent=4) + "\n", encoding="utf-8")
        before = tenant.files()
        result, _, _ = tenant.switch(host, "codex")
        check(result is not None and result.describe().endswith("nothing to change") and tenant.files() == before,
              f"a consistent tenant formatted by another writer is not rewritten: {result and result.describe()}")
    finally:
        host.close()


@sandbox
def test_a_failed_switch_leaves_every_copy_as_it_was(tmp: Path) -> None:
    tenant = Tenant(tmp)
    before = tenant.files()
    host = tenant.host(dies={"codex"})
    try:
        result, refused, said = tenant.switch(host, "codex")
        check(result is None and "the switch was undone" in refused, (refused, said))
        check(tenant.files() == before, "every tenant file is back byte for byte")
        check(tenant.runtimes()["board"] == "claude" and host.running("porter-audit") == "claude",
              f"and the board and the worker are back: {tenant.runtimes()}")
        check(not harness._journal(tenant.config_path).exists(), "a clean rollback leaves no journal")
    finally:
        host.close()


@sandbox
def test_an_interrupted_write_leaves_a_journal_and_the_same_command_finishes(tmp: Path) -> None:
    """The process dies between files: the journal names every file to put back, and the launcher still says claude."""
    tenant = Tenant(tmp)
    before = tenant.files()
    host = tenant.host()
    real_atomic = workflow_manage.atomic

    written: list[Path] = []

    def dies_on_the_second_file(path: Path, content: str) -> None:
        if len(written) == 1:
            raise KeyboardInterrupt("the process died here")  # not an Exception: nothing in-process undoes it
        written.append(path)
        real_atomic(path, content)

    workflow_manage.atomic = dies_on_the_second_file
    try:
        try:
            tenant.switch(host, "codex")
            check(False, "the interrupted switch did not stop")
        except KeyboardInterrupt:
            pass
    finally:
        workflow_manage.atomic = real_atomic
    try:
        journal = json.loads(harness._journal(tenant.config_path).read_text(encoding="utf-8"))
        check(set(journal["previous_projection"]) == {str(p) for p in (tenant.config_path, tenant.workflow, tenant.plan,
                                                                        tenant.artifact, Path(json.loads(before[str(tenant.config_path)])["layout"]))}
              and all(journal["previous_projection"][name] == before[name] for name in journal["previous_projection"] if name in before),
              f"the journal holds every projected file as it was, written before the first byte: {sorted(journal['previous_projection'])}")
        check(tenant.config_path not in written and tenant.runtimes()["launcher entry"] == "claude"
              and tenant.runtimes()["board"] == "codex",
              f"the launcher config is written last, so a role cut off part-way still reads as not switched: {written}")
        result, refused, _ = tenant.switch(host, "codex")
        check(result is not None and tenant.runtimes() == ALL_CODEX and tenant.preparable(),
              f"and the same command finishes it: {tenant.runtimes()} {refused}")
    finally:
        host.close()


@sandbox
def test_a_projection_that_cannot_be_produced_refuses_before_anything_moves(tmp: Path) -> None:
    tenant = Tenant(tmp)
    tenant.board.document["project"] = "someone-else"
    before, revision = tenant.files(), tenant.board.revision
    host = tenant.host()
    try:
        result, refused, _ = tenant.switch(host, "codex")
        check(result is None and "cannot project the declared workflow" in refused,
              f"refused in preflight: {refused}")
        check(tenant.files() == before and tenant.board.revision == revision and host.running("porter-audit") == "claude",
              "nothing written, nothing applied, nothing stopped")
    finally:
        host.close()


@sandbox
def test_a_tenant_without_an_embedded_workflow_is_switched_as_before(tmp: Path) -> None:
    config_path = harness._tenant(tmp)
    text = json.dumps(json.loads(config_path.read_text(encoding="utf-8")), indent=4) + "\n"  # another writer's format
    config_path.write_text(text, encoding="utf-8")
    board = harness.StrictBoard()
    host = harness.Host(tmp, config_path)
    try:
        result, refused, _ = harness._switch(config_path, host, board, "claude")
        check(result is not None and result.describe().endswith("nothing to change") and config_path.read_text(encoding="utf-8") == text,
              f"no embedded workflow, nothing to change: the file is not even reformatted: {result and result.describe()} {refused}")
        result, refused, _ = harness._switch(config_path, host, board, "codex")
        check(result is not None and harness._entry(config_path)["cli"] == ["codex"] and "workflow" not in json.loads(config_path.read_text())
              and not (config_path.parent / "workflow.json").exists(),
              f"a legacy tenant's switch writes its launcher entry, and invents no workflow files: {refused}")
    finally:
        host.close()


@sandbox
def test_a_no_op_never_turns_a_legacy_tenant_into_a_workflow_driven_one(tmp: Path) -> None:
    """A tenant whose config carries no declared workflow, on a board that declares one: a no-op writes nothing."""
    tenant = Tenant(tmp)
    raw = json.loads(tenant.config_path.read_text(encoding="utf-8"))
    raw.pop("workflow")
    tenant.config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tenant.workflow.unlink()
    before = tenant.files()
    host = tenant.host()
    try:
        result, refused, _ = tenant.switch(host, "claude")
        check(result is not None and result.describe().endswith("nothing to change") and tenant.files() == before
              and not tenant.workflow.exists(),
              f"no projection is invented for it: {result and result.describe()} {refused}")
    finally:
        host.close()


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"role_runtime_projection_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
