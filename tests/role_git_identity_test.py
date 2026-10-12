#!/usr/bin/env python3
"""SYRD-458: each role's pane commits as that role, whatever the shared repository's config says.

On MEFP four ephemeral luna workers have worktrees of one control repository,
and a worktree reads its repository's single config. One worker's
`git config user.name "MEFP luna-2"` therefore became every worker's identity,
and luna-3's audited MEFP-29 commit is recorded as luna-2's.

The scenario runs twice -- on main from a `git archive` (`BEFORE`), then on
this tree -- and is the same both times:

- A MEFP-shaped project: a three-member `luna` pool, an `ops` role whose own
  config sets Git options, and a Director. Every worker's worktree belongs to
  one shared repository whose config already names `MEFP luna-2`.
- The project config is read by the real `load_project_config`. Each role's
  pane command is the one `tmux_new_session_args` builds, run through `sh -c`
  from the role's working directory, as tmux runs it. The CLI is a stand-in
  named `codex` that does what an agent does there: luna-2 first sets
  `user.name` itself, as the MEFP agent did, then each worker commits; the
  Director cherry-picks luna-3's commit, and Switchyard's own `owner_git`
  makes a machine commit from the Director's pane.

Nothing touches the host: every repository, config and session directory is in
a test-owned tree, HOME is that tree, system config is off, and no tmux, board
or network is used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import io
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: main before SYRD-458.
BEFORE = "e38cedf2"
CHECKS = 0

#: What a role's pane runs as its CLI: what an agent in each pane does with Git.
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
role = os.environ["TICKET_BOARD_CALLER_ROLE"]
out = Path(sys.argv[0]).resolve().parents[1] / "out"
def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()
def who(rev="HEAD"):
    return git("log", "-1", "--format=%an <%ae>|%cn <%ce>", rev)
seen = {"cwd": os.getcwd()}
if role == "luna-2":
    git("config", "user.name", "MEFP luna-2")  # what MEFP's luna-2 agent did: it writes the shared config
if role.startswith("luna-") or role == "ops":
    Path(f"{role}.txt").write_text(role + "\n")
    git("add", f"{role}.txt")
    git("commit", "-q", "-m", f"{role} work")
    seen.update(commit=git("rev-parse", "HEAD"), who=who(), origin=git("config", "--show-origin", "--show-scope", "--get", "user.name"),
                abbrev=subprocess.run(["git", "config", "core.abbrev"], capture_output=True, text=True).stdout.strip())
elif role == "director":
    picked = json.loads((out / "luna-3.json").read_text())["commit"]
    git("cherry-pick", picked)
    seen.update(picked=picked, pick=git("rev-parse", "HEAD"), pick_who=who(), source_who=who(picked))
    sys.path.insert(0, os.environ["SYRD458_ROOT"])
    from scripts import team_launcher as launcher
    Path("machine.txt").write_text("machine\n")
    launcher._commit_project_git_changes(owner_user=launcher.current_user_name(), project_dir=Path.cwd(),
                                         message="machine", runner=subprocess.run)
    seen.update(machine_who=who(), machine_subject=git("log", "-1", "--format=%s"))
(out / f"{role}.json").write_text(json.dumps(seen))
'''

#: The scenario, run in a fresh interpreter against one tree: `python3 -c SCENARIO <root> <work>`.
SCENARIO = r'''
import json, os, subprocess, sys
from pathlib import Path
root, work = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root))
from scripts import team_launcher as launcher
def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()
repo, trees, bin_dir, out = work / "control", work / "worktrees", work / "bin", work / "out"
for path in (trees, bin_dir, out):
    path.mkdir(parents=True)
subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
git(repo, "config", "user.name", "MEFP luna-2")
git(repo, "config", "user.email", "stellaris-agent@localhost")
(repo / "README").write_text("mefp\n")
git(repo, "add", "README")
git(repo, "commit", "-q", "-m", "base")
for role in ("luna-1", "luna-2", "luna-3", "ops"):
    git(repo, "worktree", "add", "-q", "-b", f"work/{role}", str(trees / role))
shared_before = (repo / ".git" / "config").read_bytes()
cli = bin_dir / "codex"
cli.write_text(os.environ["SYRD458_FAKE_CLI"])
cli.chmod(0o755)
(work / "layout.json").write_text(json.dumps({"Orientation": "Horizontal", "Widgets": [{"SessionRestoreId": 0}]}))
roles = [{"role": "director", "slot": 0, "cli": str(cli), "workdir": str(repo)}]
roles += [{"role": f"luna-{n}", "detached": True, "ephemeral": True, "cli": str(cli), "workdir": str(trees / f"luna-{n}")} for n in (1, 2, 3)]
roles += [{"role": "ops", "detached": True, "cli": str(cli), "workdir": str(trees / "ops"),
           "env": {"GIT_CONFIG_PARAMETERS": "'user.name=Ops Operator' 'core.abbrev=12'"}}]
document = {"project": "mefp458", "project_name": "Morfane's Epic Fix Patch", "ticket_prefix": "MEFP",
            "layout": "layout.json", "session_dir": str(work / "sessions"), "repository": str(repo),
            "worker_pool": {"name": "luna", "runtime": "codex", "size": 3}, "roles": roles}
config_path = work / "mefp458.json"
config_path.write_text(json.dumps(document))
config = launcher.load_project_config("mefp458", config_path=config_path)
by_role = {role.role: role for role in config.roles}
env = {"HOME": str(work), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1", "SYRD458_ROOT": str(root)}
ran = {}
for name in ("luna-1", "luna-2", "luna-3", "ops", "director"):
    role = by_role[name]
    argv = launcher.tmux_new_session_args(role, session_dir=work / "sessions" / name)
    proc = subprocess.run(["sh", "-c", argv[-1]], cwd=argv[argv.index("-c") + 1], env=env, capture_output=True, text=True, timeout=120)
    ran[name] = {"rc": proc.returncode, "stderr": proc.stderr[-600:]}
results = {name: json.loads((out / f"{name}.json").read_text()) for name in ran if (out / f"{name}.json").exists()}
print(json.dumps({"ran": ran, "results": results, "shared_unchanged": (repo / ".git" / "config").read_bytes() == shared_before,
                  "shared_name": git(repo, "config", "--file", str(repo / ".git" / "config"), "user.name"),
                  "feature_tip": git(repo, "rev-parse", "work/luna-3"),
                  "pane_env": {name: role.env.get("GIT_CONFIG_PARAMETERS", "") for name, role in by_role.items()}}))
'''


def check(condition: bool, label: str, detail: object = "") -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        print(f"FAILED CHECK: {label}", file=sys.stderr)
        raise AssertionError(f"{label}: {detail}")


def run(root: Path, work: Path) -> dict:
    env = {"HOME": str(work), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
           "SYRD458_FAKE_CLI": FAKE_CLI}
    proc = subprocess.run([sys.executable, "-B", "-c", SCENARIO, str(root), str(work)], env=env,
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise AssertionError(f"the scenario did not finish on {root}: {proc.stderr[-3000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def baseline_tree(dest: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", BEFORE], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    return dest


def ident(role: str) -> str:
    return f"MEFP {role} <{role}@mefp458.switchyard.invalid>"


def before(tmp: Path) -> None:
    seen = run(baseline_tree(tmp / "before-tree"), tmp / "before")
    results = seen["results"]
    check(all(r["rc"] == 0 for r in seen["ran"].values()) and set(results) == {"luna-1", "luna-2", "luna-3", "ops", "director"},
          "main: every pane ran its stand-in CLI to the end", seen["ran"])
    leaked = "MEFP luna-2 <stellaris-agent@localhost>"
    check([results[n]["who"] for n in ("luna-1", "luna-3")] == [f"{leaked}|{leaked}"] * 2,
          "main reproduces MEFP-29: luna-1 and luna-3 commit as luna-2, read from the shared config",
          {n: (results[n]["who"], results[n]["origin"]) for n in ("luna-1", "luna-3")})
    check(results["luna-3"]["origin"].startswith("local\t") and "control/.git/config" in results["luna-3"]["origin"],
          "main: luna-3's name comes from the shared repository's config", results["luna-3"]["origin"])
    check(results["director"]["pick_who"] == f"{leaked}|{leaked}",
          "main: the Director's cherry-pick keeps the wrong author and records luna-2 as committer too", results["director"])


def after(tmp: Path) -> None:
    seen = run(ROOT, tmp / "after")
    results = seen["results"]
    check(all(r["rc"] == 0 for r in seen["ran"].values()) and set(results) == {"luna-1", "luna-2", "luna-3", "ops", "director"},
          "every pane ran its stand-in CLI to the end", seen["ran"])
    for name in ("luna-1", "luna-2", "luna-3"):
        check(results[name]["who"] == f"{ident(name)}|{ident(name)}",
              f"{name} commits as itself, author and committer", results[name])
        check(results[name]["origin"] == f"command\tcommand line:\tMEFP {name}",
              f"{name}'s identity is its pane's, and git says where it came from", results[name]["origin"])
    check(seen["shared_unchanged"] and seen["shared_name"] == "MEFP luna-2",
          "nothing rewrote the shared config: it still says what luna-2's agent wrote there", seen["shared_name"])
    check(results["ops"]["who"] == "Ops Operator <ops@mefp458.switchyard.invalid>|Ops Operator <ops@mefp458.switchyard.invalid>"
          and results["ops"]["abbrev"] == "12",
          "a role's own GIT_CONFIG_PARAMETERS is kept and wins, its other settings too", results["ops"])
    director = results["director"]
    check(director["source_who"] == f"{ident('luna-3')}|{ident('luna-3')}" and director["picked"] == seen["feature_tip"],
          "luna-3's commit is untouched by the Director's integration", director)
    check(director["pick_who"] == f"{ident('luna-3')}|{ident('director')}",
          "the Director's cherry-pick keeps luna-3 as author and records the Director as committer", director)
    check(director["machine_who"] == "Switchyard <switchyard@localhost>|Switchyard <switchyard@localhost>"
          and director["machine_subject"] == "machine",
          "Switchyard's own owner_git commit, made from a role's pane, keeps its pinned identity", director)
    check(seen["pane_env"]["luna-3"] == "'user.name=MEFP luna-3' 'user.email=luna-3@mefp458.switchyard.invalid'"
          and seen["pane_env"]["ops"].endswith("'user.name=Ops Operator' 'core.abbrev=12'"),
          "the role env carries the identity first and the role's own settings after it", seen["pane_env"])


def units(tmp: Path) -> None:
    from types import SimpleNamespace

    from scripts import role_git_identity as m

    hostile = SimpleNamespace(ticket_prefix="A'B", project="x'y")
    env = m.role_git_identity_env(hostile, SimpleNamespace(role="r <1>\nb", env={}))
    repo = tmp / "units"
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   env={"HOME": str(tmp), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1"})
    got = subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "u"],
                         env={"HOME": str(tmp), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", **env},
                         capture_output=True, text=True)
    shown = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%an|%ae"], capture_output=True, text=True,
                           env={"HOME": str(tmp), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1"}).stdout.strip()
    check(got.returncode == 0 and shown == "A'B r 1 b|r1b@x'y.switchyard.invalid",
          "an apostrophe survives Git's own parsing, a line break is a space in the name and nothing in the address",
          (got.stderr, shown))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd458.") as raw:
        tmp = Path(raw)
        if os.environ.get("SYRD458_MUTATION_SKIP_BEFORE") != "1":
            before(tmp)
        after(tmp)
        units(tmp)
    print(f"role_git_identity_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
