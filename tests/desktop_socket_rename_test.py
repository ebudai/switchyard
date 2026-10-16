#!/usr/bin/env python3
"""SYRD-592: a policy pinned to a Wayland socket name survives the compositor moving to another name.

After plasma-login-manager 6.8.0 the greeter took `wayland-0` and the owner's
own KWin moved to `wayland-1`. syrd's and mefp's policies said `wayland-0`, so
their watchers retried an absent socket every five seconds and never granted
anything, and the tenant's launch failed on EACCES for the readiness record.

Real Unix sockets, real filesystem ACLs (setfacl/getfacl) and the real helper,
watcher loop, tenant verifier and launcher pre-launch check. The accounts and
logind are fixtures, as in desktop_access_test: the GUI owner is this uid, the
tenant a numeric uid with no account, and each login's runtime a temporary
directory. Nothing outside the temporary directory is touched.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import desktop_access as desktop
from scripts import team_launcher as launcher

FAILURES = []


def check(label, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + label + ('' if ok else f' -- {detail}'))
    if not ok:
        FAILURES.append(label)


def policy(display, tenant='cerulean-worker', project='cerulean'):
    return desktop.validate_policy({'mode': 'wayland', 'gui_user': 'desktop-owner', 'tenant_user': tenant,
        'project': project, 'wayland_display': display,
        'consent': {'approved': True, 'by': 'desktop-owner', 'at': '2026-09-05T22:27:00Z', 'reference': 'recorded test consent'}},
        project=project, tenant=tenant)


class StopWatching(Exception):
    pass


class Host:
    """One GUI owner's logins: its runtime, its compositor sockets, and whether its session is active."""

    def __init__(self, root):
        self.root = root
        self.gui = SimpleNamespace(pw_name='desktop-owner', pw_uid=os.getuid(), pw_dir=str(root / 'gui-home'))
        self.tenant = SimpleNamespace(pw_name='cerulean-worker', pw_uid=62001, pw_dir=str(root / 'tenant-home'))
        self.runtime = root / 'run-user-gui'
        self.tenant_runtime = root / 'run-user-tenant'
        self.tenant_runtime.mkdir(mode=0o700)
        # The greeter is another account with its own runtime; it holds wayland-0.
        self.greeter_runtime = root / 'run-user-greeter'
        self.greeter_runtime.mkdir(mode=0o700)
        self.servers = {}
        self.greeter = self._listen(self.greeter_runtime / 'wayland-0')
        self.active = False
        self.session_uid = self.gui.pw_uid
        self.accounts = {a.pw_name: a for a in (self.gui, self.tenant)}

    def _listen(self, path):
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        server.listen(8)
        return server

    def login(self, *names):
        """logind creates the runtime; the compositor binds each named socket and its lock file."""
        self.runtime.mkdir(mode=0o700)
        self.active = True
        for name in names:
            self.start(name)

    def start(self, name):
        self.servers[name] = self._listen(self.runtime / name)
        (self.runtime / f'{name}.lock').write_text('')

    def stop(self, name):
        self.servers.pop(name).close()
        (self.runtime / name).unlink()
        (self.runtime / f'{name}.lock').unlink()

    def logout(self):
        """The session ends and logind removes the whole runtime, receipt and ACLs with it."""
        for name in list(self.servers):
            self.servers.pop(name).close()
        self.active = False
        shutil.rmtree(self.runtime)

    def run(self, actual):
        def fixture(args):
            if args[0] == 'loginctl':
                if args[1] == 'show-session':
                    return f"Type=wayland\nActive={'yes' if self.active else 'no'}\nUser={self.session_uid}"
                if 'Sessions' in args:
                    return 'fixture-session'
                return str(self.runtime if args[2] == self.gui.pw_name else self.tenant_runtime)
            if args[0] == 'systemctl':
                return 'active'
            return actual(args)
        return fixture

    def acl(self, path):
        return desktop.acl_map(desktop.read_acl(path)).get(f'user:{self.tenant.pw_uid}')

    def as_tenant(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(desktop.os, 'geteuid', return_value=self.tenant.pw_uid))
        stack.enter_context(patch.object(desktop, 'runtime_for',
            side_effect=lambda user: self.runtime if user == self.gui else self.tenant_runtime))
        return stack

    def accept(self, name):
        """Take the verifier's probe connection off the compositor's queue."""
        self.servers[name].settimeout(2)
        self.servers[name].accept()[0].close()


def watch_once(policy_path):
    """One pass of the owner's watcher service: its real loop, stopped at its first sleep. Returns its stderr."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err), patch.object(desktop.time, 'sleep', side_effect=StopWatching):
        try:
            desktop.main(['watch', str(policy_path)])
        except StopWatching:
            pass
    return err.getvalue()


def verify_error(host, p):
    with host.as_tenant():
        try:
            desktop.verify(p)
        except desktop.DesktopAccessError as exc:
            return str(exc)
    return ''


def launch_error(host, p):
    """The launch's own pre-launch check, its tenant verifier run as `__main__` reports it."""
    raw ={'project': 'cerulean', 'run_as_user': 'cerulean-worker', 'layout': 'layout.json',
           'roles': [{'role': 'main', 'slot': 0, 'cli': ['codex']}], 'desktop_access': p}
    path = host.root / 'cerulean.json'
    path.write_text(json.dumps(raw))
    config = launcher.load_project_config('cerulean', path)

    def runner(args, **kwargs):
        policy_file = args[args.index('verify') + 1]
        out = io.StringIO()
        try:
            with host.as_tenant(), contextlib.redirect_stdout(out):
                desktop.main(['verify', policy_file])
        except (desktop.DesktopAccessError, OSError, ValueError) as exc:
            return subprocess.CompletedProcess(args, 1, '', f'switchyard desktop access: {exc}')
        return subprocess.CompletedProcess(args, 0, out.getvalue(), '')
    try:
        with patch.object(launcher, 'current_user_name', return_value='cerulean-worker'):
            launcher.prepare_project_desktop(config, runner=runner)
    except SystemExit as exc:
        return str(exc)
    return ''


def main():
    for tool in ('setfacl', 'getfacl'):
        assert shutil.which(tool), f'{tool} required; this suite must not silently skip'
    with tempfile.TemporaryDirectory(prefix='desktop-rename-') as tmp:
        root = Path(tmp)
        host = Host(root)
        actual = desktop.run
        with patch.object(desktop.pwd, 'getpwnam', side_effect=lambda name: host.accounts[name]), \
             patch.object(desktop, 'run', side_effect=host.run(actual)):
            pinned = policy('wayland-0')
            name = desktop.names(pinned)[2]
            policy_path = root / 'gui-home' / f'{name}.json'
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text(json.dumps(pinned))
            receipt = host.runtime / f'{name}.json'

            # 1. The reboot after the upgrade: the greeter holds wayland-0 in its own
            #    runtime, the owner's session KWin listens on wayland-1 only.
            host.login('wayland-1')
            log = watch_once(policy_path)
            check('watcher grants the owner\'s only socket when the pinned name is absent',
                  receipt.exists() and host.acl(host.runtime / 'wayland-1') == 'rw-' and host.acl(host.runtime) == '--x',
                  log)
            value = json.loads(receipt.read_text()) if receipt.exists() else {}
            check('readiness receipt names the socket used and the one configured',
                  value.get('socket') == str(host.runtime / 'wayland-1') and value.get('configured_socket') == 'wayland-0', value)
            check('watcher reports the absent configured socket and the one it granted',
                  'configured socket wayland-0 is absent' in log and 'wayland-1' in log and 'pending' not in log, log)
            check('the greeter\'s wayland-0 in another runtime is never granted',
                  host.acl(host.greeter_runtime / 'wayland-0') is None and host.acl(host.greeter_runtime) is None)
            before = receipt.read_bytes() if receipt.exists() else b''
            log = watch_once(policy_path)
            check('a later pass leaves the ready receipt alone and says nothing', receipt.exists()
                  and receipt.read_bytes() == before and log == '', log)
            with host.as_tenant():
                try:
                    env = desktop.verify(pinned)
                except Exception as exc:  # noqa: BLE001 - reported as the check's detail
                    env = {'error': repr(exc)}
            check('tenant verification connects to wayland-1', env.get('WAYLAND_DISPLAY') == str(host.runtime / 'wayland-1'), env)
            if 'error' not in env:
                host.accept('wayland-1')
            check('launch pre-check passes on the moved socket', launch_error(host, pinned) == '')
            if receipt.exists():
                host.accept('wayland-1')

            # 2. Logout: the session ends and logind removes the runtime.
            host.logout()
            log = watch_once(policy_path)
            check('after logout the watcher waits and grants nothing',
                  log.startswith('Wayland access pending:') and not host.runtime.exists(), log)
            error = verify_error(host, pinned)
            check('a launch after logout points at the service journal by unit name',
                  f'journalctl --user -u {name}.service' in error and 'not recorded cerulean-worker as ready' in error, error)

            # 3. The next login: KWin again on wayland-1, a new socket inode.
            host.login('wayland-1')
            log = watch_once(policy_path)
            check('next login: the watcher grants the new wayland-1', receipt.exists()
                  and host.acl(host.runtime / 'wayland-1') == 'rw-' and 'configured socket wayland-0 is absent' in log, log)
            with host.as_tenant():
                try:
                    env = desktop.verify(pinned)
                    host.accept('wayland-1')
                except Exception as exc:  # noqa: BLE001
                    env = {'error': repr(exc)}
            check('next login: tenant verification connects', env.get('WAYLAND_DISPLAY') == str(host.runtime / 'wayland-1'), env)

            # 4. The pinned name comes back beside the one used for it: the pinned
            #    name wins, and the socket used in its absence keeps no grant.
            host.start('wayland-0')
            log = watch_once(policy_path)
            value = json.loads(receipt.read_text())
            check('a pinned name that is present again is the one granted',
                  value.get('socket') == str(host.runtime / 'wayland-0') and host.acl(host.runtime / 'wayland-0') == 'rw-'
                  and 'absent' not in log, (value, log))
            check('the socket granted in its absence loses the tenant\'s grant', host.acl(host.runtime / 'wayland-1') is None)
            host.stop('wayland-0')
            watch_once(policy_path)
            check('back to the only socket when the pinned name goes again', host.acl(host.runtime / 'wayland-1') == 'rw-')

            # 5. Within that login KWin restarts onto two sockets: which one is the
            #    approved desktop is a choice nobody made, so nothing is granted,
            #    and the receipt the tenant can still reach says why.
            host.stop('wayland-1')
            host.start('wayland-2')
            host.start('wayland-3')
            log = watch_once(policy_path)
            check('watcher refuses to choose between several live sockets and names them',
                  'wayland-0 is absent' in log and 'wayland-2, wayland-3' in log and 'will not choose' in log, log)
            check('neither of the several sockets is granted',
                  host.acl(host.runtime / 'wayland-2') is None and host.acl(host.runtime / 'wayland-3') is None)
            value = json.loads(receipt.read_text())
            check('the reachable receipt is marked pending with the reason, keeping its grants',
                  value.get('state') == 'pending' and 'socket' not in value and 'wayland-2, wayland-3' in value.get('reason', '')
                  and any(r['path'] == str(host.runtime) for r in value.get('grants', [])), value)
            error = verify_error(host, pinned)
            check('tenant verification names the absent socket and the live ones',
                  'is waiting: Configured Wayland socket wayland-0 is absent' in error and 'wayland-2, wayland-3' in error, error)
            error = launch_error(host, pinned)
            check('the launch error names the condition',
                  error.startswith('switchyard: desktop setup incomplete before launch:')
                  and 'wayland-0 is absent' in error and 'wayland-2, wayland-3' in error, error)
            kept = (receipt.stat().st_ino, receipt.read_bytes())
            watch_once(policy_path)
            check('a pending pass that changes nothing rewrites nothing', (receipt.stat().st_ino, receipt.read_bytes()) == kept)
            foreign = {**json.loads(kept[1]), 'policy_digest': 'another-policy', 'socket': 'kept'}
            desktop.atomic_json(receipt, foreign, mode=0o644)
            watch_once(policy_path)
            check('a receipt of another policy is not marked pending', json.loads(receipt.read_text()) == foreign)
            receipt.write_bytes(kept[1])
            desktop.rollback(pinned)
            check('rollback from a pending receipt removes the tenant\'s runtime grant',
                  not receipt.exists() and host.acl(host.runtime) is None)

            # 6. A fresh login with several sockets: nothing is granted to report
            #    it, so the launch names the service's journal; and EACCES on the
            #    runtime (what a tenant really sees) gets the same explanation.
            host.logout()
            host.login('wayland-1', 'wayland-2')
            log = watch_once(policy_path)
            check('fresh login with several sockets: no grant and no receipt anywhere',
                  not receipt.exists() and host.acl(host.runtime) is None
                  and host.acl(host.runtime / 'wayland-1') is None and 'wayland-1, wayland-2' in log, log)
            host.runtime.chmod(0)
            try:
                error = verify_error(host, pinned)
            finally:
                host.runtime.chmod(0o700)
            check('EACCES on the runtime is explained, with the unit\'s journal',
                  'Permission denied' in error and f'journalctl --user -u {name}.service' in error, error)

            # 7. The boundary that stays: a pinned name present as a symlink is not
            #    replaced by a fallback, a sole socket of another owner is refused,
            #    and auto behaves as before.
            host.stop('wayland-2')
            (host.runtime / 'wayland-0').symlink_to('wayland-1')
            try:
                desktop.selected_socket(pinned, host.gui, host.runtime)
                outcome = 'accepted'
            except Exception as exc:  # noqa: BLE001 - main raised OSError here
                outcome = str(exc)
            check('a pinned name present as a symlink is refused, not replaced', 'unavailable or owned by another user' in outcome, outcome)
            (host.runtime / 'wayland-0').unlink()
            (host.runtime / 'wayland-0').symlink_to('wayland-gone')
            try:
                desktop.selected_socket(pinned, host.gui, host.runtime)
                outcome = 'accepted'
            except Exception as exc:  # noqa: BLE001 - main raised OSError here
                outcome = str(exc)
            check('a pinned name left as a dangling symlink is refused, not replaced',
                  'unavailable or owned by another user' in outcome, outcome)
            (host.runtime / 'wayland-0').unlink()
            (host.runtime / 'wayland-9').symlink_to('wayland-1')
            try:
                outcome = desktop.selected_socket(pinned, host.gui, host.runtime)
            except Exception as exc:  # noqa: BLE001 - main raised OSError here
                outcome = str(exc)
            check('a symlink beside the only socket is not a second socket', outcome == host.runtime / 'wayland-1', outcome)
            (host.runtime / 'wayland-9').unlink()
            stranger = SimpleNamespace(pw_name='desktop-owner', pw_uid=os.getuid() + 1)
            host.session_uid = stranger.pw_uid
            try:
                desktop.selected_socket(pinned, stranger, host.runtime)
                outcome = 'accepted'
            except Exception as exc:  # noqa: BLE001 - main raised OSError here
                outcome = str(exc)
            host.session_uid = host.gui.pw_uid
            check('the only socket is still refused when another account owns it', 'owned by another user' in outcome, outcome)
            auto = policy('auto')
            chosen = desktop.selected_socket(auto, host.gui, host.runtime)
            host.start('wayland-2')
            try:
                desktop.selected_socket(auto, host.gui, host.runtime)
                outcome = 'accepted'
            except Exception as exc:  # noqa: BLE001 - main raised OSError here
                outcome = str(exc)
            check('auto still takes one socket and refuses two', chosen == host.runtime / 'wayland-1'
                  and 'missing or ambiguous' in outcome, outcome)
            host.logout()
        host.greeter.close()
    print(f'desktop_socket_rename_test: {len(FAILURES)} failed')
    return 1 if FAILURES else 0


if __name__ == '__main__':
    raise SystemExit(main())
