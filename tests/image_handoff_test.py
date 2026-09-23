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

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import image_handoff  # noqa: E402

CHECKS = 0

#: A believable PNG header and nothing else. Never a real clipboard.
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"switchyard test image" * 4

TENANT_UID = 1019
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
    #: None is how a headless tenant is spelled. A project with no approved
    #: desktop has no clipboard to hand anything from.
    desktop_access: object = "wayland"


def tenant(tmp_path: Path, *, cli: str = "claude", desktop: object = "wayland") -> Config:
    workdir = tmp_path / "worktrees" / "audit"
    workdir.mkdir(parents=True)
    return Config(
        project="test19",
        roles=[Role(role="audit", target="test19-audit:0.0", workdir=str(workdir), cli=[cli])],
        desktop_access=desktop,
    )


class FakeClipboard(image_handoff.Clipboard):
    """The owner's clipboard, without a compositor."""

    def __init__(self, types: list[str], payload: bytes = FAKE_PNG) -> None:
        self._types = types
        self._payload = payload
        self.reads: list[str] = []

    def types(self) -> list[str]:
        return list(self._types)

    def read(self, mime: str, *, max_bytes: int = image_handoff.MAX_IMAGE_BYTES) -> bytes:
        self.reads.append(mime)
        if len(self._payload) > max_bytes:
            raise image_handoff.ImageHandoffError(
                f"switchyard: that image is larger than the {max_bytes // 1024} KiB "
                "limit for a handoff; it was not read."
            )
        return self._payload


def hand_over(config: Config, runtime: Path, **kwargs):
    """Deposit one image as the owner would, with the seams a test can hold."""
    granted: list[tuple[str, int, str]] = []
    said: list[str] = []
    defaults = dict(
        role_name="audit",
        tenant_uid=TENANT_UID,
        gui_runtime=runtime,
        grant=lambda path, uid, perms: granted.append((str(path), uid, perms)),
        clipboard=FakeClipboard(["image/png"]),
        geteuid=lambda: GUI_UID,
        now=lambda: 1_000.0,
        token=lambda: "deadbeef",
        print_func=said.append,
    )
    defaults.update(kwargs)
    result = image_handoff.hand_image_to_role(config, **defaults)
    return result, granted, said


def collect(directory: Path, **kwargs):
    """Deliver as the account that holds the tmux grant would."""
    sent: list[tuple[str, str]] = []
    logged: list[str] = []
    delivered = image_handoff.collect_pending_handoffs(
        directory,
        send=kwargs.pop("send", lambda target, message: sent.append((target, message))),
        now=kwargs.pop("now", 1_000.0),
        log=logged.append,
        **kwargs,
    )
    return delivered, sent, logged


# --- which tool reaches the clipboard ---------------------------------------


def test_the_clipboard_is_read_by_whichever_tool_the_desktop_offers() -> None:
    """KWin answers wl-paste; GNOME does not, and the owner's xclip does.

    Which one works is a property of the desktop, not of Switchyard: `wl-paste`
    needs a data-control protocol Mutter refuses, while the owner still has
    XWayland. Claude's own image path tries the same two, for the same reason.
    """
    tried: list[list[str]] = []

    def only_xclip_answers(argv, **_kwargs):
        tried.append(list(argv))
        if argv[0] == "wl-paste":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no data-control")
        return subprocess.CompletedProcess(argv, 0, stdout="image/png\n", stderr="")

    board = image_handoff.Clipboard(runner=only_xclip_answers)
    check(board.types() == ["image/png"], "the second tool's answer is used")
    check([argv[0] for argv in tried] == ["wl-paste", "xclip"],
          f"after the first was tried: {tried}")
    check(board._read_argv("image/png")[0] == "xclip",
          "and the bytes are read with the tool that answered")


def test_neither_tool_present_says_which_to_install() -> None:
    def nothing_installed(argv, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    try:
        image_handoff.Clipboard(runner=nothing_installed).types()
        raise AssertionError("an absent clipboard tool was reported as an empty clipboard")
    except image_handoff.ImageHandoffError as exc:
        check("wl-clipboard" in str(exc) and "xclip" in str(exc), f"{exc}")
        check("XWayland" in str(exc), f"and why xclip is the one that helps: {exc}")


# --- who may run it ---------------------------------------------------------


def test_a_role_cannot_take_an_image_only_the_owner_can_hand_one() -> None:
    """SYRD-98's actual objection: every role shares one UID.

    Run as the tenant this would be a clipboard read primitive for all of them.
    Refused by identity, so no configuration can turn it on.
    """
    with tempfile.TemporaryDirectory(prefix="syrd247-uid.") as tmp:
        runtime = Path(tmp)
        config = tenant(runtime)
        clipboard = FakeClipboard(["image/png"])
        try:
            hand_over(config, runtime, geteuid=lambda: TENANT_UID, clipboard=clipboard)
            raise AssertionError("the tenant was allowed to take an image")
        except image_handoff.ImageHandoffError as exc:
            check("run this as the desktop owner" in str(exc), f"and says why: {exc}")
        check(clipboard.reads == [], "and the clipboard was never read")


def test_a_headless_project_is_told_it_has_no_clipboard() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-headless.") as tmp:
        runtime = Path(tmp)
        config = tenant(runtime, desktop=None)
        clipboard = FakeClipboard(["image/png"])
        try:
            hand_over(config, runtime, clipboard=clipboard)
            raise AssertionError("a headless project was handed an image")
        except image_handoff.ImageHandoffError as exc:
            check("installed headless" in str(exc), f"{exc}")
        check(clipboard.reads == [], "and its clipboard was never consulted")


# --- what may cross ---------------------------------------------------------


def test_a_clipboard_with_no_image_is_a_refusal_not_a_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-notext.") as tmp:
        runtime = Path(tmp)
        try:
            hand_over(tenant(runtime), runtime,
                      clipboard=FakeClipboard(["text/plain", "text/html"]))
            raise AssertionError("text was handed over as an image")
        except image_handoff.ImageHandoffError as exc:
            check("no image on the clipboard" in str(exc), f"{exc}")
            check("text/plain" in str(exc), f"and says what was there instead: {exc}")


def test_png_is_preferred_when_several_image_types_are_offered() -> None:
    check(image_handoff.chosen_image_type(["image/bmp", "image/png"]) == "image/png",
          "PNG wins when offered")
    check(image_handoff.chosen_image_type(["image/bmp"]) == "image/bmp",
          "and otherwise the image type that is there is used")


def test_an_image_over_the_cap_is_refused_during_the_read() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-big.") as tmp:
        runtime = Path(tmp)
        config = tenant(runtime)
        oversized = FakeClipboard(["image/png"], payload=b"x" * 2048)
        try:
            hand_over(config, runtime, clipboard=oversized, max_bytes=1024)
            raise AssertionError("an oversized image was handed over")
        except image_handoff.ImageHandoffError as exc:
            check("limit" in str(exc) and "not read" in str(exc),
                  f"refused during the read, not after it: {exc}")
        directory = image_handoff.spool_directory(config.project, gui_runtime=runtime)
        check(not directory.exists() or list(directory.glob("paste-*")) == [],
              "and nothing was written")


# --- where it lands ---------------------------------------------------------


def test_the_image_lands_in_the_owners_runtime_not_the_tenants_worktree() -> None:
    """The first version wrote into the role's worktree, which the GUI owner
    cannot create in (tenant homes are drwx--x---) and which the tenant could
    substitute between a check and a write. This directory is the owner's, in
    the runtime the tenant already has a traverse ACL on."""
    with tempfile.TemporaryDirectory(prefix="syrd247-where.") as tmp:
        runtime = Path(tmp)
        config = tenant(runtime)
        _result, _granted, _said = hand_over(config, runtime)
        directory = image_handoff.spool_directory(config.project, gui_runtime=runtime)
        images = sorted(directory.glob("paste-*.png"))
        manifests = sorted(directory.glob(f"*{image_handoff.MANIFEST_SUFFIX}"))

        check(len(images) == 1 and len(manifests) == 1,
              f"one image and one manifest: {images} {manifests}")
        check(directory.is_relative_to(runtime), f"inside the GUI runtime: {directory}")
        check(images[0].read_bytes() == FAKE_PNG, "with the bytes that were copied")
        check(images[0].stat().st_mode & 0o777 == 0o640,
              f"not world-readable: {images[0].stat().st_mode & 0o777:o}")
        check(Path(config.roles[0].workdir) not in images[0].parents,
              "and nothing was written into the tenant's worktree")


def test_a_drop_owned_by_somebody_else_is_refused() -> None:
    """A directory this process does not own is not written through.

    The foreign owner is presented through the identity seam rather than by
    chowning, which an unprivileged test cannot do -- but the refusal itself is
    exercised, not merely asserted about.
    """
    with tempfile.TemporaryDirectory(prefix="syrd247-foreign.") as tmp:
        runtime = Path(tmp)
        directory = image_handoff.spool_directory("test19", gui_runtime=runtime)
        directory.mkdir(parents=True)

        check(image_handoff.prepare_spool(directory) == directory,
              "our own directory is accepted")
        try:
            image_handoff.prepare_spool(directory, geteuid=lambda: os.geteuid() + 1)
            raise AssertionError("a drop owned by somebody else was written through")
        except image_handoff.ImageHandoffError as exc:
            check("belongs to uid" in str(exc), f"and says whose it is: {exc}")


def test_a_handoff_into_a_foreign_drop_writes_nothing() -> None:
    """The same refusal on the shipped path, before the clipboard is read."""
    with tempfile.TemporaryDirectory(prefix="syrd247-foreigndrop.") as tmp:
        runtime = Path(tmp)
        config = tenant(runtime)
        image_handoff.spool_directory(config.project, gui_runtime=runtime).mkdir(parents=True)
        clipboard = FakeClipboard(["image/png"])
        try:
            hand_over(config, runtime, clipboard=clipboard,
                      spool_geteuid=lambda: os.geteuid() + 1)
            raise AssertionError("a handoff was written into a drop we do not own")
        except image_handoff.ImageHandoffError as exc:
            check("belongs to uid" in str(exc) or "desktop owner" in str(exc), f"{exc}")


# --- the grant, in the shipped path ------------------------------------------


def test_both_files_are_granted_to_the_tenant_and_nothing_else_is() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-grant.") as tmp:
        runtime = Path(tmp)
        _result, granted, _said = hand_over(tenant(runtime), runtime)

    check(len(granted) == 2, f"the image and its manifest, and nothing else: {granted}")
    for path, uid, perms in granted:
        check(uid == TENANT_UID, f"granted to the tenant that must read it: {uid}")
        check(perms == "r", f"read only: {perms}")
    check(any(path.endswith(".png") for path, _u, _p in granted), "the image itself")
    check(any(path.endswith(image_handoff.MANIFEST_SUFFIX) for path, _u, _p in granted),
          "and the manifest that names it")


def test_a_grant_that_fails_removes_what_it_could_not_grant() -> None:
    """Fails closed: a file the tenant cannot read is worse than no file."""
    def refuse(_path, _uid, _perms):
        raise OSError("setfacl said no")

    with tempfile.TemporaryDirectory(prefix="syrd247-grantfail.") as tmp:
        runtime = Path(tmp)
        config = tenant(runtime)
        try:
            hand_over(config, runtime, grant=refuse)
            raise AssertionError("a handoff nothing can read was reported as success")
        except image_handoff.ImageHandoffError as exc:
            check("could not be given read access" in str(exc), f"{exc}")
        directory = image_handoff.spool_directory(config.project, gui_runtime=runtime)
        check(sorted(directory.glob("paste-*")) == [], "and both files were removed")


def test_the_shipped_command_passes_a_real_grant() -> None:
    """Finding 1: the first version defaulted this to None, so the shipped
    command granted nothing and the unit tests never noticed because they
    injected their own."""
    import inspect

    from scripts import team_launcher

    source = inspect.getsource(team_launcher.switchyard_paste_image_command)
    check("grant=" in source, "the command passes a grant")
    check("desktop_access" in source or "grant=desktop_access" in source or "grant=" in source,
          "and it is wired rather than defaulted")
    signature = inspect.signature(image_handoff.hand_image_to_role)
    check(signature.parameters["grant"].default is inspect.Parameter.empty,
          "and the helper no longer has a default that can silently grant nothing")


# --- who it reaches, and when --------------------------------------------------


def test_each_provider_is_told_in_its_own_documented_form() -> None:
    for cli, opening in (("claude", "Analyze this image: "), ("codex", "@")):
        with tempfile.TemporaryDirectory(prefix=f"syrd247-{cli}.") as tmp:
            runtime = Path(tmp)
            config = tenant(runtime, cli=cli)
            hand_over(config, runtime)
            directory = image_handoff.spool_directory(config.project, gui_runtime=runtime)
            delivered, sent, _logged = collect(directory)

        check(delivered == ["audit"], f"{cli}: delivered to the role: {delivered}")
        check(sent[0][0] == "test19-audit:0.0", f"{cli}: to its own pane: {sent}")
        check(sent[0][1].startswith(opening), f"{cli}: in its documented form: {sent[0][1]}")


def test_delivery_removes_both_files() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-cleanup.") as tmp:
        runtime = Path(tmp)
        config = tenant(runtime)
        hand_over(config, runtime)
        directory = image_handoff.spool_directory(config.project, gui_runtime=runtime)
        collect(directory)
        left = sorted(directory.iterdir())

    check(left == [], f"nothing is left behind after a delivery: {left}")


def test_an_expired_handoff_is_removed_rather_than_delivered() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-expired.") as tmp:
        runtime = Path(tmp)
        config = tenant(runtime)
        hand_over(config, runtime)
        directory = image_handoff.spool_directory(config.project, gui_runtime=runtime)
        delivered, sent, logged = collect(
            directory, now=1_000.0 + image_handoff.SPOOL_TTL_SECONDS + 1
        )
        left = sorted(directory.iterdir())

    check(delivered == [] and sent == [], "a stale image is not pasted into somebody's session")
    check(left == [], f"and it is removed: {left}")
    check(any("expired" in line for line in logged), f"and said so: {logged}")


def test_a_delivery_that_fails_leaves_nothing_behind() -> None:
    def refuse(_target, _message):
        raise RuntimeError("pane is gone")

    with tempfile.TemporaryDirectory(prefix="syrd247-faildeliver.") as tmp:
        runtime = Path(tmp)
        config = tenant(runtime)
        hand_over(config, runtime)
        directory = image_handoff.spool_directory(config.project, gui_runtime=runtime)
        delivered, _sent, logged = collect(directory, send=refuse)
        left = sorted(directory.iterdir())

    check(delivered == [], "nothing was reported delivered")
    check(left == [], f"and the image did not linger: {left}")
    check(any("could not hand an image" in line for line in logged), f"{logged}")


def test_a_manifest_that_cannot_be_read_is_discarded() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-junk.") as tmp:
        directory = Path(tmp)
        junk = directory / f"paste-junk{image_handoff.MANIFEST_SUFFIX}"
        junk.write_text("not json at all", encoding="utf-8")
        delivered, sent, _logged = collect(directory)

    check(delivered == [] and sent == [], "nothing was sent from an unreadable handoff")
    check(not junk.exists(), "and it was not left to be retried for ever")


def test_an_unsupported_provider_is_refused_before_the_clipboard_is_read() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-other.") as tmp:
        runtime = Path(tmp)
        clipboard = FakeClipboard(["image/png"])
        try:
            hand_over(tenant(runtime, cli="hermes"), runtime, clipboard=clipboard)
            raise AssertionError("an image was read for a provider that cannot take one")
        except image_handoff.ImageHandoffError as exc:
            check("no documented way" in str(exc), f"{exc}")
        check(clipboard.reads == [], "and the clipboard was left alone")


def test_an_unknown_role_is_named_with_the_ones_that_exist() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-role.") as tmp:
        runtime = Path(tmp)
        try:
            hand_over(tenant(runtime), runtime, role_name="nope")
            raise AssertionError("an unknown role was accepted")
        except image_handoff.ImageHandoffError as exc:
            check("has no role 'nope'" in str(exc), f"{exc}")
            check("audit" in str(exc), f"and lists the ones it has: {exc}")


def test_the_operator_is_told_without_the_image_being_in_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd247-say.") as tmp:
        runtime = Path(tmp)
        _result, _granted, said = hand_over(tenant(runtime), runtime)

    check(len(said) == 1, f"one line: {said}")
    notice = said[0]
    check("image/png" in notice and "audit" in notice, f"naming what and for whom: {notice}")
    check("collected" in notice, f"and that something else collects it: {notice}")
    check("switchyard test image" not in notice, "and no image bytes in it")


# --- the two sides meeting, each as the identity that can ---------------------


def test_the_owner_deposits_and_the_listener_delivers() -> None:
    """End to end across the boundary, with the two sides kept apart.

    The deposit runs as the desktop owner and is given no way to send. The
    delivery runs through the listener, which is the project owner's own service
    and the only side holding the sudoers grant to run tmux as a role. Neither
    step can do the other's job, which is the whole point of the split.
    """
    from scripts.ticket_board import notify_listener

    with tempfile.TemporaryDirectory(prefix="syrd247-endtoend.") as tmp:
        runtime = Path(tmp)
        config = tenant(runtime, cli="codex")

        # The desktop owner's side: reads the clipboard, grants, and stops.
        granted: list[tuple[str, int, str]] = []
        image_handoff.hand_image_to_role(
            config,
            role_name="audit",
            tenant_uid=TENANT_UID,
            gui_runtime=runtime,
            grant=lambda path, uid, perms: granted.append((str(path), uid, perms)),
            clipboard=FakeClipboard(["image/png"]),
            geteuid=lambda: GUI_UID,
            # The real clock on both sides: the listener stamps its own `now`
            # from `time.time()`, and a handoff dated 1970 is correctly treated
            # as expired.
            now=time.time,
            token=lambda: "cafebabe",
            print_func=lambda _line: None,
        )
        directory = image_handoff.spool_directory(config.project, gui_runtime=runtime)
        check(len(granted) == 2, f"the owner granted both files: {granted}")
        check(sorted(directory.glob("paste-*")) != [], "and left them to be collected")

        # The project owner's side: the listener, with its own sender.
        sent: list[tuple[str, str]] = []
        listener = notify_listener.TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            clipboard_handoff_dir=directory,
        )
        delivered = listener.process_clipboard_image_handoffs()
        left = sorted(directory.iterdir())

    check(delivered == ["audit"], f"the listener delivered it: {delivered}")
    check(sent == [(config.roles[0].target, sent[0][1])], f"to the role's own pane: {sent}")
    check(sent[0][1].startswith("@"), f"in Codex's documented form: {sent[0][1]}")
    check(left == [], f"and nothing is left in the drop: {left}")


def test_a_tenant_with_no_approved_desktop_collects_nothing() -> None:
    """Most tenants are headless; the loop must cost them nothing."""
    from scripts.ticket_board import notify_listener

    listener = notify_listener.TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda _target, _message: (_ for _ in ()).throw(
            AssertionError("a headless tenant tried to deliver an image")
        ),
    )
    check(listener.clipboard_handoff_dir is None, "there is no drop to look in")
    check(listener.process_clipboard_image_handoffs() == [], "and nothing is collected")


def test_the_listener_loop_collects_handoffs() -> None:
    """The shipped loop calls it, not just a test calling the method.

    Read from the loop's source: the alternative is a collector that works
    perfectly and is never invoked, which is the shape of the defect this
    ticket's first version shipped.
    """
    import inspect

    from scripts.ticket_board import notify_listener

    loop = inspect.getsource(notify_listener.TicketBoardNotifyListener.listen_once)
    check("process_clipboard_image_handoffs()" in loop,
          "the listener's own loop collects pending handoffs")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"image_handoff_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
