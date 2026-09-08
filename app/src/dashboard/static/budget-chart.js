/* The budget at a glance: one column per category, two lines across them.
 *
 * Each column is a single width, filled from the bottom:
 *
 *   solid        -- what has actually gone out this month
 *   glass above  -- the rest of what history expects you to spend
 *   solid line   -- what you have budgeted
 *   dashed line  -- what the engine recommends
 *
 * One bar, not two of different widths. Nesting a narrow bar inside a wide
 * one asks the eye to compare two edges that do not share a baseline; a
 * single column filling up asks it to compare one edge against a line, which
 * is the same question everyone already answers when reading a fuel gauge.
 *
 * Kanso: the simpler drawing carries the same three facts.
 *
 * Ordered by what has actually gone out this month, largest first. That makes
 * the chart a ranking of where the money went, which is the question you have
 * when you open it -- and unlike a ranking by historical average, it moves as
 * the month does.
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

// Labels are 11px (see .bchart-label). SVG cannot measure text before it is
// in the document, and measuring would mean a reflow per render, so this
// approximates: a proportional face at 11px averages a little under 6px a
// character. Only used to choose an angle, where being a few pixels out
// changes nothing.
const CHAR_PX = 5.9;
const LABEL_CHARS = 18;

/* The shallowest angle at which names of this length stop colliding.
 *
 * A rotated label needs cos(angle) x its length of horizontal room. Shallow
 * reads more easily, so this takes the gentlest angle that fits and only
 * steepens when it has to -- ending at vertical, which always fits because
 * it needs no horizontal room at all. */
function labelAngle(slot, chars) {
  const len = Math.min(chars, LABEL_CHARS) * CHAR_PX;
  for (const a of [38, 45, 55, 65, 75]) {
    if (Math.cos((a * Math.PI) / 180) * len <= slot * 0.98) return a;
  }
  return 90;
}

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
    class: "bchart", role: "img",
    "aria-label": "Spending by category, most spent first",
  });

  if (!items.length) {
    root.setAttribute("viewBox", `0 0 ${W} ${height}`);
    root.append(svg("text", {
      x: W / 2, y: height / 2, "text-anchor": "middle", class: "chart-empty",
      text: "Nothing budgeted yet",
    }));
    return root;
  }

  // Most spent first, by what has actually gone out this month.
  //
  // It ranked by the historical estimate before, which answers "where does
  // your money usually go" -- a fair question, and not the one you have when
  // you open this. Ranking by what has actually left puts the categories
  // doing the damage *this* month on the left, which is where attention
  // goes, and it changes as the month does.
  //
  // Estimate and budget break ties, so the categories nothing has been spent
  // on yet still fall in a sensible order rather than an arbitrary one.
  const rank = (d) => [d.actual || 0, d.estimate || 0, d.budgeted || 0];
  const data = [...items].sort((a, b) => {
    const [aa, ae, ab] = rank(a), [ba, be, bb] = rank(b);
    return (ba - aa) || (be - ae) || (bb - ab) || a.name.localeCompare(b.name);
  });

  const innerW = W - PAD.left - PAD.right;
  const top = niceTop(Math.max(
    ...data.map((d) => Math.max(d.estimate || 0, d.budgeted || 0,
                                d.actual || 0, d.recommended || 0))));
  const slot = innerW / data.length;
  const barW = Math.min(MAX_BAR, slot * 0.62);

  // Room for the names, which depends on how many there are. Fixed padding
  // is what forced the old chart to drop every second label: at twenty
  // categories they did not fit, so half were thrown away rather than the
  // chart growing by the forty pixels it needed.
  const longest = Math.max(
    ...data.map((d) => Math.min(d.name.length, LABEL_CHARS)));
  const angle = labelAngle(slot, longest);
  const labelRun = longest * CHAR_PX;
  const padBottom = Math.max(
    PAD.bottom,
    Math.ceil(Math.sin((angle * Math.PI) / 180) * labelRun) + 34);

  // The drawing grows downwards to fit them; the plot keeps its full height
  // rather than being squeezed to make room.
  const chartH = height - PAD.bottom + padBottom;
  const innerH = height - PAD.top - PAD.bottom;
  root.setAttribute("viewBox", `0 0 ${W} ${chartH}`);

  const labelY = PAD.top + innerH + 22;
  const cx = (i) => PAD.left + slot * i + slot / 2;
  const y = (v) => PAD.top + innerH - (Math.max(0, v) / top) * innerH;
  const base = y(0);

  const defs = svg("defs", {},
    svg("linearGradient", { id: "barFill", x1: "0", y1: "0", x2: "0", y2: "1" },
      svg("stop", { offset: "0%", "stop-color": "var(--info)",
                    "stop-opacity": "0.34" }),
      svg("stop", { offset: "100%", "stop-color": "var(--info)",
                    "stop-opacity": "0.05" })));
  root.append(defs);

  for (let i = 0; i <= 4; i++) {
    const v = (top / 4) * i;
    root.append(
      svg("line", { class: "bchart-grid", x1: PAD.left, x2: W - PAD.right,
                    y1: y(v), y2: y(v) }),
      svg("text", { class: "bchart-tick", x: PAD.left - 10, y: y(v) + 4,
                    "text-anchor": "end", text: money(v) }));
  }

  // One column per category, filled from the bottom.
  data.forEach((d, i) => {
    const spent = d.actual || 0;
    const expected = d.estimate || 0;
    const overBudget = d.budgeted > 0 && spent > d.budgeted;
    const left = cx(i) - barW / 2;

    // The glass is the part of the expectation not yet spent. Drawn only up
    // to where the solid begins, so the two never overlap and the column
    // reads as one object rather than two stacked ones.
    if (expected > spent) {
      root.append(svg("rect", {
        class: "bchart-bar est", x: left, y: y(expected),
        width: barW, height: y(spent) - y(expected),
        rx: 3, fill: "url(#barFill)",
      }));
    }

    if (spent > 0) {
      root.append(svg("rect", {
        class: "bchart-bar actual" + (overBudget ? " over" : ""),
        x: left, y: y(spent), width: barW, height: base - y(spent), rx: 3,
      }));
    }

    // Past what history expected: mark where the expectation was, so the
    // overshoot is legible rather than merely tall.
    if (spent > expected && expected > 0) {
      root.append(svg("line", {
        class: "bchart-expected-mark", x1: left, x2: left + barW,
        y1: y(expected), y2: y(expected),
      }));
    }
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
  data.forEach((d, i) => {
    const short = (d.budgeted || 0) < (d.estimate || 0);
    const g = svg("g", {
      class: "bchart-point" + (short ? " short" : ""),
      tabindex: onPick ? 0 : null, role: onPick ? "button" : null,
      "aria-label": `${d.name}: spent ${money(d.actual || 0)}, `
                  + `budgeted ${money(d.budgeted)}, `
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

    root.append(svg("text", {
      class: "bchart-label", x: cx(i), y: labelY,
      "text-anchor": "end",
      // Rotated, and steeply enough for this many columns. Every name is
      // drawn: dropping every second one saved the collision and cost the
      // reader the ability to tell which bar is which, which is the only
      // reason the labels are there.
      transform: `rotate(${-angle} ${cx(i)} ${labelY})`,
      text: truncate(d.name, LABEL_CHARS),
    }));

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
