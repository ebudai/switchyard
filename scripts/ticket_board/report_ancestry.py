"""A project's report ancestry: the boards its reports may go to, nearest first (SYRD-721).

A tenant used to have one upstream board. MEFP consumes SAPI as a library, so one
defect may need a ticket on SAPI (the library's) and another on Switchyard (the
platform's). Two unrelated receiver URLs would not say how those boards relate,
so a project has a *report parent* instead, and a parent may have its own:
MEFP -> SAPI -> Switchyard gives MEFP the ordered ancestors SAPI, Switchyard.

* **Root's declaration, nobody else's.** A project's parent is `report_parent` in
  its root-owned registry record (`/etc/switchyard/projects/<slug>.json`),
  written only by root's `switchyard upgrade <slug> --only upstream-report`, with
  every change appended to `report_parent_history`. A tenant cannot rewrite its
  ancestry: neither through its board, nor through its own configuration, which
  every role of it can write. The records are world-readable, so a pane reads the
  chain without root and without reading another tenant's files.
* **Migration.** A record with no `report_parent` key takes its parent from root's
  record of its one upstream connection (`upstream_report`, connected to a
  registered board), so a project reports exactly where it did before. A
  declared `{"project": ""}` is an explicit "no parent".
* **Refused, never guessed:** a project that is its own parent, a cycle, an
  ancestor that is not registered, two links with the same board or ticket
  prefix, a chain deeper than `MAX_DEPTH`, and, at use, a board that does not
  answer as the project root recorded for it. Nothing is inferred from other
  boards on the host or from repository remotes.
* **Credentials.** Each ancestor has its own report-only credential,
  `report-credentials/<slug>.env` beside the immediate parent's
  (`TICKET_BOARD_TENANT_REPORT_TOKEN_FILE`), written by root. One board's token
  is never sent to another. Only whether a credential is usable, and which
  project it is bound to, is ever said; never its value.

Ancestry is not consent: nothing here files anything. `report_fanout` files one
report to one ancestor, or to each, only after an exact confirmation.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

REGISTRY_DIR_ENV = "SWITCHYARD_PROJECT_REGISTRY_DIR"  # team_launcher.SWITCHYARD_REGISTRY_DIR_ENV
DEFAULT_REGISTRY_DIR = Path("/etc/switchyard/projects")
REGISTRY_SCHEMA = "switchyard.project-registry.v1"
PARENT_KEY = "report_parent"
HISTORY_KEY = "report_parent_history"
#: Root's note of the ancestor credentials it issued: when, and whether bound. Never a token.
CREDENTIALS_KEY = "report_credentials"
#: upstream_report_sinks.OUTCOME_KEY / CONNECTED: the one connection a project had before ancestry.
OUTCOME_KEY = "upstream_report"
CONNECTED = "connected"
CREDENTIAL_DIR_NAME = "report-credentials"
TOKEN_KEYS = ("TICKET_BOARD_TENANT_REPORT_TOKEN", "TICKET_BOARD_REPORT_TOKEN")
MAX_DEPTH = 8
PROBE_SECONDS = 5.0


@dataclass(frozen=True)
class Link:
    """One ancestor: a registered project and the board root recorded for it."""

    project: str
    name: str
    url: str
    #: As root recorded it when it verified the board; "" for a link migrated from an older record.
    ticket_prefix: str
    #: "declared" (root's report_parent) or "connected" (migrated from the one upstream connection).
    source: str


@dataclass(frozen=True)
class Identity:
    """What a board said about itself when asked, and whether that is the project root recorded."""

    reachable: bool
    verified: bool
    ticket_prefix: str = ""
    report_groups: bool = False
    problem: str = ""


def registry_dir(environ: Mapping[str, str] = os.environ) -> Path:
    configured = str(environ.get(REGISTRY_DIR_ENV) or "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_REGISTRY_DIR


def registry_record(slug: str, directory: Path) -> dict[str, Any]:
    """`slug`'s registry record, or {} when there is none that names itself `slug`."""
    if not slug or "/" in slug or slug.startswith("."):
        return {}
    try:
        raw = json.loads((directory / f"{slug}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema") != REGISTRY_SCHEMA or raw.get("slug", slug) != slug:
        return {}
    return raw


def _link(parent: str, held: Mapping[str, Any], source: str, directory: Path) -> tuple[Link | None, str]:
    record = registry_record(parent, directory)
    if not record:
        return None, f"its report parent {parent} is not registered on this host"
    url = str(held.get("url") or "").strip().rstrip("/")
    if not url:
        return None, f"root's record of its report parent {parent} names no board URL"
    return Link(parent, str(record.get("name") or parent), url, str(held.get("ticket_prefix") or "").strip(),
                source), ""


def immediate_parent(slug: str, directory: Path) -> tuple[Link | None, str]:
    """(`slug`'s report parent or None, problem). None with no problem is a project with no parent."""
    record = registry_record(slug, directory)
    if not record:
        return None, f"{slug} is not registered on this host"
    held = record.get(PARENT_KEY)
    if isinstance(held, dict):
        parent = str(held.get("project") or "").strip()
        return _link(parent, held, "declared", directory) if parent else (None, "")
    outcome = record.get(OUTCOME_KEY)
    if isinstance(outcome, dict) and outcome.get("state") == CONNECTED and outcome.get("board"):
        return _link(str(outcome["board"]), outcome, "connected", directory)
    return None, ""


def chain(slug: str, directory: Path, *, first: Link | None = None) -> tuple[list[Link], str]:
    """`slug`'s ancestors, nearest first, and why the chain stops short ("" when it is whole).

    With `first`, the chain `slug` would have with that parent: what root checks before declaring it."""
    links: list[Link] = []
    current, path = slug, [slug]
    while True:
        if first is not None and current == slug:
            link, problem = first, ""
        else:
            link, problem = immediate_parent(current, directory)
        if problem:
            return links, problem if current == slug else f"{current}, an ancestor of {slug}: {problem}"
        if link is None:
            return links, ""
        if link.project == current:
            return links, f"{current} names itself as its own report parent"
        if link.project in path:
            return links, f"its report ancestry is a cycle: {' -> '.join([*path, link.project])}"
        for held in links:
            if held.url == link.url or (link.ticket_prefix and held.ticket_prefix == link.ticket_prefix):
                return links, (f"{held.project} and {link.project} are recorded with the same "
                               f"{'board' if held.url == link.url else 'ticket prefix'}, so they are not two boards")
        links.append(link)
        path.append(link.project)
        if len(links) > MAX_DEPTH:
            return links[:MAX_DEPTH], f"its report ancestry is deeper than {MAX_DEPTH} boards"
        current = link.project


def ask_identity(url: str, *, timeout: float = PROBE_SECONDS) -> tuple[dict[str, Any], str]:
    """(the public client configuration a board serves to anyone, "") or ({}, why not). Nothing is written."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/client-config", timeout=timeout) as response:
            answer = json.loads(response.read(65536).decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return {}, f"it answered HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {}, f"it did not answer ({getattr(exc, 'reason', exc)})"
    return (answer, "") if isinstance(answer, dict) else ({}, "its answer is not a board's")


def verify(link: Link, *, ask: Callable[[str], tuple[dict[str, Any], str]] | None = None) -> Identity:
    """Whether the board root recorded for `link` answers, and answers as that project."""
    answer, problem = (ask or ask_identity)(link.url)
    if problem:
        return Identity(False, False, problem=f"{link.name} ({link.project}) cannot be reached: {problem}")
    named, prefix = str(answer.get("project") or ""), str(answer.get("ticket_prefix") or "")
    groups = answer.get("report_groups") is True
    if named != link.project:
        return Identity(True, False, prefix, groups,
                        f"the board recorded for {link.project} answers as {named or 'no project'!r}")
    if not prefix or (link.ticket_prefix and prefix != link.ticket_prefix):
        return Identity(True, False, prefix, groups,
                        f"{link.project}'s board names ticket prefix {prefix or 'none'!r}, and root recorded "
                        f"{link.ticket_prefix!r}")
    return Identity(True, True, prefix, groups)


def read_token(path: Path) -> tuple[str, str]:
    """(the report token a credential file holds, "") or ("", why there is none). The token is never printed."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "", "missing"
    except (OSError, UnicodeDecodeError) as exc:
        return "", f"unreadable ({getattr(exc, 'strerror', None) or exc})"
    for line in text.splitlines():
        name, separator, value = line.strip().partition("=")
        if separator and name.strip() in TOKEN_KEYS:
            token = value.strip().strip("\"'")
            return (token, "") if token else ("", "empty")
    return "", "empty"


def credential_path(link: Link, *, immediate: bool, token_file: str) -> Path | None:
    """Where this project's credential for `link`'s board is: its own file, or, for the immediate parent of a
    project root has not given per-ancestor files yet, the one credential it always had. Never another board's."""
    if not str(token_file or "").strip():
        return None
    base = Path(token_file).expanduser()
    own = base.parent / CREDENTIAL_DIR_NAME / f"{link.project}.env"
    return base if immediate and not os.path.lexists(own) else own


def credential_state(path: Path | None, project: str) -> tuple[bool, str]:
    """(usable, what it is) for a credential `project` would send. Never its value."""
    from .report_follow_up import named_project

    if path is None:
        return False, "none: this pane names no report credential"
    token, problem = read_token(path)
    if problem:
        return False, problem
    bound = named_project(token)
    if bound and bound != project:
        return False, f"bound to {bound}, not {project}"
    return True, (f"bound to {project}" if bound else "the board's shared token: it files, and cannot follow up")


def issued_state(project: str, ancestor: str, directory: Path) -> tuple[bool, str]:
    """What root's records say about the credential it issued `project` for `ancestor`: all a board service, which
    cannot read the tenant's files, can say."""
    held = registry_record(project, directory).get(CREDENTIALS_KEY)
    note = held.get(ancestor) if isinstance(held, dict) else None
    if not isinstance(note, dict) or not note.get("issued_at"):
        return False, "root's records show none issued"
    return True, f"issued by root at {note['issued_at']}" + (", bound to this project" if note.get("bound") else "")


def describe(
    project: str,
    *,
    directory: Path,
    token_file: str = "",
    environment_url: str = "",
    ask: Callable[[str], tuple[dict[str, Any], str]] | None = None,
    from_records: bool = False,
) -> dict[str, Any]:
    """The read-only answer to "where can this project report?": the immediate parent and every ancestor, nearest
    first, each with its project, name, ticket prefix, verified identity, reachability and credential.

    `from_records` is the board service's view: each credential as root's records say it was issued (`issued`), since
    it reads no tenant file; only a pane's own view says whether one is `usable`."""
    links, problem = chain(project, directory)
    entries = []
    for position, link in enumerate(links, start=1):
        identity = verify(link, ask=ask)
        if from_records:
            usable, said = issued_state(project, link.project, directory)
        else:
            usable, said = credential_state(credential_path(link, immediate=position == 1, token_file=token_file),
                                            project)
        entries.append({
            "position": position, "project": link.project, "name": link.name,
            "ticket_prefix": identity.ticket_prefix or link.ticket_prefix, "recorded_as": link.source,
            "identity_verified": identity.verified, "reachable": identity.reachable,
            "report_groups": identity.report_groups, "problem": identity.problem,
            # The board service's view says what root issued, never that a file it cannot read is usable.
            "credential": {"issued" if from_records else "usable": usable, "state": said},
        })
    note = ""
    if not links and not problem and environment_url:
        note = (f"root's records name no report parent for {project}; this pane's environment still files to "
                f"{environment_url}, as before")
    return {"project": project, "immediate_parent": entries[0] if entries else None, "ancestors": entries,
            "problem": problem, "note": note}


def describe_text(answer: dict[str, Any]) -> str:
    """`describe` for a person: names first, never a URL or a token."""
    project, entries = answer["project"], answer["ancestors"]
    lines = [f"Report ancestry of {project}"
             + (f": {len(entries)} ancestor{'s' if len(entries) != 1 else ''}, nearest first" if entries else "")]
    if not entries:
        lines.append("  no report parent" + (f": {answer['note']}" if answer.get("note") else ""))
    for entry in entries:
        identity = ("identity verified" if entry["identity_verified"] else
                    f"NOT VERIFIED: {entry['problem']}") if entry["reachable"] else f"UNREACHABLE: {entry['problem']}"
        credential = entry["credential"]
        lines.append(f"  {entry['position']}. {entry['name']} ({entry['project']}, tickets "
                     f"{entry['ticket_prefix'] or '?'}-N){' -- immediate parent' if entry['position'] == 1 else ''}")
        lines.append(f"     board: {identity}")
        lines.append(f"     credential: {'usable, ' if credential['usable'] else 'NOT USABLE: '}{credential['state']}")
    if answer.get("problem"):
        lines.append(f"  the chain stops here: {answer['problem']}")
    if entries:
        names = ", ".join(entry["name"] for entry in entries)
        lines.append(f"file-report files to {entries[0]['name']} unless told otherwise: --to <project> for one "
                     f"ancestor, --to all for All ancestors ({names}). Ancestry is not permission: each report is "
                     "previewed and confirmed.")
    return "\n".join(lines)
