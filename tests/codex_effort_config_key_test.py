#!/usr/bin/env python3
"""SYRD-277: a Codex role's effort level reaches Codex under the key Codex reads.

The config-style effort adapter emitted `-c reasoning_effort=high`. Codex's
setting is `model_reasoning_effort`; measured on this host's Codex CLI
0.156.1, the old flag made Codex print

    session-flags: `reasoning_effort` is ignored.

and start at reasoning effort `none`. The adapter now emits
`-c model_reasoning_effort="high"` (the `-c` value is TOML, so the level is a
quoted string).

The checks here are the emitted command -- from the real
`cli_command_for_role`, on a fresh launch and on a resume -- and Codex's own
reading of it: the Codex arguments of that command are handed to `codex exec`
in an empty CODEX_HOME, which prints its session header (model, reasoning
effort) and then stops for want of credentials. No model is asked anything.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import team_launcher as tl  # noqa: E402

CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def load_role(root: Path, cli: str, *, effort: str = "high", extra_args=None, **fields):
    role = {"role": "ops", "cli": [cli], "slot": 0, "workdir": str(root / "ops"),
            "model": "gpt-5" if cli == "codex" else "some-model", "effort": effort, **fields}
    if extra_args is not None:
        role["extra_args"] = extra_args
    path = root / f"{cli}-{effort}-{len(extra_args or [])}.json"
    path.write_text(json.dumps({"project": "porter", "board_url": "http://127.0.0.1:1/",
                                "roles": [role]}), encoding="utf-8")
    return tl.load_project_config("porter", path).roles[0]


def cli_args(command: list[str], cli: str) -> list[str]:
    """The CLI's own arguments: everything after the program itself."""
    index = next(i for i, word in enumerate(command) if Path(word).name == cli)
    return command[index + 1:]


def test_codex_is_given_the_setting_it_reads() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd277-emit.") as raw:
        root = Path(raw)
        role = load_role(root, "codex")
        command = tl.cli_command_for_role(role, session_dir=root / "sessions")
        args = cli_args(command, "codex")
        check('model_reasoning_effort="high"' in args, f"the setting Codex reads: {args}")
        check(args[args.index('model_reasoning_effort="high"') - 1] == "-c", f"as a config override: {args}")
        check(not any(a.startswith("reasoning_effort") for a in args), f"and never the ignored key: {args}")


def test_a_resumed_codex_session_is_given_it_too() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd277-resume.") as raw:
        root = Path(raw)
        role = load_role(root, "codex", resume_mode="subcommand", resume_subcommand="resume")
        sessions = root / "sessions"
        sessions.mkdir()
        (sessions / tl.session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": "resume-me"}), encoding="utf-8")
        command = tl.cli_command_for_role(role, session_dir=sessions, resume=True)
        args = cli_args(command, "codex")
        check(args[:2] == ["resume", "resume-me"], f"this is the resume path: {args}")
        check('model_reasoning_effort="high"' in args, f"and it carries the effort: {args}")


def test_other_runtimes_keep_their_own_effort_styles() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd277-styles.") as raw:
        root = Path(raw)
        for cli, expected in (("claude", ["--effort", "high"]), ("hermes", ["--reasoning", "high"])):
            args = cli_args(tl.cli_command_for_role(load_role(root, cli), session_dir=root / "s"), cli)
            check(any(args[i:i + 2] == expected for i in range(len(args))), f"{cli}: {args}")
            check(not any("reasoning_effort" in a for a in args), f"{cli} is not given Codex's key: {args}")
        agy = cli_args(tl.cli_command_for_role(load_role(root, "agy"), session_dir=root / "s"), "agy")
        check(not any("effort" in a or a == "--reasoning" for a in agy), f"agy takes no effort: {agy}")


def test_a_roles_own_override_still_comes_last() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd277-order.") as raw:
        root = Path(raw)
        role = load_role(root, "codex", extra_args=["-c", 'model_reasoning_effort="low"'])
        args = cli_args(tl.cli_command_for_role(role, session_dir=root / "s"), "codex")
        check(args.index('model_reasoning_effort="high"') < args.index('model_reasoning_effort="low"'),
              f"extra_args follow the generic effort, and Codex applies the last: {args}")


def codex_header(args: list[str], home: Path, *, deadline: float = 45.0) -> str:
    """What Codex says it is running with -- read, then Codex is stopped.

    Run in a user and NETWORK namespace: Codex prints its session header
    (model, reasoning effort) before it tries to reach any provider, and in
    there it cannot reach one -- nothing leaves this machine, credentials or
    not. It then retries forever, so the probe stops it once the header's
    settings are out.
    """
    import os
    import select
    import time

    flags = {"-m", "--model", "-c", "--config"}
    session = [a for i, a in enumerate(args) if a in flags or (i and args[i - 1] in flags)]
    process = subprocess.Popen(
        ["unshare", "--user", "--map-root-user", "--net",
         "codex", "exec", "--skip-git-repo-check", *session, "probe"],
        cwd=home, env={"PATH": "/usr/bin:/bin", "HOME": str(home), "CODEX_HOME": str(home)},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    said = ""
    end = time.monotonic() + deadline
    try:
        while time.monotonic() < end and "reasoning summaries:" not in said:
            ready, _, _ = select.select([process.stdout], [], [], 0.5)
            if ready:
                chunk = os.read(process.stdout.fileno(), 65536).decode("utf-8", "replace")
                if not chunk:
                    break
                said += chunk
    finally:
        process.kill()
        process.wait()
    return said


def test_codex_itself_reports_the_effort_it_was_given() -> None:
    if shutil.which("codex") is None:
        print("codex_effort_config_key_test: codex is not installed here; banner probe NOT run")
        return
    with tempfile.TemporaryDirectory(prefix="syrd277-banner.") as raw:
        root = Path(raw)
        role = load_role(root, "codex")
        header = codex_header(cli_args(tl.cli_command_for_role(role, session_dir=root / "s"), "codex"), root)
        check("model: gpt-5" in header, f"the probe reached Codex's session header: {header[-400:]}")
        check("reasoning effort: high" in header, f"Codex runs at the configured effort: {header[-400:]}")
        check("is ignored" not in header, f"and ignores nothing it was given: {header[-400:]}")

        role = load_role(root, "codex", extra_args=["-c", 'model_reasoning_effort="low"'])
        header = codex_header(cli_args(tl.cli_command_for_role(role, session_dir=root / "s"), "codex"), root)
        check("reasoning effort: low" in header, f"a role's own override wins, as Codex applies it: {header[-400:]}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"codex_effort_config_key_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
