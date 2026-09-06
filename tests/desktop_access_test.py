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
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import desktop_access as desktop
from scripts import team_launcher as launcher


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


def test_exported_release_acl_lifecycle():
    for tool in ['setfacl','getfacl']:
        assert shutil.which(tool), f'{tool} required; this suite must not silently skip'
    with tempfile.TemporaryDirectory(prefix='desktop-access-') as tmp:
        root=Path(tmp)
        # An exported installed-release layout: no .git, helper outside GUI home.
        release=root/'opt/switchyard/releases/fixture/scripts';release.mkdir(parents=True)
        shutil.copy2(ROOT/'scripts/desktop_access.py',release/'desktop_access.py')
        spec=importlib.util.spec_from_file_location('exported_desktop',release/'desktop_access.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        gui=SimpleNamespace(pw_name='desktop-owner',pw_uid=os.getuid(),pw_dir=str(root/'gui-home'))
        tenant=SimpleNamespace(pw_name='cerulean-worker',pw_uid=62001,pw_dir=str(root/'tenant-home'))
        second=SimpleNamespace(pw_name='amber-worker',pw_uid=62002,pw_dir=str(root/'second-home'))
        gui_runtime=root/'gui-runtime';gui_runtime.mkdir(mode=0o700)
        tenant_runtime=root/'tenant-runtime';tenant_runtime.mkdir(mode=0o700)
        socket_path=gui_runtime/'wayland-7'
        server=socket.socket(socket.AF_UNIX);server.bind(str(socket_path));server.listen(10)
        accounts={x.pw_name:x for x in [gui,tenant,second]}
        actual_run=mod.run
        calls=[];fail_enable=False
        def fixture_run(args):
            nonlocal fail_enable
            calls.append(args)
            if args[0]=='loginctl':
                if args[1]=='show-session':return f'Type=wayland\nActive=yes\nUser={gui.pw_uid}'
                if 'Sessions' in args:return 'fixture-session'
                return str(gui_runtime if args[2]==gui.pw_name else tenant_runtime)
            if args[0]=='systemctl':
                if fail_enable and 'enable' in args:
                    raise mod.DesktopAccessError('fixture unit enable denied')
                return 'active'
            return actual_run(args)
        with patch.object(mod.pwd,'getpwnam',side_effect=lambda name:accounts[name]), patch.object(mod,'run',side_effect=fixture_run):
            p=policy();name=mod.names(p)[2]
            original_runtime=mod.read_acl(gui_runtime);original_socket=mod.read_acl(socket_path)
            payload={'policy':p,'helper':(release/'desktop_access.py').read_text(),'python':sys.executable}
            mod.install_owner(payload)
            receipt=gui_runtime/(name+'.json')
            assert receipt.exists()
            assert mod.acl_map(mod.read_acl(gui_runtime))[f'user:{tenant.pw_uid}']=='--x'
            assert mod.acl_map(mod.read_acl(socket_path))[f'user:{tenant.pw_uid}']=='rw-'
            installed=Path(gui.pw_dir)/'.config/switchyard/desktop-access'/(name+'.json')
            assert json.loads(installed.read_text())==p
            unit=Path(gui.pw_dir)/'.config/systemd/user'/(name+'.service')
            assert 'watch' in unit.read_text() and 'WantedBy=default.target' in unit.read_text()
            assert str(release) not in unit.read_text(), 'persistent unit depends on temporary export'
            before=receipt.read_bytes();mod.apply(p);assert receipt.read_bytes()==before
            mod.install_owner(payload)
            assert any(c[:4]==['systemctl','--user','enable','--now'] for c in calls)
            # Actual socket connection; UID/access checks are fixture boundaries.
            with patch.object(mod.os,'geteuid',return_value=tenant.pw_uid), patch.object(mod,'runtime_for',side_effect=lambda user: gui_runtime if user==gui else tenant_runtime):
                env=mod.verify(p)
                assert env['WAYLAND_DISPLAY']==str(socket_path)
                assert env['XDG_RUNTIME_DIR']==str(tenant_runtime)
                assert env['DBUS_SESSION_BUS_ADDRESS']==f'unix:path={tenant_runtime}/bus'
            server.accept()[0].close()
            # Real receipt modes; account identities and GUI sessions remain fixtures.
            # This does not exercise ownership enforcement across real accounts.
            receipt_mode=receipt.stat().st_mode & 0o7777
            for hostile_mode in (0o664,0o646):
                try:
                    receipt.chmod(hostile_mode)
                    with patch.object(mod.os,'geteuid',return_value=tenant.pw_uid), patch.object(mod,'runtime_for',side_effect=lambda user: gui_runtime if user==gui else tenant_runtime):
                        try:
                            mod.verify(p)
                        except mod.DesktopAccessError as exc:
                            assert str(exc)=='Desktop readiness record is not protected by the GUI owner', str(exc)
                        else:
                            raise AssertionError(f'writable readiness receipt accepted: {hostile_mode:o}')
                finally:
                    receipt.chmod(receipt_mode)
            # A second independent tenant's grant survives first-tenant rollback.
            p2=policy(second.pw_name,'amber');mod.apply(p2)
            mod.rollback(p)
            assert f'user:{tenant.pw_uid}' not in mod.acl_map(mod.read_acl(socket_path))
            assert mod.acl_map(mod.read_acl(socket_path))[f'user:{second.pw_uid}']=='rw-'
            mod.apply(p)
            # Compositor replaces the socket: old receipt cannot authorize it.
            server.close();socket_path.unlink()
            server=socket.socket(socket.AF_UNIX);server.bind(str(socket_path));server.listen(5)
            with patch.object(mod.os,'geteuid',return_value=tenant.pw_uid), patch.object(mod,'runtime_for',side_effect=lambda user: gui_runtime if user==gui else tenant_runtime):
                rejects(lambda:mod.verify(p))
            mod.apply(p)
            assert mod.acl_map(mod.read_acl(socket_path))[f'user:{tenant.pw_uid}']=='rw-'
            shared=policy(tenant.pw_name,'another-project')
            mod.apply(shared)
            mod.rollback(p)
            assert mod.effective_acl(socket_path,tenant.pw_uid)==6
            mod.rollback(shared)
            assert f'user:{tenant.pw_uid}' not in mod.acl_map(mod.read_acl(socket_path))
            mod.apply(p)
            # Selected desktop disappears; no grant to an unrelated path.
            with patch.object(mod,'selected_socket',side_effect=mod.DesktopAccessError('no compositor')):
                rejects(lambda:mod.apply(p))
            # A broad mask must not enable an unrelated previously masked entry.
            mod.rollback(p);mod.rollback(p2)
            actual_run(['setfacl','-m','u:62003:r--,m::---',str(socket_path)])
            snapshot=mod.read_acl(socket_path)
            rejects(lambda:mod.grant(socket_path,tenant.pw_uid,'rw-'))
            assert mod.read_acl(socket_path)==snapshot
            actual_run(['setfacl','--set='+','.join(original_socket),str(socket_path)])
            # Remove managed fixture then simulate a first-install activation failure.
            for path in [installed,unit,Path(gui.pw_dir)/'.local/lib/switchyard/desktop-access'/(name+'.py')]:
                path.unlink()
            fail_enable=True
            # Only suppress fallback systemctl calls, not filesystem ACL commands.
            real_subprocess_run=subprocess.run
            def cleanup_run(args,**kwargs):
                if args[0]=='systemctl':return subprocess.CompletedProcess(args,0,'')
                return real_subprocess_run(args,**kwargs)
            with patch.object(mod.subprocess,'run',side_effect=cleanup_run):
                rejects(lambda:mod.install_owner(payload))
            assert not installed.exists() and not unit.exists() and not receipt.exists()
            assert f'user:{tenant.pw_uid}' not in mod.acl_map(mod.read_acl(gui_runtime))
            server.close()


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
        with ExitStack() as stack:
            real_getpwnam = mod.pwd.getpwnam
            fixture_owner = SimpleNamespace(pw_name='cerulean-worker', pw_uid=os.getuid(),
                pw_gid=os.getgid(), pw_dir='/home/cerulean-worker', pw_shell='/bin/bash')
            stack.enter_context(patch.object(mod.pwd, 'getpwnam',
                side_effect=lambda name: fixture_owner if name == 'cerulean-worker' else real_getpwnam(name)))
            stack.enter_context(patch.object(mod.os,'geteuid',return_value=0))
            for name in ['_precheck_project_path_before_mutating','precheck_new_project','_chown_switchyard_project_files',
                         '_install_switchyard_onboarding_docs','_require_existing_project_git_repository',
                         '_commit_project_git_changes','_prepare_first_run_auth_worktrees','report_launch_session_records']:
                stack.enter_context(patch.object(mod,name,return_value=None))
            # SYRD-39: a fresh project defers its launch until an operator has
            # created the per-role Unix accounts. This case is about what the
            # FIRST role process inherits when it does start, so isolation is
            # stated as a precondition rather than asserted past.
            stack.enter_context(patch.object(mod,'role_isolation_gaps',return_value=[]))
            stack.enter_context(patch.object(mod,'_ensure_owner_user_and_project_dir',return_value=mod.OwnerUserProvisionResult(False,False)))
            stack.enter_context(patch.object(mod,'_owner_home_for_auth',return_value=owner_home))
            stack.enter_context(patch.object(mod,'run_first_run_auth_phase',side_effect=first_auth))
            stack.enter_context(patch.object(mod,'launch_project',side_effect=first_launch))
            stack.enter_context(patch.object(desktop,'install',side_effect=lambda *a,**k:events.append('gui-policy-unit-acl-installed')))
            assert mod.switchyard_new_command(from_artifact=artifact,desktop_policy=choice,source_repo=export,
                output_dir=output,yes=True,allow_existing_owner_user=True,git_init=False,runner=runner,euid_getter=lambda:0,
                home_base=root/'homes',registry_dir=root/'registry',config_dir=root/'configs',print_func=lambda _:None)==0
        assert events==['gui-policy-unit-acl-installed','tenant-verified','tenant-verified','auth','first-role']
        assert json.loads((output/'cerulean.json').read_text())['desktop_access']==policy()
        assert json.loads((output/'desktop-policy.json').read_text())==policy()


if __name__=='__main__':
    test_validation();test_exported_release_acl_lifecycle();test_launcher_prelaunch_and_persistence();test_exported_new_project_before_first_role()
    print('desktop_access_test: real ACL/socket and exported helper lifecycle; launcher policy/order checks passed')
