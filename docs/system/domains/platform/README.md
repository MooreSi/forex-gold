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
- `backend/src/utils/single_instance.py` — one-app-per-data-directory OS advisory lock, claimed by `run.main()` before the config is read
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
- **This checkout DOES use the `ForexTrader` folder, and that is now correct.** The fork-era isolation (`ForexTrader-Refactor2`) was reverted upstream in 212fd87 and taken in the 2026-08-25 merge, because every launcher and the installer's `[Dirs]` section still said `ForexTrader` while the code wrote to `ForexTrader-Refactor2`, stranding a `config.yaml` on the Windows client. This line said the opposite until 2026-09-19; the code is the fact. `run.py`'s log dir must still match `backend.src.config.USER_DATA_DIR` exactly.
- **`~/Forex-Update` (the original NiceGUI app) and `~/Forex-React` therefore share one `USER_DATA_DIR`** — one `config.yaml`, one `forex_trader_<env>.db`, one `reversal_engine.db`, one bridge port. Deliberate: it is what lets the owner switch between the two apps and keep one history. Only one may run at a time, and the version number is per-checkout. Both rules in [../../rules/80-two-checkouts-one-data-dir.md](../../rules/80-two-checkouts-one-data-dir.md).
- The licence auth server URL is hardcoded and cert-pinned; `guard.enforce()` runs at startup before the server starts; `keygen.py`'s `_SERVER_SECRET` must match the admin tools.
- `node_roles.py`'s two mutual-exclusion checks **fail open** — an unpaired install has no counterpart, and an error must not silently kill trading or bot control. That choice is load-bearing.
- `cluster/sync` and `cluster/remote` are deliberately separate protocols with separate certs so the two channels can never interfere.
- Controller shape rules: flat file, ≤200 lines, one service per operation, no `backend.src.db` import, no repo import, no loops/merges/formatting/fallbacks, no NiceGUI import. Enforced at zero.
- The admin server is started by the composition root in `backend/src/app.py`, never by a page; `remote_controller.py` exposes only the customer-install side.
- Model training is deliberately **not** synced continuously; transfer is a one-shot, user-triggered copy.

## Known things & gotchas

- **`os_utils.shutdown_ui()` is the only place the backend stops the NiceGUI server.** `no-nicegui-in-the-backend` counts source units, not calls, and `restart_app` plus `services/telegram/bot_infra._delayed_app_shutdown` were doing the identical `nicegui.app.shutdown()` in two of them -- one unit over baseline for no behavioural reason. It never raises: callers are mid-restart with the relaunch subprocess already spawned, so an exception there would abort the relaunch and leave nothing running. Headless mode does not call it at all -- there is no server to stop, and the relaunch was spawned separately.


- **Only the EA bridge port is offset from the live app: 9111 against 9000. The UI port is 8888 in both** (`config/__init__.py:174`) — this line claimed 8890 until 2026-09-19 and was wrong. So the two checkouts collide on the dashboard port as well as on the database.
- **`_free_port()` kills whatever is listening before starting, and `single_instance` now runs first so that it cannot.** The kill was written for a wedged instance of the SAME app; with two checkouts on one machine it meant launching the second app terminated the first mid-trade, silently. `run.main()` takes the lock before the config is read, so a live rival is refused rather than shot; by the time `_free_port()` runs, this process holds the lock and anything on the port is an orphan.
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
- **"Update Available" means BEHIND origin, not merely different from it (2026-09-04, reported live).** `check_for_update` decided availability with `local_sha != remote_sha`, which is equally true when the checkout is AHEAD of `origin/main` or has diverged. One unpushed local commit therefore made the header badge flash permanently and offer to check out an OLDER tree, while `git log <local>..<remote>` -- empty in that direction -- left the popup with no commits to list and no digest to summarise, so it said "The commit list for this update could not be read." Availability is now `rc != 0 or bool(commits)`: a range that is genuinely empty is nothing to pull, while a log that could not be READ still assumes an update, because knowing one exists matters more than being able to list it. Pinned by `tests/positions/test_app_update.py::TestOnlyBeingBEHINDOriginIsAnUpdate`, including the invariant that the badge and the popup can never disagree.
- **The remote-admin channel is authenticated, and the boot warning that said otherwise outlived the fix by three days (2026-09-05).** `remote/tls.py` is two paths, not one: `is_ca_verified(host)` is true only for `SERVER_HOST` in a build that bundles an authority, and that path gets `CERT_REQUIRED` + `check_hostname` against `ca_cert.pem`; everything else -- LAN, localhost -- keeps `CERT_NONE` and is checked at the application layer by `peer_is_acceptable()` -> `sync.tls_util.verify_or_pin()`, trust-on-first-use. `remote/client.py:520` runs that check **before** the hello carrying the licence token, machine UUID and hostname, which is the whole point: a pin verified after the token leaves is worth nothing. The residual exposure is TOFU's FIRST LAN connection, and only that one. **The trap this left:** `app.py::_remote_client_enabled` still logged "certificate verification DISABLED and no certificate pinning ... certificate pinning is the tracked fix" on every boot, and `docs/todo/security/010` still read "not started", both false from 2026-09-02 -- while `docs/todo/bugs/014` correctly recorded the fix. Two trackers described the same channel and only one was updated by the change that closed it. The warning's own docstring argues that naming a dead risk trains people to ignore warnings, so it had become the failure it was written to prevent. Pinned by `tests/controllers/test_remote_client_default.py`, whose guards are deliberately **absences** ("verification disabled", "no certificate pinning", "pinning is the tracked fix") because the thing being caught is text that survives the fix it describes.
- **A client reports its `git` version as well as its commit (2026-09-04).** `COMMIT_NO_CHECKOUT` ("no git checkout" in the admin console) is a fact about the absence of `.git` and says nothing about the binary, so a machine that had simply never self-updated and one that *cannot* (no usable `git` at all) arrived looking identical -- and every update is a `git fetch`/`git pull`. `core_app_update.get_git_version()` parses `git --version` ("git version 2.39.5 (Apple Git-154)" -> "2.39.5", "" for a missing binary, a CLT stub that exits non-zero, or output that is not a version line) and it rides on both the HELLO and the `MSG_STATUS` heartbeat as `git_version`. **It is probed once per process and the answer -- including a failure -- is remembered**: on a Mac without the Xcode Command Line Tools every `git` invocation can raise the "install the developer tools" dialog, and the heartbeat runs every ~60s. The trade is that installing git while the app runs is not noticed until restart. On the server the heartbeat's defaults are the HELLO's own answers, so an older client that sends no `git_version` does not blank the one the console already shows. `_remember_build()` does NOT persist it, so an offline client shows no git version. Pinned by `tests/remote/test_git_version_reporting.py`.
- **A KeyGen folder on disk was, on its own, the whole admin-console authorisation (2026-09-06, reported live).** `app.py::_find_admin_open_fn()` looked for `KeyGen/forex_admin.py` beside the app or in `~/Documents`, and its comment justified that with "Remote users don't have that directory" -- untrue the moment `~/Documents` is iCloud-synced or the folder is copied with the app. A remote Mac on the LAN showed the admin button while the console listed it as an ordinary client, so the console's own Grant/Remove Admin could not take it away: it was never a grant. The KeyGen path is now refused whenever `remote/is_remote_client` exists -- a marker `remote/client.py` writes on `MSG_WELCOME`, i.e. on positive proof that ANOTHER machine's admin server accepts this one. The grant path (`is_remote_admin` -> `KeyGen/admin_panel.py`) is untouched and is the only route on a client. **The marker is deliberately not written on a machine that has its own `remote/admin_password.hash`**: when the admin Mac loses its licence the activation screen makes it dial its OWN server (`config/licence/guard.py`), and that welcome must not hide the console needed to re-issue the licence. It is read via `_REMOTE_DIR`, not `auth._HASH_FILE`, so a test pointing `_REMOTE_DIR` at a tmp dir cannot be answered by the developer's real hash file. Recovery for a machine marked wrongly is deleting that one file. `LOCAL_ADMIN_AVAILABLE` (KeyGen path only) now gates `_should_start_remote_server()`, so a granted remote admin gets a console without ever becoming a server. Pinned by `tests/licence/test_admin_console_default_off.py` and `tests/remote/test_client_connect_loop.py::TestTheRemoteClientMarker`.
- **A fresh install could see the update feature but never reach it (2026-09-06, reported live).** `check_for_update()` returned a bare `"not a git checkout"` for a missing `.git`, and the Update card only ever drew its button when an update was *available* -- which that state can never be. So the machine that most needs the bootstrap `apply_update()` has done since 2026-09-03 was the one machine that could not trigger it; the only way out was an admin-console push. The check now distinguishes the two causes with `shutil.which("git")`: no binary returns `bootstrap: False` and says to install git (macOS `xcode-select --install`, Windows re-run the .bat), while a missing checkout on a machine that HAS git returns `bootstrap: True` and the card offers **Set Up Updates**, which runs the same `apply_update()`. The button choice lives in the free function `update_panel._update_action()` precisely so it can be tested without rendering NiceGUI. Both launchers now test `git --version` rather than `command -v git` / `where git`, because a macOS Command Line Tools stub exists and is executable while failing every invocation, and `FOREX Start.command` runs `xcode-select --install` itself when Homebrew is absent instead of printing an instruction into a Terminal window nobody reads.
- **`core_app_update.py`'s `_REPO_ROOT` was a fixed `.parent.parent.parent` count, silently wrong after the module moved to `services/positions/`** (2026-09-03) — the sixth instance of the class of bug `os_utils.repo_root()`'s docstring already describes for four other modules. It made every check on Settings > Update and the header's update badge fail with "not a git checkout" even though the checkout and `origin` remote were fine. Fixed by importing `os_utils.repo_root()` instead of re-deriving it; `tests/positions/test_app_update.py::test_repo_root_resolves_to_the_actual_checkout_root` pins it directly, since every other test in that file monkeypatches `_REPO_ROOT` and would never catch this. A fresh install (no prior `.git`) is a separate, verified-working path: `apply_update()` bootstraps with `git init` + `remote add origin` + `checkout -B main --track origin/main -f`, and force-checkout overwrites untracked files that pre-exist from the installer's plain file copy without erroring, confirmed by direct testing against a real git repo.
- **A copied install links itself to GitHub at the commit it ALREADY is, and moves no files doing it (2026-09-12).** The installers copy, they never clone, so a fresh download has no `.git`; the 2026-09-06 answer was a manual **Set Up Updates** button that ran `apply_update()`, which force-checkouts origin's HEAD over the working tree -- pressing it on a machine three commits old is a silent code update on a machine that may be trading. `core_app_update.link_checkout()` instead does `init` + `remote add` + `fetch`, stages with `add -A` (so `.gitignore` keeps `.venv`, the config and the databases out), and walks the newest `_LINK_SEARCH_DEPTH` (50) commits of `origin/main` comparing `ls-tree -r` against `ls-files -s` **with the file mode dropped on both sides** -- unzipping a GitHub archive can land the `.command` launchers 100644 where the tree has 100755, and comparing modes makes a byte-identical download match nothing. On a hit it sets the branch with `update-ref` + `symbolic-ref` + `reset --mixed` (never `checkout`), so HEAD is truthful and the tree is untouched. On a miss it **deletes the `.git` it just made**: a repository with an unborn HEAD is worse than none, because `check_for_update()` then stops offering the bootstrap and fails on `rev-parse HEAD` instead. Called fire-and-forget from `app.startup()`. Pinned by `tests/positions/test_checkout_linking.py`, which includes two real-git passes because the whole claim is that `ls-files -s` and `ls-tree -r` agree.

- **The admin console's Up to date / Outdated badge was comparing clients against THIS Mac, not GitHub (2026-09-12, reported live).** A MacBook running the exact tip of `origin/main` was badged Outdated while its own header correctly showed no update. Two faults in `KeyGen/forex_admin.py::_build_updates_card`, neither in this repo: `remote_full = result.get("remote_sha") or local_full` substituted this Mac's own commit whenever `check_for_update()` returned an error dict (which carries neither sha), and `_last_github_sha` was set by `ui.timer(0.1, _refresh, once=True)` -- once per panel open -- while the client cards below it re-render every 15s and badge themselves against it, so a console left open across a push kept contradicting them. Fixed locally to `or ""` (an unknown head suppresses the badge rather than inventing one) plus a 120s refresh. **KeyGen is outside this repo and cannot be committed or pushed**, so that fix lives only on the owner's Mac -- see `forex-keygen-outside-the-repo`.

- **The admin-server/client decision is made from two filesystem facts, and a synced folder can flip a client into a server (2026-09-12, reported live).** `app.py::startup()` is `if _should_start_remote_server(): … elif _remote_client_enabled(config): …` -- server wins, so a machine that promotes itself never starts the client and can never appear in the console. `_should_start_remote_server()` is `LOCAL_ADMIN_AVAILABLE and password_is_set()`: the presence of `~/Documents/KeyGen/forex_admin.py` and a non-empty `USER_DATA_DIR/remote/admin_password.hash`, nothing more. `~/Documents` is inside the iCloud Drive container on the owner's Apple ID (Desktop & Documents syncing), so KeyGen lands on every Mac he signs into. A client MacBook was found listening on 8443 with its own `remote/tls.py`-shaped certificate (`CN=217.155.25.160`, SAN IP-then-localhost, generated 2026-09-09) while the console showed it offline; its dashboard port 8888 was closed only because `run.py::_resolve_bind_host` binds loopback. **The 2026-09-06 `is_remote_client` guard does not close this**: that marker is written on `MSG_WELCOME`, by the very code path the promotion stops from running, so a machine promoted before its first welcome can never write it. **Fixed the same day** by pinning the issuer to hardware: `config/licence/issuer.py::is_licence_issuer_machine()` compares `get_fingerprint()` against `ADMIN_MACHINE_FINGERPRINT` (the owner's Mac mini, `FOREX-349E9267-…`), overridable with `FOREX_ADMIN_MACHINE_FINGERPRINT` so a replaced admin Mac is recoverable. It leads BOTH definitions of "admin machine" -- `app.py::_find_admin_open_fn()` (and so `LOCAL_ADMIN_AVAILABLE`, and so the server) and `config/licence/guard.py::_this_is_the_admin_machine()` (the activation screen) -- because `tests/licence/test_admin_machine_can_relicense_itself.py::test_it_agrees_with_the_two_sources_it_replaces` exists to stop those two drifting, and it caught the drift the moment only one was changed. It lives under `config/licence/` next to `fingerprint.py`, not under `services/`, because `config/` is the bottom of the import stack and `guard.py` may not reach up. **Every fixture that builds "the admin machine" must now declare the fingerprint too**, or the test passes only on the owner's Mac -- four did, in three files. Pinned by `tests/licence/test_only_the_issuer_machine_is_the_admin.py`. Background: `docs/simon-handover/035-a-client-mac-promoted-itself-to-admin-server.md`.


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

## The log file is shared by both checkouts (2026-09-20)

`~/Forex-Update` and `~/Forex-React` share one `USER_DATA_DIR` (rules/80), and
that includes `forex_trader.log`. Both apps append to the same file, so a
5-day export off this machine carries 1,494 lines naming `Forex-Update`
paths and 2,297 naming `Forex-React`, including NiceGUI's own stack traces.

Not filtered, deliberately: they are the same user, the same machine and the
same database, and a support bundle that hid half the machine's behaviour
would be worse. But it is why a line in an export can name a file that does
not exist in this checkout, and why "this app logged X" cannot be concluded
from the log alone -- check the module path.

It also cost a wrong diagnosis on the day it was found: ~14,000 lines
reporting two trades as "open in the database, and the broker has no record
of it" looked like a live reconciliation failure in this app. It was not.
The reconciliation throttle was working correctly -- ten WARNINGs in five
days -- and the rest were the BODY of its throttled DEBUG reports, kept by a
log filter that judged lines individually instead of records. See
`services/diagnostics/log_bundle.py`.


## The auto-restart watchdog is shared by both checkouts too (2026-09-21)

Same root cause as the shared log file, with teeth: there is **one** LaunchAgent
(`com.forextrader.watchdog`, in `~/Library/LaunchAgents/`) and one Windows task
name for both checkouts, because the label is fixed and the home directory is
shared. `core_autostart._launchd_plist()` bakes `repo_root()` into it at
`enable()` time, and `tools/watchdog.py` resolves its own `ROOT` from its own
file -- so the entry runs **whichever checkout enabled auto-restart last**,
at login and every 120s, with `--no-browser` so nothing visible happens.

Reported live 2026-09-21. A remote Mac was told to run the React checkout and
came up with the NiceGUI dashboard. The sequence:

1. `FOREX Start.command` kills whatever holds `:8888`, then does the first-run
   venv build -- minutes on a cold machine.
2. Inside that window the stale agent ticks, sees `:8888` down, and launches
   `~/Forex-Update/run.py --no-browser`.
3. The old app has a built venv, so it wins: it takes the single-instance lock
   and binds the port.
4. The new `run.py` reaches `_claim_single_instance()` and refuses -- correctly,
   loudly, into a Terminal window that then closes.
5. `localhost:8888` serves the old app, from a folder nobody launched.

`sync_from_setting()` could not repair this because it asked only whether an
entry EXISTS (`is_installed()`), which was true throughout -- so starting the
right app **re-armed the wrong one**. It now also asks
`points_at_this_checkout()`, which reads the installed entry back (the plist's
`ProgramArguments[-1]`; `schtasks /fo LIST /v`'s `Task To Run:`) and compares
the resolved path against `watchdog_script()`. A mismatch re-`enable()`s,
repointing the entry here and logging what it found. **An unreadable entry
counts as pointing here**, or a machine whose plist cannot be parsed would
reinstall the agent on every boot.

The other half is the race itself, closed in the launchers. **`FOREX Start.command`
and `Setup & Start FOREX.bat` now delete `data/watchdog.armed` before they do
anything slow**, mirroring what the stop scripts have always done. The app
re-arms itself through `sync_from_setting()` once it is genuinely up -- which
is also when it repoints a stale entry -- so the window that closes is exactly
the window in which the app is not up to re-arm. The cost, accepted: a launch
that fails for an unrelated reason leaves the machine unsupervised until
someone starts it by hand. Pinned by
`tests/core/test_launchers_disarm_the_watchdog.py`, which asserts the ORDERING
inside the four scripts, not merely that the string is present.

Two things this does NOT fix, both deliberate:

* Neither half runs until this checkout boots. On a machine already in the
  loop above, it never does, so the entry must be cleared by hand once:
  `launchctl bootout gui/$(id -u)/com.forextrader.watchdog`, delete the plist
  and `data/watchdog.armed`, then start the app you want.
* The armed flag and `watchdog.last_launch` live in the shared data dir, so
  either checkout's stop script still disarms the other's watchdog, and either
  one's Start script now does too. That is the existing intent-vs-entry design
  (see the module docstring), not a bug.

Pinned by `tests/core/test_autostart.py::TestTheEntryPointsAtThisCheckout`.
The module and the test are identical in both checkouts and must change
together (rules/80).


## Open questions

- `controllers/remote/` (licence-token issuance, admin authority) has limited tests — the largest known gap (see `docs/todo/refactor/stage0/OPEN_QUESTIONS.md`).
- The by-layer split of the websocket transports in `controllers/{remote,sync}` is "still to come".
- The installer's firewall rules use the live app's ports (8888/9000) while this checkout defaults the EA bridge to 9111 — not reconciled. (The UI port matches at 8888; the 8890 previously recorded here was never the default.)


## Updating this install (2026-09-20)

Three things about the update path, all found by using it:

* **`POST /api/node/update` is not a route.** Applying an update is
  `POST /api/node/update/apply`. The Settings button posted to the former and
  told the operator nothing, so the update it claimed to start never ran.
* **`GET /api/node/update` runs `git fetch`** and, when an update exists, a
  paid model call for the plain-English summary. Nothing polls it. The
  header's badge reads `system_ctl.cached_update_check()` instead — the last
  answer, refreshed behind the caller at most every ten minutes — and only the
  popup the operator opens asks for the summary (`include_summary`, default
  true; the Settings card passes false and lists the commit subjects, which it
  already has for free).
* **A check must look like it is happening.** It takes as long as a fetch
  takes, and a button with no visible state is indistinguishable from a dead
  one. That is precisely how this was reported.

The version string does not answer "am I on the current build?" — this app
self-updates by commit, and the string only moves when someone bumps it. Show
the installed commit against origin's.


## A standalone install could get stuck "stood down" (2026-09-20)

`get_active_trader()` **defaults to `remote_vps`**, on the sound reasoning
that a paired Mac must not assume it is in charge. On an install with no peer
at all, that default was a trap in two directions at once:

* it was **inert** for trading, because `open_trade`'s gate reads
  `if _host and get_active_trader() == TRADER_REMOTE_VPS` and there is no
  host, so the node traded normally while its own header said REMOTE; and
* it was **unreachable**, because the only control that clears it is Take
  over locally, which required a connected peer that does not exist.

`take_over_locally` now takes the local-only path when `_paired_host()` is
empty, which is the same question the order path asks. A **configured** peer
still has to acknowledge whether it is reachable or not: an unreachable peer
may still be trading the same account, and that guarantee is not weakened.
`_paired_host` treats an unreadable config as PAIRED for the same reason.

The default itself is unchanged. Changing it would mean revisiting
`test_the_active_trader_DEFAULTS_TO_THE_VPS`, which is a deliberate safety
pin, so an unpaired install still reads REMOTE until someone takes over once.


## An unreachable backend is not a signed-out operator (2026-09-21)

`AuthContext.refresh` caught every failure the same way and fell back to "no
session". A `fetch` that cannot connect throws a TypeError; the client throws
`ApiError` only when the server actually ANSWERED. Conflating them meant that
every restart put the login form in front of an install with auto-login on.

The two are now told apart, and the difference does double duty: while the
backend is unreachable the shell says it is reconnecting and keeps retrying,
and when it answers again the page reloads itself -- so restarting the app no
longer needs a manual refresh.

## The active-trader default, again: BOTH roles count (2026-09-21)

`get_active_trader()` now defaults to local on a standalone install. The first
attempt tested only `sync_remote_host`, which is the CLIENT half of a pairing.
A VPS never sets it -- it is the server, and its half is `sync_server_enabled`
-- so every VPS looked standalone, defaulted to local, and
`SyncServer.is_standing_down()` would have answered True and **stopped the VPS
trading altogether**. Caught by
`tests/core/test_sync_server_auth.py::TestStandDown::test_the_TRADE_GATE_FOLLOWS`,
which exists for exactly that. A standalone install is one that is in neither
role.
