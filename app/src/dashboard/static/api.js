/* Talking to the server.
 *
 * The CSRF token is held in memory only. Putting it in localStorage would
 * keep it readable by any script that ever runs on this origin, which is
 * exactly what the token is meant to survive.
 */

let csrfToken = null;

export function setCsrf(t) { csrfToken = t; }
export function getCsrf() { return csrfToken; }

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function api(method, path, body) {
  const headers = {};
  let payload;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  if (method !== "GET" && method !== "HEAD" && csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
  }
  const res = await fetch(path, {
    method,
    headers,
    body: payload,
    // Never send this session to another origin, and never accept one back.
    credentials: "same-origin",
    redirect: "error",
  });

  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = null; }
  }
  if (!res.ok) {
    throw new ApiError(res.status, (data && data.error) || res.statusText);
  }
  return data;
}

export const get = (p) => api("GET", p);
export const post = (p, b) => api("POST", p, b);
export const patch = (p, b) => api("PATCH", p, b);
export const del = (p) => api("DELETE", p);
