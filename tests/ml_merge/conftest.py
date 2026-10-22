"""Temp Reversal and Breakout databases for the training-data merge (docs/todo/026).

Nothing here reaches a broker or the sync link.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from backend.src.services.breakout_signal import breakout_signal_repo as bdb
from backend.src.services.breakout_signal import ml_engine as bo_ml
from backend.src.services.reversal_engine import pro_corpus_repo as pro_corpus
from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from backend.src.services.reversal_engine.ml_engine import FEATURE_NAMES
from tests.conftest import remove_db_file

OWN = "mac-node"
PEER = "vps-node"


@pytest.fixture
def dbs():
    paths = []
    for _ in range(2):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        paths.append(path)
    re_repo.init(paths[0])
    pro_corpus.create_schema()
    bdb.init(paths[1])
    yield
    re_repo.close_db()
    bdb.close_db()
    for p in paths:
        remove_db_file(p)


def re_vector(x: float) -> list:
    f = [0.3] * len(FEATURE_NAMES)
    f[0] = x
    return f


def re_row(ref: str, t: float, *, x: float = 0.3, win: bool = True,
           direction: str = "BUY", level: float = 4100.0,
           executed: bool = False, status: str = "closed") -> dict:
    """A closed Reversal signal as `re_signals` holds it. sl_dist 5 at the
    0.1 virtual lot puts $50 at risk, so +/-50 is a +/-1R label."""
    return {
        "signal_ref": ref, "created_at": t, "direction": direction,
        "level_price": level, "sl_dist": 5.0, "status": status,
        "outcome": ("win" if win else "loss") if status == "closed" else "open",
        "net_pnl_dollars": 50.0 if win else -50.0, "close_time": t + 60,
        "ml_features_json": json.dumps(re_vector(x)),
        "live_exec_status": "executed" if executed else None,
    }


def add_re(row: dict) -> int:
    return re_repo.create_signal(row)


def bo_row(ref: str, t: float, *, win: bool = True, direction: str = "BUY",
           entry: float = 4100.0, kind: str = "range_break") -> dict:
    return {
        "signal_ref": ref, "created_at": t, "direction": direction,
        "breakout_type": kind, "entry_mid": entry, "stop_loss": entry - 5,
        "sl_dist": 5.0, "rr_tp1": 1.5, "status": "closed",
        "outcome": "win" if win else "loss", "close_time": t + 60,
        "net_pnl_dollars": 50.0 if win else -50.0,
        "ml_features_json": json.dumps([0.4] * bo_ml.N_FEATURES),
    }


def add_bo(row: dict) -> int:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    return bdb.get_db().run(f"INSERT INTO bo_signals ({cols}) VALUES ({marks})",
                            *row.values()).lastrowid


def pro_row(msg_id: str, stage: str, t: float, *, direction: str = "BUY",
            rsi: float = 33.0) -> dict:
    return {
        "tg_message_id": msg_id, "stage": stage, "group_name": "Gold Diggers VIP",
        "direction": direction, "signal_ts": t, "captured_at": t,
        "regime_score": 1.0, "session": "london",
        "indicators_json": json.dumps({"M15": {"rsi14": rsi, "adx14": 30, "atr14": 6.0}}),
        "fvg_json": json.dumps({"fvg_confluence": 1.0, "fvg_dist_norm": 0.5,
                                "fvg_fresh": 1.0, "fvg_size_norm": 0.9}),
        "raw_text": "",
    }
