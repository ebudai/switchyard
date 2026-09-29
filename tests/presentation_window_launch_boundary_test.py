#!/usr/bin/env python3
"""SYRD-496: owner-side window path with synthetic desktop effects."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / 'scripts')):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import presentation_controller as direct_controller  # noqa: E402
from scripts import presentation_controller as package_controller  # noqa: E402
from scripts import presentation_window_launch as owner  # noqa: E402
from scripts import team_launcher  # noqa: E402


def config(root: Path):
    (root / 'director').mkdir()
    (root / 'app').mkdir()
    path = root / 'sample.json'
    path.write_text(json.dumps({
        'project': 'sample',
        'session_dir': str(root / 'sessions'),
        'roles': [
            {'role': 'director', 'slot': 0, 'cli': 'codex', 'workdir': str(root / 'director')},
            {'role': 'app', 'slot': 1, 'cli': 'codex', 'workdir': str(root / 'app')},
        ],
    }), encoding='utf-8')
    return team_launcher.load_project_config('sample', path), path


def refusal(call, expected: str) -> None:
    try:
        call()
    except SystemExit as exc:
        assert expected in str(exc), (expected, str(exc))
    else:
        raise AssertionError(f'expected refusal: {expected}')


def test_exact_slot_and_viewer_attach_argv_respect_account_boundary() -> None:
    user = team_launcher.current_user_name()
    assert owner.display_attach_args_for('sample', 1, owner=user, gui_user=user) == [
        'env', 'TMUX=', 'tmux', 'attach', '-t', '=sample-display-1',
    ]
    assert owner.display_attach_args_for('sample', owner.VIEWER_ATTACH_TARGET, owner=user, gui_user=user) == [
        'env', 'TMUX=', 'tmux', 'attach', '-t', '=sample-viewer',
    ]
    with patch.dict(os.environ, {'SWITCHYARD_SUDO_BIN': 'sudo'}):
        assert owner.display_attach_args_for('sample', 1, owner='other-owner', gui_user=user) == [
            'sudo', '-n', team_launcher.display_attach_helper_path('sample'), 'sample', '1',
        ]
    with tempfile.TemporaryDirectory(prefix='syrd496-window.') as directory:
        cfg, _path = config(Path(directory))
        assert owner.display_attach_args(cfg, 1, gui_user=user)[-1] == '=sample-display-1'


def test_layout_builder_uses_one_viewer_tab_or_one_tab_per_slot() -> None:
    user = team_launcher.current_user_name()
    kwargs = dict(slot_count=2, owner=user, gui_user=user, pane_program=Path('/usr/bin/true'),
                  slot_titles=('Director', 'App'), window_title='Sample Team')
    separate = owner.presentation_layout_payload('sample', **kwargs)
    viewer = owner.presentation_layout_payload('sample', layout_mode=team_launcher.LAYOUT_MODE_VIEWER, **kwargs)
    leaves = team_launcher._layout_leaves(separate)
    assert len(leaves) == 2
    assert [leaf['Title'] for leaf in leaves] == ['Director', 'App']
    assert '=sample-display-0' in leaves[0]['Command'] and '=sample-display-1' in leaves[1]['Command']
    viewer_leaves = team_launcher._layout_leaves(viewer)
    assert len(viewer_leaves) == 1 and '=sample-viewer' in viewer_leaves[0]['Command']


def test_handoff_writes_facts_then_reports_and_bad_descriptor_refuses() -> None:
    with tempfile.TemporaryDirectory(prefix='syrd496-window.') as directory:
        cfg, _path = config(Path(directory))
        read_fd, write_fd = os.pipe()
        said = io.StringIO()
        try:
            with patch.dict(os.environ, {team_launcher.PRESENTATION_HANDOFF_FD_ENV: str(write_fd)}):
                with redirect_stdout(said):
                    assert owner._hand_off_desktop_half(cfg, {'slot_count': 2}, gui_user='desktop-user')
            with os.fdopen(read_fd, 'r', encoding='utf-8') as handle:
                payload = json.loads(handle.readline())
            read_fd = -1
        finally:
            if read_fd >= 0:
                os.close(read_fd)
        assert payload['project'] == 'sample' and payload['slot_count'] == 2
        assert payload['slot_titles'] and payload['window_title']
        assert "window opens in desktop-user's own session" in said.getvalue()
        with patch.dict(os.environ, {team_launcher.PRESENTATION_HANDOFF_FD_ENV: '999999'}):
            refusal(lambda: owner._hand_off_desktop_half(cfg, {'slot_count': 2}, gui_user='desktop-user'),
                    'could not hand sample\'s presentation window to desktop-user')


def test_launch_checks_before_write_and_preserves_write_and_launch_failures() -> None:
    with tempfile.TemporaryDirectory(prefix='syrd496-window.') as directory:
        root = Path(directory)
        cfg, path = config(root)
        output = root / 'layout.json'
        events: list[tuple[str, object]] = []
        def write(_output, layout, **_kwargs):
            events.append(('write', len(team_launcher._layout_leaves(layout))))
            return ''
        def launch(_output, **_kwargs):
            events.append(('launch', _output))
            return 0
        state = {'slot_count': 2}
        env = {team_launcher.PRESENTATION_HANDOFF_FD_ENV: ''}
        with patch.dict(os.environ, env), patch.object(team_launcher, 'presentation_gui_user',
                return_value=team_launcher.current_user_name()), patch.object(
                team_launcher, 'presentation_pane_program_problem', return_value=''), patch.object(
                team_launcher, 'write_desktop_layout', side_effect=write), patch.object(
                team_launcher, 'launch_konsole_window', side_effect=launch):
            owner._launch_separate(cfg, state, config_path=path, output_path=output,
                                   runner=lambda *_args, **_kwargs: None, process_launcher=None)
            assert events == [('write', 2), ('launch', output)]
            events.clear()
            with patch.object(team_launcher, 'presentation_pane_program_problem', return_value='pane missing'):
                refusal(lambda: owner._launch_separate(cfg, state, config_path=path, output_path=output,
                        runner=lambda *_args, **_kwargs: None, process_launcher=None),
                        "not opening sample's presentation window: pane missing")
            assert events == []
            with patch.object(team_launcher, 'write_desktop_layout', return_value='layout denied'):
                refusal(lambda: owner._launch_separate(cfg, state, config_path=path, output_path=output,
                        runner=lambda *_args, **_kwargs: None, process_launcher=None),
                        "refusing to open sample's presentation window: layout denied")
            assert events == []
            with patch.object(team_launcher, 'launch_konsole_window', return_value=7):
                refusal(lambda: owner._launch_separate(cfg, state, config_path=path, output_path=output,
                        runner=lambda *_args, **_kwargs: None, process_launcher=None),
                        'could not launch separate presentation window (exit 7)')
            assert events == [('write', 2)]


def test_both_controller_import_forms_reexport_all_six_objects() -> None:
    names = ('VIEWER_ATTACH_TARGET', 'display_attach_args', 'display_attach_args_for',
             'presentation_layout_payload', '_hand_off_desktop_half', '_launch_separate')
    for name in names:
        assert getattr(package_controller, name) is getattr(owner, name), name
        assert getattr(direct_controller, name) is getattr(owner, name), name


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith('test_') and callable(value):
            value()
            checks += 1
    print(f'presentation_window_launch_boundary_test: {checks} checks ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
