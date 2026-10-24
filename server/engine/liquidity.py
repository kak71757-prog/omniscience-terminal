"""MAP A + MAP F - LIQUIDITY HEATMAP and LIQUIDATION MAP.

Two independent, real inputs are fused:

  1. Structural liquidity (BSL/SSL): fractal swing highs/lows clustered into
     pools, weighted by the notional stacked behind them.  Resting stops above
     equal highs are the magnet the algorithm hunts.
  2. Liquidation notional: modelled from LIVE open interest using the standard
     leverage-tier model (10x/25x/50x/100x buckets with maintenance margin),
     which is exactly the model Coinglass-class tools use.  Real per-order
     liquidation prints from the venue are overlaid where the venue exposes
     them so the heatmap is anchored to observed data, not just a model.
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

# leverage bucket -> (weight of OI, maintenance margin)
TIERS = [(10, 0.18, 0.005), (25, 0.30, 0.0065),
         (50, 0.28, 0.010), (100, 0.24, 0.020)]


def liq_price(entry: float, lev: float, maint: float, is_long: bool) -> float:
    if is_long:
        return entry * (1.0 - 1.0 / lev + maint)
    return entry * (1.0 + 1.0 / lev - maint)


def liquidation_map(price: float, oi_usd: float, candles: list[dict],
                    real_liqs: list[dict], bins: int = 70,
                    span: float = 0.045) -> dict:
    """Distribution of liquidation notional across price for +/- span."""
    lo = price * (1 - span)
    hi = price * (1 + span)
    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    longs = np.zeros(bins)
    shorts = np.zeros(bins)

    # anchor entries on the traded distribution of the last 12h, not a
    # single spot price: positions were opened all over that range.
    tail = candles[-144:] if len(candles) >= 144 else candles
    if tail:
        vols = np.array([c["v"] for c in tail], float)
        px = np.array([c["c"] for c in tail], float)
        w = vols / (vols.sum() or 1.0)
    else:
        px = np.array([price]); w = np.array([1.0])

    for lev, share, maint in TIERS:
        notional = oi_usd * share
        for p_, ww in zip(px, w):
            # longs
            lp = liq_price(p_, lev, maint, True)
            if lo <= lp <= hi:
                i = min(bins - 1, max(0, int((lp - lo) / (hi - lo) * bins)))
                longs[i] += notional * ww
            # shorts
            sp = liq_price(p_, lev, maint, False)
            if lo <= sp <= hi:
                i = min(bins - 1, max(0, int((sp - lo) / (hi - lo) * bins)))
                shorts[i] += notional * ww

    # overlay observed liquidation prints (real venue data)
    for r in real_liqs[-4000:]:
        p_ = r.get("price")
        if not p_ or not (lo <= p_ <= hi):
            continue
        i = min(bins - 1, max(0, int((p_ - lo) / (hi - lo) * bins)))
        sz_usd = r.get("usd", 0.0)
        if r.get("side") == "long":     # long got liquidated => sell pressure
            longs[i] += sz_usd * 6.0
        else:
            shorts[i] += sz_usd * 6.0

    total = longs + shorts
    mx = float(total.max()) or 1.0
    rows = []
    for i in range(bins):
        rows.append({
            "price": round(float(centers[i]), 6),
            "long_usd": round(float(longs[i]), 0),
            "short_usd": round(float(shorts[i]), 0),
            "total_usd": round(float(total[i]), 0),
            "intensity": round(float(total[i] / mx), 4),
            "side": "LONG" if longs[i] >= shorts[i] else "SHORT",
        })

    def band(pct: float, side: str) -> float:
        target = price * (1 + pct) if pct > 0 else price * (1 + pct)
        if side == "long":
            sel = [(c_, l_) for c_, l_, s_ in zip(centers, longs, shorts)
                   if (c_ <= price and c_ >= target) or (c_ >= price and c_ <= target)]
            return float(sum(x[1] for x in sel))
        sel = [(c_, s_) for c_, s_ in zip(centers, shorts)
               if (c_ >= price and c_ <= target) or (c_ <= price and c_ >= target)]
        return float(sum(x[1] for x in sel))

    up2 = band(0.02, "short")
    dn2 = band(-0.02, "long")

    # where do 50/100/200M triggers sit
    triggers = {}
    for notional in (50e6, 100e6, 200e6):
        acc = 0.0
        hit_up = hit_dn = None
        for i in range(bins):
            if centers[i] >= price:
                acc += shorts[i]
                if acc >= notional and hit_up is None:
                    hit_up = float(centers[i])
        acc = 0.0
        for i in range(bins - 1, -1, -1):
            if centers[i] <= price:
                acc += longs[i]
                if acc >= notional and hit_dn is None:
                    hit_dn = float(centers[i])
        triggers[f"{int(notional/1e6)}M"] = {
            "up": round(hit_up, 2) if hit_up else None,
            "down": round(hit_dn, 2) if hit_dn else None,
        }

    peak_long = float(centers[int(longs.argmax())])
    peak_short = float(centers[int(shorts.argmax())])

    return {
        "ok": True,
        "rows": rows,
        "span_pct": span * 100,
        "triggers": triggers,
        "magnet_down": round(peak_long, 2),
        "magnet_up": round(peak_short, 2),
        "liq_within_2pct": {"up_short_squeeze": round(up2, 0),
                            "down_long_cascade": round(dn2, 0)},
        "real_prints_used": len(real_liqs[-4000:]),
    }


def bsl_ssl(smc: dict, candles: list[dict], liq_map: dict, price: float) -> dict:
    """Buy-side / sell-side liquidity shelves with strength weighting."""
    A = smc.get("atr") or price * 0.002
    highs, lows = [], []
    for ob in smc.get("order_blocks", []):
        (highs if ob["kind"] == "BEAR_OB" else lows).append(ob["top"] if ob["kind"] == "BEAR_OB" else ob["bot"])
    for pool in smc.get("pools", []):
        (highs if pool["kind"] == "EQH" else lows).append(pool["price"])
    for b in smc.get("structure", {}).get("breaks", []):
        highs.append(b["price"])

    seen_h = sorted([h for h in highs if h > price], key=lambda z: z - price)[:6]
    seen_l = sorted([l for l in lows if l < price], key=lambda z: price - z)[:6]

    # notional stacked behind each shelf from the liquidation model
    rows = liq_map.get("rows", [])

    def stack(level: float, side: str) -> float:
        tot = 0.0
        for r in rows:
            if abs(r["price"] - level) <= 0.5 * A:
                tot += r["short_usd"] if side == "buy" else r["long_usd"]
        return tot

    bsl = [{"price": round(h, 6), "dist_pct": round(100 * (h - price) / price, 3),
            "usd": round(stack(h, "buy"), 0),
            "label": "BSL"} for h in seen_h]
    ssl = [{"price": round(l, 6), "dist_pct": round(100 * (l - price) / price, 3),
            "usd": round(stack(l, "sell"), 0),
            "label": "SSL"} for l in seen_l]

    # equal-high clusters get an explicit pool tag from smc
    for pool in smc.get("pools", []):
        tgt = bsl if pool["kind"] == "EQH" else ssl
        for e in tgt:
            if abs(e["price"] - pool["price"]) < 1e-9:
                e["label"] = "LIQUIDITY POOL"

    mx = max([e["usd"] for e in bsl + ssl] + [1.0])
    for e in bsl + ssl:
        e["intensity"] = round(e["usd"] / mx, 3)
    return {"bsl": bsl, "ssl": ssl}


def sweep_read(smc: dict) -> dict:
    """Human-readable read on the last sweep, used by the scanner + journal."""
    sw = smc.get("sweeps") or []
    if not sw:
        return {"active": False, "text": "No recent liquidity sweep"}
    s = sw[-1]
    return {
        "active": True,
        "dir": s["dir"],
        "text": (f"{'Sell' if s['dir']=='bear' else 'Buy'} side liquidity swept at "
                 f"{s['price']:.2f}, reclaimed {s['reclaimed']:.2f}"),
        "price": s["price"],
    }
