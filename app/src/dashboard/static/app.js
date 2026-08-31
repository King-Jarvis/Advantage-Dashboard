/* Shell and router.
 *
 * Views are nodes on a canvas, in n8n's sense: a rounded surface with a
 * coloured left bar saying which part of your life it belongs to, legible
 * before the heading is read.
 */
import { ApiError, get, setCsrf } from "./api.js";
import { el, mount } from "./dom.js";
import { loginView } from "./login.js";
import { budgetView } from "./budget-view.js";
import { overviewView } from "./overview-view.js";

const root = document.getElementById("root");

const VIEWS = [
  { id: "agenda", label: "Agenda", kind: "agenda" },
  { id: "inbox",  label: "Inbox",  kind: "inbox" },
  { id: "budget", label: "Budget", kind: "budget" },
];

const state = {
  user: null, view: "budget", month: thisMonth(),
  // The budget tab has two screens: the chart you read, and the list you
  // edit. Kept apart because reading and editing want different layouts.
  editing: false, focusCategory: null,
};

function thisMonth() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

/* ── chrome ─────────────────────────────────────────────────────────────── */
function node(kind, title, ...body) {
  return el("section", { class: "node", dataset: { kind } },
    el("header", { class: "node-head" },
      el("span", { class: "node-title", text: title })),
    el("div", { class: "node-body" }, ...body));
}

function topbar() {
  const tabs = el("nav", { class: "tabs" },
    ...VIEWS.map((v) => el("button", {
      class: "tab", type: "button",
      "aria-current": state.view === v.id ? "page" : null,
      onclick: () => { state.view = v.id; render(); },
      text: v.label,
    })));

  return el("header", { class: "topbar" },
    el("div", { class: "brandmark" },
      el("span", { class: "dot" }), el("span", { text: "Dashboard" })),
    tabs,
    el("div", { class: "spacer" }),
    el("span", { class: "label", text: state.user || "" }),
    el("button", {
      class: "btn ghost", type: "button", text: "Sign out",
      onclick: async () => {
        try { await (await import("./api.js")).post("/api/auth/logout"); }
        finally { setCsrf(null); state.user = null; render(); }
      },
    }));
}

/* ── views ──────────────────────────────────────────────────────────────── */
function backBar() {
  return el("div", { class: "row backbar" },
    el("button", {
      class: "btn ghost", type: "button", text: "‹ Overview",
      onclick: () => { state.editing = false; state.focusCategory = null; render(); },
    }),
    el("span", { class: "label", text: "editing budget" }));
}

function placeholder(kind, title, line) {
  return node(kind, title,
    el("div", { class: "empty" },
      el("div", { class: "big", text: line }),
      el("div", { class: "hint",
        text: "Connect a Google account to populate this." })));
}

/* ── render ─────────────────────────────────────────────────────────────── */
async function render() {
  if (!state.user) {
    let googleEnabled = false;
    try {
      const cfg = await get("/api/config");
      googleEnabled = Boolean(cfg.google_enabled);
    } catch { /* a config failure must not block the password form */ }
    loginView(root, { googleEnabled, onSignedIn: boot });
    return;
  }

  const body = el("main", { class: "canvas" });
  mount(root, topbar(), body);

  if (state.view === "budget") {
    try {
      const onMonth = (m) => { state.month = m; render(); };
      if (state.editing) {
        mount(body, backBar(), el("div", { id: "edit-body" }));
        await budgetView(document.getElementById("edit-body"),
                         state.month, onMonth);
      } else {
        await overviewView(body, state.month, {
          onMonth,
          onEdit: (categoryId) => {
            state.editing = true;
            state.focusCategory = categoryId;
            render();
          },
        });
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        state.user = null;
        render();
        return;
      }
      mount(body, el("div", { class: "empty" },
        el("div", { class: "big", text: "Could not load the budget" })));
    }
  } else if (state.view === "agenda") {
    mount(body, placeholder("agenda", "Agenda", "Nothing scheduled"));
  } else {
    mount(body, placeholder("inbox", "Inbox", "Nothing needs you"));
  }
}

async function boot() {
  try {
    const me = await get("/api/auth/whoami");
    state.user = me.username;
    setCsrf(me.csrf_token);
  } catch {
    state.user = null;
  }
  render();
}

boot();
