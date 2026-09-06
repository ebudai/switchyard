#!/usr/bin/env python3
"""Install and verify the portable Switchyard board skill for every agent CLI.

`switchyard board-skill ...` is the supported public entry point; this script is
the same implementation, reachable from a deployed tree's `scripts/` directory.
"""

from __future__ import annotations

import argparse
import functools
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ticket_board.board_skill import (  # noqa: E402
    CanonicalSkill,
    default_source,
    install_command,
    resolve_source_commit,
    verify_command,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


def install(args: argparse.Namespace) -> int:
    return install_command(
        home=Path(args.home).expanduser(),
        tree_root=Path(args.tree_root).expanduser(),
        source=Path(args.source).expanduser() if args.source else None,
        source_commit=args.source_commit,
        force=args.force,
        dry_run=args.dry_run,
        error_func=_stderr,
    )


def verify(args: argparse.Namespace) -> int:
    return verify_command(
        home=Path(args.home).expanduser(),
        source_commit=args.source_commit,
        error_func=_stderr,
    )


def source_commit(args: argparse.Namespace) -> int:
    source = (
        default_source(_repo_root(), CanonicalSkill(args.skill))
        if args.skill
        else Path(args.source).expanduser()
    )
    print(resolve_source_commit(source))
    return 0


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    home_default = os.environ.get("HOME") or str(Path.home())

    install_parser = subparsers.add_parser("install", help="project the canonical skill into every CLI skill tree")
    install_parser.add_argument("--home", default=home_default, help="home directory whose CLI skill trees are written")
    install_parser.add_argument(
        "--source",
        default="",
        help="one canonical SKILL.md to project (default: every canonical skill in the tree)",
    )
    install_parser.add_argument(
        "--tree-root",
        default=str(_repo_root()),
        help="Switchyard tree holding the canonical skills (default: the tree this script ships in)",
    )
    install_parser.add_argument(
        "--source-commit",
        default="",
        help="release commit recorded as provenance (default: the source tree's release marker, "
        "else the checkout commit that carries this body)",
    )
    install_parser.add_argument("--force", action="store_true", help="also overwrite a copy with no Switchyard provenance footer")
    install_parser.add_argument("--dry-run", action="store_true", help="report what would change")
    install_parser.set_defaults(func=install)

    verify_parser = subparsers.add_parser("verify", help="report whether each CLI can discover the skill")
    verify_parser.add_argument("--home", default=home_default, help="home directory to check")
    verify_parser.add_argument("--source-commit", default="", help="require the installed copies to carry this provenance commit")
    verify_parser.set_defaults(func=verify)

    source_parser = subparsers.add_parser("source-commit", help="print the provenance commit for the canonical source")
    source_parser.add_argument("--source", default=str(default_source(_repo_root())))
    source_parser.add_argument(
        "--skill",
        default="",
        help="canonical skill name to resolve instead of --source",
    )
    source_parser.set_defaults(func=source_commit)
    return parser


def main(argv: list[str] | None = None, *, prog: str | None = None) -> int:
    args = build_parser(prog).parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
