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
import { api, del, get, patch, post } from "./api.js";
import { el, keepingPlace, money, moneyEl, mount, parseMoney }
  from "./dom.js";

const state = {
  accounts: [], categories: [], coverage: null, pending: [],
  batch: null, rows: [], busy: false, error: "", note: "",
  addingAccount: false,
  unfiled: [], allCats: [], unfiledBand: "in", showFiled: false,
  tracking: [], transfers: [], candidates: [], history: [],
  nearMisses: [], unpaired: [], outside: [], exclusions: [],
  scanned: false, scanning: false,
  historyOpen: false, transfersOpen: null, lastAccount: null,
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

/* What still needs filing, grouped by merchant.
 *
 * Grouped because that is how the work divides: twelve payroll deposits are
 * one decision. A flat list of a hundred and twenty-three rows invites you to
 * make it a hundred and twenty-three times.
 *
 * Money in is the default band. Nothing arriving counts towards a budget
 * until it is filed as income, so those rows are the ones actually blocking
 * anything -- and there are usually a handful of them against a hundred
 * outgoings.
 *
 * Filing a merchant also teaches the classifier, so the next statement
 * recognises it without asking anyone.
 */
const BANDS = [
  { id: "in", label: "Money in", test: (g) => g.total_cents > 0 },
  { id: "out", label: "Money out", test: (g) => g.total_cents <= 0 },
  { id: "all", label: "All", test: () => true },
];

function unfiledPanel(refresh) {
  const band = BANDS.find((b) => b.id === state.unfiledBand) || BANDS[0];
  const shown = state.unfiled.filter(band.test);

  const chips = el("div", { class: "chips" },
    ...BANDS.map((b) => el("button", {
      type: "button",
      class: "chip" + (b.id === state.unfiledBand ? " on" : ""),
      "aria-pressed": b.id === state.unfiledBand ? "true" : "false",
      onclick: () => { state.unfiledBand = b.id; return refresh(); },
    }, el("span", { text: b.label }),
       el("span", { class: "chip-n",
                    text: String(state.unfiled.filter(b.test).length) }))),
    el("div", { class: "spacer" }),
    // Already-filed merchants, so a wrong decision can be changed rather than
    // lived with. Off by default: they are not work waiting.
    el("label", { class: "check showfiled" },
      el("input", {
        type: "checkbox", class: "check", checked: state.showFiled || null,
        onchange: () => { state.showFiled = !state.showFiled; return refresh(); },
      }),
      el("span", { text: "Show ones already filed" })));

  if (!shown.length) {
    return el("div", {}, chips,
      el("div", { class: "hint pad",
        text: state.showFiled
          ? "Nothing filed under that heading yet."
          : "Nothing left to file here." }));
  }

  const rows = shown.map((g) => {
    const sel = el("select", { class: "input",
      "aria-label": `Category for ${g.example}` },
      el("option", { value: "",
        text: g.category ? `${g.category}${g.mixed ? " (mixed)" : ""}`
                         : "Choose a category…" }),
      ...state.allCats.map((c) => el("option", {
        value: `cat:${c.id}`,
        text: c.is_income ? `${c.name} (income)` : c.name })),
      // From a statement row, "spent at Venmo" and "moved to Venmo" look
      // identical. Only you know which, so both are offered in one list.
      ...state.tracking.map((a) => el("option", {
        value: `acct:${a.id}`, text: `→ moved to ${a.name}` })));
    sel.addEventListener("change", async () => {
      if (!sel.value) return;
      sel.disabled = true;
      try {
        const [kind, id] = sel.value.split(":");
        const r = await post("/api/edit/by-payee", {
          payee_key: g.key,
          ...(kind === "acct" ? { account_id: id } : { category_id: id }),
          // Only when looking at things already filed, and only then: a
          // backlog sweep must never rewrite a decision made by hand.
          overwrite: Boolean(state.showFiled),
        });
        state.note = r.as === "transfer"
          ? `Recorded ${r.filed} row${r.filed === 1 ? "" : "s"} as a transfer.`
          : `Filed ${r.filed} row${r.filed === 1 ? "" : "s"}.`;
      } catch (e) {
        state.error = (e && e.message) || "Could not file those.";
      }
      return refresh();
    });
    return el("div", { class: "unfiled-row" },
      el("div", { class: "grow" },
        el("div", { class: "unfiled-payee", text: g.example || "(no payee)" }),
        el("div", { class: "hint",
          text: `${g.count} row${g.count === 1 ? "" : "s"} \u00b7 latest ${g.latest}`
              + (g.category ? ` \u00b7 now ${g.category}` : "") })),
      moneyEl(g.total_cents, "unfiled-amt"),
      sel);
  });

  return el("div", { class: "unfiled" }, chips,
    el("div", { class: "hint",
      text: "Biggest first. Choosing a category files every row for that "
          + "merchant, and teaches the classifier for next time." }),
    ...rows);
}

/* Movements between accounts.
 *
 * A transfer is not spending and not income, so it appears nowhere in the
 * budget -- which means without a screen of its own it is invisible, and
 * sixteen thousand pounds of it looks like nothing happening.
 *
 * Suggestions are separated from facts. A pair the matcher spotted is a guess
 * that two amounts belong together; a linked transfer is something you said.
 */
async function scanTransfers(refresh) {
  state.scanning = true; state.error = ""; state.note = "";
  refresh(true);
  try {
    const r = await get("/api/view/transfers?wide=1");
    state.candidates = r.candidates || [];
    state.transfers = r.transfers || [];
    state.nearMisses = r.near_misses || [];
    state.unpaired = r.unpaired || [];
    state.outside = r.outside || [];
    state.exclusions = r.exclusions || [];
    state.scanned = true;
    const found = state.candidates.length;
    state.note = found
      ? `Found ${found} pair${found === 1 ? "" : "s"} to look at.`
      : "Nothing new pairs cleanly. What the scan rejected is below.";
  } catch (e) {
    state.error = (e && e.message) || "Could not scan.";
  }
  state.scanning = false;
  return refresh();
}

/* What the scan rejected, and why.
 *
 * "No candidates" is not an answer to "where is my missing transfer". These
 * two lists are the two reasons a movement stays unpaired, and they need
 * different things done about them: one is a date the matcher would not
 * accept, the other is a second half that does not exist. */
function scanFindings(refresh) {
  if (!state.scanned) return null;
  const bits = [];

  if (state.nearMisses.length) {
    bits.push(el("div", { class: "label xferhead", text: "matched, but too far apart" }));
    bits.push(el("div", { class: "hint",
      text: "The amounts cancel and the accounts differ, but the two dates "
          + "are further apart than the matcher will guess across. Link one "
          + "only if you recognise it -- equal and opposite months apart is "
          + "usually coincidence." }));
    for (const c of state.nearMisses) {
      bits.push(el("div", { class: "xfer-row" },
        el("div", { class: "grow" },
          el("div", {},
            el("span", { class: "xfer-acct", text: c.from_account }),
            el("span", { class: "xfer-arrow", text: " → " }),
            el("span", { class: "xfer-acct", text: c.to_account })),
          el("div", { class: "hint",
            text: `${c.date} → ${c.in_date} · ${c.days_apart} days apart · `
                + `${c.out_payee || "—"} / ${c.in_payee || "—"}` })),
        moneyEl(c.amount_cents, "xfer-amt"),
        el("button", { class: "btn", type: "button", text: "Link anyway",
          onclick: async () => {
            try {
              await post("/api/edit/transfer",
                         { out_id: c.out_id, in_id: c.in_id });
              state.note = "Linked as one movement.";
            } catch (e) {
              state.error = (e && e.message) || "Could not link those.";
            }
            return scanTransfers(refresh);
          } })));
    }
  }

  if (state.unpaired.length) {
    const n = state.unpaired.length;
    bits.push(el("div", { class: "label xferhead",
                          text: "between your accounts, but unpaired" }));
    bits.push(el("div", { class: "hint",
      text: `${n} movement${n === 1 ? "" : "s"} that reads as going between `
          + "two of your own accounts, with nothing here that cancels it. "
          + "Usually the other account's statement has not been imported for "
          + "that month." }));
    for (const r of state.unpaired.slice(0, 12)) {
      bits.push(el("div", { class: "xfer-row" },
        el("div", { class: "grow" },
          el("div", { text: r.payee || "—" }),
          el("div", { class: "hint", text: `${r.date} · ${r.account}` })),
        moneyEl(r.amount_cents, "xfer-amt")));
    }
    if (n > 12) {
      bits.push(el("div", { class: "hint", text: `and ${n - 12} more.` }));
    }
  }

  /* Said out loud rather than quietly skipped. A scan that ignores a third
   * of what looks like a transfer is one you stop trusting the moment you
   * notice, and the rows are still sitting there to be noticed. */
  if (state.outside.length) {
    const n = state.outside.length;
    bits.push(el("details", { class: "legend" },
      el("summary", { text: `${n} left out of the scan on purpose` }),
      el("div", { class: "node-body flush" },
        el("div", { class: "hint",
          text: "Money out to somewhere you hold no account — "
              + (state.exclusions.join(", ") || "nothing configured")
              + ". The other half is in a statement you will never import, so "
              + "pairing these can only ever match a coincidence. Change the "
              + "list in Settings under \u201cNever scan these as "
              + "transfers\u201d." }),
        ...state.outside.slice(0, 15).map((r) => el("div", { class: "xfer-row" },
          el("div", { class: "grow" },
            el("div", { text: r.payee || "—" }),
            el("div", { class: "hint", text: `${r.date} · ${r.account}` })),
          moneyEl(r.amount_cents, "xfer-amt"))),
        n > 15 ? el("div", { class: "hint", text: `and ${n - 15} more.` }) : null)));
  }

  if (!bits.length) {
    bits.push(el("div", { class: "hint",
      text: "Nothing left over: every movement is either linked or does not "
          + "look like one." }));
  }
  return el("div", {}, ...bits);
}

function transfersPanel(refresh) {
  const bits = [];

  bits.push(el("div", { class: "row" },
    el("button", { class: "btn", type: "button", disabled: state.scanning,
                   text: state.scanning ? "Scanning\u2026" : "Scan for transfers",
                   onclick: () => scanTransfers(refresh) }),
    el("span", { class: "hint",
      text: "Re-checks every unlinked row, and says what it rejected." })));

  if (state.candidates.length) {
    bits.push(el("div", { class: "label", text: "looks like a pair" }));
    bits.push(el("div", { class: "hint",
      text: "Two amounts that cancel, in different accounts, within a few "
          + "days. Equal and opposite can be coincidence, so nothing is "
          + "joined until you say so." }));
    for (const c of state.candidates) {
      bits.push(el("div", { class: "xfer-row" },
        el("div", { class: "grow" },
          el("div", {},
            el("span", { class: "xfer-acct", text: c.from_account }),
            el("span", { class: "xfer-arrow", text: " → " }),
            el("span", { class: "xfer-acct", text: c.to_account })),
          el("div", { class: "hint",
            text: `${c.date} · ${c.out_payee || "—"} / ${c.in_payee || "—"}`
                + (c.days_apart ? ` · ${c.days_apart} day apart` : "") })),
        moneyEl(c.amount_cents, "xfer-amt"),
        el("button", { class: "btn", type: "button", text: "Link",
          onclick: async () => {
            try {
              await post("/api/edit/transfer",
                         { out_id: c.out_id, in_id: c.in_id });
              state.note = "Linked as one movement.";
            } catch (e) {
              state.error = (e && e.message) || "Could not link those.";
            }
            return refresh();
          } })));
    }
  }

  if (state.transfers.length) {
    bits.push(el("div", { class: "label xferhead", text: "linked movements" }));
    for (const t of state.transfers) {
      bits.push(el("div", { class: "xfer-row" },
        el("div", { class: "grow" },
          el("div", {},
            el("span", { class: "xfer-acct", text: t.from_account }),
            el("span", { class: "xfer-arrow", text: " → " }),
            el("span", { class: "xfer-acct", text: t.to_account }),
            // Whether it left the budget matters: one changes what there is
            // to spend, the other only moves it around.
            t.leaves_budget
              ? el("span", { class: "pill warn", text: "leaves budget" })
              : t.enters_budget
                ? el("span", { class: "pill ok", text: "enters budget" })
                : null),
          el("div", { class: "hint", text: `${t.date} · ${t.payee || "—"}` })),
        moneyEl(t.amount_cents, "xfer-amt"),
        el("button", { class: "btn ghost", type: "button", text: "Unlink",
          onclick: async () => {
            if (!window.confirm("Separate these back into two ordinary "
                + "transactions?")) return;
            try {
              // A real DELETE with a body -- there is no _method convention
              // here, and inventing one would only work by accident.
              await api("DELETE", "/api/edit/transfer", { id: t.id });
            } catch (e) {
              state.error = (e && e.message) || "Could not unlink that.";
            }
            return refresh();
          } })));
    }
  }

  if (!bits.length) {
    return el("div", { class: "hint pad",
      text: "No movements between accounts yet. Import a second account and "
          + "matching pairs will be suggested here." });
  }
  const findings = scanFindings(refresh);
  if (findings) bits.push(findings);
  return el("div", { class: "xfers" }, ...bits);
}

/* What has been imported, and a way back.
 *
 * Filing a statement against the wrong account is an ordinary mistake and was
 * permanent: a hundred and twenty rows in the wrong place with nothing to do
 * but delete them one at a time. Every committed row remembers the
 * transaction it created, so the undo is exact -- it removes what that import
 * added and nothing else.
 */
function historyPanel(refresh) {
  const done = state.history.filter((b) => b.state !== "review");
  if (!done.length) {
    return el("div", { class: "hint pad", text: "Nothing imported yet." });
  }
  return el("div", { class: "imphist" }, ...done.map((b) => el("div",
    { class: "imphist-row" },
    el("div", { class: "grow" },
      el("div", { class: "imphist-name", text: b.filename || "(no name)" }),
      el("div", { class: "hint",
        text: `${b.account} · ${b.uploaded_at.slice(0, 16).replace("T", " ")}`
            + ` · ${b.rows_imported} imported`
            + (b.rows_duplicate ? `, ${b.rows_duplicate} duplicate` : "") })),
    b.state === "committed"
      ? el("button", { class: "btn ghost", type: "button", text: "Undo",
          onclick: async () => {
            if (!window.confirm(`Undo "${b.filename}"?\n\nThis removes the `
                + `${b.rows_imported} transactions it added. Anything you have `
                + "since edited or categorised in them goes too.")) return;
            try {
              const r = await post(`/api/import/batch/${b.id}/revert`, {});
              state.note = `Undone. ${r.transactions_removed} transactions removed.`;
            } catch (e) {
              state.error = (e && e.message) || "Could not undo that import.";
            }
            return refresh();
          } })
      : el("span", { class: "pill", text: b.state }))));
}

export async function importView(container, { onDone } = {}) {
  const [accts, cats, cov, batches, unfiled, xfers] = await Promise.all([
    get("/api/accounts"), get("/api/categories"), get("/api/view/coverage"),
    get("/api/import/batches"), get(`/api/view/unfiled?filed=${state.showFiled ? 1 : 0}`),
    get("/api/view/transfers"),
  ]);
  // On-budget accounts first: a statement almost always belongs to one, and
  // defaulting to a tracking account is a mistake nobody would notice until
  // the spending failed to show up in any envelope.
  state.accounts = [...accts.accounts].sort(
    (a, b) => (b.on_budget ? 1 : 0) - (a.on_budget ? 1 : 0));
  state.categories = cats.categories;
  state.transfers = xfers.transfers || [];
  state.candidates = xfers.candidates || [];
  state.tracking = unfiled.tracking || [];
  state.unfiled = unfiled.groups || [];
  // Income categories are offered here even though the classifier never
  // suggests one: filing a salary is exactly the job this panel is for.
  state.allCats = cats.categories;
  state.coverage = cov;
  state.history = batches.batches || [];
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

  // Filing a merchant, linking a transfer, undoing an import: each redraws
  // the screen, and each is done to something you are looking at partway down
  // it. Landing back at the top means finding your place again every time.
  const refresh = async (rowsOnly) => keepingPlace(async () => {
    if (state.batch && rowsOnly) {
      const d = await get(`/api/import/batch/${state.batch.id}`);
      state.batch = d.batch;
      state.rows = d.rows;
      return render();
    }
    return importView(container, { onDone });
  });

  const accountSel = el("select", { class: "input", "aria-label": "Account" });
  for (const a of state.accounts) {
    accountSel.append(el("option", { value: a.id,
      text: a.on_budget ? a.name : `${a.name} (off budget)` }));
  }
  /* Where you were, not where the alphabet starts.
   *
   * Statements arrive one account at a time and in runs -- six months of the
   * same account, then six of the next. Resetting to whichever name sorts
   * first means picking the account again on every file, and picking it wrong
   * is silent until the spending fails to appear in any envelope.
   *
   * Within a session, whatever you last chose. Across a restart, the account
   * of the most recent import, which is the same answer arrived at from the
   * data rather than from memory.
   */
  const remembered = state.lastAccount
    || (state.history.find((b) => b.state === "committed") || {}).account_id;
  if (remembered && state.accounts.some((a) => a.id === remembered)) {
    accountSel.value = remembered;
  }
  accountSel.addEventListener("change", () => {
    state.lastAccount = accountSel.value;
  });

  /* Somewhere to make the first one. The endpoint has always existed and
   * nothing called it, so an empty ledger left this screen with an empty
   * dropdown and no way forward -- a dead end rather than a first step. */
  const newName = el("input", {
    class: "input", type: "text", id: "acct-name",
    placeholder: "e.g. UFCU Checking", "aria-label": "New account name" });
  // "Wallet" exists because Apple Pay is not an investment, and picking
  // Investment to get an off-budget account is not a thought anyone has.
  // Money moved to one has left the budget without being spent, which is
  // exactly what a transfer to Venmo or a loan needs somewhere to go.
  const OFF_BUDGET = new Set(["investment", "wallet"]);
  const newType = el("select", { class: "input", "aria-label": "Account type" },
    ...[["checking", "Checking"], ["savings", "Savings"],
        ["credit", "Credit card"], ["cash", "Cash"],
        ["wallet", "Wallet — Apple Pay, Venmo, a loan (off budget)"],
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
        on_budget: !OFF_BUDGET.has(newType.value),
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
        if (r.yours) bits.push(`${r.yours} from your own corrections`);
        if (r.history) bits.push(`${r.history} from how they were filed before`);
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
        `/api/import/upload?account=${(state.lastAccount = accountSel.value)}`
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

    /* Where each account started.
   *
   * A balance is otherwise the net of whatever happened to be imported --
   * which is how a savings account that has never been overdrawn shows minus
   * twelve hundred pounds, and why the savings figure on the budget screen
   * cannot be trusted until these are set. It is the closing balance on the
   * statement before the first one you imported. */
  function openingCard() {
    const rows = state.accounts.filter((a) => a.on_budget).map((a) => {
      const box = el("input", {
        class: "input amount", type: "text", inputmode: "decimal",
        value: money(a.opening_balance_cents || 0),
        "aria-label": `Starting balance for ${a.name}`,
      });
      async function commit() {
        const cents = parseMoney(box.value);
        if (cents === null || cents === (a.opening_balance_cents || 0)) {
          box.value = money(a.opening_balance_cents || 0);
          return;
        }
        try {
          await patch(`/api/accounts/${a.id}`,
                      { opening_balance_cents: cents });
          state.note = `Set the starting balance for ${a.name}.`;
        } catch (e) {
          state.error = (e && e.message) || "Could not save that.";
        }
        return refresh();
      }
      box.addEventListener("blur", commit);
      box.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); box.blur(); }
        if (e.key === "Escape") {
          box.value = money(a.opening_balance_cents || 0);
          box.blur();
        }
      });
      return el("div", { class: "xfer-row" },
        el("span", { class: "grow", text: a.name }),
        el("span", { class: "hint", text: "now" }),
        moneyEl(a.balance_cents || 0, "xfer-amt"),
        box);
    });
    if (!rows.length) return null;
    const missing = state.accounts.filter(
      (a) => a.on_budget && !a.opening_balance_cents).length;
    return el("section", { class: "node", dataset: { kind: "budget" } },
      el("header", { class: "node-head" },
        el("span", { class: "node-title", text: "Starting balances" }),
        el("div", { class: "spacer" }),
        missing
          ? el("span", { class: "pill warn", text: `${missing} not set` })
          : el("span", { class: "pill ok", text: "all set" })),
      el("div", { class: "node-body" },
        el("div", { class: "hint",
          text: "What was in each account before the first statement you "
              + "imported — the closing balance on the one before it. Without "
              + "it a balance is only the net of what has been imported, and "
              + "the savings figure on the budget screen counts from the "
              + "wrong place." }),
        ...rows));
  }

  const coverageCard = el("section", { class: "node", dataset: { kind: "budget" } },
      el("header", { class: "node-head" },
        el("span", { class: "node-title", text: "Months covered" })),
      el("div", { class: "node-body" }, coverageStrip(state.coverage)));

    const unfiledCard = el("section", { class: "node", dataset: { kind: "budget" } },
      el("header", { class: "node-head" },
        el("span", { class: "node-title", text: "Still uncategorised" }),
        el("div", { class: "spacer" }),
        el("span", { class: "label",
          text: `${state.unfiled.length} merchant`
              + `${state.unfiled.length === 1 ? "" : "s"}` })),
      el("div", { class: "node-body" }, unfiledPanel(refresh)));

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

    // Before coverage: unfiled rows are work waiting, and coverage is a
    // reference. Work first.
    // Folded like the import history, with one difference: a suggested pair
    // is work waiting, so it opens itself when there are any. A linked
    // transfer is a record and stays put.
    const xferDetails = el("details", { class: "impfold" },
      el("summary", {},
        el("span", { text: "Transfers" }),
        el("span", { class: "hint",
          text: ` · ${state.transfers.length} linked`
              + (state.candidates.length
                 ? ` · ${state.candidates.length} to confirm` : "") })),
      transfersPanel(refresh));
    const wantOpen = state.transfersOpen === null
      ? state.candidates.length > 0
      : state.transfersOpen;
    if (wantOpen) xferDetails.setAttribute("open", "");
    xferDetails.addEventListener("toggle", () => {
      // Once you have opened or closed it yourself, that decision stands --
      // otherwise confirming the last pair would fold it shut mid-task.
      state.transfersOpen = xferDetails.open;
    });

    const xferCard = el("section", { class: "node", dataset: { kind: "budget" } },
      el("div", { class: "node-body" }, xferDetails));

    // Folded away: this is a record, not work waiting. It is opened when
    // something has gone in wrong, which is rare, and the rest of the time it
    // is a list of files you already dealt with.
    const histDetails = el("details", { class: "impfold" },
      el("summary", {},
        el("span", { text: "Imported files" }),
        el("span", { class: "hint",
          text: ` · ${state.history.filter((b) => b.state !== "review").length}`
              + " · undo one here" })),
      historyPanel(refresh));
    if (state.historyOpen) histDetails.setAttribute("open", "");
    histDetails.addEventListener("toggle", () => {
      // Remembered, or undoing an import closes the list it was undone from.
      state.historyOpen = histDetails.open;
    });

    const histCard = el("section", { class: "node", dataset: { kind: "budget" } },
      el("div", { class: "node-body" }, histDetails));

    parts.push(unfiledCard, xferCard, histCard, coverageCard);
    const opening = openingCard();
    if (opening) parts.push(opening);
    mount(container, ...parts);
  }

  render();
}
