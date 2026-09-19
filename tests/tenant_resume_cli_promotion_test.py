#!/usr/bin/env python3
"""SYRD-211: a RESUMED tenant gets SYRD-210's promotion offer too.

Live Zorin UAT crossed the repaired tenant-control helper and then printed the
advice SYRD-210 had already abolished:

    switchyard: missing CLI claude ... install ... for owner user test-agent
    switchyard: these commands are yours to run; switchyard does not install agent CLIs.

and launched no panes. `require_agent_clis_for_new_tenant` is reached only from
`switchyard new`, before an owner exists. A registered tenant crosses the
tenant-control boundary first, and past it the launcher runs as the owner with a
built PATH: the operator's own claude and codex are invisible there, and root
cannot be asked to promote something nobody can still see.

So the offer has to happen on the unprivileged outer side, before the bridge:

* the operator's caller-local executable is the default, named in full;
* promotion goes through the same narrow privileged path SYRD-210 uses -- a
  root-owned host-wide copy, no credentials, config, trust or session with it;
* declining leaves the host unchanged AND still launches. Nothing is being
  created here, so refusing must not strand a tenant that already exists;
* nothing is offered for a CLI that is already host-wide.

What cannot be asked from out here is which CLIs this tenant actually selected:
that lives in its configuration under the owner's home, which this account
cannot read, and that boundary is working as intended. So the question asked is
the one that can be answered -- which agent CLIs exist for this operator alone.
"""

from __future__ import annotations

import subprocess
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


class _Promoter:
    def __init__(self, *, succeed: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.succeed = succeed

    def __call__(self, cli, source, **_kwargs):
        self.calls.append((cli, source))
        scope = (
            launcher.AGENT_CLI_SCOPE_HOST_WIDE if self.succeed
            else launcher.AGENT_CLI_SCOPE_CALLER_ONLY
        )
        return launcher.AgentCliAvailability(
            cli=cli, scope=scope,
            host_wide_path=f"/usr/local/bin/{cli}" if self.succeed else "",
            caller_path="" if self.succeed else source,
        )


def test_only_caller_only_clis_are_offered() -> None:
    offers = launcher.resolvable_agent_cli_promotions(
        which=_which(private={"claude", "codex"}, host_wide={"agy"})
    )
    names = [verdict.cli for verdict in offers]
    check("claude" in names and "codex" in names, f"the private ones are offered: {names}")
    check("agy" not in names, f"an already host-wide CLI has nothing to fix: {names}")
    check(all(verdict.caller_path for verdict in offers),
          "and each offer carries the path it would promote")


def test_nothing_is_offered_when_everything_is_already_host_wide() -> None:
    offers = launcher.resolvable_agent_cli_promotions(
        which=_which(host_wide={"claude", "codex", "agy", "hermes"})
    )
    check(offers == [], f"a fully provisioned host is asked nothing: {offers}")


def test_the_operators_own_executable_is_the_default() -> None:
    """One keystroke, and it is the path already found -- not a hunt."""
    promoter = _Promoter()
    said: list[str] = []
    promoted = launcher.offer_host_wide_promotion_before_launch(
        "test", interactive=True, which=_which(private={"claude", "codex"}),
        input_func=lambda _p: "", print_func=said.append, promoter=promoter,
    )
    check(promoted == ["claude", "codex"], f"both were promoted: {promoted}")
    check(promoter.calls == [
        ("claude", "/home/eric/.local/bin/claude"),
        ("codex", "/home/eric/.local/bin/codex"),
    ], f"from the operator's own copies: {promoter.calls}")
    text = "\n".join(said)
    check("/home/eric/.local/bin/claude" in text, f"the path is named in full: {text[:120]}")
    check("no credentials, config or session travel with it" in text,
          "and the offer states what is NOT carried across")
    check("curl" not in text, f"no vendor installer is printed: {text}")


def test_declining_leaves_the_host_unchanged_and_still_launches() -> None:
    """A resume is not `new`: refusing must not strand a tenant that exists."""
    promoter = _Promoter()
    said: list[str] = []
    promoted = launcher.offer_host_wide_promotion_before_launch(
        "test", interactive=True, which=_which(private={"claude"}),
        input_func=lambda _p: "s", print_func=said.append, promoter=promoter,
    )
    check(promoted == [], "nothing was promoted")
    check(promoter.calls == [], f"and nothing privileged ran: {promoter.calls}")
    text = "\n".join(said)
    check("leave this host unchanged" in text, "which the offer promised")
    # `s` is one of the two choices the offer printed. Telling an operator who
    # typed a documented answer that it is not a choice would be its own bug,
    # and it is what distinguishes declining from a typo.
    check("is not one of the choices" not in text,
          f"declining is a supported answer, not a mistake: {text}")


def test_an_unrecognised_answer_skips_rather_than_guessing() -> None:
    promoter = _Promoter()
    said: list[str] = []
    promoted = launcher.offer_host_wide_promotion_before_launch(
        "test", interactive=True, which=_which(private={"claude"}),
        input_func=lambda _p: "yes please", print_func=said.append, promoter=promoter,
    )
    check(promoted == [], "an answer that is not a choice promotes nothing")
    check(promoter.calls == [], f"and nothing privileged ran: {promoter.calls}")
    check("is not one of the choices" in "\n".join(said), "and it says so")


def test_an_unattended_resume_never_prompts_and_never_guesses() -> None:
    def never(_prompt: str) -> str:
        raise AssertionError("an unattended resume must not reach a prompt")

    promoter = _Promoter()
    said: list[str] = []
    promoted = launcher.offer_host_wide_promotion_before_launch(
        "test", interactive=False, which=_which(private={"claude"}),
        input_func=never, print_func=said.append, promoter=promoter,
    )
    check(promoted == [], "nothing is promoted without a declared decision")
    check(promoter.calls == [], "and nothing privileged runs")
    text = "\n".join(said)
    check("--agent-cli-policy promote-local" in text, f"the flag is named: {text}")
    check("--agent-cli-source claude=/home/eric/.local/bin/claude" in text,
          f"with the source it already found: {text}")


def test_an_unattended_resume_promotes_when_the_decision_is_declared() -> None:
    promoter = _Promoter()
    promoted = launcher.offer_host_wide_promotion_before_launch(
        "test", interactive=False, policy=launcher.AGENT_CLI_POLICY_PROMOTE_LOCAL,
        which=_which(private={"claude"}), print_func=lambda _l: None, promoter=promoter,
    )
    check(promoted == ["claude"], f"the declared policy is honoured: {promoted}")
    check(promoter.calls == [("claude", "/home/eric/.local/bin/claude")],
          f"using the caller's own copy: {promoter.calls}")


def test_a_declared_source_overrides_the_discovered_one() -> None:
    promoter = _Promoter()
    launcher.offer_host_wide_promotion_before_launch(
        "test", interactive=False, sources={"claude": "/opt/vendor/claude"},
        which=_which(private={"claude"}), print_func=lambda _l: None, promoter=promoter,
    )
    check(promoter.calls == [("claude", "/opt/vendor/claude")],
          f"the operator's explicit artifact wins: {promoter.calls}")


def test_a_promotion_that_did_not_take_is_not_reported_as_done() -> None:
    promoter = _Promoter(succeed=False)
    promoted = launcher.offer_host_wide_promotion_before_launch(
        "test", interactive=True, which=_which(private={"claude"}),
        input_func=lambda _p: "p", print_func=lambda _l: None, promoter=promoter,
    )
    check(promoter.calls, "the promotion was attempted")
    check(promoted == [], f"but a CLI that is still caller-only is not claimed: {promoted}")


def test_the_offer_is_made_before_the_bridge_is_crossed() -> None:
    """The whole point of the fix: past the bridge these paths are invisible."""
    order: list[str] = []
    with tempfile.TemporaryDirectory(prefix="syrd211-order.") as tmp:
        root = Path(tmp)
        (root / "test").mkdir()
        helper = root / "test" / "switchyard-tenant-control"
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        helper.chmod(0o755)

        def promoter(cli, source, **_kwargs):
            order.append(f"promote:{cli}")
            return launcher.AgentCliAvailability(
                cli=cli, scope=launcher.AGENT_CLI_SCOPE_HOST_WIDE,
                host_wide_path=f"/usr/local/bin/{cli}",
            )

        def runner(args, **_kwargs):
            if "switchyard-tenant-control" in " ".join(args):
                order.append("cross-bridge")
            return subprocess.CompletedProcess(args, 0)

        original_grant = launcher._tenant_control_grant
        original_complete = launcher.complete_desktop_presentation
        launcher._tenant_control_grant = lambda _p, **_k: {
            "project": "test", "authorized_user": launcher.current_user_name()
        }
        launcher.complete_desktop_presentation = lambda *_a, **_k: 0
        original_root = launcher.TENANT_CONTROL_ROOT
        launcher.TENANT_CONTROL_ROOT = root
        try:
            launcher._switchyard_cross_account(
                "test", ["test"],
                interactive=True, which=_which(private={"claude"}),
                input_func=lambda _p: "p", print_func=lambda _l: None,
                promoter=promoter, runner=runner,
                # A sandbox cannot create root-owned files; the staged-helper
                # verification has its own suite.
                ensure_helper=lambda *_a, **_k: None,
            )
        except SystemExit:
            pass
        finally:
            launcher._tenant_control_grant = original_grant
            launcher.complete_desktop_presentation = original_complete
            launcher.TENANT_CONTROL_ROOT = original_root
    check("promote:claude" in order, f"the promotion happened: {order}")
    check("cross-bridge" in order, f"and the launch still crossed: {order}")
    check(order.index("promote:claude") < order.index("cross-bridge"),
          f"promotion first, then the bridge: {order}")


def test_stop_and_status_are_not_an_installation_decision() -> None:
    """Only a start launches panes; the others must not ask about CLIs."""
    for operation, argv in (("stop", ["stop", "test"]), ("status", ["status", "test"])):
        asked: list[str] = []
        original_grant = launcher._tenant_control_grant
        original_bridge = launcher._switchyard_exec_through_tenant_control
        launcher._tenant_control_grant = lambda _p, **_k: {
            "project": "test", "authorized_user": launcher.current_user_name()
        }
        launcher._switchyard_exec_through_tenant_control = (
            lambda *_a, **_k: (_ for _ in ()).throw(SystemExit(0))
        )
        try:
            launcher._switchyard_cross_account(
                "test", argv, interactive=True, which=_which(private={"claude"}),
                input_func=lambda _p: asked.append("prompted") or "s",
                print_func=lambda _l: asked.append("printed"),
                promoter=lambda *_a, **_k: (_ for _ in ()).throw(
                    AssertionError("promoted on a non-start verb")
                ),
            )
        except SystemExit:
            pass
        finally:
            launcher._tenant_control_grant = original_grant
            launcher._switchyard_exec_through_tenant_control = original_bridge
        check(asked == [], f"{operation} asks nothing about CLIs: {asked}")


def test_the_remedy_is_host_wide_once_not_once_per_owner() -> None:
    """The exact text live UAT was given after SYRD-210 had landed."""
    reminder = launcher._owner_user_cli_reminder("test-agent")
    check("install each one for owner user" not in reminder,
          f"the superseded per-owner instruction is gone: {reminder}")
    check("host-wide" in reminder, f"and the remedy is host-wide: {reminder}")
    check("promote" in reminder, f"naming the promotion that does it: {reminder}")
    check("test-agent" in reminder, "while still saying which account the panes run as")
    # The unnamed-owner form has to carry the same remedy.
    generic = launcher._owner_user_cli_reminder("")
    check("host-wide" in generic, f"including when the owner is not known: {generic}")


def main() -> int:
    failures = 0
    for name, value in sorted(globals().items()):
        if not (name.startswith("test_") and callable(value)):
            continue
        try:
            value()
        except BaseException as exc:  # noqa: BLE001 - name what escaped
            failures += 1
            print(f"FAILED {name}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"tenant_resume_cli_promotion_test: {failures} failed")
        return 1
    print(f"tenant_resume_cli_promotion_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
