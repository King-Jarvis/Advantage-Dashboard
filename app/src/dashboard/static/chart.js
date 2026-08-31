/* A spending chart, hand-rolled in SVG.
 *
 * No charting library: the content security policy allows no external
 * origins, and a dependency for six polylines is not a trade worth making.
 *
 * Three layers, in the order the eye should read them:
 *   what you actually spent   -- filled area, the ground truth
 *   what the history suggests -- a band, whose WIDTH is the confidence
 *   what you budgeted         -- a line you can drag
 *
 * The band is drawn rather than a single suggested line on purpose. A point
 * estimate from three noisy months looks exactly as certain as one from
 * twelve steady ones, and that is a lie the shape of the drawing can avoid
 * telling.
 */
import { money, svg } from "./dom.js";

const W = 640, H = 190;
const PAD = { top: 14, right: 12, bottom: 26, left: 56 };

function scale(values, height) {
  const max = Math.max(1, ...values.map((v) => Math.abs(v)));
  // Round the top up to something legible rather than to the exact maximum.
  const step = Math.pow(10, Math.floor(Math.log10(max)));
  const top = Math.ceil(max / step) * step;
  return { top, y: (v) => height - (v / top) * height };
}

export function spendingChart(data, { onPick } = {}) {
  const months = data.months || [];
  const spend = data.spend || [];
  const budgeted = data.budgeted || [];
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;

  if (!months.length) {
    return svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img",
                        "aria-label": "No history yet" },
      svg("text", { x: W / 2, y: H / 2, "text-anchor": "middle",
                    class: "chart-empty", text: "No history yet" }));
  }

  const all = [...spend, ...budgeted, data.high_cents || 0];
  const s = scale(all, innerH);
  const x = (i) => PAD.left + (months.length === 1
    ? innerW / 2
    : (i / (months.length - 1)) * innerW);
  const y = (v) => PAD.top + s.y(v);

  const root = svg("svg", {
    viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img",
    "aria-label": `Spending over ${months.length} months`,
  });

  // Gridlines and value labels.
  for (let i = 0; i <= 2; i++) {
    const v = (s.top / 2) * i;
    root.append(
      svg("line", { class: "chart-grid", x1: PAD.left, x2: W - PAD.right,
                    y1: y(v), y2: y(v) }),
      svg("text", { class: "chart-tick", x: PAD.left - 8, y: y(v) + 4,
                    "text-anchor": "end", text: money(v) }));
  }

  // The suggestion band. Width is the confidence, so a guess from three
  // months looks like a guess.
  if (data.low_cents !== null && data.high_cents !== null) {
    const top = y(data.high_cents), bottom = y(data.low_cents);
    root.append(svg("rect", {
      class: `chart-band conf-${data.confidence || "low"}`,
      x: PAD.left, y: Math.min(top, bottom),
      width: innerW, height: Math.max(2, Math.abs(bottom - top)),
    }));
    if (data.suggested_cents !== null) {
      root.append(svg("line", {
        class: "chart-suggested", x1: PAD.left, x2: W - PAD.right,
        y1: y(data.suggested_cents), y2: y(data.suggested_cents),
      }));
    }
  }

  // Actual spend, as an area.
  const pts = months.map((m, i) => `${x(i)},${y(spend[i] || 0)}`);
  root.append(svg("polygon", {
    class: "chart-actual",
    points: `${PAD.left},${y(0)} ${pts.join(" ")} ${x(months.length - 1)},${y(0)}`,
  }));

  // What you budgeted.
  if (budgeted.some((v) => v)) {
    root.append(svg("polyline", {
      class: "chart-budget",
      points: months.map((m, i) => `${x(i)},${y(budgeted[i] || 0)}`).join(" "),
    }));
  }

  // Points, and month labels on the ends and middle only -- twelve labels at
  // this width overlap into noise.
  months.forEach((m, i) => {
    const dot = svg("circle", {
      class: "chart-dot", cx: x(i), cy: y(spend[i] || 0), r: 3,
      tabindex: onPick ? 0 : null,
      "aria-label": `${m}: ${money(spend[i] || 0)}`,
    });
    if (onPick) {
      dot.addEventListener("click", () => onPick(m, spend[i] || 0));
      dot.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onPick(m, spend[i] || 0);
        }
      });
    }
    root.append(dot);
    if (i === 0 || i === months.length - 1 ||
        i === Math.floor((months.length - 1) / 2)) {
      root.append(svg("text", {
        class: "chart-tick", x: x(i), y: H - 8, "text-anchor":
          i === 0 ? "start" : i === months.length - 1 ? "end" : "middle",
        text: m.slice(2),
      }));
    }
  });

  return root;
}
