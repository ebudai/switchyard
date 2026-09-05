#!/usr/bin/env python3
"""Scoped Wayland policy installation and pre-launch readiness (no clipboard reads)."""
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
import subprocess
import sys
import tempfile
import time


class DesktopAccessError(RuntimeError):
    pass


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


def selected_socket(policy, gui, runtime):
    sessions = run(['loginctl', 'show-user', gui.pw_name, '-p', 'Sessions', '--value']).split()
    active = False
    for session in sessions:
        values = dict(line.split('=',1) for line in run(['loginctl','show-session',session,'-p','Type','-p','Active','-p','User']).splitlines() if '=' in line)
        active |= values.get('Type') == 'wayland' and values.get('Active') == 'yes' and values.get('User') == str(gui.pw_uid)
    if not active:
        raise DesktopAccessError(f'{gui.pw_name} has no active Wayland session; log into the selected desktop or choose headless')
    if policy['wayland_display'] == 'auto':
        candidates = [p for p in runtime.glob('wayland-*') if p.is_socket() and not p.is_symlink()]
        if len(candidates) != 1:
            raise DesktopAccessError('Wayland socket discovery is missing or ambiguous; select a socket basename in the policy')
        path = candidates[0]
    else:
        path = runtime / policy['wayland_display']
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
def gui_lock(runtime):
    path = runtime / '.switchyard-desktop.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def apply(policy):
    gui, tenant, name = names(policy)
    if os.geteuid() != gui.pw_uid:
        raise DesktopAccessError('ACL reapplication must execute as the configured GUI owner')
    runtime = runtime_for(gui)
    path = selected_socket(policy, gui, runtime)
    ready = runtime / (name+'.json')
    with gui_lock(runtime):
        previous = json.loads(ready.read_text()) if ready.exists() else {}
        info = path.stat()
        if previous.get('policy_digest') == digest(policy) and previous.get('socket_identity') == [info.st_dev,info.st_ino,info.st_ctime_ns]:
            # Check ACLs as well: revocation should not be hidden by a stale marker.
            if effective_acl(runtime,tenant.pw_uid) == 1 and effective_acl(path,tenant.pw_uid) == 6:
                return ready
        records = []
        try:
            records.append(grant(runtime, tenant.pw_uid, '--x'))
            records.append(grant(path, tenant.pw_uid, 'rw-'))
            info = path.stat()
            # Preserve original rollback state for the runtime and same socket.
            original_records = list(previous.get('grants', []))
            for peer in runtime.glob(f'switchyard-desktop-{gui.pw_uid}-{tenant.pw_uid}-*.json'):
                if peer != ready:
                    original_records.extend(json.loads(peer.read_text()).get('grants', []))
            originals = {(r['device'],r['inode']):r for r in original_records}
            for r in records:
                prior = originals.get((r['device'],r['inode']))
                if prior:
                    r['before'] = prior['before']
            atomic_json(ready, {'policy_digest':digest(policy), 'socket':str(path),
                'socket_identity':[info.st_dev,info.st_ino,info.st_ctime_ns],
                'gui_uid':gui.pw_uid, 'tenant_uid':tenant.pw_uid, 'grants':records}, mode=0o644)
        except Exception:
            for record in reversed(records):
                restore_grant(record)
            raise
    return ready


def verify(policy):
    gui, tenant, name = names(policy)
    if os.geteuid() != tenant.pw_uid:
        raise DesktopAccessError('Readiness validation must execute as the tenant')
    gui_runtime = runtime_for(gui)
    tenant_runtime = runtime_for(tenant)
    ready = gui_runtime / (name+'.json')
    try:
        info = ready.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != gui.pw_uid or info.st_mode & 0o022:
            raise DesktopAccessError('Desktop readiness record is not protected by the GUI owner')
        value = json.loads(ready.read_text())
        if value.get('policy_digest') != digest(policy):
            raise DesktopAccessError('Desktop policy has not been installed by its GUI owner; run the supported upgrade with this policy before launch')
        path = Path(value['socket'])
        if path.parent != gui_runtime or path.is_symlink():
            raise DesktopAccessError('Desktop readiness refers to an invalid socket')
        info = path.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != gui.pw_uid or value['socket_identity'] != [info.st_dev,info.st_ino,info.st_ctime_ns]:
            raise DesktopAccessError('Wayland socket was recreated or changed; wait for the GUI access service to reapply before launching')
        if not os.access(gui_runtime,os.X_OK) or not os.access(path,os.R_OK|os.W_OK):
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
    try:
        atomic_json(policy_path,policy)
        helper_path.parent.mkdir(parents=True,exist_ok=True)
        helper_path.write_text(payload['helper'])
        helper_path.chmod(0o600)
        unit_path.parent.mkdir(parents=True,exist_ok=True)
        unit_path.write_text(unit)
        apply(policy)
        run(['systemctl','--user','daemon-reload'])
        run(['systemctl','--user','enable','--now',name+'.service'])
        # Apply new helper version to the service without touching worker panes.
        if was_installed and previous[helper_path] != payload['helper'].encode():
            run(['systemctl','--user','restart',name+'.service'])
        run(['systemctl','--user','is-active',name+'.service'])
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
    with gui_lock(runtime):
        ready = runtime/(name+'.json')
        if ready.exists():
            value = json.loads(ready.read_text())
            if value['policy_digest'] != digest(policy):
                raise DesktopAccessError('Rollback policy differs from active grant')
            peers = []
            for other in runtime.glob(f'switchyard-desktop-{gui.pw_uid}-{tenant.pw_uid}-*.json'):
                if other != ready:
                    peers.extend(json.loads(other.read_text()).get('grants', []))
            active = {(r['device'],r['inode']) for r in peers}
            for record in reversed(value['grants']):
                if (record['device'],record['inode']) not in active:
                    restore_grant(record)
            ready.unlink()


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
            apply(policy)
        except (DesktopAccessError,OSError) as exc:
            print(f'Wayland access pending: {exc}',file=sys.stderr,flush=True)
        time.sleep(5)


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (DesktopAccessError,OSError,ValueError) as exc:
        raise SystemExit(f'switchyard desktop access: {exc}')
