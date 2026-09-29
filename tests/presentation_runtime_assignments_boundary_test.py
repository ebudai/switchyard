#!/usr/bin/env python3
"""SYRD-494: live assignment ownership with synthetic board and pane evidence."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import presentation_controller as direct_controller  # noqa: E402
from scripts import presentation_controller as package_controller  # noqa: E402
from scripts import presentation_runtime_assignments as owner  # noqa: E402
from scripts import team_launcher  # noqa: E402


def project_config(root: Path):
    path = root / "sample.json"
    path.write_text(json.dumps({
        "project": "sample",
        "board_url": "http://127.0.0.1:1/",
        "role_state_isolation": True,
        "roles": [
            {"role": "director", "cli": "codex", "slot": 0, "workdir": str(root)},
        ],
    }), encoding="utf-8")
    config = team_launcher.load_project_config("sample", path)
    assert config.role_state_isolation
    return config


class Response:
    def __init__(self, payload: dict) -> None:
        self.data = json.dumps(payload).encode()

    def read(self, *_args) -> bytes:
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def assignment(*, target: str = "sample-director-r2:0.0", runtime: str = "codex") -> dict:
    return {
        "project": "sample", "authority_mode": "process",
        "assignments": {"director": {"actual_target": target, "runtime": runtime}},
    }


def refusal(call, expected: str) -> None:
    try:
        call()
    except SystemExit as exc:
        assert expected in str(exc), (expected, str(exc))
    else:
        raise AssertionError(f"expected refusal: {expected}")


def test_late_assignment_resolves_recovery_target_once() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd494-config.") as directory:
        config = project_config(Path(directory))
        answers = [
            {"project": "sample", "authority_mode": "process", "assignments": {}},
            assignment(),
        ]
        urls: list[str] = []
        notices: list[str] = []
        sleeps: list[float] = []
        def open_url(url: str) -> Response:
            urls.append(url)
            return Response(answers.pop(0))
        resolved = owner.runtime_assignment_config(
            config, opener=open_url, wait_seconds=2, poll_seconds=0.5,
            sleep=sleeps.append, monotonic=lambda: 0.0, print_func=notices.append,
        )
        assert resolved is not config
        assert resolved.roles[0].target == "sample-director-r2:0.0"
        assert resolved.roles[0].tmux_session == "sample-director-r2"
        assert config.roles[0].target != resolved.roles[0].target
        assert urls == ["http://127.0.0.1:1/api/runtime-assignments"] * 2
        assert sleeps == [0.5] and len(notices) == 1
        assert "director" in notices[0] and "2s" in notices[0]


def test_assignment_refusals_do_not_retry() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd494-config.") as directory:
        config = project_config(Path(directory))
        cases = (
            (dict(assignment(), project="foreign"), "belongs to another project"),
            (dict(assignment(), authority_mode="uid"), "legacy uid authority"),
            (assignment(target="foreign-director:0.0"), "refusing foreign runtime assignment"),
            (assignment(runtime="claude"), "differs from the launcher projection"),
        )
        for payload, expected in cases:
            calls: list[str] = []
            refusal(
                lambda: owner.runtime_assignment_config(
                    config,
                    opener=lambda url: (calls.append(url), Response(payload))[1],
                    wait_seconds=30,
                    sleep=lambda _seconds: (_ for _ in ()).throw(AssertionError("unexpected wait")),
                ), expected,
            )
            assert calls == ["http://127.0.0.1:1/api/runtime-assignments"]


def test_divergence_reads_only_declared_exact_target() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd494-config.") as directory:
        root = Path(directory)
        config = project_config(root)
        (root / "17").mkdir()
        (root / "17" / "cmdline").write_bytes(b"codex\0resume\0")
        calls: list[list[str]] = []
        urls: list[str] = []
        def open_url(url: str) -> Response:
            urls.append(url)
            if url.endswith("/api/runtime-assignments"):
                return Response({"project": "sample", "authority_mode": "process", "assignments": {}})
            assert url.endswith("/api/workflow")
            return Response({"document": {"roles": [{
                "name": "director", "runtime": "claude", "target": "sample-director-r2:0.0",
            }]}})
        class Probe:
            returncode = 0
            stdout = "17\n"
        def runner(args, **kwargs):
            calls.append(args)
            assert kwargs == {"capture_output": True, "text": True, "check": False}
            return Probe()
        message = owner.runtime_divergence_refusal(
            config, opener=open_url, runner=runner, proc_root=root,
        )
        assert "declared claude at sample-director-r2:0.0" in message
        assert "runs codex (pid 17)" in message and "Nothing was changed" in message
        assert calls == [["tmux", "display-message", "-p", "-t", "=sample-director-r2:0.0", "#{pane_pid}"]]
        assert urls == [
            "http://127.0.0.1:1/api/runtime-assignments",
            "http://127.0.0.1:1/api/workflow",
        ]


def test_both_controller_import_forms_reexport_owner_objects() -> None:
    for name in (
        "runtime_assignment_config", "_resolved_runtime_assignments",
        "runtime_divergence_refusal", "_exact_tmux_target",
    ):
        assert getattr(direct_controller, name) is getattr(owner, name)
        assert getattr(package_controller, name) is getattr(owner, name)
    assert owner._exact_tmux_target("sample-director:0.0") == "=sample-director:0.0"
    assert owner._exact_tmux_target("=sample-director:0.0") == "=sample-director:0.0"
    seen: list[list[str]] = []
    def runner(args, **_kwargs):
        seen.append(args)
        return object()
    package_controller._exact_tmux_runner(runner)(
        ["tmux", "has-session", "-t", "sample-director:0.0"]
    )
    assert seen == [["tmux", "has-session", "-t", "=sample-director:0.0"]]


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    print(f"presentation_runtime_assignments_boundary_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
