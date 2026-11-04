
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

  /* 銘柄名・理由などは外部データ由来。innerHTML に入れる前に必ずエスケープする */
  function esc(x) {
    return String(x == null ? '' : x).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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
  function starBtn(c) { var on = inWatch(c); return '<button class="star' + (on ? ' on' : '') + '" data-star="' + esc(c) + '">' + (on ? '★' : '☆') + '</button>'; }

  function card(s, mode) {
    var scls = s.sc >= 0 ? 'pos' : 'neg';
    var seg = s.m ? '<span class="seg">' + esc(s.m) + '</span>' : '';
    var levels = '';
    if (s.t && s.st) {
      levels = '<div class="levels" data-tgt="' + esc(s.t) + '" data-stp="' + esc(s.st) + '"><span class="lv tgt">利確 ' + fmtMoney(s.t) + '</span>' +
        '<span class="lv stp">損切 ' + fmtMoney(s.st) + '</span>' +
        (s.rr ? '<span class="lv rr">RR ' + s.rr + '</span>' : '') + '</div>';
    }
    var an = (s.tp != null) ? '<div class="analyst ' + (s.tp >= 0 ? 'up' : 'dn') + '">プロ予想 ' + (s.tp >= 0 ? '+' : '') + s.tp + '%</div>' : '';
    var fair = (s.val) ? '<div class="fair ' + (s.val === '割安' ? 'up' : s.val === '割高' ? 'dn' : 'hold') + '">理論株価 <b>' + esc(s.val) + '</b>（' + (s.fg >= 0 ? '+' : '') + s.fg + '%）</div>' : '';
    var reasons = (s.r && s.r.length) ? '<div class="reasons">' + s.r.map(function (r) { return '<span class="chip">' + esc(r) + '</span>'; }).join('') + '</div>' : '';
    var c = esc(s.c);
    var rm = (mode === 'watch') ? '<button class="rm" data-rm="' + c + '">×</button>' : '';
    return '<div class="card"><div class="row1"><span class="rank">' + (s.rk || '-') + '</span>' +
      '<div class="title"><span class="code">' + c + '</span><span class="name">' + esc(s.n) + '</span>' + seg + '</div>' +
      badge(s.g) + starBtn(s.c) + rm + '</div>' +
      '<div class="row2"><span class="price" data-px="' + c + '" data-usd="' + esc(s.p) + '">' + fmtMoney(s.p) + '</span>' +
      '<span class="score ' + scls + '">' + (s.sc >= 0 ? '+' : '') + s.sc + '</span>' + bar(s.sc) + '</div>' +
      levels + an + fair + reasons +
      '<div class="reasons"><span class="chip">スコア順 ' + (s.rk || '-') + ' 位 / ' + TOTAL + ' 銘柄</span></div></div>';
  }
  /* 検索キー（社名＋ティッカー）はクライアントで生成してキャッシュ（stocks.json を軽くするため） */
  function skey(s) { return s._k || (s._k = norm(s.n + ' ' + s.c)); }
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
    /* 1,800銘柄超ではスコア順だけだと "MU" 検索で MU 自体が9位以下に埋もれる。
       ティッカー完全一致 → ティッカー前方一致 → 社名一致 の順に並べ、同グループ内はスコア順。
       全角入力（ＮＶＤＡ）も NFKC で半角に揃えて照合する。 */
    var v = norm(raw), hits = [];
    for (var i = 0; i < STOCKS.length; i++) {
      var s = STOCKS[i], c = s.c.toLowerCase(), g;
      if (c === v) g = 0; else if (c.indexOf(v) === 0) g = 1; else if (skey(s).indexOf(v) >= 0) g = 2; else continue;
      hits.push([g, s]);
    }
    hits.sort(function (a, b) { return a[0] - b[0] || b[1].sc - a[1].sc; });
    var m = hits.map(function (x) { return x[1]; });
    var shown = m.slice(0, 8);
    if (hitEl) hitEl.textContent = m.length ? (m.length + '件ヒット / 上位' + shown.length + '件') : '';
    results.innerHTML = shown.length ? shown.map(function (s) { return card(s, 'search'); }).join('') : '<p class="empty">該当なし。社名(apple)やティッカー(AAPL)で検索してください。</p>';
    renderSizing();
  }
  var deb = null; function runDebounced() { if (deb) clearTimeout(deb); deb = setTimeout(run, 150); }
  if (q) { q.addEventListener('focus', function () { ensureStocks(); }); q.addEventListener('input', function () { ensureStocks(); runDebounced(); }); }

  var watchSec = null, watchResults = null;
  function ensureWatchSec() {
    if (watchSec) return;
    watchSec = document.createElement('section'); watchSec.id = 'watch-sec'; watchSec.className = 'sec'; watchSec.style.display = 'none';
    watchSec.setAttribute('data-key', 'watch'); watchSec.setAttribute('data-nav', 'ウォッチ');
    watchSec.innerHTML = '<h2 class="find"><span>ウォッチリスト</span><em>WATCHLIST</em></h2><div id="watch-results" class="cards"></div>';
    if (searchSec && searchSec.parentNode) searchSec.parentNode.insertBefore(watchSec, searchSec.nextSibling);
    watchResults = watchSec.querySelector('#watch-results'); makeFoldable(watchSec);
  }
  function renderWatch() {
    ensureWatchSec(); var w = getWatch();
    if (!w.length) { watchSec.style.display = 'none'; if (watchResults) watchResults.innerHTML = ''; buildNav(); return; }
    if (!STOCKS) { ensureStocks(); return; }
    var cards = []; for (var i = 0; i < w.length; i++) { var s = byCode(w[i]); if (s) cards.push(card(s, 'watch')); }
    watchSec.style.display = cards.length ? '' : 'none'; if (watchResults) watchResults.innerHTML = cards.join('');
    updateCount(watchSec); buildNav(); renderSizing();
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
  /* M&Aシグナル欄でウォッチ中の銘柄を強調（ウォッチはこの端末のブラウザにだけ保存されている） */
  function markMaWatch() {
    document.querySelectorAll('[data-ma-c]').forEach(function (el) {
      var on = inWatch(el.getAttribute('data-ma-c')), tag = el.querySelector('.ma-watch');
      el.classList.toggle('watched', on);
      if (on && !tag) { var t = document.createElement('span'); t.className = 'chip ma-watch'; t.textContent = '★ウォッチ中'; el.insertBefore(t, el.querySelector('.ma-link')); }
      if (!on && tag) tag.remove();
    });
  }
  function syncStars() { var btns = document.querySelectorAll('[data-star]'); for (var i = 0; i < btns.length; i++) { var c = btns[i].getAttribute('data-star'), on = inWatch(c); btns[i].className = 'star' + (on ? ' on' : ''); btns[i].textContent = on ? '★' : '☆'; } }
  document.addEventListener('click', function (ev) {
    var t = ev.target; if (!t || !t.getAttribute) return;
    var sc = t.getAttribute('data-star'); if (sc) { toggleWatch(sc); syncStars(); renderWatch(); markMaWatch(); return; }
    var rm = t.getAttribute('data-rm'); if (rm) { toggleWatch(rm); syncStars(); renderWatch(); markMaWatch(); return; }
  });

  /* ---- USD⇄JPY トグル ---- */
  function reformatMoney() {
    document.querySelectorAll('[data-usd]').forEach(function (el) { el.textContent = fmtMoney(el.getAttribute('data-usd')); });
    if (q && q.value.trim()) run(); renderWatch(); renderSizing(); renderTrades();
  }
  function addJpyBtn() {
    var meta = document.querySelector('header .meta'); if (!meta || document.getElementById('jpybtn')) return;
    var b = document.createElement('button'); b.id = 'jpybtn'; b.className = 'jpy' + (jpyOn() ? ' on' : ''); b.type = 'button';
    b.textContent = jpyOn() ? '¥ 円' : '$ ドル';
    b.addEventListener('click', function () { try { if (jpyOn()) localStorage.removeItem(JPY_KEY); else localStorage.setItem(JPY_KEY, '1'); } catch (e) {} b.className = 'jpy' + (jpyOn() ? ' on' : ''); b.textContent = jpyOn() ? '¥ 円' : '$ ドル'; reformatMoney(); });
    meta.appendChild(b);
  }

  /* ---- ライブ価格 ---- */
  /* 保有カードの損益と利確・損切の到達状況（サーバ側 _hold_pl_html と同じ表示） */
  function holdPl(price, avg, tgt, stp) {
    var pct = avg ? Math.round((price - avg) / avg * 1000) / 10 : 0, cls = pct >= 0 ? 'up' : 'dn', hit = '';
    if (stp && price <= stp) hit = '<b class="hit dn">⚠ 損切ライン到達</b>';
    else if (tgt && price >= tgt) hit = '<b class="hit up">✅ 利確ライン到達</b>';
    var dist = (!hit && price > 0 && tgt && stp) ? '<span class="hnote">利確まで +' + ((tgt - price) / price * 100).toFixed(1) +
      '% ／ 損切まで -' + ((price - stp) / price * 100).toFixed(1) + '%</span>' : '';
    return '<span class="' + cls + '">損益 ' + (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%</span>' + hit +
      '<span class="hnote">買値 ' + fmtMoney(avg) + ' → 現値 ' + fmtMoney(price) + '</span>' + dist;
  }
  function applyPrices(map) {
    /* 損益の行も現値に追従させる（以前は生成時の株価のままで、上の現値と食い違っていた） */
    document.querySelectorAll('[data-hold-c]').forEach(function (el) {
      var c = el.getAttribute('data-hold-c'); if (map[c] == null) return;
      el.innerHTML = holdPl(Number(map[c]), parseFloat(el.getAttribute('data-avg')) || 0,
        parseFloat(el.getAttribute('data-tgt')) || 0, parseFloat(el.getAttribute('data-stp')) || 0);
    });
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
    var now = Date.now();
    /* 開場判定は埋め込み窓(epoch)で行う。market_openはHTML/prices.json生成時点の
       スナップショットなので、窓内に入っても閉場のまま固まる（＝5分ポーリングに
       切り替わらない）。窓が無いときだけフラグにフォールバックする。 */
    var open = (MKT.open_ms && MKT.close_ms)
      ? (now >= MKT.open_ms && now < MKT.close_ms)
      : MKT.market_open;
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
        setMktStatus(); if (q && q.value.trim()) run(); renderWatch(); renderSizing();
      })
      .catch(function (e) { console.warn('[globe] prices取得失敗', e); });
  }

  /* ---- 市場時間ポーリング：埋め込み窓(JST epoch)を使用 ---- */
  function tick() {
    var open = setMktStatus();
    if (open) { refreshPrices(); setTimeout(tick, 5 * 60 * 1000); }
    else {
      // 閉場中でも60分毎に更新（窓の再取得＝翌営業日窓へ自動追従・デッドロック回避）
      refreshPrices();
      var now = Date.now(), wait;
      if (MKT.open_ms && now < MKT.open_ms) wait = Math.min(MKT.open_ms - now, 60 * 60 * 1000);
      else wait = 60 * 60 * 1000;
      setTimeout(tick, Math.max(60000, wait));
    }
  }

  function addRefreshBtn() {
    var meta = document.querySelector('header .meta'); if (!meta || document.getElementById('pxrefresh')) return;
    var b = document.createElement('button'); b.id = 'pxrefresh'; b.className = 'refresh'; b.type = 'button'; b.textContent = '⟳ 更新';
    b.addEventListener('click', function () { b.disabled = true; refreshPrices().then(function () { setTimeout(function () { b.disabled = false; }, 1500); }); });
    meta.appendChild(b);
  }


  /* ── 取引コーチ：資金管理（推奨株数）・手数料チェック・取引成績（すべて端末内で完結） ──
     ※ 取引履歴は個人情報のため、リポジトリ（公開）には一切送らず localStorage にだけ保存する。 */
  var RISK_KEY = 'go_risk_v1', TR_KEY = 'go_trades_v1';
  var FEE = 0.00495, FEE_RT = 0.0099, MAX_POS = 0.25, MIN_EDGE = 2.0;   /* SBI米国株 0.495%(税込)/片道 */
  function rateNow() { return RATE > 0 ? RATE : 150; }
  function yen(v) { return '¥' + Math.round(v).toLocaleString(); }
  function getRisk() { try { var o = JSON.parse(localStorage.getItem(RISK_KEY) || 'null'); return (o && o.cap > 0) ? o : { cap: 0, pct: 1 }; } catch (e) { return { cap: 0, pct: 1 }; } }
  function setRisk(o) { try { localStorage.setItem(RISK_KEY, JSON.stringify(o)); } catch (e) {} }

  function sizingHtml(px, tgt, stp) {
    if (!(px > 0 && stp > 0 && stp < px)) return '';
    var r = getRisk(), R = rateNow(), gain = (tgt / px - 1) * 100, loss = (1 - stp / px) * 100, out = [];
    if (r.cap > 0) {
      var perShare = (px - stp) + px * FEE_RT, riskYen = r.cap * r.pct / 100;
      var byRisk = Math.floor(riskYen / R / perShare), byCap = Math.floor(r.cap * MAX_POS / R / px), n = Math.min(byRisk, byCap);
      if (n < 1) out.push('<div class="sz warn">📐 1株でも許容損失（' + yen(riskYen) + '）を超えます → 見送り推奨</div>');
      else out.push('<div class="sz">📐 推奨 <b>' + n + '株</b>（約' + yen(n * px * R) + '・資金の' + Math.round(n * px * R / r.cap * 100) + '%）' +
        '<span class="szn">損切り時 −' + yen(n * perShare * R) + (byCap < byRisk ? '・1銘柄25%上限' : '') + '</span></div>');
    } else out.push('<div class="sz mute">📐 「取引コーチ」で運用資金を入れると推奨株数を表示します</div>');
    if (gain > 0 && gain < MIN_EDGE) out.push('<div class="sz warn">⚠ 利確幅 +' + gain.toFixed(1) + '% は特定口座の往復手数料（約1%）で半分以上消えます</div>');
    else if (gain > 0) out.push('<div class="sz mute">手数料込み（特定）：利確 +' + (gain - FEE_RT * 100).toFixed(1) + '% ／ 損切 −' + (loss + FEE_RT * 100).toFixed(1) + '%</div>');
    return out.join('');
  }
  function renderSizing() {
    document.querySelectorAll('.card .levels[data-stp]').forEach(function (lv) {
      var cardEl = lv.closest('.card'); if (!cardEl || lv.closest('#sec-hold')) return;
      var pe = cardEl.querySelector('.price[data-usd]'); var px = pe ? parseFloat(pe.getAttribute('data-usd')) : 0;
      var old = cardEl.querySelector('.szw'); if (old) old.parentNode.removeChild(old);
      var h = sizingHtml(px, parseFloat(lv.getAttribute('data-tgt')) || 0, parseFloat(lv.getAttribute('data-stp')) || 0); if (!h) return;
      var w = document.createElement('div'); w.className = 'szw'; w.innerHTML = h; lv.parentNode.insertBefore(w, lv.nextSibling);
    });
  }

  /* ---- SBI 約定履歴CSV（Shift_JIS）の読み込み ---- */
  function csvLine(line) {
    var out = [], cur = '', inq = false;
    for (var i = 0; i < line.length; i++) {
      var ch = line[i];
      if (inq) { if (ch === '"') { if (line[i + 1] === '"') { cur += '"'; i++; } else inq = false; } else cur += ch; }
      else if (ch === '"') inq = true; else if (ch === ',') { out.push(cur); cur = ''; } else cur += ch;
    }
    out.push(cur); return out.map(function (x) { return x.trim(); });
  }
  function parseSbi(text) {
    var lines = String(text || '').split(/\r?\n/), hi = -1;
    for (var i = 0; i < lines.length; i++) { if (lines[i].indexOf('約定日') >= 0 && lines[i].indexOf('取引') >= 0) { hi = i; break; } }
    if (hi < 0) return null;
    var H = csvLine(lines[hi]);
    function col(names) { for (var j = 0; j < names.length; j++) { for (var k = 0; k < H.length; k++) { if (H[k].indexOf(names[j]) >= 0) return k; } } return -1; }
    var C = { d: col(['国内約定日', '約定日']), cur: col(['通貨']), nm: col(['銘柄']), side: col(['取引']), ac: col(['預り']),
      q: col(['約定数量', '数量']), px: col(['約定単価', '単価']), amt: col(['受渡金額']) };
    if (C.d < 0 || C.nm < 0 || C.side < 0 || C.q < 0 || C.px < 0) return null;
    var rows = [];
    for (var r = hi + 1; r < lines.length; r++) {
      if (!lines[r].trim()) continue;
      var f = csvLine(lines[r]), dm = (f[C.d] || '').match(/(\d{4})\D+(\d{1,2})\D+(\d{1,2})/);
      var tm = (f[C.nm] || '').match(/([A-Z][A-Z0-9.\-]{0,6})\s*\/\s*[A-Za-z][^\/]*$/);
      var sd = f[C.side] || '', side = sd.indexOf('買') >= 0 ? 'B' : (sd.indexOf('売') >= 0 ? 'S' : '');
      var qn = parseFloat(String(f[C.q]).replace(/,/g, '')), pn = parseFloat(String(f[C.px]).replace(/,/g, ''));
      if (!dm || !tm || !side || !(qn > 0) || !(pn > 0)) continue;
      rows.push({ d: dm[1] + '-' + ('0' + dm[2]).slice(-2) + '-' + ('0' + dm[3]).slice(-2), c: tm[1], s: side,
        a: (f[C.ac] || '').indexOf('NISA') >= 0 ? 'NISA' : ((f[C.ac] || '特定').replace(/預り/, '') || '特定'),
        j: (C.cur >= 0 && (f[C.cur] || '').indexOf('円') >= 0) ? 1 : 0, q: qn, p: pn,
        m: C.amt >= 0 ? parseFloat(String(f[C.amt]).replace(/,/g, '')) || 0 : 0 });
    }
    return rows;
  }
  function getTrades() { try { return JSON.parse(localStorage.getItem(TR_KEY) || '[]') || []; } catch (e) { return []; } }
  function rowKey(x) { return [x.d, x.c, x.s, x.a, x.q, x.p].join('|'); }
  function mergeTrades(add) {
    var cur = getTrades(), seen = {}; cur.forEach(function (x) { seen[rowKey(x)] = 1; });
    var n = 0; add.forEach(function (x) { if (!seen[rowKey(x)]) { seen[rowKey(x)] = 1; cur.push(x); n++; } });
    try { localStorage.setItem(TR_KEY, JSON.stringify(cur)); } catch (e) {}
    return n;
  }
  function daysBetween(a, b) { return Math.round((Date.parse(b) - Date.parse(a)) / 86400000); }
  /* 先入れ先出しで買いと売りを対応付け。ドル決済は受渡金額（手数料込み）、円貨決済は単価から推計。 */
  function pairTrades(rows) {
    var idx = rows.map(function (x, i) { return [x, i]; });
    idx.sort(function (A, B) { var a = A[0], b = B[0]; return a.d < b.d ? -1 : a.d > b.d ? 1 : (a.s === b.s ? B[1] - A[1] : (a.s === 'B' ? -1 : 1)); });
    var lots = {}, closed = [], unmatched = 0;
    idx.forEach(function (it) {
      var x = it[0], k = x.c + '|' + x.a, nisa = x.a === 'NISA';
      var feeUsd = x.j ? (nisa ? 0 : x.q * x.p * FEE) : (x.m > 0 ? Math.abs(x.m - x.q * x.p) : (nisa ? 0 : x.q * x.p * FEE));
      if (feeUsd > x.q * x.p * 0.02) feeUsd = nisa ? 0 : x.q * x.p * FEE;   /* 受渡金額の解釈違いに対する保険 */
      if (x.s === 'B') { (lots[k] = lots[k] || []).push({ d: x.d, q: x.q, cps: x.p + feeUsd / x.q, fps: feeUsd / x.q }); return; }
      var pps = x.p - feeUsd / x.q, rem = x.q, L = lots[k] || [];
      while (rem > 1e-9 && L.length) {
        var lot = L[0], m = Math.min(rem, lot.q);
        closed.push({ c: x.c, a: x.a, bd: lot.d, sd: x.d, q: m, cost: m * lot.cps, proc: m * pps, fee: m * (lot.fps + feeUsd / x.q), days: daysBetween(lot.d, x.d) });
        lot.q -= m; rem -= m; if (lot.q <= 1e-9) L.shift();
      }
      if (rem > 1e-9) unmatched++;
    });
    var open = []; Object.keys(lots).forEach(function (k) { lots[k].forEach(function (l) { if (l.q > 1e-9) open.push({ c: k.split('|')[0], a: k.split('|')[1], d: l.d, q: l.q, cps: l.cps }); }); });
    closed.forEach(function (t) { t.pl = t.proc - t.cost; t.pct = (t.proc / t.cost - 1) * 100; });
    return { closed: closed, open: open, unmatched: unmatched };
  }
  function avg(a) { return a.length ? a.reduce(function (s, v) { return s + v; }, 0) / a.length : 0; }
  function diagnose(cl) {
    var W = cl.filter(function (t) { return t.pct > 0.05; }), Ls = cl.filter(function (t) { return t.pct < -0.05; });
    var gw = W.reduce(function (s, t) { return s + t.pl; }, 0), fee = cl.reduce(function (s, t) { return s + t.fee; }, 0);
    var aw = avg(W.map(function (t) { return t.pct; })), al = avg(Ls.map(function (t) { return t.pct; }));
    var out = [];
    if (fee > 0 && gw > 0 && fee / (gw + fee) > 0.2) out.push(['warn', '手数料（' + fmtMoney(fee) + '）が勝ち取引の利益の ' + Math.round(fee / (gw + fee) * 100) + '% を食っています。特定口座は利確幅 +2% 以上の取引だけに絞りましょう。']);
    if (W.length && Ls.length && Math.abs(al) > aw) out.push(['warn', '平均の負け（' + al.toFixed(1) + '%）が平均の勝ち（+' + aw.toFixed(1) + '%）より大きい「損大利小」です。損切りは買う前に決めた価格で機械的に。']);
    var big = Ls.filter(function (t) { return t.pct <= -8; });
    if (big.length) out.push(['warn', '−8% を超えて持ち続けた負け：' + big.map(function (t) { return t.c + ' ' + t.pct.toFixed(1) + '%'; }).join('、') + '。−8%で損切りしていれば、損失を約 ' + fmtMoney(big.reduce(function (s, t) { return s + (-t.pct - 8) / 100 * t.cost; }, 0)) + ' 減らせました。']);
    var sw = avg(W.map(function (t) { return t.cost; })), sl = avg(Ls.map(function (t) { return t.cost; }));
    if (W.length && Ls.length && sl > sw * 1.3) out.push(['warn', '負けた取引の平均投資額（' + fmtMoney(sl) + '）が勝ちの ' + (sl / sw).toFixed(1) + ' 倍。大きく張った時ほど負けています → 推奨株数で金額をそろえましょう。']);
    var ns = cl.filter(function (t) { return t.a === 'NISA' && t.days < 60; });
    if (ns.length) out.push(['warn', 'NISAで ' + ns.length + ' 回の短期売買（60日未満）。NISAは売っても枠が年内に戻らず、損失は他と相殺できません。NISAは長期保有向けに。']);
    var dt = cl.filter(function (t) { return t.days <= 1; });
    if (dt.length) out.push(['warn', '買って1日以内に売った取引が ' + dt.length + ' 回（' + dt.map(function (t) { return t.c; }).join('、') + '）。手数料だけ払う結果になりやすいので避けましょう。']);
    var good = Ls.filter(function (t) { return t.pct > -6 && t.days > 1; });
    if (good.length) out.push(['good', '小さく負けて撤退できた取引：' + good.map(function (t) { return t.c + ' ' + t.pct.toFixed(1) + '%'; }).join('、') + '。この損切りは正解です。']);
    if (W.length) out.push(['good', '勝ちパターン：平均 +' + aw.toFixed(1) + '%・平均 ' + Math.round(avg(W.map(function (t) { return t.days; }))) + ' 日保有。']);
    return out;
  }
  function renderTrades() {
    var box = document.getElementById('trstats'); if (!box) return;
    var rows = getTrades();
    if (!rows.length) { box.innerHTML = '<p class="cnote">まだ取引データがありません。SBI証券の外国株式「約定履歴」でCSVをダウンロードし、上のボタンから読み込んでください。</p>'; return; }
    var P = pairTrades(rows), cl = P.closed.slice().sort(function (a, b) { return a.sd < b.sd ? 1 : -1; });
    var W = cl.filter(function (t) { return t.pct > 0.05; }), Ls = cl.filter(function (t) { return t.pct < -0.05; });
    var pl = cl.reduce(function (s, t) { return s + t.pl; }, 0), fee = cl.reduce(function (s, t) { return s + t.fee; }, 0);
    var aw = avg(W.map(function (t) { return t.pct; })), al = avg(Ls.map(function (t) { return t.pct; }));
    var wr = cl.length ? W.length / cl.length : 0, ev = wr * aw + (cl.length ? Ls.length / cl.length : 0) * al;
    function kpi(label, val, cls) { return '<div class="kpi"><div class="kl">' + label + '</div><div class="kv ' + (cls || '') + '">' + val + '</div></div>'; }
    var h = '<div class="kpis">' +
      kpi('実現損益', (pl >= 0 ? '+' : '−') + fmtMoney(Math.abs(pl)), pl >= 0 ? 'up' : 'dn') +
      kpi('勝率', Math.round(wr * 100) + '%<small>' + W.length + '勝' + Ls.length + '敗' + (cl.length - W.length - Ls.length ? (cl.length - W.length - Ls.length) + '分' : '') + '</small>') +
      kpi('平均の勝ち', W.length ? '+' + aw.toFixed(1) + '%' : '—', 'up') +
      kpi('平均の負け', Ls.length ? al.toFixed(1) + '%' : '—', 'dn') +
      kpi('損益比', (W.length && Ls.length) ? (aw / Math.abs(al)).toFixed(2) : '—', (W.length && Ls.length && aw / Math.abs(al) >= 1.5) ? 'up' : 'dn') +
      kpi('1回の期待値', (ev >= 0 ? '+' : '') + ev.toFixed(2) + '%', ev >= 0 ? 'up' : 'dn') +
      kpi('手数料合計', fmtMoney(fee)) +
      kpi('平均保有', Math.round(avg(cl.map(function (t) { return t.days; }))) + '日') + '</div>';
    var dg = diagnose(cl);
    if (dg.length) h += '<ul class="diag">' + dg.map(function (d) { return '<li class="' + d[0] + '">' + esc(d[1]) + '</li>'; }).join('') + '</ul>';
    h += '<div class="trs">' + cl.map(function (t) {
      return '<div class="tr-row"><span class="ma-date">' + t.sd.slice(5).replace('-', '/') + '</span><span class="code">' + esc(t.c) + '</span>' +
        '<span class="tac ' + (t.a === 'NISA' ? 'nisa' : '') + '">' + esc(t.a) + '</span><span class="trd">' + t.days + '日</span>' +
        '<span class="trp ' + (t.pl >= 0 ? 'up' : 'dn') + '">' + (t.pct >= 0 ? '+' : '') + t.pct.toFixed(1) + '%</span>' +
        '<span class="trv ' + (t.pl >= 0 ? 'up' : 'dn') + '">' + (t.pl >= 0 ? '+' : '−') + fmtMoney(Math.abs(t.pl)) + '</span></div>';
    }).join('') + '</div>';
    if (P.open.length) h += '<p class="cnote">保有中（未決済）：' + P.open.map(function (o) { return esc(o.c) + ' ' + o.q + '株 @' + fmtMoney(o.cps); }).join('、') + '</p>';
    h += '<p class="cnote">損益はドル建て（為替差損益を含まず、円貨決済は手数料を推計）。SBIの円建て損益とは為替の分だけ差が出ます。' +
      (P.unmatched ? ' 買付が期間外の売却 ' + P.unmatched + ' 件は集計対象外です。' : '') + '</p>';
    box.innerHTML = h;
  }
  function renderCoach() {
    var box = document.getElementById('coach'); if (!box) return;
    var r = getRisk();
    box.innerHTML =
      '<div class="cset"><label>運用資金<span class="inp"><input id="capin" type="text" inputmode="numeric" placeholder="例 1000000" value="' + (r.cap || '') + '">円</span></label>' +
      '<label>1回の許容損失<select id="riskin">' + [0.5, 1, 2].map(function (v) { return '<option value="' + v + '"' + (r.pct == v ? ' selected' : '') + '>' + v + '%</option>'; }).join('') + '</select></label>' +
      '<div id="riskout" class="cnote"></div></div>' +
      '<ol class="rules">' +
      '<li><b>1回の損失は資金の1%まで</b> — 各カードの「📐 推奨株数」で買う</li>' +
      '<li><b>損切りは買う前に決める</b> — 損切ライン到達で即売り。−8%を超えて持たない</li>' +
      '<li><b>特定口座は利確幅 +2% 以上だけ</b> — 往復手数料が約1%かかる</li>' +
      '<li><b>NISAは長期保有用</b> — 短期売買は枠を消費し、損失も通算できない</li>' +
      '<li><b>1銘柄は資金の25%まで</b>・<b>当日の往復売買はしない</b></li></ol>' +
      '<div class="cimp"><label class="cbtn">📥 SBIの約定履歴CSVを読み込む<input id="trfile" type="file" accept=".csv,text/csv"></label>' +
      '<button type="button" id="trclr" class="cbtn ghost">消去</button></div>' +
      '<p class="cnote">CSVはこの端末の中だけで処理・保存され、どこにも送信されません。</p><div id="trstats"></div>';
    function riskOut() {
      var o = getRisk(), el = document.getElementById('riskout'); if (!el) return;
      el.textContent = o.cap > 0 ? '→ 1回の最大損失 ' + yen(o.cap * o.pct / 100) + '／1銘柄の上限 ' + yen(o.cap * MAX_POS) : '資金を入れると、各カードに推奨株数が出ます。';
    }
    function save() {
      var cap = parseFloat(String(document.getElementById('capin').value).replace(/[^\d.]/g, '')) || 0;
      setRisk({ cap: cap, pct: parseFloat(document.getElementById('riskin').value) || 1 }); riskOut(); renderSizing();
    }
    document.getElementById('capin').addEventListener('change', save);
    document.getElementById('riskin').addEventListener('change', save);
    document.getElementById('trfile').addEventListener('change', function (e) {
      var f = e.target.files && e.target.files[0]; if (!f) return;
      var rd = new FileReader();
      rd.onload = function () {
        var txt; try { txt = new TextDecoder('shift_jis').decode(rd.result); } catch (er) { txt = ''; }
        var rows = parseSbi(txt);
        if (!rows || !rows.length) { try { rows = parseSbi(new TextDecoder('utf-8').decode(rd.result)); } catch (er) { rows = null; } }
        if (!rows || !rows.length) { alert('約定履歴のCSVとして読み取れませんでした。SBI証券の外国株式「約定履歴」のCSVを選んでください。'); return; }
        var n = mergeTrades(rows); renderTrades(); alert(rows.length + '件を読み込みました（新規 ' + n + '件）。');
        e.target.value = '';
      };
      rd.readAsArrayBuffer(f);
    });
    document.getElementById('trclr').addEventListener('click', function () {
      if (!confirm('この端末に保存した取引データを消去しますか？')) return;
      try { localStorage.removeItem(TR_KEY); } catch (er) {} renderTrades();
    });
    riskOut(); renderTrades();
  }

  /* ── 折りたたみ（▼）と目次 ── 開閉状態は端末(localStorage)に保存 */
  var FOLD_KEY = 'go_fold_v1', FOLD_DEFAULT = { ma: 1, cand: 1 };
  function getFold() { try { var o = JSON.parse(localStorage.getItem(FOLD_KEY) || 'null'); return (o && typeof o === 'object') ? o : null; } catch (e) { return null; } }
  function saveFold(k, on) { var o = getFold() || JSON.parse(JSON.stringify(FOLD_DEFAULT)); o[k] = on ? 1 : 0; try { localStorage.setItem(FOLD_KEY, JSON.stringify(o)); } catch (e) {} }
  function isFolded(k) { var o = getFold(); return o ? !!o[k] : !!FOLD_DEFAULT[k]; }
  function foldSecs() { return Array.prototype.slice.call(document.querySelectorAll('section.sec[data-key]')); }
  function setFolded(sec, on, save) {
    sec.classList.toggle('collapsed', !!on);
    var h = sec.querySelector('.sec-h'); if (h) h.setAttribute('aria-expanded', on ? 'false' : 'true');
    if (save) saveFold(sec.getAttribute('data-key'), on);
  }
  function secCount(sec) { return sec.querySelectorAll('.card, .ma-row').length; }
  function updateCount(sec) {
    var c = sec && sec.querySelector('.sec-h .cnt'); if (!c) return;
    var n = secCount(sec); c.textContent = n ? String(n) : ''; c.style.display = n ? '' : 'none';
  }
  function makeFoldable(sec) {
    if (!sec || sec.querySelector('.sec-h')) return;
    var h2 = sec.querySelector('h2.find'); if (!h2 || h2.parentNode !== sec) return;
    var k = sec.getAttribute('data-key');
    var h = document.createElement('div'); h.className = 'sec-h'; h.setAttribute('role', 'button'); h.tabIndex = 0;
    sec.insertBefore(h, h2); h.appendChild(h2);
    var cnt = document.createElement('span'); cnt.className = 'cnt'; h.appendChild(cnt);
    var ch = document.createElement('span'); ch.className = 'chev'; ch.setAttribute('aria-hidden', 'true'); ch.textContent = '▼'; h.appendChild(ch);
    var body = document.createElement('div'); body.className = 'sec-body'; body.id = 'body-' + k;
    var inn = document.createElement('div'); inn.className = 'sec-in'; body.appendChild(inn);
    while (h.nextSibling) inn.appendChild(h.nextSibling);
    sec.appendChild(body); h.setAttribute('aria-controls', body.id);
    function toggle() { setFolded(sec, !sec.classList.contains('collapsed'), true); syncAllBtn(); }
    h.addEventListener('click', function (e) { if (e.target.closest && e.target.closest('a,button,input')) return; toggle(); });
    h.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
    setFolded(sec, isFolded(k), false); updateCount(sec);
  }
  function syncAllBtn() {
    var b = document.getElementById('navall'); if (!b) return;
    var anyOpen = foldSecs().some(function (s) { return s.style.display !== 'none' && !s.classList.contains('collapsed'); });
    b.textContent = anyOpen ? '▲ すべて閉じる' : '▼ すべて開く';
  }
  function buildNav() {
    var nav = document.getElementById('nav'); if (!nav) return;
    var html = [];
    if (document.getElementById('q')) html.push('<a href="#search-sec" data-go="search">🔍 検索</a>');
    foldSecs().forEach(function (s) {
      if (s.style.display === 'none') return;
      var n = secCount(s);
      html.push('<a href="#' + esc(s.id) + '" data-go="' + esc(s.getAttribute('data-key')) + '">' + esc(s.getAttribute('data-nav') || '') +
        (n ? '<span class="n">' + n + '</span>' : '') + '</a>');
    });
    html.push('<button type="button" id="navall"></button>');
    nav.innerHTML = html.join(''); syncAllBtn();
  }
  function initFold() {
    foldSecs().forEach(makeFoldable); buildNav();
    var nav = document.getElementById('nav'); if (!nav || nav._bound) return; nav._bound = 1;
    nav.addEventListener('click', function (e) {
      var t = e.target.closest ? e.target.closest('a,button') : null; if (!t) return;
      if (t.id === 'navall') {
        var anyOpen = foldSecs().some(function (s) { return s.style.display !== 'none' && !s.classList.contains('collapsed'); });
        foldSecs().forEach(function (s) { setFolded(s, anyOpen, true); }); syncAllBtn(); return;
      }
      var k = t.getAttribute('data-go');
      if (k === 'search') { e.preventDefault(); var q = document.getElementById('q'); var ss = document.getElementById('search-sec');
        if (ss) ss.scrollIntoView({ behavior: 'smooth', block: 'start' }); if (q) setTimeout(function () { q.focus({ preventScroll: true }); }, 350); return; }
      var sec = document.querySelector('section.sec[data-key="' + k + '"]');
      if (sec) { e.preventDefault(); if (sec.classList.contains('collapsed')) { setFolded(sec, false, true); syncAllBtn(); }
        sec.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    });
  }

  function init() {
    renderCoach(); initFold(); addRefreshBtn(); addJpyBtn(); injectStars(); setMktStatus(); markMaWatch(); renderSizing();
    if (getWatch().length) ensureStocks(renderWatch); else ensureWatchSec();
    tick();   /* tick が即座に refreshPrices を呼ぶ（以前は二重に取得していた） */
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
