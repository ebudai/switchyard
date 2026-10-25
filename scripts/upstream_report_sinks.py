"""Which board a tenant files Switchyard reports to, decided before the tenant exists (SYRD-674).

`pie` was created with no report link and nothing said so until its Director tried to file one. A board
that accepts reports cannot be recognised by its report token: every board's provisioning mints one, and
all six boards registered on that host held one. Which board takes the host's tenant reports is root's
decision instead, kept in that board's own root-owned registry record (`tenant_reports`, set by
`switchyard upgrade <slug> --only accept-reports`); nothing a tenant writes can make a board the default.

- `report_boards` lists the registered boards a tenant could report to, read as root's records name them;
- `choose_report_board` is `switchyard new`'s decision, made before anything is created: the one declared
  board, an operator's explicit choice, an explicit opt-out, or -- when no board is registered at all --
  none available. Two candidates are never chosen between silently;
- `record_report_decision` keeps that decision in root's provisioning directory, so `resume-provision`
  honours it after an interrupted `new`;
- `record_report_outcome` writes what became of it into the tenant's root-owned registry record
  (`upstream_report`: connected, failed, opted-out or unavailable), which status and later upgrades read;
- `board_answer` asks whether the board answers at its URL as itself, so an unreachable board is never
  reported connected;
- `resolve_repair_board` is the board `switchyard upgrade <p> --only upstream-report` connects to when no
  URL is given;
- `running_panes_note` says whether panes already running need a relaunch to carry a new link;
- `report_status_lines` is what `switchyard status` says about each project's reports.

No report token passes through here: a token is only ever checked for (`_board_env_report_token`) and copied
by `upstream_report.refresh_upstream_report_credential`, into the tenant's owner-only credential.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig

#: A board's own registry record: root declared that it takes reports from the host's other tenants.
SINK_KEY = "tenant_reports"
#: A tenant's own registry record: what became of its report connection.
OUTCOME_KEY = "upstream_report"
#: Root's copy of `switchyard new`'s decision, beside its plan.
DECISION_NAME = "upstream-report.json"
DECISION_SCHEMA = "switchyard.upstream-report-decision.v1"
REGISTRY_SCHEMA = "switchyard.project-registry.v1"

CONNECT = "connect"
CONNECTED = "connected"
FAILED = "failed"
OPTED_OUT = "opted-out"
UNAVAILABLE = "unavailable"

PROBE_SECONDS = 3.0


@dataclass(frozen=True)
class ReportBoard:
    """A registered board a tenant could file reports to."""

    slug: str
    name: str
    url: str
    declared: bool
    #: Why it cannot be one, from root's records: unreadable, or no board URL.
    problem: str = ""


@dataclass(frozen=True)
class ReportChoice:
    """`switchyard new`'s decision: connect to `board`, or why not (opted-out, unavailable)."""

    state: str
    board: str = ""
    url: str = ""
    #: How it was decided, as the summary says it.
    source: str = ""


def _registry_record(slug: str, registry_dir: Path | None) -> dict[str, Any]:
    from scripts import team_launcher as launcher

    path = (registry_dir or launcher.switchyard_registry_dir()) / f"{slug}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) and raw.get("schema") == REGISTRY_SCHEMA else {}


def is_declared(record: dict[str, Any]) -> bool:
    held = record.get(SINK_KEY)
    return isinstance(held, dict) and held.get("accepted") is True


def report_boards(*, exclude: str = "", registry_dir: Path | None = None) -> list[ReportBoard]:
    """Every registered board but `exclude`'s, with the URL root's records give it.

    As root a configuration is read as the owner root provisioned it for, by descriptor, exactly as
    `upstream_report_board_owner` reads it, so a tenant's link cannot change what is listed (SYRD-667).
    """
    from scripts import owner_writes
    from scripts import team_launcher as launcher

    as_root = os.geteuid() == 0
    boards: list[ReportBoard] = []
    for entry in launcher._registry_project_entries(registry_dir):
        if entry.slug == exclude:
            continue
        declared = is_declared(_registry_record(entry.slug, registry_dir))
        url, problem = "", ""
        try:
            if as_root:
                recorded, why = launcher.root_recorded_owner(entry.slug, entry.config_path, strict=False)
                raw = owner_writes.read_owner_json(recorded, entry.config_path) if recorded else None
                if raw is None:
                    problem = why or "root's records name no owner for its configuration"
            else:
                raw = launcher._load_json(entry.config_path)
        except (OSError, json.JSONDecodeError, SystemExit) as exc:
            raw, problem = None, f"its configuration cannot be read ({exc})"
        if isinstance(raw, dict):
            url = str(raw.get("board_url") or "").strip().rstrip("/")
            if not url:
                problem = "its configuration records no board URL"
        boards.append(ReportBoard(slug=entry.slug, name=entry.name, url=url, declared=declared, problem=problem))
    return boards


def board_problem(
    board: ReportBoard,
    *,
    registry_dir: Path | None = None,
    home_for_user: Callable[[str], Path | None] | None = None,
) -> str:
    """Why `board` cannot take a tenant's reports, or "": a URL root can resolve, and a report token in its
    environment. The token is only looked for, never kept."""
    from scripts import upstream_report

    if board.problem:
        return board.problem
    # Whose home holds the board's environment is `upstream_report`'s to resolve, by its own default unless one
    # is given, so a suite that sandboxes it there sandboxes this too.
    _slug, env_file, owner, problem = upstream_report.upstream_report_board_owner(
        board.url, registry_dir=registry_dir, **({"home_for_user": home_for_user} if home_for_user else {}))
    if problem:
        return problem
    _token, problem = upstream_report._board_env_report_token(env_file, owner)
    return problem


def declared_default(
    boards: Sequence[ReportBoard],
    *,
    registry_dir: Path | None = None,
    home_for_user: Callable[[str], Path | None] | None = None,
) -> tuple[ReportBoard | None, str]:
    """(the host's one declared report board, "") or (None, why there is no default)."""
    declared = [board for board in boards if board.declared]
    if not declared:
        return None, "no board on this host is declared to take tenant reports"
    if len(declared) > 1:
        named = ", ".join(board.slug for board in declared)
        return None, f"{len(declared)} boards are declared to take tenant reports ({named})"
    problem = board_problem(declared[0], registry_dir=registry_dir, home_for_user=home_for_user)
    if problem:
        return None, f"{declared[0].slug} is declared to take tenant reports, but {problem}"
    return declared[0], ""


def repair_command(slug: str, *, board: str = "") -> str:
    named = f" --upstream-report-board {board}" if board else ""
    return f"sudo switchyard upgrade {slug} --only upstream-report{named}"


def repair_hint(slug: str, *, registry_dir: Path | None = None) -> str:
    """The command that connects `slug`, as it can be run on this host now: plain when the host's declared
    board or a recorded one resolves it, otherwise with the boards it may name."""
    boards = report_boards(exclude=slug, registry_dir=registry_dir)
    default, _why = declared_default(boards, registry_dir=registry_dir)
    if default is not None or not boards:
        return f"`{repair_command(slug)}`"
    return f"`{repair_command(slug, board='<slug>')}`, naming one of: {', '.join(b.slug for b in boards)}"


def unconnected_warning(slug: str, why: str, *, repair: str) -> str:
    return (f"switchyard: WARNING: {slug} will not be able to file Switchyard reports ({why}): its Director's "
            f"`file-report` refuses until it is connected. Connect it at any time with {repair}.")


def _listing(boards: Sequence[ReportBoard]) -> list[str]:
    return [f"{board.slug} ({board.name}) at {board.url or '?'}" + (" -- declared" if board.declared else "")
            + (f" -- {board.problem}" if board.problem else "") for board in boards]


def choose_report_board(
    slug: str,
    *,
    upstream_report_url: str = "",
    upstream_report_board: str = "",
    no_upstream_report: bool = False,
    yes: bool = False,
    registry_dir: Path | None = None,
    home_for_user: Callable[[str], Path | None] | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> ReportChoice:
    """Where `slug` will file reports, decided before anything of it exists. A refusal raises SystemExit.

    Named (`--upstream-report-url`, `--upstream-report-board`): it must be a registered board other than
    `slug`. `--no-upstream-report`: an explicit opt-out. Otherwise the one board root declared; with no
    other board registered, none is available. Anything else is the operator's to choose: asked when
    there is somebody to ask, refused under --yes.
    """
    url = upstream_report_url.strip().rstrip("/")
    named = upstream_report_board.strip()
    if no_upstream_report and (url or named):
        raise SystemExit("switchyard: --no-upstream-report cannot be combined with a report board. "
                         "Nothing was created.")
    if no_upstream_report:
        return ReportChoice(OPTED_OUT, source="--no-upstream-report")
    boards = report_boards(exclude=slug, registry_dir=registry_dir)
    if url or named:
        if named == slug:
            raise SystemExit(f"switchyard: {slug} cannot file reports to its own board. Nothing was created.")
        matches = [b for b in boards if (not named or b.slug == named) and (not url or b.url == url)]
        if not matches:
            what = " and ".join(part for part in (f"--upstream-report-board {named}" if named else "",
                                                   f"--upstream-report-url {url}" if url else "") if part)
            listed = "; ".join(_listing(boards)) or "none"
            raise SystemExit(f"switchyard: {what} names no board registered on this host (registered: {listed}). "
                             "Nothing was created.")
        board = matches[0]
        problem = board_problem(board, registry_dir=registry_dir, home_for_user=home_for_user)
        if problem:
            raise SystemExit(f"switchyard: {slug} cannot file reports to {board.slug}: {problem}. Nothing was created.")
        return ReportChoice(CONNECT, board=board.slug, url=board.url,
                            source="--upstream-report-board" if named else "--upstream-report-url")
    default, why = declared_default(boards, registry_dir=registry_dir, home_for_user=home_for_user)
    if default is not None:
        return ReportChoice(CONNECT, board=default.slug, url=default.url,
                            source="the host's declared report board")
    if not boards:
        return ReportChoice(UNAVAILABLE, source="no other board is registered on this host")
    listing = _listing(boards)
    if yes:
        raise SystemExit(
            f"switchyard: {slug} needs a decision about Switchyard reports before it is created: {why}. "
            f"Registered boards: {'; '.join(listing)}. Re-run with --upstream-report-board <slug>, or with "
            "--no-upstream-report to create it unable to file reports. To make one board every new tenant's "
            "default: `sudo switchyard upgrade <slug> --only accept-reports`. Nothing was created."
        )
    from scripts import team_launcher as launcher

    print_func(f"switchyard: where should {slug} file Switchyard reports? {why[0].upper() + why[1:]}.")
    for number, line in enumerate(listing, 1):
        print_func(f"switchyard:   {number}. {line}")
    print_func(f"switchyard:   none. create {slug} unable to file reports")
    for _attempt in range(launcher.SWITCHYARD_PROMPT_MAX_ATTEMPTS):
        answer = launcher._read_prompt(f"Report board [1-{len(boards)} or none]: ", input_func=input_func).strip()
        if answer.lower() == "none":
            return ReportChoice(OPTED_OUT, source="the operator's answer")
        board = next((b for n, b in enumerate(boards, 1) if answer in (str(n), b.slug)), None)
        if board is None:
            print_func(f"switchyard: answer a number from 1 to {len(boards)}, a slug, or none")
            continue
        problem = board_problem(board, registry_dir=registry_dir, home_for_user=home_for_user)
        if problem:
            print_func(f"switchyard: {board.slug} cannot take {slug}'s reports: {problem}")
            continue
        return ReportChoice(CONNECT, board=board.slug, url=board.url, source="the operator's answer")
    raise SystemExit(f"switchyard: no report board was chosen for {slug}. Nothing was created.")


def choice_summary(slug: str, choice: ReportChoice, *, registry_dir: Path | None = None) -> str:
    """The line `switchyard new` says before its confirmation, and again when it is done."""
    if choice.state == CONNECT:
        return (f"switchyard: Switchyard reports: {slug} files them to {choice.board} at {choice.url} "
                f"({choice.source}), connected before any pane starts")
    repair = repair_hint(slug, registry_dir=registry_dir)
    why = "opted out with --no-upstream-report" if choice.source == "--no-upstream-report" else (
        "the operator chose none" if choice.state == OPTED_OUT else choice.source)
    return unconnected_warning(slug, why, repair=repair)


# ------------------------------------------------------------------ root's records


def decision_path(slug: str) -> Path:
    from scripts import team_launcher as launcher

    return launcher.privileged_baseline_plan_path(slug).with_name(DECISION_NAME)


def record_report_decision(slug: str, choice: ReportChoice, *, print_func: Callable[[str], None] = print) -> None:
    """Root's copy of `new`'s decision, root-only beside its plan, for `resume-provision` to honour."""
    from scripts import team_launcher as launcher

    if os.geteuid() != 0:
        return
    path = decision_path(slug)
    payload = {"schema": DECISION_SCHEMA, "project": slug, "state": choice.state, "board": choice.board,
               "url": choice.url, "source": choice.source}
    try:
        launcher.ensure_privileged_provision_dir(path.parent)
        staged = path.with_name(f".{path.name}.new")
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            os.fchmod(descriptor, launcher.privileged_artifact_mode(path.name))
        finally:
            os.close(descriptor)
        staged.replace(path)
    except (OSError, SystemExit) as exc:
        print_func(f"switchyard: {slug}'s report decision was not recorded for a resumed provision: {exc}")


def recorded_report_decision(slug: str) -> ReportChoice | None:
    """`new`'s decision as root recorded it, or None: no record, or one root does not own."""
    from scripts import team_launcher as launcher

    read, _problem = launcher.read_plan_no_follow(decision_path(slug), require_root_owned=True)
    document = read.data if read is not None else {}
    if document.get("schema") != DECISION_SCHEMA or document.get("project") != slug:
        return None
    state = str(document.get("state") or "")
    if state not in (CONNECT, OPTED_OUT, UNAVAILABLE):
        return None
    return ReportChoice(state, board=str(document.get("board") or ""), url=str(document.get("url") or ""),
                        source=str(document.get("source") or ""))


def recorded_outcome(slug: str, *, registry_dir: Path | None = None) -> dict[str, Any]:
    held = _registry_record(slug, registry_dir).get(OUTCOME_KEY)
    return held if isinstance(held, dict) else {}


def _write_record_key(slug: str, key: str, value: dict[str, Any] | None, *, registry_dir: Path | None) -> str:
    """Set (or, with None, remove) `key` in `slug`'s registry record. "" or why not. Unchanged is not written."""
    from scripts import team_launcher as launcher
    from scripts.board_ui_record import _write_atomically

    path = (registry_dir or launcher.switchyard_registry_dir()) / f"{slug}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"{path} cannot be read ({exc})"
    if not isinstance(payload, dict) or payload.get("schema") != REGISTRY_SCHEMA:
        return f"{path} is not a registry record"
    held = payload.get(key)
    stamps = ("recorded_at", "recorded_by")
    if value is None and key not in payload:
        return ""
    if value is not None and isinstance(held, dict) and {k: v for k, v in held.items() if k not in stamps} == value:
        return ""
    if value is None:
        payload.pop(key, None)
    else:
        from scripts.ticket_board.rollout_journal import resolve_operator

        payload[key] = {**value, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "recorded_by": str(getattr(resolve_operator(), "name", "") or "")}
    try:
        _write_atomically(path, payload)
    except OSError as exc:
        return f"{path} cannot be written ({exc})"
    return ""


def record_report_outcome(
    slug: str,
    state: str,
    *,
    board: str = "",
    url: str = "",
    detail: str = "",
    registry_dir: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> None:
    """What became of `slug`'s report connection, in its root-owned registry record."""
    problem = _write_record_key(slug, OUTCOME_KEY, {"state": state, "board": board, "url": url, "detail": detail},
                                registry_dir=registry_dir)
    if problem:
        print_func(f"switchyard: {slug}'s report state ({state}) was not recorded: {problem}")


def board_answer(url: str, slug: str, *, timeout: float = PROBE_SECONDS) -> str:
    """"" when the board at `url` answers as `slug`, else what happened instead.

    The read every board serves to anyone (`GET /api/runtime-assignments`, as `board_socket_answer` asks
    on the socket): HTTP 200, a JSON object naming `slug`. Nothing is written to the board.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname:
        return f"{url} is not an http:// board URL"
    try:
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
        try:
            connection.request("GET", parsed.path.rstrip("/") + "/api/runtime-assignments")
            response = connection.getresponse()
            body = response.read(65536).decode("utf-8", errors="replace")
        finally:
            connection.close()
    except (OSError, http.client.HTTPException) as exc:
        return f"the board at {url} did not answer ({exc})"
    if response.status != 200:
        return f"the board at {url} answered HTTP {response.status}"
    try:
        named = json.loads(body).get("project")
    except (ValueError, AttributeError):
        return f"what answers at {url} is not a board (its answer is not a JSON object)"
    if named != slug:
        return f"what answers at {url} is {named!r}, not {slug}'s board" if isinstance(named, str) else (
            f"what answers at {url} names no project")
    return ""


def resolve_repair_board(
    config: ProjectConfig,
    *,
    upstream_report_url: str = "",
    upstream_report_board: str = "",
    registry_dir: Path | None = None,
    home_for_user: Callable[[str], Path | None] | None = None,
) -> tuple[str, str]:
    """(URL, "") the narrow step connects `config` to, or ("", why none can be chosen).

    In order: the URL or board named; the URL the configuration already records; the board the tenant's
    own root-owned records name; the host's one declared report board. Never a guess between two.
    """
    url = upstream_report_url.strip().rstrip("/")
    named = upstream_report_board.strip()
    boards = report_boards(exclude=config.project, registry_dir=registry_dir)
    if named:
        board = next((b for b in boards if b.slug == named), None)
        if board is None or not board.url:
            return "", (f"--upstream-report-board {named} names no other board registered on this host"
                        + (f" ({board.problem})" if board is not None and board.problem else ""))
        if url and url != board.url:
            return "", f"--upstream-report-url {url} is not {named}'s board ({board.url})"
        return board.url, ""
    if url:
        return url, ""
    configured = str(config.upstream_report_url or "").strip()
    if configured:
        return configured, ""
    outcome = recorded_outcome(config.project, registry_dir=registry_dir)
    recorded, recorded_url = str(outcome.get("board") or ""), str(outcome.get("url") or "")
    if not recorded and not recorded_url and os.geteuid() == 0:
        decision = recorded_report_decision(config.project)
        recorded, recorded_url = (decision.board, decision.url) if decision is not None else ("", "")
    board = next((b for b in boards if recorded and b.slug == recorded), None)
    if board is not None and board.url:
        return board.url, ""
    if recorded_url and not recorded:
        return recorded_url, ""
    default, why = declared_default(boards, registry_dir=registry_dir, home_for_user=home_for_user)
    if default is not None:
        return default.url, ""
    listing = "; ".join(_listing(boards)) or "none"
    return "", (f"{config.project} records no report board and {why}; name one with --upstream-report-board "
                f"<slug> (registered: {listing})")


def running_panes_note(
    config: ProjectConfig,
    *,
    url: str,
    token_file: str,
    runner: Callable[..., Any] | None = None,
) -> str:
    """Whether panes already running need a relaunch to carry a link just written: each pane's environment
    is set when it launches, so one started before the link has no `TICKET_BOARD_REPORT_URL`."""
    from scripts import team_launcher as launcher

    kwargs = {"runner": runner} if runner is not None else {}
    targets, problem = launcher.owner_tmux_targets(config, timeout_seconds=PROBE_SECONDS, interactive=False, **kwargs)
    # A pane launched before the link has none of the three report variables, its origin project included.
    by_hand = (f"until then file by hand with `ticket-board-write --report-board-url {url} --report-token-file "
               f"{token_file} file-report --origin-project {config.project} ...`")
    relaunch = f"`switchyard stop {config.project}` then `switchyard start {config.project}`"
    if problem:
        return (f"switchyard: whether {config.project}'s panes are running could not be told ({problem}); any "
                f"pane launched before now files reports only after a relaunch ({relaunch}); {by_hand}.")
    running = [role.role for role in config.roles if role.target in targets]
    if not running:
        return f"switchyard: no {config.project} pane is running; each carries the report link from its next launch."
    return (f"switchyard: {len(running)} of {len(config.roles)} {config.project} panes ({', '.join(running)}) were "
            f"launched before this link and file reports only after a relaunch ({relaunch}); {by_hand}.")


def report_status(slugs: Sequence[str], *, registry_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """`report_status_lines` for `switchyard status --json`: each registered project's recorded state."""
    found: dict[str, dict[str, Any]] = {}
    for slug in slugs:
        record = _registry_record(slug, registry_dir)
        if record:
            outcome = record.get(OUTCOME_KEY) if isinstance(record.get(OUTCOME_KEY), dict) else {}
            found[slug] = {"state": str(outcome.get("state") or "not-recorded"),
                           **{k: str(outcome.get(k) or "") for k in ("board", "url", "detail", "recorded_at")},
                           "takes_tenant_reports": is_declared(record)}
    return found


def _recorded_hint(slug: str, *, registry_dir: Path | None) -> str:
    """`repair_hint` from the world-readable registry alone, for a status any account can ask for."""
    from scripts import team_launcher as launcher

    others = [e.slug for e in launcher._registry_project_entries(registry_dir) if e.slug != slug]
    declared = [other for other in others if is_declared(_registry_record(other, registry_dir))]
    if len(declared) == 1 or not others:
        return f"`{repair_command(slug)}`"
    return f"`{repair_command(slug, board='<slug>')}`, naming one of: {', '.join(others)}"


def report_status_lines(slugs: Sequence[str], *, registry_dir: Path | None = None) -> list[str]:
    """Each project's report state from root-owned registry records only: no root, no configuration, no token."""
    lines: list[str] = []
    for slug in slugs:
        record = _registry_record(slug, registry_dir)
        if not record:
            continue
        outcome = record.get(OUTCOME_KEY) if isinstance(record.get(OUTCOME_KEY), dict) else {}
        state = str(outcome.get("state") or "")
        board, url, detail = (str(outcome.get(k) or "") for k in ("board", "url", "detail"))
        if state == CONNECTED:
            said = f"files them to {board} at {url}"
        elif state == OPTED_OUT:
            said = f"opted out: files none (connect it with {_recorded_hint(slug, registry_dir=registry_dir)})"
        elif state == UNAVAILABLE:
            said = (f"none: no board was available when it was created; connect it with "
                    f"{_recorded_hint(slug, registry_dir=registry_dir)}")
        elif state == FAILED:
            said = (f"NOT CONNECTED: connecting to {board or url or 'its board'} failed ({detail}); retry with "
                    f"`{repair_command(slug)}`")
        elif is_declared(record):
            said = "the board this host's tenants report to (declared by root)"
        else:
            said = f"not recorded; check it with `{repair_command(slug)} --dry-run`"
        if state and is_declared(record):
            said += "; and the board this host's tenants report to (declared by root)"
        lines.append(f"switchyard: reports: {slug}: {said}")
    return lines


def connect_chosen_report_board(
    config: ProjectConfig,
    *,
    config_path: Path,
    choice: ReportChoice,
    upstream_report_token_file: str = "",
    registry_dir: Path | None,
    print_func: Callable[[str], None],
) -> tuple[ProjectConfig, str]:
    """The report decision carried out before any pane launches, and recorded: (config, the summary line).

    `switchyard new` and `resume-provision` both end here (SYRD-674). A connection that fails does not undo
    the project: it is recorded as failed, with the one command that retries it -- which needs no URL,
    since the board it was meant for is recorded.
    """
    from scripts import team_launcher as launcher

    slug = config.project
    if choice.state != CONNECT:
        record_report_outcome(slug, choice.state, detail=choice.source, registry_dir=registry_dir,
                              print_func=print_func)
        summary = choice_summary(slug, choice, registry_dir=registry_dir)
        print_func(summary)
        return config, summary
    _linked, problems = launcher.connect_upstream_report(
        config, config_path=config_path, upstream_report_url=choice.url,
        upstream_report_token_file=upstream_report_token_file, registry_dir=registry_dir, print_func=print_func,
    )
    if not choice.board:
        board = next((b.slug for b in report_boards(exclude=slug, registry_dir=registry_dir)
                      if b.url == choice.url.rstrip("/")), "")
        choice = ReportChoice(choice.state, board=board, url=choice.url, source=choice.source)
    for problem in problems:
        print_func(f"switchyard: {problem}")
    if problems:
        record_report_outcome(slug, FAILED, board=choice.board, url=choice.url,
                              detail="; ".join(problems), registry_dir=registry_dir, print_func=print_func)
        summary = unconnected_warning(slug, f"connecting to {choice.board or choice.url} failed",
                                      repair=f"`{repair_command(slug)}`")
        print_func(summary)
        return config, summary
    record_report_outcome(slug, CONNECTED, board=choice.board, url=choice.url,
                          registry_dir=registry_dir, print_func=print_func)
    summary = (f"switchyard: Switchyard reports: {slug} files them to {choice.board} at {choice.url}; its link and "
               "report-only credential are in place before any pane starts")
    print_func(summary)
    return launcher.load_project_config(slug, config_path), summary


def resume_report_connection(
    config: ProjectConfig,
    *,
    config_path: Path,
    registry_dir: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> ProjectConfig:
    """`resume-provision`'s report step, before it starts any role: what `new` decided, carried out.

    A recorded outcome other than a failure is left as it is. Otherwise root's record of `new`'s decision
    is honoured; with none -- `new` stopped before it was kept -- only the host's one declared board, or
    no board at all, decides it without the operator, and anything else is said, not guessed (SYRD-674).
    """
    slug = config.project
    held = str(recorded_outcome(slug, registry_dir=registry_dir).get("state") or "")
    if held in (CONNECTED, OPTED_OUT, UNAVAILABLE):
        return config
    choice = recorded_report_decision(slug)
    if choice is None:
        boards = report_boards(exclude=slug, registry_dir=registry_dir)
        default, why = declared_default(boards, registry_dir=registry_dir)
        if default is not None:
            choice = ReportChoice(CONNECT, board=default.slug, url=default.url,
                                  source="the host's declared report board")
        elif not boards:
            choice = ReportChoice(UNAVAILABLE, source="no other board is registered on this host")
        else:
            print_func(unconnected_warning(slug, f"no report board was decided, and {why}",
                                           repair=repair_hint(slug, registry_dir=registry_dir)))
            return config
    connected, _summary = connect_chosen_report_board(config, config_path=config_path, choice=choice,
                                                      registry_dir=registry_dir, print_func=print_func)
    return connected


# ------------------------------------------------------------------ the declaration


def declare_report_board(
    config: ProjectConfig,
    *,
    accept: bool,
    dry_run: bool = False,
    registry_dir: Path | None = None,
    home_for_user: Callable[[str], Path | None] | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """`switchyard upgrade <slug> --only accept-reports | refuse-reports`: root's declaration, nothing else."""
    slug = config.project
    if accept:
        board = next((b for b in report_boards(registry_dir=registry_dir) if b.slug == slug), None)
        problem = "it is not registered on this host" if board is None else board_problem(
            board, registry_dir=registry_dir, home_for_user=home_for_user)
        if board is not None and not problem:
            problem = board_answer(board.url, slug)
        if problem:
            print_func(f"switchyard: {slug} cannot take tenant reports: {problem}. Nothing was changed.")
            return 1
    if dry_run:
        print_func(f"switchyard: would record that {slug} {'takes' if accept else 'does not take'} tenant reports; "
                   "nothing written")
        return 0
    problem = _write_record_key(slug, SINK_KEY, {"accepted": True} if accept else None, registry_dir=registry_dir)
    if problem:
        print_func(f"switchyard: {slug}'s declaration was not recorded: {problem}")
        return 1
    others = [b.slug for b in report_boards(exclude=slug, registry_dir=registry_dir) if b.declared]
    print_func(f"switchyard: {slug} {'takes' if accept else 'does not take'} reports from this host's other tenants"
               + ("; `switchyard new` connects every new tenant to it by default" if accept and not others else "")
               + (f"; with {', '.join(others)} also declared, `switchyard new` asks which"
                  if accept and others else ""))
    return 0
