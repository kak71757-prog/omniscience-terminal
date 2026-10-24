"""FastAPI application: REST control surface + WebSocket push + static UI.

Run:  uvicorn server.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import PARAMS, RISK, SHIELDS, STREAM_HZ, UNIVERSE, EngineParams
from .engine import ict as ict_engine
from .hub import Hub
from .store import db

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

hub: Hub | None = None
_clients: set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global hub
    hub = Hub()
    print("[hub] bootstrapping feeds ...", flush=True)
    await hub.start()
    print("[hub] live", flush=True)
    task = asyncio.create_task(broadcaster())
    yield
    task.cancel()
    await hub.stop()


app = FastAPI(title="OMNISCIENCE", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


# ------------------------------------------------------------------ push --
async def broadcaster():
    """Single source of truth pushed to every terminal.  Heavy map data is
    rebuilt at most every 2s; the light tick every cycle."""
    last_heavy = 0.0
    interval = 1.0 / max(STREAM_HZ, 0.2)
    while True:
        try:
            if not _clients:
                await asyncio.sleep(0.5)
                continue
            now = time.time()
            heavy = (now - last_heavy) > 2.0
            if heavy:
                last_heavy = now
                hub.cycle += 1
                hub.scan()
            analysis = await asyncio.to_thread(hub.analyse_focus)
            payload = hub.snapshot(analysis, heavy=heavy)
            dead = []
            for ws in list(_clients):
                try:
                    await ws.send_text(json.dumps(payload, default=float))
                except Exception:
                    dead.append(ws)
            for ws in dead:
                _clients.discard(ws)
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[broadcaster]", type(e).__name__, e, flush=True)
            await asyncio.sleep(1)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "hello", **bootstrap_payload()},
                                      default=float))
        while True:
            msg = await ws.receive_text()
            try:
                cmd = json.loads(msg)
            except Exception:
                continue
            await handle_command(cmd)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _clients.discard(ws)


# ------------------------------------------------------------- REST API ---
def bootstrap_payload() -> dict:
    candles = {s: list(hub.c5[s]) for s in UNIVERSE.symbols}
    return {
        "type": "bootstrap",
        "ts": int(time.time() * 1000),
        "config": {
            "focus": UNIVERSE.focus,
            "symbols": UNIVERSE.symbols,
            "killzones": [[n, a, b] for n, a, b in SHIELDS.killzones],
            "news_keywords": list(SHIELDS.news_high_impact),
        },
        "candles": candles,
        "params": PARAMS.as_dict(),
        "risk": {"capital": RISK.capital, "risk_pct": RISK.risk_pct,
                 "mode": RISK.mode, "auto_trade": RISK.auto_trade},
        "journal": db.journal_recent(60),
        "stats": db.trade_stats(),
        "positions": hub.broker.summary(),
        "trades": db.recent_trades(120),
        "params_history": db.params_history(20),
        "ml": hub.brain.ml_info,
        "snapshot": hub.snapshot(None, heavy=True),
    }


@app.get("/api/bootstrap")
async def api_bootstrap():
    return JSONResponse(bootstrap_payload(), media_type="application/json")


@app.get("/api/candles")
async def api_candles(symbol: str | None = None, tf: str = "5m",
                      limit: int = 400):
    sym = symbol or UNIVERSE.focus
    src = hub.c5.get(sym, [])
    return {"symbol": sym, "tf": tf, "candles": list(src)[-limit:]}


@app.get("/api/snapshot")
async def api_snapshot(heavy: bool = True):
    a = await asyncio.to_thread(hub.analyse_focus)
    return JSONResponse(hub.snapshot(a, heavy=heavy), media_type="application/json")


@app.get("/api/trades")
async def api_trades(limit: int = 200):
    return {"trades": db.recent_trades(limit), "stats": db.trade_stats(),
            "positions": hub.broker.summary()}


@app.get("/api/journal")
async def api_journal(limit: int = 100):
    return {"journal": db.journal_recent(limit)}


@app.get("/api/backtest")
async def api_backtest(threshold: float | None = None, decisions: int = 160):
    return await hub.run_backtest(threshold, decisions)


@app.get("/api/state")
async def api_state():
    return {
        "params": PARAMS.as_dict(),
        "risk": {"capital": RISK.capital, "risk_pct": RISK.risk_pct,
                 "mode": RISK.mode, "auto_trade": RISK.auto_trade,
                 "max_lev": RISK.max_leverage},
        "evolution": hub.brain.evolution,
        "bandit": hub.brain.bandit.snapshot(),
        "vol": hub.brain.vol,
        "ml": hub.brain.ml_info,
        "feeds": {"errors": {**hub.errors}, "history": hub.history_note},
        "params_history": db.params_history(30),
        "equity": db.equity_curve(300),
    }


class Control(BaseModel):
    capital: float | None = None
    risk_pct: float | None = None
    mode: str | None = None
    auto_trade: bool | None = None
    threshold: float | None = None
    max_lev: float | None = None


@app.post("/api/control")
async def api_control(c: Control):
    if c.capital is not None:
        RISK.capital = max(50.0, float(c.capital))
    if c.risk_pct is not None:
        RISK.risk_pct = min(0.05, max(0.001, float(c.risk_pct)))
    if c.mode in ("PAPER", "PROP"):
        RISK.mode = c.mode
    if c.auto_trade is not None:
        RISK.auto_trade = bool(c.auto_trade)
    if c.threshold is not None:
        PARAMS.score_threshold = min(99.0, max(50.0, float(c.threshold)))
    if c.max_lev is not None:
        RISK.max_leverage = min(50.0, max(1.0, float(c.max_lev)))
    db.journal_add("SYSTEM", f"Risk config changed: capital ${RISK.capital:.0f}, "
                   f"risk {RISK.risk_pct*100:.2f}%, mode {RISK.mode}, "
                   f"auto-trade {'ON' if RISK.auto_trade else 'OFF'}, "
                   f"threshold {PARAMS.score_threshold:.0f}.")
    return {"ok": True, "risk": {"capital": RISK.capital, "risk_pct": RISK.risk_pct,
                                 "mode": RISK.mode, "auto_trade": RISK.auto_trade},
            "threshold": PARAMS.score_threshold}


class FocusReq(BaseModel):
    symbol: str


@app.post("/api/focus")
async def api_focus(f: FocusReq):
    try:
        return await hub.set_focus(f.symbol)
    except ValueError as e:
        raise HTTPException(400, str(e))


class Order(BaseModel):
    symbol: str | None = None
    direction: str = "LONG"
    score: float | None = None
    override: bool = False


@app.post("/api/trade/open")
async def api_trade_open(o: Order):
    sym = o.symbol or UNIVERSE.focus
    direction = o.direction.upper()
    if direction not in ("LONG", "SHORT"):
        raise HTTPException(400, "direction must be LONG or SHORT")
    a = await asyncio.to_thread(hub.analyse_focus)
    if sym != UNIVERSE.focus:
        candles = list(hub.c5[sym])
        smc = ict_engine.detect(candles, PARAMS)
        divine = {"score": o.score or 0.0, "direction": direction,
                  "parts": {"ict": o.score or 0.0, "lstm": 0, "intent": 0},
                  "sides": []}
        ctx = hub._ctx_for(sym)
    else:
        smc, divine, ctx = a["smc"], a["divine"], a["ctx"]
    if not smc.get("ok"):
        raise HTTPException(400, "no structure available")
    # manual orders are subject to the shields.  The ONLY escape hatch is an
    # explicit operator override in PAPER mode, which is stamped on the trade
    # and written to the audit journal.  The autonomous brain can never use it.
    sh = hub.brain.shield_stack(ctx, direction, divine.get("score", 0))
    if sh["blocked"]:
        if not o.override or RISK.mode != "PAPER":
            raise HTTPException(409, {"blocked": True, "blocks": sh["blocks"],
                                      "overridable": RISK.mode == "PAPER"})
        db.journal_add(
            "OVERRIDE",
            "OPERATOR OVERRIDE: manual paper order placed with shields engaged ("
            + " + ".join(b["text"] for b in sh["blocks"])
            + "). Auto-trade logic remains blocked.",
            metrics={"blocks": sh["blocks"]})
    elif o.override:
        raise HTTPException(409, "shields are clear - override not required")
    why = ("Manual operator order — PAPER override, shields engaged."
           if (o.override and sh["blocked"]) else "Manual operator order.")
    res = hub._try_open(sym, direction, smc, divine, ctx, reason=why)
    if not res:
        raise HTTPException(409, "entry rejected: open position, cooldown, or prop limit")
    return {"ok": True, **res}


class CloseReq(BaseModel):
    trade_id: int | None = None
    symbol: str | None = None


@app.post("/api/trade/close")
async def api_trade_close(c: CloseReq):
    price_map = hub.last
    closed = []
    if c.trade_id:
        p = hub.broker.positions.get(c.trade_id)
        if p:
            closed.append(hub.broker.close_manual(c.trade_id, price_map.get(p["symbol"], p["entry"])))
    else:
        for tid, p in list(hub.broker.positions.items()):
            if c.symbol and p["symbol"] != c.symbol:
                continue
            closed.append(hub.broker.close_manual(tid, price_map.get(p["symbol"], p["entry"])))
    return {"ok": True, "closed": closed}


@app.post("/api/evolve")
async def api_evolve():
    candles = list(hub.c5[UNIVERSE.focus])
    ok = hub.brain.maybe_start_evolution(candles, force=True, reason="operator")
    return {"ok": ok, "running": hub.brain.evolution["running"]}


@app.get("/api/backtest-report")
async def api_backtest_report():
    return await hub.run_backtest(decisions=220)


async def handle_command(cmd: dict):
    """Commands arriving over the WebSocket (used by the terminal buttons)."""
    op = cmd.get("op")
    try:
        if op == "control":
            await api_control(Control(**{k: v for k, v in cmd.items() if k != "op"}))
        elif op == "open":
            await api_trade_open(Order(**{k: v for k, v in cmd.items() if k != "op"}))
        elif op == "close":
            await api_trade_close(CloseReq(**{k: v for k, v in cmd.items() if k != "op"}))
        elif op == "evolve":
            await api_evolve()
    except Exception as e:
        pass


@app.get("/api/health")
async def api_health():
    return {"ok": True, "uptime": int(time.time() * 1000) - (hub.started if hub else 0),
            "clients": len(_clients), "ml_ready": hub.ml_ready if hub else False,
            "wire": bool(hub.stream and hub.stream.connected) if hub else False}


# --------------------------------------------------------------- static ---
if os.path.isdir(WEB_DIR):
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(os.path.join(WEB_DIR, "index.html"))

    @app.get("/config.js")
    async def runtime_config():
        from fastapi.responses import Response
        body = ('/* engine endpoint for this deployment */\n'
                'window.OMNI_ENGINE_URL = "%s";\n'
                % os.getenv("OMNI_PUBLIC_ENGINE_URL", ""))
        return Response(body, media_type="application/javascript")

    @app.get("/vendor/{name}")
    async def vendor(name: str):
        p = os.path.join(WEB_DIR, "vendor", name)
        if os.path.exists(p):
            return FileResponse(p)
        raise HTTPException(404)
