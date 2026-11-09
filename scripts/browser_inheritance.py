"""A new project's agent account opens links in the desktop user's default browser, set once at creation (SYRD-751).

A project's provider CLIs run as its agent account, so a browser sign-in opens whatever that account's XDG URL handlers
name, not the desktop user's choice: Eric's default is Firefox, and Claude's re-login opened Chrome. `switchyard new`
now copies the desktop user's effective default browser into the account it provisions, once, after that user has been
identified and has consented to desktop access, and before the first provider sign-in.

- **Read, never written:** the desktop user's `x-scheme-handler/http`, `x-scheme-handler/https` and `text/html`
  handlers, asked as that user. All three must name the same desktop entry, and it must be installed where every
  account can see it.
- **Written as the agent, by the agent:** `xdg-settings set default-web-browser`, run as the agent account with its own
  HOME and nothing else of anyone's environment -- the generic handlers, and KDE's `BrowserApplication` where KDE's
  config tool is installed. It is read back after.
- **Left alone, and said so:** a headless project, or one without approved desktop access; an existing account that
  already names its own browser; a desktop user whose handlers disagree or name an entry that is not installed; any
  failure. None of these stops provisioning.

It runs only from `switchyard new`. Upgrade, launch and resume never call it, and nothing mirrors a later change of the
desktop user's default. An operator changes an existing agent account deliberately, with the command each message
names.
"""

from __future__ import annotations

import configparser
import os
import pwd
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

#: The handlers that decide which application opens a link or a page.
HANDLERS = ("x-scheme-handler/https", "x-scheme-handler/http", "text/html")
#: Where an application's desktop entry is visible to every account (`$dir/applications/<name>.desktop`).
SYSTEM_DATA_DIRS = (Path("/usr/local/share"), Path("/usr/share"), Path("/var/lib/flatpak/exports/share"))
PATH = "/usr/local/bin:/usr/bin:/bin"
#: Where a desktop entry may resolve to, through any symlink, and still be a shared installation: the data directories,
#: the system's own trees, and the flatpak and snap stores whose exports link into them.
SHARED_ROOTS = (*SYSTEM_DATA_DIRS, Path("/usr"), Path("/opt"), Path("/var/lib/flatpak"), Path("/var/lib/snapd/desktop"),
                Path("/snap"))
#: Who must own a shared entry, and the directory it is in, for an agent to trust it: root.
TRUSTED_UID = 0

Runner = Callable[..., subprocess.CompletedProcess[Any]]


def _home(user: str) -> str:
    return pwd.getpwnam(user).pw_dir


def change_command(owner_user: str) -> str:
    """How an operator sets an existing agent account's browser, deliberately."""
    return f"sudo -u {owner_user} -H xdg-settings set default-web-browser NAME.desktop"


def _as(user: str, args: Sequence[str], *, runner: Runner, home_for_user: Callable[[str], str],
        data_dirs: Sequence[Path], extra: Sequence[str] = ()) -> subprocess.CompletedProcess[Any]:
    """`args` as `user`, with that account's HOME and nothing else of root's or the operator's environment."""
    env = ["env", "-i", f"HOME={home_for_user(user)}", f"USER={user}", f"LOGNAME={user}", f"PATH={PATH}",
           f"XDG_DATA_DIRS={':'.join(str(d) for d in data_dirs)}", *extra]
    return runner(["sudo", "-u", user, *env, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                  stdin=subprocess.DEVNULL)


def _out(proc: subprocess.CompletedProcess[Any]) -> str:
    return str(getattr(proc, "stdout", "") or "").strip() if getattr(proc, "returncode", 1) == 0 else ""


def _kde_tools() -> tuple[str, str] | None:
    """(KDE_SESSION_VERSION, kreadconfig) for the installed KDE config tool, or None without one."""
    for version, tool in (("6", "kwriteconfig6"), ("5", "kwriteconfig5")):
        if shutil.which(tool):
            return version, tool.replace("write", "read")
    return None


#: Every per-user list an XDG lookup reads a default from: the plain and desktop-specific lists in the config home and
#: in the data home's applications directory, and the legacy defaults lists (xdg-mime's `check_mimeapps_list`).
USER_LISTS = (".config/*mimeapps.list", ".local/share/applications/*mimeapps.list",
              ".local/share/applications/*defaults.list")
#: Every file the writes below can touch, relative to the agent's home; `{browser}` is a local copy xdg-settings may fix.
WRITTEN = (".config/mimeapps.list", ".config/kde-mimeapps.list", ".config/kdeglobals",
           ".local/share/applications/mimeapps.list", ".local/share/applications/{browser}")
RECORD = "\x1e"


def _listed_choice(text: str) -> str:
    """The first of `HANDLERS` a mimeapps or defaults list names in its [Default Applications], or ""."""
    parser = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",))
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    try:
        parser.read_string(text)
    except configparser.Error:
        return ""
    if parser.has_section("Default Applications"):
        for handler in HANDLERS:
            named = parser.get("Default Applications", handler, fallback="").split(";")[0].strip()
            if named:
                return named
    return ""


def _explicit_choice(owner_user: str, *, runner: Runner, home_for_user: Callable[[str], str],
                     data_dirs: Sequence[Path]) -> str:
    """The browser an existing account names itself, or "": a system default is not a choice.

    Read from every per-user list an XDG lookup consults and KDE's own key; and, whatever file it is kept in, an
    account whose answer differs from what a bare home on the same system gets has made a choice of its own (Audit of
    56ff2441: a choice in the data home's list was missed and overwritten).
    """
    kwargs = dict(runner=runner, home_for_user=home_for_user, data_dirs=data_dirs)
    shown = _as(owner_user, ["sh", "-c", 'cd "$HOME" || exit 0; for f in ' + " ".join(USER_LISTS) + '; do '
                             '[ -f "$f" ] && { printf "\\036%s\\n" "$f"; cat -- "$f"; }; done; exit 0'], **kwargs)
    for record in _out(shown).split(RECORD):
        _path, _, text = record.partition("\n")
        named = _listed_choice(text)
        if named:
            return named
    kde = _kde_tools()
    if kde is not None:
        named = _out(_as(owner_user, [kde[1], "--file", "kdeglobals", "--group", "General", "--key",
                                      "BrowserApplication"], runner=runner, home_for_user=home_for_user,
                         data_dirs=data_dirs))
        if named:
            return named
    bare = 'd=$(mktemp -d) || exit 1; HOME="$d" xdg-mime query default "$1"; rc=$?; rmdir "$d"; exit $rc'
    for handler in HANDLERS:
        own = _out(_as(owner_user, ["xdg-mime", "query", "default", handler], **kwargs))
        system = _out(_as(owner_user, ["sh", "-c", bare, "sh", handler], **kwargs))
        if own and own != system:
            return own
    return ""


def _usable_entry(owner_user: str, browser: str, *, data_dirs: Sequence[Path], shared_roots: Sequence[Path],
                  trusted_uid: int, **kwargs: Any) -> str:
    """Why `owner_user` could not use the desktop entry `browser`, or "": decided by the agent itself, before any write.

    The first of the data directories that has it, in lookup order, as the agent sees it: resolved through any symlink,
    a regular file the agent can read, inside a shared installation root, and -- with its directory -- owned by root and
    writable by no other account. Root seeing a file proves none of that (Audit of eeccc1b9).
    """
    script = ('b="$1"; shift; for d in "$@"; do f="$d/applications/$b"; '
              'if [ -e "$f" ] || [ -L "$f" ]; then '
              'r=$(readlink -f -- "$f") || { printf "unresolved\\t%s\\n" "$f"; exit 0; }; '
              'if [ ! -f "$r" ]; then printf "notfile\\t%s\\t%s\\n" "$f" "$r"; exit 0; fi; '
              'if [ ! -r "$r" ]; then printf "unreadable\\t%s\\t%s\\n" "$f" "$r"; exit 0; fi; '
              'printf "ok\\t%s\\t%s\\t%s\\t%s\\n" "$f" "$r" "$(stat -L -c "%u %a" -- "$r")" '
              '"$(stat -L -c "%u %a" -- "$(dirname -- "$r")")"; exit 0; fi; done; printf "absent\\n"')
    seen = _out(_as(owner_user, ["sh", "-c", script, "sh", browser, *(str(d) for d in data_dirs)],
                    data_dirs=data_dirs, **kwargs)).split("\t")
    verdict = seen[0]
    if verdict in ("", "absent"):
        return f"it is not installed where {owner_user} can open it"
    if verdict == "unreadable":
        return f"{owner_user} cannot read {seen[2]}"
    if verdict != "ok" or len(seen) != 5:
        return f"{seen[1] if len(seen) > 1 else browser} does not resolve to a file {owner_user} can open"
    problem, resolved = _trust_chain(Path(seen[1]), shared_roots=shared_roots, trusted_uid=trusted_uid,
                                     owner_user=owner_user)
    if problem:
        return problem
    if os.path.realpath(seen[2]) != str(resolved):
        return f"{seen[1]} resolved differently for {owner_user} ({seen[2]}) than for root ({resolved})"
    return ""


def _trust_chain(source: Path, *, shared_roots: Sequence[Path], trusted_uid: int,
                 owner_user: str) -> tuple[str, Path]:
    """(why the entry at `source` is not root's alone, "" when it is; the file it finally is).

    Every symlink is followed one hop at a time. For each hop, every directory from the one holding it up to the shared
    root it lies in -- as written and as resolved -- must be root's and writable by no other account: whoever can write
    one of them can replace or repoint the link after this check (Audit of 86500977: a link in a writable directory,
    to a trusted target, was accepted). The file it ends at must be the same. Ownership and modes are the same facts
    whoever reads them; readability is the agent's own check, made before this one.
    """
    logical_roots = [Path(os.path.abspath(root)) for root in shared_roots]
    physical_roots = [Path(os.path.realpath(root)) for root in shared_roots]

    def untrusted(path: Path) -> str:
        info = os.stat(path)
        if info.st_uid != trusted_uid or info.st_mode & 0o022:
            return (f"{path} is not root's alone (owner uid {info.st_uid}, mode {oct(info.st_mode & 0o7777)}), so "
                    f"another account could change or repoint what {owner_user} opens")
        return ""

    def directories(directory: Path) -> str:
        for spelled, roots in ((directory, logical_roots), (Path(os.path.realpath(directory)), physical_roots)):
            root = max((r for r in roots if spelled == r or spelled.is_relative_to(r)), key=lambda r: len(r.parts),
                       default=None)
            if root is None:
                return f"{source} points outside the shared installation directories, to {spelled}"
            walk = spelled
            while True:
                problem = untrusted(walk)
                if problem:
                    return problem
                if walk == root:
                    break
                walk = walk.parent
        return ""

    current = Path(os.path.abspath(source))
    try:
        for _hop in range(40):
            problem = directories(current.parent)
            if problem:
                return problem, current
            info = os.lstat(current)
            if not stat.S_ISLNK(info.st_mode):
                if not stat.S_ISREG(info.st_mode):
                    return f"{current} is not a regular file", current
                problem = untrusted(current)
                final = Path(os.path.realpath(current))
                return (problem.replace(f"{current} is not", f"{current}: it is not", 1) if problem else ""), final
            current = Path(os.path.normpath(os.path.join(os.path.realpath(current.parent), os.readlink(current))))
    except OSError as exc:
        return f"{current} could not be checked ({exc.strerror or exc})", current
    return f"{source} is a chain of more than 40 links", current


def _snapshot(owner_user: str, paths: Sequence[str], **kwargs: Any) -> str:
    """The agent's own copy of each file the writes can touch, or a note that it was absent: the directory, or ""."""
    script = ('set -e; cd "$HOME"; snap=$(mktemp -d); kept=""; trap \'[ -n "$kept" ] || rm -rf -- "$snap"\' EXIT; '
              'i=0; for f in "$@"; do i=$((i+1)); '
              'if [ -e "$f" ] || [ -L "$f" ]; then cp -P -p -- "$f" "$snap/$i"; else : > "$snap/$i.absent"; fi; '
              'done; kept=1; printf "%s" "$snap"')
    return _out(_as(owner_user, ["sh", "-c", script, "sh", *paths], **kwargs))


def _restore(owner_user: str, snap: str, paths: Sequence[str], **kwargs: Any) -> bool:
    """Put each file back as the snapshot had it -- or remove it, if it was absent -- as the agent; then drop it."""
    script = ('cd "$HOME" || exit 1; snap="$1"; shift; i=0; ok=0; for f in "$@"; do i=$((i+1)); '
              'if [ -e "$snap/$i.absent" ]; then rm -f -- "$f" || ok=1; '
              'elif [ -e "$snap/$i" ] || [ -L "$snap/$i" ]; then rm -f -- "$f" && cp -P -p -- "$snap/$i" "$f" || ok=1; fi; '
              'done; rm -rf -- "$snap"; exit $ok')
    return getattr(_as(owner_user, ["sh", "-c", script, "sh", snap, *paths], **kwargs), "returncode", 1) == 0


def inherit_desktop_browser(
    desktop_policy: dict,
    *,
    owner_user: str,
    created: bool,
    runner: Runner = subprocess.run,
    home_for_user: Callable[[str], str] = _home,
    data_dirs: Sequence[Path] = SYSTEM_DATA_DIRS,
    shared_roots: Sequence[Path] = SHARED_ROOTS,
    trusted_uid: int = TRUSTED_UID,
    print_func: Callable[[str], None] = print,
) -> str:
    """Copy the consenting desktop user's default browser into `owner_user`, once. Returns what became of it:
    "set", "kept", "headless", "unknown", "uninstalled" or "failed". Never raises: a browser is not worth a provision.
    """
    say = lambda line: print_func(f"switchyard: browser: {line}")  # noqa: E731
    change = change_command(owner_user)
    gui_user = str(desktop_policy.get("gui_user") or "") if isinstance(desktop_policy, dict) else ""
    if not isinstance(desktop_policy, dict) or desktop_policy.get("mode") != "wayland" or not gui_user:
        say(f"{owner_user}'s browser is left as this host's default: the project has no approved desktop user to "
            f"take it from. Set one deliberately with `{change}`.")
        return "headless"
    if gui_user == owner_user:
        say(f"{owner_user} is the desktop user itself; its browser is its own.")
        return "kept"
    try:
        kwargs = dict(runner=runner, home_for_user=home_for_user, data_dirs=data_dirs)
        if not created:
            own = _explicit_choice(owner_user, **kwargs)
            if own:
                say(f"{owner_user} already names its own browser, {own}; it is kept, not replaced by {gui_user}'s.")
                return "kept"
        seen = {handler: _out(_as(gui_user, ["xdg-mime", "query", "default", handler], **kwargs))
                for handler in HANDLERS}
        names = {name for name in seen.values() if name}
        if len(names) != 1 or "" in seen.values():
            said = ", ".join(f"{handler.rsplit('/', 1)[-1]} {name or 'none'}" for handler, name in seen.items())
            say(f"{gui_user}'s default browser could not be told ({said}), so {owner_user} keeps this host's default. "
                f"Set it deliberately with `{change}`.")
            return "unknown"
        browser = names.pop()
        unusable = (f"{browser} is not a desktop entry name" if not browser.endswith(".desktop") or "/" in browser
                    else _usable_entry(owner_user, browser, shared_roots=shared_roots, trusted_uid=trusted_uid,
                                       **kwargs))
        if unusable:
            say(f"{gui_user}'s default browser, {browser}, cannot be {owner_user}'s: {unusable}. So {owner_user} keeps "
                f"this host's default, untouched. Set it deliberately with `{change}`.")
            return "uninstalled"
        # The account's own config directory, made by the account: xdg-mime cannot write into one that is missing.
        made = _as(owner_user, ["mkdir", "-p", "--", f"{home_for_user(owner_user)}/.config"], **kwargs)
        paths = [path.format(browser=browser) for path in WRITTEN]
        snap = _snapshot(owner_user, paths, **kwargs) if getattr(made, "returncode", 1) == 0 else ""
        if not snap:
            say(f"{owner_user}'s browser was not set: its own settings could not be saved first, so nothing was "
                f"changed. Set it deliberately with `{change}`.")
            return "failed"
        return _write_or_restore(owner_user, browser, gui_user=gui_user, snap=snap, paths=paths, say=say,
                                 change=change, kwargs=kwargs)
    except (OSError, KeyError, subprocess.SubprocessError) as exc:  # before anything was written: nothing to put back
        say(f"{owner_user}'s browser was not set ({exc}); it keeps this host's default. Set it deliberately with "
            f"`{change}`.")
        return "failed"


def _write_or_restore(owner_user: str, browser: str, *, gui_user: str, snap: str, paths: Sequence[str],
                      say: Callable[[str], None], change: str, kwargs: dict) -> str:
    """Everything after the snapshot: the writes, all or nothing. A refusal, an unexpected answer and an exception all
    take the one way back -- every file restored, the handlers read again, what holds said, the snapshot gone (Audit of
    56ff2441 and 50dcbdc6: a refusal, then an exception, after the generic step had changed every handler, each left
    the change in place and said the account kept its default)."""
    errors = (OSError, KeyError, subprocess.SubprocessError)

    def handlers() -> dict[str, str]:
        return {h: _out(_as(owner_user, ["xdg-mime", "query", "default", h], **kwargs)) for h in HANDLERS}

    before: dict[str, str] | None = None
    why = ""
    try:
        before = handlers()
        steps: list[list[str]] = [[]]
        kde = _kde_tools()
        if kde is not None:
            steps.append(["XDG_CURRENT_DESKTOP=KDE", f"KDE_SESSION_VERSION={kde[0]}"])
        for extra in steps:  # one after another, each only if the last held
            step = _as(owner_user, ["xdg-settings", "set", "default-web-browser", browser], extra=extra, **kwargs)
            if getattr(step, "returncode", 1) != 0:
                why = (str(getattr(step, "stderr", "") or "").strip().splitlines() or ["it was refused"])[-1]
                break
        if not why:
            got = set(handlers().values())
            if got != {browser}:
                why = f"it read back as {', '.join(sorted(got)) or 'nothing'}"
    except errors as exc:
        why = str(exc) or type(exc).__name__
    if not why:
        try:
            _as(owner_user, ["rm", "-rf", "--", snap], **kwargs)
        except errors:
            pass  # set and read back: a snapshot left in the agent's own temporary directory changes nothing
        say(f"{owner_user} opens links with {browser}, {gui_user}'s default browser, set once now: a later change of "
            f"{gui_user}'s default is not copied. Change it deliberately with `{change}`.")
        return "set"
    try:
        restored = _restore(owner_user, snap, paths, **kwargs)
    except errors:
        restored = False
    if not restored:
        try:
            _as(owner_user, ["rm", "-rf", "--", snap], **kwargs)
        except errors:
            pass
    try:
        after: dict[str, str] | None = handlers()
    except errors:
        after = None
    if restored and after is not None and (before is None or after == before):
        say(f"{owner_user}'s browser could not be set to {browser} ({why[:200]}); its own settings are put back as "
            f"they were, so it opens links as before ({after['x-scheme-handler/https'] or 'no handler'}). "
            f"Set it deliberately with `{change}`.")
    elif after is not None:
        said = ", ".join(f"{h.rsplit('/', 1)[-1]} {n or 'none'}" for h, n in after.items())
        say(f"{owner_user}'s browser could not be set to {browser} ({why[:200]}), and its settings could not all be "
            f"put back: it now opens {said}. Set it deliberately with `{change}`.")
    else:
        say(f"{owner_user}'s browser could not be set to {browser} ({why[:200]}), and what it opens now could not be "
            f"read back{'' if restored else ' nor all of its settings put back'}. Check it, and set it deliberately "
            f"with `{change}`.")
    return "failed"
