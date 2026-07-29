"""core_ui_theme.py: Settings > Theme -- Light/Dark presets persisted via
app_config. Added 2026-07-24, reduced to Light/Dark and given a real light
theme 2026-07-29 (previously shipped four dark-only variants).

The app is built entirely from literal Tailwind utility classes
(bg-gray-900/800/700, text-white, text-gray-300, etc.) hand-written across
every page, with no CSS-variable abstraction. Re-theming every page
individually is out of scope, so instead this module overrides only the
neutral/structural classes (backgrounds, borders, the gray text scale, and
text-white) via attribute-scoped CSS -- `[data-fx-theme="light"]
.bg-gray-900 { ... }` -- injected once into <head>. The semantic accent
colors (green/red/yellow/blue/purple/teal/orange/amber, used throughout for
P&L, alerts, and status) are deliberately left untouched by every preset:
they carry meaning (profit=green, loss=red, warning=yellow) that would be
actively harmful to remap.

`text-white` is overridden too (it's used app-wide as headline/emphasis
text on dark cards, which would be invisible once those cards turn light) --
but excluded when the element is itself a button (`.q-btn`), since the same
class also colors labels on colored action buttons (bg-green-700 text-white,
etc.) where white-on-saturated-color should stay white in both themes.

Persisted app-wide via app_config (single-user desktop app, same pattern as
core_strategy_params.py) -- not per-browser, and not synced between Local/
Remote nodes (this is a local display preference, not trading state).
"""
from __future__ import annotations

from forex_trader.core import database as db_module

_THEME_KEY = "ui_theme"
DEFAULT_THEME = "dark"

# Neutral scale + border overrides per preset. "dark" is the literal
# Tailwind gray scale already hard-coded across every page, so it needs no
# override rules at all -- omitted from _THEME_VARS/_THEME_CSS below.
_THEME_VARS: dict[str, dict[str, str]] = {
    "light": {
        "bg-gray-900": "#ffffff", "bg-gray-800": "#f1f5f9",
        "bg-gray-750": "#eef2f6", "bg-gray-700": "#e2e8f0", "bg-gray-600": "#cbd5e1",
        "border-gray-800": "#e2e8f0", "border-gray-700": "#cbd5e1",
        "border-gray-600": "#94a3b8", "border-gray-500": "#64748b",
        "text-gray-100": "#0f172a", "text-gray-200": "#1e293b", "text-gray-300": "#334155",
        "text-gray-400": "#475569", "text-gray-500": "#64748b", "text-gray-600": "#94a3b8",
        "text-white": "#0f172a",
    },
}

# body background/text -- separate from _THEME_VARS since "body" isn't a
# Tailwind utility class matched by the generic mechanism below. "dark" is
# the existing hardcoded body style in ui/app.py, so it needs no entry here.
_BODY_VARS: dict[str, tuple[str, str]] = {
    "light": ("#f8fafc", "#1e293b"),
}

THEME_LABELS: dict[str, str] = {
    "dark": "Dark",
    "light": "Light",
}

THEME_DESCRIPTIONS: dict[str, str] = {
    "dark": "The app's default dark palette.",
    "light": "Light background, dark text. Profit/loss/warning colors never change.",
}

# Swatch colors for the picker UI -- (bg, border, text) sampled from each
# preset's own bg-gray-900/border-gray-700/text-gray-300 (or text-white) override.
THEME_SWATCHES: dict[str, tuple[str, str, str]] = {
    "dark": ("#111827", "#374151", "#d1d5db"),
    "light": ("#ffffff", "#cbd5e1", "#1e293b"),
}

THEMES: list[str] = ["dark", "light"]


def _css_for(theme: str) -> str:
    rules = []
    for cls, color in _THEME_VARS[theme].items():
        prop = "background-color" if cls.startswith("bg-") else (
            "border-color" if cls.startswith("border-") else "color"
        )
        if cls == "text-white":
            # Same class also labels colored action buttons -- leave those alone.
            rules.append(
                f'[data-fx-theme="{theme}"] .{cls}:not(.q-btn) {{ {prop}: {color} !important; }}'
            )
        else:
            rules.append(f'[data-fx-theme="{theme}"] .{cls} {{ {prop}: {color} !important; }}')
    return "\n".join(rules)


def _body_css_for(theme: str) -> str:
    if theme not in _BODY_VARS:
        return ""
    bg, text = _BODY_VARS[theme]
    return f'[data-fx-theme="{theme}"] body {{ background: {bg} !important; color: {text} !important; }}'


THEME_HEAD_CSS = (
    "<style>\n"
    + "\n".join(_css_for(t) for t in _THEME_VARS)
    + "\n"
    + "\n".join(_body_css_for(t) for t in _BODY_VARS)
    + "\n</style>"
)


def get_theme() -> str:
    stored = db_module.get_app_config(_THEME_KEY)
    return stored if stored in THEMES else DEFAULT_THEME


def set_theme(theme: str) -> None:
    if theme not in THEMES:
        raise ValueError(f"Unknown theme: {theme}")
    db_module.set_app_config(_THEME_KEY, theme)
