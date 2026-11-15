#!/usr/bin/env python3
"""Scoped Wayland policy installation and pre-launch readiness (no clipboard reads)."""
from __future__ import annotations

import argparse
import array
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time


class DesktopAccessError(RuntimeError):
    pass


class SocketSelectionError(DesktopAccessError):
    """The GUI owner's Wayland session is active, but no one socket in its runtime is the approved one."""


def run(args):
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode:
        raise DesktopAccessError(f"{shlex.join(args[:3])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def validate_policy(raw, *, project: str, tenant: str) -> dict:
    if not isinstance(raw, dict) or raw.get('mode') not in {'headless', 'wayland'}:
        raise DesktopAccessError('Choose a desktop policy before launch: --desktop-policy FILE with mode headless or wayland.')
    allowed = {'mode', 'project', 'tenant_user', 'gui_user', 'wayland_display', 'consent'}
    if set(raw) - allowed:
        raise DesktopAccessError('Unknown desktop policy fields: ' + ', '.join(sorted(set(raw)-allowed)))
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', project):
        raise DesktopAccessError('Invalid desktop policy project')
    for key, expected in [('project', project), ('tenant_user', tenant)]:
        if raw.get(key, expected) != expected:
            raise DesktopAccessError(f'Desktop policy {key} does not match this tenant')
    result = {**raw, 'project': project, 'tenant_user': tenant}
    if raw['mode'] == 'headless':
        if set(raw) - {'mode', 'project', 'tenant_user'}:
            raise DesktopAccessError('Headless policy must not contain a desktop grant')
        return result
    for user in [tenant, raw.get('gui_user', '')]:
        if not isinstance(user, str) or not re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?', user):
            raise DesktopAccessError('Wayland policy requires explicit valid GUI and tenant users')
    consent = raw.get('consent')
    if not isinstance(consent, dict) or set(consent) != {'approved', 'by', 'at', 'reference'}:
        raise DesktopAccessError('Wayland consent requires approved, by, at and reference attribution')
    if consent['approved'] is not True or any(not isinstance(consent[k], str) or not consent[k].strip() for k in ['by','at','reference']):
        raise DesktopAccessError('Wayland grant is not approved; select headless or record this tenant’s own consent')
    from datetime import datetime
    try:
        when = datetime.fromisoformat(consent['at'].replace('Z','+00:00'))
        if when.tzinfo is None:
            raise ValueError('timezone required')
    except ValueError as exc:
        raise DesktopAccessError('Consent timestamp must be ISO-8601 with timezone') from exc
    display = raw.get('wayland_display', 'auto')
    if not isinstance(display, str) or (display != 'auto' and not re.fullmatch(r'wayland-[A-Za-z0-9_.-]+', display)):
        raise DesktopAccessError('wayland_display must be auto or a Wayland socket basename')
    result['wayland_display'] = display
    return result


#: How a generated policy says where its approval came from, so a reader of the
#: installed file can find the record rather than taking the word "approved".
GENERATED_REFERENCE_PREFIX = 'switchyard desktop approval recorded at '


def session_ids(runner=None) -> list[str]:
    """Every logind session id on this host, or none when logind cannot say."""
    runner = runner or run
    try:
        listing = runner(['loginctl', 'list-sessions', '--no-legend'])
    except (DesktopAccessError, OSError):
        return []
    ids = []
    for line in listing.splitlines():
        fields = line.split()
        if fields:
            ids.append(fields[0])
    return ids


def active_wayland_owners(runner=None) -> list[str]:
    """The accounts with an active Wayland session, asked of logind.

    Not `SUDO_USER`, and not the invoking account: those say who typed the
    command, which is a different question from whose compositor is running.
    The answer is a list because a host can have more than one, and choosing
    between them is not something provisioning may do quietly (SYRD-143).
    """
    runner = runner or run
    owners: list[str] = []
    for session in session_ids(runner):
        try:
            raw = runner(['loginctl', 'show-session', session, '-p', 'Type', '-p', 'Active', '-p', 'Name'])
        except (DesktopAccessError, OSError):
            continue
        values = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
        name = values.get('Name', '').strip()
        if values.get('Type') == 'wayland' and values.get('Active') == 'yes' and name and name not in owners:
            owners.append(name)
    return owners


def resolve_gui_owner(*, preferred: str = '', runner=None) -> str:
    """The one account whose desktop this project may be granted access to.

    Exactly one, or a refusal that says what to do. A host with no active
    Wayland session is not a failure of this function -- it is a headless host,
    and the caller decides what that means -- but a host with several desktops
    is an ambiguity nobody should resolve by guessing, so it is named and
    stopped (SYRD-143).
    """
    owners = active_wayland_owners(runner)
    if not owners:
        raise DesktopAccessError(
            'No active Wayland session was found on this host. Log into the desktop that '
            'should own this project, or install headless with --headless.'
        )
    if len(owners) == 1:
        return owners[0]
    chosen = (preferred or '').strip()
    if chosen and chosen in owners:
        return chosen
    raise DesktopAccessError(
        'Several accounts have an active Wayland session (' + ', '.join(sorted(owners)) + '). '
        'Name the one this project may use with --desktop-gui-user USER, or supply an '
        'explicit policy with --desktop-policy FILE.'
    )


def generated_policy(*, project: str, tenant: str, gui_user: str, approved_by: str,
                     approved_at: str = '', reference: str = '', wayland_display: str = 'auto') -> dict:
    """A scoped consent policy for one project, in the shape the validator takes.

    Generated rather than typed, because the fields a person could get wrong
    here -- which tenant, which desktop, which socket -- are facts provisioning
    already knows, and the one thing it cannot know is whether the desktop
    owner agrees. That is what the approval this records is for, and it is
    written into `consent` exactly as an operator-authored file would be, so
    what is installed and checked afterwards is one shape, not two.
    """
    at = approved_at.strip() or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    policy = {
        'mode': 'wayland',
        'project': project,
        'tenant_user': tenant,
        'gui_user': gui_user,
        'wayland_display': wayland_display or 'auto',
        'consent': {
            'approved': True,
            'by': approved_by or gui_user,
            'at': at,
            'reference': reference.strip() or (GENERATED_REFERENCE_PREFIX + at),
        },
    }
    return validate_policy(policy, project=project, tenant=tenant)


def digest(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def identity(policy):
    try:
        return pwd.getpwnam(policy['gui_user']), pwd.getpwnam(policy['tenant_user'])
    except KeyError as exc:
        raise DesktopAccessError('Configured GUI or tenant account is unavailable') from exc


def runtime_for(user) -> Path:
    runtime = Path(run(['loginctl', 'show-user', user.pw_name, '-p', 'RuntimePath', '--value']))
    if not runtime.is_absolute() or runtime == Path('/'):
        raise DesktopAccessError(f'No runtime directory for {user.pw_name}; start its login/user manager before project launch')
    info = runtime.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != user.pw_uid or runtime.is_symlink():
        raise DesktopAccessError(f'Unsafe runtime directory for {user.pw_name}: {runtime}')
    return runtime


def wayland_sockets(runtime) -> list[Path]:
    """The runtime's Wayland sockets: real sockets named `wayland-*`, never a symlink to one."""
    return sorted(p for p in runtime.glob('wayland-*') if p.is_socket() and not p.is_symlink())


def sole_socket_for_absent(configured, runtime) -> Path:
    """The one socket a policy pinned to a name that no longer exists may use instead, or a refusal naming why not.

    A compositor's socket name is not stable across logins: after
    plasma-login-manager 6.8.0 its greeter took `wayland-0` and the owner's own
    KWin moved to `wayland-1`, so a policy approved as `wayland-0` stranded the
    desktop until something renamed the socket by hand (SYRD-592). The name was
    never the approval -- the owner, the tenant and the active session are, and
    `selected_socket` still checks each of those -- so the owner's only socket
    serves. Several are a choice nobody approved, and none is a compositor not
    yet up; both are refused by name.
    """
    candidates = wayland_sockets(runtime)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise SocketSelectionError(f'Configured Wayland socket {configured} is absent from {runtime}, and no other '
                                   'Wayland socket is live there; waiting for the compositor')
    raise SocketSelectionError(
        f'Configured Wayland socket {configured} is absent from {runtime}, while several other Wayland sockets are live '
        f'({", ".join(p.name for p in candidates)}); Switchyard will not choose between them. Name one in the policy\'s '
        'wayland_display, or auto once only one remains, with: switchyard upgrade <project> --desktop-policy FILE')


def selected_socket(policy, gui, runtime):
    sessions = run(['loginctl', 'show-user', gui.pw_name, '-p', 'Sessions', '--value']).split()
    active = False
    for session in sessions:
        values = dict(line.split('=',1) for line in run(['loginctl','show-session',session,'-p','Type','-p','Active','-p','User']).splitlines() if '=' in line)
        active |= values.get('Type') == 'wayland' and values.get('Active') == 'yes' and values.get('User') == str(gui.pw_uid)
    if not active:
        raise DesktopAccessError(f'{gui.pw_name} has no active Wayland session; log into the selected desktop or choose headless')
    configured = policy['wayland_display']
    if configured == 'auto':
        candidates = wayland_sockets(runtime)
        if len(candidates) != 1:
            raise SocketSelectionError('Wayland socket discovery is missing or ambiguous; select a socket basename in the policy')
        path = candidates[0]
    else:
        path = runtime / configured
        if not os.path.lexists(path):
            path = sole_socket_for_absent(configured, runtime)
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != gui.pw_uid:
        raise DesktopAccessError(f'Selected Wayland socket is unavailable or owned by another user: {path}')
    return path


#: The tenant is a client of a socket KWin marks as sandboxed, never of the compositor's own socket (SYRD-791). A client
#: of the desktop user's raw socket is offered every global the desktop user's own apps are, and on KWin 6.8.2 that let
#: another account list every window with its title and type into the focused one (`org_kde_plasma_window_management`,
#: `org_kde_kwin_fake_input`). A `wp_security_context_v1` listener withholds those, screen capture and the clipboard.
SECURITY_CONTEXT_INTERFACE = 'wp_security_context_manager_v1'
SECURITY_CONTEXT_ENGINE = 'org.switchyard.desktop-access'
WAYLAND_TIMEOUT_SECONDS = 5


class WaylandProtocolError(DesktopAccessError):
    """The compositor refused or did not answer a request."""


def _wl_string(text: str) -> bytes:
    raw = text.encode() + b'\0'
    return struct.pack('<I', len(raw)) + raw + b'\0' * (-len(raw) % 4)


def _wl_read_string(body: bytes, at: int) -> tuple[str, int]:
    (length,) = struct.unpack_from('<I', body, at)
    raw = body[at + 4:at + 4 + length]
    return raw[:-1].decode(errors='replace'), at + 4 + length + (-length % 4)


class WaylandClient:
    """Just enough of the Wayland wire protocol to list a compositor's globals and register a security context: the
    desktop access helper runs on its own, from the standard library alone."""

    def __init__(self, path: Path, *, timeout: float = WAYLAND_TIMEOUT_SECONDS):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.buffer = b''
        self.last_id = 1  # 1 is wl_display

    def close(self):
        self.sock.close()

    def new_id(self) -> int:
        self.last_id += 1
        return self.last_id

    def send(self, obj: int, opcode: int, payload: bytes = b'', fds: tuple[int, ...] = ()):
        message = struct.pack('<II', obj, ((8 + len(payload)) << 16) | opcode) + payload
        if fds:
            self.sock.sendmsg([message], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', fds))])
        else:
            self.sock.sendall(message)

    def _event(self) -> tuple[int, int, bytes]:
        while True:
            if len(self.buffer) >= 8:
                obj, word = struct.unpack_from('<II', self.buffer)
                size = word >> 16
                if size < 8:
                    raise WaylandProtocolError('the compositor sent a malformed message')
                if len(self.buffer) >= size:
                    body, self.buffer = self.buffer[8:size], self.buffer[size:]
                    return obj, word & 0xffff, body
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WaylandProtocolError('the compositor closed the connection')
            self.buffer += chunk

    def roundtrip(self, on_event=lambda obj, opcode, body: None):
        """Everything the compositor says until it has handled every request sent so far; its error, raised."""
        callback = self.new_id()
        self.send(1, 0, struct.pack('<I', callback))  # wl_display.sync
        while True:
            obj, opcode, body = self._event()
            if obj == 1 and opcode == 0:  # wl_display.error(object_id, code, message)
                failed, code = struct.unpack_from('<II', body)
                message, _ = _wl_read_string(body, 8)
                raise WaylandProtocolError(f'the compositor refused a request (object {failed}, code {code}): {message}')
            if obj == callback and opcode == 0:  # wl_callback.done
                return
            on_event(obj, opcode, body)

    def globals(self) -> tuple[int, dict[str, tuple[int, int]]]:
        """(the registry's id, {interface: (name, version)})."""
        registry = self.new_id()
        self.send(1, 1, struct.pack('<I', registry))  # wl_display.get_registry
        found: dict[str, tuple[int, int]] = {}

        def on_event(obj, opcode, body):
            if obj == registry and opcode == 0:  # wl_registry.global(name, interface, version)
                (name,) = struct.unpack_from('<I', body)
                interface, at = _wl_read_string(body, 4)
                (version,) = struct.unpack_from('<I', body, at)
                found[interface] = (name, version)

        self.roundtrip(on_event)
        return registry, found

    def bind(self, registry: int, name: int, interface: str, version: int) -> int:
        obj = self.new_id()
        self.send(registry, 0, struct.pack('<I', name) + _wl_string(interface) + struct.pack('<II', version, obj))
        return obj


def offers_security_context(compositor: Path) -> bool:
    client = WaylandClient(compositor)
    try:
        return SECURITY_CONTEXT_INTERFACE in client.globals()[1]
    finally:
        client.close()


def register_security_context(compositor: Path, listen_fd: int, close_fd: int, *, app_id: str, instance_id: str):
    """Ask the compositor to accept clients on `listen_fd` as sandboxed ones, until `close_fd`'s peer hangs up."""
    client = WaylandClient(compositor)
    try:
        registry, offered = client.globals()
        if SECURITY_CONTEXT_INTERFACE not in offered:
            raise DesktopAccessError(
                f'the compositor at {compositor} offers no {SECURITY_CONTEXT_INTERFACE}, so it cannot keep another '
                "account's windows from the desktop user's: desktop access grants nothing rather than the compositor's "
                'own socket (SYRD-791)')
        manager = client.bind(registry, offered[SECURITY_CONTEXT_INTERFACE][0], SECURITY_CONTEXT_INTERFACE, 1)
        context = client.new_id()
        client.send(manager, 1, struct.pack('<I', context), fds=(listen_fd, close_fd))  # create_listener
        client.send(context, 1, _wl_string(SECURITY_CONTEXT_ENGINE))  # set_sandbox_engine
        client.send(context, 2, _wl_string(app_id))  # set_app_id
        client.send(context, 3, _wl_string(instance_id))  # set_instance_id
        client.send(context, 4)  # commit
        client.send(context, 0)  # destroy: the listener outlives this object and this connection
        client.send(manager, 0)  # destroy
        client.roundtrip()
    finally:
        client.close()


#: Security contexts this process holds, by grant name: the listener's path and identity, the compositor socket it was
#: registered on, and the write end of the pipe whose closing ends it. Only the owner's long-lived service holds one;
#: a context dies with the process that registered it.
HELD_CONTEXTS: dict[str, dict] = {}


def socket_identity(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_ctime_ns]


#: A Unix socket's path is at most 107 bytes (`sun_path` less its NUL); a project's name has no such limit.
UNIX_PATH_MAX = 107


def context_socket(directory: Path, name: str) -> Path:
    """`<grant directory>/<grant name>.wayland`, or -- when that would not fit in a socket address -- a name of fixed
    length derived from it, the same for the service that binds it and the tenant that connects."""
    path = directory / f'{name}.wayland'
    if len(os.fsencode(str(path))) <= UNIX_PATH_MAX:
        return path
    return directory / f"switchyard-{hashlib.sha256(name.encode()).hexdigest()[:12]}.wayland"


def release_context(name: str):
    held = HELD_CONTEXTS.pop(name, None)
    if held:
        with contextlib.suppress(OSError):
            os.close(held['closer'])


def _remove_own_socket(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
        raise DesktopAccessError(f'{path} is not a socket of this account; it is left alone, and nothing is granted')
    path.unlink()


def hold_context(name: str, compositor: Path, directory: Path, *, app_id: str, instance_id: str) -> Path:
    """This grant's security-context socket, registered on `compositor` and held by this process -- a new one whenever
    the compositor's socket was replaced or the listener's file is gone."""
    path = context_socket(directory, name)
    held = HELD_CONTEXTS.get(name)
    with contextlib.suppress(OSError):
        # Device and inode, not ctime: granting the socket's ACL changes its ctime, and that is still this listener.
        if held and held['compositor'] == socket_identity(compositor)[:2] and held['identity'] == socket_identity(path)[:2]:
            return path
    release_context(name)
    _remove_own_socket(path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    read_end, write_end = os.pipe()
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(64)
        register_security_context(compositor, listener.fileno(), read_end, app_id=app_id, instance_id=instance_id)
    except BaseException:
        os.close(write_end)
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    finally:
        listener.close()  # the compositor holds its own copies of both
        os.close(read_end)
    HELD_CONTEXTS[name] = {'compositor': socket_identity(compositor)[:2], 'identity': socket_identity(path)[:2],
                           'closer': write_end}
    return path


def account_exists(uid: int) -> bool:
    try:
        pwd.getpwuid(uid)
        return True
    except KeyError:
        return False


def drop_orphan_entries(paths) -> list[str]:
    """Remove named ACL entries for uids no account has: a later account given that uid would inherit the grant."""
    dropped = []
    for path in paths:
        try:
            entries = acl_map(read_acl(path))
        except (DesktopAccessError, OSError):
            continue
        for entry in entries:
            kind, _, who = entry.partition(':')
            if kind == 'user' and who.isdigit() and not account_exists(int(who)):
                run(['setfacl', '--no-mask', '-x', f'u:{who}', str(path)])
                dropped.append(f'{path}:{who}')
    return dropped


#: Where a desktop user's grants live, outside its runtime (SYRD-791): `/run/switchyard-desktop/<gui uid>/`, the user's
#: own 0700 directory under root's 0755 parent, recreated at boot by tmpfiles.d. The tenant gets `--x` there only. A
#: user runtime is 0700 because the sockets in it are not protected by their own modes: with `--x` on it, an agent
#: reached the user's PipeWire and PulseAudio and recorded the microphone (measured in a VM).
DESKTOP_BASE = Path('/run/switchyard-desktop')
#: Who must own `DESKTOP_BASE`: root, so no account can create the name first.
BASE_OWNER_UID = 0
TMPFILES_DIR = Path('/etc/tmpfiles.d')


def grant_dir(gui) -> Path:
    return DESKTOP_BASE / str(gui.pw_uid)


def _trusted_dir(path: Path, uid: int, what: str):
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise DesktopAccessError(f'{path} does not exist; {what}') from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
        raise DesktopAccessError(f'{path} is not a directory of uid {uid} that no one else may write; {what}')


def grant_dir_for(gui) -> Path:
    """The desktop user's grant directory, checked by whoever reads it: root's parent, the user's own directory."""
    what = ("the operator's `switchyard upgrade <project>` with the approved desktop policy makes it, "
            'or choose headless')
    _trusted_dir(DESKTOP_BASE, BASE_OWNER_UID, what)
    directory = grant_dir(gui)
    _trusted_dir(directory, gui.pw_uid, what)
    return directory


def ensure_grant_dir(gui) -> Path:
    """As root, at install: make the parent and the desktop user's directory, and the tmpfiles.d entries that make
    them again at boot. Anyone else may only find them made."""
    if os.geteuid() != BASE_OWNER_UID:
        return grant_dir_for(gui)
    directory = grant_dir(gui)
    for path, owner, mode in ((DESKTOP_BASE, BASE_OWNER_UID, 0o755), (directory, gui.pw_uid, 0o700)):
        if not os.path.lexists(path):
            os.mkdir(path, mode)
            os.chown(path, owner, -1)
        os.chmod(path, mode, follow_symlinks=False)
    grant_dir_for(gui)
    group = getattr(gui, 'pw_gid', gui.pw_uid)
    TMPFILES_DIR.mkdir(parents=True, exist_ok=True)
    for name, line in (('switchyard-desktop.conf', f'd {DESKTOP_BASE} 0755 {BASE_OWNER_UID} {BASE_OWNER_UID} -\n'),
                       (f'switchyard-desktop-{gui.pw_uid}.conf', f'd {directory} 0700 {gui.pw_uid} {group} -\n')):
        target = TMPFILES_DIR / name
        if not target.exists() or target.read_text() != line:
            target.write_text(line)
            target.chmod(0o644)
    return directory


def revoke_runtime_entries(runtime: Path, uids) -> list[str]:
    """Remove these tenants' named entries from the desktop user's runtime and its Wayland sockets: grants from before
    SYRD-791 put them there, and nothing does now."""
    removed = []
    for path in (runtime, *wayland_sockets(runtime)):
        try:
            entries = acl_map(read_acl(path))
        except (DesktopAccessError, OSError):
            continue
        for uid in sorted(set(uids)):
            if f'user:{uid}' in entries:
                run(['setfacl', '--no-mask', '-x', f'u:{uid}', str(path)])
                removed.append(f'{path}:{uid}')
    return removed


def names(policy):
    policy = validate_policy(policy, project=policy["project"], tenant=policy["tenant_user"])
    gui, tenant = identity(policy)
    name = f"switchyard-desktop-{gui.pw_uid}-{tenant.pw_uid}-{policy['project']}"
    return gui, tenant, name


def read_acl(path):
    return [line for line in run(['getfacl','-cpn',str(path)]).splitlines() if line and not line.startswith('#')]


def acl_map(lines):
    return {':'.join(line.split(':')[:2]): line.split(':')[2].split()[0] for line in lines if not line.startswith('default:')}


def bits(perms):
    return sum(bit for ch, bit in zip(perms, [4,2,1]) if ch != '-')


def perms(value):
    return ''.join(ch if value & bit else '-' for ch, bit in [('r',4),('w',2),('x',1)])


def effective_acl(path, uid):
    entries = acl_map(read_acl(path))
    return bits(entries.get(f'user:{uid}', '---')) & bits(entries.get('mask:', entries['group:']))


def grant(path, uid, desired):
    before = read_acl(path)
    entries = acl_map(before)
    key = f'user:{uid}'
    # Recalculating masks can broaden unrelated entries. Refuse that expansion.
    old_mask = bits(entries.get('mask:', entries['group:']))
    new_mask = old_mask | bits(desired)
    for entry, value in entries.items():
        if entry == key or entry in {'user:', 'other:', 'mask:'}:
            continue
        if bits(value) & new_mask != bits(value) & old_mask:
            raise DesktopAccessError(f'{path}: requested ACL would broaden an unrelated masked entry; operator must review its ACL first')
    run(['setfacl','--no-mask','-m',f'u:{uid}:{desired},m::{perms(new_mask)}',str(path)])
    return {'path': str(path), 'inode': path.stat().st_ino, 'device': path.stat().st_dev,
            'before': before, 'after': read_acl(path), 'uid': uid}


def restore_grant(record):
    path = Path(record['path'])
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (info.st_dev, info.st_ino) != (record['device'], record['inode']):
        return
    current = read_acl(path)
    if current == record['after']:
        # The exact ACL is still ours; restore its original mask as well.
        run(['setfacl', '--set=' + ','.join(record['before']), str(path)])
    else:
        # Another tenant may now depend on the mask. Restore just our entry,
        # and only if nobody has changed that entry since our grant.
        key = f"user:{record['uid']}"
        if acl_map(current).get(key) != acl_map(record['after']).get(key):
            return
        old = acl_map(record['before']).get(key)
        args = ['setfacl','--no-mask']
        args += ['-m',f"u:{record['uid']}:{old}"] if old is not None else ['-x',f"u:{record['uid']}"]
        run([*args,str(path)])


def atomic_json(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.'+path.name)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write('\n')
            os.fchmod(stream.fileno(), mode)
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextlib.contextmanager
def gui_lock(directory):
    path = directory / '.switchyard-desktop.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


#: Where the GUI owner leaves a clipboard image for one project's tenant
#: (SYRD-247): `<grant directory>/switchyard-paste/<project>/`, inside the
#: directory the approved grant already lets the tenant traverse -- since
#: SYRD-791 the grant directory, never the GUI runtime. The owner creates
#: everything here; the tenant may traverse the parent, list its own project's
#: directory and read each file, and write nothing.
HANDOFF_DIR = 'switchyard-paste'
#: How long a handed-over image exists, from the moment it was copied. The
#: owner's access service removes it within one watch interval after this.
HANDOFF_TTL_SECONDS = 15 * 60
#: The only names a handoff directory holds: an image and its manifest, sharing
#: one random stem, plus the manifest's temporary name before it is renamed in.
HANDOFF_NAME = re.compile(r'(?:\.tmp-)?paste-[0-9a-f]{32}\.(?:png|jpg|gif|webp|bmp|handoff\.json)')


def handoff_dir(runtime, project):
    return Path(runtime) / HANDOFF_DIR / project


def _owned_directory(path, uid, *, create):
    """`path` as a real directory `uid` owns that nobody else may write, or a refusal.

    Created 0700 when `create` and missing. A symlink, a non-directory, another
    owner, or a group/other write bit is refused rather than written through.
    """
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, 0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022 or info.st_mode & 0o007:
        raise DesktopAccessError(f'{path} is not a private directory owned by uid {uid}; refusing to use it for an image handoff')
    return Path(path)


def prepare_handoff_dir(runtime, project, tenant_uid):
    """As the GUI owner: the project's handoff directory, with the tenant's two entries.

    The parent gets `--x` for the tenant (reach the project directory, list
    nothing), the project directory `r-x` (list and open what is there). Both
    through `grant`, which refuses to broaden any unrelated masked entry.
    """
    runtime = Path(runtime)
    owner = os.geteuid()
    if runtime.lstat().st_uid != owner or runtime.is_symlink():
        raise DesktopAccessError(f'{runtime} is not this account\'s runtime directory; run this as the desktop owner')
    if not effective_acl(runtime, tenant_uid) & 1:
        # The approved grant's own traverse entry, which `apply` makes. Without
        # it the tenant can reach nothing in here, so nothing is handed over.
        raise DesktopAccessError(f'the approved desktop grant is not applied to {runtime}; start the project\'s desktop access service first')
    parent = _owned_directory(runtime / HANDOFF_DIR, owner, create=True)
    leaf = _owned_directory(parent / project, owner, create=True)
    for path, wanted in ((parent, '--x'), (leaf, 'r-x')):
        if effective_acl(path, tenant_uid) != bits(wanted):
            grant(path, tenant_uid, wanted)
    return leaf


def _handoff_entries(runtime, project):
    leaf = handoff_dir(runtime, project)
    try:
        _owned_directory(leaf, os.geteuid(), create=False)
        with os.scandir(leaf) as entries:
            return [entry for entry in entries if HANDOFF_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)]
    except (OSError, DesktopAccessError):
        return []


def sweep_handoffs(runtime, project, *, now=None, ttl=HANDOFF_TTL_SECONDS):
    """As the GUI owner: remove this project's handed-over files older than `ttl`.

    Run by the owner's access service on every watch pass, so an image leaves
    within one interval of expiring whether or not anything is pasted again.
    """
    now = time.time() if now is None else now
    removed = []
    for entry in _handoff_entries(runtime, project):
        with contextlib.suppress(OSError):
            if now - entry.stat(follow_symlinks=False).st_mtime >= ttl:
                os.unlink(entry.path)
                removed.append(entry.name)
    return removed


def remove_handoffs(runtime, project):
    """As the GUI owner: remove the project's handoff directory, as a grant is withdrawn."""
    for entry in _handoff_entries(runtime, project):
        with contextlib.suppress(OSError):
            os.unlink(entry.path)
    for path in (handoff_dir(runtime, project), Path(runtime) / HANDOFF_DIR):
        with contextlib.suppress(OSError):
            os.rmdir(path)


def record_pending(policy, gui, tenant, directory, ready, reason):
    """Say in the readiness receipt why access is waiting, where the tenant can already read it.

    Only where the tenant can already traverse the grant directory -- an
    earlier grant to this or another of its projects -- because that is the only
    place the tenant's launch can read it. Nothing is granted to say it: before
    the first grant of a login the launch can only point at the service's
    journal (`verify`). The grants already recorded are kept, so rollback still
    restores them, and the socket the receipt named is dropped, so no launch
    takes it for ready.
    """
    with gui_lock(directory):
        previous = json.loads(ready.read_text()) if ready.exists() else {}
        if previous and previous.get('policy_digest') != digest(policy):
            return
        if not effective_acl(directory, tenant.pw_uid) & 1:
            return
        pending = {'policy_digest': digest(policy), 'state': 'pending', 'reason': reason,
                   'gui_uid': gui.pw_uid, 'tenant_uid': tenant.pw_uid, 'grants': previous.get('grants', [])}
        if previous != pending:
            atomic_json(ready, pending, mode=0o644)


def apply(policy):
    gui, tenant, name = names(policy)
    if os.geteuid() != gui.pw_uid:
        raise DesktopAccessError('ACL reapplication must execute as the configured GUI owner')
    # Everything the tenant may reach is in the grant directory; nothing of the runtime is granted (SYRD-791).
    directory = grant_dir_for(gui)
    ready = directory / (name+'.json')
    try:
        runtime = runtime_for(gui)
        compositor = selected_socket(policy, gui, runtime)
    except (DesktopAccessError, OSError) as exc:
        # No session, or no one socket to use: the receipt outlives a logout now that it is outside the runtime, so
        # it says why, and the socket whose compositor is gone goes (SYRD-791).
        release_context(name)
        with contextlib.suppress(DesktopAccessError, OSError):
            _remove_own_socket(context_socket(directory, name))
        reason = (str(exc) if isinstance(exc, DesktopAccessError)
                  else f'{gui.pw_name} is not logged in: its runtime directory is unavailable ({exc.strerror or exc})')
        record_pending(policy, gui, tenant, directory, ready, reason)
        if isinstance(exc, DesktopAccessError):
            raise
        raise DesktopAccessError(reason) from exc
    with gui_lock(directory):
        # No grant outlives its account, and the runtime keeps none of this tenant's (SYRD-791).
        drop_orphan_entries([runtime, directory, *wayland_sockets(runtime), *directory.glob('switchyard-*.wayland')])
        revoke_runtime_entries(runtime, [tenant.pw_uid])
        legacy = runtime / (name+'.json')
        with contextlib.suppress(OSError):
            legacy.unlink()  # a receipt from before SYRD-791, in the runtime; its grants are gone with the entries
    # The tenant connects to a security-context socket this service holds, never to the compositor's own (SYRD-791).
    try:
        path = hold_context(name, compositor, directory, app_id=tenant.pw_name, instance_id=policy['project'])
    except (DesktopAccessError, OSError) as exc:
        record_pending(policy, gui, tenant, directory, ready, f'no sandboxed Wayland socket for {tenant.pw_name}: {exc}')
        raise DesktopAccessError(str(exc)) from exc
    with gui_lock(directory):
        previous = json.loads(ready.read_text()) if ready.exists() else {}
        info = path.stat()
        if (previous.get('policy_digest') == digest(policy) and previous.get('state') != 'pending'
                and previous.get('socket_identity') == [info.st_dev,info.st_ino,info.st_ctime_ns]):
            # Check ACLs as well: revocation should not be hidden by a stale marker.
            if effective_acl(directory,tenant.pw_uid) == 1 and effective_acl(path,tenant.pw_uid) == 6:
                return ready
        records = []
        try:
            records.append(grant(directory, tenant.pw_uid, '--x'))
            records.append(grant(path, tenant.pw_uid, 'rw-'))
            info = path.stat()
            # Preserve original rollback state for the grant directory and same socket.
            peer_records = []
            for peer in directory.glob(f'switchyard-desktop-{gui.pw_uid}-{tenant.pw_uid}-*.json'):
                if peer != ready:
                    peer_records.extend(json.loads(peer.read_text()).get('grants', []))
            originals = {(r['device'],r['inode']):r for r in [*previous.get('grants', []), *peer_records]}
            for r in records:
                prior = originals.get((r['device'],r['inode']))
                if prior:
                    r['before'] = prior['before']
            # A socket this receipt granted before and no longer uses keeps no
            # grant from it: the tenant is a client of its security-context
            # socket only. That includes the compositor's own socket, which a
            # receipt from before SYRD-791 granted directly.
            held = {(r['device'],r['inode']) for r in [*records, *peer_records]}
            for stale in previous.get('grants', []):
                if (stale['device'],stale['inode']) not in held:
                    restore_grant(stale)
            compositor_info = compositor.stat()
            atomic_json(ready, {'policy_digest':digest(policy), 'socket':str(path),
                'socket_identity':[info.st_dev,info.st_ino,info.st_ctime_ns],
                'compositor_socket':str(compositor),
                'compositor_identity':[compositor_info.st_dev,compositor_info.st_ino,compositor_info.st_ctime_ns],
                'security_context':{'engine':SECURITY_CONTEXT_ENGINE,'app_id':tenant.pw_name,
                                    'instance_id':policy['project']},
                'configured_socket':policy['wayland_display'],
                'gui_uid':gui.pw_uid, 'tenant_uid':tenant.pw_uid, 'grants':records}, mode=0o644)
        except Exception:
            for record in reversed(records):
                restore_grant(record)
            raise
    if policy['wayland_display'] not in ('auto', compositor.name):
        print(f"Wayland access: configured socket {policy['wayland_display']} is absent from {runtime}; "
              f'used the only live Wayland socket there, {compositor.name}', file=sys.stderr, flush=True)
    return ready


def verify(policy):
    gui, tenant, name = names(policy)
    if os.geteuid() != tenant.pw_uid:
        raise DesktopAccessError('Readiness validation must execute as the tenant')
    tenant_runtime = runtime_for(tenant)
    try:
        directory = grant_dir_for(gui)
        ready = directory / (name+'.json')
        info = ready.lstat()
    except (FileNotFoundError, PermissionError, DesktopAccessError) as exc:
        # Unreadable before the owner's service has granted anything in this
        # login, so why it is waiting is in its journal, not here (SYRD-592).
        # Or no service at all: desktop grants withdrawn as eric (SYRD-791).
        raise DesktopAccessError(
            f"{tenant.pw_name} has no desktop socket: {gui.pw_name}'s desktop access service for {policy['project']} "
            f'has not granted one in this login ({exc}). Either the service is stopped -- desktop grants withdrawn -- '
            f'and `switchyard upgrade {policy["project"]}` with the approved desktop policy grants it again, or it is '
            f'still waiting and says why; as {gui.pw_name}: systemctl --user status {name}.service. '
            'A headless policy needs no desktop socket.') from exc
    try:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != gui.pw_uid or info.st_mode & 0o022:
            raise DesktopAccessError('Desktop readiness record is not protected by the GUI owner')
        value = json.loads(ready.read_text())
        if value.get('policy_digest') != digest(policy):
            raise DesktopAccessError('Desktop policy has not been installed by its GUI owner; run the supported upgrade with this policy before launch')
        if value.get('state') == 'pending':
            raise DesktopAccessError(f"Desktop access is not ready before launch: {gui.pw_name}'s desktop access service "
                                     f"is waiting: {value.get('reason')}")
        path = Path(value['socket'])
        if path.parent != directory or path.is_symlink():
            raise DesktopAccessError('Desktop readiness refers to an invalid socket')
        if path != context_socket(directory, name) or path.name.startswith('wayland-'):
            # A receipt from before SYRD-791 named the compositor's own socket, whose clients may list and type
            # into every window of the desktop user's. It is not used, whatever its ACL still says.
            raise DesktopAccessError(
                f"{tenant.pw_name}'s desktop grant names {gui.pw_name}'s own Wayland socket ({path.name}), not a "
                f'sandboxed one; it is not used. Grant it again with `switchyard upgrade {policy["project"]}` and the '
                'approved desktop policy, or choose headless')
        info = path.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != gui.pw_uid or value['socket_identity'] != [info.st_dev,info.st_ino,info.st_ctime_ns]:
            raise DesktopAccessError('Wayland socket was recreated or changed; wait for the GUI access service to reapply before launching')
        if not os.access(directory,os.X_OK) or not os.access(path,os.R_OK|os.W_OK):
            raise DesktopAccessError('Approved Wayland ACL is not ready; check the GUI access service before launching')
        with socket.socket(socket.AF_UNIX) as probe:
            probe.settimeout(2)
            probe.connect(str(path))
    except (OSError,ValueError,KeyError) as exc:
        raise DesktopAccessError(f'Desktop access is not ready before launch: {exc}. Run supported upgrade with the approved policy, or select headless.') from exc
    return {'WAYLAND_DISPLAY':str(path), 'XDG_RUNTIME_DIR':str(tenant_runtime),
            'DBUS_SESSION_BUS_ADDRESS':f'unix:path={tenant_runtime}/bus'}


def gui_command(gui, runtime, args):
    # This is a GUI-owner process, never substitution of a tenant's runtime/bus.
    command = ['env',f'XDG_RUNTIME_DIR={runtime}',f'DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus',*args]
    if os.geteuid() == gui.pw_uid:
        return command
    if os.geteuid() != 0:
        raise DesktopAccessError('Initial Wayland setup requires the GUI owner or operator during new/upgrade, before role launch')
    return ['runuser','-u',gui.pw_name,'--',*command]


def install(policy, *, helper: Path):
    gui, tenant, name = names(policy)
    runtime = runtime_for(gui)
    selected_socket(policy,gui,runtime)
    run(gui_command(gui,runtime,['which','setfacl','getfacl']))
    # Root makes the grant directory, outside the desktop user's runtime, before the user's service runs (SYRD-791).
    ensure_grant_dir(gui)
    if not helper.is_absolute() or not helper.is_file():
        raise DesktopAccessError('Installed desktop-access helper is missing')
    # Copy reviewed release code into GUI-owned state. Its service must not depend
    # on another user's private checkout, nor on an export later being removed.
    payload = {'policy':policy, 'helper':helper.read_text(), 'python':sys.executable}
    run(gui_command(gui,runtime,[sys.executable,'-c',helper.read_text(),'install-owner',json.dumps(payload)]))


def install_owner(payload):
    policy = validate_policy(payload['policy'],project=payload['policy']['project'],tenant=payload['policy']['tenant_user'])
    gui,tenant,name = names(policy)
    if os.geteuid() != gui.pw_uid:
        raise DesktopAccessError('Policy installation must run as GUI owner')
    runtime = runtime_for(gui)
    policy_path = Path(gui.pw_dir)/'.config/switchyard/desktop-access'/(name+'.json')
    helper_path = Path(gui.pw_dir)/'.local/lib/switchyard/desktop-access'/(name+'.py')
    unit_path = Path(gui.pw_dir)/'.config/systemd/user'/(name+'.service')
    paths = [policy_path,helper_path,unit_path]
    if unit_path.exists() and not policy_path.exists():
        raise DesktopAccessError(f'Existing unmanaged unit {unit_path}; preserve it and resolve the conflict before launch')
    if policy_path.exists() and json.loads(policy_path.read_text()) != policy:
        raise DesktopAccessError('Existing desktop grant differs; revoke that scoped policy through upgrade before replacing it')
    previous = {path:path.read_bytes() if path.exists() else None for path in paths}
    was_installed = policy_path.exists()
    command = ' '.join('"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"' for value in [payload['python'],helper_path,'watch',policy_path])
    unit = f"""[Unit]
Description=Switchyard scoped Wayland access for {policy['project']} ({tenant.pw_name})
After=graphical-session.target
[Service]
Type=simple
ExecStart={command}
Restart=on-failure
RestartSec=5
[Install]
WantedBy=default.target
"""
    directory = grant_dir_for(gui)
    # Installs left by tenants whose account is gone: their service, files and receipt (SYRD-791).
    remove_orphan_installs(gui, directory, runtime)
    # Grants from before SYRD-791 put this owner's tenants on the runtime and its Wayland sockets: none stays.
    revoke_runtime_entries(runtime, owner_tenant_uids(gui))
    # Refused before anything is written: a compositor that cannot sandbox the tenant gets no grant at all.
    compositor = selected_socket(policy, gui, runtime)
    if not offers_security_context(compositor):
        raise DesktopAccessError(
            f'the compositor at {compositor} offers no {SECURITY_CONTEXT_INTERFACE}, so it cannot keep '
            f"{tenant.pw_name}'s windows from {gui.pw_name}'s; desktop access is not installed (SYRD-791). "
            'Choose headless for this project.')
    try:
        atomic_json(policy_path,policy)
        helper_path.parent.mkdir(parents=True,exist_ok=True)
        helper_path.write_text(payload['helper'])
        helper_path.chmod(0o600)
        unit_path.parent.mkdir(parents=True,exist_ok=True)
        unit_path.write_text(unit)
        run(['systemctl','--user','daemon-reload'])
        run(['systemctl','--user','enable','--now',name+'.service'])
        # Apply new helper version to the service without touching worker panes.
        if was_installed and previous[helper_path] != payload['helper'].encode():
            run(['systemctl','--user','restart',name+'.service'])
        run(['systemctl','--user','is-active',name+'.service'])
        # The service holds the tenant's security context, so the grant is ready when its receipt says so.
        wait_for_service_grant(policy, gui, directory, name)
    except Exception:
        if not was_installed:
            subprocess.run(['systemctl','--user','disable','--now',name+'.service'],capture_output=True)
            rollback(policy)
        for path,content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        subprocess.run(['systemctl','--user','daemon-reload'],capture_output=True)
        if was_installed:
            subprocess.run(['systemctl','--user','restart',name+'.service'],capture_output=True)
        raise


SERVICE_GRANT_TIMEOUT_SECONDS = 20


def wait_for_service_grant(policy, gui, directory, name, *, timeout=SERVICE_GRANT_TIMEOUT_SECONDS,
                           sleep=time.sleep, monotonic=time.monotonic):
    """Until the owner's service has recorded this tenant's sandboxed socket as ready; its reason, raised, if not."""
    ready = directory / (name + '.json')
    deadline = monotonic() + timeout
    reason = 'it has not recorded a grant yet'
    while True:
        with contextlib.suppress(OSError, ValueError):
            value = json.loads(ready.read_text())
            if value.get('policy_digest') == digest(policy):
                if value.get('state') == 'pending':
                    reason = value.get('reason') or 'it is waiting'
                elif Path(value.get('socket', '')) == context_socket(directory, name) and Path(value['socket']).is_socket():
                    return value
        if monotonic() >= deadline:
            raise DesktopAccessError(f"{gui.pw_name}'s desktop access service did not grant {policy['tenant_user']} a "
                                     f'sandboxed Wayland socket within {timeout:g}s: {reason}. As {gui.pw_name}: '
                                     f'journalctl --user -u {name}.service')
        sleep(0.25)


GRANT_FILE = re.compile(r'switchyard-desktop-(\d+)-(\d+)-([a-z0-9][a-z0-9_-]*)')


def owner_tenant_uids(gui) -> set[int]:
    """Every tenant uid this desktop user has installed a grant for, past or present, from its own unit names."""
    uids = set()
    for unit in (Path(gui.pw_dir) / '.config/systemd/user').glob(f'switchyard-desktop-{gui.pw_uid}-*.service'):
        match = GRANT_FILE.fullmatch(unit.name[:-len('.service')])
        if match and int(match.group(1)) == gui.pw_uid:
            uids.add(int(match.group(2)))
    return uids


def remove_orphan_installs(gui, directory, runtime) -> list[str]:
    """Stop and remove this owner's desktop grants for tenants whose account no longer exists -- service, policy,
    helper, receipt and socket -- so a later account given the same uid inherits none of it."""
    removed = []
    unit_dir = Path(gui.pw_dir) / '.config/systemd/user'
    for unit in sorted(unit_dir.glob(f'switchyard-desktop-{gui.pw_uid}-*.service')):
        match = GRANT_FILE.fullmatch(unit.name[:-len('.service')])
        if not match or int(match.group(1)) != gui.pw_uid or account_exists(int(match.group(2))):
            continue
        name = match.group(0)
        subprocess.run(['systemctl', '--user', 'disable', '--now', unit.name], capture_output=True)
        for path in (unit, Path(gui.pw_dir) / '.config/switchyard/desktop-access' / (name + '.json'),
                     Path(gui.pw_dir) / '.local/lib/switchyard/desktop-access' / (name + '.py'),
                     directory / (name + '.json'), runtime / (name + '.json')):
            path.unlink(missing_ok=True)
        with contextlib.suppress(DesktopAccessError, OSError):
            _remove_own_socket(context_socket(directory, name))
        removed.append(name)
    if removed:
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
    return removed


def uninstall(policy):
    gui,tenant,name = names(policy)
    runtime = runtime_for(gui)
    helper_path = Path(gui.pw_dir)/'.local/lib/switchyard/desktop-access'/(name+'.py')
    policy_path = Path(gui.pw_dir)/'.config/switchyard/desktop-access'/(name+'.json')
    run(gui_command(gui,runtime,[sys.executable,str(helper_path),'uninstall-owner',str(policy_path)]))


def uninstall_owner(policy):
    gui,tenant,name = names(policy)
    if os.geteuid() != gui.pw_uid:
        raise DesktopAccessError('Policy removal must execute as GUI owner')
    run(['systemctl','--user','disable','--now',name+'.service'])
    rollback(policy)
    for path in [Path(gui.pw_dir)/'.config/switchyard/desktop-access'/(name+'.json'),
                 Path(gui.pw_dir)/'.config/systemd/user'/(name+'.service'),
                 Path(gui.pw_dir)/'.local/lib/switchyard/desktop-access'/(name+'.py')]:
        path.unlink(missing_ok=True)
    run(['systemctl','--user','daemon-reload'])


def rollback(policy):
    gui,tenant,name = names(policy)
    if os.geteuid() != gui.pw_uid:
        raise DesktopAccessError('Rollback must run as GUI owner')
    runtime = runtime_for(gui)
    # A grant from before SYRD-791 lived in the runtime: its receipt and the tenant's entries go, whatever else exists.
    with contextlib.suppress(OSError):
        (runtime/(name+'.json')).unlink()
    revoke_runtime_entries(runtime, [tenant.pw_uid])
    try:
        directory = grant_dir_for(gui)
    except DesktopAccessError:
        release_context(name)
        return
    with gui_lock(directory):
        ready = directory/(name+'.json')
        if ready.exists():
            value = json.loads(ready.read_text())
            if value['policy_digest'] != digest(policy):
                raise DesktopAccessError('Rollback policy differs from active grant')
            peers = []
            for other in directory.glob(f'switchyard-desktop-{gui.pw_uid}-{tenant.pw_uid}-*.json'):
                if other != ready:
                    peers.extend(json.loads(other.read_text()).get('grants', []))
            active = {(r['device'],r['inode']) for r in peers}
            for record in reversed(value['grants']):
                if (record['device'],record['inode']) not in active:
                    restore_grant(record)
            ready.unlink()
        # The sandboxed socket goes with the grant; its service, if still running, stops listening when it ends.
        release_context(name)
        with contextlib.suppress(DesktopAccessError, OSError):
            _remove_own_socket(context_socket(directory, name))
        # Withdrawing the grant withdraws anything still handed over under it.
        remove_handoffs(directory, policy['project'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['watch','apply','verify','install-owner','uninstall-owner','rollback'])
    parser.add_argument('policy')
    args=parser.parse_args(argv)
    if args.action == 'install-owner':
        install_owner(json.loads(args.policy)); return 0
    raw=json.loads(Path(args.policy).read_text())
    policy=validate_policy(raw,project=raw['project'],tenant=raw['tenant_user'])
    if args.action=='verify':
        print(json.dumps(verify(policy)));return 0
    if args.action=='uninstall-owner':
        uninstall_owner(policy);return 0
    if args.action=='rollback':
        rollback(policy);return 0
    if args.action=='apply':
        apply(policy);return 0
    while True:
        try:
            # The owner's reliable cleanup for handed-over images (SYRD-247).
            sweep_handoffs(apply(policy).parent, policy['project'])
        except (DesktopAccessError,OSError) as exc:
            print(f'Wayland access pending: {exc}',file=sys.stderr,flush=True)
        time.sleep(5)


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (DesktopAccessError,OSError,ValueError) as exc:
        raise SystemExit(f'switchyard desktop access: {exc}')
