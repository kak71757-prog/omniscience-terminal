"""PHASE 3 - THE SELF-EVOLVING QUANT BRAIN.

Ensemble = 0.40 * ICT rule engine  +  0.30 * LSTM  +  0.30 * intent classifier.
Trades only fire at Divine Score >= threshold (default 85) AND with all three
shields open.

Self-evolution has three loops:
  1. 60s volatility loop   - ATR regime rescales SL/TP distance.
  2. 100-trade evolution   - genetic algorithm re-tunes structural parameters
                             against a walk-forward backtest of the live engine.
  3. UCB1 policy bandit    - chooses which gene group to mutate hardest based
                             on the realised outcome of past evolutions.
Every loop writes a factual journal entry.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import random
import time
from collections import defaultdict, deque
from dataclasses import asdict, replace
from datetime import datetime, timezone

import numpy as np

from ..config import BACKTEST_THRESHOLD, PARAMS, RISK, UNIVERSE, EngineParams, SHIELDS
from ..store import db
from ..util import atr, clamp
from . import ict as ict_engine
from . import liquidity, profile
from .ml import (FEATS, SEQ, DivineLSTM, build_features, intent_matrix)
from .risk import ladder, prop_check, size_position
from .shields import anti_manipulation, evaluate, killzone, news

GENES = {
    "ob_retrace_min": (0.30, 0.72),
    "fvg_min_atr": (0.05, 0.45),
    "ema_fast": (13, 34),
    "ema_slow": (45, 120),
    "sweep_lookback": (8, 48),
    "disp_atr_mult": (0.7, 1.8),
    "sl_atr_mult": (0.8, 2.2),
    "eqh_tol_atr": (0.03, 0.18),
}
INT_GENES = {"ema_fast", "ema_slow", "sweep_lookback"}

POLICIES = ["STRUCTURE", "FLOW", "RISK", "VOLATILITY"]
POLICY_GENES = {
    "STRUCTURE": ["fvg_min_atr", "eqh_tol_atr", "disp_atr_mult"],
    "FLOW": ["sweep_lookback", "ema_fast", "ema_slow"],
    "RISK": ["sl_atr_mult", "ob_retrace_min"],
    "VOLATILITY": ["fvg_min_atr", "sl_atr_mult", "disp_atr_mult"],
}


# ============================== backtest =================================
def backtest(candles: list[dict], p: EngineParams, decisions: int = 60,
             step: int = 3, window: int = 170, threshold: float = 65.0,
             max_hold: int = 60) -> dict:
    """Walk-forward replay of the live ICT engine over historical bars.

    For each decision bar the engine is given *only* data up to that bar, so
    there is no lookahead.  SL/TP come from the same ATR ladder the broker
    uses, so backtest R-multiples are directly comparable to live ones.
    """
    n = len(candles)
    if n < window + 20:
        return {"ok": False, "reason": "insufficient bars"}
    h = np.array([c["h"] for c in candles], float)
    l = np.array([c["l"] for c in candles], float)
    c_ = np.array([c["c"] for c in candles], float)
    A_arr = atr(h, l, c_, p.atr_period)

    idxs = list(range(n - max_hold - 2, window, -step))[:decisions]
    idxs.reverse()
    trades = []
    for i in idxs:
        slice_ = candles[i - window + 1:i + 1]
        smc = ict_engine.detect(slice_, p)
        if not smc.get("ok"):
            continue
        A = smc["atr"]
        best = None
        for direction in ("LONG", "SHORT"):
            s, detail = ict_engine.ict_score(smc, p, direction)
            if best is None or s > best[0]:
                best = (s, direction, detail)
        score, direction, detail = best
        if score < threshold:
            continue
        entry = smc["price"]
        sl_dist = p.sl_atr_mult * A
        if direction == "LONG":
            sl = entry - sl_dist
            tp1, tp2, tp3 = (entry + sl_dist * p.tp1_r, entry + sl_dist * p.tp2_r,
                             entry + sl_dist * p.tp3_r)
        else:
            sl = entry + sl_dist
            tp1, tp2, tp3 = (entry - sl_dist * p.tp1_r, entry - sl_dist * p.tp2_r,
                             entry - sl_dist * p.tp3_r)
        legs_hit = 0
        remaining = 100.0
        pnl_r = 0.0
        outcome = "OPEN_EXPIRED"
        be = False
        for j in range(i + 1, min(i + 1 + max_hold, n)):
            hi, lo = h[j], l[j]
            if direction == "LONG":
                if lo <= sl:
                    pnl_r += -1.0 * (remaining / 100.0) if not be else 0.0
                    outcome = "SL" if not be else "BE"
                    break
                if hi >= tp1 and legs_hit < 1:
                    pnl_r += p.tp1_r * 0.5; remaining -= 50; legs_hit = 1
                    sl = entry; be = True
                if hi >= tp2 and legs_hit < 2:
                    pnl_r += p.tp2_r * 0.3; remaining -= 30; legs_hit = 2
                if hi >= tp3 and legs_hit < 3:
                    pnl_r += p.tp3_r * 0.2; remaining = 0; outcome = "TP3"; break
            else:
                if hi >= sl:
                    pnl_r += -1.0 * (remaining / 100.0) if not be else 0.0
                    outcome = "SL" if not be else "BE"
                    break
                if lo <= tp1 and legs_hit < 1:
                    pnl_r += p.tp1_r * 0.5; remaining -= 50; legs_hit = 1
                    sl = entry; be = True
                if lo <= tp2 and legs_hit < 2:
                    pnl_r += p.tp2_r * 0.3; remaining -= 30; legs_hit = 2
                if lo <= tp3 and legs_hit < 3:
                    pnl_r += p.tp3_r * 0.2; remaining = 0; outcome = "TP3"; break
        else:
            last = c_[min(i + max_hold, n - 1)]
            pnl_r += ((last - entry) if direction == "LONG" else (entry - last)) \
                / max(sl_dist, 1e-9) * (remaining / 100.0)
        trades.append({"i": i, "dir": direction, "score": round(score, 1),
                       "r": round(pnl_r, 3), "outcome": outcome})

    if not trades:
        return {"ok": True, "trades": 0, "pf": 0.0, "winrate": 0.0,
                "net_r": 0.0, "detail": [], "equity": []}
    rs = [t["r"] for t in trades]
    gw = sum(r for r in rs if r > 0)
    gl = abs(sum(r for r in rs if r <= 0))
    eq = np.cumsum(rs)
    peak = np.maximum.accumulate(eq)
    dd = float((eq - peak).min()) if len(eq) else 0.0
    return {
        "ok": True,
        "trades": len(rs),
        "pf": round(gw / gl, 3) if gl > 1e-9 else (99.0 if gw > 0 else 0.0),
        "winrate": round(100 * sum(1 for r in rs if r > 0) / len(rs), 1),
        "net_r": round(float(sum(rs)), 3),
        "max_dd_r": round(dd, 3),
        "avg_r": round(float(np.mean(rs)), 3),
        "equity": [round(float(x), 3) for x in eq],
        "detail": trades[-40:],
        "threshold": threshold,
    }


# ============================ genetic algorithm ==========================
class GeneticEvolver:
    def __init__(self, base: EngineParams, history_5m: list[dict],
                 pop: int = 12, gens: int = 3, seed: int | None = None):
        self.base = base
        self.history = history_5m
        self.pop = pop
        self.gens = gens
        self.rng = random.Random(seed)
        self.log: list[str] = []

    def rand_gene(self, g: str):
        lo, hi = GENES[g]
        v = self.rng.uniform(lo, hi)
        return int(round(v)) if g in INT_GENES else round(v, 4)

    def mutate(self, params: EngineParams, hard: list[str], rate: float = 0.45):
        child = replace(params)
        for g, (lo, hi) in GENES.items():
            r = rate * (2.2 if g in hard else 1.0)
            if self.rng.random() < r:
                cur = getattr(child, g)
                span = (hi - lo) * (0.06 if g in hard else 0.03)
                v = min(hi, max(lo, cur + self.rng.gauss(0, span)))
                setattr(child, g, int(round(v)) if g in INT_GENES else round(v, 4))
        if child.ema_fast >= child.ema_slow:
            child.ema_fast = max(9, int(child.ema_slow * 0.45))
        child.ob_retrace_min = min(child.ob_retrace_min, 0.72)
        return child

    def crossover(self, a: EngineParams, b: EngineParams):
        child = replace(a)
        for g in GENES:
            if self.rng.random() < 0.5:
                setattr(child, g, getattr(b, g))
        return child

    def fitness(self, params: EngineParams) -> tuple[float, dict]:
        bt = backtest(self.history, params, decisions=55, step=3, window=170,
                      threshold=BACKTEST_THRESHOLD)
        if not bt.get("trades"):
            return -1.0, bt
        # reward profit factor and sample size, punish ruin
        f = (min(bt["pf"], 4.0) * 0.55 + (bt["winrate"] / 100.0) * 0.30
             + min(bt["net_r"], 20) / 40.0 + min(bt["trades"], 25) / 250.0
             - abs(bt["max_dd_r"]) / 60.0)
        return round(float(f), 4), bt

    def run(self, hard: list[str]) -> tuple[EngineParams, dict]:
        population = [replace(self.base)]
        for _ in range(self.pop - 1):
            population.append(self.mutate(self.base, hard))
        best = (self.base, -9e9, {})
        for gen in range(self.gens):
            scored = []
            for cand in population:
                f, bt = self.fitness(cand)
                scored.append((f, cand, bt))
            scored.sort(key=lambda z: -z[0])
            if scored[0][0] > best[1]:
                best = (scored[0][1], scored[0][0], scored[0][2])
            self.log.append(
                f"gen {gen+1}: best fitness {scored[0][0]:.3f} "
                f"pf {scored[0][2].get('pf')} wr {scored[0][2].get('winrate')}")
            elite = [s[1] for s in scored[:max(2, self.pop // 3)]]
            population = elite[:]
            while len(population) < self.pop:
                a = self.rng.choice(elite); b = self.rng.choice(elite)
                child = self.crossover(a, b)
                population.append(self.mutate(child, hard, rate=0.30))
        return best[0], {"fitness": best[1], "backtest": best[2], "log": self.log}


class PolicyBandit:
    """UCB1 over mutation policies.  Reward = realised PF improvement after a
    parameter generation went live with that policy in effect."""

    def __init__(self):
        self.pulls = {k: 1 for k in POLICIES}
        self.reward = {k: 0.0 for k in POLICIES}
        self.last_policy: str | None = None
        self.last_pf_at_change: float | None = None

    def choose(self) -> str:
        n = sum(self.pulls.values())
        scores = {k: self.reward[k] / self.pulls[k]
                  + math.sqrt(2 * math.log(n) / self.pulls[k]) for k in POLICIES}
        pick = max(scores, key=scores.get)
        self.pulls[pick] += 1
        self.last_policy = pick
        return pick

    def reward_realised(self, pf_now: float) -> float | None:
        if self.last_pf_at_change is None or self.last_policy is None:
            self.last_pf_at_change = pf_now
            return None
        gain = max(-2.0, min(2.0, pf_now - self.last_pf_at_change))
        self.reward[self.last_policy] += gain
        self.last_pf_at_change = pf_now
        return gain

    def snapshot(self) -> list[dict]:
        return [{"policy": k, "pulls": self.pulls[k],
                 "avg_reward": round(self.reward[k] / self.pulls[k], 3)}
                for k in POLICIES]


# ================================= brain =================================
class Brain:
    def __init__(self):
        self.params = PARAMS
        self.lstms: dict[str, DivineLSTM] = {}
        self.clf = None
        self.ml_info: dict = {"status": "untrained"}
        self.symbol_ml: dict[str, dict] = {}
        self.bandit = PolicyBandit()
        self.evolution = {"generation": 0, "last_run": None, "running": False,
                          "fitness": 0.0, "log": [], "backtest": {},
                          "next_at_trades": 100}
        self.vol = {"atr_pct": 0.5, "sl_mult": 1.0, "regime": "NORMAL",
                    "note": "", "updated": 0}
        self.oos: dict = {}
        self.baseline = self._baseline()
        self._evo_task = None

    def _baseline(self) -> dict:
        """Default commercial-indicator comparison used by the validation panel."""
        return {"note": "RSI(14) > 55 long / < 45 short, 1R SL 1.5xATR, fixed 2R TP"}

    # ------------------------------------------------------------- state --
    def attach(self, symbol: str, lstm, clf, ml_info):
        if lstm is not None:
            self.lstms[symbol] = lstm
        if clf is not None:
            self.clf = clf
        self.symbol_ml[symbol] = ml_info
        if symbol == UNIVERSE.focus:
            self.ml_info = ml_info

    @property
    def lstm(self):
        return self.lstms.get(UNIVERSE.focus)

    def modelled(self, symbol: str) -> bool:
        return symbol in self.lstms

    def base_rate(self) -> float:
        return float((getattr(self.clf, "metrics", {}) or {}).get("base_rate", 0.378)) 

    def intent_score(self, p: float) -> float:
        """Calibrated against the classifier's own base rate, so 50 means
        'average setup' and 100 means 'never-seen conviction'.  Without this
        the raw probability (base rate ~0.38) would permanently drag the
        composite under the entry threshold."""
        base = max(min(self.base_rate(), 0.9), 0.1)
        if p <= 0:
            return 0.0
        lift = (p / (1 - p)) / (base / (1 - base))
        return clamp(50.0 + 50.0 * math.log2(max(lift, 1e-6)) / 1.5)

    def lstm_component(self, direction: str, fc: dict | None) -> float:
        if not fc:
            return 50.0
        want_up = direction == "LONG"
        mag = min(1.0, abs(fc["cum_return_pct"]) / 0.35)
        if (fc["direction"] == "UP") == want_up:
            return clamp(55.0 + 45.0 * mag * fc["confidence"])
        return clamp(45.0 * (1 - mag) * (1 - fc["confidence"]))

    # ============================================================ scoring =
    def lstm_forecast(self, candles_1m: list[dict], symbol: str | None = None) -> dict | None:
        model = self.lstms.get(symbol or UNIVERSE.focus)
        if model is None or len(candles_1m) < SEQ + 2:
            return None
        candles_1m = candles_1m[-400:]          # inference needs the tail only
        X = build_features(candles_1m)
        seq = X[-SEQ:][None, ...].astype(np.float32)
        out = model.forward(seq)[0]           # 15 normalised log-returns
        # denormalise: 0.004 log units per unit output
        path = np.exp(np.cumsum(out * 0.004))
        last = candles_1m[-1]["c"]
        prices = [round(float(last * p), 6) for p in path]
        cum = float(out.sum() * 0.004)
        agree = float(np.mean(np.sign(out) == np.sign(cum))) if cum != 0 else 0.5
        return {
            "prices": prices,
            "cum_return_pct": round(100 * (math.exp(cum) - 1), 4),
            "direction": "UP" if cum > 0 else "DOWN",
            "confidence": round(agree, 3),
            "raw": [round(float(x), 4) for x in out],
            "val_mse": self.symbol_ml.get(symbol or UNIVERSE.focus, {}).get("lstm_val_mse"),
            "dir_acc": self.symbol_ml.get(symbol or UNIVERSE.focus, {}).get("lstm_dir_acc"),
        }

    def intent_prob(self, candles_1m: list[dict], extra: dict | None = None) -> float:
        if self.clf is None or getattr(self.clf, "trained", False) is False:
            return 0.5
        if len(candles_1m) < 60:
            return 0.5
        candles_1m = candles_1m[-160:]          # inference needs the tail only
        X = build_features(candles_1m)
        M = intent_matrix(candles_1m, X)
        return self.clf.predict_proba(M[-1])

    def divine(self, smc: dict, candles_1m: list[dict], ctx: dict,
               symbol: str | None = None) -> dict:
        """Full ensemble.  `symbol` selects that instrument's LSTM."""
        symbol = symbol or UNIVERSE.focus
        fc = self.lstm_forecast(candles_1m, symbol)
        pr = self.intent_prob(candles_1m)

        sides = []
        for direction in ("LONG", "SHORT"):
            ict_s, detail = ict_engine.ict_score(smc, self.params, direction)
            lstm_s = self.lstm_component(direction, fc)
            intent_s = self.intent_score(pr)
            composite = (ict_s * self.params.w_ict + lstm_s * self.params.w_lstm
                         + intent_s * self.params.w_intent)
            sides.append({
                "direction": direction,
                "score": round(clamp(composite), 1),
                "parts": {"ict": round(ict_s, 1), "lstm": round(lstm_s, 1),
                          "intent": round(intent_s, 1), "intent_prob": round(pr, 4)},
                "ict_detail": detail,
            })
        best = max(sides, key=lambda z: z["score"])
        return {
            "score": best["score"],
            "direction": best["direction"],
            "parts": best["parts"],
            "sides": sides,
            "lstm": fc,
            "intent_prob": round(pr, 4),
            "intent_metrics": getattr(self.clf, "metrics", {}) if self.clf else {},
            "model_versions": {"ict": "rules v" + str(self.params.generation),
                               "lstm": f"lstm[{UNIVERSE.base(symbol)}]" if fc else "lstm[untrained]",
                               "intent": getattr(self.clf, "kind", "none")},
            "modelled": fc is not None,
        }

    # ============================================================== vetoes =
    def shield_stack(self, ctx: dict, direction: str, score: float) -> dict:
        kz = killzone()
        am = anti_manipulation(
            funding_rate=ctx.get("funding", 0.0),
            price_chg_pct_15m=ctx.get("chg_15m_pct", 0.0),
            oi_chg_pct_15m=ctx.get("oi_chg_15m_pct", 0.0),
            usdt_dom_chg_1h=ctx.get("usdt_dom_chg_1h", 0.0),
            side=direction,
        )
        nw = news(ctx.get("news", {}))
        res = evaluate(kz, am, nw, score)
        res["killzone"] = kz
        res["anti_manipulation"] = am
        res["news"] = nw
        return res

    # ========================================================== evolution =
    def maybe_start_evolution(self, history_5m: list[dict], force: bool = False,
                              reason: str = "scheduled") -> bool:
        if self.evolution["running"]:
            return False
        if not force and not self._evolution_due():
            return False
        self.evolution["running"] = True
        asyncio.get_event_loop().create_task(
            self._run_evolution(history_5m, reason))
        return True

    def _evolution_due(self) -> bool:
        stats = db.trade_stats()
        due = stats["trades"] >= self.evolution["next_at_trades"]
        # also run once we have real evidence but shy of 100 trades
        if not due and stats["trades"] >= 12 and stats["profit_factor"] < 1.5 \
                and self.evolution["generation"] == 0:
            return True
        return due

    async def _run_evolution(self, history_5m: list[dict], reason: str):
        t0 = time.time()
        try:
            policy = self.bandit.choose()
            hard = POLICY_GENES[policy]
            ev = GeneticEvolver(self.params, history_5m, pop=12, gens=3)
            best, info = await asyncio.to_thread(ev.run, hard)
            before = self.params.as_dict()
            stats = db.trade_stats()
            gain = self.bandit.reward_realised(stats["profit_factor"])

            improved = info["fitness"] > (self.evolution.get("fitness") or -9e9)
            adopted = False
            if improved and info["backtest"].get("trades", 0) >= 4:
                best.generation = self.params.generation + 1
                best.fitness = info["fitness"]
                best.w_ict, best.w_lstm, best.w_intent = (self.params.w_ict,
                                                          self.params.w_lstm,
                                                          self.params.w_intent)
                best.score_threshold = self.params.score_threshold
                best.tp1_r, best.tp2_r, best.tp3_r = (self.params.tp1_r,
                                                      self.params.tp2_r,
                                                      self.params.tp3_r)
                best.atr_period = self.params.atr_period
                for k, v in asdict(best).items():
                    setattr(self.params, k, v)
                adopted = True
                db.params_save(self.params.as_dict(),
                               f"gen {self.params.generation} via {policy} ({reason})")

            delta = {k: [before[k], getattr(self.params, k)]
                     for k in GENES if before[k] != getattr(self.params, k)}
            self.evolution.update({
                "generation": self.params.generation,
                "last_run": int(time.time() * 1000),
                "running": False,
                "fitness": round(info["fitness"], 4),
                "log": info["log"][-12:],
                "backtest": {k: info["backtest"].get(k) for k in
                             ("trades", "pf", "winrate", "net_r", "max_dd_r")},
                "policy": policy,
                "adopted": adopted,
                "deltas": delta,
                "seconds": round(time.time() - t0, 1),
                "next_at_trades": stats["trades"] + 100,
            })
            self.oos = info["backtest"]

            changed = ", ".join(f"{k} {v[0]}->{v[1]}" for k, v in delta.items()) \
                or "no gene exceeded its fitness delta"
            db.journal_add(
                "EVOLUTION",
                (f"Evolution gen {self.params.generation} ({policy} policy, {reason}). "
                 f"Live PF {stats['profit_factor']} / WR {stats['winrate']}%. "
                 f"GA best fitness {info['fitness']:.3f} with PF "
                 f"{info['backtest'].get('pf')} over {info['backtest'].get('trades')} "
                 f"walk-forward trades. "
                 + (f"Adopted mutations: {changed}." if adopted else
                    "Best mutant did not beat incumbent - parameters held. ")
                 + (f"UCB1 reward for {self.bandit.last_policy}: {gain:+.3f}." if gain else "")),
                metrics={"deltas": delta, "backtest": self.evolution["backtest"]})
        except Exception as e:
            self.evolution["running"] = False
            db.journal_add("EVOLUTION", f"Evolution aborted: {type(e).__name__}: {e}",
                           metrics={})
        finally:
            self.evolution["running"] = False

    def volatility_loop(self, candles_5m: list[dict]) -> dict:
        """Every 60s: rescale stops to the current ATR regime."""
        if len(candles_5m) < 60:
            return self.vol
        h = np.array([c["h"] for c in candles_5m], float)
        l = np.array([c["l"] for c in candles_5m], float)
        c_ = np.array([c["c"] for c in candles_5m], float)
        a = atr(h, l, c_, self.params.atr_period)
        pct = a / np.maximum(c_, 1e-9)
        window = pct[-200:]
        cur = float(pct[-1])
        rank = float((window <= cur).mean())
        mult = 1.0
        regime = "NORMAL"
        if rank > 0.82:
            mult, regime = 1.45, "EXPANSION"
        elif rank > 0.6:
            mult, regime = 1.15, "ELEVATED"
        elif rank < 0.18:
            mult, regime = 0.82, "COMPRESSION"
        self.vol = {
            "atr_pct": round(cur * 100, 4),
            "percentile": round(rank * 100, 1),
            "sl_mult": mult,
            "regime": regime,
            "note": {"EXPANSION": "stops widened - volatility god awake",
                     "ELEVATED": "stops slightly widened",
                     "NORMAL": "baseline stop distance",
                     "COMPRESSION": "stops tightened - breakout imminent"}[regime],
            "updated": int(time.time() * 1000),
        }
        return self.vol

    # ============================================================ scanner =
    def scan_symbol(self, symbol: str, candles_5m: list[dict],
                    ctx: dict) -> dict | None:
        if len(candles_5m) < 60:
            return None
        smc = ict_engine.detect(candles_5m, self.params)
        if not smc.get("ok"):
            return None
        sides = []
        for direction in ("LONG", "SHORT"):
            s, detail = ict_engine.ict_score(smc, self.params, direction)
            sides.append({"direction": direction, "ict": s, "detail": detail})
        best = max(sides, key=lambda z: z["ict"])
        intent = ctx.get("intent_prob", 0.5)
        fc = ctx.get("lstm_fc")
        lstm_s = self.lstm_component(best["direction"], fc)
        intent_s = self.intent_score(intent)
        score = (best["ict"] * self.params.w_ict + lstm_s * self.params.w_lstm
                 + intent_s * self.params.w_intent)
        shields = self.shield_stack(ctx, best["direction"], score)
        return {
            "symbol": symbol,
            "base": UNIVERSE.base(symbol),
            "price": smc["price"],
            "direction": best["direction"],
            "ict": round(best["ict"], 1),
            "score_raw": round(clamp(score), 1),
            "score": shields["score"],
            "bias": smc["structure"]["bias"],
            "zone": smc["structure"]["zone"],
            "sweep": bool(smc["sweeps"]),
            "blocks": shields["blocks"],
            "atr": smc["atr"],
            "chg_pct": ctx.get("chg_pct", 0.0),
            "lstm": round(lstm_s, 1), "intent": round(intent_s, 1),
            "modelled": self.modelled(symbol),
        }

    # ====================================================== order tickets =
    def build_ticket(self, symbol: str, direction: str, smc: dict, divine: dict,
                     candles_1m: list[dict], ctx: dict) -> dict:
        entry = smc["price"]
        A = smc["atr"]
        vol_mult = self.vol.get("sl_mult", 1.0)
        sl_dist = self.params.sl_atr_mult * A * vol_mult
        side = "LONG" if direction == "LONG" else "SHORT"
        sl = entry - sl_dist if side == "LONG" else entry + sl_dist
        tp = ladder(entry, sl, side, self.params.tp1_r, self.params.tp2_r,
                    self.params.tp3_r)
        sizing = size_position(entry, sl, RISK.capital, RISK.risk_pct,
                               RISK.max_leverage, RISK.fee_bps, RISK.slippage_bps)
        prop = prop_check(RISK.capital, RISK.capital + db.trade_stats()["net_pnl"])
        rr = abs(tp["tp3"] - entry) / max(abs(entry - sl), 1e-9)
        return {
            "symbol": symbol, "direction": side, "entry": entry, "sl": sl,
            "tp": tp, "sizing": sizing, "prop": prop, "atr": A,
            "vol_regime": self.vol, "rr": round(rr, 2),
            "score": divine["score"], "parts": divine["parts"],
            "liq_target_up": ctx.get("magnet_up"),
            "liq_target_down": ctx.get("magnet_down"),
        }
