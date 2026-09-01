#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_design_project_command_slug_validation_rejects_dash_and_normalizes_uppercase() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-design-slug.") as tmp:
        tmp_path = Path(tmp)
        try:
            design_project_command(
                "bad-project",
                output_dir=tmp_path / "bad",
                design_title="Bad Project",
                design_body="bad",
                repository=tmp_path,
                remote="origin",
                default_branch="main",
                worktree_policy="shared",
                owner_user="bad-agent",
                ticket_prefix="BAD",
                push_policy="director-main-only",
                audit_signoff=True,
                needs_inspection=False,
                needs_user_signoff=False,
                board_service_traversal=True,
                supplementary_groups=(),
                linger=True,
                owner_shell="fish",
                print_func=lambda _line: None,
            )
            raise AssertionError("expected dashed design slug rejection")
        except SystemExit as exc:
            message = str(exc)

        lines: list[str] = []
        assert (
            design_project_command(
                "OTTO",
                output_dir=tmp_path / "good",
                design_title="Otto Design",
                design_body="ok",
                repository=tmp_path,
                remote="origin",
                default_branch="main",
                worktree_policy="shared",
                owner_user="otto-agent",
                ticket_prefix="OTTO",
                push_policy="director-main-only",
                audit_signoff=True,
                needs_inspection=False,
                needs_user_signoff=False,
                board_service_traversal=True,
                supplementary_groups=(),
                linger=True,
                owner_shell="fish",
                print_func=lines.append,
            )
            == 0
        )
        artifact = load_project_design_artifact(tmp_path / "good" / "otto.project.json", expected_project="otto")

    assert "project slug must match ^[a-z0-9][a-z0-9_]{0,39}$" in message
    assert artifact.project == "otto"
    assert lines and "otto.project.json" in lines[-1]

def test_project_design_artifact_slug_validation_rejects_dash_and_normalizes_uppercase() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-artifact-slug.") as tmp:
        tmp_path = Path(tmp)
        dashed = tmp_path / "dashed.project.json"
        uppercase = tmp_path / "uppercase.project.json"

        def write_artifact(path: Path, slug: str) -> None:
            path.write_text(
                json.dumps(
                    {
                        "schema": "switchyard.project.v1",
                        "design_document": str(tmp_path / "PROJECT_DESIGN.md"),
                        "project": {
                            "slug": slug,
                            "name": "Otto Scheduler",
                            "ticket_prefix": "OTTO",
                            "owner_user": "otto-agent",
                            "repository": str(tmp_path),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

        write_artifact(dashed, "otto-scheduler")
        write_artifact(uppercase, "OTTO")

        try:
            load_project_design_artifact(dashed)
            raise AssertionError("expected dashed artifact slug rejection")
        except SystemExit as exc:
            message = str(exc)
        artifact = load_project_design_artifact(uppercase, expected_project="otto")

    assert "project slug must match ^[a-z0-9][a-z0-9_]{0,39}$" in message
    assert artifact.project == "otto"

def test_project_entry_readers_skip_invalid_slugs_with_warning() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-entry-slug.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "configs"
        registry_dir = tmp_path / "registry"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        registry_dir.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = config_dir / "bad-project.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "bad-project",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path)}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        registry_path = registry_dir / "bad-project.json"
        registry_path.write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "bad-project",
                    "name": "Bad Project",
                    "config_path": str(config_path),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        stderr = StringIO()

        with redirect_stderr(stderr):
            config_entries = team_launcher._project_entries(config_dir)
            registry_entries = team_launcher._registry_project_entries(registry_dir)

    warning = stderr.getvalue()
    assert config_entries == []
    assert registry_entries == []
    assert f"skipping {config_path}" in warning
    assert f"skipping {registry_path}" in warning
    assert "project slug must match ^[a-z0-9][a-z0-9_]{0,39}$" in warning

def test_slug_from_project_name_normalizes_unicode_and_caps_length() -> None:
    assert team_launcher._slug_from_project_name("Otto Scheduler") == "otto_scheduler"
    assert team_launcher._slug_from_project_name("Ötto Scheduler") == "otto_scheduler"
    assert team_launcher._slug_from_project_name("日本 Project") == "project"
    long_slug = team_launcher._slug_from_project_name("A" * 60)
    assert long_slug == "a" * 40
    assert team_launcher.PROJECT_SLUG_RE.fullmatch(long_slug)

def test_switchyard_registry_registers_pointer_and_survives_checkout_clean() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-registry.") as tmp:
        tmp_path = Path(tmp)
        checkout_config_dir = tmp_path / "checkout" / "config" / "team-launcher"
        registry_dir = tmp_path / "etc" / "switchyard" / "projects"
        project_config_dir = tmp_path / "otto" / ".switchyard" / "provision"
        layout = tmp_path / "layout.json"
        checkout_config_dir.mkdir(parents=True)
        project_config_dir.mkdir(parents=True)
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        pgu_config = checkout_config_dir / "pgu.json"
        pgu_config.write_text(
            json.dumps(
                {
                    "project": "pgu",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path / "pgu")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        otto_config = project_config_dir / "otto.json"
        otto_config.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path / "otto")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        lines: list[str] = []

        assert switchyard_register_command(otto_config, registry_dir=registry_dir, print_func=lines.append) == 0
        pointer = json.loads((registry_dir / "otto.json").read_text(encoding="utf-8"))
        assert pointer == {
            "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
            "slug": "otto",
            "name": "Otto System",
            "config_path": str(otto_config.resolve(strict=False)),
        }
        assert (registry_dir / "otto.json").stat().st_mode & 0o777 == 0o644
        assert team_launcher._resolve_switchyard_project("otto", config_dir=checkout_config_dir, registry_dir=registry_dir).config_path == otto_config

        _run_git(["git", "init", "-b", "main"], cwd=tmp_path / "checkout")
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=tmp_path / "checkout")
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=tmp_path / "checkout")
        _run_git(["git", "add", "config/team-launcher/pgu.json"], cwd=tmp_path / "checkout")
        _run_git(["git", "commit", "-m", "tracked pgu config"], cwd=tmp_path / "checkout")
        (checkout_config_dir / "stray.json").write_text("{}\n", encoding="utf-8")
        _run_git(["git", "clean", "-fdx"], cwd=tmp_path / "checkout")

        menu_lines: list[str] = []
        assert switchyard_menu_command(config_dir=checkout_config_dir, registry_dir=registry_dir, print_func=menu_lines.append) == 0
        assert menu_lines == ["new...", "Otto System (otto)", "pgu"]
        assert (registry_dir / "otto.json").is_file()
        assert team_launcher._resolve_switchyard_project("pgu", config_dir=checkout_config_dir, registry_dir=registry_dir).config_path == pgu_config

    assert lines and "switchyard: registered" in lines[0]

def test_switchyard_register_skips_the_config_file_being_registered() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-register-self.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "configs"
        registry_dir = tmp_path / "registry"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = config_dir / "pgu.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "pgu",
                    "project_name": "pgu",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path / "pgu")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        assert switchyard_register_command(config_path, config_dir=config_dir, registry_dir=registry_dir, print_func=lambda _line: None) == 0
        pointer = json.loads((registry_dir / "pgu.json").read_text(encoding="utf-8"))

    assert pointer["slug"] == "pgu"
    assert pointer["config_path"] == str(config_path.resolve(strict=False))

def test_switchyard_register_rejects_dashed_slug() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-register.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        config_path = tmp_path / "bad-project.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "project": "bad-project",
                    "project_name": "Bad Project",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path)}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            switchyard_register_command(config_path, registry_dir=tmp_path / "registry", print_func=lambda _line: None)
            raise AssertionError("expected dashed slug rejection")
        except SystemExit as exc:
            message = str(exc)

    assert "project slug must match" in message
    assert "^[a-z0-9][a-z0-9_]{0,39}$" in message

def test_switchyard_register_rejects_duplicate_slug_without_overwriting_pointer() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-register.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        config_dir = tmp_path / "configs"
        layout = tmp_path / "layout.json"
        registry_dir.mkdir()
        config_dir.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        existing_config = config_dir / "existing.json"
        existing_config.write_text("{}\n", encoding="utf-8")
        existing_payload = {
            "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
            "slug": "otto",
            "name": "Otto Existing",
            "config_path": str(existing_config),
        }
        pointer = registry_dir / "otto.json"
        pointer.write_text(json.dumps(existing_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        before = pointer.read_bytes()
        duplicate_config = config_dir / "duplicate.json"
        duplicate_config.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Different Otto",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path / "other")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            switchyard_register_command(duplicate_config, registry_dir=registry_dir, print_func=lambda _line: None)
            raise AssertionError("expected duplicate slug rejection")
        except SystemExit as exc:
            message = str(exc)

        after = pointer.read_bytes()

    assert "project slug 'otto' is already registered to 'Otto Existing'" in message
    assert str(existing_config) in message
    assert after == before

def test_switchyard_register_refuses_existing_unparseable_registry_pointer() -> None:
    cases = (
        ("wrong-schema", '{"schema":"switchyard.registry.v0","slug":"otto","name":"Old","config_path":"/old"}\n'),
        ("corrupt-json", "{not json\n"),
    )
    for label, existing_bytes in cases:
        with tempfile.TemporaryDirectory(prefix=f"pgu-switchyard-register-{label}.") as tmp:
            tmp_path = Path(tmp)
            registry_dir = tmp_path / "registry"
            config_dir = tmp_path / "configs"
            layout = tmp_path / "layout.json"
            registry_dir.mkdir()
            config_dir.mkdir()
            layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
            pointer = registry_dir / "otto.json"
            pointer.write_text(existing_bytes, encoding="utf-8")
            before = pointer.read_bytes()
            config_path = config_dir / "otto.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": "otto",
                        "project_name": "Otto New",
                        "layout": str(layout),
                        "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path / "otto")}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            try:
                switchyard_register_command(config_path, config_dir=config_dir, registry_dir=registry_dir, print_func=lambda _line: None)
                raise AssertionError("expected existing pointer refusal")
            except SystemExit as exc:
                message = str(exc)

            assert f"registry entry {pointer} already exists; refusing to overwrite" in message
            assert pointer.read_bytes() == before

def test_switchyard_register_rejects_duplicate_project_name_without_writing_pointer() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-register.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        config_dir = tmp_path / "configs"
        layout = tmp_path / "layout.json"
        registry_dir.mkdir()
        config_dir.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        existing_config = config_dir / "otto.json"
        existing_config.write_text("{}\n", encoding="utf-8")
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto Scheduler",
                    "config_path": str(existing_config),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        duplicate_config = config_dir / "atlas.json"
        duplicate_config.write_text(
            json.dumps(
                {
                    "project": "atlas",
                    "project_name": "Otto Scheduler",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path / "atlas")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            switchyard_register_command(duplicate_config, registry_dir=registry_dir, print_func=lambda _line: None)
            raise AssertionError("expected duplicate project name rejection")
        except SystemExit as exc:
            message = str(exc)
        duplicate_pointer_exists = (registry_dir / "atlas.json").exists()

    assert "project name 'Otto Scheduler' is already registered as 'otto'" in message
    assert str(existing_config) in message
    assert not duplicate_pointer_exists

def test_switchyard_new_rejects_config_dir_project_slug_before_mutating() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "configs"
        registry_dir = tmp_path / "registry"
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        registry_dir.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        (config_dir / "pgu.json").write_text(
            json.dumps(
                {
                    "project": "pgu",
                    "project_name": "pgu",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path / "pgu")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[list[str]] = []
        output: list[str] = []

        try:
            switchyard_new_command(
                slug="pgu",
                agent_name="pgu2",
                project_name="PGU Two",
                output_dir=output_dir,
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=lambda args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
                config_dir=config_dir,
                registry_dir=registry_dir,
                input_func=lambda _prompt: "",
                print_func=output.append,
            )
            raise AssertionError("expected duplicate config-dir slug rejection")
        except SystemExit as exc:
            message = str(exc)

    assert "project slug 'pgu' is already registered to 'pgu'" in message
    assert str(config_dir / "pgu.json") in message
    assert calls == []
    assert output == []
    assert not output_dir.exists()
    assert not (home_base / "pgu2-agent").exists()

def test_switchyard_new_rejects_duplicate_slug_before_mutating() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        registry_dir.mkdir()
        existing_config = tmp_path / "otto.json"
        existing_config.write_text("{}\n", encoding="utf-8")
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto Scheduler",
                    "config_path": str(existing_config),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[list[str]] = []

        try:
            switchyard_new_command(
                slug="otto",
                agent_name="atlas",
                project_name="Atlas",
                output_dir=output_dir,
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=lambda args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
                registry_dir=registry_dir,
                input_func=lambda _prompt: "",
            )
            raise AssertionError("expected duplicate slug rejection")
        except SystemExit as exc:
            message = str(exc)

    assert "project slug 'otto' is already registered to 'Otto Scheduler'" in message
    assert str(existing_config) in message
    assert calls == []
    assert not output_dir.exists()
    assert not (home_base / "atlas-agent").exists()

def test_switchyard_new_rejects_duplicate_project_name_before_mutating() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        registry_dir.mkdir()
        existing_config = tmp_path / "otto.json"
        existing_config.write_text("{}\n", encoding="utf-8")
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto Scheduler",
                    "config_path": str(existing_config),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[list[str]] = []

        try:
            switchyard_new_command(
                slug="atlas",
                agent_name="atlas",
                project_name="Otto Scheduler",
                output_dir=output_dir,
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=lambda args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
                registry_dir=registry_dir,
                input_func=lambda _prompt: "",
            )
            raise AssertionError("expected duplicate project name rejection")
        except SystemExit as exc:
            message = str(exc)

    assert "project name 'Otto Scheduler' is already registered as 'otto'" in message
    assert str(existing_config) in message
    assert calls == []
    assert not output_dir.exists()
    assert not (home_base / "atlas-agent").exists()

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_registry_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
