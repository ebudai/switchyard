#!/usr/bin/env python3
"""SYRD-627: a test run never changes a live account's home or runtime directory.

* **Production.** The caller's pane stores (TICKET_BOARD_PANE_SESSION_DIR,
  TICKET_BOARD_PANE_STATE_DIR) stand only for the caller's own account. A
  project configuration with no session_dir, loaded in a role pane for another
  owner, resolves that owner's own store.
* **The p-ops/S1 switch,** as SYRD-612's suite staged it and as the incident
  happened. It runs in a namespace child as root, with the Audit pane's
  variables pointing at a decoy live store under a disposable root. A project
  `p` run by owner 1500, configured with no session_dir, records `S1` for
  `p-ops:0.0` and sets it aside. Here the record and its set-aside land in the
  owner's store and the decoy stays empty. The before-run, on main at BEFORE,
  writes both into the decoy, which is the incident's write.
* **The boundary** (tests/live_boundary.py):
  - `isolate()` scrubs the pane's variables from the run;
  - the guard refuses every change under a live root, in this process and in a
    namespace child that inherits it through sitecustomize, naming the command,
    and is invisible to a lookup recorder;
  - the sentinel reports an owner change in the live store, failing the run
    with the namespace command that caused it.
* **Coverage.** Every suite that maps root in a user namespace isolates itself
  before it imports production code.

The before-run is skipped with SYRD627_SKIP_BEFORE=1.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from; the before-run's tree.
BEFORE = "f22638f5ff2f72ad4b4e301e7c102d77aebad6bb"
OWNER, OWNER_UID = "1500", 1500
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


# --------------------------------------------------------------------------
# children: run from whichever tree they are told, so the before-run uses main's code
# --------------------------------------------------------------------------


def _use_tree(tree: Path) -> None:
    for extra in (str(tree), str(tree / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)


def _patch_owner(home: Path) -> None:
    real = pwd.getpwnam

    def getpwnam(name: str):
        if name == OWNER:
            return pwd.struct_passwd((OWNER, "x", OWNER_UID, OWNER_UID, "", str(home), "/bin/sh"))
        return real(name)

    pwd.getpwnam = getpwnam


def switch_child(tree: Path, base: Path, out: Path) -> None:
    """As namespace root, with the inherited pane variables: owner 1500's project p records S1 and sets it aside."""
    _use_tree(tree)
    home = base / "home" / OWNER
    (home / "prov").mkdir(parents=True)
    os.lchown(home, OWNER_UID, OWNER_UID)
    _patch_owner(home)
    from scripts import team_launcher as launcher

    config_path = home / "prov" / "p.json"
    config_path.write_text(json.dumps({"project": "p", "run_as_user": OWNER, "role_state_isolation": True, "roles": [
        {"role": "ops", "cli": ["claude"], "detached": True, "workdir": str(home / "wt")}]}))
    config = launcher.load_project_config("p", config_path)
    role = launcher._role_by_name(config, "ops")
    store = launcher.role_session_dir(config, role)
    store.mkdir(parents=True, exist_ok=True)  # a directory only; nothing above it is ever chowned
    record = store / launcher.session_file_name(role.target)
    record.write_text(json.dumps({"target": role.target, "session_id": "S1"}))
    launcher.clear_session_record_for_role(role, store)
    guarded = hasattr(os.lchown, "__wrapped__") and hasattr(os.rename, "__wrapped__")
    out.write_text(json.dumps({"session_dir": str(config.session_dir), "store": str(store), "target": role.target,
                               "guarded": guarded,
                               "pane_state": str(launcher.default_pane_state_dir_for_user(OWNER, project="p"))}))


def resolution_child(tree: Path, out: Path) -> None:
    """What the session helpers answer, with an ambient pane store, for this account, another, and an unknown one."""
    _use_tree(tree)
    from scripts import team_launcher as launcher

    me = pwd.getpwuid(os.getuid()).pw_name
    other = next(p.pw_name for p in pwd.getpwall() if p.pw_uid != os.getuid() and p.pw_dir not in ("", "/"))
    out.write_text(json.dumps({
        "me": str(launcher.default_session_dir_for_user(me)),
        "other": str(launcher.default_session_dir_for_user(other)),
        "other_home": pwd.getpwnam(other).pw_dir,
        "unknown": str(launcher.default_session_dir_for_user("syrd627-no-such-account")),
        "me_state": str(launcher.default_pane_state_dir_for_user(me, project="p")),
        "other_state": str(launcher.default_pane_state_dir_for_user(other, project="p")),
    }))


# --------------------------------------------------------------------------
# the parent
# --------------------------------------------------------------------------


def pane_env(live: Path) -> dict[str, str]:
    """The Audit pane's variables, but naming a decoy live store."""
    return {**os.environ, "HOME": str(live / "home"),
            "TICKET_BOARD_PANE_SESSION_DIR": str(live / "home/.local/state/syrd-ticket-board/pane-sessions/roles/audit"),
            "TICKET_BOARD_PANE_STATE_DIR": str(live / "run/syrd-ticket-board/pane-state"),
            "TICKET_BOARD_PANE_TARGET": "syrd-audit:0.0", "TICKET_BOARD_CALLER_ROLE": "audit",
            "TICKET_BOARD_PROJECT": "syrd"}


def files_under(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*") if p.is_file())


def run_switch(tree: Path, scratch: Path, label: str) -> dict:
    live, base, out = scratch / f"{label}-live", scratch / f"{label}-fixture", scratch / f"{label}.json"
    (live / "home/.local/state/syrd-ticket-board/pane-sessions/roles/audit").mkdir(parents=True)
    (live / "run").mkdir()
    base.mkdir()
    done = subprocess.run(["unshare", "--user", "--map-auto", "--map-root-user", sys.executable,
                           str(Path(__file__).resolve()), "--switch-child", str(tree), str(base), str(out)],
                          env=pane_env(live), capture_output=True, text=True, timeout=300)
    check(done.returncode == 0, f"{label}: the namespace child ran: {done.stderr[-1500:]}")
    result = json.loads(out.read_text())
    result["live_files"] = files_under(live)
    result["base"] = str(base)
    return result


def run_resolution(tree: Path, scratch: Path, label: str) -> dict:
    live, out = scratch / f"{label}-resolve-live", scratch / f"{label}-resolve.json"
    (live / "home").mkdir(parents=True)
    done = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--resolution-child", str(tree), str(out)],
                          env=pane_env(live), capture_output=True, text=True, timeout=120)
    check(done.returncode == 0, f"{label}: the resolution child ran: {done.stderr[-1500:]}")
    result = json.loads(out.read_text())
    result["ambient"] = pane_env(live)["TICKET_BOARD_PANE_SESSION_DIR"]
    result["ambient_state"] = pane_env(live)["TICKET_BOARD_PANE_STATE_DIR"]
    return result


def before_tree(into: Path) -> Path:
    tree = into / "before"
    tree.mkdir()
    archive = subprocess.run(["git", "-C", str(HERE), "archive", BEFORE], check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(tree)], input=archive.stdout, check=True)
    check(not (tree / "tests/live_boundary.py").exists(), "main has no test boundary")
    return tree


def test_production(scratch: Path) -> None:
    here = run_resolution(HERE, scratch, "here")
    check(here["me"] == here["ambient"] and here["me_state"] == here["ambient_state"],
          f"the caller's own account keeps its pane's stores: {here}")
    check(here["other"].startswith(here["other_home"] + "/") and here["other"] != here["ambient"]
          and here["other_state"] != here["ambient_state"],
          f"another account resolves its own store, never the caller's pane's: {here}")
    check(here["unknown"] == here["ambient"], f"an account this host cannot resolve keeps the ambient store: {here}")
    switch = run_switch(HERE, scratch, "here")
    owner_store = f"{switch['base']}/home/{OWNER}/.local/state/pgu-ticket-board/pane-sessions"
    check(switch["session_dir"] == owner_store and switch["target"] == "p-ops:0.0",
          f"p's session directory is its owner's own, not the Audit pane's: {switch}")
    check(switch["live_files"] == [], f"the decoy live store receives nothing: {switch['live_files']}")
    check(switch["guarded"] is True, "the namespace child carried the guard, through sitecustomize")
    set_aside = Path(switch["store"]) / "p-ops_0.0.json.superseded"
    check(set_aside.exists() and json.loads(set_aside.read_text())["session_id"] == "S1"
          and not (Path(switch["store"]) / "p-ops_0.0.json").exists(),
          f"S1 is recorded and set aside in the owner's store: {switch['store']}")
    check(not switch["pane_state"].startswith(str(scratch / "here-live")), f"and its pane state is not the pane's: {switch}")


def test_before_run(scratch: Path) -> None:
    tree = before_tree(scratch)
    before = run_resolution(tree, scratch, "before")
    check(before["other"] == before["ambient"], f"main hands another account the caller's pane store: {before}")
    switch = run_switch(tree, scratch, "before")
    check(switch["session_dir"].startswith(str(scratch / "before-live")),
          f"main resolves p's session directory to the Audit pane's live store: {switch}")
    check("home/.local/state/syrd-ticket-board/pane-sessions/roles/audit/roles/ops/p-ops_0.0.json.superseded"
          in switch["live_files"], f"and the S1 switch writes into it, the incident's write: {switch['live_files']}")


def test_boundary(scratch: Path) -> None:
    import live_boundary

    live = scratch / "boundary-live"
    (live / "home/.local/state/x-ticket-board/pane-sessions/roles/audit").mkdir(parents=True)
    (live / "run").mkdir()
    keep = scratch / "boundary-writable"
    keep.mkdir()
    code = ("import os, pathlib, subprocess, sys; L=sys.argv[1]; K=sys.argv[2]; out=[]\n"
            "def run(argv):  # a refused command fails with 126 and says why\n"
            "    done = subprocess.run(argv, capture_output=True)\n"
            "    if done.returncode == 126 and b'test boundary: refused' in done.stderr:\n"
            "        raise PermissionError(done.stderr.decode())\n"
            "for name, act in (('chown', lambda: os.lchown(L + '/home', os.getuid(), -1)),\n"
            "                  ('mkdir', lambda: os.makedirs(L + '/home/new')),\n"
            "                  ('write', lambda: pathlib.Path(L, 'home/f').write_text('x')),\n"
            "                  ('rename', lambda: os.rename(L + '/run', L + '/run2')),\n"
            "                  ('exec chown', lambda: run(['chown', '-h', '0', L + '/home'])),\n"
            "                  ('writable chown', lambda: os.lchown(K, os.getuid(), -1)),\n"
            "                  ('writable write', lambda: pathlib.Path(K, 'f').write_text('x'))):\n"
            "    try: act(); out.append(name + ' allowed')\n"
            "    except PermissionError: out.append(name + ' refused')\n"
            "print(';'.join(out))\n")
    env = {**os.environ, live_boundary.LIVE_ROOTS_ENV: f"{live}:{keep}", live_boundary.ALLOW_ENV: str(keep),
           "PYTHONPATH": str(live_boundary.SITE_DIR)}
    expected = ("chown refused;mkdir refused;write refused;rename refused;exec chown refused;writable chown refused;"
                "writable write allowed")
    for label, argv in (("a Python child", [sys.executable, "-c", code, str(live), str(keep)]),
                        ("a namespace child", ["unshare", "--user", "--map-auto", "--map-root-user", sys.executable,
                                               "-c", code, str(live), str(keep)])):
        done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        check(done.stdout.strip() == expected, f"{label} carries the guard: {done.stdout} {done.stderr[-600:]}")
        check("test boundary: REFUSED lchown" in done.stderr and "command " in done.stderr,
              f"{label}: each refusal names its command and stack: {done.stderr[-400:]}")
    check(not (live / "home/new").exists() and not (live / "home/f").exists() and (live / "run").exists()
          and (live / "home").stat().st_uid == os.getuid(), "and nothing under the live root changed")
    # Invisible to a suite's own lookup recorder.
    seen: list[str] = []
    real = os.lstat
    os.lstat = lambda path, *a, **k: (seen.append(str(path)), real(path, *a, **k))[1]  # type: ignore[assignment]
    try:
        live_boundary._resolve(str(live / "home/f"))
    finally:
        os.lstat = real  # type: ignore[assignment]
    check(seen == [], f"resolving a path is not a lookup a suite records: {seen}")
    # isolate() leaves a child none of the pane's variables.
    # A fresh run from a pane has no boundary yet; this test's own boundary always carries its variables into the probe
    # (test_inheritance), so the probe forgets them before isolating. Its guards stay loaded.
    probe = ("import json, os, sys; sys.path.insert(0, sys.argv[1]); import live_boundary\n"
             "for k in [k for k in os.environ if k.startswith('SWITCHYARD_TEST_')]: del os.environ[k]\n"
             "root = live_boundary.isolate();"
             "print(json.dumps({k: v for k, v in os.environ.items() if k.startswith(('TICKET_BOARD_', 'PGU_', 'TMUX', "
             "'XDG_', 'HOME', 'TMPDIR', 'DIRECTORCTL'))}))")
    # A fresh run from a pane, not a child of this already isolated one: its HOME is the account's real home.
    # (Only the environment is read; the probe writes nothing there.)
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    dirty = {**{k: v for k, v in pane_env(live).items() if not k.startswith("SWITCHYARD_TEST_")},
             "HOME": real_home, "PGU_TICKET_BOARD_PANE_SESSION_DIR": "/x", "DIRECTORCTL_SOCKET": "/x",
             "TMUX": "/x,1,0", "XDG_STATE_HOME": f"{real_home}/.local/state"}
    done = subprocess.run([sys.executable, "-c", probe, str(HERE / "tests")], env=dirty, capture_output=True, text=True,
                          timeout=60)
    seen_env = json.loads(done.stdout.strip().splitlines()[-1])
    check(not any(k.startswith(("TICKET_BOARD_", "PGU_", "DIRECTORCTL")) or k in ("TMUX", "TMUX_PANE") for k in seen_env)
          and all("/sytb." in seen_env.get(k, "") for k in ("HOME", "TMPDIR", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "TMUX_TMPDIR")),
          f"an isolated run carries none of the pane's variables, and a disposable TMUX_TMPDIR: {seen_env}")
    # A child a suite prepared on purpose keeps its environment, less whatever points into a live root.
    prepared = {**{k: v for k, v in os.environ.items() if not k.startswith("SWITCHYARD_TEST_")},
                "HOME": str(live / "fixture-home"), "TICKET_BOARD_URL": "http://127.0.0.1:1",
                "TICKET_BOARD_PANE_SESSION_DIR": f"{real_home}/.local/state/syrd-ticket-board/pane-sessions",
                "XDG_STATE_HOME": f"{real_home}/.local/state"}
    done = subprocess.run([sys.executable, "-c", probe, str(HERE / "tests")], env=prepared, capture_output=True,
                          text=True, timeout=60)
    kept = json.loads(done.stdout.strip().splitlines()[-1])
    check(kept.get("HOME") == str(live / "fixture-home") and kept.get("TICKET_BOARD_URL") == "http://127.0.0.1:1"
          and "TICKET_BOARD_PANE_SESSION_DIR" not in kept and "XDG_STATE_HOME" not in kept,
          f"a prepared child keeps its own HOME and settings, and loses only what names a live root: {kept}")
    # Its tmux is never the account's: a TMUX naming the account's directory and its pane go, and a TMUX_TMPDIR that
    # is missing or /tmp is replaced by a private one. A TMUX_TMPDIR the suite chose elsewhere stays.
    own = live_boundary.account_tmux_dir(os.getuid())
    for given, expect_private in (("/tmp", True), (None, True), (str(live / "suite-tmux"), False)):
        env = {k: v for k, v in prepared.items() if not k.startswith("TMUX")}
        env.update(TMUX=f"{own}/default,1,0", TMUX_PANE="%1")
        if given is not None:
            env["TMUX_TMPDIR"] = given
        done = subprocess.run([sys.executable, "-c", probe, str(HERE / "tests")], env=env, capture_output=True,
                              text=True, timeout=60)
        tmux = json.loads(done.stdout.strip().splitlines()[-1])
        chosen = tmux.get("TMUX_TMPDIR", "")
        check("TMUX" not in tmux and "TMUX_PANE" not in tmux
              and (("/sytx." in chosen) if expect_private else chosen == given),
              f"a prepared child given TMUX_TMPDIR={given} never reaches the account's tmux: {tmux}")
    # The sentinel names an owner change in the live store, and the namespace command that made it.
    sentinel_code = (
        "import os, subprocess, sys, atexit; sys.path.insert(0, sys.argv[2]); import live_boundary as b\n"
        "b.install_guard([], [])\n"
        "s = b.Sentinel(sys.argv[1] + '/home', sys.argv[1] + '/run', os.getuid())\n"
        "atexit.register(b._verify_at_exit, s)\n"
        "subprocess.run(['unshare', '--user', '--map-auto', '--map-root-user', 'chown', '-h', '1500:1500',\n"
        "                sys.argv[1] + '/home/.local/state/x-ticket-board/pane-sessions'], check=True)\n")
    done = subprocess.run([sys.executable, "-c", sentinel_code, str(live), str(HERE / "tests")], capture_output=True,
                          text=True, timeout=60)
    check(done.returncode == 97 and "LIVE DRIFT" in done.stderr and "pane-sessions" in done.stderr
          and "namespace command: unshare --user --map-auto --map-root-user chown -h 1500:1500" in done.stderr,
          f"the sentinel fails the run on drift and names the command: {done.returncode} {done.stderr[-600:]}")
    subprocess.run(["unshare", "--user", "--map-auto", "--map-root-user", "chown", "-R", "-h", "0:0", str(live)], check=True)


def test_tmux(scratch: Path) -> None:
    """The account's own tmux is never reachable: removing TMUX is not isolation, a disposable TMUX_TMPDIR is."""
    import live_boundary

    uid, own = os.getuid(), live_boundary.account_tmux_dir(os.getuid())
    cases = (
        (["tmux", "kill-server"], {}, f"{own}/default"),
        (["tmux", "kill-server"], {"TMUX_TMPDIR": "/q"}, f"/q/tmux-{uid}/default"),
        (["tmux", "-L", "x", "ls"], {"TMUX": f"{own}/default,1,0"}, f"{own}/x"),
        (["tmux", "ls"], {"TMUX": f"{own}/default,1,0", "TMUX_TMPDIR": "/q"}, f"{own}/default"),
        (["tmux", "-S", "/s/t", "ls"], {"TMUX": f"{own}/default,1,0"}, "/s/t"),
        (["env", "TMUX_TMPDIR=/q", "tmux", "-L", "y", "ls"], {}, f"/q/tmux-{uid}/y"),
        (["env", "-i", "tmux", "ls"], {"TMUX_TMPDIR": "/q"}, f"{own}/default"),
        (["git", "status"], {}, ""),
    )
    for words, environ, expected in cases:
        check(live_boundary.tmux_socket(words, environ) == expected,
              f"tmux reaches what tmux would: {words} {environ} -> {live_boundary.tmux_socket(words, environ)}")
    # Every shared default directory is refused, not only the account's: namespace root's bare tmux resolves to
    # /tmp/tmux-0, which it would create on the host owned by the invoking account (seen there on 2026-10-18).
    for socket, expected in (("/tmp/tmux-0/default", "/tmp/tmux-0"), (f"{own}/x", own), ("/tmp/tmux-2/s", "/tmp/tmux-2"),
                             (f"{scratch}/tmux-0/default", ""), ("/s/t", "")):
        check(live_boundary._shared_tmux_dir(socket) == expected,
              f"shared tmux directory of {socket}: {live_boundary._shared_tmux_dir(socket)!r}")
    # Refused before tmux runs, in a Python child and a namespace child; a disposable socket is allowed.
    private = scratch / "tmux-private"
    private.mkdir()
    code = ("import subprocess, sys, os\n"
            "out = []\n"
            "for name, argv, env in (('bare', ['tmux', 'kill-server'], {k: v for k, v in os.environ.items() if k not in ('TMUX', 'TMUX_TMPDIR')}),\n"
            "                        ('explicit', ['tmux', '-S', sys.argv[1] + '/default', 'kill-server'], None),\n"
            "                        ('shell', 'tmux kill-server', {k: v for k, v in os.environ.items() if k != 'TMUX_TMPDIR'}),\n"
            "                        ('private', ['tmux', '-S', sys.argv[2] + '/s', 'list-sessions'], None)):\n"
            "    done = subprocess.run(argv, env=env, shell=isinstance(argv, str), capture_output=True, timeout=10)\n"
            "    refused = done.returncode == 126 and b'test boundary: refused' in done.stderr\n"
            "    out.append(name + (' refused' if refused else ' ran'))\n"
            "print(';'.join(out))\n")
    env = {**os.environ, live_boundary.LIVE_ROOTS_ENV: str(scratch / "nothing-live"),
           live_boundary.INVOKER_UID_ENV: str(uid), "PYTHONPATH": str(live_boundary.SITE_DIR)}
    for label, argv in (("a Python child", [sys.executable, "-c", code, own, str(private)]),
                        ("a namespace child", ["unshare", "--user", "--map-auto", "--map-root-user", sys.executable, "-c",
                                               code, own, str(private)])):
        done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        check(done.stdout.strip() == "bare refused;explicit refused;shell refused;private ran"
              and "REFUSED tmux on" in done.stderr,
              f"{label}: tmux on the account's own socket is refused before it runs: {done.stdout} {done.stderr[-500:]}")
    # An isolated run has its own TMUX_TMPDIR, so a bare tmux in it, kill-server included, reaches its own server.
    # A fresh run: it forgets the boundary variables this test's own boundary carries in (see test_boundary).
    probe = ("import json, os, subprocess, sys; sys.path.insert(0, sys.argv[1]); import live_boundary\n"
             "for k in [k for k in os.environ if k.startswith('SWITCHYARD_TEST_')]: del os.environ[k]\n"
             "live_boundary.isolate();"
             "assert '/sytb.' in os.environ.get('TMUX_TMPDIR', ''), 'not disposable: no kill-server';"
             "subprocess.run(['tmux', 'new-session', '-d', '-s', 'probe', 'sleep 60'], check=True);"
             "subprocess.run(['tmux', 'kill-server'], check=True);"
             "print(json.dumps({'TMUX_TMPDIR': os.environ.get('TMUX_TMPDIR', ''), 'TMUX': os.environ.get('TMUX', '')}))")
    fresh = {k: v for k, v in os.environ.items() if not k.startswith(("SWITCHYARD_TEST_", "TMUX"))}
    fresh.update(HOME=pwd.getpwuid(uid).pw_dir, TMUX=f"{own}/default,1,0")
    done = subprocess.run([sys.executable, "-c", probe, str(HERE / "tests")], env=fresh, capture_output=True, text=True,
                          timeout=60)
    seen = json.loads(done.stdout.strip().splitlines()[-1]) if done.returncode == 0 else {}
    check(seen.get("TMUX") == "" and "/sytb." in seen.get("TMUX_TMPDIR", "")
          and not seen["TMUX_TMPDIR"].startswith(own),
          f"an isolated run's tmux is a disposable server of its own: {seen} {done.stderr[-500:]}")
    # The sentinel fails the run when a tmux server is gone: one the run was started from, by its pid, and one it
    # was not (the old sweep had no TMUX), by its socket. Each stops just before the run exits.
    sentinel_code = (
        "import os, subprocess, sys, atexit; sys.path.insert(0, sys.argv[1]); import live_boundary as b\n"
        "b.install_guard([], [])\n"
        "s = b.Sentinel(sys.argv[2] + '/home', sys.argv[2] + '/run', os.getuid(), tmux=sys.argv[3], tmux_dir=sys.argv[4])\n"
        "atexit.register(b._verify_at_exit, s)\n"
        "subprocess.run(['tmux', '-S', sys.argv[4] + '/default', 'kill-server'])\n")
    for label, recorded in (("a recorded server", True), ("an unrecorded server", False)):
        dir_ = scratch / f"fake-account-tmux-{int(recorded)}"
        dir_.mkdir(mode=0o700)
        socket = dir_ / "default"
        subprocess.run(["tmux", "-S", str(socket), "new-session", "-d", "-s", "live", "sleep 300"], check=True,
                       env={k: v for k, v in os.environ.items() if not k.startswith("TMUX")})
        pid = subprocess.run(["tmux", "-S", str(socket), "display-message", "-p", "#{pid}"], capture_output=True,
                             text=True, check=True).stdout.strip()
        done = subprocess.run([sys.executable, "-c", sentinel_code, str(HERE / "tests"), str(scratch / "boundary-live"),
                               f"{socket},{pid},0" if recorded else "", str(dir_)], capture_output=True, text=True,
                              timeout=60)
        named = f"tmux server {pid} on {socket}" if recorded else f"{dir_}/default: tmux server gone"
        check(done.returncode == 97 and named in done.stderr,
              f"the sentinel fails a run that stops {label}: {done.returncode} {done.stderr[-500:]}")

def test_commands(scratch: Path) -> None:
    """Real privileged and service tools (but for queries), kills by name, and signals to everything or to a foreign
    group never run; a suite's stand-in sudo does, and what it runs is checked in turn.

    Every broad signal here is SIGCONT, and every service action names a unit or user that does not exist, so a guard
    that failed would still harm nothing."""
    import live_boundary

    code = ("import os, signal, subprocess, sys\n"
            "if sys.argv[1:] == ['start']:  # the run began now: an older group is foreign, a newer one the run's\n"
            f"    os.environ['{live_boundary.RUN_START_ENV}'] = open('/proc/self/stat').read().rsplit(')', 1)[1].split()[19]\n"
            "own = subprocess.Popen(['sleep', '30'], start_new_session=True)\n"
            "out = []\n"
            "def attempt(name, action, must_succeed=False):\n"
            "    # A refused command fails with 126 and says why; a refused signal raises PermissionError.\n"
            "    try:\n"
            "        done = action()\n"
            "    except PermissionError:\n"
            "        out.append(name + ' raised')\n"
            "        return\n"
            "    except (OSError, subprocess.SubprocessError):\n"
            "        done = None\n"
            "    refused = done == 126 << 8 or (getattr(done, 'returncode', None) == 126\n"
            "                                   and b'test boundary: refused' in done.stderr)\n"
            "    failed = must_succeed and not refused and getattr(done, 'returncode', 0) != 0\n"
            "    out.append(name + (' refused' if refused else ' failed' if failed else ' ran'))\n"
            "run = lambda argv, **kw: subprocess.run(argv, capture_output=True, timeout=10, **kw)\n"
            "attempt('sudo', lambda: run(['sudo', '-n', 'true']))\n"
            "fake = {**os.environ, 'PATH': os.environ['SYRD627_FAKE_BIN'] + ':' + os.environ['PATH']}\n"
            "attempt('wrapped systemctl', lambda: run(['env', 'A=1', 'timeout', '-k', '1s', '5s', 'systemctl', '--user', 'stop', 'syrd627-none.service']))\n"
            "attempt('loginctl in a shell', lambda: run('true && loginctl terminate-user syrd627-nobody', shell=True))\n"
            "attempt('systemctl query', lambda: run(['systemctl', '--user', 'is-active', '--quiet', 'syrd627-none.service']))\n"
            "attempt('query past an option value', lambda: run(['systemctl', '--user', '-p', 'Id', 'show', 'syrd627-none.service']))\n"
            "attempt('fake sudo', lambda: run(['sudo', 'true'], env=fake), True)\n"
            "attempt('fake sudo stop', lambda: run(['sudo', '-n', 'systemctl', '--user', 'stop', 'syrd627-none.service'], env=fake))\n"
            "attempt('script sudo function', lambda: run(['bash', '-c', 'sudo() { \"$@\"; }; sudo true']), True)\n"
            "attempt('script function pkill', lambda: run(['bash', '-c', 'sudo() { \"$@\"; }; sudo pkill -CONT -f ^no-such-syrd627']))\n"
            "attempt('fake sudo install live', lambda: run(['sudo', 'install', '-d', os.environ['SYRD627_LIVE'] + '/x'], env=fake))\n"
            "attempt('pkill', lambda: run(['pkill', '-CONT', '-f', '^no-such-process-syrd627']))\n"
            "attempt('killall', lambda: run(['/usr/bin/killall', '-CONT', 'no-such-process-syrd627']))\n"
            "attempt('kill everything', lambda: run(['kill', '-CONT', '--', '-1']))\n"
            "attempt('kill foreign group', lambda: run(['kill', '-s', 'CONT', '--', '-' + str(os.getpgid(os.getppid()))]))\n"
            "attempt('os.kill(-1)', lambda: os.kill(-1, signal.SIGCONT))\n"
            "attempt('os.killpg foreign', lambda: os.killpg(os.getpgid(os.getppid()), signal.SIGCONT))\n"
            "attempt('os.system sudo', lambda: os.system('true; sudo -n true'))\n"
            "attempt('grep names', lambda: run(['grep', '-q', 'sudo', '/dev/null']))\n"
            "attempt('os.killpg own', lambda: os.killpg(own.pid, signal.SIGTERM))\n"
            "own.wait(timeout=10)\n"
            "print(';'.join(out))\n")
    expected = ("sudo refused;wrapped systemctl refused;loginctl in a shell refused;systemctl query ran;"
                "query past an option value ran;fake sudo ran;"
                "fake sudo stop refused;script sudo function ran;script function pkill refused;fake sudo install live refused;pkill refused;killall refused;"
                "kill everything refused;kill foreign group refused;os.kill(-1) raised;os.killpg foreign raised;"
                "os.system sudo refused;grep names ran;os.killpg own ran")
    # A suite's stand-in sudo runs what it is given; the guard checks that instead.
    fake_bin = scratch / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "sudo").write_text('#!/bin/sh\nwhile [ "${1#-}" != "$1" ]; do shift; done\nexec "$@"\n')
    (fake_bin / "sudo").chmod(0o755)
    live = scratch / "commands-live"
    live.mkdir()
    env = {**os.environ, live_boundary.LIVE_ROOTS_ENV: str(live), "SYRD627_FAKE_BIN": str(fake_bin),
           "SYRD627_LIVE": str(live), live_boundary.INVOKER_UID_ENV: str(os.getuid()),
           "PYTHONPATH": str(live_boundary.SITE_DIR)}
    # Each child marks the run as begun when it starts: the run's own start is carried into every child (a child may
    # only make it later), and this process's group may be the run's.
    for label, argv in (("a Python child", [sys.executable, "-c", code, "start"]),
                        ("a namespace child", ["unshare", "--user", "--map-auto", "--map-root-user", sys.executable, "-c",
                                               code, "start"])):
        done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        check(done.stdout.strip() == expected and "REFUSED the real sudo" in done.stderr
              and "REFUSED pkill, which kills by name" in done.stderr,
              f"{label}: privileged tools and broad kills are refused, the run's own group is not: "
              f"{done.stdout} {done.stderr[-800:]}")


def test_mutations(scratch: Path) -> None:
    """A write a command would make under a live root is refused before it runs, however it is spelled: a relative
    path under the directory it runs in, a shell redirection, a `cd` in a script, `shell=True`, `os.system`, `dd of=`,
    a stand-in sudo. Writes elsewhere run. (SYRD-627 Audit.)"""
    import live_boundary

    live, keep, elsewhere = scratch / "mutations-live", scratch / "mutations-writable", scratch / "mutations-elsewhere"
    for directory in (live / "home", keep, elsewhere):
        directory.mkdir(parents=True)
    (live / "source").write_text("read, never written\n")
    fake_bin = scratch / "mutations-bin"
    fake_bin.mkdir()
    (fake_bin / "sudo").write_text('#!/bin/sh\nwhile [ "${1#-}" != "$1" ]; do shift; done\nexec "$@"\n')
    (fake_bin / "sudo").chmod(0o755)
    code = ("import os, subprocess, sys\n"
            "L, K, E = sys.argv[1] + '/home', sys.argv[2], sys.argv[3]\n"
            "fake = {**os.environ, 'PATH': sys.argv[4] + ':' + os.environ['PATH']}\n"
            "out = []\n"
            "def attempt(name, action):\n"
            "    done = action()\n"
            "    status = done if isinstance(done, int) else done.returncode\n"
            "    # Direct argv is refused before exec with 126. A shell may handle the\n"
            "    # interposer's EACCES and choose its own status; its diagnostic is authoritative.\n"
            "    refused = status != 0 if isinstance(done, int) else b'test boundary: refused' in done.stderr\n"
            "    out.append(name + (' refused' if refused else ' ran'))\n"
            "run = lambda argv, **kw: subprocess.run(argv, capture_output=True, timeout=10, **kw)\n"
            "attempt('relative under cwd', lambda: run(['touch', 'relative-write'], cwd=L))\n"
            "attempt('shell redirect', lambda: run(['sh', '-c', f'printf shell > {L}/shell-write']))\n"
            "attempt('attached redirect', lambda: run(['sh', '-c', f'echo x 2>{L}/stderr-write']))\n"
            "attempt('cd in a script', lambda: run(['sh', '-c', f'cd {L} && touch cd-write']))\n"
            "attempt('shell=True', lambda: run(f'echo x >> {L}/append-write', shell=True))\n"
            "attempt('os.system', lambda: os.system(f'touch {L}/system-write'))\n"
            "attempt('dd of=', lambda: run(['dd', 'if=/dev/null', f'of={L}/dd-write']))\n"
            "attempt('relative chmod', lambda: run(['chmod', '600', 'x'], cwd=L))\n"
            "attempt('stand-in sudo, relative', lambda: run(['sudo', 'install', '-m', '0644', '/dev/null', 'f'], cwd=L, env=fake))\n"
            "attempt('behind wrappers', lambda: run(['env', 'A=1', 'timeout', '5', 'touch', f'{L}/wrapped']))\n"
            "attempt('heredoc to a shell', lambda: run(['sh', '-c', f'sh <<EOF\\ntouch {L}/heredoc-run\\nEOF']))\n"
            "attempt('copy into a live root', lambda: run(['cp', '/dev/null', f'{L}/copied']))\n"
            "attempt('writable redirect', lambda: run(['sh', '-c', f'printf ok > {K}/ok']))\n"
            "attempt('syntax check only', lambda: run(['bash', '-n', '-c', f'touch {L}/never-run && sudo true']))\n"
            "attempt('heredoc as data', lambda: run(['sh', '-c', f\"cat > {E}/doc <<'EOF'\\ntouch {L}/in-a-heredoc\\nEOF\"]))\n"
            "attempt('quoted separators', lambda: run(['sh', '-c', f\"echo 'x; touch {L}/no; y' > {E}/quoted\"]))\n"
            "attempt('install into a writable root', lambda: run(['install', '-m', '0644', '/dev/null', f'{K}/installed']))\n"
            "attempt('install from a live root', lambda: run(['install', '-m', '0644', sys.argv[1] + '/source', f'{E}/copied']))\n"
            "attempt('null redirect', lambda: run(['sh', '-c', 'echo x > /dev/null 2>&1']))\n"
            "attempt('relative elsewhere', lambda: run(['touch', 'fine'], cwd=E))\n"
            "attempt('owner operand elsewhere', lambda: run(['chown', str(os.getuid()), 'fine'], cwd=E))\n"
            "attempt('mode operand under cwd', lambda: run(['chmod', '600', f'{E}/fine'], cwd=L))\n"
            "attempt('option value under cwd', lambda: run(['mkdir', '-m', '0755', f'{E}/made'], cwd=L))\n"
            "print(';'.join(out))\n")
    expected = ("relative under cwd refused;shell redirect refused;attached redirect refused;cd in a script refused;"
                "shell=True refused;os.system refused;dd of= refused;relative chmod refused;"
                "stand-in sudo, relative refused;behind wrappers refused;heredoc to a shell refused;copy into a live root refused;writable redirect ran;"
                "syntax check only ran;heredoc as data ran;quoted separators ran;install into a writable root ran;install from a live root ran;null redirect ran;relative elsewhere ran;"
                "owner operand elsewhere ran;mode operand under cwd ran;option value under cwd ran")
    env = {**os.environ, live_boundary.LIVE_ROOTS_ENV: f"{live}:{keep}", live_boundary.ALLOW_ENV: str(keep),
           live_boundary.INVOKER_UID_ENV: str(os.getuid()), "PYTHONPATH": str(live_boundary.SITE_DIR)}
    for label, argv in (("a Python child", [sys.executable, "-c", code]),
                        ("a namespace child", ["unshare", "--user", "--map-auto", "--map-root-user", sys.executable,
                                               "-c", code])):
        done = subprocess.run([*argv, str(live), str(keep), str(elsewhere), str(fake_bin)], env=env,
                              capture_output=True, text=True, timeout=120)
        check(done.stdout.strip() == expected and sorted(os.listdir(live / "home")) == [],
              f"{label}: every spelling of a write under a live root is refused, nothing lands there, and writes "
              f"elsewhere run: {done.stdout} {sorted(os.listdir(live / 'home'))} {done.stderr[-800:]}")


def test_runtime_shell_expansion(scratch: Path) -> None:
    """The native guard judges what Bash actually expands and executes.

    This deliberately includes the array expansion that the retired static
    reader could not model. Safe uses of the same constructs remain usable.
    """
    import live_boundary

    live, elsewhere = scratch / "runtime-live", scratch / "runtime-elsewhere"
    (live / "home").mkdir(parents=True)
    elsewhere.mkdir()
    seeded = {"delete-me": "x\n", "edit-me": "a\n"}
    for name, contents in seeded.items():
        (live / "home" / name).write_text(contents)
    script_file = elsewhere / "writes.sh"
    script_file.write_text(f'touch "{live}/home/from-file"\n')
    code = ("import os, subprocess, sys\n"
            "L, E, script_file = sys.argv[1:]\n"
            "out = []\n"
            "def attempt(name, script):\n"
            "    done = subprocess.run(['bash', '-c', script], capture_output=True, timeout=10)\n"
            "    refused = b'test boundary: refused' in done.stderr\n"
            "    out.append(name + (' refused' if refused else ' ran'))\n"
            "attempt('variable', f'P={L}; touch \"$P/variable\"')\n"
            "attempt('argv array', f'command=(touch \"{L}/array\"); \"${{command[@]}}\"')\n"
            "attempt('function', f'f() {{ touch \"$1\"; }}; f {L}/function')\n"
            "attempt('eval', f\"eval 'touch {L}/eval'\")\n"
            "attempt('script file', f'bash {script_file}')\n"
            "attempt('substitution', f'echo \"$(touch {L}/substitution)\"')\n"
            "attempt('find delete', f'find {L} -name delete-me -delete')\n"
            "attempt('sed in place', f'sed -i s/a/b/ {L}/edit-me')\n"
            "attempt('safe argv array', f'command=(touch \"{E}/safe-array\"); \"${{command[@]}}\"')\n"
            "attempt('quoted heredoc', f\"cat > {E}/doc <<'EOF'\\ntouch {L}/literal\\nEOF\")\n"
            "print(';'.join(out))\n")
    refused = ("variable", "argv array", "function", "eval", "script file", "substitution", "find delete",
               "sed in place")
    ran = ("safe argv array", "quoted heredoc")
    expected = ";".join([*(f"{name} refused" for name in refused), *(f"{name} ran" for name in ran)])
    env = {**os.environ, live_boundary.LIVE_ROOTS_ENV: str(live),
           live_boundary.INVOKER_UID_ENV: str(os.getuid()), "PYTHONPATH": str(live_boundary.SITE_DIR)}
    for label, argv in (("a Python child", [sys.executable, "-c", code]),
                        ("a namespace child", ["unshare", "--user", "--map-auto", "--map-root-user",
                                               sys.executable, "-c", code])):
        done = subprocess.run([*argv, str(live / "home"), str(elsewhere), str(script_file)], env=env,
                              capture_output=True, text=True, timeout=120)
        left = {path.name: path.read_text() for path in (live / "home").iterdir()}
        check(done.stdout.strip() == expected and left == seeded and (elsewhere / "safe-array").exists(),
              f"{label}: runtime expansion is confined without parsing the script: {done.stdout} {left} "
              f"{done.stderr[-800:]}")


def test_inheritance(scratch: Path) -> None:
    """A descendant stays behind the boundary whatever environment it is started with: a boundary variable omitted,
    emptied or replaced is the run's again, and a child may only add to it. (SYRD-627 Audit, 2f5ffe41.)

    Each change is made by `env`, a program the native layer is loaded into, so its own exec is what carries the
    boundary on; a minimal `env=` from Python is carried by the Python layer."""
    import live_boundary

    live, keep, elsewhere = scratch / "inherit-live", scratch / "inherit-writable", scratch / "inherit-elsewhere"
    for directory in (live / "home", keep, elsewhere):
        directory.mkdir(parents=True)
    (elsewhere / "link-to-live").symlink_to(live / "home")
    alternate = scratch / "alternate.so"
    source = scratch / "alternate.c"
    source.write_text("int syrd627_alternate = 1;\n")
    subprocess.run(["cc", "-shared", "-fPIC", "-o", str(alternate), str(source)], check=True)
    # The run's own log, when its runner named one (a child cannot replace it); else one for this test.
    log, other_log = Path(os.environ.get(live_boundary.LOG_ENV) or scratch / "inherit.log"), elsewhere / "replaced.log"
    LIVE, ALLOW, RUN = live_boundary.LIVE_ROOTS_ENV, live_boundary.ALLOW_ENV, live_boundary.RUN_START_ENV
    reserved = live_boundary.CARRIED_ENV
    code = ("import json, os, posix, subprocess, sys\n"
            "L, K, E, ALT, OTHER_LOG = sys.argv[1:6]\n"
            f"LIVE, ALLOW, RUN, RESERVED = {LIVE!r}, {ALLOW!r}, {RUN!r}, {list(reserved)!r}\n"
            "# The run began now: an older process group is foreign.\n"
            "os.environ[RUN] = open('/proc/self/stat').read().rsplit(')', 1)[1].split()[19]\n"
            "foreign = '-' + str(os.getpgid(os.getppid()))\n"
            "out = []\n"
            "def attempt(name, argv, **kw):\n"
            "    done = subprocess.run(argv, capture_output=True, timeout=10, **kw)\n"
            "    out.append(name + (' refused' if b'test boundary: refused' in done.stderr else ' ran'))\n"
            "# Every change goes through a shell, so the native layer decides it, not the Python one's argv check.\n"
            "touch = lambda leaf: ['sh', '-c', f'touch {L}/{leaf}']\n"
            "attempt('preload omitted', ['env', '-u', 'LD_PRELOAD', *touch('omitted-preload')])\n"
            "attempt('roots omitted', ['env', '-u', LIVE, *touch('omitted-roots')])\n"
            "attempt('cleared environment', ['env', '-i', 'PATH=/usr/bin:/bin', *touch('cleared')])\n"
            "attempt('preload empty', ['env', 'LD_PRELOAD=', *touch('empty-preload')])\n"
            "attempt('roots empty', ['env', LIVE + '=', *touch('empty-roots')])\n"
            "attempt('every variable empty', ['env', *(n + '=' for n in RESERVED), *touch('all-empty')])\n"
            "attempt('preload replaced', ['env', 'LD_PRELOAD=' + ALT, *touch('alternate-preload')])\n"
            "attempt('roots replaced', ['env', f'{LIVE}={E}', *touch('replaced-roots')])\n"
            "attempt('writable made the live root', ['env', f'{ALLOW}={L}', *touch('writable-live')])\n"
            "attempt('writable made /', ['env', f'{ALLOW}=/', *touch('writable-slash')])\n"
            "attempt('writable through a symlink', ['env', f'{ALLOW}={E}/link-to-live', *touch('writable-link')])\n"
            "attempt('chown, roots empty', ['env', LIVE + '=', 'sh', '-c', f'chown -h {os.getuid()} {L}'])\n"
            "attempt('log replaced', ['env', f'SWITCHYARD_TEST_BOUNDARY_LOG={OTHER_LOG}', *touch('replaced-log')])\n"
            "# (SIGCONT, which harms nothing if it ran)\n"
            "attempt('run start made earlier', ['sh', '-c', f'env {RUN}=1 kill -CONT -- {foreign}'])\n"
            "attempt('nested', ['env', 'LD_PRELOAD=', 'sh', '-c',\n"
            "                   f'env -i PATH=/usr/bin:/bin {LIVE}= sh -c \"touch {L}/nested\"'])\n"
            "# And with no shell between: a shell would itself collapse a duplicated variable before its exec.\n"
            "write = [sys.executable, '-I', '-c', 'import sys; open(sys.argv[1], \"w\").close()']\n"
            "attempt('direct, preload empty', ['env', 'LD_PRELOAD=', *write, f'{L}/direct-preload'])\n"
            "attempt('direct, roots empty', ['env', LIVE + '=', *write, f'{L}/direct-roots'])\n"
            "attempt('direct, roots replaced', ['env', f'{LIVE}={E}', *write, f'{L}/direct-replaced'])\n"
            "attempt('direct, writable made the live root', ['env', f'{ALLOW}={L}', *write, f'{L}/direct-writable'])\n"
            "attempt('minimal env from Python', touch('minimal-env'), env={'PATH': '/usr/bin:/bin'})\n"
            "status = os.spawnve(os.P_WAIT, '/bin/sh', ['sh', '-c', f'touch {L}/spawnve 2>/dev/null'], {'PATH': '/bin'})\n"
            "out.append('os.spawnve ' + ('refused' if status else 'ran'))\n"
            "attempt('added root', ['env', f'{LIVE}={E}/added', 'sh', '-c', f'mkdir -p {E}/added; touch {E}/added/f'])\n"
            "attempt('writable in an added root', ['env', f'{LIVE}={E}/own', f'{ALLOW}={E}/own',\n"
            "                                      'sh', '-c', f'mkdir -p {E}/own && touch {E}/own/f'])\n"
            "attempt('writable kept', ['env', '-u', ALLOW, 'sh', '-c', f'touch {K}/still-writable'])\n"
            "seen = subprocess.run(['env', *(n + '=' for n in RESERVED), f'{ALLOW}=/', f'{RUN}=1', 'env'],\n"
            "                      capture_output=True, text=True, timeout=10).stdout.splitlines()\n"
            "seen = dict(line.split('=', 1) for line in seen if '=' in line)\n"
            "# In this process, after the library loaded: dropping the variables does not switch it off.\n"
            "for name in ('LD_PRELOAD', LIVE):\n"
            "    os.environ.pop(name)\n"
            "try:\n"
            "    posix.close(posix.open(f'{L}/in-process', os.O_WRONLY | os.O_CREAT, 0o600))\n"
            "    out.append('in-process ran')\n"
            "except PermissionError:\n"
            "    out.append('in-process refused')\n"
            "print(json.dumps({'out': ';'.join(out), 'seen': {n: seen.get(n) for n in RESERVED}}))\n")
    refused = ("preload omitted", "roots omitted", "cleared environment", "preload empty", "roots empty",
               "every variable empty", "preload replaced", "roots replaced", "writable made the live root",
               "writable made /", "writable through a symlink", "chown, roots empty", "log replaced",
               "run start made earlier", "nested", "direct, preload empty", "direct, roots empty",
               "direct, roots replaced", "direct, writable made the live root", "minimal env from Python", "os.spawnve",
               "added root")
    ran = ("writable in an added root", "writable kept")
    expected = ";".join([*(f"{name} refused" for name in refused), *(f"{name} ran" for name in ran),
                         "in-process refused"])
    env = {**os.environ, LIVE: f"{live}:{keep}", ALLOW: str(keep), live_boundary.LOG_ENV: str(log),
           live_boundary.INVOKER_UID_ENV: str(os.getuid()), "PYTHONPATH": str(live_boundary.SITE_DIR)}
    for label, argv in (("a Python child", [sys.executable, "-c", code]),
                        ("a namespace child", ["unshare", "--user", "--map-auto", "--map-root-user", sys.executable,
                                               "-c", code])):
        if not os.environ.get(live_boundary.LOG_ENV):
            log.unlink(missing_ok=True)
        done = subprocess.run([*argv, str(live / "home"), str(keep), str(elsewhere), str(alternate), str(other_log)],
                              env=env, capture_output=True, text=True, timeout=180)
        try:
            result = json.loads(done.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            result = {"out": done.stdout, "seen": {}}
        seen = result["seen"]
        check(result["out"] == expected and sorted(os.listdir(live / "home")) == []
              and (live / "home").stat().st_uid == os.getuid(),
              f"{label}: an omitted, emptied or replaced boundary variable leaves the descendant behind the "
              f"boundary, and nothing lands in the live root: {result['out']} {sorted(os.listdir(live / 'home'))} "
              f"{done.stderr[-1200:]}")
        preload = (seen.get("LD_PRELOAD") or "").split(":")
        check(preload[0].endswith("/live-boundary.so") and str(live) in (seen.get(LIVE) or "").split(":")
              and "/" not in (seen.get(ALLOW) or "").split(":") and seen.get(RUN) not in (None, "", "1")
              and seen.get(live_boundary.LOG_ENV) == str(log),
              f"{label}: a descendant given empty values sees the run's: {seen}")
        check(log.exists() and "refused" in log.read_text() and not other_log.exists(),
              f"{label}: refusals go to the run's log, never to a log a descendant named: {log.exists()}")
    # The isolating process itself carries the boundary into a child started with a minimal or a replaced environment.
    # (Popen is tried on both of its paths: posix_spawn, and fork and exec, which a preexec_fn forces.)
    for label, env, extra in (
            ("env={'PATH': ...}", {"PATH": "/usr/bin:/bin"}, {}),
            ("env={'PATH': ...} by fork and exec", {"PATH": "/usr/bin:/bin"}, {"preexec_fn": os.getpid}),
            ("emptied", {**os.environ, "LD_PRELOAD": "", LIVE: "", ALLOW: "/", RUN: "1"}, {"preexec_fn": os.getpid}),
            ("replaced", {**os.environ, "LD_PRELOAD": str(alternate), LIVE: str(elsewhere),
                          ALLOW: pwd.getpwuid(os.getuid()).pw_dir,
                          RUN: "1", live_boundary.INVOKER_UID_ENV: "0", live_boundary.LOG_ENV: str(other_log)}, {})):
        probe = subprocess.run([sys.executable, "-I", "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
                               env=env, capture_output=True, text=True, timeout=60, **extra)
        given = json.loads(probe.stdout)
        # The run's own values, which a child may only extend (a preload or a live root after the run's).
        check(all(given.get(name) == os.environ.get(name) for name in reserved
                  if name not in ("LD_PRELOAD", LIVE) and (name in os.environ or name != live_boundary.LOG_ENV))
              and all(given.get(name, "").split(":")[:len(os.environ[name].split(":"))] == os.environ[name].split(":")
                      for name in ("LD_PRELOAD", LIVE))
              and given["LD_PRELOAD"].split(":")[0].endswith("/live-boundary.so"),
              f"a child the isolating process starts with {label} still gets the run's boundary: "
              f"{ {n: given.get(n) for n in reserved} }")


def test_sentinel_contents(scratch: Path) -> None:
    """The sentinel sees a store record rewritten, appended to, created or deleted by the account itself, not only an
    owner change; the live pane's own records may change contents, as its hooks do, and nothing more."""
    import live_boundary

    home, run = scratch / "contents-live" / "home", scratch / "contents-live" / "run"
    store, runtime_store = home / ".local/state/syrd-ticket-board", run / "syrd-ticket-board"
    (store / "pane-sessions/roles/ops").mkdir(parents=True)
    (runtime_store / "pane-state").mkdir(parents=True)
    record, live_record = store / "pane-sessions/roles/ops/record.json", store / "pane-sessions/roles/ops/syrd-ops_0.0.json"
    pane_state = runtime_store / "pane-state/syrd-ops_0.0.json"
    for path in (record, live_record, pane_state):
        path.write_text("{}")
        path.chmod(0o600)
    pane = {"TICKET_BOARD_PANE_SESSION_DIR": f"{store}/pane-sessions/roles/app",
            "TICKET_BOARD_PANE_STATE_DIR": f"{runtime_store}/pane-state"}
    live = live_boundary.live_stores(pane)
    check(live == sorted([str(store), str(runtime_store)]), f"the live pane's stores are read from its variables: {live}")
    uid = os.getuid()

    def drift_after(action, stores=live):
        before = live_boundary.snapshot(str(home), str(run))
        action()
        return live_boundary.drift(before, live_boundary.snapshot(str(home), str(run)), uid, stores)

    for label, action, flagged in (
            ("a record rewritten", lambda: record.write_text('{"session_id": "S1"}'), True),
            ("a record appended to", lambda: record.open("a").write("x"), True),
            ("a record created", lambda: (store / "pane-sessions/roles/ops/p-ops_0.0.json").write_text("{}"), True),
            ("a record deleted", lambda: (store / "pane-sessions/roles/ops/p-ops_0.0.json").unlink(), True),
            ("a nested test-shaped record", lambda: ((store / "pane-sessions/roles/ops/roles").mkdir(),
                                                     (store / "pane-sessions/roles/ops/roles/syrd-x_0.0.json").write_text("{}")), True),
            ("a record named for another role", lambda: (store / "pane-sessions/roles/ops/syrd-audit_0.0.json").write_text("{}"),
             True),
            ("the live pane's state rewritten", lambda: pane_state.write_text('{"busy": true}'), False),
            ("the live pane's record rewritten", lambda: live_record.write_text('{"session_id": "L"}'), False),
            ("a live record's mode changed", lambda: live_record.chmod(0o644), True),
            ("a live record deleted", lambda: pane_state.unlink(), True)):
        problems = drift_after(action)
        check(bool(problems) == flagged, f"{label}: {'reported' if flagged else 'not reported'}: {problems}")
    problems = drift_after(lambda: live_record.write_text('{"session_id": "M"}'), stores=[])
    check(problems != [], f"run from no pane, even a live pane's record rewritten is reported: {problems}")
    # And at exit: a same-owner rewrite fails the run with status 97, naming the record.
    for label, target, live_args, status in (
            ("a test record rewritten", record, [], 97),
            ("the live pane's record rewritten", live_record, [str(store)], 0)):
        script = ("import os, sys, atexit; sys.path.insert(0, sys.argv[1]); import live_boundary as b\n"
                  "s = b.Sentinel(sys.argv[2], sys.argv[3], os.getuid(), tmux_dir=sys.argv[4], live=sys.argv[6:])\n"
                  "atexit.register(b._verify_at_exit, s)\n"
                  "open(sys.argv[5], 'w').write('{\"rewritten\": true}')\n")
        done = subprocess.run([sys.executable, "-c", script, str(HERE / "tests"), str(home), str(run),
                               str(scratch / "contents-no-tmux"), str(target), *live_args],
                              capture_output=True, text=True, timeout=60)
        check(done.returncode == status and (status == 0 or f"{target}: " in done.stderr),
              f"{label}: the run exits {status}: {done.returncode} {done.stderr[-500:]}")


def test_every_namespace_suite_is_isolated() -> None:
    missing = []
    for path in sorted((HERE / "tests").glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "--map-root-user" not in text or path.name in ("live_boundary_test.py", "live_boundary.py"):
            continue
        at = text.find("live_boundary.isolate()")
        first = min((text.find(m) for m in ("\nfrom scripts", "\nimport scripts") if text.find(m) >= 0), default=len(text))
        if at < 0 or at > first:
            missing.append(path.name)
    for path in sorted((HERE / "tests").glob("*.sh")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "--map-root-user" in text and "live_boundary.py\" --run --" not in "\n".join(text.splitlines()[:8]):
            missing.append(path.name)
    check(missing == [], f"every suite that maps root in a user namespace isolates itself before importing production "
                         f"code: {missing}")


def main() -> int:
    if "--switch-child" in sys.argv:
        at = sys.argv.index("--switch-child")
        switch_child(Path(sys.argv[at + 1]), Path(sys.argv[at + 2]), Path(sys.argv[at + 3]))
        return 0
    if "--resolution-child" in sys.argv:
        at = sys.argv.index("--resolution-child")
        resolution_child(Path(sys.argv[at + 1]), Path(sys.argv[at + 2]))
        return 0
    sys.path.insert(0, str(HERE / "tests"))
    import live_boundary

    live_boundary.isolate()
    with tempfile.TemporaryDirectory(prefix="syrd627.") as raw:
        scratch = Path(raw)
        try:
            test_production(scratch)
            if not os.environ.get("SYRD627_SKIP_BEFORE"):
                test_before_run(scratch)
            test_boundary(scratch)
            test_tmux(scratch)
            test_commands(scratch)
            test_mutations(scratch)
            test_runtime_shell_expansion(scratch)
            test_inheritance(scratch)
            test_sentinel_contents(scratch)
        finally:
            subprocess.run(["unshare", "--user", "--map-auto", "--map-root-user", "chown", "-R", "-h", "0:0", str(scratch)],
                           capture_output=True, check=False)
    test_every_namespace_suite_is_isolated()
    print(f"live_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
