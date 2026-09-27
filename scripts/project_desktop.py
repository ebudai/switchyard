"""Making a project's desktop policy real: verified at every launch, installed and recorded at `new` and `upgrade`.

- `prepare_project_desktop` validates the policy and, for Wayland, verifies it
  as the tenant's own uid -- installing first only when asked -- then gives
  every role the verified desktop environment in place of whatever it carried
  (`DESKTOP_ENV_KEYS`).
- `configure_project_desktop` installs a new policy, prepares the project on
  it, and only then records it; anything that fails undoes the install and
  puts the previous policy back.

The JSON reader and writer, the owner command and the current user stay in
`scripts/team_launcher.py` and are read there when a function runs; the
desktop-access helpers are each function's own import. The suites patch both
entry points on the launcher, and every caller -- here, in the launcher and in
the modules already moved out -- reaches them there.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-327). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig



DESKTOP_ENV_KEYS = ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def prepare_project_desktop(config: ProjectConfig, *, install: bool = False,
                            helper: Path | None = None,
                            runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run) -> ProjectConfig:
    from scripts import team_launcher as launcher

    from scripts import desktop_access as desktop
    tenant = config.run_as_user or launcher.current_user_name()
    try:
        policy = desktop.validate_policy(config.desktop_access, project=config.project, tenant=tenant)
        env: dict[str, str] = {}
        if policy["mode"] == "wayland":
            helper = helper or Path(__file__).resolve().with_name("desktop_access.py")
            if install:
                desktop.install(policy, helper=helper)
            # Invoke the verifier with the tenant's actual uid, never a GUI runtime
            # masquerading as the tenant. The protected GUI receipt is authoritative.
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as stream:
                json.dump(policy, stream)
                stream.flush()
                os.chmod(stream.name, 0o644)
                args = [sys.executable, str(helper), "verify", stream.name]
                result = runner(launcher._owner_command_args(tenant, args), text=True, capture_output=True)
            if result.returncode:
                raise desktop.DesktopAccessError(str(result.stderr).strip())
            env = json.loads(result.stdout)
        roles = [replace(role,
                         env={**{k:v for k,v in role.env.items() if k not in DESKTOP_ENV_KEYS}, **env},
                         unset_env=DESKTOP_ENV_KEYS) for role in config.roles]
        return replace(config, desktop_access=policy, roles=roles)
    except (desktop.DesktopAccessError, OSError, ValueError) as exc:
        raise SystemExit(f"switchyard: desktop setup incomplete before launch: {exc}") from exc


def configure_project_desktop(config: ProjectConfig, *, config_path: Path,
                              policy_path: Path | None = None, dry_run: bool = False,
                              helper: Path | None = None,
                              runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run) -> ProjectConfig:
    from scripts import team_launcher as launcher

    from scripts import desktop_access as desktop
    raw = ({"mode": "headless"} if str(policy_path) == "headless" else launcher._load_json(policy_path)) if policy_path else config.desktop_access
    try:
        policy = desktop.validate_policy(raw, project=config.project, tenant=config.run_as_user or launcher.current_user_name())
    except desktop.DesktopAccessError as exc:
        raise SystemExit(f"switchyard: {exc}") from exc
    if dry_run:
        print(f"switchyard: desktop policy for {config.project}: {json.dumps(policy, sort_keys=True)}; no access changed")
        return config
    # Complete privileged setup and tenant readiness before persisting the launch
    # choice; failures leave the previous project config intact.
    previous = (desktop.validate_policy(config.desktop_access, project=config.project, tenant=config.run_as_user or launcher.current_user_name())
                if config.desktop_access is not None else None)
    if previous and previous.get("mode") == "wayland" and previous != policy:
        desktop.uninstall(previous)
    installed_new = False
    try:
        if policy["mode"] == "wayland":
            desktop.install(policy, helper=helper or Path(__file__).resolve().with_name("desktop_access.py"))
            installed_new = previous != policy
        configured = launcher.prepare_project_desktop(replace(config, desktop_access=policy), runner=runner)
        payload = launcher._load_json(config_path)
        payload["desktop_access"] = policy
        owner_user = config.run_as_user or launcher.current_user_name()
        launcher._write_json_atomic(config_path, payload, owner_user=owner_user)
        launcher._write_json_atomic(config_path.parent / "desktop-policy.json", policy, owner_user=owner_user)
        return configured
    except (Exception, SystemExit) as exc:
        if installed_new:
            desktop.uninstall(policy)
        if previous and previous.get("mode") == "wayland" and previous != policy:
            desktop.install(previous, helper=helper or Path(__file__).resolve().with_name("desktop_access.py"))
        if isinstance(exc, desktop.DesktopAccessError):
            # Said, not dumped. A privileged install that cannot reach the GUI
            # session is an operator's problem to act on, and a traceback out
            # of `switchyard upgrade` reads as a crash in the middle of it --
            # which, since this runs before any phase, it is not (SYRD-232).
            raise SystemExit(
                f"switchyard: {config.project}'s desktop policy could not be installed: {exc}. "
                "Nothing was recorded and the previous policy is back in place; the upgrade "
                "stopped before any phase, so no board, listener or role session was touched."
            ) from exc
        raise
