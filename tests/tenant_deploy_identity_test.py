#!/usr/bin/env python3
"""SYRD-138: who the printed release sequence actually runs the deploy as.

`switchyard upgrade <tenant>` prints a command sequence for an operator to run.
The deploy line decided whether to drop to the tenant owner by asking who was
RENDERING it -- `current_user_name()` -- for a command that somebody else runs
later, under the rollout recorder, as root. Rendered by root the line carried
`sudo -u <owner>`; rendered by the tenant owner it carried nothing at all, and
the same line then executed as root because the recorder is invoked with sudo.

What that cost is not theoretical. `ticket-board-service.sh` resolves its user
manager as `/run/user/$(id -u)`, so under root it addressed `/run/user/0`:
`stop_listener_for_upgrade` looked for the tenant's notification listener in
root's user manager, did not find it, and returned success having stopped
nothing. The migrations then ran with the real listener live -- the outage the
stop exists to prevent, reached by a path that reported success.

Two things are proved here, because either alone leaves the fault reachable:

* the rendering no longer depends on who renders. The boundary is a run-time
  decision carried in the command itself, so a sequence rendered unprivileged
  and one rendered by root are byte-identical and both land on the owner;
* the deploy script refuses root's user manager for a tenant root does not own,
  so any other caller that gets this wrong fails loudly instead of silently
  migrating around a listener it never stopped.
"""

from __future__ import annotations

import json
import os
import pwd
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher as tl  # noqa: E402
from scripts.ticket_board.rollout_journal import JOURNAL_ROOT_ENV, RESULT_NAME  # noqa: E402

SERVICE_SCRIPT = ROOT / "scripts" / "ticket-board-service.sh"
RECORDER = ROOT / "scripts" / "switchyard-record-rollout"
ME = pwd.getpwuid(os.getuid()).pw_name


def namespaces_available() -> bool:
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "true"], capture_output=True, text=True
    )
    return probe.returncode == 0


def status(**overrides: object) -> tl.TenantReleaseStatus:
    base = dict(
        board_root=Path("/home/owner/porter-ticketboard-live"),
        owner_user="owner",
        owner_home=Path("/home/owner"),
        provisioned_system_unit=Path("/etc/switchyard/provision/porter/porter-ticket-board.service"),
        commit_git_dir="/home/owner/cache.git",
        current_release=Path("/home/owner/porter-ticketboard-live/releases/aaa"),
        current_sha="a" * 40,
        target_sha="b" * 40,
        deploy_ref="b" * 40,
        source_repo=Path("/opt/switchyard/releases/" + "b" * 40),
        board_port="23326",
        board_socket="/run/porter-ticket-board/ticket-board.sock",
    )
    base.update(overrides)
    return tl.TenantReleaseStatus(**base)


def rendered_as(user: str, render) -> str:
    """Render exactly as the named account would have rendered it."""
    original = tl.current_user_name
    tl.current_user_name = lambda: user
    try:
        return render()
    finally:
        tl.current_user_name = original


def fake_sudo(directory: Path) -> tuple[Path, Path]:
    """A `sudo` that records how it was called and then does what it was asked.

    Not a stand-in for the real one: it is here so the drop can be OBSERVED.
    `sudo -u <name> <command>` is the contract being relied on, so it records
    the argv and then runs the command, and a test can assert both that the
    drop happened and that nothing about the command changed on the way.
    """
    log = directory / "sudo.log"
    binary = directory / "sudo"
    binary.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" >> "$SUDO_LOG"\n'
        'if [ "$1" = "-u" ]; then shift 2; fi\n'
        'exec "$@"\n',
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, log


def run_boundary(command: list[str], *, bin_dir: Path, log: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "SUDO_LOG": str(log),
        },
    )


# --------------------------------------------------------------------------
# the rendering, from both contexts
# --------------------------------------------------------------------------


def test_both_renderers_produce_the_same_deploy_command() -> None:
    """The defect, stated as the property it broke.

    Nothing about the tenant changes between these two calls. Only who is
    typing the command does -- and that is not a fact about the tenant, so it
    must not reach the output.
    """
    from_root = rendered_as("root", lambda: tl.tenant_release_deploy_command(status(), "porter"))
    from_owner = rendered_as("owner", lambda: tl.tenant_release_deploy_command(status(), "porter"))
    assert from_root == from_owner, (from_root, from_owner)
    assert "owner" in shlex.split(from_root), from_root
    # And the same for a tenant deployed from a bundled clone, which builds a
    # different command through the same boundary.
    clone = status(clone_source_repo=Path("/home/owner/cache.git"))
    assert rendered_as("root", lambda: tl.tenant_release_deploy_command(clone, "porter")) == rendered_as(
        "owner", lambda: tl.tenant_release_deploy_command(clone, "porter")
    )


def test_both_renderers_produce_the_same_listener_commands() -> None:
    """An operator pastes these into the same shell as the deploy.

    A `systemctl --user stop` that reaches root's manager stops nothing and
    reports success, which is the same failure with a shorter command.
    """
    for action in ("stop", "start"):
        from_root = rendered_as(
            "root", lambda action=action: tl.tenant_release_listener_command(status(), "porter", action)
        )
        from_owner = rendered_as(
            "owner", lambda action=action: tl.tenant_release_listener_command(status(), "porter", action)
        )
        assert from_root == from_owner, (action, from_root, from_owner)
        assert "owner" in shlex.split(from_root), (action, from_root)


def test_the_boundary_runs_directly_when_it_is_already_the_owner() -> None:
    """No sudo where none is needed: the unprivileged path still works."""
    with tempfile.TemporaryDirectory(prefix="syrd138-own.") as raw:
        bin_dir = Path(raw)
        _binary, log = fake_sudo(bin_dir)
        command = tl._owner_boundary_env_args(ME, Path.home(), ["sh", "-c", "id -un"])
        done = run_boundary(command, bin_dir=bin_dir, log=log)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == ME, done.stdout
        assert not log.exists(), log.read_text(encoding="utf-8")


def test_the_boundary_drops_to_the_owner_when_it_is_somebody_else() -> None:
    """The branch the recorder always takes, because the recorder is root."""
    with tempfile.TemporaryDirectory(prefix="syrd138-drop.") as raw:
        bin_dir = Path(raw)
        _binary, log = fake_sudo(bin_dir)
        command = tl._owner_boundary_env_args(
            "tenant-owner", Path("/home/tenant-owner"), ["sh", "-c", "echo ran"]
        )
        done = run_boundary(command, bin_dir=bin_dir, log=log)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "ran", done.stdout
        recorded = log.read_text(encoding="utf-8").splitlines()
        assert recorded[:2] == ["-u", "tenant-owner"], recorded
        # The command itself is unchanged by the drop: same env, same argv.
        assert recorded[2] == "env", recorded
        assert "HOME=/home/tenant-owner" in recorded, recorded


def test_tenant_paths_survive_the_boundary_intact() -> None:
    """Arbitrary valid tenant paths, which is what quoting is for.

    A space, a single quote and a dollar sign in one path: nothing here is
    interpolated into the boundary script, so all three arrive as data.
    """
    awkward = Path("/home/o'wner/a dir/$NOT_A_VAR")
    rendered = tl.tenant_release_deploy_command(
        status(owner_home=awkward, board_root=awkward / "live", source_repo=awkward / "rel"),
        "porter",
    )
    argv = shlex.split(rendered)
    assert f"HOME={awkward}" in argv, argv
    assert f"BOARD_ROOT={awkward / 'live'}" in argv, argv
    assert str(awkward / "rel" / "scripts" / "ticket-board-service.sh") in argv, argv

    with tempfile.TemporaryDirectory(prefix="syrd138-quote.") as raw:
        bin_dir = Path(raw)
        _binary, log = fake_sudo(bin_dir)
        command = tl._owner_boundary_env_args(
            "tenant-owner", awkward, ["sh", "-c", 'printf "%s\\n" "$0"', str(awkward)]
        )
        done = run_boundary(command, bin_dir=bin_dir, log=log)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == str(awkward), done.stdout


def test_only_the_journal_wrapper_is_privileged_in_the_printed_sequence() -> None:
    """The shape the acceptance asks for, read off the sequence itself.

    Driven through the real reporter rather than by re-composing the line here,
    because the composition IS what was wrong: the recorder ran as root and the
    step inside it inherited that.
    """
    printed = printed_sequence(status())
    steps = [line.strip() for line in printed if line.startswith("  ")]
    assert steps, printed
    deploy = [step for step in steps if "deploy-restart" in step]
    assert len(deploy) == 1, steps
    argv = shlex.split(deploy[0])
    # Privileged on the outside...
    assert argv[0] == "sudo", argv[:3]
    assert Path(argv[1]).name == "switchyard-record-rollout", argv[:3]
    # ...and the step it records drops to the owner on the inside.
    inner = argv[-1]
    assert "sudo -u" not in inner.split("exec ", 1)[0], inner
    assert shlex.split(inner)[0] == "sh", inner
    assert "owner" in shlex.split(inner), inner
    assert "ticket-board-service.sh" in inner, inner
    # The listener steps are the operator's own lines -- unrecorded, because
    # they change no release -- and they carry the boundary too.
    listener_steps = [step for step in steps if "systemctl --user" in step]
    assert len(listener_steps) == 2, steps
    for step in listener_steps:
        assert shlex.split(step)[0] == "sh", step
        assert "owner" in shlex.split(step), step
        assert "switchyard-record-rollout" not in step, step


def printed_sequence(release: tl.TenantReleaseStatus) -> list[str]:
    printed: list[str] = []
    original = tl.tenant_release_status
    tl.tenant_release_status = lambda *args, **kwargs: release
    try:
        tl.report_tenant_release_upgrade(
            types.SimpleNamespace(project="porter"),
            config_path=Path("/nonexistent/porter.json"),
            print_func=printed.append,
        )
    finally:
        tl.tenant_release_status = original
    return printed


def test_a_tenant_writable_unit_is_never_handed_to_root() -> None:
    """Acceptance (5): the step root runs must not read what the tenant writes.

    An unprivileged render points the unit-install step at the tenant's own
    provision directory, and the step is executed by root through the recorder.
    A warning above it is not a control: on SYRD-137 it had to be recognised
    and skipped by hand, and the copy it would have installed was four days
    stale and would have stripped the live board's socket-group confinement.
    """
    tenant_owned = status(
        provisioned_system_unit=Path("/home/owner/.switchyard/provision/porter-ticket-board.service")
    )
    printed = printed_sequence(tenant_owned)
    steps = [line.strip() for line in printed if line.startswith("  ")]
    assert not [step for step in steps if "install units" in step], steps
    assert any("omitting the unit-install step" in line for line in printed), printed
    assert any("as root to stage a root-owned copy" in line for line in printed), printed
    # The deploy is still offered: it reads the root-published readable copy,
    # not the tenant's directory, so it is not the step at risk.
    assert [step for step in steps if "deploy-restart" in step], steps

    # And a root-staged unit still gets its step, because that source is root's.
    root_owned = printed_sequence(status())
    assert [line for line in root_owned if "install units" in line], root_owned
    assert not any("omitting the unit-install step" in line for line in root_owned), root_owned


def test_provisioning_and_upgrade_agree_on_who_runs_the_deploy() -> None:
    """Fresh provisioning was already right; the upgrade had drifted from it."""
    from scripts.ticket_board import project_provision

    rendered = tl.tenant_release_deploy_command(status(), "porter")
    assert "ticket-board-service.sh" in rendered
    # Provisioning names the owner on the deploy line, unconditionally, and has
    # since it was written. That is the semantics the upgrade now matches.
    source = (ROOT / "scripts" / "ticket_board" / "project_provision.py").read_text(encoding="utf-8")
    deploy_line = [
        line for line in source.splitlines()
        if "q_deploy_script" in line and "sudo -u" in line
    ]
    assert deploy_line, "provisioning must still run the deploy script as the tenant owner"
    assert hasattr(project_provision, "render_operator_commands")


# --------------------------------------------------------------------------
# the script, which must not be able to address /run/user/0 for a tenant
# --------------------------------------------------------------------------


PROBE = """
set -u
source "$1/scripts/ticket-board-service.sh"
if out="$(runtime_dir)"; then printf 'runtime=%s\\n' "$out"; else printf 'refused\\n'; fi
"""


def probe_runtime_dir(*, owner_home: str, board_root: str, as_root: bool) -> subprocess.CompletedProcess[str]:
    prefix = ["unshare", "--user", "--map-root-user"] if as_root else []
    return subprocess.run(
        [*prefix, "bash", "-c", PROBE, "probe", str(ROOT)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "TICKET_BOARD_COMMIT_GIT_DIR": "/nonexistent/commits.git",
            "TICKET_BOARD_OWNER_HOME": owner_home,
            "BOARD_ROOT": board_root,
        },
    )


def test_the_deploy_script_refuses_roots_user_manager_for_a_tenant() -> None:
    """The half a renderer cannot guarantee, proved by running it.

    Inside a user namespace this process is uid 0, and a path owned by any
    other account reads as a non-root owner -- which is exactly the shape that
    resolved `/run/user/0` on the live host.
    """
    if not namespaces_available():
        return
    # A tenant root does not own: refused, and it says whose it is.
    refused = probe_runtime_dir(owner_home="/usr", board_root="/usr/share", as_root=True)
    assert "refused" in refused.stdout, (refused.stdout, refused.stderr)
    assert "/run/user/0" not in refused.stdout, refused.stdout

    with tempfile.TemporaryDirectory(prefix="syrd138-tenant.") as raw:
        mine = Path(raw)
        # A tenant this uid owns, inside the namespace where this uid is root:
        # root really is the owner, so nothing is refused.
        allowed = probe_runtime_dir(owner_home=str(mine), board_root=str(mine), as_root=True)
        assert allowed.stdout.strip() == "runtime=/run/user/0", (allowed.stdout, allowed.stderr)
        # And the ordinary unprivileged deploy is untouched.
        ordinary = probe_runtime_dir(owner_home=str(Path.home()), board_root=str(mine), as_root=False)
        assert ordinary.stdout.strip() == f"runtime=/run/user/{os.getuid()}", ordinary.stdout


def test_a_root_deploy_for_a_tenant_stops_before_it_writes_anything() -> None:
    """The refusal reaches the operator as a failed deploy, not a silent skip."""
    if not namespaces_available():
        return
    done = subprocess.run(
        ["unshare", "--user", "--map-root-user", "bash", str(SERVICE_SCRIPT), "deploy-restart"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "TICKET_BOARD_COMMIT_GIT_DIR": "/nonexistent/commits.git",
            "TICKET_BOARD_OWNER_HOME": "/usr",
            "BOARD_ROOT": "/usr/share",
            "TICKET_BOARD_PROJECT": "porter",
        },
    )
    assert done.returncode != 0, done.stdout + done.stderr
    combined = done.stdout + done.stderr
    assert "refusing to drive root's user manager" in combined, combined
    assert "porter" in combined, combined


def fake_systemctl(directory: Path) -> tuple[Path, Path]:
    """A `systemctl` that records which user manager it was pointed at."""
    log = directory / "systemctl.log"
    binary = directory / "systemctl"
    binary.write_text(
        "#!/bin/sh\n"
        'printf "%s|%s\\n" "$*" "${XDG_RUNTIME_DIR:-}" >> "$SYSTEMCTL_LOG"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, log


LISTENER_PROBE = """
set -u
source "$1/scripts/ticket-board-service.sh"
stop_listener_for_upgrade
: >"$READY_FILE"
sleep 30
"""


def test_the_tenants_own_listener_is_the_one_stopped_and_put_back() -> None:
    """Acceptance (3), against the identity the deploy actually runs under.

    The stop and the restore already exist and are already a trap rather than a
    line (SYRD-136). What they were missing is the right user manager: under
    root they asked `/run/user/0` whether the tenant's listener was installed,
    were told no, and returned success having stopped nothing -- so the
    migrations ran with it live. Here the run is the tenant's own, and every
    call has to name the tenant's runtime directory.

    The interruption path is the one driven, because it is the one an operator
    reaches by pressing Ctrl-C through a Polkit prompt.
    """
    with tempfile.TemporaryDirectory(prefix="syrd138-listener.") as raw:
        tmp = Path(raw)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        _binary, log = fake_systemctl(bin_dir)
        ready = tmp / "ready"
        # Interrupted from outside, the way an operator's Ctrl-C arrives. A
        # shell signalling itself is not the same thing: bash still runs its
        # EXIT trap then, so a self-signal would pass with the INT handler
        # deleted -- which is the handler SYRD-136 added.
        probe = subprocess.Popen(
            ["bash", "-c", LISTENER_PROBE, "probe", str(ROOT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "SYSTEMCTL_LOG": str(log),
                "READY_FILE": str(ready),
                "TICKET_BOARD_COMMIT_GIT_DIR": "/nonexistent/commits.git",
                "TICKET_BOARD_OWNER_HOME": str(Path.home()),
                "BOARD_ROOT": str(tmp),
                "TICKET_BOARD_PROJECT": "porter",
            },
        )
        for _ in range(600):
            if ready.exists() or probe.poll() is not None:
                break
            time.sleep(0.05)
        assert ready.exists(), "the probe never reached the stop"
        probe.send_signal(signal.SIGINT)
        out, err = probe.communicate(timeout=60)
        done = subprocess.CompletedProcess(probe.args, probe.returncode, out, err)
        calls = [line.split("|") for line in log.read_text(encoding="utf-8").splitlines()]
        assert calls, done.stdout + done.stderr
        mine = f"/run/user/{os.getuid()}"
        for invocation, runtime in calls:
            assert runtime == mine, (invocation, runtime)
            assert "/run/user/0" not in runtime, invocation
        actions = [invocation for invocation, _runtime in calls]
        assert any(action.startswith("--user stop ") for action in actions), actions
        # Interrupted, and the listener is running again anyway.
        assert any(action.startswith("--user start ") for action in actions), actions
        assert done.returncode != 0, done.returncode


def test_pkexec_attribution_survives_the_privilege_drop() -> None:
    """The outer record still names the operator, which is the point of it.

    The drop happens inside the recorded step, so the recorder -- which is what
    polkit elevated -- resolves the operator from its own environment before
    anything drops. This runs the real recorder around a real boundary.
    """
    if not namespaces_available():
        return
    with tempfile.TemporaryDirectory(prefix="syrd138-record.") as raw:
        tmp = Path(raw)
        journal = tmp / "rollout"
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        _binary, log = fake_sudo(bin_dir)
        inner = tl._quote_command(
            tl._owner_boundary_env_args("tenant-owner", tmp, ["sh", "-c", "echo deployed"])
        )
        done = subprocess.run(
            [
                "unshare", "--user", "--map-root-user",
                sys.executable, str(RECORDER), "otto", "--label", "deploy-restart",
                "--", "bash", "-c", inner,
            ],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "SUDO_LOG": str(log),
                JOURNAL_ROOT_ENV: str(journal),
                "PKEXEC_UID": str(os.getuid()),
                "SUDO_USER": "",
            },
        )
        assert done.returncode == 0, done.stdout + done.stderr
        latest = sorted((journal / "otto").glob("[0-9]*-*"), key=lambda path: path.name)[-1]
        result = json.loads((latest / RESULT_NAME).read_text(encoding="utf-8"))
        assert result["operator"] == ME, result
        assert result["operator_source"] == "pkexec", result
        assert (result["status"], result["exit_status"]) == ("completed", 0), result
        # The step really did drop on its way through, rather than the record
        # being right about a command that never left root.
        assert log.read_text(encoding="utf-8").splitlines()[:2] == ["-u", "tenant-owner"], log.read_text()


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    if not namespaces_available():
        print("tenant_deploy_identity_test: user namespaces unavailable; script half skipped")
    print(f"tenant_deploy_identity_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
