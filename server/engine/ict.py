"""MAP C - SMART MONEY MAP.  ICT/SMC structural detection.

Everything here is derived from the live candle series that is streaming in;
nothing is hard-coded or synthetic.  Objects returned are JSON-safe and carry
chart-space timestamps (seconds) plus price bounds so the frontend canvas
overlay can render them without re-running any logic.
"""
from __future__ import annotations

import numpy as np

from ..config import EngineParams
from ..util import atr, clamp, swing_points


def _bar(t, i, arrs):
    return {
        "t": int(arrs["t"][i]),
        "o": float(arrs["o"][i]), "h": float(arrs["h"][i]),
        "l": float(arrs["l"][i]), "c": float(arrs["c"][i]),
    }


def _mk(t, i, arrs):
    """Scalar timestamp helper (seconds) for a bar index."""
    return int(arrs["t"][i])


def detect(candles: list[dict], p: EngineParams) -> dict:
    """candles: [{t(sec), o,h,l,c,v}] oldest -> newest."""
    if len(candles) < 40:
        return {"ok": False, "reason": "insufficient history"}

    t = np.array([c["t"] for c in candles], dtype=np.int64)
    o = np.array([c["o"] for c in candles], float)
    h = np.array([c["h"] for c in candles], float)
    l = np.array([c["l"] for c in candles], float)
    c = np.array([c["c"] for c in candles], float)
    v = np.array([c["v"] for c in candles], float)
    arrs = {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}

    a = atr(h, l, c, p.atr_period)
    A = float(a[-1]) or (float(c[-1]) * 0.002)
    last = float(c[-1])
    n = len(candles)

    sh_idx, sl_idx = swing_points(h, l, 2, 2)

    # ---------------- dealing range / premium-discount -------------------
    recent_sh = [i for i in sh_idx if i > n - 120]
    recent_sl = [i for i in sl_idx if i > n - 120]
    rng_high = float(h[recent_sh[-1]]) if recent_sh else float(h[-60:].max())
    rng_low = float(l[recent_sl[-1]]) if recent_sl else float(l[-60:].min())
    # use the *widest* extremes of the active dealing range for equilibrium
    dh = float(h[-120:].max()); dl = float(l[-120:].min())
    eq = (dh + dl) / 2.0
    zone = "PREMIUM" if last > eq else "DISCOUNT"
    zone_pos = clamp(50.0 + 50.0 * (last - eq) / max(eq - dl, 1e-9) if last > eq
                     else 50.0 - 50.0 * (eq - last) / max(dh - eq, 1e-9), 0, 100)

    # ---------------- BOS / MSS ------------------------------------------
    breaks: list[dict] = []
    last_dir = 0
    hi_ptr = 0
    lo_ptr = 0
    for i in range(max(3, n - 150), n):
        # bullish break of the most recent unbroken swing high
        while hi_ptr < len(sh_idx) and sh_idx[hi_ptr] < i - 60:
            hi_ptr += 1
        while lo_ptr < len(sl_idx) and sl_idx[lo_ptr] < i - 60:
            lo_ptr += 1
        if hi_ptr < len(sh_idx):
            lvl = float(h[sh_idx[hi_ptr]])
            if c[i] > lvl and (c[i] - o[i]) > p.disp_atr_mult * A:
                kind = "MSS" if last_dir < 0 else "BOS"
                breaks.append({"type": kind, "dir": "bull", "t": int(t[i]),
                               "price": lvl, "idx": i})
                last_dir = 1
                hi_ptr += 1
                continue
        if lo_ptr < len(sl_idx):
            lvl = float(l[sl_idx[lo_ptr]])
            if c[i] < lvl and (o[i] - c[i]) > p.disp_atr_mult * A:
                kind = "MSS" if last_dir > 0 else "BOS"
                breaks.append({"type": kind, "dir": "bear", "t": int(t[i]),
                               "price": lvl, "idx": i})
                last_dir = -1
                lo_ptr += 1
    breaks = breaks[-6:]
    bias = "BULLISH" if last_dir > 0 else ("BEARISH" if last_dir < 0 else "NEUTRAL")

    # ---------------- Order Blocks ---------------------------------------
    obs: list[dict] = []
    for i in range(max(2, n - 120), n):
        disp_up = (c[i] - o[i]) > p.disp_atr_mult * A
        disp_dn = (o[i] - c[i]) > p.disp_atr_mult * A
        if disp_up:
            # last bearish candle before the up displacement
            j = i - 1
            if j >= 0 and c[j] < o[j]:
                top, bot = float(max(o[j], c[j]) * 1.0), float(min(l[j], c[j]))
                top = float(h[j])
                retr = (last - bot) / max(top - bot, 1e-9)
                mitigated = last < bot
                obs.append({"kind": "BULL_OB", "t": int(t[j]), "top": top, "bot": bot,
                            "idx": j, "retrace": round(float(retr), 3),
                            "valid": bool(p.ob_retrace_min <= retr <= 1.6),
                            "mitigated": bool(mitigated), "strength": round(float(
                                (c[i] - o[i]) / A), 2)})
        if disp_dn:
            j = i - 1
            if j >= 0 and c[j] > o[j]:
                top, bot = float(h[j]), float(max(o[j], c[j]))
                bot = float(l[j])
                retr = (top - last) / max(top - bot, 1e-9)
                obs.append({"kind": "BEAR_OB", "t": int(t[j]), "top": top, "bot": bot,
                            "idx": j, "retrace": round(float(retr), 3),
                            "valid": bool(p.ob_retrace_min <= retr <= 1.6),
                            "mitigated": bool(last > top), "strength": round(float(
                                (o[i] - c[i]) / A), 2)})
    obs = [o_ for o_ in obs if o_["top"] > o_["bot"]][-5:]

    # ---------------- Fair Value Gaps ------------------------------------
    fvgs: list[dict] = []
    min_gap = p.fvg_min_atr * A
    for i in range(max(2, n - 120), n):
        if l[i] > h[i - 2]:                       # bullish imbalance
            gap = float(l[i] - h[i - 2])
            if gap >= min_gap:
                top, bot = float(l[i]), float(h[i - 2])
                filled = float(min(c[i:].min() if i < n - 1 else c[i], top)) <= bot
                fvgs.append({"kind": "BULL_FVG", "t": int(t[i - 1]), "top": top,
                             "bot": bot, "gap_atr": round(gap / A, 2),
                             "filled": bool(filled), "idx": i - 1})
        if h[i] < l[i - 2]:                       # bearish imbalance
            gap = float(l[i - 2] - h[i])
            if gap >= min_gap:
                top, bot = float(l[i - 2]), float(h[i])
                filled = float(max(c[i:].max() if i < n - 1 else c[i], bot)) >= top
                fvgs.append({"kind": "BEAR_FVG", "t": int(t[i - 1]), "top": top,
                             "bot": bot, "gap_atr": round(gap / A, 2),
                             "filled": bool(filled), "idx": i - 1})
    fvgs = [f for f in fvgs if not f["filled"]][-6:]

    # ---------------- Equal highs / lows (liquidity pools) ---------------
    pools: list[dict] = []
    tol = p.eqh_tol_atr * A
    for lst, side in ((sh_idx, "EQH"), (sl_idx, "EQL")):
        lst = [i for i in lst if i > n - 150]
        for a_i in range(len(lst)):
            for b_i in range(a_i + 1, len(lst)):
                p1 = float(h[lst[a_i]] if side == "EQH" else l[lst[a_i]])
                p2 = float(h[lst[b_i]] if side == "EQH" else l[lst[b_i]])
                if abs(p1 - p2) <= tol and lst[b_i] - lst[a_i] >= 3:
                    pools.append({"kind": side, "t": int(t[lst[a_i]]),
                                  "t2": int(t[lst[b_i]]),
                                  "price": round((p1 + p2) / 2, 6),
                                  "label": "LIQUIDITY POOL", "idx": lst[a_i]})
    pools = pools[-5:]

    # ---------------- Liquidity sweeps -----------------------------------
    sweeps: list[dict] = []
    lb = p.sweep_lookback
    for i in range(max(5, n - lb), n):
        prior_hi = float(h[max(0, i - lb):i].max())
        prior_lo = float(l[max(0, i - lb):i].min())
        if h[i] > prior_hi and c[i] < prior_hi:
            sweeps.append({"dir": "bear", "t": int(t[i]), "price": float(h[i]),
                           "reclaimed": float(prior_hi), "idx": i})
        if l[i] < prior_lo and c[i] > prior_lo:
            sweeps.append({"dir": "bull", "t": int(t[i]), "price": float(l[i]),
                           "reclaimed": float(prior_lo), "idx": i})

    # ---------------- displacement / momentum ----------------------------
    ema_f = float(np.mean(c[-3:]))
    ema_s = float(np.mean(c[-20:]))
    trend = 1 if ema_f > ema_s else -1

    return {
        "ok": True,
        "atr": round(A, 8),
        "price": last,
        "structure": {
            "breaks": breaks,
            "bias": bias,
            "eq": round(eq, 6),
            "range_high": round(dh, 6),
            "range_low": round(dl, 6),
            "zone": zone,
            "zone_pos": round(zone_pos, 1),
            "last_swing_high": round(rng_high, 6),
            "last_swing_low": round(rng_low, 6),
        },
        "order_blocks": obs,
        "fvgs": fvgs,
        "pools": pools,
        "sweeps": sweeps[-4:],
        "trend": trend,
        "displacement_atr": round(float(abs(c[-1] - o[-1]) / A), 2),
    }


def ict_score(smc: dict, p: EngineParams,
              direction: str | None = None) -> tuple[float, dict]:
    """MODEL 1 - ICT RULE ENGINE (40% of the ensemble).

    Weighted rule conjunction.  Score 100 requires the full stack:
    sweep -> MSS/BOS -> OB -> FVG, all located in the correct premium/discount
    half of the dealing range, aligned with structural bias.

    `direction` ("LONG"/"SHORT") forces evaluation for that side; when omitted
    the structural bias is used.  The brain evaluates both sides and takes the
    stronger, which is how a discretionary desk actually works the book.
    """
    if not smc.get("ok"):
        return 0.0, {"reason": "no structure"}

    st = smc["structure"]
    bias = st["bias"]
    if direction == "LONG":
        bias = "BULLISH"
    elif direction == "SHORT":
        bias = "BEARISH"
    zone = st["zone"]
    A = smc["atr"]
    price = smc["price"]

    comp: dict[str, float] = {}
    notes: list[str] = []

    # 1. liquidity sweep present & aligned (25)
    sw = 0.0
    aligned_sweep = None
    for s in smc["sweeps"]:
        if bias == "BULLISH" and s["dir"] == "bull":
            sw = 25.0; aligned_sweep = s
        elif bias == "BEARISH" and s["dir"] == "bear":
            sw = 25.0; aligned_sweep = s
    if sw: notes.append("Liquidity sweep confirmed")
    comp["sweep"] = sw

    # 2. structure break / MSS (25) - MSS weighted higher than BOS
    brk = 0.0
    if st["breaks"]:
        b = st["breaks"][-1]
        if (bias == "BULLISH" and b["dir"] == "bull") or (bias == "BEARISH" and b["dir"] == "bear"):
            brk = 25.0 if b["type"] == "MSS" else 18.0
            notes.append(f"{b['type']} {'bullish' if b['dir']=='bull' else 'bearish'}")
    comp["structure_break"] = brk

    # 3. order block confluence (20) - price inside a valid, unmitigated OB
    ob_s = 0.0
    for ob in smc["order_blocks"]:
        want = "BULL_OB" if bias == "BULLISH" else "BEAR_OB"
        if ob["kind"] != want or ob["mitigated"]:
            continue
        near = ob["bot"] - 0.35 * A <= price <= ob["top"] + 0.35 * A
        if near and ob["valid"]:
            ob_s = 20.0 if ob["strength"] >= 1.4 else 16.0
            notes.append(f"{ob['kind'].replace('_',' ')} tapped")
            break
    comp["order_block"] = ob_s

    # 4. FVG confluence (15)
    fvg_s = 0.0
    for f in smc["fvgs"]:
        want = "BULL_FVG" if bias == "BULLISH" else "BEAR_FVG"
        if f["kind"] != want:
            continue
        if f["bot"] - 0.5 * A <= price <= f["top"] + 0.5 * A:
            fvg_s = 15.0
            notes.append(f"{f['kind'].replace('_',' ')} in range ({f['gap_atr']}x ATR)")
            break
    comp["fvg"] = fvg_s

    # 5. premium/discount location (15)
    pd_s = 0.0
    if (bias == "BULLISH" and zone == "DISCOUNT") or (bias == "BEARISH" and zone == "PREMIUM"):
        pd_s = 15.0
        notes.append(f"Entry in {zone.lower()} zone")
    elif abs(st.get("zone_pos", 50) - 50) < 12:
        pd_s = 6.0
        notes.append("Entry at equilibrium")
    comp["premium_discount"] = pd_s

    total = sum(comp.values())
    return round(clamp(total), 1), {
        "components": comp,
        "notes": notes,
        "bias": bias,
        "zone": zone,
        "sweep": aligned_sweep,
    }
