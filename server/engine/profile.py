"""MAP D - VOLUME PROFILE.  Session profile, POC, VAH/VAL, HVN/LVN.

Volume is distributed across each candle's range with a triangular kernel
peaked at the close (standard TPO/VP approximation) so a 5m candle
contributes realistically instead of dumping all volume on one tick.
"""
from __future__ import annotations

import numpy as np


def build(candles: list[dict], bins: int = 90, value_area: float = 0.70,
          session_bars: int = 288) -> dict:
    if len(candles) < 12:
        return {"ok": False, "levels": [], "poc": None, "vah": None, "val": None}

    cs = candles[-session_bars:]
    hi = max(c["h"] for c in cs)
    lo = min(c["l"] for c in cs)
    if hi <= lo:
        return {"ok": False, "levels": [], "poc": None, "vah": None, "val": None}

    edges = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    prof = np.zeros(bins)

    for c in cs:
        rng = c["h"] - c["l"]
        if rng <= 0:
            i = min(bins - 1, max(0, int((c["c"] - lo) / (hi - lo) * bins)))
            prof[i] += c["v"]
            continue
        i0 = int((c["l"] - lo) / (hi - lo) * bins)
        i1 = int((c["h"] - lo) / (hi - lo) * bins)
        i0 = max(0, min(bins - 1, i0)); i1 = max(0, min(bins - 1, i1))
        idx = np.arange(i0, i1 + 1)
        cx = centers[idx]
        # triangular kernel peaked at close, floor at edges
        w = 1.0 - np.abs(cx - c["c"]) / max(rng, 1e-12) * 0.85
        w = np.clip(w, 0.15, 1.0)
        w = w / w.sum()
        prof[idx] += c["v"] * w

    total = prof.sum() or 1.0
    poc_i = int(prof.argmax())
    # value area expansion from POC
    lo_i = hi_i = poc_i
    acc = prof[poc_i]
    while acc < total * value_area and (lo_i > 0 or hi_i < bins - 1):
        down = prof[lo_i - 1] if lo_i > 0 else -1
        up = prof[hi_i + 1] if hi_i < bins - 1 else -1
        if up >= down:
            hi_i += 1; acc += prof[hi_i]
        else:
            lo_i -= 1; acc += prof[lo_i]

    mean = prof.mean()
    levels = []
    for i in range(bins):
        if prof[i] <= 0:
            continue
        levels.append({
            "price": round(float(centers[i]), 6),
            "vol": round(float(prof[i]), 2),
            "pct": round(float(100 * prof[i] / total), 3),
            "node": ("HVN" if prof[i] > mean * 1.6 else
                     "LVN" if prof[i] < mean * 0.45 else ""),
        })

    hvn = sorted([l for l in levels if l["node"] == "HVN"],
                 key=lambda z: -z["vol"])[:4]
    price = cs[-1]["c"]
    return {
        "ok": True,
        "poc": round(float(centers[poc_i]), 6),
        "vah": round(float(centers[hi_i]), 6),
        "val": round(float(centers[lo_i]), 6),
        "high": round(hi, 6),
        "low": round(lo, 6),
        "levels": levels,
        "hvn": hvn,
        "total_volume": round(float(total), 2),
        "in_value_area": bool(centers[lo_i] <= price <= centers[hi_i]),
        "position": ("ABOVE_VAH" if price > centers[hi_i] else
                     "BELOW_VAL" if price < centers[lo_i] else "IN_VA"),
    }
