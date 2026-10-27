"""One opt-in, host-wide local inference service for the exact model Qwen/Qwen3.8-27B (SYRD-691).

Hermes roles that run on a local model reach it as an OpenAI-compatible endpoint on loopback (SYRD-690). This
module is the host side of that: one shared service, never one per tenant or role, that the operator opts into
explicitly and can remove completely.

Why llama.cpp and this quantization, measured on the host it was qualified on (RTX 4070, 12 GiB, of which the
desktop already holds about 1.8 GiB; Ryzen 9 9950X; 172 GiB RAM; about 63 GB/s of sustained read bandwidth):

- BF16 is 55.6 GB and the official FP8 checkpoint 30.9 GB. vLLM and SGLang keep weights in VRAM; with CPU
  offload, every generated token would move about 19 GB over PCIe, about one token a second. Neither is usable
  on 12 GiB.
- llama.cpp runs the layers it cannot fit on the CPU, from RAM, so the cost per token is RAM bandwidth, not
  PCIe. The llama.cpp project's own conversion (`ggml-org/Qwen3.8-27B-GGUF`, architecture `qwen35`) at Q4_K_M
  is 19.0 GB: about 7 GB on the GPU and 12 GB from RAM, so an estimated 4-5 tokens a second.
- llama-server parses Qwen3.8's reasoning into `reasoning_content` and its tool calls into structured
  `tool_calls` (`--jinja`, `--reasoning-format deepseek`). `probe` proves that against the running server and
  rejects one that only writes tool markup as text.

Everything is pinned: the upstream model revision, the GGUF repository revision, the file's sha256, and the
llama.cpp release tag, commit and asset digest. A newer Qwen or llama.cpp is a new pin, a new packet and a new
approval -- never drift. `preview` changes nothing and prints the approval packet; `install` needs the packet's
digest (`--approve`), so what the User approved is exactly what is installed. Nothing here enables the service
at boot, and no project launch starts it or downloads anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

# Hermes refuses a model whose window is below this (SYRD-692), so the service never holds one.
from scripts.hermes_local_profile import HERMES_MINIMUM_CONTEXT

SCHEMA = "switchyard.local-model.v1"
#: The rollout journal's name for this host service. Tenant slugs cannot contain "-", so no tenant shares it.
JOURNAL_NAME = "local-model"
UNIT_NAME = "switchyard-local-model.service"
SERVICE_USER = "switchyard-local-model"

MODEL_ID = "Qwen/Qwen3.8-27B"
UPSTREAM_REVISION = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
LICENSE = "apache-2.0"
GGUF_REPO = "ggml-org/Qwen3.8-27B-GGUF"
GGUF_REVISION = "a316ea527fde32c12487be7574e2db5ac2ad2a7e"
QUANTIZATION = "Q4_K_M"
MODEL_FILE = "Qwen3.8-27B-Q4_K_M.gguf"
MODEL_SIZE = 18_973_870_528
MODEL_SHA256 = "c600de0300ae8a0eb3a6c0b8b5561b8b96f16bd2c863c2a66c42de29d391a747"

ENGINE = "llama.cpp"
ENGINE_TAG = "b11803"
ENGINE_COMMIT = "a37cc46104eb50900d5ab4a9ba02a0abdac039db"
ENGINE_ASSET = "llama-b11803-bin-ubuntu-cuda-13.4-x64.tar.gz"
ENGINE_SIZE = 158_781_355
ENGINE_SHA256 = "ef7f81e8747e485e1856294c89a25a558e8e34dd95d295a3ef3f503b05fe2bae"
#: The asset links against the CUDA 13.4 runtime and cuBLAS; the host's own CUDA package provides them.
ENGINE_RUNTIME = "CUDA 13.4 runtime and cuBLAS from the host's cuda package (libcudart.so.13, libcublas.so.13)"

LOOPBACK_HOSTS = ("127.0.0.1", "::1")
#: What may be tuned after measuring, without a new approval: how much of the model sits on the GPU, and how
#: many CPU threads read the rest. Both are bounded, and the packet states the bounds; nothing else is tunable.
TUNABLE = {"gpu_layers": (16, 40), "threads": (4, 16)}
#: Every server flag this module renders, checked against the installed `llama-server --help` before install
#: finishes, so a release that renamed one is refused rather than started.
SERVER_FLAGS = ("--model", "--alias", "--host", "--port", "--ctx-size", "--n-gpu-layers", "--threads",
                "--parallel", "--flash-attn", "--cache-type-k", "--cache-type-v", "--jinja", "--reasoning-format",
                "--metrics", "--no-webui", "--no-slots", "--cors-origins", "--no-cors-credentials",
                "--temp", "--top-p", "--top-k", "--min-p")
#: An origin no web page can have (RFC 2606 `.invalid`): llama-server reflects any Origin by default, so a page
#: open in a browser on this host could drive the model and read its answers. Nothing that uses this service is
#: a browser, so no origin is allowed.
NO_BROWSER_ORIGIN = "https://cors.invalid"
NVIDIA_DEVICES = ("/dev/nvidiactl", "/dev/nvidia0", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools", "/dev/nvidia-modeset")
PROBE_TOOL = {"type": "function", "function": {
    "name": "get_weather", "description": "Current weather for a city.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}
#: How tool markup looks when a server hands it back as text instead of parsing it.
PSEUDO_TOOL_MARKERS = ("<tool_call>", "</tool_call>", "<function=", "\"name\": \"get_weather\"")


class Refusal(Exception):
    """Why this module will not do what it was asked; nothing was changed when it is raised before a step."""


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 17431
    #: Tokens. Hermes refuses a context below 64,000 (SYRD-692). Only 16 of the 64 layers are full attention
    #: (4 KV heads, dim 256), so a q8_0 cache costs about 32 KiB a token: 2 GiB at 64k, of which the 6
    #: attention layers among the 24 on the GPU hold 0.8 GiB.
    context: int = 65_536
    #: Layers on the GPU. A start that does not fit in VRAM fails at load, once; `--gpu-layers` lowers it.
    gpu_layers: int = 24
    threads: int = 12
    #: One request at a time: a second waits rather than splitting a 12 GiB card and the CPU between two.
    parallel: int = 1
    kv_cache_type: str = "q8_0"
    memory_high: str = "28G"
    memory_max: str = "32G"
    cpu_weight: int = 20
    #: What the desktop keeps: the service is sized so that this much VRAM stays free.
    vram_reserve_mib: int = 1536

    def problems(self) -> list[str]:
        found = []
        if self.host not in LOOPBACK_HOSTS:
            found.append(f"--host {self.host} is not loopback; the service binds only {' or '.join(LOOPBACK_HOSTS)}")
        if not 1024 <= self.port <= 65535:
            found.append(f"--port {self.port} is not an unprivileged port")
        if not HERMES_MINIMUM_CONTEXT <= self.context <= 262_144:
            found.append(f"--context {self.context} is outside {HERMES_MINIMUM_CONTEXT}..262144: Hermes refuses a "
                         f"context below {HERMES_MINIMUM_CONTEXT:,} tokens")
        for name, (low, high) in TUNABLE.items():
            if not low <= getattr(self, name) <= high:
                found.append(f"--{name.replace('_', '-')} {getattr(self, name)} is outside the approved {low}..{high}")
        if self.parallel != 1:
            found.append("--parallel is 1: one request at a time on this hardware")
        return found


@dataclass(frozen=True)
class Layout:
    engine_root: Path = Path("/opt/switchyard/local-models")
    state_root: Path = Path("/var/lib/switchyard/local-models")
    unit_dir: Path = Path("/etc/systemd/system")
    #: Where the ownership walk starts, and whose every directory below it must be: / and root, so the whole
    #: chain is checked. A suite anchors at its own tree and its own uid.
    anchor: Path = Path("/")
    owner_uid: int = 0

    @property
    def engine_dir(self) -> Path:
        return self.engine_root / ENGINE / ENGINE_TAG

    @property
    def model_dir(self) -> Path:
        return self.state_root.joinpath(*GGUF_REPO.split("/")) / GGUF_REVISION

    @property
    def model_path(self) -> Path:
        return self.model_dir / MODEL_FILE

    @property
    def record_path(self) -> Path:
        return self.state_root / "service.json"

    @property
    def unit_path(self) -> Path:
        return self.unit_dir / UNIT_NAME


def model_url() -> str:
    return f"https://huggingface.co/{GGUF_REPO}/resolve/{GGUF_REVISION}/{MODEL_FILE}"


def engine_url() -> str:
    return f"https://github.com/ggml-org/llama.cpp/releases/download/{ENGINE_TAG}/{ENGINE_ASSET}"


# ------------------------------------------------------------------ what would run


def server_argv(server: Path, settings: Settings, layout: Layout) -> list[str]:
    """The one llama-server command line: the pinned file by path, never a download (no `-hf`, no `--model-url`)."""
    return [str(server), "--model", str(layout.model_path), "--alias", MODEL_ID,
            "--host", settings.host, "--port", str(settings.port), "--ctx-size", str(settings.context),
            "--n-gpu-layers", str(settings.gpu_layers), "--threads", str(settings.threads),
            "--parallel", str(settings.parallel), "--flash-attn", "on",
            "--cache-type-k", settings.kv_cache_type, "--cache-type-v", settings.kv_cache_type,
            "--jinja", "--reasoning-format", "deepseek", "--metrics", "--no-webui",
            # Live per-request state stays private; load is read from /metrics instead.
            "--no-slots", "--cors-origins", NO_BROWSER_ORIGIN, "--no-cors-credentials",
            # Qwen's recommended sampling for thinking mode (model card).
            "--temp", "1.0", "--top-p", "0.95", "--top-k", "20", "--min-p", "0"]


def render_unit(server: Path, settings: Settings, layout: Layout) -> str:
    """The one system unit. No [Install] section: it never starts at boot, and nothing but `start` starts it."""
    command = " ".join(shlex.quote(part) for part in server_argv(server, settings, layout))
    devices = "\n".join(f"DeviceAllow={device} rw" for device in NVIDIA_DEVICES)
    return f"""# Generated by `switchyard local-model install` (SYRD-691). Do not edit; re-run install instead.
[Unit]
Description=Switchyard local model service ({MODEL_ID} {QUANTIZATION}, {ENGINE} {ENGINE_TAG})
After=network.target
# A start that keeps failing (no GPU memory, a corrupt file) stops after two tries in 15 minutes.
StartLimitIntervalSec=900
StartLimitBurst=2

[Service]
Type=exec
DynamicUser=yes
User={SERVICE_USER}
ExecStart={command}
Restart=on-failure
RestartSec=30
TimeoutStartSec=300
TimeoutStopSec=30
Nice=10
CPUWeight={settings.cpu_weight}
IOWeight={settings.cpu_weight}
MemoryHigh={settings.memory_high}
MemoryMax={settings.memory_max}
MemorySwapMax=0
TasksMax=256
OOMPolicy=stop
OOMScoreAdjust=500
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
IPAddressDeny=any
IPAddressAllow=localhost
DevicePolicy=closed
{devices}
ReadOnlyPaths={layout.engine_dir} {layout.model_dir}
"""


@dataclass(frozen=True)
class Packet:
    """What the User approves: every fact that decides what is downloaded, installed and run."""

    body: dict[str, Any]
    #: Measured now, shown, and not part of the digest: the desktop's VRAM use changes minute to minute.
    observed: dict[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def approval_packet(settings: Settings, layout: Layout, *, observed: dict[str, Any] | None = None) -> Packet:
    """The packet for `settings`, with the tunable values at their defaults: tuning them within their stated
    bounds is part of what is approved, so it does not change the digest."""
    settings = Settings(**{**asdict(settings), **{name: getattr(Settings(), name) for name in TUNABLE}})
    server = layout.engine_dir / "llama-server"
    kv_gib = round(settings.context * 16 * 2 * 4 * 256 * (1 if settings.kv_cache_type == "q8_0" else 2) / 2**30, 2)
    body = {
        "schema": SCHEMA,
        "model": {"id": MODEL_ID, "upstream_revision": UPSTREAM_REVISION, "license": LICENSE,
                  "artifact_repository": GGUF_REPO, "artifact_revision": GGUF_REVISION, "file": MODEL_FILE,
                  "quantization": QUANTIZATION, "bytes": MODEL_SIZE, "sha256": MODEL_SHA256, "url": model_url()},
        "engine": {"name": ENGINE, "tag": ENGINE_TAG, "commit": ENGINE_COMMIT, "asset": ENGINE_ASSET,
                   "bytes": ENGINE_SIZE, "sha256": ENGINE_SHA256, "url": engine_url(), "runtime": ENGINE_RUNTIME},
        "packages_and_containers": "none: no pacman, pip or container image is installed or changed",
        "paths": {"engine": str(layout.engine_dir), "model": str(layout.model_path),
                  "record": str(layout.record_path), "unit": str(layout.unit_path)},
        "service": {"unit": UNIT_NAME, "identity": f"systemd DynamicUser {SERVICE_USER} (no login, no home)",
                    "listen": f"http://{settings.host}:{settings.port}/v1 (loopback only; IPAddressAllow=localhost)",
                    "starts": "only on `switchyard local-model start`; never at boot, never by a project launch",
                    "command": server_argv(server, settings, layout),
                    "tunable": {name: f"{low}..{high}, default {getattr(Settings(), name)}"
                                for name, (low, high) in TUNABLE.items()},
                    "command_note": ("llama-server's path is wherever the pinned archive puts it inside the engine "
                                     "directory; install records that exact path and its sha256"),
                    "limits": {"MemoryHigh": settings.memory_high, "MemoryMax": settings.memory_max,
                               "CPUWeight": settings.cpu_weight, "Nice": 10, "parallel_requests": settings.parallel,
                               "restarts": "on failure, at most 2 starts in 15 minutes"}},
        "sizing": {"disk_bytes": MODEL_SIZE + ENGINE_SIZE, "disk_note": "plus about 0.5 GB extracted engine",
                   "context_tokens": settings.context, "kv_cache_gib": kv_gib,
                   "gpu_layers": settings.gpu_layers, "vram_reserved_for_desktop_mib": settings.vram_reserve_mib,
                   "estimate": ("about 7 GB of weights and 0.8 GiB of KV cache on the GPU (about 8.4 GiB in all), "
                                "about 12 GB read from RAM per token; about 4-5 "
                                "tokens/s generated (host measured at about 63 GB/s read). Thinking is on by "
                                "default, so a turn can take minutes. Measured after install, not promised."),
                   "ram": f"about 15 GiB (12 GiB of weights, 2-3 GiB of KV cache and buffers), capped at MemoryMax {settings.memory_max}"},
        "network": "install downloads from huggingface.co and github.com only; the service itself may reach localhost only",
        "rollback": ("`switchyard local-model stop`, then `remove`: stops and deletes the unit, the engine "
                     "directory, the model directory and the record, and nothing else. Re-pinning is a new packet."),
    }
    return Packet(body, observed or {})


def render_packet(packet: Packet) -> list[str]:
    b = packet.body
    m, e, s, z = b["model"], b["engine"], b["service"], b["sizing"]
    lines = [
        "switchyard: local model service -- approval packet (nothing has been downloaded or installed)",
        f"  model:    {m['id']} @ {m['upstream_revision']} ({m['license']})",
        f"  artifact: {m['artifact_repository']} @ {m['artifact_revision']}",
        f"            {m['file']} ({m['quantization']}), {m['bytes']} bytes ({m['bytes'] / 1e9:.2f} GB)",
        f"            sha256 {m['sha256']}",
        f"            from {m['url']}",
        f"  engine:   {e['name']} {e['tag']} (commit {e['commit']})",
        f"            {e['asset']}, {e['bytes']} bytes, sha256 {e['sha256']}",
        f"            from {e['url']}",
        f"            uses {e['runtime']}",
        f"  packages: {b['packages_and_containers']}",
        f"  paths:    engine {b['paths']['engine']}",
        f"            model  {b['paths']['model']}",
        f"            record {b['paths']['record']}",
        f"            unit   {b['paths']['unit']}",
        f"  service:  {s['unit']}, {s['identity']}",
        f"            {s['listen']}",
        f"            {s['starts']}",
        f"            limits: " + ", ".join(f"{k} {v}" for k, v in s["limits"].items()),
        f"            command: {' '.join(shlex.quote(part) for part in s['command'])}",
        f"            ({s['command_note']})",
        f"            tunable after measuring, without a new approval: "
        + ", ".join(f"--{k.replace('_', '-')} {v}" for k, v in s["tunable"].items()),
        f"  sizing:   disk {z['disk_bytes'] / 1e9:.2f} GB ({z['disk_note']}); context {z['context_tokens']} tokens, "
        f"KV cache {z['kv_cache_gib']} GiB; {z['gpu_layers']} layers on the GPU, "
        f"{z['vram_reserved_for_desktop_mib']} MiB kept for the desktop",
        f"            RAM {z['ram']}",
        f"            {z['estimate']}",
        f"  network:  {b['network']}",
        f"  rollback: {b['rollback']}",
    ]
    for key, value in sorted(packet.observed.items()):
        lines.append(f"  now:      {key}: {value}")
    lines.append(f"switchyard: approve exactly this with `sudo switchyard local-model install --approve {packet.digest}`")
    return lines


def observe_host(runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict[str, Any]:
    """Read-only facts shown beside the packet: the GPU and what the desktop already uses, and the free disk."""
    found: dict[str, Any] = {}
    try:
        done = runner(["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                      capture_output=True, text=True, timeout=10, check=False)
        name, total, used = [part.strip() for part in done.stdout.splitlines()[0].split(",")]
        found["gpu"] = f"{name}, {used} of {total} MiB in use"
    except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
        found["gpu"] = "nvidia-smi did not answer"
    try:
        usage = shutil.disk_usage(Path("/var/lib"))
        found["disk_free_var_lib"] = f"{usage.free / 1e9:.1f} GB"
    except OSError:
        pass
    return found


# ------------------------------------------------------------------ root-owned paths, without following links


def _ensure_root_directory(path: Path, layout: Layout, *, create: bool = True) -> None:
    """Every component from the anchor (/) to `path` a real directory, root-owned and writable by root alone;
    create what is missing (0755). Refuses a symlink anywhere, so no write here can land where a link points."""
    current = layout.anchor
    for part in path.relative_to(layout.anchor).parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if not create:
                raise Refusal(f"{current} does not exist") from None
            os.mkdir(current, 0o755)
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise Refusal(f"{current} is a symlink or not a directory")
        if info.st_uid != layout.owner_uid or info.st_mode & 0o022:
            raise Refusal(f"{current} is not root's alone (owner uid {info.st_uid}, mode {oct(info.st_mode & 0o777)})")


def _write_root_file(path: Path, text: str, mode: int, layout: Layout) -> bool:
    """Write `text` at `path` atomically in a root-owned directory; False when it already says exactly that."""
    _ensure_root_directory(path.parent, layout)
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise Refusal(f"{path} is a symlink or not a regular file")
        if Path(path).read_text(encoding="utf-8") == text and stat.S_IMODE(info.st_mode) == mode:
            return False
    except FileNotFoundError:
        pass
    staged = path.with_name(f".{path.name}.{os.getpid()}.new")
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.write(descriptor, text.encode("utf-8"))
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)
    os.replace(staged, path)
    return True


def sha256_file(path: Path, *, chunk: int = 16 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb", buffering=0) as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, destination: Path, *, size: int, sha256: str, print_func: Callable[[str], None] = print,
          opener: Callable[..., Any] = urllib.request.urlopen) -> None:
    """Download `url` to `destination`, resuming a partial one, and keep it only if its size and sha256 match."""
    partial = destination.with_name(destination.name + ".partial")
    have = partial.stat().st_size if partial.exists() else 0
    if have > size:
        partial.unlink()
        have = 0
    if have < size:
        request = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        with opener(request, timeout=60) as response, open(partial, "ab" if have else "wb") as out:
            if have and getattr(response, "status", 206) != 206:
                raise Refusal(f"{url} did not resume the partial download (HTTP {getattr(response, 'status', '?')})")
            reported = time.monotonic()
            while block := response.read(8 << 20):
                out.write(block)
                have += len(block)
                if time.monotonic() - reported > 30:
                    print_func(f"switchyard: {destination.name}: {have / 1e9:.2f} of {size / 1e9:.2f} GB")
                    reported = time.monotonic()
    got = sha256_file(partial)
    if partial.stat().st_size != size or got != sha256:
        partial.unlink()
        raise Refusal(f"{url} did not match its pin (sha256 {got}, {have} bytes); the download was discarded")
    os.chmod(partial, 0o644)
    os.replace(partial, destination)


def _extract_engine(archive: Path, layout: Layout) -> Path:
    """Unpack the engine into its own directory and return its llama-server. The tar `data` filter refuses
    absolute paths, `..`, devices and links that leave the directory, and strips set-id bits."""
    _ensure_root_directory(layout.engine_dir.parent, layout)
    staging = layout.engine_dir.with_name(f".{ENGINE_TAG}.{os.getpid()}.staging")
    if staging.exists():
        shutil.rmtree(staging)
    os.mkdir(staging, 0o755)
    with tarfile.open(archive) as tar:
        tar.extractall(staging, filter="data")
    servers = [p for p in staging.rglob("llama-server") if p.is_file() and not p.is_symlink()]
    if len(servers) != 1:
        shutil.rmtree(staging)
        raise Refusal(f"{archive.name} holds {len(servers)} llama-server programs, not one")
    if layout.engine_dir.exists():
        shutil.rmtree(layout.engine_dir)
    os.replace(staging, layout.engine_dir)
    return layout.engine_dir / servers[0].relative_to(staging)


# ------------------------------------------------------------------ the commands


def preview(settings: Settings, layout: Layout, *, print_func: Callable[[str], None] = print,
            runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> int:
    for line in render_packet(approval_packet(settings, layout, observed=observe_host(runner))):
        print_func(line)
    return 0


def read_record(layout: Layout) -> dict[str, Any] | None:
    try:
        info = os.lstat(layout.record_path)
        if stat.S_ISLNK(info.st_mode) or info.st_uid != layout.owner_uid:
            return None
        record = json.loads(layout.record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("schema") == SCHEMA else None


def install(settings: Settings, layout: Layout, *, approve: str, print_func: Callable[[str], None] = print,
            runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
            fetcher: Callable[..., None] = fetch, euid: Callable[[], int] = os.geteuid) -> int:
    """Download, verify and install exactly the approved packet; start nothing. Run again, it changes nothing."""
    packet = approval_packet(settings, layout)
    problems = settings.problems()
    if euid() != 0:
        problems.append("installing the local model service is root's: `sudo switchyard local-model install ...`")
    if approve != packet.digest:
        problems.append(f"--approve {approve or '(none)'} is not this packet ({packet.digest}); run "
                        "`switchyard local-model preview` and approve what it prints")
    if problems:
        for problem in problems:
            print_func(f"switchyard: {problem}")
        print_func("switchyard: nothing was downloaded or installed")
        return 1
    try:
        _ensure_root_directory(layout.engine_root, layout)
        _ensure_root_directory(layout.model_dir, layout)
        if layout.model_path.is_file() and not layout.model_path.is_symlink() and sha256_file(layout.model_path) == MODEL_SHA256:
            print_func(f"switchyard: {MODEL_FILE} is already here and matches its sha256")
        else:
            print_func(f"switchyard: downloading {MODEL_FILE} ({MODEL_SIZE / 1e9:.2f} GB) from {GGUF_REPO} @ {GGUF_REVISION}")
            fetcher(model_url(), layout.model_path, size=MODEL_SIZE, sha256=MODEL_SHA256, print_func=print_func)
        archive = layout.engine_root / ENGINE_ASSET
        installed = (read_record(layout) or {}).get("engine") or {}
        server = Path(str(installed.get("server") or ""))
        reuse = (installed.get("sha256") == ENGINE_SHA256 and server.is_file() and not server.is_symlink()
                 and layout.engine_dir in server.parents and sha256_file(server) == installed.get("server_sha256"))
        if reuse:
            print_func(f"switchyard: {ENGINE} {ENGINE_TAG} is already installed and unchanged")
        else:
            if not (archive.is_file() and sha256_file(archive) == ENGINE_SHA256):
                print_func(f"switchyard: downloading {ENGINE_ASSET} ({ENGINE_SIZE / 1e6:.0f} MB)")
                fetcher(engine_url(), archive, size=ENGINE_SIZE, sha256=ENGINE_SHA256, print_func=print_func)
            server = _extract_engine(archive, layout)
        missing = [flag for flag in SERVER_FLAGS if flag not in _server_help(server, runner)]
        if missing:
            raise Refusal(f"{server} does not accept {', '.join(missing)}; this release cannot be configured as pinned")
        changed = _write_root_file(layout.unit_path, render_unit(server, settings, layout), 0o644, layout)
        if changed:
            runner(["systemctl", "daemon-reload"], check=True, capture_output=True, text=True, timeout=60)
        body = {"schema": SCHEMA, "packet_digest": packet.digest, "model": packet.body["model"],
                "engine": {**packet.body["engine"], "server": str(server), "server_sha256": sha256_file(server)},
                "settings": asdict(settings), "unit": str(layout.unit_path)}
        if (read_record(layout) or {}).get("packet_digest") != packet.digest or changed:
            body["installed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _write_root_file(layout.record_path, json.dumps(body, indent=2, sort_keys=True) + "\n", 0o644, layout)
    except (Refusal, OSError, subprocess.CalledProcessError, urllib.error.URLError) as exc:
        print_func(f"switchyard: the local model service was not installed: {exc}")
        return 1
    print_func(f"switchyard: installed {MODEL_ID} ({QUANTIZATION}) with {ENGINE} {ENGINE_TAG} as {UNIT_NAME}; "
               "it is not running. Start it with `sudo switchyard local-model start`.")
    return 0


def _server_help(server: Path, runner: Callable[..., subprocess.CompletedProcess]) -> str:
    done = runner([str(server), "--help"], capture_output=True, text=True, timeout=60, check=False)
    return (done.stdout or "") + (done.stderr or "")


def remove(layout: Layout, *, print_func: Callable[[str], None] = print,
           runner: Callable[..., subprocess.CompletedProcess] = subprocess.run, euid: Callable[[], int] = os.geteuid) -> int:
    """Stop and delete the unit, the engine, the model and the record -- only those paths. Run again, nothing."""
    if euid() != 0:
        print_func("switchyard: removing the local model service is root's: `sudo switchyard local-model remove`")
        return 1
    removed = []
    try:
        for root in (layout.unit_dir, layout.engine_root, layout.state_root):
            if os.path.lexists(root):
                _ensure_root_directory(root, layout, create=False)
    except Refusal as exc:
        print_func(f"switchyard: nothing was removed: {exc}")
        return 1
    if layout.unit_path.exists() or layout.unit_path.is_symlink():
        runner(["systemctl", "stop", UNIT_NAME], capture_output=True, text=True, timeout=60, check=False)
        layout.unit_path.unlink()
        runner(["systemctl", "daemon-reload"], capture_output=True, text=True, timeout=60, check=False)
        runner(["systemctl", "reset-failed", UNIT_NAME], capture_output=True, text=True, timeout=60, check=False)
        removed.append(str(layout.unit_path))
    for path in (layout.engine_dir, layout.model_dir, layout.engine_root / ENGINE_ASSET, layout.record_path):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(path)
        else:
            os.unlink(path)
        removed.append(str(path))
    for empty in (layout.model_dir.parent, layout.model_dir.parent.parent, layout.engine_dir.parent,
                  layout.engine_root, layout.state_root):
        try:
            os.rmdir(empty)
        except OSError:
            pass
    print_func("switchyard: removed " + (", ".join(removed) if removed else "nothing; the service was not installed"))
    return 0


def control(action: str, *, print_func: Callable[[str], None] = print, layout: Layout = Layout(),
            runner: Callable[..., subprocess.CompletedProcess] = subprocess.run, euid: Callable[[], int] = os.geteuid) -> int:
    """start, stop or restart the one service; only an installed one, and only as root."""
    if euid() != 0:
        print_func(f"switchyard: {action} is root's: `sudo switchyard local-model {action}`")
        return 1
    if action in ("start", "restart") and read_record(layout) is None:
        print_func("switchyard: the local model service is not installed; see `switchyard local-model preview`")
        return 1
    done = runner(["systemctl", action, UNIT_NAME], capture_output=True, text=True, timeout=330, check=False)
    if done.returncode != 0:
        print_func(f"switchyard: systemctl {action} {UNIT_NAME} failed: {(done.stderr or done.stdout).strip()[:400]}")
        print_func("switchyard: see `switchyard local-model status` and `switchyard local-model logs`")
        return 1
    print_func(f"switchyard: {action} {UNIT_NAME}: done")
    return 0


# ------------------------------------------------------------------ observing it


def http_json(url: str, *, body: dict | None = None, timeout: float = 5.0) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read(1 << 20) or b"null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read(1 << 16) or b"null")
        except ValueError:
            return exc.code, None
    except (OSError, ValueError) as exc:
        return 0, str(exc)


def http_text(url: str, *, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read(1 << 20).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except OSError as exc:
        return 0, str(exc)


def status_lines(settings: Settings, layout: Layout, *,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 get: Callable[..., tuple[int, Any]] = http_json,
                 get_text: Callable[..., tuple[int, str]] = http_text) -> list[str]:
    """What the service is doing, said in bounded lines: installed or not, its unit's state and why it last
    stopped, whether the endpoint answers as the pinned model, and how busy it is."""
    record = read_record(layout)
    if record is None:
        return ["switchyard: local model: not installed (see `switchyard local-model preview`)"]
    lines = [f"switchyard: local model: {record['model']['id']} {record['model']['quantization']} "
             f"({record['model']['artifact_repository']} @ {record['model']['artifact_revision'][:12]}), "
             f"{record['engine']['name']} {record['engine']['tag']}, packet {record['packet_digest'][:12]}"]
    done = runner(["systemctl", "show", UNIT_NAME, "--property",
                   "ActiveState,SubState,Result,NRestarts,ExecMainStatus,MemoryCurrent,MemoryPeak"],
                  capture_output=True, text=True, timeout=10, check=False)
    unit = dict(line.split("=", 1) for line in (done.stdout or "").splitlines() if "=" in line)
    state, result = unit.get("ActiveState", "unknown"), unit.get("Result", "")
    why = {"oom-kill": f"it ran out of memory (MemoryMax {record['settings']['memory_max']}): lower --context or "
                       "--gpu-layers, or raise the limit",
           "start-limit-hit": "it failed twice within 15 minutes and was given up on rather than restarted again",
           "exit-code": f"llama-server exited with status {unit.get('ExecMainStatus', '?')}",
           "timeout": "it did not finish loading within 5 minutes"}.get(result, "")
    memory = unit.get("MemoryCurrent", "")
    lines.append(f"switchyard:   unit {UNIT_NAME}: {state}/{unit.get('SubState', '?')}"
                 + (f", {int(memory) / 2**30:.1f} GiB RAM" if memory.isdigit() else "")
                 + (f", restarted {unit['NRestarts']} times" if unit.get("NRestarts", "0") not in ("", "0") else "")
                 + (f"; last stop: {why}" if why else ""))
    if state != "active":
        lines.append("switchyard:   endpoint: not running; `sudo switchyard local-model start`, then "
                     "`switchyard local-model logs` if it does not come up")
        return lines
    base = f"http://{settings.host}:{settings.port}"
    code, health = get(f"{base}/health", timeout=3)
    if code == 503:
        lines.append("switchyard:   endpoint: loading the model")
        return lines
    if code != 200:
        lines.append(f"switchyard:   endpoint: does not answer at {base} ({health if code == 0 else f'HTTP {code}'})")
        return lines
    code, models = get(f"{base}/v1/models", timeout=3)
    ids = [m.get("id") for m in (models or {}).get("data", [])] if code == 200 and isinstance(models, dict) else []
    lines.append(f"switchyard:   endpoint: {base}/v1 answers as {', '.join(map(str, ids)) or 'nothing'}"
                 + ("" if ids == [MODEL_ID] else f" -- NOT the pinned {MODEL_ID}"))
    code, metrics = get_text(f"{base}/metrics", timeout=3)
    gauges = {}
    for line in metrics.splitlines() if code == 200 else []:
        name, _, value = line.partition(" ")
        if name in ("llamacpp:requests_processing", "llamacpp:requests_deferred"):
            gauges[name.split(":")[1]] = int(float(value))
    if gauges:
        lines.append(f"switchyard:   load: {gauges.get('requests_processing', 0)} of {settings.parallel} request(s) "
                     f"running, {gauges.get('requests_deferred', 0)} waiting")
    return lines


def probe(settings: Settings, *, get: Callable[..., tuple[int, Any]] = http_json, timeout: float = 900.0) -> list[str]:
    """Problems with the running endpoint as Hermes would use it, or []: the pinned model, and one real
    structured tool call -- a name it was offered, arguments that parse -- not tool markup written as text."""
    base = f"http://{settings.host}:{settings.port}"
    code, models = get(f"{base}/v1/models", timeout=5)
    if code != 200:
        return [f"{base}/v1/models did not answer ({models if code == 0 else f'HTTP {code}'})"]
    ids = [m.get("id") for m in models.get("data", [])] if isinstance(models, dict) else []
    if ids != [MODEL_ID]:
        return [f"{base} serves {ids}, not exactly {MODEL_ID}"]
    # Thinking stays on, as Hermes runs it; room for it before the call.
    request = {"model": MODEL_ID, "tools": [PROBE_TOOL], "tool_choice": "auto", "max_tokens": 16384,
               "messages": [{"role": "user", "content": "What is the weather in Paris right now? Use the tool."}]}
    code, answer = get(f"{base}/v1/chat/completions", body=request, timeout=timeout)
    if code != 200 or not isinstance(answer, dict):
        return [f"the tool probe got {answer if code == 0 else f'HTTP {code}'}"]
    choice = (answer.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    calls = message.get("tool_calls") or []
    content = str(message.get("content") or "")
    problems = []
    if any(marker in content for marker in PSEUDO_TOOL_MARKERS):
        problems.append("the reply carries tool markup as text: the server is not parsing tool calls")
    if not calls:
        problems.append(f"no structured tool call came back (finish_reason {choice.get('finish_reason')!r})")
    for call in calls:
        function = call.get("function") or {}
        if function.get("name") != "get_weather":
            problems.append(f"a tool call names {function.get('name')!r}, which it was not offered")
            continue
        try:
            arguments = json.loads(function.get("arguments") or "")
        except (TypeError, ValueError):
            problems.append(f"get_weather's arguments are not a JSON string: {function.get('arguments')!r}")
            continue
        if not isinstance(arguments, dict) or not isinstance(arguments.get("city"), str):
            problems.append(f"get_weather's arguments do not match its schema: {arguments!r}")
    return problems


def logs(*, lines: int = 60, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
         print_func: Callable[[str], None] = print) -> int:
    done = runner(["journalctl", "--unit", UNIT_NAME, "--lines", str(lines), "--no-pager", "--output", "short-iso"],
                  capture_output=True, text=True, timeout=30, check=False)
    for line in ((done.stdout or "") + (done.stderr or "")).splitlines()[-lines:]:
        print_func(line)
    return done.returncode


def settings_from_args(args: Any) -> Settings:
    base = Settings()
    return Settings(**{name: getattr(args, name) for name in ("port", "context", "gpu_layers", "threads")
                       if getattr(args, name, None) is not None},
                    **{k: v for k, v in asdict(base).items() if k not in ("port", "context", "gpu_layers", "threads")})


def switchyard_local_model_command(argv: Sequence[str], *, print_func: Callable[[str], None] = print) -> int:
    """`switchyard local-model <preview|install|start|stop|restart|status|probe|logs|remove>`."""
    import argparse

    parser = argparse.ArgumentParser(prog="switchyard local-model", description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["preview", "install", "start", "stop", "restart", "status", "probe",
                                           "logs", "remove"])
    parser.add_argument("--approve", default="", help="install: the digest `preview` printed for this packet")
    parser.add_argument("--port", type=int)
    parser.add_argument("--context", type=int)
    parser.add_argument("--gpu-layers", type=int)
    parser.add_argument("--threads", type=int)
    args = parser.parse_args(list(argv))
    layout = Layout()
    record = read_record(layout)
    # What is installed is what status, probe and the service controls speak to; the flags shape a new packet.
    settings = (Settings(**record["settings"]) if record and args.action not in ("preview", "install")
                else settings_from_args(args))
    problems = settings.problems()
    if problems:
        for problem in problems:
            print_func(f"switchyard: {problem}")
        return 1
    if args.action == "preview":
        return preview(settings, layout, print_func=print_func)
    if args.action == "install":
        return install(settings, layout, approve=args.approve, print_func=print_func)
    if args.action == "remove":
        return remove(layout, print_func=print_func)
    if args.action in ("start", "stop", "restart"):
        return control(args.action, print_func=print_func, layout=layout)
    if args.action == "logs":
        return logs(print_func=print_func)
    if args.action == "status":
        for line in status_lines(settings, layout):
            print_func(line)
        return 0
    problems = probe(settings)
    for problem in problems:
        print_func(f"switchyard: probe: {problem}")
    print_func(f"switchyard: probe: {'FAILED' if problems else f'{MODEL_ID} answered with one structured tool call'}")
    return 1 if problems else 0
