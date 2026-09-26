"""Which risk settings travel between the paired nodes, and which never do.

`SYNCED_SETTINGS_KEYS` is what the VPS applies from a Mac's proposal and what
its confirmed snapshot carries. It is an explicit list rather than "every
column" because a proposal arrives off the network and its keys become SQL
column names (see utils/sql_identifiers.py): the list is the gate.

`PER_NODE_SETTINGS` is the other half. Every column of vantage_risk_settings
is in exactly one of the two, and tests/core/test_sync_covers_every_setting.py
fails the suite on a column that is in neither. Moved out of server.py, with
its comments verbatim, on 2026-09-25 when the list was completed.
"""
from __future__ import annotations


# Settings that must differ per machine. Each needs its reason.
PER_NODE_SETTINGS: frozenset[str] = frozenset({
    # One-way on purpose: the VPS adopts the Mac's UTC offset from its pings
    # (_server_peer_data._apply_peer_clock_offset). As a synced setting the
    # broadcast would push the VPS's value back and the Mac would run on its
    # server's clock.
    "trading_clock_offset_min",
})


SYNCED_SETTINGS_KEYS = (
    "risk_governor_enabled", "risk_per_trade_pct", "max_risk_per_trade_pct",
    "max_lot_size", "max_daily_loss_pct", "max_total_drawdown_pct",
    "cooldown_after_loss_min", "trade_strategy",
    "session_asia_enabled", "session_london_enabled", "session_ny_enabled",
    "accept_tg_signals", "auto_execute_signals", "exclude_high_risk",
    # The Signal Decision Log (2026-09-18). Synced for the same reason its
    # Parsing-page neighbours above are: left per-node, the Mac and the VPS
    # would record different halves of one study and nothing would say so.
    "tg_decision_log_enabled",
    # The contradiction study (2026-09-21). Same reason, and one more: with
    # it on, each node writes its own Telegram signals to its own signal
    # bus, so a node where it is off would be judging contradictions
    # against half the evidence.
    "tg_contradiction_log_enabled",
    "bo_live_execution", "bo_claude_eval_enabled", "kelly_sizing_enabled",
    "max_open_trades",
    # sg_claude_eval_enabled (Bounce Generator's own Claude-eval toggle) was
    # missing here entirely — its Breakout Engine sibling (bo_claude_eval_enabled,
    # above) was already synced, but this one wasn't, found investigating a
    # sudden token-usage spike 2026-07-06 traced to both engines' Claude
    # evaluation defaulting ON and running independently on each node.
    "sg_claude_eval_enabled",
    # Circuit breaker fields (settings.py's save_risk()) were never added
    # here when that feature shipped, so every circuit-breaker change from
    # either node was silently filtered out and rejected by the other side
    # — not a timing issue, a deterministic naming gap.
    "circuit_breaker_enabled", "circuit_breaker_losses", "circuit_breaker_cooldown_mins",
    "profit_close_usd",
    # circuit_breaker_active_until / circuit_breaker_consec_losses (the
    # breaker's own runtime state, set by database.py's trip/reset logic
    # rather than the Settings UI) were never added alongside the three
    # circuit_breaker_* config fields above — same "no recognised settings
    # keys in proposal" rejection on every reconnect, found 2026-07-11.
    # Syncing this state (not just the policy) matters here specifically
    # because a trip on the active node must still be honored if failover
    # hands trading to the other node mid-cooldown.
    "circuit_breaker_active_until", "circuit_breaker_consec_losses",
    # Also missing (found investigating a "IME didn't fire" report 2026-07-06 —
    # turned out not to be the actual cause that time, both nodes already
    # agreed, but it's a real latent gap for the next time either side
    # changes it without the other noticing).
    "immediate_market_entry",
    # EA bridge is per-node infrastructure (each node has its own attached
    # MT5 terminal + EA), but the ON/OFF *policy* should be shared like every
    # other risk toggle above — otherwise whichever node becomes active
    # later silently reverts to Python-only management because this node's
    # own DB never learned the setting was turned on elsewhere.
    "ea_bridge_enabled",
    # Same gap, found 2026-07-07: Dynamic Position Management was never
    # added here either, so toggling it on one node silently never reached
    # the other — whichever node takes over trading later would manage
    # positions without DPM even though the user explicitly turned it on.
    "dpm_enabled",
    # Same gap again, found 2026-07-07: Reversal Engine's live-execution
    # toggle was never added here, so every propose from either node was
    # silently rejected outright ("no recognised settings keys in
    # proposal") — the two nodes could show opposite ON/OFF states
    # indefinitely with no error surfaced to the user.
    "re_live_execution",
    # Same gap again, found 2026-07-07: five more fields saved by the same
    # Strategy panel save_strategy() call as trade_strategy/profit_close_usd/
    # exclude_high_risk/kelly_sizing_enabled (all already synced above) were
    # never added here, so the whole settings proposal was rejected outright
    # ("no recognised settings keys in proposal") on every strategy-panel
    # save — trail_stop_sl_pts, trailing_stop_distance and strategy_lot_size
    # feed live SL/TP and lot-size math in engine.py, atr_collapse_threshold
    # gates entries in breakout_signal/engine.py and test_signal/engine.py,
    # and display_strategy_id is the display companion of trade_strategy
    # (set together in engine.py's !strategy command) — a mismatch there
    # leaves the picker on the other node highlighting the wrong strategy
    # even when the executed trade_strategy itself is correct.
    "trail_stop_sl_pts", "trailing_stop_distance", "strategy_lot_size",
    "atr_collapse_threshold", "display_strategy_id",
    # Trading > Global Parameters (2026-07-24) -- strategy_lot_size (above)
    # moved here from Active Strategy; these three are new. Added up front
    # this time rather than found later as a gap, per the recurring pattern
    # every entry above this one already documents.
    "strategy_lot_size_grid", "global_harvest_enabled", "global_harvest_threshold_usd",
    "hour_blocklist_enabled",
    # Per-trade sizing (docs/todo/risk/010). Whichever node trades must size
    # the way the operator chose, whichever node they chose it on.
    "global_sizing_override", "strategy_lot_size_parked",
    # ORB/IVB Report's auto-execute toggle — whichever node ends up as the
    # active trader is the one whose scheduler actually checks this flag, so
    # toggling it from the other node's UI must reach it or the setting is
    # silently a no-op on the node that matters.
    "orb_auto_execute_enabled",
    # Bounce Generator's live-execution toggle (its sibling toggles,
    # bo_live_execution for Breakout and re_live_execution for Reversal Engine,
    # were both already synced) — same deterministic naming gap as those
    # two, found 2026-07-10 investigating a "no recognised settings keys
    # in proposal" rejection on every Mac reconnect.
    "sg_live_execution",
    # ORB/IVB Report's lot size — same reasoning as orb_auto_execute_enabled
    # above: whichever node executes (manually or via the scheduler) needs
    # the value the user actually set, regardless of which node's UI they
    # set it from.
    "orb_lot_size",
    # Centralized signal generation toggle — whichever node ends up the
    # active trader must agree with the Mac on whether it should be
    # analyzing at all (should_generate_signals_here() reads this locally on
    # each node), so it needs to reach both sides like every other execution
    # -affecting flag above, not just live on whichever node's UI set it.
    "centralized_signal_gen_enabled",
    # Trading > Strategy > Internal Engine Exposure (2026-07-28) -- added up
    # front rather than found later as a gap, per the recurring pattern
    # documented throughout this list. Whichever node is the active trader is
    # the one whose internal engines actually consult this before executing,
    # so it has to reach both sides like every other execution-affecting flag.
    "internal_hedge_mode", "internal_net_exposure_max_lots",
    # Signal Generator > Reversal > Learn From Pro Signals (2026-08-06) --
    # added up front, same reasoning as the entries above. Whichever node
    # runs the Reversal Engine is the one that reads this when scoring a
    # signal, so a toggle set on the Mac has to reach the VPS or the engine
    # there keeps scoring with pro_likeness pinned at its neutral.
    "re_learn_from_ref_signals",
    # Trading > Which signals are taken / Exposure -- the stale-release
    # guards (2026-09-21). Same reasoning as every execution-affecting flag
    # above, and it applies with particular force here: the pending watcher
    # runs on whichever node is the active trader, so a guard switched on
    # from the Mac that never reached the VPS would leave the backlog
    # releasing exactly as it did on the day these were written.
    "stale_better_fill_cap_enabled", "stale_better_fill_cap_pts",
    "pending_momentum_gate_enabled", "burst_hedge_guard_enabled",
    # Everything else (owner, 2026-09-25): "sync everything except the
    # per-machine list". Until then this list grew one found gap at a time,
    # and these ~65 were still missing: the Mac had 38 changes queued that the
    # VPS silently dropped, including setforget_lot_size, the give-back guard
    # and every Telegram parsing switch. tests/core/test_sync_covers_every_
    # setting.py now fails on any column that is in neither list.
    "max_pending_signals", "default_lot_size", "require_sl_and_tp", "require_at_least_tp1",
    "allow_no_sl", "move_sl_to_be_after_tp1", "pause_after_losses", "dpm_be_trigger_usd",
    "dpm_trail_distance", "dpm_tp1_partial_pct", "ooh_enabled", "ooh_start_time",
    "ooh_end_time", "ooh_strategy", "ooh_date_from", "ooh_date_to", "ooh_date_active",
    "unattended_mode", "lk_enable_tp_parsing", "lk_enable_sl_parsing", "lk_enable_close_all_parsing",
    "lk_enable_risk_free_be_parsing", "lk_enable_tp_hit_parsing", "lk_ignore_media_messages",
    "lk_ignore_forwarded_messages", "re_use_limit_order", "lk_entry_realignment",
    "lk_enable_mirror_copy", "lk_queue_closed_market_limits", "lk_enable_second_message_tp_sl",
    "lk_second_message_match_window_sec", "re_require_ref_confirmation", "re_ref_confirmation_window_min",
    "lk_fallback_sl_pips", "giveback_guard_enabled", "giveback_arm_usd", "giveback_pct",
    "ooh_timezone", "htf_bias_gate_enabled", "min_fill_delay_enabled", "min_fill_delay_s",
    "resting_revalidation_enabled", "re_atr_barriers_enabled", "re_atr_stop_mult",
    "re_atr_tp1_mult", "entry_trigger_enabled", "entry_trigger_rejection", "entry_trigger_deceleration",
    "entry_trigger_max_range_ratio", "meta_label_gate_enabled", "meta_label_threshold",
    "session_liquidity_gate_enabled", "event_tier_gate_enabled", "vol_target_sizing_enabled",
    "correlated_exposure_cap_lots", "liquidity_map_levels_enabled", "re_blocked_level_types",
    "re_ai_tuning_enabled", "htf_bias_asian_exempt", "re_cme_context_enabled",
    "setforget_lot_size", "tg_event_tier_gate_enabled", "re_xasset_features_enabled",
    "re_require_proven_edge",
)
