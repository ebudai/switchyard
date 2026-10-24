#!/usr/bin/env python3
"""SYRD-671: `switchyard release-status <slug>` runs as printed through the installed trampoline; `--close` stays root's.

For a tenant whose configuration the caller cannot read, `release-status`
(SYRD-659) ends by naming two commands that compare everything:
`pkexec switchyard release-status pie`, or `switchyard release-status pie` as
pie-agent. The second never ran: `/usr/local/bin/switchyard` asked the target to
classify the invocation, the target called every `release-status` privileged,
and the owner -- with no sudo -- was told "command requires root". Another
tenant's account was refused the same way, and never saw the root-records view
at all.

Here, over SYRD-659's namespace (real uids, the tenant's 0700 home and its
generated configuration, root's registry entry, unit and rollout record, a board
on the unit's port), the trampoline is rendered from the real generator,
through its own unquoted heredoc, and installed as `/usr/local/bin/switchyard`
in front of the real target. `sudo` is a stub that records every attempt and
grants nothing. Each caller is a child that has become its uid. The
classification is also asked of the real target directly, for every spelling of
`--close` its parser takes. The before-run is main's code, from a git archive.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "9c576705b8456f0976df015d321a49d78f1b3f59"  # main before SYRD-671
CHECKS = 0

#: Every spelling `release-status`'s own parser reads as a close, and some it reads as a plain read.
CLOSES = (["pie", "--close"], ["--close", "pie"], ["pie", "--clos"], ["pie", "--c"], ["PIE", "--close"])
READS = (["pie"], ["pie", "-h"], ["pie", "--help"], [], ["pie", "--close=yes"], ["pie", "extra"])


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def classify(tree: Path, arguments: list[str]) -> str:
    """The real target's answer to the trampoline's question, as the trampoline asks it."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX", "SWITCHYARD_"))}
    done = subprocess.run([sys.executable, str(tree / "scripts" / "switchyard"), "--switchyard-wrapper-requires-root",
                           "release-status", *arguments], capture_output=True, text=True,
                          env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=120, stdin=subprocess.DEVNULL)
    return done.stdout.strip() if done.returncode == 0 else f"failed {done.returncode}: {done.stderr[-300:]}"


def render_trampoline(target: Path, into: Path) -> None:
    """`/usr/local/bin/switchyard` as install-switchyard writes it: its heredoc, rendered by bash."""
    lines = (Path(target).parents[1] / "scripts" / "install-switchyard").read_text(encoding="utf-8").splitlines()
    opening = next(i for i, line in enumerate(lines) if line.strip() == 'cat >"$tmp" <<EOF')
    closing = next(i for i in range(opening + 1, len(lines)) if lines[i].strip() == "EOF")
    body = "\n".join(lines[opening + 1:closing])
    script = into.with_name("render.sh")
    script.write_text("#!/usr/bin/env bash\nset -euo pipefail\nquote() { printf '%q' \"$1\"; }\n"
                      f"default_target={target}\nhelp_text=HELP\nversion_text=VERSION\nrecovery_command=RECOVERY\n"
                      f"cat <<EOF\n{body}\nEOF\n", encoding="utf-8")
    rendered = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=True).stdout
    into.write_text(rendered, encoding="utf-8")
    into.chmod(0o755)
    script.unlink()


def run_as(uid: int, home: str, argv: list[str]) -> dict:
    """`argv` executed as is, in a child that has become `uid`, with a rebuilt environment and no terminal."""
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the caller's child
        os.close(reader)
        os.dup2(writer, 1)
        os.dup2(writer, 2)
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        try:
            if uid:
                os.setgroups([uid])
                os.setresgid(uid, uid, uid)
                os.setresuid(uid, uid, uid)
            os.chdir("/")
            os.execve(argv[0], argv, {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/bin:/bin", "HOME": home,
                                      "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"})
        except BaseException as exc:  # noqa: BLE001
            os.write(2, f"could not become {uid}: {exc!r}".encode())
        os._exit(126)
    os.close(writer)
    data = b""
    while chunk := os.read(reader, 65536):
        data += chunk
    os.close(reader)
    _, status = os.waitpid(pid, 0)
    return {"code": os.waitstatus_to_exitcode(status), "said": data.decode("utf-8", "replace")}


def inner(root: Path, work: Path) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import release_status_cross_account_test as base

    # SYRD-659's host, whole: its own cases run first and leave the tenant, root's records and the accounts in place.
    setup = base.inner(root, work)
    release = Path("/opt/sw")
    target = release / "scripts" / "switchyard"
    subprocess.run(["mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", "/usr/local/bin"], check=True)
    render_trampoline(target, Path("/usr/local/bin/switchyard"))
    log = Path("/var/tmp/sudo-log")
    log.mkdir(mode=0o1777)
    log.chmod(0o1777)
    (log / "calls").touch()
    (log / "calls").chmod(0o666)  # every caller's attempt lands in it, whoever is first
    Path("/usr/local/sbin/sudo").write_text(
        '#!/bin/sh\necho "$(id -un): sudo $*" >> /var/tmp/sudo-log/calls\nexit 1\n', encoding="utf-8")
    Path("/usr/local/sbin/sudo").chmod(0o755)
    import http.server

    from scripts.ticket_board import rollout_journal

    server = http.server.ThreadingHTTPServer(("127.0.0.1", setup["port"]), base.Board)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    journal = rollout_journal.index_path(base.SLUG)
    callers = {"owner": (base.OWNER_UID, str(base.HOME)), "director": (base.DIRECTOR_UID, f"/home/{base.DIRECTOR}"),
               "root": (0, "/run/root-home")}
    trampoline = ["/usr/local/bin/switchyard", "release-status"]
    out: dict = {"config_path": setup["config_path"], "journal_before": journal.read_text(encoding="utf-8")}
    for name, (uid, home) in callers.items():
        out[name] = run_as(uid, home, [*trampoline, base.SLUG])
    # The remedy the other account was given, run exactly as printed, as the account it names.
    named = re.search(r"or `(switchyard release-status [^`]+)` as (\S+)\.", out["director"]["said"])
    out["remedy"] = named.groups() if named else None
    if named:
        out["remedy_run"] = run_as(base.OWNER_UID, str(base.HOME), ["/usr/local/bin/" + named.group(1).split()[0],
                                                                     *named.group(1).split()[1:]])
    for name in ("owner", "director"):
        uid, home = callers[name]
        out[f"{name}_closes"] = [run_as(uid, home, [*trampoline, *spelling]) for spelling in CLOSES]
    out["sudo_calls"] = (log / "calls").read_text(encoding="utf-8").splitlines()
    out["journal_after_callers"] = journal.read_text(encoding="utf-8")
    out["root_close"] = run_as(0, "/run/root-home", [*trampoline, base.SLUG, "--close"])
    server.shutdown()
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "skills",
                              "docs", "tests"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd671.") as tmp:
        stage = Path(tmp)
        os.chmod(stage, 0o755)
        shutil.copy2(Path(__file__), stage / "suite.py")
        shutil.copy2(ROOT / "tests" / "release_status_cross_account_test.py", stage / "release_status_cross_account_test.py")
        code = stage / "tree"
        for part in ("scripts", "config", "examples", "skills", "docs"):
            if (tree / part).exists():
                shutil.copytree(tree / part, code / part, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run(["chmod", "-R", "a+rX", str(stage)], check=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX",
                                                                         "SWITCHYARD_"))}
        child = subprocess.run(["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private",
                                "--pid", "--fork", "--mount-proc", "--net", sys.executable, str(stage / "suite.py"),
                                "--inner", str(code), tmp], capture_output=True, text=True,
                               env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900, stdin=subprocess.DEVNULL)
        assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


REFUSED = "switchyard: command requires root: switchyard release-status"


def before(tree: Path, out: dict) -> None:
    check(all(classify(tree, spelling) == "requires-root" for spelling in [["pie"], ["pie", "extra"]]),
          "before: the target called every release-status privileged, the read included")
    for name in ("owner", "director"):
        check(out[name]["code"] == 1 and REFUSED in out[name]["said"],
              f"before: {name}, with no sudo, was refused the read by the trampoline: {out[name]}")


def candidate(out: dict) -> None:
    for spelling in CLOSES:
        check(classify(ROOT, spelling) == "requires-root",
              f"every spelling of --close the command's own parser takes is root's: {spelling}")
    for spelling in READS:
        check(classify(ROOT, spelling) == "no-root", f"a read, help, or a form the parser refuses runs as the caller: {spelling}")
    owner, director = out["owner"], out["director"]
    check(REFUSED not in owner["said"] and owner["said"].startswith("switchyard: pie release phase\n")
          and "from root-owned records only" not in owner["said"] and "Traceback" not in owner["said"],
          f"the owner, with no sudo, reaches the full read-only comparison through the trampoline: {owner}")
    check(REFUSED not in director["said"] and "from root-owned records only" in director["said"]
          and f"{out['config_path']} is not readable from syrd-agent" in director["said"] and director["code"] == 1,
          f"another tenant's account, with no sudo, reaches only the root-records view: {director}")
    check(out["remedy"] == ["switchyard release-status pie", "pie-agent"] and out["remedy_run"]["said"] == owner["said"],
          f"the remedy it prints runs as printed, as the account it names, and is the owner's comparison: "
          f"{out['remedy']} {out.get('remedy_run')}")
    for name in ("owner", "director"):
        for spelling, result in zip(CLOSES, out[f"{name}_closes"]):
            check(result["code"] == 1 and REFUSED in result["said"] and "release phase" not in result["said"],
                  f"{name}: {' '.join(spelling)} is refused at the trampoline, before the target reads anything: {result}")
    check(len(out["sudo_calls"]) == 2 * len(CLOSES) and all(" sudo -n -v" in call for call in out["sudo_calls"])
          and not any(call.startswith("root:") for call in out["sudo_calls"]),
          f"only the closes asked sudo -- every read ran as its caller -- and sudo granted none: {out['sudo_calls']}")
    check(out["journal_after_callers"] == out["journal_before"],
          "nothing the owner or the other account ran wrote root's journal")
    root, close = out["root"], out["root_close"]
    check(root["said"].startswith("switchyard: pie release phase\n") and REFUSED not in root["said"],
          f"root reads the full comparison, as before: {root}")
    # This host's board root names no deployed release, so the close's own re-proof refuses -- and records why.
    check(REFUSED not in close["said"] and "switchyard: pie release phase\n" in close["said"]
          and "the release phase cannot be recorded done:" in close["said"]
          and "recorded pie's release phase blocked with that reason" in close["said"],
          f"root's --close still reaches the close, which re-proves before it records anything: {close}")


def operator_note() -> None:
    """The upgrade's note about `release-status` says what the bare command shows each caller, and names the command
    that shows an operator the rest -- each runs as printed for the caller it names."""
    from types import SimpleNamespace

    sys.path.insert(0, str(ROOT))
    from scripts import team_launcher as launcher

    lines = launcher.outstanding_release_phase_report(SimpleNamespace(project="pie", run_as_user="pie-agent"),
                                                      config_path=Path("/nonexistent/syrd671/pie.json"), journal={})
    note = next(line for line in lines if line.startswith("switchyard: `switchyard release-status pie` compares"))
    check("as pie-agent or root everything, as any other account what root's own records show" in note
          and "`pkexec switchyard release-status pie` for the rest" in note,
          f"the operator's note says what each caller sees, and how an operator sees the rest: {note}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    after = run(ROOT)
    candidate(after)
    operator_note()
    if os.environ.get("SYRD671_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd671-before.") as tmp:
            os.chmod(tmp, 0o755)
            tree = tree_at(BEFORE, Path(tmp) / "before")
            before(tree, run(tree))
    print(f"release_status_trampoline_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
