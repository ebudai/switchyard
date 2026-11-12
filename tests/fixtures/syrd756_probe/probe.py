"""SYRD-756 probe: does a runtime's own interrupt end a busy turn, leave the CLI running, and keep the conversation?

Run offline, as root of a private user and network namespace (loopback only):

    unshare -rn python3 probe.py RUNTIME OUTDIR [--key KEY ...]

Each CLI runs with the flags a Switchyard role runs it with (scripts/role_command.py), and is resumed as the launcher
resumes it, by the session id its own hooks named.

RUNTIME is claude, codex, copilot, hermes or agy. The real CLI runs in a private tmux server with Switchyard's real
pane hooks (scripts/ticket-board-install-pane-hooks, scripts/ticket-board-pane-idle-hook) into a throwaway HOME, so the
pane state file the board reads is the one recorded. Every hook call is also logged. fake_models.py answers the model
requests: a turn whose last probe word is PROBE-BUSY is held open until the interrupt.

Steps: wait for the prompt; send "PROBE-BUSY ..."; once the model request is held, send the interrupt key(s) (default:
the runtime's own, see INTERRUPT); then send "PROBE-AFTER ..." in the same CLI and see what that request carried; then
quit the CLI, resume its conversation with the runtime's resume command, and send "PROBE-RESUMED ...". Screens, cursor,
pane state, hook log and model log are kept under OUTDIR; summary.json says what was seen. No model is ever called and
nothing outside the namespace is touched.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PORT = 18756
MODEL = {"claude": "claude-sonnet-4-5", "codex": "gpt-6-sol", "copilot": "probe-model", "hermes": "Qwen/Qwen3.8-27B",
         "agy": "probe-model"}
#: Each runtime's own interrupt for a running turn, as its screen offers it. Never C-c.
INTERRUPT = {"claude": ["Escape"], "codex": ["Escape"], "copilot": ["Escape"], "hermes": ["Escape"], "agy": ["Escape"]}
#: How each runtime is asked to quit, between the interrupt check and the resume check.
QUIT = {"claude": "/exit", "codex": "/quit", "copilot": "/exit", "hermes": "/quit", "agy": "/quit"}
TARGET = "probe:0.0"


def sh(*argv: str, **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), text=True, capture_output=True, **kw)


class Rig:
    def __init__(self, runtime: str, out: Path):
        self.runtime, self.out = runtime, out
        self.tmp = Path(tempfile.mkdtemp(prefix="p756."))  # short: tmux caps a socket path
        self.home = out / "home"
        self.work = out / "work"
        self.state = out / "state"
        self.sessions = out / "sessions"
        for d in (self.home, self.work, self.state, self.sessions, out / "bodies"):
            d.mkdir(parents=True, exist_ok=True)
        self.sock = str(self.tmp / "s")
        self.hooks = out / "hooks.jsonl"
        self.models = out / "models.jsonl"
        self.seen: dict[str, object] = {"runtime": runtime}
        self.partial = False
        self.copilot_pkg = ""

    # -- tmux -------------------------------------------------------------------------------------------------------
    def tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return sh("tmux", "-S", self.sock, *args)

    def screen(self) -> str:
        return self.tmux("capture-pane", "-p", "-t", TARGET).stdout

    def snap(self, name: str) -> str:
        text = self.screen()
        (self.out / f"screen_{name}.txt").write_text(text)
        cursor = self.tmux("display-message", "-p", "-t", TARGET, "#{cursor_x} #{cursor_y} #{pane_pid}").stdout.strip()
        (self.out / f"screen_{name}.cursor").write_text(cursor + "\n")
        state = self.pane_state()
        (self.out / f"state_{name}.json").write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
        return text

    def pane_state(self) -> dict:
        for path in sorted(self.state.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("target") == TARGET:
                return {k: data.get(k) for k in ("state", "source", "updated_at", "turn_started_at", "session_id")
                        if k in data}
        return {}

    def keys(self, *keys: str) -> None:
        self.tmux("send-keys", "-t", TARGET, *keys)

    def type_line(self, text: str) -> None:
        self.tmux("send-keys", "-t", TARGET, "-l", text)
        time.sleep(0.6)
        self.keys("Enter")

    def cli_pid(self) -> int | None:
        """The CLI under the pane's shell: the pane's first child process."""
        pane = self.tmux("display-message", "-p", "-t", TARGET, "#{pane_pid}").stdout.strip()
        if not pane.isdigit():
            return None
        kids = sh("pgrep", "-P", pane).stdout.split()
        return int(kids[0]) if kids else None

    # -- the stand-in model -----------------------------------------------------------------------------------------
    def requests(self) -> list[dict]:
        try:
            return [json.loads(line) for line in self.models.read_text().splitlines() if line.strip()]
        except OSError:
            return []

    def wait_request(self, predicate, timeout: float) -> dict | None:
        end = time.time() + timeout
        while time.time() < end:
            for entry in self.requests():
                if entry.get("m") == "POST" and predicate(entry):
                    return entry
            time.sleep(0.25)
        return None

    def wait_screen(self, predicate, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if predicate(self.screen()):
                return True
            time.sleep(0.25)
        return False

    # -- the runtime ------------------------------------------------------------------------------------------------
    def install_hooks(self) -> None:
        wrapper = self.home / "hook-wrapper"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"t=$(mktemp); cat > \"$t\"\n"
            f"python3 {HERE / 'hooklog.py'} {self.hooks} \"$*\" < \"$t\"\n"
            f"python3 {REPO / 'scripts' / 'ticket-board-pane-idle-hook'} \"$@\" < \"$t\"; rc=$?\n"
            "rm -f \"$t\"; exit $rc\n")
        wrapper.chmod(0o755)
        env = {**os.environ, "HOME": str(self.home), "TICKET_BOARD_PANE_STATE_DIR": str(self.state),
               "TICKET_BOARD_PANE_SESSION_DIR": str(self.sessions)}
        result = sh(sys.executable, str(REPO / "scripts" / "ticket-board-install-pane-hooks"), "install",
                    "--home", str(self.home), "--hook-source", str(wrapper),
                    "--bin-path", str(self.home / "bin" / "ticket-board-pane-idle-hook"),
                    "--claude-permission-hook", str(REPO / "scripts" / "ticket-board-claude-permission-hook"),
                    "--shell-guard", str(REPO / "scripts" / "ticket-board-shell-guard"),
                    "--seed-codex-hook-trust-if-new", env=env)
        (self.out / "install.txt").write_text(result.stdout + result.stderr)
        if result.returncode != 0:
            raise SystemExit(f"hook install failed: {result.stderr[-400:]}")

    def configure(self) -> tuple[dict[str, str], list[str], list[str]]:
        """The pane environment, the start command and the resume command."""
        base = f"http://127.0.0.1:{PORT}"
        env = {"HOME": str(self.home), "PATH": "/usr/local/bin:/usr/bin:/bin", "TERM": "xterm-256color",
               "LANG": "C.UTF-8", "SHELL": "/bin/sh", "TICKET_BOARD_PANE_TARGET": TARGET,
               "TICKET_BOARD_PANE_STATE_DIR": str(self.state), "TICKET_BOARD_PANE_SESSION_DIR": str(self.sessions),
               "TICKET_BOARD_PROJECT": "probe"}
        r = self.runtime
        if r == "claude":
            now = int(time.time())
            (self.home / ".claude").mkdir(exist_ok=True)
            (self.home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
                "accessToken": "sk-ant-oat01-fake", "refreshToken": "sk-ant-ort01-fake",
                "expiresAt": (now + 30 * 86400) * 1000,
                "scopes": ["user:inference", "user:profile", "user:sessions:claude_code"],
                "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}}))
            (self.home / ".claude.json").write_text(json.dumps({
                "hasCompletedOnboarding": True, "lastOnboardingVersion": "2.1.318", "numStartups": 5,
                "bypassPermissionsModeAccepted": True,
                "oauthAccount": {"accountUuid": "00000000-0000-4000-8000-000000000001",
                                 "emailAddress": "probe@example.invalid",
                                 "organizationUuid": "00000000-0000-4000-8000-000000000002",
                                 "organizationRole": "admin", "displayName": "probe",
                                 "billingType": "stripe_subscription"},
                "projects": {str(self.work): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}}))
            env.update({"IS_SANDBOX": "1", "ANTHROPIC_BASE_URL": base, "DISABLE_UPDATES": "1",
                        "DISABLE_INSTALLATION_CHECKS": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
            return env, ["claude", "--dangerously-skip-permissions"], ["claude", "--dangerously-skip-permissions", "--resume", "{session}"]
        if r == "codex":
            codex = self.home / ".codex"
            config = (f'model = "{MODEL[r]}"\nmodel_provider = "mock"\n'
                      f'[model_providers.mock]\nname = "mock"\nbase_url = "{base}/v1"\nwire_api = "responses"\n'
                      'env_key = "MOCK_API_KEY"\n'
                      f'[projects."{self.work}"]\ntrust_level = "trusted"\n')
            codex.mkdir(exist_ok=True)
            (codex / "config.toml").write_text(config)
            env.update({"CODEX_HOME": str(codex), "MOCK_API_KEY": "probe"})
            flags = ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"]
            return env, ["codex", *flags], ["codex", "resume", "{session}", *flags]
        if r == "copilot":
            if self.copilot_pkg:  # the loader runs the newest package under $HOME/.cache/copilot/pkg (measured)
                shutil.copytree(self.copilot_pkg, self.home / ".cache/copilot/pkg/linux-x64" / Path(self.copilot_pkg).name)
            env.update({"COPILOT_OFFLINE": "true", "COPILOT_PROVIDER_BASE_URL": f"{base}/v1",
                        "COPILOT_PROVIDER_TYPE": "openai", "COPILOT_PROVIDER_API_KEY": "probe",
                        "COPILOT_ALLOW_ALL": "true"})
            cli = ["copilot", "--allow-all", "--model", MODEL[r]]
            return env, cli, cli + ["--resume", "{session}"]
        if r == "hermes":
            hermes = self.home / ".hermes"
            hermes.mkdir(exist_ok=True)
            config = sh(sys.executable, str(REPO / "tests" / "fixtures" / "syrd734_probe" / "config.py"), str(REPO))
            if config.returncode != 0:
                raise SystemExit(f"hermes config failed: {config.stderr[-400:]}")
            text = config.stdout.replace("127.0.0.1:18734", f"127.0.0.1:{PORT}")
            (hermes / "config.yaml").write_text(text)
            env.update({"HERMES_HOME": str(hermes)})
            cli = ["hermes", "--yolo", "--accept-hooks", "--pass-session-id"]
            return env, cli, cli + ["--resume", "{session}"]
        if r == "agy":
            env.update(self.agy_offline())
            return env, ["agy", "--dangerously-skip-permissions"], ["agy", "--dangerously-skip-permissions", "--conversation", "{session}"]
        raise SystemExit(f"unknown runtime {r}")

    def agy_offline(self) -> dict[str, str]:
        """agy talks only to Google: a throwaway CA, a leaf for Google's names, agy_mitm.py as the HTTPS proxy, a
        fake account whose token names no real one, and settings that pick the stand-in's one model."""
        tls = self.out / "tls"
        tls.mkdir(exist_ok=True)
        names = ",".join(f"DNS:{n}" for n in ("*.googleapis.com", "googleapis.com", "*.google.com", "google.com",
                                               "antigravity.google", "*.antigravity.google", "*.gstatic.com"))
        sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=probe-ca",
           "-keyout", str(tls / "ca.key"), "-out", str(tls / "ca.pem"))
        sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=googleapis.com",
           "-keyout", str(tls / "leaf.key"), "-out", str(tls / "leaf.csr"))
        (tls / "ext.cnf").write_text(f"subjectAltName={names}\n")
        sh("openssl", "x509", "-req", "-in", str(tls / "leaf.csr"), "-CA", str(tls / "ca.pem"), "-CAkey",
           str(tls / "ca.key"), "-CAcreateserial", "-days", "2", "-extfile", str(tls / "ext.cnf"),
           "-out", str(tls / "leaf.pem"))
        bundle = tls / "bundle.pem"
        bundle.write_text(Path("/etc/ssl/certs/ca-certificates.crt").read_text() + (tls / "ca.pem").read_text())
        self.mitm = subprocess.Popen([sys.executable, str(HERE / "agy_mitm.py"), str(PORT + 1), str(tls / "leaf.pem"),
                                      str(tls / "leaf.key"), str(self.models)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cli = self.home / ".gemini" / "antigravity-cli"
        (cli / "cache").mkdir(parents=True, exist_ok=True)
        import base64
        claims = base64.urlsafe_b64encode(json.dumps({"email": "probe@example.invalid", "sub": "1"}).encode()).decode().rstrip("=")
        (cli / "antigravity-oauth-token").write_text(json.dumps({
            "token": {"access_token": "ya29.probe-not-a-token", "token_type": "Bearer",
                      "refresh_token": "1//probe-not-a-token", "expiry": "2099-01-01T00:00:00Z"},
            "auth_method": "consumer", "id_token": f"eyJhbGciOiJub25lIn0.{claims}."}))
        (cli / "settings.json").write_text(json.dumps({"colorScheme": "dark", "model": "Probe Flash",
                                                       "trustedWorkspaces": [str(self.work)]}))
        (cli / "cache" / "onboarding.json").write_text(json.dumps({
            "consumerOnboardingComplete": True, "enterpriseOnboardingComplete": False, "onboardingComplete": True}))
        proxy = f"http://127.0.0.1:{PORT + 1}"
        return {"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy,
                "NO_PROXY": "127.0.0.1,localhost", "SSL_CERT_FILE": str(bundle)}

    def start(self, env: dict[str, str], argv: list[str], name: str) -> None:
        line = " ".join(["env", "-i", *[f"{k}={v}" for k, v in env.items()], *argv]) + f" 2>{self.out}/{name}.err"
        if self.tmux("has-session", "-t", "probe").returncode != 0:
            self.tmux("-f", "/dev/null", "new-session", "-d", "-x", "160", "-y", "45", "-s", "probe", "-c",
                      str(self.work), "/bin/sh")
        self.tmux("send-keys", "-t", TARGET, "-l", line)
        self.keys("Enter")

    def run(self, keys: list[str]) -> dict:
        server = subprocess.Popen([sys.executable, str(HERE / "fake_models.py"), str(PORT), str(self.models), MODEL[self.runtime]],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  env={**os.environ, "PROBE_PARTIAL": "1" if self.partial else "0",
                                       "PROBE_BODIES": str(self.out / "bodies")})
        try:
            time.sleep(0.5)
            env, cli, resume = self.configure()  # first: the hook installer merges into what it finds
            self.install_hooks()
            self.seen["versions"] = sh(cli[0], "--version", env={**env}).stdout.strip()[:120]
            self.start(env, cli, "start")
            time.sleep(8)
            self.snap("ready")
            self.seen["pid_before"] = self.cli_pid()
            self.type_line("PROBE-BUSY please start on the long task")
            held = self.wait_request(lambda e: e.get("held"), 40)
            self.seen["held_request"] = bool(held)
            time.sleep(3)
            self.snap("busy")
            self.seen["interrupt_keys"] = keys
            sent_at = time.time()
            for key in keys:
                self.keys(key)
                time.sleep(0.3)
            for t in (0.5, 1, 2, 4, 8):
                time.sleep(t if t == 0.5 else t / 2)
                self.snap(f"interrupted_{t}")
            self.seen["pid_after_interrupt"] = self.cli_pid()
            self.seen["cli_survived"] = bool(self.seen["pid_before"]) and self.seen["pid_before"] == self.seen["pid_after_interrupt"]
            self.seen["state_after_interrupt"] = self.pane_state()
            self.seen["hooks_after_interrupt"] = [h for h in self._hooks() if h["t"] >= sent_at]
            self.type_line("PROBE-AFTER what did I ask you before")
            after = self.wait_request(lambda e: e.get("words") and e["words"][-1] == "PROBE-AFTER", 40)
            time.sleep(4)
            self.snap("after")
            self.seen["after_request_words"] = after.get("words") if after else None
            self.seen["after_carries_interrupted_prompt"] = bool(after and "PROBE-BUSY" in after["words"])
            self.seen["state_after_reply"] = self.pane_state()
            self.type_line(QUIT[self.runtime])
            time.sleep(5)
            self.snap("quit")
            self.seen["pid_after_quit"] = self.cli_pid()
            session = self.session_id()
            self.seen["session"] = session
            self.start(env, [part.replace("{session}", session) for part in resume], "resume")
            time.sleep(10)
            self.snap("resumed_ready")
            self.type_line("PROBE-RESUMED and now")
            resumed = self.wait_request(lambda e: e.get("words") and e["words"][-1] == "PROBE-RESUMED", 40)
            time.sleep(4)
            self.snap("resumed")
            self.seen["resumed_request_words"] = resumed.get("words") if resumed else None
            self.seen["resume_carries_history"] = bool(resumed and "PROBE-BUSY" in resumed["words"]
                                                       and "PROBE-AFTER" in resumed["words"])
            return self.seen
        finally:
            self.tmux("kill-server")
            server.terminate()
            if getattr(self, "mitm", None) is not None:
                self.mitm.terminate()
            (self.out / "summary.json").write_text(json.dumps(self.seen, indent=1, sort_keys=True, default=str) + "\n")
            shutil.rmtree(self.tmp, ignore_errors=True)

    def session_id(self) -> str:
        """The conversation the runtime's own hooks named last: what the launcher resumes (role_command). agy ran no
        hook offline, so for it the id it prints as it quits ("agy --conversation=<id>") is read instead."""
        for entry in reversed(self._hooks()):
            for key in ("session_id", "sessionId"):
                if entry.get(key):
                    return str(entry[key])
        import re
        quit_screen = (self.out / "screen_quit.txt").read_text() if (self.out / "screen_quit.txt").exists() else ""
        found = re.findall(r"--conversation=([0-9a-f-]{36})", quit_screen)
        return found[-1] if found else ""

    def _hooks(self) -> list[dict]:
        try:
            return [json.loads(line) for line in self.hooks.read_text().splitlines() if line.strip()]
        except OSError:
            return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", choices=sorted(MODEL))
    parser.add_argument("out")
    parser.add_argument("--key", action="append", default=[], help="tmux key name(s) to interrupt with")
    parser.add_argument("--partial", action="store_true", help="the held turn streams part of an answer first")
    parser.add_argument("--copilot-pkg", default="", help="a Copilot package directory (~/.cache/copilot/pkg/linux-x64/"
                                                          "<version>) for the loader to run instead of its own")
    args = parser.parse_args()
    if os.getuid() != 0 or sh("ip", "link", "set", "lo", "up").returncode != 0:
        raise SystemExit("run as: unshare -rn python3 probe.py ...")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    rig = Rig(args.runtime, out)
    rig.partial = rig.seen["partial"] = args.partial
    rig.copilot_pkg = args.copilot_pkg
    seen = rig.run(args.key or INTERRUPT[args.runtime])
    print(json.dumps({k: seen.get(k) for k in ("runtime", "held_request", "cli_survived", "state_after_interrupt",
                                                "after_carries_interrupted_prompt", "resume_carries_history")},
                     default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
