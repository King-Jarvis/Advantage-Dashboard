/* The budget: assign and cover, move money, and see what history suggests.
 *
 * "To be budgeted" anchors the screen because it is the one number that says
 * whether you are finished. Everything else is in service of driving it to
 * zero.
 */
import { get, patch, post } from "./api.js";
import { clear, el, money, moneyEl, mount, parseMoney } from "./dom.js";
import { spendingChart } from "./chart.js";

const state = {
  month: null, data: null, suggestions: null,
  expanded: null, moveFrom: null,
};

function toneFor(cents) {
  if (cents === 0) return { cls: "ok", says: "Every pound has a job" };
  if (cents > 0) return { cls: "warn", says: "Waiting to be assigned" };
  return { cls: "danger", says: "Assigned more than you have" };
}

function suggestionFor(id) {
  if (!state.suggestions) return null;
  return state.suggestions.find((s) => s.category_id === id) || null;
}

/* ── header ─────────────────────────────────────────────────────────────── */
function header(onMonth) {
  const tbb = state.data.to_be_budgeted_cents;
  const tone = toneFor(tbb);
  const shift = (delta) => {
    const [y, m] = state.month.split("-").map(Number);
    const d = new Date(y, m - 1 + delta, 1);
    onMonth(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`);
  };

  return el("section", { class: "node", dataset: { kind: "budget" } },
    el("header", { class: "node-head" },
      el("button", { class: "btn ghost", type: "button", text: "‹",
                     "aria-label": "Previous month", onclick: () => shift(-1) }),
      el("span", { class: "node-title", text: state.month }),
      el("button", { class: "btn ghost", type: "button", text: "›",
                     "aria-label": "Next month", onclick: () => shift(1) }),
      el("div", { class: "spacer" }),
      el("span", { class: "label", text: "to be budgeted" })),
    el("div", { class: "node-body" },
      el("div", { class: "row" },
        moneyEl(tbb, "hero"),
        el("span", { class: `pill ${tone.cls}`, text: tone.says }))));
}

/* ── one category ───────────────────────────────────────────────────────── */
function categoryRow(cat, refresh) {
  const suggestion = suggestionFor(cat.id);
  const over = cat.balance_cents < 0;
  const expanded = state.expanded === cat.id;

  const amount = el("input", {
    class: "input amount", type: "text", inputmode: "decimal",
    value: money(cat.budgeted_cents),
    "aria-label": `Budgeted for ${cat.name}`,
  });

  async function commitAmount() {
    const cents = parseMoney(amount.value);
    if (cents === null || cents === cat.budgeted_cents) {
      amount.value = money(cat.budgeted_cents);
      return;
    }
    await patch(`/api/edit/budget/${state.month}/${cat.id}`,
                { budgeted_cents: cents });
    refresh();
  }
  amount.addEventListener("blur", commitAmount);
  amount.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); amount.blur(); }
    if (e.key === "Escape") { amount.value = money(cat.budgeted_cents); amount.blur(); }
  });

  // Cover: the single tap that fixes an underfunded category.
  const cover = over
    ? el("button", {
        class: "btn", type: "button", text: "Cover",
        title: `Add ${money(-cat.balance_cents)} to clear the overspend`,
        onclick: async () => {
          await patch(`/api/edit/budget/${state.month}/${cat.id}`,
                      { budgeted_cents: cat.budgeted_cents - cat.balance_cents });
          refresh();
        },
      })
    : null;

  const moving = state.moveFrom && state.moveFrom !== cat.id;
  const moveBtn = el("button", {
    class: "btn ghost", type: "button",
    text: state.moveFrom === cat.id ? "Cancel" : moving ? "Move here" : "Move",
    "aria-label": moving ? `Move money into ${cat.name}`
                         : `Move money out of ${cat.name}`,
    onclick: async () => {
      if (state.moveFrom === cat.id) { state.moveFrom = null; refresh(true); return; }
      if (!state.moveFrom) { state.moveFrom = cat.id; refresh(true); return; }
      await moveMoney(state.moveFrom, cat.id, refresh);
    },
  });

  const row = el("div", {
    class: `item cat${over ? " over" : ""}${expanded ? " expanded" : ""}`,
    draggable: "true",
    dataset: { id: cat.id },
  },
    el("span", { class: `status-dot ${over ? "danger" : "ok"}` }),
    el("button", {
      class: "btn ghost name", type: "button",
      "aria-expanded": expanded ? "true" : "false",
      onclick: () => { state.expanded = expanded ? null : cat.id; refresh(true); },
    }, el("span", { text: cat.name })),
    el("div", { class: "grow" },
      suggestion && suggestion.suggested_cents !== null
        ? el("span", { class: "hint",
            text: `suggests ${money(suggestion.suggested_cents)} · ${suggestion.confidence}` })
        : el("span", { class: "hint", text: cat.group })),
    amount,
    el("span", { class: "money muted activity", text: money(cat.activity_cents) }),
    moneyEl(cat.balance_cents, "balance"),
    cover,
    moveBtn);

  // Drag is an accelerator, never the only route: everything it does is also
  // reachable through the Move button above, which works by keyboard.
  row.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/plain", cat.id);
    e.dataTransfer.effectAllowed = "move";
    row.classList.add("dragging");
  });
  row.addEventListener("dragend", () => row.classList.remove("dragging"));
  row.addEventListener("dragover", (e) => {
    e.preventDefault();
    row.classList.add("drop-target");
  });
  row.addEventListener("dragleave", () => row.classList.remove("drop-target"));
  row.addEventListener("drop", async (e) => {
    e.preventDefault();
    row.classList.remove("drop-target");
    const from = e.dataTransfer.getData("text/plain");
    if (from && from !== cat.id) await moveMoney(from, cat.id, refresh);
  });

  return expanded ? el("div", {}, row, detail(cat, suggestion, refresh)) : row;
}

async function moveMoney(from, to, refresh) {
  const raw = window.prompt("How much to move?");
  if (raw === null) return;
  const cents = parseMoney(raw);
  if (cents === null || cents <= 0) return;
  await post(`/api/edit/budget/${state.month}/move`,
             { from_category: from, to_category: to, cents });
  state.moveFrom = null;
  refresh();
}

/* ── the expanded panel ─────────────────────────────────────────────────── */
function detail(cat, suggestion, refresh) {
  const box = el("div", { class: "detail" },
    el("div", { class: "hint", text: "Loading history…" }));

  get(`/api/view/history?month=${state.month}&category=${cat.id}&months=12`)
    .then((h) => {
      const legend = el("div", { class: "chart-legend" },
        el("span", { class: "key" },
          el("span", { class: "swatch actual" }), el("span", { text: "spent" })),
        el("span", { class: "key" },
          el("span", { class: "swatch budget" }), el("span", { text: "budgeted" })),
        el("span", { class: "key" },
          el("span", { class: "swatch suggested" }),
          el("span", { text: "suggested" })));

      const parts = [spendingChart(h), legend];

      if (h.suggested_cents === null) {
        parts.push(el("div", { class: "hint basis", text: h.basis }));
      } else {
        const accept = el("button", {
          class: "btn primary", type: "button",
          text: `Use ${money(h.suggested_cents)}`,
          onclick: async () => {
            await patch(`/api/edit/budget/${state.month}/${cat.id}`,
                        { budgeted_cents: h.suggested_cents });
            refresh();
          },
        });
        parts.push(el("div", { class: "row detail-actions" },
          accept,
          el("span", { class: "hint basis",
            text: `${h.basis} · ${h.sample_months} covered months · ${h.confidence} confidence` })));
      }
      mount(box, ...parts);
    })
    .catch(() => mount(box, el("div", { class: "hint", text: "Could not load history." })));

  return box;
}

/* ── the view ───────────────────────────────────────────────────────────── */
export async function budgetView(container, month, onMonth) {
  state.month = month;
  state.data = await get(`/api/view/budget?month=${encodeURIComponent(month)}`);

  const cols = el("div", { class: "item head" },
    el("span", { class: "status-dot" }),
    el("span", { class: "btn ghost name label", text: "category" }),
    el("div", { class: "grow" }),
    el("span", { class: "label amount", text: "budgeted" }),
    el("span", { class: "label activity", text: "activity" }),
    el("span", { class: "label balance", text: "balance" }));

  const refresh = (localOnly) => {
    if (localOnly) return render();
    return budgetView(container, state.month, onMonth);
  };

  function render() {
    const list = el("div", { class: "list" }, cols,
      ...state.data.categories.map((c) => categoryRow(c, refresh)));
    const table = el("section", { class: "node", dataset: { kind: "budget" } },
      el("header", { class: "node-head" },
        el("span", { class: "node-title", text: "Categories" }),
        el("div", { class: "spacer" }),
        state.moveFrom
          ? el("span", { class: "pill brand", text: "choose where to move it" })
          : null),
      el("div", { class: "node-body flush" }, list));
    mount(container, header(onMonth), table);
  }

  render();

  // Suggestions arrive separately: they are slower, and the budget must be
  // usable whether or not they are available.
  get(`/api/view/suggestions?month=${encodeURIComponent(month)}`)
    .then((s) => { state.suggestions = s.suggestions; render(); })
    .catch(() => { /* the budget stands on its own without them */ });
}
