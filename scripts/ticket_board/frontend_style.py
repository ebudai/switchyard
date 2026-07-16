"""CSS for the ticket-board browser UI."""

STYLE = """    :root {
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
    .topbar-main {
      display: grid;
      gap: 10px;
      min-width: 0;
    }
    .topbar-controls {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
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
    .check.compact {
      font-size: 12px;
      color: var(--muted);
      padding: 6px 10px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: rgba(255,255,255,0.03);
    }
    .visually-hidden {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }
    .attach-panel {
      border: 1px dashed var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      background: rgba(255,255,255,0.02);
      display: grid;
      gap: 8px;
      transition: border-color 120ms ease, background 120ms ease;
    }
    .attach-panel.drag-active {
      border-color: rgba(125, 211, 252, 0.75);
      background: rgba(125, 211, 252, 0.1);
    }
    .attach-panel button {
      width: fit-content;
      max-width: 100%;
      background: var(--accent-soft);
      border-color: rgba(125, 211, 252, 0.45);
    }
    .attach-help {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
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
    .attachment-set-list {
      display: grid;
      gap: 12px;
    }
    .attachment-set {
      border: 1px solid var(--border);
      border-radius: 10px;
      background: rgba(255,255,255,0.025);
      overflow: hidden;
    }
    .attachment-set-summary {
      cursor: pointer;
      padding: 10px 12px;
      color: var(--text);
      font-weight: 700;
      background: rgba(125, 211, 252, 0.055);
      border-bottom: 1px solid transparent;
    }
    .attachment-set[open] .attachment-set-summary {
      border-bottom-color: var(--border);
    }
    .attachment-set .attachment-gallery {
      padding: 12px;
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
    .attachment-card-clickable {
      cursor: zoom-in;
      transition: border-color 120ms ease, transform 120ms ease, background 120ms ease;
    }
    .attachment-card-clickable:hover,
    .attachment-card-clickable:focus-visible {
      border-color: rgba(125, 211, 252, 0.45);
      background: rgba(125, 211, 252, 0.08);
      transform: translateY(-1px);
    }
    .attachment-card-clickable:focus-visible {
      outline: 2px solid rgba(125, 211, 252, 0.5);
      outline-offset: 2px;
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
    .mobile-section-toggle-panel {
      display: none;
    }
    .mobile-section-content[hidden],
    .column-body[hidden] {
      display: none;
    }
    .comment-composer {
      display: grid;
      gap: 10px;
      margin-bottom: 12px;
    }
    .urgent-comment-toggle {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      width: fit-content;
      color: var(--muted);
      font-size: 0.9rem;
      font-weight: 700;
    }
    .board-scroll {
      overflow-x: auto;
      overflow-y: visible;
      padding: 18px;
    }
    .board {
      min-width: 100%;
      display: grid;
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
      width: 100%;
      text-align: left;
      appearance: none;
    }
    .mobile-section-title-wrap {
      min-width: 0;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: baseline;
    }
    .mobile-section-title {
      min-width: 0;
    }
    .mobile-section-count {
      display: none;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.3;
    }
    .mobile-section-chevron {
      display: none;
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
    .card-signed-off {
      border-color: rgba(134, 239, 172, 0.4);
      box-shadow: 0 0 0 1px rgba(134, 239, 172, 0.15) inset;
    }
    .card-active-work {
      border-color: rgba(252, 211, 77, 0.58);
      background: linear-gradient(90deg, rgba(252, 211, 77, 0.12), var(--panel-2) 30%);
      box-shadow: 0 0 0 1px rgba(252, 211, 77, 0.22) inset;
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
    .card-assignee-value {
      color: var(--text);
      font-weight: 600;
      overflow-wrap: anywhere;
    }
    .card-signoff-state {
      display: inline-flex;
      width: fit-content;
      max-width: 100%;
      margin-top: 6px;
      padding: 5px 10px;
      border-radius: 999px;
      border: 1px solid var(--border);
      font-size: 12px;
      line-height: 1.2;
      font-weight: 700;
    }
    .card-signoff-state.pending {
      color: var(--warn);
      border-color: rgba(252, 211, 77, 0.35);
      background: rgba(252, 211, 77, 0.08);
    }
    .card-signoff-state.signed {
      color: var(--ok);
      border-color: rgba(134, 239, 172, 0.4);
      background: rgba(134, 239, 172, 0.1);
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
    .child-ticket-list {
      display: grid;
      gap: 8px;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: rgba(255,255,255,0.03);
    }
    .child-ticket-list-compact {
      padding: 8px 10px;
    }
    .child-ticket-head {
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
    }
    .child-ticket-items {
      display: grid;
      gap: 8px;
    }
    .child-ticket-item {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      text-align: left;
      padding: 10px 12px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.03);
    }
    .child-ticket-item.selected {
      border-color: rgba(125, 211, 252, 0.55);
      box-shadow: 0 0 0 1px rgba(125, 211, 252, 0.3) inset;
    }
    .child-ticket-text {
      min-width: 0;
      display: grid;
      gap: 2px;
    }
    .child-ticket-id {
      color: var(--accent);
      font-size: 12px;
      font-weight: 600;
    }
    .child-ticket-title {
      font-size: 13px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .child-ticket-state {
      white-space: nowrap;
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
      overflow: auto;
    }
    .detail-modal {
      width: min(100%, 1280px);
      height: min(100vh - 36px, 100%);
      max-height: min(100vh - 36px, 100%);
      margin: 0 auto;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      border: 1px solid var(--border);
      border-radius: 8px;
      background: linear-gradient(180deg, rgba(23, 26, 32, 0.98), rgba(15, 17, 21, 0.98));
      box-shadow: 0 30px 90px rgba(0, 0, 0, 0.42);
      overflow: hidden;
      min-height: 0;
    }
    .image-lightbox-overlay[hidden] { display: none; }
    .image-lightbox-overlay {
      position: fixed;
      inset: 0;
      z-index: 30;
      display: grid;
      place-items: center;
      background: rgba(4, 6, 10, 0.84);
      padding: 24px;
    }
    .image-lightbox {
      width: min(100%, 1220px);
      max-height: 100%;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
      gap: 12px;
      padding: 16px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: rgba(15, 17, 21, 0.98);
      box-shadow: 0 30px 90px rgba(0, 0, 0, 0.48);
    }
    .image-lightbox-stage {
      min-height: 0;
      display: grid;
      place-items: center;
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #0b0d11;
      padding: 12px;
    }
    .image-lightbox-image {
      display: block;
      max-width: 100%;
      max-height: min(78vh, 920px);
      width: auto;
      height: auto;
      object-fit: contain;
      border-radius: 6px;
      background: #0b0d11;
    }
    .image-lightbox-caption {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .image-lightbox-close {
      width: 40px;
      height: 40px;
      min-width: 40px;
      padding: 0;
      justify-self: end;
      border-radius: 999px;
      font-size: 24px;
      line-height: 1;
      display: grid;
      place-items: center;
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
    .detail-ticket-headline {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
      line-height: 1.35;
    }
    .detail-stage-chip {
      display: inline-flex;
      align-items: center;
      width: fit-content;
      max-width: 100%;
      padding: 4px 9px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.05);
      color: var(--text);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.02em;
      text-transform: uppercase;
      white-space: nowrap;
    }
    .detail-stage-analysis {
      color: var(--accent);
      border-color: rgba(125, 211, 252, 0.35);
      background: rgba(125, 211, 252, 0.12);
    }
    .detail-stage-in_progress,
    .detail-stage-inspection {
      color: var(--warn);
      border-color: rgba(252, 211, 77, 0.35);
      background: rgba(252, 211, 77, 0.1);
    }
    .detail-stage-audit,
    .detail-stage-eric_review,
    .detail-stage-director_review {
      color: rgba(196, 181, 253, 0.96);
      border-color: rgba(196, 181, 253, 0.35);
      background: rgba(196, 181, 253, 0.12);
    }
    .detail-stage-done {
      color: var(--ok);
      border-color: rgba(134, 239, 172, 0.35);
      background: rgba(134, 239, 172, 0.1);
    }
    .detail-stage-cancelled,
    .detail-stage-backlog {
      color: var(--muted);
      border-color: rgba(148, 163, 184, 0.3);
      background: rgba(148, 163, 184, 0.08);
    }
    .detail-modal-body {
      overflow: auto;
      min-height: 0;
      overscroll-behavior: contain;
      -webkit-overflow-scrolling: touch;
      padding: 22px;
      padding-bottom: calc(22px + env(safe-area-inset-bottom, 0px));
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
    .eric-banner-confirmed {
      border-color: rgba(134, 239, 172, 0.42);
      background: linear-gradient(180deg, rgba(134, 239, 172, 0.18), rgba(134, 239, 172, 0.08));
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
    .eric-signoff-confirmation,
    .signoff-state-chip {
      display: inline-flex;
      width: fit-content;
      max-width: 100%;
      padding: 8px 12px;
      border-radius: 999px;
      border: 1px solid rgba(134, 239, 172, 0.4);
      background: rgba(134, 239, 172, 0.12);
      color: var(--ok);
      font-size: 13px;
      font-weight: 700;
      line-height: 1.35;
    }
    .eric-signoff-confirmation {
      width: 100%;
      justify-content: center;
      text-align: center;
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
    .compact-textarea {
      min-height: 88px;
    }
    .detail-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .field-label { font-size: 12px; color: var(--muted); text-transform: uppercase; }
    .field-preview {
      display: grid;
      gap: 6px;
      margin-top: 8px;
    }
    .field-preview-label {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
    }
    .body-text {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-size: 14px;
      line-height: 1.65;
      max-width: 84ch;
    }
    .ticket-ref-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .ticket-ref {
      width: auto;
      min-width: 0;
      padding: 3px 9px;
      border-radius: 999px;
      font-size: 12px;
      line-height: 1.3;
      color: var(--accent);
      border-color: rgba(125, 211, 252, 0.4);
      background: rgba(125, 211, 252, 0.12);
      display: inline-flex;
      align-items: center;
      vertical-align: baseline;
    }
    .ticket-ref:hover {
      background: rgba(125, 211, 252, 0.2);
    }
    .ticket-ref-missing,
    .ticket-ref:disabled {
      color: var(--muted);
      border-color: rgba(255,255,255,0.12);
      background: rgba(255,255,255,0.04);
      cursor: default;
    }
    .linked-text {
      display: block;
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
    .comment-urgent-marker {
      display: inline-block;
      margin-left: 8px;
      padding: 2px 6px;
      border-radius: 999px;
      background: rgba(248, 113, 113, 0.16);
      color: #fecaca;
      font-size: 0.72rem;
      font-weight: 800;
      letter-spacing: 0.04em;
    }
    @media (max-width: 1200px) {
      .detail-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 980px) {
      .detail-overlay {
        padding: 0;
      }
      .image-lightbox-overlay {
        padding: 12px;
      }
      .detail-modal {
        width: 100%;
        height: 100vh;
        max-height: 100vh;
        border-radius: 0;
        border-left: 0;
        border-right: 0;
      }
      .detail-modal-head {
        padding-top: calc(16px + env(safe-area-inset-top, 0px));
        padding-left: calc(16px + env(safe-area-inset-left, 0px));
        padding-right: calc(16px + env(safe-area-inset-right, 0px));
      }
      .detail-modal-body {
        padding-left: calc(16px + env(safe-area-inset-left, 0px));
        padding-right: calc(16px + env(safe-area-inset-right, 0px));
        padding-bottom: calc(16px + env(safe-area-inset-bottom, 0px));
      }
      .image-lightbox {
        width: 100%;
        max-height: 100%;
      }
      .image-lightbox-stage {
        padding: 8px;
      }
    }
    @media (max-width: 900px) {
      .board {
        min-width: 0;
        grid-template-columns: 1fr;
      }
      .mobile-section-toggle-panel {
        display: flex;
        justify-content: space-between;
        gap: 10px;
        align-items: center;
        border-radius: 0;
        border-left: 0;
        border-right: 0;
        border-top: 0;
        background: rgba(255,255,255,0.02);
        text-align: left;
      }
      .mobile-section-count {
        display: inline;
      }
      .mobile-section-chevron {
        display: inline-block;
        flex: 0 0 auto;
        color: var(--muted);
        transition: transform 120ms ease;
      }
      .mobile-section-toggle-panel[aria-expanded="false"] .mobile-section-chevron,
      .column-head[aria-expanded="false"] .mobile-section-chevron {
        transform: rotate(-90deg);
      }
      .column {
        min-height: 0;
        grid-template-rows: auto auto;
      }
      .column-head {
        border: 0;
        border-bottom: 1px solid var(--border);
        border-radius: 0;
        background: transparent;
        cursor: pointer;
      }
      .count {
        display: none;
      }
      .panel-head {
        position: static;
      }
      .panel-head h2 {
        display: none;
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
    @supports (height: 100dvh) {
      body,
      .layout,
      aside {
        min-height: 100dvh;
      }
      .detail-modal {
        height: min(100dvh - 36px, 100%);
        max-height: min(100dvh - 36px, 100%);
      }
      @media (max-width: 980px) {
        aside {
          min-height: 0;
        }
        .detail-modal {
          height: 100dvh;
          max-height: 100dvh;
        }
      }
    }
"""
