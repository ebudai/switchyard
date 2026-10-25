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

`upgrade_project_command` calls the last two, and `connect_upstream_report`
calls them alone, with nothing else, for `switchyard upgrade --only
upstream-report` and `switchyard new --upstream-report-url` (SYRD-548).

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
from typing import TYPE_CHECKING, Any, Callable

from scripts.host_accounts import home_dir_for_user
from scripts.ticket_board.report_follow_up import bound_token, usable_binding_key

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


def _board_env_report_token(path: Path, owner_user: str = "", *, key: str = UPSTREAM_REPORT_TOKEN_KEY) -> tuple[str, str]:
    """The report token recorded in a board's own environment file.

    Reads ONLY `TICKET_BOARD_TENANT_REPORT_TOKEN` (or, for the board's own use, the one `key` named: the binding
    key a tenant's credential is derived from, SYRD-677, never copied anywhere). Everything else in that file
    is the board's business and must not be copied into a credential a tenant
    holds -- the point of this credential is that it opens `file_report` and
    nothing else.

    With `owner_user`, the file is that account's, and root reads it by
    descriptor from the account's roots: a link at it or above it -- an
    attacker's `~/.config/<slug>` pointing into another tenant's -- is refused,
    never read through (SYRD-667).
    """
    from scripts import owner_writes

    try:
        if owner_user:
            data = owner_writes.read_owner_bytes(owner_user, path)
            if data is None:
                raise FileNotFoundError(2, "No such file or directory", str(path))
            text = data.decode("utf-8")
        else:
            text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return "", f"{path} cannot be read ({exc.strerror})"
    except (SystemExit, UnicodeDecodeError) as exc:
        return "", f"{path} cannot be read ({exc})"
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() != key:
            continue
        token = value.strip().strip("\"'")
        if not token:
            return "", f"{path} records an empty {key}"
        return token, ""
    return "", f"{path} has no {key}"


def upstream_report_board(
    board_url: str,
    *,
    registry_dir: Path | None = None,
    home_for_user: Callable[[str], Path | None] = home_dir_for_user,
) -> tuple[str, Path, str]:
    """(project, its board env file, problem) for the board a tenant reports to: `upstream_report_board_owner`'s
    answer, without the owner."""
    slug, env_file, _owner, problem = upstream_report_board_owner(
        board_url, registry_dir=registry_dir, home_for_user=home_for_user)
    return slug, env_file, problem


def upstream_report_board_owner(
    board_url: str,
    *,
    registry_dir: Path | None = None,
    home_for_user: Callable[[str], Path | None] = home_dir_for_user,
) -> tuple[str, Path, str, str]:
    """(project, its board env file, its owner, problem) for the board a tenant reports to.

    Resolved from the host registry rather than configured a second time: the
    upstream board is itself a Switchyard project here, and its provision
    artifact already records the URL it serves and the account that owns it.
    Asking the registry is what keeps the tenant's configuration from carrying
    a second copy of a fact that can drift.

    As root, each registered configuration is read as the owner root's own
    records name for it, by descriptor from that owner's roots, and a board is
    the one asked for only while its configuration names that same owner: a
    tenant's configuration -- or a link it planted -- cannot make root copy
    another tenant's report token (SYRD-667).
    """
    from scripts import owner_writes
    from scripts import team_launcher as launcher

    wanted = board_url.strip().rstrip("/")
    if not wanted:
        return "", Path(), "", "no upstream report URL is configured"
    as_root = os.geteuid() == 0
    for entry in launcher._registry_project_entries(registry_dir):
        recorded = ""
        try:
            if as_root:
                recorded, problem = launcher.root_recorded_owner(entry.slug, entry.config_path, strict=False)
                if not recorded:
                    # Root's records say nothing about this configuration, or nothing usable: root reads nothing of it.
                    continue
                raw = owner_writes.read_owner_json(recorded, entry.config_path)
            else:
                raw = launcher._load_json(entry.config_path)
        except (OSError, json.JSONDecodeError, SystemExit):
            continue
        if str(raw.get("board_url") or "").strip().rstrip("/") != wanted:
            continue
        owner = str(raw.get("run_as_user") or "").strip()
        if not owner:
            return "", Path(), "", f"{entry.slug} serves {board_url} but records no owner account"
        if as_root and owner != recorded:
            return "", Path(), "", (f"{entry.slug} serves {board_url}, but its configuration names {owner!r} as its "
                                    f"owner and root provisioned it for {recorded!r}")
        home = home_for_user(owner)
        if home is None:
            return "", Path(), "", f"{entry.slug}'s owner {owner} is not an account on this host"
        return entry.slug, home / ".config" / entry.slug / "ticket-board.env", owner, ""
    return "", Path(), "", f"no project registered on this host serves {board_url}"


def record_upstream_report_link(
    config: ProjectConfig,
    *,
    config_path: Path,
    upstream_report_url: str,
    upstream_report_token_file: str = "",
    dry_run: bool = False,
    home_for_user: Callable[[str], Path | None] = home_dir_for_user,
    print_func: Callable[[str], None] = print,
    document: dict | None = None,
    written: dict | None = None,
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
        # The configuration root holds, when it holds one: after a repatriation the tenant's file may have been
        # rewritten since root wrote it, and this writes the whole document back (SYRD-665).
        raw = dict(document) if document is not None else launcher.read_owner_json(config.run_as_user or "", config_path)
    except (OSError, json.JSONDecodeError, SystemExit) as exc:
        return config, [f"{config_path} could not be read ({exc})"]
    raw["upstream_report_url"] = wanted_url
    raw["upstream_report_token_file"] = token_file
    # The token itself never goes in here: the configuration is readable by
    # every role, and `load_project_config` refuses an inline token outright.
    raw.pop("tenant_report_token", None)
    raw.pop("upstream_report_token", None)
    launcher._write_json_atomic(config_path, raw, owner_user=config.run_as_user or "")
    if written is not None:
        written["document"] = raw
    print_func(
        f"switchyard: recorded {config.project}'s upstream report board {wanted_url} and "
        f"credential {token_file}, so its next launch carries both"
    )
    return replace(
        config, upstream_report_url=wanted_url, upstream_report_token_file=token_file
    ), []


def _board_takes_bound_credentials(board_url: str, *, timeout: float = 5.0) -> bool:
    """Whether the upstream board says it reads a report credential bound to a project (SYRD-677).

    Read from the answer every board serves to anyone, which a connection already reads to know the board is
    itself (`GET /api/runtime-assignments`), and nothing else: a board that does not answer, or does not say so,
    is given its own report secret, as before, and the next refresh asks again."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(board_url.strip().rstrip("/") + "/api/runtime-assignments",
                                    timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("report_credentials") == "bound"


#: The board's own key for tenant credentials bound to a project (SYRD-677): in its environment file, read by
#: its server and by this file's refresh, and never written into any tenant's credential.
REPORT_BINDING_KEY = "TICKET_BOARD_REPORT_BINDING_KEY"


def ensure_report_binding_key(
    board_env: Path,
    board_owner: str,
    *,
    slug: str,
    dry_run: bool = False,
    home_for_user: Callable[[str], Path | None] = home_dir_for_user,
    print_func: Callable[[str], None] = print,
) -> str:
    """Give a board that takes tenant reports its binding key, once: "" or why not (SYRD-677).

    A board provisioned before SYRD-677 has none: its environment file is written once, at provision. The key
    is the board's own; its server reads it at start, and each tenant's next refresh derives that tenant's
    credential from it."""
    import secrets

    from scripts import owner_writes

    existing, _missing = _board_env_report_token(board_env, board_owner, key=REPORT_BINDING_KEY)
    secret, problem = _board_env_report_token(board_env, board_owner)
    if problem:
        return problem
    if usable_binding_key(secret, existing):
        print_func(f"switchyard: {slug}'s board already holds its key for project-bound report credentials")
        return ""
    try:
        data = owner_writes.read_owner_bytes(board_owner, board_env)
        text = (data or b"").decode("utf-8")
    except (OSError, SystemExit, UnicodeDecodeError) as exc:
        return f"{board_env} cannot be read ({exc})"
    kept = [line for line in text.splitlines() if line.partition("=")[0].strip() != REPORT_BINDING_KEY]
    if dry_run:
        print_func(f"switchyard: would give {slug}'s board a key for project-bound report credentials in "
                   f"{board_env}; nothing written")
        return ""
    written = _write_owner_private_file(board_env, "\n".join([*kept, f"{REPORT_BINDING_KEY}={secrets.token_urlsafe(32)}"])
                                        + "\n", owner_user=board_owner, home_for_user=home_for_user)
    if written:
        return written
    print_func(f"switchyard: gave {slug}'s board a key for project-bound report credentials in {board_env}; it "
               f"takes effect when {slug}'s board next starts, and each tenant's next upgrade then binds its credential")
    return ""


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
    # Whether the upstream board reads a credential bound to a project: asked of it over HTTP (SYRD-677). A
    # test answers for it, so no case asks a live board.
    takes_bound: Callable[[str], bool] | None = None,
) -> list[str]:
    """Give this tenant the report-only credential the upstream board accepts now, bound to this project.

    The board compares a submitted report token against one configured string,
    so rotating it -- which a cutover does, because the token is minted when
    the board's env file is first written -- silently strands every credential
    handed out before. MEFP's copy was from 2026-08-31 and its Director could
    not file a report at all (SYRD-238).

    Nothing wrote this credential before: it was an operator-supplied path, so
    there was nothing to refresh. Ownership of it starts here.

    Report-only by construction: the only value read is the report token, and
    the only things the credential opens are `file_report`, which the board
    forces to `analysis`/`unassigned`, and a follow-up on a report this project
    filed. No write token is read, and none is written.

    Bound to this project (SYRD-677): the tenant is given a credential derived
    from the board's binding key and its own project -- not the report secret,
    which every tenant held before, and never the key -- so the board reads the
    reporting project from the credential and no tenant can speak for another.
    A credential handed out before this is replaced by the next refresh.
    """
    if not str(config.upstream_report_url or "").strip():
        return []
    owner = str(config.run_as_user or "").strip()
    if not owner:
        return [f"{config.project} reports upstream but records no owner account"]
    destination = upstream_report_credential_path(config, home_for_user=home_for_user)
    if destination is None:
        return [f"{config.project}'s upstream report credential path cannot be resolved"]
    upstream, board_env, board_owner, problem = upstream_report_board_owner(
        config.upstream_report_url, registry_dir=registry_dir, home_for_user=home_for_user
    )
    if problem:
        return [f"{config.project} cannot be given a report credential: {problem}"]
    token, problem = _board_env_report_token(board_env, board_owner)
    if problem:
        return [f"{config.project} cannot be given {upstream}'s report credential: {problem}"]

    # Only for a board that reads it: one older than SYRD-677 accepts nothing but its own secret.
    bound = (takes_bound or _board_takes_bound_credentials)(config.upstream_report_url)
    if bound:
        binding_key, _missing = _board_env_report_token(board_env, board_owner, key=REPORT_BINDING_KEY)
        bound = bool(usable_binding_key(token, binding_key))  # a key this cannot read: the secret, as before
    if bound:
        token = bound_token(binding_key, config.project)
    desired = f"{UPSTREAM_REPORT_TOKEN_KEY}={token}\n"
    current, _ = _board_env_report_token(destination, owner)
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
        f"switchyard: gave {config.project} {upstream}'s current report credential"
        f"{', bound to ' + config.project if bound else ''} in "
        f"{destination}, so its panes can file reports without being handed a token"
    )
    return []


def connect_upstream_report(
    config: ProjectConfig,
    *,
    config_path: Path,
    upstream_report_url: str = "",
    upstream_report_token_file: str = "",
    dry_run: bool = False,
    registry_dir: Path | None = None,
    home_for_user: Callable[[str], Path | None] = home_dir_for_user,
    print_func: Callable[[str], None] = print,
) -> tuple[ProjectConfig, list[str]]:
    """Connect a tenant to the board it reports to, and do nothing else (SYRD-548).

    The link in its configuration (which is what puts `TICKET_BOARD_REPORT_URL`
    and the credential's path in every pane's environment) and the report-only
    credential, 0600 and the tenant's own. No release is resolved, no ownership
    repaired, no workflow, identity, account or pane touched: getting report
    access used to cost a full upgrade, and otto's could not even complete its
    release phase.

    Everything that can refuse is asked first -- which board serves the URL, and
    whether its environment holds a report token -- so a refusal writes nothing.
    With no URL given, the one the configuration already records is reconnected,
    which is how a rotated token is fetched again.
    """
    from scripts import team_launcher as launcher

    url = upstream_report_url.strip() or str(config.upstream_report_url or "").strip()
    if not url:
        return config, [
            f"{config.project} records no upstream report board; name it with --upstream-report-url"
        ]
    upstream, board_env, board_owner, problem = launcher.upstream_report_board_owner(
        url, registry_dir=registry_dir, home_for_user=home_for_user
    )
    if problem:
        return config, [f"{config.project} cannot be connected for reports: {problem}"]
    if upstream == config.project:
        return config, [f"{config.project} cannot be connected for reports: {url} is its own board"]
    _token, problem = _board_env_report_token(board_env, board_owner)
    if problem:
        return config, [f"{config.project} cannot be given {upstream}'s report credential: {problem}"]
    # A board that does not answer as itself is not connected to, whatever the records say (SYRD-674).
    from scripts import upstream_report_sinks

    problem, answered = upstream_report_sinks.board_reading(url, upstream)
    if problem:
        return config, [f"{config.project} cannot be connected for reports: {problem}"]
    linked, problems = launcher.record_upstream_report_link(
        config,
        config_path=config_path,
        upstream_report_url=url,
        upstream_report_token_file=upstream_report_token_file,
        dry_run=dry_run,
        home_for_user=home_for_user,
        print_func=print_func,
    )
    if problems:
        return config, problems
    if dry_run:
        # Nothing was recorded, so ask about the credential the link would name.
        token_file = upstream_report_token_file.strip()
        if url != str(config.upstream_report_url or "").strip() or token_file:
            linked = replace(config, upstream_report_url=url, upstream_report_token_file=token_file)
    return linked, launcher.refresh_upstream_report_credential(
        linked, dry_run=dry_run, registry_dir=registry_dir, home_for_user=home_for_user, print_func=print_func,
        # The board just said what it reads: it is not asked again (SYRD-677).
        takes_bound=lambda _url: answered.get("report_credentials") == "bound",
    )


def connect_upstream_report_command(
    only: str,
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool,
    registry_dir: Path | None,
    upstream_report_url: str,
    upstream_report_token_file: str,
    print_func: Callable[[str], None],
    upstream_report_board: str = "",
    no_upstream_report: bool = False,
    runner: Callable[..., Any] | None = None,
) -> int:
    """`switchyard upgrade <project> --only upstream-report`: the report connection and nothing else (SYRD-548).

    `upgrade_project_command` returns this before the source is pinned, so no release is resolved or repaired
    and no phase -- artifacts, tooling, identities, workflow, panes -- runs.
    Otto's Director could not get report access without an upgrade it had chosen
    not to run, and its dry run could not even finish the release phase.

    No URL is needed (SYRD-674): the board is the one named, the one recorded, or the host's one declared
    report board, and two are never guessed between. What became of it is recorded in the tenant's
    registry record, and a link written while panes run says which of them need a relaunch.
    `--only accept-reports` / `refuse-reports` is root's declaration that this project's board takes the
    host's tenant reports.
    """
    from scripts import team_launcher as launcher
    from scripts import upstream_report_sinks as sinks

    if only == "workflow-projection":
        # The tenant's workflow projection, given back as `switchyard new` would have, and nothing else (SYRD-676).
        from scripts import projection_ownership

        return projection_ownership.restore_command(config, config_path=config_path, dry_run=dry_run,
                                                    print_func=print_func)
    if only in ("accept-reports", "refuse-reports"):
        return sinks.declare_report_board(config, accept=only == "accept-reports", dry_run=dry_run,
                                          registry_dir=registry_dir, print_func=print_func)
    if only != "upstream-report":
        print_func(f"switchyard: --only takes upstream-report, accept-reports, refuse-reports or workflow-projection, "
                   f"not {only!r}. Nothing was changed.")
        return 2
    if no_upstream_report:
        configured = str(config.upstream_report_url or "").strip()
        if upstream_report_url.strip() or upstream_report_board.strip() or configured:
            print_func(f"switchyard: --no-upstream-report records that {config.project} files no reports; "
                       + (f"it files them to {configured}, and removing that link is not this step's"
                          if configured else "it cannot be combined with a report board") + ". Nothing was changed.")
            return 2 if not configured else 1
        if dry_run:
            print_func(f"switchyard: would record that {config.project} opted out of Switchyard reports; "
                       "nothing written")
            return 0
        sinks.record_report_outcome(config.project, sinks.OPTED_OUT, registry_dir=registry_dir, print_func=print_func)
        print_func(f"switchyard: {config.project} files no Switchyard reports, as its operator chose; connect it "
                   f"at any time with `{sinks.repair_command(config.project)}`.")
        return 0
    url, problem = sinks.resolve_repair_board(
        config, upstream_report_url=upstream_report_url, upstream_report_board=upstream_report_board,
        registry_dir=registry_dir,
    )
    problems = [f"{config.project} cannot be connected for reports: {problem}"] if problem else []
    connected = config
    if url:
        connected, problems = launcher.connect_upstream_report(
            config,
            config_path=config_path,
            upstream_report_url=url,
            upstream_report_token_file=upstream_report_token_file,
            dry_run=dry_run,
            registry_dir=registry_dir,
            print_func=print_func,
        )
    for problem in problems:
        print_func(f"switchyard: {problem}")
    if problems:
        # A refusal writes nothing, its record included: what was recorded before still stands.
        print_func(f"switchyard: {config.project} was not connected for reports, and no upgrade phase ran.")
        return 1
    if not dry_run:
        board = next((b.slug for b in sinks.report_boards(exclude=config.project, registry_dir=registry_dir)
                      if b.url == url.rstrip("/")), "")
        if sinks.recorded_outcome(config.project, registry_dir=registry_dir).get("state") == sinks.OPTED_OUT:
            print_func(f"switchyard: {config.project} had opted out of Switchyard reports; this connection replaces "
                       "that record")
        sinks.record_report_outcome(config.project, sinks.CONNECTED, board=board, url=url,
                                    registry_dir=registry_dir, print_func=print_func)
    print_func(
        f"switchyard: {config.project}'s report connection is {'checked' if dry_run else 'in place'}; no upgrade "
        "phase ran. Each pane carries it from its next launch."
    )
    linked = (str(connected.upstream_report_url or ""), str(connected.upstream_report_token_file or ""))
    if not dry_run and linked != (str(config.upstream_report_url or ""), str(config.upstream_report_token_file or "")):
        print_func(sinks.running_panes_note(connected, url=linked[0], token_file=linked[1], runner=runner))
    return 0


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
