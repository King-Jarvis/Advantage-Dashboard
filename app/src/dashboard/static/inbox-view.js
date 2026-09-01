/* What wants you.
 *
 * Deliberately not a mail client. It answers one question -- is there
 * anything here I should deal with -- and offers only the actions that follow
 * from the answer: archive it, star it, or tell the classifier it was wrong.
 * Reading and replying happen in a mail client, which is already good at it.
 *
 * Every string here comes from someone else: subjects, sender names, and the
 * model's own reason text. All of it is set with textContent, and CI rejects
 * any markup sink in this directory.
 */
import { get, patch } from "./api.js";
import { el, mount } from "./dom.js";

const WORDS = { 5: "urgent", 4: "important", 3: "worth a look",
                2: "low", 1: "noise" };

function ago(iso) {
  const then = new Date(iso.length <= 10 ? `${iso}T00:00:00` : iso);
  if (Number.isNaN(then.getTime())) return "";
  const mins = Math.round((Date.now() - then.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  return days < 7 ? `${days}d ago` : then.toISOString().slice(0, 10);
}

function scoreTone(n) {
  return n >= 5 ? "danger" : n >= 4 ? "warn" : n >= 3 ? "info" : "";
}

function messageRow(m, refresh) {
  const corrected = m.importance_override !== null
                 && m.importance_override !== undefined;

  const scorePick = el("select", { class: "input scorepick",
                                   "aria-label": `Importance of ${m.subject}` });
  for (const n of [5, 4, 3, 2, 1]) {
    const o = el("option", { value: String(n), text: `${n} · ${WORDS[n]}` });
    if (Number(m.score) === n) o.selected = true;
    scorePick.append(o);
  }
  scorePick.addEventListener("change", async () => {
    await patch(`/api/edit/message/${m.id}`,
                { importance_override: Number(scorePick.value) });
    refresh();
  });

  return el("div", { class: "msgrow" + (m.is_unread ? " unread" : "") },
    el("span", { class: `pill ${scoreTone(m.score)}`, text: String(m.score) }),
    el("div", { class: "grow" },
      el("div", { class: "msgsubject", text: m.subject || "(no subject)" }),
      el("div", { class: "msgmeta" },
        el("span", { text: m.sender || m.sender_email || "unknown sender" }),
        el("span", { class: "dot", text: "·" }),
        el("span", { text: ago(m.received_at) }),
        m.account_email
          ? el("span", { class: "hint", text: ` · ${m.account_email}` })
          : null),
      // Why the classifier scored it this way. Shown rather than hidden: a
      // score with no reason is something to either trust blindly or ignore,
      // and neither is useful.
      m.reason ? el("div", { class: "msgreason", text: m.reason }) : null,
      corrected
        ? el("div", { class: "hint", text: "you corrected this score" })
        : null),
    scorePick,
    el("button", { class: "btn ghost", type: "button",
      text: m.is_starred ? "★" : "☆",
      "aria-label": m.is_starred ? "Unstar" : "Star",
      onclick: async () => {
        await patch(`/api/edit/message/${m.id}`, { is_starred: !m.is_starred });
        refresh();
      } }),
    el("button", { class: "btn", type: "button", text: "Archive",
      onclick: async () => {
        await patch(`/api/edit/message/${m.id}`, { archived: true });
        refresh();
      } }));
}

export async function inboxView(container, { onSettings } = {}) {
  const d = await get("/api/view/inbox");
  const refresh = () => inboxView(container, { onSettings });

  if (!d.messages.length) {
    const status = await get("/api/view/status").catch(() => ({ accounts: [] }));
    const connected = (status.accounts || []).length > 0;
    return mount(container,
      el("section", { class: "node", dataset: { kind: "inbox" } },
        el("header", { class: "node-head" },
          el("span", { class: "node-title", text: "Inbox" })),
        el("div", { class: "node-body" },
          el("div", { class: "empty" },
            el("div", { class: "big",
              text: connected ? "Nothing needs you" : "Not connected" }),
            el("div", { class: "hint",
              text: connected
                ? `Nothing has scored ${d.min_importance} or above since the `
                  + "last check."
                : "Connect a Google account in Settings to see what needs you." }),
            connected ? null : el("div", { class: "pad" },
              el("button", { class: "btn primary", type: "button",
                             text: "Open Settings",
                             onclick: () => onSettings && onSettings() }))))));
  }

  mount(container,
    el("section", { class: "node", dataset: { kind: "inbox" } },
      el("header", { class: "node-head" },
        el("span", { class: "node-title", text: "Inbox" }),
        el("div", { class: "spacer" }),
        el("span", { class: "label",
          text: `${d.messages.length} scoring ${d.min_importance}+` })),
      el("div", { class: "node-body flush" },
        ...d.messages.map((m) => messageRow(m, refresh)))));
}
