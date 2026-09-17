#!/usr/bin/env python3
"""The privileged boundary must not depend on nobody else holding a descriptor.

The live UAT on 2026-09-17 ran `switchyard stop testing` and produced no rollout
record and no diagnostic at all -- the mode that means the recorder was never
reached. The bridge read the handoff pipe to EOF before reaping the child, and
HANDOFF_CHILD_FD is deliberately inheritable, so a process the owner half left
behind kept the write end open, EOF never came, and the read blocked forever:
no `waitpid`, no boundary record, and no result back to the caller -- so the
desktop half that closes the window never ran either.

One defect, three symptoms. These pin the drain, not the symptoms.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from standalone_test_runner import run_module_tests


def _bridge():
    """The bridge module, which has no .py suffix."""
    loader = importlib.machinery.SourceFileLoader(
        "switchyard_tenant_control", str(ROOT / "scripts" / "switchyard-tenant-control")
    )
    spec = importlib.util.spec_from_loader("switchyard_tenant_control", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


BRIDGE = _bridge()


def _with_deadline(call, seconds=5.0):
    """Run `call`, failing loudly if it blocks rather than hanging the suite.

    A test for "this used to block forever" must not itself block forever when
    it regresses: the whole point is to turn a hang into a failure.
    """
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # pragma: no cover - surfaced below
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), (
        f"the drain blocked for more than {seconds}s -- this is the SYRD-202 hang: "
        "it is waiting for EOF on a descriptor somebody else is holding"
    )
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["value"]


def test_the_drain_returns_while_another_process_holds_the_write_end() -> None:
    """The defect itself. A surviving holder must not stall the boundary."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'{"schema":"x"}')
    # A second holder of the same write end: this is the inherited descriptor
    # the owner half leaves behind. It is never closed during this test.
    survivor = os.dup(write_fd)
    os.close(write_fd)
    try:
        handoff = _with_deadline(lambda: BRIDGE._drain_handoff(read_fd))
        assert handoff == '{"schema":"x"}', handoff
    finally:
        os.close(survivor)


def test_the_drain_returns_when_nothing_was_written() -> None:
    """A stop writes no handoff at all, and still has a boundary to record."""
    read_fd, write_fd = os.pipe()
    survivor = os.dup(write_fd)
    os.close(write_fd)
    try:
        assert _with_deadline(lambda: BRIDGE._drain_handoff(read_fd)) == ""
    finally:
        os.close(survivor)


def test_the_drain_takes_everything_the_child_wrote() -> None:
    """Reaping first must not cost us the handoff a start depends on."""
    read_fd, write_fd = os.pipe()
    payload = b'{"schema":"switchyard.presentation-handoff.v2","slot_count":6}'
    os.write(write_fd, payload)
    os.close(write_fd)
    assert _with_deadline(lambda: BRIDGE._drain_handoff(read_fd)) == payload.decode()


def test_the_drain_still_refuses_an_oversized_handoff() -> None:
    """The bound survives the rewrite: one byte past the limit is still read."""
    read_fd, write_fd = os.pipe()
    limit = BRIDGE.HANDOFF_LIMIT_BYTES
    threading.Thread(
        target=lambda: (os.write(write_fd, b"x" * (limit + 64)), os.close(write_fd)),
        daemon=True,
    ).start()
    time.sleep(0.2)
    handoff = _with_deadline(lambda: BRIDGE._drain_handoff(read_fd))
    assert len(handoff) == limit + 1, len(handoff)
    assert len(handoff) > limit, "an oversized handoff must still be detectable as oversized"


def test_the_source_reaps_before_it_drains() -> None:
    """Order is the fix. Draining first reintroduces the hang exactly.

    Asserted against the source because the ordering is the whole defect and it
    is otherwise invisible: both orders pass every functional test above when
    nothing happens to be holding the descriptor, which is why this shipped.
    """
    source = (ROOT / "scripts" / "switchyard-tenant-control").read_text(encoding="utf-8")
    reap = source.index("_pid, status = os.waitpid(child, 0)")
    drain = source.index("handoff = _drain_handoff(read_fd)")
    assert reap < drain, "the child must be reaped before the pipe is drained"
    assert "handle.read(HANDOFF_LIMIT_BYTES + 1)" not in source, (
        "the blocking read-to-EOF is back; it is what hung the live stop"
    )


if __name__ == "__main__":
    run_module_tests(globals())
    print("tenant_control_handoff_eof_test: ok")
