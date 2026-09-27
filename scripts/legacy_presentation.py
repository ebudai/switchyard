"""Giving a legacy tenant the desktop-account presentation, and the presentation-config wrappers it rests on.

- **The section:** `legacy_presentation_section` and
  `presentation_section_for_roles` build the presentation section `switchyard
  new` writes, from a tenant's configured slots.
- **The decision:** `legacy_presentation_migration` says whether this tenant's
  presentation has to move to the desktop account's own state directory, and
  why it must not (`LegacyPresentationMigration`), checking the destination
  and the state root without following anything
  (`_desktop_state_root_problem`).
- **The move:** `migrate_legacy_presentation` writes the section before an
  upgrade declares the tenant ready, or refuses; `legacy_presentation_refusal`
  says why a launch must not open the fallback window.
- `presentation_controller_enabled` asks the presentation controller.

Accounts, homes, the desktop account, the layout paths, the JSON reader and
writer and the config loader stay in `scripts/team_launcher.py` or the
modules it re-exports, and are read through the launcher when a function
runs; the presentation controller is each function's own import. The suites
patch `presentation_section_for_roles` on the launcher, and its readers reach
it there.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-328). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig



@dataclass(frozen=True)
class LegacyPresentationMigration:
    """What giving a legacy tenant the desktop-account presentation would do.

    A tenant provisioned before `switchyard new` wrote a `presentation` section
    has none, so its launch takes the fallback that hands the terminal a layout
    in the owner's own 0700 state directory. That works when the desktop account
    IS the owner and cannot work otherwise: the terminal runs as the desktop
    account and cannot enter the directory (SYRD-233, live on mefp).
    """

    #: Whether the tenant needs the section at all.
    needed: bool
    #: The section to add, built from the tenant's own configured slots.
    section: dict[str, Any]
    #: Where its presentation window will read its layout from, once migrated.
    destination: Path | None
    gui_user: str
    #: Why the migration must not happen, when it must not.
    refusal: str = ""
    #: Why nothing is needed, for the report.
    reason: str = ""

    def describe(self) -> str:
        roles = ", ".join(
            f"{slot}={role}" for slot, role in sorted(
                self.section.get("layouts", {}).get("default", {}).items(), key=lambda item: int(item[0])
            )
        )
        return (
            f"a presentation section ({self.section.get('slot_count')} slot(s): {roles}), so its window "
            f"reads its layout from {self.gui_user}'s own state directory ({self.destination}) instead "
            "of the tenant's private one"
        )


def presentation_controller_enabled(config: "ProjectConfig", *, config_path: Path) -> bool:
    from scripts import presentation_controller

    return presentation_controller.presentation_enabled(config, config_path=config_path)


def presentation_section_for_roles(roles: "Iterable[Mapping[str, Any]]") -> dict[str, Any]:
    """`legacy_presentation_section`'s rule, over a configuration's raw role entries."""
    mapping = {
        str(role["slot"]): str(role.get("role"))
        for role in roles
        if isinstance(role, Mapping) and not role.get("detached") and role.get("slot") is not None
    }
    slot_count = max((int(slot) for slot in mapping), default=-1) + 1
    return {"slot_count": slot_count, "layouts": {"default": mapping}}


def legacy_presentation_section(config: "ProjectConfig") -> dict[str, Any]:
    """The section `switchyard new` writes, from this tenant's configured slots.

    The configured slots, not a fresh enumeration: a tenant whose roles sit at
    0, 1, 2 and 3 keeps exactly those, and one with a gap keeps the gap. The
    presentation controller derives its default mapping from the same role
    slots, so the two cannot disagree about who is where.
    """
    mapping = {
        str(role.slot): role.role
        for role in config.roles
        if not role.detached and role.slot is not None
    }
    slot_count = max((int(slot) for slot in mapping), default=-1) + 1
    return {"slot_count": slot_count, "layouts": {"default": mapping}}


def legacy_presentation_migration(
    config: "ProjectConfig", *, config_path: Path
) -> LegacyPresentationMigration:
    """Decide whether this tenant's presentation has to move, and where to."""
    from scripts import team_launcher as launcher

    from scripts import presentation_controller

    gui_user = launcher.pinned_presentation_gui_user(config)
    owner = config.run_as_user or launcher.current_user_name()
    empty = {"slot_count": 0, "layouts": {"default": {}}}
    if not gui_user:
        return LegacyPresentationMigration(
            False, empty, None, "", reason="no Wayland desktop is granted, so nothing is presented"
        )
    if gui_user == owner:
        return LegacyPresentationMigration(
            False, empty, None, gui_user,
            reason=f"the desktop account is the tenant owner {owner}, whose own state it can read",
        )
    if presentation_controller.presentation_enabled(config, config_path=config_path):
        return LegacyPresentationMigration(
            False, empty, None, gui_user,
            reason="it already presents through the desktop account's own state directory",
        )
    section = legacy_presentation_section(config)
    if section["slot_count"] < 1:
        return LegacyPresentationMigration(
            False, section, None, gui_user, reason="it has no visible role to present"
        )
    if section["slot_count"] > launcher.MAX_VISIBLE_PANES_PER_WINDOW:
        return LegacyPresentationMigration(
            True, section, None, gui_user,
            refusal=(
                f"its roles occupy slots up to {section['slot_count'] - 1}, and a presentation "
                f"window has {launcher.MAX_VISIBLE_PANES_PER_WINDOW}"
            ),
        )
    destination = launcher.desktop_presentation_layout_path(config, config_path=config_path, gui_user=gui_user)
    problem = launcher.desktop_layout_destination_problem(
        destination, gui_user=gui_user, project=config.project
    )
    if not problem:
        problem = _desktop_state_root_problem(gui_user, config.project)
    return LegacyPresentationMigration(
        True, section, destination, gui_user, refusal=problem
    )


def _desktop_state_root_problem(gui_user: str, project: str) -> str:
    """Why the desktop account's state root cannot safely receive a layout, or "".

    Read-only, so a dry run can report it: the same walk the write makes, stopped
    at the first component that does not yet exist -- absent is fine, the write
    creates it; a symlink or somebody else's directory is not.
    """
    from scripts import team_launcher as launcher

    uid = launcher.uid_for_user(gui_user)
    if uid is None:
        return f"{gui_user} is not a local account"
    home = Path(launcher._gui_home(gui_user))
    walked = home
    for component in (None, *launcher.desktop_state_dir(project, gui_user).relative_to(home).parts):
        if component is not None:
            walked = walked / component
        try:
            info = os.lstat(walked)
        except FileNotFoundError:
            return ""
        except OSError as exc:
            return f"{walked} cannot be inspected ({exc.strerror})"
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return f"{walked} is a symlink or not a directory"
        if info.st_uid != uid:
            return f"{walked} is owned by uid {info.st_uid}, not by {gui_user} (uid {uid})"
    return ""


def migrate_legacy_presentation(
    config: "ProjectConfig",
    *,
    config_path: Path,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> tuple["ProjectConfig", bool]:
    """Give a legacy tenant the presentation section before it is declared ready.

    Returns the (possibly reloaded) configuration and whether the upgrade may
    go on. Nothing under the tenant's home is loosened: the tenant's config
    gains one section, and the layout goes to the desktop account's own state
    directory when the window is next opened (SYRD-233).
    """
    from scripts import team_launcher as launcher

    migration = launcher.legacy_presentation_migration(config, config_path=config_path)
    if migration.refusal:
        print_func(
            f"switchyard: {config.project}'s presentation window cannot be moved to "
            f"{migration.gui_user or 'the desktop account'}'s own state directory: {migration.refusal}. "
            "Its roles would start and its window would fail to open, so this upgrade stops "
            "before declaring it ready. Nothing was changed."
        )
        return config, False
    if not migration.needed:
        return config, True
    if dry_run:
        print_func(f"switchyard: would give {config.project} {migration.describe()}; nothing written")
        return config, True
    payload = launcher._load_json(config_path)
    if payload.get("project") != config.project:
        print_func(
            f"switchyard: {config_path} names project {payload.get('project')!r}, not "
            f"{config.project!r}; refusing to add a presentation section to it."
        )
        return config, False
    payload["presentation"] = migration.section
    launcher._write_json_atomic(config_path, payload, owner_user=config.run_as_user or launcher.current_user_name())
    print_func(f"switchyard: gave {config.project} {migration.describe()}")
    return launcher.load_project_config(config.project, config_path), True


def legacy_presentation_refusal(config: "ProjectConfig", *, output_path: Path) -> str:
    """Why the fallback window must not be opened, or "" when it may.

    The fallback hands the terminal `output_path`, which lives in the owner's
    own state directory. The terminal runs as the desktop account. When those
    are different accounts the file cannot be read, so the only honest outcome
    is to say so, with the command that fixes it, and leave the workers be.
    """
    from scripts import team_launcher as launcher

    gui_user = launcher.presentation_gui_user(config)
    owner = config.run_as_user or launcher.current_user_name()
    if not gui_user or gui_user == owner:
        return ""
    return (
        f"switchyard: {config.project}'s roles are running, but its presentation window was not "
        f"opened: the layout it would be handed, {output_path}, is in {owner}'s private state "
        f"directory and the window runs as {gui_user}, who cannot read it. This tenant predates "
        f"presenting through the desktop account's own state directory. Run `sudo switchyard "
        f"upgrade {config.project}` to move it there, then `switchyard {config.project}` to open "
        f"the window over the running roles; `switchyard attach {config.project} <role>` reaches "
        "any role now. Nothing under the tenant's home was loosened."
    )
