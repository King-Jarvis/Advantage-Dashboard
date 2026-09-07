/* The calendar.
 *
 * A month grid, because that is the shape a month has in the head. A list of
 * upcoming events answers "what is next"; it cannot answer "is the 14th
 * free", and that is most of what a calendar is for.
 *
 * Two halves, and the split is the point. The grid gives shape at a glance --
 * where the busy weeks are, which days are empty. The panel beneath gives one
 * day in full. Trying to fit both into the grid is what makes calendars
 * cluttered: every cell grows to fit its worst day, and the shape is lost.
 *
 * Ma: empty days are left empty. No zero, no placeholder, no faint dash. An
 * empty Tuesday is information, and drawing something in it hides that.
 */
import { del, get, patch, post } from "./api.js";
import { el, mount } from "./dom.js";

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTH = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"];

function pad(n) { return String(n).padStart(2, "0"); }
function key(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }

function parse(iso) {
  if (!iso) return null;
  const d = new Date(String(iso).length <= 10 ? `${iso}T00:00:00` : iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

function hhmm(d) { return `${pad(d.getHours())}:${pad(d.getMinutes())}`; }

/* The form works in your own time, the server in UTC. Sending a local wall
 * clock with an offset lets the server do the conversion in one place rather
 * than every caller guessing. */
function localISO(dateStr, timeStr) {
  const [h, mi] = (timeStr || "00:00").split(":").map(Number);
  const [y, mo, d] = dateStr.split("-").map(Number);
  const dt = new Date(y, (mo || 1) - 1, d || 1, h || 0, mi || 0, 0);
  const off = -dt.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const oh = pad(Math.floor(Math.abs(off) / 60));
  const om = pad(Math.abs(off) % 60);
  return `${dateStr}T${pad(h || 0)}:${pad(mi || 0)}:00${sign}${oh}:${om}`;
}

/* Monday-first. Sunday-first grids put the weekend either side of the week,
 * which makes a weekend look like two unrelated days. */
function gridStart(year, month) {
  const first = new Date(year, month, 1);
  const back = (first.getDay() + 6) % 7;
  return new Date(year, month, 1 - back);
}

function spanDays(e) {
  const s = parse(e.starts_at);
  const raw = parse(e.ends_at) || s;
  if (!s) return [];
  // An all-day event's end is exclusive in Google's model: a single day runs
  // to the next midnight. Treating it inclusively paints an extra cell.
  const end = new Date(raw.getTime() - (e.all_day ? 1000 : 0));
  const out = [];
  const cur = new Date(s.getFullYear(), s.getMonth(), s.getDate());
  const last = new Date(end.getFullYear(), end.getMonth(), end.getDate());
  while (cur <= last && out.length < 60) {
    out.push(key(cur));
    cur.setDate(cur.getDate() + 1);
  }
  return out;
}

export async function agendaView(root, state) {
  const now = new Date();
  let year = state.calYear ?? now.getFullYear();
  let month = state.calMonth ?? now.getMonth();
  let selected = state.calDay ?? key(now);
  let byDay = new Map();
  let editing = null;           // null | {mode:'new'} | {mode:'edit', event}

  const grid = el("div", { class: "cal-grid" });
  const dayPanel = el("div", { class: "cal-day" });
  const title = el("h2", { class: "cal-title" });
  const status = el("div", { class: "hint" });

  function eventsOn(k) {
    return (byDay.get(k) || []).slice().sort((a, b) => {
      if (a.all_day !== b.all_day) return a.all_day ? -1 : 1;
      return String(a.starts_at).localeCompare(String(b.starts_at));
    });
  }

  /* The editor. One form for both making and changing an event, because they
   * are the same fields and two forms would drift apart. */
  function eventForm(existing, dayKey) {
    const err = el("div", { class: "error", role: "alert" });
    const start = existing ? parse(existing.starts_at) : null;
    const end = existing ? parse(existing.ends_at) : null;

    const f = {
      title: el("input", { class: "input", type: "text", id: "ev-title",
                           value: existing ? existing.title || "" : "",
                           placeholder: "What is it?" }),
      date: el("input", { class: "input", type: "date", id: "ev-date",
                          value: start ? key(start) : dayKey }),
      start: el("input", { class: "input", type: "time", id: "ev-start",
                           value: start && !existing.all_day ? hhmm(start) : "09:00" }),
      end: el("input", { class: "input", type: "time", id: "ev-end",
                         value: end && !existing.all_day ? hhmm(end) : "10:00" }),
      allDay: el("input", { class: "check", type: "checkbox", id: "ev-allday" }),
      location: el("input", { class: "input", type: "text", id: "ev-loc",
                              value: existing ? existing.location || "" : "",
                              placeholder: "Where? (optional)" }),
    };
    if (existing && existing.all_day) f.allDay.checked = true;

    const timed = el("div", { class: "ev-times" },
      el("div", { class: "field" },
        el("label", { class: "label", for: "ev-start", text: "From" }), f.start),
      el("div", { class: "field" },
        el("label", { class: "label", for: "ev-end", text: "To" }), f.end));

    function syncAllDay() { timed.hidden = f.allDay.checked; }
    f.allDay.addEventListener("change", syncAllDay);
    syncAllDay();

    const save = el("button", { class: "btn primary", type: "submit",
      text: existing ? "Save" : "Add event" });

    async function submit(e) {
      e.preventDefault();
      err.textContent = "";
      const allDay = f.allDay.checked;
      const body = {
        title: f.title.value.trim(),
        location: f.location.value.trim(),
        all_day: allDay,
        starts_at: localISO(f.date.value, allDay ? "00:00" : f.start.value),
        ends_at: allDay ? null : localISO(f.date.value, f.end.value),
      };
      if (!body.title) { err.textContent = "Give it a name."; f.title.focus(); return; }
      save.disabled = true;
      try {
        if (existing) await patch(`/api/edit/event/${existing.id}`, body);
        else await post("/api/edit/event", body);
        editing = null;
        await load();
      } catch (ex) {
        err.textContent = (ex && ex.message) || "That did not save.";
        save.disabled = false;
      }
    }

    const buttons = el("div", { class: "ev-buttons" }, save,
      el("button", { class: "btn", type: "button", text: "Cancel",
        onclick: () => { editing = null; renderPanel(); } }),
      existing
        ? el("button", { class: "btn danger", type: "button", text: "Delete",
            onclick: async () => {
              if (!window.confirm(`Delete "${existing.title || "this event"}"? `
                  + "It will be removed from Google Calendar too.")) return;
              try {
                await del(`/api/edit/event/${existing.id}`);
                editing = null;
                await load();
              } catch { err.textContent = "Could not delete it."; }
            } })
        : null);

    return el("form", { class: "ev-form", onsubmit: submit, novalidate: true },
      err,
      el("div", { class: "field" },
        el("label", { class: "label", for: "ev-title", text: "Event" }), f.title),
      el("div", { class: "ev-row" },
        el("div", { class: "field" },
          el("label", { class: "label", for: "ev-date", text: "Date" }), f.date),
        el("label", { class: "ev-allday", for: "ev-allday" },
          f.allDay, el("span", { text: "All day" }))),
      timed,
      el("div", { class: "field" },
        el("label", { class: "label", for: "ev-loc", text: "Location" }), f.location),
      buttons);
  }

  function renderPanel() {
    const d = parse(selected);
    const list = eventsOn(selected);
    const head = el("div", { class: "cal-day-head" },
      el("h3", { text: d
        ? `${DOW[(d.getDay() + 6) % 7]} ${d.getDate()} ${MONTH[d.getMonth()]}`
        : selected }),
      el("span", { class: "hint",
        text: list.length === 0 ? "Nothing scheduled"
            : list.length === 1 ? "1 event" : `${list.length} events` }),
      editing ? null : el("button", {
        class: "btn", type: "button", text: "Add event",
        onclick: () => { editing = { mode: "new" }; renderPanel(); } }));

    if (editing) {
      mount(dayPanel, head,
            eventForm(editing.mode === "edit" ? editing.event : null, selected));
      return;
    }

    const body = list.length === 0
      // Said plainly rather than drawn as an empty box. A free day is a good
      // thing and should not look like a loading failure.
      ? el("p", { class: "cal-free", text: "Free." })
      : el("ul", { class: "cal-day-list" }, ...list.map((e) => {
          const s = parse(e.starts_at);
          const en = parse(e.ends_at);
          const when = e.all_day ? "All day"
            : en && en > s ? `${hhmm(s)} – ${hhmm(en)}` : hhmm(s);
          return el("li", { class: "cal-day-item" },
            el("span", { class: "cal-when", text: when }),
            el("button", { class: "cal-what", type: "button",
              onclick: () => { editing = { mode: "edit", event: e }; renderPanel(); } },
              el("div", { class: "cal-title-row", text: e.title || "(no title)" }),
              e.location
                ? el("div", { class: "hint", text: e.location }) : null,
              e.description
                ? el("div", { class: "hint clamp",
                    text: String(e.description).slice(0, 240) }) : null,
              // An edit that has not reached Google, said plainly rather than
              // left to be noticed when the two copies disagree.
              e.dirty
                ? el("div", { class: "ev-pending",
                    text: e.push_error || "not sent to Google yet" })
                : null));
        }));
    mount(dayPanel, head, body);
  }

  function renderGrid() {
    title.textContent = `${MONTH[month]} ${year}`;
    const start = gridStart(year, month);
    const todayKey = key(new Date());
    const cells = [
      ...DOW.map((d) => el("div", { class: "cal-dow", text: d })),
    ];
    for (let i = 0; i < 42; i += 1) {
      const d = new Date(start.getFullYear(), start.getMonth(),
                         start.getDate() + i);
      const k = key(d);
      const outside = d.getMonth() !== month;
      const list = eventsOn(k);
      const cell = el("button", {
        type: "button",
        class: "cal-cell"
          + (outside ? " outside" : "")
          + (k === todayKey ? " today" : "")
          + (k === selected ? " selected" : ""),
        "aria-label": `${d.getDate()} ${MONTH[d.getMonth()]}, `
          + (list.length ? `${list.length} events` : "no events"),
        "aria-pressed": k === selected ? "true" : "false",
        onclick: () => {
          selected = k; state.calDay = k;
          editing = null;      // a form left open on another day is a trap
          renderGrid(); renderPanel();
        },
      },
        el("span", { class: "cal-num", text: String(d.getDate()) }),
        // Up to three chips, then a count. A cell that grows to fit its
        // busiest day destroys the even rhythm the grid exists to give.
        el("span", { class: "cal-chips" },
          ...list.slice(0, 3).map((e) => el("span", {
            class: "cal-chip" + (e.all_day ? " allday" : ""),
            title: e.title || "",
          },
            e.all_day ? null : el("span", { class: "cal-chip-t",
              text: hhmm(parse(e.starts_at)) }),
            el("span", { class: "cal-chip-x", text: e.title || "(no title)" }))),
          list.length > 3
            ? el("span", { class: "cal-more", text: `+${list.length - 3} more` })
            : null));
      cells.push(cell);
    }
    mount(grid, ...cells);
  }

  async function load() {
    // A whole grid, not a whole month: the leading and trailing cells belong
    // to neighbouring months and would otherwise always look empty.
    const start = gridStart(year, month);
    const end = new Date(start.getFullYear(), start.getMonth(),
                         start.getDate() + 41);
    status.textContent = "Loading…";
    try {
      const r = await get(`/api/view/calendar?from=${key(start)}&to=${key(end)}`);
      byDay = new Map();
      for (const e of r.events || []) {
        for (const k of spanDays(e)) {
          if (!byDay.has(k)) byDay.set(k, []);
          byDay.get(k).push(e);
        }
      }
      const n = (r.events || []).length;
      status.textContent = n === 0
        ? "No events this month."
        : `${n} event${n === 1 ? "" : "s"} this month`;
    } catch {
      status.textContent = "Could not load the calendar.";
    }
    renderGrid();
    renderPanel();
  }

  function go(delta) {
    const d = new Date(year, month + delta, 1);
    year = d.getFullYear(); month = d.getMonth();
    state.calYear = year; state.calMonth = month;
    load();
  }

  const nav = el("div", { class: "cal-nav" },
    el("button", { class: "btn ghost", type: "button", text: "‹",
      "aria-label": "Previous month", onclick: () => go(-1) }),
    title,
    el("button", { class: "btn ghost", type: "button", text: "›",
      "aria-label": "Next month", onclick: () => go(1) }),
    el("button", { class: "btn", type: "button", text: "Today",
      onclick: () => {
        const t = new Date();
        year = t.getFullYear(); month = t.getMonth(); selected = key(t);
        state.calYear = year; state.calMonth = month; state.calDay = selected;
        load();
      } }),
    status);

  mount(root, el("section", { class: "cal" }, nav, grid, dayPanel));
  await load();
}
