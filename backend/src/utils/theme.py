"""UI color theme presets -- Settings > Theme, added 2026-07-24.

The app is built entirely from literal dark-mode Tailwind utility classes
(bg-gray-900/800/700, text-yellow-300, etc.) hand-written across every page,
with no CSS-variable abstraction. Re-theming every page individually is out
of scope, so instead this module ships a handful of DARK-ONLY presets that
override only the neutral/structural classes (backgrounds, borders, and the
gray text scale) via attribute-scoped CSS -- `[data-fx-theme="slate"]
.bg-gray-900 { ... }` -- injected once into <head>. The semantic accent
colors (green/red/yellow/blue/purple/teal/orange/amber, used throughout for
P&L, alerts, and status) are deliberately left untouched by every preset:
they carry meaning (profit=green, loss=red, warning=yellow) that would be
actively harmful to remap, and a genuine light mode is out of scope too --
it would also need to override Quasar's own internal dark-mode component
styling (cards, inputs, dialogs) everywhere, which is a much bigger, higher
-risk undertaking than this session's "pick a few presets" ask.

Persisted app-wide via app_config (single-user desktop app, same pattern as
core_strategy_params.py) -- not per-browser, and not synced between Local/
Remote nodes (this is a local display preference, not trading state).
"""
from __future__ import annotations

from backend.src.db import database as db_module

_THEME_KEY = "ui_theme"
DEFAULT_THEME = "midnight"

# Neutral scale + border overrides per preset. "midnight" is the literal
# Tailwind gray scale already hard-coded across every page, so it needs no
# override rules at all -- omitted from _THEME_VARS/_THEME_CSS below.
_THEME_VARS: dict[str, dict[str, str]] = {
    "slate": {
        "bg-gray-900": "#0f172a", "bg-gray-800": "#1e293b",
        "bg-gray-750": "#243244", "bg-gray-700": "#334155", "bg-gray-600": "#475569",
        "border-gray-800": "#1e293b", "border-gray-700": "#334155", "border-gray-600": "#475569",
        "text-gray-200": "#e2e8f0", "text-gray-300": "#cbd5e1", "text-gray-400": "#94a3b8",
        "text-gray-500": "#64748b", "text-gray-600": "#475569",
    },
    "ember": {
        "bg-gray-900": "#171717", "bg-gray-800": "#262626",
        "bg-gray-750": "#2e2e2e", "bg-gray-700": "#404040", "bg-gray-600": "#525252",
        "border-gray-800": "#262626", "border-gray-700": "#404040", "border-gray-600": "#525252",
        "text-gray-200": "#e5e5e5", "text-gray-300": "#d4d4d4", "text-gray-400": "#a3a3a3",
        "text-gray-500": "#737373", "text-gray-600": "#525252",
    },
    "contrast": {
        "bg-gray-900": "#000000", "bg-gray-800": "#0a0a0a",
        "bg-gray-750": "#101010", "bg-gray-700": "#161616", "bg-gray-600": "#222222",
        "border-gray-800": "#262626", "border-gray-700": "#333333", "border-gray-600": "#404040",
        "text-gray-200": "#ffffff", "text-gray-300": "#f5f5f5", "text-gray-400": "#d4d4d4",
        "text-gray-500": "#a3a3a3", "text-gray-600": "#737373",
    },
}

THEME_LABELS: dict[str, str] = {
    "midnight": "Midnight (default)",
    "slate": "Slate",
    "ember": "Ember",
    "contrast": "High Contrast",
}

THEME_DESCRIPTIONS: dict[str, str] = {
    "midnight": "The current default -- neutral dark gray.",
    "slate": "Cooler, bluer dark neutrals.",
    "ember": "Warmer, true-neutral dark tones.",
    "contrast": "Near-black background, brighter text and borders for maximum readability.",
}

# Swatch colors for the picker UI -- (bg, border, text) sampled from each
# preset's own bg-gray-900/border-gray-700/text-gray-300 override.
THEME_SWATCHES: dict[str, tuple[str, str, str]] = {
    "midnight": ("#111827", "#374151", "#d1d5db"),
    "slate": ("#0f172a", "#334155", "#cbd5e1"),
    "ember": ("#171717", "#404040", "#d4d4d4"),
    "contrast": ("#000000", "#333333", "#f5f5f5"),
}

THEMES: list[str] = ["midnight", "slate", "ember", "contrast"]


def _css_for(theme: str) -> str:
    rules = []
    for cls, color in _THEME_VARS[theme].items():
        prop = "background-color" if cls.startswith("bg-") else (
            "border-color" if cls.startswith("border-") else "color"
        )
        rules.append(f'[data-fx-theme="{theme}"] .{cls} {{ {prop}: {color} !important; }}')
    return "\n".join(rules)


THEME_HEAD_CSS = "<style>\n" + "\n".join(_css_for(t) for t in _THEME_VARS) + "\n</style>"


def get_theme() -> str:
    stored = db_module.get_app_config(_THEME_KEY)
    return stored if stored in THEMES else DEFAULT_THEME


def set_theme(theme: str) -> None:
    if theme not in THEMES:
        raise ValueError(f"Unknown theme: {theme}")
    db_module.set_app_config(_THEME_KEY, theme)
