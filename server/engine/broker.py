"""PHASE 5 - EXECUTION.

Mode A PAPER: fills are simulated against the live tape with fees + slippage,
triple-TP ladder, break-even stop after TP1 and ATR trailing after TP2.
Mode B PROP: same engine, but the risk module's hard rules gate every order.

Every state transition writes to SQLite and to the AI self-journal, so the
journal is a real record of what happened rather than prose.
"""
from __future__ import annotations

import json
import time

from ..config import RISK, UNIVERSE
from ..store import db
from ..util import fmt_usd


class Broker:
    def __init__(self):
        self.positions: dict[int, dict] = {}
        self._load()

    # ------------------------------------------------------------ state --
    def _load(self):
        for t in db.open_trades():
            try:
                legs = json.loads(t.get("legs") or "[]")
            except Exception:
                legs = []
            try:
                feats = json.loads(t.get("features") or "{}")
            except Exception:
                feats = {}
            self.positions[t["id"]] = {
                **t, "legs": legs, "features": feats,
                "remaining": 100.0 - sum(l.get("pct", 0) for l in legs),
                "high_water": t["entry"], "low_water": t["entry"],
            }

    def has_position(self, symbol: str) -> bool:
        return any(p["symbol"] == symbol for p in self.positions.values())

    # ------------------------------------------------------------- open --
    def open(self, symbol: str, side: str, entry: float, sl: float,
             tp: dict, qty: float, risk_usd: float, score: float,
             score_parts: dict, features: dict, mode: str | None = None,
             reason: str = "") -> int:
        tid = db.insert_trade({
            "symbol": symbol, "side": side, "mode": mode or RISK.mode,
            "entry": entry, "sl": sl, "tp1": tp["tp1"], "tp2": tp["tp2"],
            "tp3": tp["tp3"], "qty": qty, "risk_usd": risk_usd, "score": score,
            "score_parts": score_parts, "features": features,
            "opened_at": int(time.time() * 1000),
            "legs": [], "reason": reason,
        })
        self.positions[tid] = {
            "id": tid, "symbol": symbol, "side": side, "entry": entry,
            "sl": sl, "tp1": tp["tp1"], "tp2": tp["tp2"], "tp3": tp["tp3"],
            "qty": qty, "risk_usd": risk_usd, "score": score,
            "score_parts": score_parts, "features": features,
            "legs": [], "remaining": 100.0, "high_water": entry,
            "low_water": entry, "sl_to_be": 0,
            "opened_at": int(time.time() * 1000),
        }
        base = UNIVERSE.base(symbol)
        db.journal_add(
            "ENTRY",
            f"ENTERED {side} {base} @ {entry:.4f} | SL {sl:.4f} | TP1 {tp['tp1']:.4f} "
            f"| risk {fmt_usd(risk_usd)} | Divine Score {score:.1f} "
            f"({score_parts.get('ict',0):.0f} ICT / {score_parts.get('lstm',0):.0f} LSTM / "
            f"{score_parts.get('intent',0):.0f} INTENT). {reason}",
            tid, score_parts)
        return tid

    # ------------------------------------------------------------- tick --
    def on_price(self, symbol: str, price: float) -> list[dict]:
        events = []
        for tid, p in list(self.positions.items()):
            if p["symbol"] != symbol:
                continue
            p["high_water"] = max(p["high_water"], price)
            p["low_water"] = min(p["low_water"], price)
            long_ = p["side"] == "LONG"
            # --- stop loss first (conservative: SL beats TP on the same tick)
            hit_sl = (price <= p["sl"]) if long_ else (price >= p["sl"])
            if hit_sl:
                ev = self._close(tid, p["sl"], "TRAILING STOP" if p["sl_to_be"] else "STOP LOSS")
                events.append(ev)
                continue
            # --- TP ladder
            for leg in ("tp1", "tp2", "tp3"):
                if any(l["name"] == leg.upper() for l in p["legs"]):
                    continue
                lvl = p[leg]
                reached = (price >= lvl) if long_ else (price <= lvl)
                if not reached:
                    break
                pct = {"tp1": 50, "tp2": 30, "tp3": 20}[leg]
                p["legs"].append({"name": leg.upper(), "pct": pct, "price": lvl,
                                  "hit_at": int(time.time() * 1000)})
                p["remaining"] = max(0.0, p["remaining"] - pct)
                db.update_trade(tid, legs=json.dumps(p["legs"]))
                events.append({"kind": "LEG", "tid": tid, "leg": leg.upper(),
                               "price": lvl, "pct": pct})
                if leg == "tp1":
                    p["sl"] = p["entry"]; p["sl_to_be"] = 1
                    db.update_trade(tid, sl=p["sl"], sl_to_be=1)
                    db.journal_add("MANAGE", f"{UNIVERSE.base(symbol)} TP1 hit at {lvl:.4f} "
                                   f"-> 50% booked, stop moved to break-even.", tid)
                elif leg == "tp2":
                    atr_ = float(p["features"].get("atr") or price * 0.004)
                    trail = price - 1.5 * atr_ if long_ else price + 1.5 * atr_
                    if (long_ and trail > p["sl"]) or ((not long_) and trail < p["sl"]):
                        p["sl"] = round(trail, 6)
                        db.update_trade(tid, sl=p["sl"])
                    db.journal_add("MANAGE", f"{UNIVERSE.base(symbol)} TP2 hit at {lvl:.4f} "
                                   f"-> 30% booked, runner on 1.5xATR trail.", tid)
                if p["remaining"] <= 0:
                    events.append(self._close(tid, lvl, "TRIPLE TP COMPLETE"))
                    break
            # --- trailing after TP2
            if tid in self.positions and p["sl_to_be"] and any(
                    l["name"] == "TP2" for l in p["legs"]):
                atr_ = float(p["features"].get("atr") or price * 0.004)
                trail = price - 1.8 * atr_ if long_ else price + 1.8 * atr_
                if (long_ and trail > p["sl"]) or ((not long_) and trail < p["sl"]):
                    p["sl"] = round(trail, 6)
                    db.update_trade(tid, sl=p["sl"])
        return events

    # ------------------------------------------------------------ close --
    def _close(self, tid: int, price: float, reason: str) -> dict:
        p = self.positions.pop(tid)
        long_ = p["side"] == "LONG"
        sgn = 1 if long_ else -1
        remaining = p["remaining"]
        pnl = 0.0
        r_mult = 0.0
        for leg in p["legs"]:
            q = p["qty"] * leg["pct"] / 100.0
            pnl += (leg["price"] - p["entry"]) * sgn * q
        if remaining > 0:
            q = p["qty"] * remaining / 100.0
            pnl += (price - p["entry"]) * sgn * q
        # fees + slippage on full notional, both sides
        notional = p["qty"] * p["entry"]
        cost = notional * (RISK.fee_bps * 2 + RISK.slippage_bps * 2) / 10_000.0
        pnl -= cost
        r_mult = pnl / p["risk_usd"] if p["risk_usd"] else 0.0
        db.update_trade(tid, status="CLOSED", closed_at=int(time.time() * 1000),
                        exit_price=price, realised_pnl=round(pnl, 4),
                        r_multiple=round(r_mult, 4), close_note=reason)
        db.equity_add(RISK.capital + db.trade_stats()["net_pnl"], 0)
        self._reflect(tid, p, price, pnl, r_mult, reason)
        return {"kind": "CLOSE", "tid": tid, "price": price, "pnl": round(pnl, 4),
                "r": round(r_mult, 3), "reason": reason,
                "symbol": p["symbol"], "side": p["side"]}

    def _reflect(self, tid: int, p: dict, price: float, pnl: float,
                 r: float, reason: str):
        """The self-journal: a post-trade attribution written from the stored
        feature snapshot, not a template with the number swapped in."""
        f = p.get("features") or {}
        sp = p.get("score_parts") or {}
        base = UNIVERSE.base(p["symbol"])
        causes = []
        if pnl < 0:
            if f.get("zone") == "PREMIUM" and p["side"] == "LONG":
                causes.append("entered longs in PREMIUM (bought at supply)")
            if f.get("zone") == "DISCOUNT" and p["side"] == "SHORT":
                causes.append("shorted into DISCOUNT (sold at demand)")
            if abs(f.get("oi_chg", 0)) < 0.02 and f.get("funding", 0) != 0:
                causes.append("OI flat at entry - move lacked fresh positioning")
            if not f.get("sweep"):
                causes.append("no liquidity sweep preceded entry")
            if f.get("cvd_div") == "BEARISH" and p["side"] == "LONG":
                causes.append("ignored bearish CVD divergence")
            if f.get("cvd_div") == "BULLISH" and p["side"] == "SHORT":
                causes.append("ignored bullish CVD divergence")
            if f.get("in_value_area") and sp.get("ict", 0) > 60:
                causes.append("entered inside value area - poor R:R from the start")
            text = (f"Trade #{tid} {base} {p['side']} closed {r:+.2f}R at {price:.4f} ({reason}). "
                    f"Loss attribution: " + ("; ".join(causes) if causes else
                    "adverse selection beyond model horizon - no rule violation logged") + ".")
            fix = self._fix_for(causes)
            if fix:
                text += f" Next time: {fix}."
        else:
            good = [k for k in ("sweep", "structure_break", "order_block", "fvg",
                                "premium_discount") if (sp.get("components") or {}).get(k, 0) > 0]
            text = (f"Trade #{tid} {base} {p['side']} closed {r:+.2f}R at {price:.4f} ({reason}). "
                    f"Confluences that paid: {', '.join(good) if good else 'momentum only'}. "
                    f"ICTS {sp.get('ict',0):.0f} / LSTM {sp.get('lstm',0):.0f} / "
                    f"INTENT {sp.get('intent',0):.0f}.")
        db.journal_add("REVIEW" if pnl < 0 else "WIN", text, tid,
                       {"r": round(r, 3), "pnl": round(pnl, 2), "causes": causes})

    @staticmethod
    def _fix_for(causes: list[str]) -> str:
        m = {
            "entered longs in PREMIUM (bought at supply)": "require zone==DISCOUNT for longs",
            "shorted into DISCOUNT (sold at demand)": "require zone==PREMIUM for shorts",
            "OI flat at entry - move lacked fresh positioning": "raise the OI-weight in the intent model",
            "no liquidity sweep preceded entry": "make the sweep component mandatory",
            "ignored bearish CVD divergence": "veto longs while CVD diverges bearish",
            "ignored bullish CVD divergence": "veto shorts while CVD diverges bullish",
            "entered inside value area - poor R:R from the start": "require POC-to-target distance > 1.5x ATR",
        }
        return "; ".join(m[c] for c in causes if c in m)

    # ----------------------------------------------------------- manual --
    def close_manual(self, tid: int, price: float) -> dict | None:
        if tid not in self.positions:
            return None
        return self._close(tid, price, "MANUAL CLOSE")

    def summary(self) -> list[dict]:
        out = []
        for p in self.positions.values():
            out.append({
                "id": p["id"], "symbol": p["symbol"], "side": p["side"],
                "entry": p["entry"], "sl": p["sl"], "tp1": p["tp1"],
                "tp2": p["tp2"], "tp3": p["tp3"], "qty": p["qty"],
                "risk_usd": p["risk_usd"], "score": p["score"],
                "legs": p["legs"], "remaining": p["remaining"],
                "sl_to_be": p["sl_to_be"],
                "opened_at": p.get("opened_at"),
            })
        return out
