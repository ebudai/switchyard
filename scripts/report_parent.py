"""Root's half of report ancestry: declaring a project's report parent, and its ancestor credentials (SYRD-721).

`ticket_board.report_ancestry` reads a project's ancestors from root's registry
records; this module is what writes them, and only root runs it, through
`switchyard upgrade <slug> --only upstream-report` (to set or change the parent,
`--upstream-report-board <slug>`; to remove it, `--no-upstream-report`), through
`switchyard new`, and through every full upgrade, which migrates a project's
one upstream connection into its declared parent.

* **Checked before anything is written.** The proposed parent must be another
  registered project whose board answers as itself, and the chain it gives --
  that parent's own ancestors, read from root's records -- must have no cycle, no
  board or ticket prefix twice, and every board in it must answer as the
  project root recorded. A refusal writes nothing.
* **Recorded where no tenant writes.** `report_parent` in the project's
  root-owned registry record, with the board and ticket prefix verified, and
  every change appended to `report_parent_history` (from, to, when, who). The
  same declaration again changes nothing.
* **One credential per ancestor.** For each board in the chain, root writes the
  project a credential of that board's own -- bound to the project where the
  board reads bound credentials (SYRD-677) -- to
  `report-credentials/<ancestor>.env` beside the project's immediate-parent
  credential, owner-only, and notes in the registry record that it did, never
  the token. A board's token is never written into another board's file.
* **Removing or changing a parent erases nothing.** Credentials already issued
  and the reporting project's receipts stay where they are, so a report filed
  before the change can still be followed up while its board accepts that
  credential.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from scripts.host_accounts import home_dir_for_user
from scripts.ticket_board import report_ancestry as ra

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def _registry(registry_dir: Path | None) -> Path:
    from scripts import team_launcher as launcher

    return registry_dir or launcher.switchyard_registry_dir()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _operator() -> str:
    from scripts.ticket_board.rollout_journal import resolve_operator

    return str(getattr(resolve_operator(), "name", "") or "")


def _update_record(slug: str, change: Callable[[dict[str, Any]], bool], registry_dir: Path | None) -> str:
    """Apply `change` to `slug`'s registry record and write it when it says it changed. "" or why not."""
    from scripts.board_ui_record import _write_atomically

    path = _registry(registry_dir) / f"{slug}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"{path} cannot be read ({exc})"
    if not isinstance(payload, dict) or payload.get("schema") != ra.REGISTRY_SCHEMA:
        return f"{path} is not a registry record"
    if not change(payload):
        return ""
    try:
        _write_atomically(path, payload)
    except OSError as exc:
        return f"{path} cannot be written ({exc})"
    return ""


def names(links: list[ra.Link]) -> str:
    return " -> ".join(f"{link.name} ({link.project})" for link in links) or "none"


def proposed_chain(
    project: str,
    url: str,
    *,
    registry_dir: Path | None = None,
    ask: Callable[[str], tuple[dict, str]] | None = None,
    parent_verified: bool = False,
    answered: dict | None = None,
) -> tuple[list[ra.Link], str]:
    """The ancestors `project` would have with the board at `url` as its parent, each board verified live, or why
    that parent cannot be declared. Nothing is written. `parent_verified`: the connection, which asks the parent
    itself before it writes anything (SYRD-674), has asked or will ask, so it is not asked twice; `answered` is what
    that board said, which names its ticket prefix (a board older than SYRD-721 names none, and is checked at use)."""
    from scripts import upstream_report_sinks as sinks

    wanted = url.strip().rstrip("/")
    board = next((b for b in sinks.report_boards(exclude=project, registry_dir=registry_dir) if b.url == wanted), None)
    if board is None:
        return [], f"no project registered on this host serves {wanted}, other than {project} itself"
    # Root's own check that a board is itself (SYRD-674); the same answer names its ticket prefix.
    if answered is None and not parent_verified:
        problem, answered = sinks.board_reading(wanted, board.slug)
        if problem:
            return [], f"{board.slug}'s board cannot be verified: {problem}"
    first = ra.Link(board.slug, board.name, wanted, str((answered or {}).get("ticket_prefix") or ""), "declared")
    links, problem = ra.chain(project, _registry(registry_dir), first=first)
    if problem:
        return links, problem
    for link in links[1:]:
        identity = ra.verify(link, ask=ask)
        if not identity.verified:
            return links, f"an ancestor's board cannot be verified: {identity.problem}"
    return links, ""


def record_parent(
    project: str,
    link: ra.Link | None,
    *,
    registry_dir: Path | None = None,
    dry_run: bool = False,
    reason: str = "",
    print_func: Callable[[str], None] = print,
) -> str:
    """Declare `link` as `project`'s report parent (None: no parent), with a history entry. "" or why not."""
    value = {"project": link.project, "url": link.url, "ticket_prefix": link.ticket_prefix} if link else {"project": ""}
    record = ra.registry_record(project, _registry(registry_dir))
    held = record.get(ra.PARENT_KEY)
    if isinstance(held, dict) and {k: held.get(k, "") for k in value} == value and set(held) - {
            "recorded_at", "recorded_by"} <= set(value):
        print_func(f"switchyard: {project}'s report parent is already {link.name if link else 'none'}; nothing changed")
        return ""
    before = str(held.get("project") or "") if isinstance(held, dict) else ""
    said = f"{link.name} ({link.project})" if link else "none"
    if dry_run:
        print_func(f"switchyard: would record {project}'s report parent as {said}"
                   f"{' (was ' + before + ')' if before else ''}; nothing written")
        return ""
    at, by = _now(), _operator()

    def change(payload: dict[str, Any]) -> bool:
        payload[ra.PARENT_KEY] = {**value, "recorded_at": at, "recorded_by": by}
        history = payload.get(ra.HISTORY_KEY) if isinstance(payload.get(ra.HISTORY_KEY), list) else []
        payload[ra.HISTORY_KEY] = [*history, {"from": before, "to": value["project"], "at": at, "by": by,
                                              "reason": reason}]
        return True

    problem = _update_record(project, change, registry_dir)
    if not problem:
        print_func(f"switchyard: recorded {project}'s report parent as {said}"
                   f"{' (was ' + before + ')' if before else ''}, in root's registry record with its history")
    return problem


def issue_credentials(
    config: ProjectConfig,
    links: list[ra.Link],
    *,
    registry_dir: Path | None = None,
    home_for_user: Callable[[str], Path | None] = home_dir_for_user,
    takes_bound: Callable[[str], bool] | None = None,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Give `config`'s project a report-only credential of each ancestor's own board, and note which in root's
    records. A board that cannot be read is said and skipped: the others are still issued."""
    from scripts import upstream_report as ur
    from scripts.ticket_board.report_follow_up import bound_token, usable_binding_key

    owner = str(config.run_as_user or "").strip()
    base = ur.upstream_report_credential_path(config, home_for_user=home_for_user)
    if not owner or base is None:
        return [f"{config.project}'s ancestor credentials have nowhere to go (no owner account or credential path)"]
    problems, issued = [], {}
    for link in links:
        destination = base.parent / ra.CREDENTIAL_DIR_NAME / f"{link.project}.env"
        upstream, board_env, board_owner, problem = ur.upstream_report_board_owner(
            link.url, registry_dir=registry_dir, home_for_user=home_for_user)
        if not problem and upstream != link.project:
            problem = f"{link.url} is {upstream}'s board on this host, not {link.project}'s"
        token, problem = ("", problem) if problem else ur._board_env_report_token(board_env, board_owner)
        if problem:
            problems.append(f"{config.project} was not given {link.project}'s report credential: {problem}")
            continue
        bound = (takes_bound or ur._board_takes_bound_credentials)(link.url)
        if bound:
            key, _missing = ur._board_env_report_token(board_env, board_owner, key=ur.REPORT_BINDING_KEY)
            bound = bool(usable_binding_key(token, key))
            token = bound_token(key, config.project) if bound else token
        current, _ = ur._board_env_report_token(destination, owner)
        if current == token and ur._credential_is_private(destination, owner):
            print_func(f"switchyard: {config.project} already holds {link.project}'s current report credential")
            # A run that wrote the file but not root's note (Audit of 52f543e1) is repaired here, the token untouched;
            # a dry run says it would be (Audit of d92895d6).
            note = (ra.registry_record(config.project, _registry(registry_dir)).get(ra.CREDENTIALS_KEY) or {})
            held = note.get(link.project) if isinstance(note, dict) else None
            if not (isinstance(held, dict) and held.get("issued_at") and held.get("bound") == bound):
                if dry_run:
                    print_func(f"switchyard: would repair root's record that it issued {config.project} "
                               f"{link.project}'s credential ({'out of date' if held else 'missing'}), leaving the "
                               "credential as it is; nothing written")
                else:
                    issued[link.project] = {"issued_at": _now(), "bound": bound}
            continue
        if dry_run:
            print_func(f"switchyard: would give {config.project} {link.project}'s own report credential in "
                       f"{destination}, 0600, and record in root's records that it did; nothing written")
            continue
        written = ur._write_owner_private_file(destination, f"{ur.UPSTREAM_REPORT_TOKEN_KEY}={token}\n",
                                               owner_user=owner, home_for_user=home_for_user)
        if written:
            problems.append(f"{config.project}'s credential for {link.project} was not written: {written}")
            continue
        issued[link.project] = {"issued_at": _now(), "bound": bound}
        print_func(f"switchyard: gave {config.project} {link.project}'s own report credential"
                   f"{', bound to ' + config.project if bound else ''} in {destination}")
    if issued:

        def change(payload: dict[str, Any]) -> bool:
            held = payload.get(ra.CREDENTIALS_KEY) if isinstance(payload.get(ra.CREDENTIALS_KEY), dict) else {}
            payload[ra.CREDENTIALS_KEY] = {**held, **issued}
            return True

        problem = _update_record(config.project, change, registry_dir)
        if problem:
            problems.append(f"root's note of {config.project}'s ancestor credentials was not kept: {problem}")
    return problems


def settle(
    config: ProjectConfig,
    url: str,
    *,
    registry_dir: Path | None = None,
    home_for_user: Callable[[str], Path | None] = home_dir_for_user,
    ask: Callable[[str], tuple[dict, str]] | None = None,
    takes_bound: Callable[[str], bool] | None = None,
    dry_run: bool = False,
    reason: str = "",
    links: list[ra.Link] | None = None,
    answered: dict | None = None,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Once the project is connected to the board at `url` (which verified it): that board declared its parent, and
    every ancestor's credential issued. `links` is the chain already checked before connecting."""
    problem = ""
    if links is None:
        # The connection hands over what the board said of itself; without it, the board is asked here.
        links, problem = proposed_chain(config.project, url, registry_dir=registry_dir, ask=ask, answered=answered)
    if problem:
        return [f"{config.project}'s report parent was not recorded: {problem}"]
    print_func(f"switchyard: {config.project}'s report ancestors, nearest first: {names(links)}")
    problem = record_parent(config.project, links[0], registry_dir=registry_dir, dry_run=dry_run, reason=reason,
                            print_func=print_func)
    if problem:
        return [f"{config.project}'s report parent was not recorded: {problem}"]
    return issue_credentials(config, links, registry_dir=registry_dir, home_for_user=home_for_user,
                             takes_bound=takes_bound, dry_run=dry_run, print_func=print_func)


def migrate(
    config: ProjectConfig,
    *,
    registry_dir: Path | None = None,
    home_for_user: Callable[[str], Path | None] = home_dir_for_user,
    ask: Callable[[str], tuple[dict, str]] | None = None,
    takes_bound: Callable[[str], bool] | None = None,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """A full upgrade's step: a project connected before ancestry existed gets that board declared as its parent,
    and every project with ancestors gets each ancestor's credential. A project with no parent is left alone."""
    directory = _registry(registry_dir)
    record = ra.registry_record(config.project, directory)
    if not record:
        return []
    if ra.PARENT_KEY not in record:
        link, _problem = ra.immediate_parent(config.project, directory)
        if link is None:
            return []
        links, problem = proposed_chain(config.project, link.url, registry_dir=registry_dir, ask=ask)
        if problem:
            return [f"{config.project}'s one upstream connection was not declared its report parent: {problem}"]
        return settle(config, link.url, registry_dir=registry_dir, home_for_user=home_for_user, ask=ask,
                      takes_bound=takes_bound, dry_run=dry_run, print_func=print_func, links=links,
                      reason="migrated from its one upstream connection")
    links, problem = ra.chain(config.project, directory)
    if problem:
        return [f"{config.project}'s report ancestry cannot be used: {problem}"]
    return issue_credentials(config, links, registry_dir=registry_dir, home_for_user=home_for_user,
                             takes_bound=takes_bound, dry_run=dry_run, print_func=print_func) if links else []


def remove(
    config: ProjectConfig,
    *,
    config_path: Path,
    registry_dir: Path | None = None,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """No report parent: declared none, and the link removed from the project's configuration so no pane files to
    it. Credentials and receipts are kept, for follow-ups on reports already filed."""
    from scripts import team_launcher as launcher

    problem = record_parent(config.project, None, registry_dir=registry_dir, dry_run=dry_run,
                            reason="removed by the operator", print_func=print_func)
    if problem or dry_run:
        return [problem] if problem else []
    try:
        raw = launcher.read_owner_json(config.run_as_user or "", config_path)
    except (OSError, ValueError, SystemExit) as exc:
        return [f"{config_path} could not be read ({exc})"]
    removed = [raw.pop(key) for key in ("upstream_report_url", "upstream_report_token_file") if key in raw]
    if removed:
        launcher._write_json_atomic(config_path, raw, owner_user=config.run_as_user or "")
    print_func(f"switchyard: {config.project} has no report parent now; its panes file no reports from their next "
               "launch. Credentials already issued and its receipts are kept, so reports it filed can still be "
               "followed up.")
    return []
