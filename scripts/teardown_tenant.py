"""Removing a whole tenant: what `switchyard teardown <slug>` does by default, and what it must leave nothing of (SYRD-786).

The User, after `sudo switchyard teardown test13 --confirm test13 --destroy-registered-tenant --remove-owner-home
--remove-owner-user`: "the flags are supposed to be used when I want to deviate from the norm. I should only need
switchyard teardown test12". The teardowns of test11 and test12 also left their tenants half there: the panes' tmux
servers and agents still running as the account (outside its user manager and any logind session, so stopping those
reached none of them), so `userdel` refused it; the linger file; the desktop grant's ACL entries naming the uid; and
root's rollout journal group-owned by a gid the next account was given.

This module holds what the teardown command adds for that, kept apart from the moved `project_teardown`:

- `process_stop_script` ends every process of the tenant's account -- its user manager and session, then everything
  else (tmux servers, agents) with SIGTERM and then SIGKILL, each bounded -- and fails, naming what is left, unless
  none remains. It never acts on the account running the teardown or the one behind sudo;
- `withdraw_desktop` takes back the tenant's desktop grant: each grant's own uninstall, as the desktop user, from the
  desktop user's own record of it (`installed_grant_policies`), then any ACL entry naming the uid on a desktop user's
  runtime, its Wayland sockets or its grant directory;
- `settle_root_records` removes root's per-tenant directories (the staged tooling, root's provisioning record), keeps
  root's rollout journal but re-owns it to root, and re-owns to root anything else under root's Switchyard
  directories that the uid or gid still owns -- saying what it kept and why;
- `preview_facts`, `self_tenant_refusal`, `agent_account_refusal` and `confirm_by_typing` are the preview and the
  guards that replace the old flags: the home's size and the processes running, never the tenant this command runs
  from, never an account that is not a tenant's agent account, and the slug typed once.

Run as a program (`python3 -I teardown_tenant.py withdraw-desktop|root-records ...`), it is the step the teardown plan
shows and runs, as root.
"""

from __future__ import annotations

import grp
import os
import pwd
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

#: Groups whose members are people or administrators, never a tenant's agent account.
ADMIN_GROUPS = frozenset({"root", "sudo", "wheel", "adm", "admin"})
LINGER_DIR = Path("/var/lib/systemd/linger")
#: Root's Switchyard directories, swept for anything the removed uid or gid still owns.
ROOT_RECORD_DIRS = (Path("/etc/switchyard"), Path("/var/lib/switchyard"), Path("/usr/local/lib/switchyard"),
                    Path("/run/switchyard-desktop"))
STAGED_TOOLING_ROOT = Path("/usr/local/lib/switchyard")
#: Where desktop users' runtimes and grant directories are.
USER_RUNTIME_ROOT = Path("/run/user")
DESKTOP_GRANT_ROOT = Path("/run/switchyard-desktop")


# -- the account's processes ----------------------------------------------------------------------------------------
def linger_script(owner_user: str) -> str:
    """Disable linger and remove its file: `disable-linger` left `/var/lib/systemd/linger/<user>` behind on test11."""
    quoted = shlex.quote(owner_user)
    return (f"if id -u {quoted} >/dev/null 2>&1; then loginctl disable-linger {quoted} 2>/dev/null || true; "
            "else echo 'account already absent; nothing to unlinger'; fi; "
            f"rm -f -- {shlex.quote(str(LINGER_DIR / owner_user))}")


def process_stop_script(owner_user: str) -> str:
    """End every process of `owner_user`: its user manager and session, then -- because tmux servers and agents
    started for its panes are under neither -- every process with that uid, SIGTERM then SIGKILL, each up to five
    seconds. Exits 0 only when none remains; refuses the account running this, the one behind sudo or pkexec, and
    any uid below 1000."""
    quoted = shlex.quote(owner_user)
    return (
        f"if ! id -u {quoted} >/dev/null 2>&1; then echo 'account already absent; no process to stop'; exit 0; fi; "
        f"owner_uid=$(id -u {quoted}); "
        'if [ "$owner_uid" = "$(id -u)" ] || [ "$owner_uid" = "${SUDO_UID:-}" ] || [ "$owner_uid" = "${PKEXEC_UID:-}" ] '
        '|| [ "$owner_uid" -lt 1000 ]; then '
        f"echo 'refusing to stop the processes of {owner_user}: it is the account running this teardown, or a "
        "system account' >&2; exit 1; fi; "
        'systemctl stop "user@${owner_uid}.service" 2>/dev/null || true; '
        f"loginctl terminate-user {quoted} 2>/dev/null || true; "
        'systemctl stop "user-runtime-dir@${owner_uid}.service" 2>/dev/null || true; '
        "for signal in TERM KILL; do "
        'pkill "-$signal" -U "$owner_uid" 2>/dev/null || true; pkill "-$signal" -u "$owner_uid" 2>/dev/null || true; '
        'for _ in $(seq 1 25); do pgrep -U "$owner_uid" >/dev/null 2>&1 || pgrep -u "$owner_uid" >/dev/null 2>&1 '
        "|| break; sleep 0.2; done; "
        'if ! pgrep -U "$owner_uid" >/dev/null 2>&1 && ! pgrep -u "$owner_uid" >/dev/null 2>&1; then '
        f"echo 'no process of {owner_user} remains'; exit 0; fi; "
        "done; "
        f"echo 'processes still running as {owner_user} after SIGTERM and SIGKILL:' >&2; "
        'ps -o pid=,comm= -U "$owner_uid" >&2; exit 1'
    )


# -- the desktop grant ------------------------------------------------------------------------------------------------
def _acl_paths(user_runtime_root: Path, desktop_grant_root: Path) -> list[Path]:
    """Every path a desktop grant names tenants on: each user runtime and its Wayland sockets, each grant directory
    and what is in it."""
    paths: list[Path] = []
    for root in (user_runtime_root, desktop_grant_root):
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for directory in children:
            if directory.is_symlink() or not directory.is_dir():
                continue
            paths.append(directory)
            try:
                inside = sorted(directory.iterdir())
            except OSError:
                continue
            for path in inside:
                if path.is_symlink():
                    continue
                if root == desktop_grant_root or path.name.startswith("wayland-"):
                    paths.append(path)
    return paths


def _acl_entries(path: Path, runner: Callable[..., Any]) -> list[str]:
    result = runner(["getfacl", "-cpn", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if getattr(result, "returncode", 1) != 0:
        return []
    return [line for line in str(result.stdout or "").splitlines() if line and not line.startswith("#")]


def installed_grant_policies(owner_uid: int, *, accounts: Callable[[], Sequence[Any]] = pwd.getpwall
                             ) -> list[tuple[Path, dict[str, Any]]]:
    """The desktop grants installed for `owner_uid`, as each desktop user recorded them in its own home
    (`~/.config/switchyard/desktop-access/switchyard-desktop-<gui uid>-<tenant uid>-<project>.json`, owned by that
    user): the desktop user's record, never anything the tenant can write."""
    import json

    found = []
    for account in accounts():
        directory = Path(account.pw_dir) / ".config" / "switchyard" / "desktop-access"
        try:
            records = sorted(directory.glob(f"switchyard-desktop-{account.pw_uid}-{owner_uid}-*.json"))
        except OSError:
            continue
        for record in records:
            try:
                info = record.lstat()
                if record.is_symlink() or info.st_uid != account.pw_uid:
                    continue
                policy = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(policy, dict):
                found.append((record, policy))
    return found


def withdraw_desktop(
    owner_uid: int,
    *,
    policies: Sequence[tuple[Path, Mapping[str, Any]]] = (),
    user_runtime_root: Path = USER_RUNTIME_ROOT,
    desktop_grant_root: Path = DESKTOP_GRANT_ROOT,
    runner: Callable[..., Any] = subprocess.run,
    uninstall: Callable[[Mapping[str, Any]], Any] | None = None,
) -> list[str]:
    """Take back the tenant's desktop grant: each installed grant's own uninstall, as its desktop user, then every
    ACL entry still naming `owner_uid`. Returns what it did, one line each."""
    said: list[str] = []
    for record, policy in policies:
        try:
            if uninstall is None:
                from scripts import desktop_access

                uninstall = desktop_access.uninstall
            uninstall(dict(policy))
            said.append(f"uninstalled the desktop grant {record.name}")
        except Exception as exc:  # noqa: BLE001 - the ACL sweep below still runs, and says what it removed
            said.append(f"the desktop grant {record.name} did not uninstall ({exc}); its ACL entries are removed "
                        "below")
    for path in _acl_paths(user_runtime_root, desktop_grant_root):
        if not any(entry.startswith(f"user:{owner_uid}:") for entry in _acl_entries(path, runner)):
            continue
        result = runner(["setfacl", "--no-mask", "-x", f"u:{owner_uid}", str(path)], stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True)
        if getattr(result, "returncode", 1) != 0:
            raise RuntimeError(f"cannot remove uid {owner_uid}'s ACL entry from {path}: "
                               f"{str(result.stderr or '').strip() or 'setfacl failed'}")
        said.append(f"removed uid {owner_uid}'s ACL entry from {path}")
    return said


# -- root's records -------------------------------------------------------------------------------------------------
def _owned_by(path: Path, uid: int | None, gid: int | None) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (uid is not None and info.st_uid == uid) or (gid is not None and info.st_gid == gid)


def settle_root_records(
    project: str,
    *,
    uid: int | None,
    gid: int | None,
    staged_tooling_root: Path = STAGED_TOOLING_ROOT,
    provision_dir: Path | None = None,
    rollout_root: Path | None = None,
    publication_files: Sequence[Path] = (),
    publication_staging: Path | None = None,
    record_dirs: Sequence[Path] = ROOT_RECORD_DIRS,
    chown: Callable[[str, int, int], None] = os.lchown,
) -> list[str]:
    """Leave root nothing of the tenant that its uid, gid or account name could later reach: remove its staged
    tooling, root's provisioning record and its publication grant (the sudoers rule naming its account, root's push
    grant and key, its staging), keep root's rollout journal (the record of what root ran for it) re-owned to root and
    closed, and re-own anything else under root's directories still owned by the uid or gid. Returns what it did and
    kept."""
    said: list[str] = []
    for path in publication_files:
        if os.path.lexists(path):
            if path.is_dir() and not path.is_symlink():
                raise RuntimeError(f"{path} is a directory, not a publication file; it is left alone")
            path.unlink()
            said.append(f"removed its publication record {path}")
    for directory, what in ((staged_tooling_root / project, "its staged tooling"),
                            (provision_dir, "root's provisioning record"),
                            (publication_staging, "its publication staging")):
        if directory is None or not os.path.lexists(directory):
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError(f"{directory} is not a directory; it is left alone")
        shutil.rmtree(directory)
        said.append(f"removed {what} {directory}")
    if rollout_root is not None:
        journal = rollout_root / project
        if os.path.lexists(journal) and not journal.is_symlink() and journal.is_dir():
            for path in [journal, *journal.rglob("*")]:
                if not path.is_symlink():
                    chown(str(path), 0, 0)
            os.chmod(journal, 0o700)
            said.append(f"kept root's rollout journal {journal} as the record of what root ran for {project}; "
                        "re-owned to root and closed to everyone else")
    if uid is not None or gid is not None:
        for root in record_dirs:
            if not root.is_dir():
                continue
            for path in [root, *root.rglob("*")]:
                if _owned_by(path, uid, gid):
                    chown(str(path), 0, 0)
                    said.append(f"re-owned {path} to root: it was still owned by the tenant's uid or gid")
    return said


# -- the preview and the guards --------------------------------------------------------------------------------------
def preview_facts(owner_user: str, owner_home: Path, *,
                  runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """What the preview says beyond the plan: the home's size and the account's running processes."""
    size = ""
    if owner_home.is_dir():
        result = runner(["du", "-sh", "--", str(owner_home)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        text=True)
        size = (str(getattr(result, "stdout", "") or "").split() or [""])[0]
    processes: list[str] = []
    try:
        pwd.getpwnam(owner_user)
        exists = True
    except KeyError:
        exists = False
    if exists:
        result = runner(["ps", "-o", "comm=", "-U", owner_user], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        text=True)
        processes = [line.strip() for line in str(getattr(result, "stdout", "") or "").splitlines() if line.strip()]
    return {"home_size": size, "processes": processes, "account_exists": exists}


def describe_processes(processes: Sequence[str]) -> str:
    if not processes:
        return "none"
    counts: dict[str, int] = {}
    for name in processes:
        counts[name] = counts.get(name, 0) + 1
    shown = ", ".join(f"{name} x{count}" if count > 1 else name for name, count in sorted(counts.items())[:8])
    return f"{len(processes)} ({shown}{', ...' if len(counts) > 8 else ''})"


def self_tenant_refusal(project: str, *, registry_name: str = "") -> str:
    """Why `project` is the tenant this command runs from, or "": the project of the pane running it, or the
    Switchyard project's own board."""
    from scripts.upstream_report_sinks import SWITCHYARD_BOARD_NAME

    if os.environ.get("TICKET_BOARD_PROJECT", "").strip() == project:
        return f"{project} is the project this command runs from; a tenant does not tear itself down"
    if registry_name == SWITCHYARD_BOARD_NAME:
        return f"{project} is the Switchyard project's own board; it is never torn down as a tenant"
    return ""


def agent_account_refusal(owner_user: str, *, group_names: Callable[[str], set[str]] | None = None) -> str:
    """Why `owner_user` is not a tenant's agent account, or "": a member of an administrators' group is a person or
    an administrator, whatever the plan says."""
    def groups_of(user: str) -> set[str]:
        names = {group.gr_name for group in grp.getgrall() if user in group.gr_mem}
        try:
            names.add(grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name)
        except KeyError:
            pass
        return names

    privileged = sorted((group_names or groups_of)(owner_user) & ADMIN_GROUPS)
    if privileged:
        return (f"{owner_user} belongs to {', '.join(privileged)}; that is a person's or an administrator's account, "
                "not a tenant's agent account")
    return ""


def registered_owner(entry_path: Path) -> tuple[str, str]:
    """The slug a registry entry registers and the account its tenant runs as: its configuration's `run_as_user`, else
    its plan's `owner_user`, else `<slug>-agent`. Raises ValueError, naming what could not be read."""
    import json

    def load(path: Path, what: str) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read {what} {path}: {exc}") from None
        if not isinstance(value, dict):
            raise ValueError(f"{what} {path} is not a JSON object")
        return value

    entry = load(entry_path, "registry entry")
    if str(entry.get("schema") or "") != "switchyard.project-registry.v1":
        return "", ""  # not a registry entry: it registers no tenant
    slug = str(entry.get("slug") or entry_path.stem)
    config_path = Path(str(entry.get("config_path") or ""))
    if not str(entry.get("config_path") or ""):
        raise ValueError(f"registry entry {entry_path} names no configuration")
    owner = str(load(config_path, "configuration").get("run_as_user") or "").strip()
    if not owner:
        plan = config_path.parent / "plan.json"
        if plan.exists():
            owner = str(load(plan, "plan").get("owner_user") or "").strip()
    return slug, owner or f"{slug}-agent"


def other_tenant_refusal(project: str, owner_user: str, *, registry_dir: Path, owner_home: Path) -> str:
    """Why `owner_user` belongs to a tenant other than `project`, or "": another registry entry's tenant runs as it, a
    registry entry whose account cannot be read might, or its home holds another project's Switchyard state. The typed
    slug confirms the slug, not the account, so this is what keeps a leftover's teardown off a live tenant (SYRD-786)."""
    try:
        # Never through a link: a registry entry is a regular file root wrote (SYRD-709).
        entries = sorted(path for path in registry_dir.glob("*.json") if path.stem != project and not path.is_symlink())
    except OSError as exc:
        return f"the registry {registry_dir} cannot be read ({exc}), so whether {owner_user} is another tenant's is unknown"
    for path in entries:
        try:
            slug, owner = registered_owner(path)
        except ValueError as exc:
            return (f"{exc}; that tenant's account is unknown and could be {owner_user}, so nothing of {owner_user} is "
                    "touched. Repair that entry, or run the teardown with permissions that can read it")
        if slug and slug != project and owner == owner_user:
            return f"{owner_user} is {slug}'s account; tear down {slug} instead"
    try:
        states = sorted(child.name for child in (owner_home / ".local" / "state" / "switchyard" / "projects").iterdir()
                        if child.is_dir() and child.name != project)
    except OSError:
        states = []
    if states:
        return (f"{owner_user}'s home holds Switchyard state for {', '.join(states)} "
                f"({owner_home / '.local' / 'state' / 'switchyard' / 'projects'}); it is not {project}'s alone")
    return ""


def private_group_gid(account: Any, *, accounts: Callable[[], Sequence[Any]] = pwd.getpwall,
                      group_name: Callable[[int], str] | None = None) -> int | None:
    """The account's primary gid when that group is its own -- named after it and nobody else's primary group -- so a
    sweep by gid touches only this tenant's records; otherwise None, and the sweep goes by uid alone."""
    def name_of(gid: int) -> str:
        try:
            return grp.getgrgid(gid).gr_name
        except KeyError:
            return ""

    gid = account.pw_gid
    if (group_name or name_of)(gid) != account.pw_name:
        return None
    if any(other.pw_gid == gid and other.pw_name != account.pw_name for other in accounts()):
        return None
    return gid


def confirm_by_typing(project: str, *, yes: bool, confirm: str | None,
                      input_func: Callable[[str], str]) -> None:
    """The one safety step: the slug typed, or `--yes` / `--confirm <slug>` for a script. Raises SystemExit, having
    changed nothing, when it is not given."""
    from scripts import new_project_questions
    from scripts import team_launcher as launcher

    if confirm is not None:
        if confirm.strip() != project:
            raise SystemExit(f"switchyard: --confirm {confirm!r} does not name {project}; nothing was changed")
        return
    if yes:
        return
    if not new_project_questions.can_ask(input_func):
        raise SystemExit(f"switchyard: teardown asks for {project} to be typed, and nobody can answer here; "
                         "pass --yes (or --confirm <slug>) to run it unattended. Nothing was changed.")
    answer = launcher._read_prompt(f"Type {project} to remove it: ", input_func=input_func).strip()
    if answer != project:
        raise SystemExit(f"switchyard: {answer!r} is not {project}; nothing was changed")


# -- the steps the plan runs -----------------------------------------------------------------------------------------
def step_command(*args: str) -> tuple[str, ...]:
    """This module run as a program, isolated, by the interpreter running the teardown."""
    return (sys.executable, "-I", str(Path(__file__).resolve()), *args)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    action, rest = (argv[0], argv[1:]) if argv else ("", [])
    if action == "withdraw-desktop" and len(rest) == 1:
        owner = rest[0]
        try:
            owner_uid = pwd.getpwnam(owner).pw_uid
        except KeyError:
            print(f"{owner} is already gone; its uid cannot be named, and nothing is withdrawn")
            return 0
        lines = withdraw_desktop(owner_uid, policies=installed_grant_policies(owner_uid))
        for line in lines or [f"{owner} holds no desktop grant"]:
            print(line)
        return 0
    if action == "root-records" and len(rest) == 4:
        project, owner, provision_dir, rollout_root = rest
        try:
            account = pwd.getpwnam(owner)
            uid, gid = account.pw_uid, private_group_gid(account)
        except KeyError:
            uid = gid = None
        from scripts.ticket_board import provision_publication_grants as publication

        key = Path(publication.publish_identity_path(project))
        files = (publication.publish_sudoers_path(project), Path(publication.publish_grant_path(project)), key,
                 key.with_name(key.name + ".pub"))
        for line in settle_root_records(project, uid=uid, gid=gid, provision_dir=Path(provision_dir),
                                        rollout_root=Path(rollout_root), publication_files=files,
                                        publication_staging=Path(publication.publish_staging_root()) / f"{project}.git"):
            print(line)
        return 0
    print("usage: teardown_tenant.py withdraw-desktop OWNER | root-records PROJECT OWNER PROVISION ROLLOUT",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
