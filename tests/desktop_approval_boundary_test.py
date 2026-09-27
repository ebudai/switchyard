#!/usr/bin/env python3
"""SYRD-325: this host's desktop approval, against the launcher it came out of.

The standing desktop approval -- its path, its readers and writers, and
`switchyard approve-desktop` -- moved into `scripts/desktop_approval.py`
unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- Every name the launcher and the suites reach as `team_launcher.<name>` is
  still there and is the very same object, whichever module is imported first.
- **Seams (rule 24).** The suites patch `read_host_desktop_approval` on the
  launcher and the launcher's callers reach it there; the module reads the
  user-name check, the JSON writer, the privileged uid and the no-follow
  reader from the launcher when it runs.
- **Rule 27.** The operator resolver and the desktop sessions stay each
  function's own imports; neither is read through the launcher.
- **Authority and consent are unchanged:** writing needs root and a person the
  elevating mechanism names; a reference is required; an unreadable record is
  not written over; a revocation keeps the record with nobody approved in it;
  a write is read back.

Every record here is a file this test writes in its own temporary directory,
and the documented privileged-root seam makes this account the privileged
owner, so no /etc file, grant or real operator is touched; the operator,
desktop sessions and effective uid are fakes.
"""

from __future__ import annotations

import ast
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    'DEFAULT_DESKTOP_APPROVAL_SETTING_PATH', 'DESKTOP_APPROVAL_REVOKED', '_desktop_approval_lines',
    '_desktop_approval_operator', '_desktop_approval_setting_path', 'read_desktop_approval_record',
    'read_host_desktop_approval', 'switchyard_approve_desktop_command', 'write_desktop_approval_record',
    'write_host_desktop_approval',
)
#: The launcher's own call sites, measured on the baseline and unchanged.
LAUNCHER_CALLS = {"read_host_desktop_approval": 3, "write_host_desktop_approval": 1,
                  "switchyard_approve_desktop_command": 1}
LAUNCHER_READS = ("_is_valid_owner_user_name", "_write_json_atomic", "expected_privileged_uid", "read_plan_no_follow")
ME = pwd.getpwuid(os.getuid()).pw_name
OPERATOR = SimpleNamespace(name="syrd325-operator", uid=1325, source="pkexec", known=True)
NOBODY = SimpleNamespace(name="", uid=None, source="", known=False)
WHY = "SYRD-325 boundary fixture"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


class patched:
    """Rebind attributes of one module for one block, as the suites do."""

    def __init__(self, module: object, **values: object) -> None:
        self.module = module
        self.values = values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.module, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.module, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.module, name, value)


class privileged_root:
    """The documented seam: with it set, this account's files count as root's."""

    def __init__(self, root: Path) -> None:
        self.root = str(root)

    def __enter__(self) -> None:
        self.saved = os.environ.get("SWITCHYARD_PRIVILEGED_PROVISION_ROOT")
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = self.root

    def __exit__(self, *exc: object) -> None:
        if self.saved is None:
            os.environ.pop("SWITCHYARD_PRIVILEGED_PROVISION_ROOT", None)
        else:
            os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = self.saved


def approve(path: Path, **kwargs: object) -> tuple[int, list[str]]:
    from scripts import desktop_approval

    said: list[str] = []
    operator = kwargs.pop("operator", OPERATOR)
    status = desktop_approval.switchyard_approve_desktop_command(
        settings_path=path, euid_getter=kwargs.pop("euid_getter", lambda: 0),
        operator_resolver=lambda: operator,
        owners_lister=kwargs.pop("owners", lambda: [ME]), print_func=said.append, **kwargs)
    return status, said


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.desktop_approval; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.desktop_approval'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.desktop_approval", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.desktop_approval")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.desktop_approval as d; "
            f"print(all(getattr(t, n) is getattr(d, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_and_the_functions_own_imports() -> None:
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    for name, count in LAUNCHER_CALLS.items():
        calls = [n for n in ast.walk(launcher_tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(calls) == count and all(isinstance(n.func, ast.Name) for n in calls),
              f"the launcher calls {name} at its {count} baseline sites, by its own patchable name")
    moved = ast.parse((ROOT / "scripts" / "desktop_approval.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in LAUNCHER_READS})
    check(bare == [], f"the launcher's helpers are read through it, never past it: {bare}")
    through = sorted({n.attr for n in ast.walk(moved) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher"
                      and n.attr in ("resolve_operator", "desktop", "desktop_access")})
    check(through == [], f"and the operator resolver and desktop sessions are the functions' own: {through}")


def test_the_host_approval_reads_and_writes() -> None:
    from scripts import desktop_approval, team_launcher

    written: list[tuple[Path, dict[str, object]]] = []
    with tempfile.TemporaryDirectory(prefix="syrd325.") as raw:
        path = Path(raw) / "etc" / "desktop-approval.json"
        missing = desktop_approval.read_host_desktop_approval(path)
        with patched(team_launcher, _write_json_atomic=lambda target, payload: (
                written.append((target, dict(payload))), target.write_text(json.dumps(payload)))):
            desktop_approval.write_host_desktop_approval(ME, settings_path=path, reference=WHY)
        read = desktop_approval.read_host_desktop_approval(path)
        path.write_text(json.dumps({"gui_user": "not a user; rm -rf"}), encoding="utf-8")
        try:
            desktop_approval.read_host_desktop_approval(path)
            refused = ""
        except SystemExit as exc:
            refused = str(exc)
    check(missing == {}, "no file, no approval")
    check(len(written) == 1 and written[0][0] == path and written[0][1]["approved_by"] == ME
          and written[0][1]["reference"] == WHY, f"written through the launcher's JSON writer: {written}")
    check(read["gui_user"] == ME and read["approved_by"] == ME and read["approved_at"].endswith("Z"),
          f"and read back as the approval: {read}")
    check("which is not a plain Unix user name" in refused, f"a malformed user is refused, not ignored: {refused!r}")
    check(desktop_approval._desktop_approval_setting_path() == desktop_approval.DEFAULT_DESKTOP_APPROVAL_SETTING_PATH
          == Path("/etc/switchyard/desktop-approval.json"), "the default is root's host setting")


def test_approving_revoking_and_showing() -> None:
    # A non-root account cannot give a file to root's group, so the record's
    # fchown is recorded rather than performed; the mode and rename are real.
    chowned: list[tuple[int, int]] = []
    real_fchown = os.fchown
    os.fchown = lambda fd, uid, gid: chowned.append((uid, gid))
    try:
        results = _approve_revoke_show()
    finally:
        os.fchown = real_fchown
    (not_root, nobody, no_reason, several, unknown, wrote_nothing, approved, record, mode, again, shown, revoked,
     after, still_revoked, host_view, unreadable) = results
    check(chowned and all(entry == (os.getuid(), 0) for entry in chowned),
          f"every record is given to the privileged uid and root's group: {chowned}")
    _check_approve_revoke_show(not_root, nobody, no_reason, several, unknown, wrote_nothing, approved, record, mode,
                               again, shown, revoked, after, still_revoked, host_view, unreadable)


def _approve_revoke_show() -> tuple[object, ...]:
    from scripts import desktop_approval

    with tempfile.TemporaryDirectory(prefix="syrd325.") as raw, privileged_root(Path(raw)):
        path = Path(raw) / "etc" / "desktop-approval.json"
        not_root = approve(path, reference=WHY, euid_getter=lambda: 1000)
        nobody = approve(path, reference=WHY, operator=NOBODY)
        no_reason = approve(path, reference="  ")
        several = approve(path, reference=WHY, owners=lambda: [ME, "syrd325-other"])
        unknown = approve(path, reference=WHY, gui_user="syrd325-no-such-account")
        wrote_nothing = not path.exists()
        approved = approve(path, reference=WHY)
        record = json.loads(path.read_text(encoding="utf-8"))
        mode = stat.S_IMODE(path.stat().st_mode)
        again = approve(path, reference=WHY)
        shown = approve(path, show=True)
        revoked = approve(path, revoke=True, reference="withdrawn by SYRD-325")
        after = json.loads(path.read_text(encoding="utf-8"))
        still_revoked = approve(path, revoke=True, reference="again")
        host_view = desktop_approval.read_host_desktop_approval(path)
        path.chmod(0o666)
        unreadable = approve(path, reference=WHY)
    return (not_root, nobody, no_reason, several, unknown, wrote_nothing, approved, record, mode, again, shown,
            revoked, after, still_revoked, host_view, unreadable)


def _check_approve_revoke_show(not_root, nobody, no_reason, several, unknown, wrote_nothing, approved, record, mode,
                               again, shown, revoked, after, still_revoked, host_view, unreadable) -> None:
    check(not_root[0] == 1 and "Run: pkexec switchyard approve-desktop" in not_root[1][0],
          f"not root: it says how, and writes nothing: {not_root}")
    check(nobody[0] == 1 and "there is nobody to record" in nobody[1][0], f"nobody to attribute: {nobody}")
    check(no_reason[0] == 1 and "--reference is required" in no_reason[1][0], f"no reference: {no_reason}")
    check(several[0] == 1 and "Name the one this host is approving" in several[1][0], f"several desktops: {several}")
    check(unknown[0] == 1 and "is not an account on this host" in unknown[1][0], f"an unknown account: {unknown}")
    check(wrote_nothing, "none of those wrote a record")
    check(approved[0] == 0 and record["gui_user"] == ME and record["approved_by"] == "syrd325-operator"
          and record["approved_via"] == "pkexec" and record["approved_uid"] == "1325" and record["reference"] == WHY,
          f"an approval names the account, the person who elevated and why: {record}")
    check(mode == 0o600, f"root's record, 0600: {mode:o}")
    check(again[0] == 0 and "already approved for" in again[1][0], f"approving twice changes nothing: {again}")
    check(shown[0] == 0 and shown[1][0] == f"switchyard: this host's desktop is {ME}'s, and scoped project access to it",
          f"show reads it back: {shown}")
    check(revoked[0] == 0 and after["gui_user"] == "" and after["revoked_by"] == "syrd325-operator"
          and after["withdrew"]["gui_user"] == ME, f"a revocation keeps the record, with nobody approved: {after}")
    check(still_revoked[0] == 0 and "already revoked" in still_revoked[1][0] and host_view == {},
          f"a revoked host has no approval to act on: {still_revoked} {host_view}")
    check(unreadable[0] == 1 and "refusing to write over a record this cannot read" in unreadable[1][1],
          f"a record anyone could have written is not written over: {unreadable}")


def test_the_record_reads_the_launcher_when_it_runs() -> None:
    from scripts import desktop_approval, team_launcher

    asked: list[tuple[Path, bool]] = []
    with tempfile.TemporaryDirectory(prefix="syrd325.") as raw:
        path = Path(raw) / "desktop-approval.json"
        path.write_text(json.dumps({"gui_user": ME}), encoding="utf-8")
        with patched(team_launcher, read_plan_no_follow=lambda target, *, require_root_owned: (
                asked.append((target, require_root_owned)), (None, "syrd325 refused"))[1]):
            refused = desktop_approval.read_desktop_approval_record(path)
        chowned: list[int] = []
        real_fchown = os.fchown
        os.fchown = lambda fd, uid, gid: chowned.append(uid)
        try:
            with patched(team_launcher, expected_privileged_uid=lambda: 4325):
                desktop_approval.write_desktop_approval_record({"gui_user": ME}, settings_path=path)
        finally:
            os.fchown = real_fchown
        staged_gone = not (Path(raw) / ".desktop-approval.json.new").exists()
    check(asked == [(path, True)] and refused == (None, "syrd325 refused"),
          "the record is read by the launcher's no-follow reader, as rebound, and must be root's")
    check(chowned == [4325] and staged_gone,
          f"and written to the launcher's privileged uid, staged and renamed: {chowned}")


def test_a_planted_symlink_and_a_write_that_does_not_read_back() -> None:
    from scripts import desktop_approval, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd325.") as raw:
        root = Path(raw)
        path = root / "desktop-approval.json"
        elsewhere = root / "elsewhere"
        (root / ".desktop-approval.json.new").symlink_to(elsewhere)
        try:
            with patched(team_launcher, expected_privileged_uid=os.getuid):
                desktop_approval.write_desktop_approval_record({"gui_user": ME}, settings_path=path)
            planted = ""
        except OSError as exc:
            planted = exc.strerror or str(exc)
        through = elsewhere.exists() or path.exists()
        fresh = root / "fresh" / "desktop-approval.json"
        chowned: list[tuple[int, int]] = []
        real_fchown = os.fchown
        os.fchown = lambda fd, uid, gid: chowned.append((uid, gid))
        try:
            with patched(team_launcher, read_plan_no_follow=lambda target, *, require_root_owned: (
                    None, "syrd325 cannot read it back")):
                unread = approve(fresh, reference=WHY)
        finally:
            os.fchown = real_fchown
    check(planted != "" and not through,
          f"a symlink planted at the staged name is an error, and nothing is written through it: {planted!r}")
    check(unread[0] == 1 and unread[1][-1].endswith("was written but does not read back; nothing is approved."),
          f"a record that does not read back approves nothing: {unread}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"desktop_approval_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
