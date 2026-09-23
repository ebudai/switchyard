#!/usr/bin/env python3
"""SYRD-247: one image crosses, and nothing else does.

On `test19` neither provider could paste an image. Verified from the installed
binaries: Claude shells out to `xclip`/`wl-paste`, Codex links arboard and asks
for `ext-data-control` or `wlr-data-control`. GNOME's Mutter implements neither
and declines to, and the desktop policy deliberately removes `DISPLAY` and
`XAUTHORITY`, so both are out of transports. There is nothing to configure.

So the desktop owner hands over one image instead of anything being granted a
clipboard. These cases are the boundary that creates: who may run it, what may
cross, where it lands, who it is delivered to, and that it is cleaned up when
anything goes wrong.

Clipboard bytes appear nowhere in this file but as a few deliberate fake PNG
bytes; nothing here reads a real clipboard.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import image_handoff  # noqa: E402

CHECKS = 0

#: A believable PNG header and nothing else. Never a real clipboard.
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"switchyard test image" * 4

OWNER_UID = 1019
GUI_UID = 1000


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


@dataclass
class Role:
    role: str
    target: str
    workdir: str
    cli: list[str]


@dataclass
class Config:
    project: str
    roles: list[Role]


def tenant(tmp_path: Path, *, cli: str = "claude") -> Config:
    workdir = tmp_path / "worktrees" / "audit"
    workdir.mkdir(parents=True)
    return Config(
        project="test19",
        roles=[Role(role="audit", target="test19-audit:0.0", workdir=str(workdir), cli=[cli])],
    )


class FakeClipboard(image_handoff.Clipboard):
    """The owner's clipboard, without a compositor."""

    def __init__(self, types: list[str], payload: bytes = FAKE_PNG) -> None:
        self._types = types
        self._payload = payload
        self.reads: list[str] = []

    def types(self) -> list[str]:
        return list(self._types)

    def read(self, mime: str) -> bytes:
        self.reads.append(mime)
        return self._payload


def hand_over(config: Config, **kwargs):
    """Drive the real handoff with the seams a test can hold."""
    sent: list[tuple[str, str]] = []
    said: list[str] = []
    defaults = dict(
        role_name="audit",
        owner_uid=OWNER_UID,
        live_cli=lambda role: [role.cli[0]],
        sender=lambda target, message: sent.append((target, message)),
        clipboard=FakeClipboard(["image/png"]),
        geteuid=lambda: GUI_UID,
        now=lambda: 1_000.0,
        token=lambda: "deadbeef",
        print_func=said.append,
    )
    defaults.update(kwargs)
    result = image_handoff.hand_image_to_role(config, **defaults)
    return result, sent, said


# --- who may run it ---------------------------------------------------------


def test_a_role_cannot_take_an_image_only_the_owner_can_hand_one() -> None:
    """SYRD-98's actual objection: every role shares one UID.

    Run as the tenant this would be a clipboard read primitive for all of them.
    Refused by identity, so no configuration can turn it on.
    """
    with tempfile.TemporaryDirectory(prefix="syrd247-uid.") as tmp:
        config = tenant(Path(tmp))
        clipboard = FakeClipboard(["image/png"])
        try:
            hand_over(config, geteuid=lambda: OWNER_UID, clipboard=clipboard)
            raise AssertionError("the tenant was allowed to take an image")
        except image_handoff.ImageHandoffError as exc:
            check("run this as the desktop owner" in str(exc), f"and says why: {exc}")
        check(clipboard.reads == [], "and the clipboard was never read")


# --- what may cross ---------------------------------------------------------


def test_a_clipboard_with_no_image_is_a_refusal_not_a_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-notext.") as tmp:
        config = tenant(Path(tmp))
        try:
            hand_over(config, clipboard=FakeClipboard(["text/plain", "text/html"]))
            raise AssertionError("text was handed over as an image")
        except image_handoff.ImageHandoffError as exc:
            check("no image on the clipboard" in str(exc), f"{exc}")
            check("text/plain" in str(exc), f"and says what was there instead: {exc}")


def test_png_is_preferred_when_several_image_types_are_offered() -> None:
    check(image_handoff.chosen_image_type(["image/bmp", "image/png"]) == "image/png",
          "PNG wins when offered")
    check(image_handoff.chosen_image_type(["image/bmp"]) == "image/bmp",
          "and otherwise the image type that is there is used")


def test_an_image_over_the_cap_is_refused_and_nothing_is_written() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-big.") as tmp:
        tmp_path = Path(tmp)
        config = tenant(tmp_path)
        oversized = FakeClipboard(["image/png"], payload=b"x" * 2048)
        try:
            hand_over(config, clipboard=oversized, max_bytes=1024)
            raise AssertionError("an oversized image was handed over")
        except image_handoff.ImageHandoffError as exc:
            check("over the" in str(exc) and "limit" in str(exc), f"{exc}")
        directory, _ignore = image_handoff.spool_paths(config.roles[0])
        check(not directory.exists() or list(directory.glob("paste-*")) == [],
              "and no image was left in the working directory")


# --- where it lands ---------------------------------------------------------


def test_the_image_lands_in_the_roles_own_working_directory() -> None:
    """Codex issue #46391: an @-attachment silently becomes plain text when the
    session and process working directories differ. Under the role's own cwd
    that condition cannot arise, which is why the file goes here and not in a
    shared or owner-side spool."""
    with tempfile.TemporaryDirectory(prefix="syrd247-where.") as tmp:
        tmp_path = Path(tmp)
        config = tenant(tmp_path)
        _result, sent, _said = hand_over(config)
        role = config.roles[0]
        directory, ignore = image_handoff.spool_paths(role)
        written = sorted(directory.glob("paste-*"))

        check(len(written) == 1, f"exactly one image was written: {written}")
        spool = written[0]
        check(spool.parent.parent.parent == Path(role.workdir),
              f"inside the role's own working directory: {spool}")
        check(spool.read_bytes() == FAKE_PNG, "with the bytes that were copied")
        check(spool.stat().st_mode & 0o777 == 0o640,
              f"not world-readable: {spool.stat().st_mode & 0o777:o}")
        check(ignore.read_text(encoding="utf-8").strip() == "*",
              "and it can never become a commit")
        check(sent and str(spool) in sent[0][1],
              f"and the pane was told where it is: {sent}")


def test_the_grant_is_one_entry_on_one_file() -> None:
    granted: list[tuple[str, int, str]] = []
    with tempfile.TemporaryDirectory(prefix="syrd247-grant.") as tmp:
        config = tenant(Path(tmp))
        hand_over(config, grant=lambda path, uid, perms: granted.append((str(path), uid, perms)))

    check(len(granted) == 1, f"one grant, on one path: {granted}")
    path, uid, perms = granted[0]
    check(path.endswith(".png") and "paste-" in path, f"on the image itself: {path}")
    check(uid == OWNER_UID, f"for the tenant that must read it: {uid}")
    check(perms == "r", f"read only: {perms}")


# --- who it is delivered to -------------------------------------------------


def test_each_provider_is_told_in_its_own_documented_form() -> None:
    """Claude takes an image path in the composer; Codex takes an @ attachment."""
    with tempfile.TemporaryDirectory(prefix="syrd247-claude.") as tmp:
        config = tenant(Path(tmp), cli="claude")
        _result, sent, _said = hand_over(config)
    check(sent[0][1].startswith("Analyze this image: "), f"Claude: {sent[0][1]}")

    with tempfile.TemporaryDirectory(prefix="syrd247-codex.") as tmp:
        config = tenant(Path(tmp), cli="codex")
        _result, sent, _said = hand_over(config)
    check(sent[0][1].startswith("@"), f"Codex: {sent[0][1]}")
    check(sent[0][0] == "test19-audit:0.0", f"and both to the role's own pane: {sent[0][0]}")


def test_the_pane_must_actually_be_running_a_provider() -> None:
    """Liveness read from the pane, not claimed by configuration."""
    with tempfile.TemporaryDirectory(prefix="syrd247-dead.") as tmp:
        config = tenant(Path(tmp))
        clipboard = FakeClipboard(["image/png"])
        try:
            hand_over(config, live_cli=lambda _role: [], clipboard=clipboard)
            raise AssertionError("an image was handed to a pane with nothing in it")
        except image_handoff.ImageHandoffError as exc:
            check("not running a provider" in str(exc), f"{exc}")
        check(clipboard.reads == [], "and the clipboard was not read for a dead pane")


def test_a_provider_with_no_documented_image_path_is_refused_before_reading() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-other.") as tmp:
        config = tenant(Path(tmp), cli="hermes")
        clipboard = FakeClipboard(["image/png"])
        try:
            hand_over(config, clipboard=clipboard)
            raise AssertionError("an image was handed to a provider that cannot take one")
        except image_handoff.ImageHandoffError as exc:
            check("no documented way" in str(exc), f"{exc}")
            check("Nothing was read from your clipboard" in str(exc),
                  f"and says the clipboard was left alone: {exc}")
        check(clipboard.reads == [], "which it was")


def test_an_unknown_role_is_named_with_the_ones_that_exist() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-role.") as tmp:
        config = tenant(Path(tmp))
        try:
            hand_over(config, role_name="nope")
            raise AssertionError("an unknown role was accepted")
        except image_handoff.ImageHandoffError as exc:
            check("has no role 'nope'" in str(exc), f"{exc}")
            check("audit" in str(exc), f"and lists the ones it has: {exc}")


# --- cleanup ----------------------------------------------------------------


def test_a_delivery_that_fails_leaves_no_picture_behind() -> None:
    """Fails closed: the only durable trace is removed when the handoff does
    not land."""
    def refuse(_target, _message):
        raise RuntimeError("pane is gone")

    with tempfile.TemporaryDirectory(prefix="syrd247-fail.") as tmp:
        config = tenant(Path(tmp))
        try:
            hand_over(config, sender=refuse)
            raise AssertionError("a failed delivery was reported as success")
        except image_handoff.ImageHandoffError as exc:
            check("could not be told" in str(exc), f"{exc}")
        directory, _ignore = image_handoff.spool_paths(config.roles[0])
        check(list(directory.glob("paste-*")) == [],
              "and the image was removed from the working directory")


def test_an_unclaimed_image_is_swept_after_its_ttl() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-sweep.") as tmp:
        directory = Path(tmp) / "paste"
        directory.mkdir()
        fresh = directory / "paste-new.png"
        stale = directory / "paste-old.png"
        for entry in (fresh, stale):
            entry.write_bytes(FAKE_PNG)
        import os

        os.utime(stale, (0, 0))
        removed = image_handoff.sweep_spool(directory, now=image_handoff.SPOOL_TTL_SECONDS + 10)
        # Read inside the temporary directory: outside it, nothing exists and
        # the assertion would pass for the wrong reason.
        fresh_survived = fresh.exists()
        stale_gone = not stale.exists()

    check(removed == [stale], f"the stale one went: {removed}")
    check(stale_gone, "and is actually off disk")
    check(fresh_survived, "while the fresh one is left for the turn that will use it")


def test_the_operator_is_told_what_crossed_without_the_image_in_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-say.") as tmp:
        config = tenant(Path(tmp))
        _result, _sent, said = hand_over(config)

    check(len(said) == 1, f"one line: {said}")
    notice = said[0]
    check("image/png" in notice and "audit" in notice, f"naming what and to whom: {notice}")
    check("removed once used" in notice, f"and that it does not linger: {notice}")
    check("PNG" not in notice.replace("image/png", ""), "and no image bytes in it")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"image_handoff_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
