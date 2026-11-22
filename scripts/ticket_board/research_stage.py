"""An optional research stage: a question, an evidence-reviewed finding, and a hand-back to the decider (SYRD-842).

Some projects need a spike before they can plan or build: a format to
reverse-engineer, a tool to evaluate, a corpus to measure. Without a stage the
Director ran these by hand outside the board, so nothing marked the work as
research, nothing reviewed its evidence and nothing handed the finding back.
Like the documentation gate (documentation_gate.py), this is an opt-in
composer over the tenant's own declared document; a tenant that does not
enable it is unchanged.

`with_research_stage` adds:

* the `needs_research` gate flag, off by default. The Director turns it on with
  a research brief -- the question, a timebox and what a usable answer looks
  like (`ticket-board-write research-brief`, or `create-ticket --research`).
  The database refuses a ticket into Research without the flag and a brief
  with a question, and refuses the ordinary route into Implementation for a
  ticket that carries the flag (pgu1051);
* the `research` stage (kind `system`), owned by the research role: an existing
  role (`role`), or a new detached `researcher` (`researcher`), copied from a
  template role like the Librarian and Router. The Director's `start_research`
  moves a research ticket there from triage;
* the researcher's three ways out, owner-scoped:
  - `submit_finding`, carrying a findings commit: one Markdown file, the
    ticket's own, under the project's research directory
    (`research_path`), which research_candidate checks before anything moves;
  - `submit_finding_without_commit`, for a finding with nothing to keep: the
    reason is the finding;
  - `research_hand_back` to triage, with why the question is not researchable
    as written;
* when the project runs Audit, `research_review` (kind `review`), owned by the
  Audit stage's owners and gated by the ticket's own `needs_audit`: both
  submissions enter it, and with the gate off it skips to triage. Audit's
  `research_sign_off` approves the finding's evidence into triage, and its
  `research_kick_back` returns it to the researcher with what is missing.
  The review holds no sign-off flag -- Audit's decision is the move itself, as
  the Director's is on a Director-owned documentation gate -- so no sign-off
  joins the ones every other gate clears, and turning the stage off leaves no
  review obligation behind on tickets that never did research;
  Without Audit both submissions go straight to triage;
* the Director's two decisions on a finding back in triage: `return_finding` to
  the researcher with what is missing, and `close_on_finding` to the terminal
  stage work is finished in, with the reason. Filing the implementation tickets
  the finding justifies is the Director's ordinary create-ticket; nothing is
  created automatically. The database refuses both for a ticket without the
  flag;
* on a board that runs the Publisher (SYRD-849), a finding with a commit reaches
  main through it. The Director accepts the finding with `publish_finding`,
  from triage into publication, with the reason, in place of close_on_finding.
  The Publisher publishes the findings commit as it publishes any candidate:
  - `finding_published` moves it on to the terminal stage;
  - `finding_publication_return` returns it to the researcher. A stale base,
    for example, is rebased and resubmitted;
  - an escalation goes to `publication_failed` as any other does. There the
    Director retries it, or returns it to the researcher with
    `finding_publication_kick_back`.

  The database picks the finding moves for a ticket the Director accepted
  this way (pgu1054). A no-commit finding still closes with
  close_on_finding. Findings take no documentation review: the documentation
  gate does not apply to research tickets. The moves are composed whichever of
  the two composers runs first: with_publisher and without_publisher
  recompose them (`recompose_research`);
* a defer (and any other Director-only exit to parking or a terminal stage) out
  of both new stages, under the names the document already uses for
  Implementation's;
* `research` among the stages the Director may wait on the User in
  (`user_waits`), when the project has an active User;
* the top-level `research` policy, `{"directory": <repository-relative dir>}`.

A research ticket never enters User Review: nothing composed here leads there.

`without_research_stage` removes all of it, names both stages in
`remove_stages` (the board refuses that while a ticket is in either), and
leaves a researcher role declared but inactive. `validate_research_stage` is
the shape check workflow_config runs on every document.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Callable, Iterable

RESEARCH_STAGE = "research"
REVIEW_STAGE = "research_review"
GATE = "needs_research"
AUDIT_GATE = "needs_audit"
START = "start_research"
SUBMIT = "submit_finding"
SUBMIT_NO_CODE = "submit_finding_without_commit"
HAND_BACK = "research_hand_back"
SIGN_OFF = "research_sign_off"
KICK_BACK = "research_kick_back"
RETURN = "return_finding"
CLOSE = "close_on_finding"
#: SYRD-849: a finding with a commit through the Publisher -- the Director's acceptance, the Publisher's two finding
#: moves, and the Director's return of a failed publication to the researcher.
PUBLISH = "publish_finding"
PUBLISHED = "finding_published"
PUBLICATION_RETURN = "finding_publication_return"
PUBLICATION_KICK_BACK = "finding_publication_kick_back"
#: The Publisher's moves this composer declares, beside its own three.
PUBLISHER_FINDING_ACTIONS = (PUBLISHED, PUBLICATION_RETURN)
PUBLICATION_ACTIONS = (PUBLISH, *PUBLISHER_FINDING_ACTIONS, PUBLICATION_KICK_BACK)
#: The researcher's own moves, the review's, and the Director's: every action this composer declares.
RESEARCHER_ACTIONS = (SUBMIT, SUBMIT_NO_CODE, HAND_BACK)
REVIEW_ACTIONS = (SIGN_OFF, KICK_BACK)
DIRECTOR_ACTIONS = (START, RETURN, CLOSE)
ACTIONS = (*DIRECTOR_ACTIONS, *RESEARCHER_ACTIONS, *REVIEW_ACTIONS, *PUBLICATION_ACTIONS)
RESEARCHER_ROLE = "researcher"
RESEARCHER_KIND = "support"
#: A Researcher comments and records waits; it routes, edits, files and publishes nothing on the board.
RESEARCHER_CAPABILITIES = ("add_comment", "await_role", "clear_awaiting_role")
POLICY_KEY = "research"
DEFAULT_DIRECTORY = "docs/research"
#: Roles that can never be the research role: the board's own identities and the single-purpose roles.
NOT_RESEARCHERS = frozenset({"director", "user", "unassigned", "router", "librarian", "publisher"})
_DIRECTORY = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
#: The Publisher's stages and role (publisher_stage.py), named here so this module imports nothing from it.
PUBLICATION, PUBLICATION_FAILED, PUBLISHER = "publication", "publication_failed", "publisher"


#: The timebox a brief may set, in seconds: 15 minutes to 30 days, as the database checks.
TIMEBOX_RANGE = (900, 2592000)
_DURATION = re.compile(r"^\s*(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*$", re.IGNORECASE)


def timebox_seconds(text: str) -> int:
    """`2h`, `90m`, `1d 6h`, `3d`: a timebox in whole seconds. Raises ValueError for anything else or out of range."""
    match = _DURATION.fullmatch(str(text or ""))
    if not match or not any(match.groups()):
        raise ValueError(f"a research timebox is a duration such as 90m, 4h or 2d, not {text!r}")
    days, hours, minutes = (int(part or 0) for part in match.groups())
    seconds = days * 86400 + hours * 3600 + minutes * 60
    if not TIMEBOX_RANGE[0] <= seconds <= TIMEBOX_RANGE[1]:
        raise ValueError(f"a research timebox is from 15 minutes to 30 days, not {text!r}")
    return seconds


def research_path(directory: str, ticket_id: str) -> str:
    """Where a ticket's findings file lives: `<directory>/<TICKET>.md`, the ticket id as the board writes it."""
    return f"{directory.strip().strip('/')}/{ticket_id.strip().upper()}.md"


def outside_finding(entries: Iterable[tuple[str, str, str, str]], expected: str) -> list[str]:
    """Each change in `entries` -- (old mode, new mode, status, path) -- a findings commit may not make.

    A findings commit adds or edits exactly `expected`, as an ordinary non-executable file, and nothing else.
    """
    problems = []
    for old_mode, new_mode, status, path in entries:
        if path != expected:
            problems.append(f"{path} (a findings commit changes only {expected})")
        elif status.startswith("D") or new_mode != "100644" or old_mode not in ("100644", "000000"):
            problems.append(f"{path} (mode {old_mode} -> {new_mode}, {status}; the findings file is added or edited "
                            "as an ordinary, non-executable file)")
    return problems


#: The Researcher's remit, stored as its onboarding prompt. docs/onboarding/switchyard-researcher-guide.md is the
#: full guide; this names what it answers, how, and what is never its.
RESEARCHER_PROMPT = """You are the Researcher. You answer one research question per ticket, with evidence, and hand the finding back to whoever decides what to do with it. You build nothing.

A ticket reaches you, in the Research stage, only when the Director has written its research brief: the question, a timebox, and what a usable answer looks like. Read that one ticket -- the brief is in its comments and in the notice that brought you here -- and nothing it does not need:

    ticket-board-read ticket <ID> --json

Work the question inside the timebox. Run what you measure, read what you cite, and keep the commands, sources and numbers: Audit reviews the evidence behind every claim, not the prose. Then submit exactly one finding, in one of two forms:

    ticket-board-write submit-finding <ID> --commit <your commit>
    ticket-board-write submit-finding-without-commit <ID> --reason-file PATH

A findings commit changes one file, the ticket's own under the project's research directory (docs/research/<ID>.md unless the project chose another; the notice names it), on a branch named after the ticket, published with `switchyard-publish-candidate --worktree <dir> --commit <sha> --ref roles/<you>/<id> --role <you>`. Use the no-commit form only when nothing is worth keeping; then the reason is the finding. Either way the finding names the question, the evidence (commands, sources, measurements), the answer, your confidence, and a recommendation.

When the question cannot be researched as written -- it is ambiguous, it needs data only the User has, or it is not a research question -- hand it back instead of guessing:

    ticket-board-write research-hand-back <ID> --reason-file PATH

You do not implement, change product code, file follow-up tickets, route, reassign, publish a release, or touch any other ticket. The Director decides what the finding justifies. When your submission is recorded, stop and wait for the next notification."""


def _transition(source: str, destination: str, action: str, label: str, actors: list[str], *, primitive: str = "move",
                owner_scoped: bool = False, require_commit: bool = False, require_reason: bool = False,
                clear_signoffs: list[str] | None = None) -> dict[str, Any]:
    return {
        "from": source, "to": destination, "action": action, "label": label, "actors": actors,
        "primitive": primitive, "owner_scoped": owner_scoped, "require_commit": require_commit,
        "require_reason": require_reason, "clear_signoffs": clear_signoffs or [], "allow_no_code": False,
        "relays_decision_of": None,
    }


def research_stage(cfg: dict[str, Any] | None) -> dict[str, Any] | None:
    return next((s for s in (cfg or {}).get("stages", []) if s["name"] == RESEARCH_STAGE), None)


def review_stage(cfg: dict[str, Any] | None) -> dict[str, Any] | None:
    return next((s for s in (cfg or {}).get("stages", []) if s["name"] == REVIEW_STAGE), None)


def research_owner(cfg: dict[str, Any] | None) -> str | None:
    """Who researches on this board, or None without a research stage."""
    stage = research_stage(cfg)
    return stage["owners"][0] if stage and stage["owners"] else None


def research_directory(cfg: dict[str, Any] | None) -> str:
    policy = (cfg or {}).get(POLICY_KEY)
    return policy["directory"] if isinstance(policy, dict) and isinstance(policy.get("directory"), str) \
        else DEFAULT_DIRECTORY


def _implementation(cfg: dict[str, Any]) -> dict[str, Any] | None:
    return next((s for s in cfg["stages"] if s["kind"] == "implementation"), None)


def _audit(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The stage the project's Audit reviews in: the review gated by needs_audit, when there is exactly one."""
    found = [s for s in cfg["stages"] if s["kind"] == "review" and s["gate"] == AUDIT_GATE
             and s["name"] != REVIEW_STAGE and s["signoff"] is not None]
    return found[0] if len(found) == 1 else None


def _routes(cfg: dict[str, Any], impl: str) -> list[dict[str, Any]]:
    """The Director's ordinary routes into Implementation: moves from an owned, non-review stage before it."""
    from scripts.ticket_board.workflow_config import DIRECTOR_ROLE, parking_stage_names

    stages = {s["name"]: s for s in cfg["stages"]}
    parking = parking_stage_names(cfg)
    return [t for t in cfg["transitions"]
            if t["to"] == impl and t["primitive"] == "move" and t["actors"] == [DIRECTOR_ROLE]
            and t["from"] not in parking and stages[t["from"]]["kind"] in ("system", "draft")
            and t["from"] != RESEARCH_STAGE]


def _triage(cfg: dict[str, Any], impl: str) -> str:
    found = sorted({t["from"] for t in _routes(cfg, impl)})
    if len(found) != 1:
        raise ValueError(f"research stage: cannot tell where the Director routes work into Implementation from "
                         f"({', '.join(found) or 'no route found'}); add the stage by hand and apply the document")
    return found[0]


def _finished(cfg: dict[str, Any], triage: str) -> str:
    """The terminal stage finished work goes to: the one the Director cannot already move triage work into."""
    stages = {s["name"]: s for s in cfg["stages"]}
    entered = {t["to"] for t in cfg["transitions"] if t["from"] == triage and t["action"] != CLOSE}
    found = sorted(name for name, s in stages.items() if s["terminal"] and name not in entered)
    if len(found) != 1:
        raise ValueError(f"research stage: cannot tell which terminal stage a finding closes into "
                         f"({', '.join(found) or 'none found'}); add the stage by hand and apply the document")
    return found[0]


def _director_exits(cfg: dict[str, Any], source: str, impl: str) -> list[dict[str, Any]]:
    """Implementation's Director-only exits to parking or a terminal stage, out of `source` instead."""
    from scripts.ticket_board.workflow_config import DIRECTOR_ROLE, parking_stage_names

    stages = {s["name"]: s for s in cfg["stages"]}
    parking = parking_stage_names(cfg)
    return [dict(copy.deepcopy(t), **{"from": source}) for t in cfg["transitions"]
            if t["from"] == impl and t["actors"] == [DIRECTOR_ROLE] and t["primitive"] == "move"
            and (t["to"] in parking or stages[t["to"]]["terminal"])]


def _composed(cfg: dict[str, Any], owner: str, triage: str, finished: str, audit: dict[str, Any] | None,
              impl: str) -> list[dict[str, Any]]:
    """Every transition the stage declares, as composed: what validate_research_stage holds them to."""
    from scripts.ticket_board.workflow_config import DIRECTOR_ROLE

    director = [DIRECTOR_ROLE]
    after = REVIEW_STAGE if audit is not None else triage
    moves = [
        _transition(triage, RESEARCH_STAGE, START, "Start research", director),
        _transition(RESEARCH_STAGE, after, SUBMIT, "Submit finding", [owner], owner_scoped=True, require_commit=True),
        _transition(RESEARCH_STAGE, after, SUBMIT_NO_CODE, "Submit finding without commit", [owner],
                    owner_scoped=True, require_reason=True),
        _transition(RESEARCH_STAGE, triage, HAND_BACK, "Hand back: not researchable as written", [owner],
                    owner_scoped=True, require_reason=True),
        _transition(triage, RESEARCH_STAGE, RETURN, "Return finding to the researcher", director,
                    require_reason=True),
        _transition(triage, finished, CLOSE, "Close on the finding", director, require_reason=True),
    ]
    if audit is not None:
        reviewers = list(audit["owners"])
        moves += [
            _transition(REVIEW_STAGE, triage, SIGN_OFF, "Sign off the finding's evidence", reviewers,
                        owner_scoped=True),
            _transition(REVIEW_STAGE, RESEARCH_STAGE, KICK_BACK, "Return finding: evidence missing", reviewers,
                        primitive="return", owner_scoped=True, require_reason=True),
        ]
    for source in (RESEARCH_STAGE, *([REVIEW_STAGE] if audit is not None else [])):
        moves += _director_exits(cfg, source, impl)
    if any(s["name"] == PUBLICATION for s in cfg["stages"]):
        moves += [
            _transition(triage, PUBLICATION, PUBLISH, "Accept the finding: publish its commit", director,
                        require_reason=True),
            _transition(PUBLICATION, finished, PUBLISHED, "Finding published to the integration branch",
                        [PUBLISHER], owner_scoped=True, require_commit=True, require_reason=True),
            # Moves, not returns: a return goes back into a review only from the stage that submitted into it.
            _transition(PUBLICATION, RESEARCH_STAGE, PUBLICATION_RETURN,
                        "Return finding: its commit cannot be published as it is", [PUBLISHER],
                        owner_scoped=True, require_reason=True),
            _transition(PUBLICATION_FAILED, RESEARCH_STAGE, PUBLICATION_KICK_BACK, "Return finding to the researcher",
                        director, require_reason=True),
        ]
    return moves


def _shape(cfg: dict[str, Any]) -> tuple[str, str, str, dict[str, Any] | None, str]:
    """(owner, triage, finished, the review's Audit stage or None, implementation) of a declared research stage."""
    impl = _implementation(cfg)
    triage = _triage(cfg, impl["name"])
    return (research_owner(cfg) or "", triage, _finished(cfg, triage),
            _audit(cfg) if review_stage(cfg) is not None else None, impl["name"])


def recompose_research(cfg: dict[str, Any]) -> dict[str, Any]:
    """`cfg` with the research stage's moves composed again for what it declares now, in place (SYRD-849).

    The moves depend on whether the board runs the Publisher, so with_publisher and without_publisher call this
    before they validate: the two composers can be turned on and off in either order. Without a research stage it
    changes nothing.
    """
    if research_stage(cfg) is None:
        return cfg
    owner, triage, finished, audit, impl = _shape(cfg)
    gone = (RESEARCH_STAGE, REVIEW_STAGE)
    cfg["transitions"] = [t for t in cfg["transitions"]
                          if t["action"] not in ACTIONS and t["from"] not in gone and t["to"] not in gone]
    cfg["transitions"] += _composed(cfg, owner, triage, finished, audit, impl)
    return cfg


def with_research_stage(document: dict[str, Any], *, role: str | None = None,
                        researcher: dict[str, Any] | None = None, directory: str = DEFAULT_DIRECTORY) -> dict[str, Any]:
    """`document` with the research stage, owned by the existing `role` or by a new Researcher.

    `researcher` is `{"runtime": <cli>, "template_role": <role or None>, "slot": <display slot or None>}`. Exactly
    one of the two is given. Raises ValueError, changing nothing, when the stage cannot be added safely.
    """
    from scripts.ticket_board.workflow_config import RUNTIMES, validate

    cfg = validate(copy.deepcopy(document))
    if research_stage(cfg) is not None or review_stage(cfg) is not None:
        raise ValueError(f"research stage: this workflow already has a {RESEARCH_STAGE} stage, owned by "
                         f"{research_owner(cfg) or 'nobody'}; disable it first to change who researches")
    if (role is None) == (researcher is None):
        raise ValueError("research stage: name an existing role to research (--role), or a runtime for a new "
                         "Researcher (--runtime) -- one of the two")
    taken = sorted({GATE} & set(cfg["flags"]))
    clashing = sorted({t["action"] for t in cfg["transitions"] if t["action"] in ACTIONS})
    if taken or clashing or POLICY_KEY in cfg:
        raise ValueError(f"research stage: this workflow already declares "
                         f"{', '.join(taken + clashing + ([POLICY_KEY] if POLICY_KEY in cfg else []))}; "
                         "nothing was changed")
    directory = str(directory or "").strip().rstrip("/")
    if not _DIRECTORY.fullmatch(directory) or any(part in (".", "..") for part in directory.split("/")):
        raise ValueError(f"research stage: the research directory is a repository-relative path such as "
                         f"{DEFAULT_DIRECTORY}, not {directory!r}")
    impl = _implementation(cfg)
    if impl is None:
        raise ValueError("research stage: this workflow has no Implementation stage to sit beside")
    triage = _triage(cfg, impl["name"])
    finished = _finished(cfg, triage)
    roles = {r["name"]: r for r in cfg["roles"]}
    if role is not None:
        role = role.strip().lower()
        chosen = roles.get(role)
        if chosen is None or not chosen["active"] or role in NOT_RESEARCHERS or chosen["kind"] in ("user", "router"):
            raise ValueError(f"research stage: {role!r} is not an active role of this workflow that can research; "
                             "name an implementer, reviewer or other working role, or use --runtime")
        if not chosen.get("target"):
            raise ValueError(f"research stage: {role} has no pane, so the board could not tell it about work")
        owner = role
    else:
        assert researcher is not None
        project = str(cfg.get("project") or "").strip()
        runtime = researcher.get("runtime")
        if runtime not in RUNTIMES:
            raise ValueError(f"researcher: runtime must be one of {', '.join(sorted(RUNTIMES))}, not {runtime!r}")
        if not project:
            raise ValueError("researcher: this workflow names no project, so the Researcher's pane target cannot "
                             "be named")
        existing = roles.get(RESEARCHER_ROLE)
        if existing is not None and existing["kind"] != RESEARCHER_KIND:
            raise ValueError("researcher: this workflow already has a role named researcher that is not the "
                             "Researcher; name it with --role instead")
        template = researcher.get("template_role") or impl["owners"][0]
        if template not in roles or template == RESEARCHER_ROLE:
            raise ValueError(f"researcher: template role {template!r} is not a role of this workflow")
        new = {
            "name": RESEARCHER_ROLE, "label": "Researcher", "kind": RESEARCHER_KIND, "active": True,
            "capabilities": list(RESEARCHER_CAPABILITIES), "runtime": runtime,
            "target": f"{project}-{RESEARCHER_ROLE}:0.0", "slot": researcher.get("slot"), "serial": False,
            "ephemeral": True, "template_role": template, "onboarding_prompt": RESEARCHER_PROMPT,
            "presentation_label": "Researcher",
        }
        if existing is not None:  # a Researcher turned off earlier: turn the same role back on
            cfg["roles"] = [new if r["name"] == RESEARCHER_ROLE else r for r in cfg["roles"]]
        else:
            cfg["roles"].append(new)
        owner = RESEARCHER_ROLE
    audit = _audit(cfg)
    cfg["flags"][GATE] = {"kind": "gate", "default": False}
    position = next(i for i, s in enumerate(cfg["stages"]) if s["name"] == impl["name"])
    added = [{
        "name": RESEARCH_STAGE, "label": "Research", "owners": [owner], "kind": "system", "gate": None,
        "skip_to": None, "signoff": None, "terminal": False, "notify": {"kind": "assignee", "role": None},
    }]
    if audit is not None:
        added.append({
            "name": REVIEW_STAGE, "label": "Research review", "owners": list(audit["owners"]), "kind": "review",
            "gate": AUDIT_GATE, "skip_to": triage, "signoff": None, "terminal": False,
            "notify": copy.deepcopy(audit["notify"]),
        })
    cfg["stages"][position:position] = added
    composed = _composed(cfg, owner, triage, finished, audit, impl["name"])
    if not any(t["from"] == RESEARCH_STAGE and t["to"] not in (triage, REVIEW_STAGE) for t in composed):
        raise ValueError(f"research stage: the Director cannot defer or cancel work out of {impl['name']}, so the "
                         "new stage would have no way to put work down; add the stage by hand instead")
    cfg["transitions"] += composed
    if any(r.get("kind") == "user" and r.get("active") is True for r in cfg["roles"]):
        waits = list(cfg.get("user_waits") or [])
        cfg["user_waits"] = waits + [RESEARCH_STAGE] if RESEARCH_STAGE not in waits else waits
    cfg[POLICY_KEY] = {"directory": directory}
    cfg["remove_stages"] = [n for n in cfg.get("remove_stages") or [] if n not in (RESEARCH_STAGE, REVIEW_STAGE)]
    return validate(cfg)


def without_research_stage(document: dict[str, Any]) -> dict[str, Any]:
    """`document` without the stage: everything composed is removed; a Researcher stays, inactive."""
    from scripts.ticket_board.workflow_config import validate

    cfg = validate(copy.deepcopy(document))
    if research_stage(cfg) is None:
        raise ValueError("research stage: this workflow has no research stage; nothing was changed")
    gone = {RESEARCH_STAGE, REVIEW_STAGE}
    cfg["stages"] = [s for s in cfg["stages"] if s["name"] not in gone]
    cfg["transitions"] = [t for t in cfg["transitions"]
                          if t["from"] not in gone and t["to"] not in gone and t["action"] not in ACTIONS]
    cfg["flags"].pop(GATE, None)
    if "user_waits" in cfg:
        cfg["user_waits"] = [name for name in cfg["user_waits"] if name not in gone]
        if not cfg["user_waits"]:
            del cfg["user_waits"]
    cfg.pop(POLICY_KEY, None)
    removed = list(cfg.get("remove_stages") or [])
    cfg["remove_stages"] = removed + [n for n in (RESEARCH_STAGE, REVIEW_STAGE) if n not in removed]
    role = next((r for r in cfg["roles"] if r["name"] == RESEARCHER_ROLE and r["kind"] == RESEARCHER_KIND), None)
    if role is not None and role["active"]:
        role["active"] = False
        role.pop("slot", None)
    return validate(cfg)


def validate_research_stage(cfg: dict[str, Any], need: Callable[[bool, str], None]) -> None:
    """The stage's shape, wherever it is declared: who researches, what they may do, and what the stage means."""
    roles = {r["name"]: r for r in cfg["roles"]}
    stage, review = research_stage(cfg), review_stage(cfg)
    researcher = roles.get(RESEARCHER_ROLE)
    if researcher is not None and researcher["kind"] == RESEARCHER_KIND:
        need(set(researcher["capabilities"]) <= set(RESEARCHER_CAPABILITIES),
             "invalid researcher: it holds nothing but add_comment, await_role and clear_awaiting_role")
        need(researcher.get("serial") is not True, "invalid researcher: it never holds a serial slot")
        need(not any(RESEARCHER_ROLE in s["owners"] and s["name"] != RESEARCH_STAGE for s in cfg["stages"]),
             "invalid researcher: it owns the research stage and nothing else")
        need(not any((RESEARCHER_ROLE in t["actors"] or t.get("relays_decision_of") == RESEARCHER_ROLE)
                     and t["action"] not in RESEARCHER_ACTIONS for t in cfg["transitions"]),
             "invalid researcher: its only moves are its finding submissions and its hand-back")
    declared = [t for t in cfg["transitions"] if t["action"] in ACTIONS]
    if stage is None:
        need(review is None, "invalid research stage: research_review needs the research stage")
        need(not declared, "invalid research stage: its moves need the research stage")
        need(POLICY_KEY not in cfg and GATE not in cfg["flags"],
             "invalid research stage: its policy and flags need the research stage")
        need(not (researcher and researcher["kind"] == RESEARCHER_KIND and researcher["active"]),
             "invalid researcher: an active researcher needs its stage")
        return
    policy = cfg.get(POLICY_KEY)
    need(isinstance(policy, dict) and set(policy) == {"directory"} and isinstance(policy["directory"], str)
         and bool(_DIRECTORY.fullmatch(policy["directory"]))
         and not any(part in (".", "..") for part in policy["directory"].split("/")),
         'invalid research stage: its policy is {"directory": <repository-relative directory>}')
    need(cfg["flags"].get(GATE) == {"kind": "gate", "default": False},
         "invalid research stage: needs_research is a gate that is off unless the director turns it on")
    owners = stage["owners"]
    need(len(owners) == 1 and owners[0] not in NOT_RESEARCHERS,
         "invalid research stage: it has exactly one owner, a working role")
    need(stage["kind"] == "system" and stage["gate"] is None and stage["signoff"] is None and not stage["terminal"]
         and stage["notify"]["kind"] in {"assignee", "stage_owner_fallback"},
         "invalid research stage: an ungated system stage that tells its owner about work")
    impl = _implementation(cfg)
    audit = _audit(cfg)
    try:
        triage = _triage(cfg, impl["name"]) if impl else ""
        finished = _finished(cfg, triage)
    except ValueError as exc:
        need(False, f"invalid research stage: {exc}")
        return
    if review is not None:
        need(audit is not None and review["owners"] == audit["owners"] and review["kind"] == "review"
             and review["gate"] == AUDIT_GATE and review["skip_to"] == triage and review["signoff"] is None
             and not review["terminal"],
             "invalid research stage: research_review is Audit's, gated by needs_audit, skipping to triage, with "
             "no sign-off flag of its own")
    else:
        need(audit is None,
             "invalid research stage: a project that runs Audit reviews findings in research_review")
    composed = _composed(cfg, owners[0], triage, finished, audit if review is not None else None, impl["name"])
    key = (lambda t: (t["from"], t["to"], t["action"]))
    in_stage = [t for t in cfg["transitions"] if t["action"] in ACTIONS
                or t["from"] in (RESEARCH_STAGE, REVIEW_STAGE) or t["to"] in (RESEARCH_STAGE, REVIEW_STAGE)]
    need(sorted(in_stage, key=key) == sorted(composed, key=key),
         "invalid research stage: its moves are exactly as composed -- start_research from triage; the "
         "researcher's owner-scoped submit_finding (a commit), submit_finding_without_commit (a reason) and "
         "research_hand_back (a reason); Audit's research_sign_off and research_kick_back; the director's "
         "return_finding and close_on_finding (each with a reason), defer and cancel; with the Publisher, the "
         "director's publish_finding and finding_publication_kick_back and the Publisher's finding_published and "
         "finding_publication_return -- and nothing else enters or leaves the stage")
    if any(r.get("kind") == "user" and r.get("active") is True for r in cfg["roles"]):
        need(RESEARCH_STAGE in (cfg.get("user_waits") or []),
             "invalid research stage: the director may wait on the User in research (user_waits)")
