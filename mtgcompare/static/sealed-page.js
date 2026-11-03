// Server-provided values: the JSON block #sealed-config in the template.
const SEALED_CONFIG = JSON.parse(document.getElementById('sealed-config').textContent);

(function () {
  const historyUrl = SEALED_CONFIG.historyUrl;
  const refreshUrl = SEALED_CONFIG.refreshUrl;
  const statusUrl = SEALED_CONFIG.statusUrl;
  const priceChart = window.mtgcompare.priceChart;

  document.addEventListener('click', event => {
    const btn = event.target.closest('.market-chart-trigger');
    if (!btn) return;
    const seriesUrl = series => `${historyUrl}?${new URLSearchParams({ uuid: btn.dataset.uuid, series })}`;
    priceChart.show({
      url: seriesUrl(btn.dataset.series),
      seriesUrl,
      title: btn.dataset.name,
      subtitle: btn.dataset.meta,
      subtitleFor: d => `${btn.dataset.meta}${d.available_since ? ` / tracking since ${priceChart.formatDate(d.available_since)}` : ''}`,
      errorMessage: 'Could not load price history for this product.',
      emptyMessage: SEALED_CONFIG.emptyMessage,
    });
  });

  // Refresh: POST starts a background job; poll its status until done.
  const refreshBtn = document.getElementById('sealed-refresh-btn');
  if (!refreshBtn) return;  // hosted site: the daily cron refreshes
  const detailEl = document.getElementById('sealed-refresh-detail');
  function finish(text) {
    refreshBtn.disabled = false;
    refreshBtn.textContent = 'Refresh prices';
    detailEl.textContent = text;
  }
  refreshBtn.addEventListener('click', () => {
    refreshBtn.disabled = true;
    refreshBtn.textContent = 'Refreshing...';
    detailEl.textContent = 'Starting...';
    window.mtgcompare.runJob({
      startUrl: refreshUrl,
      statusUrl,
      onProgress: job => { detailEl.textContent = `${job.progress || 0}% · ${job.detail || job.phase || ''}`; },
      onDone: job => {
        finish(job.detail || 'Done.');
        window.setTimeout(() => window.location.reload(), 900);
      },
      onFail: finish,
    });
  });
})();
