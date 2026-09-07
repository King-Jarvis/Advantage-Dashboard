---
name: theme-designer
description: Write a theme for the personal dashboard. Use when asked for a new colour scheme, palette or look for the dashboard, or to vary an existing theme. Produces a JSON file that pastes straight into Settings → Themes → Import.
---

# Writing a dashboard theme

Read `docs/THEMES.md` first — it lists every token and what each one paints.

Output a single JSON object and nothing else, so it can be pasted directly
into the import box.

## What matters

**The four state colours each mean exactly one thing.** `--ok` is healthy or
funded, `--warn` needs attention, `--danger` is overspent or failed, `--info`
is neutral information. A palette where warn and danger are hard to tell apart
looks fine as a row of swatches and fails on the screen that matters. Check
them against each other, not just against the background.

**`--text` on `--surface` carries every word in the application.** Aim well
past 7:1. Atmosphere is worth nothing if the inbox cannot be read at arm's
length on a tablet, which is where this is actually used.

**`--text-4` is never read.** It is for dormant marks. Do not use it for
anything a person needs.

**Set `base` honestly** — `light` if the canvas is pale. It does not change a
colour; it tells the browser which way the palette runs so native form
controls follow instead of staying dark on a pale theme.

## Practical

- Every token is optional; omitted ones keep the stylesheet's default. A
  coherent theme usually sets all the colours and leaves geometry alone.
- Tints (`--brand-tint`, `--ok-tint`, …) are the same hue at roughly 0.12
  alpha. Keep that relationship or tinted backgrounds stop reading as related
  to their colour.
- `--canvas-dot` should be barely distinguishable from `--canvas`. It is felt,
  not read.
- No `url()`, no `@import`, no semicolons or braces inside a value. The
  importer refuses them and will name the token it rejected.

## Starting point

Ask for an export of an existing theme rather than starting from nothing.
Varying something that works beats filling in thirty-seven blank fields.
