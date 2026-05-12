/*
 * decklist-stream.js — progressive enhancement for /decklist.
 *
 * Two responsibilities:
 *
 *   1. On any page that exposes a #decklist-form (the Search page),
 *      hijack the form submit, POST to /decklist/jobs, and navigate to
 *      /decklist/jobs/<id> on success. If the browser doesn't speak
 *      EventSource or the fetch fails, we fall back to the native form
 *      action (POST /decklist) so the synchronous path still works.
 *
 *   2. On the skeleton page (decklist.html with `streaming_job_id`
 *      present), open an EventSource on the /stream endpoint and
 *      progressively fill the table, shop-totals strip, dl-meta line,
 *      and shop-timeout banner as `row`, `totals`, and `shop_timeout`
 *      events arrive. The connection always has bytes flowing — either
 *      events or 15s server-side keepalive comments — so Cloudflare
 *      doesn't 524.
 *
 *      A status block at the top (#dl-stream-status) gives the user a
 *      live "Connecting → Searching N/M cards → Complete (or Error)"
 *      affordance with a progress bar — useful because between the
 *      `meta` and the first `row` the page can sit empty for several
 *      seconds while shops are slow.
 */
(function () {
  'use strict';

  // ─── 1. Submit hijack on the search page ─────────────────────────
  const submitForm = document.getElementById('decklist-form');
  if (submitForm && window.EventSource && window.fetch) {
    submitForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      // Record the submit time so the skeleton page can compute true
      // end-to-end wall-clock (form-submit → "Search complete") instead
      // of just the server-side fan-out duration. sessionStorage keyed
      // by job_id lets the next page find this without URL pollution.
      const submitEpochMs = Date.now();
      const fd = new FormData(submitForm);
      let resp;
      try {
        resp = await fetch('/decklist/jobs', { method: 'POST', body: fd });
      } catch {
        submitForm.submit();
        return;
      }
      if (resp.status === 202) {
        const { job_id } = await resp.json();
        try {
          sessionStorage.setItem(`dl-start-${job_id}`, String(submitEpochMs));
        } catch { /* private mode or quota; the skeleton has a fallback */ }
        window.location.href = `/decklist/jobs/${encodeURIComponent(job_id)}`;
      } else if (resp.status === 400) {
        // Validation error — fall back to the synchronous endpoint
        // which renders the error page with the same message.
        submitForm.submit();
      } else if (resp.status === 429) {
        const body = await resp.json();
        alert(body.error || 'Too many in-flight searches; wait for one to finish.');
      } else {
        submitForm.submit();
      }
    });
  }

  // ─── 2. Stream consumer on the skeleton page ─────────────────────
  const root = document.getElementById('dl-stream-root');
  if (!root || !window.EventSource) return;

  const jobId = root.dataset.jobId;
  const useInventory = root.dataset.useInventory === '1';

  // Status block (top affordance)
  const statusBox      = document.getElementById('dl-stream-status');
  const statusLabel    = document.getElementById('dl-stream-status-label');
  const statusDetail   = document.getElementById('dl-stream-status-detail');
  const progressFill   = document.getElementById('dl-stream-progress-fill');

  // Result-area containers
  const tbody       = document.getElementById('dl-stream-tbody');
  const shopsBox    = document.getElementById('dl-stream-shops');
  const metaBox     = document.getElementById('dl-stream-meta');
  const timeoutBox  = document.getElementById('dl-stream-timeout-warning');

  // Wall-clock origin for the "front-to-back" elapsed display. Prefer
  // the form-submit time recorded by the submit hijack above (which
  // covers the POST + nav + skeleton render); fall back to
  // performance.timeOrigin (page-load) if sessionStorage is empty.
  let frontToBackOriginMs;
  try {
    const saved = sessionStorage.getItem(`dl-start-${jobId}`);
    frontToBackOriginMs = saved ? parseInt(saved, 10) : Date.now();
  } catch {
    frontToBackOriginMs = Date.now();
  }

  // Live state for the dl-meta line + progress.
  const state = {
    distinct: 0,            // total rows we expect to render
    namesToSearch: 0,       // shop-searched cards (the "work" the bar tracks)
    rowsReceived: 0,        // every row event (inventory + shop)
    shopRowsReceived: 0,    // only shop-searched rows; drives the progress bar
    sourcedCount: 0,        // inventory-covered OR has-best (for meta line)
    invCovered: 0,
    skippedBasics: 0,
    fx: null,
    grandTotalUsd: 0,
    grandTotalJpy: 0,
    grandTotalUsdWithShipping: 0,
    grandTotalJpyWithShipping: 0,
    shippingTotalJpy: 0,
    timedOutShops: new Set(),
  };

  const yenFmt = (n) => '¥' + Math.round(n).toLocaleString('en-US');
  const usdFmt = (n) => '$' + n.toFixed(2);

  // Cache the original page title once so the tab indicator can
  // toggle between "Searching… | …" and "✓ Done | …" without losing
  // the suffix.
  const titleSuffix = ' · ' + (document.title || 'mtgcompare');

  function setStatus(kind, label, detail = '') {
    statusBox.classList.remove('complete', 'error');
    if (kind === 'complete' || kind === 'error') statusBox.classList.add(kind);
    statusLabel.textContent = label;
    statusDetail.textContent = detail;
    // Mirror the state in the tab title so an inactive tab still
    // tells the user whether the search is running or finished.
    const prefix =
      kind === 'complete' ? '✓ Done'
      : kind === 'error'    ? '⚠ Failed'
      : kind === 'searching' && state.distinct
          ? `Searching ${state.rowsReceived}/${state.distinct}`
      : 'Searching…';
    document.title = prefix + titleSuffix;
  }

  function updateProgress() {
    // Bar tracks shop-searches-done / shop-searches-total. The
    // up-front inventory burst (could be 63 of 81 cards arriving in
    // one second) shouldn't make the bar jump to 78% — that was
    // misleading. Now the bar reflects the actual long-running work
    // (the shop fan-out) and the meta line shows the total card
    // count separately.
    if (state.namesToSearch === 0) {
      // Either all-inventory or before meta arrives. Either way, no
      // shop work to do; bar stays at 0 until done flips it to 100.
      progressFill.style.width = '0%';
    } else {
      const pct = Math.min(100, Math.round(100 * state.shopRowsReceived / state.namesToSearch));
      progressFill.style.width = pct + '%';
    }
    if (!statusBox.classList.contains('complete') && !statusBox.classList.contains('error')) {
      if (state.namesToSearch === 0 && state.distinct > 0) {
        setStatus('searching',
                  'All cards covered by inventory',
                  `${state.distinct} cards`);
      } else if (state.namesToSearch > 0) {
        setStatus('searching',
                  `Searching shops · ${state.shopRowsReceived} of ${state.namesToSearch}`,
                  state.distinct
                    ? `${state.rowsReceived} of ${state.distinct} cards rendered`
                    : '');
      }
    }
  }

  function renderMetaLine() {
    const moneyPart = (state.grandTotalUsd > 0 || state.grandTotalUsdWithShipping > 0)
      ? (state.shippingTotalJpy > 0
          ? `<strong>${usdFmt(state.grandTotalUsdWithShipping)}</strong>` +
            `&nbsp;<strong>${yenFmt(state.grandTotalJpyWithShipping)}</strong> to buy ` +
            `<span class="dl-meta-note">(${yenFmt(state.grandTotalJpy)} cards ` +
            `+ ${yenFmt(state.shippingTotalJpy)} shipping)</span> &middot; `
          : `<strong>${usdFmt(state.grandTotalUsd)}</strong>` +
            `&nbsp;<strong>${yenFmt(state.grandTotalJpy)}</strong> to buy &middot; `)
      : '';
    const sourced = `${state.sourcedCount} of ${state.distinct} card${state.distinct === 1 ? '' : 's'} sourced`;
    const inv = (useInventory && state.invCovered)
      ? ` &middot; <span style="color:#4db87a">${state.invCovered} in inventory</span>`
      : '';
    const basics = state.skippedBasics
      ? ` &middot; <span class="dl-meta-note">${state.skippedBasics} basic land${state.skippedBasics === 1 ? '' : 's'} excluded</span>`
      : '';
    const fx = state.fx ? ` &middot; FX ¥${state.fx.toFixed(2)} per $1` : '';
    metaBox.innerHTML = moneyPart + sourced + inv + basics + fx;
  }

  function renderShops(shopList) {
    if (!shopList || !shopList.length) {
      shopsBox.innerHTML = '';
      return;
    }
    const flagsByShop = JSON.parse(root.dataset.shopFlags || '{}');
    const html = shopList.map((s) => {
      const flag = flagsByShop[s.shop] || '';
      const shipLine = s.shipping_jpy > 0
        ? `<span class="ship-line">+ ${yenFmt(s.shipping_jpy)} shipping</span>`
        : '';
      const cardWord = s.unique_cards === 1 ? 'card' : 'cards';
      const copyWord = s.total_copies === 1 ? 'copy' : 'copies';
      return `
        <div class="dl-shop-card">
          <div class="shop-name">${flag} ${s.shop}</div>
          <div class="shop-total">${yenFmt(s.total_jpy_with_shipping)}</div>
          <div class="shop-breakdown">
            <span>${yenFmt(s.total_jpy)} cards</span>
            ${shipLine}
            <span>${s.unique_cards} ${cardWord} (${s.total_copies} ${copyWord})</span>
          </div>
        </div>
      `;
    }).join('');
    shopsBox.innerHTML = html;
  }

  function renderTimeoutBanner() {
    if (!state.timedOutShops.size) {
      timeoutBox.innerHTML = '';
      timeoutBox.className = '';
      return;
    }
    const flagsByShop = JSON.parse(root.dataset.shopFlags || '{}');
    const shops = [...state.timedOutShops].sort();
    const list = shops
      .map((s) => `${flagsByShop[s] || ''} ${s}`)
      .join(', ');
    const plural = shops.length === 1 ? '' : 's';
    timeoutBox.innerHTML =
      `<strong>Partial results:</strong> ${shops.length} shop${plural} ` +
      `timed out on at least one card — ${list}. Re-run to retry.`;
    timeoutBox.className = 'dl-timeout-warning';
  }

  // Open the stream. The status block stays in "Connecting…" until the
  // first event lands; if the connection takes >2 s the user still sees
  // an animated spinner from the start, so they know something's happening.
  const es = new EventSource(`/decklist/jobs/${encodeURIComponent(jobId)}/stream`);

  es.addEventListener('meta', (evt) => {
    const d = JSON.parse(evt.data);
    state.distinct = d.distinct_names;
    state.namesToSearch = d.names_to_search;
    state.invCovered = d.inventory_hits;
    state.skippedBasics = d.skipped_basics;
    state.fx = d.fx;
    renderMetaLine();
    updateProgress();
  });

  es.addEventListener('row', (evt) => {
    const d = JSON.parse(evt.data);
    insertRowSorted(d.html, d.key);
    state.rowsReceived += 1;
    // Server emits inventory-covered rows (qty_needed=0) up-front, then
    // shop-searched rows from the fan-out as they complete. Track them
    // separately so the progress bar reflects shop work, not the
    // inventory burst.
    if (d.qty_needed === 0) {
      state.sourcedCount += 1;
    } else {
      state.shopRowsReceived += 1;
      if (d.has_best) state.sourcedCount += 1;
    }
    renderMetaLine();
    updateProgress();
  });

  // Server emits inventory rows up-front in alphabetical order, then
  // streams non-inventory rows in completion order. To match the
  // synchronous /decklist's "all rows sorted by canonical name" layout
  // we have to insert each arriving row at its sorted slot instead of
  // just appending. `key` is the lowercase canonical name; a
  // data-sort-key attribute on the inserted <tr> lets us find the
  // right neighbour in O(N) without re-sorting the whole tbody.
  function insertRowSorted(html, key) {
    const tmp = document.createElement('tbody');
    tmp.innerHTML = html.trim();
    const tr = tmp.querySelector('tr');
    if (!tr) return;
    tr.dataset.sortKey = key;
    const existing = tbody.children;
    for (let i = 0; i < existing.length; i++) {
      const k = existing[i].dataset.sortKey;
      if (k && k > key) {
        tbody.insertBefore(tr, existing[i]);
        return;
      }
    }
    tbody.appendChild(tr);
  }

  es.addEventListener('shop_timeout', (evt) => {
    const d = JSON.parse(evt.data);
    state.timedOutShops.add(d.shop);
    renderTimeoutBanner();
  });

  es.addEventListener('totals', (evt) => {
    const d = JSON.parse(evt.data);
    state.grandTotalUsd = d.grand_total_usd;
    state.grandTotalJpy = d.grand_total_jpy;
    state.grandTotalUsdWithShipping = d.grand_total_usd_with_shipping;
    state.grandTotalJpyWithShipping = d.grand_total_jpy_with_shipping;
    state.shippingTotalJpy = d.shipping_total_jpy;
    renderShops(d.shop_list);
    renderMetaLine();
  });

  es.addEventListener('done', (evt) => {
    const d = evt.data ? JSON.parse(evt.data) : {};
    // "Front to back" wall-clock from form submit to done — this is
    // what the user perceives. The server's d.duration_ms only covers
    // the fan-out itself (no POST overhead, no page navigation), so
    // it's typically 100-500ms shorter than the wall-clock the user
    // saw. Show the front-to-back figure prominently and the
    // server-side number as a parenthetical for debugging.
    const elapsedSec = ((Date.now() - frontToBackOriginMs) / 1000).toFixed(1);
    const serverSec  = d.duration_ms != null ? (d.duration_ms / 1000).toFixed(1) : null;
    const timedOut   = (d.timed_out_shops || []).length;

    const parts = [`${state.distinct} card${state.distinct === 1 ? '' : 's'}`];
    if (state.invCovered) parts.push(`${state.invCovered} from inventory`);
    if (state.shopRowsReceived) parts.push(`${state.shopRowsReceived} shop search${state.shopRowsReceived === 1 ? '' : 'es'}`);
    if (timedOut) parts.push(`${timedOut} shop${timedOut === 1 ? '' : 's'} timed out`);
    const detail = parts.join(' · ');

    progressFill.style.width = '100%';
    const labelTime = serverSec && serverSec !== elapsedSec
      ? `Done in ${elapsedSec}s  (search ${serverSec}s)`
      : `Done in ${elapsedSec}s`;
    setStatus('complete', labelTime, detail);

    // Clear the sessionStorage marker so a future fresh page load
    // doesn't anchor the "front-to-back" timer to a long-stale value.
    try { sessionStorage.removeItem(`dl-start-${jobId}`); } catch { /* ignore */ }
    es.close();
  });

  es.addEventListener('error', (evt) => {
    // EventSource fires "error" both on transport drops AND on the
    // server's explicit "error" event. Distinguish: a server-emitted
    // event has .data; a transport drop doesn't.
    if (evt.data) {
      try {
        const d = JSON.parse(evt.data);
        setStatus('error', 'Search failed', d.message || '');
      } catch {
        setStatus('error', 'Search failed', 'Malformed server event.');
      }
    } else {
      setStatus('error', 'Connection lost',
                'The streaming connection dropped. Refresh to retry.');
    }
    es.close();
  });
})();
