# Platform

**Living file — update when this domain teaches you something.**
Covers: `backend/src/config/` (settings, secrets, licence),
`services/cluster/`, `backend/src/controllers/`, `run.py`, `installer/`.

## What it is

Everything that gets the app running and keeps two machines coordinated:
config loading and user-data paths, at-rest secret encryption, offline HMAC
licensing plus a hub-and-spoke admin/update server, the 1:1 Mac↔VPS sync
cluster, the `run.py` launcher (logging, port freeing, MT5 bridge
subprocess, config migration), and the Inno Setup Windows installer. The
controller layer sits here too, as the single narrow API the frontend is
allowed to call.

## Where the code lives

- `run.py` — launcher: rotating file logging into `USER_DATA_DIR/data`, `_free_port`, `_start_mt5_bridge`, `_migrate_config_yaml`, server startup
- `backend/src/config/__init__.py` — YAML config + env overrides, `USER_DATA_DIR`/`DATA_DIR`/`SESSIONS_DIR`/`DB_PATH`, port defaults, Wine paths, Claude model alias resolution; all access via `config.get()`
- `backend/src/config/secrets.py` — Fernet at-rest encryption (`enc:v1:` prefix), key in the OS keychain
- `backend/src/config/licence/` — `guard.py` (offline HMAC enforcement at startup), `keygen.py`, `fingerprint.py`, `client.py` (cert-pinned HTTP to the auth server), `store.py`
- `services/cluster/node.py`, `node_roles.py` — node identity, sync token, which paired node owns which job
- `services/cluster/sync/` — Mac client / VPS server for the 1:1 link, settings mirroring, STAND_DOWN/RESUME, the consolidated closed-trade ledger, remote stats facades, one-shot ML model transfer
- `services/cluster/remote/` — hub-and-spoke admin/licence/update channel (wss to the admin server)
- `backend/src/services/positions/core_app_update.py` — the other update mechanism: client-initiated, `git fetch`/`checkout` straight from `https://github.com/MooreSi/forex`, no admin server involved. Lives under `services/positions/` despite having nothing to do with trade positions.
- `backend/src/controllers/__init__.py` — the controller contract; one flat `<page>_controller.py` per page
- `installer/FOREX_Trader_Setup.iss` + `BUILD_INSTALLER.md` — Inno Setup 6 Windows installer

## Constraints / must not change

- All user data (config, DBs, sessions, logs) lives **outside** the project tree; every downstream path derives from `USER_DATA_DIR`.
- This checkout must never default to the live app's `ForexTrader` folder — the default is `ForexTrader-Refactor2`. `run.py`'s log dir must match `backend.src.config.USER_DATA_DIR` exactly.
- The licence auth server URL is hardcoded and cert-pinned; `guard.enforce()` runs at startup before the server starts; `keygen.py`'s `_SERVER_SECRET` must match the admin tools.
- `node_roles.py`'s two mutual-exclusion checks **fail open** — an unpaired install has no counterpart, and an error must not silently kill trading or bot control. That choice is load-bearing.
- `cluster/sync` and `cluster/remote` are deliberately separate protocols with separate certs so the two channels can never interfere.
- Controller shape rules: flat file, ≤200 lines, one service per operation, no `backend.src.db` import, no repo import, no loops/merges/formatting/fallbacks, no NiceGUI import. Enforced at zero.
- The admin server is started by the composition root in `backend/src/app.py`, never by a page; `remote_controller.py` exposes only the customer-install side.
- Model training is deliberately **not** synced continuously; transfer is a one-shot, user-triggered copy.

## Known things & gotchas

- **`os_utils.shutdown_ui()` is the only place the backend stops the NiceGUI server.** `no-nicegui-in-the-backend` counts source units, not calls, and `restart_app` plus `services/telegram/bot_infra._delayed_app_shutdown` were doing the identical `nicegui.app.shutdown()` in two of them -- one unit over baseline for no behavioural reason. It never raises: callers are mid-restart with the relaunch subprocess already spawned, so an exception there would abort the relaunch and leave nothing running. Headless mode does not call it at all -- there is no server to stop, and the relaunch was spawned separately.


- Default ports here are offset from the live app: UI 8890 (live 8888), EA bridge 9111 (live 9000). `_free_port()` kills whatever is listening before starting.
- On native Windows the app imports `MetaTrader5` in-process and skips the bridge subprocess; on macOS the bridge runs under Wine Python.
- `run.py` must set `BRIDGE_CREDS_PATH` for the bridge subprocess — without it every cold boot connects with no credentials, masked as a normal startup delay because the watchdog's restart path sets it correctly.
- `_migrate_config_yaml()` rewrites stale Claude model IDs before any module reads config, so old files on remote machines can't crash the app.
- **`config.save_to_yaml()` can persist a key that `config.load()` then throws away.** `load()` rebuilds the in-memory `_cfg` from one literal dict of named keys, and `save_to_yaml()` calls `reload()` at the end — so a setting whose key is not named in `load()` is written to `config.yaml` correctly and is gone from `config.get()` immediately, on the same call that saved it. The file on disk is right; every reader sees the default. Found 2026-09-04 when Settings > Security's "Log in automatically" never stuck. Two sets of keys were affected and both are now declared in `load()`: `auto_login_enabled`, and the four `news_blackout_*` keys Settings > News writes, which `news_calendar.get_blackout_settings()` had been reading back as its own defaults — so the blackout ran permanently ON (its default is True) while the owner's saved choice of OFF sat in the file doing nothing. **Any new `save_config()` key must be added to `load()` in the same change.**

- **Do not resolve a config default through `_e()` when the value can legitimately be falsy.** `_e()` is `os.environ.get(K) or base.get(k) or default`, so a saved `False`, `0` or `""` is skipped and the default wins — the same silent-revert symptom as an undeclared key, with the declaration present and looking correct. The `news_blackout_*` block reads env-then-yaml-then-default by hand for exactly this reason (`enabled` defaults True, `minutes_before` accepts a real 0). Its defaults duplicate `news_calendar._DEF_*` because `backend.src.config` imports nothing; clamping stays in `news_calendar`.

- **The owner's blackout setting was restored to `true` in `config.yaml` before the fix landed** (2026-09-04, their call), so switching the keys on changed no live behaviour that day. Had it been fixed with the file left at `false`, the fix alone would have opened automated entries inside news windows that had been blocked for months.
- `secrets.decrypt()` passes non-prefixed values through unchanged — legacy plaintext keeps working and upgrades opportunistically on next read. No keychain (headless/Wine) → 0600 key file fallback.
- The hardware fingerprint deliberately excludes MAC address and boot-volume UUID (macOS) / hostname and NIC MAC (Windows) because those change across OS updates.
- The sync ledger records locally first, then forwards over whichever sync role is active — engines never need to know which.
- STAND_DOWN records which engines it stopped, so RESUME only restarts what sync itself paused.
- Remote users can run normally when the admin server is offline; only updates are unavailable.
- The installer no longer bundles Python: it downloads the 3.11 embeddable runtime at install time, creates a venv under `%LOCALAPPDATA%\FOREX Trader\.venv\`, and adds firewall rules (requires internet).
- **`core_app_update.py`'s `_REPO_ROOT` was a fixed `.parent.parent.parent` count, silently wrong after the module moved to `services/positions/`** (2026-09-03) — the sixth instance of the class of bug `os_utils.repo_root()`'s docstring already describes for four other modules. It made every check on Settings > Update and the header's update badge fail with "not a git checkout" even though the checkout and `origin` remote were fine. Fixed by importing `os_utils.repo_root()` instead of re-deriving it; `tests/core/test_app_update.py::test_repo_root_resolves_to_the_actual_checkout_root` pins it directly, since every other test in that file monkeypatches `_REPO_ROOT` and would never catch this. A fresh install (no prior `.git`) is a separate, verified-working path: `apply_update()` bootstraps with `git init` + `remote add origin` + `checkout -B main --track origin/main -f`, and force-checkout overwrites untracked files that pre-exist from the installer's plain file copy without erroring, confirmed by direct testing against a real git repo.

- **`node_roles` is one exclusion expressed twice, and exactly one side must
  answer True.** `is_bot_command_authority()` decides who long-polls the
  Telegram bot token; two True answers is the 409-Conflict cycle where each
  side's `deleteWebhook` kicks the other. The VPS branch keys off
  `get_app_config("sync_server_enabled") == "1"` -- **the string**, since
  app_config stores text and an int `1` falls through to the client branch,
  finds no host and answers True unconditionally, which is the loop again.
  `tests/core/test_node_roles.py` asserts the pair-wide property directly for
  both switch positions rather than only the four branches.
- **`is_active_trader_node()` does NOT fail open, despite its docstring
  saying it does.** Both try blocks catch `ImportError` only, so a database
  error out of `get_active_trader()` propagates. Its caller wraps it, so the
  observed effect is the paid AI fallback being skipped -- fail-CLOSED, the
  opposite of what is written. Pinned by a test named for the mismatch. The
  sibling `is_bot_command_authority()` catches broad `Exception` and does fail
  open as described; the asymmetry looks unintended but changing an error path
  on a live gate was left as its own decision.

- **`_do_restart()` re-execs in place on macOS/Linux; only Windows spawns a
  relaunch child.** Every automatic restart -- licence activation, admin
  revoke, admin-pushed git update -- goes through it, and if it fails the app
  is simply gone: the process has already exited and the browser sits on
  "Licence Activated / Loading..." forever. The POSIX side used to spawn a
  detached `bash -c "sleep 3 && python run.py"` and hard-exit one second
  later, so the only route back was a grandchild that had to outlive its
  parent's session teardown, with its only diagnostics going to
  `restart.log`. On a fresh macOS install (2026-09-04) that lost -- approved,
  licence verified and stored, log ends on "Licence received — signalling UI
  then restarting", app never returned, user relaunched by hand. `os.execv`
  keeps the same PID, session, parent and Terminal window, and the port-8888
  socket is released by exec (Python sets close-on-exec on sockets), so
  nothing has to be waited out. `guard.py`'s "Activate Manually" button had
  always used execv here; the automatic path had not. Windows keeps
  spawn-then-`os._exit(_RESTART_EXIT_CODE)` because the bat launcher's loop
  reads that exit code and `os.execv` on Windows is a spawn-and-exit
  emulation that would hand it the wrong one. Pinned by
  `tests/remote/test_do_restart.py`.
- **The `/licence-activated` wait page polls a probe path, not `/`, and
  leaves via `location.replace('/')`, not `location.reload()`.** It is plain
  HTML with no socket.io so it survives the process dying underneath it, and
  its only job is to notice the replacement process and go there. It had two
  ways of failing that, both ending in the user relaunching by hand.
  (1) It polled `/` and navigated on the first 200 — but the process serving
  this page also serves `/` and answers 200 until it exits, and the guard
  navigates here, sleeps 0.6 s, then restarts, while the first poll fires
  800 ms after page load. ~200 ms of margin was the entire safety mechanism.
  `_ACTIVATION_PROBE_PATH` (`/licence-activated/probe`) is registered by
  `_show_registration_page` and nowhere else, so 200 means "still the
  activation screen" and 404 means "the app is up" — a distinction by
  construction rather than by timing. There is no catch-all route in the
  app, so the 404 is real. (2) It called `location.reload()`, but this page's
  URL is `/licence-activated`, which only the activation screen registers —
  so even when the timing worked, the reload re-requested a route the new app
  does not serve and landed on a 404. If the restarted process lands on the
  activation screen again the probe keeps answering 200 and the page keeps
  waiting, surfacing its manual link after ~10 s; that is deliberate, and
  better than silently reloading into the registration form. Pinned by
  `tests/licence/test_activation_wait_page.py`, which asserts over the script
  block with its `//` comments stripped — the comments there name the calls
  they warn against, so asserting over the raw document would only assert
  that the page agrees with its own prose.
- **The activation screen's "no licence yet" path is not an error.**
  `_show_error_and_exit("")` used to log `ERROR ... Licence check failed:`
  with an empty reason on every first install, one line after `enforce()`
  had already explained the same thing at INFO. The activation flow is the
  one part of this app a user watches in a terminal, so that line is what
  they point at when something else goes wrong. It now logs only when there
  is a reason, and the `nicegui` import moved below the `allow_register`
  branch (that branch never used it).

## Open questions

- `controllers/remote/` (licence-token issuance, admin authority) has limited tests — the largest known gap (see `docs/todo/refactor/stage0/OPEN_QUESTIONS.md`).
- The by-layer split of the websocket transports in `controllers/{remote,sync}` is "still to come".
- The installer's firewall rules use the live app's ports (8888/9000) while this checkout defaults to 8890/9111 — not reconciled.
