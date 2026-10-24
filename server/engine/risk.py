"""PHASE 5 - RISK ENGINE.

Position = (capital * risk%) / |entry - SL|, with leverage/tier caps and full
prop-firm rule enforcement.  The triple-TP ladder and break-even logic live in
the broker; this module owns sizing and hard limits.
"""
from __future__ import annotations

import math
import time

from ..config import RISK
from ..store import db


def day_start_ms() -> int:
    now = time.time() * 1000
    return int(now - (now % 86_400_000))


def size_position(entry: float, sl: float, capital: float, risk_pct: float,
                  max_leverage: float, fee_bps: float, slippage_bps: float) -> dict:
    dist = abs(entry - sl)
    if dist <= 0:
        return {"ok": False, "error": "SL equals entry"}
    risk_usd = capital * risk_pct
    qty = risk_usd / dist
    notional = qty * entry
    lev = notional / max(capital, 1e-9)
    capped = False
    if lev > max_leverage:
        lev = max_leverage
        notional = capital * lev
        qty = notional / entry
        risk_usd = qty * dist
        capped = True
    fee = notional * (fee_bps / 10_000.0) * 2
    slip = notional * (slippage_bps / 10_000.0)
    return {"ok": True, "qty": qty, "notional": round(notional, 2),
            "risk_usd": round(risk_usd, 2), "leverage": round(lev, 2),
            "fees_usd": round(fee, 3), "slippage_usd": round(slip, 3),
            "dist": dist, "capped": capped,
            "risk_pct_actual": round(100 * risk_usd / capital, 3)}


def prop_check(capital: float, equity_now: float) -> dict:
    """Prop-firm hard rules: 5% daily loss, 10% total loss, 3 trades/day."""
    day_pnl, day_count = db.day_pnl_and_count(day_start_ms())
    total_pnl = db.trade_stats()["net_pnl"]
    daily_used = -day_pnl / capital if day_pnl < 0 else 0.0
    total_used = -total_pnl / capital if total_pnl < 0 else 0.0
    breached = []
    if daily_used >= RISK.prop_max_daily_loss:
        breached.append(f"DAILY LOSS LIMIT {RISK.prop_max_daily_loss*100:.0f}% BREACHED")
    if total_used >= RISK.prop_max_total_loss:
        breached.append(f"TOTAL DRAWDOWN LIMIT {RISK.prop_max_total_loss*100:.0f}% BREACHED")
    if day_count >= RISK.prop_max_trades_day:
        breached.append(f"MAX {RISK.prop_max_trades_day} TRADES/DAY REACHED")
    return {
        "mode": RISK.mode,
        "enforced": RISK.mode == "PROP",
        "day_pnl": round(day_pnl, 2),
        "day_trades": day_count,
        "max_trades_day": RISK.prop_max_trades_day,
        "daily_loss_used_pct": round(100 * daily_used, 2),
        "daily_loss_limit_pct": RISK.prop_max_daily_loss * 100,
        "total_loss_used_pct": round(100 * total_used, 2),
        "total_loss_limit_pct": RISK.prop_max_total_loss * 100,
        "breached": breached,
        "allowed": not (RISK.mode == "PROP" and breached),
    }


def ladder(entry: float, sl: float, side: str, tp1_r: float, tp2_r: float,
           tp3_r: float) -> dict:
    r = abs(entry - sl)
    sgn = 1 if side == "LONG" else -1
    return {
        "tp1": round(entry + sgn * r * tp1_r, 6),
        "tp2": round(entry + sgn * r * tp2_r, 6),
        "tp3": round(entry + sgn * r * tp3_r, 6),
        "legs": [
            {"name": "TP1", "pct": 50, "price": round(entry + sgn * r * tp1_r, 6),
             "r": tp1_r, "after": "SL -> BREAK EVEN"},
            {"name": "TP2", "pct": 30, "price": round(entry + sgn * r * tp2_r, 6),
             "r": tp2_r, "after": "runner trailed"},
            {"name": "TP3", "pct": 20, "price": round(entry + sgn * r * tp3_r, 6),
             "r": tp3_r, "after": "full exit"},
        ],
    }
