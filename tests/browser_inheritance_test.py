#!/usr/bin/env python3
"""SYRD-751: a new project's agent account inherits the consenting desktop user's default browser, once.

Everything here is the real thing except the account switch. The real `xdg-mime`, `xdg-settings` and KDE config tool
run on real files in throwaway home directories, against desktop entries in a throwaway data directory. The runner
strips only `sudo -u <user>` from each command and records whom it was for. So the test shows the desktop user is only
ever asked (`xdg-mime query`), and every write is the agent's.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import browser_inheritance as bi  # noqa: E402

CHECKS = 0
DESK, AGENT = "syrd751-desk", "syrd751-agent"
WAYLAND = {"mode": "wayland", "gui_user": DESK, "project": "p751", "tenant_user": AGENT,
           "consent": {"approved": True}}


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Host:
    """Two homes, a data directory with two browsers, and a runner that runs each command as asked, recording whom
    for."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.homes = {DESK: tmp / "home-desk", AGENT: tmp / "home-agent"}
        for home in self.homes.values():
            home.mkdir()
        self.share = tmp / "share"
        (self.share / "applications").mkdir(parents=True)
        for name in ("foxtest", "chrometest"):
            (self.share / "applications" / f"{name}.desktop").write_text(
                f"[Desktop Entry]\nType=Application\nName={name}\nExec=/usr/bin/true %u\n"
                "MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;\n", encoding="utf-8")
        self.calls: list[tuple[str, list[str]]] = []
        self.fail: str = ""
        self.fail_when = lambda args: False
        self.raise_when = lambda args: False
        self.snaps: list[str] = []
        self.trusted = os.getuid()  # the fixture's entries are this account's; production trusts root alone

    def home(self, user: str) -> str:
        return str(self.homes[user])

    def runner(self, args, **kwargs):
        assert args[:2] == ["sudo", "-u"] and args[3:5] == ["env", "-i"], args
        user, rest = args[2], args[3:]
        command = [word for word in rest[2:] if "=" not in word or word.startswith("--")]
        self.calls.append((user, command))
        if self.raise_when(args):
            raise OSError("second launch failed")
        if (self.fail and self.fail in " ".join(command)) or self.fail_when(args):
            return subprocess.CompletedProcess(args, 1, "", f"{self.fail or 'this step'} refused here\n")
        done = subprocess.run(rest, **kwargs)
        if any("snap=$(mktemp -d)" in word for word in rest) and done.returncode == 0:
            self.snaps.append(str(done.stdout).strip())
        return done

    def xdg(self, user: str, *args: str, kde: bool = False) -> str:
        extra = ["XDG_CURRENT_DESKTOP=KDE", "KDE_SESSION_VERSION=6"] if kde else []
        done = subprocess.run(["env", "-i", f"HOME={self.homes[user]}", "PATH=/usr/local/bin:/usr/bin:/bin",
                               f"XDG_DATA_DIRS={self.share}", *extra, *args], capture_output=True, text=True)
        return done.stdout.strip()

    def browse(self, user: str, browser: str) -> None:
        (self.homes[user] / ".config").mkdir(exist_ok=True)
        self.xdg(user, "xdg-settings", "set", "default-web-browser", browser)

    def handlers(self, user: str) -> set[str]:
        return {self.xdg(user, "xdg-mime", "query", "default", handler) for handler in bi.HANDLERS}

    def tree(self, user: str) -> dict:
        return {str(p.relative_to(self.homes[user])): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.homes[user].rglob("*")) if p.is_file()}

    def inherit(self, policy=WAYLAND, *, created: bool = True) -> tuple[str, list[str]]:
        said: list[str] = []
        outcome = bi.inherit_desktop_browser(policy, owner_user=AGENT, created=created, runner=self.runner,
                                             home_for_user=self.home, data_dirs=(self.share,), print_func=said.append,
                                             shared_roots=(self.share, self.tmp / "opt"), trusted_uid=self.trusted)
        return outcome, said


def host():
    raw = tempfile.TemporaryDirectory(prefix="syrd751.")
    return raw, Host(Path(raw.name))


def test_a_fresh_account_inherits_the_desktop_users_browser_and_only_the_agent_writes() -> None:
    for browser in ("foxtest.desktop", "chrometest.desktop"):  # discovered, never a name the code knows
        raw, h = host()
        with raw:
            h.browse(DESK, browser)
            desk_before = h.tree(DESK)
            outcome, said = h.inherit()
            check(outcome == "set" and h.handlers(AGENT) == {browser},
                  f"{browser}: the fresh account's http, https and html handlers name the desktop user's browser: "
                  f"{outcome} {h.handlers(AGENT)} {said}")
            kde = bi._kde_tools()
            if kde is not None:
                named = h.xdg(AGENT, kde[1], "--file", "kdeglobals", "--group", "General", "--key", "BrowserApplication")
                check(named == browser, f"{browser}: and KDE's own BrowserApplication for the account too: {named!r}")
            check(h.tree(DESK) == desk_before, f"{browser}: the desktop user's files are byte-for-byte unchanged")
            desk_calls = [command for user, command in h.calls if user == DESK]
            check(desk_calls and all(command[:3] == ["xdg-mime", "query", "default"] for command in desk_calls),
                  f"{browser}: the desktop user is only asked: {desk_calls}")
            writes = [(user, command) for user, command in h.calls
                      if command[:1] == ["mkdir"] or command[:2] == ["xdg-settings", "set"]]
            check(writes and all(user == AGENT for user, _ in writes),
                  f"{browser}: every write is the agent's own: {writes}")
            check(any(f"opens links with {browser}, {DESK}'s default browser, set once now" in s for s in said)
                  and any("sudo -u syrd751-agent -H xdg-settings set default-web-browser NAME.desktop" in s
                          for s in said), f"{browser}: said once, with the deliberate change: {said}")


def test_an_existing_account_with_its_own_browser_keeps_it() -> None:
    raw, h = host()
    with raw:
        h.browse(DESK, "foxtest.desktop")
        h.browse(AGENT, "chrometest.desktop")
        before = h.tree(AGENT)
        outcome, said = h.inherit(created=False)
        check(outcome == "kept" and h.tree(AGENT) == before and h.handlers(AGENT) == {"chrometest.desktop"},
              f"a reused account's own choice is kept, byte for byte: {outcome} {said}")
        check(any("already names its own browser, chrometest.desktop; it is kept" in s for s in said), said)
    raw, h = host()
    with raw:
        h.browse(DESK, "foxtest.desktop")
        outcome, _said = h.inherit(created=False)
        check(outcome == "set" and h.handlers(AGENT) == {"foxtest.desktop"},
              "a reused account that names no browser of its own inherits it")
    raw, h = host()
    with raw:
        h.browse(DESK, "foxtest.desktop")
        h.browse(AGENT, "chrometest.desktop")
        outcome, _said = h.inherit(created=True)
        check(outcome == "set" and h.handlers(AGENT) == {"foxtest.desktop"},
              "a freshly created account inherits it, whatever a skeleton gave it")


def test_an_existing_accounts_choice_is_kept_wherever_it_keeps_it() -> None:
    """Audit of 56ff2441: a choice in the data home's list was missed, and overwritten."""
    for where in (".local/share/applications/mimeapps.list", ".config/kde-mimeapps.list",
                  ".local/share/applications/defaults.list"):
        raw, h = host()
        with raw:
            h.browse(DESK, "foxtest.desktop")
            listed = h.homes[AGENT] / where
            listed.parent.mkdir(parents=True, exist_ok=True)
            listed.write_text("[Default Applications]\n" + "".join(f"{handler}=chrometest.desktop\n"
                                                                     for handler in bi.HANDLERS), encoding="utf-8")
            before = h.tree(AGENT)
            outcome, said = h.inherit(created=False)
            check(outcome == "kept" and h.tree(AGENT) == before
                  and any("already names its own browser, chrometest.desktop" in s for s in said),
                  f"{where}: a reused account's choice there is kept, byte for byte: {outcome} {said}")
    raw, h = host()
    with raw:
        # A choice in a place no list above names still shows: the account answers otherwise than a bare home does.
        h.browse(DESK, "foxtest.desktop")
        # An installed browser that claims no link type, so the system never picks it; only this account's cache does.
        (h.share / "applications" / "othertest.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=othertest\nExec=/usr/bin/true %u\n", encoding="utf-8")
        cache = h.homes[AGENT] / ".local/share/applications/mimeinfo.cache"
        cache.parent.mkdir(parents=True)
        cache.write_text("[MIME Cache]\n" + "".join(f"{handler}=othertest.desktop;\n" for handler in bi.HANDLERS),
                         encoding="utf-8")
        mine = h.handlers(AGENT)
        bare = {h.xdg(DESK, "sh", "-c", 'd=$(mktemp -d); HOME=$d xdg-mime query default "$1"; rmdir $d', "sh",
                      handler) for handler in bi.HANDLERS}
        check(mine == {"othertest.desktop"} and "othertest.desktop" not in bare,
              f"fixture: the account answers otherwise than a bare home: {mine} {bare}")
        before = h.tree(AGENT)
        outcome, said = h.inherit(created=False)
        check(outcome == "kept" and h.tree(AGENT) == before and any("othertest.desktop" in s for s in said),
              f"a choice kept where no list names it is still a choice, kept: {outcome} {said}")


def test_a_refusal_after_an_earlier_write_puts_every_file_back() -> None:
    """Audit of 56ff2441: the KDE step refused after the generic one had changed every handler, and it said "keeps"."""
    if bi._kde_tools() is None:
        return
    for label, prepare in (("an existing account with settings", True), ("a fresh account", False)):
        raw, h = host()
        with raw:
            h.browse(DESK, "foxtest.desktop")
            if prepare:
                h.browse(AGENT, "chrometest.desktop")
                h.xdg(AGENT, "kwriteconfig6", "--file", "kdeglobals", "--group", "Other", "--key", "keep", "me")
            before_tree, before = h.tree(AGENT), h.handlers(AGENT)
            h.fail_when = lambda args: "XDG_CURRENT_DESKTOP=KDE" in args and "set" in args
            outcome, said = h.inherit(created=True)
            generic = [command for user, command in h.calls if user == AGENT and command[:2] == ["xdg-settings", "set"]]
            check(outcome == "failed" and len(generic) == 2,
                  f"{label}: the generic step ran and the KDE step was refused: {outcome} {generic}")
            check(h.tree(AGENT) == before_tree and h.handlers(AGENT) == before,
                  f"{label}: every file is back byte for byte, and the handlers are what they were: "
                  f"{h.handlers(AGENT)} {before}")
            check(any("could not be set to foxtest.desktop (this step refused here); its own settings are put back as "
                      "they were" in s for s in said), f"{label}: and it says exactly that: {said}")


def test_an_exception_after_an_earlier_write_takes_the_same_way_back() -> None:
    """Audit of 50dcbdc6: the KDE step raised after the generic one had changed every handler, which bypassed the
    restore, said "keeps", and left the snapshot behind."""
    if bi._kde_tools() is None:
        return
    raw, h = host()
    with raw:
        h.browse(DESK, "foxtest.desktop")
        h.browse(AGENT, "chrometest.desktop")
        before_tree, before = h.tree(AGENT), h.handlers(AGENT)
        h.raise_when = lambda args: "XDG_CURRENT_DESKTOP=KDE" in args and "set" in args
        outcome, said = h.inherit(created=True)
        generic = [command for user, command in h.calls if user == AGENT and command[:2] == ["xdg-settings", "set"]]
        check(outcome == "failed" and len(generic) == 2,
              f"the generic step ran, then launching the KDE step raised: {outcome} {generic}")
        check(h.tree(AGENT) == before_tree and h.handlers(AGENT) == before,
              f"every file is back byte for byte, and the handlers are what they were: {h.handlers(AGENT)} {before}")
        check(any("could not be set to foxtest.desktop (second launch failed); its own settings are put back as they "
                  "were, so it opens links as before (chrometest.desktop)" in s for s in said),
              f"and it says exactly that: {said}")
        check(len(h.snaps) == 1 and not Path(h.snaps[0]).exists(), f"the snapshot is gone: {h.snaps}")
    raw, h = host()
    with raw:
        # The restore itself raises: no claim it was put back, and the snapshot is still removed.
        h.browse(DESK, "foxtest.desktop")
        h.browse(AGENT, "chrometest.desktop")
        h.fail_when = lambda args: "XDG_CURRENT_DESKTOP=KDE" in args and "set" in args
        h.raise_when = lambda args: any('snap="$1"' in word for word in args)
        outcome, said = h.inherit(created=True)
        check(outcome == "failed" and any("its settings could not all be put back: it now opens" in s for s in said)
              and not any("put back as they were" in s for s in said),
              f"a restore that raises is never reported as done, and what holds is said: {said}")
        check(len(h.snaps) == 1 and not Path(h.snaps[0]).exists(), f"and the snapshot is removed anyway: {h.snaps}")


def test_a_refused_first_write_stops_there() -> None:
    if bi._kde_tools() is None:
        return
    raw, h = host()
    with raw:
        h.browse(DESK, "foxtest.desktop")
        h.browse(AGENT, "chrometest.desktop")
        before_tree = h.tree(AGENT)
        h.fail_when = lambda args: "set" in args and "XDG_CURRENT_DESKTOP=KDE" not in args and "xdg-settings" in args
        outcome, said = h.inherit(created=True)
        sets = [command for user, command in h.calls if user == AGENT and command[:2] == ["xdg-settings", "set"]]
        check(outcome == "failed" and len(sets) == 1 and h.tree(AGENT) == before_tree,
              f"the generic step refused: the KDE step is never run, and every file is as it was: {sets} {outcome}")


def entry(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"[Desktop Entry]\nType=Application\nName={name}\nExec=/usr/bin/true %u\n"
                    "MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;\n", encoding="utf-8")


def test_an_entry_must_be_one_the_agent_can_open_within_a_shared_installation() -> None:
    """Audit of eeccc1b9: root seeing a file, through any symlink, proved nothing about the agent."""
    for label, target, ok in (("a symlink within the data directory", "share/real/linked.desktop", True),
                              ("a symlink into a shared tree, as packages in /opt do", "opt/vendor/linked.desktop", True),
                              ("a symlink out of every shared installation", "elsewhere/linked.desktop", False)):
        raw, h = host()
        with raw:
            entry(h.tmp / target, "linked")
            (h.share / "applications" / "linked.desktop").symlink_to(h.tmp / target)
            h.browse(DESK, "linked.desktop")
            check(h.handlers(DESK) == {"linked.desktop"}, f"fixture: {label}: the desktop user resolves it")
            before = h.tree(AGENT)
            outcome, said = h.inherit()
            if ok:
                check(outcome == "set" and h.handlers(AGENT) == {"linked.desktop"}, f"{label}: used: {outcome} {said}")
            else:
                check(outcome == "uninstalled" and h.tree(AGENT) == before
                      and any("points outside the shared installation directories" in s for s in said)
                      and not [c for u, c in h.calls if u == AGENT and c[:2] == ["xdg-settings", "set"]],
                      f"{label}: refused before any write, untouched, and why: {outcome} {said}")
    for label, prepare, why in (
            ("an entry other accounts can change", lambda h: (h.share / "applications" / "foxtest.desktop").chmod(0o666),
             "it is not root's alone"),
            ("an entry in a directory other accounts can change",
             lambda h: (h.share / "applications").chmod(0o777), "applications is not root's alone (owner uid"),
            ("an entry not owned by the trusted account", lambda h: setattr(h, "trusted", 0), "is not root's alone")):
        raw, h = host()
        with raw:
            h.browse(DESK, "foxtest.desktop")
            prepare(h)
            before = h.tree(AGENT)
            outcome, said = h.inherit()
            check(outcome == "uninstalled" and h.tree(AGENT) == before and any(why in s for s in said),
                  f"{label}: refused, untouched, and why: {outcome} {said}")
            (h.share / "applications").chmod(0o755)
    raw, h = host()
    with raw:
        # Flatpak's shape: an export links, relatively, through symlinked directories (`current`, `active`) into the
        # app's own tree. Every part is the trusted account's and unwritable by others, so it is used.
        app = h.tmp / "opt/app/org.test.Fox"
        entry(app / "x86_64/stable/abc123/export/share/applications/org.test.Fox.desktop", "flatfox")
        (app / "current").symlink_to("x86_64/stable")
        (app / "x86_64/stable/active").symlink_to("abc123")
        (h.share / "applications" / "org.test.Fox.desktop").symlink_to(
            "../../opt/app/org.test.Fox/current/active/export/share/applications/org.test.Fox.desktop")
        h.browse(DESK, "org.test.Fox.desktop")
        check(h.handlers(DESK) == {"org.test.Fox.desktop"}, "fixture: the desktop user resolves the flatpak-shaped entry")
        outcome, said = h.inherit()
        check(outcome == "set" and h.handlers(AGENT) == {"org.test.Fox.desktop"},
              f"a trusted chain through symlinked directories, as flatpak exports are, is used: {outcome} {said}")
    # Audit of 86500977: every directory a link or its target sits in, up to the shared root, must be root's alone, or
    # another account can repoint the link after the check. Each case is refused before any write, naming the part.
    for label, build, mutable in (
            ("a trusted target, linked from a writable directory",
             lambda h: (entry(h.tmp / "opt/vendor/linked.desktop", "linked"),
                        (h.share / "applications" / "linked.desktop").symlink_to(h.tmp / "opt/vendor/linked.desktop"),
                        (h.share / "applications").chmod(0o777)), "share/applications"),
            ("a target below a writable directory, itself under the shared root",
             lambda h: (entry(h.tmp / "opt/vendor/sub/linked.desktop", "linked"),
                        (h.share / "applications" / "linked.desktop").symlink_to(h.tmp / "opt/vendor/sub/linked.desktop"),
                        (h.tmp / "opt/vendor").chmod(0o777)), "opt/vendor"),
            ("a link through an intermediate link in a writable directory",
             lambda h: (entry(h.tmp / "opt/vendor/linked.desktop", "linked"),
                        (h.tmp / "opt/hop").mkdir(parents=True),
                        (h.tmp / "opt/hop/link2.desktop").symlink_to(h.tmp / "opt/vendor/linked.desktop"),
                        (h.share / "applications" / "linked.desktop").symlink_to(h.tmp / "opt/hop/link2.desktop"),
                        (h.tmp / "opt/hop").chmod(0o777)), "opt/hop"),
            ("a link through a trusted symlinked directory whose resolved parent is writable",
             lambda h: (entry(h.tmp / "opt/mut/real/linked.desktop", "linked"),
                        (h.share / "dirlink").symlink_to(h.tmp / "opt/mut/real"),
                        (h.share / "applications" / "linked.desktop").symlink_to("../dirlink/linked.desktop"),
                        (h.tmp / "opt/mut").chmod(0o777)), "opt/mut")):
        raw, h = host()
        with raw:
            build(h)
            h.browse(DESK, "linked.desktop")
            check(h.handlers(DESK) == {"linked.desktop"}, f"fixture: {label}: the desktop user resolves it")
            before = h.tree(AGENT)
            outcome, said = h.inherit()
            check(outcome == "uninstalled" and h.tree(AGENT) == before
                  and not [c for u, c in h.calls if u == AGENT and c[:1] == ["mkdir"]]
                  and any(f"{mutable} is not root's alone" in s and "could change or repoint" in s for s in said),
                  f"{label}: refused before any write, naming {mutable}: {outcome} {said}")
            for directory in ("share/applications", "opt/vendor", "opt/hop", "opt/mut"):
                if (h.tmp / directory).is_dir():
                    (h.tmp / directory).chmod(0o755)
    raw, h = host()
    with raw:
        # The agent's own read is what decides: an entry it cannot read is refused, whoever else can.
        locked = h.share / "applications" / "foxtest.desktop"
        locked.chmod(0)
        try:
            problem = bi._usable_entry(AGENT, "foxtest.desktop", data_dirs=(h.share,), shared_roots=(h.share,),
                                       trusted_uid=h.trusted, runner=h.runner, home_for_user=h.home)
        finally:
            locked.chmod(0o644)
        check(problem == f"{AGENT} cannot read {locked}" and all(u == AGENT for u, _c in h.calls),
              f"an entry the agent cannot read, checked as the agent: {problem!r} {h.calls}")


def test_the_file_the_agent_reads_must_be_the_file_root_checked() -> None:
    """Defence in depth: the agent resolves the entry for its read, root walks the chain; if they ever disagree -- a
    different view of the filesystem -- the entry is refused. Only this seam can make them disagree."""
    raw, h = host()
    with raw:
        real = h.share / "applications" / "foxtest.desktop"
        elsewhere = h.share / "applications" / "chrometest.desktop"

        def agent_view(args, **kwargs):
            report = f"ok\t{real}\t{elsewhere}\t{os.getuid()} 644\t{os.getuid()} 755\n"
            return subprocess.CompletedProcess(args, 0, report, "")

        problem = bi._usable_entry(AGENT, "foxtest.desktop", data_dirs=(h.share,), shared_roots=(h.share,),
                                   trusted_uid=os.getuid(), runner=agent_view, home_for_user=h.home)
        check(problem.startswith(f"{real} resolved differently for {AGENT} ({elsewhere}) than for root ({real})"),
              f"refused, naming both: {problem!r}")


def test_headless_or_unconsented_projects_are_left_alone_and_say_so() -> None:
    for label, policy in (("headless", {"mode": "headless"}), ("no desktop user", {"mode": "wayland"}),
                          ("headless, naming a desktop user anyway", {"mode": "headless", "gui_user": DESK}),
                          ("no policy", None)):
        raw, h = host()
        with raw:
            h.browse(DESK, "foxtest.desktop")
            before = h.tree(AGENT)
            outcome, said = h.inherit(policy)
            check(outcome == "headless" and h.calls == [] and h.tree(AGENT) == before
                  and any("no approved desktop user" in s for s in said),
                  f"{label}: nothing asked, nothing written, and said: {outcome} {h.calls} {said}")


def test_an_unclear_or_unusable_browser_is_said_and_nothing_is_written() -> None:
    raw, h = host()
    with raw:
        h.browse(DESK, "foxtest.desktop")
        h.xdg(DESK, "xdg-mime", "default", "chrometest.desktop", "text/html")  # html disagrees with the links
        outcome, said = h.inherit()
        check(outcome == "unknown" and h.tree(AGENT) == {} and any("could not be told" in s and "html chrometest"
                                                                   in s for s in said),
              f"handlers that disagree: no guess, the account untouched, and why: {outcome} {said}")
    raw, h = host()
    with raw:
        effective = h.handlers(DESK)
        outcome, said = h.inherit()
        check(len(effective) == 1 and outcome == "set" and h.handlers(AGENT) == effective,
              f"a desktop user with no choice of its own: its effective default, the system's, is what is copied: "
              f"{effective} {outcome} {h.handlers(AGENT)}")
    raw, h = host()
    with raw:
        # A browser the desktop user installed for itself alone, in its own data home: it opens it; the agent cannot.
        local = h.homes[DESK] / ".local" / "share" / "applications"
        local.mkdir(parents=True)
        (local / "mine.desktop").write_text((h.share / "applications" / "foxtest.desktop").read_text()
                                            .replace("Name=foxtest", "Name=mine"), encoding="utf-8")
        h.browse(DESK, "mine.desktop")
        check(h.handlers(DESK) == {"mine.desktop"}, f"fixture: the desktop user's own browser: {h.handlers(DESK)}")
        outcome, said = h.inherit()
        check(outcome == "uninstalled" and h.tree(AGENT) == {} and any("is not installed where" in s for s in said),
              f"a browser the agent could not open: untouched, and said: {outcome} {said}")
    raw, h = host()
    with raw:
        h.browse(DESK, "foxtest.desktop")
        h.fail = "xdg-settings set"
        outcome, said = h.inherit()
        check(outcome == "failed" and any("could not be set to foxtest.desktop (xdg-settings set refused here)" in s
                                          for s in said),
              f"a refused write never stops provisioning, and is said: {outcome} {said}")
    raw, h = host()
    with raw:
        policy = {**WAYLAND, "gui_user": AGENT}
        outcome, said = h.inherit(policy)
        check(outcome == "kept" and h.calls == [], f"the owner is the desktop user: nothing to copy: {said}")


def test_only_switchyard_new_inherits() -> None:
    from scripts import new_project_phases

    fn = next(n for n in ast.parse(textwrap.dedent(inspect.getsource(new_project_phases))).body
              if isinstance(n, ast.FunctionDef) and n.name == "_prepare_new_project_accounts")
    calls = [node for node in ast.walk(fn) if isinstance(node, ast.Call)
             and ast.unparse(node.func) == "browser_inheritance.inherit_desktop_browser"]
    check(len(calls) == 1 and {kw.arg for kw in calls[0].keywords} >= {"owner_user", "created", "runner"}
          and ast.unparse(calls[0].args[0]) == "selected_desktop_policy",
          f"the account phase of `switchyard new` inherits, with its consented desktop policy: {len(calls)}")
    users = sorted(p.name for p in (ROOT / "scripts").rglob("*.py")
                   if "browser_inheritance" in p.read_text(encoding="utf-8", errors="replace")
                   and p.name != "browser_inheritance.py")
    check(users == ["new_project_phases.py"],
          f"and nothing else does: no upgrade, launch or resume path copies a browser: {users}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"browser_inheritance_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
