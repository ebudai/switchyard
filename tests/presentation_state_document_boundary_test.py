#!/usr/bin/env python3
"""SYRD-493: the presentation document has one persisted-state owner."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

import presentation_controller as direct  # noqa: E402
from scripts import presentation_controller as controller  # noqa: E402
from scripts import presentation_state_document as document  # noqa: E402
from scripts import team_launcher  # noqa: E402
from team_launcher_presentation_test import _write_presentation_config  # noqa: E402


NAMES = (
    "PRESENTATION_SCHEMA", "PRESENTATION_HISTORY_LIMIT",
    "presentation_state_path", "_configured_presentation",
    "default_presentation_document", "_complete_mapping", "_validated_mapping",
    "validate_presentation_document", "_read_state", "_locked_state", "_write_state",
)


def _runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 0, "", "")


def test_lock_read_write_and_mapping_rollback() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd493-state.") as raw:
        root = Path(raw)
        config_path = _write_presentation_config(root)
        config = team_launcher.load_project_config("porter", config_path)
        state_path = root / "presentation.json"
        initial = document.default_presentation_document(config, config_path=config_path)
        with document._locked_state(state_path, config=config, runner=_runner):
            document._write_state(config, state_path, initial, runner=_runner)
        assert state_path.with_suffix(".json.lock").exists()
        assert document._read_state(state_path, config=config, config_path=config_path) == initial

        calls: list[dict[str, str | None]] = []
        previous = controller._apply_mapping

        def fail_once(_config: Any, mapping: Any, *, runner: Any) -> None:
            calls.append(dict(mapping))
            if len(calls) == 1:
                raise RuntimeError("synthetic apply failure")

        def swap(state: dict[str, Any]) -> None:
            state["slots"] = {"0": "app", "1": "director"}

        try:
            controller._apply_mapping = fail_once
            try:
                controller._mutate(
                    config, config_path=config_path, state_path=state_path,
                    actor="director", action="swap", detail={}, transform=swap,
                    runner=_runner,
                )
            except SystemExit as exc:
                assert "prior mapping restored" in str(exc), exc
            else:
                raise AssertionError("failed mapping was accepted")
            assert json.loads(state_path.read_text()) == initial
            assert calls == [{"0": "app", "1": "director"}, initial["slots"]]

            controller._apply_mapping = lambda _config, mapping, *, runner: calls.append(dict(mapping))
            saved = controller._mutate(
                config, config_path=config_path, state_path=state_path,
                actor="director", action="swap", detail={"source": "probe"},
                transform=swap, runner=_runner,
            )
        finally:
            controller._apply_mapping = previous
        assert saved == json.loads(state_path.read_text())
        assert saved["revision"] == 1 and saved["slots"] == {"0": "app", "1": "director"}
        assert saved["history"][0]["action"] == "swap"
        assert calls[-1] == saved["slots"]


def test_project_and_role_validation_still_refuses_invalid_documents() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd493-validation.") as raw:
        root = Path(raw)
        config_path = _write_presentation_config(root)
        config = team_launcher.load_project_config("porter", config_path)
        valid = document.default_presentation_document(config, config_path=config_path)
        for change, expected in (
            ({"project": "other"}, "does not match"),
            ({"slots": {"0": "app", "1": "app"}}, "more than one slot"),
        ):
            try:
                document.validate_presentation_document({**valid, **change}, config=config)
            except SystemExit as exc:
                assert expected in str(exc), exc
            else:
                raise AssertionError(f"invalid presentation document was accepted: {change}")


def test_public_aliases_and_source_boundary() -> None:
    for name in NAMES:
        assert getattr(controller, name) is getattr(document, name), name
        assert getattr(direct, name) is getattr(document, name), name
    source = (ROOT / "scripts/presentation_state_document.py").read_text()
    assert "presentation_controller" not in source
    owner = ast.parse(source)
    retained = ast.parse((ROOT / "scripts/presentation_controller.py").read_text())
    moved = {name for name in NAMES if name not in ("PRESENTATION_SCHEMA", "PRESENTATION_HISTORY_LIMIT")}
    owner_functions = {node.name for node in owner.body if isinstance(node, ast.FunctionDef)}
    retained_functions = {node.name for node in retained.body if isinstance(node, ast.FunctionDef)}
    assert owner_functions == moved
    assert not retained_functions & moved


def main() -> int:
    test_lock_read_write_and_mapping_rollback()
    test_project_and_role_validation_still_refuses_invalid_documents()
    test_public_aliases_and_source_boundary()
    print("presentation_state_document_boundary_test: 3 cases ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
