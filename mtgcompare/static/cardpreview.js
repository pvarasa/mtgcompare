// Click-to-preview card art. Attach to `.card-icon` buttons carrying
// data-name (required) + data-set (optional). Opens a modal with the
// Scryfall image(s); double-faced cards show both faces side by side.
// Dismisses on backdrop click, close button, or Escape.
(function () {
  const NAMED = "https://api.scryfall.com/cards/named";

  // Deckbox exports suffixed set codes like "GK1_GOLGAR" — Scryfall just
  // uses "GK1". Name-only fallback handles anything we can't normalize.
  function normalizeSet(set) {
    if (!set) return "";
    const u = set.indexOf("_");
    return u === -1 ? set : set.slice(0, u);
  }

  function apiUrl(name, set) {
    const p = new URLSearchParams({ exact: name });
    if (set) p.set("set", set);
    return NAMED + "?" + p.toString();
  }

  // Single reusable modal, built on first open.
  let modal = null, facesEl = null, msgEl = null, controller = null;

  function buildModal() {
    modal = document.createElement("div");
    modal.className = "card-modal";
    modal.innerHTML = `
      <div class="card-modal-inner">
        <button type="button" class="card-modal-close" aria-label="Close">&times;</button>
        <div class="card-modal-msg">Loading…</div>
        <div class="card-modal-faces"></div>
      </div>`;
    facesEl = modal.querySelector(".card-modal-faces");
    msgEl   = modal.querySelector(".card-modal-msg");
    modal.addEventListener("click", e => {
      if (e.target === modal || e.target.classList.contains("card-modal-close")) close();
    });
    document.body.appendChild(modal);
  }

  function showImages(urls) {
    facesEl.innerHTML = "";
    const maxW = urls.length > 1 ? "44vw" : "88vw";
    let loaded = 0;
    urls.forEach(url => {
      const img = document.createElement("img");
      img.className = "card-modal-img";
      img.alt = "";
      img.referrerPolicy = "no-referrer";
      img.decoding = "async";
      img.style.maxWidth = maxW;
      img.addEventListener("load", () => {
        if (++loaded === urls.length) {
          msgEl.style.display = "none";
          facesEl.style.display = "flex";
        }
      });
      img.addEventListener("error", () => { msgEl.textContent = "No image found."; });
      facesEl.appendChild(img);
      img.src = url;  // set src after appending so load/error always fire
    });
  }

  async function open(name, set) {
    if (!modal) buildModal();
    if (controller) controller.abort();
    controller = new AbortController();
    const signal = controller.signal;

    msgEl.textContent = "Loading…";
    msgEl.style.display = "block";
    facesEl.style.display = "none";
    facesEl.innerHTML = "";
    modal.classList.add("open");

    try {
      let data = null;
      const r = await fetch(apiUrl(name, set), { signal });
      if (r.ok) data = await r.json();
      if (!data && set) {
        const r2 = await fetch(apiUrl(name, ""), { signal });
        if (r2.ok) data = await r2.json();
      }
      if (!data) { msgEl.textContent = "No image found."; return; }

      let urls;
      if (data.card_faces?.[0]?.image_uris) {
        // Double-faced card — collect all face images
        urls = data.card_faces.map(f => f.image_uris.normal).filter(Boolean);
      } else if (data.image_uris) {
        urls = [data.image_uris.normal];
      } else {
        msgEl.textContent = "No image found."; return;
      }
      showImages(urls);
    } catch (e) {
      if (e.name !== "AbortError") msgEl.textContent = "No image found.";
    }
  }

  function close() {
    if (controller) { controller.abort(); controller = null; }
    if (!modal) return;
    modal.classList.remove("open");
    facesEl.innerHTML = "";  // remove imgs to cancel any in-flight loads
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
