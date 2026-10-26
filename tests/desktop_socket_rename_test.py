#!/usr/bin/env python3
"""SYRD-592: a policy pinned to a Wayland socket name survives the compositor moving to another name.

After plasma-login-manager 6.8.0 the greeter took `wayland-0` and the owner's
own KWin moved to `wayland-1`. syrd's and mefp's policies said `wayland-0`, so
their watchers retried an absent socket every five seconds and never granted
anything, and the tenant's launch failed on EACCES for the readiness record.

Since SYRD-687 the tenant reaches the compositor through its grant's relay, so
the socket the owner's service selects is the relay's upstream, and the
tenant's WAYLAND_DISPLAY -- the relay -- never changes with it.

Real Unix sockets answering the Wayland handshake (`wayland_fixture`), the real
relay, its service passes, receipt, tenant verifier and launcher pre-launch
check. The accounts and logind are fixtures: the GUI owner and the tenant are
this uid -- the relay serves only its tenant's uid -- and each login's runtime
and the exposure root are temporary directories. Nothing outside them is
touched.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from scripts import desktop_access as desktop
from scripts import team_launcher as launcher
from wayland_fixture import FakeCompositor

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


class Host:
    """One GUI owner's logins: its runtime, its compositor sockets, and whether its session is active."""

    def __init__(self, root):
        self.root = root
        me = os.getuid()
        self.gui = SimpleNamespace(pw_name='desktop-owner', pw_uid=me, pw_gid=os.getgid(), pw_dir=str(root / 'gui-home'))
        self.tenant = SimpleNamespace(pw_name='cerulean-worker', pw_uid=me, pw_gid=os.getgid(), pw_dir=str(root / 'tenant-home'))
        self.runtime = root / 'run-user-gui'
        self.tenant_runtime = root / 'run-user-tenant'
        self.tenant_runtime.mkdir(mode=0o700)
        # The greeter is another account with its own runtime; it holds wayland-0.
        self.greeter_runtime = root / 'run-user-greeter'
        self.greeter_runtime.mkdir(mode=0o700)
        self.greeter = FakeCompositor(self.greeter_runtime / 'wayland-0')
        self.servers = {}
        self.active = False
        self.session_uid = self.gui.pw_uid
        self.accounts = {a.pw_name: a for a in (self.gui, self.tenant)}

    def login(self, *names):
        """logind creates the runtime; the compositor binds each named socket and its lock file."""
        self.runtime.mkdir(mode=0o700)
        self.active = True
        for name in names:
            self.start(name)

    def start(self, name):
        self.servers[name] = FakeCompositor(self.runtime / name)
        (self.runtime / f'{name}.lock').write_text('')

    def stop(self, name):
        self.servers.pop(name).close()
        (self.runtime / f'{name}.lock').unlink()

    def logout(self):
        """The session ends and logind removes the whole runtime."""
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

    def runtime_entries(self):
        """Every named entry on the owner's runtime directory: there must never be one."""
        return [k for k in desktop.acl_map(desktop.read_acl(self.runtime)) if k.split(':')[1]]


def service_pass(p, relay, leaf):
    """One pass of the owner's service, as `watch` runs it. Returns its stderr."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        desktop.refresh(p, relay, leaf)
    return err.getvalue()


def verify_error(p):
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
            with contextlib.redirect_stdout(out):
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
        trust = root / 'trust'
        trust.mkdir(mode=0o755)
        with patch.object(desktop.pwd, 'getpwnam', side_effect=lambda name: host.accounts[name]), \
             patch.object(desktop, 'run', side_effect=host.run(actual)), \
             patch.object(desktop, 'TRUST', (str(trust), os.getuid())):
            pinned = policy('wayland-0')
            name = desktop.names(pinned)[2]
            leaf = desktop.exposure_dir(host.gui, name, create=True)
            leaf = desktop.expose(pinned)
            relay = desktop.WaylandRelay(pinned, leaf, log=lambda _message: None)
            receipt = leaf / desktop.EXPOSURE_RECEIPT
            display = str(leaf / desktop.EXPOSURE_SOCKET)

            # 1. The reboot after the upgrade: the greeter holds wayland-0 in its own
            #    runtime, the owner's session KWin listens on wayland-1 only.
            host.login('wayland-1')
            log = service_pass(pinned, relay, leaf)
            value = json.loads(receipt.read_text()) if receipt.exists() else {}
            check('the service relays to the owner\'s only socket when the pinned name is absent',
                  value.get('state') == 'ready' and value.get('upstream') == 'wayland-1' and relay.upstream == host.runtime / 'wayland-1',
                  (value, log))
            check('the service reports the absent configured socket and the one it relays to',
                  'configured socket wayland-0 is absent' in log and 'wayland-1' in log and 'pending' not in log, log)
            check('the owner\'s runtime directory carries no entry for anyone', host.runtime_entries() == [],
                  host.runtime_entries())
            before = receipt.read_bytes()
            log = service_pass(pinned, relay, leaf)
            check('a later pass leaves the ready receipt alone and says nothing', receipt.read_bytes() == before and log == '', log)
            try:
                env = desktop.verify(pinned)
            except Exception as exc:  # noqa: BLE001 - reported as the check's detail
                env = {'error': repr(exc)}
            check('tenant verification is given the relay and reaches wayland-1 through it',
                  env.get('WAYLAND_DISPLAY') == display and host.servers['wayland-1'].peers, (env, host.servers['wayland-1'].peers))
            check('the greeter\'s wayland-0 in another runtime is never reached', host.greeter.peers == [], host.greeter.peers)
            check('launch pre-check passes on the moved socket', launch_error(host, pinned) == '')

            # 2. Logout: the session ends and logind removes the runtime.
            host.logout()
            log = service_pass(pinned, relay, leaf)
            check('after logout the service waits and says so', log.startswith('Wayland access pending:'), log)
            value = json.loads(receipt.read_text())
            check('the receipt is pending, with the reason', value.get('state') == 'pending' and 'it exists while desktop-owner is logged in'
                  in value.get('reason', ''), value)
            error = verify_error(pinned)
            check('a launch after logout quotes why the service waits',
                  'is waiting' in error and 'it exists while desktop-owner is logged in' in error, error)

            # 3. The next login: KWin again on wayland-1, a new socket inode.
            host.login('wayland-1')
            log = service_pass(pinned, relay, leaf)
            check('next login: the relay follows the new wayland-1', relay.upstream == host.runtime / 'wayland-1'
                  and json.loads(receipt.read_text()).get('state') == 'ready' and 'configured socket wayland-0 is absent' in log, log)
            try:
                env = desktop.verify(pinned)
            except Exception as exc:  # noqa: BLE001
                env = {'error': repr(exc)}
            check('next login: tenant verification connects, through the same relay', env.get('WAYLAND_DISPLAY') == display, env)
            check('next login: the new runtime has no entry either', host.runtime_entries() == [], host.runtime_entries())

            # 4. The pinned name comes back beside the one used for it: the pinned
            #    name wins, and the socket used in its absence is not used.
            host.start('wayland-0')
            log = service_pass(pinned, relay, leaf)
            value = json.loads(receipt.read_text())
            check('a pinned name that is present again is the one relayed to',
                  value.get('upstream') == 'wayland-0' and relay.upstream == host.runtime / 'wayland-0' and 'absent' not in log,
                  (value, log))
            seen = len(host.servers['wayland-1'].peers)
            desktop.wayland_roundtrip(display)
            check('a connection now reaches wayland-0, not the socket used in its absence',
                  len(host.servers['wayland-1'].peers) == seen and host.servers['wayland-0'].peers, seen)
            host.stop('wayland-0')
            service_pass(pinned, relay, leaf)
            check('back to the only socket when the pinned name goes again', relay.upstream == host.runtime / 'wayland-1')

            # 5. Within that login KWin restarts onto two sockets: which one is the
            #    approved desktop is a choice nobody made, so nothing is relayed,
            #    and the receipt says why.
            host.stop('wayland-1')
            host.start('wayland-2')
            host.start('wayland-3')
            log = service_pass(pinned, relay, leaf)
            check('the service refuses to choose between several live sockets and names them',
                  'wayland-0 is absent' in log and 'wayland-2, wayland-3' in log and 'will not choose' in log, log)
            try:
                desktop.wayland_roundtrip(display)
                outcome = 'answered'
            except (desktop.DesktopAccessError, OSError) as exc:
                outcome = str(exc)
            check('the relay answers nobody while it has no socket to relay to',
                  relay.upstream is None and outcome != 'answered'
                  and not host.servers['wayland-2'].peers and not host.servers['wayland-3'].peers, outcome)
            value = json.loads(receipt.read_text())
            check('the receipt is marked pending with the reason',
                  value.get('state') == 'pending' and 'upstream' not in value and 'wayland-2, wayland-3' in value.get('reason', ''), value)
            error = verify_error(pinned)
            check('tenant verification names the absent socket and the live ones',
                  'is waiting: Configured Wayland socket wayland-0 is absent' in error and 'wayland-2, wayland-3' in error, error)
            error = launch_error(host, pinned)
            check('the launch error names the condition',
                  error.startswith('switchyard: desktop setup incomplete before launch:')
                  and 'wayland-0 is absent' in error and 'wayland-2, wayland-3' in error, error)
            kept = (receipt.stat().st_ino, receipt.read_bytes())
            service_pass(pinned, relay, leaf)
            check('a pending pass that changes nothing rewrites nothing', (receipt.stat().st_ino, receipt.read_bytes()) == kept)
            relay.close()
            desktop.revoke_exposure(pinned)
            check('withdrawing the grant leaves its exposure with no socket, receipt or tenant entry',
                  sorted(os.listdir(leaf)) == [] and not any(k.startswith('user:') and k != 'user:'
                                                             for k in desktop.acl_map(desktop.read_acl(leaf))),
                  (os.listdir(leaf), desktop.read_acl(leaf)))
            error = verify_error(pinned)
            check('and a launch then reports that nothing is ready', 'has not recorded' in error, error)

            # 6. The boundary that stays: a pinned name present as a symlink is not
            #    replaced by a fallback, a sole socket of another owner is refused,
            #    and auto behaves as before.
            host.logout()
            host.login('wayland-1')
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
