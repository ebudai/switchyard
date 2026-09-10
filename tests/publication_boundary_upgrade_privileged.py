#!/usr/bin/env python3
"""The root half of the SYRD-97 suite, run inside a root-owned chroot.

`unshare --map-root-user` maps this uid to 0 but leaves the host's `/` owned by
nobody, and the whole-path check correctly refuses that -- so proving the
accepting case needs a tree root really owns. This builds one, chroots into it,
and reports what it found as JSON on the last line of stdout.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def enter(root: Path, repo: Path) -> None:
    subprocess.run(["mount", "--make-rprivate", "/"], check=False)
    for name in ("usr", "etc", "tmp", "opt", "src", "repo", "dev", "proc", "var"):
        (root / name).mkdir(parents=True, exist_ok=True)
    subprocess.run(["mount", "--rbind", "/usr", str(root / "usr")], check=True)
    subprocess.run(["mount", "--rbind", "/dev", str(root / "dev")], check=True)
    # `mount -t proc` needs a PID namespace of its own; the host's is enough for
    # /dev/stdin, which is a symlink into /proc/self/fd.
    subprocess.run(["mount", "--rbind", "/proc", str(root / "proc")], check=True)
    subprocess.run(["mount", "--rbind", str(repo), str(root / "repo")], check=True)
    # /usr is the host's, and the real defaults write inside it: the trampoline
    # lands in /usr/local/bin and the per-tenant role tooling in
    # /usr/local/lib/switchyard. Shadowing /usr/local with an empty root-owned
    # directory is what lets those defaults be exercised for real without the
    # run touching the host -- the namespace is rprivate, so the bind stops here.
    (root / "usrlocal/bin").mkdir(parents=True, exist_ok=True)
    (root / "usrlocal/lib").mkdir(parents=True, exist_ok=True)
    subprocess.run(["mount", "--bind", str(root / "usrlocal"), str(root / "usr/local")], check=True)
    # The real sudo cannot run here: in a user namespace its setuid binary is
    # owned by nobody and PAM has nothing to talk to. This process is already
    # root, so `sudo` means "run it" -- which is what lets every command below be
    # run exactly as it is written, `sudo` prefix included, instead of edited
    # first. Only the argument forms these commands actually use are handled.
    shim = root / "usrlocal/bin/sudo"
    shim.write_text(
        "#!/bin/sh\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -n|-H|-E) shift ;;\n"
        "    -u) shift 2 ;;\n"
        "    *) break ;;\n"
        "  esac\n"
        "done\n"
        "exec \"$@\"\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    # `install -o root` reads these; without them it cannot resolve the name.
    (root / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/sh\n", encoding="utf-8")
    (root / "etc/group").write_text("root:x:0:\n", encoding="utf-8")
    for name in ("bin", "lib", "lib64"):
        link = root / name
        if not link.exists() and Path("/" + name).is_symlink():
            link.symlink_to(os.readlink("/" + name))
    os.chroot(str(root))
    os.chdir("/")
    # The caller's temp directory is 0700, so the project account this models
    # could not traverse it -- and its own two commands run as that account.
    os.chmod("/", 0o755)
    for shared in ("/tmp",):
        try:
            os.chmod(shared, 0o1777)
        except OSError:
            pass
    # Inside a tree root really owns, the real defaults are what should be
    # exercised. The suites that drive the unprivileged branch redirect these so
    # they do not write to the host's /etc; here that redirection would point at
    # a path outside this chroot.
    for sandbox in (
        "SWITCHYARD_PUBLISH_ROOT",
        "SWITCHYARD_PUBLISH_STAGING_ROOT",
        "SWITCHYARD_SUDOERS_ROOT",
        "SWITCHYARD_SHARED_INSTALL_ROOT",
        "SWITCHYARD_PRIVILEGED_PROVISION_ROOT",
    ):
        os.environ.pop(sandbox, None)
    os.environ.update(
        # /usr/local/bin because that is where the installer puts the public
        # `switchyard`, and the operator sequence's last command names it bare.
        PATH="/usr/local/bin:/usr/bin:/bin",
        HOME="/tmp",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )


def _tree(path: str) -> list[str]:
    """Every path under `path` with the mode and owner it actually has."""
    base = Path(path)
    if not base.exists():
        return []
    return sorted(
        f"{item}:{oct(item.lstat().st_mode)}:{item.lstat().st_uid}"
        for item in [base, *base.rglob("*")]
    )


def _boundary_state(install_root: Path) -> dict[str, object]:
    """Everything a refused or previewed step must have left exactly as it was."""
    current = install_root / "current"
    releases = install_root / "releases"
    return {
        "current": os.readlink(current) if current.is_symlink() else "",
        "releases": sorted(item.name for item in releases.glob("*")) if releases.is_dir() else [],
        "pin": _tree("/etc/switchyard/provision/porter"),
        "publish": _tree("/etc/switchyard/publish"),
        "staging": _tree("/var/lib/switchyard/publish"),
        "tooling": _tree("/usr/local/lib/switchyard"),
        "sudoers": _tree("/etc/sudoers.d"),
    }


def _runtime_fingerprint(config_path: Path) -> dict[str, object]:
    """The tenant's live assignments: which role runs where, and as whom.

    An identities cutover is what changes these -- it stops every worker,
    rewrites the configuration to name per-role accounts and restarts them. A
    publication-boundary upgrade of a shared-account tenant must not.
    """
    raw = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    layout = Path(str(raw.get("layout") or "/nonexistent"))
    return {
        "assignments": [
            {key: role.get(key) for key in ("role", "slot", "target", "run_as_user")}
            for role in raw.get("roles", [])
        ],
        "layout": (
            hashlib.sha256(layout.read_bytes()).hexdigest() if layout.is_file() else ""
        ),
        "worktrees": sorted(str(item) for item in (config_path.parent / "worktrees").rglob("*")),
    }


def main() -> int:
    root = Path(sys.argv[1])
    enter(root, Path(sys.argv[2]))
    sys.path.insert(0, "/repo")

    subprocess.run(["git", "init", "-q", "/src"], check=True)
    Path("/src/f").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", "/src", "add", "."], check=True)
    subprocess.run(["git", "-C", "/src", "commit", "-qm", "one"], check=True)
    commit = subprocess.run(
        ["git", "-C", "/src", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    # The tenant's own cache, with a remote a same-UID role could set. It must
    # never become the destination root pins.
    subprocess.run(["git", "init", "-q", "--bare", "/cache.git"], check=True)
    subprocess.run(
        ["git", "-C", "/cache.git", "remote", "add", "origin", "git@attacker.invalid:evil/repo.git"],
        check=True,
    )

    from scripts.ticket_board.project_provision import (
        publish_grant_path,
        publish_identity_path,
        publish_sudoers_document,
    )
    REMOTE = "git@example.invalid:owner/repo.git"
    from scripts.ticket_board.publication_boundary import (
        install_publication_boundary,
        materialize_trusted_release,
        report_publication_outcome,
    )

    report: dict[str, object] = {}
    # Written into the working tree and never committed, before anything is
    # materialized: a role under the shared account can do exactly this, and the
    # release must not contain it.
    Path("/src/planted").write_text("role-written", encoding="utf-8")
    release, problems = materialize_trusted_release(
        commit, source_repo=Path("/src"), install_root=Path("/opt")
    )
    assert release is not None, problems
    report["materialized"] = release.materialized
    report["release_commit"] = release.commit
    report["marker_matches_commit"] = (
        json.loads((release.root / ".switchyard-release.json").read_text())["commit"] == commit
    )
    again, _ = materialize_trusted_release(commit, source_repo=Path("/src"), install_root=Path("/opt"))
    report["second_call_rebuilt"] = bool(again and again.materialized)
    report["worktree_file_absent_from_release"] = not (release.root / "planted").exists()

    key = publish_identity_path("syrd")
    grant = publish_grant_path("syrd")
    sudoers = "/etc/sudoers.d/48-syrd-publish"
    Path("/etc/sudoers.d").mkdir(parents=True, exist_ok=True)
    document = publish_sudoers_document("syrd", "root")

    # The preview, as root, before anything is installed: it must name the exact
    # release and every privileged artifact, and change nothing.
    preview: list[str] = []
    install_publication_boundary(
        project="syrd", release=release, registration_root=Path("/etc/switchyard/provision"),
        declared_remote=REMOTE,
        sudoers_path=sudoers, sudoers_document=document, dry_run=True, print_func=preview.append,
    )
    report["dry_run_text"] = "\n".join(preview)
    report["dry_run_changed_nothing"] = not Path("/etc/switchyard/publish").exists()

    printed: list[str] = []
    first = install_publication_boundary(
        project="syrd", release=release, registration_root=Path("/etc/switchyard/provision"),
        declared_remote=REMOTE,
        sudoers_path=sudoers, sudoers_document=document, print_func=printed.append,
    )
    report["first_problems"] = first.problems
    report["key_created_first"] = first.key_created
    report["public_key"] = first.public_key
    report["fingerprint"] = first.fingerprint
    report["key"] = key
    report["grant"] = grant
    report["sudoers"] = sudoers
    report["grant_remote"] = json.loads(Path(grant).read_text())["remote"]
    report["modes"] = {
        path: {"mode": oct(os.stat(path).st_mode & 0o777), "uid": os.stat(path).st_uid}
        for path in ("/etc/switchyard/publish", "/var/lib/switchyard/publish",
                     key, f"{key}.pub", grant, sudoers)
    }

    before = Path(key).read_bytes()
    second = install_publication_boundary(
        project="syrd", release=release, registration_root=Path("/etc/switchyard/provision"),
        declared_remote=REMOTE,
        sudoers_path=sudoers, sudoers_document=document, print_func=printed.append,
    )
    report["retry_problems"] = second.problems
    report["key_created_again"] = second.key_created
    report["key_unchanged_on_retry"] = Path(key).read_bytes() == before

    lines: list[str] = []
    report_publication_outcome(second, project="syrd", print_func=lines.append)
    report["report_text"] = "\n".join(lines)

    # A releases directory somebody else can write is refused, and refusing
    # leaves what is there alone rather than replacing it.
    planted = Path("/opt/releases/planted")
    planted.mkdir()
    (planted / "evidence").write_text("still here", encoding="utf-8")
    os.chmod("/opt/releases", 0o777)
    refused, reasons = materialize_trusted_release(
        "0" * 40, source_repo=Path("/src"), install_root=Path("/opt")
    )
    report["untrusted_refused"] = refused is None
    report["untrusted_reason"] = reasons[0] if reasons else ""
    report["untrusted_left_alone"] = (planted / "evidence").read_text() == "still here"
    os.chmod("/opt/releases", 0o755)

    unknown, unknown_reasons = materialize_trusted_release(
        "0" * 40, source_repo=Path("/src"), install_root=Path("/opt")
    )
    report["unknown_commit_refused"] = unknown is None
    report["unknown_commit_reason"] = unknown_reasons[0] if unknown_reasons else ""
    report["unknown_commit_left_no_tree"] = not Path("/opt/releases/" + "0" * 40).exists()

    # A rule that is not valid sudoers is never allowed to become live: an
    # unparsable file in sudoers.d can lock the host out of sudo entirely.
    invalid = install_publication_boundary(
        project="syrd", release=release, registration_root=Path("/etc/switchyard/provision"),
        declared_remote=REMOTE,
        sudoers_path="/etc/sudoers.d/48-invalid-publish",
        sudoers_document="this is not sudoers syntax at all\n",
        print_func=printed.append,
    )
    report["invalid_sudoers_refused"] = bool(invalid.problems)
    report["invalid_sudoers_reason"] = invalid.problems[0] if invalid.problems else ""
    report["invalid_sudoers_not_installed"] = not Path("/etc/sudoers.d/48-invalid-publish").exists()
    report["invalid_sudoers_left_no_staging"] = not Path(
        "/etc/sudoers.d/48-invalid-publish.staged"
    ).exists()

    # A release tree that is tampered with after it lands is not consumed: the
    # verification is of the tree that is installed, not of one that was.
    tampered = Path("/opt/releases/tampered")
    tampered.mkdir()
    (tampered / ".switchyard-release.json").write_text(
        json.dumps({"commit": "tampered"}), encoding="utf-8"
    )
    os.chmod(tampered, 0o777)
    bad, bad_reasons = materialize_trusted_release(
        "tampered", source_repo=Path("/src"), install_root=Path("/opt")
    )
    report["tampered_release_refused"] = bad is None
    report["tampered_release_reason"] = bad_reasons[0] if bad_reasons else ""

    loose = Path("/loose")
    loose.mkdir()
    os.chmod(loose, 0o777)
    under_loose, loose_reasons = materialize_trusted_release(
        commit, source_repo=Path("/src"), install_root=loose / "opt"
    )
    report["writable_parent_refused"] = under_loose is None
    report["writable_parent_reason"] = loose_reasons[0] if loose_reasons else ""

    # FINDING 2: the tenant's cache names an attacker's remote. Nothing root
    # writes may come from it, and with no root-owned pin there is no grant.
    report["grant_never_names_tenant_remote"] = "attacker.invalid" not in Path(grant).read_text()
    os.unlink(grant)
    os.unlink("/etc/switchyard/provision/syrd/publish-remote")
    unpinned_now = install_publication_boundary(
        project="syrd", release=release, registration_root=Path("/etc/switchyard/provision"),
        sudoers_path="/etc/sudoers.d/48-nopin-publish", sudoers_document=document,
        print_func=printed.append,
    )
    report["no_root_pin_refuses"] = bool(unpinned_now.problems)
    report["no_root_pin_reason"] = unpinned_now.problems[0] if unpinned_now.problems else ""
    report["no_root_pin_wrote_no_grant"] = not Path(grant).exists()

    # FINDING 3: an interrupted run that left the public half behind converges
    # without replacing the key the forge already knows.
    restored_pin = install_publication_boundary(
        project="syrd", release=release, registration_root=Path("/etc/switchyard/provision"),
        declared_remote=REMOTE, sudoers_path=sudoers, sudoers_document=document,
        print_func=printed.append,
    )
    assert not restored_pin.problems, restored_pin.problems
    assert Path(f"{key}.pub").exists(), sorted(p.name for p in Path("/etc/switchyard/publish").iterdir())
    private_before = Path(key).read_bytes()
    public_before = Path(f"{key}.pub").read_text()
    os.unlink(f"{key}.pub")
    os.chmod(key, 0o644)
    repaired = install_publication_boundary(
        project="syrd", release=release, registration_root=Path("/etc/switchyard/provision"),
        declared_remote=REMOTE, sudoers_path=sudoers, sudoers_document=document,
        print_func=printed.append,
    )
    report["damaged_key_problems"] = repaired.problems
    assert Path(f"{key}.pub").exists(), (repaired.problems, repaired.public_key_restored)
    report["damaged_key_kept_private"] = Path(key).read_bytes() == private_before
    report["damaged_key_restored_public"] = Path(f"{key}.pub").read_text().split()[:2] == public_before.split()[:2]
    report["damaged_key_flagged_restore"] = repaired.public_key_restored
    report["damaged_key_never_recreated"] = repaired.key_created is False
    report["damaged_key_modes"] = {
        key: oct(os.stat(key).st_mode & 0o777),
        f"{key}.pub": oct(os.stat(f"{key}.pub").st_mode & 0o777),
    }

    # FINDING 4: an unreachable forge leaves known_hosts pending, and the report
    # must not claim it is installed.
    # example.invalid does not resolve either, so it may already be absent.
    Path("/etc/switchyard/publish/known_hosts").unlink(missing_ok=True)
    partial_lines: list[str] = []
    partial = install_publication_boundary(
        project="syrd", release=release, registration_root=Path("/etc/switchyard/provision"),
        declared_remote="git@nonexistent.invalid:owner/repo.git",
        sudoers_path=sudoers, sudoers_document=document, print_func=printed.append,
    )
    report_publication_outcome(partial, project="syrd", print_func=partial_lines.append)
    report["partial_pending"] = partial.pending

    # And again with a known_hosts that exists but holds some other host. It is
    # a file, so existence alone would call it installed; it is not the pinned
    # host, so publication would still refuse.
    Path("/etc/switchyard/publish/known_hosts").write_text(
        "elsewhere.invalid ssh-ed25519 AAAAsomethingelse\n", encoding="utf-8"
    )
    stale_hosts_lines: list[str] = []
    stale_hosts = install_publication_boundary(
        project="syrd", release=release, registration_root=Path("/etc/switchyard/provision"),
        declared_remote="git@nonexistent.invalid:owner/repo.git",
        sudoers_path=sudoers, sudoers_document=document, print_func=printed.append,
    )
    report_publication_outcome(stale_hosts, project="syrd", print_func=stale_hosts_lines.append)
    report["unrelated_known_hosts_pending"] = stale_hosts.pending
    report["unrelated_known_hosts_exists"] = Path("/etc/switchyard/publish/known_hosts").exists()
    report["unrelated_known_hosts_text"] = "\n".join(stale_hosts_lines)
    report["partial_complete"] = partial.complete
    report["partial_text"] = "\n".join(partial_lines)

    # REVIEW FINDING 1 (second round). The public wrapper sends privileged
    # commands to whatever `<install root>/current` points at, so an operator who
    # pulls a checkout and runs `sudo switchyard upgrade` is still running the
    # previously installed launcher. Driven here from that exact state.
    old_commit = subprocess.run(
        ["git", "-C", "/src", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    # A whole Switchyard source, because the release the bootstrap builds has to
    # be a working one: the installer smoke-tests what it installs.
    shutil.copytree("/repo/scripts", "/src/scripts", dirs_exist_ok=True)
    shutil.copytree("/repo/skills", "/src/skills", dirs_exist_ok=True)
    shutil.copy("/repo/switchyard", "/src/switchyard")
    subprocess.run(["git", "-C", "/src", "add", "-A"], check=True)
    subprocess.run(["git", "-C", "/src", "commit", "-qm", "switchyard source"], check=True)
    new_commit = subprocess.run(
        ["git", "-C", "/src", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    # An install root whose `current` is the OLD release, exactly as the live
    # host's is.
    install_root = Path("/opt/switchyard")  # the real default
    old_release = install_root / "releases" / old_commit
    old_release.mkdir(parents=True)
    (old_release / ".switchyard-release.json").write_text(
        json.dumps({"commit": old_commit, "source_repo": "/src", "source_ref": old_commit}),
        encoding="utf-8",
    )
    (old_release / "scripts").mkdir()
    shutil.copy("/repo/scripts/install-switchyard", old_release / "scripts/install-switchyard")
    os.chmod(old_release / "scripts/install-switchyard", 0o755)
    (install_root / "current").symlink_to(old_release)
    # ADVERSARIAL: a same-UID role installs a replacement ref for the reviewed
    # commit whose tree carries a malicious executable `switchyard`. `git archive
    # <sha>` in that repository honours it, which is why root is given no access
    # to the repository at all.
    marker_path = Path("/tmp/attacker-ran")
    Path("/src/switchyard").write_text(
        f"#!/bin/sh\nprintf pwned > {marker_path}\nexit 0\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", "/src", "add", "-A"], check=True)
    subprocess.run(["git", "-C", "/src", "commit", "-qm", "attacker"], check=True)
    attacker_commit = subprocess.run(
        ["git", "-C", "/src", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(["git", "-C", "/src", "replace", new_commit, attacker_commit], check=True)
    # A commit with no history in common with the reviewed one, carrying the same
    # malicious `switchyard`. The bundle case below needs this rather than the
    # commit above: `attacker_commit` is a child of the reviewed commit, so a
    # bundle of it carries the reviewed objects too and root simply finds them.
    blob = subprocess.run(
        ["git", "-C", "/src", "hash-object", "-w", "--stdin"],
        input=f"#!/bin/sh\nprintf pwned > {marker_path}\nexit 0\n",
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "-C", "/src", "mktree"], input=f"100755 blob {blob}\tswitchyard\n",
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    unrelated_commit = subprocess.run(
        ["git", "-C", "/src", "commit-tree", tree, "-m", "unrelated"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    substituted = subprocess.run(
        ["sh", "-c", f"git -C /src archive {new_commit} | tar -xO switchyard"],
        capture_output=True, text=True,
    )
    # Proof the substitution is real in that repository, so the bootstrap below
    # is refusing it rather than never having been exposed to it.
    report["replacement_substitutes_in_source"] = "pwned" in substituted.stdout
    report["attacker_marker_before"] = marker_path.exists()

    # The live shape: the checkout the bootstrap reads belongs to the shared
    # project account, not to root, so git's ownership check applies.
    PROJECT_UID = 1006
    for path in [Path("/src"), *Path("/src").rglob("*")]:
        try:
            os.chown(path, PROJECT_UID, PROJECT_UID, follow_symlinks=False)
        except OSError:
            pass
    report["source_owned_by_project_uid"] = os.stat("/src").st_uid == PROJECT_UID
    unsafe = subprocess.run(
        ["git", "-C", "/src", "rev-parse", "HEAD"], capture_output=True, text=True
    )
    # Proof the check is real here, so the bootstrap below is not passing by luck.
    report["git_refuses_foreign_source"] = unsafe.returncode != 0 and "dubious" in unsafe.stderr

    report["bootstrap_started_stale"] = (
        json.loads((install_root / "current" / ".switchyard-release.json").read_text())["commit"]
        == old_commit
    )

    from scripts.team_launcher import trusted_bootstrap_commands
    sequence = trusted_bootstrap_commands(
        Path("/src"), new_commit, project="porter", publish_remote=REMOTE
    )
    report["bootstrap_sequence"] = sequence
    bundle_path = next(
        (word for line in sequence for word in line.split() if word.endswith(".bundle")), ""
    )
    command = " && ".join(sequence[:-1])
    report["bootstrap_command"] = command
    # Concrete: no placeholder is left for a shell to choke on.
    report["sequence_has_no_placeholders"] = not any(
        "<" in line or ">" in line for line in sequence
    )
    # Root never reads the operator's checkout.
    root_lines = [line for line in sequence if line.startswith("sudo")]
    # The boundary is that root never runs git AGAINST the operator's repository
    # and never hands it to the installer as a source. Reading one file out of it
    # is not that: a bundle is data, and root demands the exact commit from it
    # afterwards.
    report["root_never_reads_source"] = all(
        "-C /src " not in f"{line} " and "SWITCHYARD_SOURCE_REPO=/src " not in f"{line} "
        for line in root_lines
    )
    # Precise: the operator's repository is `/src`, and the root-owned staging
    # checkout is `<bootstrap>/src`, so a substring test confuses the two.
    report["root_reads_only_the_bundle_from_source"] = all(
        not re.search(r"(?<![\w/.])/src/", line) or ".bundle" in line for line in root_lines
    )
    report["bootstrap_disables_replacement"] = all(
        "GIT_NO_REPLACE_OBJECTS=1" in line for line in root_lines if " git " in line
    )
    # The last command is what durably re-selects the release; without it the
    # root-owned pin still names the old one and the next upgrade walks back.
    repin = sequence[-1]
    report["sequence_repins"] = (
        len(sequence) > 1
        and repin.startswith("sudo switchyard upgrade ")
        and "--deploy-ref" in repin
        and new_commit in repin
        and "--source-repo" in repin
    )
    # Only root-owned code: the installer it names belongs to the release that is
    # already installed, not to the candidate checkout.
    report["bootstrap_runs_installed_code"] = any(
        str(install_root / "current" / "scripts" / "install-switchyard") in line for line in sequence
    )
    report["bootstrap_names_no_candidate_script"] = "/src/scripts/install-switchyard" not in command

    # The live stale pin: the root-owned record still selects the OLD release,
    # so an upgrade that only repointed `current` would recover it and go back.
    provision = Path("/etc/switchyard/provision/porter")
    provision.mkdir(parents=True, exist_ok=True)
    (provision / "upgrade-source.json").write_text(
        json.dumps({
            "schema": "switchyard.upgrade-source.v1", "project": "porter",
            "source_repo": str(old_release), "deploy_ref": old_commit,
            "commit_git_dir": "/cache.git", "at": "2026-09-09T00:00:00+00:00",
        }),
        encoding="utf-8",
    )
    report["pin_started_stale"] = json.loads(
        (provision / "upgrade-source.json").read_text()
    )["deploy_ref"] == old_commit

    # A registered tenant, because the last command of the sequence is
    # `switchyard upgrade porter ...` and that has to go through the installed
    # trampoline, the CLI parser and the project registry rather than through an
    # internal call. Its roles share the project account, which is the live shape
    # (SYRD-69), so there is no identity cutover to run and nothing is restarted.
    tenant_dir = Path("/srv/porter")
    (tenant_dir / "worktrees").mkdir(parents=True, exist_ok=True)
    tenant_layout = tenant_dir / "layout.json"
    tenant_layout.write_text(
        json.dumps({"KonsoleTabs": [{"Widgets": [
            {"SessionRestoreId": 0, "Command": "", "WorkingDirectory": ""}
        ]}]}),
        encoding="utf-8",
    )
    tenant_config_path = tenant_dir / "porter.json"
    tenant_config_path.write_text(
        json.dumps({
            "desktop_access": {"mode": "headless"},
            "project": "porter", "project_name": "porter", "ticket_prefix": "P",
            "layout": str(tenant_layout), "repository": "/src", "run_as_user": "root",
            "worktree_base": str(tenant_dir / "worktrees"),
            "roles": [{"role": "app", "slot": 0, "target": "porter-app:0.0", "cli": ["codex"]}],
        }),
        encoding="utf-8",
    )
    registry_dir = Path("/etc/switchyard/projects")
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "porter.json").write_text(
        json.dumps({
            "schema": "switchyard.project-registry.v1", "slug": "porter", "name": "porter",
            "config_path": str(tenant_config_path),
        }),
        encoding="utf-8",
    )

    # What the tenant's runtime looks like before any of this runs. The upgrade
    # must leave it alone: nothing here restarts a worker or reassigns a pane.
    runtime_before = _runtime_fingerprint(tenant_config_path)

    # Run each line exactly as it was rendered, `sudo` and all. Nothing about a
    # command is edited first: the install root, the install path and the tenant
    # control root are the real defaults, so the environment carries no override
    # for any of them, and the shim above makes the `sudo` prefix mean what it
    # means on a host with a sudo-capable operator.
    # Each line runs under the identity it is written for: the operator's own
    # commands as the operator in their own repository, root's as root. That
    # split is the boundary this ticket is about.
    env = dict(os.environ)
    executed: list[str] = []

    def run_step(line: str, *, suffix: str = "") -> tuple[bool, str, subprocess.CompletedProcess[str]]:
        as_root = line.startswith("sudo ")
        step = line + suffix
        executed.append(step)
        return as_root, step, subprocess.run(
            ["sh", "-c", step], capture_output=True, text=True, env=env, cwd="/tmp",
            **({} if as_root else {"user": PROJECT_UID, "group": PROJECT_UID}),
        )

    # SUBSTITUTION, AND RECOVERABILITY. The operator's two commands run in a
    # repository every role can write, so a role can point the bootstrap ref at
    # a commit of their own between them -- the bundle then carries the reviewed
    # ref NAME and the attacker's commit. Root must refuse, and a refusal must
    # leave `current`, the root-owned pin and every privileged artifact exactly
    # as they were.
    tampered_before = _boundary_state(install_root)
    tampered_failure = ""
    for line in sequence[:-1]:
        # The ref is created pointing at the attacker's commit instead. Only the
        # target is changed; the ref's name still names the reviewed commit.
        moved = (
            unrelated_commit.join(line.rsplit(new_commit, 1))
            if " update-ref " in line else line
        )
        as_root, step, done = run_step(moved)
        if done.returncode != 0:
            tampered_failure = f"{step} -> {(done.stderr or done.stdout)[-200:]}"
            break
    report["tampered_ref_refused"] = bool(tampered_failure)
    report["tampered_ref_failure"] = tampered_failure
    report["tampered_ref_changed_nothing"] = _boundary_state(install_root) == tampered_before
    report["tampered_ref_did_not_run_attacker"] = not marker_path.exists()
    # The bundle really did carry the attacker's commit, so the refusal is a
    # refusal rather than a fixture that never delivered anything.
    carried = subprocess.run(
        ["git", "bundle", "list-heads", str(bundle_path)], capture_output=True, text=True,
    )
    # The ref NAME contains the reviewed commit, so only the object column says
    # what the bundle actually carries.
    carried_shas = [line.split()[0] for line in carried.stdout.splitlines() if line.split()]
    report["tampered_bundle_carried_attacker"] = unrelated_commit in carried_shas
    report["tampered_bundle_lacks_reviewed_commit"] = new_commit not in carried_shas
    # Nothing half-built may be left behind, or the run below would be resuming
    # rather than converging.
    shutil.rmtree(install_root / "bootstrap", ignore_errors=True)
    Path(bundle_path).unlink(missing_ok=True)

    failures: list[str] = []
    for line in sequence[:-1]:
        as_root, step, done = run_step(line)
        if done.returncode != 0:
            failures.append(f"{'root' if as_root else 'operator'}: {step} -> {done.stderr[-200:]}")
            break
    report["bootstrap_exit"] = 1 if failures else 0
    report["bootstrap_error"] = "\n".join(failures)[-400:]
    # The attacker's `switchyard` must never have run, and what landed must be
    # the reviewed tree rather than the replacement.
    report["attacker_marker_after"] = marker_path.exists()
    materialized = install_root / "releases" / new_commit / "switchyard"
    report["materialized_is_not_attacker"] = (
        "pwned" not in materialized.read_text(encoding="utf-8") if materialized.exists() else True
    )
    landed = install_root / "current" / ".switchyard-release.json"
    report["bootstrap_landed_on_new_commit"] = (
        json.loads(landed.read_text())["commit"] == new_commit if landed.exists() else False
    )
    # What the ticket claims an operator can do. Before the bootstrap the
    # installed launcher has no `--publish-remote` at all, which is exactly why
    # `sudo switchyard upgrade syrd --publish-remote ...` could not work; after
    # it, the command the ticket names is there.
    def upgrade_help(release_root: Path) -> str:
        proc = subprocess.run(
            [sys.executable, str(release_root / "switchyard"), "upgrade", "--help"],
            capture_output=True, text=True,
            env=dict(os.environ),
        )
        return f"{proc.stdout}\n{proc.stderr}"

    report["old_launcher_lacks_publish_remote"] = "--publish-remote" not in upgrade_help(old_release)
    report["new_launcher_has_publish_remote"] = "--publish-remote" in upgrade_help(
        install_root / "current"
    )

    # THE LAST COMMAND OF THE SEQUENCE, RUN. Not inspected, not simulated by
    # calling what it would have called: `switchyard upgrade porter ...` through
    # the trampoline the bootstrap just installed, so the public wrapper, the CLI
    # parser, the project registry, the phase order and the journal are all in
    # the path being tested. Checking the string and then calling
    # `record_upgrade_source()` skipped exactly the boundary that stranded Ops
    # (SYRD-97 review, round 4).
    from scripts.ticket_board.project_provision import publish_sudoers_path
    last = sequence[-1]
    porter_key = publish_identity_path("porter")
    porter_grant = publish_grant_path("porter")
    porter_sudoers = publish_sudoers_path("porter")
    porter_journal = Path("/etc/switchyard/provision/porter/upgrade.json")
    porter_tooling = Path("/usr/local/lib/switchyard/porter")
    report["last_command"] = last
    report["last_command_is_the_upgrade"] = last.startswith("sudo switchyard upgrade porter ")
    report["trampoline_installed"] = Path("/usr/local/bin/switchyard").is_file()

    # The trampoline is what `switchyard` resolves to; nothing here reaches past
    # it into the checkout.
    resolved = subprocess.run(
        ["sh", "-c", "command -v switchyard"], capture_output=True, text=True, env=env
    )
    report["switchyard_resolves_to_trampoline"] = resolved.stdout.strip() == "/usr/local/bin/switchyard"

    # First, what an upgrade that carries no arguments selects. The root-owned
    # pin still names the old release, so this is the state repointing `current`
    # alone would leave an operator in: the very next upgrade walks back. Proved
    # by running the command, not by asking the resolver.
    _, stale_step, stale = run_step("sudo switchyard upgrade porter --dry-run")
    report["argumentless_step"] = stale_step
    report["argumentless_exit"] = stale.returncode
    report["argumentless_text"] = f"{stale.stdout}\n{stale.stderr}"
    report["argumentless_recovers_old_release"] = (
        old_commit in f"{stale.stdout}{stale.stderr}"
        and new_commit not in f"{stale.stdout}{stale.stderr}"
    )

    # DRY RUN FIRST, exactly as an operator is told to. It must describe the real
    # run and change nothing at all.
    before_preview = _boundary_state(install_root)
    _, dry_step, dry = run_step(last, suffix=" --dry-run")
    report["upgrade_dry_run_step"] = dry_step
    report["upgrade_dry_run_exit"] = dry.returncode
    report["upgrade_dry_run_text"] = f"{dry.stdout}\n{dry.stderr}"
    report["upgrade_dry_run_changed_nothing"] = _boundary_state(install_root) == before_preview
    report["upgrade_dry_run_left_pin_stale"] = json.loads(
        (provision / "upgrade-source.json").read_text()
    )["deploy_ref"] == old_commit

    # THE REAL RUN, first against a host whose keys cannot be read. The boundary
    # must install what it can, refuse to claim what it could not, and record the
    # phase as incomplete rather than done.
    report["known_hosts_lacks_remote_host"] = "example.invalid" not in (
        Path("/etc/switchyard/publish/known_hosts").read_text(encoding="utf-8")
        if Path("/etc/switchyard/publish/known_hosts").exists() else ""
    )
    _, real_step, real = run_step(last)
    report["upgrade_step"] = real_step
    report["upgrade_exit"] = real.returncode
    report["upgrade_text"] = f"{real.stdout}\n{real.stderr}"
    report["upgrade_pin_reselected"] = json.loads(
        (provision / "upgrade-source.json").read_text()
    )["deploy_ref"] == new_commit
    report["upgrade_pin_source"] = json.loads(
        (provision / "upgrade-source.json").read_text()
    )["source_repo"]
    report["upgrade_key_installed"] = Path(porter_key).is_file()
    report["upgrade_grant_installed"] = Path(porter_grant).is_file()
    report["upgrade_sudoers_installed"] = Path(porter_sudoers).is_file()
    report["upgrade_sudoers_text"] = (
        Path(porter_sudoers).read_text(encoding="utf-8") if Path(porter_sudoers).is_file() else ""
    )
    report["upgrade_artifact_modes"] = {
        str(path): {"mode": oct(os.stat(path).st_mode & 0o777), "uid": os.stat(path).st_uid}
        for path in (porter_key, f"{porter_key}.pub", porter_grant, porter_sudoers)
        if Path(path).exists()
    }
    # Staged from the release the command selected, and carrying that release's
    # marker -- the privileged bytes a role reaches are the reviewed ones.
    report["upgrade_staged_tooling"] = sorted(
        item.name for item in porter_tooling.iterdir()
    ) if porter_tooling.is_dir() else []
    staged_marker = porter_tooling / ".switchyard-release.json"
    report["upgrade_staged_commit"] = (
        json.loads(staged_marker.read_text(encoding="utf-8")).get("commit", "")
        if staged_marker.is_file() else ""
    )
    report["upgrade_journal"] = (
        json.loads(porter_journal.read_text(encoding="utf-8")) if porter_journal.is_file() else {}
    )
    report["upgrade_runtime_unchanged"] = _runtime_fingerprint(tenant_config_path) == runtime_before
    key_after_first = Path(porter_key).read_bytes() if Path(porter_key).is_file() else b""

    # CONVERGENT RETRY: with the host's keys reachable -- modelled by the entry
    # already being pinned -- the same command completes, and the key it made
    # last time is the key it keeps.
    Path("/etc/switchyard/publish/known_hosts").write_text(
        "example.invalid ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleHostKeyMaterial\n",
        encoding="utf-8",
    )
    # Left permissive and owned by the shared account, which is what an
    # interrupted earlier run leaves behind. Converging has to re-secure it, not
    # just find it present.
    os.chmod("/etc/switchyard/publish/known_hosts", 0o666)
    os.chown("/etc/switchyard/publish/known_hosts", PROJECT_UID, PROJECT_UID)
    _, retry_step, retry = run_step(last)
    report["upgrade_retry_step"] = retry_step
    report["upgrade_retry_exit"] = retry.returncode
    report["upgrade_retry_text"] = f"{retry.stdout}\n{retry.stderr}"
    report["upgrade_retry_journal"] = (
        json.loads(porter_journal.read_text(encoding="utf-8")) if porter_journal.is_file() else {}
    )
    report["upgrade_retry_kept_key"] = (
        Path(porter_key).is_file()
        and Path(porter_key).read_bytes() == key_after_first
        and bool(key_after_first)
    )
    report["upgrade_retry_known_hosts_mode"] = (
        oct(os.stat("/etc/switchyard/publish/known_hosts").st_mode & 0o777)
        if Path("/etc/switchyard/publish/known_hosts").exists() else ""
    )
    report["upgrade_retry_known_hosts_uid"] = (
        os.stat("/etc/switchyard/publish/known_hosts").st_uid
        if Path("/etc/switchyard/publish/known_hosts").exists() else -1
    )
    report["upgrade_retry_runtime_unchanged"] = (
        _runtime_fingerprint(tenant_config_path) == runtime_before
    )

    # Every command of the sequence was run as emitted -- the last one included,
    # which is the one the previous revision only inspected.
    report["executed_commands"] = executed
    report["every_emitted_command_ran"] = all(line in executed for line in sequence)
    report["last_command_ran_verbatim"] = sequence[-1] in executed

    # REVIEW FINDING 2 (second round): a source that IS the installed release,
    # combined with a newer pinned ref, must refuse rather than stage the older
    # tools and report success.
    from scripts.team_launcher import resolve_trusted_upgrade_release
    # An exact commit pinned against a release that is a different commit. No
    # ref resolution is involved, so there is no state in which this fails open.
    mismatched, mismatch_reasons = resolve_trusted_upgrade_release(
        old_release, new_commit, ref_is_pinned=True
    )
    report["stale_release_source_refused"] = mismatched is None
    report["stale_release_reason"] = mismatch_reasons[0] if mismatch_reasons else ""
    agreed, _ = resolve_trusted_upgrade_release(old_release, old_commit, ref_is_pinned=True)
    report["matching_release_source_accepted"] = agreed is not None

    # The exact mismatch must refuse even when nothing could resolve the ref:
    # the release's recorded source is gone entirely here.
    (old_release / ".switchyard-release.json").write_text(
        json.dumps({"commit": old_commit, "source_repo": "/gone", "source_ref": old_commit}),
        encoding="utf-8",
    )
    unresolvable, unresolvable_reasons = resolve_trusted_upgrade_release(
        old_release, new_commit, ref_is_pinned=True
    )
    report["unresolvable_exact_mismatch_refused"] = unresolvable is None
    report["unresolvable_exact_mismatch_reason"] = (
        unresolvable_reasons[0] if unresolvable_reasons else ""
    )

    # A symbolic pin is never resolved here at all, so a role who moves that ref
    # in the shared-account repository cannot make the old release be accepted.
    moved = Path("/moved")
    subprocess.run(["git", "init", "-q", str(moved)], check=True)
    Path(moved / "f").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(moved), "add", "."], check=True)
    subprocess.run(["git", "-C", str(moved), "commit", "-qm", "x"], check=True)
    subprocess.run(["git", "-C", str(moved), "branch", "-f", "release"], check=True)
    os.chmod(moved, 0o777)  # what a shared account can do to its own repository
    (old_release / ".switchyard-release.json").write_text(
        json.dumps({"commit": old_commit, "source_repo": str(moved), "source_ref": old_commit}),
        encoding="utf-8",
    )
    symbolic, symbolic_reasons = resolve_trusted_upgrade_release(
        old_release, "release", ref_is_pinned=True
    )
    report["moved_ref_refused"] = symbolic is None
    report["moved_ref_reason"] = symbolic_reasons[0] if symbolic_reasons else ""
    (old_release / ".switchyard-release.json").write_text(
        json.dumps({"commit": old_commit, "source_repo": "/src", "source_ref": old_commit}),
        encoding="utf-8",
    )

    # REVIEW FINDING 4 (second round): a public half that does not belong to the
    # private key is rewritten from it, and the private key is not replaced.
    good_key = Path(key).read_bytes()
    good_pub = Path(f"{key}.pub").read_text()
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "somebody else", "-f", "/tmp/other"],
        check=True,
    )
    Path(f"{key}.pub").write_text(Path("/tmp/other.pub").read_text(), encoding="utf-8")
    repaired_pub = install_publication_boundary(
        project="syrd", release=release, registration_root=Path("/etc/switchyard/provision"),
        declared_remote=REMOTE, sudoers_path=sudoers, sudoers_document=document,
        print_func=printed.append,
    )
    report["mismatched_pub_problems"] = repaired_pub.problems
    report["mismatched_pub_flagged"] = repaired_pub.public_key_mismatched
    report["mismatched_pub_kept_private"] = Path(key).read_bytes() == good_key
    report["mismatched_pub_rewritten"] = (
        Path(f"{key}.pub").read_text().split()[:2] == good_pub.split()[:2]
    )
    report["mismatched_pub_never_recreated"] = repaired_pub.key_created is False

    # REVIEW FINDING 3 (second round): a hard failure must not print artifacts it
    # never reached as installed.
    Path(grant).unlink(missing_ok=True)
    Path("/etc/switchyard/provision/syrd/publish-remote").unlink(missing_ok=True)
    failed_lines: list[str] = []
    failed = install_publication_boundary(
        project="syrd", release=release, registration_root=Path("/etc/switchyard/provision"),
        sudoers_path="/etc/sudoers.d/48-nopin2-publish", sudoers_document=document,
        print_func=failed_lines.append,
    )
    report_publication_outcome(failed, project="syrd", print_func=failed_lines.append)
    report["hard_failure_problems"] = bool(failed.problems)
    report["hard_failure_text"] = "\n".join(failed_lines)
    report["hard_failure_grant_absent"] = not Path(grant).exists()

    # ROUND 3 ADDENDUM: an artifact that merely exists is not correct. A
    # known_hosts left permissive and owned by somebody else must be re-secured
    # on the next run, and never reported as though it were already right.
    kh = "/etc/switchyard/publish/known_hosts"
    Path(kh).write_text("example.invalid ssh-ed25519 AAAA\n", encoding="utf-8")
    os.chmod(kh, 0o666)
    os.chown(kh, 1006, 1006)
    resecured = install_publication_boundary(
        project="syrd", release=release, registration_root=Path("/etc/switchyard/provision"),
        declared_remote=REMOTE, sudoers_path=sudoers, sudoers_document=document,
        print_func=printed.append,
    )
    info = os.stat(kh)
    report["permissive_artifact_problems"] = resecured.problems
    report["permissive_artifact_mode"] = oct(info.st_mode & 0o777)
    report["permissive_artifact_uid"] = info.st_uid
    resecured_lines: list[str] = []
    report_publication_outcome(resecured, project="syrd", print_func=resecured_lines.append)
    report["permissive_artifact_text"] = "\n".join(resecured_lines)

    print(json.dumps(report))
    return 0


def _hand_back_ownership() -> None:
    """Files left owned by another uid cannot be removed by the unprivileged
    process that made the directory, so a failure here would otherwise leave an
    undeletable tree behind every run."""
    for path in [Path("/src"), *Path("/src").rglob("*")]:
        try:
            os.chown(path, 0, 0, follow_symlinks=False)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        _hand_back_ownership()
