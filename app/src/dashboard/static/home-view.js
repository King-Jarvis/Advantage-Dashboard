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
import { el, money, moneyEl, mount, svg } from "./dom.js";
import { fromServer } from "./time.js";

/* Times come off the wire as ISO strings; the widget wants "09:30" and a day
 * marker when it is not today. Reading e.time -- a field the API does not
 * send -- left every row blank, which looked like a styling problem and was
 * not. */
function whenLabel(e) {
  const d = fromServer(e.starts_at, { floating: e.all_day });
  if (!d) return e.all_day ? "all day" : "";

  const today = new Date();
  const sameDay = (a, b) => a.getDate() === b.getDate()
    && a.getMonth() === b.getMonth() && a.getFullYear() === b.getFullYear();
  const tom = new Date(Date.now() + 86400000);

  const DAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  // Within the week a weekday name is enough; past that it needs a date,
  // because "Fri" three weeks out tells you almost nothing.
  const ahead = Math.round((d - today) / 86400000);
  const day = sameDay(d, today) ? "today"
    : sameDay(d, tom) ? "tom"
    : ahead < 7 ? DAY[d.getDay()]
    : `${d.getDate()} ${MON[d.getMonth()]}`;

  // An all-day event still has to say which day. "all day" on its own is the
  // one thing about it that was never in question.
  if (e.all_day) return day === "today" ? "all day" : `${day} · all day`;

  const hhmm = `${String(d.getHours()).padStart(2, "0")}:`
             + `${String(d.getMinutes()).padStart(2, "0")}`;
  return day === "today" ? hhmm : `${day} ${hhmm}`;
}

function widget(kind, title, action, ...body) {
  return el("section", { class: "node widget", dataset: { kind } },
    el("header", { class: "node-head" },
      el("span", { class: "node-title", text: title }),
      el("div", { class: "spacer" }),
      action || null),
    el("div", { class: "node-body" }, ...body));
}

/* A whole widget that is one big target.
 *
 * Used where the widget has nothing else to press. A real button would have
 * to wrap the header and the body, and a button cannot contain the heading
 * structure without becoming a mess, so this is the role/tabindex form --
 * which then owes the keyboard the behaviour a button would have given for
 * free. Enter and Space both, because a role="button" that ignores Space is
 * the half-implementation screen reader users learn to distrust.
 */
function openable(node, label, onOpen) {
  node.classList.add("is-openable");
  node.setAttribute("role", "button");
  node.setAttribute("tabindex", "0");
  node.setAttribute("aria-label", label);
  node.addEventListener("click", onOpen);
  node.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
      e.preventDefault();
      onOpen();
    }
  });
  return node;
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

/* A small chart of how far through each category the month is.
 *
 * Proportion of budget used, not amount spent. Absolute amounts would make
 * this a chart of how big rent is -- which nobody needs telling, and which
 * flattens every other category into an illegible sliver. The question a
 * glance should answer is "is anything running away from me", and that is a
 * ratio.
 *
 * The categories are chosen by the same measure they are drawn by: the ones
 * closest to using their envelope up, not the ones that happen to be
 * largest. Seven fit, and a small category at ninety-nine per cent is the
 * one worth the slot.
 *
 * A line marks the budget. Bars that cross it are over, drawn in red and
 * allowed to stand above the line rather than being clipped to it, because
 * how far past matters.
 *
 * Exact figures live on the budget screen; this carries shape and colour.
 */
function miniSpend(items) {
  // x is normalised to 100 so bar widths stay proportional at any rendered
  // width. The alternative -- fixed units stretched to fit -- distorts the
  // bars horizontally and makes them look squashed on a wide widget.
  const W = 100, H = 62, FLOOR = 4, LINE = 42;   // LINE is the budget mark
  const CAP = H - FLOOR;
  const chart = svg("svg", {
    class: "mini", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none",
    role: "img",
    "aria-label": items.map((i) => {
      const pc = i.budgeted_cents > 0
        ? Math.round((i.spent_cents / i.budgeted_cents) * 100) : null;
      return `${i.name}: ${money(i.spent_cents)}`
           + (pc === null ? ", nothing budgeted" : ` of ${money(i.budgeted_cents)}, ${pc}%`);
    }).join("; "),
  });
  if (!items.length) return chart;

  const slot = W / items.length;
  const barW = slot * 0.54;

  chart.append(svg("line", { class: "mini-budget-line",
                             x1: 0, x2: W, y1: H - LINE, y2: H - LINE }));

  items.forEach((i, n) => {
    const x = slot * n + (slot - barW) / 2;
    const ratio = i.budgeted_cents > 0 ? i.spent_cents / i.budgeted_cents : 0;
    // Anything past about 1.4x is drawn at the ceiling: the difference
    // between 180% and 260% over is not something to squint at a bar for.
    const h = Math.min(CAP, ratio * LINE);
    const over = ratio > 1;
    if (i.budgeted_cents > 0) {
      chart.append(svg("rect", { class: "mini-plan", x, y: H - FLOOR - LINE,
                                 width: barW, height: LINE, rx: 1.5 }));
    }
    if (h > 0.5) {
      chart.append(svg("rect", {
        class: "mini-spent" + (over ? " over" : ""),
        x, y: H - FLOOR - h, width: barW, height: h, rx: 1.5 }));
    }
  });
  return chart;
}

/* ── budget ─────────────────────────────────────────────────────────────── */
function budgetWidget(b, onGo) {
  if (!b.has_data) {
    return openable(
      widget("budget", "Budget", null,
        empty("No categories yet",
              "Add a few in the budget screen and this fills in.")),
      "Set up the budget", () => onGo("budget"));
  }
  return openable(budgetBody(b), `Open the budget for ${b.month}`,
                  () => onGo("budget"));
}

function budgetBody(b) {
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

  return widget("budget", `Budget · ${b.month}`, null,
    el("div", { class: "wstat" },
      el("span", { class: "stat-label", text: "to be budgeted" }),
      el("div", { class: "row" },
        moneyEl(tbb, "big"),
        el("span", { class: `pill ${tone}`,
          text: tbb === 0 ? "all assigned" : tbb > 0 ? "unassigned" : "over" }))),
    progress,
    b.top && b.top.length
      ? el("div", { class: "minibox" },
          el("div", { class: "hint minihead", text: "closest to spent" }),
          miniSpend(b.top),
          el("div", { class: "minilabels" },
            ...b.top.map((i) => el("span", { class: "minilabel", text: i.name }))))
      : null,
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
function calendarWidget(c, onGo, onSettings, onOpenDay) {
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
      ...c.events.slice(0, 6).map((e) => el("button", {
        // A button, not a div with a click on it: this is reachable by
        // keyboard and announced as pressable without any of it being
        // reimplemented here.
        class: "wrow wrow-hit", type: "button",
        "aria-label": `Open ${e.title || "this event"} in the agenda`,
        onclick: () => onOpenDay(dayKey(e)),
      },
        el("span", { class: "wtime", text: whenLabel(e) }),
        el("span", { class: "grow" },
          el("div", { text: e.title || "(untitled)" }),
          e.location ? el("div", { class: "hint", text: e.location }) : null)))));
}

/* The calendar day an event belongs to, in the reader's own zone.
 *
 * Not starts_at.slice(0, 10): that is the UTC date, and an event at 8pm in
 * Austin is stored on the following day. Tapping it would open a day with
 * nothing on it, which reads as the tap having failed. */
function dayKey(e) {
  const d = fromServer(e.starts_at, { floating: e.all_day });
  if (!d) return String(e.starts_at || "").slice(0, 10);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/* ── mail ───────────────────────────────────────────────────────────────── */
function mailWidget(m, onGo, onSettings, onOpenMail) {
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
      ...m.messages.slice(0, 5).map((x) => el("button", {
        class: "wrow wrow-hit", type: "button",
        "aria-label": `Read ${x.subject || "this message"}`,
        onclick: () => onOpenMail(x.id),
      },
        el("span", {
          class: "pill " + (x.score >= 5 ? "danger" : x.score >= 4 ? "warn" : "info"),
          text: String(x.score) }),
        el("span", { class: "grow" },
          el("div", { class: "wsubject", text: x.subject || "(no subject)" }),
          el("div", { class: "hint", text: x.sender || "" }))))));
}

export async function homeView(container,
                              { onGo, onSettings, onOpenMail, onOpenDay }) {
  const d = await get("/api/view/home");
  // Inbox and budget share the top; the agenda takes the full width beneath
  // them. What is coming up is a sequence, and a sequence wants length --
  // squeezed into a third of the width it shows two entries and a scrollbar.
  mount(container,
    el("div", { class: "widgets" },
      mailWidget(d.mail, onGo, onSettings, onOpenMail),
      budgetWidget(d.budget, onGo),
      calendarWidget(d.calendar, onGo, onSettings, onOpenDay)));
}
