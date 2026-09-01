/* The very first screen: claiming a fresh installation.
 *
 * Shown instead of the sign-in form while the database has no users. It asks
 * for the code the server printed on startup, which is what stops a guest on
 * the wifi from claiming a dashboard that is about to hold your mail,
 * calendar and finances.
 *
 * The code is a nuisance for exactly one screen, once, and then never again.
 * That seemed a fair trade for the alternative, which is that whoever loads
 * the page first wins.
 */
import { post, setCsrf } from "./api.js";
import { el, mount } from "./dom.js";

const MIN_PASSWORD = 12;

export function setupView(root, { onSignedIn }) {
  const errorBox = el("div", { class: "error", role: "alert" });

  const token = el("input", {
    class: "input mono", type: "text", id: "token", name: "token",
    autocomplete: "off", spellcheck: false, required: true,
  });
  const username = el("input", {
    class: "input", type: "text", id: "username", name: "username",
    autocomplete: "username", required: true,
  });
  const password = el("input", {
    class: "input", type: "password", id: "password", name: "password",
    autocomplete: "new-password", required: true,
  });
  const confirm = el("input", {
    class: "input", type: "password", id: "confirm", name: "confirm",
    autocomplete: "new-password", required: true,
  });

  // Said before it is typed rather than after it is rejected. A rule you
  // discover by failing is a worse rule than one you were told.
  const strength = el("div", { class: "hint",
    text: `At least ${MIN_PASSWORD} characters. This one account protects `
        + `everything the dashboard holds.` });
  password.addEventListener("input", () => {
    const n = password.value.length;
    strength.textContent = n === 0
      ? `At least ${MIN_PASSWORD} characters. This one account protects `
        + `everything the dashboard holds.`
      : n < MIN_PASSWORD
        ? `${MIN_PASSWORD - n} more character${MIN_PASSWORD - n === 1 ? "" : "s"}.`
        : "Long enough.";
    strength.classList.toggle("err", n > 0 && n < MIN_PASSWORD);
  });

  const submit = el("button", { class: "btn primary", type: "submit",
    text: "Create account" });

  async function claim(ev) {
    ev.preventDefault();
    errorBox.textContent = "";
    if (password.value !== confirm.value) {
      errorBox.textContent = "The two passwords do not match.";
      confirm.value = ""; confirm.focus();
      return;
    }
    submit.disabled = true;
    try {
      const res = await post("/api/setup/claim", {
        token: token.value.trim(),
        username: username.value.trim(),
        password: password.value,
      });
      setCsrf(res.csrf_token);
      onSignedIn();
    } catch (err) {
      // The server's message is the specific one -- wrong code, password too
      // short, already claimed. Replacing it with a generic line here would
      // throw away the only thing that tells you which.
      errorBox.textContent = err.status === 400 && err.message
        ? err.message
        : "Could not complete setup. Try again.";
      submit.disabled = false;
    }
  }

  const form = el("form", { onsubmit: claim, novalidate: true },
    el("div", { class: "field" },
      el("label", { class: "label", for: "token", text: "Setup code" }),
      el("div", { class: "hint",
        text: "Printed by the installer, and in the server's output." }),
      token),
    el("div", { class: "field" },
      el("label", { class: "label", for: "username", text: "Username" }),
      username),
    el("div", { class: "field" },
      el("label", { class: "label", for: "password", text: "Password" }),
      password, strength),
    el("div", { class: "field" },
      el("label", { class: "label", for: "confirm", text: "Confirm password" }),
      confirm),
    submit,
  );

  const card = el("div", { class: "login" },
    el("div", { class: "brandmark" }, el("span", { class: "dot" }),
      el("span", { text: "Dashboard" })),
    el("h1", { text: "Set up" }),
    el("p", { class: "sub",
      text: "Nobody has claimed this dashboard yet. Create your account." }),
    errorBox, form,
    el("p", { class: "hint",
      text: "Google sign-in and API keys come later, in Settings." }));

  mount(root, el("div", { class: "login-wrap" }, card));
  token.focus();
}
