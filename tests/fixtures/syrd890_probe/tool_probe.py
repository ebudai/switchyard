"""SYRD-890 probe: what the installed agy does with Switchyard's PreToolUse guard, offline.

Run as root of a private user and network namespace (loopback only):

    unshare -rn python3 tool_probe.py OUTDIR [COMMAND_LINE]

The real installer (ticket-board-install-pane-hooks) writes agy's hooks into a throwaway HOME, with this tree's shell
guard as the PreToolUse hook, behind a wrapper that logs each call's stdin and answer (guard.jsonl). The stand-in model
(agy_tool_mitm.py) answers a turn naming PROBE-TOOL with one run_command call, COMMAND_LINE (default `echo
PROBE-TOOL-RAN`), and turns on the experiment flags agy runs hooks under; the next model request carries agy's tool
result, which says whether the command ran or was denied, and why. summary.json says what was seen. Built on SYRD-756's Rig (tests/fixtures/syrd756_probe/probe.py): a
throwaway HOME, a fake account, a throwaway CA; nothing leaves the namespace and no real login is read.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(os.environ.get("PROBE_REPO") or Path(__file__).resolve().parents[3])
sys.path.insert(0, str(REPO / "tests" / "fixtures" / "syrd756_probe"))
import probe as base  # noqa: E402  (SYRD-756's Rig)

HERE = Path(__file__).resolve().parent


class Rig890(base.Rig):
    def __init__(self, out: Path, command: str):
        super().__init__("agy", out)
        self.command = command

    def guard_wrapper(self) -> Path:
        log = self.out / "guard.jsonl"
        wrapper = self.home / "guard-wrapper"
        wrapper.write_text(f'''#!/usr/bin/env python3
import json, subprocess, sys
raw = sys.stdin.read()
done = subprocess.run([sys.executable, {str(REPO / "scripts" / "ticket-board-shell-guard")!r}, *sys.argv[1:]],
                      input=raw, capture_output=True, text=True)
out = done.stdout
with open({str(log)!r}, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": raw[:2000], "guard_stdout": done.stdout, "printed": out,
                        "rc": done.returncode, "stderr": done.stderr[-500:]}}) + "\\n")
sys.stdout.write(out)
sys.exit(done.returncode)
''')
        wrapper.chmod(0o755)
        return wrapper

    def install_hooks(self) -> None:
        wrapper = self.home / "hook-wrapper"
        wrapper.write_text("#!/bin/sh\n"
                           f"python3 {REPO / 'scripts' / 'ticket-board-pane-idle-hook'} \"$@\"\n")
        wrapper.chmod(0o755)
        env = {**os.environ, "HOME": str(self.home), "TICKET_BOARD_PANE_STATE_DIR": str(self.state),
               "TICKET_BOARD_PANE_SESSION_DIR": str(self.sessions)}
        result = base.sh(sys.executable, str(REPO / "scripts" / "ticket-board-install-pane-hooks"), "install",
                         "--home", str(self.home), "--hook-source", str(wrapper),
                         "--bin-path", str(self.home / "bin" / "ticket-board-pane-idle-hook"),
                         "--claude-permission-hook", str(REPO / "scripts" / "ticket-board-claude-permission-hook"),
                         "--shell-guard", str(self.guard_wrapper()), env=env)
        (self.out / "install.txt").write_text(result.stdout + result.stderr)
        if result.returncode != 0:
            raise SystemExit(f"hook install failed: {result.stderr[-400:]}")
        hooks = self.home / ".gemini" / "config" / "hooks.json"
        (self.out / "hooks.json").write_text(hooks.read_text() if hooks.exists() else "(none)\n")

    def agy_offline(self) -> dict[str, str]:
        env = super().agy_offline()  # starts SYRD-756's stand-in; replace it with the tool-calling one
        self.mitm.terminate()
        self.mitm.wait()
        tls = self.out / "tls"
        call = {"name": "run_command", "args": {"CommandLine": self.command, "toolSummary": "Probe command",
                                                 "WaitMsBeforeAsync": 5000}}
        self.mitm = subprocess.Popen([sys.executable, str(HERE / "agy_tool_mitm.py"), str(base.PORT + 1),
                                      str(tls / "leaf.pem"), str(tls / "leaf.key"), str(self.models)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     env={**os.environ, "PROBE_TOOL_CALL": json.dumps(call)})
        return env

    def probe(self) -> dict:
        try:
            env, cli, _resume = self.configure()
            self.install_hooks()
            self.seen["command"] = self.command
            self.seen["versions"] = base.sh(cli[0], "--version", env={**env}).stdout.strip()[:120]
            self.start(env, cli, "start")
            time.sleep(10)
            self.snap("ready")
            self.type_line("PROBE-TOOL please run the probe command")
            bodies = self.out / "bodies"
            end = time.time() + 90
            result = None
            while time.time() < end:
                for path in sorted(bodies.glob("stream-*.json")):
                    text = path.read_text(errors="replace")
                    if "functionResponse" in text:
                        result = text
                        break
                if result:
                    break
                time.sleep(0.5)
            time.sleep(3)
            self.snap("after_tool")
            guard = [json.loads(line) for line in (self.out / "guard.jsonl").read_text().splitlines()] \
                if (self.out / "guard.jsonl").exists() else []
            self.seen["guard_calls"] = len(guard)
            self.seen["guard_printed"] = [g["printed"].strip() for g in guard]
            self.seen["guard_stdin"] = [g["stdin"][:600] for g in guard]
            self.seen["tool_result_seen"] = result is not None
            response = ""
            if result:
                at = result.find("functionResponse")
                response = result[at:result.find("systemInstruction", at) if "systemInstruction" in result[at:] else at + 1500]
                self.seen["function_response"] = response[:800]
            self.seen["tool_ran"] = "Command exited with code 0" in response and "denied by" not in response
            self.seen["denied_by_hook"] = "denied by pre-tool hook" in response
            return self.seen
        finally:
            self.tmux("kill-server")
            if getattr(self, "mitm", None) is not None:
                self.mitm.terminate()
            (self.out / "summary.json").write_text(json.dumps(self.seen, indent=1, sort_keys=True, default=str) + "\n")


if __name__ == "__main__":
    out = Path(sys.argv[1])
    command = sys.argv[2] if len(sys.argv) > 2 else "echo PROBE-TOOL-RAN"
    subprocess.run(["ip", "link", "set", "lo", "up"], check=False, capture_output=True)
    print(json.dumps(Rig890(out, command).probe(), indent=1, default=str))
