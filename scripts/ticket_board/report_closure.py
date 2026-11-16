"""A reporting project's Director is told, once per close, when a ticket it filed upstream ends (SYRD-827).

The User, 2026-11-16: "when another tenant files a ticket upstream, the board tells that project's director when the
ticket has been closed or cancelled". Two halves, and no new write across boards:

* **The upstream board answers a read.** `GET /api/report-status`, with the report credential the reporting project
  already holds (`X-Ticket-Board-Report-Token`, bound to that project; the project comes from the credential, never
  from the request): that project's own reports, by `report_provenance`, that are in a terminal stage -- id, title,
  state, closing commit, how many times it was reopened, and for a cancelled one its Director's latest comment. Given
  `?release=<the caller's build>`, it also says whether that release contains each closing commit, from this board's
  own commit repositories (`git merge-base --is-ancestor`): true, false, or null when it cannot tell.
* **The reporting board's own listener asks.** On a slow interval (`TICKET_BOARD_REPORT_CLOSE_SECONDS`, default 900),
  it walks its project's report chain as `file-report` does (root's registry; each ancestor's own credential file) and
  records each close with `ticket_board.note_report_closed`, which queues one Director notice for a close it has not
  recorded. A close is (upstream, ticket, terminal state, reopen count): a ticket reopened and closed again is a new
  close; one still open is none. An unreachable ancestor is logged when it fails and when it is back, never per pass,
  and tells nothing. A close older than ANNOUNCE_DAYS is recorded without a notice, so the first pass after this ships
  does not replay a project's whole history to its Director.
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
         WHERE c.ticket_id = t.id AND c.who = 'director' ORDER BY c.position DESC LIMIT 1) AS director_comment
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
    names = ("id", "title", "state", "commit_hash", "updated_at", "reopens", "director_comment")
    out = []
    for row in rows:
        values = dict(row) if isinstance(row, dict) else dict(zip(names, row))
        updated = values.get("updated_at")
        out.append({"id": values["id"], "title": values["title"], "state": values["state"],
                    "commit_hash": values.get("commit_hash") or "", "reopens": int(values.get("reopens") or 0),
                    "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else (updated or None),
                    "reason": (values.get("director_comment") or "") if values["state"] == "cancelled" else ""})
    return out


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
        release = (parse_qs(parsed.query).get("release") or [""])[0].strip().lower()
        if release and not RELEASE_RE.fullmatch(release):
            raise ValueError("release must be a full commit hash")
        reports = statuses(handler.app, project)
        git_dirs = commit_git_dirs_for_project(project=str(handler.app.project)) if release else ()
        for report in reports:
            report["release_contains"] = (release_contains(git_dirs, report["commit_hash"], release)
                                          if release and report["commit_hash"] else None)
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


def fetch_status(url: str, token: str, release: str, *, timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    """One ancestor's answer about this project's ended reports. Raises OSError/ValueError when it cannot say."""
    from urllib import error, parse, request

    query = f"?release={parse.quote(release)}" if RELEASE_RE.fullmatch(release or "") else ""
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
            contains: bool | None) -> str:
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
        elif contains is False:
            lines.append(f"This board's release ({shown}) does not contain it yet. Once a release on this host does "
                         f"(`switchyard release-status {reporter}`), preview, upgrade and deploy it:")
            lines += [f"  switchyard privileged-action {reporter} {action} commit=<that release>"
                      for action in ("preview-upgrade", "upgrade-tenant-release", "deploy-release")]
        else:
            lines.append(f"Whether this board's release ({shown}) contains it could not be told; check "
                         f"`switchyard release-status {reporter}`.")
    else:
        lines.append("It closed with no commit.")
    lines.append(f"Read it: ticket-board-read --board-url {url} ticket {ticket}")
    text = "\n".join(lines)
    return text if len(text) <= MAX_MESSAGE_CHARS else text[:MAX_MESSAGE_CHARS - 3] + "..."


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
           now: datetime) -> dict[str, int]:
    """Record every close in one ancestor's answer; how many were told, recorded silently, known, or unanchored."""
    counts = {"told": 0, "recorded": 0, "already": 0, "no_anchor": 0, "skipped": 0}
    for report in answer.get("reports") or []:
        if not isinstance(report, dict):
            counts["skipped"] += 1
            continue
        ticket, state = str(report.get("id") or ""), str(report.get("state") or "")
        commit = str(report.get("commit_hash") or "")
        if not (TICKET_RE.fullmatch(ticket) and STATE_RE.fullmatch(state)) or (commit and not COMMIT_RE.fullmatch(commit)):
            counts["skipped"] += 1  # not something this board stores or says: an answer it cannot vouch for
            continue
        key = f"{state}#{int(report.get('reopens') or 0)}"
        contains = report.get("release_contains")
        text = message(reporter=reporter, upstream=upstream, url=url, report=report, build=build,
                       contains=contains if isinstance(contains, bool) else None)
        row = conn.execute("SELECT ticket_board.note_report_closed(%s, %s, %s, %s, %s, %s, %s) AS outcome",
                           (upstream, ticket, key, state, commit, text, announce(report, now=now))).fetchone()
        outcome = row["outcome"] if isinstance(row, dict) else row[0]
        counts[outcome if outcome in counts else "skipped"] += 1
    return counts


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
        for link, credential in destinations(project, environ):
            token, why = report_ancestry.read_token(credential) if credential else ("", "none")
            if not token:
                problem = f"no report credential for {link.project} ({why})"
            else:
                try:
                    answer = fetch(link.url, token, build)
                    counts = record(conn, reporter=project, upstream=link.project, url=link.url, answer=answer,
                                    build=build, now=datetime.now(timezone.utc))
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
