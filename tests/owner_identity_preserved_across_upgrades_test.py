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


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("owner_identity_preserved_across_upgrades_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
