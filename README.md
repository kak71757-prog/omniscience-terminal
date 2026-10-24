# PROJECT OMNISCIENCE

**A live liquidity-hunting trading terminal.** Three-column institutional layout,
seven map overlays, a three-model ensemble scoring engine, three veto shields, a
genetic self-evolution loop and a paper execution engine with a triple-TP ladder.

Everything you see is driven by live market data. There is no fixture file, no
random walk, and no placeholder panel. Where a data source is unavailable the
terminal says so instead of inventing a number.

```
┌───────────────────────────────────────────────────────────────────────────┐
│ OMNISCIENCE │ BTC PERP 80,809.6 │ DIVINE 67.7 │ KILLZONE │ WIRE ● ML ●    │
├───────────────┬───────────────────────────────────────────┬───────────────┤
│ LIVE SCANNER  │  MAP A liquidity heat + BSL/SSL rails     │ DIVINE ENGINE │
│ FUNDING HEAT  │  MAP C BOS/MSS/OB/FVG/pools/sweeps        │ THREE SHIELDS │
│ ON-CHAIN      │  MAP D volume profile POC/VAH/VAL/HVN     │ RISK ENGINE   │
│ NEWSWIRE      │  MAP F liquidation triggers 50/100/200M   │ EXECUTION     │
│ CALENDAR      │  MAP G LSTM 15-bar predicted path         │ POSITIONS     │
│               │  MAP B footprint + CVD (sub-charts)       │ PERFORMANCE   │
│               │  MAP E correlation matrix                 │ AI JOURNAL    │
│               │                                           │ EVOLUTION     │
└───────────────┴───────────────────────────────────────────┴───────────────┘
```

---

## Run it

```bash
cd omniscience
pip install -r requirements.txt
bash run.sh                 # http://localhost:8000
```

First boot takes ~30–45 s: it pulls 300 × 5m bars for 12 instruments, 30 days of
1m bars for the focus instrument, trains the LSTM and the intent classifier, and
starts every websocket. The terminal is interactive immediately — the ML panel
shows training progress and the score engine comes online when the models land.

Tests:

```bash
python3 -m pytest tests -q      # 35 tests: sizing, ladder, shields, SMC, ML, broker
```

Docker:

```bash
docker build -t omniscience .
docker run -p 8000:8000 -v omni_data:/app/data omniscience
```

**Railway** — engine and UI in one container:

```bash
railway login && railway init && railway up
railway volume add -m /app/data     # persist trades, journal, model weights
railway domain
```

Full instructions, env vars, sizing and troubleshooting: [`RAILWAY.md`](RAILWAY.md).
The container honours Railway's injected `$PORT` and starts with `bash run.sh`;
the Dockerfile targets Python 3.11 and exposes port 8000.

---

## What is actually live

| Feed | Source | Transport | Refresh |
|---|---|---|---|
| Candles (12 perps) | OKX SWAP | WebSocket `candle5m` + REST history | streaming |
| Aggressor trades (focus) | OKX | WebSocket `trades` (~150 msg/s) | streaming |
| Order book 20L | OKX | WebSocket `books5` + REST 50L snapshot | streaming |
| Liquidations (**real prints**) | OKX | REST `/public/liquidation-orders` | 20 s |
| Open interest | OKX | REST `/public/open-interest` | 15 s |
| Funding rate | OKX | REST `/public/funding-rate` | 60 s |
| Spot reference / Coinbase premium | Coinbase Exchange | WebSocket `ticker` | streaming |
| Macro DXY / SPX / NDX / US10Y / US2Y | CNBC quote + chart | HTTPS | 30 s + seeded intraday |
| Fear & Greed | alternative.me | HTTPS | 10 min |
| Stablecoin supply / net flows | DefiLlama | HTTPS | 20 min |
| USDT+USDC dominance | CoinGecko global | HTTPS | 60 s |
| High-impact calendar | ForexFactory JSON + Federal Reserve FOMC page | HTTPS | 5 min |
| Headlines | CoinTelegraph RSS | HTTPS | 5 min |

**Venue note.** Binance Futures and Bybit return HTTP 451/403 from this
deployment region. The venue layer is pluggable (`server/feeds/market.py:VENUES`)
and OKX is the live primary because it publishes every primitive the terminal
needs on public endpoints — including genuine per-order liquidation data, which
most venues hide. Deploy from a permitted region and set the adapter to Binance
to get `forceOrder` and 1000-level depth; the engine consumes a normalised
schema either way.

---

## The seven maps

| Map | Implementation |
|---|---|
| **A · Liquidity heatmap** | Liquidation notional modelled from *live* open interest with the standard leverage-tier model (10/25/50/100x, maintenance margin), anchored on the traded price distribution of the last 12 h and overlaid with real venue liquidation prints. BSL/SSL shelves are fractal swing clusters weighted by the notional stacked behind them. |
| **B · Order flow** | Footprint built from the live aggressor-tagged tape: each 1m bucket split into bid/ask rows per price, POC per bucket, cumulative delta, and a CVD-divergence classifier (price up / CVD down = distribution). |
| **C · Smart money** | Fractal swing detection → BOS/MSS with displacement filters, last-opposing-candle order blocks with retracement validity, ATR-filtered fair value gaps, equal-high/low clustering labelled `LIQUIDITY POOL`, sweep-and-reclaim flags, premium/discount shading around the dealing-range equilibrium. |
| **D · Volume profile** | Session profile with a triangular kernel peaked at each bar's close (not volume dumped on one tick), 70% value area expansion from POC, HVN/LVN classification. |
| **E · Correlation matrix** | Bar-to-bar **return** correlation on timestamps both series actually share (levels correlation across assets is spurious). Series with >50% zero returns — a closed market carrying its last close forward — report `--` rather than a fake zero. |
| **F · Liquidation map** | Where 50M / 100M / 200M of liquidation notional triggers above and below spot, plus the cumulative cascade within ±2%. |
| **G · AI prediction** | The trained LSTM's next-15-bar path with a √t confidence band, drawn forward of the live bar. |

---

## The ensemble

```
Divine Score = 0.40 × ICT rules + 0.30 × LSTM + 0.30 × Intent
              → trade only at ≥ 85 (configurable) AND all three shields open
```

* **Model 1 — ICT rule engine (40%).** Weighted conjunction: liquidity sweep 25,
  MSS 25 / BOS 18, order block 20, FVG 15, premium-discount location 15. Both
  directions are evaluated and the stronger side wins. This is what gets
  walk-forward validated, because it is the only component that can be replayed
  without lookahead.
* **Model 2 — LSTM (30%).** A numpy LSTM (40 cells, hand-written forward pass and
  BPTT with Adam, no framework) trained on 1m bars to predict the next 15 closes
  as normalised log-returns. **One model per instrument** (40 features → 15
  outputs), retrained hourly. Inference runs on the trailing 400 bars.
* **Model 3 — intent classifier (30%).** Gradient-boosted trees (xgboost if
  installed, otherwise sklearn HistGradientBoosting) predicting *real move* vs
  *liquidity grab*. Labels are derived from forward price behaviour: displacement
  that holds is a real move, a spike that reclaims is a grab. Output is
  calibrated against the model's own base rate, so 50 = average setup and 100 =
  never-seen conviction — without this the raw probability (base rate ≈ 0.38)
  would pin the composite below the entry threshold forever.

Model quality is reported in the terminal, honestly: LSTM validation MSE,
15-bar directional accuracy and intent AUC are all on a held-out split and
visible in the right-hand panel.

---

## The three shields (hard vetoes)

1. **Killzone** — London 08:00–11:00 UTC, New York 13:00–16:00 UTC, Silver
   Bullet 15:00–16:00 UTC. Outside them the Divine Score is forced to **0** and
   the banner reads `MARKET DEAD — GOD IS SLEEPING`.
2. **Anti-manipulation** — funding beyond ±0.08% on the side you want, price up
   with open interest down (`FAKE PUMP — REJECT`), or USDT+USDC dominance pumping
   >1% in an hour (`STABLECOIN FLIGHT — REJECT ALL LONGS`).
3. **News blackout** — ±15 min around high-impact events, read from the
   ForexFactory feed **and** the Federal Reserve's own FOMC calendar (so the
   shield keeps working when the JSON feed rate-limits, which it does).

The autonomous brain can never bypass a shield. A human operator can place a
**paper-only** order with shields engaged; it is stamped `OPERATOR OVERRIDE` in
the trade record and in the audit journal. That is a deliberate design choice:
silent bypass destroys the audit trail, and audit trails are the entire point of
an institutional terminal.

---

## Self-evolution

* **60 s volatility loop** — ATR percentile rank scales stop distance
  (compression ×0.82 → expansion ×1.45) and is surfaced as the vol regime.
* **Genetic algorithm** — when the trade count hits the next 100-trade mark (or
  early, if ≥12 trades show PF < 1.5), a population of 12 parameter sets is
  mutated across 8 genes (OB retracement window, FVG ATR threshold, EMA periods,
  sweep lookback, displacement multiplier, SL ATR multiple, equal-high tolerance),
  scored by **walk-forward backtest of the live engine** (no lookahead: each
  decision bar only sees data up to itself), and the incumbent is replaced only
  if the best mutant beats it. 3 generations per pass.
* **UCB1 policy bandit** — decides which gene group (`STRUCTURE`, `FLOW`, `RISK`,
  `VOLATILITY`) gets mutated hardest, with reward = realised profit-factor change
  after the previous generation went live.
* **Self-journal** — every entry, management action, close, veto and evolution is
  written to SQLite. Losing trades get an attribution pass over the feature
  snapshot stored at entry (`entered longs in PREMIUM`, `ignored bearish CVD
  divergence`, `no liquidity sweep preceded entry`, …) and a concrete corrective
  rule, which is exactly the mechanism the GA then acts on.

---

## Risk & execution

* `Position = (Capital × Risk%) / |Entry − SL|`, capped by max leverage, with
  taker fees and slippage charged on both sides so paper results are honest.
* Triple TP: 50% at 1R → stop to break-even; 30% at 2R → 1.8×ATR trail on the
  runner; 20% at 4R. Stops are checked before targets on the same tick.
* PROP mode enforces 5% daily loss, 10% total loss and 3 trades/day, with a live
  guard meter in the UI; breaches are named, not just blocked.
* Paper fills are driven by the live trade tape, so a position is marked and
  managed against the same prices the chart is drawing.

---

## Architecture

```
omniscience/
├── server/
│   ├── app.py              FastAPI: REST control surface, /ws push, static UI
│   ├── hub.py              runtime: feeds, analysis pipeline, broker, loops
│   ├── config.py           instruments, engine genes, risk, shields, endpoints
│   ├── util.py             EMA/ATR/RSI, fractals, correlation, formatting
│   ├── engine/
│   │   ├── ict.py          MAP C — BOS/MSS/OB/FVG/pools/sweeps + rule score
│   │   ├── liquidity.py    MAP A/F — liquidation model, BSL/SSL, triggers
│   │   ├── profile.py      MAP D — volume profile, POC, value area, HVN/LVN
│   │   ├── orderflow.py    MAP B — footprint, CVD, divergence, imbalance
│   │   ├── ml.py           LSTM (numpy BPTT) + intent classifier + training
│   │   ├── brain.py        ensemble, GA evolution, UCB1 bandit, tickets
│   │   ├── shields.py      the three vetoes
│   │   ├── risk.py         sizing, prop guard, TP ladder
│   │   └── broker.py       paper execution, ladder, trailing, journal attribution
│   ├── feeds/
│   │   ├── market.py       OKX REST/WS adapters, whale tape, coinbase premium
│   │   └── context.py      macro, fear&greed, stablecoins, dominance, news, FOMC
│   └── store/db.py         SQLite: trades, journal, evolved params, equity
├── web/                    zero-build terminal (no bundler, no CDN dependency)
│   ├── index.html
│   ├── css/terminal.css
│   ├── js/chart.js         lightweight-charts + canvas overlay for 7 maps
│   ├── js/panels.js        DOM bindings
│   ├── js/app.js           websocket + HTTP fallback, commands
│   └── vendor/lightweight-charts.js   (vendored v4.2.0, Apache-2.0)
├── tests/                  35 tests
├── Dockerfile  requirements.txt  .env.example  run.sh
```

**Transport.** The UI prefers the `/ws` push (single source of truth, ~1.5 Hz,
heavy map rebuild at 2 s). If the websocket cannot be established — some reverse
proxies strip upgrades — it automatically falls back to 1.6 s HTTP polling and
says so in the status bar. Both paths run the same server code.

**Why plain JS instead of Next.js.** The target runtime is one Python process
serving the terminal; adding a Node build step would mean two cold starts and a
second deploy target for zero functional gain, and the in-app preview can't load
external bundles anyway. The UI is dependency-free apart from a vendored
lightweight-charts, so it renders offline. `server/app.py` keeps the API surface
(`/api/bootstrap`, `/api/snapshot`, `/api/trades`, `/api/journal`,
`/api/control`, `/api/trade/*`, `/api/evolve`, `/api/backtest`, `/ws`) that a
Next.js shell would consume unchanged if you want to port the frontend.

---

## Keyboard

`L` long · `S` short · `C` close all · `E` evolve · `A B C D F G` toggle maps.

---

## Honest limits

* Walk-forward validation covers the **ICT rule engine only**. The LSTM and the
  intent classifier cannot be replayed without point-in-time retraining, so they
  are reported with their own holdout metrics and are excluded from the GA
  fitness function rather than quietly backtested with future data.
* Liquidation heat is a model over real open interest plus real prints, not a
  vendor's proprietary position map. The terminal tells you which is which.
* Fill quality in paper mode is tape-price with fees and slippage; it does not
  model queue position or partial fills.
* No private API keys are required and none are used. Live order routing is
  intentionally out of scope — this is the decision and risk layer.
