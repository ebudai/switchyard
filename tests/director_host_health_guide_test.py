#!/usr/bin/env python3
"""SYRD-554: the Director's guide carries the host checks it ensures, and every remedy in it is real.

"Host health the board depends on" makes the 2026-10-04 housekeeping the
Director's to check and escalate. Its remedies are the ones SYRD-553's check
prints, byte for byte, and each is read by the real tool against a fixture host
that carries the distribution's own small defaults: `systemd-analyze
cat-config` merges the journald and coredump drop-ins after them, and `sysctl
--dry-run -p` parses the SysRq file. The Director's own commands are run as
written, against the fixture, and the ones that need root say `sudo`.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import host_diagnostics as hd  # noqa: E402

GUIDE = ROOT / "docs" / "onboarding" / "switchyard-director-guide.md"
README = ROOT / "docs" / "onboarding" / "README.md"
HEADING = "## Host health the board depends on"
SHARED_RELEASE = "/opt/switchyard/current"
CHECKS = 0
CORE_PATTERN = "|/usr/lib/systemd/systemd-coredump %P %u %g %s %t %c %h %d\n"


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def section() -> str:
    text = GUIDE.read_text(encoding="utf-8")
    start = text.index(HEADING)
    end = text.index("\n## ", start + len(HEADING))
    return text[start:end]


def blocks(language: str) -> list[str]:
    return re.findall(rf"^```{language}\n(.*?)^```$", section(), flags=re.MULTILINE | re.DOTALL)


def remedies() -> dict[str, tuple[str, str, str]]:
    """Each remedy block by the file it names: (body, apply command, whole block)."""
    found = {}
    for block in blocks("ini"):
        lines = block.splitlines(keepends=True)
        check(lines[0].startswith("# /etc/") and lines[1].startswith("# then: "),
              f"a remedy names its file, then the command that applies it: {block!r}")
        found[lines[0][2:].strip()] = ("".join(lines[2:]), lines[1][len("# then: "):].strip(), block)
    return found


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def distribution_host(root: Path) -> Path:
    """A host as a distribution ships it: a 50M journal, uncapped dumps, sync-only SysRq."""
    write(root, "usr/lib/systemd/journald.conf", "[Journal]\n#SystemMaxUse=\n")
    write(root, "usr/lib/systemd/journald.conf.d/00-journal-size.conf", "[Journal]\nSystemMaxUse=50M\n")
    write(root, "usr/lib/systemd/coredump.conf", "[Coredump]\n#MaxUse=\n")
    write(root, "usr/lib/sysctl.d/50-default.conf", "kernel.sysrq = 16\n")
    write(root, "proc/sys/kernel/sysrq", "16\n")
    write(root, "proc/sys/kernel/core_pattern", CORE_PATTERN)
    (root / "var/lib/systemd/coredump").mkdir(parents=True)
    for index in range(3):
        write(root, f"etc/switchyard/projects/t{index}.json", "{}\n")
    return root


def remedied_host(root: Path) -> Path:
    """The same host with every remedy from the guide written where it says."""
    distribution_host(root)
    for path, (_body, _apply, block) in remedies().items():
        write(root, path, block)
    return root


def cat_config(root: Path, name: str) -> str:
    shown = subprocess.run(["systemd-analyze", "cat-config", f"--root={root}", name],
                           capture_output=True, text=True, timeout=60)
    check(shown.returncode == 0, f"systemd-analyze reads {name}: {shown.stderr}")
    return shown.stdout


def director_command(prefix: str) -> str:
    found = next((line for block in blocks("bash") for line in block.splitlines() if line.startswith(prefix)), None)
    check(found is not None, f"the guide gives the Director a command starting {prefix!r}")
    return found


def run_bash(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", "set -o pipefail; " + command], capture_output=True, text=True, timeout=60)


def test_the_section_sits_beside_escalation_and_the_readme_sends_the_director_to_it() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    after = text.index("\n## ", text.index(HEADING) + len(HEADING))
    check(text[after:].startswith("\n## Escalating to the human"),
          "the section comes right before Escalating to the human")
    readme = README.read_text(encoding="utf-8")
    entry = readme[readme.index("3. **[switchyard-director-guide.md]"):readme.index("## Start here")]
    check("Host health the board depends on" in " ".join(entry.split()), f"the reading order names the section: {entry}")
    body = section()
    check("SYRD-552" in body and "SYRD-553" in body, "it cross-references the restart storm and the readiness check")
    check("Never apply a host-wide change yourself" in body and "escalate" in body,
          "the Director checks and escalates, and never applies")


def test_each_remedy_is_the_one_the_check_prints() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd554-dist.") as raw:
        findings = hd.host_diagnostics_findings(distribution_host(Path(raw)))
    documented = remedies()
    check(sorted(f.remedy_path for f in findings) == sorted(documented),
          f"one documented remedy per finding, at the check's path: {sorted(documented)}")
    for finding in findings:
        body, apply, _block = documented[finding.remedy_path]
        check(body == finding.remedy_text and apply == finding.apply_command,
              f"{finding.item}: the guide's lines are the check's: {body!r} {apply!r} vs "
              f"{finding.remedy_text!r} {finding.apply_command!r}")


def test_each_remedy_is_read_by_the_real_tool_after_the_distributions_default() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd554-fixed.") as raw:
        fixed = remedied_host(Path(raw))
        documented = remedies()
        for name, key, small, wanted in (("systemd/journald.conf", "SystemMaxUse", "50M", "1G"),
                                         ("systemd/coredump.conf", "MaxUse", None, "1G")):
            shown = cat_config(fixed, name)
            path = next((p for p in documented if p.startswith(f"/etc/{name}.d/")), None)
            check(path is not None, f"{name}: the guide has a drop-in for it: {sorted(documented)}")
            check(f"# {fixed}{path}" in shown, f"{name}: systemd-analyze reads the drop-in at {path}: {shown}")
            values = re.findall(rf"^{key}=(\S+)$", shown, flags=re.MULTILINE)
            check(values[-1:] == [wanted] and (small is None or small in values),
                  f"{name}: the drop-in's {key}={wanted} is merged last, after the distribution's: {values}")
        sysrq = next((p for p in documented if p.startswith("/etc/sysctl.d/")), None)
        check(sysrq is not None, f"the guide has a sysctl.d file for SysRq: {sorted(documented)}")
        parsed = subprocess.run(["sysctl", "--dry-run", "-p", str(fixed / sysrq.lstrip("/"))],
                                capture_output=True, text=True, timeout=60)
        check(parsed.returncode == 0 and parsed.stdout.strip() == "kernel.sysrq = 244",
              f"the SysRq file parses with sysctl -p, without writing: {parsed.stdout}{parsed.stderr}")
        order = re.findall(r"^kernel\.sysrq = (\d+)$", cat_config(fixed, "sysctl.d"), flags=re.MULTILINE)
        check(order == ["16", "244"], f"the SysRq file is applied after systemd's 50-default.conf: {order}")
        write(fixed, "proc/sys/kernel/sysrq", order[-1] + "\n")
        remaining = hd.host_diagnostics_findings(fixed)
    check(remaining == [], f"with the guide's remedies in place the check finds nothing: {remaining}")


def test_the_directors_reads_run_as_written_against_a_host() -> None:
    journal = director_command("systemd-analyze cat-config systemd/journald.conf")
    dumps = director_command("systemd-analyze cat-config systemd/coredump.conf")
    for label, build in (("distribution", distribution_host), ("remedied", remedied_host)):
        with tempfile.TemporaryDirectory(prefix=f"syrd554-{label}.") as raw:
            host = build(Path(raw))
            rooted = f"systemd-analyze cat-config --root={shlex.quote(str(host))} "
            said = run_bash(journal.replace("systemd-analyze cat-config ", rooted)).stdout.split()
            cap = run_bash(dumps.replace("systemd-analyze cat-config ", rooted))
        if label == "distribution":
            check(said == ["SystemMaxUse=50M"], f"the journal read shows the distribution's 50M: {said}")
            check(cap.returncode == 1 and cap.stdout == "", f"no MaxUse= is printed when none is set: {cap}")
        else:
            check(said[-1] == "SystemMaxUse=1G" and "Storage=persistent" in said,
                  f"the journal read ends on the remedy's value: {said}")
            check(cap.stdout.split() == ["MaxUse=1G"], f"the coredump read shows the cap: {cap.stdout}")
    for line in (line for block in blocks("bash") for line in block.splitlines()):
        parsed = subprocess.run(["bash", "-n", "-c", line.replace("<repository>", "repo").replace("<inode>", "1")],
                                capture_output=True, text=True, timeout=60)
        check(parsed.returncode == 0, f"bash parses {line!r}: {parsed.stderr}")


def test_only_the_post_crash_reads_ask_for_root() -> None:
    body = section()
    split = body.index("**4. After any unclean shutdown")
    own = [line for block in re.findall(r"^```bash\n(.*?)^```$", body[:split], flags=re.M | re.S)
           for line in block.splitlines()]
    root = [line for block in re.findall(r"^```bash\n(.*?)^```$", body[split:], flags=re.M | re.S)
            for line in block.splitlines()]
    check(own and not any("sudo" in line for line in own), f"the Director's own reads need no root: {own}")
    check([line.split()[:2] for line in root if line.startswith("sudo ")]
          == [["sudo", "btrfs"], ["sudo", "journalctl"], ["sudo", "smartctl"]],
          f"the reads that need root say so, for the human to run: {root}")
    check(any(line.startswith("git -C <repository> fsck --full") for line in root),
          "the repository check is the Director's own, without sudo")


def test_the_check_command_runs_the_shared_releases_module() -> None:
    line = director_command("python3 -I -c ")
    argv = shlex.split(line)
    check(argv[:3] == ["python3", "-I", "-c"] and SHARED_RELEASE in argv[3], f"it reads the shared release: {argv}")
    with tempfile.TemporaryDirectory(prefix="syrd554-release.") as raw:
        release = Path(raw) / "release"
        (release / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts" / "host_diagnostics.py", release / "scripts" / "host_diagnostics.py")
        ran = subprocess.run([*argv[:3], argv[3].replace(SHARED_RELEASE, str(release))],
                             capture_output=True, text=True, timeout=60, cwd=raw)
        old = Path(raw) / "old"
        (old / "scripts").mkdir(parents=True)
        missing = subprocess.run([*argv[:3], argv[3].replace(SHARED_RELEASE, str(old))],
                                 capture_output=True, text=True, timeout=60, cwd=raw)
    first = (ran.stdout.splitlines() or [""])[0]
    check(ran.returncode == 0 and (first == "host readiness: no findings"
                                   or first.startswith("switchyard: host readiness:")),
          f"with the module in the release it prints the report or says there is none: {ran.stdout}{ran.stderr}")
    check(missing.returncode != 0 and "ModuleNotFoundError" in missing.stderr
          and "`ModuleNotFoundError` means the shared release predates SYRD-553" in section(),
          f"a release before SYRD-553 fails as the guide says it will: {missing.stderr}")


def test_the_stated_thresholds_are_the_checks() -> None:
    body = section()
    check(hd.journal_needed(8) == hd.GIB and hd.journal_needed(9) == 2 * hd.GIB
          and hd.JOURNAL_PER_TENANT == 128 * hd.MIB
          and "at least `1G`, plus 128 MiB for each registered tenant\n  past eight" in body,
          "the journal floor and its growth per tenant")
    check(hd.COREDUMP_CAP == hd.GIB and hd.COREDUMP_ON_DISK == 512 * hd.MIB
          and "`MaxUse` is above `1G`" in body and "more than 512M" in body,
          "the coredump cap and the store size that triggers escalation")
    check(hd.SYSRQ_NEEDED == 16 | 32 | 128 and hd.SYSRQ_RECOMMENDED == 244 == 4 | 16 | 32 | 64 | 128
          and "sync (16), remount read-only (32) and reboot (128)" in body
          and all(f"({bit})" in body for bit in (4, 64, 16, 32, 128, 8)),
          "the SysRq bits the guide names are the check's, and 244 is exactly them")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"director_host_health_guide_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
