"""The themes that ship with the dashboard.

Written out rather than left implicit in the stylesheet so they can be
exported, edited and imported back -- which is how a new theme starts:
you take one that works and change it, rather than facing thirty-seven
blank fields.
"""

N8N_DARK = {
    "name": "n8n Dark",
    "author": "built in",
    "base": "dark",
    "tokens": {
        "--brand": "#ea4b71",
        "--brand-hover": "#f26185",
        "--brand-press": "#c93d5e",
        "--brand-tint": "rgba(234, 75, 113, 0.12)",
        "--brand-edge": "rgba(234, 75, 113, 0.35)",
        "--canvas": "#15171c",
        "--canvas-dot": "#23272f",
        "--surface": "#1c1f26",
        "--surface-2": "#22262e",
        "--surface-3": "#2a2f39",
        "--line": "#2b303a",
        "--line-lit": "#3a414e",
        "--text": "#e8eaee",
        "--text-2": "#a7aeba",
        "--text-3": "#737b88",
        "--text-4": "#4d545f",
        "--ok": "#35b67f",
        "--ok-tint": "rgba(53, 182, 127, 0.12)",
        "--warn": "#e0a03c",
        "--warn-tint": "rgba(224, 160, 60, 0.12)",
        "--danger": "#e4574c",
        "--danger-tint": "rgba(228, 87, 76, 0.12)",
        "--info": "#4a9eff",
        "--info-tint": "rgba(74, 158, 255, 0.12)",
        "--node-agenda": "#7d6cf0",
        "--node-inbox": "#4a9eff",
        "--node-budget": "#35b67f",
        "--node-system": "#737b88",
        "--r-sm": "4px",
        "--r": "8px",
        "--r-lg": "12px",
        "--r-pill": "999px",
        "--font":
            "ui-sans-serif, system-ui, -apple-system, \"Segoe UI\", Roboto, "
            "\"Helvetica Neue\", Arial, sans-serif",
        "--mono":
            "ui-monospace, \"JetBrains Mono\", \"SFMono-Regular\", Menlo, "
            "\"DejaVu Sans Mono\", monospace",
        "--track": "0.08em",
        "--shadow": "0 1px 2px rgba(0,0,0,.4), 0 4px 12px rgba(0,0,0,.25)",
        "--shadow-lg": "0 8px 32px rgba(0,0,0,.45)",
    },
}


PAINTING = {
    "name": "Painting",
    "author": "built in",
    "base": "light",
    "tokens": {
        "--brand": "#9F4E2D",
        "--brand-hover": "#B85C36",
        "--brand-press": "#843F24",
        "--brand-tint": "rgba(159, 78, 45, 0.12)",
        "--brand-edge": "rgba(159, 78, 45, 0.32)",
        "--canvas": "#DED3BF",
        "--canvas-dot": "#D3C7B1",
        "--surface": "#EFE7D7",
        "--surface-2": "#E7DDCA",
        "--surface-3": "#DCD0B9",
        "--line": "#C4B69C",
        "--line-lit": "#A8977A",
        "--text": "#000000",
        "--text-2": "#4A4237",
        "--text-3": "#6F6553",
        "--text-4": "#A2947C",
        "--ok": "#487039",
        "--ok-tint": "rgba(72, 112, 57, 0.14)",
        "--warn": "#846007",
        "--warn-tint": "rgba(132, 96, 7, 0.14)",
        "--danger": "#5E2114",
        "--danger-tint": "rgba(94, 33, 20, 0.14)",
        "--info": "#3D5A80",
        "--info-tint": "rgba(61, 90, 128, 0.14)",
        "--node-agenda": "#6B5B95",
        "--node-inbox": "#3D5A80",
        "--node-budget": "#487039",
        "--node-system": "#6F6553",
        "--font":
            "Fraunces, \"Iowan Old Style\", \"Palatino Linotype\", Georgia, "
            "serif",
        "--mono": "ui-monospace, \"SFMono-Regular\", Menlo, monospace",
        "--shadow":
            "0 1px 2px rgba(60, 45, 25, 0.16), 0 6px 14px rgba(60, 45, "
            "25, 0.10)",
        "--shadow-lg": "0 10px 30px rgba(60, 45, 25, 0.18)",
        "--r": "3px",
        "--r-sm": "2px",
        "--r-lg": "5px",
        "--r-pill": "999px",
        "--track": "0.05em",
    },
}


ALL = [N8N_DARK, PAINTING]
