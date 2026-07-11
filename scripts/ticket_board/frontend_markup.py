"""Static markup shell for the ticket-board browser UI."""

MARKUP = """  <div class="layout">
    <aside>
      <button
        id="createSectionToggle"
        class="mobile-section-toggle-panel"
        type="button"
        aria-expanded="false"
        aria-controls="createSectionContent"
      >
        <span class="mobile-section-title-wrap">
          <span class="mobile-section-title">New Ticket</span>
          <span id="createSectionCount" class="mobile-section-count" hidden></span>
        </span>
        <span class="mobile-section-chevron" aria-hidden="true">▾</span>
      </button>
      <div id="createSectionContent" class="mobile-section-content">
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
      </div>
    </aside>

    <section class="shell">
      <div class="topbar">
        <div class="topbar-main">
          <h1>PGU Ticket Board</h1>
          <div class="topbar-controls">
            <label class="check compact">
              <input id="showDeferredInput" type="checkbox">
              <span>Show Deferred <span id="showDeferredCount">(0)</span></span>
            </label>
            <label class="check compact">
              <input id="showDoneInput" type="checkbox">
              <span>Show Done <span id="showDoneCount">(0)</span></span>
            </label>
            <label class="check compact">
              <input id="showCancelledInput" type="checkbox">
              <span>Show Cancelled <span id="showCancelledCount">(0)</span></span>
            </label>
          </div>
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
"""
