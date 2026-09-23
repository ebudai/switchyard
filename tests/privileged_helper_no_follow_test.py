#!/usr/bin/env python3
"""The privileged helper's board URL comes from documents nobody could redirect.

Found during SYRD-228: `board_url_for` re-derives a project's board from
root-owned registration state rather than from argv -- "a caller that could name
the board could name one that would happily agree it is the Director" -- and
then read both documents with `Path.read_text`, which follows every symlink. The
registered configuration lives in a directory the tenant owns, so a link planted
there chose the file, and the `board_url` taken from it is what the helper
authorizes against (SYRD-242).

The unprivileged cases drive the real `board_url_for` and the real `main`
against real files. The privileged case builds what only root can: a registry
directory that belongs to somebody else, which is the shape the boundary exists
to refuse.
"""

from __future__ import annotations

import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from scripts.ticket_board import privileged_helper as ph  # noqa: E402

#: What a file root can read might hold, and what a diagnostic must never echo.
SECRET = "ticket-board-service-password-syrd242"
#: Where an attacker would like the helper to ask about its own authority.
ATTACKER_BOARD = "http://127.0.0.1:59999/attacker-board"


class Host:
    """A registered tenant, and the documents the helper reads to find its board."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="syrd242."))
        self.registry = self.tmp / "projects"
        self.registry.mkdir()
        self.provision = self.tmp / "checkout" / ".switchyard" / "provision"
        self.provision.mkdir(parents=True)
        self.config = self.provision / "mefp.json"
        self.config.write_text(
            json.dumps({"project": "mefp", "board_url": "http://127.0.0.1:23326/"}),
            encoding="utf-8",
        )
        self.record = self.registry / "mefp.json"
        self.record.write_text(
            json.dumps({"schema": "switchyard.project-registry.v1", "slug": "mefp",
                        "config_path": str(self.config)}),
            encoding="utf-8",
        )
        # The file an attacker would point the helper at, and one it must never
        # quote: a real host has plenty of both.
        self.attacker = self.tmp / "attacker.json"
        self.attacker.write_text(
            json.dumps({"project": "mefp", "board_url": ATTACKER_BOARD}), encoding="utf-8"
        )
        self.secret = self.tmp / "root-only.txt"
        self.secret.write_text(SECRET + "\n", encoding="utf-8")

    def url(self) -> str:
        return ph.board_url_for("mefp", registry_dir=self.registry)

    def refusal(self) -> str:
        try:
            self.url()
        except ph.Refused as exc:
            return str(exc)
        raise AssertionError("the read was accepted")

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def _check(host: Host, said: str, expected: str) -> None:
    assert expected in said, (expected, said)
    assert SECRET not in said, said
    assert ATTACKER_BOARD not in said, said
    assert "Traceback" not in said, said


def test_the_registered_tenant_is_read_normally() -> None:
    host = Host()
    try:
        assert host.url() == "http://127.0.0.1:23326/"
    finally:
        host.close()


def test_a_symlinked_registration_record_is_refused() -> None:
    host = Host()
    try:
        host.record.unlink()
        host.record.symlink_to(host.attacker)
        said = host.refusal()
        _check(host, said, "is a symlink")
        assert str(host.record) in said, said
    finally:
        host.close()


def test_a_symlinked_registry_directory_is_refused() -> None:
    host = Host()
    try:
        moved = host.tmp / "moved-projects"
        host.registry.rename(moved)
        host.registry.symlink_to(moved)
        said = host.refusal()
        _check(host, said, "is a symlink")
    finally:
        host.close()


def test_a_symlinked_tenant_configuration_is_refused() -> None:
    """The reported defect: the tenant chooses the file, and so the board URL."""
    host = Host()
    try:
        host.config.unlink()
        host.config.symlink_to(host.attacker)
        said = host.refusal()
        _check(host, said, "is a symlink")
        assert str(host.config) in said, said
        # The attacker's document is still exactly as it was: not read into a
        # message, not acted on.
        assert json.loads(host.attacker.read_text(encoding="utf-8"))["board_url"] == ATTACKER_BOARD
    finally:
        host.close()


def test_a_symlinked_ancestor_of_the_configuration_is_refused() -> None:
    host = Host()
    try:
        moved = host.tmp / "moved-provision"
        host.provision.rename(moved)
        host.provision.symlink_to(moved)
        said = host.refusal()
        _check(host, said, "symlink")
    finally:
        host.close()


def test_a_hard_linked_configuration_is_refused() -> None:
    host = Host()
    try:
        host.config.unlink()
        os.link(host.attacker, host.config)
        said = host.refusal()
        _check(host, said, "links")
    finally:
        host.close()


def test_a_configuration_anybody_can_rewrite_is_refused() -> None:
    host = Host()
    try:
        host.config.chmod(0o666)
        _check(host, host.refusal(), "which anybody in its group or beyond can write")
    finally:
        host.close()


def test_a_directory_anybody_can_fill_is_refused() -> None:
    host = Host()
    try:
        host.provision.chmod(0o777)
        said = host.refusal()
        _check(host, said, "so anybody in its group or beyond can replace what is in it")
        host.provision.chmod(0o755)
    finally:
        host.close()


def test_a_configuration_that_is_not_json_is_refused_without_quoting_it() -> None:
    host = Host()
    try:
        host.config.write_text(f"# {SECRET}\nnot json at all\n", encoding="utf-8")
        _check(host, host.refusal(), "is not JSON")
    finally:
        host.close()


def test_a_configuration_that_is_not_text_is_refused_without_quoting_it() -> None:
    host = Host()
    try:
        host.config.write_bytes(b"\x89PNG\r\n\x1a\n" + SECRET.encode())
        _check(host, host.refusal(), "is not UTF-8 text")
    finally:
        host.close()


def test_an_unregistered_project_is_still_reported_as_unregistered() -> None:
    host = Host()
    try:
        said = ""
        try:
            ph.board_url_for("not-a-tenant", registry_dir=host.registry)
        except ph.Refused as exc:
            said = str(exc)
        assert "not registered on this host" in said, said
    finally:
        host.close()


class _swapped:
    def __init__(self, module, **replacements):
        self.module = module
        self.replacements = replacements

    def __enter__(self):
        self.previous = {name: getattr(self.module, name) for name in self.replacements}
        for name, value in self.replacements.items():
            setattr(self.module, name, value)
        return self

    def __exit__(self, *exc):
        for name, value in self.previous.items():
            setattr(self.module, name, value)


class _StubInstall:
    def verify_installation(self, *_a, **_k):
        return []


def test_a_tampered_configuration_refuses_before_the_action_runs() -> None:
    """Not merely a wrong URL: nothing is dispatched and nothing is recorded."""
    host = Host()
    try:
        host.config.unlink()
        host.config.symlink_to(host.attacker)
        ran: list = []
        opened: list = []
        asked: list = []
        buffer = io.StringIO()
        # The real read, told only where this fixture's registry is -- the
        # default binds at definition time, so swapping the constant would not
        # reach it, and swapping the function would stop testing it.
        real_board_url_for = ph.board_url_for
        with _swapped(
            ph,
            privileged_install=_StubInstall(),
            caller_identity=lambda *a, **k: ph.CallerIdentity(4242, 99, 1006),
            board_url_for=lambda project, **k: real_board_url_for(
                project, registry_dir=host.registry
            ),
            require_registered_control_caller=lambda project, **k: asked.append(project),
            run_privileged_action=lambda *a, **k: ran.append(a) or 0,
            _attempt_factory=lambda *a, **k: opened.append(a),
        ), redirect_stderr(buffer):
            code = ph.main(
                ["deploy-release", "project=mefp",
                 "commit=1111111111111111111111111111111111111111"]
            )
        said = buffer.getvalue()
        assert code != 0, said
        assert ran == [], "an action ran after an unsafe read"
        assert opened == [], "an attempt was recorded after an unsafe read"
        assert asked == [], "the caller was authorised against a URL from an unsafe document"
        _check(host, said, "is a symlink")
    finally:
        host.close()


def test_the_helper_never_reads_a_document_by_following_it() -> None:
    """The source itself: no read_text on a path the tenant can aim (SYRD-242)."""
    body = (ROOT / "scripts" / "ticket_board" / "privileged_helper.py").read_text(encoding="utf-8")
    assert "read_text" not in body.split("def _uid_of", 1)[0], body[:200]
    assert "no_follow" in body, "the helper reads through the no-follow module"
    reader = (ROOT / "scripts" / "ticket_board" / "no_follow.py").read_text(encoding="utf-8")
    assert "team_launcher" not in reader.replace(
        "`team_launcher`", ""
    ), "the staged helper must not depend on team_launcher"
    for forbidden in ("read_text(", "open(str(", "json.load("):
        assert forbidden not in reader, forbidden


PRIVILEGED_PROBE = """
import json, os, sys
sys.path.insert(0, os.environ["SYRD242_ROOT"])
from pathlib import Path
from scripts.ticket_board import privileged_helper as ph

registry = Path(os.environ["SYRD242_REGISTRY"])
try:
    print(json.dumps({"url": ph.board_url_for("mefp", registry_dir=registry)}))
except ph.Refused as exc:
    print(json.dumps({"refused": str(exc)}))
"""


def _root_child(owner: str) -> None:
    """As root: the registry has to be root's, whoever else owns a lookalike."""
    account = pwd.getpwnam(owner)
    host = Host()
    try:
        env = {
            "PATH": "/usr/bin:/bin",
            "SYRD242_ROOT": str(ROOT),
            "SYRD242_REGISTRY": str(host.registry),
        }

        def ask() -> dict:
            result = subprocess.run(
                [sys.executable, "-c", PRIVILEGED_PROBE], env=env,
                capture_output=True, text=True, timeout=120,
            )
            assert result.returncode == 0, (result.stdout, result.stderr)
            return json.loads(result.stdout.strip().splitlines()[-1])

        # Root's own registry, the tenant's own configuration: the ordinary
        # shape, and it is read.
        for path in (host.tmp, host.registry, host.record):
            os.chown(path, 0, 0)
        for path in (host.tmp / "checkout", host.tmp / "checkout" / ".switchyard",
                     host.provision, host.config):
            os.chown(path, account.pw_uid, account.pw_gid)
        said = ask()
        assert said.get("url") == "http://127.0.0.1:23326/", said

        # A registry directory somebody else owns is not root's registry, even
        # though root can read every byte of it.
        os.chown(host.registry, account.pw_uid, account.pw_gid)
        said = ask()
        assert "rather than by root" in said.get("refused", ""), said
        os.chown(host.registry, 0, 0)

        # A configuration owned by a third account, in the tenant's own
        # directory: neither the owner's nor root's, and its owner can rewrite
        # it at will.
        os.chown(host.config, 4242, 4242)
        said = ask()
        assert "is owned by uid 4242" in said.get("refused", ""), said
        assert SECRET not in said.get("refused", ""), said
        print("  ok   the privileged registry boundary holds")
    finally:
        host.close()


def main() -> int:
    cases = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for case in cases:
        case()
        print(f"  ok   {case.__name__[5:].replace('_', ' ')}")
    owner = pwd.getpwuid(os.geteuid()).pw_name
    command = [sys.executable, str(Path(__file__).resolve()), "--root-child", owner]
    if os.geteuid() != 0:
        command = ["unshare", "--user", "--map-auto", "--map-root-user", *command]
    else:
        command[-1] = "www-data"
    subprocess.run(command, check=True)
    print(f"privileged_helper_no_follow_test: {len(cases)} tests plus the privileged case ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--root-child":
        _root_child(sys.argv[2])
    else:
        raise SystemExit(main())
