#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from urllib import request


DEFAULT_BOARD_URL = "http://127.0.0.1:8770/api/tickets"
DEFAULT_DEDUPE_DIR = "/tmp/pgu-file-size-ticket-dedupe"
DEFAULT_LINE_LIMIT = 1250
SOURCE_EXTENSIONS = {".cpp", ".cu", ".fish", ".h", ".hpp", ".py", ".sh"}
SKIP_PREFIXES = ("external/", "third_party/")
ZERO_OID = "0" * 40
EMPTY_TREE_OID = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create board tickets for oversized source files introduced by a push.")
    parser.add_argument("--git-dir", required=True, help="Bare repo git dir that received the push.")
    parser.add_argument("--ref", required=True, help="Updated ref name, e.g. refs/heads/feature/foo.")
    parser.add_argument("--oldrev", required=True, help="Old object id from the update hook.")
    parser.add_argument("--newrev", required=True, help="New object id from the update hook.")
    parser.add_argument("--board-url", default=DEFAULT_BOARD_URL, help=f"Ticket board POST endpoint (default: {DEFAULT_BOARD_URL}).")
    parser.add_argument("--dedupe-dir", default=DEFAULT_DEDUPE_DIR, help=f"Directory holding file+size dedupe markers (default: {DEFAULT_DEDUPE_DIR}).")
    parser.add_argument("--line-limit", type=int, default=DEFAULT_LINE_LIMIT, help=f"Soft line limit (default: {DEFAULT_LINE_LIMIT}).")
    return parser.parse_args(argv)


def git_output(git_dir: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def iter_new_commits(git_dir: Path, oldrev: str, newrev: str) -> list[str]:
    if newrev == ZERO_OID:
        return []
    rev_range = newrev if oldrev == ZERO_OID else f"{oldrev}..{newrev}"
    output = git_output(git_dir, "rev-list", "--reverse", rev_range)
    return [line.strip() for line in output.splitlines() if line.strip()]


def first_parent(git_dir: Path, commit: str) -> str:
    output = git_output(git_dir, "rev-list", "--parents", "-n", "1", commit).strip().split()
    if len(output) <= 1:
        return EMPTY_TREE_OID
    return output[1]


def changed_paths(git_dir: Path, parent: str, commit: str) -> list[str]:
    output = git_output(
        git_dir,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "--diff-filter=AM",
        parent,
        commit,
    )
    return [line.strip() for line in output.splitlines() if line.strip()]


def should_check_path(path: str) -> bool:
    if path.startswith(SKIP_PREFIXES):
        return False
    return Path(path).suffix.lower() in SOURCE_EXTENSIONS


def line_count_for_blob(git_dir: Path, treeish: str, path: str) -> int | None:
    proc = subprocess.run(
        ["git", f"--git-dir={git_dir}", "show", f"{treeish}:{path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if proc.returncode != 0:
        return None
    return sum(1 for _ in proc.stdout.splitlines())


def dedupe_key(path: str, line_count: int) -> str:
    digest = hashlib.sha256()
    digest.update(path.encode("utf-8", errors="replace"))
    digest.update(b"\n")
    digest.update(str(line_count).encode("ascii"))
    return digest.hexdigest()


def should_emit_ticket(dedupe_dir: Path, path: str, line_count: int) -> bool:
    dedupe_dir.mkdir(parents=True, exist_ok=True)
    marker = dedupe_dir / dedupe_key(path, line_count)
    if marker.exists():
        return False
    marker.write_text(f"{path}\n{line_count}\n", encoding="utf-8")
    return True


def build_ticket_payload(*, path: str, line_count: int, line_limit: int, commit: str, refname: str) -> dict[str, object]:
    title = f"soft file-size limit exceeded: {path} ({line_count} lines)"
    body = "\n".join(
        [
            f"File `{path}` exceeded the soft source-file limit of {line_limit} lines.",
            "",
            f"Observed size: {line_count} lines",
            f"Commit: {commit}",
            f"Ref: {refname}",
            "",
            "This ticket was auto-created by the bare-repo update hook.",
        ]
    )
    return {
        "title": title,
        "body": body,
        "assignee": "unassigned",
        "needs_eric_signoff": False,
        "screenshot": None,
    }


def create_ticket(board_url: str, payload: dict[str, object]) -> str:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        board_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=10) as response:
        response_body = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(response_body)
    ticket = parsed.get("ticket")
    if not isinstance(ticket, dict) or not isinstance(ticket.get("id"), str):
        raise RuntimeError("ticket board response did not include ticket.id")
    return ticket["id"]


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    git_dir = Path(args.git_dir).resolve()
    dedupe_dir = Path(args.dedupe_dir)

    try:
        commits = iter_new_commits(git_dir, args.oldrev, args.newrev)
    except subprocess.CalledProcessError as exc:
        print(f"[file-size-hook] failed to enumerate commits: {exc}", file=sys.stderr)
        return 1

    emitted: set[tuple[str, int]] = set()
    for commit in commits:
        parent = first_parent(git_dir, commit)
        for path in changed_paths(git_dir, parent, commit):
            if not should_check_path(path):
                continue
            current_lines = line_count_for_blob(git_dir, commit, path)
            if current_lines is None or current_lines <= args.line_limit:
                continue
            previous_lines = line_count_for_blob(git_dir, parent, path) or 0
            if current_lines <= previous_lines:
                continue
            key = (path, current_lines)
            if key in emitted:
                continue
            if not should_emit_ticket(dedupe_dir, path, current_lines):
                continue
            payload = build_ticket_payload(
                path=path,
                line_count=current_lines,
                line_limit=args.line_limit,
                commit=commit,
                refname=args.ref,
            )
            try:
                ticket_id = create_ticket(args.board_url, payload)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[file-size-hook] failed to create ticket for {path} ({current_lines} lines): {exc}",
                    file=sys.stderr,
                )
                continue
            emitted.add(key)
            print(
                f"[file-size-hook] created {ticket_id} for {path} ({current_lines} lines) on {args.ref}",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
