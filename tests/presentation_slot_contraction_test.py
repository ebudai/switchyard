#!/usr/bin/env python3
"""SYRD-262: a presentation that only ever grew kept MEFP's window at six panes.

After MEFP's roles were put back on its four-pane layout (slots 0-3, workflow
revision 5), `switchyard start mefp` opened SIX panes: Director, Audit, Main,
Ops and two inert "display slot hidden" panes. The stored presentation state
still said six -- the count its example-derived layout (Director 1, Main 2,
Audit 4, Ops 5) had once needed -- and `_read_state` grew a stored count to
fit the projection but never shrank it back.

It shrinks now, to what is still in use: never below what the configuration
projects, and never past a slot a named layout, or a non-default active
mapping, still shows somebody in. Workers are not involved at all.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import team_launcher_presentation_test as fixtures  # noqa: E402
from team_launcher_test_helpers import team_launcher  # noqa: E402
from scripts import presentation_controller as presentation  # noqa: E402

CHECKS = 0
PROJECT = "porter"
FOUR = (("director", 0), ("audit", 1), ("main", 2), ("ops", 3))
#: Where the example-derived declaration had put them.
SIX = (("director", 1), ("main", 2), ("audit", 4), ("ops", 5))


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def write_config(root: Path, placement, *, section: dict | None = None) -> Path:
    layout = root / "layout.json"
    count = max(slot for _role, slot in placement) + 1
    layout.write_text(json.dumps(team_launcher._new_project_layout_payload(count)), encoding="utf-8")
    roles = []
    for role, slot in placement:
        (root / role).mkdir(exist_ok=True)
        roles.append({
            "role": role, "slot": slot, "tmux_session": f"{PROJECT}-{role}",
            "target": f"{PROJECT}-{role}:0.0", "workdir": str(root / role),
            "cli": ["codex"], "live_commands": ["codex"],
        })
    path = root / f"{PROJECT}.json"
    path.write_text(json.dumps({
        "project": PROJECT, "layout": str(layout), "session_dir": str(root / "sessions"),
        "presentation": section if section is not None
        else team_launcher.presentation_section_for_roles(roles),
        "roles": roles,
    }), encoding="utf-8")
    return path


def stale_state(root: Path, state_path: Path, *, mutate=None) -> None:
    """The state a six-slot era left behind, saved exactly as the controller saves it."""
    (root / "old").mkdir()
    old_path = write_config(root / "old", SIX)
    old = team_launcher.load_project_config(PROJECT, old_path)
    document = presentation.default_presentation_document(old, config_path=old_path)
    check(document["slot_count"] == 6, f"the old era needed six: {document['slot_count']}")
    if mutate:
        mutate(document)
    state_path.write_text(json.dumps(document), encoding="utf-8")


def read(root: Path, state_path: Path, **config_kwargs) -> dict:
    config_path = write_config(root, FOUR, **config_kwargs)
    config = team_launcher.load_project_config(PROJECT, config_path)
    return presentation._read_state(state_path, config=config, config_path=config_path)


def test_a_stale_six_slot_state_contracts_to_the_four_panes_in_use() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd262-contract.") as raw:
        root, state_path = Path(raw), Path(raw) / "presentation.json"
        stale_state(root, state_path, mutate=lambda d: d.update(focused_slot=5))
        state = read(root, state_path)
        check(state["slot_count"] == 4, f"four roles, four panes: {state['slot_count']}")
        check(state["slots"] == {"0": "director", "1": "audit", "2": "main", "3": "ops"},
              f"at the slots they are configured at: {state['slots']}")
        check(all(set(m) == {"0", "1", "2", "3"} for m in state["layouts"].values()),
              f"no layout keeps a slot past the window: {state['layouts']}")
        check(state["focused_slot"] == 0, f"focus that fell off the end comes back: {state['focused_slot']}")


def test_a_named_layout_keeps_every_slot_it_shows_somebody_in() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd262-named.") as raw:
        root, state_path = Path(raw), Path(raw) / "presentation.json"

        def named(document):
            document["layouts"]["review"] = {str(s): None for s in range(6)}
            document["layouts"]["review"]["4"] = "audit"

        stale_state(root, state_path, mutate=named)
        state = read(root, state_path)
        check(state["slot_count"] == 5, f"a custom layout's slot 4 keeps the window at five: {state['slot_count']}")
        check(state["layouts"]["review"]["4"] == "audit", f"and the layout is intact: {state['layouts']['review']}")


def test_a_custom_active_mapping_keeps_its_slots() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd262-active.") as raw:
        root, state_path = Path(raw), Path(raw) / "presentation.json"

        def active(document):
            document["layouts"]["mine"] = {str(s): None for s in range(6)}
            document["active_layout"] = "mine"
            document["slots"] = {str(s): None for s in range(6)}
            document["slots"]["5"] = "ops"

        stale_state(root, state_path, mutate=active)
        state = read(root, state_path)
        check(state["slot_count"] == 6, f"what the operator is looking at is not taken away: {state['slot_count']}")
        check(state["slots"]["5"] == "ops", state["slots"])


def test_a_count_the_configuration_asks_for_is_honoured() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd262-explicit.") as raw:
        root, state_path = Path(raw), Path(raw) / "presentation.json"
        stale_state(root, state_path)
        section = {"slot_count": 6, "layouts": {"default": {}, "spare": {"5": "ops"}}}
        state = read(root, state_path, section=section)
        check(state["slot_count"] == 6, f"a configured six stays six: {state['slot_count']}")


def test_an_ordinary_start_from_a_stale_state_opens_only_the_panes_in_use() -> None:
    """`switchyard start` builds the window from this state.

    Driven through the presentation suite's own native-window fixture, whose
    simulated desktop models its two-role project (director 0, app 1): the
    same defect as MEFP's six-to-four, as four-to-two. The stored state is one
    a wider era left behind, saved exactly as the controller saves it.
    """
    with tempfile.TemporaryDirectory(prefix="syrd262-start.") as raw:
        root = Path(raw)
        config_path = fixtures._write_presentation_config(root)
        config = team_launcher.load_project_config(PROJECT, config_path)
        state_path = root / "presentation.json"
        wider = presentation.default_presentation_document(config, config_path=config_path)
        wider["slot_count"] = 4
        for mapping in (wider["slots"], *wider["layouts"].values()):
            mapping.update({"2": None, "3": None})
        state_path.write_text(json.dumps(wider), encoding="utf-8")
        runner = fixtures.PresentationRunner()
        # The launch every `switchyard start` makes: it reads the state,
        # creates one display per stored slot, and saves the state -- all
        # before any window. (Its native-window variant is exercised by
        # team_launcher_presentation_test, whose window simulation is red on
        # main in this environment; this one needs none.)
        presentation.launch_presentation(
            config, config_path=config_path, state_path=state_path, layout="viewer", runner=runner,
        )
        started = json.loads(state_path.read_text(encoding="utf-8"))
        check(started["slot_count"] == 2 and started["slots"] == {"0": "director", "1": "app"},
              f"the window has the two slots in use: {started}")
        displays = sorted(s for s in runner.sessions if s.startswith(f"{PROJECT}-display-"))
        check(displays == [f"{PROJECT}-display-0", f"{PROJECT}-display-1"],
              f"and two displays, no inert hidden ones: {displays}")
        check("focused_slot" in started and started["focused_slot"] < 2, "and focus is on a slot that exists")


def test_the_section_rule_is_the_one_the_migration_wrote_with() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd262-rule.") as raw:
        root = Path(raw)
        for placement in (FOUR, SIX):
            (root / str(len(placement) + placement[0][1])).mkdir()
            path = write_config(root / str(len(placement) + placement[0][1]), placement)
            config = team_launcher.load_project_config(PROJECT, path)
            raw_roles = json.loads(path.read_text())["roles"]
            check(team_launcher.presentation_section_for_roles(raw_roles)
                  == team_launcher.legacy_presentation_section(config),
                  f"one rule for both: {placement}")


def test_only_a_section_switchyard_wrote_is_re_derived() -> None:
    reconcile = team_launcher._reconciled_presentation_section
    roles = [{"role": r, "slot": s} for r, s in FOUR]
    derived = {"presentation": {"slot_count": 6, "layouts": {"default": {"1": "director", "5": "ops"}}}, "roles": roles}
    out = json.loads(reconcile(json.dumps(derived)))
    check(out["presentation"] == {"slot_count": 4, "layouts": {"default": {"0": "director", "1": "audit", "2": "main", "3": "ops"}}},
          f"the migration's own section follows the slots now configured: {out['presentation']}")
    custom = {"presentation": {"slot_count": 6, "layouts": {"default": {}, "spare": {"5": "ops"}}}, "roles": roles}
    check(reconcile(json.dumps(custom)) == json.dumps(custom), "one with a layout somebody made is left alone")
    extra = {"presentation": {"slot_count": 6, "layouts": {"default": {}}, "window": "tall"}, "roles": roles}
    check(reconcile(json.dumps(extra)) == json.dumps(extra), "and so is one with anything else in it")
    none = {"roles": roles}
    check(reconcile(json.dumps(none)) == json.dumps(none), "no section is not invented")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"presentation_slot_contraction_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
