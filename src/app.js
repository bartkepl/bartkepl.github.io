"use strict";

(() => {
  const root = document.documentElement;
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /* ---------------------------------------------------------------- theme */

  const toggle = document.querySelector("[data-theme-toggle]");
  const prefersLight = matchMedia("(prefers-color-scheme: light)");
  const themeColor = document.querySelector('meta[name="theme-color"]');
  const currentTheme = () => root.dataset.theme || (prefersLight.matches ? "light" : "dark");

  function paintTheme() {
    const theme = currentTheme();
    toggle.dataset.mode = theme;
    toggle.setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
    themeColor.content = getComputedStyle(root).getPropertyValue("--bg").trim();
  }

  if (toggle) {
    toggle.hidden = false;
    toggle.addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try {
        localStorage.setItem("theme", next);
      } catch (e) {
        /* the choice just won't persist */
      }
      paintTheme();
    });
    prefersLight.addEventListener("change", paintTheme);
    paintTheme();
  }

  /* -------------------------------------------------------------- readout */

  // Types the SCPI queries once on load; replies appear whole, as from an instrument.
  async function playReadout(readout) {
    const lines = Array.from(readout.querySelectorAll(".line"));
    readout.classList.add("is-playing");
    if (reducedMotion.matches) return;

    const typed = lines.map((line) => line.querySelector("[data-type]"));
    const texts = typed.map((el) => (el ? el.textContent : ""));
    const render = (el, text, count) => {
      const pending = document.createElement("span");
      pending.className = "pending";
      pending.textContent = text.slice(count);
      el.replaceChildren(text.slice(0, count), pending);
    };

    lines.forEach((line, i) => {
      line.classList.add("is-hidden");
      if (typed[i]) render(typed[i], texts[i], 0);
    });
    await sleep(450);

    for (const [i, line] of lines.entries()) {
      line.classList.remove("is-hidden");
      if (typed[i]) {
        for (let count = 1; count <= texts[i].length; count++) {
          await sleep(70);
          render(typed[i], texts[i], count);
        }
        typed[i].textContent = texts[i];
        await sleep(280);
      } else if (line.classList.contains("line-reply")) {
        await sleep(520);
      }
    }
  }

  const readout = document.querySelector("[data-readout]");
  if (readout) playReadout(readout);

  /* ------------------------------------------------------- relative dates */

  const relativeFormat = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  const units = [
    ["year", 31536000],
    ["month", 2592000],
    ["week", 604800],
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];

  function relative(date) {
    const seconds = (date.getTime() - Date.now()) / 1000;
    const [unit, size] = units.find(([, size]) => Math.abs(seconds) >= size) || units[units.length - 1];
    return relativeFormat.format(Math.round(seconds / size), unit);
  }

  for (const time of document.querySelectorAll("time[data-relative]")) {
    const date = new Date(time.dateTime);
    if (Number.isNaN(date.getTime())) continue;
    time.title = date.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });
    time.textContent = `Updated ${relative(date)}`;
  }

  /* ------------------------------------------------------------ filtering */

  const grid = document.querySelector("[data-grid]");
  if (!grid) return;

  const cards = Array.from(grid.children);
  const radios = Array.from(document.querySelectorAll('input[name="category"]'));
  const search = document.querySelector("[data-search]");
  const sort = document.querySelector("[data-sort]");
  const empty = document.querySelector("[data-empty]");
  const status = document.querySelector("[data-status]");
  const projects = document.getElementById("projects");
  const categories = new Set(radios.map((radio) => radio.value));
  const state = { category: "all", query: "", sort: "updated" };

  const compare = {
    updated: (a, b) => b.dataset.pinned - a.dataset.pinned || b.dataset.pushed.localeCompare(a.dataset.pushed),
    created: (a, b) => b.dataset.created.localeCompare(a.dataset.created),
    name: (a, b) => a.dataset.name.localeCompare(b.dataset.name, "en", { numeric: true, sensitivity: "base" }),
  };

  function apply() {
    const words = state.query.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    for (const card of cards) {
      const visible =
        (state.category === "all" || card.dataset.category === state.category) &&
        words.every((word) => card.dataset.search.includes(word));
      card.hidden = !visible;
      if (visible) shown++;
    }
    grid.append(...cards.slice().sort(compare[state.sort]));
    empty.hidden = shown > 0;
    status.textContent = shown === cards.length ? "" : `${shown} of ${cards.length} projects shown`;
  }

  function update(change, animate) {
    change();
    if (animate && document.startViewTransition && !reducedMotion.matches) {
      document.startViewTransition(apply);
    } else {
      apply();
    }
  }

  function setCategory(value) {
    state.category = categories.has(value) ? value : "all";
    for (const radio of radios) radio.checked = radio.value === state.category;
    const url = state.category === "all" ? location.pathname + location.search : `#${state.category}`;
    history.replaceState(null, "", url);
  }

  for (const radio of radios) {
    radio.addEventListener("change", () => update(() => setCategory(radio.value), true));
  }

  search.addEventListener("input", () => update(() => (state.query = search.value.trim()), false));

  sort.addEventListener("change", () => update(() => (state.sort = sort.value), true));

  document.querySelector("[data-reset]").addEventListener("click", () => {
    search.value = "";
    update(() => {
      state.query = "";
      setCategory("all");
    }, true);
    search.focus();
  });

  for (const link of document.querySelectorAll("[data-category-link]")) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      update(() => setCategory(link.dataset.categoryLink), false);
      projects.scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth" });
    });
  }

  window.addEventListener("hashchange", () => {
    const hash = location.hash.slice(1);
    if (categories.has(hash)) update(() => setCategory(hash), true);
  });

  const initial = location.hash.slice(1);
  if (categories.has(initial) && initial !== "all") {
    setCategory(initial);
    projects.scrollIntoView();
  }
  apply();
})();
