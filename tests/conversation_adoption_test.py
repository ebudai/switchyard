#!/usr/bin/env python3
"""SYRD-694: the control role resumes a legacy conversation of its own only by root's adoption record.

MEFP's Director conversation began before role worktrees existed, so Codex files it under the project
repository. SYRD-638 refuses it at every start, and the User could get it back only with Codex's in-pane
`/resume` picker. Here, over a test home shaped as Codex writes it (rollouts with their `session_meta` cwd) and
the real launcher start path (`_start_role_session`, `judge`, `refuse_foreign`), it is shown that:

- the preview names the provider, source, target, evidence and changes, and changes nothing;
- `--confirm` needs the preview's exact digest, and then asks for the catalogued privileged action, whose
  values the real catalogue validates and whose root operation is the real argv;
- the proof a start trusts is root's record alone. A record the tenant account wrote, in its store or in root's
  place, is refused, and so is any record reached through a link, at any component, or in a directory somebody
  else can write;
- every unapproved source is refused: another role's worktree, an arbitrary or unrecorded directory, a
  conversation another role owns or recorded, a live one, a non-control role, and a malformed id;
- an earlier worktree of the same role is proven only by a conversation that role owns, never by an adopted one;
- the next start resumes exactly the adopted conversation, and every other conversation from that workspace,
  or for another role, pane or worktree, is still refused;
- rollback removes only root's record, and the conversation is refused again;
- status says the conversation is adopted, and where it began.

Root's tree is a directory of the test's own, which `conversation_adoption_record` is pointed at, since a test
is not root; what ships is asserted. tmux is stood in for and refuses anything that types into a pane. No live
home is used: HOME is the test's own.
"""

from __future__ import annotations

import getpass
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CHECKS = 0
LEGACY = "019944aa-1111-7000-8000-000000000694"
OWN = "019944aa-2222-7000-8000-000000000694"
OTHER = "019944aa-3333-7000-8000-000000000694"
EARLIER = "019944aa-4444-7000-8000-000000000694"


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def main() -> int:
    for key in list(os.environ):
        if key.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL", "TMUX")):
            del os.environ[key]
    from scripts import conversation_adoption as adoption
    from scripts import conversation_adoption_record as root_record
    from scripts import session_provenance as provenance
    from scripts import team_launcher as launcher
    from scripts.ticket_board import privileged_actions, privileged_helper, privileged_operations

    shipped = (root_record.ROOT, root_record.ANCHOR, root_record.TRUSTED_OWNER_UID)
    check(shipped == (Path("/var/lib/switchyard/conversation-adoptions"), Path("/"), 0),
          f"as shipped, the record is root's, at a fixed path, walked from /: {shipped}")
    for name, arguments in (("adopt-conversation", ["project", "role", "conversation", "digest"]),
                            ("withdraw-conversation-adoption", ["project", "role", "conversation"])):
        action = privileged_actions.action_for(name)
        check(action.authentication == privileged_actions.ALLOW_ACTIVE and action.requester_argument == "role"
              and [n for n, _ in action.arguments] == arguments,
              f"{name} names its project (so its helper proves the control pane), binds its role to that pane, and "
              f"takes no value root records unchecked: {action}")

    # Audit's probe: the genuine Director's pane asks root to adopt for Ops.
    adopt = privileged_actions.action_for("adopt-conversation")
    ran: list = []

    class Attempt:
        attempt, describe_job = "a1", None

        def open(self):
            return "journal"

        def close(self, **_kwargs):
            pass

    def helper(role: str, requester: str) -> object:
        try:
            return privileged_helper.run_privileged_action(
                adopt, adopt.validate({"project": "pgu", "role": role, "conversation": LEGACY, "digest": "ab" * 32}),
                project="pgu", attempt_factory=lambda *a, **k: Attempt(),
                runner=lambda command, **_k: ran.append(command) or subprocess.CompletedProcess(command, 0),
                requester_role=requester, print_func=lambda _line: None)
        except privileged_helper.Refused as exc:
            return exc

    refused = helper("ops", "director")
    check(isinstance(refused, privileged_helper.Refused) and "acts only for the role that asked" in str(refused)
          and not ran, f"the helper refuses an adoption for any role but the one it proved asked: {refused}")
    check(isinstance(helper("director", ""), privileged_helper.Refused) and not ran,
          "and one no verified role asked for")
    check(helper("director", "director") == 0 and ran and ran[0][2:] == ["record", "pgu", "director", LEGACY, "ab" * 32],
          f"the Director's own adoption reaches root's operation with exactly its values: {ran}")

    work = Path(tempfile.mkdtemp(prefix="syrd694-"))
    os.chmod(work, 0o755)
    try:
        home = work / "home"
        home.mkdir()
        os.environ["HOME"] = str(home)
        rootfs = work / "rootfs"
        rootfs.mkdir(mode=0o755)
        root_record.ANCHOR, root_record.TRUSTED_OWNER_UID = rootfs, os.getuid()
        root_record.ROOT = rootfs / "var" / "lib" / "switchyard" / "conversation-adoptions"
        root_record.REGISTRY = rootfs / "etc" / "switchyard" / "projects"
        raw = json.loads((ROOT / "config" / "team-launcher" / "pgu.json").read_text(encoding="utf-8"))
        raw["run_as_user"] = getpass.getuser()
        repo = work / "stellaris-bugfix"
        repo.mkdir()
        raw["repository"] = str(repo)
        ops_raw = next(r for r in raw["roles"] if r["role"] == "ops")
        for role in raw["roles"]:
            role["workdir"] = str(work / "worktrees" / role["role"])
            if role["role"] == "director":  # MEFP's Director runs Codex
                role.update({k: ops_raw[k] for k in ("cli", "live_commands", "resume_mode", "resume_subcommand")})
                role.pop("model", None)
        layout = ROOT / "config" / "team-launcher" / raw["layout"]
        shutil.copy(layout, work / layout.name)
        config_path = work / "pgu.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        shared = home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
        state = work / "pane-state"
        config = replace(launcher.load_project_config("pgu", config_path), session_dir=shared,
                         role_state_isolation=True, desktop_access={"mode": "headless"})
        roles = {role.role: role for role in config.roles}
        director, ops = roles["director"], roles["ops"]
        launcher.live_command_matches_role = lambda role, runner=None: True
        launcher.RESUME_STARTUP_TIMEOUT_SECONDS = 0.3
        launcher.RESUME_STARTUP_POLL_SECONDS = 0.05

        class Tmux:
            def __init__(self, live: set[str] | None = None) -> None:
                self.calls: list[list[str]] = []
                self.live = set(live or ())

            def __call__(self, args, **_kwargs):
                args = [str(a) for a in args]
                self.calls.append(args)
                if args[:1] != ["tmux"] or args[1] in ("send-keys", "paste-buffer", "load-buffer", "set-buffer"):
                    raise AssertionError(f"refused: {args}")
                if args[1] == "has-session":
                    return subprocess.CompletedProcess(args, 0 if args[3] in self.live else 1, "", "")
                if args[1] == "new-session":
                    self.live.add(args[args.index("-s") + 1])
                return subprocess.CompletedProcess(args, 0, "", "")

        def own_dir(role) -> Path:
            return launcher.role_session_dir(config, role)

        def record_path(role) -> Path:
            return own_dir(role) / launcher.session_file_name(role.target)

        def converse(session_id: str, cwd: str | None) -> Path:
            path = home / ".codex" / "sessions" / "2026" / "09" / "05" / f"rollout-2026-09-05T15-50-41-{session_id}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            meta = {"id": session_id, **({"cwd": cwd} if cwd is not None else {})}
            path.write_text(json.dumps({"type": "session_meta", "payload": meta}) + "\n", encoding="utf-8")
            return path

        def seed(role, session_id: str) -> None:
            path = record_path(role)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"target": role.target, "session_id": session_id, "updated_at": time.time(),
                                        "source": "codex.SessionStart", "payload": None}) + "\n", encoding="utf-8")

        def owner(session_id: str, role: str, how: str = "workspace") -> None:
            path = shared / provenance.OWNERS_DIR / session_id
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"role": role, "session_id": session_id, "how": how}) + "\n")

        def history(role) -> list[dict]:
            path = own_dir(role) / f"{launcher.session_file_name(role.target).removesuffix('.json')}.resume.log"
            return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []

        def start(role) -> dict:
            tmux = Tmux()
            err = io.StringIO()
            before = len(history(role))
            with redirect_stderr(err):
                code = launcher._start_role_session(role, session_dir=own_dir(role), pane_state_dir=state,
                                                    prefer_resume=True, seed_source="syrd694.start", runner=tmux)
            argv = " ".join(c[-1] for c in tmux.calls if c[1:2] == ["new-session"])
            return {"code": code, "resumed": [s for s in (LEGACY, OWN, OTHER, EARLIER) if s in argv],
                    "said": err.getvalue(), "events": [e["event"] for e in history(role)[before:]]}

        proc = work / "proc"

        def process(pid: int, cmdline: list[str], ppid: int = 1) -> None:
            entry = proc / str(pid)
            entry.mkdir(parents=True, exist_ok=True)
            (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in cmdline) + b"\0")
            (entry / "stat").write_text(f"{pid} ({os.path.basename(cmdline[0])}) S {ppid} 0 0\n")

        # This process runs under 4100, the Director's `adopt-conversation ... --confirm`, which names the id.
        process(os.getpid(), ["python3", "conversation_adoption_test.py"], ppid=4100)

        asked: list[tuple[str, str, dict]] = []

        def tenant_plan(project: str, role: str, session_id: str) -> dict:
            """What root's operation reads from the tenant's own `--plan-json` run, computed here in-process."""
            said: list[str] = []
            adoption.adopt_conversation_command(config, config_path=config_path, role=role, session_id=session_id,
                                                plan_json=True, runner=Tmux(), proc=proc, print_func=said.append)
            return json.loads(said[-1])

        def front_door(answer: int = 0):
            """The privileged front door and root's operation: the real catalogue's validation and root argv, then
            what root does with it -- the plan computed again as the tenant, and recorded only if it is the one."""
            def ask(project, action, values, *, print_func):
                asked.append((project, action, dict(values)))
                validated = privileged_actions.action_for(action).validate({"project": project, **values})
                argv = privileged_operations.command_for(action, validated)
                check(argv[:2] == [privileged_operations.LAUNCHER, "conversation-adoption-record"],
                      f"{action} runs root's record operation from the shared release: {argv}")
                if answer:
                    return answer
                if argv[2] == "record":
                    project, role, session_id, digest = argv[3:]
                    root_record.record_from_plan(project, role, session_id, digest,
                                                 plan=tenant_plan(project, role, session_id))
                else:
                    root_record.remove(*argv[3:])
                return 0
            return ask

        def tree() -> dict[str, bytes]:
            return {str(p.relative_to(work)): p.read_bytes() for p in sorted(work.rglob("*"))
                    if p.is_file() and "proc" not in p.relative_to(work).parts}

        def command(*, role="director", session_id=LEGACY, rollback=False, confirm="", runner=None,
                    privileged=None) -> tuple[int | None, list[str]]:
            """The command's answer and what it printed; a crash is an answer of None, so a check names it."""
            lines: list[str] = []
            try:
                code = adoption.adopt_conversation_command(
                    config, config_path=config_path, role=role, session_id=session_id, rollback=rollback,
                    confirm=confirm, runner=runner or Tmux(), proc=proc, privileged=privileged or front_door(),
                    print_func=lines.append)
            except Exception as exc:  # noqa: BLE001 -- reported by the check that reads it
                return None, lines + [f"CRASHED: {exc!r}"]
            return code, lines

        def digest(lines: list[str]) -> str:
            found = re.search(r"--confirm ([0-9a-f]{64})", "\n".join(lines))
            return found.group(1) if found else ""

        legacy_rollout = converse(LEGACY, str(repo))
        converse(OWN, director.workdir)
        record = root_record.ROOT / f"{LEGACY}.json"

        # 1. As on MEFP: the User's /resume put the legacy conversation in the Director's record. Without an
        #    adoption, SYRD-638 refuses it exactly as before.
        seed(director, LEGACY)
        first = start(director)
        check(first["resumed"] == [] and "foreign_workspace" in first["events"],
              f"without an adoption the legacy conversation is refused as foreign: {first}")
        check((own_dir(director) / f"{record_path(director).name}.foreign-workspace").is_file(),
              "and its record is set aside as before")

        # 2. Audit's reproduction: a record any role of the tenant account could write is no proof.
        forged = {"schema": root_record.SCHEMA, "project": "pgu", "role": "director", "target": director.target,
                  "runtime": "codex", "session_id": LEGACY, "workspace_sha256": adoption.path_digest(str(repo)),
                  "worktree_sha256": adoption.path_digest(director.workdir), "plan_digest": "cd" * 32}
        tenant_store = shared / "conversation-adoptions" / f"{LEGACY}.json"
        tenant_store.parent.mkdir(parents=True)
        tenant_store.write_text(json.dumps({**forged, "schema": "switchyard.conversation-adoption.v1"}))
        check(not provenance.judge(director, LEGACY, None, session_dir=own_dir(director)).ok,
              "an adoption file in the project's own store, as a sibling role could write it, is refused")
        shutil.rmtree(tenant_store.parent)
        root_record.ROOT.mkdir(parents=True)
        record.write_text(json.dumps(forged))
        root_record.TRUSTED_OWNER_UID = 0
        verdict = provenance.judge(director, LEGACY, None, session_dir=own_dir(director))
        check(not verdict.ok and verdict.reason == "workspace",
              f"a well-formed record in root's place, but owned by the tenant account, is refused: {verdict}")
        root_record.TRUSTED_OWNER_UID = os.getuid()
        check(provenance.judge(director, LEGACY, None, session_dir=own_dir(director)).ok,
              "(the same record, owned by the trusted owner, is the proof)")
        for name, spoil, undo in (
                ("its directory writable by others", lambda: os.chmod(root_record.ROOT, 0o777),
                 lambda: os.chmod(root_record.ROOT, 0o755)),
                ("the record writable by others", lambda: os.chmod(record, 0o666), lambda: os.chmod(record, 0o644)),
                ("an ancestor writable by others", lambda: os.chmod(rootfs / "var", 0o777),
                 lambda: os.chmod(rootfs / "var", 0o755))):
            spoil()
            check(not provenance.judge(director, LEGACY, None, session_dir=own_dir(director)).ok,
                  f"a record with {name} is refused")
            undo()
        outside = work / "outside"
        outside.mkdir(mode=0o755)
        shutil.move(str(rootfs / "var" / "lib" / "switchyard"), str(outside / "switchyard"))
        os.symlink(outside / "switchyard", rootfs / "var" / "lib" / "switchyard")
        check(not provenance.judge(director, LEGACY, None, session_dir=own_dir(director)).ok
              and root_record.listing() == [],
              "a record reached through a link at an intermediate directory is refused")
        try:
            root_record.write({**forged, "session_id": OTHER})
            wrote = True
        except root_record.RecordError:
            wrote = False
        check(not wrote and not (outside / "switchyard" / "conversation-adoptions" / f"{OTHER}.json").exists(),
              "root's write refuses an intermediate link, and nothing is created where it points")
        (rootfs / "var" / "lib" / "switchyard").unlink()
        shutil.move(str(outside / "switchyard"), str(rootfs / "var" / "lib" / "switchyard"))
        record.unlink()
        os.symlink(outside / "planted.json", record)
        (outside / "planted.json").write_text(json.dumps(forged))
        check(not provenance.judge(director, LEGACY, None, session_dir=own_dir(director)).ok,
              "a record that is itself a link is refused")
        record.unlink()
        check(root_record.main(["record", "pgu", "director", LEGACY, "cd" * 32]) == 1 and not record.exists(),
              "root's operation refuses anybody but root")
        os.symlink(outside, record)
        record.unlink()

        # 2b. Root's own data: the tenant account from its registry, and the plan computed as that account.
        root_record.REGISTRY.mkdir(parents=True, mode=0o755)
        entry = root_record.REGISTRY / "pgu.json"
        entry.write_text(json.dumps({"slug": "pgu", "board_ui": {"tenant_user": "pgu-agent"}}))
        check(root_record.tenant_account("pgu") == "pgu-agent", "the tenant account is root's registry's")
        for name, document in (("no tenant account", {"slug": "pgu"}), ("root", {"board_ui": {"tenant_user": "root"}}),
                               ("a malformed account", {"board_ui": {"tenant_user": "pgu agent; id"}})):
            entry.write_text(json.dumps(document))
            try:
                root_record.tenant_account("pgu")
                answered = True
            except root_record.RecordError:
                answered = False
            check(not answered, f"a registry entry with {name} names no account to compute the plan as")
        os.chmod(root_record.REGISTRY, 0o777)
        try:
            root_record.tenant_account("pgu")
            answered = True
        except root_record.RecordError:
            answered = False
        check(not answered, "a registry others can write is not root's word")
        os.chmod(root_record.REGISTRY, 0o755)
        argv = root_record.plan_argv("pgu-agent", "/home/pgu-agent", "pgu", "director", LEGACY)
        check(argv[:6] == ["/usr/bin/runuser", "-u", "pgu-agent", "--", "/usr/bin/env", "-i"]
              and argv[-5:] == ["adopt-conversation", "pgu", "director", LEGACY, "--plan-json"]
              and str(root_record.WRAPPER) in argv and argv[argv.index(str(root_record.WRAPPER)) - 2:][:2] == ["-I", "-B"]
              and root_record.WRAPPER == ROOT / "scripts" / "switchyard",
              f"root computes the plan as the tenant, in a clean environment, with the release's own wrapper: {argv}")

        # 3. The preview: everything that decides the adoption, and nothing changed.
        before = tree()
        code, lines = command()
        text = "\n".join(lines)
        check(code == 0 and digest(lines), f"the preview offers a digest: {lines}")
        check(f"provider:  codex, at {legacy_rollout}" in text and f"source:    {repo} (the project repository" in text
              and f"target:    director ({director.target}), worktree {director.workdir}" in text,
              f"it names the provider, source workspace and target: {lines}")
        for evidence in ("codex holds it at", f"codex files it under {repo}", "no other role owns it",
                         "no other role's records or history name it", "it is not live"):
            check(f"evidence:  {evidence}" in text, f"its evidence says {evidence!r}: {lines}")
        check("changes:   root writes " in text and "adopt-conversation" in text and "registered pane" in text
              and "next start" in text and "nothing else" in text, f"it states every change: {lines}")
        check(tree() == before and not asked, "the preview writes nothing and asks for nothing privileged")
        good = digest(lines)

        # 4. --confirm: the exact digest, then the privileged action, and root's record is all that changes.
        code, lines = command(confirm="0" * 64)
        check(code == 1 and not asked and tree() == before, f"another digest asks for nothing: {lines}")
        code, lines = command(confirm=good, privileged=front_door(answer=1))
        check(code == 1 and not record.exists() and "did not complete" in "\n".join(lines),
              f"a privileged action that is refused leaves nothing adopted: {lines}")
        asked.clear()
        code, lines = command(confirm=good)
        check(code == 0 and record.is_file() and asked == [("pgu", "adopt-conversation", {
            "role": "director", "conversation": LEGACY, "digest": good})],
              f"the confirmed preview asks for adopt-conversation with the role, conversation and its digest: {asked} {lines}")
        written = root_record.read(LEGACY)
        check(written and {k: written[k] for k in root_record.FIELDS} == {**{k: forged[k] for k in root_record.FIELDS},
                                                                           "plan_digest": good}
              and written.get("recorded_at") and oct(record.stat().st_mode & 0o777) == "0o644",
              f"root's record holds the recomputed plan's values and its digest, readable by the tenant's starts: {written}")
        changed = {k for k in set(tree()) | set(before) if tree().get(k) != before.get(k)}
        check(changed == {str(record.relative_to(work))}, f"apply changes root's record and nothing else: {changed}")
        check(legacy_rollout.read_bytes() == before[str(legacy_rollout.relative_to(work))],
              "the conversation's content is not touched")
        code, lines = command(confirm=good)
        check(code == 1 and "adopted already" in "\n".join(lines) and len(asked) == 1, f"it is adopted once: {lines}")
        check(any("by adoption (SYRD-694)" in line and "its next start resumes it" in line
                  for line in adoption.status_lines(config)), f"status: adopted, not yet resumed: {adoption.status_lines(config)}")

        # 5. The next start resumes exactly it, and keeps the record it replaced.
        seed(director, OWN)  # the Director carried on fresh in its worktree meanwhile
        resumed = start(director)
        check(resumed["resumed"] == [LEGACY] and "adoption_installed" in resumed["events"]
              and "adopted_conversation" in resumed["events"] and "resumed" in resumed["events"],
              f"the Director's next start resumes the adopted conversation, no /resume needed: {resumed}")
        check(json.loads(record_path(director).read_text())["session_id"] == LEGACY
              and json.loads((own_dir(director) / f"{record_path(director).name}.superseded").read_text())["session_id"] == OWN,
              "its record names it, and the one it replaced is kept as superseded")
        claim = json.loads((shared / provenance.OWNERS_DIR / LEGACY).read_text())
        check(claim["role"] == "director" and claim["how"] == "adopted", f"the first resume claims it, as adopted: {claim}")
        again = start(director)
        check(again["resumed"] == [LEGACY] and "adoption_installed" not in again["events"],
              f"and every later start resumes it with no further install: {again}")
        check(any("resumed since" in line and f"began in {repo}" in line for line in adoption.status_lines(config)),
              f"status says it is adopted and where it began: {adoption.status_lines(config)}")

        # 6. Only that conversation, only for that role.
        converse(OTHER, str(repo))
        seed(director, OTHER)
        other = start(director)
        check(other["resumed"] == [] and "foreign_workspace" in other["events"],
              f"another conversation from the same repository is still refused: {other}")
        seed(ops, LEGACY)
        foreign = start(ops)
        check(foreign["resumed"] == [] and any(e.startswith("foreign_") for e in foreign["events"]),
              f"the adopted conversation is still refused for another role: {foreign}")
        verdict = provenance.judge(director, LEGACY, None, session_dir=own_dir(director))
        check(not verdict.ok and verdict.reason == "other-role",
              f"once another role's store names it, the adopted conversation is refused like any other: {verdict}")
        shutil.rmtree(own_dir(ops))
        claimed = (shared / provenance.OWNERS_DIR / LEGACY).read_bytes()
        (shared / provenance.OWNERS_DIR / LEGACY).unlink()
        for name, role in (("another role", replace(ops, workdir=director.workdir)),
                           ("another pane", replace(director, target="pgu-director:1.0")),
                           ("another worktree", replace(director, workdir=str(work / "worktrees" / "director-2")))):
            verdict = provenance.judge(role, LEGACY, None, session_dir=own_dir(director))
            check(not verdict.ok and verdict.reason == "workspace", f"the adoption is not {name}'s: {verdict}")
        verdict = provenance.judge(replace(director, workdir_peers=("ops",)), LEGACY, None, session_dir=own_dir(director))
        check(not verdict.ok and verdict.reason == "shared-worktree",
              f"an adopted conversation in a worktree shared with another role still fails closed: {verdict}")
        (shared / provenance.OWNERS_DIR / LEGACY).write_bytes(claimed)
        seed(director, OWN)
        moved = start(director)
        check(moved["resumed"] == [OWN], f"a Director that moved on is not pulled back to the adoption: {moved}")
        verdict = provenance.judge(director, LEGACY, None, session_dir=own_dir(director))
        check(verdict.ok and verdict.proof == "adopted", f"judge says adopted, distinctly: {verdict}")
        converse(LEGACY, str(work / "elsewhere"))
        check(not provenance.judge(director, LEGACY, None, session_dir=own_dir(director)).ok,
              "a conversation the provider now files elsewhere than the adopted workspace is refused")
        converse(LEGACY, str(repo))

        # 7. Rollback: a preview, then root's record alone goes, through the privileged action.
        code, lines = command(role="ops", rollback=True)
        check(code == 1 and "adopts it for pgu's director, not pgu's ops" in "\n".join(lines),
              f"another role cannot roll back the control role's adoption: {lines}")
        code, lines = command(rollback=True)
        check(code == 0 and "root removes " in "\n".join(lines) and digest(lines), f"rollback previews: {lines}")
        back = digest(lines)
        before = tree()
        asked.clear()
        code, lines = command(rollback=True, confirm=back)
        changed = {k for k in set(tree()) | set(before) if tree().get(k) != before.get(k)}
        check(code == 0 and not record.exists() and changed == {str(record.relative_to(work))}
              and asked == [("pgu", "withdraw-conversation-adoption", {"role": "director", "conversation": LEGACY})],
              f"rollback asks root to remove its record, and nothing else changes: {changed} {asked} {lines}")
        check(legacy_rollout.is_file(), "the conversation is kept")
        seed(director, LEGACY)
        refused = start(director)
        check(refused["resumed"] == [] and "foreign_workspace" in refused["events"],
              f"after rollback the legacy conversation is refused again: {refused}")
        check(adoption.status_lines(config) == [], "and status says nothing is adopted")
        # Root records only the confirmed, clean plan that was asked for.
        plan = tenant_plan("pgu", "director", LEGACY)
        for name, args, spoiled in (
                ("another digest", ("pgu", "director", LEGACY, "0" * 64), plan),
                ("a plan with problems", ("pgu", "director", LEGACY, plan["digest"]), {**plan, "problems": ["no"]}),
                ("another role's plan", ("pgu", "ops", LEGACY, plan["digest"]), plan),
                ("another project's plan", ("other", "director", LEGACY, plan["digest"]), plan),
                ("another conversation's plan", ("pgu", "director", OTHER, plan["digest"]), plan),
                ("something that is no plan", ("pgu", "director", LEGACY, plan["digest"]), {**plan, "schema": "x"})):
            try:
                root_record.record_from_plan(*args, plan=spoiled)
                recorded = True
            except root_record.RecordError:
                recorded = False
            check(not recorded and not record.exists(), f"root records nothing for {name}")
        root_record.write({**forged, "project": "other"})
        owned = shared / provenance.OWNERS_DIR / LEGACY
        claimed_by = owned.read_bytes() if owned.exists() else None
        owned.unlink(missing_ok=True)  # so only the project can stop the install
        check(not provenance.judge(director, LEGACY, None, session_dir=own_dir(director)).ok
              and adoption.install_adopted_record(director, own_dir(director), "syrd694.test") == "",
              "another project's adoption of the same conversation is no proof for this one, and is not installed")
        if claimed_by is not None:
            owned.write_bytes(claimed_by)
        try:
            root_record.write(forged)
            overwrote = True
        except root_record.RecordError:
            overwrote = False
        check(not overwrote and json.loads(record.read_text())["project"] == "other",
              "root never writes an adoption over an existing one")
        try:
            root_record.remove("pgu", "director", LEGACY)
            removed = True
        except root_record.RecordError:
            removed = False
        check(not removed and record.exists(), "root removes only the named project's adoption")
        record.unlink()
        root_record.write({**forged, "role": "ops"})
        try:
            root_record.remove("pgu", "director", LEGACY)
            removed = True
        except root_record.RecordError:
            removed = False
        check(not removed and record.exists(), "and only the named role's")
        record.unlink()
        (shared / provenance.OWNERS_DIR / LEGACY).unlink()

        # 8. Every unapproved source and state is refused, before anything is asked for.
        def refused_plan(why: str, setup=None, **kwargs) -> None:
            undo = setup() if setup else None
            asked.clear()
            code, lines = command(**kwargs)
            check(code == 1 and not asked and "refused:" in "\n".join(lines) and why in "\n".join(lines),
                  f"refused, {why!r}: {lines}")
            if undo:
                undo()

        shutil.rmtree(own_dir(director))
        refused_plan("is ops's worktree", lambda: converse(LEGACY, ops.workdir) and None)
        refused_plan("is neither pgu's project repository", lambda: converse(LEGACY, str(work / "anywhere")) and None)
        refused_plan("records no workspace", lambda: converse(LEGACY, None) and None)
        refused_plan("it is not an absolute path", lambda: converse(LEGACY, "relative/repo") and None)
        refused_plan("already files it under director's worktree", lambda: converse(LEGACY, director.workdir) and None)
        converse(LEGACY, str(repo))
        refused_plan("holds no conversation", session_id=EARLIER)
        refused_plan("is not a conversation id", session_id="*")
        refused_plan("only pgu's control role (director)", role="ops")
        refused_plan("it is ops's", lambda: owner(LEGACY, "ops") or (lambda: (shared / provenance.OWNERS_DIR / LEGACY).unlink()))
        refused_plan("another role recorded it", lambda: seed(ops, LEGACY) or (lambda: shutil.rmtree(own_dir(ops))))
        refused_plan("it is live: director's pane", lambda: seed(director, LEGACY) or (lambda: shutil.rmtree(own_dir(director))),
                     runner=Tmux({director.tmux_session}))
        process(4100, ["python3", "switchyard", "adopt-conversation", "pgu", "director", LEGACY, "--confirm", "x"])
        code, lines = command()
        check(code == 0, f"the command above this one, which names the id, is not the conversation being live: {lines}")
        process(5000, ["codex", "resume", LEGACY])
        refused_plan("it is live: process 5000")
        shutil.rmtree(proc / "5000")
        converse(OTHER, str(repo))
        refused_plan("director has adopted", lambda: root_record.write({**forged, "session_id": OTHER}) and None)
        (root_record.ROOT / f"{OTHER}.json").unlink()

        # 9. An earlier worktree of the Director's is approved only by a conversation the Director owns there.
        earlier = work / "old-worktrees" / "director"
        converse(LEGACY, str(earlier))
        converse(EARLIER, str(earlier))
        refused_plan("nor a worktree any conversation of director's shows it ran in")
        owner(EARLIER, "director", how="adopted")
        refused_plan("nor a worktree any conversation of director's shows it ran in")  # an adoption proves nothing
        owner(EARLIER, "ops")
        refused_plan("ops ran there")
        owner(EARLIER, "director", how="workspace")
        code, lines = command()
        check(code == 0 and f"an earlier worktree of director: director's own conversation {EARLIER}" in "\n".join(lines),
              f"a conversation the Director owns there proves the earlier worktree: {lines}")

        # 10. Audit's fourth probe: the control pane is proven only against the board root's unit names, over a
        #     connection the kernel shows that board's own process holds -- never a URL from the tenant's config.
        import http.server
        import socketserver
        import threading

        from scripts.ticket_board import board_identity

        board_identity.ANCHOR, board_identity.TRUSTED_OWNER_UID = rootfs, os.getuid()
        board_identity.UNIT_DIR = rootfs / "etc" / "systemd" / "system"
        board_identity.UNIT_DIR.mkdir(parents=True, mode=0o755)
        run = rootfs / "run" / "pgu-ticket-board"
        run.mkdir(parents=True, mode=0o755)
        socket_path = run / "ticket-board.sock"
        unit = board_identity.UNIT_DIR / "pgu-ticket-board.service"
        good_unit = (f"[Service]\nUser={getpass.getuser()}\nExecStart=/usr/bin/python3 /x/ticket-board.py --host "
                     f"127.0.0.1 --port 1 --unix-socket {socket_path} --frames /f\n")
        unit.write_text(good_unit)
        director_pane, ops_pane = (4100, 20, os.getuid()), (4200, 21, os.getuid())
        panes = work / "panes-proc"  # both panes live, as /proc shows a process's start time
        for pid, start_time, _uid in (director_pane, ops_pane):
            (panes / str(pid)).mkdir(parents=True)
            (panes / str(pid) / "stat").write_text(f"{pid} (codex) S 1 " + "0 " * 17 + f"{start_time} 0\n")
        answers = {"/api/workflow": {"document": {"roles": [
                       {"name": "director", "active": True, "capabilities": ["set_manually_controlled", "merge"]}]}},
                   "/api/runtime-assignments/director": {"authority_mode": "process", "assignment": dict(zip(
                       ("process_pid", "process_start_time", "process_uid"), director_pane))}}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(answers.get(self.path)).encode()
                self.send_response(200 if self.path in answers else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = socketserver.UnixStreamServer(str(socket_path), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            check(board_identity.unit_endpoint("pgu") == (str(socket_path), getpass.getuser()),
                  "the board's socket and account are root's unit's")
            proven = board_identity.board_get_for("pgu", main_pid=lambda _unit: os.getpid())
            check(proven("ignored", "/api/workflow") == answers["/api/workflow"],
                  "the board is asked over a connection its own process holds, whatever URL the caller names")
            try:
                proven("", "/../etc/shadow")
                asked_outside = True
            except board_identity.BoardIdentityError:
                asked_outside = False
            check(not asked_outside, "the proven board is asked for its API and nothing else")
            for name, kwargs in (("another account", dict(service_uid=lambda _user: os.getuid() + 1)),
                                 ("another process than the unit's", dict(main_pid=lambda _unit: os.getpid() + 1)),
                                 ("a unit that is not running", dict(main_pid=lambda _unit: 0))):
                try:
                    board_identity.board_get_for("pgu", **{"main_pid": lambda _unit: os.getpid(), **kwargs})(
                        "", "/api/workflow")
                    answered = True
                except board_identity.BoardIdentityError:
                    answered = False
                check(not answered, f"a socket answered by {name} is not the board")
            for name, text, spoil in (
                    ("writable by others", good_unit, lambda: os.chmod(unit, 0o666)),
                    ("in a directory others can write", good_unit, lambda: os.chmod(board_identity.UNIT_DIR, 0o777)),
                    ("naming no socket", good_unit.replace(f"--unix-socket {socket_path} ", ""), None),
                    ("naming two sockets", good_unit.replace("--frames", f"--unix-socket {socket_path} --frames"), None),
                    ("naming root as its account", good_unit.replace(f"User={getpass.getuser()}", "User=root"), None)):
                unit.write_text(text)
                if spoil:
                    spoil()
                try:
                    board_identity.unit_endpoint("pgu")
                    trusted = True
                except board_identity.BoardIdentityError:
                    trusted = False
                check(not trusted, f"a board unit {name} is not root's word")
                os.chmod(unit, 0o644)
                os.chmod(board_identity.UNIT_DIR, 0o755)
            unit.write_text(good_unit)
            board_identity.TRUSTED_OWNER_UID = 0  # the unit, and the tree above it, are this account's, not root's
            try:
                board_identity.unit_endpoint("pgu", anchor=board_identity.UNIT_DIR)
                trusted = True
            except board_identity.BoardIdentityError:
                trusted = False
            board_identity.TRUSTED_OWNER_UID = os.getuid()
            check(not trusted, "a board unit owned by anybody but root is not root's word")
            unit.rename(work / "real.service")
            os.symlink(work / "real.service", unit)
            try:
                board_identity.unit_endpoint("pgu")
                trusted = True
            except board_identity.BoardIdentityError:
                trusted = False
            check(not trusted, "a board unit that is a link is not root's word")
            unit.unlink()
            (work / "real.service").rename(unit)

            # The probe: the tenant's configuration names a board that declares Ops the control role, with Ops's pane.
            def forged_url(_project):
                raise AssertionError("the tenant's configuration was consulted for an adoption")

            for name in ("adopt-conversation", "withdraw-conversation-adoption"):
                board_get, _where = privileged_helper.authorizing_board(
                    privileged_actions.action_for(name), "pgu", url_for=forged_url,
                    board_get_for=lambda project: board_identity.board_get_for(project, main_pid=lambda _u: os.getpid()))
                for pane, expected in ((ops_pane, None), (director_pane, "director")):
                    caller = privileged_helper.CallerIdentity(*pane)
                    try:
                        got = privileged_helper.require_registered_control_caller(
                            "pgu", board_get=board_get, board_url="http://forged.invalid", caller=caller,
                            proc_root=panes)
                    except privileged_helper.Refused:
                        got = None
                    check(got == expected, f"{name}: the pane {pane} is proven against root's board alone: {got}")
            deploy_get, deploy_url = privileged_helper.authorizing_board(
                privileged_actions.action_for("deploy-release"), "pgu", url_for=lambda project: f"url-of-{project}")
            check(deploy_url == "url-of-pgu" and deploy_get is privileged_helper._board_get,
                  "an action that mints no proof asks as it always has")
            try:
                privileged_helper.authorizing_board(privileged_actions.action_for("adopt-conversation"), "nobody",
                                                    url_for=forged_url)
                found = True
            except privileged_helper.Refused:
                found = False
            check(not found, "a project root has no board unit for has no board to prove anything against")
        finally:
            server.shutdown()
            server.server_close()

        # 11. The command is reached through the dispatcher, with its arguments.
        seen = {}
        saved = (launcher._resolve_switchyard_project, launcher._load_switchyard_project_config_for_command,
                 launcher.adopt_conversation_command)
        launcher._resolve_switchyard_project = lambda project: type("E", (), {"config_path": config_path, "slug": project})()
        launcher._load_switchyard_project_config_for_command = lambda entry, argv: config
        launcher.adopt_conversation_command = lambda cfg, **kw: seen.update(kw) or 7
        try:
            code = launcher.switchyard_main(["adopt-conversation", "pgu", "director", LEGACY, "--rollback",
                                             "--confirm", "ab" * 32])
        finally:
            (launcher._resolve_switchyard_project, launcher._load_switchyard_project_config_for_command,
             launcher.adopt_conversation_command) = saved
        check(code == 7 and seen == {"config_path": config_path, "role": "director", "session_id": LEGACY,
                                     "rollback": True, "confirm": "ab" * 32, "plan_json": False},
              f"`switchyard adopt-conversation` dispatches with every argument: {seen}")
        wrapper = subprocess.run([sys.executable, str(ROOT / "scripts" / "switchyard"), "conversation-adoption-record",
                                  "withdraw", "pgu", "director", LEGACY], capture_output=True, text=True, timeout=60)
        check(wrapper.returncode == 1 and "root's" in wrapper.stderr,
              f"the wrapper hands root's operation straight to its module, which refuses a non-root caller: {wrapper}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"conversation_adoption_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
