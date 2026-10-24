#!/usr/bin/env python3
"""SYRD-665: the configuration an upgrade acts on after repatriation is the one root wrote.

Repatriation moves a legacy tenant's role state back to the project account
and writes the configuration without any role's `run_as_user`. The upgrade then
read that configuration back from the tenant's tree and computed the identity
cutover from what it read. A tenant that put a `run_as_user` naming another
account back in between -- the file is the tenant's -- made the cutover partial,
and the upgrade went on into `cutover_role_identities_command`: stopping the
roles, handing worktrees over and, on failure, the rollback whose restores act
by path.

Here the real upgrade runs over SYRD-45's legacy tenant (its own suite's
harness), and the instant root's repatriation write lands, the configuration on
disk gets each role's dedicated `run_as_user` back, as the tenant would write
it. The upgrade must act on what root wrote: no partial cutover, no identity
transaction. The before-run is main's code, from a git archive.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "54073520e828f6f9bd744c77a3c85dca87e1f3a9"  # main before SYRD-665
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"{detail}\nFAILED CHECK: {detail.split(': ', 1)[0]}")
    CHECKS += 1


def scenario(tree: Path, *, stale_layout: bool, report_link: bool = False) -> dict:
    """The race, run against `tree`'s code with `tree`'s own harness.

    With `stale_layout`, the tenant's layout is a stale generated one -- one of
    the product's own known legacy shapes -- so the upgrade's layout phase (U3)
    really changes it and the configuration is loaded again after it (Audit of
    12429073). Only the decision that the layout is a generated template stands
    in: the harness keeps its configuration outside a provision directory.

    With `report_link`, the operator also names a new upstream report board, so
    U3's report-link writer rewrites the configuration (Audit of 6aa8665c).
    """
    sys.path[:0] = [str(tree), str(tree / "tests")]
    import team_launcher_upgrade_cutover_test as cutover
    from scripts import team_launcher

    out: dict = {}
    with tempfile.TemporaryDirectory(prefix="syrd665.") as tmp:
        config_path, _ = cutover._declarative_tenant(Path(tmp), accounts=True)
        dedicated = {role["role"]: role["run_as_user"]
                     for role in json.loads(config_path.read_text(encoding="utf-8"))["roles"] if role.get("run_as_user")}
        out["dedicated"] = dedicated
        real_write = team_launcher._write_json_atomic
        raced: list[str] = []

        root_writes: list[list[str]] = []

        def write_then_race(path, payload, **kwargs):
            real_write(path, payload, **kwargs)
            if Path(path) == config_path and raced:
                root_writes.append(sorted(r.get("role", "") for r in payload.get("roles", []) if r.get("run_as_user")))
            # Root's repatriation write, and at once the tenant's: each role's dedicated account named again.
            if Path(path) == config_path and payload.get("role_state_isolation") is True and not raced:
                document = json.loads(config_path.read_text(encoding="utf-8"))
                for role in document["roles"]:
                    if role["role"] in dedicated:
                        role["run_as_user"] = dedicated[role["role"]]
                config_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
                raced.append(str(path))

        real_cutover = team_launcher.cutover_role_identities_command
        transactions: list[str] = []
        real_load = team_launcher.load_project_config
        loads: list[dict] = []

        def traced_load(project, path=None, **kwargs):
            config = real_load(project, path, **kwargs)
            loads.append({"from_memory": kwargs.get("document") is not None,
                          "dedicated": sorted(role.role for role in config.roles if role.run_as_user),
                          "pane_launcher": str(config.pane_launcher)})
            return config

        real_template = team_launcher._is_generated_project_layout_template
        if stale_layout:
            config = real_load("porter", config_path)
            count = len([r for r in config.roles if not r.detached])
            current = team_launcher._new_project_layout_payload(count)
            # A known generated shape that is not the current one: at a small role count some of them are the same.
            stale = next(shape for shape in team_launcher._known_generated_project_layout_payloads(count) if shape != current)
            config.layout.write_text(json.dumps(stale), encoding="utf-8")
            # And a pane launcher that is not the shared one, so the layout phase rewrites the configuration itself.
            document = json.loads(config_path.read_text(encoding="utf-8"))
            document["pane_launcher"] = "/syrd665/old-release/scripts/team-launcher"
            config_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
            team_launcher._is_generated_project_layout_template = lambda config, *, config_path: True

        def counted_cutover(config, **kwargs):
            transactions.append(config.project)
            return real_cutover(config, **kwargs)

        team_launcher._write_json_atomic = write_then_race
        team_launcher.cutover_role_identities_command = counted_cutover
        team_launcher.load_project_config = traced_load
        real_upgrade = team_launcher.upgrade_project_command
        if report_link:
            report = {"upstream_report_url": "http://127.0.0.1:9/syrd665",
                      "upstream_report_token_file": str(Path(tmp) / "report.token")}
            team_launcher.upgrade_project_command = lambda config, **kwargs: real_upgrade(config, **{**kwargs, **report})
        try:
            with cutover._RunningTenant(config_path, account_uid=os.getuid()) as tenant:
                tenant.live = False
                result, said, _m = cutover._upgrade(config_path, as_root=True, exists=set(), runner=tenant.runner(),
                                                    board=cutover._board_with_marker(False))
        finally:
            team_launcher._write_json_atomic = real_write
            team_launcher.cutover_role_identities_command = real_cutover
            team_launcher.load_project_config = real_load
            team_launcher._is_generated_project_layout_template = real_template
            team_launcher.upgrade_project_command = real_upgrade
        on_disk = json.loads(config_path.read_text(encoding="utf-8"))
        out.update(result=result, raced=raced, transactions=transactions, loads=loads, root_writes=root_writes,
                   report_recorded=on_disk.get("upstream_report_url") == "http://127.0.0.1:9/syrd665",
                   layout_line=next((line for line in said.splitlines() if "layout" in line), ""),
                   layout_upgraded="upgraded generated project" in said,
                   disk_dedicated=sorted(r["role"] for r in on_disk["roles"] if r.get("run_as_user")),
                   disk_stale_pin=on_disk.get("pane_launcher") == "/syrd665/old-release/scripts/team-launcher",
                   repatriated="repatriated porter's resumable role state" in said,
                   partial="is part-way onto per-role accounts" in said,
                   said=said[-1500:])
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests",
                              "skills", "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, *, stale_layout: bool = False, report_link: bool = False) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(tree),
                            *(["--stale-layout"] if stale_layout else []), *(["--report-link"] if report_link else [])],
                           capture_output=True,
                           text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900, stdin=subprocess.DEVNULL,
                           cwd=str(tree))
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(out: dict) -> None:
    # The race decides the cutover verdict, which is what sends a tenant with running roles into the identity
    # transaction (`_upgrade_identities_and_accounts`); this tenant's roles are stopped, as repatriation needs.
    check(out["raced"] and out["partial"],
          f"before: the run_as_user put back after repatriation's write is what the upgrade acts on -- the tenant "
          f"reads as part-way onto per-role accounts: {out['partial']} {out['said']}")


def before_stale_layout(out: dict) -> None:
    check(out["layout_upgraded"] and any(not load["from_memory"] and load["dedicated"] for load in out["loads"][1:]),
          f"before: with a stale generated layout, the configuration is read back from the tenant's file after "
          f"repatriation, its dedicated bindings with it: {out['loads']}")


def before_report_link(out: dict) -> None:
    check(out["report_recorded"] and any(out["root_writes"]),
          f"before: recording a new report link writes the tenant's re-added bindings back as root: {out['root_writes']}")


def candidate_report_link(out: dict) -> None:
    check(out["report_recorded"] and out["root_writes"] and not any(out["root_writes"]),
          f"the report link is recorded, and every root write of the configuration after repatriation is of root's "
          f"own document, no role on a dedicated account: {out['root_writes']}")
    check(out["disk_dedicated"] == [] and out["transactions"] == [] and out["result"] == 0,
          f"so the configuration on disk names no dedicated account, and no identity transaction ran: "
          f"{out['disk_dedicated']} {out['transactions']}")


def candidate(out: dict) -> None:
    check(out["dedicated"], f"the tenant is a legacy one, its roles on dedicated accounts: {out['dedicated']}")
    check(out["raced"] and out["repatriated"],
          f"repatriation wrote the configuration, and the tenant rewrote it straight after: {out['raced']}")
    check(out["transactions"] == [] and not out["partial"],
          f"the upgrade acts on what root wrote: no partial cutover, no identity transaction: {out['transactions']} "
          f"{out['said']}")
    check(out["result"] == 0, f"and finishes as a repatriation does: {out['result']} {out['said']}")


def candidate_stale_layout(out: dict) -> None:
    after_repatriation = out["loads"][next(i for i, load in enumerate(out["loads"]) if load["from_memory"]):]
    check(out["raced"] and out["layout_upgraded"] and len(after_repatriation) >= 2,
          f"with a stale generated layout the layout phase upgrades it and the configuration is loaded again: "
          f"{out['loads']} {out['said']}")
    check(all(load["from_memory"] and load["dedicated"] == [] for load in after_repatriation),
          f"every load after repatriation is of root's own document, with no role on a dedicated account: {out['loads']}")
    check(out["transactions"] == [] and not out["partial"] and out["result"] == 0,
          f"no partial verdict and no identity transaction, and the upgrade finishes: {out['transactions']} {out['said']}")
    check(out["loads"][-1]["pane_launcher"] != "/syrd665/old-release/scripts/team-launcher",
          f"and what the upgrade goes on with is the layout phase's result -- its launcher upgraded -- not root's "
          f"document from before it: {out['loads'][-1]}")
    check(not out["disk_stale_pin"] and out["disk_dedicated"] == [],
          f"the layout phase's own rewrite of the configuration starts from root's document: the launcher is upgraded and "
          f"the tenant's re-added bindings are not written back: {out['disk_dedicated']} {out['disk_stale_pin']}")



def main() -> int:
    if len(sys.argv) in (3, 4, 5) and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), stale_layout="--stale-layout" in sys.argv,
                                  report_link="--report-link" in sys.argv), default=str))
        return 0
    after = run(ROOT)
    candidate_stale_layout(run(ROOT, stale_layout=True))
    candidate_report_link(run(ROOT, report_link=True))
    if os.environ.get("SYRD665_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd665-before.") as tmp:
            before_tree = tree_at(BEFORE, Path(tmp) / "before")
            before(run(before_tree))
            before_stale_layout(run(before_tree, stale_layout=True))
            before_report_link(run(before_tree, report_link=True))
    candidate(after)
    print(f"repatriation_reload_race_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
