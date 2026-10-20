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
    probe = ("import json, os, sys; sys.path.insert(0, sys.argv[1]); import live_boundary; root = live_boundary.isolate();"
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
    probe = ("import json, os, subprocess, sys; sys.path.insert(0, sys.argv[1]); import live_boundary; live_boundary.isolate();"
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
            "attempt('kill own group', lambda: run(['kill', '-CONT', '-' + str(own.pid)]), True)\n"
            "attempt('os.killpg own', lambda: os.killpg(own.pid, signal.SIGTERM))\n"
            "own.wait(timeout=10)\n"
            "print(';'.join(out))\n")
    expected = ("sudo refused;wrapped systemctl refused;loginctl in a shell refused;systemctl query ran;"
                "query past an option value ran;fake sudo ran;"
                "fake sudo stop refused;script sudo function ran;script function pkill refused;fake sudo install live refused;pkill refused;killall refused;"
                "kill everything refused;kill foreign group refused;os.kill(-1) raised;os.killpg foreign raised;"
                "os.system sudo refused;grep names ran;kill own group ran;os.killpg own ran")
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
    env.pop(live_boundary.RUN_START_ENV, None)
    # The Python child knows its run by descent; the namespace child by when the run began.
    for label, argv in (("a Python child", [sys.executable, "-c", code]),
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
            "    refused = status == 126 << 8 or (status == 126 and b'test boundary: refused' in done.stderr)\n"
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


def test_expansions(scratch: Path) -> None:
    """A write a script would make through what the shell expands is refused before the shell runs: a variable, an
    exported one, $HOME and XDG paths, a tilde, `${X:-default}`, a command substitution and a backtick (their bodies
    run too), eval, a sourced or executed script file, find -delete, sed -i, compound commands and groups. A target the
    guard cannot resolve -- an unset variable, a positional parameter, a loop variable, stdin -- is refused as well:
    it fails closed. (SYRD-627 Audit.)"""
    import live_boundary

    live, elsewhere = scratch / "expansions-live", scratch / "expansions-elsewhere"
    (live / "home").mkdir(parents=True)
    elsewhere.mkdir()
    (elsewhere / "writes.sh").write_text(f"touch {live}/home/from-a-file\n")
    (elsewhere / "itself.sh").write_text(f". {elsewhere}/itself.sh\n")
    (elsewhere / "library.sh").write_text("never_called() { loginctl enable-linger nobody; }\n"
                                          'write() { touch "$1"; }\n'
                                          f"write_live() {{ touch {live}/home/library; }}\n")
    before = os.lstat(live / "home")
    code = ("import os, subprocess, sys\n"
            "L, E = sys.argv[1] + '/home', sys.argv[2]\n"
            "out = []\n"
            "def attempt(name, script, env=None, cwd=None):\n"
            "    done = subprocess.run(['sh', '-c', script], capture_output=True, timeout=10,\n"
            "                          env=None if env is None else {**os.environ, **env}, cwd=cwd)\n"
            "    refused = done.returncode == 126 and b'test boundary: refused' in done.stderr\n"
            "    out.append(name + (' refused' if refused else ' ran'))\n"
            "attempt('variable', f'P={L}; touch \"$P/variable\"')\n"
            "attempt('exported', f'export P={L}; mkdir \"$P/exported\"')\n"
            "attempt('chown through a variable', f'P={L}; chown -h 0 \"$P\"')\n"
            "attempt('HOME', 'touch \"$HOME/home\"', env={'HOME': L})\n"
            "attempt('XDG', 'mkdir -p \"$XDG_STATE_HOME/x\"', env={'XDG_STATE_HOME': L})\n"
            "attempt('tilde', 'touch ~/tilde', env={'HOME': L})\n"
            "attempt('default', 'touch \"${NOPE_SYRD627:-' + L + '}/default\"')\n"
            "attempt('substitution', f'echo \"$(touch {L}/substitution)\"')\n"
            "attempt('backtick', f'echo \"`touch {L}/backtick`\"')\n"
            "attempt('substitution as the target', f'touch \"$(echo {E})/z\"')\n"
            "attempt('eval', f\"eval 'touch {L}/eval'\")\n"
            "attempt('script file', f'bash {E}/writes.sh')\n"
            "attempt('sourced', f'. {E}/writes.sh')\n"
            "attempt('find -delete', f'find {L} -name x -delete')\n"
            "attempt('sed -i', f'sed -i s/a/b/ {L}/file')\n"
            "attempt('if', f'if true; then touch {L}/if; fi')\n"
            "attempt('case', f'case x in x) touch {L}/case;; esac')\n"
            "attempt('function', f'f() {{ touch {L}/function; }}; f')\n"
            "attempt('cd in a group', f'( cd {L} && touch group )')\n"
            "attempt('expanded heredoc', f'cat > {E}/doc <<EOF\\n$(touch {L}/heredoc)\\nEOF')\n"
            "attempt('unset variable, empty as in the shell', 'touch \"$NOPE_SYRD627/x\"')\n"
            "attempt('positional, known at the call', 'f() { touch \"$1\"; }; f x')\n"
            "attempt('positional from a read', f'f() {{ touch \"$1\"; }}; read y; f \"$y\"')\n"
            "attempt('loop variable', f'for f in {E}/a {L}/b; do touch \"$f\"; done')\n"
            "attempt('read variable', 'read x; touch \"$x\"')\n"
            "attempt('function before its variable', f'f() {{ rm -rf \"$D\"; }}; D={L}; f')\n"
            "attempt('trap', f\"D={L}; trap 'rm -rf \\\"$D\\\"' EXIT\")\n"
            "attempt('trap through a function', f'cleanup() {{ rm -rf \"$D\"; }}; trap cleanup EXIT; D={L}')\n"
            "attempt('library function', f'. {E}/library.sh; write_live')\n"
            "attempt('library wrapper', f'. {E}/library.sh; write {L}/wrapped')\n"
            "attempt('a value set in one branch', f'if x; then D={E}; else D={L}; fi; touch \"$D/q\"')\n"
            "attempt('sudo shim', f'sudo() {{ \"$@\"; }}; sudo touch {L}/shim')\n"
            "attempt('unknown command', '$(echo rm) -rf /x')\n"
            "attempt('stdin', 'echo x | xargs rm')\n"
            "attempt('relative after an unknown cd', 'read d; cd \"$d\"; touch x', cwd=E)\n"
            "attempt('past the depth limit', f'. {E}/itself.sh')\n"
            "attempt('safe variable', f'P={E}; touch \"$P/fine\"')\n"
            "attempt('safe export', f'export P={E}; touch \"$P/fine-export\"')\n"
            "attempt('uncalled library function', f'. {E}/library.sh; echo hi')\n"
            "attempt('library wrapper, safe', f'. {E}/library.sh; write {E}/wrapped')\n"
            "attempt('guarded cleanup', 'cleanup() { if [ -n \"${work_root:-}\" ]; then rm -rf -- \"$work_root\"; fi; }; trap cleanup EXIT')\n"
            "attempt('repository root', 'R=\"$(cd \"$(dirname \"$0\")\" && pwd)\"; touch \"$R/root\"', cwd=E)\n"
            "attempt('mktemp', 'w=$(mktemp -d); mkdir -p \"$w/x\"')\n"
            "attempt('safe default', 'touch \"${NOPE_SYRD627:-' + E + '}/fine-default\"')\n"
            "attempt('quoted heredoc', f\"cat > {E}/doc2 <<'EOF'\\n$(touch {L}/data)\\nEOF\")\n"
            "attempt('single-quoted', f\"echo '$(touch {L}/literal)' > {E}/literal\")\n"
            "attempt('cd scoped to a group', f'( cd {L} ); touch after', cwd=E)\n"
            "print(';'.join(out))\n")
    refused = ("variable", "exported", "chown through a variable", "HOME", "XDG", "tilde", "default", "substitution",
               "backtick", "substitution as the target", "eval", "script file", "sourced", "find -delete", "sed -i", "if",
               "case", "function", "cd in a group", "expanded heredoc", "positional from a read", "loop variable",
               "stdin", "relative after an unknown cd", "past the depth limit", "read variable",
               "function before its variable", "trap", "trap through a function", "library function", "library wrapper",
               "a value set in one branch", "sudo shim", "unknown command")
    ran = ("unset variable, empty as in the shell", "positional, known at the call", "safe variable", "safe export", "uncalled library function", "library wrapper, safe", "guarded cleanup",
           "repository root", "mktemp", "safe default", "quoted heredoc", "single-quoted", "cd scoped to a group")
    expected = {**{name: "refused" for name in refused}, **{name: "ran" for name in ran}}
    env = {**os.environ, live_boundary.LIVE_ROOTS_ENV: str(live), live_boundary.INVOKER_UID_ENV: str(os.getuid()),
           "PYTHONPATH": str(live_boundary.SITE_DIR)}
    for label, argv in (("a Python child", [sys.executable, "-c", code]),
                        ("a namespace child", ["unshare", "--user", "--map-auto", "--map-root-user", sys.executable,
                                               "-c", code])):
        done = subprocess.run([*argv, str(live), str(elsewhere)], env=env, capture_output=True, text=True, timeout=180)
        after = os.lstat(live / "home")
        outcomes = dict(item.rsplit(" ", 1) for item in done.stdout.strip().split(";") if " " in item)
        wrong = {name: outcomes.get(name) for name in expected if outcomes.get(name) != expected[name]}
        check(not wrong and len(outcomes) == len(expected) and sorted(os.listdir(live / "home")) == []
              and (after.st_uid, after.st_gid, after.st_mode) == (before.st_uid, before.st_gid, before.st_mode),
              f"{label}: every expanded write under a live root is refused (126), nothing is created or chowned, and "
              f"what is safe runs: wrong {wrong}, landed {sorted(os.listdir(live / 'home'))} {done.stderr[-800:]}")


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
            test_expansions(scratch)
            test_sentinel_contents(scratch)
        finally:
            subprocess.run(["unshare", "--user", "--map-auto", "--map-root-user", "chown", "-R", "-h", "0:0", str(scratch)],
                           capture_output=True, check=False)
    test_every_namespace_suite_is_isolated()
    print(f"live_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
