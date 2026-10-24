"""Vector-light numerical helpers (numpy only)."""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np


def ema(values: Sequence[float], period: int) -> np.ndarray:
    v = np.asarray(values, dtype=float)
    if v.size == 0:
        return v
    a = 2.0 / (period + 1.0)
    out = np.empty_like(v)
    out[0] = v[0]
    for i in range(1, v.size):
        out[i] = a * v[i] + (1 - a) * out[i - 1]
    return out


def atr(high, low, close, period: int = 14) -> np.ndarray:
    h = np.asarray(high, float); l = np.asarray(low, float); c = np.asarray(close, float)
    if c.size < 2:
        return np.zeros_like(c)
    prev = np.concatenate(([c[0]], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    return ema(tr, period)


def rsi(close, period: int = 14) -> np.ndarray:
    c = np.asarray(close, float)
    if c.size < 3:
        return np.full_like(c, 50.0)
    d = np.diff(c, prepend=c[0])
    up = ema(np.maximum(d, 0), period)
    dn = ema(np.maximum(-d, 0), period)
    rs = up / np.where(dn == 0, 1e-12, dn)
    return 100 - 100 / (1 + rs)


def zscore(series: Sequence[float], window: int = 60) -> float:
    v = np.asarray(series, float)
    if v.size < 5:
        return 0.0
    w = v[-window:] if v.size > window else v
    sd = float(np.std(w))
    return 0.0 if sd < 1e-12 else float((v[-1] - np.mean(w)) / sd)


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, x)))


def pct(a: float, b: float) -> float:
    return 0.0 if b == 0 else (a - b) / b


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return default if b == 0 or not math.isfinite(b) else a / b


def ts_ms_to_sec(ts) -> int:
    return int(float(ts) / 1000.0)


def fmt_usd(x: float) -> str:
    ax = abs(x)
    if ax >= 1e9:
        return f"${x/1e9:.2f}B"
    if ax >= 1e6:
        return f"${x/1e6:.2f}M"
    if ax >= 1e3:
        return f"${x/1e3:.1f}K"
    return f"${x:.0f}"


def swing_points(high, low, left: int = 2, right: int = 2):
    """Fractal swing detection. Returns (swing_high_idx, swing_low_idx)."""
    h = np.asarray(high, float); l = np.asarray(low, float)
    n = h.size
    sh, sl = [], []
    for i in range(left, n - right):
        if h[i] == max(h[i - left:i + right + 1]) and h[i] > max(h[i - left:i]) :
            sh.append(i)
        if l[i] == min(l[i - left:i + right + 1]) and l[i] < min(l[i - left:i]):
            sl.append(i)
    return sh, sl


def linreg_slope(y: Sequence[float]) -> float:
    v = np.asarray(y, float)
    if v.size < 2:
        return 0.0
    x = np.arange(v.size, dtype=float)
    xm, ym = x.mean(), v.mean()
    den = float(((x - xm) ** 2).sum())
    return 0.0 if den == 0 else float(((x - xm) * (v - ym)).sum() / den)


def rolling_corr(a: Sequence[float], b: Sequence[float], n: int = 240) -> float:
    x = np.asarray(a, float)[-n:]
    y = np.asarray(b, float)[-n:]
    m = min(x.size, y.size)
    if m < 8:
        return 0.0
    x, y = x[-m:], y[-m:]
    if np.std(x) < 1e-12 or np.std(y) < 1e-12:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])
