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
    aside {
      background: var(--panel);
      min-height: 100vh;
      overflow: auto;
      border-right: 1px solid var(--border);
    }
    body.detail-open {
      overflow: hidden;
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
    .topbar h1, .panel-head h2, .detail-modal-head h2 { margin: 0; font-size: 18px; }
    .subtle, .meta, .hint { color: var(--muted); font-size: 13px; line-height: 1.45; }
    .paths {
      text-align: right;
      max-width: min(100%, 34rem);
      margin-left: auto;
    }
    .panel-head {
      padding: 16px 18px;
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      background: rgba(23, 26, 32, 0.96);
      z-index: 2;
    }
    .panel-body {
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
    .attachment-gallery {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
      gap: 10px;
    }
    .attachment-card {
      position: relative;
      display: grid;
      gap: 8px;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px;
      background: rgba(255,255,255,0.03);
      overflow: hidden;
    }
    .attachment-thumb,
    .attachment-missing {
      width: 100%;
      aspect-ratio: 4 / 3;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: #0b0d11;
    }
    .attachment-thumb {
      object-fit: contain;
    }
    .attachment-missing {
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 10px;
      color: var(--muted);
      font-size: 12px;
      text-align: center;
      line-height: 1.4;
    }
    .attachment-meta {
      font-size: 12px;
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .attachment-remove {
      position: absolute;
      top: 10px;
      right: 10px;
      width: 28px;
      height: 28px;
      padding: 0;
      border-radius: 999px;
      background: rgba(15, 17, 21, 0.9);
      border-color: rgba(255,255,255,0.2);
      display: grid;
      place-items: center;
      opacity: 0;
      transition: opacity 120ms ease;
    }
    .attachment-card:hover .attachment-remove,
    .attachment-card:focus-within .attachment-remove {
      opacity: 1;
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
    .card-blocked {
      border-color: rgba(253, 164, 175, 0.32);
      box-shadow: 0 0 0 1px rgba(253, 164, 175, 0.12) inset;
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
    .card-assignee {
      display: flex;
      gap: 6px;
      align-items: baseline;
      font-size: 12px;
      line-height: 1.3;
      color: var(--muted);
    }
    .card-assignee-label {
      text-transform: uppercase;
      color: var(--muted);
    }
    .card-assignee-value {
      color: var(--text);
      font-weight: 600;
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
      gap: 14px;
      width: 100%;
    }
    .detail-overlay[hidden] { display: none; }
    .detail-overlay {
      position: fixed;
      inset: 0;
      z-index: 20;
      display: grid;
      place-items: stretch;
      background: rgba(4, 6, 10, 0.72);
      padding: 18px;
    }
    .detail-modal {
      width: min(100%, 1280px);
      height: min(100vh - 36px, 100%);
      margin: 0 auto;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      border: 1px solid var(--border);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(23, 26, 32, 0.98), rgba(15, 17, 21, 0.98));
      box-shadow: 0 30px 90px rgba(0, 0, 0, 0.42);
      overflow: hidden;
    }
    .detail-modal-head {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: start;
      padding: 18px 22px;
      border-bottom: 1px solid var(--border);
      background: rgba(15, 17, 21, 0.96);
    }
    .detail-modal-body {
      overflow: auto;
      padding: 22px;
    }
    .detail-content {
      width: min(100%, 1080px);
      margin: 0 auto;
      display: grid;
      gap: 14px;
    }
    .detail-close {
      width: 40px;
      height: 40px;
      min-width: 40px;
      padding: 0;
      border-radius: 999px;
      font-size: 24px;
      line-height: 1;
      display: grid;
      place-items: center;
    }
    .eric-banner {
      border: 1px solid rgba(125, 211, 252, 0.5);
      border-radius: 8px;
      padding: 14px;
      background: linear-gradient(180deg, rgba(125, 211, 252, 0.18), rgba(125, 211, 252, 0.08));
      display: grid;
      gap: 10px;
    }
    .eric-banner-subtitle {
      font-size: 13px;
      font-weight: 700;
      color: var(--accent);
      text-transform: uppercase;
    }
    .eric-banner-title {
      font-size: 18px;
      font-weight: 700;
      color: #d8f3ff;
      line-height: 1.3;
    }
    .eric-banner-note {
      font-size: 14px;
      line-height: 1.45;
      color: var(--text);
    }
    .eric-summary {
      display: grid;
      gap: 10px;
      padding: 12px;
      border-radius: 8px;
      border: 1px solid rgba(255,255,255,0.12);
      background: rgba(15, 17, 21, 0.26);
    }
    .eric-summary-head {
      font-size: 12px;
      font-weight: 700;
      color: var(--accent);
      text-transform: uppercase;
    }
    .eric-summary-statuses,
    .eric-summary-sections {
      display: grid;
      gap: 8px;
    }
    .eric-summary-status {
      display: flex;
      gap: 8px;
      align-items: baseline;
      font-size: 13px;
      line-height: 1.4;
    }
    .eric-summary-status strong {
      color: var(--text);
    }
    .eric-summary-status-ok { color: var(--ok); }
    .eric-summary-status-missing { color: var(--warn); }
    .eric-summary-section {
      display: grid;
      gap: 4px;
    }
    .eric-summary-label {
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
    }
    .eric-summary-list {
      margin: 0;
      padding-left: 18px;
      display: grid;
      gap: 4px;
      font-size: 13px;
      line-height: 1.45;
    }
    .eric-summary-text {
      font-size: 13px;
      line-height: 1.5;
      color: var(--text);
      white-space: pre-wrap;
    }
    .alert-stack {
      display: grid;
      gap: 10px;
    }
    .alert {
      display: grid;
      gap: 6px;
      padding: 12px 14px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.03);
    }
    .card-alert {
      padding: 10px 12px;
      font-size: 12px;
      line-height: 1.45;
    }
    .alert strong {
      font-size: 12px;
      text-transform: uppercase;
    }
    .alert-blocked,
    .card-alert-blocked {
      border-color: rgba(253, 164, 175, 0.42);
      background: rgba(127, 29, 29, 0.24);
    }
    .alert-blocked strong,
    .card-alert-blocked strong {
      color: #fecdd3;
    }
    .alert-guard,
    .card-alert-guard {
      border-color: rgba(252, 211, 77, 0.42);
      background: rgba(120, 53, 15, 0.2);
    }
    .alert-guard strong,
    .card-alert-guard strong {
      color: #fde68a;
    }
    .compact-textarea {
      min-height: 88px;
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
      line-height: 1.65;
      max-width: 84ch;
    }
    .comment-list {
      display: grid;
      gap: 8px;
      max-width: 90ch;
    }
    .comment {
      border-left: 2px solid var(--border);
      padding: 10px 0 10px 12px;
      background: rgba(255,255,255,0.02);
      border-radius: 0 8px 8px 0;
    }
    @media (max-width: 1200px) {
      .detail-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 980px) {
      .board { min-width: 980px; }
      .detail-overlay {
        padding: 0;
      }
      .detail-modal {
        width: 100%;
        height: 100vh;
        border-radius: 0;
        border-left: 0;
        border-right: 0;
      }
      .detail-modal-head,
      .detail-modal-body {
        padding-left: 16px;
        padding-right: 16px;
      }
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
      aside { min-height: 0; border-right: 0; border-left: 0; }
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
          Available Frame
          <select id="screenshotInput"></select>
        </label>
        <div class="inline-actions">
          <button id="addCreateAttachmentBtn" class="small" type="button">Add Attachment</button>
        </div>
        <div class="paste-hint">Paste an image from the clipboard here to upload and attach it.</div>
        <div id="createPreview" class="preview-card" hidden>
          <div id="createPreviewGallery" class="attachment-gallery"></div>
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

  </div>

  <div id="detailOverlay" class="detail-overlay" hidden>
    <section class="detail-modal" role="dialog" aria-modal="true" aria-labelledby="detailModalTitle">
      <div class="detail-modal-head">
        <div>
          <div class="subtle">Ticket Detail</div>
          <h2 id="detailModalTitle">Select a ticket</h2>
        </div>
        <button id="detailCloseBtn" class="detail-close" type="button" aria-label="Close ticket detail">×</button>
      </div>
      <div class="detail-modal-body">
        <div id="detailContent" class="detail-content">
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
      detailOpen: false,
      detailDraft: null,
      eventSource: null,
      eventReconnectTimer: null,
      loadInFlight: null,
      loadQueued: false,
      pendingCreateScreenshots: [],
    };

    const boardEl = document.getElementById('board');
    const assigneeInput = document.getElementById('assigneeInput');
    const screenshotInput = document.getElementById('screenshotInput');
    const addCreateAttachmentBtn = document.getElementById('addCreateAttachmentBtn');
    const createPreviewEl = document.getElementById('createPreview');
    const createPreviewGalleryEl = document.getElementById('createPreviewGallery');
    const titleInput = document.getElementById('titleInput');
    const bodyInput = document.getElementById('bodyInput');
    const needsEricInput = document.getElementById('needsEricInput');
    const createBtn = document.getElementById('createBtn');
    const createStatus = document.getElementById('createStatus');
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

    function defaultAdvanceState(ticket) {
      if (ticket.state === 'open') {
        return 'in_progress';
      }
      if (ticket.state === 'in_progress') {
        return 'director_review';
      }
      if (ticket.state === 'director_review') {
        return 'audit';
      }
      if (ticket.state === 'audit') {
        return ticket.needs_eric_signoff ? 'eric_review' : 'done';
      }
      return null;
    }

    function advanceBlockedReason(ticket) {
      const nextState = defaultAdvanceState(ticket);
      if (!nextState) {
        return 'No default advance from this state.';
      }
      if (ticket.state === 'open') {
        if (ticket.assignee === 'unassigned') {
          return 'Assign the ticket before advancing to in progress.';
        }
        if (!(ticket.implementation || '').trim()) {
          return 'Save implementation before advancing to in progress.';
        }
      }
      if (ticket.state === 'director_review' && !(ticket.audit_prompt || '').trim()) {
        return 'Save audit prompt before advancing to audit.';
      }
      if (ticket.state === 'audit' && !ticket.audit_signoff) {
        return ticket.needs_eric_signoff
          ? 'Set audit signoff before advancing to Eric review.'
          : 'Set audit signoff before advancing to done.';
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

    function guardBlockedSummary(ticket) {
      const reason = advanceBlockedReason(ticket);
      return defaultAdvanceState(ticket) ? reason : '';
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
      const blockedSummary = manualBlockedSummary(ticket);
      if (blockedSummary) {
        alerts.push({ kind: 'blocked', title: 'Blocked', text: blockedSummary });
      }
      const guardSummary = guardBlockedSummary(ticket);
      if (guardSummary) {
        alerts.push({ kind: 'guard', title: 'Advance Blocked', text: guardSummary });
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
      await updateTicket(ticketId, { state: nextState });
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

    function columnTickets(columnKey) {
      return state.tickets
        .filter((ticket) => ticket.state === columnKey && (columnKey !== 'eric_review' || ticket.needs_eric_signoff))
        .sort(compareTicketsOldestFirst);
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
      if (manualBlockedSummary(ticket)) {
        card.classList.add('card-blocked');
      }
      if (ticket.id === state.selectedId) {
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
          await updateTicket(ticket.id, { state: nextState });
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
      if (ticketScreenshotEntries(ticket).some((entry) => !entry.available)) {
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
      if (!state.detailOpen || !ticket) {
        detailContentEl.innerHTML = '<div class="meta">No ticket selected.</div>';
        syncDetailOverlay();
        return;
      }

      detailContentEl.innerHTML = '';

      const box = document.createElement('div');
      box.className = 'detail-box';
      const draftFields = new Map();
      const commentWho = document.createElement('input');
      commentWho.type = 'text';
      commentWho.placeholder = 'who';
      commentWho.value = ticketIsEricReview(ticket) ? 'eric' : 'director';
      bindDetailDraftField(draftFields, commentWho, 'commentWho', commentWho.value);
      const commentText = document.createElement('textarea');
      commentText.placeholder = 'Add a comment or bounce-back note';
      bindDetailDraftField(draftFields, commentText, 'commentText', '');

      if (ticketIsEricReview(ticket)) {
        const ericBanner = document.createElement('div');
        ericBanner.className = 'eric-banner';
        const ericBannerSubtitle = document.createElement('div');
        ericBannerSubtitle.className = 'eric-banner-subtitle';
        ericBannerSubtitle.textContent = 'Awaiting Eric sign-off';
        const ericBannerTitle = document.createElement('div');
        ericBannerTitle.className = 'eric-banner-title';
        ericBannerTitle.textContent = ticket.title;
        const ericBannerNote = document.createElement('div');
        ericBannerNote.className = 'eric-banner-note';
        ericBannerNote.textContent = ericReviewCheckText(ticket);
        const ericSummary = document.createElement('div');
        ericSummary.className = 'eric-summary';
        const ericSummaryHead = document.createElement('div');
        ericSummaryHead.className = 'eric-summary-head';
        ericSummaryHead.textContent = 'Check Before Sign-off';
        const ericSummaryStatuses = document.createElement('div');
        ericSummaryStatuses.className = 'eric-summary-statuses';
        ericReviewStatusItems(ticket).forEach((item) => {
          const row = document.createElement('div');
          row.className = 'eric-summary-status';
          const strong = document.createElement('strong');
          strong.textContent = item.label;
          const value = document.createElement('span');
          value.className = item.ok ? 'eric-summary-status-ok' : 'eric-summary-status-missing';
          value.textContent = item.ok ? (item.okText || 'ready') : (item.missingText || 'missing');
          row.append(strong, value);
          ericSummaryStatuses.appendChild(row);
        });
        const ericSummarySections = document.createElement('div');
        ericSummarySections.className = 'eric-summary-sections';
        ericReviewSummarySections(ticket).forEach((section) => {
          const sectionEl = document.createElement('div');
          sectionEl.className = 'eric-summary-section';
          const label = document.createElement('div');
          label.className = 'eric-summary-label';
          label.textContent = section.label;
          sectionEl.appendChild(label);
          if (section.checklist) {
            const list = document.createElement('ul');
            list.className = 'eric-summary-list';
            section.checklist.forEach((item) => {
              const entry = document.createElement('li');
              entry.textContent = item;
              list.appendChild(entry);
            });
            sectionEl.appendChild(list);
          } else if (section.summary) {
            const text = document.createElement('div');
            text.className = 'eric-summary-text';
            text.textContent = section.summary;
            sectionEl.appendChild(text);
          }
          ericSummarySections.appendChild(sectionEl);
        });
        ericSummary.append(ericSummaryHead, ericSummaryStatuses, ericSummarySections);
        const ericBannerActions = document.createElement('div');
        ericBannerActions.className = 'inline-actions';
        const ericBannerSignoffButton = document.createElement('button');
        ericBannerSignoffButton.className = 'primary';
        ericBannerSignoffButton.textContent = 'Sign Off -> Done';
        ericBannerSignoffButton.addEventListener('click', async () => {
          await submitEricSignoff(ticket.id, commentWho.value, commentText.value);
        });
        ericBannerActions.appendChild(ericBannerSignoffButton);
        ericBanner.append(ericBannerSubtitle, ericBannerTitle, ericBannerNote, ericSummary, ericBannerActions);
        box.appendChild(ericBanner);
      }

      const detailAlerts = renderAlertStack(ticket, { detail: true });
      if (detailAlerts) {
        box.appendChild(detailAlerts);
      }

      const controls = document.createElement('div');
      controls.className = 'detail-grid';

      const workflowActions = document.createElement('div');
      workflowActions.className = 'inline-actions';
      const detailAdvanceState = defaultAdvanceState(ticket);
      if (detailAdvanceState) {
        const advanceButton = document.createElement('button');
        advanceButton.className = 'primary';
        advanceButton.type = 'button';
        advanceButton.textContent = `Advance -> ${stateLabel(detailAdvanceState)}`;
        const blockedReason = advanceBlockedReason(ticket);
        if (blockedReason) {
          advanceButton.disabled = true;
          advanceButton.title = blockedReason;
        }
        advanceButton.addEventListener('click', async () => {
          try {
            await advanceTicket(ticket.id);
          } catch (error) {
            setCreateStatus(error.message, true);
            await requestBoardReload();
          }
        });
        workflowActions.appendChild(advanceButton);
        if (blockedReason) {
          const advanceNote = document.createElement('div');
          advanceNote.className = 'soft-note';
          advanceNote.textContent = blockedReason;
          workflowActions.appendChild(advanceNote);
        }
      }

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
      screenshotLabel.innerHTML = '<span class="field-label">Available Frame</span>';
      const screenshotSelect = document.createElement('select');
      buildOption(screenshotSelect, '', '(none)');
      state.screenshots.forEach((shot) => buildOption(screenshotSelect, shot.path, `${shot.name} - ${shot.modified}`));
      ticketScreenshotEntries(ticket).forEach((entry) => {
        if (!Array.from(screenshotSelect.options).some((option) => option.value === entry.path)) {
          buildOption(
            screenshotSelect,
            entry.path,
            entry.available ? entry.path.split('/').pop() : `${entry.path.split('/').pop()} - unavailable`,
          );
        }
      });
      const screenshotActions = document.createElement('div');
      screenshotActions.className = 'inline-actions';
      const addAttachmentButton = document.createElement('button');
      addAttachmentButton.type = 'button';
      addAttachmentButton.textContent = 'Add Attachment';
      addAttachmentButton.addEventListener('click', async () => {
        if (!screenshotSelect.value) {
          return;
        }
        await updateTicket(ticket.id, {
          screenshots: uniquePaths([...ticketScreenshotPaths(ticket), screenshotSelect.value]),
        });
      });
      screenshotActions.appendChild(addAttachmentButton);
      screenshotLabel.append(screenshotSelect, screenshotActions);

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
      const titleField = document.createElement('div');
      titleField.innerHTML = '<div class="field-label">Title</div>';
      const titleEditInput = document.createElement('input');
      titleEditInput.type = 'text';
      titleEditInput.value = ticket.title;
      titleEditInput.placeholder = 'Short issue title';
      bindDetailDraftField(draftFields, titleEditInput, 'title', titleEditInput.value);
      const titleActions = document.createElement('div');
      titleActions.className = 'inline-actions';
      const saveTitleButton = document.createElement('button');
      saveTitleButton.textContent = 'Save Title';
      saveTitleButton.addEventListener('click', async () => {
        await updateTicket(ticket.id, { title: titleEditInput.value });
      });
      titleActions.appendChild(saveTitleButton);
      titleField.append(titleEditInput, titleActions);
      const metaLine1 = document.createElement('div');
      const strong = document.createElement('strong');
      strong.textContent = ticket.id;
      metaLine1.append(strong, document.createTextNode(` - ${ticket.title}`));
      const metaLine2 = document.createElement('div');
      metaLine2.textContent = `State: ${stateLabel(ticket.state)} | Created: ${formatWhen(ticket.created)} | Updated: ${formatWhen(ticket.updated)}`;
      meta.append(metaLine1, metaLine2);
      if ((ticket.blocked_by || []).length) {
        const metaLine3 = document.createElement('div');
        metaLine3.textContent = `Blocked By: ${formatBlockedByList(ticket.blocked_by)}`;
        meta.appendChild(metaLine3);
      }

      const body = document.createElement('div');
      body.innerHTML = '<div class="field-label">Body</div>';
      const bodyText = document.createElement('div');
      bodyText.className = 'body-text';
      bodyText.textContent = ticket.body || '(no body)';
      body.appendChild(bodyText);

      const blockedBy = document.createElement('div');
      blockedBy.innerHTML = '<div class="field-label">Blocked By</div>';
      const blockedByInput = document.createElement('input');
      blockedByInput.type = 'text';
      blockedByInput.value = formatBlockedByList(ticket.blocked_by);
      blockedByInput.placeholder = 'PGU-23, PGU-25';
      bindDetailDraftField(draftFields, blockedByInput, 'blockedBy', blockedByInput.value);
      const blockedByActions = document.createElement('div');
      blockedByActions.className = 'inline-actions';
      const saveBlockedByButton = document.createElement('button');
      saveBlockedByButton.textContent = 'Save Blockers';
      saveBlockedByButton.addEventListener('click', async () => {
        await updateTicket(ticket.id, {
          blocked_by: parseBlockedByInput(blockedByInput.value),
          blocked_reason: blockedReasonInput.value,
        });
      });
      blockedByActions.appendChild(saveBlockedByButton);
      const blockedByNote = document.createElement('div');
      blockedByNote.className = 'soft-note';
      blockedByNote.textContent = blockedBySummary(ticket);
      blockedBy.append(blockedByInput, blockedByActions, blockedByNote);

      const blockedReason = document.createElement('div');
      blockedReason.innerHTML = '<div class="field-label">Blocked Reason</div>';
      const blockedReasonInput = document.createElement('textarea');
      blockedReasonInput.className = 'compact-textarea';
      blockedReasonInput.value = ticket.blocked_reason || '';
      blockedReasonInput.placeholder = 'Why this ticket is blocked right now.';
      bindDetailDraftField(draftFields, blockedReasonInput, 'blockedReason', blockedReasonInput.value);
      const blockedReasonActions = document.createElement('div');
      blockedReasonActions.className = 'inline-actions';
      const saveBlockedReasonButton = document.createElement('button');
      saveBlockedReasonButton.textContent = 'Save Blocked Reason';
      saveBlockedReasonButton.addEventListener('click', async () => {
        await updateTicket(ticket.id, { blocked_reason: blockedReasonInput.value });
      });
      blockedReasonActions.appendChild(saveBlockedReasonButton);
      const blockedReasonNote = document.createElement('div');
      blockedReasonNote.className = 'soft-note';
      blockedReasonNote.textContent = 'Required when blocked-by dependencies are set. Also use this for non-dependency stalls.';
      blockedReason.append(blockedReasonInput, blockedReasonActions, blockedReasonNote);

      const implementation = document.createElement('div');
      implementation.innerHTML = '<div class="field-label">Implementation</div>';
      const implementationInput = document.createElement('textarea');
      implementationInput.value = ticket.implementation || '';
      implementationInput.placeholder = 'Director-authored implementation package/spec for the implementer at in_progress.';
      bindDetailDraftField(draftFields, implementationInput, 'implementation', implementationInput.value);
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
      bindDetailDraftField(draftFields, auditPromptInput, 'auditPrompt', auditPromptInput.value);
      const auditPromptActions = document.createElement('div');
      auditPromptActions.className = 'inline-actions';
      const saveAuditPromptButton = document.createElement('button');
      saveAuditPromptButton.textContent = 'Save Audit Prompt';
      saveAuditPromptButton.addEventListener('click', async () => {
        await updateTicket(ticket.id, { audit_prompt: auditPromptInput.value });
      });
      auditPromptActions.appendChild(saveAuditPromptButton);
      auditPrompt.append(auditPromptInput, auditPromptActions);

      const commitInfo = document.createElement('div');
      commitInfo.innerHTML = '<div class="field-label">Commit Hash</div>';
      const commitHashInput = document.createElement('input');
      commitHashInput.type = 'text';
      commitHashInput.value = ticket.commit_hash || '';
      commitHashInput.placeholder = 'Required before done unless exempt';
      bindDetailDraftField(draftFields, commitHashInput, 'commitHash', commitHashInput.value);
      const commitActions = document.createElement('div');
      commitActions.className = 'inline-actions';
      const saveCommitButton = document.createElement('button');
      saveCommitButton.textContent = 'Save Commit';
      saveCommitButton.addEventListener('click', async () => {
        await updateTicket(ticket.id, { commit_hash: commitHashInput.value });
      });
      commitActions.appendChild(saveCommitButton);
      commitInfo.append(commitHashInput, commitActions);
      const commitOverride = toggleControl('No commit required', ticket.commit_exempt, async (checked) => {
        await updateTicket(ticket.id, { commit_exempt: checked });
      });
      const commitNote = document.createElement('div');
      commitNote.className = 'soft-note';
      commitNote.textContent = ticket.commit_exempt
        ? 'Done-state commit check is bypassed for this ticket.'
        : 'A verified git commit is required before moving this ticket to done.';
      commitInfo.append(commitOverride, commitNote);

      box.append(
        titleField,
        meta,
        workflowActions,
        controls,
        toggles,
        blockedReason,
        body,
        blockedBy,
        implementation,
        auditPrompt,
        commitInfo,
      );

      if (ticketScreenshotEntries(ticket).length) {
        const imageWrap = document.createElement('div');
        imageWrap.innerHTML = '<div class="field-label">Attachments</div>';
        const entries = ticketScreenshotEntries(ticket).map((entry) => ({
          ...entry,
          label: screenshotLabelFor(entry.path),
        }));
        const gallery = document.createElement('div');
        gallery.className = 'attachment-gallery';
        renderAttachmentGallery(gallery, entries, 'Remove attachment', async (path) => {
          await updateTicket(ticket.id, {
            screenshots: ticketScreenshotPaths(ticket).filter((item) => item !== path),
          });
        });
        imageWrap.appendChild(gallery);
        box.appendChild(imageWrap);
      }

      const comments = document.createElement('div');
      comments.innerHTML = '<div class="field-label">Comments</div>';
      const commentComposer = document.createElement('div');
      commentComposer.className = 'comment-composer';
      const commentActions = document.createElement('div');
      commentActions.className = 'inline-actions';
      const addCommentButton = document.createElement('button');
      addCommentButton.textContent = 'Add Comment';
      addCommentButton.addEventListener('click', async () => {
        await submitComment(ticket.id, commentWho.value, commentText.value);
      });
      commentActions.appendChild(addCommentButton);
      if (ticketIsEricReview(ticket)) {
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
      restoreDetailDraft(ticket.id, draftFields);
      syncDetailOverlay();
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
      state.selectedId = result.ticket.id;
      state.detailOpen = true;
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
      clearDetailDraft(ticketId);
      await updateTicket(ticketId, patch);
    }

    async function submitEricSignoff(ticketId, who, text) {
      const patch = { eric_signoff: true, state: 'done' };
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
  </script>
</body>
</html>
"""
