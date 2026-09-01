#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

DEFAULT_BOARD_URL = "http://127.0.0.1:8770"
DEFAULT_STATE_DIR = "/data/git/pgu.git/hooks/file-size-ticket-state"
DEFAULT_LINE_LIMIT = 1250
DEFAULT_EMIT_CAP = 8
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
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help=f"Directory holding persistent per-file markers (default: {DEFAULT_STATE_DIR}).")
    parser.add_argument("--line-limit", type=int, default=DEFAULT_LINE_LIMIT, help=f"Soft line limit (default: {DEFAULT_LINE_LIMIT}).")
    parser.add_argument("--emit-cap", type=int, default=DEFAULT_EMIT_CAP, help=f"Maximum tickets to create per push (default: {DEFAULT_EMIT_CAP}).")
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
    if oldrev == ZERO_OID:
        output = git_output(git_dir, "rev-list", "--reverse", newrev, "--not", "--all")
    else:
        output = git_output(git_dir, "rev-list", "--reverse", f"{oldrev}..{newrev}")
    return [line.strip() for line in output.splitlines() if line.strip()]


def first_parent(git_dir: Path, commit: str) -> str:
    output = git_output(git_dir, "rev-list", "--parents", "-n", "1", commit).strip().split()
    if len(output) <= 1:
        return EMPTY_TREE_OID
    return output[1]


def changed_paths_for_commit(git_dir: Path, parent: str, commit: str) -> list[str]:
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


def dedupe_key(path: str) -> str:
    digest = hashlib.sha256()
    digest.update(path.encode("utf-8", errors="replace"))
    return digest.hexdigest()


def should_emit_ticket(state_dir: Path, path: str) -> bool:
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / dedupe_key(path)
    if marker.exists():
        return False
    marker.write_text(f"{path}\n", encoding="utf-8")
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
        "needs_user_signoff": False,
        "screenshot": None,
    }


def create_ticket(board_url: str, payload: dict[str, object]) -> str:
    try:
        from ticket_board.write_client import TicketBoardWriteClient
    except ModuleNotFoundError:  # pragma: no cover - package import path
        from scripts.ticket_board.write_client import TicketBoardWriteClient

    parsed = TicketBoardWriteClient(board_url, "director").create_ticket(
        title=str(payload["title"]),
        body=str(payload["body"]),
        assignee=str(payload.get("assignee", "unassigned")),
        needs_user_signoff=bool(payload.get("needs_user_signoff", False)),
        screenshot=payload.get("screenshot"),  # type: ignore[arg-type]
    )
    ticket = parsed.get("ticket")
    if not isinstance(ticket, dict) or not isinstance(ticket.get("id"), str):
        raise RuntimeError("ticket board response did not include ticket.id")
    return ticket["id"]


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    git_dir = Path(args.git_dir).resolve()
    state_dir = Path(args.state_dir)
    if args.emit_cap <= 0:
        print("[file-size-hook] emit cap must be positive", file=sys.stderr)
        return 1

    try:
        commits = iter_new_commits(git_dir, args.oldrev, args.newrev)
    except subprocess.CalledProcessError as exc:
        print(f"[file-size-hook] failed to enumerate new commits: {exc}", file=sys.stderr)
        return 1

    changed_path_set: set[str] = set()
    for commit in commits:
        parent = first_parent(git_dir, commit)
        changed_path_set.update(changed_paths_for_commit(git_dir, parent, commit))

    oversized_paths: list[tuple[str, int]] = []
    for path in sorted(changed_path_set):
        if not should_check_path(path):
            continue
        current_lines = line_count_for_blob(git_dir, args.newrev, path)
        if current_lines is None or current_lines <= args.line_limit:
            continue
        oversized_paths.append((path, current_lines))

    emitted = 0
    for path, current_lines in oversized_paths:
        if emitted >= args.emit_cap:
            print(
                f"[file-size-hook] emit cap {args.emit_cap} reached on {args.ref}; suppressing additional oversized files",
                file=sys.stderr,
            )
            break
        if not should_emit_ticket(state_dir, path):
            continue
        payload = build_ticket_payload(
            path=path,
            line_count=current_lines,
            line_limit=args.line_limit,
            commit=args.newrev,
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
        emitted += 1
        print(
            f"[file-size-hook] created {ticket_id} for {path} ({current_lines} lines) on {args.ref}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
