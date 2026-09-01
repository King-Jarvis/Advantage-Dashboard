/* What is coming up.
 *
 * Grouped by day, because "Thursday" is how people hold a week in their head,
 * not as a flat list of timestamps. Today is labelled as today rather than by
 * its date, for the same reason.
 */
import { get } from "./api.js";
import { el, mount } from "./dom.js";

const DAY = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function localDate(iso) {
  const d = new Date(iso.length <= 10 ? `${iso}T00:00:00` : iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

function dayKey(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`
       + `-${String(d.getDate()).padStart(2, "0")}`;
}

function dayLabel(key) {
  const d = localDate(key);
  if (!d) return key;
  const today = dayKey(new Date());
  const tomorrow = dayKey(new Date(Date.now() + 86400000));
  if (key === today) return "Today";
  if (key === tomorrow) return "Tomorrow";
  return `${DAY[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]}`;
}

function timeLabel(e) {
  if (e.all_day) return "all day";
  const d = localDate(e.starts_at);
  if (!d) return "";
  return `${String(d.getHours()).padStart(2, "0")}:`
       + `${String(d.getMinutes()).padStart(2, "0")}`;
}

export async function agendaView(container, { onSettings } = {}) {
  const d = await get("/api/view/agenda?days=14");

  if (!d.events.length) {
    // Whether this is "a clear fortnight" or "nothing is connected" is the
    // difference between good news and a broken integration.
    const status = await get("/api/view/status").catch(() => ({ accounts: [] }));
    const connected = (status.accounts || []).length > 0;
    return mount(container,
      el("section", { class: "node", dataset: { kind: "agenda" } },
        el("header", { class: "node-head" },
          el("span", { class: "node-title", text: "Agenda" })),
        el("div", { class: "node-body" },
          el("div", { class: "empty" },
            el("div", { class: "big",
              text: connected ? "Nothing scheduled" : "Not connected" }),
            el("div", { class: "hint",
              text: connected
                ? "The next fortnight is clear."
                : "Connect a Google account in Settings to see your calendar." }),
            connected ? null : el("div", { class: "pad" },
              el("button", { class: "btn primary", type: "button",
                             text: "Open Settings",
                             onclick: () => onSettings && onSettings() }))))));
  }

  const days = new Map();
  for (const e of d.events) {
    const dt = localDate(e.starts_at);
    if (!dt) continue;
    const k = dayKey(dt);
    if (!days.has(k)) days.set(k, []);
    days.get(k).push(e);
  }

  mount(container,
    el("section", { class: "node", dataset: { kind: "agenda" } },
      el("header", { class: "node-head" },
        el("span", { class: "node-title", text: "Agenda" }),
        el("div", { class: "spacer" }),
        el("span", { class: "label",
          text: `${d.events.length} in the next ${d.days} days` })),
      el("div", { class: "node-body flush" },
        ...[...days].map(([key, list]) =>
          el("div", { class: "dayblock" },
            el("div", { class: "dayhead" },
              el("span", { class: "label", text: dayLabel(key) }),
              el("div", { class: "spacer" }),
              el("span", { class: "hint",
                text: `${list.length} event${list.length === 1 ? "" : "s"}` })),
            ...list.map((e) => el("div", { class: "eventrow" },
              el("span", { class: "evtime", text: timeLabel(e) }),
              el("div", { class: "grow" },
                el("div", { class: "evtitle", text: e.title || "(untitled)" }),
                e.location
                  ? el("div", { class: "hint", text: e.location })
                  : null),
              e.account_email
                ? el("span", { class: "hint acctchip", text: e.account_email })
                : null)))))));
}
