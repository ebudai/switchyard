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
    """The wrapper as install-switchyard really writes it.

    Rendered by BASH, through the same unquoted heredoc the generator uses, not
    by substituting strings in Python. That difference is the point: an unquoted
    heredoc expands `$...` AND executes backticks, so a backtick in a comment
    silently deletes itself and everything up to its partner. A Python renderer
    that only swaps `\\$` for `$` shows text the installed file never contains,
    and would have passed the very defect this guards.
    """
    source = GENERATOR.read_text(encoding="utf-8")
    lines = source.splitlines()
    opening = next(i for i, line in enumerate(lines) if line.strip() == 'cat >"$tmp" <<EOF')
    closing = next(i for i in range(opening + 1, len(lines)) if lines[i].strip() == "EOF")
    body = "\n".join(lines[opening + 1 : closing])

    script = tmp / "render.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "quote() { printf '%q' \"$1\"; }\n"
        f'default_target={target}\n'
        'help_text="HELP"\n'
        'version_text="VERSION"\n'
        'recovery_command="RECOVERY"\n'
        f"cat <<EOF\n{body}\nEOF\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    done = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    # Keep the real functions, drop the real dispatch: the wrapper's own main
    # body would exec the stub target instead of reporting the routing decision.
    text = done.stdout
    marker = 'if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then'
    assert marker in text, "the rendered wrapper no longer has the dispatch this trims"
    rendered = tmp / "switchyard"
    rendered.write_text(
        text[: text.index(marker)]
        + '\nif switchyard_invocation_requires_root "$@"; then echo ROOT; else echo USER; fi\n',
        encoding="utf-8",
    )
    rendered.chmod(0o755)
    return rendered


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


def test_the_rendered_wrapper_says_what_the_generator_meant() -> None:
    """The generator writes this file through a heredoc it expands.

    A backtick in a comment or a string is command substitution there, so it and
    everything between it and its partner is executed and removed before the
    file is ever written. I shipped exactly that: `switchyard %s` vanished from
    the fallback diagnostic, leaving one %s and two arguments, so printf reused
    the format and printed the line twice with the wrong values -- and the
    comments explaining the fix lost their subjects.

    Checked on the RENDERED text, because the generator's source looks perfectly
    fine; only the output shows the loss.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rendered = _render_wrapper(root, _stub_target(root, "t", "echo no-root")).read_text(
            encoding="utf-8"
        )

    body_start = rendered.index("switchyard_target_invocation_requires_root() {")
    body = rendered[body_start:]
    assert "`" not in body, "a backtick survived into the rendered wrapper"

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("printf "):
            continue
        # The format is the first single-quoted argument on the line.
        first = stripped.index("'")
        last = stripped.index("'", first + 1)
        fmt = stripped[first + 1 : last]
        placeholders = fmt.count("%s")
        rest = stripped[last + 1 :].replace("\\", "").strip()
        arguments = len([piece for piece in rest.split('"') if piece.strip() and piece.strip() != ">&2"])
        assert placeholders == 0 or arguments == 0 or placeholders == arguments, (
            f"printf format has {placeholders} %s but {arguments} arguments; "
            f"printf will reuse the format and print more than once: {fmt!r}"
        )

    # And the diagnostic must still name the verb it could not classify.
    assert "could not classify" in body
    diagnostic = next(l for l in body.splitlines() if "could not classify" in l and "printf" in l)
    assert diagnostic.count("%s") == 2, (
        f"the fallback diagnostic lost a placeholder: {diagnostic.strip()!r}"
    )


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
