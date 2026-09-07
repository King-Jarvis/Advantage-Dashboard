/* Putting things where they belong.
 *
 * Account, then group, then category, then the rows. That order is not
 * arbitrary -- it is the order in which a thing turns out to be wrong. "That
 * whole file went to the wrong account", "those belong under Bills", "that
 * one is not groceries": each level answers a different mistake.
 *
 * A change re-reads the tree rather than moving the row in place. Moving it
 * locally would be faster and would eventually disagree with the database
 * about where things are, which on a screen whose entire job is showing where
 * things are is the one bug worth spending a round trip to avoid.
 *
 * Deleting is behind a toggle. It is rare, it is the only irreversible thing
 * here, and a delete control on four hundred rows is four hundred chances to
 * hit the wrong one.
 */
import { api, get, patch } from "./api.js";
import { el, money, moneyEl, mount } from "./dom.js";

const state = {
  month: null, tree: [], accounts: [], categories: [],
  deleting: false, busy: false, error: "", note: "",
};

function categoryOptions(selected) {
  const byGroup = new Map();
  for (const c of state.categories) {
    if (!byGroup.has(c.group_name)) byGroup.set(c.group_name, []);
    byGroup.get(c.group_name).push(c);
  }
  const opts = [el("option", { value: "", text: "— no category —" })];
  for (const [gname, cats] of byGroup) {
    // optgroup so the group a category belongs to is visible while choosing,
    // which is half of what makes a wrong one obvious.
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

function row(r, reload) {
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

  return el("div", { class: "lrow" },
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
    el("span", { class: "lrow-payee grow", text: r.payee || "—" }),
    r.is_split_part ? el("span", { class: "pill", text: "part of a split" }) : null,
    moneyEl(r.amount_cents, "lrow-amt"),
    cat, acct);
}

export async function ledgerView(container, { month, onBack } = {}) {
  state.month = month || state.month;

  async function reload() {
    const q = state.month ? `?month=${encodeURIComponent(state.month)}` : "";
    const d = await get(`/api/view/ledger${q}`);
    state.tree = d.tree || [];
    state.accounts = d.accounts || [];
    state.categories = d.categories || [];
    render();
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

    const body = [];
    if (state.error) body.push(el("div", { class: "error", text: state.error }));
    if (state.note) body.push(el("div", { class: "pill ok", text: state.note }));
    if (state.deleting) {
      body.push(el("div", { class: "hint warn-note",
        text: "Deleting is permanent and only affects this dashboard, not "
            + "your bank. If a whole file went in wrong, undo the import "
            + "instead." }));
    }

    if (!state.tree.length) {
      body.push(el("div", { class: "hint pad", text: "Nothing in this month." }));
    }
    for (const a of state.tree) {
      const groups = a.groups.map((g) => el("div", { class: "lgroup" },
        el("div", { class: "lgroup-name", text: g.name }),
        ...g.categories.map((c) => el("div", { class: "lcat" },
          el("div", { class: "lcat-name" },
            el("span", { text: c.name }),
            el("span", { class: "hint",
              text: ` · ${c.rows.length} row${c.rows.length === 1 ? "" : "s"}` })),
          ...c.rows.map((r) => row(r, reload))))));
      body.push(el("div", { class: "lacct" },
        el("div", { class: "lacct-name", text: a.name }), ...groups));
    }

    mount(container, el("section", { class: "node", dataset: { kind: "budget" } },
      head, el("div", { class: "node-body" }, ...body)));
  }

  render();
  await reload();
}
