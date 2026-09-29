"""Reconciliation tells "in flight" from "did not fill" (bug 070, 2026-09-29).

On the Mac, every forwarded signal whose ack timed out that day was reported
as "the broker has no trace — the send did not fill" while MT5 still held its
order in state "started", and again after the VPS had recorded the trade. Both
claims were false.

- A parked signal, or an open placeholder row, whose order MT5 still holds is
  IN_FLIGHT: it may yet fill.
- An order list that could not be read (`broker_orders=None`) is not an empty
  one: the report says it could not tell.
- A parked signal the paired node recorded (matched by signal_id from the
  heartbeat) is REMOTE_NODE.

Pure function, no I/O. Reconciliation still writes nothing.
"""
from backend.src.services.positions import reconciliation as rc

TRADE_ID = "0ed7581e-c326-4d"
SIGNAL = {"signal_id": "2fa8cbd9-81a6-48", "trade_id": TRADE_ID}
ORDER = {"ticket": 2107562994, "type": 0, "state": 0,
         "comment": "ea:" + TRADE_ID[:12], "volume": 0.02}
PLACEHOLDER = {"trade_id": TRADE_ID, "mt5_ticket": 0, "status": "open"}


def _kinds(diff):
    return [e.kind for e in diff.entries]


def test_a_parked_signal_with_its_order_in_flight_is_in_flight():
    diff = rc.diff_snapshots([], [], [], [SIGNAL], broker_orders=[ORDER])
    assert _kinds(diff) == [rc.IN_FLIGHT]
    assert diff.entries[0].ticket == 2107562994
    assert diff.needs_attention


def test_a_placeholder_with_its_order_in_flight_is_in_flight_not_no_evidence():
    diff = rc.diff_snapshots([], [], [PLACEHOLDER], broker_orders=[ORDER])
    assert _kinds(diff) == [rc.IN_FLIGHT]


def test_with_no_orders_a_parked_signal_did_not_fill_as_before():
    diff = rc.diff_snapshots([], [], [], [SIGNAL], broker_orders=[])
    assert _kinds(diff) == [rc.UNKNOWN_NOT_FILLED]
    assert "did not fill" in diff.entries[0].detail


def test_an_unreadable_order_list_does_not_claim_it_did_not_fill():
    diff = rc.diff_snapshots([], [], [], [SIGNAL], broker_orders=None)
    assert _kinds(diff) == [rc.UNKNOWN_NOT_FILLED]
    assert "did not fill" not in diff.entries[0].detail
    assert "could not be read" in diff.entries[0].detail


def test_an_order_for_another_trade_changes_nothing():
    other = dict(ORDER, comment="ea:ffffffff-fff")
    diff = rc.diff_snapshots([], [], [PLACEHOLDER], broker_orders=[other])
    assert _kinds(diff) == [rc.DB_ONLY_NO_EVIDENCE]


def test_a_fill_still_wins_over_an_order():
    position = {"ticket": 2107562994, "open_price": 4147.0, "comment": ORDER["comment"]}
    diff = rc.diff_snapshots([position], [], [], [SIGNAL], broker_orders=[ORDER])
    assert rc.UNKNOWN_FILLED in _kinds(diff)
    assert rc.IN_FLIGHT not in _kinds(diff)


def test_a_parked_signal_the_paired_node_recorded_is_the_paired_nodes():
    # The Mac forwarded it; the ack timed out; the VPS has the row.
    forwarded = {"signal_id": SIGNAL["signal_id"], "trade_id": None}
    vps_row = {"trade_id": "9a9a9a9a-0000-00", "signal_id": SIGNAL["signal_id"],
               "mt5_ticket": 0}
    diff = rc.diff_snapshots([], [], [], [forwarded], remote_open_trades=[vps_row],
                             broker_orders=[])
    assert _kinds(diff) == [rc.REMOTE_NODE]
    assert not diff.needs_attention


def test_the_default_is_the_old_behaviour():
    # Callers that pass no order list get exactly what they got before.
    diff = rc.diff_snapshots([], [], [], [SIGNAL])
    assert _kinds(diff) == [rc.UNKNOWN_NOT_FILLED]
    assert "did not fill" in diff.entries[0].detail


def test_the_report_names_the_in_flight_kind():
    diff = rc.diff_snapshots([], [], [], [SIGNAL], broker_orders=[ORDER])
    assert "in_flight: 1" in rc.report(diff)


def test_the_heartbeat_carries_each_rows_signal_id():
    """What the match above reads on the Mac: the VPS's own open rows."""
    import asyncio

    from backend.src.services.cluster.sync.server import SyncServer

    class _Engine:
        def get_open_trades(self):
            return [{"trade_id": TRADE_ID, "signal_id": SIGNAL["signal_id"],
                     "mt5_ticket": 0}]

        async def get_mt5_account(self):
            return None

        async def get_triggered_tps(self, _trade_id):
            return set()

    payload = asyncio.run(SyncServer(main_engine=_Engine())._status_payload())
    assert payload["open_positions"][0]["signal_id"] == SIGNAL["signal_id"]
