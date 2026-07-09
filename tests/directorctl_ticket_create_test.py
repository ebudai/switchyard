#!/usr/bin/env python3
"""Regression test: directorctl ticket-create uses the atomic reservation path."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from multiprocessing import Process, Queue
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORCTL = ROOT / "scripts" / "directorctl"
WORKERS = 8


def run_create(index: int, store: Path, frames: Path, queue: Queue[dict[str, str]]) -> None:
    cmd = [
        str(DIRECTORCTL),
        "ticket-create",
        "--store",
        str(store),
        "--frames",
        str(frames),
        "--assignee",
        "app",
        "--state",
        "ready",
        "--blocked-by",
        "PGU-23, PGU-25",
        "--blocked-reason",
        "Waiting on PGU-23 and PGU-25.",
        "--title",
        f"cli collision test {index}",
        "--body",
        f"worker {index}",
        "--comment-who",
        "director",
        "--comment-text",
        f"created from worker {index}",
        "--implementation",
        f"implementation {index}",
    ]
    completed = subprocess.run(cmd, check=True, text=True, capture_output=True)
    queue.put(json.loads(completed.stdout))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="directorctl-ticket-create.") as tmpdir:
        root = Path(tmpdir)
        store = root / "store"
        frames = root / "frames"
        store.mkdir()
        frames.mkdir()

        single = subprocess.run(
            [
                str(DIRECTORCTL),
                "ticket-create",
                "--store",
                str(store),
                "--frames",
                str(frames),
                "--assignee",
                "director",
                "--state",
                "ready",
                "--blocked-by",
                "PGU-23 PGU-25",
                "--blocked-reason",
                "Waiting on PGU-23 and PGU-25.",
                "--title",
                "single create",
                "--body",
                "single body",
                "--comment-who",
                "director",
                "--comment-text",
                "speced + routed",
                "--implementation",
                "ship the helper",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        ticket = json.loads(single.stdout)
        assert ticket["assignee"] == "director"
        assert ticket["state"] == "ready"
        assert ticket["screenshots"] == []
        assert ticket["screenshot"] is None
        assert ticket["comments"][0]["who"] == "director"
        assert ticket["comments"][0]["text"] == "speced + routed"
        assert ticket["comments"][0]["ts"] == ticket["created"]
        assert ticket["blocked_by"] == ["PGU-23", "PGU-25"]
        assert ticket["blocked_reason"] == "Waiting on PGU-23 and PGU-25."
        assert ticket["implementation"] == "ship the helper"

        loaded = json.loads((store / f"{ticket['id']}.json").read_text(encoding="utf-8"))
        assert loaded["state"] == "ready"
        assert loaded["comments"][0]["ts"] == loaded["created"]

        queue: Queue[dict[str, str]] = Queue()
        workers = [Process(target=run_create, args=(index, store, frames, queue)) for index in range(WORKERS)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0, f"worker exitcode={worker.exitcode}"

        created = [queue.get(timeout=2) for _ in workers]
        ids = [item["id"] for item in created]
        assert len(ids) == WORKERS
        assert len(set(ids)) == WORKERS, ids

        files = sorted(store.glob("PGU-*.json"))
        assert len(files) == WORKERS + 1

    print("directorctl_ticket_create_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
