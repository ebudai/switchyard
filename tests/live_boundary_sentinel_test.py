#!/usr/bin/env python3
"""SYRD-627: a run behind the test boundary leaves every account's tmux servers alive -- its own and others'.

On 2026-10-18 every SYRD and MEFP role server exited while a broad comparison
was running. The comparison started each suite with no TMUX and no TMUX_TMPDIR,
so a bare tmux command reached the invoking account's default socket: its live
server. That explains SYRD's loss as a mechanism. MEFP's servers belong to
another account, which nothing in a test could reach without a broader kill.

Everything here runs in a sandbox: `unshare --user --map-auto --map-root-user
--mount --pid`, with private tmpfs mounts on /tmp and /run, the invoking home
read-only, and passwd entries that exist only in the sandbox. Three sentinel
tmux servers, each with two sessions on its own default socket, stand for:
- the runner's own account (UID 1600), as switchyard-agent's server is SYRD's;
- two other accounts (UIDs 1501 and 1502), as stellaris-agent's is MEFP's.

Each check runs as the runner in the old sweep's environment (`env -i`, no
TMUX, no TMUX_TMPDIR) against a clone of this tree's committed HEAD, and then
asks whether every sentinel server and session survived:

* **The mechanism, without the boundary:** a bare `tmux kill-server` loses the
  runner's own sentinel and only that one; so does an unprivileged
  `pkill -f '^tmux'`. The same pkill with privilege over every account
  (namespace root) loses all three. These are the controls: the harness sees
  both a same-account and a cross-account kill, and a cross-account kill needs
  privilege -- which is why the boundary refuses the privileged paths.
* **The same commands behind `live_boundary.isolate()`:** the run's tmux is a
  disposable server of its own, and pkill, `kill -- -1` and tmux on the
  account's own socket are refused. Every sentinel survives.
* **runtime_switch_stall_test.py,** the suite that was running when the servers
  exited, behind the boundary: it passes, and every sentinel survives.
  SYRD627_SKIP_SUITE=1 skips this one (about ten minutes).
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER, OTHERS = 1600, (1501, 1502)
CHECKS = 0

SANDBOX = r'''
set -u
out=$1; repo=$2; commit=$3; runner=$4; shift 4
home=$INVOKER_HOME
mount -t tmpfs tmpfs /run && mkdir -p /run/out && mount --bind "$out" /run/out
mount -t tmpfs tmpfs /tmp && chmod 1777 /tmp
git clone -q --no-checkout "$repo" /tmp/tree && git -C /tmp/tree -c advice.detachedHead=false checkout -q "$commit" || exit 4
mount --bind "$home" "$home" && mount -o remount,bind,ro "$home"
cp /etc/passwd /run/passwd && cp /etc/group /run/group
for uid in "$runner" 1501 1502; do
  echo "sandbox$uid:x:$uid:$uid::/tmp/runner-home:/bin/sh" >> /run/passwd; echo "sandbox$uid:x:$uid:" >> /run/group
done
mount --bind /run/passwd /etc/passwd && mount --bind /run/group /etc/group
mkdir -p /tmp/runner-home /tmp/stubs && cp -r /run/out/stubs/. /tmp/stubs/ 2>/dev/null
chown -R "$runner:$runner" /tmp/tree /tmp/runner-home /tmp/stubs
: > /run/out/sentinels
for uid in "$runner" 1501 1502; do
  for s in a b; do
    setpriv --reuid="$uid" --regid="$uid" --clear-groups env -i PATH=/usr/bin:/bin HOME=/tmp \
      tmux new-session -d -s "sentinel-$uid-$s" "sleep 3600"
  done
  echo "$uid $(tmux -S /tmp/tmux-$uid/default display-message -p '#{pid}')" >> /run/out/sentinels
done
cd /tmp/tree
as=${RUN_AS:-$runner}
setpriv --reuid="$as" --regid="$as" --clear-groups env -i PATH=/tmp/stubs:/usr/bin:/bin \
  HOME=/tmp/runner-home LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 timeout 1100 "$@" > /run/out/command.out 2>&1 < /dev/null
echo "$?" > /run/out/rc
: > /run/out/verdict
while read -r uid pid; do
  sessions=$(tmux -S "/tmp/tmux-$uid/default" ls -F '#S' 2>/dev/null | tr '\n' ' ')
  alive=no; kill -0 "$pid" 2>/dev/null && alive=yes
  echo "$uid $alive $sessions" >> /run/out/verdict
done < /run/out/sentinels
'''


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def sandboxed(scratch: Path, label: str, command: list[str], *, run_as: int = RUNNER) -> dict:
    """Run `command` as `run_as` (the runner by default) in the sandbox; report its status, output and each
    sentinel's fate."""
    out = scratch / label
    (out / "stubs").mkdir(parents=True)
    script = scratch / "sandbox.sh"
    script.write_text(SANDBOX)
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                            check=True).stdout.strip()
    env = {**os.environ, "INVOKER_HOME": pwd.getpwuid(os.getuid()).pw_dir, "RUN_AS": str(run_as)}
    done = subprocess.run(["unshare", "--user", "--map-auto", "--map-root-user", "--mount", "--pid", "--fork",
                           "--mount-proc", "bash", str(script), str(out), str(ROOT), commit, str(RUNNER), *command],
                          env=env, capture_output=True, text=True, timeout=1300)
    check(done.returncode == 0 and (out / "verdict").exists(), f"{label}: the sandbox ran: {done.stderr[-800:]}")
    fate = {}
    for line in (out / "verdict").read_text().splitlines():
        uid, alive, *sessions = line.split()
        fate[int(uid)] = alive == "yes" and {f"sentinel-{uid}-a", f"sentinel-{uid}-b"} <= set(sessions)
    return {"rc": int((out / "rc").read_text()), "out": (out / "command.out").read_text(errors="replace"),
            "fate": fate}


ISOLATED = ("import os, subprocess, sys; sys.path.insert(0, 'tests'); import live_boundary; live_boundary.isolate()\n"
            "out = []\n"
            "for name, argv in (('bare kill-server', ['tmux', 'kill-server']),\n"
            "                   ('own socket', ['tmux', '-S', f'/tmp/tmux-{os.getuid()}/default', 'kill-server']),\n"
            "                   ('pkill', ['pkill', '-f', '^tmux']),\n"
            "                   ('kill everything', ['kill', '-TERM', '--', '-1']),\n"
            "                   ('shell', ['sh', '-c', 'tmux kill-server; pkill -f ^tmux'])):\n"
            "    done = subprocess.run(argv, capture_output=True, timeout=20)\n"
            "    refused = done.returncode == 126 and b'test boundary: refused' in done.stderr\n"
            "    out.append(name + (' refused' if refused else ' ran'))\n"
            "try:\n"
            "    os.kill(-1, 15)\n"
            "    out.append('os.kill(-1) ran')\n"
            "except PermissionError:\n"
            "    out.append('os.kill(-1) refused')\n"
            "print(';'.join(out))\n")


def main() -> int:
    for tool in ("unshare", "setpriv", "tmux", "git"):
        check(shutil.which(tool), f"{tool} is required; this suite must not silently skip")
    with tempfile.TemporaryDirectory(prefix="syrd627s.") as raw:
        scratch = Path(raw)
        everyone = (RUNNER, *OTHERS)
        bare = sandboxed(scratch, "bare", ["tmux", "kill-server"])
        check(bare["fate"] == {RUNNER: False, OTHERS[0]: True, OTHERS[1]: True},
              f"without the boundary, a bare tmux kill-server loses the runner's own account's server and no other: "
              f"{bare['fate']}")
        own = sandboxed(scratch, "pkill", ["pkill", "-f", "^tmux"])
        check(own["fate"] == {RUNNER: False, OTHERS[0]: True, OTHERS[1]: True},
              f"a kill by name, unprivileged, still reaches only the runner's own account: {own['fate']}")
        broad = sandboxed(scratch, "pkill-privileged", ["pkill", "-f", "^tmux"], run_as=0)
        check(broad["fate"] == {uid: False for uid in everyone},
              f"the same kill with privilege over every account (namespace root, as a privileged path would have) "
              f"loses them all: the harness sees a cross-account kill: {broad['fate']}")
        guarded = sandboxed(scratch, "guarded", ["python3", "-c", ISOLATED])
        check(guarded["out"].strip().splitlines()[-1:] == [
            "bare kill-server ran;own socket refused;pkill refused;kill everything refused;shell refused;"
            "os.kill(-1) refused"], f"behind the boundary the bare tmux is the run's own, and every other kill is "
                                    f"refused: {guarded['out'][-1500:]}")
        check(guarded["fate"] == {uid: True for uid in everyone},
              f"and every account's sentinel server and sessions survive: {guarded['fate']}")
        if not os.environ.get("SYRD627_SKIP_SUITE"):
            suite = sandboxed(scratch, "suite", ["python3", "tests/runtime_switch_stall_test.py"])
            check(suite["rc"] == 0 and "checks ok" in suite["out"].strip().splitlines()[-1],
                  f"runtime_switch_stall_test passes in the sandbox: {suite['out'][-1500:]}")
            check(suite["fate"] == {uid: True for uid in everyone},
                  f"and every account's sentinel server and sessions survive it: {suite['fate']}")
    print(f"live_boundary_sentinel_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
