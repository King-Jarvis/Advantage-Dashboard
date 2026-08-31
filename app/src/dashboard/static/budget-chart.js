/* The budget at a glance: bars for what you will spend, lines for what you
 * planned and what is advised.
 *
 *   glassy bars  -- estimated spending, from your own history
 *   solid line   -- what you have budgeted
 *   dashed line  -- what the engine recommends
 *
 * Ordered largest to smallest, left to right. That ordering makes the chart a
 * ranking, which is the honest reading: the categories that decide whether
 * the month works are on the left, and attention should go there first.
 *
 * The lines are drawn as straight segments between bar centres with visible
 * points, not smoothed. A curve through categorical data implies the space
 * between categories means something, and it does not.
 *
 * Hand-rolled SVG: the content security policy allows no external origin, and
 * a charting library for two series is not a trade worth making.
 */
import { money, svg } from "./dom.js";

const W = 900, H = 340;
const PAD = { top: 24, right: 20, bottom: 68, left: 68 };
const MAX_BAR = 64;

function niceTop(max) {
  if (max <= 0) return 100;
  // Step through 1, 2, 2.5 and 5 rather than powers of ten alone. A plain
  // decade rounds 1250 up to 2000 and squashes every bar into the lower
  // third of the chart for no reason.
  const decade = Math.pow(10, Math.floor(Math.log10(max)));
  for (const m of [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10]) {
    const candidate = decade * m;
    if (candidate >= max * 1.05) return candidate;
  }
  return decade * 10;
}

function truncate(name, limit) {
  return name.length > limit ? name.slice(0, limit - 1) + "…" : name;
}

/* `items`: [{ id, name, budgeted, recommended, estimate }] in cents. */
export function budgetChart(items, { onPick, height = H } = {}) {
  const root = svg("svg", {
    viewBox: `0 0 ${W} ${height}`, class: "bchart", role: "img",
    "aria-label": "Budget by category, largest first",
  });

  if (!items.length) {
    root.append(svg("text", {
      x: W / 2, y: height / 2, "text-anchor": "middle", class: "chart-empty",
      text: "Nothing budgeted yet",
    }));
    return root;
  }

  // Largest first. Ranked by what it will actually cost, since that is what
  // decides whether the month works -- not by what happens to be budgeted.
  const data = [...items].sort(
    (a, b) => (b.estimate || b.budgeted || 0) - (a.estimate || a.budgeted || 0));

  const innerW = W - PAD.left - PAD.right;
  const innerH = height - PAD.top - PAD.bottom;
  const top = niceTop(Math.max(
    ...data.map((d) => Math.max(d.estimate || 0, d.budgeted || 0,
                                d.recommended || 0))));
  const slot = innerW / data.length;
  const barW = Math.min(MAX_BAR, slot * 0.62);
  const cx = (i) => PAD.left + slot * i + slot / 2;
  const y = (v) => PAD.top + innerH - (Math.max(0, v) / top) * innerH;
  const base = y(0);

  const defs = svg("defs", {},
    svg("linearGradient", { id: "barFill", x1: "0", y1: "0", x2: "0", y2: "1" },
      svg("stop", { offset: "0%", "stop-color": "var(--info)",
                    "stop-opacity": "0.42" }),
      svg("stop", { offset: "100%", "stop-color": "var(--info)",
                    "stop-opacity": "0.06" })));
  root.append(defs);

  for (let i = 0; i <= 4; i++) {
    const v = (top / 4) * i;
    root.append(
      svg("line", { class: "bchart-grid", x1: PAD.left, x2: W - PAD.right,
                    y1: y(v), y2: y(v) }),
      svg("text", { class: "bchart-tick", x: PAD.left - 10, y: y(v) + 4,
                    "text-anchor": "end", text: money(v) }));
  }

  // 1. Estimated spending, as glassy bars behind everything else.
  data.forEach((d, i) => {
    const h = base - y(d.estimate || 0);
    root.append(svg("rect", {
      class: "bchart-bar", x: cx(i) - barW / 2, y: y(d.estimate || 0),
      width: barW, height: Math.max(0, h), rx: 3, fill: "url(#barFill)",
    }));
  });

  // A budget of nothing is a real answer, so that line is continuous.
  root.append(svg("polyline", {
    class: "bchart-bud",
    points: data.map((d, i) => `${cx(i)},${y(d.budgeted || 0)}`).join(" "),
  }));

  // The recommendation line breaks where there is no recommendation. Drawing
  // a null as zero would read as "spend nothing here", which is the opposite
  // of "not enough history to say".
  let run = [];
  const flushRun = () => {
    if (run.length > 1) {
      root.append(svg("polyline", { class: "bchart-rec", points: run.join(" ") }));
    } else if (run.length === 1) {
      const [px, py] = run[0].split(",");
      root.append(svg("line", { class: "bchart-rec", x1: Number(px) - 9,
                                x2: Number(px) + 9, y1: py, y2: py }));
    }
    run = [];
  };
  data.forEach((d, i) => {
    if (d.recommended === null || d.recommended === undefined) flushRun();
    else run.push(`${cx(i)},${y(d.recommended)}`);
  });
  flushRun();

  // Points, labels and hit targets.
  const labelEvery = data.length > 14 ? 2 : 1;
  data.forEach((d, i) => {
    const short = (d.budgeted || 0) < (d.estimate || 0);
    const g = svg("g", {
      class: "bchart-point" + (short ? " short" : ""),
      tabindex: onPick ? 0 : null, role: onPick ? "button" : null,
      "aria-label": `${d.name}: budgeted ${money(d.budgeted)}, `
                  + `estimated ${money(d.estimate)}`
                  + (d.recommended !== null && d.recommended !== undefined
                     ? `, recommended ${money(d.recommended)}`
                     : ", no recommendation yet"),
    });
    const hasRec = d.recommended !== null && d.recommended !== undefined;
    g.append(
      hasRec
        ? svg("circle", { class: "bchart-dot rec", cx: cx(i),
                          cy: y(d.recommended), r: 3 })
        : null,
      svg("circle", { class: "bchart-dot bud", cx: cx(i),
                      cy: y(d.budgeted || 0), r: 4.5 }),
      svg("rect", { class: "bchart-hit", x: cx(i) - slot / 2, y: PAD.top,
                    width: slot, height: innerH + 10 }));
    root.append(g);

    if (i % labelEvery === 0) {
      root.append(svg("text", {
        class: "bchart-label", x: cx(i), y: height - PAD.bottom + 22,
        "text-anchor": "end",
        // Rotated: horizontal names collide past about six categories, and
        // truncating them all to fit would lose the distinction between
        // "Groceries" and "Gifts".
        transform: `rotate(-38 ${cx(i)} ${height - PAD.bottom + 22})`,
        text: truncate(d.name, 16),
      }));
    }

    if (onPick) {
      const fire = () => onPick(d);
      g.addEventListener("click", fire);
      g.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fire(); }
      });
    }
  });

  return root;
}
