#!/usr/bin/env python3
"""SYRD-857: the review VM kit, end to end, in a real KVM VM. Needs /dev/kvm and the network (sweep_requirements).

`tests/vm_kit/vm_kit.py check` on this tree's HEAD commit: it boots a fresh Arch VM, installs that commit with the
real `./install`, creates a tenant with the real `switchyard new` (offline: stand-in CLIs, no sign-in to wait for),
then requires the tenant's board to run a declared workflow, `research-stage enable --dry-run` to pass from its
Director's pane, and the listener to deliver a real notification into main's pane. It always powers the VM off.

Everything goes in a scratch state directory, removed at the end. The base image is the one `vm_kit.py
fetch-image` verified into this account's cache, when there is one (read only); otherwise it is downloaded and
verified into the scratch directory.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "tests" / "vm_kit" / "vm_kit.py"
sys.path.insert(0, str(KIT.parent))
sys.dont_write_bytecode = True  # nothing beside the kit: install copies the stand-ins out of it
import vm_kit  # noqa: E402

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def cached_image() -> Path | None:
    """The account's own verified image -- looked up by account, since a sweep gives this suite a scratch HOME."""
    images = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache" / "switchyard-test-vm" / "images"
    image = images / vm_kit.IMAGE_NAME
    return image if image.is_file() and (images / f"{vm_kit.IMAGE_NAME}.verified").is_file() else None


def main() -> int:
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True, capture_output=True,
                            text=True).stdout.strip()
    state = Path(tempfile.mkdtemp(prefix="vmkit."))
    name = f"suite-{os.getpid()}"
    image = cached_image()
    argv = [sys.executable, "-B", str(KIT), "--state-dir", str(state), "check", "--repo", str(ROOT), "--commit",
            commit, "--name", name, "--slug", "vmt", "--lifetime", "2400", *(["--image", str(image)] if image else [])]
    print(f"vm_kit_test: {commit[:12]} from {ROOT} ({'cached image ' + str(image) if image else 'downloading'})",
          flush=True)
    try:
        done = subprocess.run(argv, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=2700)
        sys.stdout.write(done.stdout)
        sys.stderr.write(done.stderr[-4000:])
        out = done.stdout
        check(done.returncode == 0, f"vm_kit check exits 0: {done.returncode}")
        check(f"installing {commit[:12]}" in out and "install rc=0" in out,
              "the VM installed this commit with ./install")
        check("switchyard new rc=0" in out and "every pane of vmt is up" in out,
              "switchyard new created the tenant offline, its panes up, with no sign-in to wait for")
        check("vmt's board runs a declared workflow (revision" in out, "the tenant's board runs a declared workflow")
        check("research-stage enable --dry-run passes from vmt's Director pane" in out,
              "research-stage enable --dry-run passes from the Director's pane")
        check("Delivered queued notification" in out and "to vmt-main:0.0" in out,
              "the listener delivered a real notification into main's pane")
        check(f"{name}: gone (its supervisor and qemu have ended; overlay, seed and keys removed" in out,
              "the VM was powered off and proved gone")
        check(not list((state / "vms").glob("*")), "nothing of it is left: no VM directory")
    finally:
        for leftover in sorted((state / "vms").glob("*")):  # a failure before check's own down
            subprocess.run([sys.executable, "-B", str(KIT), "--state-dir", str(state), "down", leftover.name],
                           stdin=subprocess.DEVNULL, timeout=600)
        shutil.rmtree(state, ignore_errors=True)
    print(f"vm_kit_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
