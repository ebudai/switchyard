#!/usr/bin/env python3
"""SYRD-97: an existing tenant can be given the publication boundary.

SYRD-93 put the push credential where no role can reach it. SYRD-96 then tried
to deploy it to the live tenant and found there was no supported way: the grant
is reachable from the fresh-provisioning command packet and from nowhere else,
and the shared-project-account path skips the role-account migration that used
to carry it. The tenant upgraded its board, its schema and its staged tooling
and still had no publisher rule, no /etc/switchyard/publish and no key. Ops was
left generating root commands into a project-account-writable /tmp path, which
under one shared account (SYRD-69) is a root shell for every role.

The privileged half runs inside an unprivileged user namespace with a
root-owned tree chrooted into it, because `unshare --map-root-user` alone maps
the host's `/` to nobody -- and the whole-path check correctly refuses that, so
the accepting case is untestable without the chroot.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ticket_board.project_provision import (  # noqa: E402
    publish_grant_path,
    publish_identity_path,
    publish_sudoers_document,
    publish_sudoers_path,
)
from scripts import team_launcher  # noqa: E402
from scripts.ticket_board.publication_boundary import (  # noqa: E402
    publication_artifacts,
    root_controlled_problems,
)

PRIVILEGED = ROOT / "tests" / "publication_boundary_upgrade_privileged.py"


def _namespaces_available() -> bool:
    if not shutil.which("unshare"):
        return False
    probe = subprocess.run(
        ["unshare", "--user", "--map-auto", "--map-root-user", "--mount", "true"],
        capture_output=True,
    )
    return probe.returncode == 0


def test_the_publisher_rule_is_one_program_and_its_own_file() -> None:
    """A grant wide enough to be convenient is not a boundary."""
    document = publish_sudoers_document("porter", "porter-owner")
    lines = [line for line in document.splitlines() if line and not line.startswith("#")]

    assert len(lines) == 1, document
    assert lines[0] == (
        "porter-owner ALL=(root) NOPASSWD: /usr/local/lib/switchyard/porter/switchyard-publish-ref"
    ), lines
    # No wildcard, no argument the caller chooses, no shell.
    for forbidden in ("*", "ALL:", "/bin/sh", "%"):
        assert forbidden not in lines[0], lines[0]
    # Its own file: an existing tenant has no role accounts, so the role-control
    # rule is not this rule's business and removing one must not disturb the other.
    assert publish_sudoers_path("porter").name == "48-porter-publish"


def test_every_privileged_artifact_is_named_for_a_dry_run() -> None:
    """A preview that does not name what it installs is not a preview."""
    artifacts = publication_artifacts("porter", "/etc/sudoers.d/48-porter-publish")
    paths = [artifact.path for artifact in artifacts]

    assert publish_identity_path("porter") in paths, paths
    assert f"{publish_identity_path('porter')}.pub" in paths, paths
    assert publish_grant_path("porter") in paths, paths
    assert "/etc/sudoers.d/48-porter-publish" in paths, paths
    # Against the configured roots, not the host's literals: these suites
    # redirect them so a real privileged branch cannot write to /etc.
    from scripts.ticket_board.project_provision import publish_grant_root, publish_staging_root

    assert publish_grant_root() in paths and publish_staging_root() in paths, paths
    for artifact in artifacts:
        assert artifact.path.startswith("/"), artifact
        assert "root:root" in artifact.mode, artifact
        assert artifact.description.strip(), artifact
    # The key's line says it out loud, because that is the one an operator most
    # needs to know will not be touched.
    key_line = next(a for a in artifacts if a.path == publish_identity_path("porter"))
    assert "never regenerated" in key_line.description, key_line


def test_a_path_is_only_as_pinned_as_the_directories_leading_to_it() -> None:
    """Checking the final file is not enough (SYRD-50).

    Asserted against this host's own filesystem rather than a fixture, because
    the property is about the whole chain: a fixture under /tmp cannot have a
    clean baseline, since /tmp is world-writable and that is exactly what the
    check is supposed to notice.
    """
    # Root owns /usr/bin and every directory above it, and none of them is
    # writable by anyone else.
    assert root_controlled_problems("/usr/bin", expect_uid=0) == []

    # The leaf is irrelevant here: /tmp is world-writable, so anything under it
    # is refused and the refusal names the directory rather than the leaf.
    problems = root_controlled_problems("/tmp/whatever", expect_uid=0)
    assert problems and "/tmp" in problems[0], problems
    assert "group- or world-writable" in problems[0], problems

    # Owned by somebody who is not the expected owner, starting at the root.
    other = root_controlled_problems("/usr/bin", expect_uid=os.getuid() + 4242)
    assert other and other[0].startswith("/ is owned by uid"), other

    # The string is judged before pathlib is allowed to tidy it: constructing a
    # Path silently drops "." components, so what is walked would stop being
    # what was written.
    assert root_controlled_problems("/usr/./bin", expect_uid=0) != []
    assert root_controlled_problems("/usr/../usr/bin", expect_uid=0) != []
    assert root_controlled_problems("usr/bin", expect_uid=0) != []
    assert root_controlled_problems("", expect_uid=0) != []


_PRIVILEGED_REPORT: dict | None = None


def _privileged_report() -> dict:
    """The privileged run's findings, produced once and read by several cases."""
    global _PRIVILEGED_REPORT
    if _PRIVILEGED_REPORT is None:
        with tempfile.TemporaryDirectory(prefix="pub-root.") as tmp:
            proc = subprocess.run(
                ["unshare", "--user", "--map-auto", "--map-root-user", "--mount",
                 sys.executable, str(PRIVILEGED), tmp, str(ROOT)],
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
            _PRIVILEGED_REPORT = json.loads(proc.stdout.strip().splitlines()[-1])
    return _PRIVILEGED_REPORT


def test_the_privileged_install_end_to_end() -> None:
    """Materialize, install, retry, and refuse -- against real files as root."""
    if not _namespaces_available():
        return
    report = _privileged_report()

    # Materialized from the object store, verified, and not rebuilt on retry.
    assert report["materialized"] is True, report
    assert report["marker_matches_commit"] is True, report
    assert report["second_call_rebuilt"] is False, report

    # The preview names the exact release and every privileged artifact, and
    # installs none of them.
    assert report["dry_run_changed_nothing"] is True, report["dry_run_text"]
    preview = report["dry_run_text"]
    assert report["release_commit"] in preview, preview
    # The paths come from the privileged run, not from this process: these
    # suites redirect the publication roots so they cannot write to the host's
    # /etc, and the chroot deliberately uses the real defaults.
    for path in (report["key"], f"{report['key']}.pub", report["grant"], report["sudoers"]):
        assert path in preview, (path, preview)
    for mode in ("0600 root:root", "0644 root:root", "0640 root:root", "0440 root:root", "0755 root:root"):
        assert mode in preview, (mode, preview)

    # Every artifact root-owned with the mode it advertises.
    for path, mode in report["modes"].items():
        assert mode["uid"] == 0, (path, mode)
    assert report["modes"]["/etc/switchyard/publish"]["mode"] == "0o755"
    assert report["modes"][report["key"]]["mode"] == "0o600"
    assert report["modes"][report["key"] + ".pub"]["mode"] == "0o644"
    assert report["modes"][report["grant"]]["mode"] == "0o640"
    assert report["modes"][report["sudoers"]]["mode"] == "0o440"

    # The destination comes from the trusted cache, not from a role's checkout.
    assert report["grant_remote"] == "git@example.invalid:owner/repo.git", report

    # The key is created once and never again, which is what stops an upgrade
    # silently breaking publication until somebody re-registers it at the forge.
    assert report["key_created_first"] is True and report["key_created_again"] is False, report
    assert report["key_unchanged_on_retry"] is True, report
    assert report["retry_problems"] == [], report

    # The operator is told the public key, its fingerprint, and the one thing
    # this cannot do for them.
    assert report["public_key"].startswith("ssh-ed25519 "), report
    assert "SHA256:" in report["fingerprint"], report
    assert "WRITE key" in report["report_text"], report["report_text"]
    assert "still has write authority" in report["report_text"], report["report_text"]

    # A release path somebody else could write is refused, not repaired.
    assert report["untrusted_refused"] is True, report
    assert "not root-controlled" in report["untrusted_reason"], report
    # And refusing means refusing: the tree that was there is still there.
    assert report["untrusted_left_alone"] is True, report

    # A commit the source does not contain cannot be materialized from it, and
    # refusing leaves no half-built tree behind for the next run to consume.
    assert report["unknown_commit_refused"] is True, report
    assert "does not contain commit" in report["unknown_commit_reason"], report
    assert report["unknown_commit_left_no_tree"] is True, report

    # What is materialized comes from the object store, so a file a role wrote
    # into the working tree is not in the release.
    assert report["worktree_file_absent_from_release"] is True, report

    # An unparsable sudoers file is never allowed to become live, and nothing is
    # left staged beside it.
    assert report["invalid_sudoers_refused"] is True, report
    assert "not valid sudoers" in report["invalid_sudoers_reason"], report
    assert report["invalid_sudoers_not_installed"] is True, report
    assert report["invalid_sudoers_left_no_staging"] is True, report

    # A release root created under a directory somebody else can write is
    # refused after it lands. Nothing existed on that path beforehand, so the
    # check before materializing had nothing to look at: this is the one after.
    assert report["writable_parent_refused"] is True, report
    assert "not root-controlled" in report["writable_parent_reason"], report

    # REVIEW FINDING 2: the tenant's own cache named an attacker's remote and
    # none of it reached the grant; with no root-owned pin there is no grant at
    # all, rather than one pointing at a server a role chose.
    assert report["grant_never_names_tenant_remote"] is True, report
    assert report["no_root_pin_refuses"] is True, report
    assert "not trusted for this" in report["no_root_pin_reason"], report
    assert report["no_root_pin_wrote_no_grant"] is True, report

    # REVIEW FINDING 3: an interrupted run that lost the public half and left
    # the private key world-readable converges without replacing the key the
    # forge already knows.
    assert report["damaged_key_problems"] == [], report
    assert report["damaged_key_kept_private"] is True, report
    assert report["damaged_key_restored_public"] is True, report
    assert report["damaged_key_flagged_restore"] is True, report
    assert report["damaged_key_never_recreated"] is True, report
    modes = report["damaged_key_modes"]
    assert list(modes.values()) == ["0o600", "0o644"], modes

    # REVIEW FINDING 4 (first round): an unreachable forge leaves known_hosts
    # absent, and the report says so rather than claiming it is there. What is
    # reported is decided by looking at the path, so this also covers a run that
    # failed before reaching it.
    assert report["partial_pending"] == ["/etc/switchyard/publish/known_hosts"], report
    assert report["partial_complete"] is False, report
    text = report["partial_text"]
    assert "/etc/switchyard/publish/known_hosts NOT INSTALLED" in text, text
    assert "INCOMPLETE" in text, text
    assert "register that key" not in text.split("INCOMPLETE")[0], text

    # A release tree already on the path that somebody else can write is
    # refused, whatever its marker claims.
    assert report["tampered_release_refused"] is True, report
    assert "not root-controlled" in report["tampered_release_reason"], report


def test_the_upgrade_step_installs_it_and_restarts_no_worker() -> None:
    """It belongs to the artifacts phase, which touches no running role."""
    source = (ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")
    start = source.index("def upgrade_project_command(")
    end = source.index("def _role_accounts_ready(", start)
    body = source[start:end]

    assert "install_tenant_publication_boundary" in body, "the upgrade never installs it"
    # Before the identities transaction, in the phase that runs on every upgrade
    # including a resumed one, and nowhere near a stop or a start.
    artifacts = body.index("install_tenant_publication_boundary")
    identities = body.index("cutover_role_identities_command")
    assert artifacts < identities, "publication must be installed before any role is moved"

    step = source[source.index("def install_tenant_publication_boundary("):]
    step = step[: step.index("def _selected_release_commit(")]
    for restarts in ("stop_project", "launch_project", "_start_role_sessions", "systemctl restart"):
        assert restarts not in step, f"{restarts} would disturb a running worker"


def test_the_ordinary_upgrade_really_reaches_the_publication_step() -> None:
    """Driven, not read: the point of the ticket is that the upgrade installs it.

    Reading the source cannot tell the difference between a call and a call that
    is never reached, and "never reached" is exactly the defect this repairs --
    `publish_grant_commands()` was present in the tree the whole time and
    invoked only by fresh provisioning.
    """
    calls: list[dict] = []
    original = team_launcher.install_tenant_publication_boundary

    def recording(config, **kwargs):
        calls.append({"project": config.project, "dry_run": kwargs.get("dry_run")})
        return []

    with tempfile.TemporaryDirectory(prefix="pub-upgrade.") as tmp:
        sys.path.insert(0, str(ROOT / "tests"))
        import team_launcher_upgrade_cutover_test as cutover

        config_path, _ = cutover._declarative_tenant(Path(tmp))
        team_launcher.install_tenant_publication_boundary = recording
        try:
            cutover._upgrade(config_path, as_root=True, exists=set())
            live = list(calls)
            calls.clear()
            cutover._upgrade(config_path, as_root=True, exists=set(), dry_run=True)
            previewed = list(calls)
        finally:
            team_launcher.install_tenant_publication_boundary = original

    assert live and live[0]["project"] == "porter", live
    assert live[0]["dry_run"] is False, live
    # And the preview reports it too, because previewing an upgrade that will
    # install privileged artifacts is exactly when they should be named.
    assert previewed and previewed[0]["dry_run"] is True, previewed


def test_privileged_tooling_is_staged_from_the_commit_not_the_worktree() -> None:
    """REVIEW FINDING 1, and the most serious of them.

    `refresh_staged_role_tooling` copies `switchyard-publish-ref` into a
    root-owned path that a NOPASSWD rule points root at. It used to copy it out
    of the source checkout, and under one shared account (SYRD-69) every role
    can write that checkout -- so planting the file was a root shell. The staged
    bytes must be the commit's.
    """
    helper = ROOT / "scripts" / "switchyard-publish-ref"
    planted = b"#!/bin/sh\nexec /bin/sh   # a role planted this\n"

    with tempfile.TemporaryDirectory(prefix="pub-staging.") as tmp:
        sys.path.insert(0, str(ROOT / "tests"))
        import team_launcher_upgrade_cutover_test as cutover

        config_path, root = cutover._declarative_tenant(Path(tmp))
        original = helper.read_bytes()
        try:
            # The worktree now holds something the commit does not.
            helper.write_bytes(planted)
            cutover._upgrade(config_path, as_root=True, exists=set())
            staged = Path(root) / "tooling" / "porter" / "switchyard-publish-ref"
            assert staged.exists(), sorted(p.name for p in (Path(root) / "tooling" / "porter").iterdir())
            installed = staged.read_bytes()
            # Which commit the upgrade actually selected, from the marker it
            # staged beside the tooling -- not this checkout's HEAD, which is a
            # different commit on any branch that has moved ahead of the ref the
            # fixture deploys.
            marker = json.loads(
                (Path(root) / "tooling" / "porter" / ".switchyard-release.json").read_text()
            )
        finally:
            helper.write_bytes(original)

    committed = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{marker['commit']}:scripts/switchyard-publish-ref"],
        capture_output=True, check=True,
    ).stdout
    assert planted != committed
    assert installed != planted, "root staged the bytes a role planted in the worktree"
    assert installed == committed, "the staged helper is not the selected commit's"


def test_a_stale_installed_launcher_is_bootstrapped_with_root_owned_code_only() -> None:
    """REVIEW FINDING 1 (second round): the advertised entry point must reach this.

    `/usr/local/bin/switchyard` sends privileged commands to whatever
    `<install root>/current` points at, so an operator who pulls a checkout and
    runs `sudo switchyard upgrade` is still running the previously installed
    launcher -- on the tenant that found this, one several weeks old with none of
    this code and no `--publish-remote`. The way out cannot be `sudo ./install`:
    that runs a script from the checkout, which every role can write.

    Driven from that exact state in the privileged run: an install root whose
    `current` is the old release, the bootstrap this names, and the operator
    command before and after.
    """
    if not _namespaces_available():
        return
    report = _privileged_report()

    assert report["bootstrap_started_stale"] is True, report
    # Only root-owned code runs: the installer named belongs to the release that
    # is already installed, never to the candidate checkout.
    assert report["bootstrap_runs_installed_code"] is True, report["bootstrap_command"]
    assert report["bootstrap_names_no_candidate_script"] is True, report["bootstrap_command"]
    assert "./install" not in report["bootstrap_command"], report["bootstrap_command"]
    assert report["bootstrap_exit"] == 0, report["bootstrap_error"]
    assert report["bootstrap_landed_on_new_commit"] is True, report

    # And the command the ticket claims an operator can run: absent before,
    # present after. That is the boundary the previous revision's tests missed.
    assert report["old_launcher_lacks_publish_remote"] is True, report
    assert report["new_launcher_has_publish_remote"] is True, report

    # ROUND 4: a full SHA does NOT make content immutable. `git archive <sha>`
    # honours `refs/replace/<sha>`, so whoever can write the repository can
    # substitute the tree -- and the old installer executes the exported release
    # before activating it. The fixture installs exactly that: a replacement ref
    # whose tree carries a malicious executable `switchyard`.
    assert report["source_owned_by_project_uid"] is True, report
    assert report["git_refuses_foreign_source"] is True, report
    assert report["replacement_substitutes_in_source"] is True, (
        "the attack did not reproduce, so refusing it proves nothing"
    )
    assert report["attacker_marker_before"] is False, report
    # It never ran, and what landed is the reviewed tree.
    assert report["attacker_marker_after"] is False, "the planted executable ran as root"
    assert report["materialized_is_not_attacker"] is True, report

    # Because root is given no access to that repository at all: it never runs
    # git against it and never hands it to the installer. It reads one bundle
    # out of it, then demands the exact commit in a repository of its own.
    assert report["root_never_reads_source"] is True, report["bootstrap_sequence"]
    assert report["root_reads_only_the_bundle_from_source"] is True, report["bootstrap_sequence"]
    assert report["bootstrap_disables_replacement"] is True, report["bootstrap_sequence"]
    assert "safe.directory" not in " ".join(report["bootstrap_sequence"]), report["bootstrap_sequence"]
    # And the sequence is concrete: no placeholder a shell would choke on.
    assert report["sequence_has_no_placeholders"] is True, report["bootstrap_sequence"]

    # ROUND 3, FINDING 2: repointing `current` is not enough. The root-owned
    # record still selects the old release, so an upgrade with no arguments goes
    # straight back to it. Proved by running that command, not by asking the
    # resolver: the last command of the sequence is what re-selects the reviewed
    # release durably, and it is executed in the case below.
    assert report["pin_started_stale"] is True, report
    assert report["argumentless_recovers_old_release"] is True, report["argumentless_text"]
    assert report["sequence_repins"] is True, report["bootstrap_sequence"]

    # ROUND 4: the operator's two commands run in a repository every role can
    # write, so a role can point the bootstrap ref at a commit of their own
    # between them. The bundle then carries the reviewed ref NAME and the
    # attacker's commit, and root must refuse -- and a refusal must leave
    # `current`, the root-owned pin and every privileged artifact as they were.
    assert report["tampered_bundle_lacks_reviewed_commit"] is True, (
        "the substituted bundle carried the reviewed commit after all"
    )
    assert report["tampered_bundle_carried_attacker"] is True, (
        "the substitution did not reach the bundle, so refusing it proves nothing"
    )
    assert report["tampered_ref_refused"] is True, (
        "a bundle that does not carry the reviewed commit was accepted"
    )
    assert report["tampered_ref_did_not_run_attacker"] is True, "the planted executable ran as root"
    assert report["tampered_ref_changed_nothing"] is True, report["tampered_ref_failure"]


def test_the_whole_operator_sequence_runs_including_its_last_command() -> None:
    """ROUND 4, FINDING 2: the last command was inspected, not executed.

    The previous revision ran `sequence[:-1]` and then called
    `record_upgrade_source()` and `resolve_trusted_upgrade_release()` directly,
    which skips the boundary that stranded Ops in the first place: the public
    wrapper's dispatch, the CLI parser, the project registry, durable recording
    by the real command, the dry run, the publication install and the phase
    journal. Every command is now run as emitted, `sudo` prefix included, against
    a registered tenant and the stale root-owned pin.
    """
    if not _namespaces_available():
        return
    report = _privileged_report()

    assert report["every_emitted_command_ran"] is True, report["executed_commands"]
    assert report["last_command_ran_verbatim"] is True, (
        report["last_command"], report["executed_commands"],
    )
    assert report["last_command_is_the_upgrade"] is True, report["last_command"]
    # Through the trampoline the bootstrap installed, not past it into a checkout.
    assert report["trampoline_installed"] is True, report
    assert report["switchyard_resolves_to_trampoline"] is True, report

    # THE DRY RUN an operator is told to do first. It has to describe the real
    # run and change nothing at all -- not the pin, not `current`, not one
    # privileged artifact.
    assert report["upgrade_dry_run_exit"] == 0, report["upgrade_dry_run_text"]
    assert report["upgrade_dry_run_changed_nothing"] is True, report["upgrade_dry_run_text"]
    assert report["upgrade_dry_run_left_pin_stale"] is True, report
    preview = report["upgrade_dry_run_text"]
    assert "would install porter's publication boundary" in preview, preview
    assert "would pin porter publication at" in preview, preview
    for artifact in ("porter-publish-key", "porter.json", "48-porter-publish"):
        assert artifact in preview, (artifact, preview)

    # THE REAL RUN. The release it selects is the reviewed one, and it is
    # recorded where only root can write it, so the next upgrade does not walk
    # back to the old release.
    assert report["upgrade_exit"] == 0, report["upgrade_text"]
    assert report["upgrade_pin_reselected"] is True, report["upgrade_text"]
    assert report["upgrade_pin_source"].endswith(report["upgrade_staged_commit"]), report

    # The privileged bytes a role reaches come from that release.
    assert "switchyard-publish-ref" in report["upgrade_staged_tooling"], report
    assert report["upgrade_staged_commit"], report

    # Key, public half, grant and the narrow rule, each root-owned with the mode
    # it advertises.
    assert report["upgrade_key_installed"] is True, report["upgrade_text"]
    assert report["upgrade_grant_installed"] is True, report["upgrade_text"]
    assert report["upgrade_sudoers_installed"] is True, report["upgrade_text"]
    for path, state in report["upgrade_artifact_modes"].items():
        assert state["uid"] == 0, (path, state)
    assert report["upgrade_artifact_modes"]["/etc/switchyard/publish/porter-publish-key"]["mode"] == "0o600"
    assert report["upgrade_artifact_modes"]["/etc/sudoers.d/48-porter-publish"]["mode"] == "0o440"
    # One rule, one program, no wildcard.
    rule = report["upgrade_sudoers_text"]
    assert rule.count("NOPASSWD:") == 1, rule
    assert "switchyard-publish-ref" in rule and "*" not in rule, rule

    # The host key could not be read, so the boundary is incomplete -- and says
    # so, in the output and in root's own journal, instead of reporting a phase
    # that completed cleanly.
    assert report["known_hosts_lacks_remote_host"] is True, report
    text = report["upgrade_text"]
    assert "known_hosts INCOMPLETE" in text, text
    assert "publication boundary is INCOMPLETE" in text, text
    artifacts = report["upgrade_journal"]["phases"]["artifacts"]
    assert artifacts["state"] == "incomplete", report["upgrade_journal"]
    assert "known_hosts" in artifacts["detail"], artifacts

    # What the ticket requires an operator be told, said by the real command.
    assert "public key:" in text and "fingerprint:" in text, text
    assert "shared project credential still has write authority" in text, text

    # CONVERGENT RETRY: the same command again, with the host reachable. It
    # completes, and the key it made the first time is the key it keeps.
    assert report["upgrade_retry_exit"] == 0, report["upgrade_retry_text"]
    assert report["upgrade_retry_journal"]["phases"]["artifacts"]["state"] == "done", (
        report["upgrade_retry_journal"]
    )
    assert report["upgrade_retry_kept_key"] is True, "the publication key was regenerated"
    assert "already had a publication key and it was left alone" in report["upgrade_retry_text"]
    # And converging re-secures what it finds: known_hosts was left 0666 owned by
    # the shared account, and comes back root-owned at the mode it advertises.
    assert report["upgrade_retry_known_hosts_mode"] == "0o644", report
    assert report["upgrade_retry_known_hosts_uid"] == 0, report

    # And nothing about the tenant's live assignments moved: no role was given a
    # different account, no pane was reassigned, no worktree was touched. An
    # identities cutover is what changes those, and it does not run here.
    assert report["upgrade_runtime_unchanged"] is True, report
    assert report["upgrade_retry_runtime_unchanged"] is True, report
    for restarted in ("restarting", "stopping worker", "cutover"):
        assert restarted not in text.casefold(), (restarted, text)


def test_the_privileged_run_leaves_this_host_alone() -> None:
    """The chroot exercises the real default paths, so prove it stayed inside.

    /usr is the host's, and the defaults write into /usr/local. The run shadows
    that with a bind mount in its own namespace; if that ever stopped working
    this suite would be installing a tenant on the machine it runs on.
    """
    if not _namespaces_available():
        return
    _privileged_report()

    for path in (
        "/usr/local/lib/switchyard/porter",
        "/etc/sudoers.d/48-porter-publish",
        "/etc/switchyard/provision/porter",
        "/etc/switchyard/projects/porter.json",
        "/opt/switchyard/bootstrap",
        "/srv/porter",
    ):
        assert not Path(path).exists(), f"the privileged run escaped its namespace: {path}"


def test_an_installed_release_that_is_not_the_pinned_one_is_refused() -> None:
    """REVIEW FINDING 2 (second round): being a release is not being THE release.

    Pointing at the installed release while pinning a newer ref would otherwise
    stage the older tools and report success, which is the stale-global-release
    case the ticket names.
    """
    if not _namespaces_available():
        return
    report = _privileged_report()

    assert report["stale_release_source_refused"] is True, report
    assert "pinned at" in report["stale_release_reason"], report["stale_release_reason"]
    assert "Nothing was staged" in report["stale_release_reason"], report["stale_release_reason"]
    # And the matching case still works, so this is a check rather than a block.
    assert report["matching_release_source_accepted"] is True, report

    # ROUND 3, FINDING 4: it used to fail open. An exact commit needs no
    # resolution, so it refuses even when nothing could resolve a ref at all.
    assert report["unresolvable_exact_mismatch_refused"] is True, report
    assert "pinned at" in report["unresolvable_exact_mismatch_reason"], report

    # And a symbolic pin is never resolved here, so a role who moves that ref in
    # the shared-account repository cannot make the old release be accepted.
    assert report["moved_ref_refused"] is True, report
    assert "name rather than a commit" in report["moved_ref_reason"], report["moved_ref_reason"]
    assert "writable by the account every role runs as" in report["moved_ref_reason"], report


def test_a_public_half_that_is_not_this_key_s_is_rewritten_from_it() -> None:
    """REVIEW FINDING 4 (second round): present is not the same as correct.

    An interrupted run can leave a public half belonging to a previous key. An
    operator would register something that cannot sign, and every later run
    would report success.
    """
    if not _namespaces_available():
        return
    report = _privileged_report()

    assert report["mismatched_pub_problems"] == [], report
    assert report["mismatched_pub_flagged"] is True, report
    assert report["mismatched_pub_kept_private"] is True, report
    assert report["mismatched_pub_rewritten"] is True, report
    assert report["mismatched_pub_never_recreated"] is True, report


def test_a_hard_failure_never_reports_an_artifact_as_installed() -> None:
    """REVIEW FINDING 3 (second round): what is reported is what is on disk.

    The report used to run before the verdict and print every artifact the run
    meant to write, so a refusal part-way through announced an absent grant and
    an absent sudoers rule as installed.
    """
    if not _namespaces_available():
        return
    report = _privileged_report()
    text = report["hard_failure_text"]

    assert report["hard_failure_problems"] is True, report
    assert report["hard_failure_grant_absent"] is True, report
    for absent in ("/etc/switchyard/publish/syrd.json", "/etc/sudoers.d/48-nopin2-publish"):
        assert f"{absent} NOT INSTALLED" in text, (absent, text)
        assert f"{absent} (0" not in text, (absent, text)
    assert "INCOMPLETE" in text, text
    # And nothing about registering a key, which would read as a finished setup.
    assert "register that key" not in text, text

    # ROUND 3, FINDING 1: a known_hosts that exists but holds another host is
    # still pending. Existence alone would have called it installed, which tells
    # an operator the push will be verified when it will be refused.
    assert report["unrelated_known_hosts_exists"] is True, report
    assert report["unrelated_known_hosts_pending"] == [
        "/etc/switchyard/publish/known_hosts"
    ], report
    stale_text = report["unrelated_known_hosts_text"]
    assert "known_hosts INCOMPLETE" in stale_text, stale_text
    assert "known_hosts (0644" not in stale_text, stale_text
    assert "INCOMPLETE:" in stale_text, stale_text

    # ROUND 3 ADDENDUM: existence is not correctness. A known_hosts left
    # world-writable and owned by another account is re-secured on the next run,
    # and the report gives the mode it really has rather than the advertised one.
    assert report["permissive_artifact_problems"] == [], report
    assert report["permissive_artifact_mode"] == "0o644", report
    assert report["permissive_artifact_uid"] == 0, report
    assert "known_hosts (0644 uid 0)" in report["permissive_artifact_text"], report[
        "permissive_artifact_text"
    ]


def _upgrade_with_no_root_pin(tmp: Path, **kwargs):
    """An upgrade whose publication step has no root-owned remote to pin.

    Its own publication root, because these suites share one per process and a
    key or grant another case left behind changes which refusal comes first.
    """
    sys.path.insert(0, str(ROOT / "tests"))
    import team_launcher_upgrade_cutover_test as cutover

    previous = {
        name: os.environ.get(name)
        for name in ("SWITCHYARD_PUBLISH_ROOT", "SWITCHYARD_PUBLISH_STAGING_ROOT", "SWITCHYARD_SUDOERS_ROOT")
    }
    os.environ["SWITCHYARD_PUBLISH_ROOT"] = str(tmp / "publish" / "etc")
    os.environ["SWITCHYARD_PUBLISH_STAGING_ROOT"] = str(tmp / "publish" / "var")
    os.environ["SWITCHYARD_SUDOERS_ROOT"] = str(tmp / "publish" / "sudoers.d")
    try:
        config_path, _ = cutover._declarative_tenant(tmp)
        result, output, _m = cutover._upgrade(config_path, as_root=True, exists=set(), **kwargs)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return config_path, result, output


def test_the_dry_run_says_the_real_run_could_not_pin_a_remote() -> None:
    """REVIEW FINDING 3 (second round): a preview that cannot warn is not one.

    Asking for a dry run is how an operator finds out that the real run would
    refuse. Returning before the remote is resolved answered a different
    question than the one asked.
    """
    with tempfile.TemporaryDirectory(prefix="pub-dry-remote.") as tmp:
        _config_path, result, output = _upgrade_with_no_root_pin(Path(tmp), dry_run=True)

    assert result == 0, output
    # The preview resolves the remote, so it can say the real run would refuse.
    assert "no remote is pinned" in output, output
    assert "--publish-remote" in output, output
    # And it still previewed the artifacts, so the warning is additional rather
    # than instead of.
    assert "would install" in output, output
    # A dry run changes nothing, warning or not.
    assert "would pin" not in output, output


def test_a_failed_publication_survives_in_the_phase_journal() -> None:
    """REVIEW FINDING 3 (second round): the journal must not be overwritten.

    The publication failure was recorded and then `artifacts=done` was recorded
    unconditionally over it, so the journal claimed a phase completed cleanly
    when part of it had not run.
    """
    with tempfile.TemporaryDirectory(prefix="pub-journal.") as tmp:
        config_path, result, output = _upgrade_with_no_root_pin(Path(tmp))
        config = team_launcher.load_project_config("porter", config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)

    # The rest of the upgrade continued: absence is the safe direction here.
    assert result == 0, output
    state = team_launcher.upgrade_phase_state(journal, "artifacts")
    assert state == "incomplete", (state, journal)
    detail = journal["phases"]["artifacts"]["detail"]
    # The reason itself is whatever the fixture's host makes it; what must
    # survive to the end of the phase is that the boundary did not go in.
    assert "publication boundary not installed" in detail, detail
    assert detail.split("publication boundary not installed:", 1)[1].strip(), detail
    # And nothing was reported as installed that is not there.
    assert "NOT INSTALLED" in output, output


def test_running_from_a_stale_installed_launcher_stops_the_upgrade() -> None:
    """REVIEW FINDING 1 (second round), at the upgrade boundary.

    The wrapper dispatches to whatever is installed, so this process may not be
    the release the operator pinned. Staging that older release's tools and
    reporting success is the trap; it stops and names the bootstrap instead.
    """
    import scripts.team_launcher as team_launcher_module

    class _Stale:
        root = Path("/opt/switchyard/releases/ae0293c")
        marker_commit = "ae0293c7fc22b2c308442b109cbdf3eec1f9145b"

    original = team_launcher_module.running_launcher_release
    with tempfile.TemporaryDirectory(prefix="pub-stale.") as tmp:
        team_launcher_module.running_launcher_release = lambda root=None: _Stale()
        try:
            config_path, result, output = _upgrade_with_no_root_pin(Path(tmp))
            config = team_launcher.load_project_config("porter", config_path)
            journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        finally:
            team_launcher_module.running_launcher_release = original

    assert result == 1, output
    assert "running from installed release ae0293c" in output, output
    assert "nothing privileged was staged" in output, output
    # It names the bootstrap, and the bootstrap runs root-owned code.
    assert "install-switchyard" in output, output
    assert "./install " not in output, output
    assert team_launcher.upgrade_phase_state(journal, "artifacts") == "blocked", journal


def test_a_pending_artifact_becomes_a_problem_the_upgrade_records() -> None:
    """ROUND 3, FINDING 1: pending was reaching nobody.

    A failed keyscan put known_hosts in `pending`, the step returned only
    `problems`, and the phase was recorded done. The chain is asserted in three
    places rather than one, because it broke in the join: the privileged run
    proves a failed keyscan produces `pending` and that the report never calls
    it installed; this proves `pending` becomes a problem the caller sees; and
    `test_a_failed_publication_survives_in_the_phase_journal` proves a problem
    reaches the journal as `incomplete`.
    """
    import scripts.team_launcher as team_launcher_module
    from scripts.ticket_board.publication_boundary import PublicationOutcome

    outcome = PublicationOutcome(pending=["/etc/switchyard/publish/known_hosts"])
    assert outcome.problems == []
    assert outcome.complete is False

    original = team_launcher_module.install_publication_boundary if hasattr(
        team_launcher_module, "install_publication_boundary"
    ) else None
    del original

    with tempfile.TemporaryDirectory(prefix="pub-pending.") as tmp:
        sys.path.insert(0, str(ROOT / "tests"))
        import team_launcher_upgrade_cutover_test as cutover
        import scripts.ticket_board.publication_boundary as boundary

        config_path, _ = cutover._declarative_tenant(Path(tmp))
        config = team_launcher.load_project_config("porter", config_path)
        release = type("R", (), {"root": Path(tmp), "commit": "a" * 40, "materialized": False})()

        real = boundary.install_publication_boundary
        boundary.install_publication_boundary = lambda **kwargs: outcome
        try:
            returned = team_launcher.install_tenant_publication_boundary(
                config, release=release, print_func=lambda _line: None
            )
        finally:
            boundary.install_publication_boundary = real

    # A pending artifact is not success, and the caller is the one that has to
    # be told: it is what decides the phase's verdict.
    assert returned, "pending reached nobody"
    assert any("known_hosts" in problem for problem in returned), returned
    assert any("incomplete" in problem for problem in returned), returned


def test_nothing_privileged_is_written_out_for_root_to_execute() -> None:
    """The reported non-acceptable deployment path, closed.

    Ops was asked to run a generated file as root from a project-account-writable
    /tmp. Under one shared account that file is writable by every role, so this
    step composes argv and runs it here instead.
    """
    module = (ROOT / "scripts" / "ticket_board" / "publication_boundary.py").read_text(encoding="utf-8")

    assert "/tmp" not in module, "nothing privileged may be staged in a world-writable place"
    # `sh -c` appears, but only for pipelines this process builds and runs; no
    # path is ever written and then handed to root to execute.
    for writes_a_script in ("chmod +x", "os.execv", "NamedTemporaryFile", "mkstemp"):
        assert writes_a_script not in module, writes_a_script


def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"publication_boundary_upgrade_test: {len(tests)} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
