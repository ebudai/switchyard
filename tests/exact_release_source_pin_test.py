#!/usr/bin/env python3
"""SYRD-578: an exact-release upgrade with no source records one that deploy-release accepts, or refuses.

On Otto the operator ran `sudo switchyard upgrade otto --deploy-ref <sha>` on a
tenant with no recorded pin. It exited 0 and advertised `deploy-release`, but it
had recorded the commit beside an empty source, and `deploy-release` refused:
"root holds no installed release of <sha> as otto's pinned source (None)". The
catalogued `upgrade-tenant-release` runs the same argv.

Each scenario is an unpinned legacy tenant (the pinned-release suite's: a
declarative config, a deployed board, staged tooling) and an installed shared
release under an overridden install root. The tenant is stopped, with no live
role and no presentation. The upgrade is the real `upgrade_project_command` as
root, and the continuation is the real `switchyard_deploy_release_command`.
Every host command goes to the recording runner those suites use, and the
deploy really moves the board's `current`.

The scenarios run in a child, once on main `BEFORE` from a `git archive` (this
file copied in) and once on this tree.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "0d9e16de132dfa3d03062f5cc1e2348121e60d74"  # main before SYRD-578
OTHER = "f" * 40
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
#: What would start a pane or a presentation; a stopped tenant needs none.
LAUNCH_VERBS = ("new-session", "respawn-pane", "respawn-window")
LAUNCH_PROGRAMS = ("konsole", "switchyard-pane-window", "switchyard-display-attach")


def launches(argv: list[str]) -> bool:
    """Whether this argv starts a pane or a presentation: a tmux verb that makes one, or a launcher program run."""
    words = list(argv)
    while words[:1] == ["sudo"]:
        words = words[4:] if words[1:2] == ["-u"] and words[3:4] == ["-H"] else words[3:] if words[1:2] == ["-u"] else words[1:]
    return any(verb in words for verb in LAUNCH_VERBS) or (bool(words) and os.path.basename(words[0]) in LAUNCH_PROGRAMS)
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        print(f"FAILED CHECK: {detail[:300]}", file=sys.stderr)
        raise AssertionError(detail)
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


# --- the child: one tree's behaviour ------------------------------------------------------------------------------


def scenarios(tree: Path) -> dict:
    sys.path[:0] = [str(tree / "tests"), str(tree)]
    import release_phase_journal_test as journal
    import team_launcher_pinned_release_resume_test as pinned
    from scripts import team_launcher as launcher
    from scripts.ticket_board import privileged_operations as operations

    def tenant(tmp: Path, shape: str):
        config_path, current, cache, sha, board_root = pinned._pinned_tenant(tmp)
        install_root = current.parent
        release = install_root / "releases" / sha
        if shape == "inactive":
            other = install_root / "releases" / OTHER
            shutil.copytree(release, other, symlinks=True)
            (other / ".switchyard-release.json").write_text(json.dumps({"commit": OTHER}) + "\n", encoding="utf-8")
            current.unlink()
            current.symlink_to(other)
        elif shape == "missing":
            other = install_root / "releases" / OTHER
            release.rename(other)
            (other / ".switchyard-release.json").write_text(json.dumps({"commit": OTHER}) + "\n", encoding="utf-8")
            current.unlink()
            current.symlink_to(other)
        elif shape == "mismatched":
            (release / ".switchyard-release.json").write_text(json.dumps({"commit": OTHER}) + "\n", encoding="utf-8")
        elif shape == "symlinked":
            elsewhere = tmp / "elsewhere" / sha
            elsewhere.parent.mkdir()
            release.rename(elsewhere)
            release.symlink_to(elsewhere)
        return config_path, current, sha, board_root, release

    def upgrade(config_path, current, sha, board_root, *, euid=0, dry_run=False, recorded=None, **selection):
        seen: list[list[str]] = []
        config = launcher.load_project_config(pinned.PROJECT, config_path)
        if recorded is not None:
            path = launcher.privileged_upgrade_source_path(config)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"schema": launcher.UPGRADE_SOURCE_SCHEMA, "project": pinned.PROJECT, **recorded}),
                            encoding="utf-8")
            os.chmod(path, 0o600)
        saved = launcher.os.geteuid
        with pinned._RunningTenant(config_path, account_uid=os.getuid()) as running:
            running.live = False
            runner = pinned._pinned_runner(running.runner(), board_root=board_root, sha=sha, seen=seen, deploys=[])
            if euid == 0:
                code, said = pinned._run_upgrade(config_path, exists=pinned.ROLE_ACCOUNTS, repo_root=current,
                                                 runner=runner, dry_run=dry_run, **selection)
            else:
                # `_run_upgrade` is root's; the same call with the uid an operator's own account has.
                code, said = _as_uid(pinned, launcher, euid, config_path, current, runner, dry_run, selection)
        launcher.os.geteuid = saved
        config = launcher.load_project_config(pinned.PROJECT, config_path)
        record_path = launcher.privileged_upgrade_source_path(config)
        return {
            "code": code, "said": said,
            "record": launcher.read_upgrade_source(config) if record_path.exists() else None,
            "phases": launcher.read_upgrade_journal(config, config_path=config_path, trusted=True).get("phases", {}),
            "launches": [" ".join(a)[:120] for a in seen if launches(a)],
            "seen": len(seen),
        }

    def deploy(config_path, sha, board_root) -> dict:
        said: list[str] = []
        deploys: list[str] = []
        board = journal.FakeBoard(sha)
        saved = (launcher._resolve_switchyard_project, launcher.os.geteuid, launcher._open_board_url)
        launcher._resolve_switchyard_project = lambda project, **_k: SimpleNamespace(slug=pinned.PROJECT, config_path=config_path)
        launcher.os.geteuid = lambda: 0
        launcher._open_board_url = board
        try:
            with pinned._RunningTenant(config_path, account_uid=os.getuid()) as running:
                running.live = False
                runner = pinned._pinned_runner(running.runner(), board_root=board_root, sha=sha, seen=[], deploys=deploys)
                code = launcher.switchyard_deploy_release_command(pinned.PROJECT, commit=sha, runner=runner,
                                                                  print_func=said.append)
        finally:
            launcher._resolve_switchyard_project, launcher.os.geteuid, launcher._open_board_url = saved
        config = launcher.load_project_config(pinned.PROJECT, config_path)
        return {"code": code, "said": "\n".join(said), "deploys": len(deploys),
                "board": launcher._current_tenant_release(board_root)[1],
                "release": launcher.upgrade_phase_state(
                    launcher.read_upgrade_journal(config, config_path=config_path, trusted=True), "release")}

    def run(shape: str, **kwargs) -> dict:
        with pinned._Environment(), tempfile.TemporaryDirectory(prefix="syrd578.") as raw:
            tmp = Path(raw)
            config_path, current, sha, board_root, release = tenant(tmp, shape)
            sel = dict(kwargs.pop("selection", {"deploy_ref": "SHA"}))
            sel = {k: (sha if v == "SHA" else v) for k, v in sel.items()}
            recorded = kwargs.pop("recorded", None)
            if recorded is not None:
                recorded = {k: (sha if v == "SHA" else v) for k, v in recorded.items()}
            out = upgrade(config_path, current, sha, board_root, recorded=recorded, **sel, **kwargs)
            out["sha"], out["release_path"] = sha, str(release)
            out["said"] = out["said"].replace(str(tmp), "TMP").replace(sha, "SHA")
            if out["code"] == 0 and not kwargs.get("dry_run"):
                out["deploy"] = deploy(config_path, sha, board_root)
                out["deploy"]["said"] = out["deploy"]["said"].replace(str(tmp), "TMP").replace(sha, "SHA")
                out["deploy"]["board"] = "SHA" if out["deploy"]["board"] == sha else out["deploy"]["board"]
            if out["record"]:
                out["record"] = {k: v.replace(str(release), "RELEASE").replace(sha, "SHA") for k, v in out["record"].items()}
            return out

    # The catalogued Director actions: the argv root is asked to run, parsed by the real upgrade parser.
    catalogued = {}
    for action in ("upgrade-tenant-release", "preview-upgrade"):
        argv = operations.command_for(action, {"project": pinned.PROJECT, "commit": "a" * 40})
        parsed = launcher._build_switchyard_upgrade_parser().parse_args(argv[2:])
        catalogued[action] = {"argv": argv[1:], "source_repo": parsed.source_repo, "commit_git_dir": parsed.commit_git_dir,
                              "deploy_ref": parsed.deploy_ref, "dry_run": parsed.dry_run}

    results = {"catalogued": catalogued}
    selection = {"deploy_ref": "SHA"}
    results["trusted"] = run("trusted", selection=selection)
    results["trusted dry"] = run("trusted", selection=selection, dry_run=True)
    for shape in ("inactive", "missing", "mismatched", "symlinked"):
        results[shape] = run(shape, selection=selection)
        results[f"{shape} dry"] = run(shape, selection=selection, dry_run=True)
    # Otto's state after the incident: the commit recorded beside an empty source; an argument-free rerun.
    results["recovered empty source"] = run("trusted", selection={},
                                            recorded={"source_repo": "", "commit_git_dir": "", "deploy_ref": "SHA"})
    results["not root"] = run("trusted", selection=selection, euid=1006)
    # A branch is not an exact release: nothing to infer, and nothing new to refuse.
    results["branch"] = run("trusted", selection={"deploy_ref": "origin/main"})
    return results


def _as_uid(pinned, launcher, euid, config_path, current, runner, dry_run, selection):
    """`_run_upgrade`, but as an account that is not root."""
    printed: list[str] = []
    saved = (launcher.os.geteuid, launcher.local_account_exists, launcher.migrate_declarative_director_onboarding,
             launcher._open_board_url, launcher._repo_root)
    try:
        launcher._repo_root = lambda: current
        launcher._open_board_url = pinned._board_with_marker(False)
        launcher.os.geteuid = lambda: euid
        launcher.local_account_exists = lambda account: account in pinned.ROLE_ACCOUNTS
        launcher.migrate_declarative_director_onboarding = lambda config, **kwargs: True
        config = launcher.load_project_config(pinned.PROJECT, config_path)
        code = launcher.upgrade_project_command(config, config_path=config_path, tooling_root=config_path.parent / "tooling",
                                                runner=runner, print_func=printed.append, dry_run=dry_run, **selection)
    finally:
        (launcher.os.geteuid, launcher.local_account_exists, launcher.migrate_declarative_director_onboarding,
         launcher._open_board_url, launcher._repo_root) = saved
    return code, "\n".join(printed)


def untrusted_owner() -> dict:
    """The whole-path ownership proof, with the shipped expectation: root's uid, which a test tree is not."""
    sys.path[:0] = [str(ROOT / "tests"), str(ROOT)]
    from scripts import active_release_source as active
    from scripts.ticket_board import publication_boundary

    with tempfile.TemporaryDirectory(prefix="syrd578-owner.") as raw:
        install_root = Path(raw) / "opt" / "switchyard"
        release = install_root / "releases" / ("b" * 40)
        release.mkdir(parents=True)
        (release / ".switchyard-release.json").write_text(json.dumps({"commit": "b" * 40}), encoding="utf-8")
        (install_root / "current").symlink_to(release)
        saved_env = os.environ.get("SWITCHYARD_SHARED_INSTALL_ROOT")
        os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = str(install_root)
        try:
            as_owner = active.active_release_source("b" * 40)
            real_getuid = publication_boundary.os.getuid
            publication_boundary.os.getuid = lambda: 0
            try:
                as_root = active.active_release_source("b" * 40)
            finally:
                publication_boundary.os.getuid = real_getuid
        finally:
            if saved_env is None:
                os.environ.pop("SWITCHYARD_SHARED_INSTALL_ROOT", None)
            else:
                os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = saved_env
        return {"owner": [str(as_owner[0]), as_owner[1]], "root": [str(as_root[0]), as_root[1].replace(raw, "TMP")],
                "release": str(release)}


# --- the parent: what each tree does, and what that has to be -----------------------------------------------------


def child(tree: Path, mode: str = "--scenarios") -> dict:
    proc = subprocess.run([sys.executable, "-B", str(tree / "tests" / Path(__file__).name), mode, str(tree)], text=True,
                          capture_output=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"}, timeout=1800)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
    if proc.returncode != 0 or not line:
        raise AssertionError(f"the scenarios did not finish on {tree}: {proc.stdout[-2000:]}{proc.stderr[-4000:]}")
    return json.loads(line[len("RESULT "):])


def before_tree(dest: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", BEFORE], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    shutil.copy2(Path(__file__).resolve(), dest / "tests" / Path(__file__).name)
    return dest


def refused_before_anything(result: dict, name: str) -> None:
    check(result["code"] == 1 and result["record"] is None and result["phases"] == {} and result["launches"] == [],
          f"{name}: refused, with no pin recorded, no phase, nothing started: {json.dumps(result)[:900]}")
    check("names no source for SHA, and root has no release it may take for one" in result["said"]
          and "install-shared-release commit=SHA" in result["said"]
          and "--source-repo TMP/opt/switchyard/releases/SHA" in result["said"]
          and "Nothing was changed" in result["said"],
          f"{name}: says why and names the preparation: {result['said'][-900:]}")


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenarios":
        print("RESULT " + json.dumps(scenarios(Path(sys.argv[2]))))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--owner":
        print("RESULT " + json.dumps(untrusted_owner()))
        return 0

    skip_before = os.environ.get("SYRD578_MUTATION_SKIP_BEFORE") == "1"
    if not skip_before:
        with tempfile.TemporaryDirectory(prefix="syrd578-before.") as raw:
            before = child(before_tree(Path(raw) / "tree"))
        old = before["trusted"]
        check(old["code"] == 0 and old["record"] == {"source_repo": "", "commit_git_dir": "", "deploy_ref": "SHA"}
              and "deploy-release commit=SHA" in old["said"],
              f"reproduced on main: exit 0, the commit recorded beside an empty source, deploy-release advertised: {old['record']}")
        check(old["deploy"]["code"] == 1 and old["deploy"]["deploys"] == 0
              and "root holds no installed release of SHA as porter's pinned source (None)" in old["deploy"]["said"],
              f"reproduced on main: the advertised deploy-release refuses it, as on Otto: {old['deploy']['said'][-400:]}")
        check(before["catalogued"]["upgrade-tenant-release"]["source_repo"] is None,
              f"main's catalogued upgrade-tenant-release names no source either: {before['catalogued']}")

    now = child(ROOT)
    catalogued = now["catalogued"]
    check(catalogued["upgrade-tenant-release"]["argv"] == ["upgrade", "porter", "--deploy-ref", "a" * 40]
          and catalogued["upgrade-tenant-release"]["source_repo"] is None
          and catalogued["upgrade-tenant-release"]["commit_git_dir"] is None
          and catalogued["upgrade-tenant-release"]["dry_run"] is False
          and catalogued["preview-upgrade"]["argv"] == ["upgrade", "porter", "--deploy-ref", "a" * 40, "--dry-run"]
          and catalogued["preview-upgrade"]["dry_run"] is True,
          f"the catalogued actions run exactly the selection these scenarios use, the preview as its dry run: {catalogued}")

    trusted = now["trusted"]
    check(trusted["code"] == 0 and trusted["record"] == {"source_repo": "RELEASE", "commit_git_dir": "", "deploy_ref": "SHA"},
          f"the active installed release of the chosen commit is recorded as the source: {trusted['record']} {trusted['said'][-600:]}")
    check("porter names no source for SHA; its source is TMP/opt/switchyard/releases/SHA, this host's active, "
          "root-controlled installed release of that commit." in trusted["said"],
          f"and the upgrade says which release it took: {trusted['said'][:1200]}")
    check(launches(["sudo", "-u", "porter-app", "-H", "tmux", "new-session", "-d", "-s", "porter-app"])
          and launches(["/opt/switchyard/current/scripts/switchyard-pane-window", "porter"])
          and not launches(["sudo", "-u", "porter-app", "-H", "tmux", "has-session", "-t", "porter-app"]),
          "the launch screen sees a pane or presentation being started, and not a read")
    check(trusted["launches"] == [] and trusted["seen"] > 0 and "deploy-release commit=SHA" in trusted["said"],
          f"a stopped tenant, with no live role or presentation, is prepared without starting any: {trusted['launches']}")
    continued = trusted["deploy"]
    check(continued["code"] == 0 and continued["deploys"] == 1 and continued["board"] == "SHA" and continued["release"] == "done",
          f"exit 0 keeps its promise: the advertised deploy-release deploys that release and closes the phase: "
          f"{continued}")

    dry = now["trusted dry"]
    check(dry["code"] == 0 and dry["record"] is None
          and "porter names no source for SHA; its source is TMP/opt/switchyard/releases/SHA" in dry["said"],
          f"the dry run takes the same release, and records nothing: {dry['record']} {dry['said'][:800]}")

    for shape in ("inactive", "missing", "mismatched", "symlinked"):
        refused_before_anything(now[shape], shape)
        refused_before_anything(now[f"{shape} dry"], f"{shape}, dry run")
        decision = [l for l in now[shape]["said"].splitlines() if "names no source" in l or "install and activate" in l]
        dry_decision = [l for l in now[f"{shape} dry"]["said"].splitlines() if "names no source" in l or "install and activate" in l]
        check(decision == dry_decision and len(decision) == 2,
              f"{shape}: the dry run decides exactly as the upgrade: {decision} / {dry_decision}")
    reasons = {shape: now[shape]["said"] for shape in ("inactive", "missing", "mismatched", "symlinked")}
    check("is installed but not active: TMP/opt/switchyard/current is TMP/opt/switchyard/releases/" + OTHER in reasons["inactive"]
          and "TMP/opt/switchyard/releases/SHA does not exist" in reasons["missing"]
          and f"TMP/opt/switchyard/releases/SHA records {OTHER}, not SHA" in reasons["mismatched"]
          and "TMP/opt/switchyard/releases/SHA is a symbolic link" in reasons["symlinked"],
          f"each refusal names its own reason: {json.dumps({k: v[-500:] for k, v in reasons.items()})[:2000]}")

    recovered = now["recovered empty source"]
    check(recovered["code"] == 0 and recovered["record"] == {"source_repo": "RELEASE", "commit_git_dir": "", "deploy_ref": "SHA"}
          and recovered["deploy"]["code"] == 0 and recovered["deploy"]["release"] == "done",
          f"a tenant left with an empty source is repaired by an argument-free rerun, and deploys: "
          f"{recovered['record']} {recovered['deploy']} {recovered['said'][:600]}")

    unprivileged = now["not root"]
    check(unprivileged["record"] is None and "names no source" not in unprivileged["said"]
          and "pinned release is not recorded" in unprivileged["said"],
          f"an upgrade that is not root records nothing and decides nothing new, as before: {unprivileged['said'][:900]}")

    branch = now["branch"]
    check("names no source" not in branch["said"] and (branch["record"] or {}).get("deploy_ref") == "origin/main",
          f"a branch ref is not an exact release: no source is inferred and nothing new refused: {branch['record']} {branch['said'][-300:]}")

    owner = child(ROOT, "--owner")
    check(owner["owner"] == [owner["release"], ""], f"a tree its owner holds passes when that owner is the expected one: {owner}")
    check(owner["root"][0] == "None" and "is not root-controlled" in owner["root"][1] and "rather than by uid 0" in owner["root"][1],
          f"with root's uid expected, as on a host, a release not owned by root is refused: {owner}")

    print(f"exact_release_source_pin_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
