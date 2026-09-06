#!/usr/bin/env python3
"""SYRD-53: a named-user ACL cannot be granted before the account exists.

The live SYRD-19 rollout stopped in the generated role-accounts script with

    setfacl: Option -m: Invalid argument near character 3

because the control-role grant was emitted before the loop that creates the
per-role accounts, so `setfacl -m u:<project>-<control role>:--x ...` ran before
`useradd` had made that account. Group grants had already succeeded, which is
why the failure was partial rather than total.

The static case is data-driven over every principal any generated artifact
names. The executing case really runs the generated bash under `set -euo
pipefail` against shims that model the account database and reject an ACL for
an unknown principal exactly the way setfacl does.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher
from scripts.ticket_board.project_provision import (
    build_plan,
    render_operator_commands,
    write_artifacts,
)

#: Every principal form an ACL entry can carry, default entries included.
ACL_PRINCIPAL = re.compile(r"(?:^|[\s,])(?:d:)?(u|g):([A-Za-z0-9_][A-Za-z0-9_.-]*):")
CREATES_USER = re.compile(r"\buseradd\b.*?'([A-Za-z0-9_][A-Za-z0-9_.-]*)'\s*$")
CREATES_GROUP = re.compile(r"\bgroupadd\b.*?'([A-Za-z0-9_][A-Za-z0-9_.-]*)'\s*$")

SHIM_PROGRAMS = (
    "sudo",
    "setfacl",
    "useradd",
    "groupadd",
    "gpasswd",
    "usermod",
    "getent",
    "install",
    "chown",
    "chmod",
    "mkdir",
    "cp",
    "mv",
    "rm",
    "ln",
    "loginctl",
    "visudo",
    "systemctl",
    "switchyard",
    # Staged programs this host does not have; the ordering under test does not
    # depend on what they do.
    "env",
    "find",
    "git",
)

SHIM_SOURCE = r'''#!/usr/bin/env python3
"""A host whose account database is a file, and whose setfacl means it."""
import json, os, sys
from pathlib import Path

state_path = Path(os.environ["SHIM_STATE"])
state = json.loads(state_path.read_text())
name = Path(sys.argv[0]).name
args = sys.argv[1:]


def save():
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))


def record(status=0):
    state["log"].append({"program": name, "args": args, "status": status})
    save()


if name == "sudo":
    record()
    while args and args[0].startswith("-"):
        option = args.pop(0)
        if option in ("-u", "-g", "-p", "-C", "--user", "--group") and args:
            args.pop(0)
    if not args:
        sys.exit(0)
    try:
        os.execvp(args[0], args)
    except FileNotFoundError:
        # A program the real migration stages into the tenant's tooling
        # directory, which this fixture does not populate. Recorded and
        # succeeded: what is under test is the order of the account and ACL
        # steps, and no staged program takes part in it.
        state["absent"].append(args)
        save()
        sys.exit(0)
    except OSError as exc:
        sys.stderr.write("shim sudo: cannot run %r: %s\n" % (args, exc))
        sys.exit(127)

record()

if name == "getent":
    database, key = (args + ["", ""])[:2]
    table = {"passwd": "users", "group": "groups"}.get(database)
    sys.exit(0 if table and key in state[table] else 2)

if name == "useradd":
    account = args[-1]
    if account in state["users"]:
        sys.exit(9)
    state["users"].append(account)
    state["groups"].append(account)
    save()
    sys.exit(0)

if name == "groupadd":
    group = args[-1]
    if group in state["groups"]:
        sys.exit(9)
    state["groups"].append(group)
    save()
    sys.exit(0)

if name == "gpasswd":
    if args[:1] == ["-a"]:
        account, group = args[1], args[2]
        if account not in state["users"]:
            sys.stderr.write("gpasswd: user '%s' does not exist\n" % account)
            sys.exit(1)
        if group not in state["groups"]:
            sys.stderr.write("gpasswd: group '%s' does not exist\n" % group)
            sys.exit(1)
    sys.exit(0)

if name == "setfacl":
    # Only the ACL specification carries principals; -m/-x/-R/-d are options.
    for index, argument in enumerate(args):
        if argument in ("-m", "-x", "--modify", "--remove"):
            continue
        if argument.startswith("-"):
            continue
        if index == 0 or not args[index - 1] in ("-m", "-x", "--modify", "--remove"):
            continue
        for entry in argument.split(","):
            parts = entry.split(":")
            if parts and parts[0] == "d":
                parts = parts[1:]
            if len(parts) < 2 or parts[0] not in ("u", "g"):
                continue
            table = "users" if parts[0] == "u" else "groups"
            if parts[1] and parts[1] not in state[table]:
                # The real message, from the real failure this reproduces.
                sys.stderr.write("setfacl: Option -m: Invalid argument near character 3\n")
                sys.exit(1)
    state["acls"].append(" ".join(args))
    save()
    sys.exit(0)

sys.exit(0)
'''


def _shim_host(directory: Path, *, users: tuple[str, ...] = (), groups: tuple[str, ...] = ()) -> tuple[Path, Path]:
    """A PATH whose account tools are the shims, and the state they share.

    The starting state is the one a tenant is actually migrated from: the board
    service account and the project owner exist, and no role account does.
    """
    binaries = directory / "bin"
    binaries.mkdir(parents=True, exist_ok=True)
    shim = binaries / "_shim.py"
    shim.write_text(SHIM_SOURCE, encoding="utf-8")
    shim.chmod(0o755)
    for program in SHIM_PROGRAMS:
        path = binaries / program
        path.write_text(
            "#!/bin/sh\nexec python3 "
            + str(shim)
            + ' "$@"\n',
            encoding="utf-8",
        )
        path.chmod(0o755)
        # argv[0] has to be the program's own name for the shim to dispatch.
        path.write_text(
            '#!/bin/sh\nexec python3 -c "import os,sys; sys.argv[0]=\'%s\'; '
            "exec(open('%s').read())\" \"$@\"\n" % (program, shim),
            encoding="utf-8",
        )
        path.chmod(0o755)
    state = directory / "host-state.json"
    state.write_text(
        json.dumps(
            {
                "users": list(users),
                "groups": [*groups, *users],
                "acls": [],
                "absent": [],
                "log": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return binaries, state


def _fixture(tmp: Path, *, project: str = "porter") -> tuple[object, Path]:
    """A tenant whose control role is not called "director"."""
    provision = tmp / "provision"
    provision.mkdir(parents=True, exist_ok=True)
    repository = tmp / "repository"
    repository.mkdir(parents=True, exist_ok=True)
    plan = build_plan(
        project=project,
        project_name=project.title(),
        owner_user=f"{project}-agent",
        owner_home=tmp / "home",
        source_repo=ROOT,
    )
    write_artifacts(plan, provision, enable_owner_linger=False)
    config_path = launcher.write_new_project_launcher_artifacts(
        plan, provision, repository=repository, print_func=lambda _text: None
    )
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    roles = [role["role"] for role in stored["roles"]]
    assert {"director", "designer"} <= set(roles), roles
    # The control role is the one holding the capabilities, and it is
    # deliberately not the one called "director": a role by that name stays in
    # the configuration without them, so a grant aimed at the literal name is
    # visibly the wrong one (SYRD-49).
    for role in stored["roles"]:
        if role["role"] != "designer":
            continue
        for field in ("role", "run_as_user", "target", "tmux_session", "workdir"):
            role[field] = role[field].replace("designer", "conductor")
    stored["workflow"] = {
        "roles": [
            {
                "name": role["role"],
                "active": True,
                "capabilities": (
                    ["add_comment", "set_manually_controlled", "merge"]
                    if role["role"] == "conductor"
                    else ["add_comment"]
                ),
            }
            for role in stored["roles"]
        ]
    }
    config_path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return launcher.load_project_config(project, config_path), config_path


def _ordering_violations(script: str) -> list[str]:
    """Principals an ACL names before the script has created them."""
    created: dict[str, int] = {}
    granted: dict[str, int] = {}
    for index, line in enumerate(script.splitlines()):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for match in (CREATES_USER.search(stripped), CREATES_GROUP.search(stripped)):
            if match:
                created.setdefault(match.group(1), index)
        if "setfacl" in stripped:
            for _kind, principal in ACL_PRINCIPAL.findall(stripped):
                granted.setdefault(principal, index)
    violations = []
    for principal, at in sorted(granted.items()):
        if principal in created and created[principal] > at:
            violations.append(f"{principal}: granted at line {at}, created at line {created[principal]}")
    return violations


def test_no_generated_script_grants_before_it_creates() -> None:
    """Every principal an artifact both creates and grants, in that order."""
    with tempfile.TemporaryDirectory(prefix="acl-order-static.") as tmp:
        config, config_path = _fixture(Path(tmp))
        migration = launcher.render_role_account_migration(config, config_path=config_path)
        assert not _ordering_violations(migration), _ordering_violations(migration)
        plan = build_plan(
            project=config.project,
            owner_user=config.run_as_user,
            owner_home=Path(tmp) / "home",
            source_repo=ROOT,
        )
        operator = render_operator_commands(plan, enable_owner_linger=False)
        assert not _ordering_violations(operator), _ordering_violations(operator)


def test_the_grant_names_the_capability_derived_control_role() -> None:
    """Not the role called "director": the one the workflow gives the capabilities."""
    with tempfile.TemporaryDirectory(prefix="acl-order-control-role.") as tmp:
        config, config_path = _fixture(Path(tmp))
        migration = launcher.render_role_account_migration(config, config_path=config_path)
        principals = {
            principal
            for line in migration.splitlines()
            if "setfacl" in line
            for kind, principal in ACL_PRINCIPAL.findall(line)
            if kind == "u"
        }
        assert f"{config.project}-conductor" in principals, principals
        assert f"{config.project}-director" not in principals, principals


def test_the_guard_says_why_rather_than_setfacl() -> None:
    """If a grant ever gets ahead of its account again, the script says so."""
    with tempfile.TemporaryDirectory(prefix="acl-order-guard.") as tmp:
        root = Path(tmp)
        binaries, state_path = _shim_host(root, users=("boardsvc",))
        guard = launcher.account_existence_guard(["porter-conductor"])
        script = root / "guard.sh"
        script.write_text(
            "\n".join(["#!/usr/bin/env bash", "set -euo pipefail", *guard]) + "\n",
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment["PATH"] = f"{binaries}:{environment.get('PATH', '')}"
        environment["SHIM_STATE"] = str(state_path)
        outcome = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, env=environment
        )
        assert outcome.returncode == 1, outcome
        assert "porter-conductor does not exist yet" in outcome.stderr, outcome.stderr
        assert "out of order" in outcome.stderr, outcome.stderr
        # And it passes once the account is there.
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["users"].append("porter-conductor")
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        assert subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, env=environment
        ).returncode == 0


def executes_the_generated_sequence() -> None:
    """Really run the script, on a host where no role account exists yet."""
    with tempfile.TemporaryDirectory(prefix="acl-order-execute.") as tmp:
        root = Path(tmp)
        config, config_path = _fixture(root)
        binaries, state_path = _shim_host(
            root, users=("boardsvc", config.run_as_user)
        )
        script = root / "role-accounts.sh"
        script.write_text(
            launcher.render_role_account_migration(config, config_path=config_path),
            encoding="utf-8",
        )
        script.chmod(0o755)

        environment = dict(os.environ)
        environment["PATH"] = f"{binaries}:{environment.get('PATH', '')}"
        environment["SHIM_STATE"] = str(state_path)

        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", str(script)],
                capture_output=True,
                text=True,
                env=environment,
            )

        first = run()
        assert first.returncode == 0, (first.returncode, first.stdout[-2000:], first.stderr[-2000:])
        assert "Invalid argument" not in first.stderr, first.stderr

        state = json.loads(state_path.read_text(encoding="utf-8"))
        # The control role's account really was created, and really was granted.
        control_account = f"{config.project}-conductor"
        assert control_account in state["users"], state["users"]
        assert any(control_account in entry for entry in state["acls"]), state["acls"]
        # And in the transcript the creation precedes every grant naming it.
        created_at = next(
            index
            for index, entry in enumerate(state["log"])
            if entry["program"] == "useradd" and entry["args"][-1] == control_account
        )
        granted_at = [
            index
            for index, entry in enumerate(state["log"])
            if entry["program"] == "setfacl"
            and any(f"u:{control_account}:" in argument for argument in entry["args"])
        ]
        assert granted_at, state["log"]
        assert min(granted_at) > created_at, (created_at, granted_at)

        # Rerun safety, which is what the live host needs after its partial
        # attempt: the accounts and group now exist and nothing objects.
        second = run()
        assert second.returncode == 0, (second.returncode, second.stdout[-2000:], second.stderr[-2000:])


def resumes_after_a_partial_attempt() -> None:
    """The live host's state: the group and its ACLs applied, no accounts yet."""
    with tempfile.TemporaryDirectory(prefix="acl-order-resume.") as tmp:
        root = Path(tmp)
        config, config_path = _fixture(root)
        from scripts.ticket_board.project_provision import roles_group_name

        # Exactly where the live rollout stopped: the group exists and its ACLs
        # were applied, and no role account was ever created.
        binaries, state_path = _shim_host(
            root,
            users=("boardsvc", config.run_as_user),
            groups=(roles_group_name(config.project),),
        )

        script = root / "role-accounts.sh"
        script.write_text(
            launcher.render_role_account_migration(config, config_path=config_path),
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment["PATH"] = f"{binaries}:{environment.get('PATH', '')}"
        environment["SHIM_STATE"] = str(state_path)
        outcome = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, env=environment
        )
        assert outcome.returncode == 0, (outcome.returncode, outcome.stderr[-2000:])
        assert "Invalid argument" not in outcome.stderr, outcome.stderr


def main() -> int:
    test_no_generated_script_grants_before_it_creates()
    test_the_grant_names_the_capability_derived_control_role()
    test_the_guard_says_why_rather_than_setfacl()
    executes_the_generated_sequence()
    resumes_after_a_partial_attempt()
    print("team_launcher_role_account_script_order_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
