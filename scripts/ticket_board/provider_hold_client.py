"""The write client's provider account hold calls (SYRD-672), beside it so write_client.py does not grow."""

from __future__ import annotations

from typing import Any


class ProviderAccountHoldWrites:
    """Mixed into TicketBoardWriteClient, whose `_post` sends each as the caller."""

    def hold_provider_account(self, runtime: str, *, reason: str, caller_role: str | None = None) -> dict[str, Any]:
        """Hold every role on a provider account by hand: a suspected safety refusal, with what was seen."""
        return self._post("/actions/hold_provider_account", {"runtime": runtime, "reason": reason}, caller_role=caller_role)

    def clear_provider_account_hold(self, runtime: str, *, reason: str, caller_role: str | None = None) -> dict[str, Any]:
        """End a provider account's safety hold, once the operator confirms it is safe, with who confirmed it."""
        return self._post("/actions/clear_provider_account_hold", {"runtime": runtime, "reason": reason},
                          caller_role=caller_role)
