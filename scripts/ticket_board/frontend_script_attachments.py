"""Attachment gallery JavaScript: screenshot entries, set grouping and gallery rendering.

The preview and thumbnail URLs, a ticket's screenshot entries, the
target/attempt/feedback/named set parsing and ordering, and the gallery and
set-group renderers. SCRIPT_CORE splices this in at its original position, so
the page's single script is unchanged. It reads the shared `state` and calls
`cropMetadataCaption`, both defined in SCRIPT_CORE; SCRIPT_CORE, SCRIPT_DETAIL
and SCRIPT_APP call its functions.
"""

from __future__ import annotations

SCRIPT_ATTACHMENTS = """    function previewUrlFor(path) {
      return `/api/image/${encodeURIComponent(path)}`;
    }

    function thumbnailUrlFor(path) {
      return `/api/thumb/${encodeURIComponent(path)}?w=512`;
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

    function attachmentSetLabelSlug(slug) {
      return String(slug || '')
        .replace(/[-_]+/g, ' ')
        .replace(/([A-Za-z])(\\d)/g, '$1 $2')
        .replace(/\\s+/g, ' ')
        .trim()
        .split(' ')
        .map((word) => word.length <= 3 ? word.toUpperCase() : `${word.charAt(0).toUpperCase()}${word.slice(1)}`)
        .join(' ');
    }

    function parseAttachmentSet(path) {
      const filename = String(path || '').split('/').pop() || '';
      const targetMatch = filename.match(/^target__(.+)$/i);
      if (targetMatch) {
        return {
          key: 'target',
          type: 'target',
          label: 'Target',
          order: -1000000,
          itemLabel: targetMatch[1],
        };
      }
      const attemptMatch = filename.match(/^attempt-(\\d+)(?:-(.+?))?__(.+)$/i);
      if (attemptMatch) {
        const attemptNumber = Number.parseInt(attemptMatch[1], 10);
        const labelSlug = attemptMatch[2] || '';
        const suffix = attachmentSetLabelSlug(labelSlug);
        return {
          key: `attempt-${attemptMatch[1]}-${labelSlug}`,
          type: 'attempt',
          attemptNumber,
          label: `Render Attempt ${attemptMatch[1]}${suffix ? ` - ${suffix}` : ''}`,
          order: attemptNumber,
          itemLabel: attemptMatch[3],
        };
      }
      const feedbackMatch = filename.match(/^feedback-(\\d+)(?:-(.+?))?__(.+)$/i);
      if (feedbackMatch) {
        const feedbackNumber = Number.parseInt(feedbackMatch[1], 10);
        const labelSlug = feedbackMatch[2] || '';
        const suffix = attachmentSetLabelSlug(labelSlug);
        return {
          key: `feedback-${feedbackMatch[1]}-${labelSlug}`,
          type: 'feedback',
          feedbackNumber,
          label: `Feedback #${feedbackNumber}${suffix ? ` - ${suffix}` : ''}`,
          order: feedbackNumber,
          itemLabel: feedbackMatch[3],
        };
      }
      const namedSetMatch = filename.match(/^([a-z0-9][a-z0-9-]*)__(.+)$/i);
      if (namedSetMatch) {
        const label = attachmentSetLabelSlug(namedSetMatch[1]);
        return {
          key: `named-${namedSetMatch[1].toLowerCase()}`,
          type: 'named',
          label,
          order: 500000,
          itemLabel: namedSetMatch[2],
        };
      }
      return {
        key: 'ungrouped',
        type: 'ungrouped',
        label: 'Ungrouped',
        order: 1000000,
        itemLabel: filename,
      };
    }

    function groupAttachmentEntries(entries) {
      const groupsByKey = new Map();
      entries.forEach((entry) => {
        const parsed = parseAttachmentSet(entry.path);
        if (!groupsByKey.has(parsed.key)) {
          groupsByKey.set(parsed.key, {
            key: parsed.key,
            type: parsed.type,
            label: parsed.label,
            order: parsed.order,
            attemptNumber: parsed.attemptNumber || 0,
            feedbackNumber: parsed.feedbackNumber || 0,
            entries: [],
            open: false,
          });
        }
        groupsByKey.get(parsed.key).entries.push({
          ...entry,
          label: parsed.itemLabel || entry.label,
        });
      });
      const groups = Array.from(groupsByKey.values()).sort((left, right) => {
        if (left.type === 'target' && right.type !== 'target') {
          return -1;
        }
        if (right.type === 'target' && left.type !== 'target') {
          return 1;
        }
        if (left.type === 'attempt' && right.type === 'attempt') {
          return right.attemptNumber - left.attemptNumber || left.label.localeCompare(right.label);
        }
        if (left.type === 'attempt' && right.type !== 'attempt') {
          return -1;
        }
        if (right.type === 'attempt' && left.type !== 'attempt') {
          return 1;
        }
        if (left.type === 'feedback' && right.type === 'feedback') {
          return right.feedbackNumber - left.feedbackNumber || left.label.localeCompare(right.label);
        }
        if (left.type === 'feedback' && right.type !== 'feedback') {
          return -1;
        }
        if (right.type === 'feedback' && left.type !== 'feedback') {
          return 1;
        }
        if (left.type === 'named' && right.type === 'named') {
          return left.label.localeCompare(right.label);
        }
        if (left.type === 'named' && right.type !== 'named') {
          return -1;
        }
        if (right.type === 'named' && left.type !== 'named') {
          return 1;
        }
        return left.label.localeCompare(right.label);
      });
      const newestAttempt = groups
        .filter((group) => group.type === 'attempt')
        .sort((left, right) => right.attemptNumber - left.attemptNumber)[0];
      const newestFeedback = groups
        .filter((group) => group.type === 'feedback')
        .sort((left, right) => right.feedbackNumber - left.feedbackNumber)[0];
      if (newestAttempt) {
        newestAttempt.open = true;
      } else if (newestFeedback) {
        newestFeedback.open = true;
      } else if (groups.length === 1) {
        groups[0].open = true;
      }
      return groups;
    }

    function renderAttachmentGallery(container, entries, removeLabel, onRemove, onOpen = null) {
      container.innerHTML = '';
      entries.forEach((entry) => {
        const card = document.createElement('div');
        card.className = 'attachment-card';
        if (onOpen && entry.available) {
          card.classList.add('attachment-card-clickable');
          card.tabIndex = 0;
          card.setAttribute('role', 'button');
          card.setAttribute('aria-haspopup', 'dialog');
          card.setAttribute('aria-label', `Open attachment full size: ${entry.label}`);
          card.addEventListener('click', () => onOpen(entry));
          card.addEventListener('keydown', (event) => {
            if (event.target !== card) {
              return;
            }
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              onOpen(entry);
            }
          });
        }
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
          image.src = thumbnailUrlFor(entry.path);
          image.loading = 'lazy';
          image.decoding = 'async';
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
        const provenance = cropMetadataCaption(entry);
        if (provenance) {
          const provenanceLine = document.createElement('div');
          provenanceLine.className = 'attachment-provenance';
          provenanceLine.textContent = provenance;
          card.appendChild(provenanceLine);
        }
        container.appendChild(card);
      });
    }

    function renderAttachmentSetGroups(container, entries, removeLabel, onRemove, onOpen = null) {
      container.innerHTML = '';
      groupAttachmentEntries(entries).forEach((group) => {
        const details = document.createElement('details');
        details.className = `attachment-set attachment-set-${group.type}`;
        details.open = group.open;
        const summary = document.createElement('summary');
        summary.className = 'attachment-set-summary';
        summary.textContent = `${group.label} (${group.entries.length} image${group.entries.length === 1 ? '' : 's'})`;
        const gallery = document.createElement('div');
        gallery.className = 'attachment-gallery';
        renderAttachmentGallery(gallery, group.entries, removeLabel, onRemove, onOpen);
        details.append(summary, gallery);
        container.appendChild(details);
      });
    }

"""
