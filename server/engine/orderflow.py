"""MAP B - ORDER FLOW MAP.  Footprint, CVD and absorption/divergence.

Built from the live aggregated-trade stream (aggressor side included).  Each
5m bucket is split into price rows of bid volume (aggressive sells) and ask
volume (aggressive buys).  CVD is the running sum of signed volume; the
divergence detector flags price making a higher high while CVD makes a lower
high (distribution) and the inverse (accumulation).
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Iterable

import numpy as np

from ..util import zscore


class OrderFlow:
    def __init__(self, max_trades: int = 60000, rows: int = 18):
        self.trades: Deque[dict] = deque(maxlen=max_trades)
        self.rows = rows
        self._cvd_hist: Deque[tuple] = deque(maxlen=600)
        self.buy_vol = 0.0
        self.sell_vol = 0.0

    def add_trade(self, ts_ms: int, price: float, size: float, side: str) -> None:
        usd = price * size
        # `side` is the taker/aggressor side: buy => ask volume
        aggressive_buy = side in ("buy", "BUY", "b", "Buy")
        self.trades.append({"t": ts_ms, "p": price, "s": size, "usd": usd,
                            "b": aggressive_buy})
        if aggressive_buy:
            self.buy_vol += usd
        else:
            self.sell_vol += usd

    def add_trades_bulk(self, rows: Iterable[tuple]) -> None:
        for ts_ms, price, size, side in rows:
            self.add_trade(ts_ms, price, size, side)

    # ------------------------------------------------------------------
    def footprint(self, bucket_ms: int = 300_000, buckets: int = 60,
                  atr: float = 0.0) -> dict:
        if not self.trades:
            return {"ok": False, "candles": []}
        now = self.trades[-1]["t"]
        start = now - buckets * bucket_ms
        sel = [t for t in self.trades if t["t"] >= start]
        if not sel:
            return {"ok": False, "candles": []}

        out = []
        cvd = 0.0
        cur_bucket = None
        rows: dict[int, dict] = {}
        lo = min(t["p"] for t in sel)
        hi = max(t["p"] for t in sel)
        span = max(hi - lo, 1e-9)
        step = span / self.rows

        def flush(b_end: int):
            nonlocal cvd, rows
            if cur_bucket is None or not rows:
                return
            rlist = sorted(rows.values(), key=lambda r: r["price"])
            bid = sum(r["bid"] for r in rlist)
            ask = sum(r["ask"] for r in rlist)
            delta = ask - bid
            cvd += delta
            poc = max(rlist, key=lambda r: r["bid"] + r["ask"])
            out.append({
                "t": int(b_end // 1000),
                "bid": round(bid, 0), "ask": round(ask, 0),
                "delta": round(delta, 0), "cvd": round(cvd, 0),
                "total": round(bid + ask, 0),
                "poc_price": round(poc["price"], 6),
                "rows": [{"price": round(r["price"], 6),
                          "bid": round(r["bid"], 0), "ask": round(r["ask"], 0),
                          "delta": round(r["ask"] - r["bid"], 0)} for r in rlist],
            })
            rows = {}

        for t in sel:
            b = int(t["t"] // bucket_ms) * bucket_ms
            if cur_bucket is None:
                cur_bucket = b
            if b != cur_bucket:
                flush(cur_bucket)
                cur_bucket = b
            idx = int((t["p"] - lo) / step) if step > 0 else 0
            idx = max(0, min(self.rows - 1, idx))
            r = rows.setdefault(idx, {"price": lo + (idx + 0.5) * step,
                                      "bid": 0.0, "ask": 0.0})
            if t["b"]:
                r["ask"] += t["usd"]
            else:
                r["bid"] += t["usd"]
        flush(cur_bucket)

        # CVD divergence on the reconstructed series
        div = self._divergence(out)
        tot_bid = sum(c["bid"] for c in out)
        tot_ask = sum(c["ask"] for c in out)
        return {
            "ok": True,
            "candles": out[-40:],
            "cvd": round(cvd, 0),
            "delta_last": out[-1]["delta"] if out else 0,
            "buy_usd": round(tot_ask, 0),
            "sell_usd": round(tot_bid, 0),
            "buy_ratio": round(100 * tot_ask / (tot_bid + tot_ask), 2) if (tot_bid + tot_ask) else 50.0,
            "divergence": div,
            "bucket_ms": bucket_ms,
        }

    def _divergence(self, candles: list[dict]) -> dict:
        if len(candles) < 12:
            return {"state": "NONE", "strength": 0.0, "text": "Insufficient trade history"}
        px = [c["poc_price"] for c in candles[-14:]]
        cv = [c["cvd"] for c in candles[-14:]]
        n = len(px)
        half = n // 2
        p_slope = (px[-1] - px[half]) / max(abs(px[half]), 1e-9)
        c_slope = (cv[-1] - cv[half])
        scale = max(abs(cv[-1] - cv[0]), 1e-9)
        c_norm = c_slope / scale
        strength = round(min(1.0, abs(p_slope) * 400 + abs(c_norm)), 3)
        if p_slope > 0.0004 and c_norm < -0.05:
            return {"state": "BEARISH", "strength": strength,
                    "text": "Price up / CVD down - distribution, buyers absorbed"}
        if p_slope < -0.0004 and c_norm > 0.05:
            return {"state": "BULLISH", "strength": strength,
                    "text": "Price down / CVD up - accumulation, sellers absorbed"}
        return {"state": "NONE", "strength": strength, "text": "CVD aligned with price"}

    # ------------------------------------------------------------------
    def cvd_series(self, bucket_ms: int = 300_000, buckets: int = 90) -> list[dict]:
        fp = self.footprint(bucket_ms, buckets)
        return [{"t": c["t"], "cvd": c["cvd"]} for c in fp.get("candles", [])]

    def imbalance(self, window_s: float = 60.0) -> float:
        """Aggressor imbalance over the trailing window, in [-1, 1]."""
        if not self.trades:
            return 0.0
        cutoff = self.trades[-1]["t"] - window_s * 1000
        b = s = 0.0
        for t in reversed(self.trades):
            if t["t"] < cutoff:
                break
            if t["b"]:
                b += t["usd"]
            else:
                s += t["usd"]
        return 0.0 if (b + s) == 0 else (b - s) / (b + s)

    def recent_usd_flow(self, window_s: float = 60.0) -> dict:
        if not self.trades:
            return {"buy": 0.0, "sell": 0.0, "net": 0.0, "trades": 0}
        cutoff = self.trades[-1]["t"] - window_s * 1000
        b = s = 0.0; n = 0
        for t in reversed(self.trades):
            if t["t"] < cutoff:
                break
            n += 1
            if t["b"]:
                b += t["usd"]
            else:
                s += t["usd"]
        return {"buy": round(b, 0), "sell": round(s, 0),
                "net": round(b - s, 0), "trades": n}
