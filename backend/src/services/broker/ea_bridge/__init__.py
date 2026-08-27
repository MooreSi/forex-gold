"""
EA bridge — local TCP link between this app (the "brain": Telegram signal
parsing, ML scoring, decides direction/entry/SL/TP) and a companion MQL5 EA
(the "hands": places the order with real broker-side SL/TP and manages the
trail/partial-close ladder tick-by-tick, inside the MT5 terminal's own
OnTick — no polling, no asyncio-stall exposure, no Python/IPC hop on the
hot path).

Runs identically on the Mac (MT5 under Wine) and the VPS (native MT5) —
both sides only ever talk over 127.0.0.1, so the same protocol and the same
compiled .ex5 work unmodified on either machine.

Newline-delimited JSON, one connection (the EA connects out to this node's
own local Python process; this node never dials out to anything). If the
EA disconnects or stops heartbeating, is_ea_healthy() goes False and
engine.py's existing Python-side management (_monitor_loop's per-strategy
handlers) takes back over for any trade still marked managed_by='ea' — see
_fallback_watchdog_loop() in engine.py. Nothing about a trade is ever left
with no manager at all.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# Not called here any more -- the two call sites moved to _events.py, which
# reads this name back off the package at call time (see _events._pkg). It is
# the patch target the schedule-blocked fill tests use, so removing it as an
# unused import would disarm the gate they check.
from backend.src.services.risk.schedule import check_trading_schedule  # noqa: F401
from backend.src.utils.models import CONTRACT_SIZE
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.broker import repo as broker_repo
# Re-exported so the package's import surface is unchanged: four other services
# and several tests do `from ...ea_bridge import comment_for_trade` and friends.
from backend.src.services.broker.ea_bridge._ids import (  # noqa: F401
    COMMENT_ID_LEN,
    COMMENT_PREFIX,
    _LEG_ID_RE,
    _LEG_KIND_LABELS,
    comment_for_trade,
    leg_label,
    split_leg_trade_id,
    trade_id_prefix_from_comment,
)
from backend.src.services.broker.ea_bridge._panel import PanelMixin
from backend.src.services.broker.ea_bridge._events import EventsMixin
# Re-exported, not incidental: tests/core/test_ea_bridge_panel_actions.py reads
# ea_bridge._ORDER_ACTIONS to assert the panel button list has not drifted from
# the actions that place real orders.
from backend.src.services.broker.ea_bridge._panel import _ORDER_ACTIONS

log = logging.getLogger("ea_bridge")

# Strategies whose per-tick SL/TP/partial-close rules are fixed, deterministic
# point/percentage math — faithfully portable to MQL5 and handed to the EA.
# DPM is deliberately excluded: it continuously recomputes trail distance/BE
# trigger/partial % from live ATR, session, momentum, structural swing levels,
# and a per-trade calibration history read from this app's own SQLite DB —
# there is no MT5-native equivalent of that calibration loop, so DPM trades
# always stay Python-managed regardless of whether the EA is connected.
EA_PORTABLE_STRATEGIES = frozenset({
    "scale_out", "be_runner", "trail_stop", "protected_scale",
    "conservative", "scalp_runner", "conservative_trial",
    "signal_climber", "reversal_runner", "no_sl_scale",
    "adaptive_runner", "adaptive_runner_2", "orb_fixed",
    "limit_runner", "fixed_rr",
})

_HEARTBEAT_TIMEOUT_S = 8.0   # no EA ping/message within this -> treat as unhealthy
_HOST = "127.0.0.1"

# ── EA version handshake (2026-08-05) ────────────────────────────────────────
# The repo's mql5/ForexTraderBridge.mq5 and the terminal's compiled .ex5 are
# two unlinked files; nothing in MetaTrader reports that the build it is
# running predates the source. tools/deploy_ea.sh catches that on disk, but
# only for the terminals on the machine you happen to run it on, and only if
# you remember to run it. This catches it from the other end: the EA states
# its own version on every connection and we check it against the source we
# were shipped with, so a stale build is a log line instead of a day of
# fixes that were never loaded.
def _repo_root() -> Path:
    """The checkout root (the directory holding run.py).

    Walks up for the marker rather than counting parents: upstream counted two
    from forex_trader/core/, but this module now sits at
    backend/src/services/broker/, so the fixed index resolved to backend/src
    and the EA source lookup pointed at a file that does not exist -- the
    version handshake then reported every EA as stale. Found by
    tests/core/test_ea_bridge_version_handshake.py in the 2026-08-25 merge."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "run.py").exists():
            return candidate
    return here.parents[2]


_EA_SOURCE = _repo_root() / "mql5" / "ForexTraderBridge.mq5"
_EA_VERSION_RE = re.compile(r'^\s*#define\s+EA_VERSION\s+"([^"]+)"', re.M)
# MetaEditor stamps __DATETIME__ in local time, and this compares it to a
# local mtime -- sound only because the EA and this process always share a
# machine (see module docstring).
_EA_COMPILED_FMT = "%Y.%m.%d %H:%M:%S"
# The .ex5 is written when you press F7, the .mq5 when you save. Saving a
# file a second or two after the compile that read it is normal and means
# nothing; treat only a clear gap as evidence of an uncompiled edit.
_EA_COMPILE_SLACK_S = 120.0


def _expected_ea_version() -> Optional[str]:
    """EA_VERSION as declared by the repo copy of the EA source, or None if
    that source isn't present -- a packaged/frozen install ships the .ex5
    without the .mq5, and has nothing to compare against. Deliberately
    uncached: the source changes under a long-running dev process far more
    often than an EA reconnects, and reading ~100KB once per connection
    costs nothing.
    """
    try:
        m = _EA_VERSION_RE.search(_EA_SOURCE.read_text(errors="replace"))
    except OSError:
        return None
    return m.group(1) if m else None

# How long a leg fill waits for open_trade()'s INSERT to appear before giving
# up and leaving the row to core_template_placeholder_repair. The row only
# lands once the EA's parent ack returns, which core_open_trade allows up to
# 60s for on a multi-leg template -- so this must exceed that cap, not the 5s
# the ack used to be capped at. Waited OFF the reader loop, so a generous
# budget costs nothing (see _promote_leg_when_row_exists).
_LEG_ROW_WAIT_S = 75.0

# EA Template legs. HandleOpenTemplateGrid opens each leg of a template trade
# as its own broker position and reports it under its own suffixed trade_id:
#   "<trade_id>-a<N>"  Anchor leg   -- immediate market fill at open time
#   "<trade_id>-g<N>"  Grid leg     -- a resting limit that later fills
async def find_template_leg_tickets(trade_id: str, bridge: Any, days: int = 7) -> set[int]:
    """Every broker position opened for an EA Template trade's legs (the
    anchor plus any grid legs), discovered from the EA's own order comment
    -- the only link between a template trade's single Python row and its
    N broker positions, since sibling legs never get a row of their own.

    Returns an empty set if nothing can be resolved (deal history
    unavailable, or `trade_id` never actually opened as a template). Only
    tickets that have actually filled show up here -- a resting pending leg
    that hasn't filled yet produces no opening deal, so it is invisible to
    this lookup until it does.

    Same mechanism reversal_engine_manage.py's own _template_leg_tickets
    and history.py's _template_leg_maps already use independently; this is
    the shared version core_profit_sync.sync_profit uses so a grid's
    sibling legs' profit stops being silently dropped from net_pnl.
    """
    prefix = trade_id_prefix_from_comment(comment_for_trade(trade_id))
    if not prefix:
        return set()
    legs: set[int] = set()
    try:
        for d in (await bridge.get_deal_history(days) or []):
            if d.get("entry") != 0:
                continue  # opening deals carry the EA's comment
            if trade_id_prefix_from_comment(d.get("comment") or "") == prefix:
                pid = d.get("position_id")
                if pid:
                    legs.add(int(pid))
    except Exception:
        return set()
    return legs


def _resolve_port() -> int:
    try:
        import backend.src.config as _cfg_module
        return int(_cfg_module.get("ea_bridge_port", 9101))
    except Exception:
        return 9101


_PORT = _resolve_port()

# Ports an older build of this app listened on, kept open alongside the
# configured one.
#
# WHY (2026-08-07): MetaTrader stores the EA's InpPort inside the chart file,
# and persisted chart inputs always beat the recompiled source default. A
# terminal that crashes never writes its charts, so the restart restores
# whatever was last saved to disk -- which can predate a change to
# ea_bridge_port by days. That is exactly what happened: MT5 crashed at 13:35,
# restored a chart saved on 4 Aug carrying InpPort=9101, and the EA then
# reconnected every 2.3s to a port nothing was listening on while this process
# sat on 9111. Both sides looked healthy in their own logs and the link was
# dead. One extra idle listening socket makes that drift heal itself.
_LEGACY_PORTS = (9101,)


def listen_ports() -> list[int]:
    """Configured port first, then any legacy port not equal to it."""
    return [_PORT] + [p for p in _LEGACY_PORTS if p != _PORT]


class EABridge(PanelMixin, EventsMixin):
    def __init__(self, engine):
        self._engine = engine
        # port -> listening server. Several, so an EA whose chart-persisted
        # InpPort predates a port change still lands here (see _LEGACY_PORTS).
        self._servers: dict[int, Any] = {}
        self._writer: Optional[asyncio.StreamWriter] = None
        self._last_seen: float = 0.0
        # Wall-clock time the EA last connected, 0.0 if it never has in this
        # process. The EA link watchdog uses it to tell "the EA dropped and
        # hasn't come back" apart from "this install doesn't use the EA".
        self.last_connected_at: float = 0.0
        # trade_id -> {"ticket": int, "strategy": str} for trades currently
        # handed to the EA — used to validate/enrich incoming events and by
        # the fallback watchdog to know what needs reclaiming if the EA dies.
        self._active: dict[str, dict] = {}
        # trade_id -> {"ticket": int} for Limit Runner orders currently
        # resting on the broker (placed, not yet filled/cancelled/expired).
        self._pending_orders: dict[str, dict] = {}
        # What the connected EA said about itself in "hello" -- see
        # _check_ea_version(). None until an EA connects; an EA old enough to
        # predate the handshake leaves ea_version None while still connecting
        # normally, which is itself the strongest possible staleness signal.
        self.ea_version: Optional[str] = None
        self.ea_compiled: Optional[str] = None
        self.ea_version_ok: Optional[bool] = None
        # On-chart panel: which CH tab the terminal has selected (0-based,
        # indexes TelegramReader's own slots) and the periodic push task that
        # feeds the panel's right-hand dashboard. The slot lives here rather
        # than on the EA because a panel_action arrives with only a template
        # name, and after an EA reload the terminal has forgotten which tab
        # it was on -- this is what lets the reconnect push restore it.
        self._panel_slot: int = 0
        self._panel_task: Optional[asyncio.Task] = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self.bind_ports()

    async def bind_ports(self) -> list[int]:
        """Listen on every port in listen_ports() that isn't bound yet, and
        return the ones newly bound.

        Idempotent, so the EA link watchdog can call it again later: a legacy
        port held by another process at startup (a second copy of the app, a
        leftover from a crash) is not fatal here, and this picks it up once
        that process lets go. Failing to bind the *configured* port is still
        fatal -- that is the port a current EA dials, and silently running
        without it is how a dead link goes unnoticed.
        """
        newly: list[int] = []
        for port in listen_ports():
            if port in self._servers:
                continue
            try:
                srv = await asyncio.start_server(self._handle_conn, _HOST, port)
            except OSError as e:
                if port == _PORT:
                    raise
                log.info("[EABridge] fallback port %d unavailable (%s) — "
                         "will retry while the EA is offline", port, e)
                continue
            self._servers[port] = srv
            newly.append(port)
            log.info("[EABridge] listening on %s:%d%s", _HOST, port,
                     "" if port == _PORT else " (fallback for a stale chart InpPort)")
        return newly

    def listening_ports(self) -> list[int]:
        return sorted(self._servers)

    async def stop(self) -> None:
        if self._panel_task is not None:
            self._panel_task.cancel()
            self._panel_task = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        for srv in self._servers.values():
            srv.close()
        self._servers.clear()

    def is_ea_healthy(self) -> bool:
        return self._writer is not None and (time.time() - self._last_seen) < _HEARTBEAT_TIMEOUT_S

    def is_strategy_portable(self, strategy: str) -> bool:
        # EA Templates (Trading > Strategy > EA Templates, "template:<name>"
        # channel overrides) are always EA-portable by design -- a template
        # IS an EA-native management definition, there's no Python-managed
        # equivalent to fall back to (see core_open_trade.open_trade's
        # template branch, which raises rather than falling through to the
        # Python bridge path when the EA isn't reachable).
        from backend.src.services.broker.ea_templates import is_template_override
        if is_template_override(strategy):
            return True
        return strategy in EA_PORTABLE_STRATEGIES

    # ── Connection handling ──────────────────────────────────────────────────

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        # Only one EA per node — a second connection replaces the first
        # (covers an EA restart/chart reload without a stale writer lingering).
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._writer = writer
        self._last_seen = time.time()
        self.last_connected_at = self._last_seen
        local = writer.get_extra_info("sockname")
        local_port = local[1] if isinstance(local, tuple) and len(local) > 1 else None
        log.info("[EABridge] EA connected from %s on port %s", peer, local_port)
        if local_port is not None and local_port != _PORT:
            log.warning(
                "[EABridge] EA reached us on fallback port %d, not the configured "
                "%d — the chart's saved InpPort is stale (MetaTrader restores a "
                "crashed terminal's charts from the last file it wrote, inputs "
                "and all). The link is working, but set InpPort=%d in the EA's "
                "inputs and save the chart so it survives the next restart.",
                local_port, _PORT, _PORT,
            )
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                self._last_seen = time.time()
                try:
                    msg = json.loads(line.decode().strip())
                except Exception as e:
                    log.debug("[EABridge] bad message: %s (%s)", line[:200], e)
                    continue
                await self._dispatch(msg)
        except Exception as e:
            log.info("[EABridge] EA connection closed: %s", e)
        finally:
            if self._writer is writer:
                self._writer = None
                # Only clear the reported version if this really was the live
                # connection. A replaced-by-reconnect writer arrives here
                # AFTER its replacement already ran _check_ea_version, and
                # must not wipe the new EA's identity.
                self.ea_version = None
                self.ea_compiled = None
                self.ea_version_ok = None
            try:
                writer.close()
            except Exception:
                pass

    async def _send(self, msg: dict) -> bool:
        # drain() has no timeout of its own — if the EA's TCP connection
        # stalls (e.g. it's slow to read while busy managing an existing
        # trade, or the socket has gone zombie without a clean FIN), this
        # would otherwise block forever with nothing above it timing out.
        # That silently hung every caller (open_trade/update_trade), which
        # in turn hung open_manual_market_order() and left the Market Order
        # button greyed out until the page was refreshed. Bounded here so a
        # stalled EA connection fails fast instead.
        if self._writer is None:
            return False
        try:
            self._writer.write((json.dumps(msg) + "\n").encode())
            await asyncio.wait_for(self._writer.drain(), timeout=3.0)
            return True
        except Exception as e:
            log.warning("[EABridge] send failed: %s", e)
            return False

    # ── Outbound: hand a fully-decided trade to the EA ───────────────────────

    async def open_trade(self, trade_id: str, direction: str, lot_size: float,
                         stop_loss: float, tps: dict[int, float], strategy: str,
                         pcts: Optional[list[float]] = None,
                         be_at_pos: Optional[int] = None,
                         trail_mode: Optional[str] = None,
                         template: Optional[dict] = None,
                         zone_low: Optional[float] = None,
                         zone_high: Optional[float] = None,
                         timeout: float = 5.0) -> dict:
        """Ask the EA to place the order and take over its management.
        Returns the trade_opened/trade_open_failed payload. Raises
        TimeoutError if the EA doesn't respond — caller falls back to the
        existing Python/bridge open_trade path in that case.

        pcts/be_at_pos: for the ladder-shaped strategies (signal_climber,
        reversal_runner, adaptive_runner, adaptive_runner_2) — the per-TP
        close percentage table and the compacted TP position where SL
        first moves to breakeven. Sent over the wire as flat pct1..pct8 +
        be_at_pos fields (matching tp1..tp8's existing pattern — the EA's
        JSON parser is deliberately flat/non-nested, see
        ForexTraderBridge.mq5's own comment on this). Python is the single
        source of truth for these tables; the EA no longer hardcodes its
        own copy for these strategies, so a tuning change needs no EA
        rebuild.

        trail_mode: which SL rule to apply to every TP after be_at_pos.
        None/omitted (the default, and what every strategy before Adaptive
        Runner 2 sends) means "trail to the single immediately-previous TP
        price" — the EA's original, still-hardcoded fallback behaviour.
        "midpoint_lag2" means "trail to the midpoint of the two TPs before
        this one" (Adaptive Runner 2) — unlike pcts/be_at_pos, this DOES
        require its own branch in the EA's ManageLadder(), since the rule
        itself (not just its parameters) differs; see
        core_run_tp_ladder.run_tp_ladder's sl_rule parameter for the
        Python-side equivalent, which must stay in lockstep with this.

        template: the full EA Template field dict (core_ea_templates.py)
        for a "template:<name>" strategy -- sent as flat tpl_* fields
        (same flat-JSON convention as pct1..pct8/be_at_pos above) so the
        EA can run Grid/Stealth/Anchor/Trail/BE/Cancel-Pending/Harvest
        management natively, no recompile needed when a template's values
        change. None for every other (built-in) strategy.
        """
        if not self.is_ea_healthy():
            raise ConnectionError("EA not connected/healthy")
        msg = {
            "type": "open_trade", "trade_id": trade_id, "direction": direction,
            "lot_size": lot_size, "stop_loss": stop_loss, "strategy": strategy,
        }
        for n in range(1, 9):
            if n in tps:
                msg[f"tp{n}"] = tps[n]
        if pcts is not None:
            for i, p in enumerate(pcts, start=1):
                msg[f"pct{i}"] = p
            if be_at_pos is not None:
                msg["be_at_pos"] = be_at_pos
            if trail_mode is not None:
                msg["trail_mode"] = trail_mode
        if template is not None:
            # Every template field is forwarded generically as tpl_<key>.
            #
            # This used to be a hand-written line per field, which meant a
            # new template setting needed edits in three places (the
            # template schema, here, and the EA's own parser) and an EA
            # recompile before it could do anything. The EA now keeps the
            # raw payload and reads keys on demand with a default (see
            # TplS/TplD/TplI/TplB in ForexTraderBridge.mq5), so a field
            # added to core_ea_templates.DEFAULTS reaches the EA with no
            # change here and no recompile. Keys the EA doesn't understand
            # are carried harmlessly.
            #
            # Bools are coerced to 1/0 rather than sent as native JSON
            # booleans: the EA's minimal parser only understands numbers
            # and strings for flag fields. Confirmed live 2026-07-23 --
            # sending a Python bool silently evaluated false on the EA
            # side (StringToInteger("true") == 0), so harvest and grid
            # cancel-pending never fired regardless of the setting.
            for _k, _v in template.items():
                if _k in ("name", "created_at", "updated_at"):
                    continue          # bookkeeping, not behaviour
                msg[f"tpl_{_k}"] = (1 if _v else 0) if isinstance(_v, bool) else _v
            # Grid mode, zone-spanned staging (2026-07-28) -- the signal's own
            # stated entry zone. When present, HandleOpenTemplateGrid stages
            # the legs ACROSS this zone rather than stepping grid_step_pts
            # away from current price: a "BUY LIMITS 4063/4068 AREA" message
            # is itself already a grid instruction, and its SL sits just
            # beyond the zone, so fixed stepping walks the legs straight
            # through the stop and every one gets broker-rejected as invalid
            # stops. Omitted (and the EA falls back to step-based staging)
            # for any signal with no meaningful zone.
            if (zone_low is not None and zone_high is not None
                    and float(zone_high) > float(zone_low) > 0):
                msg["zone_low"]  = float(zone_low)
                msg["zone_high"] = float(zone_high)
        ack_event = asyncio.Event()
        ack_box: dict = {}

        def _on_ack(payload: dict) -> None:
            ack_box.update(payload)
            ack_event.set()

        self._pending_open_acks = getattr(self, "_pending_open_acks", {})
        self._pending_open_acks[trade_id] = _on_ack
        try:
            if not await self._send(msg):
                raise ConnectionError("EA send failed")
            await asyncio.wait_for(ack_event.wait(), timeout=timeout)
        finally:
            self._pending_open_acks.pop(trade_id, None)

        if ack_box.get("type") == "trade_opened":
            self._active[trade_id] = {
                "ticket": ack_box.get("ticket"), "strategy": strategy,
            }
        return ack_box

    async def place_pending_order(self, trade_id: str, direction: str, price: float,
                                  lot_size: float, stop_loss: float, tps: dict[int, float],
                                  pcts: list[float], be_at_pos: int, strategy: str,
                                  expire_minutes: float = 240.0,
                                  close_full_on_last: bool = True,
                                  trail_mode: Optional[str] = None,
                                  timeout: float = 5.0) -> dict:
        """Ask the EA to place a genuine resting BuyLimit/SellLimit order —
        unlike open_trade(), this does NOT fill immediately, so the returned
        ack only confirms the order was accepted onto the broker's book, not
        that a trade has opened. Raises TimeoutError if the EA doesn't
        respond. There is no Python-bridge fallback for this call (see
        core_limit_order_signal.py) — a raised/failed result here means the
        signal is simply not captured, not "retry a different way."

        The eventual fill is reported later, asynchronously, as an
        unsolicited "pending_order_filled" message (see _dispatch) — MT5
        gives no synchronous fill confirmation for a pending order, only for
        the order's initial acceptance onto the book.

        strategy: which management strategy the EA (and, once filled,
        ManageTrade's dispatch) should treat this trade as — e.g.
        "limit_runner" (ladder-managed, ManageLadder) or "orb_fixed"
        (single-TP close-all, ManageOrbFixed). Required explicitly rather
        than assumed, since a wrong value here silently mismanages the
        trade once it fills — see _on_pending_order_filled, which stores
        this on vantage_pending_orders and reads it back at fill time
        instead of hardcoding a strategy the way earlier versions of this
        method did.

        tps/pcts/be_at_pos: same shapes as open_trade()'s own ladder
        params (flat tp1..tp8/pct1..pct8/be_at_pos fields). Only meaningful
        for ladder-managed strategies (ManageLadder reads t.pcts/t.beAtPos);
        a single-TP strategy like orb_fixed ignores them — ManageOrbFixed
        just closes everything the instant its one TP is touched — so callers
        for those can pass tps={1: target}, pcts=[1.0], be_at_pos=0 as inert
        placeholders.

        close_full_on_last: True (default) means the last TP closes
        everything remaining, same as every other ladder strategy. False —
        sent only when the originating signal had a literal "TP OPEN" line
        (core_limit_order_signal.py) — means the last TP only closes its own
        pcts[] share, leaving the rest open indefinitely with no further TP
        to close it (ManageLadder's own close_full_on_last branch handles
        this the same way core_run_tp_ladder.run_tp_ladder's Python-side
        fallback does). Sent as an int (0/1), matching this EA's minimal
        JSON parser (no native boolean support — see be_at_pos above it).

        trail_mode: same meaning as open_trade()'s own trail_mode param
        (None/omitted = trail to previous TP; "midpoint_lag2" = Adaptive
        Runner 2's rule) — needed because place_pending_order() is now
        reused by more than just Limit Runner (e.g. Reversal Engine's LIMIT
        ORDER toggle can resolve to any strategy, including AR2).
        """
        if not self.is_ea_healthy():
            raise ConnectionError("EA not connected/healthy")
        msg = {
            "type": "place_pending_order", "trade_id": trade_id, "direction": direction,
            "price": price, "lot_size": lot_size, "stop_loss": stop_loss,
            "strategy": strategy, "expire_minutes": expire_minutes,
            "be_at_pos": be_at_pos, "close_full_on_last": int(close_full_on_last),
        }
        if trail_mode is not None:
            msg["trail_mode"] = trail_mode
        for n in range(1, 9):
            if n in tps:
                msg[f"tp{n}"] = tps[n]
        for i, p in enumerate(pcts, start=1):
            msg[f"pct{i}"] = p
        ack_event = asyncio.Event()
        ack_box: dict = {}

        def _on_ack(payload: dict) -> None:
            ack_box.update(payload)
            ack_event.set()

        self._pending_open_acks = getattr(self, "_pending_open_acks", {})
        self._pending_open_acks[trade_id] = _on_ack
        try:
            if not await self._send(msg):
                raise ConnectionError("EA send failed")
            await asyncio.wait_for(ack_event.wait(), timeout=timeout)
        finally:
            self._pending_open_acks.pop(trade_id, None)

        if ack_box.get("type") == "pending_order_placed":
            self._pending_orders[trade_id] = {"ticket": ack_box.get("ticket")}
        return ack_box


    async def push_template(self, name: str, template: dict) -> bool:
        """Push a template's values to the EA immediately, outside of any
        trade open.

        A template is normally sent with each open_trade/place_pending_order
        call, so an edit only reaches the EA on the NEXT signal. That is
        fine for a change made between sessions, but not for adjusting a
        live setup -- hence the panel's Send button, which calls this.

        Sent as set_template with the same generic tpl_<key> encoding
        open_trade uses, so the EA parses it through exactly the same path
        (and, like open_trade, simply ignores keys it doesn't recognise).
        Best-effort: returns False when the EA isn't connected rather than
        raising, since the values are already saved and will apply on the
        next signal regardless.
        """
        if not self.is_ea_healthy():
            return False
        msg = {"type": "set_template", "template_name": name}
        for k, v in template.items():
            if k in ("name", "created_at", "updated_at"):
                continue
            msg[f"tpl_{k}"] = (1 if v else 0) if isinstance(v, bool) else v
        ok = await self._send(msg)
        if ok:
            log.info("[EABridge] pushed template '%s' to EA (%d fields)",
                     name, len(msg) - 2)
        return ok

    async def restore_pending_order(self, row: dict) -> None:
        """Push one still-'working' vantage_pending_orders row back to the
        EA right after it reconnects (see _dispatch's "hello" handling).

        g_pending[] is pure in-memory state on the EA side with no
        persistence of its own -- any EA restart (recompile, terminal
        restart, a dropped socket that re-triggers OnInit) silently forgets
        every order that was still resting, and CheckPendingOrders() then
        has nothing left to check: it can never again notice that order's
        eventual fill or broker-side expiry. Confirmed live 2026-07-24: 5
        Limit Runner orders sat "pending" in the UI for 16+ hours after
        genuinely expiring on MT5 hours earlier, because whichever EA
        restart happened in between wiped them from tracking with no way
        for either side to notice afterward. Python is the durable source
        of truth for every field here, so pushing it back closes that gap
        regardless of why tracking was lost.

        Fire-and-forget: no ack is awaited. The EA's own reply -- nothing,
        for an order still genuinely resting; pending_order_filled/
        pending_order_cancelled for one that resolved while this EA was
        disconnected -- already routes through the normal _dispatch
        handlers, identical to a live fill/cancel event."""
        tps  = json.loads(row["tps_json"])
        pcts = json.loads(row["pcts_json"])
        msg = {
            "type": "restore_pending_order",
            "trade_id": row["trade_id"],
            "ticket": row["ea_ticket"],
            "direction": row["direction"],
            "lot_size": row["lot_size"],
            "stop_loss": row["stop_loss"],
            "strategy": row["strategy"],
            "be_at_pos": row["be_at_pos"],
            "close_full_on_last": 0 if row.get("tp_open") else 1,
        }
        for n_str, price in tps.items():
            msg[f"tp{n_str}"] = price
        for i, p in enumerate(pcts, start=1):
            msg[f"pct{i}"] = p
        await self._send(msg)

    async def _restore_pending_orders(self) -> None:
        """Called once per EA connection (on "hello") -- restores every
        still-'working' pending order so a prior EA restart can't leave any
        of them permanently untracked. See restore_pending_order()."""
        from backend.src.db import database as db_module

        from backend.src.services.broker import repo as broker_repo
        rows = await db_module.to_db_thread(broker_repo.fetch_working_pending_orders)
        for row in rows:
            try:
                await self.restore_pending_order(row)
            except Exception as e:
                log.warning("[EABridge] restore_pending_order failed for trade_id=%s: %s",
                            row.get("trade_id"), e)

    async def restore_trade(self, row: dict) -> None:
        """Push one still-open EA-managed POSITION back to the EA after it
        reconnects.

        g_trades[] has exactly the same no-persistence problem
        restore_pending_order() documents for g_pending[], but for live
        positions rather than resting orders, and it went unclosed until
        2026-08-04. Any EA restart (recompile, terminal restart, dropped
        socket re-triggering OnInit) silently forgot every open position:
        no partial closes, no breakeven, no trailing, and -- because the
        app learns a trade closed from the EA's own trade_closed message --
        no close notification either, so the row stayed 'open' in
        vantage_simulated_trades forever.

        Confirmed live 2026-08-04, ticket 1704757612: a recompile at 15:30
        orphaned it, it closed at the broker at 16:13 for +$35, and the
        trades table still read status='open' remaining_lots=0.1 net_pnl=0
        afterwards. That combination got worse, not better, once
        close_full_on_last=false legitimately started leaving positions with
        NO broker-side TP -- an orphan then has nothing at all to close it.

        Sends the template payload fresh from the DB rather than anything
        cached, so a restored trade is managed by the template's CURRENT
        settings, same as set_template already does for live ones.
        """
        from backend.src.services.broker import ea_templates as ea_templates
        from backend.src.services.trading.open_trade import (
            _EA_LADDER_PCTS, _EA_LADDER_BE_AT_POS, _EA_LADDER_TRAIL_MODE,
        )

        strategy = row.get("strategy") or ""
        msg = {
            "type": "restore_trade",
            "trade_id": row["trade_id"],
            "ticket": int(row["mt5_ticket"]),
            "direction": (row.get("direction") or "").upper(),
            "entry_price": float(row.get("entry_price") or 0),
            "orig_lots": float(row.get("lot_size") or 0),
            # What is left NOW. The EA uses this to work out how much of the
            # ladder already fired, so restoring cannot re-run a partial
            # close that has already happened.
            "remaining_lots": float(row.get("remaining_lots") or 0),
            "stop_loss": float(row.get("stop_loss") or 0),
            "strategy": strategy,
        }
        for n in range(1, 9):
            v = row.get(f"tp{n}")
            if v:
                msg[f"tp{n}"] = float(v)

        if ea_templates.is_template_override(strategy):
            tpl = ea_templates.get_ea_template(
                ea_templates.template_name_from_override(strategy))
            if tpl:
                for k, v in tpl.items():
                    if k in ("name", "created_at", "updated_at"):
                        continue
                    msg[f"tpl_{k}"] = (1 if v else 0) if isinstance(v, bool) else v
                pcts = [float(tpl.get(f"tp{n}_pct", 0) or 0) / 100.0 for n in range(1, 9)]
                for i, p in enumerate(pcts, start=1):
                    msg[f"pct{i}"] = p
        elif strategy in _EA_LADDER_PCTS:
            _table = _EA_LADDER_PCTS[strategy]
            _n_tps = sum(1 for n in range(1, 9) if row.get(f"tp{n}"))
            for i, p in enumerate(_table.get(_n_tps, _table[max(_table)]), start=1):
                msg[f"pct{i}"] = p
            msg["be_at_pos"] = _EA_LADDER_BE_AT_POS[strategy]
            if _EA_LADDER_TRAIL_MODE.get(strategy):
                msg["trail_mode"] = _EA_LADDER_TRAIL_MODE[strategy]

        await self._send(msg)

    async def _restore_open_trades(self) -> None:
        """Called once per EA connection (on "hello"), alongside
        _restore_pending_orders. See restore_trade()."""
        from backend.src.db import database as db_module

        rows = await db_module.to_db_thread(broker_repo.fetch_open_ea_managed_trades)
        if not rows:
            return
        for row in rows:
            try:
                await self.restore_trade(row)
            except Exception as e:
                log.warning("[EABridge] restore_trade failed for trade_id=%s: %s",
                            row.get("trade_id"), e)
        log.info("[EABridge] pushed %d open position(s) back to the EA after reconnect",
                 len(rows))

    async def update_trade(self, trade_id: str, tps: dict[int, float],
                           stop_loss: float | None = None) -> bool:
        """Push corrected TP levels to a trade the EA is already managing.

        The EA captures tp[]/hasTp[] once, from the open_trade message, and
        never refreshes them on its own — so an IME instant entry (opened
        with provisional/no TP levels) whose real TP1 arrives a minute later
        via a follow-up signal was previously invisible to the EA forever:
        Python's DB had the corrected tp1, but the EA's own on-tick
        TpCleared() check kept comparing against its stale original value,
        so the partial close at TP1 could never fire. Only meaningful for
        trades this bridge is actively tracking (self._active) — a no-op
        (returns False) for anything Python-managed, where update_signal()'s
        normal DB write is already sufficient since the per-tick handler
        re-reads the trade row fresh every cycle.
        """
        if trade_id not in self._active:
            return False
        msg = {"type": "update_trade", "trade_id": trade_id}
        for n in range(1, 9):
            if n in tps:
                msg[f"tp{n}"] = tps[n]
        # stop_loss (2026-08-04): the EA is the only writer of this order
        # while it is healthy (core_update_signal deliberately skips its own
        # modify_order to avoid racing the EA's SL progression), so a
        # corrected stop has to travel this way or it never leaves Python's
        # DB. See core_update_signal's call site for the live case that
        # exposed it.
        if stop_loss:
            msg["stop_loss"] = float(stop_loss)
        return await self._send(msg)

    async def cancel_pending_order(self, trade_id: str, ticket: int, reason: str) -> bool:
        """Pull a still-resting broker-side pending order before it can fill
        blind (core_pending_order_revalidation.py). Fire-and-forget, same as
        update_trade -- the EA deletes the order and reports its own
        pending_order_cancelled event back (_on_pending_order_cancelled),
        carrying `reason` through unchanged, exactly like any other
        cancellation (expiry, manual). That existing handler already does
        the vantage_pending_orders/vantage_signals update and Telegram
        alert, so this call itself does neither.
        """
        return await self._send({
            "type": "cancel_pending_order", "trade_id": trade_id,
            "ticket": ticket, "reason": reason,
        })

    async def push_global_config(self) -> bool:
        """Push Trading > Global Parameters' Harvest setting to the EA --
        called after every save on that card, and once per connection (see
        _dispatch's "hello" handling) since the EA's own copy is pure
        in-memory state with no persistence, same gap restore_pending_order
        closes for pending orders.

        Unlike the old per-template tpl_harvest_enabled/tpl_harvest_threshold
        (sent once, at open_trade time, and only ever applied to that one
        EA-managed trade), this is standing config the EA checks against
        EVERY open position on the symbol each tick — see
        CheckGlobalHarvest() in ForexTraderBridge.mq5 — so it applies
        immediately to trades already open when the setting changes, and to
        Python-managed (non-EA) trades too, not just ones opened after the
        change. Fire-and-forget: no ack expected, same as update_trade.
        """
        from backend.src.db import database as db_module
        rs = await db_module.to_db_thread(db_module.get_risk_settings)
        msg = {
            "type": "set_global_config",
            "harvest_enabled": 1 if rs.get("global_harvest_enabled", 0) else 0,
            "harvest_threshold": float(rs.get("global_harvest_threshold_usd", 50.0)),
        }
        return await self._send(msg)

    # ── Inbound events from the EA ────────────────────────────────────────────

    def _check_ea_version(self, msg: dict) -> None:
        """Compare the connecting EA's self-reported build against the EA
        source this app was shipped with, and say so loudly when they differ.

        ea_version_ok tracks the version comparison alone: True/False when
        there is a source version to compare, None when there isn't. The
        source-newer-than-binary check below is advisory and deliberately
        does NOT flip it false -- it fires on any unsaved-then-saved edit,
        including ones that never reach a terminal, so it is worth a warning
        but not worth anything downstream branching on.

        Only ever logs. A stale EA is still a working EA -- it manages trades
        with whatever rules it was compiled with -- so refusing to talk to it
        would turn "some fixes aren't live" into "nothing is managed", which
        is strictly worse. The point is that the mismatch stops being silent.
        """
        self.ea_version = msg.get("ea_version") or None
        self.ea_compiled = msg.get("compiled") or None
        expected = _expected_ea_version()

        if self.ea_version is None:
            self.ea_version_ok = False
            log.warning(
                "[EABridge] EA sent no version in hello -- it predates the "
                "version handshake (expected v%s). Deploy and recompile: "
                "tools/deploy_ea.sh", expected or "?")
            return

        if expected is None:
            # Packaged install with no .mq5 alongside. Record what connected
            # so it still shows up in logs, but there's nothing to check.
            self.ea_version_ok = None
            log.info("[EABridge] EA v%s (compiled %s); no EA source present "
                     "to check it against", self.ea_version, self.ea_compiled)
            return

        self.ea_version_ok = (self.ea_version == expected)
        if not self.ea_version_ok:
            log.warning(
                "[EABridge] EA VERSION MISMATCH: terminal is running v%s "
                "(compiled %s) but this app ships EA source v%s. The .ex5 is "
                "stale -- run tools/deploy_ea.sh, then compile (F7).",
                self.ea_version, self.ea_compiled, expected)
            return

        # Versions agree, so check the weaker signal too: an edit made after
        # the last compile that didn't move EA_VERSION is invisible to the
        # comparison above, but does show up as source newer than binary.
        try:
            compiled_at = datetime.strptime(self.ea_compiled or "", _EA_COMPILED_FMT)
            src_mtime = datetime.fromtimestamp(_EA_SOURCE.stat().st_mtime)
        except (ValueError, OSError):
            compiled_at = src_mtime = None
        if compiled_at is not None and src_mtime is not None:
            drift = (src_mtime - compiled_at).total_seconds()
            if drift > _EA_COMPILE_SLACK_S:
                log.warning(
                    "[EABridge] EA v%s matches, but the source was modified "
                    "%.0f min after this build was compiled (%s) -- there are "
                    "edits the running EA does not have. Recompile (F7).",
                    self.ea_version, drift / 60.0, self.ea_compiled)
                return

        log.info("[EABridge] EA v%s (compiled %s, MQL build %s, terminal "
                 "build %s)", self.ea_version, self.ea_compiled,
                 msg.get("mql_build"), msg.get("terminal_build"))


    async def _fetch_pending_order(self, trade_id: str) -> Optional[dict]:
        from backend.src.db import database as db_module
        from backend.src.services.broker import repo as broker_repo
        return await db_module.to_db_thread(broker_repo.fetch_pending_order, trade_id)

    async def _fetch_trade(self, trade_id: str) -> Optional[dict]:
        from backend.src.db import database as db_module
        from backend.src.services.broker import repo as broker_repo
        return await db_module.to_db_thread(broker_repo.fetch_trade, trade_id)


_instance: Optional[EABridge] = None


def schedule_push_template(instance, name: str, values: dict) -> None:
    """Fire-and-forget a template push to the EA from a sync caller.

    Exists so the UI does not have to import _schedule_coro out of
    backend.src.db.database: "frontend never imports the database" is enforced
    at zero (tests/refactor/test_import_contracts.py), and the scheduler only
    lives in that module for historical reasons -- it is an event-loop utility,
    not data access. The thread-safety it provides is still needed: this runs on
    a NiceGUI handler thread, not the loop thread. (2026-08-25 merge.)
    """
    from backend.src.db.database import _schedule_coro
    _schedule_coro(instance.push_template(name, values))


def get_instance() -> Optional[EABridge]:
    return _instance


def set_instance(bridge: Optional[EABridge]) -> None:
    global _instance
    _instance = bridge


def get_effective_ea_status() -> tuple[bool, str]:
    """Return (ea_connected, scope_label) for whichever node is actually
    executing trades right now.

    If a paired Local/Remote node is the active trader, this node's own EA
    connection is irrelevant -- it isn't placing any orders -- so this
    reflects the active node's EA state via the sync heartbeat instead
    (SyncServer._status_payload's "ea_connected" field, refreshed every 3s
    on its own). Otherwise (standalone, or this node itself is the active
    trader) this reflects this node's own EABridge connection directly.

    Used by the top-bar EA badge (see ui/app.py) so it stays accurate no
    matter which node is actually doing the trading."""
    try:
        from backend.src.db import database as db_module
        from backend.src.services.cluster.sync import client as _sync_cli_mod
        cli = _sync_cli_mod.get_instance()
        if cli is not None and cli.conn_state == "connected" and db_module.get_active_trader() != "local":
            return bool(cli.remote_status.get("ea_connected")), "VPS"
    except Exception:
        pass
    _ea = get_instance()
    return (_ea is not None and _ea.is_ea_healthy()), "this node"
