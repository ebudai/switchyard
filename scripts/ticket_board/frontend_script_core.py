"""Core board frontend JavaScript helpers and board rendering."""

SCRIPT_CORE = """    const TICKET_REF_PATTERN = /\\b(PGU-\\d+)\\b/ig;
    const COLUMNS = [
      { key: 'backlog', label: 'Backlog' },
      { key: 'analysis', label: 'Analysis' },
      { key: 'in_progress', label: 'Implementation' },
      { key: 'inspection', label: 'Inspection' },
      { key: 'audit', label: 'Audit' },
      { key: 'eric_review', label: 'Eric Review' },
      { key: 'director_review', label: 'Ready' },
      { key: 'done', label: 'Done' },
      { key: 'cancelled', label: 'Cancelled' },
    ];

    const state = {
      tickets: [],
      screenshots: [],
      errors: [],
      assignees: [],
      selectedId: null,
      detailOpen: false,
      showDeferred: false,
      showDone: false,
      showCancelled: false,
      detailDraft: null,
      eventSource: null,
      eventReconnectTimer: null,
      loadInFlight: null,
      loadQueued: false,
      pendingCreateScreenshots: [],
      mobileSectionOpen: {},
    };

    const mobileSectionMedia = window.matchMedia('(max-width: 900px)');
    const boardEl = document.getElementById('board');
    const assigneeInput = document.getElementById('assigneeInput');
    const screenshotInput = document.getElementById('screenshotInput');
    const addCreateAttachmentBtn = document.getElementById('addCreateAttachmentBtn');
    const createPreviewEl = document.getElementById('createPreview');
    const createPreviewGalleryEl = document.getElementById('createPreviewGallery');
    const titleInput = document.getElementById('titleInput');
    const bodyInput = document.getElementById('bodyInput');
    const needsEricInput = document.getElementById('needsEricInput');
    const needsInspectionInput = document.getElementById('needsInspectionInput');
    const showDeferredInput = document.getElementById('showDeferredInput');
    const showDeferredCountEl = document.getElementById('showDeferredCount');
    const showDoneInput = document.getElementById('showDoneInput');
    const showDoneCountEl = document.getElementById('showDoneCount');
    const showCancelledInput = document.getElementById('showCancelledInput');
    const showCancelledCountEl = document.getElementById('showCancelledCount');
    const createBtn = document.getElementById('createBtn');
    const createStatus = document.getElementById('createStatus');
    const createSectionToggleEl = document.getElementById('createSectionToggle');
    const createSectionCountEl = document.getElementById('createSectionCount');
    const createSectionContentEl = document.getElementById('createSectionContent');
    const storePathEl = document.getElementById('storePath');
    const framePathEl = document.getElementById('framePath');
    const refreshLineEl = document.getElementById('refreshLine');
    const errorBoxEl = document.getElementById('errorBox');
    const detailOverlayEl = document.getElementById('detailOverlay');
    const detailModalTitleEl = document.getElementById('detailModalTitle');
    const detailCloseBtn = document.getElementById('detailCloseBtn');
    const detailContentEl = document.getElementById('detailContent');

    function formatWhen(raw) {
      const date = new Date(raw);
      return Number.isNaN(date.getTime()) ? raw : date.toLocaleString();
    }

    function setCreateStatus(text, isError = false) {
      createStatus.textContent = text;
      createStatus.style.borderColor = isError ? 'rgba(253,164,175,0.35)' : 'var(--border)';
      createStatus.style.color = isError ? '#fecdd3' : 'var(--text)';
    }

    function stateLabel(key) {
      return COLUMNS.find((column) => column.key === key)?.label || key;
    }

    function mobileSectionsEnabled() {
      return mobileSectionMedia.matches;
    }

    function sectionIsOpen(key) {
      if (!mobileSectionsEnabled()) {
        return true;
      }
      if (!(key in state.mobileSectionOpen)) {
        state.mobileSectionOpen[key] = false;
      }
      return state.mobileSectionOpen[key];
    }

    function toggleSection(key) {
      if (!mobileSectionsEnabled()) {
        return;
      }
      state.mobileSectionOpen[key] = !sectionIsOpen(key);
      if (key === 'new_ticket') {
        renderCreateSection();
        return;
      }
      renderBoard();
    }

    function defaultAdvanceState(ticket) {
      if (ticket.state === 'backlog') {
        return 'analysis';
      }
      if (ticket.state === 'analysis') {
        return 'in_progress';
      }
      if (ticket.state === 'in_progress') {
        return ticket.needs_inspection ? 'inspection' : 'audit';
      }
      if (ticket.state === 'inspection') {
        return 'audit';
      }
      if (ticket.state === 'audit') {
        return ticket.needs_eric_signoff ? 'eric_review' : 'director_review';
      }
      if (ticket.state === 'eric_review') {
        return 'director_review';
      }
      if (ticket.state === 'director_review') {
        return 'done';
      }
      return null;
    }

    function stateTransitionCallerRole(ticket) {
      if (ticket.state === 'inspection') {
        return 'inspector';
      }
      return ticket.state === 'in_progress'
        ? ticket.assignee
        : 'director';
    }

    function advanceBlockedReason(ticket) {
      const nextState = defaultAdvanceState(ticket);
      if (!nextState) {
        return 'No default advance from this state.';
      }
      if (ticket.state === 'analysis') {
        if (ticket.assignee === 'unassigned') {
          return 'Assign the ticket before advancing to Implementation.';
        }
        if (!(ticket.implementation || '').trim()) {
          return 'Save implementation before advancing to Implementation.';
        }
      }
      if (ticket.state === 'inspection' && !ticket.inspector_signoff) {
        return 'Record inspector signoff before advancing to audit.';
      }
      if (ticket.state === 'audit' && !ticket.audit_signoff) {
        return ticket.needs_eric_signoff
          ? 'Set audit signoff before advancing to Eric review.'
          : 'Set audit signoff before advancing to Ready.';
      }
      if (ticket.state === 'eric_review' && ticket.needs_eric_signoff && !ticket.eric_signoff) {
        return 'Record Eric signoff before advancing to Ready.';
      }
      if (nextState === 'done' && !ticket.commit_exempt && !(ticket.commit_hash || '').trim()) {
        return 'Save a verified commit hash or enable no-commit override before advancing to done.';
      }
      return '';
    }

    function ticketBlockedReason(ticket) {
      return (ticket.blocked_reason || '').trim();
    }

    function manualBlockedSummary(ticket) {
      const reason = ticketBlockedReason(ticket);
      const unresolved = unresolvedBlockedBy(ticket);
      if (reason && unresolved.length) {
        return `${reason} (blocked by ${formatBlockedByList(unresolved)})`;
      }
      if (reason) {
        return reason;
      }
      if (unresolved.length) {
        return `Blocked by ${formatBlockedByList(unresolved)}. Add a blocked reason.`;
      }
      return '';
    }

    function ericSignoffSummary(ticket) {
      if (!ticket.needs_eric_signoff || !ticket.eric_signoff) {
        return '';
      }
      if (ticket.state === 'eric_review') {
        return 'Eric signed off. Waiting for Ready.';
      }
      if (ticket.state === 'director_review') {
        return 'Eric signed off. In Ready.';
      }
      return '';
    }
    function cardAlert(kind, title, text) {
      const alert = document.createElement('div');
      alert.className = `alert card-alert card-alert-${kind}`;
      const strong = document.createElement('strong');
      strong.textContent = title;
      const body = document.createElement('div');
      body.textContent = text;
      alert.append(strong, body);
      return alert;
    }

    function alertStackForTicket(ticket) {
      const alerts = [];
      const signoffSummary = ericSignoffSummary(ticket);
      if (signoffSummary) {
        alerts.push({ kind: 'ok', title: 'Signed Off', text: signoffSummary });
      }
      const blockedSummary = manualBlockedSummary(ticket);
      if (blockedSummary) {
        alerts.push({ kind: 'blocked', title: 'Blocked', text: blockedSummary });
      }
      return alerts;
    }

    function renderAlertStack(ticket, { detail = false } = {}) {
      const alerts = alertStackForTicket(ticket);
      if (!alerts.length) {
        return null;
      }
      const stack = document.createElement('div');
      stack.className = 'alert-stack';
      alerts.forEach((item) => {
        const alert = detail
          ? document.createElement('div')
          : cardAlert(item.kind, item.title, item.text);
        if (detail) {
          alert.className = `alert alert-${item.kind}`;
          const strong = document.createElement('strong');
          strong.textContent = item.title;
          const body = document.createElement('div');
          body.textContent = item.text;
          alert.append(strong, body);
        }
        stack.appendChild(alert);
      });
      return stack;
    }

    async function advanceTicket(ticketId) {
      const ticket = state.tickets.find((item) => item.id === ticketId);
      if (!ticket) {
        throw new Error(`ticket not found: ${ticketId}`);
      }
      const nextState = defaultAdvanceState(ticket);
      if (!nextState) {
        throw new Error('no default advance from this state');
      }
      const blockedReason = advanceBlockedReason(ticket);
      if (blockedReason) {
        throw new Error(blockedReason);
      }
      await updateTicket(ticketId, { state: nextState }, stateTransitionCallerRole(ticket));
    }

    async function cancelTicket(ticketId, who, text) {
      const trimmedWho = who.trim();
      const trimmedText = text.trim();
      if (!trimmedWho || !trimmedText) {
        throw new Error('cancellation requires a non-empty reason');
      }
      clearDetailDraft(ticketId);
      await updateTicket(ticketId, {
        state: 'cancelled',
        comment: {
          who: trimmedWho,
          text: trimmedText,
        },
      }, trimmedWho);
      setCreateStatus(`Cancelled ${ticketId}.`);
    }

    function clearDetailDraft(ticketId = null) {
      if (!ticketId || state.detailDraft?.ticketId === ticketId) {
        state.detailDraft = null;
      }
    }

    function syncDetailOverlay() {
      const ticket = selectedTicket();
      const isOpen = !!(state.detailOpen && ticket);
      detailOverlayEl.hidden = !isOpen;
      document.body.classList.toggle('detail-open', isOpen);
      detailModalTitleEl.textContent = ticket ? `${ticket.id} - ${ticket.title}` : 'Select a ticket';
    }

    function openDetail(ticketId) {
      if (state.selectedId !== ticketId) {
        clearDetailDraft();
      }
      state.selectedId = ticketId;
      state.detailOpen = true;
      renderBoard();
      renderDetail();
    }

    function closeDetail() {
      const ticketId = state.selectedId;
      clearDetailDraft(ticketId);
      state.selectedId = null;
      state.detailOpen = false;
      renderBoard();
      renderDetail();
    }

    function rememberDetailDraft() {
      if (!state.detailOpen) {
        return;
      }
      const ticket = selectedTicket();
      if (!ticket) {
        clearDetailDraft();
        return;
      }
      const fields = {};
      let activeKey = null;
      detailContentEl.querySelectorAll('[data-draft-key]').forEach((element) => {
        const key = element.dataset.draftKey;
        if (!key) {
          return;
        }
        const value = element.value ?? '';
        const serverValue = element.dataset.serverValue ?? '';
        const isFocused = element === document.activeElement;
        const isDirty = value !== serverValue;
        if (!isDirty && !isFocused) {
          return;
        }
        const entry = { value };
        if (typeof element.selectionStart === 'number' && typeof element.selectionEnd === 'number') {
          entry.selectionStart = element.selectionStart;
          entry.selectionEnd = element.selectionEnd;
        }
        fields[key] = entry;
        if (isFocused) {
          activeKey = key;
        }
      });
      state.detailDraft = Object.keys(fields).length ? { ticketId: ticket.id, fields, activeKey } : null;
    }

    function bindDetailDraftField(fields, element, key, serverValue) {
      element.dataset.draftKey = key;
      element.dataset.serverValue = serverValue ?? '';
      fields.set(key, element);
    }

    function restoreDetailDraft(ticketId, fields) {
      const draft = state.detailDraft;
      if (!draft || draft.ticketId !== ticketId) {
        return;
      }
      Object.entries(draft.fields).forEach(([key, entry]) => {
        const element = fields.get(key);
        if (!element) {
          return;
        }
        element.value = entry.value;
        if (draft.activeKey === key) {
          element.focus();
          if (typeof entry.selectionStart === 'number' && typeof entry.selectionEnd === 'number') {
            element.setSelectionRange(entry.selectionStart, entry.selectionEnd);
          }
        }
      });
    }

    function ticketAllowsKickback(ticket) {
      return ['director_review', 'audit', 'eric_review'].includes(ticket.state);
    }

    function ticketIsEricReview(ticket) {
      return ticket.state === 'eric_review';
    }

    function firstNonEmptyLine(text) {
      if (!text) {
        return '';
      }
      return text
        .split(/\\r?\\n/)
        .map((line) => line.trim())
        .find((line) => line.length > 0) || '';
    }

    function ericReviewCheckText(ticket) {
      return firstNonEmptyLine(ticket.implementation)
        || firstNonEmptyLine(ticket.body)
        || 'Review this ticket, then sign off when it looks right.';
    }

    function checklistItemsFromText(text) {
      if (!text) {
        return [];
      }
      return text
        .split(/\\r?\\n/)
        .map((line) => {
          const match = line.match(/^\\s*[-*]\\s+\\[(?: |x|X)\\]\\s+(.+)$/);
          return match ? match[1].trim() : '';
        })
        .filter((item) => item.length > 0);
    }

    function summarizeTextBlock(text, maxLines = 3) {
      if (!text) {
        return '';
      }
      const lines = text
        .split(/\\r?\\n/)
        .map((line) => line.trim())
        .filter((line) => line.length > 0 && !/^[-*]\\s+\\[(?: |x|X)\\]\\s+/.test(line));
      return lines.slice(0, maxLines).join('\\n');
    }

    function ericReviewSummarySections(ticket) {
      const fields = [
        ['Implementation', ticket.implementation],
        ['Body', ticket.body],
        ['Audit Prompt', ticket.audit_prompt],
      ];
      return fields
        .map(([label, text]) => {
          const checklist = checklistItemsFromText(text);
          if (checklist.length) {
            return { label, checklist };
          }
          const summary = summarizeTextBlock(text);
          if (summary) {
            return { label, summary };
          }
          return null;
        })
        .filter((section) => !!section);
    }

    function ericReviewStatusItems(ticket) {
      return [
        { label: 'Audit sign-off', ok: !!ticket.audit_signoff },
        { label: 'Inspector sign-off', ok: !ticket.needs_inspection || !!ticket.inspector_signoff },
        { label: 'Needs Eric sign-off', ok: !!ticket.needs_eric_signoff },
        {
          label: 'Commit evidence',
          ok: !!ticket.commit_exempt || !!(ticket.commit_hash || '').trim(),
          okText: ticket.commit_exempt ? 'exempt' : 'ready',
          missingText: 'missing',
        },
      ];
    }

    function parseBlockedByInput(value) {
      return Array.from(new Set(
        value
          .split(/[^A-Za-z0-9-]+/)
          .map((item) => item.trim().toUpperCase())
          .filter((item) => item.length > 0),
      ));
    }

    function formatBlockedByList(blockedBy) {
      return (blockedBy || []).join(', ');
    }

    function blockerTicket(id) {
      return state.tickets.find((ticket) => ticket.id === id) || null;
    }

    function unresolvedBlockedBy(ticket) {
      return (ticket.blocked_by || []).filter((id) => {
        const blocker = blockerTicket(id);
        return !blocker || blocker.state !== 'done';
      });
    }

    function blockedBySummary(ticket) {
      const blockedBy = ticket.blocked_by || [];
      if (!blockedBy.length) {
        return 'No ticket blockers recorded.';
      }
      const unresolved = unresolvedBlockedBy(ticket);
      if (!unresolved.length) {
        return `Resolved blockers: ${formatBlockedByList(blockedBy)}`;
      }
      return `Unresolved blockers: ${formatBlockedByList(unresolved)}`;
    }

    function buildOption(select, value, label) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    }

    function previewUrlFor(path) {
      return `/api/image/${encodeURIComponent(path)}`;
    }

    function ticketScreenshotEntries(ticket) {
      if (ticket.screenshots_info && ticket.screenshots_info.length) {
        return ticket.screenshots_info;
      }
      if (ticket.screenshots && ticket.screenshots.length) {
        return ticket.screenshots.map((path) => ({ path, available: true }));
      }
      if (ticket.screenshot) {
        return [{ path: ticket.screenshot, available: !!ticket.screenshot_available }];
      }
      return [];
    }

    function ticketScreenshotPaths(ticket) {
      return ticketScreenshotEntries(ticket).map((entry) => entry.path);
    }

    function uniquePaths(paths) {
      return Array.from(new Set((paths || []).filter((path) => !!path)));
    }

    function ensureScreenshotOption(path, label = null) {
      if (!path) {
        return;
      }
      const exists = Array.from(screenshotInput.options).some((option) => option.value === path);
      if (!exists) {
        buildOption(screenshotInput, path, label || path.split('/').pop());
      }
    }

    function screenshotLabelFor(path) {
      const shot = state.screenshots.find((item) => item.path === path);
      return shot ? `${shot.name} - ${shot.modified}` : path.split('/').pop();
    }

    function screenshotEntriesForPaths(paths) {
      return uniquePaths(paths).map((path) => {
        const ticketEntry = state.tickets
          .flatMap((ticket) => ticketScreenshotEntries(ticket))
          .find((entry) => entry.path === path);
        return {
          path,
          available: ticketEntry ? ticketEntry.available : true,
          label: screenshotLabelFor(path),
        };
      });
    }

    function renderAttachmentGallery(container, entries, removeLabel, onRemove) {
      container.innerHTML = '';
      entries.forEach((entry) => {
        const card = document.createElement('div');
        card.className = 'attachment-card';
        if (onRemove) {
          const removeButton = document.createElement('button');
          removeButton.type = 'button';
          removeButton.className = 'attachment-remove';
          removeButton.textContent = '×';
          removeButton.title = removeLabel;
          removeButton.addEventListener('click', async (event) => {
            event.preventDefault();
            event.stopPropagation();
            await onRemove(entry.path);
          });
          card.appendChild(removeButton);
        }
        if (entry.available) {
          const image = document.createElement('img');
          image.className = 'attachment-thumb';
          image.src = previewUrlFor(entry.path);
          image.alt = entry.path;
          card.appendChild(image);
        } else {
          const missing = document.createElement('div');
          missing.className = 'attachment-missing';
          missing.textContent = 'image unavailable';
          card.appendChild(missing);
        }
        const meta = document.createElement('div');
        meta.className = 'attachment-meta';
        meta.textContent = entry.label;
        card.appendChild(meta);
        container.appendChild(card);
      });
    }

    function renderCreatePreview() {
      if (!state.pendingCreateScreenshots.length) {
        createPreviewEl.hidden = true;
        createPreviewGalleryEl.innerHTML = '';
        return;
      }
      createPreviewEl.hidden = false;
      renderAttachmentGallery(
        createPreviewGalleryEl,
        screenshotEntriesForPaths(state.pendingCreateScreenshots),
        'Remove attachment',
        async (path) => {
          state.pendingCreateScreenshots = state.pendingCreateScreenshots.filter((item) => item !== path);
          renderCreatePreview();
        },
      );
    }

    function populateCreateForm() {
      const assigneeValue = assigneeInput.value;
      const screenshotValue = screenshotInput.value;
      assigneeInput.innerHTML = '';
      state.assignees.forEach((assignee) => buildOption(assigneeInput, assignee, assignee));
      if (assigneeValue && Array.from(assigneeInput.options).some((option) => option.value === assigneeValue)) {
        assigneeInput.value = assigneeValue;
      }
      screenshotInput.innerHTML = '';
      buildOption(screenshotInput, '', '(none)');
      state.screenshots.forEach((shot) => buildOption(screenshotInput, shot.path, `${shot.name} - ${shot.modified}`));
      state.pendingCreateScreenshots.forEach((path) => ensureScreenshotOption(path));
      if (screenshotValue) {
        ensureScreenshotOption(screenshotValue);
        screenshotInput.value = screenshotValue;
      }
      renderCreatePreview();
    }

    function badge(text, isOk) {
      const span = document.createElement('span');
      span.className = `badge ${isOk ? 'ok' : 'bad'}`;
      span.textContent = text;
      return span;
    }

    function ticketById(ticketId) {
      const normalizedId = String(ticketId || '').toUpperCase();
      return state.tickets.find((ticket) => ticket.id === normalizedId) || null;
    }

    function buildTicketReference(ticketId) {
      const normalizedId = String(ticketId || '').toUpperCase();
      const reference = document.createElement('button');
      reference.type = 'button';
      reference.className = 'ticket-ref';
      reference.textContent = normalizedId;
      if (ticketById(normalizedId)) {
        reference.addEventListener('click', (event) => {
          event.preventDefault();
          event.stopPropagation();
          openDetail(normalizedId);
        });
      } else {
        reference.disabled = true;
        reference.classList.add('ticket-ref-missing');
        reference.title = 'Ticket not found in the current board snapshot.';
      }
      return reference;
    }

    function buildChildTicketList(children, { compact = false } = {}) {
      const wrap = document.createElement('div');
      wrap.className = compact ? 'child-ticket-list child-ticket-list-compact' : 'child-ticket-list';

      const head = document.createElement('div');
      head.className = 'child-ticket-head';
      head.textContent = children.length === 1 ? '1 linked child' : `${children.length} linked children`;
      wrap.appendChild(head);

      const list = document.createElement('div');
      list.className = 'child-ticket-items';
      children.forEach((child) => {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'child-ticket-item';
        if (state.selectedId === child.id) {
          row.classList.add('selected');
        }
        row.addEventListener('click', (event) => {
          event.preventDefault();
          event.stopPropagation();
          openDetail(child.id);
        });

        const text = document.createElement('div');
        text.className = 'child-ticket-text';
        const id = document.createElement('div');
        id.className = 'child-ticket-id';
        id.textContent = child.id;
        const title = document.createElement('div');
        title.className = 'child-ticket-title';
        title.textContent = child.title;
        text.append(id, title);

        const stateChip = document.createElement('span');
        stateChip.className = 'tag child-ticket-state';
        stateChip.textContent = stateLabel(child.state);
        row.append(text, stateChip);
        list.appendChild(row);
      });
      wrap.appendChild(list);
      return wrap;
    }

    function appendLinkedTicketText(container, text) {
      const source = text || '';
      const lines = source.split(/\\r?\\n/);
      lines.forEach((line, lineIndex) => {
        let cursor = 0;
        TICKET_REF_PATTERN.lastIndex = 0;
        let match = TICKET_REF_PATTERN.exec(line);
        while (match) {
          if (match.index > cursor) {
            container.appendChild(document.createTextNode(line.slice(cursor, match.index)));
          }
          container.appendChild(buildTicketReference(match[1]));
          cursor = match.index + match[1].length;
          match = TICKET_REF_PATTERN.exec(line);
        }
        if (cursor < line.length) {
          container.appendChild(document.createTextNode(line.slice(cursor)));
        }
        if (lineIndex < lines.length - 1) {
          container.appendChild(document.createElement('br'));
        }
      });
    }

    function linkedTextBlock(text, emptyText = '(none)') {
      const block = document.createElement('div');
      block.className = 'body-text linked-text';
      appendLinkedTicketText(block, text && text.length ? text : emptyText);
      return block;
    }

    function linkedPreview(label, text, emptyText = '(none)') {
      const preview = document.createElement('div');
      preview.className = 'field-preview';
      const previewLabel = document.createElement('div');
      previewLabel.className = 'field-preview-label';
      previewLabel.textContent = label;
      preview.append(previewLabel, linkedTextBlock(text, emptyText));
      return preview;
    }

    function linkedTicketRow(ticketIds) {
      const row = document.createElement('div');
      row.className = 'ticket-ref-row';
      if (!ticketIds.length) {
        const empty = document.createElement('div');
        empty.className = 'soft-note';
        empty.textContent = 'No linked tickets.';
        row.appendChild(empty);
        return row;
      }
      ticketIds.forEach((ticketId) => {
        row.appendChild(buildTicketReference(ticketId));
      });
      return row;
    }

    function ticketNumber(ticketId) {
      const match = /^PGU-(\\d+)$/.exec(ticketId || '');
      return match ? Number.parseInt(match[1], 10) : Number.MAX_SAFE_INTEGER;
    }

    function compareTicketsOldestFirst(left, right) {
      const createdCompare = (left.created || '').localeCompare(right.created || '');
      if (createdCompare !== 0) {
        return createdCompare;
      }
      return ticketNumber(left.id) - ticketNumber(right.id);
    }

    function normalizedParentId(ticket) {
      return String(ticket?.parent_id || '').trim().toUpperCase();
    }

    function parentTicketFor(ticket) {
      const parentId = normalizedParentId(ticket);
      return parentId ? ticketById(parentId) : null;
    }

    function rootTicketForBoard(ticket) {
      let current = ticket;
      const seen = new Set([ticket.id]);
      while (true) {
        const parent = parentTicketFor(current);
        if (!parent || seen.has(parent.id)) {
          return current;
        }
        seen.add(parent.id);
        current = parent;
      }
    }

    function directChildTickets(ticket) {
      return state.tickets
        .filter((candidate) => normalizedParentId(candidate) === ticket.id)
        .sort(compareTicketsOldestFirst);
    }

    function childTicketsForBoard(ticket) {
      const seen = new Set();
      const collected = [];
      const visit = (parent) => {
        directChildTickets(parent).forEach((child) => {
          if (seen.has(child.id)) {
            return;
          }
          seen.add(child.id);
          collected.push(child);
          visit(child);
        });
      };
      visit(ticket);
      return collected;
    }

    function isTopLevelBoardTicket(ticket) {
      return rootTicketForBoard(ticket).id === ticket.id;
    }

    function doneTicketsForBoard() {
      return state.tickets
        .filter((ticket) => ticket.state === 'done')
        .sort(compareTicketsOldestFirst);
    }

    function doneRootTicketsForBoard() {
      const seen = new Set();
      const roots = [];
      doneTicketsForBoard().forEach((ticket) => {
        const root = rootTicketForBoard(ticket);
        if (seen.has(root.id)) {
          return;
        }
        seen.add(root.id);
        roots.push(root);
      });
      return roots.sort(compareTicketsOldestFirst);
    }

    function columnTicketCount(columnKey) {
      if (columnKey === 'done') {
        return doneTicketsForBoard().length;
      }
      return state.tickets
        .filter((ticket) => (
          ticket.state === columnKey
          && (columnKey !== 'eric_review' || ticket.needs_eric_signoff)
          && isTopLevelBoardTicket(ticket)
        ))
        .length;
    }

    function columnTickets(columnKey) {
      if (columnKey === 'done') {
        return doneRootTicketsForBoard();
      }
      return state.tickets
        .filter((ticket) => (
          ticket.state === columnKey
          && (columnKey !== 'eric_review' || ticket.needs_eric_signoff)
          && isTopLevelBoardTicket(ticket)
        ))
        .sort(compareTicketsOldestFirst);
    }

    function visibleColumns() {
      return COLUMNS.filter((column) => {
        if (column.key === 'backlog' && !state.showDeferred) {
          return false;
        }
        if (column.key === 'done' && !state.showDone) {
          return false;
        }
        if (column.key === 'cancelled' && !state.showCancelled) {
          return false;
        }
        return true;
      });
    }

    function renderStateVisibilityToggles() {
      const deferredCount = columnTicketCount('backlog');
      const doneCount = columnTicketCount('done');
      const cancelledCount = columnTicketCount('cancelled');
      showDeferredInput.checked = state.showDeferred;
      showDeferredCountEl.textContent = `(${deferredCount})`;
      showDoneInput.checked = state.showDone;
      showDoneCountEl.textContent = `(${doneCount})`;
      showCancelledInput.checked = state.showCancelled;
      showCancelledCountEl.textContent = `(${cancelledCount})`;
    }

    function renderCreateSection() {
      if (!createSectionToggleEl || !createSectionContentEl) {
        return;
      }
      const isOpen = sectionIsOpen('new_ticket');
      createSectionToggleEl.setAttribute('aria-expanded', String(isOpen));
      createSectionContentEl.hidden = !isOpen;
      if (createSectionCountEl) {
        createSectionCountEl.hidden = true;
        createSectionCountEl.textContent = '';
      }
    }

    function renderBoard() {
      boardEl.innerHTML = '';
      visibleColumns().forEach((column) => {
        const columnEl = document.createElement('section');
        columnEl.className = 'column';
        const tickets = columnTickets(column.key);
        const ticketCount = columnTicketCount(column.key);
        const sectionKey = `column:${column.key}`;
        const isOpen = sectionIsOpen(sectionKey);
        const head = document.createElement(mobileSectionsEnabled() ? 'button' : 'div');
        head.className = 'column-head';
        if (mobileSectionsEnabled()) {
          head.type = 'button';
          head.setAttribute('aria-expanded', String(isOpen));
          head.addEventListener('click', () => {
            toggleSection(sectionKey);
          });
        }
        const titleWrap = document.createElement('div');
        titleWrap.className = 'mobile-section-title-wrap';
        const title = document.createElement('div');
        title.className = 'column-title mobile-section-title';
        title.textContent = column.label;
        titleWrap.appendChild(title);
        if (ticketCount) {
          const inlineCount = document.createElement('span');
          inlineCount.className = 'mobile-section-count';
          inlineCount.textContent = `(${ticketCount})`;
          titleWrap.appendChild(inlineCount);
        }
        const count = document.createElement('div');
        count.className = 'count';
        count.textContent = ticketCount;
        count.hidden = ticketCount === 0;
        const chevron = document.createElement('span');
        chevron.className = 'mobile-section-chevron';
        chevron.setAttribute('aria-hidden', 'true');
        chevron.textContent = '▾';
        head.append(titleWrap, count, chevron);
        columnEl.appendChild(head);
        const body = document.createElement('div');
        body.className = 'column-body';
        body.hidden = !isOpen;
        if (!tickets.length) {
          const empty = document.createElement('div');
          empty.className = 'empty';
          empty.textContent = column.key === 'eric_review'
            ? 'Only tickets flagged for Eric signoff appear here.'
            : 'No tickets in this state.';
          body.appendChild(empty);
        }
        tickets.forEach((ticket) => body.appendChild(renderCard(ticket)));
        columnEl.appendChild(body);
        boardEl.appendChild(columnEl);
      });
    }

    function renderCard(ticket) {
      const childTickets = childTicketsForBoard(ticket);
      const doneChildren = childTickets.filter((child) => child.state === 'done');
      const card = document.createElement('article');
      card.className = 'card';
      if (manualBlockedSummary(ticket)) {
        card.classList.add('card-blocked');
      }
      if (ericSignoffSummary(ticket)) {
        card.classList.add('card-signed-off');
      }
      if (ticket.active_work_highlight) {
        card.classList.add('card-active-work');
      }
      if (ticket.id === state.selectedId || childTickets.some((child) => child.id === state.selectedId)) {
        card.classList.add('selected');
      }
      card.addEventListener('click', () => openDetail(ticket.id));

      const top = document.createElement('div');
      top.className = 'card-top';
      const titleWrap = document.createElement('div');
      const idEl = document.createElement('div');
      idEl.className = 'card-id';
      idEl.textContent = ticket.id;
      const titleEl = document.createElement('div');
      titleEl.className = 'card-title';
      titleEl.textContent = ticket.title;
      const assigneeLine = document.createElement('div');
      assigneeLine.className = 'card-assignee';
      const assigneeLabel = document.createElement('span');
      assigneeLabel.className = 'card-assignee-label';
      assigneeLabel.textContent = 'assignee';
      const assigneeValue = document.createElement('span');
      assigneeValue.className = 'card-assignee-value';
      assigneeValue.textContent = ticket.assignee;
      assigneeLine.append(assigneeLabel, assigneeValue);
      titleWrap.append(idEl, titleEl, assigneeLine);
      if (ticket.needs_eric_signoff && ticket.state === 'eric_review') {
        const signoffState = document.createElement('div');
        signoffState.className = `card-signoff-state ${ticket.eric_signoff ? 'signed' : 'pending'}`;
        signoffState.textContent = ticket.eric_signoff ? 'Signed Off ✓' : 'Awaiting Sign-off';
        titleWrap.appendChild(signoffState);
      }
      top.appendChild(titleWrap);

      const tags = document.createElement('div');
      tags.className = 'tag-row';
      const stateTag = document.createElement('span');
      stateTag.className = 'tag';
      stateTag.textContent = stateLabel(ticket.state);
      tags.append(stateTag);

      const badges = document.createElement('div');
      badges.className = 'badge-row';
      badges.appendChild(badge(`audit ${ticket.audit_signoff ? '✓' : '✗'}`, ticket.audit_signoff));
      const unresolvedBlockers = unresolvedBlockedBy(ticket);
      if (unresolvedBlockers.length) {
        badges.appendChild(badge(
          unresolvedBlockers.length === 1 ? `blocked ${unresolvedBlockers[0]}` : `blocked ${unresolvedBlockers.length}`,
          false,
        ));
      }
      if (ticket.needs_eric_signoff) {
        badges.appendChild(badge(`eric ${ticket.eric_signoff ? '✓' : '✗'}`, ticket.eric_signoff));
      }
      if (doneChildren.length) {
        badges.appendChild(badge(
          doneChildren.length === 1 ? 'done child 1' : `done children ${doneChildren.length}`,
          true,
        ));
      }

      const controls = document.createElement('div');
      controls.className = 'control-row';
      const nextState = defaultAdvanceState(ticket);
      if (nextState) {
        const advanceButton = document.createElement('button');
        advanceButton.className = 'small primary';
        advanceButton.type = 'button';
        advanceButton.textContent = `Advance -> ${stateLabel(nextState)}`;
        const blockedReason = advanceBlockedReason(ticket);
        if (blockedReason) {
          advanceButton.disabled = true;
          advanceButton.title = blockedReason;
        }
        advanceButton.addEventListener('click', async (event) => {
          event.stopPropagation();
          try {
            await advanceTicket(ticket.id);
          } catch (error) {
            setCreateStatus(error.message, true);
            await requestBoardReload();
          }
        });
        controls.appendChild(advanceButton);
      }
      const stateSelect = document.createElement('select');
      stateSelect.className = 'small';
      COLUMNS.forEach((column) => {
        const option = document.createElement('option');
        option.value = column.key;
        option.textContent = column.label;
        stateSelect.appendChild(option);
      });
      stateSelect.value = ticket.state;
      stateSelect.addEventListener('click', (event) => event.stopPropagation());
      stateSelect.addEventListener('change', async (event) => {
        const nextState = event.target.value;
        try {
          await updateTicket(ticket.id, { state: nextState }, stateTransitionCallerRole(ticket));
        } catch (error) {
          setCreateStatus(error.message, true);
          await requestBoardReload();
        }
      });
      controls.appendChild(stateSelect);

      card.append(top, tags, badges);
      const alerts = renderAlertStack(ticket);
      if (alerts) {
        card.appendChild(alerts);
      }
      if (childTickets.length) {
        card.appendChild(buildChildTicketList(childTickets, { compact: true }));
      }
      if (ticketScreenshotEntries(ticket).some((entry) => !entry.available)) {
        const missing = document.createElement('div');
        missing.className = 'soft-note';
        missing.textContent = 'image unavailable';
        card.appendChild(missing);
      }
      card.appendChild(controls);
      return card;
    }
"""
