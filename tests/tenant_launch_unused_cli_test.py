#!/usr/bin/env python3
"""SYRD-220: a launch asks about the CLIs its tenant uses, and no others.

On the preserved Zorin `test` tenant every configured role uses Claude or
Codex, and every `switchyard test` asked:

    switchyard: hermes is installed at /home/eric/.local/bin/hermes, which only
    eric can reach...
    Promote hermes host-wide? [p/s] (p):

Hermes is not required by any role there. `p` attempted a promotion and failed
on a mode-0775 source; `s` launched, and the question came back on the next
invocation, and the one after that.

SYRD-211 answered a question it could answer rather than the one that mattered:
the operator's private CLIs are visible out here, and the tenant's selection was
not, so it offered every caller-only CLI on the host. What was missing was not
the boundary -- the boundary is right, and the tenant's configuration under the
owner's home stays unreadable from here -- but a record of the tenant's CLI
selection on the side of it an operator can read.

The registry entry is that record: root-owned, world-readable, one per project,
and already read before the bridge is crossed. It now carries the CLIs the
tenant's roles are configured with, and the launch offers a promotion only for
those.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher  # noqa: E402

CHECKS = 0


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


def _which(*, private: set[str] = frozenset(), host_wide: set[str] = frozenset()):
    """The operator's context: some CLIs private, some installed for everyone."""

    def which(binary, path=None):
        if path == launcher.DEFAULT_PANE_BASE_PATH:
            return f"/usr/local/bin/{binary}" if binary in host_wide else None
        if binary in host_wide:
            return f"/usr/local/bin/{binary}"
        if binary in private:
            return f"/home/eric/.local/bin/{binary}"
        return None

    return which


def _registry(tmp_path: Path, *, slug: str = "test", clis: list[str] | None = None) -> Path:
    """A registry entry as root writes it, with or without the CLI record."""
    registry_dir = tmp_path / "projects"
    registry_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": launcher.SWITCHYARD_REGISTRY_SCHEMA,
        "slug": slug,
        "name": slug,
        "config_path": f"/home/{slug}-agent/Projects/{slug}/.switchyard/provision/{slug}.json",
    }
    if clis is not None:
        payload["agent_clis"] = clis
    (registry_dir / f"{slug}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return registry_dir


class _Refused:
    """A promoter that must never be reached, and says so if it is."""

    def __call__(self, cli, source, **_kwargs):
        raise AssertionError(f"promoted {cli} from {source} for a tenant that does not use it")


def _launch_offer(registry_dir: Path, *, private: set[str], host_wide: set[str] = frozenset(),
                  answer: str = "s", promoter=None):
    said: list[str] = []
    asked: list[str] = []

    def input_func(prompt: str) -> str:
        asked.append(prompt)
        return answer

    promoted = launcher.offer_host_wide_promotion_before_launch(
        "test",
        interactive=True,
        which=_which(private=private, host_wide=host_wide),
        input_func=input_func,
        print_func=said.append,
        promoter=promoter or _Refused(),
        registry_dir=registry_dir,
    )
    return promoted, asked, said


def test_an_unused_caller_only_cli_is_not_offered_at_all() -> None:
    """The report: hermes, on a tenant of Claude and Codex roles."""
    with tempfile.TemporaryDirectory(prefix="syrd220-unused.") as tmp:
        registry_dir = _registry(Path(tmp), clis=["claude", "codex"])
        promoted, asked, said = _launch_offer(
            registry_dir, private={"hermes"}, host_wide={"claude", "codex"}
        )

    check(asked == [], f"nothing was asked about a CLI no role uses: {asked}")
    check(promoted == [], f"and nothing was promoted: {promoted}")
    check(not any("hermes" in line for line in said),
          f"and hermes is not named at all: {said}")


def test_a_tenant_that_uses_the_cli_is_still_offered_the_promotion() -> None:
    """The other half: SYRD-211's offer survives where it belongs."""
    calls: list[tuple[str, str]] = []

    def promoter(cli, source, **_kwargs):
        calls.append((cli, source))
        return launcher.AgentCliAvailability(
            cli=cli, scope=launcher.AGENT_CLI_SCOPE_HOST_WIDE,
            host_wide_path=f"/usr/local/bin/{cli}",
        )

    with tempfile.TemporaryDirectory(prefix="syrd220-used.") as tmp:
        registry_dir = _registry(Path(tmp), clis=["claude", "hermes"])
        promoted, asked, said = _launch_offer(
            registry_dir, private={"hermes"}, host_wide={"claude"},
            answer="p", promoter=promoter,
        )

    check(len(asked) == 1, f"the tenant that uses hermes is asked once: {asked}")
    check("hermes" in asked[0], f"and asked about hermes: {asked}")
    check(calls == [("hermes", "/home/eric/.local/bin/hermes")],
          f"and accepting promotes the operator's own copy: {calls}")
    check(promoted == ["hermes"], f"and it is reported as promoted: {promoted}")
    del said


def test_a_mixed_tenant_is_asked_only_about_the_clis_it_selected() -> None:
    """Two private CLIs, one of them this tenant's."""
    with tempfile.TemporaryDirectory(prefix="syrd220-mixed.") as tmp:
        registry_dir = _registry(Path(tmp), clis=["claude", "codex"])
        promoted, asked, said = _launch_offer(
            registry_dir, private={"codex", "hermes"}, host_wide={"claude"}
        )

    check(len(asked) == 1, f"one question, for the one CLI that is this tenant's: {asked}")
    check("codex" in asked[0], f"and it is the selected one: {asked}")
    check(not any("hermes" in line for line in said),
          f"the unselected private CLI is never mentioned: {said}")
    check(promoted == [], f"declining promotes nothing: {promoted}")


def test_skipping_is_not_asked_again_on_the_next_launch() -> None:
    """The complaint that made it a regression: it came back every time.

    An unselected CLI is not asked about once, so there is nothing to come
    back. This drives the launch twice over the same registry to say that in
    the terms the report used.
    """
    with tempfile.TemporaryDirectory(prefix="syrd220-again.") as tmp:
        registry_dir = _registry(Path(tmp), clis=["claude", "codex"])
        first = _launch_offer(registry_dir, private={"hermes"}, host_wide={"claude", "codex"})
        second = _launch_offer(registry_dir, private={"hermes"}, host_wide={"claude", "codex"})

    check(first[1] == [] and second[1] == [],
          f"neither launch asked: {first[1]} then {second[1]}")
    check(first[2] == second[2],
          "the second launch says exactly what the first did, which is nothing about hermes")


def test_a_tenant_with_no_recorded_selection_is_not_asked_either() -> None:
    """Registered before this record existed: silence, and a way to end it.

    Nothing out here can say which CLIs such a tenant uses, and guessing "all
    of them" is the reported defect. The launch is left unchanged and the
    operator is told, once per launch, what would record it -- a line, not a
    question, and not a decision anybody has to make to get their tenant
    started.
    """
    with tempfile.TemporaryDirectory(prefix="syrd220-legacy.") as tmp:
        registry_dir = _registry(Path(tmp), clis=None)
        promoted, asked, said = _launch_offer(
            registry_dir, private={"hermes"}, host_wide={"claude", "codex"}
        )

    check(asked == [], f"an unrecorded tenant is not asked to guess: {asked}")
    check(promoted == [], f"and nothing is promoted: {promoted}")
    check(any("upgrade" in line and "test" in line for line in said),
          f"and the operator is told how to record the selection: {said}")


def test_an_unattended_launch_is_silent_about_an_unused_cli() -> None:
    """The non-interactive path had the same defect and the same fix."""
    said: list[str] = []
    with tempfile.TemporaryDirectory(prefix="syrd220-unattended.") as tmp:
        registry_dir = _registry(Path(tmp), clis=["claude", "codex"])
        promoted = launcher.offer_host_wide_promotion_before_launch(
            "test",
            interactive=False,
            which=_which(private={"hermes"}, host_wide={"claude", "codex"}),
            print_func=said.append,
            promoter=_Refused(),
            registry_dir=registry_dir,
        )

    check(promoted == [], f"nothing was promoted unattended: {promoted}")
    check(not any("hermes" in line for line in said),
          f"and an unused CLI is not even reported: {said}")


# --- the other side of it: how the record comes to say anything ------------
#
# A launch can only read what registration wrote, and every tenant registered
# before this record existed has one that says nothing. Registration writes it
# from the configuration it is already loading; `switchyard upgrade` is what
# gives an existing tenant the same record, because a launch runs unprivileged
# and this file is root's.


def _config_with(tmp_path: Path, roles: list[tuple[str, str]]):
    sys.path.insert(0, str(ROOT / "tests"))
    from team_launcher_test_helpers import (  # noqa: E402
        _write_first_run_auth_config,
        load_project_config,
    )
    # The shared helper writes this project under its own slug; what matters
    # here is the roles it carries, so the slug follows it.
    config_path = _write_first_run_auth_config(tmp_path, roles=roles)
    return load_project_config("otto", config_path), config_path


def test_registration_records_the_clis_the_roles_are_configured_with() -> None:
    """Written where it can be read from: root's own file, world-readable."""
    with tempfile.TemporaryDirectory(prefix="syrd220-register.") as tmp:
        tmp_path = Path(tmp)
        _config, config_path = _config_with(
            tmp_path, [("director", "claude"), ("main", "claude"), ("ops", "codex")]
        )
        registry_dir = tmp_path / "projects"
        registry_dir.mkdir()
        registry_path = launcher._register_switchyard_project(
            config_path, config_dir=tmp_path / "config", registry_dir=registry_dir
        )
        recorded = json.loads(registry_path.read_text(encoding="utf-8"))
        selection = launcher.registered_tenant_agent_clis("otto", registry_dir=registry_dir)
        mode = registry_path.stat().st_mode & 0o777

    check(recorded[launcher.SWITCHYARD_REGISTRY_AGENT_CLIS_KEY] == ["claude", "codex"],
          f"one entry per distinct CLI, not one per role: {recorded}")
    check(selection == frozenset({"claude", "codex"}),
          f"and the launch reads back what was written: {selection}")
    check(mode == 0o644,
          f"and the record stays readable by the operator who launches: {mode:o}")


def test_an_upgrade_records_the_selection_for_a_tenant_that_predates_it() -> None:
    """What the launch's message promises, and the only moment it can happen."""
    with tempfile.TemporaryDirectory(prefix="syrd220-upgrade.") as tmp:
        tmp_path = Path(tmp)
        config, _config_path = _config_with(
            tmp_path, [("director", "claude"), ("ops", "codex")]
        )
        registry_dir = _registry(tmp_path, slug="otto", clis=None)
        before = launcher.registered_tenant_agent_clis("otto", registry_dir=registry_dir)
        said: list[str] = []
        problems = launcher.refresh_registered_agent_clis(
            config, registry_dir=registry_dir, print_func=said.append
        )
        after = launcher.registered_tenant_agent_clis("otto", registry_dir=registry_dir)
        entry = json.loads((registry_dir / "otto.json").read_text(encoding="utf-8"))
        # Idempotent: a second pass has nothing to say and nothing to write.
        again: list[str] = []
        problems += launcher.refresh_registered_agent_clis(
            config, registry_dir=registry_dir, print_func=again.append
        )

    check(before is None, "the tenant started with no recorded selection")
    check(after == frozenset({"claude", "codex"}), f"and the upgrade records it: {after}")
    check(problems == [], f"with nothing to report as a problem: {problems}")
    check(said and "otto" in said[0], f"and it says what it recorded: {said}")
    check(again == [], f"a second upgrade rewrites nothing: {again}")
    check(entry["config_path"], "and every other field of the record survives it")


def test_a_record_that_is_not_a_registry_document_is_left_alone() -> None:
    """Repairing a stale record is not this command's decision."""
    with tempfile.TemporaryDirectory(prefix="syrd220-foreign.") as tmp:
        tmp_path = Path(tmp)
        config, _config_path = _config_with(tmp_path, [("director", "claude")])
        registry_dir = tmp_path / "projects"
        registry_dir.mkdir()
        foreign = registry_dir / "otto.json"
        foreign.write_text('{"schema": "something.else.v1"}\n', encoding="utf-8")
        problems = launcher.refresh_registered_agent_clis(
            config, registry_dir=registry_dir, print_func=lambda _line: None
        )
        unchanged = foreign.read_text(encoding="utf-8")

    check(problems and "not a registry record" in problems[0],
          f"the refusal says what it found: {problems}")
    check(unchanged == '{"schema": "something.else.v1"}\n',
          f"and the file it did not understand is untouched: {unchanged!r}")


def test_a_dry_run_upgrade_writes_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd220-dry.") as tmp:
        tmp_path = Path(tmp)
        config, _config_path = _config_with(tmp_path, [("director", "claude")])
        registry_dir = _registry(tmp_path, slug="otto", clis=None)
        said: list[str] = []
        launcher.refresh_registered_agent_clis(
            config, registry_dir=registry_dir, dry_run=True, print_func=said.append
        )
        after = launcher.registered_tenant_agent_clis("otto", registry_dir=registry_dir)

    check(after is None, "a dry run left the record exactly as it was")
    check(any("would record" in line for line in said),
          f"and said what it would have done: {said}")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"tenant_launch_unused_cli_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
