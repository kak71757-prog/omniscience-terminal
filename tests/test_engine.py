"""OMNISCIENCE engine tests — run with: python3 -m pytest tests -q

These exercise the deterministic parts of the system: position sizing, the
triple-TP ladder, prop-firm guards, SMC detection on synthetic structure, the
ensemble calibration and the ML training loop.  Live feeds are not touched.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.config import EngineParams, RISK
from server.engine import ict as I
from server.engine import liquidity, profile
from server.engine.brain import Brain, GeneticEvolver, backtest
from server.engine.ml import (DivineLSTM, build_features, build_targets,
                             intent_labels, seq_windows)
from server.engine.orderflow import OrderFlow
from server.engine.risk import ladder, prop_check, size_position
from server.engine.shields import anti_manipulation, evaluate, killzone, news


# ------------------------------------------------------------------ fixtures
def synth_candles(n=400, start=100.0, vol=0.004, seed=3):
    rng = np.random.default_rng(seed)
    out = []
    px = start
    t = 1_700_000_000
    for i in range(n):
        shock = rng.normal(0, vol)
        # inject structure: trend, impulse, sweep-and-reclaim
        if 150 <= i < 170:
            shock += 0.004
        if i in (220, 260, 300):
            shock -= 0.010
        px *= float(np.exp(shock))
        o = px * (1 - abs(rng.normal(0, vol / 3)))
        c = px
        h = max(o, c) * (1 + abs(rng.normal(0, vol / 4)))
        l = min(o, c) * (1 - abs(rng.normal(0, vol / 4)))
        out.append({"t": t + i * 300, "o": o, "h": h, "l": l, "c": c,
                    "v": float(abs(rng.normal(1000, 200)))})
    return out


# ------------------------------------------------------------------- sizing
def test_position_sizing_respects_risk_budget():
    s = size_position(entry=100.0, sl=98.0, capital=1000.0, risk_pct=0.01,
                      max_leverage=10.0, fee_bps=5, slippage_bps=1.5)
    assert s["ok"]
    # $10 risk over a $2 stop => 5 units
    assert s["qty"] == pytest.approx(5.0)
    assert s["risk_usd"] == pytest.approx(10.0)
    assert s["notional"] == pytest.approx(500.0)


def test_position_sizing_caps_leverage():
    s = size_position(entry=100.0, sl=99.9, capital=1000.0, risk_pct=0.01,
                      max_leverage=5.0, fee_bps=5, slippage_bps=1.5)
    assert s["leverage"] == pytest.approx(5.0)
    assert s["capped"] is True
    assert s["risk_usd"] <= 10.0 + 1e-9


def test_sizing_rejects_zero_stop():
    assert size_position(100, 100, 1000, 0.01, 10, 5, 1.5)["ok"] is False


# ------------------------------------------------------------------- ladder
def test_triple_tp_ladder_geometry():
    t = ladder(entry=100.0, sl=98.0, side="LONG", tp1_r=1, tp2_r=2, tp3_r=4)
    assert t["tp1"] == pytest.approx(102.0)
    assert t["tp2"] == pytest.approx(104.0)
    assert t["tp3"] == pytest.approx(108.0)
    assert [l["pct"] for l in t["legs"]] == [50, 30, 20]
    short = ladder(entry=100.0, sl=102.0, side="SHORT", tp1_r=1, tp2_r=2, tp3_r=4)
    assert short["tp1"] == pytest.approx(98.0)
    assert short["tp3"] == pytest.approx(92.0)


# ------------------------------------------------------------------ shields
def test_killzone_open_and_closed():
    from datetime import datetime, timezone
    kz = killzone(datetime(2026, 10, 24, 9, 30, tzinfo=timezone.utc))
    assert kz["open"] and kz["zone"] == "LONDON OPEN"
    closed = killzone(datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc))
    assert not closed["open"]
    assert closed["next_in_min"] > 0


def test_anti_manipulation_funding_extreme_blocks_only_one_side():
    r = anti_manipulation(funding_rate=0.0012, price_chg_pct_15m=0.0,
                          oi_chg_pct_15m=0.0, usdt_dom_chg_1h=0.0, side="LONG")
    assert r["blocked"] and r["reasons"][0]["code"] == "FUNDING_EXTREME"
    r2 = anti_manipulation(funding_rate=0.0012, price_chg_pct_15m=0.0,
                           oi_chg_pct_15m=0.0, usdt_dom_chg_1h=0.0, side="SHORT")
    assert not r2["blocked"]


def test_anti_manipulation_fake_pump():
    r = anti_manipulation(0.0, price_chg_pct_15m=0.4, oi_chg_pct_15m=-0.2,
                          usdt_dom_chg_1h=0.0, side="LONG")
    assert r["blocked"]
    assert any(x["code"] == "OI_DIVERGENCE" for x in r["reasons"])


def test_stablecoin_flight_blocks_longs():
    r = anti_manipulation(0.0, 0.0, 0.0, usdt_dom_chg_1h=1.6, side="LONG")
    assert r["blocked"]
    r2 = anti_manipulation(0.0, 0.0, 0.0, usdt_dom_chg_1h=1.6, side="SHORT")
    assert not r2["blocked"]


def test_shield_stack_forces_score_to_zero():
    kz = {"open": False, "zone": "CLOSED", "next": "LONDON OPEN", "next_in_min": 30}
    am = {"blocked": False, "reasons": [], "checks": {}}
    nw = news({"active": False})
    res = evaluate(kz, am, nw, 95.0)
    assert res["score"] == 0.0 and res["raw_score"] == 95.0
    assert res["blocks"][0]["code"] == "KILLZONE"
    ok = evaluate({"open": True}, am, nw, 91.0)
    assert ok["score"] == 91.0 and not ok["blocked"]


def test_news_blackout_window():
    nw = news({"active": True, "events": [{"title": "CPI", "date": 0}],
               "window_min": 15})
    assert nw["blocked"] and "VOLATILITY GOD" in nw["banner"]


# ---------------------------------------------------------------------- SMC
def test_smc_detects_objects_and_is_well_formed():
    candles = synth_candles()
    p = EngineParams()
    smc = I.detect(candles, p)
    assert smc["ok"]
    assert 0 <= smc["structure"]["zone_pos"] <= 100
    for ob in smc["order_blocks"]:
        assert ob["top"] > ob["bot"]
        assert 0.0 <= ob["retrace"] <= 3.0
    for f in smc["fvgs"]:
        assert f["top"] > f["bot"]
        assert f["gap_atr"] >= p.fvg_min_atr * 0.99
    for b in smc["structure"]["breaks"]:
        assert b["type"] in ("BOS", "MSS")


def test_ict_score_bounds_and_direction_sensitivity():
    candles = synth_candles()
    p = EngineParams()
    smc = I.detect(candles, p)
    for d in ("LONG", "SHORT"):
        s, detail = I.ict_score(smc, p, d)
        assert 0 <= s <= 100
        assert set(detail["components"]) == {
            "sweep", "structure_break", "order_block", "fvg", "premium_discount"}


def test_detect_requires_history():
    assert I.detect(synth_candles(20), EngineParams())["ok"] is False


# ------------------------------------------------------------------ profile
def test_volume_profile_value_area_contains_poc():
    candles = synth_candles()
    p = profile.build(candles)
    assert p["ok"]
    assert p["val"] <= p["poc"] <= p["vah"]
    assert p["high"] >= p["vah"] and p["low"] <= p["val"]
    assert abs(sum(l["pct"] for l in p["levels"]) - 100.0) < 1.0


# ---------------------------------------------------------------- liquidity
def test_liquidation_map_triggers_and_magnets():
    candles = synth_candles()
    price = candles[-1]["c"]
    lm = liquidity.liquidation_map(price, oi_usd=2e9, candles=candles,
                                   real_liqs=[], bins=60)
    assert lm["ok"] and len(lm["rows"]) == 60
    assert lm["liq_within_2pct"]["up_short_squeeze"] >= 0
    assert lm["liq_within_2pct"]["down_long_cascade"] >= 0
    assert set(lm["triggers"]) == {"50M", "100M", "200M"}
    # notional must be conserved-ish: heat concentrated near spot
    assert lm["magnet_up"] > price > lm["magnet_down"]


def test_bsl_ssl_shelves_are_ordered():
    candles = synth_candles()
    smc = I.detect(candles, EngineParams())
    price = candles[-1]["c"]
    lm = liquidity.liquidation_map(price, 1e9, candles, [])
    rails = liquidity.bsl_ssl(smc, candles, lm, price)
    for r in rails["bsl"]:
        assert r["price"] > price
    for r in rails["ssl"]:
        assert r["price"] < price


# ---------------------------------------------------------------- orderflow
def test_footprint_splits_aggressor_volume():
    of = OrderFlow()
    base_t = 1_700_000_000_000
    for i in range(600):
        px = 100 + (i % 7) * 0.01
        of.add_trade(base_t + i * 1000, px, 1.0, "buy" if i % 3 else "sell")
    fp = of.footprint(bucket_ms=60_000, buckets=10)
    assert fp["ok"] and fp["candles"]
    assert fp["buy_ratio"] > 50  # 2/3 of prints are aggressive buys
    assert fp["cvd"] > 0
    for c in fp["candles"]:
        assert c["ask"] >= 0 and c["bid"] >= 0
        assert abs(c["delta"] - (c["ask"] - c["bid"])) <= 2.0  # usd rounded to 0dp


def test_cvd_divergence_flags_distribution():
    of = OrderFlow()
    base_t = 1_700_000_000_000
    # rising price, heavy aggressive selling => bearish divergence
    for i in range(400):
        px = 100 + i * 0.02
        of.add_trade(base_t + i * 60_000, px, 3.0, "sell")
    fp = of.footprint(bucket_ms=300_000, buckets=60)
    assert fp["divergence"]["state"] == "BEARISH"


# ----------------------------------------------------------------------- ML
def test_lstm_learns_and_shapes_are_correct():
    candles = synth_candles(600)
    X = build_features(candles)
    Y = build_targets(candles)
    xs, ys = seq_windows(X, Y)
    assert xs.shape[1:] == (32, X.shape[1])
    hold = xs[400:464]
    m = DivineLSTM()
    before = float(np.mean((m.forward(hold) - ys[400:464]) ** 2))
    m.fit(xs[:400], ys[:400], epochs=4, lr=3e-3)
    after = float(np.mean((m.forward(hold) - ys[400:464]) ** 2))
    assert after < before, f"holdout MSE rose: {before:.5f} -> {after:.5f}"
    assert m.forward(xs[:5]).shape == (5, 15)


def test_lstm_roundtrip(tmp_path):
    candles = synth_candles(200)
    X = build_features(candles)
    Y = build_targets(candles)
    xs, ys = seq_windows(X, Y)
    m = DivineLSTM().fit(xs[:50], ys[:50], epochs=1)
    p = tmp_path / "m.npz"
    m.save(str(p))
    m2 = DivineLSTM.load(str(p))
    a = m.forward(xs[:3]); b = m2.forward(xs[:3])
    assert np.allclose(a, b)


def test_intent_labels_are_binary_and_balanced():
    candles = synth_candles(800, seed=11)
    y = intent_labels(candles)
    assert set(np.unique(y)).issubset({0, 1})
    assert 0.02 < y.mean() < 0.98


def test_intent_score_calibration():
    b = Brain()
    b.clf = type("C", (), {"metrics": {"base_rate": 0.38}})()
    # a probability equal to the base rate must land at the neutral 50
    assert b.intent_score(0.38) == pytest.approx(50.0, abs=1.0)
    assert b.intent_score(0.60) > 60
    assert b.intent_score(0.10) < 25
    assert b.intent_score(0.0) == 0.0


def test_lstm_component_direction_awareness():
    b = Brain()
    fc_up = {"direction": "UP", "cum_return_pct": 0.5, "confidence": 0.9}
    assert b.lstm_component("LONG", fc_up) > b.lstm_component("SHORT", fc_up)
    assert b.lstm_component("LONG", None) == 50.0


# ---------------------------------------------------------------- backtest
def test_backtest_is_deterministic_and_bounded():
    candles = synth_candles(500, seed=5)
    p = EngineParams()
    a = backtest(candles, p, decisions=40, step=3, window=170, threshold=60)
    b = backtest(candles, p, decisions=40, step=3, window=170, threshold=60)
    assert a == b
    assert a["trades"] >= 0
    if a["trades"]:
        assert a["max_dd_r"] <= 0 or a["net_r"] > 0
        assert 0 <= a["winrate"] <= 100


def test_genetic_mutation_stays_inside_gene_bounds():
    from server.engine.brain import GENES
    candles = synth_candles(400)
    ga = GeneticEvolver(EngineParams(), candles, pop=4, gens=1, seed=1)
    child = ga.mutate(EngineParams(), ["fvg_min_atr", "sl_atr_mult"], rate=0.9)
    for g, (lo, hi) in GENES.items():
        v = getattr(child, g)
        assert lo <= v <= hi, f"{g}={v} outside [{lo},{hi}]"
    assert child.ema_fast < child.ema_slow


# ------------------------------------------------------------------ prop
def test_prop_guard_reports_limits():
    p = prop_check(1000.0, 1000.0)
    assert p["max_trades_day"] == RISK.prop_max_trades_day
    assert 0 <= p["daily_loss_used_pct"] <= 200
    assert isinstance(p["breached"], list)
