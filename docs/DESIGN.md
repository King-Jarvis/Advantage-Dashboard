# Design

The interface follows Japanese design principles, applied to a data surface rather than
decoration.

| Principle | Here |
|---|---|
| **Kanso** 簡素 | Three surfaces, one question each: what's next, what needs me, where's my money. Everything else is behind a door. |
| **Ma** 間 | A 4/8/12/16/24/32 spacing scale throughout. Whitespace separates; a hairline appears only where a boundary carries meaning. No cards inside cards. |
| **Shibui** 渋い | Sumi-ink neutrals with one working accent. Saturated colour is rationed: vermillion means overspent or urgent and nothing else. |
| **Seijaku** 静寂 | A quiet week should look quiet. Colour is an event, not a decoration. |
| **Fukinsei** 不均整 | Deliberate asymmetry. The agenda column is wider than the rail; weight sits left. Nothing is centred for its own sake. |
| **Yūgen** 幽玄 | Progressive disclosure. A transaction shows payee and amount; category, notes and history appear on intent. |

## Held equally

- One primary action per screen.
- Every number traceable to its source — no figure appears without a route to its inputs.
- Optimistic updates with honest pending states. A queued edit says so.
- No destructive action without undo.
- Full keyboard reachability, including moving money between envelopes.
- WCAG AA contrast.
- Motion under 200ms, and none at all under `prefers-reduced-motion`.

## Responsive is a requirement, not a nicety

The dashboard is checked on a phone as often as a desktop. One fluid layout: the rail
collapses below 900px, the budget grid becomes a stack, and drag-to-move gains a tap-based
equivalent. Touch targets are at least 44px.

Drag is always an accelerator, never the only route to an action — it is unusable by
keyboard and awkward on a phone.

## Charts

Hand-rolled SVG. No charting library, no CDN, nothing that would require loosening the
content security policy.
