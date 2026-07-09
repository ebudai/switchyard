"""Board frontend API, event, and boot JavaScript."""

SCRIPT_APP = """    async function uploadImageBlob(blob) {
      const response = await fetch('/api/upload', {
        method: 'POST',
        headers: { 'Content-Type': blob.type || 'image/png' },
        body: blob,
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const payload = await response.json();
      return payload.image;
    }

    async function attachPastedImage(image, context) {
      const label = context === 'detail' ? 'Attaching pasted screenshot…' : 'Uploading pasted screenshot…';
      setCreateStatus(label);
      const uploaded = await uploadImageBlob(image);
      await requestBoardReload();
      if (context === 'detail') {
        const ticket = selectedTicket();
        if (!ticket) {
          throw new Error('no ticket selected for screenshot attach');
        }
        await updateTicket(ticket.id, {
          screenshots: uniquePaths([...ticketScreenshotPaths(ticket), uploaded.path]),
        });
        setCreateStatus(`Attached pasted image to ${ticket.id}.`);
        return;
      }
      state.pendingCreateScreenshots = uniquePaths([...state.pendingCreateScreenshots, uploaded.path]);
      ensureScreenshotOption(uploaded.path, `${uploaded.name} - ${uploaded.modified}`);
      screenshotInput.value = uploaded.path;
      renderCreatePreview();
      setCreateStatus('Pasted image attached to the new ticket.');
    }

    function toggleControl(labelText, checked, onChange) {
      const label = document.createElement('label');
      label.className = 'check';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = checked;
      input.addEventListener('change', async () => {
        await onChange(input.checked);
      });
      label.append(input, document.createTextNode(labelText));
      return label;
    }

    async function loadBoard() {
      rememberDetailDraft();
      const response = await fetch('/api/board', { cache: 'no-store' });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const payload = await response.json();
      state.tickets = payload.tickets;
      state.screenshots = payload.screenshots;
      state.errors = payload.errors;
      state.assignees = payload.assignees;
      if (state.selectedId && !state.tickets.some((ticket) => ticket.id === state.selectedId)) {
        clearDetailDraft();
        state.selectedId = null;
        state.detailOpen = false;
      }
      storePathEl.textContent = payload.store_path;
      framePathEl.textContent = payload.frame_dir;
      refreshLineEl.textContent = formatWhen(payload.refreshed_at);
      populateCreateForm();
      renderErrors();
      renderDoneToggle();
      renderBoard();
      renderDetail();
    }

    async function requestBoardReload() {
      if (state.loadInFlight) {
        state.loadQueued = true;
        return state.loadInFlight;
      }
      state.loadInFlight = (async () => {
        try {
          await loadBoard();
        } finally {
          state.loadInFlight = null;
          if (state.loadQueued) {
            state.loadQueued = false;
            void requestBoardReload();
          }
        }
      })();
      return state.loadInFlight;
    }

    function renderErrors() {
      if (!state.errors.length) {
        errorBoxEl.hidden = true;
        errorBoxEl.textContent = '';
        return;
      }
      errorBoxEl.hidden = false;
      errorBoxEl.textContent = '';
      const heading = document.createElement('strong');
      heading.textContent = 'Store read errors';
      errorBoxEl.appendChild(heading);
      state.errors.forEach((item) => {
        errorBoxEl.appendChild(document.createElement('br'));
        errorBoxEl.appendChild(document.createTextNode(`${item.file}: ${item.error}`));
      });
    }

    async function createTicket() {
      const payload = {
        title: titleInput.value,
        body: bodyInput.value,
        assignee: assigneeInput.value,
        screenshot: state.pendingCreateScreenshots[0] || null,
        screenshots: state.pendingCreateScreenshots,
        needs_eric_signoff: needsEricInput.checked,
      };
      const response = await fetch('/api/tickets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const result = await response.json();
      titleInput.value = '';
      bodyInput.value = '';
      needsEricInput.checked = false;
      state.pendingCreateScreenshots = [];
      screenshotInput.value = '';
      renderCreatePreview();
      setCreateStatus(`Created ${result.ticket.id}.`);
      await requestBoardReload();
    }

    async function updateTicket(ticketId, patch) {
      const response = await fetch(`/api/tickets/${ticketId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      await requestBoardReload();
    }

    async function mergeTicket(sourceTicketId, targetTicketId) {
      const response = await fetch(`/api/tickets/${sourceTicketId}/merge`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_id: targetTicketId, actor: 'director' }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const result = await response.json();
      clearDetailDraft(sourceTicketId);
      state.selectedId = result.target.id;
      await requestBoardReload();
      setCreateStatus(`Merged ${sourceTicketId} into ${targetTicketId}.`);
    }

    async function submitComment(ticketId, who, text, nextState = null) {
      const trimmedWho = who.trim();
      const trimmedText = text.trim();
      if (!trimmedWho || !trimmedText) {
        throw new Error('comment requires both who and text');
      }
      const patch = {
        comment: {
          who: trimmedWho,
          text: trimmedText,
        },
      };
      if (nextState) {
        patch.state = nextState;
      }
      clearDetailDraft(ticketId);
      await updateTicket(ticketId, patch);
    }

    async function submitEricSignoff(ticketId, who, text) {
      const patch = { eric_signoff: true };
      const trimmedWho = who.trim();
      const trimmedText = text.trim();
      if (trimmedWho && trimmedText) {
        patch.comment = {
          who: trimmedWho,
          text: trimmedText,
        };
      }
      clearDetailDraft(ticketId);
      await updateTicket(ticketId, patch);
      setCreateStatus(`Signed off ✓ ${ticketId} is now waiting for director completion.`);
    }

    createBtn.addEventListener('click', async () => {
      try {
        await createTicket();
      } catch (error) {
        setCreateStatus(error.message, true);
      }
    });

    addCreateAttachmentBtn.addEventListener('click', () => {
      if (!screenshotInput.value) {
        return;
      }
      state.pendingCreateScreenshots = uniquePaths([...state.pendingCreateScreenshots, screenshotInput.value]);
      renderCreatePreview();
    });

    showDoneInput.addEventListener('change', () => {
      state.showDone = showDoneInput.checked;
      renderDoneToggle();
      renderBoard();
    });

    detailCloseBtn.addEventListener('click', () => {
      closeDetail();
    });

    detailOverlayEl.addEventListener('click', (event) => {
      if (event.target === detailOverlayEl) {
        closeDetail();
      }
    });

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && state.detailOpen) {
        closeDetail();
      }
    });

    document.addEventListener('paste', async (event) => {
      const items = Array.from(event.clipboardData?.items || []);
      const imageItem = items.find((item) => item.type.startsWith('image/'));
      if (!imageItem) {
        return;
      }
      const blob = imageItem.getAsFile();
      if (!blob) {
        return;
      }
      event.preventDefault();
      try {
        const activeElement = document.activeElement;
        const useDetail = !!(activeElement && detailContentEl.contains(activeElement) && selectedTicket());
        await attachPastedImage(blob, useDetail ? 'detail' : 'create');
      } catch (error) {
        setCreateStatus(error.message, true);
      }
    });

    function connectEvents() {
      if (!window.EventSource) {
        setCreateStatus('EventSource unavailable; live updates disabled.', true);
        return;
      }
      if (state.eventReconnectTimer) {
        window.clearTimeout(state.eventReconnectTimer);
        state.eventReconnectTimer = null;
      }
      if (state.eventSource) {
        state.eventSource.close();
      }
      const eventSource = new EventSource('/events');
      state.eventSource = eventSource;
      eventSource.addEventListener('board', () => {
        void requestBoardReload();
      });
      eventSource.onerror = () => {
        if (state.eventSource !== eventSource) {
          return;
        }
        eventSource.close();
        state.eventSource = null;
        state.eventReconnectTimer = window.setTimeout(() => {
          state.eventReconnectTimer = null;
          connectEvents();
        }, 1500);
      };
    }

    async function boot() {
      try {
        await requestBoardReload();
        setCreateStatus('Ready.');
      } catch (error) {
        setCreateStatus(error.message, true);
      }
      connectEvents();
    }

    window.addEventListener('beforeunload', () => {
      if (state.eventReconnectTimer) {
        window.clearTimeout(state.eventReconnectTimer);
      }
      if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
      }
    });

    boot();
"""
