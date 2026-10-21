"""An optional documentation gate before review, decided by a Librarian or by the Director (SYRD-624).

A ticket that adds, changes or retires something a person reads about -- CLI
help, an operator or Director guide, onboarding, project documentation, an
example -- can be held, after Implementation and before the reviews, until its
documentation is consistent with it. The Director asks for that per ticket, by
turning the ticket's `needs_documentation` gate on, at triage or at any point
before the work leaves Implementation. A ticket whose gate is off never enters
the stage: the board's ordinary gate walk skips it.

`with_documentation_gate` adds, to a tenant's own declared document:

* the `needs_documentation` gate flag, off by default, and the
  `documentation_signoff` sign-off flag;
* the `documentation` review stage, directly after Implementation: every
  submission out of Implementation enters it, and with the gate off it skips
  to wherever those submissions went before (Inspection or Audit), so the
  reviews after it are unchanged and see the documentation as it ends up;
* three ways out of it, owner-scoped:
  - `documentation_sign_off`, with a documentation commit. The commit's sole
    parent is the submitted candidate, and it is published at the ticket's
    documentation candidate ref (`candidate_ref`), which documentation_candidate
    checks before the board moves anything and records with both SHAs. It
    replaces the ticket's candidate and clears every later review's sign-off,
    so Inspection and Audit review the commit that carries the documentation
    and never keep a verdict on an older one;
  - `documentation_no_change`, with a reason: nothing a person reads needs to
    change, and the candidate stays exactly what was submitted. The reason is
    recorded on the ticket as the decision's comment;
  - `documentation_kick_back`, a `return` to the recorded implementer with
    concrete findings as its reason. It is also the answer when the
    documentation commit cannot be formed or published as above;
* a defer (and any other Director-only exit to parking or a terminal stage)
  from the new stage, under the names the document already uses for the
  stage the submissions went to.

Who decides depends on the project. With a Librarian (`librarian` given), a
`librarian` role of kind `reviewer` owns the stage. It holds no capability but
`add_comment`, is the actor of those three transitions and nothing else, and
runs detached under the project account with its own onboarding prompt and a
fresh conversation per ticket. Without one, the Director owns the stage, so the
project gets the same decision with no worker and no cost. The document never
gives the Director an `approve` (workflow_config refuses one), so there the
stage carries no sign-off and its three exits are the Director's moves.

`without_documentation_gate` removes the stage, its transitions and its gate
from the document and puts the submissions back where they went; a Librarian
stays declared, inactive, so its decisions keep a name. The board refuses it
while a ticket is in the stage.

Both results are validated like any document -- by workflow_config and, on
apply, by the board -- so nothing reaches the board that `apply` would refuse.
`validate_documentation_gate` is the shape check workflow_config runs on every
document.
"""

from __future__ import annotations

import copy
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable

from .publisher_stage import canonical_publisher_returns

LIBRARIAN_ROLE = "librarian"
LIBRARIAN_KIND = "reviewer"
DOCUMENTATION_STAGE = "documentation"
GATE = "needs_documentation"
SIGNOFF = "documentation_signoff"
SIGN_OFF = "documentation_sign_off"
NO_CHANGE = "documentation_no_change"
KICK_BACK = "documentation_kick_back"
GATE_ACTIONS = (SIGN_OFF, NO_CHANGE, KICK_BACK)
#: Everything a Librarian may hold. Its decisions are transitions, which need no
#: capability; a comment is how it records findings it is not yet returning.
LIBRARIAN_CAPABILITIES = ("add_comment",)
#: The skill a Librarian session is told to load, beside the shared board skill.
LIBRARIAN_SKILL = "switchyard-librarian"
#: How much of the submitted candidate's SHA names its documentation candidate.
CANDIDATE_REF_SHA_CHARS = 12


def candidate_ref(role: str, ticket_id: str, candidate: str) -> str:
    """Where `role` publishes the documentation commit on top of `candidate` for `ticket_id`.

    Ticket-scoped and candidate-scoped: one ref per submitted candidate, so a
    later round is a new ref, never a rewrite of the old one. The publisher
    (switchyard-publish-candidate, for the Librarian) and the board both derive
    it here, so neither takes a ref from the caller.
    """
    return f"roles/{role}/{ticket_id.strip().lower()}-{candidate.strip().lower()[:CANDIDATE_REF_SHA_CHARS]}"


#: What a documentation commit may change: text written for people, and nothing
#: a program runs or a test compares. Documents in these formats, anywhere...
DOCUMENT_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".adoc", ".asciidoc"})
#: ...and, inside a top-level documentation directory, plain text and images too.
DOCUMENTATION_DIRS = frozenset({"docs", "doc"})
DOCUMENTATION_DIR_SUFFIXES = frozenset({".txt", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"})
#: Directories whose files tests read, whatever their format: a fixture is not documentation.
TEST_DATA_DIRS = frozenset({"test", "tests", "testdata", "fixtures", "__snapshots__", "golden", "goldens"})
#: A documentation commit adds, edits and removes ordinary files: no executable bit, link or submodule.
DOCUMENTATION_FILE_MODES = frozenset({"100644", "000000"})


def documentation_surface(path: str) -> bool:
    """Whether a documentation commit may add, edit or remove `path` (a repository-relative POSIX path).

    CLI help lives in code, so it is not one: a help text that must change is
    the implementer's, and the Librarian kicks the ticket back for it.
    """
    parts = path.split("/")
    if not path or any(part in ("", ".", "..") for part in parts):
        return False
    if TEST_DATA_DIRS.intersection(part.lower() for part in parts[:-1]):
        return False
    suffix = PurePosixPath(parts[-1]).suffix.lower()
    if suffix in DOCUMENT_SUFFIXES:
        return True
    return len(parts) > 1 and parts[0] in DOCUMENTATION_DIRS and suffix in DOCUMENTATION_DIR_SUFFIXES


def outside_documentation(entries: Iterable[tuple[str, str, str, str]]) -> list[str]:
    """Each change in `entries` -- (old mode, new mode, status, path) -- a documentation commit may not make."""
    problems = []
    for old_mode, new_mode, _status, path in entries:
        if not documentation_surface(path):
            problems.append(f"{path} (not a documentation file)")
        elif old_mode not in DOCUMENTATION_FILE_MODES or new_mode not in DOCUMENTATION_FILE_MODES:
            problems.append(f"{path} (mode {old_mode} -> {new_mode}; only ordinary, non-executable files)")
    return problems


#: The Librarian's remit, stored as its onboarding prompt. The full guide is the
#: switchyard-librarian skill and docs/onboarding/switchyard-librarian-guide.md;
#: this names what it may touch and the three decisions, and what is never its.
LIBRARIAN_PROMPT = """You are the Librarian. You keep this project's documentation consistent with the work that reaches you, and nothing else. Load the switchyard-librarian skill now and follow it; it is your full guide.

A ticket reaches you, in the Documentation stage, only when the Director asked for documentation review. Read that one ticket and its candidate, and nothing else:

    ticket-board-read ticket <ID> --json
    git show --stat <commit_hash>; git diff <commit_hash>~1 <commit_hash>

Check every surface a person reads about what the ticket adds, changes or retires: CLI help and usage text, the operator, Director and board guides, onboarding documents, project documentation, examples, and instructions the change made stale or contradictory. Then make exactly one decision:

    ticket-board-write documentation-no-change <ID> --reason "<why nothing a person reads needs to change>"
    ticket-board-write documentation-kick-back <ID> --reason "<the concrete findings>"
    ticket-board-write documentation-sign-off <ID> --commit <your commit>

To edit, commit documents only -- Markdown, reStructuredText or AsciiDoc files outside test data, and text or images under docs/ -- as ordinary files, on a branch named after the ticket, with the submitted candidate as its sole parent; publish it with `switchyard-publish-candidate <ID> --commit <your commit>`, which chooses the ticket's documentation ref; then sign off naming it. Your commit becomes the candidate, and every later review starts again on it. Never change product code, tests, behaviour or configuration -- the board refuses a commit that changes anything but documents -- so help text in code and example configurations are the implementer's: kick back with the exact wording they need. Never rebase, merge, force-push or choose a ref or remote; when the documentation cannot be made true that way, or your commit cannot be formed or published, kick the ticket back with what is wrong.

You do not route, reassign, integrate, publish a release, merge, accept, close, or touch any other ticket, and you use no other board operation. When your decision is recorded, stop and wait for the next notification."""

def _flag_kinds(cfg: dict[str, Any]) -> dict[str, str]:
    return {name: flag["kind"] for name, flag in cfg["flags"].items()}


def implementation_stage(cfg: dict[str, Any]) -> dict[str, Any] | None:
    return next((s for s in cfg["stages"] if s["kind"] == "implementation"), None)


def documentation_stage(cfg: dict[str, Any]) -> dict[str, Any] | None:
    return next((s for s in cfg.get("stages", []) if s["name"] == DOCUMENTATION_STAGE), None)


def librarian_role(cfg: dict[str, Any]) -> dict[str, Any] | None:
    return next((r for r in cfg.get("roles", []) if r["name"] == LIBRARIAN_ROLE), None)


def gate_owner(cfg: dict[str, Any] | None) -> str | None:
    """Who decides documentation on this board: `librarian`, `director`, or None without a gate."""
    stage = documentation_stage(cfg or {})
    return stage["owners"][0] if stage and stage["owners"] else None


def _submissions(cfg: dict[str, Any], impl: str) -> list[dict[str, Any]]:
    """Every way work leaves Implementation for review: what the gate is placed in front of."""
    stages = {s["name"]: s for s in cfg["stages"]}
    return [t for t in cfg["transitions"]
            if t["from"] == impl and stages[t["to"]]["kind"] == "review"
            and t["primitive"] == "move" and (t["require_commit"] or t["allow_no_code"])]


def _transition(source: str, destination: str, action: str, label: str, actors: list[str], *, primitive: str,
                require_commit: bool = False, require_reason: bool = False,
                clear_signoffs: list[str] | None = None) -> dict[str, Any]:
    return {
        "from": source, "to": destination, "action": action, "label": label, "actors": actors,
        "primitive": primitive, "owner_scoped": True, "require_commit": require_commit,
        "require_reason": require_reason, "clear_signoffs": clear_signoffs or [], "allow_no_code": False,
        "relays_decision_of": None,
    }


def _gate_transitions(cfg: dict[str, Any], impl: str, after: str, owner: str) -> list[dict[str, Any]]:
    """The stage's three exits. A Librarian approves; the Director, who never approves, moves."""
    later = sorted(name for name, kind in _flag_kinds(cfg).items() if kind == "signoff" and name != SIGNOFF)
    decide = "approve" if owner == LIBRARIAN_ROLE else "move"
    return [
        _transition(DOCUMENTATION_STAGE, after, SIGN_OFF, "Sign off with documentation commit", [owner],
                    primitive=decide, require_commit=True, clear_signoffs=later),
        _transition(DOCUMENTATION_STAGE, after, NO_CHANGE, "Approve: no documentation change", [owner],
                    primitive=decide, require_reason=True),
        _transition(DOCUMENTATION_STAGE, impl, KICK_BACK, "Return documentation findings", [owner],
                    primitive="return", require_reason=True, clear_signoffs=sorted(later + [SIGNOFF])),
    ]


def with_documentation_gate(document: dict[str, Any], *, librarian: dict[str, Any] | None = None) -> dict[str, Any]:
    """`document` with the documentation gate, owned by a Librarian when `librarian` is given, else the Director.

    `librarian` is `{"runtime": <cli>, "template_role": <role or None>}`. Raises
    ValueError, changing nothing, when the gate cannot be added safely.
    """
    from scripts.ticket_board.workflow_config import DIRECTOR_ROLE, RUNTIMES, parking_stage_names, validate

    cfg = validate(copy.deepcopy(document))
    if documentation_stage(cfg) is not None:
        raise ValueError(f"documentation gate: this workflow already has a {DOCUMENTATION_STAGE} stage, owned by "
                         f"{gate_owner(cfg) or 'nobody'}; disable it first to change who decides")
    taken = sorted({GATE, SIGNOFF} & set(cfg["flags"]))
    clashing = sorted(t["action"] for t in cfg["transitions"] if t["action"] in GATE_ACTIONS)
    if taken or clashing:
        raise ValueError(f"documentation gate: this workflow already declares {', '.join(taken + clashing)}; "
                         "nothing was changed")
    impl = implementation_stage(cfg)
    if impl is None:
        raise ValueError("documentation gate: this workflow has no Implementation stage to follow")
    submissions = _submissions(cfg, impl["name"])
    targets = sorted({t["to"] for t in submissions})
    if len(targets) != 1:
        raise ValueError(f"documentation gate: cannot tell where work goes after Implementation "
                         f"({', '.join(targets) or 'no submission to a review stage'}); add the stage by hand "
                         "and apply the document instead")
    after = targets[0]
    owner = DIRECTOR_ROLE
    if librarian is not None:
        project = str(cfg.get("project") or "").strip()
        runtime = librarian.get("runtime")
        if runtime not in RUNTIMES:
            raise ValueError(f"librarian: runtime must be one of {', '.join(sorted(RUNTIMES))}, not {runtime!r}")
        if not project:
            raise ValueError("librarian: this workflow names no project, so the Librarian's pane target cannot be named")
        existing = librarian_role(cfg)
        if existing is not None and existing["kind"] != LIBRARIAN_KIND:
            raise ValueError("librarian: this workflow already has a role named librarian that is not a reviewer; "
                             "nothing was changed")
        roles = {r["name"]: r for r in cfg["roles"]}
        template = librarian.get("template_role") or impl["owners"][0]
        if template not in roles or template == LIBRARIAN_ROLE:
            raise ValueError(f"librarian: template role {template!r} is not a role of this workflow")
        role = {
            "name": LIBRARIAN_ROLE, "label": "Librarian", "kind": LIBRARIAN_KIND, "active": True,
            "capabilities": list(LIBRARIAN_CAPABILITIES), "runtime": runtime,
            "target": f"{project}-{LIBRARIAN_ROLE}:0.0", "slot": None, "serial": False, "ephemeral": True,
            "template_role": template, "onboarding_prompt": LIBRARIAN_PROMPT, "presentation_label": "Librarian",
        }
        if existing is not None:  # a Librarian turned off earlier: turn the same role back on
            cfg["roles"] = [role if r["name"] == LIBRARIAN_ROLE else r for r in cfg["roles"]]
        else:
            cfg["roles"].append(role)
        owner = LIBRARIAN_ROLE
    cfg["flags"][GATE] = {"kind": "gate", "default": False}
    cfg["flags"][SIGNOFF] = {"kind": "signoff", "default": False}
    position = next(i for i, s in enumerate(cfg["stages"]) if s["name"] == impl["name"]) + 1
    cfg["stages"].insert(position, {
        "name": DOCUMENTATION_STAGE, "label": "Documentation", "owners": [owner], "kind": "review",
        "gate": GATE, "skip_to": after, "signoff": SIGNOFF if owner == LIBRARIAN_ROLE else None,
        "terminal": False, "notify": {"kind": "assignee", "role": None},
    })
    for submission in submissions:
        submission["to"] = DOCUMENTATION_STAGE
    stages = {s["name"]: s for s in cfg["stages"]}
    parking = parking_stage_names(cfg)
    exits = [dict(t, **{"from": DOCUMENTATION_STAGE}) for t in cfg["transitions"]
             if t["from"] == after and t["actors"] == [DIRECTOR_ROLE] and t["primitive"] == "move"
             and (t["to"] in parking or stages[t["to"]]["terminal"])]
    if not any(t["to"] in parking for t in exits):
        raise ValueError(f"documentation gate: the Director cannot defer work out of {after}, so the new stage "
                         "would have no way to put work down; add the stage by hand instead")
    cfg["transitions"] += _gate_transitions(cfg, impl["name"], after, owner) + exits
    # Turned off earlier: the stage is declared again, so its removal no longer stands.
    cfg["remove_stages"] = [name for name in cfg.get("remove_stages") or [] if name != DOCUMENTATION_STAGE]
    return validate(canonical_publisher_returns(cfg))


def without_documentation_gate(document: dict[str, Any]) -> dict[str, Any]:
    """`document` without the gate: submissions go where they went before; a Librarian stays, inactive."""
    from scripts.ticket_board.workflow_config import validate

    cfg = validate(copy.deepcopy(document))
    stage = documentation_stage(cfg)
    if stage is None:
        raise ValueError("documentation gate: this workflow has no documentation stage; nothing was changed")
    after = stage["skip_to"]
    cfg["stages"] = [s for s in cfg["stages"] if s["name"] != DOCUMENTATION_STAGE]
    cfg["transitions"] = [t for t in cfg["transitions"] if t["from"] != DOCUMENTATION_STAGE]
    for t in cfg["transitions"]:
        if t["to"] == DOCUMENTATION_STAGE:
            t["to"] = after
        t["clear_signoffs"] = [f for f in t["clear_signoffs"] if f != SIGNOFF]
    for name in (GATE, SIGNOFF):
        cfg["flags"].pop(name, None)
    removed = list(cfg.get("remove_stages") or [])
    cfg["remove_stages"] = removed + [DOCUMENTATION_STAGE] if DOCUMENTATION_STAGE not in removed else removed
    role = librarian_role(cfg)
    if role is not None and role["active"]:
        role["active"] = False
        role.pop("slot", None)
    return validate(cfg)


def validate_documentation_gate(cfg: dict[str, Any], need: Callable[[bool, str], None]) -> None:
    """The gate's shape, wherever it is declared: what a Librarian may do, and what the gate always means."""
    roles = {r["name"]: r for r in cfg["roles"]}
    stage = documentation_stage(cfg)
    librarian = roles.get(LIBRARIAN_ROLE)
    if librarian is not None:
        need(librarian["kind"] == LIBRARIAN_KIND, "invalid librarian: the librarian role is a reviewer")
        need(set(librarian["capabilities"]) <= set(LIBRARIAN_CAPABILITIES),
             "invalid librarian: it holds no capability but add_comment -- no publication, routing, merge or edit")
        need(librarian.get("serial") is not True, "invalid librarian: it never holds a serial slot")
        need(not any(LIBRARIAN_ROLE in s["owners"] and s["name"] != DOCUMENTATION_STAGE for s in cfg["stages"]),
             "invalid librarian: it owns the documentation stage and nothing else")
        need(not any((LIBRARIAN_ROLE in t["actors"] or t.get("relays_decision_of") == LIBRARIAN_ROLE)
                     and t["action"] not in GATE_ACTIONS for t in cfg["transitions"]),
             "invalid librarian: its only moves are the documentation decisions")
    gate_moves = [t for t in cfg["transitions"] if t["action"] in GATE_ACTIONS]
    if stage is None:
        need(not gate_moves, "invalid documentation gate: its decisions need the documentation stage")
        need(not (librarian and librarian["active"]), "invalid librarian: an active librarian needs its stage")
        return
    impl = implementation_stage(cfg) or {}
    owners = stage["owners"]
    librarian_owned = owners == [LIBRARIAN_ROLE]
    need(librarian_owned or owners == ["director"],
         "invalid documentation gate: the documentation stage is the librarian's alone, or the director's")
    need(not librarian_owned or bool(librarian and librarian["active"] and librarian.get("target")),
         "invalid documentation gate: its librarian must be active, with a pane target to be told about work")
    need(stage["kind"] == "review" and stage["gate"] == GATE and not stage["terminal"]
         and stage["signoff"] == (SIGNOFF if librarian_owned else None)
         and stage["notify"]["kind"] in {"assignee", "stage_owner_fallback"},
         "invalid documentation gate: a review gated by needs_documentation that tells its owner, signed off "
         "by the librarian (the director holds no sign-off)")
    flags = cfg["flags"]
    need(flags.get(GATE) == {"kind": "gate", "default": False},
         "invalid documentation gate: needs_documentation is a gate that is off unless the director turns it on")
    after = stage["skip_to"]
    stages = {s["name"]: s for s in cfg["stages"]}
    need(after in stages and stages[after]["kind"] == "review",
         "invalid documentation gate: it skips to the review that follows it")
    need(all(t["to"] == DOCUMENTATION_STAGE for t in _submissions(cfg, impl.get("name", ""))),
         "invalid documentation gate: every submission out of Implementation enters it")
    actor = owners[0]
    expected = {t["action"]: t for t in _gate_transitions(cfg, impl.get("name", ""), after, actor)}
    need(sorted(t["action"] for t in gate_moves) == sorted(GATE_ACTIONS)
         and all(t == expected[t["action"]] for t in gate_moves),
         "invalid documentation gate: its three decisions are owner-scoped and exactly as composed -- sign-off "
         "carries the documentation commit and clears every later sign-off, no-change needs a reason, the return "
         "needs findings")
