#!/usr/bin/env python3
"""SYRD-709: a root registry entry is authoritative for its slug; a release's bundled launcher config never shadows it.

Found in SYRD-705's v6 run (2026-10-30): every release ships
`config/team-launcher/pgu.json`, the legacy config (`run_as_user: agent`,
`repository: /home/agent/Projects/pgu`). At 218a906d:
- `switchyard register <pgu-agent's pgu.json>` refused: the bundled config
  counted as a registration of `pgu`;
- and once registered anyway, every resolution of `pgu` with the default
  config directory still answered with the bundled config, so deploy-release,
  release-status, privileged actions and launches would act on the legacy
  tenant.

Everything here is the real code, run as this account with the release's own
bundled config directory (`team_launcher.DEFAULT_CONFIG_DIR`, which holds the
real legacy `pgu.json`) and a registry in a test-owned directory, named the
way root names it (`SWITCHYARD_REGISTRY_DIR`). No real registry or tenant is
read or written. Deploy-release is entered as root would enter it, with its
euid check stood in and the config loader stopped at its first read, so
nothing is deployed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher  # noqa: E402,I001
from scripts import launcher_dispatch, project_resolution, project_status, tenant_release_deploy  # noqa: E402

CHECKS = 0
BUNDLED = launcher.DEFAULT_CONFIG_DIR / "pgu.json"


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def refusal(call: Callable[[], Any]) -> str:
    try:
        call()
    except SystemExit as exc:
        return str(exc)
    return ""


def resolved(selection: str, **kwargs: Any) -> Any:
    """The config a selection resolves to, or the refusal, so a wrong answer fails a check rather than the run."""
    try:
        return launcher._resolve_switchyard_project(selection, **kwargs).config_path
    except SystemExit as exc:
        return f"refused: {exc}"


def launcher_resolved(selection: str) -> Any:
    try:
        return launcher._resolve_launcher_project_config(selection).config_path
    except SystemExit as exc:
        return f"refused: {exc}"


def registered(path: Path, **kwargs: Any) -> Any:
    try:
        return launcher._register_switchyard_project(path, **kwargs)
    except SystemExit as exc:
        return f"refused: {exc}"


class Stop(Exception):
    """Raised by a stand-in loader: the command has chosen its configuration, and goes no further."""


def chosen(command: Callable[[], Any]) -> Path | None:
    """The configuration a command loads first, stopping it there."""
    seen: list[Path] = []
    saved = launcher.load_project_config

    def loader(project: str, path: Path | None = None, **_kwargs: Any) -> Any:
        seen.append(Path(path) if path is not None else Path())
        raise Stop

    launcher.load_project_config = loader
    try:
        command()
    except (Stop, SystemExit):  # a refusal before any read loads nothing
        pass
    finally:
        launcher.load_project_config = saved
    return seen[0] if seen else None


def main() -> int:
    check(BUNDLED.is_file() and json.loads(BUNDLED.read_text())["run_as_user"] == "agent",
          f"the release ships the legacy pgu config as a bundled launcher config: {BUNDLED}")
    tmp = Path(tempfile.mkdtemp(prefix="s709-"))
    saved_env = os.environ.get(launcher.SWITCHYARD_REGISTRY_DIR_ENV)
    try:
        registry = tmp / "etc" / "switchyard" / "projects"
        os.environ[launcher.SWITCHYARD_REGISTRY_DIR_ENV] = str(registry)
        tenant = tmp / "home" / "pgu-agent" / "Projects" / "pgu" / ".switchyard" / "provision" / "pgu.json"
        tenant.parent.mkdir(parents=True)
        document = json.loads(BUNDLED.read_text())
        document.update(run_as_user="pgu-agent", repository=str(tmp / "home" / "pgu-agent" / "Projects" / "pgu"))
        tenant.write_text(json.dumps(document, indent=2))

        # Nothing registered: the bundled legacy project is offered exactly as before.
        check(resolved("pgu") == BUNDLED,
              "with no registration, `pgu` resolves to the release's bundled config, as before")

        # 1. Registration: the bundled config is no registration of `pgu`.
        written = registered(tenant)
        check(written == registry / "pgu.json" and json.loads((registry / "pgu.json").read_text())["config_path"] == str(tenant.resolve()),
              f"pgu-agent's config registers despite the bundled pgu: {written}")

        # 2. Resolution: the registration, by slug and by name, whatever the default config directory holds.
        check(resolved("pgu") == tenant.resolve(),
              "the default resolution of `pgu` is the registered pgu-agent config")
        check(resolved("PGU") == tenant.resolve(), "in any case")
        listed = [entry for entry in launcher._switchyard_entries() if entry.slug == "pgu"]
        check([entry.config_path for entry in listed] == [tenant.resolve()], f"and the only `pgu` listed: {listed}")
        check(launcher_resolved("pgu") == tenant.resolve(),
              "the launcher's own resolution (launch, upgrade, provision-runtime) follows it")

        # 3. Commands inherit it: deploy-release as root enters it, and release-status.
        saved_euid = tenant_release_deploy.os.geteuid
        tenant_release_deploy.os.geteuid = lambda: 0
        try:
            path = chosen(lambda: tenant_release_deploy.switchyard_deploy_release_command(
                "pgu", commit="0" * 40, print_func=lambda _line: None))
        finally:
            tenant_release_deploy.os.geteuid = saved_euid
        check(path == tenant.resolve(), f"deploy-release reads the registered config, never the legacy one: {path}")
        path = chosen(lambda: project_status.switchyard_release_status_command("pgu", print_func=lambda _line: None))
        check(path == tenant.resolve(), f"release-status reads the registered config: {path}")
        config = launcher.load_project_config("pgu", tenant.resolve())
        check(config.run_as_user == "pgu-agent" and Path(config.repository) == tmp / "home" / "pgu-agent" / "Projects" / "pgu",
              "so the upgrade pin and every privileged action see pgu-agent's account and repository")

        # 4. A genuine registry-to-registry collision still refuses, by slug and by name.
        other = tmp / "other" / "pgu.json"
        other.parent.mkdir()
        other.write_text(json.dumps(document))
        said = refusal(lambda: launcher._register_switchyard_project(other))
        check(said.startswith("switchyard: project slug 'pgu' is already registered to 'pgu' at ") and str(tenant.resolve()) in said,
              f"a second registration of `pgu` is refused, naming the registered one: {said}")
        named = tmp / "other" / "pgu2.json"
        named.write_text(json.dumps({**document, "project": "pgu2", "project_name": "pgu"}))
        said = refusal(lambda: launcher._register_switchyard_project(named))
        check(said == f"switchyard: project name 'pgu' is already registered as 'pgu' at {tenant.resolve()}",
              f"and so is another slug under the registered name: {said}")
        check(refusal(lambda: launcher._register_switchyard_project(tenant)).startswith("switchyard: registry entry "),
              "and its own config again only meets the existing entry")

        # 5. Names: a bundled config of another slug under a registered name is shadowed, deterministically.
        bundled = tmp / "bundled"
        bundled.mkdir()
        shutil.copy(BUNDLED, bundled / "pgu.json")
        (bundled / "legacy.json").write_text(json.dumps({**document, "project": "legacy", "project_name": "pgu"}))
        (bundled / "one.json").write_text(json.dumps({**document, "project": "one", "project_name": "twin"}))
        (bundled / "two.json").write_text(json.dumps({**document, "project": "two", "project_name": "twin"}))
        (bundled / "dup_a.json").write_text(json.dumps({**document, "project": "dup", "project_name": "dup a"}))
        (bundled / "dup_b.json").write_text(json.dumps({**document, "project": "dup", "project_name": "dup b"}))
        entries = {entry.slug: entry.config_path for entry in launcher._switchyard_entries(config_dir=bundled)}
        check(entries.get("pgu") == tenant.resolve() and "legacy" not in entries and {"one", "two"} <= set(entries),
              f"a bundled config named like a registration is shadowed; bundled-only configs stay: {entries}")
        check(entries.get("dup") == bundled / "dup_a.json", f"two bundled configs of one slug: the first, as before: {entries}")
        check(resolved("pgu", config_dir=bundled) == tenant.resolve(),
              "so `pgu`, slug and name, is the registration alone")
        check(refusal(lambda: launcher._resolve_switchyard_project("twin", config_dir=bundled))
              == "switchyard: project selector 'twin' is ambiguous",
              "while two bundled configs sharing a name stay ambiguous, as before")
        renamed = tmp / "renamed.json"
        renamed.write_text(json.dumps({**document, "project": "fresh", "project_name": "twin"}))
        check(registered(renamed, config_dir=bundled) == registry / "fresh.json",
              "registering under a bundled config's name is no collision either")
        check(resolved("twin", config_dir=bundled) == renamed.resolve(),
              "and the name then resolves to the registration")
        (registry / "fresh.json").unlink(missing_ok=True)

        # 6. Fail closed: an entry for `pgu` that cannot be used is refused, never answered with the bundled config.
        good = (registry / "pgu.json").read_text()
        for label, body in (("not JSON", "{"), ("another schema", json.dumps({"schema": "switchyard.project-registry.v0", "slug": "pgu",
                                                                                 "config_path": str(tenant)})),
                            ("no configuration", json.dumps({"schema": launcher.SWITCHYARD_REGISTRY_SCHEMA, "slug": "pgu",
                                                              "config_path": " "})),
                            ("no valid slug", json.dumps({"schema": launcher.SWITCHYARD_REGISTRY_SCHEMA, "slug": "Bad Slug!",
                                                          "config_path": str(tenant)}))):
            (registry / "pgu.json").write_text(body)
            said = refusal(lambda: launcher._resolve_switchyard_project("pgu"))
            check(said.startswith("switchyard: project 'pgu' is registered, but its registry entry ")
                  and str(registry / "pgu.json") in said and "refusing to use any other configuration" in said,
                  f"an entry with {label} is refused, naming it: {said}")
            check(all(entry.slug != "pgu" for entry in launcher._switchyard_entries()),
                  f"and no `pgu` is offered in its place ({label})")
            said = refusal(lambda: launcher._resolve_launcher_project_config("pgu"))
            check(said.startswith("switchyard: project 'pgu' is registered, but"),
                  f"the launcher's resolution says so too, not 'unknown project' ({label}): {said}")
            check(chosen(lambda: launcher_dispatch.main(["pgu", "provision-runtime", "--runtime-user", "nobody"])) is None
                  and refusal(lambda: launcher_dispatch.main(["pgu", "provision-runtime", "--runtime-user", "nobody"]))
                  .startswith("switchyard: project 'pgu' is registered, but"),
                  f"and provision-runtime never falls back to the bundled config ({label})")
        # Present but not a regular file, or not readable: claimed as surely, never looked through (Audit).
        elsewhere = tmp / "elsewhere.json"
        elsewhere.write_text(good)
        entry = registry / "pgu.json"

        def replace(make: Callable[[], None]) -> None:
            if entry.is_dir() and not entry.is_symlink():
                entry.rmdir()
            else:
                entry.unlink(missing_ok=True)
            make()

        forms = [("a broken symlink", lambda: entry.symlink_to(tmp / "nowhere.json"), "is not a regular file"),
                 ("a symlink to a valid entry", lambda: entry.symlink_to(elsewhere), "is not a regular file"),
                 ("a directory", lambda: entry.mkdir(), "is not a regular file"),
                 ("a FIFO", lambda: os.mkfifo(entry), "is not a regular file")]
        if os.geteuid() != 0:
            forms.append(("an unreadable file", lambda: (entry.write_text(good), entry.chmod(0)), "cannot be read"))
        for label, make, why in forms:
            replace(make)
            said = refusal(lambda: launcher._resolve_switchyard_project("pgu"))
            check(said.startswith(f"switchyard: project 'pgu' is registered, but its registry entry {entry} {why}"),
                  f"{label} at the entry's path is refused, never the bundled config: {said}")
            check(all(item.slug != "pgu" for item in launcher._switchyard_entries()), f"and no `pgu` is listed ({label})")
            check(str(launcher_resolved("pgu")).startswith("refused: switchyard: project 'pgu' is registered, but"),
                  f"the launcher's resolution refuses too ({label})")
            check(chosen(lambda: launcher_dispatch.main(["pgu", "provision-runtime", "--runtime-user", "nobody"])) is None,
                  f"and provision-runtime loads nothing ({label})")
        replace(lambda: entry.write_text(good))
        check(resolved("pgu") == tenant.resolve(), "repaired, it resolves again")

        # A registry that exists but cannot be listed: nothing can be known not to be registered.
        if os.geteuid() != 0:
            registry.chmod(0)
            try:
                said = refusal(lambda: launcher._resolve_switchyard_project("pgu"))
                check(said.startswith("switchyard: cannot resolve 'pgu': the registry ") and "cannot be read" in said,
                      f"an unreadable registry refuses rather than offering the bundled config: {said}")
                check(launcher._switchyard_entries() == [], "and offers no bundled config in any listing")
                check(chosen(lambda: launcher_dispatch.main(["pgu", "provision-runtime", "--runtime-user", "nobody"])) is None
                      and refusal(lambda: launcher_dispatch.main(["pgu", "provision-runtime", "--runtime-user", "nobody"]))
                      .startswith("switchyard: cannot resolve 'pgu'"),
                      "nor does provision-runtime fall back to it")
            finally:
                registry.chmod(0o755)

        # An unknown project still falls back to the bundled path for provision-runtime, as before (nothing there).
        ran: list[Any] = []
        saved_provision = launcher.provision_runtime_command
        launcher.provision_runtime_command = lambda user, config: ran.append((user, config)) or 0
        try:
            try:
                answer: Any = launcher_dispatch.main(["nosuch709", "provision-runtime", "--runtime-user", "nobody"])
            except SystemExit as exc:
                answer = f"refused: {exc}"
            check(answer == 0 and ran == [("nobody", None)],
                  f"an unknown project's provision-runtime runs with no config, as before: {answer} {ran}")
        finally:
            launcher.provision_runtime_command = saved_provision

        # 7. Removing the registration exposes the bundled legacy config again.
        (registry / "pgu.json").unlink()
        check(resolved("pgu") == BUNDLED,
              "with the entry removed, `pgu` is the bundled legacy config again")
        check(project_resolution._registry_claims() == {}, "and the registry claims nothing")

        # A link under another name to a valid record naming `pgu` is no registration of `pgu`, and is never read
        # through (Audit, round 2: `alias.json` -> a record for `pgu` made `pgu` resolve to the link's target).
        linked = tmp / "linked" / "pgu.json"
        linked.parent.mkdir()
        linked.write_text(json.dumps(document))
        record = tmp / "linked" / "record.json"
        record.write_text(json.dumps({"schema": launcher.SWITCHYARD_REGISTRY_SCHEMA, "slug": "pgu", "name": "pgu",
                                      "config_path": str(linked)}))
        (registry / "alias.json").symlink_to(record)
        check([entry.config_path for entry in launcher._registry_project_entries()] == [],
              "the registration listing does not read through the link")
        check(all(entry.config_path != linked for entry in launcher._switchyard_entries()),
              "no listing offers the link's target")
        check(resolved("pgu") == BUNDLED, f"`pgu` is not the link's target: nothing regular registers it: {resolved('pgu')}")
        check(str(resolved("alias")).startswith(f"refused: switchyard: project 'alias' is registered, but its registry entry "
                                                f"{registry / 'alias.json'} is not a regular file"),
              f"the link's own name is refused: {resolved('alias')}")
        check(registered(tenant) == registry / "pgu.json", "and a real registration of `pgu` is not blocked by it")
        check(resolved("pgu") == tenant.resolve(), "which then resolves, the link still not followed")
        (registry / "pgu.json").unlink()
        (registry / "alias.json").unlink()

        # A registry that contradicts itself -- a valid record for `pgu` under another name beside an unusable `pgu.json`
        # -- offers `pgu` nowhere, and refuses it.
        (registry / "other.json").write_text(json.dumps({"schema": launcher.SWITCHYARD_REGISTRY_SCHEMA, "slug": "pgu",
                                                         "name": "pgu", "config_path": str(tenant)}))
        (registry / "pgu.json").write_text("{")
        check(all(entry.slug != "pgu" for entry in launcher._switchyard_entries()),
              "a slug the registry both registers and claims is not listed")
        check(str(resolved("pgu")).startswith("refused: switchyard: project 'pgu' is registered, but"), "and is refused")
        (registry / "other.json").unlink()
        (registry / "pgu.json").unlink()
    finally:
        if saved_env is None:
            os.environ.pop(launcher.SWITCHYARD_REGISTRY_DIR_ENV, None)
        else:
            os.environ[launcher.SWITCHYARD_REGISTRY_DIR_ENV] = saved_env
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"registry_authoritative_resolution_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
