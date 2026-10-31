"""The phone bridge's configuration, credential, private links and service unit (SYRD-714).

**Configuration** is root's `/etc/switchyard/user-notify/config.json`: whether it is enabled, the provider, the
operator whose `operator:<name>` blockers count, the private base URL, which event kinds and which projects send,
and after how long an unanswered board is an outage. Nothing in it is secret. It is read on every pass, so a change
needs no restart, and installing or upgrading never writes it: a disabled bridge stays disabled.

**The credential** is root's `pushover.json` beside it: a regular file, not a link, owned by root and readable by
nobody else, read through a descriptor that refuses a link, at every send -- so a rotation takes effect on the next
message without restarting anything. It is written only by `set-credential`, from a descriptor or a prompt, never
from a command-line argument, and it is never rendered into tenant files, logs or board comments.

**Links** go to the board's own page over the tailnet: `https://<base host>:<board port>/#ticket=<ID>`. The port and
the ticket prefix come from root's unit for that project (`board_identity.unit_board_address`), so no tenant content
chooses where a link points, and an ID not of that project's prefix gets no link. The base host must be a tailnet
name (`*.ts.net`) or a tailnet address. A project's link is sent only while `tailscale serve status --json` shows
that exact host and port proxied to the board's loopback port, and never while Funnel would publish it. Nothing here
changes Tailscale: the preview prints the one `tailscale serve` command each missing project needs. The board still
decides what the page may do: over the proxy it acts as the User, as any HTTP caller does, and lends no Director
operation, since the connection is not the desktop owner's (ui_user_identity). No write token is ever in a link.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import stat
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from scripts import user_notify_pushover as pushover

SCHEMA = "switchyard.user-notify.v1"
UNIT = "switchyard-user-notify.service"
INSTALLED_SCRIPT = "/opt/switchyard/current/scripts/user_notify.py"
KINDS = ("user_review", "operator_blocker", "operation_failed", "project_outage", "director_ad_hoc")
PROVIDERS = ("pushover",)
NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
PROJECT = re.compile(r"[a-z][a-z0-9-]{0,31}")
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
MAX_FILE_BYTES = 64 << 10
DEFAULTS: dict[str, Any] = {
    "schema": SCHEMA, "enabled": False, "provider": "pushover", "operator": "", "base_url": "",
    "events": {kind: True for kind in KINDS}, "projects": {}, "outage_minutes": 15,
    # Whether a Director's urgent ad hoc notification may go at high priority, past the phone's quiet hours: the
    # operator's decision, separate from the Director's request.
    "director_urgent": False,
}


class SetupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Paths:
    config_dir: Path = Path("/etc/switchyard/user-notify")
    state_dir: Path = Path("/var/lib/switchyard/user-notify")
    registry_dir: Path = Path("/etc/switchyard/projects")
    unit_dir: Path = Path("/etc/systemd/system")
    owner_uid: int = 0

    @property
    def config(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def credential(self) -> Path:
        return self.config_dir / "pushover.json"

    @property
    def outbox(self) -> Path:
        return self.state_dir / "outbox.sqlite3"


@dataclass
class Config:
    values: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DEFAULTS)))
    problem: str = ""

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    @property
    def ready(self) -> bool:
        return not self.problem and bool(self.values["operator"]) and bool(self.values["base_url"])


def _read_root_file(path: Path, *, owner_uid: int, private: bool) -> bytes | None:
    """The file's bytes as root keeps it, or None when it does not exist. A link, another owner, a file others may
    write (or, for `private`, read) is refused."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SetupError(f"cannot read {path} ({exc.strerror})") from None
    try:
        info = os.fstat(fd)
        loose = stat.S_IRWXG | stat.S_IRWXO if private else stat.S_IWGRP | stat.S_IWOTH
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & loose:
            raise SetupError(f"{path} must be a regular file owned by root and "
                             f"{'readable by nobody else' if private else 'writable by nobody else'}")
        data = os.read(fd, MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise SetupError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
    return data


def write_root_file(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def validate(values: dict[str, Any]) -> dict[str, Any]:
    """The configuration, complete and checked; raises SetupError naming the first problem."""
    unknown = sorted(set(values) - set(DEFAULTS))
    if unknown:
        raise SetupError(f"unknown configuration keys: {', '.join(unknown)}")
    merged = {**json.loads(json.dumps(DEFAULTS)), **values}
    merged["events"] = {**DEFAULTS["events"], **(values.get("events") or {})}
    if merged["schema"] != SCHEMA:
        raise SetupError(f"the configuration is not {SCHEMA}")
    if type(merged["enabled"]) is not bool or type(merged["director_urgent"]) is not bool:
        raise SetupError("enabled and director_urgent must be true or false")
    if merged["provider"] not in PROVIDERS:
        raise SetupError(f"provider must be one of: {', '.join(PROVIDERS)}")
    if merged["operator"] and not NAME.fullmatch(str(merged["operator"])):
        raise SetupError("operator must be a name as operator:<name> blockers write it ([a-z][a-z0-9_]*)")
    if merged["base_url"]:
        merged["base_url"] = base_url(str(merged["base_url"]))
    if set(merged["events"]) - set(KINDS) or any(type(v) is not bool for v in merged["events"].values()):
        raise SetupError(f"events maps each of {', '.join(KINDS)} to true or false")
    projects = merged["projects"]
    if not isinstance(projects, dict) or any(not PROJECT.fullmatch(str(k)) or type(v) is not bool
                                             for k, v in projects.items()):
        raise SetupError("projects maps a project slug to true or false")
    minutes = merged["outage_minutes"]
    if type(minutes) is not int or not 5 <= minutes <= 1440:
        raise SetupError("outage_minutes must be a whole number from 5 to 1440")
    return merged


def load_config(paths: Paths) -> Config:
    try:
        data = _read_root_file(paths.config, owner_uid=paths.owner_uid, private=False)
        if data is None:
            return Config()
        document = json.loads(data.decode("utf-8"))
        if not isinstance(document, dict):
            raise SetupError(f"{paths.config} is not a JSON object")
        return Config(validate(document))
    except (SetupError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return Config(problem=f"configuration unusable, nothing is sent: {exc}")


def save_config(paths: Paths, values: dict[str, Any]) -> None:
    write_root_file(paths.config, json.dumps(validate(values), indent=2, sort_keys=True) + "\n", 0o644)


def base_url(text: str) -> str:
    parts = urllib.parse.urlsplit(text.strip())
    host = parts.hostname or ""
    if (parts.scheme != "https" or parts.username or parts.password or parts.port or parts.query or parts.fragment
            or parts.path not in ("", "/") or not host):
        raise SetupError("base_url must be https://<tailnet host>, with no port, path, query or credentials: each "
                         "project's link adds its own board's port")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if (len(labels) < 3 or labels[-2:] != ["ts", "net"]
                or not all(re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
            raise SetupError(f"{host} is not a tailnet name (<machine>.<tailnet>.ts.net); links must stay private")
        return f"https://{host}"
    if address not in TAILNET_V4 and address not in TAILNET_V6:
        raise SetupError(f"{host} is not a tailnet address (100.64.0.0/10 or fd7a:115c:a1e0::/48)")
    return f"https://[{address}]" if address.version == 6 else f"https://{address}"


def link(base: str, port: int, prefix: str, ticket: str) -> str:
    """The board page for one ticket of this project, or for the board itself; '' when `ticket` is not the project's."""
    if ticket and not re.fullmatch(rf"{re.escape(prefix)}-[0-9]{{1,9}}", ticket):
        return ""
    return f"{base}:{port}/" + (f"#ticket={ticket}" if ticket else "")


def serve_host(base: str) -> str:
    return base.removeprefix("https://")


def serve_status(runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict[str, Any]:
    result = runner(["tailscale", "serve", "status", "--json"], capture_output=True, text=True, timeout=15,
                    stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise SetupError(f"tailscale serve status failed: {pushover.clean_text(result.stderr, 200)}")
    document = json.loads(result.stdout or "{}")
    if not isinstance(document, dict):
        raise SetupError("tailscale serve status --json answered no object")
    return document


def served(document: dict[str, Any], base: str, port: int) -> str:
    """'' when this host and port are served to the tailnet from the board's loopback port, and not by Funnel;
    otherwise why not."""
    where = f"{serve_host(base)}:{port}"
    configs = [document, *[v for v in (document.get("Foreground") or {}).values() if isinstance(v, dict)]]
    for config in configs:
        if (config.get("AllowFunnel") or {}).get(where):
            return f"refused: Funnel publishes {where} to the internet; turn it off (tailscale funnel --https={port} off)"
    for config in configs:
        handler = (((config.get("Web") or {}).get(where) or {}).get("Handlers") or {}).get("/") or {}
        if handler.get("Proxy") in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return ""
    return f"not served: run `tailscale serve --bg --https={port} http://127.0.0.1:{port}`"


def read_credential(paths: Paths) -> dict[str, str]:
    data = _read_root_file(paths.credential, owner_uid=paths.owner_uid, private=True)
    if data is None:
        raise SetupError(f"no Pushover credential: run `switchyard-user-notify set-credential` as root")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SetupError(f"{paths.credential} is not JSON") from None
    token, user = (document.get(k) if isinstance(document, dict) else None for k in ("app_token", "user_key"))
    if not (isinstance(token, str) and isinstance(user, str)
            and pushover.CREDENTIAL.fullmatch(token) and pushover.CREDENTIAL.fullmatch(user)):
        raise SetupError(f"{paths.credential} holds no application token and user key")
    return {"app_token": token, "user_key": user}


def save_credential(paths: Paths, lines: list[str]) -> None:
    words = [line.strip() for line in lines if line.strip()]
    if len(words) != 2 or not all(pushover.CREDENTIAL.fullmatch(word) for word in words):
        raise SetupError("expected two lines: the Pushover application token, then the user key (30 letters and "
                         "digits each); nothing was written")
    os.makedirs(paths.config_dir, mode=0o755, exist_ok=True)
    write_root_file(paths.credential, json.dumps({"app_token": words[0], "user_key": words[1]}) + "\n", 0o600)


def registered(paths: Paths) -> list[str]:
    """Every project root registered on this host, by its record's name."""
    try:
        names = sorted(entry.name[:-len(".json")] for entry in os.scandir(paths.registry_dir)
                       if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False))
    except FileNotFoundError:
        return []
    return [name for name in names if PROJECT.fullmatch(name)]


def render_unit(script: str = INSTALLED_SCRIPT) -> str:
    """One host service for every tenant. It reads boards and root's records, writes only its own state."""
    return f"""[Unit]
Description=Switchyard user notifications (one phone bridge for every tenant board)
Documentation=file:///opt/switchyard/current/docs/user-notifications.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I -B {script} serve
Restart=always
RestartSec=10s
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
StateDirectory=switchyard/user-notify
StateDirectoryMode=0700
UMask=0077
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def install(paths: Paths, *, apply: bool, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
            script: str = INSTALLED_SCRIPT, out: Callable[[str], None] = print, euid: Callable[[], int] | None = None
            ) -> int:
    """Write the unit. A first install enables and starts it; a reinstall restarts it only if it was running, and
    leaves whether it is enabled alone. The configuration is never written, so neither is its enabled state."""
    unit_path = paths.unit_dir / UNIT
    first = not unit_path.exists()
    steps = [f"write {unit_path} running {script}", "systemctl daemon-reload",
             f"systemctl enable --now {UNIT}" if first else f"systemctl try-restart {UNIT} (only if it is running)"]
    if not apply:
        out("switchyard-user-notify: install (plan; nothing was changed)\n"
            + "".join(f"  {n}. {step}\n" for n, step in enumerate(steps, 1))
            + f"  {paths.config} is not written: notifications stay {'as configured' if paths.config.exists() else 'off until enabled'}.")
        return 0
    if (euid or os.geteuid)() != 0:
        out("switchyard-user-notify: install --apply runs as root")
        return 1
    write_root_file(unit_path, render_unit(script), 0o644)
    for argv in (["systemctl", "daemon-reload"],
                 ["systemctl", "enable", "--now", UNIT] if first else ["systemctl", "try-restart", UNIT]):
        result = runner(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            out(f"switchyard-user-notify: {shlex.join(argv)} failed: {pushover.clean_text(result.stderr, 300)}")
            return 1
    out(f"switchyard-user-notify: {'installed and started' if first else 'reinstalled'} {UNIT}; "
        f"its configuration was left as it was")
    return 0
