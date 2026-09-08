/* Putting things where they belong.
 *
 * Account, then group, then category, then the rows. That order is not
 * arbitrary -- it is the order in which a thing turns out to be wrong. "That
 * whole file went to the wrong account", "those belong under Bills", "that
 * one is not groceries": each level answers a different mistake.
 *
 * Every level folds. Four hundred rows opened flat is not a hierarchy, it is
 * a list with headings in it, and the point of the arrangement is to be able
 * to ignore most of it.
 *
 * A change re-reads the tree rather than moving the row in place. Moving it
 * locally would be faster and would eventually disagree with the database
 * about where things are, which on a screen whose whole job is showing where
 * things are is the one bug worth a round trip to avoid.
 */
import { api, get, patch } from "./api.js";
import { el, keepingPlace, money, moneyEl, mount } from "./dom.js";

const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"];

const FILTERS = [
  { id: "all", label: "Everything", test: () => true },
  { id: "unfiled", label: "No category", test: (r) => !r.category_id },
  { id: "transfers", label: "Transfers", test: (r) => r.is_transfer },
  { id: "in", label: "Money in", test: (r) => r.amount_cents > 0 },
  { id: "out", label: "Money out", test: (r) => r.amount_cents < 0 },
];

const state = {
  month: null, tree: [], accounts: [], categories: [],
  deleting: false, error: "", note: "",
  query: "", filter: "all",
  // Which nodes are open, by path. A Set rather than a flag on the node,
  // because the tree is rebuilt on every change and anything stored on it
  // would be lost exactly when the screen re-drew.
  open: new Set(),
  detail: null, detailFor: null,
};

function monthLabel(m) {
  if (!m) return "Every month";
  const [y, mo] = m.split("-");
  return `${MONTHS[Number(mo) - 1]} ${y}`;
}

function shiftMonth(m, by) {
  const [y, mo] = m.split("-").map(Number);
  const d = new Date(y, mo - 1 + by, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function matches(r) {
  const f = FILTERS.find((x) => x.id === state.filter) || FILTERS[0];
  if (!f.test(r)) return false;
  const q = state.query.trim().toLowerCase();
  if (!q) return true;
  // Amount as well as payee: "13.30" is how you look for a charge you can see
  // on a statement but cannot name.
  return (r.payee || "").toLowerCase().includes(q)
      || money(r.amount_cents).toLowerCase().includes(q)
      || r.date.includes(q);
}

function categoryOptions(selected) {
  const byGroup = new Map();
  for (const c of state.categories) {
    if (!byGroup.has(c.group_name)) byGroup.set(c.group_name, []);
    byGroup.get(c.group_name).push(c);
  }
  const opts = [el("option", { value: "", text: "— no category —" })];
  for (const [gname, cats] of byGroup) {
    const og = el("optgroup", { label: gname });
    for (const c of cats) {
      const o = el("option", { value: c.id,
        text: c.is_income ? `${c.name} (income)` : c.name });
      if (c.id === selected) o.selected = true;
      og.append(o);
    }
    opts.push(og);
  }
  return opts;
}

function accountOptions(selected) {
  return state.accounts.map((a) => {
    const o = el("option", { value: a.id,
      text: a.on_budget ? a.name : `${a.name} (off budget)` });
    if (a.id === selected) o.selected = true;
    return o;
  });
}

function row(r, reload, rerender) {
  const cat = el("select", { class: "input", "aria-label": "Category" },
    ...categoryOptions(r.category_id));
  const acct = el("select", { class: "input", "aria-label": "Account" },
    ...accountOptions(r.account_id));

  async function change(body) {
    state.error = "";
    try {
      await patch(`/api/edit/transaction/${r.id}`, body);
    } catch (e) {
      state.error = (e && e.message) || "Could not move that.";
    }
    return reload();
  }
  cat.addEventListener("change", () => change({ category_id: cat.value || null }));
  acct.addEventListener("change", () => change({ account_id: acct.value }));

  if (r.is_transfer) {
    // A transfer carries no category by definition, and moving one half
    // between accounts would put both halves in the same place.
    cat.disabled = true;
    acct.disabled = true;
  }

  const open = state.detailFor === r.id;

  return el("div", { class: `lrow${open ? " open" : ""}` },
    state.deleting
      ? el("button", { class: "iconbtn danger", type: "button", text: "🗑",
          "aria-label": `Delete ${r.payee || "this transaction"}`,
          onclick: async () => {
            if (!window.confirm(
              `Delete this?\n\n${r.date}  ${r.payee || "(no payee)"}  `
              + `${money(r.amount_cents)}\n\nIt will no longer count towards `
              + "any balance or budget. Undoing the whole import is usually "
              + "the better answer if a file went in wrong.")) return;
            try {
              await api("DELETE", `/api/edit/transaction/${r.id}`);
              state.note = "Deleted.";
            } catch (e) {
              state.error = (e && e.message) || "Could not delete that.";
            }
            return reload();
          } })
      : null,
    el("span", { class: "lrow-date", text: r.date.slice(5) }),
    // The payee is the button. The list can only show the bank's truncated
    // NAME, so "what actually was this?" needs somewhere to be answered.
    el("button", {
      class: "btn ghost lrow-payee grow", type: "button",
      "aria-expanded": open ? "true" : "false",
      title: r.payee || "",
      text: r.payee || "—",
      onclick: () => showDetail(open ? null : r.id, rerender),
    }),
    r.is_split_part ? el("span", { class: "pill", text: "part of a split" }) : null,
    moneyEl(r.amount_cents, "lrow-amt"),
    cat, acct,
    open ? detailPanel() : null);
}

/* What the bank actually sent.
 *
 * Fetched per row rather than carried in the tree: the tree is fifteen
 * hundred rows and this is wanted for one of them at a time. */
async function showDetail(id, rerender) {
  state.detailFor = id;
  state.detail = null;
  // rerender, not reload: reload re-fetches the whole tree, and opening one
  // row is not a reason to ask the server for fifteen hundred others.
  rerender();
  if (!id) return;
  try {
    state.detail = await get(`/api/view/transaction/${id}`);
  } catch (e) {
    state.error = (e && e.message) || "Could not load that charge.";
  }
  rerender();
}

function fact(label, value) {
  if (value === null || value === undefined || value === "") return null;
  return el("div", { class: "lfact" },
    el("span", { class: "lfact-k", text: label }),
    el("span", { class: "lfact-v", text: String(value) }));
}

function detailPanel() {
  const d = state.detail;
  if (!d) return el("div", { class: "ldetail" },
    el("span", { class: "hint", text: "Loading…" }));

  const bits = [];

  // The full title first, because it is the reason this screen exists. Only
  // when it says more than the row already does.
  if (d.description && d.description !== d.payee) {
    bits.push(el("div", { class: "ldetail-title" },
      el("span", { class: "lfact-k", text: "as the bank sent it" }),
      el("div", { class: "ldetail-full", text: d.description })));
  }

  bits.push(el("div", { class: "lfacts" },
    fact("date", d.date),
    fact("amount", money(d.amount_cents)),
    fact("account", d.account + (d.on_budget ? "" : " (off budget)")),
    fact("category", d.category
      ? `${d.category}${d.group ? ` · ${d.group}` : ""}` : "none"),
    fact("filed by", { you: "you", model: "the model",
                       history: "how it was filed before",
                       similar: "resemblance to another merchant"
                     }[d.category_source] || (d.category ? "before this was recorded" : "")),
    fact("matched as", d.payee_norm),
    fact("cleared", d.reconciled ? "reconciled" : d.cleared ? "yes" : "no")));

  if (d.transfer_with) {
    bits.push(el("div", { class: "hint",
      text: `Half of a transfer with ${d.transfer_with.account} on `
          + `${d.transfer_with.date} (${money(d.transfer_with.amount_cents)}).` }));
  }
  if (d.split_of) {
    bits.push(el("div", { class: "hint",
      text: `Part of a split of ${money(d.split_of.amount_cents)} on `
          + `${d.split_of.date}.` }));
  }
  if (d.split_parts) {
    bits.push(el("div", { class: "hint",
      text: `Split into ${d.split_parts.length} parts: `
          + d.split_parts.map((x) =>
              `${x.category || "uncategorised"} ${money(x.amount_cents)}`)
            .join(", ") }));
  }

  if (d.import) {
    bits.push(el("details", { class: "legend" },
      el("summary", { text: `From ${d.import.filename}, line ${d.import.line_no}` }),
      el("div", { class: "ldetail-raw", text: d.import.raw || "(not kept)" })));
  } else {
    bits.push(el("div", { class: "hint",
      text: d.source === "manual" ? "Entered by hand." : "No statement line kept." }));
  }

  return el("div", { class: "ldetail" }, ...bits);
}

/* A folding level. Searching forces it open: a hit buried inside something
 * collapsed is the same as no hit at all. */
function fold(path, title, count, total, kids, cls) {
  const searching = Boolean(state.query.trim());
  const node = el("details", { class: cls });
  if (searching || state.open.has(path)) node.setAttribute("open", "");
  node.append(
    el("summary", {},
      el("span", { class: "foldname", text: title }),
      el("span", { class: "hint",
        text: ` · ${count} row${count === 1 ? "" : "s"}` }),
      moneyEl(total, "foldtotal")),
    ...kids);
  node.addEventListener("toggle", () => {
    if (node.open) state.open.add(path);
    else state.open.delete(path);
  });
  return node;
}

export async function ledgerView(container, { month, onBack } = {}) {
  if (state.month === null && month) state.month = month;

  // Re-filing a row rebuilds the tree, and the row you are working on is
  // usually a long way down it.
  async function reload() {
    return keepingPlace(async () => {
      const q = state.month ? `?month=${encodeURIComponent(state.month)}` : "";
      const d = await get(`/api/view/ledger${q}`);
      state.tree = d.tree || [];
      state.accounts = d.accounts || [];
      state.categories = d.categories || [];
      render();
    });
  }

  function setMonth(m) {
    state.month = m;
    state.note = state.error = "";
    return reload();
  }

  function controls() {
    const search = el("input", {
      class: "input", type: "search", value: state.query,
      placeholder: "Find a payee, amount or date…",
      "aria-label": "Search transactions",
    });
    // Filtering as you type, because the alternative is typing, pressing a
    // button, and forgetting which of the two you changed.
    search.addEventListener("input", () => { state.query = search.value; render(); });

    return el("div", { class: "lcontrols" },
      el("div", { class: "row wrap" },
        el("button", { class: "btn ghost", type: "button", text: "‹",
          "aria-label": "Previous month",
          disabled: !state.month || null,
          onclick: () => setMonth(shiftMonth(state.month, -1)) }),
        el("span", { class: "lmonth", text: monthLabel(state.month) }),
        el("button", { class: "btn ghost", type: "button", text: "›",
          "aria-label": "Next month",
          disabled: !state.month || null,
          onclick: () => setMonth(shiftMonth(state.month, 1)) }),
        el("button", { class: "btn", type: "button",
          text: state.month ? "Every month" : "This month only",
          onclick: () => setMonth(state.month ? null
            : new Date().toISOString().slice(0, 7)) })),
      search,
      el("div", { class: "chips" }, ...FILTERS.map((f) => el("button", {
        type: "button",
        class: "chip" + (f.id === state.filter ? " on" : ""),
        "aria-pressed": f.id === state.filter ? "true" : "false",
        onclick: () => { state.filter = f.id; render(); },
      }, el("span", { text: f.label })))));
  }

  function render() {
    const head = el("header", { class: "node-head" },
      el("button", { class: "btn ghost", type: "button", text: "‹ Back",
                     onclick: onBack }),
      el("span", { class: "node-title", text: "Where things are filed" }),
      el("div", { class: "spacer" }),
      el("label", { class: "check" },
        el("input", { type: "checkbox", class: "check",
          checked: state.deleting || null,
          onchange: () => { state.deleting = !state.deleting; render(); } }),
        el("span", { text: "Allow deleting" })));

    const body = [controls()];
    if (state.error) body.push(el("div", { class: "error", text: state.error }));
    if (state.note) body.push(el("div", { class: "pill ok", text: state.note }));
    if (state.deleting) {
      body.push(el("div", { class: "hint warn-note",
        text: "Deleting is permanent and only affects this dashboard, not "
            + "your bank. If a whole file went in wrong, undo the import "
            + "instead." }));
    }

    let shown = 0;
    for (const a of state.tree) {
      const groupNodes = [];
      let acctCount = 0, acctTotal = 0;
      for (const g of a.groups) {
        const catNodes = [];
        let grpCount = 0, grpTotal = 0;
        for (const c of g.categories) {
          const rows = c.rows.filter(matches);
          if (!rows.length) continue;
          const total = rows.reduce((n, r) => n + r.amount_cents, 0);
          grpCount += rows.length;
          grpTotal += total;
          catNodes.push(fold(`${a.id}/${g.id}/${c.id}`, c.name, rows.length,
                             total, rows.map((r) => row(r, reload, render)), "lcat"));
        }
        if (!catNodes.length) continue;
        acctCount += grpCount;
        acctTotal += grpTotal;
        groupNodes.push(fold(`${a.id}/${g.id}`, g.name, grpCount, grpTotal,
                             catNodes, "lgroup"));
      }
      if (!groupNodes.length) continue;
      shown += acctCount;
      body.push(fold(a.id, a.name, acctCount, acctTotal, groupNodes, "lacct"));
    }

    if (!shown) {
      body.push(el("div", { class: "hint pad",
        text: state.query || state.filter !== "all"
          ? "Nothing matches that."
          : "Nothing in this month." }));
    }

    mount(container, el("section", { class: "node", dataset: { kind: "budget" } },
      head, el("div", { class: "node-body" }, ...body)));
  }

  render();
  await reload();
}
