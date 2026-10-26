#!/usr/bin/env python3
"""`switchyard paste-image <project> <role>`: hand one clipboard image to one role (SYRD-247).

On a compositor without a data-control protocol -- GNOME's Mutter implements
neither `wlr-data-control` nor `ext-data-control` -- no provider in a role pane
can read the desktop clipboard. Claude shells out to `wl-paste`/`xclip` for
images; Codex links arboard and wl-clipboard-rs and falls back to an X11 that
the approved desktop policy deliberately leaves unreachable. Nothing on the
tenant's side can be configured to fix that without handing every role sharing
the tenant's UID a reusable clipboard read, which SYRD-98 forbids.

So the desktop owner hands over one image, never a capability:

* Run by the GUI owner named in a desktop policy that owner installed for this
  project -- read from the owner's own copy under
  `~/.config/switchyard/desktop-access/`, the one record that proves both. Any
  other caller is refused before the clipboard is touched.
* One `image/*` type the clipboard offers, read with whichever tool reaches it
  (`wl-paste`, then `xclip` through XWayland), and refused past
  `MAX_IMAGE_BYTES` WHILE it is read.
* Written into the hand-off directory of the grant's exposure
  (`/var/lib/switchyard/desktop/<grant>/paste/`, SYRD-687), a directory
  only the owner can write. The tenant may traverse the exposure, list the
  directory, and read each file, through `desktop_access.grant`.
* The image first, then its manifest, renamed into place last, so the tenant's
  listener never sees a manifest for an image that is not complete.

Delivery is the tenant's listener's job (`scripts/ticket_board/image_handoff.py`):
the owner holds no grant to type into a tenant's pane. Removal is the owner's
access service's job (`desktop_access.sweep_handoffs`), 15 minutes after the
copy. This command never logs or echoes clipboard contents.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import secrets
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from scripts import desktop_access as desktop

#: The largest image handed over: a full-screen 4K screenshot fits, and a
#: mistaken copy of something enormous is refused while it is being read.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: The image types a provider can open, and the extension each is written with.
IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
}
MANIFEST_SCHEMA = "switchyard.image-handoff.v1"
MANIFEST_SUFFIX = ".handoff.json"
ROLE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


class PasteImageError(RuntimeError):
    """Refused. The message is written for the person who ran the command."""


def _open_reader(args: Sequence[str]) -> Any:
    return subprocess.Popen(list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


@dataclass
class Clipboard:
    """The GUI owner's clipboard, asked two things: which types, and one type's bytes."""

    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run
    opener: Callable[[Sequence[str]], Any] = _open_reader
    reader: str = field(default="", init=False)

    def types(self) -> list[str]:
        """What the clipboard offers, from the first tool that can reach it.

        `wl-paste` needs a data-control protocol, which KWin offers and Mutter
        refuses; under GNOME the owner still has XWayland, so `xclip` is the
        tool that answers there. Claude's own image path tries the same two.
        """
        tried: list[str] = []
        for argv in (["wl-paste", "--list-types"], ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]):
            tried.append(argv[0])
            try:
                proc = self.runner(argv, capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                continue
            offered = [line.strip() for line in str(proc.stdout or "").splitlines() if line.strip()]
            if proc.returncode == 0 and offered:
                self.reader = argv[0]
                return offered
        raise PasteImageError(
            "switchyard: could not read this desktop's clipboard with " + " or ".join(tried) + ". "
            "Run this as the desktop owner, in their session; on GNOME install xclip, which "
            "reaches the clipboard through XWayland."
        )

    def read(self, mime: str, *, max_bytes: int = MAX_IMAGE_BYTES) -> bytes:
        """At most `max_bytes` of one type; the reader is killed past that, unread."""
        if self.reader == "xclip":
            argv = ["xclip", "-selection", "clipboard", "-t", mime, "-o"]
        else:
            argv = ["wl-paste", "--no-newline", "--type", mime]
        with self.opener(argv) as proc:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    proc.kill()
                    raise PasteImageError(
                        f"switchyard: that image is larger than the {max_bytes // (1024 * 1024)} MiB "
                        "limit for a handoff; nothing was written."
                    )
                chunks.append(chunk)
            if proc.wait() != 0:
                raise PasteImageError(f"switchyard: the clipboard would not give up its {mime}.")
        return b"".join(chunks)


def chosen_image_type(types: Sequence[str]) -> str:
    """PNG, then JPEG, then any other type a provider can open; or a refusal naming what is there."""
    images = [entry for entry in types if entry in IMAGE_TYPES]
    if not images:
        shown = ", ".join(types[:8])
        raise PasteImageError(
            "switchyard: there is no image on the clipboard -- copy one first. "
            + (f"It holds: {shown}." if shown else "It is empty.")
        )
    for preferred in ("image/png", "image/jpeg"):
        if preferred in images:
            return preferred
    return images[0]


def approved_policy(project: str, *, uid: int, home: Path) -> dict[str, Any]:
    """The desktop policy this account installed, as GUI owner, for `project`.

    `desktop_access.install_owner` writes it to the owner's own home as
    `switchyard-desktop-<gui uid>-<tenant uid>-<project>.json`; only the owner
    named in it ever does. So finding it there, owned by the caller and naming
    the caller as GUI user, is the proof that the caller is this project's
    approved desktop owner.
    """
    directory = home / ".config" / "switchyard" / "desktop-access"
    pattern = re.compile(rf"switchyard-desktop-{uid}-(\d+)-{re.escape(project)}\.json")
    try:
        names = sorted(entry for entry in os.listdir(directory))
    except OSError:
        names = []
    matches = [(name, pattern.fullmatch(name)) for name in names]
    found = [(name, match) for name, match in matches if match]
    if len(found) != 1:
        approved = sorted({name.rsplit("-", 1)[-1][: -len(".json")] for name in names
                           if name.startswith(f"switchyard-desktop-{uid}-") and name.endswith(".json")})
        raise PasteImageError(
            f"switchyard: you have not approved a desktop grant for {project}, so there is no "
            "role you may hand an image to. "
            + (f"Your approved projects: {', '.join(approved)}." if approved else
               "Run this as the desktop owner who approved the project's desktop policy.")
        )
    name, match = found[0]
    path = directory / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
        raise PasteImageError(f"switchyard: {path} is not a policy file you own; refusing to trust it.")
    raw = json.loads(path.read_text(encoding="utf-8"))
    try:
        policy = desktop.validate_policy(raw, project=project, tenant=str(raw.get("tenant_user", "")))
    except desktop.DesktopAccessError as exc:
        raise PasteImageError(f"switchyard: {path} is not a valid desktop policy: {exc}") from None
    me = pwd.getpwuid(uid).pw_name
    if policy["mode"] != "wayland" or policy["gui_user"] != me:
        raise PasteImageError(f"switchyard: {project}'s desktop policy does not name you as its desktop owner.")
    try:
        tenant = pwd.getpwnam(policy["tenant_user"])
    except KeyError:
        raise PasteImageError(f"switchyard: {project}'s tenant account {policy['tenant_user']} does not exist.") from None
    if str(tenant.pw_uid) != match.group(1):
        raise PasteImageError(f"switchyard: {path} names a different tenant account than its file name.")
    if tenant.pw_uid == uid:
        # Same account: every role pane runs as the caller, so this would be a
        # clipboard read any role could make for itself (SYRD-98).
        raise PasteImageError(
            f"switchyard: {project} runs as your own account, so a role could take your clipboard "
            "with this command; it is refused."
        )
    return policy


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def deposit(
    project: str,
    role: str,
    *,
    clipboard: Clipboard | None = None,
    uid: int | None = None,
    home: Path | None = None,
    locate: Callable[[dict[str, Any]], Path] = desktop.exposure_for,
    grant: Callable[[Path, int, str], Any] = desktop.grant,
    clock: Callable[[], float] = time.time,
    token: Callable[[], str] = lambda: secrets.token_hex(16),
    max_bytes: int = MAX_IMAGE_BYTES,
) -> dict[str, Any]:
    """Read one image as the GUI owner and leave it where `role`'s listener collects it."""
    if not PROJECT_NAME.fullmatch(project):
        raise PasteImageError(f"switchyard: {project!r} is not a project name.")
    if not ROLE_NAME.fullmatch(role):
        raise PasteImageError(f"switchyard: {role!r} is not a role name.")
    uid = os.geteuid() if uid is None else uid
    me = pwd.getpwuid(uid)
    policy = approved_policy(project, uid=uid, home=Path(me.pw_dir) if home is None else home)
    tenant_uid = pwd.getpwnam(policy["tenant_user"]).pw_uid
    exposure = locate(policy)

    board = clipboard or Clipboard()
    mime = chosen_image_type(board.types())
    payload = board.read(mime, max_bytes=max_bytes)
    if not payload:
        raise PasteImageError("switchyard: the clipboard's image was empty; nothing was written.")

    leaf = desktop.prepare_handoff_dir(exposure, tenant_uid)
    desktop.sweep_handoffs(exposure)
    stem = f"paste-{token()}"
    image = leaf / f"{stem}.{IMAGE_TYPES[mime]}"
    staged = leaf / f".tmp-{stem}{MANIFEST_SUFFIX}"
    manifest = leaf / f"{stem}{MANIFEST_SUFFIX}"
    created_at = clock()
    record = {
        "schema": MANIFEST_SCHEMA,
        "project": project,
        "role": role,
        "image": image.name,
        "mime": mime,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "from": me.pw_name,
        "created_at": created_at,
        "expires_at": created_at + desktop.HANDOFF_TTL_SECONDS,
    }
    written: list[Path] = []
    try:
        _write_new(image, payload)
        written.append(image)
        grant(image, tenant_uid, "r--")
        _write_new(staged, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        written.append(staged)
        grant(staged, tenant_uid, "r--")
        os.rename(staged, manifest)
        written[-1] = manifest
    except Exception as exc:
        for path in written:
            path.unlink(missing_ok=True)
        raise PasteImageError(
            f"switchyard: the image could not be handed to {project}'s {role}, and nothing was left "
            f"behind: {exc}"
        ) from exc
    return {**record, "manifest": str(manifest), "path": str(image)}


def main(argv: Sequence[str] | None = None, *, print_func: Callable[[str], None] = print) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0].startswith("-"):
        raise SystemExit(
            "usage: switchyard paste-image <project> <role>\n"
            "Copy an image first, then run this as the desktop owner. It hands that one image to "
            "the role, which is told where it is; it is deleted 15 minutes after this."
        )
    try:
        result = deposit(args[0], args[1])
    except (PasteImageError, desktop.DesktopAccessError) as exc:
        raise SystemExit(str(exc)) from None
    expires = time.strftime("%H:%M:%S", time.localtime(result["expires_at"]))
    print_func(
        f"switchyard: {max(1, result['bytes'] // 1024)} KiB of {result['mime']} is waiting for "
        f"{result['project']}'s {result['role']}. Its listener tells the role where it is once the "
        f"pane is idle; it is deleted at {expires}."
    )
    return 0
