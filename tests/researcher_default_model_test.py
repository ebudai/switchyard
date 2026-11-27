"""SYRD-889: a new Researcher defaults to agy with Gemini 4 Hydrogen (High), not Claude Opus 5.5.

The User, 2026-11-27: "lets change that to gemini 4 hydrogen high please" -- the default model a new Researcher is
given. Pinned here, wherever Switchyard supplies it:

* **the agy fallback catalog** records `gemini-4-hydrogen-high` as the Researcher's default, last, so agy's first
  model -- every other agy role's default -- does not move; the catalog version moves with the table;
* **`switchyard new`'s Research stage question**: choosing a Researcher offers agy first and preselects
  `gemini-4-hydrogen-high`, from the recorded catalog and from a live `agy models` that lists it anywhere; no effort is
  asked on agy; any other runtime is offered as before (Claude: Opus 5.5 at high; Codex: its own first model); the
  plan's record carries the choice;
* **`team-launcher <project> design --researcher-runtime agy`** without a model records the same default;
* **the launcher settings `research-stage enable` gives a new Researcher**: Hydrogen on agy, effort removed; a named
  model wins; on any other runtime with no model, nothing (the template's stay, as before);
* **sign-in**: a Researcher projected on agy, the tenant's only agy role, gets agy's sign-in step in `switchyard
  new`'s first-run phase, and `new`'s CLI checks are handed its runtime.

The real `switchyard research-stage` command is driven in research_stage_test.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import research_onboarding as onboarding  # noqa: E402
from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board import research_stage as rs  # noqa: E402
from scripts.ticket_board import runtime_catalog as catalog  # noqa: E402

CHECKS = 0
HYDROGEN = "gemini-4-hydrogen-high"


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def test_the_agy_catalog_records_the_researcher_default() -> None:
    agy = [choice.value for choice in catalog.RECORDED_MODELS["agy"]]
    check(HYDROGEN in agy, f"agy's recorded catalog has {HYDROGEN}: {agy}")
    check(agy[0] == "gemini-3.7-flash-high", f"and agy's first model, every other agy role's default, stays: {agy}")
    check(rs.RESEARCHER_DEFAULT_RUNTIME == "agy" and rs.RESEARCHER_DEFAULT_MODEL == HYDROGEN,
          f"the Researcher's defaults: {rs.RESEARCHER_DEFAULT_RUNTIME} {rs.RESEARCHER_DEFAULT_MODEL}")
    check(not catalog.runtime_takes_effort("agy"), "agy takes no effort, so 'high' is part of the model id")
    check(catalog.CATALOG_VERSION == 5 and catalog.CATALOG_RECORDED == "2026-11-27",
          f"the recorded tables changed, so their version and date did: v{catalog.CATALOG_VERSION} "
          f"{catalog.CATALOG_RECORDED}")
    check(catalog.default_model("claude") == "claude-opus-5-5" and catalog.default_model("agy") == "",
          "every other role's default is unchanged: Opus 5.5 on Claude, agy's own elsewhere")


def test_the_launcher_settings() -> None:
    check(rs.researcher_default_model("agy") == HYDROGEN, "on agy, Hydrogen")
    check(rs.researcher_default_model("claude") == "" and rs.researcher_default_model("codex") == "",
          "on any other runtime, none of the Researcher's own")
    check(onboarding.researcher_launcher_settings("agy") == {"model": HYDROGEN, "effort": ""},
          f"enable on agy: Hydrogen, and no effort: {onboarding.researcher_launcher_settings('agy')}")
    check(onboarding.researcher_launcher_settings("agy", model="gemini-4-hydrogen-xhigh")
          == {"model": "gemini-4-hydrogen-xhigh", "effort": ""}, "a named model wins")
    check(onboarding.researcher_launcher_settings("claude") == {} and onboarding.researcher_launcher_settings("codex") == {},
          "another runtime with no model keeps the template's model and effort")
    check(onboarding.researcher_launcher_settings("claude", model=" claude-opus-5-5 ") == {"model": "claude-opus-5-5"},
          "a model named for Claude is set, its effort left alone")


class Asked:
    """The answers a person types, in order, and every prompt they were shown."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"FAILED CHECK: a question nobody expected: {prompt!r}")
        return self.answers.pop(0)


def choose(asked: Asked, *, runner=None):
    out: list[str] = []
    chosen = onboarding.prompt_research(["main", "ops"], runner=runner, owner_args=(),
                                        unverified_because="" if runner else "not yet", input_func=asked,
                                        print_func=out.append)
    return chosen, out


def test_new_offers_agy_and_hydrogen_first() -> None:
    field = onboarding.research_field(["main", "ops"])
    researcher = next(choice for choice in field.choices if choice.value == "researcher")
    check("agy" in researcher.description and "Gemini 4 Hydrogen (High)" in researcher.description,
          f"the menu says where a Researcher starts: {researcher.description}")
    check(onboarding.RESEARCHER_DEFAULT_CLI == "agy", "agy is the runtime offered first")

    asked = Asked("4", "", "")
    chosen, out = choose(asked)
    check(chosen == launcher.RoleSelection(rs.RESEARCHER_ROLE, "agy", model=HYDROGEN, effort=""),
          f"Enter, Enter: a Researcher on agy with Hydrogen and no effort: {chosen}")
    check(len(asked.prompts) == 3 and not any("effort" in p for p in asked.prompts),
          f"no effort is asked on agy: {asked.prompts}")
    check(asked.prompts[1] == "researcher runtime [Antigravity]: "
          and asked.prompts[2] == "researcher model [Gemini 4 Hydrogen (High)]: ",
          f"Enter's answer, shown in each question: agy, then Hydrogen: {asked.prompts}")
    rest, record = onboarding.split_role_plan([chosen])
    check(rest == () and record == (("stage", "researcher"), ("runtime", "agy"), ("model", HYDROGEN)),
          f"and the plan records it: {record}")

    # A live `agy models` in the owner's account, listing Hydrogen after others: still preselected.
    listing = "gemini-3.7-flash-high  Gemini 3.7 Flash\ngemini-4-hydrogen-low  low\n" \
              f"{HYDROGEN}  Gemini 4 Hydrogen (High)\ngemini-4-hydrogen-xhigh  xhigh\n"

    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0 if argv[-2:] == ["agy", "models"] else 1, listing, "")

    chosen, out = choose(Asked("4", "", ""), runner=runner)
    check(chosen.model == HYDROGEN and any("listed by agy models" in line for line in out),
          f"from the account's own list too: {chosen} {out[-2:]}")

    chosen, _ = choose(Asked("4", "claude", "", ""))
    check(chosen == launcher.RoleSelection(rs.RESEARCHER_ROLE, "claude", model="claude-opus-5-5", effort="high"),
          f"choosing Claude offers Opus 5.5 at high, as before: {chosen}")
    chosen, _ = choose(Asked("4", "codex", "", ""))
    check(chosen.cli == "codex" and chosen.model == catalog.RECORDED_MODELS["codex"][0].value,
          f"and Codex its own first model: {chosen}")
    chosen, _ = choose(Asked("4", "", "gemini-3.7-pro"))
    check(chosen.model == "gemini-3.7-pro", f"any other agy model is still one answer away: {chosen}")


def test_design_records_the_default() -> None:
    from scripts import project_design_command as design

    def no_input(prompt: str) -> str:
        raise AssertionError(f"FAILED CHECK: design asked {prompt!r} with every answer given")

    with tempfile.TemporaryDirectory(prefix="syrd889-design.") as tmp:
        recorded = {}
        repo = Path(tmp) / "repo"
        repo.mkdir()
        for name, model in (("plain", None), ("named", "gemini-4-hydrogen-medium")):
            artifact = Path(tmp) / f"{name}.project.json"
            code = design.design_project_command(
                "rsd", artifact_path=artifact, design_document=Path(tmp) / f"{name}-design.md", design_title="RSD",
                design_body="A project.", repository=repo, remote="origin", default_branch="main",
                worktree_policy="shared", owner_user="rsd-agent", ticket_prefix="RSD", implementer_roles=["main", "ops"],
                push_policy="director-main-only", audit_signoff=True, needs_inspection=False, needs_user_signoff=False,
                board_service_traversal=True, supplementary_groups=[], linger=True, owner_shell="fish",
                researcher_runtime="agy", researcher_model=model, input_func=no_input, print_func=lambda _l: None)
            check(code == 0, f"design {name} succeeds: {code}")
            recorded[name] = json.loads(artifact.read_text())["project"].get("research")
    check(recorded["plain"] == {"stage": "researcher", "runtime": "agy", "model": HYDROGEN},
          f"--researcher-runtime agy alone records Hydrogen: {recorded['plain']}")
    check(recorded["named"]["model"] == "gemini-4-hydrogen-medium", f"a named model wins: {recorded['named']}")


def test_an_agy_researcher_signs_in() -> None:
    from team_launcher_test_helpers import FirstRunAuthRunner, _write_first_run_auth_config
    from scripts.workflow_launcher import project_roles
    from documentation_gate_board_test import example

    # The projection `new` and `enable` write: the Researcher's entry runs agy, whatever its template ran.
    raw = {"project": "pgu", "roles": [{"role": "main", "cli": ["codex"], "model": "gpt-5.6-sol", "effort": "high"}]}
    projected = project_roles(raw, rs.with_research_stage(example(), researcher={"runtime": "agy", "slot": None}))
    entry = next(r for r in projected["roles"] if r["role"] == rs.RESEARCHER_ROLE)
    check(entry["cli"] == ["agy"], f"the projected Researcher runs agy: {entry['cli']}")

    with tempfile.TemporaryDirectory(prefix="syrd889-signin.") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home" / "otto-agent"
        home.mkdir(parents=True)
        config = launcher.load_project_config("otto", _write_first_run_auth_config(
            tmp_path, roles=[("director", "claude"), ("main", "codex"), (rs.RESEARCHER_ROLE, "agy")],
            role_models={rs.RESEARCHER_ROLE: HYDROGEN}))
        runner = FirstRunAuthRunner(unauthenticated_clis={"agy"}, model_catalogs={"agy": [HYDROGEN]})
        manifest = launcher.build_first_run_setup_manifest(config, owner_user="otto-agent", owner_home=home,
                                                           runner=runner)
    agy = [step for step in manifest.login_steps if step.cli == "agy"]
    check(len(agy) == 1 and agy[0].roles == (rs.RESEARCHER_ROLE,) and list(agy[0].command) == ["agy"],
          f"the tenant's only agy role, the Researcher, gets agy's sign-in step: {manifest.login_steps}")
    research = (("stage", "researcher"), ("runtime", "agy"), ("model", HYDROGEN))
    check(onboarding.with_researcher_cli((("director", "claude"), ("main", "codex")), research)[-1]
          == (rs.RESEARCHER_ROLE, "agy"), "and new's CLI checks are handed its runtime")


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"researcher_default_model_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
