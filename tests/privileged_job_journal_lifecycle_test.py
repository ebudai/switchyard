#!/usr/bin/env python3
"""SYRD-113 slice C: a tenant's journal is its own, kept for a bounded time, and a rollback is an outcome.

Slices A and B made each privileged run a job in root's rollout journal and put
it on its ticket. Three things were left:

- **Every account could read every tenant's journal.** Each attempt's
  directory was 0755 and its files 0444/0644, so MEFP's output was readable by
  Otto's account. A tenant's journal is now root-owned in the group its own
  board service runs with -- taken from root's own unit for that board, read
  without following a link -- with directories 0750 and files 0640 while live,
  0440 once finished. Root applies those modes when it writes, and repairs the
  whole journal every time it closes an attempt: provisioning's recorded
  packet, `upgrade-tenant`, `repair-boundary` and every other privileged action
  all close one, so existing records are repaired by the next root write.
- **Nothing was ever removed.** Attempts past retention (the newest 200 kept,
  older ones once finished 180 days ago) are removed, each with a chained
  `pruned` entry, so the index still verifies and `rollout-log` says why.
- **A rolled-back deploy read as a plain failure.** `deploy-release` whose
  health gate failed after the deploy, and whose rollback restored the previous
  release, now exits 3, and root records the job as `rolled_back`.

The test cannot be root, so where root's owner and a tenant group are needed it
uses its own uid and one of its own supplementary groups. It runs on main, from
a git archive in a child process, and on this tree.
"""

from __future__ import annotations

import grp
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "2c23f01012e472e6ae90f80c52370c9203572f3f"  # main before SYRD-113 slice C
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples", "config"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def modes(base: Path) -> dict:
    """Each path under a journal, as mode and group."""
    found = {}
    for path in sorted([base, *base.rglob("*")]):
        status = path.lstat()
        found[str(path.relative_to(base.parent))] = [f"{stat.S_IMODE(status.st_mode):o}", status.st_gid]
    return found


def journal_scenario(root: Path) -> dict:
    sys.path[:0] = [str(root), str(root / "scripts")]
    from scripts.ticket_board import rollout_journal as rj

    seen: dict = {"has": {name: hasattr(rj, name) for name in ("tenant_group", "repair_journal", "prune")}}
    tenant = next((gid for gid in os.getgroups() if gid != os.getgid()), None)
    seen["tenant_gid"] = tenant
    with tempfile.TemporaryDirectory(prefix="syrd113c-") as tmp:
        tmp = Path(tmp)
        journal = tmp / "journal"

        def run(project: str, *, group=None, status: str = "succeeded") -> str:
            kwargs = {} if group is None or not seen["has"]["tenant_group"] else {"group": group}
            attempt = rj.Attempt(project, ["true"], root=journal, **kwargs)
            attempt.open()
            attempt.write("stdout", "a line\n")
            if status != "running":
                attempt.close(status=status, exit_status=0 if status == "succeeded" else 1)
            return attempt.attempt

        # Legacy writes, as every attempt was written before.
        legacy = [run("mefp") for _ in range(2)]
        seen["legacy_modes"] = modes(journal / "mefp")
        if not seen["has"]["tenant_group"]:
            return seen

        # Where the group comes from: root's own unit for the tenant's board.
        units = tmp / "units"
        units.mkdir()
        name = grp.getgrgid(tenant).gr_name

        def unit(project: str, text: str, mode: int = 0o644) -> None:
            path = units / f"{project}-ticket-board.service"
            path.write_text(text)
            os.chmod(path, mode)

        unit("mefp", f"[Unit]\nDescription=x\n\n[Service]\nUser=boardsvc\nGroup={name}\nSupplementaryGroups={name}\n")
        unit("nogroup", "[Service]\nUser=boardsvc\n")
        unit("wronggroup", f"[Install]\nGroup={name}\n")
        unit("writable", f"[Service]\nGroup={name}\n", 0o666)
        unit("unknown", "[Service]\nGroup=no-such-group-anywhere\n")
        (units / "linked-ticket-board.service").symlink_to(units / "mefp-ticket-board.service")
        me = os.getuid()
        seen["groups"] = {project: rj.tenant_group(project, unit_dir=units, owner_uid=me)
                          for project in ("mefp", "nogroup", "wronggroup", "writable", "unknown", "linked", "absent", "_host")}
        seen["groups"]["not root's"] = rj.tenant_group("mefp", unit_dir=units)

        # A tenant's live and finished attempts, written in its group.
        live = rj.Attempt("mefp", ["true"], root=journal, group=tenant)
        live.open()
        live.write("stdout", "running\n")
        seen["live_modes"] = {k: v for k, v in modes(journal / "mefp").items() if live.attempt in k}
        # Closing it repairs the whole journal: the two legacy attempts too.
        live.close(status="succeeded", exit_status=0)
        seen["after_close"] = modes(journal / "mefp")
        seen["close_repairs"] = live.repairs
        seen["repair_again"] = rj.repair_journal("mefp", root=journal, group=tenant, owner_uid=me)

        # A link in the journal is left alone and reported; a running attempt keeps its live modes.
        (journal / "mefp" / "0099-20261012T000000Z").symlink_to(journal / "mefp" / legacy[0])
        running = run("mefp", status="running")
        os.chmod(journal / "mefp" / running / "result.json", 0o644)
        seen["repair_with_link"] = rj.repair_journal("mefp", root=journal, group=tenant, owner_uid=me)
        seen["running_modes"] = {k: v for k, v in modes(journal / "mefp").items() if running in k}
        (journal / "mefp" / "0099-20261012T000000Z").unlink()
        seen["no_group"] = rj.repair_journal("nogroup", root=journal, group=None, owner_uid=me)

        # Retention: four old finished attempts beyond the newest three, one of them still running.
        project = "retained"
        names = [run(project, status="running")] + [run(project) for _ in range(6)]
        old = time.time() - 200 * 86400
        for attempt_name in names[:5]:
            for path in (journal / project / attempt_name).rglob("*"):
                os.utime(path, (old, old), follow_symlinks=False)
            os.utime(journal / project / attempt_name, (old, old))
        seen["defaults"] = [rj.RETENTION_KEEP, rj.RETENTION_DAYS]
        seen["pruned"] = rj.prune(project, root=journal, keep=3, days=180)
        seen["left"] = sorted(p.name for p in (journal / project).iterdir() if p.is_dir())
        seen["names"] = names
        seen["verify"] = rj.verify_index(project, root=journal)
        seen["listed"] = rj.format_attempts(project, rj.attempts(project, root=journal), [])
        seen["next"] = run(project)
    return seen


def deploy_scenario(root: Path) -> dict:
    """`deploy-release` through the helper, the deploy's rollback exit as the script now gives it."""
    sys.path[:0] = [str(root), str(root / "tests")]
    import catalogued_deploy_release_test as deploy

    deploy.install_test_dispatch()
    original = deploy.Host.__call__

    def rolled_back(self, argv, **kwargs):
        result = original(self, argv, **kwargs)
        # The script's rolled-back exit: the deploy failed its health gate and the board is back where it was.
        if "deploy-restart" in " ".join(str(part) for part in argv) and self.outcome["deploy"] == "fail":
            result.returncode = 3
        return result

    deploy.Host.__call__ = rolled_back
    seen = deploy.scenarios(root)
    return {name: {"code": run["code"], "closed": run["attempts"][-1].get("closed") if run["attempts"] else None,
                   "said": run["said"][-400:]}
            for name, run in seen.items() if name.startswith("deploy fails")}


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] in ("--journal", "--deploy"):
        tree = Path(sys.argv[2])
        scenario = journal_scenario if sys.argv[1] == "--journal" else deploy_scenario
        print("RESULT " + json.dumps(scenario(tree)))
        return 0

    def child(mode: str, tree: Path) -> dict:
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), mode, str(tree)], text=True,
                              capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        assert proc.returncode == 0 and line, proc.stdout[-3000:] + proc.stderr[-3000:]
        return json.loads(line[len("RESULT "):])

    with tempfile.TemporaryDirectory(prefix="syrd113c.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        before = {"journal": child("--journal", before_root), "deploy": child("--deploy", before_root)}
    after = {"journal": child("--journal", ROOT), "deploy": child("--deploy", ROOT)}
    b, a = before["journal"], after["journal"]
    tenant = a["tenant_gid"]
    check(tenant is not None, "this suite needs an account with a supplementary group to stand in for a tenant's")

    def by_kind(found: dict) -> dict:
        """Each path's [mode, group], keyed by what it is rather than its attempt id."""
        kinds: dict = {}
        for path, value in found.items():
            parts = path.split("/")
            kind = "journal" if len(parts) == 1 else parts[-1] if not re.fullmatch(r"[0-9]{4,}-.*", parts[-1]) else "attempt"
            kinds.setdefault(kind, set()).add(tuple(value))
        return {kind: [list(value) for value in sorted(values)] for kind, values in kinds.items()}

    legacy = {"journal": [["755", os.getgid()]], "attempt": [["755", os.getgid()]], "index.jsonl": [["644", os.getgid()]],
              "result.json": [["444", os.getgid()]], "stdout.log": [["444", os.getgid()]], "stderr.log": [["444", os.getgid()]]}
    check(not any(b["has"].values()) and by_kind(b["legacy_modes"]) == legacy,
          f"main: every attempt is readable by every account, and nothing repairs or prunes it: {by_kind(b['legacy_modes'])}")
    check(by_kind(a["legacy_modes"]) == legacy,
          "a writer that is not root still writes the legacy modes: only root can give a file to a tenant's group")
    check(a["groups"] == {"mefp": tenant, "nogroup": None, "wronggroup": None, "writable": None, "unknown": None,
                          "linked": None, "absent": None, "_host": None, "not root's": None},
          f"the group is the one root's own unit for the tenant's board names in [Service], and only when that unit "
          f"is a regular file root owns and nobody else may write: {a['groups']}")
    check(by_kind(a["live_modes"]) == {"attempt": [["750", tenant]], "result.json": [["640", tenant]],
                                       "stdout.log": [["640", tenant]], "stderr.log": [["640", tenant]]},
          f"a tenant's live attempt is root's and its group's, and nobody else's: {a['live_modes']}")
    check(by_kind(a["after_close"]) == {"journal": [["750", tenant]], "attempt": [["750", tenant]],
                                        "index.jsonl": [["640", tenant]], "result.json": [["440", tenant]],
                                        "stdout.log": [["440", tenant]], "stderr.log": [["440", tenant]]}
          and len(a["close_repairs"]) == 8 and a["repair_again"] == [],
          f"closing it leaves the whole journal in the tenant's modes, the two legacy attempts repaired with it, "
          f"and a second repair finds nothing: {a['close_repairs'][:2]}")
    check(any("0099-20261012T000000Z cannot be opened without following a link; left alone" in line
              for line in a["repair_with_link"])
          and by_kind(a["running_modes"])["result.json"] == [["640", tenant]]
          and a["no_group"] == ["nogroup's board unit names no group, so its rollout journal stays readable by every account"],
          f"a link is left alone and reported, a running attempt keeps its live modes, and a tenant whose unit names "
          f"no group is reported rather than guessed: {a['repair_with_link']}")
    names = a["names"]
    # Seven attempts, the newest three kept; of the four older, three finished long ago and one still running.
    check(a["defaults"] == [200, 180] and a["pruned"] == names[1:4] and a["left"] == [names[0], *names[4:]]
          and a["verify"] == [] and a["listed"].count("removed by retention at") == 3
          and a["next"].startswith("0008-"),
          f"past retention, finished attempts are removed -- never a running one, never the newest -- each said in a "
          f"chained entry, so the journal still verifies and numbering goes on: {a['pruned']} left {a['left']}")
    bd, ad = before["deploy"], after["deploy"]
    check(bd["deploy fails, board unchanged"]["code"] == 1 and bd["deploy fails, board unchanged"]["closed"]["status"] == "failed",
          f"main: a deploy its rollback undid exits 1 and is recorded failed: {bd['deploy fails, board unchanged']['closed']}")
    check(ad["deploy fails, board unchanged"]["code"] == 3
          and ad["deploy fails, board unchanged"]["closed"] == {"status": "rolled_back", "exit_status": 3,
                                                                "detail": "org.switchyard.privileged.deploy-release"}
          and ad["deploy fails, board moved"]["code"] == 1 and ad["deploy fails, board moved"]["closed"]["status"] == "failed",
          f"a deploy its rollback undid exits 3 and is recorded rolled_back; one that left the board moved is a "
          f"failure: {ad}")
    service = (ROOT / "scripts" / "ticket-board-service.sh").read_text(encoding="utf-8")
    deploy_block = service[service.index("    if ! smoke_check_http; then"):service.index('    log "deployed $deployed_sha')]
    calls = re.findall(r"rollback_live_service[^\n]*\n(?:\s*start_listener_after_upgrade\n)?\s*(exit [^\n]*)", deploy_block)
    sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
    from scripts import tenant_release_deploy
    from scripts.ticket_board import privileged_operations
    check("ROLLED_BACK_EXIT=3" in service and calls == ['exit "$ROLLED_BACK_EXIT"'] * 5 and "exit 1" not in deploy_block
          and tenant_release_deploy.ROLLED_BACK_EXIT == privileged_operations.ROLLED_BACK_EXITS["deploy-release"] == 3,
          f"each health gate's rollback ends the deploy with the one rolled-back exit, and the script, the deploy and "
          f"the helper agree what it is: {calls}")
    print(f"privileged_job_journal_lifecycle_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
