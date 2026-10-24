/* ==========================================================================
   Panels — every DOM binding.  All values are pushed from the server snapshot;
   nothing here invents numbers.
   ========================================================================== */
const P = {
  $: (id) => document.getElementById(id),
  _last: {},

  text(id, v) {
    const el = this.$(id);
    if (!el) return;
    if (this._last[id] === v) return;
    this._last[id] = v;
    el.textContent = v;
  },
  html(id, v) {
    const el = this.$(id);
    if (!el) return;
    if (this._last[id] === v) return;
    this._last[id] = v;
    el.innerHTML = v;
  },

  /* -------------------------------- top -------------------------------- */
  top(s) {
    const fx = s.focus;
    const bar = fx.bar;
    const price = fx.price;
    this.text('price', F.price(price));
    const first = fx.session_open || (bar ? bar.o : price);
    const chg = first ? ((price - first) / first) * 100 : 0;
    const chgEl = this.$('price-chg');
    if (chgEl) {
      chgEl.textContent = `${chg >= 0 ? '▲' : '▼'} ${Math.abs(chg).toFixed(2)}% 24H`;
      chgEl.className = 'chg ' + F.cls(chg);
    }
    const ob = (s.maps && s.maps.orderbook) || null;
    this.text('spread', ob ? `SPREAD ${ob.spread} · IMB ${ob.imbalance > 0 ? '+' : ''}${ob.imbalance}%` : 'SPREAD --');

    // divine ring
    const d = s.tick.divine || {};
    const score = d.score || 0;
    const ring = this.$('ring-fg');
    if (ring) {
      const circ = 2 * Math.PI * 27;
      ring.style.strokeDasharray = circ;
      ring.style.strokeDashoffset = circ * (1 - F.clamp(score, 0, 100) / 100);
      ring.style.stroke = F.scoreColor(score);
    }
    this.text('divine-score', Math.round(score));
    const badge = this.$('dir-badge');
    if (badge) {
      const dir = d.direction || 'NONE';
      badge.textContent = s.tick.shields && s.tick.shields.blocked ? 'VETOED' : dir;
      badge.className = 'dir-badge ' + (dir === 'LONG' ? 'long' : dir === 'SHORT' ? 'short' : '');
    }
    this.text('threshold-line', `THRESHOLD ${s.params.score_threshold.toFixed(0)} · RAW ${(s.tick.shields ? s.tick.shields.raw_score : 0).toFixed(1)}`);

    // killzone
    const kz = s.tick.killzone;
    const kzEl = this.$('kz-state');
    if (kzEl) {
      kzEl.textContent = kz.open ? `KILLZONE OPEN · ${kz.zone}` : 'MARKET DEAD';
      kzEl.className = 'kz-state' + (kz.open ? ' open' : '');
    }
    this.text('kz-window', kz.open ? `CLOSES IN ${Math.round(kz.mins_left)}m`
      : `OPENS ${kz.next || ''} IN ${Math.round((kz.next_in_min || 0) / 60 * 10) / 10}h`);
    this.text('kz-count', `UTC ${kz.utc}`);

    // status
    const wire = this.$('dot-wire');
    if (wire) wire.className = 'dot ' + (s.feeds.wire.connected ? 'on' : 'off');
    const ml = this.$('dot-ml');
    if (ml) ml.className = 'dot ' + (s.ml.ready ? 'on' : 'warn');
    this.text('latency', `${Math.round(s.tick.latency_ms.analysis_ms || 0)} ms`);
    this.text('clock', F.clock(s.ts) + ' UTC');
  },

  /* ------------------------------ banner ------------------------------- */
  banner(s) {
    const blocks = (s.tick.shields && s.tick.shields.blocks) || [];
    const el = this.$('banner');
    if (!blocks.length) { el.classList.add('hidden'); return; }
    el.classList.remove('hidden');
    const primary = blocks[0].text;
    this.text('banner-text', primary.toUpperCase());
    this.text('banner-sub', blocks.map(b => b.code).join(' · '));
  },

  /* ----------------------------- scanner ------------------------------- */
  scanner(rows, threshold) {
    const tb = this.$('scanner').querySelector('tbody');
    this.text('scan-count', String(rows.length));
    const html = rows.map(r => {
      const vetoed = r.blocks && r.blocks.length;
      const score = vetoed ? 0 : r.score;
      const col = F.scoreColor(r.score_raw);
      return `<tr>
        <td><b>${F.esc(r.base)}</b></td>
        <td class="${r.direction === 'LONG' ? 'c-green' : 'c-red'}">${r.direction === 'LONG' ? 'L' : 'S'}</td>
        <td style="color:${col};font-weight:700">${score.toFixed(1)}${vetoed ? '<span class="dim">*</span>' : ''}</td>
        <td>${r.ict.toFixed(0)}</td>
        <td class="${r.modelled ? '' : 'dim'}">${r.lstm.toFixed(0)}</td>
        <td>${r.intent.toFixed(0)}</td>
        <td class="${F.cls(r.chg_pct)}">${F.pct(r.chg_pct, 2)}</td>
      </tr>`;
    }).join('');
    if (this._last.scan !== html) { this._last.scan = html; tb.innerHTML = html; }
  },

  /* --------------------------- funding heat ---------------------------- */
  funding(rows) {
    const maxAbs = Math.max(...rows.map(r => Math.abs(r.rate)), 0.01);
    const html = rows.slice(0, 9).map(r => {
      const w = (Math.abs(r.rate) / maxAbs) * 50;
      const pos = r.rate >= 0;
      return `<div class="heat-row">
        <span>${F.esc(r.base)}</span>
        <div class="heat-bar"><div style="${pos ? 'left:50%' : `right:50%;left:auto`};width:${w}%;background:${pos ? 'rgba(0,255,136,.7)' : 'rgba(255,0,51,.7)'}"></div></div>
        <span class="${pos ? 'c-green' : 'c-red'}" style="text-align:right">${r.rate.toFixed(4)}%</span>
      </div>`;
    }).join('');
    this.html('funding-heat', html);
  },

  /* --------------------------- sentiment ------------------------------- */
  sentiment(sen, whales) {
    const fg = sen.fear_greed || {};
    this.text('fng', fg.value !== undefined ? `${fg.value} · ${fg.label}` : '--');
    const bar = this.$('fng-bar');
    if (bar && fg.value !== undefined) {
      bar.style.width = fg.value + '%';
      bar.style.background = fg.value < 25 ? '#FF0033' : fg.value < 45 ? '#FFB000'
        : fg.value < 60 ? '#8A8A8A' : '#00FF88';
    }
    const st = sen.stablecoin || {};
    const el24 = this.$('stbl-24');
    if (el24) {
      el24.textContent = st.inflow_24h !== undefined ? F.usd(st.inflow_24h) : '--';
      el24.className = F.cls(st.inflow_24h);
    }
    const el7 = this.$('stbl-7');
    if (el7) { el7.textContent = st.inflow_7d !== undefined ? F.usd(st.inflow_7d) : '--'; el7.className = F.cls(st.inflow_7d); }
    const dom = sen.dominance || {};
    this.text('dom', dom.usdt !== undefined ? `${(dom.usdt + dom.usdc).toFixed(2)}%` : '--');
    const dchg = this.$('dom-chg');
    if (dchg) { dchg.textContent = F.pct(sen.dominance_chg_1h, 3); dchg.className = F.cls(sen.dominance_chg_1h); }
    const cb = this.$('cb-prem');
    if (cb) {
      cb.textContent = sen.coinbase_premium !== null && sen.coinbase_premium !== undefined
        ? `${sen.coinbase_premium > 0 ? '+' : ''}${sen.coinbase_premium.toFixed(4)}%` : '--';
      cb.className = F.cls(sen.coinbase_premium);
    }
    const w = this.$('whale');
    if (w && whales) {
      w.textContent = `${F.usd(whales.notional.net)} (${whales.notional.count})`;
      w.className = F.cls(whales.notional.net);
    }
  },

  /* ------------------------------ news --------------------------------- */
  news(n) {
    const items = (n.rss || []).map(x => `<div class="feed-item${/ETF|SEC|FED|CRASH|HACK|liquidat/i.test(x.title) ? ' hot' : ''}">
      <div class="t">${F.hhmm(x.ts)} · ${F.ago(x.ts)}</div>${F.esc(x.title)}</div>`).join('');
    this.html('news-feed', items || '<div class="dim">NO HEADLINES</div>');

    const cal = (n.calendar || []).map(e => {
      const mins = (e.date - Date.now()) / 60000;
      const hot = Math.abs(mins) <= 60;
      return `<div class="feed-item${hot ? ' hot' : ''}">
        <div class="t">${F.hhmm(e.date)} UTC · ${F.esc(e.country)} · ${mins > 0 ? 'in ' + Math.round(mins) + 'm' : Math.abs(Math.round(mins)) + 'm ago'}</div>
        ${F.esc(e.title)}</div>`;
    }).join('');
    this.html('calendar', cal || '<div class="dim">NO HIGH-IMPACT EVENTS IN WINDOW</div>');
    this.text('cal-src', (n.blackout && n.blackout.degraded) ? 'DEGRADED' : 'LIVE');
  },

  /* ----------------------------- models -------------------------------- */
  models(d) {
    const p = d.parts || {};
    const set = (barId, valId, v) => {
      const b = this.$(barId); if (b) b.style.width = F.clamp(v, 0, 100) + '%';
      const el = this.$(valId); if (el) el.textContent = Math.round(v);
    };
    set('bar-ict', 'v-ict', p.ict || 0);
    set('bar-lstm', 'v-lstm', p.lstm || 0);
    set('bar-intent', 'v-intent', p.intent || 0);
    const fc = d.lstm;
    const im = d.intent_metrics || {};
    let note = `ENSEMBLE ${(d.score || 0).toFixed(1)} / 100 · ${d.direction || '--'}`;
    if (fc) note += ` · LSTM path ${fc.direction} ${fc.cum_return_pct > 0 ? '+' : ''}${fc.cum_return_pct}% (conf ${(fc.confidence * 100).toFixed(0)}%)`;
    note += ` · INTENT P(real)=${((d.intent_prob || 0) * 100).toFixed(1)}% (base ${((im.base_rate || 0) * 100).toFixed(0)}%)`;
    this.text('ensemble-note', note);
  },

  /* ----------------------------- shields -------------------------------- */
  shields(sh) {
    // 1 killzone
    const kz = sh.killzone || {};
    const e1 = this.$('shield-1');
    e1.className = 'shield ' + (kz.open ? 'pass' : 'fail');
    e1.querySelector('.sh-body').textContent = kz.open
      ? `${kz.zone} open · closes in ${Math.round(kz.mins_left)}m`
      : `MARKET DEAD — GOD IS SLEEPING · next ${kz.next} in ${Math.round(kz.next_in_min)}m`;

    // 2 anti-manipulation
    const am = sh.anti_manipulation || {};
    const e2 = this.$('shield-2');
    e2.className = 'shield ' + (am.blocked ? 'fail' : 'pass');
    const c = am.checks || {};
    const bits = [];
    if (c.funding) bits.push(`funding ${c.funding.value}% ${c.funding.blocked ? '✕' : '✓'}`);
    if (c.oi_divergence) bits.push(`OI ${c.oi_divergence.oi_chg > 0 ? '+' : ''}${c.oi_divergence.oi_chg}% vs P ${c.oi_divergence.price_chg > 0 ? '+' : ''}${c.oi_divergence.price_chg}% ${c.oi_divergence.blocked ? '✕' : '✓'}`);
    if (c.stablecoin_flight) bits.push(`USDT.D ${c.stablecoin_flight.value}% ${c.stablecoin_flight.blocked ? '✕' : '✓'}`);
    e2.querySelector('.sh-body').textContent = am.blocked
      ? am.reasons.map(r => r.text).join(' | ')
      : bits.join(' · ') || 'CLEAR';

    // 3 news
    const nw = sh.news || {};
    const e3 = this.$('shield-3');
    e3.className = 'shield ' + (nw.blocked ? 'fail' : (nw.degraded ? 'warn' : 'pass'));
    const ev = (nw.events || [])[0];
    e3.querySelector('.sh-body').textContent = nw.blocked
      ? (nw.manual ? 'OPERATOR BLACKOUT ENGAGED' : `NEWS BLACKOUT ±${nw.window_min}m — ${ev ? ev.title : 'HIGH IMPACT'}`)
      : `no high-impact event within ±${nw.window_min}m${nw.degraded ? ' · calendar degraded' : ''}${(nw.upcoming || [])[0] ? ' · next ' + F.esc((nw.upcoming || [])[0].title) : ''}`;
  },

  /* ------------------------------- risk --------------------------------- */
  risk(s) {
    const r = s.risk;
    const setIn = (id, v) => {
      const el = this.$(id);
      if (el && document.activeElement !== el) el.value = v;
    };
    setIn('in-capital', Math.round(r.capital));
    setIn('in-risk', (r.risk_pct * 100).toFixed(2));
    setIn('in-threshold', s.params.score_threshold.toFixed(0));
    document.querySelectorAll('[data-mode]').forEach(b =>
      b.classList.toggle('on', b.dataset.mode === r.mode));
    document.querySelectorAll('[data-auto]').forEach(b =>
      b.classList.toggle('on', b.dataset.auto === (r.auto_trade ? '1' : '0')));

    const t = s.tick.ticket;
    if (t && t.sizing) {
      this.text('rk-qty', `${F.num(t.sizing.qty, 4)} ${F.base(t.symbol)}`);
      this.text('rk-notional', `${F.usd(t.sizing.notional)} · ${t.sizing.leverage}x`);
      this.text('rk-risk', `${F.usd(t.sizing.risk_usd)} (${t.sizing.risk_pct_actual}%)`);
      this.text('rk-levels', `${F.price(t.entry)} / ${F.price(t.sl)}`);
      this.text('rk-tps', `${F.price(t.tp.tp1)} / ${F.price(t.tp.tp2)} / ${F.price(t.tp.tp3)}`);
    } else {
      ['rk-qty', 'rk-notional', 'rk-risk', 'rk-levels', 'rk-tps'].forEach(id => this.text(id, '--'));
    }

    const p = s.prop || {};
    const box = this.$('prop-box');
    if (box) {
      box.innerHTML = `PROP GUARD ${p.enforced ? '<b class="c-amber">ON</b>' : '<b class="dim">OFF</b>'} ·
        day ${F.usd(p.day_pnl)} / ${p.day_trades}/${p.max_trades_day} trades<br>
        <div class="bar"><div style="width:${F.clamp(p.daily_loss_used_pct / p.daily_loss_limit_pct * 100, 0, 100)}%;background:${p.daily_loss_used_pct > 0 ? '#FF0033' : '#FFB000'}"></div></div>
        daily loss used ${p.daily_loss_used_pct}% of ${p.daily_loss_limit_pct}%<br>
        <div class="bar"><div style="width:${F.clamp(p.total_loss_used_pct / p.total_loss_limit_pct * 100, 0, 100)}%;background:${p.total_loss_used_pct > 0 ? '#FF0033' : '#FFB000'}"></div></div>
        total loss used ${p.total_loss_used_pct}% of ${p.total_loss_limit_pct}%
        ${(p.breached || []).length ? `<br><b class="c-red">${p.breached.join('<br>')}</b>` : ''}`;
    }
  },

  /* ---------------------------- positions ------------------------------- */
  positions(rows) {
    this.text('pos-count', String(rows.length));
    if (!rows.length) { this.html('positions', '<div class="dim">FLAT — NO OPEN RISK</div>'); return; }
    const html = rows.map(p => {
      const legs = (p.legs || []).map(l => l.name).join(',') || 'NONE';
      return `<div class="feed-item">
        <div class="t">#${p.id} · ${F.base(p.symbol)} ${p.side} · <span class="${p.side === 'LONG' ? 'c-green' : 'c-red'}">${p.side}</span></div>
        E ${F.price(p.entry)} · SL ${F.price(p.sl)} ${p.sl_to_be ? '<b class="c-green">BE</b>' : ''}<br>
        legs ${legs} · open ${p.remaining.toFixed(0)}% · risk ${F.usd(p.risk_usd)}</div>`;
    }).join('');
    this.html('positions', html);
  },

  /* ------------------------------ perf ---------------------------------- */
  perf(stats, trades) {
    this.text('pf', stats.profit_factor.toFixed(2));
    this.text('wr', `${stats.winrate.toFixed(1)}%`);
    const net = this.$('net');
    if (net) { net.textContent = F.usd(stats.net_pnl); net.className = F.cls(stats.net_pnl); }
    const dd = this.$('dd');
    if (dd) { dd.textContent = F.usd(stats.max_dd); dd.className = stats.max_dd < 0 ? 'c-red' : 'dim'; }
    this.text('avgr', stats.avg_r.toFixed(2));
    this.text('ntr', String(stats.trades));
    const rows = (trades || []).filter(t => t.status === 'CLOSED').slice(0, 12).map(t => `<tr>
      <td>${t.id}</td><td>${F.base(t.symbol)}</td>
      <td class="${t.side === 'LONG' ? 'c-green' : 'c-red'}">${t.side[0]}</td>
      <td class="${F.cls(t.r_multiple)}">${t.r_multiple > 0 ? '+' : ''}${t.r_multiple.toFixed(2)}</td>
      <td class="${F.cls(t.realised_pnl)}">${F.usd(t.realised_pnl)}</td>
      <td class="dim">${F.esc((t.close_note || '').slice(0, 14))}</td></tr>`).join('');
    const body = rows || '<tr><td colspan="6" class="dim">NO CLOSED TRADES</td></tr>';
    if (this._last.hist !== body) {
      this._last.hist = body;
      document.querySelector('#history tbody').innerHTML = body;
    }
  },

  /* ----------------------------- journal -------------------------------- */
  journal(items) {
    const html = (items || []).slice(0, 26).map(j => `<div class="j-item ${F.esc(j.kind)}">
      <div class="j-head">${F.hhmm(j.ts)} · ${F.esc(j.kind)}${j.trade_id ? ' #' + j.trade_id : ''}</div>
      ${F.esc(j.text)}</div>`).join('');
    this.html('journal', html || '<div class="dim">JOURNAL EMPTY</div>');
  },

  /* ---------------------------- evolution ------------------------------- */
  evolution(ev, bandit, ml, vol) {
    this.text('ev-gen', String(ev.generation || 0));
    this.text('ev-fit', ev.fitness ? ev.fitness.toFixed(3) : '--');
    const best = (bandit || []).slice().sort((a, b) => b.avg_reward - a.avg_reward)[0];
    this.text('ev-pol', best ? `${best.policy} ${best.avg_reward.toFixed(2)}` : '--');
    this.text('ev-vol', vol ? `${vol.regime} · ATR ${vol.atr_pct}% · ×${vol.sl_mult}` : '--');
    const deltas = ev.deltas || {};
    const html = Object.entries(deltas).map(([k, v]) =>
      `<div>${F.esc(k)} <b>${v[0]}</b> → <b class="c-amber">${v[1]}</b></div>`).join('');
    this.html('ev-deltas', html || '<div class="dim">NO MUTATION ADOPTED YET</div>');
    this.text('ml-models', `${ml.n_models || 0}/${12}`);
    this.text('ml-mse', ml.lstm_val_mse !== null && ml.lstm_val_mse !== undefined ? ml.lstm_val_mse.toFixed(4) : '--');
    this.text('ml-acc', ml.lstm_dir_acc !== null && ml.lstm_dir_acc !== undefined ? (ml.lstm_dir_acc * 100).toFixed(1) + '%' : '--');
    const im = ml.intent || {};
    this.text('ml-auc', im.auc !== undefined ? im.auc.toFixed(3) : '--');
    const nErr = Object.keys(ml.errors || {}).length;
    this.text('ml-err', String(nErr));
  },

  /* --------------------------- correlations ---------------------------- */
  corr(rows) {
    const html = (rows || []).map(r => {
      const c = r.corr;
      const col = c === null ? 'dim' : Math.abs(c) > 0.6 ? (c > 0 ? 'c-green' : 'c-red') : 'c-amber';
      const drift = (c === null) ? '' : (Math.sign(c) !== Math.sign(r.expected) ? ' ⚠' : '');
      return `<tr><td>${F.esc(r.pair)}</td>
        <td class="${col}">${c === null ? '--' : c.toFixed(2)}${drift}</td>
        <td class="dim">${r.expected}</td>
        <td>${r.price !== undefined && r.price !== null ? F.price(r.price) : '--'}</td>
        <td class="${F.cls(r.chg_pct)}">${F.pct(r.chg_pct, 2)}</td></tr>`;
    }).join('');
    const body = html || '<tr><td colspan="5" class="dim">ALIGNING MACRO SERIES…</td></tr>';
    if (this._last.corr !== body) {
      this._last.corr = body;
      document.querySelector('#corr tbody').innerHTML = body;
    }
  },

  /* ----------------------------- triggers ------------------------------ */
  triggers(lm, price) {
    if (!lm || !lm.triggers) return;
    const rows = ['50M', '100M', '200M'].map(k => {
      const t = lm.triggers[k] || {};
      const up = t.up ? F.price(t.up) : '--';
      const dn = t.down ? F.price(t.down) : '--';
      return `<div class="trig"><i>${k}</i><span class="c-green">▲ ${up}</span><span class="c-red">▼ ${dn}</span></div>`;
    }).join('');
    const extra = `<div class="trig" style="margin-top:3px"><i>±2%</i>
      <span class="c-green">${F.usd(lm.liq_within_2pct ? lm.liq_within_2pct.up_short_squeeze : 0)}</span>
      <span class="c-red">${F.usd(lm.liq_within_2pct ? lm.liq_within_2pct.down_long_cascade : 0)}</span></div>
      <div class="dim" style="font-size:8px;margin-top:3px">${lm.real_prints_used} live liquidation prints fused</div>`;
    this.html('liq-triggers', rows + extra);
  },

  chartMeta(s, m) {
    if (!m || !m.smc || !m.smc.ok) return this.text('chart-meta', 'STRUCTURE BUILDING');
    const st = m.smc.structure;
    this.text('chart-meta',
      `${st.bias} · ${st.zone} · ATR ${F.price(m.smc.atr)} · ${(m.smc.order_blocks || []).length} OB · ${(m.smc.fvgs || []).length} FVG`);
    const fp = m.footprint || {};
    this.text('fp-note', fp.ok ? `${(fp.candles || []).length} buckets · CVD ${F.usd(fp.cvd)} · Δ ${F.usd(fp.delta_last)}` : 'accumulating');
    this.text('cvd-num', fp.ok ? `${fp.divergence ? fp.divergence.state : '--'} · buy ${fp.buy_ratio}%` : '--');
  },
};
