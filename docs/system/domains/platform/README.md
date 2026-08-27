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
- `secrets.decrypt()` passes non-prefixed values through unchanged — legacy plaintext keeps working and upgrades opportunistically on next read. No keychain (headless/Wine) → 0600 key file fallback.
- The hardware fingerprint deliberately excludes MAC address and boot-volume UUID (macOS) / hostname and NIC MAC (Windows) because those change across OS updates.
- The sync ledger records locally first, then forwards over whichever sync role is active — engines never need to know which.
- STAND_DOWN records which engines it stopped, so RESUME only restarts what sync itself paused.
- Remote users can run normally when the admin server is offline; only updates are unavailable.
- The installer no longer bundles Python: it downloads the 3.11 embeddable runtime at install time, creates a venv under `%LOCALAPPDATA%\FOREX Trader\.venv\`, and adds firewall rules (requires internet).

## Open questions

- `controllers/remote/` (licence-token issuance, admin authority) has limited tests — the largest known gap (see `docs/todo/refactor/stage0/OPEN_QUESTIONS.md`).
- The by-layer split of the websocket transports in `controllers/{remote,sync}` is "still to come".
- The installer's firewall rules use the live app's ports (8888/9000) while this checkout defaults to 8890/9111 — not reconciled.
