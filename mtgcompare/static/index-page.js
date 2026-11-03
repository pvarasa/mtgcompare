(function () {
  // --- mode toggle ---
  const btnSingle     = document.getElementById('mode-single');
  const btnDecklist   = document.getElementById('mode-decklist');
  const panelSingle   = document.getElementById('panel-single');
  const panelDecklist = document.getElementById('panel-decklist');

  function activate(mode) {
    const dl = mode === 'decklist';
    btnSingle.classList.toggle('active', !dl);
    btnDecklist.classList.toggle('active', dl);
    panelSingle.style.display   = dl ? 'none' : '';
    panelDecklist.style.display = dl ? '' : 'none';
  }
  btnSingle.addEventListener('click',   () => activate('single'));
  btnDecklist.addEventListener('click', () => activate('decklist'));

  // --- shipping toggle: hidden input is what actually submits ---
  const toggle    = document.getElementById('ship-toggle');
  const shipVal   = document.getElementById('shipping-val');
  const cfgPanel  = document.getElementById('ship-cfg-panel');
  if (toggle && shipVal && cfgPanel) {
    toggle.addEventListener('change', () => {
      shipVal.value = toggle.checked ? '1' : '0';
      cfgPanel.style.display = toggle.checked ? '' : 'none';
    });
  }

  // --- shop filter ---
  // localStorage persistence keeps the filter across browser sessions
  // even when the URL is bare. Each form has its own panel; both read
  // from and write to the same key so toggling on the single-card tab
  // pre-fills the decklist tab. The two panels render different
  // checkbox sets (marketplace shops are decklist-excluded), so state
  // is stored as explicit enabled/disabled name lists and persist()
  // merges around names the saving form doesn't render — otherwise a
  // decklist-panel save would silently drop the single-card-only
  // shops, and a renamed shop would come back permanently unchecked.
  // Names in neither list keep their server-rendered default.
  const SHOP_FILTER_KEY = 'mtgcompare.shopFilter';
  function loadShopFilter() {
    try {
      const raw = localStorage.getItem(SHOP_FILTER_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }
  function saveShopFilter(state) {
    try { localStorage.setItem(SHOP_FILTER_KEY, JSON.stringify(state)); }
    catch (e) { /* private mode etc. */ }
  }

  function wireShopFilter(form) {
    if (!form) return;
    const flag = form.querySelector('[data-shop-filter-flag]');
    const toggleEl = form.querySelector('[data-shop-filter-toggle]');
    const panel = form.querySelector('[data-shop-filter-panel]');
    const summary = form.querySelector('[data-shop-filter-summary]');
    const checkboxes = form.querySelectorAll('[data-shop-filter-checkbox]');
    const allBtn = form.querySelector('[data-shop-filter-all]');
    const noneBtn = form.querySelector('[data-shop-filter-none]');
    if (!flag || !toggleEl || !panel || !checkboxes.length) return;

    function updateSummary() {
      const total = checkboxes.length;
      const on = Array.from(checkboxes).filter(c => c.checked).length;
      if (!toggleEl.checked) {
        summary.textContent = '';
      } else if (on === total) {
        summary.textContent = `all ${total}`;
      } else {
        summary.textContent = `${on} of ${total}`;
      }
    }

    function persist() {
      if (!toggleEl.checked) {
        saveShopFilter({active: false});
        return;
      }
      const stored = loadShopFilter() || {};
      const mine = new Set(Array.from(checkboxes).map(c => c.name));
      const keep = arr => (Array.isArray(arr) ? arr.filter(n => !mine.has(n)) : []);
      const enabled = keep(stored.enabled);
      const disabled = keep(stored.disabled);
      checkboxes.forEach(c => (c.checked ? enabled : disabled).push(c.name));
      saveShopFilter({active: true, enabled, disabled});
    }

    // Hydrate from localStorage if the server didn't tell us a state.
    if (flag.value !== '1') {
      const stored = loadShopFilter();
      if (stored && stored.active) {
        toggleEl.checked = true;
        flag.value = '1';
        panel.style.display = '';
        const on = new Set(stored.enabled || []);
        const off = new Set(stored.disabled || []);
        checkboxes.forEach(c => {
          if (on.has(c.name)) c.checked = true;
          else if (off.has(c.name)) c.checked = false;
        });
      }
    }
    updateSummary();

    toggleEl.addEventListener('change', () => {
      flag.value = toggleEl.checked ? '1' : '0';
      panel.style.display = toggleEl.checked ? '' : 'none';
      updateSummary();
      persist();
    });
    checkboxes.forEach(cb => cb.addEventListener('change', () => {
      updateSummary();
      persist();
    }));
    if (allBtn) {
      allBtn.addEventListener('click', () => {
        checkboxes.forEach(c => { c.checked = true; });
        updateSummary();
        persist();
      });
    }
    if (noneBtn) {
      noneBtn.addEventListener('click', () => {
        checkboxes.forEach(c => { c.checked = false; });
        updateSummary();
        persist();
      });
    }
  }
  wireShopFilter(panelSingle.querySelector('form'));
  wireShopFilter(panelDecklist.querySelector('form'));

  // --- loading indicator: cold scrapes can take ~5s; show progress ---
  function wireLoadingIndicator(form, loadingEl) {
    if (!form || !loadingEl) return;
    form.addEventListener('submit', () => {
      loadingEl.classList.add('active');
      const btn = form.querySelector('button[type=submit]');
      if (btn) {
        btn.disabled = true;
        btn.dataset.label = btn.textContent;
        btn.textContent = 'Searching…';
      }
    });
  }
  wireLoadingIndicator(
    panelSingle.querySelector('form'),
    document.getElementById('search-loading-single'),
  );
  wireLoadingIndicator(
    panelDecklist.querySelector('form'),
    document.getElementById('search-loading-decklist'),
  );

  // BFCache: clicking back can restore a still-disabled button. Reset.
  window.addEventListener('pageshow', (e) => {
    if (!e.persisted) return;
    document.querySelectorAll('.search-loading.active').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('button[data-label]').forEach(btn => {
      btn.disabled = false;
      btn.textContent = btn.dataset.label;
      delete btn.dataset.label;
    });
  });
})();
