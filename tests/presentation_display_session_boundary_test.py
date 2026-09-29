#!/usr/bin/env python3
"""SYRD-495: display construction owner with synthetic tmux and config."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / 'scripts')):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import presentation_controller as direct_controller  # noqa: E402
from scripts import presentation_controller as package_controller  # noqa: E402
from scripts import presentation_display_session as owner  # noqa: E402
from scripts import team_launcher  # noqa: E402


def config(root: Path):
    path = root / 'sample.json'
    path.write_text(json.dumps({
        'project': 'sample',
        'session_dir': str(root / 'sessions'),
        'roles': [
            {'role': 'director', 'slot': 0, 'cli': 'codex', 'workdir': str(root)},
        ],
    }), encoding='utf-8')
    return team_launcher.load_project_config('sample', path)


class Tmux:
    def __init__(self, *, slot_exists: bool = False, fail: str = '') -> None:
        self.slot_exists = slot_exists
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, args, **_kwargs):
        argv = list(args)
        self.calls.append(argv)
        verb = argv[1]
        if verb == 'has-session':
            target = argv[argv.index('-t') + 1]
            found = target == '=sample-director' or (target == '=sample-display-0' and self.slot_exists)
            return subprocess.CompletedProcess(argv, 0 if found else 1, '', '' if found else "can't find session")
        if verb == 'display-message':
            return subprocess.CompletedProcess(argv, 0, '0\n', '')
        if verb == 'list-clients':
            return subprocess.CompletedProcess(argv, 0, '', '')
        return subprocess.CompletedProcess(argv, 7 if verb == self.fail else 0, '', '')


def test_new_slot_is_locked_labeled_and_hooked_after_proxy_creation() -> None:
    with tempfile.TemporaryDirectory(prefix='syrd495-owner.') as directory:
        cfg = config(Path(directory))
        tmux = Tmux()
        owner._configure_display_session(cfg, 0, 'director', runner=tmux, presentation_ttys=set())
        calls = tmux.calls
        new_index = next(i for i, c in enumerate(calls) if c[:2] == ['tmux', 'new-session'])
        new = calls[new_index]
        assert new[new.index('-s') + 1] == 'sample-display-0'
        assert '=sample-director' in new[-1]
        assert calls[new_index + 1:new_index + 4] == list(owner.display_lock_commands('sample-display-0'))
        assert ['tmux', 'set-option', '-t', '=sample-display-0:', 'status', 'off'] in calls
        assert ['tmux', 'set-option', '-t', '=sample-display-0:', 'status-left', ' slot 0: director '] in calls
        assert ['tmux', 'set-option', '-p', '-t', '=sample-display-0:0.0', '@switchyard_role', 'director'] in calls
        hook = calls[-1]
        assert hook[:3] == ['tmux', 'set-hook', '-g'] and len(hook) == 5
        assert hook[3].startswith('session-closed[')
        assert '=sample-display-0:0.0' in hook[-1]
        assert 'switchyard recover-display sample' in hook[-1]


def test_cross_account_worker_lock_precedes_display_mutation() -> None:
    with tempfile.TemporaryDirectory(prefix='syrd495-owner.') as directory:
        cfg = config(Path(directory))
        role = replace(cfg.roles[0], run_as_user='another-account')
        cfg = replace(cfg, roles=[role])
        assert owner._proxy_crosses_account(cfg, role)
        tmux = Tmux()
        owner._configure_display_session(cfg, 0, 'director', runner=tmux, presentation_ttys=set())
        calls = tmux.calls
        worker_locks = list(owner.display_lock_commands(role.tmux_session))
        lock_start = calls.index(worker_locks[0])
        assert calls[lock_start:lock_start + 3] == worker_locks
        detach_index = next(i for i, c in enumerate(calls) if c[-2:] == ['detach-on-destroy', 'on'])
        create_index = next(i for i, c in enumerate(calls) if c[:2] == ['tmux', 'new-session'])
        assert lock_start + 2 < detach_index < create_index
        assert '/usr/bin/sudo' in calls[create_index][-1]
        assert calls[create_index + 1:create_index + 4] == list(owner.display_lock_commands('sample-display-0'))


def test_hidden_existing_slot_respawns_status_and_removes_hook() -> None:
    with tempfile.TemporaryDirectory(prefix='syrd495-owner.') as directory:
        cfg = config(Path(directory))
        tmux = Tmux(slot_exists=True)
        owner._configure_display_session(cfg, 0, None, runner=tmux, presentation_ttys=set())
        calls = tmux.calls
        assert not any(c[:2] == ['tmux', 'new-session'] for c in calls)
        respawn = next(c for c in calls if c[:2] == ['tmux', 'respawn-pane'])
        assert respawn[respawn.index('-t') + 1] == '=sample-display-0:0.0'
        assert 'sample: display slot hidden' in respawn[-1]
        assert ['tmux', 'set-option', '-p', '-t', '=sample-display-0:0.0', '@switchyard_role', 'hidden'] in calls
        assert calls[-1][:3] == ['tmux', 'set-hook', '-gu']
        assert calls[-1][3].startswith('session-closed[')


def test_failed_creation_never_labels_or_hooks_slot() -> None:
    with tempfile.TemporaryDirectory(prefix='syrd495-owner.') as directory:
        cfg = config(Path(directory))
        tmux = Tmux(fail='new-session')
        try:
            owner._configure_display_session(cfg, 0, 'director', runner=tmux, presentation_ttys=set())
        except RuntimeError as exc:
            assert 'could not update display slot 0 (exit 7)' in str(exc)
        else:
            raise AssertionError('failed creation was accepted')
        assert tmux.calls[-1][:2] == ['tmux', 'new-session']
        assert not any(c[:2] == ['tmux', 'set-hook'] for c in tmux.calls)
        assert not any(c[-2:] == ['status', 'off'] for c in tmux.calls)


def test_controller_import_forms_reexport_the_owner_without_wrappers() -> None:
    names = (
        'DIRECTOR_ROLE', 'DISPLAY_KEY_TABLE', 'display_session_name', '_role_by_name',
        '_session_exists', '_role_status', '_worker_has_independent_client',
        'display_lock_options', 'display_lock_commands', '_proxy_client_flags',
        'worker_attach_argv', '_proxy_crosses_account', '_proxy_command',
        '_recovery_instruction', '_status_script', '_status_command',
        '_recovery_hook_index', '_configure_recovery_hook',
        'display_slot_terminal_title_commands', '_configure_display_session',
    )
    for name in names:
        assert getattr(package_controller, name) is getattr(owner, name), name
        assert getattr(direct_controller, name) is getattr(owner, name), name


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith('test_') and callable(value):
            value()
            checks += 1
    print(f'presentation_display_session_boundary_test: {checks} checks ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
