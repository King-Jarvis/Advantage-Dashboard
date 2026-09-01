/* Settings: connected accounts, credentials, and how the dashboard behaves.
 *
 * Grouped rather than listed flat, because a single column of twenty controls
 * gives no sense of what belongs to what. Each control states what it does in
 * a sentence, so nothing has to be guessed from its name.
 *
 * Secrets are write-only. The server never returns one, so a field can show
 * that a key is set and offer to replace it, but never reveal it -- which
 * also means a stolen session cannot harvest credentials.
 */
import { del, get, patch } from "./api.js";
import { el, mount } from "./dom.js";

const GROUPS = [
  ["Accounts", []],
  ["Credentials", ["google_client_id", "google_client_secret",
                   "anthropic_api_key"]],
  ["Assistance", ["enable_llm_categories", "enable_spending_analysis",
                  "classify_model"]],
  ["Budget engine", ["baseline_window_months", "min_months_for_suggestion",
                     "month_start_day", "currency_symbol"]],
  ["Syncing", ["mail_poll_seconds", "calendar_poll_seconds",
               "inbox_min_importance", "timezone"]],
];

const state = { data: null, note: "", error: "" };

function control(item, onChange) {
  const id = `set-${item.key}`;
  if (item.kind === "bool") {
    const box = el("input", { type: "checkbox", id });
    box.checked = Boolean(item.value);
    box.addEventListener("change", () => onChange(item.key, box.checked));
    return box;
  }
  if (item.kind === "secret") {
    const input = el("input", {
      class: "input", type: "password", id, autocomplete: "new-password",
      placeholder: item.is_set ? "•••••••• (set — type to replace)" : "not set",
    });
    const save = el("button", {
      class: "btn", type: "button", text: "Save",
      onclick: () => {
        if (!input.value) return;
        onChange(item.key, input.value);
        input.value = "";
      } });
    const clear = item.is_set ? el("button", {
      class: "btn danger", type: "button", text: "Clear",
      onclick: () => onChange(item.key, "", { clear: true }) }) : null;
    return el("div", { class: "row secretrow" }, input, save, clear);
  }
  const input = el("input", {
    class: "input", id,
    type: item.kind === "int" ? "number" : "text",
    value: item.value === null || item.value === undefined ? "" : String(item.value),
  });
  const commit = () => {
    const v = item.kind === "int" ? Number(input.value) : input.value;
    if (item.kind === "int" && !Number.isFinite(v)) return;
    if (String(v) !== String(item.value)) onChange(item.key, v);
  };
  input.addEventListener("blur", commit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
  });
  return input;
}

function settingRow(item, onChange) {
  return el("div", { class: "setrow" },
    el("label", { class: "setlabel", for: `set-${item.key}` },
      el("span", { class: "setname", text: item.label || item.key }),
      el("span", { class: "hint", text: item.description })),
    el("div", { class: "setcontrol" }, control(item, onChange)));
}

/* ── accounts ───────────────────────────────────────────────────────────── */
function accountsSection(refresh) {
  const g = state.data.google;
  const rows = g.accounts.map((a) => el("div", { class: "setrow acct" },
    el("div", {},
      el("div", { class: "setname", text: a.email }),
      el("div", { class: "hint",
        text: (a.scopes || "").replace(/https:\/\/www\.googleapis\.com\/auth\//g, "")
              || "no scopes recorded" }),
      a.last_error
        ? el("div", { class: "hint err", text: `last sync: ${a.last_error}` })
        : null),
    el("div", { class: "setcontrol" },
      el("button", { class: "btn danger", type: "button", text: "Disconnect",
        onclick: async () => {
          if (!window.confirm(`Disconnect ${a.email}?`)) return;
          await del(`/api/google/accounts/${a.id}`);
          refresh();
        } }))));

  // A live check rather than an instruction to restart. Credentials are read
  // per request, so this reports the state the next sign-in will actually
  // see -- which is the confirmation a form should give you.
  const checkOut = el("div", { class: "checkout" });
  const check = el("button", {
    class: "btn", type: "button", text: "Check connection",
    onclick: async () => {
      mount(checkOut, el("span", { class: "hint", text: "Checking…" }));
      try {
        const r = await get("/api/google/check");
        mount(checkOut,
          el("div", { class: "row wrap" },
            el("span", { class: `pill ${r.configured ? "ok" : "warn"}`,
              text: r.configured ? "ready" : "not configured yet" }),
            el("span", { class: "hint",
              text: r.has_client_id ? `client ${r.client_id_hint}`
                                    : "no client ID" }),
            el("span", { class: "hint",
              text: r.has_client_secret ? "secret set" : "no secret" })),
          el("div", { class: "hint",
            text: `Redirect URI to register with Google: ${r.redirect_uri}` }),
          r.configured
            ? el("div", { class: "pad" },
                el("a", { class: "btn primary", href: "/api/google/connect",
                          text: "Connect a Google account" }))
            : null);
      } catch (err) {
        mount(checkOut, el("div", { class: "error",
          text: err.message || "Could not check." }));
      }
    } });

  const connect = el("div", {},
    el("div", { class: "row wrap" },
      g.configured
        ? el("a", { class: "btn primary", href: "/api/google/connect",
                    text: "Connect a Google account" })
        : el("span", { class: "hint",
            text: "Enter the client ID and secret below, then check." }),
      check),
    checkOut);

  return el("div", { class: "setgroup" },
    el("div", { class: "setgroup-head" },
      el("span", { class: "label", text: "Accounts" }),
      el("div", { class: "spacer" }),
      el("span", { class: `pill ${g.connected ? "ok" : "warn"}`,
        text: g.connected ? `${g.accounts.length} connected` : "none connected" })),
    ...(rows.length ? rows : [el("div", { class: "hint pad",
      text: "Mail and calendar stay empty until an account is connected." })]),
    el("div", { class: "pad" }, connect));
}

/* ── the view ───────────────────────────────────────────────────────────── */
export async function settingsView(container, { onGo } = {}) {
  state.data = await get("/api/settings");
  const byKey = Object.fromEntries(state.data.settings.map((s) => [s.key, s]));

  const refresh = () => settingsView(container, { onGo });

  async function onChange(key, value, opts = {}) {
    state.note = state.error = "";
    try {
      if (opts.clear) await del(`/api/settings/${key}`);
      else await patch("/api/settings", { [key]: value });
      state.note = "Saved.";
    } catch (err) {
      state.error = err.message || "Could not save that.";
    }
    refresh();
  }

  const sections = GROUPS.map(([title, keys]) => {
    if (title === "Accounts") return accountsSection(refresh);
    return el("div", { class: "setgroup" },
      el("div", { class: "setgroup-head" },
        el("span", { class: "label", text: title })),
      ...keys.filter((k) => byKey[k]).map((k) => settingRow(byKey[k], onChange)));
  });

  const warn = state.data.secrets_available ? null : el("div", { class: "error",
    text: "No encryption key is configured, so credentials cannot be stored. "
        + "Run scripts/bootstrap.sh and set TOKEN_KEY_PATH." });

  mount(container,
    el("section", { class: "node", dataset: { kind: "system" } },
      el("header", { class: "node-head" },
        el("span", { class: "node-title", text: "Settings" }),
        el("div", { class: "spacer" }),
        state.note ? el("span", { class: "pill ok", text: state.note }) : null),
      el("div", { class: "node-body" },
        warn,
        state.error ? el("div", { class: "error", text: state.error }) : null,
        el("div", { class: "hint",
          text: "Credentials are encrypted before they are stored and are "
              + "never sent back to the browser." }),
        ...sections)));
}
