---
name: frontend-conventions
description: Where frontend code lives, how a NiceGUI page is decomposed, and the do's and don'ts of writing UI in this app. Use when creating, moving or splitting anything under frontend/, deciding which module a new section belongs in, adding a control that can move money, or naming a new page/section/component. Covers the controller boundary, page-package shape, size budgets, theme and colour rules, refresh timers, and money-safety in the UI. Pairs with docs/system/rules/70-file-organisation.md (the mechanics of splitting) and docs/system/rules/30-architecture.md (layers).
user-invocable: true
allowed-tools: Read, Grep, Glob
---

# Frontend conventions

The canonical rule set for **where** frontend code lives and **how** a page is decomposed.

This app is **NiceGUI (Python)**, not React. Every widget is a Python object created inside a
`with` block; the browser is a rendering surface, not a place where your code runs. If you find
yourself reaching for a React idiom, you are about to write something this codebase cannot hold.

These rules apply to `frontend/**`. They do not apply to `backend/`.

**Related docs — read rather than duplicate:**

| Topic | Owner |
|---|---|
| How to split a big file, mechanically | [docs/system/rules/70-file-organisation.md](../../../docs/system/rules/70-file-organisation.md) |
| Layers and which import is legal | [docs/system/rules/30-architecture.md](../../../docs/system/rules/30-architecture.md) |
| What can cost money | [docs/system/rules/20-trading-safety.md](../../../docs/system/rules/20-trading-safety.md) |
| Test protocol | [docs/system/rules/40-testing.md](../../../docs/system/rules/40-testing.md) |

---

## 0. Stop conditions (read first, every time)

If any of these is true, **stop and fix it before writing more code.** Do not finish the edit and
clean up later — later doesn't come.

- **You are about to `import` anything from `backend.src` that is not `backend.src.controllers`.**
  Stop. The `frontend-reaches-the-backend-through-controllers` contract counts it, and it is being
  driven to zero. `backend.src.utils.models` and `backend.src.config` are the two narrow exceptions
  (constants and config only — see §2).
- **You are about to take a page file past 800 lines.** That is the LOC gate. Split it into a package
  first — [70-file-organisation.md](../../../docs/system/rules/70-file-organisation.md) — then make your edit
  in the smaller module.
- **You are about to add a fourth `_render_*` function to a module.** Three is the limit. The fourth
  means the module is now two sections; give it a name and split it.
- **You are about to add a control that opens, closes, resizes or re-prices a position.** Stop and
  read [`/safe-change`](../safe-change/SKILL.md). A button that can lose money is not a UI change.
- **You are about to write SQL, open a DB connection, or import `backend.src.db`.** Stop. That
  contract is enforced at zero and the `ui_db` gate is empty; you would be the first violation since
  M3. Pages ask a controller a named question.
- **You are about to remap green, red, yellow or amber to mean something new.** Stop. In this app
  green = profit, red = loss, yellow/amber = warning or attention. `theme.py` deliberately leaves
  every semantic accent untouched across all four presets for exactly this reason.
- **You are about to add a second `ui.timer()` to a page that already has one refreshing the same
  data.** Stop. Two timers on one dataset produce two different answers on screen and double the
  load on the event loop.
- **You are about to build a second way to do something the app already does** — another stat row,
  another P&L colour helper, another trade table, another confirmation dialog. Stop and check §5.
  A near-duplicate is more expensive than the thing it duplicates, because now both must be
  maintained and the user has to learn which is which.
- **You are about to hoist a function-local import to module level.** Stop. In this app a
  function-local import is usually load-bearing — it defers a heavy or circular import past app
  boot. `app.py:982`, `:1127`, `:1246` are all deliberate. Check `git blame` before moving one.

Reaching a stop condition mid-edit means abandoning the in-flight edit, doing the fix first, then
restarting. One extra round-trip beats a 3,112-line file nobody can land safely.

---

## 1. The layer rule (the one that is enforced)

```
frontend/ → controllers/ → services/ → db/
```

A page **asks a controller a named question**. It does not reach past it.

```python
# ✅ DO
from backend.src.controllers import trading_controller as trading_ctl
state = trading_ctl.get_circuit_breaker_state()

# ❌ DON'T — counted by the contract, and it puts a service call on the UI event loop
from backend.src.services.risk import strategy_params as _sp
```

**If the controller doesn't expose what you need**, the answer is never "reach around it". In order
of preference:

1. The service already has the function → add a flat forwarding function to the controller.
2. The service doesn't → add the named function **to the service**, then forward to it.
3. Neither fits → you are probably asking the wrong question. Say so rather than inventing a layer.

**Controllers have a hard 200-line ceiling, enforced at zero** (`structure_gates`). A controller is a
flat `<name>_controller.py` — never a package. If forwarding functions would push one past 200, that
is the signal that the *service* should expose one coarser function, not that the controller needs
room. Do not split a controller into a package to make space; the gate rejects it, and
`services/cluster/remote/` is the recorded reason why.

**Never put logic in a controller.** No loops, no merges, no formatting, no fallbacks.
`history_controller` acquiring three-source ledger merges is the documented example of how that ends.

### The two narrow exceptions

`backend.src.utils.models` (strategy-name constants and dataclasses) and `backend.src.config`
(configuration values) are allowed. Both are leaf modules that depend on nothing above them, and
neither performs an action. Everything else goes through a controller.

---

## 2. Where things live

```
frontend/
  app.py                    # the shell: framework patches, lifecycle hooks, header, composition
  theme.py                  # theme presets — see §6
  static/                   # favicon, banner, icons
  pages/
    <page>.py               # a page that still fits in one file
    <page>/                 # a page that outgrew it — see §3
      __init__.py           # render() and the page's own wiring. Nothing else public.
      _<section>.py         # one coherent section
      _shared.py            # helpers used by MORE THAN ONE section of THIS page
  components/
    <domain>/               # components used by more than one PAGE
    shared/                 # generic primitives with no domain knowledge at all
```

**The placement rules, in order:**

1. **Used by one section of one page** → it lives in that section module. Not in `_shared.py`, not
   in `components/`.
2. **Used by two sections of one page** → `<page>/_shared.py`. `pages/trading/_shared.py` is the
   worked example: `_pnl_colour`, `_stat_cell`, `_uk`, small and pure.
3. **Used by two pages** → `components/<domain>/`. **On the second caller, not in anticipation of
   one.** Speculative sharing is how a components directory fills with things one page uses through
   three optional parameters.
4. **Generic, no domain knowledge** → `components/shared/`. If the file mentions a trade, a signal,
   a strategy, a channel or an engine by name, it does not belong here.

**Do not create a near-empty directory.** A domain with one component lives at
`components/<domain>/<name>.py` — no sub-directories until they earn their place.

**Do not re-export a domain component through `shared/` to make it look generic.** Import it from the
domain that owns it.

---

## 3. Page shape

A page module's public surface is **`render()`** and nothing else, unless another page genuinely
renders part of it (`trading` exports `render_signals_card` because the Telegram page draws it too —
that is the bar).

`__init__.py` composes. It says *what appears and in what order*, and it does not implement:

```python
# ✅ DO — pages/trading/__init__.py, the reference implementation
with ui.tab_panels(trade_tabs, value=t_strategy).classes("bg-gray-900 p-4"):
    with ui.tab_panel(t_strategy):
        _render_strategy(engine)
    with ui.tab_panel(t_active):
        _render_active_trades(engine)
```

**Section modules are `_`-prefixed** — they are internals of the package, not an API. A sibling
imports what it needs explicitly; nothing is re-exported "just in case".

**Every section module needs its own module-level assignments.** The classic split failure is a
section calling `log.warning(...)` while `log = logging.getLogger(__name__)` stayed in `__init__.py`
— it imports fine, renders fine, and raises `NameError` the first time that error path runs,
replacing the real error with a confusing one. `tests/frontend/test_page_packages_are_wired.py`
catches this statically. Run it after any split.

**Watch for module-level state.** If a module rebinds a global via `global`, splitting *forks that
state* — each module gets its own copy and writes go to the wrong one. Move the state to a module
both import, or do not split.

---

## 4. Size budgets

The repo-wide gate is **800 lines**, shrink-only, baselined in `structure_baseline.json`.

| Tier | Lines | Action |
|---|---|---|
| Comfortable | <400 | Default for a section module. |
| Acceptable | 400–600 | Fine if it is one coherent section. |
| Refactor warning | 600–800 | Extract a section before adding more. |
| Hard stop | >800 | Fails the gate. Cannot land. |
| Controllers | >200 | Fails the gate, enforced at zero, no baseline. |

**A file on the baseline is not permission to grow it.** The ratchet fails if any listed file gets
longer. Adding to `settings.py` today means splitting it today.

**Do not split untested code to hit a number.** From
[70-file-organisation.md](../../../docs/system/rules/70-file-organisation.md): tests exist and pass → split →
the same tests still pass, unmodified. If step 1 is missing, step 1 *is* the task.

**An exemption with a written reason is a decision. Without one it is an oversight**, and the file
cannot tell you which it is looking at. `mt5_bridge.py` (separate interpreter) and `runtime.py`
(composition root at its floor) are the two recorded permanent exemptions.

---

## 5. Reuse before you build

Check this table before writing a new helper. Everything in it already exists.

| Need | Use | Where |
|---|---|---|
| Colour a P&L number | `_pnl_colour(v)` | `pages/trading/_shared.py` |
| Background for a P&L row | `_pnl_bg(v)` | `pages/trading/_shared.py` |
| A label + value stat cell | `_stat_cell(label, value, cls)` | `pages/trading/_shared.py` |
| Format an MT5 broker timestamp | `_uk(ts)` | `pages/trading/_shared.py` — MT5 stamps are UTC+3 encoded as epoch; do **not** roll your own |
| TP1–TP8 progress chips | `_tp_progress(triggered, trade)` | `pages/trading/_shared.py` |
| The signals card | `render_signals_card()` | `pages/trading` |
| Trade source / channel labels | `trade_source_label`, `trade_channel_label` | `controllers/history_controller` |
| Read or write a user preference | `settings_controller.get_app_config` / `set_app_config` | never `db.database` |
| Make a constant user-editable | [`/add-tunable`](../add-tunable/SKILL.md) | Settings → Expert Tunables, rendered generically |

**The variant test.** Adding a display for a *variant* of something already shown? It gets an extra
pill or line on the **existing** component — never a parallel one. If you are filtering rows out of
an existing table so your new component can render them instead, that exclusion is the smell, not the
solution.

---

## 6. Colour, theme and styling

The app is built from **literal dark-mode Tailwind utility classes**, hand-written on every element.
There is no CSS-variable abstraction and adding one is out of scope.

**Semantic colours carry meaning. Never remap them:**

| Colour | Means |
|---|---|
| green | profit, connected, running, success |
| red | loss, disconnected, stopped, danger |
| yellow / amber | warning, attention, the app's own brand accent |
| blue | informational, the remote/VPS node |
| gray | neutral, disabled, absent data |

`theme.py`'s four presets override **only** the neutral scale — `bg-gray-*`, `border-gray-*`,
`text-gray-*`. Every accent is deliberately left alone, because remapping profit-green would be
actively harmful.

**Do:**
- Use the neutral scale (`bg-gray-900/800/750/700`, `text-gray-200/300/400/500`) for structure — it
  is what the theme presets actually re-skin.
- Use `font-mono` for any number the user might compare against another number.
- Give a control a `.tooltip()` when its label cannot be self-explanatory in three words.

**Don't:**
- Hard-code a hex colour in a page. If you need one the theme can't express, that is a `theme.py`
  change, not a page change.
- Add a light-mode style. There is no light mode; a genuine one needs Quasar's internal component
  styling overridden everywhere, which is explicitly out of scope.
- Introduce a new `bg-gray-*` step. `bg-gray-750` is custom and already defined in every preset;
  a `bg-gray-650` would silently fall back to un-themed in all four.

---

## 7. Live data and refresh

Pages are refreshed by `ui.timer()`. This is a real cost: every timer runs on the server's event
loop, for every connected client, forever.

**Do:**
- One timer per dataset, owned by the section that displays it.
- `asyncio.ensure_future(_refresh())` alongside `ui.timer(n, _refresh)` so the page is populated
  before the first tick, exactly as `pages/trading/__init__.py:106-107` does.
- Pick the interval from how fast the data actually changes. 5s for account/positions is the
  established cadence; a static config panel does not need a timer at all.
- Wrap a refresh body so one failure doesn't kill the timer — but log it. A bare `except: pass`
  around a whole refresh hides a broken page behind stale numbers.

**Don't:**
- Add a second timer for data an existing one already fetches.
- Do heavy work in a timer callback. It blocks the event loop for *every* connected client. An
  unattended browser tab on the VPS was directly implicated in event-loop stalls — which is why
  headless mode exists.
- Assume the page is visible. Browsers throttle background tabs; that is why `ws_ping_interval` and
  `reconnect_timeout` are tuned in `run.py:262-277`.
- Push an unbounded payload. History at `days=3650` is what forced the WebSocket buffer from 1MB to
  10MB (`app.py:48-60`). A new unbounded view is a new instance of that bug.

---

## 8. Money in the UI

**This app places real orders on a live MT5 account with real money.** The frontend is where the
button is.

**Do:**
- Route every order action through a controller. No exceptions.
- Require an explicit confirmation for anything that opens, closes or resizes a position. State the
  instrument, direction and size in the confirmation — not just "Are you sure?".
- Show the disabled reason. A greyed-out Execute button with no explanation is indistinguishable from
  a broken one. Say *trading paused*, *bridge disconnected*, *stood down as Remote*, *out of hours*.
- Make demo vs live unmistakable. The account badge exists for this.
- Surface a rejection verbatim. If the backend refuses an order, the user needs the real reason, not
  "something went wrong".

**Don't:**
- Let a page decide whether an order is allowed. The page renders the answer; the backend decides it.
  Duplicating a risk check in the UI produces two answers that drift.
- Add an "are you sure" that defaults to yes, or a destructive action on a single click.
- Show a stale P&L without indicating it is stale. A number that stopped updating reads as a number
  that stopped moving.
- Write a test that places, closes or modifies a real **or demo** order. Fakes and sentinels only —
  and if a test file could reach a broker call, it says so in its docstring and proves it cannot.

---

## 9. Adding a new page or section

1. **Does it belong in an existing page?** A new tab on `trading` beats a new top-level page nobody
   finds. Check the existing tab set first.
2. **Name it after what the user calls it**, not after the service behind it. (`test_panel.py` is the
   Bounce engine — a standing example of what that costs.)
3. **Start as one file** at `pages/<name>.py`. Create the package only when it crosses ~600 lines or
   has a genuine second section.
4. **Wire it through a controller from the first line.** Retro-fitting the boundary is how the count
   got to 99.
5. **Open the nearest sibling page and match it.** Building an engine panel? Read
   `breakout_panel.py` and follow its structure — status header, controls, stats, recent signals —
   before inventing your own. A page that doesn't look like its siblings is a bug even when every
   line is correct.
6. **Register it** in `app.py`'s tab wiring, and add it to `tests/frontend/test_pages_render.py`.
7. **Add its terms to the in-app Glossary** if it introduces vocabulary. The Glossary is the app's
   real user manual, not an afterthought.

---

## 10. Known state (parking lot)

Current as of 2026-08-06. New code must not extend these — split first, then add.

**Over the 800-line gate** (all baselined in `structure_baseline.json`):

| File | Lines | Plan |
|---|---|---|
| `pages/settings.py` | 3,112 | package split — 8–9 unrelated settings domains in one file |
| `app.py` | 1,633 | package split — ~530 lines of it is About/Glossary *content* |
| `pages/history.py` | 1,416 | package split |
| `pages/ai_trade_analysis.py` | 1,250 | package split |
| `pages/test_panel.py` | 1,246 | package split |
| `pages/breakout_panel.py` | 919 | package split |
| `pages/chart.py` | 839 | 39 over — likely a deliberate exemption |
| `pages/reversal_panel.py` | 804 | 4 over — likely a deliberate exemption |

**The open boundary:** `frontend-reaches-the-backend-through-controllers` stands at **59**
violations (down from 99). It is baselined shrink-only and is being driven to zero, after which it
becomes the fifth contract enforced at zero.

**`frontend/components/` is empty** — created, never used. §2 is the rule for filling it, and the
second-caller rule is what keeps it honest.

**Known duplication, deliberately not fixed:** the three engine panels (`breakout_panel`,
`reversal_panel`, `test_panel`) are structurally near-identical. Collapsing them is a behaviour-risk
change wearing a restructure's clothes — they look alike and behave differently. Noted, not actioned.

**The live plan for all of the above:** `docs/todo/refactor/frontend/restructure/` (spec
`docs/specs/001-frontend-restructure.md`). Read it before starting any of this work — the tasks are
already ordered, and several have characterization tests that must be written first.

---

## What this skill is NOT

- Not a styling guide. Colour *semantics* are here (§6); visual design is not specified anywhere and
  changing it is its own spec.
- Not the split procedure — that is
  [70-file-organisation.md](../../../docs/system/rules/70-file-organisation.md), and it carries the four
  hard-won lessons from the trading split.
- Not a licence to refactor while passing. Restructuring and behaviour changes travel in separate
  commits, always.
