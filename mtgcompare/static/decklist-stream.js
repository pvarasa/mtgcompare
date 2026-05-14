/*
 * decklist-stream.js — progressive enhancement for /decklist.
 *
 * Hijacks the decklist form submit, POSTs to /decklist/stream, and
 * consumes the SSE response (text/event-stream) directly out of the
 * fetch response body. Renders rows / shop totals / progress in-place
 * below the form as `meta`, `row`, `shop_timeout`, `totals`, and `done`
 * events arrive.
 *
 * Why fetch+ReadableStream instead of EventSource: EventSource only
 * supports GET with no body, which forced the previous two-request
 * design (POST creates job → GET stream by job_id). That coupled the
 * two requests to one pod, blocking horizontal scale-out. With a single
 * streaming POST the search is always served end-to-end by the worker
 * that received it, so we can add gunicorn workers / pod replicas
 * without sticky-session ingress config.
 *
 * Fallback: if fetch / ReadableStream are missing or the POST fails to
 * connect, we resubmit the form natively to /decklist (the synchronous
 * full-page-render endpoint). Same UX as before JS-stream support.
 */
(function () {
  'use strict';

  const submitForm = document.getElementById('decklist-form');
  if (!submitForm || !window.fetch || !window.ReadableStream) return;

  // Page-level mount point. Both index.html and decklist.html render an
  // empty <div id="dl-stream-mount"></div> for us to fill on submit.
  function getMount() {
    let mount = document.getElementById('dl-stream-mount');
    if (!mount) {
      // Defensive: insert one right after the form so older cached
      // templates still work. Shouldn't fire in practice.
      mount = document.createElement('div');
      mount.id = 'dl-stream-mount';
      submitForm.parentNode.insertBefore(mount, submitForm.nextSibling);
    }
    return mount;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function buildSkeleton(useInventory, shopFlagsJson) {
    const invHeader = useInventory
      ? '<th class="num">Have</th><th class="num">Need</th>'
      : '';
    return `
      <div id="dl-stream-root"
           data-use-inventory="${useInventory ? '1' : '0'}"
           data-shop-flags='${escapeHtml(shopFlagsJson)}'></div>
      <div id="dl-stream-status" class="dl-stream-status"
           role="status" aria-live="polite">
        <span class="dl-stream-status-icon" aria-hidden="true">
          <span class="spinner"></span>
        </span>
        <span id="dl-stream-status-label"
              class="dl-stream-status-label">Connecting…</span>
        <span class="dl-stream-progress" aria-hidden="true"><span
              id="dl-stream-progress-fill"
              class="dl-stream-progress-fill"></span></span>
        <span id="dl-stream-status-detail"
              class="dl-stream-status-detail"></span>
      </div>
      <div id="dl-stream-timeout-warning"></div>
      <p id="dl-stream-meta" class="dl-meta"></p>
      <div id="dl-stream-shops" class="dl-shops"></div>
      <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Card</th>
            <th class="num">Qty</th>
            ${invHeader}
            <th>Best shop</th>
            <th>Set</th>
            <th class="num">Unit ¥</th>
            <th class="num">Total ¥</th>
            <th class="num">USD</th>
            <th>Cond.</th>
            <th>Link</th>
          </tr>
        </thead>
        <tbody id="dl-stream-tbody"></tbody>
      </table>
      </div>
    `;
  }

  // ─── SSE frame parser for fetch streams ──────────────────────────────
  //
  // SSE frames are separated by blank lines. Each frame has zero or more
  // ``event: ...`` and ``data: ...`` lines, plus comment lines starting
  // with ``:`` we just skip. This parser is intentionally minimal — we
  // don't need ``id:``/``retry:`` fields since we never reconnect.
  async function consumeSSE(body, onEvent) {
    const reader = body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        if (!block || block.startsWith(':')) continue;
        let evt = 'message';
        let data = '';
        for (const line of block.split('\n')) {
          if (line.startsWith('event: ')) evt = line.slice(7);
          else if (line.startsWith('data: ')) data = line.slice(6);
        }
        onEvent(evt, data);
      }
    }
  }

  // The page-level inline ``wireLoadingIndicator`` in index.html also
  // listens for the form's submit event and shows the legacy "Pricing
  // decklist across shops…" spinner + disables the submit button. That
  // was the right UX when the form did a full-page POST; with the
  // streaming UI we own the affordance, so we hide the legacy spinner
  // on submit and re-enable the button when the stream terminates.
  function hideLegacySpinner() {
    const legacy = document.getElementById('search-loading-decklist');
    if (legacy) legacy.classList.remove('active');
  }
  function restoreSubmitButton() {
    const btn = submitForm.querySelector('button[type=submit]');
    if (btn && btn.dataset.label) {
      btn.disabled = false;
      btn.textContent = btn.dataset.label;
      delete btn.dataset.label;
    }
  }

  submitForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    hideLegacySpinner();
    const submitEpochMs = Date.now();
    const useInventory =
      submitForm.querySelector('input[name="use_inventory"]:checked') != null;
    const shopFlagsAttr = submitForm.dataset.shopFlags || '{}';

    const mount = getMount();
    mount.innerHTML = buildSkeleton(useInventory, shopFlagsAttr);
    setupHandlers(mount, useInventory, submitEpochMs, restoreSubmitButton);

    const fd = new FormData(submitForm);
    let resp;
    try {
      resp = await fetch('/decklist/stream', { method: 'POST', body: fd });
    } catch {
      // Network drop before we got headers. Wipe the in-progress UI and
      // fall back to the synchronous endpoint so the user still sees
      // *some* result.
      mount.innerHTML = '';
      submitForm.submit();
      return;
    }

    if (resp.status === 400) {
      // Validation error — synchronous endpoint renders the error page
      // with the same message, so just hand off to it.
      mount.innerHTML = '';
      submitForm.submit();
      return;
    }
    if (resp.status === 429) {
      const body = await resp.json().catch(() => ({}));
      mount.innerHTML = '';
      restoreSubmitButton();
      alert(body.error || 'Too many in-flight searches; wait for one to finish.');
      return;
    }
    if (!resp.ok || !resp.body) {
      mount.innerHTML = '';
      submitForm.submit();
      return;
    }

    try {
      await consumeSSE(resp.body, (evt, data) => {
        const handler = window.__dlStreamHandlers && window.__dlStreamHandlers[evt];
        if (handler) handler(data);
      });
    } catch {
      // Stream interrupted mid-flight. The status block driver below
      // already shows an error if the server sent one; if the connection
      // just dropped, fall through to the generic message.
      const fn = window.__dlStreamHandlers && window.__dlStreamHandlers.__transportDrop;
      if (fn) fn();
    }
  });

  // ─── Skeleton-fill handlers ──────────────────────────────────────────
  //
  // Installed each time we render a fresh skeleton. We keep them on
  // window.__dlStreamHandlers so the SSE consumer above can dispatch by
  // event name without forming a closure on stale element refs from a
  // previous search.
  function setupHandlers(mount, useInventory, frontToBackOriginMs, onTerminal) {
    const root         = mount.querySelector('#dl-stream-root');
    const statusBox    = mount.querySelector('#dl-stream-status');
    const statusLabel  = mount.querySelector('#dl-stream-status-label');
    const statusDetail = mount.querySelector('#dl-stream-status-detail');
    const progressFill = mount.querySelector('#dl-stream-progress-fill');
    const tbody        = mount.querySelector('#dl-stream-tbody');
    const shopsBox     = mount.querySelector('#dl-stream-shops');
    const metaBox      = mount.querySelector('#dl-stream-meta');
    const timeoutBox   = mount.querySelector('#dl-stream-timeout-warning');

    let flagsByShop = {};
    try { flagsByShop = JSON.parse(root.dataset.shopFlags || '{}'); } catch { /* defaults to {} */ }

    const state = {
      distinct: 0,
      namesToSearch: 0,
      rowsReceived: 0,
      shopRowsReceived: 0,
      sourcedCount: 0,
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

    const titleSuffix = ' · ' + (document.title.replace(/^(✓ Done|⚠ Failed|Searching[^|]*)\s·\s/, '') || 'mtgcompare');

    function setStatus(kind, label, detail = '') {
      statusBox.classList.remove('complete', 'error');
      if (kind === 'complete' || kind === 'error') statusBox.classList.add(kind);
      statusLabel.textContent = label;
      statusDetail.textContent = detail;
      const prefix =
        kind === 'complete' ? '✓ Done'
        : kind === 'error'    ? '⚠ Failed'
        : kind === 'searching' && state.distinct
            ? `Searching ${state.rowsReceived}/${state.distinct}`
        : 'Searching…';
      document.title = prefix + titleSuffix;
    }

    function updateProgress() {
      if (state.namesToSearch === 0) {
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

    window.__dlStreamHandlers = {
      meta(raw) {
        const d = JSON.parse(raw);
        state.distinct = d.distinct_names;
        state.namesToSearch = d.names_to_search;
        state.invCovered = d.inventory_hits;
        state.skippedBasics = d.skipped_basics;
        state.fx = d.fx;
        renderMetaLine();
        updateProgress();
      },
      row(raw) {
        const d = JSON.parse(raw);
        insertRowSorted(d.html, d.key);
        state.rowsReceived += 1;
        // Server emits inventory-covered rows (qty_needed=0) up-front,
        // then shop-searched rows from the fan-out as they complete.
        // Track them separately so the progress bar reflects shop work,
        // not the inventory burst.
        if (d.qty_needed === 0) {
          state.sourcedCount += 1;
        } else {
          state.shopRowsReceived += 1;
          if (d.has_best) state.sourcedCount += 1;
        }
        renderMetaLine();
        updateProgress();
      },
      shop_timeout(raw) {
        const d = JSON.parse(raw);
        state.timedOutShops.add(d.shop);
        renderTimeoutBanner();
      },
      totals(raw) {
        const d = JSON.parse(raw);
        state.grandTotalUsd = d.grand_total_usd;
        state.grandTotalJpy = d.grand_total_jpy;
        state.grandTotalUsdWithShipping = d.grand_total_usd_with_shipping;
        state.grandTotalJpyWithShipping = d.grand_total_jpy_with_shipping;
        state.shippingTotalJpy = d.shipping_total_jpy;
        renderShops(d.shop_list);
        renderMetaLine();
      },
      done(raw) {
        const d = raw ? JSON.parse(raw) : {};
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
        if (onTerminal) onTerminal();
      },
      error(raw) {
        let msg = '';
        try { msg = (JSON.parse(raw || '{}')).message || ''; } catch { /* ignore */ }
        setStatus('error', 'Search failed', msg);
        if (onTerminal) onTerminal();
      },
      __transportDrop() {
        if (!statusBox.classList.contains('complete') && !statusBox.classList.contains('error')) {
          setStatus('error', 'Connection lost',
                    'The streaming connection dropped. Refresh to retry.');
        }
        if (onTerminal) onTerminal();
      },
    };
  }
})();
