#!/usr/bin/env python3
"""SYRD-895: a tenant's role panes never inherit the launching user's real-time scheduling.

On 2026-11-27 ocstab's panes, run as `ocstab-agent`, had `Max realtime priority 99` and `Max nice priority 31`, the
User's own from `@audio - rtprio 99` / `nice -11`, and a stress run's threads took SCHED_RR on a host without RT
throttling and starved every tenant.

* **The real start.** The real `_start_role_session`, run by a process holding this host's launching limits, starts a
  role on a private tmux server whose pane writes its own `/proc/self/limits`: `Max realtime priority 0 0` and `Max
  nice priority 0 0`. The same start with the drop taken out shows the pane inheriting the launching limits, so the
  check can see a leak (where this host's launching user has none to inherit, that half says so and is skipped).
* **A server another path started first** (Audit, round 1): break-glass's tmux start, through the real owner runner
  (`role_process_runner_for`), starts the project's server; its pane and the role pane `_start_role_session` opens
  afterwards both read 0 0.
* **Every command's entry**: `scripts/switchyard` and `scripts/team-launcher`, run as themselves, leave their process
  at 0:0, so everything they start, by any path, inherits it.
* **The drop itself** (`drop_inherited_scheduling`): from a process at nice -4 and, where it may, SCHED_RR: both
  limits 0:0, nice 0, SCHED_OTHER, said in words; and what it starts inherits all of it. A second call changes nothing.
* **Reading a process** (`process_scheduling`, `too_permissive`) from /proc's own `limits` text.
* **The readiness check** (`host_diagnostics`, on a fixture tree as root reads it at install-shared-release): a
  registered tenant's tmux server with the launching limits is named, with the restart that clears it, under its own
  heading; one with a tenant's limits, another account's server, and a tenant's non-tmux process are not; the
  diagnosis findings read as they did.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import host_diagnostics as hd  # noqa: E402
from scripts import tenant_limits as tl  # noqa: E402

CHECKS = 0
LAUNCHING = resource.getrlimit(resource.RLIMIT_RTPRIO)


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


START = r'''
import os, shutil, subprocess, sys, time
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
from tmux_bus_isolation import isolate_tmux_bus
isolate_tmux_bus()
from scripts import team_launcher, tenant_limits
scratch, drop = Path(sys.argv[2]), sys.argv[3] == "drop"
if not drop:  # the drop taken out: what the pane would have without it
    tenant_limits.drop_inherited_scheduling = lambda: []
real = shutil.which("tmux")
socket = scratch / "tmux.sock"
(scratch / "bin").mkdir()
(scratch / "bin" / "tmux").write_text(f'#!/bin/sh\nexec "{real}" -S "{socket}" -f /dev/null "$@"\n')
(scratch / "bin" / "tmux").chmod(0o755)
os.environ["PATH"] = f"{scratch / 'bin'}:{os.environ['PATH']}"
os.environ.pop("TMUX", None)
os.environ["SHELL"] = "/bin/sh"
out = scratch / "limits.txt"
role = team_launcher.RoleConfig(
    role="limits", slot=0, detached=True, tmux_session="s895-limits", target="s895-limits:0.0", workdir=str(scratch),
    cli=["/bin/sh", "-c", f"grep -E '^Max (realtime|nice) priority' /proc/self/limits > {out}; sleep 30"], model="",
    model_arg="--model", effort="", yolo=False, extra_args=[], resume_mode="flag", resume_flag="--resume",
    resume_subcommand="resume", fresh_session_per_ticket=False, live_commands=["sh"], env={})
try:
    code = team_launcher._start_role_session(role, session_dir=scratch / "sessions", pane_state_dir=scratch / "panes",
                                             prefer_resume=False, seed_source="syrd895")
    for _ in range(100):
        if out.exists() and out.read_text().count("\n") >= 2:
            break
        time.sleep(0.1)
    print(f"start={code}")
    print(out.read_text() if out.exists() else "no limits written")
finally:
    subprocess.run([real, "-S", str(socket), "kill-server"], capture_output=True)
'''


def pane_limits(drop: bool) -> dict[str, list[str]]:
    with tempfile.TemporaryDirectory(prefix="s895.", dir="/tmp") as scratch:  # short: a tmux socket path lives under it
        done = subprocess.run([sys.executable, "-c", START, str(ROOT), scratch, "drop" if drop else "keep"],
                              capture_output=True, text=True, timeout=120)
    found = {" ".join(line.split()[:3]): line.split()[3:5] for line in done.stdout.splitlines() if line.startswith("Max ")}
    check("start=0" in done.stdout and len(found) == 2, f"the real start ran and its pane wrote its limits: "
          f"{done.stdout[-600:]} {done.stderr[-1200:]}")
    return found


def test_the_real_start() -> None:
    found = pane_limits(drop=True)
    check(found == {"Max realtime priority": ["0", "0"], "Max nice priority": ["0", "0"]},
          f"the pane _start_role_session opened has no real-time priority and no negative nice, though this process "
          f"had rtprio {LAUNCHING[0]}:{LAUNCHING[1]}: {found}")
    if max(LAUNCHING) == 0:
        print("tenant_limits_test: this host's launching user has no rtprio to inherit; the leak half is skipped")
        return
    kept = pane_limits(drop=False)
    check(kept["Max realtime priority"] == [str(LAUNCHING[0]), str(LAUNCHING[1])],
          f"without the drop the pane inherits the launching rtprio, so the check above can see a leak: {kept}")


FIRST = r'''
import os, shutil, subprocess, sys, time, types
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
from tmux_bus_isolation import isolate_tmux_bus
isolate_tmux_bus()
from scripts import team_launcher
from scripts.process_inspection import role_process_runner_for
scratch = Path(sys.argv[2])
real = shutil.which("tmux")
socket = scratch / "tmux.sock"
(scratch / "bin").mkdir()
(scratch / "bin" / "tmux").write_text(f'#!/bin/sh\nexec "{real}" -S "{socket}" -f /dev/null "$@"\n')
(scratch / "bin" / "tmux").chmod(0o755)
os.environ["PATH"] = f"{scratch / 'bin'}:{os.environ['PATH']}"
os.environ.pop("TMUX", None)
os.environ["SHELL"] = "/bin/sh"
glass, out = scratch / "glass.txt", scratch / "limits.txt"
role = team_launcher.RoleConfig(
    role="limits", slot=0, detached=True, tmux_session="s895-limits", target="s895-limits:0.0", workdir=str(scratch),
    cli=["/bin/sh", "-c", f"grep -E '^Max (realtime|nice) priority' /proc/self/limits > {out}; sleep 30"], model="",
    model_arg="--model", effort="", yolo=False, extra_args=[], resume_mode="flag", resume_flag="--resume",
    resume_subcommand="resume", fresh_session_per_ticket=False, live_commands=["sh"], env={})
config = types.SimpleNamespace(role_state_isolation=True, run_as_user=team_launcher.current_user_name())
try:
    # Break-glass's tmux start, as break_glass_role.start_command shapes it, through the runner it really uses
    # (role_process_runner_for for the Director): the first command to start the project's tmux server.
    owner_runner = role_process_runner_for(config, role)
    first = owner_runner(["tmux", "new-session", "-d", "-s", "break-glass-s895", "-x", "200", "-y", "50", "/bin/bash", "-c",
                          f"grep -E '^Max (realtime|nice) priority' /proc/self/limits > {glass}; sleep 30"])
    code = team_launcher._start_role_session(role, session_dir=scratch / "sessions", pane_state_dir=scratch / "panes",
                                             prefer_resume=False, seed_source="syrd895")
    for _ in range(100):
        if all(f.exists() and f.read_text().count("\n") >= 2 for f in (glass, out)):
            break
        time.sleep(0.1)
    print(f"first={first.returncode} start={code}")
    print("GLASS " + (glass.read_text().replace("\n", " | ") if glass.exists() else "none"))
    print("ROLE " + (out.read_text().replace("\n", " | ") if out.exists() else "none"))
finally:
    subprocess.run([real, "-S", str(socket), "kill-server"], capture_output=True)
'''


def _read(line: str) -> dict[str, list[str]]:
    return {" ".join(part.split()[:3]): part.split()[3:5] for part in line.split(" | ") if part.startswith("Max ")}


def test_a_server_break_glass_started_first() -> None:
    """Audit, round 1: a tenant server started by another path first decides the limits of every pane on it."""
    with tempfile.TemporaryDirectory(prefix="s895b.", dir="/tmp") as scratch:
        done = subprocess.run([sys.executable, "-c", FIRST, str(ROOT), scratch], capture_output=True, text=True,
                              timeout=120)
    lines = {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in done.stdout.splitlines()
             if line.startswith(("GLASS ", "ROLE "))}
    zero = {"Max realtime priority": ["0", "0"], "Max nice priority": ["0", "0"]}
    check("first=0 start=0" in done.stdout, f"break-glass's start, then the role's, both ran: {done.stdout[-500:]} "
          f"{done.stderr[-1200:]}")
    check(_read(lines.get("GLASS", "")) == zero,
          f"the break-glass pane, first on the server through the real owner runner, has none of the launching "
          f"rtprio {LAUNCHING[0]}:{LAUNCHING[1]}: {lines.get('GLASS')}")
    check(_read(lines.get("ROLE", "")) == zero,
          f"and the role pane opened afterwards on that server has none either: {lines.get('ROLE')}")


OWNER = r'''
import resource, sys
sys.path.insert(0, sys.argv[1])
from scripts.process_inspection import _owner_process_runner
seen = []
def fake(args, **kwargs):
    seen.append((args[:4], resource.getrlimit(resource.RLIMIT_RTPRIO), resource.getrlimit(resource.RLIMIT_NICE)))
_owner_process_runner(owner_user="alpha-agent", runner=fake)(["tmux", "new-session", "-d"])
print("SEEN", seen)
'''


def test_the_owner_runner_drops_before_crossing() -> None:
    done = subprocess.run([sys.executable, "-c", OWNER, str(ROOT)], capture_output=True, text=True, timeout=60)
    line = next((l for l in done.stdout.splitlines() if l.startswith("SEEN ")), done.stderr[-500:])
    check(line == "SEEN [(['sudo', '-u', 'alpha-agent', '-H'], (0, 0), (0, 0))]",
          f"the owner-account runner hands sudo a process already at 0:0: {line}")


ENTRY = r'''
import resource, runpy, sys
sys.argv = [sys.argv[1], "--help"]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit:
    pass
print("LIMITS", resource.getrlimit(resource.RLIMIT_RTPRIO), resource.getrlimit(resource.RLIMIT_NICE))
'''


def test_every_command_drops_at_its_entry() -> None:
    for name in ("switchyard", "team-launcher"):
        done = subprocess.run([sys.executable, "-c", ENTRY, str(ROOT / "scripts" / name)], capture_output=True,
                              text=True, timeout=120, stdin=subprocess.DEVNULL)
        line = next((l for l in done.stdout.splitlines() if l.startswith("LIMITS ")), "")
        check(line == "LIMITS (0, 0) (0, 0)",
              f"scripts/{name}, run as itself, leaves its process (and all it starts) at 0:0 from rtprio "
              f"{LAUNCHING[0]}:{LAUNCHING[1]}: {line or done.stderr[-600:]}")


DROP = r'''
import json, os, resource, subprocess, sys
sys.path.insert(0, sys.argv[1])
from scripts import tenant_limits
before = {"nice": os.getpriority(os.PRIO_PROCESS, 0), "rtprio": resource.getrlimit(resource.RLIMIT_RTPRIO)}
try:
    if before["nice"] > -4 and resource.getrlimit(resource.RLIMIT_NICE)[0] >= 24:
        os.setpriority(os.PRIO_PROCESS, 0, -4)
except OSError:
    pass
rr = False
try:
    if resource.getrlimit(resource.RLIMIT_RTPRIO)[0] >= 1:
        os.sched_setscheduler(0, os.SCHED_RR, os.sched_param(1))
        rr = True
except OSError:
    pass
start = {"nice": os.getpriority(os.PRIO_PROCESS, 0), "rr": rr}
changed = tenant_limits.drop_inherited_scheduling()
again = tenant_limits.drop_inherited_scheduling()
child = subprocess.run(["sh", "-c", "grep -E '^Max (realtime|nice) priority' /proc/self/limits; "
                        "cut -d' ' -f19,41 /proc/self/stat"], capture_output=True, text=True).stdout
print(json.dumps({"start": start, "changed": changed, "again": again,
                  "rtprio": resource.getrlimit(resource.RLIMIT_RTPRIO), "nice_limit": resource.getrlimit(resource.RLIMIT_NICE),
                  "nice": os.getpriority(os.PRIO_PROCESS, 0), "policy": os.sched_getscheduler(0), "child": child}))
'''


def test_the_drop() -> None:
    done = subprocess.run([sys.executable, "-c", DROP, str(ROOT)], capture_output=True, text=True, timeout=60)
    seen = json.loads(done.stdout)
    check(seen["rtprio"] == [0, 0] and seen["nice_limit"] == [0, 0] and seen["nice"] >= 0
          and seen["policy"] == os.SCHED_OTHER, f"both limits 0:0, nice at least 0, SCHED_OTHER: {seen}")
    check(seen["again"] == [], f"a second drop changes nothing: {seen['again']}")
    if max(LAUNCHING) > 0:
        check(f"RLIMIT_RTPRIO {LAUNCHING[0]}:{LAUNCHING[1]} -> 0:0" in seen["changed"],
              f"and says what it lowered: {seen['changed']}")
    if seen["start"]["nice"] < 0:
        check(f"nice {seen['start']['nice']} -> 0" in seen["changed"], f"a negative nice is raised: {seen['changed']}")
    if seen["start"]["rr"]:
        check("a real-time scheduling policy -> SCHED_OTHER" in seen["changed"], f"SCHED_RR is left: {seen['changed']}")
    lines = seen["child"].splitlines()
    check(lines[:2] and all(line.split()[3:5] == ["0", "0"] for line in lines[:2]) and lines[2].split()[0] == "0",
          f"what it starts inherits it: limits 0 0, nice 0: {lines}")


LIMITS = """Limit                     Soft Limit           Hard Limit           Units
Max cpu time              unlimited            unlimited            seconds
Max nice priority         {nice}                   {nice}
Max realtime priority     {rt}                   {rt}
Max realtime timeout      unlimited            unlimited            us
"""


def test_reading_a_process() -> None:
    with tempfile.TemporaryDirectory(prefix="s895-proc.") as tmp:
        proc = Path(tmp)
        (proc / "7").mkdir()
        (proc / "7" / "limits").write_text(LIMITS.format(nice=31, rt=99))
        seen = tl.process_scheduling(7, proc)
        check(seen == {"rtprio": (99, 99), "nice": (31, 31)}, f"read from /proc's own text: {seen}")
        check(tl.too_permissive(seen) == ["real-time priority up to 99 (RLIMIT_RTPRIO 99:99)",
                                          "nice down to -11 (RLIMIT_NICE 31:31)"], f"{tl.too_permissive(seen)}")
        (proc / "7" / "limits").write_text(LIMITS.format(nice=20, rt=0))
        check(tl.too_permissive(tl.process_scheduling(7, proc)) == [], "RLIMIT_NICE 20 lowers nothing below 0")
        check(tl.process_scheduling(8, proc) is None, "a process that is gone: None")


def fixture_root(tmp: Path, uid: int) -> Path:
    root = tmp / "root"
    (root / "etc/switchyard/projects").mkdir(parents=True)
    (root / "home/alpha-agent").mkdir(parents=True)
    (root / "home/alpha-agent/alpha.json").write_text("{}")
    (root / "etc/switchyard/projects/alpha.json").write_text(json.dumps({"slug": "alpha",
                                                                          "config_path": "/home/alpha-agent/alpha.json"}))
    for pid, comm, owner, nice, rt in ((101, "tmux: server", uid, 31, 99), (102, "tmux: server", uid + 1, 31, 99),
                                       (103, "codex", uid, 31, 99), (104, "tmux: server", uid, 0, 0)):
        d = root / "proc" / str(pid)
        d.mkdir(parents=True)
        (d / "comm").write_text(comm + "\n")
        (d / "status").write_text(f"Name:\t{comm}\nUid:\t{owner}\t{owner}\t{owner}\t{owner}\n")
        (d / "limits").write_text(LIMITS.format(nice=nice, rt=rt))
    return root


def test_the_readiness_check() -> None:
    with tempfile.TemporaryDirectory(prefix="s895-root.") as tmp:
        root = fixture_root(Path(tmp), os.getuid())
        findings = [f for f in hd.host_diagnostics_findings(root) if f.kind == "isolation"]
        check(len(findings) == 1 and findings[0].item == "alpha's panes" and "(pid 101)" in findings[0].detail
              and "real-time priority up to 99" in findings[0].detail and "nice down to -11" in findings[0].detail,
              f"the tenant's tmux server with the launching limits is named, and only it: {findings}")
        report = hd.host_diagnostics_report(findings)
        check(report[0].startswith("switchyard: tenant readiness: these tenants' panes can take real-time scheduling")
              and "switchyard stop alpha, then switchyard start alpha" in "\n".join(report)
              and not any("hard to diagnose" in line for line in report),
              f"under its own heading, with the restart that clears it: {report}")
        (root / "proc/101/limits").write_text(LIMITS.format(nice=0, rt=0))
        check(not [f for f in hd.host_diagnostics_findings(root) if f.kind == "isolation"],
              "a server with a tenant's limits is not named")
    diagnosis = hd.HostFinding("Magic SysRq", "kernel.sysrq is 16", hd.SYSRQ_DROP_IN, "kernel.sysrq = 244\n",
                               f"sudo sysctl -p {hd.SYSRQ_DROP_IN}")
    report = hd.host_diagnostics_report([diagnosis])
    check(report[0].startswith("switchyard: host readiness: this host would be hard to diagnose") and len(report) == 5,
          f"a diagnosis finding reads as it did: {report}")


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"tenant_limits_test: {CHECKS} checks ok (launching rtprio here {LAUNCHING[0]}:{LAUNCHING[1]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
