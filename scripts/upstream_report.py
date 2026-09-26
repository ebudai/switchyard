"""A tenant's link to the board it reports upstream to, and that board's credential.

A tenant may report tickets to another board (an operator's, or an upstream
project's). This module covers:
- where the tenant's copy of that board's report token lives
  (`upstream_report_credential_path`), inside the tenant's own tree and private
  to its owner;
- which board that is (`upstream_report_board`);
- recording the link in the tenant config (`record_upstream_report_link`);
- refreshing the credential from the upstream board's environment
  (`refresh_upstream_report_credential`), written owner-only through
  `_write_owner_private_file`.

`upgrade_project_command` calls the last two.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-288). `team_launcher`
imports this module at its top and still exports every name that lived there;
the suites patch `team_launcher.record_upstream_report_link` and
`refresh_upstream_report_credential` at that call site. This module never
imports `team_launcher` at its top. Launcher facilities (`uid_for_user`,
`_load_json`, `_write_json_atomic`, ...) are read from `scripts.team_launcher`
when a function runs.

The one exception is `home_dir_for_user`. It is the *default value* of every
`home_for_user` parameter here, and a default is bound when the `def` runs, at
import. It therefore comes from `scripts.host_accounts`, the leaf module the
launcher imports it from too, so it is the same object the launcher exports.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from scripts.host_accounts import home_dir_for_user

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


UPSTREAM_REPORT_CREDENTIAL_NAME = "upstream-report.env"


#: The only key this ever writes. The board's own `ticket-board.env` holds this
#: same key and nothing else that matters here; a write token, if one ever
#: appeared beside it, must not travel into a tenant's client credential.
UPSTREAM_REPORT_TOKEN_KEY = "TICKET_BOARD_TENANT_REPORT_TOKEN"


def upstream_report_credential_path(
    config: ProjectConfig,
    *,
    home_for_user: Callable[[str], Path | None] = home_dir_for_user,
) -> Path | None:
    """Where this tenant's report-only credential belongs.

    The configured path when there is one, so an operator who placed it
    somewhere keeps it; otherwise the same shape provisioning uses for the
    board's own env file -- `~/.config/<project>/upstream-report.env`.
    """
    configured = str(config.upstream_report_token_file or "").strip()
    if configured:
        return Path(configured).expanduser()
    owner = str(config.run_as_user or "").strip()
    if not owner:
        return None
    home = home_for_user(owner)
    if home is None:
        return None
    return home / ".config" / config.project / UPSTREAM_REPORT_CREDENTIAL_NAME


def _board_env_report_token(path: Path) -> tuple[str, str]:
    """The report token recorded in a board's own environment file.

    Reads ONLY `TICKET_BOARD_TENANT_REPORT_TOKEN`. Everything else in that file
    is the board's business and must not be copied into a credential a tenant
    holds -- the point of this credential is that it opens `file_report` and
    nothing else.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return "", f"{path} cannot be read ({exc.strerror})"
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != UPSTREAM_REPORT_TOKEN_KEY:
            continue
        token = value.strip().strip("\"'")
        if not token:
            return "", f"{path} records an empty {UPSTREAM_REPORT_TOKEN_KEY}"
        return token, ""
    return "", f"{path} has no {UPSTREAM_REPORT_TOKEN_KEY}"


def upstream_report_board(
    board_url: str,
    *,
    registry_dir: Path | None = None,
    home_for_user: Callable[[str], Path | None] = home_dir_for_user,
) -> tuple[str, Path, str]:
    """(project, its board env file, problem) for the board a tenant reports to.

    Resolved from the host registry rather than configured a second time: the
    upstream board is itself a Switchyard project here, and its provision
    artifact already records the URL it serves and the account that owns it.
    Asking the registry is what keeps the tenant's configuration from carrying
    a second copy of a fact that can drift.
    """
    from scripts import team_launcher as launcher

    wanted = board_url.strip().rstrip("/")
    if not wanted:
        return "", Path(), "no upstream report URL is configured"
    for entry in launcher._registry_project_entries(registry_dir):
        try:
            raw = launcher._load_json(entry.config_path)
        except (OSError, json.JSONDecodeError, SystemExit):
            continue
        if str(raw.get("board_url") or "").strip().rstrip("/") != wanted:
            continue
        owner = str(raw.get("run_as_user") or "").strip()
        if not owner:
            return "", Path(), f"{entry.slug} serves {board_url} but records no owner account"
        home = home_for_user(owner)
        if home is None:
            return "", Path(), f"{entry.slug}'s owner {owner} is not an account on this host"
        return entry.slug, home / ".config" / entry.slug / "ticket-board.env", ""
    return "", Path(), f"no project registered on this host serves {board_url}"


def record_upstream_report_link(
    config: ProjectConfig,
    *,
    config_path: Path,
    upstream_report_url: str,
    upstream_report_token_file: str = "",
    dry_run: bool = False,
    home_for_user: Callable[[str], Path | None] = home_dir_for_user,
    print_func: Callable[[str], None] = print,
) -> tuple[ProjectConfig, list[str]]:
    """Record where this tenant files reports, once, in its own configuration.

    The pane environment already carries `TICKET_BOARD_REPORT_URL`,
    `TICKET_BOARD_REPORT_ORIGIN_PROJECT` and
    `TICKET_BOARD_TENANT_REPORT_TOKEN_FILE` -- but only for a tenant whose
    configuration names the upstream board, and those two keys could only ever
    be supplied to `switchyard new`. A tenant provisioned before the feature,
    or one whose upstream board moved, has no way to acquire them, so its
    Director has to pass the URL and the token path by hand every time
    (SYRD-238).

    Writes nothing when the configuration already says this, so an upgrade that
    is run twice changes the file once.
    """
    from scripts import team_launcher as launcher

    wanted_url = upstream_report_url.strip()
    if not wanted_url:
        return config, []
    token_file = upstream_report_token_file.strip()
    if not token_file:
        derived = upstream_report_credential_path(
            replace(config, upstream_report_url=wanted_url), home_for_user=home_for_user
        )
        if derived is None:
            return config, [f"{config.project}'s report credential path cannot be resolved"]
        token_file = str(derived)
    if (
        str(config.upstream_report_url or "").strip() == wanted_url
        and str(config.upstream_report_token_file or "").strip() == token_file
    ):
        return config, []
    if dry_run:
        print_func(
            f"switchyard: would record {config.project}'s upstream report board {wanted_url} and "
            f"credential {token_file} in {config_path}; nothing written"
        )
        return config, []
    try:
        raw = launcher._load_json(config_path)
    except (OSError, json.JSONDecodeError, SystemExit) as exc:
        return config, [f"{config_path} could not be read ({exc})"]
    raw["upstream_report_url"] = wanted_url
    raw["upstream_report_token_file"] = token_file
    # The token itself never goes in here: the configuration is readable by
    # every role, and `load_project_config` refuses an inline token outright.
    raw.pop("tenant_report_token", None)
    raw.pop("upstream_report_token", None)
    launcher._write_json_atomic(config_path, raw, owner_user=config.run_as_user or "")
    print_func(
        f"switchyard: recorded {config.project}'s upstream report board {wanted_url} and "
        f"credential {token_file}, so its next launch carries both"
    )
    return replace(
        config, upstream_report_url=wanted_url, upstream_report_token_file=token_file
    ), []


def refresh_upstream_report_credential(
    config: ProjectConfig,
    *,
    dry_run: bool = False,
    registry_dir: Path | None = None,
    # A host resolves an account's home from the passwd database. A test must
    # not: the first version of this ticket's suite resolved the REAL home and
    # wrote test tokens into the live board's own credential file (SYRD-238).
    home_for_user: Callable[[str], Path | None] = home_dir_for_user,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Give this tenant the report-only token the upstream board accepts now.

    The board compares a submitted report token against one configured string,
    so rotating it -- which a cutover does, because the token is minted when
    the board's env file is first written -- silently strands every credential
    handed out before. MEFP's copy was from 2026-08-31 and its Director could
    not file a report at all (SYRD-238).

    Nothing wrote this credential before: it was an operator-supplied path, so
    there was nothing to refresh. Ownership of it starts here.

    Report-only by construction: the only value copied is the report token, and
    the only thing that token opens is `file_report`, which the board forces to
    `analysis`/`unassigned`. No write token is read, and none is written.
    """
    if not str(config.upstream_report_url or "").strip():
        return []
    owner = str(config.run_as_user or "").strip()
    if not owner:
        return [f"{config.project} reports upstream but records no owner account"]
    destination = upstream_report_credential_path(config, home_for_user=home_for_user)
    if destination is None:
        return [f"{config.project}'s upstream report credential path cannot be resolved"]
    upstream, board_env, problem = upstream_report_board(
        config.upstream_report_url, registry_dir=registry_dir, home_for_user=home_for_user
    )
    if problem:
        return [f"{config.project} cannot be given a report credential: {problem}"]
    token, problem = _board_env_report_token(board_env)
    if problem:
        return [f"{config.project} cannot be given {upstream}'s report credential: {problem}"]

    desired = f"{UPSTREAM_REPORT_TOKEN_KEY}={token}\n"
    current, _ = _board_env_report_token(destination)
    if current == token and _credential_is_private(destination, owner):
        print_func(
            f"switchyard: {config.project} already holds {upstream}'s current report credential "
            f"in {destination}"
        )
        return []
    if dry_run:
        print_func(
            f"switchyard: would give {config.project} {upstream}'s current report credential in "
            f"{destination}, 0600 and owned by {owner}; nothing written"
        )
        return []
    written = _write_owner_private_file(
        destination, desired, owner_user=owner, home_for_user=home_for_user
    )
    if written:
        return [f"{config.project}'s report credential was not written: {written}"]
    print_func(
        f"switchyard: gave {config.project} {upstream}'s current report credential in "
        f"{destination}, so its panes can file reports without being handed a token"
    )
    return []


def _credential_is_private(path: Path, owner: str) -> bool:
    """0600 and the tenant's own, read without following a link to ask."""
    from scripts import team_launcher as launcher

    uid = launcher.uid_for_user(owner)
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o600
        and (uid is None or info.st_uid == uid)
    )


def _write_owner_private_file(
    path: Path,
    text: str,
    *,
    owner_user: str,
    home_for_user: Callable[[str], Path | None] = home_dir_for_user,
) -> str:
    """Write `text` where only `owner_user` can read it, atomically. "" or why not.

    Root writing into an account's own tree, so it follows nothing: every
    directory from the home down is opened with O_NOFOLLOW and must already
    belong to that account, the file is created 0600 and chowned before it is
    named, and the rename happens through the same descriptor. A credential
    that appears at the right path with the wrong owner is worse than one that
    is missing, because nothing afterwards looks again.
    """
    from scripts import team_launcher as launcher

    uid = launcher.uid_for_user(owner_user)
    if uid is None:
        return f"{owner_user} is not an account on this host"
    try:
        gid = pwd.getpwnam(owner_user).pw_gid
    except KeyError:
        return f"{owner_user} is not an account on this host"
    home = home_for_user(owner_user)
    if home is None:
        return f"{owner_user} has no home directory"
    try:
        relative = path.parent.relative_to(home)
    except ValueError:
        return f"{path} is not inside {owner_user}'s home"
    fd, problem, _created = launcher._open_owned_directory_chain(home, relative.parts, uid=uid, gid=gid)
    if problem:
        return problem
    staged = f".{path.name}.{os.getpid()}.tmp"
    try:
        descriptor = os.open(
            staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        try:
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(os.dup(descriptor), "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        os.replace(staged, path.name, src_dir_fd=fd, dst_dir_fd=fd)
    except OSError as exc:
        try:
            os.unlink(staged, dir_fd=fd)
        except OSError:
            pass
        return f"{path} could not be written ({exc.strerror})"
    finally:
        os.close(fd)
    return ""
