
/* GLOBE ORACLE app.js — 検索/ウォッチ/ライブ価格/市場時間(埋め込み窓)/USD⇄JPY
   素のJS(ES2018)・iOS Safari動作。DST計算はサーバー埋め込み窓を読むだけ。 */
(function () {
  'use strict';
  var B = document.body;
  var TOTAL = parseInt((B && B.getAttribute('data-total')) || '0', 10) || 0;
  var WATCH_KEY = 'globe_watch', JPY_KEY = 'globe_jpy';
  var STOCKS = null, loading = false, loadTries = 0;
  var RATE = parseFloat((B && B.getAttribute('data-usdjpy')) || '0') || 0;
  var MKT = {
    open_ms: parseFloat((B && B.getAttribute('data-open-ms')) || '0') || 0,
    close_ms: parseFloat((B && B.getAttribute('data-close-ms')) || '0') || 0,
    market_open: (B && B.getAttribute('data-mopen')) === '1',
    next_open: (B && B.getAttribute('data-nopen')) || '',
    next_close: (B && B.getAttribute('data-nclose')) || ''
  };

  var q = document.getElementById('q');
  var results = document.getElementById('results');
  var hint = document.getElementById('hint');
  var searchSec = document.getElementById('search-sec');

  function jpyOn() { try { return localStorage.getItem(JPY_KEY) === '1'; } catch (e) { return false; } }
  function fmtMoney(usd) {
    var v = Number(usd);
    if (jpyOn() && RATE > 0) return '¥' + Math.round(v * RATE).toLocaleString();
    return '$' + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function norm(s) {
    s = (s == null ? '' : String(s));
    try { s = s.normalize('NFKC'); } catch (e) {}
    return s.toLowerCase();
  }

  /* ---- ウォッチリスト ---- */
  function getWatch() { try { return JSON.parse(localStorage.getItem(WATCH_KEY) || '[]') || []; } catch (e) { return []; } }
  function setWatch(a) { try { localStorage.setItem(WATCH_KEY, JSON.stringify(a)); } catch (e) {} }
  function inWatch(c) { return getWatch().indexOf(c) >= 0; }
  function toggleWatch(c) { var w = getWatch(), i = w.indexOf(c); if (i >= 0) w.splice(i, 1); else w.push(c); setWatch(w); }

  function badge(g) { var m = { BUY: ['買', 'buy'], SELL: ['売', 'sell'], HOLD: ['待', 'hold'] }; var x = m[g] || m.HOLD; return '<span class="badge ' + x[1] + '">' + x[0] + '</span>'; }
  function bar(sc) { var p = Math.max(-100, Math.min(100, sc)) / 100; if (p >= 0) return '<span class="bar"><span class="bar-pos" style="width:' + (p * 50) + '%"></span></span>'; return '<span class="bar"><span class="bar-neg" style="width:' + (Math.abs(p) * 50) + '%;margin-left:' + (50 - Math.abs(p) * 50) + '%"></span></span>'; }
  function starBtn(c) { var on = inWatch(c); return '<button class="star' + (on ? ' on' : '') + '" data-star="' + c + '">' + (on ? '★' : '☆') + '</button>'; }

  function card(s, mode) {
    var scls = s.sc >= 0 ? 'pos' : 'neg';
    var seg = s.m ? '<span class="seg">' + s.m + '</span>' : '';
    var levels = '';
    if (s.t && s.st) {
      levels = '<div class="levels"><span class="lv tgt">利確 ' + fmtMoney(s.t) + '</span>' +
        '<span class="lv stp">損切 ' + fmtMoney(s.st) + '</span>' +
        (s.rr ? '<span class="lv rr">RR ' + s.rr + '</span>' : '') + '</div>';
    }
    var an = (s.tp != null) ? '<div class="analyst ' + (s.tp >= 0 ? 'up' : 'dn') + '">プロ予想 ' + (s.tp >= 0 ? '+' : '') + s.tp + '%</div>' : '';
    var fair = (s.val) ? '<div class="fair ' + (s.val === '割安' ? 'up' : s.val === '割高' ? 'dn' : 'hold') + '">理論株価 <b>' + s.val + '</b>（' + (s.fg >= 0 ? '+' : '') + s.fg + '%）</div>' : '';
    var reasons = (s.r && s.r.length) ? '<div class="reasons">' + s.r.map(function (r) { return '<span class="chip">' + r + '</span>'; }).join('') + '</div>' : '';
    var rm = (mode === 'watch') ? '<button class="rm" data-rm="' + s.c + '">×</button>' : '';
    return '<div class="card"><div class="row1"><span class="rank">' + (s.rk || '-') + '</span>' +
      '<div class="title"><span class="code">' + s.c + '</span><span class="name">' + s.n + '</span>' + seg + '</div>' +
      badge(s.g) + starBtn(s.c) + rm + '</div>' +
      '<div class="row2"><span class="price" data-px="' + s.c + '" data-usd="' + s.p + '">' + fmtMoney(s.p) + '</span>' +
      '<span class="score ' + scls + '">' + (s.sc >= 0 ? '+' : '') + s.sc + '</span>' + bar(s.sc) + '</div>' +
      levels + an + fair + reasons +
      '<div class="reasons"><span class="chip">スコア順 ' + (s.rk || '-') + ' 位 / ' + TOTAL + ' 銘柄</span></div></div>';
  }
  function byCode(c) { if (!STOCKS) return null; var v = String(c).toLowerCase(); for (var i = 0; i < STOCKS.length; i++) { if (STOCKS[i].c.toLowerCase() === v) return STOCKS[i]; } return null; }

  function ensureStocks(cb) {
    if (STOCKS) { if (cb) cb(); return; }
    if (loading) return; loading = true;
    (function attempt() {
      fetch('stocks.json?t=' + Date.now())
        .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
        .then(function (j) { STOCKS = j.stocks || j; loading = false; loadTries = 0; if (cb) cb(); renderWatch(); if (q && q.value.trim()) run(); })
        .catch(function (e) {
          loadTries++; console.warn('[globe] stocks.json 読込失敗(' + loadTries + ')', e);
          if (loadTries < 3) setTimeout(attempt, 3000);
          else { loading = false; if (results && q && q.value.trim()) results.innerHTML = '<p class="empty">銘柄データの読込に失敗しました。通信を確認して再度お試しください。</p>'; }
        });
    })();
  }

  function ensureHitEl() { var el = document.getElementById('hitcount'); if (!el && results && results.parentNode) { el = document.createElement('p'); el.id = 'hitcount'; results.parentNode.insertBefore(el, results); } return el; }
  function run() {
    if (!q || !results) return;
    var raw = q.value.trim(), hitEl = ensureHitEl();
    if (!raw) { results.innerHTML = ''; if (hint) hint.style.display = ''; if (hitEl) hitEl.textContent = ''; return; }
    if (hint) hint.style.display = 'none';
    if (!STOCKS) { if (hitEl) hitEl.textContent = ''; results.innerHTML = '<p class="empty">銘柄データを読込中…</p>'; ensureStocks(); return; }
    var v = norm(raw), code = raw.toLowerCase();
    var m = STOCKS.filter(function (s) { return (s.k && s.k.indexOf(v) >= 0) || s.c.toLowerCase().indexOf(code) === 0; }).sort(function (a, b) { return b.sc - a.sc; });
    var shown = m.slice(0, 8);
    if (hitEl) hitEl.textContent = m.length ? (m.length + '件ヒット / 上位' + shown.length + '件') : '';
    results.innerHTML = shown.length ? shown.map(function (s) { return card(s, 'search'); }).join('') : '<p class="empty">該当なし。社名(apple)やティッカー(AAPL)で検索してください。</p>';
  }
  var deb = null; function runDebounced() { if (deb) clearTimeout(deb); deb = setTimeout(run, 150); }
  if (q) { q.addEventListener('focus', function () { ensureStocks(); }); q.addEventListener('input', function () { ensureStocks(); runDebounced(); }); }

  var watchSec = null, watchResults = null;
  function ensureWatchSec() {
    if (watchSec) return;
    watchSec = document.createElement('section'); watchSec.id = 'watch-sec'; watchSec.style.display = 'none';
    watchSec.innerHTML = '<h2 class="find"><span>ウォッチリスト</span><em>WATCHLIST</em></h2><div id="watch-results" class="cards"></div>';
    if (searchSec && searchSec.parentNode) searchSec.parentNode.insertBefore(watchSec, searchSec.nextSibling);
    watchResults = watchSec.querySelector('#watch-results');
  }
  function renderWatch() {
    ensureWatchSec(); var w = getWatch();
    if (!w.length) { watchSec.style.display = 'none'; if (watchResults) watchResults.innerHTML = ''; return; }
    if (!STOCKS) { ensureStocks(); return; }
    var cards = []; for (var i = 0; i < w.length; i++) { var s = byCode(w[i]); if (s) cards.push(card(s, 'watch')); }
    watchSec.style.display = cards.length ? '' : 'none'; if (watchResults) watchResults.innerHTML = cards.join('');
  }
  function injectStars() {
    var cards = document.querySelectorAll('.card');
    for (var i = 0; i < cards.length; i++) {
      var cd = cards[i]; if (cd.querySelector('[data-star]')) continue;
      var px = cd.querySelector('[data-px]'); if (!px) continue;
      var c = px.getAttribute('data-px'); var r1 = cd.querySelector('.row1'); if (!r1) continue;
      var b = document.createElement('button'); b.className = 'star' + (inWatch(c) ? ' on' : ''); b.setAttribute('data-star', c); b.textContent = inWatch(c) ? '★' : '☆'; r1.appendChild(b);
    }
  }
  function syncStars() { var btns = document.querySelectorAll('[data-star]'); for (var i = 0; i < btns.length; i++) { var c = btns[i].getAttribute('data-star'), on = inWatch(c); btns[i].className = 'star' + (on ? ' on' : ''); btns[i].textContent = on ? '★' : '☆'; } }
  document.addEventListener('click', function (ev) {
    var t = ev.target; if (!t || !t.getAttribute) return;
    var sc = t.getAttribute('data-star'); if (sc) { toggleWatch(sc); syncStars(); renderWatch(); return; }
    var rm = t.getAttribute('data-rm'); if (rm) { toggleWatch(rm); syncStars(); renderWatch(); return; }
  });

  /* ---- USD⇄JPY トグル ---- */
  function reformatMoney() {
    document.querySelectorAll('[data-usd]').forEach(function (el) { el.textContent = fmtMoney(el.getAttribute('data-usd')); });
    if (q && q.value.trim()) run(); renderWatch();
  }
  function addJpyBtn() {
    var meta = document.querySelector('header .meta'); if (!meta || document.getElementById('jpybtn')) return;
    var b = document.createElement('button'); b.id = 'jpybtn'; b.className = 'jpy' + (jpyOn() ? ' on' : ''); b.type = 'button';
    b.textContent = jpyOn() ? '¥ 円' : '$ ドル';
    b.addEventListener('click', function () { try { if (jpyOn()) localStorage.removeItem(JPY_KEY); else localStorage.setItem(JPY_KEY, '1'); } catch (e) {} b.className = 'jpy' + (jpyOn() ? ' on' : ''); b.textContent = jpyOn() ? '¥ 円' : '$ ドル'; reformatMoney(); });
    meta.appendChild(b);
  }

  /* ---- ライブ価格 ---- */
  function applyPrices(map) {
    document.querySelectorAll('[data-px]').forEach(function (el) {
      var c = el.getAttribute('data-px'); if (map[c] != null) { el.setAttribute('data-usd', map[c]); el.textContent = fmtMoney(map[c]); }
    });
    document.querySelectorAll('.ez[data-ez-c]').forEach(function (el) {
      var c = el.getAttribute('data-ez-c'), limit = parseFloat(el.getAttribute('data-ez-limit'));
      if (map[c] == null || !limit) return; var pr = Number(map[c]);
      if (pr <= limit) { el.className = 'ez hit'; el.innerHTML = '🎯 狙い目 指値 ' + fmtMoney(limit) + ' <b>✅ 指値到達</b>（現値 ' + fmtMoney(pr) + '）'; }
      else { var pct = Math.round((pr - limit) / pr * 100); el.className = 'ez'; el.innerHTML = '🎯 狙い目 指値 ' + fmtMoney(limit) + ' 〜 現値 ' + fmtMoney(pr) + '<span class="ezn">-' + pct + '% の押し目</span>'; }
    });
    if (STOCKS) { for (var i = 0; i < STOCKS.length; i++) { if (map[STOCKS[i].c] != null) STOCKS[i].p = Number(map[STOCKS[i].c]); } }
  }
  function applyIndices(idx) {
    if (!idx) return;
    document.querySelectorAll('[data-idx]').forEach(function (el) {
      var k = el.getAttribute('data-idx'), d = idx[k]; if (!d) return;
      var v = el.querySelector('.v'), c = el.querySelector('.c');
      if (v) v.textContent = (k === 'USDJPY' ? '¥' : '') + Number(d.price).toLocaleString();
      if (c) { c.textContent = (d.chg >= 0 ? '+' : '') + d.chg + '%'; c.className = 'c ' + (d.chg >= 0 ? 'up' : 'dn'); }
    });
    if (idx.USDJPY && idx.USDJPY.price) RATE = Number(idx.USDJPY.price);
  }
  function setMktStatus() {
    var el = document.getElementById('mkt'); if (!el) return;
    var now = Date.now(), open = MKT.market_open && now >= MKT.open_ms && now < MKT.close_ms;
    if (open) { el.className = 'mkt open'; el.textContent = 'NY市場：開場中🟢'; }
    else { el.className = 'mkt closed'; el.textContent = 'NY市場：閉場⚫（次回 JST ' + MKT.next_open + '〜' + MKT.next_close + '）'; }
    return open;
  }
  function refreshPrices() {
    return fetch('prices.json?t=' + Date.now())
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        if (d.mkt) { MKT.open_ms = d.mkt.open_ms; MKT.close_ms = d.mkt.close_ms; MKT.market_open = d.mkt.market_open; MKT.next_open = d.mkt.next_open_jst || MKT.next_open; MKT.next_close = d.mkt.next_close_jst || MKT.next_close; }
        if (d.px) applyPrices(d.px);
        if (d.idx) applyIndices(d.idx);
        var lab = document.getElementById('pxasof'); if (lab && d.asof) lab.textContent = '株価 ' + d.asof + ' 時点（約15分遅延）';
        setMktStatus(); if (q && q.value.trim()) run(); renderWatch();
      })
      .catch(function (e) { console.warn('[globe] prices取得失敗', e); });
  }

  /* ---- 市場時間ポーリング：埋め込み窓(JST epoch)を使用 ---- */
  function tick() {
    var open = setMktStatus();
    if (open) { refreshPrices(); setTimeout(tick, 5 * 60 * 1000); }
    else {
      var now = Date.now(), wait;
      if (MKT.open_ms && now < MKT.open_ms) wait = MKT.open_ms - now; else wait = 6 * 3600 * 1000;
      setTimeout(tick, Math.max(60000, Math.min(wait, 6 * 3600 * 1000)));
    }
  }

  function addRefreshBtn() {
    var meta = document.querySelector('header .meta'); if (!meta || document.getElementById('pxrefresh')) return;
    var b = document.createElement('button'); b.id = 'pxrefresh'; b.className = 'refresh'; b.type = 'button'; b.textContent = '⟳ 更新';
    b.addEventListener('click', function () { b.disabled = true; refreshPrices().then(function () { setTimeout(function () { b.disabled = false; }, 1500); }); });
    meta.appendChild(b);
  }

  function init() {
    addRefreshBtn(); addJpyBtn(); injectStars(); setMktStatus();
    if (getWatch().length) ensureStocks(renderWatch); else ensureWatchSec();
    refreshPrices(); tick();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
