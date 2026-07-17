"""Board frontend API, event, and boot JavaScript."""

SCRIPT_APP = """    function uploadSetQuery(setOptions = {}) {
      const params = new URLSearchParams();
      const setKind = String(setOptions.set || '').trim();
      const label = String(setOptions.label || '').trim();
      const attempt = String(setOptions.attempt || '').trim();
      if (setKind) {
        params.set('set', setKind);
      }
      if (label) {
        params.set('label', label);
      }
      if (attempt) {
        params.set('attempt', attempt);
      }
      if (setOptions.filename) {
        params.set('filename', String(setOptions.filename));
      }
      const query = params.toString();
      return query ? `?${query}` : '';
    }

    async function uploadImageBlob(blob, setOptions = {}) {
      const headers = { 'Content-Type': blob.type || 'image/png' };
      if (window.PGU_TICKET_BOARD_WRITE_TOKEN) {
        headers['X-PGU-Write-Token'] = window.PGU_TICKET_BOARD_WRITE_TOKEN;
      }
      const response = await fetch(`/api/upload${uploadSetQuery(setOptions)}`, {
        method: 'POST',
        headers,
        body: blob,
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const payload = await response.json();
      return payload.image;
    }

    function readAttachmentSetOptions(root) {
      if (!root) {
        return {};
      }
      return {
        set: root.querySelector('[data-attach-set]')?.value || '',
        label: root.querySelector('[data-attach-label]')?.value || '',
        attempt: root.querySelector('[data-attach-attempt]')?.value || '',
      };
    }

    function readCreateAttachmentSetOptions() {
      return readAttachmentSetOptions(createAttachDropZone);
    }

    function imageFilesFromList(files) {
      return Array.from(files || []).filter((file) => String(file.type || '').startsWith('image/'));
    }

    async function attachImageFiles(files, context, setOptions = {}) {
      const imageFiles = imageFilesFromList(files);
      if (!imageFiles.length) {
        throw new Error('attach requires at least one image file');
      }
      const detailTicket = context === 'detail' ? selectedTicket() : null;
      if (context === 'detail' && !detailTicket) {
        throw new Error('no ticket selected for image attach');
      }
      const label = imageFiles.length === 1 ? 'image' : `${imageFiles.length} images`;
      setCreateStatus(`Uploading ${label}…`);
      const uploaded = [];
      for (const imageFile of imageFiles) {
        uploaded.push(await uploadImageBlob(imageFile, { ...setOptions, filename: imageFile.name || '' }));
      }
      const uploadedPaths = uploaded.map((item) => item.path);
      await requestBoardReload();
      if (context === 'detail') {
        const ticket = selectedTicket();
        if (!ticket || ticket.id !== detailTicket.id) {
          throw new Error('selected ticket changed during image attach');
        }
        setCreateStatus(`Attached ${label} to ${ticket.id}.`);
        await updateTicket(ticket.id, {
          screenshots: uniquePaths([...ticketScreenshotPaths(ticket), ...uploadedPaths]),
        }, 'director');
        setCreateStatus(`Attached ${label} to ${ticket.id}.`);
        return;
      }
      state.pendingCreateScreenshots = uniquePaths([...state.pendingCreateScreenshots, ...uploadedPaths]);
      renderCreatePreview();
      setCreateStatus(`Attached ${label} to the new ticket.`);
    }

    function wireImageDropZone(dropZone, context, getSetOptions = () => ({})) {
      if (!dropZone) {
        return;
      }
      const reset = () => dropZone.classList.remove('drag-active');
      dropZone.addEventListener('dragover', (event) => {
        const items = Array.from(event.dataTransfer?.items || []);
        const hasImage = items.some((item) => String(item.type || '').startsWith('image/'))
          || imageFilesFromList(event.dataTransfer?.files || []).length > 0;
        if (!hasImage) {
          return;
        }
        event.preventDefault();
        dropZone.classList.add('drag-active');
      });
      dropZone.addEventListener('dragleave', (event) => {
        if (!dropZone.contains(event.relatedTarget)) {
          reset();
        }
      });
      dropZone.addEventListener('drop', async (event) => {
        const files = imageFilesFromList(event.dataTransfer?.files || []);
        if (!files.length) {
          reset();
          return;
        }
        event.preventDefault();
        reset();
        try {
          await attachImageFiles(files, context, getSetOptions());
        } catch (error) {
          setCreateStatus(error.message, true);
          await requestBoardReload();
        }
      });
    }

    async function attachPastedImage(image, context) {
      const label = context === 'detail' ? 'Attaching pasted screenshot…' : 'Uploading pasted screenshot…';
      setCreateStatus(label);
      const uploaded = await uploadImageBlob(
        image,
        context === 'detail' ? readAttachmentSetOptions(detailContentEl) : readCreateAttachmentSetOptions(),
      );
      await requestBoardReload();
      if (context === 'detail') {
        const ticket = selectedTicket();
        if (!ticket) {
          throw new Error('no ticket selected for screenshot attach');
        }
        await updateTicket(ticket.id, {
          screenshots: uniquePaths([...ticketScreenshotPaths(ticket), uploaded.path]),
        }, 'director');
        setCreateStatus(`Attached pasted image to ${ticket.id}.`);
        return;
      }
      state.pendingCreateScreenshots = uniquePaths([...state.pendingCreateScreenshots, uploaded.path]);
      renderCreatePreview();
      setCreateStatus('Pasted image attached to the new ticket.');
    }

    function toggleControl(labelText, checked, onChange, options = {}) {
      const label = document.createElement('label');
      label.className = 'check';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = checked;
      input.disabled = !!options.disabled;
      if (options.title) {
        label.title = options.title;
      }
      input.addEventListener('change', async () => {
        const nextChecked = input.checked;
        const previousChecked = !nextChecked;
        try {
          await onChange(nextChecked);
        } catch (error) {
          input.checked = previousChecked;
          setCreateStatus(error.message, true);
          await requestBoardReload();
        }
      });
      label.append(input, document.createTextNode(labelText));
      return label;
    }

    async function loadBoard() {
      const loadSequence = state.loadSequence + 1;
      state.loadSequence = loadSequence;
      rememberDetailDraft();
      const response = await fetch('/api/board', { cache: 'no-store' });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const payload = await response.json();
      if (loadSequence !== state.loadSequence) {
        return;
      }
      state.tickets = payload.tickets;
      state.screenshots = payload.screenshots;
      state.errors = payload.errors;
      state.assignees = payload.assignees;
      state.callerRoles = payload.caller_roles || [];
      if (state.selectedId && !state.tickets.some((ticket) => ticket.id === state.selectedId)) {
        clearDetailDraft();
        state.selectedId = null;
        state.detailOpen = false;
        state.lightboxEntry = null;
      }
      storePathEl.textContent = payload.store_path;
      framePathEl.textContent = payload.frame_dir;
      refreshLineEl.textContent = formatWhen(payload.refreshed_at);
      populateCreateForm();
      renderCreateSection();
      renderErrors();
      renderStateVisibilityToggles();
      renderBoard();
      renderDetail();
      syncImageLightbox();
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
        initial_state: createDraftInput.checked ? 'draft' : (createBacklogInput.checked ? 'backlog' : 'analysis'),
        screenshot: state.pendingCreateScreenshots[0] || null,
        screenshots: state.pendingCreateScreenshots,
        needs_eric_signoff: needsEricInput.checked,
        needs_inspection: needsInspectionInput.checked,
        regression: createRegressionInput.checked,
      };
      const result = await postTicketAction('/api/tickets/actions/create_ticket', payload, 'director');
      titleInput.value = '';
      bodyInput.value = '';
      createDraftInput.checked = false;
      createBacklogInput.checked = false;
      needsEricInput.checked = false;
      needsInspectionInput.checked = false;
      createRegressionInput.checked = false;
      state.pendingCreateScreenshots = [];
      renderCreatePreview();
      setCreateStatus(`Created ${result.ticket.id}.`);
      await requestBoardReload();
    }

    function ticketWriteHeaders(callerRole = null) {
      const headers = { 'Content-Type': 'application/json' };
      if (window.PGU_TICKET_BOARD_WRITE_TOKEN) {
        headers['X-PGU-Write-Token'] = window.PGU_TICKET_BOARD_WRITE_TOKEN;
      }
      const normalizedCaller = callerRole ? callerRole.trim().toLowerCase() : '';
      if (normalizedCaller) {
        headers['X-PGU-Caller-Role'] = normalizedCaller;
      }
      return headers;
    }

    async function postTicketAction(path, payload, callerRole) {
      const response = await fetch(path, {
        method: 'POST',
        headers: ticketWriteHeaders(callerRole),
        body: JSON.stringify(payload || {}),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      return response.json();
    }

    function ticketForWrite(ticketId) {
      return state.tickets.find((ticket) => ticket.id === ticketId) || null;
    }

    function actionReason(patch) {
      if (patch.comment && patch.comment.text) {
        return patch.comment.text;
      }
      return patch.reason || patch.text || '';
    }

    function metadataPatch(patch) {
      const allowed = new Set([
        'title',
        'body',
        'parent_id',
        'screenshots',
        'screenshot',
        'implementation',
        'audit_prompt',
        'needs_inspection',
        'needs_eric_signoff',
        'commit_exempt',
        'regression',
        'commit_hash',
        'audit_signoff',
        'inspector_signoff',
        'eric_signoff',
      ]);
      const metadata = {};
      Object.entries(patch).forEach(([key, value]) => {
        if (allowed.has(key)) {
          metadata[key] = value;
        }
      });
      return metadata;
    }

    function savedDraftFieldsForPatch(patch, consumed) {
      const fieldKeys = {
        title: 'title',
        parent_id: 'parentId',
        blocked_reason: 'blockedReason',
        implementation: 'implementation',
        audit_prompt: 'auditPrompt',
        commit_hash: 'commitHash',
      };
      const savedFields = {};
      Object.entries(fieldKeys).forEach(([patchKey, draftKey]) => {
        if (Object.prototype.hasOwnProperty.call(patch, patchKey) && consumed.has(patchKey)) {
          savedFields[draftKey] = patch[patchKey];
        }
      });
      return savedFields;
    }

    async function updateTicketAction(ticketId, operation, payload, callerRole) {
      return postTicketAction(
        `/api/tickets/${encodeURIComponent(ticketId)}/actions/${encodeURIComponent(operation)}`,
        payload,
        callerRole,
      );
    }

    async function updateTicket(ticketId, patch, callerRole = null) {
      const normalizedCaller = callerRole ? callerRole.trim().toLowerCase() : '';
      if (!normalizedCaller) {
        throw new Error('ticket write requires a caller role');
      }
      const ticket = ticketForWrite(ticketId);
      const previousState = ticket ? ticket.state : '';
      const consumed = new Set();
      let consumedComment = false;

      if (Object.prototype.hasOwnProperty.call(patch, 'state')) {
        const nextState = String(patch.state || '').trim().toLowerCase();
        if (nextState === 'in_progress' && patch.comment && previousState === 'inspection') {
          const payload = { recommendations: actionReason(patch) };
          if (Object.prototype.hasOwnProperty.call(patch, 'assignee')) {
            payload.target_assignee = patch.assignee;
          }
          await updateTicketAction(ticketId, 'inspector_kick_back', payload, normalizedCaller);
          consumed.add('assignee');
          consumedComment = true;
        } else if (nextState === 'in_progress' && patch.comment && previousState === 'audit') {
          const payload = { reason: actionReason(patch) };
          if (Object.prototype.hasOwnProperty.call(patch, 'assignee')) {
            payload.target_assignee = patch.assignee;
          }
          await updateTicketAction(ticketId, 'audit_kick_back', payload, normalizedCaller);
          consumed.add('assignee');
          consumedComment = true;
        } else if (nextState === 'in_progress' && Object.prototype.hasOwnProperty.call(patch, 'assignee')) {
          await updateTicketAction(
            ticketId,
            'route',
            { state: nextState, assignee: patch.assignee },
            normalizedCaller,
          );
          consumed.add('assignee');
        } else if (nextState === 'in_progress') {
          await updateTicketAction(ticketId, 'start_work', {}, normalizedCaller);
        } else if (nextState === 'inspection') {
          await updateTicketAction(
            ticketId,
            'submit_to_audit',
            { commit_hash: patch.commit_hash || ticket?.commit_hash || '' },
            normalizedCaller,
          );
          consumed.add('commit_hash');
        } else if (nextState === 'audit') {
          if (previousState === 'inspection') {
            await updateTicketAction(ticketId, 'inspector_sign_off', {}, normalizedCaller);
            consumed.add('inspector_signoff');
          } else {
            await updateTicketAction(
              ticketId,
              'submit_to_audit',
              { commit_hash: patch.commit_hash || ticket?.commit_hash || '' },
              normalizedCaller,
            );
            consumed.add('commit_hash');
          }
        } else if (nextState === 'done') {
          await updateTicketAction(
            ticketId,
            'mark_done',
            { commit_hash: patch.commit_hash || ticket?.commit_hash || '' },
            normalizedCaller,
          );
          consumed.add('commit_hash');
        } else if (nextState === 'backlog') {
          await updateTicketAction(ticketId, 'defer', {}, normalizedCaller);
        } else if (nextState === 'cancelled') {
          await updateTicketAction(ticketId, 'cancel', { reason: actionReason(patch) }, normalizedCaller);
          consumedComment = true;
        } else if (nextState === 'analysis' && previousState === 'draft') {
          await updateTicketAction(ticketId, 'release_draft', {}, normalizedCaller);
        } else if (
          nextState === 'analysis' &&
          ['eric_review', 'director_review', 'done'].includes(previousState)
        ) {
          await updateTicketAction(ticketId, 'eric_reopen', { reason: actionReason(patch) }, normalizedCaller);
          consumedComment = true;
        } else if (['director_review', 'eric_review'].includes(nextState) && previousState === 'audit') {
          await updateTicketAction(ticketId, 'audit_sign_off', { text: actionReason(patch) }, normalizedCaller);
          consumed.add('audit_signoff');
          consumedComment = true;
        } else if (nextState === 'director_review' && previousState === 'eric_review') {
          await updateTicketAction(ticketId, 'eric_sign_off', { text: actionReason(patch) }, normalizedCaller);
          consumed.add('eric_signoff');
          consumedComment = !!actionReason(patch);
        } else {
          await updateTicketAction(
            ticketId,
            'route',
            { state: nextState, assignee: patch.assignee || ticket?.assignee || '' },
            normalizedCaller,
          );
          consumed.add('assignee');
        }
        consumed.add('state');
      } else if (Object.prototype.hasOwnProperty.call(patch, 'assignee')) {
        await updateTicketAction(
          ticketId,
          'route',
          { state: ticket?.state || '', assignee: patch.assignee },
          normalizedCaller,
        );
        consumed.add('assignee');
      }

      if (Object.prototype.hasOwnProperty.call(patch, 'blocked_by') || Object.prototype.hasOwnProperty.call(patch, 'blocked_reason')) {
        await updateTicketAction(
          ticketId,
          'set_blockers',
          {
            blocked_by: patch.blocked_by || ticket?.blocked_by || [],
            blocked_reason: Object.prototype.hasOwnProperty.call(patch, 'blocked_reason') ? patch.blocked_reason : ticket?.blocked_reason || '',
          },
          normalizedCaller,
        );
        consumed.add('blocked_by');
        consumed.add('blocked_reason');
      }

      if (Object.prototype.hasOwnProperty.call(patch, 'manually_controlled')) {
        await updateTicketAction(ticketId, 'set_manually_controlled', { manually_controlled: patch.manually_controlled }, normalizedCaller);
        consumed.add('manually_controlled');
      }

      if (patch.audit_signoff === true && !consumed.has('audit_signoff')) {
        await updateTicketAction(ticketId, 'audit_sign_off', { text: actionReason(patch) }, normalizedCaller);
        consumed.add('audit_signoff');
        consumedComment = true;
      }
      if (patch.inspector_signoff === true && !consumed.has('inspector_signoff')) {
        await updateTicketAction(ticketId, 'inspector_sign_off', {}, normalizedCaller);
        consumed.add('inspector_signoff');
      }
      if (patch.eric_signoff === true && !consumed.has('eric_signoff')) {
        await updateTicketAction(ticketId, 'eric_sign_off', { text: actionReason(patch) }, normalizedCaller);
        consumed.add('eric_signoff');
        consumedComment = !!actionReason(patch);
      }

      if (patch.comment && !consumedComment) {
        await updateTicketAction(ticketId, 'add_comment', { text: patch.comment.text || '', urgent: !!patch.comment.urgent }, normalizedCaller);
        consumed.add('comment');
      }

      const editable = metadataPatch(patch);
      consumed.forEach((key) => delete editable[key]);
      if (Object.keys(editable).length) {
        await updateTicketAction(ticketId, 'edit_fields', editable, normalizedCaller);
        Object.keys(editable).forEach((key) => consumed.add(key));
      }
      markDetailDraftFieldsSaved(ticketId, savedDraftFieldsForPatch(patch, consumed));
      await requestBoardReload();
    }

    async function mergeTicket(sourceTicketId, targetTicketId) {
      const result = await postTicketAction(
        `/api/tickets/${encodeURIComponent(sourceTicketId)}/actions/merge`,
        { target_id: targetTicketId },
        'director',
      );
      clearDetailDraft(sourceTicketId);
      state.selectedId = result.target.id;
      await requestBoardReload();
      setCreateStatus(`Merged ${sourceTicketId} into ${targetTicketId}.`);
    }

    async function submitComment(ticketId, who, text, nextState = null, urgent = false) {
      const trimmedWho = who.trim();
      const trimmedText = text.trim();
      if (!trimmedWho) {
        throw new Error('comment requires an author');
      }
      if (!trimmedText && !nextState) {
        throw new Error('comment requires non-empty text');
      }
      const patch = {};
      if (trimmedText) {
        patch.comment = {
          who: trimmedWho,
          text: trimmedText,
          urgent: !!urgent,
        };
      }
      if (nextState) {
        patch.state = nextState;
      }
      clearDetailDraft(ticketId);
      await updateTicket(ticketId, patch, trimmedWho);
      setCreateStatus(nextState ? `Moved ${ticketId} to ${stateLabel(nextState)}.` : `Comment added to ${ticketId}.`);
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
      await updateTicket(ticketId, patch, trimmedWho);
      setCreateStatus(`Signed off ✓ ${ticketId} moved to Final Sign-Off.`);
    }

    async function handleCreateSubmit() {
      try {
        await createTicket();
      } catch (error) {
        setCreateStatus(error.message, true);
      }
    }

    function submitCreateOnEnter(event) {
      if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) {
        return;
      }
      event.preventDefault();
      void handleCreateSubmit();
    }

    createBtn.addEventListener('click', handleCreateSubmit);
    createAttachImageBtn.addEventListener('click', () => {
      createImageInput.click();
    });
    createImageInput.addEventListener('change', async () => {
      try {
        await attachImageFiles(createImageInput.files, 'create', readCreateAttachmentSetOptions());
      } catch (error) {
        setCreateStatus(error.message, true);
      } finally {
        createImageInput.value = '';
      }
    });
    wireImageDropZone(createAttachDropZone, 'create', readCreateAttachmentSetOptions);
    titleInput.addEventListener('keydown', submitCreateOnEnter);
    assigneeInput.addEventListener('keydown', submitCreateOnEnter);
    createDraftInput.addEventListener('keydown', submitCreateOnEnter);
    createBacklogInput.addEventListener('keydown', submitCreateOnEnter);
    createRegressionInput.addEventListener('keydown', submitCreateOnEnter);

    createSectionToggleEl.addEventListener('click', () => {
      toggleSection('new_ticket');
    });

    showDeferredInput.addEventListener('change', () => {
      state.showDeferred = showDeferredInput.checked;
      renderStateVisibilityToggles();
      renderBoard();
    });

    showDoneInput.addEventListener('change', () => {
      state.showDone = showDoneInput.checked;
      renderStateVisibilityToggles();
      renderBoard();
    });

    showCancelledInput.addEventListener('change', () => {
      state.showCancelled = showCancelledInput.checked;
      renderStateVisibilityToggles();
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

    imageLightboxCloseBtn.addEventListener('click', () => {
      closeImageLightbox();
    });

    imageLightboxOverlayEl.addEventListener('click', (event) => {
      if (event.target === imageLightboxOverlayEl) {
        closeImageLightbox();
      }
    });

    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') {
        return;
      }
      if (imageLightboxIsOpen()) {
        closeImageLightbox();
        return;
      }
      if (state.detailOpen) {
        closeDetail();
      }
    });

    const handleMobileSectionMediaChange = () => {
      renderCreateSection();
      renderBoard();
    };
    if (typeof mobileSectionMedia.addEventListener === 'function') {
      mobileSectionMedia.addEventListener('change', handleMobileSectionMediaChange);
    } else if (typeof mobileSectionMedia.addListener === 'function') {
      mobileSectionMedia.addListener(handleMobileSectionMediaChange);
    }

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
