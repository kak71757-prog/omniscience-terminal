"""PHASE 4 - THE THREE SHIELDS.

Any shield can veto a trade outright.  The veto reason is always returned as
structured data so the terminal can display exactly which god said no.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..config import SHIELDS


def killzone(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    h = now.hour + now.minute / 60.0
    for name, start, end in SHIELDS.killzones:
        if start <= h < end:
            return {"open": True, "zone": name,
                    "window": f"{start:02d}:00-{end:02d}:00 UTC",
                    "mins_left": round((end - h) * 60, 0),
                    "utc": now.strftime("%H:%M:%S")}
    # next open
    nxt = None
    for name, start, end in sorted(SHIELDS.killzones, key=lambda z: z[1]):
        if h < start:
            nxt = (name, start)
            break
    if nxt is None:
        nxt = (sorted(SHIELDS.killzones, key=lambda z: z[1])[0][0],
               sorted(SHIELDS.killzones, key=lambda z: z[1])[0][1] + 24)
    mins = (nxt[1] - h) * 60
    return {"open": False, "zone": "CLOSED", "window": "—",
            "mins_left": round(mins, 0), "utc": now.strftime("%H:%M:%S"),
            "next": nxt[0], "next_in_min": round(mins, 0)}


def anti_manipulation(funding_rate: float, price_chg_pct_15m: float,
                      oi_chg_pct_15m: float, usdt_dom_chg_1h: float,
                      side: str) -> dict:
    """Returns {"blocked": bool, "reasons": [...], "checks": {...}}"""
    reasons: list[dict] = []
    checks: dict[str, dict] = {}

    fr_pct = funding_rate * 100
    fr_block = (side == "LONG" and funding_rate > SHIELDS.funding_extreme) or \
               (side == "SHORT" and funding_rate < -SHIELDS.funding_extreme)
    checks["funding"] = {"value": round(fr_pct, 4), "unit": "%",
                         "limit": SHIELDS.funding_extreme * 100,
                         "blocked": bool(fr_block)}
    if fr_block:
        reasons.append({"code": "FUNDING_EXTREME",
                        "text": f"FUNDING EXTREME - SQUEEZE RISK ({fr_pct:+.4f}%)"})

    fake_pump = price_chg_pct_15m > 0.15 and oi_chg_pct_15m < -0.05
    fake_dump = price_chg_pct_15m < -0.15 and oi_chg_pct_15m < -0.05
    oi_block = (fake_pump and side == "LONG") or (fake_dump and side == "SHORT")
    checks["oi_divergence"] = {
        "price_chg": round(price_chg_pct_15m, 3),
        "oi_chg": round(oi_chg_pct_15m, 3), "blocked": bool(oi_block)}
    if oi_block:
        reasons.append({"code": "OI_DIVERGENCE",
                        "text": ("FAKE PUMP - REJECT" if fake_pump else
                                 "FAKE DUMP - OI UNWINDING")})

    dom_block = usdt_dom_chg_1h > SHIELDS.usdt_dom_pump_1h and side == "LONG"
    checks["stablecoin_flight"] = {"value": round(usdt_dom_chg_1h, 3),
                                   "limit": SHIELDS.usdt_dom_pump_1h * 100,
                                   "blocked": bool(dom_block)}
    if dom_block:
        reasons.append({"code": "STABLECOIN_FLIGHT",
                        "text": f"STABLECOIN FLIGHT (+{usdt_dom_chg_1h:.2f}% dominance 1h) - REJECT ALL LONGS"})

    return {"blocked": bool(reasons), "reasons": reasons, "checks": checks}


def news(blackout: dict) -> dict:
    active = blackout.get("active")
    return {
        "blocked": bool(active),
        "banner": "NEWS BLACKOUT - VOLATILITY GOD IS ANGRY" if active else "",
        "events": blackout.get("events", []),
        "upcoming": blackout.get("upcoming", []),
        "window_min": blackout.get("window_min", SHIELDS.news_window_min),
    }


def evaluate(kz: dict, am: dict, nw: dict, score: float) -> dict:
    blocks = []
    if not kz.get("open"):
        blocks.append({"code": "KILLZONE", "text": "MARKET DEAD - GOD IS SLEEPING"})
    blocks.extend(am.get("reasons", []))
    if nw.get("blocked"):
        blocks.append({"code": "NEWS", "text": nw.get("banner", "NEWS BLACKOUT")})
    final = 0.0 if blocks else score
    return {"score": round(final, 1), "raw_score": round(score, 1),
            "blocked": bool(blocks), "blocks": blocks}
