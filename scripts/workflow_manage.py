from __future__ import annotations
import argparse
import copy
import json
import os
import sys
from pathlib import Path
import tempfile
import urllib.request
from scripts.ticket_board.workflow_config import validate
from scripts.ticket_board.write_client import TicketBoardWriteClient
from scripts.workflow_launcher import projection_files, prepare_role


def atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        if path.exists():
            os.chmod(name, path.stat().st_mode & 0o777)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def apply_files(files: dict[Path, str]) -> None:
    prior = {p: p.read_text() if p.exists() else None for p in files}
    try:
        for path, content in files.items():
            atomic(path, content)
    except Exception:
        for path, content in prior.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, content)
        raise


def _role_named(current: dict, name: str, parser: argparse.ArgumentParser) -> dict:
    document = current.get("document")
    if not document:
        parser.error("no workflow configuration is active for this board")
    for role in document["roles"]:
        if role.get("name") == name:
            return role
    known = ", ".join(sorted(r.get("name", "") for r in document["roles"]))
    parser.error(f"unknown role: {name} (configured roles: {known})")


def _requested_prompt(args, parser: argparse.ArgumentParser) -> str:
    if args.prompt is not None and args.prompt_file is not None:
        parser.error("give --prompt or --prompt-file, not both")
    if args.prompt is not None:
        text = args.prompt
    elif args.prompt_file is not None:
        text = (
            sys.stdin.read()
            if str(args.prompt_file) == "-"
            else args.prompt_file.read_text(encoding="utf-8")
        )
    else:
        parser.error("set-role-prompt requires --prompt or --prompt-file")
    if not text.strip():
        parser.error("onboarding prompt is empty; use clear-role-prompt to remove one")
    return text


def _document_with_role_prompt(current: dict, args, parser: argparse.ArgumentParser) -> dict:
    if not args.role:
        parser.error(f"{args.operation} requires --role")
    document = copy.deepcopy(current["document"]) if current.get("document") else None
    if document is None:
        parser.error("no workflow configuration is active for this board")
    _role_named({"document": document}, args.role, parser)
    for role in document["roles"]:
        if role.get("name") != args.role:
            continue
        if args.operation == "clear-role-prompt":
            # Absent, not empty: the schema treats an empty prompt as invalid so that
            # "set" and "clear" cannot be confused when the document is read back.
            role.pop("onboarding_prompt", None)
        else:
            role["onboarding_prompt"] = _requested_prompt(args, parser)
    return validate(document)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=[
            "show",
            "validate",
            "apply",
            "rollback",
            "prepare-role",
            "show-role-prompt",
            "set-role-prompt",
            "clear-role-prompt",
        ],
    )
    parser.add_argument("--document", type=Path)
    parser.add_argument(
        "--rollback-document",
        type=Path,
        help="reviewed baseline policy required for first activation",
    )
    parser.add_argument(
        "--config", type=Path, help="existing/generated launcher config"
    )
    parser.add_argument(
        "--board-url",
        default=os.environ.get("TICKET_BOARD_URL", "http://127.0.0.1:8770"),
    )
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--journal", type=Path, help="saved apply journal (required for rollback)"
    )
    parser.add_argument("--role")
    parser.add_argument("--prompt", help="onboarding prompt text for set-role-prompt")
    parser.add_argument(
        "--prompt-file",
        type=Path,
        help="read the onboarding prompt from a file, or from stdin when given as -",
    )
    args = parser.parse_args(argv)
    if args.operation == "prepare-role":
        if not args.config or not args.role:
            parser.error("prepare-role requires --config and --role")
        if args.dry_run:
            print(
                f"Would prepare only {args.role} worktree/hooks/trust from {args.config}; no session started"
            )
        else:
            prepare_role(args.config, args.role)
        return 0
    if args.operation == "validate":
        if not args.document:
            parser.error("validate requires --document")
        cfg = validate(json.loads(args.document.read_text()))
        files = projection_files(args.config, cfg) if args.config else {}
        print(
            json.dumps(
                {
                    "valid": True,
                    "roles": len(cfg["roles"]),
                    "stages": len(cfg["stages"]),
                    "files": [str(p) for p in files],
                },
                indent=2,
            )
        )
        return 0
    board_root = (
        args.board_url.rstrip("/").removesuffix("/api/tickets").removesuffix("/api")
    )
    with urllib.request.urlopen(board_root + "/api/workflow") as response:
        current = json.load(response)
    if args.operation == "show":
        print(json.dumps(current, indent=2))
        return 0
    if args.operation == "show-role-prompt":
        if not args.role:
            parser.error("show-role-prompt requires --role")
        role = _role_named(current, args.role, parser)
        print(
            json.dumps(
                {
                    "role": args.role,
                    "revision": current["revision"],
                    "onboarding_prompt": role.get("onboarding_prompt"),
                    "onboarding": role.get("onboarding"),
                },
                indent=2,
            )
        )
        return 0
    if not args.config:
        parser.error(f"{args.operation} requires --config")
    if args.operation == "rollback":
        if not args.journal:
            parser.error("rollback requires --journal")
        saved = json.loads(args.journal.read_text())
        cfg = saved["previous"]["document"]
        if cfg is None:
            raise ValueError(
                "initial activation cannot revert to implicit legacy policy; apply a reviewed legacy-equivalent document"
            )
        # Keep later identities for attribution without granting them activity.
        names = {r["name"] for r in cfg["roles"]}
        for role in current["document"]["roles"]:
            if role["name"] not in names:
                cfg["roles"].append({**role, "active": False, "slot": None})
        files = {
            Path(p): content
            for p, content in saved["previous_files"].items()
            if content is not None
        }
        # Persist the rollback document too, including retained historical identities.
        files.update(projection_files(args.config, cfg))
    elif args.operation in {"set-role-prompt", "clear-role-prompt"}:
        # The whole document is rewritten, but the director edits one field through a
        # command rather than hand-editing generated JSON. Everything below -- schema
        # validation, the writability precheck, optimistic concurrency on
        # expected_revision, the rollback journal and the atomic projection write -- is
        # the same path `apply` takes, so this cannot drift from it.
        cfg = _document_with_role_prompt(current, args, parser)
        files = projection_files(args.config, cfg)
    else:
        if not args.document:
            parser.error("apply requires --document")
        cfg = validate(json.loads(args.document.read_text()))
        files = projection_files(args.config, cfg)
    rollback_baseline = current
    if args.operation == "apply" and current["document"] is None:
        if not args.rollback_document:
            parser.error(
                "first activation requires --rollback-document for a validated explicit baseline"
            )
        baseline = validate(json.loads(args.rollback_document.read_text()))
        projection_files(args.config, baseline)
        rollback_baseline = {**current, "document": baseline}
    for path in files:
        parent = path.parent
        while not parent.exists():
            parent = parent.parent
        if not os.access(parent, os.W_OK) or (
            path.exists() and not os.access(path, os.W_OK)
        ):
            raise PermissionError(f"projection path is not writable: {path}")
    expected = (
        args.expected_revision
        if args.expected_revision is not None
        else current["revision"]
    )
    client = TicketBoardWriteClient(board_url=args.board_url)
    if rollback_baseline is not current:
        client.configure_workflow(
            rollback_baseline["document"], expected_revision=expected, dry_run=True
        )
    client.configure_workflow(cfg, expected_revision=expected, dry_run=True)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "expected_revision": expected,
                    "document": cfg,
                    "projection": {str(p): json.loads(v) for p, v in files.items()},
                },
                indent=2,
            )
        )
        return 0
    journal = (
        args.journal
        if args.operation == "apply" and args.journal
        else args.config.parent / f"workflow-before-{expected}.json"
    )
    if journal.exists():
        saved = json.loads(journal.read_text())
        if saved.get("desired") != cfg:
            raise ValueError(
                f"preserve existing journal {journal}; select a fresh --journal path"
            )
    else:
        atomic(
            journal,
            json.dumps(
                {
                    "previous": rollback_baseline,
                    "desired": cfg,
                    "previous_files": {
                        str(p): p.read_text() if p.exists() else None for p in files
                    },
                },
                indent=2,
            )
            + "\n",
        )
    result = client.configure_workflow(cfg, expected_revision=expected)
    try:
        apply_files(files)
    except Exception as exc:
        raise RuntimeError(
            f'board revision {result["revision"]} applied; local projection pending. Preserve {journal} and rerun apply with the same document: {exc}'
        ) from exc
    print(
        json.dumps(
            {
                "revision": result["revision"],
                "journal": str(journal),
                "files": [str(p) for p in files],
                "sessions_started": False,
            },
            indent=2,
        )
    )
    return 0
