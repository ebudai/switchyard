"""Refresh-on-new-build and scroll restoration JavaScript.

When the server reports a build ID other than the one this page loaded, the
board reloads itself once it is safe: after the idle delay
(TICKET_BOARD_REFRESH_IDLE_MS, legacy PGU_TICKET_BOARD_REFRESH_IDLE_MS), with
no create draft, open detail or preview, and no unsaved detail edits. Until
then the refresh banner shows why it is waiting. The window and board scroll
positions survive the reload through sessionStorage, under the board's
namespace or the legacy PGU key. SCRIPT_CORE splices this in at its original
position; it uses SCRIPT_CORE's shared state, storage namespaces, create-form
inputs, banner element and detail, lightbox and body-modal helpers, and
SCRIPT_APP wires its events.
"""

from __future__ import annotations

SCRIPT_REFRESH = """    function normalizeBuildId(value) {
      return String(value || '').trim();
    }

    function refreshIdleMs() {
      const configured = window.TICKET_BOARD_REFRESH_IDLE_MS ?? window.PGU_TICKET_BOARD_REFRESH_IDLE_MS ?? 2500;
      const value = Number(configured);
      return Number.isFinite(value) && value >= 0 ? value : 2500;
    }

    function markUserActivity() {
      state.lastActivityAt = Date.now();
      scheduleAutoRefreshCheck();
    }

    function boardScrollElement() {
      return document.querySelector('.board-scroll');
    }

    function scrollPositionStorageKey(buildId) {
      return `${BOARD_STORAGE_NAMESPACE}:scroll:${buildId || 'latest'}`;
    }

    function legacyScrollPositionStorageKey(buildId) {
      return `${LEGACY_PGU_STORAGE_NAMESPACE}:scroll:${buildId || 'latest'}`;
    }

    function rememberScrollPositionForRefresh() {
      const boardScroll = boardScrollElement();
      const value = {
        windowX: window.scrollX,
        windowY: window.scrollY,
        boardLeft: boardScroll ? boardScroll.scrollLeft : 0,
        boardTop: boardScroll ? boardScroll.scrollTop : 0,
      };
      try {
        window.sessionStorage.setItem(scrollPositionStorageKey(state.serverBuildId), JSON.stringify(value));
      } catch (error) {
        // sessionStorage can be unavailable in private or locked-down browsers; refresh still works.
      }
    }

    function restoreScrollPositionAfterRefresh() {
      let raw = '';
      try {
        const key = scrollPositionStorageKey(state.loadedBuildId);
        raw = window.sessionStorage.getItem(key) || '';
        if (raw) {
          window.sessionStorage.removeItem(key);
        } else if (IS_LEGACY_PGU_BOARD) {
          const legacyKey = legacyScrollPositionStorageKey(state.loadedBuildId);
          raw = window.sessionStorage.getItem(legacyKey) || '';
          if (raw) {
            window.sessionStorage.removeItem(legacyKey);
          }
        }
      } catch (error) {
        return;
      }
      if (!raw) {
        return;
      }
      try {
        const value = JSON.parse(raw);
        window.scrollTo(Number(value.windowX) || 0, Number(value.windowY) || 0);
        const boardScroll = boardScrollElement();
        if (boardScroll) {
          boardScroll.scrollLeft = Number(value.boardLeft) || 0;
          boardScroll.scrollTop = Number(value.boardTop) || 0;
        }
      } catch (error) {
        // Ignore malformed persisted scroll state from an older page.
      }
    }

    function detailHasDirtyDraft() {
      rememberDetailDraft();
      return !!(state.detailDraft && Object.keys(state.detailDraft.fields || {}).length);
    }

    function createFormHasDraft() {
      return !!(
        titleInput.value.trim()
        || bodyInput.value.trim()
        || state.pendingCreateScreenshots.length
      );
    }

    function refreshUnsafeReason() {
      if (!state.refreshRequired) {
        return '';
      }
      if (Date.now() - state.lastActivityAt < refreshIdleMs()) {
        return 'waiting for idle';
      }
      if (createFormHasDraft()) {
        return 'new ticket draft';
      }
      if (detailModalIsOpen()) {
        return 'detail open';
      }
      if (imageLightboxIsOpen()) {
        return 'attachment preview open';
      }
      if (detailHasDirtyDraft()) {
        return 'unsaved detail edits';
      }
      return '';
    }

    function syncRefreshUpdateBanner(reason = '') {
      refreshUpdateBannerEl.hidden = !(state.refreshRequired && reason);
      if (typeof syncBodyModalState === 'function') {
        syncBodyModalState();
      }
    }

    function scheduleAutoRefreshCheck(delay = 250) {
      if (!state.refreshRequired) {
        return;
      }
      if (state.autoRefreshTimer) {
        window.clearTimeout(state.autoRefreshTimer);
      }
      state.autoRefreshTimer = window.setTimeout(() => {
        state.autoRefreshTimer = null;
        maybeAutoRefresh();
      }, delay);
    }

    function performSmartRefresh() {
      rememberDetailDraft();
      rememberScrollPositionForRefresh();
      state.autoRefreshHandledBuildIds.add(state.pendingRefreshBuildId);
      window.location.reload();
    }

    function maybeAutoRefresh() {
      if (!state.refreshRequired || !state.pendingRefreshBuildId) {
        return;
      }
      const reason = refreshUnsafeReason();
      syncRefreshUpdateBanner(reason);
      if (reason) {
        scheduleAutoRefreshCheck(reason === 'waiting for idle' ? Math.max(100, refreshIdleMs()) : 500);
        return;
      }
      performSmartRefresh();
    }

    function updateRefreshRequired(buildId) {
      const serverBuildId = normalizeBuildId(buildId);
      if (serverBuildId) {
        state.serverBuildId = serverBuildId;
      }
      const changed = !!(state.loadedBuildId && state.serverBuildId && state.loadedBuildId !== state.serverBuildId);
      const alreadyHandled = state.autoRefreshHandledBuildIds.has(state.serverBuildId);
      state.refreshRequired = changed && !alreadyHandled;
      state.pendingRefreshBuildId = state.refreshRequired ? state.serverBuildId : '';
      if (state.refreshRequired) {
        maybeAutoRefresh();
      } else {
        syncRefreshUpdateBanner('');
      }
    }

"""
