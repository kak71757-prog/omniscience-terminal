"""PROJECT OMNISCIENCE - central configuration.

Transport reality note (read this before changing venues):
Binance Futures (fapi) and Bybit return HTTP 451/403 from this deployment
region.  The venue layer is pluggable; OKX is the live primary because it
exposes *all* of the required primitives on public endpoints:
  - 5m/1m candles (WS + REST, paginated history)
  - full depth books (WS books5 + REST 50/400 level snapshots)
  - aggregated trades with aggressor side (WS)
  - REAL liquidation orders (REST /api/v5/public/liquidation-orders)
  - open interest (REST, per instrument)
  - funding rate (REST, per instrument)
Set OMNISCIENCE_VENUE=binance|bybit only from a region where they resolve.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict

def _f(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default

def _i(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Universe:
    """Scanner universe.  `focus` is the instrument that receives full depth,
    footprint and ML treatment."""
    focus: str = os.getenv("OMNI_FOCUS", "BTC-USDT-SWAP")
    symbols: list[str] = field(default_factory=lambda: [
        "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP",
        "DOGE-USDT-SWAP", "BNB-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP",
        "SUI-USDT-SWAP", "ADA-USDT-SWAP", "ARB-USDT-SWAP", "OP-USDT-SWAP",
    ])

    @property
    def scanner(self) -> list[str]:
        return self.symbols

    @staticmethod
    def base(inst: str) -> str:
        return inst.split("-")[0]


@dataclass
class EngineParams:
    """Live-evolvable parameters.  The genetic algorithm mutates these and the
    result is persisted to SQLite + surfaced in the UI.  Every consumer reads
    from this single object, so an evolution pass takes effect immediately."""
    ob_retrace_min: float = 0.50      # OB validity: 50% -> 79% retracement
    ob_retrace_max: float = 0.79
    fvg_min_atr: float = 0.15         # FVG must be >= 0.15 * ATR to count
    ema_fast: int = 21
    ema_slow: int = 55
    sweep_lookback: int = 24          # bars to look back for a liquidity sweep
    eqh_tol_atr: float = 0.08         # equal highs/lows tolerance
    disp_atr_mult: float = 1.10       # displacement required for BOS/MSS
    score_threshold: float = 85.0     # ONLY trade at or above this
    sl_atr_mult: float = 1.20
    tp1_r: float = 1.0
    tp2_r: float = 2.0
    tp3_r: float = 4.0
    atr_period: int = 14
    w_ict: float = 0.40
    w_lstm: float = 0.30
    w_intent: float = 0.30
    generation: int = 0
    fitness: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)

    def update(self, d: dict) -> None:
        for k, v in d.items():
            if hasattr(self, k) and isinstance(v, (int, float)):
                setattr(self, k, type(getattr(self, k))(v))


@dataclass
class RiskConfig:
    capital: float = _f("OMNI_CAPITAL", 1000.0)
    risk_pct: float = _f("OMNI_RISK_PCT", 0.01)
    mode: str = os.getenv("OMNI_MODE", "PAPER")     # PAPER | PROP
    auto_trade: bool = False
    prop_max_daily_loss: float = 0.05
    prop_max_total_loss: float = 0.10
    prop_max_trades_day: int = 3
    max_leverage: float = _f("OMNI_MAX_LEV", 10.0)
    fee_bps: float = 5.0            # taker, per side
    slippage_bps: float = 1.5


@dataclass
class Shields:
    killzones: list[tuple[str, int, int]] = field(default_factory=lambda: [
        ("LONDON OPEN", 8, 11),
        ("NEW YORK OPEN", 13, 16),
        ("SILVER BULLET", 15, 16),
    ])
    funding_extreme: float = 0.0008     # 0.08%
    usdt_dom_pump_1h: float = 0.01      # +1% dominance in 1h
    news_window_min: int = 15
    manual_blackout: bool = False
    news_high_impact: tuple[str, ...] = (
        "FOMC", "CPI", "NFP", "NON-FARM", "INTEREST RATE", "POWELL",
        "UNEMPLOYMENT", "GDP", "PCE", "RETAIL SALES", "PPI",
    )


UNIVERSE = Universe()
PARAMS = EngineParams()
RISK = RiskConfig()
SHIELDS = Shields()

# ---- transport endpoints -------------------------------------------------
OKX_REST = "https://www.okx.com"
OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
OKX_WS_BUSINESS = "wss://ws.okx.com:8443/ws/v5/business"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
CNBC_QUOTES = ("https://quote.cnbc.com/quote-html-webservice/quote.htm"
               "?symbols=.SPX%7C.NDX%7C.DXY%7CUS10Y%7CUS2Y&requestMethod=itv"
               "&noform=1&partnerId=2&fund=1&exthrs=1&output=json")
FOREXFACTORY = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
COINTELEGRAPH = "https://cointelegraph.com/rss"
FEAR_GREED = "https://api.alternative.me/fng/?limit=2"
DEFILLAMA_STABLE = "https://stablecoins.llama.fi/stablecoincharts/all"
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"

DB_PATH = os.getenv("OMNI_DB", os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "omniscience.db"))

# walk-forward validation runs the ICT rule engine standalone (the LSTM and
# intent models cannot be replayed without point-in-time retraining), so it is
# graded on the rule engine's own A-setup threshold rather than the 85 ensemble
# threshold used for live entries.
BACKTEST_THRESHOLD = _f("OMNI_BT_THRESHOLD", 65.0)

STREAM_HZ = _f("OMNI_STREAM_HZ", 1.5)          # UI push rate
BOOTSTRAP_5M = _i("OMNI_BOOT_5M", 300)         # bars of 5m history per symbol
BOOTSTRAP_1M_DAYS = _f("OMNI_BOOT_1M_DAYS", 5.0)  # 1m history for ML training
