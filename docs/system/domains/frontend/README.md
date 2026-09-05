# Frontend

**Living file — update when this domain teaches you something.**
Covers: `frontend/`. The canonical rule set is the
`/frontend-conventions` skill; the active restructure plan is
`docs/todo/refactor/frontend/restructure/` (the pack is its own spec — the old standalone anchor `001-frontend-restructure.md` was lost in a docs reorg; its substance is the pack README's "What we're building & why").

## What it is

A NiceGUI (Python) dashboard served in-process alongside the backend —
every widget is a Python object built inside a `with` block, and the
browser is only a rendering surface. Pages live in `frontend/pages/`, the
shell and lifecycle in `frontend/app.py`, dark-only theme presets in
`frontend/theme.py`. A React/Next.js rewrite was proposed and explicitly
rejected (2026-08-06); its two durable ideas — one narrow enforced backend
boundary, and slim views composing domain-scoped components — are being
delivered in NiceGUI instead.

## Where the code lives

- `frontend/app.py` — the shell: framework patches, lifecycle hooks, header, page composition
- `frontend/theme.py` — four dark-only presets re-skinning only the neutral Tailwind scale
- `frontend/pages/` — one module or package per page; `pages/trading/` is the reference page-package shape (`__init__.py` composes; `_active_trades.py`, `_manual_entry.py`, `_signals_card.py`, `_shared.py`)
- `frontend/components/` — first real residents landed 2026-08-11 (stage2 phase 1/5): `start_here.py` (first-run checklist + status gathering + attach()), `getting_started.py` (Help dialog; owns the shared `DAILY_ROUTINE` copy), `tab_labels.py` (tab subtitles, data-only), `empty_state.py` (shared "do this next" empty states, copy keyed by surface), `about_home.py` (About home, imports the routine from getting_started so the two can't drift), `debug_banner.py`. All flat for now — `components/<domain>/` sub-dirs only when one earns it
- `frontend/static/` — favicon, banner, icons

## Constraints / must not change

- Layer rule: `frontend → controllers → services → db`. A page asks a controller a named question and never reaches past it. The only permitted non-controller backend imports are `backend.src.utils.models` and `backend.src.config` — leaf modules that perform no action.
- No SQL, no DB connections, no `backend.src.db` import anywhere in `frontend/**` — enforced at zero.
- Controllers: flat `<name>_controller.py`, never a package, hard 200-line ceiling, no loops/merges/formatting/fallbacks, no NiceGUI import.
- 800-line LOC gate, shrink-only; no file newly enters the baseline, no baselined file grows. Max three `_render_*` functions per module — the fourth means it is two sections.
- Semantic colours are frozen: green = profit, red = loss, yellow/amber = warning, blue = remote/VPS, gray = neutral. No hex colours in a page, no light mode.
- Money rules: every order action goes through a controller; explicit confirmation naming instrument, direction and size; the backend decides whether an order is allowed.
- The restructure changes no behaviour — "if a user could tell the difference, something went wrong."

## Known things & gotchas

- The login gate (`auth_gate.py`) has a first-run branch (2026-08-11, review C2): a real install with no stored password gets a create-password card (username is fixed to `admin`) instead of a login form that can never succeed — before this, `set_password` had no caller and real mode was locked out. `create_initial_password()` refuses once a hash exists, so the unauthenticated setup surface can never reset an existing password (pinned by `tests/services/auth/test_dashboard_auth.py` and the wiring pin in `tests/frontend/test_auth_gate.py`).
- The `frontend-reaches-the-backend-through-controllers` contract is being driven from 59 violations to zero in restructure phase 1; only one phase-1 task touches money.
- Placement rule: extract "on the second caller, not in anticipation" — one section's helper stays in that section; two sections → `<page>/_shared.py`; two pages → `components/<domain>/`.
- Classic split failure: a section calling `log.warning` while `log = logging.getLogger(__name__)` stayed in `__init__.py` — renders fine, `NameError` only on the error path. `tests/frontend/test_page_packages_are_wired.py` catches it statically.
- Splitting a module that rebinds a global via `global` forks that state — each module gets its own copy.
- One `ui.timer()` per dataset, paired with `asyncio.ensure_future(_refresh())` so the page populates before the first tick. Heavy work in a timer blocks the event loop for *every* connected client — an unattended VPS browser tab was directly implicated in stalls; that's why headless mode exists.
- History at `days=3650` forced the WebSocket buffer from 1MB to 10MB; ping/reconnect tuned in `run.py` for throttled background tabs.
- `app.py` patches a NiceGUI 3.12.x bug (`parent_slot` dead-weakref `RuntimeError` on client disconnect).
- MT5 timestamps: use `_uk(ts)` from `pages/trading/_shared.py`; do not roll your own.
- **A backend column with no control anywhere is a setting that can only ever be wrong (2026-09-05, bugs/024).** `channel_parser_config.instant_entry_enabled` is the per-channel half of the gate that turns a bare direction into a real market order, and it reached the frontend in exactly two places, both of which only echoed it back unchanged. So the only value it ever held was whatever a channel's auto-bootstrap wrote on first sight -- and because that config is keyed by `channel_name`, not `group_id`, a channel renamed on Telegram's side silently gets a fresh row. A channel therefore matched its BUY trigger correctly, with the global gate open, and placed nothing, with nothing on screen able to say why. The switch now sits beside the enable switch on the Channels Active card. **Two lessons.** First: when a save takes several positional arguments and two of them are adjacent booleans, do not write a second call site -- `_feed._save_channel_flags()` is keyword-only precisely so the pair cannot be swapped, a mutation that type-checks, runs, and disables the channel while reporting that instant entry changed. Second: `tests/frontend/conftest.py`'s harness stubs a reader with **no slots**, so any card that draws per-channel controls renders its empty state there and a render test through that harness proves nothing about it. Render the section detached into a `ui.card()` instead, walk the element tree and fire the real handler -- same code path, with the data the card is about. `tests/frontend/test_channel_instant_entry_toggle.py` is the reference.
- A page's render tests must pin its *settings*, not only its most eye-catching card. The Parsing tab shipped from the upstream merge (1e383fe) with `_render_parsing_settings_section` an empty stub and its whole body parked in `_render_logic_keywords_section`, which nothing called: every switch on the tab disappeared from the UI while staying fully wired in the backend, so `immediate_market_entry` could not be turned on and a bare "Buy Now" signal was missed. The telegram landmarks in `test_remaining_pages_render.py` pinned only the auth wizard, so nothing went red. Fixed 2026-08-26; `tests/frontend/test_parsing_settings_render.py` now pins the block and asserts every row of `_PARSING_CATEGORIES` reaches the screen.
- A settings switch that vanishes fails *silently and expensively*: the DB column keeps its default, the backend keeps gating on it, and the page still renders. Deleting a toggle is never a cosmetic change.
- `test_panel.py` is actually the Bounce engine — named after the service, not what the user calls it.
- Permanent LOC exemptions with written reasons: `mt5_bridge.py` (separate interpreter) and `runtime.py` (composition root, stopped at 1,310 lines).

## Open questions

Four owner decisions are open in `docs/todo/refactor/frontend/restructure/QUESTIONS.md`:
split-or-exempt `app.py`; split-or-exempt `chart.py` (839 lines); whether
phase-2 splits need new tests beyond boot smoke + import tests; whether
phase 1 must land in one release. `components/` has no established
convention yet (phase 2, task 010). Whether a React port ever happens is
deferred — "decidable later on evidence."
