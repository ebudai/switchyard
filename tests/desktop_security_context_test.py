#!/usr/bin/env python3
"""SYRD-791: a tenant's desktop access is a security-context socket, never the desktop user's own Wayland socket.

On KWin 6.8.2 (measured in a disposable VM) a client of the desktop user's raw socket is offered every global the
desktop user's own apps are: an agent account with desktop access's ACL listed every window with its title and typed
into the focused window. Through a `wp_security_context_v1` socket the desktop user opened, KWin withheld those.

Real files, sockets and ACLs (setfacl/getfacl); the compositor is `fake_wayland_compositor`, which speaks the Wayland
wire protocol and implements `create_listener` for real; logind, systemd and accounts are fixtures, as in
desktop_access_test. Nothing outside the temporary directory is touched.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / 'tests'):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
from scripts import desktop_access as desktop  # noqa: E402
from fake_wayland_compositor import FakeCompositor, PRIVILEGED, globals_seen  # noqa: E402

CHECKS = 0


def check(condition, detail):
    global CHECKS
    assert condition, detail
    CHECKS += 1


def policy(tenant='cerulean-worker', project='cerulean'):
    return desktop.validate_policy({'mode': 'wayland', 'gui_user': 'desktop-owner', 'tenant_user': tenant,
        'project': project, 'wayland_display': 'auto',
        'consent': {'approved': True, 'by': 'desktop-owner', 'at': '2026-09-05T22:27:00Z', 'reference': 'test consent'}},
        project=project, tenant=tenant)


class Host:
    def __init__(self, root: Path, *, security_context=True):
        self.root = root
        self.gui = SimpleNamespace(pw_name='desktop-owner', pw_uid=os.getuid(), pw_dir=str(root / 'gui-home'))
        self.tenant = SimpleNamespace(pw_name='cerulean-worker', pw_uid=62001, pw_dir=str(root / 'tenant-home'))
        self.second = SimpleNamespace(pw_name='amber-worker', pw_uid=62002, pw_dir=str(root / 'second-home'))
        self.runtime = root / 'gui-runtime'
        self.runtime.mkdir(mode=0o700)
        self.tenant_runtime = root / 'tenant-runtime'
        self.tenant_runtime.mkdir(mode=0o700)
        # Root's /run/switchyard-desktop, here this uid's: the grant directory is the only thing the tenant traverses.
        self.base = root / 'sd'
        self.grant = self.base / str(self.gui.pw_uid)
        self.tmpfiles = root / 'tmpfiles.d'
        self.compositor_path = self.runtime / 'wayland-7'
        self.compositor = FakeCompositor(self.compositor_path, security_context=security_context)
        self.accounts = {a.pw_name: a for a in (self.gui, self.tenant, self.second)}
        self.uids = {a.pw_uid for a in self.accounts.values()}
        self.systemctl: list[list[str]] = []
        self.on_enable = None

    def run(self, actual):
        def fixture(args):
            if args[0] == 'loginctl':
                if args[1] == 'show-session':
                    return f'Type=wayland\nActive=yes\nUser={self.gui.pw_uid}'
                if 'Sessions' in args:
                    return 'fixture-session'
                return str(self.runtime if args[2] == self.gui.pw_name else self.tenant_runtime)
            if args[0] == 'systemctl':
                self.systemctl.append(args)
                if ('enable' in args or 'restart' in args) and self.on_enable:
                    self.on_enable()
                return 'active'
            return actual(args)
        return fixture

    @contextlib.contextmanager
    def fixtures(self):
        actual = desktop.run
        real_subprocess = subprocess.run

        def quiet_systemctl(args, **kwargs):
            if args[0] == 'systemctl':
                self.systemctl.append(list(args))
                return subprocess.CompletedProcess(args, 0, '', '')
            return real_subprocess(args, **kwargs)
        with patch.object(desktop.pwd, 'getpwnam', side_effect=lambda name: self.accounts[name]), \
             patch.object(desktop, 'account_exists', side_effect=lambda uid: uid in self.uids), \
             patch.object(desktop, 'run', side_effect=self.run(actual)), \
             patch.object(desktop.subprocess, 'run', side_effect=quiet_systemctl), \
             patch.object(desktop, 'DESKTOP_BASE', self.base), patch.object(desktop, 'BASE_OWNER_UID', os.getuid()), \
             patch.object(desktop, 'TMPFILES_DIR', self.tmpfiles):
            desktop.ensure_grant_dir(self.gui)  # what root's install does
            try:
                yield
            finally:
                for name in list(desktop.HELD_CONTEXTS):
                    desktop.release_context(name)

    @contextlib.contextmanager
    def as_tenant(self):
        with patch.object(desktop.os, 'geteuid', return_value=self.tenant.pw_uid), \
             patch.object(desktop, 'runtime_for', side_effect=lambda u: self.runtime if u == self.gui else self.tenant_runtime):
            yield

    def acl(self, path, who=None):
        return desktop.acl_map(desktop.read_acl(path)).get(f'user:{(who or self.tenant).pw_uid}')


def host():
    raw = tempfile.TemporaryDirectory(prefix='syrd791.')
    return raw, Host(Path(raw.name))


def test_the_wire_client_lists_registers_and_reports_refusals() -> None:
    raw, h = host()
    with raw:
        check(desktop.offers_security_context(h.compositor_path), 'a compositor offering the manager is seen to')
        bare = FakeCompositor(h.runtime / 'wayland-8', security_context=False)
        check(not desktop.offers_security_context(h.runtime / 'wayland-8'), 'and one that does not, not')
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(h.root / 'ctx'))
        listener.listen(4)
        read_end, write_end = os.pipe()
        desktop.register_security_context(h.compositor_path, listener.fileno(), read_end, app_id='a1', instance_id='p1')
        listener.close()
        os.close(read_end)
        context = h.compositor.contexts[-1]
        check(context.committed and context.metadata == {'engine': 'org.switchyard.desktop-access', 'app_id': 'a1',
                                                         'instance_id': 'p1'}, f'registered and committed: {context.metadata}')
        seen = globals_seen(h.root / 'ctx')
        check(seen and not set(seen) & set(PRIVILEGED), f'a client of the context sees no privileged global: {seen}')
        check(set(PRIVILEGED) <= set(globals_seen(h.compositor_path)), 'while the compositor socket offers them all')
        os.close(write_end)
        check(context.closed.wait(2), 'the compositor stops listening once the close fd hangs up')
        refuser = FakeCompositor(h.runtime / 'wayland-9', refuse_create=True)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(h.root / 'ctx2'))
        listener.listen(1)
        read_end, write_end = os.pipe()
        try:
            desktop.register_security_context(h.runtime / 'wayland-9', listener.fileno(), read_end, app_id='a', instance_id='b')
            outcome = 'accepted'
        except desktop.WaylandProtocolError as exc:
            outcome = str(exc)
        check('refused by the fixture' in outcome, f"a compositor's refusal is raised with its words: {outcome}")
        try:
            desktop.register_security_context(h.runtime / 'wayland-8', listener.fileno(), read_end, app_id='a', instance_id='b')
            outcome = 'accepted'
        except desktop.DesktopAccessError as exc:
            outcome = str(exc)
        check('offers no wp_security_context_manager_v1' in outcome and 'grants nothing' in outcome,
              f'a compositor without the manager is refused, never used directly: {outcome}')
        for fd in (read_end, write_end):
            os.close(fd)
        listener.close()
        for c in (bare, refuser, h.compositor):
            c.close()


def test_the_tenant_is_granted_its_sandboxed_socket_and_never_the_compositors() -> None:
    raw, h = host()
    with raw, h.fixtures():
        p = policy()
        name = desktop.names(p)[2]
        ready = desktop.apply(p)
        context = desktop.context_socket(h.grant, name)
        info = context.lstat()
        entries = desktop.acl_map(desktop.read_acl(context))
        check(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o007 == 0
              and entries.get('group:') == '---' and set(entries) == {'user:', 'group:', 'mask:', 'other:', 'user:62001'},
              f"the sandboxed socket is the desktop user's, with no access but the tenant's: {oct(info.st_mode)} {entries}")
        check(h.acl(context) == 'rw-' and h.acl(h.grant) == '--x' and h.acl(h.compositor_path) is None,
              'rw on the sandboxed socket, search on the grant directory, nothing on the compositor socket')
        check(h.acl(h.runtime) is None and context.parent == h.grant,
              "and no entry at all on the desktop user's runtime, whose bus and PipeWire it would reach (Audit, round 2)")
        value = json.loads(ready.read_text())
        check(value['socket'] == str(context) and value['compositor_socket'] == str(h.compositor_path)
              and value['security_context'] == {'engine': 'org.switchyard.desktop-access', 'app_id': 'cerulean-worker',
                                                'instance_id': 'cerulean'}, f'the receipt says which is which: {value}')
        with h.as_tenant():
            env = desktop.verify(p)
        check(env['WAYLAND_DISPLAY'] == str(context), f"the tenant's WAYLAND_DISPLAY is the sandboxed socket: {env}")
        seen = globals_seen(Path(env['WAYLAND_DISPLAY']))
        check(seen and not set(seen) & set(PRIVILEGED),
              f'and through it no window list, typing, capture, clipboard or context manager: {seen}')
        before = ready.read_bytes()
        desktop.apply(p)
        check(ready.read_bytes() == before and len(h.compositor.contexts) == 1,
              f'a later pass leaves the receipt and its context alone: {len(h.compositor.contexts)} contexts')
        # The compositor restarts: a new context on the new socket, and the old receipt no longer authorizes.
        h.compositor.close()
        h.compositor = FakeCompositor(h.compositor_path)
        with h.as_tenant():
            try:
                desktop.verify(p)
                outcome = 'accepted'
            except desktop.DesktopAccessError as exc:
                outcome = str(exc)
        check(outcome != 'accepted', f'a receipt whose compositor is gone does not verify: {outcome}')
        desktop.apply(p)
        check(len(h.compositor.contexts) == 1 and h.acl(context) == 'rw-' and h.acl(h.compositor_path) is None,
              'the next pass registers again on the new compositor, still granting only the sandboxed socket')
        with h.as_tenant():
            check(desktop.verify(p)['WAYLAND_DISPLAY'] == str(context), 'and the tenant verifies again')
        old = h.compositor.contexts[-1]
        desktop.rollback(p)
        check(old.closed.wait(2) and not context.exists() and h.acl(h.grant) is None and h.acl(h.runtime) is None,
              'rollback closes the context, removes its socket and the grant-directory entry')
        h.compositor.close()


def test_a_grant_from_before_syrd_791_is_not_used_and_is_taken_back() -> None:
    raw, h = host()
    with raw, h.fixtures():
        p = policy()
        name = desktop.names(p)[2]
        ready = h.runtime / f'{name}.json'
        # What the old helper left: the compositor's own socket granted, and named by the receipt.
        grants = [desktop.grant(h.runtime, h.tenant.pw_uid, '--x'), desktop.grant(h.compositor_path, h.tenant.pw_uid, 'rw-')]
        info = h.compositor_path.stat()
        desktop.atomic_json(ready, {'policy_digest': desktop.digest(p), 'socket': str(h.compositor_path),
                                    'socket_identity': [info.st_dev, info.st_ino, info.st_ctime_ns],
                                    'configured_socket': 'auto', 'gui_uid': h.gui.pw_uid, 'tenant_uid': h.tenant.pw_uid,
                                    'grants': grants}, mode=0o644)
        with h.as_tenant():
            try:
                desktop.verify(p)
                outcome = 'accepted'
            except desktop.DesktopAccessError as exc:
                outcome = str(exc)
        check(outcome.startswith('cerulean-worker has no desktop socket') and 'switchyard upgrade cerulean' in outcome,
              f'a launch never reads the old grant in the runtime: no desktop socket, said plainly: {outcome}')
        # A forged receipt in the grant directory naming a compositor socket is refused too.
        desktop.atomic_json(h.grant / f'{name}.json', {'policy_digest': desktop.digest(p), 'socket': str(h.grant / 'wayland-7'),
                            'socket_identity': [0, 0, 0], 'grants': []}, mode=0o644)
        with h.as_tenant():
            try:
                desktop.verify(p)
                outcome = 'accepted'
            except desktop.DesktopAccessError as exc:
                outcome = str(exc)
        check("names desktop-owner's own Wayland socket (wayland-7)" in outcome, f'a receipt naming a wayland socket is refused: {outcome}')
        (h.grant / f'{name}.json').unlink()
        desktop.apply(p)
        check(h.acl(h.compositor_path) is None and h.acl(h.runtime) is None and not ready.exists()
              and h.acl(desktop.context_socket(h.grant, name)) == 'rw-',
              "the next pass takes the runtime's and the compositor socket's grants back, drops the old receipt, and "
              'grants the sandboxed socket')
        h.compositor.close()


def test_no_sandboxing_compositor_means_no_grant_at_all() -> None:
    raw = tempfile.TemporaryDirectory(prefix='syrd791.')
    with raw:
        h = Host(Path(raw.name), security_context=False)
        with h.fixtures():
            p = policy()
            name = desktop.names(p)[2]
            try:
                desktop.apply(p)
                outcome = 'granted'
            except desktop.DesktopAccessError as exc:
                outcome = str(exc)
            check('offers no wp_security_context_manager_v1' in outcome, f'apply refuses and says why: {outcome}')
            check(h.acl(h.compositor_path) is None and h.acl(h.runtime) is None and h.acl(h.grant) is None
                  and not desktop.context_socket(h.grant, name).exists(),
                  'nothing is granted, and no socket is left behind')
            payload = {'policy': p, 'helper': (ROOT / 'scripts/desktop_access.py').read_text(), 'python': sys.executable}
            try:
                desktop.install_owner(payload)
                outcome = 'installed'
            except desktop.DesktopAccessError as exc:
                outcome = str(exc)
            installed = Path(h.gui.pw_dir) / '.config/switchyard/desktop-access' / f'{name}.json'
            check('desktop access is not installed' in outcome and 'Choose headless' in outcome and not installed.exists()
                  and not (Path(h.gui.pw_dir) / '.config/systemd/user' / f'{name}.service').exists(),
                  f'install refuses before writing anything: {outcome}')
        h.compositor.close()


def test_no_grant_outlives_its_account() -> None:
    raw, h = host()
    with raw, h.fixtures():
        p = policy()
        gone = 62099
        for path in (h.runtime, h.compositor_path, h.grant):
            desktop.run(['setfacl', '-m', f'u:{gone}:r-x', str(path)])
        desktop.grant(h.grant, h.second.pw_uid, '--x')
        desktop.apply(p)
        check(all(f'user:{gone}' not in desktop.acl_map(desktop.read_acl(path)) for path in (h.runtime, h.compositor_path, h.grant)),
              'entries for a uid with no account are removed from the runtime, the compositor socket and the grant directory')
        check(h.acl(h.grant, h.second) == '--x', "an existing account's entry is left alone")
        # A deleted tenant's whole install: service, policy, helper, receipt and socket.
        orphan = f'switchyard-desktop-{h.gui.pw_uid}-{gone}-oldproject'
        kept = f'switchyard-desktop-{h.gui.pw_uid}-{h.second.pw_uid}-amber'
        files = {}
        for key in (orphan, kept):
            files[key] = [Path(h.gui.pw_dir) / '.config/systemd/user' / f'{key}.service',
                          Path(h.gui.pw_dir) / '.config/switchyard/desktop-access' / f'{key}.json',
                          Path(h.gui.pw_dir) / '.local/lib/switchyard/desktop-access' / f'{key}.py',
                          h.grant / f'{key}.json']
            for path in files[key]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(desktop.context_socket(h.grant, key)))
            sock.close()
        h.systemctl.clear()
        removed = desktop.remove_orphan_installs(h.gui, h.grant, h.runtime)
        check(removed == [orphan] and not any(p.exists() for p in files[orphan])
              and not desktop.context_socket(h.grant, orphan).exists(),
              f"a deleted tenant's service, files, receipt and socket are removed: {removed}")
        check(all(p.exists() for p in files[kept]) and desktop.context_socket(h.grant, kept).exists(),
              "an existing tenant's install is left alone")
        check(['systemctl', '--user', 'disable', '--now', f'{orphan}.service'] in h.systemctl
              and not any(f'{kept}.service' in c for c in h.systemctl), f'only the orphan is stopped: {h.systemctl}')
        h.compositor.close()


def test_install_waits_for_the_service_to_hold_the_context() -> None:
    raw, h = host()
    with raw, h.fixtures():
        p = policy()
        name = desktop.names(p)[2]
        started = []

        def service_starts():  # the owner's service: its first pass, a moment after systemd starts it
            def first_pass():
                time.sleep(0.6)
                desktop.apply(p)
            thread = threading.Thread(target=first_pass, daemon=True)
            thread.start()
            started.append(time.monotonic())
        h.on_enable = service_starts
        payload = {'policy': p, 'helper': (ROOT / 'scripts/desktop_access.py').read_text(), 'python': sys.executable}
        desktop.install_owner(payload)
        finished = time.monotonic()
        ready = h.grant / f'{name}.json'
        check(started and finished - started[0] >= 0.5 and json.loads(ready.read_text())['socket']
              == str(desktop.context_socket(h.grant, name)), 'install returns once the service has recorded the grant')
        check(h.acl(h.compositor_path) is None, 'and install itself never grants the compositor socket')
        # A service that only records why it is waiting: install says why, within its bound.
        desktop.atomic_json(ready, {'policy_digest': desktop.digest(p), 'state': 'pending',
                                    'reason': 'the fixture compositor went away', 'grants': []}, mode=0o644)
        try:
            desktop.wait_for_service_grant(p, h.gui, h.grant, name, timeout=0.5)
            outcome = 'ready'
        except desktop.DesktopAccessError as exc:
            outcome = str(exc)
        check('did not grant cerulean-worker a sandboxed Wayland socket' in outcome
              and 'the fixture compositor went away' in outcome and f'journalctl --user -u {name}.service' in outcome,
              f'a pending service is reported with its reason: {outcome}')
        h.compositor.close()


def test_a_long_project_name_still_gets_a_socket_that_fits() -> None:
    raw, h = host()
    with raw, h.fixtures():
        p = policy(project='a-project-whose-name-is-long-enough-to-overflow-a-unix-socket-address-' + 'x' * 30)
        name = desktop.names(p)[2]
        path = desktop.context_socket(h.grant, name)
        check(len(os.fsencode(str(path))) <= 107 and path.name.startswith('switchyard-') and len(path.name) == len('switchyard-') + 12 + len('.wayland')
              and path == desktop.context_socket(h.grant, name),
              f'the socket name is shortened, the same each time, to fit a socket address: {path.name}')
        desktop.apply(p)
        with h.as_tenant():
            env = desktop.verify(p)
        check(env['WAYLAND_DISPLAY'] == str(path) and h.acl(path) == 'rw-',
              'the grant works through it, and the tenant finds the same path')
        desktop.rollback(p)
        h.compositor.close()


def test_the_grant_directory_is_roots_parent_and_the_users_own() -> None:
    raw, h = host()
    with raw, h.fixtures():
        base, grant = h.base.stat(), h.grant.stat()
        check(base.st_uid == os.getuid() and stat.S_IMODE(base.st_mode) == 0o755
              and grant.st_uid == h.gui.pw_uid and stat.S_IMODE(grant.st_mode) == 0o700,
              f'the parent is its owner\'s at 0755, the directory the desktop user\'s at 0700: '
              f'{oct(base.st_mode)} {oct(grant.st_mode)}')
        boot = {f.name: f.read_text() for f in h.tmpfiles.iterdir()}
        check(boot == {'switchyard-desktop.conf': f'd {h.base} 0755 {os.getuid()} {os.getuid()} -\n',
                       f'switchyard-desktop-{h.gui.pw_uid}.conf': f'd {h.grant} 0700 {h.gui.pw_uid} {h.gui.pw_uid} -\n'},
              f'tmpfiles.d makes both again at boot: {boot}')
        check(desktop.grant_dir_for(h.gui) == h.grant, 'and readers find it')
        for label, spoil, mend in (
                ('a writable grant directory', lambda: h.grant.chmod(0o770), lambda: h.grant.chmod(0o700)),
                ('a parent another account owns', lambda: setattr(desktop, 'BASE_OWNER_UID', os.getuid() + 1),
                 lambda: setattr(desktop, 'BASE_OWNER_UID', os.getuid())),
                ('a grant directory that is a symlink', lambda: (h.grant.rename(h.root / 'moved'), h.grant.symlink_to(h.root / 'moved')),
                 lambda: (h.grant.unlink(), (h.root / 'moved').rename(h.grant)))):
            spoil()
            try:
                desktop.grant_dir_for(h.gui)
                outcome = 'accepted'
            except desktop.DesktopAccessError as exc:
                outcome = str(exc)
            mend()
            check("that no one else may write" in outcome and 'switchyard upgrade' in outcome,
                  f'{label} is refused, with what makes it: {outcome}')
        h.compositor.close()


def test_install_never_grants_the_runtime_and_takes_earlier_grants_back() -> None:
    raw, h = host()
    with raw, h.fixtures():
        p = policy()
        # Earlier grants of this owner's, to this tenant and another: on the runtime and the compositor socket.
        for who in (h.tenant, h.second):
            desktop.grant(h.runtime, who.pw_uid, '--x')
            desktop.grant(h.compositor_path, who.pw_uid, 'rw-')
            unit = Path(h.gui.pw_dir) / '.config/systemd/user' / f'switchyard-desktop-{h.gui.pw_uid}-{who.pw_uid}-old.service'
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text('[Service]\n')
        h.on_enable = lambda: desktop.apply(p)
        payload = {'policy': p, 'helper': (ROOT / 'scripts/desktop_access.py').read_text(), 'python': sys.executable}
        desktop.install_owner(payload)
        entries = {path.name: desktop.acl_map(desktop.read_acl(path)) for path in (h.runtime, h.compositor_path)}
        check(all(not any(k.startswith('user:') and k != 'user:' for k in acl) for acl in entries.values()),
              f'after install the runtime and the compositor socket carry no named entry, for either tenant: {entries}')
        check(h.acl(h.grant) == '--x' and h.acl(desktop.context_socket(h.grant, desktop.names(p)[2])) == 'rw-',
              'and the tenant reaches only the grant directory and its sandboxed socket')
        h.compositor.close()


def _payload(p):
    return {'policy': p, 'helper': (ROOT / 'scripts/desktop_access.py').read_text(), 'python': sys.executable}


def _old_install(h, name, p, helper_text):
    """A tenant's install as SYRD-791's interim revocation left it: files in place, its service stopped."""
    home = Path(h.gui.pw_dir)
    for path, text in ((home / '.config/switchyard/desktop-access' / f'{name}.json', json.dumps(p)),
                       (home / '.local/lib/switchyard/desktop-access' / f'{name}.py', helper_text),
                       (home / '.config/systemd/user' / f'{name}.service', '[Service]\nExecStart=old\n')):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return home / '.local/lib/switchyard/desktop-access' / f'{name}.py'


def test_installing_tenants_in_turn_leaves_every_grant_effective() -> None:
    """SYRD-796: root's ensure_grant_dir ran before each install and chmod-ed the directory, which set its ACL mask to
    ---: mefp's and pie's grants were in place and did nothing, and the next grant was refused."""
    raw, h = host()
    third = SimpleNamespace(pw_name='violet-worker', pw_uid=62003, pw_dir=str(h.root / 'third-home'))
    h.accounts[third.pw_name] = third
    h.uids.add(third.pw_uid)
    with raw, h.fixtures():
        refused = []
        for who, project in ((h.tenant, 'cerulean'), (h.second, 'amber'), (third, 'violet')):
            p = policy(who.pw_name, project)
            desktop.ensure_grant_dir(h.gui)  # root's step, on every install
            h.on_enable = lambda p=p: desktop.apply(p)
            try:
                desktop.install_owner(_payload(p))
            except desktop.DesktopAccessError as exc:  # what the live second install met
                refused.append(f'{project}: {exc}')
        effective = {who.pw_name: desktop.effective_acl(h.grant, who.pw_uid) for who in (h.tenant, h.second, third)}
        check(not refused and effective == {'cerulean-worker': 1, 'amber-worker': 1, 'violet-worker': 1},
              f'after three installs in turn every tenant can still traverse the grant directory: {effective} {refused}')
        desktop.ensure_grant_dir(h.gui)
        entries = desktop.acl_map(desktop.read_acl(h.grant))
        check(entries.get('mask:') == '--x' and all(desktop.effective_acl(h.grant, w.pw_uid) == 1 for w in (h.tenant, h.second, third))
              and stat.S_IMODE(h.grant.stat().st_mode) & 0o007 == 0 and entries.get('group:') == '---',
              f'and another of root\'s passes keeps them so, the owner\'s own and nobody else\'s: {entries}')
        h.compositor.close()


def test_a_failed_upgrade_never_restarts_a_helper_from_before_syrd_791() -> None:
    """SYRD-796: sapi's upgrade failed, the failure path put its pre-791 helper back and restarted it, and that helper
    granted --x on the desktop user's runtime again."""
    raw, h = host()
    with raw, h.fixtures():
        p = policy()
        name = desktop.names(p)[2]
        helper = _old_install(h, name, p, '# a desktop access helper from before SYRD-791\n')
        desktop.grant(h.runtime, h.tenant.pw_uid, '--x')  # what that helper granted while it ran
        h.systemctl.clear()
        with patch.object(desktop, 'wait_for_service_grant',
                          side_effect=desktop.DesktopAccessError('fixture: the service granted nothing within 20s')):
            try:
                desktop.install_owner(_payload(p))
                outcome = 'installed'
            except desktop.DesktopAccessError as exc:
                outcome = str(exc)
        unit = f'{name}.service'
        after_failure = h.systemctl[max(i for i, c in enumerate(h.systemctl) if 'disable' in c and unit in c):] \
            if any('disable' in c and unit in c for c in h.systemctl) else h.systemctl
        check('granted nothing' in outcome and not any('restart' in c and unit in c for c in after_failure)
              and any(c[:4] == ['systemctl', '--user', 'disable', '--now'] and unit in c for c in h.systemctl),
              f'the failed upgrade stops the service and never restarts the old helper: {h.systemctl}')
        check(desktop.SECURITY_CONTEXT_INTERFACE in helper.read_text() and h.acl(h.runtime) is None,
              "the old helper is not put back, and the runtime keeps no entry of the tenant's")
        # The same failure over this release's helper still puts the working install back.
        helper.write_text(_payload(p)['helper'])
        h.systemctl.clear()
        with patch.object(desktop, 'wait_for_service_grant',
                          side_effect=desktop.DesktopAccessError('fixture: the service granted nothing within 20s')):
            try:
                desktop.install_owner(_payload(p))
            except desktop.DesktopAccessError:
                pass
        check(['systemctl', '--user', 'restart', unit] in h.systemctl and h.acl(h.runtime) is None,
              f'a failure over the same helper restarts it, as before: {h.systemctl}')
        h.compositor.close()


def test_a_refused_upgrade_still_stops_the_tenants_own_service_from_before_syrd_791() -> None:
    """Audit of 7b4f604b: an upgrade refused before install_owner's `try` (no security-context manager, or no one
    socket to choose) left the tenant's own pre-791 service enabled, re-granting the runtime within 5 s."""
    for label, make in (('a compositor without the security-context manager',
                         lambda root: Host(root, security_context=False)),
                        ('two compositor sockets and none chosen', lambda root: Host(root))):
        raw = tempfile.TemporaryDirectory(prefix='syrd796.')
        with raw:
            h = make(Path(raw.name))
            second = FakeCompositor(h.runtime / 'wayland-8') if label.startswith('two') else None
            with h.fixtures():
                p = policy()
                name = desktop.names(p)[2]
                helper = _old_install(h, name, p, '# a desktop access helper from before SYRD-791\n')
                desktop.grant(h.runtime, h.tenant.pw_uid, '--x')  # what that helper grants every pass
                h.systemctl.clear()
                try:
                    desktop.install_owner(_payload(p))
                    outcome = 'installed'
                except desktop.DesktopAccessError as exc:
                    outcome = str(exc)
                check(outcome != 'installed', f'{label}: the upgrade is refused: {outcome}')
                check(['systemctl', '--user', 'disable', '--now', f'{name}.service'] in h.systemctl
                      and h.acl(h.runtime) is None,
                      f"{label}: and still the tenant's own pre-791 service is stopped and the runtime keeps no entry "
                      f'of its: {h.systemctl} {h.acl(h.runtime)}')
                check(helper.read_text().startswith('# a desktop access helper from before'),
                      f'{label}: its files stay for the next upgrade to replace')
            if second:
                second.close()
            h.compositor.close()


def test_install_stops_this_owners_services_from_before_syrd_791() -> None:
    raw, h = host()
    with raw, h.fixtures():
        p = policy()
        old_name = desktop.names(policy(h.second.pw_name, 'amber'))[2]
        new_name = desktop.names(policy(h.second.pw_name, 'amber-two'))[2]
        _old_install(h, old_name, policy(h.second.pw_name, 'amber'), '# from before SYRD-791\n')
        _old_install(h, new_name, policy(h.second.pw_name, 'amber-two'), _payload(p)['helper'])
        h.on_enable = lambda: desktop.apply(p)
        h.systemctl.clear()
        desktop.install_owner(_payload(p))
        check(['systemctl', '--user', 'disable', '--now', f'{old_name}.service'] in h.systemctl
              and not any(f'{new_name}.service' in c for c in h.systemctl),
              f"another tenant's service from before SYRD-791 is stopped; one on this release is left alone: {h.systemctl}")
        h.compositor.close()


def test_a_launch_with_no_desktop_socket_says_so_plainly() -> None:
    raw, h = host()
    with raw, h.fixtures():
        p = policy()
        name = desktop.names(p)[2]
        with h.as_tenant():
            try:
                desktop.verify(p)
                outcome = 'ready'
            except desktop.DesktopAccessError as exc:
                outcome = str(exc)
        check(outcome.startswith('cerulean-worker has no desktop socket:') and 'switchyard upgrade cerulean' in outcome
              and f'systemctl --user status {name}.service' in outcome and 'headless' in outcome,
              f'no receipt: no desktop socket, said plainly, with how to grant it and how to look: {outcome}')
        # Something not ours where the sandboxed socket goes is left alone, and nothing is granted.
        squatter = desktop.context_socket(h.grant, name)
        squatter.write_text('not a socket')
        try:
            desktop.apply(p)
            outcome = 'granted'
        except desktop.DesktopAccessError as exc:
            outcome = str(exc)
        check('is not a socket of this account' in outcome and squatter.read_text() == 'not a socket'
              and h.acl(h.compositor_path) is None, f'a file in the way is refused, untouched: {outcome}')
        h.compositor.close()


def main() -> int:
    for tool in ('setfacl', 'getfacl'):
        assert shutil.which(tool), f'{tool} required; this suite must not silently skip'
    for name, value in sorted(globals().items()):
        if name.startswith('test_') and callable(value):
            value()
    print(f'desktop_security_context_test: {CHECKS} checks ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
