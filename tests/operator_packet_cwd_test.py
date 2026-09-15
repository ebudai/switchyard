#!/usr/bin/env python3
"""SYRD-149: the privileged packet found its own artifacts only from one directory.

The SYRD-147 recovery regenerated `/etc/switchyard/provision/testing/operator-commands.sh`
and SYRD-146 ran it by absolute path through the rollout journal. It failed with
`install: cannot stat testing-ticket-board.conf`: the root-owned packet named
the artifacts beside it by bare file name, and a bare name is resolved against
whatever directory the caller happened to be in.

That is a real defect rather than an operator mistake. A packet run from a
journal, from a Polkit transaction, or by an operator standing anywhere at all
has no promise about its working directory, and the instructions that told
somebody to `cd` first are what made the dependency look like a convention.

So this suite runs a generated packet the way that run did: by absolute path,
from a directory that has nothing to do with it -- and it runs the whole packet
rather than reading it, because the failure was a real command looking in a
real directory.

Nothing privileged happens. The run gets a PATH holding one directory of stubs
and nothing else, so a command with no stub is `command not found` and the run
fails rather than reaching the host's copy: the safety here is enforced by the
kernel, not by parsing the script. Two stubs are not stubs at all -- `install`
and `cat` look for their source and say `cannot stat` when it is not there,
which is the whole question this suite asks. The run also gets its own user and
mount namespace, so the user bus it waits for and the root-owned role tooling
directory it stages into are this test's, not the host's.
"""

from __future__ import annotations

import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.ticket_board.project_provision import (  # noqa: E402
    build_plan,
    render_operator_commands,
    write_artifacts,
)

PROJECT = "testing"
TENANT = "testing-agent"

#: Everything the packet may invoke, each replaced by something that records
#: instead of acting. `install` and `cat` are deliberately absent: they are the
#: two that have to notice a missing source.
NEUTRALISED = (
    "sudo", "env", "getent", "useradd", "groupadd", "usermod", "chown", "chmod",
    "setfacl", "find", "systemctl", "systemd-tmpfiles", "visudo", "loginctl",
    "psql", "curl", "tee", "mv", "ssh-keygen", "git", "seq", "sleep", "id",
    "sha256sum", "stat", "mkdir", "printf", "echo", "grep", "test", "gpasswd",
    "groupmod", "chgrp", "setpriv", "cp", "ln", "touch", "su", "runuser",
    "openssl", "base64", "tar", "rsync", "xargs", "chsh", "getfacl",
)
#: Commands that only compute, provided for real. Two of them are how the
#: packet works out where it is, so stubbing those would break the resolution
#: this suite exists to check.
PASSED_THROUGH = ("sh", "bash", "dirname", "pwd", "mktemp", "rm", "cut", "sed", "tr", "head", "date", "readlink", "awk")


def stub_dir(tmp: Path, log: Path) -> Path:
    """A PATH where nothing acts, except the two commands under test."""
    bin_dir = tmp / "stub-bin"
    bin_dir.mkdir()
    for name in NEUTRALISED:
        script = bin_dir / name
        if name == "sudo":
            # Drops sudo's own options and runs the rest through this same PATH,
            # so what the packet asks `install` to do still reaches the stub.
            script.write_text(
                "#!/bin/sh\n"
                'while [ "$#" -gt 0 ]; do\n'
                '  case "$1" in\n'
                '    -u|-g) shift 2 ;;\n'
                '    -H|-E|-n) shift ;;\n'
                '    -v) exit 0 ;;\n'
                '    --) shift; break ;;\n'
                '    -*) shift ;;\n'
                '    *) break ;;\n'
                "  esac\n"
                "done\n"
                '[ "$#" -gt 0 ] || exit 0\n'
                'exec "$@"\n',
                encoding="utf-8",
            )
        elif name == "getent":
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        elif name == "id":
            # The sandbox has one user -- the caller, mapped to root by the
            # namespace -- and the packet asks for the owner's uid to find
            # their runtime directory. Answering for that one user is the
            # truthful answer here.
            script.write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="utf-8")
        elif name == "curl":
            # The packet's last step is the board health check. Failing it once
            # is how this suite stops a run after everything before it has
            # already been done -- a partially completed packet, which is the
            # state journal attempt 0007 left behind.
            script.write_text(
                "#!/bin/sh\n"
                'printf "curl %s\\n" "$*" >> "$PACKET_LOG"\n'
                'if [ -n "$PACKET_FAIL_ONCE" ] && [ ! -e "$PACKET_FAIL_ONCE" ]; then\n'
                '  : > "$PACKET_FAIL_ONCE"\n'
                '  echo "curl: (7) Failed to connect" >&2\n'
                "  exit 7\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
        elif name in {"printf", "echo"}:
            script.write_text('#!/bin/sh\nexit 0\n', encoding="utf-8")
        else:
            script.write_text(
                "#!/bin/sh\n" f'printf "%s %s\\n" {name} "$*" >> "$PACKET_LOG"\nexit 0\n',
                encoding="utf-8",
            )
        script.chmod(0o755)

    # The two that must notice a missing source, because that is the failure.
    install = bin_dir / "install"
    install.write_text(
        "#!/bin/sh\n"
        'printf "install %s\\n" "$*" >> "$PACKET_LOG"\n'
        '# `install -d` creates a directory; it reads nothing, so there is no\n'
        '# source for it to fail to find.\n'
        'for word in "$@"; do [ "$word" = "-d" ] && exit 0; done\n'
        "args=\"\"\n"
        'while [ "$#" -gt 1 ]; do\n'
        '  case "$1" in\n'
        '    -m|-o|-g) shift 2 ;;\n'
        '    -d) shift ;;\n'
        '    -*) shift ;;\n'
        '    *) args="$args $1"; shift ;;\n'
        "  esac\n"
        "done\n"
        'for source in $args; do\n'
        '  if [ ! -e "$source" ]; then\n'
        '    echo "install: cannot stat $source" >&2\n'
        "    exit 1\n"
        "  fi\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    install.chmod(0o755)
    cat = bin_dir / "cat"
    cat.write_text(
        "#!/bin/sh\n"
        'for source in "$@"; do\n'
        '  case "$source" in -*) continue ;; esac\n'
        '  if [ ! -e "$source" ]; then\n'
        '    echo "cat: $source: No such file or directory" >&2\n'
        "    exit 1\n"
        "  fi\n"
        "done\n"
        'printf "cat %s\\n" "$*" >> "$PACKET_LOG"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    cat.chmod(0o755)
    for name in PASSED_THROUGH:
        real = shutil.which(name)
        if real:
            (bin_dir / name).symlink_to(real)
    return bin_dir


def rendered_packet(tmp: Path) -> tuple[Path, Path]:
    """The real artifacts, written where root would install them."""
    provision = tmp / "provision" / PROJECT
    provision.mkdir(parents=True)
    plan = build_plan(
        project=PROJECT,
        owner_user=TENANT,
        owner_home=tmp / "home" / TENANT,
        source_repo=ROOT,
        control_user="an-operator",
    )
    # Role accounts and a named controller so the packet renders every branch
    # that installs one of its own artifacts -- the role control interface and
    # the lifecycle bridge among them. A branch that does not render is a
    # companion reference this suite would not look at.
    plan = replace(
        plan,
        role_accounts=(("director", f"{PROJECT}-director"), ("main", f"{PROJECT}-main")),
        roles_group=f"{PROJECT}-roles",
    )
    write_artifacts(plan, provision)
    packet = provision / "operator-commands.sh"
    # Stand in for what the deploy step leaves behind. Those commands are
    # stubbed, so the release they would have checked out has to exist for the
    # steps after them: the packet reads its schema and runs its migrate script
    # by absolute path. Those are not companions -- they are the release -- and
    # every one the packet names is created here rather than listed, so a new
    # reference cannot quietly go unexercised.
    body = packet.read_text(encoding="utf-8")
    release = str(plan.board_current)
    for reference in sorted(set(re.findall(re.escape(release) + r"/[A-Za-z0-9_./-]+", body))):
        target = Path(reference)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            continue
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
    # Every path under the root-owned role tooling directory the packet touches.
    # The packet stages these itself, from the installed release, in steps this
    # suite stubs out -- so they are created in the sandbox instead, on a tmpfs
    # that keeps the host's copy untouched.
    (tmp / "prestage.txt").write_text(
        "\n".join(
            sorted(set(re.findall(r"/usr/local/lib/switchyard/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", body)))
        )
        + "\n",
        encoding="utf-8",
    )
    return provision, packet


#: The packet waits for the owner's systemd user bus before it enables their
#: listener, and that socket lives at an absolute path outside any fixture. So
#: the run happens in a user and mount namespace with a private `/run/user`,
#: where the socket can be created without touching the host's.
SANDBOX = r"""#!/bin/sh
set -e
mount -t tmpfs tmpfs /run/user
mkdir -p /run/user/0
python3 -c 'import socket, sys
sock = socket.socket(socket.AF_UNIX)
sock.bind(sys.argv[1])' /run/user/0/bus
# The role tooling root is real, and this run must not write a byte of it. A
# tmpfs over it inside this namespace is both the isolation and the fixture:
# what the packet's own staging steps would have put there is created below,
# because those steps are stubbed.
mount -t tmpfs tmpfs /usr/local/lib/switchyard
while IFS= read -r staged; do
    [ -n "$staged" ] || continue
    mkdir -p "$(dirname "$staged")"
    : > "$staged"
    chmod 0755 "$staged"
done < "$PACKET_PRESTAGE"
cd "$PACKET_CWD"
exec env PATH="$STUB_PATH" HOME="$PACKET_CWD" PACKET_LOG="$PACKET_LOG" \
    PACKET_FAIL_ONCE="$PACKET_FAIL_ONCE" SHELL=/bin/sh bash "$PACKET"
"""


def sandbox_runner(tmp: Path) -> Path:
    runner = tmp / "sandbox.sh"
    runner.write_text(SANDBOX, encoding="utf-8")
    runner.chmod(0o755)
    return runner


def run_packet(
    packet: Path, *, cwd: Path, bin_dir: Path, log: Path, runner: Path,
    prestage: Path, fail_once: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        {
            # The packet itself sees this directory and nothing else. A command
            # it runs that has no stub here is not silently the host's -- it is
            # `command not found`, and the run fails. That is the safety this
            # suite depends on, enforced by the kernel rather than by reading
            # the script.
            "STUB_PATH": str(bin_dir),
            "PACKET": str(packet),
            "PACKET_CWD": str(cwd),
            "PACKET_LOG": str(log),
            "PACKET_FAIL_ONCE": str(fail_once) if fail_once else "",
            "PACKET_PRESTAGE": str(prestage),
        }
    )
    return subprocess.run(
        ["unshare", "--user", "--map-root-user", "--mount", "/bin/sh", str(runner)],
        text=True,
        capture_output=True,
        env=env,
    )


def test_the_packet_finds_its_companions_from_an_unrelated_directory() -> None:
    """The live failure, and the fix, from a directory that owns none of this.

    `/` is the hostile case on purpose: nothing beside the packet is reachable
    by name from there, so every companion reference has to be resolved from
    the packet's own location or the run stops exactly where journal attempt
    0007 stopped.
    """
    with tempfile.TemporaryDirectory(prefix="syrd149-cwd.") as tmp:
        tmp_path = Path(tmp)
        provision, packet = rendered_packet(tmp_path)
        log = tmp_path / "packet.log"
        bin_dir = stub_dir(tmp_path, log)
        elsewhere = tmp_path / "unrelated"
        elsewhere.mkdir()
        runner = sandbox_runner(tmp_path)

        for cwd in (Path("/"), elsewhere):
            log.write_text("", encoding="utf-8")
            done = run_packet(
                packet, cwd=cwd, bin_dir=bin_dir, log=log, runner=runner,
                prestage=tmp_path / "prestage.txt",
            )
            assert done.returncode == 0, (cwd, done.stdout, done.stderr)
            assert "cannot stat" not in done.stderr, (cwd, done.stderr)

            # Every companion the packet installed or read came from beside it.
            recorded = log.read_text(encoding="utf-8")
            for name in (
                "testing-ticket-board.conf",
                "49-testing-ticket-board-deploy.rules",
                "testing-ticket-board-notify-listener.service",
                "testing-database.sql",
            ):
                assert f"{provision}/{name}" in recorded, (cwd, name, recorded[:600])


def test_a_stopped_packet_is_retried_by_running_it_again() -> None:
    """Acceptance 6: the retry after journal 0007 is the packet, run again.

    The first run here stops where a real one stopped -- at the end, on the
    board health check, with everything before it already done. The retry then
    starts from a different directory, because the operator running
    `switchyard resume-provision` stands wherever they stand, and it has to
    walk back over the completed work without tripping on it.
    """
    with tempfile.TemporaryDirectory(prefix="syrd149-retry.") as tmp:
        tmp_path = Path(tmp)
        provision, packet = rendered_packet(tmp_path)
        log = tmp_path / "packet.log"
        bin_dir = stub_dir(tmp_path, log)
        runner = sandbox_runner(tmp_path)
        elsewhere = tmp_path / "unrelated"
        elsewhere.mkdir()

        stopped = run_packet(
            packet, cwd=Path("/"), bin_dir=bin_dir, log=log, runner=runner,
            prestage=tmp_path / "prestage.txt",
            fail_once=tmp_path / "health-check-failed",
        )
        assert stopped.returncode != 0, stopped.stdout
        assert "Failed to connect" in stopped.stderr, stopped.stderr
        # It stopped at the health check, which means every step before it --
        # every companion install among them -- had already succeeded.
        assert "cannot stat" not in stopped.stderr, stopped.stderr

        again = run_packet(
            packet, cwd=elsewhere, bin_dir=bin_dir, log=log, runner=runner,
            prestage=tmp_path / "prestage.txt",
        )
        assert again.returncode == 0, (again.stdout, again.stderr)


def test_a_bare_companion_name_is_the_failure_it_was() -> None:
    """The reproduction, stated as what the packet must never contain again.

    Rendering is checked rather than only running, because a run proves one
    path through the script and this proves every line of it: a companion named
    without `$provision_dir` in front of it is a line that works from one
    directory and one directory only.
    """
    with tempfile.TemporaryDirectory(prefix="syrd149-shape.") as tmp:
        provision, packet = rendered_packet(Path(tmp))
        body = packet.read_text(encoding="utf-8")
        companions = sorted(
            entry.name for entry in provision.iterdir()
            if entry.is_file() and entry.name != packet.name
        )
    assert companions, provision
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "provision_dir=" in stripped:
            continue
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            continue
        # systemd takes unit NAMES, which are not files and are not looked up
        # in a directory. Everything else that names one of these is naming a
        # file, and a file needs to say where it is.
        if any(token == "systemctl" or token.endswith("/systemctl") for token in tokens):
            continue
        for token in tokens:
            assert token not in companions, (
                f"{token} is named without a directory, so this line only works "
                f"from one cwd: {stripped}"
            )


def test_companions_are_addressed_safely_for_any_slug() -> None:
    """Acceptance 3: the name is quoted and the directory expands.

    A generated slug is tame, but the helper is the one place this is decided
    and a companion whose name met the shell would be a companion somebody
    could choose.
    """
    # Imported here rather than at the top so that the rest of this suite
    # still runs against a tree that has no such helper -- which is how it
    # reproduces the behaviour it was written against.
    from scripts.ticket_board.project_provision import packet_companion

    rendered = packet_companion("awkward name'with quote.conf")
    assert rendered.startswith('"$provision_dir"/'), rendered
    tokens = shlex.split(f"install {rendered}")
    assert tokens[1] == "$provision_dir/awkward name'with quote.conf", tokens
    # A path is not a companion: companions live beside the packet.
    try:
        packet_companion("/etc/passwd")
    except SystemExit:
        pass
    else:
        raise AssertionError("an absolute path was accepted as a companion")


def test_the_printed_instruction_names_the_packet_by_path() -> None:
    """Acceptance 4: what an operator is told to run carries its own location.

    Driven through the command that prints it rather than through the helper
    that formats it, because the defect was never in the formatting: it was an
    instruction that put a `cd` in front of a bare file name, and taught every
    operator and every later recovery that the packet needed a directory.
    """
    from contextlib import redirect_stdout
    from io import StringIO

    from team_launcher_test_helpers import FakeRunner

    from scripts.team_launcher import current_user_name, new_project_command

    with tempfile.TemporaryDirectory(prefix="syrd149-instruction.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        printed = StringIO()
        with redirect_stdout(printed):
            assert (
                new_project_command(
                    "porter",
                    owner_user=current_user_name(),
                    source_repo=source_repo,
                    commit_git_dir="/srv/git/review-cache.git",
                    repository=project_repo,
                    output_dir=output_dir,
                    runner=FakeRunner(),
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                )
                == 0
            )
        # The execution plan only -- the packet's own text is printed after it
        # for review, and a failure here should read as the instruction it is.
        printed_lines = printed.getvalue().splitlines()
        start = printed_lines.index("team-launcher: execution plan:")
        lines = printed_lines[start : printed_lines.index("", start)]
        packet = output_dir / "operator-commands.sh"

    instruction = [line for line in lines if str(packet) in line]
    assert instruction, lines
    # The path is the whole instruction: absolute, and reached without first
    # being sent anywhere.
    assert not any(line.strip().startswith("cd ") for line in lines), lines
    for line in instruction:
        for token in shlex.split(line.strip()):
            if token.endswith("operator-commands.sh"):
                assert token == str(packet), token
                assert Path(token).is_absolute(), token


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    print(f"operator_packet_cwd_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
