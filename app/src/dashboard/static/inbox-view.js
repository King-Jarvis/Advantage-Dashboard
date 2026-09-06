/* What wants you.
 *
 * Deliberately not a mail client. It answers one question -- is there anything
 * here I should deal with -- and offers only the actions that follow from the
 * answer: archive it, star it, or tell the classifier it was wrong. Reading
 * and replying happen in a mail client, which is already good at it.
 *
 * The hard part is not showing mail, it is showing less of it without hiding
 * anything. A threshold that silently drops twenty-six of forty messages is
 * indistinguishable from a broken sync. So: everything is reachable, the
 * filter is visible and says what it is holding back, and rank is carried by
 * weight and order rather than by removal.
 *
 * Every string here comes from someone else -- subjects, sender names, the
 * classifier's reason. All of it is set with textContent, and CI rejects any
 * markup sink in this directory.
 */
import { get, patch } from "./api.js";
import { el, mount } from "./dom.js";

const WORDS = { 5: "urgent", 4: "important", 3: "worth a look",
                2: "low", 1: "noise" };

const BANDS = [
  { id: "all", label: "All", min: 0 },
  { id: "attention", label: "Needs you", min: 3 },
  { id: "unread", label: "Unread", min: 0, unread: true },
  { id: "starred", label: "Starred", min: 0, starred: true },
];

/* Gmail's own view of the thread. We deliberately do not compose, so this is
 * the honest way to reply rather than pretending the feature is missing. */
function gmailLink(m) {
  return `https://mail.google.com/mail/u/0/#all/${encodeURIComponent(m.thread_id || "")}`;
}

function ago(iso) {
  const then = new Date(String(iso).length <= 10 ? `${iso}T00:00:00` : iso);
  if (Number.isNaN(then.getTime())) return "";
  const mins = Math.round((Date.now() - then.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  const days = Math.round(hrs / 24);
  return days < 7 ? `${days}d` : then.toISOString().slice(5, 10);
}

function tone(n) {
  if (n >= 5) return "danger";
  if (n === 4) return "warn";
  if (n === 3) return "info";
  return "mute";
}

export async function inboxView(root, state) {
  let all = [];
  let band = state.inboxBand || "all";
  let busy = new Set();
  let openId = null;
  const bodies = new Map();     // id -> text, so reopening never refetches

  const list = el("div", { class: "mail-list" });
  const status = el("div", { class: "hint" });
  const chips = el("div", { class: "chips" });

  function shown() {
    const b = BANDS.find((x) => x.id === band) || BANDS[0];
    // One predicate for both the list and the chip counts, so a chip can
    // never say 12 and show 11.
    return all.filter((m) => matches(m, b));
  }

  function matches(m, b) {
    const score = m.importance_override ?? m.importance ?? 0;
    // Something you binned should not sit in the list looking undecided.
    if (m.trashed) return false;
    if (b.unread && !m.is_unread) return false;
    if (b.starred && !m.is_starred) return false;
    return score >= b.min;
  }

  function count(b) {
    return all.filter((m) => matches(m, b)).length;
  }

  async function openRow(m) {
    // Toggling closed must not clear the cache: reopening is free.
    if (openId === m.id) { openId = null; render(); return; }
    openId = m.id;
    // Opening a message is reading it, so say so -- and only once.
    if (m.is_unread) act(m, { is_unread: 0 });
    render();
    if (bodies.has(m.id)) return;
    bodies.set(m.id, null);                        // null == in flight
    try {
      const r = await get(`/api/view/message/${m.id}`);
      bodies.set(m.id, r.body || "");
    } catch (err) {
      bodies.set(m.id,
        err && err.status === 502
          ? "Gmail would not return this message."
          : "Could not load this message.");
    }
    if (openId === m.id) render();
  }

  async function act(m, patchBody) {
    if (busy.has(m.id)) return;
    busy.add(m.id);
    try {
      await patch(`/api/edit/message/${m.id}`, patchBody);
      Object.assign(m, patchBody);
    } catch {
      status.textContent = "That change did not save.";
    } finally {
      busy.delete(m.id);
      render();
    }
  }

  function row(m) {
    const score = m.importance_override ?? m.importance ?? 0;
    const corrected = m.importance_override != null
      && m.importance_override !== m.importance;

    // The score sits in a fixed column so the eye can run down it. A pill
    // that moves with the text length cannot be scanned.
    const rank = el("div", { class: `mail-rank ${tone(score)}` },
      el("span", { class: "mail-score", text: String(score || "–") }),
      el("span", { class: "mail-word", text: WORDS[score] || "" }));

    const head = el("div", { class: "mail-head" },
      el("span", { class: "mail-from", text: m.sender || m.sender_email || "unknown" }),
      el("span", { class: "mail-when", text: ago(m.received_at) }));

    const subject = el("div", {
      class: "mail-subject" + (m.is_unread ? " unread" : ""),
      text: m.subject || "(no subject)",
    });

    const why = m.reason
      ? el("div", { class: "mail-why", text: corrected ? `you set this — ${m.reason}` : m.reason })
      : null;

    const snippet = m.snippet
      ? el("div", { class: "mail-snip clamp", text: m.snippet }) : null;

    // Correcting the score is the whole point of showing it, so it is a
    // control rather than a label -- but a quiet one, off to the side.
    const grade = el("div", { class: "mail-grade" },
      ...[1, 2, 3, 4, 5].map((n) => el("button", {
        type: "button",
        class: "grade" + (n === score ? " on" : ""),
        title: `Mark as ${WORDS[n]}`,
        "aria-label": `Mark as ${WORDS[n]}`,
        onclick: (e) => { e.stopPropagation(); act(m, { importance_override: n }); },
      }, el("span", { text: String(n) }))));

    function icon(cls, label, glyph, patchBody, confirm) {
      return el("button", {
        type: "button", class: "iconbtn" + cls, title: label,
        "aria-label": label,
        onclick: (e) => {
          e.stopPropagation();
          // Trash and spam are the two a mis-click actually costs you.
          if (confirm && !window.confirm(confirm)) return;
          act(m, patchBody);
        },
      }, el("span", { text: glyph }));
    }

    const actions = el("div", { class: "mail-actions" },
      icon(m.is_starred ? " on" : "", m.is_starred ? "Unstar" : "Star",
           m.is_starred ? "★" : "☆", { is_starred: m.is_starred ? 0 : 1 }),
      icon("", m.is_unread ? "Mark as read" : "Mark as unread",
           m.is_unread ? "○" : "●", { is_unread: m.is_unread ? 0 : 1 }),
      icon("", m.archived ? "Move to inbox" : "Archive",
           m.archived ? "↩" : "✓", { archived: m.archived ? 0 : 1 }),
      icon("", m.is_spam ? "Not spam" : "Report spam", "⌀",
           { is_spam: m.is_spam ? 0 : 1 },
           m.is_spam ? null : "Report this as spam? It moves out of your inbox in Gmail."),
      icon(" danger", "Move to bin", "🗑",
           { trashed: 1 },
           "Move this to the bin in Gmail?"));

    const isOpen = openId === m.id;
    const body = bodies.get(m.id);
    const expanded = !isOpen ? null : el("div", { class: "mail-open" },
      body === undefined || body === null
        ? el("p", { class: "hint", text: "Loading…" })
        : body === ""
          ? el("p", { class: "hint", text: "This message has no text — "
              + "it may be an image or an attachment." })
          : el("pre", { class: "mail-text", text: body }),
      el("div", { class: "mail-open-foot" },
        el("a", { class: "btn", href: gmailLink(m), target: "_blank",
                  rel: "noopener noreferrer", text: "Open in Gmail" }),
        el("span", { class: "hint",
          text: "Replying happens in Gmail — this is a reader, not a client." })));

    // A push that has not landed is worth saying out loud: the edit is real
    // locally and Gmail has not accepted it yet.
    const stuck = m.push_error
      ? el("div", { class: "mail-stuck", text: `Not sent to Gmail yet: ${m.push_error}` })
      : null;

    return el("article", {
      class: "mail-row" + (m.is_unread ? " is-unread" : "")
           + (score <= 1 ? " is-quiet" : "") + (isOpen ? " is-open" : ""),
    }, rank,
       el("div", { class: "mail-body" },
         el("button", { class: "mail-hit", type: "button",
           "aria-expanded": isOpen ? "true" : "false",
           onclick: () => openRow(m) },
           head, subject, isOpen ? null : snippet),
         why, stuck, expanded, grade),
       actions);
  }

  function render() {
    mount(chips, ...BANDS.map((b) => el("button", {
      type: "button",
      class: "chip" + (b.id === band ? " on" : ""),
      "aria-pressed": b.id === band ? "true" : "false",
      onclick: () => { band = b.id; state.inboxBand = b.id; render(); },
    }, el("span", { text: b.label }),
       el("span", { class: "chip-n", text: String(count(b)) }))));

    const rows = shown();
    if (rows.length === 0) {
      mount(list, el("p", { class: "cal-free",
        text: all.length === 0
          ? "Nothing has synced yet."
          : "Nothing in this filter." }));
    } else {
      mount(list, ...rows.map(row));
    }
    // Always say what is not being shown. A filter you cannot see is the same
    // thing as a bug, from the outside.
    const hidden = all.length - rows.length;
    status.textContent = hidden > 0
      ? `${rows.length} shown · ${hidden} not in this filter`
      : `${rows.length} message${rows.length === 1 ? "" : "s"}`;
  }

  async function load() {
    status.textContent = "Loading…";
    try {
      // Ask for everything and filter here: the server's threshold is a
      // default for the widget, not a cage for this screen.
      const r = await get("/api/view/inbox?min_importance=0&archived=1");
      all = (r.messages || []).slice().sort((a, b) => {
        const sa = a.importance_override ?? a.importance ?? 0;
        const sb = b.importance_override ?? b.importance ?? 0;
        if (sa !== sb) return sb - sa;
        return String(b.received_at).localeCompare(String(a.received_at));
      });
    } catch {
      status.textContent = "Could not load your mail.";
      return;
    }
    render();
  }

  mount(root, el("section", { class: "mail" },
    el("div", { class: "mail-bar" }, chips, status), list));
  await load();
}
