# Frontend

**Living file — update when this domain teaches you something.**
Covers: `frontend/` (the React dashboard) and `backend/src/api/` (the HTTP layer
it talks to). The canonical rule set is the `/frontend-conventions` skill. The
port's plan pack is `docs/todo/frontend/react-port/`; the decision behind it,
and the 2026-08-06 decision it reverses, are in
[010-the-react-decision.md](010-the-react-decision.md).

## What it is

A **React dashboard**, written in TypeScript, compiled by Vite into
`frontend/dist`, and served as static files by the same FastAPI process that
serves the JSON API. There is no Node runtime at run time and no second
process: the bundle is built by a developer and committed, which is what keeps
the install Python-only.

It replaced a NiceGUI dashboard on 2026-09-18 in this repository
(`MooreSi/forex-react`). `MooreSi/forex` still runs the NiceGUI version.

```
browser  →  frontend/dist (React)  →  HTTP/JSON  →  backend/src/api/
                                                        │ named questions
                                                    controllers/ → services/ → db/
```

## Where the code lives

- `frontend/src/api/client.ts` — the one HTTP client. Nothing calls `fetch` directly. It is also where a 401 becomes "go to the login screen" and where a refusal is told apart from a failure.
- `frontend/src/hooks/usePoll.ts` — the **only** polling primitive. One interval per key however many components subscribe, deduped in flight, paused while the tab is hidden.
- `frontend/src/components/shared/` — `PanelShell`, `DialogShell`, `Button`, `StatCard`, `EmptyState`, `NotPortedPanel`, and `format.ts`, the single place money, prices and MT5 timestamps are formatted.
- `frontend/src/components/<domain>/` — `shell/`, `chart/`, `trading/`. A domain folder is named after a business concept and matches the backend's vocabulary.
- `frontend/src/index.css` — the colour tokens. Dark only.
- `frontend/static/` — favicon and icons, served at `/static`.
- `frontend/dist/` — the compiled bundle. **Committed on purpose.**
- `backend/src/api/server.py` — the composition root: mounts routers, then the bundle, and injects the engine.
- `backend/src/api/routers/` — one per domain. `orders.py` is the money router and is separate from `trading.py` deliberately.

## Constraints / must not change

- **Layer rule:** `frontend (browser) → backend/src/api → controllers → services → db`. A router asks a controller a named question and never reaches past it. Two contracts enforce it at zero: `the-api-layer-never-imports-the-database` and `the-api-layer-reaches-the-backend-through-controllers`. `server.py` is the single named exemption, because it holds the engine handle.
- **No SQL and no `backend.src.db` import anywhere in `backend/src/api/**`** — enforced at zero.
- **Controllers:** flat `<name>_controller.py`, never a package, hard 200-line ceiling, no loops/merges/formatting/fallbacks. Routers inherit the same ceiling and the same "no logic" rule.
- **Semantic colours are frozen.** green = profit, red = loss, yellow/amber = warning and the app's accent, blue = remote/VPS, gray = neutral. They are CSS tokens now instead of hand-written utility classes; they are the same colours. There is no light mode (QUESTIONS Q3).
- **Money rules, unchanged from the NiceGUI conventions:** every order action goes through a controller; an explicit confirmation names instrument, direction and size; the backend decides whether an order is allowed and the page renders the answer; a refusal is surfaced verbatim; demo vs live is unmistakable; no destructive action on a single click.
- **Component size budgets:** Dialog/Panel/Tab top-level files under 150 LOC, 250 is a refactor warning, 400 is a hard stop. Push state into a `hooks/use*Controller.ts` and JSX into `internal/`.

## Known things & gotchas

- **The SPA fallback is middleware, not a catch-all route (2026-09-18).** A `@app.get("/{full_path:path}")` route full-matches every path, which defeats Starlette's partial-match handling: a GET to a POST-only endpoint stops being a 405 and becomes whatever the catch-all returns. On `orders.py` that matters — "GET /orders/market returns 404" reads as "that endpoint does not exist" and sends the next person hunting a bug that is not there. Middleware runs after routing, so a real 405 stays a 405. Pinned by `tests/api/test_static_bundle_is_served.py`, and the planted mutation was watched go red.
- **Mount the routers before the bundle.** With the order reversed, `/api/chart/timeframes` returns `index.html` with a 200. Nothing errors; every panel just renders empty.
- **A fair-value gap whose index is outside the candle window must be dropped, not returned without a timestamp (2026-09-18).** The response model requires `ts`, so one stale index failed validation for the *whole* overlay payload — EMAs and RSI included. One unplaceable zone should cost that zone.
- **The API answers 401 with JSON, never a redirect.** A 302 to an HTML login page inside an XHR is how a session expiry becomes a login form rendered inside the trading panel.
- **`auto_login_enabled` is read from the user's config file, so a test that depends on it must set it.** The first version of `test_an_unauthenticated_order_request_is_rejected` passed by accident on a machine where the operator had the setting on. Anything that reads the real config in a test is asserting something about whoever ran it.
- **Guard the array boundary between the typed client and the untyped wire (2026-09-18).** A response that is an object where a list was expected makes `.map` throw *inside render*, and React tears down the whole tree — one wrong endpoint blanks the entire dashboard rather than one panel. `frontend/src/lib/asArray.ts` is that boundary. Found by a shell test whose stubbed fetch returned `{}` for everything.
- **MT5 timestamps are UTC+3 encoded as an epoch.** `formatBrokerTime` in `shared/format.ts` shifts them back; it is the port of `_uk()` from the NiceGUI `pages/trading/_shared.py`. Do not roll your own — formatting the raw stamp puts every trade three hours into the future, which looks plausible.
- **The account badge has three states, not two.** A bridge that has not answered renders UNKNOWN in amber. A missing answer shown as "DEMO" is how somebody places a live order believing otherwise. Note that `is_demo` must be compared strictly: the string `"false"` is truthy.
- **A settings switch that vanishes fails silently and expensively.** The DB column keeps its default, the backend keeps gating on it, and the page still renders. This bit the Parsing tab under NiceGUI (shipped 1e383fe with its whole settings body in a function nothing called, so `immediate_market_entry` could not be turned on and a bare "Buy Now" signal was missed). It is a React problem in exactly the same way: when the Settings tab is ported (task 080), pin every row of the category list reaching the screen, not just the most eye-catching card.
- **A settings fixture must not use the code's own defaults.** Storing the values the component falls back to means a component that ignores the stored row produces identical output and the test passes. Proved by mutation under NiceGUI on 2026-09-07; the class of error is framework-independent.
- **`test_panel.py` was the Bounce engine** — named after the service, not what the user calls it. The React tab is "Signal Generator". Name a component after what the user calls it.
- Permanent LOC exemptions with written reasons: `mt5_bridge.py` (separate interpreter) and `runtime.py` (composition root).

## Still NiceGUI, and why

`backend/src/config/licence/guard.py` renders the **licence error screen and the
activation screen** with NiceGUI, and it runs *before* the main app starts, in
its own `ui.run()`. It is the reason `nicegui` is still in `requirements.txt`
and the reason `no-nicegui-in-the-backend` still carries 2 baselined
violations. Porting it is task 090 in `docs/todo/frontend/react-port/`. It was
left alone deliberately rather than rushed: it is a licence surface, it is
~280 lines of interactive flow (remote agents, delivery polling, manual
activation), and a half-ported activation screen locks people out of an app
they have paid for.

## Open questions

`docs/todo/frontend/react-port/QUESTIONS.md` — four, with provisional defaults
stated so work continues without them: whether unported tabs stay visible (the
default, and what is implemented, is yes with an honest placeholder); whether
the dashboard needs a phone layout; whether light mode is wanted now that CSS
tokens make it cheap; and how a release guarantees the committed bundle is not
stale.
