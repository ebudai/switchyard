#!/usr/bin/env python3
"""SYRD-658: resume-provision finds the generated configuration where root recorded the checkout.

PIE's `switchyard new` failed before it registered the project. Its checkout
was `Projects/personal_inference_engine`, the operator's choice, and root's
own plan record says so: `project_repository`. `resume-provision pie` did not
read it. It tried `Projects/Personal Inference Engine` and `Projects/pie`, the
display name and the slug, found neither, and -- because the artifacts it
re-rendered came from a plan rebuilt without the field -- rewrote root's
record without it too. Recovery went through only once the operator typed:

    switchyard resume-provision pie --config /home/pie-agent/Projects/personal_inference_engine/.switchyard/provision/pie.json

Here, as uid 0 in a user namespace, root's record is the one the product's
renderer installs for a plan whose checkout is named unlike its project, and
the configuration is the one the product generates there. The real
`switchyard_resume_provision_command` runs; only the role launch, the packet
read-back and the owner account lookups stand in, as in SYRD-155's suite. Each
printed retry is split and parsed by the real `resume-provision` parser. The
before-run is main's code, from a git archive.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "d1bd10023314780d329c80e9127b0593772f6595"  # main before SYRD-658
SLUG, NAME, TENANT = "pie", "Personal Inference Engine", "pie-agent"
OWNER_UID = 0  # the one uid in the namespace, and so the owner's (as in SYRD-155's suite)
#: Checkout names no spelling of the project's name or slug reaches: normalized, spaced, punctuated.
SPELLINGS = {
    "normalized": "personal_inference_engine",
    "spaces": "Personal Inference Engine (2026)",
    "punctuation": "pie's engine; v2 & $HOME",
}
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def inner(tree: Path) -> dict:
    sys.path[:0] = [str(tree), str(tree / "tests")]
    import live_boundary

    live_boundary.isolate()
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import patch

    from scripts import team_launcher as launcher
    from scripts.ticket_board.project_provision import build_plan

    seam = "SWITCHYARD_PRIVILEGED_PROVISION_ROOT"

    def owner_patches(home: Path) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch.object(launcher, "uid_for_user", lambda name: OWNER_UID if name == TENANT else None))
        stack.enter_context(patch.object(launcher, "home_dir_for_user", lambda name: home if name == TENANT else None))
        real = launcher.pwd.getpwuid
        stack.enter_context(patch.object(
            launcher.pwd, "getpwuid",
            side_effect=lambda uid: SimpleNamespace(pw_gid=OWNER_UID, pw_name=TENANT) if uid == OWNER_UID else real(uid)))
        return stack

    def quiet(*_a, **_k) -> None:
        return None

    def provisioned(root: Path, checkout_name: str) -> SimpleNamespace:
        """What PIE's failed `switchyard new` left: root's record, the account's home, the generated checkout."""
        os.environ[seam] = str(root)
        release = root / "release"
        shutil.copytree(tree / "scripts", release / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        home = root / "home" / TENANT
        checkout = home / "Projects" / checkout_name
        checkout.mkdir(parents=True)
        plan = build_plan(project=SLUG, project_name=NAME, owner_user=TENANT, owner_home=home, source_repo=release,
                          service_user="boardsvc", project_repository=checkout)
        installed = launcher.install_privileged_artifacts(plan, launcher.render_privileged_artifacts(plan))
        provision = checkout / ".switchyard" / "provision"
        provision.mkdir(parents=True)
        with owner_patches(home):
            config_path = launcher.write_new_project_launcher_artifacts(
                plan, provision, repository=checkout, project_name=NAME, print_func=quiet)
            config = launcher.load_project_config(SLUG, config_path)
        registry = root / "registry"
        registry.mkdir()
        return SimpleNamespace(release=release, home=home, checkout=checkout, installed=installed,
                               config_path=config_path, config=config, registry=registry, root=root)

    def resume(world: SimpleNamespace, *, packet_done: bool = True, launch: int = 0,
               config_path: Path | None = None) -> dict:
        said: list[str] = []
        roles = world.config.roles
        packet = launcher.PacketCompletion(() if packet_done else ("the board service is not running",))
        with owner_patches(world.home), patch.object(launcher, "launch_project", lambda *a, **k: launch):
            status = launcher.switchyard_resume_provision_command(
                SLUG, source_repo=world.release, registry_dir=world.registry, config_path=config_path,
                euid_getter=lambda: 0, completion_reader=lambda _plan: packet,
                desktop_approval_path=world.root / "no-desktop-approval.json",
                registration=launcher.RuntimeRegistrationWait(),
                process_commands=[f"agy TICKET_BOARD_PANE_TARGET={role.target}" for role in roles],
                pane_liveness_states=[launcher.PaneLiveness(role.role, True, "fixture: pane up") for role in roles],
                session_statuses=[launcher.LaunchSessionRecordStatus(role=role.role, target=role.target,
                                                                     session_id=f"sid-{role.role}") for role in roles],
                print_func=said.append,
            )
        entry = world.registry / f"{SLUG}.json"
        record = json.loads((world.installed / "plan.json").read_text(encoding="utf-8"))
        return {"status": status, "said": said,
                "registered": json.loads(entry.read_text())["config_path"] if entry.exists() else None,
                "recorded_checkout": record.get("project_repository", ""),
                "retries": [parse_retry(launcher, line) for line in said for line in re.findall(r"`([^`]*)`", line)
                            if "resume-provision" in line]}

    out: dict = {}
    for case, name in SPELLINGS.items():
        with tempfile.TemporaryDirectory(prefix="syrd658.") as tmp:
            root = Path(tmp)
            root.chmod(0o755)
            world = provisioned(root, name)
            # The interrupted shape first: the packet has not finished, so root re-renders its artifacts.
            unfinished = resume(world, packet_done=False)
            # Then the roles fail to start once, and then everything finishes.
            failed_launch = resume(world, launch=1)
            finished = resume(world)
            out[case] = {"expected": str(world.config_path), "checkout": str(world.checkout),
                         "unfinished": unfinished, "failed_launch": failed_launch, "finished": finished}

    # No path the tenant can write replaces root's record: a valid configuration at a guessed spelling, or a link
    # in the recorded checkout to one elsewhere, is not registered.
    for case in ("guessed_spelling", "linked_checkout"):
        with tempfile.TemporaryDirectory(prefix="syrd658.") as tmp:
            root = Path(tmp)
            root.chmod(0o755)
            world = provisioned(root, SPELLINGS["normalized"])
            recorded_dir = world.checkout / ".switchyard"
            if case == "guessed_spelling":
                decoy_checkout = world.home / "Projects" / SLUG
            else:
                decoy_checkout = world.home / "elsewhere"
            decoy_provision = decoy_checkout / ".switchyard" / "provision"
            decoy_provision.mkdir(parents=True)
            from scripts.ticket_board.project_provision import build_plan as _build

            decoy_plan = _build(project=SLUG, project_name=NAME, owner_user=TENANT, owner_home=world.home,
                                source_repo=world.release, service_user="boardsvc", project_repository=decoy_checkout)
            with owner_patches(world.home):
                decoy = launcher.write_new_project_launcher_artifacts(
                    decoy_plan, decoy_provision, repository=decoy_checkout, project_name=NAME, print_func=quiet)
            shutil.rmtree(recorded_dir)
            if case == "linked_checkout":
                recorded_dir.symlink_to(decoy_checkout / ".switchyard")
            result = resume(world)
            out[case] = {"decoy": str(decoy), "expected": str(world.config_path), **result}

    # Root's recorded checkout is taken only inside the owner's home, without `..`; otherwise the old guesses.
    from dataclasses import replace

    home = Path("/home") / TENANT
    plan = build_plan(project=SLUG, project_name=NAME, owner_user=TENANT, owner_home=home, source_repo=tree,
                      service_user="boardsvc")
    out["candidates"] = {
        label: [str(c) for c in launcher._tenant_config_candidates(
            replace(plan, project_repository=checkout), SLUG, explicit=None, recorded=None, registered=None)]
        for label, checkout in (("inside", f"{home}/Projects/pie engine"), ("dotdot", f"{home}/../eric/Projects/pie"),
                                ("outside", "/srv/pie"), ("home", str(home)), ("relative", "Projects/pie"), ("none", ""))}
    return out


def parse_retry(launcher, command: str) -> dict:
    try:
        argv = shlex.split(command)
    except ValueError:  # not even one shell command: a retry nobody can paste
        return {"command": command, "refused_by_shell": True}
    if argv[:3] != ["sudo", "switchyard", "resume-provision"]:
        return {"command": command, "argv": argv}
    try:
        args = launcher._build_switchyard_resume_provision_parser().parse_args(argv[3:])
    except SystemExit:  # the real parser refused it: a retry nobody can run
        return {"command": command, "refused_by_parser": True}
    return {"command": command, "project": args.project,
            "config_path": str(args.config_path) if args.config_path else None}


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
    child = subprocess.run(["unshare", "--user", "--map-root-user", sys.executable, str(Path(__file__).resolve()),
                            "--inner", str(tree)], capture_output=True, text=True,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900, stdin=subprocess.DEVNULL)
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(out: dict) -> None:
    case = out["normalized"]
    first = case["unfinished"]
    check(first["recorded_checkout"] == "",
          f"before: a resume that re-renders root's artifacts drops the recorded checkout from root's record: {first}")
    finished = case["finished"]
    check(finished["status"] == 1 and finished["registered"] is None
          and any("--config <path>" in line for line in finished["said"]),
          f"before: without --config the recovery refuses and offers only a placeholder, as on PIE: {finished}")
    guessed = out["guessed_spelling"]
    check(guessed["registered"] == guessed["decoy"],
          f"before: a configuration the tenant placed at the slug's spelling is registered in place of root's record: {guessed}")


def candidate(out: dict) -> None:
    for case, name in SPELLINGS.items():
        result = out[case]
        expected, checkout = result["expected"], result["checkout"]
        exact = {"project": SLUG, "config_path": expected}
        unfinished = result["unfinished"]
        check(unfinished["status"] == 1 and unfinished["registered"] is None
              and unfinished["recorded_checkout"] == checkout
              and [{k: r.get(k) for k in exact} for r in unfinished["retries"]][-1:] == [exact],
              f"{case}: an unfinished packet leaves root's record of the checkout {name!r} in place, and its retry "
              f"names that checkout's configuration exactly, as the real parser reads it: {unfinished}")
        failed = result["failed_launch"]
        check(failed["status"] == 1 and failed["registered"] == expected
              and [{k: r.get(k) for k in exact} for r in failed["retries"]][-1:] == [exact],
              f"{case}: without --config the configuration is found from root's record and registered, and when the "
              f"roles then fail to start the retry names it exactly: {failed}")
        finished = result["finished"]
        check(finished["status"] == 0 and finished["registered"] == expected and finished["recorded_checkout"] == checkout,
              f"{case}: a retry finishes the recovery from that configuration: {finished}")
    for case in ("guessed_spelling", "linked_checkout"):
        result = out[case]
        check(result["status"] == 1 and result["registered"] is None
              and not any(result["decoy"] in line for line in result["said"] if "registered" in line),
              f"{case}: a valid configuration the tenant placed where root's record does not point is not "
              f"registered in its place: {result}")


def candidates(out: dict) -> None:
    home = f"/home/{TENANT}"
    guesses = [f"{home}/Projects/{NAME}/.switchyard/provision/{SLUG}.json", f"{home}/Projects/{SLUG}/.switchyard/provision/{SLUG}.json"]
    got = out["candidates"]
    check(got["inside"] == [f"{home}/Projects/pie engine/.switchyard/provision/{SLUG}.json"],
          f"a recorded checkout inside the owner's home is the whole answer: {got['inside']}")
    check(all(got[label] == guesses for label in ("dotdot", "outside", "home", "relative", "none")),
          f"one with `..`, outside the home, the home itself, relative or absent is not taken: {got}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2])), default=str))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT), indent=1, default=str))
        return 0
    after = run(ROOT)
    if os.environ.get("SYRD658_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd658-before.") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    candidates(after)
    print(f"resume_provision_recorded_checkout_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
