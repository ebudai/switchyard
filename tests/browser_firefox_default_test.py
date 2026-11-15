#!/usr/bin/env python3
"""SYRD-776: an agent account `switchyard new` creates opens links in Firefox when no desktop user's browser is copied.

The host here is SYRD-751's test host (real `xdg-mime`, `xdg-settings` and KDE config tool on throwaway homes; only the
account switch is stood in for), with this host's own shape added: `firefox.desktop` and `com.google.Chrome.desktop`
installed, and a desktop-entry cache that lists Chrome first for every link type, as `/usr/share/applications` does
here. An account with no choice of its own therefore opens Chrome -- which is what each case starts from.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import browser_inheritance as bi  # noqa: E402
from tests import browser_inheritance_test as base  # noqa: E402

CHECKS = 0
DESK, AGENT, WAYLAND = base.DESK, base.AGENT, base.WAYLAND
FIREFOX, CHROME = "firefox.desktop", "com.google.Chrome.desktop"
CHANGE = f"sudo -u {AGENT} -H xdg-settings set default-web-browser NAME.desktop"
DEFAULT_LINE = f"{AGENT} opens links with {FIREFOX}, the Firefox default, because nobody chose a browser for it."


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def host(*, firefox: bool = True):
    raw, h = base.host()
    apps = h.share / "applications"
    for name in (CHROME, FIREFOX) if firefox else (CHROME,):
        base.entry(apps / name, name)
    (apps / "mimeinfo.cache").write_text("[MIME Cache]\n" + "".join(
        f"{handler}={CHROME};{FIREFOX};foxtest.desktop;chrometest.desktop;\n" for handler in bi.HANDLERS),
        encoding="utf-8")
    return raw, h


def kde_browser(h) -> str | None:
    kde = bi._kde_tools()
    if kde is None:
        return None
    return h.xdg(AGENT, kde[1], "--file", "kdeglobals", "--group", "General", "--key", "BrowserApplication")


def sets(h, browser: str) -> list[list[str]]:
    return [c for u, c in h.calls if u == AGENT and c[:2] == ["xdg-settings", "set"] and browser in c]


def own_desktop_browser(h) -> None:
    """The desktop user's browser is one only that user can open: the agent cannot have it."""
    local = h.homes[DESK] / ".local" / "share" / "applications"
    base.entry(local / "mine.desktop", "mine")
    h.browse(DESK, "mine.desktop")


def refuse_chrometest(h) -> None:
    """The desktop user's choice is usable, but writing it is refused; writing Firefox is not."""
    h.browse(DESK, "chrometest.desktop")
    h.fail_when = lambda args: "xdg-settings" in args and "set" in args and "chrometest.desktop" in args


CASES = (
    ("headless", {"mode": "headless"}, lambda h: h.browse(DESK, "foxtest.desktop"),
     "the project has no approved desktop user to take it from. Trying the Firefox default instead."),
    ("headless", {"mode": "wayland"}, lambda h: None, "no approved desktop user"),
    ("unknown", WAYLAND,
     lambda h: (h.browse(DESK, "foxtest.desktop"), h.xdg(DESK, "xdg-mime", "default", "chrometest.desktop", "text/html")),
     "could not be told (https foxtest.desktop, http foxtest.desktop, html chrometest.desktop), so it is not copied"),
    ("uninstalled", WAYLAND, own_desktop_browser, "mine.desktop, cannot be syrd751-agent's: it is not installed where"),
    ("failed", WAYLAND, refuse_chrometest, "could not be set to chrometest.desktop (this step refused here); its own "
     "settings are put back as they were, so it opens links as before (com.google.Chrome.desktop). Trying the Firefox"),
)


def test_a_new_account_gets_firefox_whenever_no_desktop_browser_is_copied() -> None:
    for label, policy, prepare, why in CASES:
        raw, h = host()
        with raw:
            check(h.handlers(AGENT) == {CHROME}, f"fixture: {label}: a bare account opens Chrome, the cache's first")
            prepare(h)
            outcome, said = h.inherit(policy, created=True)
            check(outcome == "default" and h.handlers(AGENT) == {FIREFOX},
                  f"{label}: the new account's http, https and html handlers all name Firefox: {outcome} "
                  f"{h.handlers(AGENT)} {said}")
            named = kde_browser(h)
            check(named in (None, FIREFOX), f"{label}: and KDE's own BrowserApplication: {named!r}")
            check(any(why in s for s in said), f"{label}: why the desktop user's browser was not copied: {said}")
            check(said and said[-1] == f"switchyard: browser: {DEFAULT_LINE} Change it deliberately with `{CHANGE}`.",
                  f"{label}: the last line names {FIREFOX} and why -- nobody chose: {said}")
            check(all(u == AGENT for u, c in h.calls if c[:1] == ["mkdir"] or c[:2] == ["xdg-settings", "set"]),
                  f"{label}: every write is the agent's own")


def test_a_copied_desktop_choice_still_comes_first() -> None:
    for browser in ("chrometest.desktop", FIREFOX):
        raw, h = host()
        with raw:
            h.browse(DESK, browser)
            outcome, said = h.inherit(created=True)
            check(outcome == "set" and h.handlers(AGENT) == {browser},
                  f"the desktop user's {browser} is copied, not replaced by the default: {outcome} {said}")
            check(any(f"opens links with {browser}, {DESK}'s default browser, set once now" in s for s in said)
                  and not any("Firefox default" in s for s in said), f"and said as the desktop user's choice: {said}")
            if browser != FIREFOX:
                check(not sets(h, FIREFOX), f"Firefox is never written over a copied choice: {h.calls}")


def test_a_refused_firefox_copy_is_not_tried_twice() -> None:
    raw, h = host()
    with raw:
        h.browse(DESK, FIREFOX)
        h.fail = "xdg-settings set"
        outcome, said = h.inherit(created=True)
        check(outcome == "failed" and len(sets(h, FIREFOX)) == 1 and h.handlers(AGENT) == {CHROME},
              f"the desktop user's Firefox, refused once, is not written again as the default: {sets(h, FIREFOX)}")
        check(not any("Trying the Firefox default" in s for s in said) and any(f"`{CHANGE}`" in s for s in said),
              f"and no default is promised: {said}")


def test_an_existing_account_is_never_given_the_default() -> None:
    for label, policy, prepare, _why in CASES:
        raw, h = host()
        with raw:
            h.browse(AGENT, "chrometest.desktop")
            prepare(h)
            before = h.tree(AGENT)
            outcome, said = h.inherit(policy, created=False)
            check(outcome != "default" and h.tree(AGENT) == before and h.handlers(AGENT) == {"chrometest.desktop"},
                  f"{label}: a reused account's own choice is kept, byte for byte: {outcome} {said}")
            check(not sets(h, FIREFOX) and not any("Firefox default" in s for s in said),
                  f"{label}: and Firefox is never written or promised: {said}")
    for label, policy, prepare, _why in CASES[:4]:  # nothing of its own, and no desktop choice to copy
        raw, h = host()
        with raw:
            prepare(h)
            outcome, said = h.inherit(policy, created=False)
            check(outcome == label and h.tree(AGENT) == {} and h.handlers(AGENT) == {CHROME} and not sets(h, FIREFOX),
                  f"{label}: a reused account with no choice is not given one either: {outcome} {said}")


def test_without_a_usable_firefox_nothing_is_written_and_what_it_opens_is_said() -> None:
    for label, firefox, spoil, why in (
            ("not installed", False, lambda h: None, "it is not installed where syrd751-agent can open it"),
            ("changeable by other accounts", True, lambda h: (h.share / "applications" / FIREFOX).chmod(0o666),
             "it is not root's alone")):
        for policy_label, policy, prepare, _why in CASES[:4]:
            raw, h = host(firefox=firefox)
            with raw:
                prepare(h)
                spoil(h)
                outcome, said = h.inherit(policy, created=True)
                check(outcome == policy_label and h.tree(AGENT) == {} and h.handlers(AGENT) == {CHROME}
                      and not [c for u, c in h.calls if u == AGENT and c[:1] == ["mkdir"]],
                      f"{label}, {policy_label}: nothing written, the account still opens Chrome: {outcome} {said}")
                check(said[-1].startswith(f"switchyard: browser: {AGENT} keeps this host's default browser, {CHROME}: "
                                          f"nobody chose one for it, and the Firefox default, {FIREFOX}, cannot be "
                                          "its: ") and why in said[-1] and said[-1].endswith(f"`{CHANGE}`."),
                      f"{label}, {policy_label}: one line naming what it will open, why, and the command: {said[-1]}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"browser_firefox_default_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
