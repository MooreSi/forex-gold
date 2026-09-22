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
(`MooreSi/forex-gold`). `MooreSi/forex` still runs the NiceGUI version.

```
browser  →  frontend/dist (React)  →  HTTP/JSON  →  backend/src/api/
                                                        │ named questions
                                                    controllers/ → services/ → db/
```

## Where the code lives

- `frontend/src/api/client.ts` — the one HTTP client. Nothing calls `fetch` directly. It is also where a 401 becomes "go to the login screen" and where a refusal is told apart from a failure.
- `frontend/src/hooks/usePoll.ts` — the **only** polling primitive. One interval per key however many components subscribe, deduped in flight, paused while the tab is hidden.
- `frontend/src/components/shared/` — `PanelShell`, `DialogShell`, `Button`, `StatCard`, `EmptyState`, `NotPortedPanel`, and `format.ts`, the single place money, prices and MT5 timestamps are formatted.
- `frontend/src/components/<domain>/` — `shell/`, `dashboard/`, `chart/`, `trading/`, `news/`, `about/`, `backtest/`, `parsing/`, `history/`, `ai/`, `engines/`, `settings/`. A domain folder is named after a business concept and matches the backend's vocabulary.
- `frontend/src/components/<domain>/content/` — copy and switch definitions transcribed from the NiceGUI pages **by parsing their source, not by retyping**: the Glossary's 47 terms, the 12 parsing switches, the reversal capabilities, the risk warning. A typo in a hand copy is a wrong definition nobody notices, and a missing switch is a feature that cannot be turned on.
- `frontend/src/index.css` — the colour tokens. Dark, light and auto: light landed 2026-09-19 (QUESTIONS Q3, answered by the owner). Only the neutral scale is re-skinned; every accent keeps its hue, and therefore its meaning, in both.
- `frontend/static/` — favicon and icons, served at `/static`.
- `frontend/dist/` — the compiled bundle. **Committed on purpose.**
- `backend/src/api/server.py` — the composition root: mounts routers, then the bundle, and injects the engine.
- `backend/src/api/routers/` — one per domain. `orders.py` is the money router and is separate from `trading.py` deliberately.

## Constraints / must not change

- **Layer rule:** `frontend (browser) → backend/src/api → controllers → services → db`. A router asks a controller a named question and never reaches past it. Two contracts enforce it at zero: `the-api-layer-never-imports-the-database` and `the-api-layer-reaches-the-backend-through-controllers`. `server.py` is the single named exemption, because it holds the engine handle.
- **No SQL and no `backend.src.db` import anywhere in `backend/src/api/**`** — enforced at zero.
- **Controllers:** flat `<name>_controller.py`, never a package, hard 200-line ceiling, no loops/merges/formatting/fallbacks. Routers inherit the same ceiling and the same "no logic" rule.
- **Semantic colours are frozen.** green = profit, red = loss, yellow/amber = warning and the app's accent, blue = remote/VPS, gray = neutral. They are CSS tokens now instead of hand-written utility classes; they are the same colours. Light mode (2026-09-19) moves their LIGHTNESS only -- #00cc88 on white fails contrast at 11px, which on a P&L column is not a styling problem -- and remaps nothing.
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
- **`/api/chart/trades` answers `pnl: null`; `/api/trading/trades` carries the running profit (2026-09-22).** The chart's payload exists to place markers. A positions list built on it shows an em dash for every position's P&L -- honest, and needlessly so, since the broker's number is one endpoint away. The Dashboard reads the chart's copy for the chart and the trading copy for the positions card, and `DashboardPanel.test.tsx` stubs them with DIFFERENT values so the two cannot be swapped back silently.
- **`/api/chart/candles` refuses a `count` below 10 (2026-09-22).** The query is declared `ge=10`, so asking for the two daily bars you actually want is a 422, not a short answer. The Dashboard's "today's move" showed an em dash for a figure the feed had all along, and the test suite could not see it: a stubbed fetch answers whatever it is asked. Found by opening the running app. `DAILY_COUNT` in `dashboard/hooks/useDashboardController.ts` asks for ten and reads the last.
- **The Set & Forget `zones` are sorted by price, not by distance (2026-09-22).** The backend picks the nearest few by `aoi.distance` and then re-sorts what it picked by `low`, so `zones[0]` is the LOWEST of them. A card labelling it "the nearest zone" would be confidently wrong; the Dashboard shows the range they span instead, and does not re-derive the distance in TypeScript.
- **MT5 timestamps are UTC+3 encoded as an epoch.** `formatBrokerTime` in `shared/format.ts` shifts them back; it is the port of `_uk()` from the NiceGUI `pages/trading/_shared.py`. Do not roll your own — formatting the raw stamp puts every trade three hours into the future, which looks plausible.
- **The account badge has three states, not two.** A bridge that has not answered renders UNKNOWN in amber. A missing answer shown as "DEMO" is how somebody places a live order believing otherwise. Note that `is_demo` must be compared strictly: the string `"false"` is truthy.
- **A settings switch that vanishes fails silently and expensively.** The DB column keeps its default, the backend keeps gating on it, and the page still renders. This bit the Parsing tab under NiceGUI (shipped 1e383fe with its whole settings body in a function nothing called, so `immediate_market_entry` could not be turned on and a bare "Buy Now" signal was missed). It is a React problem in exactly the same way: when the Settings tab is ported (task 080), pin every row of the category list reaching the screen, not just the most eye-catching card.
- **A settings fixture must not use the code's own defaults.** Storing the values the component falls back to means a component that ignores the stored row produces identical output and the test passes. Proved by mutation under NiceGUI on 2026-09-07; the class of error is framework-independent.
- **`test_panel.py` was the Bounce engine** — named after the service, not what the user calls it. The React tab is "Signal Generator". Name a component after what the user calls it.
- Permanent LOC exemptions with written reasons: `mt5_bridge.py` (separate interpreter) and `runtime.py` (composition root).

## What each tab is built on

All eleven tabs are React. Ten of them since 2026-09-18; **Dashboard** was
added on 2026-09-22 at the owner's request -- one screen that answers "what is
happening right now" without moving between the other ten.

**The Dashboard summarises and controls nothing.** No close button, no lot-size
box, no engine switch: every card names the tab that owns what it shows. A
control on a summary screen is one misclick from something that costs money,
and the tabs that own those controls confirm first.

**It opens no poll key of its own** (`dashboard/hooks/useDashboardController.ts`).
Every read is the key the owning tab already uses -- the shell's 5s header, the
chart's candles, the Analysis window, the free Set & Forget read -- so the tab
costs what the tabs it summarises cost, and two screens cannot disagree about a
number. Its tick comes from `/api/system/header`, which already carries one,
rather than from `/api/chart/tick`.

**Nothing on it bills a model.** The AI figures are read back from what the AI
Analysis tab last produced. A screen that bills by being looked at is a screen
nobody can leave open, and this is the tab left open all day. Three are narrower than their NiceGUI
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

## Four ports finished on 2026-09-21

Each was reported by the owner as "missing", and in three of the four the
backend was already there and working — what was missing was the surface, or
half of it. That is the shape to expect from the rest of the port backlog, so
**look for the service before writing one.**

- **EA template Import / Export.** `ea_templates.export_templates` /
  `import_templates` — the envelope, the validate-everything-before-writing
  rule and the overwrite guard — have been in the tree since the NiceGUI app,
  and `broker_controller` already forwarded all four functions. Only the two
  routes (`GET /api/trading/templates/export`, `POST …/import`) and the
  buttons were missing. Both routes are declared **before** `/{name}`, the
  same trap `/schema` documents. `TemplateTransfer.tsx` reads the file with
  `FileReader`, not `File.prototype.text` — jsdom does not implement the
  latter, so a component using it reads every import as "chosen.text is not a
  function" under test while working in the browser.

- **The keep-alive watchdog toggle.** Not missing: reported by half and
  labelled as something else. `/api/node/state` returned `installed` (does the
  OS scheduler entry exist) and never `enabled` (did the operator turn it on),
  and the checkbox — called "Start the app when this machine boots" — was
  bound to `installed`. So the one state worth showing, the setting on with
  the entry lost to an OS upgrade or a machine migration, rendered as a plain
  unticked box. Worse, the toggle **wrote to `/api/node/state`**, which has no
  PUT handler: it answered 405, `useSettingsResource.save` swallowed it into
  its own error field, and nothing reached the screen. It had never worked.
  `KeepAliveSection.tsx` reports four states and writes to
  `/api/node/autostart`. A *disarmed* watchdog is not a fault — the Stop
  scripts disarm it so Stop genuinely stops.

- **The equity curve.** Kept its realised-P&L-from-zero meaning, which is a
  documented decision and not a cosmetic one. What it gained is the axes:
  without them, +$120 and +$12,000 draw the identical picture. The tick values
  are 1/2/5×10ⁿ and always include zero (`equityGeometry.niceTicks`), and the
  gradient area closes back to the **zero line**, not to the floor of the box
  — filled to the floor, a losing window shades exactly like a winning one.
  The gradient's `id` carries the window length, because two of these on one
  page would share one definition.

- **About → Setup instructions.** Ported from `frontend/app/_about.py` and
  brought up to date rather than transcribed: every Settings path in the
  original names a NiceGUI page that no longer exists, and two ports moved
  with the fork (bridge 9000→9010, EA bridge 9101→9111, so the two apps can
  share a machine without one trading through the other's bridge).
  `SetupSection.test.tsx` asserts that every `Settings → X` in the copy names
  a tab this build has, and that neither old port number appears. Instructions
  that confidently send someone to a screen that is not there are worse than
  no instructions, and nothing else would catch it.

## What the owner reported on 2026-09-21, and what each one turned out to be

Nine reports in one sitting. Six of them were **a screen reading a field name
that is not a column**, or a screen that could not tell it had been lied to.
That is now the first thing to check when a panel "shows nothing".

- **Parsing → Channels could not be ticked or unticked.** Not a frontend bug at
  all: `PUT /api/parsing/channel-parser` called
  `save_channel_parser_config(channel, {…})` and that function takes **six**
  positional arguments. Every click raised `TypeError` and answered 500. The
  router's own test faked the writer with a two-argument lambda — a fake
  describing a function nobody had written — so it stayed green throughout.
  The merge moved into `channels/performance.set_parser_enabled` and is pinned
  against the real table. **A router test that fakes a controller function is
  only as true as its arity.**

- **Trading → Signals showed a direction, a status and four em dashes.** The
  rows are `vantage_signals` columns (`source_name`, `entry_low`, `stop_loss`,
  `lot_size`) and the table read `source` and `entry`, which are not columns.
  Fixed the way the Positions table was two days earlier: a response model
  (`SignalOut`) that **fills** the short names from the real ones and never
  renames them, because the signal editor and the commentary panel read the
  long names. `GET /api/trading/signals` had no response model at all before.

- **Positions showed no P&L, and one position where MT5 had two.** Both in the
  positions domain — `mt5_profit` is only written at close, and the table is
  the app's own record. See that domain's README.

- **The restart banner never went away.** The reconnect-and-reload in
  `AuthContext` only ran once something had *already* discovered the server was
  gone, and the only thing that ever read the session was the first mount.
  Every other poll fails quietly into its own error state, so a running page
  never noticed a restart. There is a 5s **heartbeat** now, which covers every
  cause — the header button, the Telegram `/restartapp`, an environment
  switch, the bridge watchdog, a crash — rather than the one button that was
  reported. The two "it worked" lines it left behind go through `Notice`,
  which clears itself; its countdown deliberately does **not** depend on
  `children`, because both parents poll and a fresh object each render would
  reset the timer for ever.

- **Switching account did the opposite of what the badge implied.** The badge
  shows what the BRIDGE says is connected; the direction of the switch comes
  from what the APP is configured for. An install had `account_env: live` while
  MetaTrader was logged into demo, so the badge read DEMO, the owner pressed it
  expecting live, and it correctly switched to demo. That disagreement is the
  half-switched state `services/broker/environment.py` exists to prevent, so
  it is now **stated** (`environment-mismatch`), the dialog names the account
  being left as well as the one being taken, and the control re-reads its state
  on a poll instead of once on mount.

- **Three statements of the halt on one header.** A badge of its own, the
  trading-status badge, and the Pause button growing into a "Paused" pill — on
  a bar that already forced the document to 1153px at a 1024px viewport. One
  survives: `TradingStatusBadge`, because it reads the backend's decision
  across all **four** mechanisms that can hold an entry where the removed badge
  knew about two. Its all-clear reads "Trading Active", not "Circuit Breaker
  OK" — naming one of the four mechanisms in the all-clear made the whole badge
  look like a breaker readout.

- **Two controls for one fact, 2026-09-22.** The Pause button survived that
  clean-up as an icon and kept its own pause/resume dialog, so the header had
  two controls for the same thing — and the Pause button read only
  `pause.paused`, the governor's manual pause. With a profit target reached or
  the breaker tripped it offered "Pause" while every automated entry was
  already held, and the way to lift it was the other control. Merged: the
  badge is the only one now (`TradingStatusDialog`), and which half it offers
  — Resume or the pause form — is the backend's own `can_resume`, never a
  second opinion computed here. A news blackout has no Resume (it lifts
  itself) but still offers the pause form, because a blackout is no reason to
  lose the ability to stop trading by hand.

## Hover help

`components/shared/Tooltip.tsx` is the one place hover help is drawn. Half the
app carried a `title` attribute — the browser's own tooltip, about a second
late and in no theme — and the other half carried nothing.

- **It is hand-rolled, not `@radix-ui/react-tooltip`**, which is already a
  dependency and was the obvious choice. Radix positions its content with
  floating-ui, and under jsdom — where every element measures 0×0 — that
  settles into a render loop that never stops. The tooltip *does* open; React
  then never goes idle, so every `findBy*` in the test times out. Checked, not
  assumed. A component this small is not worth a class of test that cannot be
  written.
- **The wrapper is `display: contents`**, so putting one around an input or a
  table cell changes no layout. That is what made it safe to apply across every
  panel in one pass. The bubble is portalled to `document.body` and positioned
  against the trigger's own rectangle, so the header's `overflow-hidden` and a
  panel's scroll container cannot clip it. It measures the **first element
  child**, never the wrapper — the wrapper has no box, so measuring it puts
  every bubble at 0,0.
- **`<tr>` may only contain `<th>` and `<td>`.** A tooltip around a header cell
  must wrap the span *inside* it. React says so out loud in the console.
- **Buttons keep the native `title`.** A disabled button fires no pointer
  events at all, so a hover wrapper never hears about it — and the reason a
  control is unavailable is the one piece of hover text that must not go
  missing. `Button` therefore has both: `title` always, and the themed tooltip
  only through an explicit `tooltip` prop.

## Adjustable panes

`components/shared/SplitPane.tsx`. The Chart tab put a fixed `20rem` beside the
candles for a table with seven columns; the split is now the operator's, drawn
with a real `separator` that arrow keys move.

- The position is remembered per `storageKey` **in `localStorage` only**. It is
  a view preference: it decides nothing about trading, it differs between a
  laptop and a desk monitor, and putting it in the shared `config.yaml` would
  make one machine's window shape the other machine's problem.
- **This repository's jsdom provides no `localStorage` at all** — tests stub
  the global, as `ThemeContext`'s do. Every read and write is guarded anyway.
- The drag uses **mouse** events, not pointer events: the divider only renders
  at `md` and above where there is a mouse, and jsdom's pointer events carry no
  `clientX`, so a pointer-based drag is one that cannot be tested.
- **Each slot is a flex column, and a panel that wants the height asks with
  `flex-1`.** Discovered on 2026-09-22, when the Chart tab lost its candles:
  the canvas rendered 30 pixels tall — its time axis and nothing else — behind
  a correct 200-candle payload. `height: 100%` resolves against a containing
  block whose height is definite, and a `PanelShell` sitting as a plain block
  child of a plain block slot has neither: the section takes its height from
  `min-h-[24rem]`, its body from `flex-1` inside an indefinite chain, and every
  percentage below that collapses to the tallest thing that can size itself.
  Making the slot `flex flex-col` gives the panel a height flex layout has
  already resolved. **`flex-1` on the panel is the other half** — a flex item
  in a column container still takes its height from its content otherwise.
  Pinned in `SplitPane.test.tsx`; jsdom does no layout, so what the test can
  pin is the class contract, not the pixels.
- **The symptom to recognise:** a chart, canvas or map that renders as a thin
  strip with its axis or frame visible. It is almost never the charting library
  and almost never the data. Walk the ancestors in the browser console and find
  the first one whose *specified* height is `auto`.
- **A disposed lightweight-charts SERIES does not throw — it queues a repaint
  that does.** The chart and its time scale throw "Object is disposed" the
  moment you touch them after `remove()`, which is loud and easy to find. A
  series is worse: `removePriceLine` reaches the model, `updateSource` marks
  the chart dirty, and `requestAnimationFrame` schedules a paint. A frame
  later that paint hits the disposed object and throws with **no application
  frame anywhere in the stack** — the component that caused it is already
  gone. Three of them per unmount, in the console on 2026-09-22, from the
  Chart tab's bid/ask price lines: React runs effect cleanups in definition
  order, so `chart.remove()` had already run when the tick effect removed its
  two lines. `CandleChart`'s `disposed` ref existed for exactly this and
  guarded only the time-scale unsubscribe. **Every cleanup that touches a
  chart object needs the guard, not just the ones that throw where you can
  see it.**
- **`src/test/chartStub.ts` records series calls rather than throwing on
  them,** and `touchedAfterRemove` is the assertion: nothing may touch a chart
  after `remove()`. A stub that threw would catch the bug by inventing a
  behaviour the real library does not have, and the next person would go
  looking for the try/except that ought to exist.
- **An answer to a control belongs beside the control.** The Set & Forget
  outcome line was rendered last in a panel two thousand pixels tall, so a
  correctly written message about an evaluation that had just run was never
  seen. Same rule `disabledReason` follows.
- **A table in a draggable pane needs its gutters declared.** Seven columns of
  similar-looking numbers with no cell padding render as
  `BUY0.104320.444315.424324.42` the moment the pane is narrower than the
  table wants. `whitespace-nowrap` on the table, a gutter on every cell but
  the last, and `overflow-x-auto` on the wrapper: scrolling sideways beats
  both wrapping a price across two lines and running four of them together.
- **Vitest 4 stopped augmenting Vite's config type, and an untyped `vi.fn()`
  no longer narrows.** Upgrading 3.2.7 -> 4.1.11 (2026-09-22, for advisory
  GHSA-82fw-gwwq-j7x9) passed all 1014 tests unchanged and broke nothing at
  runtime, but failed `tsc -b` in two places. `vite.config.ts` must import
  `defineConfig` from **`vitest/config`**, not `vite`, or the `test` block is
  `TS2769: 'test' does not exist in type 'UserConfigExport'`. And
  `ReturnType<typeof vi.fn>` is now `Mock<Procedure | Constructable>`, which
  is assignable to nothing specific — a mock standing in for a prop wants
  `Mock<NonNullable<Props["onThing"]>>`. Typing it properly then reveals what
  the old `any` hid: a payload captured from a `Record<string, unknown>` prop
  reads back as `unknown` per key, so the shape has to be named. That is a
  better test, not a worse one, but it is three changes deep from what looks
  like a version bump. **`npm test` passing is not evidence the upgrade is
  done — `npm run build` runs `tsc -b` first, so a type-only break stops you
  rebuilding `dist/`.**
- **One rule, deliberately written twice — and pinned against itself.** The TP
  ladder's reward-per-unit-risk is computed in `services/broker/ea_templates.py`
  (`ladder_rr`) and again in
  `components/trading/internal/ladderRr.ts`. The duplication is the decision,
  not an accident: the readout under the EA template editor's ladders has to
  move on every keystroke, and a round trip per character is a lag, not a
  readout. What makes it safe is that **neither copy is the reference** —
  `tests/fixtures/ladder_rr_cases.json` holds the cases, its expected numbers
  worked out from the rule rather than captured from a run, and both
  `tests/core/test_ladder_rr_shared_cases.py` and
  `components/trading/__tests__/ladderRr.test.ts` run them. A change to one
  side that the other does not follow fails on the side that did not move.
  If a third place ever needs this arithmetic, it joins the case file; it does
  not get its own copy of the rule.
