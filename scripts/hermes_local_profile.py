"""A Hermes role on the host's local model service: its declared profile, its home, its start check (SYRD-692).

The local model service (SYRD-691) is one loopback, OpenAI-compatible endpoint serving one pinned model. A role
that uses it stays `runtime=hermes`: Hermes owns provider selection, in its own `config.yaml` (SYRD-105), and
this module only writes the one profile the operator chose into each such role's own HERMES_HOME, keeps it
there, and shows it.

**The declaration.** A role's launcher entry carries `hermes_local`: the exact model id, the endpoint, the
server and its release, the model's upstream revision, the artifact and its revision, the quantization and the
context the server holds. All of it is taken from the service's own root-owned record when the operator opts
in (`profile_from_service`) and is non-secret. No floating alias is accepted: a model or artifact that is not
pinned is refused.

**The home.** At every start the role's `config.yaml` is written privately, marked as Switchyard's
(`CONFIG_MARK`): the shared document with every credential value removed, the main model on the endpoint
(`provider: custom`, a placeholder key, its context stated), no fallback providers, delegation inheriting the
main model, and every auxiliary task explicitly on the same endpoint. Hermes's auxiliary `auto` provider walks
hosted providers when the main one fails (`agent/auxiliary_client.py`, 0.15.2); an explicit `custom` falls back
only to its own chain, which is emptied, and then to the main model. The shared `.env` and `auth.json`, which
carry cloud keys and OAuth state, are not linked in; links left from a cloud configuration are removed. Every
write goes through a descriptor of the home opened without following links, so a planted link redirects
nothing.

**The start check.** The pane runs this file before Hermes (`pane_command`), with the role's own HERMES_HOME
and environment. Hermes starts only when the endpoint serves exactly the declared model with the declared
context, that context is at least Hermes's own minimum, the service's record still names the declared
revisions, the home's config is this profile's, and no cloud key is in the environment, in the home's `.env`,
or in an `auth.json`. Anything else is refused in the pane, in plain words, with the remedy; nothing falls back
to a hosted model.

This file runs standalone in the pane (`python3 -I`), so it imports only the standard library at its top.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

#: The launcher-entry field, and the profile's name as Switchyard shows it.
PROFILE_KEY = "hermes_local"
PROFILE_NAME = "local-qwen"
#: The local model service's record (SYRD-691), and the schema this reads.
SERVICE_RECORD = Path("/var/lib/switchyard/local-models/service.json")
SERVICE_SCHEMA = "switchyard.local-model.v1"
#: Hermes 0.15.2 refuses a model whose context is below this, even one stated in config.yaml
#: (`agent/agent_init.py`, `MINIMUM_CONTEXT_LENGTH`).
HERMES_MINIMUM_CONTEXT = 64_000
#: The key Hermes sends a local server that needs none; stated, so it never reaches for OPENAI_API_KEY.
NO_KEY = "no-key-required"
CONFIG_NAME = "config.yaml"
CONFIG_MARK = (
    "# Written by Switchyard for this role (SYRD-692): Hermes on the local model service only. It is rewritten "
    "at every start from the shared config.yaml and the role's declared profile; change the profile with "
    "`switchyard set-role-runtime`."
)
#: The second line: the profile this config was written for, which the start check compares.
PROFILE_LINE = "# switchyard-local-profile: "
#: Every auxiliary task Hermes 0.15.2 knows (`hermes_cli/config.py` defaults, and `goal_judge` from goals.py).
AUXILIARY_TASKS = ("vision", "web_extract", "compression", "skills_hub", "approval", "mcp", "title_generation",
                   "triage_specifier", "kanban_decomposer", "profile_describer", "curator", "goal_judge")
#: Shared-home entries that carry hosted-provider credentials or OAuth state: never linked into a local role.
CREDENTIAL_ENTRIES = (".env", "auth.json", "auth.lock")
#: Environment Hermes reads for a provider, an endpoint or a hosted key. Unset in a local role's pane, and any of
#: them still set refuses the start. Anything else named `*_API_KEY` is refused too.
CLOUD_ENV_KEYS = frozenset({
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "ANTHROPIC_API_KEY",
    "ANTHROPIC_TOKEN", "AZURE_ANTHROPIC_KEY", "OLLAMA_API_KEY", "NOUS_API_KEY", "CUSTOM_BASE_URL",
    "HERMES_INFERENCE_PROVIDER", "HERMES_TUI_PROVIDER", "HERMES_INFERENCE_MODEL", "HERMES_MODEL",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
})
_SECRET_NAME = re.compile(r"(^|_)(api_?key|token|secret|password|access_key)$", re.IGNORECASE)
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_FLOATING = re.compile(r"(^|[/:@_.-])(latest|main|head|stable|default)$", re.IGNORECASE)
_LOOPBACK_URL = re.compile(r"^http://(127\.0\.0\.1|\[::1\]):([0-9]{2,5})/v1$")


class ProfileRefusal(SystemExit):
    """Why a local profile cannot be declared, written or started; nothing was changed by what raised it."""


@dataclass(frozen=True)
class LocalProfile:
    model: str
    base_url: str
    server: str
    revision: str
    artifact: str
    quantization: str
    context: int
    name: str = PROFILE_NAME

    def problems(self) -> list[str]:
        found = []
        if not _MODEL_ID.match(self.model) or _FLOATING.search(self.model):
            found.append(f"model {self.model!r} is not one exact, pinned model id")
        match = _LOOPBACK_URL.match(self.base_url)
        if not match or not 1024 <= int(match.group(2)) <= 65535:
            found.append(f"endpoint {self.base_url!r} is not http://127.0.0.1:<port>/v1 or http://[::1]:<port>/v1")
        if not _REVISION.match(self.revision):
            found.append(f"revision {self.revision!r} is not a full 40-hex commit")
        repository, _, artifact_revision = self.artifact.partition("@")
        if not _MODEL_ID.match(repository) or not _REVISION.match(artifact_revision):
            found.append(f"artifact {self.artifact!r} is not <repository>@<40-hex revision>")
        if not re.match(r"^[A-Za-z0-9._ -]+$", self.server or "") or not self.server.strip():
            found.append(f"server {self.server!r} is not a server and release")
        if not re.match(r"^[A-Za-z0-9_.-]+$", self.quantization or ""):
            found.append(f"quantization {self.quantization!r} is not a quantization name")
        if not isinstance(self.context, int) or isinstance(self.context, bool) or self.context <= 0:
            found.append(f"context {self.context!r} is not a token count")
        if self.name != PROFILE_NAME:
            found.append(f"profile {self.name!r} is not {PROFILE_NAME!r}")
        return found

    def entry(self) -> dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        return (f"{self.name}: {self.model} at {self.base_url} ({self.server}; revision {self.revision[:12]}; "
                f"{self.artifact.partition('@')[0]}@{self.artifact.partition('@')[2][:12]} {self.quantization}; "
                f"context {self.context:,})")


def role_fields(raw: Mapping[str, Any], *, role: str, cli_name: str) -> dict[str, Any]:
    """The RoleConfig field a launcher entry's profile gives, or none at all, so every other role loads as before."""
    if raw.get(PROFILE_KEY) is None:
        return {}
    return {PROFILE_KEY: profile_from_entry(raw[PROFILE_KEY], role=role, cli_name=cli_name,
                                            model=str(raw.get("model") or "").strip())}


def artifact_roles(raw: Any, *, path: Path, role_clis: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    """A design artifact's `project.local_qwen_roles`: roles it puts on the local model service, each a Hermes role."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise SystemExit(f"{path} project.local_qwen_roles must be a list of role names")
    clis = dict(role_clis)
    roles = tuple(dict.fromkeys(item.strip() for item in raw))
    wrong = [f"{role} ({clis.get(role, 'not a role')})" for role in roles if clis.get(role) != "hermes"]
    if wrong:
        raise SystemExit(f"{path} project.local_qwen_roles names roles that are not Hermes roles: {', '.join(wrong)}")
    return roles


def _said(roles: Iterable[str], profile: LocalProfile, print_func: Callable[[str], None]) -> None:
    for role in roles:
        print_func(f"switchyard: {role} will run hermes on {profile.describe()}")
    if context_problem(profile):
        print_func(f"switchyard: it will not start until then: {context_problem(profile)}")


def new_project_roles(design_artifact: Any, role_models: Mapping[str, str] | None, *,
                      print_func: Callable[[str], None]) -> tuple[dict[str, dict[str, Any]], Mapping[str, str] | None]:
    """`switchyard new`: each of the artifact's local roles' profile, from the service's record, and its model."""
    roles = tuple(getattr(design_artifact, "local_qwen_roles", ()) or ())
    if not roles:
        return {}, role_models
    profile = profile_from_service()
    _said(roles, profile, print_func)
    return {role: profile.entry() for role in roles}, {**(role_models or {}), **{role: profile.model for role in roles}}


def opt_in(role_name: str, cli: str, *, print_func: Callable[[str], None]) -> tuple[str, LocalProfile]:
    """`add-role --local-qwen`: the runtime (Hermes, or a refusal) and the service's profile."""
    if cli not in ("", "hermes"):
        raise SystemExit(f"switchyard: --local-qwen is Hermes configuration and {cli} is not Hermes. Nothing was changed.")
    profile = profile_from_service()
    _said((role_name,), profile, print_func)
    return "hermes", profile


def declare(config: Any, config_path: Path, role_name: str, profile: LocalProfile, *, runner: Callable[..., Any]) -> Any:
    """Write a role's profile into its entry, unless it is already there; the config as it now stands."""
    from scripts import team_launcher as launcher

    if getattr(launcher._role_named(config, role_name), PROFILE_KEY, None) == profile:
        return config
    write_entry(config, config_path, role_name, profile, runner=runner)
    return launcher.load_project_config(config.project, config_path)


def remove_role_material(config: Any, role: Any) -> bool:
    """A role leaving Hermes: its profile's config goes from the home it ran in, and nothing else."""
    from scripts import team_launcher as launcher

    return remove_role_config(launcher.role_hermes_home(config, role))


def profile_from_entry(raw: Any, *, role: str, cli_name: str, model: str) -> LocalProfile | None:
    """The profile a launcher entry declares, or None; a declaration that cannot be one refuses the config."""
    if raw is None:
        return None
    where = f"role {role} {PROFILE_KEY}"
    if not isinstance(raw, dict):
        raise ProfileRefusal(f"{where} must be a JSON object")
    names = {item.name for item in fields(LocalProfile)}
    unknown = sorted(set(raw) - names)
    missing = sorted(names - set(raw) - {"name"})
    if unknown or missing:
        raise ProfileRefusal(f"{where} has unknown keys {unknown} or lacks {missing}")
    profile = LocalProfile(**{key: raw[key] for key in names if key in raw})
    problems = profile.problems()
    if cli_name != "hermes":
        problems.append(f"it is a Hermes profile and the role runs {cli_name!r}")
    if model != profile.model:
        problems.append(f"the role's model {model!r} is not the profile's {profile.model!r}")
    if problems:
        raise ProfileRefusal(f"{where} is refused: " + "; ".join(problems))
    return profile


def _open_no_follow(path: Path, *, anchor: Path, owner_uid: int, directory: bool) -> int:
    """A descriptor for `path`, walked from `anchor` one component at a time, never through a link.

    Every directory on the way and the target itself must belong to `owner_uid` and be writable by nobody else,
    so nobody but that owner can have put the record there.
    """
    relative = path.relative_to(anchor)
    fd = os.open(anchor, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(relative.parts):
            last = index == len(relative.parts) - 1
            here = os.fstat(fd)
            if here.st_uid != owner_uid or here.st_mode & 0o022:
                raise ProfileRefusal(f"{anchor.joinpath(*relative.parts[:index])} is not owned by uid {owner_uid} "
                                     f"and writable only by it")
            flags = os.O_NOFOLLOW | (os.O_PATH | os.O_DIRECTORY if not last or directory else os.O_RDONLY)
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                raise  # absent, not untrusted: the caller says the service is not installed
            except OSError as exc:
                raise ProfileRefusal(f"{anchor.joinpath(*relative.parts[:index + 1])} cannot be opened without "
                                     f"following a link ({exc.strerror})") from exc
            os.close(fd)
            fd = child
        final = os.fstat(fd)
        if final.st_uid != owner_uid or final.st_mode & 0o022 or (
                not directory and not stat.S_ISREG(final.st_mode)):
            raise ProfileRefusal(f"{path} is not a {'directory' if directory else 'regular file'} owned by uid "
                                 f"{owner_uid} and writable only by it")
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_service_record(path: Path = SERVICE_RECORD, *, anchor: Path = Path("/"), owner_uid: int = 0) -> dict[str, Any]:
    """The local model service's own record, read without following a link and only if root owns all of it."""
    try:
        fd = _open_no_follow(path, anchor=anchor, owner_uid=owner_uid, directory=False)
    except ProfileRefusal as exc:
        raise ProfileRefusal(f"switchyard: the local model service's record cannot be trusted: {exc}") from None
    except FileNotFoundError:
        raise ProfileRefusal(f"switchyard: the local model service is not installed ({path} does not exist); see "
                             "`switchyard local-model preview`") from None
    with os.fdopen(fd, encoding="utf-8") as handle:
        try:
            record = json.load(handle)
        except ValueError as exc:
            raise ProfileRefusal(f"switchyard: {path} is not JSON: {exc}") from None
    if not isinstance(record, dict) or record.get("schema") != SERVICE_SCHEMA:
        raise ProfileRefusal(f"switchyard: {path} is not a {SERVICE_SCHEMA} record")
    return record


def profile_from_record(record: Mapping[str, Any]) -> LocalProfile:
    """The profile a service record describes, refused unless every pin is there and the endpoint is loopback."""
    try:
        model, engine, settings = record["model"], record["engine"], record["settings"]
        host = str(settings["host"])
        profile = LocalProfile(
            model=str(model["id"]),
            base_url=f"http://{'[' + host + ']' if ':' in host else host}:{int(settings['port'])}/v1",
            server=f"{engine['name']} {engine['tag']}",
            revision=str(model["upstream_revision"]),
            artifact=f"{model['artifact_repository']}@{model['artifact_revision']}",
            quantization=str(model["quantization"]),
            context=int(settings["context"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProfileRefusal(f"switchyard: the local model service's record lacks {exc}") from None
    problems = profile.problems()
    if problems:
        raise ProfileRefusal("switchyard: the local model service's record is refused: " + "; ".join(problems))
    return profile


def profile_from_service(path: Path = SERVICE_RECORD, **where: Any) -> LocalProfile:
    """What the operator opts a role into: the installed service, exactly as its record pins it."""
    return profile_from_record(read_service_record(path, **where))


def context_problem(profile: LocalProfile) -> str:
    """Why Hermes would refuse this profile's context, or ""."""
    if profile.context < HERMES_MINIMUM_CONTEXT:
        return (f"the service holds a {profile.context:,}-token context and Hermes refuses any model below "
                f"{HERMES_MINIMUM_CONTEXT:,}; raise the service's --context to at least {HERMES_MINIMUM_CONTEXT:,} "
                f"(`switchyard local-model`), then re-pin the role with `switchyard set-role-runtime <project> "
                f"<role> --cli hermes --local-qwen`")
    return ""


def _scrubbed(value: Any) -> Any:
    """The document with every credential-named value removed, at any depth."""
    if isinstance(value, dict):
        return {key: _scrubbed(item) for key, item in value.items()
                if not (_SECRET_NAME.search(str(key)) and isinstance(item, str) and item)}
    if isinstance(value, list):
        return [_scrubbed(item) for item in value]
    return value


def render_config(shared: Mapping[str, Any] | None, profile: LocalProfile, *, effort: str = "") -> dict[str, Any]:
    """The role's whole config: the shared document's settings, on the local endpoint and nowhere else."""
    document = _scrubbed(dict(shared or {}))
    for key in ("providers", "custom_providers", "fallback_model"):
        document.pop(key, None)
    local = {"provider": "custom", "model": profile.model, "base_url": profile.base_url, "api_key": NO_KEY}
    document["model"] = {"provider": "custom", "default": profile.model, "base_url": profile.base_url,
                         "api_key": NO_KEY, "api_mode": "chat_completions", "context_length": profile.context}
    document["fallback_providers"] = []
    delegation = document.get("delegation") if isinstance(document.get("delegation"), dict) else {}
    # Empty means a subagent inherits the parent's provider: the local endpoint.
    document["delegation"] = {**delegation, "provider": "", "model": "", "base_url": "", "api_key": ""}
    auxiliary = document.get("auxiliary") if isinstance(document.get("auxiliary"), dict) else {}
    document["auxiliary"] = {
        task: {**(auxiliary.get(task) if isinstance(auxiliary.get(task), dict) else {}), **local, "fallback_chain": []}
        for task in dict.fromkeys((*AUXILIARY_TASKS, *auxiliary))
    }
    if effort:
        agent = document.get("agent") if isinstance(document.get("agent"), dict) else {}
        document["agent"] = {**agent, "reasoning_effort": effort}
    return document


def _profile_line(profile: LocalProfile) -> str:
    return PROFILE_LINE + json.dumps(profile.entry(), sort_keys=True, separators=(",", ":"))


def is_ours(text_head: str) -> bool:
    return text_head.split("\n", 1)[0] == CONFIG_MARK


def account_home() -> Path:
    """This account's home, as the password database names it: the root a local role's home is walked from."""
    import pwd

    return Path(pwd.getpwuid(os.geteuid()).pw_dir)


def _home_fd(hermes_home: Path, *, create: bool = True) -> int:
    """The role's home, as a descriptor, walked from the account's own home one component at a time.

    The root is the home the password database names (`account_home`), never a prefix of the configured session
    directory, which a configured link could carry anywhere (Audit). The home may be named by its resolved path
    too, but only when that is the same directory. Every component below it -- a configured prefix before
    `.local/state`, `hermes-homes`, the role's home -- is opened without following a link, made (0700) by
    descriptor when it is missing and `create` says so, and must belong to this account and be writable by nobody
    else. A link anywhere on the way is refused before anything is made, changed or removed through it.
    FileNotFoundError when the home is not there and `create` is False.
    """
    named = account_home()
    me = os.geteuid()
    fd = os.open(named, os.O_RDONLY | os.O_DIRECTORY)
    try:
        home = os.fstat(fd)
        if home.st_uid != me:
            raise ProfileRefusal(f"{named} does not belong to this account")
        for anchor in dict.fromkeys((named, Path(os.path.realpath(named)))):
            try:
                relative = hermes_home.relative_to(anchor)
                break
            except ValueError:
                continue
        else:
            raise ProfileRefusal(f"{hermes_home} is not inside {named}, this account's home; the local profile is not "
                                 "written there")
        if any(part in ("", ".", "..") for part in relative.parts):
            raise ProfileRefusal(f"{hermes_home} is not a plain path below {anchor}")
        for index, part in enumerate(relative.parts):
            where = anchor.joinpath(*relative.parts[:index + 1])
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise ProfileRefusal(f"{where} is a link or not a directory ({exc.strerror}); nothing is written "
                                     "through it") from None
            found = os.fstat(child)
            if found.st_uid != me or found.st_mode & 0o022:
                os.close(child)
                raise ProfileRefusal(f"{where} does not belong to this account alone")
            os.close(fd)
            fd = child
        os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _entry(fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _head(fd: int, name: str) -> str:
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    except OSError:
        return ""
    with os.fdopen(handle, "rb") as stream:
        return stream.read(4096).decode("utf-8", errors="replace")


def write_role_home(hermes_home: Path, profile: LocalProfile, *, shared_home: Path, effort: str = "") -> str:
    """Give a local role's home its config and drop its credential links; any notice, or "" when done.

    A `config.yaml` of the role's own that Switchyard did not write is left alone and said; the start check
    then refuses, because the config is not this profile's.
    """
    try:
        import yaml
    except ImportError:
        return "PyYAML is not available, so the local profile's config.yaml cannot be written"
    fd = _home_fd(hermes_home)
    try:
        for name in CREDENTIAL_ENTRIES:
            found = _entry(fd, name)
            if found is not None and stat.S_ISLNK(found.st_mode):
                os.unlink(name, dir_fd=fd)  # the link only: whatever it named is untouched
        found = _entry(fd, CONFIG_NAME)
        if found is not None and not stat.S_ISLNK(found.st_mode) and not is_ours(_head(fd, CONFIG_NAME)):
            from scripts.hermes_role_config import HERMES_ROLE_CONFIG_MARK

            # The effort-only config (SYRD-546) is Switchyard's too, and this one replaces it.
            if _head(fd, CONFIG_NAME).split("\n", 1)[0] != HERMES_ROLE_CONFIG_MARK:
                return (f"{hermes_home / CONFIG_NAME} is the role's own, not Switchyard's; the local profile is not "
                        "written over it, so the role will not start -- move it aside to use the profile")
        shared = shared_home / CONFIG_NAME
        try:
            document = yaml.safe_load(shared.read_text(encoding="utf-8")) if shared.is_file() else {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            return f"cannot read {shared} for the local profile: {exc}"
        if document is not None and not isinstance(document, dict):
            document = {}
        text = "\n".join((CONFIG_MARK, _profile_line(profile),
                          yaml.safe_dump(render_config(document, profile, effort=effort), sort_keys=False)))
        temp = f".{CONFIG_NAME}.{os.getpid()}.tmp"
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            # Over the link, if one is there: what it named is never written.
            os.replace(temp, CONFIG_NAME, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            try:
                os.unlink(temp, dir_fd=fd)
            except FileNotFoundError:
                pass
        return ""
    finally:
        os.close(fd)


def link_shared(hermes_home: Path, shared_home: Path, names: Iterable[str]) -> None:
    """Link the shared home's other entries (SOUL.md, hooks, skills, ...) into a local role's home, by descriptor.

    As every role gets them, but never through a link on the way: an entry already linked there stays, a link
    elsewhere is replaced, and anything else of the role's own is left.
    """
    fd = _home_fd(hermes_home)
    try:
        for name in names:
            source = str(shared_home / name)
            found = _entry(fd, name)
            if found is not None and stat.S_ISLNK(found.st_mode):
                if os.readlink(name, dir_fd=fd) == source:
                    continue
                os.unlink(name, dir_fd=fd)
            elif found is not None:
                continue
            os.symlink(source, name, dir_fd=fd)
    finally:
        os.close(fd)


def remove_role_config(hermes_home: Path) -> bool:
    """Remove the local profile's config.yaml from a role's home, if Switchyard wrote it; whether it did."""
    try:
        fd = _home_fd(hermes_home, create=False)
    except (FileNotFoundError, ProfileRefusal):
        return False
    try:
        found = _entry(fd, CONFIG_NAME)
        if found is None or not stat.S_ISREG(found.st_mode) or not is_ours(_head(fd, CONFIG_NAME)):
            return False
        os.unlink(CONFIG_NAME, dir_fd=fd)
        return True
    finally:
        os.close(fd)


# -- the start check, run in the pane ----------------------------------------------------------------------


def _get_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback, checked above
        return json.loads(response.read(4 << 20).decode("utf-8"))


def served(profile: LocalProfile, *, get: Callable[[str, float], Any] = _get_json, timeout: float = 5.0) -> tuple[str, int]:
    """Whether the endpoint serves the profile's model, and the context it holds: (problem or "", n_ctx or 0)."""
    try:
        listing = get(f"{profile.base_url}/models", timeout)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return f"{profile.base_url} did not answer ({getattr(exc, 'reason', exc)})", 0
    models = [item for item in (listing or {}).get("data") or [] if isinstance(item, dict)] if isinstance(
        listing, dict) else []
    ids = [str(item.get("id")) for item in models]
    if profile.model not in ids:
        return f"{profile.base_url} serves {ids or 'no model'}, not {profile.model}", 0
    meta = next(item for item in models if item.get("id") == profile.model).get("meta") or {}
    n_ctx = meta.get("n_ctx") if isinstance(meta, dict) else None
    if not isinstance(n_ctx, int):
        try:
            props = get(profile.base_url.removesuffix("/v1") + "/props", timeout)
            n_ctx = int(props["default_generation_settings"]["n_ctx"])
        except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError) as exc:
            return f"{profile.base_url} does not say what context it holds ({exc})", 0
    return "", n_ctx


def endpoint_problems(profile: LocalProfile, *, record: Mapping[str, Any] | None,
                      get: Callable[[str, float], Any] = _get_json) -> list[str]:
    """What the service says against the profile now: its pins, its context and its endpoint; empty when it serves it."""
    found = []
    if record is not None:
        try:
            pinned = profile_from_record(record)
        except ProfileRefusal as exc:
            found.append(str(exc).removeprefix("switchyard: "))
        else:
            if pinned != profile:
                changed = [f"{name} {getattr(profile, name)!r} -> {getattr(pinned, name)!r}"
                           for name in ("model", "base_url", "server", "revision", "artifact", "quantization", "context")
                           if getattr(profile, name) != getattr(pinned, name)]
                found.append("the service now pins something else (" + "; ".join(changed) + "); re-pin the role with "
                             "`switchyard set-role-runtime <project> <role> --cli hermes --local-qwen`")
    problem = context_problem(profile)
    if problem:
        found.append(problem)
    problem, n_ctx = served(profile, get=get)
    if problem:
        found.append(problem + "; start it with `switchyard local-model start` and read `switchyard local-model status`")
    elif n_ctx != profile.context:
        found.append(f"{profile.base_url} holds a {n_ctx:,}-token context, not the declared {profile.context:,}; "
                     "re-pin the role")
    return found


def start_problems(profile: LocalProfile, *, environ: Mapping[str, str], record: Mapping[str, Any] | None,
                   get: Callable[[str, float], Any] = _get_json) -> list[str]:
    """Everything that stops this role starting Hermes on its profile now: the endpoint's answer, and the role's
    own environment and home. Empty when it may."""
    found = endpoint_problems(profile, record=record, get=get)
    leaked = sorted(key for key, value in environ.items()
                    if value.strip() and (key in CLOUD_ENV_KEYS or key.endswith("_API_KEY")))
    if leaked:
        found.append(f"the environment carries {', '.join(leaked)}, which a local role must not have")
    home = Path(environ.get("HERMES_HOME") or "")
    if not str(home) or not home.is_absolute():
        found.append("HERMES_HOME is not set to the role's own home")
        return found
    head = ""
    try:
        with open(home / CONFIG_NAME, "rb") as stream:  # a link is fine to read; its text decides
            head = stream.read(8192).decode("utf-8", errors="replace")
    except OSError:
        pass
    lines = head.split("\n")
    if lines[0] != CONFIG_MARK or len(lines) < 2 or lines[1] != _profile_line(profile):
        found.append(f"{home / CONFIG_NAME} is not this profile's config, so Hermes could use another provider")
    keys = []
    try:
        for line in (home / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
            name, _, value = line.removeprefix("export ").partition("=")
            if value.strip().strip("'\"") and (name.strip() in CLOUD_ENV_KEYS or name.strip().endswith("_API_KEY")):
                keys.append(name.strip())
    except OSError:
        pass
    if keys:
        found.append(f"{home / '.env'} holds {', '.join(sorted(keys))}; a local role must have no hosted key -- move it aside")
    if os.path.lexists(home / "auth.json"):
        found.append(f"{home / 'auth.json'} holds hosted-provider sign-ins; a local role must have none -- move it aside")
    return found


def pane_command(role_name: str, profile: LocalProfile, command: list[str], *, python: str = "python3") -> list[str]:
    """`command`, run only once the start check passes, in the pane, with the role's own environment."""
    return [python, "-I", str(Path(__file__).resolve()), "check", "--role", role_name,
            "--profile", json.dumps(profile.entry(), sort_keys=True, separators=(",", ":")), "--", *command]


def unset_keys() -> tuple[str, ...]:
    """What a local role's pane unsets before anything runs: every hosted key or endpoint Hermes would read."""
    names = set(CLOUD_ENV_KEYS)
    try:
        from scripts.role_credentials import HERMES_PROVIDER_ENV_KEYS
    except ImportError:  # the standalone check has its own list
        pass
    else:
        names |= set(HERMES_PROVIDER_ENV_KEYS)
    return tuple(sorted(names))


def write_entry(config: Any, config_path: Path, role_name: str, profile: LocalProfile | None, *,
                runner: Callable[..., Any]) -> Any:
    """Set (or, with None, remove) a role's declared profile in its launcher entry; a profile brings its model.

    Returns what was there, for `restore_entry`. Written as the project owner, as `record_role_model` writes a
    model, so a root-run command leaves a config the tenant can still read.
    """
    from scripts import team_launcher as launcher

    raw = launcher.read_owner_json(config.run_as_user, config_path)
    entry = next((item for item in raw.get("roles") or [] if isinstance(item, dict) and item.get("role") == role_name),
                 None)
    if entry is None:
        raise ProfileRefusal(f"switchyard: {config_path} has no role {role_name!r}")
    previous = (entry.get(PROFILE_KEY), entry.get("model"))
    entry.pop(PROFILE_KEY, None)
    if profile is not None:
        entry[PROFILE_KEY] = profile.entry()
        entry["model"] = profile.model  # one declaration: the config never names a model its profile does not serve
    launcher._write_json_atomic(config_path, raw, owner_user=config.run_as_user)
    launcher.ensure_owner_file(config, config_path, runner=runner)
    return previous


def restore_entry(config: Any, config_path: Path, role_name: str, previous: Any, *, runner: Callable[..., Any]) -> None:
    """Put back what `write_entry` replaced, after the change it was written for did not happen."""
    from scripts import team_launcher as launcher

    raw = launcher.read_owner_json(config.run_as_user, config_path)
    profile, model = previous
    for entry in raw.get("roles") or []:
        if isinstance(entry, dict) and entry.get("role") == role_name:
            for key, value in ((PROFILE_KEY, profile), ("model", model)):
                entry.pop(key, None)
                if value is not None:
                    entry[key] = value
    launcher._write_json_atomic(config_path, raw, owner_user=config.run_as_user)
    launcher.ensure_owner_file(config, config_path, runner=runner)


def validation_failures(roles: Iterable[Any], *, print_func: Callable[[str], None] = print,
                        get: Callable[[str, float], Any] = _get_json,
                        record_reader: Callable[[], Mapping[str, Any]] | None = None) -> list[Any]:
    """`validate-models` for local model service roles: the endpoint against each declared profile."""
    from scripts.model_validation import ModelValidationFailure

    failures = []
    for role in roles:
        profile = getattr(role, "hermes_local", None)
        if profile is None:
            continue
        print_func(f"switchyard: checking {role.role}'s local profile ({profile.describe()}) against the service")
        try:
            problems = endpoint_problems(profile, record=(record_reader or read_service_record)(), get=get)
        except ProfileRefusal as exc:
            problems = [str(exc).removeprefix("switchyard: ")]
        if problems:
            failures.append(ModelValidationFailure(
                role=role.role, cli="hermes", model=profile.model, reason="the local model service does not serve "
                "the declared profile", suggestion="; ".join(problems), evidence=()))
    return failures


def describe_role(role: Any) -> str:
    """How a role's runtime reads in a preview or status line: its profile, when it has one."""
    profile = getattr(role, "hermes_local", None)
    return f"hermes on {profile.describe()}" if profile else ""


def status_lines(configs: Iterable[Any], *, get: Callable[[str, float], Any] = _get_json) -> list[str]:
    """`switchyard status`'s section: every local role, its profile, and whether the endpoint serves it now."""
    rows = [(config.project, role) for config in configs for role in config.roles
            if getattr(role, "hermes_local", None) is not None]
    if not rows:
        return []
    lines = ["LOCAL MODEL ROLES (Hermes on the local model service; no hosted provider)"]
    answers: dict[LocalProfile, str] = {}
    for project, role in rows:
        profile = role.hermes_local
        if profile not in answers:
            problem, n_ctx = served(profile, get=get, timeout=2.0)
            answers[profile] = problem or (f"serving it, {n_ctx:,}-token context" + (
                "" if n_ctx == profile.context else f" -- NOT the declared {profile.context:,}"))
        lines.append(f"  {project} {role.role}: {describe_role(role)}")
        lines.append(f"    endpoint: {answers[profile]}" + (f"; {context_problem(profile)}" if context_problem(profile) else ""))
    return lines


def main(argv: list[str] | None = None) -> int:
    """`check --role R --profile JSON -- command...`: refuse in plain words, or become Hermes."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 6 or argv[0] != "check" or argv[1] != "--role" or argv[3] != "--profile" or "--" not in argv:
        print("usage: hermes_local_profile.py check --role ROLE --profile JSON -- COMMAND...", file=sys.stderr)
        return 2
    role, raw, command = argv[2], argv[4], argv[argv.index("--") + 1:]
    try:
        try:
            declared = json.loads(raw)
        except ValueError as exc:
            raise ProfileRefusal(f"its profile is not JSON ({exc})") from None
        model = str(declared.get("model") or "") if isinstance(declared, dict) else ""
        profile = profile_from_entry(declared, role=role, cli_name="hermes", model=model)
        record: dict[str, Any] | None = read_service_record()
    except ProfileRefusal as exc:
        print(f"switchyard: {role} will not start Hermes on its local profile: {str(exc).removeprefix('switchyard: ')}",
              file=sys.stderr)
        return 1
    problems = start_problems(profile, environ=os.environ, record=record)
    if problems or not command:
        print(f"switchyard: {role} will not start Hermes on {profile.describe()}:", file=sys.stderr)
        for problem in problems or ["no command to run"]:
            print(f"switchyard:   {problem}", file=sys.stderr)
        print("switchyard: nothing falls back to a hosted model; fix the above and start the role again.", file=sys.stderr)
        return 1
    os.execvp(command[0], command)
    return 127  # not reached


if __name__ == "__main__":
    raise SystemExit(main())
