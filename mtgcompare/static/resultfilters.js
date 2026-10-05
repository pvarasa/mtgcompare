// Printing filters for the single-card results table (index.html).
//
// Filtering is client-side over rows the server already rendered, so
// narrowing to a set or a treatment never re-queries the shops. Rows carry
// data-set / data-variant (comma-joined tags, "" = regular, "?" = unknown)
// / data-number. Rows whose version the shop doesn't state ("?") stay
// visible under every version filter — hiding them could hide the
// cheapest copy. The initial state comes from filter tokens in the query
// ("set:DMR is:borderless"), parsed server-side into data-initial.
(function () {
  const bar = document.getElementById('printing-filter');
  const table = document.getElementById('results-table');
  if (!bar || !table || !table.tBodies.length) return;

  const rows = Array.from(table.tBodies[0].rows);
  const setSelect = bar.querySelector('[data-filter-set]');
  const chips = Array.from(bar.querySelectorAll('[data-filter-variant]'));
  const numberChip = bar.querySelector('[data-filter-number]');
  const count = bar.querySelector('[data-filter-count]');

  let initial = {};
  try {
    initial = JSON.parse(bar.dataset.initial || '{}') || {};
  } catch (e) {
    initial = {};
  }
  const state = {
    set: initial.set || '',
    number: initial.number || '',
    variants: Array.isArray(initial.variants) ? initial.variants : [],
  };

  function matches(tr) {
    if (state.set && tr.dataset.set !== state.set) return false;
    const number = tr.dataset.number;
    if (state.number && number && number !== state.number) return false;
    const variant = tr.dataset.variant;
    if (variant === '?') return true;
    const tags = variant ? variant.split(',') : [];
    return state.variants.every((want) =>
      want === 'regular' ? tags.length === 0 : tags.includes(want));
  }

  function moveCheapestBadge(first) {
    table.querySelectorAll('tbody .badge').forEach((b) => b.remove());
    rows.forEach((tr) => tr.classList.remove('cheapest'));
    if (!first) return;
    first.classList.add('cheapest');
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = 'cheapest';
    first.cells[0].appendChild(badge);
  }

  function render() {
    let shown = 0;
    let first = null;
    rows.forEach((tr) => {
      const ok = matches(tr);
      tr.hidden = !ok;
      if (ok) {
        shown += 1;
        if (!first) first = tr;
      }
    });
    moveCheapestBadge(first);

    if (setSelect) setSelect.value = state.set;
    chips.forEach((chip) => {
      const value = chip.dataset.filterVariant;
      const pressed = value === ''
        ? state.variants.length === 0
        : state.variants.includes(value);
      chip.setAttribute('aria-pressed', pressed ? 'true' : 'false');
    });
    if (numberChip) {
      numberChip.hidden = !state.number;
      numberChip.textContent = state.number ? `#${state.number} ×` : '';
      numberChip.setAttribute('aria-pressed', state.number ? 'true' : 'false');
      numberChip.title = 'Clear the collector-number filter';
    }

    const filtering = state.set || state.number || state.variants.length;
    if (count) {
      if (!filtering) count.textContent = '';
      else if (shown === 0) count.textContent = 'No copies match these filters.';
      else count.textContent = `Showing ${shown} of ${rows.length}`;
    }
  }

  if (setSelect) {
    setSelect.addEventListener('change', () => {
      state.set = setSelect.value;
      render();
    });
  }
  chips.forEach((chip) => {
    chip.addEventListener('click', () => {
      const value = chip.dataset.filterVariant;
      const alreadyOnly = state.variants.length === 1 && state.variants[0] === value;
      state.variants = value === '' || alreadyOnly ? [] : [value];
      render();
    });
  });
  if (numberChip) {
    numberChip.addEventListener('click', () => {
      state.number = '';
      render();
    });
  }

  render();
})();
