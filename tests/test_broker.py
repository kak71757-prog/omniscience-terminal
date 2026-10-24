"""Execution engine lifecycle: entry -> TP1 (BE) -> TP2 (trail) -> TP3 -> close,
plus stop-outs, manual closes, journal attribution and the prop guard."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.config import RISK
from server.engine.broker import Broker
from server.engine.risk import ladder
from server.store import db


def fresh():
    for row in db.q("SELECT id FROM trades WHERE status='OPEN'"):
        db.x("DELETE FROM trades WHERE id=?", (row["id"],))
    return Broker()


def open_long(b: Broker, entry=100.0, sl=98.0):
    tp = ladder(entry, sl, "LONG", 1, 2, 4)
    return b.open("BTC-USDT-SWAP", "LONG", entry, sl, tp, qty=5.0, risk_usd=10.0,
                  score=88.0, score_parts={"ict": 90, "lstm": 88, "intent": 85},
                  features={"atr": 2.0, "zone": "DISCOUNT", "sweep": True,
                            "cvd_div": "NONE"})


def test_full_triple_tp_lifecycle():
    b = fresh()
    tid = open_long(b)
    assert b.has_position("BTC-USDT-SWAP")

    # price runs to TP1 (1R) -> half booked, stop to break-even
    b.on_price("BTC-USDT-SWAP", 102.0)
    p = b.positions[tid]
    assert [l["name"] for l in p["legs"]] == ["TP1"]
    assert p["remaining"] == 50.0
    assert p["sl"] == pytest.approx(100.0)      # moved to break even
    assert p["sl_to_be"] == 1

    # TP2 (2R) -> 30% booked, runner trails
    b.on_price("BTC-USDT-SWAP", 104.0)
    p = b.positions[tid]
    assert [l["name"] for l in p["legs"]] == ["TP1", "TP2"]
    assert p["remaining"] == 20.0
    assert p["sl"] >= 100.0

    # TP3 (4R) -> flat
    ev = b.on_price("BTC-USDT-SWAP", 108.0)
    assert any(e["kind"] == "CLOSE" for e in ev)
    assert not b.has_position("BTC-USDT-SWAP")

    row = dict(db.q("SELECT * FROM trades WHERE id=?", (tid,))[0])
    assert row["status"] == "CLOSED"
    assert row["realised_pnl"] > 0
    assert row["r_multiple"] > 1.5


def test_stop_loss_closes_at_minus_one_r():
    b = fresh()
    tid = open_long(b)
    ev = b.on_price("BTC-USDT-SWAP", 97.9)
    assert ev and ev[0]["kind"] == "CLOSE"
    row = dict(db.q("SELECT * FROM trades WHERE id=?", (tid,))[0])
    assert row["realised_pnl"] < 0
    assert -1.6 < row["r_multiple"] <= 0        # fees included, never worse than ~1R


def test_break_even_stop_after_tp1():
    b = fresh()
    tid = open_long(b)
    b.on_price("BTC-USDT-SWAP", 102.0)          # TP1 hit, SL -> entry
    b.on_price("BTC-USDT-SWAP", 99.9)           # back through entry
    row = dict(db.q("SELECT * FROM trades WHERE id=?", (tid,))[0])
    assert row["status"] == "CLOSED"
    assert row["r_multiple"] > 0.2              # banked the TP1 leg


def test_short_side_mirrors():
    b = fresh()
    tp = ladder(100.0, 102.0, "SHORT", 1, 2, 4)
    tid = b.open("ETH-USDT-SWAP", "SHORT", 100.0, 102.0, tp, 5.0, 10.0, 90.0,
                 {"ict": 90}, {"atr": 2.0})
    b.on_price("ETH-USDT-SWAP", 98.0)           # TP1
    p = b.positions[tid]
    assert [l["name"] for l in p["legs"]] == ["TP1"]
    assert p["sl"] == pytest.approx(100.0)
    b.on_price("ETH-USDT-SWAP", 96.0)           # TP2
    b.on_price("ETH-USDT-SWAP", 92.0)           # TP3
    assert not b.has_position("ETH-USDT-SWAP")


def test_manual_close_and_journal_attribution():
    b = fresh()
    tid = open_long(b)
    ev = b.close_manual(tid, 100.5)
    assert ev and ev["reason"] == "MANUAL CLOSE"
    kinds = [j["kind"] for j in db.journal_recent(40)]
    assert "ENTRY" in kinds
    assert any(k in ("WIN", "REVIEW") for k in kinds)
    # the review must cite a concrete cause or explicitly state none
    reviews = [j for j in db.journal_recent(40) if j["kind"] in ("WIN", "REVIEW")]
    assert reviews and len(reviews[0]["text"]) > 40


def test_losing_trade_writes_attribution():
    b = fresh()
    tp = ladder(100.0, 98.0, "LONG", 1, 2, 4)
    tid = b.open("BTC-USDT-SWAP", "LONG", 100.0, 98.0, tp, 5.0, 10.0, 90.0,
                 {"ict": 55, "components": {"premium_discount": 0}},
                 {"atr": 2.0, "zone": "PREMIUM", "sweep": False,
                  "cvd_div": "BEARISH", "oi_chg": 0.0, "funding": 0.0001,
                  "in_value_area": True})
    b.on_price("BTC-USDT-SWAP", 97.5)
    rev = [j for j in db.journal_recent(20) if j["kind"] == "REVIEW"]
    assert rev
    txt = rev[0]["text"]
    assert "Loss attribution" in txt
    assert "PREMIUM" in txt or "sweep" in txt or "divergence" in txt
    assert "Next time" in txt


def test_no_duplicate_positions_per_symbol():
    b = fresh()
    open_long(b)
    assert b.has_position("BTC-USDT-SWAP")
    # the hub refuses a second entry; the broker itself is idempotent on reload
    b2 = Broker()
    assert len(b2.summary()) == 1


def test_stats_track_profit_factor_and_drawdown():
    b = fresh()
    for row in db.q("SELECT id FROM trades"):
        db.x("DELETE FROM trades WHERE id=?", (row["id"],))
    t1 = open_long(b)
    b.on_price("BTC-USDT-SWAP", 108.0)          # full win
    t2 = fresh().open("ETH-USDT-SWAP", "LONG", 100.0, 98.0,
                      ladder(100.0, 98.0, "LONG", 1, 2, 4), 5.0, 10.0, 90.0,
                      {}, {"atr": 2.0})
    b = Broker()
    b.on_price("ETH-USDT-SWAP", 97.0)           # full loss
    st = db.trade_stats()
    assert st["trades"] == 2
    assert st["wins"] == 1 and st["losses"] == 1
    assert st["profit_factor"] > 1.0
    assert st["winrate"] == pytest.approx(50.0)
    assert st["max_dd"] < 0
    assert len(st["equity_curve"]) == 2


def test_prop_limits_gate_entries():
    from server.engine.risk import prop_check
    old_mode = RISK.mode
    RISK.mode = "PROP"
    try:
        p = prop_check(1000.0, 1000.0)
        assert p["enforced"] is True
        assert p["max_trades_day"] == 3
        assert isinstance(p["allowed"], bool)
    finally:
        RISK.mode = old_mode
