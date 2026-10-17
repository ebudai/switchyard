#!/usr/bin/env python3
"""SYRD-598: root decides who may confirm Director operations in the board UI, and the board reads only that.

`scripts/board_ui_record.py` records the approved board UI users in the project's registry record: the
host's approved desktop owner (from the host's root-owned desktop approval, never the tenant's own
configuration), or the accounts an operator names with --board-ui-user. Root, the board service, the
project account and role accounts are refused rather than recorded. Each decision is journalled. The
board's unit carries the record's path on its command line, and the board accepts the record root wrote.
"""

from __future__ import annotations

import json
import os
import pwd
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def main() -> int:
    from scripts import board_ui_record as record
    from scripts.ticket_board import project_provision, ui_user_identity

    me = pwd.getpwuid(os.getuid()).pw_name
    human = pwd.getpwnam("nobody")  # an account that is no tenant's, role's or service's here
    with tempfile.TemporaryDirectory(prefix="b598rec.") as tmp:
        tmp_path = Path(tmp)
        registry, journal = tmp_path / "projects", tmp_path / "rollout"
        registry.mkdir(mode=0o755)
        approval = tmp_path / "desktop-approval.json"
        approval.write_text(json.dumps({"gui_user": human.pw_name, "approved_by": human.pw_name}))

        def project(**changes):
            base = {"project": "pgu", "run_as_user": me, "roles": [], "desktop_access": None}
            return SimpleNamespace(**{**base, **changes})

        def registered() -> dict:
            return json.loads((registry / "pgu.json").read_text())

        (registry / "pgu.json").write_text(json.dumps({"schema": "switchyard.project-registry.v1", "slug": "pgu",
                                                       "name": "PGU", "config_path": "/x.json", "agent_clis": ["claude"]}))
        said: list[str] = []
        problems = record.record_board_ui_users(project(), registry_dir=registry, approval_path=approval,
                                                journal_root=journal, print_func=said.append)
        entry = registered()
        block = entry.get(record.RECORD_KEY, {})
        check(problems == [] and block.get("users") == [{"account": human.pw_name, "uid": human.pw_uid}]
              and block.get("source") == "the host's approved desktop owner",
              f"the host's approved desktop owner is recorded, by name and the uid root resolved: {problems}, {block}")
        check(block.get("tenant_user") == me and block.get("service_user") == "boardsvc"
              and block.get("roles_group") == "pgu-roles" and block.get("role_accounts") == [],
              f"root's exclusions are recorded with it, for the board to refuse by: {block}")
        check({k: entry[k] for k in ("schema", "slug", "name", "config_path", "agent_clis")}
              == {"schema": "switchyard.project-registry.v1", "slug": "pgu", "name": "PGU", "config_path": "/x.json",
                  "agent_clis": ["claude"]}
              and stat.S_IMODE((registry / "pgu.json").stat().st_mode) == 0o644
              and not [p for p in registry.iterdir() if p.name.startswith(".")],
              f"only its own key is written, atomically, 0644, nothing left behind: {sorted(entry)}")
        results = sorted(journal.rglob("result.json"))
        journalled = json.loads(results[-1].read_text()) if results else {}
        check(journalled.get("status") == "recorded" and "board-ui-users" in journalled.get("command", [])
              and f"{human.pw_name} (uid {human.pw_uid})" in journalled.get("detail", ""),
              f"the decision is written to the rollout journal: {journalled}")
        written, journalled_count = (registry / "pgu.json").read_text(), len(results)
        said.clear()
        record.record_board_ui_users(project(), registry_dir=registry, approval_path=approval, journal_root=journal,
                                     print_func=said.append)
        check((registry / "pgu.json").read_text() == written and len(sorted(journal.rglob("result.json"))) == journalled_count
              and said == [f"switchyard: pgu's board UI users are already recorded: {human.pw_name} (uid {human.pw_uid})"],
              f"the same decision again rewrites and journals nothing: {said}")
        registry.chmod(0o555)
        try:
            unwritable = record.record_board_ui_users(project(), registry_dir=registry, operator_users=[],
                                                      approval_path=tmp_path / "none.json",
                                                      journal_root=journal, print_func=said.append)
        finally:
            registry.chmod(0o755)
        failed = json.loads(sorted(journal.rglob("result.json"))[-1].read_text())
        check(len(unwritable) == 1 and unwritable[0].startswith(f"pgu's board UI users were not recorded: "
                                                                f"{registry / 'pgu.json'} cannot be written")
              and (registry / "pgu.json").read_text() == written and failed.get("status") == "failed"
              and failed.get("detail") == unwritable[0],
              f"a record that cannot be written is said, journalled as failed, and left as it was: {unwritable} {failed}")
        (tmp_path / "not-a-directory").write_text("")
        unjournalled = record.record_board_ui_users(project(), registry_dir=registry, operator_users=[],
                                                    approval_path=tmp_path / "none.json",
                                                    journal_root=tmp_path / "not-a-directory", print_func=said.append)
        check(len(unjournalled) == 1 and unjournalled[0].startswith(
            "pgu's board UI users were not recorded: the rollout journal cannot be opened")
              and (registry / "pgu.json").read_text() == written,
              f"a decision the journal cannot hold is not recorded either: {unjournalled}")
        (registry / "other.json").write_text(json.dumps({"slug": "other"}))
        check(record.record_board_ui_users(project(project="other"), registry_dir=registry, approval_path=approval,
                                           journal_root=journal, print_func=said.append)
              == [f"other's board UI users were not recorded: {registry / 'other.json'} is not a registry record"]
              and json.loads((registry / "other.json").read_text()) == {"slug": "other"},
              "a document that is not a registry record is not written into")
        missing_record = record.record_board_ui_users(project(project="absent"), registry_dir=registry,
                                                      approval_path=approval, journal_root=journal, print_func=said.append)
        check(len(missing_record) == 1 and "absent's board UI users were not recorded: " in missing_record[0]
              and "cannot be read" in missing_record[0] and not (registry / "absent.json").exists(),
              f"an unregistered project is not registered by this: {missing_record}")

        # The tenant's own configuration may not name another desktop.
        tenant_says = {"mode": "wayland", "project": "pgu", "tenant_user": me, "gui_user": me, "wayland_display": "auto",
                       "consent": {"approved": True, "by": me, "at": "2026-10-17T00:00:00Z", "reference": "x"}}
        problems = record.record_board_ui_users(project(desktop_access=tenant_says), registry_dir=registry,
                                                approval_path=approval, journal_root=journal, print_func=said.append)
        check(registered()[record.RECORD_KEY]["users"] == [] and problems and "not the tenant's to answer" in problems[0],
              f"a tenant configuration naming another desktop records nobody, and says why: {problems}")
        # An operator names the account (a headless host's way).
        problems = record.record_board_ui_users(project(desktop_access={"mode": "headless"}), registry_dir=registry,
                                                operator_users=[human.pw_name], journal_root=journal,
                                                print_func=said.append)
        block = registered()[record.RECORD_KEY]
        check(problems == [] and block["users"] == [{"account": human.pw_name, "uid": human.pw_uid}]
              and block["source"] == "operator (--board-ui-user)",
              f"an operator's --board-ui-user is recorded as the operator's decision: {block}")
        problems = record.record_board_ui_users(project(desktop_access={"mode": "headless"}), registry_dir=registry,
                                                approval_path=tmp_path / "none.json", journal_root=journal,
                                                print_func=said.append)
        check(registered()[record.RECORD_KEY]["users"] == [] and problems == [record.NONE_APPROVED],
              f"a headless host with nobody named records nobody: the board UI lends nothing: {problems}")

        # Accounts that are never a board UI user are refused, and nothing is written.
        before = (registry / "pgu.json").read_text()
        refusals = {}
        for name, config in (("the project account", project()), ("root", project()),
                             ("a role account", project(roles=[SimpleNamespace(run_as_user=human.pw_name)]))):
            account = {"the project account": me, "root": "root", "a role account": human.pw_name}[name]
            refusals[name] = record.record_board_ui_users(config, registry_dir=registry, operator_users=[account],
                                                          journal_root=journal, print_func=said.append)
        why_never = {"the project account": "is the project account", "root": "(uid 0) is root",
                     "a role account": "is a role account"}
        check(all(len(why) == 1 and why_never[name] in why[0] and why[0].endswith(
            ", which is never a board UI user; pgu's board UI users were left as they were") for name, why in refusals.items())
              and (registry / "pgu.json").read_text() == before,
              f"the project account, root and a role account are refused, and the record is unchanged: {refusals}")
        missing = record.record_board_ui_users(project(), registry_dir=registry, operator_users=["no-such-account-598"],
                                               journal_root=journal, print_func=said.append)
        check(missing == ["board UI user 'no-such-account-598' is not an account on this host; pgu's board UI users "
                          "were left as they were"] and (registry / "pgu.json").read_text() == before,
              f"an account that does not exist is refused, and the record is unchanged: {missing}")
        os.environ.pop("SWITCHYARD_ROLLOUT_JOURNAL_ROOT", None)
        said.clear()
        record.record_board_ui_users(project(), registry_dir=registry, approval_path=approval, print_func=said.append)
        check(registered()[record.RECORD_KEY]["users"] and said and said[-1].endswith(
            "; not journalled: only root keeps the rollout journal"),
              f"an unprivileged run told of no journal leaves root's alone, and says so: {said}")
        before = (registry / "pgu.json").read_text()
        said.clear()
        record.record_board_ui_users(project(), registry_dir=registry, approval_path=approval, dry_run=True,
                                     journal_root=journal, print_func=said.append)
        check((registry / "pgu.json").read_text() == before and any("would record" in line for line in said),
              f"a dry run writes nothing: {said}")

        # The board reads exactly what root wrote, from the path its unit names.
        record.record_board_ui_users(project(), registry_dir=registry, approval_path=approval, journal_root=journal,
                                     print_func=said.append)
        from scripts.project_identity import switchyard_registry_dir
        from scripts.ticket_board import cli

        os.environ.pop("SWITCHYARD_PROJECT_REGISTRY_DIR", None)
        os.environ["TICKET_BOARD_PYTHON"] = sys.executable
        unit = project_provision.render_board_unit(project_provision.build_plan(project="pgu", owner_user=me,
                                                                                owner_home=tmp_path))
        exec_start = shlex.split(next(line for line in unit.splitlines() if line.startswith("ExecStart="))[10:])
        sys.argv = exec_start[1:]
        check(cli.parse_args().board_ui_record == str(switchyard_registry_dir() / "pgu.json")
              == "/etc/switchyard/projects/pgu.json",
              f"the board's unit names the record root writes, on its command line, where the owner's "
              f"EnvironmentFile cannot replace it, and the board takes it from there: {exec_start}")

        class Started(Exception):
            pass

        def started(**_kwargs):
            raise Started

        ui_user_identity.RECORD_PATH, real_app = None, cli.TicketBoardApp
        cli.TicketBoardApp = started  # the board's first act after reading its arguments: stop there
        try:
            cli.run_server(cli.parse_args())
        except Started:
            pass
        finally:
            cli.TicketBoardApp = real_app
        check(ui_user_identity.RECORD_PATH == Path("/etc/switchyard/projects/pgu.json"),
              f"the board that unit starts reads its UI users from that record: {ui_user_identity.RECORD_PATH}")
        ui_user_identity.RECORD_PATH = registry / "pgu.json"
        ui_user_identity.TRUSTED_OWNER_UIDS = frozenset({0, os.getuid()})
        users = ui_user_identity.configured({})
        check(users.problem == "" and users.uids == frozenset({human.pw_uid}),
              f"the board accepts the record root wrote: {users}")
        ui_user_identity.TRUSTED_OWNER_UIDS = frozenset({0})
        check("is not owned by root" in ui_user_identity.configured({}).problem,
              "and only while root owns it: this test's own copy is not root's")
        ui_user_identity.TRUSTED_OWNER_UIDS = frozenset({0, os.getuid()})
        (registry / "linked.json").symlink_to(registry / "pgu.json")
        ui_user_identity.RECORD_PATH = registry / "linked.json"
        check(ui_user_identity.configured({}).problem.startswith(
            f"the project record is not trusted: {registry / 'linked.json'} is a symbolic link"),
              f"nor through a link, which whoever owns its target could repoint: {ui_user_identity.configured({})}")
        (registry / "self.json").write_text(json.dumps({"schema": record.REGISTRY_SCHEMA, record.RECORD_KEY: {
            "users": [{"account": me, "uid": os.getuid()}]}}))
        ui_user_identity.RECORD_PATH = registry / "self.json"
        check(f"names {me} (uid {os.getuid()}), which is the board service and never a board UI user"
              in ui_user_identity.configured({}).problem,
              f"the board's own account is never its UI user, though the record names no service: "
              f"{ui_user_identity.configured({})}")

    # Upgrade's U3 records them last, as the operator named them, and says a note on its own (not as the report
    # credential's, whose remedy it is not); tests/upgrade_phases_boundary_test.py fences the step in U3's order.
    import upgrade_phases_boundary_test as upb
    from scripts import team_launcher, upgrade_phases

    art, asked, said = upb.Artifacts(), [], []
    note = lambda config, **kwargs: asked.append((config, kwargs)) or ["syrd598: nobody approved"]  # noqa: E731
    with upb.patched(team_launcher, **art.names()), upb.patched(record, record_board_ui_users=note):
        result = upgrade_phases._refresh_upgrade_artifacts(
            SimpleNamespace(project=upb.PROJECT, run_as_user="syrd598-owner", name="incoming"), commit_git_dir="c",
            config_path=upb.CONFIG_PATH, dry_run=False, effective_source_repo=upb.SOURCE, print_func=said.append,
            registry_dir=Path("/nonexistent/syrd598/registry"), runner=upb.refusing_runner, source_repo=upb.GIVEN_SOURCE,
            upstream_report_token_file="", upstream_report_url="", board_ui_users=["syrd598-user"])
    check(result is art.linked and asked == [(art.linked, dict(registry_dir=Path("/nonexistent/syrd598/registry"),
                                                              operator_users=["syrd598-user"], dry_run=False,
                                                              print_func=said.append))]
          and said[-1] == "switchyard: syrd598: nobody approved" and not any("report credential" in line for line in said),
          f"upgrade records the operator's board UI users for the config it settled, and says a note on its own: "
          f"{asked} {said}")
    print(f"board_ui_record_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
