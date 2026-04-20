// Click-to-preview card art. Attach to `.card-icon` buttons carrying
// data-name (required) + data-set (optional). Opens a modal with the
// Scryfall image; dismisses on backdrop click, close button, or Escape.
(function () {
  const BASE = "https://api.scryfall.com/cards/named";

  // Deckbox exports suffixed set codes like "GK1_GOLGAR" — Scryfall just
  // uses "GK1". Name-only fallback handles anything we can't normalize.
  function normalizeSet(set) {
    if (!set) return "";
    const u = set.indexOf("_");
    return u === -1 ? set : set.slice(0, u);
  }

  function buildUrl(name, set) {
    const p = new URLSearchParams({ exact: name, format: "image", version: "normal" });
    if (set) p.set("set", set);
    return BASE + "?" + p.toString();
  }

  // Single reusable modal, built on first open.
  let modal = null, img = null, msg = null, current = null;

  function buildModal() {
    modal = document.createElement("div");
    modal.className = "card-modal";
    modal.innerHTML = `
      <div class="card-modal-inner">
        <button type="button" class="card-modal-close" aria-label="Close">&times;</button>
        <div class="card-modal-msg">Loading…</div>
        <img class="card-modal-img" alt="" referrerpolicy="no-referrer" decoding="async">
      </div>`;
    img = modal.querySelector(".card-modal-img");
    msg = modal.querySelector(".card-modal-msg");

    img.addEventListener("load",  () => { msg.style.display = "none"; img.style.display = "block"; });
    img.addEventListener("error", () => {
      if (current && !current.triedNameOnly && current.set) {
        current.triedNameOnly = true;
        img.src = buildUrl(current.name, "");
      } else {
        msg.textContent = "No image found.";
      }
    });

    modal.addEventListener("click", e => {
      if (e.target === modal || e.target.classList.contains("card-modal-close")) close();
    });
    document.body.appendChild(modal);
  }

  function open(name, set) {
    if (!modal) buildModal();
    current = { name, set, triedNameOnly: false };
    msg.textContent = "Loading…";
    msg.style.display = "block";
    img.style.display = "none";
    img.src = buildUrl(name, set);
    modal.classList.add("open");
  }

  function close() {
    current = null;
    if (!modal) return;
    modal.classList.remove("open");
    img.src = "";  // cancel in-flight request
  }

  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && modal && modal.classList.contains("open")) close();
  });

  document.addEventListener("click", e => {
    const btn = e.target.closest(".card-icon");
    if (!btn) return;

    e.preventDefault();
    const name = btn.dataset.name;
    if (!name) return;
    open(name, normalizeSet(btn.dataset.set || ""));
  });
})();
