#!/usr/bin/env python3
"""SYRD-786: `switchyard teardown <slug>` removes the whole tenant after one preview and the slug typed once.

The User: "the flags are supposed to be used when I want to deviate from the norm. I should only need switchyard
teardown test12". And the teardowns of test11 and test12 left half a tenant: the account (its panes' tmux servers and
agents still running, so userdel refused it), the home, the linger file, the desktop grant's ACL entries, and root's
rollout journal group-owned by a gid the next account was given.

Here, unprivileged, the real `switchyard_teardown_command` runs against a disposable home with a runner that records
each command (and carries out the `rm` ones inside the disposable tree), in a child on a real pty so the slug is typed
to the real `input`:
* the slug typed: the whole tenant's actions run -- the desktop grant, every process, linger, the home, the account,
  root's records -- and the disposable home is gone;
* a wrong slug, or no terminal and no --yes: nothing destructive runs;
* each --keep-* keeps exactly its part; the old flags change nothing;
* the Switchyard project's own board, and the project this command runs from, are refused.
And `teardown_tenant`'s own steps on disposable trees: root's records, the desktop grant, the agent-account guard, the
linger residue, and the process stop's refusal of the account running it. That the process stop ends a real lingering
tenant is measured as root in a disposable VM (docs/syrd-786-whole-tenant-teardown.md).
"""

from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import team_launcher, teardown_tenant  # noqa: E402

CHECKS = 0
SLUG = "t786"
OWNER = "t786-agent"


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


#: The child: the real command, its runner recording every command into `calls.json` and carrying out `rm` inside
#: the disposable tree only. Arguments: the tree, the disposable root, then the command's flags as JSON.
CHILD = r'''
import builtins, json, subprocess, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
from scripts import team_launcher
work = Path(sys.argv[2])
flags = json.loads(sys.argv[3])
calls = []
def runner(argv, **_kwargs):
    argv = [str(a) for a in argv]
    calls.append(argv)
    (work / "calls.json").write_text(json.dumps(calls))
    if argv[:1] == ["rm"] and all(not a.startswith("/") or a.startswith(str(work)) for a in argv[1:]):
        subprocess.run(argv, check=False)
    if argv[:2] == ["psql", "-XAt"]:
        return subprocess.CompletedProcess(argv, 0, "0\n", "")
    if argv[:1] == ["du"]:
        return subprocess.CompletedProcess(argv, 0, "12K\t" + argv[-1] + "\n", "")
    return subprocess.CompletedProcess(argv, 0, "", "")
team_launcher.read_host_desktop_approval = lambda path=None: {}
try:
    code = team_launcher.switchyard_teardown_command(
        "t786", registry_dir=work / "registry", config_dir=work / "config", home_base=work / "home",
        runner=runner, input_func=builtins.input, **flags)
    print(f"\r\nRESULT {code}", flush=True)
except SystemExit as stop:
    print(f"\r\nRESULT refused {stop}", flush=True)
'''


def tree() -> Path:
    work = Path(tempfile.mkdtemp(prefix="syrd786."))
    for name in ("registry", "config", "home/t786-agent/Projects/t786"):
        (work / name).mkdir(parents=True)
    (work / "home" / OWNER / "notes.txt").write_text("work\n", encoding="utf-8")
    return work


def child(work: Path, *, typed: str | None, flags: dict | None = None) -> tuple[str, list[list[str]]]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX", "SUDO_"))}
    argv = [sys.executable, "-c", CHILD, str(ROOT), str(work), json.dumps(flags or {})]
    if typed is None:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, env=env)
        out = done.stdout + done.stderr
    else:
        master, slave = pty.openpty()
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        chunks, sent, deadline = [], False, time.monotonic() + 120
        while proc.poll() is None and time.monotonic() < deadline:
            ready, _w, _x = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunks.append(os.read(master, 65536).decode("utf-8", "replace"))
                except OSError:
                    break
            if not sent and "to remove it: " in "".join(chunks):
                os.write(master, (typed + "\n").encode())
                sent = True
        if proc.poll() is None:
            proc.kill()
        try:
            while select.select([master], [], [], 0.2)[0]:
                chunks.append(os.read(master, 65536).decode("utf-8", "replace"))
        except OSError:
            pass
        os.close(master)
        out = "".join(chunks)
    calls_file = work / "calls.json"
    calls = json.loads(calls_file.read_text()) if calls_file.exists() else []
    return out, calls


READ_ONLY = (("psql", "-XAt"), ("du", "-sh"), ("ps", "-o"))


def destructive(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if tuple(call[:2]) not in READ_ONLY]


def joined(calls: list[list[str]]) -> str:
    return "\n".join(" ".join(call) for call in calls)


# -- the typed slug -----------------------------------------------------------------------------------------------------
def test_typing_the_slug_removes_the_whole_tenant() -> None:
    work = tree()
    out, calls = child(work, typed=SLUG)
    ran = joined(calls)
    check("Type t786 to remove it: " in out and "teardown of t786 removes the whole tenant" in out
          and "owner home: " + str(work / "home" / OWNER) + " (12K)" in out,
          f"one preview, the home's size in it, then the slug asked once: {out[-1500:]}")
    check("RESULT 0" in out and "teardown complete for t786" in out, f"typed, the whole tenant goes: {out[-800:]}")
    for part in ("withdraw-desktop t786-agent", "loginctl disable-linger t786-agent", "/var/lib/systemd/linger/t786-agent",
                 "for signal in TERM KILL", 'pkill "-$signal" -U "$owner_uid"', 'pkill "-$signal" -u "$owner_uid"',
                 "DROP DATABASE IF EXISTS", "root-records t786 t786-agent",
                 f"rm -rf -- {work}/home/t786-agent", "userdel t786-agent"):
        check(part in ran, f"{part!r} is among what ran: {ran[-1500:]}")
    positions = {part: ran.index(part) for part in ("withdraw-desktop", "disable-linger", "for signal in TERM KILL",
                                                    f"rm -rf -- {work}/home/t786-agent", "userdel")}
    check(positions["withdraw-desktop"] < positions["disable-linger"] < positions["for signal in TERM KILL"]
          < positions[f"rm -rf -- {work}/home/t786-agent"] < positions["userdel"],
          f"the desktop grant and every process before the home, and the account last: {positions}")
    check(not (work / "home" / OWNER).exists(), "and the disposable home is really gone")


def test_a_wrong_slug_changes_nothing() -> None:
    work = tree()
    out, calls = child(work, typed="t785")
    check("RESULT refused" in out and "'t785' is not t786; nothing was changed" in out, f"refused: {out[-600:]}")
    check(destructive(calls) == [] and (work / "home" / OWNER / "notes.txt").exists(),
          f"nothing but the read-only questions ran, and the home is untouched: {destructive(calls)}")


def test_no_terminal_and_no_yes_is_refused() -> None:
    work = tree()
    out, calls = child(work, typed=None)
    check("RESULT refused" in out and "pass --yes (or --confirm <slug>)" in out and "Nothing was changed." in out,
          f"nobody to type the slug: refused, naming the flag: {out[-600:]}")
    check(destructive(calls) == [], f"and nothing destructive ran: {destructive(calls)}")
    out, calls = child(work, typed=None, flags={"confirm": "t785"})
    check("RESULT refused" in out and "--confirm 't785' does not name t786; nothing was changed" in out
          and destructive(calls) == [], f"a --confirm naming another slug: refused, nothing run: {out[-400:]}")
    out, calls = child(work, typed=None, flags={"yes": True})
    check("RESULT 0" in out and "userdel t786-agent" in joined(calls), f"--yes runs it unattended: {out[-400:]}")


# -- the flags that keep a part --------------------------------------------------------------------------------------
def plan_labels(**flags) -> list[str]:
    said: list[str] = []
    with tempfile.TemporaryDirectory(prefix="syrd786.") as raw:
        work = Path(raw)
        for name in ("registry", "config", "home"):
            (work / name).mkdir()
        with patch.object(team_launcher, "read_host_desktop_approval", lambda path=None: {}):
            team_launcher.switchyard_teardown_command(
                SLUG, dry_run=True, registry_dir=work / "registry", config_dir=work / "config", home_base=work / "home",
                runner=lambda argv, **_k: subprocess.CompletedProcess(argv, 0, "0\n", ""), print_func=said.append,
                **flags)
        return [line.split(". ", 1)[1].replace(str(work), "<work>") for line in said
                if line.strip()[:1].isdigit() and ". " in line]


def test_each_keep_flag_keeps_exactly_its_part() -> None:
    whole = plan_labels()
    parts = {"home": "remove owner home directory", "user": "remove owner user account",
             "linger": "disable linger", "board": "drop PostgreSQL database"}
    for name, fragment in parts.items():
        check(any(fragment in label for label in whole), f"by default the {name} goes: {whole}")
    for flag, gone in (("keep_home", {"home"}), ("keep_user", {"user", "linger"}), ("keep_board_data", {"board"})):
        labels = plan_labels(**{flag: True})
        for name, fragment in parts.items():
            present = any(fragment in label for label in labels)
            check(present == (name not in gone), f"--{flag.replace('_', '-')}: the {name} is "
                                                 f"{'kept' if name in gone else 'still removed'}: {labels}")
        check(any("stop every process" in label for label in labels),
              f"--{flag.replace('_', '-')}: the tenant's processes are stopped all the same: {labels}")
    check(plan_labels(destroy_registered_tenant=True, drop_nonempty_board=True, remove_owner_home=True,
                      remove_owner_user=True) == whole, "the old flags are accepted and change nothing")


# -- the guards that stay ---------------------------------------------------------------------------------------------
def test_the_switchyard_project_and_the_running_project_are_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd786.") as raw:
        work = Path(raw)
        registry = work / "syrd.json"
        registry.write_text(json.dumps({"schema": "switchyard.project-registry.v1", "slug": "syrd", "name": "Switchyard"}))
        plan = SimpleNamespace(project="syrd", owner_user="switchyard-svc", listener_unit="syrd-notify.service",
                               board_unit="syrd-ticket-board.service", board_root=str(work / "live"),
                               tmpfiles_name="syrd.conf", tenant_control_sudoers_name="47-syrd-control",
                               polkit_name="50-syrd.rules", database="syrd_board")
        calls: list = []
        with patch.object(team_launcher, "_teardown_project_context",
                          lambda project, **_k: (plan, work / "checkout", registry, True)), \
                patch.object(team_launcher, "read_host_desktop_approval", lambda path=None: {}):
            try:
                team_launcher.switchyard_teardown_command(
                    "syrd", yes=True, home_base=work, print_func=lambda _l: None,
                    runner=lambda argv, **_k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "0\n", ""))
                got = "ran"
            except SystemExit as stop:
                got = str(stop)
        check("the Switchyard project's own board; it is never torn down" in got
              and destructive([list(c) for c in calls]) == [],
              f"syrd itself: refused, nothing run: {got} {calls}")
    with patch.dict(os.environ, {"TICKET_BOARD_PROJECT": "test14"}):
        check("is the project this command runs from" in teardown_tenant.self_tenant_refusal("test14"),
              "and the project of the pane running it")
    check(teardown_tenant.self_tenant_refusal("test14", registry_name="test14") == "", "any other tenant is not")


def test_only_a_tenants_agent_account_is_removed() -> None:
    check("not a tenant's agent account" in teardown_tenant.agent_account_refusal("eric", group_names=lambda _u: {"wheel"})
          and "sudo" in teardown_tenant.agent_account_refusal("ops", group_names=lambda _u: {"sudo", "ops"}),
          "a member of an administrators' group is refused")
    check(teardown_tenant.agent_account_refusal("t786-agent", group_names=lambda _u: {"t786-agent", "t786-roles"}) == "",
          "a tenant's agent account is not")


def test_the_process_stop_never_acts_on_the_account_running_it() -> None:
    import pwd

    me = pwd.getpwuid(os.getuid()).pw_name
    for owner in (me, "root"):
        result = subprocess.run(["bash", "-c", teardown_tenant.process_stop_script(owner)], capture_output=True,
                                text=True, timeout=30)
        check(result.returncode != 0 and "refusing to stop the processes" in result.stderr,
              f"{owner}: refused before any signal: {result.returncode} {result.stderr[:200]}")
    result = subprocess.run(["bash", "-c", teardown_tenant.process_stop_script("syrd786-absent")], capture_output=True,
                            text=True, timeout=30)
    check(result.returncode == 0 and "already absent" in result.stdout, "an account already gone: nothing to stop")


def test_the_command_refuses_a_persons_account() -> None:
    def run(**flags) -> tuple[str, list]:
        calls: list = []
        with tempfile.TemporaryDirectory(prefix="syrd786.") as raw:
            work = Path(raw)
            for name in ("registry", "config", "home"):
                (work / name).mkdir()
            with patch.object(teardown_tenant, "agent_account_refusal",
                              lambda owner, **_k: f"{owner} belongs to wheel; that is a person's account"), \
                    patch.object(team_launcher, "read_host_desktop_approval", lambda path=None: {}):
                try:
                    team_launcher.switchyard_teardown_command(
                        SLUG, yes=True, registry_dir=work / "registry", config_dir=work / "config",
                        home_base=work / "home", print_func=lambda _l: None,
                        runner=lambda argv, **_k: calls.append(list(argv)) or subprocess.CompletedProcess(argv, 0, "0\n", ""),
                        **flags)
                    return "ran", calls
                except SystemExit as stop:
                    return str(stop), calls
    got, calls = run()
    check("refusing to tear down t786: t786-agent belongs to wheel" in got and destructive(calls) == [],
          f"an owner in an administrators' group: refused before anything runs: {got}")
    got, calls = run(keep_user=True)
    check("t786-agent belongs to wheel" in got and destructive(calls) == [],
          f"with --keep-user too: its processes and home would still be acted on (SYRD-786 Audit): {got}")


def registry_with(work: Path, slug: str, *, run_as_user: str = "", plan_owner: str = "", slug_field: bool = True) -> Path:
    registry = work / "registry"
    registry.mkdir(exist_ok=True)
    config_dir = work / "configs" / slug
    config_dir.mkdir(parents=True)
    config = config_dir / f"{slug}.json"
    config.write_text(json.dumps({"project": slug, **({"run_as_user": run_as_user} if run_as_user else {})}))
    if plan_owner:
        (config_dir / "plan.json").write_text(json.dumps({"owner_user": plan_owner}))
    entry = {"schema": "switchyard.project-registry.v1", "name": slug.upper(), "config_path": str(config)}
    if slug_field:
        entry["slug"] = slug
    (registry / f"{slug}.json").write_text(json.dumps(entry))
    return registry


def dry_run(slug: str, work: Path, **flags) -> tuple[list[str], list]:
    said: list[str] = []
    calls: list = []
    (work / "home").mkdir(exist_ok=True)
    (work / "config").mkdir(exist_ok=True)
    with patch.object(team_launcher, "read_host_desktop_approval", lambda path=None: {}):
        code = team_launcher.switchyard_teardown_command(
            slug, dry_run=True, registry_dir=work / "registry", config_dir=work / "config", home_base=work / "home",
            print_func=said.append,
            runner=lambda argv, **_k: calls.append(list(argv)) or subprocess.CompletedProcess(argv, 0, "0\n", ""), **flags)
    check(code == 0, f"a dry run answers 0: {said[-3:]}")
    return said, calls


def real_run(slug: str, work: Path, **flags) -> tuple[str, list]:
    calls: list = []
    with patch.object(team_launcher, "read_host_desktop_approval", lambda path=None: {}):
        try:
            team_launcher.switchyard_teardown_command(
                slug, yes=True, registry_dir=work / "registry", config_dir=work / "config", home_base=work / "home",
                print_func=lambda _l: None,
                runner=lambda argv, **_k: calls.append(list(argv)) or subprocess.CompletedProcess(argv, 0, "0\n", ""),
                **flags)
            return "ran", calls
        except SystemExit as stop:
            return str(stop), calls


def test_another_tenants_account_is_refused() -> None:
    # Audit's probe: `teardown stellaris` (default owner stellaris-agent, mefp's account), and --owner-user naming a
    # registered tenant's account, each planned userdel of a live tenant with no refusal.
    with tempfile.TemporaryDirectory(prefix="syrd786.") as raw:
        work = Path(raw)
        registry_with(work, "mefp", run_as_user="stellaris-agent", slug_field=False)
        registry_with(work, "pie", plan_owner="pie-svc")  # not the default: read from its plan
        registry_with(work, "otto")
        for slug, flags, owner, tenant in (("stellaris", {}, "stellaris-agent", "mefp"),
                                           ("test9", {"owner_user": "stellaris-agent"}, "stellaris-agent", "mefp"),
                                           ("leftover", {"owner_user": "pie-svc"}, "pie-svc", "pie"),
                                           ("leftover", {"owner_user": "otto-agent"}, "otto-agent", "otto")):
            said, calls = dry_run(slug, work, **flags)
            want = f"switchyard: refused: {owner} is {tenant}'s account; tear down {tenant} instead"
            check(want in said and destructive(calls) == [], f"{slug} {flags}: the dry run names the tenant: {said[-4:]}")
            for keep in ({}, {"keep_user": True}):
                got, calls = real_run(slug, work, **flags, **keep)
                check(f"refusing to tear down {slug}: {owner} is {tenant}'s account" in got and destructive(calls) == [],
                      f"{slug} {flags} {keep}: refused, nothing run: {got}")
        check(teardown_tenant.other_tenant_refusal("pie", "pie-svc", registry_dir=work / "registry",
                                                   owner_home=work / "home" / "pie-svc") == "",
              "a tenant's own account is not refused as another's")
        (work / "registry" / "notes.json").write_text(json.dumps({"schema": "something.else"}))
        (work / "registry" / "linked.json").symlink_to(work / "absent.json")
        got, calls = real_run("leftover", work, owner_user="leftover-agent")
        # Not refused: every action runs (the stub runner's read-back then sees "0" as an account still there).
        check("refusing" not in got and any("userdel leftover-agent" in " ".join(call) for call in calls),
              f"a file of another schema, or a link, in the registry is not an entry: {got}")
        check("refusing" not in got and any("userdel leftover-agent" in " ".join(call) for call in calls),
              f"a leftover whose account no tenant runs as is removed: {got}")


def test_an_unreadable_registry_entry_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd786.") as raw:
        work = Path(raw)
        registry = registry_with(work, "otto")
        (registry / "sapi.json").write_text(json.dumps({"schema": "switchyard.project-registry.v1",
                                                        "config_path": str(work / "absent" / "sapi.json")}))
        got, calls = real_run("leftover", work)
        check("cannot read configuration" in got and str(work / "absent" / "sapi.json") in got
              and "could be leftover-agent" in got and destructive(calls) == [],
              f"an entry whose account cannot be read: refused, naming it: {got}")
        (registry / "sapi.json").write_text("{not json")
        got, calls = real_run("leftover", work, keep_user=True)
        check("cannot read registry entry" in got and destructive(calls) == [], f"a broken entry, with --keep-user too: {got}")


def test_a_home_holding_another_projects_state_is_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd786.") as raw:
        work = Path(raw)
        registry_with(work, "otto")
        (work / "home" / "leftover-agent" / ".local" / "state" / "switchyard" / "projects" / "gone").mkdir(parents=True)
        (work / "home" / "leftover-agent" / ".local" / "state" / "switchyard" / "projects" / "leftover").mkdir()
        got, calls = real_run("leftover", work)
        check("leftover-agent's home holds Switchyard state for gone" in got and destructive(calls) == [],
              f"a home holding another slug's state (its registry record gone): refused: {got}")


def test_root_records_are_swept_by_gid_only_when_the_group_is_private() -> None:
    me = SimpleNamespace(pw_name="t786-agent", pw_gid=1022)
    others = [me, SimpleNamespace(pw_name="other-agent", pw_gid=1023)]
    check(teardown_tenant.private_group_gid(me, accounts=lambda: others, group_name=lambda gid: "t786-agent") == 1022,
          "its own group: swept by gid")
    check(teardown_tenant.private_group_gid(me, accounts=lambda: others, group_name=lambda gid: "users") is None,
          "a shared group by name (users, a roles group): not by gid")
    shared = others + [SimpleNamespace(pw_name="intruder", pw_gid=1022)]
    check(teardown_tenant.private_group_gid(me, accounts=lambda: shared, group_name=lambda gid: "t786-agent") is None,
          "another account's primary group too: not by gid")


def test_a_left_linger_file_is_residue() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd786.") as raw:
        work = Path(raw)
        for name in ("registry", "config", "home", "linger"):
            (work / name).mkdir()
        (work / "linger" / OWNER).write_text("")
        (work / "home" / OWNER).mkdir()
        with patch.object(teardown_tenant, "LINGER_DIR", work / "linger"), \
                patch.object(team_launcher, "read_host_desktop_approval", lambda path=None: {}):
            try:
                team_launcher.switchyard_teardown_command(
                    SLUG, confirm=SLUG, registry_dir=work / "registry", config_dir=work / "config",
                    home_base=work / "home", print_func=lambda _l: None,
                    runner=lambda argv, **_k: subprocess.CompletedProcess(argv, 0, "0\n" if argv[:1] == ["psql"] else "", ""))
                got = "complete"
            except SystemExit as stop:
                got = str(stop)
    check("did NOT remove all of t786" in got and f"the linger file {work / 'linger' / OWNER} is still there" in got
          and f"the home {work / 'home' / OWNER} is still there" in got,
          f"a linger file or a home left behind fails the teardown, naming each: {got}")


# -- teardown_tenant's steps on disposable trees ---------------------------------------------------------------------
def test_roots_records_are_removed_kept_or_re_owned() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd786.") as raw:
        work = Path(raw)
        staged = work / "usr-local-lib" / SLUG
        provision = work / "etc" / "provision" / SLUG
        rollout = work / "var-lib" / "rollout" / SLUG
        other = work / "etc" / "publish" / "t786.json"
        for directory in (staged, provision, rollout, other.parent):
            directory.mkdir(parents=True)
        (staged / "control-grant.json").write_text("{}")
        (provision / "plan.json").write_text("{}")
        (rollout / "0001.json").write_text("{}")
        other.write_text("{}")
        os.chmod(rollout, 0o750)
        sudoers = work / "sudoers.d" / "48-t786-publish"
        grant = work / "publish" / "t786.json"
        key = work / "publish" / "t786-publish-key"
        staging = work / "publish-staging" / "t786.git"
        other_grant = work / "publish" / "t787.json"
        for path in (sudoers, grant, key, key.with_name(key.name + ".pub"), other_grant):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        (staging / "objects").mkdir(parents=True)
        chowned: list = []
        said = teardown_tenant.settle_root_records(
            SLUG, uid=os.getuid(), gid=None, staged_tooling_root=work / "usr-local-lib", provision_dir=provision,
            rollout_root=work / "var-lib" / "rollout", record_dirs=(work / "etc",),
            publication_files=(sudoers, grant, key, key.with_name(key.name + ".pub"), work / "publish" / "absent"),
            publication_staging=staging, chown=lambda path, uid, gid: chowned.append((path, uid, gid)))
        check(not staged.exists() and not provision.exists(), "its staged tooling and root's provisioning record go")
        check(not sudoers.exists() and not grant.exists() and not key.exists()
              and not key.with_name(key.name + ".pub").exists() and not staging.exists() and other_grant.exists(),
              "its publication grant goes -- the sudoers rule naming its account, root's grant, key and staging -- "
              "and another tenant's stays")
        check(rollout.is_dir() and (rollout / "0001.json").exists() and (rollout.stat().st_mode & 0o777) == 0o700
              and (str(rollout), 0, 0) in chowned and (str(rollout / "0001.json"), 0, 0) in chowned,
              f"root's rollout journal is kept, re-owned to root and closed: {chowned}")
        check((str(other), 0, 0) in chowned, f"anything else the uid owns under root's directories is re-owned: {chowned}")
        check(any("kept root's rollout journal" in line and "re-owned to root" in line for line in said)
              and any(f"removed its staged tooling {staged}" == line for line in said),
              f"and it says what it removed and what it kept, and why: {said}")


def test_the_desktop_grant_and_its_acl_entries_are_withdrawn() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd786.") as raw:
        work = Path(raw)
        runtime = work / "run-user" / "1000"
        grants = work / "run-desktop" / "1000"
        for directory in (runtime, grants):
            directory.mkdir(parents=True)
        (runtime / "wayland-1").write_text("")
        (runtime / "pulse").write_text("")
        (grants / "switchyard-desktop-1000-1022-t786").write_text("")
        (work / "run-user" / "1001").mkdir()
        (work / "run-user" / "1001" / "wayland-0").write_text("")
        named = {str(runtime), str(runtime / "wayland-1"), str(grants), str(grants / "switchyard-desktop-1000-1022-t786")}
        setfacl: list = []

        def runner(argv, **_kwargs):
            if argv[0] == "getfacl":
                entries = ("user::rwx\nuser:1022:--x\nuser:1023:--x\n" if argv[-1] in named
                           else "user::rwx\nuser:1023:--x\n")  # another desktop user's: only another tenant
                return subprocess.CompletedProcess(argv, 0, entries, "")
            setfacl.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        uninstalled: list = []
        home = work / "gui-home"
        record_dir = home / ".config" / "switchyard" / "desktop-access"
        record_dir.mkdir(parents=True)
        record = record_dir / f"switchyard-desktop-{os.getuid()}-1022-t786.json"
        record.write_text(json.dumps({"mode": "wayland", "project": SLUG, "tenant_user": OWNER}))
        policies = teardown_tenant.installed_grant_policies(
            1022, accounts=lambda: [SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid())])
        said = teardown_tenant.withdraw_desktop(1022, policies=policies, user_runtime_root=work / "run-user",
                                                desktop_grant_root=work / "run-desktop", runner=runner,
                                                uninstall=uninstalled.append)
        check(uninstalled == [{"mode": "wayland", "project": SLUG, "tenant_user": OWNER}],
              f"the grant the desktop user recorded is uninstalled: {uninstalled}")
        removed = sorted(call[-1] for call in setfacl)
        check(removed == sorted(named) and all(call[:4] == ["setfacl", "--no-mask", "-x", "u:1022"] for call in setfacl),
              f"uid 1022's entries go from the runtime, its Wayland socket, the grant directory and its socket, and "
              f"nothing else -- not another tenant's: {setfacl}")
        check(not any("pulse" in call[-1] for call in setfacl), "a runtime file that is not a Wayland socket is left alone")
        check(len(said) == 1 + len(named), f"and it says each: {said}")
        foreign = record_dir / f"switchyard-desktop-{os.getuid()}-1022-other.json"
        foreign.symlink_to(record)
        check(len(teardown_tenant.installed_grant_policies(
            1022, accounts=lambda: [SimpleNamespace(pw_dir=str(home), pw_uid=os.getuid())])) == 1,
              "a link in the desktop user's records is not followed")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"whole_tenant_teardown_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
