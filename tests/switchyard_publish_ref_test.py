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


def test_role_and_ref_boundary_is_unchanged() -> None:
    config = {"roles": [{"role": "app", "run_as_user": "syrd-app"}]}
    assert publisher.role_for_account(config, "syrd-app")["role"] == "app"
    refusal(lambda: publisher.role_for_account(config, "syrd-ops"), "not a configured role account")
    assert publisher.validate_ref("app/syrd-69", "app") == "app/syrd-69"
    refusal(lambda: publisher.validate_ref("main", "app"), "integration branch")
    refusal(lambda: publisher.validate_ref("ops/syrd-69", "app"), "only publish refs under app/")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print("switchyard_publish_ref_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
