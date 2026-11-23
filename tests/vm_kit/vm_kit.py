#!/usr/bin/env python3
"""A disposable KVM VM for reviewing a candidate release the way production runs it (SYRD-857).

    tests/vm_kit/vm_kit.py fetch-image
    tests/vm_kit/vm_kit.py boot NAME [--lifetime SECONDS] [--cpus N] [--mem MB] [--image PATH]
    tests/vm_kit/vm_kit.py install NAME [--repo DIR] [--commit REV]
    tests/vm_kit/vm_kit.py tenant NAME SLUG
    tests/vm_kit/vm_kit.py run NAME [--role SLUG/ROLE | --as USER | --root] [--timeout SECONDS] -- COMMAND...
    tests/vm_kit/vm_kit.py deliveries NAME SLUG
    tests/vm_kit/vm_kit.py logs NAME DEST
    tests/vm_kit/vm_kit.py down NAME
    tests/vm_kit/vm_kit.py list
    tests/vm_kit/vm_kit.py check [--repo DIR] [--commit REV] [--keep-logs DEST] [--slug SLUG] [--lifetime SECONDS]

Everything runs as the invoking account, never root, and nothing on the host is changed but the kit's own directory
(`$XDG_CACHE_HOME/switchyard-test-vm`, or `--state-dir`). Inside the VM the kit is root, so `./install` and
`switchyard new` run for real with nothing live to reach.

**Every VM is bounded.** `boot` starts a supervisor in its own session, which runs qemu as its child (qemu dies with
it). The supervisor powers the VM off at its lifetime (one hour by default) and, however qemu ends, removes the
overlay, the seed and the VM's keys. `down` asks the guest to power off, waits for the supervisor, then checks that
neither process it started (the supervisor and qemu) still runs and that the overlay is gone, and says so. `check`
always ends with `down` for a VM it created. The kit looks at no other process on the host. ssh and scp believe only
the guest host key the kit made and seeded, pinned in the VM's own known_hosts.

**Test tenants get past sign-in with stand-in CLIs, not a flag** (`tests/vm_kit/standins/`). Each stand-in refuses
to run unless the machine carries the marker only this kit's first-boot seed writes, so a stand-in copied anywhere
else does nothing. `switchyard new` is unchanged. `run --role` types the command into that role's live pane
(role_run.py), because the board takes a role's writes only from its pane. See docs/syrd-857-test-vm.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
STANDINS = HERE / "standins"
IMAGE_URL = "https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2"
IMAGE_NAME = "Arch-Linux-x86_64-cloudimg.qcow2"
#: What only this kit's first-boot seed writes. The stand-ins refuse to run without it.
MARKER = "/etc/switchyard-test-vm"
MARKER_TEXT = "switchyard-test-vm: a disposable review VM; stand-in agent CLIs are allowed here\n"
DEFAULT_LIFETIME = 3600
#: A fresh Arch image's packages before ./install: scripts/install-switchyard-prereqs's own pacman list (it asks
#: before installing a missing one), and bc for the kit's timing.
PACKAGES = ("python", "postgresql", "tmux", "git", "curl", "python-pillow", "python-psycopg", "acl", "polkit", "bc")
USER = "operator"
HOST_KEY = "host_ed25519"  # the guest's ssh host key, made on the host and seeded, so it can be pinned
NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


class KitError(SystemExit):
    def __init__(self, message: str) -> None:
        super().__init__(f"vm_kit: {message}")


class NameTaken(KitError):
    """A VM of that name already exists: someone else's to keep or take down."""


def say(message: str) -> None:
    print(f"vm_kit: {message}", flush=True)


# --- where things live -------------------------------------------------------------------------------------------


def state_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "switchyard-test-vm"


def vm_dir(root: Path, name: str) -> Path:
    if not name or not set(name) <= NAME_CHARS or name.startswith("-") or len(name) > 32:
        raise KitError(f"a VM name is 1-32 of a-z, 0-9 and '-': {name!r}")
    return root / "vms" / name


def read_info(directory: Path) -> dict:
    try:
        return json.loads((directory / "vm.json").read_text())
    except (OSError, ValueError):
        raise KitError(f"no VM at {directory} (boot it first)") from None


def pid_runs(pid: int, needle: str) -> bool:
    """True when `pid` is alive and its command line names `needle`, so a reused pid is never mistaken for ours."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # gone: say so without reading anything of it
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    return needle in cmdline


def survivors(directory: Path) -> list[int]:
    """Which of the two processes the kit started for this VM still run: its supervisor, and its qemu (by the pid
    the supervisor recorded, still running this VM's overlay). Nothing else on the host is looked at."""
    try:
        supervisor = int(json.loads((directory / "vm.json").read_text()).get("supervisor", 0))
    except (OSError, ValueError):
        supervisor = 0
    try:
        qemu = int((directory / "qemu.pid").read_text().strip())
    except (OSError, ValueError):
        qemu = 0
    return [pid for pid, needle in ((supervisor, str(directory)), (qemu, str(directory / "overlay.qcow2")))
            if pid > 0 and pid_runs(pid, needle)]


# --- the image ---------------------------------------------------------------------------------------------------


def fetch_image(root: Path, *, url: str = IMAGE_URL) -> Path:
    """The Arch cloud image, verified against the mirror's published SHA-256, downloaded once."""
    images = root / "images"
    images.mkdir(parents=True, exist_ok=True)
    target = images / IMAGE_NAME
    if target.exists() and (images / f"{IMAGE_NAME}.verified").exists():
        return target
    with urllib.request.urlopen(url + ".SHA256", timeout=60) as response:
        expected = response.read().decode().split()[0].lower()
    partial = images / f"{IMAGE_NAME}.part"
    digest = hashlib.sha256()
    say(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as out:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != expected:
        partial.unlink(missing_ok=True)
        raise KitError(f"{url}: SHA-256 {digest.hexdigest()} is not the published {expected}")
    partial.rename(target)
    (images / f"{IMAGE_NAME}.verified").write_text(expected + "\n")
    say(f"verified {target} ({expected})")
    return target


def base_image(root: Path, explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).resolve()
        if not path.is_file():
            raise KitError(f"no image at {path}")
        return path
    cached = root / "images" / IMAGE_NAME
    if cached.exists() and (root / "images" / f"{IMAGE_NAME}.verified").exists():
        return cached
    return fetch_image(root)


# --- boot --------------------------------------------------------------------------------------------------------


def tool(argv: list[str], what: str) -> None:
    """A host tool boot needs, its own words kept when it fails, so the real cause is the error."""
    done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if done.returncode != 0:
        raise KitError(f"{what}: `{argv[0]}` exited {done.returncode}: {(done.stderr or done.stdout).strip()[-1500:]}")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def cloud_config(directory: Path, name: str, public_key: str) -> dict:
    """The first-boot cloud-config: the operator account with this VM's key, the guest's ssh host key and nothing
    else (made on the host, so the host can pin it), and the root-owned marker the stand-ins require."""
    return {
        "hostname": name,
        "users": [{"name": USER, "groups": ["wheel"], "sudo": "ALL=(ALL) NOPASSWD:ALL", "shell": "/bin/bash",
                   "lock_passwd": True, "ssh_authorized_keys": [public_key]}],
        "write_files": [{"path": MARKER, "permissions": "0644", "owner": "root:root", "content": MARKER_TEXT}],
        "growpart": {"mode": "auto", "devices": ["/"]},
        # The guest's sshd answers with this key and no other; generated keys would be ones the host never saw.
        "ssh_deletekeys": True,
        "ssh_genkeytypes": [],
        "ssh_keys": {"ed25519_private": (directory / HOST_KEY).read_text(),
                     "ed25519_public": (directory / f"{HOST_KEY}.pub").read_text().strip()},
    }


def seed_iso(directory: Path, name: str, public_key: str) -> Path:
    """The NoCloud seed: the operator account with this VM's key, the guest's own ssh host key (made here, so the
    host can pin it), and the marker the stand-ins require."""
    seed = directory / "seed"
    seed.mkdir()
    (seed / "meta-data").write_text(f"instance-id: {name}-{int(time.time())}\nlocal-hostname: {name}\n")
    user_data = cloud_config(directory, name, public_key)
    (seed / "user-data").write_text("#cloud-config\n" + json.dumps(user_data, indent=1) + "\n")
    tool = next((t for t in ("xorriso", "mkisofs", "genisoimage") if shutil.which(t)), None)
    if tool is None:
        raise KitError("building the seed needs xorriso, mkisofs or genisoimage")
    argv = [tool, *(["-as", "mkisofs"] if tool == "xorriso" else []), "-quiet", "-V", "cidata", "-J", "-r",
            "-o", str(directory / "seed.iso"), str(seed)]
    built = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if built.returncode != 0:
        raise KitError(f"{tool} could not build the seed: {built.stderr.strip()[-1000:]}")
    shutil.rmtree(seed)
    return directory / "seed.iso"


def qemu_argv(directory: Path, info: dict) -> list[str]:
    return ["qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-smp", str(info["cpus"]), "-m", str(info["mem"]),
            "-name", f"switchyard-test-vm-{info['name']}",
            "-drive", f"file={directory / 'overlay.qcow2'},if=virtio",
            "-drive", f"file={directory / 'seed.iso'},media=cdrom,readonly=on",
            "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{info['port']}-:22", "-device", "virtio-net-pci,netdev=n0",
            "-display", "none", "-serial", f"file:{directory / 'serial.log'}",
            "-qmp", f"unix:{directory / 'qmp.sock'},server=on,wait=off"]


def boot(root: Path, name: str, *, image: str | None, cpus: int, mem: int, lifetime: int, wait: int = 300) -> dict:
    if lifetime <= 0:
        raise KitError("a VM needs a lifetime: every VM is bounded")
    if os.geteuid() == 0:
        raise KitError("refusing to run as root: the kit needs nothing of root on the host")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise KitError("/dev/kvm is not usable by this account")
    directory = vm_dir(root, name)
    taken = NameTaken(f"{name} already exists at {directory}: `down {name}` first, or pick another name")
    if directory.exists():
        raise taken
    backing = base_image(root, image)
    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        directory.mkdir()  # atomically ours, or someone else's
    except FileExistsError:
        raise taken from None
    supervisor: subprocess.Popen | None = None
    try:
        directory.chmod(0o700)
        for key, comment in (("key", f"switchyard-test-vm-{name}"), (HOST_KEY, f"switchyard-test-vm-{name}-host")):
            tool(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(directory / key)],
                 f"{name}: making its ssh keys")
        pin_host_key(directory, name)
        seed_iso(directory, name, (directory / "key.pub").read_text().strip())
        tool(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(backing),
              str(directory / "overlay.qcow2"), "20G"], f"{name}: making its overlay on {backing}")
        info = {"name": name, "port": free_port(), "cpus": cpus, "mem": mem, "image": str(backing),
                "lifetime": lifetime, "deadline": time.time() + lifetime, "booted_at": time.time()}
        (directory / "vm.json").write_text(json.dumps(info, indent=1) + "\n")
        with (directory / "supervisor.log").open("ab") as log:
            # -s, not -I: no user site, and a suite's host-state guard still follows it (the supervisor runs as this
            # account, so -I guarded no boundary).
            supervisor = subprocess.Popen([sys.executable, "-s", "-B", str(Path(__file__).resolve()), "_supervise",
                                           str(directory)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                          start_new_session=True, close_fds=True)
        info["supervisor"] = supervisor.pid
        (directory / "vm.json").write_text(json.dumps(info, indent=1) + "\n")
    except BaseException:
        # The directory is wholly this boot's. A supervisor it started is stopped first (it powers off and cleans
        # up after itself); then nothing of the VM runs, and nothing of it is left for anyone to remove by hand.
        if supervisor is not None:
            supervisor.terminate()
            try:
                supervisor.wait(timeout=150)
            except subprocess.TimeoutExpired:
                supervisor.kill()  # its qemu dies with it (PR_SET_PDEATHSIG)
                supervisor.wait()
        shutil.rmtree(directory, ignore_errors=True)
        raise
    say(f"{name}: booting (ssh 127.0.0.1:{info['port']}, powered off by {time.ctime(info['deadline'])} at the latest)")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if supervisor.poll() is not None:
            raise KitError(f"{name}: qemu ended during boot; see {directory / 'supervisor.log'}")
        if ssh(directory, info, ["true"], timeout=15, check=False, quiet=True).returncode == 0:
            ssh(directory, info, ["cloud-init", "status", "--wait"], timeout=wait, check=False, quiet=True)
            say(f"{name}: up")
            return info
        time.sleep(3)
    down(root, name)
    raise KitError(f"{name}: no ssh within {wait}s (or the guest never showed the host key the kit seeded); "
                   "powered off")


def supervise(directory: Path) -> int:
    """Run qemu until it ends or the lifetime passes, then remove what only this VM used. Runs detached."""
    info = json.loads((directory / "vm.json").read_text())
    ended = {"reason": "", "at": 0.0, "qemu_exit": None}
    stop = {"asked": False}

    def asked(_signum, _frame):
        stop["asked"] = True

    signal.signal(signal.SIGTERM, asked)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def die_with_supervisor() -> None:  # qemu never outlives its supervisor
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG

    qemu = subprocess.Popen(qemu_argv(directory, info), stdin=subprocess.DEVNULL, preexec_fn=die_with_supervisor)
    (directory / "qemu.pid").write_text(f"{qemu.pid}\n")
    print(f"{time.ctime()} qemu {qemu.pid} started", flush=True)
    while qemu.poll() is None and not stop["asked"] and time.time() < info["deadline"]:
        time.sleep(1)
    if qemu.poll() is None:
        ended["reason"] = "stopped by request" if stop["asked"] else "lifetime reached"
        print(f"{time.ctime()} {ended['reason']}: powering off", flush=True)
        try:
            # A guest asked to power off gets 90 seconds; with nobody to ask (no monitor), it is stopped at once.
            qemu.wait(timeout=90 if qmp(directory, "system_powerdown") else 0.1)
        except subprocess.TimeoutExpired:
            qemu.terminate()
            try:
                qemu.wait(timeout=15)
            except subprocess.TimeoutExpired:
                qemu.kill()
                qemu.wait()
    else:
        ended["reason"] = "the guest powered off"
    ended["qemu_exit"] = qemu.returncode
    for leftover in ("overlay.qcow2", "seed.iso", "key", "key.pub", HOST_KEY, f"{HOST_KEY}.pub", "qmp.sock"):
        (directory / leftover).unlink(missing_ok=True)
    ended["at"] = time.time()
    (directory / "ended.json").write_text(json.dumps(ended) + "\n")
    print(f"{time.ctime()} qemu exited {qemu.returncode}; overlay, seed and keys removed ({ended['reason']})", flush=True)
    return 0


def qmp(directory: Path, command: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(5)
            conn.connect(str(directory / "qmp.sock"))
            conn.recv(4096)
            for message in ({"execute": "qmp_capabilities"}, {"execute": command}):
                conn.sendall(json.dumps(message).encode() + b"\n")
                conn.recv(4096)
        return True
    except OSError:
        return False


# --- talking to it -----------------------------------------------------------------------------------------------


def pin_host_key(directory: Path, name: str) -> None:
    """The per-VM known_hosts: the guest's host key under the VM's own alias, so ssh trusts that key alone."""
    kind, key = (directory / f"{HOST_KEY}.pub").read_text().split()[:2]
    (directory / "known_hosts").write_text(f"switchyard-test-vm-{name} {kind} {key}\n")


def ssh_options(directory: Path, info: dict) -> list[str]:
    """For ssh and scp alike. The port was free when boot picked it, but another account could bind it first: the
    guest is believed only when it shows the host key the kit seeded, never one met for the first time. No ssh
    config is read, the system's or the account's, so nothing there can loosen these options."""
    return ["-F", "/dev/null", "-i", str(directory / "key"), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={directory / 'known_hosts'}",
            "-o", f"HostKeyAlias=switchyard-test-vm-{info['name']}", "-o", "HostKeyAlgorithms=ssh-ed25519",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=30"]


def ssh_base(directory: Path, info: dict) -> list[str]:
    return ["ssh", *ssh_options(directory, info), "-p", str(info["port"]), f"{USER}@127.0.0.1"]


def ssh(directory: Path, info: dict, argv: list[str], *, timeout: float, check: bool = True, quiet: bool = False,
        stdin=subprocess.DEVNULL, capture: bool = False) -> subprocess.CompletedProcess:
    """One command in the VM, as argv: each word is quoted for the remote shell, so nothing is expanded twice."""
    try:
        done = subprocess.run([*ssh_base(directory, info), shlex.join(argv)], stdin=stdin, timeout=timeout,
                              text=True, capture_output=capture or quiet)
    except subprocess.TimeoutExpired:
        raise KitError(f"{info['name']}: `{shlex.join(argv)[:120]}` passed its {timeout:.0f}s bound") from None
    if check and done.returncode != 0:
        detail = (done.stderr or "").strip()[-2000:] if (capture or quiet) else ""
        raise KitError(f"{info['name']}: `{shlex.join(argv)[:160]}` exited {done.returncode}" +
                       (f"\n{detail}" if detail else ""))
    return done


def push(directory: Path, info: dict, sources: list[Path], dest: str) -> None:
    subprocess.run(["scp", "-q", *ssh_options(directory, info), "-P", str(info["port"]),
                    *map(str, sources), f"{USER}@127.0.0.1:{dest}"], check=True, stdin=subprocess.DEVNULL,
                   timeout=600)


def remote_script(directory: Path, info: dict, script: str, *, timeout: float, check: bool = True,
                  capture: bool = False) -> subprocess.CompletedProcess:
    """A bash script run in the VM from stdin, so the host shell never sees its words."""
    return subprocess.run([*ssh_base(directory, info), "bash", "-s"], input=script, text=True, timeout=timeout,
                          check=check, capture_output=capture)


def live(root: Path, name: str) -> tuple[Path, dict]:
    directory = vm_dir(root, name)
    info = read_info(directory)
    if (directory / "ended.json").exists() or not pid_runs(info.get("supervisor", 0), str(directory)):
        raise KitError(f"{name} is not running")
    return directory, info


# --- install, tenant ---------------------------------------------------------------------------------------------


def commit_pack(repo: Path, commit: str, out: Path) -> str:
    """The commit and its history as one pack, read from the repository without writing to it."""
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"{commit}^{{commit}}"], check=True,
                         capture_output=True, text=True).stdout.strip()
    objects = subprocess.run(["git", "-C", str(repo), "rev-list", "--objects", sha], check=True,
                             capture_output=True).stdout
    with out.open("wb") as pack:
        subprocess.run(["git", "-C", str(repo), "pack-objects", "--stdout", "-q"], input=objects, stdout=pack,
                       check=True)
    return sha


def standin_files() -> list[Path]:
    """The stand-in CLIs: each executable file in standins/, named for the CLI it stands in for."""
    return [path for path in sorted(STANDINS.iterdir()) if path.is_file() and os.access(path, os.X_OK)]


def install_script(sha: str, standins: list[str]) -> str:
    """What runs in the VM to install `sha`: packages, the stand-ins, the commit checked out, then ./install, any
    question it asks answered yes, and ./install's own exit status."""
    standins_words = " ".join(shlex.quote(name) for name in standins)
    return f"""set -euo pipefail
sudo pacman -Syu --noconfirm --needed {' '.join(PACKAGES)} >~/pacman.log 2>&1
for cli in {standins_words}; do sudo install -m 0755 ~/"$cli" /usr/local/bin/"$cli"; done
rm -rf ~/switchyard && git init -q ~/switchyard && cd ~/switchyard
git index-pack --stdin <~/release.pack >/dev/null
git update-ref refs/heads/candidate {sha}
git -c advice.detachedHead=false checkout -q {sha}
start=$EPOCHREALTIME
set +e +o pipefail
{{ yes || true; }} | sudo ./install --yes >~/install.log 2>&1; rc=${{PIPESTATUS[1]}}  # any question it asks: yes
echo "install rc=$rc in $(echo "$EPOCHREALTIME - $start" | bc)s; current -> $(readlink /opt/switchyard/current)"
exit $rc
"""


def install(root: Path, name: str, *, repo: Path, commit: str) -> str:
    directory, info = live(root, name)
    with tempfile.TemporaryDirectory(dir=directory) as scratch:
        pack = Path(scratch) / "release.pack"
        sha = commit_pack(repo, commit, pack)
        push(directory, info, [pack, HERE / "role_run.py", *standin_files()], "")
    say(f"{name}: installing {sha[:12]} (packages, then ./install)")
    script = install_script(sha, [p.name for p in standin_files()])
    done = remote_script(directory, info, script, timeout=1800, check=False, capture=True)
    sys.stdout.write(done.stdout)
    if done.returncode != 0:
        tail = ssh(directory, info, ["tail", "-40", "install.log"], timeout=30, check=False, capture=True).stdout
        raise KitError(f"{name}: install of {sha[:12]} failed\n{done.stderr}{tail}")
    return sha


def tenant_workflow(repo: Path, slug: str) -> dict:
    """The shipped example workflow, declared for this slug, every role that runs on the stand-in Codex.

    The source of the artifact's roles. The tenant is made without `--workflow-config`, as a production tenant is:
    since SYRD-860 `switchyard new` declares the workflow its plan describes, so `check` proves that default."""
    document = json.loads((repo / "examples" / "workflows" / "inspection.json").read_text())
    document["project"] = slug
    for role in document["roles"]:
        if role.get("runtime"):
            role["runtime"] = "codex"
            role.pop("model", None)
            role.pop("effort", None)
        if (role.get("target") or "").startswith("cerulean-"):
            role["target"] = slug + role["target"][len("cerulean"):]
    return document


def tenant_artifact(slug: str, workflow: dict) -> dict:
    """A design artifact, as a design session emits one, naming the workflow's own roles, all on the stand-in Codex.

    It settles every question `switchyard new` would otherwise ask, so the run needs no terminal."""
    active = [r for r in workflow["roles"] if r.get("active") and r.get("target")]
    implementers = [r["name"] for r in active if r.get("kind") == "implementer"]
    reviewers = [r["name"] for r in active if r.get("kind") == "reviewer"]
    return {
        "schema": "switchyard.project.v1",
        "design_document": f"/home/{USER}/{slug}-DESIGN.md",
        "project": {
            "slug": slug, "name": f"{slug} review tenant", "ticket_prefix": slug.upper()[:6],
            "owner_user": f"{slug}-agent", "repository": f"/srv/{slug}", "roles": implementers,
            "audit_roles": reviewers, "role_clis": {r["name"]: "codex" for r in active},
            "include_designer": False, "include_audit": bool(reviewers),
        },
    }


def tenant_script(slug: str, argv: list[str]) -> str:
    """What runs in the VM to create the tenant: `switchyard new` with no terminal, its status and its log's tail."""
    return f"""set -uo pipefail
sudo install -d -m 0755 /srv
{shlex.join(argv)} </dev/null >~/new-{slug}.log 2>&1; rc=$?
echo "switchyard new rc=$rc"; tail -25 ~/new-{slug}.log
exit $rc
"""


def tenant(root: Path, name: str, slug: str, *, repo: Path) -> None:
    if not (slug.isascii() and slug.isalnum() and slug.islower() and slug[0].isalpha()):
        raise KitError(f"a test tenant's slug is lower-case letters and digits, starting with a letter: {slug!r}")
    directory, info = live(root, name)
    with tempfile.TemporaryDirectory(dir=directory) as scratch:
        document = tenant_workflow(repo, slug)
        artifact = Path(scratch) / f"{slug}-project.json"
        artifact.write_text(json.dumps(tenant_artifact(slug, document), indent=1) + "\n")
        design = Path(scratch) / f"{slug}-DESIGN.md"
        design.write_text(f"# {slug}\n\nA disposable review tenant (tests/vm_kit).\n")
        push(directory, info, [artifact, design], "")
    say(f"{name}: switchyard new {slug} (stand-in CLIs; no sign-in to wait for)")
    # No --workflow-config: the default declaration is what a production tenant gets (SYRD-860).
    argv = ["sudo", "switchyard", "new", "--from", f"/home/{USER}/{slug}-project.json", "--yes", "--headless",
            "--layout", "viewer", "--agent-cli-policy", "require-host-wide", "--no-upstream-report"]
    script = tenant_script(slug, argv)
    done = remote_script(directory, info, script, timeout=1500, check=False, capture=True)
    sys.stdout.write(done.stdout)
    if done.returncode != 0:
        raise KitError(f"{name}: switchyard new {slug} exited {done.returncode}\n{done.stderr}")


def logs(root: Path, name: str, dest: Path) -> Path:
    directory, info = live(root, name)
    dest.mkdir(parents=True, exist_ok=True)
    script = """set -u
out=$(mktemp -d); cd ~
cp -f pacman.log install.log new-*.log "$out"/ 2>/dev/null
sudo journalctl -b --no-pager >"$out/journal.log" 2>&1
for home in /home/*-agent; do
  [ -d "$home" ] || continue; who=${home##*/}
  sudo find "$home/.local/state" -name '*.log' -size -20M -print0 2>/dev/null |
    while IFS= read -r -d '' f; do d="$out/$who${f#$home}"; mkdir -p "${d%/*}"; sudo cat "$f" >"$d"; done
done
tar -C "$out" -czf - . ; rm -rf "$out"
"""
    with (dest / "vm-logs.tgz").open("wb") as archive:
        subprocess.run([*ssh_base(directory, info), "bash", "-s"], input=script.encode(), stdout=archive,
                       check=True, timeout=600)
    subprocess.run(["tar", "-C", str(dest), "-xzf", str(dest / "vm-logs.tgz")], check=True)
    (dest / "vm-logs.tgz").unlink()
    shutil.copy(directory / "serial.log", dest / "serial.log")
    say(f"{name}: logs in {dest}")
    return dest


# --- down --------------------------------------------------------------------------------------------------------


def down(root: Path, name: str, *, wait: int = 150) -> None:
    """Power the VM off, then prove it: neither its supervisor nor its qemu runs, and the overlay is gone."""
    directory = vm_dir(root, name)
    if not directory.exists():
        raise KitError(f"no VM named {name} in {root}")
    try:
        info = read_info(directory)
    except SystemExit:  # a boot that stopped before recording anything: no process of it was ever started
        info = {"name": name}
    supervisor = int(info.get("supervisor", 0))
    if pid_runs(supervisor, str(directory)):
        # The guest's own poweroff first (ssh ends 255 when the guest drops it, so its status says nothing), then
        # the virtual power button a third of the way through the wait, then the supervisor's own stop.
        if (directory / "key").exists():
            ssh(directory, info, ["sudo", "systemctl", "poweroff"], timeout=20, check=False, quiet=True)
        started, pressed = time.monotonic(), False
        while pid_runs(supervisor, str(directory)) and time.monotonic() < started + wait:
            if not pressed and time.monotonic() > started + wait / 3:
                pressed = qmp(directory, "system_powerdown") or True
            time.sleep(0.5)
        if pid_runs(supervisor, str(directory)):
            os.kill(supervisor, signal.SIGTERM)  # it powers off, then terminates qemu
            deadline = time.monotonic() + 120
            while pid_runs(supervisor, str(directory)) and time.monotonic() < deadline:
                time.sleep(1)
        if pid_runs(supervisor, str(directory)):
            os.kill(supervisor, signal.SIGKILL)  # qemu dies with it (PR_SET_PDEATHSIG)
            time.sleep(2)
    running = survivors(directory)
    if running:
        raise KitError(f"{name}: still running as {running}; nothing removed")
    for leftover in ("overlay.qcow2", "seed.iso", "key", "key.pub", HOST_KEY, f"{HOST_KEY}.pub", "qmp.sock",
                     "qemu.pid"):
        (directory / leftover).unlink(missing_ok=True)
    if (directory / "overlay.qcow2").exists():
        raise KitError(f"{name}: {directory / 'overlay.qcow2'} is still there")
    ended = {}
    try:
        ended = json.loads((directory / "ended.json").read_text())
    except (OSError, ValueError):
        pass
    shutil.rmtree(directory)
    say(f"{name}: gone (its supervisor and qemu have ended; overlay, seed and keys removed"
        + (f"; {ended['reason']}" if ended.get("reason") else "") + ")")


def list_vms(root: Path) -> None:
    for directory in sorted((root / "vms").glob("*")):
        try:
            info = read_info(directory)
        except SystemExit:
            print(f"{directory.name}\tincomplete: never started (run `down {directory.name}` to remove it)")
            continue
        running = pid_runs(int(info.get("supervisor", 0)), str(directory))
        left = max(0, int(info["deadline"] - time.time()))
        print(f"{info['name']}\t{'running' if running else 'stopped'}\tport {info['port']}\t"
              f"{left}s left" if running else f"{info['name']}\tstopped (run `down {info['name']}` to remove it)")


# --- the whole review --------------------------------------------------------------------------------------------


def as_role(directory: Path, info: dict, project: str, role: str, words: list[str], *,
            timeout: float = 600) -> subprocess.CompletedProcess:
    """`words` typed in that role's live pane (role_run.py), its output and status captured."""
    return ssh(directory, info, ["sudo", "python3", "-I", f"/home/{USER}/role_run.py", project, role,
                                 "--timeout", str(int(timeout)), "--", *words],
               timeout=timeout + 60, check=False, capture=True)


def listener_log(slug: str) -> str:
    return f"/home/{slug}-agent/.local/state/{slug}-ticket-board-notify-listener.log"


def deliveries(directory: Path, info: dict, slug: str) -> list[str]:
    """The listener's own lines for notifications it delivered into a pane."""
    log = ssh(directory, info, ["sudo", "cat", listener_log(slug)], timeout=60, check=False, capture=True).stdout
    return [line for line in log.splitlines() if " Delivered queued notification " in line]


def delivery_probe(directory: Path, info: dict, slug: str, *, wait: float = 180) -> str:
    """As the Director: a ticket routed to main. Proven only by the listener's line delivering it to main's pane."""
    created = as_role(directory, info, slug, "director",
                      ["ticket-board-write", "create-ticket", "--title", "vm_kit delivery probe", "--body",
                       "Routed to main so the listener delivers a real notification (tests/vm_kit).",
                       "--state", "analysis"])
    try:
        ticket = json.loads(created.stdout.strip().splitlines()[-1])["id"]
    except (IndexError, ValueError, KeyError):
        raise KitError(f"{info['name']}: the Director could not create the probe ticket\n{created.stdout}"
                       f"{created.stderr}") from None
    routed = as_role(directory, info, slug, "director",
                     ["ticket-board-write", "route", ticket, "--state", "in_progress", "--assignee", "main"])
    if routed.returncode != 0:
        raise KitError(f"{info['name']}: the Director could not route {ticket} to main\n{routed.stdout}")
    wanted = f" for {ticket} to {slug}-main:0.0"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        found = [line for line in deliveries(directory, info, slug) if wanted in line]
        if found:
            return found[0]
        time.sleep(3)
    raise KitError(f"{info['name']}: no delivery of {ticket} to main's pane within {wait:.0f}s ({listener_log(slug)})")


def check(root: Path, *, repo: Path, commit: str, name: str, slug: str, image: str | None, keep_logs: Path | None,
          lifetime: int) -> int:
    """Boot, install the commit, create a tenant, then prove: an active declared workflow, `research-stage enable
    --dry-run` from the Director's pane, and a notification the listener really delivered. It always ends with
    `down` for a VM it created, however it ends -- qemu dying during boot included -- and never touches a VM of the
    same name that was there before it."""
    directory = vm_dir(root, name)
    try:
        boot(root, name, image=image, cpus=4, mem=8192, lifetime=lifetime)
    except NameTaken:
        raise  # not ours: left exactly as it was
    except BaseException:
        if directory.exists():  # ours, half booted (qemu ended, or no ssh): taken down like any other
            try:
                down(root, name)
            except BaseException as cleanup:  # said as well as the boot's own error, never instead of it
                print(f"vm_kit: {name}: and taking it down failed too: {cleanup}", file=sys.stderr, flush=True)
        raise
    try:
        sha = install(root, name, repo=repo, commit=commit)
        tenant(root, name, slug, repo=repo)
        directory, info = live(root, name)
        workflow = as_role(directory, info, slug, "director",
                           ["sh", "-c", 'curl -sf "$TICKET_BOARD_URL/api/workflow"'])
        try:
            active = json.loads(workflow.stdout)
        except ValueError:
            active = {}
        if not active.get("document") or not active.get("revision"):
            raise KitError(f"{name}: {slug} has no active workflow: {workflow.stdout[:300]}{workflow.stderr[:300]}")
        say(f"{name}: {slug}'s board runs a declared workflow (revision {active['revision']})")
        dry_run = as_role(directory, info, slug, "director",
                          ["switchyard", "research-stage", "enable", "--dry-run", "--role", "main"])
        if dry_run.returncode != 0 or '"expected_revision"' not in dry_run.stdout:
            raise KitError(f"{name}: research-stage enable --dry-run exited {dry_run.returncode}\n"
                           f"{dry_run.stdout[-2000:]}{dry_run.stderr[-1000:]}")
        say(f"{name}: research-stage enable --dry-run passes from {slug}'s Director pane")
        say(f"{name}: listener: {delivery_probe(directory, info, slug).split(' INFO ', 1)[-1][:160]}")
        if keep_logs is not None:
            logs(root, name, keep_logs)
        say(f"{name}: {sha[:12]} passed: install, tenant, active workflow, research-stage dry run, real delivery")
        return 0
    finally:
        down(root, name)


class HelpFormat(argparse.RawDescriptionHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
    """The synopsis as written, and every option's default said, where it has one."""

    def _get_help_string(self, action: argparse.Action) -> str:
        if action.default in (None, False) or action.default is argparse.SUPPRESS:
            return action.help or ""
        return super()._get_help_string(action)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vm_kit.py", description=__doc__, formatter_class=HelpFormat)
    parser.add_argument("--state-dir", help="the kit's directory (default $XDG_CACHE_HOME/switchyard-test-vm)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, text: str, **extra) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=text, description=text, formatter_class=HelpFormat, **extra)

    def commit_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", type=Path, default=REPO, help="the git repository holding the commit")
        p.add_argument("--commit", default="HEAD", help="the commit to install: any revision --repo resolves")

    command("fetch-image", "download and verify the Arch cloud image once")
    p = command("boot", "boot a fresh VM from the image")
    p.add_argument("name", help="the VM's name: 1-32 of a-z, 0-9 and '-'")
    p.add_argument("--image", help="a qcow2 base image to use instead of the verified cached one")
    p.add_argument("--cpus", type=int, default=4, help="virtual CPUs")
    p.add_argument("--mem", type=int, default=8192, help="memory, in MB")
    p.add_argument("--lifetime", type=int, default=DEFAULT_LIFETIME, help="seconds before it powers off regardless")
    p = command("install", "install a commit with the real ./install (default HEAD of --repo)")
    p.add_argument("name", help="a running VM")
    commit_options(p)
    p = command("tenant", "create a test tenant with the real switchyard new, roles on the stand-in CLI")
    p.add_argument("name", help="a running VM, with a release installed")
    p.add_argument("slug", help="the tenant's slug: lower-case letters and digits, starting with a letter")
    p.add_argument("--repo", type=Path, default=REPO, help="the checkout whose examples/workflows/inspection.json "
                                                            "the tenant's declared workflow comes from")
    p = command("run", "run one command in the VM", epilog="Everything after -- is the command.",
                usage="%(prog)s NAME [--role SLUG/ROLE | --as USER | --root] [--timeout SECONDS] -- COMMAND...")
    p.add_argument("name", help="a running VM")
    who = p.add_mutually_exclusive_group()
    who.add_argument("--as", dest="as_user", help="run as this account (sudo -u USER -H, a login shell in its home)")
    who.add_argument("--root", action="store_true", help="run as root (sudo)")
    who.add_argument("--role", metavar="SLUG/ROLE",
                     help="type it into that role's live pane: its account, directory, board socket and caller role")
    p.add_argument("--timeout", type=float, default=1800, help="seconds before the command is given up on")
    p = command("logs", "copy the install, journal, tenant and listener logs out of the VM")
    p.add_argument("name", help="a running VM")
    p.add_argument("dest", type=Path, help="the directory to copy them into")
    p = command("deliveries", "the listener's lines for notifications it delivered into a pane")
    p.add_argument("name", help="a running VM")
    p.add_argument("slug", help="the tenant")
    p = command("down", "power the VM off, then prove its supervisor and qemu gone and its overlay removed")
    p.add_argument("name", help="the VM")
    command("list", "the kit's VMs and how long each has left")
    p = command("check", "boot, install, create a tenant, check its workflow, a dry run and a delivery, power off")
    commit_options(p)
    p.add_argument("--name", help="the VM's name (default review-<pid>)")
    p.add_argument("--slug", default="vmt", help="the test tenant's slug")
    p.add_argument("--image", help="a qcow2 base image to use instead of the verified cached one")
    p.add_argument("--keep-logs", type=Path, help="copy the VM's logs into this directory before powering off")
    p.add_argument("--lifetime", type=int, default=DEFAULT_LIFETIME, help="seconds before it powers off regardless")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["_supervise"] and len(argv) == 2:  # boot's own detached supervisor: not a command for people
        if os.geteuid() == 0:
            raise KitError("refusing to run as root: the kit needs nothing of root on the host")
        return supervise(Path(argv[1]))
    # `run`'s command is everything after the first `--`, untouched by this parser.
    words = argv[argv.index("--") + 1:] if "--" in argv else []
    args = parser.parse_args(argv[:argv.index("--")] if "--" in argv else argv)
    if os.geteuid() == 0:
        raise KitError("refusing to run as root: the kit needs nothing of root on the host")
    root = state_root(args.state_dir)
    if args.command == "fetch-image":
        fetch_image(root)
    elif args.command == "boot":
        boot(root, args.name, image=args.image, cpus=args.cpus, mem=args.mem, lifetime=args.lifetime)
    elif args.command == "install":
        install(root, args.name, repo=args.repo.resolve(), commit=args.commit)
    elif args.command == "tenant":
        tenant(root, args.name, args.slug, repo=args.repo.resolve())
    elif args.command == "run":
        directory, info = live(root, args.name)
        if not words:
            raise KitError("run: give the command after --")
        if args.as_user:
            words = ["sudo", "-u", args.as_user, "-H", "bash", "-lc", 'cd ~ && exec "$@"', "run", *words]
        elif args.root:
            words = ["sudo", *words]
        elif args.role:
            project, _, role = args.role.partition("/")
            if not project or not role:
                raise KitError("--role is PROJECT/ROLE, e.g. vmt/director")
            words = ["sudo", "python3", "-I", f"/home/{USER}/role_run.py", project, role,
                     "--timeout", str(int(args.timeout)), "--", *words]
        return ssh(directory, info, words, timeout=args.timeout, check=False, stdin=None).returncode
    elif args.command == "logs":
        logs(root, args.name, args.dest.resolve())
    elif args.command == "deliveries":
        directory, info = live(root, args.name)
        found = deliveries(directory, info, args.slug)
        print("\n".join(found) if found else f"vm_kit: no delivery in {listener_log(args.slug)}")
        return 0 if found else 1
    elif args.command == "down":
        down(root, args.name)
    elif args.command == "list":
        list_vms(root)
    elif args.command == "check":
        return check(root, repo=args.repo.resolve(), commit=args.commit, name=args.name or f"review-{os.getpid()}",
                     slug=args.slug,
                     image=args.image, keep_logs=args.keep_logs.resolve() if args.keep_logs else None,
                     lifetime=args.lifetime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
