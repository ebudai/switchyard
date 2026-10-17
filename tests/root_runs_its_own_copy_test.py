#!/usr/bin/env python3
"""Root runs and installs its own copy, never the tenant's (SYRD-607).

`switchyard new` used to write the operator packet into the project's
`.switchyard/provision` and then run it as root from there, and `add-role` and
`set-vcs-close-role` installed the board unit from the same directory. That
directory is the tenant's whenever the project is a second one for an existing
tenant, or lives under a path the tenant chose, so the tenant could replace the
file between the write and the run or install.

Each case here pauses at the exact boundary -- the runner call that runs the
packet, or installs the unit -- and has the tenant, a second real uid, replace,
delete or link the file root wrote. Then it proves what root actually used:

- the packet: the real `new_project_command`, as root, with the packet
  renderer swapped for a small real script that appends to a root-only marker
  the directory it ran from and the digest of the unit companion it read
  beside itself. The runner really runs `bash` on the path root asked for, so
  a substituted script that ran would say so in the marker;
- the unit: the real `set-vcs-close-role` and `add-role` commands, whose
  `install`, `mv` and `rm` really run, against a sandbox standing in for
  `/etc/systemd/system`; the restart reads back what was installed.

Runs as root in a user namespace (`unshare --map-auto --map-root-user`), so the
tenant's uid is a real, different identity whose writes the kernel judges.
Nothing outside a fresh /tmp sandbox is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts import root_packet  # noqa: E402
from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board import project_provision  # noqa: E402
from team_launcher_test_helpers import FakeRunner  # noqa: E402

PROJECT = "porter607"
OWNER = "syrd607tenant"
TENANT = 2607
CALLER = 2700
UNIT = f"{PROJECT}-ticket-board.service"
PACKET = "operator-commands.sh"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def text_of(path: Path) -> str | None:
    return path.read_text() if path.is_file() else None


def sha(body: bytes | str) -> str:
    return hashlib.sha256(body.encode() if isinstance(body, str) else body).hexdigest()


def as_tenant(action: Callable[[], None]) -> None:
    """Run `action` in a child whose every uid and gid is the tenant's."""
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            os.setgroups([])
            os.setresgid(TENANT, TENANT, TENANT)
            os.setresuid(TENANT, TENANT, TENANT)
            action()
            code = 0
        except BaseException as exc:  # noqa: BLE001 - reported to the parent by status
            sys.stderr.write(f"tenant action failed: {exc!r}\n")
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, "the tenant's action failed"


def tenant_owned(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, TENANT, TENANT)
        path.chmod(0o755)


SUBSTITUTED_UNIT = "[Service]\nUser=root\nExecStart=/bin/sh -c 'touch /tmp/syrd607-pwned'\n"


class Sandbox:
    """One tenant tree, root's provisioning root and a stand-in for /etc/systemd/system."""

    def __init__(self, *, chosen_path: bool = False) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="syrd607.", dir="/tmp"))
        self.base.chmod(0o755)
        self.privileged = self.base / "etc" / "switchyard" / "provision"
        self.root_copy = self.privileged / PROJECT
        private = self.base / "root-only"
        private.mkdir(mode=0o700)
        self.marker = private / "ran"
        self.units = self.base / "etc" / "systemd" / "system"
        self.units.mkdir(parents=True)
        self.home = self.base / "home" / OWNER
        tenant_owned(self.base / "home", self.home)
        if chosen_path:
            # A project path the tenant chose: outside its home, and its own.
            tenant_owned(self.base / "srv", self.base / "srv" / "chosen")
            self.repository = self.base / "srv" / "chosen" / PROJECT
        else:
            # A second project for a tenant that already exists: in its home.
            tenant_owned(self.home / "Projects")
            self.repository = self.home / "Projects" / PROJECT
        self.provision = self.repository / ".switchyard" / "provision"
        tenant_owned(self.repository, self.repository / ".switchyard", self.provision)
        self.attacker = self.home / "attacker"
        tenant_owned(self.attacker)
        self.env = {
            launcher.PRIVILEGED_PROVISION_ROOT_ENV: str(self.privileged),
            launcher.SWITCHYARD_REGISTRY_DIR_ENV: str(self.base / "registry"),
        }

    def substitute_script(self) -> str:
        return f"#!/usr/bin/env bash\nprintf 'substituted\\n' >> {shlex.quote(str(self.marker))}\n"

    def remove(self) -> None:
        shutil.rmtree(self.base)


def generated_packet(marker: Path) -> Callable[..., str]:
    """A real script in place of the packet: it says where it ran and which unit companion it read."""

    def render(plan: Any, *, enable_owner_linger: bool = True) -> str:
        return (
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            'provision_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
            f'unit="$(sha256sum < "$provision_dir/{plan.board_unit}" | cut -d" " -f1)"\n'
            f'printf "generated %s %s\\n" "$provision_dir" "$unit" >> {shlex.quote(str(marker))}\n'
        )

    return render


class PacketHost:
    """The host: everything simulated except the run of the packet, which is real."""

    def __init__(self, at_boundary: Callable[[], None] = lambda: None) -> None:
        self.fake = FakeRunner()
        self.at_boundary = at_boundary
        self.ran: list[str] = []
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if "bash" in args and str(args[-1]).endswith(PACKET):
            self.ran.append(str(args[-1]))
            self.at_boundary()
            return subprocess.run(["bash", str(args[-1])], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env={"PATH": "/usr/bin:/bin"})
        return self.fake(args, **kwargs)


def provision(sb: Sandbox, host: PacketHost, printed: list[str], *, output_dir: Path | None = None,
              source_repo: Path = ROOT) -> object:
    with patch.object(project_provision, "render_operator_commands", generated_packet(sb.marker)), \
            patch.dict(os.environ, sb.env):
        try:
            return launcher.new_project_command(
                PROJECT, owner_user=OWNER, owner_home=sb.home, repository=sb.repository, source_repo=source_repo,
                output_dir=output_dir or sb.provision, execute=True, runner=host,
                port_in_use=lambda _port: False, socket_exists=lambda _path: False,
                require_owner_user=False, enable_owner_linger=False, print_func=printed.append,
            )
        except SystemExit as exc:
            return str(exc)


def expected_packet(sb: Sandbox) -> str:
    return generated_packet(sb.marker)(type("Plan", (), {"board_unit": UNIT})())


def proves_root_ran_its_own(sb: Sandbox, host: PacketHost, outcome: object, label: str, *,
                            afterwards: str = "") -> None:
    if afterwards:
        # The run was root's own; what comes after it reads the tenant's tree,
        # and refuses the link the tenant left there.
        check(isinstance(outcome, str) and afterwards in outcome, f"{label}: the later read refuses ({outcome!r})")
    else:
        check(outcome == 0, f"{label}: provisioning converged ({outcome!r})")
    check(host.ran == [str(sb.root_copy / PACKET)], f"{label}: root ran its own copy, not the tenant's: {host.ran}")
    root_unit = (sb.root_copy / UNIT).read_bytes()
    check(sb.marker.read_text() == f"generated {sb.root_copy} {sha(root_unit)}\n",
          f"{label}: the generated packet ran once, reading its unit beside root's copy: {sb.marker.read_text()!r}")
    check("substituted" not in sb.marker.read_text(), f"{label}: the substituted script never ran")
    check(sha(root_unit) != sha(SUBSTITUTED_UNIT), f"{label}: the companion root read is not the tenant's")
    check(sha((sb.root_copy / PACKET).read_bytes()) == sha(expected_packet(sb)),
          f"{label}: what root ran is byte for byte what root generated")
    check((sb.root_copy / PACKET).stat().st_uid == 0 and (sb.root_copy.stat().st_mode & 0o077) == 0,
          f"{label}: root's copy is root's alone")


def replace_packet_and_unit(directory: Path, script: str) -> Callable[[], None]:
    def act() -> None:
        for name, body in ((PACKET, script), (UNIT, SUBSTITUTED_UNIT)):
            staged = directory / f".{name}.tenant"
            staged.write_text(body)
            os.chmod(staged, 0o755)
            os.replace(staged, directory / name)
    return act


# --- the packet -------------------------------------------------------------------------------


def test_packet_untouched_record_matches() -> None:
    sb = Sandbox()
    try:
        host, printed = PacketHost(), []
        outcome = provision(sb, host, printed)
        proves_root_ran_its_own(sb, host, outcome, "untouched")
        check(text_of(sb.provision / PACKET) == expected_packet(sb), "the tenant's record is the bytes root ran")
        check(text_of(sb.provision / root_packet.RECORD_NAME) is not None, "and says which bytes those were")
        record = json.loads((sb.provision / root_packet.RECORD_NAME).read_text())
        check(record["files"][PACKET] == {"sha256": sha(expected_packet(sb)), "root_used": str(sb.root_copy / PACKET)},
              f"the record names the digest and root's copy: {record['files'].get(PACKET)}")
        check(record["files"][UNIT]["sha256"] == sha((sb.root_copy / UNIT).read_bytes())
              and "plan.json" not in record["files"], "the record covers what root runs and installs, not the plan")
        check(any(f"runs its own {PACKET} there (sha256 {sha(expected_packet(sb))})" in line for line in printed),
              f"the operator is told which copy ran, and its digest: {printed}")
    finally:
        sb.remove()


def _attacked(label: str, attack: Callable[[Sandbox], Callable[[], None]], *, chosen_path: bool = False,
              afterwards: str = "") -> Sandbox:
    sb = Sandbox(chosen_path=chosen_path)
    try:
        host, printed = PacketHost(lambda: as_tenant(attack(sb))), []
        outcome = provision(sb, host, printed)
        proves_root_ran_its_own(sb, host, outcome, label, afterwards=afterwards)
    except BaseException:
        sb.remove()
        raise
    return sb


def test_packet_replaced_at_the_boundary() -> None:
    sb = _attacked("replaced", lambda sb: replace_packet_and_unit(sb.provision, sb.substitute_script()))
    try:
        check((sb.provision / PACKET).read_text() == sb.substitute_script(),
              "the tenant really did replace its copy before the run")
    finally:
        sb.remove()


def test_packet_replaced_under_a_tenant_chosen_path() -> None:
    sb = _attacked("chosen path", lambda sb: replace_packet_and_unit(sb.provision, sb.substitute_script()),
                   chosen_path=True)
    try:
        check(not sb.provision.is_relative_to(sb.home) and sb.provision.stat().st_uid == TENANT,
              "the project path was the tenant's own, outside its home")
    finally:
        sb.remove()


def test_packet_deleted_at_the_boundary() -> None:
    def attack(sb: Sandbox) -> Callable[[], None]:
        return lambda: [(sb.provision / name).unlink() for name in (PACKET, UNIT)]

    sb = _attacked("deleted", attack)
    try:
        check(not (sb.provision / PACKET).exists(), "the tenant really did delete its copy before the run")
    finally:
        sb.remove()


def test_packet_leaf_link_at_the_boundary() -> None:
    def attack(sb: Sandbox) -> Callable[[], None]:
        def act() -> None:
            replace_packet_and_unit(sb.attacker, sb.substitute_script())()
            for name in (PACKET, UNIT):
                (sb.provision / name).unlink()
                (sb.provision / name).symlink_to(sb.attacker / name)
        return act

    sb = _attacked("leaf link", attack)
    try:
        check((sb.provision / PACKET).is_symlink(), "the tenant really did link its copy away before the run")
    finally:
        sb.remove()


def test_packet_ancestor_link_at_the_boundary() -> None:
    def attack(sb: Sandbox) -> Callable[[], None]:
        def act() -> None:
            replace_packet_and_unit(sb.attacker, sb.substitute_script())()
            sb.provision.rename(sb.provision.with_name("provision.moved"))
            sb.provision.symlink_to(sb.attacker)
        return act

    sb = _attacked("ancestor link", attack, afterwards="ancestor provision is a symlink. Nothing was changed.")
    try:
        check(sb.provision.is_symlink(), "the tenant really did swap the directory for a link before the run")
    finally:
        sb.remove()


def test_packet_replaced_concurrently() -> None:
    sb = Sandbox()
    stop = sb.base / "stop"
    count = sb.attacker / "replacements"

    def hammer() -> None:
        replaced = 0
        while not stop.exists():
            try:
                replace_packet_and_unit(sb.provision, sb.substitute_script())()
                replaced += 1
            except OSError:
                pass
        count.write_text(str(replaced))

    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            os.setgroups([])
            os.setresgid(TENANT, TENANT, TENANT)
            os.setresuid(TENANT, TENANT, TENANT)
            hammer()
            code = 0
        finally:
            os._exit(code)
    try:
        host, printed = PacketHost(), []
        outcome = provision(sb, host, printed)
    finally:
        stop.write_text("")
        os.waitpid(pid, 0)
    try:
        proves_root_ran_its_own(sb, host, outcome, "concurrent")
        check(int(count.read_text()) > 0, "the tenant was replacing its copy throughout")
    finally:
        sb.remove()


def test_record_that_cannot_be_written_is_not_claimed() -> None:
    sb = Sandbox()
    try:
        bystander = sb.base / "root-owned"
        bystander.write_text("root's own\n")

        def plant() -> None:
            (sb.provision / PACKET).symlink_to(bystander)
            # A provenance file from an earlier run, which would now be a lie.
            (sb.provision / root_packet.RECORD_NAME).write_text(json.dumps({"files": {PACKET: {"sha256": "x"}}}))

        as_tenant(plant)
        host, printed = PacketHost(), []
        outcome = provision(sb, host, printed)
        proves_root_ran_its_own(sb, host, outcome, "record refused")
        check(bystander.read_text() == "root's own\n", "the record was not written through the tenant's link")
        check(any("could not publish the record of the packet" in line
                  and "is not a record of it" in line for line in printed),
              f"the failed record is reported on its own: {printed}")
        check(not (sb.provision / root_packet.RECORD_NAME).exists(), "and no provenance claims it matches")
    finally:
        sb.remove()


def test_packet_replaced_on_the_unrecorded_path() -> None:
    sb = Sandbox()
    try:
        host = PacketHost(lambda: as_tenant(replace_packet_and_unit(sb.provision, sb.substitute_script())))
        printed: list[str] = []
        with patch.object(launcher, "_rollout_recorder_path", lambda: None):
            outcome = provision(sb, host, printed)
        proves_root_ran_its_own(sb, host, outcome, "unrecorded")
        check(["bash", str(sb.root_copy / PACKET)] in host.calls, f"run plainly, from root's copy: {host.calls}")
    finally:
        sb.remove()


def test_root_copy_root_does_not_control_runs_nothing() -> None:
    sb = Sandbox()
    try:
        host, printed = PacketHost(), []
        with patch.object(launcher, "root_controlled_problems_for", lambda path: [f"{path} is not root's"]):
            outcome = provision(sb, host, printed)
        check(isinstance(outcome, str) and "is not root's alone" in outcome and outcome.endswith("Nothing was run."),
              f"a root copy that is not root's alone is refused: {outcome!r}")
        check(host.ran == [] and ["sudo", "-v"] not in host.calls and not sb.marker.exists(), "and nothing ran")
    finally:
        sb.remove()


def test_staging_refused_runs_nothing() -> None:
    sb = Sandbox()
    try:
        tenant_owned(sb.base / "etc" / "switchyard")
        host, printed = PacketHost(), []
        outcome = provision(sb, host, printed)
        check(isinstance(outcome, str) and "owned by uid 2607" in outcome and "Nothing was written" in outcome,
              f"root refuses a provisioning root the tenant could change: {outcome!r}")
        check(host.ran == [] and ["sudo", "-v"] not in host.calls, f"nothing privileged ran: {host.calls}")
        check(not sb.marker.exists() and not sb.privileged.exists(), "and nothing was staged or run")
        check(not (sb.provision / PACKET).exists(), "nor was a record published for a run that never happened")
    finally:
        sb.remove()


def as_caller(action: Callable[[], dict]) -> dict:
    """Run `action` as a real caller that is not root -- every uid and gid CALLER -- and return its JSON answer."""
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read)
        code = 1
        try:
            os.setgroups([])
            os.setresgid(CALLER, CALLER, CALLER)
            os.setresuid(CALLER, CALLER, CALLER)
            answer = action()
            code = 0
        except BaseException as exc:  # noqa: BLE001 - handed to the parent as the answer
            answer = {"crashed": repr(exc)}
        finally:
            os.write(write, json.dumps(answer).encode())
            os._exit(code)
    os.close(write)
    data = b""
    while chunk := os.read(read, 65536):
        data += chunk
    os.close(read)
    os.waitpid(pid, 0)
    return json.loads(data or b"{}")


def test_unprivileged_execute_is_refused_even_from_its_own_closed_directory() -> None:
    """A packet the caller wrote is the caller's account's to replace, however closed its directory (SYRD-607 audit)."""
    sb = Sandbox()
    try:
        own = sb.base / "operator"
        own.mkdir(mode=0o700)
        os.chown(own, CALLER, CALLER)
        # The caller cannot enter this checkout; a release it can read stands in for it.
        release = sb.base / "release"
        release.mkdir(mode=0o755)
        substitute = sb.substitute_script()

        def run() -> dict:
            # The same account swaps the packet at the run call, as Audit's check did.
            host = PacketHost(replace_packet_and_unit(own, substitute))
            outcome = provision(sb, host, [], output_dir=own, source_repo=release)
            return {"uid": os.getuid(), "euid": os.geteuid(), "outcome": outcome, "calls": host.calls, "ran": host.ran,
                    "mode": oct(own.stat().st_mode & 0o777), "owner": own.stat().st_uid}

        answer = as_caller(run)
        check(answer.get("uid") == answer.get("euid") == CALLER and answer.get("mode") == "0o700" and answer.get("owner") == CALLER,
              f"a real caller that is not root, in a directory only it and root can change: {answer}")
        check(isinstance(answer.get("outcome"), str)
              and answer["outcome"].startswith(f"team-launcher: --execute runs {PROJECT}'s provisioning packet as root, "
                                               "and root runs only a copy it staged itself")
              and answer["outcome"].endswith("Nothing was written or run."),
              f"executing without root is refused: {answer.get('outcome')!r}")
        check(answer.get("ran") == [] and ["sudo", "-v"] not in answer.get("calls", []),
              f"before sudo is asked and before any packet runs: {answer.get('calls')}")
        check(sorted(p.name for p in own.iterdir()) == [] and not sb.marker.exists(),
              "nothing was written for a run that will not happen, and neither packet ran")
    finally:
        sb.remove()


RELEASE = "6" * 40


def exported_release(release: Path, commit: str) -> Path:
    """What the installer exports: the marker, the entry point and the scripts the precheck looks for."""
    (release / "scripts").mkdir(parents=True)
    (release / launcher.SWITCHYARD_RELEASE_MARKER_NAME).write_text(json.dumps({"commit": commit}) + "\n")
    for name in ("switchyard", "scripts/team-launcher", "scripts/ticket-board-service.sh",
                 "scripts/ticket-board-boardsvc-setup.sh"):
        (release / name).write_text("#!/bin/sh\n")
    for path in (release, release / "scripts", *release.rglob("*")):
        path.chmod(0o755 if path.is_dir() or path.parent.name == "scripts" or path.name == "switchyard" else 0o644)
    return release / "scripts" / "team-launcher"


def installed_release(sb: Sandbox) -> tuple[Path, Path]:
    """A shared install root as the installer leaves it: releases/<commit>, and `current` linking to it."""
    install = sb.base / "opt" / "switchyard"
    (install / "releases").mkdir(parents=True)
    for path in (sb.base / "opt", install, install / "releases"):
        path.chmod(0o755)
    script = exported_release(install / "releases" / RELEASE, RELEASE)
    (install / "current").symlink_to(install / "releases" / RELEASE)
    return install, script


def proven_below(install: Path) -> Callable[[Path], str]:
    """The test's own seam for `root_packet.root_controlled`: the same uid-0 proof, from the sandbox install root.

    Production always walks from `/` for uid 0; a namespace cannot own `/`, so
    the fixture judges every component below its install root instead. The
    runtime has no such exception (SYRD-607 audit).
    """
    from scripts.ticket_board.publication_boundary import root_controlled_problems

    def judged(path: Path) -> str:
        problems = root_controlled_problems(str(path), expect_uid=0, base=str(install))
        return problems[0] if problems else ""

    return judged


def dry_run(sb: Sandbox, output_dir: Path, *, install: Path, source_repo: Path | None = None) -> tuple[object, list[str]]:
    printed: list[str] = []
    with patch.object(project_provision, "render_operator_commands", generated_packet(sb.marker)), \
            patch.dict(os.environ, {**sb.env, "SWITCHYARD_SHARED_INSTALL_ROOT": str(install)}), \
            patch.object(root_packet, "root_controlled", proven_below(install)), \
            patch("os.geteuid", return_value=CALLER):
        outcome = launcher.new_project_command(
            PROJECT, owner_user=OWNER, owner_home=sb.home, repository=sb.repository, source_repo=source_repo,
            output_dir=output_dir, port=24607, runner=PacketHost(), port_in_use=lambda _port: False,
            socket_exists=lambda _path: False, require_owner_user=False, print_func=printed.append,
        )
    start = printed.index("team-launcher: execution plan:")
    return outcome, printed[start + 1:printed.index("", start)]


def root_commands(plan: list[str]) -> list[list[str]]:
    return [shlex.split(line) for line in plan if line.strip().startswith("sudo ") and line.strip() != "sudo -v"]


def test_dry_run_hands_root_only_the_installed_release() -> None:
    """Audit, 23:38 and 23:58: what the dry run wrote, and the checkout it ran from, are its caller's to change."""
    from scripts.launcher_parser import _build_parser

    sb = Sandbox()
    try:
        install, trusted = installed_release(sb)
        own = sb.base / "operator"
        own.mkdir(mode=0o700)
        os.chown(own, CALLER, CALLER)
        checkout_script = Path(launcher.__file__).resolve().with_name("team-launcher")
        for label, output_dir in (("the caller's own closed directory", own), ("the tenant's directory", sb.provision)):
            outcome, plan = dry_run(sb, output_dir, install=install)
            check(outcome == 0 and (output_dir / PACKET).is_file(), f"{label}: the dry run wrote its packet for review")
            check(not any(PACKET in line or str(output_dir) + "/" in line or str(checkout_script) in line for line in plan),
                  f"{label}: no root command names a file the dry run wrote, or the checkout it ran from: {plan}")
            commands = root_commands(plan)
            argv = commands[0] if len(commands) == 1 else []
            check(argv[:4] == ["sudo", str(trusted), PROJECT, "new"] and argv[-1] == "--execute",
                  f"{label}: the one root command is the installed release's `new`, executing: {plan}")
            args = _build_parser().parse_args(argv[2:])
            check(args.execute and not args.dry_run and args.project == PROJECT and args.command == "new"
                  and args.owner_user == OWNER and args.repository == sb.repository and args.source_repo is None
                  and args.new_output_dir == output_dir and args.port == 24607,
                  f"{label}: and it parses back to the same choices, by absolute path: {argv}")
        release = install / "releases" / RELEASE
        outcome, plan = dry_run(sb, own, install=install, source_repo=release)
        commands = root_commands(plan)
        check(outcome == 0 and len(commands) == 1 and commands[0][commands[0].index("--source-repo") + 1] == str(release),
              f"a source root owns goes with the command: {plan}")
        # A release-shaped tree its caller owns: everything about it passes but whose it is.
        checkout = install / "releases" / ("7" * 40)
        exported_release(checkout, "7" * 40)
        for path in (checkout, *checkout.rglob("*")):
            os.chown(path, CALLER, CALLER)
        outcome, plan = dry_run(sb, own, install=install, source_repo=checkout)
        check(outcome == 0 and root_commands(plan) == []
              and any(line.startswith(f"  (no root command: --source-repo {checkout} is not root's: ") and f"uid {CALLER}" in line
                      for line in plan),
              f"a source in a checkout its caller owns gets no root command, and says why: {plan}")
        os.chown(release, CALLER, CALLER)
        outcome, plan = dry_run(sb, own, install=install)
        check(outcome == 0 and root_commands(plan) == []
              and any(line.startswith(f"  (no root command: {trusted} is not root's: ") for line in plan),
              f"nor does a release somebody else owns: {plan}")
        outcome, plan = dry_run(sb, own, install=sb.base / "nowhere")
        check(outcome == 0 and root_commands(plan) == []
              and any(line.startswith("  (no root command: no installed release provides ") for line in plan),
              f"nor does a host with no installed release: {plan}")
        command = root_packet.root_execute_command(
            "p607", script=Path("/opt/release/scripts/team-launcher"),
            options=(("--repository", Path("relative/repo")), ("--desktop-policy", Path("headless")),
                     ("--port", None), ("--database", "")))
        check(command == f"sudo /opt/release/scripts/team-launcher p607 new --repository {Path.cwd() / 'relative/repo'} "
                         "--desktop-policy headless --execute",
              f"a relative path is made absolute, `headless` stays a name, and an unset option is left out: {command}")
    finally:
        sb.remove()


def test_a_real_caller_cannot_name_its_own_release_through_the_environment() -> None:
    """Audit, 00:18: SWITCHYARD_SHARED_INSTALL_ROOT may say where to look, never whose files count as root's.

    A real process whose every uid is CALLER runs the dry run from a copy of
    this tree it can read, with the override pointing at a release-shaped tree
    it owns throughout. No root command may come out of it.
    """
    sb = Sandbox()
    try:
        tree = sb.base / "tree"
        for part in ("scripts", "tests"):
            shutil.copytree(ROOT / part, tree / part, ignore=shutil.ignore_patterns("__pycache__"))
        for path in (tree, *tree.rglob("*")):
            path.chmod(0o755 if path.is_dir() else 0o644)
        mine = sb.base / "mine"
        mine.mkdir(mode=0o755)
        script = exported_release(mine / "releases" / RELEASE, RELEASE)
        (mine / "current").symlink_to(mine / "releases" / RELEASE)
        own = sb.base / "operator"
        own.mkdir(mode=0o700)
        for path in (mine, *mine.rglob("*"), own):
            os.chown(path, CALLER, CALLER, follow_symlinks=False)
        probe = (
            "import json, os, sys\n"
            "sys.path[:0] = ['tests', '.']\n"
            "from pathlib import Path\n"
            "from team_launcher_test_helpers import FakeRunner\n"
            "from scripts import root_packet, team_launcher as launcher\n"
            "# After the helpers, which point it at their own fixture when they load.\n"
            f"os.environ['SWITCHYARD_SHARED_INSTALL_ROOT'] = {str(mine)!r}\n"
            "printed = []\n"
            f"outcome = launcher.new_project_command({PROJECT!r}, owner_user={OWNER!r}, owner_home=Path({str(sb.home)!r}),\n"
            f"    repository=Path({str(sb.repository)!r}), output_dir=Path({str(own)!r}), runner=FakeRunner(),\n"
            "    port_in_use=lambda _p: False, socket_exists=lambda _p: False, require_owner_user=False,\n"
            "    print_func=printed.append)\n"
            "print(json.dumps({'uid': os.getuid(), 'euid': os.geteuid(), 'outcome': outcome, 'printed': printed,\n"
            "                  'trusted': [str(x) if x else x for x in root_packet.trusted_launcher()]}))\n"
        )

        def become_caller() -> None:
            os.setgroups([])
            os.setresgid(CALLER, CALLER, CALLER)
            os.setresuid(CALLER, CALLER, CALLER)

        ran = subprocess.run([sys.executable, "-B", "-c", probe], cwd=tree, preexec_fn=become_caller,
                             capture_output=True, text=True, timeout=240,
                             env={"PATH": "/usr/bin:/bin", "HOME": str(own), "LANG": "C.UTF-8",
                                  "SWITCHYARD_SHARED_INSTALL_ROOT": str(mine)})
        answer = json.loads(ran.stdout.strip().splitlines()[-1]) if ran.returncode == 0 and ran.stdout.strip() else {}
        check(answer.get("uid") == answer.get("euid") == CALLER and answer.get("outcome") == 0,
              f"a real caller, not root, ran the dry run: {answer or ran.stderr[-1500:]}")
        check(answer["trusted"][0] is None and str(script) in answer["trusted"][1] and "is not root's" in answer["trusted"][1],
              f"its own release is not trusted, whatever the environment names: {answer['trusted']}")
        printed = answer["printed"]
        start = printed.index("team-launcher: execution plan:")
        plan = printed[start + 1:printed.index("", start)]
        check(root_commands(plan) == [] and not any(str(mine) in line and "sudo" in line for line in plan)
              and any(line.startswith(f"  (no root command: {script} is not root's: ") for line in plan),
              f"so the dry run emits no root command, and says why: {plan}")
    finally:
        sb.remove()


# --- the board unit ---------------------------------------------------------------------------


class UnitHost:
    """The host for add-role and set-vcs-close-role: the unit install is real, into a sandbox.

    The tenant acts once, at the first step after the command has written its
    record of the unit -- the board SQL, which comes before the install -- so a
    command that read the unit back from there at any point up to the install
    would read the tenant's.
    """

    def __init__(self, sb: Sandbox, at_boundary: Callable[[], None] = lambda: None, *,
                 fail: tuple[str, ...] = ()) -> None:
        self.sb = sb
        self.fake = FakeRunner()
        self.at_boundary = at_boundary
        self.fail = fail
        self.calls: list[list[str]] = []
        self.started: list[str] = []
        self.acted = False

    def act(self) -> None:
        if not self.acted:
            self.acted = True
            self.at_boundary()

    def mapped(self, value: str) -> str:
        return value.replace("/etc/systemd/system", str(self.sb.units), 1)

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        command = args[1] if args[:1] == ["sudo"] and len(args) > 1 else ""
        if command in ("install", "mv", "rm") and any(a.startswith("/etc/systemd/system") for a in args):
            if command == "install":
                self.act()
            if command in self.fail:
                return subprocess.CompletedProcess(args, 1, "", "refused")
            return subprocess.run([self.mapped(a) for a in args[1:]], input=kwargs.get("input"),
                                  text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if args[:3] == ["sudo", "systemctl", "restart"]:
            self.started.append(sha((self.sb.units / args[3]).read_bytes()))
            return subprocess.CompletedProcess(args, 0, "", "")
        if "psql" in args:
            self.act()
            return subprocess.CompletedProcess(args, 1 if "psql" in self.fail else 0, "", "")
        return self.fake(args, **kwargs)


def tenant_project(sb: Sandbox) -> tuple[Any, Path]:
    plan = project_provision.build_plan(project=PROJECT, project_name="Porter", owner_user=OWNER,
                                        owner_home=sb.home, source_repo=ROOT)
    project_provision.write_artifacts(plan, sb.provision, enable_owner_linger=False)
    config_path = launcher.write_new_project_launcher_artifacts(
        plan, sb.provision, repository=sb.repository, print_func=lambda _text: None
    )
    for path in (sb.provision, *sb.provision.iterdir()):
        os.chown(path, TENANT, TENANT, follow_symlinks=False)
    return launcher.load_project_config(PROJECT, config_path), config_path


def close_role(sb: Sandbox, host: UnitHost, printed: list[str]) -> tuple[object, str]:
    with patch.dict(os.environ, sb.env):
        config, config_path = tenant_project(sb)
        expected = launcher.render_board_unit(
            launcher._project_plan_for_vcs_close_role(config, config_path=config_path, role_name="main"))
        try:
            outcome: object = launcher.set_project_vcs_close_role_command(
                config, config_path=config_path, role_name="main", runner=host, print_func=printed.append)
        except SystemExit as exc:
            outcome = str(exc)
    return outcome, expected


def proves_root_installed_its_own(sb: Sandbox, host: UnitHost, outcome: object, expected: str, label: str) -> None:
    check(outcome == 0, f"{label}: the command converged ({outcome!r})")
    installed = (sb.units / UNIT).read_text()
    check(sha(installed) == sha(expected), f"{label}: the installed unit is the text root rendered")
    check("User=root" not in installed, f"{label}: the substituted unit was never installed")
    check(host.started == [sha(expected)], f"{label}: the unit restarted is the one root rendered: {host.started}")
    check(sorted(p.name for p in sb.units.iterdir()) == [UNIT], f"{label}: nothing staged is left behind")
    check(not any(str(sb.provision) in arg for call in host.calls for arg in call if call[:2] == ["sudo", "install"]),
          f"{label}: the install never named the tenant's directory")


def _unit_attacked(label: str, attack: Callable[[Sandbox], Callable[[], None]]) -> Sandbox:
    sb = Sandbox()
    try:
        host, printed = UnitHost(sb, lambda: as_tenant(attack(sb))), []
        outcome, expected = close_role(sb, host, printed)
        proves_root_installed_its_own(sb, host, outcome, expected, label)
    except BaseException:
        sb.remove()
        raise
    return sb


def test_unit_untouched_record_matches() -> None:
    sb = Sandbox()
    try:
        host, printed = UnitHost(sb), []
        outcome, expected = close_role(sb, host, printed)
        proves_root_installed_its_own(sb, host, outcome, expected, "unit untouched")
        check(text_of(sb.provision / UNIT) == expected, "the tenant's record is the text root installed")
        check(text_of(sb.provision / root_packet.RECORD_NAME) is not None, "and says which text that was")
        record = json.loads((sb.provision / root_packet.RECORD_NAME).read_text())
        check(record["files"][UNIT] == {"sha256": sha(expected), "root_used": f"/etc/systemd/system/{UNIT}"},
              f"the record names the installed digest: {record}")
        check(host.calls[[c[:2] for c in host.calls].index(["sudo", "install"])]
              == ["sudo", "install", "-m", "0644", "-o", "root", "-g", "root", "/dev/stdin",
                  f"/etc/systemd/system/.{UNIT}.switchyard-new"], "installed from stdin, staged beside the target")
    finally:
        sb.remove()


def test_unit_replaced_at_the_boundary() -> None:
    sb = _unit_attacked("unit replaced", lambda sb: replace_packet_and_unit(sb.provision, sb.substitute_script()))
    try:
        check((sb.provision / UNIT).read_text() == SUBSTITUTED_UNIT, "the tenant really did replace the unit")
    finally:
        sb.remove()


def test_unit_deleted_at_the_boundary() -> None:
    sb = _unit_attacked("unit deleted", lambda sb: (lambda: (sb.provision / UNIT).unlink()))
    sb.remove()


def test_unit_links_at_the_boundary() -> None:
    def leaf(sb: Sandbox) -> Callable[[], None]:
        def act() -> None:
            replace_packet_and_unit(sb.attacker, sb.substitute_script())()
            (sb.provision / UNIT).unlink()
            (sb.provision / UNIT).symlink_to(sb.attacker / UNIT)
        return act

    def ancestor(sb: Sandbox) -> Callable[[], None]:
        def act() -> None:
            replace_packet_and_unit(sb.attacker, sb.substitute_script())()
            sb.provision.rename(sb.provision.with_name("provision.moved"))
            sb.provision.symlink_to(sb.attacker)
        return act

    _unit_attacked("unit leaf link", leaf).remove()
    _unit_attacked("unit ancestor link", ancestor).remove()


def test_unit_failure_before_install_installs_nothing() -> None:
    for fail, label in ((("psql",), "SQL refused"), (("install",), "install refused"), (("mv",), "rename refused")):
        sb = Sandbox()
        try:
            (sb.units / UNIT).write_text("the unit systemd has\n")
            host, printed = UnitHost(sb, fail=fail), []
            outcome, _expected = close_role(sb, host, printed)
            check(isinstance(outcome, str) and outcome.startswith("team-launcher: failed to"),
                  f"{label}: the command stops ({outcome!r})")
            check((sb.units / UNIT).read_text() == "the unit systemd has\n"
                  and sorted(p.name for p in sb.units.iterdir()) == [UNIT],
                  f"{label}: the loaded unit is untouched and nothing staged is left")
            check(host.started == [], f"{label}: nothing restarted")
        finally:
            sb.remove()


def test_add_role_installs_the_text_it_rendered() -> None:
    sb = Sandbox()
    try:
        host = UnitHost(sb, lambda: as_tenant(replace_packet_and_unit(sb.provision, sb.substitute_script())))
        printed: list[str] = []
        with patch.dict(os.environ, sb.env):
            config, config_path = tenant_project(sb)
            try:
                outcome: object = launcher.add_project_role_command(
                    config, config_path=config_path, role_name="perf", cli="codex", start=False,
                    runner=host, print_func=printed.append, account_exists=lambda _name: True,
                )
            except SystemExit as exc:
                outcome = str(exc)
            updated = launcher.load_project_config(PROJECT, config_path)
            expected = launcher.render_board_unit(
                launcher._project_plan_for_added_role(updated, config_path=config_path, role_name="perf"))
        check("perf" in expected, "the expected unit is the one that names the added role")
        check((sb.provision / UNIT).read_text() == SUBSTITUTED_UNIT, "the tenant really did replace the unit")
        proves_root_installed_its_own(sb, host, outcome, expected, "add-role")
    finally:
        sb.remove()


def root_child() -> None:
    assert os.geteuid() == 0
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"root_runs_its_own_copy_test: ok ({CHECKS} checks)")


def main() -> int:
    command = [sys.executable, str(Path(__file__).resolve()), "--root-child"]
    if os.geteuid() != 0:
        command = ["unshare", "--user", "--map-auto", "--map-root-user", *command]
    return subprocess.run(command).returncode


if __name__ == "__main__":
    if sys.argv[1:] == ["--root-child"]:
        root_child()
    else:
        raise SystemExit(main())
