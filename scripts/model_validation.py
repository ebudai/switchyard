"""Checking that a role's configured model exists and can work, before a role runs on it.

This module covers:
- **The probe.** `validate_role_models` asks each role's CLI, in a scratch
  workspace as the project owner, to make one tool call against a named file
  (`MODEL_VALIDATION_PROMPT`). A model that cannot does not pass.
- **Failures.** They carry the evidence and a suggestion.
- **Unknown models.** `switchyard new` reports that models were not probed,
  and has the owner confirm and record models the runtime catalog does not
  know. A launch stops before running on an unconfirmed model.
- **Interactive fields.** Validation of the model and effort fields in role
  runtime plans.

Running the probes, and the CLI discovery and auth they depend on, stays in the
launcher. So does the `validate-models` verb: it is the one call site allowed to
turn validation on in the first-run auth phase, and
`launch_without_model_probes_test` holds it to that in `team_launcher.py`. This
module decides what to ask and how to read the answer.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-290). It imports
`YOLO_ARGS_BY_CLI` from `scripts/role_command.py`, which never imports this
module. `team_launcher` imports this module at its top and still exports every
name callers read there. This module never imports `team_launcher` at its top.
Launcher facilities (`_run_owner_cli_probe`, `_owner_catalog_args`,
`_role_cli_name`, `load_project_config`, `run_switchyard_launch_first_run_auth`,
...) are read from `scripts.team_launcher` when a function runs.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from scripts.role_command import YOLO_ARGS_BY_CLI
from scripts.ticket_board import runtime_catalog, terminal_select
from scripts.ticket_board.prompt_schema import KIND_SINGLE, Choice, Field, with_existing_value

if TYPE_CHECKING:
    from scripts.team_launcher import FirstRunAuthReport, ProjectConfig, RoleConfig


def _validate_model_identifier(value: str) -> str:
    """A model named by hand still has to be a model identifier.

    There is no catalog to check it against -- that is why it was typed -- so
    what can be checked is checked: something, on one line, with no spaces in
    it. "Custom" must not become the one entry that accepts anything (SYRD-115).
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("a model identifier cannot be empty")
    if len(cleaned.split()) > 1:
        raise ValueError(f"a model identifier is one word; {cleaned!r} is several")
    return cleaned


def _model_field(
    role: str,
    *,
    configured: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    owner_args: Sequence[str] = (),
    unverified_because: str = "",
    print_func: Callable[[str], None] = print,
) -> Field:
    """The models for whichever runtime was chosen a moment ago.

    A callable rather than a list, because this is the dependent half of the
    pair: choosing a different runtime has to produce a different catalog, not
    the previous runtime's models with a new label.
    """
    announced: set[str] = set()

    def choices(answers: Mapping[str, Any]) -> tuple[Choice, ...]:
        runtime = str(answers.get("runtime") or "")
        found = runtime_catalog.model_catalog(
            runtime, configured=configured, runner=runner, owner_args=owner_args,
            unverified_because=unverified_because,
        )
        if runtime not in announced:
            # Where the list came from, said once per runtime: a recorded
            # catalog and a live one are not equally trustworthy, and an
            # operator choosing from the first should know it (SYRD-115).
            announced.add(runtime)
            print_func(f"  ({found.note})")
        return with_existing_value(found.choices, configured)

    return Field(
        name="model",
        kind=KIND_SINGLE,
        title=f"{role} model",
        choices=choices,
        depends_on=("runtime",),
        default=configured or None,
        allow_custom=True,
        custom_title="A model not listed here",
        validate=_validate_model_identifier,
        required=False,
    )


def _effort_field(role: str, *, configured: str = "") -> Field:
    def choices(answers: Mapping[str, Any]) -> tuple[Choice, ...]:
        found = runtime_catalog.effort_catalog(str(answers.get("runtime") or ""))
        return with_existing_value(found.choices, configured)

    return Field(
        name="effort",
        kind=KIND_SINGLE,
        title=f"{role} effort",
        choices=choices,
        depends_on=("runtime",),
        default=configured or None,
        allow_custom=True,
        custom_title="An effort level not listed here",
        validate=_validate_model_identifier,
        required=False,
    )


@dataclass(frozen=True)
class ModelValidationFailure:
    role: str
    cli: str
    model: str
    reason: str
    suggestion: str
    #: One bounded, token-free line per probe attempt. Kept so an operator can
    #: tell a model that never tried to read the file from one whose read
    #: failed, from a provider error -- which the reason alone cannot do, and
    #: which is what sent a fresh Zorin run looking for a model change it may
    #: not have needed (SYRD-244). Defaulted so every existing construction of
    #: this record keeps working unchanged.
    evidence: tuple[str, ...] = ()


#: The file the probe leaves for the model to read, named so that a stale one
#: found in a temp directory says what made it.
MODEL_PROBE_FILENAME = "switchyard-model-probe.txt"


#: Every Switchyard role is a tool-calling agent, so the question the first-run
#: probe has to answer is whether the configured model can call a tool -- not
#: whether it can produce prose. The reply must carry a token that exists only
#: inside a file in the working directory, so a model that answers from the
#: prompt alone cannot produce it and a tool call that arrives as prose does not
#: count. `model-ok` stays: an exit 0 carrying no usable content is still a
#: failed completion (SYRD-111).
MODEL_VALIDATION_PROMPT = (
    f"Read the file {MODEL_PROBE_FILENAME} in the current directory and reply with exactly: "
    "model-ok <the token on its first line>"
)


#: What a probe says when the model answered but never read the file.
MODEL_PROBE_NO_TOOL_CALL_REASON = (
    "model answered but completed no tool call: the reply did not carry the token from "
    f"{MODEL_PROBE_FILENAME}"
)


#: How many times a model that ANSWERS but returns no token is asked again.
#:
#: One prose reply does not establish that a model cannot call tools. A model
#: that answers from the prompt on one turn and reads the file on the next is a
#: model that can call tools, and declaring otherwise sent an operator to change
#: a model that was fine (SYRD-244). Deliberately small: the probe is a live
#: model round trip on the critical path of `switchyard new`, so every extra
#: attempt is time a person spends watching a terminal.
#:
#: Only the ambiguous outcome is retried. A non-zero exit already carries the
#: vendor's own message -- an unauthenticated CLI or an unknown model name --
#: and asking again produces the same message more slowly.
MODEL_PROBE_TOOL_CALL_ATTEMPTS = 2


#: How much of one attempt's output is kept as evidence, per stream.
#:
#: Enough to tell the four outcomes apart -- no attempt, a failed read, a
#: provider error, a later success -- and short enough that a preflight warning
#: stays readable. The token is removed before anything is kept: it appears in
#: BOTH streams on a healthy codex run, which is exactly how a transcript of a
#: successful probe would otherwise carry the one value the probe's whole
#: meaning rests on being unguessable.
MODEL_PROBE_EVIDENCE_CHARS = 240


class _ModelProbeWorkspace:
    """A throwaway directory holding one token the model can only read.

    World-readable on purpose: the probe runs as the owner user through sudo and
    this process may be somebody else, and there is nothing to protect -- the
    token's whole value is being unguessable for the length of one probe, which
    is what makes echoing it proof that a tool ran rather than proof that a model
    can talk.
    """

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="switchyard-model-probe."))
        self.token = secrets.token_hex(8)
        os.chmod(self.root, 0o755)
        probe_file = self.root / MODEL_PROBE_FILENAME
        probe_file.write_text(f"{self.token}\n", encoding="utf-8")
        os.chmod(probe_file, 0o644)

    def remove(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _model_validation_command(role: RoleConfig, workspace: Path | None = None) -> list[str] | None:
    from scripts import team_launcher as launcher

    cli = launcher._role_cli_name(role)
    if not cli or not role.model:
        return None
    model_args = [role.model_arg, role.model] if role.model_arg else []
    # The flags a live pane uses for this CLI. A probe has nobody to approve a
    # tool call for it, so without these the reply is a permission prompt rather
    # than an answer -- and the question under test would go unasked (SYRD-111).
    tool_args = list(YOLO_ARGS_BY_CLI.get(cli, []))
    if cli == "codex":
        workspace_args = ["-C", str(workspace)] if workspace is not None else []
        return [
            *role.cli, "exec", "--skip-git-repo-check", *model_args, *tool_args,
            *workspace_args, MODEL_VALIDATION_PROMPT,
        ]
    if cli in {"agy", "claude"}:
        return [*role.cli, *model_args, *tool_args, "-p", MODEL_VALIDATION_PROMPT]
    if cli == "hermes":
        return [*role.cli, *model_args, *tool_args, "-z", MODEL_VALIDATION_PROMPT]
    return None


def _parse_agy_model_ids(stdout: str) -> list[str]:
    ids: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        model_id = stripped.split(maxsplit=1)[0]
        if model_id:
            ids.append(model_id)
    return list(dict.fromkeys(ids))


def _tool_call_failure_suggestion(cli: str, model: str) -> str:
    """What to change when the model talks but cannot act.

    Named separately from the model-list suggestion because the remedy is
    different in kind: no catalogue entry tells you whether a model can call a
    tool, so the thing to change is the model itself, not the spelling of it.
    """
    return (
        f"configure a {cli} model that supports tool calling for this role; {model} answered "
        "the prompt but never read the file it was asked to read"
    )


def _model_failure_suggestion(
    cli: str,
    *,
    owner_user: str,
    owner_home: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    from scripts import team_launcher as launcher

    if cli != "agy":
        return f"no model list is available for {cli}; validation used a one-shot prompt"
    proc = launcher._run_owner_cli_probe(
        owner_user=owner_user,
        owner_home=owner_home,
        command=["agy", "models"],
        runner=runner,
    )
    if proc.returncode != 0:
        return "agy exposes a model list, but `agy models` failed while building suggestions"
    ids = _parse_agy_model_ids(str(getattr(proc, "stdout", "") or ""))
    if not ids:
        return "agy exposes a model list, but `agy models` returned no ids"
    return f"valid agy models include: {', '.join(ids[:8])}"


def _model_validation_passed(proc: subprocess.CompletedProcess[Any]) -> bool:
    # Codex can echo the prompt text to stderr on model failures; the prompt
    # contains "model-ok", so stderr must never satisfy the sentinel check.
    return proc.returncode == 0 and "model-ok" in str(getattr(proc, "stdout", "") or "")


def _model_probe_called_a_tool(proc: subprocess.CompletedProcess[Any], token: str) -> bool:
    """Whether the reply carries something only a tool call could have fetched.

    Read from stdout for the same reason the sentinel is: the prompt names the
    file, so a CLI that echoes the prompt to stderr must not be able to satisfy
    this either. The token itself is never in the prompt.
    """
    if not token:
        return True
    return token in str(getattr(proc, "stdout", "") or "")


@dataclass(frozen=True)
class ModelProbeAttempt:
    """What one probe round trip showed, with nothing secret kept.

    The four outcomes SYRD-244 asks to be told apart are read off these:
    a token means a tool ran; a sentinel without a token means the model
    answered without reading; a non-zero exit carries the provider's own
    message; and a later attempt carrying the token means the earlier one was
    a transient reply rather than a limit of the model.
    """

    attempt: int
    exit_status: int
    answered: bool
    called_tool: bool
    evidence: str

    def describe(self) -> str:
        if self.called_tool:
            outcome = "read the file"
        elif self.answered:
            outcome = "answered without reading the file"
        else:
            outcome = "did not answer"
        detail = f": {self.evidence}" if self.evidence else ""
        return f"attempt {self.attempt} (exit {self.exit_status}) {outcome}{detail}"


def _model_probe_evidence(
    proc: subprocess.CompletedProcess[Any], token: str, *, limit: int = MODEL_PROBE_EVIDENCE_CHARS
) -> str:
    """One bounded, token-free line describing what the probe got back.

    The token is removed FIRST and from both streams. A healthy codex run
    echoes it to stdout and stderr alike, so evidence kept before redaction
    would publish the one value the probe depends on being unguessable -- into
    a warning an operator may well paste somewhere.

    stderr before stdout: when a probe fails, the reason is almost always
    there, while stdout is empty or a partial answer.
    """
    parts: list[str] = []
    for stream in ("stderr", "stdout"):
        raw = getattr(proc, stream, "") or ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = str(raw)
        if token:
            text = text.replace(token, "<token>")
        collapsed = " ".join(text.split())
        if collapsed:
            parts.append(f"{stream}: {collapsed}")
    joined = " | ".join(parts)
    if len(joined) > limit:
        joined = joined[: limit - 1].rstrip() + "\u2026"
    return joined


def _probe_role_model(
    *,
    command: Sequence[str],
    owner_user: str,
    owner_home: Path,
    workspace: "_ModelProbeWorkspace",
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    attempts: int = MODEL_PROBE_TOOL_CALL_ATTEMPTS,
) -> tuple[subprocess.CompletedProcess[Any], list[ModelProbeAttempt]]:
    """Ask until the model reads the file, or until the attempts run out.

    Returns the LAST process and every attempt's evidence. Only the ambiguous
    outcome -- answered, no token -- is retried; a non-zero exit is the
    provider telling us something deterministic, and repeating it just makes
    the operator wait twice for the same sentence.
    """
    from scripts import team_launcher as launcher

    history: list[ModelProbeAttempt] = []
    proc: subprocess.CompletedProcess[Any] | None = None
    for attempt in range(1, max(1, attempts) + 1):
        proc = launcher._run_owner_cli_probe(
            owner_user=owner_user,
            owner_home=owner_home,
            command=command,
            runner=runner,
            cwd=workspace.root,
        )
        answered = _model_validation_passed(proc)
        called_tool = answered and _model_probe_called_a_tool(proc, workspace.token)
        history.append(
            ModelProbeAttempt(
                attempt=attempt,
                exit_status=int(getattr(proc, "returncode", 1) or 0),
                answered=answered,
                called_tool=called_tool,
                evidence=_model_probe_evidence(proc, workspace.token),
            )
        )
        if called_tool or not answered:
            break
    assert proc is not None
    return proc, history


def validate_role_models(
    roles: Sequence[RoleConfig],
    *,
    owner_user: str,
    owner_home: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[ModelValidationFailure]:
    """Ask each role's model to prove it can work, and say so while doing it.

    This runs straight after the last folder-trust step, and it was the one
    part of the first-run phase that made no sound at all: stdin is /dev/null,
    stdout and stderr are captured, and nothing was printed before or between
    the probes. The setup window has already been handed back by then -- title
    "Switchyard" -- and the screen cleared, so a fresh tenant showed a cursor
    on an empty screen for as long as the probes took, with no way to tell a
    slow model from a stopped launcher. test15 and then test16 both stopped
    here, and both were read as a stall (SYRD-245).
    """
    from scripts import team_launcher as launcher

    failures: list[ModelValidationFailure] = []
    workspace = _ModelProbeWorkspace()
    try:
        for role in roles:
            cli = launcher._role_cli_name(role)
            command = _model_validation_command(role, workspace.root)
            if command is None:
                continue
            named_model = role.model or f"{cli}'s default model"
            print_func(
                f"switchyard: checking {role.role}'s model ({named_model}) with {cli}; "
                f"this asks it one question and waits up to "
                f"{launcher.OWNER_CLI_PROBE_TIMEOUT_SECONDS:g}s for the answer"
            )
            proc, history = _probe_role_model(
                command=command,
                owner_user=owner_user,
                owner_home=owner_home,
                workspace=workspace,
                runner=runner,
            )
            # A later attempt that reads the file settles it: the model can call
            # tools, and the earlier reply was a reply rather than a limit.
            if any(entry.called_tool for entry in history):
                continue
            tool_call_missing = history[-1].answered
            if tool_call_missing:
                # Distinct from an unauthenticated CLI, which fails non-zero
                # with the vendor's own message, and from an unknown model,
                # which fails the same way with a name in it. This one answered
                # -- every time it was asked.
                # Left exactly as SYRD-111 defined it. The string is the
                # identifier for this outcome and other code compares against
                # it; how many times the model was asked belongs in the
                # evidence below, which is printed with it and says "attempt 1"
                # and "attempt 2" in as many words.
                reason = MODEL_PROBE_NO_TOOL_CALL_REASON
                suggestion = _tool_call_failure_suggestion(cli, role.model)
            else:
                reason = launcher._proc_failure_reason(proc, f"model probe failed with exit {proc.returncode}")
                if proc.returncode == 0:
                    reason = "model probe did not confirm model-ok"
                suggestion = _model_failure_suggestion(
                    cli,
                    owner_user=owner_user,
                    owner_home=owner_home,
                    runner=runner,
                )
            failures.append(
                ModelValidationFailure(
                    role=role.role,
                    cli=cli,
                    model=role.model,
                    reason=reason,
                    suggestion=suggestion,
                    evidence=tuple(entry.describe() for entry in history),
                )
            )
    finally:
        workspace.remove()
    return failures


def report_models_were_not_probed(
    config: ProjectConfig, *, print_func: Callable[[str], None] = print
) -> None:
    """Say that nothing checked the models, because nothing did.

    A launch that quietly stopped probing would be a launch that silently
    claims less than it used to while looking the same. The ticket is explicit:
    do not claim model capability was verified when no probe ran. So this says
    the opposite out loud, once, and names the command that does ask.

    Said only when there is something it could have asked about. A tenant whose
    roles configure no model has nothing to report and no reason to be told
    about a diagnostic it does not need (SYRD-246).
    """
    from scripts import team_launcher as launcher

    configured = sorted(
        {
            f"{launcher._role_cli_name(role)} {role.model}".strip()
            for role in config.roles
            if str(getattr(role, "model", "") or "").strip()
        }
    )
    if not configured:
        return
    print_func(
        "switchyard: the configured models were NOT checked; nothing here asked them to "
        "prove anything. If one is wrong the provider says so in that role's own pane, in "
        f"its own words. To ask on purpose: `switchyard validate-models {config.project}`."
    )


def record_role_model(
    config: "ProjectConfig",
    *,
    config_path: Path,
    role_name: str,
    model: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> "ProjectConfig":
    """Write one role's model into the project config, and nothing else.

    Ownership is restored afterwards because this runs as root during
    `switchyard new`, and a tenant config the tenant cannot read is a worse
    outcome than the model it was repairing.
    """
    from scripts import team_launcher as launcher

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    roles = raw.get("roles")
    if not isinstance(roles, list):
        raise SystemExit(f"switchyard: {config_path} must define a roles list")
    for entry in roles:
        if isinstance(entry, dict) and entry.get("role") == role_name:
            entry.pop("model", None)
            if model:
                entry["model"] = model
            break
    else:
        raise SystemExit(f"switchyard: {config_path} has no role {role_name!r}")
    launcher._write_json_atomic(config_path, raw)
    launcher.ensure_owner_file(config, config_path, runner=runner)
    return launcher.load_project_config(config.project, config_path)


def confirm_unknown_models_with_owner(
    config: "ProjectConfig",
    report: FirstRunAuthReport,
    *,
    config_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    interactive: bool | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> tuple["ProjectConfig", FirstRunAuthReport]:
    """Ask the account that now exists, and record what it answers.

    This is the other half of the ordering problem. `switchyard new` must
    choose its roles' models before it creates anything -- the plan review is
    the last point at which nothing has been mutated -- so at that moment there
    is no owner account to enumerate and the recorded fallback is all there is.
    By the time this runs the account exists AND has authenticated, which is
    the first moment its catalog is a real answer.

    Refusing the launch here would be correct and useless: the operator chose
    from the only list available to them and would be told, after provisioning,
    that it was the wrong list. So the choice is offered again, against the
    real one. Nothing is substituted -- the configured value is shown and an
    answer is required (SYRD-250 DAT).

    Returns the config and report as they now stand. Roles still carrying a
    model the owner does not offer are left in `unknown_model_roles` for the
    launch gate, which is what happens when there is nobody to ask.
    """
    from scripts import team_launcher as launcher

    if not report.unknown_model_roles:
        return config, report
    can_ask = (sys.stdin.isatty() if interactive is None else interactive)
    if not can_ask:
        return config, report

    _owner_user, owner_args = launcher._owner_catalog_args(config)
    unresolved: list[tuple[str, str, str, tuple[str, ...]]] = []
    for role, cli, model, available in report.unknown_model_roles:
        print_func(
            f"switchyard: {role} was configured for {model!r} before "
            f"{report.owner_user or 'the project account'} existed, and that account "
            f"does not offer it. Choose from what it does offer:"
        )
        try:
            chosen = terminal_select.select_one(
                # Deliberately NOT `configured=model`. That would put the value
                # the account has just refused at the top of the list and make
                # it the default, so pressing Enter would keep the broken
                # model. It is named in the line above instead, and typing it
                # again is still possible through the custom path -- as a
                # deliberate act rather than the path of least resistance.
                _model_field(
                    role, runner=runner, owner_args=owner_args,
                    print_func=print_func,
                ),
                {"runtime": cli},
                input_func=input_func,
                print_func=print_func,
            )
        except terminal_select.Cancelled:
            # Somebody who cannot answer this is not somebody to crash a
            # half-provisioned project at. The role keeps its configured value
            # and the launch gate below says what to run to repair it.
            print_func(f"switchyard: {role}'s model was left as {model!r}.")
            unresolved.append((role, cli, model, available))
            continue
        config = record_role_model(
            config, config_path=config_path, role_name=role, model=chosen, runner=runner
        )
        still_absent = runtime_catalog.model_absent_from(
            runtime_catalog.owner_model_catalog(cli, runner=runner, owner_args=owner_args),
            chosen,
        )
        if still_absent is None:
            print_func(
                f"switchyard: {role} will run {chosen or cli + "'s own default"}."
            )
            continue
        # They were shown the account's list and typed something else. That is
        # a decision somebody made rather than a warning nobody read, which is
        # the whole complaint -- but it is still not a model this account
        # knows, so the launch gate below says so and stops.
        unresolved.append(
            (role, cli, chosen, tuple(c.value for c in still_absent.choices))
        )
    return config, replace(report, unknown_model_roles=unresolved)


def stop_before_launch_for_unknown_models(
    report: FirstRunAuthReport,
    *,
    project: str = "",
    print_func: Callable[[str], None] = print,
) -> bool:
    """A role configured for a model its own account does not offer is not started.

    The alternative is what `test2` did: the pane came up, the provider printed
    `model gemini-3.7-flash-high is not recognized ... Ignoring the flag`, and
    the role ran on something nobody chose. A warning inside a pane nobody is
    reading is not a decision anybody made.

    Nothing is substituted. The configured value is left exactly as it is and
    the operator is told what their own account offers instead, because picking
    a replacement here would be the silent rewrite this ticket forbids -- and
    the value may be right while the account is simply not set up yet.
    """
    if not report.unknown_model_roles:
        return False
    for role, cli, model, available in report.unknown_model_roles:
        offered = ", ".join(available) if available else "(its catalog is empty)"
        print_func(
            f"switchyard: not starting {role}: {cli} on "
            f"{report.owner_user or 'the project account'} does not offer "
            f"{model!r}, so the provider would ignore it and run something else. "
            f"That account offers: {offered}. Nothing has been changed for you."
        )
        # A remedy has to be a command that runs and fixes this. The first
        # version of this message named `switchyard set-role-runtime <role>`,
        # which omits the required project AND, even spelled correctly, could
        # not change a model while the runtime stayed the same -- it reported
        # "no change" and kept the bad value (SYRD-250 DAT).
        print_func(
            f"switchyard:   repair it with: switchyard set-role-runtime "
            f"{project or '<project>'} {role} --cli {cli} "
            f"--model {available[0] if available else '<model>'}"
        )
        print_func(
            f"switchyard:   or take {cli}'s own default with: switchyard set-role-runtime "
            f"{project or '<project>'} {role} --cli {cli} --model ''"
        )
    return True
