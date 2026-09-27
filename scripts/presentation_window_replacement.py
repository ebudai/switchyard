"""`switchyard replace-window`: replacing an unsafe root-owned presentation window while every worker keeps running.

`replace_presentation_window_command` finds the project's root-owned
presentation windows, refuses unless it is root, stops exactly those
processes (SIGTERM, then SIGKILL for any that outlive the settle time),
checks that no worker session was lost, and opens a fresh window as the
desktop account -- through the presentation controller when the project
presents through it, or from a freshly rendered layout otherwise.

The window scan and report, the worker listing, the pane launcher, the layout
and the window launch are read from `scripts/team_launcher.py` when the
command runs; the presentation controller is its own import; the layout modes
come from `scripts/layout_modes.py`.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-329). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from scripts.layout_modes import LAYOUT_MODE_AUTO, LAYOUT_MODE_SEPARATE

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig



def replace_presentation_window_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    script_path: Path | None = None,
    pane_state_dir: Path | None = None,
    layout_mode: str = LAYOUT_MODE_AUTO,
    layout_environ: dict[str, str] | None = None,
    proc_root: Path | None = None,
    euid_getter: Callable[[], int] = os.geteuid,
    signaller: Callable[[int, int], None] = os.kill,
    settle_seconds: float = 2.0,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    konsole_process_launcher: Callable[..., Any] | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """Replace an unsafe root-owned presentation window, leaving workers running.

    The tenant cannot signal a root process, so this is the operator's path off
    a window that predates the repair. It touches exactly two things: the GUI
    processes it found, and a freshly rendered layout. Worker sessions are
    listed before and after and are never a target -- losing a role's session
    would lose that role's work, which is a worse outcome than the window
    (SYRD-43).
    """
    from scripts import team_launcher as launcher

    windows = launcher.unsafe_root_presentation_windows(config, config_path=config_path, proc_root=proc_root)
    workers_before = [role.role for role in launcher._running_project_roles(config, runner=runner)]
    if not windows:
        print_func(
            f"switchyard: {config.project} has no root-owned presentation window; nothing to replace."
        )
        return 0
    if euid_getter() != 0:
        print_func(
            launcher.unsafe_presentation_report(config, windows)
            + "\nThis command must run as root: a tenant cannot signal a root process."
        )
        return 1
    print_func(launcher.unsafe_presentation_report(config, windows))
    for window in windows:
        try:
            signaller(window.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError) as exc:
            print_func(f"switchyard: could not stop presentation pid {window.pid}: {exc}")
    deadline = time.monotonic() + max(0.0, settle_seconds)
    while time.monotonic() < deadline:
        if not launcher.unsafe_root_presentation_windows(config, config_path=config_path, proc_root=proc_root):
            break
        time.sleep(0.1)
    remaining = launcher.unsafe_root_presentation_windows(config, config_path=config_path, proc_root=proc_root)
    for window in remaining:
        try:
            signaller(window.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError) as exc:
            print_func(f"switchyard: could not stop presentation pid {window.pid}: {exc}")
    workers_after = [role.role for role in launcher._running_project_roles(config, runner=runner)]
    lost = [role for role in workers_before if role not in workers_after]
    if lost:
        print_func(
            "switchyard: worker sessions that were running are no longer running: "
            + ", ".join(lost)
            + ". They were not a target of this command; start the project to recover them."
        )
    from scripts import presentation_controller

    resolved_script = script_path or launcher.switchyard_pane_launcher_for(config)
    output_path = launcher.default_layout_output_path(config, config_path=config_path)
    if presentation_controller.presentation_enabled(config, config_path=config_path):
        result = presentation_controller.launch_presentation(
            config,
            config_path=config_path,
            layout=LAYOUT_MODE_SEPARATE,
            runner=runner,
            process_launcher=konsole_process_launcher,
        )
    else:
        launcher.materialize_layout(
            config,
            config_path=config_path,
            mode="attach-or-start",
            script_path=resolved_script,
            output_path=output_path,
            pane_state_dir=pane_state_dir,
        )
        result = launcher.launch_konsole_window(
            output_path,
            project=config.project,
            window_title=launcher.project_window_title(config),
            gui_user=launcher.default_gui_user() or None,
            runner=runner,
            process_launcher=konsole_process_launcher,
        )
    if result != 0:
        print_func(
            f"switchyard: replaced the root-owned window but could not open a new one (exit {result}). "
            f"Worker sessions are untouched; run `switchyard {config.project}` from the desktop account."
        )
        return result
    print_func(
        f"switchyard: replaced {config.project}'s presentation window as {launcher.default_gui_user()}; "
        f"worker sessions still running: {', '.join(workers_after) or 'none'}"
    )
    return 0
