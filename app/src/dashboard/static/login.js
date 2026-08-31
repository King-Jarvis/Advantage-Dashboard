import { api, post, setCsrf } from "./api.js";
import { el, mount } from "./dom.js";
import { googleMark } from "./google-mark.js";

/* Whether Google sign-in is configured is a server fact, not a client
 * guess: the button is only shown if the server says a client id exists,
 * so a fresh deployment does not offer a route that cannot work. */
export function loginView(root, { googleEnabled, onSignedIn }) {
  const errorBox = el("div", { class: "error", role: "alert" });

  const username = el("input", {
    class: "input", type: "text", id: "username", name: "username",
    autocomplete: "username", required: true,
  });
  const password = el("input", {
    class: "input", type: "password", id: "password", name: "password",
    autocomplete: "current-password", required: true,
  });

  const submit = el("button", { class: "btn primary", type: "submit", text: "Sign in" });

  async function signIn(ev) {
    ev.preventDefault();
    errorBox.textContent = "";
    submit.disabled = true;
    try {
      const res = await post("/api/auth/login", {
        username: username.value, password: password.value,
      });
      setCsrf(res.csrf_token);
      onSignedIn();
    } catch (err) {
      // The server returns one message for every failure. Echoing it
      // verbatim keeps the client from inventing a more specific one.
      errorBox.textContent = err.status === 401
        ? "Invalid username or password."
        : "Could not sign in. Try again.";
      password.value = "";
      password.focus();
    } finally {
      submit.disabled = false;
    }
  }

  const form = el("form", { onsubmit: signIn, novalidate: true },
    el("div", { class: "field" },
      el("label", { class: "label", for: "username", text: "Username" }),
      username),
    el("div", { class: "field" },
      el("label", { class: "label", for: "password", text: "Password" }),
      password),
    submit,
  );

  const card = el("div", { class: "login" },
    el("div", { class: "brandmark" }, el("span", { class: "dot" }),
      el("span", { text: "Dashboard" })),
    el("h1", { text: "Sign in" }),
    el("p", { class: "sub", text: "Your calendar, mail and money in one place." }),
    errorBox,
  );

  if (googleEnabled) {
    const gbtn = el("button", {
      class: "btn-google", type: "button",
      onclick: () => { window.location.href = "/api/auth/google/start"; },
    }, googleMark(), el("span", { text: "Sign in with Google" }));
    card.append(gbtn, el("div", { class: "divider" }, el("span", { text: "or" })));
  }

  card.append(form);
  if (!googleEnabled) {
    card.append(el("p", { class: "hint",
      text: "Google sign-in is not configured on this deployment." }));
  }

  mount(root, el("div", { class: "login-wrap" }, card));
  username.focus();
  return { api };
}
