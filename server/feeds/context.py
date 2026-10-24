"""PHASE 1 - MACRO / ON-CHAIN / SENTIMENT / NEWS ingestion.

Every source here is polled live over HTTPS and feeds the correlation matrix,
the anti-manipulation shield and the news blackout.  No fabricated values: if a
source is unreachable the field goes null and the UI renders it as `--`, because
a silently faked macro print is worse than a missing one.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import httpx

from ..config import (CNBC_QUOTES, COINGECKO_GLOBAL, COINTELEGRAPH, DB_PATH,
                      DEFILLAMA_STABLE, FEAR_GREED, FOREXFACTORY, SHIELDS)
from ..util import clamp, rolling_corr

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Omniscience/1.0)"}


@dataclass
class MacroState:
    quotes: dict = field(default_factory=dict)     # {"DXY": {...}}
    history: dict = field(default_factory=dict)    # {"DXY": [(ts, px), ...]}
    updated: int = 0
    errors: dict = field(default_factory=dict)


@dataclass
class SentimentState:
    fear_greed: dict = field(default_factory=dict)
    stablecoin: dict = field(default_factory=dict)
    dominance: dict = field(default_factory=dict)
    dominance_hist: list = field(default_factory=list)
    news: list = field(default_factory=list)
    calendar: list = field(default_factory=list)
    updated: int = 0
    errors: dict = field(default_factory=dict)


class ContextFeeds:
    def __init__(self):
        self.macro = MacroState()
        self.sent = SentimentState()
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0, headers=_HEADERS,
                                             follow_redirects=True)
        return self._client

    async def aclose(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ---------------------------------------------------------- macro ---
    async def poll_macro(self):
        try:
            c = await self.client()
            r = await c.get(CNBC_QUOTES)
            js = r.json()
            items = js.get("ITVQuoteResult", {}).get("ITVQuote", [])
            now = int(time.time() * 1000)
            for it in items:
                sym = it.get("symbol", "").lstrip(".")
                name = {"SPX": "SPX", "NDX": "NDX", "DXY": "DXY",
                        "US10Y": "US10Y", "US2Y": "US2Y"}.get(sym, sym)
                try:
                    last = _num(it.get("last"))
                except Exception:
                    continue
                if last is None:
                    continue
                prev = _num(it.get("previous_day_closing"))
                if prev is None or prev == 0:
                    prev = last
                q = {"symbol": name, "last": last, "prev": prev,
                     "chg_pct": round((last - prev) / prev * 100, 3) if prev else 0.0,
                     "ts": now, "change_type": it.get("changetype", "")}
                self.macro.quotes[name] = q
                h = self.macro.history.setdefault(name, [])
                if not h or h[-1][0] != now:
                    h.append((now, last))
                    del h[:-1500]
            self.macro.updated = now
            self.macro.errors.pop("cnbc", None)
        except Exception as e:
            self.macro.errors["cnbc"] = str(e)[:120]

    async def seed_macro_history(self, hours: int = 30):
        """CNBC's chart endpoint gives real intraday bars, so the correlation
        matrix is populated from the first cycle instead of after an hour of
        polling."""
        c = await self.client()
        for sym in (".SPX", ".NDX", ".DXY", "US10Y", "US2Y"):
            try:
                r = await c.get("https://ts-api.cnbc.com/harmony/app/charts/1D.json",
                                params={"symbol": sym, "interval": "5",
                                        "requestMethod": "extended"})
                bars = r.json().get("barData", {}).get("priceBars", [])
                key = sym.lstrip(".")
                h = self.macro.history.setdefault(key, [])
                have = {t for t, _ in h}
                bucket: dict[int, float] = {t: v for t, v in h}
                for b in bars:
                    raw = int(b.get("tradeTimeinMills") or 0)
                    if not raw:
                        continue
                    ts = raw - (raw % 300_000)     # snap to the 5m grid
                    try:
                        px = float(str(b.get("close")).replace(",", "")
                                   .replace("%", ""))
                    except Exception:
                        continue
                    bucket[ts] = px
                h[:] = sorted(bucket.items())
                h.sort()
                cut = int(time.time() * 1000) - hours * 3600_000
                self.macro.history[key] = [x for x in h if x[0] >= cut][-2000:]
                self.macro.errors.pop(f"seed_{key}", None)
            except Exception as e:
                self.macro.errors[f"seed_{key}"] = str(e)[:80]

    async def poll_fomc(self):
        """Authoritative FOMC schedule from the Federal Reserve.  This is the
        backup calendar that keeps SHIELD 3 alive when the ForexFactory feed
        rate-limits us."""
        try:
            c = await self.client()
            r = await c.get("https://www.federalreserve.gov/monetarypolicy/"
                            "fomccalendars.htm")
            events = parse_fomc(r.text)
            self.sent.fomc = events
            self.sent.errors.pop("fomc", None)
        except Exception as e:
            self.sent.errors["fomc"] = str(e)[:100]

    # ------------------------------------------------------ sentiment ---
    async def poll_fear_greed(self):
        try:
            c = await self.client()
            js = (await c.get(FEAR_GREED)).json()
            d = js.get("data", [])
            if d:
                self.sent.fear_greed = {
                    "value": int(d[0]["value"]),
                    "label": d[0]["value_classification"],
                    "prev": int(d[1]["value"]) if len(d) > 1 else None,
                    "ts": int(d[0]["timestamp"]) * 1000,
                }
            self.sent.errors.pop("fng", None)
        except Exception as e:
            self.sent.errors["fng"] = str(e)[:100]

    async def poll_stablecoins(self):
        try:
            c = await self.client()
            js = (await c.get(DEFILLAMA_STABLE)).json()
            if not isinstance(js, list) or len(js) < 2:
                return
            def tot(row):
                return float(row.get("totalCirculatingUSD", {}).get("peggedUSD", 0) or 0)
            now = js[-1]; d1 = js[-2]
            w = js[-8] if len(js) > 8 else js[0]
            m = js[-31] if len(js) > 31 else js[0]
            self.sent.stablecoin = {
                "supply": round(tot(now), 0),
                "inflow_24h": round(tot(now) - tot(d1), 0),
                "inflow_7d": round(tot(now) - tot(w), 0),
                "inflow_30d": round(tot(now) - tot(m), 0),
                "ts": int(float(now["date"])) * 1000,
            }
            self.sent.errors.pop("stable", None)
        except Exception as e:
            self.sent.errors["stable"] = str(e)[:100]

    async def poll_dominance(self):
        try:
            c = await self.client()
            js = (await c.get(COINGECKO_GLOBAL)).json()
            mp = js.get("data", {}).get("market_cap_percentage", {})
            ts = int(time.time() * 1000)
            snap = {"ts": ts, "usdt": round(float(mp.get("usdt", 0)), 4),
                    "usdc": round(float(mp.get("usdc", 0)), 4),
                    "btc": round(float(mp.get("btc", 0)), 4),
                    "eth": round(float(mp.get("eth", 0)), 4)}
            self.sent.dominance = snap
            self.sent.dominance_hist.append(snap)
            del self.sent.dominance_hist[:-240]
            self.sent.errors.pop("dom", None)
        except Exception as e:
            self.sent.errors["dom"] = str(e)[:100]

    def dominance_change_1h(self) -> float:
        """Percentage-point change in (USDT+USDC) dominance over ~1h."""
        h = self.sent.dominance_hist
        if len(h) < 2:
            return 0.0
        cut = h[-1]["ts"] - 3_600_000
        base = next((x for x in h if x["ts"] >= cut), h[0])
        cur = h[-1]
        return round(((cur["usdt"] + cur["usdc"]) - (base["usdt"] + base["usdc"]))
                     / max(base["usdt"] + base["usdc"], 1e-9) * 100, 3)

    # ----------------------------------------------------------- news ---
    async def poll_calendar(self):
        cache = os.path.join(os.path.dirname(DB_PATH), "calendar_cache.json")
        try:
            c = await self.client()
            js = None
            for attempt in range(3):
                r = await c.get(FOREXFACTORY)
                if r.status_code == 200 and r.text.strip().startswith("["):
                    js = r.json()
                    break
                await asyncio.sleep(4 * (attempt + 1))
            if js is None:
                # keep the last good calendar (a blackout shield built on stale
                # data beats no blackout shield at all)
                if os.path.exists(cache):
                    with open(cache) as f:
                        js = json.load(f)
                    self.sent.errors["ff"] = "rate limited - serving cached calendar"
                else:
                    self.sent.calendar = self._merge_fomc([])
                    self.sent.errors["ff"] = ("ForexFactory rate limited and no "
                                             "cache - calendar limited to official "
                                             "Fed FOMC schedule")
                    return
            else:
                try:
                    with open(cache, "w") as f:
                        json.dump(js, f)
                except Exception:
                    pass
                self.sent.errors.pop("ff", None)
            events = []
            now = time.time() * 1000
            for e in js:
                imp = (e.get("impact") or "").lower()
                if imp != "high":
                    continue
                try:
                    dt = _iso_ms(e.get("date", ""))
                except Exception:
                    continue
                events.append({
                    "title": e.get("title", ""),
                    "country": e.get("country", ""),
                    "date": dt,
                    "impact": "HIGH",
                    "forecast": e.get("forecast", ""),
                    "previous": e.get("previous", ""),
                    "mins_away": round((dt - now) / 60000, 1),
                })
            events.sort(key=lambda z: z["date"])
            self.sent.calendar = self._merge_fomc(events)
            self.sent.errors.pop("ff", None)
        except Exception as e:
            self.sent.errors["ff"] = str(e)[:120]

    async def poll_rss(self):
        try:
            c = await self.client()
            r = await c.get(COINTELEGRAPH)
            root = ET.fromstring(r.text)
            items = []
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                pub = (it.findtext("pubDate") or "").strip()
                desc = re.sub(r"<[^>]+>", "", it.findtext("description") or "")[:180]
                items.append({"title": title, "link": link, "pub": pub,
                              "desc": desc, "ts": _rss_ms(pub)})
            self.sent.news = items[:25]
            self.sent.updated = int(time.time() * 1000)
            self.sent.errors.pop("rss", None)
        except Exception as e:
            self.sent.errors["rss"] = str(e)[:120]

    def _merge_fomc(self, events: list[dict]) -> list[dict]:
        fomc = getattr(self.sent, "fomc", [])
        now = time.time() * 1000
        merged = list(events)
        for e in fomc:
            if e["date"] < now - 30 * 86_400_000:
                continue
            near = [x for x in merged if abs(x["date"] - e["date"]) < 12 * 3600_000
                    and "FOMC" in x["title"].upper()]
            if not near:
                merged.append({**e, "mins_away": round((e["date"] - now) / 60000, 1)})
        merged.sort(key=lambda z: z["date"])
        return merged

    # -------------------------------------------------------- shields ---
    def news_blackout(self) -> dict:
        now = time.time() * 1000
        win = SHIELDS.news_window_min * 60_000
        if SHIELDS.manual_blackout:
            return {"active": True, "events": [], "upcoming": self.sent.calendar[:6],
                    "window_min": SHIELDS.news_window_min,
                    "source": "OPERATOR", "degraded": False,
                    "manual": True}
        active = []
        for e in self.sent.calendar:
            delta = e["date"] - now
            if -win <= delta <= win:
                active.append({**e, "state": "IMMINENT" if delta >= 0 else "JUST_PRINTED"})
        # keyword-matched high impact events outside the window come next
        upcoming = [e for e in self.sent.calendar
                    if 0 < e["date"] - now <= 6 * 3600_000][:6]
        return {
            "active": bool(active),
            "events": active,
            "upcoming": upcoming,
            "window_min": SHIELDS.news_window_min,
            "source": ("ForexFactory + Fed FOMC"
                       if "ff" not in self.sent.errors else "Fed FOMC only"),
            "degraded": "ff" in self.sent.errors,
            "manual": False,
            "calendar_events": len(self.sent.calendar),
        }

    def correlation_rows(self, btc_series: list[float], btc_candles: list[dict],
                         btc_by_ts: dict[int, float] | None = None) -> list[dict]:
        """Correlation matrix against macro.  Uses aligned 5m closes where the
        macro feed has enough samples, otherwise returns a null cell rather than
        inventing a number."""
        rows = []
        pairs = [("DXY", -0.8), ("SPX", 0.6), ("NDX", 0.65), ("US10Y", -0.35),
                 ("US2Y", -0.3)]
        for sym, expected in pairs:
            h = self.macro.history.get(sym, [])
            corr = None
            if btc_by_ts and len(h) >= 20:
                corr = _aligned_return_corr(btc_by_ts, dict(h))
            elif len(h) >= 20 and len(btc_series) >= 20:
                mp = [v for _, v in h][-len(btc_series):]
                bp = btc_series[-len(mp):]
                if len(mp) >= 12 and len(bp) == len(mp):
                    corr = rolling_corr(bp, mp, len(mp))
            rows.append({"pair": f"BTC/{sym}", "corr": None if corr is None else round(corr, 3),
                         "expected": expected,
                         "price": self.macro.quotes.get(sym, {}).get("last"),
                         "chg_pct": self.macro.quotes.get(sym, {}).get("chg_pct")})
        # crypto internal correlations from the scanner price series
        return rows


# ------------------------------------------------------------------ utils
def _aligned_return_corr(a: dict[int, float], b: dict[int, float],
                         min_points: int = 20) -> float | None:
    """Pearson correlation of bar-to-bar returns on timestamps both series
    actually share.  Levels correlation across different assets is spurious;
    returns are what a desk quotes."""
    ts = sorted(set(a) & set(b))
    if len(ts) < min_points:
        return None
    ra = []; rb = []
    for i in range(1, len(ts)):
        pa0, pa1 = a[ts[i - 1]], a[ts[i]]
        pb0, pb1 = b[ts[i - 1]], b[ts[i]]
        if not (pa0 and pb0):
            continue
        ra.append(pa1 / pa0 - 1.0)
        rb.append(pb1 / pb0 - 1.0)
    if len(ra) < min_points:
        return None
    # a closed market carries its last close forward: zero returns everywhere
    # means the series has no information, so report nothing rather than zero
    if sum(1 for x in ra if x == 0) > 0.5 * len(ra) or \
       sum(1 for x in rb if x == 0) > 0.5 * len(rb):
        return None
    import statistics
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((x - mb) ** 2 for x in rb) ** 0.5
    if va < 1e-12 or vb < 1e-12:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    return round(float(cov / (va * vb)), 3)


def parse_fomc(html: str) -> list[dict]:
    """Extract FOMC decision dates from the Federal Reserve calendar page.

    Two independent passes over the same authoritative page:
      * the meeting markup (month + day range, `*` marks SEP meetings), which
        also covers meetings whose statement has not been published yet;
      * the statement filename (`monetary20261028a1.pdf`), which is exact.
    The statement lands at 14:00 New York: 18:00 UTC under EDT, 19:00 UTC
    under EST.
    """
    import re as _re
    from datetime import datetime as _dt, timedelta, timezone as _tz
    found: set[tuple[int, int, int]] = set()

    tokens = _re.findall(
        r"(\d{4})\s+FOMC\s+Meetings"
        r"|fomc-meeting__month[^>]*>\s*<strong>([A-Za-z]+)</strong>"
        r"|fomc-meeting__date[^>]*>\s*([0-9\-\*]+)", html)
    cur_year = cur_month = None
    for y, mo, days in tokens:
        if y:
            cur_year = int(y); continue
        if mo:
            cur_month = mo; continue
        if days and cur_year and cur_month:
            d = days.split("-")[-1].strip().rstrip("*")
            if not d.isdigit():
                continue
            try:
                dt = _dt(cur_year, int(d) and _month_num(cur_month), int(d))
            except Exception:
                continue
            found.add((dt.year, dt.month, dt.day))

    for m in _re.finditer(r"monetary(\d{4})(\d{2})(\d{2})a1", html):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y >= 2020:
            found.add((y, mo, d))

    out = []
    for (y, mo, d) in sorted(found):
        if y < 2020:
            continue
        dt = _dt(y, mo, d)
        march = _dt(y, 3, 8)
        dst_start = march + timedelta(days=(6 - march.weekday()) % 7)
        nov = _dt(y, 11, 1)
        dst_end = nov + timedelta(days=(6 - nov.weekday()) % 7)
        utc_hour = 18 if dst_start <= dt < dst_end else 19
        ts = int(_dt(y, mo, d, utc_hour, tzinfo=_tz.utc).timestamp() * 1000)
        out.append({"title": "FOMC Rate Decision", "country": "USD",
                    "date": ts, "impact": "HIGH", "forecast": "",
                    "previous": "", "source": "FED"})
    return out


def _month_num(name: str) -> int:
    from datetime import datetime as _dt
    for fmt in ("%B", "%b"):
        try:
            return _dt.strptime(name.title(), fmt).month
        except Exception:
            continue
    raise ValueError(name)


def _num(v):
    """CNBC sends '4.999%' for yields and '7,623.68' for indices."""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except Exception:
        return None


def _iso_ms(s: str) -> int:
    from datetime import datetime
    s = s.replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        from email.utils import parsedate_to_datetime
        return int(parsedate_to_datetime(s).timestamp() * 1000)


def _rss_ms(s: str) -> int:
    try:
        from email.utils import parsedate_to_datetime
        return int(parsedate_to_datetime(s).timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)
