#!/usr/bin/env python3
"""SYRD-691: the opt-in, host-wide local model service -- its packet, its boundary and its diagnostics.

Nothing here downloads a model or touches the host's systemd. The pins are the shipped ones for the packet; for
install and remove they are swapped for small fixtures of the same shape (a GGUF stand-in, and an engine archive
whose llama-server prints a help text), served by a real HTTP server on 127.0.0.1 and written into a layout
anchored at a test-owned tree whose owner stands in for root. systemctl and nvidia-smi are recorded stand-ins;
the rendered unit is checked by the host's own `systemd-analyze verify`. The endpoint probe and status run
against a real loopback server answering as llama-server does, or as servers that fall short.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(detail)


def tree(path: Path) -> dict[str, tuple]:
    seen = {}
    for p in sorted(path.rglob("*")):
        info = p.lstat()
        seen[str(p.relative_to(path))] = (stat.S_IMODE(info.st_mode), stat.S_ISLNK(info.st_mode),
                                          hashlib.sha256(p.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else "")
    return seen


class Served:
    """A loopback HTTP server: GET files with Range, and JSON routes."""

    def __init__(self, files: dict[str, bytes] | None = None, routes: dict | None = None) -> None:
        self.files, self.routes, self.requests = files or {}, routes or {}, []
        served = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self._answer(None)

            def do_POST(self):  # noqa: N802
                self._answer(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null"))

            def _answer(self, body):
                served.requests.append((self.command, self.path, body, self.headers.get("Range")))
                if self.path in served.files:
                    data = served.files[self.path]
                    start = int(self.headers["Range"].split("=")[1].rstrip("-")) if self.headers.get("Range") else 0
                    self.send_response(206 if start else 200)
                    self.send_header("Content-Length", str(len(data) - start))
                    self.end_headers()
                    self.wfile.write(data[start:])
                    return
                code, payload = served.routes.get(self.path, (404, {"error": "no route"}))
                if callable(payload):
                    payload = payload(body)
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Host:
    """A layout anchored at a test-owned tree, its owner standing in for root, and recorded systemctl calls."""

    HELP = "\n".join(f"  {flag} VALUE" for flag in (
        "--model", "--alias", "--host", "--port", "--ctx-size", "--n-gpu-layers", "--threads", "--parallel",
        "--flash-attn", "--cache-type-k", "--cache-type-v", "--jinja", "--reasoning-format", "--metrics",
        "--no-webui", "--no-slots", "--cors-origins", "--no-cors-credentials", "--temp", "--top-p", "--top-k",
        "--min-p"))

    def __init__(self, tmp: Path, *, help_text: str | None = None) -> None:
        from scripts import local_model_service as m

        self.m, self.tmp = m, tmp
        os.chmod(tmp, 0o755)
        self.layout = m.Layout(engine_root=tmp / "opt/switchyard/local-models",
                               state_root=tmp / "var/lib/switchyard/local-models",
                               unit_dir=tmp / "etc/systemd/system", anchor=tmp, owner_uid=os.getuid())
        for d in ("etc/systemd/system", "var/lib/switchyard", "opt"):
            (tmp / d).mkdir(parents=True, exist_ok=True)
        for d in (tmp / "etc", tmp / "etc/systemd", tmp / "etc/systemd/system", tmp / "var", tmp / "var/lib",
                  tmp / "var/lib/switchyard", tmp / "opt"):
            os.chmod(d, 0o755)
        # Unrelated host state the service must never touch.
        (tmp / "var/lib/switchyard/rollout").mkdir()
        (tmp / "var/lib/switchyard/rollout/keep.json").write_text("{}")
        (tmp / "etc/systemd/system/other.service").write_text("[Service]\nExecStart=/bin/true\n")
        self.model = os.urandom(3 << 20)
        script = "#!/bin/sh\ncat <<'EOF'\n" + (self.HELP if help_text is None else help_text) + "\nEOF\n"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for name, data, mode in ((f"build/bin/llama-server", script.encode(), 0o755),
                                     ("build/bin/libllama.so.0", b"\x7fELF fake", 0o644)):
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), mode
                tar.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo("build/bin/libllama.so")
            link.type, link.linkname = tarfile.SYMTYPE, "libllama.so.0"
            tar.addfile(link)
        self.engine = buffer.getvalue()
        self.served = Served({"/model.gguf": self.model, "/engine.tar.gz": self.engine})
        self.calls: list[list[str]] = []

    def pins(self):
        m = self.m
        return contextlib.ExitStack().__enter__(), [
            patch.object(m, "MODEL_SIZE", len(self.model)),
            patch.object(m, "MODEL_SHA256", hashlib.sha256(self.model).hexdigest()),
            patch.object(m, "ENGINE_SIZE", len(self.engine)),
            patch.object(m, "ENGINE_SHA256", hashlib.sha256(self.engine).hexdigest()),
            patch.object(m, "model_url", lambda: self.served.url("/model.gguf")),
            patch.object(m, "engine_url", lambda: self.served.url("/engine.tar.gz")),
        ]

    def runner(self, argv, **_kwargs):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[-1] == "--help":
            done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
            return subprocess.CompletedProcess(argv, done.returncode, done.stdout, done.stderr)
        if argv[0] == "nvidia-smi":
            return subprocess.CompletedProcess(argv, 0, "NVIDIA GeForce RTX 4070, 12282, 1852\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def systemctl(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "systemctl"]


@contextlib.contextmanager
def host(**kwargs):
    with tempfile.TemporaryDirectory(prefix="syrd691.") as raw:
        h = Host(Path(raw), **kwargs)
        with contextlib.ExitStack() as stack:
            for p in h.pins()[1]:
                stack.enter_context(p)
            try:
                yield h
            finally:
                h.served.close()


def said(function, *args, **kwargs):
    lines: list[str] = []
    result = function(*args, print_func=lines.append, **kwargs)
    return result, lines


# ------------------------------------------------------------------ the packet


def test_every_statement_of_what_it_holds_agrees() -> None:
    """SYRD-690 (Audit): the packet said 3 GiB of RAM in one line and about 15 GiB in another."""
    from scripts import local_model_service as m

    packet = m.approval_packet(m.Settings(), m.Layout())
    lifecycle, ram = packet.body["service"]["lifecycle"], packet.body["sizing"]["ram"]
    guide = (ROOT / "docs" / "local-model-service.md").read_text(encoding="utf-8")
    opening = guide[:guide.index("## What it is")]
    for where, text in (("the packet's lifecycle", lifecycle), ("the packet's RAM", ram), ("HOLDS", m.HOLDS),
                        ("the guide's opening", " ".join(opening.split()))):
        check("up to about 15 gib" in text.lower() and "about 3 gib" in text.lower() and "page cache" in text,
              f"{where} gives the same RAM: up to about 15 GiB, about 3 GiB its own, the rest page cache: {text}")
    check("MemoryHigh 28G" in ram and "MemoryMax 32G" in ram and "9.15 GiB" in ram,
          f"the packet's RAM names its limits and the measured peak: {ram}")
    check(m.HOLDS in lifecycle, "the packet's lifecycle says what the help, start and install say")


def test_the_packet_names_every_pin_and_preview_changes_nothing() -> None:
    from scripts import local_model_service as m

    packet = m.approval_packet(m.Settings(), m.Layout())
    body = packet.body
    check(body["model"] == {"id": "Qwen/Qwen3.8-27B", "upstream_revision": "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0",
                            "license": "apache-2.0", "artifact_repository": "ggml-org/Qwen3.8-27B-GGUF",
                            "artifact_revision": "a316ea527fde32c12487be7574e2db5ac2ad2a7e",
                            "file": "Qwen3.8-27B-Q4_K_M.gguf", "quantization": "Q4_K_M", "bytes": 18973870528,
                            "sha256": "c600de0300ae8a0eb3a6c0b8b5561b8b96f16bd2c863c2a66c42de29d391a747",
                            "url": "https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF/resolve/"
                                   "a316ea527fde32c12487be7574e2db5ac2ad2a7e/Qwen3.8-27B-Q4_K_M.gguf"},
          f"the exact model, its immutable revisions and the artifact's size and sha256: {body['model']}")
    check(body["engine"]["tag"] == "b11803" and body["engine"]["commit"] == "a37cc46104eb50900d5ab4a9ba02a0abdac039db"
          and body["engine"]["sha256"] == "ef7f81e8747e485e1856294c89a25a558e8e34dd95d295a3ef3f503b05fe2bae"
          and body["engine"]["url"].endswith("/releases/download/b11803/llama-b11803-bin-ubuntu-cuda-13.4-x64.tar.gz"),
          f"the engine release, commit and asset digest: {body['engine']}")
    check(all(word not in json.dumps(body).lower() for word in ("latest", "main/", "/main")),
          "no floating alias anywhere in the packet")
    observed = m.approval_packet(m.Settings(), m.Layout(), observed={"gpu": "other"})
    check(observed.digest == packet.digest, "what is measured now is shown but is not part of what is approved")
    check(m.approval_packet(m.Settings(context=131072), m.Layout()).digest != packet.digest
          and m.approval_packet(m.Settings(port=17432), m.Layout()).digest != packet.digest,
          "a setting that decides what runs changes the digest")
    check(m.approval_packet(m.Settings(gpu_layers=18, threads=8), m.Layout()).digest == packet.digest
          and m.approval_packet(m.Settings(idle_minutes=60), m.Layout()).digest == packet.digest
          and body["service"]["tunable"] == {"gpu_layers": "16..40, default 24", "threads": "4..16, default 12",
                                             "idle_minutes": "5..120, default 15"},
          "tuning within the stated bounds is part of what is approved")
    check(m.Settings(gpu_layers=41).problems() and m.Settings(gpu_layers=15).problems()
          and m.Settings(threads=32).problems() and m.Settings(idle_minutes=4).problems()
          and m.Settings(idle_minutes=121).problems() and not m.Settings(gpu_layers=40, threads=4, idle_minutes=5).problems(),
          "and outside them is refused: the service never runs without an idle stop")
    supervisor = body["service"]["supervisor"]
    check(supervisor["sha256"] == hashlib.sha256((ROOT / "scripts/local_model_supervisor.py").read_bytes()).hexdigest()
          and supervisor["path"] == "/opt/switchyard/local-models/local-model-supervisor.py"
          and supervisor["command"][:4] == ["/usr/bin/python3", "-I", "-B", supervisor["path"]]
          and supervisor["command"][supervisor["command"].index("--idle-seconds") + 1] == "900",
          f"the program that stops it is approved by its digest, and runs isolated from anything writable: {supervisor}")
    with tempfile.TemporaryDirectory(prefix="syrd691.") as raw:
        other = Path(raw) / "local_model_supervisor.py"
        other.write_bytes((ROOT / "scripts/local_model_supervisor.py").read_bytes() + b"\n# changed\n")
        with patch.object(m, "SUPERVISOR_SOURCE", other):
            check(m.approval_packet(m.Settings(), m.Layout()).digest != packet.digest,
                  "another supervisor is another packet")
    with host() as h:
        before = tree(h.tmp)
        result, lines = said(m.preview, m.Settings(), h.layout, runner=h.runner)
        check(result == 0 and tree(h.tmp) == before and [c[0] for c in h.calls] == ["nvidia-smi"],
              f"preview writes nothing and runs only a read: {h.calls}")
        text = "\n".join(lines)
        for needle in ("nothing has been downloaded or installed", "Qwen3.8-27B-Q4_K_M.gguf", "sha256",
                       "llama.cpp b11803", "packages: none", "loopback only", "never at boot, never by a project launch",
                       "MemoryMax 32G", "at most 2 starts in 15 minutes", "rollback:", "now:      gpu: NVIDIA GeForce RTX 4070",
                       "holds about 8.3 GiB of GPU memory", "stops itself after 15 minutes with no request running",
                       "never stopped for being idle while a request runs", "`qualify` stops what it started",
                       f"supervisor {h.layout.supervisor_path}, sha256 ",
                       f"install --approve {m.approval_packet(m.Settings(), h.layout).digest}"):
            check(needle in text, f"the packet says {needle!r}: {text[:2000]}")


def test_the_server_binds_loopback_and_never_downloads() -> None:
    from scripts import local_model_service as m

    for host_value in ("0.0.0.0", "192.168.1.5", "::", "localhost.example"):
        check(any("not loopback" in p for p in m.Settings(host=host_value).problems()), f"{host_value} is refused")
    check(m.Settings().problems() == [] and m.Settings(host="::1").problems() == [], "loopback is accepted")
    check(m.Settings(parallel=2).problems(), "one request at a time on this hardware")
    check(m.Settings().context >= 64_000 and any("Hermes" in p for p in m.Settings(context=63_999).problems())
          and not m.Settings(context=64_000).problems(),
          "the context is at least Hermes's 64,000-token minimum (SYRD-692), and a smaller one is refused")
    argv = m.server_argv(Path("/x/llama-server"), m.Settings(), m.Layout())
    check(not {"-hf", "--hf-repo", "--model-url", "-mu", "--hf-file"} & set(argv)
          and argv[argv.index("--model") + 1] == str(m.Layout().model_path)
          and argv[argv.index("--alias") + 1] == "Qwen/Qwen3.8-27B" and argv[argv.index("--host") + 1] == "127.0.0.1",
          f"the pinned file by path, the exact model id, loopback: {argv}")
    check({"--jinja", "--reasoning-format"} <= set(argv) and argv[argv.index("--reasoning-format") + 1] == "deepseek",
          "reasoning and tool calls are parsed by the server")
    check(argv[argv.index("--cors-origins") + 1] == "https://cors.invalid" and {"--no-cors-credentials", "--no-slots"} <= set(argv),
          "no web page can drive it or read its answers, and live per-request state is not exposed")
    check(set(flag for flag in argv if flag.startswith("--")) == set(m.SERVER_FLAGS),
          "every flag rendered is one install checks against the installed --help")


def test_the_unit_is_bounded_unprivileged_and_never_starts_on_its_own() -> None:
    from scripts import local_model_service as m

    with host() as h:
        server = h.tmp / "llama-server"
        server.write_text("#!/bin/sh\n")
        server.chmod(0o755)
        unit = m.render_unit(server, m.Settings(), h.layout)
        lines = set(unit.splitlines())
        for wanted in ("DynamicUser=yes", "User=switchyard-local-model", "IPAddressDeny=any", "IPAddressAllow=localhost",
                       "MemoryMax=32G", "MemoryHigh=28G", "MemorySwapMax=0", "CPUWeight=20", "Nice=10",
                       "Restart=on-failure", "StartLimitBurst=2", "StartLimitIntervalSec=900", "OOMPolicy=stop",
                       "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes",
                       "DevicePolicy=closed", "DeviceAllow=/dev/nvidia0 rw", "DeviceAllow=/dev/nvidia-uvm rw",
                       f"ReadOnlyPaths={h.layout.engine_dir} {h.layout.model_dir} {h.layout.supervisor_path}",
                       "SuccessExitStatus=75", "KillMode=mixed", "RuntimeDirectory=switchyard-local-model",
                       "RuntimeDirectoryMode=0755"):
            check(wanted in lines, f"the unit carries {wanted}")
        check("[Install]" not in unit and "WantedBy" not in unit and "Alias=" not in unit and "Also=" not in unit,
              "no [Install]: it never starts at boot")
        exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        check(exec_start.startswith(f"ExecStart=/usr/bin/python3 -I -B {h.layout.supervisor_path} --url "
                                    "http://127.0.0.1:17431 --idle-seconds 900 --poll-seconds 30 --state "
                                    f"{h.layout.state_path} -- {server} --model "),
              f"its main process is the supervisor, with the server's command after `--`: {exec_start}")
        ipv6 = m.render_unit(server, m.Settings(host="::1", idle_minutes=30), h.layout)
        check("--url 'http://[::1]:17431' --idle-seconds 1800 " in ipv6, "an IPv6 loopback is addressed as one")
        path = h.tmp / "switchyard-local-model.service"
        path.write_text(unit)
        done = subprocess.run(["systemd-analyze", "verify", "--man=no", str(path)], capture_output=True, text=True,
                              timeout=60)
        problems = [line for line in (done.stdout + done.stderr).splitlines()
                    if "switchyard-local-model.service" in line and "not executable" not in line
                    and "Failed to" not in line]
        check(done.returncode == 0 or not problems, f"systemd's own verifier accepts it: {done.stdout} {done.stderr}")


# ------------------------------------------------------------------ install and remove


def test_install_needs_root_and_exactly_the_approved_packet() -> None:
    from scripts import local_model_service as m

    with host() as h:
        before = tree(h.tmp)
        digest = m.approval_packet(m.Settings(), h.layout).digest
        for label, kwargs in (("no approval", dict(approve="")), ("another packet", dict(approve="0" * 64)),
                              ("not root", dict(approve=digest, euid=lambda: 1000))):
            result, lines = said(m.install, m.Settings(), h.layout, runner=h.runner, **{"euid": lambda: 0, **kwargs})
            check(result == 1 and lines[-1] == "switchyard: nothing was downloaded or installed"
                  and tree(h.tmp) == before and h.served.requests == [] and h.calls == [],
                  f"{label}: refused, nothing fetched, written or run: {lines}")
        stale = m.approval_packet(m.Settings(context=16384), h.layout).digest
        result, lines = said(m.install, m.Settings(), h.layout, approve=stale, runner=h.runner, euid=lambda: 0)
        check(result == 1 and "is not this packet" in lines[0], f"approving other settings does not install these: {lines}")


def test_install_places_verified_artifacts_records_provenance_and_starts_nothing() -> None:
    from scripts import local_model_service as m

    with host() as h:
        digest = m.approval_packet(m.Settings(), h.layout).digest
        unrelated = {k: v for k, v in tree(h.tmp).items()}
        result, lines = said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
        check(result == 0 and "it is not running" in lines[-1], f"installed: {lines}")
        check(h.layout.model_path.read_bytes() == h.model and stat.S_IMODE(h.layout.model_path.stat().st_mode) == 0o644,
              "the model is the verified bytes, readable by the service identity")
        server = h.layout.engine_dir / "build/bin/llama-server"
        record = json.loads(h.layout.record_path.read_text())
        check(record["packet_digest"] == digest and record["model"]["sha256"] == hashlib.sha256(h.model).hexdigest()
              and record["engine"]["server"] == str(server)
              and record["engine"]["server_sha256"] == hashlib.sha256(server.read_bytes()).hexdigest()
              and record["settings"]["host"] == "127.0.0.1" and record.get("installed_at"),
              f"the record carries the packet, the artifact and the exact program that runs: {record}")
        from scripts import hermes_local_profile as hermes
        profile = hermes.profile_from_record(record)
        check(record["schema"] == hermes.SERVICE_SCHEMA and profile.model == m.MODEL_ID
              and profile.base_url == "http://127.0.0.1:17431/v1" and profile.context == 65_536
              and hermes.context_problem(profile) == "",
              f"the Hermes profile (SYRD-692) reads this record as a model Hermes runs: {profile}")
        unit = h.layout.unit_path.read_text()
        check(f" -- {server} --model {h.layout.model_path}" in unit, "the unit runs the recorded program")
        installed = h.layout.supervisor_path
        info = installed.lstat()
        check(installed.read_bytes() == (ROOT / "scripts/local_model_supervisor.py").read_bytes()
              and stat.S_IMODE(info.st_mode) == 0o644 and info.st_uid == h.layout.owner_uid
              and record["supervisor"] == {"path": str(installed),
                                           "sha256": hashlib.sha256(installed.read_bytes()).hexdigest()}
              and record["supervisor"]["sha256"] == m.approval_packet(m.Settings(), h.layout).body["service"]["supervisor"]["sha256"],
              f"the supervisor is the approved bytes, root's alone, recorded by digest: {record.get('supervisor')}")
        check("holds about 8.3 GiB of GPU memory" in lines[-1] and "stops itself after 15 idle minutes" in lines[-1],
              f"and install says what running it will hold: {lines[-1]}")
        check(h.systemctl() == [["systemctl", "daemon-reload"]], f"systemd reloaded once, nothing started: {h.systemctl()}")
        check(all(path in tree(h.tmp) and tree(h.tmp)[path] == value for path, value in unrelated.items()
                  if path.startswith(("var/lib/switchyard/rollout", "etc/systemd/system/other"))),
              "unrelated host state is untouched")
        check((h.layout.engine_dir / "build/bin/libllama.so").is_symlink(), "the archive's own library link is kept")
        # Again: nothing downloaded, nothing reloaded, nothing rewritten.
        h.calls.clear()
        fetched = len(h.served.requests)
        again = tree(h.tmp)
        result, lines = said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
        check(result == 0 and len(h.served.requests) == fetched and h.systemctl() == [] and tree(h.tmp) == again,
              f"a second install changes nothing: {lines} {h.systemctl()}")


def test_install_refuses_links_corrupt_downloads_and_an_engine_that_cannot_be_configured() -> None:
    from scripts import local_model_service as m

    with host() as h:
        elsewhere = h.tmp / "elsewhere"
        elsewhere.mkdir(mode=0o755)
        (h.tmp / "var/lib/switchyard").rename(h.tmp / "var/lib/switchyard-real")
        (h.tmp / "var/lib/switchyard").symlink_to(elsewhere)
        digest = m.approval_packet(m.Settings(), h.layout).digest
        result, lines = said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
        check(result == 1 and "symlink" in lines[-1] and list(elsewhere.iterdir()) == []
              and not h.layout.unit_path.exists(), f"a link in the path is refused and its target untouched: {lines}")
    with host() as h:
        h.served.files["/model.gguf"] = os.urandom(len(h.model))  # the right size, the wrong bytes
        digest = m.approval_packet(m.Settings(), h.layout).digest
        result, lines = said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
        check(result == 1 and "did not match its pin" in lines[-1] and not h.layout.model_path.exists()
              and not any(h.layout.model_dir.glob("*.partial")) and not h.layout.unit_path.exists(),
              f"a download that does not match is discarded and nothing is installed: {lines}")
    with host(help_text="  --model VALUE\n  --port VALUE") as h:
        digest = m.approval_packet(m.Settings(), h.layout).digest
        result, lines = said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
        check(result == 1 and "does not accept" in lines[-1] and "--reasoning-format" in lines[-1]
              and not h.layout.unit_path.exists() and not h.layout.record_path.exists(),
              f"an engine that lacks a flag the server needs is refused before any unit exists: {lines}")


def test_a_partial_download_resumes() -> None:
    from scripts import local_model_service as m

    with host() as h:
        dest = h.tmp / "model.gguf"
        (h.tmp / "model.gguf.partial").write_bytes(h.model[:1000])
        m.fetch(h.served.url("/model.gguf"), dest, size=len(h.model), sha256=hashlib.sha256(h.model).hexdigest(),
                print_func=lambda _l: None)
        check(dest.read_bytes() == h.model and h.served.requests[-1][3] == "bytes=1000-",
              f"resumed from the partial, verified whole: {h.served.requests[-1]}")


def test_remove_deletes_only_its_own_paths_and_is_idempotent() -> None:
    from scripts import local_model_service as m

    with host() as h:
        digest = m.approval_packet(m.Settings(), h.layout).digest
        said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
        result, lines = said(m.remove, h.layout, runner=h.runner, euid=lambda: 1000)
        check(result == 1 and h.layout.unit_path.exists(), f"not root: refused: {lines}")
        h.calls.clear()
        result, lines = said(m.remove, h.layout, runner=h.runner, euid=lambda: 0)
        left = tree(h.tmp)
        check(result == 0 and not h.layout.unit_path.exists() and not h.layout.engine_root.exists()
              and not h.layout.supervisor_path.exists() and str(h.layout.supervisor_path) in lines[-1]
              and not h.layout.state_root.exists(),
              f"its unit, engine, supervisor, model and record are gone: {lines} {sorted(left)}")
        check("var/lib/switchyard/rollout/keep.json" in left and "etc/systemd/system/other.service" in left,
              f"and nothing else: {sorted(left)}")
        check([c[1] for c in h.systemctl()] == ["stop", "daemon-reload", "reset-failed"], f"stopped first: {h.systemctl()}")
        result, lines = said(m.remove, h.layout, runner=h.runner, euid=lambda: 0)
        check(result == 0 and "removed nothing" in lines[-1], f"run again, nothing: {lines}")
    with host() as h:
        digest = m.approval_packet(m.Settings(), h.layout).digest
        said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
        os.chmod(h.tmp / "var/lib/switchyard", 0o777)
        result, lines = said(m.remove, h.layout, runner=h.runner, euid=lambda: 0)
        check(result == 1 and "nothing was removed" in lines[-1] and h.layout.model_path.exists(),
              f"a directory anybody could have swapped is refused before anything is deleted: {lines}")
        os.chmod(h.tmp / "var/lib/switchyard", 0o755)


def test_remove_deletes_nothing_when_the_service_does_not_stop() -> None:
    from scripts import local_model_service as m

    for name, failure in (("a failed stop", lambda argv: subprocess.CompletedProcess(argv, 1, "", "stop failed\n")),
                          ("a stop that hangs", lambda argv: (_ for _ in ()).throw(
                              subprocess.TimeoutExpired(argv, 60)))):
        with host() as h:
            digest = m.approval_packet(m.Settings(), h.layout).digest
            said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
            before = tree(h.tmp)
            h.calls.clear()

            def runner(argv, **kwargs):
                if [str(a) for a in argv[:2]] == ["systemctl", "stop"]:
                    h.calls.append([str(a) for a in argv])
                    return failure(argv)
                return h.runner(argv, **kwargs)

            result, lines = said(m.remove, h.layout, runner=runner, euid=lambda: 0)
            check(result == 1, f"{name}: remove fails: {lines}")
            check(tree(h.tmp) == before and h.layout.unit_path.exists() and h.layout.model_path.exists()
                  and h.layout.supervisor_path.exists() and h.layout.record_path.exists(),
                  f"{name}: every managed path is kept, so the service can still be stopped and removed: {lines}")
            check([c[1] for c in h.systemctl()] == ["stop"], f"{name}: nothing after the stop: {h.systemctl()}")
            told = "\n".join(lines)
            check("nothing was removed" in told and ("stop failed" in told or "60 s" in told)
                  and "sudo switchyard local-model stop" in told and "local-model remove` again" in told,
                  f"{name}: it says why and how to recover: {lines}")
            result, lines = said(m.remove, h.layout, runner=h.runner, euid=lambda: 0)
            check(result == 0 and not h.layout.unit_path.exists() and not h.layout.state_root.exists(),
                  f"{name}: once it stops, remove completes: {lines}")


# ------------------------------------------------------------------ status, probe and the controls


def llama(*, model: str = "Qwen/Qwen3.8-27B", completion=None, health=(200, {"status": "ok"}), running=0,
          waiting=0) -> dict:
    def answer(_body):
        return completion
    metrics = (f"# HELP llamacpp:requests_processing Number of requests processing\n"
               f"llamacpp:requests_processing {running}\nllamacpp:requests_deferred {waiting}\n").encode()
    return {"/health": health, "/v1/models": (200, {"data": [{"id": model}]}), "/metrics": (200, metrics),
            "/v1/chat/completions": (200, answer)}


def tool_reply(**message) -> dict:
    return {"choices": [{"finish_reason": message.pop("finish_reason", "tool_calls"),
                         "message": {"role": "assistant", "content": "", "reasoning_content": "The user wants...",
                                     **message}}]}


def test_the_probe_wants_one_real_structured_tool_call_from_the_pinned_model() -> None:
    from scripts import local_model_service as m

    good = tool_reply(tool_calls=[{"id": "c1", "type": "function",
                                   "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}}])
    cases = {
        "a parsed tool call": (llama(completion=good), []),
        "markup as text": (llama(completion=tool_reply(content="<tool_call>\n{\"name\": \"get_weather\"}\n</tool_call>",
                                                       finish_reason="stop")), ["tool markup as text", "no structured tool call"]),
        "a tool it was not offered": (llama(completion=tool_reply(tool_calls=[{"function": {"name": "run_shell",
                                                                                            "arguments": "{}"}}])),
                                      ["'run_shell', which it was not offered"]),
        "arguments as an object": (llama(completion=tool_reply(tool_calls=[{"function": {"name": "get_weather",
                                                                                         "arguments": {"city": "Paris"}}}])),
                                   ["are not a JSON string"]),
        "arguments off the schema": (llama(completion=tool_reply(tool_calls=[{"function": {"name": "get_weather",
                                                                                           "arguments": "{\"town\": 1}"}}])),
                                     ["do not match its schema"]),
        "another model": (llama(model="qwen3.8:latest", completion=good), ["not exactly Qwen/Qwen3.8-27B"]),
    }
    for label, (routes, wanted) in cases.items():
        served = Served(routes=routes)
        try:
            problems = m.probe(m.Settings(port=served.port))
        finally:
            served.close()
        check((problems == []) if not wanted else all(any(w in p for p in problems) for w in wanted),
              f"{label}: {problems}")
        if label == "a parsed tool call":
            sent = [r for r in served.requests if r[1] == "/v1/chat/completions"][0][2]
            check(sent["model"] == "Qwen/Qwen3.8-27B" and sent["tools"][0]["function"]["name"] == "get_weather",
                  f"the probe asks the pinned model with one offered tool: {sent}")
    problems = m.probe(m.Settings(port=9), timeout=2)
    check(len(problems) == 1 and "did not answer" in problems[0], f"no server: one bounded line: {problems}")


def test_status_says_why_in_bounded_lines() -> None:
    from scripts import local_model_service as m

    with host() as h:
        check(m.status_lines(m.Settings(), h.layout, runner=h.runner) ==
              ["switchyard: local model: not installed (see `switchyard local-model preview`)"], "not installed")
        digest = m.approval_packet(m.Settings(), h.layout).digest
        said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
        for unit, wanted in (
            ("ActiveState=failed\nSubState=failed\nResult=oom-kill\nNRestarts=1\n", "it ran out of memory (MemoryMax 32G)"),
            ("ActiveState=failed\nSubState=failed\nResult=start-limit-hit\nNRestarts=2\n",
             "failed twice within 15 minutes and was given up on"),
            ("ActiveState=inactive\nSubState=dead\nResult=success\n", "endpoint: not running"),
        ):
            show = lambda argv, **_k: subprocess.CompletedProcess(argv, 0, unit, "")  # noqa: E731
            lines = m.status_lines(m.Settings(), h.layout, runner=show)
            check(any(wanted in line for line in lines) and len(lines) <= 4, f"{wanted}: {lines}")
        active = lambda argv, **_k: subprocess.CompletedProcess(  # noqa: E731
            argv, 0, "ActiveState=active\nSubState=running\nResult=success\nMemoryCurrent=15032385536\n", "")
        for routes, wanted in ((llama(health=(503, {"error": "Loading model"})), "loading the model"),
                               (llama(running=1, waiting=2), "load: 1 of 1 request(s) running, 2 waiting"),
                               (llama(model="something-else"), "NOT the pinned Qwen/Qwen3.8-27B")):
            served = Served(routes=routes)
            try:
                lines = m.status_lines(m.Settings(port=served.port), h.layout, runner=active)
            finally:
                served.close()
            check(any(wanted in line for line in lines) and any("14.0 GiB RAM" in line for line in lines),
                  f"{wanted}: {lines}")
        lines = m.status_lines(m.Settings(port=9), h.layout, runner=active)
        check(any("does not answer" in line for line in lines), f"an active unit that does not answer: {lines}")


def test_the_controls_are_root_s_and_only_for_an_installed_service() -> None:
    from scripts import local_model_service as m

    with host() as h:
        result, lines = said(m.control, "start", layout=h.layout, runner=h.runner, euid=lambda: 1000)
        check(result == 1 and "`sudo switchyard local-model start`" in lines[0] and h.calls == [], f"not root: {lines}")
        result, lines = said(m.control, "start", layout=h.layout, runner=h.runner, euid=lambda: 0)
        check(result == 1 and "not installed" in lines[0] and h.calls == [], f"nothing to start: {lines}")
        digest = m.approval_packet(m.Settings(), h.layout).digest
        said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
        h.calls.clear()
        failing = lambda argv, **_k: subprocess.CompletedProcess(argv, 1, "", "Job failed. See journalctl." * 50)  # noqa: E731
        result, lines = said(m.control, "start", layout=h.layout, runner=failing, euid=lambda: 0)
        check(result == 1 and len(lines[0]) < 600 and "local-model logs" in lines[1], f"a failed start, bounded: {lines}")
        result, lines = said(m.control, "stop", layout=h.layout, runner=h.runner, euid=lambda: 0)
        check(result == 0 and h.systemctl() == [["systemctl", "stop", "switchyard-local-model.service"]]
              and "holds no GPU memory now" in lines[-1], f"stop: {h.calls} {lines}")
        h.calls.clear()
        result, lines = said(m.control, "start", layout=h.layout, runner=h.runner, euid=lambda: 0)
        check(result == 0 and "holds about 8.3 GiB of GPU memory" in lines[-1]
              and "stops itself after 15 idle minutes" in lines[-1] and "local-model stop` releases it now" in lines[-1],
              f"start says what it now holds and when it lets go: {lines}")
        record = json.loads(h.layout.record_path.read_text())
        del record["supervisor"]
        h.layout.record_path.write_text(json.dumps(record))
        h.calls.clear()
        for action in ("start", "restart"):
            result, lines = said(m.control, action, layout=h.layout, runner=h.runner, euid=lambda: 0)
            check(result == 1 and "predates its idle stop" in lines[0] and h.calls == [],
                  f"{action}: a service installed without its idle stop is not started again: {lines}")
        result, lines = said(m.control, "stop", layout=h.layout, runner=h.runner, euid=lambda: 0)
        check(result == 0, "but it can always be stopped")


def test_status_says_what_holds_the_gpu_and_when_it_lets_go() -> None:
    import dataclasses
    import time

    from scripts import local_model_service as m

    def answers(unit: str, apps: str, *, nvidia: int = 0):
        def run(argv, **_kwargs):
            if argv[0] == "nvidia-smi":
                return subprocess.CompletedProcess(argv, nvidia, apps, "")
            return subprocess.CompletedProcess(argv, 0, unit, "")
        return run

    with host() as h:
        digest = m.approval_packet(m.Settings(), h.layout).digest
        said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
        layout = dataclasses.replace(h.layout, cgroup_root=h.tmp / "sys/fs/cgroup", run_dir=h.tmp / "run/switchyard-local-model")
        cgroup = layout.cgroup_root / "system.slice/switchyard-local-model.service"
        cgroup.mkdir(parents=True)
        (cgroup / "cgroup.procs").write_text("4100\n4101\n")
        active = ("ActiveState=active\nSubState=running\nResult=success\n"
                  "ControlGroup=/system.slice/switchyard-local-model.service\n")
        holding = "4034, /usr/bin/kwin_wayland, 10\n4101, /opt/switchyard/local-models/llama.cpp/b11803/llama-server, 8546\n"
        served = Served(routes=llama())
        try:
            settings = m.Settings(port=served.port)
            lines = m.status_lines(settings, layout, runner=answers(active, holding))
            check(any("GPU: this service holds 8,546 MiB of GPU memory (pid 4101) until `sudo switchyard local-model "
                      "stop`, or until it has been idle 15 minutes" in line for line in lines)
                  and not any("outside this service" in line or "idle stop:" in line for line in lines),
                  f"running: whose GPU memory it is, how much, and what releases it: {lines}")
            layout.run_dir.mkdir(parents=True)
            stops = time.time() + 660
            layout.state_path.write_text(json.dumps({"schema": "switchyard.local-model.supervisor.v1", "busy": 0,
                                                     "idle_for": 240, "idle_seconds": 900, "stops_at": stops}))
            lines = m.status_lines(settings, layout, runner=answers(active, holding))
            check(lines[-1] == f"switchyard:   idle stop: idle 4 of 15 minutes; it stops itself at "
                               f"{time.strftime('%H:%M', time.localtime(stops))} unless a request comes",
                  f"and, from the supervisor, when it will let go: {lines}")
            layout.state_path.write_text(json.dumps({"schema": "switchyard.local-model.supervisor.v1", "busy": 1,
                                                     "idle_for": 0, "idle_seconds": 900, "stops_at": None}))
            lines = m.status_lines(settings, layout, runner=answers(active, holding))
            check("a request is running or waiting; it is never stopped for being idle" in lines[-1], f"busy: {lines}")
            (h.tmp / "elsewhere.json").write_text(layout.state_path.read_text())
            layout.state_path.unlink()
            layout.state_path.symlink_to(h.tmp / "elsewhere.json")
            lines = m.status_lines(settings, layout, runner=answers(active, holding))
            check(not any("idle stop:" in line for line in lines), f"a state that is not a plain file is not read: {lines}")
        finally:
            served.close()
        idled = "ActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=75\nControlGroup=\n"
        lines = m.status_lines(m.Settings(), layout, runner=answers(idled, "4034, /usr/bin/kwin_wayland, 10\n"))
        check(any("last stop: it stopped itself after 15 idle minutes, releasing its GPU and host memory" in line
                  for line in lines) and "switchyard:   GPU: this service holds no GPU memory" in lines and len(lines) <= 4,
              f"stopped for being idle: said so, and it holds nothing: {lines}")
        lines = m.status_lines(m.Settings(), layout, runner=answers(idled, "5555, /home/eric/llama.cpp/llama-server, 9000\n"))
        check(any("llama-server outside this service holds 9,000 MiB (pid 5555); its idle stop does not reach it" in line
                  for line in lines), f"a llama-server started by hand is named, not taken for this service: {lines}")
        lines = m.status_lines(m.Settings(), layout, runner=answers(idled, "", nvidia=9))
        check(any("nvidia-smi did not answer, so what holds GPU memory is unknown" in line for line in lines),
              f"an unanswered GPU query is said, not guessed: {lines}")
        record = json.loads(h.layout.record_path.read_text())
        del record["supervisor"]
        h.layout.record_path.write_text(json.dumps(record))
        lines = m.status_lines(m.Settings(), layout, runner=answers(idled, ""))
        check(any("predates its idle stop and would hold the GPU until stopped" in line for line in lines),
              f"an install without its idle stop is named: {lines}")


def test_status_finds_an_idle_stop_systemd_has_forgotten() -> None:
    """Measured on the host (2026-11-01): after the supervisor's idle exit, systemd unloads the inactive unit and
    `systemctl show` reads Result=success, ExecMainStatus=0 and no invocation, and /run/switchyard-local-model is gone.
    The supervisor's own journal lines are the record left."""
    import re

    from scripts import local_model_service as m

    serving = "switchyard-local-model: serving; stops itself after {} idle minutes (no request running or waiting, no tokens processed)"
    idled = ("switchyard-local-model: idle for {} minutes with no request running or waiting; stopping llama-server to "
             "release its GPU and host memory")
    noise = ["srv  update_slots: all slots are idle", "llama_context: CUDA0 compute buffer size = 1032.00 MiB"]
    forgotten = "ActiveState=inactive\nSubState=dead\nResult=success\nNRestarts=0\nExecMainStatus=0\nControlGroup=\n"

    def journal(entries: list[str], *, code: int = 0):
        """journalctl as it filters: --grep, then the newest --lines, printed oldest first unless --reverse."""
        def run(argv, **_kwargs):
            if argv[0] == "nvidia-smi":
                return subprocess.CompletedProcess(argv, 0, "4034, /usr/bin/kwin_wayland, 10\n", "")
            if argv[0] != "journalctl":
                return subprocess.CompletedProcess(argv, 0, forgotten, "")
            check(argv[argv.index("--unit") + 1] == m.UNIT_NAME, f"the journal asked is this unit's: {argv}")
            picked = list(entries)
            if "--grep" in argv:
                picked = [line for line in picked if re.search(argv[argv.index("--grep") + 1], line)]
            if "--lines" in argv:
                picked = picked[-int(argv[argv.index("--lines") + 1]):]
            if "--reverse" in argv:
                picked.reverse()
            return subprocess.CompletedProcess(argv, code, "".join(f"{line}\n" for line in picked), "")
        return run

    def last_stop(runner) -> list[str]:
        return [line for line in m.status_lines(m.Settings(), h.layout, runner=runner) if "last stop:" in line]

    with host() as h:
        digest = m.approval_packet(m.Settings(), h.layout).digest
        said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
        earlier = [serving.format(15), *noise, idled.format(15), *noise]
        stopped_itself = [*earlier, serving.format(5), *noise, idled.format(5), "srv    operator(): cleaning up", *noise]
        lines = last_stop(journal(stopped_itself))
        check(len(lines) == 1 and lines[0].endswith("; last stop: it stopped itself after 5 idle minutes, releasing its "
                                                    "GPU and host memory"),
              f"the newest idle stop, with the window it ran under, though systemd has forgotten it: {lines}")
        stopped_by_hand = [*stopped_itself, serving.format(15), *noise, "srv    operator(): cleaning up"]
        check(last_stop(journal(stopped_by_hand)) == [],
              "a later run stopped by `systemctl stop` is not taken for an idle stop")
        crashed = [*stopped_itself, serving.format(15), *noise,
                   "switchyard-local-model: llama-server exited with status 3"]
        check(last_stop(journal(crashed)) == [], "nor is a later run whose server exited on its own")
        check(last_stop(journal(stopped_itself, code=1)) == [], "a journal that cannot be read claims nothing")
        check(last_stop(journal([])) == [], "nor does an empty one")


class Qualified:
    """systemctl and the endpoint as `qualify` sees them: a unit that can start, load, fail or refuse to stop."""

    def __init__(self, m, h, *, active=False, start=0, stop=0, loads_after=2, fails_loading=False, completion=None) -> None:
        self.m, self.h, self.state, self.start, self.stop = m, h, "active" if active else "inactive", start, stop
        self.loads_after, self.fails_loading, self.healths, self.now = loads_after, fails_loading, 0, [0.0]
        good = tool_reply(tool_calls=[{"id": "c1", "type": "function",
                                       "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}}])
        self.served = Served(routes=llama(completion=completion or good))
        self.settings = m.Settings(port=self.served.port)

    def runner(self, argv, **_kwargs):
        argv = [str(a) for a in argv]
        self.h.calls.append(argv)
        if argv[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(argv, 0, self.state + "\n", "")
        if argv[:2] == ["systemctl", "start"]:
            self.state = "activating" if self.start == 0 else "failed"
            return subprocess.CompletedProcess(argv, self.start, "", "" if self.start == 0 else "Job failed.")
        if argv[:2] == ["systemctl", "stop"]:
            if self.stop == 0:
                self.state = "inactive"
            return subprocess.CompletedProcess(argv, self.stop, "", "" if self.stop == 0 else "Access denied")
        if argv[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(argv, 0, f"ActiveState={self.state}\nSubState=running\n", "")
        return self.h.runner(argv)

    def get(self, url, **kwargs):
        if url.endswith("/health"):
            self.healths += 1
            if self.healths >= 1000:
                raise AssertionError("qualify waited for the load past its bound")
            if self.fails_loading:
                self.state = "failed"
            loaded = self.state in ("active", "activating") and self.healths >= self.loads_after
            if loaded:
                self.state = "active"
            self.served.routes["/health"] = (200, {"status": "ok"}) if loaded else (503, {"error": "Loading model"})
        return self.m.http_json(url, **kwargs)

    def sleep(self, seconds):
        self.now[0] += seconds

    def run(self, **kwargs):
        try:
            return said(self.m.qualify, self.settings, self.h.layout, runner=self.runner, get=self.get,
                        euid=kwargs.pop("euid", lambda: 0), sleep=self.sleep, clock=lambda: self.now[0], **kwargs)
        finally:
            self.served.close()


def test_qualify_stops_what_it_started_whatever_happens() -> None:
    from scripts import local_model_service as m

    with host() as h:
        digest = m.approval_packet(m.Settings(), h.layout).digest
        said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
        sequence = lambda: [c[1] for c in h.systemctl() if c[1] in ("start", "stop")]  # noqa: E731
        cases = {
            "it loads and answers": (dict(), 0, ["answered with one structured tool call"]),
            "it answers with markup": (dict(completion=tool_reply(content="<tool_call>x</tool_call>", finish_reason="stop")),
                                       1, ["probe: the reply carries tool markup", "qualify: FAILED"]),
            "it never finishes loading": (dict(loads_after=10**6), 1, ["did not finish loading within 330 s"]),
            "it fails while loading": (dict(fails_loading=True), 1, ["stopped while loading"]),
            "it does not start": (dict(start=1), 1, ["systemctl start switchyard-local-model.service failed"]),
        }
        for label, (kwargs, wanted, said_lines) in cases.items():
            h.calls.clear()
            unit = Qualified(m, h, **kwargs)
            result, lines = unit.run()
            check(result == wanted and all(any(w in line for line in lines) for w in said_lines),
                  f"{label}: {result} {lines}")
            check(sequence() == ["start", "stop"] and unit.state == "inactive"
                  and lines[-1] == "switchyard: qualify: stopped switchyard-local-model.service, which it started; it "
                                   "holds no GPU memory now",
                  f"{label}: what it started, it stopped, last: {h.systemctl()} {lines[-1:]}")
        h.calls.clear()
        unit = Qualified(m, h, stop=1)
        result, lines = unit.run()
        check(result == 1 and "could not stop" in lines[-1] and "`sudo switchyard local-model stop`" in lines[-1],
              f"a stop that fails fails the qualification, and says what to run: {lines[-1:]}")
        h.calls.clear()
        unit = Qualified(m, h, active=True)
        result, lines = unit.run()
        check(result == 0 and sequence() == [] and unit.state == "active"
              and "was already running, so it is left running; it stops itself after 15 idle minutes" in lines[-1],
              f"a service already running is someone's: probed and left, its idle stop still bounding it: {lines[-1:]}")
        for label, euid, edit in (("not root", lambda: 1000, None), ("no idle stop", lambda: 0, "supervisor")):
            if edit:
                record = json.loads(h.layout.record_path.read_text())
                del record[edit]
                h.layout.record_path.write_text(json.dumps(record))
            h.calls.clear()
            result, lines = Qualified(m, h).run(euid=euid)
            check(result == 1 and h.calls == [], f"{label}: refused before anything runs: {lines}")


def test_nothing_but_its_own_commands_starts_it() -> None:
    from scripts import local_model_service as m

    named = []
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if (not path.is_file() or path.is_symlink() or relative.parts[0] in (".git", "tests", "docs")
                or "__pycache__" in relative.parts or path.suffix == ".md"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(name in text for name in (m.UNIT_NAME, "switchyard-local-model", "local_model_supervisor", "local-model-supervisor")):
            named.append(str(relative))
    check(named == ["scripts/local_model_service.py", "scripts/local_model_supervisor.py"],
          f"no launcher, unit, timer, hook or project path names the unit or its supervisor: {named}")
    with host() as h:
        digest = m.approval_packet(m.Settings(), h.layout).digest
        said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
        said(m.install, m.Settings(), h.layout, approve=digest, runner=h.runner, euid=lambda: 0)
        check(not [c for c in h.systemctl() if c[1] in ("start", "enable", "restart", "reload-or-restart")],
              f"installing, twice, starts and enables nothing: {h.systemctl()}")


def test_switchyard_dispatches_it_unprivileged_and_no_launch_reaches_it() -> None:
    from scripts import team_launcher as launcher

    check("local-model" in launcher.SWITCHYARD_COMMANDS and "local-model" in launcher.SWITCHYARD_UNPRIVILEGED_COMMANDS
          and not launcher.switchyard_invocation_requires_root(["local-model", "install"]),
          "the wrapper never escalates it; its root actions are typed with sudo")
    check("local-model      preview, install and control" in launcher.switchyard_help_text(), "and help names it")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = launcher.switchyard_main(["local-model", "install", "--approve", "nope"])
    check(result == 1 and "nothing was downloaded or installed" in out.getvalue(), f"through switchyard: {out.getvalue()}")
    importers = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "scripts").rglob("*.py")
                       if "local_model_service" in p.read_text(encoding="utf-8", errors="replace")
                       and p.name != "local_model_service.py")
    check(importers == ["scripts/team_launcher.py"], f"only the front door names it; no launch path does: {importers}")


def main() -> int:
    for name, value in list(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"local_model_service_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    raise SystemExit(main())
