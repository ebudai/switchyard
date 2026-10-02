"""HTTP write client for the ticket-board action API."""

from __future__ import annotations

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
        assignee: str | None = None,
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
        # Only an assignee the caller chose is sent: an omitted one lets the
        # board place the ticket, an explicit one is a request (SYRD-521).
        if assignee is not None:
            payload["assignee"] = assignee
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

    def pinned_to_resolved_endpoint(self) -> "TicketBoardWriteClient":
        """This client, fixed to the endpoint it resolves to now (SYRD-535).

        The ambient socket is re-resolved on every call -- it is chosen only if
        its file exists -- so a board that went away between a read and a write
        turned the write into an HTTP write to whatever board the URL names. A
        workflow change resolves once: an explicit socket, which `_post` never
        falls back from, or no socket at all.
        """
        socket_path = self.effective_socket_path
        return TicketBoardWriteClient(
            board_url=self.board_url,
            caller_role=self.caller_role,
            timeout=self.timeout,
            socket_path=socket_path,
            report_token=self.report_token,
            write_token=self.write_token,
            report_board_url=self.report_board_url,
            report_token_file=self.report_token_file,
            socket_disabled=socket_path is None,
        )

    @property
    def workflow_endpoint(self) -> str:
        """The one board a workflow change reads from and writes to (SYRD-535).

        Where writes resolve to a Unix socket, so does the read: the preview and
        the apply must address the same board, and a socket that cannot be read
        is a refusal, never a quiet fall back to whatever HTTP board the URL
        names.
        """
        socket_path = self.effective_socket_path
        return f"unix:{socket_path}" if socket_path else self.board_url.rstrip("/")

    def read_workflow(self) -> dict[str, Any]:
        """The board's declared workflow and its revision, from `workflow_endpoint` (SYRD-535)."""
        socket_path = self.effective_socket_path
        if socket_path:
            conn = UnixHTTPConnection(socket_path, self.timeout)
            try:
                conn.request("GET", "/api/workflow")
                response = conn.getresponse()
                body = response.read().decode("utf-8", errors="replace")
                if response.status >= 400:
                    raise TicketBoardWriteError(f"the board at {socket_path} refused the workflow read: {body or response.status}")
                parsed = json.loads(body)
            except (OSError, ValueError) as exc:
                raise TicketBoardWriteError(
                    f"could not read the workflow from {socket_path} ({exc}); not falling back to another board"
                ) from exc
            finally:
                conn.close()
        else:
            root = self.board_url.rstrip("/")
            if root.endswith("/api/tickets"):
                root = root[: -len("/api/tickets")]
            try:
                with request.urlopen(f"{root}/api/workflow", timeout=self.timeout) as response:
                    parsed = json.loads(response.read().decode("utf-8"))
            except (OSError, ValueError) as exc:
                raise TicketBoardWriteError(f"could not read {root}/api/workflow: {exc}") from exc
        if not isinstance(parsed, dict):
            raise TicketBoardWriteError("the board's workflow response was not an object")
        return parsed

    def configure_workflow(
        self, document: dict[str, Any], *, expected_revision: int, dry_run: bool = False, same_endpoint: bool = False
    ) -> dict[str, Any]:
        payload = {"document": document, "expected_revision": expected_revision, "dry_run": dry_run}
        socket_path = self.effective_socket_path
        if same_endpoint and socket_path:
            # The board `read_workflow` read, and no other: `_post` would fall
            # back to TCP if the socket failed, and the URL may name another
            # board (SYRD-535).
            role = self.caller_role.strip().lower()
            if not role:
                raise ValueError("caller_role must be non-empty")
            _refuse_production_write_under_test(self.board_url, socket_path)
            try:
                return self._post_unix("/actions/configure_workflow", payload, role, socket_path)
            except OSError as exc:
                raise TicketBoardWriteError(
                    f"could not reach {socket_path} ({exc}); not falling back to another board"
                ) from exc
        return self._post("/actions/configure_workflow", payload)

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

    def snooze_reminders(
        self, tickets: list[str], *, until: str, reason: str, apply: bool = False, caller_role: str | None = None
    ) -> dict[str, Any]:
        """Defer the named tickets' optional reminders until `until` (SYRD-537). A preview unless `apply`."""
        payload = {"tickets": list(tickets), "until": until, "reason": reason, "apply": apply}
        return self._post("/actions/snooze_reminders", payload, caller_role=caller_role)

    def clear_reminder_snooze(
        self, batch: int, *, tickets: list[str] | None = None, reason: str, apply: bool = False,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        """End a snooze early, for the named tickets or the whole batch. A preview unless `apply`."""
        payload = {"batch": batch, "tickets": list(tickets or []), "reason": reason, "apply": apply}
        return self._post("/actions/clear_reminder_snooze", payload, caller_role=caller_role)


def main(argv: list[str] | None = None) -> int:
    """The `ticket-board-write` command, which `write_cli` owns (SYRD-515)."""
    try:
        from . import write_cli
    except ImportError:  # pragma: no cover - `python write_client.py` runs outside the package
        import write_cli
    return write_cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
