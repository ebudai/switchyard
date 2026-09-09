#!/usr/bin/env python3
"""Focused security regressions for the narrow owner-side ref publisher."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "switchyard-publish-ref"


def load_module():
    loader = importlib.machinery.SourceFileLoader("switchyard_publish_ref", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


publisher = load_module()


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fixture(root: Path, *, project: str = "syrd", checkout: str = "switchyard") -> tuple[Path, Path, Path]:
    owner_home = root / "owner"
    repository = owner_home / "Projects" / checkout
    config = repository / ".switchyard" / "provision" / f"{project}.json"
    registry = root / "etc" / "switchyard" / "projects"
    repository.mkdir(parents=True, exist_ok=True)
    write_json(
        config,
        {
            "project": project,
            "repository": str(repository),
            "worktree_remote": "origin",
            "roles": [{"role": "app", "run_as_user": "syrd-app"}],
        },
    )
    write_json(
        registry / f"{project}.json",
        {
            "schema": publisher.PROJECT_REGISTRY_SCHEMA,
            "slug": project,
            "name": "Switchyard",
            "config_path": str(config),
        },
    )
    return owner_home, registry, config


def load_registered(owner_home: Path, registry: Path, project: str = "syrd") -> dict:
    return publisher.load_project(
        project,
        owner_home=owner_home,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
        registry_root=registry,
        registry_uid=os.getuid(),
    )


def refusal(call, expected: str) -> None:
    try:
        call()
    except SystemExit as exc:
        assert expected in str(exc), exc
    else:
        raise AssertionError(f"expected refusal containing {expected!r}")


def test_registered_checkout_name_is_independent_from_project_slug() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-slug.") as tmp:
        owner_home, registry, config = fixture(Path(tmp), project="syrd", checkout="switchyard")
        loaded = load_registered(owner_home, registry)

    assert loaded["project"] == "syrd"
    assert loaded["repository"] == str(config.parents[2].resolve())
    assert publisher.role_for_account(loaded, "syrd-app")["role"] == "app"


def test_matching_checkout_name_remains_compatible() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-matching.") as tmp:
        owner_home, registry, config = fixture(Path(tmp), project="porter", checkout="porter")
        loaded = load_registered(owner_home, registry, "porter")

    assert loaded["project"] == "porter"
    assert loaded["repository"] == str(config.parents[2].resolve())


def test_registry_entry_must_be_trusted_and_match_the_requested_project() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-registry.") as tmp:
        root = Path(tmp)
        owner_home, registry, _config = fixture(root)
        entry = registry / "syrd.json"

        entry.chmod(0o664)
        refusal(lambda: load_registered(owner_home, registry), "writable by an untrusted account")
        entry.chmod(0o644)

        replacement = registry / "replacement.json"
        replacement.write_bytes(entry.read_bytes())
        entry.unlink()
        entry.symlink_to(replacement)
        refusal(lambda: load_registered(owner_home, registry), "regular file, not a link")
        entry.unlink()

        owner_home, registry, _config = fixture(root)
        document = json.loads(entry.read_text(encoding="utf-8"))
        document["slug"] = "other"
        write_json(entry, document)
        refusal(lambda: load_registered(owner_home, registry), "is not for project syrd")


def test_registered_config_rejects_escape_symlink_and_untrusted_write_modes() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-config.") as tmp:
        root = Path(tmp)
        owner_home, registry, config = fixture(root)
        entry = registry / "syrd.json"
        outside = root / "role-controlled.json"
        outside.write_bytes(config.read_bytes())

        pointer = json.loads(entry.read_text(encoding="utf-8"))
        pointer["config_path"] = "relative.json"
        write_json(entry, pointer)
        refusal(lambda: load_registered(owner_home, registry), "must be absolute")

        pointer["config_path"] = str(outside)
        write_json(entry, pointer)
        refusal(lambda: load_registered(owner_home, registry), "escapes")

        pointer["config_path"] = str(config.with_name("linked.json"))
        write_json(entry, pointer)
        config.with_name("linked.json").symlink_to(config)
        refusal(lambda: load_registered(owner_home, registry), "real regular file, not a link")

        linked_checkout = owner_home / "Projects" / "linked-checkout"
        linked_checkout.symlink_to(config.parents[2], target_is_directory=True)
        pointer["config_path"] = str(
            linked_checkout / ".switchyard" / "provision" / "syrd.json"
        )
        write_json(entry, pointer)
        refusal(lambda: load_registered(owner_home, registry), "not a real directory")

        pointer["config_path"] = str(config)
        write_json(entry, pointer)
        config.chmod(0o666)
        refusal(lambda: load_registered(owner_home, registry), "writable by an untrusted account")


def test_project_identity_and_repository_containment_remain_authoritative() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-identity.") as tmp:
        root = Path(tmp)
        owner_home, registry, config = fixture(root)
        document = json.loads(config.read_text(encoding="utf-8"))

        document["project"] = "other"
        write_json(config, document)
        refusal(lambda: load_registered(owner_home, registry), "not the configuration for project syrd")

        document["project"] = "syrd"
        outside_repository = root / "role-repository"
        outside_repository.mkdir()
        document["repository"] = str(outside_repository)
        write_json(config, document)
        refusal(lambda: load_registered(owner_home, registry), "escapes")


def test_missing_and_stale_registrations_fail_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-stale.") as tmp:
        root = Path(tmp)
        owner_home = root / "owner"
        registry = root / "registry"
        (owner_home / "Projects").mkdir(parents=True)
        registry.mkdir()
        refusal(lambda: load_registered(owner_home, registry), "no registered configuration")

        owner_home, registry, config = fixture(root)
        config.unlink()
        refusal(lambda: load_registered(owner_home, registry), "is stale")


def test_project_slug_is_validated_before_it_builds_any_path() -> None:
    """The slug reaches a filesystem path, so it is checked before it does.

    The sudoers grant allows any arguments; --project is the first thing a role
    controls, and the registry lookup concatenates it into a filename.
    """
    assert publisher.validate_project(" syrd ") == "syrd"
    for hostile in ("../../etc/switchyard", "syrd/../other", "SYRD", "", "-syrd", "syrd.json"):
        refusal(lambda value=hostile: publisher.validate_project(value), "is not a valid project name")


def test_registry_root_itself_must_be_trusted() -> None:
    """A registry an untrusted account can replace is not a name authority.

    Every later check trusts the entry this directory hands back, so the
    directory is checked first: a link, a foreign owner, or a writable bit is
    refused before any entry is read.
    """
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-root.") as tmp:
        root = Path(tmp)
        owner_home, registry, _config = fixture(root)

        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        linked_registry = root / "linked-registry"
        linked_registry.symlink_to(registry, target_is_directory=True)
        refusal(
            lambda: load_registered(owner_home, linked_registry),
            "must be a real directory",
        )

        not_a_directory = root / "registry-file"
        not_a_directory.write_text("{}", encoding="utf-8")
        refusal(lambda: load_registered(owner_home, not_a_directory), "must be a real directory")

        missing = root / "absent-registry"
        refusal(lambda: load_registered(owner_home, missing), "cannot inspect project registry")

        # Owned by somebody else: proved by telling the checker which uid it
        # should have found, rather than by needing a second account to chown to.
        refusal(
            lambda: publisher.load_project(
                "syrd",
                owner_home=owner_home,
                owner_uid=os.getuid(),
                owner_gid=os.getgid(),
                registry_root=registry,
                registry_uid=os.getuid() + 1,
            ),
            "is not owned by the trusted account",
        )

        for mode in (0o775, 0o777):
            registry.chmod(mode)
            refusal(
                lambda: load_registered(owner_home, registry),
                "writable by an untrusted account",
            )
        registry.chmod(0o755)


def test_registry_entry_shape_is_validated() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-shape.") as tmp:
        root = Path(tmp)
        owner_home, registry, config = fixture(root)
        entry = registry / "syrd.json"
        pointer = json.loads(entry.read_text(encoding="utf-8"))

        broken = dict(pointer, schema="switchyard.project-registry.v0")
        write_json(entry, broken)
        refusal(lambda: load_registered(owner_home, registry), "unsupported schema")

        for empty in ({k: v for k, v in pointer.items() if k != "config_path"},
                      dict(pointer, config_path=""),
                      dict(pointer, config_path="   "),
                      dict(pointer, config_path=17)):
            write_json(entry, empty)
            refusal(lambda: load_registered(owner_home, registry), "has no config_path")

        write_json(entry, pointer)
        assert load_registered(owner_home, registry)["project"] == "syrd"


def test_registered_config_ownership_is_enforced_along_the_whole_path() -> None:
    """Containment is not enough: the path has to belong to the owner.

    A location inside Projects that some other account controls is exactly what
    a role would arrange if it could. Note where the refusal lands: every
    directory component is checked, so a foreign owner is caught while walking
    the path, before the file itself is ever read. Pinning the component message
    keeps that ordering from quietly regressing into a leaf-only check.
    """
    def load_as(*, owner_uid: int, owner_gid: int):
        owner_home, registry, _config = fixture(Path(tmp))
        return publisher.load_project(
            "syrd",
            owner_home=owner_home,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            registry_root=registry,
            registry_uid=os.getuid(),
        )

    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-owner.") as tmp:
        assert load_as(owner_uid=os.getuid(), owner_gid=os.getgid())["project"] == "syrd"
        refusal(
            lambda: load_as(owner_uid=os.getuid() + 1, owner_gid=os.getgid()),
            "has an untrusted owner",
        )


def test_bundle_must_be_a_role_owned_regular_file() -> None:
    """The role hands over a bundle; the owner must not read one it did not.

    A link here would let a role point the owner's git at a file it cannot
    otherwise read, so the check is on the link itself, not its destination.
    """
    import pwd

    me = pwd.getpwuid(os.getuid()).pw_name
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-bundle.") as tmp:
        root = Path(tmp)
        bundle = root / "work.bundle"
        bundle.write_bytes(b"not really a bundle, but a real file")
        publisher.require_role_owned_file(bundle, me)

        link = root / "linked.bundle"
        link.symlink_to(bundle)
        refusal(lambda: publisher.require_role_owned_file(link, me), "must be a regular file, not a link")

        directory = root / "directory.bundle"
        directory.mkdir()
        refusal(lambda: publisher.require_role_owned_file(directory, me), "must be a regular file, not a link")

        refusal(lambda: publisher.require_role_owned_file(root / "absent.bundle", me), "cannot read bundle")

        # root always exists and never owns this file.
        refusal(lambda: publisher.require_role_owned_file(bundle, "root"), "is not owned by root")
        refusal(
            lambda: publisher.require_role_owned_file(bundle, "switchyard-no-such-account"),
            "is not a local account",
        )


def test_remote_name_resolves_from_the_owner_checkout() -> None:
    """The URL comes from the owner's repository, never the role's.

    A role controls its own checkout's git configuration, so a remote NAME is
    resolved in the owner's repository and anything already a URL is left alone.
    """
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-remote.") as tmp:
        root = Path(tmp)
        repository = root / "checkout"
        repository.mkdir()
        assert publisher.git(["-C", str(repository), "init", "-q"]).returncode == 0
        expected = "https://example.invalid/switchyard.git"
        assert publisher.git(
            ["-C", str(repository), "remote", "add", "origin", expected]
        ).returncode == 0

        config = {"repository": str(repository)}
        assert publisher.resolve_remote_url(config, "origin") == expected
        refusal(lambda: publisher.resolve_remote_url(config, "upstream"), "has no remote named upstream")

        # Already a URL or an scp-style location: returned untouched, and the
        # owner checkout is not consulted at all.
        for literal in (expected, "git@github.com:ebudai/switchyard.git", "/srv/git/switchyard.git"):
            assert publisher.resolve_remote_url({}, literal) == literal

        refusal(
            lambda: publisher.resolve_remote_url({}, "origin"),
            "no owner repository",
        )


def test_role_and_ref_boundary_is_unchanged() -> None:
    config = {"roles": [{"role": "app", "run_as_user": "syrd-app"}]}
    assert publisher.role_for_account(config, "syrd-app")["role"] == "app"
    refusal(lambda: publisher.role_for_account(config, "syrd-ops"), "not a configured role account")
    assert publisher.validate_ref("app/syrd-69", "app") == "app/syrd-69"
    refusal(lambda: publisher.validate_ref("ops/syrd-69", "app"), "only publish refs under app/")
    for protected in ("main", "master", "trunk", "release", "head", "MAIN", "Main"):
        refusal(lambda value=protected: publisher.validate_ref(value, value), "integration branch")
    refusal(lambda: publisher.validate_ref("app", "app"), "below its namespace")
    refusal(lambda: publisher.validate_ref("refs/heads/app/x", "app"), "not a full ref path")
    refusal(lambda: publisher.validate_ref("app/x.lock", "app"), "not a valid branch name")
    refusal(lambda: publisher.validate_ref("app/../ops/x", "app"), "not a valid branch name")
    refusal(lambda: publisher.validate_ref("", "app"), "a ref name is required")


# --- SYRD-75: publication is not finished until the board can verify it -------


def unit_dir(root: Path, project: str = "syrd", cache: Path | None = None) -> Path:
    """A board unit like the provisioned one, naming the tenant's cache."""
    directory = root / "systemd"
    directory.mkdir(parents=True, exist_ok=True)
    body = ["[Service]", "Environment=TICKET_BOARD_PROJECT=" + project]
    if cache is not None:
        body.append(f"Environment={publisher.COMMIT_CACHE_ENVIRONMENT_KEY}={cache}")
    (directory / f"{project}-ticket-board.service").write_text("\n".join(body) + "\n", encoding="utf-8")
    return directory


def bare_repo(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert publisher.git(["init", "--bare", "-q", str(path)]).returncode == 0
    return path


def seeded_origin(root: Path, ref: str, content: str = "one") -> tuple[Path, str]:
    """A real remote holding refs/heads/<ref>, and the commit it points at.

    `content` distinguishes otherwise identical fixtures: same tree, same author
    and the same second produce the same hash, which would hide the very thing a
    concurrency test is trying to show.
    """
    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    assert publisher.git(["init", "-q", "-b", "trunkless", str(work)]).returncode == 0
    for name, value in (("user.email", "t@example.invalid"), ("user.name", "T")):
        assert publisher.git(["-C", str(work), "config", name, value]).returncode == 0
    (work / "file.txt").write_text(content + "\n", encoding="utf-8")
    assert publisher.git(["-C", str(work), "add", "file.txt"]).returncode == 0
    assert publisher.git(["-C", str(work), "commit", "-qm", "one"]).returncode == 0
    origin = bare_repo(root / "origin.git")
    assert publisher.git(
        ["-C", str(work), "push", "-q", str(origin), f"HEAD:refs/heads/{ref}"]
    ).returncode == 0
    commit = publisher.git(["-C", str(work), "rev-parse", "HEAD"]).stdout.strip()
    return origin, commit


def test_the_configured_cache_is_read_from_the_board_unit_not_the_slug() -> None:
    """The tenant's cache is not derivable from its name.

    Deriving it is how SYRD-73 happened; here the same mistake would refresh a
    cache the board does not read and report the work ready anyway.
    """
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-cache.") as tmp:
        root = Path(tmp)
        cache = bare_repo(root / "syrd-source-cache.git")
        units = unit_dir(root, cache=cache)
        assert publisher.configured_commit_cache("syrd", unit_dir=units) == cache

        # A unit that names no cache is a configuration gap, not a licence to guess.
        bare_units = unit_dir(root / "bare", cache=None)
        refusal(
            lambda: publisher.configured_commit_cache("syrd", unit_dir=bare_units),
            "names no TICKET_BOARD_COMMIT_GIT_DIR",
        )
        refusal(
            lambda: publisher.configured_commit_cache("absent", unit_dir=units),
            "cannot read board unit",
        )


def test_the_board_unit_and_the_cache_must_both_be_trustworthy() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-cachetrust.") as tmp:
        root = Path(tmp)
        cache = bare_repo(root / "cache.git")
        units = unit_dir(root, cache=cache)
        unit = units / "syrd-ticket-board.service"

        unit.chmod(0o664)
        refusal(
            lambda: publisher.configured_commit_cache("syrd", unit_dir=units),
            "writable by an untrusted account",
        )
        unit.chmod(0o644)

        body = unit.read_text(encoding="utf-8")
        unit.unlink()
        replacement = units / "replacement.service"
        replacement.write_text(body, encoding="utf-8")
        unit.symlink_to(replacement)
        refusal(
            lambda: publisher.configured_commit_cache("syrd", unit_dir=units),
            "must be a regular file, not a link",
        )
        unit.unlink()
        unit.write_text(body, encoding="utf-8")

        # A relative or missing cache, a link, or one this account does not own.
        relative = unit_dir(root / "relative", cache=Path("cache.git"))
        refusal(
            lambda: publisher.configured_commit_cache("syrd", unit_dir=relative),
            "is not an absolute path",
        )
        linked = root / "linked-cache.git"
        linked.symlink_to(cache, target_is_directory=True)
        linked_units = unit_dir(root / "linked", cache=linked)
        refusal(
            lambda: publisher.configured_commit_cache("syrd", unit_dir=linked_units),
            "must be a real directory, not a link",
        )
        # The unit is the only thing saying which cache to refresh, so an
        # untrusted owner on it means a role could choose that cache. Proved the
        # same way as the registry root: by naming the uid it should have found.
        refusal(
            lambda: publisher.configured_commit_cache("syrd", unit_dir=units, unit_uid=os.getuid() + 1),
            "board unit",
        )
        refusal(
            lambda: publisher.configured_commit_cache("syrd", unit_dir=units, unit_uid=os.getuid() + 1),
            "is not owned by the trusted account",
        )
        refusal(
            lambda: publisher.configured_commit_cache("syrd", unit_dir=units, owner_uid=os.getuid() + 1),
            "is not owned by this project account",
        )
        cache.chmod(0o775)
        refusal(
            lambda: publisher.configured_commit_cache("syrd", unit_dir=units),
            "writable by an untrusted account",
        )
        cache.chmod(0o755)

        # The last assignment wins, as systemd reads it.
        elsewhere = bare_repo(root / "second.git")
        unit.write_text(
            "[Service]\n"
            f"Environment={publisher.COMMIT_CACHE_ENVIRONMENT_KEY}={cache}\n"
            f"Environment={publisher.COMMIT_CACHE_ENVIRONMENT_KEY}={elsewhere}\n",
            encoding="utf-8",
        )
        assert publisher.configured_commit_cache("syrd", unit_dir=units) == elsewhere


def test_a_successful_refresh_leaves_the_exact_commit_in_the_cache() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-refresh.") as tmp:
        root = Path(tmp)
        origin, commit = seeded_origin(root, "app/syrd-75")
        cache = bare_repo(root / "cache.git")

        assert publisher.refresh_commit_cache(cache, str(origin), "app/syrd-75", commit) is None
        resolved = publisher.git(
            ["--git-dir", str(cache), "rev-parse", "refs/remotes/origin/app/syrd-75^{commit}"]
        )
        assert resolved.stdout.strip() == commit, resolved.stdout

        # Idempotent: publishing the same ref again is safe, which is what the
        # failure message tells the operator to do.
        assert publisher.refresh_commit_cache(cache, str(origin), "app/syrd-75", commit) is None


def test_an_unreachable_remote_is_reported_and_retried() -> None:
    calls: list[float] = []
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-unreachable.") as tmp:
        root = Path(tmp)
        cache = bare_repo(root / "cache.git")
        missing = root / "not-a-repo.git"
        problem = publisher.refresh_commit_cache(
            cache, str(missing), "app/syrd-75", "0" * 40, sleep=calls.append
        )
    assert problem is not None
    assert "could not refresh the trusted commit cache" in problem, problem
    assert len(calls) == publisher.CACHE_FETCH_ATTEMPTS - 1, calls


def test_a_ref_that_moved_after_the_push_is_refused_not_cached_over() -> None:
    """The replacement race. The cache must not answer for another commit.

    Submitting against a cache that quietly holds somebody else's commit is
    worse than failing: the hash resolves and sends a reviewer into the wrong
    diff.
    """
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-race.") as tmp:
        root = Path(tmp)
        origin, commit = seeded_origin(root, "app/syrd-75")
        cache = bare_repo(root / "cache.git")
        other = "1" * 40
        problem = publisher.refresh_commit_cache(cache, str(origin), "app/syrd-75", other)
    assert problem is not None
    assert f"not {other}" in problem, problem
    assert "changed after this publication" in problem, problem


def test_concurrent_role_publications_do_not_move_each_others_refs() -> None:
    """Two roles, one cache. Each fetch names only its own ref."""
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-concurrent.") as tmp:
        root = Path(tmp)
        cache = bare_repo(root / "cache.git")
        first_origin, first_commit = seeded_origin(root / "a", "app/one", content="first")
        second_origin, second_commit = seeded_origin(root / "b", "ops/two", content="second")
        assert first_commit != second_commit

        assert publisher.refresh_commit_cache(cache, str(first_origin), "app/one", first_commit) is None
        assert publisher.refresh_commit_cache(cache, str(second_origin), "ops/two", second_commit) is None
        for ref, expected in (("app/one", first_commit), ("ops/two", second_commit)):
            resolved = publisher.git(
                ["--git-dir", str(cache), "rev-parse", f"refs/remotes/origin/{ref}^{{commit}}"]
            )
            assert resolved.stdout.strip() == expected, (ref, resolved.stdout)


def test_the_public_ref_is_read_back_by_name() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-publish-ref-public.") as tmp:
        root = Path(tmp)
        origin, commit = seeded_origin(root, "app/syrd-75")
        assert publisher.remote_ref_commit(str(origin), "app/syrd-75") == commit
        # Present remote, absent ref: an empty answer, not a crash and not a match.
        assert publisher.remote_ref_commit(str(origin), "app/never-pushed") == ""
        # Unreachable remote is unknown, which the caller treats as not ready.
        assert publisher.remote_ref_commit(str(root / "absent.git"), "app/syrd-75") is None


def test_a_publication_that_cannot_be_verified_is_not_reported_as_ready() -> None:
    """The failure the ticket is about, in the words the operator will read."""
    import io
    import contextlib

    stream = io.StringIO()
    with contextlib.redirect_stderr(stream):
        status = publisher.not_workflow_ready("app/syrd-75", "a" * 40, "the cache is unreachable")
    printed = stream.getvalue()
    assert status == 1, status
    assert "was pushed at " + "a" * 40 in printed, printed
    assert "NOT ready to submit" in printed, printed
    assert "publishing the same ref" in printed, printed


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print("switchyard_publish_ref_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
