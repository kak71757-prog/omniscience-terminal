"""OMNISCIENCE HUB - the global brain's runtime.

Owns every live feed, the analysis pipeline, the broker, and the evolution
loops.  The FastAPI layer is a thin shell around this object.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
import traceback
from collections import defaultdict, deque
from typing import Deque

import numpy as np

from .config import (BACKTEST_THRESHOLD, BOOTSTRAP_5M, PARAMS, RISK, SHIELDS,
                     UNIVERSE, EngineParams)
from .engine import ict as ict_engine
from .engine import liquidity, profile
from .engine.brain import Brain
from .engine.broker import Broker
from .engine.ml import IntentClassifier, DivineLSTM, train_all, build_features
from .engine.orderflow import OrderFlow
from .engine.risk import ladder, prop_check, size_position
from .engine.shields import anti_manipulation, evaluate, killzone, news
from .feeds.context import ContextFeeds
from .feeds.market import (VENUES, CoinbasePremium, OkxRest, OkxStream,
                           WhaleTape)
from .store import db
from .util import atr, pct, rolling_corr

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}


class Hub:
    def __init__(self):
        self.rest = OkxRest()
        self.ctx = ContextFeeds()
        self.broker = Broker()
        self.brain = Brain()
        self.params = PARAMS
        self.of = OrderFlow()
        self.whales = WhaleTape(threshold=1_000_000)
        self.coinbase = CoinbasePremium()

        self.c5: dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=900))
        self.c1: dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=45_000))
        self.last: dict[str, float] = {}
        self.oi: dict[str, float] = {}
        self.oi_hist: dict[str, Deque[tuple]] = defaultdict(lambda: deque(maxlen=400))
        self.funding: dict[str, dict] = {}
        self.liqs: Deque[dict] = deque(maxlen=20000)
        self.book: dict | None = None
        self.ticker_prev: dict[str, float] = {}
        self.ml_ready = False
        self.training = False
        self.train_progress = {"bars": 0, "target": 0, "stage": "idle"}
        self.started = int(time.time() * 1000)
        self.cycle = 0
        self.errors: dict[str, str] = {}
        self.history_note = ""
        self.stream: OkxStream | None = None
        self._tasks: list[asyncio.Task] = []
        self._dirty_maps = True
        self._cache_maps: dict = {}
        self._cache_map_ts = 0
        self._last_veto_note = 0.0
        self._cooldown: dict[str, float] = {}
        self._metrics = {"ws_msgs": 0, "ws_reconnects": 0, "push": 0,
                         "analysis_ms": 0.0, "latency_ms": 0}
        self._focus_smc: dict = {}
        self._scan: list[dict] = []
        self._eq_saved = 0

    # ================================================================ boot
    async def start(self):
        await self._bootstrap()
        self._tasks = [
            asyncio.create_task(self._guard(self._ws_loop, "ws")),
            asyncio.create_task(self._guard(self._coinbase_loop, "coinbase")),
            asyncio.create_task(self._guard(self._ticker_loop, "tickers")),
            asyncio.create_task(self._guard(self._oi_loop, "open_interest")),
            asyncio.create_task(self._guard(self._funding_loop, "funding")),
            asyncio.create_task(self._guard(self._liq_loop, "liquidations")),
            asyncio.create_task(self._guard(self._macro_loop, "macro")),
            asyncio.create_task(self._guard(self._sentiment_loop, "sentiment")),
            asyncio.create_task(self._guard(self._news_loop, "news")),
            asyncio.create_task(self._guard(self._vol_loop, "volatility")),
            asyncio.create_task(self._guard(self._autotrade_loop, "autotrade")),
            asyncio.create_task(self._guard(self._train_loop, "ml")),
            asyncio.create_task(self._guard(self._bar1m_loop, "bars1m")),
            asyncio.create_task(self._guard(self._equity_loop, "equity")),
        ]

    async def set_focus(self, symbol: str) -> dict:
        """Rewire the tape to a different instrument.  The order-flow engine and
        whale tape are per-instrument, so they are rebuilt rather than mixed."""
        if symbol not in UNIVERSE.symbols:
            raise ValueError(f"unknown instrument {symbol}")
        if symbol == UNIVERSE.focus:
            return {"ok": True, "focus": symbol, "changed": False}
        old = UNIVERSE.focus
        UNIVERSE.focus = symbol
        if self.stream:
            await self.stream.stop()
            await asyncio.sleep(0.3)
        self.of = OrderFlow()
        self.whales = WhaleTape(threshold=1_000_000)
        self.book = None
        self._fp_cache = None
        self._prof_cache = None
        if len(self.c1[symbol]) < 500:
            try:
                await self._load_1m(symbol, 5.0)
            except Exception:
                pass
        try:
            self.book = await self.rest.books(symbol, 50)
        except Exception:
            pass
        self.stream = OkxStream(UNIVERSE.symbols, self.on_candle, self.on_trade,
                                self.on_book)
        self._tasks.append(asyncio.create_task(self._guard(self.stream.run, "ws")))
        db.journal_add("SYSTEM", f"Focus switched {UNIVERSE.base(old)} -> "
                       f"{UNIVERSE.base(symbol)}; tape, order flow and depth "
                       f"resubscribed to the live feed.")
        return {"ok": True, "focus": symbol, "changed": True}

    async def stop(self):
        if self.stream:
            await self.stream.stop()
        await self.coinbase.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.rest.aclose()
        await self.ctx.aclose()

    async def _guard(self, fn, name: str):
        """Supervision: a crashing loop must never take the terminal down."""
        while True:
            try:
                await fn()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.errors[name] = f"{type(e).__name__}: {e}"[:200]
                traceback.print_exc()
                await asyncio.sleep(3)

    async def _bootstrap(self):
        # ---- validate the universe against the venue's live instrument list
        try:
            js = await self.rest.get("/api/v5/public/instruments", {"instType": "SWAP"})
            live = {d["instId"] for d in js.get("data", []) if d.get("state") != "suspend"}
            dropped = [s for s in UNIVERSE.symbols if s not in live]
            if dropped:
                UNIVERSE.symbols = [s for s in UNIVERSE.symbols if s in live]
                db.journal_add("SYSTEM", f"Universe validated against venue: "
                               f"dropped {', '.join(dropped)} (not listed).")
        except Exception as e:
            self.errors["universe"] = str(e)[:120]

        # ---- 5m history for the whole scanner universe
        async def load(sym):
            bars = await self.rest.history(sym, "5m", BOOTSTRAP_5M)
            self.c5[sym].clear()
            for b in bars:
                self.c5[sym].append(b)
            if bars:
                self.last[sym] = bars[-1]["c"]
        await asyncio.gather(*[load(s) for s in UNIVERSE.symbols],
                             return_exceptions=True)

        # ---- 1m history for the ML focus instrument
        days = float(os.getenv("OMNI_ML_1M_DAYS", "30"))
        self.train_progress = {"bars": 0, "target": int(days * 1440),
                               "stage": "loading 1m history"}
        end = int(time.time() * 1000)
        start = end - int(days * 86_400_000)
        bars1 = await self.rest.history_range(UNIVERSE.focus, "1m", start, end,
                                              concurrency=8)
        self.c1[UNIVERSE.focus].clear()
        for b in bars1:
            self.c1[UNIVERSE.focus].append(b)
        self.train_progress["bars"] = len(bars1)
        self.history_note = (f"{len(bars1)} x 1m bars ({len(bars1)/1440:.1f} days) "
                             f"loaded for {UNIVERSE.focus}")

        # ---- state snapshots
        await self._oi_loop_once()
        await asyncio.gather(*[self._funding_once(s) for s in UNIVERSE.symbols],
                             return_exceptions=True)
        await self._liq_once()
        await self.ctx.seed_macro_history()
        await self.ctx.poll_fomc()
        await self.ctx.poll_macro()
        book = await self.rest.books(UNIVERSE.focus, 50)
        self.book = book
        db.journal_add("SYSTEM",
                       f"Terminal online. Feeds: OKX public WS (candles/trades/depth/"
                       f"liquidations), Coinbase spot, CNBC macro, DefiLlama stablecoins, "
                       f"CoinGecko dominance, Alternative.me F&G, ForexFactory calendar, "
                       f"CoinTelegraph RSS. {self.history_note}.")

    # ============================================================ ML train
    async def _load_1m(self, symbol: str, days: float) -> list[dict]:
        end = int(time.time() * 1000)
        start = end - int(days * 86_400_000)
        bars = await self.rest.history_range(symbol, "1m", start, end, concurrency=8)
        dq = self.c1[symbol]
        dq.clear()
        for b in bars:
            dq.append(b)
        return bars

    async def _train_loop(self):
        while len(self.c1[UNIVERSE.focus]) < 2000:
            await asyncio.sleep(2)
        self.training = True
        self.train_progress["stage"] = "training LSTM + intent classifier"
        try:
            lstm, clf, info = await asyncio.to_thread(
                train_all, list(self.c1[UNIVERSE.focus]),
                lambda m: print("[ml]", m, flush=True))
            self.brain.attach(UNIVERSE.focus, lstm, clf,
                              {**info, "symbol": UNIVERSE.focus,
                               "status": "trained",
                               "trained_at": int(time.time() * 1000)})
            self.ml_ready = True
            self.train_progress["stage"] = "ready"
            db.journal_add(
                "ML",
                (f"Models retrained on {info['bars_1m']} x 1m bars "
                 f"({info['bars_1m']/1440:.1f} days). LSTM {info['windows']} windows, "
                 f"val MSE {info['lstm_val_mse']}, 15-bar directional accuracy "
                 f"{round(100*(info.get('lstm_dir_acc') or 0),1)}%. Intent classifier "
                 f"({(info.get('intent') or {}).get('family','n/a')}) AUC "
                 f"{(info.get('intent') or {}).get('auc')} on a 20% holdout, "
                 f"base rate {(info.get('intent') or {}).get('base_rate')}. "
                 f"Training took {info['train_seconds']}s."),
                metrics=info)
            # per-symbol models, trained in the background so the terminal
            # stays interactive while the fleet comes online
            self.train_progress["stage"] = "training per-symbol models"
            for sym in UNIVERSE.symbols:
                if sym == UNIVERSE.focus:
                    continue
                try:
                    bars = await self._load_1m(sym, 10.0)
                    if len(bars) < 3000:
                        continue
                    lstm_s, _, info_s = await asyncio.to_thread(
                        train_all, bars, lambda m: None)
                    self.brain.attach(sym, lstm_s, None,
                                      {**info_s, "symbol": sym,
                                       "status": "trained",
                                       "trained_at": int(time.time() * 1000)})
                    self.train_progress["stage"] = (
                        f"models ready: {len(self.brain.lstms)}/{len(UNIVERSE.symbols)}")
                    db.journal_add(
                        "ML", f"{UNIVERSE.base(sym)} model online: "
                        f"{len(bars)} x 1m bars, LSTM dir-acc "
                        f"{round(100*(info_s.get('lstm_dir_acc') or 0),1)}%.")
                except Exception as e:
                    self.errors[f"ml_{sym}"] = str(e)[:160]
            self.train_progress["stage"] = "ready (all models)"
            # retrain the LSTM hourly on the freshest tape
            while True:
                await asyncio.sleep(3600)
                self.train_progress["stage"] = "retraining"
                lstm, clf, info = await asyncio.to_thread(
                    train_all, list(self.c1[UNIVERSE.focus]), lambda m: None)
                self.brain.attach(UNIVERSE.focus, lstm, clf,
                                  {"status": "trained", **info})
                db.journal_add("ML", f"Scheduled retrain complete: LSTM val MSE "
                               f"{info['lstm_val_mse']}, intent AUC "
                               f"{(info.get('intent') or {}).get('auc')}.")
        except Exception as e:
            self.training = False
            db.journal_add("ML", f"Training failed: {type(e).__name__}: {e}")
            raise
        self.training = False

    # ============================================================= streams
    async def _ws_loop(self):
        self.stream = OkxStream(UNIVERSE.symbols, self.on_candle, self.on_trade,
                                self.on_book)
        await self.stream.run()

    async def _coinbase_loop(self):
        await self.coinbase.run()

    async def on_candle(self, inst: str, bar: dict):
        dq = self.c5[inst]
        if dq and dq[-1]["t"] == bar["t"]:
            dq[-1] = bar
        elif not dq or bar["t"] > dq[-1]["t"]:
            dq.append(bar)
        self.last[inst] = bar["c"]
        self._dirty_maps = True
        if inst == UNIVERSE.focus:
            b1 = {**bar, "t": bar["t"]}      # rolling 5m proxy appended to 1m tail
            # the 1m series is maintained by REST polling; here we just keep the
            # live forming bar visible to the LSTM feature builder
            if self.c1 and bar["t"] >= self.c1[-1]["t"] + 300:
                pass

    async def on_trade(self, inst: str, ts: int, px: float, sz: float, side: str):
        if inst != UNIVERSE.focus:
            return
        self.of.add_trade(ts, px, sz, side)
        self.whales.feed(inst, ts, px, sz, side)
        price = px
        self.last[inst] = price
        self._on_price(inst, price)

    async def on_book(self, inst: str, book: dict):
        if inst == UNIVERSE.focus:
            self.book = book

    def _on_price(self, inst: str, price: float):
        events = self.broker.on_price(inst, price)
        for ev in events:
            self._dirty_maps = True

    # ============================================================== polls
    async def _ticker_loop(self):
        while True:
            try:
                rows = await self.rest.tickers()
                for r in rows:
                    inst = r.get("instId")
                    if inst not in self.c5:
                        continue
                    px = float(r.get("last", 0) or 0)
                    if px:
                        self.last[inst] = px
                self.errors.pop("tickers", None)
            except Exception as e:
                self.errors["tickers"] = str(e)[:140]
            await asyncio.sleep(4)

    async def _oi_loop_once(self):
        try:
            alloi = await self.rest.open_interest_all()
            now = int(time.time() * 1000)
            for s in UNIVERSE.symbols:
                if s in alloi:
                    self.oi[s] = alloi[s]
                    self.oi_hist[s].append((now, alloi[s]))
            self.errors.pop("open_interest", None)
        except Exception as e:
            self.errors["open_interest"] = str(e)[:140]

    async def _oi_loop(self):
        while True:
            await self._oi_loop_once()
            await asyncio.sleep(15)

    async def _funding_once(self, inst: str):
        try:
            f = await self.rest.funding(inst)
            self.funding[inst] = f
        except Exception as e:
            self.errors["funding"] = str(e)[:140]

    async def _funding_loop(self):
        while True:
            await asyncio.gather(*[self._funding_once(s) for s in UNIVERSE.symbols],
                                 return_exceptions=True)
            await asyncio.sleep(60)

    async def _liq_once(self):
        try:
            rows = await self.rest.liquidations("BTC-USDT", 100)
            rows += await self.rest.liquidations("ETH-USDT", 100)
            have = {r["t"] for r in self.liqs}
            for r in rows:
                if r["t"] not in have:
                    self.liqs.append(r)
            self.liqs = deque(sorted(self.liqs, key=lambda z: z["t"]), maxlen=20000)
            self.errors.pop("liquidations", None)
        except Exception as e:
            self.errors["liquidations"] = str(e)[:140]

    async def _liq_loop(self):
        while True:
            await self._liq_once()
            await asyncio.sleep(20)

    async def _macro_loop(self):
        n = 0
        while True:
            await self.ctx.poll_macro()
            if n % 20 == 0:
                await self.ctx.poll_fomc()
            n += 1
            await asyncio.sleep(30)

    async def _sentiment_loop(self):
        n = 0
        while True:
            if n % 10 == 0:
                await self.ctx.poll_fear_greed()
            if n % 20 == 0:
                await self.ctx.poll_stablecoins()
            await self.ctx.poll_dominance()
            n += 1
            await asyncio.sleep(60)

    async def _news_loop(self):
        n = 0
        while True:
            if n % 3 == 0:
                await self.ctx.poll_calendar()
            await self.ctx.poll_rss()
            n += 1
            await asyncio.sleep(300)

    async def _bar1m_loop(self):
        """Roll the 1m series forward from the 5m feed + periodic REST repair."""
        while True:
            for sym in UNIVERSE.symbols:
                if not self.brain.modelled(sym):
                    continue
                try:
                    bars = await self.rest.candles(sym, "1m", 30)
                    dq = self.c1[sym]
                    for b in bars:
                        if not dq or b["t"] > dq[-1]["t"]:
                            dq.append(b)
                        elif dq and dq[-1]["t"] == b["t"]:
                            dq[-1] = b
                except Exception:
                    pass
            await asyncio.sleep(45)

    async def _vol_loop(self):
        while True:
            candles = list(self.c5[UNIVERSE.focus])
            if len(candles) > 60:
                self.brain.volatility_loop(candles)
                stats = db.trade_stats()
                if stats["trades"] >= 12 or self.brain.evolution["generation"] > 0:
                    self.brain.maybe_start_evolution(candles)
            await asyncio.sleep(60)

    async def _equity_loop(self):
        while True:
            stats = db.trade_stats()
            eq = RISK.capital + stats["net_pnl"]
            db.equity_add(eq, stats["max_dd"])
            await asyncio.sleep(60)

    # ========================================================== analysis
    def _ctx_for(self, symbol: str) -> dict:
        cs = list(self.c5[symbol])
        chg15 = 0.0
        if len(cs) > 4:
            chg15 = 100 * pct(cs[-1]["c"], cs[-4]["c"])
        oi_now = self.oi.get(symbol, 0.0)
        oi_chg = 0.0
        h = self.oi_hist.get(symbol)
        if h and len(h) > 1 and oi_now:
            cut = h[-1][0] - 900_000
            base = next((x for x in reversed(h) if x[0] <= cut), h[0])
            if base[1]:
                oi_chg = 100 * pct(oi_now, base[1])
        f = self.funding.get(symbol, {})
        nw = self.ctx.news_blackout()
        div = self.of._divergence(self.of.footprint(30_000, 40).get("candles", [])) \
            if symbol == UNIVERSE.focus and self.of.trades else {"state": "NONE"}
        return {
            "chg_pct": round(chg15, 3),
            "chg_15m_pct": round(chg15, 3),
            "oi_chg_15m_pct": round(oi_chg, 3),
            "funding": f.get("rate", 0.0),
            "usdt_dom_chg_1h": self.ctx.dominance_change_1h(),
            "news": nw,
            "cvd_div": div.get("state", "NONE"),
        }

    def analyse_focus(self) -> dict:
        t0 = time.perf_counter()
        sym = UNIVERSE.focus
        candles = list(self.c5[sym])
        smc = ict_engine.detect(candles, self.params) if len(candles) > 60 else {"ok": False}
        self._focus_smc = smc
        price = self.last.get(sym) or (candles[-1]["c"] if candles else 0.0)
        ctx = self._ctx_for(sym)

        now = time.time()
        if now - getattr(self, "_fp_ts", 0) > 1.5 and self.of.trades:
            self._fp_cache = self.of.footprint(30_000, 120)
            self._fp_ts = now
        if now - getattr(self, "_prof_ts", 0) > 5:
            self._prof_cache = profile.build(candles)
            self._prof_ts = now
        liq = liquidity.liquidation_map(price, self.oi.get(sym, 0.0), candles,
                                        list(self.liqs))
        prof = getattr(self, "_prof_cache", None) or profile.build(candles)
        shel = liquidity.bsl_ssl(smc, candles, liq, price) if smc.get("ok") else {"bsl": [], "ssl": []}
        fp = getattr(self, "_fp_cache", None) if self.of.trades else {"ok": False, "candles": []}
        if not fp:
            fp = {"ok": False, "candles": []}
        divine = self.brain.divine(smc, list(self.c1[sym]), ctx, sym) if smc.get("ok") else {
            "score": 0, "direction": "NONE", "parts": {}, "sides": [], "lstm": None}
        shields = self.brain.shield_stack(ctx, divine.get("direction", "LONG"),
                                          divine.get("score", 0))
        ticket = (self.brain.build_ticket(sym, divine["direction"], smc, divine,
                                          list(self.c1[sym]), ctx)
                  if smc.get("ok") else None)

        maps = {
            "liq_map": liq,
            "profile": prof,
            "liquidity": shel,
            "smc": smc,
            "footprint": fp,
            "prediction": self._prediction_map(smc, divine, candles, price),
            "orderbook": self._book_view(),
        }
        self._cache_maps = maps
        self._cache_map_ts = int(time.time() * 1000)
        self._metrics["analysis_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return {"smc": smc, "ctx": ctx, "divine": divine, "shields": shields,
                "ticket": ticket, "maps": maps, "price": price}

    def _prediction_map(self, smc: dict, divine: dict, candles: list[dict],
                        price: float) -> dict:
        fc = divine.get("lstm")
        if not fc:
            return {"ok": False}
        step = 60
        t_now = candles[-1]["t"] if candles else int(time.time())
        pts = [{"t": t_now + (i + 1) * step, "p": p} for i, p in enumerate(fc["prices"])]
        A = smc.get("atr") or price * 0.003
        band = [{"t": pt["t"],
                 "hi": pt["p"] + A * 0.55 * math.sqrt(i + 1) / 1.4,
                 "lo": pt["p"] - A * 0.55 * math.sqrt(i + 1) / 1.4}
                for i, pt in enumerate(pts)]
        return {
            "ok": True,
            "path": pts,
            "band": band,
            "direction": fc["direction"],
            "cum_return_pct": fc["cum_return_pct"],
            "confidence": fc["confidence"],
            "val_mse": fc["val_mse"],
            "dir_acc": fc["dir_acc"],
            "horizon_min": 15,
            "intent_prob": divine.get("intent_prob"),
        }

    def _book_view(self, levels: int = 20) -> dict:
        b = self.book or {"bids": [], "asks": []}
        bids = [tuple(x) for x in b.get("bids", [])][:levels]
        asks = [tuple(x) for x in b.get("asks", [])][:levels]
        bid_usd = sum(p * q for p, q in bids)
        ask_usd = sum(p * q for p, q in asks)
        return {
            "bids": [{"p": p, "q": q, "usd": round(p * q, 0)} for p, q in bids],
            "asks": [{"p": p, "q": q, "usd": round(p * q, 0)} for p, q in asks],
            "bid_usd": round(bid_usd, 0), "ask_usd": round(ask_usd, 0),
            "imbalance": round(100 * (bid_usd - ask_usd) / max(bid_usd + ask_usd, 1), 2),
            "spread": round(asks[0][0] - bids[0][0], 4) if bids and asks else 0.0,
        }

    def scan(self) -> list[dict]:
        out = []
        for sym in UNIVERSE.symbols:
            candles = list(self.c5[sym])
            ctx = self._ctx_for(sym)
            if self.brain.modelled(sym):
                ctx["lstm_fc"] = self.brain.lstm_forecast(list(self.c1[sym]), sym)
                ctx["intent_prob"] = self.brain.intent_prob(list(self.c1[sym]))
            row = self.brain.scan_symbol(sym, candles, ctx)
            if row:
                out.append(row)
        out.sort(key=lambda z: -z["score_raw"])
        self._scan = out
        return out

    # ========================================================== autotrade
    async def _autotrade_loop(self):
        while True:
            try:
                if not self.ml_ready:
                    await asyncio.sleep(3)
                    continue
                res = self.analyse_focus()
                if RISK.auto_trade and res["shields"]["score"] >= self.params.score_threshold \
                        and not res["shields"]["blocked"] and res["ticket"]:
                    self._try_open(UNIVERSE.focus, res["divine"]["direction"],
                                   res["smc"], res["divine"], res["ctx"])
                # scanner-driven entries on the rest of the universe
                if RISK.auto_trade:
                    for row in self.scan():
                        if row["symbol"] == UNIVERSE.focus:
                            continue
                        if row["score"] >= self.params.score_threshold and not row["blocks"]:
                            candles = list(self.c5[row["symbol"]])
                            smc = ict_engine.detect(candles, self.params)
                            if smc.get("ok"):
                                divine = {"score": row["score_raw"],
                                          "direction": row["direction"],
                                          "parts": {"ict": row["ict"],
                                                    "lstm": 0, "intent": 0},
                                          "sides": []}
                                self._try_open(row["symbol"], row["direction"], smc,
                                               divine, self._ctx_for(row["symbol"]))
                elif res["shields"]["score"] < self.params.score_threshold:
                    pass
                self._log_veto(res)
            except Exception:
                traceback.print_exc()
            await asyncio.sleep(5)

    def _log_veto(self, res: dict):
        sh = res["shields"]
        if not sh["blocked"]:
            return
        now = time.time()
        if now - self._last_veto_note < 90:
            return
        self._last_veto_note = now
        why = " + ".join(b["text"] for b in sh["blocks"])
        db.journal_add("VETO", f"Entry vetoed: {why} "
                       f"(raw score {sh['raw_score']:.1f} forced to {sh['score']:.1f}).",
                       metrics={"blocks": sh["blocks"]})

    def _try_open(self, symbol: str, direction: str, smc: dict, divine: dict,
                  ctx: dict, reason: str | None = None) -> dict | None:
        if self.broker.has_position(symbol):
            return None
        if time.time() - self._cooldown.get(symbol, 0) < 900:
            return None
        prop = prop_check(RISK.capital, RISK.capital + db.trade_stats()["net_pnl"])
        if not prop["allowed"]:
            return None
        ticket = self.brain.build_ticket(symbol, direction, smc, divine,
                                         list(self.c1[symbol]), ctx)
        if not ticket["sizing"].get("ok"):
            return None
        tp = ticket["tp"]
        features = {
            "atr": smc["atr"], "zone": smc["structure"]["zone"],
            "bias": smc["structure"]["bias"],
            "oi_chg": ctx.get("oi_chg_15m_pct", 0.0),
            "funding": ctx.get("funding", 0.0),
            "cvd_div": ctx.get("cvd_div", "NONE"),
            "sweep": bool(smc.get("sweeps")),
            "in_value_area": None,
            "vol_regime": self.brain.vol.get("regime"),
            "intent_prob": divine.get("parts", {}).get("intent_prob"),
            "lstm_dir": (divine.get("lstm") or {}).get("direction"),
        }
        if self._cache_maps.get("profile"):
            features["in_value_area"] = self._cache_maps["profile"].get("in_value_area")
        tid = self.broker.open(symbol, ticket["direction"], ticket["entry"],
                               ticket["sl"], tp, ticket["sizing"]["qty"],
                               ticket["sizing"]["risk_usd"], ticket["score"],
                               divine.get("parts", {}), features,
                               reason=reason or (
                                   f"Auto. Vol {self.brain.vol['regime']}, "
                                   f"killzone {killzone()['zone']}."))
        self._cooldown[symbol] = time.time()
        return {"tid": tid}

    # =========================================================== snapshot
    def snapshot(self, analysis: dict | None = None, heavy: bool = False) -> dict:
        if time.time() - getattr(self, "_scan_ts", 0) > 3.0:
            self.scan()
            self._scan_ts = time.time()
        a = analysis or self.analyse_focus()
        sym = UNIVERSE.focus
        candles = list(self.c5[sym])
        stats = db.trade_stats()
        prop = prop_check(RISK.capital, RISK.capital + stats["net_pnl"])
        kz = a["shields"]["killzone"]
        price = a["price"]
        base = UNIVERSE.base(sym)

        cb_prem = None
        if self.coinbase.price and price:
            cb_prem = round(100 * (self.coinbase.price - price) / price, 4)

        funding_rows = []
        for s in UNIVERSE.symbols:
            f = self.funding.get(s, {})
            funding_rows.append({
                "base": UNIVERSE.base(s),
                "rate": round(f.get("rate", 0.0) * 100, 5),
                "next": f.get("next", 0),
                "oi_usd": round(self.oi.get(s, 0.0), 0),
            })
        funding_rows.sort(key=lambda z: -abs(z["rate"]))

        return {
            "type": "snapshot",
            "ts": int(time.time() * 1000),
            "cycle": self.cycle,
            "focus": {"symbol": sym, "base": base, "price": price,
                      "bar": candles[-1] if candles else None,
                      "candles_len": len(candles),
                      "session_open": candles[-288]["c"] if len(candles) > 288 else None},
            "tick": {
                "divine": a["divine"], "shields": a["shields"],
                "ticket": a["ticket"], "ctx": a["ctx"],
                "killzone": kz,
                "vol": self.brain.vol,
                "latency_ms": self._metrics,
            },
            "maps": a["maps"] if heavy else None,
            "scanner": self._scan,
            "positions": self.broker.summary(),
            "stats": stats,
            "recent_trades": db.recent_trades(60),
            "journal": db.journal_recent(40),
            "prop": prop,
            "risk": {"capital": RISK.capital, "risk_pct": RISK.risk_pct,
                     "mode": RISK.mode, "auto_trade": RISK.auto_trade,
                     "max_lev": RISK.max_leverage, "fee_bps": RISK.fee_bps},
            "params": self.params.as_dict(),
            "evolution": self.brain.evolution,
            "bandit": self.brain.bandit.snapshot(),
            "ml": {**self.brain.ml_info, "ready": self.ml_ready,
                   "training": self.training, "progress": self.train_progress,
                   "symbols_modelled": sorted(self.brain.lstms.keys()),
                   "n_models": len(self.brain.lstms),
                   "base_rate": self.brain.base_rate()},
            "macro": {"quotes": self.ctx.macro.quotes,
                      "correlation": self._correlation(),
                      "errors": self.ctx.macro.errors},
            "sentiment": {
                "fear_greed": self.ctx.sent.fear_greed,
                "stablecoin": self.ctx.sent.stablecoin,
                "dominance": self.ctx.sent.dominance,
                "dominance_chg_1h": self.ctx.dominance_change_1h(),
                "coinbase_premium": cb_prem,
                "coinbase_price": self.coinbase.price,
                "coinbase_connected": self.coinbase.connected,
            },
            "news": {"rss": self.ctx.sent.news[:12],
                     "calendar": self.ctx.sent.calendar[:10],
                     "blackout": self.ctx.news_blackout()},
            "whales": {"notional": self.whales.notional(60),
                       "recent": self.whales.recent(120)[:12],
                       "threshold": self.whales.threshold},
            "funding_rows": funding_rows,
            "venues": VENUES,
            "feeds": {
                "wire": {"connected": bool(self.stream and self.stream.connected),
                         "msgs": self.stream.msgs if self.stream else 0,
                         "trades": self.stream.trades if self.stream else 0,
                         "reconnects": self.stream.reconnects if self.stream else 0,
                         "insts": len(UNIVERSE.symbols)},
                "coinbase": self.coinbase.connected,
                "liqs_tracked": len(self.liqs),
                "errors": {**self.errors, **self.ctx.macro.errors,
                           **self.ctx.sent.errors},
                "history": self.history_note,
            },
            "server": {"uptime_s": round((time.time() * 1000 - self.started) / 1000),
                       "py": "3.13", "cycle": self.cycle},
        }

    def _correlation(self) -> list[dict]:
        sym = UNIVERSE.focus
        cs = list(self.c5[sym])
        if len(cs) < 40:
            return []
        btc = [c["c"] for c in cs][-120:]
        by_ts = {int(c["t"]) * 1000: c["c"] for c in cs[-400:]}
        rows = self.ctx.correlation_rows(btc, cs, by_ts)
        # crypto internal: BTC vs ETH/SOL from the same 5m grid
        for other in ("ETH-USDT-SWAP", "SOL-USDT-SWAP"):
            oc = list(self.c5[other])[-120:]
            if len(oc) >= 20:
                n = min(len(oc), len(btc))
                rows.append({"pair": f"{UNIVERSE.base(sym)}/{UNIVERSE.base(other)}",
                             "corr": round(rolling_corr(btc, [x["c"] for x in oc], n), 3),
                             "expected": 0.85,
                             "price": self.last.get(other),
                             "chg_pct": round(100 * pct(oc[-1]["c"], oc[0]["c"]), 2)})
        return rows

    # ============================================================== misc
    async def run_backtest(self, threshold: float | None = None,
                           decisions: int = 160) -> dict:
        sym = UNIVERSE.focus
        candles = list(self.c5[sym])
        p = EngineParams(**self.params.as_dict())
        thr = threshold if threshold is not None else BACKTEST_THRESHOLD
        from .engine.brain import backtest as _bt
        res = await asyncio.to_thread(_bt, candles, p, decisions, 2, 170, thr, 60)
        # comparative baseline: classic RSI trend-follow on the same bars
        base = await asyncio.to_thread(_baseline_backtest, candles)
        return {"engine": res, "baseline": base, "symbol": sym,
                "threshold": thr, "bars": len(candles),
                "generated_at": int(time.time() * 1000)}


def _baseline_backtest(candles: list[dict]) -> dict:
    """Commercial-indicator reference: RSI(14) cross + 1.5xATR stop, 2R target."""
    from .util import rsi as _rsi
    n = len(candles)
    if n < 120:
        return {"ok": False}
    h = np.array([c["h"] for c in candles], float)
    l = np.array([c["l"] for c in candles], float)
    c_ = np.array([c["c"] for c in candles], float)
    a = atr(h, l, c_, 14)
    r = _rsi(c_, 14)
    rs = []
    i = 60
    while i < n - 40:
        side = None
        if r[i] > 55 and r[i - 1] <= 55:
            side = 1
        elif r[i] < 45 and r[i - 1] >= 45:
            side = -1
        if side:
            entry = c_[i]; dist = 1.5 * a[i]
            sl = entry - side * dist; tp = entry + side * 2 * dist
            out = 0.0
            for j in range(i + 1, min(i + 60, n)):
                if side == 1:
                    if l[j] <= sl:
                        out = -1.0; break
                    if h[j] >= tp:
                        out = 2.0; break
                else:
                    if h[j] >= sl:
                        out = -1.0; break
                    if l[j] <= tp:
                        out = 2.0; break
            rs.append(out)
            i += 6
        else:
            i += 1
    if not rs:
        return {"ok": False}
    gw = sum(x for x in rs if x > 0); gl = abs(sum(x for x in rs if x <= 0))
    return {"ok": True, "trades": len(rs),
            "pf": round(gw / gl, 3) if gl > 1e-9 else 99.0,
            "winrate": round(100 * sum(1 for x in rs if x > 0) / len(rs), 1),
            "net_r": round(sum(rs), 2)}
