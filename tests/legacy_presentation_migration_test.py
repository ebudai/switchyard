#!/usr/bin/env python3
"""SYRD-233: a legacy tenant must not hand its window a layout it cannot read.

Live on mefp, 2026-09-22: the upgrade accepted a Wayland policy for GUI user
`eric` over tenant owner `stellaris-agent`, `switchyard start mefp` brought the
roles up, and Konsole then said "A problem occurred when loading the Layout.
/home/stellaris-agent/.local/state/switchyard/projects/mefp/mefp-team-layout.json".

The tenant predates `switchyard new` writing a `presentation` section, so its
launch took the fallback that writes the layout into the OWNER's 0700 state
directory -- and the terminal runs as the DESKTOP account, which cannot enter
it. Tenants born with the section present through `_launch_separate`, which
stages the layout under the desktop account instead (SYRD-65, SYRD-90).

So the upgrade gives a legacy tenant that section, built from its own slots;
the root write into the desktop account's tree walks it without following
anything and refuses what it does not expect; and the fallback, reached by a
tenant nobody has upgraded yet, refuses the window with the reason and the
command that fixes it instead of handing Konsole an unreadable path. Nothing
under the tenant's home is loosened by any of it.

The readability claim is not asserted from paths: one case builds the two
homes inside a user namespace with the real uids of `stellaris-agent` and
`eric`, and asks the kernel.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from team_launcher_test_helpers import *  # noqa: F401,F403
from scripts import presentation_controller as presentation  # noqa: E402

CHECKS = 0
PROJECT = "stellar"
OWNER = "stellaris-agent"
GUI = "eric"
#: A legacy tenant's shape: four visible roles, no presentation section.
ROLES = ("director", "main", "ops", "audit")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def wayland_policy(*, gui: str = GUI, tenant: str = OWNER, project: str = PROJECT) -> dict:
    return {
        "mode": "wayland",
        "project": project,
        "tenant_user": tenant,
        "gui_user": gui,
        "wayland_display": "wayland-0",
        "consent": {
            "approved": True,
            "by": "eric",
            "at": "2026-09-22T10:08:00-04:00",
            "reference": "SYRD-233 fixture",
        },
    }


def legacy_config(tmp: Path, *, slots=(0, 1, 2, 3), gui: str = GUI, title: str = "Stellar Fix"):
    """A tenant as an old release left it: roles and slots, no presentation."""
    layout = tmp / f"{PROJECT}-layout.json"
    layout.write_text(
        json.dumps({
            "Orientation": "Horizontal",
            "Widgets": [
                {"Command": "", "SessionRestoreId": index, "WorkingDirectory": ""}
                for index in range(max(slots) + 1)
            ],
        }),
        encoding="utf-8",
    )
    repo = tmp / "repo"
    repo.mkdir(exist_ok=True)
    payload = {
        "project": PROJECT,
        "project_name": title,
        "layout": str(layout),
        "repository": str(repo),
        "run_as_user": OWNER,
        "session_dir": str(tmp / "sessions"),
        "desktop_access": wayland_policy(gui=gui),
        "roles": [
            {
                "role": role,
                "slot": slot,
                "cli": ["hermes"],
                "live_commands": ["hermes"],
                "target": f"{PROJECT}-{role}:0.0",
                "tmux_session": f"{PROJECT}-{role}",
            }
            for role, slot in zip(ROLES, slots)
        ],
    }
    config_path = tmp / f"{PROJECT}.json"
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return team_launcher.load_project_config(PROJECT, config_path), config_path


@contextmanager
def sandboxed_homes(tmp: Path, *, gui: str = GUI, gui_uid: int | None = None):
    """Both accounts' homes inside the sandbox, never the real ones.

    The owner's state path derives from a real home through pwd; a suite that
    lets it do so leaves fixtures in a live tenant's tree, which is exactly
    what this ticket's own reproduction did before it was sandboxed.
    """
    homes = {OWNER: tmp / "home" / OWNER, gui: tmp / "home" / gui}
    for home in homes.values():
        home.mkdir(parents=True, exist_ok=True)
    saved = (
        team_launcher._gui_home,
        team_launcher.default_layout_output_path,
        team_launcher.uid_for_user,
    )
    owner_state = homes[OWNER] / ".local" / "state" / "switchyard" / "projects"
    team_launcher._gui_home = lambda user: str(homes.get(user, tmp / "home" / user))
    team_launcher.default_layout_output_path = (
        lambda config, *, config_path: owner_state / config.project / f"{config.project}-team-layout.json"
    )
    real_uid = saved[2]
    # A sandbox cannot make files another account owns, so the desktop account
    # is this one by default and its home genuinely belongs to it. The case
    # about a component owned by somebody else names another uid instead; the
    # namespace case at the bottom does all of this with the real accounts.
    desktop_uid = os.getuid() if gui_uid is None else gui_uid
    team_launcher.uid_for_user = lambda name: desktop_uid if name == gui else real_uid(name)
    try:
        yield homes
    finally:
        (
            team_launcher._gui_home,
            team_launcher.default_layout_output_path,
            team_launcher.uid_for_user,
        ) = saved


@contextmanager
def environment(**values: str | None):
    saved = {name: os.environ.get(name) for name in values}
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class _Window:
    """A terminal that started and is still running, as far as the launch can tell.

    Behaves like a live Popen, so a launch that wrongly opens a window records
    one and carries on -- and the case fails on that, not on a crash here.
    """

    pid = 4242

    def poll(self):
        return None


def fake_runner(calls: list[list[str]]):
    fake = FakeRunner()

    def run(args, **kwargs):
        calls.append([str(part) for part in args])
        return fake(args, **kwargs)

    return run


# --------------------------------------------------------------------------
# The fallback: a tenant nobody has upgraded yet
# --------------------------------------------------------------------------


def test_the_legacy_fallback_refuses_the_window_instead_of_an_unreadable_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-fallback.") as raw:
        tmp = Path(raw)
        config, config_path = legacy_config(tmp)
        # The owner is this account for the launch mechanics; the desktop is
        # not, and the policy pins it -- mefp's live shape. SUDO_USER is left
        # unset so the only thing naming eric is the recorded grant.
        me = team_launcher.current_user_name()
        config = replace(config, run_as_user=me, desktop_access=wayland_policy(tenant=me))
        calls: list[list[str]] = []
        windows: list[list[str]] = []
        printed: list[str] = []
        saved_prepare = team_launcher.prepare_project_desktop
        # Verifying the Wayland grant runs a helper as the tenant against the
        # installed receipt; that is SYRD-232's subject, not this one's.
        team_launcher.prepare_project_desktop = lambda config, **_k: config
        with sandboxed_homes(tmp), environment(
            TMUX=None, TICKET_BOARD_CALLER_ROLE=None, SUDO_USER=None,
            **{team_launcher.TENANT_CONTROL_CALLER_ENV: None, team_launcher.GUI_USER_ENV: None},
        ):
          try:
            result = team_launcher.launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=fake_runner(calls),
                konsole_process_launcher=lambda args, **_k: windows.append(list(args)) or _Window(),
                layout_mode="separate",
                print_func=printed.append,
            )
          finally:
            team_launcher.prepare_project_desktop = saved_prepare
    output = "\n".join(printed)
    check(result != 0, f"a window that could not open is not a presented tenant: {result}")
    check(windows == [], f"and no terminal was handed the unreadable path: {windows}")
    check("was not opened" in output and GUI in output, output)
    check(f"sudo switchyard upgrade {PROJECT}" in output, f"it names the repair: {output}")
    check(f"switchyard attach {PROJECT} <role>" in output, f"and how to reach a role now: {output}")
    started = [call for call in calls if any(f"{PROJECT}-{role}" in " ".join(call) for role in ROLES)]
    check(started, f"the workers were started before the window was refused: {calls[:6]}")
    killed = [call for call in calls if "kill-session" in call]
    check(killed == [], f"and nothing was torn down to refuse it: {killed}")


def test_the_same_account_fallback_is_unchanged() -> None:
    # A tenant driven from its owner's own desktop can read its own state; the
    # fallback was only ever wrong across an account boundary.
    with tempfile.TemporaryDirectory(prefix="syrd233-same.") as raw:
        tmp = Path(raw)
        me = team_launcher.current_user_name()
        config, config_path = legacy_config(tmp, gui=me)
        config = replace(config, run_as_user=me, desktop_access=None)
        with sandboxed_homes(tmp, gui=me), environment(SUDO_USER=None):
            output = team_launcher.default_layout_output_path(config, config_path=config_path)
            check(
                team_launcher.legacy_presentation_refusal(config, output_path=output) == "",
                "no boundary, no refusal",
            )


# --------------------------------------------------------------------------
# The migration
# --------------------------------------------------------------------------


def test_the_section_is_the_tenants_own_slots_not_a_fresh_enumeration() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-section.") as raw:
        tmp = Path(raw)
        config, _ = legacy_config(tmp)
        section = team_launcher.legacy_presentation_section(config)
        check(section["slot_count"] == 4, f"four roles stay four: {section}")
        check(
            section["layouts"]["default"] == {"0": "director", "1": "main", "2": "ops", "3": "audit"},
            f"at the slots they were configured at: {section}",
        )
        (tmp / "gap").mkdir()
        gapped, _ = legacy_config(tmp / "gap", slots=(0, 2, 3, 5))
        gap = team_launcher.legacy_presentation_section(gapped)
        check(gap["slot_count"] == 6, f"a gap is kept, not closed up: {gap}")
        check(set(gap["layouts"]["default"]) == {"0", "2", "3", "5"}, gap)


def test_a_dry_run_reports_the_migration_and_its_destination_and_writes_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-dry.") as raw:
        tmp = Path(raw)
        config, config_path = legacy_config(tmp)
        before = config_path.read_bytes()
        printed: list[str] = []
        with sandboxed_homes(tmp) as homes:
            _config, ready = team_launcher.migrate_legacy_presentation(
                config, config_path=config_path, dry_run=True, print_func=printed.append
            )
            gui_tree = sorted(str(p) for p in homes[GUI].rglob("*"))
        output = "\n".join(printed)
        check(ready, f"a dry run of a migratable tenant is not a refusal: {output}")
        check("would give" in output and "nothing written" in output, output)
        check("4 slot(s)" in output, f"it says what it would add: {output}")
        check(f"{GUI}'s own state directory" in output, f"and the destination class: {output}")
        check(
            f"/.local/state/switchyard/projects/{PROJECT}/{PROJECT}-presentation-layout.json" in output,
            f"and the destination itself: {output}",
        )
        check(config_path.read_bytes() == before, "the tenant's configuration is byte-identical")
        check(gui_tree == [], f"and nothing was created under the desktop account: {gui_tree}")


def test_apply_adds_the_section_and_rerun_changes_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-apply.") as raw:
        tmp = Path(raw)
        config, config_path = legacy_config(tmp)
        printed: list[str] = []
        with sandboxed_homes(tmp):
            migrated, ready = team_launcher.migrate_legacy_presentation(
                config, config_path=config_path, print_func=printed.append
            )
            check(ready, "\n".join(printed))
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            check(
                payload["presentation"] == team_launcher.legacy_presentation_section(config),
                f"the section is the one described: {payload.get('presentation')}",
            )
            check(
                [role["role"] for role in payload["roles"]] == list(ROLES),
                "and the roles are exactly the roles they were",
            )
            check(payload["project_name"] == "Stellar Fix", "and the window title is untouched")
            check(
                presentation.presentation_enabled(migrated, config_path=config_path),
                "the next start takes the desktop-account path",
            )
            after_first = config_path.read_bytes()
            printed.clear()
            _again, ready_again = team_launcher.migrate_legacy_presentation(
                migrated, config_path=config_path, print_func=printed.append
            )
            check(ready_again and printed == [], f"a rerun is silent and ready: {printed}")
            check(config_path.read_bytes() == after_first, "and writes nothing")


def test_no_migration_where_none_is_needed() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-none.") as raw:
        tmp = Path(raw)
        with sandboxed_homes(tmp):
            headless, path = legacy_config(tmp)
            headless = replace(headless, desktop_access={"mode": "headless"})
            check(
                not team_launcher.legacy_presentation_migration(headless, config_path=path).needed,
                "a headless tenant presents nothing",
            )
            same, _ = legacy_config(tmp, gui=OWNER)
            check(
                not team_launcher.legacy_presentation_migration(same, config_path=path).needed,
                "a tenant whose desktop account is its owner can read its own state",
            )


def test_the_desktop_account_is_the_one_the_policy_pins() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-pinned.") as raw:
        tmp = Path(raw)
        config, _ = legacy_config(tmp)
        with environment(SUDO_USER="somebody-else", **{team_launcher.GUI_USER_ENV: None}):
            check(
                team_launcher.presentation_gui_user(config) == GUI,
                "the recorded Wayland grant wins over whoever ran the command",
            )
        unpinned = replace(config, desktop_access={"mode": "headless"})
        with environment(SUDO_USER="somebody-else", **{team_launcher.GUI_USER_ENV: None}):
            check(
                team_launcher.presentation_gui_user(unpinned) == "somebody-else",
                "and with no grant the old answer stands",
            )


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_a_symlink_in_the_desktop_state_root_is_refused_before_anything_changes() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-symlink.") as raw:
        tmp = Path(raw)
        config, config_path = legacy_config(tmp)
        with sandboxed_homes(tmp) as homes:
            elsewhere = tmp / "elsewhere"
            elsewhere.mkdir()
            (homes[GUI] / ".local").symlink_to(elsewhere)
            before = config_path.read_bytes()
            printed: list[str] = []
            _config, ready = team_launcher.migrate_legacy_presentation(
                config, config_path=config_path, print_func=printed.append
            )
        output = "\n".join(printed)
        check(not ready, f"a symlinked state root is refused: {output}")
        check("symlink" in output and "Nothing was changed" in output, output)
        check(config_path.read_bytes() == before, "and the tenant config is untouched")


def test_a_state_root_owned_by_somebody_else_is_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-owner.") as raw:
        tmp = Path(raw)
        config, config_path = legacy_config(tmp)
        with sandboxed_homes(tmp, gui_uid=os.getuid() + 1) as homes:
            # The desktop account is modelled as uid+1, so every directory this
            # process made -- its home included -- is somebody else's to it.
            (homes[GUI] / ".local").mkdir()
            printed: list[str] = []
            _config, ready = team_launcher.migrate_legacy_presentation(
                config, config_path=config_path, print_func=printed.append
            )
        output = "\n".join(printed)
        check(not ready, output)
        check("is owned by uid" in output, output)


def test_a_destination_outside_the_pinned_accounts_project_root_is_refused() -> None:
    check(
        "is not in"
        in team_launcher.desktop_layout_destination_problem(
            Path(f"/home/{GUI}/.local/state/switchyard/projects/other/{PROJECT}-presentation-layout.json"),
            gui_user=GUI,
            project=PROJECT,
        ),
        "another project's directory is refused",
    )
    check(
        "is not in"
        in team_launcher.desktop_layout_destination_problem(
            Path(f"/home/{GUI}/.local/state/switchyard/projects/{PROJECT}/../../x.json"),
            gui_user=GUI,
            project=PROJECT,
        ),
        "and so is a path that climbs out of it",
    )
    check(
        team_launcher.desktop_layout_destination_problem(
            Path(f"/home/{GUI}/.local/state/switchyard/projects/{PROJECT}/{PROJECT}-presentation-layout.json"),
            gui_user=GUI,
            project=PROJECT,
        )
        == "",
        "while the one place it belongs is accepted",
    )
    check(
        "no project"
        in team_launcher.desktop_layout_destination_problem(Path("/x"), gui_user=GUI, project=""),
        "and a crossing write that names no project is refused outright",
    )


def test_a_config_that_names_another_project_is_not_rewritten() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-mismatch.") as raw:
        tmp = Path(raw)
        config, config_path = legacy_config(tmp)
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        # A valid slug, so a missing check fails on the byte comparison below
        # rather than on the reload that follows a write.
        payload["project"] = "otherproj"
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        before = config_path.read_bytes()
        printed: list[str] = []
        with sandboxed_homes(tmp):
            try:
                _config, ready = team_launcher.migrate_legacy_presentation(
                    config, config_path=config_path, print_func=printed.append
                )
            except SystemExit as exc:
                # A refusal raised later -- by a reload of a file that should
                # never have been written -- is not the refusal this is about;
                # the byte comparison below is what decides.
                printed.append(str(exc))
                ready = False
        check(config_path.read_bytes() == before, f"it is left exactly as it was: {printed}")
        check(not ready, "\n".join(printed))
        check("names project" in "\n".join(printed), printed)


# --------------------------------------------------------------------------
# After the migration: the running workers, and the one readable window
# --------------------------------------------------------------------------


def test_the_migrated_start_opens_the_desktop_path_without_touching_workers() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-recover.") as raw:
        tmp = Path(raw)
        config, config_path = legacy_config(tmp)
        with sandboxed_homes(tmp):
            migrated, ready = team_launcher.migrate_legacy_presentation(
                config, config_path=config_path, print_func=lambda _l: None
            )
            check(ready, "migrated")
            opened: list[Path] = []
            written: list[Path] = []
            calls: list[list[str]] = []
            saved = (team_launcher.launch_konsole_window, team_launcher.write_desktop_layout)
            try:
                team_launcher.launch_konsole_window = lambda output, **_k: opened.append(Path(output)) or 0
                # Crossing needs root; this case is about which path is chosen
                # and which sessions are touched, so the handover is stood down.
                team_launcher.write_desktop_layout = (
                    lambda path, payload, **_k: written.append(Path(path)) or ""
                )
                presentation.launch_presentation(
                    migrated,
                    config_path=config_path,
                    state_path=tmp / "owner-state" / "presentation.json",
                    layout="separate",
                    runner=fake_runner(calls),
                )
            finally:
                team_launcher.launch_konsole_window, team_launcher.write_desktop_layout = saved
            expected = team_launcher.desktop_presentation_layout_path(
                migrated, config_path=config_path, gui_user=GUI
            )
    check(opened == [expected], f"one window, reading the desktop account's copy: {opened}")
    check(str(tmp / "home" / GUI) in str(expected), expected)
    check(written == [expected], f"and that is where the layout was written: {written}")
    worker_sessions = {f"{PROJECT}-{role}" for role in ROLES}
    touched = [
        call for call in calls
        if any(verb in call for verb in ("kill-session", "new-session", "respawn-pane"))
        and any(session in " ".join(call) for session in worker_sessions)
        and not any("display" in part for part in call)
    ]
    check(touched == [], f"no worker session was created, killed or respawned: {touched}")


# --------------------------------------------------------------------------
# Through the real upgrade, not only the helper it calls
# --------------------------------------------------------------------------


def _upgrade_dry_run(tmp: Path, *, attack: bool = False):
    """Run `upgrade_project_command` as root, dry, with a Wayland policy supplied.

    The call site is what this proves: a helper that is right and never called
    is the same defect with a better name.
    """
    import team_launcher_upgrade_cutover_test as cutover

    (tmp / "tenant").mkdir()
    config_path, _ = cutover._declarative_tenant(tmp / "tenant")
    me = team_launcher.current_user_name()
    policy_path = tmp / "policy.json"
    policy_path.write_text(json.dumps(wayland_policy(tenant=me, project="porter")), encoding="utf-8")
    before = config_path.read_bytes()
    printed: list[str] = []
    saved = (
        team_launcher.os.geteuid,
        team_launcher.local_account_exists,
        team_launcher._open_board_url,
    )
    with sandboxed_homes(tmp) as homes:
        if attack:
            (tmp / "elsewhere").mkdir()
            (homes[GUI] / ".local").symlink_to(tmp / "elsewhere")
        try:
            team_launcher.os.geteuid = lambda: 0
            team_launcher.local_account_exists = lambda _account: False
            team_launcher._open_board_url = cutover._unreachable_board
            config = team_launcher.load_project_config("porter", config_path)
            result = team_launcher.upgrade_project_command(
                config,
                config_path=config_path,
                dry_run=True,
                desktop_policy=policy_path,
                source_repo=cutover.TRUSTED_RELEASE_ROOT,
                tooling_root=config_path.parent / "tooling",
                runner=FakeRunner(),
                print_func=printed.append,
            )
        finally:
            (
                team_launcher.os.geteuid,
                team_launcher.local_account_exists,
                team_launcher._open_board_url,
            ) = saved
        gui_tree = sorted(str(path) for path in homes[GUI].rglob("*") if "elsewhere" not in str(path))
    return result, "\n".join(printed), config_path.read_bytes() == before, gui_tree


def test_the_upgrade_dry_run_reports_the_presentation_migration() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-upgrade.") as raw:
        tmp = Path(raw)
        result, output, unchanged, gui_tree = _upgrade_dry_run(tmp)
    check("would give porter a presentation section (6 slot(s)" in output, output[-3000:])
    check(f"{GUI}'s own state directory" in output, output[-3000:])
    check(unchanged, "a dry run leaves the tenant's configuration byte-identical")
    check(gui_tree == [], f"and creates nothing under the desktop account: {gui_tree}")
    check(
        output.index("would give porter a presentation section") < output.index("upgrade phases"),
        "it is reported before any phase, so before anything is declared ready",
    )


def test_the_upgrade_stops_before_readiness_when_the_destination_is_unsafe() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd233-upgrade-refuse.") as raw:
        tmp = Path(raw)
        result, output, unchanged, _tree = _upgrade_dry_run(tmp, attack=True)
    check(result == 1, f"an upgrade whose window could not open is not finished: {result}")
    check("stops before declaring it ready" in output, output[-3000:])
    check("is a symlink or not a directory" in output, output[-3000:])
    check("upgrade phases" not in output, "and no phase report follows the refusal")
    check(unchanged, "and nothing was changed")


# --------------------------------------------------------------------------
# The real accounts, the real kernel
# --------------------------------------------------------------------------


NAMESPACE_CHILD = r'''
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts import team_launcher

root = Path(sys.argv[2])
owner, gui = "stellaris-agent", "eric"
owner_uid, gui_uid = team_launcher.uid_for_user(owner), team_launcher.uid_for_user(gui)
homes = {owner: root / owner, gui: root / gui}
for user, uid in ((owner, owner_uid), (gui, gui_uid)):
    homes[user].mkdir(mode=0o700)
    os.chown(homes[user], uid, uid)

# The tenant's private layout, exactly as the fallback leaves it.
state = homes[owner] / ".local" / "state" / "switchyard" / "projects" / "stellar"
state.mkdir(parents=True, mode=0o700)
for path in (homes[owner] / ".local", homes[owner] / ".local" / "state",
             homes[owner] / ".local" / "state" / "switchyard",
             homes[owner] / ".local" / "state" / "switchyard" / "projects", state):
    os.chown(path, owner_uid, owner_uid); path.chmod(0o700)
tenant_layout = state / "stellar-team-layout.json"
tenant_layout.write_text("{}"); os.chown(tenant_layout, owner_uid, owner_uid); tenant_layout.chmod(0o600)

team_launcher._gui_home = lambda user: str(homes[user])
desktop = team_launcher.desktop_state_dir("stellar", gui) / "stellar-presentation-layout.json"
refusal = team_launcher.write_desktop_layout(
    desktop, {"Orientation": "Vertical"}, gui_user=gui, runner=subprocess.run, project="stellar"
)

def readable_as(uid, path):
    return subprocess.run(
        ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", "cat", str(path)],
        capture_output=True,
    ).returncode == 0

info = os.stat(desktop) if desktop.exists() else None
result = {
    "refusal": refusal,
    "tenant_readable_by_gui": readable_as(gui_uid, tenant_layout),
    "tenant_readable_by_owner": readable_as(owner_uid, tenant_layout),
    "desktop_readable_by_gui": readable_as(gui_uid, desktop),
    "desktop_owner": info.st_uid if info else None,
    "desktop_mode": oct(info.st_mode & 0o777) if info else None,
    "gui_uid": gui_uid,
    "owner_home_mode": oct(os.stat(homes[owner]).st_mode & 0o777),
}
# The write itself, attacked. Each scenario gets a fresh desktop home, and the
# question each time is whether root's write went anywhere but the one place.
def fresh_gui_home(name):
    home = root / name
    home.mkdir(mode=0o700)
    os.chown(home, gui_uid, gui_uid)
    team_launcher._gui_home = lambda user, home=home: str(home if user == gui else homes[user])
    return home

def attempt():
    target = team_launcher.desktop_state_dir("stellar", gui) / "stellar-presentation-layout.json"
    return team_launcher.write_desktop_layout(
        target, {"Orientation": "Vertical"}, gui_user=gui, runner=subprocess.run, project="stellar"
    )

attacks = {}

# A symlinked directory in the chain, pointing into the tenant's private tree.
home = fresh_gui_home("desktop-a")
os.symlink(homes[owner] / ".local", home / ".local")
os.lchown(home / ".local", gui_uid, gui_uid)
before = sorted(str(p) for p in (homes[owner] / ".local").rglob("*"))
refusal = attempt()
attacks["symlinked_chain"] = {
    "refusal": refusal,
    "tenant_tree_unchanged": sorted(str(p) for p in (homes[owner] / ".local").rglob("*")) == before,
}

# A directory in the chain that somebody else owns.
home = fresh_gui_home("desktop-b")
(home / ".local").mkdir(mode=0o755)
os.chown(home / ".local", owner_uid, owner_uid)
refusal = attempt()
attacks["foreign_owner"] = {
    "refusal": refusal,
    "wrote": any((home / ".local").rglob("*presentation-layout.json")),
}

# The destination itself a symlink, aimed at the tenant's layout.
home = fresh_gui_home("desktop-c")
leaf_dir = home / ".local" / "state" / "switchyard" / "projects" / "stellar"
leaf_dir.mkdir(parents=True, mode=0o700)
for path in [home / ".local", *list(leaf_dir.relative_to(home).parents)[:-1], leaf_dir]:
    full = path if path.is_absolute() else home / path
    os.chown(full, gui_uid, gui_uid)
os.symlink(tenant_layout, leaf_dir / "stellar-presentation-layout.json")
os.lchown(leaf_dir / "stellar-presentation-layout.json", gui_uid, gui_uid)
tenant_before = tenant_layout.read_text()
refusal = attempt()
attacks["symlinked_leaf"] = {
    "refusal": refusal,
    "tenant_layout_unchanged": tenant_layout.read_text() == tenant_before,
    "tenant_layout_owner": os.stat(tenant_layout).st_uid == owner_uid,
}

# And a clean rerun on the first home is idempotent.
team_launcher._gui_home = lambda user: str(homes[user])
first = attempt()
second = attempt()
attacks["rerun"] = {"first": first, "second": second}
result["attacks"] = attacks

# Removed from inside, where these uids exist: outside the namespace they are
# subordinate ids this suite cannot delete.
import shutil
shutil.rmtree(root)
root.mkdir()
print(json.dumps(result))
'''


def test_the_kernel_agrees_about_who_can_read_which_layout() -> None:
    """Real uids, real modes, real permission checks -- not a path comparison."""
    if not shutil.which("unshare") or not shutil.which("setpriv"):
        check(False, "unshare and setpriv are required to ask the kernel")
    with tempfile.TemporaryDirectory(prefix="syrd233-kernel.") as raw:
        sandbox = Path(raw) / "homes"
        sandbox.mkdir()
        os.chmod(raw, 0o755)
        sandbox.chmod(0o755)
        child = Path(raw) / "child.py"
        child.write_text(NAMESPACE_CHILD, encoding="utf-8")
        child.chmod(0o755)
        proc = subprocess.run(
            ["unshare", "--user", "--map-auto", "--map-root-user", "--mount",
             sys.executable, str(child), str(ROOT), str(sandbox)],
            capture_output=True, text=True, timeout=120,
        )
        check(proc.returncode == 0, f"the namespace child ran: {proc.stderr[-2000:]}")
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    check(result["refusal"] == "", f"root staged the layout for the desktop account: {result}")
    check(result["tenant_readable_by_owner"], f"the owner reads its own layout: {result}")
    check(
        not result["tenant_readable_by_gui"],
        f"the desktop account CANNOT read the tenant's layout -- the live defect: {result}",
    )
    check(result["desktop_readable_by_gui"], f"and CAN read the one staged for it: {result}")
    check(result["desktop_owner"] == result["gui_uid"], f"which it owns: {result}")
    check(result["desktop_mode"] == "0o600", f"privately: {result}")
    check(result["owner_home_mode"] == "0o700", f"and the tenant's home was not loosened: {result}")

    attacks = result["attacks"]
    chain = attacks["symlinked_chain"]
    # The exact refusal, not the word: a path containing "symlink" would
    # satisfy a substring match whatever the walk had done.
    check(
        chain["refusal"].endswith("/.local is a symlink or not a directory"),
        f"a symlinked directory in the chain is refused as one: {chain}",
    )
    check(chain["tenant_tree_unchanged"], f"and nothing landed where it pointed: {chain}")
    foreign = attacks["foreign_owner"]
    check(
        "/.local is owned by uid " in foreign["refusal"],
        f"a directory somebody else owns is refused: {foreign}",
    )
    check(not foreign["wrote"], f"and nothing was written under it: {foreign}")
    leaf = attacks["symlinked_leaf"]
    check(
        leaf["refusal"].endswith("stellar-presentation-layout.json is not a regular file"),
        f"a symlinked destination is refused: {leaf}",
    )
    check(
        leaf["tenant_layout_unchanged"] and leaf["tenant_layout_owner"],
        f"and the file it pointed at is untouched and still the owner's: {leaf}",
    )
    check(attacks["rerun"] == {"first": "", "second": ""}, f"a rerun is idempotent: {attacks['rerun']}")


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("legacy_presentation_migration_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(120)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"legacy_presentation_migration_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
