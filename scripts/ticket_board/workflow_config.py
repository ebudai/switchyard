"""Validated, executable-code-free workflow documents and metadata helpers."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

SCHEMA = "switchyard.workflow.v1"
NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ROLE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
KINDS = {"system", "draft", "implementer", "reviewer", "support", "user"}
PRIMITIVES = {"move", "approve", "return", "reopen"}
RUNTIMES = {"claude", "codex", "agy", "hermes"}
# The director is a structural role of this model, not a tenant-chosen name: a
# document without it is rejected below. Anything needing to find the director role
# derives it from here rather than repeating the literal.
DIRECTOR_ROLE = "director"
# Marker for the one-time director onboarding backfill. Once set, a director with
# no stored prompt means the operator cleared it, not that the tenant predates the
# feature -- which is what keeps `role-prompt clear director` durable.
DIRECTOR_ONBOARDING_MIGRATION = "director_onboarding"
# A role remit, not a document. Bounded so a stored prompt cannot grow the tenant
# configuration without limit, and so it stays small enough to prepend to a session.
ONBOARDING_PROMPT_MAX_CHARS = 16384
# Named operations a role may be granted. Unrelated to the document's own
# `reassign` field below, which maps a stage to the owner its tickets move to
# when a configuration change would orphan them; the capability here is the
# per-ticket director operation.
CAPABILITIES = {
    "create_ticket",
    "file_bug",
    "add_comment",
    "edit_fields",
    "await_role",
    "clear_awaiting_role",
    "set_blockers",
    "set_manually_controlled",
    "crop_attachment",
    "merge",
    "dismiss_notification",
    "reassign",
    # SYRD-93: the two halves of publication. An implementer asks; the control
    # role decides. Neither one is a credential, which is the point.
    "request_publication",
    "resolve_publication",
    # SYRD-83: the Director's generic edit. A capability like any other, so a
    # tenant that does not want it simply does not grant it, and the handler
    # decides from the document rather than from a name.
    "director_edit",
}
# The control floor: what a declarative document may never take away from the
# director, and what it may never give. A tenant configures its own pipeline;
# it does not get to leave the project without a controller, and it does not get
# to make the controller a reviewer of its own work (SYRD-82).
#
# Each entry is here because it carries one of the authorities the ticket names,
# not because the current document happens to list it:
#   reassign                -> route/reassign: move work to another owner
#   set_manually_controlled -> hold; with `merge`, half of what identifies the
#                              control role, which is what the database derives
#                              force_move/override_move authority from (SYRD-78)
#   set_blockers            -> block
#   merge                   -> merge; the other half of that identity
#   edit_fields             -> hierarchy (a ticket's parent is set through it)
#   dismiss_notification    -> notification recovery
#   resolve_publication     -> publish or reject an implementer's ref: the only
#                              path to an outbound push, so a project without it
#                              has work that can be committed and never shipped
#                              (SYRD-93)
#   director_edit           -> the generic edit: the one way to move a field the
#                              transition table has nothing to say about, which
#                              is what a Director reworking somebody else's
#                              ticket needs (SYRD-83)
# Queue, defer, cancel and reopen are transitions rather than capabilities, so
# they are held by the structural rules below instead: a director that cannot
# leave a stage has lost them whatever its capability list says.
DIRECTOR_CONTROL_CAPABILITIES = frozenset(
    {
        "reassign",
        "set_manually_controlled",
        "set_blockers",
        "merge",
        "edit_fields",
        "dismiss_notification",
        "director_edit",
        "resolve_publication",
    }
)
# The subset that identifies which role is the controller, for consumers that
# must find it without trusting the name. Kept here so it cannot drift from the
# floor: a discriminator naming a capability the floor does not guarantee would
# stop finding the director the moment a tenant dropped it.
DIRECTOR_IDENTIFYING_CAPABILITIES = frozenset(
    {"merge", "set_blockers", "set_manually_controlled"}
)
assert DIRECTOR_IDENTIFYING_CAPABILITIES <= DIRECTOR_CONTROL_CAPABILITIES

RESERVED_FLAGS = {
    "id",
    "ticket_number",
    "title",
    "body",
    "state",
    "assignee",
    "parent_id",
    "origin_project",
    "external_source_ref",
    "blocked_reason",
    "implementation",
    "audit_prompt",
    "regression",
    "manually_controlled",
    "parked",
    "commit_hash",
    "commit_exempt",
    "created_text",
    "updated_text",
    "created_at",
    "updated_at",
    "workflow_flags",
}
BUILTIN_FLAGS = {
    "needs_inspection": "gate",
    "needs_audit": "gate",
    "needs_user_signoff": "gate",
    "inspector_signoff": "signoff",
    "audit_signoff": "signoff",
    "user_signoff": "signoff",
}


def parking_stage_names(cfg: dict[str, Any]) -> set[str]:
    """Stages that hold work nobody is doing.

    Described by shape rather than by name, because the label is the tenant's:
    somewhere a ticket can wait that is not finished, that nobody owns, and that
    notifies nobody. That is what makes deferral different from cancelling --
    the work survives -- and different from routing, which always lands on an
    owner (SYRD-92).
    """
    return {
        stage["name"]
        for stage in cfg["stages"]
        if not stage["terminal"] and not stage["owners"] and stage["notify"]["kind"] == "none"
    }


def validate(document: Any, *, project: str | None = None) -> dict[str, Any]:
    """Validate the complete desired graph. Never infer silence or actor authority."""
    cfg = copy.deepcopy(document)

    def need(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    need(
        isinstance(cfg, dict) and cfg.get("schema") == SCHEMA,
        f"workflow schema must be {SCHEMA}",
    )
    need(
        set(cfg)
        <= {
            "schema",
            "roles",
            "stages",
            "transitions",
            "flags",
            "reassign",
            "queue",
            "project",
            "remove_stages",
            "migrations",
        },
        "unknown workflow configuration field",
    )
    if project is not None:
        need(
            cfg.get("project", project) == project,
            "workflow belongs to another project",
        )
        cfg["project"] = project
    if cfg.get("project") is not None:
        need(
            isinstance(cfg["project"], str)
            and bool(re.fullmatch(r"[a-z0-9][a-z0-9_]{0,39}", cfg["project"])),
            "invalid workflow project",
        )
        project = cfg["project"]
    for key in ("roles", "stages", "transitions"):
        need(
            isinstance(cfg.get(key), list) and bool(cfg[key]),
            f"{key} must be a nonempty list",
        )
    migrations = cfg.get("migrations")
    if migrations is not None:
        # Durable one-time markers. They record that a backfill has already been
        # considered for this document, so a value an operator deliberately removed is
        # not treated as missing and refilled on the next write.
        need(
            isinstance(migrations, dict)
            and all(
                isinstance(k, str) and bool(NAME.fullmatch(k)) and isinstance(v, bool)
                for k, v in migrations.items()
            ),
            "migrations must map migration names to booleans",
        )
    flags = cfg.setdefault("flags", {})
    need(isinstance(flags, dict), "flags must be an object")
    for name, spec in flags.items():
        need(name not in RESERVED_FLAGS, "flag collides with protected ticket field")
        need(bool(NAME.fullmatch(name)) and isinstance(spec, dict), "invalid flag")
        need(
            set(spec) == {"kind", "default"}
            and spec["kind"] in {"gate", "signoff"}
            and type(spec["default"]) is bool,
            f"invalid flag policy: {name}",
        )
        need(
            name not in BUILTIN_FLAGS or BUILTIN_FLAGS[name] == spec["kind"],
            f"cannot change builtin flag kind: {name}",
        )
        need(
            spec["kind"] != "signoff" or spec["default"] is False,
            "signoffs cannot default to approved",
        )
    roles: dict[str, Any] = {}
    slots: set[int] = set()
    targets: set[str] = set()
    for role in cfg["roles"]:
        need(isinstance(role, dict), "role must be an object")
        need(
            set(role)
            <= {
                "name",
                "label",
                "kind",
                "active",
                "capabilities",
                "runtime",
                "target",
                "slot",
                "onboarding",
                "onboarding_prompt",
                "template_role",
            },
            "unknown role field",
        )
        name = role.get("name", "")
        need(
            isinstance(name, str) and bool(ROLE.fullmatch(name)) and name not in roles,
            f"invalid/duplicate role: {name}",
        )
        need(
            isinstance(role.get("label"), str) and bool(role["label"].strip()),
            f"role needs a label: {name}",
        )
        need(
            role.get("kind") in KINDS and type(role.get("active")) is bool,
            f"role needs kind and active: {name}",
        )
        caps = role.get("capabilities")
        need(
            isinstance(caps, list)
            and all(c in CAPABILITIES for c in caps)
            and len(set(caps)) == len(caps),
            f"invalid capabilities: {name}",
        )
        runtime, target = role.get("runtime"), role.get("target")
        need(
            (runtime is None and target is None)
            or (
                runtime in RUNTIMES
                and isinstance(target, str)
                and bool(re.fullmatch(r"[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+", target))
            ),
            f"invalid runtime/target: {name}",
        )
        if target is not None and project is not None:
            target_session = target.split(":", 1)[0]
            need(
                target_session.startswith(f"{project}-"),
                f"runtime target must belong to selected project: {project}",
            )
        slot = role.get("slot")
        need(
            slot is None
            or (
                type(slot) is int
                and 0 <= slot < 6
                and role["active"]
                and target is not None
            ),
            f"invalid visible slot: {name}",
        )
        if slot is not None:
            need(slot not in slots, "visible slots must be unique")
            slots.add(slot)
        if target is not None and role["active"]:
            need(target not in targets, "active pane targets must be unique")
            targets.add(target)
        if role.get("onboarding") is not None:
            need(
                isinstance(role["onboarding"], str)
                and role["onboarding"].startswith("/")
                and "\n" not in role["onboarding"],
                "onboarding must be an absolute file path",
            )
        if role.get("onboarding_prompt") is not None:
            # Prompt text, not a path: multiline is the point, so newlines are allowed
            # here where `onboarding` forbids them. A cleared prompt is an absent key
            # rather than an empty string, so that "set" and "clear" cannot be confused
            # when reading the document back.
            prompt = role["onboarding_prompt"]
            need(
                isinstance(prompt, str) and bool(prompt.strip()),
                f"onboarding_prompt must be non-empty text: {name}",
            )
            need(
                len(prompt) <= ONBOARDING_PROMPT_MAX_CHARS,
                f"onboarding_prompt must be at most {ONBOARDING_PROMPT_MAX_CHARS} characters: {name}",
            )
            need("\x00" not in prompt, f"onboarding_prompt must not contain NUL: {name}")
        if role.get("template_role") is not None:
            need(
                isinstance(role["template_role"], str)
                and bool(ROLE.fullmatch(role["template_role"])),
                "invalid template role",
            )
        roles[name] = role
    need(
        all(name in roles for name in (DIRECTOR_ROLE, "user", "unassigned")),
        "director, user and unassigned identities are required",
    )
    need(
        all(
            r.get("template_role") is None or r["template_role"] in roles
            for r in roles.values()
        ),
        "unknown template role",
    )
    removed = cfg.setdefault("remove_stages", [])
    need(
        isinstance(removed, list)
        and all(isinstance(n, str) and NAME.fullmatch(n) for n in removed)
        and len(set(removed)) == len(removed),
        "invalid explicit stage removals",
    )
    need(roles[DIRECTOR_ROLE]["active"], "director must remain active")
    stripped = sorted(DIRECTOR_CONTROL_CAPABILITIES - set(roles[DIRECTOR_ROLE]["capabilities"]))
    need(
        not stripped,
        "director must keep its control capabilities: " + ", ".join(stripped),
    )
    stages: dict[str, Any] = {}
    for stage in cfg["stages"]:
        need(isinstance(stage, dict), "stage must be an object")
        need(
            set(stage)
            == {
                "name",
                "label",
                "owners",
                "kind",
                "gate",
                "skip_to",
                "signoff",
                "terminal",
                "notify",
            },
            "stage fields must be explicit",
        )
        name = stage["name"]
        need(
            isinstance(name, str) and bool(NAME.fullmatch(name)) and name not in stages,
            f"invalid/duplicate stage: {name}",
        )
        need(
            isinstance(stage["label"], str) and bool(stage["label"].strip()),
            "stage needs a label",
        )
        need(
            stage["kind"] in {"draft", "implementation", "review", "system"}
            and type(stage["terminal"]) is bool,
            "invalid stage kind/terminal policy",
        )
        owners = stage["owners"]
        need(
            isinstance(owners, list)
            and len(set(owners)) == len(owners)
            and all(r in roles and roles[r]["active"] for r in owners),
            f"invalid stage owners: {name}",
        )
        if stage["kind"] == "implementation":
            need(
                bool(owners) and all(roles[r]["kind"] == "implementer" for r in owners),
                "implementation owners must be implementers",
            )
        for field, kind in (("gate", "gate"), ("signoff", "signoff")):
            flag = stage[field]
            need(
                flag is None or (flag in flags and flags[flag]["kind"] == kind),
                f"unknown {field}: {name}",
            )
        need(
            (stage["gate"] is None) == (stage["skip_to"] is None),
            "gated stages must name a skip target",
        )
        notify = stage["notify"]
        need(
            isinstance(notify, dict) and set(notify) == {"kind", "role"},
            "notification policy must be explicit",
        )
        need(
            notify["kind"]
            in {"none", "assignee", "fixed_role", "stage_owner_fallback"},
            "invalid notification policy",
        )
        need(
            (
                (notify["role"] in roles and roles[notify["role"]]["active"])
                if notify["kind"] == "fixed_role"
                else notify["role"] is None
            ),
            "invalid fixed notification role",
        )
        destinations = (
            [notify["role"]]
            if notify["kind"] == "fixed_role"
            else (
                owners if notify["kind"] in {"assignee", "stage_owner_fallback"} else []
            )
        )
        need(
            all(roles[r].get("target") for r in destinations),
            "notifying owners require declared pane targets; choose explicit silence otherwise",
        )
        stages[name] = stage
    need(
        sum(s["kind"] == "implementation" for s in stages.values()) == 1,
        "exactly one implementation stage is supported",
    )
    outgoing: dict[str, set[str]] = {name: set() for name in stages}
    incoming: set[str] = set()
    seen: set[tuple[str, str, str]] = set()
    for tr in cfg["transitions"]:
        if isinstance(tr, dict):
            tr.setdefault("allow_no_code", False)
        need(
            isinstance(tr, dict)
            and set(tr)
            == {
                "allow_no_code",
                "from",
                "to",
                "action",
                "label",
                "primitive",
                "actors",
                "owner_scoped",
                "require_commit",
                "require_reason",
                "clear_signoffs",
            },
            "transition fields must be explicit",
        )
        a, b = tr["from"], tr["to"]
        need(a in stages and b in stages and a != b, "invalid transition endpoints")
        need(
            isinstance(tr["action"], str) and bool(NAME.fullmatch(tr["action"])),
            "invalid action name",
        )
        key = (a, b, tr["action"])
        need(key not in seen, "duplicate transition")
        seen.add(key)
        need(
            tr["primitive"] in PRIMITIVES
            and isinstance(tr["label"], str)
            and bool(tr["label"].strip()),
            "invalid behavior primitive/label",
        )
        need(
            all(
                type(tr[k]) is bool
                for k in ("owner_scoped", "require_commit", "require_reason")
            ),
            "transition switches must be booleans",
        )
        need(
            isinstance(tr["actors"], list)
            and bool(tr["actors"])
            and all(r in roles and roles[r]["active"] for r in tr["actors"]),
            "invalid transition actors",
        )
        need(
            not stages[a]["terminal"] or tr["primitive"] == "reopen",
            "terminal exit must be an explicit reopen",
        )
        need(
            tr["primitive"] != "approve" or stages[a]["signoff"] is not None,
            "approval requires a source-stage signoff",
        )
        need(
            tr["primitive"] != "return" or stages[b]["kind"] == "implementation",
            "return must target the implementation stage",
        )
        need(
            isinstance(tr["clear_signoffs"], list)
            and all(
                f in flags and flags[f]["kind"] == "signoff"
                for f in tr["clear_signoffs"]
            ),
            "only signoffs may be cleared",
        )
        need(
            type(tr["allow_no_code"]) is bool
            and (
                not tr["allow_no_code"]
                or (
                    stages[a]["kind"] == "implementation"
                    and tr["require_reason"]
                    and not tr["require_commit"]
                )
            ),
            "no-code completion requires implementation origin and reason",
        )
        outgoing[a].add(b)
        incoming.add(b)
    for name, stage in stages.items():
        trail: set[str] = set()
        cursor = name
        while stages[cursor]["skip_to"] is not None:
            need(cursor not in trail, "gate skip cycle")
            trail.add(cursor)
            cursor = stages[cursor]["skip_to"]
            need(cursor in stages, "unknown gate skip target")
        if stage["terminal"]:
            continue
        need(bool(outgoing[name]), f"stage has no exit: {name}")
        need(
            name in incoming or stage["kind"] == "draft", f"stage has no entry: {name}"
        )
        reached, todo = set(), [name]
        while todo:
            node = todo.pop()
            if node not in reached:
                reached.add(node)
                todo.extend(outgoing[node] - reached)
        need(
            any(stages[n]["terminal"] for n in reached),
            f"stage cannot reach a terminal: {name}",
        )
    # What the capability list cannot express. Queue, defer, cancel and reopen
    # are transitions, and their names are the tenant's to choose, so the floor
    # is stated as the shape they have to leave behind rather than as a list of
    # actions to look for: a director that cannot leave a stage has lost control
    # of every ticket sitting in it, whatever the document calls the move
    # (SYRD-82).
    director_exits: set[str] = set()
    director_reopens: set[str] = set()
    for tr in cfg["transitions"]:
        if DIRECTOR_ROLE not in tr["actors"]:
            continue
        director_exits.add(tr["from"])
        if tr["primitive"] == "reopen":
            director_reopens.add(tr["from"])
    stuck = sorted(
        name for name, stage in stages.items() if not stage["terminal"] and name not in director_exits
    )
    need(not stuck, "director must be able to move work out of every stage: " + ", ".join(stuck))
    # Being able to leave is not the same as being able to put something down.
    # This floor was satisfied by `cancel`, which is an exit from everywhere and
    # the wrong one: it ends the work rather than parking it, so a director with
    # nothing but cancel has to destroy a ticket to clear a queue. A tenant must
    # keep somewhere to defer to, and a way to get there and back (SYRD-92).
    parking = parking_stage_names(cfg)
    need(bool(parking), "workflow must keep a stage where deferred work can wait")
    parked_from: set[str] = set()
    parked_revivals: set[str] = set()
    for tr in cfg["transitions"]:
        if DIRECTOR_ROLE not in tr["actors"]:
            continue
        if tr["to"] in parking:
            parked_from.add(tr["from"])
        if tr["from"] in parking and tr["to"] not in parking and not stages[tr["to"]]["terminal"]:
            parked_revivals.add(tr["from"])
    # Every stage where work is live: not finished and not already parked. That
    # includes reviews. A defer is not a review decision -- it raises no
    # sign-off, clears none, and leaves every gate, comment and flag where it
    # was -- so a stage being a review is no reason to take the control away.
    # Whether anybody is mid-judgement is an operational question the director
    # answers before using it; the document must not answer it by silently
    # removing the control (SYRD-92).
    active = sorted(
        name for name, stage in stages.items() if not stage["terminal"] and name not in parking
    )
    unparkable = [name for name in active if name not in parked_from]
    need(
        not unparkable,
        "director must be able to defer work out of every active stage: " + ", ".join(unparkable),
    )
    stranded = sorted(name for name in parking if name not in parked_revivals)
    need(
        not stranded,
        "deferred work must have an ordinary way back: " + ", ".join(stranded),
    )
    sealed = sorted(
        name for name, stage in stages.items() if stage["terminal"] and name not in director_reopens
    )
    need(not sealed, "director must be able to reopen every terminal stage: " + ", ".join(sealed))
    # The other half of the floor, and the one that is a prohibition rather than
    # a guarantee. An `approve` transition is what writes its source stage's
    # sign-off flag, so listing the director among its actors is how a document
    # would hand the controller the power to approve the work it directs.
    approvals = sorted(
        tr["action"]
        for tr in cfg["transitions"]
        if tr["primitive"] == "approve" and DIRECTOR_ROLE in tr["actors"]
    )
    need(
        not approvals,
        "director must not be granted sign-off authority: " + ", ".join(approvals),
    )
    queue = cfg.setdefault("queue", None)
    if queue is not None:
        need(
            isinstance(queue, dict) and set(queue) == {"stage", "assignee"},
            "invalid queue policy",
        )
        need(
            queue["stage"] in stages and queue["assignee"] in roles,
            "unknown queue destination",
        )
        need(
            queue["assignee"] == "unassigned" or roles[queue["assignee"]]["active"],
            "queue owner must be active",
        )
        stage = stages[queue["stage"]]
        need(
            not stage["terminal"]
            and stage["kind"] not in {"implementation", "review"}
            and stage["gate"] is None,
            "queue must be an ungated holding stage",
        )
        need(
            not stage["owners"] or queue["assignee"] in stage["owners"],
            "queue assignee must own holding stage",
        )
    assignments = cfg.setdefault("reassign", {})
    need(
        isinstance(assignments, dict)
        and all(
            s in stages
            and r in roles
            and (not stages[s]["owners"] or r in stages[s]["owners"])
            for s, r in assignments.items()
        ),
        "invalid deliberate stage reassignment",
    )
    return cfg


def read_configuration(conn: Any) -> dict[str, Any] | None:
    # Older exported releases/fixtures remain usable until the foundation migration.
    row = conn.execute(
        "SELECT to_regclass('ticket_board.workflow_configuration') AS relation"
    ).fetchone()
    if not row or not (row.get("relation") if isinstance(row, dict) else row[0]):
        return None
    row = conn.execute(
        "SELECT document FROM ticket_board.workflow_configuration WHERE singleton"
    ).fetchone()
    if not row:
        return None
    value = row["document"] if isinstance(row, dict) else row[0]
    return json.loads(value) if isinstance(value, str) else value


def available_transitions(
    cfg: dict[str, Any], ticket: dict[str, Any], actor: str | None = None
) -> list[dict[str, Any]]:
    return [
        tr
        for tr in cfg["transitions"]
        if tr["from"] == ticket["state"]
        and (
            actor is None
            or (
                actor in tr["actors"]
                and (not tr["owner_scoped"] or actor == ticket["assignee"])
            )
        )
    ]


def notification_role(cfg: dict[str, Any], state: str, assignee: str) -> str | None:
    stage = next((s for s in cfg["stages"] if s["name"] == state), None)
    if not stage:
        return None
    policy, owners = stage["notify"], stage["owners"]
    if policy["kind"] == "fixed_role":
        return policy["role"]
    if policy["kind"] in {"assignee", "stage_owner_fallback"}:
        if assignee in owners:
            return assignee
        if policy["kind"] == "stage_owner_fallback" and owners:
            return owners[0]
    return None
