// Server-provided values: the JSON block #inventory-config in the template.
const INVENTORY_CONFIG = JSON.parse(document.getElementById('inventory-config').textContent);

// Inventory-specific glue on top of the shared paginated-table client:
// selection state, bulk delete (per-page and "all matching"), CSV /
// decklist export. The shared client handles filtering, sorting,
// pagination, fragment fetches, and URL sync.
(function () {
  const wrapper = document.getElementById('inv-table-wrapper');
  const form    = document.getElementById('inv-filter-form');
  if (!wrapper || !form) return;

  const inventoryDeleteUrl = INVENTORY_CONFIG.inventoryDeleteUrl;

  function download(filename, content, mime) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([content], { type: mime }));
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
  }
  function csvCell(v) {
    const s = v === null || v === undefined ? '' : String(v);
    return s.includes(',') || s.includes('"') || s.includes('\n')
      ? `"${s.replace(/"/g, '""')}"` : s;
  }
  function csrfToken() {
    return document.querySelector('meta[name="csrf-token"]')?.content || '';
  }

  // Gmail-style virtual selection:
  //   - "page" mode (default): row checkboxes drive selection; toolbar
  //     acts on what's visibly ticked.
  //   - "all" mode: user has clicked "Select all N matching" — the
  //     virtual selection covers the entire filtered set across pages.
  //     Row checkboxes appear ticked; clicking any of them collapses
  //     back to page mode with the resulting checkbox state. Exports
  //     are disabled because they're page-DOM-driven.
  const TYPED_CONFIRM_THRESHOLD = 100;

  function attachSelectionHandlers(scope) {
    const checkAll = scope.querySelector('#inv-check-all');
    const banner   = scope.querySelector('#inv-select-banner');
    const bannerPage = scope.querySelector('[data-banner-state="page"]');
    const bannerAll  = scope.querySelector('[data-banner-state="all"]');
    const btnDeck  = scope.querySelector('#inv-export-deck');
    const btnCsv   = scope.querySelector('#inv-export-csv');
    const btnDel   = scope.querySelector('#inv-delete');
    const selBtn   = scope.querySelector('#inv-select-all-matching');
    const clrBtn   = scope.querySelector('#inv-clear-selection');
    const selSpan  = scope.querySelector('#inv-sel-count');
    if (!selSpan) return;

    const totalMatching = parseInt(
      scope.querySelector('.result-summary')?.dataset.total || '0', 10);
    const rowCbs = () => Array.from(scope.querySelectorAll('#inv-table tbody tr input[type=checkbox]'));
    const checkedTrs = () => Array.from(scope.querySelectorAll('#inv-table tbody tr'))
      .filter(tr => tr.querySelector('input[type=checkbox]:checked'));

    // Closure-local: virtual all-matching mode flag. Reset on every
    // partial swap (this handler reattaches fresh).
    let allMatching = false;

    function setExportDisabled(disabled, reason) {
      for (const btn of [btnDeck, btnCsv]) {
        if (!btn) continue;
        btn.disabled = disabled;
        if (disabled && reason) btn.title = reason;
        else btn.removeAttribute('title');
      }
    }

    function updateState() {
      if (allMatching) {
        selSpan.textContent = `${totalMatching.toLocaleString()} selected (all matching)`;
        if (btnDel) {
          btnDel.disabled = totalMatching === 0;
          btnDel.textContent = `Delete ${totalMatching.toLocaleString()} selected`;
        }
        setExportDisabled(true, 'Clear selection to export the visible page');
        if (checkAll) { checkAll.checked = true; checkAll.indeterminate = false; }
        if (banner)   banner.hidden = false;
        if (bannerPage) bannerPage.hidden = true;
        if (bannerAll)  bannerAll.hidden  = false;
        return;
      }

      const n = checkedTrs().length;
      const cbs = rowCbs();
      const allPageChecked = cbs.length > 0 && cbs.every(cb => cb.checked);
      selSpan.textContent = n === 0
        ? '0 selected on this page'
        : `${n} selected on this page`;
      setExportDisabled(n === 0);
      if (btnDel) {
        btnDel.disabled = n === 0;
        btnDel.textContent = n === 0
          ? 'Delete selected'
          : `Delete ${n} selected`;
      }
      if (checkAll) {
        checkAll.checked = allPageChecked;
        checkAll.indeterminate = !allPageChecked && n > 0;
      }
      if (banner) {
        // Banner only shows when the user has ticked the whole page and
        // there are more matching rows off-page.
        banner.hidden = !(allPageChecked && totalMatching > cbs.length);
        if (bannerPage) bannerPage.hidden = false;
        if (bannerAll)  bannerAll.hidden  = true;
      }
    }

    function collapseToPage() { allMatching = false; updateState(); }

    rowCbs().forEach(cb => {
      cb.addEventListener('change', () => {
        // Any explicit row-level interaction breaks out of virtual mode.
        if (allMatching) allMatching = false;
        updateState();
      });
    });
    if (checkAll) {
      checkAll.addEventListener('change', () => {
        allMatching = false;
        rowCbs().forEach(cb => { cb.checked = checkAll.checked; });
        updateState();
      });
    }
    if (selBtn) {
      selBtn.addEventListener('click', () => {
        allMatching = true;
        rowCbs().forEach(cb => { cb.checked = true; });
        updateState();
      });
    }
    if (clrBtn) {
      clrBtn.addEventListener('click', () => {
        rowCbs().forEach(cb => { cb.checked = false; });
        collapseToPage();
      });
    }

    if (btnDeck) {
      btnDeck.addEventListener('click', () => {
        const lines = checkedTrs().map(tr =>
          `${tr.dataset.qty}x ${tr.dataset.card} (${tr.dataset.set})`);
        download('decklist.txt', lines.join('\n'), 'text/plain');
      });
    }
    if (btnCsv) {
      btnCsv.addEventListener('click', () => {
        const headers = ['Card Name','Set Code','Set Name','Card Number',
                         'Quantity','Condition','Printing','Language',
                         'Price Bought','Date Bought'];
        const dataRows = checkedTrs().map(tr => [
          tr.dataset.card, tr.dataset.set, tr.dataset.setname, tr.dataset.num,
          tr.dataset.qty,  tr.dataset.cond, tr.dataset.print, tr.dataset.lang,
          tr.dataset.price, tr.dataset.date,
        ].map(csvCell).join(','));
        download('inventory.csv', [headers.join(','), ...dataRows].join('\r\n'), 'text/csv');
      });
    }

    if (btnDel) {
      btnDel.addEventListener('click', async () => {
        let body;
        let count;
        if (allMatching) {
          count = totalMatching;
          const q          = form.querySelector('input[name="q"]').value.trim();
          const price_mode = form.querySelector('select[name="price_mode"]').value;
          const price_value = form.querySelector('input[name="price_value"]').value.trim();
          const set_code  = form.querySelector('select[name="set_code"]')?.value || '';
          const condition = form.querySelector('select[name="condition"]')?.value || '';
          body = { match: { q, price_mode, price_value, set_code, condition } };
        } else {
          const targets = checkedTrs();
          if (targets.length === 0) return;
          count = targets.length;
          const ids = targets.map(tr => parseInt(tr.dataset.id, 10)).filter(Number.isFinite);
          body = { ids };
        }
        if (!confirmDelete(count, allMatching)) return;
        btnDel.disabled = true;
        try {
          const response = await fetch(inventoryDeleteUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
            body: JSON.stringify(body),
          });
          if (!response.ok) {
            const errBody = await response.json().catch(() => ({}));
            window.alert(`Delete failed: ${errBody.error || response.status}`);
            btnDel.disabled = false;
            return;
          }
          // Full reload: the stats line at the top is server-rendered
          // outside the partial, so we need a fresh page to reflect
          // the new totals (cost basis, total copies, lot count).
          window.location.reload();
        } catch (error) {
          window.alert(`Network error: ${error.message}`);
          btnDel.disabled = false;
        }
      });
    }

    updateState();
  }

  function confirmDelete(count, virtual) {
    if (count <= 0) return false;
    const noun = count === 1 ? 'row' : 'rows';
    const scope = virtual
      ? `all ${count.toLocaleString()} matching ${noun} across all pages`
      : `${count.toLocaleString()} ${noun}`;
    if (count > TYPED_CONFIRM_THRESHOLD) {
      const answer = window.prompt(
        `Delete ${scope}?\n\nThis cannot be undone. Type DELETE to confirm.`);
      return answer === 'DELETE';
    }
    return window.confirm(`Delete ${scope}? This cannot be undone.`);
  }

  window.mtgcompare.attachPaginatedTable({
    form, wrapper,
    summaryEl: document.getElementById('inv-stats'),
    spinnerClass: 'inv-loading',
    defaults: { sort: 'card_name', dir: 'asc', price_mode: 'any', per_page: '50' },
    onAfterSwap: attachSelectionHandlers,
  });
})();
