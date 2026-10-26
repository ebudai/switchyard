#!/usr/bin/env python3
"""One image crosses from the desktop owner to a role pane, as two real Unix identities (SYRD-247).

Director review of a2a74e9 found the production path unable to deliver: the
listener's entry point never wired the spool, the owner's spool was 0700 with
no directory grant, and the image was deleted as soon as its reference was
typed. This runs the whole crossing with separate accounts and real ACLs, in a
user namespace (`unshare --map-auto --map-root-user --mount`) so nothing on the
host is touched:

* a chroot whose `/` is the namespace root's, so the grant's exposure under
  `/var/lib/switchyard/desktop` is proven by the shipped trust walk; a tmpfs
  over `/run/user` and a namespace-only `/etc/passwd` naming a desktop owner
  (uid 1000), the tenant (1019) and an unrelated tenant (1020);
* the desktop owner installs its policy copy and runs the shipped
  `scripts/switchyard paste-image` wrapper, with only `wl-paste` and `loginctl`
  stubbed -- `setfacl`/`getfacl` and the kernel's ACL checks are real;
* the tenant runs the shipped listener's `main()` with the environment its
  generated unit declares. Only `run_forever` is stood in for, by one pass of
  the real `listen_once` against a fake database. Delivery goes through the
  real `directorctl send` into a tmux server the tenant owns, where a stand-in
  provider opens the image a second after the message lands;
* the owner's access-service sweep and the grant's withdrawal then remove it.

Main, from a git archive, runs the same steps: it has no `paste-image`, and
its listener delivers nothing.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import zlib
from pathlib import Path

import live_boundary  # noqa: E402

live_boundary.isolate()  # SYRD-627: no live home, pane store or runtime directory, here or in a child

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "3d410cadd9a81835fad9422bb9513707c4140ec9"  # main before SYRD-247's resumed implementation
PROJECT = "imgproj"
GUI, TENANT, OTHER = ("deskowner", 1000), ("tenantacct", 1019), ("othertenant", 1020)
RUNTIME = Path("/run/user/1000")
#: The grant's exposure (SYRD-687), outside the owner's runtime directory, and its hand-off directory.
EXPOSURE = Path(f"/var/lib/switchyard/desktop/1000-1019-{PROJECT}")
LEAF = EXPOSURE / "paste"
TTL = 15 * 60

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("ok   " if condition else "FAIL ") + message)
    if not condition:
        failures.append(message)


def png() -> bytes:
    """A real 2x2 PNG, so the stand-in provider opens an actual image."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + bytes([255, 0, 0, 255, 0, 0, 255, 255]) for _ in range(2))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


IMAGE = png()

# ---------------------------------------------------------------- inside the namespace

STUB_WL_PASTE = """#!/bin/sh
case "$*" in
  "--list-types") printf 'image/png\\ntext/plain;charset=utf-8\\n' ;;
  "--no-newline --type image/png") cat "$IMAGE_FIXTURE" ;;
  *) echo "unexpected wl-paste $*" >&2; exit 2 ;;
esac
"""
STUB_LOGINCTL = """#!/bin/sh
# loginctl show-user NAME -p RuntimePath --value
[ "$1" = show-user ] && [ "$3 $4 $5" = "-p RuntimePath --value" ] || { echo "unexpected loginctl $*" >&2; exit 2; }
printf '/run/user/%s\\n' "$(id -u "$2")"
"""
#: Like an agent CLI: the submitted line lands on screen (what directorctl waits
#: for), then the image is opened a second later, as a model's tool call would.
PROVIDER = r"""stty -echo; while IFS= read -r line; do printf 'got: %s\n' "$line"; printf '%s\n' "$line" >> "$RECEIVED"
p=$(printf '%s' "$line" | grep -o '/[^ ]*\.png'); sleep 1
if [ -f "$p" ]; then printf 'opened %s %s\n' "$p" "$(sha256sum < "$p" | cut -d' ' -f1)" >> "$OPENED"
else printf 'missing %s\n' "$p" >> "$OPENED"; fi; done"""

LISTENER_PASS = r"""
import json, os, sys, threading, http.server, subprocess, time
from pathlib import Path
tree, base, label = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path[:0] = [str(tree / "tests"), str(tree)]
# The listener first, so it reads the unit's environment as the service would;
# the test helpers strip pane variables when imported, so they are put back.
unit_environment = dict(os.environ)
from scripts.ticket_board import notify_listener as nl
import ticket_board_notify_listener_test as lt
os.environ.clear(); os.environ.update(unit_environment)

class Board(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/runtime-assignments/app":
            body = json.dumps({"project": "imgproj", "authority_mode": "process",
                               "assignment": {"actual_target": "imgproj-app:0.0"}}).encode()
            self.send_response(200)
        else:
            body = b"not found"; self.send_response(404)
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Board)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["TICKET_BOARD_URL"] = f"http://127.0.0.1:{server.server_port}"
seen = {}

def one_pass(self):
    # The unit's run_forever, as one pass of the real loop against a fake
    # database. The pane-hook verdict is idle: no hooks run in this test.
    seen["handoffs"] = type(getattr(self, "image_handoffs", None)).__name__
    self.connector = lambda *a, **k: lt.FakeConnection([])
    self.poll_seconds = 0
    self.activity_gate = lambda target: False
    self.listen_once(max_notifications=1)

nl.TicketBoardNotifyListener.run_forever = one_pass
nl.main(["--directorctl", str(tree / "scripts" / "directorctl"),
         "--pane-state-dir", str(base / "tenant-state" / "pane-state")])
opened = base / "tenant-state" / f"opened-{label}"
deadline = time.monotonic() + 15
while time.monotonic() < deadline and not opened.exists():
    time.sleep(0.2)
time.sleep(1.5)
server.shutdown()
print(json.dumps(seen))
"""


def as_user(user: tuple[str, int], argv: list[str], env: dict[str, str], *, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    name, uid = user
    return subprocess.run(["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", *argv],
                          env={**env, "USER": name, "LOGNAME": name}, capture_output=True, text=True,
                          timeout=timeout)


def py(user, code: str, *args: str, env: dict[str, str], tree: Path) -> subprocess.CompletedProcess[str]:
    return as_user(user, [sys.executable, "-c", f"import sys; sys.path[:0] = [{str(tree)!r}]\n" + code, *args], env)


def enter_root(base: Path) -> None:
    """A `/` the namespace's root owns, as a host's is root's, with the host's /usr, /etc and /tmp bound in."""
    root = base / "root"
    root.mkdir(mode=0o755)
    for top in ("usr", "etc", "dev", "proc", "sys", "tmp", "root"):
        (root / top).mkdir()
        subprocess.run(["mount", "--rbind", f"/{top}", str(root / top)], check=True)
    for link in ("bin", "sbin", "lib", "lib64"):
        if os.path.islink(f"/{link}"):
            os.symlink(os.readlink(f"/{link}"), root / link)
    (root / "run").mkdir()
    (root / "var/lib/switchyard").mkdir(parents=True, mode=0o755)
    subprocess.run(["mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", str(root / "run")], check=True)
    (root / "run/user").mkdir(mode=0o755)
    for name in ("passwd", "group"):
        subprocess.run(["mount", "--bind", str(base / name), str(root / "etc" / name)], check=True)
    os.chroot(root)
    os.chdir("/")


def namespace(base: Path) -> dict:
    users = [GUI, TENANT, OTHER]
    (base / "passwd").write_text("root:x:0:0::/root:/bin/sh\n" + "".join(
        f"{name}:x:{uid}:{uid}::{base / 'home' / name}:/bin/sh\n" for name, uid in users))
    (base / "group").write_text("root:x:0:\n" + "".join(f"{name}:x:{uid}:\n" for name, uid in users))
    enter_root(base)
    for name, uid in users:
        for path in (Path(f"/run/user/{uid}"), base / "home" / name):
            path.mkdir(parents=True, mode=0o700)
            os.chown(path, uid, uid)
    stubs = base / "stubs"
    stubs.mkdir(mode=0o755)
    for name, text in (("wl-paste", STUB_WL_PASTE), ("loginctl", STUB_LOGINCTL)):
        (stubs / name).write_text(text)
        (stubs / name).chmod(0o755)
    (base / "image.png").write_bytes(IMAGE)
    (base / "image.png").chmod(0o644)

    sys.path[:0] = [str(base / "after")]
    from scripts import desktop_access
    from scripts.ticket_board.project_provision import build_plan, render_listener_unit

    policy = desktop_access.generated_policy(project=PROJECT, tenant=TENANT[0], gui_user=GUI[0],
                                             approved_by=GUI[0], approved_at="2026-10-12T00:00:00Z")
    tenant_home = base / "home" / TENANT[0]
    config = tenant_home / "project.json"
    config.write_text(json.dumps({"project": PROJECT, "desktop_access": policy}))
    os.chown(config, TENANT[1], TENANT[1])
    config.chmod(0o600)
    registry = base / "registry"
    registry.mkdir(mode=0o755)
    (registry / f"{PROJECT}.json").write_text(json.dumps({"slug": PROJECT, "config_path": str(config)}))
    state = base / "tenant-state"
    state.mkdir(mode=0o700)
    os.chown(state, TENANT[1], TENANT[1])

    unit = render_listener_unit(build_plan(project=PROJECT, owner_user=TENANT[0], owner_home=tenant_home,
                                           board_root=base / "board", commit_git_dir=str(base / "cache.git"),
                                           asset_dir=base / "assets", frame_dir=base / "frames"))
    unit_env: dict[str, str] = {}
    for line in unit.splitlines():
        if line.startswith("Environment="):
            key, _, value = line[len("Environment="):].partition("=")
            unit_env[key] = value
    common = {"PATH": f"{stubs}:/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
              "SWITCHYARD_PROJECT_REGISTRY_DIR": str(registry), "PYTHONDONTWRITEBYTECODE": "1"}
    gui_env = {**common, "HOME": str(base / "home" / GUI[0]), "XDG_RUNTIME_DIR": str(RUNTIME),
               "IMAGE_FIXTURE": str(base / "image.png")}
    tenant_env = {**common, **unit_env, "HOME": str(tenant_home), "XDG_RUNTIME_DIR": "/run/user/1019",
                  "TMUX_TMPDIR": str(state), "TERM": "xterm-256color",
                  "TICKET_BOARD_PANE_STATE_DIR": str(state / "pane-state")}

    # The policy copy the owner's own install writes (desktop_access.install_owner).
    installed = py(GUI, "from scripts import desktop_access as d; from pathlib import Path; import json, os\n"
                        "home = Path(os.environ['HOME'])\n"
                        f"d.atomic_json(home / '.config/switchyard/desktop-access' / 'switchyard-desktop-1000-1019-{PROJECT}.json', json.loads(sys.argv[1]))",
                   json.dumps(policy), env=gui_env, tree=base / "after")
    assert installed.returncode == 0, installed.stderr

    # Before root has made the grant's exposure, the owner's command refuses:
    # there is nowhere the tenant could reach anything handed over.
    unapplied = as_user(GUI, [sys.executable, "-c", "import runpy, sys; sys.argv = sys.argv[1:]; "
                              "runpy.run_path(sys.argv[0], run_name='__main__')",
                              str(base / "after" / "scripts" / "switchyard"), "paste-image", PROJECT, "app"], gui_env)
    exposed_before = os.path.exists(EXPOSURE)
    # Root makes the exposure, as an install does; the owner's access service
    # then gives the tenant its entries there (`expose`). The relay needs a
    # compositor and is not involved.
    sys.path[:0] = [str(base / "after")]
    from scripts import desktop_access as made
    made.exposure_dir(made.pwd.getpwnam(GUI[0]), f"switchyard-desktop-{GUI[1]}-{TENANT[1]}-{PROJECT}", create=True)
    applied = py(GUI, "from scripts import desktop_access as d; import json; d.expose(json.loads(sys.argv[1]))",
                 json.dumps(policy), env=gui_env, tree=base / "after")
    assert applied.returncode == 0, applied.stderr
    result: dict = {"unapplied": [unapplied.returncode, (unapplied.stdout + unapplied.stderr).strip()[-300:],
                                  exposed_before]}
    for label in ("before", "after"):
        tree = base / label
        run: dict = {}
        result[label] = run
        received, opened = state / f"received-{label}", state / f"opened-{label}"
        provider = PROVIDER.replace("$RECEIVED", str(received)).replace("$OPENED", str(opened))
        started = as_user(TENANT, ["tmux", "new-session", "-d", "-s", f"{PROJECT}-app", "-x", "220", "-y", "20",
                                   "bash", "-c", provider], tenant_env)
        run["tmux"] = started.returncode
        socket = as_user(TENANT, ["tmux", "display-message", "-p", "#{socket_path}"], tenant_env).stdout.strip()
        run["tmux_socket_private"] = socket.startswith(str(state))

        paste = as_user(GUI, [sys.executable, "-c", "import runpy, sys; sys.argv = sys.argv[1:]; "
                              "runpy.run_path(sys.argv[0], run_name='__main__')",
                              str(tree / "scripts" / "switchyard"), "paste-image", PROJECT, "app"], gui_env)
        run["paste"] = [paste.returncode, (paste.stdout + paste.stderr).strip()[-400:]]
        listing = py(GUI, "import os, json, subprocess\n"
                          f"leaf = {str(LEAF)!r}\n"
                          "out = {}\n"
                          "for p in (os.path.dirname(leaf), leaf) + tuple(os.path.join(leaf, n) for n in sorted(os.listdir(leaf))):\n"
                          "    st = os.lstat(p)\n"
                          "    out[p] = [oct(st.st_mode & 0o777), st.st_uid, subprocess.run(['getfacl', '-cpn', p], capture_output=True, text=True).stdout.split()]\n"
                          "print(json.dumps(out))", env=gui_env, tree=tree)
        run["owner_view"] = json.loads(listing.stdout) if listing.returncode == 0 else listing.stderr[-300:]
        if label == "after" and listing.returncode == 0:
            image = next(p for p in run["owner_view"] if p.endswith(".png"))
            manifest = image[: -len(".png")] + ".handoff.json"
            run["created_at"] = json.loads(py(GUI, f"print(open({manifest!r}).read())", env=gui_env, tree=tree).stdout)["created_at"]
            reach = ("import os, json, hashlib\n"
                     "def attempt(f):\n"
                     "    try:\n        return ['ok', f()]\n"
                     "    except OSError as e:\n        return ['refused', e.errno]\n"
                     f"image, leaf = {image!r}, {str(LEAF)!r}\n"
                     "print(json.dumps({\n"
                     f"  'list_runtime': attempt(lambda: os.listdir({str(RUNTIME)!r})),\n"
                     "  'list_parent': attempt(lambda: os.listdir(os.path.dirname(leaf))),\n"
                     "  'list_leaf': attempt(lambda: sorted(os.listdir(leaf))),\n"
                     "  'read_image': attempt(lambda: hashlib.sha256(open(image, 'rb').read()).hexdigest()),\n"
                     "  'create_in_leaf': attempt(lambda: open(os.path.join(leaf, 'planted'), 'x').close()),\n"
                     "  'unlink_image': attempt(lambda: os.unlink(image)),\n"
                     "  'write_image': attempt(lambda: open(image, 'ab').close()),\n"
                     "}))")
            run["tenant_reach"] = json.loads(py(TENANT, reach, env=tenant_env, tree=tree).stdout)
            run["other_reach"] = json.loads(py(OTHER, reach, env={**common, "HOME": str(base / "home" / OTHER[0])}, tree=tree).stdout)

        listener = as_user(TENANT, [sys.executable, "-c", LISTENER_PASS, str(tree), str(base), label], tenant_env, timeout=180)
        run["listener"] = [listener.returncode, listener.stdout.strip().splitlines()[-1:] if listener.stdout else [],
                           listener.stderr[-1500:] if listener.returncode else ""]
        run["received"] = received.read_text().splitlines() if received.exists() else []
        run["opened"] = opened.read_text().splitlines() if opened.exists() else []
        if label == "after":
            # Restarted: the same handoff is not delivered twice.
            again = as_user(TENANT, [sys.executable, "-c", LISTENER_PASS, str(tree), str(base), "after-restart"],
                            tenant_env, timeout=180)
            run["restart"] = [again.returncode, received.read_text().splitlines() if received.exists() else []]
            created = run.get("created_at", 0.0)
            sweep = ("from scripts import desktop_access as d; import os, json\n"
                     f"print(json.dumps([d.sweep_handoffs({str(EXPOSURE)!r}, now=float(sys.argv[1])), sorted(os.listdir({str(LEAF)!r}))]))")
            run["sweep_early"] = json.loads(py(GUI, sweep, str(created + TTL - 2), env=gui_env, tree=tree).stdout)
            run["sweep_due"] = json.loads(py(GUI, sweep, str(created + TTL + 1), env=gui_env, tree=tree).stdout)
            # A second handoff, then the grant withdrawn: rollback removes it.
            second = as_user(GUI, [sys.executable, "-c", "import runpy, sys; sys.argv = sys.argv[1:]; "
                                   "runpy.run_path(sys.argv[0], run_name='__main__')",
                                   str(tree / "scripts" / "switchyard"), "paste-image", PROJECT, "app"], gui_env)
            withdraw = py(GUI, "from scripts import desktop_access as d; import json, os, subprocess\n"
                               "d.rollback(json.loads(sys.argv[1]))\n"
                               f"print(json.dumps([os.path.exists({str(LEAF)!r}), 'user:{TENANT[1]}:--x' in subprocess.run(['getfacl', '-cpn', {str(EXPOSURE)!r}], capture_output=True, text=True).stdout]))",
                          json.dumps(policy), env=gui_env, tree=tree)
            run["withdraw"] = [second.returncode, withdraw.returncode, withdraw.stdout.strip() or withdraw.stderr[-300:]]
        as_user(TENANT, ["tmux", "kill-server"], tenant_env)
    return result


# ---------------------------------------------------------------- the parent


def tree_at(commit: str, into: Path) -> None:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--namespace":
        print(json.dumps(namespace(Path(sys.argv[2]))))
        return 0

    base = Path(tempfile.mkdtemp(prefix="syrd247."))
    try:
        base.chmod(0o755)
        tree_at(BEFORE, base / "before")
        for part in ("scripts", "tests", "examples"):
            shutil.copytree(ROOT / part, base / "after" / part, ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run(["chmod", "-R", "a+rX", str(base / "before"), str(base / "after")], check=True)
        child = subprocess.run(
            ["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private",
             sys.executable, str(Path(__file__).resolve()), "--namespace", str(base)],
            capture_output=True, text=True, timeout=900,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert child.returncode == 0, child.stderr[-4000:]
        result = json.loads(child.stdout.strip().splitlines()[-1])
    finally:
        # Files the namespace's accounts own are removable only from inside one.
        subprocess.run(["unshare", "--map-auto", "--map-root-user", sys.executable, "-c",
                        "import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)", str(base)],
                       capture_output=True)
        shutil.rmtree(base, ignore_errors=True)
    before, after = result["before"], result["after"]
    digest = hashlib.sha256(IMAGE).hexdigest()
    check(result["unapplied"][0] != 0 and "does not exist; root makes it once" in result["unapplied"][1]
          and result["unapplied"][2] is False,
          f"with the approved grant not applied, nothing is handed over: {result['unapplied']}")

    # Main: there is no command, and nothing reaches the pane.
    check(before["tmux"] == 0 and before["tmux_socket_private"], "before: the tenant's tmux server is the test's own")
    check(before["paste"][0] != 0 and "paste-image" in before["paste"][1],
          f"before: main has no paste-image: {before['paste']}")
    check(not isinstance(before["owner_view"], dict) or not any(path.startswith(str(LEAF) + "/") for path in before["owner_view"]),
          f"before: nothing was handed over: {before['owner_view']}")
    check(before["listener"][0] == 0 and before["received"] == [] and before["opened"] == [],
          f"before: main's listener delivers nothing: {before['listener']} {before['received']}")

    # The owner's command, through the shipped wrapper.
    check(after["tmux"] == 0 and after["tmux_socket_private"], "after: the tenant's tmux server is the test's own")
    check(after["paste"][0] == 0 and "is waiting for imgproj's app" in after["paste"][1],
          f"after: the desktop owner's paste-image deposits: {after['paste']}")
    view = after["owner_view"]
    check(isinstance(view, dict) and len(view) == 4, f"after: a directory, the project's, an image and a manifest: {view}")
    parent, leaf = view[str(LEAF.parent)], view[str(LEAF)]
    files = {path: entry for path, entry in view.items() if path.startswith(str(LEAF) + "/")}
    check(parent[1] == 1000 and "user:1019:--x" in parent[2] and "other::---" in parent[2] and "group::---" in parent[2],
          f"after: the exposure is the owner's, the tenant may only traverse it: {parent}")
    check(leaf[1] == 1000 and "user:1019:r-x" in leaf[2] and "other::---" in leaf[2] and "group::---" in leaf[2],
          f"after: the hand-off directory is the owner's, the tenant may list it: {leaf}")
    check(all(entry[1] == 1000 and "user:1019:r--" in entry[2] and "other::---" in entry[2] and "user::rw-" in entry[2]
              for entry in files.values()) and len(files) == 2,
          f"after: each file is the owner's, readable by the tenant alone: {files}")
    reach = after["tenant_reach"]
    check(reach["list_runtime"] == ["refused", 13] and reach["list_parent"] == ["refused", 13],
          f"after: the tenant cannot list the owner's runtime or the handoff parent: {reach}")
    check(reach["list_leaf"][0] == "ok" and len(reach["list_leaf"][1]) == 2 and reach["read_image"] == ["ok", digest],
          f"after: the tenant lists its project's handoffs and reads the image: {reach}")
    check(reach["create_in_leaf"] == ["refused", 13] and reach["unlink_image"] == ["refused", 13]
          and reach["write_image"] == ["refused", 13],
          f"after: the tenant writes nothing there, and cannot remove the image: {reach}")
    other = after["other_reach"]
    check(all(other[key] == ["refused", 13] for key in ("list_parent", "list_leaf", "read_image")),
          f"after: another tenant cannot reach it at all: {other}")

    # The tenant's listener, from its shipped entry point.
    check(after["listener"][0] == 0 and after["listener"][1] == ['{"handoffs": "ImageHandoffs"}'],
          f"after: the listener's main() wires the handoffs from the tenant's own policy: {after['listener']}")
    # The exposure's path is longer than the runtime's was, so the line is the short form; either fits one line.
    check(len(after["received"]) == 1 and after["received"][0].startswith((f"Image from deskowner's clipboard: {LEAF}/paste-",
                                                                           f"Image: {LEAF}/paste-"))
          and len(after["received"][0]) <= 200,
          f"after: the role is told once, in one direct line, where its image is: {after['received']}")
    check(len(after["opened"]) == 1 and after["opened"][0].startswith("opened ") and after["opened"][0].endswith(digest),
          f"after: the provider opens it after the message lands, and it is the copied image: {after['opened']}")
    check(after["restart"][0] == 0 and after["restart"][1] == after["received"],
          f"after: a restarted listener does not deliver it again: {after['restart']}")

    # Retention and removal.
    check(after["sweep_early"][0] == [] and len(after["sweep_early"][1]) == 2,
          f"after: the owner's sweep keeps it until 15 minutes after the copy: {after['sweep_early']}")
    check(sorted(after["sweep_due"][0]) == sorted(after["sweep_early"][1]) and after["sweep_due"][1] == [],
          f"after: and then removes the image and its manifest: {after['sweep_due']}")
    check(after["withdraw"][:2] == [0, 0] and after["withdraw"][2] == "[false, false]",
          f"after: withdrawing the grant removes what is still handed over: {after['withdraw']}")

    if failures:
        print(f"image_handoff_two_identity_test: {len(failures)} failed")
        return 1
    print("image_handoff_two_identity_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
