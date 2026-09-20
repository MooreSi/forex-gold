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
- `frontend/src/components/<domain>/` — `shell/`, `chart/`, `trading/`, `news/`, `about/`, `backtest/`, `parsing/`, `history/`, `ai/`, `engines/`, `settings/`. A domain folder is named after a business concept and matches the backend's vocabulary.
- `frontend/src/components/<domain>/content/` — copy and switch definitions transcribed from the NiceGUI pages **by parsing their source, not by retyping**: the Glossary's 47 terms, the 12 parsing switches, the reversal capabilities, the risk warning. A typo in a hand copy is a wrong definition nobody notices, and a missing switch is a feature that cannot be turned on.
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
- **`usePoll` must look its entry up by key on every render, never hold it in a ref (2026-09-18).** The first version used `useRef`, which initialises once — so when the key changed (the Chart tab's timeframe, the Analysis tab's window) the hook registered the OLD entry under the NEW key, the effect's dependency never changed, and no fetch happened. The panel went on showing the previous window's data with no error anywhere. The Chart tab's timeframe buttons were silently inert for two commits.
- **A numeric input's state is a string while it is being edited (2026-09-18).** `Number("1.")` is 1, so a field that stores a number drops the decimal point as it is typed and "1.25" arrives as 125 — a spread of 125 points instead of 1.25, which turns a profitable backtest into a disaster and still looks like a strategy result. `SettingsField` and the Backtest form both hold strings and convert once, on submit.
- **A settings field must re-sync after every save, not only when the value changes (2026-09-18).** "The backend rejected your number" and "the backend agreed with what was stored" both leave the value where it was, so a field keyed on the value alone keeps the rejected input on screen: typing 99 into a risk field the service clamps to 2 left "99" in the box. `useSettingsResource` exposes a `version` counter for this.
- **Guard the array boundary between the typed client and the untyped wire (2026-09-18).** A response that is an object where a list was expected makes `.map` throw *inside render*, and React tears down the whole tree — one wrong endpoint blanks the entire dashboard rather than one panel. `frontend/src/lib/asArray.ts` is that boundary — and `asObject` beside it, because a MISSING object field throws the same way (`Object.keys(undefined)`), which the shell test found by answering `{}` for every endpoint: exactly what a half-deployed backend looks like from the browser.
- **MT5 timestamps are UTC+3 encoded as an epoch.** `formatBrokerTime` in `shared/format.ts` shifts them back; it is the port of `_uk()` from the NiceGUI `pages/trading/_shared.py`. Do not roll your own — formatting the raw stamp puts every trade three hours into the future, which looks plausible.
- **The account badge has three states, not two.** A bridge that has not answered renders UNKNOWN in amber. A missing answer shown as "DEMO" is how somebody places a live order believing otherwise. Note that `is_demo` must be compared strictly: the string `"false"` is truthy.
- **A settings switch that vanishes fails silently and expensively.** The DB column keeps its default, the backend keeps gating on it, and the page still renders. This bit the Parsing tab under NiceGUI (shipped 1e383fe with its whole settings body in a function nothing called, so `immediate_market_entry` could not be turned on and a bare "Buy Now" signal was missed). It is a React problem in exactly the same way: when the Settings tab is ported (task 080), pin every row of the category list reaching the screen, not just the most eye-catching card.
- **A settings fixture must not use the code's own defaults.** Storing the values the component falls back to means a component that ignores the stored row produces identical output and the test passes. Proved by mutation under NiceGUI on 2026-09-07; the class of error is framework-independent.
- **`test_panel.py` was the Bounce engine** — named after the service, not what the user calls it. The React tab is "Signal Generator". Name a component after what the user calls it.
- Permanent LOC exemptions with written reasons: `mt5_bridge.py` (separate interpreter) and `runtime.py` (composition root).

## What each tab is built on

All ten tabs are React as of 2026-09-18. Three are narrower than their NiceGUI
originals, and the reasons are about the boundary rather than effort — see
`docs/todo/frontend/react-port/080-remaining-tabs.md` for the list, including
the Analysis tab's missing deal-level trade table (it needs `get_deal_history`
through a controller, which does not exist; the old page reached
`engine._bridge` directly).

**One consolidated read per tab.** The Analysis tab is the worked example:
three NiceGUI panels each polled the bridge independently, costing 4.3 calls a
minute at idle and 388 round-trips in 25 seconds on one page load (bugs/030).
The React answer is structural rather than a cache — one endpoint assembles
every panel and one shared poll reads it, asserted by
`test_a_full_render_costs_one_trip_into_the_broker`.

## The controller layer is swept, not sampled

`tests/controllers/test_controller_forwarding.py` asserts the definition in
`docs/system/rules/30-architecture.md` — *"a flat `<name>_controller.py` that
names an operation and forwards it to one service"* — against all 213
operations for which that is the whole specification. It resolves each call's
target from the source (including the function-local imports, which are
load-bearing here), replaces it, and binds both sides against their real
signatures so "my `source` arrived as the service's `source`" is a check rather
than a hope.

**Two things it taught, worth keeping.** Deriving the expectation from the
function body is not a test: the first version read "does it return?" from the
AST, so a forwarder that dropped its `return` also dropped the assertion that
would have caught it, and the mutation passed. The oracle moved to the return
annotation. And a membership check is not a position check: a swapped pair of
arguments passed until both calls were bound by name.

It found `news_controller.save_config` forwarding to `_config.save_config`,
which does not exist. Deleted — `settings_controller.save_config` is the one
that works, and was what the page actually used.

An operation that grows a branch leaves the sweep, and
`test_the_complex_operations_are_the_ones_we_know_about` fails until somebody
lists it and gives it a behavioural test. That is the intended friction: a
controller acquiring logic should cost a conversation.

## No NiceGUI anywhere

`nicegui` is not a dependency of this project. The last two screens that used
it — the pre-boot licence error and activation pages — became plain
server-rendered HTML on 2026-09-18
(`config/licence/activation_server.py`), and `no-nicegui-in-the-backend` is
enforced at zero.

**Plain HTML, not React, and deliberately so.** Those screens run before the app
starts and are the only way back into a stranded install: one that needed
`frontend/dist` to have been compiled could not rescue a broken one.
`test_the_form_renders_with_no_bundle_and_no_database` is the first test in that
file.

**One consequence, on the admin machine only.** The KeyGen licence console
(`~/Documents/KeyGen/forex_admin.py`) is a separate NiceGUI tool that lives
outside this repository, and `backend/src/app.py` imports it to offer the Admin
button. That import now fails on a machine with no nicegui installed — cleanly,
by design, because the admin console is optional and the trading app must start
without it. The button simply does not appear. `pip install nicegui` on that
machine brings it back.

## The committed bundle is not byte-identical across operating systems (2026-09-19)

`frontend/dist` is committed and is what the app serves, so CI rebuilds it and
fails on any diff. That check lived as a step in the Windows `checks` job and
failed on a tree whose `dist` was current.

It was not stale. Windows genuinely builds a different bundle from the same
source: **773.45 kB against macOS's 773.42 kB**, roughly 30 bytes and a
different content hash. The CSS hash matched exactly; only the JS moved.

Everything cheaper was ruled out first, in this order:

- **Dependency drift** — `npm ci` from the committed `package-lock.json`
  reproduced the committed hash exactly.
- **Node version** — CI pins 22, the dev machine had 26. Building with Node
  22.17.1 produced the same bytes. It changes only the *reported* gzip figure
  (229.19 against 229.69), because vite computes that with Node's own zlib.
  The file is identical; the number printed next to it is not. Worth knowing
  before chasing a gzip delta.
- **Line endings** — the whole source tree converted to CRLF locally rebuilt to
  the same hash. (The 2026-09-18 `.gitattributes` fix was a real but different
  problem: there the asset names matched and only `dist/index.html` differed.)
- **Absolute paths leaking in** — a build from a much longer, unrelated
  directory produced the same hash, so nothing about the build location is
  embedded.

What is left is that **esbuild and rollup ship a native binary per platform** —
`@esbuild/win32-x64` and `@rollup/rollup-win32-x64-msvc` against the
`darwin-arm64` pair. Same versions, different compiled minifiers, not
bit-identical output.

So cross-OS byte equality is not a property this toolchain has, and a check
asserting it was testing the runner rather than the commit. The check is
unchanged and not weakened — it still rebuilds from source and still fails on
any diff. It now runs as its own `bundle` job on `macos-latest`, the platform
`dist` is committed from, which is the only way the comparison means anything.
**If `dist` ever starts being committed from another OS, that job's runner has
to move with it.** The Windows `checks` job keeps the Python suite, which needs
Windows because MetaTrader5 is a win32 dependency.

Side benefit: bundle staleness is now reported in about two minutes instead of
behind a forty-minute Windows suite.

## Open questions

`docs/todo/frontend/react-port/QUESTIONS.md` — four, with provisional defaults
stated so work continues without them: whether unported tabs stay visible (the
default, and what is implemented, is yes with an honest placeholder); whether
the dashboard needs a phone layout; whether light mode is wanted now that CSS
tokens make it cheap; and how a release guarantees the committed bundle is not
stale.

## A recorder will not catch a shape mismatch (2026-09-18)

Every router test in `tests/api/routers/` replaces its controller with a
recorder, which is right for what those tests are about and blind to the one
thing that breaks a form in practice: **the router and the store disagreeing
about the shape of a write.** A recorder takes `(*args, **kwargs)`, so it
accepts any signature, any key and any order — and so does every controller,
because a controller is a forwarder by design.

Three defects shipped through a green suite because of this, all found on
2026-09-18 by a second pass rather than by a test:

* the Connections tab wrote `recipient` to a table whose column is `to_addr`;
* it wrote the Telethon reader's three credentials to the alert bot's table,
  which has none of them;
* `PUT /api/settings/telegram` passed the whole request body to a
  three-parameter `save_telegram_config(bot_token, chat_id, enabled)`.

**When a test needs to prove a call is correctly shaped, bind it against the
real signature** — `inspect.signature(real_fn).bind(...)`, the way
`tests/controllers/test_controller_forwarding.py` does and the way
`tests/api/test_settings_writes_reach_the_store.py` now does for the settings
forms. That file also reads the React tab's own field list and checks each name
against `backend/migrations/schema_sql.py`, so a field added to a form and not
to the schema fails in CI rather than at the operator's keyboard.

## The header's LOCAL/REMOTE control is a money path (2026-09-18)

`PUT /api/node/active-trader` decides which of two paired nodes may open
positions against the shared MT5 account. It is **not** a flag write, and it
was one between the port and 2026-09-18 — setting `local` without the peer
standing down leaves two nodes each believing they own the account.

The sequence lives in `backend/src/services/cluster/handover.py` and the
ordering is the safety property: a peer that does not acknowledge leaves the
account with **no** active trader rather than two. Anything that touches that
file needs the owner's sign-off and a demo session, the same as the close path.

## One table of which engines exist (2026-09-18)

`backend/src/services/engines/registry.py`. `engines_controller` held it and
`services/cluster/handover.py` grew a second copy, which is how the empty
`bounce` slot gets re-introduced in one of them and not the other. The slot's
NAME stays although its code went on 2026-09-14, because `server_start` binds
(breakout, bounce, reversal) positionally and a paired node on an older build
would otherwise see Reversal shift into Bounce's place.


## Catching up with `MooreSi/forex` (2026-09-18)

`MooreSi/forex` keeps running the NiceGUI dashboard and keeps moving. Merging
it into this branch is routine, and the cost is predictable: **it is
proportional to how many UI-reachability tests the upstream work brought with
it, not to how many lines it changed.**

The first catch-up (`1d594cb`..`0622ea7`, ~5,600 lines) produced five conflicts
— four NiceGUI files this branch has deleted, which stay deleted, and one
`__all__` both sides had added to. Of 8,463 tests, five failed, every one of
them a test that reads a NiceGUI page's source to prove a switch is reachable.

Those tests are right to exist and must not be deleted: `docs/todo/refactor`
records a guardrail that scanned a deleted directory and printed "all good" for
months. **Re-point them at the React source and keep the assertions.**
`tests/refactor/_react_port.py` has the helpers; `test_cme_context_switch.py`
and `test_decision_log_is_wired.py` are the worked examples.

One trap, found the hard way: a test that reads forward from the first
occurrence of a key in a file will happily match a COMMENT above the real
thing. A comment that quotes the phrases the test looks for satisfies it on its
own, and the code underneath can then say anything. Do not restate a test's
expected strings in a comment next to the thing it checks.


## Which NiceGUI page belongs on which React tab (2026-09-20)

Two tabs were carrying the wrong page. Worth writing down, because the names
are close enough that it was invisible for weeks:

* **AI Analysis** is `frontend/pages/ai_summary.py` — one Research Now button,
  **one** model call across every metric, a structured answer rendered as
  cards (sentiment + confidence, the day's range, drivers, risks, levels, a
  strategy recommendation). The port instead put the three-subject trade
  analysis here and printed the model's raw prose.
* **Analysis** is where `ai_trade_analysis` lives
  (`frontend/pages/history/__init__.py` renders it), as a section of the
  history tab. That is where the three-subject panel now sits, as a sub-tab.
* **Signal Generator** has two sub-tabs and always did —
  `frontend/pages/test_panel/__init__.py` builds Breakout and Reversal Engine.

The general lesson: when a React tab "feels wrong" to the owner, check which
NiceGUI module the tab of that name actually rendered before redesigning it.
The original is in `~/Forex-Update`, not in this checkout.

### One question, one call

`ai_summary` asks the model once and gets a structured answer back; the
three-subject page asks once per subject and gets prose. Both are still here
and both are right for what they do — but the tab an operator opens daily is
the first kind. A call per metric costs several calls and produces several
views that can contradict each other.

### The learning chart

`frontend/components/learning_chart.py` upstream carries a finding that cost
real time to get: both engine panels plotted a **cumulative** mean, which over
the Reversal engine's ~4,880 labelled signals is a constant with extra steps
and cannot answer "is it learning?". The React port
(`components/engines/internal/learning_series.ts`) uses the rolling window and
keeps the reasoning in its docstring. The two lines it draws are on different
scales and are deliberately **not** comparable to each other.
