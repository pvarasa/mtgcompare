// Price-history chart modal shared by the Market and Sealed pages.
//
// Markup lives in templates/_price_chart_modal.html, styles in
// _price_chart_styles.html. Pages call
//   window.mtgcompare.priceChart.show({ url, title, subtitle, subtitleFor, errorMessage })
// with a history endpoint whose payload has the /market/history shape:
//   { ok, points: [{market_date, price_usd|null}], available_periods,
//     period_days, default_period, has_history, source: {detail}, downloaded_at }
//
// X positions are by date, not by point index: the card history is dense
// daily (where the two are the same) but sealed history mixes a weekly
// backfill with daily points, which index spacing would distort.
(function () {
  const modal = document.getElementById('market-chart-modal');
  if (!modal) return;
  const titleEl = document.getElementById('market-chart-title');
  const subtitleEl = document.getElementById('market-chart-subtitle');
  const currentEl = document.getElementById('market-chart-current');
  const deltaEl = document.getElementById('market-chart-delta');
  const lowEl = document.getElementById('market-chart-low');
  const controlsEl = document.getElementById('market-chart-controls');
  const stageEl = document.getElementById('market-chart-stage');
  const footnoteEl = document.getElementById('market-chart-footnote');
  const closeEl = document.getElementById('market-chart-close');

  const DAY_MS = 24 * 60 * 60 * 1000;
  let activeHistory = null;

  function formatUsd(value) {
    if (value == null || Number.isNaN(value)) return '-';
    return `$${value.toFixed(2)}`;
  }

  function formatDate(stamp) {
    if (!stamp) return '-';
    const dt = new Date(stamp);
    if (Number.isNaN(dt.getTime())) return stamp;
    return dt.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }

  function slicePoints(points, period) {
    if (period === 'all') return points.slice();
    const days = activeHistory && activeHistory.period_days ? activeHistory.period_days[period] : null;
    if (days == null) return points.slice();
    const cutoff = Date.now() - days * DAY_MS;
    return points.filter(point => new Date(point.market_date).getTime() >= cutoff);
  }

  function summarize(points) {
    const priced = points.filter(point => point.price_usd != null);
    if (!priced.length) return { current: null, delta: null, low: null };
    const current = priced[priced.length - 1].price_usd;
    const low = Math.min.apply(null, priced.map(point => point.price_usd));
    return { current, delta: current - priced[0].price_usd, low };
  }

  function setDelta(delta) {
    if (delta == null) {
      deltaEl.textContent = '-';
      deltaEl.className = 'market-chart-stat-value';
      return;
    }
    const sign = delta > 0 ? '+' : '';
    deltaEl.textContent = `${sign}$${delta.toFixed(2)}`;
    deltaEl.className = `market-chart-stat-value ${delta > 0 ? 'positive' : (delta < 0 ? 'negative' : '')}`.trim();
  }

  function buildChart(points) {
    const times = points.map(point => new Date(point.market_date).getTime());
    const priced = points
      .map((point, index) => ({ ...point, t: times[index] }))
      .filter(point => point.price_usd != null);
    if (priced.length < 2) return '';

    const width = 760;
    const height = 360;
    const pad = { top: 18, right: 18, bottom: 34, left: 58 };
    const innerWidth = width - pad.left - pad.right;
    const innerHeight = height - pad.top - pad.bottom;
    const values = priced.map(point => point.price_usd);
    const min = Math.min.apply(null, values);
    const max = Math.max.apply(null, values);
    const spread = Math.max(max - min, Math.max(max * 0.08, 0.25));
    const lo = Math.max(0, min - spread * 0.35);
    const hi = max + spread * 0.35;
    const t0 = times[0];
    const tSpan = times[times.length - 1] - t0;
    const xFor = t => pad.left + (tSpan > 0 ? ((t - t0) / tSpan) * innerWidth : innerWidth / 2);
    const yFor = value => pad.top + (1 - ((value - lo) / (hi - lo || 1))) * innerHeight;
    const yTicks = Array.from({ length: 5 }, (_, index) => lo + ((hi - lo) * index / 4));

    // A null point breaks the line (a day with no price shows as a gap).
    const segments = [];
    let currentSegment = [];
    points.forEach((point, index) => {
      if (point.price_usd == null) {
        if (currentSegment.length) segments.push(currentSegment);
        currentSegment = [];
        return;
      }
      currentSegment.push({ x: xFor(times[index]), y: yFor(point.price_usd) });
    });
    if (currentSegment.length) segments.push(currentSegment);

    const lineMarkup = segments.map(segment => {
      const linePath = segment.map((point, idx) => `${idx ? 'L' : 'M'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(' ');
      const areaPath = `${linePath} L ${segment[segment.length - 1].x.toFixed(2)} ${(pad.top + innerHeight).toFixed(2)} L ${segment[0].x.toFixed(2)} ${(pad.top + innerHeight).toFixed(2)} Z`;
      return `
        <path d="${areaPath}" fill="url(#chart-area-fill)"></path>
        <path d="${linePath}" fill="none" stroke="url(#chart-line-stroke)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></path>
      `;
    }).join('');

    // Drop the middle date label on narrow viewports — three ticks
    // collide because tick text is rendered in screen pixels while
    // tick X positions scale with the viewBox.
    const narrow = window.innerWidth < 480;
    const last = points.length - 1;
    const xIndexes = (narrow ? [0, last] : [0, Math.floor(last / 2), last])
      .filter((index, position, array) => array.indexOf(index) === position);

    const yMarkup = yTicks.map(value => {
      const y = yFor(value).toFixed(2);
      return `
        <line class="market-chart-grid" x1="${pad.left}" y1="${y}" x2="${width - pad.right}" y2="${y}"></line>
        <text class="market-chart-axis" x="${pad.left - 8}" y="${Number(y) + 4}" text-anchor="end">$${value.toFixed(2)}</text>
      `;
    }).join('');

    const xMarkup = xIndexes.map(index => `
      <text class="market-chart-axis" x="${xFor(times[index]).toFixed(2)}" y="${height - 8}" text-anchor="middle">${formatDate(points[index].market_date)}</text>
    `).join('');

    const dotMarkup = priced.map((point, index) => `
      <circle cx="${xFor(point.t).toFixed(2)}" cy="${yFor(point.price_usd).toFixed(2)}" r="${index === priced.length - 1 ? 4.5 : 3.2}" fill="${index === priced.length - 1 ? '#c9a84c' : '#5a9fd4'}"></circle>
    `).join('');

    return `
      <svg class="market-chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Price history chart">
        <defs>
          <linearGradient id="chart-area-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="rgba(201, 168, 76, 0.38)"></stop>
            <stop offset="100%" stop-color="rgba(90, 159, 212, 0.04)"></stop>
          </linearGradient>
          <linearGradient id="chart-line-stroke" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stop-color="#5a9fd4"></stop>
            <stop offset="100%" stop-color="#c9a84c"></stop>
          </linearGradient>
        </defs>
        ${yMarkup}
        ${lineMarkup}
        ${dotMarkup}
        ${xMarkup}
      </svg>
    `;
  }

  function renderPeriod(period) {
    if (!activeHistory) return;
    controlsEl.querySelectorAll('.market-chart-range').forEach(button => {
      button.classList.toggle('active', button.dataset.period === period);
    });

    const points = slicePoints(activeHistory.points, period);
    const priced = points.filter(point => point.price_usd != null);
    const stats = summarize(points);
    currentEl.textContent = formatUsd(stats.current);
    lowEl.textContent = formatUsd(stats.low);
    setDelta(stats.delta);

    if (!activeHistory.has_history) {
      stageEl.innerHTML = `<div class="market-chart-empty">${activeHistory.empty_message || 'No price history is available yet.'}</div>`;
    } else if (!priced.length) {
      stageEl.innerHTML = '<div class="market-chart-empty">There is no priced data in this range.</div>';
    } else if (priced.length === 1) {
      stageEl.innerHTML = '<div class="market-chart-empty">Only one price point is available in this range.</div>';
    } else {
      stageEl.innerHTML = buildChart(points);
    }

    footnoteEl.textContent = `${activeHistory.source.detail} Last updated ${activeHistory.downloaded_at ? formatDate(activeHistory.downloaded_at) : 'never'}.`;
  }

  function openModal() { modal.classList.add('open'); }
  function closeModal() { modal.classList.remove('open'); }

  closeEl.addEventListener('click', closeModal);
  modal.addEventListener('click', event => {
    if (event.target === modal) closeModal();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeModal();
  });

  function resetModalChrome(title, subtitle) {
    openModal();
    titleEl.textContent = title;
    subtitleEl.textContent = subtitle || '';
    currentEl.textContent = '-';
    lowEl.textContent = '-';
    setDelta(null);
    controlsEl.innerHTML = '';
    stageEl.innerHTML = '<div class="market-chart-loading">Loading chart...</div>';
    footnoteEl.textContent = '';
  }

  function renderHistory(data, subtitleFor) {
    activeHistory = data;
    if (subtitleFor) subtitleEl.textContent = subtitleFor(data);
    controlsEl.innerHTML = data.available_periods.map(period => `
      <button type="button" class="market-chart-range${period === data.default_period ? ' active' : ''}" data-period="${period}">
        ${period.toUpperCase()}
      </button>
    `).join('');
    controlsEl.querySelectorAll('.market-chart-range').forEach(rangeButton => {
      rangeButton.addEventListener('click', () => renderPeriod(rangeButton.dataset.period));
    });
    renderPeriod(data.default_period || '1m');
  }

  function showError(message, footnote) {
    activeHistory = null;
    stageEl.innerHTML = `<div class="market-chart-empty">${message}</div>`;
    footnoteEl.textContent = footnote || '';
  }

  function show({ url, title, subtitle, subtitleFor, errorMessage, errorFootnote, emptyMessage }) {
    resetModalChrome(title, subtitle);
    return fetch(url)
      .then(response => response.json())
      .then(data => {
        if (!data.ok) throw new Error(data.error || 'History fetch failed');
        if (emptyMessage) data.empty_message = emptyMessage;
        renderHistory(data, subtitleFor);
      })
      .catch(() => showError(errorMessage || 'Could not load price history.', errorFootnote));
  }

  window.mtgcompare = window.mtgcompare || {};
  window.mtgcompare.priceChart = { show, formatDate };
})();
