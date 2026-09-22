"""The MT5 bridge has to follow the demo/live switch, and a restart is not
enough to make it.

`services/broker/environment.switch` writes the target account's credentials
to `bridge_credentials.json` and the app restarts, on the stated assumption
that a restart rebuilds "every cached handle — the runtime, the bridge, the
engines". The bridge is not such a handle. On a Mac it is a Wine subprocess
with the MT5 terminal behind it, and `run._start_mt5_bridge` deliberately
leaves one that is already listening alone — a Wine relaunch tears down
wineserver and every child, which would turn a routine restart into an outage.

Measured on the owner's Mac, 2026-09-22: bridge and terminal up since 10:12,
the app switched to live and restarted at 17:17, `config.yaml` reading
`account_env: live` and `bridge_credentials.json` holding the live account —
and MT5 still logged into the demo one. The dashboard's badge, which reports
what the BRIDGE says, therefore still read DEMO, which is what "the switch did
nothing" looks like from the outside.

The missing step is `environment.align_bridge`, called on the way up. These
tests pin the two ends of that seam; what it actually does is in
`test_environment_switch.py`.
"""
from __future__ import annotations

import inspect


class TestStartupChecksWhichAccountTheBridgeIsOn:
    def test_app_startup_aligns_the_bridge(self):
        from backend.src import app

        assert "align_bridge(" in inspect.getsource(app), (
            "nothing on the startup path re-points the bridge, so a demo/live "
            "switch leaves MetaTrader 5 on the account the app just left"
        )

    def test_it_runs_after_the_credentials_file_has_been_synced(self):
        """The bridge re-reads that file when it reconnects. Aligning before
        it is written would re-point it at the account being left."""
        from backend.src import app

        src = inspect.getsource(app)

        assert src.index("sync_bridge_credentials_file") < src.index("align_bridge(")

    def test_debug_mode_never_re_points_a_bridge(self):
        """Debug mode runs on `fake_bridge`, whose account is a fiction
        (login 80000000). Comparing it against real credentials would send a
        real broker password into the fake on every debug boot."""
        from backend.src import app

        lines = inspect.getsource(app).splitlines()
        call = next(i for i, ln in enumerate(lines) if "align_bridge(" in ln)

        assert any("is_debug()" in ln for ln in lines[max(0, call - 6):call])


class TestWhyTheRestartIsNotEnough:
    def test_boot_leaves_a_bridge_that_is_already_listening_alone(self):
        """The fact that makes alignment necessary. If this ever stops being
        true — if boot starts killing and relaunching the bridge — the
        alignment becomes belt and braces rather than the only thing holding
        the two accounts together."""
        src = open("run.py", encoding="utf-8").read()

        assert "is_port_listening" in src
