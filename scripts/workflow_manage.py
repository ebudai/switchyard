from __future__ import annotations
import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import urllib.request
from scripts.ticket_board.workflow_config import validate
from scripts.ticket_board.write_client import TicketBoardWriteClient
from scripts.workflow_launcher import (
    board_unit_role_account_gap,
    prepare_role,
    projection_files,
)


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


def _project_dir_for(config_path: Path, document: dict) -> Path | None:
    """Where this tenant's project lives, from the artifact the projection already knows."""
    project = document.get("project")
    if not project:
        return None
    artifact_path = config_path.parent.parent / f"{project}.project.json"
    try:
        repository = json.loads(artifact_path.read_text())["project"]["repository"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    return Path(repository)


def _hand_projection_to_tenant(config_path: Path, *extra: Path) -> None:
    """Give the projection back to the tenant when this write ran privileged.

    `switchyard upgrade` re-executes as root, so a migration applied from there would
    otherwise leave root-owned controls the tenant cannot rewrite. Provisioning already
    has the handover; this reuses it rather than inventing a second one, and names any
    extra paths -- the rollback journal -- that its fixed projection set does not cover.
    It is a no-op for an ordinary unprivileged write, where the files are the tenant's.
    """
    if os.geteuid() != 0:
        return
    from scripts import team_launcher as launcher
    from scripts.workflow_launcher import assign_projection_owner

    raw = json.loads(config_path.read_text())
    project = raw.get("project")
    if not project:
        return
    config = launcher.load_project_config(project, config_path)
    assign_projection_owner(config, config_path, runner=subprocess.run)
    # The rollback journal is part of the tenant's recovery path, so it must be theirs
    # too. assign_projection_owner enumerates a fixed projection set and deliberately
    # does not sweep the directory, so the journal has to be named explicitly.
    for path in extra:
        if path.exists():
            launcher.ensure_owner_file(config, path, runner=subprocess.run)


PHASE_ONE_BOARD_REQUIRED = "board predates the phase-one schema"


def _board_predates_phase_one(board_url: str, cfg: dict, expected_revision: int) -> bool:
    """Whether the running board rejects the phase-one schema.

    Asked with a dry run, so nothing is written to a board that cannot accept it. Only a
    schema rejection counts: any other failure is a real error and must surface.
    """
    try:
        TicketBoardWriteClient(board_url=board_url).configure_workflow(
            cfg, expected_revision=expected_revision, dry_run=True
        )
    except Exception as exc:
        message = str(exc).lower()
        return (
            "unknown workflow configuration field" in message
            or "unknown role field" in message
        )
    return False


def assert_projection_writable(files: dict[Path, str]) -> None:
    """Refuse before the board write if any projected file cannot be replaced.

    The board commits first, so a projection that fails afterwards leaves the
    board ahead of the files. Everything the projection writes must therefore be
    tenant-owned; anything root installs -- the generated systemd unit -- is
    deliberately not part of it.
    """
    for path in files:
        parent = path.parent
        while not parent.exists():
            parent = parent.parent
        if not os.access(parent, os.W_OK) or (
            path.exists() and not os.access(path, os.W_OK)
        ):
            raise PermissionError(f"projection path is not writable: {path}")


def _project_slug(config_path: Path) -> str:
    try:
        return str(json.loads(config_path.read_text()).get("project") or "").strip() or "<project>"
    except (OSError, ValueError):
        return "<project>"


def _pending_board_unit_roles(config_path: Path) -> list[str]:
    """Roles the generated board unit cannot resolve yet, if this is a generated project."""
    plan_path = config_path.parent / "plan.json"
    try:
        plan = json.loads(plan_path.read_text())
    except (OSError, ValueError):
        return []
    return board_unit_role_account_gap(plan, config_path.parent)


def _reconcile_projection(config_path: Path, document: dict) -> bool:
    """Rewrite the projection when it does not match the board's document.

    configure_workflow commits before the projection is written, so a failure between
    the two leaves the board ahead of the files. The migration is idempotent on the
    board, which would otherwise mean a retry reported success while the stale
    projection stayed on disk.
    """
    try:
        files = projection_files(config_path, document)
    except Exception:
        return False
    stale = {
        path: content
        for path, content in files.items()
        if not path.exists() or path.read_text() != content
    }
    if not stale:
        return False
    apply_files(stale)
    _hand_projection_to_tenant(config_path)
    return True


def _migrate_director_onboarding(config_path: Path, cfg: dict) -> dict:
    """Fill a configured director's missing onboarding before the document is written.

    Runs on every path that persists a document -- apply, rollback/recovery, and the
    role-prompt operations -- so a tenant is migrated by ordinary use rather than by a
    manual per-tenant step. It is idempotent and only ever fills a gap, and it reports
    when it changes something so an automatic configuration change is not silent.
    """
    from scripts import team_launcher as launcher

    project_dir = _project_dir_for(config_path, cfg)
    if project_dir is None:
        return cfg
    cfg, outcome = launcher.seed_director_onboarding(cfg, project_dir)
    if outcome.seeded:
        print(
            f"workflow: seeded the director's onboarding prompt from {project_dir}; "
            "it had none stored. Change it with `switchyard role-prompt set director`.",
            file=sys.stderr,
        )
    return cfg


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
            "migrate-director-onboarding",
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
    elif args.operation == "migrate-director-onboarding":
        # The same migration the apply tail performs, exposed so upgrade and recovery can
        # invoke it deliberately. Short-circuits when there is nothing to fill, so
        # running it repeatedly costs no revision and no journal.
        from scripts import team_launcher as launcher

        if not current.get("document"):
            print(json.dumps({"migrated": False, "reason": "no active workflow configuration"}))
            return 0
        candidate = copy.deepcopy(current["document"])
        project_dir = _project_dir_for(args.config, candidate)
        if project_dir is None:
            print(json.dumps({"migrated": False, "reason": "project directory could not be resolved"}))
            return 0
        candidate, outcome = launcher.seed_director_onboarding(candidate, project_dir)
        if not outcome.changed:
            # The board is migrated, but a previous run may have committed the board
            # change and then failed to write the projection. Retrying must repair that
            # rather than report success over a stale projection.
            reconciled = _reconcile_projection(args.config, current["document"])
            print(
                json.dumps(
                    {
                        "migrated": False,
                        "reason": "already migrated",
                        "projection_reconciled": reconciled,
                    }
                )
            )
            return 0
        # A tenant whose director already had its own onboarding still needs the marker
        # persisted. Short-circuiting on "nothing seeded" would drop it, and a later
        # clear would then look like a legacy gap and be refilled.
        cfg = validate(candidate)
        # Preflight against the board that is actually running. A pre-phase-one release
        # rejects the new fields, and there is no write we can make it accept: the
        # correct next step is to deploy phase one, not to fail the upgrade that would
        # have told the operator to do so.
        if _board_predates_phase_one(args.board_url, cfg, current["revision"]):
            print(
                json.dumps(
                    {
                        "migrated": False,
                        "reason": PHASE_ONE_BOARD_REQUIRED,
                        "action_required": (
                            "deploy the phase-one board release, then rerun switchyard "
                            "upgrade to migrate this tenant"
                        ),
                    }
                )
            )
            return 0
        files = projection_files(args.config, cfg)
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
    cfg = _migrate_director_onboarding(args.config, cfg)
    files = projection_files(args.config, cfg)
    assert_projection_writable(files)
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
    # Every operation that persists a document writes a journal, so every one of them
    # may name it. The default is keyed on the expected revision, which collides when
    # two writes are prepared against the same revision.
    journal = (
        args.journal
        if args.journal and args.operation != "rollback"
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
    # Before the board write and before the fallible projection: on a
    # board-success/projection-failure the journal is the tenant's recovery evidence, and
    # handing it over afterwards would leave it root-owned exactly when it is needed.
    _hand_projection_to_tenant(args.config, journal)
    result = client.configure_workflow(cfg, expected_revision=expected)
    try:
        apply_files(files)
        _hand_projection_to_tenant(args.config, journal)
    except Exception as exc:
        raise RuntimeError(
            f'board revision {result["revision"]} applied; local projection pending. Preserve {journal} and rerun apply with the same document: {exc}'
        ) from exc
    # A role the document added now has a Unix account in the plan, but only an
    # operator can rewrite and install the board unit that carries the table the
    # board resolves uids through. Until that happens the role is configured on
    # the board and unrecognised on the socket, so say which roles and what to
    # run rather than leaving it to be discovered as a role that cannot write.
    pending_roles = _pending_board_unit_roles(args.config)
    if pending_roles:
        print(
            f"workflow: {', '.join(pending_roles)} now run as their own Unix accounts, but the "
            f"board unit still carries the previous table. An operator must run "
            f"`switchyard upgrade {_project_slug(args.config)}` to regenerate and install it "
            "before those roles can write to the board.",
            file=sys.stderr,
        )
    print(
        json.dumps(
            {
                "revision": result["revision"],
                "journal": str(journal),
                "files": [str(p) for p in files],
                "role_account_refresh_required": pending_roles,
                "sessions_started": False,
            },
            indent=2,
        )
    )
    return 0
