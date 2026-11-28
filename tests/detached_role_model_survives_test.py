#!/usr/bin/env python3
"""SYRD-899: `set-role-runtime --model` records the role's model everywhere it is recorded, and an upgrade keeps it.

ocstab (2026-11-28, releases 1d116d9 to 17f3ee5): the User moved the Researcher from `gemini-4-hydrogen-high` to
`gemini-4-neon-high`, and after the upgrades and the reboot it was back on Hydrogen. The Director ran
`switchyard set-role-runtime ocstab researcher --cli agy --model gemini-4-neon-high`: the launcher entry said Neon,
and `.switchyard/ocstab.project.json` still said `"research": {"model": "gemini-4-hydrogen-high", ...}`, so the
Director hand-edited it. The switch rewrote the role's entry, and the artifact only when the tenant carried a
declared workflow -- and then only its `workflow`, never the role's own block.

The tenant here has every role kind whose model the artifact records: the Researcher (`project.research`), the
Librarian (`project.documentation`) and Router (`project.router`), detached, and Audit, in `role_clis`,
`role_models` and `role_efforts`. Each is switched by the supported path (`switch_role_runtime`), against the strict
board, the real start path and its verifier; tmux is stood in for and every "provider" is a copy of `sleep`, as in
the neighbouring suites. The upgrade is the real `upgrade_project_command`, its phases stood in, with the artifact
phase making each detached role's entry again from the artifact -- read by the real `load_project_design_artifact`
and written by the `record_*_runtime` writers `switchyard new --from` uses -- so a model the artifact lost is the
model the role comes back on, and the upgrade's own check says so. No live tenant is touched.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import role_runtime_consistency_test as harness  # noqa: E402
from scripts import (  # noqa: E402
    documentation_onboarding, research_onboarding, role_runtime, router_onboarding, runtime_projection,
    team_launcher as launcher,
)

CHECKS = 0
PROJECT = "porter"
#: Each role kind, what `switchyard new` gave it, and the change the User makes with `set-role-runtime`.
ROLES = {
    #  role         made with: runtime, model, effort                  chosen: runtime, model, effort
    "researcher": (("agy", "gemini-4-hydrogen-high", ""), ("agy", "gemini-4-neon-high", None)),
    #: To a runtime that takes no effort: the effort the Librarian was made with leaves its block with it.
    "librarian": (("claude", "claude-opus-5-5", "high"), ("agy", "gemini-4-neon-high", None)),
    "router": (("claude", "claude-haiku-5-5", "high"), ("codex", "gpt-5.5", "low")),
    "audit": (("claude", "claude-opus-5-5", "high"), ("claude", "claude-sonnet-5-5", "low")),
}
DETACHED = ("researcher", "librarian", "router")
BLOCK = {"researcher": "research", "librarian": "documentation", "router": "router"}


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:  # the label again, last, so a truncated tail still says which check failed
        raise AssertionError(f"{detail}\nFAILED CHECK: {re.split(r': [\[{(]', detail, maxsplit=1)[0]}")
    CHECKS += 1


def document() -> dict:
    """The board's declaration: every role the launcher starts has its runtime."""
    roles = [{"name": "director", "kind": "system", "active": True, "label": "Director", "runtime": "claude",
              "target": f"{PROJECT}-director:0.0", "slot": 0, "capabilities": []}]
    for name, ((runtime, _model, _effort), _chosen) in ROLES.items():
        roles.append({"name": name, "kind": "auditor" if name == "audit" else "system", "active": True,
                      "label": name.title(), "runtime": runtime, "target": f"{PROJECT}-{name}:0.0",
                      "slot": 1 if name == "audit" else None, "capabilities": []})
    roles.append({"name": "user", "kind": "system", "active": True, "label": "User", "runtime": None,
                  "target": None, "slot": None, "capabilities": []})
    return {"schema": "switchyard.workflow.v1", "project": PROJECT, "roles": roles, "stages": [], "transitions": [],
            "flags": {}, "remove_stages": []}


class Tenant:
    """A tenant `switchyard new --from` made: its launcher config, and the project artifact it was made from."""

    def __init__(self, tmp: Path, *, artifact: bool = True) -> None:
        self.tmp = tmp
        provision = tmp / ".switchyard" / "provision"
        provision.mkdir(parents=True)
        layout = provision / f"{PROJECT}-konsole-layout.json"
        layout.write_text(json.dumps(launcher._new_project_layout_payload(2), indent=2, sort_keys=True) + "\n")
        roles = [self.entry("director", "claude", "claude-opus-5-5", "high", slot=0)]
        for name, ((runtime, model, effort), _chosen) in ROLES.items():
            roles.append(self.entry(name, runtime, model, effort, slot=1 if name == "audit" else None))
        self.config_path = provision / f"{PROJECT}.json"
        self.config_path.write_text(json.dumps({
            "project": PROJECT, "layout": str(layout), "session_dir": str(tmp / "sessions"),
            "board_url": "http://127.0.0.1:65535", "presentation": {"slot_count": 2}, "roles": roles,
        }, indent=2, sort_keys=True) + "\n")
        self.artifact = tmp / ".switchyard" / f"{PROJECT}.project.json"
        if artifact:
            (tmp / "design.md").write_text("# porter\n")
            (tmp / "repository").mkdir()
            made = {name: made for name, (made, _chosen) in ROLES.items()}
            self.artifact.write_text(json.dumps({
                "schema": launcher.PROJECT_DESIGN_ARTIFACT_SCHEMA,
                "design_document": str(tmp / "design.md"),
                "project": {
                    "slug": PROJECT, "name": PROJECT, "ticket_prefix": "PORT", "owner_user": launcher.current_user_name(),
                    "repository": str(tmp / "repository"), "roles": ["main", "app"], "audit_roles": ["audit"],
                    "role_clis": {"main": "claude", "app": "codex", "audit": made["audit"][0]},
                    "role_models": {"main": "claude-opus-5-5", "audit": made["audit"][1]},
                    "role_efforts": {"audit": made["audit"][2]},
                    "research": {"stage": "researcher", "runtime": made["researcher"][0], "model": made["researcher"][1]},
                    "documentation": {"gate": "librarian", "runtime": made["librarian"][0],
                                      "model": made["librarian"][1], "effort": made["librarian"][2]},
                    "router": {"enabled": True, "runtime": made["router"][0], "model": made["router"][1],
                               "effort": made["router"][2]},
                },
            }, indent=2, sort_keys=True) + "\n")
        self.board = harness.StrictBoard(document=document())

    def entry(self, name: str, runtime: str, model: str, effort: str, *, slot: int | None) -> dict:
        workdir = self.tmp / name
        workdir.mkdir()
        return {"role": name, "slot": slot, "detached": slot is None, "target": f"{PROJECT}-{name}:0.0",
                "tmux_session": f"{PROJECT}-{name}", "workdir": str(workdir), "cli": [runtime],
                "live_commands": [runtime], "model": model, **({"effort": effort} if effort else {})}

    def host(self, name: str = "host", **kwargs) -> harness.Host:
        (self.tmp / name).mkdir()
        return harness.Host(self.tmp / name, self.config_path, live=tuple(ROLES), **kwargs)

    def launcher_entry(self, role: str) -> dict:
        return harness._entry(self.config_path, role)

    def project(self) -> dict:
        return json.loads(self.artifact.read_text())["project"]

    def switch(self, host: harness.Host, role: str, runtime: str, **kwargs):
        said: list[str] = []
        config = launcher.load_project_config(PROJECT, self.config_path)
        readiness = role_runtime._readiness_blockers
        role_runtime._readiness_blockers = harness.base._ready  # provider readiness has its own suites
        try:
            result = role_runtime.switch_role_runtime(
                config, config_path=self.config_path, role_name=role, runtime=runtime,
                environ=harness.base.DIRECTOR_ENV, runner=host, client=self.board, workflow_reader=self.board.reader,
                pane_state_dir=self.config_path.parent / "pane-state", print_func=said.append,
                busy_check=lambda *a, **k: False, **kwargs,
            )
            return result, "", said
        except SystemExit as exc:
            return None, str(exc), said
        finally:
            role_runtime._readiness_blockers = readiness

    def choose(self, host: harness.Host, role: str):
        runtime, model, effort = ROLES[role][1]
        return self.switch(host, role, runtime, model=model, **({} if effort is None else {"effort": effort}))

    def recorded(self, role: str) -> dict[str, str]:
        """What the artifact records of the role, by the fields `switchyard new --from` reads it from."""
        project = self.project()
        if role in BLOCK:
            block = project[BLOCK[role]]
            return {key: block.get(key, "") for key in ("runtime", "model", "effort")}
        return {"runtime": project["role_clis"][role], "model": project["role_models"][role],
                "effort": project["role_efforts"][role]}

    def regenerate_detached(self) -> None:
        """Each detached role's entry made again from the artifact, as `switchyard new --from` makes it."""
        design = launcher.load_project_design_artifact(self.artifact, expected_project=PROJECT)
        owner = launcher.current_user_name()
        research_onboarding.record_researcher_runtime(self.config_path, research=design.research, owner_user=owner)
        documentation_onboarding.record_librarian_runtime(self.config_path, None, documentation=design.documentation,
                                                          owner_user=owner)
        router_onboarding.record_router_runtime(self.config_path, None, router=design.router, owner_user=owner)


def runs_on(entry: dict) -> dict[str, str]:
    return {"runtime": entry["cli"][0], "model": entry.get("model", ""), "effort": entry.get("effort", "")}


def sandbox(case):
    def run() -> None:
        with tempfile.TemporaryDirectory(prefix="syrd899.") as tmp:
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
    run.__doc__ = case.__doc__
    return run


def upgrade(tenant: Tenant, refresh) -> tuple[int, list[str]]:
    """The whole upgrade command, its phases stood in for and `refresh` as its artifact phase; launcher put back."""
    names = ("UpgradeSourcePinned", "UpgradeStateReady", "UpgradeToolingStaged", "UpgradeIdentitiesDone",
             "_pin_upgrade_source", "_recover_upgrade_state", "_refresh_upgrade_artifacts", "_stage_upgrade_tooling",
             "_upgrade_identities_and_accounts", "_finish_upgrade")
    saved = {name: getattr(launcher, name) for name in names}
    launcher.UpgradeSourcePinned = launcher.UpgradeStateReady = SimpleNamespace
    launcher.UpgradeToolingStaged = launcher.UpgradeIdentitiesDone = SimpleNamespace
    launcher._pin_upgrade_source = lambda *_a, **_k: SimpleNamespace(
        desktop_choice=None, deploy_ref_chosen=False, source_repo=None, commit_git_dir=None, deploy_ref="x")
    launcher._recover_upgrade_state = lambda c, **_k: SimpleNamespace(
        source_repo=None, effective_source_repo=ROOT, config=c, cutover=None, document=None)

    def artifacts(c, **_k):
        refresh()
        return c

    launcher._refresh_upgrade_artifacts = artifacts
    launcher._stage_upgrade_tooling = lambda *_a, **_k: SimpleNamespace(trusted_release_root=None, publication_detail="")
    launcher._upgrade_identities_and_accounts = lambda c, **_k: SimpleNamespace(config=c, release_report_config=c)
    launcher._finish_upgrade = lambda *_a, **_k: 0
    printed = io.StringIO()
    try:
        with contextlib.redirect_stdout(printed):
            code = launcher.upgrade_project_command(launcher.load_project_config(PROJECT, tenant.config_path),
                                                    config_path=tenant.config_path, runner=lambda *a, **k: None,
                                                    print_func=print)
    finally:
        for name, value in saved.items():
            setattr(launcher, name, value)
    return code, printed.getvalue().splitlines()


# --------------------------------------------------------------------------


@sandbox
def test_each_role_kind_is_recorded_in_the_artifact_as_its_entry_runs(tmp: Path) -> None:
    tenant = Tenant(tmp)
    host = tenant.host()
    try:
        for role in ROLES:
            result, refused, said = tenant.choose(host, role)
            check(result is not None and not refused, f"{role}: the switch succeeds: {refused} {said}")
            entry = runs_on(tenant.launcher_entry(role))
            runtime, model, _effort = ROLES[role][1]
            check(entry["runtime"] == runtime and entry["model"] == model,
                  f"{role}: the launcher entry runs the choice: {entry}")
            check(tenant.recorded(role) == entry,
                  f"{role}: the artifact records the runtime, model and effort the entry runs: "
                  f"{tenant.recorded(role)} vs {entry}")
        project = tenant.project()
        check(project["role_clis"]["main"] == "claude" and project["role_models"]["main"] == "claude-opus-5-5"
              and project["role_clis"]["app"] == "codex" and "main" not in project["role_efforts"],
              f"a role nobody switched is recorded as it was: {project['role_clis']} {project['role_models']}")
        check(project["research"]["stage"] == "researcher" and project["documentation"]["gate"] == "librarian"
              and project["router"]["enabled"] is True,
              f"each block still says which role it is: {project['research']} {project['documentation']} "
              f"{project['router']}")
        design = launcher.load_project_design_artifact(tenant.artifact, expected_project=PROJECT)
        check(dict(design.research).get("model") == "gemini-4-neon-high"
              and dict(design.documentation).get("model") == "gemini-4-neon-high"
              and "effort" not in dict(design.documentation)
              and dict(design.router).get("runtime") == "codex" and dict(design.router).get("model") == "gpt-5.5"
              and dict(design.role_models).get("audit") == "claude-sonnet-5-5",
              f"the artifact `switchyard new --from` reads names each choice: {design.research} "
              f"{design.documentation} {design.router} {design.role_models}")
    finally:
        host.close()


@sandbox
def test_an_upgrade_keeps_each_detached_roles_model(tmp: Path) -> None:
    tenant = Tenant(tmp)
    host = tenant.host()
    try:
        for role in DETACHED:
            result, refused, said = tenant.choose(host, role)
            check(result is not None, f"{role}: switched: {refused} {said}")
        chosen = {role: runs_on(tenant.launcher_entry(role)) for role in DETACHED}
        code, printed = upgrade(tenant, tenant.regenerate_detached)
        after = {role: runs_on(tenant.launcher_entry(role)) for role in DETACHED}
        check(after == chosen, f"each detached role runs its choice after the upgrade: {after} vs {chosen}")
        check(after["researcher"]["model"] == "gemini-4-neon-high",
              f"ocstab's Researcher stays on Neon, not the model it was made with: {after['researcher']}")
        check(code == 0 and not [line for line in printed if "the upgrade changed" in line],
              f"the upgrade's own check finds no model it changed: {code} {printed}")
    finally:
        host.close()


@sandbox
def test_ocstab_the_same_command_repairs_an_artifact_left_behind_without_a_restart(tmp: Path) -> None:
    tenant = Tenant(tmp)
    host = tenant.host()
    try:
        result, refused, said = tenant.choose(host, "researcher")
        check(result is not None, f"switched: {refused} {said}")
        # ocstab on 2026-11-28 before the hand edit: the entry on Neon, the artifact on Hydrogen.
        stale = json.loads(tenant.artifact.read_text())
        stale["project"]["research"]["model"] = "gemini-4-hydrogen-high"
        tenant.artifact.write_text(json.dumps(stale, indent=2, sort_keys=True) + "\n")
        before = host.processes[f"{PROJECT}-researcher"].pid
        result, refused, _ = tenant.choose(host, "researcher")
        check(result is not None and not result.live_session_changed
              and host.processes[f"{PROJECT}-researcher"].pid == before,
              f"a switch to what the role already runs restarts nothing: {refused} {result}")
        check(result.projection_repaired == (str(tenant.artifact),) and result.projection_written,
              f"it names the artifact it brought in line, and only that: {result.projection_repaired}")
        check(tenant.recorded("researcher")["model"] == "gemini-4-neon-high",
              f"the artifact names the model the role runs: {tenant.recorded('researcher')}")
        result, _, _ = tenant.choose(host, "researcher")
        check(result is not None and result.projection_repaired == (),
              f"an artifact in line is left alone: {result.projection_repaired}")
    finally:
        host.close()


@sandbox
def test_a_dry_run_writes_nothing_and_says_where_the_role_is_recorded(tmp: Path) -> None:
    tenant = Tenant(tmp)
    host = tenant.host()
    try:
        before = tenant.artifact.read_text()
        config_before = tenant.config_path.read_text()
        result, refused, _ = tenant.switch(host, "researcher", "agy", model="gemini-4-neon-high", dry_run=True)
        check(result is not None and tenant.artifact.read_text() == before
              and tenant.config_path.read_text() == config_before,
              f"a dry run writes neither file: {refused}")
        files = list(runtime_projection.runtime_projection_files(
            tenant.config_path, role_name="researcher", runtime="agy", document=None, model="gemini-4-neon-high"))
        check(files == [tenant.artifact, tenant.config_path],
              f"the switch writes the artifact, and the launcher config last, which says the switch is made: {files}")
        where = runtime_projection.recorded_where(tenant.config_path, "researcher")
        check(where == [f"{tenant.config_path} (roles: researcher)", f"{tenant.artifact} (project.research)"],
              f"the Researcher is recorded in its entry and the artifact's research block: {where}")
        where = runtime_projection.recorded_where(tenant.config_path, "audit")
        check(where == [f"{tenant.config_path} (roles: audit)",
                        f"{tenant.artifact} (project.role_clis, project.role_models, project.role_efforts)"],
              f"Audit in its entry and the artifact's three tables: {where}")
        where = runtime_projection.recorded_where(tenant.config_path, "director")
        check(where == [f"{tenant.config_path} (roles: director)"],
              f"a role the artifact does not record, in its entry alone: {where}")
    finally:
        host.close()


@sandbox
def test_a_failed_switch_leaves_the_artifact_as_it_was(tmp: Path) -> None:
    tenant = Tenant(tmp)
    host = tenant.host(dies={"codex"})
    try:
        before = tenant.artifact.read_text()
        result, refused, said = tenant.choose(host, "router")
        check(result is None and "switch was undone" in refused, f"the start fails and is undone: {refused} {said}")
        check(tenant.artifact.read_text() == before, "the artifact is put back byte for byte")
        check(runs_on(tenant.launcher_entry("router"))["model"] == "claude-haiku-5-5",
              f"and the entry: {tenant.launcher_entry('router')}")
    finally:
        host.close()


@sandbox
def test_a_tenant_without_an_artifact_is_switched_as_before(tmp: Path) -> None:
    tenant = Tenant(tmp, artifact=False)
    host = tenant.host()
    try:
        result, refused, said = tenant.choose(host, "researcher")
        check(result is not None and runs_on(tenant.launcher_entry("researcher"))["model"] == "gemini-4-neon-high",
              f"switched: {refused} {said}")
        check(not tenant.artifact.exists(), "no artifact is made where there was none")
        check(runtime_projection.recorded_where(tenant.config_path, "researcher")
              == [f"{tenant.config_path} (roles: researcher)"], "and the entry is the one place it is recorded")
    finally:
        host.close()


def main() -> int:
    for name, case in list(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"detached_role_model_survives_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
