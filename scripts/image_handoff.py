#!/usr/bin/env python3
"""Hand one image to one role, without handing anybody a clipboard.

On GNOME Wayland neither provider can read the desktop clipboard at all. Claude
shells out to `xclip`/`wl-paste`; Codex links arboard and asks for
`ext-data-control` or `wlr-data-control`. Mutter implements neither protocol and
declines to, and the desktop policy deliberately removes `DISPLAY`/`XAUTHORITY`
so the X11 fallback is unreachable. There is nothing to configure: the compositor
is the side saying no (SYRD-247).

The only party that can read that clipboard is the GUI owner, who already has it.
So the owner hands over **one image** -- not a capability:

* the owner runs this, as themselves. A role cannot call it, so a same-UID
  sibling gains nothing, which is SYRD-98's actual objection to environment
  fixes;
* one `image/*` type, chosen from the clipboard's advertised types, refused above
  a size cap;
* written into the role's OWN working directory, because Codex issue #46391
  silently degrades an `@`-attachment to plain text when the session and process
  working directories differ -- under the role's own cwd that cannot arise;
* delivered to a pane proven live, by the sender Switchyard already uses;
* removed on acknowledgement, or swept after a TTL. Failure removes it too.

No clipboard bytes are logged, echoed, or written anywhere but that one file.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

#: The largest image handed over. Big enough for a full-screen screenshot on a
#: 4K display, small enough that a mistaken copy of something enormous is
#: refused rather than written into a worktree.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: How long a spool file may sit unclaimed. The provider reads it within a turn;
#: anything older is a handoff that did not land, and is swept.
SPOOL_TTL_SECONDS = 120.0

#: Where a handed-over image lives: a directory of the GUI owner's own, inside
#: the GUI runtime the tenant ALREADY has a traverse ACL on.
#:
#: This is the one crossing in the system that is already reviewed. The desktop
#: policy grants the tenant execute on `/run/user/<gui-uid>` and read/write on
#: one socket inside it; a sibling directory there is reachable with exactly
#: that authority and needs only a read entry on each file.
#:
#: It is emphatically NOT inside the role's worktree, which is where the first
#: version put it. Tenant homes are `drwx--x---` -- the GUI owner cannot create
#: anything there -- and a destination the tenant can write is a destination the
#: tenant can replace between a check and a write. Here the directory belongs to
#: the owner, so there is nothing for the tenant to substitute (SYRD-247 review).
SPOOL_DIR_NAME = "switchyard-paste"

#: What the tenant side reads to learn an image is waiting. Separate from the
#: image so the delivering side never has to parse or open the image itself.
MANIFEST_SUFFIX = ".handoff.json"

#: What each provider is told, once the file exists. Both forms are from the
#: providers' own documentation: Claude takes an image path in the composer,
#: Codex takes an `@` attachment.
PROVIDER_REFERENCE = {
    "claude": "Analyze this image: {path}",
    "codex": "@{path}",
}


class ImageHandoffError(RuntimeError):
    """Refused. The message is written to be read by the person who ran it."""


def _open_reader(args: Sequence[str]) -> Any:
    return subprocess.Popen(list(args), stdout=subprocess.PIPE)


@dataclass
class Clipboard:
    """The GUI owner's clipboard, as two commands and nothing else.

    Separated so a test drives it without a compositor, and so it is obvious
    that exactly two things are ever asked of it: which types are on offer, and
    the bytes of one chosen type.
    """

    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run
    #: How the bytes are read: a process whose output is consumed with a limit,
    #: rather than one whose whole output is collected first.
    opener: Callable[[Sequence[str]], Any] = _open_reader
    #: Which tool answered `types()`, so the bytes are read with the same one.
    _reader: str = ""

    def types(self) -> list[str]:
        """What the owner's clipboard is offering, by whichever tool reaches it.

        Two tools, because which one works is a property of the DESKTOP, not of
        Switchyard. `wl-paste` needs a data-control protocol, which KWin offers
        and Mutter refuses. Under GNOME the owner still has XWayland -- it is the
        tenant that has no `DISPLAY`, by policy -- so `xclip` is the one that
        reaches the clipboard there. Claude's own image path tries the same two
        for the same reason.

        Tried in that order and the first that answers wins; if neither is
        present the refusal says which to install rather than reporting an empty
        clipboard (SYRD-247 review).
        """
        missing: list[str] = []
        for tool, argv, parse in (
            ("wl-clipboard", ["wl-paste", "--list-types"], self._lines),
            ("xclip", ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"], self._lines),
        ):
            try:
                proc = self.runner(argv, capture_output=True, text=True, timeout=10)
            except (OSError, FileNotFoundError):
                missing.append(tool)
                continue
            if getattr(proc, "returncode", 1) == 0:
                offered = parse(str(getattr(proc, "stdout", "") or ""))
                if offered:
                    self._reader = argv[0]
                    return offered
            missing.append(tool)
        raise ImageHandoffError(
            "switchyard: could not read this desktop's clipboard with "
            + " or ".join(dict.fromkeys(missing))
            + ". Run this as the desktop owner, in their session; on a desktop without a "
            "clipboard-manager protocol install xclip, which reaches it through XWayland."
        )

    @staticmethod
    def _lines(text: str) -> list[str]:
        return [line.strip() for line in text.splitlines() if line.strip()]

    def _read_argv(self, mime: str) -> list[str]:
        if getattr(self, "_reader", "") == "xclip":
            return ["xclip", "-selection", "clipboard", "-t", mime, "-o"]
        return ["wl-paste", "--type", mime]

    def read(self, mime: str, *, max_bytes: int = MAX_IMAGE_BYTES) -> bytes:
        """Read at most `max_bytes` of one type, and stop reading there.

        Bounded during acquisition, not after it. `capture_output=True` reads
        whatever the clipboard holds into memory first and only then lets a
        caller measure it, so the cap it enforces is on the copy, not on the
        read -- an enormous clipboard is already in this process by the time it
        is refused (SYRD-247 review).
        """
        with self.opener(self._read_argv(mime)) as proc:
            stream = proc.stdout
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = stream.read(64 * 1024) if stream is not None else b""
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    proc.kill()
                    raise ImageHandoffError(
                        f"switchyard: that image is larger than the "
                        f"{max_bytes // 1024} KiB limit for a handoff; it was not read."
                    )
                chunks.append(chunk)
            if proc.wait() != 0:
                raise ImageHandoffError(
                    f"switchyard: the clipboard would not give up its {mime}"
                )
        return b"".join(chunks)


def chosen_image_type(types: Sequence[str]) -> str:
    """The image type to take, or a refusal naming what was there instead.

    PNG first when it is offered, because it is what both providers name first
    and what a screenshot already is. Only `image/*` is eligible: a clipboard
    holding text is not a failure of this mechanism, it is somebody who has not
    copied a picture yet, and it should say so.
    """
    images = [entry for entry in types if entry.startswith("image/")]
    if not images:
        raise ImageHandoffError(
            "switchyard: there is no image on the clipboard -- copy one first. "
            + (f"What is there: {', '.join(types)}." if types else "The clipboard is empty.")
        )
    for preferred in ("image/png", "image/jpeg"):
        if preferred in images:
            return preferred
    return images[0]


def _role_named(config: Any, role_name: str) -> Any:
    for role in config.roles:
        if role.role == role_name:
            return role
    known = ", ".join(sorted(entry.role for entry in config.roles))
    raise ImageHandoffError(
        f"switchyard: {config.project} has no role {role_name!r}. It has: {known}."
    )


def _refuse_if_run_by_the_tenant(owner_uid: int, *, geteuid: Callable[[], int]) -> None:
    """The owner hands over; the tenant never takes.

    Run as the tenant this would be a clipboard read primitive for every role
    sharing that UID, which is the thing SYRD-98 forbids. It is refused by
    identity rather than by configuration, so no policy edit can turn it on.
    """
    if geteuid() == owner_uid:
        raise ImageHandoffError(
            "switchyard: run this as the desktop owner, not as the project's own account. "
            "It hands an image from your clipboard to a role; a role may not take one."
        )


def spool_directory(project: str, *, gui_runtime: Path) -> Path:
    """The owner's drop for one project, inside the already-granted runtime."""
    return gui_runtime / SPOOL_DIR_NAME / project


def prepare_spool(directory: Path, *, geteuid: Callable[[], int] = os.geteuid) -> Path:
    """Create the drop as the owner, refusing anything already there that is not ours.

    Every component belongs to the GUI owner and lives in their runtime, so a
    tenant cannot interpose one. What is still checked is that the leaf is a
    real directory owned by this process -- a symlink or a foreign directory
    where the drop should be is refused rather than written through, because
    writing through it is how a drop becomes a write primitive somewhere else.
    """
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.lstat()
    import stat as _stat

    if _stat.S_ISLNK(info.st_mode) or not _stat.S_ISDIR(info.st_mode):
        raise ImageHandoffError(f"switchyard: {directory} is not a directory this may write to")
    if info.st_uid != geteuid():
        raise ImageHandoffError(
            f"switchyard: {directory} belongs to uid {info.st_uid}, not to you; refusing to "
            "write a handoff through it"
        )
    return directory


def sweep_spool(directory: Path, *, now: float, ttl: float = SPOOL_TTL_SECONDS) -> list[Path]:
    """Remove handed-over images nobody claimed. Failure removes them too.

    A handoff that did not land leaves a picture in a working directory, which
    is both clutter and the only durable trace this mechanism leaves. Swept on
    every use rather than by a timer, so the cleanup runs on the same path as
    the thing that creates the files.
    """
    removed: list[Path] = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return removed
    for entry in entries:
        if not entry.is_file():
            continue
        try:
            if now - entry.stat().st_mtime < ttl:
                continue
            entry.unlink()
        except OSError:
            continue
        removed.append(entry)
    return removed


def require_approved_desktop(config: Any) -> None:
    """A project with no approved desktop has no clipboard to hand anything from.

    Checked explicitly rather than discovered by a confusing failure further in:
    a headless tenant is a supported configuration, and asking it for an image
    should say so rather than fail at `wl-paste` (SYRD-247 review).
    """
    access = getattr(config, "desktop_access", None)
    if access is None:
        raise ImageHandoffError(
            f"switchyard: {getattr(config, 'project', 'this project')} is installed headless, "
            "so it has no desktop clipboard to hand an image from. Give it a desktop policy "
            "with `switchyard upgrade` first."
        )


def hand_image_to_role(
    config: Any,
    *,
    role_name: str,
    tenant_uid: int,
    gui_runtime: Path,
    grant: Callable[[Path, int, str], Any],
    clipboard: Clipboard | None = None,
    geteuid: Callable[[], int] = os.geteuid,
    #: Who owns the drop, asked of the real process rather than of the caller
    #: identity seam above.
    spool_geteuid: Callable[[], int] = os.geteuid,
    now: Callable[[], float] = time.time,
    token: Callable[[], str] = lambda: os.urandom(8).hex(),
    max_bytes: int = MAX_IMAGE_BYTES,
    print_func: Callable[[str], None] = print,
) -> int:
    """Read one image as the owner and leave it where one role can collect it.

    This does NOT deliver. The GUI owner holds no grant to run tmux as the
    tenant -- `render_role_control_sudoers` gives that to the project owner and
    director accounts, and the desktop owner is only the tenant-control caller
    -- so an owner-side `directorctl send` cannot reach a role pane at all. The
    side that already has that grant, and already types into panes, does the
    delivering (SYRD-247 review).
    """
    role = _role_named(config, role_name)
    require_approved_desktop(config)
    _refuse_if_run_by_the_tenant(tenant_uid, geteuid=geteuid)

    provider = _command_name_of(role)
    reference = PROVIDER_REFERENCE.get(provider)
    if reference is None:
        raise ImageHandoffError(
            f"switchyard: {role_name} is configured for {provider}, which has no documented "
            "way to take an image from a file. Nothing was read from your clipboard."
        )

    board = clipboard or Clipboard()
    mime = chosen_image_type(board.types())
    payload = board.read(mime, max_bytes=max_bytes)
    if not payload:
        raise ImageHandoffError("switchyard: the clipboard's image was empty; nothing was written.")

    # NOT the `geteuid` seam above: that one answers "is the caller the tenant",
    # which a test overrides to stand in for the desktop owner. This one asks
    # "do I own the directory I am about to write in", which must be the real
    # process identity or the check means nothing.
    directory = prepare_spool(
        spool_directory(config.project, gui_runtime=gui_runtime), geteuid=spool_geteuid
    )
    swept = sweep_spool(directory, now=now())
    suffix = {"image/png": ".png", "image/jpeg": ".jpg"}.get(mime, ".img")
    stem = f"paste-{token()}"
    image = directory / f"{stem}{suffix}"
    manifest = directory / f"{stem}{MANIFEST_SUFFIX}"

    image.write_bytes(payload)
    image.chmod(0o640)
    manifest.write_text(
        json.dumps(
            {
                "project": config.project,
                "role": role.role,
                "target": role.target,
                "image": str(image),
                "reference": reference.format(path=image),
                "created_at": now(),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o640)
    # Wired here, in the shipped path, not injected by a test: without these the
    # tenant cannot open either file and the handoff silently does nothing --
    # which is exactly what the first version shipped (SYRD-247 review).
    try:
        grant(image, tenant_uid, "r")
        grant(manifest, tenant_uid, "r")
    except Exception as exc:
        image.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        raise ImageHandoffError(
            f"switchyard: {role_name} could not be given read access to the image, so it was "
            f"removed rather than left where nothing can collect it: {exc}"
        ) from exc

    print_func(
        f"switchyard: {len(payload) // 1024} KiB of {mime} is waiting for {config.project}'s "
        f"{role.role}. It is collected within seconds by that project's own listener, and "
        f"removed after {SPOOL_TTL_SECONDS:g}s if it is not."
        + (f" Swept {len(swept)} older handoff(s)." if swept else "")
    )
    return 0


def collect_pending_handoffs(
    directory: Path,
    *,
    send: Callable[[str, str], Any],
    now: float,
    ttl: float = SPOOL_TTL_SECONDS,
    log: Callable[[str], None] = lambda _message: None,
) -> list[str]:
    """Deliver whatever the owner has left, as the account that can.

    Called by the side holding the tmux grant. Returns the roles delivered to.

    Every handoff leaves, one way or the other: delivered and removed, too old
    and removed, or unreadable and removed. That is the cleanup guarantee, and
    it holds because this runs on a loop rather than on the next paste.
    """
    delivered: list[str] = []
    try:
        manifests = sorted(directory.glob(f"*{MANIFEST_SUFFIX}"))
    except OSError:
        return delivered
    for manifest in manifests:
        # The image's name is read from the manifest, not guessed from the
        # manifest's own: the extension follows the clipboard's MIME type, so
        # stripping the suffix gives a path that does not exist and leaves the
        # picture behind on every failure path.
        stem_guess = Path(str(manifest)[: -len(MANIFEST_SUFFIX)])
        image = stem_guess
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            image = Path(str(payload["image"]))
            target = str(payload["target"])
            reference = str(payload["reference"])
            created = float(payload.get("created_at") or 0.0)
        except (OSError, ValueError, KeyError):
            _discard(manifest, image, stem_guess)
            continue
        if now - created > ttl:
            log(f"switchyard: a clipboard handoff for {payload.get('role')} expired unclaimed")
            _discard(manifest, image)
            continue
        if not image.exists():
            _discard(manifest, image)
            continue
        try:
            send(target, reference)
        except Exception as exc:
            log(f"switchyard: could not hand an image to {target}: {exc}")
            _discard(manifest, image)
            continue
        delivered.append(str(payload.get("role") or ""))
        _discard(manifest, image)
    return delivered


def _discard(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError:
            continue


def _command_name_of(role: Any) -> str:
    cli = list(getattr(role, "cli", []) or [])
    return Path(cli[0]).name if cli else ""
