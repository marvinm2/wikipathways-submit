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

  // ---------- tabs (index.html, logged in) ----------
  var tabs = document.querySelectorAll('.tab');
  tabs.forEach(function (t) {
    t.addEventListener('click', function () {
      var name = t.getAttribute('data-tab');
      tabs.forEach(function (x) {
        var on = x === t;
        x.classList.toggle('tab--active', on);
        x.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      document.querySelectorAll('.tab-panel').forEach(function (p) {
        p.classList.toggle('tab-panel--hidden', p.getAttribute('data-panel') !== name);
      });
    });
  });

  // ---------- new-pathway submit (single action — validate + open PR together) ----------
  var submitForm = document.getElementById('submit-form');
  if (submitForm) {
    submitForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var file = document.getElementById('new-file').files[0];
      var submitBtn = document.getElementById('submit-btn');
      var resultCard = document.getElementById('result-card');
      if (!file) { toast('Choose a .gpml file first.', 'error'); return; }
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

  // ---------- WPID field: normalise to WP#### and verify the pathway exists ----------
  var wpidInput = document.getElementById('update-wpid');
  var wpidStatus = document.getElementById('update-wpid-status');
  function wpidNumber(v) { return (v || '').replace(/\D/g, ''); }
  if (wpidInput) {
    wpidInput.addEventListener('blur', function () {
      var num = wpidNumber(wpidInput.value);
      if (!num) { wpidInput.value = ''; if (wpidStatus) wpidStatus.hidden = true; return; }
      wpidInput.value = 'WP' + num;  // always display WP####
      if (!wpidStatus) return;
      wpidStatus.hidden = false;
      wpidStatus.className = 'wpid-status wpid-status--checking';
      wpidStatus.textContent = 'Checking WP' + num + '…';
      fetch('/api/pathways/' + num)
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (info) {
          if (!info) { wpidStatus.hidden = true; return; }
          if (info.exists) {
            wpidStatus.className = 'wpid-status wpid-status--ok';
            wpidStatus.textContent = info.wpid + (info.name ? ' — ' + info.name : '') + ' found.';
          } else {
            wpidStatus.className = 'wpid-status wpid-status--err';
            wpidStatus.textContent = info.wpid + ' does not exist on main. Use "New pathway" for a new one.';
          }
        })
        .catch(function () { wpidStatus.hidden = true; });
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
      // Compose the "what changed" note from the ticked options + the free-text details.
      var updateDesc = document.getElementById('update-description');
      var detail = updateDesc ? (updateDesc.value || '').trim() : '';
      var otherBox = document.getElementById('update-change-other');
      if (otherBox && otherBox.checked && !detail) {
        toast('You ticked "Other" — please describe the change in Details.', 'error'); return;
      }
      var changes = [];
      document.querySelectorAll('.update-change:checked').forEach(function (cb) {
        changes.push(cb.getAttribute('data-label'));
      });
      var parts = [];
      if (changes.length) { parts.push('Changed: ' + changes.join('; ') + '.'); }
      if (detail) { parts.push(detail); }
      var description = parts.join('\n');
      var btn = document.getElementById('update-btn');
      btn.disabled = true;
      btn.textContent = 'Submitting…';
      var fd = new FormData();
      fd.append('file', file);
      fd.append('description', description);
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
        pill.textContent = srv.state;
      }
      item.querySelectorAll('.chip-btn').forEach(function (b) {
        b.setAttribute('aria-pressed', b.getAttribute('data-state') === srv.state ? 'true' : 'false');
      });
    });
    recomputeApprove(card);
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
        toast(select.value ? 'Assigned to @' + select.value + '.' : 'Unassigned.', 'success');
      })
      .catch(function () { select.disabled = false; toast('Could not reach the server. Try again.', 'error'); });
  });
})();
