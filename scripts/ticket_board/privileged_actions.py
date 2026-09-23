#!/usr/bin/env python3
"""The catalogue of privileged operations, and what may be said to them.

Every privileged Switchyard operation is one of a fixed, named set. A caller
picks an action by name and supplies typed values; it never supplies a program,
a path, a command string, an environment, or an extra argument. polkit is asked
only whether one installed, root-owned helper may run a named action -- there is
nothing in the authorization surface for a caller to aim somewhere else.

That shape is the point. The failure on SYRD-102 was reached through a generic
`pkexec` invocation, and the fallback was another sudo command handed through
chat: both are surfaces where the *command* is the payload. Here the payload is
a name from this table plus values that have to parse.

Two things are deliberately not configurable by a caller:

* **What runs.** The helper for an action is fixed in this table.
* **Whether a human is asked.** Each action declares it here, and it is
  rendered into the installed policy. A caller cannot ask for a weaker one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

ACTION_NAMESPACE = "org.switchyard.privileged"
POLICY_VENDOR = "Switchyard"
POLICY_VENDOR_URL = "https://github.com/ebudai/switchyard"

#: Pre-authorized for an active local session of the project account. The
#: helper still proves the caller is the registered control pane, so this is
#: "no human is asked", not "anybody may".
ALLOW_ACTIVE = "yes"
#: A human must authenticate as an administrator. Legitimate for an action
#: whose blast radius is the host rather than one tenant -- but it must never
#: become a silent wait, which is what the pre-flight in `polkit_preflight` is
#: for.
AUTH_ADMIN = "auth_admin"


class ArgumentError(ValueError):
    """A supplied value is not of the declared type. Never partially applied."""


def _slug(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value or ""):
        raise ArgumentError(
            f"project must be a slug of lowercase letters, digits and hyphens: {value!r}"
        )
    return value


def _commit(value: str) -> str:
    """A full, content-addressed commit. Not a ref, not an abbreviation.

    A ref names whatever it points at when it is read, which is not the thing
    an operator approved; an abbreviation can become ambiguous in a repository
    that has grown since. The point of pinning is that the approved bytes and
    the deployed bytes are the same bytes.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", value or ""):
        raise ArgumentError(
            f"commit must be a full 40-character content-addressed sha: {value!r}"
        )
    return value


@dataclass(frozen=True)
class PrivilegedAction:
    """One catalogued operation: its name, its policy, and its arguments."""

    name: str
    summary: str
    #: What the person authenticating is told they are allowing.
    message: str
    #: `ALLOW_ACTIVE` or `AUTH_ADMIN`, rendered into the installed policy.
    authentication: str
    #: Ordered argument names and their validators. Nothing else is accepted.
    arguments: tuple[tuple[str, Callable[[str], str]], ...]

    @property
    def action_id(self) -> str:
        return f"{ACTION_NAMESPACE}.{self.name}"

    def validate(self, values: Mapping[str, str]) -> dict[str, str]:
        """The typed values for this action, or raise. All or nothing.

        Refuses anything not declared, which is what stops an extra argument
        riding along -- an environment assignment, a second path, a flag the
        helper might otherwise pass on.
        """
        declared = {name for name, _ in self.arguments}
        extra = sorted(set(values) - declared)
        if extra:
            raise ArgumentError(
                f"{self.name} takes {sorted(declared) or 'no arguments'}; "
                f"refusing unexpected {extra}"
            )
        validated: dict[str, str] = {}
        for name, validate in self.arguments:
            if name not in values:
                raise ArgumentError(f"{self.name} requires {name}")
            raw = values[name]
            if not isinstance(raw, str):
                raise ArgumentError(f"{name} must be text: {raw!r}")
            validated[name] = validate(raw)
        return validated


CATALOGUE: tuple[PrivilegedAction, ...] = (
    PrivilegedAction(
        name="deploy-release",
        summary="Deploy an exact, already-approved release to a tenant's board",
        message="Authentication is required to deploy an approved Switchyard release",
        # The account already owns the tenant, the helper proves the caller is
        # its registered control pane, and the release is pinned to a commit an
        # operator approved. Asking a human here is what produced the silent
        # three-minute wait on SYRD-102 with nobody to answer.
        authentication=ALLOW_ACTIVE,
        arguments=(("project", _slug), ("commit", _commit)),
    ),
    PrivilegedAction(
        name="upgrade-tenant",
        summary="Run the privileged phases of a tenant upgrade",
        message="Authentication is required to upgrade a Switchyard tenant",
        authentication=ALLOW_ACTIVE,
        arguments=(("project", _slug),),
    ),
    PrivilegedAction(
        name="install-shared-release",
        summary="Make an already-built shared release this host's current one",
        message="Authentication is required to install a shared Switchyard release",
        # Host-wide rather than tenant-scoped: this one changes what every
        # tenant on the machine will run, so a human authenticates for it.
        #
        # A commit and nothing else. The release it names is resolved against
        # root's own cache and its marker is checked, so the caller contributes
        # forty hex characters and no path. See
        # `privileged_operations.trusted_release_root`, and
        # `UNCATALOGUED_BY_DESIGN` for the half that stays an operator packet.
        authentication=AUTH_ADMIN,
        arguments=(("commit", _commit),),
    ),
    PrivilegedAction(
        name="select-shared-release",
        summary="Run a tenant from an already-installed shared release, by commit",
        message="Authentication is required to select a Switchyard release for a tenant",
        # This one chooses which Switchyard code root itself will execute for
        # this tenant from now on, so a human authenticates for it even though
        # the caller is already proven to be the control pane.
        authentication=AUTH_ADMIN,
        arguments=(("project", _slug), ("commit", _commit)),
    ),
    PrivilegedAction(
        name="repair-boundary",
        summary="Reapply a tenant's reviewed repository boundary",
        message="Authentication is required to repair a Switchyard tenant's boundary",
        authentication=ALLOW_ACTIVE,
        arguments=(("project", _slug),),
    ),
)

#: Deliberately NOT catalogued: BUILDING a new shared release from source.
#:
#: That operation cannot be expressed here without breaking the rule this file
#: exists to enforce. `shared_release_install_commands` has the operator, as
#: themselves, produce a bundle from their own repository, which root then
#: fetches into a repository root creates before demanding the exact commit
#: inside it. Root is given no access to the operator's checkout at all, and
#: that is the whole safety argument (SYRD-97 review): a full sha does not make
#: content immutable, because `refs/replace` and repository config can both
#: redirect what root would execute.
#:
#: A catalogued action for it would therefore have to take the operator's
#: checkout as an argument -- a caller-supplied path to a tree root would then
#: read -- which is exactly the payload-shaped surface this boundary removes.
#: What IS commit-addressable is everything after the build: once a release
#: exists under root's own releases directory, naming it needs nothing but its
#: commit. That is `install-shared-release` and `select-shared-release` above,
#: both resolved through `privileged_operations.trusted_release_root`. Only the
#: build itself stays an operator packet, and saying so is better than
#: cataloguing a path argument and calling the surface bounded.
UNCATALOGUED_BY_DESIGN: tuple[tuple[str, str], ...] = (
    (
        "build-shared-release",
        "requires the operator's own unprivileged checkout as a source, which no "
        "catalogued action may take; see shared_release_install_commands",
    ),
)

BY_NAME: dict[str, PrivilegedAction] = {action.name: action for action in CATALOGUE}


def action_for(name: str) -> PrivilegedAction:
    try:
        return BY_NAME[name]
    except KeyError:
        known = ", ".join(sorted(BY_NAME))
        raise ArgumentError(f"{name!r} is not a catalogued action; known actions: {known}") from None


def _xml_text(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_policy(helper_path: str, actions: Sequence[PrivilegedAction] = CATALOGUE) -> str:
    """The installed polkit action catalogue.

    `annotate org.freedesktop.policykit.exec.path` is what binds each action to
    one fixed, root-owned program. There is no substitution in it and nothing a
    caller contributes: the only thing a caller chooses is which of these
    actions to ask for.
    """
    entries = []
    for action in actions:
        entries.append(
            f"""  <action id="{_xml_text(action.action_id)}">
    <description>{_xml_text(action.summary)}</description>
    <message>{_xml_text(action.message)}</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>{_xml_text(action.authentication)}</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">{_xml_text(helper_path)}</annotate>
    <annotate key="org.freedesktop.policykit.exec.argv1">{_xml_text(action.name)}</annotate>
  </action>"""
        )
    body = "\n".join(entries)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC
 "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<!--
  Switchyard's bounded privileged operations (SYRD-112).

  Every action below runs ONE fixed, root-owned helper, named by exec.path.
  A caller chooses an action name and supplies typed values; it never supplies
  a program, a path, a command string or an environment. Adding a generic
  "run this" action here would undo the whole point of the file.

  allow_active is per action and is decided in the catalogue, not by the
  caller. An action that is pre-authorized is still not unguarded: the helper
  proves the caller is the tenant's registered control pane before it does
  anything, so a sibling role sharing the same account is refused.
-->
<policyconfig>
  <vendor>{_xml_text(POLICY_VENDOR)}</vendor>
  <vendor_url>{_xml_text(POLICY_VENDOR_URL)}</vendor_url>
{body}
</policyconfig>
"""
