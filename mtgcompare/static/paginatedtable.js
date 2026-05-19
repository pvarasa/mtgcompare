/* Shared client for server-paginated tables (/inventory, /market).
 *
 * Each page has the same shape:
 *   - a <form> with text/number/select inputs (q, price_mode, price_value)
 *     plus hidden inputs (sort, dir, page, per_page)
 *   - a "wrapper" <div> containing the table + pagination
 *   - optionally a "summary" element (e.g. #inv-stats, #mkt-summary)
 *     above the wrapper that shows aggregates over the filtered set
 *
 * The server's ?partial=tbody endpoint returns JSON
 *   { "table_html": ..., "summary_html": ... }
 * so a filter/sort/page change swaps both the table AND the aggregates
 * row in one round-trip — without this the summary line shows stale
 * totals (e.g. "cost basis $X" reflecting the whole inventory while
 * the table is filtered to one card). On change we swap, mirror the
 * URL via history.pushState/replaceState, and give the page a hook
 * (`onAfterSwap`) to re-bind anything inside the wrapper (e.g. row
 * checkboxes, chart triggers).
 *
 * Conventions inside the wrapper (rendered by _pagination.html and the
 * per-page partial):
 *   - sort links:        th.sortable a.sort-link   (data-sort, data-dir)
 *   - pager links:       a.pg-btn                  (data-page)
 *   - page input:        input.page-input
 *   - per-page select:   select.per-page-select
 */
(function (global) {
  'use strict';

  function attachPaginatedTable(config) {
    const form    = config.form;
    const wrapper = config.wrapper;
    if (!form || !wrapper) return null;

    const defaults    = config.defaults    || {};
    const onAfterSwap = config.onAfterSwap || function () {};
    const debounceMs  = config.debounceMs  != null ? config.debounceMs : 250;
    const summaryEl   = config.summaryEl   || null;

    const fields = {
      q:           form.querySelector('input[name="q"]'),
      priceMode:   form.querySelector('select[name="price_mode"]'),
      priceValue:  form.querySelector('input[name="price_value"]'),
      setCode:     form.querySelector('select[name="set_code"]'),
      condition:   form.querySelector('select[name="condition"]'),
      sort:        form.querySelector('input[name="sort"]'),
      dir:         form.querySelector('input[name="dir"]'),
      page:        form.querySelector('input[name="page"]'),
      perPage:     form.querySelector('input[name="per_page"]'),
    };

    // Hide the submit button — the page works without JS via plain GET,
    // but with JS we drive everything through fetchFragment.
    const submitContainer = form.querySelector('.filter-actions');
    if (submitContainer) submitContainer.style.display = 'none';

    let inflight = null;
    let debounce = 0;

    function syncPriceValueState() {
      if (!fields.priceMode || !fields.priceValue) return;
      const needsValue = ['lte', 'gte', 'eq'].includes(fields.priceMode.value);
      fields.priceValue.disabled = !needsValue;
      if (!needsValue) fields.priceValue.value = '';
    }

    function buildQuery(overrides) {
      overrides = overrides || {};
      const params = new URLSearchParams();
      const merged = {
        q:           fields.q          ? fields.q.value.trim()          : '',
        price_mode:  fields.priceMode  ? fields.priceMode.value         : '',
        price_value: fields.priceValue ? fields.priceValue.value.trim() : '',
        set_code:    fields.setCode    ? fields.setCode.value           : '',
        condition:   fields.condition  ? fields.condition.value         : '',
        sort:        fields.sort       ? fields.sort.value              : '',
        dir:         fields.dir        ? fields.dir.value               : '',
        page:        fields.page       ? fields.page.value              : '',
        per_page:    fields.perPage    ? fields.perPage.value           : '',
      };
      Object.assign(merged, overrides);
      for (const [k, v] of Object.entries(merged)) {
        if (v !== '' && v !== null && v !== undefined) params.set(k, v);
      }
      return params.toString();
    }

    async function fetchFragment(overrides, opts) {
      overrides = overrides || {};
      opts      = opts      || {};

      // Push overrides into the form BEFORE building the URL so the
      // resulting query string matches what a plain-GET form submit would
      // produce.
      if (overrides.sort     !== undefined && fields.sort)    fields.sort.value    = overrides.sort;
      if (overrides.dir      !== undefined && fields.dir)     fields.dir.value     = overrides.dir;
      if (overrides.page     !== undefined && fields.page)    fields.page.value    = overrides.page;
      if (overrides.per_page !== undefined && fields.perPage) fields.perPage.value = overrides.per_page;

      const qs = buildQuery();
      const fullPath = `${form.action}?${qs}`;

      if (inflight) inflight.abort();
      inflight = new AbortController();

      wrapper.classList.add('is-loading');
      try {
        const res = await fetch(`${fullPath}&partial=tbody`, { signal: inflight.signal });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const payload = await res.json();
        // The loading spinner sits inside the wrapper and is wiped by the
        // swap. The CSS targets `#wrapper.is-loading .loading-indicator`,
        // so we re-prepend a spinner element after the swap.
        const spinnerClass = config.spinnerClass || 'pt-loading';
        wrapper.innerHTML = `<span class="${spinnerClass}">Loading…</span>` + (payload.table_html || '');
        if (summaryEl && payload.summary_html != null) {
          summaryEl.innerHTML = payload.summary_html;
        }
        attachTableHandlers();
      } catch (err) {
        if (err.name !== 'AbortError') console.error('paginatedtable fetch failed', err);
      } finally {
        wrapper.classList.remove('is-loading');
        inflight = null;
      }

      const navMode = opts.history || 'replace';
      if (navMode === 'push') {
        history.pushState({ pt: true, qs }, '', fullPath);
      } else {
        history.replaceState({ pt: true, qs }, '', fullPath);
      }
    }

    function attachTableHandlers() {
      wrapper.querySelectorAll('th.sortable a.sort-link').forEach((a) => {
        a.addEventListener('click', (event) => {
          event.preventDefault();
          fetchFragment(
            { sort: a.dataset.sort, dir: a.dataset.dir, page: 1 },
            { history: 'push' },
          );
        });
      });

      wrapper.querySelectorAll('a.pg-btn').forEach((a) => {
        a.addEventListener('click', (event) => {
          event.preventDefault();
          if (a.classList.contains('disabled')) return;
          fetchFragment({ page: a.dataset.page }, { history: 'push' });
        });
      });

      const pageInput = wrapper.querySelector('.page-input');
      if (pageInput) {
        const commit = () => {
          const n = Math.max(1, parseInt(pageInput.value, 10) || 1);
          fetchFragment({ page: n }, { history: 'push' });
        };
        pageInput.addEventListener('change', commit);
        pageInput.addEventListener('keydown', (event) => {
          if (event.key === 'Enter') { event.preventDefault(); commit(); }
        });
      }

      const perSel = wrapper.querySelector('.per-page-select');
      if (perSel) {
        perSel.addEventListener('change', () => {
          fetchFragment({ per_page: perSel.value, page: 1 }, { history: 'push' });
        });
      }

      onAfterSwap(wrapper);
    }

    // --- Form-level event wiring (debounced typing, immediate selects).
    function bumpToFirstPage() {
      if (fields.page) fields.page.value = '1';
    }

    if (fields.q) {
      fields.q.addEventListener('input', () => {
        clearTimeout(debounce);
        debounce = setTimeout(() => {
          bumpToFirstPage();
          fetchFragment({});
        }, debounceMs);
      });
    }
    if (fields.priceMode) {
      fields.priceMode.addEventListener('change', () => {
        syncPriceValueState();
        bumpToFirstPage();
        fetchFragment({});
      });
    }
    if (fields.priceValue) {
      fields.priceValue.addEventListener('input', () => {
        clearTimeout(debounce);
        debounce = setTimeout(() => {
          bumpToFirstPage();
          fetchFragment({});
        }, debounceMs);
      });
    }
    for (const sel of [fields.setCode, fields.condition]) {
      if (!sel) continue;
      sel.addEventListener('change', () => {
        bumpToFirstPage();
        fetchFragment({});
      });
    }
    form.addEventListener('submit', (event) => {
      // Non-JS path also works; with JS we intercept.
      event.preventDefault();
      bumpToFirstPage();
      fetchFragment({});
    });

    window.addEventListener('popstate', () => {
      const usp = new URLSearchParams(window.location.search);
      if (fields.q)          fields.q.value          = usp.get('q')           || '';
      if (fields.priceMode)  fields.priceMode.value  = usp.get('price_mode')  || (defaults.price_mode || 'any');
      if (fields.priceValue) fields.priceValue.value = usp.get('price_value') || '';
      if (fields.setCode)    fields.setCode.value    = usp.get('set_code')    || '';
      if (fields.condition)  fields.condition.value  = usp.get('condition')   || '';
      if (fields.sort)       fields.sort.value       = usp.get('sort')        || (defaults.sort     || '');
      if (fields.dir)        fields.dir.value        = usp.get('dir')         || (defaults.dir      || 'asc');
      if (fields.page)       fields.page.value       = usp.get('page')        || '1';
      if (fields.perPage)    fields.perPage.value    = usp.get('per_page')    || (defaults.per_page || '50');
      syncPriceValueState();
      fetchFragment({}, { history: 'replace' });
    });

    syncPriceValueState();
    attachTableHandlers();

    return { fetchFragment, fields };
  }

  global.mtgcompare = global.mtgcompare || {};
  global.mtgcompare.attachPaginatedTable = attachPaginatedTable;
})(window);
