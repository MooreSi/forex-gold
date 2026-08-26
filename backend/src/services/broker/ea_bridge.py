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

from backend.src.services.risk.schedule import check_trading_schedule
from backend.src.utils.models import CONTRACT_SIZE
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.broker import repo as broker_repo

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
# Python keeps ONE vantage_simulated_trades row per template trade, so every
# inbound leg event has to be mapped back onto that row (see
# EABridge._resolve_leg_event). Anchor-leg events used to have no mapping at
# all: their unsolicited "trade_opened" matched no open_trade() ack callback
# and was dropped, so the placeholder row kept mt5_ticket=0/entry_price=0
# forever and every later tp_hit/sl_moved/trade_closed for the leg was logged
# as "unknown trade_id" and discarded (confirmed live 2026-07-29 on
# 76687f1a/e93f3fe7/c2ebb432).
_LEG_ID_RE = re.compile(r"^(?P<base>.+)-(?P<kind>[ag])(?P<num>\d+)$")

_LEG_KIND_LABELS = {"a": "Anchor Leg", "g": "Grid Leg"}

# panel_action values that place or pull real orders, as opposed to editing a
# template field. Listed explicitly so a typo in an action name can never fall
# through to the generic template-field path and quietly write garbage into a
# saved template.
_ORDER_ACTIONS = frozenset({
    "market_buy", "market_sell", "limit_buy", "limit_sell",
    "close_all", "cancel_limits",
})


def split_leg_trade_id(trade_id: str) -> tuple[str, Optional[str], str]:
    """Split an EA leg trade_id into (base_trade_id, kind, leg_num).

    kind is "a" (anchor), "g" (grid), or None when the id carries no leg
    suffix at all -- in which case base_trade_id is the id unchanged."""
    m = _LEG_ID_RE.match(trade_id or "")
    if not m:
        return (trade_id, None, "")
    return (m.group("base"), m.group("kind"), m.group("num"))


def leg_label(kind: Optional[str], num: str) -> str:
    """Human label for a leg suffix, e.g. ("g", "2") -> "Grid Leg 2"."""
    return f"{_LEG_KIND_LABELS.get(kind or '', 'Leg')} {num}".strip()


# The order comment the EA stamps on every template leg:
#   "ea:" + StringSubstr(trade_id, 0, 10) + <a|g> + <N>
# It is the ONLY link from a broker position back to the app's trade_id that
# survives into MT5's own position and deal records, and for every leg except
# the one that promoted the row it is the only link that exists at all --
# Python keeps one vantage_simulated_trades row per template trade, so sibling
# legs have no row and no ticket of their own on this side.
COMMENT_PREFIX = "ea:"
COMMENT_ID_LEN = 10


def comment_for_trade(trade_id: str) -> str:
    """The comment prefix every leg of `trade_id` will carry."""
    return f"{COMMENT_PREFIX}{(trade_id or '')[:COMMENT_ID_LEN]}"


def trade_id_prefix_from_comment(comment: str) -> Optional[str]:
    """Recover a trade_id's leading characters from a leg's order comment.

    "ea:5b88a61e-6g3" -> "5b88a61e-6". Returns None for any comment the EA
    did not write (broker-generated "[sl 4046.50]", "batchClose", blanks).
    Match the result with a prefix comparison, not equality -- it is only the
    first COMMENT_ID_LEN characters of the full trade_id.
    """
    if not comment or not comment.startswith(COMMENT_PREFIX):
        return None
    # The EA always writes exactly COMMENT_ID_LEN id characters before the leg
    # marker, so a slice is unambiguous where pattern-matching the marker off
    # the end is not: "ea:f4ef1085-aa1" is the id "f4ef1085-a" plus leg "a1",
    # and no regex can tell that from a shorter id without knowing the length.
    ident = comment[len(COMMENT_PREFIX):][:COMMENT_ID_LEN].strip()
    return ident or None


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


class EABridge:
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

    async def _push_panel_context(self) -> None:
        """Give the on-chart panel a template to act on as soon as the EA
        connects.

        Without this the panel starts blank and every click is refused with
        "no template in context" -- it only gained context once a template
        TRADE happened to open, which on a quiet channel could be hours.
        Observed immediately after the first panel deploy (2026-07-29):
        three button presses all rejected.

        Picks the template assigned to the panel's currently selected
        channel, since that is the one whose behaviour is live; falls back
        to any channel-assigned template, then to the most recently edited
        one, so the panel is still usable while a template is being set up.

        Also sends the channel roster (panel_context) so the CH tabs and the
        TELEGRAM / TG CMD lamps have something to show immediately, rather
        than only after the first template trade opens.
        """
        from backend.src.services.broker import ea_templates as _et
        from backend.src.db import database as db_module
        try:
            def _pick() -> Optional[dict]:
                assigned = db_module.get_all_channel_strategy_overrides() or {}
                # get_all_channel_strategy_overrides returns {"strategy",
                # "auto"}; get_all_channel_strategy_settings returns
                # {"strategy_override", ...}. Read both keys so this cannot
                # silently match nothing again if the caller is ever swapped
                # -- it was written against the wrong key originally, which
                # made this branch dead and sent the most-recently-edited
                # template every time regardless of what was assigned.
                names = set()
                for v in assigned.values():
                    ov = v.get("strategy") or v.get("strategy_override") or ""
                    if _et.is_template_override(ov):
                        names.add(_et.template_name_from_override(ov))
                rows = _et.list_ea_templates()
                if not rows:
                    return None
                for r in rows:
                    if r["name"] in names:
                        return r
                return max(rows, key=lambda r: r.get("updated_at") or 0)

            sel = await self._template_for_selected_slot()
            tpl = sel or await db_module.to_db_thread(_pick)
            if tpl:
                await self.push_template(tpl["name"], tpl)
            await self.push_panel_context()
        except Exception as e:
            log.debug("[EABridge] panel context push skipped: %s", e)

    # ── On-chart panel feeds ─────────────────────────────────────────────────
    #
    # The panel is a view plus a remote control, never an authority (see
    # _on_panel_action). These three pushes are the "view" half:
    #
    #   set_template   the left column -- every editable field, already sent
    #                  generically as tpl_<key>, so a field added to
    #                  core_ea_templates.DEFAULTS reaches the panel with no
    #                  protocol change and no EA recompile.
    #   panel_context  the CH tabs and link lamps (core_panel_context).
    #   panel_signal   the right column's ICT criteria and confidence score
    #                  (core_panel_signal). The EA computes the rest of that
    #                  column -- bid/ask/spread/P&L, the M5-D1 trend row, ATR,
    #                  session countdown, VWAP -- locally, because those need
    #                  a ticking clock and the chart's own series.
    #
    # All three are best-effort and silent when the EA is away: nothing here
    # is on a trading path, and a blank panel must never be able to raise
    # into the reader loop.

    async def _template_for_selected_slot(self) -> Optional[dict]:
        """The saved template assigned to the panel's selected CH tab, or
        None when that tab has no channel or its channel has no template."""
        from backend.src.services.broker import ea_templates as _et
        from backend.src.services.positions import core_panel_context as _pc
        from backend.src.db import database as db_module
        try:
            reader = getattr(self._engine, "_tg_reader", None)
            name = await db_module.to_db_thread(
                _pc.template_for_slot, reader, self._panel_slot)
            if not name:
                return None
            return await db_module.to_db_thread(_et.get_ea_template, name)
        except Exception as e:
            log.debug("[EABridge] slot template lookup failed: %s", e)
            return None

    async def push_panel_context(self) -> bool:
        from backend.src.services.positions import core_panel_context as _pc
        from backend.src.db import database as db_module
        if not self.is_ea_healthy():
            return False
        try:
            reader = getattr(self._engine, "_tg_reader", None)
            msg = await db_module.to_db_thread(
                _pc.build_context, reader, self._panel_slot)
        except Exception as e:
            log.debug("[EABridge] panel_context build failed: %s", e)
            return False
        return await self._send(msg)

    async def push_panel_signal(self) -> bool:
        from backend.src.services.positions import core_panel_signal as _ps
        if not self.is_ea_healthy():
            return False
        bridge = getattr(self._engine, "_bridge", None)
        if bridge is None:
            return False
        payload = await _ps.build_payload(bridge)
        msg: dict = {"type": "panel_signal"}
        for k, v in payload.items():
            if k == "levels":
                continue
            msg[k] = (1 if v else 0) if isinstance(v, bool) else v
        # Levels are flattened rather than nested: the EA's JSON reader is a
        # flat key scanner by design (see ForexTraderBridge.mq5's JsonGet*),
        # and giving it an array to parse would mean a real parser for one
        # optional display tab.
        for i, lv in enumerate(payload.get("levels") or [], start=1):
            msg[f"lvl{i}_price"] = lv.get("price")
            msg[f"lvl{i}_kind"]  = lv.get("kind")
            msg[f"lvl{i}_dir"]   = lv.get("dir")
        msg["level_count"] = len(payload.get("levels") or [])
        return await self._send(msg)

    async def push_panel_log(self, text: str) -> bool:
        """Append one line to the panel's RECENT SYSTEM LOGS strip.

        Fire-and-forget commentary for things that happen app-side and would
        otherwise be invisible at the terminal (a signal rejected, a template
        pushed). The EA keeps its own ring buffer and interleaves its own
        lines with these.
        """
        if not self.is_ea_healthy():
            return False
        return await self._send({"type": "panel_log", "text": str(text)[:120]})

    async def _panel_loop(self) -> None:
        """Keep the panel's read-only halves current while an EA is connected.

        Cadence is deliberately unequal. The signal payload is candle-derived
        and the mt5 bridge caches candles anyway, so 3s costs almost nothing
        and keeps the criteria row honest. The channel roster only changes
        when someone edits config, so 30s is generous.
        """
        ctx_every = 10          # every 10th signal push -> ~30s
        n = 0
        try:
            while True:
                await asyncio.sleep(3.0)
                if not self.is_ea_healthy():
                    continue
                try:
                    await self.push_panel_signal()
                    if n % ctx_every == 0:
                        await self.push_panel_context()
                except Exception as e:
                    log.debug("[EABridge] panel push failed: %s", e)
                n += 1
        except asyncio.CancelledError:
            raise

    def _ensure_panel_loop(self) -> None:
        if self._panel_task is None or self._panel_task.done():
            self._panel_task = asyncio.create_task(self._panel_loop())

    async def _on_panel_action(self, msg: dict) -> None:
        """A button was pressed on the EA's on-chart panel.

        The panel holds no authoritative state: it asks for a change, this
        applies it to the SAVED template, and the resulting set_template
        push is what actually moves the panel's display. That ordering is
        the point -- if the chart mutated its own copy, a chart click and
        an app edit could silently diverge, and the EA's copy would quietly
        win until the next restart.

        A "refresh" action carries no change and simply re-pushes, which is
        also how the panel repopulates after an EA reload.

        Three kinds of action arrive here:

          * a template FIELD name (anchors, lot_anchor, sl_pips, tp3_pips,
            trail_mode, ...) -- the generic path below, which works for any
            key in core_ea_templates.DEFAULTS with no code here per field.
          * select_channel -- moves the CH tab, which only changes WHICH
            template the panel edits.
          * an order action -- routed to the same app-side functions the UI
            and the Telegram bot use, so a trade started from the chart is
            risk-checked, tracked and logged identically to every other
            trade. The EA does the pip arithmetic (it owns _Point) and sends
            finished prices; this never re-derives them.
        """
        # Local imports: this module is pulled in early by the engine, and
        # importing database/templates at module scope reintroduces the
        # circular import the rest of this file already avoids the same way.
        from backend.src.services.broker import ea_templates as _et
        from backend.src.db import database as db_module
        action = (msg.get("action") or "").strip()
        value  = (msg.get("value") or "").strip()
        name   = (msg.get("template") or "").strip()

        if action == "select_channel":
            try:
                self._panel_slot = max(0, int(float(value)))
            except (TypeError, ValueError):
                return
            tpl = await self._template_for_selected_slot()
            if tpl:
                await self.push_template(tpl["name"], tpl)
            await self.push_panel_context()
            return

        if action in _ORDER_ACTIONS:
            await self._on_panel_order(action, msg, name)
            return

        if not name:
            log.info("[EABridge] panel_action %r ignored -- no template in context", action)
            return
        try:
            tpl = await db_module.to_db_thread(_et.get_ea_template, name)
            if not tpl:
                log.warning("[EABridge] panel_action for unknown template %r", name)
                return
            if action and action != "refresh":
                if action not in _et.DEFAULTS:
                    log.warning("[EABridge] panel_action unknown field %r", action)
                    return
                # The wire is all strings; coerce to whatever the field is.
                cur = _et.DEFAULTS[action]
                if isinstance(cur, bool):
                    tpl[action] = value not in ("0", "false", "False", "")
                elif isinstance(cur, int):
                    tpl[action] = int(float(value))
                elif isinstance(cur, float):
                    tpl[action] = float(value)
                else:
                    tpl[action] = value
                tpl = await db_module.to_db_thread(_et.save_ea_template, name, tpl)
                log.info("[EABridge] panel set %s=%s on template '%s'", action, value, name)
            await self.push_template(name, tpl)
        except Exception as e:
            log.warning("[EABridge] panel_action %r failed: %s", action, e)

    async def _on_panel_order(self, action: str, msg: dict, template: str) -> None:
        """Entry Management buttons: SELL / BUY / SELL LIMIT / BUY LIMIT /
        CANCEL LIMITS / CLOSE ALL.

        Every one of these goes through the app's normal order paths rather
        than being sent by the EA itself. The EA could place them in two
        lines -- it has CTrade right there -- but a trade that appears at the
        broker with no app-side row is invisible to the risk governor, to the
        monitor loop, to reporting, and to the fallback watchdog. The chart
        is a remote control for the app, not a second trader.

        The EA supplies finished prices (price/sl/tp1..tp5/lots), computed
        with its own _Point and the selected template's pip fields. Deriving
        them again here would mean a second pip convention to keep in step --
        see PipsToPrice()'s note in the EA about how easily that goes wrong.

        Outcomes are reported back as panel_log lines, since the terminal has
        no other way to learn that an order it asked for was refused.
        """
        engine = self._engine
        bridge = getattr(engine, "_bridge", None)
        if bridge is None:
            await self.push_panel_log("ORDER REFUSED: no MT5 bridge")
            return
        cfg = getattr(engine, "_cfg", {}) or {}
        starting = float(cfg.get("starting_balance", 1000.0))

        def _f(key: str) -> Optional[float]:
            v = msg.get(key)
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if f > 0 else None

        try:
            if action in ("market_buy", "market_sell"):
                from backend.src.services.trading.manual_market_order import (
                    open_manual_market_order)
                direction = "BUY" if action == "market_buy" else "SELL"
                res = await open_manual_market_order(
                    bridge, direction,
                    stop_loss=_f("sl"), lot_size=_f("lots"),
                    take_profit=_f("tp1"),
                    source_name="panel_manual",
                    starting_balance=starting,
                )
                await self.push_panel_log(
                    f"{direction} {res.get('lot_size', '')} @ "
                    f"{float(res.get('entry_price', 0) or 0):.2f}")

            elif action in ("limit_buy", "limit_sell"):
                from backend.src.services.trading.manual_limit_order import (
                    open_manual_limit_order)
                direction = "BUY" if action == "limit_buy" else "SELL"
                price = _f("price")
                sl    = _f("sl")
                if price is None or sl is None:
                    await self.push_panel_log("LIMIT REFUSED: needs price and SL")
                    return
                tps = [_f(f"tp{n}") for n in range(1, 6)]
                if not any(t is not None for t in tps):
                    await self.push_panel_log("LIMIT REFUSED: template has no TP1")
                    return
                res = await open_manual_limit_order(
                    bridge, direction,
                    entry_low=price, entry_high=price, stop_loss=sl,
                    tp1=tps[0], tp2=tps[1], tp3=tps[2], tp4=tps[3], tp5=tps[4],
                    lot_size=_f("lots"),
                    notes=f"chart panel ({template or 'no template'})",
                    starting_balance=starting,
                )
                await self.push_panel_log(
                    f"{direction} LIMIT {res.get('lot_size', '')} @ {price:.2f}")

            elif action == "close_all":
                # The runtime's own close command, not bot_trading.cmd_close:
                # that extraction was deleted (2847e32) while the live copy
                # stayed on the runtime, so the import upstream's panel used
                # raises ImportError here. Found 2026-08-26.
                out = await self._engine.close_cmd(["all"])
                await self.push_panel_log(out.splitlines()[-1] if out else "CLOSE ALL done")

            elif action == "cancel_limits":
                n = await self._cancel_all_working_pendings()
                await self.push_panel_log(f"CANCELLED {n} PENDING")
        except Exception as e:
            log.warning("[EABridge] panel order %s failed: %s", action, e)
            await self.push_panel_log(f"{action.upper()} FAILED: {e}")

    async def _cancel_all_working_pendings(self) -> int:
        """Pull every still-working pending order this app knows about.

        Deliberately driven off vantage_pending_orders rather than off
        OrdersTotal() in the terminal: the app's rows are the ones that would
        otherwise be left marked 'working' forever, and cancelling by trade_id
        routes through cancel_pending_order so the row is closed out properly.
        Orders placed outside this app are not ours to delete.
        """
        from backend.src.db import database as db_module

        def _rows() -> list[dict]:
            with db_module.db() as conn:
                return [db_module.row_to_dict(r) for r in conn.execute(
                    "SELECT * FROM vantage_pending_orders WHERE status='working'"
                ).fetchall()]

        rows = await db_module.to_db_thread(_rows)
        done = 0
        for row in rows:
            try:
                # ea_ticket, not mt5_ticket -- that is the column name on
                # vantage_pending_orders (see database.py's schema).
                if await self.cancel_pending_order(
                        row["trade_id"], int(row.get("ea_ticket") or 0),
                        "panel_cancel_limits"):
                    done += 1
            except Exception as e:
                log.warning("[EABridge] panel cancel %s failed: %s",
                            row.get("trade_id"), e)
        return done

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

        def _fetch():
            with db_module.db() as conn:
                return [
                    db_module.row_to_dict(r) for r in conn.execute(
                        "SELECT * FROM vantage_simulated_trades "
                        "WHERE status='open' AND managed_by='ea' "
                        "AND mt5_ticket IS NOT NULL AND mt5_ticket > 0"
                    ).fetchall()
                ]
        rows = await db_module.to_db_thread(_fetch)
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

    async def _dispatch(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "hello":
            log.info("[EABridge] EA hello: account=%s symbol=%s",
                     msg.get("account"), msg.get("symbol"))
            self._check_ea_version(msg)
            asyncio.create_task(self.push_global_config())
            asyncio.create_task(self._restore_pending_orders())
            # Re-adopt live positions too, not just resting orders -- see
            # restore_trade(). Without this every EA reload orphaned every
            # open template trade.
            asyncio.create_task(self._restore_open_trades())
            asyncio.create_task(self._push_panel_context())
            self._ensure_panel_loop()
        elif t == "ping":
            await self._send({"type": "pong"})
        elif t == "panel_action":
            await self._on_panel_action(msg)
        elif t in ("trade_opened", "trade_open_failed",
                   "pending_order_placed", "pending_order_open_failed"):
            cb = getattr(self, "_pending_open_acks", {}).get(msg.get("trade_id"))
            if cb:
                cb(msg)
            elif t == "trade_opened":
                # No waiting ack for this id. An EA Template Anchor leg reports
                # its immediate market fill as an unsolicited "trade_opened"
                # under "<trade_id>-a<N>" (HandleOpenTemplateGrid), while the
                # open_trade() call that started it all is waiting on the
                # un-suffixed parent id -- so this never matched a callback and
                # was silently dropped, leaving the parent row a permanent
                # mt5_ticket=0/entry_price=0 placeholder.
                _base, _kind, _num = split_leg_trade_id(msg.get("trade_id") or "")
                if _kind:
                    await self._promote_leg_fill(
                        msg.get("trade_id"), msg.get("ticket"),
                        float(msg.get("fill_price", 0) or 0),
                    )
                else:
                    log.debug("[EABridge] trade_opened with no waiting ack: %s",
                              msg.get("trade_id"))
        elif t == "tp_hit":
            await self._on_tp_hit(msg)
        elif t == "sl_moved":
            await self._on_sl_moved(msg)
        elif t == "trade_closed":
            await self._on_trade_closed(msg)
        elif t == "pending_order_filled":
            await self._on_pending_order_filled(msg)
        elif t == "pending_order_cancelled":
            await self._on_pending_order_cancelled(msg)
        elif t == "grid_leg_skipped":
            # The EA declined to place one of a grid's resting legs. Until
            # 2026-08-04 this only reached the terminal's own Experts log,
            # so a grid that placed its anchor and quietly lost its pending
            # leg was indistinguishable from one configured with no legs at
            # all -- the symptom that hid zone-mode's wrong-side skip.
            # WARNING rather than info: a leg the template asked for did
            # not reach the broker, which is always worth seeing.
            log.warning(
                "[EABridge] grid leg %s/%s NOT placed for trade=%s: %s "
                "(leg price %.2f vs base %.2f). wrong_side in zone mode now "
                "means price has left the zone ENTIRELY (a leg merely inside "
                "it is pulled back to the market side instead of skipped); "
                "beyond_sl means the template's grid_step_pts is wider than "
                "the signal's own entry-to-SL distance -- these are raw price "
                "deltas, so 20.0 on gold is $20.",
                msg.get("leg"), msg.get("of"), str(msg.get("trade_id", ""))[:8],
                msg.get("reason"), float(msg.get("price", 0) or 0),
                float(msg.get("base", 0) or 0),
            )
        else:
            log.debug("[EABridge] unhandled message type: %s", t)

    async def _resolve_leg_event(self, msg: dict, event: str) -> tuple:
        """Map an inbound EA event onto the vantage_simulated_trades row it
        belongs to, following EA Template leg suffixes.

        Returns (row, row_trade_id, label, owns_row):
          row           the DB row, or None if there is nothing to map onto
          row_trade_id  the id to write against (the un-suffixed parent for
                        a leg event)
          label         "" for a normal trade, else "Anchor Leg 1"/"Grid Leg 2"
          owns_row      True when the reporting leg IS the broker position
                        this row tracks (its mt5_ticket), so trade state may
                        be written. False for a sibling leg -- one row per
                        template trade means there is nowhere to record a
                        second concurrent position, and writing anyway
                        corrupts the tracked leg's lots/close state.
        """
        trade_id = msg.get("trade_id") or ""
        row = await self._fetch_trade(trade_id)
        if row:
            return (row, trade_id, "", True)
        base, kind, num = split_leg_trade_id(trade_id)
        if not kind:
            log.warning("[EABridge] %s for unknown trade_id=%s", event, trade_id)
            return (None, trade_id, "", False)
        row = await self._fetch_trade(base)
        if not row:
            log.warning("[EABridge] %s for unknown template leg trade_id=%s "
                        "(no parent row %s)", event, trade_id, base)
            return (None, base, leg_label(kind, num), False)
        ev_ticket  = int(msg.get("ticket") or 0)
        row_ticket = int(row.get("mt5_ticket") or 0)
        owns = (row_ticket == 0) or (ev_ticket == 0) or (ev_ticket == row_ticket)
        return (row, base, leg_label(kind, num), owns)

    async def _on_tp_hit(self, msg: dict) -> None:
        from backend.src.services.telegram import alerts as telegram_alerts
        trade_id = msg.get("trade_id")
        tp_num   = int(msg.get("tp_num", 0))
        price    = float(msg.get("price", 0))
        lots     = float(msg.get("lots_closed", 0))
        try:
            trade, trade_id, label, owns = await self._resolve_leg_event(msg, "tp_hit")
            if not trade:
                return
            if not owns:
                asyncio.create_task(telegram_alerts.send_message(
                    telegram_alerts.fmt_template_leg_note(
                        trade, label, f"TP{tp_num} Hit", [
                            f"MT5 Ticket: {msg.get('ticket')}",
                            f"TP{tp_num} price: ${price:.2f}",
                            f"Lots closed: {lots:.2f}",
                        ],
                    ),
                    trade_id, f"tp{tp_num}_hit_sibling_leg",
                ))
                return
            res = await self._engine.partial_close_trade(trade_id, lots, price, f"TP{tp_num}")
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_tp_hit(trade, tp_num, price, lots, res.get("partial_pnl", 0)),
                trade_id, f"tp{tp_num}_hit",
            ))
        except Exception as e:
            log.warning("[EABridge] tp_hit handling failed for %s: %s", msg.get("trade_id"), e)

    async def _on_sl_moved(self, msg: dict) -> None:
        from backend.src.db import database as db_module
        from backend.src.services.telegram import alerts as telegram_alerts
        trade_id = msg.get("trade_id")
        new_sl   = float(msg.get("new_sl", 0))
        # 1-based TP number that triggered this move, 0 if not tied to a
        # specific TP (a continuous trail) — reported by the EA itself since
        # only it knows which tp[] index fired. Previously hardcoded to 0
        # here, which displayed as the misleading "TP0 cleared" on every
        # EA-reported breakeven lock (confirmed live on ticket 1556988985).
        tp_cleared_num = int(msg.get("tp_cleared_num", 0) or 0)
        try:
            trade, trade_id, label, owns = await self._resolve_leg_event(msg, "sl_moved")
            if not trade:
                return
            if not owns:
                asyncio.create_task(telegram_alerts.send_message(
                    telegram_alerts.fmt_template_leg_note(
                        trade, label, "SL Moved", [
                            f"MT5 Ticket: {msg.get('ticket')}",
                            f"New SL: ${new_sl:.2f}",
                        ],
                    ),
                    trade_id, "sl_moved_ea_sibling_leg",
                ))
                return
            from backend.src.services.broker import repo as broker_repo
            await db_module.to_db_thread(broker_repo.set_stop_loss_be, trade_id, new_sl)
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_sl_moved(trade, tp_cleared_num, new_sl),
                trade_id, "sl_moved_ea",
            ))
        except Exception as e:
            log.warning("[EABridge] sl_moved handling failed for %s: %s", trade_id, e)

    async def _on_trade_closed(self, msg: dict) -> None:
        trade_id    = msg.get("trade_id")
        close_price = float(msg.get("close_price", 0))
        reason      = msg.get("reason", "EA_close")
        from backend.src.services.telegram import alerts
        try:
            trade, trade_id, label, owns = await self._resolve_leg_event(msg, "trade_closed")
            if not trade:
                return
            if not owns:
                asyncio.create_task(telegram_alerts.send_message(
                    telegram_alerts.fmt_template_leg_note(
                        trade, label, f"Closed ({reason})", [
                            f"MT5 Ticket: {msg.get('ticket')}",
                            f"Close price: ${close_price:.2f}",
                        ],
                    ),
                    trade_id, "ea_close_sibling_leg",
                ))
                return
            # A leg's close IS this trade's close when the leg owns the row's
            # ticket -- record it against the parent id, never the suffixed
            # one (which has no row of its own and previously made the whole
            # event a no-op, leaving the trade permanently "open" in the UI).
            result = await self._engine.record_close(trade_id, close_price, reason)
            account = await self._engine.get_mt5_account()
            closed_row = await self._fetch_trade(trade_id)
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_trade_close(closed_row, result, {}, account),
                trade_id, "ea_close",
            ))
            if int(closed_row.get("mt5_ticket") or 0):
                # Replace the entry-vs-exit estimate with the broker's own
                # realised figure once the deal history settles.
                # Through the engine's PUBLIC facade: upstream called
                # _schedule_profit_sync, and a service reaching into a runtime
                # private is what tests/core/test_runtime_facade.py stops. The
                # method was promoted and allowlisted instead, which is the
                # route that test names.
                asyncio.create_task(self._engine.schedule_profit_sync(
                    trade_id, int(closed_row["mt5_ticket"]),
                ))
        except Exception as e:
            log.warning("[EABridge] trade_closed handling failed for %s: %s", trade_id, e)
        finally:
            self._active.pop(trade_id, None)

    async def _on_pending_order_filled(self, msg: dict) -> None:
        """A resting Limit Runner order has filled — register it as a
        normal EA-managed trade, mirroring exactly what open_trade()'s own
        EA-ack branch does synchronously for a market order
        (core_open_trade.py), just deferred until the real broker fill
        instead of immediate. From here on this trade is indistinguishable
        from any other EA-managed trade — same vantage_simulated_trades
        row shape, same self._active tracking, same fallback-watchdog
        reclaim path if the EA later goes unhealthy."""
        from backend.src.db import database as db_module
        from backend.src.services.telegram import alerts as telegram_alerts
        trade_id   = msg.get("trade_id")
        ticket     = msg.get("ticket")
        fill_price = float(msg.get("fill_price", 0))
        try:
            row = await self._fetch_pending_order(trade_id)
            if not row:
                # EA Template grid legs (core_ea_templates.py / HandleOpenTemplateGrid
                # in the EA) never get a vantage_pending_orders row -- each leg is
                # tracked only in the EA's own g_pending[], keyed "<original
                # trade_id>-g<N>". Fall back to promoting the original open_trade()
                # placeholder row (mt5_ticket=0, entry_price=0.0) instead of
                # dropping the fill silently.
                if split_leg_trade_id(trade_id)[1]:
                    await self._promote_leg_fill(trade_id, ticket, fill_price)
                else:
                    log.warning("[EABridge] pending_order_filled for unknown trade_id=%s", trade_id)
                return
            tps = json.loads(row["tps_json"])
            now = time.time()

            from backend.src.services.broker import repo as broker_repo
            await db_module.to_db_thread(
                broker_repo.apply_pending_fill,
                trade_id, row, ticket, fill_price, tps, now,
            )
            self._active[trade_id] = {"ticket": ticket, "strategy": row["strategy"]}
            self._pending_orders.pop(trade_id, None)

            # Trading Schedule gate -- the resting order was accepted by the
            # broker before we could know whether the window's profit target
            # would still allow it by the time it actually filled. ORB/IVB is
            # exempt (its own once-a-day dedup already caps volume, and it's
            # never reached resolve_open_trade_params() by design); every
            # other pending-order strategy (Limit Runner today) must not add
            # risk once the target is hit. The fill already happened -- an
            # immediate real close is the only protective action left.
            if row["strategy"] != "orb_fixed":
                _sched_ok, _sched_reason = check_trading_schedule(source=row["channel_name"])
                if not _sched_ok and self._engine is not None:
                    try:
                        await self._engine.close_trade(trade_id, "trading_schedule_blocked")
                        asyncio.create_task(telegram_alerts.send_message(
                            f"Limit order filled then immediately closed — {_sched_reason}",
                            trade_id, "pending_order_filled_schedule_blocked",
                        ))
                    except Exception as e:
                        log.warning("[EABridge] schedule-blocked close failed for %s: %s", trade_id, e)
                    return

            asyncio.create_task(telegram_alerts.send_message(
                f"Limit order FILLED — {row['direction']} {row['lot_size']:g} lots @ "
                f"{fill_price:.2f} (ticket {ticket}), SL {row['stop_loss']:.2f}",
                trade_id, "pending_order_filled",
            ))
        except Exception as e:
            log.warning("[EABridge] pending_order_filled handling failed for %s: %s", trade_id, e)

    async def _promote_leg_fill(self, leg_trade_id: str, ticket, fill_price: float) -> None:
        """A leg of an EA Template trade (HandleOpenTemplateGrid in the EA)
        went live at the broker -- leg_trade_id is "<original trade_id>-g<N>"
        for a filled grid limit, or "<original trade_id>-a<N>" for an anchor
        leg's immediate market fill. The EA already has this trade fully in
        its own g_trades[] (isTemplate + tpl* fields copied over) and manages
        it correctly regardless of what happens here; this only updates
        Python's own record so the trade shows up as a real, trackable row
        instead of the permanent mt5_ticket=0 placeholder open_trade() wrote
        at template-open time.

        Anchor legs used to reach nothing at all: their fill arrives as an
        unsolicited "trade_opened" (not "pending_order_filled" -- an anchor
        is a market order, never a resting one) under the suffixed id, which
        matched no open_trade() ack callback and was dropped. The row then
        kept mt5_ticket=0/entry_price=0 for life, so the trade showed a $0
        entry in Active Trades, its EA-reported TP/SL/close events were all
        logged as "unknown trade_id" and discarded, and the Telegram close
        message quoted ticket 0, a $0 entry and a P&L computed from it
        (confirmed live 2026-07-29: -$16086 reported on a real -$15.63 loss).

        Only the first leg to go live can promote the row -- there is one
        vantage_simulated_trades row per template trade, and the SELECT below
        finds it via mt5_ticket=0, which only the not-yet-promoted
        placeholder has. With cancel_pending on (the common case) that's the
        only leg that ever fills anyway. With it off, a later leg's fill is a
        genuine second broker position with no DB row of its own -- reported
        via the same formatter, explicitly marked as an additional leg rather
        than pretending it's the trade's row."""
        from backend.src.db import database as db_module
        original_id, kind, num = split_leg_trade_id(leg_trade_id)
        label = leg_label(kind, num)
        now = time.time()
        # The EA reports no volume with a leg fill, and the row's lot_size is
        # Python's own pre-trade sizing -- for a template trade the EA sizes
        # each leg from the template's own Anchor/Pending Lot instead, so the
        # two genuinely differ (0.04 sized vs 0.03 filled, live 2026-07-29).
        # Take the broker's real volume for the promoted leg when we can read
        # it; keep the existing value if the bridge can't answer.
        lots = await self._leg_position_volume(ticket)

        def _apply():
            return broker_repo.claim_template_leg_fill(
                original_id, ticket, fill_price, lots, kind, now)

        # An anchor leg is a market order: the EA fills it and reports back
        # before open_trade() has INSERTed the row, because that INSERT only
        # happens once the EA's own parent ack returns -- and for a multi-leg
        # template that ack legitimately takes tens of seconds (10.5s observed
        # live on 2026-07-30). So a miss here is expected and temporary.
        #
        # The wait is deliberately NOT done inline: _handle_conn awaits
        # _dispatch directly, so blocking here stalls every subsequent EA
        # message AND _last_seen with it -- an inline 10s wait pushed
        # is_ea_healthy() past its 8s timeout and made three template
        # activations fail with "no healthy EA" on 2026-07-30. Hand the retry
        # to its own task and let the reader loop carry on.
        result = await db_module.to_db_thread(_apply)
        row, is_first = result if result else ({}, False)
        if not row:
            asyncio.create_task(self._promote_leg_when_row_exists(
                _apply, label, leg_trade_id, original_id, ticket, fill_price, lots))
            return
        await self._finish_leg_promotion(
            row, is_first, label, original_id, ticket, fill_price, lots)

    async def _promote_leg_when_row_exists(self, _apply, label: str, leg_trade_id: str,
                                           original_id: str, ticket, fill_price: float,
                                           lots) -> None:
        """Wait for open_trade()'s INSERT, then promote the leg.

        Runs as its own task so the EA reader loop keeps draining messages and
        keeps the heartbeat fresh while this waits -- see the call site.

        The budget covers core_open_trade's own ack timeout (capped at 60s)
        plus room for the INSERT that follows it, since the row cannot appear
        until that ack returns. Overshooting costs nothing here; giving up too
        early leaves the row for core_template_placeholder_repair to adopt on
        its next poll, which works but is slower and noisier.
        """
        from backend.src.db import database as db_module
        deadline = time.time() + _LEG_ROW_WAIT_S
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            result = await db_module.to_db_thread(_apply)
            row, is_first = result if result else ({}, False)
            if row:
                await self._finish_leg_promotion(
                    row, is_first, label, original_id, ticket, fill_price, lots)
                return
        log.warning("[EABridge] %s filled (trade_id=%s) but no trade row appeared "
                    "for original trade_id=%s within %.0fs — leaving it to "
                    "TemplateRepair",
                    label, leg_trade_id, original_id, _LEG_ROW_WAIT_S)

    async def _finish_leg_promotion(self, row: dict, is_first: bool, label: str,
                                    original_id: str, ticket, fill_price: float,
                                    lots) -> None:
        """Everything that happens once a leg fill has been matched to its row,
        shared by the immediate and the deferred paths."""
        from backend.src.services.telegram import alerts
        log.info("[EABridge] %s live: trade=%s ticket=%s @ %.2f lots=%s (%s)",
                 label, original_id[:8], ticket, fill_price, lots or row.get("lot_size"),
                 "promoted this trade's row" if is_first else "sibling leg, row already promoted")

        if is_first:
            self._active[original_id] = {"ticket": ticket, "strategy": row["strategy"]}

            # Trading Schedule gate -- same reasoning as _on_pending_order_
            # filled's own check: the leg was accepted by the broker before
            # we could know whether the window's profit target would still
            # allow it by fill time, and the only protective action left is
            # an immediate real close. Only meaningful for the promoted row
            # -- a later leg has no DB-tracked ticket this app could close.
            _sched_ok, _sched_reason = check_trading_schedule(source=row.get("tg_source") or "")
            if not _sched_ok and self._engine is not None:
                try:
                    await self._engine.close_trade(original_id, "trading_schedule_blocked")
                    asyncio.create_task(telegram_alerts.send_message(
                        f"EA Template {label} filled then immediately closed — {_sched_reason}",
                        original_id, "template_leg_filled_schedule_blocked",
                    ))
                except Exception as e:
                    log.warning("[EABridge] schedule-blocked close failed for %s: %s", original_id, e)
                return

        asyncio.create_task(telegram_alerts.send_message(
            telegram_alerts.fmt_leg_fill(row, label, ticket, fill_price, lots, is_first),
            original_id, "template_leg_filled",
        ))

    async def _leg_position_volume(self, ticket) -> Optional[float]:
        """The broker's own volume for a just-filled leg ticket, or None if it
        can't be read (bridge offline, position already gone). Best-effort
        only -- never blocks promoting the row."""
        try:
            if not ticket or self._engine is None:
                return None
            positions = await self._engine._bridge.get_positions() or []
            for p in positions:
                if int(p.get("ticket", 0) or 0) == int(ticket):
                    vol = round(float(p.get("volume", 0) or 0), 4)
                    return vol or None
        except Exception as e:
            log.debug("[EABridge] leg volume lookup failed for ticket=%s: %s", ticket, e)
        return None

    async def _on_grid_leg_cancelled(self, leg_trade_id: str, reason: str) -> None:
        """Counterpart to _promote_leg_fill for a leg that never fills.

        grid_legs_total (2026-08-03, core_open_trade.py -- the EA's own
        trade_opened ack now carries legs_placed) is the one piece of grid
        shape Python can actually know here; without it, this used to have
        no way to tell "one sibling of several cancelled, others may still
        fill" apart from "every leg this grid ever had is now gone with none
        filled" and always assumed the former -- confirmed live 2026-08-03:
        two single-leg grids (no anchor, price outside the zone at signal
        time) each had their only resting leg expire unfilled and sat in
        Active Trades for 5+ hours at a fabricated ~$16,132 unrealised P&L
        (the (current - 0) * lots arithmetic every $0-entry row produces).

        grid_legs_total is None for a row from a synthetic ack-timeout
        placeholder (core_open_trade.py never guesses a leg count there,
        since the EA may genuinely have placed legs Python never heard
        about) -- this still can't safely close in that case, so it falls
        back to the old surface-and-wait behaviour."""
        from backend.src.services.telegram import alerts
        original_id = split_leg_trade_id(leg_trade_id)[0]
        row = await self._fetch_trade(original_id)
        if not row:
            log.debug("[EABridge] grid leg %s cancelled (%s) — no placeholder row (already "
                      "closed?)", leg_trade_id, reason)
            return
        if row["status"] != "open" or int(row["mt5_ticket"] or 0) != 0:
            # Another leg already filled and promoted this row, or it's
            # since been closed -- a losing sibling leg cancelling now is
            # expected and harmless.
            return
        log.warning("[EABridge] grid leg %s cancelled (%s) — trade=%s still has no filled "
                    "leg (mt5_ticket=0)", leg_trade_id, reason, original_id[:8])

        total = row.get("grid_legs_total")
        cancelled = await self._incr_grid_leg_cancelled(original_id)
        # `total == 0` is a confirmed "this grid placed nothing" -- not the
        # same as `total is None` ("unknown, don't touch"). `if total and`
        # treated both identically, which mattered in practice: with
        # core_open_trade.py now refusing to insert a row at all when the
        # EA's ack reports 0 legs placed, this branch of 0 should be
        # unreachable going forward, but keep the check correct regardless.
        if total is not None and cancelled >= int(total):
            row = await self._fetch_trade(original_id)  # re-check post-increment
            if row and row["status"] == "open" and int(row["mt5_ticket"] or 0) == 0:
                await self._close_dead_grid_placeholder(row, reason)
            return

        asyncio.create_task(telegram_alerts.send_message(
            f"EA Template grid leg not filled — {row['direction']} {row.get('tg_source', '')} "
            f"({reason}). Other legs may still be resting; this trade stays open at $0 until "
            f"one fills or you close it manually.",
            original_id, "template_grid_leg_cancelled",
        ))

    async def _incr_grid_leg_cancelled(self, trade_id: str) -> int:
        """Atomically bump grid_legs_cancelled and return the new count."""
        from backend.src.db import database as db_module

        def _apply():
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_simulated_trades SET grid_legs_cancelled=grid_legs_cancelled+1 "
                    "WHERE trade_id=?",
                    (trade_id,),
                )
                row = conn.execute(
                    "SELECT grid_legs_cancelled FROM vantage_simulated_trades WHERE trade_id=?",
                    (trade_id,),
                ).fetchone()
                return int(row[0]) if row else 0
        return await db_module.to_db_thread(_apply)

    async def _close_dead_grid_placeholder(self, row: dict, reason: str) -> None:
        """Every leg this grid ever placed has now cancelled unfilled -- no
        broker position was ever opened, so close the $0-entry placeholder
        via record_close() rather than leaving it a permanent ghost in
        Active Trades. record_close's own entry_price==0 guard (see
        core_close_trade.py) already stops this from fabricating a P&L
        figure from a zero entry, same as core_template_placeholder_repair
        relies on for its own close path."""
        from backend.src.services.telegram import alerts
        from backend.src.services.trading.close_trade import CloseTradeContext, record_close

        trade_id = row["trade_id"]
        bridge = getattr(self._engine, "_bridge", None) if self._engine is not None else None
        if bridge is None:
            log.warning("[EABridge] grid trade=%s has no filled leg left to wait for, but no "
                        "trading bridge is available to close it via — leaving it open",
                        trade_id[:8])
            return
        try:
            ctx = CloseTradeContext(bridge)
            await record_close(trade_id, 0.0, "no_fill_expired", ctx)
        except Exception as e:
            log.warning("[EABridge] failed to close dead grid placeholder trade=%s: %s",
                        trade_id[:8], e)
            return
        log.warning(
            "[EABridge] grid trade=%s closed — every leg (%s total) cancelled (%s) with none "
            "filled, no broker position was ever opened",
            trade_id[:8], row.get("grid_legs_total"), reason,
        )
        asyncio.create_task(telegram_alerts.send_message(
            f"EA Template grid — every leg for {row['direction']} {row.get('tg_source', '')} "
            f"expired/cancelled with none filled. Closing the placeholder (no position was "
            f"ever opened, no P&L).",
            trade_id, "template_grid_no_fill",
        ))

    async def _on_pending_order_cancelled(self, msg: dict) -> None:
        """A resting Limit Runner order was removed from the broker's book
        without filling — either it expired (expire_minutes elapsed, same
        4h default as the Python-simulated zone-wait signals' own pending
        expiry) or was cancelled manually in the terminal; the EA can't
        reliably distinguish the two from a bare "order gone, no matching
        position" observation, so `reason` is best-effort, not authoritative."""
        from backend.src.db import database as db_module
        from backend.src.services.telegram import alerts as telegram_alerts
        trade_id = msg.get("trade_id")
        reason   = msg.get("reason", "cancelled")
        try:
            row = await self._fetch_pending_order(trade_id)
            if not row:
                # EA Template grid legs never get a vantage_pending_orders row
                # (see _on_pending_order_filled's identical fallback) -- without
                # this, a leg that never fills leaves the open_trade() placeholder
                # (mt5_ticket=0) permanently invisible: nothing ever updates it,
                # nothing ever alerts, and it sits in Active Trades at $0 until
                # someone notices and cleans it up by hand (confirmed live,
                # trade eb8ca404, sat orphaned 2026-07-28 to 2026-07-29).
                if split_leg_trade_id(trade_id)[1]:
                    await self._on_grid_leg_cancelled(trade_id, reason)
                else:
                    log.warning("[EABridge] pending_order_cancelled for unknown trade_id=%s", trade_id)
                return
            now = time.time()

            from backend.src.services.broker import repo as broker_repo
            await db_module.to_db_thread(
                broker_repo.apply_pending_cancelled, trade_id, row["signal_id"], now)
            self._pending_orders.pop(trade_id, None)
            asyncio.create_task(telegram_alerts.send_message(
                f"Limit order not filled — {row['direction']} @ {float(row['price']):.2f} "
                f"{reason} before price reached the zone.",
                trade_id, "pending_order_cancelled",
            ))
        except Exception as e:
            log.warning("[EABridge] pending_order_cancelled handling failed for %s: %s", trade_id, e)

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
