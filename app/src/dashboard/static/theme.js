/* Wearing a theme.
 *
 * Tokens are applied by setting custom properties on the root element.
 * `element.style.setProperty` is CSSOM, not an inline style attribute and not
 * an injected stylesheet, so the content security policy permits it while
 * still forbidding both of those. That is the whole reason a theme can be
 * data here rather than a build step.
 *
 * Applied before the first paint, so nothing is ever drawn in one palette and
 * then repainted in another.
 */
import { get } from "./api.js";

let applied = [];

export function apply(tokens, base) {
  const root = document.documentElement;
  // Remove what the last theme set before setting the next one. Without this,
  // switching to a theme that defines fewer tokens leaves the previous
  // theme's leftovers behind and produces a look neither of them describes.
  for (const name of applied) root.style.removeProperty(name);
  applied = [];

  for (const [name, value] of Object.entries(tokens || {})) {
    // Only custom properties, and only ones this page could have asked for.
    // The server validates too; this is the second lock.
    if (!/^--[a-z0-9-]+$/.test(name)) continue;
    root.style.setProperty(name, String(value));
    applied.push(name);
  }
  // Tells the browser which way round the palette runs, so form controls,
  // scrollbars and the like follow rather than staying dark on a light theme.
  root.style.colorScheme = base === "light" ? "light" : "dark";
  document.documentElement.dataset.themeBase = base || "dark";
}

export async function load() {
  try {
    const t = await get("/api/theme");
    apply(t.tokens, t.base);
    return t;
  } catch {
    // A theme that will not load must not stop the application starting: the
    // stylesheet's own defaults are a complete, working look.
    return null;
  }
}
