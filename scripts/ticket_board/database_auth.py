#!/usr/bin/env python3
"""A cluster that trusts its local connections, rendered as one that does not (SYRD-706).

`initdb` with no authentication method writes `trust` for every local socket and loopback TCP connection. On such a
cluster every account on the host connects as any role it names, the `postgres` superuser included, to every
tenant's board database. This renders the replacement from the files the cluster reads:

- every `trust` becomes `peer` on the socket -- the OS account is the role, or what an ident map lets it be -- and
  `scram-sha-256` over TCP, where a role connects only with its password; nothing else in the file changes;
- each tenant's notify listener, which runs as the tenant's owner and connects as `ticket_board_listener`, gets a
  rule of its own before the catch-all: its own database only, through its own ident map
  (`project_provision.listener_ident_map`), so an owner reaches its own board as the listener and nothing else;
- a line whose method cannot be found with certainty is a refusal, never a guess.

Pure: it reads text and returns text. Root's transition installs it, checks the cluster's own reading of the result
(`pg_hba_file_rules`) before reloading, probes the accounts after, and puts the previous files back on any failure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

LISTENER_ROLE = "ticket_board_listener"
MARKER = "# SYRD-706: was trust"
LISTENER_MARKER = "# SYRD-706: each tenant's notify listener, as its owner, on its own board only"
#: What replaces `trust`, by connection type.
REPLACEMENT = {
    "local": "peer",
    "host": "scram-sha-256",
    "hostssl": "scram-sha-256",
    "hostnossl": "scram-sha-256",
    "hostgssenc": "scram-sha-256",
    "hostnogssenc": "scram-sha-256",
}
#: The address keywords a host rule may name instead of an address.
ADDRESS_KEYWORDS = {"all", "samehost", "samenet"}
METHODS = {"trust", "reject", "scram-sha-256", "md5", "password", "gss", "sspi", "ident", "peer", "pam", "ldap",
           "radius", "cert", "oauth"}
PLAIN = re.compile(r"[a-z_][a-z0-9_]{0,62}")
ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
PROJECT = re.compile(r"[a-z][a-z0-9-]{0,31}")


class HbaError(Exception):
    """The file cannot be rendered with certainty; the message names the line."""


@dataclass(frozen=True)
class Tenant:
    """A tenant board, as root's own records name it."""

    project: str
    owner: str
    database: str

    def __post_init__(self) -> None:
        if not PROJECT.fullmatch(self.project):
            raise HbaError(f"{self.project!r} is not a project")
        if not ACCOUNT.fullmatch(self.owner) or self.owner in ("root", "postgres"):
            raise HbaError(f"{self.owner!r} is not a tenant owner account")
        if not PLAIN.fullmatch(self.database):
            raise HbaError(f"{self.database!r} is not a plain database name")

    @property
    def ident_map(self) -> str:
        return f"{self.project.replace('-', '_')}_ticket_board_listener"

    @property
    def hba_line(self) -> str:
        return f"local   {self.database}   {LISTENER_ROLE}   peer map={self.ident_map}"

    @property
    def ident_line(self) -> str:
        return f"{self.ident_map}   {self.owner}   {LISTENER_ROLE}"


def _method_index(fields: list[str], number: int) -> int:
    kind = fields[0]
    if kind == "local":
        index = 3
    elif kind in REPLACEMENT:
        if len(fields) < 5:
            raise HbaError(f"pg_hba line {number} is a {kind} rule with too few fields")
        address = fields[3]
        # An address with a CIDR suffix, a keyword or a host name is one field; a bare IP is followed by its mask.
        bare_ip = re.fullmatch(r"[0-9.]+|[0-9a-fA-F:]+", address) is not None and "/" not in address
        index = 5 if bare_ip and address not in ADDRESS_KEYWORDS else 4
    else:
        raise HbaError(f"pg_hba line {number} has a connection type this does not know: {kind!r}")
    if len(fields) <= index or fields[index] not in METHODS:
        raise HbaError(f"pg_hba line {number}'s authentication method cannot be found with certainty")
    return index


def _rules(text: str):
    """(index, fields) for each rule line; include directives and comments are not rules."""
    for index, line in enumerate(text.splitlines()):
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        if '"' in body:
            raise HbaError(f"pg_hba line {index + 1} quotes a field; render it by hand")
        fields = body.split("#", 1)[0].split()
        if fields[0].startswith("include"):
            continue
        yield index, fields


def harden_hba(text: str, tenants: Sequence[Tenant]) -> str:
    """`text` with no `trust` left, and each tenant's listener rule before the local catch-all."""
    lines = text.splitlines()
    catchall: int | None = None
    present = set()
    for index, fields in _rules(text):
        method = _method_index(fields, index + 1)
        if fields[method] == "trust":
            if fields.count("trust") != 1:
                raise HbaError(f"pg_hba line {index + 1} names trust more than once")
            # The method's own field is replaced where it stands, so spacing and options are kept.
            lines[index] = re.sub(r"(?<!\S)trust(?!\S)", REPLACEMENT[fields[0]], lines[index], count=1)
            lines[index] = f"{lines[index]}  {MARKER}"
        if catchall is None and fields[0] == "local" and fields[1] == "all" and fields[2] == "all":
            catchall = index
        present.add(" ".join(fields))
    block = [tenant.hba_line for tenant in tenants if " ".join(tenant.hba_line.split()) not in present]
    if block:
        at = catchall if catchall is not None else len(lines)
        lines[at:at] = [LISTENER_MARKER, *block]
    return "\n".join(lines) + "\n"


def harden_ident(text: str, tenants: Sequence[Tenant]) -> str:
    """`text` with each tenant's listener mapping, once."""
    lines = text.splitlines()
    present = {" ".join(line.split()) for line in lines if line.strip() and not line.lstrip().startswith("#")}
    missing = [tenant.ident_line for tenant in tenants if " ".join(tenant.ident_line.split()) not in present]
    if missing:
        lines += [LISTENER_MARKER, *missing]
    return "\n".join(lines) + "\n"


def remaining_trust(rules: Sequence[dict]) -> list[str]:
    """From `pg_hba_file_rules` rows: every rule still trusted, and every line the cluster cannot read."""
    problems = []
    for rule in rules:
        where = f"{rule.get('file_name') or 'pg_hba.conf'}:{rule.get('line_number')}"
        if rule.get("error"):
            problems.append(f"{where} does not parse: {rule['error']}")
        elif rule.get("auth_method") == "trust":
            problems.append(f"{where} still trusts {rule.get('type')} connections")
    return problems
