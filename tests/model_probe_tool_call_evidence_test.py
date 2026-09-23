#!/usr/bin/env python3
"""SYRD-244: one prose reply is not proof that a model cannot call tools.

A fresh Zorin run on `test14` stopped at preflight with

    model validation failed for role ops (cli codex, model gpt-5.6-sol): model
    answered but completed no tool call ... configure a codex model that
    supports tool calling for this role

from a SINGLE probe. The reported output cannot establish that verdict: a model
that answers from the prompt on one turn and reads the file on the next can call
tools, and the operator was sent to change a model that may have been fine.

What must not change is the gate. SYRD-111 requires a real tool call, and prose
never satisfies it. So the probe asks again rather than asking for less, and a
model that never reads the file still fails before any pane launches.

The other half is evidence. The old verdict was a fixed sentence with nothing
behind it, so no reader could tell these apart:

  * the model never tried to read the file,
  * it tried and the read failed,
  * the provider errored,
  * or it would have succeeded on the next attempt.

Each is now visible in a bounded, token-free line per attempt.

The probe's own mechanics were checked against the real `codex` CLI on this
host before any of this was written: the argv the code builds, run against a
tool-capable model, returns `model-ok <token>` on stdout. So the command,
workspace, `-C` flag and permission flags are not the fault -- which is what
made "transient or genuinely unable" the question worth answering.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import team_launcher as tl  # noqa: E402

CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def role(name: str = "ops", model: str = "gpt-5.6-sol"):
    return types.SimpleNamespace(role=name, cli=["codex"], model=model, model_arg="-m")


class Model:
    """A fake CLI that answers a scripted way, and records how often it was asked.

    `read` fetches the token out of the probe's own workspace, which is what a
    tool-capable model does and the only way to produce a value the prompt
    never contained. `prose` answers with the sentinel and no token -- the
    shape the Zorin run actually produced.
    """

    def __init__(self, *script: str) -> None:
        self.script = list(script)
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        kind = self.script.pop(0) if self.script else "prose"
        cwd = Path(kwargs.get("cwd") or ".")
        if kind == "read":
            token = (cwd / tl.MODEL_PROBE_FILENAME).read_text(encoding="utf-8").strip()
            # A real codex run echoes the answer to BOTH streams.
            return subprocess.CompletedProcess(
                args, 0, stdout=f"model-ok {token}\n", stderr=f"codex\nmodel-ok {token}\n"
            )
        if kind == "prose":
            return subprocess.CompletedProcess(
                args, 0, stdout="model-ok <the token on its first line>\n", stderr=""
            )
        if kind == "read-failed":
            return subprocess.CompletedProcess(
                args, 0,
                stdout="model-ok unknown\n",
                stderr=f"tool error: open {tl.MODEL_PROBE_FILENAME}: permission denied\n",
            )
        if kind == "provider-error":
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="stream error: 503 upstream unavailable\n"
            )
        if kind == "unauthenticated":
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="You are not signed in. Run `codex login`.\n"
            )
        raise AssertionError(f"unknown script step {kind!r}")


def validate(model: Model, roles=None):
    return tl.validate_role_models(
        roles or [role()], owner_user="owner", owner_home=Path("/tmp"), runner=model
    )


# -- the regression ----------------------------------------------------------


def test_a_single_prose_reply_no_longer_condemns_the_model() -> None:
    """The Zorin shape: answered without the token once, then read the file."""
    model = Model("prose", "read")
    failures = validate(model)
    check(failures == [], f"the model passes preflight: {[f.reason for f in failures]}")
    check(len(model.calls) == 2, f"because it was asked again: {len(model.calls)} attempts")


def test_a_model_that_never_reads_the_file_still_fails_before_panes_launch() -> None:
    """SYRD-111's gate, unweakened. Prose is not a tool call, however often."""
    model = Model("prose", "prose")
    failures = validate(model)
    check(len(failures) == 1, f"it fails: {failures}")
    failure = failures[0]
    check(
        tl.MODEL_PROBE_NO_TOOL_CALL_REASON in failure.reason,
        f"for the right reason: {failure.reason}",
    )
    check(
        failure.reason == tl.MODEL_PROBE_NO_TOOL_CALL_REASON,
        f"the reason string SYRD-111 defined is unchanged: {failure.reason!r}",
    )
    # That it was not a one-off is carried by the evidence, which is printed
    # with the reason -- rather than appended to a string other code compares
    # against.
    check(
        len(failure.evidence) == 2 and "attempt 2" in failure.evidence[1],
        f"and the second asking is on the record: {failure.evidence}",
    )
    check(
        "supports tool calling" in failure.suggestion,
        f"with the remedy that fits: {failure.suggestion}",
    )
    check(len(model.calls) == 2, f"{len(model.calls)}")


def test_the_retry_is_bounded_and_only_for_the_ambiguous_outcome() -> None:
    """A provider's own message is deterministic; repeating it just wastes time."""
    for script, expected_calls in (
        (("provider-error",), 1),
        (("unauthenticated",), 1),
        (("prose", "prose"), 2),
    ):
        model = Model(*script)
        validate(model)
        check(
            len(model.calls) == expected_calls,
            f"{script} asked {len(model.calls)} times, expected {expected_calls}",
        )
    check(
        tl.MODEL_PROBE_TOOL_CALL_ATTEMPTS == 2,
        f"and the bound is small: {tl.MODEL_PROBE_TOOL_CALL_ATTEMPTS}",
    )


# -- the four outcomes, told apart -------------------------------------------


def test_the_evidence_distinguishes_no_attempt_from_a_failed_read() -> None:
    never = validate(Model("prose", "prose"))[0]
    joined = " ".join(never.evidence)
    check("answered without reading the file" in joined, f"no attempt: {joined}")
    check(tl.MODEL_PROBE_FILENAME not in joined, f"and nothing about a read: {joined}")

    failed = validate(Model("read-failed", "read-failed"))[0]
    read_evidence = " ".join(failed.evidence)
    check(
        "permission denied" in read_evidence and tl.MODEL_PROBE_FILENAME in read_evidence,
        f"a failed read names the file and the error: {read_evidence}",
    )


def test_a_provider_error_keeps_the_providers_own_message() -> None:
    failure = validate(Model("provider-error"))[0]
    check("503 upstream unavailable" in failure.reason, f"{failure.reason}")
    check(
        tl.MODEL_PROBE_NO_TOOL_CALL_REASON not in failure.reason,
        "and is not reported as a missing tool call",
    )
    check(
        "did not answer" in " ".join(failure.evidence),
        f"the evidence says so too: {failure.evidence}",
    )


def test_every_attempt_is_recorded_not_just_the_last() -> None:
    failure = validate(Model("prose", "prose"))[0]
    check(len(failure.evidence) == 2, f"one line per attempt: {failure.evidence}")
    check(
        failure.evidence[0].startswith("attempt 1")
        and failure.evidence[1].startswith("attempt 2"),
        f"numbered in order: {failure.evidence}",
    )


# -- nothing secret survives -------------------------------------------------


def test_the_probe_token_never_reaches_the_evidence() -> None:
    """A healthy codex run echoes the token to BOTH streams.

    Evidence kept before redaction would publish the one value the probe's
    meaning rests on being unguessable -- into a warning an operator may paste
    into a ticket.
    """
    token = "cafebabedeadbeef"
    proc = subprocess.CompletedProcess(
        ["codex"], 0,
        stdout=f"model-ok {token}\n",
        stderr=f"codex\nmodel-ok {token}\nhook: Stop\n",
    )
    evidence = tl._model_probe_evidence(proc, token)
    check(token not in evidence, f"the token is gone: {evidence}")
    check("<token>" in evidence, f"and its place is marked: {evidence}")
    check("hook: Stop" in evidence, f"while the rest survives: {evidence}")


def test_the_evidence_is_bounded() -> None:
    proc = subprocess.CompletedProcess(["codex"], 1, stdout="", stderr="x" * 5000)
    evidence = tl._model_probe_evidence(proc, "tok")
    check(
        len(evidence) <= tl.MODEL_PROBE_EVIDENCE_CHARS,
        f"a warning stays readable: {len(evidence)} chars",
    )
    check(evidence.endswith("…"), f"and says it was cut: {evidence[-20:]}")


def test_the_warning_carries_the_evidence_to_the_operator() -> None:
    """Evidence nobody prints is evidence nobody has."""
    failure = tl.ModelValidationFailure(
        role="ops", cli="codex", model="gpt-5.6-sol",
        reason="model answered but completed no tool call (asked 2 times)",
        suggestion="configure a codex model that supports tool calling for this role",
        evidence=("attempt 1 (exit 0) answered without reading the file: stdout: model-ok x",
                  "attempt 2 (exit 0) answered without reading the file: stdout: model-ok y"),
    )
    # A real report, not a namespace with just enough attributes: this function
    # reads a dozen fields and the one that matters is easy to satisfy by
    # accident.
    report = tl.FirstRunAuthReport(
        unauthenticated_roles={},
        untrusted_roles=[],
        model_validation_failures=[failure],
        owner_user="owner",
    )
    printed: list[str] = []
    tl.report_first_run_auth_warnings(report, print_func=printed.append)
    body = "\n".join(printed)
    check("model validation failed for role ops" in body, body)
    check("probe attempt 1" in body, f"the first attempt is shown: {body}")
    check("probe attempt 2" in body, f"and the second: {body}")


def test_a_failure_record_without_evidence_still_works() -> None:
    """Every other construction of this record is untouched."""
    failure = tl.ModelValidationFailure(
        role="main", cli="claude", model="x", reason="r", suggestion="s"
    )
    check(failure.evidence == (), f"defaulted, not required: {failure.evidence!r}")


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("model_probe_tool_call_evidence_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(120)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"model_probe_tool_call_evidence_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
