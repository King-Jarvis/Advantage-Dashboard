# Writing a theme

A theme is a JSON file. It is not code, it is not compiled, and it does not
need this repository -- paste one into **Settings → Themes → Import** and it
applies immediately.

## The shape

```json
{
  "name": "Painting",
  "author": "you",
  "base": "light",
  "tokens": {
    "--canvas": "#EDE6D8",
    "--text": "#000000"
  }
}
```

`base` is `dark` or `light`. It does not change any colour; it tells the
browser which way round the palette runs so native controls, scrollbars and
form widgets follow instead of staying dark on a pale theme.

Every token is optional. Anything you leave out keeps the stylesheet's own
value, so a theme can be two colours or all thirty-seven.

## Starting from one that works

Export any theme from Settings and edit it. Thirty-seven blank fields is a
bad way to begin, and the exported file is already valid.

## What is refused

Validation is an allowlist in both directions: an unknown token name is
rejected, and a known one must look like its kind. `url(...)`, `@import`,
braces, semicolons and comment markers are refused wherever they appear --
a token value is a value, and anything that can stop being one is not.

The error names the token it did not like, so a rejected theme tells you
which line to fix.

## What a theme may not change

Spacing, type sizes and motion timings are deliberately not themeable. A
theme changes the look; those are layout and legibility, and a file arriving
from elsewhere that can set body text to eight pixels is one that can make
the dashboard unusable.

## The tokens

### Colours

Hex, `rgb()`, `rgba()`, `hsl()`, `transparent`.

| token | what it paints |
| --- | --- |
| `--brand` | primary actions, the active tab, the selected day |
| `--brand-edge` | brand at medium opacity: borders on tinted things |
| `--brand-hover` | brand, one step lighter |
| `--brand-press` | brand, while held |
| `--brand-tint` | brand at low opacity: chip fills, selected backgrounds |
| `--canvas` | the ground the whole page sits on |
| `--canvas-dot` | the texture on that ground; felt, not read |
| `--danger` | overspent, failed, destructive |
| `--danger-tint` | the same at low opacity |
| `--info` | informational, transfers, links |
| `--info-tint` | the same at low opacity |
| `--line` | hairline borders and dividers |
| `--line-lit` | a hairline that carries meaning |
| `--node-agenda` | the calendar's colour, on its left bar |
| `--node-budget` | money's colour |
| `--node-inbox` | mail's colour |
| `--node-system` | settings and anything structural |
| `--ok` | healthy, funded, connected |
| `--ok-tint` | the same at low opacity |
| `--surface` | a panel or card sitting on the canvas |
| `--surface-2` | a raised surface: inputs, hovered rows |
| `--surface-3` | raised further still |
| `--text` | body text; the highest contrast in the design |
| `--text-2` | secondary text: senders, times, labels |
| `--text-3` | hints and captions |
| `--text-4` | dormant; never used for anything that must be read |
| `--warn` | needs attention, underfunded, stale |
| `--warn-tint` | the same at low opacity |

### Type

A font stack. Families must already be available to the browser: there is no `url()`, so a bundled face has to be declared in the stylesheet.

| token | what it paints |
| --- | --- |
| `--font` | everything except code and numbers |
| `--mono` | message bodies, amounts, anything aligned |

### Shadow

Lengths and colours, or `none`.

| token | what it paints |
| --- | --- |
| `--shadow` | the ordinary lift on a panel |
| `--shadow-lg` | a deeper lift, for things floating above |

### Geometry

A single length: `px`, `rem`, `em`, `%`.

| token | what it paints |
| --- | --- |
| `--r` | default corner radius |
| `--r-lg` | large radius: cards, panels |
| `--r-pill` | fully round: filter chips |
| `--r-sm` | small radius: chips, score badges |
| `--track` | letter spacing on small uppercase labels |

## Asking Claude for one

The format is small enough to describe in a sentence. Export a theme, hand
it over, and say what you want changed -- "the same but in autumn browns,
keep the states distinguishable". The reply pastes straight into the import
box, and if it is wrong the validator says which token.

One thing worth asking for explicitly: `--ok`, `--warn`, `--danger` and
`--info` each mean exactly one thing. A palette where warn and danger are
hard to tell apart looks fine on a swatch and fails on the screen that
matters.
