# Two checkouts, one data directory

Two checkouts of this app live on the owner's machine:

| | |
|---|---|
| `~/Forex-Update` | the original app, NiceGUI dashboard |
| `~/Forex-React` | this one, React dashboard (replaced NiceGUI 2026-09-18) |

`backend/src/config/__init__.py` is identical in both, so both resolve to the
**same** `USER_DATA_DIR`:

```
macOS    ~/Library/Application Support/ForexTrader/
Windows  %APPDATA%\ForexTrader\
Linux    ~/.config/ForexTrader/
```

That means one `config.yaml`, one `forex_trader_<env>.db`, one
`reversal_engine.db`, one `breakout_signal.db`, one backups folder, one set of
MT5 credentials and one bridge port — shared by both apps.

**This sharing is deliberate and must not be broken.** It is what lets the
owner switch between the two apps and keep the same trade history, settings,
learned parser rules and EA templates. A change that gave this checkout its
own data directory would silently strand every one of those.

`FOREX_TRADER_DATA_DIR` (or `FOREX_TRADER_DATA_DIR_NAME`) is the escape hatch
for deliberately separating an install. It is the only supported way.

---

## Rule 1 — only one of them may run at a time

They share the database, the config file, the MT5 bridge port and the broker
connection. Two running at once is two processes trading one account.

Enforced by `backend/src/utils/single_instance.py`, claimed in `run.main()`
**before** the config is read, before the database is opened and before
`_free_port()` — which is a kill, not a check. Until 2026-09-19 that kill was
the only thing standing in for a lock, so launching the second app terminated
the first mid-trade and said nothing about it.

The lock is an **OS advisory lock** on a byte of `USER_DATA_DIR/forex_trader.lock`,
not a pid file. The kernel drops it when the holder dies, so a crash or a
`kill -9` cannot leave an install that will not start, and there is no
staleness rule to get wrong. The pid and checkout path in the file are for the
error message only.

Three things about it are load-bearing:

- **It waits `_SINGLE_INSTANCE_WAIT` (15s) before refusing.** A restart is two
  instances overlapping on purpose: the Windows path spawns the replacement and
  only then exits. Refusing instantly makes the replacement lose the race with
  its own parent and leaves nothing running — the 2026-08-07 failure
  `_claim_port` exists for, one layer up.
- **It fails open.** A lock that cannot be taken at all (read-only or missing
  data directory) logs a warning and starts anyway. Refusing to boot is a worse
  failure than the overlap, and it is what every build before the lock did.
- **The lock file lives in the data directory, never in the checkout.** A lock
  beside the code would be per-checkout, so each app would take its own and
  both would still run.

Pinned by `tests/utils/test_single_instance.py` (real subprocesses — the claim
is about processes, so nothing else proves it) and
`tests/test_single_instance_boot.py` (the ordering in `main()`).

**Both checkouts carry this lock, and they must stay in sync.** A lock in only
one of them is half a guard: that app declines to start while the other runs,
but the other still kills it. `backend/src/utils/single_instance.py`, the
`_claim_single_instance()` helper in `run.py`, its call site in `main()` and
the two test files are byte-identical in `~/Forex-Update` and `~/Forex-React`
as of 2026-09-19. **A change to any of them belongs in both checkouts, in the
same sitting** — they contend for one lock file, so a divergence in the
offset, the filename or the data directory silently turns the guard off.

---

## Rule 2 — the version number is per-checkout, and stays that way

The version number is the deliberate **exception** to the sharing above.
Bumping one app's version must not change what the other reports.

It is derived in the checkout and stored in the checkout:

- `backend/src/utils/version_history.py` — `RELEASES[0][0]` is the single
  source of truth; `_derive_version()` syncs it to the **repo root** `VERSION`
  file for callers that cannot import the package
- `os_utils.repo_root()` finds that root by walking up for `run.py`, never by
  counting parents

**Never** put version state in `USER_DATA_DIR`, `config.yaml` or any database.
Everything that answers "which code is this?" reads the derived value:

- Settings → Update's *Installed Version*
- the header's update badge, via `check_for_update`
- the admin console's per-client version column
- the remote client's HELLO and its ~60s status heartbeat

Sourced from shared state, all four would describe whichever app was launched
last, and the admin console would show one machine flipping between two
versions while nothing on it changed.

Pinned by `tests/utils/test_version_is_per_checkout.py`. Its central test
plants a decoy `VERSION` in a stand-in data directory and asserts nothing
reads or rewrites it — an *empty* directory proves nothing there, because
`_derive_version()` swallows the read error on a file that is not present and
then skips the write, so a version wired to the shared directory would pass an
emptiness check without doing anything.

## Rule 3 — the update remote is per-checkout too (2026-09-21)

The second deliberate difference, for the same reason as the version: it
describes *the code*, not *the account*.

`core_app_update._GITHUB_REPO_URL` is the remote a machine with **no `.git`**
is wired to -- and the installers copy rather than clone, so every fresh
download is such a machine. It must name the repo its own checkout pulls from:

| Checkout | `origin` | `_GITHUB_REPO_URL` |
|---|---|---|
| `~/Forex-React` | `MooreSi/forex-react` | `https://github.com/MooreSi/forex-react` |
| `~/Forex-Update` | `MooreSi/forex` | `https://github.com/MooreSi/forex` |

**The two lines differ on purpose. Do not "fix" one to match the other.**

Until 2026-09-21 the React checkout carried the NiceGUI repo's URL. That is
not a cosmetic mismatch: `apply_update()` on a folder with no `.git` does
`remote add origin <this>` then `checkout -B main --track origin/main -f`, so
pressing **Set Up Updates** on a downloaded React folder would have
force-checked-out the *old app over it*. `link_checkout()` hit the same
constant at every startup, matched nothing, and deleted the `.git` it had just
made -- leaving the machine permanently unable to self-update, silently.

Owner's decision the same day: **forex-react is canonical for the React
checkout.** Making the NiceGUI app update itself into the React app is a
separate change, with its own demo session, not a line edit here.

Pinned by
`tests/positions/test_app_update.py::TestTheBootstrapPointsAtTHISRepo`, which
compares the constant against `git remote get-url origin` rather than only
against a literal -- the two disagreeing is what the bug WAS. Note that
`"forex-react"` contains `"forex"`, so a substring check passes on the old
value and proves nothing; the test asserts the suffix.

---

## When you add shared state

Ask which of the two rules it falls under:

- **Shared** (the default): trade history, settings, credentials, learned
  rules, templates, ML models, backups. Put it in `USER_DATA_DIR`.
- **Per-checkout**: anything that describes *the code* rather than *the
  account* — the version, the git commit, the update remote, the checkout
  path. Put it in the
  repo, and derive it from `os_utils.repo_root()`.
