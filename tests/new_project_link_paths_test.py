#!/usr/bin/env python3
"""SYRD-666: root's `switchyard new` names the project path as given, and writes by path only where nobody else can steer it.

Three root paths in `new` still acted by path, outside the owner's roots:

- the project path was resolved, so an existing owner's link at the default
  path in its home moved the project wherever it pointed, and root then made
  that directory with `install -d -o owner` and trusted it as the owner's;
  outside the home, `install -d` by path followed a link in the chain too;
- Router's and the Librarian's rewrite of a file outside the owner's roots
  (`lstat`, write, `lchown`, `chmod`) followed a link at the name or above;
- root's dry run (`team-launcher <p> new` without `--execute`) wrote its
  artifacts by path into whatever output directory it was named, a tenant's
  provision directory included.

Each case plants an owner-owned link, and proves where root's write lands. The
privileged half runs in a user namespace with the invoking account's subuid
range mapped, over SYRD-611's harness (`new_project_writes_test`); the owner
account is a stand-in for the lookups only. The before-run is main's code,
from a git archive, under the same harness.
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

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "bedf215cdc756e1a81e5b03a6c9fd6bcba1499a8"  # main before SYRD-666
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"{detail}\nFAILED CHECK: {detail.split(': ', 1)[0]}")
    CHECKS += 1


def outcome(call) -> str:
    try:
        call()
    except SystemExit as stop:
        return f"refused: {stop}"
    return "ok"


def cases(tree: Path) -> dict:
    """What root does in `tree`'s code, as namespace root, with a link planted for each path."""
    sys.path[:0] = [str(tree), str(tree / "tests")]
    import new_project_writes_test as harness

    from scripts import documentation_onboarding, root_packet, router_onboarding
    from scripts import team_launcher as launcher
    from scripts.ticket_board.publication_boundary import root_controlled_problems

    owner, owner_uid, foreign_uid = harness.OWNER, harness.OWNER_UID, harness.FOREIGN_UID

    def owner_dir(path: Path, mode: int = 0o755) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, owner_uid, owner_uid)
        os.chmod(path, mode)
        return path

    def owner_link(at: Path, target: Path) -> Path:
        at.symlink_to(target)
        os.lchown(at, owner_uid, owner_uid)
        return at

    def root_runner(argv, **kwargs):
        """The host's answers: the owner exists with linger, and may use what it was given; the helper and install
        really run."""
        if argv[:2] == ["id", "-u"] or argv[:1] == ["sudo"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "show-user" in argv:
            return subprocess.CompletedProcess(argv, 0, "yes\n", "")
        return subprocess.run(argv, capture_output=True, text=True)

    def ensure(host, project: Path) -> str:
        return outcome(lambda: launcher._ensure_owner_user_and_project_dir(owner, project, runner=root_runner,
                                                                            owner_home=host.home))

    out: dict = {}
    with tempfile.TemporaryDirectory(prefix="syrd666.") as raw:
        base = Path(raw)
        base.chmod(0o755)

        # 1. The default project path in an existing owner's home, a link the owner left there to another account's
        # tree, where the directory it names does not exist yet.
        host = harness.Host(base / "default")
        harness.owner_lookups(host.home)
        elsewhere = host.base / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        os.chown(elsewhere, foreign_uid, foreign_uid)
        default = owner_dir(host.home / "Projects") / "p"
        owner_link(default, elsewhere / "p")
        named = launcher._resolve_project_path(default)
        out["default_named"] = str(named)
        out["default_ensure"] = ensure(host, named)
        out["default_made_elsewhere"] = os.path.lexists(elsewhere / "p")
        out["dotdot"] = outcome(lambda: launcher._resolve_project_path(host.home / "Projects" / "x" / ".." / "p"))

        # 2. Outside the home, in a directory the owner owns: a link above the project directory, a link at it, and a
        # chain with nothing planted in it.
        host = harness.Host(base / "outside")
        harness.owner_lookups(host.home)
        srv = owner_dir(host.base / "srv")
        foreign_dir = host.base / "elsewhere"
        foreign_dir.mkdir(mode=0o700)
        os.chown(foreign_dir, foreign_uid, foreign_uid)
        owner_link(srv / "above", foreign_dir)
        out["above_ensure"] = ensure(host, srv / "above" / "q")
        out["above_made_elsewhere"] = os.path.lexists(foreign_dir / "q")
        owners_other = owner_dir(host.base / "owners-other", 0o700)
        owner_link(srv / "at", owners_other)
        out["at_ensure"] = ensure(host, srv / "at")
        out["clean_ensure"] = ensure(host, srv / "fresh" / "q")
        out["clean_owned"] = [list(harness.owned(srv / "fresh")) if (srv / "fresh").exists() else None,
                              list(harness.owned(srv / "fresh" / "q")) if (srv / "fresh" / "q").exists() else None]
        out["outside_foreign_unchanged"] = host.foreign_unchanged() and sorted(os.listdir(foreign_dir)) == \
            (["q"] if out["above_made_elsewhere"] else [])

        # 3. Router's and the Librarian's rewrite of a file outside the owner's roots, through a link above it to a
        # directory only root may enter; and in a directory only root controls.
        host = harness.Host(base / "router")
        harness.owner_lookups(host.home)
        roots_only = host.base / "roots-only"
        roots_only.mkdir(mode=0o700)
        design = roots_only / "design.json"
        design.write_text(json.dumps({"project": {"slug": "roots"}}), encoding="utf-8")
        design.chmod(0o600)
        untouched = design.read_bytes()
        through = owner_link(host.base / "cfgdir", roots_only)
        router = (("runtime", "claude"), ("model", "m"))
        out["router_choice"] = outcome(lambda: router_onboarding.record_router_choice(
            through / "design.json", router, owner_user=owner))
        out["librarian_choice"] = outcome(lambda: documentation_onboarding.record_documentation_choice(
            through / "design.json", (("gate", "librarian"), ("runtime", "claude")), owner_user=owner))
        out["router_foreign_unchanged"] = design.read_bytes() == untouched and os.listdir(roots_only) == ["design.json"]
        roots_own = host.base / "rootdir"
        roots_own.mkdir(mode=0o700)
        artifact = roots_own / "p.project.json"
        artifact.write_text(json.dumps({"project": {"slug": "p"}}), encoding="utf-8")
        artifact.chmod(0o640)
        real_judge = root_packet.root_controlled
        # From this suite's own base rather than from `/`, which in a user namespace is not root's.
        root_packet.root_controlled = lambda path: next(
            iter(root_controlled_problems(str(path), expect_uid=0, base=str(host.base))), "")
        try:
            out["root_dir_choice"] = outcome(lambda: router_onboarding.record_router_choice(artifact, router,
                                                                                           owner_user=owner))
        finally:
            root_packet.root_controlled = real_judge
        out["root_dir_written"] = json.loads(artifact.read_text(encoding="utf-8")).get("project", {}).get("router")
        out["root_dir_mode"] = oct(artifact.stat().st_mode & 0o777)

        # 4. Root's dry run named the owner's provision directory as its output, with a link the owner left there.
        host = harness.Host(base / "dryrun")
        harness.owner_lookups(host.home)
        aside = host.plant(host.provision, host.foreign)
        source = host.base / "release"
        (source / "scripts").mkdir(parents=True)
        for name in ("switchyard", "scripts/team-launcher", "scripts/ticket-board-service.sh",
                     "scripts/ticket-board-boardsvc-setup.sh"):
            (source / name).write_text("#!/bin/sh\n", encoding="utf-8")
        (source / launcher.SWITCHYARD_RELEASE_MARKER_NAME).write_text('{"commit": "' + "6" * 40 + '"}\n')
        from team_launcher_test_helpers import FakeRunner

        said: list[str] = []
        out["dry_run"] = outcome(lambda: launcher.new_project_command(
            "porter", owner_user=owner, source_repo=source, commit_git_dir="/srv/git/review-cache.git",
            repository=host.project, output_dir=host.provision, runner=FakeRunner(), port_in_use=lambda _port: False,
            socket_exists=lambda _path: False, print_func=said.append))
        # Without --new-output-dir, root's temporary directory, when the temporary-directory setting puts it in the
        # owner's tree: root made it, but the owner can rename it and put a link there before the artifacts are
        # written by path, so it is refused the same (Audit of b4a2b905). The stand-in makes the directory and,
        # as the owner would, swaps a link to another tree in at its name.
        real_default = launcher._new_project_artifact_dir
        made = owner_dir(host.home / "root-made")
        swapped_into = host.base / "swapped-into"
        swapped_into.mkdir(mode=0o700)

        def swapped(_project):
            leaf = made / "porter-team-launcher-new.x"
            leaf.mkdir(mode=0o700)
            os.rename(leaf, made / "taken")
            owner_link(leaf, swapped_into)
            return leaf

        launcher._new_project_artifact_dir = swapped
        try:
            out["default_dry_run"] = outcome(lambda: launcher.new_project_command(
                "porter", owner_user=owner, source_repo=source, commit_git_dir="/srv/git/review-cache.git",
                repository=host.project, runner=FakeRunner(), port_in_use=lambda _port: False,
                socket_exists=lambda _path: False, print_func=said.append))
        finally:
            launcher._new_project_artifact_dir = real_default
        out["swapped_into"] = sorted(os.listdir(swapped_into))
        out["dry_run_foreign_unchanged"] = host.foreign_unchanged()
        out["dry_run_foreign"] = sorted(os.listdir(host.foreign))
        host.unplant(host.provision, aside)

        # 5. The repository `new` is handed, a link the owner left, is the project root root trusts for the owner's
        # writes: as named, never where the link points. (The run stops later, at the output directory.)
        from scripts import owner_writes

        linked = owner_link(host.home / "linked", owners_other)
        owner_writes.TRUSTED_PROJECT_ROOTS.pop(owner, None)
        outcome(lambda: launcher.new_project_command(
            "porter", owner_user=owner, source_repo=source, commit_git_dir="/srv/git/review-cache.git",
            repository=linked, output_dir=host.provision, runner=FakeRunner(), port_in_use=lambda _port: False,
            socket_exists=lambda _path: False, print_func=said.append))
        out["trusted"] = sorted(owner_writes.TRUSTED_PROJECT_ROOTS.get(owner, ()))
        out["linked"], out["owners_other"] = str(linked), str(owners_other)

        # 6. A design artifact read through a link names a relative repository beneath the artifact as named.
        real = owner_dir(host.home / "artifacts")
        launcher._write_initial_switchyard_project_artifact(
            project_name="Porter", slug="porter", owner_user=owner, project_dir=host.project,
            artifact_path=real / "porter.project.json", design_document=host.project / "DESIGN.md",
            owner_shell="/bin/sh", implementer_roles=("app",), role_clis=(("director", "claude"), ("app", "claude")),
            include_designer=False, include_audit=False, audit_roles=())
        document = json.loads((real / "porter.project.json").read_text(encoding="utf-8"))
        document["project"]["repository"] = "proj"
        (real / "porter.project.json").write_text(json.dumps(document), encoding="utf-8")
        via = owner_link(host.base / "via", real)
        out["artifact_repository"] = str(launcher.load_project_design_artifact(via / "porter.project.json").repository)
        out["via"], out["real"] = str(via), str(real)

        # 7. And `new --from` that artifact: the repository it names is trusted as named.
        owner_writes.TRUSTED_PROJECT_ROOTS.pop(owner, None)
        outcome(lambda: launcher.new_project_command(
            "porter", from_artifact=via / "porter.project.json", source_repo=source,
            commit_git_dir="/srv/git/review-cache.git", output_dir=host.provision, runner=FakeRunner(),
            port_in_use=lambda _port: False, socket_exists=lambda _path: False, print_func=said.append))
        out["from_trusted"] = sorted(owner_writes.TRUSTED_PROJECT_ROOTS.get(owner, ()))

        # 8. Root's dry run named another tenant's project directory, one the host's registry names, as its output.
        import pwd

        other_home = host.base / "other-home"
        other_project = host.base / "other" / "proj"
        (other_project / ".switchyard" / "provision").mkdir(parents=True)
        for item in (other_home, host.base / "other", other_project, other_project / ".switchyard",
                     other_project / ".switchyard" / "provision"):
            item.mkdir(exist_ok=True)
            os.chown(item, 1600, 1600)
        registry = host.base / "registry"
        registry.mkdir(mode=0o755)
        (registry / "other.json").write_text(json.dumps(
            {"config_path": str(other_project / ".switchyard" / "provision" / "other.json")}), encoding="utf-8")
        other = pwd.struct_passwd(("1600", "x", 1600, 1600, "", str(other_home), "/bin/sh"))
        by_name, by_uid = pwd.getpwnam, pwd.getpwuid
        pwd.getpwnam = lambda name: other if name == "1600" else by_name(name)
        pwd.getpwuid = lambda uid: other if uid == 1600 else by_uid(uid)
        real_registry = owner_writes.TRUSTED_REGISTRY
        owner_writes.TRUSTED_REGISTRY = (registry, str(host.base))
        try:
            out["other_dry_run"] = outcome(lambda: launcher.new_project_command(
                "porter", owner_user=owner, source_repo=source, commit_git_dir="/srv/git/review-cache.git",
                repository=host.project, output_dir=other_project / ".switchyard" / "out", runner=FakeRunner(),
                port_in_use=lambda _port: False, socket_exists=lambda _path: False, print_func=said.append))
        finally:
            owner_writes.TRUSTED_REGISTRY = real_registry
            pwd.getpwnam, pwd.getpwuid = by_name, by_uid
        out["other_written"] = sorted(os.listdir(other_project / ".switchyard" / "out")) \
            if (other_project / ".switchyard" / "out").exists() else []
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests",
                              "skills", "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
    child = subprocess.run(
        ["unshare", "--user", "--map-auto", "--map-root-user", sys.executable, str(Path(__file__).resolve()),
         "--child", str(tree)], capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=900, stdin=subprocess.DEVNULL, cwd=str(tree))
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(out: dict) -> None:
    check(out["default_named"].endswith("/elsewhere/p") and out["default_made_elsewhere"],
          f"before: the default path was resolved through the owner's link, and root made the directory in another "
          f"account's tree: {out['default_named']} {out['default_ensure']}")
    check(out["above_made_elsewhere"],
          f"before: outside the home, install -d by path followed a link above the project directory: "
          f"{out['above_ensure']}")
    check(out["router_choice"] == "ok" and out["librarian_choice"] == "ok" and not out["router_foreign_unchanged"],
          f"before: Router's rewrite outside the owner's roots followed a link above the file into root's own directory: "
          f"{out['router_choice']}")
    check(out["dry_run"] != "ok" and "plan.json" in out["dry_run_foreign"],
          f"before: root's dry run wrote its artifacts through the link at the owner's provision directory: "
          f"{out['dry_run_foreign']} {out['dry_run']}")
    check("plan.json" in out["swapped_into"],
          f"before: without --new-output-dir, root's temporary directory in the owner's tree was written through the "
          f"link swapped in at its name: {out['swapped_into']}")
    check(out["trusted"] == [out["owners_other"]],
          f"before: the repository was resolved, and root trusted where the owner's link pointed: {out['trusted']}")
    check(out["artifact_repository"] == out["real"] + "/proj" and out["from_trusted"] == [out["real"] + "/proj"],
          f"before: a relative repository was taken beneath where the link to the artifact pointed, and trusted there: "
          f"{out['artifact_repository']} {out['from_trusted']}")
    check("plan.json" in out["other_written"],
          f"before: root's dry run wrote into another registered tenant's project directory: {out['other_written']}")


def candidate(out: dict) -> None:
    check(out["default_named"].endswith("/home/tenant/Projects/p"),
          f"the project path is named as given, not resolved through the owner's link: {out['default_named']}")
    check(out["default_ensure"].startswith("refused:") and "symbolic link" in out["default_ensure"]
          and not out["default_made_elsewhere"],
          f"and root refuses the link there, making nothing in the other account's tree: {out['default_ensure']}")
    check(out["dotdot"].startswith("refused:") and "'..'" in out["dotdot"],
          f"a '..' in the project path is refused, not normalised past a link: {out['dotdot']}")
    check(out["above_ensure"].startswith("refused:") and not out["above_made_elsewhere"],
          f"outside the home, a link above the project directory is refused by the no-follow helper: "
          f"{out['above_ensure']}")
    check(out["at_ensure"].startswith("refused:") and "symbolic link" in out["at_ensure"],
          f"and a link at the project directory itself is refused, never checked or trusted through: {out['at_ensure']}")
    check(out["clean_ensure"] == "ok" and out["clean_owned"] == [[1500, 0o750], [1500, 0o750]],
          f"a chain with nothing planted in it is made, each new directory the owner's at the tree's mode: "
          f"{out['clean_ensure']} {out['clean_owned']}")
    check(out["outside_foreign_unchanged"], "nothing in another account's tree was made or changed")
    check(out["router_choice"].startswith("refused:") and out["librarian_choice"].startswith("refused:")
          and out["router_foreign_unchanged"],
          f"Router's and the Librarian's rewrite outside the owner's roots, where the directory is not root's alone, "
          f"is refused with nothing changed: {out['router_choice']} {out['librarian_choice']}")
    check(out["root_dir_choice"] == "ok" and out["root_dir_written"] == {"enabled": True, "runtime": "claude",
                                                                         "model": "m"}
          and out["root_dir_mode"] == "0o640",
          f"in a directory only root controls -- its own temporary output directory -- the rewrite goes on, keeping "
          f"the mode: {out['root_dir_choice']} {out['root_dir_written']} {out['root_dir_mode']}")
    check(out["dry_run"].startswith("refused:") and "Nothing was written" in out["dry_run"]
          and out["dry_run_foreign_unchanged"],
          f"root's dry run into the owner's tree is refused before it writes anything: {out['dry_run']} "
          f"{out['dry_run_foreign']}")
    check(out["default_dry_run"].startswith("refused:") and "a dry run as root writes" in out["default_dry_run"]
          and "with --new-output-dir" in out["default_dry_run"] and out["swapped_into"] == [],
          f"without --new-output-dir, root's temporary directory in the owner's tree -- a link swapped in at its name "
          f"-- is refused the same, nothing written through it: {out['default_dry_run'][:400]}")
    check(out["trusted"] == [out["linked"]],
          f"the repository `new` is handed is trusted as named, not where the owner's link points: {out['trusted']}")
    check(out["artifact_repository"] == out["via"] + "/proj",
          f"a relative repository in a design artifact is beneath the artifact as named: {out['artifact_repository']}")
    check(out["from_trusted"] == [out["via"] + "/proj"],
          f"and `new --from` it trusts that repository as named: {out['from_trusted']}")
    check(out["other_dry_run"].startswith("refused:") and "1600's tree" in out["other_dry_run"]
          and out["other_written"] == [],
          f"root's dry run into another tenant's project directory, one the host's registry names, is refused too: "
          f"{out['other_dry_run']} {out['other_written']}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        print(json.dumps(cases(Path(sys.argv[2])), default=str))
        return 0
    for tool in ("setfacl", "getfacl", "unshare"):
        assert shutil.which(tool), f"{tool} is required; this suite must not silently skip"
    after = run(ROOT)
    if os.environ.get("SYRD666_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd666-before.") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    print(f"new_project_link_paths_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
