"""The listener's half of an image handoff: tell a role where the owner left its image (SYRD-247).

`switchyard paste-image`, run by the desktop owner, leaves one image and a
manifest in the hand-off directory of the grant's exposure
(`/var/lib/switchyard/desktop/<grant>/paste/`, SYRD-687) and grants this
tenant read on them. The owner cannot type into a tenant's pane; this account can,
through the same `directorctl send` every notice uses. So the listener, on each
pass of its loop:

* lists the project's handoff directory (the approved grant gives it `r-x`
  there and nothing to write);
* takes a manifest only if it and its image are regular files owned by the GUI
  owner, opened without following links, and the image's size and SHA-256
  match the manifest;
* delivers each handoff once, to the pane of the role the manifest names --
  looked up in the listener's own role table, never taken from the file -- and
  only while that pane is idle;
* remembers what it delivered in its own runtime directory, so a restart does
  not deliver twice.

It never deletes anything; it cannot. The owner's access service removes each
image `desktop_access.HANDOFF_TTL_SECONDS` after the copy. A handoff is
delivered only inside `DELIVERY_WINDOW_SECONDS` of the copy, so the image a
role is told about still exists for at least `MIN_RETAINED_AFTER_DELIVERY`
seconds after the message reaches it -- the provider opens it, with Claude's
Read or Codex's view_image, when it acts on that message.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    from scripts import desktop_access as desktop
except ImportError:  # pragma: no cover - the listener's own entry point puts scripts/ on the path
    import desktop_access as desktop  # type: ignore[no-redef]

#: The shortest time an image is guaranteed to exist after its role is told.
MIN_RETAINED_AFTER_DELIVERY = 5 * 60
#: A handoff is delivered only this long after the copy; later it is let expire.
DELIVERY_WINDOW_SECONDS = desktop.HANDOFF_TTL_SECONDS - MIN_RETAINED_AFTER_DELIVERY
#: Where the project registry lives, as `team_launcher` resolves it.
REGISTRY_DIR_ENV = "SWITCHYARD_PROJECT_REGISTRY_DIR"
DEFAULT_REGISTRY_DIR = Path("/etc/switchyard/projects")
#: How often the exposure is proven again: an upgrade or a withdrawn grant changes it.
RELOCATE_SECONDS = 60.0
MANIFEST_NAME = re.compile(r"(paste-[0-9a-f]{32})\.handoff\.json")
MAX_MANIFEST_BYTES = 4096
MAX_IMAGE_BYTES = 10 * 1024 * 1024
SCHEMA = "switchyard.image-handoff.v1"
#: What a role is told. directorctl types anything longer than 200 characters
#: into a file and sends a pointer to it, so the shorter form is used when the
#: path makes the first one too long.
MESSAGE = ("Image from {owner}'s clipboard: {path} - open it with your image tool "
           "(Read or view_image); it is deleted at {expires}.")
SHORT_MESSAGE = "Image: {path} - open it (Read or view_image); deleted at {expires}."
DIRECT_MESSAGE_MAX = 200


@dataclass(frozen=True)
class Handoff:
    stem: str
    role: str
    image: Path
    created_at: float
    expires_at: float
    owner: str

    def message(self) -> str:
        expires = time.strftime("%H:%M", time.localtime(self.expires_at))
        message = MESSAGE.format(owner=self.owner, path=self.image, expires=expires)
        if len(message) > DIRECT_MESSAGE_MAX:
            message = SHORT_MESSAGE.format(path=self.image, expires=expires)
        return message


def _open_owned(path: Path, owner_uid: int, limit: int) -> tuple[int, os.stat_result]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_size > limit:
            raise ValueError(f"{path.name} is not a regular file of the desktop owner's within {limit} bytes")
    except BaseException:
        os.close(fd)
        raise
    return fd, info


def _read_fd(fd: int, limit: int) -> bytes:
    chunks, total = [], 0
    with os.fdopen(fd, "rb") as stream:
        while chunk := stream.read(64 * 1024):
            total += len(chunk)
            if total > limit:
                raise ValueError("larger than its limit")
            chunks.append(chunk)
    return b"".join(chunks)


@dataclass
class ImageHandoffs:
    """One project's handoff directory, as this tenant reads it."""

    project: str
    owner: str
    owner_uid: int
    #: The grant's exposure directory, proven again every RELOCATE_SECONDS.
    locate: Callable[[], Path]
    ledger_path: Path | None = None
    clock: Callable[[], float] = time.time
    delivered: dict[str, float] = field(default_factory=dict)
    _directory: Path | None = field(default=None, init=False)
    _located_at: float = field(default=float("-inf"), init=False)
    _reported: set[str] = field(default_factory=set, init=False)
    #: Handoffs already read and checked, so a busy pane does not mean
    #: re-reading its image on every pass. Only the owner can write here.
    _verified: dict[str, Handoff] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        try:
            loaded = json.loads(self.ledger_path.read_text(encoding="utf-8")) if self.ledger_path else {}
            self.delivered = {str(k): float(v) for k, v in dict(loaded).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            self.delivered = {}

    def directory(self) -> Path | None:
        now = self.clock()
        if now - self._located_at >= RELOCATE_SECONDS:
            self._located_at = now
            try:
                self._directory = desktop.handoff_dir(self.locate())
            except (desktop.DesktopAccessError, OSError):
                self._directory = None
        return self._directory

    def pending(self, roles: Mapping[str, str], log: Callable[[str], None]) -> list[Handoff]:
        """Every complete, owner-made handoff for a known role not yet delivered, oldest first."""
        directory = self.directory()
        if directory is None:
            return []
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return []
        now = self.clock()
        found: list[Handoff] = []
        for name in names:
            match = MANIFEST_NAME.fullmatch(name)
            if not match or match.group(1) in self.delivered:
                continue
            stem = match.group(1)
            try:
                handoff = self._verified.get(stem) or self._read(directory, stem, roles)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._report(stem, log, f"image handoff {stem} refused: {exc}")
                continue
            self._verified[stem] = handoff
            if now - handoff.created_at > DELIVERY_WINDOW_SECONDS:
                self._report(stem, log, f"image handoff {stem} for {handoff.role} was not delivered in time; "
                                        "it is left to expire")
                continue
            found.append(handoff)
        self._verified = {stem: seen for stem, seen in self._verified.items() if f"{stem}.handoff.json" in names}
        return sorted(found, key=lambda handoff: handoff.created_at)

    def _read(self, directory: Path, stem: str, roles: Mapping[str, str]) -> Handoff:
        fd, _info = _open_owned(directory / f"{stem}.handoff.json", self.owner_uid, MAX_MANIFEST_BYTES)
        record = json.loads(_read_fd(fd, MAX_MANIFEST_BYTES).decode("utf-8"))
        if record["schema"] != SCHEMA or record["project"] != self.project:
            raise ValueError("it is not this project's handoff")
        role = str(record["role"])
        if role not in roles:
            raise ValueError(f"{self.project} has no role {role!r}")
        image_name = str(record["image"])
        if not re.fullmatch(re.escape(stem) + r"\.(?:png|jpg|gif|webp|bmp)", image_name):
            raise ValueError(f"its image {image_name!r} is not its own")
        image = directory / image_name
        fd, info = _open_owned(image, self.owner_uid, MAX_IMAGE_BYTES)
        payload = _read_fd(fd, MAX_IMAGE_BYTES)
        if len(payload) != int(record["bytes"]) or hashlib.sha256(payload).hexdigest() != record["sha256"]:
            raise ValueError("its image does not match its manifest")
        return Handoff(stem=stem, role=role, image=image, created_at=float(record["created_at"]),
                       expires_at=float(record["expires_at"]), owner=self.owner)

    def _report(self, stem: str, log: Callable[[str], None], message: str) -> None:
        if stem not in self._reported:
            self._reported.add(stem)
            log(message)

    def deliver(
        self,
        *,
        targets: Mapping[str, str],
        is_working: Callable[[str], bool],
        send: Callable[[str, str], Any],
        log: Callable[[str], None],
    ) -> list[str]:
        """Tell each idle role where its image is, once. Returns the stems delivered."""
        done: list[str] = []
        for handoff in self.pending(targets, log):
            target = targets[handoff.role]
            if is_working(target):
                continue
            try:
                send(target, handoff.message())
            except Exception as exc:  # retried on the next pass, inside the window
                log(f"image handoff {handoff.stem} to {handoff.role} failed, will retry: {exc}")
                continue
            self.delivered[handoff.stem] = self.clock()
            self._verified.pop(handoff.stem, None)
            done.append(handoff.stem)
            log(f"image handoff {handoff.stem} delivered to {handoff.role} ({target})")
        if done:
            self._save()
        return done

    def _save(self) -> None:
        horizon = self.clock() - desktop.HANDOFF_TTL_SECONDS
        self.delivered = {stem: at for stem, at in self.delivered.items() if at >= horizon}
        if self.ledger_path is None:
            return
        temporary = self.ledger_path.with_name(f".{self.ledger_path.name}.tmp")
        temporary.write_text(json.dumps(self.delivered, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.ledger_path)


def from_environment(
    project: str,
    *,
    environ: Mapping[str, str] = os.environ,
    log: Callable[[str], None] = lambda _message: None,
) -> ImageHandoffs | None:
    """This tenant's handoffs, from its own approved desktop policy; None when it has none.

    The policy is read where the tenant's own configuration records it -- the
    registry names the configuration, the configuration carries the policy --
    and is accepted only when it is a valid Wayland grant naming this account
    as its tenant. A headless tenant has no desktop and so no handoffs.
    """
    try:
        registry = Path(environ.get(REGISTRY_DIR_ENV, "").strip() or DEFAULT_REGISTRY_DIR)
        entry = json.loads((registry / f"{project}.json").read_text(encoding="utf-8"))
        config = json.loads(Path(entry["config_path"]).read_text(encoding="utf-8"))
        raw = config.get("desktop_access")
        if not isinstance(raw, dict) or raw.get("mode") != "wayland":
            return None
        policy = desktop.validate_policy(raw, project=project, tenant=str(raw.get("tenant_user", "")))
        me = pwd.getpwuid(os.geteuid()).pw_name
        if policy["tenant_user"] != me:
            log(f"image handoff off: {project}'s desktop policy names {policy['tenant_user']}, not {me}")
            return None
        owner = pwd.getpwnam(policy["gui_user"])
    except (OSError, ValueError, KeyError, TypeError, desktop.DesktopAccessError) as exc:
        log(f"image handoff off for {project}: {exc}")
        return None
    runtime = environ.get("XDG_RUNTIME_DIR", "").strip()
    return ImageHandoffs(
        project=project,
        owner=owner.pw_name,
        owner_uid=owner.pw_uid,
        locate=lambda: desktop.exposure_for(policy),
        ledger_path=Path(runtime) / f"switchyard-image-handoffs-{project}.json" if runtime else None,
    )


def run(listener: Any) -> list[str]:
    """One pass, from the listener's loop. A listener with no handoffs does nothing."""
    handoffs = getattr(listener, "image_handoffs", None)
    if handoffs is None:
        return []
    try:
        return handoffs.deliver(
            targets=listener.role_targets,
            is_working=listener.activity_gate,
            send=listener.sender,
            log=listener.logger.info,
        )
    except Exception as exc:  # a handoff must never stop the listener's notices
        listener.logger.warning("image handoff pass failed: %s", exc)
        return []
