"""A position the paired VPS opened is not "lost" on the Mac (2026-09-28).

With the VPS as active trader, every trade lives in the VPS's database and
none in the Mac's. The Mac's report-only reconciliation reads the same MT5
account, so it logged each of them hourly as BROKER_ONLY_OURS -- "we placed
this and then lost its row -- nothing is managing it" -- about ticket
2103198838 while the VPS was managing it. Now that the owner runs signal
generation on the VPS (2026-09-28), that would be every trade.

The Mac already holds the VPS's open trades from the sync heartbeat. A broker
position matching one of them, by ticket or by the EA order comment, is
REMOTE_NODE: accounted for, and not something needing attention. Anything the
VPS does not claim is reported exactly as before. Pure function, no I/O.
"""
from backend.src.services.positions import reconciliation as rc

VPS_TRADE = {"trade_id": "0ed7581e-c326-4d", "mt5_ticket": 2103198838}
POSITION = {"ticket": 2103198838, "open_price": 4151.03,
            "comment": "ea:0ed7581e-c32"}
ORPHAN = {"ticket": 555, "open_price": 4150.0, "comment": "ea:ffffffff-fff"}


def test_a_vps_trade_is_the_paired_nodes_not_lost():
    diff = rc.diff_snapshots([POSITION], [], [], remote_open_trades=[VPS_TRADE])
    assert [e.kind for e in diff.entries] == [rc.REMOTE_NODE]
    assert not diff.needs_attention


def test_a_vps_placeholder_is_matched_by_its_order_comment():
    placeholder = {"trade_id": "0ed7581e-c326-4d", "mt5_ticket": 0}
    diff = rc.diff_snapshots([POSITION], [], [], remote_open_trades=[placeholder])
    assert [e.kind for e in diff.entries] == [rc.REMOTE_NODE]


def test_a_position_neither_node_claims_is_still_reported():
    diff = rc.diff_snapshots([POSITION, ORPHAN], [], [],
                             remote_open_trades=[VPS_TRADE])
    kinds = sorted(e.kind for e in diff.entries)
    assert kinds == sorted([rc.REMOTE_NODE, rc.BROKER_ONLY_OURS])
    assert diff.needs_attention


def test_without_a_paired_node_nothing_changes():
    diff = rc.diff_snapshots([POSITION], [], [])
    assert [e.kind for e in diff.entries] == [rc.BROKER_ONLY_OURS]


def test_the_report_names_it_as_the_paired_nodes():
    diff = rc.diff_snapshots([POSITION, ORPHAN], [], [],
                             remote_open_trades=[VPS_TRADE])
    text = rc._report_text(diff)
    assert "ticket=555" in text
    assert "2103198838" not in text
