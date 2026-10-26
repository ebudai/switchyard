#!/usr/bin/env python3
"""The decisions inside an image handoff, one at a time (SYRD-247).

`image_handoff_two_identity_test` runs the whole crossing with real accounts.
These cases pin each refusal and limit on its own, with the clipboard, the
grant, the sender and the clock stood in for:

* the owner's command: which clipboard tool answers, the bounded read, which
  types count as an image, whose policy proves the caller, and that a failure
  part-way leaves nothing behind;
* the owner's directories and sweep: what is refused, and what expires;
* the listener's collector: what it refuses to deliver, when it waits, that it
  delivers once and only inside the window that guarantees retention, and that
  it can never stop the listener's own loop.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import pwd
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from standalone_test_runner import run_module_tests  # noqa: E402
from scripts import desktop_access, paste_image, team_launcher  # noqa: E402
from scripts.ticket_board import image_handoff  # noqa: E402

ME = pwd.getpwuid(os.geteuid())
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 300


def raises(kind, function, *args, **kwargs) -> str:
    try:
        function(*args, **kwargs)
    except kind as exc:
        return str(exc)
    raise AssertionError(f"{function.__name__} did not raise {kind.__name__}")


# --- the clipboard ------------------------------------------------------------


class Proc:
    def __init__(self, data: bytes, code: int = 0) -> None:
        self.stdout = io.BytesIO(data)
        self.code = code
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> int:
        return self.code

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def answering(answers: dict[str, tuple[int, str]]):
    asked: list[list[str]] = []

    def runner(argv, **_kwargs):
        asked.append(list(argv))
        if argv[0] not in answers:
            raise FileNotFoundError(argv[0])
        code, out = answers[argv[0]]
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")

    return runner, asked


def test_the_clipboard_is_read_with_whichever_tool_reaches_it() -> None:
    runner, asked = answering({"wl-paste": (1, ""), "xclip": (0, "TARGETS\nimage/png\n")})
    opened: list[list[str]] = []
    board = paste_image.Clipboard(runner=runner, opener=lambda argv: opened.append(list(argv)) or Proc(PNG))
    assert board.types() == ["TARGETS", "image/png"]
    assert [argv[0] for argv in asked] == ["wl-paste", "xclip"], asked
    # The bytes come from the tool that answered: under GNOME, xclip via XWayland.
    assert board.read("image/png") == PNG
    assert opened == [["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]], opened
    # And wl-paste first when it answers, as on a compositor with data-control.
    runner, asked = answering({"wl-paste": (0, "image/png\n")})
    board = paste_image.Clipboard(runner=runner, opener=lambda argv: opened.append(list(argv)) or Proc(PNG))
    board.types()
    board.read("image/png")
    assert asked == [["wl-paste", "--list-types"]] and opened[-1] == ["wl-paste", "--no-newline", "--type", "image/png"]


def test_no_clipboard_tool_is_a_refusal_naming_both() -> None:
    runner, _asked = answering({})
    message = raises(paste_image.PasteImageError, paste_image.Clipboard(runner=runner).types)
    assert "wl-paste or xclip" in message and "install xclip" in message, message


def test_the_read_stops_at_the_cap_rather_than_measuring_after() -> None:
    proc = Proc(b"y" * (300 * 1024))
    board = paste_image.Clipboard(opener=lambda _argv: proc)
    message = raises(paste_image.PasteImageError, board.read, "image/png", max_bytes=100 * 1024)
    assert proc.killed and "limit" in message, message
    # Only what was needed to cross the cap was read: one chunk past it.
    assert proc.stdout.tell() <= 100 * 1024 + 64 * 1024, proc.stdout.tell()
    failed = Proc(b"", code=1)
    assert "would not give up" in raises(paste_image.PasteImageError, paste_image.Clipboard(opener=lambda _a: failed).read, "image/png")


def test_only_an_image_type_a_provider_can_open_counts() -> None:
    assert paste_image.chosen_image_type(["text/plain", "image/jpeg", "image/png"]) == "image/png"
    assert paste_image.chosen_image_type(["image/jpeg", "image/webp"]) == "image/jpeg"
    assert paste_image.chosen_image_type(["image/webp"]) == "image/webp"
    message = raises(paste_image.PasteImageError, paste_image.chosen_image_type, ["text/plain", "image/svg+xml"])
    assert "no image on the clipboard" in message and "text/plain" in message, message
    assert "It is empty" in raises(paste_image.PasteImageError, paste_image.chosen_image_type, [])


# --- whose policy proves the caller -------------------------------------------


def _policy(project: str = "imgproj", *, gui: str = ME.pw_name, tenant: str = "root") -> dict:
    return desktop_access.generated_policy(project=project, tenant=tenant, gui_user=gui, approved_by=gui,
                                           approved_at="2026-10-12T00:00:00Z")


def _install(home: Path, policy: dict, *, uid: int = ME.pw_uid, tenant_uid: int = 0) -> Path:
    path = home / ".config/switchyard/desktop-access" / f"switchyard-desktop-{uid}-{tenant_uid}-{policy['project']}.json"
    desktop_access.atomic_json(path, policy)
    return path


def test_the_caller_must_have_installed_this_projects_policy_as_its_desktop_owner() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        message = raises(paste_image.PasteImageError, paste_image.approved_policy, "imgproj", uid=ME.pw_uid, home=home)
        assert "not approved a desktop grant for imgproj" in message, message
        _install(home, _policy("other"))
        message = raises(paste_image.PasteImageError, paste_image.approved_policy, "imgproj", uid=ME.pw_uid, home=home)
        assert "Your approved projects: other." in message, message
        # A project whose name ends another's is not matched by it.
        _install(home, _policy("x-imgproj"))
        assert "not approved" in raises(paste_image.PasteImageError, paste_image.approved_policy, "imgproj",
                                        uid=ME.pw_uid, home=home)
        path = _install(home, _policy())
        assert paste_image.approved_policy("imgproj", uid=ME.pw_uid, home=home)["tenant_user"] == "root"
        # Not trusted when another account could have written it.
        path.chmod(0o666)
        assert "not a policy file you own" in raises(paste_image.PasteImageError, paste_image.approved_policy,
                                                     "imgproj", uid=ME.pw_uid, home=home)
        path.chmod(0o600)
        # A policy naming someone else as the desktop owner proves nothing about the caller.
        _install(home, _policy(gui="root"))
        assert "does not name you" in raises(paste_image.PasteImageError, paste_image.approved_policy, "imgproj",
                                             uid=ME.pw_uid, home=home)
        # The tenant in the file must be the tenant its name says.
        path.unlink()
        _install(home, _policy(), tenant_uid=4242)
        assert "different tenant" in raises(paste_image.PasteImageError, paste_image.approved_policy, "imgproj",
                                            uid=ME.pw_uid, home=home)


def test_a_project_running_as_the_desktop_owner_is_refused() -> None:
    """Every role pane would be the caller, so this would be a clipboard read any role could make (SYRD-98)."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _install(home, _policy(tenant=ME.pw_name), tenant_uid=ME.pw_uid)
        message = raises(paste_image.PasteImageError, paste_image.approved_policy, "imgproj", uid=ME.pw_uid, home=home)
        assert "runs as your own account" in message, message


# --- the deposit ----------------------------------------------------------------


class Board:
    def __init__(self, data: bytes = PNG, types: list[str] | None = None) -> None:
        self.data, self.offered, self.reads = data, types or ["image/png"], 0

    def types(self) -> list[str]:
        return self.offered

    def read(self, mime: str, *, max_bytes: int) -> bytes:
        self.reads += 1
        return self.data


def _deposit_fixture(tmp: Path) -> tuple[Path, Path, list]:
    home, exposure = tmp / "home", tmp / "exposure"
    exposure.mkdir(mode=0o700)
    _install(home, _policy())
    # The approved grant's traverse entry on its exposure, as `expose` makes it.
    desktop_access.grant(exposure, 0, "--x")
    grants: list = []
    return home, exposure, grants


def test_a_deposit_writes_the_image_then_its_manifest_and_grants_each() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home, runtime, grants = _deposit_fixture(Path(tmp))
        seen: list[list[str]] = []

        def grant(path, uid, perms):
            seen.append(sorted(os.listdir(path.parent)))
            grants.append((path.name, uid, perms))
            return desktop_access.grant(path, uid, perms)

        result = paste_image.deposit("imgproj", "app", clipboard=Board(), uid=ME.pw_uid, home=home,
                                     locate=lambda _policy: runtime, grant=grant, clock=lambda: 1000.0,
                                     token=lambda: "ab" * 16)
        leaf = runtime / "paste"
        stem = "paste-" + "ab" * 16
        assert sorted(os.listdir(leaf)) == [f"{stem}.handoff.json", f"{stem}.png"]
        assert grants == [(f"{stem}.png", 0, "r--"), (f".tmp-{stem}.handoff.json", 0, "r--")], grants
        # The manifest takes its real name only after both are complete and granted.
        assert f"{stem}.handoff.json" not in seen[-1], seen
        record = json.loads((leaf / f"{stem}.handoff.json").read_text())
        assert record == {**{k: result[k] for k in record}, "image": f"{stem}.png", "role": "app", "bytes": len(PNG),
                          "sha256": hashlib.sha256(PNG).hexdigest(), "created_at": 1000.0,
                          "expires_at": 1000.0 + desktop_access.HANDOFF_TTL_SECONDS, "from": ME.pw_name}
        assert (leaf / f"{stem}.png").read_bytes() == PNG
        assert oct((leaf / f"{stem}.png").stat().st_mode & 0o777) == "0o640"  # 0600 plus the tenant's read, as mask
        assert desktop_access.effective_acl(leaf, 0) == 5 and desktop_access.effective_acl(leaf.parent, 0) == 1
        assert leaf.parent == runtime  # the exposure, never the owner's runtime directory (SYRD-687)


def test_a_failed_grant_leaves_nothing_behind() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home, runtime, _grants = _deposit_fixture(Path(tmp))
        calls = []

        def grant(path, uid, perms):
            calls.append(path.name)
            if len(calls) == 2:
                raise desktop_access.DesktopAccessError("setfacl refused")
            return desktop_access.grant(path, uid, perms)

        message = raises(paste_image.PasteImageError, paste_image.deposit, "imgproj", "app", clipboard=Board(),
                         uid=ME.pw_uid, home=home, locate=lambda _policy: runtime, grant=grant)
        assert "nothing was left behind" in message and "setfacl refused" in message, message
        assert os.listdir(runtime / "paste") == []


def test_nothing_is_read_or_written_for_a_bad_name_or_an_unapproved_caller() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home, runtime, _grants = _deposit_fixture(Path(tmp))
        board = Board()
        for project, role in (("imgproj", "../app"), ("Img proj", "app"), ("imgproj", "")):
            raises(paste_image.PasteImageError, paste_image.deposit, project, role, clipboard=board,
                   uid=ME.pw_uid, home=home, locate=lambda _policy: runtime)
        raises(paste_image.PasteImageError, paste_image.deposit, "unknown", "app", clipboard=board,
               uid=ME.pw_uid, home=home, locate=lambda _policy: runtime)
        empty = Board(data=b"")
        assert "was empty" in raises(paste_image.PasteImageError, paste_image.deposit, "imgproj", "app",
                                     clipboard=empty, uid=ME.pw_uid, home=home, locate=lambda _policy: runtime)
        assert board.reads == 0 and not (runtime / "paste").exists()


def test_the_command_entry_refuses_without_writing() -> None:
    lines: list[str] = []
    assert "usage: switchyard paste-image <project> <role>" in raises(SystemExit, paste_image.main, ["imgproj"])
    assert "usage:" in raises(SystemExit, paste_image.main, ["--help", "x"])
    assert "not approved a desktop grant" in raises(SystemExit, paste_image.main, ["no-such-project", "app"],
                                                    print_func=lines.append)
    assert lines == []
    # It is a verb the CLI has, and it runs as typed: escalated it would read
    # root's clipboard, and the policy that proves the caller is the caller's.
    assert "paste-image" in team_launcher.SWITCHYARD_COMMANDS
    assert "paste-image" in team_launcher.SWITCHYARD_UNPRIVILEGED_COMMANDS
    assert not team_launcher.switchyard_invocation_requires_root(["paste-image", "imgproj", "app"])
    assert "paste-image" in team_launcher.switchyard_help_text()


# --- the owner's directories and sweep -----------------------------------------


def test_the_handoff_directory_refuses_what_is_not_the_owners_own() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        exposure = Path(tmp) / "exposure"
        exposure.mkdir(mode=0o700)
        message = raises(desktop_access.DesktopAccessError, desktop_access.prepare_handoff_dir, exposure, 0)
        assert "approved desktop grant is not applied" in message, message
        desktop_access.grant(exposure, 0, "--x")
        target = Path(tmp) / "elsewhere"
        target.mkdir(mode=0o700)
        (exposure / "paste").symlink_to(target)
        assert "not a private directory" in raises(desktop_access.DesktopAccessError,
                                                   desktop_access.prepare_handoff_dir, exposure, 0)
        (exposure / "paste").unlink()
        (exposure / "paste").mkdir(mode=0o770)
        (exposure / "paste").chmod(0o770)
        assert "not a private directory" in raises(desktop_access.DesktopAccessError,
                                                   desktop_access.prepare_handoff_dir, exposure, 0)
        (exposure / "paste").chmod(0o700)
        assert "owned by uid 4242" in raises(desktop_access.DesktopAccessError, desktop_access._owned_directory,
                                             exposure / "paste", 4242, create=False)
        leaf = desktop_access.prepare_handoff_dir(exposure, 0)
        assert leaf == exposure / "paste" and os.listdir(target) == []
        assert desktop_access.effective_acl(leaf, 0) == 5


def test_the_sweep_removes_only_expired_handoff_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        exposure = Path(tmp)
        leaf = exposure / "paste"
        leaf.mkdir(mode=0o700)
        stem = "paste-" + "cd" * 16
        for name in (f"{stem}.png", f"{stem}.handoff.json", f".tmp-{stem}.handoff.json", "notes.txt"):
            (leaf / name).write_bytes(b"x")
            os.utime(leaf / name, (1000.0, 1000.0))
        (leaf / ("paste-" + "ef" * 16 + ".png")).symlink_to(leaf / "notes.txt")
        os.utime(leaf / ("paste-" + "ef" * 16 + ".png"), (1000.0, 1000.0), follow_symlinks=False)
        ttl = desktop_access.HANDOFF_TTL_SECONDS
        assert desktop_access.sweep_handoffs(exposure, now=1000.0 + ttl - 1) == []
        removed = desktop_access.sweep_handoffs(exposure, now=1000.0 + ttl)
        assert sorted(removed) == sorted([f"{stem}.png", f"{stem}.handoff.json", f".tmp-{stem}.handoff.json"]), removed
        assert sorted(os.listdir(leaf)) == ["notes.txt", "paste-" + "ef" * 16 + ".png"]
        # And an exposure with no handoff directory is not an error.
        assert desktop_access.sweep_handoffs(Path(tmp) / "elsewhere", now=10.0 ** 10) == []


def test_the_owners_access_service_sweeps_on_every_pass() -> None:
    """The reliable cleanup: the GUI owner's existing `watch` loop, not the next paste."""
    with tempfile.TemporaryDirectory() as tmp:
        exposure = Path(tmp) / "exposure"
        exposure.mkdir()
        policy_path = Path(tmp) / "policy.json"
        policy_path.write_text(json.dumps(_policy(tenant=ME.pw_name)))
        swept: list[Path] = []
        names = ("expose", "WaylandRelay", "refresh", "sweep_handoffs")
        original = {name: getattr(desktop_access, name) for name in names}
        original_sleep = desktop_access.time.sleep

        class Stop(Exception):
            pass

        class Relay:
            closed = False

            def __init__(self, _policy, _leaf):
                pass

            def close(self):
                Relay.closed = True

        def sleep(_seconds):
            raise Stop

        try:
            desktop_access.expose = lambda _policy: exposure
            desktop_access.WaylandRelay = Relay
            desktop_access.refresh = lambda *_a: None
            desktop_access.sweep_handoffs = lambda path: swept.append(path)
            desktop_access.time.sleep = sleep
            raises(Stop, desktop_access.main, ["watch", str(policy_path)])
        finally:
            for name, value in original.items():
                setattr(desktop_access, name, value)
            desktop_access.time.sleep = original_sleep
        assert swept == [exposure] and Relay.closed, swept


# --- the listener's collector ----------------------------------------------------


class Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, message, *args) -> None:
        self.lines.append(message % args if args else message)

    warning = info


def _handoff(leaf: Path, *, stem_hex: str = "12" * 16, role: str = "app", created: float = 1000.0,
             data: bytes = PNG, **overrides) -> str:
    stem = f"paste-{stem_hex}"
    (leaf / f"{stem}.png").write_bytes(data)
    record = {"schema": paste_image.MANIFEST_SCHEMA, "project": "imgproj", "role": role, "image": f"{stem}.png",
              "mime": "image/png", "bytes": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest(),
              "from": ME.pw_name, "created_at": created,
              "expires_at": created + desktop_access.HANDOFF_TTL_SECONDS, **overrides}
    (leaf / f"{stem}.handoff.json").write_text(json.dumps(record))
    return stem


def _collector(tmp: Path, clock: list[float], ledger: Path | None = None) -> tuple[image_handoff.ImageHandoffs, Path]:
    runtime = tmp / "exposure"
    leaf = runtime / "paste"
    leaf.mkdir(parents=True)
    handoffs = image_handoff.ImageHandoffs(project="imgproj", owner=ME.pw_name, owner_uid=ME.pw_uid,
                                           locate=lambda: runtime, ledger_path=ledger, clock=lambda: clock[0])
    return handoffs, leaf


TARGETS = {"app": "imgproj-app:0.0", "ops": "imgproj-ops:0.0"}


def test_the_collector_delivers_each_handoff_once_when_the_pane_is_idle() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        clock = [1001.0]
        ledger = Path(tmp) / "ledger.json"
        handoffs, leaf = _collector(Path(tmp), clock, ledger)
        stem = _handoff(leaf)
        sent: list[tuple[str, str]] = []
        log: list[str] = []
        busy = {"imgproj-app:0.0"}
        working = lambda target: target in busy  # noqa: E731
        # Busy: kept for a later pass.
        assert handoffs.deliver(targets=TARGETS, is_working=working, send=lambda *a: sent.append(a), log=log.append) == []
        busy.clear()
        assert handoffs.deliver(targets=TARGETS, is_working=working, send=lambda *a: sent.append(a), log=log.append) == [stem]
        assert len(sent) == 1 and sent[0][0] == "imgproj-app:0.0" and str(leaf / f"{stem}.png") in sent[0][1], sent
        assert handoffs.deliver(targets=TARGETS, is_working=working, send=lambda *a: sent.append(a), log=log.append) == []
        # A restarted listener reads what it delivered and does not send again.
        again = image_handoff.ImageHandoffs(project="imgproj", owner=ME.pw_name, owner_uid=ME.pw_uid,
                                            locate=lambda: leaf.parent, ledger_path=ledger, clock=lambda: clock[0])
        assert again.deliver(targets=TARGETS, is_working=lambda _t: False, send=lambda *a: sent.append(a), log=log.append) == []
        assert len(sent) == 1
        # Nothing is removed by the tenant's side.
        assert sorted(os.listdir(leaf)) == [f"{stem}.handoff.json", f"{stem}.png"]


def test_a_handoff_is_delivered_only_while_its_image_will_outlive_the_provider_opening_it() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        window = image_handoff.DELIVERY_WINDOW_SECONDS
        assert window + image_handoff.MIN_RETAINED_AFTER_DELIVERY == desktop_access.HANDOFF_TTL_SECONDS
        assert image_handoff.MIN_RETAINED_AFTER_DELIVERY >= 300
        clock = [1000.0 + window + 1]
        handoffs, leaf = _collector(Path(tmp), clock)
        stem = _handoff(leaf)
        sent, log = [], []
        assert handoffs.deliver(targets=TARGETS, is_working=lambda _t: False, send=lambda *a: sent.append(a),
                                log=log.append) == [] and sent == []
        assert any("not delivered in time" in line for line in log), log
        clock[0] = 1000.0 + window
        handoffs._reported.clear()
        assert handoffs.deliver(targets=TARGETS, is_working=lambda _t: False, send=lambda *a: sent.append(a),
                                log=log.append) == [stem]


def test_the_collector_refuses_what_the_owner_did_not_make_for_this_role() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        clock = [1001.0]
        handoffs, leaf = _collector(Path(tmp), clock)
        cases = {
            "unknown role": _handoff(leaf, stem_hex="a1" * 16, role="nobody"),
            "other project": _handoff(leaf, stem_hex="a2" * 16, project="elsewhere"),
            "foreign image": _handoff(leaf, stem_hex="a3" * 16, image="paste-" + "a1" * 16 + ".png"),
            # Same length, one byte changed: only the digest tells them apart.
            "tampered image": _handoff(leaf, stem_hex="a4" * 16, data=PNG[:-1] + b"y"),
            "wrong schema": _handoff(leaf, stem_hex="a5" * 16, schema="v0"),
        }
        # A manifest that is a link to a real one, and one in a name the owner never writes.
        os.symlink(leaf / f"{cases['unknown role']}.handoff.json", leaf / ("paste-" + "a6" * 16 + ".handoff.json"))
        good = _handoff(leaf, stem_hex="a7" * 16)
        # An image that is a link to the good one: its bytes match, and it is still refused.
        linked = _handoff(leaf, stem_hex="a8" * 16)
        (leaf / f"{linked}.png").unlink()
        os.symlink(leaf / f"{good}.png", leaf / f"{linked}.png")
        cases["linked image"] = linked
        (leaf / "paste-a7.handoff.json").write_text((leaf / f"{good}.handoff.json").read_text())
        sent, log = [], []
        assert handoffs.deliver(targets=TARGETS, is_working=lambda _t: False, send=lambda *a: sent.append(a),
                                log=log.append) == [good]
        refused = " ".join(log)
        for stem in [*cases.values(), "paste-" + "a6" * 16]:
            assert f"{stem} refused" in refused, (stem, log)
        assert len(sent) == 1 and sent[0][0] == TARGETS["app"]
        # Reported once each, not on every pass.
        handoffs.deliver(targets=TARGETS, is_working=lambda _t: False, send=lambda *a: sent.append(a), log=log.append)
        assert sum("refused" in line for line in log) == 7, log
        # Files the desktop owner did not make are refused, however well formed.
        stranger = image_handoff.ImageHandoffs(project="imgproj", owner="someone", owner_uid=ME.pw_uid + 1,
                                               locate=lambda: leaf.parent, clock=lambda: clock[0])
        log.clear()
        assert stranger.deliver(targets=TARGETS, is_working=lambda _t: False, send=lambda *a: sent.append(a),
                                log=log.append) == []
        assert any(f"{good} refused" in line and "desktop owner's" in line for line in log), log


def test_a_failed_send_is_retried_and_never_stops_the_listener() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        clock = [1001.0]
        handoffs, leaf = _collector(Path(tmp), clock)
        stem = _handoff(leaf)
        attempts: list[str] = []

        def send(target, message):
            attempts.append(target)
            if len(attempts) == 1:
                raise subprocess.CalledProcessError(1, ["directorctl"])

        log: list[str] = []
        assert handoffs.deliver(targets=TARGETS, is_working=lambda _t: False, send=send, log=log.append) == []
        assert handoffs.deliver(targets=TARGETS, is_working=lambda _t: False, send=send, log=log.append) == [stem]
        assert len(attempts) == 2 and any("will retry" in line for line in log), log

        class Listener:
            role_targets = TARGETS
            logger = Logger()
            sender = staticmethod(lambda *_a: None)

            @staticmethod
            def activity_gate(_target):
                raise RuntimeError("gate broke")

        Listener.image_handoffs = image_handoff.ImageHandoffs(
            project="imgproj", owner=ME.pw_name, owner_uid=ME.pw_uid, locate=lambda: leaf.parent,
            clock=lambda: clock[0])
        _handoff(leaf, stem_hex="99" * 16)
        assert image_handoff.run(Listener) == []
        assert any("image handoff pass failed: gate broke" in line for line in Listener.logger.lines)
        # A listener built without handoffs -- every tenant without a desktop -- does nothing.
        assert image_handoff.run(object()) == []


def test_the_message_stays_one_direct_line() -> None:
    """directorctl turns anything over 200 characters into a pointer to a file."""
    short = image_handoff.Handoff(stem="s", role="app", image=Path("/var/lib/switchyard/desktop/1000-1018-pie/paste/paste-" + "0" * 32 + ".png"),
                                  created_at=0.0, expires_at=0.0, owner="eric")
    assert len(short.message()) <= image_handoff.DIRECT_MESSAGE_MAX and short.message().startswith("Image from eric's")
    long = image_handoff.Handoff(stem="s", role="app", image=Path("/var/lib/switchyard/desktop/65534-65533-" + "p" * 40 + "/paste/paste-" + "0" * 32 + ".png"),
                                 created_at=0.0, expires_at=0.0, owner="desktop-operator")
    assert len(long.message()) <= image_handoff.DIRECT_MESSAGE_MAX and long.message().startswith("Image: /var/lib/switchyard/desktop/")
    assert "\n" not in short.message() + long.message()


def test_the_listener_reads_its_own_policy_and_nobody_elses() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry, config = Path(tmp) / "registry", Path(tmp) / "project.json"
        registry.mkdir()
        (registry / "imgproj.json").write_text(json.dumps({"slug": "imgproj", "config_path": str(config)}))
        env = {"SWITCHYARD_PROJECT_REGISTRY_DIR": str(registry), "XDG_RUNTIME_DIR": tmp}
        log: list[str] = []
        config.write_text(json.dumps({"desktop_access": {"mode": "headless"}}))
        assert image_handoff.from_environment("imgproj", environ=env, log=log.append) is None and log == []
        config.write_text(json.dumps({"desktop_access": _policy(gui="root", tenant="root")}))
        assert image_handoff.from_environment("imgproj", environ=env, log=log.append) is None
        assert "names root, not" in log[-1] or ME.pw_uid == 0, log
        config.write_text(json.dumps({"desktop_access": _policy(gui="root", tenant=ME.pw_name)}))
        handoffs = image_handoff.from_environment("imgproj", environ=env, log=log.append)
        assert handoffs.owner == "root" and handoffs.owner_uid == 0
        assert handoffs.ledger_path == Path(tmp) / "switchyard-image-handoffs-imgproj.json"
        assert image_handoff.from_environment("absent", environ=env, log=log.append) is None
        assert "image handoff off for absent" in log[-1], log


def test_shared_names_agree() -> None:
    """The owner's command, the owner's sweep and the listener read one set of names."""
    assert image_handoff.REGISTRY_DIR_ENV == team_launcher.SWITCHYARD_REGISTRY_DIR_ENV
    assert image_handoff.DEFAULT_REGISTRY_DIR == team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
    assert image_handoff.SCHEMA == paste_image.MANIFEST_SCHEMA
    assert image_handoff.MAX_IMAGE_BYTES == paste_image.MAX_IMAGE_BYTES
    stem = "paste-" + "0f" * 16
    for extension in paste_image.IMAGE_TYPES.values():
        assert desktop_access.HANDOFF_NAME.fullmatch(f"{stem}.{extension}"), extension
    assert desktop_access.HANDOFF_NAME.fullmatch(f"{stem}{paste_image.MANIFEST_SUFFIX}")
    assert image_handoff.MANIFEST_NAME.fullmatch(f"{stem}{paste_image.MANIFEST_SUFFIX}")
    assert image_handoff.MANIFEST_NAME.pattern.count("[0-9a-f]{32}") == 1 and len(paste_image.secrets.token_hex(16)) == 32


def test_the_listeners_loop_runs_a_handoff_pass() -> None:
    import ticket_board_notify_listener_test as lt
    from scripts.ticket_board import notify_listener as nl

    calls: list[object] = []

    class Recorder:
        def deliver(self, **kwargs):
            calls.append(kwargs)
            return []

    listener = nl.TicketBoardNotifyListener(conninfo="dbname=test", sender=lambda *_a: None,
                                            activity_gate=lambda _t: False,
                                            connector=lambda *a, **k: lt.FakeConnection([]), poll_seconds=0,
                                            target_exists=lambda _t: True, logger=logging.getLogger("syrd247"))
    listener.image_handoffs = Recorder()
    listener.listen_once(max_notifications=1)
    assert len(calls) == 1 and calls[0]["targets"] == listener.role_targets, calls
    assert calls[0]["is_working"] is listener.activity_gate and calls[0]["send"] is listener.sender


def main() -> int:
    run_module_tests(globals())
    print("image_handoff_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
