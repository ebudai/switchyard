#!/usr/bin/env python3
"""SYRD-692: a Hermes role on the host's local model service keeps exactly that profile, and nothing hosted.

Every scenario is a disposable tenant -- the launcher's own config loader, `prepare_hermes_home_for_role` and
`cli_command_for_role` -- with HOME, the owner's shared Hermes home and the local model service's record inside a
temporary tree. The start check runs as the pane runs it, as a separate `python3 -I` process, against a loopback
HTTP server standing in for llama-server's `/v1/models`. The home it produces is then handed to the installed
Hermes, when it is there, offline in its own user and network namespace (`unshare -rn`), whose own provider
resolution says where the main agent and every auxiliary task would go.
"""

from __future__ import annotations

import contextlib
import copy
import http.server
import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from scripts import hermes_local_profile as hlp  # noqa: E402
from scripts import team_launcher  # noqa: E402

HERMES_PYTHON = Path("/opt/hermes/venv/bin/python3")
#: The real reader, kept before any case patches the module's own, which reads the host's record.
READ_RECORD = hlp.read_service_record
CHECKS = 0
MODEL = "Qwen/Qwen3.8-27B"
REVISION = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
ARTIFACT_REVISION = "a316ea527fde32c12487be7574e2db5ac2ad2a7e"
SECRETS = ("sk-or-SHARED-OPENROUTER", "sk-SHARED-VISION", "sk-SHARED-PROVIDER", "SHARED-PASSWORD")
SHARED_CONFIG = f"""model:
  default: anthropic/claude-opus-4
  provider: openrouter
  api_key: {SECRETS[0]}
providers:
  openrouter:
    api_key: {SECRETS[2]}
fallback_providers:
  - provider: openrouter
    model: openai/gpt-5
fallback_model: openai/gpt-5
auxiliary:
  vision:
    provider: openrouter
    model: google/gemini-2.5-flash
    api_key: {SECRETS[1]}
    timeout: 77
delegation:
  provider: openrouter
  model: openai/gpt-5
agent:
  max_turns: 60
terminal:
  sudo_password: {SECRETS[3]}
display:
  compact: true
"""


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def record(port: int, *, context: int = 65536, host: str = "127.0.0.1", **model: str) -> dict:
    return {"schema": hlp.SERVICE_SCHEMA, "packet_digest": "0" * 64,
            "model": {"id": MODEL, "upstream_revision": REVISION, "artifact_repository": "ggml-org/Qwen3.8-27B-GGUF",
                      "artifact_revision": ARTIFACT_REVISION, "quantization": "Q4_K_M", **model},
            "engine": {"name": "llama.cpp", "tag": "b11803"},
            "settings": {"host": host, "port": port, "context": context}}


def profile(port: int = 17431, **changes) -> hlp.LocalProfile:
    return hlp.profile_from_record({**record(port), **changes}) if changes else hlp.profile_from_record(record(port))


class Server:
    """llama-server's `/v1/models`, as the live one answers it, on a loopback port of its own."""

    def __init__(self, *, model: str = MODEL, n_ctx: int = 65536) -> None:
        state = self
        self.model, self.n_ctx, self.asked = model, n_ctx, []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                state.asked.append(self.path)
                if self.path != "/v1/models":
                    self.send_response(404), self.end_headers()
                    return
                body = json.dumps({"object": "list", "data": [{"id": state.model, "object": "model", "owned_by": "llamacpp",
                                                               "meta": {"n_ctx": state.n_ctx, "n_ctx_train": 262144}}]})
                self.send_response(200), self.send_header("Content-Type", "application/json"), self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown(), self.httpd.server_close()


class Tenant:
    """A disposable tenant with Hermes roles, its owner's shared Hermes home and the service's record."""

    def __init__(self, port: int, *, roles: tuple[str, ...] = ("app",)) -> None:
        import team_launcher_presentation_test as fixture

        self.root = Path(tempfile.mkdtemp(prefix="syrd692-"))
        (self.root / "tenant").mkdir()
        self.config_path = fixture._write_presentation_config(self.root / "tenant")
        self.raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.home = self.root / "home"
        self.raw["session_dir"] = str(self.home / ".local" / "state" / "porter-ticket-board" / "pane-sessions")
        self.raw["desktop_access"] = {"mode": "headless"}
        self.shared = self.home / ".hermes"
        self.shared.mkdir(parents=True)
        (self.shared / "config.yaml").write_text(SHARED_CONFIG, encoding="utf-8")
        (self.shared / ".env").write_text("OPENROUTER_API_KEY=sk-or-ENV-SECRET\n", encoding="utf-8")
        (self.shared / "auth.json").write_text('{"active_provider": "nous"}\n', encoding="utf-8")
        (self.shared / "SOUL.md").write_text("soul\n", encoding="utf-8")
        self.service = self.root / "srv" / "local-models"
        self.service.mkdir(parents=True)
        os.chmod(self.root / "srv", 0o755), os.chmod(self.service, 0o755)
        self.port = port
        self.write_record(record(port))
        for role in self.raw["roles"]:
            if role["role"] in roles:
                role.update(cli=["hermes"], live_commands=["hermes"], model=MODEL, yolo=True,
                            hermes_local=profile(port).entry())
        self.save()

    def write_record(self, data: dict) -> None:
        (self.service / "service.json").write_text(json.dumps(data), encoding="utf-8")
        os.chmod(self.service / "service.json", 0o644)

    def save(self) -> None:
        self.config_path.write_text(json.dumps(self.raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def config(self):
        return team_launcher.load_project_config("porter", self.config_path)

    def role(self, name: str = "app"):
        return team_launcher._role_by_name(self.config(), name)

    def store(self, name: str = "app") -> Path:
        return team_launcher.role_session_dir(self.config(), self.role(name))

    def role_home(self, name: str = "app") -> Path:
        return team_launcher.hermes_home_for_role(self.role(name), session_dir=self.store(name))

    def prepare(self, name: str = "app") -> str:
        notices = io.StringIO()
        with contextlib.redirect_stderr(notices):
            team_launcher.prepare_hermes_home_for_role(self.role(name), session_dir=self.store(name))
        return notices.getvalue()

    def command(self, name: str = "app", *, resume: bool = False) -> list[str]:
        return team_launcher.cli_command_for_role(self.role(name), session_dir=self.store(name), resume=resume)

    def read_record(self) -> dict:
        return READ_RECORD(self.service / "service.json", anchor=self.root, owner_uid=os.getuid())

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


@contextlib.contextmanager
def tenant(**kwargs):
    server = Server()
    made = Tenant(server.port, **kwargs)
    try:
        # The tenant's home stands for this account's home, as the password database would name it on a host.
        with patch.object(hlp, "account_home", lambda: made.home):
            yield made, server
    finally:
        made.close(), server.close()


#: The start check as the pane runs it, but reading the tenant's record rather than the host's.
CHECK_CHILD = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import hermes_local_profile as hlp
record, anchor = Path(sys.argv[2]), Path(sys.argv[3])
real = hlp.read_service_record
hlp.read_service_record = lambda *a, **k: real(record, anchor=anchor, owner_uid=__import__("os").getuid())
raise SystemExit(hlp.main(sys.argv[4:]))
'''


def start(t: Tenant, command: list[str], *, environ: dict | None = None) -> subprocess.CompletedProcess[str]:
    """The pane's check, as a separate python3 -I process, with the role's own HERMES_HOME; `command` follows it."""
    pane = t.command()
    check("check" in pane, f"the pane runs the start check before Hermes: {pane}")
    at = pane.index("check")
    check(pane[at - 2:at] == ["-I", str(ROOT / "scripts" / "hermes_local_profile.py")], f"the pane runs this file: {pane}")
    args = pane[at:pane.index("--", at) + 1]
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(t.home), "HERMES_HOME": str(t.role_home()),
           **(environ or {})}
    return subprocess.run([sys.executable, "-I", "-c", CHECK_CHILD, str(ROOT / "scripts"), str(t.service / "service.json"),
                           str(t.root), *args, *command], env=env, capture_output=True, text=True, timeout=60)


def test_the_profile_comes_from_the_services_own_record_and_nothing_floats() -> None:
    with tenant() as (t, server):
        got = hlp.profile_from_record(t.read_record())
        check(got == hlp.LocalProfile(model=MODEL, base_url=f"http://127.0.0.1:{server.port}/v1", server="llama.cpp b11803",
                                      revision=REVISION, artifact=f"ggml-org/Qwen3.8-27B-GGUF@{ARTIFACT_REVISION}",
                                      quantization="Q4_K_M", context=65536),
              f"exactly what the record pins, endpoint on loopback: {got}")
        check(hlp.profile_from_record(record(17431, host="::1")).base_url == "http://[::1]:17431/v1", "IPv6 loopback too")
        for bad, why in ((record(17431, host="0.0.0.0"), "not http://127.0.0.1"),
                         (record(17431, host="192.168.1.5"), "not http://127.0.0.1"),
                         (record(17431, id="Qwen/Qwen3.8-latest"), "not one exact, pinned model id"),
                         (record(17431, id="qwen3.8"), "not one exact, pinned model id"),
                         (record(17431, upstream_revision="main"), "not a full 40-hex commit"),
                         (record(17431, artifact_revision="main"), "not <repository>@<40-hex revision>")):
            try:
                hlp.profile_from_record(bad)
                refused = ""
            except hlp.ProfileRefusal as exc:
                refused = str(exc)
            check(why in refused, f"refused: {why}: {refused}")
        for change in ({"schema": "other"}, {"model": None}):
            t.write_record({**record(server.port), **change})
            try:
                hlp.profile_from_service(t.service / "service.json", anchor=t.root, owner_uid=os.getuid())
                refused = ""
            except hlp.ProfileRefusal as exc:
                refused = str(exc)
            check(refused.startswith("switchyard: "), f"a record that is not the service's is refused: {change} {refused}")


def test_the_record_is_read_only_where_nobody_else_could_have_put_it() -> None:
    with tenant() as (t, _server):
        path = t.service / "service.json"

        def refused() -> str:
            try:
                hlp.read_service_record(path, anchor=t.root, owner_uid=os.getuid())
            except hlp.ProfileRefusal as exc:
                return str(exc)
            return ""

        check(refused() == "", "the record as the service writes it reads")
        elsewhere = t.root / "forged.json"
        elsewhere.write_text(json.dumps(record(9999)), encoding="utf-8")
        path.unlink(), path.symlink_to(elsewhere)
        check("without following a link" in refused(), f"a record that is a link is refused: {refused()}")
        path.unlink(), t.write_record(record(t.port))
        os.chmod(t.service, 0o777)
        check("writable only by it" in refused(), f"a directory others can write is refused: {refused()}")
        os.chmod(t.service, 0o755), os.chmod(path, 0o666)
        check("writable only by it" in refused(), f"a record others can write is refused: {refused()}")
        os.chmod(path, 0o644)
        real = t.root / "srv-real"
        (t.root / "srv").rename(real), (t.root / "srv").symlink_to(real)
        check("without following a link" in refused(), f"a linked directory on the way is refused: {refused()}")
        (t.root / "srv").unlink(), real.rename(t.root / "srv")
        check("not owned by uid 0" in _refusal(lambda: hlp.read_service_record(path, anchor=t.root, owner_uid=0)),
              "only root's own record counts on a host")
        check("not installed" in _refusal(lambda: hlp.read_service_record(t.root / "srv" / "gone.json", anchor=t.root,
                                                                           owner_uid=os.getuid())),
              "no service: said plainly")


def _refusal(call) -> str:
    try:
        call()
    except hlp.ProfileRefusal as exc:
        return str(exc)
    return ""


def test_a_declaration_the_config_cannot_hold_is_refused_when_it_loads() -> None:
    with tenant() as (t, _server):
        check(t.role().hermes_local == profile(t.port) and t.role("director").hermes_local is None,
              "the declared role carries its profile, the others none")
        entry = next(r for r in t.raw["roles"] if r["role"] == "app")
        for change, why in (({"model": "Qwen/Qwen3.8-32B"}, "is not the profile's"),
                            ({"cli": ["codex"]}, "the role runs 'codex'"),
                            ({"hermes_local": {**entry["hermes_local"], "extra": 1}}, "unknown keys ['extra']"),
                            ({"hermes_local": {**entry["hermes_local"], "base_url": "http://10.0.0.2:17431/v1"}}, "is not http://127.0.0.1"),
                            ({"hermes_local": "local-qwen"}, "must be a JSON object")):
            saved = copy.deepcopy(entry)
            entry.update(change)
            t.save()
            refused = _refusal(t.config)
            entry.clear(), entry.update(saved), t.save()
            check(why in refused, f"refused when it loads: {why}: {refused}")


def test_the_home_holds_the_profile_and_no_hosted_credential() -> None:
    with tenant() as (t, _server):
        home = t.role_home()
        home.mkdir(parents=True)
        # Left from when the role was cloud-backed: the shared credential links.
        (home / ".env").symlink_to(t.shared / ".env"), (home / "auth.json").symlink_to(t.shared / "auth.json")
        before = {name: (t.shared / name).read_text(encoding="utf-8") for name in ("config.yaml", ".env", "auth.json")}
        check(t.prepare() == "", "prepared without a notice")
        text = (home / "config.yaml").read_text(encoding="utf-8")
        check((home / "config.yaml").is_file() and not (home / "config.yaml").is_symlink()
              and oct((home / "config.yaml").stat().st_mode & 0o777) == "0o600"
              and text.split("\n")[:2] == [hlp.CONFIG_MARK, hlp._profile_line(profile(t.port))],
              f"a private config.yaml, marked, naming its profile: {text[:300]}")
        check(not any(os.path.lexists(home / name) for name in hlp.CREDENTIAL_ENTRIES),
              f"no .env, auth.json or auth.lock in the home: {sorted(os.listdir(home))}")
        check(before == {name: (t.shared / name).read_text(encoding="utf-8") for name in before},
              "the owner's shared files are untouched")
        check((home / "SOUL.md").is_symlink(), "the other shared entries are still linked")
        check(not any(secret in text for secret in SECRETS), f"no credential value from the shared config: {text}")
        import yaml

        document = yaml.safe_load(text)
        check(document["model"] == {"provider": "custom", "default": MODEL, "base_url": f"http://127.0.0.1:{t.port}/v1",
                                    "api_key": hlp.NO_KEY, "api_mode": "chat_completions", "context_length": 65536},
              f"the main model on the endpoint only: {document['model']}")
        check(document["fallback_providers"] == [] and "fallback_model" not in document and "providers" not in document,
              "no fallback, no other provider")
        check(set(hlp.AUXILIARY_TASKS) <= set(document["auxiliary"]) and all(
            task["provider"] == "custom" and task["base_url"] == f"http://127.0.0.1:{t.port}/v1" and task["model"] == MODEL
            and task["fallback_chain"] == [] for task in document["auxiliary"].values()),
              f"every auxiliary task on the endpoint, with no fallback chain: {document['auxiliary']}")
        check(document["auxiliary"]["vision"]["timeout"] == 77 and document["agent"]["max_turns"] == 60
              and document["display"] == {"compact": True}, "the owner's other settings are kept")
        check(document["delegation"]["provider"] == "" and document["delegation"]["base_url"] == "",
              "subagents inherit the main, local model")
        again = (home / "config.yaml").read_text(encoding="utf-8")
        check(t.prepare() == "" and (home / "config.yaml").read_text(encoding="utf-8") == again,
              "the next start writes the same config: stop/start keeps the profile")


def test_a_link_anywhere_on_the_way_to_the_home_redirects_nothing() -> None:
    """Audit's case: `hermes-homes` itself, above the role's home, is a link the role could have planted."""
    with tenant() as (t, _server):
        home = t.role_home()
        homes = home.parent
        check(homes.name == "hermes-homes", f"the role's home is in hermes-homes: {home}")
        elsewhere = t.root / "elsewhere"
        elsewhere.mkdir()
        if homes.exists():
            shutil.rmtree(homes)
        homes.parent.mkdir(parents=True, exist_ok=True)
        homes.symlink_to(elsewhere)
        refused = _refusal(t.prepare)
        check("is a link or not a directory" in refused and str(homes) in refused and list(elsewhere.iterdir()) == [],
              f"through prepare_hermes_home_for_role: refused at hermes-homes, nothing made or written where it points: "
              f"{refused} {list(elsewhere.rglob('*'))}")
        # The same, after the home was made: removing the profile's config reaches nothing through the link.
        homes.unlink()
        t.prepare()
        check((home / "config.yaml").is_file(), "with the link gone, the home is prepared")
        (elsewhere / home.name).mkdir()
        decoy = elsewhere / home.name / "config.yaml"
        decoy.write_text(hlp.CONFIG_MARK + "\n", encoding="utf-8")
        shutil.rmtree(homes)
        homes.symlink_to(elsewhere)
        check(not hlp.remove_role_config(home) and decoy.exists(), "removal does not follow it either")
        homes.unlink()
        # And a link higher up, under the account's home: `.local/state` replaced by a link.
        state = t.home / ".local" / "state"
        moved = t.root / "state-elsewhere"
        state.rename(moved)
        state.symlink_to(moved)
        before = sorted(str(p) for p in moved.rglob("*"))
        refused = _refusal(t.prepare)
        check(str(state) in refused and sorted(str(p) for p in moved.rglob("*")) == before,
              f"a link higher up is refused too, and nothing changes behind it: {refused}")
        # A directory on the way that others can write: whoever could write it could have planted the next link.
        state.unlink(), moved.rename(state)
        os.chmod(homes.parent, 0o777)
        refused = _refusal(t.prepare)
        os.chmod(homes.parent, 0o755)
        check("does not belong to this account alone" in refused and str(homes.parent) in refused,
              f"a directory others can write, on the way, is refused: {refused}")
        check(not hlp.remove_role_config(t.root / "outside" / "x"), "removal outside the home: nothing")
        outside = t.root / "outside" / "porter-app_0.0"
        try:
            hlp.write_role_home(outside, profile(t.port), shared_home=t.shared)
            refused = "written"
        except BaseException as exc:  # noqa: BLE001 - a refusal is a SystemExit; only the plain refusal is the answer
            refused = f"{type(exc).__name__}: {exc}"
        check(refused.startswith("ProfileRefusal: ") and "is not inside" in refused and not (t.root / "outside").exists(),
              f"a home outside the account's own home is refused, and nothing is made: {refused}")


def test_a_configured_session_store_under_a_link_redirects_nothing() -> None:
    """Audit's second case: the configured session store names the account's home through a link of its own."""
    with tenant() as (t, _server):
        target = t.root / "elsewhere-home"
        (target / ".hermes").mkdir(parents=True)
        (target / ".hermes" / "config.yaml").write_text(SHARED_CONFIG, encoding="utf-8")
        alias = t.home / "owner-home-alias"
        alias.symlink_to(target)
        t.raw["session_dir"] = str(alias / ".local" / "state" / "porter-ticket-board" / "pane-sessions")
        t.save()
        home = t.role_home()
        check(str(home).startswith(str(alias)), f"the role's home is named through the link: {home}")
        before = sorted(str(p) for p in target.rglob("*"))
        refused = _refusal(t.prepare)
        check(str(alias) in refused and "is a link or not a directory" in refused
              and sorted(str(p) for p in target.rglob("*")) == before,
              f"through prepare_hermes_home_for_role: refused at the configured link, nothing made behind it: {refused}")
        check(not hlp.remove_role_config(home), "and removal reaches nothing behind it")
        # The account's home itself named through a link (as /home -> /var/home is on some hosts), and the store by
        # the resolved path: the same directory, so the role is prepared as usual.
        alias.unlink()
        t.raw["session_dir"] = str(t.home / ".local" / "state" / "porter-ticket-board" / "pane-sessions")
        t.save()
        named = t.root / "home-by-link"
        named.symlink_to(t.home)
        with patch.object(hlp, "account_home", lambda: named):
            refused = _refusal(t.prepare)
        config = t.role_home() / "config.yaml"
        check(refused == "" and config.is_file() and hlp.is_ours(config.read_text(encoding="utf-8")),
              f"a home named through a link: its resolved path is the same home, and the role is prepared: {refused}")
        # A home that is not this account's: refused before anything is made below it.
        with patch.object(hlp, "account_home", lambda: Path("/var")):
            try:
                hlp.write_role_home(Path("/var/syrd692-not-made/home"), profile(t.port), shared_home=t.shared)
                ended = "written"
            except BaseException as exc:  # noqa: BLE001 - only the plain refusal is the answer
                ended = f"{type(exc).__name__}: {exc}"
        check(ended == "ProfileRefusal: /var does not belong to this account" and not Path("/var/syrd692-not-made").exists(),
              f"a home owned by another account is refused as such: {ended}")


def test_two_roles_share_the_service_and_nothing_else() -> None:
    with tenant(roles=("app", "director")) as (t, _server):
        t.prepare("app"), t.prepare("director")
        check(t.role_home("app") != t.role_home("director"), "each role its own HERMES_HOME")
        a, b = (t.role_home(name) / "config.yaml" for name in ("app", "director"))
        check(a.read_text(encoding="utf-8") == b.read_text(encoding="utf-8") and a.stat().st_ino != b.stat().st_ino,
              "the same profile, in two separate files")
        (t.role_home("app") / "state.db").write_text("app's sessions", encoding="utf-8")
        check(not (t.role_home("director") / "state.db").exists(), "one role's sessions are not the other's")
        for name in ("app", "director"):
            pane = t.command(name)
            check(f"HERMES_HOME={t.role_home(name)}" in pane, f"{name}'s pane runs in its own home")


def test_a_planted_link_redirects_no_write() -> None:
    with tenant() as (t, _server):
        home = t.role_home()
        home.mkdir(parents=True)
        victim = t.root / "victim.yaml"
        victim.write_text("victim\n", encoding="utf-8")
        (home / "config.yaml").symlink_to(victim)
        (home / ".env").symlink_to(t.root / "victim.env")
        t.prepare()
        check(victim.read_text(encoding="utf-8") == "victim\n" and (home / "config.yaml").is_file()
              and not (home / "config.yaml").is_symlink(), "a config.yaml link is replaced, never written through")
        check(not os.path.lexists(home / ".env"), "a credential link is removed, whatever it named")
        shutil.rmtree(home)
        elsewhere = t.root / "elsewhere"
        elsewhere.mkdir()
        home.symlink_to(elsewhere)
        refused = _refusal(t.prepare)
        check("is a link or not a directory" in refused and os.listdir(elsewhere) == [],
              f"a home that is a link is refused, and nothing is written where it points: {refused}")
        home.unlink()
        home.mkdir()
        (home / "config.yaml").write_text("model:\n  provider: openrouter\n", encoding="utf-8")
        notice = t.prepare()
        check("is the role's own, not Switchyard's" in notice
              and (home / "config.yaml").read_text(encoding="utf-8") == "model:\n  provider: openrouter\n",
              f"a config.yaml of the role's own is left alone and said: {notice}")


def test_the_pane_runs_the_check_first_with_no_hosted_key() -> None:
    with tenant() as (t, _server):
        t.prepare()
        pane = t.command()
        at = pane.index("check")
        check(pane[pane.index("--", at) + 1:] == ["hermes", "-m", MODEL, "--yolo", "--accept-hooks", "--pass-session-id"],
              f"the check, then exactly the Hermes argv a cloud role gets: {pane[at:]}")
        check(json.loads(pane[at + 4]) == profile(t.port).entry() and pane[at + 1:at + 3] == ["--role", "app"],
              "the check is handed the declared profile")
        unset = [pane[i + 1] for i, word in enumerate(pane) if word == "-u"]
        check({"OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CUSTOM_BASE_URL", "OPENAI_BASE_URL",
               "HERMES_INFERENCE_PROVIDER"} <= set(unset), f"the pane unsets every hosted key and override: {unset}")
        cloud = t.command("director")
        check("check" not in cloud and "OPENROUTER_API_KEY" not in cloud, "a role without a profile is started as before")
        check(any(word == "check" for word in t.command(resume=True)), "a resumed start is checked too")


def test_the_check_starts_hermes_only_on_the_declared_profile() -> None:
    with tenant() as (t, server):
        t.prepare()
        ok = start(t, ["/bin/echo", "HERMES STARTED"])
        check(ok.returncode == 0 and ok.stdout.strip() == "HERMES STARTED" and server.asked == ["/v1/models"],
              f"the endpoint serves exactly the profile: Hermes runs: {ok.returncode} {ok.stdout} {ok.stderr}")

        def refused(why: str, **kwargs) -> None:
            done = start(t, ["/bin/echo", "HERMES STARTED"], **kwargs)
            check(done.returncode == 1 and "HERMES STARTED" not in done.stdout and why in done.stderr
                  and "nothing falls back to a hosted model" in done.stderr,
                  f"refused, in the pane, in plain words: {why}: {done.returncode} {done.stdout} {done.stderr}")

        server.model = "Qwen/Qwen3-8B"
        refused("serves ['Qwen/Qwen3-8B'], not Qwen/Qwen3.8-27B")
        server.model, server.n_ctx = MODEL, 32768
        refused("holds a 32,768-token context, not the declared 65,536")
        server.n_ctx = 65536
        t.write_record(record(t.port, upstream_revision="f" * 40))
        refused("the service now pins something else (revision")
        t.write_record(record(t.port))
        refused("the environment carries OPENROUTER_API_KEY", environ={"OPENROUTER_API_KEY": "sk-or-leak"})
        refused("the environment carries MISTRAL_API_KEY", environ={"MISTRAL_API_KEY": "leak"})
        (t.role_home() / ".env").write_text("export ANTHROPIC_API_KEY='sk-ant-x'\nEDITOR=vi\n", encoding="utf-8")
        refused(".env holds ANTHROPIC_API_KEY")
        (t.role_home() / ".env").unlink()
        (t.role_home() / "auth.json").write_text("{}", encoding="utf-8")
        refused("auth.json holds hosted-provider sign-ins")
        (t.role_home() / "auth.json").unlink()
        (t.role_home() / "config.yaml").write_text("model:\n  provider: openrouter\n", encoding="utf-8")
        refused("is not this profile's config")
        t.prepare()
        server.close()
        refused("did not answer")
        check(start(t, ["/bin/echo", "x"], environ={"HERMES_HOME": ""}).returncode == 1, "no home: refused")


def test_a_context_hermes_refuses_is_said_before_and_at_the_start() -> None:
    small = profile(17431, settings={"host": "127.0.0.1", "port": 17431, "context": 32768})
    problem = hlp.context_problem(small)
    check("Hermes refuses any model below 64,000" in problem and "--local-qwen" in problem,
          f"the service's 32K context is named, with the remedy: {problem}")
    check(hlp.context_problem(profile()) == "", "64K and above: nothing to say")
    with tenant() as (t, server):
        server.n_ctx = 32768
        t.write_record(record(t.port, context=32768))
        t.raw["roles"][[r["role"] for r in t.raw["roles"]].index("app")]["hermes_local"]["context"] = 32768
        t.save(), t.prepare()
        done = start(t, ["/bin/echo", "HERMES STARTED"])
        check(done.returncode == 1 and "Hermes refuses any model below 64,000" in done.stderr,
              f"and at the start, before Hermes would refuse it itself: {done.stderr}")


def test_installed_hermes_resolves_only_the_local_endpoint() -> None:
    if not HERMES_PYTHON.exists() or shutil.which("unshare") is None:
        print(f"hermes_local_profile_test: {HERMES_PYTHON} is not installed here; the Hermes checks are SKIPPED")
        return
    with tenant() as (t, _server):
        t.prepare()
        home = t.root / "hermes-copy"
        shutil.copytree(t.role_home(), home, symlinks=True)
        script = r'''
import json
from hermes_cli.runtime_provider import resolve_runtime_provider
from hermes_cli.config import load_config
from agent.auxiliary_client import _resolve_task_provider_model
from agent.model_metadata import MINIMUM_CONTEXT_LENGTH
import sys
main = resolve_runtime_provider()
cfg = load_config()
aux = {task: list(_resolve_task_provider_model(task=task))[:4] for task in sys.argv[1:]}
print("RESOLVED " + json.dumps({"provider": main.get("provider"), "base_url": main.get("base_url"),
      "api_key": main.get("api_key"), "fallback": cfg.get("fallback_providers"), "fallback_model": cfg.get("fallback_model"),
      "model": cfg["model"].get("default"), "context": cfg["model"].get("context_length"), "aux": aux,
      "minimum": MINIMUM_CONTEXT_LENGTH}))
'''
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(home), "HERMES_HOME": str(home),
               # A hosted key in the environment: the explicit local provider must not reach for it.
               "OPENROUTER_API_KEY": "sk-or-AMBIENT", "OPENAI_API_KEY": "sk-AMBIENT"}
        done = subprocess.run(["unshare", "-rn", str(HERMES_PYTHON), "-I", "-c", script, *hlp.AUXILIARY_TASKS], env=env,
                              capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        line = next((line for line in reversed(done.stdout.splitlines()) if line.startswith("RESOLVED ")), "")
        check(done.returncode == 0 and line, f"the installed Hermes loaded the role's home: {done.stderr[-1500:]}")
        got = json.loads(line.removeprefix("RESOLVED "))
        url = f"http://127.0.0.1:{t.port}/v1"
        check(got["provider"] == "custom" and got["base_url"] == url and got["api_key"] == hlp.NO_KEY
              and got["model"] == MODEL and got["context"] == 65536,
              f"Hermes's own resolution: the main agent on the endpoint, its placeholder key, not the ambient one: {got}")
        check(got["fallback"] == [] and got["fallback_model"] in (None, ""), f"no fallback: {got['fallback']}")
        check(all(value == ["custom", MODEL, url, hlp.NO_KEY] for value in got["aux"].values()),
              f"every auxiliary task, by Hermes's own resolution, on the endpoint only: {got['aux']}")
        check(got["minimum"] == hlp.HERMES_MINIMUM_CONTEXT, f"the context floor is Hermes's own: {got['minimum']}")


def test_leaving_the_profile_removes_only_switchyards_material() -> None:
    from scripts import role_runtime

    with tenant() as (t, _server):
        t.prepare()
        entry = copy.deepcopy(next(r for r in t.raw["roles"] if r["role"] == "app"))
        kept = copy.deepcopy(entry)
        role_runtime.project_role_runtime(kept, runtime="hermes")
        check(kept["hermes_local"] == entry["hermes_local"], "a rewrite that keeps Hermes keeps the profile (upgrade, repair)")
        role_runtime.project_role_runtime(entry, runtime="codex", model="")
        check("hermes_local" not in entry and entry["cli"] == ["codex"], f"leaving Hermes takes the profile with it: {entry}")
        home = t.role_home()
        (home / "memories").mkdir()
        check(hlp.remove_role_config(home) and not (home / "config.yaml").exists() and (home / "memories").is_dir()
              and (home / "SOUL.md").is_symlink(), "only the marked config goes; the role's state stays")
        (home / "config.yaml").write_text("own\n", encoding="utf-8")
        check(not hlp.remove_role_config(home) and (home / "config.yaml").read_text(encoding="utf-8") == "own\n",
              "a config.yaml of the role's own is never removed")
        (home / "config.yaml").unlink()
        # Back on cloud Hermes (no profile): the next start links the shared config and credentials again.
        for role in t.raw["roles"]:
            if role["role"] == "app":
                role.pop("hermes_local"), role.update(model="glm-5.3")
        t.save(), t.prepare()
        check((home / "config.yaml").is_symlink() and (home / ".env").is_symlink(),
              "a cloud role gets the shared config and credentials back, as before")
        # And from a profile with an effort, a role leaving its profile gets the effort-only config (SYRD-546).
        for role in t.raw["roles"]:
            if role["role"] == "app":
                role.update(model=MODEL, hermes_local=profile(t.port).entry(), effort="high")
        t.save(), t.prepare()
        check(hlp.is_ours((home / "config.yaml").read_text(encoding="utf-8")) and "reasoning_effort: high" in
              (home / "config.yaml").read_text(encoding="utf-8"), "a local role keeps its effort in the profile's config")
        for role in t.raw["roles"]:
            if role["role"] == "app":
                role.pop("hermes_local"), role.update(model="glm-5.3")
        t.save(), t.prepare()
        text = (home / "config.yaml").read_text(encoding="utf-8")
        check(text.startswith("# Written by Switchyard for this role (SYRD-546)") and "127.0.0.1" not in text,
              f"off the profile, the effort-only config replaces it: {text[:200]}")


def test_set_role_runtime_writes_the_profile_and_puts_it_back_when_the_switch_fails() -> None:
    from scripts import project_role_runtime, role_runtime

    with tenant() as (t, _server):
        for role in t.raw["roles"]:
            if role["role"] == "app":
                role.pop("hermes_local"), role.update(model="glm-5.3")
        t.save()
        config = t.config()
        said: list[str] = []
        calls: list[dict] = []

        def switch(config, **kwargs):
            calls.append(kwargs)
            on_disk = json.loads(t.config_path.read_text(encoding="utf-8"))
            entry = next(r for r in on_disk["roles"] if r["role"] == "app")
            calls[-1]["on_disk"] = {key: entry.get(key) for key in ("model", "hermes_local")}
            if kwargs.get("dry_run") is False and getattr(switch, "fail", False):
                raise role_runtime.RoleRuntimeRefusal("switchyard: the board refused")
            return SimpleNamespace(configured_changed=True, describe=lambda: "switched", forced=False, reason="",
                                   role="app", previous_runtime="hermes", runtime=kwargs["runtime"])

        service = t.service / "service.json"
        local = lambda real=hlp.profile_from_service: real(service, anchor=t.root, owner_uid=os.getuid())  # noqa: E731
        with patch.object(role_runtime, "switch_role_runtime", switch), patch.object(hlp, "profile_from_service", local), \
                patch.object(team_launcher.runtime_catalog, "owner_model_catalog", lambda *a, **k: None):
            def run(**kwargs) -> int:
                return project_role_runtime.set_project_role_runtime_command(
                    config, config_path=t.config_path, role_name="app", interactive=False, print_func=said.append,
                    runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""), **kwargs)

            check(run(local=True, dry_run=True) == 0 and calls[-1]["model"] == MODEL and calls[-1]["runtime"] == "hermes"
                  and calls[-1]["on_disk"] == {"model": "glm-5.3", "hermes_local": None}
                  and any("would run hermes on local-qwen: Qwen/Qwen3.8-27B" in line for line in said),
                  f"a dry run says the profile and writes nothing: {calls[-1]} {said}")
            switch.fail = True
            check("the board refused" in _exit(lambda: run(local=True)), "the switch fails")
            on_disk = next(r for r in json.loads(t.config_path.read_text())["roles"] if r["role"] == "app")
            check(calls[-1]["on_disk"]["hermes_local"] == profile(t.port).entry() and calls[-1]["on_disk"]["model"] == MODEL
                  and "hermes_local" not in on_disk and on_disk["model"] == "glm-5.3",
                  f"written before the switch, put back when it fails: {calls[-1]['on_disk']} {on_disk}")
            switch.fail = False
            check(run(local=True) == 0, "enabled")
            on_disk = next(r for r in json.loads(t.config_path.read_text())["roles"] if r["role"] == "app")
            check(on_disk["hermes_local"] == profile(t.port).entry() and on_disk["model"] == MODEL,
                  f"the profile and its model stay: {on_disk}")
            config = t.config()
            check("--model" in _exit(lambda: run(runtime="hermes", local=False)), "leaving the profile needs the hosted model named")
            check("drop --model" in _exit(lambda: run(local=True, model="Qwen/Qwen3-8B")), "a different model is refused")
            check("not Hermes" in _exit(lambda: run(local=True, runtime="codex")), "a profile on another runtime is refused")
            check("pass --no-local-qwen" in _exit(lambda: run(runtime="hermes", model="glm-5.3")),
                  "a local role's model is changed only by leaving the profile")
            check(run(runtime="hermes", effort="high") == 0 and calls[-1]["model"] is None
                  and calls[-1]["on_disk"]["hermes_local"] == profile(t.port).entry(),
                  "a local role keeps its model, unchecked by the hosted catalog, through other changes")
            check(run(runtime="hermes", local=False, model="glm-5.3") == 0 and calls[-1]["on_disk"]["hermes_local"] is None,
                  "--no-local-qwen removes the profile before the switch")
            # Moving a local role off Hermes: nothing starts it as Hermes again, so its profile's config goes now.
            hlp.write_entry(config, t.config_path, "app", profile(t.port), runner=lambda *a, **k: None)
            config = t.config()
            t.prepare()
            (t.role_home() / "memories").mkdir()
            check(hlp.is_ours((t.role_home() / "config.yaml").read_text(encoding="utf-8")), "a local role's config is there")
            check(run(runtime="codex", model="gpt-5.5") == 0 and not (t.role_home() / "config.yaml").exists()
                  and (t.role_home() / "memories").is_dir(), "off Hermes: the profile's config is removed, the role's state kept")


def test_new_project_and_add_role_take_the_profile_from_the_service() -> None:
    from scripts import new_project_artifacts

    with tenant() as (t, _server):
        path = t.root / "p.json"
        clis = (("main", "hermes"), ("audit", "codex"))
        check(hlp.artifact_roles(["main"], path=path, role_clis=clis) == ("main",), "a Hermes role is taken")
        check("not Hermes roles: audit (codex)" in str(_exit(lambda: hlp.artifact_roles(
            ["audit"], path=path, role_clis=clis))), "a role on another runtime is refused")
        check(hlp.artifact_roles(None, path=path, role_clis=clis) == (), "absent: none")
        said: list[str] = []
        local = lambda real=hlp.profile_from_service: real(t.service / "service.json", anchor=t.root, owner_uid=os.getuid())  # noqa: E731
        with patch.object(hlp, "profile_from_service", local):
            role_local, models = hlp.new_project_roles(
                SimpleNamespace(local_qwen_roles=("main",)), {"audit": "gpt-5.5"}, print_func=said.append)
        check(role_local == {"main": profile(t.port).entry()} and models == {"audit": "gpt-5.5", "main": MODEL}
              and said == [f"switchyard: main will run hermes on {profile(t.port).describe()}"],
              f"switchyard new: the profile and its model for each local role, said: {said}")
        plan = SimpleNamespace(project="porter", owner_user="porter-agent", port=8765, socket_path="/run/x.sock",
                               ticket_prefix="POR", workflow=None, board_current=None)
        with patch.object(team_launcher, "_new_project_role_env", lambda *a, **k: {}):
            payload = new_project_artifacts._new_project_launcher_config_payload(
                plan, repository=t.root, role_clis=[("main", "hermes"), ("audit", "codex")], role_models=models,
                role_local=role_local)
        roles = {r["role"]: r for r in payload["roles"]}
        check(roles["main"].get("hermes_local") == profile(t.port).entry() and roles["main"].get("model") == MODEL
              and "hermes_local" not in roles["audit"], f"the generated config declares it: {roles['main']}")
    from scripts import switchyard_parsers

    args = switchyard_parsers._build_switchyard_add_role_parser().parse_args(["porter", "impl", "--local-qwen"])
    check(args.local_qwen is True, "add-role takes --local-qwen")
    parser = switchyard_parsers._build_switchyard_set_role_runtime_parser()
    check(parser.parse_args(["p", "r"]).local is None and parser.parse_args(["p", "r", "--local-qwen"]).local is True
          and parser.parse_args(["p", "r", "--no-local-qwen"]).local is False, "set-role-runtime takes either, or neither")


def _exit(call) -> str:
    try:
        call()
    except SystemExit as exc:
        return str(exc)
    return ""


def test_status_and_validation_show_the_profile_and_what_the_endpoint_says() -> None:
    from scripts import first_run_setup

    with tenant(roles=("app", "director")) as (t, server):
        config = t.config()
        lines = hlp.status_lines([config])
        check(lines[0].startswith("LOCAL MODEL ROLES") and f"  porter app: hermes on {profile(t.port).describe()}" in lines
              and "    endpoint: serving it, 65,536-token context" in lines and len(lines) == 5,
              f"status: each local role, its whole profile, and the endpoint's answer: {lines}")
        check(hlp.status_lines([SimpleNamespace(project="x", roles=[SimpleNamespace(role="r", hermes_local=None)])]) == [],
              "no local role: no section")
        said: list[str] = []
        record = lambda: t.read_record()  # noqa: E731
        check(hlp.validation_failures(config.roles, print_func=said.append, record_reader=record) == []
              and len(said) == 2, f"validate-models checks each local role against the service: {said}")
        server.close()
        failures = hlp.validation_failures(config.roles, print_func=said.append, record_reader=record)
        check(sorted(f.role for f in failures) == ["app", "director"] and "did not answer" in failures[0].suggestion,
              f"and says when the service does not serve it: {failures}")
        check("    endpoint: " in hlp.status_lines([config])[2] and "did not answer" in hlp.status_lines([config])[2],
              "status says so too")
        grouped = first_run_setup._roles_by_first_cli(config)
        check("app" not in [r.role for r in grouped.get("hermes", [])],
              f"a local role asks the owner's Hermes account for no sign-in: {grouped.keys()}")


def test_add_role_declares_the_profile_before_anything_outside_the_config() -> None:
    from scripts import project_role_add

    class Declared(Exception):
        """Stops the command at the declaration: everything after it is the board's, the units' and the pane's."""

    with tenant() as (t, _server):
        config = t.config()
        seen: dict = {}
        said: list[str] = []

        def written(cfg, *, config_path, role_name, cli, **kwargs):
            seen["written"] = (role_name, cli)
            return cfg, False

        def declare(cfg, path, role, local, *, runner):
            seen["declared"] = (role, local)
            raise Declared

        local = lambda real=hlp.profile_from_service: real(t.service / "service.json", anchor=t.root,  # noqa: E731
                                                            owner_uid=os.getuid())

        def add(**kwargs) -> str:
            """How the command ended: refused (its message), stopped at the declaration, or anything else, named."""
            try:
                project_role_add.add_project_role_command(
                    config, config_path=t.config_path, role_name="impl", detached=True, start=False, interactive=False,
                    print_func=said.append, **kwargs)
            except Declared:
                return "declared"
            except SystemExit as exc:
                return f"refused: {exc}"
            except Exception as exc:  # noqa: BLE001 - past the declaration into what this case stubs away
                return f"went on without declaring: {type(exc).__name__}: {exc}"
            return "returned"

        with patch.object(hlp, "profile_from_service", local), patch.object(hlp, "declare", declare), \
                patch.object(team_launcher, "_project_plan_for_added_role", lambda *a, **k: None), \
                patch.object(team_launcher, "render_add_role_sql", lambda *a, **k: ""), \
                patch.object(team_launcher, "_write_added_role_config", written):
            ended = add(local_qwen=True)
            check(ended == "declared" and seen == {"written": ("impl", "hermes"), "declared": ("impl", profile(t.port))}
                  and said == [f"switchyard: impl will run hermes on {profile(t.port).describe()}"],
                  f"add-role --local-qwen: a Hermes role, its profile declared next, and said: {ended} {seen} {said}")
            seen.clear()
            ended = add(local_qwen=True, cli="codex")
            check(ended.startswith("refused: ") and "is not Hermes" in ended and seen == {},
                  "--local-qwen with another runtime is refused before anything is written")
        # The declaration itself, on a cloud Hermes role: the profile and its model in the entry, read back; a second
        # time changes nothing.
        for entry in t.raw["roles"]:
            if entry["role"] == "app":
                entry.pop("hermes_local"), entry.update(model="glm-5.3")
        t.save()
        updated = hlp.declare(t.config(), t.config_path, "app", profile(t.port), runner=lambda *a, **k: None)
        role = team_launcher._role_by_name(updated, "app")
        check(role.hermes_local == profile(t.port) and role.model == MODEL, f"declared and read back: {role.model}")
        before = t.config_path.stat().st_mtime_ns
        check(hlp.declare(updated, t.config_path, "app", profile(t.port), runner=lambda *a, **k: None) is updated
              and t.config_path.stat().st_mtime_ns == before, "already declared: nothing written")


def test_validate_models_checks_a_local_role_against_the_service() -> None:
    from scripts import first_run_auth

    with tenant(roles=("app",)) as (t, server):
        server.close()
        from dataclasses import replace

        # Only the local role: nothing here may start another provider's CLI or reach a network.
        config = replace(t.config(), roles=[t.role("app")])
        said: list[str] = []
        stub = lambda args, **k: subprocess.CompletedProcess(args, 0, "", "")  # noqa: E731
        with patch.object(team_launcher, "validate_role_models", lambda roles, **k: []), \
                patch.object(hlp, "read_service_record", lambda *a, **k: t.read_record()):
            report = first_run_auth.run_first_run_auth_phase(
                config, owner_user=pwd.getpwuid(os.getuid()).pw_name, owner_home=t.home, validate_models=True,
                print_func=said.append,
                runner=stub, foreground_runner=stub)
        failures = [f for f in report.model_validation_failures if f.role == "app"]
        check(len(failures) == 1 and failures[0].model == MODEL and "did not answer" in failures[0].suggestion,
              f"validate-models: the local role is checked against the service, and its silence is a failure: "
              f"{report.model_validation_failures} {said[-3:]}")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"hermes_local_profile_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
