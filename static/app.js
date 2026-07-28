// wikipathways-submit client behavior
// No build step: plain ES5-ish JS, event delegation, optimistic UI updates.
(function () {
  'use strict';

  // Whether approving merges the pull request or hands it to the target repository's own
  // publication workflow. Set from the server context; the two read very differently on screen.
  var PIPELINE_MODE = document.body.getAttribute('data-publish-mode') === 'pipeline';

  // ---------- toasts ----------
  function toast(message, type) {
    var region = document.getElementById('toast-region');
    if (!region) return;
    var el = document.createElement('div');
    el.className = 'toast' + (type ? ' toast--' + type : '');
    el.setAttribute('role', type === 'error' ? 'alert' : 'status');
    var text = document.createElement('span');
    text.textContent = message;
    el.appendChild(text);
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast__close';
    close.setAttribute('aria-label', 'Dismiss notification');
    close.innerHTML = '&times;';
    close.addEventListener('click', function () { el.remove(); });
    el.appendChild(close);
    // The region is fixed to a corner and does not scroll, so an unbounded stack grows off the
    // top of the screen — taking its own close buttons with it — and covers the card actions
    // underneath. Three is enough to see that several things went wrong.
    while (region.children.length >= 3) { region.firstChild.remove(); }
    region.appendChild(el);
    // An error is not a notification you can afford to miss: it is usually a list of validation
    // reasons, and six seconds is not enough to read one, let alone copy it. Errors stay until
    // dismissed; everything else still self-clears.
    if (type !== 'error') {
      setTimeout(function () { if (el.parentNode) el.remove(); }, 6000);
    }
  }

  // Where there is somewhere permanent on the page to put an error, put it there instead of in
  // a toast: the result card holds the full text, can be re-read and copied, and does not sit
  // on top of the controls.
  function showError(outletId, message) {
    var out = document.getElementById(outletId);
    if (!out) { toast(message, 'error'); return; }
    out.className = 'result-card result-card--error';
    out.setAttribute('role', 'alert');
    out.textContent = message;
    out.hidden = false;
  }

  // Speak to a pathway author, not to a developer: no status codes in the message. The API's
  // own `detail` is already written for humans, so prefer it; fall back per status.
  var STATUS_FALLBACK = {
    401: 'Your session has expired. Log in with GitHub again.',
    403: 'You do not have permission to do that.',
    404: 'That pathway or review no longer exists.',
    409: 'Someone else is editing this pathway right now.',
    413: 'That file is too large to upload.',
    502: 'GitHub did not accept the change. Try again in a moment.',
    503: 'That part of the app is not configured yet.'
  };
  function describeError(status, body) {
    var detail = body && body.detail;
    if (detail && Array.isArray(detail.errors)) return detail.errors.join('; ');
    if (typeof detail === 'string') return detail;
    if (detail && detail.reason) {
      return detail.held_by
        ? 'This pathway is checked out by @' + detail.held_by + '. Try again once their edit is merged.'
        : String(detail.reason);
    }
    return STATUS_FALLBACK[status] || 'Something went wrong. Try again.';
  }

  function postForm(url, fields) {
    var fd = new FormData();
    for (var k in fields) if (Object.prototype.hasOwnProperty.call(fields, k)) fd.append(k, fields[k]);
    return fetch(url, { method: 'POST', body: fd }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        return { ok: r.ok, status: r.status, body: j };
      });
    });
  }

  // ---------- logout ----------
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-action="logout"]');
    if (!btn) return;
    if (btn.disabled) return;
    btn.disabled = true;
    // Navigate either way: the session lives on the server, so reloading / shows the truth
    // whether or not the POST landed. Without this the button just looks dead.
    fetch('/auth/logout', { method: 'POST' })
      .catch(function () { toast('Could not reach the server; reloading to check your session.', 'error'); })
      .finally(function () { location.href = '/'; });
  });

  // ---------- tabs (index.html, logged in) ----------
  var tabs = document.querySelectorAll('.tab');
  function selectTab(name) {
    tabs.forEach(function (x) {
      var on = x.getAttribute('data-tab') === name;
      x.classList.toggle('tab--active', on);
      x.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.tab-panel').forEach(function (p) {
      p.classList.toggle('tab-panel--hidden', p.getAttribute('data-panel') !== name);
    });
  }
  tabs.forEach(function (t) {
    t.addEventListener('click', function () { selectTab(t.getAttribute('data-tab')); });
  });


  // ---------- new-pathway submit (single action — validate + open PR together) ----------
  var submitForm = document.getElementById('submit-form');
  if (submitForm) {
    submitForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var file = document.getElementById('new-file').files[0];
      var submitBtn = document.getElementById('submit-btn');
      var resultCard = document.getElementById('result-card');
      if (!file) { showError('result-card', 'Choose a .gpml file first.'); return; }
      submitBtn.disabled = true;
      submitBtn.textContent = 'Submitting…';
      var fd = new FormData();
      fd.append('file', file);
      var submitDesc = document.getElementById('submit-description');
      if (submitDesc) { fd.append('description', submitDesc.value || ''); }
      fetch('/api/submit', { method: 'POST', body: fd })
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, body: j }; }); })
        .then(function (res) {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Submit new pathway';
          if (!res.ok) { showError('result-card', describeError(res.status, res.body)); return; }
          var pr = '<a href="' + res.body.pr_url + '" target="_blank" rel="noopener">#' + res.body.pr_number + '</a>';
          var mine = ' <a href="/dashboard/' + res.body.pr_number + '">Follow the review</a>.';
          // Not "Assigned WP0001": where the database assigns the identifier at publication,
          // the app committed a placeholder and has no id to report. Saying otherwise sends
          // people back to the update tab with a number that means a different pathway.
          resultCard.className = 'result-card';
          // A previous failure left role="alert" on this container; leaving it there makes a
          // screen reader interrupt with the success message as though it were another error.
          resultCard.removeAttribute('role');
          resultCard.innerHTML = PIPELINE_MODE
            ? 'Submitted. Opened pull request ' + pr + ' (<code>' + res.body.path + '</code>). ' +
              'The WPID is assigned by the database when a curator approves it, not now.' + mine
            : 'Assigned <strong>' + res.body.wpid + '</strong>. Opened pull request ' + pr +
              ' (<code>' + res.body.path + '</code>).' + mine;
          resultCard.hidden = false;
          toast('Pathway submitted.', 'success');
        })
        .catch(function () {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Submit new pathway';
          showError('result-card', 'Could not reach the server. Try again.');
        });
    });
  }

  // ---------- WPID field: normalise to WP#### and verify the pathway exists ----------
  var wpidInput = document.getElementById('update-wpid');
  var wpidStatus = document.getElementById('update-wpid-status');
  function wpidNumber(v) { return (v || '').replace(/\D/g, ''); }
  // A WikiPathways identifier never carries a leading zero, so "WP0001" is not WP1 — it is the
  // placeholder this app commits before the database assigns an id. Stripping the zeros would
  // silently point the upload at a real, unrelated pathway.
  function isPadded(digits) { return digits.length > 1 && digits.charAt(0) === '0'; }
  function paddedMessage(digits) {
    // WP0001 is not a typo for WP1 — it is what a submission carries before it has an id, so
    // offering WP1 there would send them to a real and unrelated pathway.
    if (digits === '0001') {
      return 'WP0001 is the placeholder a new submission carries until the database assigns its ' +
        'id, not a pathway. Find your submission under My submissions to revise it.';
    }
    var real = digits.replace(/^0+/, '');
    return 'WikiPathways identifiers have no leading zeros.' +
      (real ? ' Did you mean WP' + real + '?' : '');
  }
  if (wpidInput) {
    wpidInput.addEventListener('blur', function () {
      var num = wpidNumber(wpidInput.value);
      if (!num) { wpidInput.value = ''; if (wpidStatus) wpidStatus.hidden = true; return; }
      wpidInput.value = 'WP' + num;  // always display WP####
      if (!wpidStatus) return;
      if (isPadded(num)) {
        delete wpidInput.dataset.state;
        wpidStatus.hidden = false;
        wpidStatus.className = 'wpid-status wpid-status--err';
        wpidStatus.textContent = paddedMessage(num);
        return;
      }
      wpidStatus.hidden = false;
      wpidStatus.className = 'wpid-status wpid-status--checking';
      wpidStatus.textContent = 'Checking WP' + num + '…';
      fetch('/api/pathways/' + num)
        .then(function (r) {
          // A 401/502 must NOT read as "absent": telling someone an existing pathway does not
          // exist sends them to the New-pathway tab, which burns a WPID on a duplicate.
          if (r.ok) return r.json();
          return r.json().catch(function () { return {}; }).then(function (body) {
            return { lookupFailed: true, message: describeError(r.status, body) };
          });
        })
        .then(function (info) {
          if (!info) { wpidStatus.hidden = true; return; }
          if (info.lookupFailed) {
            delete wpidInput.dataset.state;
            wpidStatus.className = 'wpid-status wpid-status--error';
            wpidStatus.textContent = 'Could not check WP' + num + '. ' + info.message;
            return;
          }
          wpidInput.dataset.state = info.state;
          if (info.state === 'on_main') {
            wpidStatus.className = 'wpid-status wpid-status--ok';
            wpidStatus.textContent = 'Found ' + info.wpid + (info.name ? ': ' + info.name : '') + '. Uploading opens an update.';
            setUpdateLabel('Submit update');
          } else if (info.state === 'pending_new') {
            wpidStatus.className = 'wpid-status wpid-status--pending';
            wpidStatus.textContent = info.wpid + ' is still an open submission (pull request #' + info.pr_number + '). Uploading revises it.';
            setUpdateLabel('Submit revision');
          } else {
            wpidStatus.className = 'wpid-status wpid-status--err';
            wpidStatus.textContent = info.wpid + ' does not exist yet. Use the "New pathway" tab.';
            // Otherwise the button keeps whatever the *previous* lookup put there — offering
            // "Submit revision" for a pathway the line above says does not exist.
            setUpdateLabel(updateLabelForState());
          }
        })
        .catch(function () { wpidStatus.hidden = true; });
    });
  }

  // /?wpid=WP554 opens the update form with that pathway filled in. A curator's change request
  // on an *update* has to send the submitter somewhere that works, and without this the link
  // would land them on the New pathway tab — the one thing that must not happen, since it would
  // file their fix as a second, duplicate pathway.
  //
  // Placed after the blur listener above, not next to the tab handlers: it dispatches a blur to
  // run the presence lookup, and a dispatch made before that listener exists reaches nothing.
  (function prefillFromQuery() {
    if (!wpidInput || !tabs.length) return;
    var match = /[?&]wpid=(WP)?(\d+)/i.exec(window.location.search);
    if (!match) return;
    selectTab('update');
    wpidInput.value = 'WP' + match[2];
    wpidInput.dispatchEvent(new Event('blur'));
  })();

  // The blur handler and the click handler race: blur fires first when someone tabs or clicks
  // straight from the WPID field to the button, so a lookup can land after the submit handler
  // already wrote "Submitting…" and disabled the button. The user then sees an idle-looking
  // label on a dead button. While a submission is in flight the label is not the lookup's to set.
  function setUpdateLabel(text) {
    var btn = document.getElementById('update-btn');
    if (!btn || btn.disabled) return;
    btn.textContent = text;
  }
  function updateLabelForState() {
    var state = wpidInput ? wpidInput.dataset.state : null;
    return state === 'pending_new' ? 'Submit revision' : 'Submit update';
  }

  // Bound to the form's submit, not the button's click: with a single text field the browser
  // implicitly submits on Enter, which used to reload the page and discard the whole form.
  var updateForm = document.getElementById('update-form');
  if (updateForm) {
    updateForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var wpid = (document.getElementById('update-wpid').value || '').trim().replace(/^WP/i, '');
      var file = document.getElementById('update-file').files[0];
      if (!/^\d+$/.test(wpid)) { showError('update-result', 'Enter a numeric WPID, like 554.'); return; }
      if (isPadded(wpid)) { showError('update-result', paddedMessage(wpid)); return; }
      if (!file) { showError('update-result', 'Choose a .gpml file first.'); return; }
      // Compose the "what changed" note from the ticked options + the free-text details.
      var updateDesc = document.getElementById('update-description');
      var detail = updateDesc ? (updateDesc.value || '').trim() : '';
      var otherBox = document.getElementById('update-change-other');
      if (otherBox && otherBox.checked && !detail) {
        showError('update-result', 'You ticked Other, so say what changed in Details.'); return;
      }
      var changes = [];
      document.querySelectorAll('.update-change:checked').forEach(function (cb) {
        changes.push(cb.getAttribute('data-label'));
      });
      var parts = [];
      if (changes.length) { parts.push(changes.join('; ') + '.'); }
      if (detail) { parts.push(detail); }
      var description = parts.join('\n');
      var btn = document.getElementById('update-btn');
      btn.disabled = true;
      btn.textContent = 'Submitting…';
      var prevOut = document.getElementById('update-result');
      if (prevOut) { prevOut.hidden = true; prevOut.innerHTML = ''; prevOut.className = 'result-card'; }
      var fd = new FormData();
      fd.append('file', file);
      fd.append('description', description);
      // Recomputed from the field's last lookup, not from a label captured before it: an
      // in-flight lookup could have been about to change it.
      function reset() { btn.disabled = false; btn.textContent = updateLabelForState(); }
      // Route by where the WPID lives: an existing pathway → update; a still-open new
      // submission → revise (commit onto its PR); nowhere → tell the user.
      fetch('/api/pathways/' + wpid)
        .then(function (r) {
          if (r.ok) return r.json();
          return r.json().catch(function () { return {}; }).then(function (body) {
            return { lookupFailed: true, message: describeError(r.status, body) };
          });
        })
        .then(function (info) {
          if (info.lookupFailed) {
            // Unknown whether WP#### exists — stop rather than route the upload on a guess.
            reset();
            showError('update-result', 'Could not check WP' + wpid + '. ' + info.message);
            return null;
          }
          if (info.state === 'absent') {
            reset();
            showError('update-result', info.wpid + ' does not exist yet. Use the "New pathway" tab.');
            return null;
          }
          // A revision is keyed by pull request, not by WPID: a new submission has no WPID
          // until the repository publishes it, so there is no /api/pathways/<id>/revise.
          var revising = info.state === 'pending_new';
          if (revising && !info.pr_number) {
            reset();
            showError('update-result', 'Could not find the pull request for ' + info.wpid + '. Open it from the dashboard instead.');
            return null;
          }
          var url = revising
            ? '/api/reviews/' + info.pr_number + '/revise'
            : '/api/pathways/' + wpid + '/update';
          var verb = revising ? 'revise' : 'update';
          return fetch(url, { method: 'POST', body: fd })
            .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, body: j, verb: verb }; }); });
        })
        .then(function (res) {
          if (!res) return;
          reset();
          if (!res.ok) { showError('update-result', describeError(res.status, res.body)); return; }
          var word = res.verb === 'revise' ? 'Revised' : 'Updated';
          var noun = res.verb === 'revise' ? 'Revision' : 'Update';
          var out = document.getElementById('update-result');
          out.className = 'result-card';
          out.removeAttribute('role');
          out.innerHTML =
            word + ' <strong>' + (res.verb === 'revise' && PIPELINE_MODE ? 'the submission' : res.body.wpid) +
            '</strong> on pull request ' +
            '<a href="' + res.body.pr_url + '" target="_blank" rel="noopener">#' + res.body.pr_number + '</a> ' +
            '(<code>' + res.body.path + '</code>). <a href="/dashboard/' + res.body.pr_number + '">Follow the review</a>.';
          out.hidden = false;
          toast(noun + ' submitted.', 'success');
        })
        .catch(function () {
          reset();
          showError('update-result', 'Could not reach the server. Try again.');
        });
    });
  }

  // ---------- review cards (dashboard.html / review_detail.html) ----------
  // Mirrors the state_icon() macro in templates/dashboard.html: the pill's glyph is the
  // non-colour cue for its state, so a JS update has to redraw it, not just the word.
  var PILL_ICON = {
    pass: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3 8.5l3 3 7-7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    fail: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    na: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 8h8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    pending: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="8" cy="8" r="4" stroke="currentColor" stroke-width="2"/></svg>'
  };
  var PILL_LABEL = { pass: 'Pass', fail: 'Fail', na: 'N/A', pending: 'Not checked' };
  function setPillContent(pill, state) {
    // The glyph is the non-colour cue, and the word beside it is the label the template wrote.
    // Replacing the pill's whole contents with the raw state would drop the icon and print the
    // identifier, so the row a curator just clicked would be the one that looks wrong.
    pill.innerHTML = (PILL_ICON[state] || '') + ' ';
    var text = document.createElement('span');
    text.className = 'state-pill__text';
    text.textContent = PILL_LABEL[state] || state;
    pill.appendChild(text);
  }

  function recomputeApprove(card) {
    var approveBtn = card.querySelector('.btn--approve');
    if (!approveBtn) return;
    var required = card.querySelectorAll('.checklist__item[data-required="true"]');
    var allPass = true;
    required.forEach(function (li) {
      var pill = li.querySelector('.state-pill');
      if (!pill || pill.dataset.state !== 'pass') allPass = false;
    });
    if (allPass) {
      approveBtn.removeAttribute('aria-disabled');
      approveBtn.disabled = false;
    } else {
      approveBtn.setAttribute('aria-disabled', 'true');
      approveBtn.disabled = true;
    }
  }

  // Reconcile every pill/chip in a card against the authoritative server checklist, so a write
  // that the server resolved differently than our optimistic click (e.g. after a concurrent
  // update, issue #15) becomes visible instead of silently diverging.
  function reconcileChecklist(card, checklist) {
    if (!checklist) return;
    checklist.forEach(function (srv) {
      var item = card.querySelector('.checklist__item[data-key="' + srv.key + '"]');
      if (!item) return;
      var pill = item.querySelector('.state-pill');
      if (pill) {
        pill.className = 'state-pill state-pill--' + srv.state;
        pill.setAttribute('data-state', srv.state);
        setPillContent(pill, srv.state);
      }
      item.querySelectorAll('.chip-btn').forEach(function (b) {
        b.setAttribute('aria-pressed', b.getAttribute('data-state') === srv.state ? 'true' : 'false');
      });
    });
    recomputeApprove(card);
  }

  // Only one of the two decision panels is open at a time: they sit in the same place and both
  // end in an irreversible action, so leaving the other one open invites the wrong click.
  function togglePanel(card, selector, focusSelector) {
    if (!card) return;
    card.querySelectorAll('.changes-panel, .reject-panel').forEach(function (p) {
      if (!p.matches(selector)) p.hidden = true;
    });
    var panel = card.querySelector(selector);
    if (!panel) return;
    panel.hidden = !panel.hidden;
    if (!panel.hidden) {
      var focusable = panel.querySelector(focusSelector);
      if (focusable) focusable.focus();
    }
  }

  document.querySelectorAll('.review-card').forEach(recomputeApprove);

  document.addEventListener('click', function (e) {
    var chip = e.target.closest('.chip-btn');
    if (chip) {
      var card = chip.closest('.review-card');
      var item = chip.closest('.checklist__item');
      var pr = card.getAttribute('data-pr');
      var key = item.getAttribute('data-key');
      var state = chip.getAttribute('data-state');
      var siblingChips = item.querySelectorAll('.chip-btn');
      siblingChips.forEach(function (b) { b.disabled = true; });
      postForm('/api/reviews/' + pr + '/checklist', { key: key, state: state })
        .then(function (res) {
          siblingChips.forEach(function (b) { b.disabled = false; });
          if (!res.ok) { toast(describeError(res.status, res.body), 'error'); return; }
          // Trust the server's checklist (the full ReviewDetail), not just the clicked item.
          reconcileChecklist(card, res.body && res.body.checklist);
        })
        .catch(function () {
          siblingChips.forEach(function (b) { b.disabled = false; });
          toast('Could not reach the server. Try again.', 'error');
        });
      return;
    }

    var approveBtn = e.target.closest('.btn--approve');
    if (approveBtn) {
      if (approveBtn.getAttribute('aria-disabled') === 'true') return;
      var card2 = approveBtn.closest('.review-card');
      var pr2 = card2.getAttribute('data-pr');
      var originalLabel = approveBtn.textContent;
      approveBtn.disabled = true;
      // Nothing is merged where the repository publishes through its own Actions. Saying
      // "Merging…" there sends the curator to GitHub to look for a merge that never happens.
      approveBtn.textContent = PIPELINE_MODE ? 'Handing it over…' : 'Merging…';
      fetch('/api/reviews/' + pr2 + '/approve', { method: 'POST' })
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, body: j }; }); })
        .then(function (res) {
          if (!res.ok) {
            approveBtn.disabled = false;
            approveBtn.textContent = originalLabel;
            toast(describeError(res.status, res.body), 'error');
            return;
          }
          toast(PIPELINE_MODE
            ? 'Approved. The repository assigns the WPID and publishes it from here.'
            : 'Approved and merged.', 'success');
          setTimeout(function () { location.reload(); }, 700);
        })
        .catch(function () {
          approveBtn.disabled = false;
          approveBtn.textContent = originalLabel;
          toast('Could not reach the server. Try again.', 'error');
        });
      return;
    }

    var changesBtn = e.target.closest('.btn--changes');
    if (changesBtn) {
      togglePanel(changesBtn.closest('.review-card'), '.changes-panel', '.changes-note');
      return;
    }

    var rejectBtn = e.target.closest('.btn--reject');
    if (rejectBtn) {
      togglePanel(rejectBtn.closest('.review-card'), '.reject-panel', '.reject-note');
      return;
    }

    var rejectSend = e.target.closest('.btn--reject-send');
    if (rejectSend) {
      var cardJ = rejectSend.closest('.review-card');
      var prJ = cardJ.getAttribute('data-pr');
      var noteJ = cardJ.querySelector('.reject-note');
      rejectSend.disabled = true;
      rejectSend.textContent = 'Rejecting…';
      postForm('/api/reviews/' + prJ + '/reject', { note: noteJ ? noteJ.value : '' })
        .then(function (res) {
          if (!res.ok) {
            rejectSend.disabled = false;
            rejectSend.textContent = 'Reject this submission';
            toast(describeError(res.status, res.body), 'error');
            return;
          }
          toast('Submission rejected.', 'success');
          setTimeout(function () { location.reload(); }, 700);
        })
        .catch(function () {
          rejectSend.disabled = false;
          rejectSend.textContent = 'Reject this submission';
          toast('Could not reach the server. Try again.', 'error');
        });
      return;
    }

    var recordBtn = e.target.closest('.btn--record-wpid');
    if (recordBtn) {
      var cardW = recordBtn.closest('.review-card');
      var prW = cardW.getAttribute('data-pr');
      var field = cardW.querySelector('.published-wpid');
      var digits = wpidNumber(field ? field.value : '');
      if (!digits || isPadded(digits)) {
        toast(digits ? paddedMessage(digits) : 'Enter the WPID the database assigned, like WP5678.', 'error');
        if (field) field.focus();
        return;
      }
      recordBtn.disabled = true;
      recordBtn.textContent = 'Recording…';
      postForm('/api/reviews/' + prW + '/published-wpid', { wpid: digits })
        .then(function (res) {
          if (!res.ok) {
            recordBtn.disabled = false;
            recordBtn.textContent = 'Record it';
            toast(describeError(res.status, res.body), 'error');
            return;
          }
          toast('Recorded as WP' + digits + '.', 'success');
          setTimeout(function () { location.reload(); }, 700);
        })
        .catch(function () {
          recordBtn.disabled = false;
          recordBtn.textContent = 'Record it';
          toast('Could not reach the server. Try again.', 'error');
        });
      return;
    }

    var reviseBtn = e.target.closest('.btn--revise');
    if (reviseBtn) {
      var cardR = reviseBtn.closest('.review-card');
      var prR = cardR.getAttribute('data-pr');
      var fileEl = cardR.querySelector('.revise-file');
      if (!fileEl || !fileEl.files || !fileEl.files.length) {
        toast('Choose a GPML file first.', 'error');
        return;
      }
      reviseBtn.disabled = true;
      reviseBtn.textContent = 'Uploading\u2026';
      postForm('/api/reviews/' + prR + '/revise', { file: fileEl.files[0] })
        .then(function (res) {
          if (res.ok) {
            toast('Revision committed onto pull request #' + res.body.pr_number + '.', 'success');
            setTimeout(function () { location.reload(); }, 900);
            return;
          }
          // 422 carries the per-reason validation list; anything else has a plain detail string.
          var d = res.body && res.body.detail;
          var msg = d && d.errors ? d.errors.join('; ') : (d || 'Upload failed.');
          toast(msg, 'error');
          reviseBtn.disabled = false;
          reviseBtn.textContent = 'Commit onto this pull request';
        })
        .catch(function () {
          toast('Could not reach the server. Try again.', 'error');
          reviseBtn.disabled = false;
          reviseBtn.textContent = 'Commit onto this pull request';
        });
      return;
    }

    var sendBtn = e.target.closest('.btn--changes-send');
    if (sendBtn) {
      var card3 = sendBtn.closest('.review-card');
      var pr3 = card3.getAttribute('data-pr');
      var noteEl = card3.querySelector('.changes-note');
      sendBtn.disabled = true;
      sendBtn.textContent = 'Sending…';
      postForm('/api/reviews/' + pr3 + '/request-changes', { note: noteEl ? noteEl.value : '' })
        .then(function (res) {
          sendBtn.disabled = false;
          sendBtn.textContent = 'Send request';
          if (!res.ok) { toast(describeError(res.status, res.body), 'error'); return; }
          toast('Changes requested.', 'success');
          setTimeout(function () { location.reload(); }, 600);
        })
        .catch(function () {
          sendBtn.disabled = false;
          sendBtn.textContent = 'Send request';
          toast('Could not reach the server. Try again.', 'error');
        });
    }
  });

  // ---------- pan / zoom of a rendered preview ----------
  // Zoom by resizing the element (not CSS transform: scale) so the SVG re-rasterizes crisply at
  // the new size; pan with a translate. Scaling an <img>-loaded SVG with transform only magnifies
  // the already-rasterized bitmap, which is blurry.
  function initZoom(root) {
    var img = root.querySelector('.zoom__img');
    var viewport = root.querySelector('.zoom__viewport');
    if (!img || !viewport) return;
    var scale = 1, tx = 0, ty = 0, min = 1, max = 8;
    var dragging = false, sx = 0, sy = 0, baseW = 0, baseH = 0;

    function measure() {
      // Fitted size at scale 1 (max-width/height:100%, natural aspect). Basis for crisp resizing.
      var prev = img.style.cssText;
      img.style.width = ''; img.style.height = '';
      img.style.maxWidth = '100%'; img.style.maxHeight = '100%'; img.style.transform = 'none';
      var r = img.getBoundingClientRect();
      baseW = r.width; baseH = r.height;
      img.style.cssText = prev;
    }
    function apply() {
      if (scale > 1.001 && baseW) {
        img.style.maxWidth = 'none'; img.style.maxHeight = 'none';
        img.style.width = (baseW * scale) + 'px';
        img.style.height = (baseH * scale) + 'px';
      } else {
        img.style.width = ''; img.style.height = '';
        img.style.maxWidth = '100%'; img.style.maxHeight = '100%';
      }
      img.style.transform = 'translate(' + tx + 'px,' + ty + 'px)';
      root.classList.toggle('zoom--zoomed', scale > 1.001);
    }
    function clamp(s) { return Math.max(min, Math.min(max, s)); }
    // Keep the diagram overlapping its frame. Without this, holding an arrow key walks it
    // entirely out of a viewport that clips at overflow:hidden, with nothing left on screen.
    function clampPan() {
      if (!baseW) return;
      var r = viewport.getBoundingClientRect();
      var limitX = Math.max(0, (baseW * scale - r.width) / 2);
      var limitY = Math.max(0, (baseH * scale - r.height) / 2);
      tx = Math.max(-limitX, Math.min(limitX, tx));
      ty = Math.max(-limitY, Math.min(limitY, ty));
    }
    function reset() { scale = 1; tx = 0; ty = 0; apply(); }
    function zoomAt(factor, cx, cy) {
      if (!baseW) measure();
      var rect = viewport.getBoundingClientRect();
      var ox = cx - rect.left - rect.width / 2 - tx;
      var oy = cy - rect.top - rect.height / 2 - ty;
      var ns = clamp(scale * factor), ratio = ns / scale;
      tx -= ox * (ratio - 1); ty -= oy * (ratio - 1); scale = ns;
      if (scale === 1) { tx = 0; ty = 0; }
      apply();
    }
    function center() { var r = viewport.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }

    if (img.complete && img.naturalWidth) { measure(); } else { img.addEventListener('load', measure); }

    viewport.addEventListener('wheel', function (e) {
      // Don't hijack the page scroll: a queue card is mostly preview, so swallowing every wheel
      // event traps the reader. Ctrl+wheel (the pinch gesture) zooms from any scale; a plain
      // wheel only once they have zoomed in and are navigating inside the diagram.
      if (!e.ctrlKey && scale <= 1.001) return;
      e.preventDefault();
      zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX, e.clientY);
    }, { passive: false });
    viewport.addEventListener('pointerdown', function (e) {
      if (scale <= 1) return;
      dragging = true; sx = e.clientX - tx; sy = e.clientY - ty;
      viewport.setPointerCapture(e.pointerId);
    });
    viewport.addEventListener('pointermove', function (e) {
      if (!dragging) return;
      tx = e.clientX - sx; ty = e.clientY - sy; apply();
    });
    function end(e) { if (dragging) { dragging = false; try { viewport.releasePointerCapture(e.pointerId); } catch (_) {} } }
    viewport.addEventListener('pointerup', end);
    viewport.addEventListener('pointercancel', end);

    // Keyboard: the viewport clips at overflow:hidden and has no scrollbars, so without this a
    // curator who cannot use a pointer can zoom to 8x and then only ever see the centre of the
    // diagram. Reading node labels in a dense pathway is the review.
    var PAN_STEP = 40;
    viewport.addEventListener('keydown', function (e) {
      var handled = true;
      // Only pan once there is something to pan to. The viewport is focusable and every card
      // carries one or two, so swallowing the arrow keys at scale 1 would stop a keyboard user
      // scrolling the queue at all — it would slide the un-zoomed diagram out of its frame
      // instead, which is not even a thing they asked for.
      var panning = scale > 1.001;
      if (e.key === 'ArrowLeft' && panning) { tx += PAN_STEP; }
      else if (e.key === 'ArrowRight' && panning) { tx -= PAN_STEP; }
      else if (e.key === 'ArrowUp' && panning) { ty += PAN_STEP; }
      else if (e.key === 'ArrowDown' && panning) { ty -= PAN_STEP; }
      else if (e.key === 'Home') { reset(); }
      else if (e.key === '+' || e.key === '=') { var ci = center(); zoomAt(1.3, ci[0], ci[1]); }
      else if (e.key === '-' || e.key === '_') { var co = center(); zoomAt(1 / 1.3, co[0], co[1]); }
      else { handled = false; }
      if (!handled) return;
      e.preventDefault();
      clampPan();
      apply();
    });

    var zin = root.querySelector('[data-zoom-in]'), zout = root.querySelector('[data-zoom-out]'), zr = root.querySelector('[data-zoom-reset]');
    if (zin) zin.addEventListener('click', function () { var c = center(); zoomAt(1.3, c[0], c[1]); });
    if (zout) zout.addEventListener('click', function () { var c = center(); zoomAt(1 / 1.3, c[0], c[1]); });
    if (zr) zr.addEventListener('click', reset);
    apply();
  }
  document.querySelectorAll('[data-zoom]').forEach(initZoom);

  document.addEventListener('change', function (e) {
    var select = e.target.closest('.assign__select');
    if (!select) return;
    var card = select.closest('.review-card');
    var pr = card.getAttribute('data-pr');
    select.disabled = true;
    postForm('/api/reviews/' + pr + '/assign', { curator: select.value })
      .then(function (res) {
        select.disabled = false;
        if (!res.ok) { toast(describeError(res.status, res.body), 'error'); return; }
        // The server-rendered "assigned to" line would otherwise contradict the select.
        var line = card.querySelector('.review-card__submitter');
        if (line && res.body && res.body.submitter) {
          line.textContent = '';
          line.appendChild(document.createTextNode('Submitted by '));
          var who = document.createElement('strong');
          who.textContent = '@' + res.body.submitter;
          line.appendChild(who);
          if (res.body.assigned_curator) {
            line.appendChild(document.createTextNode(' · assigned to '));
            var cur = document.createElement('strong');
            cur.textContent = '@' + res.body.assigned_curator;
            line.appendChild(cur);
          }
        }
        toast(select.value ? 'Assigned to @' + select.value + '.' : 'Unassigned.', 'success');
      })
      .catch(function () { select.disabled = false; toast('Could not reach the server. Try again.', 'error'); });
  });
})();
