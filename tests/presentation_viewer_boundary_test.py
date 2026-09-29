#!/usr/bin/env python3
"""SYRD-497: viewer construction and observer sizing with synthetic tmux."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / 'scripts')):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import presentation_controller as direct_controller  # noqa: E402
from scripts import presentation_controller as package_controller  # noqa: E402
from scripts import presentation_viewer as owner  # noqa: E402


class Tmux:
    def __init__(self, *, fail: str = '') -> None:
        self.fail = fail
        self.sessions = {'sample-display-0', 'sample-display-1'}
        self.calls: list[list[str]] = []
        self.panes: dict[str, list[str]] = {}
        self.clients: dict[str, list[str]] = {}
        self.flags: dict[str, str] = {}

    def __call__(self, args, **_kwargs):
        argv = list(args)
        self.calls.append(argv)
        verb = argv[1]
        target = argv[argv.index('-t') + 1].lstrip('=').split(':', 1)[0] if '-t' in argv else ''
        if verb == 'has-session':
            return subprocess.CompletedProcess(argv, 0 if target in self.sessions else 1, '', '')
        if verb == 'new-session':
            if self.fail == verb:
                return subprocess.CompletedProcess(argv, 7, '', '')
            self.sessions.add(argv[argv.index('-s') + 1])
        if verb in {'resize-window', 'split-window'} and self.fail == verb:
            return subprocess.CompletedProcess(argv, 8, '', '')
        if verb == 'list-panes':
            return subprocess.CompletedProcess(argv, 0 if target in self.sessions else 1,
                                               '\n'.join(self.panes.get(target, [])), '')
        if verb == 'list-clients':
            if '-t' not in argv:
                body = ''.join(f'{tty}\t{session}\t{self.flags.get(tty, "attached")}\n'
                               for session, ttys in self.clients.items() for tty in ttys)
                return subprocess.CompletedProcess(argv, 0, body, '')
            return subprocess.CompletedProcess(argv, 0 if target in self.sessions else 1,
                                               '\n'.join(self.clients.get(target, [])), '')
        if verb == 'refresh-client':
            tty = argv[argv.index('-t') + 1]
            change = argv[argv.index('-f') + 1]
            flags = [f for f in self.flags.get(tty, 'attached').split(',') if f]
            if change.startswith('!'):
                flags = [f for f in flags if f != change[1:]]
            elif change not in flags:
                flags.append(change)
            self.flags[tty] = ','.join(flags)
        return subprocess.CompletedProcess(argv, 0, '', '')


def config():
    return SimpleNamespace(project='sample')


def state():
    return {'slot_count': 2, 'slots': {'0': 'director', '1': 'app'}}


def refusal(call, expected: str) -> None:
    try:
        call()
    except SystemExit as exc:
        assert expected in str(exc), (expected, str(exc))
    else:
        raise AssertionError(f'expected refusal: {expected}')


def test_viewer_build_pins_before_split_then_labels_exact_targets() -> None:
    tmux = Tmux()
    tmux.clients['sample-display-1'] = ['/dev/pts/desktop']
    owner._launch_viewer(config(), state(), runner=tmux)
    calls = tmux.calls
    verbs = [call[1] for call in calls]
    create = verbs.index('new-session')
    pin = verbs.index('resize-window')
    split = verbs.index('split-window')
    layout = verbs.index('select-layout')
    unpin = next(i for i, call in enumerate(calls) if call[:2] == ['tmux', 'set-option']
                 and 'window-size' in call)
    assert create < pin < split < layout < unpin
    assert calls[pin][calls[pin].index('-t') + 1] == '=sample-viewer:0'
    assert calls[unpin][calls[unpin].index('-t') + 1] == '=sample-viewer:0'
    assert calls[create][calls[create].index('-s') + 1] == 'sample-viewer'
    assert '!ignore-size' in calls[create][-1]
    assert calls[split][calls[split].index('-t') + 1] == '=sample-viewer:0'
    assert ' -f ignore-size ' in shlex.split(calls[split][-1])[2]
    assert ['tmux', 'set-option', '-p', '-t', '=sample-viewer:0.0', '@switchyard_role', 'director'] in calls
    assert ['tmux', 'set-option', '-p', '-t', '=sample-viewer:0.1', '@switchyard_role', 'app'] in calls
    assert len([c for c in calls if c[:2] == ['tmux', 'set-hook']]) == 3
    hooks = [c[-2] for c in calls if c[:2] == ['tmux', 'set-hook']]
    assert hooks[0] == 'window-resized'
    assert hooks[1].startswith('client-attached[')
    assert hooks[2].startswith('client-detached[')
    command = owner._viewer_observer_attach('sample-display-0', sizing=True)
    assert shlex.split(shlex.split(command)[2])[-4:] == ['-f', '!ignore-size', '-t', '=sample-display-0']


def test_observer_refreshes_only_slot_without_external_window() -> None:
    tmux = Tmux()
    tmux.sessions.add('sample-viewer')
    tmux.panes['sample-viewer'] = ['/dev/pts/200', '/dev/pts/201']
    tmux.clients = {
        'sample-display-0': ['/dev/pts/200'],
        'sample-display-1': ['/dev/pts/201', '/dev/pts/desktop'],
    }
    tmux.flags = {'/dev/pts/200': 'attached,ignore-size', '/dev/pts/201': 'attached,ignore-size'}
    owner.reconcile_viewer_observer_flags('sample-viewer', runner=tmux)
    assert [c for c in tmux.calls if c[:2] == ['tmux', 'refresh-client']] == [
        ['tmux', 'refresh-client', '-f', '!ignore-size', '-t', '/dev/pts/200'],
    ]
    assert tmux.flags['/dev/pts/200'] == 'attached'
    assert tmux.flags['/dev/pts/201'] == 'attached,ignore-size'
    tmux.calls.clear()
    owner.reconcile_viewer_observer_flags('sample-viewer', runner=tmux)
    assert not [c for c in tmux.calls if c[:2] == ['tmux', 'refresh-client']]
    assert package_controller.external_presentation_clients(config(), 2, runner=tmux) == {'/dev/pts/desktop'}


def test_create_pin_and_split_failures_stop_before_layout_and_hooks() -> None:
    for verb, expected in (('new-session', 'could not create viewer sample-viewer'),
                           ('resize-window', 'could not size viewer sample-viewer'),
                           ('split-window', 'could not populate viewer sample-viewer')):
        tmux = Tmux(fail=verb)
        refusal(lambda: owner._launch_viewer(config(), state(), runner=tmux), expected)
        assert tmux.calls[-1][1] == verb
        assert not any(c[1] in {'select-layout', 'set-hook'} for c in tmux.calls)


def test_both_controller_import_forms_reexport_all_ten_objects() -> None:
    names = (
        'VIEWER_OBSERVER_CLIENT_FLAGS', '_session_pane_ttys', '_exact_target_args',
        '_clients_by_tty', '_client_table', '_viewer_observer_attach',
        '_viewer_frame_commands', '_reconcile_viewer_observers',
        'reconcile_viewer_observer_flags', '_launch_viewer',
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
    print(f'presentation_viewer_boundary_test: {checks} checks ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
