#!/usr/bin/env python3
"""SYRD-143: provisioning writes the desktop policy nobody should have to author.

`switchyard new` used to ask "Desktop policy file, or 'headless' for no
clipboard:". A desktop user cannot answer that. Everything in the file except
one fact is something provisioning already knows -- this project, this tenant,
this desktop, which socket rule -- and the one fact it cannot know is whether
the desktop's owner agrees. So the question is now that fact, asked as a
choice, and the answer is recorded once per host.

What must not follow from making it easy:

* the GUI owner comes from logind, not from who typed the command. A host with
  no active Wayland session is headless and says so; a host with several is an
  ambiguity that stops rather than being guessed;
* a recorded approval is honoured only for the account that actually owns the
  active desktop, so an approval file naming somebody else grants nothing;
* `--yes` still never grants desktop access that nobody approved;
* the generated policy is checked by the same validator an operator-authored
  one is, and installed and verified before the first role starts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import desktop_access as desktop  # noqa: E402
from scripts import team_launcher as launcher  # noqa: E402

PROJECT = "testing"
TENANT = "testing-agent"
OWNER = "desktop-owner"


def loginctl(sessions: dict[str, dict[str, str]]):
    """A logind that answers about exactly these sessions."""

    def runner(args: list[str]) -> str:
        if args[:2] == ["loginctl", "list-sessions"]:
            return "\n".join(f"{sid} 1000 {values.get('Name', '')} seat0" for sid, values in sessions.items())
        if args[:2] == ["loginctl", "show-session"]:
            values = sessions[args[2]]
            wanted = [args[index + 1] for index, item in enumerate(args) if item == "-p"]
            return "\n".join(f"{key}={values.get(key, '')}" for key in wanted)
        raise AssertionError(args)

    return runner


def session(name: str, *, kind: str = "wayland", active: str = "yes") -> dict[str, str]:
    return {"Name": name, "Type": kind, "Active": active}


def refused(call, expected: str) -> None:
    try:
        call()
    except (SystemExit, desktop.DesktopAccessError) as exc:
        assert expected in str(exc), (expected, str(exc))
        return
    raise AssertionError(f"expected a refusal containing {expected!r}")


# --------------------------------------------------------------------------
# who owns the desktop
# --------------------------------------------------------------------------


def test_the_gui_owner_comes_from_logind_not_from_the_caller() -> None:
    """One active Wayland session is the answer; a manager session is not.

    Every role account on a Switchyard host has a logind session of its own --
    a systemd user manager, `Type=unspecified`. Counting those would make every
    host ambiguous and every desktop unidentifiable, which is why the type and
    the active flag are both part of the question.
    """
    runner = loginctl(
        {
            "1": session("syrd-app", kind="unspecified"),
            "2": session(TENANT, kind="unspecified"),
            "13": session(OWNER),
            "4": session("someone-logged-out", active="no"),
        }
    )
    assert desktop.active_wayland_owners(runner) == [OWNER]
    assert desktop.resolve_gui_owner(runner=runner) == OWNER


def test_no_desktop_and_too_many_desktops_are_different_refusals() -> None:
    """One says install headless; the other says which one, and how to say it."""
    refused(
        lambda: desktop.resolve_gui_owner(runner=loginctl({"1": session("agent", kind="unspecified")})),
        "--headless",
    )
    crowded = loginctl({"1": session("ana"), "2": session("ben")})
    refused(lambda: desktop.resolve_gui_owner(runner=crowded), "--desktop-gui-user")
    refused(lambda: desktop.resolve_gui_owner(runner=crowded), "ana, ben")
    # Named, it is no longer ambiguous -- and a name nobody is signed in as is
    # not accepted just because it was asked for.
    assert desktop.resolve_gui_owner(preferred="ben", runner=crowded) == "ben"
    refused(lambda: desktop.resolve_gui_owner(preferred="carla", runner=crowded), "--desktop-gui-user")


# --------------------------------------------------------------------------
# what provisioning decides
# --------------------------------------------------------------------------


def resolve(tmp: Path, **overrides: Any) -> tuple[dict[str, Any], str]:
    asked: list[str] = []
    printed: list[str] = []
    arguments: dict[str, Any] = {
        "desktop_policy": None,
        "headless": False,
        "gui_user": "",
        "project": PROJECT,
        "tenant": TENANT,
        "yes": False,
        "input_func": lambda prompt: asked.append(prompt) or "",
        "print_func": printed.append,
        "settings_path": tmp / "desktop-approval.json",
        "owner_resolver": lambda *, preferred="": OWNER,
        "owners_lister": lambda: [OWNER],
    }
    arguments.update(overrides)
    policy, origin = launcher._resolve_desktop_policy(**arguments)
    return policy, origin, asked, printed  # type: ignore[return-value]


def test_an_ordinary_desktop_provision_asks_one_question_and_generates_the_policy() -> None:
    """Acceptance 1, 2, 3 and 7, in the order a person meets them.

    No path is asked for. The choice is numbered, desktop is the default, and
    the answer becomes a policy that says exactly who approved what, when, and
    with reference to which record.
    """
    with tempfile.TemporaryDirectory(prefix="syrd143-generate.") as tmp:
        tmp_path = Path(tmp)
        policy, origin, asked, printed = resolve(tmp_path)

    assert origin == launcher.DESKTOP_FROM_NEW_APPROVAL, origin
    # The question it no longer asks.
    assert not any("policy file" in prompt.lower() for prompt in asked), asked
    assert not any(".json" in prompt for prompt in asked), asked
    # The question it asks instead: named choices, with the recommendation said
    # out loud rather than implied by the order.
    menu = "\n".join(printed)
    assert "1) desktop" in menu and "2) headless" in menu, menu
    assert "recommended" in menu, menu
    assert any("[default]" in line and "desktop" in line for line in printed), printed
    assert asked == ["Desktop access [desktop]: "], asked

    assert policy["mode"] == "wayland", policy
    assert policy["project"] == PROJECT and policy["tenant_user"] == TENANT, policy
    assert policy["gui_user"] == OWNER, policy
    assert policy["wayland_display"] == "auto", policy
    consent = policy["consent"]
    assert consent["approved"] is True and consent["by"] == OWNER, consent
    assert consent["at"].endswith("Z") and consent["reference"].strip(), consent
    # And it is a policy by the same rules as one somebody wrote by hand.
    assert desktop.validate_policy(policy, project=PROJECT, tenant=TENANT) == policy


def test_the_choice_is_constrained_and_a_wrong_answer_is_asked_again() -> None:
    """Acceptance 7: a few named answers, not a string somebody has to get right.

    A prompt that accepts whatever is typed is a free-form prompt wearing a
    list. Numbers and names are the answers; anything else is re-asked, and a
    caller who keeps missing is stopped rather than silently given a value
    nobody offered.
    """
    printed: list[str] = []
    answers = iter(["maybe", "2"])
    chosen = launcher._prompt_choice(
        "Desktop access",
        options=(("desktop", "recommended"), ("headless", "no clipboard")),
        default="desktop",
        input_func=lambda _prompt: next(answers),
        print_func=printed.append,
    )
    assert chosen == "headless", chosen
    assert any("answer with the number or the name" in line for line in printed), printed
    # By name, and by empty for the default.
    assert launcher._prompt_choice(
        "Desktop access", options=(("desktop", ""), ("headless", "")), default="desktop",
        input_func=lambda _prompt: "headless", print_func=lambda _line: None,
    ) == "headless"
    assert launcher._prompt_choice(
        "Desktop access", options=(("desktop", ""), ("headless", "")), default="desktop",
        input_func=lambda _prompt: "", print_func=lambda _line: None,
    ) == "desktop"
    refused(
        lambda: launcher._prompt_choice(
            "Desktop access", options=(("desktop", ""), ("headless", "")), default="desktop",
            input_func=lambda _prompt: "/home/someone/policy.json", print_func=lambda _line: None,
        ),
        "too many invalid answers",
    )


def test_generation_is_checked_rather_than_trusted() -> None:
    """What provisioning writes is subject to the rules it would refuse to read.

    The fields come from this code rather than from a person, which is an
    argument for getting them right and not an argument for skipping the check:
    the one thing generation must never become is a way to install a policy
    that `--desktop-policy` would have rejected.
    """
    refused(
        lambda: desktop.generated_policy(
            project=PROJECT, tenant=TENANT, gui_user=OWNER, approved_by=OWNER,
            wayland_display="../../private",
        ),
        "wayland_display",
    )
    refused(
        lambda: desktop.generated_policy(
            project=PROJECT, tenant=TENANT, gui_user="name\nExecStart=bad", approved_by=OWNER,
        ),
        "valid GUI and tenant users",
    )
    refused(
        lambda: desktop.generated_policy(
            project="Not A Slug", tenant=TENANT, gui_user=OWNER, approved_by=OWNER,
        ),
        "Invalid desktop policy project",
    )
    # And a named socket, which is the one field a host with several may need.
    named = desktop.generated_policy(
        project=PROJECT, tenant=TENANT, gui_user=OWNER, approved_by=OWNER,
        wayland_display="wayland-1",
    )
    assert named["wayland_display"] == "wayland-1", named


def test_the_approval_is_recorded_once_and_reused_without_asking() -> None:
    """Acceptance 2: the second project does not interrupt anybody."""
    with tempfile.TemporaryDirectory(prefix="syrd143-reuse.") as tmp:
        tmp_path = Path(tmp)
        settings = tmp_path / "desktop-approval.json"
        _first, _origin, _asked, _printed = resolve(tmp_path)
        recorded = json.loads(settings.read_text(encoding="utf-8"))
        assert recorded["gui_user"] == OWNER, recorded
        # Written by the privileged provisioning run and readable by nobody
        # else. An approval a tenant could edit is an approval a tenant could
        # give itself, and this is the file the later runs act on.
        import stat as _stat

        assert _stat.S_IMODE(settings.stat().st_mode) == 0o600, oct(settings.stat().st_mode)
        assert recorded["approved_by"] == OWNER and recorded["approved_at"].endswith("Z"), recorded
        assert OWNER in recorded["reference"], recorded

        def must_not_ask(prompt: str) -> str:
            raise AssertionError(f"asked again: {prompt}")

        second, origin, _asked, printed = resolve(
            tmp_path, project="second", tenant="second-agent", yes=True, input_func=must_not_ask
        )
    assert origin == launcher.DESKTOP_FROM_HOST_APPROVAL, origin
    assert second["project"] == "second" and second["tenant_user"] == "second-agent", second
    # Narrowly scoped per project, from one approval: the grant names this
    # tenant and nobody else's.
    assert second["gui_user"] == OWNER and second["consent"]["by"] == OWNER, second
    assert second["consent"]["reference"] == recorded["reference"], second
    assert any("recorded desktop approval" in line for line in printed), printed


def test_an_approval_for_somebody_else_grants_nothing() -> None:
    """The anti-self-authorization property this ticket's new file has to hold.

    The record says who approved, and logind says whose desktop is running.
    Only the account that is both gets a generated grant, so a file naming a
    tenant -- however it came to say that -- is simply not the owner of the
    session being granted, and provisioning asks the human instead.
    """
    with tempfile.TemporaryDirectory(prefix="syrd143-forged.") as tmp:
        tmp_path = Path(tmp)
        settings = tmp_path / "desktop-approval.json"
        settings.write_text(
            json.dumps(
                {
                    "gui_user": TENANT,
                    "approved_by": TENANT,
                    "approved_at": "2026-09-14T00:00:00Z",
                    "reference": "a tenant approving itself",
                }
            ),
            encoding="utf-8",
        )
        # Non-interactive: nothing is generated from it at all.
        refused(
            lambda: resolve(tmp_path, yes=True),
            "--yes does not grant desktop access",
        )
        # Interactive: the human is asked, and what gets recorded and granted
        # is the account that actually owns the desktop.
        policy, origin, _asked, _printed = resolve(tmp_path)
        rewritten = json.loads(settings.read_text(encoding="utf-8"))
    assert origin == launcher.DESKTOP_FROM_NEW_APPROVAL, origin
    assert policy["gui_user"] == OWNER and policy["consent"]["by"] == OWNER, policy
    assert rewritten["gui_user"] == OWNER, rewritten


def test_the_headless_install_is_explicit_and_needs_no_compositor() -> None:
    """Acceptance 3: available, and never the answer nobody chose."""
    with tempfile.TemporaryDirectory(prefix="syrd143-headless.") as tmp:
        tmp_path = Path(tmp)
        settings = tmp_path / "desktop-approval.json"

        def must_not_ask(prompt: str) -> str:
            raise AssertionError(f"asked: {prompt}")

        def no_desktop(*, preferred: str = "") -> str:
            raise desktop.DesktopAccessError("No active Wayland session was found on this host.")

        policy, origin, _asked, _printed = resolve(
            tmp_path, headless=True, yes=True, input_func=must_not_ask,
            owner_resolver=no_desktop, owners_lister=list,
        )
        assert (policy, origin) == ({"mode": "headless"}, launcher.DESKTOP_FROM_HEADLESS_OPTION)
        assert not settings.exists(), "an explicit headless install records no desktop approval"

        # On a host with no desktop at all, an interactive run offers it rather
        # than dying, and a refusal to choose it provisions nothing.
        chosen, origin, _asked, _printed = resolve(
            tmp_path, owner_resolver=no_desktop, owners_lister=list
        )
        assert (chosen, origin) == ({"mode": "headless"}, launcher.DESKTOP_FROM_CHOSEN_HEADLESS)
        refused(
            lambda: resolve(
                tmp_path, owner_resolver=no_desktop, owners_lister=list,
                input_func=lambda prompt: "no",
            ),
            "nothing was provisioned",
        )
        assert not settings.exists()

        # And an ambiguous host is never offered headless: it is a question
        # about which desktop, not about whether to have one.
        def ambiguous(*, preferred: str = "") -> str:
            raise desktop.DesktopAccessError("Several accounts have an active Wayland session (ana, ben).")

        refused(
            lambda: resolve(
                tmp_path, owner_resolver=ambiguous, owners_lister=lambda: ["ana", "ben"],
                input_func=must_not_ask,
            ),
            "Several accounts",
        )


def test_an_explicit_policy_file_is_still_imported_and_still_checked() -> None:
    """Acceptance 4: the advanced path keeps its validation and its refusals."""
    with tempfile.TemporaryDirectory(prefix="syrd143-import.") as tmp:
        tmp_path = Path(tmp)
        authored = desktop.generated_policy(
            project=PROJECT, tenant=TENANT, gui_user=OWNER, approved_by="an operator",
            reference="ticket SYRD-143",
        )
        path = tmp_path / "approved.json"
        path.write_text(json.dumps(authored), encoding="utf-8")

        def must_not_ask(prompt: str) -> str:
            raise AssertionError(f"asked: {prompt}")

        imported, origin, _asked, _printed = resolve(
            tmp_path, desktop_policy=path, yes=True, input_func=must_not_ask
        )
        assert origin == launcher.DESKTOP_FROM_POLICY_FILE, origin
        assert imported == authored, imported
        assert desktop.validate_policy(imported, project=PROJECT, tenant=TENANT) == authored
        # The mismatch refusal the import path has always had.
        refused(
            lambda: desktop.validate_policy(imported, project=PROJECT, tenant="somebody-else"),
            "does not match this tenant",
        )
        # `headless` by that route still means headless.
        assert resolve(tmp_path, desktop_policy=Path("headless"), yes=True)[0] == {"mode": "headless"}
        # Two ways of saying different things is a mistake, not a precedence rule.
        refused(
            lambda: resolve(tmp_path, desktop_policy=path, headless=True, yes=True),
            "not both",
        )


def test_the_new_command_offers_the_choices_rather_than_a_free_form_path() -> None:
    """The flags a person reads before they ever see a prompt."""
    parser = launcher._build_switchyard_new_parser()
    args = parser.parse_args([])
    assert args.headless is False and args.desktop_gui_user == "" and args.desktop_policy is None
    assert parser.parse_args(["--headless"]).headless is True
    assert parser.parse_args(["--desktop-gui-user", "ana"]).desktop_gui_user == "ana"
    help_text = parser.format_help()
    assert "--headless" in help_text and "no compositor" in help_text, help_text
    assert "advanced" in help_text, help_text


# --------------------------------------------------------------------------
# and the whole provisioning run
# --------------------------------------------------------------------------


def test_a_desktop_project_is_granted_installed_and_verified_before_its_first_role() -> None:
    """Acceptance 5 and 8: the `testing` / `testing-agent` flow, end to end.

    No policy file anywhere. The approval this host already recorded is the
    only input, and what has to be true by the time the first role process
    starts is the same as it ever was: the GUI owner installed the grant, the
    tenant verified it twice, and the role's environment names the approved
    socket.
    """
    from contextlib import ExitStack

    with tempfile.TemporaryDirectory(prefix="syrd143-new.") as tmp:
        root = Path(tmp)
        export = root / "installed-release"
        shutil.copytree(ROOT / "scripts", export / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        import importlib.util

        spec = importlib.util.spec_from_file_location("syrd143_launcher", export / "scripts/team_launcher.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)

        project = root / "project"
        project.mkdir()
        owner_home = root / "tenant-home"
        owner_home.mkdir()
        artifact = root / "project.json"
        artifact.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": str(project / "DESIGN.md"),
                    "project": {
                        "slug": PROJECT,
                        "name": "Testing",
                        "ticket_prefix": "TST",
                        "owner_user": TENANT,
                        "repository": str(project),
                        "roles": ["main"],
                        "role_clis": {"director": "claude", "main": "codex"},
                        "include_designer": False,
                        "include_audit": False,
                    },
                }
            )
        )
        settings = root / "desktop-approval.json"
        mod.write_host_desktop_approval(OWNER, settings_path=settings, confirmed_by=OWNER)

        output = project / ".switchyard/provision"
        events: list[str] = []
        environment = {
            "WAYLAND_DISPLAY": "/run/user/7900/wayland-7",
            "XDG_RUNTIME_DIR": "/run/user/7901",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/7901/bus",
        }

        def runner(args, **kwargs):
            if "verify" in args:
                events.append("tenant-verified")
                return subprocess.CompletedProcess(args, 0, json.dumps(environment), "")
            return subprocess.CompletedProcess(args, 0, "", "")

        def first_auth(config, **kwargs):
            assert events == ["gui-installed", "tenant-verified", "tenant-verified"], events
            assert config.roles[0].env["WAYLAND_DISPLAY"] == environment["WAYLAND_DISPLAY"], config.roles[0].env
            events.append("auth")
            return mod.FirstRunAuthReport({}, [])

        def first_launch(config, **kwargs):
            assert events[-1] == "auth", events
            events.append("first-role")
            return 0

        with ExitStack() as stack:
            real_getpwnam = mod.pwd.getpwnam
            fixture_owner = SimpleNamespace(
                pw_name=TENANT, pw_uid=os.getuid(), pw_gid=os.getgid(),
                pw_dir=f"/home/{TENANT}", pw_shell="/bin/bash",
            )
            stack.enter_context(
                patch.object(
                    mod.pwd, "getpwnam",
                    side_effect=lambda name: fixture_owner if name == TENANT else real_getpwnam(name),
                )
            )
            stack.enter_context(patch.object(mod.os, "geteuid", return_value=0))
            for name in [
                "_precheck_project_path_before_mutating", "precheck_new_project",
                "_chown_switchyard_project_files", "_install_switchyard_onboarding_docs",
                "_require_existing_project_git_repository", "_commit_project_git_changes",
                "_prepare_first_run_auth_worktrees", "report_launch_session_records",
            ]:
                stack.enter_context(patch.object(mod, name, return_value=None))
            stack.enter_context(patch.object(mod, "role_isolation_gaps", return_value=[]))
            stack.enter_context(
                patch.object(mod, "_ensure_owner_user_and_project_dir",
                             return_value=mod.OwnerUserProvisionResult(False, False))
            )
            stack.enter_context(patch.object(mod, "_owner_home_for_auth", return_value=owner_home))
            stack.enter_context(patch.object(mod, "run_first_run_auth_phase", side_effect=first_auth))
            stack.enter_context(patch.object(mod, "launch_project", side_effect=first_launch))
            stack.enter_context(
                patch.object(desktop, "install", side_effect=lambda *a, **k: events.append("gui-installed"))
            )
            # The only thing standing in for the host: which account logind
            # says owns the running desktop.
            stack.enter_context(patch.object(desktop, "active_wayland_owners", return_value=[OWNER]))
            stack.enter_context(patch.object(desktop, "resolve_gui_owner", return_value=OWNER))

            def must_not_ask(prompt: str) -> str:
                raise AssertionError(f"asked: {prompt}")

            assert mod.switchyard_new_command(
                from_artifact=artifact,
                source_repo=export,
                output_dir=output,
                yes=True,
                desktop_approval_settings_path=settings,
                allow_existing_owner_user=True,
                git_init=False,
                runner=runner,
                euid_getter=lambda: 0,
                home_base=root / "homes",
                registry_dir=root / "registry",
                config_dir=root / "configs",
                input_func=must_not_ask,
                print_func=lambda _message: None,
            ) == 0

        assert events == ["gui-installed", "tenant-verified", "tenant-verified", "auth", "first-role"], events
        written = json.loads((output / "desktop-policy.json").read_text(encoding="utf-8"))
        assert written["mode"] == "wayland", written
        assert (written["project"], written["tenant_user"], written["gui_user"]) == (PROJECT, TENANT, OWNER)
        assert written["consent"]["approved"] is True and written["consent"]["by"] == OWNER
        assert json.loads((output / f"{PROJECT}.json").read_text(encoding="utf-8"))["desktop_access"] == written


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    print(f"desktop_policy_generation_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
