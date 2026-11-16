#!/usr/bin/env python3
"""SYRD-808: `host_state_guard` refuses, records and reports every reach of this host's Switchyard state.

Each probe runs in a child Python that installs the guard, so this process is never guarded and never reaches
anything. The probes that start privileged tools ask only for `--version`: if the guard were broken they would run
that, which reads nothing and changes nothing, and nothing escalates.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


PRELUDE = f"""
import json, os, subprocess, sys
sys.path.insert(0, {str(HERE)!r})
import host_state_guard as guard
"""


def probe(body: str, *, env: dict | None = None) -> tuple[subprocess.CompletedProcess, dict]:
    """`body` in a guarded child; it prints one JSON line starting RESULT."""
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("SWITCHYARD_TEST_HOST_GUARD")}
    child_env.pop("PYTHONPATH", None)
    child_env.update(env or {})
    done = subprocess.run([sys.executable, "-c", PRELUDE + body], capture_output=True, text=True, env=child_env,
                          timeout=120)
    line = next((l for l in done.stdout.splitlines() if l.startswith("RESULT ")), None)
    return done, (json.loads(line[len("RESULT "):]) if line else {})


def refused(action: str) -> str:
    return f"""
try:
    out = repr({action})
except PermissionError as exc:
    out = "refused: " + str(exc)
"""


def test_paths_are_refused_and_recorded_even_when_swallowed() -> None:
    done, result = probe(f"""
from pathlib import Path
guard.install()
outcomes = {{}}
for name, action in {{
    "read": "Path('/etc/switchyard/projects/pgu.json').read_text()",
    "list": "os.listdir('/usr/local/lib/switchyard')",
    "unit": "open('/etc/systemd/system/pgu-ticket-board.service')",
    "venv": "os.stat('/opt/switchyard/venv/bin/python')",
    "journal": "os.listdir('/var/lib/switchyard/rollout')",
    "init": "open('/proc/1/status').read()",
}}.items():
    try:
        outcomes[name] = repr(eval(action))
    except PermissionError as exc:
        outcomes[name] = "refused"
swallowed = Path('/etc/switchyard/provision').exists()
own = open(f'/proc/{{os.getpid()}}/status').read()[:4]
child = subprocess.run(['sh', '-c', 'cat /proc/$$/stat > /dev/null && echo ok'], capture_output=True, text=True).stdout
fine = os.path.isdir('/usr/share')
print("RESULT " + json.dumps({{"outcomes": outcomes, "swallowed": swallowed, "own": own, "child": child.strip(),
                              "fine": fine, "reached": guard.reached()}}))
guard.assert_untouched("probe") if False else guard._state.update(asserted=True)
""")
    outcomes = result.get("outcomes", {})
    check(outcomes and set(outcomes.values()) == {"refused"},
          f"a read, listing, unit, venv, journal and another process's /proc entry are each refused: {outcomes} "
          f"{done.stderr[-400:]}")
    reached = result.get("reached", [])
    check(result.get("swallowed") is False and any("/etc/switchyard/provision" in line for line in reached),
          f"a reach the caller swallows (exists() is False) is still recorded: {reached}")
    check(len(reached) == 7 and all("probe" not in line or "pid" in line for line in reached),
          f"each reach is recorded once, with its process: {reached}")
    check(result.get("own") == "Name" and result.get("child") == "ok" and result.get("fine") is True,
          f"its own and its children's /proc entries, and any other path, are untouched: {result}")


def test_a_link_into_host_state_counts() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd808g.") as tmp:
        link = Path(tmp) / "looks-local"
        link.symlink_to("/etc/switchyard")
        done, result = probe(f"""
from pathlib import Path
guard.install()
try:
    Path({str(link / 'projects')!r}).read_text()
    out = "read"
except PermissionError:
    out = "refused"
except OSError as exc:
    out = "real error: " + type(exc).__name__
print("RESULT " + json.dumps({{"out": out, "reached": guard.reached()}}))
guard._state.update(asserted=True)
""")
    check(result.get("out") == "refused" and any("/etc/switchyard/projects" in l for l in result.get("reached", [])),
          f"a path through a link into /etc/switchyard is refused as that path: {result} {done.stderr[-300:]}")


def test_privileged_tools_are_refused_queries_included_and_stand_ins_are_not() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd808g.") as tmp:
        stand_in = Path(tmp) / "bin"
        stand_in.mkdir()
        (stand_in / "systemctl").write_text("#!/bin/sh\necho stand-in\n")
        (stand_in / "systemctl").chmod(0o755)
        done, result = probe(f"""
guard.install()
outcomes = {{}}
for name, argv in {{
    "sudo": ["sudo", "--version"],
    "absolute": ["/usr/bin/sudo", "--version"],
    "query": ["systemctl", "--version"],
    "journal": ["journalctl", "--version"],
    "runuser": ["runuser", "--version"],
    "names a host path": ["cat", "/etc/switchyard/projects/pgu.json"],
}}.items():
    try:
        outcomes[name] = subprocess.run(argv, capture_output=True, env={{"PATH": "/usr/bin:/bin"}}).returncode
    except PermissionError:
        outcomes[name] = "refused"
try:
    outcomes["os.system"] = os.waitstatus_to_exitcode(os.system("systemctl --version > /dev/null 2>&1"))
except PermissionError:
    outcomes["os.system"] = "refused"
stand = subprocess.run(["systemctl", "status"], capture_output=True, text=True,
                       env={{"PATH": {str(stand_in)!r} + ":/usr/bin:/bin"}}).stdout.strip()
print("RESULT " + json.dumps({{"outcomes": outcomes, "stand_in": stand, "reached": guard.reached()}}))
guard._state.update(asserted=True)
""")
    outcomes, reached = result.get("outcomes", {}), result.get("reached", [])
    # By name the tool is the refusing shim first on the child's PATH (exit 126); by path the guard refuses it in
    # process. Either way nothing real runs, and the reach is recorded.
    check(len(outcomes) == 7 and set(outcomes.values()) <= {"refused", 126} and len(reached) == 7,
          f"the real sudo (by name or path), systemctl and journalctl queries, runuser, os.system and a command "
          f"naming a host path each run nothing and are recorded: {outcomes} {reached} {done.stderr[-300:]}")
    check(outcomes.get("absolute") == "refused" and outcomes.get("names a host path") == "refused"
          and any(l.startswith("shim refused sudo --version") for l in reached),
          f"a tool named by path is refused in process, and by name meets the shim: {outcomes} {reached}")
    check(result.get("stand_in") == "stand-in",
          f"a stand-in a suite puts first on PATH is not refused: {result.get('stand_in')!r}")


def test_children_carry_the_guard_and_meet_the_shims() -> None:
    done, result = probe("""
guard.install()
read = "from pathlib import Path; Path('/etc/switchyard/projects').exists()"
codes = {
    "inherited": subprocess.run([sys.executable, "-c", read]).returncode,
    "replaced env": subprocess.run([sys.executable, "-c", read], env={"PATH": "/usr/bin:/bin"}).returncode,
    "empty env": subprocess.run([sys.executable, "-c", read], env={}).returncode,
    "grandchild": subprocess.run([sys.executable, "-c",
                                  "import subprocess, sys; subprocess.run([sys.executable, '-c', " + repr(read) + "])"]
                                 ).returncode,
    "shell sudo": subprocess.run(["bash", "-c", "sudo --version"], env={"PATH": "/usr/bin:/bin"}).returncode,
    "shell systemctl": subprocess.run(["sh", "-c", "systemctl --version"], env={}).returncode,
}
print("RESULT " + json.dumps({"codes": codes, "reached": guard.reached(), "shims": os.environ.get(guard.SHIMS_ENV)}))
guard._state.update(asserted=True)
""")
    reached = result.get("reached", [])
    check(sum("stat /etc/switchyard/projects" in line for line in reached) == 4,
          f"a Python child -- inheriting, with a replaced or empty environment, or a grandchild -- is guarded and its "
          f"reach recorded in the run's record: {reached} {done.stderr[-300:]}")
    codes = result.get("codes", {})
    check(codes.get("shell sudo") == 126 and codes.get("shell systemctl") == 126
          and any(l.startswith("shim refused sudo --version") for l in reached)
          and any(l.startswith("shim refused systemctl --version") for l in reached),
          f"a shell a test starts meets the refusing shims first on its PATH, even given its own: {codes} {reached}")


def test_a_reset_path_or_an_isolated_python_is_judged_as_it_will_run() -> None:
    """Audit of 32f8b75b: `env -i systemctl` found the shim on the caller's PATH, was allowed, and ran the real tool;
    a `python3 -I` child never loaded the guard. Each is now refused and recorded."""
    with tempfile.TemporaryDirectory(prefix="syrd808g.") as tmp:
        stand_in = Path(tmp) / "bin"
        stand_in.mkdir()
        (stand_in / "systemctl").write_text("#!/bin/sh\necho stand-in\n")
        (stand_in / "systemctl").chmod(0o755)
        done, result = probe(f"""
guard.install()
outcomes = {{}}
for name, argv in {{
    "env -i": ["env", "-i", "systemctl", "--version"],
    "env PATH=": ["env", "PATH=/usr/bin:/bin", "systemctl", "--version"],
    "env -u PATH": ["env", "-u", "PATH", "sudo", "--version"],
    "shell assignment": ["sh", "-c", "PATH=/usr/bin:/bin sudo --version"],
    "shell export": ["bash", "-c", "export PATH=/usr/bin; systemctl --version"],
    "command -p": ["sh", "-c", "command -p sudo --version"],
    "shell absolute path": ["sh", "-c", "/usr/bin/sudo --version"],
    "python -I": [sys.executable, "-I", "-c", "pass"],
    "python -E": [sys.executable, "-E", "-c", "pass"],
    "python -S": [sys.executable, "-S", "-c", "pass"],
    "python, env -i": ["env", "-i", sys.executable, "-c", "pass"],
    "python, PYTHONPATH unset": ["env", "-u", "PYTHONPATH", sys.executable, "-c", "pass"],
    "python -I from a shell": ["sh", "-c", sys.executable + " -I -c pass"],
}}.items():
    try:
        outcomes[name] = subprocess.run(argv, capture_output=True).returncode
    except PermissionError:
        outcomes[name] = "refused"
try:
    outcomes["shell=True"] = subprocess.run("PATH=/usr/bin sudo --version", shell=True, capture_output=True).returncode
except PermissionError:
    outcomes["shell=True"] = "refused"
# A preexec_fn keeps subprocess off posix_spawn, so only Popen's own check sees the shell string.
try:
    outcomes["shell=True, forked"] = subprocess.run("PATH=/usr/bin sudo --version", shell=True, capture_output=True,
                                                    preexec_fn=lambda: None).returncode
except PermissionError:
    outcomes["shell=True, forked"] = "refused"
allowed = {{
    "a stand-in on its own PATH": subprocess.run(["env", "PATH={stand_in}:/usr/bin", "systemctl", "status"],
                                                 capture_output=True, text=True).stdout.strip(),
    "a guarded python": subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True).stdout.strip(),
    "a wrapped python": subprocess.run(["timeout", "5", sys.executable, "-c", "print('ok')"], capture_output=True,
                                       text=True).stdout.strip(),
}}
print("RESULT " + json.dumps({{"outcomes": outcomes, "allowed": allowed, "reached": guard.reached()}}))
guard._state.update(asserted=True)
""")
    outcomes, reached = result.get("outcomes", {}), result.get("reached", [])
    tools = {k: v for k, v in outcomes.items() if not k.startswith("python")}
    check(len(tools) == 9 and set(tools.values()) == {"refused"}
          and sum("exec of the real" in line for line in reached) == 9,
          f"a privileged tool after env -i, env PATH=, env -u PATH, a shell assignment or export, command -p, an "
          f"absolute path in a shell, or shell=True is refused before it runs, and recorded: {tools} {reached} "
          f"{done.stderr[-300:]}")
    pythons = {k: v for k, v in outcomes.items() if k.startswith("python")}
    check(len(pythons) == 6 and set(pythons.values()) == {"refused"}
          and sum("an unguarded Python child" in line for line in reached) == 6,
          f"a Python child given -I, -E or -S, or an environment without the guard, from Python or a shell, is "
          f"refused and recorded: {pythons} {reached}")
    check(result.get("allowed") == {"a stand-in on its own PATH": "stand-in", "a guarded python": "ok",
                                    "a wrapped python": "ok"},
          f"a stand-in first on the PATH that applies, and a Python child that carries the guard, still run: "
          f"{result.get('allowed')}")


def test_what_the_guard_cannot_model_is_refused() -> None:
    """SYRD-816 (Audit of e15974c7): each of these ran the real tool, or an unguarded Python, with nothing recorded."""
    with tempfile.TemporaryDirectory(prefix="syrd816g.") as tmp:
        stand_in = Path(tmp) / "bin"
        stand_in.mkdir()
        (stand_in / "systemctl").write_text("#!/bin/sh\necho stand-in\n")
        (stand_in / "systemctl").chmod(0o755)
        done, result = probe(f"""
guard.install()
outcomes = {{}}
for name, argv in {{
    "an expansion in a PATH assignment": ["sh", "-c", "p=/usr/bin:/bin; PATH=$p systemctl --version"],
    "an expansion in an exported PATH": ["sh", "-c", "export PATH=`echo /usr/bin`; systemctl --version"],
    "an expansion in a command word": ["sh", "-c", "t=systemctl; $t --version"],
    "eval": ["sh", "-c", "eval 'PATH=/usr/bin:/bin systemctl --version'"],
    "sourcing": ["sh", "-c", ". /dev/null; sudo --version"],
    "a function": ["sh", "-c", "f() {{ PATH=/usr/bin:/bin systemctl --version; }}; f"],
    "a relative command after cd": ["sh", "-c", "cd /usr/bin && ./systemctl --version"],
    "an assembled command name": ["sh", "-c", "s=sys; t=temctl; PATH=/usr/bin:/bin $s$t --version"],
    "an expanded command naming no tool": ["sh", "-c", "t=echo; $t ok"],
    "an expanded command after exec": ["sh", "-c", "t=systemctl; exec $t --version"],
}}.items():
    try:
        outcomes[name] = subprocess.run(argv, capture_output=True).returncode
    except PermissionError:
        outcomes[name] = "refused"
# Audit of 47083b2f: a reserved word hid the simple command after it, so its PATH reset passed.
reserved = {{}}
for name, argv in {{
    "if ... then": ["sh", "-c", "if true; then PATH=/usr/bin:/bin systemctl --version; fi"],
    "while ... do": ["sh", "-c", "while true; do PATH=/usr/bin:/bin systemctl --version; break; done"],
    "a brace group": ["sh", "-c", "{{ PATH=/usr/bin:/bin systemctl --version; }}"],
    "!": ["sh", "-c", "! PATH=/usr/bin:/bin sudo --version"],
    "time -p": ["bash", "-c", "time -p PATH=/usr/bin:/bin sudo --version"],
}}.items():
    try:
        reserved[name] = subprocess.run(argv, capture_output=True).returncode
    except PermissionError:
        reserved[name] = "refused"
for name, argv in {{
    "python -X utf8 -I": [sys.executable, "-X", "utf8", "-I", "-c", "pass"],
    "python -Xutf8 -E": [sys.executable, "-Xutf8", "-E", "-c", "pass"],
    "python -W ignore -S": [sys.executable, "-W", "ignore", "-S", "-c", "pass"],
}}.items():
    try:
        outcomes[name] = subprocess.run(argv, capture_output=True).returncode
    except PermissionError:
        outcomes[name] = "refused"
allowed = {{
    "an option of the program, not the interpreter": subprocess.run(
        [sys.executable, "-X", "utf8", "-c", "import sys; print(sys.argv[1])", "-I"], capture_output=True,
        text=True).stdout.strip(),
    "a function in a script naming no tool": subprocess.run(["sh", "-c", "f() {{ echo ok; }}; f"], capture_output=True,
                                                           text=True).stdout.strip(),
    "a reserved word before an ordinary command": subprocess.run(["sh", "-c", "if true; then echo ok; fi"],
                                                                 capture_output=True, text=True).stdout.strip(),
    "an absolute command after cd": subprocess.run(["sh", "-c", "cd /tmp && /bin/echo ok"], capture_output=True,
                                                   text=True).stdout.strip(),
    "a stand-in on its own PATH": subprocess.run(["env", "PATH={stand_in}:/usr/bin", "systemctl", "status"],
                                                 capture_output=True, text=True).stdout.strip(),
}}
print("RESULT " + json.dumps({{"outcomes": outcomes, "reserved": reserved, "allowed": allowed,
                              "reached": guard.reached()}}))
guard._state.update(asserted=True)
""")
    outcomes, reached = result.get("outcomes", {}), result.get("reached", [])
    reserved = result.get("reserved", {})
    check(len(reserved) == 5 and set(reserved.values()) == {"refused"}
          and sum(line.startswith("exec of the real") for line in reached) == 5,
          f"after if/then, while/do, a brace, ! or time, a command's own PATH reset is seen and its real tool refused: "
          f"{reserved} {[l for l in reached if l.startswith('exec of the real')]}")
    shells = {k: v for k, v in outcomes.items() if not k.startswith("python")}
    check(len(shells) == 10 and set(shells.values()) == {"refused"}
          and sum("a shell script the guard cannot model" in line for line in reached) == 10,
          f"a script naming a tool is refused, and recorded, for an expansion where a command or its PATH is decided, "
          f"eval, sourcing, a function and a relative command after cd: {shells} {reached} {done.stderr[-300:]}")
    for shape, why in (("p=/usr/bin", "an expansion in a PATH assignment"), ("`echo", "an expansion in a PATH assignment"),
                       ("$t", "an expansion in a command word"), ("eval '", "`eval`"), (". /dev/null", "`.`"),
                       ("f() {", "a function definition"), ("cd /usr/bin", "a relative command after cd"),
                       ("$s$t", "an expansion in a command word"), ("t=echo; $t ok", "an expansion in a command word")):
        check(any(shape in line and f"({why})" in line for line in reached), f"{shape!r} is recorded as {why}: {reached}")
    pythons = {k: v for k, v in outcomes.items() if k.startswith("python")}
    check(set(pythons.values()) == {"refused"} and sum("an unguarded Python child" in l for l in reached) == 3,
          f"an -I, -E or -S after an option that takes a value is seen: {pythons} {reached}")
    check(result.get("allowed") == {"an option of the program, not the interpreter": "-I",
                                    "a function in a script naming no tool": "ok",
                                    "a reserved word before an ordinary command": "ok",
                                    "an absolute command after cd": "ok", "a stand-in on its own PATH": "stand-in"},
          f"what the guard does model still runs: {result.get('allowed')}")


def test_the_run_fails_on_any_reach_and_never_runs_as_root() -> None:
    done, result = probe("""
guard.install()
try:
    os.stat('/etc/switchyard')
except PermissionError:
    pass
try:
    guard.assert_untouched("probe suite")
    out = "passed"
except AssertionError as exc:
    out = str(exc)
print("RESULT " + json.dumps({"out": out}))
""")
    check(result.get("out", "").startswith("FAILED CHECK: probe suite reached host state 1 time(s): stat /etc/switchyard"),
          f"the suite's last check fails, naming each reach: {result}")
    done, _ = probe("""
guard.install()
try:
    os.stat('/opt/switchyard')
except PermissionError:
    pass
""")
    check(done.returncode == 98 and "never checked it" in done.stderr,
          f"a run that reached host state and never checked exits 98: {done.returncode} {done.stderr[-300:]}")
    done, result = probe("""
guard.install()
log, shims = os.environ[guard.LOG_ENV], os.environ[guard.SHIMS_ENV]
guard.assert_untouched("clean suite")
print("RESULT " + json.dumps({"log": log, "shims": shims}))
""")
    check(done.returncode == 0 and result and not os.path.exists(result["log"]) and not os.path.exists(result["shims"]),
          f"a clean run passes and leaves neither its record nor its shims behind: {done.returncode} {result}")
    done, _ = probe("""
os.getuid = lambda: 0
guard.install()
""")
    check(done.returncode != 0 and "never runs as root" in done.stderr,
          f"the guard refuses to start as root: {done.returncode} {done.stderr[-200:]}")


def main() -> int:
    for name, value in list(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"host_state_guard_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
