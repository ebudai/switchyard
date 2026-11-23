#!/usr/bin/env python3
"""SYRD-854: the release smoke proves a release's served board and listener before a deploy switches to it.

bceacabf crash-looped syrd's listener 31 s after deploy-release, which exited 0. Every check before it ran the code
from the repository root, where `scripts.ticket_board` imports; the served processes run from the release's
`scripts/`, where it does not.

1. `scripts/release_smoke.py` on releases archived from this repository:
   - bceacabf: NOT READY, naming its function-level `scripts.ticket_board` imports;
   - HEAD: READY. Every step ran: the imports, the throwaway board, the dry runs of the declared workflow and of
     each composer, and the listener's Publisher outcome and claimed notice;
   - HEAD with such an import behind an ImportError handler that re-raises, which the import check leaves to the
     handler: NOT READY at the configure_workflow dry run, with "No module named 'scripts'";
   - HEAD whose Publisher fails before it records anything: the listener logs "was not recorded", which is no
     outcome: NOT READY;
   - HEAD whose listener never claims a notice: the Publisher's outcome alone is not READY.
2. Root's entries, through a stand-in for `runuser` that runs the same argv as this account: `run_as` reads the
   declared workflow as `postgres` and starts the smoke as the owner with `python3 -I`; `for_tenant` takes the
   database and interpreter from the tenant's board unit; with no declared workflow, any project name is READY on
   the release's example; a smoke killed outright leaves no process, and one told to stop also removes its
   directory; `preview` turns NOT READY into the reason the dry run is
   not ready, and passes a tenant already on the release. preview-upgrade's `_finish_upgrade` (driven through
   upgrade_phases_boundary_test's stand-ins) asks it on root's pinned dry run only, and fails on its reason.
3. The deploy's watch (`tenant_release_deploy.listener_holds`): a listener that stays up and logged LISTEN
   established holds; one that restarts, or stays up without LISTEN, does not.
4. SYRD-866, the live board's stages (tests/fixtures/syrd866_workflows: sapi's, mefp's and syrd's declared workflows
   and live stages, read from their boards): 79280dab's own smoke refuses sapi's, which has no `inspection`, as
   sapi's preview-upgrade did; on HEAD all three are READY. A workflow that omits a stage its live board has is
   still refused, a default stage or one the throwaway board never had, and root's entry reads the live stages.
   SYRD-867: one that removes a live stage explicitly, as an operator does, is not refused for omitting it; an empty
   live stage list, from root's read or from --stages, is NOT READY, naming the database or the file.
"""

from __future__ import annotations

import live_boundary  # noqa: E402

live_boundary.isolate()  # no live home, pane store or runtime directory, here or in a child (SYRD-819)

import getpass
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import release_smoke, tenant_release_deploy  # noqa: E402

BROKEN = "bceacabfeb57f42d5dd5b5de85daae07d0663952"  # the release that crash-looped syrd's listener
BASE = "79280dab3b326d41ae1048464516b4fe2b8c35ea"  # the release sapi's preview-upgrade found NOT READY (SYRD-866)
FIXTURES = ROOT / "tests" / "fixtures" / "syrd866_workflows"
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def archive(commit: str, into: Path) -> Path:
    into.mkdir(parents=True)
    subprocess.run(f"git -C {ROOT} archive {commit} scripts examples | tar -C {into} -x", shell=True, check=True)
    return into


def workflow() -> dict:
    cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
    cfg["project"] = "cerulean"
    return cfg


def smoke(release: Path) -> tuple[list[str], list[str]]:
    said: list[str] = []
    problems = release_smoke.smoke(release, project="cerulean", document=workflow(), say=said.append)
    return problems, said


def edit(path: Path, before: str, after: str) -> None:
    text = path.read_text()
    check(text.count(before) == 1, f"{path.name} holds `{before.strip()}` once")
    path.write_text(text.replace(before, after))


def test_releases(tmp: Path) -> Path:
    problems, said = smoke(archive(BROKEN, tmp / "broken"))
    joined = "\n".join(problems)
    check(problems and "publisher_stage.py:254 imports scripts.ticket_board.research_stage, and 'scripts' does not "
          "import in the release layout" in joined and "research_stage.py:227" in joined,
          f"bceacabf is NOT READY, naming its function-level scripts.ticket_board imports: {joined[:1500]}")

    head = archive("HEAD", tmp / "head")
    problems, said = smoke(head)
    check(not problems, f"HEAD is READY: {problems}")
    said = []
    problems = release_smoke.smoke(head, project="audit854", document=None, say=said.append)
    check(not problems and any("whose board is 'cerulean'" in line for line in said),
          f"with no declared workflow, any --project is READY on the example, under the example's own name: "
          f"{problems} {said}")
    for step in ("imports from the release layout", "built from the release's schema, migrations and grants",
                 "the release's board serves", "configure_workflow dry-runs the declared workflow",
                 "with the documentation gate", "with the Router", "with the Publisher", "with the research stage",
                 "with the plan gate", "its Publisher recorded", "claimed and routed the notice", "still running"):
        check(any(step in line for line in said), f"and the smoke said it ran this step ({step}): {said}")

    reraised = tmp / "reraised"
    shutil.copytree(head, reraised)
    edit(reraised / "scripts/ticket_board/publisher_stage.py",
         "        from .research_stage import PUBLISHER_FINDING_ACTIONS\n",
         "        try:\n            from scripts.ticket_board.research_stage import PUBLISHER_FINDING_ACTIONS\n"
         "        except ImportError:\n            raise\n")
    problems, said = smoke(reraised)
    joined = "\n".join(problems)
    check("configure_workflow's dry run of the declared workflow with the Publisher failed" in joined
          and "No module named 'scripts'" in joined and "imports from the release layout" in said[0],
          f"an import the check leaves to its handler fails the served board's dry run instead: {joined[:1500]}")

    unrecorded = tmp / "unrecorded"
    shutil.copytree(head, unrecorded)
    publisher = unrecorded / "scripts/ticket_board/publisher.py"
    text = publisher.read_text()
    marker = "def publish_ticket("
    start = text.index(marker)
    body = text.index('"""', text.index('"""', start) + 3) + 3  # after the function's docstring
    publisher.write_text(text[:body] + "\n    raise RuntimeError('the smoke test: this Publisher records nothing')\n"
                         + text[body:])
    problems, said = smoke(unrecorded)
    joined = "\n".join(problems)
    check("did not establish LISTEN and record a Publisher outcome" in joined and "was not recorded" in joined
          and "No module named" not in joined,
          f"a Publisher that records nothing is no outcome: the listener's pass is NOT READY: {joined[-1500:]}")

    unclaimed = tmp / "unclaimed"
    shutil.copytree(head, unclaimed)
    edit(unclaimed / "scripts/ticket_board/notify_listener.py",
         "    def _claim_notification(self, conn: Any) -> tuple[int, str, str, str, str, int] | None:\n",
         "    def _claim_notification(self, conn: Any) -> tuple[int, str, str, str, str, int] | None:\n"
         "        return None  # the smoke test: this listener never claims a notice\n")
    problems, said = smoke(unclaimed)
    joined = "\n".join(problems)
    check("its Publisher recorded" in "\n".join(said) and "did not claim and route the notice" in joined,
          f"a Publisher outcome without the notice claimed is NOT READY: {joined[-1500:]}")
    return head


def scratch_processes(scratch: str) -> list[str]:
    """Every process whose command line names the smoke's directory."""
    found = []
    for entry in Path("/proc").iterdir():
        try:
            line = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if entry.name.isdigit() and scratch in line:
            found.append(f"{entry.name}: {line[:160]}")
    return found


def test_killed(head: Path) -> None:
    """A smoke that is killed leaves no cluster, board or listener running; one told to stop, or whose parent (root's
    `runuser`, when `run_as` gives up) is killed, cleans up."""
    import shlex
    import signal
    import time

    # A release whose listener never gets going: left alone, the smoke would wait out its listener's hang guard, so
    # one gone within seconds of its parent was stopped by that death, not by finishing.
    check(release_smoke.LISTENER_SECONDS > 90, "the listener's hang guard outlasts the 60 s this test waits")
    hanging = head.parent / "hanging"
    if not hanging.exists():
        shutil.copytree(head, hanging)
        edit(hanging / "scripts/ticket_board/notify_listener.py", "def main(argv: list[str] | None = None) -> int:\n",
             "def main(argv: list[str] | None = None) -> int:\n    time.sleep(10 ** 6)  # the smoke test: it hangs\n")
    for sig in (signal.SIGKILL, signal.SIGTERM, "parent"):
        # "parent": a shell stands where runuser does, and is killed outright.
        argv = [sys.executable, "-I", "-B", str(ROOT / "scripts" / "release_smoke.py"), "--release",
                str(hanging if sig == "parent" else head), "--project", "audit854"]
        child = subprocess.Popen(argv if sig != "parent" else ["sh", "-c", shlex.join(argv) + "; :"],
                                 stdout=subprocess.PIPE, text=True)
        scratch = ""
        for line in child.stdout:
            if line.startswith("release smoke: working in "):
                scratch = line.split(" in ", 1)[1].strip()
            if "the release's board serves" in line:
                break
        check(scratch and scratch_processes(scratch), f"the smoke's cluster and board run in {scratch}")
        child.send_signal(signal.SIGKILL if sig == "parent" else sig)
        child.wait(timeout=120)
        deadline = time.monotonic() + 60  # well inside the listener's hang guard (release_smoke.LISTENER_SECONDS)
        while (scratch_processes(scratch) or (sig == "parent" and Path(scratch).exists())) \
                and time.monotonic() < deadline:
            time.sleep(0.5)
        left = scratch_processes(scratch)
        name = sig if sig == "parent" else sig.name
        check(not left, f"killed ({name}), the smoke leaves nothing running: {left}")
        if sig == signal.SIGTERM:
            check(child.returncode == 143 and not Path(scratch).exists(),
                  f"told to stop, it stops its processes and removes its directory: {child.returncode}")
        if sig == "parent":
            check(not Path(scratch).exists(), "its parent killed, it stops its processes and removes its directory")
        shutil.rmtree(scratch, ignore_errors=True)


class Runuser:
    """`runuser`, as the stand-in for root's: the postgres read answers the declared workflow; the owner's run is
    the same argv, run as this account."""

    def __init__(self, declared: str, stages: list[str] | None = None):
        self.declared, self.calls = declared, []
        self.stages = [s["name"] for s in json.loads(declared)["stages"]] if stages is None and declared else stages

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:4] == ["runuser", "-u", "postgres", "--"]:
            if "ticket_board.workflow_stages" in argv[-1]:
                return subprocess.CompletedProcess(argv, 0, "".join(f"{name}\n" for name in self.stages or []), "")
            return subprocess.CompletedProcess(argv, 0, self.declared, "")
        assert argv[:4] == ["runuser", "-u", getpass.getuser(), "--"], argv
        return subprocess.run(argv[4:], capture_output=True, text=True, timeout=kwargs.get("timeout"))


def test_root_entries(tmp: Path, head: Path) -> None:
    from scripts.ticket_board.project_provision import build_plan, render_board_unit

    plan = build_plan(project="cerulean", owner_user=getpass.getuser(), owner_home=tmp / "owner",
                      board_root=tmp / "owner" / "board", port=29118, commit_git_dir=tmp / "cache.git",
                      asset_dir=tmp / "assets", frame_dir=tmp / "frames")
    template = tmp / "cerulean-ticket-board.service"
    template.write_text(render_board_unit(plan))
    status = SimpleNamespace(provisioned_system_unit=template, owner_user=getpass.getuser(), unchanged=False,
                             target_sha="e" * 40)
    said: list[str] = []
    runner = Runuser(json.dumps(workflow()))
    problems = release_smoke.for_tenant(status, "cerulean", head, runner=runner, say=said.append)
    owner_run = runner.calls[-1]
    check(not problems and owner_run[4:6] == ["env", "-i"] and "-I" in owner_run and "--workflow" in owner_run
          and owner_run[owner_run.index("--release") + 1] == str(head),
          f"root's entry runs the smoke as the owner with python -I on the tenant's declared workflow: {problems} "
          f"{owner_run}")
    read = runner.calls[0]
    check(read[:6] == ["runuser", "-u", "postgres", "--", "env", "-i"] and "cerulean_ticket_board" in read
          and "SELECT document::text FROM ticket_board.workflow_configuration" in read,
          f"the declared workflow is read from the database the board unit names, as postgres: {read}")
    check(any(line.startswith("switchyard: release smoke: ") for line in said), f"and relays what it ran: {said}")
    silent = Runuser(json.dumps(workflow()))
    silent_run = lambda argv, **kwargs: (subprocess.CompletedProcess(argv, 0, "", "") if argv[2] != "postgres"  # noqa
                                         else silent(argv, **kwargs))
    problems = release_smoke.for_tenant(status, "cerulean", head, runner=silent_run, say=said.append)
    check(problems and "never said READY" in problems[0],
          f"a smoke that exits 0 without saying READY proved nothing: NOT READY: {problems}")

    from scripts.ticket_board import privileged_operations

    saved = privileged_operations.trusted_release_root
    try:
        privileged_operations.trusted_release_root = lambda commit: str(tmp / "broken")
        said = []
        why = release_smoke.preview(status, "cerulean", runner=Runuser(json.dumps(workflow())), say=said.append)
        check(why.startswith(f"the release smoke found {'e' * 40} NOT READY") and "'scripts' does not import" in why
              and any("NOT READY" in line for line in said),
              f"preview-upgrade's smoke makes a broken release the dry run's reason it is not ready: {why}")
        privileged_operations.trusted_release_root = lambda commit: str(head)
        said = []
        check(release_smoke.preview(status, "audit854", runner=Runuser(""), say=said.append) == ""
              and any(line.endswith("is READY") for line in said)
              and any("the tenant declares no workflow" in line for line in said),
              f"and a healthy one READY, on the example workflow, for a tenant named anything that declares none "
              f"(Audit: the example was refused under any name but its own): {said}")
        never = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("ran for a tenant already on the release"))
        check(release_smoke.preview(SimpleNamespace(unchanged=True, target_sha="e" * 40), "cerulean", runner=never,
                                    say=said.append) == "",
              "a tenant already on the release has nothing to switch to and is not smoked")
    finally:
        privileged_operations.trusted_release_root = saved


def tenant(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_live_stages(tmp: Path, head: Path) -> None:
    sapi = tenant("sapi")
    document = tmp / "sapi.json"
    document.write_text(json.dumps(sapi["document"]))
    base = archive(BASE, tmp / "base")
    done = subprocess.run([sys.executable, "-I", "-B", str(base / "scripts/release_smoke.py"), "--release", str(base),
                           "--project", "sapi", "--workflow", str(document)], capture_output=True, text=True,
                          timeout=release_smoke.STEP_SECONDS * 4)
    check(done.returncode == 1 and "release smoke: NOT READY" in done.stdout
          and "configure_workflow's dry run of the declared workflow failed (HTTP 400)" in done.stdout
          and "configuration omits an existing stage" in done.stdout and "inspection" not in sapi["live_stages"],
          f"79280dab's own smoke refuses sapi's declared workflow, which has no inspection: {done.stdout[-1500:]}")

    for name in ("sapi", "mefp", "syrd"):
        fixture, said = tenant(name), []
        problems = release_smoke.smoke(head, project=name, document=fixture["document"],
                                       live_stages=fixture["live_stages"], say=said.append)
        check(not problems and "release smoke: configure_workflow dry-runs the declared workflow" in said
              and any("its Publisher recorded" in line for line in said),
              f"{name}'s declared workflow, on its live board's stages, is READY: {problems[-2:]} {said}")
        gone = [stage for stage in ("inspection",) if stage not in fixture["live_stages"]]
        check(not gone or f"release smoke: the tenant's live board has no {gone[0]}; each trial removes it from the "
              "throwaway board" in said, f"and {name}'s says which default stages its trials remove: {said}")
    done = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/release_smoke.py"), "--release", str(head),
                           "--project", "sapi", "--workflow", str(document)], capture_output=True, text=True,
                          timeout=release_smoke.STEP_SECONDS * 4)
    check(done.returncode == 0 and "release smoke: READY" in done.stdout.splitlines(),
          f"by hand, with no --stages, the live stages are the declared workflow's own: {done.stdout[-1500:]}")

    for extra in ("inspection", "uat_delivery"):  # seeded on the throwaway board too; one it never had
        problems = release_smoke.smoke(head, project="sapi", document=sapi["document"],
                                       live_stages=sapi["live_stages"] + [extra], say=[].append)
        joined = "\n".join(problems)
        check("configuration omits an existing stage; preserve it or explicitly request removal" in joined
              and (extra == "inspection" or f"the declared workflow omits the live board's stage {extra}, which its "
                                             "board refuses" in joined)
              and "the declared workflow with the plan gate" in joined,
              f"a workflow that omits the live board's stage {extra} is still refused, every trial of it: {joined}")

    said = []
    runner = Runuser(json.dumps(sapi["document"]), sapi["live_stages"])
    problems = release_smoke.run_as(head, project="sapi", owner=getpass.getuser(), database="sapi_ticket_board",
                                    python=sys.executable, runner=runner, say=said.append)
    stage_read, owner_run = runner.calls[1], runner.calls[-1]
    check(not problems and stage_read[:6] == ["runuser", "-u", "postgres", "--", "env", "-i"]
          and stage_read[-1] == "SELECT name FROM ticket_board.workflow_stages ORDER BY rank"
          and "sapi_ticket_board" in stage_read and "--stages" in owner_run,
          f"root reads the live board's stages as postgres and hands them to the smoke: {problems} {runner.calls}")
    runner = Runuser(json.dumps(sapi["document"]), sapi["live_stages"] + ["uat_delivery"])
    problems = release_smoke.run_as(head, project="sapi", owner=getpass.getuser(), database="sapi_ticket_board",
                                    python=sys.executable, runner=runner, say=said.append)
    check(problems and "omits the live board's stage uat_delivery" in problems[0],
          f"and the smoke judges the workflow by them: {problems}")

    # SYRD-867: an explicit removal is how an operator removes a stage; it is no omission.
    from scripts.ticket_board.documentation_gate import without_documentation_gate

    removing = without_documentation_gate(sapi["document"])
    check("documentation" in sapi["live_stages"] and "documentation" in removing["remove_stages"]
          and "documentation" not in [stage["name"] for stage in removing["stages"]], "sapi's, removing its gate")
    said = []
    problems = release_smoke.smoke(head, project="sapi", document=removing, live_stages=sapi["live_stages"],
                                   say=said.append)
    check(not problems and "release smoke: configure_workflow dry-runs the declared workflow" in said,
          f"a workflow that removes a live stage explicitly is not refused for omitting it: {problems[-2:]}")

    # SYRD-867: judged against no live stages, nothing is ever omitted; a board always has stages, so refuse.
    runner = Runuser(json.dumps(sapi["document"]), [])
    problems = release_smoke.run_as(head, project="sapi", owner=getpass.getuser(), database="sapi_ticket_board",
                                    python=sys.executable, runner=runner, say=said.append)
    check(problems == ["read no live stages from sapi_ticket_board: a board always has stages, so the read is wrong"]
          and len(runner.calls) == 2, f"root's read of no live stages is NOT READY, before any smoke: {problems} "
                                      f"{runner.calls[2:]}")
    empty = tmp / "stages.json"
    empty.write_text("[]")
    done = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/release_smoke.py"), "--release", str(head),
                           "--project", "sapi", "--workflow", str(document), "--stages", str(empty)],
                          capture_output=True, text=True, timeout=release_smoke.STEP_SECONDS * 4)
    check(done.returncode == 1 and done.stdout.splitlines()[:2] == [
              "release smoke: NOT READY",
              f"read no live stages from {empty}: a board always has stages, so the file is wrong"],
          f"and so is --stages []: {done.stdout[-800:]}")


def test_preview_hook() -> None:
    import upgrade_phases_boundary_test as boundary
    from scripts import release_smoke as module

    reason = "the release smoke found v348 NOT READY: ticket_board/x.py:1 imports scripts.y"
    saved = module.preview
    try:
        for label, finish, dry, answer, asked, code in (
                ("NOT READY", boundary.Finish(), True, reason, True, 1),
                ("READY", boundary.Finish(), True, "", True, 0),
                ("applying", boundary.Finish(), False, reason, False, 0),
                ("not root", boundary.Finish(euid=1006), True, reason, False, 0),
                ("already blocked", boundary.Finish(blocked="syrd348 blocked"), True, reason, False, 1)):
            seen: list = []
            module.preview = lambda status, project, **_kw: seen.append((status, project)) or answer
            result, said, _, _ = boundary.u6(finish, dry_run=dry)
            check(result == code and (seen == [(finish.status, boundary.PROJECT)]) == asked
                  and (any(reason in line for line in said) == (asked and bool(answer))),
                  f"preview-upgrade's release smoke, {label}: asked {asked}, exit {code}: {result} {seen} {said}")
    finally:
        module.preview = saved


def test_watch(tmp: Path) -> None:
    config = SimpleNamespace(project="cerulean")
    from scripts import board_services

    def runner_for(samples):
        answers = iter(samples)

        last = [samples[-1]]

        def run(config, action, unit, **_kwargs):
            assert action.startswith("show --property=") and unit == "cerulean-ticket-board-notify-listener.service"
            return subprocess.CompletedProcess([], 0, next(answers, last[0]), "")  # then it stays as it last was
        return run

    steady = "ActiveState=active\nSubState=running\nNRestarts=4\nMainPID=900\n"
    log = tmp / "listener.log"
    saved = board_services._run_owner_user_systemctl
    try:
        for label, samples, written, held in (
                ("up, LISTEN logged", [steady] * 40, "old line\nINFO LISTEN ticket_board_state_transition established\n",
                 True),
                ("up, no LISTEN since the start", [steady] * 40, "INFO pane-state authority\n", False),
                ("restarting", [steady, steady, "ActiveState=activating\nSubState=auto-restart\nNRestarts=5\nMainPID=0\n"],
                 "INFO LISTEN ticket_board_state_transition established\n", False),
                ("restarted between two reads, active again", [steady, steady,
                                                              "ActiveState=active\nSubState=running\nNRestarts=5\nMainPID=901\n"],
                 "INFO LISTEN ticket_board_state_transition established\n", False)):
            log.write_text("INFO LISTEN ticket_board_state_transition established (before the start)\n")
            start = log.stat().st_size
            with log.open("a") as handle:
                handle.write(written)
            board_services._run_owner_user_systemctl = runner_for(samples)
            now = [0.0]
            said = tenant_release_deploy.listener_holds(
                config, runner=None, config_path=None, log=log, log_from=start, clock=lambda: now[0],
                sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
            check((said == "") == held, f"the watch: {label} -> {'holds' if held else 'does not hold'}: {said!r}")
    finally:
        board_services._run_owner_user_systemctl = saved


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd854-") as name:
        tmp = Path(name)
        head = test_releases(tmp)
        test_root_entries(tmp, head)
        test_live_stages(tmp, head)
        test_killed(head)
        test_preview_hook()
        test_watch(tmp)
    print(f"release_smoke_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
