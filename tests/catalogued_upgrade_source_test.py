#!/usr/bin/env python3
"""SYRD-529: the catalogued pinned preview/upgrade resolves the release it names.

MEFP followed SYRD-284's chain: the operator installed shared 7fe0aee, the
`upgrade-tenant` bootstrap installed the new boundary and named
`preview-upgrade commit=7fe0aee` and `upgrade-tenant-release commit=7fe0aee`.
The preview then refused. Those actions send root `upgrade <slug> --deploy-ref
<commit>` and nothing else, and the upgrade fills whatever was not given from
root's record of the last pin -- so it kept the recorded source, the installed
release for 49abeb4, and refused to stage those tools against 7fe0aee. The
SYRD-284 suite never ran the named commands as the catalogue builds them: its
last step passed the new source explicitly.

Now, when the commit chosen is exactly the release the host runs, a recorded
installed release for another commit gives way to it. Nothing else changes:
the recorded commit cache (MEFP's project history, not Switchyard's) is kept;
a commit the operator has not activated, or no installed release holds, is
still refused -- and an apply is refused before it records anything, where it
used to write the mismatched pin first.

Every case runs as uid 0 in a user namespace (as SYRD-284's does) against real
releases staged the way root stages them -- 49abeb4 as the tenant's old pin, the
tree under test as the host's release -- and drives the argv the catalogue
builds. The reproduction runs main's code in a child from a git worktree.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "6726370266aa342f3eae4a2d9400a41d520d8f4a"  # main before SYRD-529
OLD_PIN = "49abeb4b00d47805e515af455ce506cb4ae41888"  # MEFP's recorded pin
NOT_CURRENT = "7fe0aee79209b958ed7588da4547b72bddebd5f5"  # installed, not what the host runs here
MISSING = "0123456789abcdef0123456789abcdef01234567"  # no release holds it
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


# The two refusals this ticket is about, matched as the lines they are. A phrase
# alone is not enough: an apply that gets further prints an unrelated advisory
# ("... re-verifies the live build before recording anything") that a bare
# substring mistook for the early refusal (SYRD-529 integration smoke).
EARLY_REFUSAL = re.compile(r"^switchyard: refusing to upgrade porter before recording anything: ", re.M)
MISMATCH = re.compile(r"is the installed release for [0-9a-f]{40}, but this upgrade is pinned at \S+")


def refused_early(said: str) -> bool:
    return bool(EARLY_REFUSAL.search(said))


def mismatched(said: str) -> bool:
    return bool(MISMATCH.search(said))


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def chain(tree: Path) -> dict:
    """The catalogued commands against a tenant pinned to an old installed release, from `tree`."""
    for extra in (str(tree), str(tree / "scripts"), str(tree / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import team_launcher_test_helpers as helpers  # noqa: F401 -- sets the sandboxed install roots
    import team_launcher_upgrade_cutover_test as cutover
    from host_boundary_bootstrap_test import shell_runner, tree as files
    from team_launcher_test_helpers import stage_trusted_release, team_launcher
    from scripts.ticket_board import privileged_operations as operations

    assert os.geteuid() == 0, "this half runs as root, so root's ownership checks are real"
    install_root = helpers.TEST_SWITCHYARD_SHARED_INSTALL_ROOT
    new = stage_trusted_release(tree, ref="HEAD")
    for commit in (OLD_PIN, NOT_CURRENT):
        stage_trusted_release(tree, ref=commit)
    old_root, new_root = install_root / "releases" / OLD_PIN, install_root / "releases" / new
    running = team_launcher.shared_switchyard_release_for_path(new_root)
    assert running is not None and running.marker_commit == new, running
    # The helper runs the host's current launcher: that release is running.
    team_launcher.running_launcher_release = lambda root=None: running
    seen: dict = {"new": new}
    with tempfile.TemporaryDirectory(prefix="syrd529.") as raw:
        tmp = Path(raw)
        config_path, _ = cutover._declarative_tenant(tmp)
        config = team_launcher.load_project_config("porter", config_path)
        cache = str(tmp / "fixpatch.git")
        team_launcher.record_upgrade_source(config, source_repo=old_root, commit_git_dir=cache, deploy_ref=OLD_PIN)
        pinned = team_launcher.read_upgrade_source(config)
        seen["pinned"] = pinned

        def catalogued(action: str, commit: str, **extra) -> dict:
            argv = operations.OPERATIONS[action]({"project": "porter", "commit": commit})
            assert argv[:3] == [operations.LAUNCHER, "upgrade", "porter"], argv
            flags = argv[3:]
            deploy_ref = flags[flags.index("--deploy-ref") + 1]
            dry_run = "--dry-run" in flags
            source = Path(flags[flags.index("--source-repo") + 1]) if "--source-repo" in flags else None
            return run(argv=argv, dry_run=dry_run, deploy_ref=deploy_ref, source_repo=source, **extra)

        def run(*, argv=None, dry_run=False, deploy_ref=None, source_repo=None) -> dict:
            before = files(tmp)
            runner = shell_runner()
            code, said, _ = cutover._upgrade(config_path, as_root=True, exists=set(), dry_run=dry_run, runner=runner,
                                             source_repo=source_repo, deploy_ref=deploy_ref)
            text = "\n".join(said) if isinstance(said, list) else str(said)
            return {
                "argv": [str(a) for a in argv or []], "code": code, "said": text.replace(str(install_root), "<opt>"),
                "wrote": files(tmp) != before, "pin": team_launcher.read_upgrade_source(config),
            }

        seen["preview other"] = catalogued("preview-upgrade", NOT_CURRENT)
        seen["apply other"] = catalogued("upgrade-tenant-release", NOT_CURRENT)
        team_launcher.record_upgrade_source(config, source_repo=old_root, commit_git_dir=cache, deploy_ref=OLD_PIN)
        seen["apply missing"] = catalogued("upgrade-tenant-release", MISSING)
        team_launcher.record_upgrade_source(config, source_repo=old_root, commit_git_dir=cache, deploy_ref=OLD_PIN)
        seen["unpinned"] = run()
        seen["operator source mismatch"] = run(source_repo=old_root, deploy_ref=new)
        team_launcher.record_upgrade_source(config, source_repo=old_root, commit_git_dir=cache, deploy_ref=OLD_PIN)
        seen["preview"] = catalogued("preview-upgrade", new)
        seen["apply"] = catalogued("upgrade-tenant-release", new)
        seen["preview after"] = catalogued("preview-upgrade", new)
        # The pinned way back: `select-shared-release` (admin-authenticated) names
        # root's own release for the old commit as the source, so nothing here
        # substitutes or refuses it. Root's cache is the sandbox's, owned by this
        # namespace's root.
        saved = operations.SHARED_RELEASES, operations.TRUSTED_OWNER_UID
        operations.SHARED_RELEASES, operations.TRUSTED_OWNER_UID = str(install_root / "releases"), 0
        try:
            seen["rollback"] = catalogued("select-shared-release", OLD_PIN)
        finally:
            operations.SHARED_RELEASES, operations.TRUSTED_OWNER_UID = saved
    seen["old root"], seen["new root"], seen["cache"] = "<opt>/releases/" + OLD_PIN, "<opt>/releases/" + new, cache
    return seen


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--privileged-child":
        print("RESULT " + json.dumps(chain(Path(sys.argv[2]))))
        return 0

    def child(tree: Path) -> dict:
        proc = subprocess.run(
            ["unshare", "--user", "--map-root-user", sys.executable, str(Path(__file__).resolve()),
             "--privileged-child", str(tree)],
            text=True, capture_output=True, check=False, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        assert proc.returncode == 0 and line, (proc.stdout[-3000:] + proc.stderr[-3000:])
        return json.loads(line[len("RESULT "):])

    with tempfile.TemporaryDirectory(prefix="syrd529-before.") as raw:
        before_tree = Path(raw) / "tree"
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", "-q", str(before_tree), BEFORE], check=True)
        try:
            # The fixtures stage the running tree's own HEAD as the host's release.
            shutil.copy2(Path(__file__), before_tree / "tests" / Path(__file__).name)
            before = child(before_tree)
        finally:
            subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(before_tree)], check=False)
    mismatch = f"is the installed release for {OLD_PIN}, but this upgrade is pinned at {before['new']}"
    check(before["preview"]["code"] == 1 and mismatch in before["preview"]["said"],
          f"reproduced: the catalogued preview of the host's release refuses on the recorded source: {before['preview']['said'][-600:]}")
    check(before["preview"]["pin"] == before["pinned"] and not before["preview"]["wrote"], "and the preview wrote nothing")
    check(before["apply"]["code"] == 1 and mismatch in before["apply"]["said"],
          f"reproduced: so does the apply: {before['apply']['said'][-600:]}")
    check(before["apply"]["pin"]["deploy_ref"] == before["new"] and before["apply"]["pin"]["source_repo"].endswith(OLD_PIN),
          f"reproduced: and it recorded the mismatched pin before refusing: {before['apply']['pin']}")

    now = child(ROOT)
    new, cache = now["new"], now["cache"]
    check(now["pinned"]["source_repo"].endswith(OLD_PIN) and now["pinned"]["commit_git_dir"] == cache,
          f"the tenant is pinned as MEFP was: {now['pinned']}")
    preview = now["preview"]
    check(preview["argv"][-3:] == ["--deploy-ref", new, "--dry-run"] and "--source-repo" not in preview["argv"],
          f"the catalogue still sends a commit and nothing else: {preview['argv']}")
    check(not mismatched(preview["said"]) and "in place of the recorded" in preview["said"]
          and f"the release this host runs and the commit chosen here" in preview["said"],
          f"the preview resolves the host's release for the commit it names, and says so: {preview['said'][-900:]}")
    check(f"from {now['new root']}" in preview["said"],
          f"and would stage from that release: {preview['said'][-900:]}")
    check(not preview["wrote"] and preview["pin"] == now["pinned"], f"a preview writes nothing: {preview['pin']}")
    apply = now["apply"]
    check(not mismatched(apply["said"]) and not refused_early(apply["said"]),
          f"the apply is not refused for its release: {apply['said'][-900:]}")
    check(apply["pin"]["source_repo"].endswith(f"/releases/{new}")
          and {k: apply["pin"][k] for k in ("commit_git_dir", "deploy_ref")} == {"commit_git_dir": cache, "deploy_ref": new},
          f"and records the reviewed tuple -- that release, that commit -- keeping the project's commit cache: {apply['pin']}")
    check("in place of the recorded" not in now["preview after"]["said"] and not mismatched(now["preview after"]["said"]),
          f"afterwards the recorded source is that release itself: {now['preview after']['said'][-400:]}")
    rollback = now["rollback"]
    check("--source-repo" in rollback["argv"] and rollback["argv"][-2:] == ["--deploy-ref", OLD_PIN]
          and "in place of the recorded" not in rollback["said"] and not refused_early(rollback["said"])
          and not mismatched(rollback["said"]),
          f"the pinned rollback to the old release is neither substituted nor refused for its release: {rollback['said'][-600:]}")
    check(rollback["pin"]["source_repo"].endswith(f"/releases/{OLD_PIN}") and rollback["pin"]["deploy_ref"] == OLD_PIN
          and rollback["pin"]["commit_git_dir"] == cache,
          f"and records the old tuple, the project's commit cache kept: {rollback['pin']}")
    for name, commit in (("other", NOT_CURRENT), ("missing", MISSING)):
        key = "apply other" if name == "other" else "apply missing"
        result = now[key]
        check(result["code"] == 1 and refused_early(result["said"]) and f"is the installed release for {OLD_PIN}, but this upgrade is pinned at {commit}" in result["said"],
              f"{key}: a commit the host does not run is refused before anything is recorded: {result['said'][-600:]}")
        check(f"this host runs {new}" in result["said"], f"{key}: and says which commit it can take: {result['said'][-400:]}")
        check(result["pin"] == now["pinned"], f"{key}: the pin is untouched: {result['pin']}")
    other = now["preview other"]
    check(other["code"] == 1 and f"is the installed release for {OLD_PIN}, but this upgrade is pinned at {NOT_CURRENT}" in other["said"]
          and not other["wrote"] and other["pin"] == now["pinned"],
          f"a preview of an installed release the host does not run is still refused, writing nothing: {other['said'][-600:]}")
    unpinned = now["unpinned"]
    check(unpinned["code"] == 1 and f"preview-upgrade commit={new}" in unpinned["said"] and unpinned["pin"] == now["pinned"],
          f"with nothing chosen, the recovered pin still means SYRD-284's refusal naming the pinned commands: {unpinned['said'][-600:]}")
    operator = now["operator source mismatch"]
    check(operator["code"] == 1 and f"is the installed release for {OLD_PIN}, but this upgrade is pinned at {new}" in operator["said"]
          and operator["pin"] == now["pinned"],
          f"an operator naming the old release and the new commit is refused as before, now before recording: {operator['said'][-600:]}")
    print(f"catalogued_upgrade_source_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
