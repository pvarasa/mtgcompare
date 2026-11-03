// Server-provided values: the JSON block #market-download-config in the template.
const MARKET_DOWNLOAD_CONFIG = JSON.parse(document.getElementById('market-download-config').textContent);

(function () {
  const isFirstDownload = MARKET_DOWNLOAD_CONFIG.isFirstDownload;
  const historyDownloadUrl = MARKET_DOWNLOAD_CONFIG.historyDownloadUrl;
  const historyStatusUrl = MARKET_DOWNLOAD_CONFIG.historyStatusUrl;
  const downloadBtn = document.getElementById('mkt-history-download-btn');
  const progressEl = document.getElementById('mkt-history-progress');
  const progressFillEl = document.getElementById('mkt-history-fill');
  const progressPhaseEl = document.getElementById('mkt-history-phase');
  const progressPctEl = document.getElementById('mkt-history-pct');
  const progressDetailEl = document.getElementById('mkt-history-detail');

  if (downloadBtn) {
    function setProgress(job) {
      progressEl.classList.add('open');
      const progress = Math.max(0, Math.min(100, job.progress || 0));
      progressFillEl.style.width = `${progress}%`;
      progressPhaseEl.textContent = job.phase || 'Working';
      progressPctEl.textContent = `${progress}%`;
      progressDetailEl.textContent = job.error || job.detail || '';
      downloadBtn.disabled = job.state === 'running';
      if (job.state === 'running') {
        downloadBtn.textContent = 'Updating...';
      } else {
        downloadBtn.textContent = 'Update prices';
      }
    }

    function showFailure(message) {
      downloadBtn.disabled = false;
      downloadBtn.textContent = 'Update prices';
      progressEl.classList.add('open');
      progressPhaseEl.textContent = 'Failed';
      progressPctEl.textContent = '';
      progressFillEl.style.width = '0%';
      progressDetailEl.textContent = message;
    }

    downloadBtn.addEventListener('click', () => {
      if (isFirstDownload && !confirm(
        'First-time setup fetches a large file (~150 MB) and builds a local price database. This takes 5–15 minutes depending on your connection and machine.\n\nContinue?'
      )) return;
      downloadBtn.disabled = true;
      downloadBtn.textContent = 'Starting...';
      progressEl.classList.add('open');
      progressPhaseEl.textContent = 'Queued';
      progressPctEl.textContent = '0%';
      progressFillEl.style.width = '0%';
      progressDetailEl.textContent = 'Starting price update...';

      window.mtgcompare.runJob({
        startUrl: historyDownloadUrl,
        statusUrl: historyStatusUrl,
        onProgress: setProgress,
        onDone: job => {
          setProgress(job);
          window.setTimeout(() => window.location.reload(), 900);
        },
        onFail: showFailure,
      });
    });
  }

})();
