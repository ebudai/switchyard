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

#: Where a handed-over image lives, relative to the role's own working
#: directory. Inside the role's cwd on purpose -- see the module docstring.
SPOOL_DIR_NAME = Path(".switchyard") / "paste"

#: What each provider is told, once the file exists. Both forms are from the
#: providers' own documentation: Claude takes an image path in the composer,
#: Codex takes an `@` attachment.
PROVIDER_REFERENCE = {
    "claude": "Analyze this image: {path}",
    "codex": "@{path}",
}


class ImageHandoffError(RuntimeError):
    """Refused. The message is written to be read by the person who ran it."""


@dataclass(frozen=True)
class Clipboard:
    """The GUI owner's clipboard, as two commands and nothing else.

    Separated so a test drives it without a compositor, and so it is obvious
    that exactly two things are ever asked of it: which types are on offer, and
    the bytes of one chosen type.
    """

    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run

    def types(self) -> list[str]:
        proc = self.runner(
            ["wl-paste", "--list-types"], capture_output=True, text=True, timeout=10
        )
        if getattr(proc, "returncode", 1) != 0:
            raise ImageHandoffError(
                "switchyard: could not read the clipboard's types. This must run as the "
                "desktop owner, in their session."
            )
        return [line.strip() for line in str(proc.stdout or "").splitlines() if line.strip()]

    def read(self, mime: str) -> bytes:
        proc = self.runner(
            ["wl-paste", "--type", mime], capture_output=True, timeout=30
        )
        if getattr(proc, "returncode", 1) != 0:
            raise ImageHandoffError(f"switchyard: the clipboard would not give up its {mime}")
        return bytes(getattr(proc, "stdout", b"") or b"")


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


def spool_paths(role: Any) -> tuple[Path, Path]:
    """The directory a role's images land in, and its VCS-ignore file."""
    workdir = Path(role.workdir)
    return workdir / SPOOL_DIR_NAME, workdir / SPOOL_DIR_NAME / ".gitignore"


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
        if entry.name == ".gitignore" or not entry.is_file():
            continue
        try:
            if now - entry.stat().st_mtime < ttl:
                continue
            entry.unlink()
        except OSError:
            continue
        removed.append(entry)
    return removed


def hand_image_to_role(
    config: Any,
    *,
    role_name: str,
    owner_uid: int,
    live_cli: Callable[[Any], list[str]],
    sender: Callable[[str, str], Any],
    clipboard: Clipboard | None = None,
    grant: Callable[[Path, int, str], Any] | None = None,
    geteuid: Callable[[], int] = os.geteuid,
    now: Callable[[], float] = time.time,
    token: Callable[[], str] = lambda: os.urandom(8).hex(),
    max_bytes: int = MAX_IMAGE_BYTES,
    print_func: Callable[[str], None] = print,
) -> int:
    """Read one image as the owner, put it where one role can read it, say so."""
    role = _role_named(config, role_name)
    _refuse_if_run_by_the_tenant(owner_uid, geteuid=geteuid)

    # Which provider is ACTUALLY in that pane, which is both the liveness proof
    # and the thing that decides how the image is referenced. A configured CLI
    # is a claim about a pane; this is a reading of it.
    running = live_cli(role)
    if not running:
        raise ImageHandoffError(
            f"switchyard: {config.project}'s {role_name} pane is not running a provider right "
            "now, so there is nothing to hand an image to."
        )
    provider = running[0]
    reference = PROVIDER_REFERENCE.get(provider)
    if reference is None:
        raise ImageHandoffError(
            f"switchyard: {role_name} is running {provider}, which has no documented way to "
            "take an image from a file. Nothing was read from your clipboard."
        )

    board = clipboard or Clipboard()
    mime = chosen_image_type(board.types())
    payload = board.read(mime)
    if not payload:
        raise ImageHandoffError("switchyard: the clipboard's image was empty; nothing was written.")
    if len(payload) > max_bytes:
        raise ImageHandoffError(
            f"switchyard: that image is {len(payload) // 1024} KiB, over the "
            f"{max_bytes // 1024} KiB limit for a handoff. Nothing was written."
        )

    directory, ignore = spool_paths(role)
    directory.mkdir(parents=True, exist_ok=True)
    sweep_spool(directory, now=now())
    if not ignore.exists():
        # A handed-over image must never become a commit.
        ignore.write_text("*\n", encoding="utf-8")
    suffix = {"image/png": ".png", "image/jpeg": ".jpg"}.get(mime, ".img")
    spool = directory / f"paste-{token()}{suffix}"
    spool.write_bytes(payload)
    spool.chmod(0o640)
    if grant is not None:
        # One regular file, one entry, through the discipline that already
        # refuses to broaden an unrelated masked entry.
        grant(spool, owner_uid, "r")

    try:
        sender(role.target, reference.format(path=spool))
    except Exception as exc:
        # Fails closed: a handoff that could not be delivered leaves no picture
        # behind in somebody's working directory.
        spool.unlink(missing_ok=True)
        raise ImageHandoffError(
            f"switchyard: {role_name} could not be told about the image, so it was removed: {exc}"
        ) from exc

    print_func(
        f"switchyard: handed {len(payload) // 1024} KiB of {mime} to {config.project}'s "
        f"{role_name} ({provider}). It is at {spool} and is removed once used, or within "
        f"{SPOOL_TTL_SECONDS:g}s if it is not."
    )
    return 0
