#!/usr/bin/env python3
"""Scoped Wayland policy installation and pre-launch readiness (no clipboard reads).

A grant reaches the desktop owner's compositor through a confined exposure
(SYRD-687): one directory per owner, tenant and project under
`EXPOSURE_ROOT`, made once by root and owned by the desktop owner, holding a
relay socket only that tenant's uid may use, the readiness receipt and the
image hand-off. The owner's XDG runtime directory keeps no tenant entry and
stays 0700. The grant this replaces put named ACL entries on that directory,
whose group bits then showed the mask -- 0710 -- and Qt, KDE and the XDG spec
reject a runtime directory that is not exactly 0700. Upgrade retires those
entries (`retire_legacy`) only once the exposure is verified, and `repair`
shows the desktop owner what still holds them.
"""
from __future__ import annotations

import argparse
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
import threading
import time


class DesktopAccessError(RuntimeError):
    pass


class SocketSelectionError(DesktopAccessError):
    """The GUI owner's Wayland session is active, but no one socket in its runtime is the approved one."""


class ExposureMissing(DesktopAccessError):
    """The grant's exposure directory does not exist: nothing has made it yet, or the grant predates it."""


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
    try:
        info = runtime.stat()
    except FileNotFoundError:
        raise DesktopAccessError(f'{user.pw_name} has no runtime directory {runtime}; it exists while {user.pw_name} is '
                                 'logged in') from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != user.pw_uid or runtime.is_symlink():
        raise DesktopAccessError(f'Unsafe runtime directory for {user.pw_name}: {runtime}')
    return runtime


def runtime_mode_problem(runtime, owner_uid) -> str:
    """What Qt's QStandardPaths would object to in this runtime directory, or ''.

    Qt, KDE and the XDG Base Directory spec take a runtime directory only when
    its user owns it and its mode is exactly 0700. A POSIX ACL's mask shows in
    the group bits, so one named `--x` entry makes it 0710, and QStandardPaths
    says "wrong permissions on runtime directory ..., 0710 instead of 0700".
    """
    info = os.lstat(runtime)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid:
        return f'{runtime} is not a directory owned by uid {owner_uid}'
    mode = stat.S_IMODE(info.st_mode) & 0o777
    if mode == 0o700:
        return ''
    named = [f'{key}:{value}' for key, value in acl_map(read_acl(runtime)).items() if key.split(':')[1]]
    return f'{runtime} is {mode:04o} instead of 0700' + (f'; its ACL names {", ".join(named)}' if named else '')


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


def drop_entry(path, uid, *, expected=None, restore=None, prior_mask=None):
    """Take `user:uid` off `path`'s ACL without widening anyone else; what was left alone, or ''.

    Only while the entry is still `expected`, the value Switchyard set; one
    somebody changed since is theirs. `restore` is the entry the path had
    before Switchyard's grant, put back instead, as effective as `prior_mask`
    -- the mask before the grant -- let it be. The mask then narrows to what
    the remaining entries need and never widens. With no named entry left the
    ACL is made minimal again -- the group entry first narrowed to what the
    mask allowed it -- so the group bits are the group entry's, not a mask's.
    """
    entries = acl_map(read_acl(path))
    key = f'user:{uid}'
    current = entries.get(key)
    if current is None:
        return ''
    if expected is not None and current != expected:
        return f'{path}: user:{uid} is {current}, not the {expected} Switchyard set; left as it is'
    old_mask = bits(entries.get('mask:', entries['group:']))
    if restore is not None:
        run(['setfacl', '--no-mask', '-m', f'u:{uid}:{restore}', str(path)])
        # The grant widened the mask for its own entry; an entry the mask had
        # silenced before must not keep the directory 0710 now (Audit, SYRD-687).
        before = bits(restore) & (old_mask if prior_mask is None else bits(prior_mask))
        need = 0
        for k, value in acl_map(read_acl(path)).items():
            if k.startswith(('user:', 'group:')) and k != 'user:':
                need |= before if k == key else bits(value)
        run(['setfacl', '--no-mask', '-m', f'm::{perms(old_mask & need)}', str(path)])
        return ''
    run(['setfacl', '--no-mask', '-x', f'u:{uid}', str(path)])
    rest = {k: v for k, v in acl_map(read_acl(path)).items() if k.startswith(('user:', 'group:')) and k != 'user:'}
    if any(k != 'group:' for k in rest):
        need = 0
        for value in rest.values():
            need |= bits(value)
        run(['setfacl', '--no-mask', '-m', f'm::{perms(old_mask & need)}', str(path)])
    elif 'mask:' in entries:
        run(['setfacl', '--no-mask', '-m', f'g::{perms(bits(rest["group:"]) & old_mask)}', str(path)])
        run(['setfacl', '-x', 'm::', str(path)])
    return ''


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
def gui_lock(runtime):
    path = runtime / '.switchyard-desktop.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# --- the exposure ---------------------------------------------------------------

#: Where every grant's exposure lives, relative to TRUST's directory. Root makes
#: it, and one directory in it per grant, `<gui uid>-<tenant uid>-<project>`,
#: owned by that grant's desktop owner. Short, because a hand-off's path is
#: typed into a role's pane and must fit one direct line (`image_handoff`).
EXPOSURE_ROOT = 'var/lib/switchyard/desktop'
#: The directory EXPOSURE_ROOT is resolved from, and the uid that must own it and
#: every directory down to EXPOSURE_ROOT. Nothing but a test moves it.
TRUST = ('/', 0)
#: In a grant's exposure: the relay socket, its receipt, and the image hand-off.
EXPOSURE_SOCKET = 'wayland-0'
EXPOSURE_RECEIPT = 'ready.json'
HANDOFF_DIR = 'paste'
#: What this form of the helper says about itself. An installed helper without
#: it is a legacy one that still keeps its tenant's entries on the runtime.
EXPOSURE_FORM = 'switchyard-desktop-exposure-v1'
#: The legacy grant's hand-off directory, in the owner's runtime directory.
LEGACY_HANDOFF_DIR = 'switchyard-paste'

_DIR_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _leaf(name):
    return name.removeprefix('switchyard-desktop-')


def exposure_path(name) -> Path:
    return Path(TRUST[0]) / EXPOSURE_ROOT / _leaf(name)


def _open_dir(parent, name, path, owner, *, create=None):
    """`name` in the directory `parent` as a real directory `owner` owns that nobody else may write."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if create is None:
            raise
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, create, dir_fd=parent)
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except NotADirectoryError:
        raise DesktopAccessError(f'{path} is not a directory') from None
    info = os.fstat(fd)
    if info.st_uid != owner or info.st_mode & 0o022:
        os.close(fd)
        raise DesktopAccessError(f'{path} is not a directory only uid {owner} may write; refusing to use it')
    return fd


def _exposure_root(*, create=False):
    """A descriptor for EXPOSURE_ROOT, every directory from TRUST down proven TRUST's owner's."""
    base, owner = TRUST
    fd = os.open(base, _DIR_FLAGS)
    try:
        info = os.fstat(fd)
        if info.st_uid != owner or info.st_mode & 0o022:
            raise DesktopAccessError(f'{base} is not a directory only uid {owner} may write')
        here = Path(base)
        for part in Path(EXPOSURE_ROOT).parts:
            here = here / part
            parent = fd
            fd = -1
            try:
                fd = _open_dir(parent, part, here, owner, create=0o755 if create else None)
            finally:
                os.close(parent)
        return fd
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise


def exposure_dir(gui, name, *, create=False) -> Path:
    """The grant's exposure directory, proven: TRUST's owner's from `/` down, then the desktop owner's own.

    Only TRUST's owner -- root -- may `create` it, once, at install: nothing
    under EXPOSURE_ROOT is the desktop owner's or the tenant's to make, so a
    tenant cannot plant one and the owner cannot place one anywhere else.
    """
    path = exposure_path(name)
    name = _leaf(name)
    create = create and os.geteuid() == TRUST[1]
    try:
        root = _exposure_root(create=create)
    except FileNotFoundError:
        raise ExposureMissing(f'{path} does not exist; root makes it once: run switchyard upgrade for this project '
                              'as root') from None
    try:
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=root)
        except FileNotFoundError:
            if not create:
                raise ExposureMissing(f'{path} does not exist; root makes it once: run switchyard upgrade for this '
                                      'project as root') from None
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=root)
            fd = os.open(name, _DIR_FLAGS, dir_fd=root)
        except NotADirectoryError:
            raise DesktopAccessError(f'{path} is not a directory') from None
        try:
            info = os.fstat(fd)
            if create and info.st_uid == TRUST[1] != gui.pw_uid:
                os.chown(name, gui.pw_uid, gui.pw_gid, dir_fd=root, follow_symlinks=False)
                info = os.fstat(fd)
            if info.st_uid != gui.pw_uid or info.st_mode & 0o022:
                raise DesktopAccessError(f'{path} is not a directory only {gui.pw_name} may write; refusing to use it')
        finally:
            os.close(fd)
    finally:
        os.close(root)
    return path


def exposure_for(policy) -> Path:
    """The policy's own exposure directory, proven as `exposure_dir` proves it."""
    gui, _, name = names(policy)
    return exposure_dir(gui, name)


def expose(policy):
    """As the desktop owner: the grant's exposure with the tenant's two entries, traverse and list hand-offs."""
    gui, tenant, name = names(policy)
    leaf = exposure_dir(gui, name)
    paste = _owned_directory(leaf / HANDOFF_DIR, gui.pw_uid, create=True)
    for path, wanted in ((leaf, '--x'), (paste, 'r-x')):
        if effective_acl(path, tenant.pw_uid) != bits(wanted):
            grant(path, tenant.pw_uid, wanted)
    return leaf


def revoke_exposure(policy):
    """As the desktop owner: withdraw the exposure -- its socket, receipt, hand-offs and both tenant entries."""
    gui, tenant, name = names(policy)
    try:
        leaf = exposure_dir(gui, name)
    except ExposureMissing:
        return
    remove_handoffs(leaf)
    for entry in (EXPOSURE_SOCKET, EXPOSURE_RECEIPT):
        (leaf / entry).unlink(missing_ok=True)
    drop_entry(leaf, tenant.pw_uid)


#: The most descriptors one message can carry: the kernel's SCM_MAX_FD.
MAX_FDS = 253


def peer_uid(conn):
    return struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))[1]


def _pump(source, sink):
    """Copy one direction of a Wayland connection, every descriptor with the bytes it arrived with."""
    try:
        while True:
            data, fds, flags, _ = socket.recv_fds(source, 65536, MAX_FDS, socket.MSG_CMSG_CLOEXEC)
            try:
                if not data or flags & socket.MSG_CTRUNC:
                    return
                sent = socket.send_fds(sink, [data], fds) if fds else sink.send(data)
                if sent < len(data):
                    sink.sendall(data[sent:])
            finally:
                for fd in fds:
                    os.close(fd)
    except OSError:
        return
    finally:
        for end in (source, sink):
            with contextlib.suppress(OSError):
                end.shutdown(socket.SHUT_RDWR)


class WaylandRelay:
    """The tenant's one way to the desktop: a socket in its exposure, every connection checked, then relayed.

    A connection is served only when its peer is the tenant's uid
    (SO_PEERCRED), and is relayed to the compositor socket the policy selects
    at that moment -- so a socket the compositor recreates or renames at the
    next login is followed without the tenant's WAYLAND_DISPLAY changing. Only
    the Wayland protocol crosses, descriptors included; the owner's session bus
    and the rest of its runtime directory stay out of reach. Stopping the
    service closes the socket and every relayed connection: revocation is
    immediate.
    """

    def __init__(self, policy, leaf, *, log=None):
        self.gui, self.tenant, self.name = names(policy)
        self.path = Path(leaf) / EXPOSURE_SOCKET
        self.upstream = None
        self.log = log or (lambda message: print(message, file=sys.stderr, flush=True))
        self.refused = set()
        with contextlib.suppress(FileNotFoundError):
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise DesktopAccessError(f'{self.path} is not a socket this account made; refusing to replace it')
            self.path.unlink()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        self.listener.bind(str(self.path))
        # The directory admits only the owner and the tenant; the peer check
        # below is what refuses everyone but the tenant.
        os.chmod(self.path, 0o666)
        self.listener.listen(64)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            uid = peer_uid(conn)
            if uid != self.tenant.pw_uid:
                if uid not in self.refused:
                    self.refused.add(uid)
                    self.log(f'Wayland access: refused a connection from uid {uid}; only {self.tenant.pw_name} '
                             f'({self.tenant.pw_uid}) is approved')
                return
            target = self.upstream
            if target is None:
                return
            with contextlib.suppress(OSError):
                info = target.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.gui.pw_uid:
                    return
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC) as upstream:
                    upstream.connect(str(target))
                    back = threading.Thread(target=_pump, args=(upstream, conn), daemon=True)
                    back.start()
                    _pump(conn, upstream)
                    back.join()

    def close(self):
        with contextlib.suppress(OSError):
            self.listener.shutdown(socket.SHUT_RDWR)
        self.listener.close()
        with contextlib.suppress(OSError):
            if os.lstat(self.path).st_uid == os.geteuid():
                os.unlink(self.path)


def record_receipt(policy, leaf, *, upstream=None, reason=''):
    """The readiness receipt the tenant checks before launch: the policy it is for, and ready or why not."""
    gui, tenant, _ = names(policy)
    value = {'form': EXPOSURE_FORM, 'policy_digest': digest(policy), 'gui_uid': gui.pw_uid, 'tenant_uid': tenant.pw_uid,
             'socket': str(Path(leaf) / EXPOSURE_SOCKET)}
    value.update({'state': 'ready', 'upstream': upstream.name} if upstream else {'state': 'pending', 'reason': reason})
    receipt = Path(leaf) / EXPOSURE_RECEIPT
    with contextlib.suppress(OSError, ValueError):
        if json.loads(receipt.read_text()) == value:
            return value
    atomic_json(receipt, value, mode=0o644)
    return value


def refresh(policy, relay, leaf):
    """One pass of the service: select the compositor socket again, and say in the receipt whether it is ready."""
    gui = relay.gui
    try:
        path = selected_socket(policy, gui, runtime_for(gui))
    except (DesktopAccessError, OSError) as exc:
        relay.upstream = None
        record_receipt(policy, leaf, reason=str(exc))
        print(f'Wayland access pending: {exc}', file=sys.stderr, flush=True)
        return None
    if path != relay.upstream and policy['wayland_display'] not in ('auto', path.name):
        print(f"Wayland access: configured socket {policy['wayland_display']} is absent from {path.parent}; "
              f'relaying to the only live Wayland socket there, {path.name}', file=sys.stderr, flush=True)
    relay.upstream = path
    record_receipt(policy, leaf, upstream=path)
    return path


def watch(policy, *, interval=5):
    """The desktop owner's service: the relay, kept pointed at the approved socket, and hand-offs swept."""
    gui, tenant, name = names(policy)
    if os.geteuid() != gui.pw_uid:
        raise DesktopAccessError('The desktop access service must run as the configured GUI owner')
    leaf = expose(policy)
    relay = WaylandRelay(policy, leaf)
    try:
        while True:
            refresh(policy, relay, leaf)
            # The owner's reliable cleanup for handed-over images (SYRD-247).
            sweep_handoffs(leaf)
            time.sleep(interval)
    finally:
        relay.close()


def await_receipt(policy, *, within=15.0):
    """The receipt a just-started service writes for this policy, or a refusal naming its journal."""
    gui, tenant, name = names(policy)
    receipt = exposure_path(name) / EXPOSURE_RECEIPT
    deadline = time.monotonic() + within
    while True:
        with contextlib.suppress(OSError, ValueError):
            value = json.loads(receipt.read_text())
            if value.get('form') == EXPOSURE_FORM and value.get('policy_digest') == digest(policy):
                return value
        if time.monotonic() >= deadline:
            raise DesktopAccessError(f'{name}.service wrote no readiness receipt in {within:.0f} seconds; as '
                                     f'{gui.pw_name}, run: journalctl --user -u {name}.service')
        time.sleep(0.1)


def wayland_roundtrip(path, *, timeout=3.0):
    """One `wl_display.sync` answered by its `done`: the compositor is there, and nothing is read from it."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC) as probe:
        probe.settimeout(timeout)
        probe.connect(str(path))
        buffer, asked = b'', False
        while True:
            try:
                if not asked:
                    probe.sendall(struct.pack('=III', 1, 12 << 16, 2))
                    asked = True
                chunk = probe.recv(4096)
            except (BrokenPipeError, ConnectionResetError):
                chunk = b''
            if not chunk:
                raise DesktopAccessError(f'{path} closed the connection before the compositor answered')
            buffer += chunk
            while len(buffer) >= 8:
                obj, word = struct.unpack_from('=II', buffer)
                size = word >> 16
                if size < 8:
                    raise DesktopAccessError(f'{path} answered with something that is not Wayland')
                if len(buffer) < size:
                    break
                if obj == 2 and word & 0xffff == 0:
                    return
                if obj == 1 and word & 0xffff == 0:
                    raise DesktopAccessError(f'the compositor behind {path} reported a protocol error')
                buffer = buffer[size:]


# --- the image hand-off -----------------------------------------------------------

#: How long a clipboard image handed to a tenant in `<exposure>/paste/` exists
#: (SYRD-247), from the copy. The owner's service removes it within one pass.
HANDOFF_TTL_SECONDS = 15 * 60
#: The only names a handoff directory holds: an image and its manifest, sharing
#: one random stem, plus the manifest's temporary name before it is renamed in.
HANDOFF_NAME = re.compile(r'(?:\.tmp-)?paste-[0-9a-f]{32}\.(?:png|jpg|gif|webp|bmp|handoff\.json)')


def handoff_dir(leaf):
    return Path(leaf) / HANDOFF_DIR


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


def prepare_handoff_dir(leaf, tenant_uid):
    """As the GUI owner: the exposure's hand-off directory, which the tenant may list and read.

    The exposure's traverse entry is the access service's (`expose`); without
    it the tenant can reach nothing in here, so nothing is handed over.
    """
    leaf = Path(leaf)
    owner = os.geteuid()
    if leaf.lstat().st_uid != owner or leaf.is_symlink():
        raise DesktopAccessError(f'{leaf} is not this account\'s exposure directory; run this as the desktop owner')
    if not effective_acl(leaf, tenant_uid) & 1:
        raise DesktopAccessError(f'the approved desktop grant is not applied to {leaf}; start the project\'s desktop access service first')
    paste = _owned_directory(handoff_dir(leaf), owner, create=True)
    if effective_acl(paste, tenant_uid) != bits('r-x'):
        grant(paste, tenant_uid, 'r-x')
    return paste


def _handoff_entries(directory):
    try:
        _owned_directory(directory, os.geteuid(), create=False)
        with os.scandir(directory) as entries:
            return [entry for entry in entries if HANDOFF_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)]
    except (OSError, DesktopAccessError):
        return []


def sweep_handoffs(leaf, *, now=None, ttl=HANDOFF_TTL_SECONDS):
    """As the GUI owner: remove this exposure's handed-over files older than `ttl`.

    Run by the owner's access service on every watch pass, so an image leaves
    within one interval of expiring whether or not anything is pasted again.
    """
    now = time.time() if now is None else now
    removed = []
    for entry in _handoff_entries(handoff_dir(leaf)):
        with contextlib.suppress(OSError):
            if now - entry.stat(follow_symlinks=False).st_mtime >= ttl:
                os.unlink(entry.path)
                removed.append(entry.name)
    return removed


def remove_handoffs(leaf, directory=None):
    """As the GUI owner: remove a hand-off directory and what is in it, as a grant is withdrawn."""
    directory = directory or handoff_dir(leaf)
    for entry in _handoff_entries(directory):
        with contextlib.suppress(OSError):
            os.unlink(entry.path)
    with contextlib.suppress(OSError):
        os.rmdir(directory)


# --- what a launch checks ---------------------------------------------------------


def verify(policy):
    gui, tenant, name = names(policy)
    if os.geteuid() != tenant.pw_uid:
        raise DesktopAccessError('Readiness validation must execute as the tenant')
    tenant_runtime = runtime_for(tenant)
    session = {'XDG_RUNTIME_DIR': str(tenant_runtime), 'DBUS_SESSION_BUS_ADDRESS': f'unix:path={tenant_runtime}/bus'}
    try:
        leaf = exposure_dir(gui, name)
    except ExposureMissing as exc:
        legacy = _legacy_socket(policy, gui, name)
        if legacy is None:
            raise DesktopAccessError(f'Desktop access is not ready before launch: {exc}') from exc
        print(f'switchyard: {policy["project"]} still reaches the desktop through the legacy grant on '
              f'{legacy.parent}; switchyard upgrade {policy["project"]} replaces it', file=sys.stderr)
        return {'WAYLAND_DISPLAY': str(legacy), **session}
    receipt = leaf / EXPOSURE_RECEIPT
    try:
        info = receipt.lstat()
    except (FileNotFoundError, PermissionError) as exc:
        raise DesktopAccessError(
            f"Desktop access is not ready before launch: {gui.pw_name}'s desktop access service has not recorded "
            f'{tenant.pw_name} as ready ({exc}). It retries every 5 seconds and logs why it is waiting; as '
            f'{gui.pw_name}, run: journalctl --user -u {name}.service') from exc
    try:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != gui.pw_uid or info.st_mode & 0o022:
            raise DesktopAccessError('Desktop readiness record is not protected by the GUI owner')
        value = json.loads(receipt.read_text())
        if value.get('policy_digest') != digest(policy) or value.get('form') != EXPOSURE_FORM:
            raise DesktopAccessError('Desktop policy has not been installed by its GUI owner; run the supported upgrade with this policy before launch')
        if value.get('state') != 'ready':
            raise DesktopAccessError(f"Desktop access is not ready before launch: {gui.pw_name}'s desktop access service "
                                     f"is waiting: {value.get('reason')}")
        path = leaf / EXPOSURE_SOCKET
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != gui.pw_uid:
            raise DesktopAccessError(f'{path} is not the desktop access service\'s socket')
        wayland_roundtrip(path)
    except (OSError,ValueError,KeyError) as exc:
        raise DesktopAccessError(f'Desktop access is not ready before launch: {exc}. Run supported upgrade with the approved policy, or select headless.') from exc
    return {'WAYLAND_DISPLAY': str(path), **session}


def _legacy_socket(policy, gui, name):
    """The socket a not-yet-upgraded grant's service still keeps for the tenant on the owner's runtime, or None.

    A release can be deployed before each project is upgraded onto it; until
    then that project's own service keeps its legacy receipt and entries, and
    its launches keep using them. Nothing here grants anything.
    """
    try:
        ready = runtime_for(gui) / (name + '.json')
        info = ready.lstat()
        value = json.loads(ready.read_text())
    except (OSError, ValueError, DesktopAccessError):
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != gui.pw_uid or info.st_mode & 0o022:
        raise DesktopAccessError('Desktop readiness record is not protected by the GUI owner')
    if value.get('policy_digest') != digest(policy) or value.get('state') == 'pending' or 'socket' not in value:
        return None
    path = Path(value['socket'])
    try:
        info = path.lstat()
        # Device and inode: the legacy grants of other tenants' services change the
        # socket's ctime on every pass, so the receipt's ctime is rarely current.
        if (path.parent != ready.parent or not stat.S_ISSOCK(info.st_mode) or info.st_uid != gui.pw_uid
                or value.get('socket_identity', [])[:2] != [info.st_dev, info.st_ino]):
            return None
        wayland_roundtrip(path)
    except (OSError, DesktopAccessError):
        return None
    return path


# --- installing and withdrawing ----------------------------------------------------


def gui_command(gui, runtime, args):
    # This is a GUI-owner process, never substitution of a tenant's runtime/bus.
    command = ['env',f'XDG_RUNTIME_DIR={runtime}',f'DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus',*args]
    if os.geteuid() == gui.pw_uid:
        return command
    if os.geteuid() != 0:
        raise DesktopAccessError('Initial Wayland setup requires the GUI owner or operator during new/upgrade, before role launch')
    return ['runuser','-u',gui.pw_name,'--',*command]


def owner_paths(gui, name):
    """The policy copy, helper and unit an install leaves in the desktop owner's home."""
    home = Path(gui.pw_dir)
    return (home/'.config/switchyard/desktop-access'/(name+'.json'), home/'.local/lib/switchyard/desktop-access'/(name+'.py'),
            home/'.config/systemd/user'/(name+'.service'))


def install(policy, *, helper: Path):
    gui, tenant, name = names(policy)
    runtime = runtime_for(gui)
    selected_socket(policy,gui,runtime)
    run(gui_command(gui,runtime,['which','setfacl','getfacl']))
    if not helper.is_absolute() or not helper.is_file():
        raise DesktopAccessError('Installed desktop-access helper is missing')
    # Root makes the exposure; the owner's own install only finds it.
    exposure_dir(gui, name, create=True)
    # Copy reviewed release code into GUI-owned state. Its service must not depend
    # on another user's private checkout, nor on an export later being removed.
    payload = {'policy':policy, 'helper':helper.read_text(), 'python':sys.executable}
    run(gui_command(gui,runtime,[sys.executable,'-c',helper.read_text(),'install-owner',json.dumps(payload)]))


def install_owner(payload, *, ready_within=15.0):
    policy = validate_policy(payload['policy'],project=payload['policy']['project'],tenant=payload['policy']['tenant_user'])
    gui,tenant,name = names(policy)
    if os.geteuid() != gui.pw_uid:
        raise DesktopAccessError('Policy installation must run as GUI owner')
    policy_path, helper_path, unit_path = owner_paths(gui, name)
    backup = helper_path.with_name(helper_path.name + '.previous')
    paths = (policy_path, helper_path, unit_path, backup)
    if unit_path.exists() and not policy_path.exists():
        raise DesktopAccessError(f'Existing unmanaged unit {unit_path}; preserve it and resolve the conflict before launch')
    if policy_path.exists() and json.loads(policy_path.read_text()) != policy:
        raise DesktopAccessError('Existing desktop grant differs; revoke that scoped policy through upgrade before replacing it')
    previous = {path:path.read_bytes() if path.exists() else None for path in paths}
    was_installed = policy_path.exists()
    # A service still on the legacy form has no exposure of its own to fall back to.
    exposed_before = was_installed and EXPOSURE_FORM.encode() in (previous[helper_path] or b'')
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
    try:
        atomic_json(policy_path,policy)
        helper_path.parent.mkdir(parents=True,exist_ok=True)
        if was_installed and previous[helper_path] not in (None, payload['helper'].encode()):
            # Kept until the cutover completes, so `revert_owner` can put it back.
            backup.write_bytes(previous[helper_path])
            backup.chmod(0o600)
        helper_path.write_text(payload['helper'])
        helper_path.chmod(0o600)
        unit_path.parent.mkdir(parents=True,exist_ok=True)
        unit_path.write_text(unit)
        leaf = expose(policy)
        # Whatever is there was written by a service that may not be this one.
        (leaf / EXPOSURE_RECEIPT).unlink(missing_ok=True)
        run(['systemctl','--user','daemon-reload'])
        run(['systemctl','--user','enable','--now',name+'.service'])
        # Apply new helper version to the service without touching worker panes.
        if was_installed and previous[helper_path] != payload['helper'].encode():
            run(['systemctl','--user','restart',name+'.service'])
        run(['systemctl','--user','is-active',name+'.service'])
        await_receipt(policy, within=ready_within)
    except Exception:
        if not was_installed:
            subprocess.run(['systemctl','--user','disable','--now',name+'.service'],capture_output=True)
        for path,content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        subprocess.run(['systemctl','--user','daemon-reload'],capture_output=True)
        if was_installed:
            subprocess.run(['systemctl','--user','restart',name+'.service'],capture_output=True)
        if not exposed_before:
            revoke_exposure(policy)
        raise


def revert_owner(policy):
    """As the owner: put back the helper this install replaced, when the cutover after it cannot complete.

    The legacy entries were never touched -- they come off only once the
    tenant has verified the exposure -- so the restored legacy service carries
    on with them, and the exposure it never had is withdrawn.
    """
    gui, tenant, name = names(policy)
    _, helper_path, _ = owner_paths(gui, name)
    backup = helper_path.with_name(helper_path.name + '.previous')
    if not backup.exists():
        return
    previous = backup.read_bytes()
    helper_path.write_bytes(previous)
    backup.unlink()
    run(['systemctl', '--user', 'daemon-reload'])
    run(['systemctl', '--user', 'restart', name + '.service'])
    if EXPOSURE_FORM.encode() not in previous:
        revoke_exposure(policy)


def owner_action(policy, action, *, helper=None):
    """Root's or the owner's call: run one owner action of this release's helper as the desktop owner."""
    gui, tenant, name = names(policy)
    runtime = runtime_for(gui)
    code = Path(helper or __file__).read_text()
    output = run(gui_command(gui, runtime, [sys.executable, '-c', code, action, json.dumps(policy)]))
    return [line for line in output.splitlines() if line.strip()]


def retire_legacy_access(policy, *, helper=None):
    """Root's or the owner's call: as the owner, retire this grant's legacy entries; the lines to show."""
    return owner_action(policy, 'retire-legacy', helper=helper)


def legacy_peers(gui, tenant, name):
    """The owner's other grants to this tenant whose installed helper is still the legacy one: they need its entries."""
    policies, helpers, _ = (path.parent for path in owner_paths(gui, name))
    peers = []
    prefix = f'switchyard-desktop-{gui.pw_uid}-{tenant.pw_uid}-'
    for policy_path in sorted(policies.glob(prefix + '*.json')):
        if policy_path.stem == name:
            continue
        try:
            text = (helpers / (policy_path.stem + '.py')).read_text()
        except OSError:
            continue
        if EXPOSURE_FORM not in text:
            peers.append(policy_path.stem[len(prefix):])
    return peers


def retire_legacy(policy):
    """As the desktop owner: take this grant's legacy entries off the owner's runtime directory, its sockets and hand-off.

    Run once the exposure is verified. Only what Switchyard set is taken: the
    tenant's `--x` on the runtime directory and on the legacy hand-off parent,
    and its `rw-` on a Wayland socket there -- or, where the legacy receipt
    recorded an entry from before the grant, that entry is put back. Every
    other entry stays, and the mask only narrows. A peer grant to the same
    tenant still on the legacy helper keeps the shared entries until it is
    upgraded too. Returns what was done and how the directory now reads.
    """
    gui, tenant, name = names(policy)
    if os.geteuid() != gui.pw_uid:
        raise DesktopAccessError('Retiring the legacy grant must run as the GUI owner')
    runtime = runtime_for(gui)
    notes = []
    with gui_lock(runtime):
        ready = runtime / (name + '.json')
        try:
            grants = json.loads(ready.read_text()).get('grants', [])
        except (OSError, ValueError, AttributeError):
            grants = []
        key = f'user:{tenant.pw_uid}'
        before = {g.get('path'): acl_map(g.get('before', [])) for g in grants if isinstance(g, dict)}
        legacy_paste = runtime / LEGACY_HANDOFF_DIR
        remove_handoffs(None, legacy_paste / policy['project'])
        peers = legacy_peers(gui, tenant, name)
        if peers:
            notes.append(f'kept {key} on {runtime} for {", ".join(peers)}, still on the legacy grant; '
                         'switchyard upgrade each of them to retire it')
        else:
            targets = [(runtime, '--x'), *((sock, 'rw-') for sock in wayland_sockets(runtime))]
            if legacy_paste.is_dir() and not legacy_paste.is_symlink():
                targets.append((legacy_paste, '--x'))
            for path, expected in targets:
                prior = before.get(str(path), {})
                note = drop_entry(path, tenant.pw_uid, expected=expected, restore=prior.get(key),
                                  prior_mask=prior.get('mask:', prior.get('group:')))
                if note:
                    notes.append(note)
            with contextlib.suppress(OSError):
                legacy_paste.rmdir()
        ready.unlink(missing_ok=True)
    helper_path = owner_paths(gui, name)[1]
    helper_path.with_name(helper_path.name + '.previous').unlink(missing_ok=True)
    notes.append(runtime_mode_problem(runtime, gui.pw_uid) or f'{runtime} is 0700')
    return notes


def uninstall(policy, *, helper=None):
    """Withdraw the grant as its owner, with this release's code; root then removes the exposure directory it made."""
    gui,tenant,name = names(policy)
    owner_action(policy, 'uninstall-owner', helper=helper)
    if os.geteuid() == TRUST[1]:
        with contextlib.suppress(FileNotFoundError):
            root = _exposure_root()
            try:
                os.rmdir(_leaf(name), dir_fd=root)
            finally:
                os.close(root)


def uninstall_owner(policy):
    gui,tenant,name = names(policy)
    if os.geteuid() != gui.pw_uid:
        raise DesktopAccessError('Policy removal must execute as GUI owner')
    run(['systemctl','--user','disable','--now',name+'.service'])
    rollback(policy)
    for path in owner_paths(gui, name):
        path.unlink(missing_ok=True)
    owner_paths(gui, name)[1].with_name(name + '.py.previous').unlink(missing_ok=True)
    run(['systemctl','--user','daemon-reload'])


def rollback(policy):
    """As the owner: withdraw the exposure and whatever legacy entries this grant still holds."""
    gui,tenant,name = names(policy)
    if os.geteuid() != gui.pw_uid:
        raise DesktopAccessError('Rollback must run as GUI owner')
    revoke_exposure(policy)
    for line in retire_legacy(policy)[:-1]:
        print(line, file=sys.stderr)


def repair(*, apply=False, registry=Path('/etc/switchyard/projects')):
    """As the desktop owner: every Switchyard grant on this desktop, and what still keeps the runtime off 0700.

    With `apply`: a grant whose project is no longer registered is withdrawn,
    and a grant already on the exposure has any legacy entries it left
    retired. A grant still on the legacy helper is named with the upgrade that
    moves it; an entry no installed grant accounts for is named and left.
    """
    me = pwd.getpwuid(os.geteuid())
    runtime = runtime_for(me)
    policies_dir = owner_paths(me, 'x')[0].parent
    lines, tenants = [], set()
    for path in sorted(policies_dir.glob(f'switchyard-desktop-{me.pw_uid}-*.json')):
        try:
            raw = json.loads(path.read_text())
            policy = validate_policy(raw, project=raw['project'], tenant=raw['tenant_user'])
            gui, tenant, name = names(policy)
        except (OSError, ValueError, KeyError, TypeError, DesktopAccessError) as exc:
            lines.append(f'{path}: not a usable policy ({exc}); left as it is')
            continue
        if name != path.stem or gui.pw_uid != me.pw_uid:
            lines.append(f'{path}: names {name}, not itself; left as it is')
            continue
        tenants.add(tenant.pw_uid)
        label = f"{policy['project']} ({tenant.pw_name})"
        try:
            legacy = EXPOSURE_FORM not in owner_paths(me, name)[1].read_text()
        except OSError:
            legacy = True
        if not (Path(registry) / f"{policy['project']}.json").exists():
            if apply:
                uninstall_owner(policy)
            lines.append(f'{label}: not a registered project; ' + ('its grant was withdrawn' if apply else
                         'repair --apply withdraws its grant'))
        elif legacy:
            lines.append(f"{label}: still on the legacy runtime-directory grant; run: sudo switchyard upgrade {policy['project']}")
        elif apply:
            lines.extend(f'{label}: {line}' for line in retire_legacy(policy)[:-1])
            lines.append(f'{label}: on its exposure, {exposure_path(name)}')
        else:
            lines.append(f'{label}: on its exposure, {exposure_path(name)}')
    for key in acl_map(read_acl(runtime)):
        kind, _, who = key.partition(':')
        if kind == 'user' and who and int(who) not in tenants:
            lines.append(f'{key} on {runtime}: no Switchyard grant on this desktop names it; left as it is')
    problem = runtime_mode_problem(runtime, me.pw_uid)
    lines.append(problem or f'{runtime} is 0700')
    return (1 if problem else 0), lines


def teardown(project, tenant_name):
    """As root, at teardown: withdraw every desktop grant made for this project and tenant, on any desktop."""
    try:
        tenant = pwd.getpwnam(tenant_name)
    except KeyError:
        return [f'no account {tenant_name}, so no desktop grant to withdraw for it']
    lines = []
    for gui in pwd.getpwall():
        name = f'switchyard-desktop-{gui.pw_uid}-{tenant.pw_uid}-{project}'
        try:
            raw = json.loads(owner_paths(gui, name)[0].read_text())
        except (FileNotFoundError, NotADirectoryError):
            continue
        except PermissionError as exc:
            lines.append(f"{gui.pw_name}'s home could not be read, so a grant there was not looked for: {exc}")
            continue
        except (OSError, ValueError) as exc:
            raise DesktopAccessError(f"{gui.pw_name}'s desktop grant for {project} cannot be read: {exc}") from exc
        policy = validate_policy(raw, project=project, tenant=tenant_name)
        if policy.get('gui_user') != gui.pw_name:
            raise DesktopAccessError(f"{gui.pw_name}'s desktop grant for {project} names {policy.get('gui_user')}; left as it is")
        uninstall(policy)
        lines.append(f"withdrew {project}'s desktop grant on {gui.pw_name}'s desktop")
    if not any(line.startswith('withdrew') for line in lines):
        lines.append(f'{project} holds no desktop grant')
    return lines


def _policy_argument(text):
    return json.loads(text) if text.lstrip().startswith('{') else json.loads(Path(text).read_text())


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['watch','verify','install-owner','uninstall-owner','rollback','retire-legacy',
                                          'revert-owner','repair','teardown'])
    parser.add_argument('policy', nargs='?', help='the policy, inline or as a file; for teardown, the project')
    parser.add_argument('tenant', nargs='?', help='teardown: the tenant account')
    parser.add_argument('--apply', action='store_true', help='repair: withdraw and retire, not only report')
    args=parser.parse_args(argv)
    if args.action == 'install-owner':
        install_owner(json.loads(args.policy)); return 0
    if args.action == 'repair':
        code, lines = repair(apply=args.apply)
        print('\n'.join(lines)); return code
    if args.action == 'teardown':
        print('\n'.join(teardown(args.policy, args.tenant))); return 0
    raw=_policy_argument(args.policy)
    policy=validate_policy(raw,project=raw['project'],tenant=raw['tenant_user'])
    if args.action=='verify':
        print(json.dumps(verify(policy)));return 0
    owner = {'uninstall-owner': uninstall_owner, 'rollback': rollback, 'revert-owner': revert_owner}
    if args.action in owner:
        owner[args.action](policy);return 0
    if args.action=='retire-legacy':
        print('\n'.join(retire_legacy(policy)));return 0
    watch(policy)


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (DesktopAccessError,OSError,ValueError) as exc:
        raise SystemExit(f'switchyard desktop access: {exc}')
