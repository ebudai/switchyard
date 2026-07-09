"""Static frontend for the ticket-board browser UI."""

HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>PGU Ticket Board</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {
      color-scheme: dark;
      --bg: #0f1115;
      --panel: #171a20;
      --panel-2: #1d232c;
      --panel-3: #222936;
      --border: #2f3744;
      --text: #edf2f7;
      --muted: #9aa5b1;
      --accent: #7dd3fc;
      --accent-soft: rgba(125, 211, 252, 0.14);
      --ok: #86efac;
      --warn: #fcd34d;
      --bad: #fda4af;
      font-family: Inter, system-ui, sans-serif;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; background: var(--bg); color: var(--text); }
    body { min-height: 100vh; }
    .layout {
      min-height: 100vh;
      display: grid;
      grid-template-columns: 300px minmax(0, 1fr);
    }
    aside, .detail {
      background: var(--panel);
      min-height: 100vh;
      overflow: auto;
    }
    aside { border-right: 1px solid var(--border); }
    .detail {
      grid-column: 1 / -1;
      min-height: 0;
      border-top: 1px solid var(--border);
    }
    .shell {
      min-width: 0;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }
    .topbar {
      padding: 16px 18px;
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 16px;
      align-items: start;
      background: rgba(15, 17, 21, 0.94);
      position: sticky;
      top: 0;
      z-index: 3;
    }
    .topbar h1, .panel-head h2, .detail-head h2 { margin: 0; font-size: 18px; }
    .subtle, .meta, .hint { color: var(--muted); font-size: 13px; line-height: 1.45; }
    .paths {
      text-align: right;
      max-width: min(100%, 34rem);
      margin-left: auto;
    }
    .panel-head, .detail-head {
      padding: 16px 18px;
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      background: rgba(23, 26, 32, 0.96);
      z-index: 2;
    }
    .panel-body, .detail-body {
      padding: 16px 18px;
      display: grid;
      gap: 14px;
      align-content: start;
    }
    label {
      display: grid;
      gap: 6px;
      font-size: 13px;
      color: var(--muted);
    }
    input, textarea, select, button {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel-2);
      color: var(--text);
      padding: 10px 12px;
      font: inherit;
    }
    textarea { min-height: 120px; resize: vertical; }
    button { cursor: pointer; }
    button.primary {
      background: var(--accent-soft);
      border-color: rgba(125, 211, 252, 0.45);
    }
    button.small, select.small {
      width: auto;
      min-width: 0;
      padding: 7px 9px;
      font-size: 12px;
    }
    .check {
      display: flex;
      gap: 10px;
      align-items: center;
      font-size: 13px;
      color: var(--text);
    }
    .check input {
      width: 16px;
      height: 16px;
      margin: 0;
      accent-color: var(--accent);
    }
    .paste-hint {
      border: 1px dashed var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      background: rgba(255,255,255,0.02);
    }
    .preview-card {
      display: grid;
      gap: 8px;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px;
      background: rgba(255,255,255,0.03);
    }
    .preview-card[hidden] { display: none; }
    .preview-thumb {
      width: 100%;
      max-height: 180px;
      object-fit: contain;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: #0b0d11;
    }
    .preview-meta {
      font-size: 12px;
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .inline-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .inline-actions button {
      width: auto;
      min-width: 0;
    }
    .comment-composer {
      display: grid;
      gap: 10px;
      margin-bottom: 12px;
    }
    .board-scroll {
      overflow: auto;
      padding: 18px;
    }
    .board {
      min-width: 1320px;
      display: grid;
      grid-template-columns: repeat(6, minmax(205px, 1fr));
      gap: 16px;
      align-items: start;
    }
    .column {
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--border);
      border-radius: 8px;
      min-height: 320px;
      display: grid;
      grid-template-rows: auto minmax(180px, 1fr);
    }
    .column-head {
      padding: 12px 14px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: center;
    }
    .column-title { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0; }
    .count {
      min-width: 22px;
      text-align: center;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      background: rgba(255,255,255,0.06);
      color: var(--muted);
    }
    .column-body {
      padding: 12px;
      display: grid;
      gap: 10px;
      align-content: start;
    }
    .card {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel-2);
      padding: 12px;
      display: grid;
      gap: 10px;
      cursor: pointer;
    }
    .card.selected {
      border-color: rgba(125, 211, 252, 0.55);
      box-shadow: 0 0 0 1px rgba(125, 211, 252, 0.3) inset;
    }
    .card-top {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: start;
    }
    .card-id {
      color: var(--accent);
      font-size: 12px;
      font-weight: 600;
    }
    .card-title {
      font-size: 14px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .tag-row, .badge-row, .control-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .tag, .badge {
      border-radius: 999px;
      padding: 4px 8px;
      font-size: 12px;
      line-height: 1;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.04);
    }
    .tag { color: var(--text); }
    .badge.ok { color: var(--ok); border-color: rgba(134,239,172,0.35); }
    .badge.bad { color: var(--bad); border-color: rgba(253,164,175,0.35); }
    .soft-note {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .missing-image-box {
      border: 1px dashed var(--border);
      border-radius: 8px;
      padding: 14px;
      background: rgba(255,255,255,0.02);
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    .empty {
      padding: 14px;
      border: 1px dashed var(--border);
      border-radius: 8px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    .status, .error-box, .detail-box {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: rgba(255,255,255,0.03);
      padding: 12px;
      font-size: 13px;
      line-height: 1.5;
    }
    .error-box { border-color: rgba(253,164,175,0.35); color: #fecdd3; }
    .detail-box {
      display: grid;
      gap: 12px;
      width: min(100%, 76ch);
    }
    .detail-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .field-label { font-size: 12px; color: var(--muted); text-transform: uppercase; }
    .body-text {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-size: 14px;
      line-height: 1.5;
    }
    .detail-image {
      width: 100%;
      max-height: 360px;
      object-fit: contain;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: #0b0d11;
    }
    .comment-list {
      display: grid;
      gap: 8px;
    }
    .comment {
      border-left: 2px solid var(--border);
      padding-left: 10px;
    }
    @media (min-width: 1800px) {
      .layout { grid-template-columns: 300px minmax(0, 1fr) minmax(420px, 32rem); }
      .detail {
        grid-column: auto;
        min-height: 100vh;
        border-top: 0;
        border-left: 1px solid var(--border);
      }
      .board {
        min-width: 1260px;
        grid-template-columns: repeat(6, minmax(200px, 1fr));
      }
    }
    @media (max-width: 1200px) {
      .detail-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 980px) {
      .board { min-width: 980px; }
    }
    @media (max-width: 900px) {
      .board {
        min-width: 0;
        grid-template-columns: 1fr;
      }
      .paths {
        text-align: left;
        margin-left: 0;
      }
    }
    @media (max-width: 980px) {
      .layout { grid-template-columns: 1fr; }
      aside, .detail { min-height: 0; border-right: 0; border-left: 0; }
      aside { border-bottom: 1px solid var(--border); }
      .topbar { position: static; }
    }
  </style>
</head>
<body>
  <div class="layout">
    <aside>
      <div class="panel-head">
        <h2>New Ticket</h2>
        <div class="subtle">Local JSON board on localhost. Director can edit the same ticket files from the shell.</div>
      </div>
      <div class="panel-body">
        <label>
          Title
          <input id="titleInput" type="text" maxlength="200" placeholder="Short issue title">
        </label>
        <label>
          Body
          <textarea id="bodyInput" placeholder="Details, repro steps, notes"></textarea>
        </label>
        <label>
          Assignee
          <select id="assigneeInput"></select>
        </label>
        <label>
          Screenshot
          <select id="screenshotInput"></select>
        </label>
        <div class="paste-hint">Paste an image from the clipboard here to upload and attach it.</div>
        <div id="createPreview" class="preview-card" hidden>
          <img id="createPreviewImage" class="preview-thumb" alt="Pasted screenshot preview">
          <div id="createPreviewMeta" class="preview-meta"></div>
        </div>
        <label class="check">
          <input id="needsEricInput" type="checkbox">
          Needs Eric signoff
        </label>
        <button id="createBtn" class="primary">Create Ticket</button>
        <div id="createStatus" class="status">Ready.</div>
      </div>
    </aside>

    <section class="shell">
      <div class="topbar">
        <div>
          <h1>PGU Ticket Board</h1>
          <div class="subtle">States: open -> in_progress -> director_review -> audit -> eric_review -> done</div>
        </div>
        <div class="paths meta">
          <div><strong>Store</strong>: <span id="storePath">(loading)</span></div>
          <div><strong>Frames</strong>: <span id="framePath">(loading)</span></div>
          <div><strong>Refresh</strong>: <span id="refreshLine">(loading)</span></div>
        </div>
      </div>
      <div class="board-scroll">
        <div id="errorBox" class="error-box" hidden></div>
        <div class="board" id="board"></div>
      </div>
    </section>

    <section class="detail">
      <div class="detail-head">
        <h2>Ticket Detail</h2>
        <div class="subtle">Click a card to inspect the full body and attached frame.</div>
      </div>
      <div class="detail-body">
        <div id="detailContent" class="detail-box">
          <div class="meta">No ticket selected.</div>
        </div>
      </div>
    </section>
  </div>

  <script>
    const COLUMNS = [
      { key: 'open', label: 'Open' },
      { key: 'in_progress', label: 'In Progress' },
      { key: 'director_review', label: 'Director Review' },
      { key: 'audit', label: 'Audit' },
      { key: 'eric_review', label: 'Eric Review' },
      { key: 'done', label: 'Done' },
    ];

    const state = {
      tickets: [],
      screenshots: [],
      errors: [],
      assignees: [],
      selectedId: null,
      eventSource: null,
      eventReconnectTimer: null,
      loadInFlight: null,
      loadQueued: false,
      pendingCreateScreenshot: null,
    };

    const boardEl = document.getElementById('board');
    const assigneeInput = document.getElementById('assigneeInput');
    const screenshotInput = document.getElementById('screenshotInput');
    const createPreviewEl = document.getElementById('createPreview');
    const createPreviewImageEl = document.getElementById('createPreviewImage');
    const createPreviewMetaEl = document.getElementById('createPreviewMeta');
    const titleInput = document.getElementById('titleInput');
    const bodyInput = document.getElementById('bodyInput');
    const needsEricInput = document.getElementById('needsEricInput');
    const createBtn = document.getElementById('createBtn');
    const createStatus = document.getElementById('createStatus');
    const storePathEl = document.getElementById('storePath');
    const framePathEl = document.getElementById('framePath');
    const refreshLineEl = document.getElementById('refreshLine');
    const errorBoxEl = document.getElementById('errorBox');
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

    function ticketAllowsKickback(ticket) {
      return ['director_review', 'audit', 'eric_review'].includes(ticket.state);
    }

    function ticketIsEricReview(ticket) {
      return ticket.state === 'eric_review';
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

    function ensureScreenshotOption(path, label = null) {
      if (!path) {
        return;
      }
      const exists = Array.from(screenshotInput.options).some((option) => option.value === path);
      if (!exists) {
        buildOption(screenshotInput, path, label || path.split('/').pop());
      }
    }

    function renderCreatePreview() {
      const path = state.pendingCreateScreenshot || screenshotInput.value;
      if (!path) {
        createPreviewEl.hidden = true;
        createPreviewImageEl.removeAttribute('src');
        createPreviewMetaEl.textContent = '';
        return;
      }
      createPreviewEl.hidden = false;
      createPreviewImageEl.src = previewUrlFor(path);
      createPreviewMetaEl.textContent = path;
    }

    function populateCreateForm() {
      const assigneeValue = assigneeInput.value;
      const screenshotValue = state.pendingCreateScreenshot || screenshotInput.value;
      assigneeInput.innerHTML = '';
      state.assignees.forEach((assignee) => buildOption(assigneeInput, assignee, assignee));
      if (assigneeValue && Array.from(assigneeInput.options).some((option) => option.value === assigneeValue)) {
        assigneeInput.value = assigneeValue;
      }
      screenshotInput.innerHTML = '';
      buildOption(screenshotInput, '', '(none)');
      state.screenshots.forEach((shot) => buildOption(screenshotInput, shot.path, `${shot.name} - ${shot.modified}`));
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

    function columnTickets(columnKey) {
      return state.tickets.filter((ticket) => ticket.state === columnKey && (columnKey !== 'eric_review' || ticket.needs_eric_signoff));
    }

    function renderBoard() {
      boardEl.innerHTML = '';
      COLUMNS.forEach((column) => {
        const columnEl = document.createElement('section');
        columnEl.className = 'column';
        const tickets = columnTickets(column.key);
        columnEl.innerHTML = `
          <div class="column-head">
            <div class="column-title">${column.label}</div>
            <div class="count">${tickets.length}</div>
          </div>
        `;
        const body = document.createElement('div');
        body.className = 'column-body';
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
      const card = document.createElement('article');
      card.className = 'card';
      if (ticket.id === state.selectedId) {
        card.classList.add('selected');
      }
      card.addEventListener('click', () => {
        state.selectedId = ticket.id;
        renderBoard();
        renderDetail();
      });

      const top = document.createElement('div');
      top.className = 'card-top';
      const titleWrap = document.createElement('div');
      const idEl = document.createElement('div');
      idEl.className = 'card-id';
      idEl.textContent = ticket.id;
      const titleEl = document.createElement('div');
      titleEl.className = 'card-title';
      titleEl.textContent = ticket.title;
      titleWrap.append(idEl, titleEl);
      top.appendChild(titleWrap);

      const tags = document.createElement('div');
      tags.className = 'tag-row';
      const assignee = document.createElement('span');
      assignee.className = 'tag';
      assignee.textContent = ticket.assignee;
      const stateTag = document.createElement('span');
      stateTag.className = 'tag';
      stateTag.textContent = stateLabel(ticket.state);
      tags.append(assignee, stateTag);

      const badges = document.createElement('div');
      badges.className = 'badge-row';
      badges.appendChild(badge(`audit ${ticket.audit_signoff ? '✓' : '✗'}`, ticket.audit_signoff));
      if (ticket.needs_eric_signoff) {
        badges.appendChild(badge(`eric ${ticket.eric_signoff ? '✓' : '✗'}`, ticket.eric_signoff));
      }

      const controls = document.createElement('div');
      controls.className = 'control-row';
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
        await updateTicket(ticket.id, { state: nextState });
      });
      controls.appendChild(stateSelect);

      card.append(top, tags, badges);
      if (ticket.screenshot && !ticket.screenshot_available) {
        const missing = document.createElement('div');
        missing.className = 'soft-note';
        missing.textContent = 'image unavailable';
        card.appendChild(missing);
      }
      card.appendChild(controls);
      return card;
    }

    function selectedTicket() {
      return state.tickets.find((ticket) => ticket.id === state.selectedId) || null;
    }

    function renderDetail() {
      const ticket = selectedTicket();
      if (!ticket) {
        detailContentEl.innerHTML = '<div class="meta">No ticket selected.</div>';
        return;
      }

      detailContentEl.innerHTML = '';

      const box = document.createElement('div');
      box.className = 'detail-box';
      const controls = document.createElement('div');
      controls.className = 'detail-grid';

      const assigneeLabel = document.createElement('label');
      assigneeLabel.innerHTML = '<span class="field-label">Assignee</span>';
      const assigneeSelect = document.createElement('select');
      state.assignees.forEach((assignee) => buildOption(assigneeSelect, assignee, assignee));
      assigneeSelect.value = ticket.assignee;
      assigneeSelect.addEventListener('change', async () => {
        await updateTicket(ticket.id, { assignee: assigneeSelect.value });
      });
      assigneeLabel.appendChild(assigneeSelect);

      const screenshotLabel = document.createElement('label');
      screenshotLabel.innerHTML = '<span class="field-label">Attached Frame</span>';
      const screenshotSelect = document.createElement('select');
      buildOption(screenshotSelect, '', '(none)');
      state.screenshots.forEach((shot) => buildOption(screenshotSelect, shot.path, `${shot.name} - ${shot.modified}`));
      if (ticket.screenshot && !Array.from(screenshotSelect.options).some((option) => option.value === ticket.screenshot)) {
        buildOption(
          screenshotSelect,
          ticket.screenshot,
          ticket.screenshot_available ? ticket.screenshot.split('/').pop() : `${ticket.screenshot.split('/').pop()} - unavailable`,
        );
      }
      screenshotSelect.value = ticket.screenshot || '';
      screenshotSelect.addEventListener('change', async () => {
        await updateTicket(ticket.id, { screenshot: screenshotSelect.value || null });
      });
      screenshotLabel.appendChild(screenshotSelect);

      controls.append(assigneeLabel, screenshotLabel);

      const toggles = document.createElement('div');
      toggles.className = 'tag-row';
      toggles.appendChild(toggleControl('Needs Eric signoff', ticket.needs_eric_signoff, async (checked) => {
        await updateTicket(ticket.id, { needs_eric_signoff: checked });
      }));
      toggles.appendChild(toggleControl('Audit signoff', ticket.audit_signoff, async (checked) => {
        await updateTicket(ticket.id, { audit_signoff: checked });
      }));
      if (ticket.needs_eric_signoff) {
        toggles.appendChild(toggleControl('Eric signoff', ticket.eric_signoff, async (checked) => {
          await updateTicket(ticket.id, { eric_signoff: checked });
        }));
      }

      const meta = document.createElement('div');
      meta.className = 'meta';
      const metaLine1 = document.createElement('div');
      const strong = document.createElement('strong');
      strong.textContent = ticket.id;
      metaLine1.append(strong, document.createTextNode(` - ${ticket.title}`));
      const metaLine2 = document.createElement('div');
      metaLine2.textContent = `State: ${stateLabel(ticket.state)} | Created: ${formatWhen(ticket.created)} | Updated: ${formatWhen(ticket.updated)}`;
      meta.append(metaLine1, metaLine2);

      const body = document.createElement('div');
      body.innerHTML = '<div class="field-label">Body</div>';
      const bodyText = document.createElement('div');
      bodyText.className = 'body-text';
      bodyText.textContent = ticket.body || '(no body)';
      body.appendChild(bodyText);

      const implementation = document.createElement('div');
      implementation.innerHTML = '<div class="field-label">Implementation</div>';
      const implementationInput = document.createElement('textarea');
      implementationInput.value = ticket.implementation || '';
      implementationInput.placeholder = 'Director-authored implementation package/spec for the implementer at in_progress.';
      const implementationActions = document.createElement('div');
      implementationActions.className = 'inline-actions';
      const saveImplementationButton = document.createElement('button');
      saveImplementationButton.textContent = 'Save Implementation';
      saveImplementationButton.addEventListener('click', async () => {
        await updateTicket(ticket.id, { implementation: implementationInput.value });
      });
      implementationActions.appendChild(saveImplementationButton);
      implementation.append(implementationInput, implementationActions);

      const auditPrompt = document.createElement('div');
      auditPrompt.innerHTML = '<div class="field-label">Audit Prompt</div>';
      const auditPromptInput = document.createElement('textarea');
      auditPromptInput.value = ticket.audit_prompt || '';
      auditPromptInput.placeholder = 'Director-authored prompt for audit when the ticket enters audit.';
      const auditPromptActions = document.createElement('div');
      auditPromptActions.className = 'inline-actions';
      const saveAuditPromptButton = document.createElement('button');
      saveAuditPromptButton.textContent = 'Save Audit Prompt';
      saveAuditPromptButton.addEventListener('click', async () => {
        await updateTicket(ticket.id, { audit_prompt: auditPromptInput.value });
      });
      auditPromptActions.appendChild(saveAuditPromptButton);
      auditPrompt.append(auditPromptInput, auditPromptActions);

      box.append(meta, controls, toggles, body, implementation, auditPrompt);

      if (ticket.screenshot) {
        const imageWrap = document.createElement('div');
        imageWrap.innerHTML = '<div class="field-label">Attached Frame</div>';
        if (ticket.screenshot_available) {
          const image = document.createElement('img');
          image.className = 'detail-image';
          image.src = `/api/image/${encodeURIComponent(ticket.screenshot)}`;
          image.alt = ticket.screenshot;
          imageWrap.appendChild(image);
        } else {
          const missing = document.createElement('div');
          missing.className = 'missing-image-box';
          missing.textContent = 'image unavailable';
          imageWrap.appendChild(missing);
        }
        box.appendChild(imageWrap);
      }

      const comments = document.createElement('div');
      comments.innerHTML = '<div class="field-label">Comments</div>';
      const commentComposer = document.createElement('div');
      commentComposer.className = 'comment-composer';
      const commentWho = document.createElement('input');
      commentWho.type = 'text';
      commentWho.placeholder = 'who';
      commentWho.value = ticketIsEricReview(ticket) ? 'eric' : 'director';
      const commentText = document.createElement('textarea');
      commentText.placeholder = 'Add a comment or bounce-back note';
      const commentActions = document.createElement('div');
      commentActions.className = 'inline-actions';
      const addCommentButton = document.createElement('button');
      addCommentButton.textContent = 'Add Comment';
      addCommentButton.addEventListener('click', async () => {
        await submitComment(ticket.id, commentWho.value, commentText.value);
      });
      commentActions.appendChild(addCommentButton);
      if (ticketIsEricReview(ticket)) {
        const signoffButton = document.createElement('button');
        signoffButton.textContent = 'Sign Off -> Done';
        signoffButton.addEventListener('click', async () => {
          const patch = { eric_signoff: true, state: 'done' };
          const trimmedWho = commentWho.value.trim();
          const trimmedText = commentText.value.trim();
          if (trimmedWho && trimmedText) {
            patch.comment = { who: trimmedWho, text: trimmedText };
          }
          await updateTicket(ticket.id, patch);
        });
        commentActions.appendChild(signoffButton);

        const kickbackButton = document.createElement('button');
        kickbackButton.textContent = 'Kick Back -> In Progress';
        kickbackButton.addEventListener('click', async () => {
          await submitComment(ticket.id, commentWho.value, commentText.value, 'in_progress');
        });
        commentActions.appendChild(kickbackButton);
      } else if (ticketAllowsKickback(ticket)) {
        const kickbackButton = document.createElement('button');
        kickbackButton.textContent = 'Comment + Move to In Progress';
        kickbackButton.addEventListener('click', async () => {
          await submitComment(ticket.id, commentWho.value, commentText.value, 'in_progress');
        });
        commentActions.appendChild(kickbackButton);
      }
      commentComposer.append(commentWho, commentText, commentActions);
      comments.appendChild(commentComposer);
      if (ticket.comments.length) {
        const list = document.createElement('div');
        list.className = 'comment-list';
        ticket.comments.forEach((comment) => {
          const item = document.createElement('div');
          item.className = 'comment';
          const header = document.createElement('div');
          const who = document.createElement('strong');
          who.textContent = comment.who;
          const ts = document.createElement('span');
          ts.className = 'meta';
          ts.textContent = ` ${formatWhen(comment.ts)}`;
          header.append(who, ts);
          const text = document.createElement('div');
          text.textContent = comment.text;
          item.append(header, text);
          list.appendChild(item);
        });
        comments.appendChild(list);
      } else {
        const empty = document.createElement('div');
        empty.className = 'meta';
        empty.textContent = 'No comments.';
        comments.appendChild(empty);
      }
      box.appendChild(comments);

      detailContentEl.appendChild(box);
    }

    async function uploadImageBlob(blob) {
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
      const label = context === 'detail' ? 'Replacing ticket screenshot…' : 'Uploading pasted screenshot…';
      setCreateStatus(label);
      const uploaded = await uploadImageBlob(image);
      await requestBoardReload();
      if (context === 'detail') {
        const ticket = selectedTicket();
        if (!ticket) {
          throw new Error('no ticket selected for screenshot replace');
        }
        await updateTicket(ticket.id, { screenshot: uploaded.path });
        setCreateStatus(`Attached pasted screenshot to ${ticket.id}.`);
        return;
      }
      state.pendingCreateScreenshot = uploaded.path;
      ensureScreenshotOption(uploaded.path, `${uploaded.name} - ${uploaded.modified}`);
      screenshotInput.value = uploaded.path;
      renderCreatePreview();
      setCreateStatus('Pasted screenshot attached to the new ticket.');
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
      const response = await fetch('/api/board', { cache: 'no-store' });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const payload = await response.json();
      state.tickets = payload.tickets;
      state.screenshots = payload.screenshots;
      state.errors = payload.errors;
      state.assignees = payload.assignees;
      if (!state.selectedId && state.tickets.length) {
        state.selectedId = state.tickets[0].id;
      }
      if (state.selectedId && !state.tickets.some((ticket) => ticket.id === state.selectedId)) {
        state.selectedId = state.tickets[0]?.id || null;
      }
      storePathEl.textContent = payload.store_path;
      framePathEl.textContent = payload.frame_dir;
      refreshLineEl.textContent = formatWhen(payload.refreshed_at);
      populateCreateForm();
      renderErrors();
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
        screenshot: screenshotInput.value || null,
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
      state.pendingCreateScreenshot = null;
      screenshotInput.value = '';
      renderCreatePreview();
      state.selectedId = result.ticket.id;
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
      await updateTicket(ticketId, patch);
    }

    createBtn.addEventListener('click', async () => {
      try {
        await createTicket();
      } catch (error) {
        setCreateStatus(error.message, true);
      }
    });

    screenshotInput.addEventListener('change', () => {
      state.pendingCreateScreenshot = screenshotInput.value || null;
      renderCreatePreview();
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
  </script>
</body>
</html>
"""
