(function () {
  const AUTOCOMPLETE = "https://api.scryfall.com/cards/autocomplete";
  const input = document.querySelector('#panel-single input[name="q"]');
  const list = document.getElementById("search-name-suggest");
  if (!input || !list) return;

  let timer = null;
  let lastQuery = "";

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const query = input.value.trim();
    if (query.length < 2 || query === lastQuery) return;
    timer = setTimeout(() => {
      lastQuery = query;
      fetchSuggestions(query);
    }, 200);
  });

  async function fetchSuggestions(query) {
    try {
      const response = await fetch(`${AUTOCOMPLETE}?q=${encodeURIComponent(query)}`);
      if (!response.ok) return;
      const { data = [] } = await response.json();
      list.replaceChildren(
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
})();
