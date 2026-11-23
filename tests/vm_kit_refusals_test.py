#!/usr/bin/env python3
"""SYRD-857: the review VM kit's test-tenant path is refused outside a kit VM, and every VM it starts is bounded.

No KVM is needed here (tests/vm_kit_test.py boots a real VM):
- the stand-in Codex and the role runner refuse wherever the kit's first-boot marker is not, and record nothing;
- inside a VM -- the marker pointed at a scratch file -- the stand-in signs in, trusts the folder it opens, fires the
  hooks Switchyard installed around every line, and runs a `!` line as its own child;
- the design artifact and workflow the kit writes load through the product's own validators;
- a VM's supervisor powers it off at its lifetime, and `down` stops a running one, then proves it gone, with a fake
  qemu standing in for the real one. A supervisor killed outright takes its qemu with it.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "tests" / "vm_kit"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(KIT))
sys.dont_write_bytecode = True  # importing the kit, or the stand-in as a module, leaves nothing in the kit
import vm_kit  # noqa: E402

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def load_standin():
    loader = importlib.machinery.SourceFileLoader("codex_standin", str(KIT / "standins" / "codex"))
    spec = importlib.util.spec_from_loader("codex_standin", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def run_standin(module, argv: list[str], *, stdin: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = module.main(argv)
            except SystemExit as exc:
                code = exc.code
    finally:
        sys.stdin = saved
    return code, out.getvalue(), err.getvalue()


def test_standin_refuses_outside_a_kit_vm() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home),
               **({"PYTHONPATH": os.environ["PYTHONPATH"]} if os.environ.get("PYTHONPATH") else {})}
        if not Path(vm_kit.MARKER).exists():  # this host is no kit VM: the real file refuses here
            for argv in (["login", "status"], ["--version"], []):
                done = subprocess.run([str(KIT / "standins" / "codex"), *argv], cwd=tmp, env=env, text=True,
                                      capture_output=True, input="hello\n", timeout=60)
                check(done.returncode == 3 and "not a switchyard-test-vm review VM" in done.stderr
                      and "Logged in" not in done.stdout,
                      f"the stand-in refuses `codex {' '.join(argv)}` on a host without the kit's marker: {done}")
            check(not (home / ".codex").exists(), "and records nothing: no trust, no config")
        module = load_standin()
        module.HOME = home / ".codex"
        module.MARKER = Path(tmp) / "marker"
        module.MARKER.write_text("switchyard-test-vm\n")
        code, out, err = run_standin(module, ["login", "status"])
        check(code == 3 and "Logged in" not in out, f"a marker without the kit's exact text is refused too: {err}")
        module.MARKER.unlink()
        os.chdir(tmp)
        code, out, err = run_standin(module, [], stdin="hello\n")
        check(code == 3 and not (home / ".codex").exists(), "a pane start outside a kit VM trusts nothing")


def test_role_runner_refuses_outside_a_kit_vm() -> None:
    if Path(vm_kit.MARKER).exists():
        return
    done = subprocess.run([sys.executable, "-B", str(KIT / "role_run.py"), "vmt", "director", "--", "true"],
                          text=True, capture_output=True, timeout=60)
    check(done.returncode != 0 and "not a switchyard-test-vm review VM" in done.stderr,
          f"role_run refuses on a host without the kit's marker: {done}")


def test_standin_inside_a_kit_vm() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        module = load_standin()
        module.MARKER = tmp_path / "marker"
        module.MARKER.write_text(vm_kit.MARKER_TEXT)
        module.HOME = tmp_path / "home" / ".codex"
        module.HOME.mkdir(parents=True)
        events = tmp_path / "events.jsonl"
        recorder = tmp_path / "record-hook"
        recorder.write_text(f"#!{sys.executable}\nimport json, sys\n"
                            f"open({str(events)!r}, 'a').write(json.dumps(json.load(sys.stdin)) + '\\n')\n")
        recorder.chmod(0o755)
        hook = str(recorder)
        (module.HOME / "hooks.json").write_text(json.dumps({"hooks": {
            event: [{"hooks": [{"type": "command", "command": hook}]}]
            for event in ("SessionStart", "UserPromptSubmit", "Stop")}}))
        code, out, _ = run_standin(module, ["login", "status"])
        check(code == 0 and "Logged in" in out, f"in a kit VM it is signed in: {out}")
        work = tmp_path / "work"
        work.mkdir()
        os.chdir(work)
        ran = tmp_path / "ran"
        code, out, _ = run_standin(module, [], stdin=f"a notification\n!echo from-the-pane >{ran}\n/exit\n")
        check(code == 0, f"/exit ends the pane: {code}")
        config = (module.HOME / "config.toml").read_text() if (module.HOME / "config.toml").exists() else ""
        check(f'[projects."{work}"]\ntrust_level = "trusted"' in config,
              f"the folder it opened is trusted as Codex records it: {config}")
        seen = [json.loads(line) for line in events.read_text().splitlines()]
        check([e["hook_event_name"] for e in seen] == ["SessionStart", "UserPromptSubmit", "Stop", "UserPromptSubmit",
                                                       "Stop"],
              f"SessionStart once, then UserPromptSubmit and Stop around each line: {seen}")
        check(len({e["session_id"] for e in seen}) == 1 and all(e["cwd"] == str(work) for e in seen),
              "one session, the pane's directory, in every payload")
        check(ran.exists() and ran.read_text().strip() == "from-the-pane", "a ! line runs as a shell command in the pane")
        check("received 14 characters" in out, f"a delivered line is read like a prompt: {out}")


def test_kit_artifacts_load_through_the_product() -> None:
    from scripts import team_launcher as launcher
    from scripts.ticket_board.workflow_config import validate

    workflow = vm_kit.tenant_workflow(ROOT, "vmt")
    validated = validate(workflow)
    check(validated["project"] == "vmt" and all(
        r.get("runtime") in (None, "codex") for r in validated["roles"]),
        "the declared workflow validates, for the tenant, with every running role on the stand-in Codex")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "vmt-project.json"
        path.write_text(json.dumps(vm_kit.tenant_artifact("vmt", workflow)))
        artifact = launcher.load_project_design_artifact(path)
    targets = {r["name"] for r in workflow["roles"] if r.get("active") and r.get("target")}
    check({role for role, _cli in artifact.role_clis} == targets and {cli for _r, cli in artifact.role_clis} == {"codex"},
          f"the design artifact loads, naming exactly the workflow's pane roles, all on codex: {artifact.role_clis}")
    for slug in ("Bad", "9lives", "a-b", ""):
        try:
            vm_kit.tenant(Path(tmp), "nothing", slug, repo=ROOT)
        except SystemExit as exc:
            check("slug" in str(exc), f"a slug the kit does not make is refused before anything: {slug!r}: {exc}")
        else:
            check(False, f"slug {slug!r} was accepted")


def test_the_scripts_run_in_the_vm_parse_and_keep_install_status() -> None:
    install = vm_kit.install_script("0" * 40, ["codex"])
    tenant = vm_kit.tenant_script("vmt", ["sudo", "switchyard", "new", "--from", "/home/operator/vmt-project.json"])
    for label, script in (("install", install), ("tenant", tenant)):
        done = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True, timeout=30)
        check(done.returncode == 0, f"the {label} script is valid bash: {done.stderr}")
    check("python-pillow" in install and "--noconfirm" in install,
          "every package the prerequisites script would ask about is installed first, without a question")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
        (tmp_path / "install").write_text('#!/bin/sh\nprintf "Proceed? [Y/n] "; read -r a; [ "$a" = y ] && exit 7; exit 9\n')
        for tool in ("sudo", "install"):
            (tmp_path / tool).chmod(0o755)
        lines = install.splitlines()
        tail = "\n".join(lines[lines.index("set +e +o pipefail"):]).replace("$(echo \"$EPOCHREALTIME - $start\" | bc)", "0").replace(
            "$(readlink /opt/switchyard/current)", "(the VM's release)")  # nothing of this host's
        done = subprocess.run(["bash", "-c", "set -euo pipefail; start=0; HOME=" + tmp + "\n" + tail], cwd=tmp,
                              env={"PATH": f"{tmp}:/usr/bin:/bin"}, text=True, capture_output=True, timeout=30)
        check(done.returncode == 7 and "install rc=7" in done.stdout,
              f"./install's question is answered yes, and its own status is the script's: {done.returncode} {done.stdout}")


def test_names_and_lifetimes_are_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in ("", "../x", "Upper", "-x", "a" * 33):
            try:
                vm_kit.vm_dir(root, name)
            except SystemExit as exc:
                check("VM name" in str(exc), f"VM name {name!r} is refused: {exc}")
            else:
                check(False, f"VM name {name!r} was accepted")
        looked: list[str] = []
        real_access = vm_kit.os.access
        vm_kit.os.access = lambda path, mode: looked.append(str(path)) or False  # nothing of this host is read
        try:
            vm_kit.boot(root, "x", image=str(root / "no-image"), cpus=1, mem=512, lifetime=0)
        except SystemExit as exc:
            check("every VM is bounded" in str(exc) and not any(root.iterdir()) and not looked,
                  f"a VM with no lifetime is refused first, before anything is looked at or made: {exc} {looked}")
        else:
            check(False, "a VM with no lifetime was booted")
        finally:
            vm_kit.os.access = real_access


def fake_vm(root: Path, name: str, *, lifetime: float) -> tuple[Path, dict]:
    """A VM directory as boot leaves it, with a fake qemu on PATH that just runs until stopped."""
    directory = root / "vms" / name
    directory.mkdir(parents=True)
    for leftover in ("overlay.qcow2", "seed.iso", "key", "key.pub"):
        (directory / leftover).write_text("x")
    info = {"name": name, "port": 1, "cpus": 1, "mem": 64, "image": "none", "lifetime": lifetime,
            "deadline": time.time() + lifetime, "booted_at": time.time()}
    (directory / "vm.json").write_text(json.dumps(info))
    return directory, info


def fake_bin(root: Path) -> Path:
    bin_dir = root / "bin"
    bin_dir.mkdir(exist_ok=True)
    qemu = bin_dir / "qemu-system-x86_64"
    qemu.write_text(f"#!{sys.executable}\nimport time\nwhile True: time.sleep(1)\n")
    ssh = bin_dir / "ssh"
    ssh.write_text("#!/bin/sh\nexit 255\n")
    for tool in (qemu, ssh):
        tool.chmod(0o755)
    return bin_dir


def qemu_pid(directory: Path) -> int:
    try:
        return int((directory / "qemu.pid").read_text().strip())
    except (OSError, ValueError):
        return 0


def alive(pid: int) -> bool:
    """By signal 0, not /proc: a reparented qemu is no longer this suite's to read."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start_supervisor(directory: Path, bin_dir: Path) -> subprocess.Popen:
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
    supervisor = subprocess.Popen([sys.executable, "-B", str(KIT / "vm_kit.py"), "_supervise", str(directory)],
                                  env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=True)
    info = json.loads((directory / "vm.json").read_text())
    info["supervisor"] = supervisor.pid
    (directory / "vm.json").write_text(json.dumps(info))
    deadline = time.monotonic() + 30
    while not qemu_pid(directory) and time.monotonic() < deadline:
        time.sleep(0.1)
    return supervisor


def test_a_vm_ends_at_its_lifetime() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        directory, _ = fake_vm(root, "short", lifetime=2)
        supervisor = start_supervisor(directory, fake_bin(root))
        check(vm_kit.survivors(directory) == [supervisor.pid, qemu_pid(directory)],
              "its supervisor runs, and its qemu runs the VM's overlay")
        try:
            supervisor.wait(timeout=60)
        except subprocess.TimeoutExpired:
            supervisor.kill()
            check(False, "the supervisor outlived its VM's lifetime")
        ended = json.loads((directory / "ended.json").read_text())
        check(ended["reason"] == "lifetime reached", f"it powered the VM off at its lifetime: {ended}")
        check(not alive(qemu_pid(directory)) and not vm_kit.survivors(directory), "and its qemu has ended")
        check(not any((directory / f).exists() for f in ("overlay.qcow2", "seed.iso", "key", "key.pub")),
              "and the overlay, seed and key are gone")


def test_down_stops_a_running_vm_and_proves_it_gone() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        directory, _ = fake_vm(root, "long", lifetime=3600)
        bin_dir = fake_bin(root)
        supervisor = start_supervisor(directory, bin_dir)
        qemu = qemu_pid(directory)
        saved = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}:{saved}"
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                vm_kit.down(root, "long", wait=2)
        finally:
            os.environ["PATH"] = saved
        supervisor.wait(timeout=30)
        check("long: gone" in out.getvalue() and "stopped by request" in out.getvalue(),
              f"down stops a guest that does not power off, and says so: {out.getvalue()}")
        check(not directory.exists() and not alive(qemu) and not alive(supervisor.pid),
              "nothing of the VM is left: no supervisor, no qemu, no directory")
        try:
            vm_kit.down(root, "long")
        except SystemExit as exc:
            check("no VM named long" in str(exc), f"a VM already gone is said to be: {exc}")


def test_down_refuses_while_the_overlay_still_runs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        directory, _ = fake_vm(root, "stray", lifetime=3600)
        qemu = fake_bin(root) / "qemu-system-x86_64"
        stray = subprocess.Popen([str(qemu), "-drive", f"file={directory / 'overlay.qcow2'},if=virtio"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (directory / "qemu.pid").write_text(f"{stray.pid}\n")  # what a supervisor that died uncleanly leaves
        try:
            deadline = time.monotonic() + 30
            while not vm_kit.pid_runs(stray.pid, str(directory / "overlay.qcow2")) and time.monotonic() < deadline:
                time.sleep(0.1)
            try:
                vm_kit.down(root, "stray", wait=1)
            except SystemExit as exc:
                check("still running" in str(exc) and (directory / "overlay.qcow2").exists(),
                      f"a qemu still running the overlay with no supervisor: down refuses and removes nothing: {exc}")
            else:
                check(False, "down removed a VM whose overlay a process still runs")
        finally:
            stray.kill()
            stray.wait()


def test_a_killed_supervisor_takes_its_qemu() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        directory, _ = fake_vm(root, "orphan", lifetime=3600)
        supervisor = start_supervisor(directory, fake_bin(root))
        qemu = qemu_pid(directory)
        check(qemu and vm_kit.pid_runs(qemu, str(directory / "overlay.qcow2")), "qemu runs")
        os.kill(supervisor.pid, signal.SIGKILL)
        supervisor.wait(timeout=30)
        deadline = time.monotonic() + 15
        while alive(qemu) and time.monotonic() < deadline:
            time.sleep(0.1)
        survived = alive(qemu)
        if survived:  # a mutant's orphan: never leave it running
            os.kill(qemu, signal.SIGKILL)
        check(not survived, "a supervisor killed outright takes its qemu with it (parent-death signal)")
        out = io.StringIO()
        with redirect_stdout(out):
            vm_kit.down(root, "orphan", wait=1)
        check("orphan: gone" in out.getvalue() and not directory.exists(), "and down still clears what it left")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            try:
                case()
            finally:
                os.chdir(ROOT)  # never left in a case's removed scratch directory
    print(f"vm_kit_refusals_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
