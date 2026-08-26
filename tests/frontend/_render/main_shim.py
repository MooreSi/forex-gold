"""A NiceGUI 'main file' that registers the real page and nothing else.

nicegui.testing.user_simulation resets NiceGUI's globals on entry, so a route
registered by importing frontend.app beforehand is wiped. The supported way in
is main_file=, which runpy-executes a file INSIDE the reset context -- this one.

It also neutralises the lifespan hooks before frontend.app binds them. The real
backend.src.app.startup calls db_module.init(config["db_path"]) against the
user's live database and rewrites bridge_credentials.json, and this app trades
a real account. It must never run from a test.
"""
import os
import sys

# A stale binding from an earlier test in the same session would re-import as a
# no-op and keep the REAL startup, so drop it and bind again against the fakes.
for _name in [m for m in sys.modules if m == "frontend.app" or m.startswith("frontend.app.")]:
    del sys.modules[_name]

import backend.src.app as _backend_app


async def _noop() -> None:
    return None


_backend_app.startup = _noop
_backend_app.shutdown = _noop

# An isolated schema, built fresh. Without this the page renders against the
# developer's live trading database, because startup() -- the only caller of
# db.init() -- has just been stubbed out.
from backend.src.db import database as _db  # noqa: E402

_db_path = os.environ["FOREX_RENDER_TEST_DB"]
_db.init(_db_path)

# The signal services each namespace their own connection (see
# backend/src/db/connection.py) and each has its own init_db. Rendering the
# tabs reads all three, so all three need pointing at the temp file too.
from backend.src.services.breakout_signal import breakout_signal_repo as _bo  # noqa: E402
from backend.src.services.reversal_engine import reversal_engine_repo as _rev  # noqa: E402
from backend.src.services.test_signal import test_signal_repo as _ts  # noqa: E402

for _repo in (_bo, _rev, _ts):
    _repo.init(_db_path)   # init(), not init_db(): this one also builds the schema

# The tab panels ask for the engine while rendering. A MagicMock is enough to
# build the widgets and, more to the point, cannot reach a broker: the real
# TradingRuntime opens an MT5 bridge connection.
class _FakeEngine:
    """Enough engine for the page to build AND refresh, and no broker.

    The awaited methods are async because _refresh_header genuinely awaits
    them on its ui.timer -- with sync stubs it logged "header refresh failed:
    object NoneType can't be used in 'await' expression" and the refresh path
    went untested while every render assertion still passed. The values are
    empty rather than invented: this file proves the header is built and
    refreshed, not what a broker would say.
    """

    _profit_sound_seq = 0

    @property
    def _bridge(self):
        """The header refreshes through engine._bridge.

        Answered by this same object rather than a second fake class: the
        fixture-dedup ratchet counts ad-hoc _FakeBridge classes and is
        already over its baseline, and one more here would have pushed it
        further for a stub with a single method on it.
        """
        return self

    async def get_deal_history(self, *a, **k): return []

    async def get_tick(self, *a, **k): return None
    async def get_mt5_account(self, *a, **k): return {}
    async def get_bridge_health(self, *a, **k): return {}
    def get_candles(self, *a, **k): return []
    def get_signals(self, *a, **k): return []
    def get_tg_signals(self, *a, **k): return []
    def get_open_trades(self, *a, **k): return []
    def compute_mt5_performance(self, *a, **k): return {}


class _FakeTgReader:
    """The Telegram tab's view of the reader.

    Every name frontend/pages/telegram.py reads off `reader`, with values of
    the right shape. Explicit rather than a MagicMock: a mock reaching a
    widget prop breaks NiceGUI's JSON serialisation, and a mock returning a
    mock hides a genuinely missing attribute.
    """

    auth_state = "disconnected"
    auth_error = None

    def get_status(self, *a, **k): return {"slots": []}
    def get_groups(self, *a, **k): return []
    def get_buffer_messages(self, *a, **k): return []
    def get_dc_info(self, *a, **k): return {}
    def send_code(self, *a, **k): return None
    def verify_code(self, *a, **k): return None
    def verify_2fa(self, *a, **k): return None
    def disconnect(self, *a, **k): return None
    def reset_session(self, *a, **k): return None
    def select_group(self, *a, **k): return None
    def save_group_selections(self, *a, **k): return None
    def start_listener(self, *a, **k): return None


_fake_engine = _FakeEngine()
_fake_reader = _FakeTgReader()

_backend_app.get_engine = lambda: _fake_engine
_backend_app.get_tg_reader = lambda: _fake_reader

import frontend.app as _frontend_app  # noqa: E402  (must follow the patching)

# Belt and braces: frontend.app took its own references at import time.
_frontend_app._lifecycle_startup = _noop
_frontend_app._lifecycle_shutdown = _noop
_frontend_app.get_engine = lambda: _fake_engine
_frontend_app.get_tg_reader = lambda: _fake_reader

from nicegui import ui  # noqa: E402

ui.run(storage_secret="simulated secret")
