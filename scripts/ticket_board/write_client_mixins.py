"""Write client calls kept beside write_client.py, so that file does not grow past its size limit.

Provider account holds (SYRD-672), the restore of a mistaken reopen (SYRD-746) and the pause switch (SYRD-756).
"""

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


class WorkPauseWrites:
    """Mixed into TicketBoardWriteClient: the Director's pause switch (SYRD-756)."""

    def pause_work(self, mode: str, *, reason: str, caller_role: str | None = None) -> dict[str, Any]:
        """Pause all work -- drain or stop -- or change the standing pause's mode, with why."""
        return self._post("/actions/pause_work", {"mode": mode, "reason": reason}, caller_role=caller_role)

    def resume_work(self, *, reason: str = "", caller_role: str | None = None) -> dict[str, Any]:
        """End the pause, whichever mode, with why."""
        return self._post("/actions/resume_work", {"reason": reason}, caller_role=caller_role)


class TicketRepairWrites:
    """Mixed into TicketBoardWriteClient, whose `_ticket_action` sends each as the caller."""

    def restore_reopened(self, ticket_id: str, *, reason: str, caller_role: str | None = None) -> dict[str, Any]:
        """Put back the closed disposition a mistaken reopen took away, as the board recorded it (SYRD-746)."""
        return self._ticket_action(ticket_id, "restore_reopened", {"reason": reason}, caller_role=caller_role)
