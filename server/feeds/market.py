"""PHASE 1 - MARKET INGESTION.

OKX public endpoints (no key required) are the live backbone.  REST is used
only for cold-start history and low-cadence state (OI, funding, liquidation
prints); everything tick-level arrives over WebSocket.

Venue note: Binance Futures / Bybit are region-blocked from this deployment.
`VENUES` documents the adapter map so a deploy in another region can switch
without touching the engine - the normalisers already produce Binance-shaped
symbols (BTCUSDT) for the UI.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Callable, Deque

import httpx
import websockets

from ..config import OKX_REST, OKX_WS_PUBLIC, UNIVERSE

VENUES = {
    "okx": {
        "status": "LIVE",
        "ws": OKX_WS_PUBLIC,
        "covers": ["candles", "trades", "depth", "open_interest",
                   "funding", "liquidation_orders"],
    },
    "binance_futures": {
        "status": "GEO_BLOCKED_451",
        "ws": "wss://fstream.binance.com/stream",
        "covers": ["candles", "trades", "depth", "open_interest",
                   "funding", "forceOrder"],
    },
    "bybit": {
        "status": "GEO_BLOCKED_403",
        "ws": "wss://stream.bybit.com/v5/public/linear",
        "covers": ["candles", "trades", "depth", "open_interest",
                   "funding", "liquidation"],
    },
}

_HEADERS = {"User-Agent": "OmniscienceTerminal/1.0"}


class OkxRest:
    def __init__(self):
        self._c: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._c is None or self._c.is_closed:
            self._c = httpx.AsyncClient(base_url=OKX_REST, timeout=12.0,
                                        headers=_HEADERS,
                                        limits=httpx.Limits(max_connections=12))
        return self._c

    async def get(self, path: str, params: dict | None = None) -> dict:
        c = await self.client()
        for attempt in range(3):
            try:
                r = await c.get(path, params=params or {})
                r.raise_for_status()
                js = r.json()
                if js.get("code") not in ("0", 0):
                    raise RuntimeError(js.get("msg", "okx error"))
                return js
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.4 * (attempt + 1))
        return {}

    async def aclose(self):
        if self._c and not self._c.is_closed:
            await self._c.aclose()

    # ---- candles -------------------------------------------------------
    async def candles(self, inst: str, bar: str = "5m", limit: int = 300,
                      after: int | None = None, history: bool = False) -> list[dict]:
        path = "/api/v5/market/history-candles" if history else "/api/v5/market/candles"
        params = {"instId": inst, "bar": bar, "limit": str(min(limit, 300))}
        if after:
            params["after"] = str(after)
        js = await self.get(path, params)
        return [self._candle(r) for r in js.get("data", [])]

    @staticmethod
    def _candle(r) -> dict:
        return {"t": int(int(r[0]) // 1000), "o": float(r[1]), "h": float(r[2]),
                "l": float(r[3]), "c": float(r[4]), "v": float(r[5]),
                "volccy": float(r[6]) if len(r) > 6 else 0.0}

    async def history(self, inst: str, bar: str, bars: int,
                      progress: Callable | None = None) -> list[dict]:
        """Paginate backwards until `bars` collected.  Returns oldest->newest."""
        out: list[dict] = []
        after = None
        seen = set()
        while len(out) < bars:
            batch = await self.candles(inst, bar, 300, after, history=True)
            if not batch:
                break
            fresh = [b for b in batch if b["t"] not in seen]
            if not fresh:
                break
            for b in fresh:
                seen.add(b["t"])
            out.extend(fresh)
            after = min(b["t"] for b in fresh) * 1000
            if progress:
                progress(len(out), bars)
            if len(batch) < 300:
                break
            await asyncio.sleep(0.12)
        out.sort(key=lambda z: z["t"])
        return out

    async def history_range(self, inst: str, bar: str, start_ms: int,
                            end_ms: int, concurrency: int = 8) -> list[dict]:
        """Fetch [start_ms, end_ms) by splitting into 300-bar windows and
        issuing them concurrently.  Used for the multi-week 1m ML dataset."""
        bar_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000}[bar]
        window = 300 * bar_ms
        spans = []
        t = start_ms - (start_ms % bar_ms)
        while t < end_ms:
            spans.append((t, min(t + window, end_ms)))
            t += window
        sem = asyncio.Semaphore(concurrency)

        async def fetch(a: int, b: int):
            async with sem:
                for attempt in range(3):
                    try:
                        js = await self.get("/api/v5/market/history-candles",
                                            {"instId": inst, "bar": bar,
                                             "after": str(b), "before": str(a - 1),
                                             "limit": "300"})
                        return [self._candle(r) for r in js.get("data", [])]
                    except Exception:
                        await asyncio.sleep(0.3 * (attempt + 1))
                return []

        chunks = await asyncio.gather(*[fetch(a, b) for a, b in spans])
        seen: dict[int, dict] = {}
        for ch in chunks:
            for c in ch:
                seen[c["t"]] = c
        return [seen[k] for k in sorted(seen)]

    # ---- state ---------------------------------------------------------
    async def tickers(self) -> list[dict]:
        js = await self.get("/api/v5/market/tickers", {"instType": "SWAP"})
        return js.get("data", [])

    async def funding(self, inst: str) -> dict:
        js = await self.get("/api/v5/public/funding-rate", {"instId": inst})
        d = (js.get("data") or [{}])[0]
        return {
            "inst": inst,
            "rate": float(d.get("fundingRate", 0) or 0),
            "next": int(d.get("fundingTime", 0) or 0),
            "ts": int(d.get("ts", 0) or 0),
        }

    async def open_interest(self, inst: str) -> dict:
        js = await self.get("/api/v5/public/open-interest",
                            {"instType": "SWAP", "instId": inst})
        d = (js.get("data") or [{}])[0]
        return {"inst": inst, "oi": float(d.get("oi", 0) or 0),
                "oi_usd": float(d.get("oiUsd", 0) or 0),
                "ts": int(d.get("ts", 0) or 0)}

    async def open_interest_all(self) -> dict[str, float]:
        js = await self.get("/api/v5/public/open-interest", {"instType": "SWAP"})
        return {d["instId"]: float(d.get("oiUsd", 0) or 0) for d in js.get("data", [])}

    async def liquidations(self, uly: str, limit: int = 100) -> list[dict]:
        """REAL liquidation orders from the venue (not a model estimate)."""
        js = await self.get("/api/v5/public/liquidation-orders",
                            {"instType": "SWAP", "uly": uly,
                             "state": "filled", "limit": str(limit)})
        rows = []
        for grp in js.get("data", []):
            inst = grp.get("instId", "")
            for d in grp.get("details", []):
                px = float(d.get("bkPx", 0) or 0)
                sz = float(d.get("sz", 0) or 0)
                # side is the *position* side that was liquidated
                side = "long" if d.get("posSide") == "long" else "short"
                rows.append({
                    "t": int(d.get("ts", 0) or 0),
                    "inst": inst, "price": px, "size": sz,
                    "usd": px * sz, "side": side,
                })
        return rows

    async def books(self, inst: str, sz: int = 50) -> dict:
        js = await self.get("/api/v5/market/books", {"instId": inst, "sz": str(sz)})
        d = (js.get("data") or [{}])[0]
        return {"asks": [[float(a[0]), float(a[1])] for a in d.get("asks", [])],
                "bids": [[float(b[0]), float(b[1])] for b in d.get("bids", [])],
                "ts": int(d.get("ts", 0) or 0)}


# ============================== websocket ===============================
class OkxStream:
    """Persistent public WS with auto-resubscribe and backoff."""

    def __init__(self, insts: list[str], on_candle: Callable, on_trade: Callable,
                 on_book: Callable | None = None):
        self.insts = insts
        self.on_candle = on_candle
        self.on_trade = on_trade
        self.on_book = on_book
        self.connected = False
        self.msgs = 0
        self.trades = 0
        self.last_msg_ts = 0
        self.reconnects = 0
        self._stop = False
        self.backoff = 1.0

    def args(self) -> list[dict]:
        focus = self.insts[0]
        a = [{"channel": "candle5m", "instId": i} for i in self.insts]
        a += [{"channel": "trades", "instId": focus},
              {"channel": "books5", "instId": focus}]
        return a

    async def run(self):
        while not self._stop:
            try:
                async with websockets.connect(
                        OKX_WS_PUBLIC, ping_interval=18, ping_timeout=12,
                        max_queue=None, close_timeout=3) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": self.args()}))
                    self.connected = True
                    self.backoff = 1.0
                    while not self._stop:
                        raw = await asyncio.wait_for(ws.recv(), timeout=45)
                        self.msgs += 1
                        self.last_msg_ts = int(time.time() * 1000)
                        msg = json.loads(raw)
                        if "event" in msg:
                            continue
                        ch = msg.get("arg", {}).get("channel")
                        data = msg.get("data", [])
                        inst = msg.get("arg", {}).get("instId", "")
                        if ch == "candle5m":
                            for r in data:
                                await self.on_candle(inst, OkxRest._candle(r))
                        elif ch == "trades":
                            for r in data:
                                self.trades += 1
                                await self.on_trade(inst, int(r["ts"]),
                                                    float(r["px"]), float(r["sz"]),
                                                    r["side"])
                        elif ch == "books5" and self.on_book and data:
                            d = data[0]
                            await self.on_book(inst, {
                                "asks": [[float(a[0]), float(a[1])] for a in d["asks"]],
                                "bids": [[float(b[0]), float(b[1])] for b in d["bids"]],
                                "ts": int(d.get("ts", 0) or 0)})
            except asyncio.CancelledError:
                raise
            except Exception:
                self.connected = False
                self.reconnects += 1
                await asyncio.sleep(self.backoff)
                self.backoff = min(self.backoff * 1.6, 20.0)

    async def stop(self):
        self._stop = True


class CoinbasePremium:
    """Spot reference feed used for the Coinbase Premium Index."""

    def __init__(self, product: str = "BTC-USD"):
        self.product = product
        self.price = 0.0
        self.bid = 0.0
        self.ask = 0.0
        self.connected = False
        self.last_ts = 0
        self._stop = False

    async def run(self):
        while not self._stop:
            try:
                async with websockets.connect("wss://ws-feed.exchange.coinbase.com",
                                              ping_interval=18) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe", "product_ids": [self.product],
                        "channels": ["ticker"]}))
                    self.connected = True
                    while not self._stop:
                        m = json.loads(await asyncio.wait_for(ws.recv(), 45))
                        if m.get("type") == "ticker":
                            self.price = float(m.get("price", 0) or 0)
                            self.bid = float(m.get("best_bid", 0) or 0)
                            self.ask = float(m.get("best_ask", 0) or 0)
                            self.last_ts = int(time.time() * 1000)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.connected = False
                await asyncio.sleep(3)

    async def stop(self):
        self._stop = True


class WhaleTape:
    """Rolling tape of large prints (> threshold USD) from the live stream."""

    def __init__(self, threshold: float = 1_000_000, maxlen: int = 60):
        self.threshold = threshold
        self.rows: Deque[dict] = deque(maxlen=maxlen)
        self.seen = deque(maxlen=2000)

    def feed(self, inst: str, ts: int, price: float, size: float, side: str):
        usd = price * size
        if usd < self.threshold:
            return
        self.rows.appendleft({"t": ts, "inst": inst, "price": price,
                              "size": size, "usd": usd,
                              "side": "BUY" if side == "buy" else "SELL"})

    def recent(self, minutes: int = 60) -> list[dict]:
        cut = int(time.time() * 1000) - minutes * 60_000
        return [r for r in self.rows if r["t"] >= cut]

    def notional(self, minutes: int = 60) -> dict:
        rows = self.recent(minutes)
        b = sum(r["usd"] for r in rows if r["side"] == "BUY")
        s = sum(r["usd"] for r in rows if r["side"] == "SELL")
        return {"buy": round(b, 0), "sell": round(s, 0),
                "net": round(b - s, 0), "count": len(rows)}
