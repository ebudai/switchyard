"""Whether this host could be diagnosed after an incident, read-only.

The 2026-10-04 crash could not be dated afterwards: the system journal had
been rotated down to a few tens of megabytes and stopped minutes before the
power-off, a crash loop had left about 930 MB of PostgreSQL core dumps with no
cap on them, and `kernel.sysrq = 16` allowed a sync but not the read-only
remount and reboot that would have kept the writes the hard power-off lost
(SYRD-553).

These settings are the operator's, host-wide, so nothing here changes them.

It also names a tenant whose tmux server can give its panes real-time scheduling or a negative nice (SYRD-895): a
server started before the launcher dropped the launching user's limits keeps them for every pane it opens, until every
role of that tenant stops. Those findings are `kind="isolation"` and are reported under their own heading.
`host_diagnostics_findings` reads the effective configuration and returns one
finding per setting that falls short, each with the exact drop-in or sysctl
file an operator can install and the command that applies it.
`host_diagnostics_report` is what `switchyard install-shared-release` prints.

Everything is read under `root`, so a suite can point it at a fixture tree.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from scripts.tenant_limits import process_scheduling, too_permissive

MIB = 1024 * 1024
GIB = 1024 * MIB

#: The journal a host running tenants should keep, at least: one gigabyte, the
#: size the 2026-10-04 review recommended, and 128 MiB for every registered
#: tenant past eight, whose board, listener and roles all log there.
JOURNAL_FLOOR = 1 * GIB
JOURNAL_PER_TENANT = 128 * MIB
#: systemd's own default for SystemMaxUse= when nothing sets it.
JOURNAL_DEFAULT_SHARE = 0.10
JOURNAL_DEFAULT_CEILING = 4 * GIB

#: Core dumps: an explicit cap no larger than this, and no more than this on disk.
COREDUMP_CAP = 1 * GIB
COREDUMP_ON_DISK = 512 * MIB

#: The SysRq functions that turn a hard power-off into a clean one: sync (16),
#: remount read-only (32) and reboot (128). `1` enables every function.
SYSRQ_NEEDED = 16 | 32 | 128
SYSRQ_RECOMMENDED = 244

JOURNALD_DROP_IN = "/etc/systemd/journald.conf.d/50-switchyard.conf"
COREDUMP_DROP_IN = "/etc/systemd/coredump.conf.d/50-switchyard.conf"
SYSRQ_DROP_IN = "/etc/sysctl.d/50-switchyard-sysrq.conf"

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTPE]?)\s*$", re.IGNORECASE)
_UNITS = {"": 1, "K": 1024, "M": MIB, "G": GIB, "T": 1024 * GIB, "P": 1024 ** 5, "E": 1024 ** 6}


@dataclass(frozen=True)
class HostFinding:
    """One host setting that would make an incident hard to diagnose, and its remedy."""

    item: str
    detail: str
    remedy_path: str
    remedy_text: str
    apply_command: str
    #: "diagnosis" (SYRD-553's settings), or "isolation": a tenant that can starve the others (SYRD-895).
    kind: str = "diagnosis"


def _size(value: str) -> int | None:
    """A systemd size (`50M`, `1G`, `4096`) in bytes, or None if it is not one."""
    match = _SIZE.match(value or "")
    if not match:
        return None
    return int(float(match.group(1)) * _UNITS[match.group(2).upper()])


def _human(size: int) -> str:
    for unit, scale in (("G", GIB), ("M", MIB)):
        if size >= scale:
            return f"{size / scale:.1f}{unit}".replace(".0", "")
    return f"{size}B"


def _systemd_config(root: Path, name: str, section: str) -> dict[str, str]:
    """`name`'s `[section]` as systemd merges it: main file, then drop-ins by file name.

    The main file is the first of /etc, /run and /usr/lib that has it. Drop-ins
    are collected from `<dir>/<name>.d/` in /usr/lib, /run and /etc, a file in a
    later directory replacing one of the same name, and applied in file-name
    order, so a later one wins for each key.
    """
    base = Path(name)
    main = next(
        (path for path in (root / "etc" / base, root / "run" / base, root / "usr/lib" / base) if path.is_file()),
        None,
    )
    drop_ins: dict[str, Path] = {}
    for directory in ("usr/lib", "run", "etc"):
        folder = root / directory / f"{name}.d"
        try:
            for path in folder.iterdir():
                if path.suffix == ".conf" and path.is_file():
                    drop_ins[path.name] = path
        except OSError:
            continue
    values: dict[str, str] = {}
    for path in ([main] if main else []) + [drop_ins[key] for key in sorted(drop_ins)]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        current = ""
        for line in lines:
            text = line.strip()
            if not text or text[0] in "#;":
                continue
            if text.startswith("[") and text.endswith("]"):
                current = text[1:-1].strip()
                continue
            if current == section and "=" in text:
                key, _, value = text.partition("=")
                values[key.strip()] = value.strip()
    return values


def _filesystem_size(path: Path) -> int | None:
    """The size of the filesystem holding `path`, or its nearest existing parent."""
    for candidate in (path, *path.parents):
        try:
            info = os.statvfs(candidate)
        except OSError:
            continue
        return info.f_blocks * info.f_frsize
    return None


def _tenant_count(root: Path) -> int:
    try:
        return sum(1 for path in (root / "etc/switchyard/projects").iterdir() if path.suffix == ".json")
    except OSError:
        return 0


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _dump_bytes(directory: Path) -> int:
    total = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            info = entry.lstat()
        except OSError:
            continue
        if not entry.is_symlink() and entry.is_file():
            total += info.st_size
    return total


def journal_needed(tenants: int) -> int:
    """The journal size a host with this many tenants should keep, rounded up to a whole GiB."""
    needed = max(JOURNAL_FLOOR, JOURNAL_PER_TENANT * tenants)
    return -(-needed // GIB) * GIB


def _journal_findings(root: Path) -> list[HostFinding]:
    config = _systemd_config(root, "systemd/journald.conf", "Journal")
    tenants = _tenant_count(root)
    needed = journal_needed(tenants)
    storage = config.get("Storage", "auto").strip().lower() or "auto"
    persistent = storage == "persistent" or (storage == "auto" and (root / "var/log/journal").is_dir())
    configured = _size(config.get("SystemMaxUse", ""))
    if configured is not None:
        effective, how = configured, f"SystemMaxUse={config['SystemMaxUse']}"
    else:
        size = _filesystem_size(root / "var/log/journal") or 0
        effective = min(int(size * JOURNAL_DEFAULT_SHARE), JOURNAL_DEFAULT_CEILING)
        how = f"systemd's default of 10% of its filesystem, about {_human(effective)}"
    problems: list[str] = []
    if not persistent:
        problems.append(
            f"the journal is not kept on disk (Storage={storage}"
            + ("" if storage != "auto" else ", and /var/log/journal does not exist")
            + "), so it is lost at every reboot -- including the one after a crash"
        )
    if effective < needed:
        problems.append(
            f"it may use {_human(effective)} ({how}), and a host with {tenants} registered "
            f"tenant(s) should keep at least {_human(needed)}: the logs that date an incident can be "
            "rotated away before anyone reads them"
        )
    if not problems:
        return []
    lines = ["[Journal]"]
    if not persistent:
        lines.append("Storage=persistent")
    lines.append(f"SystemMaxUse={_human(needed)}")
    return [HostFinding(
        item="journald retention",
        detail="; ".join(problems),
        remedy_path=JOURNALD_DROP_IN,
        remedy_text="\n".join(lines) + "\n",
        apply_command="sudo systemctl restart systemd-journald",
    )]


def _coredump_findings(root: Path) -> list[HostFinding]:
    pattern = _read(root / "proc/sys/kernel/core_pattern")
    if "systemd-coredump" not in pattern:
        # The kernel does not hand cores to systemd-coredump, so its settings
        # and its store are not where cores go on this host.
        return []
    config = _systemd_config(root, "systemd/coredump.conf", "Coredump")
    if config.get("Storage", "external").strip().lower() == "none":
        return []
    problems: list[str] = []
    cap = _size(config.get("MaxUse", ""))
    if cap is None:
        problems.append(
            "no MaxUse= caps their store, so a crash loop can fill it with one core per restart "
            "(10% of the filesystem by default)"
        )
    elif cap > COREDUMP_CAP:
        problems.append(f"MaxUse={config['MaxUse']} lets them take more than {_human(COREDUMP_CAP)}")
    store = root / "var/lib/systemd/coredump"
    on_disk = _dump_bytes(store)
    if on_disk > COREDUMP_ON_DISK:
        problems.append(
            f"{_human(on_disk)} of core dumps are already in /var/lib/systemd/coredump; "
            "`coredumpctl list` shows what they are, and the ones no longer needed can be removed"
        )
    if not problems:
        return []
    return [HostFinding(
        item="core dumps",
        detail="; ".join(problems),
        remedy_path=COREDUMP_DROP_IN,
        remedy_text=f"[Coredump]\nMaxUse={_human(COREDUMP_CAP)}\n",
        apply_command="sudo systemctl daemon-reload",
    )]


def _sysrq_findings(root: Path) -> list[HostFinding]:
    raw = _read(root / "proc/sys/kernel/sysrq")
    try:
        value = int(raw)
    except ValueError:
        return []
    if value == 1 or value & SYSRQ_NEEDED == SYSRQ_NEEDED:
        return []
    return [HostFinding(
        item="Magic SysRq",
        detail=(
            f"kernel.sysrq is {value}, which does not allow sync, read-only remount and reboot "
            "together: a hung host can only be powered off hard, and the writes still in memory "
            "are lost. With them, Alt+SysRq+S, U, B syncs, remounts read-only and reboots"
        ),
        remedy_path=SYSRQ_DROP_IN,
        remedy_text=f"kernel.sysrq = {SYSRQ_RECOMMENDED}\n",
        apply_command=f"sudo sysctl -p {SYSRQ_DROP_IN}",
    )]


def _tenant_owners(root: Path) -> dict[int, str]:
    """uid -> slug, for every registered tenant: its configuration file belongs to its owner."""
    owners: dict[int, str] = {}
    for entry in sorted((root / "etc/switchyard/projects").glob("*.json")):
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
            config = root / str(record.get("config_path") or "").lstrip("/")
            if str(record.get("config_path") or "").startswith("/"):
                owners.setdefault(config.stat().st_uid, str(record.get("slug") or entry.stem))
        except (OSError, ValueError, AttributeError):
            continue
    return owners


def _tenant_scheduling_findings(root: Path) -> list[HostFinding]:
    owners = _tenant_owners(root)
    if not owners:
        return []
    servers: dict[str, list[tuple[int, list[str]]]] = {}
    proc = root / "proc"
    for entry in proc.glob("[0-9]*"):
        status = _read(entry / "status")
        uid = next((line.split()[1] for line in status.splitlines() if line.startswith("Uid:")), "")
        if _read(entry / "comm") != "tmux: server" or not uid.isdigit() or int(uid) not in owners:
            continue
        limits = process_scheduling(int(entry.name), proc)
        problems = too_permissive(limits) if limits else []
        if problems:
            servers.setdefault(owners[int(uid)], []).append((int(entry.name), problems))
    findings = []
    for slug, found in sorted(servers.items()):
        pids = ", ".join(str(pid) for pid, _ in sorted(found))
        findings.append(HostFinding(
            item=f"{slug}'s panes",
            detail=(f"its tmux server (pid {pids}) gives every pane it opens {' and '.join(sorted(found)[0][1])}, "
                    "inherited from whoever started it: any process in a pane could take real-time scheduling and "
                    "starve every tenant on this host. A server started by this release's launcher drops them"),
            remedy_path="", remedy_text="",
            apply_command=f"switchyard stop {slug}, then switchyard start {slug} (its tmux server exits once every "
                          "role has stopped, and the next start runs a new one)",
            kind="isolation",
        ))
    return findings


def host_diagnostics_findings(root: Path = Path("/")) -> list[HostFinding]:
    """Every setting on this host that would make an incident hard to diagnose, and every tenant whose panes can take
    real-time scheduling (SYRD-895). Reads only."""
    return [*_journal_findings(root), *_coredump_findings(root), *_sysrq_findings(root),
            *_tenant_scheduling_findings(root)]


def host_diagnostics_report(findings: list[HostFinding]) -> list[str]:
    """What an operator reads: each finding, the file that fixes it, and the command that applies it."""
    lines: list[str] = []
    diagnosis = [finding for finding in findings if finding.kind != "isolation"]
    isolation = [finding for finding in findings if finding.kind == "isolation"]
    if diagnosis:
        lines.append(
            "switchyard: host readiness: this host would be hard to diagnose after an incident. "
            "These are the operator's settings and nothing here changes them:"
        )
    for finding in diagnosis:
        lines.append(f"switchyard:   {finding.item}: {finding.detail}.")
        lines.append(f"switchyard:     to change it, write {finding.remedy_path} as root:")
        lines.extend(f"switchyard:       {line}" for line in finding.remedy_text.splitlines())
        lines.append(f"switchyard:     then: {finding.apply_command}")
    if isolation:
        lines.append("switchyard: tenant readiness: these tenants' panes can take real-time scheduling or a negative "
                     "nice, and nothing here stops their running panes (SYRD-895):")
    for finding in isolation:
        lines.append(f"switchyard:   {finding.item}: {finding.detail}.")
        lines.append(f"switchyard:     to change it: {finding.apply_command}")
    return lines
