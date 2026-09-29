#!/usr/bin/env python3
"""SYRD-502: trusted commit-cache reads and refresh, against temporary repositories only."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import app as app_module  # noqa: E402
from scripts.ticket_board import commit_cache  # noqa: E402
from scripts.ticket_board.app import TicketBoardApp  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}
MOVED = ("_readable_commit_repos", "_published_ref_absence", "_cache_ref_commit", "_refresh_commit_repos", "_commit_repo_git_args")
REAL_RUN = subprocess.run


def git(*args: str, cwd: Path | None = None) -> str:
    return REAL_RUN(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env={**os.environ, **GIT_ENV}).stdout.strip()


class Recorder:
    """Records every subprocess.run the cache makes, optionally failing fetches."""

    def __init__(self, fetch: str = "real") -> None:
        self.calls: list[tuple[list[str], Any]] = []
        self.fetch = fetch

    def __call__(self, argv: list[str], *args: Any, **kwargs: Any) -> Any:
        self.calls.append((list(argv), kwargs.get("timeout")))
        if "fetch" in argv and self.fetch == "hang":
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        if "fetch" in argv and self.fetch == "fail":
            return subprocess.CompletedProcess(argv, 1, "", "unreachable")
        return REAL_RUN(argv, *args, **kwargs)

    def __enter__(self) -> Recorder:
        subprocess.run = self
        return self

    def __exit__(self, *_args: Any) -> None:
        subprocess.run = REAL_RUN


class Repos:
    def __init__(self, root: Path) -> None:
        os.environ.update(GIT_ENV)
        work = root / "work"
        work.mkdir()
        git("init", "-q", "-b", "main", str(work))
        (work / "f").write_text("a")
        git("add", "f", cwd=work)
        git("commit", "-qm", "a", cwd=work)
        self.a = git("rev-parse", "HEAD", cwd=work)
        self.remote = root / "remote.git"
        git("clone", "-q", "--bare", str(work), str(self.remote))
        git("push", "-q", str(self.remote), "main:refs/heads/roles/main/pub", cwd=work)
        self.cache = root / "cache.git"
        git("clone", "-q", "--bare", str(self.remote), str(self.cache))
        git("--git-dir", str(self.cache), "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        git("--git-dir", str(self.cache), "fetch", "-q", "origin")
        git("--git-dir", str(self.cache), "update-ref", "refs/heads/roles/main/claimed", self.a)
        (work / "f").write_text("b")
        git("commit", "-qam", "b", cwd=work)
        self.b = git("rev-parse", "HEAD", cwd=work)
        git("push", "-q", str(self.remote), "main:refs/heads/feature/b", cwd=work)
        self.checkout = root / "checkout"
        git("clone", "-q", str(self.remote), str(self.checkout))
        self.missing = root / "missing.git"
        self.missing2 = root / "missing2.git"


def refusal(operation: Any) -> str:
    try:
        operation()
    except ValueError as exc:
        return str(exc)
    raise AssertionError("expected a refusal")


def bare_app(dirs: tuple[Path, ...]) -> TicketBoardApp:
    app = TicketBoardApp.__new__(TicketBoardApp)
    app.commit_git_dirs = dirs
    return app


class Row:
    def __init__(self, row: Any) -> None:
        self.row = row

    def execute(self, sql: str, _params: tuple[Any, ...] = ()) -> Any:
        assert "FROM ticket_board.publication_requests WHERE id" in sql, sql
        return self

    def fetchone(self) -> Any:
        return self.row


def request(ref: str, commit: str, state: str = "requested") -> Row:
    return Row({"ref": ref, "commit_hash": commit, "state": state})


def test_surface_keeps_two_app_methods_and_moves_the_rest() -> None:
    for name in ("PUBLISHED_REF_NAMESPACE", "COMMIT_REFRESH_TIMEOUT_SECONDS", "PUBLISHABLE_REF"):
        assert getattr(app_module, name) is getattr(commit_cache, name), name
    assert app_module.COMMIT_REFRESH_TIMEOUT_SECONDS == 30
    for name in MOVED:
        assert not hasattr(TicketBoardApp, name), name
    assert callable(TicketBoardApp.published_ref_commit) and callable(TicketBoardApp._resolve_known_commit)
    source = Path(commit_cache.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert imported == {"__future__", "re", "subprocess", "pathlib"}, imported
    assert "://" not in source and "git@" not in source


def test_per_instance_stubs_reach_validation_and_proof_without_git() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd502-stub.") as tmp:
        app = bare_app((Path(tmp) / "absent.git",))
        commit = "4f9fd175593d5e804cadad77549b649a34c7d611"
        app._resolve_known_commit = lambda value: (commit if commit.startswith(value) else "", [])
        app.published_ref_commit = lambda ref: commit if ref == "roles/main/x" else ""
        with Recorder(fetch="fail") as rec:
            assert app._validate_commit_hash(commit[:9]) == commit
            assert app._prove_publication(request("roles/main/x", commit.upper()), 1) == commit
        assert rec.calls == [], rec.calls


def test_ref_reads_refuse_unsafe_names_before_git() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd502-ref.") as tmp:
        repos = Repos(Path(tmp).resolve())
        dirs = (repos.cache,)
        with Recorder() as rec:
            for bad in ("", " ", "../x", "roles/../x", "-roles/main/pub", "roles//x", "roles/main/pub^{tree}", "a b", "refs/heads/x.."):
                assert commit_cache.cache_ref_commit(dirs, bad) == "", bad
            assert rec.calls == [], rec.calls
            assert commit_cache.published_ref_commit(dirs, "roles/main/pub") == repos.a
            assert commit_cache.published_ref_commit(dirs, "roles/main/claimed") == ""
        assert rec.calls[0][0] == [
            "git", f"--git-dir={repos.cache}", "rev-parse", "--verify", "--quiet", "--end-of-options",
            "refs/remotes/origin/roles/main/pub^{commit}",
        ], rec.calls
        assert commit_cache.commit_repo_git_args(repos.checkout) == ["git", "-C", str(repos.checkout)]
        assert commit_cache.commit_repo_git_args(repos.cache) == ["git", f"--git-dir={repos.cache}"]
        assert refusal(lambda: commit_cache.commit_repo_git_args(repos.missing)) == (
            f"commit_hash verification repository not found: {repos.missing}"
        )
        assert commit_cache.readable_commit_repos((repos.missing, repos.cache))
        assert not commit_cache.readable_commit_repos((repos.missing, repos.missing2))


def test_refresh_is_bounded_origin_only_and_skips_failures() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd502-refresh.") as tmp:
        repos = Repos(Path(tmp).resolve())
        dirs = (repos.missing, repos.cache)
        assert commit_cache.resolve_known_commit(dirs, repos.b) == ("", [repos.missing])
        for mode in ("hang", "fail"):
            with Recorder(fetch=mode) as rec:
                assert commit_cache.refresh_commit_repos(dirs) is False
            assert rec.calls == [(["git", f"--git-dir={repos.cache}", "fetch", "--quiet", "--prune", "origin"], 30)], rec.calls
        with Recorder() as rec:
            assert commit_cache.refresh_commit_repos(dirs) is True
        assert [call[1] for call in rec.calls] == [30]
        assert commit_cache.resolve_known_commit(dirs, repos.b[:10].upper()) == (repos.b, [repos.missing])
        assert commit_cache.refresh_commit_repos((repos.missing,)) is False


def test_validate_refreshes_once_and_keeps_its_text() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd502-validate.") as tmp:
        repos = Repos(Path(tmp).resolve())
        app = bare_app((repos.cache,))
        assert app._validate_commit_hash(None) == "" and app._validate_commit_hash("  ") == ""
        assert refusal(lambda: app._validate_commit_hash(5)) == "commit_hash must be a string"
        assert refusal(lambda: app._validate_commit_hash("g" * 12)) == "commit_hash must be a 7-40 character hex commit"
        assert app._validate_commit_hash(" " + repos.a[:7].upper() + " ") == repos.a
        with Recorder(fetch="hang") as rec:
            unknown = refusal(lambda: app._validate_commit_hash(repos.b))
        assert unknown == (
            f"unknown commit_hash: {repos.b}. It is not in this board's copy of the project repository, even after "
            "refreshing it -- push the commit to the project remote and submit it again."
        )
        assert sum("fetch" in argv for argv, _ in rec.calls) == 1
        with Recorder() as rec:
            assert app._validate_commit_hash(repos.b[:10]) == repos.b
        assert sum("fetch" in argv for argv, _ in rec.calls) == 1
        app.commit_git_dirs = (repos.missing, repos.missing2)
        assert refusal(lambda: app._validate_commit_hash(repos.a)) == (
            f"commit_hash verification repository not found: {repos.missing}, {repos.missing2}"
        )
        app.commit_git_dirs = (repos.missing, repos.checkout)
        assert app._validate_commit_hash(repos.a[:9]) == repos.a
        assert refusal(lambda: app._validate_commit_hash("e" * 40)).startswith("unknown commit_hash: " + "e" * 40)


def test_proof_texts_and_runtime_reassignment() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd502-proof.") as tmp:
        repos = Repos(Path(tmp).resolve())
        app = bare_app((repos.missing, repos.cache))
        with Recorder(fetch="fail") as rec:
            assert app._prove_publication(request("roles/main/pub", repos.a.upper()), 1) == repos.a
            assert app._prove_publication(request("roles/main/pub", repos.b, "published"), 1) == ""
            assert app._prove_publication(Row(None), 1) == ""
            assert refusal(lambda: app._prove_publication(request("roles/main/pub", repos.b), 1)) == (
                f"roles/main/pub is published at {repos.a[:12]}, not the requested {repos.b[:12]}. Nothing was "
                "recorded and the request is still open: publish the commit that was asked for, or reject the "
                "request with a reason."
            )
            assert refusal(lambda: app._prove_publication(request("roles/main/claimed", repos.a), 1)) == (
                "roles/main/claimed is not published: the trusted commit cache has no "
                "refs/remotes/origin/roles/main/claimed. Its local refs/heads/roles/main/claimed is at "
                f"{repos.a[:12]}, but that branch is what filing the request creates, not evidence that anything "
                "reached the remote. Nothing was recorded and the request is still open -- publish the ref and "
                "record the outcome again, or reject the request with a reason."
            )
            assert refusal(lambda: app._prove_publication(request("roles/main/claimed", repos.b), 1)) == (
                "roles/main/claimed is not published: the trusted commit cache has no "
                "refs/remotes/origin/roles/main/claimed. Nothing was recorded and the request is still open -- "
                "publish the ref and record the outcome again, or reject the request with a reason."
            )
            assert refusal(lambda: app._prove_publication(request("roles/main/absent", repos.a), 1)).startswith(
                "roles/main/absent is not published: the trusted commit cache has no refs/remotes/origin/roles/main/absent. Nothing"
            )
            app.commit_git_dirs = (repos.missing, repos.missing2)
            assert refusal(lambda: app._prove_publication(request("roles/main/pub", repos.a), 1)) == (
                f"roles/main/pub cannot be proven published: none of this board's commit repositories ({repos.missing}, "
                f"{repos.missing2}) can be read, so it has no trusted copy of anything. Nothing was recorded and the "
                "request is still open."
            )
            app.commit_git_dirs = (repos.cache,)
            assert app.published_ref_commit("roles/main/pub") == repos.a
        assert not any("fetch" in argv for argv, _ in rec.calls), rec.calls


def main() -> int:
    count = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            count += 1
    print(f"ticket_board_commit_cache_test: {count} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
