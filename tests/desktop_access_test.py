#!/usr/bin/env python3
"""SYRD-9 real filesystem/Unix-socket/ACL tests; loginctl/systemd are fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from scripts import desktop_access as desktop
from scripts import team_launcher as launcher
from wayland_fixture import FakeCompositor, echo_through


def policy(tenant='cerulean-worker', project='cerulean'):
    return desktop.validate_policy({'mode':'wayland', 'gui_user':'desktop-owner',
        'tenant_user':tenant, 'project':project,'wayland_display':'auto',
        'consent':{'approved':True,'by':'desktop-owner','at':'2026-09-05T22:27:00Z','reference':'recorded test consent'}},project=project,tenant=tenant)


def rejects(call):
    try:
        call()
    except (RuntimeError,SystemExit,OSError):
        return
    raise AssertionError('expected fail-closed result')


def test_validation():
    for raw in [None,{}, {'mode':'headless','gui_user':'desktop-owner'},
                {**policy(),'tenant_user':'someone-else'},
                {**policy(),'consent':{**policy()['consent'],'approved':False}},
                {**policy(),'wayland_display':'../../private'},
                {**policy(),'gui_user':'name\nExecStart=bad'},
                {**policy(),'consent':{**policy()['consent'],'at':'yesterday'}}]:
        rejects(lambda: desktop.validate_policy(raw,project='cerulean',tenant='cerulean-worker'))
    assert desktop.validate_policy({'mode':'headless'},project='cerulean',tenant='cerulean-worker')['mode']=='headless'


def test_exported_release_exposure_lifecycle():
    """An exported release's helper: install, the service's relay, verify, revocation and a failed install.

    Real files, ACLs, sockets and descriptors; logind, systemd and the accounts
    are fixtures. The relay serves only its tenant's uid, so here the tenant is
    this uid; `desktop_wayland_exposure_test` runs the same as real, separate
    accounts.
    """
    for tool in ['setfacl','getfacl']:
        assert shutil.which(tool), f'{tool} required; this suite must not silently skip'
    with tempfile.TemporaryDirectory(prefix='desktop-access-') as tmp:
        root=Path(tmp)
        # An exported installed-release layout: no .git, helper outside GUI home.
        release=root/'opt/switchyard/releases/fixture/scripts';release.mkdir(parents=True)
        shutil.copy2(ROOT/'scripts/desktop_access.py',release/'desktop_access.py')
        spec=importlib.util.spec_from_file_location('exported_desktop',release/'desktop_access.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        me=os.getuid()
        gui=SimpleNamespace(pw_name='desktop-owner',pw_uid=me,pw_gid=os.getgid(),pw_dir=str(root/'gui-home'))
        tenant=SimpleNamespace(pw_name='cerulean-worker',pw_uid=me,pw_gid=os.getgid(),pw_dir=str(root/'tenant-home'))
        gui_runtime=root/'gui-runtime';gui_runtime.mkdir(mode=0o700)
        tenant_runtime=root/'tenant-runtime';tenant_runtime.mkdir(mode=0o700)
        compositor=FakeCompositor(gui_runtime/'wayland-7')
        trust=root/'trust';trust.mkdir(mode=0o755)
        accounts={x.pw_name:x for x in [gui,tenant]}
        actual_run=mod.run
        calls=[];fail_enable=False;silent=False;services={}
        def start(name):
            """The unit's service: its relay, and a pass every tenth of a second until it is stopped."""
            if silent:return  # started, but never writes a receipt
            p=json.loads((Path(gui.pw_dir)/'.config/switchyard/desktop-access'/(name+'.json')).read_text())
            leaf=mod.expose(p);relay=mod.WaylandRelay(p,leaf,log=lambda _m:None)
            relay.stopped=threading.Event()
            def passes():
                while not relay.stopped.is_set():
                    mod.refresh(p,relay,leaf);relay.stopped.wait(0.1)
            mod.refresh(p,relay,leaf)
            threading.Thread(target=passes,daemon=True).start()
            services[name]=relay
        def stop(name):
            if name in services:
                relay=services.pop(name);relay.stopped.set();relay.close()
        def fixture_run(args):
            calls.append(args)
            if args[0]=='loginctl':
                if args[1]=='show-session':return f'Type=wayland\nActive=yes\nUser={gui.pw_uid}'
                if 'Sessions' in args:return 'fixture-session'
                return str(gui_runtime if args[2]==gui.pw_name else tenant_runtime)
            if args[0]=='systemctl':
                unit=args[-1].removesuffix('.service')
                if fail_enable and 'enable' in args:
                    raise mod.DesktopAccessError('fixture unit enable denied')
                if 'enable' in args and unit not in services:start(unit)
                if 'restart' in args:stop(unit);start(unit)
                if 'disable' in args:stop(unit)
                return 'active'
            return actual_run(args)
        with patch.object(mod.pwd,'getpwnam',side_effect=lambda name:accounts[name]), patch.object(mod,'run',side_effect=fixture_run), \
                patch.object(mod,'TRUST',(str(trust),me)), patch.object(mod.time,'sleep',lambda _s:None):
            p=policy();name=mod.names(p)[2]
            leaf=mod.exposure_dir(gui,name,create=True)
            check_mode=lambda path:oct(path.stat().st_mode&0o777)
            payload={'policy':p,'helper':(release/'desktop_access.py').read_text(),'python':sys.executable}
            mod.install_owner(payload)
            receipt=leaf/'ready.json'
            value=json.loads(receipt.read_text())
            assert value['state']=='ready' and value['upstream']=='wayland-7' and value['form']==mod.EXPOSURE_FORM, value
            assert check_mode(gui_runtime)=='0o700' and mod.read_acl(gui_runtime)==['user::rwx','group::---','other::---'], \
                'the owner\'s runtime directory takes no entry'
            assert mod.runtime_mode_problem(gui_runtime,me)==''
            assert mod.acl_map(mod.read_acl(leaf))[f'user:{me}']=='--x' and mod.acl_map(mod.read_acl(leaf/'paste'))[f'user:{me}']=='r-x'
            installed=Path(gui.pw_dir)/'.config/switchyard/desktop-access'/(name+'.json')
            assert json.loads(installed.read_text())==p
            unit=Path(gui.pw_dir)/'.config/systemd/user'/(name+'.service')
            assert 'watch' in unit.read_text() and 'WantedBy=default.target' in unit.read_text()
            assert str(release) not in unit.read_text(), 'persistent unit depends on temporary export'
            mod.install_owner(payload)
            assert any(c[:4]==['systemctl','--user','enable','--now'] for c in calls)
            # Actual sockets: the tenant's verify reaches the compositor through the relay.
            with patch.object(mod,'runtime_for',side_effect=lambda user: gui_runtime if user==gui else tenant_runtime):
                env=mod.verify(p)
                assert env['WAYLAND_DISPLAY']==str(leaf/'wayland-0')
                assert env['XDG_RUNTIME_DIR']==str(tenant_runtime)
                assert env['DBUS_SESSION_BUS_ADDRESS']==f'unix:path={tenant_runtime}/bus'
            assert compositor.peers==[me], compositor.peers
            # Descriptors cross with their bytes, in order, both ways.
            assert echo_through(leaf/'wayland-0',[b'first',b'second',b'third'])==[b'first',b'second',b'third']
            receipt_mode=receipt.stat().st_mode & 0o7777
            for hostile_mode in (0o664,0o646):
                try:
                    receipt.chmod(hostile_mode)
                    with patch.object(mod,'runtime_for',side_effect=lambda user: gui_runtime if user==gui else tenant_runtime):
                        try:
                            mod.verify(p)
                        except mod.DesktopAccessError as exc:
                            assert str(exc)=='Desktop readiness record is not protected by the GUI owner', str(exc)
                        else:
                            raise AssertionError(f'writable readiness receipt accepted: {hostile_mode:o}')
                finally:
                    receipt.chmod(receipt_mode)
            # A second grant has an exposure of its own.
            p2=policy(project='amber');name2=mod.names(p2)[2]
            mod.exposure_dir(gui,name2,create=True)
            mod.install_owner({**payload,'policy':p2})
            assert mod.exposure_path(name2)!=leaf and (mod.exposure_path(name2)/'wayland-0').is_socket()
            # Withdrawn: the service stops, and the exposure keeps nothing of the grant.
            mod.uninstall_owner(p)
            assert name not in services and sorted(os.listdir(leaf))==[] and f'user:{me}' not in mod.acl_map(mod.read_acl(leaf))
            assert not installed.exists() and not unit.exists()
            assert (mod.exposure_path(name2)/'ready.json').exists(), 'the other grant stays'
            with patch.object(mod,'runtime_for',side_effect=lambda user: gui_runtime if user==gui else tenant_runtime):
                rejects(lambda:mod.verify(p))
            # A first install whose unit cannot start leaves nothing behind.
            fail_enable=True
            real_subprocess_run=subprocess.run
            def cleanup_run(args,**kwargs):
                if args[0]=='systemctl':return subprocess.CompletedProcess(args,0,'')
                return real_subprocess_run(args,**kwargs)
            with patch.object(mod.subprocess,'run',side_effect=cleanup_run):
                rejects(lambda:mod.install_owner(payload))
            assert not installed.exists() and not unit.exists() and not receipt.exists()
            assert f'user:{me}' not in mod.acl_map(mod.read_acl(leaf))
            assert mod.read_acl(gui_runtime)==['user::rwx','group::---','other::---']
            # A receipt an earlier service left is not this install's: a service that
            # never writes one fails the install, however ready that file says it is.
            fail_enable=False;silent=True
            mod.atomic_json(receipt,{'form':mod.EXPOSURE_FORM,'policy_digest':mod.digest(p),'state':'ready',
                                     'socket':str(leaf/'wayland-0'),'upstream':'wayland-7'},mode=0o644)
            with patch.object(mod.subprocess,'run',side_effect=cleanup_run):
                try:
                    mod.install_owner(payload,ready_within=0.5)
                except mod.DesktopAccessError as exc:
                    assert 'wrote no readiness receipt' in str(exc), str(exc)
                else:
                    raise AssertionError('a receipt left by an earlier service was taken for this one')
            assert not installed.exists() and not receipt.exists()
            for unit_name in list(services):
                stop(unit_name)
        compositor.close()


def test_the_exposure_is_proven_from_its_trusted_root():
    """Every directory from TRUST down is its owner's alone, then the desktop owner's own; links are refused."""
    with tempfile.TemporaryDirectory(prefix='desktop-trust-') as tmp:
        root=Path(tmp)/'trust';root.mkdir(mode=0o755)
        me=os.getuid()
        gui=SimpleNamespace(pw_name='desktop-owner',pw_uid=me,pw_gid=os.getgid())
        other=SimpleNamespace(pw_name='someone',pw_uid=me+1,pw_gid=os.getgid())
        def refused(call):
            try:
                call()
            except desktop.DesktopAccessError as exc:
                return str(exc)
            raise AssertionError('accepted what it must refuse')
        with patch.object(desktop,'TRUST',(str(root),me)):
            leaf=desktop.exposure_dir(gui,'switchyard-desktop-1-2-proj',create=True)
            assert leaf==root/'var/lib/switchyard/desktop/1-2-proj' and oct(leaf.stat().st_mode&0o777)=='0o700'
            assert all(oct(path.stat().st_mode&0o777)=='0o755' for path in leaf.parents if root in path.parents)
            assert desktop.exposure_dir(gui,'switchyard-desktop-1-2-proj')==leaf
            message=refused(lambda:desktop.exposure_dir(other,'switchyard-desktop-1-2-proj'))
            assert 'only someone may write' in message, message
            leaf.chmod(0o720)
            assert 'only desktop-owner may write' in refused(lambda:desktop.exposure_dir(gui,'switchyard-desktop-1-2-proj'))
            leaf.chmod(0o700)
            (root/'var/lib/switchyard').chmod(0o775)
            assert 'var/lib/switchyard is not a directory only uid' in refused(lambda:desktop.exposure_dir(gui,'switchyard-desktop-1-2-proj'))
            (root/'var/lib/switchyard').chmod(0o755)
            # A link anywhere on the way is refused, never followed.
            real=root/'var/lib/switchyard/desktop';moved=root/'elsewhere';real.rename(moved);real.symlink_to(moved)
            assert 'is not a directory' in refused(lambda:desktop.exposure_dir(gui,'switchyard-desktop-1-2-proj'))
            real.unlink();moved.rename(real)
            (real/'1-3-proj').symlink_to(leaf)
            assert 'is not a directory' in refused(lambda:desktop.exposure_dir(gui,'switchyard-desktop-1-3-proj'))
            missing=refused(lambda:desktop.exposure_dir(gui,'switchyard-desktop-1-4-proj'))
            assert 'does not exist; root makes it once' in missing and not (real/'1-4-proj').exists(), missing
        # Only TRUST's owner makes one; anyone else's `create` finds or refuses.
        with patch.object(desktop,'TRUST',(str(root),me+1)):
            assert 'is not a directory only uid' in refused(lambda:desktop.exposure_dir(gui,'switchyard-desktop-1-5-proj',create=True))
            assert not (root/'var/lib/switchyard/desktop/1-5-proj').exists()
    # The shipped default: `/`, root. On a host where / is root's, a grant root has
    # not made is named as missing at its real path.
    if os.stat('/').st_uid==0:
        try:
            desktop.exposure_dir(SimpleNamespace(pw_name='desktop-owner',pw_uid=os.getuid(),pw_gid=os.getgid()),
                                 'switchyard-desktop-1-2-syrd687-absent')
        except desktop.ExposureMissing as exc:
            assert '/var/lib/switchyard/desktop/1-2-syrd687-absent does not exist' in str(exc), str(exc)
        else:
            raise AssertionError('an exposure root never made was found')
    else:
        print("SKIPPED the shipped `/` walk: / is not root's here (a user namespace or sandbox maps it away)")


def test_dropping_an_entry_never_widens_another():
    with tempfile.TemporaryDirectory(prefix='desktop-drop-') as tmp:
        path=Path(tmp)/'runtime'
        def fresh(spec):
            if path.exists():path.rmdir()
            path.mkdir(mode=0o700)
            if spec:subprocess.run(['setfacl','--no-mask','-m',spec,str(path)],check=True)
            return path
        acl=lambda:desktop.read_acl(path)
        mode=lambda:oct(path.stat().st_mode&0o777)
        # Ours alone: the ACL is minimal again and the directory 0700, as Qt requires.
        fresh('u:62001:--x,m::--x')
        assert mode()=='0o710' and desktop.drop_entry(path,62001,expected='--x')==''
        assert acl()==['user::rwx','group::---','other::---'] and mode()=='0o700'
        assert desktop.runtime_mode_problem(path,os.getuid())==''
        # Beside an operator's masked entry: the mask narrows to what remains, never widens.
        fresh('u:62001:--x,u:62003:r-x,m::--x')
        desktop.drop_entry(path,62001,expected='--x')
        assert acl()==['user::rwx','user:62003:r-x\t#effective:--x','group::---','mask::--x','other::---'], acl()
        problem=desktop.runtime_mode_problem(path,os.getuid())
        assert problem.endswith('is 0710 instead of 0700; its ACL names user:62003:r-x'), problem
        fresh('u:62001:rwx,u:62003:--x,m::rwx')
        desktop.drop_entry(path,62001)
        assert desktop.acl_map(acl())['mask:']=='--x', acl()
        # A group entry wider than the mask is narrowed to what it was allowed before the mask goes.
        fresh('u:62001:--x,g::r-x,m::--x')
        desktop.drop_entry(path,62001,expected='--x')
        assert acl()==['user::rwx','group::--x','other::---'] and mode()=='0o710', acl()
        # Somebody changed the entry since: it is theirs, and is left.
        fresh('u:62001:rwx,m::rwx')
        note=desktop.drop_entry(path,62001,expected='--x')
        assert 'user:62001 is rwx, not the --x Switchyard set; left as it is' in note and 'user:62001:rwx' in acl(), (note, acl())
        # An entry from before the grant is put back, not removed, as effective as it was.
        fresh('u:62001:--x,m::r-x')
        desktop.drop_entry(path,62001,expected='--x',restore='r--',prior_mask='r-x')
        assert acl()==['user::rwx','user:62001:r--','group::---','mask::r--','other::---'] and mode()=='0o740', acl()
        # Audit's case: the entry the grant replaced was silenced by a mask of ---. The grant's
        # --x mask is Switchyard's alone, and goes with it: the directory is 0700 again.
        fresh('u:62001:r--,m::---')
        record=desktop.grant(path,62001,'--x')
        assert mode()=='0o710', acl()
        prior=desktop.acl_map(record['before'])
        desktop.drop_entry(path,62001,expected='--x',restore=prior['user:62001'],prior_mask=prior['mask:'])
        assert acl()==['user::rwx','user:62001:r--\t#effective:---','group::---','mask::---','other::---'], acl()
        assert mode()=='0o700' and desktop.runtime_mode_problem(path,os.getuid())=='', acl()
        # A silenced entry that shares the grant's bit stays silenced: the mask goes back to what it was.
        fresh('u:62001:rwx,m::---')
        record=desktop.grant(path,62001,'--x')
        prior=desktop.acl_map(record['before'])
        desktop.drop_entry(path,62001,expected='--x',restore=prior['user:62001'],prior_mask=prior['mask:'])
        assert acl()==['user::rwx','user:62001:rwx\t#effective:---','group::---','mask::---','other::---'], acl()
        assert mode()=='0o700', acl()
        # Beside a later operator entry, the mask keeps what that entry has and gives back nothing more.
        fresh('u:62001:r--,m::---')
        record=desktop.grant(path,62001,'--x')
        subprocess.run(['setfacl','--no-mask','-m','u:62003:--x',str(path)],check=True)
        prior=desktop.acl_map(record['before'])
        desktop.drop_entry(path,62001,expected='--x',restore=prior['user:62001'],prior_mask=prior['mask:'])
        assert desktop.acl_map(acl())['mask:']=='--x' and 'user:62001:r--\t#effective:---' in acl(), acl()
        assert desktop.drop_entry(path,62009)=='', 'an entry that is not there is nothing to do'


def test_retiring_a_legacy_grant_puts_back_what_it_replaced():
    """`retire_legacy` reads the receipt's record of the ACL before the grant, and the directory is 0700 again."""
    with tempfile.TemporaryDirectory(prefix='desktop-retire-') as tmp:
        root=Path(tmp);me=os.getuid()
        people={'desktop-owner':SimpleNamespace(pw_name='desktop-owner',pw_uid=me,pw_gid=os.getgid(),pw_dir=str(root/'home')),
                'cerulean-worker':SimpleNamespace(pw_name='cerulean-worker',pw_uid=62001,pw_gid=62001,pw_dir=str(root/'t'))}
        runtime=root/'runtime';runtime.mkdir(mode=0o700)
        # Silenced before the grant, and sharing its execute bit: only the receipt's record says it was.
        subprocess.run(['setfacl','--no-mask','-m','u:62001:rwx,m::---',str(runtime)],check=True)
        with patch.object(desktop.pwd,'getpwnam',side_effect=lambda name:people[name]), \
                patch.object(desktop,'runtime_for',lambda user:runtime):
            p=policy();name=desktop.names(p)[2]
            # main's legacy grant, as its receipt records it: the ACL before, and after.
            record=desktop.grant(runtime,62001,'--x')
            desktop.atomic_json(runtime/(name+'.json'),{'policy_digest':desktop.digest(p),'grants':[record]},mode=0o644)
            assert oct(runtime.stat().st_mode&0o777)=='0o710'
            notes=desktop.retire_legacy(p)
        assert desktop.read_acl(runtime)==['user::rwx','user:62001:rwx\t#effective:---','group::---','mask::---','other::---'], \
            desktop.read_acl(runtime)
        assert oct(runtime.stat().st_mode&0o777)=='0o700' and notes[-1]==f'{runtime} is 0700', notes
        assert not (runtime/(name+'.json')).exists()


def test_verify_asks_the_compositor_through_the_relay():
    """A ready receipt is a claim; the round trip is the proof, and a socket that does not answer is refused."""
    with tempfile.TemporaryDirectory(prefix='desktop-verify-') as tmp:
        root=Path(tmp);me=os.getuid()
        people={'desktop-owner':SimpleNamespace(pw_name='desktop-owner',pw_uid=me,pw_gid=os.getgid(),pw_dir=tmp),
                'cerulean-worker':SimpleNamespace(pw_name='cerulean-worker',pw_uid=me,pw_gid=os.getgid(),pw_dir=tmp)}
        leaf=root/'exposure';leaf.mkdir(mode=0o700)
        with patch.object(desktop.pwd,'getpwnam',side_effect=lambda name:people[name]), \
                patch.object(desktop,'runtime_for',lambda user:root), patch.object(desktop,'exposure_dir',lambda gui,name:leaf):
            p=policy()
            desktop.atomic_json(leaf/'ready.json',{'form':desktop.EXPOSURE_FORM,'policy_digest':desktop.digest(p),'state':'ready',
                                                   'socket':str(leaf/'wayland-0'),'upstream':'wayland-0'},mode=0o644)
            silent=socket.socket(socket.AF_UNIX);silent.bind(str(leaf/'wayland-0'));silent.listen(1)
            closer=threading.Thread(target=lambda:silent.accept()[0].close(),daemon=True);closer.start()
            try:
                desktop.verify(p)
            except desktop.DesktopAccessError as exc:
                assert 'closed the connection before the compositor answered' in str(exc), str(exc)
            else:
                raise AssertionError('a socket that never answered Wayland was taken for the compositor')
            silent.close();(leaf/'wayland-0').unlink()
            compositor=FakeCompositor(leaf/'wayland-0')
            assert desktop.verify(p)['WAYLAND_DISPLAY']==str(leaf/'wayland-0') and compositor.peers==[me]
            compositor.close()


def test_launcher_prelaunch_and_persistence():
    with tempfile.TemporaryDirectory(prefix='desktop-launch-') as tmp:
        root=Path(tmp); config_path=root/'cerulean.json'
        raw={'project':'cerulean','run_as_user':'cerulean-worker','layout':'layout.json',
             'roles':[{'role':'main','slot':0,'cli':['codex'],'env':{'DISPLAY':':99','XAUTHORITY':'/private/xauth','KEEP_ME':'yes'}}]}
        config_path.write_text(json.dumps(raw))
        config=launcher.load_project_config('cerulean',config_path)
        rejects(lambda:launcher.prepare_project_desktop(config))
        chosen=root/'choice.json';chosen.write_text(json.dumps({'mode':'headless'}))
        config=launcher.configure_project_desktop(config,config_path=config_path,policy_path=chosen)
        command=launcher.cli_command_for_role(config.roles[0],session_dir=root)
        assert '-u' in command and 'DISPLAY=:99' not in command and 'KEEP_ME=yes' in command
        assert json.loads((root/'desktop-policy.json').read_text())['mode']=='headless'
        p=policy();chosen.write_text(json.dumps(p))
        env={'WAYLAND_DISPLAY':'/run/user/7000/wayland-8','XDG_RUNTIME_DIR':'/run/user/7001','DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/7001/bus'}
        events=[]
        def verifier(args,**kwargs):
            events.append(('verify',args));return subprocess.CompletedProcess(args,0,json.dumps(env),'')
        with patch.object(desktop,'install',side_effect=lambda *a,**k:events.append(('install',a))), patch.object(launcher,'current_user_name',return_value='cerulean-worker'):
            config=launcher.configure_project_desktop(config,config_path=config_path,policy_path=chosen,runner=verifier)
            assert [e[0] for e in events]==['install','verify']
            assert config.roles[0].env['XDG_RUNTIME_DIR']=='/run/user/7001'
            assert config.roles[0].env['WAYLAND_DISPLAY']=='/run/user/7000/wayland-8'
            assert 'DISPLAY' not in config.roles[0].env
            # Fresh artifact regeneration consumes the same persisted policy input.
            plan=launcher.build_plan(project='cerulean',owner_user='cerulean-worker')
            launcher.write_new_project_launcher_artifacts(plan,root,repository=root/'repo')
            assert launcher.load_project_config('cerulean',config_path).desktop_access==p
        before=config_path.read_bytes()
        with patch.object(desktop,'install',side_effect=desktop.DesktopAccessError('not authorized')):
            rejects(lambda:launcher.configure_project_desktop(config,config_path=config_path,policy_path=chosen,runner=verifier))
        assert config_path.read_bytes()==before
        # A desktop reload cannot kill a busy worker, even with force.
        from scripts.ticket_board.notify_listener import PaneActivityGate
        calls=[]
        for reload in [launcher.run_role_pane,launcher.run_detached_role,launcher.ensure_visible_role_session_for_viewer]:
            with patch.object(PaneActivityGate,'is_busy',return_value=True):
                result=reload(config.roles[0],mode='reload',session_dir=root/'sessions',
                    pane_state_dir=root/'pane-state',force_reload=True,
                    runner=lambda args,**kwargs:(calls.append(args) or subprocess.CompletedProcess(args,0)))
            assert result==1 and not any('kill-session' in args for args in calls)
        # Missing policy must fail before any role-launch/worktree mutation.
        blank=launcher.replace(config,desktop_access=None)
        with patch.object(launcher,'upgrade_generated_project_layout',return_value=launcher.LauncherUpgradeResult(False,'')), patch.object(launcher,'_verify_pane_launcher_path') as launch:
            rejects(lambda:launcher.launch_project(blank,config_path=config_path,mode='start',script_path=ROOT/'scripts/team-launcher'))
            launch.assert_not_called()


def test_exported_new_project_before_first_role():
    from contextlib import ExitStack
    with tempfile.TemporaryDirectory(prefix='desktop-new-export-') as tmp:
        root=Path(tmp);export=root/'installed-release'
        shutil.copytree(ROOT/'scripts',export/'scripts',ignore=shutil.ignore_patterns('__pycache__'))
        spec=importlib.util.spec_from_file_location('desktop_export_launcher',export/'scripts/team_launcher.py')
        mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod)
        project=root/'project';project.mkdir();owner_home=root/'cerulean-home';owner_home.mkdir()
        artifact=root/'project.json'
        artifact.write_text(json.dumps({'schema':'switchyard.project.v1','design_document':str(project/'DESIGN.md'),
            'project':{'slug':'cerulean','name':'Cerulean','ticket_prefix':'CER','owner_user':'cerulean-worker',
                       'repository':str(project),'roles':['main'],'role_clis':{'director':'claude','main':'codex'},
                       'include_designer':False,'include_audit':False}}))
        choice=root/'approved.json';choice.write_text(json.dumps(policy()))
        output=project/'.switchyard/provision';events=[]
        environment={'WAYLAND_DISPLAY':'/run/user/7900/wayland-7','XDG_RUNTIME_DIR':'/run/user/7901',
                     'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/7901/bus'}
        def runner(args,**kwargs):
            if 'verify' in args:
                events.append('tenant-verified')
                return subprocess.CompletedProcess(args,0,json.dumps(environment),'')
            return subprocess.CompletedProcess(args,0,'','')
        def first_auth(config,**kwargs):
            assert events==['gui-policy-unit-acl-installed','tenant-verified','tenant-verified']
            assert config.roles[0].env['XDG_RUNTIME_DIR']==environment['XDG_RUNTIME_DIR']
            events.append('auth')
            return mod.FirstRunAuthReport({},[])
        def first_launch(config,**kwargs):
            assert events[-1]=='auth'
            # Execute the very first runtime command against a harmless Python
            # receiver, proving env -u removes inherited unusable X11 variables.
            code='import os,json;print(json.dumps({k:os.getenv(k) for k in ["WAYLAND_DISPLAY","XDG_RUNTIME_DIR","DISPLAY","XAUTHORITY"]}))'
            role=mod.replace(config.roles[0],cli=[sys.executable,'-c',code],yolo=False,model='',extra_args=[])
            command=mod.cli_command_for_role(role,session_dir=root/'sessions')
            result=subprocess.run(command,env={**os.environ,'DISPLAY':':99','XAUTHORITY':'/unreadable'},text=True,capture_output=True,check=True)
            values=json.loads(result.stdout)
            assert values['WAYLAND_DISPLAY']==environment['WAYLAND_DISPLAY']
            assert values['XDG_RUNTIME_DIR']==environment['XDG_RUNTIME_DIR']
            assert values['DISPLAY'] is None and values['XAUTHORITY'] is None
            events.append('first-role')
            return 0
        import scripts.team_launcher as phase_launcher
        with ExitStack() as stack:
            real_getpwnam = mod.pwd.getpwnam
            fixture_owner = SimpleNamespace(pw_name='cerulean-worker', pw_uid=os.getuid(),
                pw_gid=os.getgid(), pw_dir='/home/cerulean-worker', pw_shell='/bin/bash')
            stack.enter_context(patch.object(mod.pwd, 'getpwnam',
                side_effect=lambda name: fixture_owner if name == 'cerulean-worker' else real_getpwnam(name)))
            stack.enter_context(patch.object(mod.os,'geteuid',return_value=0))
            # Root writes the owner's files by descriptor from the owner's home and refuses a path outside it (SYRD-608):
            # here, the scratch root that holds the project.
            from scripts import owner_writes
            import owner_home_sandbox  # noqa: F401 -- and root stages its writes in a private directory
            real_owner_home = owner_writes.owner_home
            stack.enter_context(patch.object(owner_writes, 'owner_home',
                side_effect=lambda name: str(root) if name == 'cerulean-worker' else real_owner_home(name)))
            for name in ['_precheck_project_path_before_mutating','precheck_new_project','_chown_switchyard_project_files',
                         '_install_switchyard_onboarding_docs','_require_existing_project_git_repository',
                         '_commit_project_git_changes','_prepare_first_run_auth_worktrees','report_launch_session_records']:
                # SYRD-370: the command's phases read these from `scripts.team_launcher`, which is
                # not this copy, so each is stood in on that launcher too.
                for target in {id(mod):mod,id(phase_launcher):phase_launcher}.values():
                    stack.enter_context(patch.object(target,name,return_value=None))
            # SYRD-39: a fresh project defers its launch until an operator has
            # created the per-role Unix accounts. This case is about what the
            # FIRST role process inherits when it does start, so isolation is
            # stated as a precondition rather than asserted past.
            # SYRD-373: the sign-in phase reads it from `scripts.team_launcher` too.
            for target in {id(mod):mod,id(phase_launcher):phase_launcher}.values():
                stack.enter_context(patch.object(target,'role_isolation_gaps',return_value=[]))
            # SYRD-371: the accounts phase reads it from `scripts.team_launcher` too.
            for target in {id(mod):mod,id(phase_launcher):phase_launcher}.values():
                stack.enter_context(patch.object(target,'_ensure_owner_user_and_project_dir',return_value=mod.OwnerUserProvisionResult(False,False)))
            # SYRD-373: the sign-in phase reads it from `scripts.team_launcher` too.
            for target in {id(mod):mod,id(phase_launcher):phase_launcher}.values():
                stack.enter_context(patch.object(target,'_owner_home_for_auth',return_value=owner_home))
            # SYRD-373: the sign-in phase reads it from `scripts.team_launcher` too.
            for target in {id(mod):mod,id(phase_launcher):phase_launcher}.values():
                stack.enter_context(patch.object(target,'run_first_run_auth_phase',side_effect=first_auth))
            # SYRD-374: the panes phase reads it from `scripts.team_launcher` too.
            for target in {id(mod):mod,id(phase_launcher):phase_launcher}.values():
                stack.enter_context(patch.object(target,'launch_project',side_effect=first_launch))
            stack.enter_context(patch.object(desktop,'install',side_effect=lambda *a,**k:events.append('gui-policy-unit-acl-installed')))
            assert mod.switchyard_new_command(from_artifact=artifact,desktop_policy=choice,source_repo=export,
                output_dir=output,yes=True,allow_existing_owner_user=True,git_init=False,runner=runner,euid_getter=lambda:0,
                home_base=root/'homes',registry_dir=root/'registry',config_dir=root/'configs',print_func=lambda _:None)==0
        assert events==['gui-policy-unit-acl-installed','tenant-verified','tenant-verified','auth','first-role']
        assert json.loads((output/'cerulean.json').read_text())['desktop_access']==policy()
        assert json.loads((output/'desktop-policy.json').read_text())==policy()


if __name__=='__main__':
    test_validation();test_exported_release_exposure_lifecycle();test_the_exposure_is_proven_from_its_trusted_root();test_dropping_an_entry_never_widens_another();test_retiring_a_legacy_grant_puts_back_what_it_replaced();test_verify_asks_the_compositor_through_the_relay();test_launcher_prelaunch_and_persistence();test_exported_new_project_before_first_role()
    print('desktop_access_test: real ACL/socket and exported helper lifecycle; launcher policy/order checks passed')
