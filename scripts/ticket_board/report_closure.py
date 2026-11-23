"""A reporting project's Director is told, once per close, when a ticket it filed upstream ends (SYRD-827).

The User, 2026-11-16: "when another tenant files a ticket upstream, the board tells that project's director when the
ticket has been closed or cancelled". Two halves, and no new write across boards:

* **The upstream board answers a read.** `GET /api/report-status`, with the report credential the reporting project
  already holds (`X-Ticket-Board-Report-Token`, bound to that project; the project comes from the credential, never
  from the request): that project's own reports, by `report_provenance`, that are in a terminal stage -- id, title,
  state, closing commit, how many times it was reopened, and its reason when no release will tell its reporter
  anything: for a cancelled one its Director's latest comment; for one done with no commit (a no-code close, or a
  won't-fix closed as done) its no-code submission's reason, or else its Director's latest comment. Given
  `?release=<the caller's build>`, it also says whether that release contains each closing commit, from this board's
  own commit repositories (`git merge-base --is-ancestor`): true, false, or null when it cannot tell. Given
  `&installed=<the release installed on the caller's host>`, it says the same of that release (`installed_contains`);
  given `&noticed=<a release>`, of that one (`noticed_contains`; SYRD-869's listeners ask it, later ones do not).
* **The reporting board's own listener asks.** On a slow interval (`TICKET_BOARD_REPORT_CLOSE_SECONDS`, default 900),
  it walks its project's report chain as `file-report` does (root's registry; each ancestor's own credential file) and
  records each close with `ticket_board.note_report_closed`, which queues one Director notice for a close it has not
  recorded. A close is (upstream, ticket, terminal state, reopen count): a ticket reopened and closed again is a new
  close; one still open is none. An unreachable ancestor is logged when it fails and when it is back, never per pass,
  and tells nothing. A close older than ANNOUNCE_DAYS is recorded without a notice, so the first pass after this ships
  does not replay a project's whole history to its Director.
* **A release will tell it (SYRD-868).** A report closed done on a commit neither this board's release nor the release
  installed on this host contains (`release_contains` and `installed_contains` both false) is recorded without a
  notice too: the next ./install must carry the fix, and its release notice (scripts/release_notice.py) names the
  reports it resolves. One the installed release already contains is announced: that install's notice went out before
  the report was done, so it did not name it (Audit, round 1), and no later one will. A cancelled close, one with no
  commit, one this board's release already contains, and one where either cannot be told (null) are announced too.
* **Until a release notice names it (SYRD-869, SYRD-870).** A close recorded that way stays pending. ./install may
  tell this board nothing (its tenant not registered, or no ticket to carry the notice), or tell it and name nothing
  (its read of the upstream board failed), so a later pass that finds the installed release containing the fix and
  this board's release not announces it then, once (close key `<state>_installed#<reopens>`) -- unless a release
  notice this board recorded since that close named it ("This release resolves your report ... on <upstream>'s
  board", kept in `release_notices.message` by pgu1058). This board's release coming to contain it ends it quietly
  too. A record it cannot read counts as owed: a duplicate rather than silence.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable

try:
    from . import report_ancestry, report_follow_up
    from .build_identity import board_build_id
    from .commit_repos import commit_git_dirs_for_project
except ImportError:  # pragma: no cover - run outside the package, as the listener's modules may be
    import report_ancestry  # type: ignore[no-redef]
    import report_follow_up  # type: ignore[no-redef]
    from build_identity import board_build_id  # type: ignore[no-redef]
    from commit_repos import commit_git_dirs_for_project  # type: ignore[no-redef]

PATH = "/api/report-status"
#: The release ./install last installed on this host (release_notice.installed_release reads the same link).
INSTALLED_RELEASE = Path("/opt/switchyard/current")
INTERVAL_ENV = "TICKET_BOARD_REPORT_CLOSE_SECONDS"
DEFAULT_INTERVAL_SECONDS = 900.0
#: A close older than this is recorded, not announced.
ANNOUNCE_DAYS = 30
MAX_REPORTS = 200
TIMEOUT_SECONDS = 5.0
MAX_MESSAGE_CHARS = 3900
MAX_REASON_CHARS = 1500
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
RELEASE_RE = re.compile(r"^[0-9a-f]{40}$")
STATE_RE = re.compile(r"^[a-z_]{1,40}$")
TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")
#: C0 and C1 controls and DEL, newline excepted: the notice is typed into a Director's pane (`tmux send-keys -l`), and
#: an upstream board's title or comment must not arrive there as keys -- an escape sequence, a carriage return, a bell.
CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
LOGGER = logging.getLogger("ticket_board.report_closure")


# -- the upstream board's read ----------------------------------------------------------------------------------------
STATUS_SQL = """
WITH terminal AS (
    SELECT x->>'name' AS name
      FROM ticket_board.workflow_configuration w, jsonb_array_elements(w.document->'stages') x
     WHERE w.singleton AND coalesce((x->>'terminal')::boolean, false)
    UNION SELECT unnest(ARRAY['done', 'cancelled'])
     WHERE NOT EXISTS (SELECT FROM ticket_board.workflow_configuration WHERE singleton)
)
SELECT t.id, t.title, t.state, t.commit_hash, t.updated_at,
       (SELECT count(*) FROM ticket_board.ticket_reopen_records r WHERE r.ticket_id = t.id) AS reopens,
       (SELECT c.text FROM ticket_board.ticket_comments c
         WHERE c.ticket_id = t.id AND c.who = 'director' ORDER BY c.position DESC LIMIT 1) AS director_comment,
       (SELECT n.reason FROM ticket_board.ticket_no_code_reasons n
         WHERE n.ticket_id = t.id ORDER BY n.id DESC LIMIT 1) AS no_code_reason
  FROM ticket_board.report_provenance p
  JOIN ticket_board.tickets t ON t.id = p.ticket_id
 WHERE p.origin_project = %s AND t.state IN (SELECT name FROM terminal)
 ORDER BY t.updated_at DESC NULLS LAST, t.id
 LIMIT %s
"""


def statuses(app: Any, project: str) -> list[dict[str, Any]]:
    """`project`'s reports on this board that are in a terminal stage, newest first."""
    with app._pg_connect() as conn:
        rows = conn.execute(STATUS_SQL, (project, MAX_REPORTS)).fetchall()
    names = ("id", "title", "state", "commit_hash", "updated_at", "reopens", "director_comment", "no_code_reason")
    out = []
    for row in rows:
        values = dict(row) if isinstance(row, dict) else dict(zip(names, row))
        updated = values.get("updated_at")
        out.append({"id": values["id"], "title": values["title"], "state": values["state"],
                    "commit_hash": values.get("commit_hash") or "", "reopens": int(values.get("reopens") or 0),
                    "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else (updated or None),
                    "reason": reason(values)})
    return out


def reason(values: dict[str, Any]) -> str:
    """Why a report ended where no release will say anything (SYRD-868): a cancel's Director comment; for a close with
    no commit, the reason its declared no-code move recorded (pgu1057), else -- a won't-fix closed as done, a close
    from before pgu1057 or on an undeclared board -- its Director's latest comment."""
    director = values.get("director_comment") or ""
    if values["state"] == "cancelled":
        return director
    if values.get("commit_hash"):
        return ""
    return (values.get("no_code_reason") or "").strip() or director


def release_contains(git_dirs: tuple[Path, ...], commit: str, release: str,
                     runner: Callable[..., Any] = subprocess.run) -> bool | None:
    """Whether `release` contains `commit`, by this board's own repositories; None when none of them can tell."""
    if not (COMMIT_RE.fullmatch(commit or "") and RELEASE_RE.fullmatch(release or "")):
        return None
    for git_dir in git_dirs:
        if not Path(git_dir).is_dir():
            continue
        try:
            done = runner(["git", "--git-dir", str(git_dir), "merge-base", "--is-ancestor", commit, release],
                          capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode in (0, 1):  # 128: an object this repository does not have -- ask the next
            return done.returncode == 0
    return None


def serve_status(handler: Any, parsed: Any) -> None:
    """GET /api/report-status: the calling project's own ended reports. Read-only."""
    from urllib.parse import parse_qs

    try:
        project = report_follow_up.authenticate(handler)
        if not project:
            raise PermissionError("report status needs this project's own report credential, bound to it; this is "
                                  "the board's shared report token")
        query = parse_qs(parsed.query)
        asked = {name: (query.get(name) or [""])[0].strip().lower() for name in ("release", "installed", "noticed")}
        if any(value and not RELEASE_RE.fullmatch(value) for value in asked.values()):
            raise ValueError("release, installed and noticed must be full commit hashes")
        release = asked["release"]
        reports = statuses(handler.app, project)
        git_dirs = commit_git_dirs_for_project(project=str(handler.app.project)) if any(asked.values()) else ()
        for report in reports:
            for key, commit in (("release_contains", release), ("installed_contains", asked["installed"]),
                                ("noticed_contains", asked["noticed"])):
                report[key] = (release_contains(git_dirs, report["commit_hash"], commit)
                               if commit and report["commit_hash"] else None)
    except PermissionError as exc:
        return handler.send_text(str(exc), HTTPStatus.FORBIDDEN)
    except ValueError as exc:
        return handler.send_text(str(exc), HTTPStatus.BAD_REQUEST)
    handler.send_json({"project": str(handler.app.project), "ticket_prefix": str(handler.app.ticket_prefix),
                       "reporter": project, "release": release, "reports": reports})


# -- the reporting board's listener ------------------------------------------------------------------------------------
def destinations(project: str, environ: Any = os.environ) -> list[tuple[Any, Path | None]]:
    """Each ancestor `project` can file to, nearest first, with the credential file `file-report` would use."""
    links, _why = report_ancestry.chain(project, report_ancestry.registry_dir(environ))
    token_file = (str(environ.get("TICKET_BOARD_TENANT_REPORT_TOKEN_FILE") or "").strip()
                  or str(environ.get("TICKET_BOARD_REPORT_TOKEN_FILE") or "").strip()
                  or str(Path(str(environ.get("HOME") or Path.home())) / ".config" / project / "upstream-report.env"))
    return [(link, report_ancestry.credential_path(link, immediate=index == 0, token_file=token_file))
            for index, link in enumerate(links)]


def installed_release(link: Path | None = None) -> str:
    """The release installed on this host, or '' when there is none to name."""
    try:
        name = Path(os.readlink(link or INSTALLED_RELEASE)).name
    except OSError:
        return ""
    return name if RELEASE_RE.fullmatch(name) else ""


def fetch_status(url: str, token: str, release: str, installed: str = "", *,
                 timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    """One ancestor's answer about this project's ended reports. Raises OSError/ValueError when it cannot say."""
    from urllib import error, parse, request

    pairs = (("release", release), ("installed", installed))
    asked = [f"{name}={parse.quote(value)}" for name, value in pairs if RELEASE_RE.fullmatch(value or "")]
    query = "?" + "&".join(asked) if asked else ""
    req = request.Request(f"{url.rstrip('/')}{PATH}{query}", method="GET",
                          headers={report_follow_up.REPORT_TOKEN_HEADER: token})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            answer = json.loads(response.read().decode("utf-8", errors="replace"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()[:200] if hasattr(exc, "read") else ""
        raise OSError(f"HTTP {exc.code}" + (f": {detail}" if detail else "")) from None
    except error.URLError as exc:
        raise OSError(str(getattr(exc, "reason", exc))) from None
    if not isinstance(answer, dict) or not isinstance(answer.get("reports"), list):
        raise ValueError("its answer is not a report status")
    return answer


def message(*, reporter: str, upstream: str, url: str, report: dict[str, Any], build: str,
            contains: bool | None, installed: str = "") -> str:
    """The Director's notice about one close: what ended, how, whether this board runs it, and where to read it."""
    ticket, state, commit = report["id"], report["state"], report.get("commit_hash") or ""
    lines = [f"Your report {ticket} on {upstream}'s board is now {state}: {report.get('title', '')}"]
    if state == "cancelled":
        reason = (report.get("reason") or "").strip()
        lines.append("Reason, from its Director's latest comment: "
                     + (reason[:MAX_REASON_CHARS] + ("..." if len(reason) > MAX_REASON_CHARS else "")
                        if reason else "(its Director left no comment)"))
    elif commit:
        lines.append(f"It closed on commit {commit}.")
        shown = build[:12] if RELEASE_RE.fullmatch(build or "") else (build or "unknown")
        if contains is True:
            lines.append(f"This board's release ({shown}) already contains it: nothing to deploy for it.")
        elif contains is False and report.get("installed_contains") is True and RELEASE_RE.fullmatch(installed or ""):
            # SYRD-868: installed before this close, so that install's notice could not name it.
            lines.append(f"This board's release ({shown}) does not contain it yet; release {installed}, installed on "
                         "this host, does. Preview, upgrade and deploy it:")
            lines += [f"  switchyard privileged-action {reporter} {action} commit={installed}"
                      for action in ("preview-upgrade", "upgrade-tenant-release", "deploy-release")]
        elif contains is False:
            lines.append(f"This board's release ({shown}) does not contain it yet. Once a release on this host does "
                         f"(`switchyard release-status {reporter}`), preview, upgrade and deploy it:")
            lines += [f"  switchyard privileged-action {reporter} {action} commit=<that release>"
                      for action in ("preview-upgrade", "upgrade-tenant-release", "deploy-release")]
        else:
            lines.append(f"Whether this board's release ({shown}) contains it could not be told; check "
                         f"`switchyard release-status {reporter}`.")
    else:
        reason = (report.get("reason") or "").strip()
        lines.append("It closed with no commit, so no release will carry it. Reason, as its board recorded it: "
                     + (reason[:MAX_REASON_CHARS] + ("..." if len(reason) > MAX_REASON_CHARS else "")
                        if reason else "(none recorded)"))
    lines.append(f"Read it: ticket-board-read --board-url {url} ticket {ticket}")
    text = plain("\n".join(lines))
    return text if len(text) <= MAX_MESSAGE_CHARS else text[:MAX_MESSAGE_CHARS - 3] + "..."


def plain(text: str) -> str:
    """`text` without C0/C1 control characters or DEL; its newlines kept."""
    return CONTROL_CHARS.sub("", text)


def awaits_release(report: dict[str, Any]) -> bool:
    """Whether a release notice must still carry this close (SYRD-868): done, on a commit neither this board's release
    nor the one installed on this host is known to contain, so the next ./install names it."""
    return (report.get("state") == "done" and bool(report.get("commit_hash"))
            and report.get("release_contains") is False and report.get("installed_contains") is False)


def told_late(report: dict[str, Any]) -> bool:
    """Whether a close recorded without a notice may now be owed one (SYRD-869): done on a commit the installed
    release contains and this board's release does not. It is owed unless a release notice named it."""
    return (report.get("state") == "done" and bool(report.get("commit_hash"))
            and report.get("release_contains") is False and report.get("installed_contains") is True)


def _recorded_silently(conn: Any, upstream: str, ticket: str, key: str) -> bool:
    """Whether this board recorded that close without a notice. A record it cannot read counts as one (SYRD-870): the
    late key still tells it once where it can be recorded, and a duplicate is better than silence."""
    try:
        row = conn.execute("SELECT told FROM ticket_board.report_closures WHERE upstream = %s AND ticket_id = %s "
                           "AND close_key = %s", (upstream, ticket, key)).fetchone()
    except Exception:  # noqa: BLE001 - fail loud: owed
        return True
    told = (row["told"] if isinstance(row, dict) else row[0]) if row else None
    return told is False


#: The line a release notice names its reporter's resolved reports with (scripts/release_notice.py, SYRD-868).
RESOLVES_RE = re.compile(r"^This release resolves your reports? (.+) on ([a-z0-9][a-z0-9_]{0,39})'s board\.$")


def named_by_notice(conn: Any, upstream: str, ticket: str, key: str) -> bool:
    """Whether a release notice this board recorded since it recorded this close (`key`) named `ticket` on
    `upstream`'s board (SYRD-870): one that named an earlier close of a reopened report is not this close's. False when
    no record says so, or none can be read -- the close is then told."""
    try:
        rows = conn.execute(
            "SELECT n.message FROM ticket_board.release_notices n WHERE n.message IS NOT NULL AND n.noticed_at > "
            "(SELECT r.recorded_at FROM ticket_board.report_closures r WHERE r.upstream = %s AND r.ticket_id = %s "
            "AND r.close_key = %s)", (upstream, ticket, key)).fetchall()
    except Exception:  # noqa: BLE001 - fail loud: not named
        return False
    for row in rows:
        text = (row["message"] if isinstance(row, dict) else row[0]) or ""
        for line in text.splitlines():
            found = RESOLVES_RE.fullmatch(line.strip())
            named = re.sub(r" and [0-9]+ more$", "", found.group(1)).split(", ") if found else []  # a capped list
            if found and found.group(2) == upstream and ticket in named:
                return True
    return False


def announce(report: dict[str, Any], *, now: datetime) -> bool:
    """Whether a close is recent enough to announce; one with no time is."""
    try:
        updated = datetime.fromisoformat(str(report.get("updated_at") or ""))
    except ValueError:
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return now - updated <= timedelta(days=ANNOUNCE_DAYS)


def record(conn: Any, *, reporter: str, upstream: str, url: str, answer: dict[str, Any], build: str,
           now: datetime, log: Any = LOGGER, seen: set[tuple[str, str]] | None = None,
           installed: str = "") -> dict[str, int]:
    """Record every close in one ancestor's answer; how many were told, recorded silently, known, unanchored, skipped
    as malformed, or refused by this board. One bad entry is skipped -- logged once per upstream ticket (`seen` carries
    that across passes) -- and every entry after it still counts."""
    counts = {"told": 0, "recorded": 0, "already": 0, "no_anchor": 0, "skipped": 0, "refused": 0}
    seen = set() if seen is None else seen

    def once(ticket: str, problem: str) -> None:
        if (upstream, ticket) not in seen:
            seen.add((upstream, ticket))
            log.warning("%s's report status entry %r %s; skipped", upstream, ticket, problem)

    for report in answer.get("reports") or []:
        ticket = str(report.get("id") or "?")[:60] if isinstance(report, dict) else "?"
        try:  # the whole entry: whatever one entry holds, it can cost only itself
            counts[_record_one(conn, report, reporter=reporter, upstream=upstream, url=url, build=build, now=now,
                               installed=installed)] += 1
        except _Malformed as exc:
            counts["skipped"] += 1  # not something this board stores or says: an answer it cannot vouch for
            once(ticket, f"is malformed ({exc})")
        except Exception as exc:  # noqa: BLE001 - autocommit: a refused statement leaves the connection usable
            said = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
            if type(exc).__module__.startswith("psycopg"):
                counts["refused"] += 1
                once(ticket, f"was refused by this board ({said or type(exc).__name__})")
            else:
                counts["skipped"] += 1
                once(ticket, f"is malformed ({type(exc).__name__}: {said})")
    return counts


class _Malformed(ValueError):
    """An entry this board cannot vouch for: its id, state or commit is not one a board stores."""


def _record_one(conn: Any, report: Any, *, reporter: str, upstream: str, url: str, build: str, now: datetime,
                installed: str = "") -> str:
    """One entry of an ancestor's answer, recorded: note_report_closed's outcome. Raises on anything it cannot use."""
    if not isinstance(report, dict):
        raise _Malformed("not an object")
    ticket, state = str(report.get("id") or ""), str(report.get("state") or "")
    commit = str(report.get("commit_hash") or "")
    if not (TICKET_RE.fullmatch(ticket) and STATE_RE.fullmatch(state)) or (commit and not COMMIT_RE.fullmatch(commit)):
        raise _Malformed("its id, state or commit")
    key = f"{state}#{int(report.get('reopens') or 0)}"
    contains = report.get("release_contains")
    text = message(reporter=reporter, upstream=upstream, url=url, report=report, build=build,
                   contains=contains if isinstance(contains, bool) else None, installed=installed)
    recent = announce(report, now=now)
    if recent and told_late(report) and _recorded_silently(conn, upstream, ticket, key) \
            and not named_by_notice(conn, upstream, ticket, key):
        key = f"{state}_installed#{int(report.get('reopens') or 0)}"  # once: the next pass finds it
        tell = True
    else:
        tell = recent and not awaits_release(report)
    row = conn.execute("SELECT ticket_board.note_report_closed(%s, %s, %s, %s, %s, %s, %s) AS outcome",
                       (upstream, ticket, key, state, commit, text, tell)).fetchone()
    outcome = row["outcome"] if isinstance(row, dict) else row[0]
    return outcome if outcome in ("told", "recorded", "already", "no_anchor") else "skipped"


def run(listener: Any, conn: Any, *, environ: Any = os.environ, clock: Callable[[], float] = time.monotonic,
        fetch: Callable[..., dict[str, Any]] = fetch_status) -> None:
    """The listener's pass: on its interval, ask each ancestor about this project's ended reports. Never raises.

    Only the listener service asks (its main() sets `report_closures`): a listener a test builds reads no registry,
    credential or upstream board of this host's."""
    if getattr(listener, "report_closures", False) is not True:
        return
    now = clock()
    if now < getattr(listener, "_report_closure_due", 0.0):
        return
    try:
        interval = float(environ.get(INTERVAL_ENV) or DEFAULT_INTERVAL_SECONDS)
    except ValueError:
        interval = DEFAULT_INTERVAL_SECONDS
    listener._report_closure_due = now + max(interval, 0.0)
    log = getattr(listener, "logger", LOGGER)
    problems: dict[str, str] = listener.__dict__.setdefault("_report_closure_problems", {})
    project = str(getattr(listener, "project", "") or "").strip()
    if not project:
        return
    try:
        build = listener.__dict__.get("_report_closure_build") or board_build_id()
        listener._report_closure_build = build
        installed = listener.__dict__.get("_report_closure_installed") or installed_release()  # each pass: it moves
        for link, credential in destinations(project, environ):
            token, why = report_ancestry.read_token(credential) if credential else ("", "none")
            if not token:
                problem = f"no report credential for {link.project} ({why})"
            else:
                try:
                    answer = fetch(link.url, token, build, installed)
                    counts = record(conn, reporter=project, upstream=link.project, url=link.url, answer=answer,
                                    build=build, now=datetime.now(timezone.utc), log=log,
                                    seen=listener.__dict__.setdefault("_report_closure_entries", set()),
                                    installed=installed)
                    problem = ""
                    if counts["told"] or counts["recorded"]:
                        log.info("Report closes from %s: %s", link.project, counts)
                except (OSError, ValueError) as exc:
                    problem = f"{link.project}'s report status could not be read: {exc}"
            if problem != problems.get(link.project, ""):
                (log.warning if problem else log.info)(problem or f"{link.project}'s report status is readable again")
                problems[link.project] = problem
    except Exception:  # noqa: BLE001 - a pass about upstream reports must never stop the listener's deliveries
        log.exception("Report close pass failed")
