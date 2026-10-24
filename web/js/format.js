/* shared formatting helpers */
const F = {
  price(v, ref) {
    if (v === null || v === undefined || !isFinite(v)) return '--';
    const a = Math.abs(ref || v);
    let d = 2;
    if (a < 0.01) d = 6; else if (a < 1) d = 5; else if (a < 20) d = 4;
    else if (a < 200) d = 3; else d = 2;
    return v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  },
  num(v, d = 2) {
    if (v === null || v === undefined || !isFinite(v)) return '--';
    return Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  },
  usd(v, d) {
    if (v === null || v === undefined || !isFinite(v)) return '--';
    const ax = Math.abs(v);
    if (d === undefined) d = ax >= 1000 ? 0 : 2;
    const s = v < 0 ? '-' : '';
    const a = Math.abs(v);
    if (a >= 1e9) return s + '$' + (a / 1e9).toFixed(2) + 'B';
    if (a >= 1e6) return s + '$' + (a / 1e6).toFixed(2) + 'M';
    if (a >= 1e3) return s + '$' + (a / 1e3).toFixed(1) + 'K';
    return s + '$' + a.toFixed(d);
  },
  pct(v, d = 2) {
    if (v === null || v === undefined || !isFinite(v)) return '--';
    return (v > 0 ? '+' : '') + Number(v).toFixed(d) + '%';
  },
  sign(v) { return v > 0 ? 'c-green' : v < 0 ? 'c-red' : 'dim'; },
  cls(v) { return v > 0 ? 'c-green' : v < 0 ? 'c-red' : 'dim'; },
  clock(ts) {
    const d = new Date(ts);
    return d.toISOString().slice(11, 19);
  },
  hhmm(ts) {
    const d = new Date(ts);
    return d.toISOString().slice(11, 16);
  },
  ago(ts) {
    const s = Math.max(0, (Date.now() - ts) / 1000);
    if (s < 60) return Math.floor(s) + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h';
    return Math.floor(s / 86400) + 'd';
  },
  esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  },
  clamp(x, a, b) { return Math.max(a, Math.min(b, x)); },
  scoreColor(v) {
    if (v >= 85) return '#00FF88';
    if (v >= 70) return '#FFB000';
    if (v >= 50) return '#8A8A8A';
    return '#FF0033';
  },
  base(sym) { return (sym || '').split('-')[0].replace('USDT', ''); },
};
