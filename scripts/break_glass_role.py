"""`switchyard break-glass`: a temporary one-operation role for a provider safety hold (SYRD-739).

A safety refusal holds a provider account (SYRD-672). When the work it refused must be isolated now, and the Director
that would isolate it is held, the operator starts a temporary role on another provider. It makes the one board
mutation that is needed, then hands control back. Its only authority is a board grant
(`scripts/ticket_board/break_glass.py`, pgu1040):
- a random token whose sha256 alone the board keeps;
- a fixed set of operations, chosen here with --allow;
- a deadline by the database clock;
- ONE operation, all enforced by the board.

Who: root, elevated by pkexec for a named person, as `rebind-director` and `rebind-workflow-panes`. The tenant
configuration is root's verified one. The board is read over root's kernel-proven socket and written through the
database owner.

Preview (read-only, nothing to the held account):
- the SYRD-737 recovery packet for the hold, validated and verified;
- the runtime: not under a hold, installed and signed in for the owner, and one whose fresh session takes its first
  instruction on the command line (claude, codex);
- the operations and the time it may take.
It prints a review digest; --apply --expect DIGEST does exactly that.

Apply:
1. the board opens the grant;
2. a fresh scratch directory is built, owned by the project account:
   - work/ holds only the validated packet and the instructions;
   - home/ holds only the chosen runtime's own credential file (role_credentials' allowlist) and the entries that let
     it open at a prompt in work/;
   - the token is in a 0600 file beside them.
   So it has no Director session or worktree, no other provider's credentials, no refused content, and no root. It
   runs as the project account, with a cleared environment;
3. `<project>-break-glass` is started on that runtime, with one fixed first instruction;
4. it waits until the grant ends (an operation completed, refused or aborted) or its time runs out, or the session
   ends by itself; Ctrl-C aborts;
5. whatever the outcome: the session is stopped, the token deleted, and the grant ended on the board (expired or
   aborted if it had not ended);
6. hand-back: to the Director only when its account is no longer held, as one fixed notice. Otherwise the hold is
   left, nothing goes to the held account, and the operator is given the full replacement command
   (`switchyard rebind-director`).

Every step is in root's rollout journal and in the grant's board record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from scripts import director_rebind as dr

#: Runtimes whose fresh interactive session takes its first instruction as a command-line argument.
FIRST_INSTRUCTION = {"claude": lambda cli, text: [cli, text], "codex": lambda cli, text: [cli, text]}
SCHEMA = "switchyard.break-glass.v1"
POLL_SECONDS = 2.0
GRACE_SECONDS = 5.0
TERMINAL = ("completed", "refused", "expired", "aborted", "ended")
#: Where things are inside the role's sandbox. Nothing else of the host's filesystem is there but the read-only
#: system directories and the CLI's own install.
SANDBOX_HOME = "/home/break-glass"
SANDBOX_WORK = "/work"
SANDBOX_RUN = "/run/break-glass"
SANDBOX_TOKEN = SANDBOX_RUN + "/token"
SANDBOX_CLIENT = SANDBOX_RUN + "/break_glass.py"
#: Read-only in the sandbox, at their own paths; anything installed under them needs no bind of its own.
SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc", "/opt")


class Refused(Exception):
    """Nothing was changed."""


def first_instruction(grant: int, hold: int, operations: list[str]) -> str:
    return (f"You are a temporary break-glass role for this project's board (grant {grant}, provider safety hold "
            f"{hold}). Read INSTRUCTIONS.md and recovery-packet.json in this directory, and nothing else. You may "
            f"make exactly ONE board operation ({', '.join(operations)}, or abort) with the command INSTRUCTIONS.md "
            "gives; the board allows no second one, and this session is ended after it. Do not run anything else.")


def instructions(grant: int, hold: int, operations: list[str], client: str, expires: str) -> str:
    lines = [f"# Break-glass grant {grant}", "",
             f"Provider safety hold {hold} stands. The board's recovery packet is `recovery-packet.json` here: the "
             "hold, the roles and tickets it affects, and the recovery choices. It holds no ticket text.", "",
             "You may make ONE board operation. The board enforces this: after one operation, a refusal, an abort, "
             f"or {expires}, the grant has ended and this session is stopped.", "", "Allowed, one of:"]
    examples = {"record": "record --ticket PREFIX-N --text 'what the board should know'",
                "isolate": "isolate --ticket PREFIX-N",
                "reassign": "reassign --ticket PREFIX-N --assignee ROLE --reason 'why'",
                "unblock": "unblock --ticket PREFIX-N --blocker BLOCKER"}
    for operation in operations:
        lines.append(f"- `python3 {client} {examples[operation]}`")
    lines += [f"- `python3 {client} abort` -- end the grant without a change.", "",
              "Every mutation passes the board's ordinary rules; a refusal is final for this grant."]
    return "\n".join(lines) + "\n"


def runtime_problems(config: Any, runtime: str, held: set[str], *,
                     runner: Callable[..., subprocess.CompletedProcess[Any]]) -> list[str]:
    from scripts import role_credentials, team_launcher as launcher

    if runtime in held:
        return [f"the {runtime} account is under a safety hold"]
    if runtime not in FIRST_INSTRUCTION:
        return [f"{runtime!r} has no verified way to give a fresh session its first instruction "
                f"({', '.join(sorted(FIRST_INSTRUCTION))} do)"]
    owner = config.run_as_user or launcher.current_user_name()
    status = launcher._cli_auth_status(runtime, owner_user=owner, owner_home=launcher._owner_home_for_auth(owner),
                                       runner=runner)
    if status == "not_installed":
        return [f"{runtime} is not installed for {owner}"]
    if status not in ("authenticated", "unknown"):
        return [f"{runtime} is not signed in for {owner} ({status})"]
    if not role_credentials.ROLE_CREDENTIAL_ARTIFACTS.get(runtime):
        return [f"{runtime} names no credential file to give the role"]
    return []


def _install_root(path: str) -> str | None:
    """What the sandbox binds, read-only, for one installed program: the npm package it belongs to when it sits inside
    a node_modules tree (the directory directly under node_modules/, or node_modules/@scope/), else the program's own
    file and nothing beside it. None when it is under a system directory the sandbox already has. A package.json
    anywhere else -- the one `npm install` leaves in a home, say -- marks nothing, and no program's directory is bound."""
    if any(path == top or path.startswith(top + "/") for top in SYSTEM_DIRS):
        return None
    parts = Path(path).parts
    for index in range(len(parts) - 1, 0, -1):
        if parts[index - 1] == "node_modules":
            if parts[index].startswith("@") and index + 1 < len(parts) - 1:
                package = parts[:index + 2]
            else:
                package = parts[:index + 1]
            return str(Path(*package))
    return path


def _unconfined(binds: list[str], forbidden: list[str]) -> list[str]:
    """The binds that would put any forbidden directory -- the project account's home, /home, / -- in the sandbox."""
    found = []
    for bind in binds:
        for place in forbidden:
            if bind == place or place.startswith(bind.rstrip("/") + "/"):
                found.append(f"{bind} (it holds {place})")
                break
    return found


def resolve_tools(config: Any, runtime: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> dict:
    """bubblewrap and the CLI as the project account's login shell finds them, and the install trees the CLI needs:
    its own and, for a node script, node's. {bwrap, cli, binds, problems}."""
    probe = runner(["bash", "-lc", 'printf "bwrap=%s\\ncli=%s\\nnode=%s\\n" "$(command -v bwrap)" "$(command -v "$1")" '
                    '"$(command -v node)"', "break-glass-probe", runtime],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    found = dict(line.split("=", 1) for line in str(probe.stdout or "").splitlines() if "=" in line)
    problems = []
    if not found.get("bwrap"):
        problems.append("bubblewrap (bwrap) is not installed for the project account; the role runs only in its sandbox")
    if not found.get("cli"):
        problems.append(f"{runtime} is not on the project account's PATH")
    if problems:
        return {"problems": problems}
    cli = os.path.realpath(found["cli"])
    roots = [_install_root(cli)]
    try:
        with open(cli, "rb") as handle:
            first = handle.read(128).split(b"\n", 1)[0]
    except OSError:
        first = b""
    if first.startswith(b"#!") and b"node" in first and found.get("node"):
        roots.append(_install_root(os.path.realpath(found["node"])))
    binds = sorted({r for r in roots if r})
    from scripts import team_launcher as launcher

    owner = (getattr(config, "run_as_user", "") or launcher.current_user_name()) if config is not None else ""
    forbidden = ["/", "/home", *([os.path.realpath(str(launcher._owner_home_for_auth(owner)))] if owner else [])]
    wrong = _unconfined(binds, forbidden)
    if wrong:
        return {"problems": [f"{runtime} is installed in a way the sandbox cannot confine: binding {', '.join(wrong)} "
                             "would put the project account's files in the role's world"]}
    return {"bwrap": found["bwrap"], "cli": cli, "binds": binds, "problems": []}


def plan(config: Any, *, get: Callable[[str], Any], hold: int, runtime: str, allow: list[str], ttl: int,
         runner: Callable[..., subprocess.CompletedProcess[Any]],
         problems_for: Callable[..., list[str]] | None = None,
         tools_for: Callable[..., dict] | None = None) -> dict:
    from scripts.ticket_board import break_glass

    unknown = [o for o in allow if o not in break_glass.OPERATIONS]
    if not allow or unknown:
        raise Refused(f"--allow takes one or more of {', '.join(break_glass.OPERATIONS)}"
                      + (f", not {', '.join(unknown)}" if unknown else ""))
    if not 60 <= ttl <= 3600:
        raise Refused("--ttl is 60 to 3600 seconds")
    try:
        packet = dr.board_facts(get, project=config.project, hold=hold)[0]
    except dr.RebindRefused as exc:
        raise Refused(str(exc)) from None
    held = {packet["hold"]["runtime"], *(h["runtime"] for h in packet["other_holds"])}
    problems = (problems_for or runtime_problems)(config, runtime, held, runner=runner)
    tools = {} if problems else (tools_for or (lambda c, r: resolve_tools(c, r, runner=runner)))(config, runtime)
    problems = [*problems, *tools.get("problems", [])]
    allow = sorted(set(allow))
    prov = packet["provenance"]
    review = {"schema": SCHEMA, "project": config.project, "hold": hold, "runtime": runtime, "allow": allow, "ttl": ttl,
              "state_digest": prov["state_digest"], "tools": {k: tools.get(k) for k in ("bwrap", "cli", "binds")}}
    return {"packet": packet, "hold": hold, "held": sorted(held), "runtime": runtime, "allow": allow, "ttl": ttl,
            "problems": problems, "tools": tools, "director_held": bool(packet["hold"]["director_held"]),
            "provenance": {"schema": packet["schema"], "version": packet["version"], "project": prov["project"],
                           "hold": hold, "generated_at": prov["generated_at"], "db_xmax": prov["db_xmax"],
                           "state_digest": prov["state_digest"], "packet_digest": packet["packet_digest"]},
            "digest": "sha256:" + hashlib.sha256(json.dumps(review, sort_keys=True).encode()).hexdigest()}


# -- the role's scratch ------------------------------------------------------------------------------------------------
def _write(owner: str, path: Path, body: bytes, mode: int) -> None:
    from scripts import owner_writes

    if owner_writes.root_writes_for(owner):
        owner_writes.write_owner_file(owner, path, body, mode=mode)
    else:
        path.write_bytes(body)
        path.chmod(mode)


def _dirs(owner: str, paths: list[Path]) -> None:
    from scripts import owner_writes

    if owner_writes.root_writes_for(owner):
        owner_writes.ensure_owner_dirs(owner, paths, mode=0o700)
    else:
        for path in paths:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)


def build_scratch(config: Any, root: Path, *, grant: int, runtime: str, packet: dict, token: str, allow: list[str],
                  expires: str, client: Path) -> dict[str, Path]:
    """The role's whole world: work/ (packet, instructions), home/ (one credential, its trust), and the token file,
    which the role's pane deletes the moment it has opened it."""
    from scripts import owner_writes, role_credentials, team_launcher as launcher

    owner = config.run_as_user or launcher.current_user_name()
    work, home = root / "work", root / "home"
    _dirs(owner, [root, work, home])
    _write(owner, work / "recovery-packet.json", json.dumps(packet, sort_keys=True, indent=1).encode() + b"\n", 0o600)
    _write(owner, work / "INSTRUCTIONS.md",
           instructions(grant, packet["hold"]["id"], allow, SANDBOX_CLIENT, expires).encode(), 0o600)
    token_file = root / "token"
    _write(owner, token_file, token.encode("ascii") + b"\n", 0o600)
    owner_home = Path(launcher._owner_home_for_auth(owner))
    for artifact in role_credentials.ROLE_CREDENTIAL_ARTIFACTS[runtime]:
        source = owner_home / artifact.relative_path
        raw = owner_writes.read_owner_bytes(owner, source) if owner_writes.root_writes_for(owner) else (
            source.read_bytes() if source.is_file() else None)
        if raw is None:
            if artifact.required:
                raise Refused(f"{runtime}'s credential {artifact.relative_path} is not in {owner}'s home")
            continue
        target = home / artifact.relative_path
        _dirs(owner, [target.parent])
        _write(owner, target, raw, 0o600)
    if runtime == "claude":  # opens at a prompt in work/, not at onboarding or the trust dialog
        _write(owner, home / ".claude.json", json.dumps(
            {"hasCompletedOnboarding": True, "projects": {SANDBOX_WORK: {"hasTrustDialogAccepted": True}}}).encode(), 0o600)
    elif runtime == "codex":
        _dirs(owner, [home / ".codex"])
        _write(owner, home / ".codex" / "config.toml", f'[projects."{SANDBOX_WORK}"]\ntrust_level = "trusted"\n'.encode(),
               0o600)
    return {"root": root, "work": work, "home": home, "token": token_file}


def sandbox_argv(scratch: dict[str, Path], tools: dict, *, runtime: str, board_url: str, client: Path,
                 instruction: str) -> list[str]:
    """bubblewrap, from an empty root: read-only system directories and the CLI's install, fresh /proc, /dev and tmpfs,
    the scratch home read-write at SANDBOX_HOME, its work read-only at SANDBOX_WORK, the client and the token (from fd
    9, on the sandbox's own tmpfs) under SANDBOX_RUN. The project account's home, its state, the Director's worktree
    and transcripts, any other provider's credentials and any other grant's token are not in it. The network is kept,
    for the provider and the board."""
    argv = [tools["bwrap"], "--die-with-parent", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--unshare-cgroup-try", "--ro-bind", "/usr", "/usr"]
    for top in SYSTEM_DIRS[1:]:
        argv += ["--ro-bind-try", top, top]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/var", "--tmpfs", "/var/tmp",
             "--tmpfs", "/run", "--ro-bind-try", "/run/systemd/resolve", "/run/systemd/resolve", "--tmpfs", "/home",
             "--bind", str(scratch["home"]), SANDBOX_HOME, "--ro-bind", str(scratch["work"]), SANDBOX_WORK,
             "--dir", SANDBOX_RUN, "--file", "9", SANDBOX_TOKEN, "--ro-bind", str(client), SANDBOX_CLIENT]
    for root in tools["binds"]:
        argv += ["--ro-bind", root, root]
    argv += ["--clearenv", "--setenv", "HOME", SANDBOX_HOME,
             "--setenv", "PATH", f"{os.path.dirname(tools['cli'])}:/usr/local/bin:/usr/bin:/bin",
             "--setenv", "TERM", "xterm-256color", "--setenv", "LANG", "C.UTF-8",
             "--setenv", "TICKET_BOARD_URL", board_url, "--setenv", "TICKET_BOARD_BREAK_GLASS_TOKEN_FILE", SANDBOX_TOKEN,
             "--chdir", SANDBOX_WORK, "--", *FIRST_INSTRUCTION[runtime](tools["cli"], instruction)]
    return argv


def start_command(session: str, scratch: dict[str, Path], tools: dict, *, runtime: str, board_url: str, client: Path,
                  instruction: str) -> list[str]:
    """tmux, as the project account: open the token file as fd 9, delete it, and exec the sandbox, which copies fd 9
    onto its own tmpfs. From then on the token exists nowhere on disk."""
    return ["tmux", "new-session", "-d", "-s", session, "-x", "200", "-y", "50", "/bin/bash", "-c",
            'exec 9<"$1" && rm -f -- "$1" && exec "${@:2}"', "break-glass", str(scratch["token"]),
            *sandbox_argv(scratch, tools, runtime=runtime, board_url=board_url, client=client, instruction=instruction)]


# -- running it -------------------------------------------------------------------------------------------------------
def grant_state(get: Callable[[str], Any], grant: int) -> dict:
    from scripts.ticket_board import break_glass

    for row in (get(break_glass.PATH) or {}).get("grants") or []:
        if row.get("grant") == grant:
            return row
    return {}


def run(config: Any, p: dict, *, operator: str, reason: str, admin: Any, get: Callable[[str], Any],
        tmux_runner: Callable[..., subprocess.CompletedProcess[Any]], scratch_root: Path, board_url: str,
        client: Path, journal: Callable[[dict], None] = lambda _e: None, print_func: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32)) -> int:
    from scripts.ticket_board import break_glass

    session = f"{config.project}-break-glass"
    token = token_factory()
    try:
        grant = int(admin.call("ticket_board.open_break_glass", (p["hold"], "bigint"), (operator, "text"),
                               (reason, "text"), (p["runtime"], "text"), ("{" + ",".join(p["allow"]) + "}", "text[]"),
                               (p["ttl"], "integer"), (break_glass.digest(token), "text"), (p["provenance"], "jsonb")))
    except Exception as exc:  # noqa: BLE001 - refused before anything was made
        print_func(f"switchyard: the board refused the grant: {exc}. Nothing was changed.")
        return 1
    journal({"opened": grant})
    deadline = monotonic() + p["ttl"] + GRACE_SECONDS
    scratch: dict[str, Path] = {}
    outcome, by = "aborted", "operator"
    def stop(_signum: int, _frame: Any) -> None:  # SIGTERM aborts as Ctrl-C does
        raise KeyboardInterrupt
    previous = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, stop)
    except ValueError:  # not the main thread (a test): Ctrl-C still aborts
        previous = None
    try:
        scratch = build_scratch(config, scratch_root / f"break-glass-{grant}", grant=grant, runtime=p["runtime"],
                                packet=p["packet"], token=token, allow=p["allow"],
                                expires=f"{p['ttl']} seconds after it opened", client=client)
        started = tmux_runner(start_command(session, scratch, p["tools"], runtime=p["runtime"], board_url=board_url,
                                            client=client, instruction=first_instruction(grant, p["hold"], p["allow"])))
        if started.returncode != 0:
            raise RuntimeError(f"the break-glass session did not start (exit {started.returncode})")
        journal({"started": session, "runtime": p["runtime"]})
        print_func(f"switchyard: break-glass grant {grant} is open on {p['runtime']} for {p['ttl']} s, in tmux "
                   f"session {session} of the project account. Ctrl-C aborts.")
        outcome, by = "expired", "expiry"
        while monotonic() < deadline:
            state = grant_state(get, grant).get("state")
            if state in TERMINAL:
                outcome, by = state, "board"
                break
            if tmux_runner(["tmux", "has-session", "-t", session], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL).returncode != 0:
                outcome, by = "aborted", "session_ended"
                break
            sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        outcome, by = "aborted", "operator"
    except Exception as exc:  # noqa: BLE001 - the grant is still ended below
        print_func(f"switchyard: break-glass failed: {exc}")
        outcome, by = "aborted", "operator"
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        stopped = tmux_runner(["tmux", "kill-session", "-t", session], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode
        # The whole scratch goes with the role, as the project account: the credential copy, the packet copy and
        # the token (already gone from disk if the pane started).
        root = scratch_root / f"break-glass-{grant}"
        removed = tmux_runner(["rm", "-rf", "--", str(root)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if removed.returncode != 0 or root.exists():
            print_func(f"switchyard: could not remove {root} (its credential copy is still there): remove it by hand")
        ended = admin.call("ticket_board.end_break_glass", (grant, "bigint"),
                           ("expired" if outcome == "expired" else "aborted", "text"), (by, "text")) or {}
        journal({"ended": ended, "session_stopped": stopped in (0, 1), "scratch_removed": not root.exists()})
    handback = admin.call("ticket_board.hand_back_break_glass", (grant, "bigint")) or {}
    journal({"handback": handback})
    attempted = ended.get("attempted") or {}
    print_func(f"switchyard: break-glass grant {grant} {ended.get('state')}: "
               + (f"{attempted.get('operation')} {json.dumps(attempted.get('args') or {}, sort_keys=True)} -> "
                  f"{json.dumps(ended.get('completed') or {}, sort_keys=True)[:400]}" if attempted else "no operation")
               + f". Its session is stopped and its scratch, credential copy and token included, removed.")
    if handback.get("handed_back"):
        print_func(f"switchyard: handed back: the Director's account is not held, and it is told once"
                   + (f" on {handback.get('ticket')}." if handback.get("notice") else "; no ticket could carry it."))
    else:
        print_func(f"switchyard: not handed back: the Director's account is still held (hold "
                   f"{handback.get('director_hold')}); nothing was sent to it. To replace the Director: pkexec "
                   f"switchyard rebind-director {config.project} --cli <runtime> --hold {handback.get('director_hold')} "
                   "--reason-file PATH")
    return 0 if ended.get("state") == "completed" else 1


# -- the command ------------------------------------------------------------------------------------------------------
def command(slug: str, *, cli: str, hold: int, allow: list[str], reason: str, ttl: int = 900, apply_it: bool = False,
            expect: str = "", euid_getter: Callable[[], int] = os.geteuid,
            operator_resolver: Callable[[], Any] | None = None, tenant_resolver: Callable[..., Any] | None = None,
            board_get: Callable[[str], Any] | None = None, admin: Any = None,
            runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
            problems_for: Callable[..., list[str]] | None = None, tools_for: Callable[..., dict] | None = None,
            print_func: Callable[[str], None] = print, **run_seams: Any) -> int:
    from scripts import team_launcher as launcher

    slug = launcher._validate_project_slug(slug)
    again = (f"pkexec switchyard break-glass {slug} --cli {cli} --hold {hold} --allow {','.join(sorted(set(allow)))} "
             f"--ttl {ttl}")
    if euid_getter() != 0:
        print_func(f"switchyard: a break-glass role is root's to start. Run: {again} --reason-file PATH")
        return 1
    from scripts.ticket_board.rollout_journal import Attempt, resolve_operator

    operator = (operator_resolver or resolve_operator)()
    if getattr(operator, "source", "") != "pkexec" or not getattr(operator, "known", False):
        print_func("switchyard: a break-glass role is an operator's decision and has to be authorized as one. "
                   f"This run was elevated by {getattr(operator, 'source', None) or 'nothing that names a person'}. "
                   f"Run: {again} --reason-file PATH")
        return 1
    if not reason.strip():
        print_func("switchyard: a break-glass role needs a reason (--reason-file PATH). Nothing was changed.")
        return 1
    from scripts.pane_rebind import root_verified_tenant

    resolved = (tenant_resolver or root_verified_tenant)(slug, print_func=print_func)
    if resolved is None:
        print_func(f"switchyard: refusing a break-glass role for {slug}. Nothing was changed.")
        return 1
    tenant, config_path, config = resolved
    if board_get is None:
        from scripts.ticket_board import board_identity

        proven = board_identity.board_get_for(slug)
        board_get = lambda path: proven("", path)  # noqa: E731 - root's unit names the board
    from scripts.process_inspection import role_process_runner_for

    owner_runner = run_seams.pop("tmux_runner", None) or role_process_runner_for(config, dr._director(config),
                                                                                  runner=runner)
    try:
        p = plan(config, get=board_get, hold=hold, runtime=cli, allow=allow, ttl=ttl, runner=runner,
                 problems_for=problems_for,
                 tools_for=tools_for or (lambda c, r: resolve_tools(c, r, runner=owner_runner)))
    except Refused as exc:
        print_func(f"switchyard: {exc}. Nothing was changed.")
        return 1
    prov = p["provenance"]
    print_func(f"switchyard: {slug} break-glass, for safety hold {hold} (held: {', '.join(p['held'])})")
    print_func(f"  packet           {prov['schema']} v{prov['version']}, generated {prov['generated_at']}, "
               f"{prov['state_digest']}")
    print_func(f"  role             {cli}, fresh, in a sandbox holding only its own credential, the packet and its "
               f"instructions; one operation of {', '.join(p['allow'])} (or abort), within {ttl} s")
    if p["tools"].get("cli"):
        print_func(f"  sandbox          {p['tools']['bwrap']}; {p['tools']['cli']}"
                   + (f"; its install {', '.join(p['tools']['binds'])} read-only" if p["tools"]["binds"] else ""))
    print_func(f"  hand-back        " + ("not while the Director's account is held: the operator is routed to "
                                       "rebind-director" if p["director_held"] else "to the Director, one notice"))
    for line in p["problems"]:
        print_func(f"  problem          {line}")
    print_func(f"  review           digest {p['digest']}")
    if p["problems"]:
        print_func(f"switchyard: {cli} cannot take a break-glass role: {'; '.join(p['problems'])}. Nothing was changed.")
        return 1
    if not apply_it:
        print_func(f"switchyard: preview; nothing was written. Apply exactly this with: {again} --reason-file PATH "
                   f"--apply --expect {p['digest']}")
        return 0
    if expect != p["digest"]:
        print_func(f"switchyard: --expect {expect or '(none)'} is not what this would do now ({p['digest']}). Review "
                   "the preview and apply the digest it shows. Nothing was changed.")
        return 1
    attempt = Attempt(slug, ["switchyard", "break-glass", slug, f"--cli={cli}", f"--hold={hold}",
                             f"--allow={','.join(p['allow'])}", f"--ttl={ttl}", "--apply", "--expect", expect],
                      operator=operator.name)
    attempt.operator = operator
    attempt.open()
    attempt.write("stdout", json.dumps({"schema": SCHEMA, "plan": {k: p[k] for k in ("hold", "runtime", "allow", "ttl",
                                                                                       "provenance", "digest")},
                                        "reason": reason}, sort_keys=True) + "\n")
    status = run(config, p, operator=operator.name, reason=reason,
                 admin=admin or dr.PsqlAdmin(tenant.admin_database_url, runner=runner), get=board_get,
                 tmux_runner=owner_runner,
                 scratch_root=run_seams.pop("scratch_root", None) or launcher.default_layout_output_path(
                     config, config_path=Path(config_path)).parent,
                 board_url=run_seams.pop("board_url", None) or str(config.board_url),
                 client=run_seams.pop("client", None) or Path(__file__).resolve().parent / "ticket_board" / "break_glass.py",
                 journal=lambda event: attempt.write("stdout", json.dumps(event, sort_keys=True, default=str) + "\n"),
                 print_func=print_func, **run_seams)
    attempt.close(status="succeeded" if status == 0 else "failed", exit_status=status)
    return status


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(
        prog="switchyard break-glass",
        description="Start a temporary role on another provider for a provider safety hold. It may make one board "
                    "operation, enforced by the board, and is then ended. Operator only, through pkexec. Previews by "
                    "default; --apply --expect DIGEST does exactly what the preview showed.")
    out.add_argument("project")
    out.add_argument("--cli", required=True, help="the runtime the temporary role runs (claude or codex)")
    out.add_argument("--hold", required=True, type=int, help="the safety hold this answers")
    out.add_argument("--allow", required=True, help="the operations it may choose from: record,isolate,reassign,unblock")
    out.add_argument("--ttl", type=int, default=900, help="seconds the grant lasts (60-3600, default 900)")
    out.add_argument("--reason-file", required=True, type=Path)
    out.add_argument("--apply", action="store_true")
    out.add_argument("--expect", default="")
    return out


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    try:
        reason = args.reason_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"switchyard: cannot read {args.reason_file}: {exc.strerror}. Nothing was changed.")
        return 1
    return command(args.project, cli=args.cli, hold=args.hold, allow=[a.strip() for a in args.allow.split(",") if a.strip()],
                   reason=reason, ttl=args.ttl, apply_it=args.apply, expect=args.expect)
