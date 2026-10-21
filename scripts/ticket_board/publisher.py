"""The Publisher: publish one approved candidate to the integration branch, mechanically (SYRD-625).

No model, provider, pane, session or prompt is involved, and no source is
edited. The board's notify listener runs this (`publisher_pass`) for each
ticket in the workflow's `publication` stage (`publisher_stage`), as the
project account -- the account every role already publishes with -- and the
board's database decides what may be recorded (migration pgu1004).

For one ticket the Publisher:

1. begins, or resumes, the ticket's attempt in the board's journal
   (`publisher_begin`), which also says which review sign-offs the ticket
   still owes and what size review says about its candidate;
2. finds its authority: the board's single commit repository is the trusted
   cache, that cache's `origin` is the public repository (its push URL, when
   one is configured, is where the push goes), and the integration branch is
   the one the public repository's HEAD names;
3. checks the trusted cache: the candidate resolves there to the exact
   recorded commit, and the cache's copy of the public branch is current;
4. reads the public branch's tip. A candidate already on it was published
   earlier -- by a run that stopped before it could record so, say -- and is
   recorded without pushing again. Otherwise the candidate must have exactly
   one parent, and that parent must be the tip;
5. builds a bundle of exactly that one commit from the trusted cache, checks
   its one head and one prerequisite, and journals its sha256;
6. publishes from a fresh repository holding nothing but the public branch and
   that bundle: a compare-and-swap push leased on the tip it checked
   (`--force-with-lease=refs/heads/<branch>:<tip>`). The candidate's only
   parent is that tip, so the update is a fast-forward: the lease is what makes
   it fail, rather than land, if the branch moved in between;
7. reads the public branch back, refreshes the trusted cache's copy of it,
   checks both carry the candidate, and records the evidence and the move on
   (`publisher_finish`), in one transaction.

Every step is journalled before the next one starts (`publisher_step`), so a
run that stops anywhere is resumed from facts by the next pass.

What it repairs, and nothing else (each repair is recorded in the evidence):
a stale trusted-cache branch; a candidate missing from the cache, fetched by
its SHA from the cache's own origin; a missing or changed bundle or scratch
repository, rebuilt; an attempt that no longer matches its ticket, superseded
by the board; and a transient remote read or push, retried at most
`TRANSIENT_TRIES` times. None of them can change the candidate's SHA, tree or
parent, which sign-offs it has, or where it is published. It never rebases,
merges, edits a file, picks between candidates, force-pushes, or waives a gate.

What it hands back, once:

* `returned` to the implementer -- the candidate or its lineage has to change:
  the public branch moved past its parent, its history differs, it is not
  exactly one commit on the tip, or it is published nowhere;
* `escalated` to the Director -- something only the Director can fix: the
  publication target, a credential, a policy or hook refusal, sign-offs or size
  review not satisfied, the journal, a git helper failure, transient failures
  that outlasted the retries, or a publication the trusted cache cannot be made
  to show.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .commit_repos import explicit_commit_git_dirs

SHA = re.compile(r"^[0-9a-f]{40}$")
#: A branch name the Publisher will push to: one or more ordinary path segments.
BRANCH = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
GIT_TIMEOUT_SECONDS = 180
#: How many times a remote read, fetch or push is tried before it is the Director's.
TRANSIENT_TRIES = 3
RETRY_DELAYS = (2.0, 5.0, 10.0)
#: How many times the public branch is read back after a push before it is the Director's.
PUBLIC_READ_TRIES = 5
STEPS = ("started", "prepared", "pushing", "pushed", "verified")
#: Where a candidate is held in the Publisher's own repositories.
CANDIDATE_REF = "refs/publisher/candidate"
PUBLIC_REF = "refs/publisher/public"

TRANSIENT = ("could not resolve host", "connection timed out", "connection reset", "connection refused",
             "connection closed", "the remote end hung up", "early eof", "temporary failure", "timed out after",
             "operation timed out", "rpc failed", "unable to access", "network is unreachable",
             "kex_exchange_identification", "http 500", "http 502", "http 503", "http 504",
             "returned error: 50", "service unavailable", "internal server error", "broken pipe")
CREDENTIAL = ("permission denied", "authentication failed", "could not read username", "could not read password",
              "invalid username or password", "returned error: 401", "returned error: 403", "error: 403",
              "host key verification failed", "terminal prompts disabled", "denied to")
POLICY = ("remote rejected", "hook declined", "pre-receive hook", "update hook", "protected branch", "gh006",
          "gh013", "refusing to update", "push declined", "not allowed to push")
RACE = ("stale info", "non-fast-forward", "fetch first", "[rejected]", "tip of your current branch is behind")


class Superseded(Exception):
    """The board no longer considers this attempt its ticket's: stop, record nothing."""


class Handoff(Exception):
    """The attempt ends by handing the ticket on: `outcome` is `returned` or `escalated`."""

    def __init__(self, outcome: str, kind: str, text: str):
        super().__init__(text)
        self.outcome, self.kind, self.text = outcome, kind, text


def back(kind: str, text: str) -> Handoff:
    return Handoff("returned", kind, text)


def director(kind: str, text: str) -> Handoff:
    return Handoff("escalated", kind, text)


def short(sha: str) -> str:
    return (sha or "")[:12] or "nothing"


def classify(text: str) -> str:
    """What a git failure's own words say it was: race, policy, credential, transient, or helper."""
    lowered = text.lower()
    for kind, words in (("policy", POLICY), ("race", RACE), ("credential", CREDENTIAL), ("transient", TRANSIENT)):
        if any(word in lowered for word in words):
            return kind
    return "helper"


def git_said(result: subprocess.CompletedProcess[str]) -> str:
    """Git's own words for a failure, verbatim: its non-progress lines, joined, bounded."""
    lines = [line.strip() for line in f"{result.stderr}\n{result.stdout}".splitlines()
             if line.strip() and not line.strip().startswith(("remote: Counting", "remote: Compressing", "To "))]
    text = " / ".join(lines) if lines else f"git exited {result.returncode} with no output"
    return text if len(text) <= 600 else text[:600] + " ..."


def git_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Git with nothing of the account's configuration but its SSH identity, and never a prompt.

    Global and system configuration are left out, so no `insteadOf`, hook path or
    credential helper of a role's can redirect or change what is pushed; the SSH
    client still reads the account's own identity, which is the credential every
    role publishes with. Inherited GIT_* variables are dropped for the same reason.
    """
    env = {key: value for key, value in (base if base is not None else os.environ).items()
           if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_REPLACE_OBJECTS": "1", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"})
    return env


@dataclass
class Git:
    """Runs git; never raises, and a timeout is an ordinary failed result."""

    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    env: dict[str, str] = field(default_factory=git_environment)
    timeout: float = GIT_TIMEOUT_SECONDS

    def __call__(self, *args: str, repo: Path | None = None) -> subprocess.CompletedProcess[str]:
        argv = ["git", *(["--git-dir", str(repo)] if repo is not None else []), *args]
        try:
            return self.runner(argv, text=True, capture_output=True, env=self.env, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, 124, "", f"git {args[0]} timed out after {self.timeout:.0f}s")
        except OSError as exc:
            return subprocess.CompletedProcess(argv, 127, "", f"git could not run: {exc}")


def board_commit_repositories(environ: dict[str, str] | None = None) -> list[str]:
    """The repositories the board verifies commits against: the unit's explicit list, else the board's own answer."""
    environ = dict(os.environ if environ is None else environ)
    explicit = [str(path) for path in explicit_commit_git_dirs(environ)]
    if explicit:
        return explicit
    board_url = environ.get("TICKET_BOARD_URL", "").strip()
    if not board_url:
        raise director("authority", "the Publisher cannot tell which repository the board verifies commits against: "
                                    "neither TICKET_BOARD_COMMIT_GIT_DIR nor TICKET_BOARD_URL is set for the listener")
    try:
        with urllib.request.urlopen(board_url.rstrip("/") + "/api/client-config", timeout=10) as response:
            config = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise director("transient", f"the board at {board_url} could not be asked where it verifies commits ({exc})")
    repositories = config.get("commit_repositories") if isinstance(config, dict) else None
    if not isinstance(repositories, list) or not all(isinstance(item, str) for item in repositories):
        raise director("authority", f"the board at {board_url} does not say where it verifies commits")
    return [item for item in repositories if item.strip()]


def _value(row: Any) -> Any:
    """A one-column row's value, as text or JSON: the listener's connection hands text back as bytes."""
    value = next(iter(row.values())) if isinstance(row, dict) else (row[0] if row else None)
    return value.decode("utf-8") if isinstance(value, (bytes, bytearray, memoryview)) else value


class Journal:
    """The board's side of one attempt: the four listener functions, each its own transaction."""

    def __init__(self, conn: Any):
        self.conn = conn

    def _one(self, statement: str, args: Sequence[Any]) -> Any:
        value = _value(self.conn.execute(statement, args).fetchone())
        return json.loads(value) if isinstance(value, str) else value

    def pending(self) -> list[str]:
        return [_value(row) for row in self.conn.execute("SELECT * FROM ticket_board.publisher_pending()").fetchall()]

    def begin(self, ticket: str) -> dict[str, Any]:
        return self._one("SELECT ticket_board.publisher_begin(%s)", (ticket,))

    def step(self, attempt: int, step: str, expected: str, facts: dict[str, Any], repairs: list[str]) -> None:
        try:
            self.conn.execute("SELECT ticket_board.publisher_step(%s, %s, %s, %s::jsonb, %s::jsonb)",
                              (attempt, step, expected, json.dumps(facts), json.dumps(repairs)))
        except Exception as exc:  # the board's refusal is an answer; anything else is the journal failing
            if "superseded" in str(exc):
                raise Superseded(str(exc).splitlines()[0]) from None
            raise director("journal", f"the board refused to journal step {step}: "
                                      f"{str(exc).splitlines()[0] if str(exc) else type(exc).__name__}") from None

    def finish(self, attempt: int, outcome: str, text: str, facts: dict[str, Any], repairs: list[str]) -> dict:
        return self._one("SELECT ticket_board.publisher_finish(%s, %s, %s, %s::jsonb, %s::jsonb)",
                         (attempt, outcome, text, json.dumps(facts), json.dumps(repairs)))


@dataclass
class Attempt:
    """One run over one ticket: what the board said, what was learnt, what was repaired."""

    journal: Journal
    git: Git
    root: Path
    info: dict[str, Any]
    repositories: Callable[[], list[str]]
    sleep: Callable[[float], None] = time.sleep
    facts: dict[str, Any] = field(default_factory=dict)
    repairs: list[str] = field(default_factory=list)
    pushed: bool = False
    #: The public branch was read back carrying the candidate, by this run.
    confirmed: bool = False

    def __post_init__(self) -> None:
        self.facts = dict(self.info.get("facts") or {})
        self.repairs = list(self.info.get("repairs") or [])
        self.step_at = self.info.get("step") or "started"
        if self.info.get("resumed"):
            self.repaired(f"resumed attempt {self.info['attempt']} from journal step {self.step_at}")

    @property
    def ticket(self) -> str:
        return str(self.info["ticket"])

    def repaired(self, what: str) -> None:
        if what not in self.repairs:
            self.repairs.append(what)

    def journal_step(self, step: str, expected: str = "") -> None:
        """Journal `step` -- or, past it already, only what was learnt -- before going on."""
        if STEPS.index(step) > STEPS.index(self.step_at):
            self.step_at = step
        self.journal.step(int(self.info["attempt"]), self.step_at, expected, self.facts, self.repairs)

    # -- git with bounded retries ------------------------------------------------------------------------------

    def remote(self, what: str, *args: str, repo: Path | None = None) -> subprocess.CompletedProcess[str]:
        """A git command that talks to a remote: transient failures are retried, at most TRANSIENT_TRIES times."""
        result = self.git(*args, repo=repo)
        for attempt in range(1, TRANSIENT_TRIES):
            if result.returncode == 0 or classify(result.stderr + result.stdout) != "transient":
                break
            self.repaired(f"retried {what} after a transient failure ({git_said(result)})")
            self.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
            result = self.git(*args, repo=repo)
        return result

    def local(self, what: str, *args: str, repo: Path | None = None) -> str:
        """A local git command that must succeed; a failure is a helper failure, the Director's."""
        result = self.git(*args, repo=repo)
        if result.returncode != 0:
            raise director("helper", f"{what} failed: {git_said(result)}")
        return result.stdout.strip()

    def remote_failure(self, what: str, result: subprocess.CompletedProcess[str]) -> Handoff:
        kind = classify(result.stderr + result.stdout)
        if kind == "transient":
            return director("transient", f"{what} kept failing after {TRANSIENT_TRIES} tries: {git_said(result)}")
        return director(kind if kind in ("credential", "policy") else "helper", f"{what} failed: {git_said(result)}")

    # -- the steps ---------------------------------------------------------------------------------------------

    def authority(self) -> None:
        """The trusted cache, the public repository and branch, and the push target -- or the Director's."""
        repositories = self.repositories()
        if len(repositories) != 1:
            raise director("authority", f"the board verifies commits against {len(repositories)} repositories "
                                        f"({', '.join(repositories) or 'none'}); the Publisher publishes to one")
        cache = Path(repositories[0])
        if not (cache / "objects").is_dir() and not (cache / ".git").exists():
            raise director("authority", f"the board's trusted commit cache {cache} is not a repository on this host")
        cache = cache / ".git" if (cache / ".git").is_dir() else cache
        public = self.git("config", "--get", "remote.origin.url", repo=cache).stdout.strip()
        pushurl = self.git("config", "--get", "remote.origin.pushurl", repo=cache).stdout.strip()
        for name, url in (("origin", public), ("push", pushurl or public)):
            if not url or url.startswith("-") or url.startswith("ext::") or "\n" in url:
                key = "remote.origin.url" if name == "origin" else "remote.origin.pushurl"
                raise director("authority", f"the trusted cache {cache} names no usable {name} URL ({url or 'none'}); "
                                            f"set it with `git --git-dir {cache} config {key} <repository URL>`")
        head = self.remote("reading the public repository's HEAD", "ls-remote", "--symref", public, "HEAD")
        if head.returncode != 0:
            raise self.remote_failure(f"reading {public}", head)
        named = re.search(r"^ref: refs/heads/(\S+)\tHEAD$", head.stdout, re.MULTILINE)
        tip = re.search(r"^([0-9a-f]{40})\tHEAD$", head.stdout, re.MULTILINE)
        if named is None or tip is None or not BRANCH.fullmatch(named.group(1)) or ".." in named.group(1):
            raise director("authority", f"{public} does not name an integration branch with its HEAD "
                                        f"({head.stdout.strip() or 'empty answer'})")
        self.facts.update({"commit_repository": str(cache), "public_url": public, "push_url": pushurl or public,
                           "branch": named.group(1), "public_before": tip.group(1)})

    @property
    def cache(self) -> Path:
        return Path(self.facts["commit_repository"])

    def candidate(self) -> None:
        """The exact recorded commit, as the trusted cache resolves it; fetched there by SHA if it is missing."""
        recorded = str(self.info.get("candidate") or "").lower()
        found = self.git("rev-parse", "--verify", "--quiet", f"{recorded}^{{commit}}", repo=self.cache)
        if found.returncode != 0 and SHA.fullmatch(recorded):
            fetched = self.remote("fetching the candidate into the trusted cache",
                                  "fetch", "--no-tags", "--no-write-fetch-head", "origin", recorded, repo=self.cache)
            if fetched.returncode == 0:
                self.repaired(f"fetched candidate {short(recorded)} into the trusted cache from its origin")
                found = self.git("rev-parse", "--verify", "--quiet", f"{recorded}^{{commit}}", repo=self.cache)
            elif classify(fetched.stderr + fetched.stdout) in ("credential", "transient", "policy"):
                raise self.remote_failure("fetching the candidate into the trusted cache", fetched)
        full = found.stdout.strip().lower()
        if found.returncode != 0 or not SHA.fullmatch(full) or not full.startswith(recorded):
            raise back("candidate", f"candidate {recorded or '(none)'} is neither in the board's trusted commit cache "
                                    f"nor on {self.facts['public_url']}. Publish it (switchyard-publish-candidate) and "
                                    "resubmit")
        parents = self.local("reading the candidate's parents", "rev-list", "--parents", "-n", "1", full,
                             repo=self.cache).split()[1:]
        tree = self.local("reading the candidate's tree", "rev-parse", f"{full}^{{tree}}", repo=self.cache)
        self.facts.update({"candidate_full": full, "parents": parents, "tree": tree})

    def cache_current(self) -> None:
        """The trusted cache's copy of the public branch, brought up to the tip the public repository names."""
        branch, tip = self.facts["branch"], self.facts["public_before"]
        ref = f"refs/remotes/origin/{branch}"
        held = self.git("rev-parse", "--verify", "--quiet", ref, repo=self.cache).stdout.strip()
        if held == tip:
            return
        fetched = self.remote("refreshing the trusted cache", "fetch", "--no-tags", "origin",
                              f"+refs/heads/{branch}:{ref}", repo=self.cache)
        if fetched.returncode != 0:
            raise self.remote_failure("refreshing the trusted cache", fetched)
        self.repaired(f"refreshed the trusted cache's {branch} from {short(held)} to "
                      f"{short(self.git('rev-parse', ref, repo=self.cache).stdout.strip())}")

    def workspace(self) -> Path:
        """The Publisher's own repository for reading the public branch and the candidate; rebuilt when unusable."""
        work = self.root / "work.git"
        if work.exists() and self.git("rev-parse", "--git-dir", repo=work).returncode != 0:
            shutil.rmtree(work)
            self.repaired("rebuilt the Publisher's scratch repository, which git could not read")
        if not work.exists():
            self.local("creating the Publisher's scratch repository", "init", "--bare", "--quiet", "--template=",
                       str(work))
        return work

    def lineage(self, work: Path) -> None:
        """The public tip in `work`, with the candidate, and whether the candidate is one commit on that tip."""
        public, branch = self.facts["public_url"], self.facts["branch"]
        fetched = self.remote("fetching the public branch", "fetch", "--no-tags", "--quiet", public,
                              f"+refs/heads/{branch}:{PUBLIC_REF}", repo=work)
        if fetched.returncode != 0:
            raise self.remote_failure(f"fetching {branch} from {public}", fetched)
        tip = self.local("reading the public tip", "rev-parse", PUBLIC_REF, repo=work)
        self.facts["public_before"] = tip
        full = self.facts["candidate_full"]
        self.local("taking the candidate from the trusted cache", "fetch", "--no-tags", "--quiet",
                   "--no-write-fetch-head", str(self.cache), f"+{full}:{CANDIDATE_REF}", repo=work)

    def landed(self, work: Path, tip: str) -> bool:
        full = self.facts["candidate_full"]
        return tip == full or self.git("merge-base", "--is-ancestor", full, tip, repo=work).returncode == 0

    def require_one_commit_on(self, work: Path, tip: str) -> None:
        """Return the ticket unless the candidate is exactly one commit whose only parent is `tip`."""
        full, parents, branch = self.facts["candidate_full"], self.facts["parents"], self.facts["branch"]
        if parents == [tip]:
            return
        if len(parents) != 1:
            raise back("lineage", f"candidate {short(full)} has {len(parents)} parents. The Publisher publishes one "
                                  f"commit whose only parent is public {branch} ({short(tip)}), as a fast-forward, "
                                  "and never merges: rebuild it as one commit on that tip and resubmit")
        parent = parents[0]
        if self.git("merge-base", "--is-ancestor", parent, tip, repo=work).returncode == 0:
            count = self.local("counting the public commits", "rev-list", "--count", f"{parent}..{tip}", repo=work)
            ours = set(self.local("listing the candidate's paths", "diff", "--name-only", parent, full,
                                  repo=work).splitlines())
            theirs = set(self.local("listing the public paths", "diff", "--name-only", parent, tip,
                                    repo=work).splitlines())
            both = sorted(ours & theirs)
            raise back("lineage", f"public {branch} advanced: candidate {short(full)} was built on {short(parent)}, "
                                  f"and {branch} is now {short(tip)}, {count} commit(s) later. "
                                  + (f"Paths both changed: {', '.join(both[:20])}{', ...' if len(both) > 20 else ''}. "
                                     if both else "No path was changed by both. ")
                                  + f"The Publisher only fast-forwards, and never rebases or merges: rebuild the "
                                    f"candidate on {short(tip)} and resubmit; every review runs again")
        if self.git("merge-base", "--is-ancestor", tip, parent, repo=work).returncode == 0:
            count = self.local("counting the candidate's commits", "rev-list", "--count", f"{tip}..{full}", repo=work)
            raise back("lineage", f"candidate {short(full)} is {count} commits on public {branch} ({short(tip)}), "
                                  "not one. The Publisher publishes exactly one commit on the tip: squash it onto "
                                  f"{short(tip)} and resubmit")
        raise back("lineage", f"candidate {short(full)}'s history differs from public {branch}: its parent "
                              f"{short(parent)} is not on {branch} ({short(tip)}). Rebuild it on {short(tip)} and "
                              "resubmit")

    def bundle(self, work: Path, tip: str) -> Path:
        """The one-commit bundle the push is made from: reused when it is still exactly that, else rebuilt."""
        full = self.facts["candidate_full"]
        folder = self.root / "bundles"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / f"{self.ticket.lower()}-{full[:12]}-{tip[:12]}.bundle"
        recorded = self.facts.get("bundle_sha256")
        if recorded and path.is_file() and _sha256(path) == recorded and self.bundle_shape(path) == (full, tip):
            return path
        if recorded and self.facts.get("bundle") == str(path):
            self.repaired(f"rebuilt the bundle for {short(full)}, which was missing or changed")
        partial = path.with_suffix(".partial")
        partial.unlink(missing_ok=True)
        self.local("building the bundle", "bundle", "create", str(partial), CANDIDATE_REF, f"^{tip}", repo=work)
        if self.bundle_shape(partial) != (full, tip):
            raise director("helper", f"the bundle built for {short(full)} does not hold exactly that commit on "
                                     f"{short(tip)}")
        os.replace(partial, path)
        self.facts.update({"bundle": str(path), "bundle_sha256": _sha256(path)})
        return path

    @staticmethod
    def bundle_shape(path: Path) -> tuple[str, str] | None:
        """(head, prerequisite) when the bundle holds exactly one of each, else None."""
        try:
            with path.open("rb") as handle:
                header = handle.read(65536).split(b"\n\n", 1)[0].decode("ascii", errors="replace").splitlines()
        except OSError:
            return None
        if not header or not header[0].startswith("# v") or not header[0].endswith("git bundle"):
            return None
        lines = [line for line in header[1:] if not line.startswith("@")]
        prerequisites = [line[1:].split()[0] for line in lines if line.startswith("-")]
        heads = [line.split() for line in lines if not line.startswith("-")]
        if len(prerequisites) != 1 or len(heads) != 1 or heads[0][1:] != [CANDIDATE_REF]:
            return None
        return heads[0][0], prerequisites[0]

    def push(self, bundle: Path, tip: str) -> None:
        """Publish from a fresh repository holding the public tip and the bundle, leased on `tip`."""
        full, branch = self.facts["candidate_full"], self.facts["branch"]
        repo = self.root / f"push-{self.info['attempt']}.git"
        shutil.rmtree(repo, ignore_errors=True)
        try:
            self.local("creating the push repository", "init", "--bare", "--quiet", "--template=", str(repo))
            fetched = self.remote("fetching the public branch for the push", "fetch", "--no-tags", "--quiet",
                                  self.facts["public_url"], f"+refs/heads/{branch}:{PUBLIC_REF}", repo=repo)
            if fetched.returncode != 0:
                raise self.remote_failure(f"fetching {branch} for the push", fetched)
            moved = self.local("reading the public tip", "rev-parse", PUBLIC_REF, repo=repo)
            if moved != tip:
                self.facts["public_before"] = moved
                raise back("lineage", f"public {branch} moved from {short(tip)} to {short(moved)} while "
                                      f"{short(full)} was being prepared. The Publisher only fast-forwards: rebuild "
                                      f"the candidate on {short(moved)} and resubmit; every review runs again")
            self.local("verifying the bundle", "bundle", "verify", "--quiet", str(bundle), repo=repo)
            self.local("taking the candidate from the bundle", "fetch", "--no-tags", "--quiet", str(bundle),
                       f"{CANDIDATE_REF}:{CANDIDATE_REF}", repo=repo)
            line = self.local("reading the bundled commit", "rev-list", "--parents", "-n", "1", CANDIDATE_REF,
                              repo=repo).split()
            if line != [full, tip]:
                raise director("helper", f"the bundle unpacked as {' '.join(line) or 'nothing'}, not {short(full)} on "
                                         f"{short(tip)}; nothing was pushed")
            self.journal_step("pushing", tip)
            pushed = self.remote("the push", "push", "--porcelain", f"--force-with-lease=refs/heads/{branch}:{tip}",
                                 self.facts["push_url"], f"{full}:refs/heads/{branch}", repo=repo)
        finally:
            shutil.rmtree(repo, ignore_errors=True)
        if pushed.returncode == 0:
            self.pushed = True
            return
        if classify(pushed.stderr + pushed.stdout) == "race":
            return  # decided from what the public branch now says, by the read-back
        failure = self.remote_failure(f"git push to {self.facts['push_url']}", pushed)
        if failure.kind == "credential" and self.facts["push_url"] == self.facts["public_url"]:
            failure.text += (f". The trusted cache names no push URL, so the push went to its public URL; give it the "
                             f"one the project account's SSH identity pushes to: `git --git-dir {self.cache} config "
                             "remote.origin.pushurl <SSH repository URL>`")
        raise failure

    def read_back(self, work: Path) -> str:
        """The public tip after the push, read until it carries the candidate (or PUBLIC_READ_TRIES run out)."""
        public, branch, full = self.facts["public_url"], self.facts["branch"], self.facts["candidate_full"]
        tip = ""
        for attempt in range(PUBLIC_READ_TRIES):
            if attempt:
                self.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
            fetched = self.remote("reading the public branch back", "fetch", "--no-tags", "--quiet", public,
                                  f"+refs/heads/{branch}:{PUBLIC_REF}", repo=work)
            if fetched.returncode != 0:
                raise self.remote_failure(f"reading {branch} back from {public}", fetched)
            tip = self.local("reading the public tip", "rev-parse", PUBLIC_REF, repo=work)
            if self.landed(work, tip):
                self.confirmed = True
                return tip
            if not self.pushed:
                break
        if not self.pushed and tip != self.facts.get("public_before"):
            raise back("lineage", f"public {branch} moved to {short(tip)} before {short(full)} could be published "
                                  f"(the push was refused because {branch} was no longer {short(self.facts.get('public_before', ''))}). "
                                  f"The Publisher only fast-forwards: rebuild the candidate on {short(tip)} and "
                                  "resubmit; every review runs again")
        raise director("publication", f"{public} {branch} reads {short(tip)}, not {short(full)}, after "
                                      + ("a push git reported as successful" if self.pushed else
                                         "a refused push, though it still reads the tip the push was leased on"))

    def refresh_cache(self, tip: str) -> str:
        """The trusted cache's copy of the public branch, brought up to `tip`, and checked."""
        branch, full = self.facts["branch"], self.facts["candidate_full"]
        ref = f"refs/remotes/origin/{branch}"
        before = self.git("rev-parse", "--verify", "--quiet", ref, repo=self.cache).stdout.strip()
        fetched = self.remote("refreshing the trusted cache", "fetch", "--no-tags", "origin",
                              f"+refs/heads/{branch}:{ref}", repo=self.cache)
        after = self.git("rev-parse", "--verify", "--quiet", ref, repo=self.cache).stdout.strip()
        carries = after == full or self.git("merge-base", "--is-ancestor", full, after or full,
                                            repo=self.cache).returncode == 0 and bool(after)
        if fetched.returncode != 0 or not carries:
            why = git_said(fetched) if fetched.returncode != 0 else f"it reads {short(after)}"
            raise director("cache", f"{short(full)} is on public {branch} ({short(tip)}), but the board's trusted "
                                    f"cache {self.cache} could not be brought up to it: {why}")
        self.facts["cache_before"] = before
        return after

    def evidence(self, tip: str, cache_tip: str, already: bool) -> str:
        full, branch = self.facts["candidate_full"], self.facts["branch"]
        how = (f"It was already on public {branch} when this attempt read it, so nothing was pushed." if already else
               f"Pushed as a fast-forward from {short(self.facts['public_before'])}, compare-and-swap leased on that "
               f"tip, from bundle sha256 {self.facts.get('bundle_sha256', '')[:16]}.")
        return (f"Published {full} (tree {self.facts.get('tree', '')[:12]}, parent "
                f"{short((self.facts.get('parents') or [''])[0])}) to {branch} of {self.facts['public_url']}. {how} "
                f"Public {branch} reads {short(tip)} and the board's trusted cache reads {short(cache_tip)}; both "
                f"carry it. Attempt {self.info['attempt']}." + _repairs_sentence(self.repairs))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repairs_sentence(repairs: list[str]) -> str:
    return f" Mechanical repairs: {'; '.join(repairs)}." if repairs else ""


def handoff_text(attempt: Attempt, handoff: Handoff) -> str:
    """The comment that goes with a return or an escalation: what happened, what changed, what is next."""
    branch = attempt.facts.get("branch", "the integration branch")
    full = attempt.facts.get("candidate_full") or str(attempt.info.get("candidate") or "")
    state = (f" Public {branch} carries {short(full)}." if attempt.confirmed else
             f" The push reported success, but public {branch} does not carry {short(full)}." if attempt.pushed else
             " Nothing was pushed by this attempt.")
    if handoff.outcome == "returned":
        return (f"Publisher returned this ticket ({handoff.kind}): {handoff.text}.{state} Every sign-off is cleared, "
                f"so the new candidate goes through every gate again." + _repairs_sentence(attempt.repairs))
    return (f"Publisher could not publish this ticket ({handoff.kind}): {handoff.text}.{state}"
            + _repairs_sentence(attempt.repairs)
            + " Once it is fixed, publication_retry hands the ticket back to the Publisher; publication_kick_back "
              "returns it to the implementer instead.")


def publish_ticket(conn: Any, ticket: str, *, root: Path, git: Git | None = None,
                   repositories: Callable[[], list[str]] = board_commit_repositories,
                   sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """One attempt at one ticket, start to finish. Returns what the board recorded."""
    journal = Journal(conn)
    info = journal.begin(ticket)
    if not info.get("ready"):
        return {"ticket": ticket, "outcome": "not_ready"}
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    attempt = Attempt(journal, git or Git(), root, info, repositories, sleep)
    outcome, text = "published", ""
    try:
        text = run_attempt(attempt)
    except Superseded as exc:
        if not attempt.pushed:
            return {"ticket": ticket, "outcome": "superseded", "detail": str(exc)}
        # Published, but the ticket moved on first: the board keeps the evidence and moves nothing.
        text = (f"Published {attempt.facts['candidate_full']} to {attempt.facts['branch']} of "
                f"{attempt.facts['public_url']}, leased on {short(attempt.facts['public_before'])}."
                + _repairs_sentence(attempt.repairs))
    except Handoff as handoff:
        outcome, text = handoff.outcome, handoff_text(attempt, handoff)
    try:
        recorded = journal.finish(int(info["attempt"]), outcome, text, attempt.facts, attempt.repairs)
    except Exception as exc:  # the board refused what was decided: the Director's, with the board's own words
        if outcome == "escalated":
            raise
        refusal = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        text = handoff_text(attempt, director("journal", f"the board refused to record {outcome}: {refusal}"))
        outcome = "escalated"
        recorded = journal.finish(int(info["attempt"]), outcome, text, attempt.facts, attempt.repairs)
    return {"ticket": ticket, "outcome": outcome, "text": text, **(recorded or {})}


def run_attempt(attempt: Attempt) -> str:
    """The state machine. Returns the evidence of a publication; raises Handoff or Superseded otherwise."""
    info = attempt.info
    if not str(info.get("candidate") or "").strip():
        if info.get("commit_exempt"):
            if info.get("missing_signoffs"):
                raise director("gate", f"missing sign-off {', '.join(info['missing_signoffs'])}; the Publisher "
                                       "does not waive a gate")
            return "No commit to publish: this ticket was submitted without code, so the integration branch is unchanged."
        raise back("candidate", "no candidate commit is recorded on this ticket, so there is nothing to publish")
    if info.get("missing_signoffs"):
        raise director("gate", f"missing sign-off {', '.join(info['missing_signoffs'])}; the Publisher does not "
                               "waive a gate")
    attempt.authority()
    attempt.candidate()
    if info.get("size_review") and info.get("size_problem"):
        raise director("gate", f"{info['size_problem']}; the Publisher does not waive a gate")
    attempt.cache_current()
    work = attempt.workspace()
    attempt.lineage(work)
    tip = attempt.facts["public_before"]
    attempt.journal_step("started", "")
    already = attempt.landed(work, tip)
    if not already:
        attempt.require_one_commit_on(work, tip)
        bundle = attempt.bundle(work, tip)
        attempt.journal_step("prepared", tip)
        attempt.push(bundle, tip)
    tip = attempt.read_back(work)
    attempt.journal_step("pushed", attempt.facts["public_before"])
    attempt.facts["public_after"] = tip
    cache_tip = attempt.refresh_cache(tip)
    attempt.facts["cache_after"] = cache_tip
    attempt.journal_step("verified")
    return attempt.evidence(tip, cache_tip, already)


def state_root(project: str, environ: dict[str, str] | None = None) -> Path:
    """The Publisher's own scratch space: under the account's XDG state directory, per project."""
    environ = dict(os.environ if environ is None else environ)
    base = environ.get("XDG_STATE_HOME", "").strip() or str(Path(environ.get("HOME") or Path.home()) / ".local/state")
    return Path(base) / "switchyard" / "publisher" / project
