"""An optional plan gate: a designer-owned, audited, User-approved three-level plan the Director scopes from (SYRD-843).

sapi's implementation plan started with the right structure -- phase, deliverable, work packet -- and decayed into a
12,000-line log in five weeks: one writer (the Director, editing it while scoping each ticket), no review of the whole,
and the plan doubling as backlog and decision record. Like the documentation gate (documentation_gate.py), this is an
opt-in composer over the tenant's own declared document; a tenant that does not enable it is unchanged.

**The plan.** One Markdown file (`plan.file`, default docs/implementation-plan.md) in three levels: milestone, then
deliverable, then ticket. Milestones and deliverables have stable IDs; every deliverable states acceptance criteria,
dependencies and an owner specialty; only the current and next milestone are detailed. It holds no status, history or
rulings: progress is the board's (a deliverable's tickets and their states), rulings go to the decision log
(`plan.decision_log`), follow-ups to backlog tickets. The board reads one thing from it, the **index**: exactly one
fenced block tagged `plan-index`, holding JSON (`parse_plan_index`):

    ```plan-index
    {"milestones": [
      {"id": "M1", "title": "Ingest", "detailed": true,
       "deliverables": [{"id": "D1.1", "title": "Reader"}, {"id": "D1.2", "title": "Indexer"}]},
      {"id": "M2", "title": "Query", "detailed": false, "deliverables": []}
    ]}
    ```

It is read at the approved commit -- the commit the last plan ticket was closed on -- and nowhere else
(plan_candidate.py records it; the database keeps it, pgu1052).

**What `with_plan_gate` adds to the document:**

* the `plan_change` gate flag, off by default: the mark of a plan ticket, the only way the plan changes;
* the plan owner as an Implementation owner. With `owner`, an existing role that already owns Implementation, or the
  project's designer, made an Implementation owner by `designer_implementation` with `plan_change` as its routing
  scope. With `designer`, a new detached `designer` role (kind `draft`), copied from a template role, made the same;
* the top-level `plan` policy: `{"owner", "file", "decision_log", "max_lines"}`.

No stage is added. A plan ticket goes through the ordinary path -- the plan owner implements it, Audit reviews the whole
plan against its checklist, the User approves it in User Review, the Director signs it off and closes it -- and the
database refuses to close one the User did not accept. `without_plan_gate` removes the flag and the policy (and the
designer's routing scope), and the database refuses that while a plan ticket is open. `validate_plan_gate` is the shape
check workflow_config runs on every document.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable

FLAG = "plan_change"
POLICY_KEY = "plan"
DESIGNER_ROLE = "designer"
DEFAULT_FILE = "docs/implementation-plan.md"
DEFAULT_DECISION_LOG = "docs/decision-log.md"
DEFAULT_MAX_LINES = 1500
MAX_LINES_RANGE = (100, 20000)
INDEX_TAG = "plan-index"
_PATH = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*\.md$")
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,31}$")
_FENCE = re.compile(r"^```" + re.escape(INDEX_TAG) + r"[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
#: Roles that can never own the plan: the board's own identities and the single-purpose roles.
NOT_OWNERS = frozenset({"director", "user", "unassigned", "router", "librarian", "publisher", "researcher"})


class PlanIndexError(ValueError):
    """The plan's index is missing, duplicated or malformed; the message says which, for the plan's owner to fix."""


def parse_plan_index(text: str) -> dict[str, Any]:
    """The plan's index: its one `plan-index` block, checked. Raises PlanIndexError, naming what is wrong.

    The result is `{"milestones": [{"id", "title", "detailed", "deliverables": [{"id", "title"}]}]}`, in plan order.
    """
    blocks = _FENCE.findall(text or "")
    if len(blocks) != 1:
        raise PlanIndexError(f"the plan has {len(blocks)} `{INDEX_TAG}` blocks; it has exactly one, a fenced block "
                             f"tagged {INDEX_TAG} holding the JSON index of its milestones and deliverables")
    try:
        index = json.loads(blocks[0])
    except ValueError as exc:
        raise PlanIndexError(f"the `{INDEX_TAG}` block is not JSON: {exc}") from None
    return check_index(index)


def check_index(index: Any) -> dict[str, Any]:
    """`index` as the board keeps it, or PlanIndexError: every milestone and deliverable with a stable, unique ID,
    every detailed milestone with a deliverable, and nothing else in it -- no status, history or rulings."""
    if not isinstance(index, dict) or set(index) != {"milestones"} or not isinstance(index["milestones"], list) \
            or not index["milestones"]:
        raise PlanIndexError('the index is {"milestones": [...]}, with at least one milestone, and nothing else')
    seen: set[str] = set()
    milestones = []
    for position, milestone in enumerate(index["milestones"], 1):
        if not isinstance(milestone, dict) or set(milestone) != {"id", "title", "detailed", "deliverables"}:
            raise PlanIndexError(f"milestone {position} is {{\"id\", \"title\", \"detailed\", \"deliverables\"}} and "
                                 "nothing else: status, history and rulings never go in the plan")
        mid, title, detailed, deliverables = (milestone[k] for k in ("id", "title", "detailed", "deliverables"))
        if not isinstance(mid, str) or not _ID.fullmatch(mid) or mid in seen:
            raise PlanIndexError(f"milestone {position}'s id {mid!r} is not a unique stable id (letters, digits, "
                                 "dots, dashes; 32 at most)")
        seen.add(mid)
        if not isinstance(title, str) or not title.strip() or type(detailed) is not bool \
                or not isinstance(deliverables, list):
            raise PlanIndexError(f"milestone {mid} needs a title, `detailed` true or false, and a deliverables list")
        if detailed and not deliverables:
            raise PlanIndexError(f"milestone {mid} is detailed, so it lists its deliverables")
        listed = []
        for deliverable in deliverables:
            if not isinstance(deliverable, dict) or set(deliverable) != {"id", "title"}:
                raise PlanIndexError(f"a deliverable of {mid} is {{\"id\", \"title\"}} and nothing else")
            did, dtitle = deliverable["id"], deliverable["title"]
            if not isinstance(did, str) or not _ID.fullmatch(did) or did in seen:
                raise PlanIndexError(f"deliverable {did!r} of {mid} is not a unique stable id")
            if not isinstance(dtitle, str) or not dtitle.strip():
                raise PlanIndexError(f"deliverable {did} of {mid} needs a title")
            seen.add(did)
            listed.append({"id": did, "title": dtitle.strip()})
        milestones.append({"id": mid, "title": title.strip(), "detailed": detailed, "deliverables": listed})
    return {"milestones": milestones}


def policy(cfg: dict[str, Any] | None) -> dict[str, Any] | None:
    value = (cfg or {}).get(POLICY_KEY)
    return value if isinstance(value, dict) else None


def plan_owner(cfg: dict[str, Any] | None) -> str | None:
    return (policy(cfg) or {}).get("owner")


def _implementation(cfg: dict[str, Any]) -> dict[str, Any] | None:
    return next((s for s in cfg["stages"] if s["kind"] == "implementation"), None)


def _check_settings(plan_file: str, decision_log: str, max_lines: int) -> None:
    for what, path in (("plan file", plan_file), ("decision log", decision_log)):
        if not isinstance(path, str) or not _PATH.fullmatch(path) or any(p in (".", "..") for p in path.split("/")):
            raise ValueError(f"plan gate: the {what} is a repository-relative Markdown path such as "
                             f"{DEFAULT_FILE}, not {path!r}")
    if plan_file == decision_log:
        raise ValueError("plan gate: the plan and the decision log are two files")
    if type(max_lines) is not int or not MAX_LINES_RANGE[0] <= max_lines <= MAX_LINES_RANGE[1]:
        raise ValueError(f"plan gate: the plan's size limit is {MAX_LINES_RANGE[0]} to {MAX_LINES_RANGE[1]} lines, "
                         f"not {max_lines!r}")


def with_plan_gate(document: dict[str, Any], *, owner: str | None = None, designer: dict[str, Any] | None = None,
                   plan_file: str = DEFAULT_FILE, decision_log: str = DEFAULT_DECISION_LOG,
                   max_lines: int = DEFAULT_MAX_LINES) -> dict[str, Any]:
    """`document` with the plan gate, owned by the existing `owner` or a new detached Designer (`designer` =
    `{"runtime", "template_role", "slot"}`), one of the two. Raises ValueError, changing nothing, when it cannot."""
    from scripts.ticket_board.designer_implementation import with_designer_implementation
    from scripts.ticket_board.role_kinds import IMPLEMENTER, has_kind
    from scripts.ticket_board.workflow_config import RUNTIMES, validate

    cfg = validate(copy.deepcopy(document))
    if policy(cfg) is not None:
        raise ValueError(f"plan gate: this workflow already runs the plan gate, owned by {plan_owner(cfg)}; disable it "
                         "first to change its owner")
    if (owner is None) == (designer is None):
        raise ValueError("plan gate: name the plan's owner (--owner, an existing role) or a runtime for a new Designer "
                         "(--runtime) -- one of the two")
    if FLAG in cfg["flags"]:
        raise ValueError(f"plan gate: this workflow already declares {FLAG}; nothing was changed")
    _check_settings(plan_file, decision_log, max_lines)
    impl = _implementation(cfg)
    if impl is None:
        raise ValueError("plan gate: this workflow has no Implementation stage for plan tickets")
    roles = {r["name"]: r for r in cfg["roles"]}
    if designer is not None:
        project = str(cfg.get("project") or "").strip()
        runtime = designer.get("runtime")
        if runtime not in RUNTIMES:
            raise ValueError(f"designer: runtime must be one of {', '.join(sorted(RUNTIMES))}, not {runtime!r}")
        if not project:
            raise ValueError("designer: this workflow names no project, so the Designer's pane target cannot be named")
        existing = roles.get(DESIGNER_ROLE)
        if existing is not None and (existing["active"] or existing["kind"] != "draft"):
            raise ValueError("designer: this workflow already has a designer; name it with --owner designer instead")
        template = designer.get("template_role") or impl["owners"][0]
        if template not in roles or template == DESIGNER_ROLE:
            raise ValueError(f"designer: template role {template!r} is not a role of this workflow")
        new = {
            "name": DESIGNER_ROLE, "label": "Designer", "kind": "draft", "active": True,
            "capabilities": [c for c in roles[template]["capabilities"] if c != "request_publication"],
            "runtime": runtime, "target": f"{project}-{DESIGNER_ROLE}:0.0", "slot": designer.get("slot"),
            "serial": False, "template_role": template, "presentation_label": "Designer",
        }
        cfg["roles"] = [new if r["name"] == DESIGNER_ROLE else r for r in cfg["roles"]] if existing else \
            cfg["roles"] + [new]
        owner = DESIGNER_ROLE
    assert owner is not None
    owner = owner.strip().lower()
    chosen = {r["name"]: r for r in cfg["roles"]}.get(owner)
    if chosen is None or not chosen["active"] or owner in NOT_OWNERS:
        raise ValueError(f"plan gate: {owner!r} is not an active role of this workflow that can own the plan")
    if not chosen.get("target"):
        raise ValueError(f"plan gate: {owner} has no pane, so the board could not tell it about plan tickets")
    if owner == DESIGNER_ROLE and not (has_kind(chosen, IMPLEMENTER) and owner in impl["owners"]):
        cfg = with_designer_implementation(cfg, routing_flag=FLAG)
    elif not (has_kind(chosen, IMPLEMENTER) and owner in impl["owners"]):
        raise ValueError(f"plan gate: {owner} does not own Implementation, where plan tickets are worked; name an "
                         "Implementation owner or the designer")
    cfg["flags"].setdefault(FLAG, {"kind": "gate", "default": False})
    cfg[POLICY_KEY] = {"owner": owner, "file": plan_file, "decision_log": decision_log, "max_lines": max_lines}
    return validate(cfg)


def without_plan_gate(document: dict[str, Any]) -> dict[str, Any]:
    """`document` without the plan gate. The plan owner keeps its roles; the designer's routing scope goes."""
    from scripts.ticket_board.workflow_config import validate

    cfg = validate(copy.deepcopy(document))
    if policy(cfg) is None:
        raise ValueError("plan gate: this workflow does not run the plan gate; nothing was changed")
    cfg.pop(POLICY_KEY)
    for role in cfg["roles"]:
        if (role.get("routing_scope") or {}).get("flag") == FLAG:
            role.pop("routing_scope")
    cfg["flags"].pop(FLAG, None)
    return validate(cfg)


def validate_plan_gate(cfg: dict[str, Any], need: Callable[[bool, str], None]) -> None:
    """The gate's shape, wherever it is declared."""
    from scripts.ticket_board.role_kinds import IMPLEMENTER, has_kind

    value = cfg.get(POLICY_KEY)
    if value is None:
        return
    need(isinstance(value, dict) and set(value) == {"owner", "file", "decision_log", "max_lines"},
         'invalid plan gate: its policy is {"owner", "file", "decision_log", "max_lines"}')
    try:
        _check_settings(value["file"], value["decision_log"], value["max_lines"])
    except ValueError as exc:
        need(False, f"invalid plan gate: {exc}")
    need(cfg["flags"].get(FLAG) == {"kind": "gate", "default": False},
         f"invalid plan gate: {FLAG} is a gate that is off unless the director turns it on")
    roles = {r["name"]: r for r in cfg["roles"]}
    impl = _implementation(cfg) or {"owners": []}
    owner = roles.get(value["owner"])
    need(owner is not None and owner["active"] and value["owner"] not in NOT_OWNERS and bool(owner.get("target"))
         and has_kind(owner, IMPLEMENTER) and value["owner"] in impl["owners"],
         "invalid plan gate: its owner is an active Implementation owner with a pane, never a single-purpose role")
