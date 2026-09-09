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

/* Kept across renders so toggling the projection does not re-fetch, and so
 * the choice survives paging between months. */
let projecting = false;

export async function overviewView(container, month, { onMonth, onEdit }) {
  const d = await get(`/api/view/overview?month=${encodeURIComponent(month)}`);
  const sv = d.savings || {};

  /* What is in the bank, and where the month ends up if the budget holds.
   *
   * This screen is for reading; "to be budgeted" is a control for assigning
   * money and belongs where the assigning happens, so it lives on the edit
   * screen now. What you want to know from here is whether you are keeping
   * any of it. */
  const shown = projecting ? (sv.projected_cents ?? 0) : (sv.now_cents ?? 0);
  const savingsTone = shown < 0 ? "danger" : shown > 0 ? "ok" : "warn";

  const toggle = el("button", {
    class: "btn ghost", type: "button",
    "aria-pressed": projecting ? "true" : "false",
    text: projecting ? "Showing end of month" : "Project end of month",
    onclick: () => {
      projecting = !projecting;
      return overviewView(container, month, { onMonth, onEdit });
    },
  });

  const workings = projecting
    ? el("div", { class: "hint" },
        `${money(sv.now_cents ?? 0)} now `
        + `+ ${money(sv.income_incoming_cents ?? 0)} still to come `
        + `− ${money(sv.outstanding_budget_cents ?? 0)} budget left to spend`
        + (sv.confident ? "" : " · too little history to trust the income"))
    : el("div", { class: "hint" },
        `${money(sv.income_received_cents ?? 0)} in this month so far`);

  // A balance built only from imported rows is a fact about the import, not
  // about the account. The figure looks equally confident either way, so the
  // gap has to be stated rather than left to be discovered.
  const needsOpening = (sv.accounts_without_opening || 0) > 0
    ? el("div", { class: "hint warnline" },
        `${sv.accounts_without_opening} account`
        + `${sv.accounts_without_opening === 1 ? " has" : "s have"} no starting `
        + "balance, so this counts only what has been imported. Set it on the "
        + "Import screen.")
    : null;

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
        stat(projecting ? "saved by month end" : "in the bank now",
          el("div", { class: "row" },
            moneyEl(shown, "hero"),
            el("span", { class: `pill ${savingsTone}`,
                         text: shown < 0 ? "short" : "unspent" }),
            toggle)),
        stat("budgeted", el("span", { class: "money big",
                                      text: money(d.budgeted_total_cents) })),
        stat("spent so far", el("span", { class: "money big",
                                          text: money(d.actual_total_cents) })),
        stat("estimated", el("span", { class: "money big muted",
                                       text: money(d.estimate_total_cents) })),
        stat("recommended", el("span", { class: "money big muted",
                                         text: money(d.recommended_total_cents) }))),
      workings,
      needsOpening));

  const graph = el("section", { class: "node", dataset: { kind: "budget" } },
    el("header", { class: "node-head" },
      el("span", { class: "node-title", text: "By category" }),
      el("div", { class: "spacer" }),
      el("span", { class: "label",
                   text: "most spent this month first · select one to edit" })),
    el("div", { class: "node-body" }, chart, legend));

  mount(container, summary, graph);
}
