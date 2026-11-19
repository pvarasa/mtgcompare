// Rows of shops that were still answering when a single-card search
// first painted (index.html, web._stream_late_results).
//
// The server keeps the response open and appends one inert <template
// data-late> per shop as it answers, each followed by an <i
// data-late-ready> so a template is only read once the parser is past
// it. This script moves each template's rows into the results table at
// their sort position, swaps in the filter controls rendered over every
// row so far, and ticks the shop off the "Still searching" line. The last
// template (data-late-done) carries the timed-out notice, if any.
(function () {
  const status = document.querySelector('[data-late-status]');
  if (!status) return;  // every shop answered before the first paint

  const root = document.getElementById('single-results');
  const table = document.getElementById('results-table');
  const tbody = table.tBodies[0];
  const count = root.querySelector('[data-result-count]');
  const handled = new WeakSet();

  const sortKey = (tr) => parseFloat(tr.dataset.sort);

  function insert(tr) {
    const key = sortKey(tr);
    const after = Array.from(tbody.rows).find((row) => sortKey(row) > key);
    tbody.insertBefore(tr, after || null);
  }

  function showRows() {
    const n = tbody.rows.length;
    if (count) count.textContent = `${n} result${n === 1 ? '' : 's'}`;
    root.querySelectorAll('#printing-filter, .table-scroll').forEach((el) => {
      el.hidden = n === 0;
    });
    const note = root.querySelector('[data-marketplace-note]');
    if (note) note.hidden = !tbody.querySelector('tr[data-marketplace]');
  }

  function addShop(template) {
    const content = template.content;
    content.querySelectorAll('tbody > tr').forEach(insert);
    const controls = content.querySelector('[data-filter-controls]');
    const filters = window.mtgcompare && window.mtgcompare.resultFilters;
    if (filters) {
      if (controls) filters.replaceControls(controls);
      else filters.refresh();
    }
    status.querySelectorAll('[data-pending-shop]').forEach((el) => {
      if (el.dataset.pendingShop === template.dataset.shop) el.remove();
    });
    showRows();
  }

  function finish(template) {
    status.replaceWith(template.content);
    const empty = root.querySelector('[data-results-empty]');
    if (empty) empty.hidden = tbody.rows.length > 0;
  }

  function handle(template) {
    handled.add(template);
    if (template.hasAttribute('data-late-done')) finish(template);
    else addShop(template);
    template.remove();
  }

  // A template is complete once its marker has been parsed after it.
  function scan(all) {
    document.querySelectorAll('template[data-late]').forEach((t) => {
      if (handled.has(t)) return;
      const next = t.nextElementSibling;
      if (all || (next && next.hasAttribute('data-late-ready'))) handle(t);
    });
  }

  const observer = new MutationObserver(() => scan(false));
  observer.observe(document.body, { childList: true });
  scan(false);
  document.addEventListener('DOMContentLoaded', () => {
    observer.disconnect();
    scan(true);
  });
})();
