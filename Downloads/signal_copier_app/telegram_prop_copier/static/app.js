/* Signal Copier dashboard - vanilla JS, no build step. */
'use strict';

// ------------------------------------------------------------------ helpers
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtMoney = (v, cur = 'USD') => {
  if (v == null || isNaN(v)) return '—';
  try { return new Intl.NumberFormat('en-US', { style: 'currency', currency: cur || 'USD', maximumFractionDigits: 2 }).format(v); }
  catch { return v.toFixed(2); }
};
const signed = (v, cur) => v == null ? '—' : (v > 0.004 ? '+' : v < -0.004 ? '−' : '') + fmtMoney(Math.abs(v), cur);
const pnl = (v, cur) => v == null ? '<span class="muted">—</span>' :
  `<span class="${v > 0.004 ? 'up' : v < -0.004 ? 'down' : ''}">${v > 0.004 ? '▲ ' : v < -0.004 ? '▼ ' : ''}${signed(v, cur)}</span>`;
const pctTxt = (v, d = 2) => v == null || isNaN(v) ? '—' : `${v.toFixed(d)}%`;
const px = v => v == null ? '—' : (Math.abs(v) >= 100 ? v.toFixed(2) : Math.abs(v) >= 10 ? v.toFixed(3) : v.toFixed(5));
const timeStr = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
const dateTimeStr = ts => new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit' });
const ago = ts => { const s = Date.now() / 1000 - ts; return s < 60 ? 'just now' : s < 3600 ? `${Math.floor(s / 60)}m ago` : s < 86400 ? `${Math.floor(s / 3600)}h ago` : timeStr(ts); };
const store = { get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
                set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { } } };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body ? 'POST' : 'GET'),
    headers: opts.body ? { 'content-type': 'application/json' } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { }
  if (!res.ok) { const msg = data?.detail || `Request failed (${res.status})`; toast(msg, true); throw new Error(msg); }
  return data;
}

let toastTimer;
function toast(text, err = false) {
  const t = $('#toast');
  t.textContent = text; t.className = 'toast show' + (err ? ' err' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.className = 'toast', err ? 5000 : 2600);
}

function confirmModal({ title, body, ok = 'Confirm', danger = false }) {
  return new Promise(resolve => {
    const m = $('#modal');
    m.innerHTML = `<div class="modal" role="dialog" aria-modal="true"><h2>${esc(title)}</h2><p>${body}</p>
      <div class="row"><button class="btn" data-m="0">Cancel</button><button class="btn ${danger ? 'danger' : 'primary'}" data-m="1">${esc(ok)}</button></div></div>`;
    m.hidden = false;
    const done = v => { m.hidden = true; m.innerHTML = ''; resolve(v); };
    m.onclick = e => { if (e.target === m) done(false); const b = e.target.closest('[data-m]'); if (b) done(b.dataset.m === '1'); };
    $('[data-m="1"]', m).focus();
  });
}

function formModal(title, text, fields, ok) {
  return new Promise(resolve => {
    const m = $('#modal');
    m.innerHTML = `<form class="modal" role="dialog" aria-modal="true"><h2>${esc(title)}</h2>${text ? `<p style="font-size:12.5px">${esc(text)}</p>` : ''}
      <div class="form-grid" style="grid-template-columns:repeat(auto-fill,minmax(120px,1fr))">${fields.map(([k, l, type, v, opts]) => `<label class="f">${l}${type === 'select'
        ? `<select name="${k}">${opts.map(([ov, ol]) => `<option value="${ov}" ${ov === v ? 'selected' : ''}>${ol}</option>`).join('')}</select>`
        : `<input name="${k}" type="${type}" step="any" value="${esc(v)}">`}</label>`).join('')}</div>
      <div class="row"><button type="button" class="btn" data-m="0">Cancel</button><button type="submit" class="btn primary">${esc(ok)}</button></div></form>`;
    m.hidden = false;
    const form = $('form', m);
    const done = v => { m.hidden = true; m.innerHTML = ''; resolve(v); };
    m.onclick = e => { if (e.target === m || e.target.closest('[data-m="0"]')) done(null); };
    form.onsubmit = e => { e.preventDefault(); done(Object.fromEntries(new FormData(form))); };
    $('input,select', form)?.focus();
  });
}

// ------------------------------------------------------------------ state
const S = {
  settings: null, telegram: {}, accounts: {}, messages: [], events: [], trades: [],
  equity: {}, range: store.get('range', 24), tab: store.get('tab', 'overview'),
  editing: null, dialogs: null, dialogFilter: '', replyTo: null, wsOk: false,
  actAccount: '', actLevel: '', testChat: 0,
};
const PLATFORM = { mt5: 'MT5', tradelocker: 'TradeLocker', paper: 'Paper' };
const accountOrder = () => (S.settings?.accounts || []).map(a => a.name);
const colorIdx = name => accountOrder().indexOf(name);
const colorVar = name => { const i = colorIdx(name); return i >= 0 && i < 8 ? `var(--s${i + 1})` : 'var(--muted)'; };
const cssVar = v => getComputedStyle(document.documentElement).getPropertyValue(v.replace(/var\((.*)\)/, '$1')).trim();

// ------------------------------------------------------------------ boot + websocket
async function loadState() {
  const d = await api('/api/state');
  S.settings = d.settings; S.telegram = d.telegram; S.messages = d.messages; S.events = d.events; S.trades = d.trades;
  S.accounts = Object.fromEntries(d.accounts.map(a => [a.name, a]));
  S.mirrorGroups = d.mirror_groups || [];
  S.ai = d.ai;
  await loadEquity();
  render();
}
async function loadEquity() {
  S.equity = await api(`/api/equity?hours=${S.range}`);
}

let ws, wsRetry = 1000;
function connectWS() {
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onopen = () => { const was = S.wsOk; S.wsOk = true; wsRetry = 1000; if (!was) loadState().catch(() => { }); renderLive(); };
  ws.onclose = () => { S.wsOk = false; renderLive(); setTimeout(connectWS, wsRetry); wsRetry = Math.min(wsRetry * 2, 15000); };
  ws.onmessage = e => onEvent(JSON.parse(e.data));
}

function onEvent({ type, data }) {
  if (type === 'account') {
    S.accounts[data.name] = data;
    if (data.equity != null) {
      const s = (S.equity[data.name] ||= []);
      const now = Date.now() / 1000;
      if (!s.length || now - s[s.length - 1][0] > 10) s.push([now, data.equity]);
      else s[s.length - 1] = [now, data.equity];
    }
  } else if (type === 'account_removed') {
    delete S.accounts[data]; delete S.equity[data];
  } else if (type === 'message') {
    const i = S.messages.findIndex(m => m.id === data.id);
    if (i >= 0) S.messages[i] = data; else S.messages.unshift(data);
    S.messages.length = Math.min(S.messages.length, 300);
    if (data.status === 'done' && data.kind !== 'none') flashFeed(data.id);
  } else if (type === 'event') {
    S.events.unshift(data); S.events.length = Math.min(S.events.length, 500);
    if (data.level === 'error') toast(`${data.account ? data.account + ': ' : ''}${data.text}`, true);
    if (data.text.startsWith('opened') || data.text.includes('closed')) refreshTrades();
  } else if (type === 'telegram') {
    const changed = S.telegram.status !== data.status;
    S.telegram = data;
    if (changed && S.tab === 'telegram') renderTelegramConn();
  } else if (type === 'settings') {
    S.settings = data;
  }
  renderLive();
}
let tradesTimer;
function refreshTrades() {
  clearTimeout(tradesTimer);
  tradesTimer = setTimeout(async () => { const d = await api('/api/state'); S.trades = d.trades; if (S.tab === 'activity') renderActivityTables(); }, 800);
}
function flashFeed(id) { setTimeout(() => { const el = $(`[data-msg="${id}"]`); el?.animate([{ background: 'color-mix(in srgb, var(--accent) 15%, transparent)' }, { background: 'transparent' }], 1200); }, 30); }

// ------------------------------------------------------------------ rendering
let liveQueued = false;
function renderLive() {
  if (liveQueued) return;
  liveQueued = true;
  requestAnimationFrame(() => {
    liveQueued = false;
    renderTop(); renderBanner();
    if (S.tab === 'overview') { renderKpis(); renderAccCards(); renderPositions(); renderFeed('#feed'); renderChartThrottled(); }
    if (S.tab === 'telegram') { renderFeed('#feed2'); }
    if (S.tab === 'accounts') { renderAccountTableBody(); }
    if (S.tab === 'activity') renderActivityTables();
  });
}

function render() {
  $$('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === S.tab));
  const main = $('#main');
  ({ overview: renderOverview, accounts: renderAccounts, telegram: renderTelegram, activity: renderActivity, settings: renderSettings }[S.tab] || renderOverview)(main);
  renderTop(); renderBanner();
}

function setTab(tab) { S.tab = tab; store.set('tab', tab); render(); window.scrollTo(0, 0); }

// ---------- top bar / banner
function renderTop() {
  if (!S.settings) return;
  const tg = S.telegram.status;
  const tgDot = { connected: 'good', connecting: 'warning', code_required: 'warning', password_required: 'warning', login_required: 'warning', error: 'critical' }[tg] || '';
  const tgLabel = { connected: 'Telegram connected', connecting: 'Telegram connecting…', login_required: 'Telegram: log in', code_required: 'Telegram: enter code',
    password_required: 'Telegram: 2FA password', error: 'Telegram error', not_configured: 'Telegram: set up' }[tg] || 'Telegram';
  const live = !S.settings.dry_run, copying = S.settings.copying_enabled;
  $('#topStatus').innerHTML = `
    <button class="pill" data-act="tab" data-tab="telegram" title="${esc(S.telegram.error || '')}"><span class="dot ${tgDot}"></span>${tgLabel}</button>
    <button class="pill" data-act="toggle-copy" title="Pause or resume copying on all accounts">
      <span class="dot ${copying ? 'good pulse' : 'serious'}"></span>${copying ? 'Copying on' : 'Copying paused'}</button>
    <button class="pill ${live ? 'live' : 'dry'}" data-act="toggle-dry" title="Switch between dry run and live trading">
      <span class="dot ${live ? 'critical' : 'warning'}"></span>${live ? 'LIVE trading' : 'Dry run'}</button>
    <button class="btn danger sm" data-act="flatten-all" title="Close every copier position on every account">Close everything</button>`;
}

function renderBanner() {
  if (!S.settings) return;
  const parts = [];
  if (!S.wsOk) parts.push(`<div class="banner critical"><span>⚠</span><span class="grow">Lost connection to the copier app. Make sure the <b>python app.py</b> window is still running — reconnecting…</span></div>`);
  const halted = Object.values(S.accounts).filter(a => a.halted);
  for (const a of halted) parts.push(`<div class="banner critical"><span>⛔</span><span class="grow"><b>${esc(a.name)}</b> halted: ${esc(a.halted)}. No new trades until the next trading day.</span>
      <button class="btn sm" data-act="resume" data-name="${esc(a.name)}">Resume anyway</button></div>`);
  if (S.settings.dry_run) parts.push(`<div class="banner"><span>●</span><span class="grow"><b>Dry run.</b> Signals are read, sized and logged, but no orders are placed.</span>
      <button class="btn sm" data-act="toggle-dry">Go live…</button></div>`);
  const html = parts.join('');
  if ($('#banner').innerHTML !== html) $('#banner').innerHTML = html;
}

// ------------------------------------------------------------------ OVERVIEW
function renderOverview(main) {
  main.innerHTML = `
  <div class="overview">
    <div class="stack">
      <div class="kpis" id="kpis"></div>
      <section class="card">
        <div class="card-h"><h2>Equity change</h2><span class="muted" style="font-size:12px">% since start of range</span><span class="grow"></span>
          <div class="legend" id="legend"></div>
          <div class="seg" id="rangeSeg">${[[1, '1H'], [6, '6H'], [24, '24H'], [168, '7D'], [720, '30D']].map(([h, l]) =>
            `<button data-act="range" data-h="${h}" class="${S.range == h ? 'on' : ''}">${l}</button>`).join('')}</div>
        </div>
        <div class="card-b"><div class="chart" id="chart"></div></div>
      </section>
      <section>
        <div class="row" style="margin:4px 0 10px"><h2>Accounts</h2><span class="grow"></span><button class="btn sm" data-act="add-account">+ Add account</button></div>
        <div class="acc-grid" id="accGrid"></div>
      </section>
      <section class="card">
        <div class="card-h"><h2>Open positions</h2><span class="muted" id="posCount"></span></div>
        <div class="table-wrap" id="posTable"></div>
      </section>
    </div>
    <aside class="card">
      <div class="card-h"><h2>Live feed</h2><span class="muted" style="font-size:12px">Telegram + mirrored trades</span><span class="grow"></span><button class="btn sm" data-act="tab" data-tab="telegram">Test console</button></div>
      <div class="feed" id="feed"></div>
    </aside>
  </div>`;
  renderKpis(); renderAccCards(); renderPositions(); renderFeed('#feed'); renderChart();
}

function renderKpis() {
  const el = $('#kpis'); if (!el) return;
  const accs = Object.values(S.accounts);
  const live = accs.filter(a => a.equity != null);
  const sum = k => live.reduce((t, a) => t + (a[k] || 0), 0);
  const cur = live[0]?.currency || 'USD';
  const startEq = live.reduce((t, a) => t + (a.day_start_equity || a.equity), 0);
  const today = sum('daily_pnl');
  const npos = accs.reduce((t, a) => t + a.positions.length, 0);
  const sig = accs.reduce((t, a) => t + (a.signals_today || 0), 0);
  const msgsToday = S.messages.filter(m => m.kind === 'signal' && Date.now() / 1000 - m.ts < 86400).length;
  const conn = accs.filter(a => a.status === 'connected').length;
  el.innerHTML = [
    ['Total equity', fmtMoney(sum('equity'), cur), `${live.length} account${live.length === 1 ? '' : 's'} reporting`],
    ["Today's P&L", pnl(today, cur), startEq ? `${today >= 0 ? '+' : '−'}${pctTxt(Math.abs(today / startEq * 100))} of day-start equity` : ''],
    ['Open P&L', pnl(sum('open_pnl'), cur), `${npos} open position${npos === 1 ? '' : 's'}`],
    ['Signals copied today', sig, `${msgsToday} signal${msgsToday === 1 ? '' : 's'} received (24h)`],
    ['Accounts connected', `${conn} / ${accs.length}`, accs.some(a => a.status === 'error') ? '<span class="down">● some need attention</span>' : 'all healthy'],
  ].map(([l, v, s]) => `<div class="card kpi"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${s}</div></div>`).join('');
}

function statusInfo(a) {
  if (!a.enabled) return ['', 'Disabled'];
  if (a.halted) return ['critical', 'Halted'];
  return { connected: ['good', 'Connected'], connecting: ['warning', 'Connecting…'], error: ['critical', 'Error'], disconnected: ['', 'Disconnected'], disabled: ['', 'Disabled'] }[a.status] || ['', a.status];
}

function meter(label, used, limit) {
  if (limit == null || used == null) return '';
  const r = Math.min(1, used / limit);
  const cls = r >= .8 ? 'critical' : r >= .5 ? 'warning' : '';
  return `<div class="meter"><div class="meter-row"><span>${label}</span><span class="num">${pctTxt(used)} of ${pctTxt(limit, 1)}</span></div>
    <div class="meter-track" role="meter" aria-valuenow="${used.toFixed(2)}" aria-valuemax="${limit}"><div class="meter-fill ${cls}" style="width:${(r * 100).toFixed(1)}%"></div></div></div>`;
}

function renderAccCards() {
  const el = $('#accGrid'); if (!el) return;
  const names = accountOrder().filter(n => S.accounts[n]);
  if (!names.length) {
    el.innerHTML = `<div class="card empty" style="grid-column:1/-1">No accounts yet. <button class="btn primary sm" data-act="add-account">Add your first account</button><br>
      <span style="font-size:12px">Tip: add a <b>Paper</b> account to try everything without risking a prop account.</span></div>`;
    return;
  }
  el.innerHTML = names.map(n => {
    const a = S.accounts[n];
    const [dot, label] = statusInfo(a);
    const cur = a.currency || 'USD';
    return `<div class="card acc">
      <div class="acc-top"><span class="swatch" style="background:${colorVar(n)}"></span><span class="acc-name" title="${esc(n)}">${esc(n)}</span>
        <span class="badge">${PLATFORM[a.platform] || a.platform}</span></div>
      <div class="row" style="font-size:12.5px;gap:12px"><span class="row" style="gap:6px"><span class="dot ${dot}"></span>${label}</span>
        <span class="grow"></span>
        <label class="switch" title="Copy Telegram signals to this account"><input type="checkbox" data-act="acc-copy" data-name="${esc(n)}" ${a.copy_enabled ? 'checked' : ''}><span class="track"></span>Telegram</label></div>
      ${a.mirror_from ? `<div class="row mirror-line"><span>⇠ Mirrors <b>${esc(a.mirror_from)}</b> <span class="muted">· ${esc(SIZING[a.mirror_sizing || 'equity_ratio'][0])}</span></span><span class="grow"></span>
        <label class="switch" title="Copy trades from ${esc(a.mirror_from)}"><input type="checkbox" data-act="acc-mirror" data-name="${esc(n)}" ${a.mirror_enabled !== false ? 'checked' : ''}><span class="track"></span>Mirror</label></div>` : ''}
      ${a.followers?.length ? `<div class="row mirror-line"><span>⇢ Source for <b>${a.followers.map(esc).join(', ')}</b></span></div>` : ''}
      ${a.error && a.status === 'error' ? `<div class="err">${esc(a.error)}</div>` : ''}
      ${a.halted ? `<div class="halt">⛔ ${esc(a.halted)}</div>` : ''}
      <div class="acc-eq"><span class="big num">${fmtMoney(a.equity, cur)}</span><span class="num">${pnl(a.daily_pnl, cur)} <span class="muted">today</span></span></div>
      <div class="acc-stats">
        <div><span class="muted">Balance</span><b class="num">${fmtMoney(a.balance, cur)}</b></div>
        <div><span class="muted">Open P&L</span><b class="num">${pnl(a.open_pnl, cur)}</b></div>
        <div><span class="muted">Total P&L</span><b class="num">${pnl(a.total_pnl, cur)}</b></div>
        <div><span class="muted">Positions</span><b class="num">${a.positions.length}</b></div>
        <div><span class="muted">Signals today</span><b class="num">${a.signals_today}</b></div>
        <div><span class="muted">Risk / trade</span><b class="num">${a.fixed_lot ? a.fixed_lot + ' lot' : pctTxt(a.risk_pct * (a.risk_multiplier || 1), 2)}</b></div>
      </div>
      ${meter('Daily loss used', a.daily_loss_pct, a.daily_stop_pct)}
      ${meter('Max drawdown used', a.total_dd_pct, a.total_stop_pct)}
      <div class="acc-actions">
        ${a.status === 'connected'
          ? `<button class="btn sm danger" data-act="close-all" data-name="${esc(n)}" ${a.positions.length ? '' : 'disabled'}>Close all</button>
             <button class="btn sm" data-act="disconnect" data-name="${esc(n)}">Disconnect</button>`
          : `<button class="btn sm primary" data-act="connect" data-name="${esc(n)}" ${a.status === 'connecting' ? 'disabled' : ''}>Connect</button>`}
        <span class="grow"></span>
        ${a.platform === 'paper' && a.status === 'connected' ? `<button class="btn sm" data-act="paper-trade" data-name="${esc(n)}" title="Place a manual trade on this simulated account">Test trade</button>` : ''}
        <button class="btn sm" data-act="edit-account" data-name="${esc(n)}">Edit</button>
      </div>
    </div>`;
  }).join('');
}

function renderPositions() {
  const el = $('#posTable'); if (!el) return;
  const rows = [];
  for (const n of accountOrder()) for (const p of (S.accounts[n]?.positions || [])) rows.push({ ...p, account: n, cur: S.accounts[n].currency });
  $('#posCount').textContent = rows.length ? `${rows.length}` : '';
  if (!rows.length) { el.innerHTML = '<div class="empty">No open positions</div>'; return; }
  el.innerHTML = `<table><thead><tr><th>Account</th><th>Symbol</th><th>Side</th><th class="r">Lots</th><th class="r">Open</th><th class="r">Current</th>
    <th class="r">SL</th><th class="r">TP</th><th class="r">P&L</th><th></th></tr></thead><tbody>${rows.map(p => `
    <tr><td><span class="row" style="gap:6px;flex-wrap:nowrap"><span class="swatch" style="background:${colorVar(p.account)}"></span>${esc(p.account)}</span></td>
      <td><b>${esc(p.symbol)}</b></td><td class="side-${p.side}">${p.side === 'buy' ? '▲ BUY' : '▼ SELL'}</td>
      <td class="r">${p.volume.toFixed(2)}</td><td class="r">${px(p.open_price)}</td><td class="r">${p.price ? px(p.price) : '—'}</td>
      <td class="r">${px(p.sl)}</td><td class="r">${px(p.tp)}</td><td class="r">${pnl(p.profit, p.cur)}</td>
      <td class="r">${S.accounts[p.account]?.platform === 'paper' ? `<button class="icon-btn" title="Edit SL/TP (paper)" data-act="paper-modify" data-name="${esc(p.account)}" data-ticket="${p.ticket}" data-sl="${p.sl ?? ''}" data-tp="${p.tp ?? ''}">✎</button>` : ''}<button class="icon-btn" title="Close this position" aria-label="Close position" data-act="close-pos" data-name="${esc(p.account)}" data-ticket="${p.ticket}">✕</button></td></tr>`).join('')}
    </tbody></table>`;
}

// ---------- feed
function outcomeHtml(o) {
  const ic = { ok: '✓', warn: '!', error: '✕', info: '·' }[o.level] || '·';
  return `<div class="outcome ${o.level}"><span class="ic">${ic}</span><span>${o.account ? `<b>${esc(o.account)}</b> ` : ''}${esc(o.text)}</span></div>`;
}
const CMD_LABEL = { close_all: 'CLOSE ALL', close_partial: 'CLOSE', breakeven: 'SL → BREAKEVEN', move_sl: 'MOVE SL', move_tp: 'MOVE TP', cancel: 'CANCEL PENDING', tp_hit: 'TP HIT → BE' };
function cmdText(c) {
  if (c.action === 'breakeven' && c.ref) return `SL → BE AT ${c.ref.replace('_', ' ').toUpperCase()}`;
  if (c.action === 'close_worst') return 'CLOSE WORST ENTRIES';
  if (c.action === 'close_partial') return `CLOSE ${Math.round((c.value || .5) * 100)}%`;
  if (c.action === 'move_sl') return `MOVE SL → ${c.ref ? c.ref.toUpperCase() : c.value}`;
  if (c.action === 'move_tp') return `MOVE TP → ${c.value}`;
  if (c.action === 'tp_hit') return `TP${c.value || 1} HIT → BE`;
  return CMD_LABEL[c.action] || c.action;
}
function aiSummary(ai) {
  if (!ai) return '';
  if (ai.signal) {
    const s = ai.signal;
    return `${ai.kind === 'add_entries' ? 'ADD ENTRIES' : 'NEW TRADE'}: ${s.side?.toUpperCase()} ${s.symbol || ''} @ ${s.entries.join(' / ') || 'market'} · SL ${s.sl ?? '—'}${s.tps.length ? ' · TP ' + s.tps.slice(0, 3).join(' / ') : ''}`;
  }
  if (ai.kind === 'info') return 'NO ACTION';
  return ai.actions.map(a => `${a.type.replace('_', ' ').toUpperCase()}${a.fraction ? ' ' + Math.round(a.fraction * 100) + '%' : ''}${a.level ? ' AT ' + a.level.replace('_', ' ').toUpperCase() : ''}${a.price ? ' → ' + a.price : ''}`).join('  +  ');
}
function parsedSummary(m) {
  const p = m.parsed; if (!p) return '';
  if (m.kind === 'mirror') return '';
  if (m.kind === 'ai') return p.ai ? `AI ${Math.round(p.ai.confidence * 100)}% · ${aiSummary(p.ai)}` : '';
  if (Array.isArray(p)) return p.map(cmdText).join('  +  ') + (p[0]?.symbol ? ` · ${p[0].symbol}` : '') + (m.reply_to ? '  (reply)' : '');
  if (m.kind === 'signal' && p.add_on) return `ADD ENTRIES ${p.side.toUpperCase()} @ ${(p.layers.length ? p.layers : [p.entry]).join(' / ')}${p.sl ? ' · SL → ' + p.sl : ''} (reply)`;
  if (m.kind === 'signal') return `${p.side.toUpperCase()} ${p.symbol}${p.order_type !== 'market' ? ' ' + p.order_type.toUpperCase() : ''} @ ${p.layers?.length ? p.layers.join(' / ') : (p.entry ?? 'market')} · SL ${p.sl ?? '—'} · TP ${p.tps.length ? p.tps.join(' / ') : '—'}`;
  return `${p.action.replace('_', ' ').toUpperCase()}${p.symbol ? ' ' + p.symbol : ''}${p.value != null ? ' ' + p.value : ''}${m.reply_to ? ' (reply)' : ''}`;
}
function renderFeed(sel) {
  const el = $(sel); if (!el) return;
  const list = S.messages.slice(0, 120);
  if (!list.length) {
    el.innerHTML = `<div class="empty">No messages yet.<br><span style="font-size:12px">${S.telegram.status === 'connected'
      ? 'Waiting for your monitored channels to post.' : 'Connect Telegram, or send a test signal from the Telegram tab.'}</span></div>`;
    return;
  }
  const scroll = el.scrollTop;
  el.innerHTML = list.map(m => `
    <div class="msg ${m.kind === 'none' ? 'ignored' : ''}" data-msg="${m.id}">
      <div class="msg-h"><span class="kind ${m.kind}">${m.kind === 'none' ? 'IGNORED' : m.kind === 'mirror' ? 'MIRROR' : m.kind === 'ai' ? 'AI' : m.kind.toUpperCase()}</span>
        <span class="chan" title="${esc(m.chat_title)}">${esc(m.chat_title || m.chat_id)}</span><span class="grow"></span>
        ${m.status === 'processing' ? '<span>processing…</span>' : m.status === 'thinking' ? '<span>AI reading…</span>' : ''}
        <span title="${dateTimeStr(m.ts)}">${timeStr(m.ts)}</span>
        ${m.kind === 'signal' ? `<button class="icon-btn" title="Reply to this signal from the test console" data-act="reply" data-chat="${m.chat_id}" data-msgid="${m.msg_id}">↩</button>` : ''}
      </div>
      <div class="msg-text">${esc(m.text)}</div>
      ${m.parsed && m.kind !== 'mirror' ? `<div class="parsed">${esc(parsedSummary(m))}</div>` : ''}
      ${(m.outcomes || []).map(outcomeHtml).join('')}
      ${m.status === 'review' ? `<div class="row review"><span class="grow">Needs your OK</span>
        <button class="btn sm" data-act="review" data-id="${m.id}" data-d="dismiss">Dismiss</button>
        <button class="btn sm primary" data-act="review" data-id="${m.id}" data-d="approve">Approve</button></div>` : ''}
    </div>`).join('');
  el.scrollTop = scroll;
}

// ---------- chart (equity % change, one line per account)
let chartTimer = 0;
function renderChartThrottled() { if (Date.now() - chartTimer > 3000) renderChart(); }
function renderChart() {
  chartTimer = Date.now();
  const el = $('#chart'); if (!el) return;
  const W = el.clientWidth || 600, H = el.clientHeight || 260, m = { l: 48, r: 14, t: 10, b: 26 };
  const since = Date.now() / 1000 - S.range * 3600;
  const series = accountOrder().map(n => {
    const pts = (S.equity[n] || []).filter(p => p[0] >= since);
    if (pts.length < 2) return null;
    const base = pts[0][1];
    return { name: n, color: cssVar(colorVar(n)) || '#888', pts: pts.map(([t, e]) => [t, (e / base - 1) * 100, e]) };
  }).filter(Boolean);
  $('#legend').innerHTML = series.length >= 2 ? series.map(s => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join('') : '';
  if (!series.length) {
    el.innerHTML = `<div class="empty" style="height:100%;display:grid;place-items:center">Equity history builds up while the app runs (a point every 30 seconds).</div>`;
    return;
  }
  let t0 = Infinity, t1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const s of series) for (const [t, y] of s.pts) { t0 = Math.min(t0, t); t1 = Math.max(t1, t); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
  y0 = Math.min(y0, 0); y1 = Math.max(y1, 0);
  const pad = Math.max((y1 - y0) * 0.12, 0.05); y0 -= pad; y1 += pad;
  const step = niceStep((y1 - y0) / 4);
  y0 = Math.floor(y0 / step) * step; y1 = Math.ceil(y1 / step) * step;
  const X = t => m.l + (t - t0) / Math.max(1, t1 - t0) * (W - m.l - m.r);
  const Y = y => m.t + (1 - (y - y0) / (y1 - y0)) * (H - m.t - m.b);
  const grid = cssVar('--grid'), axis = cssVar('--axis'), muted = cssVar('--muted');
  let g = '';
  for (let y = y0; y <= y1 + 1e-9; y += step) {
    const yy = Y(y).toFixed(1);
    g += `<line x1="${m.l}" x2="${W - m.r}" y1="${yy}" y2="${yy}" stroke="${Math.abs(y) < 1e-9 ? axis : grid}" stroke-width="1"/>
          <text x="${m.l - 8}" y="${+yy + 4}" text-anchor="end" font-size="11" fill="${muted}">${(y > 0 ? '+' : '') + y.toFixed(step < 0.1 ? 2 : step < 1 ? 1 : 0)}%</text>`;
  }
  const nx = Math.max(2, Math.min(6, Math.floor((W - m.l - m.r) / 110)));
  for (let i = 0; i <= nx; i++) {
    const t = t0 + (t1 - t0) * i / nx;
    const lbl = S.range > 24 ? new Date(t * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' }) : timeStr(t);
    g += `<text x="${X(t).toFixed(1)}" y="${H - 6}" text-anchor="${i === 0 ? 'start' : i === nx ? 'end' : 'middle'}" font-size="11" fill="${muted}">${lbl}</text>`;
  }
  const lines = series.map(s => `<path d="${s.pts.map(([t, y], i) => `${i ? 'L' : 'M'}${X(t).toFixed(1)},${Y(y).toFixed(1)}`).join('')}"
      fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`).join('');
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Equity change by account">${g}${lines}
    <g id="hover" style="display:none"><line id="hx" y1="${m.t}" y2="${H - m.b}" stroke="${axis}" stroke-width="1"/>${series.map((s, i) =>
      `<circle id="hd${i}" r="4.5" fill="${s.color}" stroke="${cssVar('--surface')}" stroke-width="2"/>`).join('')}</g>
    <rect x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent" id="hit"/></svg>`;
  const svg = $('svg', el), hover = $('#hover', el), tip = $('#tooltip');
  $('#hit', el).addEventListener('mousemove', e => {
    const r = svg.getBoundingClientRect();
    const t = t0 + (e.clientX - r.left - m.l) / (W - m.l - m.r) * (t1 - t0);
    hover.style.display = '';
    const xx = X(t); $('#hx', el).setAttribute('x1', xx); $('#hx', el).setAttribute('x2', xx);
    let rows = '';
    series.forEach((s, i) => {
      let best = s.pts[0]; for (const p of s.pts) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
      const dot = $(`#hd${i}`, el); dot.setAttribute('cx', X(best[0])); dot.setAttribute('cy', Y(best[1]));
      const cur = S.accounts[s.name]?.currency || 'USD';
      rows += `<div class="tt-row"><span><i class="swatch" style="background:${s.color};width:8px;height:8px"></i>${esc(s.name)}</span>
        <span class="num"><b>${best[1] >= 0 ? '+' : ''}${best[1].toFixed(2)}%</b> <span class="muted">${fmtMoney(best[2], cur)}</span></span></div>`;
    });
    tip.innerHTML = `<div class="muted" style="margin-bottom:4px">${dateTimeStr(t)}</div>${rows}`;
    tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = `${Math.min(window.innerWidth - tw - 8, e.clientX + 14)}px`;
    tip.style.top = `${e.clientY + 14}px`;
  });
  $('#hit', el).addEventListener('mouseleave', () => { hover.style.display = 'none'; tip.hidden = true; });
}
function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw || 1))); const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}
window.addEventListener('resize', () => { if (S.tab === 'overview') renderChart(); });

// ------------------------------------------------------------------ ACCOUNTS
function renderAccounts(main) {
  main.innerHTML = `
  <div class="stack">
    <section class="card">
      <div class="card-h"><h2>Copy accounts</h2><span class="muted" style="font-size:12px">Every enabled account copies signals from its channels at its own risk.</span>
        <span class="grow"></span><button class="btn primary sm" data-act="add-account">+ Add account</button></div>
      <div class="table-wrap"><table><thead><tr><th>Account</th><th>Platform</th><th>Status</th><th class="r">Equity</th><th class="r">Today</th>
        <th class="r">Risk</th><th>Copies from</th><th>Telegram</th><th></th></tr></thead><tbody id="accBody"></tbody></table></div>
    </section>
    <section class="card" id="linksCard"></section>
    <div id="editor"></div>
  </div>`;
  renderLinks();
  renderAccountTableBody();
  renderEditor();
}

function renderLinks() {
  const el = $('#linksCard'); if (!el) return;
  const groups = {};
  for (const c of S.settings.accounts) if (c.mirror_from) (groups[c.mirror_from] ||= []).push(c);
  const keys = Object.keys(groups);
  el.innerHTML = `<div class="card-h"><h2>Account-to-account copying</h2><span class="muted" style="font-size:12px">Set it up with “Mirror another account” when editing a follower</span></div>
    <div class="card-b">${keys.length ? keys.map(src => {
      const s = S.accounts[src] || {};
      return `<div class="link-row"><div class="link-src"><span class="swatch" style="background:${colorVar(src)}"></span><b>${esc(src)}</b>
          <span class="badge">${PLATFORM[s.platform] || ''}</span><span class="dot ${statusInfo(s)[0]}" title="${statusInfo(s)[1]}"></span></div>
        <div class="link-arrow" aria-hidden="true">→</div>
        <div class="link-fols">${groups[src].map(f => `<div class="row" style="gap:8px"><span class="swatch" style="background:${colorVar(f.name)}"></span><b>${esc(f.name)}</b>
          <span class="badge">${PLATFORM[f.platform]}</span><span class="muted" style="font-size:12px">${esc(SIZING[f.mirror_sizing || 'equity_ratio'][0])}${f.mirror_value ? ' · ' + f.mirror_value : ''}${f.mirror_sl_tp === false ? ' · no SL/TP' : ''}</span>
          ${f.mirror_enabled === false ? '<span class="badge">paused</span>' : ''}</div>`).join('')}</div></div>`;
    }).join('') : '<div class="muted" style="font-size:12.5px">No links yet. Edit an account and pick a source under <b>Mirror another account</b> — e.g. copy your MT5 account into your TradeLocker accounts.</div>'}</div>`;
}

function channelNames(ids) {
  if (!ids?.length) return '<span class="muted">All monitored</span>';
  const m = Object.fromEntries((S.settings.channels || []).map(c => [String(c.id), c.title]));
  return ids.map(i => esc(m[String(i)] || i)).join(', ');
}

function renderAccountTableBody() {
  const el = $('#accBody'); if (!el) return;
  const names = accountOrder();
  if (!names.length) { el.innerHTML = `<tr><td colspan="9" class="empty">No accounts yet — add one to start copying.</td></tr>`; return; }
  el.innerHTML = names.map(n => {
    const a = S.accounts[n] || { name: n, positions: [] }; const cfg = S.settings.accounts.find(x => x.name === n);
    const [dot, label] = statusInfo({ ...a, enabled: cfg.enabled !== false });
    return `<tr><td><span class="row" style="gap:6px;flex-wrap:nowrap"><span class="swatch" style="background:${colorVar(n)}"></span><b>${esc(n)}</b></span></td>
      <td>${PLATFORM[cfg.platform]}</td><td><span class="row" style="gap:6px;flex-wrap:nowrap"><span class="dot ${dot}"></span>${label}</span></td>
      <td class="r">${fmtMoney(a.equity, a.currency)}</td><td class="r">${pnl(a.daily_pnl, a.currency)}</td>
      <td class="r">${a.fixed_lot ? a.fixed_lot + ' lot' : pctTxt((a.risk_pct || 0) * (a.risk_multiplier || 1))}</td>
      <td style="white-space:normal;max-width:240px">${cfg.copy_enabled === false ? '<span class="muted">Telegram off</span>' : channelNames(cfg.channels)}${cfg.mirror_from ? `<br><span class="muted">⇠ mirrors</span> ${esc(cfg.mirror_from)}` : ''}</td>
      <td><label class="switch"><input type="checkbox" data-act="acc-copy" data-name="${esc(n)}" ${cfg.copy_enabled !== false ? 'checked' : ''}><span class="track"></span></label></td>
      <td class="r"><button class="btn sm" data-act="edit-account" data-name="${esc(n)}">Edit</button>
        <button class="btn sm danger" data-act="delete-account" data-name="${esc(n)}">Remove</button></td></tr>`;
  }).join('');
}

const FIELD_META = {
  login: ['Login (account number)', 'number'], password: ['Password', 'password', 'Stored on this computer only'],
  server: ['Server', 'text'], terminal_path: ['MT5 terminal path', 'text', 'Each MT5 account needs its own terminal install, e.g. C:/MT5-FTMO/terminal64.exe'],
  symbol_suffix: ['Symbol suffix', 'text', 'e.g. .x or .raw if your broker adds one'], magic: ['Magic number', 'number'],
  max_slippage_points: ['Max slippage (points)', 'number'],
  environment: ['Environment', 'select', '', [['https://demo.tradelocker.com', 'Demo / challenge'], ['https://live.tradelocker.com', 'Live / funded']]],
  email: ['Email', 'text'], acc_num: ['Account # (if several)', 'number'], starting_balance: ['Starting balance', 'number'],
};
const SIZING = {
  equity_ratio: ['Scaled to account size', 'Multiplier', 'Lots × (this equity ÷ source equity) × multiplier. A 50K follower of a 100K source trades half.'],
  same: ['Same exposure', '', 'Same money per point as the source (adjusts for different contract sizes).'],
  multiplier: ['Lot multiplier', 'Multiplier', 'Source exposure × this number, e.g. 0.5 or 2.'],
  fixed: ['Fixed lots', 'Lots', 'Every copied trade uses this lot size.'],
  risk: ['% risk using source SL', 'Risk %', 'Sizes from the source trade’s stop loss. Falls back to account-size scaling if it has none.'],
};
const AI_PRESETS = {
  haiku: { label: 'Claude Haiku 5.5 — about $1/month', provider: 'anthropic', model: 'claude-haiku-5-5', base_url: '',
    note: 'Paid but tiny: about $0.0002 per message read. Get a key at console.anthropic.com. Most reliable of these options.' },
  gemini: { label: 'Gemini Flash-Lite — free tier', provider: 'openai', model: 'gemini-3.5-flash-lite', base_url: 'https://generativelanguage.googleapis.com/v1beta/openai/',
    note: 'Free key at aistudio.google.com. On the free tier Google may use your prompts to improve its products and daily limits apply.' },
  groq: { label: 'Groq — free tier', provider: 'openai', model: 'openai/gpt-oss-20b', base_url: 'https://api.groq.com/openai/v1',
    note: 'Free key at console.groq.com (about 1,000 requests/day on the free plan). Very fast.' },
  ollama: { label: 'Ollama on this PC — free, private', provider: 'openai', model: 'qwen2.5:7b', base_url: 'http://localhost:11434/v1',
    note: 'Install Ollama, run “ollama pull qwen2.5:7b”, leave the API key blank. Nothing leaves your computer; slower and less accurate than the cloud options.' },
};
const RISK_FIELDS = [
  ['initial_balance', 'Initial account size', 'Used for prop % limits'],
  ['risk_pct', 'Risk per signal (%)', ''], ['risk_multiplier', 'Risk multiplier', 'e.g. 0.5 to trade half size'],
  ['fixed_lot', 'Fixed lot (overrides %)', ''], ['daily_stop_pct', 'Daily loss stop (%)', 'Set below the firm limit'],
  ['total_stop_pct', 'Max drawdown stop (%)', ''], ['max_open_positions', 'Max open positions', ''],
  ['max_signals_per_day', 'Max signals per day', ''], ['stop_at_profit_target_pct', 'Stop at profit target (%)', 'Blank = keep trading'],
];

function newAccount() {
  return { name: '', platform: 'paper', enabled: true, copy_enabled: true, initial_balance: 100000, starting_balance: 100000, channels: [] };
}

function renderEditor() {
  const el = $('#editor'); if (!el) return;
  if (!S.editing) { el.innerHTML = ''; return; }
  const a = S.editing.data, isNew = !S.editing.original, d = S.settings.risk;
  const fields = S.settings.account_fields[a.platform] || [];
  const input = (k, meta) => {
    const [label, type, hint, opts] = meta; const v = a[k] ?? '';
    const ctl = type === 'select'
      ? `<select name="${k}">${opts.map(([val, l]) => `<option value="${esc(val)}" ${v === val ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`
      : `<input name="${k}" type="${type}" value="${esc(v)}" ${type === 'number' ? 'step="any" data-type="number"' : ''} autocomplete="off">`;
    return `<label class="f">${label}${ctl}${hint ? `<span class="hint">${esc(hint)}</span>` : ''}</label>`;
  };
  const mapText = obj => Object.entries(obj || {}).map(([k, v]) => `${k} = ${v}`).join('\n');
  const chans = S.settings.channels || [];
  el.innerHTML = `<section class="card">
    <div class="card-h"><h2>${isNew ? 'Add account' : 'Edit ' + esc(S.editing.original)}</h2><span class="grow"></span>
      <button class="icon-btn" data-act="cancel-edit" aria-label="Close editor">✕</button></div>
    <form class="card-b form" id="accForm" autocomplete="off">
      <div class="row"><span class="muted" style="font-size:12.5px">Platform</span>
        <div class="seg">${Object.entries(PLATFORM).map(([k, l]) => `<button type="button" data-act="platform" data-p="${k}" class="${a.platform === k ? 'on' : ''}">${l}</button>`).join('')}</div></div>
      <div class="form-grid">
        <label class="f">Account name<input name="name" type="text" value="${esc(a.name)}" placeholder="e.g. FTMO 100K #1" required></label>
        <label class="switch" style="align-self:end;padding-bottom:8px"><input type="checkbox" name="enabled" data-type="bool" ${a.enabled !== false ? 'checked' : ''}><span class="track"></span>Connected / enabled</label>
        <label class="switch" style="align-self:end;padding-bottom:8px"><input type="checkbox" name="copy_enabled" data-type="bool" ${a.copy_enabled !== false ? 'checked' : ''}><span class="track"></span>Copy Telegram signals</label>
      </div>
      <fieldset><legend>Connection</legend><div class="form-grid">${fields.map(k => input(k, FIELD_META[k] || [k, 'text'])).join('')}</div>
        ${a.platform === 'mt5' ? '<p class="muted" style="font-size:12px;margin:10px 0 0">Log into this account in its MT5 terminal once and turn on <b>Algo Trading</b>. Windows only.</p>' : ''}
        ${a.platform === 'paper' ? '<p class="muted" style="font-size:12px;margin:10px 0 0">Simulated account with moving prices — safe for testing signals and settings.</p>' : ''}
      </fieldset>
      <fieldset><legend>Prop rules & sizing</legend><div class="form-grid">
        ${RISK_FIELDS.map(([k, l, h]) => `<label class="f">${l}<input name="${k}" type="number" step="any" data-type="number" value="${a[k] ?? ''}"
            placeholder="${d[k] ?? (k === 'initial_balance' ? '' : 'off')}">${h ? `<span class="hint">${h}</span>` : '<span class="hint">Blank = global default</span>'}</label>`).join('')}
        <label class="f">If price ran past entry<select name="if_price_moved"><option value="">Global default (${d.if_price_moved})</option>
          ${['limit', 'skip', 'market'].map(o => `<option ${a.if_price_moved === o ? 'selected' : ''}>${o}</option>`).join('')}</select></label>
        <label class="f">Breakeven after TP1<select name="breakeven_after_tp1" data-type="tri"><option value="">Global default (${d.breakeven_after_tp1 ? 'yes' : 'no'})</option>
          <option value="true" ${a.breakeven_after_tp1 === true ? 'selected' : ''}>yes</option><option value="false" ${a.breakeven_after_tp1 === false ? 'selected' : ''}>no</option></select></label>
      </div></fieldset>
      <fieldset><legend>Mirror another account</legend>
        <p class="muted" style="font-size:12px;margin:0 0 10px">Repeat every trade from another connected account here: new trades, partial closes, SL/TP changes and closes. Works across MT5, TradeLocker and Paper in any direction.</p>
        <div class="form-grid">
          <label class="f">Copy trades from<select name="mirror_from" id="mirrorFrom"><option value="">— Don't mirror —</option>
            ${accountOrder().filter(x => x !== (S.editing.original || a.name)).map(x => `<option ${a.mirror_from === x ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select></label>
          <label class="f">Sizing<select name="mirror_sizing" id="mirrorSizing">${Object.entries(SIZING).map(([k, v]) => `<option value="${k}" ${(a.mirror_sizing || 'equity_ratio') === k ? 'selected' : ''}>${v[0]}</option>`).join('')}</select>
            <span class="hint" id="sizingHint">${SIZING[a.mirror_sizing || 'equity_ratio'][2]}</span></label>
          <label class="f" id="mirrorValueWrap" ${SIZING[a.mirror_sizing || 'equity_ratio'][1] ? '' : 'hidden'}><span id="mirrorValueLabel">${SIZING[a.mirror_sizing || 'equity_ratio'][1]}</span>
            <input name="mirror_value" type="number" step="any" data-type="number" value="${a.mirror_value ?? ''}" placeholder="1"></label>
          <label class="switch" style="align-self:end;padding-bottom:8px"><input type="checkbox" name="mirror_sl_tp" data-type="bool" ${a.mirror_sl_tp !== false ? 'checked' : ''}><span class="track"></span>Copy SL/TP and their changes</label>
          <label class="switch" style="align-self:end;padding-bottom:8px"><input type="checkbox" name="mirror_existing" data-type="bool" ${a.mirror_existing ? 'checked' : ''}><span class="track"></span>Also copy trades already open</label>
        </div>
      </fieldset>
      <fieldset><legend>Telegram channels this account copies</legend>
        ${chans.length ? `<div class="check-list">${chans.map(c => `<label class="check-item"><input type="checkbox" name="channels" value="${c.id}" ${(a.channels || []).map(String).includes(String(c.id)) ? 'checked' : ''}>
          <span class="t">${esc(c.title || c.id)}</span>${c.enabled ? '' : '<span class="badge">not monitored</span>'}</label>`).join('')}</div>
          <p class="muted" style="font-size:12px;margin:8px 0 0">Leave all unchecked to copy every monitored channel.</p>`
          : `<p class="muted" style="margin:0;font-size:12.5px">No channels picked yet — this account will copy every monitored channel. Pick channels in the Telegram tab.</p>`}
      </fieldset>
      <div class="cols-2">
        <label class="f">Symbol mapping<textarea name="symbol_map" data-type="map" placeholder="US30 = US30.cash&#10;NAS100 = US100.cash">${esc(mapText(a.symbol_map))}</textarea>
          <span class="hint">Signal symbol = name on this account. Check your Market Watch.</span></label>
        ${a.platform === 'tradelocker' ? `<label class="f">Contract sizes<textarea name="contract_sizes" data-type="map" placeholder="XAUUSD = 100&#10;US30 = 1">${esc(mapText(a.contract_sizes))}</textarea>
          <span class="hint">Units per 1.0 lot from your firm's contract specs. Used for lot sizing.</span></label>` : '<div></div>'}
      </div>
      <div class="row"><span class="grow"></span><button type="button" class="btn" data-act="cancel-edit">Cancel</button>
        <button type="submit" class="btn primary">${isNew ? 'Add & connect' : 'Save & reconnect'}</button></div>
    </form></section>`;
  $('#accForm').addEventListener('submit', saveAccount);
  $('#mirrorSizing').addEventListener('change', e => {
    const m = SIZING[e.target.value];
    $('#sizingHint').textContent = m[2]; $('#mirrorValueLabel').textContent = m[1]; $('#mirrorValueWrap').hidden = !m[1];
  });
  el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function collectForm(form, base = {}) {
  const out = { ...base };
  const chans = [];
  for (const elx of form.elements) {
    if (!elx.name) continue;
    const t = elx.dataset.type;
    if (elx.name === 'channels') { if (elx.checked) chans.push(Number(elx.value)); continue; }
    if (t === 'bool') out[elx.name] = elx.checked;
    else if (t === 'number') out[elx.name] = elx.value === '' ? null : Number(elx.value);
    else if (t === 'tri') out[elx.name] = elx.value === '' ? null : elx.value === 'true';
    else if (t === 'map') out[elx.name] = Object.fromEntries(elx.value.split('\n').map(l => l.split('=').map(s => s.trim())).filter(p => p[0] && p[1] != null && p[1] !== '')
        .map(([k, v]) => [k.toUpperCase(), isNaN(Number(v)) ? v : Number(v)]));
    else if (elx.type !== 'checkbox') out[elx.name] = elx.value;
  }
  if (form.querySelector('[name=channels]')) out.channels = chans;
  return out;
}

async function saveAccount(e) {
  e.preventDefault();
  const data = collectForm(e.target, S.editing.data);
  if (!data.name?.trim()) return toast('Give the account a name', true);
  data.name = data.name.trim();
  const btn = e.target.querySelector('[type=submit]'); btn.disabled = true; btn.textContent = 'Connecting…';
  try {
    await api('/api/accounts', { body: { account: data, original_name: S.editing.original } });
    toast(`Saved ${data.name}`);
    S.editing = null;
    await loadState();
  } catch { btn.disabled = false; btn.textContent = 'Save & reconnect'; }
}

// ------------------------------------------------------------------ TELEGRAM
function renderTelegram(main) {
  const chats = S.settings.channels || [];
  main.innerHTML = `
  <div class="cols-2">
    <div class="stack">
      <section class="card" id="tgConn"></section>
      <section class="card">
        <div class="card-h"><h2>Monitored channels</h2><span class="grow"></span>
          <button class="btn sm" data-act="load-dialogs" ${S.telegram.status === 'connected' ? '' : 'disabled'}>Browse my chats</button></div>
        <div class="card-b" id="chanBox"></div>
      </section>
    </div>
    <div class="stack">
      <section class="card">
        <div class="card-h"><h2>Test console</h2><span class="muted" style="font-size:12px">Send a message as if a channel posted it</span></div>
        <div class="card-b form">
          <div class="form-grid">
            <label class="f">Pretend it came from<select id="testChat"><option value="0">Test console (routes to all accounts)</option>
              ${chats.map(c => `<option value="${c.id}" ${String(S.testChat) === String(c.id) ? 'selected' : ''}>${esc(c.title || c.id)}</option>`).join('')}</select></label>
            <label class="f">Reply to message #<input id="testReply" type="number" value="${S.replyTo ?? ''}" placeholder="optional — use ↩ on a signal"></label>
          </div>
          <div class="chips">${[
            ['Gold buy', 'XAUUSD BUY NOW @ {xau}\nSL {xauSl}\nTP1 {xauTp1}\nTP2 {xauTp2}'],
            ['EURUSD sell limit', 'EURUSD SELL LIMIT 1.0890\nSL 1.0920\nTP 1.0840'],
            ['US30 sell', 'US30 sell 39250-39280 sl 39400 tp 39100 tp 39000'],
            ['Move SL to BE', 'Move SL to breakeven'], ['Close half', 'Close half now'], ['Close all', 'Close all trades'], ['TP1 hit', 'TP1 hit ✅'],
          ].map(([l, t]) => `<button class="chip" data-act="template" data-t="${esc(t)}">${l}</button>`).join('')}</div>
          <textarea id="testText" rows="5" placeholder="Paste a real message from your channel to see how it's read"></textarea>
          <div><div class="muted" style="font-size:12px;margin-bottom:4px">How the copier reads it</div><div class="preview" id="testPreview">—</div></div>
          <div class="row"><span class="muted" style="font-size:12px">${S.settings.dry_run ? 'Dry run is on — nothing will be placed.' : '<span class="down">LIVE — this will place real orders on connected accounts.</span>'}</span>
            <span class="grow"></span><button class="btn" data-act="ai-test" title="Show how the AI interpreter reads this (uses one AI call)">Ask AI</button>
            <button class="btn primary" data-act="send-test">Send</button></div>
        </div>
      </section>
      <section class="card"><div class="card-h"><h2>Message feed</h2></div><div class="feed" id="feed2" style="max-height:600px"></div></section>
    </div>
  </div>`;
  renderTelegramConn(); renderChannels(); renderFeed('#feed2');
  const ta = $('#testText'); let tmr;
  ta.addEventListener('input', () => { clearTimeout(tmr); tmr = setTimeout(previewParse, 250); });
  $('#testChat').addEventListener('change', e => S.testChat = e.target.value);
}

async function previewParse() {
  const text = $('#testText')?.value || '';
  if (!text.trim()) { $('#testPreview').textContent = '—'; return; }
  const r = await api('/api/parse?text=' + encodeURIComponent(text));
  $('#testPreview').textContent = r.kind === 'none' ? 'Not recognised — this message would be ignored.'
    : parsedSummary({ kind: r.kind, parsed: r.parsed, reply_to: $('#testReply').value });
}

function renderTelegramConn() {
  const el = $('#tgConn'); if (!el) return;
  const t = S.telegram, st = t.status, tg = S.settings.telegram;
  const order = ['not_configured', 'login_required', 'code_required', 'connected'];
  const stepIdx = st === 'password_required' ? 2 : Math.max(0, order.indexOf(st));
  const steps = ['API keys', 'Send code', 'Verify', 'Connected'].map((l, i) =>
    `<span class="${i === stepIdx ? 'on' : i < stepIdx ? 'done' : ''}">${i < stepIdx ? '✓ ' : ''}${l}</span>`).join('');
  let body = '';
  if (st === 'connected') {
    body = `<div class="row"><span class="dot good"></span><span class="grow">Connected as <b>${esc(t.me)}</b>. Listening to ${(S.settings.channels || []).filter(c => c.enabled).length} channel(s).</span>
      <button class="btn sm" data-act="tg-edit">API settings</button><button class="btn sm danger" data-act="tg-logout">Log out</button></div>`;
  } else if (st === 'code_required' || st === 'password_required') {
    const pw = st === 'password_required';
    body = `<form class="form" id="tgVerify"><label class="f">${pw ? 'Two-step verification password' : `Login code sent to your Telegram app (${esc(tg.phone)})`}
        <input name="v" type="${pw ? 'password' : 'text'}" inputmode="${pw ? 'text' : 'numeric'}" autocomplete="one-time-code" required autofocus></label>
      <div class="row"><span class="grow"></span><button class="btn primary" type="submit">${pw ? 'Unlock' : 'Verify code'}</button></div></form>`;
  } else if (st === 'login_required') {
    body = `<div class="row"><span class="grow">API keys saved. Telegram will send a login code to <b>${esc(tg.phone)}</b>.</span>
      <button class="btn sm" data-act="tg-edit">Edit</button><button class="btn primary" data-act="tg-send-code">Send login code</button></div>`;
  } else if (st === 'connecting') {
    body = `<div class="row"><span class="dot warning pulse"></span>Connecting to Telegram…</div>`;
  }
  if (!body || S._tgEdit) {
    body = `<form class="form" id="tgCfg">
      ${t.error ? `<div class="down" style="font-size:12.5px">${esc(t.error)}</div>` : ''}
      <p class="muted" style="margin:0;font-size:12.5px">Create keys at <b>my.telegram.org</b> → API development tools. The copier logs in as you, so it can read any channel you've joined.</p>
      <div class="form-grid">
        <label class="f">API ID<input name="api_id" type="text" inputmode="numeric" value="${esc(tg.api_id)}" required></label>
        <label class="f">API hash<input name="api_hash" type="password" value="${esc(tg.api_hash)}" required></label>
        <label class="f">Phone number<input name="phone" type="text" value="${esc(tg.phone)}" placeholder="+15085551234" required></label>
      </div>
      <div class="row"><span class="grow"></span>${S._tgEdit ? '<button type="button" class="btn" data-act="tg-cancel-edit">Cancel</button>' : ''}<button class="btn primary" type="submit">Save & connect</button></div></form>`;
  }
  el.innerHTML = `<div class="card-h"><h2>Telegram account</h2><span class="grow"></span><div class="steps">${steps}</div></div><div class="card-b">${body}</div>`;
  $('#tgCfg')?.addEventListener('submit', async e => {
    e.preventDefault(); const f = collectForm(e.target);
    await api('/api/telegram/config', { body: f }); S._tgEdit = false; S.settings.telegram = { ...tg, ...f, api_hash: '••••••••' };
  });
  $('#tgVerify')?.addEventListener('submit', async e => {
    e.preventDefault(); const v = e.target.v.value;
    await api(st === 'password_required' ? '/api/telegram/password' : '/api/telegram/code', { body: st === 'password_required' ? { password: v } : { code: v } });
  });
}

function renderChannels() {
  const el = $('#chanBox'); if (!el) return;
  const saved = S.settings.channels || [];
  if (S.dialogs) {
    const sel = new Set(saved.filter(c => c.enabled).map(c => String(c.id)));
    const f = S.dialogFilter.toLowerCase();
    const list = S.dialogs.filter(d => !f || d.title.toLowerCase().includes(f));
    el.innerHTML = `<div class="form"><input type="search" id="dlgFilter" placeholder="Search your chats" value="${esc(S.dialogFilter)}">
      <div class="check-list" id="dlgList">${list.map(d => `<label class="check-item"><input type="checkbox" value="${d.id}" data-title="${esc(d.title)}" ${sel.has(String(d.id)) ? 'checked' : ''}>
        <span class="t">${esc(d.title)}</span><span class="badge">${d.type}</span></label>`).join('') || '<div class="empty">No chats match</div>'}</div>
      <div class="row"><span class="grow"></span><button class="btn" data-act="cancel-dialogs">Cancel</button><button class="btn primary" data-act="save-dialogs">Save channels</button></div></div>`;
    $('#dlgFilter').addEventListener('input', e => { S.dialogFilter = e.target.value; const pos = e.target.selectionStart; keepChecks(); renderChannels(); const i = $('#dlgFilter'); i.focus(); i.setSelectionRange(pos, pos); });
    return;
  }
  el.innerHTML = saved.length
    ? `<div class="check-list">${saved.map(c => `<label class="check-item"><input type="checkbox" data-act="chan-toggle" data-id="${c.id}" ${c.enabled ? 'checked' : ''}>
        <span class="t">${esc(c.title || c.id)}</span><span class="muted" style="font-size:12px">${c.enabled ? 'monitoring' : 'paused'}</span></label>`).join('')}</div>`
    : `<div class="empty" style="padding:12px">No channels yet. ${S.telegram.status === 'connected' ? 'Click <b>Browse my chats</b> to pick your signal channels.' : 'Connect Telegram first, then pick your signal channels.'}</div>`;
}
let pendingChecks = null;
function keepChecks() {
  pendingChecks ||= new Map((S.settings.channels || []).filter(c => c.enabled).map(c => [String(c.id), c.title]));
  for (const cb of $$('#dlgList input')) { if (cb.checked) pendingChecks.set(cb.value, cb.dataset.title); else pendingChecks.delete(cb.value); }
  S.settings.channels = [...pendingChecks].map(([id, title]) => ({ id: Number(id), title, enabled: true }));
}

// ------------------------------------------------------------------ ACTIVITY
function renderActivity(main) {
  main.innerHTML = `<div class="stack">
    <section class="card"><div class="card-h"><h2>Event log</h2><span class="grow"></span>
      <select id="actAccount" style="width:auto"><option value="">All accounts</option>${accountOrder().map(n => `<option ${S.actAccount === n ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select>
      <select id="actLevel" style="width:auto"><option value="">All levels</option>${[['ok', 'Actions'], ['warn', 'Warnings'], ['error', 'Errors'], ['info', 'Info']].map(([v, l]) => `<option value="${v}" ${S.actLevel === v ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
      <div class="table-wrap" style="max-height:480px;overflow:auto"><table><thead><tr><th>Time</th><th>Level</th><th>Account</th><th>Event</th></tr></thead><tbody id="evBody"></tbody></table></div></section>
    <section class="card"><div class="card-h"><h2>Trade actions</h2></div>
      <div class="table-wrap" style="max-height:480px;overflow:auto"><table><thead><tr><th>Time</th><th>Account</th><th>Action</th><th>Symbol</th><th>Side</th><th class="r">Lots</th><th class="r">Ticket</th><th>Detail</th></tr></thead><tbody id="trBody"></tbody></table></div></section>
  </div>`;
  $('#actAccount').addEventListener('change', e => { S.actAccount = e.target.value; renderActivityTables(); });
  $('#actLevel').addEventListener('change', e => { S.actLevel = e.target.value; renderActivityTables(); });
  renderActivityTables();
}
function renderActivityTables() {
  const lv = { ok: ['✓', 'good', 'Action'], warn: ['!', 'warning', 'Warning'], error: ['✕', 'critical', 'Error'], info: ['·', '', 'Info'] };
  const ev = S.events.filter(e => (!S.actAccount || e.account === S.actAccount) && (!S.actLevel || e.level === S.actLevel)).slice(0, 300);
  const eb = $('#evBody');
  if (eb) eb.innerHTML = ev.length ? ev.map(e => { const [ic, c, l] = lv[e.level] || lv.info;
    return `<tr><td class="muted">${dateTimeStr(e.ts)}</td><td><span class="row" style="gap:6px;flex-wrap:nowrap"><span class="dot ${c}"></span>${ic} ${l}</span></td>
      <td>${esc(e.account || '—')}</td><td style="white-space:normal">${esc(e.text)}</td></tr>`; }).join('') : '<tr><td colspan="4" class="empty">Nothing yet</td></tr>';
  const tb = $('#trBody');
  const tr = S.trades.filter(t => !S.actAccount || t.account === S.actAccount);
  if (tb) tb.innerHTML = tr.length ? tr.map(t => `<tr><td class="muted">${dateTimeStr(t.ts)}</td><td>${esc(t.account)}</td><td>${esc(t.action.replace('_', ' '))}</td>
      <td><b>${esc(t.symbol)}</b></td><td class="side-${t.side}">${esc((t.side || '').toUpperCase())}</td><td class="r">${t.volume != null ? (+t.volume).toFixed(2) : '—'}</td>
      <td class="r">${t.ticket ?? ''}</td><td class="muted">${esc(t.detail || '')}</td></tr>`).join('') : '<tr><td colspan="8" class="empty">No trades yet</td></tr>';
}

// ------------------------------------------------------------------ SETTINGS
function renderSettings(main) {
  const s = S.settings, r = s.risk;
  const num = (k, l, h = '', v = r[k]) => `<label class="f">${l}<input name="${k}" type="number" step="any" data-type="number" value="${v ?? ''}">${h ? `<span class="hint">${h}</span>` : ''}</label>`;
  const sel = (k, l, opts, v = r[k], h = '') => `<label class="f">${l}<select name="${k}">${opts.map(([o, t]) => `<option value="${o}" ${String(v) === String(o) ? 'selected' : ''}>${t}</option>`).join('')}</select>${h ? `<span class="hint">${h}</span>` : ''}</label>`;
  const yn = [['true', 'Yes'], ['false', 'No']];
  const theme = store.get('theme', 'system');
  main.innerHTML = `<form class="stack" id="setForm">
    <section class="card"><div class="card-h"><h2>Default risk & prop rules</h2><span class="muted" style="font-size:12px">Every account uses these unless it overrides them</span></div>
      <div class="card-b form-grid">
        ${num('risk_pct', 'Risk per signal (%)', 'Split across TP legs')}
        ${sel('risk_base', 'Risk % measured on', [['initial', 'Initial balance'], ['balance', 'Current balance'], ['equity', 'Current equity']])}
        ${num('daily_stop_pct', 'Daily loss stop (%)', 'Keep below your firm’s limit (often 5%)')}
        ${sel('daily_basis', 'Daily loss measured on', [['initial', 'Initial balance'], ['day_start', 'Day-start equity']])}
        ${num('total_stop_pct', 'Max drawdown stop (%)', 'Often 10% at the firm')}
        ${sel('flatten_on_stop', 'Close trades when a stop hits', yn)}
        ${num('max_open_positions', 'Max open positions')}
        ${num('max_signals_per_day', 'Max signals per day')}
        ${sel('require_sl', 'Require a stop loss', yn, r.require_sl, 'Most prop firms require one')}
        ${sel('split_tps', 'One position per TP', yn)}
        ${num('max_tps', 'Max TP legs')}
        ${sel('breakeven_after_tp1', 'Move runners to BE after TP1', yn)}
        ${num('breakeven_offset', 'Breakeven offset (price)')}
        ${num('max_entry_slip_of_sl', 'Max entry slip (× SL distance)', '0.3 = 30% of the stop distance')}
        ${sel('if_price_moved', 'If price ran past entry', [['limit', 'Place limit at entry'], ['skip', 'Skip the signal'], ['market', 'Enter at market anyway']])}
        ${sel('zone_entry', 'Zone signals (4139//4129)', [['layers', 'Two layers: market + limit at 2nd price'], ['first', 'First price only']], r.zone_entry, 'Risk is spread across the layers')}
        ${sel('tp_hit_action', 'When the channel posts “TP hit”', [['breakeven', 'Move SL to breakeven'], ['trail', 'Breakeven at TP1, then SL to previous TP'], ['none', 'Do nothing']], r.tp_hit_action)}
        ${sel('add_on_entries', '“Add more” re-entries', [['half', 'Take them at half risk'], ['full', 'Take them at full risk'], ['off', 'Ignore (still apply SL updates)']], r.add_on_entries)}
      </div></section>
    <section class="card"><div class="card-h"><h2>Signals & timing</h2></div>
      <div class="card-b form-grid">
        ${num('max_signal_age_sec', 'Ignore signals older than (sec)', 'Protects against stale signals after a reconnect', s.max_signal_age_sec)}
        ${sel('unlinked_commands', 'Updates that aren’t replies', [['only_if_one_open', 'Apply only if exactly one trade could be meant'], ['symbol_or_last', 'Apply to that symbol, or the latest signal'], ['ignore', 'Ignore — only act on replies']], s.unlinked_commands, 'Replies/quotes always go to the exact signal they quote')}
        ${num('dr_hour', 'Daily reset hour (0–23)', 'When your firm resets the daily loss', s.day_reset.hour)}
        <label class="f">Reset time zone<input name="dr_tz" type="text" value="${esc(s.day_reset.timezone)}"><span class="hint">e.g. America/New_York</span></label>
        ${num('poll_sec', 'Refresh accounts every (sec)', '', s.poll_sec)}
        ${num('mirror_poll_sec', 'Check mirror sources every (sec)', '1 for MT5; 2+ for TradeLocker sources', s.mirror_poll_sec)}
        ${num('mirror_max_delay_sec', 'Don’t mirror trades older than (sec)', 'If a follower was offline, skip trades it missed by more than this', s.mirror_max_delay_sec)}
        <label class="f">Theme<select id="themeSel">${[['system', 'Match system'], ['light', 'Light'], ['dark', 'Dark']].map(([v, l]) => `<option value="${v}" ${theme === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
      </div>
      <div class="card-b" style="padding-top:0"><label class="f">Symbol aliases<textarea name="symbol_aliases" data-type="map" placeholder="GOLD = XAUUSD&#10;NAS = NAS100">${esc(Object.entries(s.symbol_aliases || {}).map(([k, v]) => `${k} = ${v}`).join('\n'))}</textarea>
        <span class="hint">Extra names your channels use for a symbol. Common ones (GOLD, US30, NAS100, DAX…) are built in.</span></label></div></section>
    <div class="row"><span class="grow"></span><button class="btn primary" type="submit">Save settings</button></div>
  </form>
  <section class="card" style="margin-top:16px"><div class="card-h"><h2>AI interpreter</h2>
    <span class="muted" style="font-size:12px">Reads messages the rules can’t — typos, layers, “add more”, odd wording</span><span class="grow"></span>
    <span class="muted" style="font-size:12px">${S.ai ? `${S.ai.calls_today} call${S.ai.calls_today === 1 ? '' : 's'} today` : ''}</span></div>
    <form class="card-b form" id="aiForm" autocomplete="off">
      <div><div class="muted" style="font-size:12px;margin-bottom:6px">Quick setup</div>
        <div class="chips" id="aiPresets">${Object.entries(AI_PRESETS).map(([k, p]) => `<button type="button" class="chip" data-act="ai-preset" data-p="${k}">${p.label}</button>`).join('')}</div>
        <p class="muted" style="font-size:12px;margin:8px 0 0" id="aiPresetNote"></p></div>
      <div class="form-grid">
        <label class="f">When to use it<select name="mode">${[['off', 'Off — rules only'], ['fallback', 'When the rules can’t read a message (recommended)'], ['always', 'Every actionable message']].map(([v, l]) => `<option value="${v}" ${s.ai.mode === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
        <label class="f">Provider<select name="provider"><option value="anthropic" ${s.ai.provider === 'anthropic' ? 'selected' : ''}>Anthropic (Claude)</option><option value="openai" ${s.ai.provider === 'openai' ? 'selected' : ''}>OpenAI-compatible (Gemini, Groq, Ollama…)</option></select></label>
        <label class="f">Model<input name="model" type="text" value="${esc(s.ai.model)}"></label>
        <label class="f">API key<input name="api_key" type="password" value="${esc(s.ai.api_key)}" placeholder="not needed for Ollama"></label>
        <label class="f">Base URL<input name="base_url" type="text" value="${esc(s.ai.base_url)}" placeholder="only for OpenAI-compatible"></label>
        <label class="f">Act automatically above<select name="min_confidence" data-type="number">${[0.7, 0.8, 0.9, 1.01].map(v => `<option value="${v}" ${+s.ai.min_confidence === v ? 'selected' : ''}>${v > 1 ? 'Never — always ask me' : Math.round(v * 100) + '% confidence'}</option>`).join('')}</select>
          <span class="hint">Below this, the feed asks you to Approve. New trades from the AI always ask.</span></label>
      </div>
      <div class="row"><input id="aiTestText" type="text" class="grow" style="flex:1;min-width:200px" placeholder="Try a message, e.g. Set Breakevn not at higher layer">
        <button type="button" class="btn" data-act="ai-test">Test</button><button type="submit" class="btn primary">Save AI settings</button></div>
      <div class="preview" id="aiTestOut">—</div>
    </form></section>`;
  $('#aiForm').addEventListener('submit', async e => {
    e.preventDefault(); const f = collectForm(e.target);
    await api('/api/ai', { body: { ...f, auto_manage: f.min_confidence <= 1 } }); toast('AI settings saved');
    const st = await api('/api/state'); S.settings = st.settings;
  });
  $('#themeSel').addEventListener('change', e => { store.set('theme', e.target.value); applyTheme(); renderChart(); });
  $('#setForm').addEventListener('submit', async e => {
    e.preventDefault();
    const f = collectForm(e.target);
    const bools = ['flatten_on_stop', 'require_sl', 'split_tps', 'breakeven_after_tp1'];
    const risk = {};
    for (const k of Object.keys(S.settings.risk)) if (k in f) risk[k] = bools.includes(k) ? f[k] === 'true' : f[k];
    await api('/api/settings', { body: {
      risk, max_signal_age_sec: f.max_signal_age_sec, unlinked_commands: f.unlinked_commands, poll_sec: f.poll_sec || 3,
      mirror_poll_sec: f.mirror_poll_sec || 1, mirror_max_delay_sec: f.mirror_max_delay_sec ?? 30,
      day_reset: { hour: f.dr_hour ?? 17, timezone: f.dr_tz || 'America/New_York' }, symbol_aliases: f.symbol_aliases } });
    toast('Settings saved');
  });
}

function applyTheme() {
  const t = store.get('theme', 'system');
  if (t === 'system') document.documentElement.removeAttribute('data-theme'); else document.documentElement.dataset.theme = t;
}

// ------------------------------------------------------------------ actions (event delegation)
document.addEventListener('click', async e => {
  const b = e.target.closest('[data-act]'); if (!b) return;
  const act = b.dataset.act, name = b.dataset.name;
  if (b.tagName === 'INPUT') return; // checkboxes handled on change
  try {
    switch (act) {
      case 'tab': setTab(b.dataset.tab); break;
      case 'range': S.range = +b.dataset.h; store.set('range', S.range); $$('#rangeSeg button').forEach(x => x.classList.toggle('on', x === b)); await loadEquity(); renderChart(); break;
      case 'toggle-copy': {
        const on = !S.settings.copying_enabled;
        await api('/api/settings', { body: { copying_enabled: on } }); S.settings.copying_enabled = on; toast(on ? 'Copying resumed' : 'Copying paused on all accounts'); renderLive(); break;
      }
      case 'toggle-dry': {
        const goLive = S.settings.dry_run;
        if (goLive && !await confirmModal({ title: 'Switch to live trading?', ok: 'Go live', danger: true,
          body: 'New signals will place <b>real orders</b> on every connected account with copying turned on. Make sure you’ve checked your prop firm’s rules on copy trading.' })) return;
        await api('/api/settings', { body: { dry_run: !goLive } }); S.settings.dry_run = !goLive; toast(goLive ? 'LIVE trading on' : 'Dry run on'); render(); break;
      }
      case 'flatten-all':
        if (!await confirmModal({ title: 'Close everything?', ok: 'Close all positions', danger: true, body: 'Closes every open copier position on every connected account at market.' })) return;
        await api('/api/flatten_all', { body: {} }); toast('Closing all positions'); break;
      case 'close-all':
        if (!await confirmModal({ title: `Close all on ${name}?`, ok: 'Close all', danger: true, body: 'Closes every open copier position on this account at market.' })) return;
        await api(`/api/accounts/${encodeURIComponent(name)}/close_all`, { body: {} }); break;
      case 'close-pos':
        if (!await confirmModal({ title: 'Close position?', ok: 'Close', danger: true, body: `Close ticket #${b.dataset.ticket} on ${esc(name)} at market.` })) return;
        await api(`/api/accounts/${encodeURIComponent(name)}/close`, { body: { ticket: +b.dataset.ticket } }); break;
      case 'paper-trade': {
        const f = await formModal(`Test trade on ${name}`, 'Simulated account only. Use it to try mirroring: followers should copy this trade within a second or two.', [
          ['symbol', 'Symbol', 'text', 'XAUUSD'], ['side', 'Side', 'select', 'buy', [['buy', 'Buy'], ['sell', 'Sell']]],
          ['volume', 'Lots', 'number', '1'], ['sl', 'Stop loss', 'number', ''], ['tp', 'Take profit', 'number', '']], 'Place trade');
        if (f) await api(`/api/accounts/${encodeURIComponent(name)}/paper_order`, { body: f });
        break;
      }
      case 'paper-modify': {
        const f = await formModal(`Edit SL/TP #${b.dataset.ticket}`, 'Simulated account. Followers mirroring this account will update too.', [
          ['sl', 'Stop loss', 'number', b.dataset.sl], ['tp', 'Take profit', 'number', b.dataset.tp]], 'Save');
        if (f) await api(`/api/accounts/${encodeURIComponent(name)}/paper_modify`, { body: { ...f, ticket: +b.dataset.ticket } });
        break;
      }
      case 'review':
        b.disabled = true;
        await api(`/api/messages/${b.dataset.id}/${b.dataset.d}`, { body: {} });
        toast(b.dataset.d === 'approve' ? 'Approved — sending to accounts' : 'Dismissed'); break;
      case 'ai-test': {
        const text = ($('#testText')?.value || $('#aiTestText')?.value || '').trim();
        if (!text) return toast('Type a message first', true);
        const out = $('#testPreview') && S.tab === 'telegram' ? $('#testPreview') : $('#aiTestOut');
        out.textContent = 'Asking the AI…';
        if ($('#aiForm') && S.tab === 'settings') {   // test what's on screen, not what was saved earlier
          const f = collectForm($('#aiForm')); await api('/api/ai', { body: { ...f, auto_manage: f.min_confidence <= 1 } });
        }
        try {
          const r = await api('/api/ai/test', { body: { text } });
          out.textContent = `AI (${Math.round(r.ai.confidence * 100)}% sure, ${r.ms} ms): ${r.summary || 'no action'}\nReason: ${r.ai.reason}`;
        } catch (err) { out.textContent = String(err.message || err); }
        break;
      }
      case 'ai-preset': {
        const pr = AI_PRESETS[b.dataset.p]; const f = $('#aiForm');
        f.provider.value = pr.provider; f.model.value = pr.model; f.base_url.value = pr.base_url;
        $$('#aiPresets .chip').forEach(x => x.classList.toggle('on', x === b));
        $('#aiPresetNote').innerHTML = pr.note; break;
      }
      case 'connect': await api(`/api/accounts/${encodeURIComponent(name)}/connect`, { body: {} }); break;
      case 'disconnect': await api(`/api/accounts/${encodeURIComponent(name)}/disconnect`, { body: {} }); break;
      case 'resume': await api(`/api/accounts/${encodeURIComponent(name)}/resume`, { body: {} }); break;
      case 'add-account': S.editing = { original: null, data: newAccount() }; if (S.tab !== 'accounts') setTab('accounts'); else renderEditor(); break;
      case 'edit-account': S.editing = { original: name, data: structuredClone(S.settings.accounts.find(a => a.name === name)) }; if (S.tab !== 'accounts') setTab('accounts'); else renderEditor(); break;
      case 'cancel-edit': S.editing = null; renderEditor(); break;
      case 'platform': S.editing.data = { ...collectForm($('#accForm'), S.editing.data), platform: b.dataset.p }; renderEditor(); break;
      case 'delete-account':
        if (!await confirmModal({ title: `Remove ${name}?`, ok: 'Remove', danger: true, body: 'Disconnects it and deletes its settings from this app. Open positions on the broker are not closed.' })) return;
        await api(`/api/accounts/${encodeURIComponent(name)}`, { method: 'DELETE' }); await loadState(); break;
      case 'tg-send-code': await api('/api/telegram/send_code', { body: {} }); toast('Code sent — check your Telegram app'); break;
      case 'tg-logout':
        if (!await confirmModal({ title: 'Log out of Telegram?', ok: 'Log out', body: 'The copier will stop reading channels until you log in again.' })) return;
        await api('/api/telegram/logout', { body: {} }); break;
      case 'tg-edit': S._tgEdit = true; renderTelegramConn(); break;
      case 'tg-cancel-edit': S._tgEdit = false; renderTelegramConn(); break;
      case 'load-dialogs': b.disabled = true; b.textContent = 'Loading…'; S.dialogs = await api('/api/telegram/dialogs'); pendingChecks = null; S.dialogFilter = ''; renderChannels(); b.disabled = false; b.textContent = 'Browse my chats'; break;
      case 'cancel-dialogs': S.dialogs = null; pendingChecks = null; await loadState(); break;
      case 'save-dialogs': {
        keepChecks();
        const prev = new Map((S.settings.channels || []).map(c => [String(c.id), c]));
        await api('/api/channels', { body: { channels: [...prev.values()] } });
        S.dialogs = null; pendingChecks = null; toast('Channels saved'); await loadState(); break;
      }
      case 'template': {
        const g = 4140; // paper-account gold trades around this level
        $('#testText').value = b.dataset.t.replace('{xau}', g).replace('{xauSl}', g - 10).replace('{xauTp1}', g + 5).replace('{xauTp2}', g + 15);
        previewParse(); break;
      }
      case 'reply':
        S.replyTo = +b.dataset.msgid; S.testChat = b.dataset.chat;
        if (S.tab !== 'telegram') setTab('telegram');
        $('#testReply').value = S.replyTo; $('#testChat').value = S.testChat; $('#testText').focus();
        toast(`Replying to message #${S.replyTo}`); break;
      case 'send-test': {
        const text = $('#testText').value.trim(); if (!text) return toast('Type a message first', true);
        const reply = $('#testReply').value;
        await api('/api/test_message', { body: { text, chat_id: +$('#testChat').value || 0, reply_to: reply ? +reply : null } });
        $('#testText').value = ''; $('#testReply').value = ''; S.replyTo = null; $('#testPreview').textContent = '—'; toast('Sent to the copier'); break;
      }
    }
  } catch (err) { console.warn(err); }
});

document.addEventListener('change', async e => {
  const t = e.target;
  if (t.dataset.act === 'acc-copy') {
    await api(`/api/accounts/${encodeURIComponent(t.dataset.name)}/copy`, { body: { enabled: t.checked } }).catch(() => t.checked = !t.checked);
    const a = S.settings.accounts.find(x => x.name === t.dataset.name); if (a) a.copy_enabled = t.checked;
    if (S.accounts[t.dataset.name]) S.accounts[t.dataset.name].copy_enabled = t.checked;
  }
  if (t.dataset.act === 'acc-mirror') {
    await api(`/api/accounts/${encodeURIComponent(t.dataset.name)}/mirror`, { body: { enabled: t.checked } }).catch(() => t.checked = !t.checked);
    const a = S.settings.accounts.find(x => x.name === t.dataset.name); if (a) a.mirror_enabled = t.checked;
  }
  if (t.dataset.act === 'chan-toggle') {
    const chans = S.settings.channels.map(c => String(c.id) === t.dataset.id ? { ...c, enabled: t.checked } : c);
    await api('/api/channels', { body: { channels: chans } }); S.settings.channels = chans; renderChannels();
  }
});

$('#tabs').addEventListener('click', e => { const b = e.target.closest('button[data-tab]'); if (b) setTab(b.dataset.tab); });
setInterval(() => { if (S.tab === 'overview') loadEquity().then(renderChart).catch(() => { }); }, 60000);

applyTheme();
loadState().then(connectWS).catch(() => { connectWS(); });
