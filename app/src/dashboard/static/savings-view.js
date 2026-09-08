/* The savings plan.
 *
 * The budget screen answers "what does this normally cost". This answers
 * "what could it cost instead", which is a different question and deserves
 * its own screen rather than a second column of numbers competing with the
 * first.
 *
 * Every target on this page is a figure the person has already spent in one
 * of their own months, and the row says which. That is the whole argument
 * for the plan: not "spend 25% less", which is a wish, but "you spent £151
 * on this in June", which is a fact they can check.
 *
 * Fixed costs appear, and appear uncut. Hiding them would make the plan look
 * better and be less useful -- seeing that half of it cannot move is the
 * thing that makes the movable half believable.
 */
import { get, patch, post } from "./api.js";
import { el, keepingPlace, money, moneyEl, mount } from "./dom.js";

const state = {
  month: null, plan: null, chosen: null, busy: false, error: "", note: "",
};

const CLASSES = [
  ["essential", "Cannot be cut"],
  ["semi", "Some room"],
  ["discretionary", "Room to cut"],
];

/* ── the headline ───────────────────────────────────────────────────────── */
function summary(refresh) {
  const p = state.plan;
  const saves = p.saves_cents;
  const pct = p.expected_cents
    ? Math.round((saves / p.expected_cents) * 100) : 0;

  const span = p.months.length
    ? (p.months.length === 1 ? p.months[0]
       : `${p.months[0]} to ${p.months[p.months.length - 1]}`)
    : "no complete months";

  return el("section", { class: "node", dataset: { kind: "savings" } },
    el("header", { class: "node-head" },
      el("span", { class: "node-title", text: "A plan to spend less" }),
      el("div", { class: "spacer" }),
      el("button", { class: "btn", type: "button", disabled: state.busy,
                     text: state.busy ? "Working…" : "Sort my categories",
                     onclick: () => sortCategories(refresh) })),
    el("div", { class: "node-body" },
      el("div", { class: "row" },
        moneyEl(saves, "hero"),
        el("span", { class: `pill ${saves > 0 ? "ok" : "warn"}`,
                     text: saves > 0
                       ? `a month — ${pct}% less than you spend`
                       : "nothing to cut yet" })),
      el("p", { class: "label",
                text: `Built from ${p.months.length} complete month`
                  + `${p.months.length === 1 ? "" : "s"}: ${span}. `
                  + `Now ${money(p.expected_cents)} a month, `
                  + `plan ${money(p.target_cents)}.` }),
      state.error ? el("div", { class: "error", text: state.error }) : null,
      state.note ? el("div", { class: "note", text: state.note }) : null,
      unclassifiedNote(),
      el("details", { class: "legend" },
        el("summary", { text: "How these targets are worked out" }),
        el("dl", {},
          el("dt", { text: "Every target is a month you have had" }),
          el("dd", { text: "Not a percentage off. Each one is a level your "
            + "own statements show you spent at or below, so it is something "
            + "you have already done rather than something to attempt." }),
          el("dt", { text: "Cannot be cut" }),
          el("dd", { text: "Rent, loans, insurance. Budgeting less does not "
            + "make the bill smaller, so these are left exactly as they are." }),
          el("dt", { text: "Some room" }),
          el("dd", { text: "Groceries, fuel, the things you need but choose "
            + "how much of. Trimmed gently." }),
          el("dt", { text: "Room to cut" }),
          el("dd", { text: "Eating out, going out, hobbies. Trimmed hardest, "
            + "but still only to a figure you have actually lived on." }),
          el("dt", { text: "Occasional costs stay whole" }),
          el("dd", { text: "An annual bill is not overspending. Cutting what "
            + "you set aside for it just moves the problem to the month it "
            + "arrives." }),
          el("dt", { text: "Got one wrong?" }),
          el("dd", { text: "Change it on the row. Your answer is kept and is "
            + "never overwritten." })))));
}

function unclassifiedNote() {
  const n = (state.plan.unclassified || []).length;
  if (!n) return null;
  // Not "treated cautiously": an unsorted category is trimmed as "Some
  // room", which means a rent or a loan payment is being cut until someone
  // says otherwise. Saying so plainly is the difference between a plan that
  // is wrong and a plan that is wrong and hiding it.
  const subject = n === 1 ? "1 category has" : `${n} categories have`;
  const pronoun = n === 1 ? "it is" : "they are";
  return el("div", { class: "error" },
    el("span", { text: `${subject} not been sorted yet, so ${pronoun} being `
      + "trimmed as \u201cSome room\u201d \u2014 including anything fixed. "
      + "Sort them before you trust these figures." }));
}

/* ── one category ───────────────────────────────────────────────────────── */
function row(c, refresh) {
  const chosen = state.chosen instanceof Set ? state.chosen : new Set();
  const movable = c.saves_cents > 0;

  const tick = el("input", {
    type: "checkbox", class: "check", checked: chosen.has(c.category_id),
    disabled: !movable,
    "aria-label": `Use the plan for ${c.name}`,
    onchange: (e) => {
      if (e.target.checked) chosen.add(c.category_id);
      else chosen.delete(c.category_id);
      state.chosen = chosen;
      refresh(true);
    },
  });

  const picker = el("select", {
    class: "input flex", "aria-label": `How movable is ${c.name}`,
    onchange: (e) => setFlexibility(c.category_id, e.target.value, refresh),
  });
  for (const [value, label] of CLASSES) {
    picker.append(el("option", { value, text: label,
                                 selected: c.flexibility === value }));
  }

  return el("div", { class: `saverow${movable ? "" : " locked"}` },
    tick,
    el("span", { class: "sname", text: c.name }),
    moneyEl(c.expected_cents, "was"),
    el("span", { class: "arrow", text: "→" }),
    moneyEl(c.target_cents, movable ? "ok" : ""),
    movable
      ? el("span", { class: "pill ok", text: `save ${money(c.saves_cents)}` })
      : el("span", { class: "pill", text: "unchanged" }),
    el("div", { class: "why" },
      picker,
      el("span", { text: c.reason }),
      el("span", { text: c.group }),
      c.flexibility_source === "you"
        ? el("span", { class: "pill", text: "your call" }) : null));
}

/* ── actions ────────────────────────────────────────────────────────────── */
function sortCategories(refresh) {
  state.busy = true; state.error = ""; state.note = "";
  refresh(true);
  post("/api/savings/classify", {})
    .then((r) => {
      state.busy = false;
      state.note = r.allowed === false
        ? "Written spending analysis is switched off. Turn it on in Settings, "
          + "or set each category yourself below."
        : r.note;
      refresh();
    })
    .catch((e) => {
      state.busy = false;
      state.error = e.message || "could not sort the categories";
      refresh(true);
    });
}

function setFlexibility(id, value, refresh) {
  patch(`/api/savings/flexibility/${id}`, { flexibility: value })
    .then(() => refresh())
    .catch((e) => { state.error = e.message || "could not save that";
                    refresh(true); });
}

function applyChosen(refresh) {
  const ids = [...(state.chosen || [])];
  if (!ids.length) return;
  state.busy = true; refresh(true);
  post(`/api/savings/apply/${encodeURIComponent(state.month)}`,
       { category_ids: ids })
    .then((r) => {
      state.busy = false;
      state.chosen = new Set();
      state.note = `Budgeted the plan for ${r.applied} categor`
        + `${r.applied === 1 ? "y" : "ies"} in ${r.month}.`;
      refresh();
    })
    .catch((e) => {
      state.busy = false;
      state.error = e.message || "could not set the budget";
      refresh(true);
    });
}

/* ── the screen ─────────────────────────────────────────────────────────── */
export function savingsView(container, month, onBack) {
  state.month = month;
  if (!(state.chosen instanceof Set)) state.chosen = new Set();

  const refresh = (localOnly) =>
    keepingPlace(() => (localOnly ? render()
                                  : savingsView(container, month, onBack)));

  function render() {
    const p = state.plan;
    const back = el("button", { class: "btn ghost", type: "button",
                                text: "‹ Back to budget", onclick: onBack });

    if (!p) {
      mount(container, back, el("p", { class: "label", text: "Working it out…" }));
      return;
    }

    if (!p.enough) {
      mount(container, back,
        el("section", { class: "node" },
          el("header", { class: "node-head" },
            el("span", { class: "node-title", text: "Not enough history yet" })),
          el("div", { class: "node-body" },
            el("p", { text: `A plan needs at least ${p.minimum} complete `
              + "months covering the same accounts. You have "
              + `${p.months.length}. Import more statements and this fills in.` }))));
      return;
    }

    const movable = p.categories.filter((c) => c.saves_cents > 0);
    const fixed = p.categories.filter((c) => c.saves_cents <= 0);
    const chosen = state.chosen;
    const picked = movable.filter((c) => chosen.has(c.category_id));
    const willSave = picked.reduce((a, c) => a + c.saves_cents, 0);

    const apply = el("div", { class: "row" },
      el("button", { class: "btn", type: "button", disabled: state.busy,
                     text: picked.length
                       ? `Budget these ${picked.length} for ${state.month}`
                       : "Choose categories to budget",
                     onclick: () => applyChosen(refresh) }),
      el("button", { class: "btn ghost", type: "button",
                     text: picked.length === movable.length
                       ? "Clear all" : "Select all",
                     onclick: () => {
                       state.chosen = picked.length === movable.length
                         ? new Set()
                         : new Set(movable.map((c) => c.category_id));
                       refresh(true);
                     } }),
      el("div", { class: "spacer" }),
      picked.length
        ? el("span", { class: "pill ok", text: `${money(willSave)} a month` })
        : null);

    mount(container, back, summary(refresh),
      el("section", { class: "node" },
        el("header", { class: "node-head" },
          el("span", { class: "node-title", text: "Where the money is" })),
        el("div", { class: "node-body" }, apply,
          ...movable.map((c) => row(c, refresh)))),
      fixed.length
        ? el("details", { class: "legend" },
            el("summary", { text: `${fixed.length} left unchanged` }),
            el("div", { class: "node-body flush" },
              ...fixed.map((c) => row(c, refresh))))
        : null);
  }

  render();
  get(`/api/view/savings?month=${encodeURIComponent(month)}`)
    .then((p) => { state.plan = p; render(); })
    .catch((e) => { state.error = e.message || "could not build a plan";
                    render(); });
}
