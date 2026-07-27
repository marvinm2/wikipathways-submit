// wikipathways-submit client behavior
// No build step: plain ES5-ish JS, event delegation, optimistic UI updates.
(function () {
  'use strict';

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
    region.appendChild(el);
    setTimeout(function () { if (el.parentNode) el.remove(); }, 6000);
  }

  function describeError(status, body) {
    var detail = body && body.detail;
    var msg;
    if (detail && Array.isArray(detail.errors)) msg = detail.errors.join('; ');
    else if (typeof detail === 'string') msg = detail;
    else if (detail) { try { msg = JSON.stringify(detail); } catch (e) { msg = 'unknown error'; } }
    else msg = 'Something went wrong.';
    return 'Error ' + status + ': ' + msg;
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
    fetch('/auth/logout', { method: 'POST' }).then(function () { location.href = '/'; });
  });

  // ---------- submit flow (index.html, logged in) ----------
  var validateForm = document.getElementById('validate-form');
  var submitForm = document.getElementById('submit-step');
  var fileInput = document.getElementById('file-input');
  var previewCard = document.getElementById('preview-card');
  var submitStep = document.getElementById('submit-step');
  var resultCard = document.getElementById('result-card');
  var submitBtn = document.getElementById('submit-btn');

  if (validateForm) {
    validateForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var file = fileInput.files[0];
      if (!file) { toast('Choose a .gpml file first.', 'error'); return; }
      var validateBtn = document.getElementById('validate-btn');
      var originalLabel = validateBtn.textContent;
      validateBtn.disabled = true;
      validateBtn.textContent = 'Validating…';
      var fd = new FormData();
      fd.append('file', file);
      fetch('/api/validate', { method: 'POST', body: fd })
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, body: j }; }); })
        .then(function (res) {
          validateBtn.disabled = false;
          validateBtn.textContent = originalLabel;
          if (!res.ok) { toast(describeError(res.status, res.body), 'error'); return; }
          document.getElementById('preview-name').textContent = res.body.name || 'not set';
          document.getElementById('preview-organism').textContent = res.body.organism || 'not set';
          document.getElementById('preview-wpid').textContent = res.body.embedded_wpid || 'None (a new WPID will be assigned)';
          document.getElementById('preview-path').textContent = res.body.will_layout_to || 'not set';
          previewCard.hidden = false;
          if (submitStep) submitStep.dataset.disabled = 'false';
          if (submitBtn) submitBtn.disabled = false;
          toast('Looks good. Review the preview, then submit.', 'success');
        })
        .catch(function () {
          validateBtn.disabled = false;
          validateBtn.textContent = originalLabel;
          toast('Could not reach the server. Try again.', 'error');
        });
    });
  }

  if (submitForm) {
    submitForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var file = fileInput.files[0];
      if (!file) { toast('Choose a .gpml file first.', 'error'); return; }
      submitBtn.disabled = true;
      submitBtn.textContent = 'Submitting…';
      var fd = new FormData();
      fd.append('file', file);
      fetch('/api/submit', { method: 'POST', body: fd })
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, body: j }; }); })
        .then(function (res) {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Submit new pathway';
          if (!res.ok) { toast(describeError(res.status, res.body), 'error'); return; }
          resultCard.innerHTML =
            'Assigned <strong>' + res.body.wpid + '</strong>. Opened pull request ' +
            '<a href="' + res.body.pr_url + '" target="_blank" rel="noopener">#' + res.body.pr_number + '</a> ' +
            '(<code>' + res.body.path + '</code>). <a href="/dashboard">Go to the dashboard</a>.';
          resultCard.hidden = false;
          toast('Pathway submitted.', 'success');
        })
        .catch(function () {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Submit new pathway';
          toast('Could not reach the server. Try again.', 'error');
        });
    });
  }

  var updateBtn0 = document.getElementById('update-btn');
  if (updateBtn0) {
    updateBtn0.addEventListener('click', function (e) {
      e.preventDefault();
      var wpid = (document.getElementById('update-wpid').value || '').trim().replace(/^WP/i, '');
      var file = document.getElementById('update-file').files[0];
      if (!/^\d+$/.test(wpid)) { toast('Enter a numeric WPID, like 554.', 'error'); return; }
      if (!file) { toast('Choose a .gpml file first.', 'error'); return; }
      var btn = document.getElementById('update-btn');
      btn.disabled = true;
      btn.textContent = 'Submitting…';
      var fd = new FormData();
      fd.append('file', file);
      fetch('/api/pathways/' + wpid + '/update', { method: 'POST', body: fd })
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, body: j }; }); })
        .then(function (res) {
          btn.disabled = false;
          btn.textContent = 'Submit update';
          if (!res.ok) { toast(describeError(res.status, res.body), 'error'); return; }
          var out = document.getElementById('update-result');
          out.innerHTML =
            'Updated <strong>' + res.body.wpid + '</strong>. Opened pull request ' +
            '<a href="' + res.body.pr_url + '" target="_blank" rel="noopener">#' + res.body.pr_number + '</a> ' +
            '(<code>' + res.body.path + '</code>). <a href="/dashboard">Go to the dashboard</a>.';
          out.hidden = false;
          toast('Update submitted.', 'success');
        })
        .catch(function () {
          btn.disabled = false;
          btn.textContent = 'Submit update';
          toast('Could not reach the server. Try again.', 'error');
        });
    });
  }

  // ---------- review cards (dashboard.html / review_detail.html) ----------
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
          var pill = item.querySelector('.state-pill');
          pill.className = 'state-pill state-pill--' + state;
          pill.setAttribute('data-state', state);
          pill.textContent = state;
          siblingChips.forEach(function (b) {
            b.setAttribute('aria-pressed', b.getAttribute('data-state') === state ? 'true' : 'false');
          });
          recomputeApprove(card);
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
      approveBtn.textContent = 'Merging…';
      fetch('/api/reviews/' + pr2 + '/approve', { method: 'POST' })
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, body: j }; }); })
        .then(function (res) {
          if (!res.ok) {
            approveBtn.disabled = false;
            approveBtn.textContent = originalLabel;
            toast(describeError(res.status, res.body), 'error');
            return;
          }
          toast('Approved and merged.', 'success');
          setTimeout(function () { location.reload(); }, 700);
        })
        .catch(function () {
          approveBtn.disabled = false;
          approveBtn.textContent = originalLabel;
          toast('Could not reach the server. Try again.', 'error');
        });
    }
  });

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
        toast(select.value ? 'Assigned to @' + select.value + '.' : 'Unassigned.', 'success');
      })
      .catch(function () { select.disabled = false; toast('Could not reach the server. Try again.', 'error'); });
  });
})();
