#!/usr/bin/env python3
"""SYRD-115: constrained provisioning answers are chosen, not recalled.

The flow this replaces asked an operator to type values that have a known finite
set. A role's runtime was chosen by reading `claude/codex/agy/hermes` out of the
prompt text and typing one back. The implementer roles were one comma-separated
string, so a typo in the middle of it was a role nobody asked for and a role
nobody noticed was missing. A model was an identifier to remember, and there was
no list of them anywhere in the launcher to remember it from.

The cases here are the ticket's acceptance list, one apiece: defaults,
multi-select roles, dependent choices, large catalogs, offline fallback, stale
configured values, cancellation, and non-TTY operation.

Every case drives the real selector through the same injected `input_func` /
`print_func` pair the launcher's own prompts use, so what is asserted is what an
operator would have typed and seen.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import runtime_catalog as catalog  # noqa: E402
from scripts.ticket_board import terminal_select as select  # noqa: E402
from scripts.ticket_board.prompt_schema import (  # noqa: E402
    KIND_BOOL,
    KIND_MULTI,
    KIND_SINGLE,
    KIND_TEXT,
    Choice,
    Field,
    Schema,
    SchemaError,
    review_lines,
    with_existing_value,
)

CHECKS = 0


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


class Terminal:
    """An operator at a keyboard: answers in order, and everything printed."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.screen: list[str] = []
        self.prompts: list[str] = []

    def input(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"asked more than it was answered: {prompt!r}")
        return self.answers.pop(0)

    def print(self, line: str) -> None:
        self.screen.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self.screen)


RUNTIME = Field(
    name="runtime", kind=KIND_SINGLE, title="Runtime", choices=catalog.RUNTIMES,
)


def _model_field(runtime_answer: str, *, configured: str = "") -> Field:
    def choices(answers):
        found = catalog.model_catalog(answers["runtime"], configured=configured)
        return with_existing_value(found.choices, configured)

    return Field(
        name="model", kind=KIND_SINGLE, title="Model", depends_on=("runtime",),
        choices=choices, allow_custom=True, default=configured or None,
    )


# --- defaults ---------------------------------------------------------------


def test_the_first_option_is_the_default_and_enter_takes_it() -> None:
    """The rule the ticket states, and the one Hermes's own picker follows."""
    terminal = Terminal("")
    chosen = select.select_one(
        RUNTIME, input_func=terminal.input, print_func=terminal.print
    )
    check(chosen == catalog.RUNTIMES[0].value,
          f"Enter took the first option: {chosen}")
    check("[default]" in terminal.text,
          f"and the default is visibly marked: {terminal.text}")
    check(terminal.prompts[0].startswith("Runtime ["),
          f"and named in the prompt: {terminal.prompts}")


def test_a_choice_can_be_made_by_number_or_by_name() -> None:
    by_number = Terminal("2")
    by_name = Terminal("codex")
    check(
        select.select_one(RUNTIME, input_func=by_number.input, print_func=by_number.print)
        == select.select_one(RUNTIME, input_func=by_name.input, print_func=by_name.print)
        == "codex",
        "the second option is reachable as '2' and as its own name",
    )


def test_every_selector_lists_its_choices() -> None:
    """"Every selector visibly lists its valid choices" -- including the values."""
    terminal = Terminal("")
    select.select_one(RUNTIME, input_func=terminal.input, print_func=terminal.print)
    for choice in catalog.RUNTIMES:
        check(choice.display in terminal.text,
              f"{choice.display} was not listed: {terminal.text}")


def test_an_invalid_answer_never_becomes_the_value() -> None:
    """"Invalid entries do not reach the generated artifact"."""
    terminal = Terminal("gemini-pro", "nonsense", "")
    chosen = select.select_one(RUNTIME, input_func=terminal.input, print_func=terminal.print)
    check(chosen in {choice.value for choice in catalog.RUNTIMES},
          f"whatever was typed, the answer is one of the choices: {chosen}")
    check("no choice matches" in terminal.text,
          f"and the operator was told why not: {terminal.text}")


# --- multi-select roles -----------------------------------------------------


ROLES = Field(
    name="roles", kind=KIND_MULTI, title="Implementer roles",
    choices=(
        Choice("main", "main", "core/domain implementation and integration"),
        Choice("ops", "ops", "operations and tooling"),
        Choice("app", "app", "application/UI work"),
        Choice("research", "research"),
        Choice("perf", "perf"),
    ),
    default=("main", "ops"),
    allow_custom=True,
    custom_title="Add a role of your own",
)


def test_roles_are_selected_rather_than_typed_as_one_string() -> None:
    terminal = Terminal("1 3")
    picked = select.select_many(ROLES, input_func=terminal.input, print_func=terminal.print)
    check(picked == ("main", "app"), f"two numbers pick two roles: {picked}")


def test_the_default_role_set_is_one_keypress() -> None:
    terminal = Terminal("")
    picked = select.select_many(ROLES, input_func=terminal.input, print_func=terminal.print)
    check(picked == ("main", "ops"), f"Enter takes the conventional pair: {picked}")


def test_a_role_of_your_own_takes_a_deliberate_action_and_is_validated() -> None:
    """"Custom roles ... require a deliberate Custom action and are validated"."""
    def validate(value: str) -> str:
        if not value.islower():
            raise ValueError(f"role {value!r} must be lower case")
        return value

    field = Field(
        name="roles", kind=KIND_MULTI, title="Implementer roles",
        choices=ROLES.choices, default=("main",), allow_custom=True, validate=validate,
    )
    terminal = Terminal("c", "Billing", "billing", "")
    picked = select.select_many(field, input_func=terminal.input, print_func=terminal.print)
    check("billing" in picked, f"the custom role was added: {picked}")
    check("must be lower case" in terminal.text,
          f"and the invalid one was refused first: {terminal.text}")
    check("Billing" not in picked, "and never became a value")
    # And it REPLACED the default rather than joining it. Carrying "main" along
    # here would create a role nobody asked for -- the same silent extra the
    # comma-separated line used to produce, from the other direction.
    check(picked == ("billing",),
          f"pressing Enter confirms what was chosen, not the default too: {picked}")


def test_a_custom_value_is_never_reached_by_accident() -> None:
    """Typing something unknown filters or fails; it does not silently become custom."""
    terminal = Terminal("wat", "")
    picked = select.select_many(ROLES, input_func=terminal.input, print_func=terminal.print)
    check(picked == ("main", "ops"), f"the unknown token was not adopted: {picked}")
    check("no choice matches" in terminal.text, f"it was refused: {terminal.text}")


# --- dependent choices ------------------------------------------------------


def test_a_models_choices_follow_the_runtime_chosen_before_it() -> None:
    for runtime in ("claude", "codex", "agy"):
        field = _model_field(runtime)
        offered = {choice.value for choice in field.choices_for({"runtime": runtime})}
        expected = {choice.value for choice in catalog.RECORDED_MODELS[runtime]}
        check(offered == expected,
              f"{runtime} offers its own models and no others: {offered}")


def test_changing_the_runtime_recomputes_the_models() -> None:
    """"Dependent selections must be recalculated when an earlier answer changes"."""
    field = _model_field("claude")
    first = {choice.value for choice in field.choices_for({"runtime": "claude"})}
    second = {choice.value for choice in field.choices_for({"runtime": "codex"})}
    check(first != second, "the two runtimes do not offer the same models")
    check(not first & second, f"and nothing leaks between them: {first & second}")


def test_a_schema_refuses_a_dependency_it_asks_too_late() -> None:
    model = _model_field("claude")
    try:
        Schema((model, RUNTIME))
        raise AssertionError("a model asked before its runtime was accepted")
    except SchemaError as exc:
        check("is not asked before it" in str(exc), f"and says why: {exc}")


def test_a_runtime_without_effort_is_not_asked_for_one() -> None:
    """agy drops an effort level before it reaches the command line."""
    check(not catalog.runtime_takes_effort("agy"),
          "agy takes no effort level")
    check(catalog.effort_catalog("agy").choices == (),
          "so there is nothing to offer")
    for runtime in ("claude", "codex", "hermes"):
        check(catalog.runtime_takes_effort(runtime),
              f"{runtime} does take one")


# --- large catalogs ---------------------------------------------------------


def _big_catalog(count: int = 60) -> Field:
    return Field(
        name="model", kind=KIND_SINGLE, title="Model",
        choices=tuple(
            Choice(f"vendor/model-{index:03d}", description=f"synthetic entry {index}")
            for index in range(count)
        ),
        allow_custom=True,
    )


def test_a_large_catalog_pages_rather_than_flooding_the_screen() -> None:
    field = _big_catalog()
    terminal = Terminal("")
    select.select_one(field, input_func=terminal.input, print_func=terminal.print)
    listed = [line for line in terminal.screen if line.strip().startswith(("1)", "2)", "13)"))]
    check(any(line.strip().startswith("1)") for line in listed), "the first page is shown")
    check(not any(line.strip().startswith("13)") for line in listed),
          f"and the thirteenth entry is not: {terminal.text[:400]}")
    check("more" in terminal.text, f"with a way to see the rest: {terminal.text[:400]}")


def test_a_long_list_is_filtered_by_typing_part_of_a_name() -> None:
    """"never require exact identifier recall"."""
    field = _big_catalog()
    terminal = Terminal("model-042", "")
    chosen = select.select_one(field, input_func=terminal.input, print_func=terminal.print)
    check(chosen == "vendor/model-042",
          f"the filtered-to entry is what Enter then takes: {chosen}")
    check("one match" in terminal.text, f"and it said so: {terminal.text[-200:]}")


def test_a_number_is_a_position_and_a_padded_number_is_a_filter() -> None:
    """The one genuine ambiguity in this interaction, decided deliberately.

    `42` is how the entry printed as `42)` is chosen. `042` is nobody's list
    position -- it is somebody typing part of `vendor/model-042` -- and reading
    it as position 42 would select the entry above the one they were reading.
    """
    field = _big_catalog()
    as_position = Terminal("42")
    check(select.select_one(field, input_func=as_position.input,
                            print_func=as_position.print) == "vendor/model-041",
          "a plain number is the printed position")
    as_filter = Terminal("042", "")
    check(select.select_one(field, input_func=as_filter.input,
                            print_func=as_filter.print) == "vendor/model-042",
          "a zero-padded one filters to the identifier that contains it")


def test_paging_reaches_an_entry_beyond_the_first_page() -> None:
    field = _big_catalog()
    terminal = Terminal("more", "13")
    chosen = select.select_one(field, input_func=terminal.input, print_func=terminal.print)
    check(chosen == "vendor/model-012",
          f"the thirteenth entry is reachable by paging: {chosen}")


def test_a_filter_that_matches_nothing_restores_the_whole_list() -> None:
    field = _big_catalog()
    terminal = Terminal("zzzz", "")
    chosen = select.select_one(field, input_func=terminal.input, print_func=terminal.print)
    check(chosen == "vendor/model-000",
          f"and the default is the whole list's default again: {chosen}")


# --- offline fallback -------------------------------------------------------


def test_a_runtime_that_can_list_its_models_is_asked() -> None:
    def runner(argv, **_kwargs):
        check(argv[-2:] == ["agy", "models"], f"it asked the runtime itself: {argv}")
        return subprocess.CompletedProcess(argv, 0, stdout="gemini-3.9-pro  newest\n")

    found = catalog.model_catalog("agy", runner=runner)
    check([choice.value for choice in found.choices] == ["gemini-3.9-pro"],
          f"the live list is used: {found.choices}")
    check(found.provenance == catalog.PROVENANCE_LIVE, f"and marked live: {found.provenance}")
    check("agy models" in found.note, f"naming what produced it: {found.note}")


def test_an_unreachable_runtime_falls_back_to_the_recorded_catalog() -> None:
    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not logged in\n")

    found = catalog.model_catalog("agy", runner=runner)
    check(found.choices == catalog.RECORDED_MODELS["agy"],
          f"the recorded table answers instead: {found.choices}")
    check(found.provenance == catalog.PROVENANCE_RECORDED,
          f"and says it is recorded rather than live: {found.provenance}")
    check(str(catalog.CATALOG_VERSION) in found.note and catalog.CATALOG_RECORDED in found.note,
          f"with its version and date: {found.note}")


def test_a_failed_listing_is_not_read_as_a_catalog_because_it_printed_something() -> None:
    """The shape that makes the exit status matter.

    `agy models` is the launcher's authentication probe as well as its catalog:
    an account that is not signed in gets a refusal, and a refusal that lands on
    stdout instead of stderr is still not a list of models. Parsing it would
    offer "You" as a model and record it in a project.
    """
    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="You are not logged into Antigravity.\n"
        )

    found = catalog.model_catalog("agy", runner=runner)
    check(found.provenance == catalog.PROVENANCE_RECORDED,
          f"a non-zero exit is not a catalog whatever it printed: {found.provenance}")
    check(found.choices == catalog.RECORDED_MODELS["agy"],
          f"and the recorded table answers instead: {[c.value for c in found.choices]}")
    check(not any(choice.value == "You" for choice in found.choices),
          "and no word of the refusal became a model")


def test_a_runtime_that_cannot_be_enumerated_says_so_rather_than_inventing() -> None:
    found = catalog.model_catalog("hermes")
    check(found.choices == (), "hermes has no recorded models")
    check(found.provenance == catalog.PROVENANCE_EMPTY, f"and says so: {found.provenance}")
    check("its own picker" in found.note, f"and why: {found.note}")


def test_the_recorded_catalog_only_holds_values_this_repository_uses() -> None:
    """The table is a record, not a guess about a vendor's line-up.

    Every recorded model has to be traceable to something in-tree; this checks
    the launcher's own live configuration, which is where most of them come
    from, rather than trusting the comment above the table.
    """
    import json

    configured = json.loads((ROOT / "config" / "team-launcher" / "pgu.json").read_text())
    in_use = {
        (role["cli"][0], role.get("model", ""))
        for role in configured["roles"] if role.get("model")
    }
    for runtime, model in in_use:
        recorded = {choice.value for choice in catalog.RECORDED_MODELS.get(runtime, ())}
        check(model in recorded,
              f"{runtime}'s configured model {model} is missing from the catalog")


# --- stale configured values ------------------------------------------------


def test_a_configured_value_the_catalog_no_longer_offers_is_kept() -> None:
    """"An already-configured value that is no longer advertised must be shown
    as an existing unlisted value and preserved"."""
    field = _model_field("claude", configured="claude-opus-4-retired")
    offered = field.choices_for({"runtime": "claude"})
    check(offered[0].value == "claude-opus-4-retired",
          f"the project's own value comes first: {[c.value for c in offered]}")
    check(offered[0].unlisted, "and is marked as one the catalog no longer offers")
    terminal = Terminal("")
    chosen = select.select_one(
        field, {"runtime": "claude"}, input_func=terminal.input, print_func=terminal.print
    )
    check(chosen == "claude-opus-4-retired",
          f"and Enter keeps it rather than re-pointing the project: {chosen}")
    check("not currently offered" in terminal.text,
          f"with the fact said out loud: {terminal.text}")


def test_replacing_a_stale_value_takes_a_deliberate_choice() -> None:
    field = _model_field("claude", configured="claude-opus-4-retired")
    terminal = Terminal("2")
    chosen = select.select_one(
        field, {"runtime": "claude"}, input_func=terminal.input, print_func=terminal.print
    )
    check(chosen == "claude-opus-5", f"choosing the listed one replaces it: {chosen}")


# --- cancellation -----------------------------------------------------------


def test_cancelling_stops_rather_than_guessing() -> None:
    terminal = Terminal("cancel")
    try:
        select.select_one(RUNTIME, input_func=terminal.input, print_func=terminal.print)
        raise AssertionError("cancelling returned a value")
    except select.Cancelled as exc:
        check("runtime" in str(exc), f"and names the question it stopped at: {exc}")


def test_an_operator_who_keeps_answering_badly_is_not_asked_for_ever() -> None:
    terminal = Terminal(*["nonsense"] * (select.MAX_ATTEMPTS + 2))
    try:
        select.select_one(RUNTIME, input_func=terminal.input, print_func=terminal.print)
        raise AssertionError("an unbounded prompt loop returned")
    except select.Cancelled:
        check(len(terminal.prompts) <= select.MAX_ATTEMPTS,
              f"it gave up after a bounded number of tries: {len(terminal.prompts)}")


# --- non-TTY operation ------------------------------------------------------


def test_a_scripted_run_never_prompts() -> None:
    """"Existing scripted invocations ... do not become interactive"."""
    def refuse(prompt: str) -> str:
        raise AssertionError(f"a non-interactive run prompted: {prompt!r}")

    chosen = select.select_one(RUNTIME, interactive=False, input_func=refuse)
    check(chosen == catalog.RUNTIMES[0].value, f"it took the default: {chosen}")
    picked = select.select_many(ROLES, interactive=False, input_func=refuse)
    check(picked == ("main", "ops"), f"and the default role set: {picked}")


def test_a_supplied_value_is_checked_against_the_same_choices() -> None:
    check(select.select_one(RUNTIME, preset="agy", interactive=False) == "agy",
          "a supplied runtime is accepted")
    try:
        select.select_one(RUNTIME, preset="gemini", interactive=False)
        raise AssertionError("a value nobody could have chosen was accepted")
    except select.NoAnswer as exc:
        check("not one of" in str(exc), f"and the refusal lists the choices: {exc}")


def test_a_scripted_run_with_nothing_to_go_on_refuses_instead_of_hanging() -> None:
    field = Field(name="name", kind=KIND_TEXT, title="Project name")
    try:
        select.ask_text(field, interactive=False)
        raise AssertionError("a required open value was invented")
    except select.NoAnswer as exc:
        check("not supplied" in str(exc), f"and says what is missing: {exc}")


# --- the review summary -----------------------------------------------------


def test_the_review_shows_every_choice_as_a_label_and_a_stored_value() -> None:
    schema = Schema((RUNTIME, _model_field("claude")))
    lines = review_lines(schema, {"runtime": "claude", "model": "claude-opus-5"})
    check(any("Claude Code (claude)" in line for line in lines),
          f"the runtime shows both: {lines}")
    check(any("Opus 5 (claude-opus-5)" in line for line in lines),
          f"and so does the model: {lines}")


def test_a_secret_is_named_and_never_shown() -> None:
    """"Secrets must never be listed, echoed, written into artifacts, or used
    as option labels"."""
    token = Field(name="token", kind=KIND_TEXT, title="Upstream token", secret=True)
    lines = review_lines(Schema((token,)), {"token": "s3cr3t"})
    check(lines == ["Upstream token: (not shown)"], f"the value never appears: {lines}")
    try:
        Field(name="token", kind=KIND_SINGLE, title="Upstream token", secret=True,
              choices=(Choice("s3cr3t"),))
        raise AssertionError("a secret was allowed to be a listed choice")
    except SchemaError as exc:
        check("cannot be a selectable choice" in str(exc), f"refused at declaration: {exc}")


def test_a_bool_reads_as_yes_or_no_rather_than_as_python() -> None:
    field = Field(name="designer", kind=KIND_BOOL, title="Include designer role", default=True)
    lines = review_lines(Schema((field,)), {"designer": True})
    check(lines == ["Include designer role: yes"], f"{lines}")


# --- a runtime switch is a dependent choice too ------------------------------
#
# The third place this choice is made. `switch_role_runtime` re-pointed the
# resume flags at the new runtime -- "resume semantics belong to the runtime,
# so a stale one left behind would try to resume the new CLI with the old CLI's
# flag" -- and left the MODEL exactly where it was. A role moved from Codex to
# Claude kept `gpt-5.5`, and the new runtime was started with the old one's
# model name.


def _one_role_config(tmp_path: Path, **role):
    import json
    from scripts import team_launcher

    (tmp_path / "layout.json").write_text(
        json.dumps({"Orientation": "Horizontal", "Widgets": []}), encoding="utf-8"
    )
    config_path = tmp_path / "demo.json"
    config_path.write_text(
        json.dumps(
            {
                "project": "demo",
                "layout": str(tmp_path / "layout.json"),
                "roles": [
                    {
                        "role": "app", "slot": 0, "workdir": str(tmp_path / "app"),
                        "target": "demo-app:0.0", "tmux_session": "demo-app", **role,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return team_launcher.load_project_config("demo", config_path), config_path


def test_a_model_from_the_old_runtime_does_not_survive_the_switch() -> None:
    import tempfile

    from scripts import role_runtime

    with tempfile.TemporaryDirectory(prefix="syrd115-switch.") as tmp:
        config, config_path = _one_role_config(
            Path(tmp), cli=["codex"], model="gpt-5.5", effort="high"
        )
        check(config.roles[0].model == "gpt-5.5", "the role starts on the old runtime's model")
        updated = role_runtime._write_runtime_projection(
            config, config_path=config_path, role_name="app", runtime="claude",
            runner=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0),
            model="claude-opus-5",
        )

    role = updated.roles[0]
    check(role.cli == ["claude"], f"the runtime changed: {role.cli}")
    check(role.model == "claude-opus-5",
          f"and the model is the new runtime's, not the old one's: {role.model}")


def test_a_runtime_that_takes_no_effort_does_not_inherit_one() -> None:
    import tempfile

    from scripts import role_runtime

    with tempfile.TemporaryDirectory(prefix="syrd115-effort.") as tmp:
        config, config_path = _one_role_config(
            Path(tmp), cli=["codex"], model="gpt-5.5", effort="high"
        )
        updated = role_runtime._write_runtime_projection(
            config, config_path=config_path, role_name="app", runtime="agy",
            runner=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0),
            model="",
        )

    role = updated.roles[0]
    check(role.effort == "",
          f"agy renders no effort level, so it does not keep one: {role.effort!r}")
    check(role.model == "", f"and the old runtime's model is gone: {role.model!r}")


def test_saying_nothing_about_the_model_leaves_it_alone() -> None:
    """`model=None` is not `model=""`: a caller with nothing to say changes nothing."""
    import tempfile

    from scripts import role_runtime

    with tempfile.TemporaryDirectory(prefix="syrd115-quiet.") as tmp:
        config, config_path = _one_role_config(
            Path(tmp), cli=["claude"], model="claude-opus-5", effort="high"
        )
        updated = role_runtime._write_runtime_projection(
            config, config_path=config_path, role_name="app", runtime="claude",
            runner=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0),
        )

    check(updated.roles[0].model == "claude-opus-5",
          f"the configured model survives: {updated.roles[0].model}")


def test_the_artifact_records_the_identifiers_and_the_catalog_they_came_from() -> None:
    """"Store exact stable identifiers in the generated project artifact"."""
    import tempfile

    from scripts import team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd115-artifact.") as tmp:
        tmp_path = Path(tmp)
        artifact_path = tmp_path / "demo.project.json"
        team_launcher._write_initial_switchyard_project_artifact(
            project_name="Demo", slug="demo", owner_user="demo-agent",
            project_dir=tmp_path, artifact_path=artifact_path,
            design_document=tmp_path / "design.md", owner_shell="fish",
            implementer_roles=("app",),
            role_clis=(("director", "claude"), ("app", "codex")),
            role_models={"director": "claude-opus-5", "app": "gpt-5.5"},
            role_efforts={"director": "high"},
        )
        written = json.loads(artifact_path.read_text(encoding="utf-8"))
        reloaded = team_launcher.load_project_design_artifact(
            artifact_path, expected_project="demo"
        )

    project = written["project"]
    check(project["role_models"] == {"director": "claude-opus-5", "app": "gpt-5.5"},
          f"the stored values are the identifiers: {project.get('role_models')}")
    check(project["role_efforts"] == {"director": "high"},
          f"and so are the effort levels: {project.get('role_efforts')}")
    check(project["catalog_version"] == catalog.CATALOG_VERSION,
          f"with the catalog they were read off: {project.get('catalog_version')}")
    check(dict(reloaded.role_models) == {"director": "claude-opus-5", "app": "gpt-5.5"},
          f"and it reads back: {reloaded.role_models}")


def test_an_artifact_written_before_these_fields_still_loads() -> None:
    """Checked-in artifacts stay reproducible; absent is not invalid."""
    import tempfile

    from scripts import team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd115-old-artifact.") as tmp:
        tmp_path = Path(tmp)
        artifact_path = tmp_path / "demo.project.json"
        team_launcher._write_initial_switchyard_project_artifact(
            project_name="Demo", slug="demo", owner_user="demo-agent",
            project_dir=tmp_path, artifact_path=artifact_path,
            design_document=tmp_path / "design.md", owner_shell="fish",
            implementer_roles=("app",),
            role_clis=(("director", "claude"), ("app", "codex")),
        )
        written = json.loads(artifact_path.read_text(encoding="utf-8"))
        reloaded = team_launcher.load_project_design_artifact(
            artifact_path, expected_project="demo"
        )

    check("role_models" not in written["project"],
          f"nothing is written when nothing was chosen: {sorted(written['project'])}")
    check(reloaded.role_models == () and reloaded.catalog_version == 0,
          "and an artifact without them loads as having chosen none")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"provisioning_selector_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
