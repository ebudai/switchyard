#!/usr/bin/env python3
"""SYRD-174: a front door onto this host's standing desktop approval.

The record at `/etc/switchyard/desktop-approval.json` is root-owned 0600 for a
reason its own writer states: an approval a tenant could write is an approval a
tenant could give itself. What it did not have was a way to write it. Its single
caller sat immediately after `_prompt_choice("Desktop access", ...)`, so the only
way to pre-authorize a host was to reach past the CLI into the library or to
hand-write root-owned JSON -- both of them the thing everything else in this
system is careful not to do.

`switchyard approve-desktop` is that door, and it is narrow on purpose:

  - approving and revoking need root, and need a person. The attribution comes
    from the mechanism that elevated the run -- PKEXEC_UID, then SUDO_USER --
    so the record cannot be made to name somebody who did not ask for it;
  - `--reference` is required, because an approval nobody can attribute is
    worse than no approval;
  - the record is read by fd with no symlink at any component, required to
    belong to root and to be unwritable by anybody else, and written the same
    way: staged with O_NOFOLLOW, renamed, root-owned 0600;
  - a record that exists and cannot be read is never written over;
  - revoking keeps the record with nobody approved in it, so a withdrawal is
    evidence rather than an absence -- and `read_host_desktop_approval` already
    answers "no approval" for exactly that shape.

The privileged half runs inside a user namespace, where this process is uid 0
and can own the file root would own.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts import team_launcher as launcher  # noqa: E402
from resume_provision_test import namespaces_available  # noqa: E402

OPERATOR = SimpleNamespace(name="eric", uid=1000, source="pkexec", known=True)
BY_SUDO = SimpleNamespace(name="an-operator", uid=1001, source="sudo", known=True)
NOBODY = SimpleNamespace(name="", uid=None, source="", known=False)
GUI_USER = "eric"
WHY = "host owner authorized unattended provisioning, ticket SYRD-174"


def approve(path: Path, **kwargs) -> tuple[int, list[str]]:
    said: list[str] = []
    euid = kwargs.pop("euid_getter", lambda: 0)
    operator = kwargs.pop("operator", OPERATOR)
    owners = kwargs.pop("owners", lambda: [GUI_USER])
    status = launcher.switchyard_approve_desktop_command(
        settings_path=path,
        euid_getter=euid,
        operator_resolver=lambda: operator,
        owners_lister=owners,
        print_func=said.append,
        **kwargs,
    )
    return status, said


# --------------------------------------------------------------------------
# the gates, which need nothing owned
# --------------------------------------------------------------------------


def test_an_unprivileged_caller_is_told_how_this_is_run() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd174-unprivileged.") as tmp:
        path = Path(tmp) / "desktop-approval.json"
        status, said = approve(path, reference=WHY, euid_getter=lambda: 1000)
        assert status == 1, said
        assert any("pkexec switchyard approve-desktop" in line for line in said), said
        assert not path.exists(), "an unprivileged run writes nothing"

        status, said = approve(path, revoke=True, reference=WHY, euid_getter=lambda: 1000)
        assert status == 1 and any("--revoke" in line for line in said), said


def test_a_run_nobody_can_be_attributed_to_is_refused() -> None:
    """Root alone is not a person, and this record names one."""
    with tempfile.TemporaryDirectory(prefix="syrd174-nobody.") as tmp:
        path = Path(tmp) / "desktop-approval.json"
        status, said = approve(path, reference=WHY, operator=NOBODY)
        assert status == 1, said
        assert any("nobody to record" in line for line in said), said
        assert not path.exists()


def test_a_reference_is_required_and_is_not_a_formality() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd174-reference.") as tmp:
        path = Path(tmp) / "desktop-approval.json"
        for empty in ("", "   "):
            status, said = approve(path, reference=empty)
            assert status == 1, said
            assert any("--reference is required" in line for line in said), said
            assert not path.exists()


def test_the_command_is_discoverable_and_says_what_it_is_not() -> None:
    assert "approve-desktop" in launcher.SWITCHYARD_COMMANDS
    assert "approve-desktop" in launcher.switchyard_help_text()
    parser = launcher._build_switchyard_approve_desktop_parser()
    args = parser.parse_args([])
    assert args.gui_user == "" and args.reference == "" and not args.show and not args.revoke
    assert parser.parse_args(["--show"]).show is True
    assert parser.parse_args(["--revoke"]).revoke is True
    # Argparse wraps, so the help is read as words rather than as lines.
    help_text = " ".join(parser.format_help().split())
    # The distinction the Director asked to keep: a standing host grant is not
    # the one-launch policy flag.
    assert "--desktop-policy" in help_text and "for one launch" in help_text, help_text
    assert "PKEXEC_UID" in help_text, help_text


def test_show_and_revoke_are_not_asked_for_at_once() -> None:
    parser = launcher._build_switchyard_approve_desktop_parser()
    try:
        parser.parse_args(["--show", "--revoke"])
    except SystemExit:
        return
    raise AssertionError("--show and --revoke are different questions")


# --------------------------------------------------------------------------
# the privileged half
# --------------------------------------------------------------------------


def privileged_cases() -> int:
    checks = 0
    with tempfile.TemporaryDirectory(prefix="syrd174-privileged.") as tmp:
        root = Path(tmp)
        path = root / "desktop-approval.json"

        # 1. Nothing recorded yet: show says so rather than inventing one.
        status, said = approve(path, show=True)
        assert status == 1, said
        assert any("no recorded desktop approval" in line for line in said), said
        checks += 1

        # 2. Approving writes the record, root-owned and 0600, and reads back.
        status, said = approve(path, gui_user=GUI_USER, reference=WHY)
        assert status == 0, said
        info = os.stat(path)
        assert info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600, (info.st_uid, oct(info.st_mode))
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["gui_user"] == GUI_USER
        assert record["reference"] == WHY
        # Attribution from the mechanism, not from anything anybody typed.
        assert record["approved_by"] == OPERATOR.name
        assert record["approved_via"] == "pkexec"
        assert record["approved_uid"] == str(OPERATOR.uid)
        assert any("recorded" in line and GUI_USER in line for line in said), said
        checks += 1

        # 3. And provisioning reads exactly that, which is the point of it.
        assert launcher.read_host_desktop_approval(path) == {
            "gui_user": GUI_USER,
            "approved_by": OPERATOR.name,
            "approved_at": record["approved_at"],
            "reference": WHY,
        }
        checks += 1

        # 4. Show validates first, then prints what is there.
        status, said = approve(path, show=True)
        assert status == 0, said
        joined = "\n".join(said)
        assert GUI_USER in joined and OPERATOR.name in joined and WHY in joined, joined
        checks += 1

        # 5. Approving the same desktop again changes nothing and says so.
        before = (path.read_bytes(), path.stat().st_ino)
        status, said = approve(path, gui_user=GUI_USER, reference="a different reason")
        assert status == 0, said
        assert any("already approved" in line for line in said), said
        assert (path.read_bytes(), path.stat().st_ino) == before
        checks += 1

        # 6. Revoking keeps the record, with nobody approved in it, and says
        #    what it withdrew. Provisioning then sees no approval at all.
        status, said = approve(path, revoke=True, reference="the owner withdrew it")
        assert status == 0, said
        revoked = json.loads(path.read_text(encoding="utf-8"))
        assert revoked["gui_user"] == ""
        assert revoked["revoked_by"] == OPERATOR.name and revoked["revoked_via"] == "pkexec"
        assert revoked["withdrew"]["gui_user"] == GUI_USER
        assert launcher.read_host_desktop_approval(path) == {}
        assert any("what was withdrawn" in line for line in said), said
        assert os.stat(path).st_uid == 0
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        checks += 1

        # 7. Revoking again is idempotent, and so is revoking nothing at all.
        before = (path.read_bytes(), path.stat().st_ino)
        status, said = approve(path, revoke=True, reference="again")
        assert status == 0, said
        assert any("already revoked" in line for line in said), said
        assert (path.read_bytes(), path.stat().st_ino) == before

        missing = root / "never-approved.json"
        status, said = approve(missing, revoke=True, reference="nothing here")
        assert status == 0, said
        assert any("nothing" in line and "to revoke" in line for line in said), said
        assert not missing.exists()
        checks += 1

        # 8. And the host can be approved again afterwards, which is what makes
        #    the revocation a state rather than a dead end.
        status, said = approve(path, gui_user=GUI_USER, reference=WHY)
        assert status == 0, said
        assert launcher.read_host_desktop_approval(path)["gui_user"] == GUI_USER
        checks += 1

        # 9. A record anybody else could rewrite is never read or written over.
        path.chmod(0o666)
        status, said = approve(path, show=True)
        assert status == 1 and any("group or beyond can write" in line for line in said), said
        status, said = approve(path, gui_user=GUI_USER, reference=WHY)
        assert status == 1, said
        assert any("refusing to write over a record this cannot read" in line for line in said), said
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o666, "the refusal changed nothing"
        path.chmod(0o600)
        checks += 1

        # 10. A symlink where the record should be is an error, not a redirect.
        planted = root / "planted.json"
        planted.write_text(json.dumps({"gui_user": "somebody-else"}), encoding="utf-8")
        link = root / "linked-approval.json"
        link.symlink_to(planted)
        status, said = approve(link, show=True)
        assert status == 1 and any("symlink" in line for line in said), said
        status, said = approve(link, gui_user=GUI_USER, reference=WHY)
        assert status == 1, said
        assert json.loads(planted.read_text(encoding="utf-8"))["gui_user"] == "somebody-else", (
            "the write followed a symlink"
        )
        checks += 1

        # 11. A record edited into a shape this will not act on is named, not
        #     quietly replaced.
        broken = root / "broken.json"
        broken.write_text(json.dumps({"gui_user": "not a user name"}), encoding="utf-8")
        os.chmod(broken, 0o600)
        status, said = approve(broken, show=True)
        assert status == 1 and any("not a plain Unix user name" in line for line in said), said
        status, said = approve(broken, gui_user=GUI_USER, reference=WHY)
        assert status == 1, said
        assert json.loads(broken.read_text(encoding="utf-8"))["gui_user"] == "not a user name"
        checks += 1

        # 12. Who is approved is validated too, and never guessed.
        fresh = root / "fresh.json"
        status, said = approve(fresh, gui_user="not a user name", reference=WHY)
        assert status == 1 and any("not a plain Unix user name" in line for line in said), said

        status, said = approve(fresh, reference=WHY, owners=lambda: ["eric", "someone-else"])
        assert status == 1, said
        assert any("--gui-user USER" in line for line in said), said

        status, said = approve(fresh, reference=WHY, owners=lambda: [])
        assert status == 1, said
        assert any("no active desktop session" in line for line in said), said
        assert not fresh.exists()
        checks += 1

        # 13. Exactly one signed-in desktop is inferred; a named account with no
        #     session is recorded with that said out loud, because whose desktop
        #     this is is not a session list's decision.
        status, said = approve(fresh, reference=WHY, owners=lambda: [GUI_USER])
        assert status == 0, said
        assert launcher.read_host_desktop_approval(fresh)["gui_user"] == GUI_USER

        elsewhere = root / "no-session.json"
        status, said = approve(
            elsewhere, gui_user="root", reference=WHY, owners=lambda: [GUI_USER]
        )
        assert status == 0, said
        assert any("no active desktop session" in line for line in said), said
        assert launcher.read_host_desktop_approval(elsewhere)["gui_user"] == "root"
        checks += 1

        # 14. sudo names a person too, and the record says which mechanism it
        #     was rather than flattening them together.
        by_sudo = root / "by-sudo.json"
        status, said = approve(by_sudo, gui_user=GUI_USER, reference=WHY, operator=BY_SUDO)
        assert status == 0, said
        stored = json.loads(by_sudo.read_text(encoding="utf-8"))
        assert stored["approved_by"] == BY_SUDO.name and stored["approved_via"] == "sudo"
        checks += 1

        # 15. What this is for: provisioning resolves a policy from the record
        #     without asking anybody anything.
        def never_asked(prompt: str) -> str:
            raise AssertionError(f"provisioning asked a question it should not have: {prompt}")

        policy, origin = launcher._resolve_desktop_policy(
            desktop_policy=None,
            headless=False,
            gui_user="",
            project="demo",
            tenant="demo-agent",
            yes=False,
            input_func=never_asked,
            settings_path=fresh,
            owner_resolver=lambda **_kwargs: GUI_USER,
            owners_lister=lambda: [GUI_USER],
            print_func=lambda _line: None,
        )
        assert policy["mode"] == "wayland" and policy["gui_user"] == GUI_USER, policy
        assert policy["consent"]["approved"] is True
        assert policy["consent"]["by"] == OPERATOR.name, policy["consent"]
        assert origin == launcher.DESKTOP_FROM_HOST_APPROVAL, origin
        checks += 1
    return checks


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    if "--privileged-child" in sys.argv:
        checks += privileged_cases()
        print(f"host_desktop_approval_test: privileged child ran {checks} checks")
        return 0
    if namespaces_available():
        done = subprocess.run(
            ["unshare", "--user", "--map-root-user", sys.executable, __file__, "--privileged-child"],
            text=True, capture_output=True,
        )
        if done.returncode != 0:
            print(done.stdout + done.stderr)
            return 1
        print(done.stdout.strip())
    else:
        print("host_desktop_approval_test: user namespaces unavailable; privileged half skipped")
    print(f"host_desktop_approval_test: {checks} unprivileged checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
