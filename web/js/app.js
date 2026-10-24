/* ==========================================================================
   App — transport (WebSocket with automatic HTTP fallback), state and wiring
   ========================================================================== */
const App = {
  engine: null,
  ws: null,
  boot: null,
  snap: null,
  sym: null,
  wsFails: 0,
  pollTimer: null,
  reconnectTimer: null,
  lastHeavy: 0,
  base: '',            // engine origin, '' = same origin
  linked: false,

  /* ------------------------- engine link resolution -------------------- */
  resolveBase() {
    const qs = new URLSearchParams(location.search).get('api');
    if (qs) { localStorage.setItem('omni_engine', qs); return this.normBase(qs); }
    const stored = localStorage.getItem('omni_engine');
    if (stored) return this.normBase(stored);
    if (window.OMNI_ENGINE_URL) return this.normBase(window.OMNI_ENGINE_URL);
    return '';
  },

  normBase(u) {
    if (!u) return '';
    let v = String(u).trim().replace(/\/+$/, '');
    if (!/^https?:\/\//i.test(v)) v = 'https://' + v;
    return v;
  },

  api(path) { return this.base + path; },

  wsURL() {
    if (this.base) {
      const u = new URL(this.base);
      const proto = u.protocol === 'https:' ? 'wss:' : 'ws:';
      return `${proto}//${u.host}/ws`;
    }
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}/ws`;
  },

  async probe(base) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 6000);
    try {
      const r = await fetch(this.normBase(base) + '/api/health',
                            { signal: ctl.signal, cache: 'no-store' });
      if (!r.ok) return { ok: false, why: 'HTTP ' + r.status };
      const js = await r.json();
      if (!js.ok) return { ok: false, why: 'engine unhealthy' };
      return { ok: true, js };
    } catch (e) {
      return { ok: false, why: e.name === 'AbortError' ? 'timeout' : 'unreachable' };
    } finally { clearTimeout(timer); }
  },

  async boot() {
    this.base = this.resolveBase();
    const gate = document.getElementById('gate');
    const app = document.getElementById('app');

    if (this.base) {
      const res = await this.probe(this.base);
      if (res.ok) return this.launch(res.js);
      this.showGate(gate, app, `could not reach ${this.base} (${res.why})`);
      return;
    }
    // same origin: local / sandbox deployment with the engine co-hosted
    const res = await this.probe('');
    if (res.ok) return this.launch(res.js);
    this.showGate(gate, app, 'no engine on this origin');
  },

  showGate(gate, app, msg) {
    gate.classList.remove('hidden');
    app.classList.add('hidden');
    const input = document.getElementById('gate-url');
    input.value = this.base || '';
    const status = document.getElementById('gate-status');
    status.textContent = msg;
    status.className = 'gate-status err';
    const go = async () => {
      const candidate = this.normBase(input.value);
      if (!candidate && !location.host) return;
      if (location.protocol === 'https:' && candidate.startsWith('http://')) {
        status.textContent = 'mixed content: an https page cannot reach an http engine — use https';
        status.className = 'gate-status err';
        return;
      }
      status.textContent = 'testing ' + (candidate || location.origin) + ' …';
      status.className = 'gate-status';
      const res = await this.probe(candidate);
      if (res.ok) {
        this.base = candidate;
        if (candidate) localStorage.setItem('omni_engine', candidate);
        else localStorage.removeItem('omni_engine');
        status.textContent = `linked — ${res.js.ml_ready ? 'models ready' : 'models warming'}, wire ${res.js.wire ? 'up' : 'down'}`;
        status.className = 'gate-status ok';
        setTimeout(() => this.launch(res.js), 500);
      } else {
        status.textContent = `failed: ${res.why}`;
        status.className = 'gate-status err';
      }
    };
    document.getElementById('gate-connect').onclick = go;
    input.onkeydown = (e) => { if (e.key === 'Enter') go(); };
    input.focus();
  },

  launch(health) {
    document.getElementById('gate').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');
    this.linked = true;
    this.engine = new ChartEngine(
      document.getElementById('chart'),
      document.getElementById('overlay'));
    this.engine.init();
    this.bindControls();
    this.connectWS();
    setTimeout(() => { if (!this.snap) this.pollOnce(); }, 2000);
    window.addEventListener('resize', () => {
      this.engine.resize();
      this.drawSubCharts();
    });
    setInterval(() => { if (this.snap) P.top(this.snap); }, 1000);
  },

  connectWS() {
    let url;
    try { url = this.wsURL(); } catch (e) { this.startPolling(); return; }
    try {
      this.ws = new WebSocket(url);
    } catch (e) { this.startPolling(); return; }

    this.ws.onopen = () => {
      this.wsFails = 0;
      this.stopPolling();
      this.setConn(true);
    };
    this.ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'hello' || msg.type === 'bootstrap') this.onBootstrap(msg);
      else if (msg.type === 'snapshot') this.onSnapshot(msg);
    };
    this.ws.onclose = () => {
      this.setConn(false);
      this.wsFails++;
      if (this.wsFails >= 2) this.startPolling();
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = setTimeout(() => this.connectWS(), Math.min(8000, 1200 * this.wsFails));
    };
    this.ws.onerror = () => { try { this.ws.close(); } catch (e) {} };
  },

  setConn(ok) {
    const el = document.getElementById('dot-wire');
    if (el && this.snap) el.className = 'dot ' + (ok && this.snap.feeds.wire.connected ? 'on' : 'warn');
    const sb = document.getElementById('sb-mid');
    const where = this.base ? this.base.replace(/^https?:\/\//, '') : 'same-origin';
    if (sb) sb.textContent = ok ? `ENGINE ${where} · STREAM: WEBSOCKET`
                               : `ENGINE ${where} · STREAM: RECONNECTING`;
  },

  /* -------------------------------- polling ---------------------------- */
  startPolling() {
    if (this.pollTimer) return;
    const sb = document.getElementById('sb-mid');
    if (sb) sb.textContent = 'STREAM: HTTP POLL (WS UNAVAILABLE)';
    this.setConn(false);
    this.pollTimer = setInterval(() => this.pollOnce(), 1600);
  },
  stopPolling() {
    if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
  },
  async pollOnce() {
    try {
      const r = await fetch(this.api('/api/snapshot?heavy=true'), { cache: 'no-store' });
      if (!r.ok) return;
      const s = await r.json();
      if (!this.boot) {
        const b = await (await fetch(this.api('/api/bootstrap'), { cache: 'no-store' })).json();
        this.onBootstrap(b);
      }
      this.onSnapshot(s);
    } catch (e) { /* keep trying */ }
  },

  /* ------------------------------- handlers ---------------------------- */
  onBootstrap(msg) {
    if (this.boot) return;
    this.boot = msg;
    const cfg = msg.config || msg.snapshot.config;
    const sel = document.getElementById('sym-select');
    sel.innerHTML = cfg.symbols.map(s =>
      `<option value="${s}">${s.replace('-USDT-SWAP', ' PERP')}</option>`).join('');
    this.sym = cfg.focus;
    sel.value = cfg.focus;
    const candles = msg.candles[cfg.focus] || (msg.snapshot && msg.snapshot.candles);
    if (candles) this.engine.setCandles(candles);
    document.getElementById('chart-stamp').style.display = 'none';
    if (msg.snapshot) this.onSnapshot(msg.snapshot, true);
  },

  onSnapshot(s, force) {
    if (!s) return;
    const prev = this.snap;
    this.snap = s;
    const heavy = s.maps !== null && s.maps !== undefined;

    if (s.focus.symbol !== this.sym) {
      this.sym = s.focus.symbol;
      document.getElementById('sym-select').value = s.focus.symbol;
      this.reloadCandles();
    }

    // chart bar
    if (s.focus.bar) this.engine.updateBar(s.focus.bar);

    if (heavy || force) {
      this.engine.state = s;
      this.engine.scheduleDraw();
      this.drawSubCharts(s);
    }

    // panels (cheap: all guarded by change detection)
    P.top(s);
    P.banner(s);
    P.scanner(s.scanner || [], s.params.score_threshold);
    P.funding(s.funding_rows || []);
    P.sentiment(s.sentiment || {}, s.whales);
    P.news(s.news || {});
    P.models(s.tick.divine || {});
    P.shields(s.tick.shields || {});
    P.risk(s);
    P.positions(s.positions || []);
    P.perf(s.stats || {}, s.recent_trades || []);
    P.journal(s.journal || []);
    P.evolution(s.evolution || {}, s.bandit || [], s.ml || {}, s.tick.vol || {});
    P.corr(s.macro ? s.macro.correlation : []);
    if (heavy) {
      P.triggers(s.maps.liq_map, s.focus.price);
      P.chartMeta(s, s.maps);
    }
    const sb = document.getElementById('sb-right');
    if (sb) {
      const errs = Object.entries(s.feeds.errors || {}).filter(([k]) => k !== 'ff');
      sb.textContent = `CYCLE ${s.cycle} · ${s.feeds.wire.msgs} WS MSGS · ${s.feeds.wire.trades} TRADES · `
        + `${s.server.uptime_s}s UP · ${s.feeds.liqs_tracked} LIQS`
        + (errs.length ? ` · ${errs.length} FEED ERR` : '');
    }
  },

  drawSubCharts(s) {
    const m = s.maps || (this.snap && this.snap.maps);
    if (!m) return;
    try {
      SubCharts.cvd(document.getElementById('cvd-canvas'), m.footprint || {});
      SubCharts.footprint(document.getElementById('fp-canvas'), m.footprint || {});
      SubCharts.book(document.getElementById('book-canvas'), m.orderbook || {});
      SubCharts.equity(document.getElementById('equity-canvas'), (s.stats || {}).equity_curve || []);
    } catch (e) { console.warn('subchart', e); }
  },

  async reloadCandles() {
    try {
      const r = await fetch(this.api(`/api/candles?symbol=${encodeURIComponent(this.sym)}&limit=500`));
      const js = await r.json();
      if (js.candles && js.candles.length) this.engine.setCandles(js.candles);
    } catch (e) {}
  },

  async validate() {
    const btn = document.getElementById('btn-validate');
    const note = document.getElementById('vf-note');
    btn.disabled = true; btn.textContent = 'RUNNING…';
    note.textContent = 'replaying history bar-by-bar…';
    try {
      const js = await (await fetch(this.api('/api/backtest?decisions=220'), { cache: 'no-store' })).json();
      const e = js.engine || {}, b = js.baseline || {};
      const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
      set('vf-pf', e.pf !== undefined ? e.pf.toFixed(2) : '--');
      set('vf-wr', e.winrate !== undefined ? e.winrate.toFixed(1) + '%' : '--');
      set('vf-r', e.net_r !== undefined ? (e.net_r > 0 ? '+' : '') + e.net_r.toFixed(2) : '--');
      set('vf-bpf', b.pf !== undefined ? b.pf.toFixed(2) : '--');
      set('vf-bwr', b.winrate !== undefined ? b.winrate.toFixed(1) + '%' : '--');
      set('vf-n', `${e.trades} / ${b.trades}`);
      note.textContent = `${js.bars} bars · threshold ${js.threshold} · engine max DD `
        + `${e.max_dd_r !== undefined ? e.max_dd_r + 'R' : '--'} · avg ${e.avg_r !== undefined ? e.avg_r + 'R' : '--'}`;
    } catch (err) {
      note.textContent = 'validation failed: ' + err;
    } finally {
      btn.disabled = false; btn.textContent = 'RUN VALIDATION';
    }
  },

  /* -------------------------------- commands --------------------------- */
  async post(path, body) {
    const r = await fetch(this.api(path), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    let js = null;
    try { js = await r.json(); } catch (e) {}
    if (!r.ok) {
      let msg = (js && js.detail) ? js.detail : r.statusText;
      if (msg && msg.blocks) msg = msg.blocks.map(b => b.text).join(' | ');
      this.note(typeof msg === 'string' ? `REJECTED: ${msg}` : 'REJECTED');
      return null;
    }
    return js;
  },

  note(text, cls) {
    const el = document.getElementById('exec-note');
    if (!el) return;
    el.textContent = text;
    el.style.color = cls || '#7A7A7A';
    clearTimeout(this._noteT);
    this._noteT = setTimeout(() => {
      el.textContent = 'ORDERS GATED BY THE THREE SHIELDS';
      el.style.color = '#5A5A5A';
    }, 9000);
  },

  async openTrade(dir, override) {
    const s = this.snap;
    if (!s) return;
    const res = await this.post('/api/trade/open',
      { symbol: s.focus.symbol, direction: dir, override: !!override });
    if (res) {
      this.note(`FILLED ${dir} ${F.base(s.focus.symbol)} — PAPER ENGINE`
        + (override ? ' (SHIELD OVERRIDE, AUDITED)' : ''), '#00FF88');
      this._pendingOverride = null;
      return;
    }
    // blocked: offer the audited paper-only override
    const shielded = s.tick.shields && s.tick.shields.blocked;
    if (shielded && s.risk.mode === 'PAPER' && !override) {
      this._pendingOverride = dir;
      this.note('BLOCKED BY SHIELD — press LONG/SHORT again to OVERRIDE in PAPER (audited)', '#FFB000');
    }
  },

  bindControls() {
    const fire = (dir) => {
      const ov = this._pendingOverride === dir;
      this.openTrade(dir, ov);
    };
    document.getElementById('btn-long').onclick = () => fire('LONG');
    document.getElementById('btn-short').onclick = () => fire('SHORT');
    document.getElementById('btn-close').onclick = async () => {
      const r = await this.post('/api/trade/close', {});
      if (r) this.note(`CLOSED ${(r.closed || []).length} POSITION(S)`, '#FFB000');
    };
    document.getElementById('btn-validate').onclick = () => this.validate();

    document.getElementById('btn-evolve').onclick = async () => {
      const r = await this.post('/api/evolve', {});
      this.note(r && r.ok ? 'GENETIC EVOLUTION STARTED' : 'EVOLUTION ALREADY RUNNING', '#FFB000');
    };

    document.getElementById('in-capital').onchange = (e) =>
      this.post('/api/control', { capital: parseFloat(e.target.value) });
    document.getElementById('in-risk').onchange = (e) =>
      this.post('/api/control', { risk_pct: parseFloat(e.target.value) / 100 });
    document.getElementById('in-threshold').onchange = (e) =>
      this.post('/api/control', { threshold: parseFloat(e.target.value) });

    document.querySelectorAll('[data-mode]').forEach(b => b.onclick = () =>
      this.post('/api/control', { mode: b.dataset.mode }));
    document.querySelectorAll('[data-auto]').forEach(b => b.onclick = () =>
      this.post('/api/control', { auto_trade: b.dataset.auto === '1' }));

    document.getElementById('sym-select').onchange = async (e) => {
      const sym = e.target.value;
      const r = await this.post('/api/focus', { symbol: sym });
      if (r) { this.sym = sym; this.note(`FOCUS → ${sym} · rewiring tape`, '#00C8FF'); }
      setTimeout(() => this.reloadCandles(), 400);
    };

    document.querySelectorAll('.map-tog').forEach(btn => {
      btn.onclick = () => {
        const key = btn.dataset.map;
        const on = !btn.classList.contains('on');
        btn.classList.toggle('on', on);
        this.engine.toggle(key, on);
      };
    });

    document.addEventListener('keydown', (ev) => {
      if (ev.target.tagName === 'INPUT' || ev.target.tagName === 'SELECT') return;
      const k = ev.key.toLowerCase();
      if (k === 'l') this.openTrade('LONG');
      if (k === 's') this.openTrade('SHORT');
      if (k === 'c') document.getElementById('btn-close').click();
      if (k === 'e') document.getElementById('btn-evolve').click();
      const mapKeys = ['a', 'b', 'c', 'd', 'f', 'g'];
      const idx = mapKeys.indexOf(k);
      if (idx >= 0) {
        const btn = document.querySelector(`.map-tog[data-map="${k.toUpperCase()}"]`);
        if (btn) btn.click();
      }
    });

    // keyboard help
    document.getElementById('sb-left').textContent =
      'OMNISCIENCE v1.0 · [L]ONG [S]HORT [C]LOSE [E]VOLVE · [1-7] MAPS';
  },
};

window.addEventListener('DOMContentLoaded', () => App.boot());
