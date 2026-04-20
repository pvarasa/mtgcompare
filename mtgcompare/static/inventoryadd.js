// Inventory add-panel wiring:
// - mode toggle (single / decklist / csv)
// - single card: Scryfall name autocomplete + set-select populate
// - decklist: parse -> resolve via /cards/collection -> preview -> POST
(function () {
  const AUTOCOMPLETE = "https://api.scryfall.com/cards/autocomplete";
  const SEARCH = "https://api.scryfall.com/cards/search";
  const COLLECTION = "https://api.scryfall.com/cards/collection";
  const CONDITION_OPTIONS = ["NM", "LP", "MP", "HP", "Damaged"];
  const PRINTING_OPTIONS = ["Normal", "Foil"];

  const modeButtons = document.querySelectorAll(".add-mode button");
  const panels = {
    single: document.getElementById("add-single"),
    decklist: document.getElementById("add-decklist"),
    csv: document.getElementById("add-csv"),
  };
  const panelEntries = Object.entries(panels);

  const nameInput = document.getElementById("add-name");
  const nameList = document.getElementById("add-name-suggest");
  const setSelect = document.getElementById("add-set");
  const setNameHidden = document.getElementById("add-set-name");
  const collectorInput = document.getElementById("add-collector");

  const dlText = document.getElementById("decklist-text");
  const dlResolveBtn = document.getElementById("decklist-resolve");
  const dlCommitBtn = document.getElementById("decklist-commit");
  const dlStatus = document.getElementById("decklist-status");
  const dlPreview = document.getElementById("decklist-preview");

  let acTimer = null;
  let lastAutocomplete = "";
  let lastSetsFor = "";
  let resolved = [];
  let previewRows = [];
  const today = new Date().toISOString().slice(0, 10);

  modeButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      modeButtons.forEach(candidate => {
        candidate.classList.toggle("active", candidate === btn);
      });
      panelEntries.forEach(([mode, panel]) => {
        if (panel) panel.style.display = mode === btn.dataset.mode ? "" : "none";
      });
    });
  });

  if (nameInput) {
    nameInput.addEventListener("input", () => {
      clearTimeout(acTimer);
      const query = nameInput.value.trim();
      if (query.length < 2 || query === lastAutocomplete) return;
      acTimer = setTimeout(() => {
        lastAutocomplete = query;
        fetchAutocomplete(query);
      }, 200);
    });
    nameInput.addEventListener("change", () => {
      populateSets(nameInput.value.trim());
    });
  }

  if (setSelect) {
    setSelect.addEventListener("change", () => {
      const option = setSelect.selectedOptions[0];
      if (!option) return;
      if (setNameHidden) setNameHidden.value = option.dataset.setName || "";
      if (collectorInput && !collectorInput.dataset.userEdited) {
        collectorInput.value = option.dataset.collector || "";
      }
    });
  }

  if (collectorInput) {
    collectorInput.addEventListener("input", () => {
      collectorInput.dataset.userEdited = "1";
    });
  }

  if (dlResolveBtn) dlResolveBtn.addEventListener("click", resolveDecklist);
  if (dlCommitBtn) dlCommitBtn.addEventListener("click", commitDecklist);

  if (dlText) {
    dlText.addEventListener("input", () => {
      if (resolved.length === 0 && !dlPreview.innerHTML) return;
      resolved = [];
      previewRows = [];
      dlCommitBtn.disabled = true;
      dlPreview.innerHTML = "";
      dlStatus.textContent = "";
    });
  }

  async function fetchAutocomplete(query) {
    try {
      const response = await fetch(`${AUTOCOMPLETE}?q=${encodeURIComponent(query)}`);
      if (!response.ok || !nameList) return;
      const { data = [] } = await response.json();
      nameList.replaceChildren(
        ...data.slice(0, 10).map(name => {
          const option = document.createElement("option");
          option.value = name;
          return option;
        }),
      );
    } catch (_) {
      // best effort
    }
  }

  async function populateSets(cardName) {
    if (!cardName || !setSelect || cardName === lastSetsFor) return;
    lastSetsFor = cardName;
    setSelect.replaceChildren(createOption("", "Loading prints..."));
    setSelect.disabled = true;

    try {
      const query = encodeURIComponent(`!"${cardName}"`);
      const response = await fetch(`${SEARCH}?q=${query}&unique=prints`);
      if (!response.ok) {
        setSelect.replaceChildren(createOption("", "Card not found"));
        return;
      }

      const { data = [] } = await response.json();
      if (data.length === 0) {
        setSelect.replaceChildren(createOption("", "No prints found"));
        return;
      }

      setSelect.replaceChildren(
        ...data.map(printing => {
          const code = (printing.set || "").toUpperCase();
          const label = `${code} - ${printing.set_name || ""} #${printing.collector_number || ""}`;
          const option = createOption(code, label);
          option.dataset.setName = printing.set_name || "";
          option.dataset.collector = printing.collector_number || "";
          return option;
        }),
      );
      setSelect.disabled = false;
      setSelect.dispatchEvent(new Event("change"));
    } catch (_) {
      setSelect.replaceChildren(createOption("", "Lookup failed"));
    }
  }

  function parseDeckLine(line) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("//") || trimmed.startsWith("#")) return null;
    if (/^(commander|sideboard|deck|maybeboard):?$/i.test(trimmed)) return null;

    const match = trimmed.match(
      /^(\d+)x?\s+(.+?)(?:\s+\(([A-Za-z0-9]+)\)(?:\s+(\d+[a-z]?))?)?\s*$/,
    );
    if (!match) return null;

    const quantity = parseInt(match[1], 10);
    if (!quantity) return null;

    return {
      qty: quantity,
      name: match[2].trim(),
      set: match[3] || null,
      cn: match[4] || null,
    };
  }

  async function resolveDecklist() {
    const parsed = (dlText.value || "")
      .split(/\r?\n/)
      .map(parseDeckLine)
      .filter(Boolean);

    resolved = [];
    previewRows = [];
    dlCommitBtn.disabled = true;
    dlPreview.innerHTML = "";

    if (parsed.length === 0) {
      dlStatus.textContent = "No cards parsed.";
      return;
    }

    dlStatus.textContent = `Resolving ${parsed.length} line(s)...`;

    const found = [];
    const notFound = [];

    for (let i = 0; i < parsed.length; i += 75) {
      const batch = parsed.slice(i, i + 75);
      const batchByKey = new Map(
        batch.map(item => [makeLookupKey(item.name, item.set), item]),
      );
      const identifiers = batch.map(item => {
        const identifier = { name: item.name };
        if (item.set) identifier.set = item.set.toLowerCase();
        if (item.cn) identifier.collector_number = item.cn;
        return identifier;
      });

      let payload;
      try {
        const response = await fetch(COLLECTION, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ identifiers }),
        });
        if (!response.ok) {
          dlStatus.textContent = `Scryfall error ${response.status}`;
          return;
        }
        payload = await response.json();
      } catch (error) {
        dlStatus.textContent = `Network error: ${error.message}`;
        return;
      }

      (payload.data || []).forEach(card => {
        const original = batchByKey.get(makeLookupKey(card.name, card.set));
        found.push({
          card_name: card.name,
          set_code: (card.set || "").toUpperCase(),
          set_name: card.set_name || "",
          card_number: card.collector_number || "",
          quantity: original ? original.qty : 1,
          condition: "NM",
          printing: "Normal",
          language: "English",
          price_bought: null,
          date_bought: null,
        });
      });
      (payload.not_found || []).forEach(item => notFound.push(item));
    }

    resolved = found;
    const parts = [`Resolved ${found.length} of ${parsed.length}.`];
    if (notFound.length) parts.push(`${notFound.length} not found.`);
    dlStatus.textContent = parts.join(" ");
    dlPreview.replaceChildren(...renderPreview(found, notFound));
    dlCommitBtn.disabled = found.length === 0;
  }

  function collectEdits() {
    return previewRows.map(row => {
      const base = resolved[parseInt(row.dataset.idx, 10)];
      const fields = row._fields;
      const { quantity, condition, printing, price_bought, date_bought } = fields;
      const qty = parseInt(quantity.value, 10);
      const price = parseFloat(price_bought.value);

      return {
        ...base,
        quantity: Number.isFinite(qty) && qty > 0 ? qty : 1,
        set_code: fields.set_code.value.trim().toUpperCase() || base.set_code,
        set_name:
          (fields.set_code.value.trim().toUpperCase() || base.set_code) === base.set_code
            ? base.set_name
            : "",
        condition: condition.value || base.condition,
        printing: printing.value || base.printing,
        price_bought: Number.isFinite(price) ? price : null,
        date_bought: date_bought.value || null,
      };
    });
  }

  async function commitDecklist() {
    if (resolved.length === 0) return;

    const records = collectEdits();
    dlCommitBtn.disabled = true;
    dlStatus.textContent = `Adding ${records.length} card(s)...`;

    try {
      const response = await fetch("/inventory/add-bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ records }),
      });
      if (!response.ok) {
        dlStatus.textContent = `Server error ${response.status}`;
        dlCommitBtn.disabled = false;
        return;
      }
      window.location.href = "/inventory";
    } catch (error) {
      dlStatus.textContent = `Network error: ${error.message}`;
      dlCommitBtn.disabled = false;
    }
  }

  function renderPreview(records, notFound) {
    const nodes = [];
    previewRows = [];

    if (records.length) {
      const table = document.createElement("table");
      table.className = "preview-table";
      table.innerHTML = "<thead><tr>"
        + "<th class=\"num\">Qty</th><th>Card</th><th>Set</th>"
        + "<th>Cond.</th><th>Print</th><th class=\"num\">Price $</th><th>Date</th>"
        + "</tr></thead>";

      const tbody = document.createElement("tbody");
      records.forEach((record, index) => {
        const row = buildPreviewRow(record, index);
        previewRows.push(row);
        tbody.appendChild(row);
      });
      table.appendChild(tbody);
      nodes.push(table);
    }

    if (notFound.length) {
      const missing = document.createElement("div");
      missing.className = "preview-missing";
      missing.innerHTML = "<strong>Not found:</strong>";

      const list = document.createElement("ul");
      notFound.forEach(item => {
        const entry = document.createElement("li");
        entry.textContent = item.name || JSON.stringify(item);
        list.appendChild(entry);
      });
      missing.appendChild(list);
      nodes.push(missing);
    }

    return nodes;
  }

  function buildPreviewRow(record, index) {
    const row = document.createElement("tr");
    row.dataset.idx = String(index);

    const quantityInput = createInput("number", "cell-qty");
    quantityInput.min = "1";
    quantityInput.max = "999";
    quantityInput.value = String(record.quantity);

    const conditionSelect = buildSelect("cell-cond", CONDITION_OPTIONS, record.condition);
    const printingSelect = buildSelect("cell-print", PRINTING_OPTIONS, record.printing);
    const setCodeInput = createInput("text", "cell-set-code");
    setCodeInput.value = record.set_code;
    setCodeInput.maxLength = 3;
    setCodeInput.addEventListener("input", () => {
      setCodeInput.value = setCodeInput.value
        .replace(/[^a-z0-9]/gi, "")
        .slice(0, 3)
        .toUpperCase();
    });

    const priceInput = createInput("number", "cell-price");
    priceInput.step = "0.01";
    priceInput.min = "0";

    const dateInput = createInput("date", "cell-date");
    dateInput.value = today;

    row.appendChild(createCell(quantityInput, "num"));
    row.appendChild(createTextCell(record.card_name));
    row.appendChild(createCell(setCodeInput));
    row.appendChild(createCell(conditionSelect));
    row.appendChild(createCell(printingSelect));
    row.appendChild(createCell(priceInput, "num"));
    row.appendChild(createCell(dateInput));

    row._fields = {
      quantity: quantityInput,
      set_code: setCodeInput,
      condition: conditionSelect,
      printing: printingSelect,
      price_bought: priceInput,
      date_bought: dateInput,
    };

    return row;
  }

  function buildSelect(className, options, selected) {
    const select = document.createElement("select");
    select.className = className;
    options.forEach(value => {
      const option = createOption(value, value);
      option.selected = value === selected;
      select.appendChild(option);
    });
    return select;
  }

  function createInput(type, className) {
    const input = document.createElement("input");
    input.type = type;
    input.className = className;
    return input;
  }

  function createTextCell(text) {
    const cell = document.createElement("td");
    cell.textContent = text;
    return cell;
  }

  function createCell(child, className) {
    const cell = document.createElement("td");
    if (className) cell.className = className;
    cell.appendChild(child);
    return cell;
  }

  function createOption(value, text) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = text;
    return option;
  }

  function makeLookupKey(name, set) {
    return `${(name || "").toLowerCase()}|${(set || "").toLowerCase()}`;
  }
})();
