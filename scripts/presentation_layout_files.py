"""The presentation layout file: where it goes, who owns it, and what it says.

- **Where:** `default_layout_output_path` chooses the layout file for a
  launch, `desktop_state_dir` and `desktop_presentation_layout_path` place a
  desktop account's own copy, and `desktop_layout_destination_problem` says
  why a destination may not be written.
- **Who owns it:** `ensure_layout_output_owner` gives the layout directory to
  the project's owner (`chown_layout_output_args`) when the invoking account
  is someone else.
- **What it says:** `materialize_layout` writes the Konsole layout, one inert
  leaf per visible role (`inert_pane_command`), each titled by
  `pane_split_title` from `role_display_name`.

The pane commands, window title, layout leaves, owner state path and account
lookups stay in `scripts/team_launcher.py` and are read there when a function
runs. The suites patch `materialize_layout`, `default_layout_output_path`,
`desktop_state_dir` and `pane_split_title` on the launcher, and every caller --
here, in the launcher and in the modules already moved out -- reaches them
there.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-319). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig



def role_display_name(role: RoleConfig) -> str:
    """One role's name as a person reads it, from the slug the project uses."""
    slug = str(role.role or "").strip()
    return slug[:1].upper() + slug[1:] if slug else slug


def pane_split_title(config: ProjectConfig, role: RoleConfig) -> str:
    """What one split in the presentation calls itself: its role, and no more.

    SYRD-122 put the project name in here too, on the understanding that
    Konsole gives a window no title of its own -- the title bar shows the
    active split's, so the project reached it only by being inside every split
    title. That bought the window a name at the price of six headers all
    beginning `Switchyard -- `, and it did not even hold: the title bar still
    changed as focus moved, because it was reading whichever split had it.

    Konsole does distinguish the two. The window title an escape sequence sets
    is separate from a split's title, and the pane wrapper now reports both
    (SYRD-139). So this is the role's name, which is what the header is for --
    as the document says it reads, which is not always the slug capitalised:
    implementers read `<role> Developer` unless their tenant says otherwise
    (SYRD-141).
    """
    from scripts import team_launcher as launcher

    return role.presentation_label or role_display_name(role) or launcher.project_window_title(config)


def inert_pane_command(
    program: Path, args: Sequence[str], *, title: str = "", window_title: str = ""
) -> str:
    """Wrap a pane's client so its terminal never falls back to a shell.

    Konsole runs the tab's program directly; when that program is the attach
    command, a detach returns the tab to whatever shell opened the window. That
    shell belongs to whoever invoked switchyard, so on a privileged invocation
    the pane becomes a root prompt. The wrapper ends inert instead (SYRD-43).

    The wrapper is also where both titles come from, because Konsole's layout
    file has no key for either (SYRD-122, SYRD-139): the split's own name, and
    the window name every split reports identically so the title bar stops
    following focus.
    """
    from scripts import team_launcher as launcher

    window_args = ["--window-title", window_title] if window_title.strip() else []
    title_args = ["--title", title] if title.strip() else []
    # Konsole's own splitter, not a shell's: see `_konsole_quote`.
    return launcher._konsole_command([str(program), *window_args, *title_args, *args])


def materialize_layout(
    config: ProjectConfig,
    *,
    config_path: Path,
    mode: str,
    script_path: Path,
    output_path: Path,
    pane_state_dir: Path | None = None,
    force_reload: bool = False,
    failed_roles: dict[str, str] | None = None,
) -> Path:
    from scripts import team_launcher as launcher

    failed_roles = failed_roles or {}
    visible_roles = [role.role for role in config.roles if not role.detached]
    if len(visible_roles) > launcher.MAX_VISIBLE_PANES_PER_WINDOW:
        raise SystemExit(
            f"team-launcher: {config.project} has {len(visible_roles)} visible roles; at most "
            f"{launcher.MAX_VISIBLE_PANES_PER_WINDOW} panes can be visible in one window; detach extra roles or open an "
            "additional director-invoked window"
        )
    layout = json.loads(config.layout.read_text(encoding="utf-8"))
    leaves = launcher._layout_leaves(layout)
    for role in config.roles:
        if role.detached:
            continue
        if role.slot is None:
            continue
        if role.slot < 0 or role.slot >= len(leaves):
            raise SystemExit(f"role {role.role} slot {role.slot} is outside layout leaf count {len(leaves)}")
        leaf = leaves[role.slot]
        if role.role in failed_roles:
            leaf["Command"] = launcher.failed_role_command(
                role, failed_roles[role.role], window_title=launcher.project_window_title(config)
            )
            leaf["WorkingDirectory"] = str(Path.home())
        else:
            leaf["Command"] = inert_pane_command(
                launcher.pane_window_program(script_path),
                launcher.pane_command_args(
                    config.project,
                    role,
                    config_path=config_path,
                    mode=mode,
                    script_path=script_path,
                    pane_state_dir=pane_state_dir,
                    force_reload=force_reload,
                    skip_launcher_check=True,
                    run_as_user=launcher.role_run_as_user(config, role),
                ),
                title=launcher.pane_split_title(config, role),
                window_title=launcher.project_window_title(config),
            )
            leaf["WorkingDirectory"] = role.workdir
        # Konsole 26.08.1 does not read this key -- its layout parser knows
        # Widgets, Orientation, Command, WorkingDirectory, Lines, Columns and
        # SessionRestoreId, and nothing else -- so the title that reaches the
        # header is the one the pane program sets for itself. This is kept
        # written and correct for anything that does read it, and so that a
        # reader comparing the file with the window is not told two different
        # things (SYRD-122).
        leaf["Title"] = launcher.pane_split_title(config, role)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def default_layout_output_path(config: ProjectConfig, *, config_path: Path) -> Path:
    from scripts import team_launcher as launcher

    if config.run_as_user:
        try:
            owner_home = Path(pwd.getpwnam(config.run_as_user).pw_dir)
        except KeyError:
            owner_home = None
            paths = [config_path.expanduser().resolve(strict=False)]
            if config.repository is not None:
                paths.append(config.repository.expanduser().resolve(strict=False))
            for path in paths:
                for candidate in (path, *path.parents):
                    if candidate.name == config.run_as_user:
                        owner_home = candidate
                        break
                if owner_home is not None:
                    break
            if owner_home is None:
                owner_home = Path("/home") / config.run_as_user
        return launcher._owner_state_layout_output_path(config.project, owner_home=owner_home)
    else:
        base_dir = config_path.parent / launcher.SWITCHYARD_PROJECT_DIR_NAME / config.project
        return base_dir / f"{config.project}-team-layout.json"


def chown_layout_output_args(config: ProjectConfig, output_path: Path) -> list[str]:
    if not config.run_as_user:
        raise ValueError("layout ownership repair requires run_as_user")
    return ["chown", "-R", f"{config.run_as_user}:{config.run_as_user}", str(output_path.parent)]


def desktop_state_dir(project: str, user: str) -> Path:
    """A desktop account's own private state directory for one project.

    Derived from the account and the project and nothing else, so the two sides
    of the bridge handoff agree on it by construction rather than by passing a
    path across (SYRD-90).
    """
    from scripts import team_launcher as launcher

    return Path(launcher._gui_home(user)) / ".local" / "state" / "switchyard" / "projects" / project


def desktop_presentation_layout_path(
    config: ProjectConfig, *, config_path: Path, gui_user: str
) -> Path:
    """Where a presentation window's layout file goes so its terminal can read it.

    Konsole is handed this path and reads it as the desktop account. The
    tenant's own state directory is 0700 and its files 0600, both owned by the
    project owner, so a window correctly dropped to the desktop user cannot
    open it at all and the terminal aborts before anything appears (SYRD-65).
    When the desktop account is the owner there is no boundary and the tenant's
    own location is right; otherwise it belongs under that person's state
    directory, still 0700 over 0600 -- protected, and owned by the reader.
    """
    from scripts import team_launcher as launcher

    tenant = launcher.default_layout_output_path(config, config_path=config_path).with_name(
        f"{config.project}-presentation-layout.json"
    )
    user = (gui_user or "").strip()
    if not user or user == config.run_as_user or (not config.run_as_user and user == launcher.current_user_name()):
        return tenant
    return launcher.desktop_state_dir(config.project, user) / f"{config.project}-presentation-layout.json"


def desktop_layout_destination_problem(path: Path, *, gui_user: str, project: str) -> str:
    """Why `path` is not the one place a layout for this project may cross to, or "".

    Exactly `~<gui_user>/.local/state/switchyard/projects/<project>/<file>`: the
    pinned desktop account's own state root for this project. Anything else is
    root being asked to write somewhere it was not told it could (SYRD-233).
    """
    from scripts import team_launcher as launcher

    if not project:
        return "no project was named for a layout that has to cross into another account"
    expected = launcher.desktop_state_dir(project, gui_user)
    if Path(os.path.normpath(path)).parent != expected or path.name in {"", ".", ".."}:
        return f"{path} is not in {gui_user}'s own state directory for {project} ({expected})"
    return ""


def ensure_layout_output_owner(
    config: ProjectConfig,
    output_path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    from scripts import team_launcher as launcher

    if not config.run_as_user or launcher.current_user_name() == config.run_as_user:
        return
    result = runner(chown_layout_output_args(config, output_path))
    if result.returncode != 0:
        reason = launcher._proc_failure_reason(result, f"chown failed with exit {result.returncode}")
        raise SystemExit(f"team-launcher: failed to assign layout output {output_path.parent} to {config.run_as_user}: {reason}")
