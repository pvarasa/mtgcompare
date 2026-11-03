/*
 * jobs.js — start a background job and follow it to the end.
 *
 *   window.mtgcompare.runJob({
 *     startUrl, statusUrl,          // POST to start; GET ?job_id= to poll
 *     onProgress(job),              // every status read while running
 *     onDone(job), onFail(message), // terminal states (incl. start errors)
 *   });
 *
 * The status endpoint is shared (/market/history/download/status). A
 * single failed poll (a network blip) is tolerated; ~10 s of consecutive
 * misses ends it with onFail.
 */
(function () {
  const POLL_MS = 1000;
  const MAX_MISSES = 10;

  function runJob({ startUrl, statusUrl, onProgress, onDone, onFail }) {
    let timer = null;
    let misses = 0;
    let finished = false;
    const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
    const end = fn => arg => { if (finished) return; finished = true; stop(); fn(arg); };
    const done = end(onDone);
    const fail = end(onFail);

    function check(jobId) {
      fetch(`${statusUrl}?job_id=${encodeURIComponent(jobId)}`)
        .then(r => r.json())
        .then(job => {
          if (!job.ok) throw new Error(job.error || 'Status fetch failed');
          misses = 0;
          if (job.state === 'done') done(job);
          else if (job.state === 'error') fail(job.error || job.detail || 'The job failed.');
          else onProgress(job);
        })
        .catch(() => { if (++misses >= MAX_MISSES) fail('Could not read the job status.'); });
    }

    const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
    fetch(startUrl, { method: 'POST', headers: { 'X-Requested-With': 'fetch', 'X-CSRFToken': csrf } })
      .then(r => r.json())
      .then(data => {
        if (!data.ok) throw new Error(data.error || 'Could not start the job.');
        check(data.job_id);
        timer = window.setInterval(() => check(data.job_id), POLL_MS);
      })
      .catch(err => fail(err.message || 'Could not start the job.'));
  }

  window.mtgcompare = window.mtgcompare || {};
  window.mtgcompare.runJob = runJob;
})();
