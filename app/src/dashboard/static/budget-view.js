/* The editing screen.
 *
 * The overview answers "how am I doing"; this answers "what do I change".
 * Those are different questions and they want different layouts, which is
 * why they are different screens.
 *
 * Ma: rows are grouped and given room, and the detail that only matters for
 * one category at a time stays folded away until asked for. Everything
 * needed to make a decision about a category is one click deep, and nothing
 * else is on screen competing with it.
 */
import { get, patch, post } from "./api.js";
import { el, money, moneyEl, mount, parseMoney, svg } from "./dom.js";
import { spendingChart } from "./chart.js";

const state = {
  month: null, data: null, suggestions: null, groups: null,
  expanded: null, moveFrom: null, adding: false, error: "",
};

const KIND_WORDS = {
  fixed: "steady each month",
  variable: "varies month to month",
  sinking: "occasional — set aside monthly",
  unused: "nothing spent yet",
  insufficient: "not enough history yet",
};

function suggestionFor(id) {
  return (state.suggestions || []).find((s) => s.category_id === id) || null;
}

/* A thin bar showing how far through the expected amount this category is --
 * the same idea as the columns on the overview, at row scale.
 *
 * Drawn as SVG rather than a div whose width is set inline. The content
 * security policy has no 'unsafe-inline' in style-src, and rather than
 * depend on the exact line browsers draw between a style attribute and a
 * CSSOM assignment, this uses geometry attributes, which the policy does not
 * govern at all. */
function progress(spent, expected, budgeted) {
  const W = 100, H = 6;
  const ref = Math.max(expected || 0, budgeted || 0, spent || 0, 1);
  const overBudget = budgeted > 0 && spent > budgeted;
  const bar = svg("svg", { class: "meter", viewBox: `0 0 ${W} ${H}`,
                           preserveAspectRatio: "none", "aria-hidden": "true" },
    svg("rect", { class: "meter-track", x: 0, y: 0, width: W, height: H, rx: 3 }),
    svg("rect", { class: "meter-fill" + (overBudget ? " over" : ""),
                  x: 0, y: 0, rx: 3, height: H,
                  width: Math.max(0, Math.min(W, (spent / ref) * W)) }));
  // Where history expected this to land, so the fill has something to be
  // read against rather than being a bar in isolation.
  if (expected > 0 && expected < ref) {
    const at = (expected / ref) * W;
    bar.append(svg("rect", { class: "meter-mark", x: at - 0.6, y: -1,
                             width: 1.2, height: H + 2 }));
  }
  return bar;
}

/* ── header ─────────────────────────────────────────────────────────────── */
function header(onMonth, refresh) {
  const tbb = state.data.to_be_budgeted_cents;
  const tone = tbb === 0 ? "ok" : tbb > 0 ? "warn" : "danger";
  const says = tbb === 0 ? "Every pound has a job"
    : tbb > 0 ? "Waiting to be assigned" : "Assigned more than you have";
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
      el("button", { class: "btn", type: "button", text: "New category",
                     onclick: () => { state.adding = !state.adding; refresh(true); } })),
    el("div", { class: "node-body" },
      el("div", { class: "row" },
        moneyEl(tbb, "hero"),
        el("span", { class: `pill ${tone}`, text: says })),
      state.error ? el("div", { class: "error", text: state.error }) : null,
      state.adding ? addCategoryForm(refresh) : null));
}

/* ── adding a category ──────────────────────────────────────────────────── */
function addCategoryForm(refresh) {
  const name = el("input", { class: "input", type: "text",
                             placeholder: "Category name", "aria-label": "Name" });
  const group = el("select", { class: "input", "aria-label": "Group" });
  for (const g of state.groups || []) {
    if (g.is_income) continue;
    group.append(el("option", { value: g.id, text: g.name }));
  }
  const newGroup = el("input", { class: "input", type: "text",
                                 placeholder: "or a new group",
                                 "aria-label": "New group name" });
  const carry = el("input", { type: "checkbox", id: "carryneg" });

  async function submit() {
    state.error = "";
    const label = name.value.trim();
    if (!label) { state.error = "The category needs a name."; return refresh(true); }
    try {
      let groupId = group.value;
      const fresh = newGroup.value.trim();
      if (fresh) {
        const g = await post("/api/category-groups", { name: fresh });
        groupId = g.id;
      }
      if (!groupId) {
        state.error = "Choose a group, or name a new one.";
        return refresh(true);
      }
      await post("/api/categories", {
        name: label, group_id: groupId, carryover_negative: carry.checked });
      state.adding = false;
      // No further step: the classifier reads the category table directly, so
      // the next import can suggest this category immediately.
      refresh();
    } catch (err) {
      state.error = err.message || "Could not create that category.";
      refresh(true);
    }
  }

  return el("div", { class: "addform" },
    el("div", { class: "row wrap" },
      name, group, newGroup,
      el("label", { class: "check" }, carry,
        el("span", { text: "carry an overspend forward" })),
      el("button", { class: "btn primary", type: "button", text: "Add",
                     onclick: submit }),
      el("button", { class: "btn ghost", type: "button", text: "Cancel",
                     onclick: () => { state.adding = false; state.error = ""; refresh(true); } })),
    el("div", { class: "hint",
      text: "New categories are available to the importer's classifier straight away." }));
}

/* ── one category ───────────────────────────────────────────────────────── */
function categoryRow(cat, refresh) {
  const s = suggestionFor(cat.id);
  const spent = Math.max(0, -cat.activity_cents);
  const expected = s ? (s.suggested_cents ?? 0) : 0;
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

  const moving = state.moveFrom && state.moveFrom !== cat.id;
  const actions = el("div", { class: "row actions" },
    over
      ? el("button", { class: "btn", type: "button", text: "Cover",
          title: `Add ${money(-cat.balance_cents)} to clear the overspend`,
          onclick: async () => {
            await patch(`/api/edit/budget/${state.month}/${cat.id}`,
              { budgeted_cents: cat.budgeted_cents - cat.balance_cents });
            refresh();
          } })
      : null,
    el("button", {
      class: "btn ghost", type: "button",
      text: state.moveFrom === cat.id ? "Cancel" : moving ? "Move here" : "Move",
      onclick: async () => {
        if (state.moveFrom === cat.id) { state.moveFrom = null; return refresh(true); }
        if (!state.moveFrom) { state.moveFrom = cat.id; return refresh(true); }
        await moveMoney(state.moveFrom, cat.id, refresh);
      } }));

  const row = el("div", {
    class: `catrow${over ? " over" : ""}${expanded ? " expanded" : ""}`,
    draggable: "true", dataset: { id: cat.id },
  },
    el("button", {
      class: "btn ghost name", type: "button",
      "aria-expanded": expanded ? "true" : "false",
      onclick: () => { state.expanded = expanded ? null : cat.id; refresh(true); },
    }, el("span", { text: cat.name }),
       el("span", { class: "chev", text: expanded ? "▾" : "▸" })),
    el("div", { class: "meterwrap" },
      progress(spent, expected, cat.budgeted_cents),
      el("div", { class: "hint undermeter",
        text: `${money(spent)} spent`
            + (expected ? ` of about ${money(expected)} expected` : "")
            + (s && s.kind ? ` · ${KIND_WORDS[s.kind] || s.kind}` : "") })),
    amount,
    moneyEl(cat.balance_cents, "balance"),
    actions);

  row.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/plain", cat.id);
    row.classList.add("dragging");
  });
  row.addEventListener("dragend", () => row.classList.remove("dragging"));
  row.addEventListener("dragover", (e) => {
    e.preventDefault(); row.classList.add("drop-target");
  });
  row.addEventListener("dragleave", () => row.classList.remove("drop-target"));
  row.addEventListener("drop", async (e) => {
    e.preventDefault();
    row.classList.remove("drop-target");
    const from = e.dataTransfer.getData("text/plain");
    if (from && from !== cat.id) await moveMoney(from, cat.id, refresh);
  });

  return expanded ? el("div", {}, row, detail(cat, s, refresh)) : row;
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
    el("div", { class: "hint", text: "Loading…" }));

  Promise.all([
    get(`/api/view/history?month=${state.month}&category=${cat.id}&months=12`),
    get(`/api/view/transactions?category=${cat.id}&month=${state.month}&limit=12`),
  ]).then(([h, t]) => {
    const facts = el("div", { class: "facts" },
      fact("basis", h.basis || "—"),
      fact("covered months", String(h.sample_months)),
      fact("confidence", h.confidence),
      fact("typical", h.suggested_cents === null ? "—" : money(h.suggested_cents)),
      fact("range", h.low_cents === null ? "—"
        : `${money(h.low_cents)} – ${money(h.high_cents)}`));

    const accept = h.suggested_cents === null ? null : el("button", {
      class: "btn primary", type: "button",
      text: `Use ${money(h.suggested_cents)}`,
      onclick: async () => {
        await patch(`/api/edit/budget/${state.month}/${cat.id}`,
                    { budgeted_cents: h.suggested_cents });
        refresh();
      } });

    const rows = t.transactions.length
      ? t.transactions.map((x) => el("div", { class: "txn" },
          el("span", { class: "txn-date", text: x.date.slice(5) }),
          el("span", { class: "txn-payee grow", text: x.payee || "—" }),
          el("span", { class: "txn-acct hint", text: x.account }),
          moneyEl(x.amount_cents, "txn-amt")))
      : [el("div", { class: "hint", text: "Nothing in this category this month." })];

    mount(box,
      el("div", { class: "detail-grid" },
        el("div", {}, spendingChart(h),
          el("div", { class: "chart-legend" },
            key("actual", "spent"), key("budget", "budgeted"),
            key("suggested", "suggested"))),
        el("div", { class: "detail-side" }, facts,
          accept ? el("div", { class: "row" }, accept) : null,
          el("div", { class: "label txnhead", text: "this month" }),
          el("div", { class: "txns" }, ...rows))));
  }).catch(() => mount(box,
    el("div", { class: "hint", text: "Could not load the detail." })));

  return box;
}

function fact(label, value) {
  return el("div", { class: "factrow" },
    el("span", { class: "label", text: label }),
    el("span", { class: "factval", text: value }));
}

function key(cls, label) {
  return el("span", { class: "key" },
    el("span", { class: `swatch ${cls}` }), el("span", { text: label }));
}

/* ── the view ───────────────────────────────────────────────────────────── */
export async function budgetView(container, month, onMonth) {
  state.month = month;
  const [data, groups] = await Promise.all([
    get(`/api/view/budget?month=${encodeURIComponent(month)}`),
    get("/api/category-groups"),
  ]);
  state.data = data;
  state.groups = groups.groups;

  const refresh = (localOnly) => localOnly
    ? render()
    : budgetView(container, state.month, onMonth);

  function render() {
    // Grouped, because a flat list of twenty categories is a wall. Ma: the
    // group headings are the breathing space that makes it scannable.
    const byGroup = new Map();
    for (const c of state.data.categories) {
      if (!byGroup.has(c.group)) byGroup.set(c.group, []);
      byGroup.get(c.group).push(c);
    }
    const sections = [...byGroup].map(([name, cats]) =>
      el("div", { class: "catgroup" },
        el("div", { class: "catgroup-head" },
          el("span", { class: "label", text: name }),
          el("div", { class: "spacer" }),
          el("span", { class: "money hint",
            text: money(cats.reduce((n, c) => n + c.budgeted_cents, 0)) })),
        ...cats.map((c) => categoryRow(c, refresh))));

    const table = el("section", { class: "node", dataset: { kind: "budget" } },
      el("header", { class: "node-head" },
        el("span", { class: "node-title", text: "Categories" }),
        el("div", { class: "spacer" }),
        state.moveFrom
          ? el("span", { class: "pill brand", text: "choose where to move it" })
          : el("span", { class: "label", text: "select one for detail" })),
      el("div", { class: "node-body flush" }, ...sections));

    mount(container, header(onMonth, refresh), table);
  }

  render();
  get(`/api/view/suggestions?month=${encodeURIComponent(month)}`)
    .then((s) => { state.suggestions = s.suggestions; render(); })
    .catch(() => { /* the budget stands without them */ });
}
