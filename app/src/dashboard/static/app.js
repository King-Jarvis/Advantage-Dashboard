/* Shell and router.
 *
 * Views are nodes on a canvas, in n8n's sense: a rounded surface with a
 * coloured left bar saying which part of your life it belongs to, legible
 * before the heading is read.
 */
import { ApiError, get, setCsrf } from "./api.js";
import { el, mount } from "./dom.js";
import { loginView } from "./login.js";
import { setupView } from "./setup-view.js";
import { budgetView } from "./budget-view.js";
import { overviewView } from "./overview-view.js";
import { homeView } from "./home-view.js";
import { settingsView } from "./settings-view.js";
import { importView } from "./import-view.js";
import { agendaView } from "./agenda-view.js";
import { inboxView } from "./inbox-view.js";

const root = document.getElementById("root");

const VIEWS = [
  { id: "home",   label: "Home",   kind: "system" },
  { id: "agenda", label: "Agenda", kind: "agenda" },
  { id: "inbox",  label: "Inbox",  kind: "inbox" },
  { id: "budget", label: "Budget", kind: "budget" },
  { id: "import", label: "Import", kind: "budget" },
  { id: "settings", label: "Settings", kind: "system" },
];

const state = {
  user: null, view: "home", month: thisMonth(),
  // The budget tab has two screens: the chart you read, and the list you
  // edit. Kept apart because reading and editing want different layouts.
  editing: false, focusCategory: null,
};

/* Routing lives in the URL fragment.
 *
 * Not for cleverness: without it the back button leaves the app entirely,
 * a reload always lands on the overview, and no screen can be linked to.
 * A fragment keeps all of that working with no server-side routing. */
function readHash() {
  const parts = (location.hash || "").replace(/^#\/?/, "").split("/");
  if (VIEWS.some((v) => v.id === parts[0])) state.view = parts[0];
  state.editing = parts[0] === "budget" && parts[1] === "edit";
  if (/^\d{4}-\d{2}$/.test(parts[2] || "")) state.month = parts[2];
}

function writeHash() {
  const parts = [state.view];
  if (state.view === "budget" && state.editing) parts.push("edit", state.month);
  const next = "#/" + parts.join("/");
  if (location.hash !== next) {
    // replaceState, not a new entry: month paging would otherwise fill the
    // history with steps nobody wants to walk back through.
    history.replaceState(null, "", next);
  }
}

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
      onclick: () => go({ view: v.id, editing: false }),
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
      onclick: () => go({ editing: false, focusCategory: null }),
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
async function go(patchState) {
  Object.assign(state, patchState);
  writeHash();
  await render();
}

async function render() {
  if (!state.user) {
    let googleEnabled = false;
    let needsSetup = false;
    try {
      const cfg = await get("/api/config");
      googleEnabled = Boolean(cfg.google_enabled);
      needsSetup = Boolean(cfg.needs_setup);
    } catch { /* a config failure must not block the password form */ }
    // An install with no users has nothing to sign in to, so offering a
    // password form could only ever fail.
    if (needsSetup) setupView(root, { onSignedIn: boot });
    else loginView(root, { googleEnabled, onSignedIn: boot });
    return;
  }

  // The home screen fills the page; the others are a reading column.
  const body = el("main", {
    class: "canvas" + (state.view === "home" ? " home" : ""),
  });
  mount(root, topbar(), body);

  if (state.view === "home") {
    try {
      await homeView(body, {
        onGo: (v) => go({ view: v, editing: false }),
        onSettings: () => go({ view: "settings" }),
      });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        state.user = null;
        await render();
        return;
      }
      mount(body, placeholder("system", "Home", "Could not load the summary"));
    }
  } else if (state.view === "import") {
    try {
      await importView(body, { onGo: (v) => go({ view: v }) });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        state.user = null;
        await render();
        return;
      }
      mount(body, placeholder("budget", "Import", "Could not load the importer"));
    }
  } else if (state.view === "settings") {
    try {
      await settingsView(body, { onGo: (v) => go({ view: v }) });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        state.user = null;
        await render();
        return;
      }
      mount(body, placeholder("system", "Settings", "Could not load settings"));
    }
  } else if (state.view === "budget") {
    try {
      const onMonth = (m) => go({ month: m });
      if (state.editing) {
        mount(body, backBar(), el("div", { id: "edit-body" }));
        await budgetView(document.getElementById("edit-body"),
                         state.month, onMonth);
      } else {
        await overviewView(body, state.month, {
          onMonth,
          onEdit: (categoryId) => go({ editing: true, focusCategory: categoryId }),
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
    try {
      await agendaView(body, state);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        state.user = null;
        await render();
        return;
      }
      mount(body, placeholder("agenda", "Agenda", "Could not load the agenda"));
    }
  } else {
    try {
      await inboxView(body, state);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        state.user = null;
        await render();
        return;
      }
      mount(body, placeholder("inbox", "Inbox", "Could not load the inbox"));
    }
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
  readHash();
  writeHash();
  render();
}

// The back button, and pasted links, both arrive as a hash change.
window.addEventListener("hashchange", () => {
  readHash();
  render().catch(() => { /* a failed re-render must not break navigation */ });
});

boot();
