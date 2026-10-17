"""The confirmation a preview would print, for suites that post reports to a board directly (SYRD-602)."""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def confirmed_report(base_url: str, payload: dict[str, object]) -> dict[str, object]:
    """`payload` with the confirmation a preview of it on that board would print (SYRD-602)."""
    from scripts.ticket_board import report_confirmation

    with urllib.request.urlopen(base_url + "/api/client-config", timeout=5) as response:
        identity = report_confirmation.destination_identity(json.loads(response.read()))
    digest = report_confirmation.report_digest(
        destination=identity, source_project=str(payload.get("origin_project", "")), title=str(payload.get("title", "")),
        body=str(payload.get("body", "")), requested_stage=str(payload.get("requested_stage") or ""),
        external_source_ref=str(payload.get("external_source_ref", payload.get("source_ref", ""))))
    return {**payload, "confirmation": digest}
