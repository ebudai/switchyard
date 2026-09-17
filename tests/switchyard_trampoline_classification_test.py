#!/usr/bin/env python3
"""The wrapper must not escalate a verb the tenant's control bridge serves.

`switchyard stop testing` suspended the live testing tenant, recorded no rollout
boundary, and left four presentation windows open -- while `switchyard start
testing` journalled every time. Identical routing in Python, opposite outcomes,
because the two verbs never reached Python by the same route.

/usr/local/bin/switchyard asks the installed target to classify the invocation
and falls back to a hardcoded verb list when it cannot parse the answer. `stop`
was on that list and `start` was not, so an unparseable answer sent `stop` to
`sudo` -- and root is neither the desktop account nor the control bridge, so it
suspends the tenant, journals nothing, and cannot close a window it does not own.

These render the real wrapper from the real generator and drive it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from standalone_test_runner import run_module_tests

GENERATOR = ROOT / "scripts" / "install-switchyard"


def _render_wrapper(tmp: Path, target: Path) -> Path:
    """The wrapper exactly as install-switchyard writes it, pointed at a stub."""
    source = GENERATOR.read_text(encoding="utf-8")
    start = source.index("switchyard_target_invocation_requires_root() {")
    end = source.index("switchyard_user_can_prompt_for_sudo() {")
    body = source[start:end]
    # The generator writes this inside a quoted heredoc, so `\$` in the source is
    # a literal `$` in the emitted file. Undo exactly that, and nothing else.
    body = body.replace("\\$", "$").replace("\\\\n", "\\n").replace("\\\\\n", "\\\n")
    script = tmp / "switchyard"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'readonly SWITCHYARD_DEFAULT_TARGET={target}\n'
        f"{body}\n"
        'if switchyard_invocation_requires_root "$@"; then echo ROOT; else echo USER; fi\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _stub_target(tmp: Path, name: str, body: str) -> Path:
    target = tmp / name
    target.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8")
    target.chmod(0o755)
    return target


def _route(wrapper: Path, *argv: str) -> tuple[str, str]:
    done = subprocess.run([str(wrapper), *argv], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip(), done.stderr


def test_a_classified_verb_is_routed_by_the_target_not_the_list() -> None:
    """When the target answers, its answer wins for every verb."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _stub_target(root, "target", 'echo no-root')
        wrapper = _render_wrapper(root, target)
        for verb in ("stop", "start", "status"):
            route, _ = _route(wrapper, verb, "testing")
            assert route == "USER", (verb, route)


def test_stray_output_before_the_answer_no_longer_forces_the_fallback() -> None:
    """The realistic failure: a notice on stdout ahead of the classification.

    This is what made the answer unparseable, and an unparseable answer is what
    sent `stop` to root.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _stub_target(root, "target", 'echo "switchyard: a notice"\necho no-root')
        wrapper = _render_wrapper(root, target)
        route, _ = _route(wrapper, "stop", "testing")
        assert route == "USER", route


def test_an_unclassifiable_stop_is_not_escalated_to_root() -> None:
    """The defect itself, at its worst: the target cannot answer at all.

    `stop` and `status` must still go to the target as this user, because the
    control bridge serves them without root. Root suspends the tenant, records
    nothing and cannot close the caller's window.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _stub_target(root, "target", "exit 3")
        wrapper = _render_wrapper(root, target)
        for verb in ("stop", "status"):
            route, complaint = _route(wrapper, verb, "testing")
            assert route == "USER", (verb, route)
            assert "could not classify" in complaint, (verb, complaint)


def test_a_genuinely_privileged_verb_still_escalates_when_unclassifiable() -> None:
    """The fallback is narrowed, not removed."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _stub_target(root, "target", "exit 3")
        wrapper = _render_wrapper(root, target)
        for verb in ("new", "register", "upgrade"):
            route, _ = _route(wrapper, verb, "thing")
            assert route == "ROOT", (verb, route)


def test_the_fallback_is_never_silent() -> None:
    """Silence here cost three rounds of diagnosis; it must announce itself."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _stub_target(root, "target", "exit 3")
        wrapper = _render_wrapper(root, target)
        _, complaint = _route(wrapper, "new", "thing")
        assert "falling back to the built-in verb list" in complaint, complaint


def test_the_targets_answer_is_actually_read() -> None:
    """The root cause, stated directly.

    `if cmd; then return 0; fi` followed by `case "$?"` reads the status of the
    `if` -- which is 0 when no branch ran -- not the status of `cmd`. So a
    `no-root` classification was indistinguishable from an unclassifiable one,
    the verb list underneath decided everything, and `stop` was on it while
    `start` was not.

    A verb NOT on the fallback list is the only way to see this from the
    outside: if the answer were still being discarded, an unclassifiable
    `upgrade` and a `no-root` `upgrade` would route identically.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        wrapper = _render_wrapper(root, _stub_target(root, "yes", "echo requires-root"))
        assert _route(wrapper, "upgrade", "thing")[0] == "ROOT"

        wrapper = _render_wrapper(root, _stub_target(root, "no", "echo no-root"))
        route, complaint = _route(wrapper, "upgrade", "thing")
        assert route == "USER", (
            "the target said no-root for a verb the fallback list calls privileged, "
            f"and the wrapper still routed {route}"
        )
        assert "could not classify" not in complaint, complaint


def test_stop_and_status_are_absent_from_the_generated_verb_list() -> None:
    """Asserted against the generator, because the list is the defect."""
    source = GENERATOR.read_text(encoding="utf-8")
    line = next(
        line for line in source.splitlines()
        if "new|register|upgrade|add-role" in line
    )
    for verb in ("stop", "status"):
        assert f"|{verb})" not in line and f"|{verb}|" not in line, (verb, line.strip())


if __name__ == "__main__":
    run_module_tests(globals())
    print("switchyard_trampoline_classification_test: ok")
