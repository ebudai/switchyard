#!/usr/bin/env python3
"""SYRD-699: board authority executes and attests one exact root release."""

from __future__ import annotations

import os
import getpass
import shlex
import http.server
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import tenant_release_deploy  # noqa: E402
from scripts.ticket_board import board_identity, release_units  # noqa: E402
from scripts.ticket_board.cli import _report_credentials  # noqa: E402
from scripts.ticket_board.project_provision import (  # noqa: E402
    build_plan,
    render_board_unit as render_legacy_board,
    render_canary_unit as render_legacy_canary,
)

SHA = "a" * 40


def plan(tmp: Path):
    return build_plan(project="p699", owner_user="tenant", owner_home=tmp / "home",
                      board_root=tmp / "tenant-board", asset_dir=tmp / "assets",
                      frame_dir=tmp / "frames", commit_git_dir=tmp / "cache.git")


def test_root_release_units_have_no_tenant_code_selector() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd699-units.") as raw:
        tmp = Path(raw)
        provision = plan(tmp)
        old_board = render_legacy_board(provision)
        old_canary = render_legacy_canary(provision)
        release = Path("/opt/switchyard/releases") / SHA
        board = release_units.render_board_unit(
            old_board, release=release, report_credentials=release_units.report_credentials_path(old_board)
        )
        canary = release_units.render_canary_unit(old_board, old_canary, release=release, port=26991)
        combined = board + canary
        assert "EnvironmentFile=" not in combined
        assert "SetCredential=board-report-credentials:" in board
        assert f"WorkingDirectory={release}" in board and f"WorkingDirectory={release}" in canary
        assert combined.count(f"-E -s -B {release}/scripts/ticket-board.py") == 2
        assert str(provision.board_current) not in combined
        assert "BOARD_CANARY_RELEASE_DIR" not in canary and "/bin/sh" not in canary
        assert "--port 26991" in canary
        # Every variable the exact-release render sets is one the attestation accepts, so the allowlist tracks it.
        words = [word for line in board.splitlines() if line.startswith("Environment=")
                 for word in shlex.split(line[len("Environment="):])]
        rendered_environment = board_identity._shown_environment(shlex.join(words), "p699-ticket-board.service")
        board_identity._trusted_environment(rendered_environment, release, "p699-ticket-board.service")
        assert rendered_environment["TICKET_BOARD_DIRECTORCTL"] == f"{release}/scripts/directorctl"
        assert f"/run/{provision.runtime_directory}-canary/frames" in canary
        # Replacing the tenant selector cannot change either root-selected argv.
        (tmp / "tenant-board").mkdir()
        (tmp / "tenant-board" / "current").symlink_to(tmp / "attacker")
        assert str(tmp / "attacker") not in combined


def test_legacy_environment_is_credential_data_only() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd699-credentials.") as raw:
        credential = Path(raw) / "board.env"
        credential.write_text(
            "TICKET_BOARD_TENANT_REPORT_TOKEN='report secret'\n"
            "TICKET_BOARD_REPORT_BINDING_KEY=binding\n"
            "PYTHONPATH=/tenant/payload\nLD_PRELOAD=/tenant/payload.so\n"
            "TICKET_BOARD_DATABASE_URL=postgresql:///attacker\n",
            encoding="utf-8",
        )
        assert _report_credentials(str(credential)) == ("report secret", "binding")


def test_failed_health_restores_captured_units_and_legitimate_upgrade_stays_selected() -> None:
    class Systemd:
        def __init__(self) -> None:
            self.restarts = 0

        def __call__(self, argv, **_kwargs):
            if argv[0] == "install":
                shutil.copyfile(argv[-2], argv[-1])
            elif argv[:2] == ["systemctl", "restart"]:
                self.restarts += 1
            assert argv[0] in {"install", "systemctl"}, argv
            return subprocess.CompletedProcess(argv, 0, "", "")

    with tempfile.TemporaryDirectory(prefix="syrd699-rollback.") as raw:
        root = Path(raw)
        board, canary = root / "board.service", root / "canary.service"
        previous = {board: b"old-board\n", canary: b"old-canary\n"}
        candidate = {board: b"new-board-exact-release\n", canary: b"new-canary-exact-release\n"}
        systemd = Systemd()
        assert tenant_release_deploy._install_units(candidate, runner=systemd) == []
        assert board.read_bytes() == candidate[board] and canary.read_bytes() == candidate[canary]
        # A legitimate health pass performs no restoration: the exact candidate remains installed.
        assert systemd.restarts == 0
        # The failed-health branch calls this same helper before it returns rolled_back.
        assert tenant_release_deploy._restore_units(previous, board.name, runner=systemd) == []
        assert board.read_bytes() == previous[board] and canary.read_bytes() == previous[canary]
        assert systemd.restarts == 1


class Runner:
    def __init__(self, output: str) -> None:
        self.output = output

    def __call__(self, argv, **_kwargs):
        assert argv[:3] == ["systemctl", "show", "p699-ticket-board.service"]
        return subprocess.CompletedProcess(argv, 0, self.output, "")


def test_effective_unit_and_live_argv_are_both_attested() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd699-attest.") as raw:
        tmp = Path(raw)
        release = tmp / "releases" / SHA
        script = release / "scripts" / "ticket-board.py"
        script.parent.mkdir(parents=True)
        script.write_text("# exact release\n", encoding="utf-8")
        python = tmp / "python3"
        python.write_text("# test interpreter\n", encoding="utf-8")
        python.chmod(0o755)
        argv = (str(python), "-E", "-s", "-B", str(script), "--unix-socket", "/run/p699/board.sock")
        proc = tmp / "proc" / "4242"
        proc.mkdir(parents=True)
        (proc / "cmdline").write_bytes(b"\0".join(value.encode() for value in argv) + b"\0")
        (proc / "cwd").symlink_to(release)
        environment = {"PYTHONUNBUFFERED": "1", "HOME": "/var/lib/p699-board",
                       "TICKET_BOARD_DIRECTORCTL": f"{release}/scripts/directorctl",
                       "TICKET_BOARD_PROJECT": "p699", "TICKET_BOARD_PROJECT_NAME": "Project 699"}
        # What systemd itself adds to a service's process, which the effective Environment never lists.
        systemd_added = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "INVOCATION_ID": "1" * 32,
                         "CREDENTIALS_DIRECTORY": "/run/credentials/p699-ticket-board.service"}

        def live_environment(**changes: str) -> None:
            live = {**environment, **systemd_added, **changes}
            (proc / "environ").write_bytes(b"".join(f"{key}={value}\0".encode() for key, value in live.items()))

        live_environment()
        shown_environment = " ".join(shlex.quote(f"{key}={value}") for key, value in environment.items())
        shown = ("User=boardsvc\nEnvironmentFiles=\nDropInPaths=\nMainPID=4242\nNeedDaemonReload=no\n"
                 f"PassEnvironment=\nWorkingDirectory={release}\nEnvironment={shown_environment}\n"
                 f"ExecStart={' '.join(argv)}\n")
        old_uid = board_identity.TRUSTED_OWNER_UID
        board_identity.TRUSTED_OWNER_UID = os.getuid()
        try:
            attested = board_identity.effective_unit(
                "p699", runner=Runner(shown), proc_root=tmp / "proc",
                trusted_release=lambda commit: str(release) if commit == SHA else "",
            )
            assert attested.argv == argv and attested.main_pid == 4242 and attested.release_commit == SHA

            for changed, expected in (
                (shown.replace("EnvironmentFiles=", "EnvironmentFiles=/home/tenant/board.env"), "EnvironmentFiles"),
                (shown.replace("DropInPaths=", "DropInPaths=/etc/systemd/system/x.d/y.conf"), "drop-ins"),
                (shown.replace(" -E -s -B ", " "), "-E -s -B"),
                # Audit's reproduction: valid argv, no EnvironmentFiles or drop-ins, but code chosen inline.
                (shown.replace(f"WorkingDirectory={release}", "WorkingDirectory=/home/tenant/payload"),
                 "WorkingDirectory"),
                (shown.replace("\nEnvironment=", "\nEnvironment=LD_PRELOAD=/home/tenant/payload.so "),
                 "LD_PRELOAD"),
                (shown.replace("\nEnvironment=", "\nEnvironment=PYTHONPATH=/home/tenant/payload "), "PYTHONPATH"),
                (shown.replace("\nEnvironment=", "\nEnvironment=TICKET_BOARD_PYTHON=/home/tenant/python "),
                 "TICKET_BOARD_PYTHON"),
                (shown.replace("\nEnvironment=", f"\nEnvironment=TICKET_BOARD_BUILD_ID={SHA} "), "TICKET_BOARD_BUILD_ID"),
                (shown.replace(f"{release}/scripts/directorctl", "/home/tenant/directorctl"),
                 "TICKET_BOARD_DIRECTORCTL"),
                (shown.replace("HOME=/var/lib/p699-board", "HOME=/home/tenant"), "HOME"),
                (shown.replace("PassEnvironment=", "PassEnvironment=LD_PRELOAD"), "PassEnvironment"),
                # Edited on disk but not reloaded: what systemd runs is no longer what the file says.
                (shown.replace("NeedDaemonReload=no", "NeedDaemonReload=yes"), "NeedDaemonReload"),
            ):
                try:
                    board_identity.effective_unit("p699", runner=Runner(changed), proc_root=tmp / "proc",
                                                  trusted_release=lambda _commit: str(release))
                except board_identity.BoardIdentityError as exc:
                    # Refused for the unit itself, not only because the live process then disagrees with it.
                    assert expected in str(exc) and "live process" not in str(exc), exc
                else:
                    raise AssertionError(f"accepted modified effective unit: {expected}")

            # The live process can predate a reload that changed only its environment or directory.
            for change, expected in (
                ({"LD_PRELOAD": "/home/tenant/payload.so"}, "LD_PRELOAD"),
                ({"PYTHONPATH": "/home/tenant/payload"}, "PYTHONPATH"),
                ({"TICKET_BOARD_DIRECTORCTL": "/home/tenant/directorctl"}, "TICKET_BOARD_DIRECTORCTL"),
                ({"TICKET_BOARD_BUILD_ID": SHA}, "TICKET_BOARD_BUILD_ID"),
            ):
                live_environment(**change)
                try:
                    board_identity.effective_unit("p699", runner=Runner(shown), proc_root=tmp / "proc",
                                                  trusted_release=lambda _commit: str(release))
                except board_identity.BoardIdentityError as exc:
                    assert expected in str(exc) and "live process" in str(exc), exc
                else:
                    raise AssertionError(f"accepted a live process carrying {expected}")
            live_environment()
            (proc / "cwd").unlink()
            (proc / "cwd").symlink_to(tmp / "payload")
            try:
                board_identity.effective_unit("p699", runner=Runner(shown), proc_root=tmp / "proc",
                                              trusted_release=lambda _commit: str(release))
            except board_identity.BoardIdentityError as exc:
                assert "does not run in its exact release" in str(exc), exc
            else:
                raise AssertionError("accepted a live process running outside its exact release")
            (proc / "cwd").unlink()
            (proc / "cwd").symlink_to(release)

            (proc / "cmdline").write_bytes(b"/tenant/sibling\0")
            try:
                board_identity.effective_unit("p699", runner=Runner(shown), proc_root=tmp / "proc",
                                              trusted_release=lambda _commit: str(release))
            except board_identity.BoardIdentityError as exc:
                assert "live process" in str(exc)
            else:
                raise AssertionError("accepted a same-uid sibling in place of the unit's live argv")

            (proc / "cmdline").write_bytes(b"\0".join(value.encode() for value in argv) + b"\0")
            script.chmod(0o666)
            try:
                board_identity.effective_unit("p699", runner=Runner(shown), proc_root=tmp / "proc",
                                              trusted_release=lambda _commit: str(release))
            except board_identity.BoardIdentityError as exc:
                assert "non-writable regular file" in str(exc)
            else:
                raise AssertionError("accepted a release program writable outside its trusted owner")
        finally:
            board_identity.TRUSTED_OWNER_UID = old_uid


def test_proven_peer_must_report_the_effective_release_build() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd699-peer-build.") as raw:
        root = Path(raw)
        units = root / "units"
        units.mkdir()
        socket_path = root / "board.sock"
        unit = units / "p699-ticket-board.service"
        unit.write_text(
            f"[Service]\nUser={getpass.getuser()}\n"
            f"ExecStart=/root/python -E -s -B /root/releases/{SHA}/scripts/ticket-board.py "
            f"--unix-socket {socket_path}\n",
            encoding="utf-8",
        )
        answer = {"build_id": SHA}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                payload = __import__("json").dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        server = socketserver.UnixStreamServer(str(socket_path), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        runtime = board_identity.EffectiveUnit(
            getpass.getuser(), ("/root/python", "-E", "-s", "-B",
                                f"/root/releases/{SHA}/scripts/ticket-board.py"),
            os.getpid(), SHA,
        )
        try:
            get = board_identity.board_get_for(
                "p699", unit_dir=units, owner_uid=os.getuid(), anchor=root,
                service_uid=lambda _user: os.getuid(), runtime_unit=lambda _project: runtime,
            )
            assert get("ignored", "/api/board")["build_id"] == SHA
            answer["build_id"] = "b" * 40
            try:
                board_identity.board_get_for(
                    "p699", unit_dir=units, owner_uid=os.getuid(), anchor=root,
                    service_uid=lambda _user: os.getuid(), runtime_unit=lambda _project: runtime,
                )
            except board_identity.BoardIdentityError as exc:
                assert "effective root release" in str(exc)
            else:
                raise AssertionError("accepted a proven peer reporting a different build")
        finally:
            server.shutdown()
            server.server_close()


def test_unit_rewriters_refuse_to_replace_an_exact_unit_before_changing_anything() -> None:
    import ast

    with tempfile.TemporaryDirectory(prefix="syrd700-rewrite.") as raw:
        tmp = Path(raw)
        provision = plan(tmp)
        legacy = render_legacy_board(provision)
        exact = release_units.render_board_unit(legacy, release=Path("/opt/switchyard/releases") / SHA,
                                                report_credentials=release_units.report_credentials_path(legacy))
        assert not release_units.is_exact_release_unit(legacy) and release_units.is_exact_release_unit(exact)
        # "This plan's unit", for the precheck: the template itself, or its exact render for the release it names.
        other = render_legacy_board(build_plan(project="p699", owner_user="tenant", owner_home=tmp / "home",
                                               board_root=tmp / "tenant-board", asset_dir=tmp / "assets",
                                               frame_dir=tmp / "frames", commit_git_dir=tmp / "other.git"))
        assert release_units.renders_template(legacy, legacy) and release_units.renders_template(exact, legacy)
        assert not release_units.renders_template(exact, other), "another plan's template"
        assert not release_units.renders_template(exact.replace("-E -s -B", "-E -s -B -X dev"), legacy), "edited"
        assert not release_units.renders_template(legacy + "# edited\n", legacy)
        units = tmp / "systemd"
        units.mkdir()
        old_dir = release_units.SYSTEM_UNIT_DIR
        release_units.SYSTEM_UNIT_DIR = units
        try:
            name = "p699-ticket-board.service"
            assert release_units.refuse_rewriting_exact_board_unit(name, "add-role") == "", "no unit installed"
            (units / name).write_text(legacy, encoding="utf-8")
            assert release_units.refuse_rewriting_exact_board_unit(name, "add-role") == "", "a legacy unit stays as is"
            (units / name).write_text(exact, encoding="utf-8")
            refused = release_units.refuse_rewriting_exact_board_unit(name, "add-role")
            assert "add-role would replace" in refused and "Nothing was changed" in refused, refused
        finally:
            release_units.SYSTEM_UNIT_DIR = old_dir

    # Each command asks before its first write, so a refusal leaves the project as it was.
    for module, command, first_write in (
        ("project_role_add.py", "add_project_role_command", "_write_added_role_config"),
        ("project_vcs_close_role.py", "set_project_vcs_close_role_command", "_write_vcs_close_role_artifacts"),
    ):
        tree = ast.parse((ROOT / "scripts" / module).read_text(encoding="utf-8"))
        node = next(n for n in tree.body if getattr(n, "name", None) == command)
        calls = sorted((n for n in ast.walk(node) if isinstance(n, ast.Call)), key=lambda n: (n.lineno, n.col_offset))
        names = [ast.unparse(n.func).rsplit(".", 1)[-1] for n in calls]
        assert "refuse_rewriting_exact_board_unit" in names and first_write in names, (module, names)
        assert names.index("refuse_rewriting_exact_board_unit") < names.index(first_write), (module, names)


def test_the_operator_packet_installs_root_renders_of_the_release_it_provisions() -> None:
    from scripts.ticket_board.provision_operator_packet import render_operator_commands

    with tempfile.TemporaryDirectory(prefix="syrd700-packet.") as raw:
        tmp = Path(raw)
        provision = plan(tmp)
        board_template, canary_template = tmp / provision.board_unit, tmp / provision.canary_unit
        board_template.write_text(render_legacy_board(provision), encoding="utf-8")
        canary_template.write_text(render_legacy_canary(provision), encoding="utf-8")
        out = tmp / "exact"
        out.mkdir()
        release = Path("/opt/switchyard/releases") / SHA
        # As the packet runs it: the release's own renderer, isolated, with no Switchyard import.
        done = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts" / "ticket_board" / "release_units.py"),
                               str(board_template), str(canary_template), str(release), str(out)],
                              capture_output=True, text=True, check=False)
        assert done.returncode == 0, done.stderr
        board = (out / provision.board_unit).read_text(encoding="utf-8")
        canary = (out / provision.canary_unit).read_text(encoding="utf-8")
        assert release_units.is_exact_release_unit(board) and release_units.renders_template(board, render_legacy_board(provision))
        assert "/bin/sh" not in canary and "EnvironmentFile=" not in canary and f"{release}/scripts/ticket-board.py" in canary

        commands = render_operator_commands(provision).splitlines()
        exact_at = next(i for i, line in enumerate(commands) if "release_units.py" in line)
        assert commands[exact_at].startswith("sudo /usr/bin/python3 -I -B "), commands[exact_at]
        assert commands[exact_at + 1] == f'system_unit_candidate="$exact_units/{provision.board_unit}"'
        assert commands[exact_at + 2] == f'canary_unit_candidate="$exact_units/{provision.canary_unit}"'
        # The readable copy and both installs come after, so all three are root's renders; the staged ones never are.
        installs = [i for i, line in enumerate(commands) if line.startswith("sudo install -m 0644 \"$")
                    and "_unit_candidate" in line]
        assert len(installs) == 2 and min(installs) > exact_at, installs
        assert not any(f'$provision_dir/{provision.board_unit}" ' in line and "install" in line for line in commands)
        migrate = next(line for line in commands if "ticket-board-migrate" in line)
        assert f"{provision.source_repo}/scripts/ticket-board-migrate" in migrate and str(provision.board_current) not in migrate


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"root_release_board_authority_test: {len(tests)} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
