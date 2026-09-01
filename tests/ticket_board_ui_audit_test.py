#!/usr/bin/env python3
"""Broad Playwright functional audit for the ticket-board UI."""

from __future__ import annotations

import sys
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.ticket_board_ui_harness import (
    BoardHarness,
    create_clipboard_png_payload,
    load_playwright,
    ticket_payload,
    wait_for_ticket_field,
)


def detail_field(modal: Any, label: str) -> Any:
    return modal.locator(".field-label", has_text=label).locator("xpath=..").first


def open_detail(page: Any, ticket_id: str) -> Any:
    page.locator(".card > .card-top .card-id").filter(has_text=re.compile(f"^{re.escape(ticket_id)}$")).click()
    modal = page.locator(".detail-modal")
    modal.wait_for(timeout=5000)
    modal.locator(".detail-ticket-headline strong", has_text=ticket_id).wait_for(timeout=5000)
    return modal


def close_detail(page: Any) -> None:
    page.locator("#detailCloseBtn").click()
    page.locator("#detailOverlay").wait_for(state="hidden", timeout=5000)


def refresh_open_detail_from_server(page: Any, ticket_id: str) -> None:
    page.evaluate(
        """async (ticketId) => {
          clearDetailDraft(ticketId);
          await requestBoardReload();
        }""",
        ticket_id,
    )


def install_update_ticket_tracker(page: Any) -> None:
    page.evaluate(
        """() => {
          if (window.__pguUpdateTicketTrackerInstalled) {
            return;
          }
          const originalUpdateTicket = updateTicket;
          window.__pguLastUpdateTicket = null;
          window.updateTicket = (...args) => {
            const promise = originalUpdateTicket(...args);
            window.__pguLastUpdateTicket = Promise.resolve(promise);
            return promise;
          };
          window.__pguUpdateTicketTrackerInstalled = true;
        }"""
    )


def reset_update_ticket_tracker(page: Any) -> None:
    page.evaluate("""() => { window.__pguLastUpdateTicket = null; }""")


def wait_for_last_update_ticket(page: Any) -> None:
    page.evaluate(
        """async () => {
          const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          for (let attempt = 0; attempt < 50 && !window.__pguLastUpdateTicket; attempt += 1) {
            await sleep(20);
          }
          if (!window.__pguLastUpdateTicket) {
            throw new Error('no updateTicket call was captured');
          }
          await window.__pguLastUpdateTicket;
        }"""
    )


def add_blocker_with_picker(page: Any, blocked_by_field: Any, blocker_id: str) -> None:
    add_button = blocked_by_field.get_by_role("button", name="Add Blocker")
    last_error = ""
    for _ in range(5):
        try:
            blocked_by_field.locator("select").select_option(blocker_id)
            page.wait_for_function(
                "(button) => !button.disabled",
                arg=add_button.element_handle(timeout=5000),
                timeout=1000,
            )
            reset_update_ticket_tracker(page)
            add_button.click()
            wait_for_last_update_ticket(page)
            return
        except Exception as exc:  # noqa: BLE001 - include Playwright timeout/detach errors in retry diagnostics.
            last_error = str(exc)
    raise AssertionError(f"Add Blocker did not persist {blocker_id}: {last_error}")


def wait_for_blocker_card_render(page: Any, ticket_id: str, blocker_id: str, *, blocked: bool) -> None:
    page.evaluate(
        """async ([ticketId, blockerId, blocked]) => {
          const expectedBadge = `blocked ${blockerId}`;
          const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const findCard = () => Array.from(document.querySelectorAll('.card')).find((card) =>
            Array.from(card.querySelectorAll('.card-id')).some((idEl) => idEl.textContent.trim() === ticketId)
          );
          const deadline = performance.now() + 5000;
          let lastCardText = '';
          let lastCardClass = '';
          while (performance.now() < deadline) {
            await requestBoardReload();
            await loadBoard();
            const card = findCard();
            lastCardText = card ? card.innerText : '<missing>';
            lastCardClass = card ? card.className : '<missing>';
            const hasBlockedClass = !!card && card.classList.contains('card-blocked');
            const hasBadge = !!card && card.innerText.includes(expectedBadge);
            if (blocked ? (hasBlockedClass && hasBadge) : (!hasBlockedClass && !hasBadge)) {
              return;
            }
            await sleep(100);
          }
          throw new Error(`Timed out waiting for ${ticketId} blocked=${blocked}; class=${lastCardClass}; text=${lastCardText}`);
        }""",
        [ticket_id, blocker_id, blocked],
    )


def get_ticket(page: Any, base_url: str, ticket_id: str) -> dict[str, Any]:
    return page.evaluate(
        """async ([baseUrl, ticketId]) => {
          const response = await fetch(`${baseUrl}api/tickets/${ticketId}`, { cache: 'no-store' });
          if (!response.ok) throw new Error(await response.text());
          return response.json();
        }""",
        [base_url, ticket_id],
    )


def complete_task_from_browser(page: Any, ticket_id: str, caller_role: str, text: str) -> None:
    page.evaluate(
        """async ([ticketId, callerRole, text]) => {
          const response = await fetch(`/api/tickets/${ticketId}/actions/complete_task`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-Ticket-Board-Caller-Role': callerRole,
              'X-Ticket-Board-Write-Token': window.PGU_TICKET_BOARD_WRITE_TOKEN,
            },
            body: JSON.stringify({ text }),
          });
          if (!response.ok) throw new Error(await response.text());
        }""",
        [ticket_id, caller_role, text],
    )


def seed_tickets() -> list[dict[str, Any]]:
    return [
        ticket_payload(
            "PGU-501",
            "Busy app implementation",
            body="Current active app task.",
            state="in_progress",
            assignee="app",
            implementation="Active work package",
            created_offset_minutes=1,
            active_work_highlight=True,
        ),
        ticket_payload(
            "PGU-502",
            "Feedback upload naming",
            body="Candidate blocker for PGU-503.",
            state="analysis",
            assignee="app",
            implementation="Ready for app when app is free.",
            created_offset_minutes=2,
        ),
        ticket_payload(
            "PGU-503",
            "Feedback upload naming follow-up",
            body="User tried setting PGU-502 as a blocker here.",
            state="analysis",
            assignee="unassigned",
            created_offset_minutes=3,
        ),
        ticket_payload(
            "PGU-506",
            "Completed reference ticket",
            state="done",
            assignee="director",
            commit_exempt=True,
            created_offset_minutes=4,
        ),
        ticket_payload(
            "PGU-507",
            "UAT sign-off target",
            body="Requires User signoff.",
            state="user_review",
            assignee="director",
            needs_user_signoff=True,
            audit_signoff=True,
            commit_exempt=True,
            created_offset_minutes=5,
        ),
        ticket_payload(
            "PGU-508",
            "Advance target",
            state="analysis",
            assignee="ops",
            implementation="Advance via default workflow.",
            created_offset_minutes=6,
        ),
        ticket_payload(
            "PGU-509",
            "Cancelled hidden target",
            state="cancelled",
            assignee="director",
            created_offset_minutes=7,
        ),
        ticket_payload(
            "PGU-510",
            "Deferred hidden target",
            state="backlog",
            assignee="research",
            created_offset_minutes=8,
        ),
        ticket_payload(
            "PGU-511",
            "Cancel from UI target",
            state="analysis",
            assignee="unassigned",
            created_offset_minutes=9,
        ),
        ticket_payload(
            "PGU-512",
            "Detail edit target",
            body="Body references PGU-502 for link checks.",
            state="analysis",
            assignee="main",
            created_offset_minutes=10,
        ),
    ]


def run_desktop_audit(playwright: Any, harness: BoardHarness) -> None:
    upload_source = harness.make_png("upload-source.png")
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(harness.url, wait_until="domcontentloaded")
        page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
        install_update_ticket_tracker(page)
        try:
            wait_for_ticket_field(page, harness.url, "PGU-501", "ticket.id === 'NOPE-SENTINEL'", timeout=100)
        except AssertionError:
            pass
        else:
            raise AssertionError("wait_for_ticket_field returned for a false predicate")

        assert page.locator(".card", has_text="PGU-506").count() == 0
        page.locator("#showDoneInput").check()
        page.locator(".card", has_text="PGU-506").wait_for(timeout=5000)
        page.locator("#showCancelledInput").check()
        page.locator(".card", has_text="PGU-509").wait_for(timeout=5000)
        page.locator("#showDeferredInput").check()
        page.locator(".card", has_text="PGU-510").wait_for(timeout=5000)

        page.locator("#createAttachDropZone [data-attach-set]").select_option("feedback")
        page.locator("#createAttachDropZone [data-attach-label]").fill("phone proof")
        page.locator("#createImageInput").set_input_files(str(upload_source))
        page.locator("#createStatus", has_text="feedback upload set requires a feedback number").wait_for(timeout=5000)
        assert page.locator("#createPreview .attachment-card").count() == 0

        page.locator("#createAttachDropZone [data-attach-attempt]").fill("7")
        page.locator("#createImageInput").set_input_files(str(upload_source))
        page.locator("#createStatus", has_text="Attached image to the new ticket.").wait_for(timeout=5000)
        assert page.locator("#createPreview .attachment-card", has_text="feedback-007-phone-proof__upload-source.png").count() == 1

        page.locator("#createImageInput").set_input_files(str(upload_source))
        page.locator("#createStatus", has_text="Attached image to the new ticket.").wait_for(timeout=5000)
        assert page.locator("#createPreview .attachment-card", has_text="feedback-007-phone-proof__upload-source-2.png").count() == 1

        page.evaluate(
            """async () => {
              const canvas = document.createElement('canvas');
              canvas.width = 2;
              canvas.height = 2;
              const context = canvas.getContext('2d');
              context.fillStyle = '#4d91d9';
              context.fillRect(0, 0, 2, 2);
              const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
              const file = new File([blob], '../Unsafe Proof!!.gif', { type: 'image/png' });
              await attachImageFiles([file], 'create', readCreateAttachmentSetOptions());
            }"""
        )
        page.locator("#createStatus", has_text="Attached image to the new ticket.").wait_for(timeout=5000)
        assert page.locator("#createPreview .attachment-card", has_text="feedback-007-phone-proof__unsafe-proof.png").count() == 1

        page.locator("#titleInput").fill("Harness created file-upload ticket")
        page.locator("#bodyInput").fill("Created through Playwright file upload path.")
        page.locator("#assigneeInput").select_option("app")
        page.locator("#needsUserInput").check()
        page.locator("#needsInspectionInput").check()
        page.locator("#createRegressionInput").check()
        page.locator("#createBtn").click()
        page.locator("#createStatus", has_text="Created PGU-513.").wait_for(timeout=5000)
        wait_for_ticket_field(page, harness.url, "PGU-513", "ticket.screenshots.length === 3 && ticket.screenshots.some((path) => path.includes('feedback-007-phone-proof__upload-source.png')) && ticket.screenshots.some((path) => path.includes('feedback-007-phone-proof__upload-source-2.png')) && ticket.screenshots.some((path) => path.includes('feedback-007-phone-proof__unsafe-proof.png')) && ticket.needs_user_signoff && ticket.needs_inspection && ticket.regression && ticket.assignee === 'app'")

        page.evaluate(create_clipboard_png_payload())
        page.locator("#createStatus", has_text="Pasted image attached to the new ticket.").wait_for(timeout=5000)
        page.locator("#titleInput").fill("Harness created paste ticket")
        page.locator("#bodyInput").fill("Created through Playwright paste path.")
        page.locator("#assigneeInput").select_option("ops")
        page.locator("#createBtn").click()
        page.locator("#createStatus", has_text="Created PGU-514.").wait_for(timeout=5000)
        wait_for_ticket_field(
            page,
            harness.url,
            "PGU-514",
            "ticket.screenshots.length === 1 && ticket.assignee === 'ops' && ticket.needs_audit === true",
        )

        modal = open_detail(page, "PGU-503")
        blocked_by = detail_field(modal, "Blocked By")
        add_blocker_with_picker(page, blocked_by, "PGU-502")
        wait_for_ticket_field(page, harness.url, "PGU-503", "ticket.blocked_by.includes('PGU-502') && ticket.blocked_reason === 'Waiting on PGU-502.'")
        wait_for_blocker_card_render(page, "PGU-503", "PGU-502", blocked=True)

        reset_update_ticket_tracker(page)
        blocked_by.get_by_role("button", name="Remove Blocker PGU-502").click()
        wait_for_last_update_ticket(page)
        wait_for_ticket_field(page, harness.url, "PGU-503", "ticket.blocked_by.length === 0")
        wait_for_blocker_card_render(page, "PGU-503", "PGU-502", blocked=False)

        close_detail(page)
        modal = open_detail(page, "PGU-512")
        detail_field(modal, "Title").locator("input").first.fill("Detail edit target renamed")
        detail_field(modal, "Title").get_by_role("button", name="Save Title").click()
        wait_for_ticket_field(page, harness.url, "PGU-512", "ticket.title === 'Detail edit target renamed'")
        refresh_open_detail_from_server(page, "PGU-512")

        close_detail(page)
        page.evaluate(
            """() => {
              window.TICKET_BOARD_REFRESH_IDLE_MS = 25;
              window.PGU_TICKET_BOARD_REFRESH_IDLE_MS = 60000;
              state.lastActivityAt = 0;
            }"""
        )
        harness.server.build_id = "pgu-525-idle-build"
        page.evaluate("""async () => { await requestBoardReload(); }""")
        page.wait_for_load_state("domcontentloaded")
        page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
        page.wait_for_function(
            """() => window.TICKET_BOARD_BUILD_ID === 'pgu-525-idle-build'
              && window.PGU_TICKET_BOARD_BUILD_ID === 'pgu-525-idle-build'""",
            timeout=5000,
        )
        page.locator("#refreshUpdateBanner").wait_for(state="hidden", timeout=5000)
        page.evaluate("""() => { window.PGU_TICKET_BOARD_REFRESH_IDLE_MS = 25; state.lastActivityAt = 0; }""")

        modal = open_detail(page, "PGU-512")
        comment_box = modal.get_by_placeholder("Add a comment or bounce-back note")
        comment_box.fill("Draft survives smart auto refresh")
        harness.server.build_id = "pgu-525-typing-build"
        page.evaluate("""async () => { await requestBoardReload(); }""")
        page.locator("#refreshUpdateBanner", has_text="A newer board version is ready").wait_for(timeout=5000)
        page.wait_for_timeout(150)
        page.wait_for_function("() => window.PGU_TICKET_BOARD_BUILD_ID === 'pgu-525-idle-build'", timeout=5000)
        close_detail(page)
        page.wait_for_load_state("domcontentloaded")
        page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
        page.wait_for_function("() => window.PGU_TICKET_BOARD_BUILD_ID === 'pgu-525-typing-build'", timeout=5000)
        page.evaluate("""() => { window.PGU_TICKET_BOARD_REFRESH_IDLE_MS = 25; state.lastActivityAt = 0; }""")
        modal = open_detail(page, "PGU-512")
        comment_box = modal.get_by_placeholder("Add a comment or bounce-back note")
        page.wait_for_function(
            "(element) => element.value === 'Draft survives smart auto refresh'",
            arg=comment_box.element_handle(timeout=5000),
            timeout=5000,
        )
        comment_box.fill("")

        harness.server.build_id = "pgu-525-detail-open-build"
        page.evaluate("""async () => { await requestBoardReload(); }""")
        page.locator("#refreshUpdateBanner", has_text="A newer board version is ready").wait_for(timeout=5000)
        page.wait_for_timeout(150)
        page.wait_for_function("() => window.PGU_TICKET_BOARD_BUILD_ID === 'pgu-525-typing-build'", timeout=5000)
        close_detail(page)
        page.wait_for_load_state("domcontentloaded")
        page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
        page.wait_for_function("() => window.PGU_TICKET_BOARD_BUILD_ID === 'pgu-525-detail-open-build'", timeout=5000)
        modal = open_detail(page, "PGU-512")
        page.evaluate("""() => { window.PGU_TICKET_BOARD_REFRESH_IDLE_MS = 25; state.lastActivityAt = 0; }""")
        comment_box = modal.get_by_placeholder("Add a comment or bounce-back note")
        comment_box.fill("Submit survives reload replay")
        page.evaluate("""() => { window.__pguNoReloadMarker = 'reload-replay'; }""")
        harness.server.build_id = "pgu-525-replay-build"
        modal.get_by_role("button", name="Add Comment").click()
        page.wait_for_load_state("domcontentloaded")
        page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
        page.wait_for_function("() => window.PGU_TICKET_BOARD_BUILD_ID === 'pgu-525-replay-build'", timeout=5000)
        page.wait_for_function("() => window.__pguNoReloadMarker === undefined", timeout=5000)
        page.locator("#createStatus", has_text="Comment added to PGU-512.").wait_for(timeout=5000)
        wait_for_ticket_field(page, harness.url, "PGU-512", "ticket.comments.some((comment) => comment.text === 'Submit survives reload replay')")
        modal = open_detail(page, "PGU-512")
        comment_box = modal.get_by_placeholder("Add a comment or bounce-back note")
        page.wait_for_function(
            "(element) => element.value === ''",
            arg=comment_box.element_handle(timeout=5000),
            timeout=5000,
        )

        token_route_state = {"rotated": False}

        def rotate_before_first_add_comment(route: Any) -> None:
            if not token_route_state["rotated"]:
                token_route_state["rotated"] = True
                harness.server.write_token = "pgu-525-reactive-token"
            route.continue_()

        page.route("**/api/tickets/PGU-512/actions/add_comment", rotate_before_first_add_comment)
        add_comment_statuses: list[int] = []

        def record_add_comment_response(response: Any) -> None:
            if "/api/tickets/PGU-512/actions/add_comment" in response.url:
                add_comment_statuses.append(response.status)

        page.on("response", record_add_comment_response)
        page.evaluate(
            """() => {
              const originalRefreshClientConfig = refreshClientConfig;
              window.__pguRefreshClientConfigResults = [];
              refreshClientConfig = async () => {
                const result = await originalRefreshClientConfig();
                window.__pguRefreshClientConfigResults.push({
                  ...result,
                  token: window.PGU_TICKET_BOARD_WRITE_TOKEN,
                });
                return result;
              };
            }"""
        )
        page.evaluate("""() => { setCreateStatus('Ready.'); }""")
        page.evaluate("""() => { window.__pguNoReloadMarker = 'reactive-retry'; }""")
        comment_box.fill("Submit survives reactive token retry")
        modal.get_by_role("button", name="Add Comment").click()
        page.locator("#createStatus", has_text="Comment added to PGU-512.").wait_for(timeout=5000)
        wait_for_ticket_field(page, harness.url, "PGU-512", "ticket.comments.some((comment) => comment.text === 'Submit survives reactive token retry')")
        page.wait_for_function("() => window.__pguNoReloadMarker === 'reactive-retry'", timeout=5000)
        refresh_results = page.evaluate("() => window.__pguRefreshClientConfigResults || []")
        page.unroute("**/api/tickets/PGU-512/actions/add_comment", rotate_before_first_add_comment)
        page.remove_listener("response", record_add_comment_response)
        assert token_route_state["rotated"]
        assert 403 in add_comment_statuses, add_comment_statuses
        assert any(result.get("tokenChanged") for result in refresh_results), refresh_results
        comment_box = modal.get_by_placeholder("Add a comment or bounce-back note")
        page.wait_for_function(
            "(element) => element.value === ''",
            arg=comment_box.element_handle(timeout=5000),
            timeout=5000,
        )
        page.evaluate("""() => { window.PGU_TICKET_BOARD_REFRESH_IDLE_MS = 2500; state.lastActivityAt = Date.now(); }""")

        close_detail(page)
        modal = open_detail(page, "PGU-512")

        detail_field(modal, "Parent Ticket").locator("input").fill("PGU-502")
        detail_field(modal, "Parent Ticket").get_by_role("button", name="Save Parent Link").click()
        wait_for_ticket_field(page, harness.url, "PGU-512", "ticket.parent_id === 'PGU-502'")
        refresh_open_detail_from_server(page, "PGU-512")
        detail_field(modal, "Implementation").locator("textarea").fill("Implementation references PGU-503.")
        detail_field(modal, "Implementation").get_by_role("button", name="Save Implementation").click()
        wait_for_ticket_field(page, harness.url, "PGU-512", "ticket.implementation.includes('PGU-503')")
        refresh_open_detail_from_server(page, "PGU-512")
        detail_field(modal, "Audit Notes").locator("textarea").fill("Audit notes reference PGU-503.")
        detail_field(modal, "Audit Notes").get_by_role("button", name="Save Audit Notes").click()
        wait_for_ticket_field(page, harness.url, "PGU-512", "ticket.audit_prompt.includes('PGU-503')")
        refresh_open_detail_from_server(page, "PGU-512")
        modal.get_by_label("Requires UAT").check()
        wait_for_ticket_field(page, harness.url, "PGU-512", "ticket.needs_user_signoff")
        modal.get_by_label("Needs inspection").check()
        wait_for_ticket_field(page, harness.url, "PGU-512", "ticket.needs_inspection")
        modal.get_by_label("Regression").check()
        wait_for_ticket_field(page, harness.url, "PGU-512", "ticket.regression")
        wait_for_ticket_field(page, harness.url, "PGU-512", "ticket.needs_user_signoff && ticket.needs_inspection && ticket.regression && ticket.implementation.includes('PGU-503') && ticket.audit_prompt.includes('PGU-503')")
        refresh_open_detail_from_server(page, "PGU-512")
        modal.locator(".detail-ticket-headline strong", has_text="PGU-512").wait_for(timeout=5000)

        comment_box = modal.get_by_placeholder("Add a comment or bounce-back note")
        comment_box.fill("Playwright comment on PGU-512")
        urgent_checkbox = modal.get_by_label("Urgent")
        urgent_checkbox.check()
        page.wait_for_function(
            "(element) => element.checked === true",
            arg=urgent_checkbox.element_handle(timeout=5000),
            timeout=5000,
        )
        page.evaluate("""() => { rememberDetailDraft(); renderDetail(); }""")
        modal.locator(".detail-ticket-headline strong", has_text="PGU-512").wait_for(timeout=5000)
        comment_box = modal.get_by_placeholder("Add a comment or bounce-back note")
        page.wait_for_function(
            "(element) => element.value === 'Playwright comment on PGU-512'",
            arg=comment_box.element_handle(timeout=5000),
            timeout=5000,
        )
        urgent_checkbox = modal.get_by_label("Urgent")
        page.wait_for_function(
            "(element) => element.checked === true",
            arg=urgent_checkbox.element_handle(timeout=5000),
            timeout=5000,
        )
        modal.get_by_role("button", name="Add Comment").click()
        wait_for_ticket_field(page, harness.url, "PGU-512", "ticket.comments.some((comment) => comment.text === 'Playwright comment on PGU-512' && comment.urgent)")

        attach_panel = modal.locator(".detail-attach-panel")
        attach_panel.locator("[data-attach-set]").select_option("target")
        attach_panel.locator("input[type=file]").set_input_files(str(upload_source))
        modal.locator(".attachment-card", has_text="upload-source.png").wait_for(timeout=5000)
        wait_for_ticket_field(page, harness.url, "PGU-512", "ticket.screenshots.length === 1 && ticket.screenshots[0].includes('target__upload-source.png')")
        modal.locator(".attachment-card-clickable", has_text="upload-source.png").click()
        page.locator("#imageLightboxOverlay").wait_for(timeout=5000)
        page.locator("#imageLightboxCloseBtn").click()
        page.locator("#imageLightboxOverlay").wait_for(state="hidden", timeout=5000)
        modal.locator(".attachment-remove").click()
        wait_for_ticket_field(page, harness.url, "PGU-512", "ticket.screenshots.length === 0")

        close_detail(page)
        modal = open_detail(page, "PGU-507")
        modal.get_by_role("button", name="Sign Off").click()
        wait_for_ticket_field(page, harness.url, "PGU-507", "ticket.user_signoff && ticket.state === 'director_review'")
        page.locator(".card.card-signed-off", has=page.locator(".card-id", has_text="PGU-507")).wait_for(timeout=5000)

        close_detail(page)
        modal = open_detail(page, "PGU-508")
        modal.get_by_role("button", name="Advance -> Implementation").click()
        wait_for_ticket_field(page, harness.url, "PGU-508", "ticket.state === 'in_progress' && ticket.assignee === 'ops'")

        close_detail(page)
        modal = open_detail(page, "PGU-511")
        modal.get_by_placeholder("Add a comment or bounce-back note").fill("No longer needed.")
        modal.get_by_role("button", name="Cancel Ticket").click()
        wait_for_ticket_field(page, harness.url, "PGU-511", "ticket.state === 'cancelled' && ticket.comments.some((comment) => comment.text === 'No longer needed.')")

        close_detail(page)
        modal = open_detail(page, "PGU-502")
        modal.get_by_role("button", name="Advance -> Implementation").click()
        wait_for_ticket_field(page, harness.url, "PGU-502", "ticket.state === 'backlog' && ticket.assignee === 'app'")
        complete_task_from_browser(page, "PGU-501", "app", "Free app serial focus slot.")
        wait_for_ticket_field(page, harness.url, "PGU-502", "ticket.state === 'in_progress' && ticket.assignee === 'app'")

        ticket_512 = get_ticket(page, harness.url, "PGU-512")
        assert ticket_512["parent_id"] == "PGU-502", {"ticket": ticket_512, "actions": harness.app.action_log}
    finally:
        browser.close()


def run_mobile_audit(playwright: Any, harness: BoardHarness) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 390, "height": 740}, is_mobile=True)
        page.goto(harness.url, wait_until="domcontentloaded")
        page.locator("#createSectionToggle").wait_for(timeout=5000)
        page.wait_for_function(
            "() => document.getElementById('createSectionContent')?.hidden === true"
            " && document.getElementById('createSectionToggle')?.getAttribute('aria-expanded') === 'false'",
            timeout=5000,
        )
        page.locator("#createSectionToggle").click()
        assert page.locator("#createSectionContent").is_visible()
        page.locator(".column-head", has_text="Triage").first.click()
        page.locator(".column", has=page.locator(".column-title", has_text="Triage")).locator(".column-body").first.wait_for(state="visible")
        overflow = page.evaluate(
            """() => ({
              html: document.documentElement.scrollWidth - document.documentElement.clientWidth,
              body: document.body.scrollWidth - document.body.clientWidth,
              board: document.querySelector('.board-scroll').scrollWidth - document.querySelector('.board-scroll').clientWidth,
            })"""
        )
        assert overflow["html"] <= 1, overflow
        assert overflow["body"] <= 1, overflow
    finally:
        browser.close()


def main() -> int:
    try:
        sync_playwright = load_playwright()
    except ModuleNotFoundError:
        print(
            "ticket_board_ui_audit_test: skipped, missing Playwright Python package; "
            "install playwright and browser binaries to run the broad ticket-board UI audit"
        )
        return 0
    with BoardHarness(seed_tickets()) as harness:
        with sync_playwright() as playwright:
            run_desktop_audit(playwright, harness)
            run_mobile_audit(playwright, harness)
    print("ticket_board_ui_audit_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
