#!/usr/bin/env python3
"""SYRD-528: an upgrade's way back is the tenant's, not the host's.

The rollback an upgrade printed came from the host's shared pointer as it
stood when the upgrade began: `sudo ln -sfn <that release> /opt/switchyard/
current`, then the upgrade pointed at it. But a tenant upgrade never moves the
shared pointer -- that is `install-shared-release`, the operator's -- so the
first command repointed every tenant on the host to undo nothing. And in the
supported order (the operator installs the shared release, then the Director
upgrades) the pointer already named the release being installed: MEFP's way
back from 7fe0aee was 7fe0aee. The note did hold the tenant's own earlier
release, as its staged tooling; nothing used it.

Now the note also records the tenant's deployed board build, and the way back
is the tenant's: `select-shared-release commit=<its earlier release>` (admin-
authenticated, root's own release only), dry run first, named only when root
holds that release, with what it does to the board said plainly. The host's
pointer is never part of it. Where no way back can be proved, it says so.

Real `record_release_rollback` and `release_rollback_commands`, in a sandbox
(shared install root, root's provision root, staging root, a board root), with
every printed command read by the real privileged-action parser and catalogue.
The reproduction runs main's code in a child, from a git worktree.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "4e912338f36c460c3e69f9db6e5dc4b5a3063372"  # main before SYRD-528's rollback fix
TENANT, HOST_OLD, NEW, BOARD = "a" * 40, "b" * 40, "c" * 40, "d" * 40
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def scenarios(tree: Path) -> dict:
    sys.path.insert(0, str(tree))
    from scripts import team_launcher as launcher

    seen: dict = {}

    def host(raw: Path, *, current: str | None, installed: tuple[str, ...], staged: str | None, board: str | None,
             marker_for: dict[str, str] | None = None):
        opt, etc, staging, board_root = raw / "opt", raw / "etc", raw / "staging", raw / "home" / "demo-ticketboard-live"
        for commit in installed:
            release = opt / "releases" / commit
            release.mkdir(parents=True)
            (release / launcher.SWITCHYARD_RELEASE_MARKER_NAME).write_text(
                json.dumps({"commit": (marker_for or {}).get(commit, commit)}), encoding="utf-8")
        opt.mkdir(parents=True, exist_ok=True)
        if current:
            os.symlink(opt / "releases" / current, opt / "current")
        os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = str(opt)
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(etc)
        pane_launcher = board_root / "current" / "scripts" / "pane-launch"
        if board:
            build = board_root / "releases" / board
            (build / "scripts").mkdir(parents=True)
            (build / ".pgu-deploy-sha").write_text(board + "\n", encoding="utf-8")
            os.symlink(build, board_root / "current")
        config = SimpleNamespace(project="demo", pane_launcher=pane_launcher)
        tooling = Path(launcher._staged_tooling_dir(config, staging))
        tooling.mkdir(parents=True, exist_ok=True)
        if staged:
            (tooling / launcher.SWITCHYARD_RELEASE_MARKER_NAME).write_text(json.dumps({"commit": staged}), encoding="utf-8")
        return config, staging, opt

    def run(name: str, *, upgrading_to: str = NEW, dry_run: bool = False, **layout) -> None:
        with tempfile.TemporaryDirectory(prefix="syrd528.") as raw:
            config, staging, opt = host(Path(raw), **layout)
            problems = launcher.record_release_rollback(
                config, release=SimpleNamespace(commit=upgrading_to), staging_root=staging, dry_run=dry_run,
                print_func=lambda _m: None)
            path = launcher.release_rollback_path("demo")
            note = json.loads(path.read_text()) if path.is_file() else None
            lines = launcher.release_rollback_commands("demo")
            seen[name] = {"problems": problems, "note": note, "lines": [l.replace(str(opt), "<opt>") for l in lines]}
            if name == "host installed first":
                # A retry after a failed phase: the host and the staged tooling
                # are now the new release, and the note keeps what it was before.
                (opt / "current").unlink()
                os.symlink(opt / "releases" / NEW, opt / "current")
                tooling = Path(launcher._staged_tooling_dir(config, staging))
                (tooling / launcher.SWITCHYARD_RELEASE_MARKER_NAME).write_text(json.dumps({"commit": NEW}), encoding="utf-8")
                launcher.record_release_rollback(config, release=SimpleNamespace(commit=NEW), staging_root=staging,
                                                 print_func=lambda _m: None)
                seen["retry"] = [l.replace(str(opt), "<opt>") for l in launcher.release_rollback_commands("demo")]

    # The supported order: the operator installed the new shared release first.
    run("host installed first", current=NEW, installed=(TENANT, NEW), staged=TENANT, board=TENANT)
    # The host moved to another release during the upgrade window; the tenant's own is different again.
    run("host switched during", current=HOST_OLD, installed=(TENANT, HOST_OLD, NEW), staged=TENANT, board=BOARD)
    run("tenant release not installed", current=NEW, installed=(NEW,), staged=TENANT, board=TENANT)
    run("tenant release marker disagrees", current=NEW, installed=(TENANT, NEW), staged=TENANT, board=TENANT,
        marker_for={TENANT: HOST_OLD})
    run("no prior proof", current=NEW, installed=(NEW,), staged=None, board=None)
    run("preview", current=NEW, installed=(TENANT, NEW), staged=TENANT, board=TENANT, dry_run=True)
    return seen


def parsed(line: str, tree: Path) -> tuple[str, dict, bool]:
    """A printed `switchyard privileged-action` line, read by the real parser and catalogue."""
    sys.path.insert(0, str(tree))
    from scripts import team_launcher as launcher
    from scripts.ticket_board import privileged_actions as pa

    argv = shlex.split(line)
    check(argv[:2] == ["switchyard", "privileged-action"], line)
    args = launcher._build_switchyard_privileged_action_parser().parse_args(argv[2:])
    values = dict(item.split("=", 1) for item in args.values)
    return args.action, pa.action_for(args.action).validate({"project": args.project, **values}), args.dry_run


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenarios":
        print("RESULT " + json.dumps(scenarios(Path(sys.argv[2]))))
        return 0

    def child(tree: Path) -> dict:
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenarios", str(tree)],
                              text=True, capture_output=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        assert proc.returncode == 0 and line, proc.stdout[-2000:] + proc.stderr[-2000:]
        return json.loads(line[len("RESULT "):])

    with tempfile.TemporaryDirectory(prefix="syrd528-before.") as raw:
        before_tree = Path(raw) / "tree"
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", "-q", str(before_tree), BEFORE], check=True)
        try:
            before = child(before_tree)
        finally:
            subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(before_tree)], check=False)
    first = before["host installed first"]["lines"]
    check(any(l.startswith("sudo ln -sfn '<opt>/releases/" + NEW) or l.startswith(f"sudo ln -sfn <opt>/releases/{NEW}") for l in first)
          and any(f"--deploy-ref {NEW}" in l for l in first),
          f"reproduced: with the host installed first, the way back repoints the host at the release being left: {first}")
    switched = before["host switched during"]["lines"]
    check(any("ln -sfn" in l and HOST_OLD in l for l in switched) and not any(TENANT in l for l in switched),
          f"reproduced: otherwise it goes to the host's old release, never the tenant's own: {switched}")
    check(before["host installed first"]["note"]["previous_staged_commit"] == TENANT,
          "reproduced: while the note already held the tenant's earlier release")

    now = child(ROOT)
    for name, result in now.items():
        if isinstance(result, dict):
            check(not any("ln -sfn" in l or l.startswith("sudo ") for l in result["lines"]),
                  f"{name}: no way back repoints the host's shared release: {result['lines']}")
    first = now["host installed first"]
    check(first["note"]["previous_staged_commit"] == TENANT and first["note"]["previous_board_commit"] == TENANT
          and first["note"]["previous_release_commit"] == NEW,
          f"the note tells the host's release, the tenant's tooling and its board apart: {first['note']}")
    commands = [l for l in first["lines"] if not l.startswith("#")]
    check(len(commands) == 2, f"a dry run, then the action: {first['lines']}")
    dry, applied = (parsed(line, ROOT) for line in commands)
    check(dry == ("select-shared-release", {"project": "demo", "commit": TENANT}, True)
          and applied == ("select-shared-release", {"project": "demo", "commit": TENANT}, False),
          f"back to the tenant's own release through the admin action, as the real parser reads it: {dry} {applied}")
    notes = [l for l in first["lines"] if l.startswith("#")]
    check(any(f"shared release ({NEW})" in l and "did not move it" in l for l in notes),
          f"the host's release is named as not part of it: {notes}")
    check(any("redeploys demo's board at " + TENANT in l and "not reversed" in l and "not verified" in l for l in notes),
          f"and what it does to the board is said, not claimed safe: {notes}")
    check(now["retry"] == first["lines"], f"a retry after a failed phase keeps the way back taken before it: {now['retry']}")
    switched = now["host switched during"]
    check([parsed(l, ROOT)[1]["commit"] for l in switched["lines"] if not l.startswith("#")] == [TENANT, TENANT]
          and switched["note"]["previous_board_commit"] == BOARD
          and any(f"it was on {BOARD}" in l for l in switched["lines"]),
          f"different host, tenant and board commits: back to the tenant's, the board's own named: {switched['lines']}")
    for name, why in (("tenant release not installed", "holds no installed release"),
                      ("tenant release marker disagrees", "holds no installed release"),
                      ("no prior proof", "no way back can be named")):
        lines = now[name]["lines"]
        check(not [l for l in lines if not l.startswith("#")] and any(why in l for l in lines),
              f"{name}: no command, and the reason instead: {lines}")
    check(now["preview"]["note"] is None and now["preview"]["problems"] == [] and now["preview"]["lines"] == [],
          f"a preview records nothing, as before: {now['preview']}")
    print(f"tenant_rollback_note_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
