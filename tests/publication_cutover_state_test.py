#!/usr/bin/env python3
"""SYRD-116: say what the cutover state IS, not what it might have been.

The boundary reported the same remediation every run: register this key for
write, take write away from the shared credential, and until you do both every
role can still push. On this host all three sentences were false -- the key was
registered, the shared credential had already been made read only, and
publication through the root key was working. A security line that is false on
a correctly configured host is worse than no line: it teaches an operator to
skip the one that would matter.

So the report is driven by recorded evidence, and the evidence is only ever what
was actually observed. Nothing here contacts a forge: the checks are driven
through the injected runner, which is what the real code uses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import publication_boundary as pb

REMOTE = "git@github-switchyard:ebudai/switchyard.git"
PUBLICATION_FINGERPRINT = "SHA256:mzm+Y0edkLLh/zlZAiOcC9XadQqV1331aQ24qCQnw84"
SHARED_FINGERPRINT = "SHA256:sharedsharedsharedsharedsharedsharedshared0"


class Recorder:
    """A runner that answers the two commands this module actually shells out to."""

    def __init__(self, *, push: tuple[int, str, str] = (0, "", "")) -> None:
        self.calls: list[list[str]] = []
        self.push = push

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        text = " ".join(str(part) for part in args)
        if "install" in text and "/dev/stdin" in text:
            # Run the real command, minus the ownership only root can set, with
            # the real payload on stdin. Writing the record is the part worth
            # exercising rather than stubbing.
            done = subprocess.run(
                [part for part in args if part not in ("-o", "root", "-g")],
                input=kwargs.get("input", ""), text=True, capture_output=True,
            )
            return subprocess.CompletedProcess(args, done.returncode, done.stdout, done.stderr)
        if "ssh-keygen" in text:
            return subprocess.CompletedProcess(args, 0, stdout=f"256 {SHARED_FINGERPRINT} shared (ED25519)\n", stderr="")
        code, out, err = self.push
        return subprocess.CompletedProcess(args, code, stdout=out, stderr=err)


def sandbox(root: Path) -> None:
    os.environ["SWITCHYARD_PUBLISH_ROOT"] = str(root / "publish")
    (root / "publish").mkdir(parents=True, exist_ok=True)


def evidence(project: str = "demo", **shared) -> pb.CutoverEvidence:
    return pb.read_cutover_evidence(
        project,
        remote=REMOTE,
        publication_fingerprint=PUBLICATION_FINGERPRINT,
        shared_fingerprint=shared.get("shared_fingerprint", SHARED_FINGERPRINT),
    )


def outcome(**overrides) -> pb.PublicationOutcome:
    base = pb.PublicationOutcome(
        public_key="ssh-ed25519 AAAA demo",
        fingerprint=f"256 {PUBLICATION_FINGERPRINT} demo (ED25519)",
        remote=REMOTE,
        shared_fingerprint=SHARED_FINGERPRINT,
        shared_check_command="sudo -u demo-agent sh -c '...'",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def report(**overrides) -> list[str]:
    lines: list[str] = []
    pb.report_publication_outcome(outcome(**overrides), project="demo", print_func=lines.append)
    return lines


def test_a_fresh_key_still_gets_the_public_half_and_the_forge_steps() -> None:
    with tempfile.TemporaryDirectory() as raw:
        sandbox(Path(raw))
        lines = report(key_created=True)
        text = "\n".join(lines)
        assert "ssh-ed25519 AAAA demo" in text, text
        assert PUBLICATION_FINGERPRINT in text, text
        assert "Register it as a WRITE key" in text, text
        # Even here it does not assert the shared credential is writable: that
        # has not been checked.
        assert "every role under that account can push" not in text, text


def test_a_verified_cutover_reports_ready_and_nothing_else() -> None:
    """The live state on this host, and the one the old report got wrong."""
    with tempfile.TemporaryDirectory() as raw:
        sandbox(Path(raw))
        recorder = Recorder()
        assert pb.record_publication_write(
            "demo", remote=REMOTE, publication_fingerprint=PUBLICATION_FINGERPRINT,
            detail="integrated main at abc123def456", runner=recorder,
        ) == ""
        pb.write_cutover_evidence(
            pb.CutoverEvidence(
                project="demo", remote=REMOTE,
                publication_fingerprint=PUBLICATION_FINGERPRINT,
                shared_fingerprint=SHARED_FINGERPRINT,
                publication=pb.CredentialFinding(pb.WRITE_VERIFIED, "2026-09-13T00:00:00+00:00", "integrated main"),
                shared=pb.CredentialFinding(pb.WRITE_READ_ONLY, "2026-09-13T00:00:00+00:00", "the forge refused the push as read only"),
            ),
            runner=recorder,
        )
        assert pb.cutover_state(evidence()) == pb.CUTOVER_READY

        text = "\n".join(report())
        assert "publication cutover is complete" in text, text
        assert "read only" in text, text
        # None of the remediation, and no public key dump: there is nothing to do.
        assert "Register it as a WRITE key" not in text, text
        assert "remove write authority" not in text.casefold(), text
        assert "ssh-ed25519" not in text, text


def test_a_writable_shared_credential_is_reported_as_remaining_risk() -> None:
    with tempfile.TemporaryDirectory() as raw:
        sandbox(Path(raw))
        recorder = Recorder()
        pb.write_cutover_evidence(
            pb.CutoverEvidence(
                project="demo", remote=REMOTE,
                publication_fingerprint=PUBLICATION_FINGERPRINT,
                shared_fingerprint=SHARED_FINGERPRINT,
                publication=pb.CredentialFinding(pb.WRITE_VERIFIED, "2026-09-13T00:00:00+00:00", "published a ref"),
                shared=pb.CredentialFinding(pb.WRITE_PRESENT, "2026-09-13T00:00:00+00:00", "a dry-run push was accepted"),
            ),
            runner=recorder,
        )
        assert pb.cutover_state(evidence()) == pb.CUTOVER_SHARED_WRITE
        text = "\n".join(report())
        assert "still does too -- checked, not assumed" in text, text
        assert "can push until it is removed" in text, text
        assert "cutover is complete" not in text, text


def test_an_unproven_state_is_reported_as_unproven() -> None:
    with tempfile.TemporaryDirectory() as raw:
        sandbox(Path(raw))
        assert pb.cutover_state(evidence()) == pb.CUTOVER_UNVERIFIED
        text = "\n".join(report())
        assert "is not recorded yet" in text, text
        # The exact bounded check, not an assertion about safety either way.
        assert "proposes no change and moves no ref" in text, text
        assert "every role under that account can push" not in text, text
        assert "cutover is complete" not in text, text


def test_a_failed_verification_claims_neither_safe_nor_unsafe() -> None:
    with tempfile.TemporaryDirectory() as raw:
        sandbox(Path(raw))
        recorder = Recorder(push=(128, "", "ssh: Could not resolve hostname github-switchyard"))
        finding = pb.verify_shared_credential(
            "demo", remote=REMOTE, owner_user="demo-agent",
            identity_file=str(Path(raw) / "id_ed25519"),
            publication_fingerprint=PUBLICATION_FINGERPRINT,
            shared_fingerprint=SHARED_FINGERPRINT,
            runner=recorder,
        )
        assert finding.state == pb.WRITE_UNKNOWN, finding
        assert "Could not resolve hostname" in finding.detail, finding

        text = "\n".join(report())
        assert "UNKNOWN" in text, text
        assert "not saying they are safe and not saying they are unsafe" in text, text


def test_the_shared_check_proposes_no_change() -> None:
    """It must be unable to move a ref even if --dry-run were ignored."""
    command = pb.shared_credential_check_command(
        REMOTE, owner_user="demo-agent", identity_file="/home/demo-agent/.ssh/id_ed25519"
    )
    script = command[-1]
    assert command[:3] == ["sudo", "-u", "demo-agent"], command
    assert "--dry-run" in script, script
    # The only refspec pushed is the remote's own tip back onto its own ref.
    assert '"$tip:$ref"' in script, script
    assert "--force" not in script and "--delete" not in script, script
    assert "BatchMode=yes" in script, script


def test_the_two_verdicts_read_back_from_a_real_dry_run() -> None:
    with tempfile.TemporaryDirectory() as raw:
        sandbox(Path(raw))
        refused = pb.verify_shared_credential(
            "demo", remote=REMOTE, owner_user="demo-agent", identity_file="/k",
            publication_fingerprint=PUBLICATION_FINGERPRINT, shared_fingerprint=SHARED_FINGERPRINT,
            runner=Recorder(push=(128, "", "ERROR: The key you are authenticating with has been marked as read only.")),
        )
        assert refused.state == pb.WRITE_READ_ONLY, refused
        accepted = pb.verify_shared_credential(
            "demo", remote=REMOTE, owner_user="demo-agent", identity_file="/k",
            publication_fingerprint=PUBLICATION_FINGERPRINT, shared_fingerprint=SHARED_FINGERPRINT,
            runner=Recorder(push=(0, "To github.com:demo/demo.git\n", "")),
        )
        assert accepted.state == pb.WRITE_PRESENT, accepted


def test_a_rotated_key_or_a_moved_remote_invalidates_the_verdict() -> None:
    with tempfile.TemporaryDirectory() as raw:
        sandbox(Path(raw))
        recorder = Recorder()
        pb.write_cutover_evidence(
            pb.CutoverEvidence(
                project="demo", remote=REMOTE,
                publication_fingerprint=PUBLICATION_FINGERPRINT,
                shared_fingerprint=SHARED_FINGERPRINT,
                publication=pb.CredentialFinding(pb.WRITE_VERIFIED, "2026-09-13T00:00:00+00:00", "published"),
                shared=pb.CredentialFinding(pb.WRITE_READ_ONLY, "2026-09-13T00:00:00+00:00", "refused as read only"),
            ),
            runner=recorder,
        )
        assert pb.cutover_state(evidence()) == pb.CUTOVER_READY

        rotated = pb.read_cutover_evidence(
            "demo", remote=REMOTE, publication_fingerprint="SHA256:adifferentkeyentirely",
            shared_fingerprint=SHARED_FINGERPRINT,
        )
        assert rotated.publication.state == pb.WRITE_UNKNOWN, rotated
        assert any("publication key changed" in reason for reason in rotated.stale), rotated.stale

        moved = pb.read_cutover_evidence(
            "demo", remote="git@github.com:someone/else.git",
            publication_fingerprint=PUBLICATION_FINGERPRINT, shared_fingerprint=SHARED_FINGERPRINT,
        )
        assert moved.publication.state == pb.WRITE_UNKNOWN, moved
        assert moved.shared.state == pb.WRITE_UNKNOWN, moved
        assert any("recorded remote" in reason for reason in moved.stale), moved.stale

        # And the report says so rather than silently downgrading.
        text = "\n".join(report(fingerprint="256 SHA256:adifferentkeyentirely demo (ED25519)"))
        assert "no longer applies" in text, text
        assert "cutover is complete" not in text, text


def test_nothing_written_is_secret() -> None:
    with tempfile.TemporaryDirectory() as raw:
        sandbox(Path(raw))
        recorder = Recorder()
        pb.record_publication_write(
            "demo", remote=REMOTE, publication_fingerprint=PUBLICATION_FINGERPRINT,
            detail="published roles/ops/x at abc123", runner=recorder,
        )
        document = json.loads(pb.cutover_evidence_path("demo").read_text())
        assert document["schema"] == pb.CUTOVER_SCHEMA, document
        assert set(document) == {
            "schema", "project", "remote",
            "publication_fingerprint", "shared_fingerprint",
            "publication_write", "publication_write_at", "publication_write_detail",
            "shared_write", "shared_write_at", "shared_write_detail",
        }, sorted(document)
        serialized = json.dumps(document)
        for secret in ("PRIVATE KEY", "BEGIN OPENSSH", "ssh-ed25519 AAAA"):
            assert secret not in serialized, serialized
        # It is installed world-readable and root-owned, like the grant beside it.
        written = [c for c in recorder.calls if "install" in " ".join(str(p) for p in c)]
        assert any("-m 0644 -o root -g root" in " ".join(str(p) for p in c) for c in written), written


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"publication_cutover_state_test: {len(tests)} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
