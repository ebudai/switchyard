#!/usr/bin/env python3
"""SYRD-781: a Librarian chosen at `switchyard new` gets a display slot whenever the window has one; Router never.

The User, creating test11: "I chose to add the librarian, but it did not give that role a pane, and started 5 instead
of 6 slots". The Librarian was declared `slot: null`, by design then. The User's rule now: "it should only not get a
slot if I have 6 filled already, but if I only have 5, there is room so why not" -- and "no, don't show the router".

Here, `switchyard new`'s real composition -- a real provisioning plan (`project_provision.build_plan`), its declared
workflow (`router_onboarding.plan_document`), Router's and the gate's (`plan_with_router`,
`plan_with_documentation_gate`) -- is projected into a real launcher configuration and layout (`projection_files`)
and loaded as the launcher loads it, and the presentation's default mapping is read from that configuration:
* five roles and a Librarian: six slots, the Librarian in slot 5;
* six roles: the Librarian in the background, and the summary says so and how to see it;
* no Librarian: five slots;
* Router: never a slot;
* the Librarian stays ephemeral, and its next session is cleared in its own pane, which its slot keeps showing; an
  existing window's saved presentation grows to the new slot when it is relaunched.
"""

from __future__ import annotations

import json
import os
import pwd
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import documentation_onboarding, router_onboarding, team_launcher  # noqa: E402
from scripts.ticket_board import documentation_gate, project_provision  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from scripts.workflow_launcher import projection_files  # noqa: E402

CHECKS = 0
ME = pwd.getpwuid(os.getuid()).pw_name
PROJECT = "t781"
LIBRARIAN = (("gate", "librarian"), ("runtime", "claude"), ("model", "claude-opus-5-5"), ("effort", "high"))
ROUTER = (("runtime", "claude"), ("model", "claude-haiku-5-5"), ("effort", "high"))


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def compose(tmp: Path, implementers: tuple[str, ...], *, designer: bool = True, documentation=LIBRARIAN,
            router=()) -> tuple[dict, list[str], tuple]:
    """`switchyard new`'s composed workflow for these roles: (document, what it said, the role/CLI pairs)."""
    role_clis = ((("designer", "claude"),) if designer else ()) + (("director", "claude"), ("audit", "claude")) \
        + tuple((role, "codex") for role in implementers)
    repo = tmp / "repo"
    (repo / "docs" / "onboarding").mkdir(parents=True, exist_ok=True)
    plan = project_provision.build_plan(project=PROJECT, owner_user=ME, owner_home=tmp, implementer_roles=implementers,
                                        include_designer=designer, include_audit=True)
    said: list[str] = []
    if router:
        plan = router_onboarding.plan_with_router(plan, router=router, role_clis=role_clis, repository=repo,
                                                  print_func=said.append)
    if documentation:
        try:
            plan = documentation_onboarding.plan_with_documentation_gate(plan, documentation=documentation,
                                                                         role_clis=role_clis, repository=repo,
                                                                         print_func=said.append)
        except SystemExit as exc:  # a refused composition is a finding, not a crash
            raise AssertionError(f"FAILED CHECK: the composition with the gate was refused: {exc}") from None
    if plan.workflow is None:  # neither chosen: the document an existing project of these roles declares
        return router_onboarding.plan_document(plan, role_clis=role_clis, repository=repo, label="test",
                                               print_func=said.append), said, role_clis
    return plan.workflow, said, role_clis


def projected(tmp: Path, document: dict, role_clis: tuple) -> tuple[object, Path, dict]:
    """The launcher configuration and layout projected from `document`, loaded as the launcher loads them."""
    cli = tmp / "bin" / "claude"
    cli.parent.mkdir(exist_ok=True)
    cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cli.chmod(0o755)
    config_path = tmp / "launcher" / f"{PROJECT}.json"
    config_path.parent.mkdir(exist_ok=True)
    slots = {role: index for index, (role, _cli) in enumerate(role_clis) if index < 6}
    config_path.write_text(json.dumps({
        "project": PROJECT, "layout": "layout.json", "desktop_access": {"mode": "headless"},
        "board_url": "http://127.0.0.1:1", "board_socket": str(tmp / "board.sock"),
        "session_dir": str(tmp / "sessions"), "run_as_user": ME, "role_state_isolation": True,
        "repository": str(tmp / "repo"),
        "roles": [{"role": role, "cli": [str(cli)], **({"slot": slots[role]} if role in slots else {"detached": True}),
                   "target": f"{PROJECT}-{role}:0.0", "workdir": str(tmp / "repo")} for role, _c in role_clis],
    }), encoding="utf-8")
    files = projection_files(config_path, document)
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")
    layout = json.loads(files[config_path.parent / "layout.json"])
    return team_launcher.load_project_config(PROJECT, config_path), config_path, layout


def role(document: dict, name: str) -> dict:
    return next((r for r in document["roles"] if r["name"] == name), {})


def test_the_limit_is_the_windows() -> None:
    import importlib.machinery

    attach = importlib.machinery.SourceFileLoader("display_attach", str(ROOT / "scripts" / "switchyard-display-attach"))
    source = (ROOT / "scripts" / "switchyard-display-attach").read_text(encoding="utf-8")
    check(documentation_gate.VISIBLE_SLOTS == team_launcher.MAX_VISIBLE_PANES_PER_WINDOW == 6
          and "MAX_SLOTS = 6" in source and attach is not None,
          "six display slots: the gate's limit, the launcher's and display-attach's")
    roles = [{"name": f"r{i}", "active": True, "slot": i} for i in range(5)]
    check(documentation_gate.free_visible_slot({"roles": roles}) == 5, "five taken: slot 5 is free")
    check(documentation_gate.free_visible_slot({"roles": roles + [{"name": "r5", "active": True, "slot": 5}]}) is None,
          "six taken: none")
    check(documentation_gate.free_visible_slot({"roles": [*roles[:2], *roles[3:],
                                                         {"name": "old", "active": False, "slot": 2}]}) == 2,
          "the lowest free one; an inactive role holds none")


def test_five_roles_and_a_librarian_render_six() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd781.") as raw:
        tmp = Path(raw)
        document, said, role_clis = compose(tmp, ("main", "ops"))
        librarian = role(document, "librarian")
        check(librarian.get("slot") == 5 and librarian.get("ephemeral") is True,
              f"test11's choices -- designer, director, audit, main, ops and a Librarian: the Librarian in slot 5, "
              f"still a fresh session per ticket: {librarian}")
        check(not any("Librarian takes display slot" in line or "runs in the background" in line for line in said),
              f"the composition agrees with the summary, so nothing is said twice: {said}")
        config, config_path, layout = projected(tmp, document, role_clis)
        entry = next(r for r in config.roles if r.role == "librarian")
        check(entry.slot == 5 and not entry.detached and entry.target == f"{PROJECT}-librarian:0.0",
              f"its launcher entry is shown in slot 5, in its own session: {entry.slot} {entry.detached}")
        check(layout == team_launcher._new_project_layout_payload(6), "the window is laid out for six slots")
        line = documentation_onboarding.summary_line(LIBRARIAN, slot=documentation_onboarding.librarian_slot(role_clis),
                                                     project=PROJECT)
        check("it has a pane of its own, display slot 5" in line and "background" not in line,
              f"the summary before anything is created says it has a pane: {line}")


def test_six_roles_leave_the_librarian_in_the_background() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd781.") as raw:
        tmp = Path(raw)
        document, _said, role_clis = compose(tmp, ("main", "ops", "app"))
        check(role(document, "librarian").get("slot") is None and len(role_clis) == 6,
              f"six roles fill the window, so the Librarian runs in the background: {role(document, 'librarian')}")
        config, _path, layout = projected(tmp, document, role_clis)
        entry = next(r for r in config.roles if r.role == "librarian")
        check(entry.detached and layout == team_launcher._new_project_layout_payload(6),
              "its launcher entry is detached, and the window keeps its six slots")
        line = documentation_onboarding.summary_line(LIBRARIAN, slot=documentation_onboarding.librarian_slot(role_clis),
                                                     project=PROJECT)
        check("all six display slots are taken, so it runs in the background" in line
              and f"switchyard attach {PROJECT} librarian" in line
              and f"switchyard present {PROJECT} show librarian --slot <n>" in line,
              f"and the summary says so, and how to see it: {line}")


def test_without_a_librarian_five_slots() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd781.") as raw:
        tmp = Path(raw)
        document, _said, role_clis = compose(tmp, ("main", "ops"), documentation=(("gate", "director"),))
        check(not role(document, "librarian"), "the Director's gate creates no Librarian")
        _config, _path, layout = projected(tmp, document, role_clis)
        check(layout == team_launcher._new_project_layout_payload(5), "and the window has five slots")


def test_router_never_gets_a_slot() -> None:
    for implementers, librarian_slot in ((("main", "ops"), 5), (("main",), 4)):
        with tempfile.TemporaryDirectory(prefix="syrd781.") as raw:
            tmp = Path(raw)
            document, _said, role_clis = compose(tmp, implementers, router=ROUTER)
            check(role(document, "router").get("slot") is None and role(document, "router").get("active"),
                  f"{implementers}: Router runs, in the background, even with a slot free: {role(document, 'router')}")
            check(role(document, "librarian").get("slot") == librarian_slot,
                  f"{implementers}: and the Librarian takes the next free one: {role(document, 'librarian')}")
            config, _path, _layout = projected(tmp, document, role_clis)
            check(next(r for r in config.roles if r.role == "router").detached, "Router's launcher entry is detached")
    line = router_onboarding.summary_line(ROUTER, project=PROJECT)
    check(f"it runs in the background with no pane (`switchyard attach {PROJECT} router` shows it)" in line,
          f"the summary says Router has no pane, and how to see it: {line}")


def test_the_ephemeral_librarians_next_session_shows_in_its_slot() -> None:
    from scripts import presentation_state_document as presentation
    from scripts.ticket_board import notification_session_clear

    with tempfile.TemporaryDirectory(prefix="syrd781.") as raw:
        tmp = Path(raw)
        document, _said, role_clis = compose(tmp, ("main", "ops"))
        config, config_path, _layout = projected(tmp, document, role_clis)
        default = presentation.default_presentation_document(config, config_path=config_path)
        check(default["slot_count"] == 6 and default["slots"].get("5") == "librarian",
              f"the window's slot 5 shows the Librarian's session: {default['slots']}")
        # A saved five-slot presentation -- an existing window -- grows to the new slot when it is next launched.
        state_path = tmp / "presentation.json"
        saved = {**default, "slot_count": 5, "slots": {k: v for k, v in default["slots"].items() if k != "5"},
                 "layouts": {name: {k: v for k, v in mapping.items() if k != "5"}
                             for name, mapping in default["layouts"].items()}}
        state_path.write_text(json.dumps(saved), encoding="utf-8")
        grown = presentation._read_state(state_path, config=config, config_path=config_path)
        check(grown["slot_count"] == 6 and grown["slots"].get("5") == "librarian",
              f"a relaunched window takes the slot, showing the Librarian: {grown['slots']}")
    # Each fresh session is a clear typed into the Librarian's own pane, which the slot keeps showing: never a new
    # session the slot would have to find.
    check(notification_session_clear.SESSION_CLEAR_COMMANDS["claude"] == "/clear"
          and "self.sender()(target, command)" in Path(notification_session_clear.__file__).read_text(),
          "the next ticket's fresh session is the same pane, cleared")


def test_documentation_gate_enable_takes_a_free_slot() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd781.") as raw:
        tmp = Path(raw)
        document, _said, _role_clis = compose(tmp, ("main", "ops"), documentation=())
        enabled = documentation_gate.with_documentation_gate(
            document, librarian={"runtime": "claude", "slot": documentation_gate.free_visible_slot(document)})
        check(role(enabled, "librarian").get("slot") == 5, f"an existing project's Librarian takes the free slot")
        check(role(documentation_gate.with_documentation_gate(document, librarian={"runtime": "claude"}),
                   "librarian").get("slot") is None, "and none named is the background")
        try:
            validate(dict(enabled, roles=[dict(r, slot=0) if r["name"] == "librarian" else r
                                          for r in enabled["roles"]]))
            taken = ""
        except Exception as exc:  # noqa: BLE001 - the validator's refusal is the answer
            taken = str(exc)
        check("visible slots must be unique" in taken, f"a taken slot is refused: {taken}")
    source = (ROOT / "scripts" / "workflow_manage.py").read_text(encoding="utf-8")
    check('librarian["slot"] = free_visible_slot(current["document"])' in source
          and "window once the window is relaunched" in source,
          "documentation-gate enable gives it the free slot and says the window must be relaunched")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"librarian_display_slot_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
