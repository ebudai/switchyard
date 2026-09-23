#!/usr/bin/env python3
"""SYRD-233 live UAT round 2: what the migrated launch must not do.

The bridge repair worked -- the window opened, the tabs crossed -- and three
other things went wrong on live mefp:

* `<session dir>/roles/main` and `.../roles/ops` were root's after a root-run
  launch created them, so the project account got `permission denied` writing
  the provider-state record for exactly those two roles;
* the launcher read that as "no record", called both roles stale, and killed
  two panes that had been up for hours (Main 3090567 -> 3452836, Ops
  3090859 -> 3452844) -- and could not write the record afterwards either, so
  the next launch would do it again;
* the replacement window showed four ordinary shells, because Konsole falls
  back to the profile shell when it cannot start a tab's `Command`, and the
  tab's command is a `switchyard-pane-window` that ships beside the tenant's
  configured pane launcher.

The ownership cases use a user namespace, because a sandbox cannot make a
directory this process does not own -- and "the account cannot write here" is
the whole condition.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from team_launcher_test_helpers import *  # noqa: F401,F403
from scripts import presentation_controller as presentation  # noqa: E402

CHECKS = 0
PROJECT = "stellar"
ROLES = ("director", "main", "ops", "audit")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tenant(tmp: Path, *, owner: str | None = None):
    """A four-role tenant whose state store lives inside the sandbox."""
    owner = owner or team_launcher.current_user_name()
    layout = tmp / f"{PROJECT}-layout.json"
    layout.write_text(
        json.dumps({
            "Orientation": "Horizontal",
            "Widgets": [{"Command": "", "SessionRestoreId": i, "WorkingDirectory": ""} for i in range(4)],
        }),
        encoding="utf-8",
    )
    (tmp / "repo").mkdir(exist_ok=True)
    config_path = tmp / f"{PROJECT}.json"
    config_path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "project_name": "Stellar Fix",
                "layout": str(layout),
                "repository": str(tmp / "repo"),
                "run_as_user": owner,
                "session_dir": str(tmp / "state" / "pane-sessions"),
                "role_state_isolation": True,
                "desktop_access": {"mode": "headless"},
                "roles": [
                    {
                        "role": role,
                        "slot": index,
                        "cli": ["claude"],
                        "live_commands": ["claude"],
                        "target": f"{PROJECT}-{role}:0.0",
                        "tmux_session": f"{PROJECT}-{role}",
                    }
                    for index, role in enumerate(ROLES)
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return team_launcher.load_project_config(PROJECT, config_path), config_path


# --------------------------------------------------------------------------
# Unknown provider state is not stale provider state
# --------------------------------------------------------------------------


def test_a_readable_store_still_decides_staleness_the_way_it_did() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-stale-ok.") as raw:
        tmp = Path(raw)
        config, _ = tenant(tmp)
        role = config.roles[1]
        home = tmp / "owner-home"
        home.mkdir()
        generation = team_launcher.provider_state_generation("claude", owner_home=home)

        check(
            team_launcher.provider_state_store_problem(config, role) == "",
            "a store this account can write reports no problem",
        )
        check(
            [r.role for r in team_launcher.roles_with_stale_provider_runtime(
                config, [role], owner_home=home)] == [role.role],
            "a role with no record at all is still stale -- one restart settles it",
        )
        team_launcher.record_provider_state_generation(config, role, generation)
        check(
            team_launcher.roles_with_stale_provider_runtime(config, [role], owner_home=home) == [],
            "and once recorded it is not",
        )
        team_launcher.record_provider_state_generation(config, role, "something-older")
        check(
            [r.role for r in team_launcher.roles_with_stale_provider_runtime(
                config, [role], owner_home=home)] == [role.role],
            "a record that disagrees is stale, which is the behaviour this keeps",
        )


UNWRITABLE_CHILD = r'''
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]) / "tests"))
from scripts import team_launcher
import legacy_presentation_launch_test as suite

tmp = Path(sys.argv[2])
owner_uid, owner_gid = 1002, 1002
config, config_path = suite.tenant(tmp, owner="stellaris-agent")
home = tmp / "owner-home"; home.mkdir()
os.chown(home, owner_uid, owner_gid)

# The store as a root-run launch left it on mefp: the roles directory and two
# role directories are root's, and the project account cannot write them.
store = Path(config.session_dir)
store.mkdir(parents=True, exist_ok=True)
os.chown(store, owner_uid, owner_gid)
(store / "roles").mkdir(exist_ok=True)
os.chown(store / "roles", owner_uid, owner_gid)
for role in ("main", "ops"):
    (store / "roles" / role).mkdir(exist_ok=True)
    os.chown(store / "roles" / role, 0, 0)
    os.chmod(store / "roles" / role, 0o700)
for role in ("director", "audit"):
    (store / "roles" / role).mkdir(exist_ok=True)
    os.chown(store / "roles" / role, owner_uid, owner_gid)
# And one role whose DIRECTORY is the tenant's but whose record is root's and
# unreadable: writable store, unanswerable question, still not a reason to kill
# a live pane.
sealed = store / "roles" / "audit" / "audit.provider-state.json"
sealed.write_text(json.dumps({"generation": "whatever"}))
os.chown(sealed, 0, 0)
os.chmod(sealed, 0o600)

# Asked as the project account, which is who the launcher runs as. Imports
# happen here, as root, because the tenant account cannot read the source tree;
# the fork then drops to it and answers over a pipe, so every check below is
# made with exactly that account's rights and the kernel deciding.
def as_owner(question):
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        code = 0
        try:
            os.setgroups([])
            os.setresgid(owner_gid, owner_gid, owner_gid)
            os.setresuid(owner_uid, owner_uid, owner_uid)
            payload = json.dumps(question()).encode("utf-8")
        except BaseException as exc:  # noqa: BLE001 - reported over the pipe
            payload = json.dumps({"__error__": f"{type(exc).__name__}: {exc}"}).encode("utf-8")
            code = 0
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(code)
    os.close(write_fd)
    chunks = []
    while True:
        chunk = os.read(read_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_fd)
    os.waitpid(pid, 0)
    answer = json.loads(b"".join(chunks).decode("utf-8"))
    if isinstance(answer, dict) and "__error__" in answer:
        raise SystemExit(f"probe failed: {answer['__error__']}")
    return answer


problems = as_owner(
    lambda: {r.role: team_launcher.provider_state_store_problem(config, r) for r in config.roles}
)
# Asked BY ROOT, which is how `switchyard new` and `switchyard upgrade` run.
# Root can write a root-owned directory and read a 0600 file of its own; the
# account that actually writes the record can do neither, and that is the
# answer that decides whether a live pane is ended.
as_root = {r.role: team_launcher.provider_state_store_problem(config, r) for r in config.roles}

stale = as_owner(
    lambda: [
        r.role
        for r in team_launcher.roles_with_stale_provider_runtime(
            config, config.roles, owner_home=home
        )
    ]
)


def kept_without_killing():
    def never(*args, **kwargs):
        raise AssertionError("killed a pane: " + repr(args))

    kept, _unreconciled = team_launcher._drop_roles_with_stale_provider_runtime(
        config,
        [r for r in config.roles if r.role in ("main", "ops")],
        owner_home=home,
        runner=never,
        print_func=lambda _line: None,
    )
    return [r.role for r in kept]


kept = as_owner(kept_without_killing)


def audit_kept_without_killing():
    def never(*args, **kwargs):
        raise AssertionError("killed a pane: " + repr(args))

    kept_roles, _unreconciled = team_launcher._drop_roles_with_stale_provider_runtime(
        config,
        [r for r in config.roles if r.role == "audit"],
        owner_home=home,
        runner=never,
        print_func=lambda _line: None,
    )
    return [r.role for r in kept_roles]


sealed_kept = as_owner(audit_kept_without_killing)


def what_it_said():
    lines = []
    team_launcher._drop_roles_with_stale_provider_runtime(
        config,
        [r for r in config.roles if r.role == "main"],
        owner_home=home,
        runner=lambda *a, **k: None,
        print_func=lines.append,
    )
    return lines


said = as_owner(what_it_said)

# The repair, as root, then the same questions again.
printed = []
repaired = team_launcher.repair_role_state_ownership(config, print_func=printed.append)
after = as_owner(
    lambda: {r.role: team_launcher.provider_state_store_problem(config, r) for r in config.roles}
)
def record_and_read_back():
    role = [r for r in config.roles if r.role == "main"][0]
    team_launcher.record_provider_state_generation(config, role, "gen-1")
    return team_launcher.recorded_provider_state_generation(config, role)


recorded = as_owner(record_and_read_back)
owners = {p.name: os.stat(p).st_uid for p in (store / "roles").iterdir()}
print(json.dumps({
    "problems": problems, "as_root": as_root, "stale": stale, "kept": kept, "said": said,
    "sealed_kept": sealed_kept,
    "repaired": repaired, "printed": printed, "after": after, "recorded": recorded,
    "owners": owners,
}))
'''


ESCAPE_CHILD = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]) / "tests"))
from scripts import team_launcher
import legacy_presentation_launch_test as suite

tmp = Path(sys.argv[2])
owner_uid, owner_gid = 1002, 1002
out = {}


def sentinel(where):
    """A file outside the tenant's state store that must never be chowned."""
    where.mkdir(parents=True, exist_ok=True)
    os.chown(where, 0, 0)
    victim = where / "victim"
    victim.write_text("do not touch")
    os.chown(victim, 0, 0)
    return victim


def planted(link, target):
    """A symlink the TENANT could have made: theirs, in a directory of theirs."""
    link.parent.mkdir(parents=True, exist_ok=True)
    os.chown(link.parent, owner_uid, owner_gid)
    link.symlink_to(target)
    os.chown(link, owner_uid, owner_gid, follow_symlinks=False)


def reported(config):
    return [[[str(p), r] for p, r in part] for part in
            team_launcher.role_state_ownership_problems(config)]


def root_owned_role_dir(config, role_name):
    """One genuine repair to do, so a refusal is provably a refusal to start."""
    role = [r for r in config.roles if r.role == role_name][0]
    directory = Path(team_launcher.role_session_dir(config, role))
    directory.mkdir(parents=True, exist_ok=True)
    os.chown(directory, 0, 0)
    return directory


# A -- the SESSION root itself is a symlink pointing out of the tenant's state.
a = tmp / "a"; a.mkdir(); os.chown(a, owner_uid, owner_gid)
config_a, _ = suite.tenant(a, owner="stellaris-agent")
victim_a = sentinel(tmp / "outside-a")
planted(Path(config_a.session_dir), tmp / "outside-a")
said_a = []
out["a_repaired"] = team_launcher.repair_role_state_ownership(config_a, print_func=said_a.append)
out["a_said"] = said_a
out["a_victim_uid"] = os.lstat(victim_a).st_uid
out["a_problems"], out["a_refusals"] = reported(config_a)

# B -- the session root is real, but one ROLE root is a symlink out of it, and
# another role directory genuinely needs the repair.
b = tmp / "b"; b.mkdir(); os.chown(b, owner_uid, owner_gid)
config_b, _ = suite.tenant(b, owner="stellaris-agent")
root_b = Path(config_b.session_dir)
root_b.mkdir(parents=True, exist_ok=True)
os.chown(root_b, owner_uid, owner_gid)
needs_repair_b = root_owned_role_dir(config_b, "main")
victim_b = sentinel(tmp / "outside-b")
ops_role = [r for r in config_b.roles if r.role == "ops"][0]
planted(Path(team_launcher.role_session_dir(config_b, ops_role)), tmp / "outside-b")
said_b = []
out["b_repaired"] = team_launcher.repair_role_state_ownership(config_b, print_func=said_b.append)
out["b_said"] = said_b
out["b_victim_uid"] = os.lstat(victim_b).st_uid
out["b_untouched_uid"] = os.lstat(needs_repair_b).st_uid
out["b_problems"], out["b_refusals"] = reported(config_b)
said_b_dry = []
out["b_dry_repaired"] = team_launcher.repair_role_state_ownership(
    config_b, dry_run=True, print_func=said_b_dry.append
)
out["b_dry_said"] = said_b_dry

# C -- a symlink INSIDE a real role directory. The link is the tenant's and is
# given back as a link; what it points at is not the tenant's to be given.
c = tmp / "c"; c.mkdir(); os.chown(c, owner_uid, owner_gid)
config_c, _ = suite.tenant(c, owner="stellaris-agent")
root_c = Path(config_c.session_dir)
root_c.mkdir(parents=True, exist_ok=True)
os.chown(root_c, owner_uid, owner_gid)
(root_c / "roles").mkdir(exist_ok=True)
os.chown(root_c / "roles", owner_uid, owner_gid)
needs_repair_c = root_owned_role_dir(config_c, "main")
for name in ("director", "ops", "audit"):
    directory = root_c / "roles" / name
    directory.mkdir(exist_ok=True)
    os.chown(directory, owner_uid, owner_gid)
victim_c = sentinel(tmp / "outside-c")
escape = root_c / "roles" / "audit" / "escape"
escape.symlink_to(tmp / "outside-c")
os.chown(escape, 0, 0, follow_symlinks=False)
said_c = []
out["c_repaired"] = team_launcher.repair_role_state_ownership(config_c, print_func=said_c.append)
out["c_said"] = said_c
out["c_victim_uid"] = os.lstat(victim_c).st_uid
out["c_victim_dir_uid"] = os.lstat(tmp / "outside-c").st_uid
out["c_link_uid"] = os.lstat(escape).st_uid
out["c_repaired_uid"] = os.lstat(needs_repair_c).st_uid

# D -- a symlink ROOT put in the path, in a directory only root can write.
# Root's own layout is followed; refusing it would refuse every such host.
d = tmp / "d"; d.mkdir(); os.chown(d, owner_uid, owner_gid)
config_d, _ = suite.tenant(d, owner="stellaris-agent")
root_d = Path(config_d.session_dir)
real_d = tmp / "elsewhere-d"
real_d.mkdir()
os.chown(real_d, owner_uid, owner_gid)
root_d.parent.mkdir(parents=True, exist_ok=True)
os.chown(root_d.parent, 0, 0)
os.chmod(root_d.parent, 0o755)
root_d.symlink_to(real_d)
os.chown(root_d, 0, 0, follow_symlinks=False)
stranded_d = root_owned_role_dir(config_d, "main")
said_d = []
out["d_repaired"] = team_launcher.repair_role_state_ownership(config_d, print_func=said_d.append)
out["d_said"] = said_d
out["d_repaired_uid"] = os.lstat(stranded_d).st_uid

print(json.dumps(out))
'''


SURVIVAL_CHILD = r'''
import json, os, shutil, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]) / "tests"))
from scripts import team_launcher
import legacy_presentation_launch_test as suite

tmp = Path(sys.argv[2])
owner_uid, owner_gid = 1002, 1002
config, _ = suite.tenant(tmp, owner="stellaris-agent")
home = tmp / "owner-home"; home.mkdir(); os.chown(home, owner_uid, owner_gid)

# The store exactly as the live failure left it: main and ops are root's, so
# neither could write its record after it started; director's is the tenant's
# and has no record either, which stays stale (SYRD-191 is not being widened).
store = Path(config.session_dir)
store.mkdir(parents=True, exist_ok=True); os.chown(store, owner_uid, owner_gid)
(store / "roles").mkdir(exist_ok=True); os.chown(store / "roles", owner_uid, owner_gid)
for name in ("main", "ops"):
    d = store / "roles" / name
    d.mkdir(exist_ok=True); os.chown(d, 0, 0); os.chmod(d, 0o700)
for name in ("director", "audit"):
    d = store / "roles" / name
    d.mkdir(exist_ok=True); os.chown(d, owner_uid, owner_gid)

# Real processes with the configured CLI's name, so "is it running claude?" is
# answered by ps against a live process tree rather than by a stub.
bindir = tmp / "bin"; bindir.mkdir(); os.chown(bindir, owner_uid, owner_gid)
fake_cli = bindir / "claude"
shutil.copy2(shutil.which("sleep"), fake_cli)
os.chmod(fake_cli, 0o755)
live = {}
for name in ("main", "ops", "director"):
    live[name] = subprocess.Popen([str(fake_cli), "300"]).pid
other = subprocess.Popen([shutil.which("sleep"), "300"])  # not the configured CLI
time.sleep(0.4)

roles = {r.role: r for r in config.roles}
kills = []


def runner_for(pids):
    def run(args, **kwargs):
        argv = list(args)
        if "kill-session" in argv:
            kills.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if "display-message" in argv:
            target = argv[argv.index("-t") + 1]
            role = target.split("-", 1)[1].split(":")[0]
            if "#{pane_pid}" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=f"{pids.get(role, 0)}\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="claude\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    return run


pids = dict(live)
runner = runner_for(pids)
out = {"live_pids": live}
out["before"] = {n: bool(team_launcher.provider_state_store_problem(config, roles[n]))
                 for n in ("main", "ops", "director")}

# Step 1 -- `sudo switchyard upgrade`: repair the ownership AND finish the
# record each of those roles could not write.
said = []
out["restored"] = team_launcher.restore_interrupted_role_state(
    config, runner=runner, owner_home=home, print_func=said.append
)
out["said"] = said
out["records"] = {
    n: team_launcher.recorded_provider_state_generation(config, roles[n])
    for n in ("main", "ops", "director")
}
out["generation"] = team_launcher.provider_state_generation("claude", owner_home=home)
out["record_uids"] = {}
for n in ("main", "ops"):
    record = Path(team_launcher.role_session_dir(config, roles[n])) / f"{n}.provider-state.json"
    out["record_uids"][n] = os.lstat(record).st_uid if record.exists() else None


# Step 2 -- `switchyard <project>`: the ordinary launch, as the tenant account.
def launch():
    kept, _unreconciled = team_launcher._drop_roles_with_stale_provider_runtime(
        config,
        [roles[n] for n in ("main", "ops", "director")],
        owner_home=home,
        runner=runner_for(pids),
        print_func=lambda _line: None,
    )
    return {"kept": [r.role for r in kept], "kills": kills}


read_fd, write_fd = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(read_fd)
    try:
        os.setgroups([]); os.setresgid(owner_gid, owner_gid, owner_gid)
        os.setresuid(owner_uid, owner_uid, owner_uid)
        payload = json.dumps(launch()).encode("utf-8")
    except BaseException as exc:
        payload = json.dumps({"__error__": f"{type(exc).__name__}: {exc}"}).encode("utf-8")
    os.write(write_fd, payload); os.close(write_fd); os._exit(0)
os.close(write_fd)
chunks = []
while True:
    chunk = os.read(read_fd, 65536)
    if not chunk:
        break
    chunks.append(chunk)
os.close(read_fd); os.waitpid(pid, 0)
answer = json.loads(b"".join(chunks).decode("utf-8"))
if "__error__" in answer:
    raise SystemExit("launch probe failed: " + answer["__error__"])
out["launch"] = answer

# The panes the repair was for are the same processes they were.
out["still_alive"] = {}
for name, was in live.items():
    try:
        os.kill(was, 0)
        out["still_alive"][name] = True
    except OSError:
        out["still_alive"][name] = False
out["pane_pids_now"] = {
    n: team_launcher.pane_pid_for_role(roles[n], runner=runner) for n in ("main", "ops")
}

# A role whose pane is NOT running its configured CLI is never vouched for.
(tmp / "b").mkdir()
config_b, _ = suite.tenant(tmp / "b", owner="stellaris-agent")
store_b = Path(config_b.session_dir)
store_b.mkdir(parents=True, exist_ok=True); os.chown(store_b, owner_uid, owner_gid)
(store_b / "roles").mkdir(exist_ok=True); os.chown(store_b / "roles", owner_uid, owner_gid)
for name in ("director", "main", "ops", "audit"):
    d = store_b / "roles" / name
    d.mkdir(exist_ok=True); os.chown(d, 0, 0); os.chmod(d, 0o700)
roles_b = {r.role: r for r in config_b.roles}
said_b = []
out["restored_b"] = team_launcher.restore_interrupted_role_state(
    config_b, runner=runner_for({"main": other.pid}), owner_home=home, print_func=said_b.append
)
out["said_b"] = said_b
out["records_b"] = {
    n: team_launcher.recorded_provider_state_generation(config_b, roles_b[n])
    for n in ("main", "ops")
}

# C -- the pane changes identity between the capture and the write. The
# transaction must stop rather than vouch for a process it never looked at.
(tmp / "c").mkdir()
config_c, _ = suite.tenant(tmp / "c", owner="stellaris-agent")
store_c = Path(config_c.session_dir)
store_c.mkdir(parents=True, exist_ok=True); os.chown(store_c, owner_uid, owner_gid)
(store_c / "roles").mkdir(exist_ok=True); os.chown(store_c / "roles", owner_uid, owner_gid)
for name in ("director", "main", "ops", "audit"):
    d = store_c / "roles" / name
    d.mkdir(exist_ok=True); os.chown(d, 0, 0); os.chmod(d, 0o700)
roles_c = {r.role: r for r in config_c.roles}
moved = subprocess.Popen([str(fake_cli), "300"])
asked = {}


def runner_that_moves(args, **kwargs):
    argv = list(args)
    if "kill-session" in argv:
        kills.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    if "display-message" in argv:
        target = argv[argv.index("-t") + 1]
        role = target.split("-", 1)[1].split(":")[0]
        if "#{pane_pid}" in argv:
            asked[role] = asked.get(role, 0) + 1
            # Answered once for the capture, then the pane is a different one.
            pid = live["main"] if asked[role] == 1 else moved.pid
            return subprocess.CompletedProcess(argv, 0, stdout=f"{pid}\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="claude\n", stderr="")
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


said_c = []
out["restored_c"] = team_launcher.restore_interrupted_role_state(
    config_c, runner=runner_that_moves, owner_home=home, print_func=said_c.append
)
out["said_c"] = said_c
out["records_c"] = {
    n: team_launcher.recorded_provider_state_generation(config_c, roles_c[n])
    for n in ("main", "ops")
}

# D -- the account's provider state changes while the store is being repaired.
(tmp / "d").mkdir()
config_d, _ = suite.tenant(tmp / "d", owner="stellaris-agent")
store_d = Path(config_d.session_dir)
store_d.mkdir(parents=True, exist_ok=True); os.chown(store_d, owner_uid, owner_gid)
(store_d / "roles").mkdir(exist_ok=True); os.chown(store_d / "roles", owner_uid, owner_gid)
for name in ("director", "main", "ops", "audit"):
    d = store_d / "roles" / name
    d.mkdir(exist_ok=True); os.chown(d, 0, 0); os.chmod(d, 0o700)
roles_d = {r.role: r for r in config_d.roles}
home_d = tmp / "owner-home-d"; home_d.mkdir(); os.chown(home_d, owner_uid, owner_gid)
turned = {"n": 0}


def runner_that_logs_in(args, **kwargs):
    argv = list(args)
    if "display-message" in argv and "#{pane_pid}" in argv:
        turned["n"] += 1
        if turned["n"] > 4:
            # A login lands between the capture and the write.
            credentials = home_d / ".claude"
            credentials.mkdir(exist_ok=True)
            (credentials / ".credentials.json").write_text("{}")
        target = argv[argv.index("-t") + 1]
        role = target.split("-", 1)[1].split(":")[0]
        return subprocess.CompletedProcess(argv, 0, stdout=f"{live.get(role, 0)}\n", stderr="")
    if "display-message" in argv:
        return subprocess.CompletedProcess(argv, 0, stdout="claude\n", stderr="")
    if "kill-session" in argv:
        kills.append(argv[-1])
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


said_d = []
out["restored_d"] = team_launcher.restore_interrupted_role_state(
    config_d, runner=runner_that_logs_in, owner_home=home_d, print_func=said_d.append
)
out["said_d"] = said_d
out["records_d"] = {
    n: team_launcher.recorded_provider_state_generation(config_d, roles_d[n])
    for n in ("main", "ops", "director")
}

# E -- a store the repair cannot fix: the role directory is missing and its
# parent is the tenant's but not writable by it. Ownership is not the problem,
# so chowning changes nothing -- and that is not a reason to fail the upgrade.
(tmp / "e").mkdir()
config_e, _ = suite.tenant(tmp / "e", owner="stellaris-agent")
store_e = Path(config_e.session_dir)
store_e.mkdir(parents=True, exist_ok=True); os.chown(store_e, owner_uid, owner_gid)
(store_e / "roles").mkdir(exist_ok=True); os.chown(store_e / "roles", owner_uid, owner_gid)
for name in ("director", "audit", "ops"):
    d = store_e / "roles" / name
    d.mkdir(exist_ok=True); os.chown(d, owner_uid, owner_gid)
os.chmod(store_e / "roles", 0o500)  # main's directory can never be created
roles_e = {r.role: r for r in config_e.roles}
said_e = []
out["restored_e"] = team_launcher.restore_interrupted_role_state(
    config_e, runner=runner_for({"main": live["main"]}), owner_home=home,
    print_func=said_e.append,
)
out["said_e"] = said_e
out["records_e"] = {
    n: team_launcher.recorded_provider_state_generation(config_e, roles_e[n])
    for n in ("main", "ops")
}
os.chmod(store_e / "roles", 0o700)

moved.kill()
for proc in (other,):
    proc.kill()
for was in live.values():
    try:
        os.kill(was, 9)
    except OSError:
        pass
print(json.dumps(out))
'''


ROOT_UPGRADE_CHILD = r'''
"""`sudo switchyard upgrade <project>` against a REAL project-owner tmux server.

No injected runner anywhere: the transaction runs with its shipped default, and
the only thing standing in for the host is a `sudo` on PATH that really does
drop to the named account. Root and the owner then reach different tmux servers
for the reason they do on the host -- the socket directory is named after the
euid -- so "root cannot see the tenant's panes" is enforced by tmux itself.

/tmp is a private tmpfs inside this namespace, so `/tmp/tmux-<uid>` here is this
test's own and the live server is unreachable. That is asserted before the first
tmux call rather than assumed: a TMUX_TMPDIR pointing at a directory that does
not exist is silently ignored and tmux falls back to the real one.
"""
import json, os, pwd, shutil, subprocess, sys, time
from pathlib import Path

subprocess.run(["mount", "--bind", sys.argv[2], "/mnt"], check=True)
subprocess.run(["mount", "-t", "tmpfs", "tmpfs", "/tmp"], check=True)
os.chmod("/tmp", 0o1777)
assert os.listdir("/tmp") == [], f"/tmp is not this namespace's own: {os.listdir('/tmp')}"
assert "TMUX" not in os.environ, "a live tmux would be addressed instead of ours"

sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]) / "tests"))
from scripts import team_launcher
import legacy_presentation_launch_test as suite

tmp = Path("/mnt")
owner = "stellaris-agent"
owner_uid = pwd.getpwnam(owner).pw_uid
owner_gid = pwd.getpwnam(owner).pw_gid
config, _ = suite.tenant(tmp, owner=owner)
home = tmp / "owner-home"; home.mkdir(); os.chown(home, owner_uid, owner_gid)
roles = {r.role: r for r in config.roles}

# The store as the live defect left it: main and ops are root's.
store = Path(config.session_dir)
store.mkdir(parents=True, exist_ok=True); os.chown(store, owner_uid, owner_gid)
(store / "roles").mkdir(); os.chown(store / "roles", owner_uid, owner_gid)
for name in ("main", "ops"):
    d = store / "roles" / name
    d.mkdir(); os.chown(d, 0, 0); os.chmod(d, 0o700)
for name in ("director", "audit"):
    d = store / "roles" / name
    d.mkdir(); os.chown(d, owner_uid, owner_gid)

# A `sudo` that really drops. Everything else on PATH is the host's.
stub = tmp / "stub"; stub.mkdir()
(stub / "sudo").write_text(
    "#!/usr/bin/env python3\n"
    "import os, pwd, sys\n"
    "args = sys.argv[1:]\n"
    "user = None\n"
    "while args:\n"
    "    if args[0] == '-u':\n"
    "        user, args = args[1], args[2:]\n"
    "    elif args[0] in ('-H', '-n'):\n"
    "        args = args[1:]\n"
    "    else:\n"
    "        break\n"
    "entry = pwd.getpwnam(user)\n"
    "os.environ['HOME'] = '/mnt/owner-home'\n"
    "os.setgroups([])\n"
    "os.setgid(entry.pw_gid)\n"
    "os.setresuid(entry.pw_uid, entry.pw_uid, entry.pw_uid)\n"
    "os.execvp(args[0], args)\n"
)
os.chmod(stub / "sudo", 0o755)
os.environ["PATH"] = f"{stub}:" + os.environ.get("PATH", "/usr/bin:/bin")

# Four live workers, each running the role's configured CLI, on the PROJECT
# ACCOUNT's tmux server -- which is where they are on the host.
bindir = tmp / "bin"; bindir.mkdir(); os.chown(bindir, owner_uid, owner_gid)
fake_cli = bindir / "claude"
shutil.copy2(shutil.which("sleep"), fake_cli)
os.chmod(fake_cli, 0o755); os.chown(fake_cli, owner_uid, owner_gid)


def as_owner(argv):
    return subprocess.run(
        [str(stub / "sudo"), "-u", owner, "-H", *argv],
        capture_output=True, text=True,
    )


for name, role in roles.items():
    made = as_owner(["tmux", "new-session", "-d", "-s", role.tmux_session,
                     f"{fake_cli} 300"])
    assert made.returncode == 0, f"{name}: {made.stderr}"
time.sleep(0.5)
# The owner's server exists and is this namespace's; root's does not.
assert f"tmux-{owner_uid}" in os.listdir("/tmp"), sorted(os.listdir("/tmp"))
probe = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True)
assert probe.returncode != 0, f"root can see sessions it must not: {probe.stdout}"

out = {}
# (a) Root's own tmux cannot see any of it -- this is the defect.
out["bare_root_pids"] = {
    n: team_launcher.pane_pid_for_role(r, runner=subprocess.run) for n, r in roles.items()
}
# (b) The owner-crossing runner can.
out["crossed_pids"] = {
    n: team_launcher.pane_pid_for_role(
        r, runner=team_launcher.role_process_runner_for(config, r, runner=subprocess.run)
    )
    for n, r in roles.items()
}
out["whoami"] = team_launcher.current_user_name()

# Director and audit were never broken: their stores work and their records are
# current, so the four that are up are the four that must still be up.
for name in ("director", "audit"):
    team_launcher.record_provider_state_generation(
        config, roles[name], team_launcher.provider_state_generation("claude", owner_home=home)
    )

# Step 1 -- the shipped command, with the shipped runner.
said = []
out["restored"] = team_launcher.restore_interrupted_role_state(
    config, owner_home=home, print_func=said.append
)
out["said"] = said
out["generation"] = team_launcher.provider_state_generation("claude", owner_home=home)
out["records"] = {
    n: team_launcher.recorded_provider_state_generation(config, r) for n, r in roles.items()
}
out["record_uids"] = {}
for n in roles:
    record = Path(team_launcher.role_session_dir(config, roles[n])) / f"{n}.provider-state.json"
    out["record_uids"][n] = os.lstat(record).st_uid if record.exists() else None


# Step 2 -- the ordinary launch, as the project account, its own tmux, no stubs.
def launch():
    kept, _unreconciled = team_launcher._drop_roles_with_stale_provider_runtime(
        config, list(config.roles), owner_home=home,
        runner=subprocess.run, print_func=lambda _l: None,
    )
    return {
        "kept": sorted(r.role for r in kept),
        "pids": {n: team_launcher.pane_pid_for_role(r, runner=subprocess.run)
                 for n, r in roles.items()},
        "sessions": sorted(
            subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                           capture_output=True, text=True).stdout.split()
        ),
    }


read_fd, write_fd = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(read_fd)
    try:
        os.setgroups([]); os.setresgid(owner_gid, owner_gid, owner_gid)
        os.setresuid(owner_uid, owner_uid, owner_uid)
        os.environ["HOME"] = str(home)
        payload = json.dumps(launch()).encode("utf-8")
    except BaseException as exc:
        payload = json.dumps({"__error__": f"{type(exc).__name__}: {exc}"}).encode("utf-8")
    os.write(write_fd, payload); os.close(write_fd); os._exit(0)
os.close(write_fd)
chunks = []
while True:
    chunk = os.read(read_fd, 65536)
    if not chunk:
        break
    chunks.append(chunk)
os.close(read_fd); os.waitpid(pid, 0)
answer = json.loads(b"".join(chunks).decode("utf-8"))
if "__error__" in answer:
    raise SystemExit("launch probe failed: " + answer["__error__"])
out["launch"] = answer

for role in roles.values():
    as_owner(["tmux", "kill-session", "-t", role.tmux_session])
print(json.dumps(out))
'''


def run_namespace(child_source: str, prefix: str) -> dict:
    with tempfile.TemporaryDirectory(prefix=prefix) as raw:
        tmp = Path(raw)
        os.chmod(raw, 0o755)
        sandbox = tmp / "tenant"
        sandbox.mkdir()
        sandbox.chmod(0o755)
        child = tmp / "child.py"
        child.write_text(child_source, encoding="utf-8")
        proc = subprocess.run(
            ["unshare", "--user", "--map-auto", "--map-root-user", "--mount",
             sys.executable, str(child), str(ROOT), str(sandbox)],
            capture_output=True, text=True, timeout=180,
        )
        # Removed from inside the namespace, where these uids exist.
        subprocess.run(
            ["unshare", "--user", "--map-auto", "--map-root-user", "sh", "-c", f"rm -rf {sandbox}"],
            capture_output=True,
        )
        check(proc.returncode == 0, f"the namespace child ran: {proc.stderr[-2500:]}")
        return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_role_whose_state_the_account_cannot_write_is_left_running() -> None:
    """The live failure, with real uids and the kernel deciding."""
    for tool in ("unshare", "setpriv"):
        check(bool(shutil.which(tool)), f"{tool} is required")
    result = run_namespace(UNWRITABLE_CHILD, "syrd233-unwritable.")

    problems = result["problems"]
    check(
        all(problems[role] for role in ("main", "ops")),
        f"the two root-owned role directories are reported: {problems}",
    )
    check(
        "cannot be written by" in problems["main"] and "root" in problems["main"],
        f"naming who owns it: {problems['main']}",
    )
    check(not problems["director"], f"a role whose store is the tenant's is not: {problems}")
    check(
        "cannot be read" in problems["audit"],
        f"and a record the account cannot read is its own answer: {problems['audit']}",
    )
    as_root = result["as_root"]
    check(
        "cannot be written by stellaris-agent" in as_root["main"],
        f"root asks the question as the account that writes the record: {as_root['main']!r}",
    )
    check(
        "cannot be read by stellaris-agent" in as_root["audit"],
        f"and reads it as that account too: {as_root['audit']!r}",
    )
    check(as_root["director"] == "", f"a healthy store is healthy for root too: {as_root!r}")
    check(
        result["stale"] == ["director"],
        f"only the role that can be judged is judged: {result['stale']}",
    )
    check(
        result["sealed_kept"] == ["audit"],
        f"a role with an unreadable record is kept, not killed: {result['sealed_kept']}",
    )
    check(
        sorted(result["kept"]) == ["main", "ops"],
        f"and the two that cannot are kept, not killed: {result['kept']}",
    )
    said = "\n".join(result["said"])
    check("leaving main running" in said, said)
    check("restarting it would settle nothing" in said, said)
    check(f"sudo switchyard upgrade {PROJECT}" in said, f"and the repair is named: {said}")

    # The repair, and the same account asking again.
    check(result["repaired"], f"root repaired the store: {result['printed']}")
    check("gave" in "\n".join(result["printed"]), result["printed"])
    check(
        not any(result["after"].values()),
        f"afterwards every role's store is writable: {result['after']}",
    )
    check(result["recorded"] == "gen-1", f"and the record can be written: {result['recorded']}")
    check(
        set(result["owners"].values()) == {1002},
        f"every role directory belongs to the tenant account: {result['owners']}",
    )


def test_a_symlinked_store_root_is_refused_and_nothing_outside_is_chowned() -> None:
    """The DAT finding: `follow_symlinks=False` guards the last component only.

    A root-run chown walk that treats a tenant-controlled path as a directory
    will happily walk through a symlink at that path -- `Path.is_dir()` follows
    it and `Path.rglob` enumerates the target -- and then chown files that were
    never the tenant's. Every root here is opened with O_NOFOLLOW from its
    parent's descriptor, and every chown goes through the descriptor the path
    was read through.
    """
    for tool in ("unshare", "setpriv"):
        check(bool(shutil.which(tool)), f"{tool} is required")
    result = run_namespace(ESCAPE_CHILD, "syrd233-escape.")

    # A: the session root itself.
    check(result["a_repaired"] is False, "a symlinked session root is refused")
    said = "\n".join(result["a_said"])
    check("refusing to touch" in said, said)
    check("symlink" in said, f"and says why: {said}")
    check("No ownership was changed" in said, said)
    check(result["a_victim_uid"] == 0, "the file outside the store is untouched")
    check(
        result["a_refusals"] and not result["a_problems"],
        f"it is reported as a refusal, not as a list of paths to chown: {result['a_refusals']}, "
        f"{result['a_problems']}",
    )
    check(
        all("outside-a" not in path for path, _reason in result["a_problems"]),
        f"and nothing outside the store is ever named as a path to repair: {result['a_problems']}",
    )

    # B: one role root, with a real repair waiting behind it.
    check(result["b_repaired"] is False, "a symlinked role root is refused too")
    check("refusing to touch" in "\n".join(result["b_said"]), result["b_said"])
    check(result["b_victim_uid"] == 0, "the file outside that one is untouched")
    check(
        result["b_untouched_uid"] == 0,
        "and the refusal happens BEFORE any repair: the root-owned role "
        f"directory is still root's, uid {result['b_untouched_uid']}",
    )
    check(
        result["b_dry_repaired"] is False and "refusing to touch" in "\n".join(result["b_dry_said"]),
        f"a dry run refuses the same way: {result['b_dry_said']}",
    )

    # C: a symlink inside a real role directory is given back AS A LINK.
    check(result["c_repaired"] is True, f"a walkable store is repaired: {result['c_said']}")
    check(result["c_repaired_uid"] == 1002, "the root-owned role directory is given back")
    check(result["c_link_uid"] == 1002, "the symlink itself is given back, as a link")
    check(result["c_victim_uid"] == 0, "what it points at is not")
    check(result["c_victim_dir_uid"] == 0, "nor is the directory holding it")

    # D: a symlink root itself put in the path, where only root can write.
    check(
        result["d_repaired"] is True,
        f"root's own layout is still followed, so such a host still upgrades: {result['d_said']}",
    )
    check(result["d_repaired_uid"] == 1002, "and the repair behind it still happens")


def test_the_upgrade_then_launch_sequence_keeps_the_same_worker_processes() -> None:
    """The acceptance requirement: `sudo switchyard upgrade` then `switchyard <p>`.

    Leaving a role running while its store is broken is only half a repair. The
    upgrade makes the store writable, the record is still absent, and absent is
    stale -- so the very next launch ends the panes the first half protected.
    The restart is postponed by one command rather than avoided. This drives
    both commands in order, against real processes running the configured CLI.
    """
    for tool in ("unshare", "setpriv"):
        check(bool(shutil.which(tool)), f"{tool} is required")
    result = run_namespace(SURVIVAL_CHILD, "syrd233-survival.")

    check(
        all(result["before"][role] for role in ("main", "ops")),
        f"before the upgrade, main and ops cannot record anything: {result['before']}",
    )
    check(
        result["before"]["director"] is False,
        "and director's store was fine all along, so it is not part of this",
    )

    # Step 1: the upgrade.
    check(result["restored"] is True, f"the upgrade repairs and finishes: {result['said']}")
    said = "\n".join(result["said"])
    check("finished the provider-state record main could not write" in said, said)
    check("finished the provider-state record ops could not write" in said, said)
    check(
        str(result["live_pids"]["main"]) in said,
        f"naming the live process it is vouching for: {said}",
    )
    generation = result["generation"]
    check(bool(generation), "the account has a provider generation to record")
    check(
        result["records"]["main"] == generation and result["records"]["ops"] == generation,
        f"both interrupted records are now the current generation: {result['records']}",
    )
    check(
        result["records"]["director"] == "",
        "and a role that merely has no record is NOT given one: "
        f"{result['records']['director']!r}",
    )
    check(
        set(result["record_uids"].values()) == {1002},
        f"the records belong to the tenant account, not to root: {result['record_uids']}",
    )

    # Step 2: the ordinary launch that follows it.
    launch = result["launch"]
    check(
        sorted(launch["kept"]) == ["main", "ops"],
        f"main and ops are presented as they are: {launch['kept']}",
    )
    check(
        launch["kills"] == [f"{PROJECT}-director"],
        "and only the role that is genuinely stale is ended, so SYRD-191's rule "
        f"that a missing record is stale is untouched: {launch['kills']}",
    )
    check(
        result["still_alive"]["main"] and result["still_alive"]["ops"],
        f"the worker processes are still running: {result['still_alive']}",
    )
    check(
        result["pane_pids_now"]["main"] == result["live_pids"]["main"]
        and result["pane_pids_now"]["ops"] == result["live_pids"]["ops"],
        f"and they are the same pids the upgrade found: {result['pane_pids_now']} vs "
        f"{result['live_pids']}",
    )

    # A pane that is not running the configured CLI is never vouched for.
    check(
        result["restored_b"] is True,
        f"a tenant with nothing to finish still repairs: {result['said_b']}",
    )
    check(
        result["records_b"] == {"main": "", "ops": ""},
        "a pane running something other than the role's CLI gets no record: "
        f"{result['records_b']}",
    )
    check(
        not any("finished" in line for line in result["said_b"]),
        f"and nothing is claimed to have been finished: {result['said_b']}",
    )


    # C: the pane is not the process the upgrade looked at any more.
    check(
        result["restored_c"] is False,
        f"a pane that moved stops the upgrade: {result['said_c']}",
    )
    check(
        "no longer the process this upgrade found" in "\n".join(result["said_c"]),
        f"and says so: {result['said_c']}",
    )
    check(
        result["records_c"] == {"main": "", "ops": ""},
        f"nothing is recorded for a runtime nobody saw: {result['records_c']}",
    )

    # D: the account's provider state moved while the store was being repaired.
    check(
        result["restored_d"] is False,
        f"a login mid-transaction stops the upgrade: {result['said_d']}",
    )
    check(
        "provider state changed while" in "\n".join(result["said_d"]),
        f"and says so: {result['said_d']}",
    )
    check(
        set(result["records_d"].values()) == {""},
        f"and no stale generation is left behind: {result['records_d']}",
    )


    # E: a store the repair cannot fix is reported, not fatal.
    check(
        result["restored_e"] is True,
        "a store whose problem is not ownership does not fail the upgrade: "
        f"{result['said_e']}",
    )
    said_e = "\n".join(result["said_e"])
    check("still cannot be recorded after the repair" in said_e, said_e)
    check(
        "keeps running and is still not checked" in said_e,
        f"and the role is left exactly as it was: {said_e}",
    )
    check(
        result["records_e"]["main"] == "",
        f"with nothing recorded for it: {result['records_e']}",
    )


def test_the_root_upgrade_reaches_the_project_accounts_own_tmux_server() -> None:
    """The shipped command is `sudo switchyard upgrade <project>`, so it is root.

    A bare `tmux` from root addresses ROOT's server. The tenant's panes are on
    the project account's, so every pane answers "gone", nothing is captured,
    the upgrade repairs ownership and stops -- and the next ordinary launch
    applies the absent-record stale rule and restarts the workers anyway. The
    injected runner every other case here uses cannot see that boundary,
    because it never crosses an account.

    So this drives the transaction with its SHIPPED default runner against a
    real tmux server owned by a real other uid.
    """
    for tool in ("unshare", "setpriv", "tmux", "mount"):
        check(bool(shutil.which(tool)), f"{tool} is required")
    result = run_namespace(ROOT_UPGRADE_CHILD, "syrd233-rootupgrade.")

    check(result["whoami"] == "root", f"the upgrade runs as root: {result['whoami']}")
    check(
        set(result["bare_root_pids"].values()) == {0},
        "root's own tmux server has none of the tenant's panes, which is the "
        f"defect: {result['bare_root_pids']}",
    )
    crossed = result["crossed_pids"]
    check(
        all(pid > 0 for pid in crossed.values()) and len(set(crossed.values())) == 4,
        f"and the owner-crossing runner finds all four, each its own: {crossed}",
    )

    # Step 1: the upgrade, run with the shipped runner.
    check(result["restored"] is True, f"the upgrade completes: {result['said']}")
    said = "\n".join(result["said"])
    for role in ("main", "ops"):
        check(
            f"finished the provider-state record {role} could not write" in said,
            f"{role}'s interrupted record is finished: {said}",
        )
        check(
            str(crossed[role]) in said,
            f"naming the pid it vouched for: {said}",
        )
    generation = result["generation"]
    check(
        all(result["records"][r] == generation for r in ("main", "ops")),
        f"both now hold the current generation: {result['records']}",
    )
    check(
        set(result["record_uids"].values()) == {1002},
        f"and every record belongs to the tenant account: {result['record_uids']}",
    )

    # Step 2: the ordinary launch that follows it.
    launch = result["launch"]
    check(
        launch["kept"] == ["audit", "director", "main", "ops"],
        f"all four roles are presented as they are: {launch['kept']}",
    )
    check(
        launch["pids"] == crossed,
        "and they are the same four pids the upgrade found: "
        f"{launch['pids']} vs {crossed}",
    )
    check(
        len(launch["sessions"]) == 4,
        f"with all four sessions still alive: {launch['sessions']}",
    )


def test_the_upgrade_asks_the_owners_own_home_when_nobody_passes_one() -> None:
    """The shipped default, which every other case here injects around."""
    with tempfile.TemporaryDirectory(prefix="syrd233-ownerhome.") as raw:
        tmp = Path(raw)
        config, _ = tenant(tmp)
        seen: list[Path] = []
        saved = team_launcher._interrupted_provider_state_roles

        def spy(cfg, *, runner, owner_home):
            seen.append(owner_home)
            return []

        try:
            team_launcher._interrupted_provider_state_roles = spy
            team_launcher.restore_interrupted_role_state(config, print_func=lambda _l: None)
        finally:
            team_launcher._interrupted_provider_state_roles = saved
    check(len(seen) == 1, f"the transaction asked once: {seen}")
    check(
        seen[0] == team_launcher.home_dir_for_user(config.run_as_user),
        f"for the project owner's own home: {seen[0]} "
        f"vs {team_launcher.home_dir_for_user(config.run_as_user)}",
    )


def test_the_repair_is_root_only_and_says_so() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-repair-unpriv.") as raw:
        tmp = Path(raw)
        config, _ = tenant(tmp, owner="stellaris-agent")
        store = Path(config.session_dir) / "roles" / "main"
        store.mkdir(parents=True)
        printed: list[str] = []
        ok = team_launcher.repair_role_state_ownership(config, print_func=printed.append)
        output = "\n".join(printed)
    check(not ok, output)
    check("only root can give it back" in output, output)
    check(f"sudo switchyard upgrade {PROJECT}" in output, output)


def test_a_store_that_is_already_the_tenants_is_not_touched() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-repair-noop.") as raw:
        tmp = Path(raw)
        config, _ = tenant(tmp)
        (Path(config.session_dir) / "roles" / "main").mkdir(parents=True)
        printed: list[str] = []
        check(
            team_launcher.repair_role_state_ownership(config, print_func=printed.append),
            "a store this account owns needs nothing",
        )
        check(printed == [], f"and nothing is said: {printed}")
        check(
            team_launcher.role_state_ownership_problems(config) == ([], []),
            "and nothing is reported, and nothing is refused",
        )


# --------------------------------------------------------------------------
# A tab whose program will not start
# --------------------------------------------------------------------------


def test_a_window_whose_tab_program_is_missing_is_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-pane-program.") as raw:
        tmp = Path(raw)
        config, config_path = tenant(tmp)
        # A legacy tenant's pane launcher, from a release that shipped no pane
        # window beside it.
        launcher = tmp / "legacy-release" / "team-launcher"
        launcher.parent.mkdir()
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)
        config = replace(config, pane_launcher=launcher)
        problem = team_launcher.presentation_pane_program_problem(config)
    check("is not there" in problem, problem)
    check("switchyard-pane-window" in problem, problem)
    check("falls back to a plain shell" in problem, f"and says what Konsole does: {problem}")


def test_the_installed_release_pane_program_is_accepted() -> None:
    """The real installed program, root-owned all the way up -- no stand-in."""
    # The host's real install root, not the sandbox one the helpers point the
    # seam at: the claim is about a genuinely root-owned installed program.
    installed = (
        Path(team_launcher.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT)
        / "current" / "scripts" / team_launcher.TEAM_LAUNCHER_NAME
    )
    program = team_launcher.pane_window_program(installed)
    if not program.exists():
        check(False, f"this host has no installed release to check: {program}")
    check(program.stat().st_uid == 0, f"the installed program is root's: {program}")
    with tempfile.TemporaryDirectory(prefix="syrd233-pane-ok.") as raw:
        tmp = Path(raw)
        config, _ = tenant(tmp)
        configured = replace(config, pane_launcher=installed)
        check(
            team_launcher.presentation_pane_program_problem(configured) == "",
            f"a provisioned tenant pointing at it is accepted: {program}",
        )


def test_a_checkout_running_its_own_panes_is_not_held_to_root_ownership() -> None:
    # `pane_launcher` unset is a checkout running its own panes, and its files
    # belong to whoever cloned it. `_verify_pane_launcher_path` draws the same
    # line, so this does not start refusing what already works.
    with tempfile.TemporaryDirectory(prefix="syrd233-pane-checkout.") as raw:
        config, _ = tenant(Path(raw))
        check(config.pane_launcher is None, "this fixture configures no launcher")
        check(
            team_launcher.presentation_pane_program_problem(config) == "",
            "a checkout's own pane program is accepted",
        )


def test_a_configured_launcher_whose_program_is_not_roots_is_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-pane-notroot.") as raw:
        tmp = Path(raw)
        config, _ = tenant(tmp)
        release = tmp / "tenant-release"
        release.mkdir()
        launcher = release / "team-launcher"
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)
        program = release / "switchyard-pane-window"
        program.write_text("#!/bin/sh\nexec sleep infinity\n", encoding="utf-8")
        program.chmod(0o755)
        problem = team_launcher.presentation_pane_program_problem(
            replace(config, pane_launcher=launcher)
        )
    check("not pinned to root" in problem, f"a tenant-owned tab program is refused: {problem}")


def test_the_launch_refuses_rather_than_opening_shells() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-launch-shells.") as raw:
        tmp = Path(raw)
        config, config_path = tenant(tmp)
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        payload["presentation"] = {
            "slot_count": 4,
            "layouts": {"default": {str(i): role for i, role in enumerate(ROLES)}},
        }
        config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = team_launcher.load_project_config(PROJECT, config_path)
        launcher = tmp / "legacy-release" / "team-launcher"
        launcher.parent.mkdir()
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)
        config = replace(config, pane_launcher=launcher)
        opened: list = []
        saved = team_launcher.launch_konsole_window
        try:
            team_launcher.launch_konsole_window = lambda output, **_k: opened.append(output) or 0
            try:
                # The window-opening half directly: reaching it through
                # launch_presentation would first ask a board that is not
                # running in this sandbox.
                presentation._launch_separate(
                    config,
                    {"slot_count": len(ROLES)},
                    config_path=config_path,
                    runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
                    process_launcher=None,
                )
            except SystemExit as exc:
                refusal = str(exc)
            else:
                refusal = ""
        finally:
            team_launcher.launch_konsole_window = saved
    check("not opening" in refusal and "is not there" in refusal, refusal)
    check("Its roles are running" in refusal, refusal)
    check(f"sudo switchyard upgrade {PROJECT}" in refusal, refusal)
    check(opened == [], f"and no window of shells was opened: {opened}")


# --------------------------------------------------------------------------
# What Konsole's own splitter does with the command we stage
# --------------------------------------------------------------------------

MEFP_TITLE = "Morfane's Epic Fix Patch"
KONSOLE_TITLES = [
    MEFP_TITLE,
    "plain",
    'say "hi"',
    "dollar $HOME and `cmd`",
    "back\\slash",
    "both ' and \"",
    "trailing apostrophe '",
    "semi; & pipe | glob * ? [x] ~ #hash",
]


def _run_konsole(layout: Path, sandbox: Path, expected: list[Path]) -> None:
    """Open one offscreen Konsole on `layout` and wait for every pane to report.

    Offscreen and with its own XDG directories, so this never reaches a real
    desktop or the caller's Konsole configuration.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(sandbox / "home"),
        "XDG_RUNTIME_DIR": str(sandbox / "run"),
        "XDG_CONFIG_HOME": str(sandbox / "cfg"),
        "XDG_DATA_HOME": str(sandbox / "data"),
        "XDG_CACHE_HOME": str(sandbox / "cache"),
        "QT_QPA_PLATFORM": "offscreen",
    }
    for key in ("home", "run", "cfg", "data", "cache"):
        (sandbox / key).mkdir(parents=True, exist_ok=True)
    (sandbox / "run").chmod(0o700)
    proc = subprocess.Popen(
        ["konsole", "--nofork", "--layout", str(layout)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if all(path.exists() for path in expected):
                return
            if proc.poll() is not None:
                break
            time.sleep(0.2)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
    missing = [p.name for p in expected if not p.exists()]
    # A pane that never reported is a pane whose command Konsole could not
    # run -- which is the failure this exists to catch, not a reason to pass.
    raise AssertionError(f"panes that never reported their argv: {missing}")


def test_konsole_hands_the_helper_exactly_the_arguments_we_staged() -> None:
    """The live four-pane window opened and every pane exited immediately.

    `switchyard-display-attach: slot must be a number or viewer`, because the
    project title carries an apostrophe and POSIX quoting closes and reopens
    the quote around it. Konsole's splitter is not a shell's: it leaves a stray
    `'` and hands it to the LAST argument, so the slot arrived as `0'`.

    `shlex.split` round-trips that string perfectly, which is exactly why every
    test before this one passed. So this one asks the installed Konsole.
    """
    check(bool(shutil.which("konsole")), "konsole is required")
    with tempfile.TemporaryDirectory(prefix="syrd233-konsole.") as raw:
        sandbox = Path(raw)
        widgets = []
        reports = []
        for slot, title in enumerate(KONSOLE_TITLES):
            recorder = sandbox / f"pane{slot}.py"
            report = sandbox / f"argv{slot}.json"
            recorder.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"json.dump(sys.argv[1:], open({str(report)!r}, 'w'))\n",
                encoding="utf-8",
            )
            recorder.chmod(0o755)
            widgets.append({
                "Command": team_launcher.inert_pane_command(
                    recorder, ["mefp", str(slot)],
                    title=f"Slot{slot}", window_title=title,
                ),
                "SessionRestoreId": slot,
                "WorkingDirectory": str(sandbox),
            })
            reports.append(report)
        layout = sandbox / "layout.json"
        layout.write_text(
            json.dumps({"Orientation": "Horizontal", "Widgets": widgets}, indent=2),
            encoding="utf-8",
        )
        _run_konsole(layout, sandbox, reports)
        seen = [json.loads(report.read_text(encoding="utf-8")) for report in reports]

    for slot, (title, argv) in enumerate(zip(KONSOLE_TITLES, seen)):
        check(
            argv == ["--window-title", title, "--title", f"Slot{slot}", "mefp", str(slot)],
            f"Konsole split slot {slot} ({title!r}) into {argv!r}",
        )
        check(
            argv[-1] == str(slot),
            f"the slot arrives as a number, with no quote residue: {argv[-1]!r}",
        )
    check(
        seen[0][1] == MEFP_TITLE,
        f"and mefp's own title survives intact: {seen[0][1]!r}",
    )


def test_a_title_konsole_cannot_carry_is_reported_as_what_arrives() -> None:
    """No quoting carries a control character through Konsole's splitter.

    Measured: a tab is dropped even inside quotes, and a newline truncates the
    rest of the command line -- which would silently drop the slot argument.
    They are collapsed to a space, so what the helper receives is what the
    staged command says it will.
    """
    check(bool(shutil.which("konsole")), "konsole is required")
    titles = ["tab\there", "newline\nhere"]
    with tempfile.TemporaryDirectory(prefix="syrd233-konsole-ctl.") as raw:
        sandbox = Path(raw)
        widgets = []
        reports = []
        for slot, title in enumerate(titles):
            recorder = sandbox / f"pane{slot}.py"
            report = sandbox / f"argv{slot}.json"
            recorder.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"json.dump(sys.argv[1:], open({str(report)!r}, 'w'))\n",
                encoding="utf-8",
            )
            recorder.chmod(0o755)
            widgets.append({
                "Command": team_launcher.inert_pane_command(
                    recorder, ["mefp", str(slot)], title="Slot", window_title=title,
                ),
                "SessionRestoreId": slot,
                "WorkingDirectory": str(sandbox),
            })
            reports.append(report)
        layout = sandbox / "layout.json"
        layout.write_text(
            json.dumps({"Orientation": "Horizontal", "Widgets": widgets}, indent=2),
            encoding="utf-8",
        )
        _run_konsole(layout, sandbox, reports)
        seen = [json.loads(report.read_text(encoding="utf-8")) for report in reports]

    for slot, argv in enumerate(seen):
        check(
            argv == ["--window-title", titles[slot].replace("\t", " ").replace("\n", " "),
                     "--title", "Slot", "mefp", str(slot)],
            f"the control character became a space and the slot still arrives: {argv!r}",
        )


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("legacy_presentation_launch_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(240)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"legacy_presentation_launch_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
