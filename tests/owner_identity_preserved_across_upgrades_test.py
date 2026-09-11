#!/usr/bin/env python3
"""SYRD-100: an upgrade must not change which key a tenant publishes with.

SYRD-74 made a named key the normal case: the account it was written for held a
good ED25519 key under a nonstandard filename with nothing selecting it, and the
managed `Host github.com` block is what made git offer it.

The upgrade path then called the renderer without a key name. The renderer
defaults to `id_ed25519`, so the live upgrade from 9667659 generated that key,
rewrote the managed block to point at it, and reported `Permission denied
(publickey)` -- for a key GitHub had never seen, while the tenant's actual
repository deploy key sat unused in the same directory.

These cases run the real upgrade against a fixture owner home, so what is
asserted is what the upgrade would do to a tenant's `.ssh`, not what a helper
returns when it is told the answer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from team_launcher_test_helpers import *  # noqa: F401,F403
import team_launcher_upgrade_cutover_test as cutover
from scripts.ticket_board.project_provision import (
    GITHUB_IDENTITY_BEGIN,
    GITHUB_IDENTITY_END,
    compose_ssh_config,
    github_identity_block,
    owner_github_key_path,
    parse_managed_github_identity,
    resolve_owner_github_identity,
)

#: The live tenant's key: a GitHub repository deploy key for ebudai/switchyard.
DEPLOY_KEY = "id_ed25519_ebudai_switchyard"
#: What the renderer produces when it is told nothing, and what the live upgrade
#: generated and selected in place of the working key.
DEFAULT_KEY = "id_ed25519"


def owner_home_with(tmp: Path, *, keys: tuple[str, ...], selected: str = "") -> Path:
    """An owner home holding `keys`, with the managed block naming `selected`."""
    home = tmp / "owner-home"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True, exist_ok=True)
    for name in keys:
        (ssh / name).write_text("PRIVATE KEY PLACEHOLDER\n", encoding="utf-8")
        (ssh / f"{name}.pub").write_text(f"ssh-ed25519 AAAA {name}\n", encoding="utf-8")
    if selected:
        (ssh / "config").write_text(
            compose_ssh_config(
                "Host bastion.invalid\n    User someone\n",
                github_identity_block(str(home), key_name=selected),
            ),
            encoding="utf-8",
        )
    return home


def tenant_with_owner_home(
    tmp: Path, home: Path, *, recorded_key: str = ""
) -> tuple[Path, dict[str, object]]:
    """A declarative tenant whose recorded owner home is the fixture's."""
    config_path, _root = cutover._declarative_tenant(tmp)
    plan_path = config_path.parent / "plan.json"
    plan: dict[str, object] = {}
    if plan_path.is_file():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["owner_home"] = str(home)
    plan["project"] = "porter"
    if recorded_key:
        plan["owner_github_key_name"] = recorded_key
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config_path, plan


def names_key(text: str, key_path: str) -> bool:
    """Whether `text` names exactly this key file.

    A plain substring test is wrong here and quietly so: the deploy key's path
    ends with the default key's path, so `id_ed25519_ebudai_switchyard` contains
    `id_ed25519` and "the default is never named" would pass by accident. The
    boundary is a character that cannot continue a file name; `.pub` still
    counts as naming the same key.
    """
    return re.search(rf"{re.escape(key_path)}(?![\w-])", text) is not None


def identity_scripts(runner: Any) -> list[str]:
    """Every shell script the upgrade handed the runner that touches the key."""
    scripts: list[str] = []
    for call in runner.calls:
        if len(call) >= 3 and call[0] == "sh" and call[1] == "-c" and "ssh" in call[2]:
            scripts.append(call[2])
    return scripts


def run_upgrade(config_path: Path, *, dry_run: bool = False) -> tuple[str, Any]:
    runner = FakeRunner()
    _result, output, _migrations = cutover._upgrade(
        config_path, as_root=True, dry_run=dry_run, runner=runner
    )
    return output, runner


def test_an_upgrade_keeps_the_named_key_the_tenant_publishes_with() -> None:
    """The live regression, driven through the real upgrade command."""
    with tempfile.TemporaryDirectory(prefix="identity-named.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEPLOY_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        before = (home / ".ssh" / "config").read_text(encoding="utf-8")

        output, runner = run_upgrade(config_path)

        scripts = identity_scripts(runner)
        assert scripts, output
        rendered = "\n".join(scripts)
        # The tenant's own key, everywhere the script names one.
        assert names_key(rendered, owner_github_key_path(str(home), key_name=DEPLOY_KEY)), rendered
        # And never the default: not generated, not selected, not chowned.
        assert not names_key(rendered, owner_github_key_path(str(home), key_name=DEFAULT_KEY)), rendered
        # The managed block it would install still selects the deploy key.
        assert f"IdentityFile {home}/.ssh/{DEPLOY_KEY}" in rendered, rendered

        # Nothing about the real `.ssh` changed: the runner is fake, and the
        # point is that the upgrade never asked for a different key.
        assert (home / ".ssh" / "config").read_text(encoding="utf-8") == before
        assert not (home / ".ssh" / DEFAULT_KEY).exists()
        # And the operator is told which key, and where that came from.
        assert DEPLOY_KEY in output, output


def test_the_readiness_probe_asks_about_the_selected_key() -> None:
    """A readiness answer about a key nobody publishes with answers nothing."""
    with tempfile.TemporaryDirectory(prefix="identity-readiness.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEPLOY_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)

        asked: list[dict[str, object]] = []
        real_status = team_launcher.github_identity_status

        def recording_status(owner_user, owner_home, **kwargs):
            asked.append({"owner_home": str(owner_home), **{k: str(v) for k, v in kwargs.items() if k != "runner"}})
            return real_status(owner_user, owner_home, **kwargs)

        team_launcher.github_identity_status = recording_status
        try:
            run_upgrade(config_path)
        finally:
            team_launcher.github_identity_status = real_status

        assert asked, "readiness was never checked"
        assert asked[0]["key_name"] == DEPLOY_KEY, asked


def test_a_second_upgrade_changes_nothing_about_the_selection() -> None:
    """Idempotent, which is the contract an upgrade path has to keep."""
    with tempfile.TemporaryDirectory(prefix="identity-repeat.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEPLOY_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)

        _first_output, first_runner = run_upgrade(config_path)
        _second_output, second_runner = run_upgrade(config_path)

        assert identity_scripts(first_runner) == identity_scripts(second_runner)
        # And the block the second run would write is a fixed point over the one
        # the first run would have left behind.
        block = github_identity_block(str(home), key_name=DEPLOY_KEY)
        once = compose_ssh_config((home / ".ssh" / "config").read_text(encoding="utf-8"), block)
        assert compose_ssh_config(once, block) == once


def test_a_recorded_selection_is_what_the_upgrade_uses() -> None:
    """Persisted, so a tenant does not depend on its own block surviving."""
    with tempfile.TemporaryDirectory(prefix="identity-recorded.") as tmp:
        tmp_path = Path(tmp)
        # The block on disk names the default -- the state a damaged tenant is
        # left in -- while the plan records what it is meant to publish with.
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY, DEFAULT_KEY), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home, recorded_key=DEPLOY_KEY)

        _output, runner = run_upgrade(config_path)

        rendered = "\n".join(identity_scripts(runner))
        assert f"IdentityFile {home}/.ssh/{DEPLOY_KEY}" in rendered, rendered
        assert not names_key(rendered, f"{home}/.ssh/{DEFAULT_KEY}"), rendered


def test_legacy_metadata_with_no_readable_selection_stops() -> None:
    """Fail closed: an owner holding keys, and nothing saying which is the one."""
    with tempfile.TemporaryDirectory(prefix="identity-legacy.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY, "id_rsa_personal"))
        config_path, _plan = tenant_with_owner_home(tmp_path, home)

        output, runner = run_upgrade(config_path)

        # Nothing rendered, so nothing generated and no block rewritten.
        assert identity_scripts(runner) == [], identity_scripts(runner)
        assert not (home / ".ssh" / "config").exists()
        assert not (home / ".ssh" / DEFAULT_KEY).exists()
        # And the operator is told precisely what is missing and what to do.
        assert "nothing records which of them" in output, output
        assert DEPLOY_KEY in output and "id_rsa_personal" in output, output
        assert "was left untouched" in output, output


def test_an_owner_with_no_keys_at_all_is_still_provisioned() -> None:
    """Fail-closed must not mean a fresh tenant can never get an identity."""
    with tempfile.TemporaryDirectory(prefix="identity-fresh.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=())
        config_path, _plan = tenant_with_owner_home(tmp_path, home)

        _output, runner = run_upgrade(config_path)

        rendered = "\n".join(identity_scripts(runner))
        assert "ssh-keygen -t ed25519" in rendered, rendered
        assert names_key(rendered, f"{home}/.ssh/{DEFAULT_KEY}"), rendered


def test_a_dry_run_names_the_selection_and_changes_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="identity-dry.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEPLOY_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        before = (home / ".ssh" / "config").read_text(encoding="utf-8")

        output, runner = run_upgrade(config_path, dry_run=True)

        assert identity_scripts(runner) == [], identity_scripts(runner)
        assert (home / ".ssh" / "config").read_text(encoding="utf-8") == before
        assert not (home / ".ssh" / DEFAULT_KEY).exists()
        assert f"would keep" in output and DEPLOY_KEY in output, output


def test_the_managed_block_is_read_back_exactly() -> None:
    """The recovery half, on its own: markers, alias, and a refusal."""
    home = "/home/agent"
    block = github_identity_block(home, key_name=DEPLOY_KEY, host_alias="github-switchyard")
    name, host, alias, problems = parse_managed_github_identity(block)
    assert (name, host, alias, problems) == (DEPLOY_KEY, "github.com", "github-switchyard", ())

    # Somebody else's stanza naming another key is not this tenant's selection.
    theirs = "Host github-personal\n    IdentityFile /home/agent/.ssh/id_rsa_personal\n"
    name, _host, _alias, problems = parse_managed_github_identity(theirs + block)
    assert (name, problems) == (DEPLOY_KEY, ())

    # A block that names two different keys says nothing, and says so.
    torn = block.replace(
        GITHUB_IDENTITY_END,
        f"    IdentityFile /home/agent/.ssh/{DEFAULT_KEY}\n{GITHUB_IDENTITY_END}",
    )
    name, _host, _alias, problems = parse_managed_github_identity(torn)
    assert name == "" and problems, (name, problems)
    assert "2 different key files" in problems[0], problems

    # No block at all is a fresh tenant, not a problem.
    assert parse_managed_github_identity(theirs) == ("", "", "", ())


def test_the_resolver_prefers_what_is_recorded_over_what_is_installed() -> None:
    with tempfile.TemporaryDirectory(prefix="identity-order.") as tmp:
        home = owner_home_with(Path(tmp), keys=(DEPLOY_KEY, DEFAULT_KEY), selected=DEFAULT_KEY)
        recorded = resolve_owner_github_identity(str(home), recorded_key_name=DEPLOY_KEY)
        assert recorded.key_name == DEPLOY_KEY and recorded.resolved
        assert "recorded" in recorded.source

        recovered = resolve_owner_github_identity(str(home))
        assert recovered.key_name == DEFAULT_KEY and recovered.resolved
        assert "managed block" in recovered.source


def test_the_generated_operator_script_renders_the_tenant_s_key() -> None:
    """The provisioning path an operator runs by hand carries it too."""
    from scripts.ticket_board.project_provision import build_plan, render_operator_commands

    plan = build_plan(
        project="otto",
        owner_user="otto-agent",
        owner_home=Path("/home/otto-agent"),
        board_root=Path("/home/otto-agent/otto-ticketboard-live"),
        source_repo=Path("/opt/switchyard/current"),
        owner_github_key_name=DEPLOY_KEY,
        owner_github_host_alias="github-otto",
    )
    rendered = render_operator_commands(plan)
    assert names_key(rendered, f"/home/otto-agent/.ssh/{DEPLOY_KEY}"), rendered[-2000:]
    assert not names_key(rendered, f"/home/otto-agent/.ssh/{DEFAULT_KEY}"), rendered[-2000:]
    assert "Host github-otto" in rendered, rendered[-2000:]


@contextmanager
def identity_reported_as(authenticated: bool, *, fingerprint: str = "SHA256:example"):
    """Pin what the readiness probe answers, so a case tests one thing.

    The fixture's keys are placeholder text and its `.ssh` has ordinary modes,
    so a real probe reports a pile of true but unrelated problems. Whether the
    probe is consulted at all, and with which key, is asserted separately.
    """
    real = team_launcher.github_identity_status
    asked: list[str] = []

    def stub(owner_user, owner_home, **kwargs):
        asked.append(str(kwargs.get("key_name")))
        return team_launcher.GithubIdentityStatus(
            owner_user=owner_user,
            key_path=Path(owner_github_key_path(str(owner_home), key_name=kwargs["key_name"])),
            problems=(),
            authenticated=authenticated,
            detail="" if authenticated else "Permission denied (publickey)",
            fingerprint=fingerprint,
            checked=True,
        )

    team_launcher.github_identity_status = stub
    try:
        yield asked
    finally:
        team_launcher.github_identity_status = real


@contextmanager
def trusted_owner(home: Path, *, owner: str = "porter-owner", uid: int | None = None):
    """Root's baseline plan, and the host facts it must agree with.

    The owner and the home now come from root's own plan cross-checked against
    the host, never from the tenant's config or plan, so a fixture has to supply
    both sides. `home_dir_for_user` and `uid_for_user` are the codebase's own
    kernel lookups and are where a test stands in for passwd.
    """
    root_plan = team_launcher.privileged_baseline_plan_path("porter")
    root_plan.parent.mkdir(parents=True, exist_ok=True)
    root_plan.write_text(
        json.dumps({"project": "porter", "owner_user": owner, "owner_home": str(home)}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    real_home, real_uid = team_launcher.home_dir_for_user, team_launcher.uid_for_user
    team_launcher.home_dir_for_user = lambda user: home if user == owner else real_home(user)
    team_launcher.uid_for_user = (
        lambda user: (os.getuid() if uid is None else uid) if user == owner else real_uid(user)
    )
    try:
        yield root_plan
    finally:
        team_launcher.home_dir_for_user, team_launcher.uid_for_user = real_home, real_uid


def repair(config_path: Path, **kwargs):
    """The operator repair command, run the way the CLI runs it."""
    printed: list[str] = []
    runner = FakeRunner()
    config = team_launcher.load_project_config("porter", config_path)
    original_euid = team_launcher.os.geteuid
    try:
        team_launcher.os.geteuid = lambda: 0
        result = team_launcher.set_owner_github_identity_command(
            config,
            config_path=config_path,
            runner=runner,
            print_func=printed.append,
            **kwargs,
        )
    finally:
        team_launcher.os.geteuid = original_euid
    return result, "\n".join(printed), runner


def selection_scripts(runner: Any) -> list[str]:
    return [call[2] for call in runner.calls if len(call) >= 3 and call[0] == "sh" and call[1] == "-c"]


def test_the_operator_repair_records_the_key_in_both_plan_authorities() -> None:
    """The supported way out for a tenant whose block was pointed at the wrong key."""
    with tempfile.TemporaryDirectory(prefix="identity-repair.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY, DEFAULT_KEY), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        with trusted_owner(home) as root_plan, identity_reported_as(True) as asked:
            result, output, runner = repair(
                config_path, key_name=DEPLOY_KEY, host_alias="github-switchyard"
            )

        assert result == 0, output
        assert asked == [DEPLOY_KEY], asked
        for path in (config_path.parent / "plan.json", root_plan):
            recorded = json.loads(path.read_text(encoding="utf-8"))
            assert recorded["owner_github_key_name"] == DEPLOY_KEY, (path, recorded)
            assert recorded["owner_github_host_alias"] == "github-switchyard", (path, recorded)
            assert str(path) in output, output
        rendered = "\n".join(selection_scripts(runner))
        assert names_key(rendered, owner_github_key_path(str(home), key_name=DEPLOY_KEY)), rendered
        assert not names_key(rendered, owner_github_key_path(str(home), key_name=DEFAULT_KEY)), rendered
        assert "Host github-switchyard" in rendered, rendered
        assert "Host bastion.invalid" in (home / ".ssh" / "config").read_text(encoding="utf-8")


def test_the_selection_never_generates_or_touches_the_key_pair() -> None:
    """Selection is not provisioning, and root writes nothing under the owner's home."""
    with tempfile.TemporaryDirectory(prefix="identity-no-touch.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        with trusted_owner(home), identity_reported_as(True):
            result, output, runner = repair(config_path, key_name=DEPLOY_KEY)

        assert result == 0, output
        rendered = "\n".join(selection_scripts(runner))
        # No key is created, and root changes neither half's owner nor its mode.
        assert "ssh-keygen" not in rendered, rendered
        for forbidden in ("sudo chown", "sudo chmod", "chown root", "chmod 0600"):
            assert forbidden not in rendered, (forbidden, rendered)
        # Everything it does, it does as the owner.
        assert rendered.strip().startswith("set -eu\nsudo -u "), rendered[:120]
        assert rendered.count("sudo ") == rendered.count("sudo -u "), rendered


def test_a_tenant_cannot_redirect_the_write_by_editing_its_own_documents() -> None:
    """Finding 1: the tenant's config and plan are writable by every role."""
    with tempfile.TemporaryDirectory(prefix="identity-substitute.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEFAULT_KEY)
        elsewhere = tmp_path / "attacker-home"
        (elsewhere / ".ssh").mkdir(parents=True)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)

        # A role rewrites both tenant documents to name another account and home.
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        payload["run_as_user"] = "somebody-else"
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        plan_path = config_path.parent / "plan.json"
        tenant_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        tenant_plan["owner_home"] = str(elsewhere)
        plan_path.write_text(json.dumps(tenant_plan), encoding="utf-8")

        with trusted_owner(home), identity_reported_as(True):
            result, output, runner = repair(config_path, key_name=DEPLOY_KEY)

        # Root acted for the owner its own baseline names, in that owner's home.
        assert result == 0, output
        rendered = "\n".join(selection_scripts(runner))
        assert str(elsewhere) not in rendered, rendered
        assert "somebody-else" not in rendered, rendered
        assert str(home) in rendered, rendered


def test_a_baseline_that_disagrees_with_the_host_stops_everything() -> None:
    """Divergence is not something to pick a winner from."""
    with tempfile.TemporaryDirectory(prefix="identity-divergent.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        with trusted_owner(home) as root_plan:
            recorded = json.loads(root_plan.read_text(encoding="utf-8"))
            recorded["owner_home"] = str(tmp_path / "somewhere-else")
            root_plan.write_text(json.dumps(recorded), encoding="utf-8")
            result, output, runner = repair(config_path, key_name=DEPLOY_KEY)

        assert result == 1, output
        assert "which one is right is not this command's to decide" in output, output
        assert selection_scripts(runner) == [], selection_scripts(runner)
        assert "owner_github_key_name" not in (config_path.parent / "plan.json").read_text()


def test_a_plan_replaced_by_a_symlink_is_refused_rather_than_followed() -> None:
    """Finding 2: root must not read, truncate or re-own a symlink's referent."""
    with tempfile.TemporaryDirectory(prefix="identity-symlink-plan.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        victim = tmp_path / "victim.json"
        victim.write_text(json.dumps({"do not": "touch"}), encoding="utf-8")
        plan_path = config_path.parent / "plan.json"
        plan_path.unlink()
        plan_path.symlink_to(victim)

        with trusted_owner(home):
            result, output, runner = repair(config_path, key_name=DEPLOY_KEY)

        assert result == 1, output
        assert "is a symlink" in output, output
        assert selection_scripts(runner) == [], selection_scripts(runner)
        # The referent is exactly as it was.
        assert json.loads(victim.read_text(encoding="utf-8")) == {"do not": "touch"}


def test_a_key_half_replaced_by_a_symlink_is_refused() -> None:
    """Finding 3: a name pointed at somebody else's file is not this owner's key."""
    for half in ("", ".pub"):
        with tempfile.TemporaryDirectory(prefix="identity-symlink-key.") as tmp:
            tmp_path = Path(tmp)
            home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEFAULT_KEY)
            victim = tmp_path / "root-owned-secret"
            victim.write_text("not the tenant's\n", encoding="utf-8")
            target = home / ".ssh" / f"{DEPLOY_KEY}{half}"
            target.unlink()
            target.symlink_to(victim)
            config_path, _plan = tenant_with_owner_home(tmp_path, home)

            with trusted_owner(home):
                result, output, runner = repair(config_path, key_name=DEPLOY_KEY)

            assert result == 1, (half, output)
            assert "is a symlink" in output, output
            assert "no key was created" in output, output
            assert selection_scripts(runner) == [], selection_scripts(runner)
            assert victim.read_text(encoding="utf-8") == "not the tenant's\n"


def test_a_key_half_owned_by_somebody_else_is_refused() -> None:
    """The check is the owner's own files, not merely files that exist."""
    with tempfile.TemporaryDirectory(prefix="identity-foreign-key.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        # The owner is somebody whose uid is not the one that owns these files.
        with trusted_owner(home, uid=os.getuid() + 1):
            result, output, runner = repair(config_path, key_name=DEPLOY_KEY)

        assert result == 1, output
        assert "rather than by the tenant owner" in output, output
        assert selection_scripts(runner) == [], selection_scripts(runner)


def test_a_key_that_disappears_after_validation_is_never_recreated() -> None:
    """Finding 3's interval: deleting the key mid-command must not generate one."""
    with tempfile.TemporaryDirectory(prefix="identity-vanishing-key.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)

        with trusted_owner(home), identity_reported_as(True):
            result, output, runner = repair(config_path, key_name=DEPLOY_KEY)
        # The key goes away in the interval the renderer used to cover.
        (home / ".ssh" / DEPLOY_KEY).unlink()

        assert result == 0, output
        rendered = "\n".join(selection_scripts(runner))
        # What was rendered cannot create it: there is no keygen in it at all,
        # so the interval has nothing to exploit.
        assert "ssh-keygen" not in rendered, rendered
        assert not (home / ".ssh" / DEPLOY_KEY).exists()


def test_a_root_plan_the_tenant_could_have_written_is_refused() -> None:
    """Root's own authority has to be root's, or it is not an authority."""
    with tempfile.TemporaryDirectory(prefix="identity-weak-root-plan.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        with trusted_owner(home) as root_plan:
            root_plan.chmod(0o666)
            try:
                result, output, runner = repair(config_path, key_name=DEPLOY_KEY)
            finally:
                root_plan.chmod(0o644)

        assert result == 1, output
        # Caught by the whole-path check before the plan is even read, which is
        # earlier than the file's own mode check and is the right place: a plan
        # anyone can write cannot say who root should act for.
        assert "is not root-controlled" in output, output
        assert "cannot establish whose it is" in output, output
        assert selection_scripts(runner) == [], selection_scripts(runner)
        assert "owner_github_key_name" not in (config_path.parent / "plan.json").read_text()


def test_a_missing_authority_stops_before_anything_is_written() -> None:
    """Finding 4: both, or neither. Two plans that disagree is the worst outcome."""
    with tempfile.TemporaryDirectory(prefix="identity-one-authority.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        (config_path.parent / "plan.json").unlink()

        with trusted_owner(home):
            result, output, runner = repair(config_path, key_name=DEPLOY_KEY)

        assert result == 1, output
        assert "both have to be writable" in output, output
        assert selection_scripts(runner) == [], selection_scripts(runner)
        root_plan = json.loads(team_launcher.privileged_baseline_plan_path("porter").read_text())
        assert "owner_github_key_name" not in root_plan, root_plan


def test_a_failed_second_write_puts_the_first_one_back() -> None:
    """The transaction: the two authorities never disagree because of a failure."""
    with tempfile.TemporaryDirectory(prefix="identity-rollback.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        tenant_plan = config_path.parent / "plan.json"
        real_write = team_launcher.write_plan_no_follow
        calls: list[Path] = []

        def fails_on_the_tenant_copy(document, body):
            calls.append(document.path)
            if document.path == tenant_plan and len(calls) == 2:
                return f"{document.path} could not be replaced (simulated)"
            return real_write(document, body)

        team_launcher.write_plan_no_follow = fails_on_the_tenant_copy
        try:
            # Inside the context, because that is what writes root's baseline.
            with trusted_owner(home) as root_plan:
                before = {"root": root_plan.read_bytes(), "tenant": tenant_plan.read_bytes()}
                result, output, runner = repair(config_path, key_name=DEPLOY_KEY)
        finally:
            team_launcher.write_plan_no_follow = real_write

        assert result == 1, output
        assert "was put back as it was" in output, output
        assert "neither plan was left disagreeing" in output, output
        assert selection_scripts(runner) == [], selection_scripts(runner)
        assert team_launcher.privileged_baseline_plan_path("porter").read_bytes() == before["root"]
        assert tenant_plan.read_bytes() == before["tenant"]


def test_the_repair_verifies_the_key_it_selected() -> None:
    """A selection that cannot authenticate is reported, not announced as done."""
    with tempfile.TemporaryDirectory(prefix="identity-repair-verify.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        with trusted_owner(home), identity_reported_as(
            False, fingerprint="SHA256:GJXVwhfB26C6U4Zway32Hgcou+cH498/TYuQwyhfo94"
        ) as asked:
            result, output, _runner = repair(config_path, key_name=DEPLOY_KEY)

        assert asked == [DEPLOY_KEY], asked
        assert result == 1, output
        assert "did not authenticate" in output, output
        assert "SHA256:GJXVwhfB26C6U4Zway32Hgcou" in output, output
        recorded = json.loads((config_path.parent / "plan.json").read_text(encoding="utf-8"))
        assert recorded["owner_github_key_name"] == DEPLOY_KEY, recorded


def test_a_dry_run_repair_writes_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="identity-repair-dry.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)
        before = (home / ".ssh" / "config").read_text(encoding="utf-8")

        with trusted_owner(home):
            result, output, runner = repair(config_path, key_name=DEPLOY_KEY, dry_run=True)

        assert result == 0, output
        assert "would record" in output and DEPLOY_KEY in output, output
        assert selection_scripts(runner) == [], selection_scripts(runner)
        assert (home / ".ssh" / "config").read_text(encoding="utf-8") == before
        recorded = json.loads((config_path.parent / "plan.json").read_text(encoding="utf-8"))
        assert "owner_github_key_name" not in recorded, recorded


SECRET = "SECRET_SENTINEL_SHOULD_NOT_BE_PRINTED"


def test_a_public_key_swapped_between_validation_and_read_is_never_printed() -> None:
    """SYRD-100 review: lstat then read_text is two lookups of one name.

    A same-UID tenant can replace `<key>.pub` with a symlink in between. The
    status path runs as root during the repair, and the remedy prints
    `status.public_key`, so root would read and print whatever the symlink names.
    The Director reproduced it by pinning lstat to the original stat while the
    path was swapped; this drives the same swap against the real code.
    """
    with tempfile.TemporaryDirectory(prefix="identity-pub-swap.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEPLOY_KEY)
        victim = tmp_path / "root-readable-secret"
        victim.write_text(f"{SECRET}\n", encoding="utf-8")
        public = home / ".ssh" / f"{DEPLOY_KEY}.pub"
        public.chmod(0o644)

        # The race itself. A plain symlink is refused by the leaf check, so the
        # hole is the interval: validation sees the real file, and the read that
        # follows sees whatever the name points at by then. Reproduced by
        # swapping at the moment the old code took its stat, which is what the
        # tenant would be racing to do.
        # Swapped at the moment the path is stat'd, which is the interval the
        # two-lookup read left open. Against the repaired code this hook never
        # fires, because that code never stats this path: it opens it once with
        # O_NOFOLLOW and reads the descriptor it validated. That is the fix, so
        # the case asserts the outcome rather than that the swap occurred.
        real_lstat = Path.lstat
        swapped: list[bool] = []

        def lstat_then_swap(self, *args, **kwargs):
            info = real_lstat(self, *args, **kwargs)
            if self == public and not swapped:
                swapped.append(True)
                public.unlink()
                public.symlink_to(victim)
            return info

        Path.lstat = lstat_then_swap
        try:
            status = team_launcher.github_identity_status(
                team_launcher.current_user_name(), home, key_name=DEPLOY_KEY, runner=FakeRunner()
            )
        finally:
            Path.lstat = real_lstat


        # The one property that matters: nothing root read through a name the
        # tenant controls reaches the report. Whether the swap was reachable at
        # all is the implementation's business, and the repaired code makes it
        # unreachable rather than detecting it.
        assert SECRET not in status.public_key, status.public_key
        remedy = team_launcher.github_identity_remedy(status, project="porter")
        assert SECRET not in remedy, remedy
        assert SECRET not in " ".join(status.problems), status.problems
        if swapped:
            # Only the two-lookup read gets here, and it must not have believed
            # what it found the second time.
            assert status.public_key == "", status.public_key


def test_an_ssh_config_swapped_for_a_symlink_cannot_steer_the_report() -> None:
    """The other file the status path reads, and what it may not become.

    Its contents decide what the report says about which identity is selected,
    so a symlink here is a tenant choosing what root concludes. A symlink as the
    last component was already refused before this ticket; this holds that while
    the read underneath it changes, rather than demonstrating a defect.
    """
    with tempfile.TemporaryDirectory(prefix="identity-config-swap.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEPLOY_KEY)
        victim = tmp_path / "root-readable-config"
        victim.write_text(f"# {SECRET}\n", encoding="utf-8")
        config = home / ".ssh" / "config"
        config.unlink()
        config.symlink_to(victim)

        status = team_launcher.github_identity_status(
            team_launcher.current_user_name(), home, key_name=DEPLOY_KEY, runner=FakeRunner()
        )

        assert any("is not a regular file" in problem for problem in status.problems), status.problems
        joined = " ".join(status.problems) + team_launcher.github_identity_remedy(status, project="porter")
        assert SECRET not in joined, joined


def test_a_symlinked_ssh_directory_is_refused_before_anything_is_read() -> None:
    """The ancestor route, which needs no race at all.

    `lstat` refuses a symlink as the last component and follows every one before
    it, so pointing `.ssh` somewhere else passes the leaf check and the read then
    goes wherever the directory does. Found while proving the case above.
    """
    with tempfile.TemporaryDirectory(prefix="identity-dir-swap.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEPLOY_KEY)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / f"{DEPLOY_KEY}.pub").write_text(f"{SECRET}\n", encoding="utf-8")
        # A configuration too, so the read that decides what the report concludes
        # is exercised and not merely the one that decides what it prints.
        (elsewhere / "config").write_text(
            f"{GITHUB_IDENTITY_BEGIN}\nHost github.com\n"
            f"    IdentityFile {home}/.ssh/{DEFAULT_KEY}\n{GITHUB_IDENTITY_END}\n",
            encoding="utf-8",
        )
        (elsewhere / "config").chmod(0o600)
        ssh_dir = home / ".ssh"
        for child in ssh_dir.iterdir():
            child.unlink()
        ssh_dir.rmdir()
        ssh_dir.symlink_to(elsewhere)

        status = team_launcher.github_identity_status(
            team_launcher.current_user_name(), home, key_name=DEPLOY_KEY, runner=FakeRunner()
        )

        assert SECRET not in status.public_key, status.public_key
        remedy = team_launcher.github_identity_remedy(status, project="porter")
        assert SECRET not in remedy, remedy
        # And nothing was concluded from the configuration behind that symlink:
        # the report must not claim the tenant selected the default key on the
        # strength of a file the tenant redirected root to.
        assert not any(
            "selects an identity other than" in problem for problem in status.problems
        ), status.problems
        assert any(
            "the owner's ssh configuration" in problem for problem in status.problems
        ), status.problems


def test_the_fingerprint_comes_from_the_bytes_that_were_validated() -> None:
    """Not from handing the path back to ssh-keygen to open a second time."""
    with tempfile.TemporaryDirectory(prefix="identity-fingerprint.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY,), selected=DEPLOY_KEY)
        (home / ".ssh" / f"{DEPLOY_KEY}.pub").chmod(0o644)
        (home / ".ssh").chmod(0o700)
        (home / ".ssh" / DEPLOY_KEY).chmod(0o600)
        (home / ".ssh" / "config").chmod(0o600)

        seen: list[list[str]] = []

        def recording(args, **kwargs):
            seen.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="256 SHA256:x probe (ED25519)\n")

        status = team_launcher.github_identity_status(
            team_launcher.current_user_name(), home, key_name=DEPLOY_KEY, runner=recording
        )

        fingerprint_calls = [argv for argv in seen if argv[:2] == ["ssh-keygen", "-l"]]
        assert fingerprint_calls, seen
        # Read from stdin, so the path is never opened a second time.
        assert fingerprint_calls[0][-1] == "-", fingerprint_calls[0]
        assert str(home) not in " ".join(fingerprint_calls[0]), fingerprint_calls[0]
        assert status.fingerprint.startswith("256 SHA256:"), status.fingerprint


def test_the_upgrade_then_keeps_what_the_repair_recorded() -> None:
    """The two halves meet: repair once, and every later upgrade honours it."""
    with tempfile.TemporaryDirectory(prefix="identity-repair-upgrade.") as tmp:
        tmp_path = Path(tmp)
        home = owner_home_with(tmp_path, keys=(DEPLOY_KEY, DEFAULT_KEY), selected=DEFAULT_KEY)
        config_path, _plan = tenant_with_owner_home(tmp_path, home)

        with trusted_owner(home), identity_reported_as(True):
            result, _output, _runner = repair(config_path, key_name=DEPLOY_KEY)
        assert result == 0

        _upgraded, runner = run_upgrade(config_path)
        rendered = "\n".join(identity_scripts(runner))
        assert names_key(rendered, owner_github_key_path(str(home), key_name=DEPLOY_KEY)), rendered
        assert not names_key(rendered, owner_github_key_path(str(home), key_name=DEFAULT_KEY)), rendered


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("owner_identity_preserved_across_upgrades_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
