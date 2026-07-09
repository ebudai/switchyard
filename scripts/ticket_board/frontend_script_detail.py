"""Ticket-detail rendering JavaScript for the board frontend."""

SCRIPT_DETAIL = """    function selectedTicket() {
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
        const signoffRecorded = !!ticket.eric_signoff;
        const ericBanner = document.createElement('div');
        ericBanner.className = signoffRecorded ? 'eric-banner eric-banner-confirmed' : 'eric-banner';
        const ericBannerSubtitle = document.createElement('div');
        ericBannerSubtitle.className = 'eric-banner-subtitle';
        ericBannerSubtitle.textContent = signoffRecorded ? 'Signed Off ✓' : 'Awaiting Eric sign-off';
        const ericBannerTitle = document.createElement('div');
        ericBannerTitle.className = 'eric-banner-title';
        ericBannerTitle.textContent = ticket.title;
        const ericBannerNote = document.createElement('div');
        ericBannerNote.className = 'eric-banner-note';
        ericBannerNote.textContent = signoffRecorded
          ? 'Eric sign-off recorded. Waiting for director completion.'
          : ericReviewCheckText(ticket);
        const ericSummary = document.createElement('div');
        ericSummary.className = 'eric-summary';
        const ericSummaryHead = document.createElement('div');
        ericSummaryHead.className = 'eric-summary-head';
        ericSummaryHead.textContent = signoffRecorded ? 'Signed-off Review Snapshot' : 'Check Before Sign-off';
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
        if (signoffRecorded) {
          const confirmation = document.createElement('div');
          confirmation.className = 'eric-signoff-confirmation';
          confirmation.textContent = 'Signed off ✓ Waiting for director completion.';
          ericSummary.appendChild(confirmation);
        }
        const ericBannerActions = document.createElement('div');
        ericBannerActions.className = 'inline-actions';
        if (signoffRecorded) {
          const signedOffState = document.createElement('div');
          signedOffState.className = 'signoff-state-chip';
          signedOffState.textContent = 'Signed Off ✓';
          ericBannerActions.appendChild(signedOffState);
        } else {
          const ericBannerSignoffButton = document.createElement('button');
          ericBannerSignoffButton.className = 'primary';
          ericBannerSignoffButton.type = 'button';
          ericBannerSignoffButton.textContent = 'Sign Off';
          ericBannerSignoffButton.addEventListener('click', async () => {
            try {
              await submitEricSignoff(ticket.id, commentWho.value, commentText.value);
            } catch (error) {
              setCreateStatus(error.message, true);
              await requestBoardReload();
            }
          });
          ericBannerActions.appendChild(ericBannerSignoffButton);
        }
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
      const mergeTargetInput = document.createElement('input');
      mergeTargetInput.type = 'text';
      mergeTargetInput.placeholder = 'PGU-123';
      mergeTargetInput.setAttribute('aria-label', 'Merge target ticket');
      bindDetailDraftField(draftFields, mergeTargetInput, 'mergeTarget', '');
      const mergeButton = document.createElement('button');
      mergeButton.textContent = 'Merge Into…';
      mergeButton.addEventListener('click', async () => {
        try {
          const targetId = mergeTargetInput.value.trim().toUpperCase();
          if (!targetId) {
            throw new Error('merge requires a target ticket ID');
          }
          const confirmed = window.confirm(
            `Merge ${ticket.id} into ${targetId}? ${ticket.id}'s comments/attachments move to ${targetId} and ${ticket.id} is closed as merged.`,
          );
          if (!confirmed) {
            return;
          }
          await mergeTicket(ticket.id, targetId);
        } catch (error) {
          setCreateStatus(error.message, true);
          await requestBoardReload();
        }
      });
      titleActions.append(mergeTargetInput, mergeButton);
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
        metaLine3.appendChild(document.createTextNode('Blocked By: '));
        metaLine3.appendChild(linkedTicketRow(ticket.blocked_by || []));
        meta.appendChild(metaLine3);
      }

      const body = document.createElement('div');
      body.innerHTML = '<div class="field-label">Body</div>';
      body.appendChild(linkedTextBlock(ticket.body, '(no body)'));

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
      const blockedByLinks = document.createElement('div');
      blockedByLinks.className = 'field-preview';
      const blockedByLinksLabel = document.createElement('div');
      blockedByLinksLabel.className = 'field-preview-label';
      blockedByLinksLabel.textContent = 'Linked Tickets';
      blockedByLinks.append(blockedByLinksLabel, linkedTicketRow(ticket.blocked_by || []));
      blockedBy.append(blockedByInput, blockedByActions, blockedByNote, blockedByLinks);

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
      implementation.append(
        implementationInput,
        implementationActions,
        linkedPreview('Rendered Preview', ticket.implementation, '(no implementation yet)'),
      );

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
      auditPrompt.append(
        auditPromptInput,
        auditPromptActions,
        linkedPreview('Rendered Preview', ticket.audit_prompt, '(no audit prompt yet)'),
      );

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
          text.className = 'body-text linked-text';
          appendLinkedTicketText(text, comment.text);
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
"""
