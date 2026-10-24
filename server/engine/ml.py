"""PHASE 3 - ML CORE.

Two genuinely trained models, no static thresholds pretending to be AI:

  * DivineLSTM      - numpy LSTM, hand-written forward + BPTT with Adam.
                      Trained on 1m bars, predicts the next 15 closes as
                      normalised log-returns.  Weights are persisted to .npz.
  * IntentClassifier- gradient boosted trees (xgboost if importable,
                      HistGradientBoostingClassifier otherwise) classifying
                      the next move as REAL MOVE vs LIQUIDITY GRAB.

Feature construction is shared between training and inference so there is no
train/serve skew.
"""
from __future__ import annotations

import math
import os
import pickle
import time
from dataclasses import dataclass

import numpy as np

from ..util import atr, ema, rsi
from ..config import DB_PATH

MODEL_DIR = os.path.join(os.path.dirname(DB_PATH), "models")
os.makedirs(MODEL_DIR, exist_ok=True)

HORIZON = 15          # bars ahead
SEQ = 32              # LSTM lookback window
FEATS = 8


# ============================== features =================================
def build_features(candles: list[dict]) -> np.ndarray:
    """(n, FEATS) float32 matrix, row i uses data up to and including bar i."""
    n = len(candles)
    o = np.array([c["o"] for c in candles], float)
    h = np.array([c["h"] for c in candles], float)
    l = np.array([c["l"] for c in candles], float)
    c = np.array([c["c"] for c in candles], float)
    v = np.array([c["v"] for c in candles], float)

    lr = np.diff(np.log(np.maximum(c, 1e-12)), prepend=np.log(max(c[0], 1e-12)))
    vol_ma = ema(v, 30)
    volz = np.where(vol_ma > 0, (v - vol_ma) / np.maximum(vol_ma, 1e-12), 0.0)
    a = atr(h, l, c, 14)
    atr_n = a / np.maximum(c, 1e-12)
    rng = np.where((h - l) > 0, (c - l) / np.maximum(h - l, 1e-12) - 0.5, 0.0)
    e21 = ema(c, 21); e55 = ema(c, 55)
    ema_d = (e21 - e55) / np.maximum(c, 1e-12)
    r = (rsi(c, 14) - 50) / 50.0
    # tick-rule proxy for delta pressure from OHLC alone (training-time CVD proxy)
    clv = np.where((h - l) > 0, (2 * c - h - l) / np.maximum(h - l, 1e-12), 0.0)
    delta_proxy = clv * v
    dp = delta_proxy / np.maximum(ema(np.abs(delta_proxy), 40), 1e-12)

    X = np.column_stack([
        np.clip(lr / 0.005, -3, 3),
        np.clip(volz, -4, 4),
        np.clip(atr_n / 0.004, 0, 4),
        np.clip(rng * 2, -1, 1),
        np.clip(ema_d / 0.004, -3, 3),
        np.clip(r, -1, 1),
        np.clip(dp, -4, 4),
        np.clip((c - np.roll(c, 5)) / np.maximum(c, 1e-12) / 0.006, -3, 3),
    ]).astype(np.float32)
    X[~np.isfinite(X)] = 0.0
    return X


def build_targets(candles: list[dict]) -> np.ndarray:
    c = np.array([x["c"] for x in candles], float)
    n = len(c)
    Y = np.zeros((n, HORIZON), np.float32)
    for i in range(n - 1):
        for k in range(HORIZON):
            j = min(n - 1, i + 1 + k)
            Y[i, k] = math.log(max(c[j], 1e-12) / max(c[i], 1e-12)) / 0.004
    return np.clip(Y, -6, 6)


def intent_labels(candles: list[dict]) -> np.ndarray:
    """1 = REAL MOVE (displacement that holds), 0 = LIQUIDITY GRAB (spike and
    reclaim).  Derived purely from forward price behaviour, so the classifier
    learns what a real move looks like from history."""
    n = len(candles)
    h = np.array([x["h"] for x in candles], float)
    l = np.array([x["l"] for x in candles], float)
    c = np.array([x["c"] for x in candles], float)
    a = atr(h, l, c, 14)
    y = np.zeros(n, np.int8)
    lb = 24
    for i in range(lb, n - HORIZON - 1):
        A = max(a[i], 1e-9)
        up_break = h[i + 1:i + HORIZON + 1].max() > h[i - lb:i].max() + 0.25 * A
        dn_break = l[i + 1:i + HORIZON + 1].min() < l[i - lb:i].min() - 0.25 * A
        end = c[i + HORIZON]
        if up_break and not dn_break:
            y[i] = 1 if end > h[i - lb:i].max() else 0
        elif dn_break and not up_break:
            y[i] = 1 if end < l[i - lb:i].min() else 0
        elif up_break and dn_break:
            y[i] = 0                     # both sides taken = liquidity raid
        else:
            y[i] = 0
    return y


# ================================ LSTM ===================================
class DivineLSTM:
    """Single-layer LSTM encoder + linear head over 15 outputs.

    Layout: x(b,f) @ Wx(f,4h) + h(b,h) @ Wh(h,4h) + b(4h)
    gate order: input | forget | candidate | output
    """

    def __init__(self, feats: int = FEATS, hidden: int = 40, out: int = HORIZON):
        self.f, self.h, self.o = feats, hidden, out
        rng = np.random.default_rng(7)
        k = 1.0 / math.sqrt(hidden)
        self.Wx = rng.normal(0, 0.6 / math.sqrt(feats), (feats, 4 * hidden))
        self.Wh = rng.normal(0, k, (hidden, 4 * hidden))
        self.b = np.zeros(4 * hidden)
        self.b[hidden:2 * hidden] = 1.0        # forget-gate bias init
        self.Wo = rng.normal(0, k, (hidden, out))
        self.bo = np.zeros(out)
        self._m: dict[str, np.ndarray] = {}
        self._v: dict[str, np.ndarray] = {}
        self.step = 0
        self.trained = False
        self.train_loss = None

    # ---------------------------------------------------------------- fwd
    @staticmethod
    def _sig(x):
        return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))

    def forward(self, X, cache=False):
        B, T, _ = X.shape
        H = self.h
        h = np.zeros((B, H)); c = np.zeros((B, H))
        zs = np.empty((T, B, 4 * H)); hs = np.empty((T, B, H))
        cs = np.empty((T, B, H)); i_s = np.empty((T, B, H))
        f_s = np.empty((T, B, H)); g_s = np.empty((T, B, H))
        o_s = np.empty((T, B, H)); tc = np.empty((T, B, H))
        for t in range(T):
            z = X[:, t, :] @ self.Wx + h @ self.Wh + self.b
            i = self._sig(z[:, :H])
            f = self._sig(z[:, H:2 * H])
            g = np.tanh(z[:, 2 * H:3 * H])
            o = self._sig(z[:, 3 * H:])
            tc_ = np.tanh(c)
            c = f * c + i * g
            h = o * np.tanh(c)
            zs[t] = z; hs[t] = h; cs[t] = c; i_s[t] = i
            f_s[t] = f; g_s[t] = g; o_s[t] = o; tc[t] = tc_
        out = hs[-1] @ self.Wo + self.bo
        if not cache:
            return out
        return out, (X, zs, hs, cs, i_s, f_s, g_s, o_s, tc)

    # ---------------------------------------------------------------- bwd
    def backward(self, cache, dout):
        X, zs, hs, cs, i_s, f_s, g_s, o_s, tc = cache
        B, T, _ = X.shape
        H = self.h
        gWo = hs[-1].T @ dout
        gbo = dout.sum(0)
        dh = dout @ self.Wo.T
        gWx = np.zeros_like(self.Wx); gWh = np.zeros_like(self.Wh)
        gb = np.zeros_like(self.b)
        dc = np.zeros((B, H))
        for t in range(T - 1, -1, -1):
            h_prev = hs[t - 1] if t > 0 else np.zeros((B, H))
            do = dh * tc[t]
            dct = dc + dh * o_s[t] * (1 - tc[t] ** 2)
            di = dct * g_s[t]
            dg = dct * i_s[t]
            df = dct * cs[t]
            dc_prev = dct * f_s[t]
            dz = np.concatenate([
                di * i_s[t] * (1 - i_s[t]),
                df * f_s[t] * (1 - f_s[t]),
                dg * (1 - g_s[t] ** 2),
                do * o_s[t] * (1 - o_s[t]),
            ], axis=1)
            gWx += X[:, t, :].T @ dz
            gWh += h_prev.T @ dz
            gb += dz.sum(0)
            dh = dz @ self.Wh.T
            dc = dc_prev
        return {"Wx": gWx, "Wh": gWh, "b": gb, "Wo": gWo, "bo": gbo}

    # ---------------------------------------------------------------- fit
    def fit(self, X, Y, epochs=14, lr=4e-3, batch=96, clip=1.0, log=None):
        """X:(N,T,f) Y:(N,out)"""
        N = X.shape[0]
        idx = np.arange(N)
        rng = np.random.default_rng(11)
        for p in ("Wx", "Wh", "b", "Wo", "bo"):
            self._m.setdefault(p, np.zeros_like(getattr(self, p)))
            self._v.setdefault(p, np.zeros_like(getattr(self, p)))
        losses = []
        for ep in range(epochs):
            rng.shuffle(idx)
            ep_loss = 0.0
            nb = 0
            for s in range(0, N - batch + 1, batch):
                b = idx[s:s + batch]
                xb, yb = X[b], Y[b]
                out, cache = self.forward(xb, cache=True)
                diff = out - yb
                ep_loss += float(np.mean(diff ** 2))
                nb += 1
                dout = (2.0 / diff.size) * diff
                grads = self.backward(cache, dout)
                self.step += 1
                bc1, bc2 = 1 - 0.9 ** self.step, 1 - 0.999 ** self.step
                for p, g in grads.items():
                    p_ = getattr(self, p)
                    if clip:
                        g = np.clip(g, -clip, clip)
                    self._m[p] = 0.9 * self._m[p] + 0.1 * g
                    self._v[p] = 0.999 * self._v[p] + 0.001 * g * g
                    p_ -= lr * (self._m[p] / bc1) / (np.sqrt(self._v[p] / bc2) + 1e-8)
            losses.append(ep_loss / max(nb, 1))
            if log:
                log(f"LSTM epoch {ep+1}/{epochs} mse={losses[-1]:.5f}")
        self.trained = True
        self.train_loss = losses[-1] if losses else None
        self.loss_curve = losses
        return self

    # ---------------------------------------------------------------- io
    def save(self, path: str):
        np.savez(path, Wx=self.Wx, Wh=self.Wh, b=self.b, Wo=self.Wo,
                 bo=self.bo, meta=np.array([self.f, self.h, self.o,
                                            float(self.train_loss or 0)]))

    @classmethod
    def load(cls, path: str):
        d = np.load(path)
        m = cls(int(d["meta"][0]), int(d["meta"][1]), int(d["meta"][2]))
        m.Wx, m.Wh, m.b, m.Wo, m.bo = d["Wx"], d["Wh"], d["b"], d["Wo"], d["bo"]
        m.train_loss = float(d["meta"][3])
        m.trained = True
        return m


# ============================ intent classifier ==========================
class IntentClassifier:
    def __init__(self):
        self.model = None
        self.kind = "none"
        self.trained = False
        self.metrics = {}

    def fit(self, X, y):
        mask = np.isfinite(X).all(axis=1)
        X, y = X[mask], y[mask]
        if len(np.unique(y)) < 2 or len(y) < 300:
            self.trained = False
            return self
        split = int(len(y) * 0.8)
        try:
            import xgboost as xgb  # type: ignore
            self.model = xgb.XGBClassifier(
                n_estimators=220, max_depth=4, learning_rate=0.06,
                subsample=0.85, colsample_bytree=0.85, eval_metric="logloss",
                n_jobs=2, verbosity=0)
            self.kind = "xgboost"
        except Exception:
            from sklearn.ensemble import HistGradientBoostingClassifier
            self.model = HistGradientBoostingClassifier(
                max_iter=180, learning_rate=0.07, max_leaf_nodes=15,
                min_samples_leaf=30)
            self.kind = "sklearn-hgb"
        self.model.fit(X[:split], y[:split])
        try:
            from sklearn.metrics import roc_auc_score
            proba = self.model.predict_proba(X[split:])[:, 1]
            auc = float(roc_auc_score(y[split:], proba))
        except Exception:
            auc = 0.0
        acc = float((self.model.predict(X[split:]) == y[split:]).mean())
        self.metrics = {"auc": round(auc, 4), "acc": round(acc, 4),
                        "n": int(len(y)), "family": self.kind,
                        "base_rate": round(float(y.mean()), 3)}
        self.trained = True
        return self

    def predict_proba(self, x: np.ndarray) -> float:
        if not self.trained or self.model is None:
            return 0.5
        x = np.nan_to_num(x.reshape(1, -1), nan=0.0, posinf=0.0, neginf=0.0)
        try:
            return float(self.model.predict_proba(x)[0][1])
        except Exception:
            return 0.5

    def save(self, path: str):
        with open(path, "wb") as f:
            pickle.dump({"model": self.model, "kind": self.kind,
                         "metrics": self.metrics, "trained": self.trained}, f)

    @classmethod
    def load(cls, path: str):
        m = cls()
        with open(path, "rb") as f:
            d = pickle.load(f)
        m.model, m.kind = d["model"], d["kind"]
        m.metrics, m.trained = d.get("metrics", {}), d.get("trained", True)
        return m


# ============================== trainer ==================================
def seq_windows(X: np.ndarray, Y: np.ndarray, seq: int = SEQ):
    N = len(X)
    if N <= seq:
        return np.empty((0, seq, X.shape[1])), np.empty((0, Y.shape[1]))
    xs = np.stack([X[i - seq:i] for i in range(seq, N)])
    ys = Y[seq:]
    return xs.astype(np.float32), ys.astype(np.float32)


def intent_matrix(candles: list[dict], X: np.ndarray) -> np.ndarray:
    """Classifier input: LSTM features + structural/liquidity context that the
    live engine also has, so train and inference share the same columns."""
    n = len(candles)
    h = np.array([c["h"] for c in candles], float)
    l = np.array([c["l"] for c in candles], float)
    c_ = np.array([c["c"] for c in candles], float)
    v = np.array([c["v"] for c in candles], float)
    a = atr(h, l, c_, 14)
    lb = 24
    rows = []
    for i in range(n):
        j0 = max(0, i - lb)
        prior_hi = h[j0:i].max() if i > j0 else h[i]
        prior_lo = l[j0:i].min() if i > j0 else l[i]
        A = max(a[i], 1e-9)
        sweep_up = (h[i] - prior_hi) / A if i > j0 else 0.0
        sweep_dn = (prior_lo - l[i]) / A if i > j0 else 0.0
        dist_hi = (prior_hi - c_[i]) / A if i > j0 else 0.0
        dist_lo = (c_[i] - prior_lo) / A if i > j0 else 0.0
        rows.append([
            *X[i].tolist(),
            float(np.clip(sweep_up, -3, 3)), float(np.clip(sweep_dn, -3, 3)),
            float(np.clip(dist_hi, -6, 6)), float(np.clip(dist_lo, -6, 6)),
            float(np.log(max(v[i], 1) / max(v[j0:i + 1].mean(), 1))),
        ])
    M = np.array(rows, np.float32)
    M[~np.isfinite(M)] = 0.0
    return M


def train_all(candles_1m: list[dict], log=print) -> tuple[DivineLSTM, IntentClassifier, dict]:
    t0 = time.time()
    X = build_features(candles_1m)
    Y = build_targets(candles_1m)
    xs, ys = seq_windows(X, Y)
    info = {"bars_1m": len(candles_1m), "windows": int(len(xs))}
    lstm = DivineLSTM()
    if len(xs) > 500:
        # keep the most recent 12k windows for training speed
        xs_t, ys_t = xs[-12000:], ys[-12000:]
        split = int(len(xs_t) * 0.85)
        lstm.fit(xs_t[:split], ys_t[:split], epochs=10, log=log)
        # holdout error, reported in the UI as model quality
        pred = lstm.forward(xs_t[split:])
        info["lstm_val_mse"] = round(float(np.mean((pred - ys_t[split:]) ** 2)), 4)
        # directional hit-rate on the 15-bar horizon
        dir_hit = float(np.mean(np.sign(pred[:, -1]) == np.sign(ys_t[split:, -1])))
        info["lstm_dir_acc"] = round(dir_hit, 4)
        lstm.save(os.path.join(MODEL_DIR, "divine_lstm.npz"))
    else:
        info["lstm_val_mse"] = None
        info["lstm_dir_acc"] = None

    M = intent_matrix(candles_1m, X)
    y = intent_labels(candles_1m)
    clf = IntentClassifier().fit(M, y)
    info["intent"] = clf.metrics
    if clf.trained:
        clf.save(os.path.join(MODEL_DIR, "intent.pkl"))
    info["train_seconds"] = round(time.time() - t0, 2)
    return lstm, clf, info
