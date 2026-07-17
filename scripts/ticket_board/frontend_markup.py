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
          <label class="check">
            <input id="createDraftInput" type="checkbox">
            Create as draft
          </label>
          <label class="check">
            <input id="createBacklogInput" type="checkbox">
            Start in backlog
          </label>
          <div class="attach-panel" id="createAttachDropZone">
            <input id="createImageInput" class="visually-hidden" type="file" accept="image/*" multiple>
            <div class="attach-set-grid">
              <label>
                Set
                <select data-attach-set>
                  <option value="">Ungrouped</option>
                  <option value="target">Target</option>
                  <option value="attempt">Attempt</option>
                  <option value="feedback">Feedback</option>
                </select>
              </label>
              <label>
                Set #
                <input data-attach-attempt type="number" min="1" max="999" placeholder="3">
              </label>
              <label>
                Label
                <input data-attach-label type="text" placeholder="uat rework, zoom-in, feedback">
              </label>
            </div>
            <button id="createAttachImageBtn" type="button">Attach image</button>
            <div class="attach-help">Choose image files, drag them here, or paste from the clipboard to attach them.</div>
          </div>
          <div id="createPreview" class="preview-card" hidden>
            <div id="createPreviewGallery" class="attachment-gallery"></div>
          </div>
          <label class="check">
            <input id="needsEricInput" type="checkbox">
            Needs UAT
          </label>
          <label class="check">
            <input id="needsInspectionInput" type="checkbox">
            Needs inspection
          </label>
          <label class="check">
            <input id="createRegressionInput" type="checkbox">
            Regression
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

  <div id="imageLightboxOverlay" class="image-lightbox-overlay" hidden>
    <section class="image-lightbox" role="dialog" aria-modal="true" aria-labelledby="imageLightboxCaption">
      <button
        id="imageLightboxCloseBtn"
        class="image-lightbox-close"
        type="button"
        aria-label="Close attachment preview"
      >
        ×
      </button>
      <div class="image-lightbox-stage">
        <img id="imageLightboxImage" class="image-lightbox-image" alt="">
      </div>
      <div id="imageLightboxCaption" class="image-lightbox-caption"></div>
    </section>
  </div>

  <div id="refreshRequiredOverlay" class="refresh-required-overlay" hidden>
    <section class="refresh-required-modal" role="alertdialog" aria-modal="true" aria-labelledby="refreshRequiredTitle" aria-describedby="refreshRequiredBody">
      <div class="subtle">Board update available</div>
      <h2 id="refreshRequiredTitle">Refresh the board</h2>
      <p id="refreshRequiredBody">A newer board version is deployed. Refresh to update before continuing.</p>
      <button id="refreshRequiredButton" class="primary" type="button">Refresh</button>
    </section>
  </div>
"""
