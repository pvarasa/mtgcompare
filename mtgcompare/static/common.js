/*
 * common.js — behaviour every page shares, wired by data attributes so
 * templates need no inline handlers (the CSP forbids inline script).
 *
 *   <form data-confirm="Remove this lot?">  asks before submitting
 *   <button data-action="back">              goes back in history
 */
(function () {
  document.addEventListener('submit', event => {
    const form = event.target.closest('form[data-confirm]');
    if (form && !window.confirm(form.dataset.confirm)) event.preventDefault();
  });
  document.addEventListener('click', event => {
    if (event.target.closest('[data-action="back"]')) window.history.back();
  });
})();
