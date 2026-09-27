"""This host's standing desktop approval: where it lives, how it is read and written, and the command that records, shows or withdraws it.

- **Where:** `DEFAULT_DESKTOP_APPROVAL_SETTING_PATH`, unless a caller names
  another (`_desktop_approval_setting_path`).
- **At provisioning:** `read_host_desktop_approval` and
  `write_host_desktop_approval` read and record the desktop owner's approval.
- **As root's record:** `read_desktop_approval_record` reads it the way root
  reads its other authorities -- no symlink anywhere, root's and nobody
  else's to write -- and `write_desktop_approval_record` publishes it root's
  0600, staged and renamed without following.
- **The command:** `switchyard_approve_desktop_command` shows, approves or
  revokes it. Writing needs root and a person resolved from the mechanism that
  elevated this process (`_desktop_approval_operator`), never a flag; a
  revocation keeps the record with nobody approved in it
  (`DESKTOP_APPROVAL_REVOKED`), and `_desktop_approval_lines` says what it
  records.

The user-name check, the JSON writer, the privileged uid and the no-follow
reader stay in `scripts/team_launcher.py` and are read there when a function
runs; the operator resolver and the desktop sessions are each function's own
imports. The suites patch `read_host_desktop_approval` on the launcher, and
its callers reach it there.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-325). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import os
import pwd
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence



# Machine-scoped for the same reason: the desktop owner says once that Switchyard
# projects may use their session, and every later `switchyard new` reads that
# instead of asking again. Root-owned, beside the other host settings, because a
# tenant that could write this could approve its own access (SYRD-143).
DEFAULT_DESKTOP_APPROVAL_SETTING_PATH = Path("/etc/switchyard/desktop-approval.json")


def _desktop_approval_setting_path(settings_path: Path | None = None) -> Path:
    return settings_path or DEFAULT_DESKTOP_APPROVAL_SETTING_PATH


def read_host_desktop_approval(settings_path: Path | None = None) -> dict[str, str]:
    """The desktop owner's standing approval for this host, or nothing.

    Nothing is the safe answer to every way of not being able to read one:
    absent, unreadable, not an object. A malformed user name is not treated
    that way -- it is a file somebody edited into a state this will not act on,
    and continuing quietly would hide it.
    """
    from scripts import team_launcher as launcher

    path = _desktop_approval_setting_path(settings_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    gui_user = str(raw.get("gui_user") or "").strip()
    if not gui_user:
        return {}
    if not launcher._is_valid_owner_user_name(gui_user):
        raise SystemExit(
            f"switchyard: {path} records gui_user {gui_user!r}, which is not a plain Unix "
            "user name; fix or remove that file before provisioning"
        )
    return {
        "gui_user": gui_user,
        "approved_by": str(raw.get("approved_by") or "").strip(),
        "approved_at": str(raw.get("approved_at") or "").strip(),
        "reference": str(raw.get("reference") or "").strip(),
    }


def write_host_desktop_approval(
    gui_user: str,
    *,
    settings_path: Path | None = None,
    confirmed_by: str = "",
    reference: str = "",
) -> Path:
    """Record that this host's desktop owner approves scoped project access."""
    from scripts import team_launcher as launcher

    path = _desktop_approval_setting_path(settings_path)
    approved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.parent.mkdir(parents=True, exist_ok=True)
    launcher._write_json_atomic(
        path,
        {
            "gui_user": gui_user,
            "approved_by": confirmed_by or gui_user,
            "approved_at": approved_at,
            "reference": reference
            or f"{gui_user} approved Switchyard desktop access on this host at {approved_at}",
        },
    )
    return path


#: A record this command wrote to take an approval away. The schema is the one
#: the approval already has: `read_host_desktop_approval` answers "no approval"
#: for an empty `gui_user` before it looks at anything else, so a revocation is
#: an approval record with nobody approved in it -- which leaves who revoked it,
#: when, and on what basis where the approval's own provenance was, instead of
#: deleting the evidence that it ever existed (SYRD-174).
DESKTOP_APPROVAL_REVOKED = ""


def _desktop_approval_operator(
    operator_resolver: Callable[[], Any] | None = None,
) -> tuple[str, str, str]:
    """Who is asking, and which mechanism said so. Never a claim, never a flag.

    The prompt path records the owner as the approver because the owner is the
    one answering it. A non-interactive command has no such witness, so the
    attribution comes from the only thing that cannot be typed: the mechanism
    that elevated this process. `PKEXEC_UID` first, then `SUDO_USER`, each
    resolved through the account database -- the same rule the rollout journal
    uses, for the same reason.
    """
    from scripts.ticket_board.rollout_journal import resolve_operator

    operator = (operator_resolver or resolve_operator)()
    name = str(getattr(operator, "name", "") or "")
    source = str(getattr(operator, "source", "") or "")
    uid = getattr(operator, "uid", None)
    return name, source, "" if uid is None else str(uid)


def read_desktop_approval_record(
    settings_path: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """This host's approval record as root wrote it, or why it may not be read.

    Read the way root reads its other authorities -- by fd, refusing a symlink
    at every component, required to belong to root and to be unwritable by
    anybody else -- because a record somebody else can replace is a record
    somebody else can approve with. Absence is not a refusal: a host that has
    never been approved simply has none.
    """
    from scripts import team_launcher as launcher

    path = _desktop_approval_setting_path(settings_path)
    try:
        path.lstat()
    except FileNotFoundError:
        return None, ""
    except OSError as exc:
        return None, f"{path} could not be read: {exc}"
    document, problem = launcher.read_plan_no_follow(path, require_root_owned=True)
    if document is None:
        return None, problem
    raw = document.data
    if not isinstance(raw, dict):
        return None, f"{path} is not a desktop approval record"
    gui_user = str(raw.get("gui_user") or "").strip()
    if gui_user and not launcher._is_valid_owner_user_name(gui_user):
        return None, (
            f"{path} records gui_user {gui_user!r}, which is not a plain Unix user name"
        )
    return dict(raw), ""


def write_desktop_approval_record(
    payload: Mapping[str, Any], *, settings_path: Path | None = None
) -> Path:
    """Publish an approval record where only root can rewrite it.

    Staged beside the target and renamed, opened with O_NOFOLLOW so a symlink
    planted at either name is an error rather than a redirect, and left
    root-owned 0600 -- the mode this file has always had, for the reason its
    first writer gives: an approval a tenant could write is an approval a
    tenant could give itself.
    """
    from scripts import team_launcher as launcher

    path = _desktop_approval_setting_path(settings_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode("utf-8")
    staged = path.with_name(f".{path.name}.new")
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, body)
        os.fchown(descriptor, launcher.expected_privileged_uid(), 0)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    staged.replace(path)
    return path


def _desktop_approval_lines(record: Mapping[str, Any], path: Path) -> list[str]:
    gui_user = str(record.get("gui_user") or "").strip()
    if not gui_user:
        return [
            f"switchyard: {path} records no approval for this host.",
            f"switchyard: revoked by {record.get('revoked_by') or 'somebody unrecorded'}"
            f" at {record.get('revoked_at') or 'an unrecorded time'}"
            + (f" ({record['reference']})" if record.get("reference") else ""),
        ]
    return [
        f"switchyard: this host's desktop is {gui_user}'s, and scoped project access to it",
        f"switchyard: was approved by {record.get('approved_by') or 'somebody unrecorded'}"
        f" at {record.get('approved_at') or 'an unrecorded time'}",
        f"switchyard: on this basis: {record.get('reference') or '(no reference recorded)'}",
        f"switchyard: recorded at {path}",
    ]


def switchyard_approve_desktop_command(
    *,
    gui_user: str = "",
    reference: str = "",
    show: bool = False,
    revoke: bool = False,
    settings_path: Path | None = None,
    euid_getter: Callable[[], int] = os.geteuid,
    operator_resolver: Callable[[], Any] | None = None,
    owners_lister: Callable[[], Sequence[str]] | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """Record, show or withdraw this host's standing desktop approval.

    The record already existed and the reasoning behind it was already right;
    what it did not have was a front door. Its single writer sat behind an
    interactive prompt, so the only way to pre-authorize a host was to reach
    past the CLI into the library or to hand-write root-owned JSON -- both of
    them the thing everything else here is careful not to do (SYRD-174).

    Approving and revoking need root, and need a person: the attribution comes
    from the mechanism that elevated this process, not from a flag, so the
    record cannot be made to name somebody who did not ask for it. `--reference`
    is required for the same reason -- an approval nobody can attribute is worse
    than no approval.
    """
    from scripts import team_launcher as launcher

    from scripts import desktop_access as desktop

    path = _desktop_approval_setting_path(settings_path)
    record, problem = read_desktop_approval_record(settings_path)

    if show:
        if problem:
            print_func(f"switchyard: {problem}")
            print_func(
                "switchyard: this host's desktop approval cannot be read, so it is not "
                "shown. Fix or re-approve it rather than acting on it."
            )
            return 1
        if record is None:
            print_func(f"switchyard: this host has no recorded desktop approval ({path}).")
            return 1
        for line in _desktop_approval_lines(record, path):
            print_func(line)
        return 0

    if euid_getter() != 0:
        action = "revoking" if revoke else "approving"
        print_func(
            f"switchyard: {action} this host's desktop access writes a root-owned record. "
            f"Run: pkexec switchyard approve-desktop"
            + (" --revoke" if revoke else " --gui-user USER --reference '<why>'")
        )
        return 1
    operator, source, operator_uid = _desktop_approval_operator(operator_resolver)
    if not operator:
        print_func(
            "switchyard: this host's desktop approval records who gave it, and this run was "
            f"elevated by {source or 'nothing that names a person'} -- so there is nobody to "
            "record. Run it through pkexec or sudo from the account that is approving."
        )
        return 1
    if not reference.strip():
        print_func(
            "switchyard: --reference is required: it is what the record says when somebody "
            "later asks on what basis this host's desktop was opened to a tenant."
        )
        return 1
    if problem:
        print_func(f"switchyard: {problem}")
        print_func(
            "switchyard: refusing to write over a record this cannot read. Nothing was changed."
        )
        return 1

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if revoke:
        if record is None:
            print_func(
                f"switchyard: this host has no recorded desktop approval ({path}); nothing "
                "to revoke and nothing was changed."
            )
            return 0
        if not str(record.get("gui_user") or "").strip():
            print_func(f"switchyard: this host's desktop approval is already revoked ({path}).")
            for line in _desktop_approval_lines(record, path):
                print_func(line)
            return 0
        withdrawn = dict(record)
        written = write_desktop_approval_record(
            {
                "gui_user": DESKTOP_APPROVAL_REVOKED,
                "revoked_by": operator,
                "revoked_at": stamp,
                "revoked_via": source,
                "revoked_uid": operator_uid,
                "reference": reference.strip(),
                "withdrew": withdrawn,
            },
            settings_path=settings_path,
        )
        print_func(
            f"switchyard: revoked this host's desktop approval at {written} "
            f"(by {operator} via {source})"
        )
        print_func(
            f"switchyard: what was withdrawn: {withdrawn.get('gui_user')}'s desktop, approved "
            f"by {withdrawn.get('approved_by') or 'somebody unrecorded'} at "
            f"{withdrawn.get('approved_at') or 'an unrecorded time'}"
        )
        print_func(
            "switchyard: the record is kept, with nobody approved in it, so the withdrawal "
            "is evidence rather than an absence."
        )
        return 0

    chosen = gui_user.strip()
    if chosen and not launcher._is_valid_owner_user_name(chosen):
        print_func(f"switchyard: {chosen!r} is not a plain Unix user name.")
        return 1
    try:
        pwd.getpwnam(chosen) if chosen else None
    except KeyError:
        print_func(f"switchyard: {chosen} is not an account on this host.")
        return 1
    lister = owners_lister or (lambda: desktop.active_wayland_owners())
    try:
        owners = list(lister())
    except Exception as exc:  # noqa: BLE001 - logind not answering is not a guess to make
        print_func(f"switchyard: this host's desktop sessions could not be read: {exc}")
        owners = []
    if not chosen:
        if len(owners) == 1:
            chosen = owners[0]
        elif owners:
            print_func(
                "switchyard: several accounts have an active desktop session ("
                + ", ".join(sorted(owners))
                + "). Name the one this host is approving with --gui-user USER."
            )
            return 1
        else:
            print_func(
                "switchyard: no active desktop session was found, so there is no owner to "
                "infer. Name the account this host is approving with --gui-user USER."
            )
            return 1
    elif owners and chosen not in owners:
        print_func(
            f"switchyard: {chosen} has no active desktop session on this host (that would be "
            + ", ".join(sorted(owners))
            + "). Recording it anyway, because whose desktop this is is not something a "
            "session list decides."
        )

    if record is not None and str(record.get("gui_user") or "").strip() == chosen:
        print_func(f"switchyard: this host's desktop is already approved for {chosen} ({path}).")
        for line in _desktop_approval_lines(record, path):
            print_func(line)
        return 0

    written = write_desktop_approval_record(
        {
            "gui_user": chosen,
            "approved_by": operator,
            "approved_at": stamp,
            "approved_via": source,
            "approved_uid": operator_uid,
            "reference": reference.strip(),
        },
        settings_path=settings_path,
    )
    stored, stored_problem = read_desktop_approval_record(settings_path)
    if stored is None or str(stored.get("gui_user") or "") != chosen:
        print_func(f"switchyard: {stored_problem or 'the record did not read back'}")
        print_func(f"switchyard: {written} was written but does not read back; nothing is approved.")
        return 1
    print_func(
        f"switchyard: recorded {chosen}'s desktop approval for this host at {written} "
        f"(by {operator} via {source})"
    )
    print_func(
        "switchyard: projects provisioned from now on use it without asking again. It grants "
        "nothing by itself: each project still gets its own scoped policy."
    )
    return 0
