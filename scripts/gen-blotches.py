"""Generate the paint-blotch masks. Kept so the shapes can be regenerated
rather than being magic strings nobody can adjust."""
import math
import random
import urllib.parse

W, H = 400, 220

def stroke_pts(x1, y1, x2, y2, w, wobble, rnd, steps=18):
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy) or 1
    ux, uy = dx / L, dy / L
    nx, ny = -uy, ux
    top, bot = [], []
    for i in range(steps + 1):
        t = i / steps
        px, py = x1 + dx * t, y1 + dy * t
        taper = math.sin(math.pi * t) ** 0.18
        half = (w / 2) * taper
        top.append((px + nx * (half + rnd.uniform(-wobble, wobble)),
                    py + ny * (half + rnd.uniform(-wobble, wobble))))
        bot.append((px - nx * (half + rnd.uniform(-wobble, wobble)),
                    py - ny * (half + rnd.uniform(-wobble, wobble))))
    return top + bot[::-1]

def strokes(seed, n=6):
    """Broad sweeps that cover the shape, plus short marks that break up the
    edges. Without the second kind the top and bottom come out straight and
    the whole thing reads as a rectangle with soft corners."""
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        t = i / max(1, n - 1)
        y = 18 + t * (H - 36) + rnd.uniform(-8, 8)
        ang = rnd.uniform(-0.05, 0.05)
        x1 = rnd.uniform(-26, 14)
        x2 = W - rnd.uniform(-26, 14)
        y1 = y + math.tan(ang) * -W / 2
        y2 = y + math.tan(ang) * W / 2
        # The outermost strokes make the top and bottom boundary. Give them
        # more wander than the inner ones, which only have to cover: an edge
        # that undulates is the difference between paint and a torn rectangle.
        edge = i in (0, n - 1)
        out.append((stroke_pts(x1, y1, x2, y2, (H / n) * rnd.uniform(2.5, 3.1),
                               7.5 if edge else 3.0, rnd,
                               steps=26 if edge else 18),
                    rnd.uniform(0.74, 0.90)))
    for _ in range(3):
        # Short diagonals near an edge, which is what makes the boundary look
        # brushed rather than cut.
        ex = rnd.choice([rnd.uniform(-10, 90), rnd.uniform(W - 90, W + 10)])
        ey = rnd.choice([rnd.uniform(-6, 40), rnd.uniform(H - 40, H + 6)])
        ang = rnd.uniform(-0.9, 0.9)
        ln = rnd.uniform(70, 140)
        out.append((stroke_pts(ex, ey, ex + math.cos(ang) * ln,
                               ey + math.sin(ang) * ln,
                               rnd.uniform(34, 52), 3.4, rnd),
                    rnd.uniform(0.55, 0.75)))
    return out

def svg(seed):
    parts = []
    for pts, op in strokes(seed):
        d = "M" + " L".join("%.1f %.1f" % p for p in pts) + " Z"
        parts.append('<path d="%s" fill="#000" fill-opacity="%.2f"/>' % (d, op))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
            'preserveAspectRatio="none">%s</svg>' % (W, H, "".join(parts)))

def brushmark(seed):
    rnd = random.Random(seed)
    pts = stroke_pts(8, 15, W - 12, 13 + rnd.uniform(-3, 3), 16, 2.4, rnd)
    d = "M" + " L".join("%.1f %.1f" % p for p in pts) + " Z"
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d 30" '
            'preserveAspectRatio="none"><path d="%s" fill="#000"/></svg>' % (W, d))

def uri(s):
    return 'url("data:image/svg+xml,%s")' % urllib.parse.quote(s, safe="")

if __name__ == "__main__":
    lines = [":root {"]
    for name, seed in (("a", 11), ("b", 27), ("c", 43)):
        lines.append("  --blotch-%s: %s;" % (name, uri(svg(seed))))
    lines.append("  --brushmark: %s;" % uri(brushmark(5)))
    lines.append("}")
    print("\n".join(lines))
