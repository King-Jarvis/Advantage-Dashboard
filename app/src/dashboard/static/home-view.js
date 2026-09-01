/* The front page: one widget per section.
 *
 * Each answers a single question at a glance and then gets out of the way --
 * what is next, what wants me, where is the money. Detail lives one click
 * deeper, in the section itself.
 *
 * A widget with nothing in it says which kind of nothing it is. An empty list
 * reads as "nothing today"; "no account connected" is a different fact, and
 * conflating them is how a broken integration goes unnoticed for a week.
 */
import { get } from "./api.js";
import { el, money, moneyEl, mount } from "./dom.js";

function widget(kind, title, action, ...body) {
  return el("section", { class: "node widget", dataset: { kind } },
    el("header", { class: "node-head" },
      el("span", { class: "node-title", text: title }),
      el("div", { class: "spacer" }),
      action || null),
    el("div", { class: "node-body" }, ...body));
}

function empty(line, hint) {
  return el("div", { class: "empty" },
    el("div", { class: "big", text: line }),
    hint ? el("div", { class: "hint", text: hint }) : null);
}

function goto(label, onGo, view) {
  return el("button", { class: "btn ghost", type: "button", text: label,
                        onclick: () => onGo(view) });
}

/* ── budget ─────────────────────────────────────────────────────────────── */
function budgetWidget(b, onGo) {
  if (!b.has_data) {
    return widget("budget", "Budget", goto("Set up", onGo, "budget"),
      empty("No categories yet",
            "Add a few in the budget screen and this fills in."));
  }
  const tbb = b.to_be_budgeted_cents;
  const tone = tbb === 0 ? "ok" : tbb > 0 ? "warn" : "danger";
  const pct = b.budgeted_cents > 0
    ? Math.min(100, Math.round((b.spent_cents / b.budgeted_cents) * 100)) : 0;

  // Nothing assigned and nothing spent this month is "not started yet", not
  // "0 of 0". Saying the first invites the obvious next action; the second
  // just looks broken.
  const progress = b.started
    ? el("div", { class: "wline" },
        el("span", { class: "hint",
          text: `${money(b.spent_cents)} spent of ${money(b.budgeted_cents)} budgeted` }),
        el("span", { class: "hint", text: `${pct}%` }))
    : el("div", { class: "wline" },
        el("span", { class: "hint",
          text: `Nothing budgeted for ${b.month} yet`
              + (b.last_budgeted_month && b.last_budgeted_month !== b.month
                 ? ` — last set up in ${b.last_budgeted_month}` : "") }));

  return widget("budget", `Budget · ${b.month}`, goto("Open", onGo, "budget"),
    el("div", { class: "wstat" },
      el("span", { class: "stat-label", text: "to be budgeted" }),
      el("div", { class: "row" },
        moneyEl(tbb, "big"),
        el("span", { class: `pill ${tone}`,
          text: tbb === 0 ? "all assigned" : tbb > 0 ? "unassigned" : "over" }))),
    progress,
    b.overspent_count
      ? el("div", { class: "wlist" },
          ...b.overspent.map((o) => el("div", { class: "wrow" },
            el("span", { class: "status-dot danger" }),
            el("span", { class: "grow", text: o.name }),
            moneyEl(-o.over_cents))),
          b.overspent_count > b.overspent.length
            ? el("div", { class: "hint",
                text: `and ${b.overspent_count - b.overspent.length} more over` })
            : null)
      : el("div", { class: "row ok-line" },
          el("span", { class: "status-dot ok" }),
          el("span", { class: "hint", text: "Nothing overspent" })));
}

/* ── calendar ───────────────────────────────────────────────────────────── */
function calendarWidget(c, onGo, onSettings) {
  if (!c.connected) {
    return widget("agenda", "Agenda",
      el("button", { class: "btn", type: "button", text: "Connect",
                     onclick: onSettings }),
      empty("Not connected",
            "Connect a Google account in Settings to see your calendar."));
  }
  if (!c.events.length) {
    return widget("agenda", "Agenda", goto("Open", onGo, "agenda"),
      empty("Nothing scheduled", "The rest of today is clear."));
  }
  return widget("agenda", "Agenda", goto("Open", onGo, "agenda"),
    el("div", { class: "wlist" },
      ...c.events.slice(0, 5).map((e) => el("div", { class: "wrow" },
        el("span", { class: "wtime", text: e.time || "" }),
        el("span", { class: "grow", text: e.title || "(untitled)" })))));
}

/* ── mail ───────────────────────────────────────────────────────────────── */
function mailWidget(m, onGo, onSettings) {
  if (!m.connected) {
    return widget("inbox", "Inbox",
      el("button", { class: "btn", type: "button", text: "Connect",
                     onclick: onSettings }),
      empty("Not connected",
            "Connect a Google account in Settings to see what needs you."));
  }
  if (!m.messages.length) {
    return widget("inbox", "Inbox", goto("Open", onGo, "inbox"),
      empty("Nothing needs you", "No mail scored important since the last check."));
  }
  return widget("inbox", "Inbox", goto("Open", onGo, "inbox"),
    el("div", { class: "wlist" },
      ...m.messages.slice(0, 5).map((x) => el("div", { class: "wrow" },
        el("span", { class: "status-dot warn" }),
        el("span", { class: "grow" },
          el("div", { class: "wsubject", text: x.subject || "(no subject)" }),
          el("div", { class: "hint", text: x.sender || "" }))))));
}

export async function homeView(container, { onGo, onSettings }) {
  const d = await get("/api/view/home");
  mount(container,
    el("div", { class: "grid widgets" },
      calendarWidget(d.calendar, onGo, onSettings),
      mailWidget(d.mail, onGo, onSettings),
      budgetWidget(d.budget, onGo)));
}
