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
          and body["service"]["tunable"] == {"gpu_layers": "16..40, default 24", "threads": "4..16, default 12"},
          "tuning within the stated bounds is part of what is approved")
    check(m.Settings(gpu_layers=41).problems() and m.Settings(gpu_layers=15).problems()
          and m.Settings(threads=32).problems() and not m.Settings(gpu_layers=40, threads=4).problems(),
          "and outside them is refused")
    with host() as h:
        before = tree(h.tmp)
        result, lines = said(m.preview, m.Settings(), h.layout, runner=h.runner)
        check(result == 0 and tree(h.tmp) == before and [c[0] for c in h.calls] == ["nvidia-smi"],
              f"preview writes nothing and runs only a read: {h.calls}")
        text = "\n".join(lines)
        for needle in ("nothing has been downloaded or installed", "Qwen3.8-27B-Q4_K_M.gguf", "sha256",
                       "llama.cpp b11803", "packages: none", "loopback only", "never at boot, never by a project launch",
                       "MemoryMax 32G", "at most 2 starts in 15 minutes", "rollback:", "now:      gpu: NVIDIA GeForce RTX 4070",
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
                       f"ReadOnlyPaths={h.layout.engine_dir} {h.layout.model_dir}"):
            check(wanted in lines, f"the unit carries {wanted}")
        check("[Install]" not in unit and "WantedBy" not in unit, "no [Install]: it never starts at boot")
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
        check(f"ExecStart={server} --model {h.layout.model_path}" in unit, "the unit runs the recorded program")
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
              and not h.layout.state_root.exists(), f"its unit, engine, model and record are gone: {lines} {sorted(left)}")
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
        check(result == 0 and h.systemctl() == [["systemctl", "stop", "switchyard-local-model.service"]], f"stop: {h.calls}")


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
