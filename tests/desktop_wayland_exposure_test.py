#!/usr/bin/env python3
"""The desktop owner's runtime directory stays 0700 while tenants keep the desktop (SYRD-687).

The scoped Wayland grant used to put named `--x` entries on the desktop owner's
XDG runtime directory. A POSIX ACL's mask shows in the group bits, so
`/run/user/1000` read 0710, and Qt refused it: "QStandardPaths: wrong
permissions on runtime directory /run/user/1000, 0710 instead of 0700". The
grant now reaches the compositor through a confined exposure under
`/var/lib/switchyard/desktop/`, and upgrade retires the legacy entries.

All of it runs as real, separate Unix identities, in a user namespace
(`unshare --map-auto --map-root-user --mount`) and a chroot whose `/` belongs
to the namespace's root. So the exposure's trust walk is the shipped default,
`("/", 0)`, not a test seam:

* deskowner (1000) runs the compositor and the real owner service each
  install starts. In a virtual machine the compositor is a real KWin
  (`kwin_wayland --virtual`, software compositing, `/dev/dri` hidden) and the
  clipboard crosses for real. Anywhere else it is `wayland_fixture`'s stand-in,
  and each clipboard check is a descriptor round trip through the same relay:
  a kwin_wayland started on the shared development host coincided with two
  hard crashes of that host (SYRD-687); tenanta (1019), tenantb (1020) and tenantc
  (1022) are tenants, stranger (1021) is nobody's;
* logind and systemd --user are stand-ins: `loginctl` answers from a fixture
  and `systemctl --user` starts and stops each unit's real ExecStart;
* the legacy state is made by main's own helper -- its install, its running
  service and its ACL entries -- and is then upgraded through the launcher's
  `configure_project_desktop`, as root;
* with KWin the clipboard crosses for real: deskowner copies with `wl-copy`,
  a tenant pastes with `wl-paste` through its relay.

Every observation is made inside the namespace and checked here, by name.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_boundary  # noqa: E402

live_boundary.isolate()  # no live home, pane store or runtime directory, here or in a child

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "db7d6018bb7f6b87b2d0128b578c05304cbd0766"  # main before SYRD-687: the legacy runtime-directory grant
OWNER, TENANT_A, TENANT_B, STRANGER, TENANT_C = ("deskowner", 1000), ("tenanta", 1019), ("tenantb", 1020), \
    ("stranger", 1021), ("tenantc", 1022)
RUNTIME = "/run/user/1000"
EXPOSURES = "/var/lib/switchyard/desktop"

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("ok   " if condition else "FAIL ") + message)
    if not condition:
        failures.append(message)
        raise AssertionError(message)


# ---------------------------------------------------------------- inside the namespace

LOGINCTL = r"""#!/usr/bin/python3
import os, pwd, sys
a = sys.argv[1:]
active = not os.path.exists('/run/fake-logind/inactive')
if a[:1] == ['show-user']:
    user = pwd.getpwnam(a[1])
    key = a[a.index('-p') + 1]
    if key == 'RuntimePath':
        print(f'/run/user/{user.pw_uid}')
    elif key == 'Sessions':
        print('1' if user.pw_uid == 1000 else '')
elif a[:1] == ['show-session']:
    print(f"Type=wayland\nActive={'yes' if active else 'no'}\nUser=1000\nName=deskowner")
elif a[:1] == ['list-sessions']:
    print('1 1000 deskowner seat0')
else:
    sys.exit(2)
"""

SYSTEMCTL = r"""#!/usr/bin/python3
# systemctl --user, for one account: each unit's real ExecStart, as that account.
import os, pwd, shlex, signal, subprocess, sys, time
a = sys.argv[1:]
assert a[0] == '--user', a
me = pwd.getpwuid(os.getuid())
state = f'/run/fake-systemd/{me.pw_uid}'
os.makedirs(state, exist_ok=True)
with open('/run/fake-systemd/calls', 'a') as log:
    log.write(f'{me.pw_name} {" ".join(a[1:])}\n')
command = a[1]
unit = a[-1]
if os.path.exists(f'/run/fake-systemd/fail-{command}'):
    sys.exit(f'fixture: systemctl {command} refused')


def running():
    try:
        pid = int(open(f'{state}/{unit}.pid').read())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def start():
    text = open(f'{me.pw_dir}/.config/systemd/user/{unit}').read()
    line = next(l for l in text.splitlines() if l.startswith('ExecStart='))[len('ExecStart='):]
    argv = [part.replace('%%', '%') for part in shlex.split(line)]
    out = open(f'{state}/{unit}.log', 'a')
    env = {'PATH': os.environ['PATH'], 'HOME': me.pw_dir, 'XDG_RUNTIME_DIR': f'/run/user/{me.pw_uid}', 'LANG': 'C.UTF-8'}
    process = subprocess.Popen(argv, stdout=out, stderr=out, stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    open(f'{state}/{unit}.pid', 'w').write(str(process.pid))


def stop():
    pid = running()
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.05)
    if os.path.exists(f'{state}/{unit}.pid'):
        os.unlink(f'{state}/{unit}.pid')


if command in ('daemon-reload',):
    pass
elif command in ('enable', 'start'):
    if not running():
        start()
elif command == 'restart':
    stop()
    start()
elif command in ('disable', 'stop'):
    stop()
elif command == 'is-active':
    print('active' if running() else 'inactive')
    sys.exit(0 if running() else 3)
else:
    sys.exit(f'fixture: systemctl {command} is not stood in for')
"""

RUNUSER = r"""#!/usr/bin/python3
# runuser -u NAME -- COMMAND...: the same switch, through setpriv (the run's boundary refuses the real runuser).
import os, pwd, sys
a = sys.argv[1:]
assert a[0] == '-u' and a[2] == '--', a
user = pwd.getpwnam(a[1])
os.environ.update({'HOME': user.pw_dir, 'USER': user.pw_name, 'LOGNAME': user.pw_name})
os.execvp('setpriv', ['setpriv', f'--reuid={user.pw_uid}', f'--regid={user.pw_gid}', '--clear-groups', *a[3:]])
"""

PATH = "/stubs:/usr/bin:/bin"


def sh(*argv: str, check_rc: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(list(argv), capture_output=True, text=True)
    if check_rc and result.returncode:
        raise RuntimeError(f"{argv}: {result.stderr}")
    return result


def as_user(user: tuple[str, int], argv: list[str], *, env: dict[str, str] | None = None, timeout: float = 60,
            background: bool = False):
    name, uid = user
    full = {"PATH": PATH, "LANG": "C.UTF-8", "HOME": f"/home/{name}", "USER": name, "LOGNAME": name,
            "XDG_RUNTIME_DIR": f"/run/user/{uid}", "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    argv = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", *argv]
    if background:
        return subprocess.Popen(argv, env=full, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    return subprocess.run(argv, env=full, capture_output=True, text=True, timeout=timeout)


def py(user, code: str, *args: str, env=None, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return as_user(user, [sys.executable, "-c", "import sys; sys.path[:0] = ['/tree']\n" + code, *args],
                   env=env, timeout=timeout)


def enter_root(base: Path) -> None:
    """A `/` the namespace's root owns, with the host's /usr, an overlay of its /etc, and this suite's accounts."""
    root = base / "root"
    root.mkdir(mode=0o755)
    for top in ("usr", "dev", "proc", "sys"):
        (root / top).mkdir()
        sh("mount", "--rbind", f"/{top}", str(root / top))
    # /etc is the host's under an overlay: what the suite writes there -- its accounts,
    # its project registry -- is the namespace's alone, whatever the host has.
    # Its registry directory is opaque: none of the host's projects show through.
    for part in ("etc", "etc-upper", "etc-work"):
        (root / part if part == "etc" else base / part).mkdir()
    registry = base / "etc-upper" / "switchyard" / "projects"
    registry.mkdir(parents=True)
    os.setxattr(registry, "user.overlay.opaque", b"y")
    sh("mount", "-t", "overlay", "overlay", "-o",
       f"userxattr,lowerdir=/etc,upperdir={base / 'etc-upper'},workdir={base / 'etc-work'}", str(root / "etc"))
    for link in ("bin", "sbin", "lib", "lib64"):
        if os.path.islink(f"/{link}"):
            os.symlink(os.readlink(f"/{link}"), root / link)
    if (root / "dev/dri").is_dir():
        sh("mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", str(root / "dev/dri"))  # no GPU in reach
    (root / "root").mkdir()
    sh("mount", "--bind", "/root", str(root / "root"))  # the run's sentinel watches root's home
    # The host's /tmp: the run's boundary keeps its preload library there.
    (root / "tmp").mkdir()
    sh("mount", "--rbind", "/tmp", str(root / "tmp"))
    for top, mode in (("run", 0o755), ("home", 0o755)):
        (root / top).mkdir()
        sh("mount", "-t", "tmpfs", "-o", f"mode={mode:o}", "tmpfs", str(root / top))
    (root / "var/lib/switchyard").mkdir(parents=True, mode=0o755)
    users = [OWNER, TENANT_A, TENANT_B, STRANGER, TENANT_C]
    (base / "passwd").write_text("root:x:0:0::/root:/bin/sh\n" + "".join(
        f"{name}:x:{uid}:{uid}::/home/{name}:/bin/sh\n" for name, uid in users))
    (base / "group").write_text("root:x:0:\n" + "".join(f"{name}:x:{uid}:\n" for name, uid in users))
    for name in ("passwd", "group"):
        sh("mount", "--bind", str(base / name), str(root / "etc" / name))
    for part in ("tree", "stubs", "legacy"):
        (root / part).mkdir()
        sh("mount", "--bind", str(base / part), str(root / part))
    os.chroot(root)
    os.chdir("/")
    os.environ["PATH"] = PATH  # root's own calls meet the same logind and systemd stand-ins
    for directory in ("/run/user", "/run/fake-systemd", "/run/fake-logind"):
        os.makedirs(directory, mode=0o755)
    os.chmod("/run/fake-systemd", 0o1777)
    for name, uid in users:
        for path in (f"/run/user/{uid}", f"/home/{name}"):
            os.mkdir(path, 0o700)
            os.chown(path, uid, uid)


#: `kwin` only in a virtual machine: a kwin_wayland started on the shared
#: development host coincided with two hard crashes of it. Anywhere else the
#: compositor is `wayland_fixture`'s stand-in, run as the desktop owner, and the
#: clipboard checks become descriptor round trips through the same relay.
COMPOSITOR = os.environ.get("SYRD687_COMPOSITOR", "fake")
LAST_COPY = [""]


def start_compositor(socket_name: str = "wayland-0"):
    if COMPOSITOR == "kwin":
        # Software compositing only, and no GPU in reach.
        argv = ["env", "KWIN_COMPOSE=Q", "LIBGL_ALWAYS_SOFTWARE=1", "dbus-run-session", "--", "kwin_wayland", "--virtual",
                "--no-lockscreen", "--no-global-shortcuts", "--socket", socket_name, "--width", "320", "--height", "240"]
    else:
        argv = [sys.executable, "-c", "import sys, time; sys.path.insert(0, '/tree/tests'); "
                "from wayland_fixture import FakeCompositor; FakeCompositor(sys.argv[1]); time.sleep(10 ** 6)",
                f"{RUNTIME}/{socket_name}"]
    process = as_user(OWNER, argv, background=True)
    for _ in range(150):
        if os.path.exists(f"{RUNTIME}/{socket_name}"):
            time.sleep(0.5)
            return process
        time.sleep(0.1)
    raise RuntimeError(f"the compositor did not bind {socket_name}")


def stop_compositor(process) -> None:
    os.killpg(process.pid, 15)
    process.wait(timeout=10)
    for _ in range(50):
        if not any(Path(RUNTIME).glob("wayland-*")):
            return
        time.sleep(0.1)
    for leftover in Path(RUNTIME).glob("wayland-*"):
        leftover.unlink()


def acl(path: str) -> list[str]:
    return sh("getfacl", "-cpn", path, check_rc=False).stdout.split()


def mode(path: str) -> str:
    return oct(os.stat(path).st_mode & 0o777)


QT_CHECK = r"""
import os, stat, sys
# QStandardPaths::writableLocation(RuntimeLocation) on Unix: the directory must be
# this user's, and its permissions exactly ReadOwner|WriteOwner|ExeOwner.
info = os.stat(sys.argv[1])
ok = stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o777 == 0o700
print('valid' if ok else f'QStandardPaths: wrong permissions on runtime directory {sys.argv[1]}, {stat.S_IMODE(info.st_mode):04o} instead of 0700')
"""


def qt(path: str = RUNTIME) -> str:
    return py(OWNER, QT_CHECK, path).stdout.strip()


def policy_for(project: str, tenant: tuple[str, int]) -> dict:
    from scripts import desktop_access
    return desktop_access.generated_policy(project=project, tenant=tenant[0], gui_user=OWNER[0], approved_by=OWNER[0],
                                           approved_at="2026-10-26T00:00:00Z")


def install_legacy(project: str, tenant: tuple[str, int]) -> subprocess.CompletedProcess[str]:
    """main's own install, as the owner: its helper copy, unit, running service and ACL entries."""
    policy = policy_for(project, tenant)
    payload = {"policy": policy, "helper": Path("/legacy/desktop_access.py").read_text(), "python": sys.executable}
    return as_user(OWNER, [sys.executable, "/legacy/desktop_access.py", "install-owner", json.dumps(payload)])


def config_path(project: str, tenant: tuple[str, int]) -> Path:
    """Where `switchyard new` leaves a tenant's configuration: its project's `.switchyard/provision/`."""
    return Path(f"/home/{tenant[0]}/Projects/{project}/.switchyard/provision/{project}.json")


def write_config(project: str, tenant: tuple[str, int], policy: dict | None) -> Path:
    path = config_path(project, tenant)
    for directory in reversed(list(path.parents)[:4]):  # Projects, the project, .switchyard, provision
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chown(directory, tenant[1], tenant[1])
    raw = {"project": project, "run_as_user": tenant[0], "layout": "layout.json",
           "roles": [{"role": "main", "slot": 0, "cli": ["codex"]}]}
    if policy is not None:
        raw["desktop_access"] = policy
    path.write_text(json.dumps(raw))
    os.chown(path, tenant[1], tenant[1])
    os.chmod(path, 0o600)
    Path(f"/etc/switchyard/projects/{project}.json").write_text(json.dumps({"slug": project, "config_path": str(path)}))
    return path


def configure(project: str, tenant: tuple[str, int], *, policy_path: str | None = None) -> tuple[int, str]:
    """`switchyard upgrade`'s desktop step, as root: the launcher's configure_project_desktop, verbatim."""
    import contextlib
    import io
    from scripts import team_launcher as launcher

    path = config_path(project, tenant)
    config = launcher.load_project_config(project, path)

    def runner(args, **kwargs):
        args = list(args)
        if args[:2] == ["sudo", "-u"]:
            uid = {name: uid for name, uid in (OWNER, TENANT_A, TENANT_B, STRANGER, TENANT_C)}[args[2]]
            args = ["runuser", "-u", args[2], "--", "env", f"PATH={PATH}", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                    *args[3:]]
        return subprocess.run(args, **kwargs)

    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            launcher.configure_project_desktop(config, config_path=path,
                                               policy_path=Path(policy_path) if policy_path else None,
                                               helper=Path("/tree/scripts/desktop_access.py"), runner=runner)
        return 0, out.getvalue()
    except SystemExit as exc:
        return 1, out.getvalue() + str(exc)


def verify_as(tenant, project: str) -> tuple[int, dict | str]:
    policy = policy_for(project, tenant)
    result = as_user(tenant, [sys.executable, "/tree/scripts/desktop_access.py", "verify", json.dumps(policy)])
    if result.returncode:
        return result.returncode, result.stderr.strip()
    return 0, json.loads(result.stdout)


def paste_as(tenant, display: str) -> str:
    if COMPOSITOR != "kwin":
        # The stand-in has no clipboard: the last copied text crosses as a descriptor and back.
        result = py(tenant, "import sys; sys.path.insert(0, '/tree/tests')\nfrom wayland_fixture import echo_through\n"
                            "print(echo_through(sys.argv[1], [sys.argv[2].encode()])[0].decode(), end='')",
                    display, LAST_COPY[0])
        return result.stdout if result.returncode == 0 else f"rc={result.returncode} {result.stderr.strip()[-200:]}"
    result = as_user(tenant, ["timeout", "10", "wl-paste", "--no-newline"], env={"WAYLAND_DISPLAY": display})
    return result.stdout if result.returncode == 0 else f"rc={result.returncode} {result.stderr.strip()[-200:]}"


def copy_as_owner(text: str) -> None:
    """wl-copy forks a server that holds the selection, so its output is not waited on."""
    LAST_COPY[0] = text
    if COMPOSITOR != "kwin":
        return
    env = {"PATH": PATH, "LANG": "C.UTF-8", "HOME": "/home/deskowner", "XDG_RUNTIME_DIR": RUNTIME, "WAYLAND_DISPLAY": "wayland-0"}
    code = subprocess.run(["setpriv", "--reuid=1000", "--regid=1000", "--clear-groups", "wl-copy", text], env=env,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20).returncode
    if code:
        raise RuntimeError(f"wl-copy exited {code}")
    time.sleep(0.3)


def reach(user, path: str) -> str:
    code = ("import os, sys\n"
            "try:\n    os.listdir(sys.argv[1]); print('listed')\n"
            "except OSError as e:\n"
            "    try:\n        os.stat(sys.argv[1]); print('stat', e.errno)\n"
            "    except OSError as f:\n        print('refused', f.errno)\n")
    return py(user, code, path).stdout.strip()


def roundtrip_as(user, display: str) -> str:
    code = ("from scripts import desktop_access as d; import sys\n"
            "try:\n    d.wayland_roundtrip(sys.argv[1]); print('answered')\n"
            "except Exception as e:\n    print(type(e).__name__, getattr(e, 'errno', ''), str(e)[:120])\n")
    return py(user, code, display).stdout.strip()


def service_pid(project: str, tenant) -> int | None:
    unit = f"switchyard-desktop-{OWNER[1]}-{tenant[1]}-{project}.service"
    try:
        pid = int(Path(f"/run/fake-systemd/{OWNER[1]}/{unit}.pid").read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def wait_for(predicate, seconds: float = 12.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def receipt(project: str, tenant) -> dict:
    try:
        return json.loads(Path(f"{EXPOSURES}/{OWNER[1]}-{tenant[1]}-{project}/ready.json").read_text())
    except (OSError, ValueError):
        return {}


def namespace(base: Path, r: dict) -> dict:
    enter_root(base)
    sys.path[:0] = ["/tree"]
    compositor = start_compositor()

    # ---- legacy state, made by main's own helper -----------------------------------
    r["qt_fresh"] = qt()
    for project, tenant in (("alpha", TENANT_A), ("gamma", TENANT_A), ("beta", TENANT_B), ("delta", TENANT_C)):
        result = install_legacy(project, tenant)
        r[f"legacy_install_{project}"] = [result.returncode, result.stderr[-300:]]
        if project != "delta":
            write_config(project, tenant, policy_for(project, tenant))
    r["legacy_mode"] = mode(RUNTIME)
    r["legacy_acl"] = acl(RUNTIME)
    r["legacy_qt"] = qt()
    copy_as_owner("legacy-clip")
    r["legacy_paste_a"] = paste_as(TENANT_A, f"{RUNTIME}/wayland-0")

    # ---- the upgrade, as root: alpha first; gamma (same tenant) and beta still legacy
    r["alpha_upgrade"] = configure("alpha", TENANT_A)
    leaf_a = f"{EXPOSURES}/{OWNER[1]}-{TENANT_A[1]}-alpha"
    r["alpha_leaf"] = [mode(leaf_a), os.stat(leaf_a).st_uid, acl(leaf_a)] if os.path.isdir(leaf_a) else None
    r["exposures_root"] = [oct(os.stat(EXPOSURES).st_mode & 0o7777), os.stat(EXPOSURES).st_uid]
    r["alpha_receipt"] = receipt("alpha", TENANT_A)
    r["alpha_verify"] = verify_as(TENANT_A, "alpha")
    r["after_alpha_acl"] = acl(RUNTIME)
    copy_as_owner("first-clip")
    r["alpha_paste"] = paste_as(TENANT_A, f"{leaf_a}/wayland-0")
    r["alpha_legacy_receipt_gone"] = not os.path.exists(f"{RUNTIME}/switchyard-desktop-1000-1019-alpha.json")

    # gamma is not upgraded yet, but this release's verify is what its launch runs:
    # it still reaches the desktop through its own legacy receipt and entries.
    r["gamma_legacy_verify"] = verify_as(TENANT_A, "gamma")
    # gamma: same tenant; its upgrade takes the shared entry off
    r["gamma_upgrade"] = configure("gamma", TENANT_A)
    r["after_gamma_acl"] = acl(RUNTIME)

    # ---- beta's upgrade with no compositor is refused before anything changes ------
    stop_compositor(compositor)
    legacy_helper = Path(f"/home/{OWNER[0]}/.local/lib/switchyard/desktop-access/switchyard-desktop-1000-1020-beta.py")
    beta_config = config_path("beta", TENANT_B).read_bytes()
    leaf_b = f"{EXPOSURES}/{OWNER[1]}-{TENANT_B[1]}-beta"
    before_acl = acl(RUNTIME)
    r["beta_no_compositor"] = configure("beta", TENANT_B)
    r["beta_no_compositor_state"] = {
        "helper_is_legacy": legacy_helper.read_text() == Path("/legacy/desktop_access.py").read_text(),
        "runtime_unchanged": acl(RUNTIME) == before_acl, "leaf": os.path.exists(leaf_b),
        "config_unchanged": config_path("beta", TENANT_B).read_bytes() == beta_config,
    }
    compositor = start_compositor()

    # ---- a cutover that cannot complete: the tenant's own verify fails after install
    os.rename("/run/user/1020", "/run/user/1020.away")  # tenantb's user manager is not running
    r["beta_failed_upgrade"] = configure("beta", TENANT_B)
    os.rename("/run/user/1020.away", "/run/user/1020")
    r["beta_after_failure"] = {
        "helper_is_legacy": legacy_helper.read_text() == Path("/legacy/desktop_access.py").read_text(),
        "backup_left": legacy_helper.with_name(legacy_helper.name + ".previous").exists(),
        "service_running": service_pid("beta", TENANT_B) is not None,
        "runtime_entry": [line for line in acl(RUNTIME) if line.startswith("user:1020:")],
        "leaf_acl": acl(leaf_b) if os.path.isdir(leaf_b) else None,
        "leaf_entries": sorted(os.listdir(leaf_b)) if os.path.isdir(leaf_b) else None,
        "config_unchanged": config_path("beta", TENANT_B).read_bytes() == beta_config,
    }
    r["beta_upgrade"] = configure("beta", TENANT_B)
    r["after_beta_mode"] = mode(RUNTIME)
    r["after_beta_acl"] = acl(RUNTIME)
    r["after_beta_qt"] = qt()

    # ---- two tenants at once, each only its own ------------------------------------
    copy_as_owner("shared-clip")
    r["concurrent_paste"] = [paste_as(TENANT_A, f"{leaf_a}/wayland-0"), paste_as(TENANT_B, f"{leaf_b}/wayland-0")]
    r["cross_reach"] = {
        "a_to_b": reach(TENANT_A, f"{leaf_b}/wayland-0"), "b_to_a": reach(TENANT_B, f"{leaf_a}/wayland-0"),
        "stranger_to_a": reach(STRANGER, f"{leaf_a}/wayland-0"), "stranger_to_receipt": reach(STRANGER, f"{leaf_a}/ready.json"),
        "a_leaf_listing": reach(TENANT_A, leaf_a),
        "a_runtime": reach(TENANT_A, RUNTIME), "a_bus": reach(TENANT_A, f"{RUNTIME}/bus"),
        "a_compositor": reach(TENANT_A, f"{RUNTIME}/wayland-0"),
        "b_paste_dir": reach(TENANT_B, f"{leaf_b}/paste"),
    }
    # The peer check, with the directory opened to the stranger on purpose.
    sh("setfacl", "-m", "u:1021:--x", leaf_a)
    r["stranger_relay"] = roundtrip_as(STRANGER, f"{leaf_a}/wayland-0")
    r["owner_relay"] = roundtrip_as(OWNER, f"{leaf_a}/wayland-0")
    sh("setfacl", "-x", "u:1021", leaf_a)
    r["refusal_logged"] = Path("/run/fake-systemd/1000/switchyard-desktop-1000-1019-alpha.service.log").read_text()[-600:]

    # ---- the compositor moves to another socket name -------------------------------
    holder = as_user(TENANT_A, [sys.executable, "-c",
                                "import socket,sys,time; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); "
                                "time.sleep(60)", f"{leaf_a}/wayland-0"], background=True)
    stop_compositor(compositor)
    r["pending_receipt"] = wait_for(lambda: receipt("alpha", TENANT_A).get("state") == "pending") and receipt("alpha", TENANT_A)
    r["verify_while_absent"] = verify_as(TENANT_A, "alpha")
    compositor = start_compositor("wayland-1")
    r["renamed_receipt"] = wait_for(lambda: receipt("alpha", TENANT_A).get("upstream") == "wayland-1") \
        and receipt("alpha", TENANT_A)
    r["renamed_roundtrip"] = roundtrip_as(TENANT_A, f"{leaf_a}/wayland-0")
    r["renamed_mode"] = mode(RUNTIME)
    holder.kill()

    # ---- logout and a new login: a fresh runtime, the services started again -------
    stop_compositor(compositor)
    for project, tenant in (("alpha", TENANT_A), ("gamma", TENANT_A), ("beta", TENANT_B), ("delta", TENANT_C)):
        as_user(OWNER, ["systemctl", "--user", "stop", f"switchyard-desktop-1000-{tenant[1]}-{project}.service"])
    shutil.rmtree(RUNTIME)
    os.mkdir(RUNTIME, 0o700)
    os.chown(RUNTIME, 1000, 1000)
    os.makedirs("/run/fake-systemd/1000", exist_ok=True)
    for project, tenant in (("alpha", TENANT_A), ("gamma", TENANT_A), ("beta", TENANT_B), ("delta", TENANT_C)):
        as_user(OWNER, ["systemctl", "--user", "start", f"switchyard-desktop-1000-{tenant[1]}-{project}.service"])
    compositor = start_compositor("wayland-0")
    r["relogin_ready"] = wait_for(lambda: receipt("alpha", TENANT_A).get("upstream") == "wayland-0"
                                  and receipt("beta", TENANT_B).get("upstream") == "wayland-0")
    r["relogin_acl"] = acl(RUNTIME)
    r["relogin_verify"] = [verify_as(TENANT_A, "alpha")[0], verify_as(TENANT_B, "beta")[0]]

    # ---- the exposure root is root's to make and keep ------------------------------
    os.chown(EXPOSURES, 1019, 1019)  # a tenant's, as if root had been tricked into making it so
    r["tenant_owned_root"] = verify_as(TENANT_A, "alpha")
    os.chown(EXPOSURES, 0, 0)
    made = py(OWNER, "import pwd\nfrom scripts import desktop_access as d\n"
                     "try:\n    d.exposure_dir(pwd.getpwnam('deskowner'), 'switchyard-desktop-1000-1019-omega', create=True)\n"
                     "    print('made')\n"
                     "except d.ExposureMissing as e:\n    print('missing:', e)\n"
                     "except Exception as e:\n    print(type(e).__name__, e)\n")
    r["owner_cannot_make"] = [made.stdout.strip(), os.path.exists(f"{EXPOSURES}/1000-1019-omega")]

    # ---- the orphan legacy grant: repair, as the desktop owner -------------------------
    r["orphan_acl"] = [line for line in acl(RUNTIME) if line.startswith("user:")]
    report = as_user(OWNER, [sys.executable, "/tree/scripts/desktop_access.py", "repair"])
    r["repair_report"] = [report.returncode, report.stdout]
    applied = as_user(OWNER, [sys.executable, "/tree/scripts/desktop_access.py", "repair", "--apply"])
    r["repair_apply"] = [applied.returncode, applied.stdout, applied.stderr[-300:]]
    r["after_repair_mode"] = mode(RUNTIME)
    r["after_repair_acl"] = acl(RUNTIME)
    r["after_repair_qt"] = qt()
    r["delta_unit_gone"] = not Path("/home/deskowner/.config/systemd/user/switchyard-desktop-1000-1022-delta.service").exists()

    # ---- revocation: alpha goes headless while a tenant connection is open ---------
    holder = subprocess.Popen(["setpriv", "--reuid=1019", "--regid=1019", "--clear-groups", sys.executable, "-c",
                               "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); "
                               "s.settimeout(20); print('open', flush=True); print('eof' if s.recv(1)==b'' else 'data', flush=True)",
                               f"{leaf_a}/wayland-0"], stdout=subprocess.PIPE, text=True, env={"PATH": PATH})
    opened = holder.stdout.readline().strip()
    r["revoke"] = configure("alpha", TENANT_A, policy_path="headless")
    try:
        closed = holder.communicate(timeout=10)[0].strip()
    except subprocess.TimeoutExpired:
        holder.kill()
        closed = "still open"
    r["revoked_connection"] = [opened, closed]
    r["revoked_state"] = {
        "leaf": os.path.exists(leaf_a), "service": service_pid("alpha", TENANT_A) is not None,
        "unit": Path("/home/deskowner/.config/systemd/user/switchyard-desktop-1000-1019-alpha.service").exists(),
        "gamma_still": receipt("gamma", TENANT_A).get("state"), "beta_still": receipt("beta", TENANT_B).get("state"),
    }
    r["revoked_verify"] = verify_as(TENANT_A, "alpha")
    copy_as_owner("after-revoke")
    r["beta_after_revoke_paste"] = paste_as(TENANT_B, f"{leaf_b}/wayland-0")

    # ---- teardown, as root, of beta -------------------------------------------------
    down = subprocess.run([sys.executable, "/tree/scripts/desktop_access.py", "teardown", "beta", "tenantb"],
                          capture_output=True, text=True, env={"PATH": PATH, "LANG": "C.UTF-8"})
    r["teardown"] = [down.returncode, down.stdout.strip(), down.stderr[-300:]]
    r["teardown_state"] = {"leaf": os.path.exists(leaf_b), "service": service_pid("beta", TENANT_B) is not None,
                           "policy": Path("/home/deskowner/.config/switchyard/desktop-access/switchyard-desktop-1000-1020-beta.json").exists()}
    again = subprocess.run([sys.executable, "/tree/scripts/desktop_access.py", "teardown", "beta", "tenantb"],
                           capture_output=True, text=True, env={"PATH": PATH, "LANG": "C.UTF-8"})
    r["teardown_again"] = [again.returncode, again.stdout.strip()]
    r["final_mode"] = mode(RUNTIME)
    r["final_qt"] = qt()

    # ---- an operator's own entry survives, and is not widened ----------------------
    # A fresh login, an entry somebody else made, and a legacy grant beside it.
    stop_compositor(compositor)
    for unit in Path("/run/fake-systemd/1000").glob("*.pid"):
        as_user(OWNER, ["systemctl", "--user", "stop", unit.name[:-len(".pid")]])
    shutil.rmtree(RUNTIME)
    os.mkdir(RUNTIME, 0o700)
    os.chown(RUNTIME, 1000, 1000)
    compositor = start_compositor()
    as_user(OWNER, ["setfacl", "-m", "u:1021:r-x,m::--x", RUNTIME])
    r["unrelated_before"] = acl(RUNTIME)
    r["epsilon_legacy"] = install_legacy("epsilon", TENANT_B).returncode
    write_config("epsilon", TENANT_B, policy_for("epsilon", TENANT_B))
    r["epsilon_legacy_acl"] = acl(RUNTIME)
    r["epsilon_upgrade"] = configure("epsilon", TENANT_B)
    r["unrelated_after"] = acl(RUNTIME)
    r["unrelated_mode"] = mode(RUNTIME)
    r["unrelated_qt"] = qt()

    # ---- an install that cannot start its service leaves nothing -------------------
    write_config("zeta", TENANT_C, policy_for("zeta", TENANT_C))
    Path("/run/fake-systemd/fail-enable").write_text("")
    r["zeta_failed"] = configure("zeta", TENANT_C)
    os.unlink("/run/fake-systemd/fail-enable")
    leaf_z = f"{EXPOSURES}/{OWNER[1]}-{TENANT_C[1]}-zeta"
    r["zeta_state"] = {"leaf_acl": acl(leaf_z) if os.path.isdir(leaf_z) else None,
                       "policy": Path("/home/deskowner/.config/switchyard/desktop-access/switchyard-desktop-1000-1022-zeta.json").exists(),
                       "runtime": [line for line in acl(RUNTIME) if line.startswith("user:1022")]}

    for unit in Path("/run/fake-systemd/1000").glob("*.pid"):
        as_user(OWNER, ["systemctl", "--user", "stop", unit.name[:-len(".pid")]])
    stop_compositor(compositor)
    return r


# ---------------------------------------------------------------- the parent


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--namespace":
        partial: dict = {}
        try:
            namespace(Path(sys.argv[2]), partial)
        except Exception:  # reported with what was observed before it
            import traceback
            partial["__error__"] = traceback.format_exc()
        print(json.dumps(partial))
        return 0
    virt = subprocess.run(["systemd-detect-virt", "--vm"], capture_output=True, text=True).stdout.strip()
    compositor = "kwin" if virt not in ("", "none") and shutil.which("kwin_wayland") else "fake"
    print(f"compositor: {compositor}" + ("" if compositor == "kwin" else
          f" (systemd-detect-virt --vm: {virt or 'unknown'}; a real compositor runs only in a virtual machine)"))
    tools = ["setfacl", "getfacl", "unshare", "setpriv"] + (["kwin_wayland", "dbus-run-session", "wl-copy", "wl-paste"]
                                                           if compositor == "kwin" else [])
    for tool in tools:
        assert shutil.which(tool), f"{tool} is required; this suite does not skip"
    base = Path(tempfile.mkdtemp(prefix="syrd687."))
    try:
        base.chmod(0o755)
        for part in ("scripts", "tests"):
            shutil.copytree(ROOT / part, base / "tree" / part, ignore=shutil.ignore_patterns("__pycache__"))
        (base / "legacy").mkdir()
        legacy = subprocess.run(["git", "-C", str(ROOT), "show", f"{BEFORE}:scripts/desktop_access.py"],
                                check=True, capture_output=True).stdout
        (base / "legacy" / "desktop_access.py").write_bytes(legacy)
        (base / "stubs").mkdir()
        for name, text in (("loginctl", LOGINCTL), ("systemctl", SYSTEMCTL), ("runuser", RUNUSER)):
            (base / "stubs" / name).write_text(text)
            (base / "stubs" / name).chmod(0o755)
        subprocess.run(["chmod", "-R", "a+rX", str(base)], check=True)
        child = subprocess.run(
            ["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private",
             sys.executable, str(Path(__file__).resolve()), "--namespace", str(base)],
            capture_output=True, text=True, timeout=540,
            env={**os.environ, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "SYRD687_COMPOSITOR": compositor},
        )
        if child.returncode:
            print(child.stderr[-6000:])
        assert child.returncode == 0, "the namespace run failed"
        r = json.loads(child.stdout.strip().splitlines()[-1])
        if "__error__" in r:
            print(json.dumps({k: v for k, v in r.items() if k != "__error__"}, indent=1)[-8000:])
            print(r["__error__"])
    finally:
        subprocess.run(["unshare", "--map-auto", "--map-root-user", sys.executable, "-c",
                        "import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)", str(base)],
                       capture_output=True)
        shutil.rmtree(base, ignore_errors=True)
    return judge(r)


def judge(r: dict) -> int:
    leaf_a, leaf_b = f"{EXPOSURES}/1000-1019-alpha", f"{EXPOSURES}/1000-1020-beta"
    check(r["qt_fresh"] == "valid", f"a fresh login's runtime passes the Qt check: {r['qt_fresh']}")

    # The legacy grant really is the defect.
    check(all(r[f"legacy_install_{p}"][0] == 0 for p in ("alpha", "gamma", "beta", "delta")),
          f"main's helper installs its legacy grants: {[r[f'legacy_install_{p}'] for p in ('alpha', 'gamma', 'beta', 'delta')]}")
    check(r["legacy_mode"] == "0o710" and "mask::--x" in r["legacy_acl"]
          and all(f"user:{uid}:--x" in r["legacy_acl"] for uid in (1019, 1020, 1022)),
          f"legacy: the runtime reads 0710, its mask --x, one entry per tenant: {r['legacy_mode']} {r['legacy_acl']}")
    check(r["legacy_qt"].startswith("QStandardPaths: wrong permissions on runtime directory /run/user/1000, 0710"),
          f"legacy: the Qt check fails as KDE reported it: {r['legacy_qt']}")
    check(r["legacy_paste_a"] == "legacy-clip", f"legacy: the tenant pasted through the runtime grant: {r['legacy_paste_a']}")

    # The upgrade of one grant.
    code, said = r["alpha_upgrade"]
    check(code == 0, f"alpha's upgrade succeeds: {said}")
    check(r["exposures_root"] == ["0o755", 0], f"the exposure root is root's, 0755: {r['exposures_root']}")
    leaf = r["alpha_leaf"]
    check(leaf is not None and leaf[1] == 1000 and leaf[0] == "0o710" and "user:1019:--x" in leaf[2]
          and "group::---" in leaf[2] and "other::---" in leaf[2],
          f"alpha's exposure: the owner's, traversable by its tenant alone: {leaf}")
    check(r["alpha_receipt"].get("state") == "ready" and r["alpha_receipt"].get("upstream") == "wayland-0"
          and r["alpha_receipt"].get("socket") == f"{leaf_a}/wayland-0",
          f"alpha's receipt is ready and names its relay: {r['alpha_receipt']}")
    code, env = r["alpha_verify"]
    check(code == 0 and env.get("WAYLAND_DISPLAY") == f"{leaf_a}/wayland-0" and env.get("XDG_RUNTIME_DIR") == "/run/user/1019"
          and env.get("DBUS_SESSION_BUS_ADDRESS") == "unix:path=/run/user/1019/bus",
          f"the tenant's verify gives the relay, its own runtime and its own bus: {env}")
    check(r["alpha_paste"] == "first-clip", f"the clipboard crosses through the relay: {r['alpha_paste']}")
    check("user:1019:--x" in r["after_alpha_acl"], f"gamma, the same tenant still on the legacy helper, keeps the shared entry: {r['after_alpha_acl']}")
    check("kept user:1019" in said and "gamma" in said, f"the upgrade says why the entry stays: {said}")
    check(r["alpha_legacy_receipt_gone"], "alpha's legacy receipt is gone from the runtime")
    code, env = r["gamma_legacy_verify"]
    check(code == 0 and env.get("WAYLAND_DISPLAY") == f"{RUNTIME}/wayland-0",
          f"a project not yet upgraded still launches on its legacy grant under this release: {env}")
    code, said = r["gamma_upgrade"]
    check(code == 0 and "user:1019:--x" not in " ".join(r["after_gamma_acl"])
          and "user:1020:--x" in r["after_gamma_acl"] and "user:1022:--x" in r["after_gamma_acl"],
          f"gamma's upgrade takes tenanta's entry off and leaves the others: {r['after_gamma_acl']} {said}")

    # No compositor: refused before anything is touched.
    code, said = r["beta_no_compositor"]
    check(code == 1 and "could not be installed" in said and "missing or ambiguous" in said,
          f"beta's upgrade with no compositor is refused: {said[-400:]}")
    check(r["beta_no_compositor_state"] == {"helper_is_legacy": True, "runtime_unchanged": True, "leaf": False,
                                            "config_unchanged": True},
          f"and nothing changed: {r['beta_no_compositor_state']}")

    # A cutover that cannot complete.
    code, said = r["beta_failed_upgrade"]
    after = r["beta_after_failure"]
    check(code == 1 and "desktop setup incomplete before launch" in said and "tenantb has no runtime directory" in said,
          f"beta's upgrade, whose tenant cannot verify, fails: {said[-400:]}")
    check(after["helper_is_legacy"] and not after["backup_left"] and after["service_running"],
          f"the legacy helper is back and running: {after}")
    check(after["runtime_entry"] == ["user:1020:--x"], f"beta's legacy entry was never touched: {after}")
    check(after["leaf_acl"] is not None and not any(e.startswith("user:1020") for e in after["leaf_acl"])
          and after["leaf_entries"] in ([], ["paste"]) and "wayland-0" not in (after["leaf_entries"] or []),
          f"the exposure it made holds no grant, socket or receipt: {after}")
    check(after["config_unchanged"], "beta's configuration is unchanged")
    code, said = r["beta_upgrade"]
    check(code == 0, f"beta's upgrade, with the compositor back, succeeds: {said}")
    check(r["after_beta_acl"] == ["user::rwx", "user:1022:--x", "group::---", "mask::--x", "other::---"],
          f"only the orphan delta's entry is left: {r['after_beta_acl']}")

    # Two tenants, each confined.
    check(r["concurrent_paste"] == ["shared-clip", "shared-clip"], f"two tenants paste at once: {r['concurrent_paste']}")
    reach = r["cross_reach"]
    check(reach["a_to_b"] == "refused 13" and reach["b_to_a"] == "refused 13" and reach["stranger_to_a"] == "refused 13"
          and reach["stranger_to_receipt"] == "refused 13",
          f"no tenant reaches another's exposure, nor does a stranger: {reach}")
    check(reach["a_leaf_listing"] == "stat 13", f"a tenant cannot list its own exposure: {reach}")
    check(reach["a_runtime"] == "stat 13" and reach["a_bus"] == "refused 13" and reach["a_compositor"] == "refused 13",
          f"a tenant reaches nothing in the owner's runtime: not its bus, not the compositor: {reach}")
    check(reach["b_paste_dir"] == "listed", f"a tenant lists its own hand-off directory: {reach}")
    check(r["stranger_relay"].startswith("DesktopAccessError") and "closed the connection" in r["stranger_relay"],
          f"the relay closes a connection from another uid unanswered: {r['stranger_relay']}")
    check(r["owner_relay"].startswith("DesktopAccessError"), f"even the owner's own uid is not the tenant: {r['owner_relay']}")
    check("refused a connection from uid 1021" in r["refusal_logged"] and "refused a connection from uid 1000" in r["refusal_logged"],
          f"each refused uid is logged once: {r['refusal_logged']}")

    # The socket moves.
    check(isinstance(r["pending_receipt"], dict) and "reason" in r["pending_receipt"],
          f"with no compositor the receipt says why it waits: {r['pending_receipt']}")
    code, said = r["verify_while_absent"]
    check(code != 0 and "is waiting" in said, f"a launch meanwhile quotes the reason: {said}")
    check(isinstance(r["renamed_receipt"], dict) and r["renamed_receipt"].get("state") == "ready",
          f"KWin on wayland-1: the relay follows it: {r['renamed_receipt']}")
    check(r["renamed_roundtrip"] == "answered", f"the tenant's unchanged WAYLAND_DISPLAY reaches it: {r['renamed_roundtrip']}")
    check(r["renamed_mode"] == "0o710", "the orphan's legacy entry is still the only thing holding 0710")

    # A new login.
    check(r["relogin_ready"] and r["relogin_verify"] == [0, 0], f"after a new login both tenants are ready: {r['relogin_verify']}")
    check(r["relogin_acl"] == ["user::rwx", "user:1022:--x", "group::---", "mask::--x", "other::---"],
          f"the new runtime has only the orphan's legacy re-grant, nothing of the upgraded grants: {r['relogin_acl']}")

    # The trust walk.
    code, said = r["tenant_owned_root"]
    check(code != 0 and "/var/lib/switchyard/desktop is not a directory only uid 0 may write" in said,
          f"an exposure root anyone but root owns is refused: {said}")
    check(r["owner_cannot_make"][0].startswith("missing: /var/lib/switchyard/desktop/1000-1019-omega does not exist")
          and r["owner_cannot_make"][1] is False,
          f"only root makes an exposure; the owner's own call finds it missing: {r['owner_cannot_make']}")

    # Repair.
    code, said = r["repair_report"]
    check(code == 1 and "delta (tenantc): not a registered project; repair --apply withdraws its grant" in said
          and "0710 instead of 0700" in said and "user:1022:--x" in said,
          f"repair names the orphan grant and what keeps the runtime 0710: {said}")
    check(all(f"{p} (" in said and "on its exposure" in said for p in ("alpha", "beta", "gamma")),
          f"repair lists the upgraded grants: {said}")
    code, said, err = r["repair_apply"]
    check(code == 0 and "its grant was withdrawn" in said and "/run/user/1000 is 0700" in said, f"repair --apply: {said} {err}")
    check(r["after_repair_mode"] == "0o700" and r["after_repair_acl"] == ["user::rwx", "group::---", "other::---"],
          f"the runtime is 0700 with a minimal ACL: {r['after_repair_mode']} {r['after_repair_acl']}")
    check(r["after_repair_qt"] == "valid" and r["delta_unit_gone"], f"the Qt check passes: {r['after_repair_qt']}")

    # Revocation.
    code, said = r["revoke"]
    check(code == 0, f"alpha's revocation succeeds: {said}")
    check(r["revoked_connection"] == ["open", "eof"], f"an open tenant connection is closed: {r['revoked_connection']}")
    state = r["revoked_state"]
    check(not state["leaf"] and not state["service"] and not state["unit"], f"its exposure, service and unit are gone: {state}")
    check(state["gamma_still"] == "ready" and state["beta_still"] == "ready", f"the same tenant's other grant and another tenant's stay: {state}")
    check(r["revoked_verify"][0] != 0, f"a revoked tenant's verify fails: {r['revoked_verify']}")
    check(r["beta_after_revoke_paste"] == "after-revoke", f"beta still pastes: {r['beta_after_revoke_paste']}")

    # Teardown.
    code, said, err = r["teardown"]
    check(code == 0 and "withdrew beta's desktop grant on deskowner's desktop" in said, f"teardown withdraws beta's grant: {said} {err}")
    check(r["teardown_state"] == {"leaf": False, "service": False, "policy": False}, f"nothing of it is left: {r['teardown_state']}")
    check(r["teardown_again"][0] == 0 and r["teardown_again"][1].endswith("beta holds no desktop grant"),
          f"a second teardown finds nothing: {r['teardown_again']}")
    check("root's home could not be read, so a grant there was not looked for" in r["teardown"][1],
          f"a home it cannot read is named, not skipped in silence: {r['teardown']}")
    check(r["final_mode"] == "0o700" and r["final_qt"] == "valid", f"the runtime ends 0700: {r['final_mode']} {r['final_qt']}")

    # An operator's entry.
    check("user:1021:r-x" in r["unrelated_before"] and "mask::--x" in r["unrelated_before"], f"the operator's entry: {r['unrelated_before']}")
    check(r["epsilon_legacy"] == 0 and "user:1020:--x" in r["epsilon_legacy_acl"], f"epsilon's legacy grant beside it: {r['epsilon_legacy_acl']}")
    code, said = r["epsilon_upgrade"]
    check(code == 0 and r["unrelated_after"] == ["user::rwx", "user:1021:r-x", "#effective:--x", "group::---", "mask::--x",
                                                  "other::---"],
          f"the upgrade leaves the operator's entry and its mask exactly so: {r['unrelated_after']} {said}")
    check(r["unrelated_mode"] == "0o710" and r["unrelated_qt"].startswith("QStandardPaths"),
          f"that entry is not Switchyard's, so the runtime stays as its owner made it: {r['unrelated_mode']}")
    check("user:1021" in said and "0710 instead of 0700" in said, f"and the upgrade says what still holds it: {said}")

    # A failed fresh install.
    code, said = r["zeta_failed"]
    check(code == 1 and "fixture: systemctl enable refused" in said, f"zeta's install fails at its service: {said[-300:]}")
    check(r["zeta_state"]["policy"] is False and r["zeta_state"]["runtime"] == []
          and not any(e.startswith("user:1022") for e in (r["zeta_state"]["leaf_acl"] or [])),
          f"and leaves no policy, no exposure grant and no runtime entry: {r['zeta_state']}")
    print(f"desktop_wayland_exposure_test: {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
