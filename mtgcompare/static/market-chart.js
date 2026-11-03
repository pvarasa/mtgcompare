// Server-provided values: the JSON block #market-chart-config in the template.
const MARKET_CHART_CONFIG = JSON.parse(document.getElementById('market-chart-config').textContent);

(function () {
  const historyUrl = MARKET_CHART_CONFIG.historyUrl;
  const portfolioHistoryUrl = MARKET_CHART_CONFIG.portfolioHistoryUrl;
  const priceChart = window.mtgcompare.priceChart;
  const formatDate = priceChart.formatDate;
  const mtgjsonEmpty = 'No MTGJSON price history is available yet.<br>Use <strong>Update prices</strong> to import it.';
  const mtgjsonFootnote = 'Download history first if MTGJSON data is not available locally.';

  // Delegate chart clicks on the wrapper, not the tbody — the
  // tbody is replaced on every filter/sort/pagination fetch, but
  // the wrapper stays put for the page's lifetime.
  const mktWrapper = document.getElementById('mkt-table-wrapper');
  (mktWrapper || document).addEventListener('click', event => {
    const btn = event.target.closest('.market-chart-trigger');
    if (!btn) return;
    const params = new URLSearchParams({
      card_name: btn.dataset.chartCard,
      set_code: btn.dataset.chartSet,
      card_number: btn.dataset.chartNumber,
      printing: btn.dataset.chartPrinting,
      period: 'all',
    });
    priceChart.show({
      url: `${historyUrl}?${params.toString()}`,
      title: btn.dataset.chartCard,
      subtitle: `${btn.dataset.chartSet}${btn.dataset.chartPrinting.toLowerCase() === 'foil' ? ' / Foil' : ' / Non-foil'}`,
      subtitleFor: d =>
        `${d.set_code}${d.card_number ? ` #${d.card_number}` : ''}${d.is_foil ? ' / Foil' : ' / Non-foil'}${d.available_since ? ` / tracking since ${formatDate(d.available_since)}` : ''}`,
      errorMessage: 'Could not load market history for this card.',
      errorFootnote: mtgjsonFootnote,
      emptyMessage: mtgjsonEmpty,
    });
  });

  const portfolioBtn = document.getElementById('mkt-portfolio-history-btn');
  if (portfolioBtn) {
    portfolioBtn.addEventListener('click', () => {
      priceChart.show({
        url: portfolioHistoryUrl,
        title: 'Portfolio total',
        subtitle: 'Whole-portfolio value across MTGJSON history',
        subtitleFor: d => {
          const since = d.available_since ? ` / since ${formatDate(d.available_since)}` : '';
          return `${d.mapped_count} of ${d.lot_count} lot${d.lot_count === 1 ? '' : 's'} tracked${since}`;
        },
        errorMessage: 'Could not load portfolio history.',
        errorFootnote: mtgjsonFootnote,
        emptyMessage: mtgjsonEmpty,
      });
    });
  }

  // Chart clicks are delegated on the wrapper (above) so they
  // survive partial swaps. Filter/sort/pagination is handled by
  // the shared paginatedtable client.
  const mktForm = document.getElementById('mkt-filter-form');
  if (mktForm && mktWrapper && window.mtgcompare && window.mtgcompare.attachPaginatedTable) {
    window.mtgcompare.attachPaginatedTable({
      form: mktForm, wrapper: mktWrapper,
      summaryEl: document.getElementById('mkt-summary'),
      spinnerClass: 'mkt-loading',
      defaults: { sort: 'pnl_usd', dir: 'desc', price_mode: 'any', per_page: '50' },
    });
  }
})();
