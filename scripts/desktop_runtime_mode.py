"""Whether the desktop owner's runtime directory is 0700, as its desktop requires (SYRD-831).

KDE refuses a wider one ("QStandardPaths: wrong permissions on runtime directory /run/user/1000, 0710 instead of
0700"). Switchyard's grants from before SYRD-791 put tenants' named entries there; revoking them with
`setfacl --no-mask -x` left the ACL's mask behind, and the mask shows as the group's mode bits. Revocation now drops
the ACL once no named entry is left; this says when a directory still is not 0700, and names the fix.

Read-only, from any account: the directory's own mode, through a stat its parent allows.
"""

from __future__ import annotations

import pwd
import stat
from pathlib import Path

RUNTIME_PARENT = Path("/run/user")


def runtime_mode_warning(gui_user: str, *, runtime_parent: Path = RUNTIME_PARENT) -> str:
    """'' when `gui_user`'s runtime directory is 0700, or cannot be read; otherwise what is wrong and its fix."""
    try:
        runtime = runtime_parent / str(pwd.getpwnam(gui_user).pw_uid)
        mode = stat.S_IMODE(runtime.stat().st_mode)
    except (KeyError, OSError):
        return ""
    if mode == 0o700:
        return ""
    return (f"{gui_user}'s runtime directory {runtime} is mode {mode:04o}, not 0700, and its desktop refuses it "
            f"(\"wrong permissions on runtime directory\"). An ACL left by revoked desktop grants does this; as "
            f"{gui_user}, `setfacl -b {runtime}` removes it (check `getfacl {runtime}` first if anything else should "
            "keep an entry there)")
