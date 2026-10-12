#!/usr/bin/env python3
"""SYRD-553: a host that could not be diagnosed after an incident is told so, and nothing is changed.

The 2026-10-04 crash left a journal capped at tens of megabytes, about 930 MB
of uncapped PostgreSQL core dumps and `kernel.sysrq = 16`. The check reads the
effective configuration under a root it is given -- a fixture tree here -- and
returns one finding per setting that falls short, each with the drop-in or
sysctl file that fixes it. Each remedy is validated by the real tool:
`systemd-analyze cat-config` sees the drop-in, and `sysctl --dry-run -p` parses
the sysctl file without writing. An audit hook proves the check writes, moves,
removes and runs nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import host_diagnostics as hd  # noqa: E402

CHECKS = 0
CORE_PATTERN = "|/usr/lib/systemd/systemd-coredump %P %u %g %s %t %c %h %d\n"


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def host(root: Path, *, journald: str | None = "[Journal]\nSystemMaxUse=50M\n", coredump: str | None = None,
         sysrq: str = "16", dumps: int = 930 * hd.MIB, tenants: int = 3, journal_dir: bool = True,
         core_pattern: str = CORE_PATTERN) -> Path:
    """A host tree: the crash's settings by default."""
    if journald is not None:
        write(root, "etc/systemd/journald.conf", journald)
    if coredump is not None:
        write(root, "etc/systemd/coredump.conf", coredump)
    write(root, "proc/sys/kernel/sysrq", sysrq + "\n")
    write(root, "proc/sys/kernel/core_pattern", core_pattern)
    store = root / "var/lib/systemd/coredump"
    store.mkdir(parents=True)
    if dumps:
        with open(store / "core.postgres.1000.zst", "wb") as handle:
            handle.truncate(dumps)
    if journal_dir:
        (root / "var/log/journal").mkdir(parents=True)
    for index in range(tenants):
        write(root, f"etc/switchyard/projects/t{index}.json", "{}\n")
    return root


def by_item(findings) -> dict[str, hd.HostFinding]:
    return {finding.item: finding for finding in findings}


def test_the_crash_hosts_settings_are_one_finding_each() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd553-bad.") as raw:
        findings = hd.host_diagnostics_findings(host(Path(raw)))
    items = by_item(findings)
    check(sorted(items) == ["Magic SysRq", "core dumps", "journald retention"] and len(findings) == 3,
          f"one finding per item: {[f.item for f in findings]}")
    journal, dumps, sysrq = items["journald retention"], items["core dumps"], items["Magic SysRq"]
    check("50M" in journal.detail and "at least 1G" in journal.detail and "SystemMaxUse=1G" in journal.remedy_text
          and journal.remedy_path == "/etc/systemd/journald.conf.d/50-switchyard.conf",
          f"the journal: its cap, the size it should be, and a drop-in that sets it: {journal}")
    check("no MaxUse=" in dumps.detail and "930M" in dumps.detail and dumps.remedy_text == "[Coredump]\nMaxUse=1G\n",
          f"core dumps: no cap, what is already there, and a drop-in that caps them: {dumps}")
    check("kernel.sysrq is 16" in sysrq.detail and sysrq.remedy_text == "kernel.sysrq = 244\n"
          and sysrq.apply_command == "sudo sysctl -p /etc/sysctl.d/50-switchyard-sysrq.conf",
          f"SysRq: the value, why it matters, and the sysctl file: {sysrq}")


def test_each_remedy_parses_with_the_real_tool_and_clears_its_finding() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd553-remedy.") as raw:
        bad = host(Path(raw) / "bad")
        items = by_item(hd.host_diagnostics_findings(bad))
        for item, conf in (("journald retention", "systemd/journald.conf"), ("core dumps", "systemd/coredump.conf")):
            finding = items[item]
            installed = write(bad, finding.remedy_path, finding.remedy_text)
            shown = subprocess.run(["systemd-analyze", "cat-config", f"--root={bad}", conf],
                                   capture_output=True, text=True)
            check(shown.returncode == 0 and str(installed) in shown.stdout
                  and all(line in shown.stdout for line in finding.remedy_text.splitlines()),
                  f"{item}: systemd reads the drop-in where it is written: {shown.stdout[-300:]}{shown.stderr[-200:]}")
        sysctl_file = write(Path(raw), "sysrq.conf", items["Magic SysRq"].remedy_text)
        parsed = subprocess.run(["sysctl", "--dry-run", "-p", str(sysctl_file)], capture_output=True, text=True)
        check(parsed.returncode == 0 and parsed.stdout.strip() == "kernel.sysrq = 244",
              f"the sysctl file parses with `sysctl -p` (dry run): {parsed.stdout}{parsed.stderr}")
        (bad / "var/lib/systemd/coredump/core.postgres.1000.zst").unlink()
        write(bad, "proc/sys/kernel/sysrq", "244\n")
        after = hd.host_diagnostics_findings(bad)
    check(after == [], f"with every remedy in place (and the old dumps gone), nothing is found: {after}")


def test_a_host_configured_as_recommended_has_no_findings() -> None:
    for sysrq in ("244", "1", "176"):
        with tempfile.TemporaryDirectory(prefix="syrd553-good.") as raw:
            root = host(Path(raw), journald="[Journal]\n", sysrq=sysrq, dumps=10 * hd.MIB)
            write(root, hd.JOURNALD_DROP_IN, "[Journal]\nStorage=persistent\nSystemMaxUse=1G\n")
            write(root, hd.COREDUMP_DROP_IN, "[Coredump]\nMaxUse=1G\n")
            findings = hd.host_diagnostics_findings(root)
        check(findings == [] and hd.host_diagnostics_report(findings) == [],
              f"kernel.sysrq={sysrq}, a 1G journal and capped dumps: nothing to say: {findings}")


def test_the_journal_is_judged_as_systemd_merges_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd553-merge.") as raw:
        root = host(Path(raw), journald="[Journal]\nSystemMaxUse=50M\n", sysrq="244", dumps=0)
        write(root, "usr/lib/systemd/journald.conf.d/10-vendor.conf", "[Journal]\nSystemMaxUse=20M\n")
        write(root, "etc/systemd/journald.conf.d/60-local.conf", "[Journal]\nSystemMaxUse=2G\n")
        later_wins = hd.host_diagnostics_findings(root)
        write(root, "etc/systemd/journald.conf.d/60-local.conf", "[Other]\nSystemMaxUse=2G\n")
        other_section = by_item(hd.host_diagnostics_findings(root)).get("journald retention")
        write(root, "usr/lib/systemd/journald.conf.d/60-local.conf", "[Journal]\nSystemMaxUse=4G\n")
        etc_replaces_vendor = by_item(hd.host_diagnostics_findings(root)).get("journald retention")
    check([f.item for f in later_wins] == ["core dumps"], f"a later drop-in's 2G is the cap: {later_wins}")
    check(other_section is not None and "20M" in other_section.detail,
          f"a key in another section is not the journal's; the vendor drop-in's 20M is: {other_section}")
    check(etc_replaces_vendor is not None and "20M" in etc_replaces_vendor.detail,
          f"/etc's file of the same name replaces /usr/lib's, whatever it says: {etc_replaces_vendor}")


def test_a_journal_lost_at_reboot_is_a_finding() -> None:
    for journald, journal_dir, said in (("[Journal]\nStorage=volatile\nSystemMaxUse=1G\n", True, "Storage=volatile"),
                                        ("[Journal]\nSystemMaxUse=1G\n", False, "/var/log/journal does not exist")):
        with tempfile.TemporaryDirectory(prefix="syrd553-volatile.") as raw:
            root = host(Path(raw), journald=journald, journal_dir=journal_dir, sysrq="244", dumps=0)
            write(root, hd.COREDUMP_DROP_IN, "[Coredump]\nMaxUse=1G\n")
            finding = by_item(hd.host_diagnostics_findings(root)).get("journald retention")
        check(finding is not None and said in finding.detail and "Storage=persistent" in finding.remedy_text,
              f"{said}: not kept on disk, and the drop-in makes it persistent: {finding}")


def test_the_journal_threshold_follows_the_tenants() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd553-tenants.") as raw:
        root = host(Path(raw), journald="[Journal]\nSystemMaxUse=1G\n", tenants=10, sysrq="244", dumps=0)
        finding = by_item(hd.host_diagnostics_findings(root)).get("journald retention")
    check(hd.journal_needed(0) == hd.journal_needed(8) == hd.GIB and hd.journal_needed(9) == 2 * hd.GIB,
          "1G for up to eight tenants, 128 MiB more for each one after, rounded up to a whole GiB")
    check(finding is not None and "10 registered tenant(s)" in finding.detail and "SystemMaxUse=2G" in finding.remedy_text,
          f"ten tenants ask for 2G: {finding}")


def test_an_unset_cap_is_systemds_default() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd553-default.") as raw:
        root = host(Path(raw), journald="[Journal]\n", sysrq="244", dumps=0)
        size = os.statvfs(root / "var/log/journal")
        default = min(int(size.f_blocks * size.f_frsize * 0.10), 4 * hd.GIB)
        finding = by_item(hd.host_diagnostics_findings(root)).get("journald retention")
    if default < hd.GIB:
        check(finding is not None and "systemd's default of 10%" in finding.detail, f"a small default is reported: {finding}")
    else:
        check(finding is None, f"a default of {default} bytes is enough: {finding}")


def test_core_dumps_only_where_systemd_keeps_them() -> None:
    cases = {
        "the kernel writes cores elsewhere": dict(core_pattern="core\n"),
        "systemd keeps none": dict(coredump="[Coredump]\nStorage=none\n"),
    }
    for label, kw in cases.items():
        with tempfile.TemporaryDirectory(prefix="syrd553-cores.") as raw:
            root = host(Path(raw), journald="[Journal]\nSystemMaxUse=1G\n", sysrq="244", **kw)
            items = by_item(hd.host_diagnostics_findings(root))
        check("core dumps" not in items, f"{label}: no core-dump finding: {items}")
    with tempfile.TemporaryDirectory(prefix="syrd553-bigcap.") as raw:
        root = host(Path(raw), journald="[Journal]\nSystemMaxUse=1G\n", sysrq="244", dumps=0,
                    coredump="[Coredump]\nMaxUse=8G\n")
        finding = by_item(hd.host_diagnostics_findings(root)).get("core dumps")
    check(finding is not None and "MaxUse=8G" in finding.detail, f"a cap above 1G is reported: {finding}")


def test_sysrq_values() -> None:
    expected = {"16": True, "0": True, "48": True, "176": False, "244": False, "1": False, "": False, "x": False}
    for value, finds in expected.items():
        with tempfile.TemporaryDirectory(prefix="syrd553-sysrq.") as raw:
            root = host(Path(raw), journald="[Journal]\nSystemMaxUse=1G\n", sysrq=value, dumps=0)
            write(root, hd.COREDUMP_DROP_IN, "[Coredump]\nMaxUse=1G\n")
            items = by_item(hd.host_diagnostics_findings(root))
        check(("Magic SysRq" in items) == finds, f"kernel.sysrq={value!r}: finding={'Magic SysRq' in items}")


def test_the_check_changes_nothing() -> None:
    forbidden = {"os.remove", "os.unlink", "os.rename", "os.replace", "os.mkdir", "os.rmdir", "os.chmod", "os.chown",
                 "os.truncate", "os.symlink", "os.link", "os.utime", "os.system", "os.exec", "os.posix_spawn",
                 "subprocess.Popen", "shutil.rmtree", "shutil.move", "shutil.copyfile"}
    seen: list = []
    active = [False]

    def hook(event: str, args: tuple) -> None:
        if not active[0]:
            return
        if event in forbidden or event.startswith("os.exec"):
            seen.append(event)
        elif event == "open":
            mode, flags = (args[1] if len(args) > 1 else "r"), (args[2] if len(args) > 2 else 0)
            if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                seen.append(f"open {args[0]} {mode!r} {flags}")

    sys.addaudithook(hook)
    with tempfile.TemporaryDirectory(prefix="syrd553-ro.") as raw:
        root = host(Path(raw))
        before = sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*"))
        active[0] = True
        try:
            findings = hd.host_diagnostics_findings(root)
            lines = hd.host_diagnostics_report(findings)
        finally:
            active[0] = False
        after = sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*"))
    check(len(findings) == 3 and len(lines) > 3, "it found what it should")
    check(seen == [], f"and wrote, moved, removed and ran nothing: {seen}")
    check(before == after, "the host tree is byte-for-byte as it was")


def test_install_shared_release_prints_the_findings() -> None:
    from scripts import host_boundary_install, team_launcher
    from scripts.ticket_board import shared_release_activation

    finding = hd.HostFinding("Magic SysRq", "kernel.sysrq is 16", hd.SYSRQ_DROP_IN, "kernel.sysrq = 244\n",
                             f"sudo sysctl -p {hd.SYSRQ_DROP_IN}")
    saved = (team_launcher.host_diagnostics_findings, host_boundary_install.os.geteuid, shared_release_activation.read_pointer,
             shared_release_activation.recorded_rollback, shared_release_activation.activate)
    team_launcher.host_diagnostics_findings = lambda root=Path("/"): [finding]
    host_boundary_install.os.geteuid = lambda: 0
    shared_release_activation.read_pointer = lambda *a, **k: "/opt/switchyard/releases/old"
    shared_release_activation.recorded_rollback = lambda *a, **k: {}
    shared_release_activation.activate = lambda commit, **kw: SimpleNamespace(
        describe=lambda: f"activated {commit}", rolled_back=False, release_root="", commit=commit)
    try:
        dry: list[str] = []
        dry_code = team_launcher.switchyard_install_shared_release_command("c" * 40, dry_run=True, print_func=dry.append)
        real: list[str] = []
        real_code = team_launcher.switchyard_install_shared_release_command("c" * 40, print_func=real.append)
    finally:
        (team_launcher.host_diagnostics_findings, host_boundary_install.os.geteuid, shared_release_activation.read_pointer,
         shared_release_activation.recorded_rollback, shared_release_activation.activate) = saved
    for label, said, code in (("dry run", dry, dry_code), ("activation", real, real_code)):
        check(code == 0 and any("host readiness" in line for line in said)
              and "switchyard:       kernel.sysrq = 244" in said
              and f"switchyard:     then: sudo sysctl -p {hd.SYSRQ_DROP_IN}" in said,
              f"{label}: the findings are reported, and the exit status is unchanged: {code} {said}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"host_diagnostics_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
