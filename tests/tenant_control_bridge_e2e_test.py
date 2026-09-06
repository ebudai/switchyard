#!/usr/bin/env python3
"""SYRD-50 end to end: one recorded human really starts another account's tenant.

The mocked suite cannot catch what this has to get right, because the parts that
would fail are exactly the parts a stub replaces: the caller identity has to
come from the kernel, the destination has to be decided by a root-owned file the
caller cannot write, the process really has to end up owned by the tenant owner,
and the environment the owner runs under has to be the bridge's and not the
caller's.

So this runs in a user namespace where this process is root, uses accounts that
really exist with really distinct uids, installs the grant at the real pinned
path, executes the real bridge, and reads every uid, argument and variable back
out of the process that actually ran.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BRIDGE = ROOT / "scripts" / "switchyard-tenant-control"
GRANT_ROOT = Path("/usr/local/lib/switchyard")
#: The real shared install root, bind-mounted in the namespace so the chain
#: the bridge walks is the one it walks in production.
SHARED_ROOT = Path("/opt/switchyard")
PROJECT = "demo"

#: What a caller might try to smuggle in. None of it may reach the owner.
POISON = {
    "PATH": "/tmp/attacker/bin:/usr/bin:/bin",
    "PYTHONPATH": "/tmp/attacker/py",
    "LD_PRELOAD": "/tmp/attacker/evil.so",
    "SWITCHYARD_CONFIG": "/tmp/attacker/config.json",
    "SWITCHYARD_SOURCE": "/tmp/attacker/src",
    "TICKET_BOARD_CALLER_ROLE": "director",
    "BASH_ENV": "/tmp/attacker/rc",
}

LAUNCHER_SOURCE = """#!/usr/bin/env python3
import json, os, sys
json.dump(
    {
        "uid": os.getuid(),
        "euid": os.geteuid(),
        "gid": os.getgid(),
        "groups": sorted(os.getgroups()),
        "argv": sys.argv,
        "environ": dict(os.environ),
        "cwd": os.getcwd(),
    },
    open("@RECORD@", "w"),
)
"""


class Accounts:
    """Three real, distinct local identities: owner, authorized human, intruder."""

    def __init__(self) -> None:
        seen: dict[int, pwd.struct_passwd] = {}
        for entry in pwd.getpwall():
            if 0 < entry.pw_uid < 1000 and entry.pw_uid not in seen:
                seen[entry.pw_uid] = entry
        picked = [seen[uid] for uid in sorted(seen)][:3]
        assert len(picked) == 3, f"need three distinct unprivileged accounts, found {picked}"
        self.owner, self.human, self.intruder = picked
        assert len({self.owner.pw_uid, self.human.pw_uid, self.intruder.pw_uid}) == 3
        assert os.geteuid() not in {self.owner.pw_uid, self.human.pw_uid, self.intruder.pw_uid}


def _write_grant(payload: dict[str, str], *, mode: int = 0o644, uid: int = 0) -> Path:
    directory = GRANT_ROOT / PROJECT
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "control-grant.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chown(path, uid, 0)
    os.chmod(path, mode)
    return path


def _record_path(work: Path, name: str, accounts: Accounts) -> Path:
    """A drop box the owner account can write, so the run can be read back."""
    box = work / "records"
    box.mkdir(exist_ok=True)
    os.chown(box, accounts.owner.pw_uid, accounts.owner.pw_gid)
    os.chmod(box, 0o755)
    return box / name


def _write_launcher(directory: Path, record: Path, *, mode: int = 0o755, uid: int = 0) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    launcher = directory / "switchyard"
    launcher.write_text(LAUNCHER_SOURCE.replace("@RECORD@", str(record)), encoding="utf-8")
    os.chown(launcher, uid, 0)
    os.chmod(launcher, mode)
    return launcher


def _shared_release(work: Path, record: Path) -> Path:
    """A root-controlled release tree shaped like the real shared install.

    /opt/switchyard/current is a root-owned symlink into a root-owned release
    directory, which is what makes the entrypoint pinned. The tests build the
    same shape so the chain being walked is a real one.
    """
    releases = SHARED_ROOT / "releases" / "r1"
    releases.mkdir(parents=True, exist_ok=True)
    launcher = _write_launcher(releases, record)
    current = SHARED_ROOT / "current"
    if current.is_symlink() or current.exists():
        current.unlink()
    current.symlink_to(releases)
    os.chown(current, 0, 0, follow_symlinks=False)
    for directory in (SHARED_ROOT, SHARED_ROOT / "releases", releases):
        os.chown(directory, 0, 0)
        os.chmod(directory, 0o755)
    assert launcher.exists()
    return current / "switchyard"


def _run(caller_uid: int | None, *args: str, environ: dict[str, str] | None = None):
    env = dict(POISON)
    if caller_uid is not None:
        env["SUDO_UID"] = str(caller_uid)
        # A name the caller chose, contradicting the uid sudo resolved.
        env["SUDO_USER"] = "root"
    env.update(environ or {})
    result = subprocess.run(
        [sys.executable, str(BRIDGE), *args],
        text=True,
        stdin=subprocess.DEVNULL,  # a password prompt here would fail, not hang
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        cwd="/",
    )
    # Real callers arrive through sudo, which drops LD_PRELOAD before the bridge
    # is even entered. Here it is set deliberately, so ld.so grumbles at the
    # bridge's own startup; that is this harness talking, not the bridge.
    result.stdout = "\n".join(
        line for line in result.stdout.splitlines() if "from LD_PRELOAD cannot be preloaded" not in line
    )
    return result


def case_authorized_human_starts_another_accounts_tenant(accounts: Accounts, work: Path) -> None:
    record = _record_path(work, "start-record.json", accounts)
    launcher = _shared_release(work, record)
    _write_grant(
        {
            "project": PROJECT,
            "owner": accounts.owner.pw_name,
            "authorized_user": accounts.human.pw_name,
            "launcher": str(launcher),
        }
    )

    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode == 0, result.stdout
    ran = json.loads(record.read_text(encoding="utf-8"))

    # It really became the owner -- not the caller, and not root.
    assert ran["uid"] == accounts.owner.pw_uid, ran
    assert ran["euid"] == accounts.owner.pw_uid, ran
    assert ran["gid"] == accounts.owner.pw_gid, ran
    assert ran["uid"] != accounts.human.pw_uid
    assert 0 not in ran["groups"], ran["groups"]

    # Exactly the pinned launcher and the project. No config, no source, no
    # role, nothing the caller typed. It execs the RESOLVED path rather than
    # the one the grant spells, so the program that runs is the one whose whole
    # chain was just checked -- not whatever the path names a moment later.
    assert ran["argv"] == [str(launcher.resolve()), PROJECT], ran["argv"]
    assert launcher.resolve() != launcher, "the fixture should exercise the symlink"

    environ = ran["environ"]
    for name, value in POISON.items():
        assert environ.get(name) != value, (name, environ.get(name))
    for name in ("PYTHONPATH", "LD_PRELOAD", "SWITCHYARD_CONFIG", "SWITCHYARD_SOURCE", "BASH_ENV"):
        assert name not in environ, name
    assert environ["PATH"] == "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    assert environ["USER"] == accounts.owner.pw_name
    assert environ["HOME"] == accounts.owner.pw_dir
    # Recorded so the tenant can attribute the action; the caller's own name,
    # taken from the uid, not from the SUDO_USER string it also sent.
    assert environ["SWITCHYARD_TENANT_CONTROL_CALLER"] == accounts.human.pw_name
    assert environ["SWITCHYARD_TENANT_CONTROL_CALLER"] != "root"

    # No prompt, no tty, no credential: stdin was closed the whole way through.
    assert "password" not in result.stdout.lower()


def case_each_verb_reaches_its_own_launcher_argv(accounts: Accounts, work: Path) -> None:
    for operation, expected in (("start", []), ("stop", ["stop"]), ("status", ["status"])):
        record = _record_path(work, f"{operation}-record.json", accounts)
        launcher = _shared_release(work, record)
        _write_grant(
            {
                "project": PROJECT,
                "owner": accounts.owner.pw_name,
                "authorized_user": accounts.human.pw_name,
                "launcher": str(launcher),
            }
        )
        result = _run(accounts.human.pw_uid, PROJECT, operation)
        assert result.returncode == 0, (operation, result.stdout)
        ran = json.loads(record.read_text(encoding="utf-8"))
        assert ran["argv"] == [str(launcher.resolve()), PROJECT, *expected], (operation, ran["argv"])
        assert ran["uid"] == accounts.owner.pw_uid


def case_an_unauthorized_local_user_is_refused(accounts: Accounts, work: Path) -> None:
    record = _record_path(work, "intruder-record.json", accounts)
    launcher = _shared_release(work, record)
    _write_grant(
        {
            "project": PROJECT,
            "owner": accounts.owner.pw_name,
            "authorized_user": accounts.human.pw_name,
            "launcher": str(launcher),
        }
    )

    result = _run(accounts.intruder.pw_uid, PROJECT, "start")
    assert result.returncode != 0, result.stdout
    assert f"{accounts.intruder.pw_name} is not the authorized control user" in result.stdout
    assert not record.exists(), "the launcher ran for an unauthorized caller"
    # Refused outright. Reading the grant is not being named by it, and being
    # refused is not an invitation to authenticate.
    assert "password" not in result.stdout.lower()

    # The owner's own account is not a way in either: only the recorded human.
    result = _run(accounts.owner.pw_uid, PROJECT, "start")
    assert result.returncode != 0, result.stdout
    assert not record.exists()


def case_a_forged_caller_name_is_ignored(accounts: Accounts, work: Path) -> None:
    record = _record_path(work, "forged-record.json", accounts)
    launcher = _shared_release(work, record)
    _write_grant(
        {
            "project": PROJECT,
            "owner": accounts.owner.pw_name,
            "authorized_user": accounts.human.pw_name,
            "launcher": str(launcher),
        }
    )
    # The uid is the intruder's; the name says otherwise. The uid wins.
    result = _run(
        accounts.intruder.pw_uid,
        PROJECT,
        "start",
        environ={"SUDO_USER": accounts.human.pw_name},
    )
    assert result.returncode != 0, result.stdout
    assert f"{accounts.intruder.pw_name} is not the authorized" in result.stdout
    assert not record.exists()

    # And with no uid at all there is no caller to check, so there is no run.
    result = _run(None, PROJECT, "start", environ={"SUDO_USER": accounts.human.pw_name})
    assert result.returncode != 0, result.stdout
    assert "must be run through the tenant control sudo grant" in result.stdout
    assert not record.exists()


def case_injected_projects_paths_and_arguments_are_refused(accounts: Accounts, work: Path) -> None:
    record = _record_path(work, "injection-record.json", accounts)
    launcher = _shared_release(work, record)
    good = {
        "project": PROJECT,
        "owner": accounts.owner.pw_name,
        "authorized_user": accounts.human.pw_name,
        "launcher": str(launcher),
    }
    _write_grant(good)

    # Arguments: no path, no shell, no extra word, no unlisted verb.
    for args, expected in (
        ((), "usage"),
        ((PROJECT,), "usage"),
        ((PROJECT, "start", "--config=/tmp/evil.json"), "usage"),
        (("../other", "start"), "plain slug"),
        (("demo/../other", "start"), "plain slug"),
        (("/etc/passwd", "start"), "plain slug"),
        (("demo;reboot", "start"), "plain slug"),
        ((PROJECT, "start;rm -rf /"), "operation must be one of"),
        ((PROJECT, "$(id)"), "operation must be one of"),
        ((PROJECT, "upgrade"), "operation must be one of"),
        ((PROJECT, "--layout=viewer"), "operation must be one of"),
    ):
        result = _run(accounts.human.pw_uid, *args)
        assert result.returncode != 0, args
        assert expected in result.stdout, (args, result.stdout)
        assert not record.exists(), args

    # A grant that names another project cannot be reached through this one.
    _write_grant({**good, "project": "other"})
    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode != 0
    assert "names a different project" in result.stdout
    assert not record.exists()

    # A path that is not absolute would resolve against whatever cwd it
    # inherits, and . or .. would let a root-owned prefix lead somewhere else.
    for launcher_value, expected in (
        ("live/scripts/switchyard", "must be an absolute path"),
        ("/opt/switchyard/current/../../tmp/switchyard", "must not contain empty, . or .."),
        ("/opt/switchyard/current/./switchyard", "must not contain empty, . or .."),
        ("/opt/switchyard//current/switchyard", "must not contain empty, . or .."),
    ):
        _write_grant({**good, "launcher": launcher_value})
        result = _run(accounts.human.pw_uid, PROJECT, "start")
        assert result.returncode != 0, launcher_value
        assert expected in result.stdout, (launcher_value, result.stdout)
        assert not record.exists(), launcher_value


def case_a_launcher_the_owner_can_replace_is_not_pinned(accounts: Accounts, work: Path) -> None:
    """The whole path has to be root's, not just the file at the end of it.

    The first version of this recorded the tenant's own deployed launcher,
    /home/<owner>/<slug>-ticketboard-live/current/scripts/switchyard, and
    checked only that final file. The owner owns every directory above it and
    the `current` symlink, so the owner -- not root -- chose what the authorized
    human's `switchyard <project>` actually ran. A root-owned JSON string and a
    root-owned leaf do not make an owner-replaceable path pinned.
    """
    record = _record_path(work, "pinning-record.json", accounts)
    launcher = _shared_release(work, record)
    good = {
        "project": PROJECT,
        "owner": accounts.owner.pw_name,
        "authorized_user": accounts.human.pw_name,
        "launcher": str(launcher),
    }
    _write_grant(good)

    # Baseline: the pinned entrypoint, whole chain root-owned, really runs.
    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode == 0, result.stdout
    assert json.loads(record.read_text(encoding="utf-8"))["uid"] == accounts.owner.pw_uid
    record.unlink()

    # Under the root-owned shared root on purpose: the only component that is
    # not root's is the one each sub-case deliberately hands to the owner, so a
    # refusal names that component and not some incidental world-writable
    # directory further up.
    owner_tree = SHARED_ROOT / "owner-tree"
    owner_tree.mkdir(parents=True, exist_ok=True)
    _write_launcher(owner_tree, record, uid=accounts.owner.pw_uid)
    os.chown(owner_tree, accounts.owner.pw_uid, accounts.owner.pw_gid)

    current = SHARED_ROOT / "current"
    release = SHARED_ROOT / "releases" / "r1"

    def restore() -> None:
        _shared_release(work, record)

    # 1. The owner repoints the `current` symlink at a tree they control. The
    #    link is still root's; what it now names is not.
    current.unlink()
    current.symlink_to(owner_tree)
    os.chown(current, 0, 0, follow_symlinks=False)
    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode != 0, result.stdout
    assert "is not root-owned" in result.stdout, result.stdout
    assert str(owner_tree) in result.stdout, result.stdout
    assert not record.exists(), "the owner's program ran"
    restore()

    # 2. The owner owns the symlink itself and can retarget it at will.
    current.unlink()
    current.symlink_to(release)
    os.chown(current, accounts.owner.pw_uid, accounts.owner.pw_gid, follow_symlinks=False)
    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode != 0, result.stdout
    assert "is not root-owned" in result.stdout
    assert not record.exists()
    restore()

    # 3. The owner owns a directory on the way to the launcher, so they can
    #    replace the launcher inside it whenever they like.
    os.chown(release, accounts.owner.pw_uid, accounts.owner.pw_gid)
    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode != 0, result.stdout
    assert "is not root-owned" in result.stdout
    assert not record.exists()
    restore()

    # 4. A root-owned parent that is group- or world-writable is the same
    #    problem wearing different clothes.
    os.chmod(release, 0o777)
    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode != 0, result.stdout
    assert "is writable by group or other" in result.stdout
    assert not record.exists()
    restore()

    # 5. The launcher file itself, owned by the tenant owner. Previously
    #    accepted on the reasoning that it runs as the owner anyway; refused
    #    now, because the human asked to start their project, not to run
    #    whatever the owner account last put at that path.
    os.chown(release / "switchyard", accounts.owner.pw_uid, accounts.owner.pw_gid)
    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode != 0, result.stdout
    assert "is not root-owned" in result.stdout
    assert not record.exists()
    restore()

    # 6. And a third party's launcher, which was never acceptable.
    os.chown(release / "switchyard", accounts.intruder.pw_uid, accounts.intruder.pw_gid)
    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode != 0, result.stdout
    assert "is not root-owned" in result.stdout
    assert not record.exists()
    restore()

    # Restored, the pinned entrypoint runs again -- so the refusals above were
    # the mutation and not a suite that had stopped working.
    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode == 0, result.stdout
    assert json.loads(record.read_text(encoding="utf-8"))["uid"] == accounts.owner.pw_uid


def case_a_grant_anyone_could_rewrite_is_not_trusted(accounts: Accounts, work: Path) -> None:
    record = _record_path(work, "grant-record.json", accounts)
    launcher = _shared_release(work, record)
    good = {
        "project": PROJECT,
        "owner": accounts.owner.pw_name,
        "authorized_user": accounts.human.pw_name,
        "launcher": str(launcher),
    }

    # A tenant that could rewrite its own grant would choose its own owner and
    # its own launcher, so the file's ownership is part of the decision.
    _write_grant(good, uid=accounts.owner.pw_uid)
    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode != 0
    assert "is not root-owned" in result.stdout
    assert not record.exists()

    _write_grant(good, mode=0o666)
    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode != 0
    assert "writable by non-root" in result.stdout
    assert not record.exists()

    for broken in ({}, {"project": PROJECT}, {**good, "authorized_user": ""}):
        _write_grant(broken)
        result = _run(accounts.human.pw_uid, PROJECT, "start")
        assert result.returncode != 0, broken
        assert not record.exists(), broken

    (GRANT_ROOT / PROJECT / "control-grant.json").unlink()
    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode != 0
    assert f"no control grant is installed for {PROJECT}" in result.stdout
    assert not record.exists()


def case_nothing_reusable_is_ever_written_down(accounts: Accounts, work: Path) -> None:
    record = _record_path(work, "material-record.json", accounts)
    launcher = _shared_release(work, record)
    grant = _write_grant(
        {
            "project": PROJECT,
            "owner": accounts.owner.pw_name,
            "authorized_user": accounts.human.pw_name,
            "launcher": str(launcher),
        }
    )
    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode == 0, result.stdout

    # Everything the bridge left behind, read as bytes.
    written = [path for path in GRANT_ROOT.rglob("*") if path.is_file()]
    assert grant in written
    for path in written:
        body = path.read_text(encoding="utf-8", errors="replace").lower()
        for forbidden in ("password", "passwd", "secret", "token", "private key", "-----begin"):
            assert forbidden not in body, (path, forbidden)

    # The grant is a name and a path, and nothing else -- there is no field a
    # thief could replay.
    payload = json.loads(grant.read_text(encoding="utf-8"))
    assert set(payload) == {"project", "owner", "authorized_user", "launcher"}

    # And the run itself carried no credential to the owner.
    environ = json.loads(record.read_text(encoding="utf-8"))["environ"]
    assert not [name for name in environ if "PASS" in name.upper() or "TOKEN" in name.upper()], environ


def case_repair_reinstalls_the_same_bridge_for_the_same_human(accounts: Accounts, work: Path) -> None:
    """Run the generated repair commands for real, twice, as root.

    Idempotence is the property a repair has to have, and it is not visible in
    the rendered text: it depends on the installed grant, not on who is running
    the repair, and on the files being rewritten rather than accumulated.
    """
    from scripts.ticket_board.project_provision import (
        resolve_control_user,
        tenant_control_commands,
        tenant_control_grant_path,
        tenant_control_sudoers_path,
    )

    sudoers_dir = Path("/etc/sudoers.d")
    grant_path = Path(tenant_control_grant_path(PROJECT))
    sudoers_path = Path(tenant_control_sudoers_path(PROJECT))
    for path in (grant_path, sudoers_path):
        if path.exists():
            path.unlink()

    # First repair: nothing installed, so the person running it is recorded.
    control = resolve_control_user(
        PROJECT, invoking_user=accounts.human.pw_name, owner_user=accounts.owner.pw_name
    )
    assert control == accounts.human.pw_name
    _apply(tenant_control_commands(PROJECT, accounts.owner.pw_name, control))
    first = (grant_path.read_bytes(), sudoers_path.read_bytes())
    assert grant_path.stat().st_uid == 0 and not grant_path.stat().st_mode & 0o022
    assert sudoers_path.stat().st_mode & 0o777 == 0o440

    # Second repair, run by a different operator: the recorded human wins, so
    # the grant does not change hands and the bytes do not move.
    control = resolve_control_user(
        PROJECT, invoking_user=accounts.intruder.pw_name, owner_user=accounts.owner.pw_name
    )
    assert control == accounts.human.pw_name, "a second operator took the grant over"
    _apply(tenant_control_commands(PROJECT, accounts.owner.pw_name, control))
    assert (grant_path.read_bytes(), sudoers_path.read_bytes()) == first

    # One rule, not two, and no staged leftovers.
    assert sorted(path.name for path in sudoers_dir.iterdir()) == [sudoers_path.name]

    # Teardown really removes them, so a torn-down tenant leaves no rule behind.
    for command in (
        ["rm", "-f", str(sudoers_path)],
        ["rm", "-f", str(grant_path)],
    ):
        subprocess.run(command, check=True)
    assert not sudoers_path.exists() and not grant_path.exists()
    assert list(sudoers_dir.iterdir()) == []
    # And with the grant gone the bridge refuses everything again.
    result = _run(accounts.human.pw_uid, PROJECT, "start")
    assert result.returncode != 0
    assert "no control grant is installed" in result.stdout


def _apply(commands: list[str]) -> None:
    """Run the rendered operator commands for real, as root inside the namespace."""
    assert commands, "the repair rendered nothing to run"
    script = "\n".join(["set -euo pipefail", *commands])
    # There is no sudo in the namespace and we are already root, so `sudo` is a
    # transparent shim -- the commands themselves are unchanged.
    subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        check=True,
        env={"PATH": f"{_SUDO_SHIM}:/usr/local/bin:/usr/bin:/bin", "HOME": _SUDO_SHIM},
    )


def end_to_end() -> None:
    assert os.geteuid() == 0, "this case must run as root inside the namespace"
    accounts = Accounts()
    with tempfile.TemporaryDirectory(prefix="switchyard-tenant-control-e2e.") as tmp:
        work = Path(tmp)
        os.chmod(work, 0o755)
        for case in (
            case_authorized_human_starts_another_accounts_tenant,
            case_each_verb_reaches_its_own_launcher_argv,
            case_an_unauthorized_local_user_is_refused,
            case_a_forged_caller_name_is_ignored,
            case_injected_projects_paths_and_arguments_are_refused,
            case_a_launcher_the_owner_can_replace_is_not_pinned,
            case_a_grant_anyone_could_rewrite_is_not_trusted,
            case_nothing_reusable_is_ever_written_down,
            case_repair_reinstalls_the_same_bridge_for_the_same_human,
        ):
            case_work = work / case.__name__
            case_work.mkdir()
            os.chmod(case_work, 0o755)
            case(accounts, case_work)
            print(f"  {case.__name__}: ok")


_SUDO_SHIM = "/tmp/switchyard-tenant-control-shim"


def _install_sudo_shim() -> None:
    """`sudo` inside the sandbox, where we are already root and sudo is absent.

    It execs its arguments unchanged, so the rendered commands run exactly as
    written -- including visudo, whose validation is part of what is tested.
    """
    shim = Path(_SUDO_SHIM)
    shim.mkdir(parents=True, exist_ok=True)
    program = shim / "sudo"
    program.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    program.chmod(0o755)
    if not shutil.which("visudo"):
        raise SystemExit("tenant_control_bridge_e2e_test: visudo is required")


def _mount(*args: str) -> None:
    subprocess.run(["mount", *args], check=True)


def _enter_root_owned_filesystem() -> None:
    """A root filesystem whose every directory really belongs to uid 0.

    The bridge refuses a launcher whose path is not root-controlled all the way
    down, so a harness that cannot present a root-owned `/` cannot exercise the
    accepting case at all -- and the host's `/` maps to nobody inside a user
    namespace, however root it looks outside. So this builds a tree as uid 0 and
    chroots into it: `/`, `/opt` and `/usr/local/lib` are then genuinely root's,
    while `/usr` and the passwd database are the host's real ones, so the
    accounts the cases use are still real accounts with real distinct uids.
    """
    sandbox = Path("/tmp/switchyard-tenant-control-root")
    for relative in ("usr", "etc", "etc/sudoers.d", "opt", "tmp", "dev", "proc", "run", "var"):
        (sandbox / relative).mkdir(parents=True, exist_ok=True)
    _mount("--bind", "--make-private", "/usr", str(sandbox / "usr"))
    _mount("-o", "remount,bind,ro", str(sandbox / "usr"))
    for name in ("bin", "lib", "lib64", "sbin"):
        link = sandbox / name
        if not link.exists():
            link.symlink_to(f"usr/{name}")
    for name in ("passwd", "group", "sudo.conf", "nsswitch.conf"):
        source = Path("/etc") / name
        if source.exists():
            target = sandbox / "etc" / name
            target.touch()
            _mount("--bind", str(source), str(target))
    # Bound rather than a fresh procfs, which would need a pid namespace this
    # harness has no use for.
    _mount("--rbind", "/proc", str(sandbox / "proc"))
    _mount("--rbind", "/dev", str(sandbox / "dev"))
    # /usr is read-only above, so the staging root gets its own writable mount.
    (sandbox / "usr/local/lib").mkdir(parents=True, exist_ok=True)
    _mount("-t", "tmpfs", "tmpfs", str(sandbox / "usr/local/lib"))
    # The repository, at the same absolute path, so the bridge under test is
    # the file in this checkout and not a copy.
    repo = sandbox / str(ROOT).lstrip("/")
    repo.mkdir(parents=True, exist_ok=True)
    _mount("--bind", str(ROOT), str(repo))
    for directory in (sandbox, sandbox / "opt", sandbox / "etc", sandbox / "usr/local/lib"):
        os.chown(directory, 0, 0)
        os.chmod(directory, 0o755)
    os.chmod(sandbox / "tmp", 0o1777)
    os.chroot(sandbox)
    os.chdir("/")
    assert os.stat("/").st_uid == 0, "the sandbox root is still not root-owned"


def namespace_child() -> None:
    _enter_root_owned_filesystem()
    GRANT_ROOT.mkdir(parents=True, exist_ok=True)
    SHARED_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(SHARED_ROOT, 0o755)
    _install_sudo_shim()
    end_to_end()


def main() -> int:
    command = [sys.executable, str(Path(__file__).resolve()), "--namespace-child"]
    if os.geteuid() != 0:
        command = ["unshare", "--user", "--map-auto", "--map-root-user", "--mount", *command]
    subprocess.run(command, check=True)
    print("tenant_control_bridge_e2e_test: ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--namespace-child":
        namespace_child()
    else:
        raise SystemExit(main())
