/* ==========================================================================
   ChartEngine — TradingView lightweight-charts candles + hand-drawn overlay
   canvas for the seven maps.  Overlay coordinates are derived from the chart's
   own timeScale/priceScale so every drawing is pinned to real data.
   ========================================================================== */
const COL = {
  bull: '#00FF88', bear: '#FF0033', amber: '#FFB000', cyan: '#00C8FF',
  ink: '#D6D6D6', dim: '#6A6A6A', grid: '#141414',
};

class ChartEngine {
  constructor(chartEl, overlayEl) {
    this.el = chartEl;
    this.ov = overlayEl;
    this.ctx = overlayEl.getContext('2d');
    this.chart = null;
    this.series = null;
    this.lastBar = null;
    this.nBars = 0;
    this.state = null;
    this.toggles = { A: true, B: false, C: true, D: false, F: true, G: true };
    this._raf = null;
  }

  init() {
    this.chart = LightweightCharts.createChart(this.el, {
      layout: {
        background: { type: 'solid', color: '#080808' },
        textColor: '#5A5A5A',
        fontFamily: '"JetBrains Mono", ui-monospace, monospace',
        fontSize: 9,
      },
      grid: {
        vertLines: { color: '#101010' },
        horzLines: { color: '#101010' },
      },
      rightPriceScale: {
        borderColor: '#1C1C1C',
        scaleMargins: { top: 0.12, bottom: 0.12 },
        entireTextOnly: true,
      },
      timeScale: {
        borderColor: '#1C1C1C',
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 12,
        barSpacing: 5,
      },
      crosshair: {
        mode: 0,
        vertLine: { color: '#2A2A2A', width: 1, style: 2, labelBackgroundColor: '#1A1A1A' },
        horzLine: { color: '#2A2A2A', width: 1, style: 2, labelBackgroundColor: '#1A1A1A' },
      },
      handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
      watermark: {
        visible: true, fontSize: 42, color: 'rgba(255,255,255,0.022)',
        text: 'LIQUIDITY IS THE ONLY GOD', horzAlign: 'center', vertAlign: 'center',
      },
    });

    this.series = this.chart.addCandlestickSeries({
      upColor: COL.bull, downColor: COL.bear,
      borderUpColor: COL.bull, borderDownColor: COL.bear,
      wickUpColor: 'rgba(0,255,136,0.75)', wickDownColor: 'rgba(255,0,51,0.75)',
      priceLineColor: '#3A3A3A',
      priceLineStyle: 1,
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    });

    this.chart.timeScale().subscribeVisibleLogicalRangeChange(() => this.scheduleDraw());
    const ro = new ResizeObserver(() => this.resize());
    ro.observe(this.el.parentElement);
    this.resize();
  }

  resize() {
    const box = this.el.parentElement.getBoundingClientRect();
    this.W = Math.max(200, box.width);
    this.H = Math.max(120, box.height);
    const dpr = window.devicePixelRatio || 1;
    this.ov.width = this.W * dpr;
    this.ov.height = this.H * dpr;
    this.ov.style.width = this.W + 'px';
    this.ov.style.height = this.H + 'px';
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.chart.applyOptions({ width: this.W, height: this.H });
    this.scheduleDraw();
  }

  setCandles(candles) {
    if (!candles || !candles.length) return;
    const seen = new Set();
    const data = [];
    for (const c of candles) {
      if (seen.has(c.t)) continue;
      seen.add(c.t);
      data.push({ time: c.t, open: c.o, high: c.h, low: c.l, close: c.c });
    }
    data.sort((a, b) => a.time - b.time);
    this.series.setData(data);
    this.lastBar = candles[candles.length - 1];
    this.nBars = data.length;
    this.chart.timeScale().fitContent();
    this.chart.timeScale().applyOptions({ rightOffset: 14 });
  }

  updateBar(bar) {
    if (!bar) return;
    try {
      this.series.update({ time: bar.t, open: bar.o, high: bar.h, low: bar.l, close: bar.c });
      this.lastBar = bar;
    } catch (e) { /* out-of-order bar during reconnect */ }
  }

  toggle(key, on) { this.toggles[key] = on; this.scheduleDraw(); }

  scheduleDraw() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = null; this.draw(); });
  }

  X(t) { return this.chart.timeScale().timeToCoordinate(t); }
  Y(p) { return this.series.priceToCoordinate(p); }
  plotRight() {
    try { return this.W - this.chart.priceScale('right').width() - 1; } catch (e) { return this.W - 60; }
  }

  /* ============================== master draw ============================ */
  draw() {
    const ctx = this.ctx;
    if (!ctx) return;
    ctx.clearRect(0, 0, this.W, this.H);
    const s = this.state;
    if (!s || !s.maps) return;
    const m = s.maps;
    const lastT = this.lastBar ? this.lastBar.t : Math.floor(Date.now() / 1000);
    const right = this.plotRight();

    if (this.toggles.A && m.liq_map && m.liq_map.rows) this.drawLiqHeat(m.liq_map, right);
    if (this.toggles.D && m.profile && m.profile.ok) this.drawProfile(m.profile, right);
    if (this.toggles.F && m.liq_map && m.liq_map.rows) this.drawLiqColumn(m.liq_map, right);
    if (this.toggles.C && m.smc && m.smc.ok) this.drawSMC(m.smc, lastT, right);
    if (this.toggles.A && m.liquidity) this.drawRails(m.liquidity, lastT, right);
    if (this.toggles.B && m.footprint && m.footprint.ok) this.drawFootprintGhost(m.footprint);
    if (this.toggles.G && m.prediction && m.prediction.ok) this.drawPrediction(m.prediction, lastT, right);
    this.drawPositions(s.positions || []);
    this.drawHud(s, m);
  }

  /* -------------------------- MAP A: liquidity heat ---------------------- */
  drawLiqHeat(lm, right) {
    const ctx = this.ctx;
    for (const r of lm.rows) {
      const y = this.Y(r.price);
      if (y === null || y < 0 || y > this.H) continue;
      if (r.intensity <= 0.07) continue;
      const a = Math.pow(r.intensity, 2.2) * 0.30;
      const longSide = r.long_usd >= r.short_usd;
      ctx.fillStyle = longSide
        ? `rgba(255,0,51,${a.toFixed(3)})`
        : `rgba(0,255,136,${a.toFixed(3)})`;
      const h = Math.max(1.4, (this.H / 90));
      ctx.fillRect(0, y - h / 2, right, h);
    }
    // magnet lines (highest liquidation density each side)
    if (lm.magnet_up) this.magnetLine(lm.magnet_up, 'rgba(0,255,136,0.55)', 'SHORT LIQ MAGNET', right);
    if (lm.magnet_down) this.magnetLine(lm.magnet_down, 'rgba(255,0,51,0.55)', 'LONG LIQ MAGNET', right);
  }

  magnetLine(price, color, label, right) {
    const y = this.Y(price);
    if (y === null || y < 0 || y > this.H) return;
    const ctx = this.ctx;
    ctx.save();
    ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash([7, 4]);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(right, y); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color; ctx.font = '8px "JetBrains Mono", monospace';
    ctx.textAlign = 'right';
    ctx.fillText(label, right - 6, y - 3);
    ctx.restore();
  }

  /* ------------------- MAP F: liquidation column on right ----------------- */
  drawLiqColumn(lm, right) {
    const ctx = this.ctx;
    const w = 34;
    const x0 = right - w;
    for (const r of lm.rows) {
      const y = this.Y(r.price);
      if (y === null || y < 0 || y > this.H) continue;
      const len = r.intensity * w;
      if (len < 0.6) continue;
      const longSide = r.long_usd >= r.short_usd;
      ctx.fillStyle = longSide ? 'rgba(255,0,51,0.55)' : 'rgba(0,255,136,0.55)';
      ctx.fillRect(x0, y - 0.8, len, 1.6);
    }
    // 50/100/200M trigger markers
    const trg = lm.triggers || {};
    ctx.font = '7.5px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    for (const key of ['50M', '100M', '200M']) {
      const t = trg[key]; if (!t) continue;
      for (const side of ['up', 'down']) {
        const p = t[side]; if (!p) continue;
        const y = this.Y(p);
        if (y === null || y < 0 || y > this.H) continue;
        const col = side === 'up' ? 'rgba(0,255,136,0.85)' : 'rgba(255,0,51,0.85)';
        ctx.strokeStyle = col;
        ctx.beginPath(); ctx.moveTo(x0 - 4, y); ctx.lineTo(right, y); ctx.stroke();
        ctx.fillStyle = col;
        ctx.fillText(key, x0 - 2, y - 2);
      }
    }
    ctx.save();
    ctx.fillStyle = 'rgba(106,106,106,0.9)';
    ctx.font = '7px "JetBrains Mono", monospace';
    ctx.textAlign = 'right';
    ctx.fillText('LIQ MAP', right - 2, 12);
    ctx.restore();
  }

  /* --------------- MAP A: BSL / SSL shelves with notional ---------------- */
  drawRails(liq, lastT, right) {
    const ctx = this.ctx;
    const rail = (rows, buy) => {
      const shown = (rows || []).slice(0, 5);
      const ranked = [...shown].sort((a, b) => (b.usd || 0) - (a.usd || 0));
      const labelled = new Set(ranked.slice(0, 2).map(r => r.price));
      for (const r of shown) {
        const y = this.Y(r.price);
        if (y === null || y < -10 || y > this.H + 10) continue;
        const a = 0.18 + 0.5 * (r.intensity || 0);
        const col = buy ? `rgba(0,255,136,${a})` : `rgba(255,0,51,${a})`;
        ctx.strokeStyle = col;
        ctx.lineWidth = 0.9;
        ctx.setLineDash([2, 4]);
        ctx.beginPath();
        ctx.moveTo(right * 0.32, y);
        ctx.lineTo(right - 40, y);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = col;
        ctx.beginPath(); ctx.arc(right * 0.32, y, 1.6, 0, 6.283); ctx.fill();
        if (labelled.has(r.price)) {
          ctx.font = '7px "JetBrains Mono", monospace';
          ctx.textAlign = 'right';
          ctx.fillText(`${r.label}${r.usd ? ' ' + F.usd(r.usd) : ''}`, right - 42, y - 2);
        }
      }
    };
    rail(liq.bsl, true);
    rail(liq.ssl, false);
  }

  /* ------------------------- MAP C: smart money -------------------------- */
  drawSMC(smc, lastT, right) {
    const ctx = this.ctx;
    const st = smc.structure || {};
    const xNow = this.X(lastT) ?? right;
    const xEnd = Math.min(right, xNow + 70);

    // ---- FVGs
    for (const f of smc.fvgs || []) {
      const yTop = this.Y(f.top), yBot = this.Y(f.bot);
      if (yTop === null || yBot === null) continue;
      const x1 = this.X(f.t);
      if (x1 === null) continue;
      const bull = f.kind === 'BULL_FVG';
      ctx.fillStyle = bull ? 'rgba(0,255,136,0.07)' : 'rgba(255,0,51,0.07)';
      ctx.strokeStyle = bull ? 'rgba(0,255,136,0.32)' : 'rgba(255,0,51,0.32)';
      ctx.lineWidth = 0.7;
      const x2 = Math.max(xEnd, x1 + 8);
      ctx.fillRect(x1, yTop, x2 - x1, Math.max(1, yBot - yTop));
      ctx.strokeRect(x1, yTop, x2 - x1, Math.max(1, yBot - yTop));
      ctx.fillStyle = bull ? 'rgba(0,255,136,0.75)' : 'rgba(255,0,51,0.75)';
      ctx.font = '7px "JetBrains Mono", monospace';
      ctx.textAlign = 'left';
      ctx.fillText(bull ? 'FVG ▲' : 'FVG ▼', x1 + 2, yTop - 2);
    }

    // ---- Order blocks
    for (const ob of smc.order_blocks || []) {
      const yTop = this.Y(ob.top), yBot = this.Y(ob.bot);
      if (yTop === null || yBot === null) continue;
      const x1 = this.X(ob.t);
      if (x1 === null) continue;
      const bull = ob.kind === 'BULL_OB';
      const alive = !ob.mitigated;
      ctx.fillStyle = bull
        ? `rgba(0,255,136,${alive ? 0.10 : 0.03})`
        : `rgba(255,0,51,${alive ? 0.10 : 0.03})`;
      ctx.strokeStyle = bull
        ? `rgba(0,255,136,${alive ? 0.55 : 0.2})`
        : `rgba(255,0,51,${alive ? 0.55 : 0.2})`;
      ctx.lineWidth = 0.9;
      const x2 = Math.max(xEnd, x1 + 8);
      const hgt = Math.max(1, yBot - yTop);
      ctx.fillRect(x1, yTop, x2 - x1, hgt);
      ctx.strokeRect(x1, yTop, x2 - x1, hgt);
      ctx.fillStyle = bull ? 'rgba(0,255,136,0.85)' : 'rgba(255,0,51,0.85)';
      ctx.font = '7px "JetBrains Mono", monospace';
      ctx.textAlign = 'left';
      ctx.fillText(`OB ${ob.strength}×${alive ? '' : ' ✕'}`, x1 + 2, yBot + 7);
    }

    // ---- BOS / MSS
    for (const b of st.breaks || []) {
      const y = this.Y(b.price);
      const x = this.X(b.t);
      if (y === null || x === null) continue;
      const col = b.dir === 'bull' ? 'rgba(0,255,136,0.8)' : 'rgba(255,0,51,0.8)';
      ctx.strokeStyle = col; ctx.lineWidth = 1.1;
      ctx.beginPath(); ctx.moveTo(x - 60, y); ctx.lineTo(x, y); ctx.stroke();
      ctx.fillStyle = col;
      ctx.font = 'bold 7.5px "JetBrains Mono", monospace';
      ctx.textAlign = 'right';
      ctx.fillText(b.type, x - 2, y - 3);
    }

    // ---- equal highs/lows => liquidity pools
    for (const p of smc.pools || []) {
      const y = this.Y(p.price);
      if (y === null) continue;
      const x1 = this.X(p.t), x2 = this.X(p.t2);
      if (x1 === null || x2 === null) continue;
      const col = p.kind === 'EQH' ? 'rgba(0,255,136,0.9)' : 'rgba(255,0,51,0.9)';
      ctx.strokeStyle = col; ctx.lineWidth = 1;
      ctx.setLineDash([3, 2]);
      ctx.beginPath(); ctx.moveTo(x1, y); ctx.lineTo(x2, y); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = col;
      ctx.beginPath(); ctx.arc(x1, y, 2.2, 0, 6.283); ctx.fill();
      ctx.beginPath(); ctx.arc(x2, y, 2.2, 0, 6.283); ctx.fill();
      ctx.font = '7px "JetBrains Mono", monospace';
      ctx.textAlign = 'left';
      ctx.fillText('LIQUIDITY POOL', Math.max(x2, x1) + 4, y + 2);
    }

    // ---- sweeps
    for (const s of smc.sweeps || []) {
      const y = this.Y(s.price), x = this.X(s.t);
      if (y === null || x === null) continue;
      const col = s.dir === 'bull' ? COL.bull : COL.bear;
      ctx.strokeStyle = col; ctx.lineWidth = 1.3;
      ctx.beginPath();
      ctx.moveTo(x - 3, y - 3); ctx.lineTo(x + 3, y + 3);
      ctx.moveTo(x + 3, y - 3); ctx.lineTo(x - 3, y + 3);
      ctx.stroke();
      ctx.fillStyle = col;
      ctx.font = '7px "JetBrains Mono", monospace';
      ctx.textAlign = 'left';
      ctx.fillText('SWEEP', x + 5, y - 4);
    }

    // ---- premium / discount shading
    if (st.eq && st.range_high && st.range_low) {
      const yEq = this.Y(st.eq), yHi = this.Y(st.range_high), yLo = this.Y(st.range_low);
      if (yEq !== null && yHi !== null) {
        ctx.fillStyle = 'rgba(255,0,51,0.025)';
        ctx.fillRect(0, yHi, right, Math.max(0, yEq - yHi));
      }
      if (yEq !== null && yLo !== null) {
        ctx.fillStyle = 'rgba(0,255,136,0.025)';
        ctx.fillRect(0, yEq, right, Math.max(0, yLo - yEq));
      }
      if (yEq !== null) {
        ctx.strokeStyle = 'rgba(180,180,180,0.22)'; ctx.lineWidth = 0.8;
        ctx.setLineDash([5, 5]);
        ctx.beginPath(); ctx.moveTo(0, yEq); ctx.lineTo(right, yEq); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = 'rgba(160,160,160,0.6)';
        ctx.font = '7px "JetBrains Mono", monospace';
        ctx.textAlign = 'left';
        ctx.fillText('EQUILIBRIUM', 4, yEq - 3);
      }
    }
  }

  /* --------------------- MAP D: volume profile --------------------------- */
  drawProfile(p, right) {
    const ctx = this.ctx;
    const maxW = 96;
    const x0 = right - 36 - maxW; // leave room for the liquidation column
    const maxVol = Math.max(...p.levels.map(l => l.vol), 1);
    for (const l of p.levels) {
      const y = this.Y(l.price);
      if (y === null || y < 0 || y > this.H) continue;
      const w = (l.vol / maxVol) * maxW;
      const inVA = l.price >= p.val && l.price <= p.vah;
      ctx.fillStyle = l.node === 'HVN' ? 'rgba(0,200,255,0.45)'
        : inVA ? 'rgba(0,200,255,0.22)' : 'rgba(120,120,120,0.14)';
      ctx.fillRect(x0 + (maxW - w), y - 0.9, w, 1.8);
    }
    const line = (price, col, label) => {
      const y = this.Y(price);
      if (y === null) return;
      ctx.strokeStyle = col; ctx.lineWidth = 0.9;
      ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(right, y); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = col;
      ctx.font = '7px "JetBrains Mono", monospace';
      ctx.textAlign = 'left';
      ctx.fillText(`${label} ${F.price(price)}`, x0 + 1, y - 2);
    };
    line(p.poc, 'rgba(255,176,0,0.95)', 'POC');
    line(p.vah, 'rgba(0,200,255,0.6)', 'VAH');
    line(p.val, 'rgba(0,200,255,0.6)', 'VAL');
  }

  /* --------------------- MAP B: footprint ghost -------------------------- */
  drawFootprintGhost(fp) {
    const ctx = this.ctx;
    const buckets = (fp.candles || []).slice(-24);
    const maxBucket = Math.max(...buckets.map(b => b.total), 1);
    for (const b of buckets) {
      const x = this.X(b.t);
      if (x === null) continue;
      const w = Math.max(3, Math.min(14, 480 / Math.max(this.nBars, 1)));
      for (const r of b.rows || []) {
        const y = this.Y(r.price);
        if (y === null) continue;
        const tot = r.bid + r.ask;
        const inten = Math.min(1, tot / (maxBucket / 6));
        if (inten < 0.08) continue;
        ctx.fillStyle = r.delta >= 0
          ? `rgba(0,255,136,${(0.28 * inten).toFixed(3)})`
          : `rgba(255,0,51,${(0.28 * inten).toFixed(3)})`;
        ctx.fillRect(x - w / 2, y - 0.9, w, 1.8);
      }
      // imbalance marker: delta of the bucket
      const norm = b.delta / maxBucket;
      ctx.fillStyle = b.delta >= 0 ? 'rgba(0,255,136,0.85)' : 'rgba(255,0,51,0.85)';
      const bh = Math.abs(norm) * 26;
      const yTop = this.H - 3 - bh;
      ctx.fillRect(x - 1.4, yTop, 2.8, bh);
    }
  }

  /* ------------------------ MAP G: AI prediction ------------------------- */
  drawPrediction(pred, lastT, right) {
    const ctx = this.ctx;
    const path = pred.path || [];
    if (!path.length) return;
    const x0 = this.X(lastT) ?? right;
    const pts = [{ x: x0, y: this.Y(this.lastBar ? this.lastBar.c : path[0].p) }];
    for (const pt of path) {
      const x = this.X(pt.t), y = this.Y(pt.p);
      if (x === null || y === null) continue;
      pts.push({ x, y });
    }
    if (pts.length < 2) {
      // future bars sit past the right offset: extend using the real bar pitch
      const step = Math.max(2.5, Math.min(14, Math.abs(x0 - (this.X(lastT - 300) ?? (x0 - 5)))));
      const limit = this.W - 46;
      let x = x0;
      for (const pt of path) {
        x += step;
        if (x > limit) break;
        const y = this.Y(pt.p);
        if (y !== null) pts.push({ x, y });
      }
    }
    if (pts.length < 2) return;
    const up = pred.direction === 'UP';
    const col = up ? COL.bull : COL.bear;

    // confidence band
    const band = pred.band || [];
    if (band.length) {
      ctx.beginPath();
      let started = false;
      const bandPts = [];
      for (const b of band) {
        const x = this.X(b.t), yh = this.Y(b.hi), yl = this.Y(b.lo);
        if (x === null || yh === null || yl === null) continue;
        bandPts.push({ x, yh, yl });
      }
      if (bandPts.length > 1) {
        ctx.moveTo(bandPts[0].x, bandPts[0].yh);
        for (const p of bandPts) ctx.lineTo(p.x, p.yh);
        for (let i = bandPts.length - 1; i >= 0; i--) ctx.lineTo(bandPts[i].x, bandPts[i].yl);
        ctx.closePath();
        ctx.fillStyle = up ? 'rgba(0,255,136,0.09)' : 'rgba(255,0,51,0.09)';
        ctx.fill();
      }
    }

    ctx.strokeStyle = col;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 3]);
    ctx.beginPath();
    ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
    ctx.stroke();
    ctx.setLineDash([]);

    const last = pts[pts.length - 1];
    ctx.fillStyle = col;
    ctx.beginPath(); ctx.arc(last.x, last.y, 2.6, 0, 6.283); ctx.fill();
    ctx.font = 'bold 8px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    const lbl = `LSTM ${pred.direction} ${pred.cum_return_pct > 0 ? '+' : ''}${pred.cum_return_pct}%`;
    ctx.fillText(lbl, Math.min(last.x + 6, this.W - 120), last.y - 5);
  }

  /* --------------------- live position levels --------------------------- */
  drawPositions(positions) {
    if (!positions || !positions.length) return;
    const ctx = this.ctx;
    const p = positions[0];
    const right = this.plotRight();
    const x1 = right * 0.30;
    const lvl = (price, col, label, dashed) => {
      const y = this.Y(price);
      if (y === null || y < 0 || y > this.H) return;
      ctx.strokeStyle = col; ctx.lineWidth = 1;
      ctx.setLineDash(dashed ? [4, 4] : []);
      ctx.beginPath(); ctx.moveTo(x1, y); ctx.lineTo(right - 40, y); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = col;
      ctx.font = 'bold 7.5px "JetBrains Mono", monospace';
      ctx.textAlign = 'left';
      ctx.fillText(label, x1 + 2, y - 2.5);
    };
    const long = p.side === 'LONG';
    lvl(p.entry, '#FFFFFF', `ENTRY ${F.price(p.entry)}`, false);
    lvl(p.sl, 'rgba(255,0,51,0.95)', `SL ${F.price(p.sl)}`, true);
    if (!(p.legs || []).find(l => l.name === 'TP1')) lvl(p.tp1, 'rgba(0,255,136,0.75)', 'TP1 1R', true);
    if (!(p.legs || []).find(l => l.name === 'TP2')) lvl(p.tp2, 'rgba(0,255,136,0.55)', 'TP2 2R', true);
    if (!(p.legs || []).find(l => l.name === 'TP3')) lvl(p.tp3, 'rgba(0,255,136,0.40)', 'TP3 4R', true);
  }

  /* ------------------------------ HUD ----------------------------------- */
  drawHud(s, m) {
    const ctx = this.ctx;
    const st = (m.smc && m.smc.structure) || {};
    const lines = [];
    lines.push([s.focus.base, '#FFFFFF']);
    if (st.bias) lines.push([`BIAS ${st.bias}`, st.bias === 'BULLISH' ? COL.bull : st.bias === 'BEARISH' ? COL.bear : COL.dim]);
    if (st.zone) lines.push([`ZONE ${st.zone}`, st.zone === 'DISCOUNT' ? COL.bull : COL.bear]);
    if (m.profile && m.profile.ok) lines.push([`VA ${m.profile.position}`, COL.cyan]);
    ctx.font = '8.5px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    let y = 14;
    for (const [text, color] of lines) {
      ctx.fillStyle = color;
      ctx.fillText(text, 6, y);
      y += 11;
    }
    // divine score chip top-right of plot
    const sc = s.tick && s.tick.divine ? s.tick.divine.score : 0;
    const dir = s.tick && s.tick.divine ? s.tick.divine.direction : '';
    const col = F.scoreColor(sc);
    const label = `${dir} ${sc.toFixed(1)}`;
    ctx.font = 'bold 10px "JetBrains Mono", monospace';
    const w = ctx.measureText(label).width + 14;
    ctx.fillStyle = 'rgba(10,10,10,0.85)';
    ctx.fillRect(this.plotRight() - w - 4, 6, w, 17);
    ctx.strokeStyle = col; ctx.lineWidth = 1;
    ctx.strokeRect(this.plotRight() - w - 4, 6, w, 17);
    ctx.fillStyle = col;
    ctx.textAlign = 'center';
    ctx.fillText(label, this.plotRight() - w / 2 - 4, 18);
  }
}

/* ==========================================================================
   Sub-chart renderers (plain canvases)
   ========================================================================== */
const SubCharts = {
  fit(canvas) {
    const dpr = window.devicePixelRatio || 1;
    // measure the parent so the canvas never inflates its own container
    const parent = canvas.parentElement;
    const box = parent.getBoundingClientRect();
    const head = parent.querySelector('.sub-h');
    const h = Math.max(24, box.height - (head ? head.offsetHeight : 0) - 2);
    const w = Math.max(40, box.width - 2);
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w: box.width, h: box.height };
  },

  cvd(canvas, fp) {
    const c = this.fit(canvas); if (!c) return;
    const { ctx, w, h } = c;
    ctx.clearRect(0, 0, w, h);
    const candles = (fp.candles || []).slice(-60);
    if (candles.length < 3) {
      ctx.fillStyle = '#2E2E2E'; ctx.font = '9px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';
      ctx.fillText('ACCUMULATING TAPE…', w / 2, h / 2);
      return;
    }
    const vals = candles.map(x => x.cvd);
    const min = Math.min(...vals), max = Math.max(...vals);
    const span = (max - min) || 1;
    const yOf = v => h - 6 - ((v - min) / span) * (h - 14);
    const xOf = i => (i / (candles.length - 1)) * (w - 4) + 2;

    // zero line
    if (min < 0 && max > 0) {
      const y0 = yOf(0);
      ctx.strokeStyle = '#2A2A2A'; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(0, y0); ctx.lineTo(w, y0); ctx.stroke();
      ctx.setLineDash([]);
    }
    // gradient fill
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    const up = vals[vals.length - 1] >= vals[0];
    grad.addColorStop(0, up ? 'rgba(0,255,136,0.28)' : 'rgba(255,0,51,0.28)');
    grad.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.beginPath();
    ctx.moveTo(xOf(0), h);
    vals.forEach((v, i) => ctx.lineTo(xOf(i), yOf(v)));
    ctx.lineTo(xOf(vals.length - 1), h);
    ctx.closePath();
    ctx.fillStyle = grad; ctx.fill();
    // line
    ctx.beginPath();
    vals.forEach((v, i) => i ? ctx.lineTo(xOf(i), yOf(v)) : ctx.moveTo(xOf(i), yOf(v)));
    ctx.strokeStyle = up ? COL.bull : COL.bear; ctx.lineWidth = 1.3; ctx.stroke();
    // delta bars
    const maxAbs = Math.max(...candles.map(x => Math.abs(x.delta)), 1);
    candles.forEach((cd, i) => {
      const bh = (Math.abs(cd.delta) / maxAbs) * (h * 0.3);
      ctx.fillStyle = cd.delta >= 0 ? 'rgba(0,255,136,0.30)' : 'rgba(255,0,51,0.30)';
      const x = xOf(i);
      ctx.fillRect(x - 1, cd.delta >= 0 ? h - bh : h, 2, bh);
    });
    ctx.fillStyle = '#4E4E4E'; ctx.font = '7.5px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText(F.usd(vals[vals.length - 1]), 3, 9);
  },

  footprint(canvas, fp) {
    const c = this.fit(canvas); if (!c) return;
    const { ctx, w, h } = c;
    ctx.clearRect(0, 0, w, h);
    const buckets = (fp.candles || []).slice(-14);
    if (buckets.length < 2) {
      ctx.fillStyle = '#2E2E2E'; ctx.font = '9px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';
      ctx.fillText('ACCUMULATING TAPE…', w / 2, h / 2);
      return;
    }
    const colW = w / buckets.length;
    const maxTot = Math.max(...buckets.flatMap(b => (b.rows || []).map(r => r.bid + r.ask)), 1);
    const lo = Math.min(...buckets.flatMap(b => (b.rows || []).map(r => r.price)));
    const hi = Math.max(...buckets.flatMap(b => (b.rows || []).map(r => r.price)));
    const span = (hi - lo) || 1;
    buckets.forEach((b, i) => {
      const x = i * colW;
      for (const r of b.rows || []) {
        const y = h - 4 - ((r.price - lo) / span) * (h - 12);
        const tot = r.bid + r.ask;
        const wid = (tot / maxTot) * colW * 0.92;
        const cx = x + colW / 2;
        const bw = (r.bid / (tot || 1)) * wid;
        const aw = (r.ask / (tot || 1)) * wid;
        ctx.fillStyle = 'rgba(255,0,51,0.55)';
        ctx.fillRect(cx - bw, y - 0.7, bw, 1.4);
        ctx.fillStyle = 'rgba(0,255,136,0.55)';
        ctx.fillRect(cx, y - 0.7, aw, 1.4);
      }
      // delta ribbon at top
      const dv = b.delta / (maxTot || 1);
      const bh = F.clamp(Math.abs(dv) * h * 0.5, 1, 14);
      ctx.fillStyle = b.delta >= 0 ? 'rgba(0,255,136,0.8)' : 'rgba(255,0,51,0.8)';
      ctx.fillRect(x + colW / 2 - 1.5, 2, 3, bh);
    });
    ctx.strokeStyle = '#141414';
    ctx.beginPath(); ctx.moveTo(0, h - 3); ctx.lineTo(w, h - 3); ctx.stroke();
  },

  book(canvas, book) {
    const c = this.fit(canvas); if (!c) return;
    const { ctx, w, h } = c;
    ctx.clearRect(0, 0, w, h);
    if (!book || !book.asks || !book.asks.length) return;
    const asks = (book.asks || []).slice(0, 12).reverse();
    const bids = (book.bids || []).slice(0, 12);
    const rows = asks.length + bids.length;
    const rowH = Math.min(9, h / rows);
    const maxUsd = Math.max(...asks.concat(bids).map(r => r.usd), 1);
    let y = 0;
    ctx.font = '7px "JetBrains Mono", monospace';
    for (const a of asks) {
      const bw = (a.usd / maxUsd) * (w * 0.62);
      ctx.fillStyle = 'rgba(255,0,51,0.16)';
      ctx.fillRect(w - bw, y, bw, rowH - 0.6);
      ctx.fillStyle = 'rgba(255,90,120,0.85)';
      ctx.textAlign = 'left';
      ctx.fillText(F.price(a.p), 1, y + rowH - 2);
      ctx.textAlign = 'right';
      ctx.fillText(F.usd(a.usd), w - 2, y + rowH - 2);
      y += rowH;
    }
    ctx.strokeStyle = '#2A2A2A';
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    y += 1.5;
    for (const b of bids) {
      const bw = (b.usd / maxUsd) * (w * 0.62);
      ctx.fillStyle = 'rgba(0,255,136,0.16)';
      ctx.fillRect(w - bw, y, bw, rowH - 0.6);
      ctx.fillStyle = 'rgba(80,255,170,0.85)';
      ctx.textAlign = 'left';
      ctx.fillText(F.price(b.p), 1, y + rowH - 2);
      ctx.textAlign = 'right';
      ctx.fillText(F.usd(b.usd), w - 2, y + rowH - 2);
      y += rowH;
    }
    ctx.fillStyle = '#6A6A6A';
    ctx.textAlign = 'center';
    ctx.fillText(`IMB ${book.imbalance > 0 ? '+' : ''}${book.imbalance}%`, w / 2, h - 1);
  },

  equity(canvas, curve) {
    const c = this.fit(canvas); if (!c) return;
    const { ctx, w, h } = c;
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = '#161616';
    ctx.beginPath(); ctx.moveTo(0, h - 1); ctx.lineTo(w, h - 1); ctx.stroke();
    if (!curve || curve.length < 2) {
      ctx.fillStyle = '#2E2E2E'; ctx.font = '8px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';
      ctx.fillText('NO CLOSED TRADES', w / 2, h / 2);
      return;
    }
    const vals = curve.map(p => p.eq);
    const min = Math.min(...vals, 0), max = Math.max(...vals, 0);
    const span = (max - min) || 1;
    const yOf = v => h - 3 - ((v - min) / span) * (h - 8);
    const xOf = i => (i / (curve.length - 1)) * (w - 2) + 1;
    ctx.beginPath();
    vals.forEach((v, i) => i ? ctx.lineTo(xOf(i), yOf(v)) : ctx.moveTo(xOf(i), yOf(v)));
    ctx.strokeStyle = vals[vals.length - 1] >= 0 ? COL.bull : COL.bear;
    ctx.lineWidth = 1.2; ctx.stroke();
    ctx.lineTo(xOf(vals.length - 1), h); ctx.lineTo(xOf(0), h); ctx.closePath();
    ctx.fillStyle = vals[vals.length - 1] >= 0 ? 'rgba(0,255,136,0.12)' : 'rgba(255,0,51,0.12)';
    ctx.fill();
  },
};
