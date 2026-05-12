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
 */
(function () {
  'use strict';

  // ─── 1. Submit hijack on the search page ─────────────────────────
  const submitForm = document.getElementById('decklist-form');
  if (submitForm && window.EventSource && window.fetch) {
    submitForm.addEventListener('submit', async (e) => {
      e.preventDefault();
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
  const tbody = document.getElementById('dl-stream-tbody');
  const shopsBox = document.getElementById('dl-stream-shops');
  const metaBox = document.getElementById('dl-stream-meta');
  const timeoutBox = document.getElementById('dl-stream-timeout-warning');
  const spinner = document.getElementById('dl-stream-spinner');

  // Latest meta values held for live recomputation of the dl-meta line
  // on each row/totals/shop_timeout tick.
  const state = {
    distinct: 0,
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

  const yenFmt = (n) =>
    '¥' + Math.round(n).toLocaleString('en-US');
  const usdFmt = (n) => '$' + n.toFixed(2);

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
    timeoutBox.classList.add('dl-timeout-warning');
  }

  const es = new EventSource(`/decklist/jobs/${encodeURIComponent(jobId)}/stream`);

  es.addEventListener('meta', (evt) => {
    const d = JSON.parse(evt.data);
    state.distinct = d.distinct_names;
    state.invCovered = d.inventory_hits;
    state.skippedBasics = d.skipped_basics;
    state.fx = d.fx;
    renderMetaLine();
  });

  es.addEventListener('row', (evt) => {
    const d = JSON.parse(evt.data);
    // Server pre-rendered the <tr> via the same Jinja partial the
    // synchronous handler uses — just append the HTML.
    tbody.insertAdjacentHTML('beforeend', d.html);
    // qty_needed === 0 cards count as "sourced" via inventory.
    // has_best cards count as "sourced" from a shop.
    if (d.qty_needed === 0 || d.has_best) state.sourcedCount += 1;
    renderMetaLine();
  });

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

  es.addEventListener('done', () => {
    if (spinner) spinner.style.display = 'none';
    es.close();
  });

  es.addEventListener('error', (evt) => {
    // EventSource fires "error" both on transport drops AND on the
    // server's explicit "error" event. Distinguish: a server-emitted
    // event has .data; a transport drop doesn't.
    if (evt.data) {
      try {
        const d = JSON.parse(evt.data);
        if (metaBox) metaBox.innerHTML = `<span class="error">${d.message || 'Search failed.'}</span>`;
      } catch { /* ignore */ }
    }
    if (spinner) spinner.style.display = 'none';
    es.close();
  });
})();
