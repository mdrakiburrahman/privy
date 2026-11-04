window.PrivyHycoFacets = (() => {
  "use strict";
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const columnsKey = "privy.hycos.columns";
  const unknown = new Set(["unknown", "none"]);
  // facet name -> Map(lowercase value -> value as shown)
  const selected = new Map();
  let loaded = false, pop = null, opener = null;

  const make = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const button = (text, cls) => { const node = make("button", text, cls); node.type = "button"; return node; };
  const facets = () => $$(".facet-pill[data-facet]").map(pill => ({ name: pill.dataset.facet, label: pill.dataset.label }));
  const rowValues = row => { try { return JSON.parse(row.dataset.facets || "{}"); } catch { return {}; } };
  const order = (a, b) => (unknown.has(a) ? 1 : 0) - (unknown.has(b) ? 1 : 0) ||
    a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });

  function load() {
    if (loaded) return;
    loaded = true;
    const params = new URLSearchParams(location.search);
    for (const { name } of facets()) {
      const values = params.getAll(name).map(value => value.trim()).filter(Boolean);
      if (values.length) selected.set(name, new Map(values.map(value => [value.toLowerCase(), value])));
    }
  }
  function matches(row, except) {
    load();
    const values = rowValues(row);
    for (const [name, wanted] of selected) {
      if (name === except) continue;
      if (!(values[name] || ["Unknown"]).some(value => wanted.has(value.toLowerCase()))) return false;
    }
    return true;
  }
  function columnChoices() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(columnsKey) || "{}") || {}; } catch {}
    return $$("#hycocolumns [data-col]").map(node => ({
      key: node.dataset.col,
      label: node.dataset.label,
      shown: Object.hasOwn(saved, node.dataset.col) ? Boolean(saved[node.dataset.col]) : node.dataset.default === "1",
    }));
  }
  function showColumns() {
    const table = $("#hycos");
    if (!table) return;
    const choices = columnChoices();
    for (const { key, shown } of choices) $$(".col-" + key, table).forEach(cell => { cell.hidden = !shown; });
    const pill = $("#hycocols");
    if (pill) pill.textContent = `Columns ${choices.filter(c => c.shown).length}/${choices.length}`;
  }
  function render() {
    for (const pill of $$(".facet-pill[data-facet]")) {
      const values = [...(selected.get(pill.dataset.facet) || new Map()).values()];
      pill.classList.toggle("on", values.length > 0);
      $(".v", pill).textContent = values.length === 1 ? ": " + values[0] : values.length ? `: ${values.length} selected` : "";
    }
    for (const pill of $$(".facet-pill")) pill.setAttribute("aria-expanded", String(isOpen() && pill === opener));
    const chips = $("#hycochips");
    if (chips) {
      const nodes = [];
      for (const { name, label } of facets()) {
        for (const value of (selected.get(name) || new Map()).values()) {
          const chip = button(undefined, "chip facet-chip");
          chip.dataset.facet = name;
          chip.dataset.value = value;
          chip.setAttribute("aria-label", `Remove filter ${label}: ${value}`);
          chip.append(make("b", label + ":"), " " + value);
          nodes.push(chip);
        }
      }
      if (nodes.length) { const clear = button("Clear all", "facet-clear"); clear.dataset.facetClear = ""; nodes.push(clear); }
      chips.replaceChildren(...nodes);
      chips.hidden = !nodes.length;
    }
    showColumns();
  }
  function sync(url) {
    load();
    for (const { name } of facets()) {
      url.searchParams.delete(name);
      for (const value of (selected.get(name) || new Map()).values()) url.searchParams.append(name, value);
    }
    render();
  }

  function isOpen() { return Boolean(pop && !pop.hidden); }
  function position() {
    if (!isOpen() || !opener) return;
    const rect = opener.getBoundingClientRect(), width = pop.offsetWidth, height = pop.offsetHeight;
    pop.style.left = Math.max(12, Math.min(rect.left, innerWidth - width - 12)) + "px";
    const below = rect.bottom + 6, above = rect.top - height - 6;
    pop.style.top = (below + height > innerHeight - 12 && above > 12 ? above : below) + "px";
  }
  function fillColumns() {
    pop.setAttribute("aria-label", "Choose columns");
    const head = make("header");
    const reset = button("Defaults");
    reset.dataset.columnsReset = "";
    head.append(make("b", "Columns"), reset);
    const list = make("div", undefined, "facet-list");
    for (const { key, label, shown } of columnChoices()) {
      const row = make("label"), box = make("input");
      box.type = "checkbox"; box.checked = shown; box.dataset.col = key;
      row.append(box, make("span", label));
      list.append(row);
    }
    pop.replaceChildren(head, list, make("footer", "Remembered in this browser."));
  }
  function fillFacet() {
    const name = opener.dataset.facet, label = opener.dataset.label;
    pop.setAttribute("aria-label", "Filter by " + label);
    const counts = new Map(), shown = new Map();
    for (const row of $$("#hycos tbody tr")) {
      const counted = row.dataset.base === "1" && matches(row, name);
      for (const value of rowValues(row)[name] || ["Unknown"]) {
        const key = value.toLowerCase();
        if (!shown.has(key)) { shown.set(key, value); counts.set(key, 0); }
        if (counted) counts.set(key, counts.get(key) + 1);
      }
    }
    const chosen = selected.get(name) || new Map();
    for (const [key, value] of chosen) if (!shown.has(key)) { shown.set(key, value); counts.set(key, 0); }
    const head = make("header");
    head.append(make("b", label));
    if (chosen.size) { const none = button("Clear"); none.dataset.facetNone = ""; head.append(none); }
    const parts = [head];
    if (shown.size > 8) {
      const find = make("input");
      find.type = "search"; find.placeholder = "Find a value…"; find.autocomplete = "off";
      find.setAttribute("aria-label", "Find a " + label + " value");
      parts.push(find);
    }
    const list = make("div", undefined, "facet-list");
    for (const key of [...shown.keys()].sort(order)) {
      const row = make("label", undefined, counts.get(key) ? "" : "zero"), box = make("input");
      box.type = "checkbox"; box.value = key; box.dataset.display = shown.get(key); box.checked = chosen.has(key);
      row.dataset.find = key;
      row.append(box, make("span", shown.get(key)), make("small", String(counts.get(key))));
      list.append(row);
    }
    parts.push(list, make("footer", "Pick any values. Counts follow the other filters."));
    pop.replaceChildren(...parts);
  }
  function open(pill) {
    if (isOpen() && opener === pill) { close(true); return; }
    if (!pop) {
      pop = make("div", undefined, "facet-pop");
      pop.id = "facet-pop";
      pop.hidden = true;
      pop.setAttribute("role", "dialog");
      document.body.append(pop);
    }
    opener = pill;
    if (pill.id === "hycocols") fillColumns(); else fillFacet();
    pop.hidden = false;
    position();
    render();
    $("input", pop)?.focus();
  }
  function close(focus) {
    if (!isOpen()) return;
    pop.hidden = true;
    const back = opener;
    opener = null;
    render();
    if (focus && back && document.contains(back)) back.focus();
  }
  function changed() {
    api.onChange();
    if (isOpen() && opener.dataset.facet) {
      const active = document.activeElement;
      const focused = pop.contains(active) && active.type === "checkbox" ? active.value : null;
      const find = $("input[type=search]", pop)?.value || "";
      fillFacet();
      const box = $("input[type=search]", pop);
      if (box) { box.value = find; narrow(box); }
      if (focused !== null) $$("input[type=checkbox]", pop).find(node => node.value === focused)?.focus();
    }
  }
  function narrow(box) {
    const text = box.value.trim().toLowerCase();
    $$(".facet-list label", pop).forEach(row => { row.hidden = Boolean(text) && !row.dataset.find.includes(text); });
  }

  document.addEventListener("click", event => {
    const pill = event.target.closest(".facet-pill");
    if (pill) { event.preventDefault(); open(pill); return; }
    const chip = event.target.closest(".facet-chip");
    if (chip) {
      const chosen = selected.get(chip.dataset.facet);
      chosen?.delete(chip.dataset.value.toLowerCase());
      if (chosen && !chosen.size) selected.delete(chip.dataset.facet);
      changed();
      return;
    }
    if (event.target.closest("[data-facet-clear]")) { selected.clear(); close(false); changed(); return; }
    if (isOpen() && event.target.closest("[data-facet-none]")) { selected.delete(opener.dataset.facet); changed(); return; }
    if (isOpen() && event.target.closest("[data-columns-reset]")) {
      try { localStorage.removeItem(columnsKey); } catch {}
      fillColumns(); showColumns();
      return;
    }
    if (isOpen() && !pop.contains(event.target)) {
      // The first click outside only closes the popover; it does not also open a row.
      event.stopImmediatePropagation();
      close(false);
    }
  });
  document.addEventListener("change", event => {
    const box = event.target;
    if (!isOpen() || !pop.contains(box) || box.type !== "checkbox") return;
    if (box.dataset.col) {
      const saved = Object.fromEntries(columnChoices().map(c => [c.key, c.shown]));
      saved[box.dataset.col] = box.checked;
      try { localStorage.setItem(columnsKey, JSON.stringify(saved)); } catch {}
      showColumns();
      return;
    }
    const name = opener.dataset.facet, chosen = selected.get(name) || new Map();
    if (box.checked) chosen.set(box.value, box.dataset.display); else chosen.delete(box.value);
    if (chosen.size) selected.set(name, chosen); else selected.delete(name);
    changed();
  });
  document.addEventListener("input", event => {
    if (isOpen() && pop.contains(event.target) && event.target.type === "search") narrow(event.target);
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && isOpen()) { event.preventDefault(); close(true); }
  });
  document.addEventListener("focusin", event => {
    if (isOpen() && !pop.contains(event.target) && event.target !== opener) close(false);
  });
  window.addEventListener("resize", position);
  document.addEventListener("scroll", event => { if (!pop?.contains(event.target)) position(); }, true);

  const api = { matches, sync, isOpen, names: () => facets().map(f => f.name), onChange() {} };
  return api;
})();
