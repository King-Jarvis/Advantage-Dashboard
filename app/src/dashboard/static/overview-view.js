/* The main budget screen: read at a glance, edit elsewhere.
 *
 * Deliberately not a table. A table of twelve categories asks you to compare
 * numbers in your head; the chart puts the comparison on the page. Editing
 * lives behind it, because reading and editing want different layouts and
 * doing both in one screen usually means doing both badly.
 */
import { get } from "./api.js";
import { el, money, moneyEl, mount } from "./dom.js";
import { budgetChart } from "./budget-chart.js";

function toneFor(cents) {
  if (cents === 0) return { cls: "ok", says: "Every pound has a job" };
  if (cents > 0) return { cls: "warn", says: "Waiting to be assigned" };
  return { cls: "danger", says: "Assigned more than you have" };
}

function stat(label, node) {
  return el("div", { class: "stat" },
    el("span", { class: "stat-label", text: label }), node);
}

function monthNav(month, onMonth) {
  const shift = (delta) => {
    const [y, m] = month.split("-").map(Number);
    const d = new Date(y, m - 1 + delta, 1);
    onMonth(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`);
  };
  return [
    el("button", { class: "btn ghost", type: "button", text: "‹",
                   "aria-label": "Previous month", onclick: () => shift(-1) }),
    el("span", { class: "node-title", text: month }),
    el("button", { class: "btn ghost", type: "button", text: "›",
                   "aria-label": "Next month", onclick: () => shift(1) }),
  ];
}

export async function overviewView(container, month, { onMonth, onEdit }) {
  const d = await get(`/api/view/overview?month=${encodeURIComponent(month)}`);
  const tone = toneFor(d.to_be_budgeted_cents);

  const legend = el("div", { class: "legend" },
    el("span", { class: "key" },
      el("span", { class: "swatch act" }),
      el("span", { text: "spent so far" })),
    el("span", { class: "key" },
      el("span", { class: "swatch est" }),
      el("span", { text: "estimated for the month" })),
    el("span", { class: "key" },
      el("span", { class: "swatch bud" }), el("span", { text: "your budget" })),
    el("span", { class: "key" },
      el("span", { class: "swatch rec" }), el("span", { text: "recommended" })),
    el("div", { class: "spacer" }),
    d.short_categories
      ? el("span", { class: "pill warn",
          text: `${d.short_categories} budgeted under estimate` })
      : el("span", { class: "pill ok", text: "all covered" }));

  const chart = budgetChart(d.items, { onPick: (item) => onEdit(item.id) });

  const summary = el("section", { class: "node", dataset: { kind: "budget" } },
    el("header", { class: "node-head" },
      ...monthNav(month, onMonth),
      el("div", { class: "spacer" }),
      el("button", { class: "btn primary", type: "button",
                     text: "Edit budget", onclick: () => onEdit(null) })),
    el("div", { class: "node-body" },
      el("div", { class: "stat-row" },
        stat("to be budgeted",
          el("div", { class: "row" },
            moneyEl(d.to_be_budgeted_cents, "hero"),
            el("span", { class: `pill ${tone.cls}`, text: tone.says }))),
        stat("budgeted", el("span", { class: "money big",
                                      text: money(d.budgeted_total_cents) })),
        stat("spent so far", el("span", { class: "money big",
                                          text: money(d.actual_total_cents) })),
        stat("estimated", el("span", { class: "money big muted",
                                       text: money(d.estimate_total_cents) })),
        stat("recommended", el("span", { class: "money big muted",
                                         text: money(d.recommended_total_cents) })))));

  const graph = el("section", { class: "node", dataset: { kind: "budget" } },
    el("header", { class: "node-head" },
      el("span", { class: "node-title", text: "By category" }),
      el("div", { class: "spacer" }),
      el("span", { class: "label",
                   text: "most spent this month first · select one to edit" })),
    el("div", { class: "node-body" }, chart, legend));

  mount(container, summary, graph);
}
