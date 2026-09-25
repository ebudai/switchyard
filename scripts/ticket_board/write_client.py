"""HTTP write client for the ticket-board action API."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib import error as urllib_error
from urllib import parse, request

CALLER_ROLE_HEADER = "X-Ticket-Board-Caller-Role"
WRITE_TOKEN_HEADER = "X-Ticket-Board-Write-Token"
REPORT_TOKEN_HEADER = "X-Ticket-Board-Report-Token"
LEGACY_CALLER_ROLE_HEADER = "X-PGU-Caller-Role"
LIVE_BOARD_URL_HOSTS = frozenset({"127.0.0.1", "localhost"})
LIVE_BOARD_URL_PORT = 8770
LIVE_BOARD_SOCKET_PATHS = frozenset(
    {
        "/run/pgu-ticket-board/ticket-board.sock",
        "/tmp/pgu-ticket-board.sock",
    }
)
ALLOW_PRODUCTION_WRITES_UNDER_TEST_ENV = "TICKET_BOARD_ALLOW_PRODUCTION_WRITES_UNDER_TEST"
TEST_INTERPRETER_NAMES = frozenset(
    {
        "bash",
        "dash",
        "fish",
        "python",
        "python3",
        "sh",
        "zsh",
    }
)
SHELL_INTERPRETER_NAMES = frozenset({"bash", "dash", "fish", "sh", "zsh"})
TEST_RUNNER_NAMES = frozenset({"pytest", "py.test"})


def _env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


#: No hard-coded endpoint any more, and in particular no hard-coded PROJECT.
#: These used to fall back to pgu's port and pgu's socket, so on a host running
#: several tenants a scrubbed environment aimed every project's client at pgu --
#: which is how `--board-url <dead port>` posted a comment to PGU-1 from syrd
#: (SYRD-198). An endpoint nobody named is now an error, not a guess.
DEFAULT_BOARD_URL = _env_first("TICKET_BOARD_URL", "PGU_TICKET_BOARD_URL")
DEFAULT_REPORT_BOARD_URL = _env_first("TICKET_BOARD_REPORT_URL", "PGU_TICKET_BOARD_REPORT_URL")
DEFAULT_BOARD_SOCKET = _env_first("TICKET_BOARD_SOCKET", "PGU_TICKET_BOARD_SOCKET")
DEFAULT_WRITE_TOKEN = _env_first("TICKET_BOARD_WRITE_TOKEN", "PGU_TICKET_BOARD_WRITE_TOKEN")
DEFAULT_REPORT_TOKEN = _env_first("TICKET_BOARD_TENANT_REPORT_TOKEN", "TICKET_BOARD_REPORT_TOKEN")
DEFAULT_REPORT_TOKEN_FILE = _env_first("TICKET_BOARD_TENANT_REPORT_TOKEN_FILE", "TICKET_BOARD_REPORT_TOKEN_FILE")
DEFAULT_REPORT_ORIGIN_PROJECT = _env_first("TICKET_BOARD_REPORT_ORIGIN_PROJECT")
#: Kept only so the production-write guard still recognises it as a live board.
#: It is never selected implicitly any more.
LEGACY_BOARD_SOCKET = "/tmp/pgu-ticket-board.sock"


class TicketBoardWriteError(RuntimeError):
    """Raised when the board rejects a write request."""


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def _normalize_api_url(board_url: str) -> str:
    normalized = board_url.rstrip("/")
    if normalized.endswith("/api/tickets"):
        return normalized
    if normalized.endswith("/api"):
        return f"{normalized}/tickets"
    return f"{normalized}/api/tickets"


def _normalize_api_path(board_url: str) -> str:
    parsed = parse.urlparse(_normalize_api_url(board_url))
    return parsed.path or "/api/tickets"


def _default_socket_path(board_url: str, environ: Mapping[str, str] = os.environ) -> str | None:
    """The socket to use when the caller named no endpoint of its own.

    This is a LIBRARY contract and it is unchanged: with the default board URL,
    the ambient socket wins, then the runtime socket if it exists, then the
    legacy one. Module attributes are read at call time because callers patch
    them.

    It deliberately does NOT try to decide whether the caller "explicitly" chose
    a URL. It cannot: a value equal to the default is indistinguishable from no
    value here, which is exactly how SYRD-198 happened. That decision belongs to
    `resolve_endpoint`, which sees whether the flag was supplied.
    """
    if board_url != DEFAULT_BOARD_URL:
        return None
    explicit = _env_first_from(environ, "TICKET_BOARD_SOCKET", "PGU_TICKET_BOARD_SOCKET")
    if explicit:
        return explicit
    if DEFAULT_BOARD_SOCKET and Path(DEFAULT_BOARD_SOCKET).exists():
        return DEFAULT_BOARD_SOCKET
    if LEGACY_BOARD_SOCKET and DEFAULT_BOARD_SOCKET != LEGACY_BOARD_SOCKET and Path(LEGACY_BOARD_SOCKET).exists():
        return LEGACY_BOARD_SOCKET
    return None


class EndpointError(TicketBoardWriteError):
    """The endpoint could not be resolved, or the two given disagree."""


def _project_at_url(board_url: str, timeout: float = 5.0) -> str | None:
    root = board_url.rstrip("/")
    if root.endswith("/api/tickets"):
        root = root[: -len("/api/tickets")]
    try:
        with request.urlopen(f"{root}/api/board", timeout=timeout) as response:
            return str(json.loads(response.read().decode("utf-8")).get("project") or "") or None
    except Exception:
        return None


def _project_at_socket(socket_path: str, timeout: float = 5.0) -> str | None:
    try:
        conn = UnixHTTPConnection(socket_path, timeout)
        conn.request("GET", "/api/board")
        body = conn.getresponse().read().decode("utf-8")
        conn.close()
        return str(json.loads(body).get("project") or "") or None
    except Exception:
        return None


def resolve_endpoint(
    board_url: str | None,
    socket_path: str | None,
    *,
    environ: Mapping[str, str] = os.environ,
) -> tuple[str, str | None]:
    """Decide the one endpoint a write goes to, from what the caller supplied.

    `None` means the flag was not given; an empty string means it was given and
    deliberately emptied. The rules, in order:

      * an explicitly EMPTY socket means no socket, and never a different one;
      * an explicit socket is authoritative;
      * an explicit board URL with no explicit socket means HTTP only -- it will
        never reach for an environment or legacy socket, which is the whole of
        SYRD-198;
      * with neither supplied, the environment decides;
      * and if nothing names an endpoint, this fails rather than guessing, since
        every guess available to it belongs to some particular project.

    When both are supplied they must agree about which project they are, and
    that is checked against the live boards rather than inferred from a path.
    """
    url_given = board_url is not None
    socket_given = socket_path is not None
    url = (board_url or "").strip() or (None if url_given else (DEFAULT_BOARD_URL or None))
    sock = (socket_path or "").strip() or None

    if socket_given and not sock:
        sock = None
    elif not socket_given:
        sock = None if url_given else _default_socket_path(url or "", environ)

    if not url and not sock:
        raise EndpointError(
            "no ticket board endpoint: pass --board-url or --socket, or set "
            "TICKET_BOARD_URL or TICKET_BOARD_SOCKET. Nothing is assumed, because "
            "every default available here would name one particular project."
        )
    if url and sock and url_given and socket_given:
        url_project = _project_at_url(url)
        sock_project = _project_at_socket(sock)
        if url_project and sock_project and url_project != sock_project:
            raise EndpointError(
                f"--board-url is {url} (project {url_project}) but --socket is {sock} "
                f"(project {sock_project}); refusing to write to one while naming the other"
            )
    return url or "", sock


def _has_explicit_socket_path(socket_path: str | None, environ: Mapping[str, str] = os.environ) -> bool:
    return bool(socket_path or _env_first_from(environ, "TICKET_BOARD_SOCKET", "PGU_TICKET_BOARD_SOCKET"))


def _env_first_from(environ: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = environ.get(name, "").strip()
        if value:
            return value
    return ""


def _unquote_env_file_value(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


def _read_report_token_file(path: str) -> str:
    token_file = Path(path).expanduser()
    try:
        lines = token_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TicketBoardWriteError(f"cannot read report token file {token_file}: {exc}") from exc
    raw_token = ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raw_token = raw_token or stripped
            continue
        key, value = stripped.split("=", 1)
        if key.strip() in {"TICKET_BOARD_TENANT_REPORT_TOKEN", "TICKET_BOARD_REPORT_TOKEN"}:
            return _unquote_env_file_value(value)
    return raw_token


def default_caller_role(environ: Mapping[str, str] = os.environ) -> str:
    return _env_first_from(environ, "TICKET_BOARD_CALLER_ROLE", "PGU_TICKET_BOARD_CALLER_ROLE").lower()


def _env_truthy(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _proc_parts(path: Path) -> list[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in data.split(b"\0") if part]


def _arg_looks_like_test_path(arg: str) -> bool:
    normalized = arg.replace("\\", "/")
    name = Path(normalized).name
    return (
        normalized.startswith("tests/")
        or "/tests/" in normalized
        or name.endswith("_test.py")
        or name.endswith("_test.sh")
        or name.startswith("test_")
    )


def _cmdline_looks_like_test_process(cmdline: list[str]) -> bool:
    if not cmdline:
        return False
    program = Path(cmdline[0]).name
    if program in TEST_RUNNER_NAMES or _arg_looks_like_test_path(cmdline[0]):
        return True
    if program not in TEST_INTERPRETER_NAMES:
        return False

    index = 1
    while index < len(cmdline):
        arg = cmdline[index]
        if arg == "--":
            index += 1
            break
        if arg == "-c":
            return False
        if program in SHELL_INTERPRETER_NAMES and arg.startswith("-") and "c" in arg[1:]:
            return False
        if arg == "-m":
            module = cmdline[index + 1] if index + 1 < len(cmdline) else ""
            return module in TEST_RUNNER_NAMES or module.startswith("pytest")
        if arg.startswith("-"):
            index += 1
            continue
        break
    return index < len(cmdline) and _arg_looks_like_test_path(cmdline[index])


def _running_under_test_process(environ: Mapping[str, str] = os.environ, *, max_depth: int = 12) -> bool:
    if _env_truthy(environ, "TICKET_BOARD_TEST_MODE") or environ.get("PYTEST_CURRENT_TEST"):
        return True
    pid = os.getpid()
    seen: set[int] = set()
    for _ in range(max_depth):
        if pid <= 1 or pid in seen:
            return False
        seen.add(pid)
        proc_dir = Path("/proc") / str(pid)
        cmdline = _proc_parts(proc_dir / "cmdline")
        if _cmdline_looks_like_test_process(cmdline):
            return True
        status = {}
        try:
            status_text = (proc_dir / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        for line in status_text.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                status[key] = value.strip()
        try:
            pid = int(status.get("PPid", "0"))
        except ValueError:
            return False
    return False


def _normalized_socket_path(socket_path: str | None) -> str:
    if not socket_path:
        return ""
    return os.path.normpath(socket_path.strip())


def _is_live_board_socket(socket_path: str | None) -> bool:
    return _normalized_socket_path(socket_path) in LIVE_BOARD_SOCKET_PATHS


def _is_live_board_url(board_url: str) -> bool:
    parsed = parse.urlparse(board_url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if (parsed.hostname or "").lower() not in LIVE_BOARD_URL_HOSTS:
        return False
    return (parsed.port or (443 if parsed.scheme == "https" else 80)) == LIVE_BOARD_URL_PORT


def _refuse_production_write_under_test(
    board_url: str,
    socket_path: str | None,
    environ: Mapping[str, str] = os.environ,
) -> None:
    if _env_truthy(environ, ALLOW_PRODUCTION_WRITES_UNDER_TEST_ENV):
        return
    if not _running_under_test_process(environ):
        return
    if _is_live_board_socket(socket_path):
        target = socket_path
    elif socket_path:
        return
    elif _is_live_board_url(board_url):
        target = board_url
    else:
        return
    raise TicketBoardWriteError(
        "refusing production ticket-board write while running under test; "
        f"target {target} is the live board. Point the test at a disposable board target "
        f"or set {ALLOW_PRODUCTION_WRITES_UNDER_TEST_ENV}=1 only for an intentional production write."
    )


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def _searched_repository() -> str:
    """The repository _run_git actually looked in, for error messages.

    _run_git passes no -C and no --git-dir, so it resolves against the current
    working directory. That is fine until the commit lives in a different
    checkout, at which point the caller needs to be told WHERE we looked.
    """
    toplevel = _run_git(["rev-parse", "--show-toplevel"])
    if toplevel.returncode == 0 and toplevel.stdout.strip():
        return toplevel.stdout.strip()
    return ""


def _unresolved_commit_message(commit_hash: str) -> str:
    """Say where we looked, because "unknown" is a claim we cannot support.

    git cannot distinguish "this hash is wrong" from "this hash is right and
    lives in another checkout", and the client resolves against the current
    working directory. The old message picked the first reading and stated it
    as fact, which sent the reader off to check a sha that was fine. Both
    readings go in the message, with the searched repository named so the
    reader can tell which one applies.
    """
    repo = _searched_repository()
    if repo:
        where = f"in the repository this command searched: {repo}"
    else:
        where = (
            "in any repository: the working directory is not a git checkout "
            f"({os.getcwd()})"
        )
    return (
        f"commit {commit_hash} was not found {where}. "
        "Commit hashes are resolved by running git in the current working "
        "directory, so a real commit that lives in a different checkout fails "
        "here exactly like a bad hash does. Rerun this from the checkout that "
        "holds the commit, or correct the hash."
    )


def _git_error_detail(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stderr.strip() or proc.stdout.strip() or f"git exited with status {proc.returncode}"


@dataclass(frozen=True)
class TicketBoardWriteClient:
    board_url: str = DEFAULT_BOARD_URL
    caller_role: str = field(default_factory=default_caller_role)
    timeout: float = 10.0
    socket_path: str | None = None
    report_token: str = DEFAULT_REPORT_TOKEN
    write_token: str = DEFAULT_WRITE_TOKEN
    report_board_url: str = DEFAULT_REPORT_BOARD_URL
    report_token_file: str = DEFAULT_REPORT_TOKEN_FILE
    #: "The caller named an HTTP endpoint and no socket", set only by the CLI,
    #: which is the only layer that can see whether a flag was SUPPLIED. It is
    #: last because for_caller() copies positionally, and it defaults to False
    #: so every existing construction of this class resolves exactly as before.
    socket_disabled: bool = False

    @property
    def api_url(self) -> str:
        return _normalize_api_url(self.board_url)

    @property
    def api_path(self) -> str:
        return _normalize_api_path(self.board_url)

    @property
    def report_api_url(self) -> str:
        return _normalize_api_url(self.report_board_url or self.board_url)

    @property
    def effective_socket_path(self) -> str | None:
        # `socket_disabled` is the only addition, and only the CLI sets it. The
        # resolution below is the library contract, untouched: a caller that
        # constructs this class directly gets exactly what it always got.
        if self.socket_disabled:
            return None
        return self.socket_path or _default_socket_path(self.board_url)

    def for_caller(self, caller_role: str) -> "TicketBoardWriteClient":
        return TicketBoardWriteClient(
            board_url=self.board_url,
            caller_role=caller_role,
            timeout=self.timeout,
            socket_path=self.socket_path,
            report_token=self.report_token,
            write_token=self.write_token,
            report_board_url=self.report_board_url,
            report_token_file=self.report_token_file,
            socket_disabled=self.socket_disabled,
        )

    def _post(self, path: str, payload: dict[str, Any], *, caller_role: str | None = None) -> dict[str, Any]:
        role = (caller_role or self.caller_role).strip().lower()
        if not role:
            raise ValueError("caller_role must be non-empty")
        socket_path = self.effective_socket_path
        _refuse_production_write_under_test(self.board_url, socket_path)
        if socket_path:
            try:
                return self._post_unix(path, payload, role, socket_path)
            except OSError as exc:
                if _has_explicit_socket_path(self.socket_path):
                    raise
                print(f"WARNING: ticket board Unix socket {socket_path} failed ({exc}); falling back to TCP {self.api_url}", file=sys.stderr)
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", CALLER_ROLE_HEADER: role, LEGACY_CALLER_ROLE_HEADER: role}
        if self.write_token:
            headers[WRITE_TOKEN_HEADER] = self.write_token
        req = request.Request(
            f"{self.api_url}{path}",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise TicketBoardWriteError(response_body or f"HTTP {exc.code}") from exc
        parsed = json.loads(response_body)
        if not isinstance(parsed, dict):
            raise TicketBoardWriteError("ticket board response was not an object")
        return parsed

    def _post_report(self, payload: dict[str, Any], *, report_token: str | None = None) -> dict[str, Any]:
        token = (report_token if report_token is not None else self.report_token).strip()
        if not token and self.report_token_file.strip():
            token = _read_report_token_file(self.report_token_file.strip()).strip()
        if not token:
            raise ValueError("report_token must be non-empty")
        _refuse_production_write_under_test(self.report_board_url or self.board_url, None)
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.report_api_url}/actions/file_report",
            data=body,
            headers={"Content-Type": "application/json", REPORT_TOKEN_HEADER: token},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise TicketBoardWriteError(response_body or f"HTTP {exc.code}") from exc
        parsed = json.loads(response_body)
        if not isinstance(parsed, dict):
            raise TicketBoardWriteError("ticket board response was not an object")
        return parsed

    def verify_caller(self) -> dict[str, Any]:
        """Ask the board which role this process is, and change nothing.

        The same request every socket write begins with, on its own. Both the
        board that resolves a role from the peer uid and the one that accepts a
        claimed role answer it, so it is the probe a cutover can use to prove a
        role can actually write before anything depends on it (SYRD-45).
        """
        socket_path = self.effective_socket_path
        if not socket_path:
            raise TicketBoardWriteError(
                "verify-caller checks local socket authority; pass --socket or set TICKET_BOARD_SOCKET"
            )
        conn = UnixHTTPConnection(socket_path, self.timeout)
        try:
            return self._request_unix_json(
                conn, "/api/register-caller", {"role": self.caller_role}, expected_status=200
            )
        except OSError as exc:
            raise TicketBoardWriteError(f"cannot reach {socket_path}: {exc}") from exc
        finally:
            conn.close()

    def _post_unix(self, path: str, payload: dict[str, Any], role: str, socket_path: str) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        conn = UnixHTTPConnection(socket_path, self.timeout)
        try:
            self._request_unix_json(conn, "/api/register-caller", {"role": role}, expected_status=200)
            return self._request_unix_json(
                conn,
                f"{self.api_path}{path}",
                payload,
                expected_status=None,
                caller_role_header=role,
                body=body,
            )
        finally:
            conn.close()

    def _request_unix_json(
        self,
        conn: UnixHTTPConnection,
        path: str,
        payload: dict[str, Any],
        *,
        expected_status: int | None,
        caller_role_header: str | None = None,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        request_body = body if body is not None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if caller_role_header is not None:
            headers[CALLER_ROLE_HEADER] = caller_role_header
        conn.request("POST", path, body=request_body, headers=headers)
        response = conn.getresponse()
        response_body = response.read().decode("utf-8", errors="replace")
        if expected_status is not None and response.status != expected_status:
            raise TicketBoardWriteError(response_body or f"HTTP {response.status}")
        if expected_status is None and response.status >= 400:
            raise TicketBoardWriteError(response_body or f"HTTP {response.status}")
        parsed = json.loads(response_body)
        if not isinstance(parsed, dict):
            raise TicketBoardWriteError("ticket board response was not an object")
        return parsed

    def _ticket_action(
        self,
        ticket_id: str,
        operation: str,
        payload: dict[str, Any] | None = None,
        *,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        encoded_ticket = parse.quote(ticket_id.strip().upper(), safe="")
        encoded_operation = parse.quote(operation, safe="")
        return self._post(f"/{encoded_ticket}/actions/{encoded_operation}", payload or {}, caller_role=caller_role)

    def _require_commit_pushed_to_origin(self, commit_hash: str, *, operation: str) -> None:
        normalized = commit_hash.strip()
        if not normalized:
            return
        resolved = _run_git(["rev-parse", "--verify", f"{normalized}^{{commit}}"])
        if resolved.returncode != 0 or not resolved.stdout.strip():
            raise TicketBoardWriteError(_unresolved_commit_message(normalized))
        fetch = _run_git(["fetch", "origin"])
        if fetch.returncode != 0:
            raise TicketBoardWriteError(
                f"unable to verify commit {normalized} against origin before {operation}: {_git_error_detail(fetch)}"
            )
        contains = _run_git(
            [
                "for-each-ref",
                "--format=%(refname:short)",
                f"--contains={resolved.stdout.strip()}",
                "refs/remotes/origin",
            ]
        )
        if contains.returncode != 0:
            raise TicketBoardWriteError(
                f"unable to verify commit {normalized} against origin before {operation}: {_git_error_detail(contains)}"
            )
        if not any(line.strip().startswith("origin/") for line in contains.stdout.splitlines()):
            raise TicketBoardWriteError(
                f"commit {normalized} is not pushed to origin; "
                f"push your branch (git push origin HEAD:refs/heads/feature/...) before {operation}."
            )

    def create_ticket(
        self,
        *,
        title: str,
        body: str,
        screenshot: str | None = None,
        screenshots: list[str] | None = None,
        assignee: str = "unassigned",
        state: str = "analysis",
        parent_id: str = "",
        blocked_by: list[str] | None = None,
        blocked_reason: str = "",
        implementation: str = "",
        audit_prompt: str = "",
        needs_inspection: bool = False,
        needs_audit: bool = True,
        needs_user_signoff: bool = False,
        comment_text: str = "",
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": title,
            "body": body,
            "screenshot": screenshot,
            "screenshots": screenshots or [],
            "assignee": assignee,
            "state": state,
            "parent_id": parent_id,
            "blocked_by": blocked_by or [],
            "blocked_reason": blocked_reason,
            "implementation": implementation,
            "audit_prompt": audit_prompt,
            "needs_inspection": needs_inspection,
            "needs_audit": needs_audit,
            "needs_user_signoff": needs_user_signoff,
        }
        if comment_text:
            payload["comment_text"] = comment_text
        return self._post("/actions/create_ticket", payload, caller_role=caller_role)

    def file_bug(
        self,
        *,
        title: str,
        body: str,
        source_ticket_id: str,
        screenshot: str | None = None,
        screenshots: list[str] | None = None,
        assignee: str = "unassigned",
        blocked_by: list[str] | None = None,
        blocked_reason: str = "",
        needs_audit: bool = True,
        needs_user_signoff: bool = False,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/actions/file_bug",
            {
                "title": title,
                "body": body,
                "source_ticket_id": source_ticket_id,
                "screenshot": screenshot,
                "screenshots": screenshots or [],
                "assignee": assignee,
                "blocked_by": blocked_by or [],
                "blocked_reason": blocked_reason,
                "needs_audit": needs_audit,
                "needs_user_signoff": needs_user_signoff,
            },
            caller_role=caller_role,
        )

    def file_report(
        self,
        *,
        title: str,
        body: str,
        origin_project: str,
        external_source_ref: str = "",
        report_token: str | None = None,
    ) -> dict[str, Any]:
        return self._post_report(
            {
                "title": title,
                "body": body,
                "origin_project": origin_project,
                "external_source_ref": external_source_ref,
            },
            report_token=report_token,
        )

    def route(self, ticket_id: str, *, state: str, assignee: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "route", {"state": state, "assignee": assignee}, caller_role=caller_role)

    def reassign(
        self,
        ticket_id: str,
        *,
        assignee: str,
        reason: str,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self._ticket_action(
            ticket_id,
            "reassign",
            {"assignee": assignee, "reason": reason},
            caller_role=caller_role,
        )

    def release_draft(self, ticket_id: str, *, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "release_draft", {}, caller_role=caller_role)

    def director_edit(
        self,
        ticket_id: str,
        *,
        patch: dict[str, Any],
        reason: str,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self._ticket_action(
            ticket_id,
            "director_edit",
            {"patch": patch, "reason": reason},
            caller_role=caller_role,
        )

    def request_publication(
        self,
        ticket_id: str,
        *,
        ref: str,
        commit: str,
        bundle: str,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self._ticket_action(
            ticket_id,
            "request_publication",
            {"ref": ref, "commit": commit, "bundle": bundle},
            caller_role=caller_role,
        )

    def resolve_publication(
        self,
        ticket_id: str,
        *,
        request_id: int,
        outcome: str,
        detail: str = "",
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self._ticket_action(
            ticket_id,
            "resolve_publication",
            {"request_id": int(request_id), "outcome": outcome, "detail": detail},
            caller_role=caller_role,
        )

    def force_move(
        self,
        ticket_id: str,
        *,
        state: str,
        assignee: str,
        suppress_notification: bool = False,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self._ticket_action(
            ticket_id,
            "force_move",
            {"state": state, "assignee": assignee, "suppress_notification": suppress_notification},
            caller_role=caller_role,
        )

    def override_move(
        self,
        ticket_id: str,
        *,
        state: str,
        assignee: str,
        notify: bool = True,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self._ticket_action(
            ticket_id,
            "override_move",
            {"state": state, "assignee": assignee, "suppress_notification": not notify},
            caller_role=caller_role,
        )

    def start_work(self, ticket_id: str, *, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "start_work", caller_role=caller_role)

    def configure_workflow(self, document: dict[str, Any], *, expected_revision: int, dry_run: bool = False) -> dict[str, Any]:
        return self._post("/actions/configure_workflow", {"document": document, "expected_revision": expected_revision, "dry_run": dry_run})

    def workflow_action(self, ticket_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("commit_hash"):
            self._require_commit_pushed_to_origin(payload["commit_hash"], operation=action)
        return self._ticket_action(ticket_id, action, payload)

    def set_workflow_flags(self, ticket_id: str, patch: dict[str, bool]) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "set_workflow_flags", patch)

    def submit_to_audit(self, ticket_id: str, *, commit_hash: str = "", caller_role: str | None = None) -> dict[str, Any]:
        self._require_commit_pushed_to_origin(commit_hash, operation="submit_to_audit")
        return self._ticket_action(ticket_id, "submit_to_audit", {"commit_hash": commit_hash}, caller_role=caller_role)

    def submit_to_audit_without_commit(
        self, ticket_id: str, *, reason: str, caller_role: str | None = None
    ) -> dict[str, Any]:
        """Submit finished work that produced no commit, with a stated reason.

        Deliberately not a flag on submit_to_audit: this skips the commit
        requirement, so it should read as its own act in the CLI, in the RBAC
        table and in the ticket's comment trail rather than as an option on the
        ordinary path.
        """
        return self._ticket_action(
            ticket_id, "submit_to_audit_without_commit", {"reason": reason}, caller_role=caller_role
        )

    def submit_to_inspection(self, ticket_id: str, *, commit_hash: str = "", caller_role: str | None = None) -> dict[str, Any]:
        if commit_hash:
            self._require_commit_pushed_to_origin(commit_hash, operation="submit_to_inspection")
        return self._ticket_action(ticket_id, "submit_to_inspection", {"commit_hash": commit_hash} if commit_hash else {}, caller_role=caller_role)

    def implementer_kick_back(self, ticket_id: str, *, reason: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "implementer_kick_back", {"reason": reason}, caller_role=caller_role)

    def request_commit_exempt(self, ticket_id: str, *, reason: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "request_commit_exempt", {"reason": reason}, caller_role=caller_role)

    def start_task(self, ticket_id: str, *, text: str = "", caller_role: str | None = None) -> dict[str, Any]:
        payload = {"text": text} if text else None
        return self._ticket_action(ticket_id, "start_task", payload, caller_role=caller_role)

    def complete_task(self, ticket_id: str, *, text: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "complete_task", {"text": text}, caller_role=caller_role)

    def recover_stalled_ticket(
        self, ticket_id: str, *, reason: str, caller_role: str | None = None
    ) -> dict[str, Any]:
        return self._ticket_action(
            ticket_id, "recover_stalled_ticket", {"reason": reason}, caller_role=caller_role
        )

    def request_dependency(
        self, ticket_id: str, *, role: str, reason: str, caller_role: str | None = None
    ) -> dict[str, Any]:
        return self._ticket_action(
            ticket_id, "request_dependency", {"role": role, "reason": reason}, caller_role=caller_role
        )

    def release_external_blocker(
        self, ticket_id: str, *, ref: str, reason: str, commit: str = "", caller_role: str | None = None
    ) -> dict[str, Any]:
        payload = {"ref": ref, "reason": reason}
        if commit:
            payload["commit"] = commit
        return self._ticket_action(ticket_id, "release_external_blocker", payload, caller_role=caller_role)

    def await_role(self, ticket_id: str, *, role: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "await_role", {"role": role}, caller_role=caller_role)

    def clear_awaiting_role(self, ticket_id: str, *, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "clear_awaiting_role", caller_role=caller_role)

    def audit_sign_off(self, ticket_id: str, *, text: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "audit_sign_off", {"text": text}, caller_role=caller_role)

    def audit_kick_back(
        self,
        ticket_id: str,
        *,
        reason: str,
        target_assignee: str = "",
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        payload = {"reason": reason}
        if target_assignee:
            payload["target_assignee"] = target_assignee
        return self._ticket_action(ticket_id, "audit_kick_back", payload, caller_role=caller_role)

    def director_dat_sign_off(self, ticket_id: str, *, text: str = "", caller_role: str | None = None) -> dict[str, Any]:
        payload = {"text": text} if text else None
        return self._ticket_action(ticket_id, "director_dat_sign_off", payload, caller_role=caller_role)

    def director_dat_kick_back(
        self,
        ticket_id: str,
        *,
        reason: str,
        target_assignee: str = "",
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        payload = {"reason": reason}
        if target_assignee:
            payload["target_assignee"] = target_assignee
        return self._ticket_action(ticket_id, "director_dat_kick_back", payload, caller_role=caller_role)

    def inspector_sign_off(self, ticket_id: str, *, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "inspector_sign_off", caller_role=caller_role)

    def inspector_kick_back(
        self,
        ticket_id: str,
        *,
        recommendations: str,
        target_assignee: str = "",
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        payload = {"recommendations": recommendations}
        if target_assignee:
            payload["target_assignee"] = target_assignee
        return self._ticket_action(ticket_id, "inspector_kick_back", payload, caller_role=caller_role)

    def user_sign_off(self, ticket_id: str, *, text: str = "", caller_role: str | None = None) -> dict[str, Any]:
        payload = {"text": text} if text else None
        return self._ticket_action(ticket_id, "user_sign_off", payload, caller_role=caller_role)

    def user_reopen(self, ticket_id: str, *, reason: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "user_reopen", {"reason": reason}, caller_role=caller_role)

    def mark_done(self, ticket_id: str, *, commit_hash: str = "", caller_role: str | None = None) -> dict[str, Any]:
        # No hash is no field. The board validates any hash it is GIVEN, so an
        # empty one was refused as "invalid commit hash" before the ticket's
        # commit exemption was ever consulted -- an audited no-code ticket
        # could not be closed without inventing provenance (SYRD-267). An
        # ordinary ticket with no hash is still refused, by the commit
        # requirement itself.
        payload = {"commit_hash": commit_hash} if commit_hash.strip() else {}
        return self._ticket_action(ticket_id, "mark_done", payload, caller_role=caller_role)

    def defer(self, ticket_id: str, *, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "defer", caller_role=caller_role)

    def cancel(self, ticket_id: str, *, reason: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "cancel", {"reason": reason}, caller_role=caller_role)

    def set_manually_controlled(self, ticket_id: str, value: bool, *, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "set_manually_controlled", {"manually_controlled": value}, caller_role=caller_role)

    def set_blockers(
        self,
        ticket_id: str,
        *,
        blocked_by: list[str],
        blocked_reason: str,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self._ticket_action(
            ticket_id,
            "set_blockers",
            {"blocked_by": blocked_by, "blocked_reason": blocked_reason},
            caller_role=caller_role,
        )

    def add_comment(self, ticket_id: str, *, text: str, urgent: bool = False, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "add_comment", {"text": text, "urgent": urgent}, caller_role=caller_role)

    def edit_fields(self, ticket_id: str, fields: dict[str, Any], *, caller_role: str | None = None) -> dict[str, Any]:
        if not isinstance(fields, dict):
            raise TicketBoardWriteError("edit_fields requires a JSON object")
        return self._ticket_action(ticket_id, "edit_fields", fields, caller_role=caller_role)

    def merge(self, source_ticket_id: str, *, target_id: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(source_ticket_id, "merge", {"target_id": target_id}, caller_role=caller_role)

    def dismiss_notification(
        self,
        notification_id: int | None = None,
        *,
        ticket_id: str = "",
        target_role: str = "",
        kind: str = "transition",
        reason: str = "",
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any]
        if notification_id is not None:
            payload = {"notification_id": notification_id, "reason": reason}
        else:
            payload = {"ticket_id": ticket_id, "target_role": target_role, "kind": kind, "reason": reason}
        return self._post(
            "/actions/dismiss_notification",
            payload,
            caller_role=caller_role,
        )


def _ticket_from_response(response: dict[str, Any]) -> dict[str, Any]:
    ticket = response.get("ticket")
    if not isinstance(ticket, dict):
        raise TicketBoardWriteError("ticket board response did not include ticket")
    return ticket


#: Free-text fields carry prose -- comment bodies, reasons, ticket bodies,
#: implementation notes. Prose in this project routinely contains backticks
#: (`function_name()`), dollar signs, quotes and newlines, and passing it as a
#: shell argument means the author's shell gets a vote on what reaches the
#: board. On SYRD-195 it took one: a review comment was posted with its
#: operative phrase missing, because the shell had executed the backticked
#: phrase instead of passing it (SYRD-196).
#:
#: So every free-text option gains two companions that cannot be interpreted:
#: `--<name>-file PATH` reads the bytes from a file, and `--<name> -` reads
#: them from standard input. The direct `--<name> VALUE` form is unchanged, so
#: nothing that works today stops working.
#:
#: Content is preserved exactly. No stripping, no newline normalisation, no
#: shell, no `eval`: the bytes in the file are the bytes the board stores.
FREE_TEXT_STDIN = "-"


def add_free_text_argument(parser, flag, *, required=False, default="", help=None):
    """Register `--flag`, `--flag-file`, and the resolution rule for both.

    `required` is enforced after parsing rather than by argparse, because either
    form may satisfy it and argparse can only require one option at a time.
    """
    dest = flag.lstrip("-").replace("-", "_")
    direct_help = help or f"{dest.replace('_', ' ')} text"
    parser.add_argument(
        flag,
        dest=dest,
        default=None,
        help=f"{direct_help}. Use {FREE_TEXT_STDIN} to read it from standard input.",
    )
    parser.add_argument(
        f"{flag}-file",
        dest=f"{dest}_file",
        default=None,
        metavar="PATH",
        help=(
            f"read {dest.replace('_', ' ')} from PATH ({FREE_TEXT_STDIN} for standard input), "
            "so backticks, dollar signs, quotes, newlines and leading dashes reach "
            "the board literally"
        ),
    )
    existing = list(getattr(parser, "_free_text_fields", ()))
    existing.append((flag, dest, required, default))
    parser._free_text_fields = existing
    parser.set_defaults(_free_text_fields=existing)


def _read_text_stream(stream, parser, flag):
    data = stream.buffer.read() if hasattr(stream, "buffer") else stream.read()
    if isinstance(data, bytes):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            parser.error(f"{flag}: standard input is not valid UTF-8 ({exc})")
    return data


def _read_free_text(source, parser, flag):
    import sys as _sys

    if source == FREE_TEXT_STDIN:
        if _sys.stdin is None:
            parser.error(f"{flag}: asked to read standard input, but there is none")
        if _sys.stdin.isatty():
            # Reading a terminal here would hang with no output, which reads as
            # the command having silently stopped.
            parser.error(
                f"{flag}: standard input is a terminal; redirect a file or a heredoc into it"
            )
        return _read_text_stream(_sys.stdin, parser, flag)
    try:
        with open(source, "rb") as handle:
            return handle.read().decode("utf-8")
    except FileNotFoundError:
        parser.error(f"{flag}: no such file: {source}")
    except IsADirectoryError:
        parser.error(f"{flag}: is a directory: {source}")
    except PermissionError:
        parser.error(f"{flag}: cannot read: {source}")
    except UnicodeDecodeError as exc:
        parser.error(f"{flag}: {source} is not valid UTF-8 ({exc})")


def resolve_free_text_arguments(args, parser):
    """Turn --flag / --flag-file into the single value the dispatch already reads.

    Resolved onto the same attribute the direct flag would have set, so every
    call site downstream is untouched.
    """
    for flag, dest, required, default in getattr(args, "_free_text_fields", ()):
        direct = getattr(args, dest, None)
        path = getattr(args, f"{dest}_file", None)
        if direct is not None and path is not None:
            parser.error(f"{flag} and {flag}-file are mutually exclusive; give one")
        if path is not None:
            text = _read_free_text(path, parser, flag)
        elif direct is not None:
            text = _read_free_text(direct, parser, flag) if direct == FREE_TEXT_STDIN else direct
        else:
            text = default
        if required and not text.strip():
            parser.error(f"{flag} (or {flag}-file) is required and must not be empty")
        setattr(args, dest, text)
    return args


def _build_parser() -> argparse.ArgumentParser:
    caller_default = default_caller_role()
    caller_default_display = caller_default or "unset"
    parser = argparse.ArgumentParser(description="Write tickets through the board action API.")
    parser.add_argument(
        "--board-url",
        default=None,
        help=(
            "Board root or /api/tickets URL. Supplying it is authoritative: the write "
            "goes there or fails, and never falls back to a socket belonging to "
            "another project (SYRD-198). Default: TICKET_BOARD_URL."
        ),
    )
    parser.add_argument(
        "--socket",
        dest="socket_path",
        default=None,
        help=(
            "Unix-domain board socket for local pane writes. Defaults to TICKET_BOARD_SOCKET "
            "(or legacy PGU_TICKET_BOARD_SOCKET), "
            f"or {DEFAULT_BOARD_SOCKET} when it exists and --board-url is the default."
        ),
    )
    parser.add_argument(
        "--caller-role",
        default=caller_default,
        help=(
            "Value for X-Ticket-Board-Caller-Role "
            "(required unless TICKET_BOARD_CALLER_ROLE or legacy PGU_TICKET_BOARD_CALLER_ROLE is set; "
            f"currently {caller_default_display})"
        ),
    )
    parser.add_argument(
        "--report-token",
        default=DEFAULT_REPORT_TOKEN,
        help="Tenant report token for file-report. Default: TICKET_BOARD_TENANT_REPORT_TOKEN or TICKET_BOARD_REPORT_TOKEN.",
    )
    parser.add_argument(
        "--report-token-file",
        default=DEFAULT_REPORT_TOKEN_FILE,
        help=(
            "EnvironmentFile-style file containing TICKET_BOARD_TENANT_REPORT_TOKEN for file-report. "
            "Default: TICKET_BOARD_TENANT_REPORT_TOKEN_FILE or TICKET_BOARD_REPORT_TOKEN_FILE."
        ),
    )
    parser.add_argument(
        "--report-board-url",
        default=DEFAULT_REPORT_BOARD_URL,
        help="Target board URL for file-report. Default: TICKET_BOARD_REPORT_URL or PGU_TICKET_BOARD_REPORT_URL; falls back to --board-url.",
    )
    parser.add_argument(
        "--write-token",
        default=DEFAULT_WRITE_TOKEN,
        help="HTTP write token for TCP board action routes. Default: TICKET_BOARD_WRITE_TOKEN or legacy PGU_TICKET_BOARD_WRITE_TOKEN.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "verify-caller",
        help="ask the board which role this process is, over the local socket; changes nothing",
    )
    workflow_action = subparsers.add_parser("workflow-action")
    workflow_action.add_argument("ticket_id")
    workflow_action.add_argument("action")
    workflow_action.add_argument("--payload-json", default="{}")
    workflow_flags = subparsers.add_parser("set-workflow-flags")
    workflow_flags.add_argument("ticket_id")
    workflow_flags.add_argument("--patch-json", required=True)

    create = subparsers.add_parser("create-ticket")
    create.add_argument("--title", required=True)
    add_free_text_argument(create, "--body", required=True)
    create.add_argument("--assignee", default="unassigned")
    create.add_argument("--state", default="analysis")
    create.add_argument("--parent-id", default="")
    create.add_argument("--draft", action="store_true")
    create.add_argument("--screenshot")
    create.add_argument("--blocked-by", action="append", default=[])
    add_free_text_argument(create, "--blocked-reason", default="")
    add_free_text_argument(create, "--implementation", default="")
    add_free_text_argument(create, "--audit-prompt", default="")
    add_free_text_argument(create, "--comment-text", default="")
    create.add_argument("--needs-inspection", action="store_true")
    create.add_argument("--needs-audit", action=argparse.BooleanOptionalAction, default=True)
    create.add_argument("--needs-user-signoff", action="store_true")

    file_bug = subparsers.add_parser("file-bug")
    file_bug.add_argument("--title", required=True)
    add_free_text_argument(file_bug, "--body", required=True)
    file_bug.add_argument("--source-ticket-id", required=True)
    file_bug.add_argument("--assignee", default="unassigned")
    file_bug.add_argument("--needs-audit", action=argparse.BooleanOptionalAction, default=True)

    file_report = subparsers.add_parser("file-report")
    file_report.add_argument("--title", required=True)
    add_free_text_argument(file_report, "--body", required=True)
    file_report.add_argument("--origin-project", default=DEFAULT_REPORT_ORIGIN_PROJECT)
    file_report.add_argument("--external-source-ref", default="")

    route = subparsers.add_parser("route")
    route.add_argument("ticket_id")
    route.add_argument("--state", required=True)
    route.add_argument("--assignee", required=True)

    reassign = subparsers.add_parser("reassign")
    reassign.add_argument("ticket_id")
    reassign.add_argument("--assignee", required=True)
    add_free_text_argument(reassign, "--reason", required=True)

    request_publication = subparsers.add_parser("request-publication")
    request_publication.add_argument("ticket_id")
    request_publication.add_argument("--ref", required=True)
    request_publication.add_argument("--commit", required=True)
    request_publication.add_argument("--bundle", required=True)

    resolve_publication = subparsers.add_parser("resolve-publication")
    resolve_publication.add_argument("ticket_id")
    resolve_publication.add_argument("--request-id", required=True, type=int)
    resolve_publication.add_argument("--outcome", required=True, choices=("published", "rejected"))
    add_free_text_argument(resolve_publication, "--reason", default="")

    director_edit = subparsers.add_parser("director-edit")
    director_edit.add_argument("ticket_id")
    director_edit.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE")
    add_free_text_argument(director_edit, "--reason", required=True)

    force_move = subparsers.add_parser("force-move")
    force_move.add_argument("ticket_id")
    force_move.add_argument("--state", required=True)
    force_move.add_argument("--assignee", required=True)
    force_move.add_argument("--suppress-notification", action="store_true")

    override_move = subparsers.add_parser("override-move")
    override_move.add_argument("ticket_id")
    override_move.add_argument("--state", required=True)
    override_move.add_argument("--assignee", required=True)
    override_move.add_argument("--no-notify", action="store_true")

    for name in ("start-work", "inspector-sign-off", "user-sign-off", "release-draft", "defer"):
        sub = subparsers.add_parser(name)
        sub.add_argument("ticket_id")

    start_task = subparsers.add_parser("start-task")
    start_task.add_argument("ticket_id")
    add_free_text_argument(start_task, "--text", default="")

    complete_task = subparsers.add_parser("complete-task")
    complete_task.add_argument("ticket_id")
    add_free_text_argument(complete_task, "--text", required=True)

    audit_sign = subparsers.add_parser("audit-sign-off")
    audit_sign.add_argument("ticket_id")
    add_free_text_argument(audit_sign, "--text", required=True)

    submit = subparsers.add_parser("submit-to-audit")
    submit.add_argument("ticket_id")
    submit.add_argument("--commit-hash", default="")

    submit_no_commit = subparsers.add_parser("submit-to-audit-without-commit")
    submit_no_commit.add_argument("ticket_id")
    add_free_text_argument(submit_no_commit, "--reason", required=True)

    submit_inspection = subparsers.add_parser("submit-to-inspection")
    submit_inspection.add_argument("ticket_id")
    submit_inspection.add_argument("--commit-hash", default="")

    implementer_kick = subparsers.add_parser("implementer-kick-back")
    implementer_kick.add_argument("ticket_id")
    add_free_text_argument(implementer_kick, "--reason", required=True)

    request_exempt = subparsers.add_parser("request-commit-exempt")
    request_exempt.add_argument("ticket_id")
    add_free_text_argument(request_exempt, "--reason", required=True)

    recover_stalled = subparsers.add_parser(
        "recover-stalled-ticket",
        help="take the transition a stalled ticket's owner did not take, to its next required gate",
    )
    recover_stalled.add_argument("ticket_id")
    add_free_text_argument(recover_stalled, "--reason", required=True, help="why the recovery is being made")
    request_dependency = subparsers.add_parser(
        "request-dependency",
        help="record why this work is waiting and hand it to the role it waits on, in one action",
    )
    request_dependency.add_argument("ticket_id")
    request_dependency.add_argument("--role", required=True, help="the role this work waits on")
    add_free_text_argument(request_dependency, "--reason", required=True, help="what they have to do, in their words")
    release_external = subparsers.add_parser(
        "release-external-blocker",
        help="end a wait on another board's work, explicitly and with why (SYRD-270)",
    )
    release_external.add_argument("ticket_id")
    release_external.add_argument("--ref", required=True, help="the external blocker, as project:PREFIX-N")
    add_free_text_argument(release_external, "--reason", required=True, help="what happened that ends the wait")
    release_external.add_argument(
        "--commit",
        default="",
        help="a commit this board must itself recognise before the wait may end",
    )
    await_role = subparsers.add_parser("await-role")
    await_role.add_argument("ticket_id")
    await_role.add_argument("--role", required=True)

    clear_awaiting = subparsers.add_parser("clear-awaiting-role")
    clear_awaiting.add_argument("ticket_id")

    audit_kick = subparsers.add_parser("audit-kick-back")
    audit_kick.add_argument("ticket_id")
    add_free_text_argument(audit_kick, "--reason", required=True)
    audit_kick.add_argument("--target-assignee", default="")

    dat_sign = subparsers.add_parser("director-dat-sign-off")
    dat_sign.add_argument("ticket_id")
    add_free_text_argument(dat_sign, "--text", default="")

    dat_kick = subparsers.add_parser("director-dat-kick-back")
    dat_kick.add_argument("ticket_id")
    add_free_text_argument(dat_kick, "--reason", required=True)
    dat_kick.add_argument("--target-assignee", default="")

    for name in ("user-reopen", "cancel"):
        sub = subparsers.add_parser(name)
        sub.add_argument("ticket_id")
        add_free_text_argument(sub, "--reason", required=True)

    inspector_kick = subparsers.add_parser("inspector-kick-back")
    inspector_kick.add_argument("ticket_id")
    add_free_text_argument(inspector_kick, "--recommendations", required=True)
    inspector_kick.add_argument("--target-assignee", default="")

    done = subparsers.add_parser("mark-done")
    done.add_argument("ticket_id")
    done.add_argument("--commit-hash", default="")

    manual = subparsers.add_parser("set-manually-controlled")
    manual.add_argument("ticket_id")
    manual.add_argument("--value", action=argparse.BooleanOptionalAction, default=True)

    blockers = subparsers.add_parser("set-blockers")
    blockers.add_argument("ticket_id")
    blockers.add_argument(
        "--blocked-by",
        action="append",
        required=True,
        help=(
            "a ticket on this board (PREFIX-N), or work on another board as project:PREFIX-N; "
            "an external blocker never resolves by itself -- see release-external-blocker"
        ),
    )
    add_free_text_argument(blockers, "--blocked-reason", required=True)

    comment = subparsers.add_parser("add-comment")
    comment.add_argument("ticket_id")
    add_free_text_argument(comment, "--text", required=True)
    comment.add_argument("--urgent", action="store_true")

    edit_fields = subparsers.add_parser("edit-fields")
    edit_fields.add_argument("ticket_id")
    edit_fields.add_argument("--json", required=True, help="JSON object of fields accepted by the edit_fields action")

    merge = subparsers.add_parser("merge")
    merge.add_argument("ticket_id")
    merge.add_argument("--target-id", required=True)

    dismiss_notification = subparsers.add_parser("dismiss-notification")
    dismiss_notification.add_argument("notification_id", type=int, nargs="?")
    dismiss_notification.add_argument("--ticket-id", default="")
    dismiss_notification.add_argument("--target-role", default="")
    dismiss_notification.add_argument("--kind", default="transition")
    add_free_text_argument(dismiss_notification, "--reason", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        parser = _build_parser()
        args = parser.parse_args(argv)
        resolve_free_text_arguments(args, parser)
        command = args.command.replace("-", "_")
        if command != "file_report" and not args.caller_role.strip():
            raise TicketBoardWriteError(
                "ticket board caller role required; pass --caller-role or set TICKET_BOARD_CALLER_ROLE"
            )
        resolved_url, resolved_socket = resolve_endpoint(args.board_url, args.socket_path)
        args.board_url, args.socket_path = resolved_url, resolved_socket
        # The CLI knows what was SUPPLIED; the client cannot. Saying "no socket"
        # out loud is what stops the library rediscovering one (SYRD-198).
        socket_disabled = resolved_socket is None
        client = TicketBoardWriteClient(
            args.board_url,
            args.caller_role,
            socket_path=args.socket_path,
            socket_disabled=socket_disabled,
            report_token=args.report_token,
            write_token=args.write_token,
            report_board_url=args.report_board_url,
            report_token_file=args.report_token_file,
        )
        if command == "verify_caller":
            response = client.verify_caller()
        elif command == "create_ticket":
            response = client.create_ticket(
                title=args.title,
                body=args.body,
                screenshot=args.screenshot,
                assignee=args.assignee,
                state="draft" if args.draft else args.state,
                parent_id=args.parent_id,
                blocked_by=args.blocked_by,
                blocked_reason=args.blocked_reason,
                implementation=args.implementation,
                audit_prompt=args.audit_prompt,
                needs_inspection=args.needs_inspection,
                needs_audit=args.needs_audit,
                needs_user_signoff=args.needs_user_signoff,
                comment_text=args.comment_text,
            )
        elif command == "file_bug":
            response = client.file_bug(
                title=args.title,
                body=args.body,
                source_ticket_id=args.source_ticket_id,
                assignee=args.assignee,
                needs_audit=args.needs_audit,
            )
        elif command == "file_report":
            if not args.origin_project.strip():
                raise TicketBoardWriteError("file-report requires --origin-project or TICKET_BOARD_REPORT_ORIGIN_PROJECT")
            response = client.file_report(
                title=args.title,
                body=args.body,
                origin_project=args.origin_project,
                external_source_ref=args.external_source_ref,
            )
        elif command == "route":
            response = client.route(args.ticket_id, state=args.state, assignee=args.assignee)
        elif command == "reassign":
            response = client.reassign(args.ticket_id, assignee=args.assignee, reason=args.reason)
        elif command == "director_edit":
            patch: dict[str, Any] = {}
            for assignment in args.set:
                if "=" not in assignment:
                    raise SystemExit(f"--set expects FIELD=VALUE, got {assignment!r}")
                key, _, raw = assignment.partition("=")
                key = key.strip()
                # Typed the way the field is: a gate is a boolean and a title is
                # not, and the database refuses a value of the wrong shape
                # rather than coercing it.
                if raw in ("true", "false"):
                    patch[key] = raw == "true"
                else:
                    patch[key] = raw
            response = client.director_edit(
                args.ticket_id, patch=patch, reason=args.reason
            )
        elif command == "request_publication":
            response = client.request_publication(
                args.ticket_id, ref=args.ref, commit=args.commit, bundle=args.bundle
            )
        elif command == "resolve_publication":
            response = client.resolve_publication(
                args.ticket_id,
                request_id=args.request_id,
                outcome=args.outcome,
                detail=args.reason,
            )
        elif command == "force_move":
            response = client.force_move(
                args.ticket_id,
                state=args.state,
                assignee=args.assignee,
                suppress_notification=args.suppress_notification,
            )
        elif command == "override_move":
            response = client.override_move(
                args.ticket_id,
                state=args.state,
                assignee=args.assignee,
                notify=not args.no_notify,
            )
        elif command == "workflow_action":
            response = client.workflow_action(args.ticket_id, args.action, json.loads(args.payload_json))
        elif command == "set_workflow_flags":
            response = client.set_workflow_flags(args.ticket_id, json.loads(args.patch_json))
        elif command == "submit_to_audit":
            response = client.submit_to_audit(args.ticket_id, commit_hash=args.commit_hash)
        elif command == "submit_to_audit_without_commit":
            response = client.submit_to_audit_without_commit(args.ticket_id, reason=args.reason)
        elif command == "submit_to_inspection":
            response = client.submit_to_inspection(args.ticket_id, commit_hash=args.commit_hash)
        elif command == "implementer_kick_back":
            response = client.implementer_kick_back(args.ticket_id, reason=args.reason)
        elif command == "request_commit_exempt":
            response = client.request_commit_exempt(args.ticket_id, reason=args.reason)
        elif command == "start_task":
            response = client.start_task(args.ticket_id, text=args.text)
        elif command == "complete_task":
            response = client.complete_task(args.ticket_id, text=args.text)
        elif command == "recover_stalled_ticket":
            response = client.recover_stalled_ticket(args.ticket_id, reason=args.reason)
        elif command == "release_external_blocker":
            response = client.release_external_blocker(
                args.ticket_id, ref=args.ref, reason=args.reason, commit=args.commit
            )
        elif command == "request_dependency":
            response = client.request_dependency(
                args.ticket_id, role=args.role, reason=args.reason
            )
        elif command == "await_role":
            response = client.await_role(args.ticket_id, role=args.role)
        elif command == "clear_awaiting_role":
            response = client.clear_awaiting_role(args.ticket_id)
        elif command == "audit_sign_off":
            response = client.audit_sign_off(args.ticket_id, text=args.text)
        elif command == "audit_kick_back":
            response = client.audit_kick_back(args.ticket_id, reason=args.reason, target_assignee=args.target_assignee)
        elif command == "director_dat_sign_off":
            response = client.director_dat_sign_off(args.ticket_id, text=args.text)
        elif command == "director_dat_kick_back":
            response = client.director_dat_kick_back(args.ticket_id, reason=args.reason, target_assignee=args.target_assignee)
        elif command in {"user_reopen", "cancel"}:
            response = getattr(client, command)(args.ticket_id, reason=args.reason)
        elif command == "inspector_kick_back":
            response = client.inspector_kick_back(
                args.ticket_id,
                recommendations=args.recommendations,
                target_assignee=args.target_assignee,
            )
        elif command == "mark_done":
            response = client.mark_done(args.ticket_id, commit_hash=args.commit_hash)
        elif command == "set_manually_controlled":
            response = client.set_manually_controlled(args.ticket_id, args.value)
        elif command == "set_blockers":
            response = client.set_blockers(args.ticket_id, blocked_by=args.blocked_by, blocked_reason=args.blocked_reason)
        elif command == "add_comment":
            response = client.add_comment(args.ticket_id, text=args.text, urgent=args.urgent)
        elif command == "edit_fields":
            patch = json.loads(args.json)
            if not isinstance(patch, dict):
                raise TicketBoardWriteError("edit-fields --json must be a JSON object")
            response = client.edit_fields(args.ticket_id, patch)
        elif command == "merge":
            response = client.merge(args.ticket_id, target_id=args.target_id)
        elif command == "dismiss_notification":
            response = client.dismiss_notification(
                args.notification_id,
                ticket_id=args.ticket_id,
                target_role=args.target_role,
                kind=args.kind,
                reason=args.reason,
            )
        else:
            response = getattr(client, command)(args.ticket_id)
    except json.JSONDecodeError as exc:
        print(f"invalid edit-fields --json: {exc}", file=sys.stderr)
        return 1
    except TicketBoardWriteError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if command in {"merge", "dismiss_notification", "verify_caller"}:
        print(json.dumps(response))
    else:
        print(json.dumps(_ticket_from_response(response)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
