/* Importing a statement.
 *
 * The shape of this screen is the point: upload, look, then commit. Nothing
 * reaches the ledger until the last step, because a misread column or a
 * mis-detected date format is cheap to catch here and expensive to find
 * months later in a budget that quietly does not add up.
 *
 * Duplicates and unparseable rows arrive already excluded. The safe default
 * is not to import; including one is a decision someone makes on purpose.
 */
import { del, get, patch, post } from "./api.js";
import { el, money, moneyEl, mount } from "./dom.js";

const state = {
  accounts: [], categories: [], coverage: null, pending: [],
  batch: null, rows: [], busy: false, error: "", note: "",
  addingAccount: false,
};

function fmtRange(a, b) {
  if (!a) return "no dated rows";
  return a === b ? a : `${a} to ${b}`;
}

/* ── coverage ───────────────────────────────────────────────────────────── */
/* Which months have a statement behind them. This exists because the budget
 * engine refuses to average over a month it does not have, so a gap is not a
 * cosmetic detail -- it is the difference between a recommendation and a
 * guess. Making gaps visible is the whole mitigation. */
function coverageStrip(cov) {
  if (!cov || !cov.covered.length) {
    return el("div", { class: "hint", text: "No statements imported yet." });
  }
  const have = new Map(cov.covered.map((c) => [c.month, c.txn_count]));
  const months = [];
  const first = cov.covered[0].month;
  const last = cov.covered[cov.covered.length - 1].month;
  let [y, m] = first.split("-").map(Number);
  while (`${y}-${String(m).padStart(2, "0")}` <= last) {
    months.push(`${y}-${String(m).padStart(2, "0")}`);
    if (++m === 13) { m = 1; y++; }
  }
  return el("div", {},
    el("div", { class: "covstrip" },
      ...months.map((mo) => el("span", {
        class: "covcell" + (have.has(mo) ? "" : " gap"),
        title: have.has(mo) ? `${mo}: ${have.get(mo)} transactions`
                            : `${mo}: no statement`,
      }, el("span", { class: "covmonth", text: mo.slice(2) })))),
    cov.gaps.length
      ? el("div", { class: "hint warnline",
          text: `${cov.gaps.length} month${cov.gaps.length === 1 ? "" : "s"} `
              + `with no statement: ${cov.gaps.join(", ")}. `
              + "The engine will not average over them." })
      : el("div", { class: "hint", text: "No gaps in the imported range." }));
}

/* ── review ─────────────────────────────────────────────────────────────── */
function reviewRow(row, refresh) {
  const excluded = Boolean(row.excluded);
  const box = el("input", { type: "checkbox",
                            "aria-label": `Import line ${row.line_no}` });
  box.checked = !excluded;
  box.addEventListener("change", async () => {
    await patch(`/api/import/row/${row.id}`, { excluded: !box.checked });
    refresh(true);
  });

  const cat = el("select", { class: "input catsel",
                             "aria-label": "Category" });
  cat.append(el("option", { value: "", text: "— uncategorised —" }));
  for (const c of state.categories) {
    const o = el("option", { value: c.id, text: `${c.group_name} · ${c.name}` });
    if (c.id === row.category_id) o.selected = true;
    cat.append(o);
  }
  cat.addEventListener("change", async () => {
    await patch(`/api/import/row/${row.id}`, { category_id: cat.value || null });
    refresh(true);
  });

  const flags = [];
  if (row.error) flags.push(el("span", { class: "pill danger", text: "unreadable" }));
  else if (row.is_duplicate) {
    flags.push(el("span", { class: `pill ${row.dup_kind === "exact" ? "warn" : "info"}`,
                            text: row.dup_kind === "exact" ? "already imported"
                                                           : "looks like a duplicate" }));
  }

  return el("div", { class: "improw" + (excluded ? " off" : "") },
    box,
    el("span", { class: "impdate", text: row.date || "—" }),
    el("span", { class: "imppayee grow", text: row.payee || "—" },
      row.error ? el("div", { class: "hint err", text: row.error }) : null),
    ...flags,
    cat,
    moneyEl(row.amount_cents === null ? 0 : row.amount_cents, "impamt"));
}

/* ── the view ───────────────────────────────────────────────────────────── */
export async function importView(container, { onDone } = {}) {
  const [accts, cats, cov, batches] = await Promise.all([
    get("/api/accounts"), get("/api/categories"), get("/api/view/coverage"),
    get("/api/import/batches"),
  ]);
  // On-budget accounts first: a statement almost always belongs to one, and
  // defaulting to a tracking account is a mistake nobody would notice until
  // the spending failed to show up in any envelope.
  state.accounts = [...accts.accounts].sort(
    (a, b) => (b.on_budget ? 1 : 0) - (a.on_budget ? 1 : 0));
  state.categories = cats.categories;
  state.coverage = cov;
  state.pending = (batches.batches || []).filter((b) => b.state === "review");

  // A batch left in review is not lost. Uploading and then navigating away is
  // an ordinary thing to do, and finding the half-finished import gone --
  // with nothing imported and no explanation -- would be worse than a prompt.
  if (!state.batch && state.pending.length) {
    const d = await get(`/api/import/batch/${state.pending[0].id}`);
    if (d.rows.length) {
      state.batch = d.batch;
      state.rows = d.rows;
      state.note = `Resumed ${d.batch.filename}, still waiting to be imported`;
    }
  }

  const refresh = async (rowsOnly) => {
    if (state.batch && rowsOnly) {
      const d = await get(`/api/import/batch/${state.batch.id}`);
      state.batch = d.batch;
      state.rows = d.rows;
      return render();
    }
    return importView(container, { onDone });
  };

  const accountSel = el("select", { class: "input", "aria-label": "Account" });
  for (const a of state.accounts) {
    accountSel.append(el("option", { value: a.id,
      text: a.on_budget ? a.name : `${a.name} (off budget)` }));
  }

  /* Somewhere to make the first one. The endpoint has always existed and
   * nothing called it, so an empty ledger left this screen with an empty
   * dropdown and no way forward -- a dead end rather than a first step. */
  const newName = el("input", {
    class: "input", type: "text", id: "acct-name",
    placeholder: "e.g. UFCU Checking", "aria-label": "New account name" });
  const newType = el("select", { class: "input", "aria-label": "Account type" },
    ...[["checking", "Checking"], ["savings", "Savings"],
        ["credit", "Credit card"], ["cash", "Cash"],
        ["investment", "Investment (off budget)"]].map(([v, t]) =>
      el("option", { value: v, text: t })));

  async function addAccount() {
    const name = newName.value.trim();
    if (!name) { state.error = "Give the account a name."; return render(); }
    state.busy = true; state.error = "";
    try {
      // Investments are tracked, not budgeted: money in one is not money you
      // are about to spend, and counting it as such makes every envelope lie.
      await post("/api/accounts", {
        name, type: newType.value,
        on_budget: newType.value !== "investment",
      });
      state.note = `Added ${name}.`;
      // Close it: adding one is usually the whole errand, and a form left
      // open reads as though it did not work.
      state.addingAccount = false;
    } catch (e) {
      state.error = (e && e.message) || "Could not add that account.";
    }
    state.busy = false;
    return importView(container, { onDone });
  }

  // Functions, not constants. Built once, these captured the state as it was
  // when the view loaded, so toggling flipped a flag that nothing ever read
  // again -- which is exactly why the button appeared to do nothing. The
  // inputs stay outside so text typed into them survives a re-render.
  //
  // With no accounts the form is the only way forward, so it is simply there.
  // With accounts it is an occasional errand and goes behind a button, out of
  // the way of the thing you came to do.
  function firstOne() { return state.accounts.length === 0; }

  function addRow() {
    if (!(firstOne() || state.addingAccount)) return null;
    return el("div", { class: "addacct" },
      el("div", { class: "label",
        text: firstOne() ? "Add an account" : "Add another account" }),
      firstOne() ? el("div", { class: "hint",
        text: "A statement belongs to an account, so there has to be one "
            + "before anything can be imported." }) : null,
      el("div", { class: "row wrap" }, newName, newType,
        el("button", { class: "btn primary", type: "button", text: "Add",
                       onclick: addAccount })));
  }

  async function categoriseAll() {
    state.busy = true; state.error = ""; state.note = "";
    render();
    try {
      const r = await post("/api/categorize", { use_model: true });
      if (r.rows === 0) {
        state.note = "Nothing left to categorise.";
      } else {
        // Say what it cost, not only what it did: the point of grouping by
        // merchant is that a hundred rows is not a hundred questions.
        const bits = [`${r.changed} of ${r.rows} rows`,
                      `${r.merchants} distinct merchants`];
        if (r.history) bits.push(`${r.history} from what you set before`);
        if (r.similar) bits.push(`${r.similar} by resemblance`);
        if (r.model) {
          bits.push(`${r.model} asked in ${r.model_calls} request`
                    + (r.model_calls === 1 ? "" : "s"));
        }
        if (r.unresolved) {
          // Say which of the two reasons it is: a missing key and a
          // switch left off need different things done about them.
          const why = !r.model_allowed
            ? " \u2014 model categorising is off in Settings"
            : !r.model_available ? " \u2014 no API key set" : "";
          bits.push(`${r.unresolved} still unmatched` + why);
        }
        state.note = bits.join(" \u00b7 ");
      }
    } catch (e) {
      state.error = (e && e.message) || "Could not categorise.";
    }
    state.busy = false;
    return importView(container, { onDone });
  }

  function addToggle() {
    if (firstOne()) return null;
    return el("button", {
      class: "btn", type: "button",
      text: state.addingAccount ? "Cancel" : "Add another account",
      onclick: () => { state.addingAccount = !state.addingAccount; render(); },
    });
  }
  const file = el("input", { type: "file", class: "input",
                             accept: ".csv,.ofx,.qfx,.txt,text/csv",
                             "aria-label": "Statement file" });

  async function doUpload() {
    if (!file.files || !file.files[0]) {
      state.error = "Choose a statement file first.";
      return render();
    }
    if (!accountSel.value) {
      state.error = "Choose which account it belongs to.";
      return render();
    }
    state.busy = true; state.error = ""; state.note = "";
    render();
    try {
      const f = file.files[0];
      const res = await post(
        `/api/import/upload?account=${accountSel.value}`
        + `&filename=${encodeURIComponent(f.name)}`, await f.arrayBuffer());
      const d = await get(`/api/import/batch/${res.batch_id}`);
      state.batch = d.batch;
      state.rows = d.rows;
      state.note = `${d.batch.rows_total} rows read`
        + (d.batch.rows_duplicate ? `, ${d.batch.rows_duplicate} already imported` : "");
    } catch (err) {
      state.error = err.message || "That file could not be read.";
    }
    state.busy = false;
    render();
  }

  function render() {
    const uploader = el("section", { class: "node", dataset: { kind: "budget" } },
      el("header", { class: "node-head" },
        el("span", { class: "node-title", text: "Import a statement" }),
        el("div", { class: "spacer" }),
        el("span", { class: "label", text: "CSV, OFX or QFX" })),
      el("div", { class: "node-body" },
        state.error ? el("div", { class: "error", text: state.error }) : null,
        state.note ? el("div", { class: "pill ok", text: state.note }) : null,
        state.accounts.length
          ? el("div", { class: "row wrap uploadrow" },
              accountSel, file,
              el("button", { class: "btn primary", type: "button",
                             text: state.busy ? "Reading…" : "Read file",
                             onclick: doUpload }),
              // Pushed to the far end, away from the thing you came to do.
              el("button", { class: "btn", type: "button",
                text: state.busy ? "Working\u2026" : "Categorise everything",
                onclick: categoriseAll }),
              el("div", { class: "spacer" }), addToggle())
          : null,
        addRow(),
        state.accounts.length
          ? el("div", { class: "hint",
              text: "Nothing is written until you commit. Duplicates and rows "
                  + "that could not be read start excluded." })
          : null));

    const coverageCard = el("section", { class: "node", dataset: { kind: "budget" } },
      el("header", { class: "node-head" },
        el("span", { class: "node-title", text: "Months covered" })),
      el("div", { class: "node-body" }, coverageStrip(state.coverage)));

    const parts = [uploader];

    if (state.batch && state.rows.length) {
      const included = state.rows.filter((r) => !r.excluded).length;
      parts.push(el("section", { class: "node", dataset: { kind: "budget" } },
        el("header", { class: "node-head" },
          el("span", { class: "node-title", text: state.batch.filename }),
          el("div", { class: "spacer" }),
          el("span", { class: "label",
            text: fmtRange(state.batch.period_start, state.batch.period_end) })),
        el("div", { class: "node-body flush" },
          el("div", { class: "implist" },
            ...state.rows.map((r) => reviewRow(r, refresh)))),
        el("div", { class: "node-body" },
          el("div", { class: "row wrap" },
            el("button", { class: "btn primary", type: "button",
              text: `Import ${included} row${included === 1 ? "" : "s"}`,
              onclick: async () => {
                state.busy = true; render();
                try {
                  const res = await post(`/api/import/batch/${state.batch.id}`, {});
                  state.note = `${res.imported} imported`;
                  state.batch = null; state.rows = [];
                  if (onDone) onDone();
                } catch (err) {
                  state.error = err.message || "Could not import.";
                }
                state.busy = false;
                await refresh();
              } }),
            el("button", { class: "btn ghost", type: "button", text: "Discard",
              onclick: async () => {
                await del(`/api/import/batch/${state.batch.id}`);
                state.batch = null; state.rows = [];
                await refresh();
              } })))));
    }

    parts.push(coverageCard);
    mount(container, ...parts);
  }

  render();
}
