# Bundled type

`font-src 'self'` in the content security policy means a webfont must ship
with the application. It cannot be pulled from a CDN at render time, which is
a deliberate constraint rather than an oversight: a dashboard holding mail,
calendar and finances should not announce every page view to a third party.

## Fraunces

- Variable, axes `opsz` (9–144), `wght` (300–700), `SOFT` (0–100), `WONK` (0–1)
- Latin subset, 121 KB
- SIL Open Font License 1.1 — full text in `LICENSE-Fraunces.txt`
- Undercase Type, https://github.com/undercasetype/Fraunces

Chosen for the Painting theme. `SOFT` rounds the terminals and `WONK` swaps in
the more idiosyncratic letterforms, which together read as hand-made without
being calligraphic — the brief was "slight if any". Both are dialled well
below their maximums: the texture in that theme carries the painted feel, and
the letterforms stay out of the way so the text can be read at a glance.

Declared once in `styles.css` as a `@font-face`. Themes then reference the
family by name — a theme is data and cannot introduce a face of its own, which
is also why it cannot smuggle in a `url()`.
