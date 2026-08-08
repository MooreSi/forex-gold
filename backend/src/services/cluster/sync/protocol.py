"""
Local/Remote node sync protocol — WebSocket messages between the Mac (client)
and the VPS (server) for the dual-instance feature.

Deliberately separate from backend.src.services.cluster.remote, which is a hub-and-spoke
licence/admin/update-distribution protocol for many customer installs
connecting to a central server. This is a 1:1 peer link between two
instances of the same person's own app: the VPS (fixed IP, always-on,
default active trader) and the Mac (opt-in takeover).

All JSON messages follow: {"type": MSG_TYPE, ...extra fields}
Binary frames are used only for the model-snapshot transfer.
"""

# ── Handshake ────────────────────────────────────────────────────────────────
MSG_HELLO           = "hello"            # Mac -> VPS: token, first message after connect
MSG_WELCOME          = "welcome"          # VPS -> Mac: accepted, sends current settings + status
MSG_REJECT           = "reject"           # VPS -> Mac: bad token

# ── Heartbeat / status ───────────────────────────────────────────────────────
MSG_STATUS_HEARTBEAT = "status_heartbeat" # VPS -> Mac: balance, equity, open positions, engine states
MSG_PING              = "ping"
MSG_PONG              = "pong"

# Full signal-generator stats (win rates, performance breakdowns, ML learning
# panels) for the Breakout/Bounce/Reversal Engines — sent far less often than
# the 3s status heartbeat since this data only changes when a signal closes
# or a periodic ML retrain happens. Deliberately excludes cycle logs (raw
# per-cycle diagnostic dumps, not a "progress" metric).
MSG_SIGNAL_GEN_STATS = "signal_gen_stats"   # VPS -> Mac: {"breakout": {...}, "bounce": {...}, "reversal_engine": {...}}

# ── Settings sync (VPS is authoritative) ─────────────────────────────────────
MSG_SETTINGS_PROPOSE = "settings_propose" # Mac -> VPS: "please apply this change"
MSG_SETTINGS_STATE   = "settings_state"   # VPS -> Mac: full confirmed settings snapshot
MSG_SETTINGS_REJECTED = "settings_rejected"  # VPS -> Mac: propose failed, with reason

# AI provider config (config.yaml, not vantage_risk_settings — a completely
# separate storage layer, so it needs its own sync path rather than joining
# MSG_SETTINGS_PROPOSE above). Deliberately NOT bundled with the credential-
# free settings sync: this one carries the DeepSeek API key too, an explicit
# user opt-in (2026-07-08) rather than the default "never sync credentials"
# stance the settings sync above was built with. One-directional (Mac -> VPS
# only) since the Mac's Settings > AI page is where this actually gets
# edited; fire-and-forget, no reject/ack — there's no legitimate rejection
# case, just plain values being mirrored.
MSG_AI_CONFIG_SYNC = "ai_config_sync"  # Mac -> VPS: mirror ai_provider/model/deepseek_api_key

# Per-channel Telegram strategy overrides (channel_performance table) — same
# propose/confirm pattern as the settings messages above, but keyed by
# channel rather than being one flat dict, so it gets its own message pair.
MSG_CHANNEL_STRATEGY_PROPOSE = "channel_strategy_propose"  # Mac -> VPS
MSG_CHANNEL_STRATEGY_STATE   = "channel_strategy_state"    # VPS -> Mac: full confirmed snapshot

# Trading Schedule (app_config "trading_schedule"/"trading_schedule_enabled",
# not vantage_risk_settings — a separate storage layer like AI config above,
# so it needs its own pair rather than joining MSG_SETTINGS_PROPOSE). One
# combined snapshot ({"enabled": bool, "schedule": {...}}) per message, same
# shape as _channel_strategy_snapshot's single-dict bundle, since the whole
# 7-day/3-window config is edited and saved as one atomic unit in the UI —
# no per-key filtering/rejection case exists here, so no REJECTED variant.
MSG_TRADING_SCHEDULE_PROPOSE = "trading_schedule_propose"  # Mac -> VPS
MSG_TRADING_SCHEDULE_STATE   = "trading_schedule_state"    # VPS -> Mac: full confirmed snapshot

# Strategy Parameters (app_config "strategy_params_<strategy>" per fixed-
# parameter strategy -- Conservative/Scalp Runner/Reversal Runner/Adaptive
# Runner/Adaptive Runner 2's live SL/TP values, core_strategy_params.py) --
# same one-combined-snapshot-per-message shape as Trading Schedule above:
# all 5 strategies' values are proposed/confirmed together, not as
# separate per-strategy messages, so two near-simultaneous edits to
# different strategies can't race each other. The named-template library
# (strategy_param_templates table) is NOT synced -- each node keeps its
# own saved presets.
MSG_STRATEGY_PARAMS_PROPOSE = "strategy_params_propose"  # Mac -> VPS
MSG_STRATEGY_PARAMS_STATE   = "strategy_params_state"    # VPS -> Mac: full confirmed snapshot

# ── Trading mutual exclusion (Option A: block switch-back on open local positions) ──
MSG_STAND_DOWN        = "stand_down"        # Mac -> VPS: taking over, stop opening new trades
MSG_STAND_DOWN_ACK    = "stand_down_ack"    # VPS -> Mac: stood down, here is my open-position summary
MSG_RESUME            = "resume"            # Mac -> VPS: handing control back
MSG_RESUME_ACK        = "resume_ack"        # VPS -> Mac: resumed accepting new trades

# ── Remote engine control (Signal Generator panels, Remote mode) ────────────
# When this node is in Remote mode, the Start/Stop/Run Now buttons on the
# Breakout/Bounce/Reversal Engine panels must act on the VPS's own engine instance,
# not this node's own (stood-down) local one — otherwise "Start Engine"
# silently does nothing useful while looking like it worked.
MSG_ENGINE_CONTROL     = "engine_control"      # Mac -> VPS: {"engine": "reversal_engine", "action": "start"/"stop"/"run_now"}
MSG_ENGINE_CONTROL_ACK = "engine_control_ack"  # VPS -> Mac: {"engine", "action", "is_running", "error"?}

# Same problem as engine control above, for the Trading tab's manual "Market
# Order" button — clicking it while stood down previously just failed with
# "Trading stood down", even though the user explicitly asked for a trade
# right now. Routes the request to the VPS's own open_manual_market_order().
MSG_MARKET_ORDER     = "market_order"      # Mac -> VPS: {"direction","stop_loss","lot_size","strategy"}
MSG_MARKET_ORDER_ACK = "market_order_ack"  # VPS -> Mac: {"result": {...}} or {"error": "..."}

# Centralized signal generation (Settings > Remote Node > "Generate signals on
# this node only") — a fully-resolved trade decision from one of the Mac's own
# generators (Breakout/TestSignal/REopy/GD2-GD-VIP), forwarded to the VPS for
# execution because the VPS has stopped analyzing anything itself under this
# mode (see core.database.should_generate_signals_here). Unlike MSG_MARKET_ORDER
# (a raw manual order), this carries open_trade()'s full parameter set — the
# Mac has already resolved risk sizing, session gates, and SL/TP logic before
# this is sent, so the VPS calls open_trade() directly, not open_trade_from_signal.
MSG_SIGNAL_ORDER     = "signal_order"      # Mac -> VPS: full open_trade() payload from a generator
MSG_SIGNAL_ORDER_ACK = "signal_order_ack"  # VPS -> Mac: {"result": {...}} or {"error": "..."}

# Instant Market Entry follow-up — when centralized_signal_gen_enabled and the
# VPS is the active trader, an IME trade opened via a forwarded MSG_SIGNAL_ORDER
# only exists in the VPS's own vantage_simulated_trades, never the Mac's (see
# open_trade()'s forwarding branch). The Mac's own "is there an open instant
# trade to apply this follow-up to" query was always querying its own (empty)
# table and never finding a match, so every IME follow-up fell through to
# opening a second, independent trade instead of updating the first — the VPS
# actually placed two real MT5 orders for one signal. This message asks the
# VPS to resolve and apply the follow-up against its own authoritative table.
MSG_SIGNAL_FOLLOWUP     = "signal_followup"      # Mac -> VPS: {"channel_name","direction","tg_id","updates":{...}}
MSG_SIGNAL_FOLLOWUP_ACK = "signal_followup_ack"  # VPS -> Mac: {"matched": bool, "error"?: "..."}

# ── Consolidated trade ledger ────────────────────────────────────────────────
MSG_TRADE_CLOSED      = "trade_closed"      # either side -> other: a trade closed, append to ledger
MSG_LEDGER_PULL        = "ledger_pull"       # Mac -> VPS: "send me your full ledger" (periodic refresh)
MSG_LEDGER_PUSH        = "ledger_push"       # VPS -> Mac: full ledger snapshot

# ── Learned parser rules (Telegram > Reader Logic > AI tab) ─────────────────
# Each node runs its own Telethon session and parses every message against its
# own local channel_learned_rules — so without this, approving an AI-recovered
# signal on one node only taught that node the new format; the other kept
# paying for an AI fallback call (and needing its own separate approval) for
# every future message of the same shape.
MSG_LEARNED_RULE_SYNC = "learned_rule_sync"  # either side -> other: mirror a newly-approved parser rule

# ── AI-recovered signal review queue (Telegram > Reader Logic > AI tab) ──────
# MSG_LEARNED_RULE_SYNC above only mirrors the END RESULT of a review (the
# generated parser rule). The review QUEUE ITSELF — ai_recovered_signals —
# was still node-local: only the active-trader node populates new entries
# (SimulationEngine._is_active_trader_node gates the paid AI call), so the
# passive node's queue always looked empty, and anything created before a
# failover became invisible/unreachable from whichever node is active now.
# One shared source of truth, keyed by tg_message_id (UNIQUE in the table):
MSG_AI_RECOVERED_SIGNAL_SYNC = "ai_recovered_signal_sync"  # either side -> other: mirror created/approved/rule_result/discarded
MSG_AI_RECOVERED_PULL = "ai_recovered_pull"  # Mac -> VPS: "send me your full unresolved queue" (periodic refresh)
MSG_AI_RECOVERED_PUSH = "ai_recovered_push"  # VPS -> Mac: full unresolved-queue snapshot

# ── Model snapshot transfer (manual, on-demand only — never automatic) ───────
MSG_MODEL_SNAPSHOT_REQUEST = "model_snapshot_request"  # requester -> other: "send me your models"
MSG_MODEL_SNAPSHOT_UPLOAD  = "model_snapshot_upload"   # uploader -> other: "here are my models, unprompted"
MSG_MODEL_SNAPSHOT_BEGIN   = "model_snapshot_begin"    # sender -> receiver: manifest of files to follow
MSG_MODEL_SNAPSHOT_END     = "model_snapshot_end"      # sender -> receiver: transfer complete

# Connection states surfaced to the UI (not wire messages — used by client.py)
CONN_DISCONNECTED = "disconnected"
CONN_CONNECTING   = "connecting"
CONN_CONNECTED    = "connected"
CONN_REJECTED     = "rejected"

# active_trader values (stored in app_config on both sides)
TRADER_LOCAL      = "local"
TRADER_REMOTE_VPS = "remote_vps"


def make(type_: str, **kwargs) -> dict:
    return {"type": type_, **kwargs}
