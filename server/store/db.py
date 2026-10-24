"""SQLite persistence: trades, journal, evolved parameters, equity curve.

Prisma is not usable in this runtime (no Node build step for the API layer), so
this is the equivalent durable store with the same schema shape the Next.js
orchestrator would read if the web layer is ported.  WAL mode, single writer.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

from ..config import DB_PATH

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,           -- OPEN | CLOSED | CANCELLED
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp1 REAL, tp2 REAL, tp3 REAL,
    qty REAL NOT NULL,
    risk_usd REAL NOT NULL,
    score REAL,
    score_parts TEXT,               -- json of ensemble breakdown
    features TEXT,                  -- json of snapshot at entry
    opened_at INTEGER NOT NULL,
    closed_at INTEGER,
    exit_price REAL,
    realised_pnl REAL DEFAULT 0,
    r_multiple REAL DEFAULT 0,
    legs TEXT,                      -- json [{name, pct, price, hit_at}]
    sl_to_be INTEGER DEFAULT 0,
    reason TEXT,
    close_note TEXT
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    trade_id INTEGER,
    text TEXT NOT NULL,
    metrics TEXT
);
CREATE TABLE IF NOT EXISTS params (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    generation INTEGER,
    fitness REAL,
    payload TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    equity REAL NOT NULL,
    drawdown REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
"""


def conn() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            _CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.execute("PRAGMA synchronous=NORMAL")
            _CONN.executescript(SCHEMA)
            _CONN.commit()
        return _CONN


def q(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _LOCK:
        return list(conn().execute(sql, args).fetchall())


def x(sql: str, args: tuple = ()) -> int:
    with _LOCK:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return int(cur.lastrowid or cur.rowcount)


# ---------------------------------------------------------------- trades ---
def insert_trade(t: dict) -> int:
    return x(
        """INSERT INTO trades (symbol,side,mode,status,entry,sl,tp1,tp2,tp3,qty,
        risk_usd,score,score_parts,features,opened_at,legs,sl_to_be,reason)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (t["symbol"], t["side"], t["mode"], "OPEN", t["entry"], t["sl"],
         t.get("tp1"), t.get("tp2"), t.get("tp3"), t["qty"], t["risk_usd"],
         t.get("score"), json.dumps(t.get("score_parts", {})),
         json.dumps(t.get("features", {})), int(t.get("opened_at", time.time() * 1000)),
         json.dumps(t.get("legs", [])), 0, t.get("reason", "")),
    )


def update_trade(tid: int, **kw) -> None:
    if not kw:
        return
    cols = ", ".join(f"{k}=?" for k in kw)
    x(f"UPDATE trades SET {cols} WHERE id=?", tuple(kw.values()) + (tid,))


def open_trades() -> list[dict]:
    return [dict(r) for r in q("SELECT * FROM trades WHERE status='OPEN'")]


def recent_trades(limit: int = 200) -> list[dict]:
    return [dict(r) for r in q(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))]


def trade_stats() -> dict:
    rows = [dict(r) for r in q("SELECT * FROM trades WHERE status='CLOSED'")]
    wins = [r for r in rows if (r["realised_pnl"] or 0) > 0]
    losses = [r for r in rows if (r["realised_pnl"] or 0) <= 0]
    gross_win = sum(r["realised_pnl"] for r in wins)
    gross_loss = abs(sum(r["realised_pnl"] for r in losses))
    pf = gross_win / gross_loss if gross_loss > 1e-9 else (gross_win > 0) * 99.0
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    curve = []
    for r in reversed(rows):
        eq += r["realised_pnl"] or 0
        peak = max(peak, eq)
        dd = (eq - peak)
        max_dd = min(max_dd, dd)
        curve.append({"t": r["closed_at"], "eq": round(eq, 2)})
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(100 * len(wins) / len(rows), 2) if rows else 0.0,
        "profit_factor": round(float(pf), 3),
        "net_pnl": round(sum(r["realised_pnl"] or 0 for r in rows), 2),
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_r": round(sum(r["r_multiple"] or 0 for r in rows) / len(rows), 3) if rows else 0.0,
        "max_dd": round(max_dd, 2),
        "equity_curve": curve[-200:],
    }


def day_pnl_and_count(day_start_ms: int) -> tuple[float, int]:
    rows = q("SELECT realised_pnl, opened_at FROM trades WHERE status='CLOSED' AND closed_at>=?",
             (day_start_ms,))
    pnl = sum((r["realised_pnl"] or 0) for r in rows)
    cnt = q("SELECT COUNT(*) c FROM trades WHERE opened_at>=?", (day_start_ms,))[0]["c"]
    return float(pnl), int(cnt)


# --------------------------------------------------------------- journal ---
def journal_add(kind: str, text: str, trade_id: int | None = None,
                metrics: dict | None = None) -> int:
    return x("INSERT INTO journal (ts,kind,trade_id,text,metrics) VALUES (?,?,?,?,?)",
             (int(time.time() * 1000), kind, trade_id, text,
              json.dumps(metrics or {})))


def journal_recent(limit: int = 60) -> list[dict]:
    rows = [dict(r) for r in q("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,))]
    for r in rows:
        try:
            r["metrics"] = json.loads(r["metrics"] or "{}")
        except Exception:
            r["metrics"] = {}
    return rows


# ---------------------------------------------------------------- params ---
def params_save(payload: dict, note: str = "") -> None:
    x("INSERT INTO params (ts,generation,fitness,payload,note) VALUES (?,?,?,?,?)",
      (int(time.time() * 1000), int(payload.get("generation", 0)),
       float(payload.get("fitness", 0.0)), json.dumps(payload), note))


def params_latest() -> dict | None:
    rows = q("SELECT payload FROM params ORDER BY id DESC LIMIT 1")
    return json.loads(rows[0]["payload"]) if rows else None


def params_history(limit: int = 20) -> list[dict]:
    rows = q("SELECT ts,generation,fitness,note FROM params ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- equity ---
def equity_add(equity: float, drawdown: float) -> None:
    x("INSERT INTO equity (ts,equity,drawdown) VALUES (?,?,?)",
      (int(time.time() * 1000), float(equity), float(drawdown)))


def equity_curve(limit: int = 400) -> list[dict]:
    rows = q("SELECT ts,equity,drawdown FROM equity ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in reversed(rows)]
