/* Safe DOM construction.
 *
 * Everything this dashboard renders is text somebody else wrote: email
 * subjects and senders, transaction payees, model-written rationales. None of
 * it may ever be parsed as markup, so this module offers no way to do that --
 * text goes in through textContent and nowhere else.
 *
 * CI rejects every markup-assigning sink anywhere under static/ -- see
 * scripts/check-no-innerhtml.sh for the list -- so the rule is enforced
 * rather than remembered. That check deliberately has no ignore comment,
 * which is why this note describes the sinks instead of naming them: an
 * exemption would get used, and there is no safe case here.
 */

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;         // never markup
    else if (k === "html") throw new Error("el(): no raw markup, use text");
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === "dataset") {
      for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
    } else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function mount(node, ...children) {
  clear(node);
  for (const c of children.flat()) if (c) node.append(c);
  return node;
}

/* An SVG namespace equivalent, for the wires and charts. Same rule: no
 * markup strings, only nodes and attributes. */
export function svg(tag, props = {}, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "text") node.textContent = v;
    else node.setAttribute(k, String(v));
  }
  for (const c of children.flat()) if (c) node.append(c);
  return node;
}

/* Money arrives as integer cents and is only ever formatted for display --
 * never parsed back out of the DOM, never used for arithmetic. */
export function money(cents, { sign = false } = {}) {
  if (cents === null || cents === undefined) return "—";
  const neg = cents < 0;
  const s = (Math.abs(cents) / 100).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  return (neg ? "−" : sign ? "+" : "") + s;
}

export function moneyEl(cents, extra = "") {
  const cls = ["money", cents < 0 ? "neg" : cents > 0 ? "pos" : "", extra]
    .filter(Boolean).join(" ");
  return el("span", { class: cls, text: money(cents) });
}
