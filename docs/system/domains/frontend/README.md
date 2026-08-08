# Frontend

**Living file — update when this domain teaches you something.**
Covers: `frontend/`. The canonical rule set is the
`/frontend-conventions` skill; the active restructure plan is
`docs/todo/frontend/restructure/` (spec `docs/specs/001-frontend-restructure.md`).

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
- `frontend/components/` — currently empty; intended layout is `components/<domain>/` and `components/shared/`
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

- The `frontend-reaches-the-backend-through-controllers` contract is being driven from 59 violations to zero in restructure phase 1; only one phase-1 task touches money.
- Placement rule: extract "on the second caller, not in anticipation" — one section's helper stays in that section; two sections → `<page>/_shared.py`; two pages → `components/<domain>/`.
- Classic split failure: a section calling `log.warning` while `log = logging.getLogger(__name__)` stayed in `__init__.py` — renders fine, `NameError` only on the error path. `tests/frontend/test_page_packages_are_wired.py` catches it statically.
- Splitting a module that rebinds a global via `global` forks that state — each module gets its own copy.
- One `ui.timer()` per dataset, paired with `asyncio.ensure_future(_refresh())` so the page populates before the first tick. Heavy work in a timer blocks the event loop for *every* connected client — an unattended VPS browser tab was directly implicated in stalls; that's why headless mode exists.
- History at `days=3650` forced the WebSocket buffer from 1MB to 10MB; ping/reconnect tuned in `run.py` for throttled background tabs.
- `app.py` patches a NiceGUI 3.12.x bug (`parent_slot` dead-weakref `RuntimeError` on client disconnect).
- MT5 timestamps: use `_uk(ts)` from `pages/trading/_shared.py`; do not roll your own.
- `test_panel.py` is actually the Bounce engine — named after the service, not what the user calls it.
- Permanent LOC exemptions with written reasons: `mt5_bridge.py` (separate interpreter) and `runtime.py` (composition root, stopped at 1,310 lines).

## Open questions

Four owner decisions are open in `docs/todo/frontend/restructure/QUESTIONS.md`:
split-or-exempt `app.py`; split-or-exempt `chart.py` (839 lines); whether
phase-2 splits need new tests beyond boot smoke + import tests; whether
phase 1 must land in one release. `components/` has no established
convention yet (phase 2, task 010). Whether a React port ever happens is
deferred — "decidable later on evidence."
