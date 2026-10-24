// ==UserScript==
// @name         Video Helper
// @namespace    https://github.com/dadanny1122/video-helper
// @version      3.11.0
// @description  安全防護（擋彈窗、跳轉、危險下載）、省流模式（廣告不下載）、影片狀態、隱藏廣告、自動略過廣告、載入時間評估、一鍵下載
// @author       dadanny1122
// @license      MIT
// @homepageURL  https://github.com/dadanny1122/video-helper
// @supportURL   https://github.com/dadanny1122/video-helper/issues
// @match        *://*.pornhub.com/*
// @match        *://*.xvideos.com/*
// @match        *://*.xnxx.com/*
// @match        *://*.youporn.com/*
// @match        *://*.redtube.com/*
// @match        *://*.jable.tv/*
// @match        *://*.hanime1.me/*
// @match        *://*.eporner.com/*
// @match        *://*.hqporner.com/*
// @match        *://*.beeg.com/*
// @match        *://*.sxyprn.com/*
// @match        *://*.spankbang.com/*
// @match        *://*.xmoviesforyou.com/*
// @match        *://*.porntrex.com/*
// @match        *://*.youjizz.com/*
// @match        *://*.pornone.com/*
// @match        *://*.3movs.com/*
// @match        *://*.tube8.com/*
// @match        *://*.porndig.com/*
// @match        *://*.cumlouder.com/*
// @match        *://*.txxx.com/*
// @match        *://*.porndoe.com/*
// @match        *://*.vporn.com/*
// @include      /^https?:\/\/([^\/]*\.)?xhamster\d*\.(com|desi|one)\//
// @include      /^https?:\/\/([^\/]*\.)?missav[^\/]*\//
// @require      https://cdn.jsdelivr.net/npm/mux.js@6.3.0/dist/mux.min.js
// @grant        GM_download
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        GM_info
// @grant        GM_setClipboard
// @grant        unsafeWindow
// @connect      *
// @connect      127.0.0.1
// @run-at       document-start
// @inject-into  page
// ==/UserScript==

/*
 * 架構說明
 *   第一層 核心：共用工具、設定、提示介面
 *   第二層 網站轉接器（SITES）：每個網站開哪些功能、影片網址在哪
 *   第三層 功能模組（MODULES）：安全防護、隱藏廣告、跳廣告、評估、列表標示、下載
 *
 * 加新網站 → 在 SITES 加一筆
 * 加新功能 → 在 MODULES 加一個，再到網站的 features 裡打開
 */

(function () {
  'use strict';

  /* ==================================================================
   * 第一層：核心
   * ================================================================== */

  const PW = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
  const IS_TOP = window.top === window.self;
  const HOST = location.hostname;
  const baseOf = h => h.split('.').slice(-2).join('.');
  const HOME = baseOf(HOST);

  // ---- 設定（存在暴力猴裡，所有網站共用）----
  // 主要存在暴力猴裡（所有網站共用），同時在網站的 localStorage 留一份備份
  const store = {
    get(k, d) {
      try { const v = GM_getValue(k); if (v !== undefined) return v; } catch (e) {}
      try { const b = localStorage.getItem('vh_' + k); if (b !== null) return JSON.parse(b); } catch (e) {}
      return d;
    },
    set(k, v) {
      try { GM_setValue(k, v); } catch (e) {}
      try { localStorage.setItem('vh_' + k, JSON.stringify(v)); } catch (e) {}
    },
  };
  const settings = store.get('settings', {});
  const isOn = name => settings[name] !== false;   // 預設全部開啟

  // ---- 網址判斷 ----
  const DANGER_EXT = /\.(exe|msi|apk|bat|cmd|scr|vbs|ps1|jar|dmg|pkg|com)$/i;

  function isExternal(url) {
    try {
      const u = new URL(url, location.href);
      return /^https?:$/.test(u.protocol) && baseOf(u.hostname) !== HOME;
    } catch (e) { return false; }
  }

  // 偽裝網址：自己網站的 /pop?url=https://廣告商... → 取出裡面真正的目的地
  function unwrap(url) {
    try {
      const u = new URL(url, location.href);
      for (const v of u.searchParams.values()) if (/^https?:\/\//i.test(v)) return v;
    } catch (e) {}
    return url;
  }

  // 這個網址開出去，是不是廣告（直接開到別的網站、包著別的網站、或是網站自己的廣告路徑）
  function isAdTarget(url) {
    return isExternal(url) || isExternal(unwrap(url)) || isAd(url);
  }

  function isDangerous(url) {
    try { return DANGER_EXT.test(new URL(url, location.href).pathname); }
    catch (e) { return false; }
  }

  // ---- 已知的廣告網路（成人網站常用的 + 一般大型廣告商）----
  const AD_DOMAINS = [
    'trafficjunky.net', 'trafficjunky.com', 'exoclick.com', 'exosrv.com', 'exdynsrv.com', 'pemsrv.com',
    'juicyads.com', 'tsyndicate.com', 'trafficstars.com', 'realsrv.com', 'magsrv.com',
    'popads.net', 'popcash.net', 'propellerads.com', 'adsterra.com', 'clickadu.com',
    'hilltopads.net', 'ero-advertising.com', 'a-ads.com', 'doubleclick.net',
    'googlesyndication.com', 'adnxs.com',
  ];
  const AD_RE = new RegExp('(^|\\.)(' + AD_DOMAINS.map(d => d.replace(/\./g, '\\.')).join('|') + ')$');

  // 使用者自己封鎖的網域（個人黑名單）
  const userBlock = new Set(store.get('userBlock', []));

  // 有些網站的廣告是從「自己的網址」拿的（例如 Pornhub 的 /_xa/ads），由網站轉接器設定
  let siteAdPath = null;

  function isAd(url) {
    try {
      const u = new URL(url, location.href);
      if (siteAdPath && baseOf(u.hostname) === HOME && siteAdPath.test(u.pathname)) return true;
      // ExoClick 的廣告程式不管放在哪個網域，檔名都叫 ad-provider.js（XVideos 影片前廣告就是它）
      if (/\/ad-provider\.js$/.test(u.pathname)) return true;
      return AD_RE.test(u.hostname) || userBlock.has(baseOf(u.hostname));
    } catch (e) { return false; }
  }

  // ---- 格式化 ----
  function fmt(sec) {
    if (!isFinite(sec) || sec < 0) return '計算中';
    if (sec < 60) return sec.toFixed(1) + ' 秒';
    return Math.floor(sec / 60) + ' 分 ' + Math.round(sec % 60) + ' 秒';
  }

  function fmtSize(bytes) {
    if (bytes >= 1e9) return (bytes / 1e9).toFixed(2) + ' GB';
    return (bytes / 1e6).toFixed(0) + ' MB';
  }

  function parseDur(str) {
    if (!str) return 0;
    if (/^\d+$/.test(str)) return Number(str);
    const m = str.match(/PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?/);
    if (!m) return 0;
    return (+m[1] || 0) * 3600 + (+m[2] || 0) * 60 + (+m[3] || 0);
  }

  // ---- 網速（量到就存起來，所有網站共用）----
  const BITRATE = { 240: 0.5, 360: 0.8, 480: 1.5, 720: 3, 1080: 5.5, 1440: 9, 2160: 16 };

  function bitrateFor(h) {
    if (!h) return BITRATE[720];
    let best = 240;
    for (const k of Object.keys(BITRATE).map(Number)) if (h >= k) best = k;
    return BITRATE[best];
  }

  function measureSpeed() {
    const entries = performance.getEntriesByType('resource')
      .filter(e => e.transferSize > 100000 && e.responseEnd - e.responseStart > 0)
      .slice(-10);
    if (entries.length) {
      let bytes = 0, ms = 0;
      for (const e of entries) { bytes += e.transferSize; ms += e.responseEnd - e.responseStart; }
      const mbps = bytes * 8 / (ms / 1000) / 1e6;
      store.set('speed', mbps);
      return { mbps, source: '實測' };
    }
    const saved = store.get('speed', 0);
    if (saved) return { mbps: saved, source: '上次實測' };
    if (navigator.connection && navigator.connection.downlink) {
      return { mbps: navigator.connection.downlink, source: '粗估' };
    }
    return null;
  }

  function ratingOf(ratio) { return ratio >= 2 ? '🟢' : ratio >= 1.2 ? '🟡' : '🔴'; }

  // ---- 載入品質紀錄：每個網站最近 20 次「實際開始播放秒數 ÷ 預估秒數」----
  function startupSamples(site) {
    const all = store.get('vqSamples', {}) || {};
    return Array.isArray(all[site]) ? all[site] : [];
  }

  function recordStartup(ratio) {
    if (!isFinite(ratio) || ratio < 0.05 || ratio > 20) return;     // 太離譜的（例如網頁卡住）不記
    const all = store.get('vqSamples', {}) || {};
    const key = HOME;
    all[key] = [ratio, ...startupSamples(key)].slice(0, 20);
    store.set('vqSamples', all);
  }

  function startupFactor() {
    const xs = startupSamples(HOME).slice().sort((a, b) => a - b);
    if (xs.length < 3) return [null, xs.length];
    const m = xs.length % 2 ? xs[(xs.length - 1) / 2] : (xs[xs.length / 2 - 1] + xs[xs.length / 2]) / 2;
    return [Math.min(5, Math.max(0.2, m)), xs.length];
  }

  // 「開始播放要多久」的原始預估（還沒校正）：先載 5 秒影片的時間 + 0.5 秒
  function startupEstimate(height) {
    const sp = measureSpeed();
    return sp ? bitrateFor(height) * 5 / sp.mbps + 0.5 : null;
  }

  function statusLine(vq) {
    if (!vq) return '';
    const stall = vq.stalls ? `｜卡頓 ${vq.stalls} 次（共 ${(vq.stallMs / 1000).toFixed(1)} 秒）` : '';
    const text = {
      searching: '🔎 尋找影片中',
      loading: shared.adShowing ? '📺 廣告播放中' : '⏳ 影片載入中…',
      ready: '✅ 已經可以播放了',
      playing: '▶ 播放中' + stall,
      buffering: '⏳ 緩衝中…' + stall,
      paused: '⏸ 暫停' + stall,
      ended: '⏹ 播完了' + stall,
      error: '❌ 載入失敗：' + vq.error,
    }[vq.state] || '';
    return text ? `<b>${text}</b><br>` : '';
  }

  function recommend(mbps) {
    const keys = Object.keys(BITRATE).map(Number).reverse();
    for (const k of keys) if (mbps / BITRATE[k] >= 1.2) return k + 'p';
    return '240p';
  }

  // ---- 網路請求 ----
  function gmGet(url, type) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET', url, responseType: type, timeout: 30000,
        onload: r => (r.status >= 200 && r.status < 300) ? resolve(r.response) : reject(new Error('HTTP ' + r.status)),
        onerror: () => reject(new Error('連線失敗')),
        ontimeout: () => reject(new Error('逾時')),
      });
    });
  }

  async function getData(url, type) {
    try {
      const r = await fetch(url);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return type === 'text' ? await r.text() : await r.arrayBuffer();
    } catch (e) {
      return gmGet(url, type);
    }
  }

  function getSize(url) {
    return new Promise(resolve => {
      GM_xmlhttpRequest({
        method: 'HEAD', url, timeout: 10000,
        onload: r => {
          const m = (r.responseHeaders || '').match(/content-length:\s*(\d+)/i);
          resolve(m ? +m[1] : 0);
        },
        onerror: () => resolve(0),
        ontimeout: () => resolve(0),
      });
    });
  }

  // ---- 這個分頁暫停擋廣告（存在 sessionStorage：只對這個分頁有效，關掉分頁就恢復）----
  const PAUSABLE = ['security', 'siteRules', 'saveData', 'hideAds', 'autoSkip'];
  const PAUSED = (() => { try { return sessionStorage.getItem('vh_paused') === '1'; } catch (e) { return false; } })();
  function setPaused(on) {
    try { if (on) sessionStorage.setItem('vh_paused', '1'); else sessionStorage.removeItem('vh_paused'); } catch (e) {}
    location.reload();
  }

  // ---- 攔截紀錄：每次擋了什麼、為什麼擋（最近 50 筆，只在這個分頁，不存檔）----
  const WHY = [
    ['擋下彈出視窗', '網頁想偷開新分頁或彈出視窗，開到別的網站（常趁你按播放時一起開）'],
    ['擋下危險檔案下載', '安裝檔（.exe、.apk 等）常是病毒；看影片不需要安裝任何程式'],
    ['擋下廣告連結', '連結會開新分頁到別的網站，是常見的廣告手法'],
    ['擋下隱藏的彈窗連結', '網頁做了看不見的連結再自己點它，是彈窗廣告的手法'],
    ['擋下自動跳轉', '網頁想把你帶去別的網站，但不是你點的'],
    ['擋下通知權限請求', '「允許通知」常被拿來推送垃圾訊息和假的中毒警告'],
    ['擋下透明廣告點擊層', '一層蓋住整個畫面的透明東西，點哪裡都會開廣告'],
  ];
  const whyOf = msg => (WHY.find(([k]) => msg.startsWith(k)) || [null, ''])[1];
  const hostOf = url => { try { return new URL(url, location.href).hostname; } catch (e) { return ''; } };
  const events = [];
  function logEvent(what, why, url) {
    const host = url ? hostOf(url) : '';
    const last = events[0];
    if (last && last.what === what && last.host === host) {          // 同一件事連續發生 → 合併成一筆
      last.n++; last.time = new Date().toLocaleTimeString();
      return;
    }
    events.unshift({ what, why, host, url: url ? String(url).split('?')[0].slice(0, 200) : '', n: 1,
                     time: new Date().toLocaleTimeString() });
    events.length = Math.min(events.length, 50);
  }

  // 「這是誤擋」→ 複製一份回報（不含網址後面的參數）
  function reportWrong(ev) {
    const text = JSON.stringify({
      type: '誤擋回報', version: (typeof GM_info !== 'undefined' && GM_info.script) ? GM_info.script.version : '?',
      site: (typeof site !== 'undefined' && site) ? site.name : HOST, page: location.href.split('?')[0],
      event: ev, recent: events.slice(0, 10),
    }, null, 1);
    navigator.clipboard.writeText(text).then(
      () => alert('✅ 誤擋回報已複製，貼給開發者就好。\n\n要先看影片的話，可以按「⏸ 這個分頁先暫停擋廣告」。'),
      () => prompt('請複製下面的內容：', text));
  }

  // ---- 左下角盾牌 + 提示 ----
  const ui = {
    count: 0, saved: 0, blockedHosts: {}, actions: {}, shield: null, toasts: null, pending: [], realOpen: null,

    report(msg, url) {
      logEvent(msg, whyOf(msg), url);
      this.count++;
      this.pending.push([msg, url]);
      this.flush();
    },

    flush() {
      if (!document.body || !IS_TOP) return;
      if (!this.shield) {
        this.shield = document.createElement('div');
        this.shield.className = 'vh-ui';
        this.shield.style.cssText = 'position:fixed;bottom:20px;left:20px;z-index:2147483647;background:rgba(0,120,60,.9);' +
          'color:#fff;padding:6px 12px;border-radius:16px;font:bold 13px sans-serif;cursor:pointer;';
        this.shield.title = '點一下：查看這頁連到的其他網站';
        this.shield.addEventListener('click', () => this.openPanel());
        this.toasts = document.createElement('div');
        this.toasts.className = 'vh-ui';
        this.toasts.style.cssText = 'position:fixed;bottom:60px;left:20px;z-index:2147483647;display:flex;' +
          'flex-direction:column-reverse;gap:6px;max-width:360px;';
        document.body.append(this.shield, this.toasts);
      }
      this.shield.textContent = PAUSED ? '⏸ 這個分頁暫停擋廣告中（點這裡恢復）'
        : `🛡 已攔截 ${this.count} 次` + (this.saved ? `｜省下 ${this.saved} 個廣告請求` : '');
      if (PAUSED) this.shield.style.background = 'rgba(180,120,0,.95)';
      while (this.pending.length) this.toast(...this.pending.shift());
    },

    // ---- 收集器：列出這頁連到的其他網站 ----
    openPanel() {
      if (this.panel) {
        const wasList = this.panel.dataset.kind === 'list';
        this.panel.remove(); this.panel = null;
        if (wasList) return;   // 再點一次盾牌 = 關閉清單
      }

      // 統計每個網域連了幾次（實際載入的 + 被擋的）
      const stat = {};
      for (const e of performance.getEntriesByType('resource')) {
        let h; try { h = new URL(e.name).hostname; } catch (err) { continue; }
        const b = baseOf(h);
        if (b === HOME) continue;
        stat[b] = stat[b] || { n: 0, blocked: 0, how: {}, hosts: {} };
        stat[b].n++;
        stat[b].hosts[h] = 1;                              // 完整網址（例如 s.pemsrv.com），拿來比對廣告清單
        const how = { img: '圖片', script: '程式', xmlhttprequest: '資料', fetch: '資料', beacon: '追蹤',
          iframe: '框框', video: '影片', audio: '聲音', css: 'CSS', link: '預先載入', other: '其他' }[e.initiatorType] || e.initiatorType;
        stat[b].how[how] = 1;
      }
      for (const [b, n] of Object.entries(this.blockedHosts)) {
        stat[b] = stat[b] || { n: 0, blocked: 0, how: {}, hosts: {} };
        stat[b].blocked += n;
      }
      const rows = Object.entries(stat).sort((a, b) => (b[1].n + b[1].blocked) - (a[1].n + a[1].blocked));

      const p = this.panel = document.createElement('div');
      p.className = 'vh-ui';
      p.dataset.kind = 'list';
      p.style.cssText = 'position:fixed;bottom:60px;left:20px;z-index:2147483647;background:rgba(20,20,20,.97);color:#fff;' +
        'padding:12px 14px;border-radius:10px;font:13px/1.6 sans-serif;width:380px;max-height:70vh;overflow-y:auto;' +
        'box-shadow:0 2px 12px rgba(0,0,0,.5);';
      if (PAUSED) {
        const pb = document.createElement('div');
        pb.style.cssText = 'margin-bottom:10px;padding:8px;background:#5d4300;border-radius:6px;';
        pb.innerHTML = '⏸ <b>這個分頁暫停擋廣告中</b><br><small>關掉分頁就會自動恢復</small> ';
        const go = document.createElement('span');
        go.textContent = '▶ 現在恢復';
        go.style.cssText = 'cursor:pointer;background:#2e7d32;padding:2px 10px;border-radius:4px;';
        go.addEventListener('click', () => setPaused(false));
        pb.appendChild(go);
        p.appendChild(pb);
      }

      // 最近攔截了什麼、為什麼
      const evBox = document.createElement('div');
      evBox.innerHTML = '<b>最近攔截</b>' + (events.length ? '' : '<div style="opacity:.7;font-size:12px">這個分頁還沒有攔截任何東西</div>');
      for (const ev of events.slice(0, 12)) {
        const row = document.createElement('div');
        row.style.cssText = 'margin-top:4px;padding:4px 8px;background:#2a2a2a;border-radius:6px;font-size:12px;';
        const head = document.createElement('div');
        head.textContent = `${ev.time}　${ev.what}${ev.n > 1 ? ` ×${ev.n}` : ''}${ev.host ? `（${ev.host}）` : ''}`;
        const why = document.createElement('div');
        why.style.cssText = 'opacity:.7;';
        why.textContent = ev.why ? '原因：' + ev.why : '';
        const wrong = document.createElement('span');
        wrong.textContent = '這是誤擋？';
        wrong.style.cssText = 'float:right;color:#8cf;text-decoration:underline;cursor:pointer;';
        wrong.addEventListener('click', () => reportWrong(ev));
        head.prepend(wrong);
        row.append(head, why);
        evBox.appendChild(row);
      }
      p.appendChild(evBox);

      const hostsTitle = document.createElement('div');
      hostsTitle.innerHTML = '<br><b>這頁連到的其他網站</b><div style="opacity:.7;font-size:12px">' +
        '影片和圖片常放在網站自己的另一個網域（例如 phncdn.com），封鎖前先確認不是它</div>';
      p.appendChild(hostsTitle);

      if (!rows.length) p.insertAdjacentHTML('beforeend', '<div style="margin-top:8px">目前沒有</div>');

      // 🤖 問 AI：判斷還沒被內建規則擋的網域
      const aiTags = {};                                   // 網域 → 顯示 AI 結果的那一格
      const OWN = new Set((site && site.ownDomains) || []);
      const unknown = rows.map(([b]) => b).filter(b => !AD_RE.test(b) && !OWN.has(b)).slice(0, 30);
      if (unknown.length) {
        const aiBar = document.createElement('div');
        aiBar.style.cssText = 'margin-top:6px;font-size:12px;';
        const aiBtn = document.createElement('span');
        aiBtn.textContent = `🔍 檢查這 ${unknown.length} 個是不是廣告`;
        aiBtn.title = '用電腦版裡的公開廣告清單比對（免費）；有設定 AI 金鑰才會再問 AI。只給建議，封不封鎖你決定';
        aiBtn.style.cssText = 'cursor:pointer;background:#6a1b9a;padding:3px 10px;border-radius:4px;';
        const aiMsg = document.createElement('span');
        aiMsg.style.cssText = 'margin-left:8px;opacity:.8;';
        aiBtn.addEventListener('click', async () => {
          if (aiBtn.dataset.busy) return;
          aiBtn.dataset.busy = '1';
          aiMsg.textContent = '檢查中…（第一次要下載清單，約幾秒）';
          try {
            const hostMap = {};
            for (const d of unknown) hostMap[d] = Object.keys(stat[d].hosts || {}).slice(0, 20);
            const res = await askAi(unknown, hostMap);
            let n = 0;
            for (const [d, r] of Object.entries(res)) {
              const tag = aiTags[d];
              if (!tag) continue;
              tag.textContent = (r.block ? '⚠ ' : r.label === '不確定' ? '？ ' : '✓ ') + r.label + (r.reason ? '：' + r.reason : '');
              tag.style.color = r.block ? '#ff8a80' : r.label === '不確定' ? '#bbb' : '#a5d6a7';
              if (r.block) n++;
            }
            aiMsg.textContent = n ? `建議封鎖 ${n} 個（紅字），確認後按「封鎖」` : '沒有找到已知的廣告';
          } catch (err) {
            aiMsg.textContent = '❌ ' + err.message;
          }
          delete aiBtn.dataset.busy;
        });
        aiBar.append(aiBtn, aiMsg);
        p.appendChild(aiBar);
      }

      for (const [b, st] of rows) {
        const builtIn = AD_RE.test(b);
        const mine = userBlock.has(b);
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:8px;margin-top:6px;padding:4px 8px;background:#333;border-radius:6px;';
        const name = document.createElement('div');
        name.style.cssText = 'flex:1;min-width:0;';
        const dom = document.createElement('div');
        dom.style.cssText = 'font-weight:bold;word-break:break-all;';
        dom.textContent = b;
        const tag = aiTags[b] = document.createElement('div');   // 「🔍 檢查」的結果會填在這裡
        tag.style.cssText = 'font-size:11px;';
        if (OWN.has(b)) {
          tag.textContent = '✓ 網站自己的影片／圖片伺服器（封鎖了影片會播不了）';
          tag.style.color = '#a5d6a7';
        }
        const info = document.createElement('div');
        info.style.cssText = 'font-size:11px;opacity:.6;';
        name.append(dom, tag, info);
        const hows = Object.keys(st.how).join('、');
        info.textContent = (st.blocked ? `擋 ${st.blocked}` : '') + (st.blocked && st.n ? '｜' : '') +
          (st.n ? `載入 ${st.n}${hows ? '（' + hows + '）' : ''}` : '');
        const btn = document.createElement('span');
        btn.style.cssText = 'cursor:pointer;padding:2px 8px;border-radius:4px;font-size:12px;white-space:nowrap;';
        if (builtIn) {
          btn.textContent = '內建已擋';
          btn.style.background = '#2e7d32';
          btn.style.cursor = 'default';
        } else {
          btn.textContent = mine ? '解除封鎖' : '封鎖';
          btn.style.background = mine ? '#555' : OWN.has(b) ? '#555' : '#c62828';
          btn.addEventListener('click', () => {
            if (!userBlock.has(b) && OWN.has(b) &&
                !confirm(`${b} 是這個網站自己的影片／圖片伺服器，封鎖後影片可能播不了。\n確定要封鎖嗎？`)) return;
            if (userBlock.has(b)) userBlock.delete(b); else userBlock.add(b);
            store.set('userBlock', [...userBlock]);
            btn.textContent = userBlock.has(b) ? '解除封鎖' : '封鎖';
            btn.style.background = userBlock.has(b) ? '#555' : '#c62828';
          });
        }
        row.append(name, btn);
        p.appendChild(row);
      }

      const foot = document.createElement('div');
      foot.style.cssText = 'margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;';
      const copy = document.createElement('span');
      copy.textContent = '📋 複製我的黑名單';
      copy.style.cssText = 'cursor:pointer;background:#1565c0;padding:3px 10px;border-radius:4px;';
      copy.addEventListener('click', () => {
        const text = [...userBlock].join('\n') || '（黑名單是空的）';
        navigator.clipboard.writeText(text).then(() => { copy.textContent = '✅ 已複製'; }, () => { prompt('複製下面的內容：', text); });
      });
      const note = document.createElement('div');
      note.style.cssText = 'font-size:12px;opacity:.7;width:100%;';
      note.textContent = `我的黑名單：${userBlock.size} 個網域｜封鎖後重新整理網頁生效`;
      const mkBtn = (text, color, fn) => {
        const b = document.createElement('span');
        b.textContent = text;
        b.style.cssText = `cursor:pointer;background:${color};padding:3px 10px;border-radius:4px;`;
        b.addEventListener('click', fn);
        return b;
      };
      const settingsBtn = mkBtn('⚙ 功能開關', '#555', () => this.actions.settings && this.actions.settings());
      const diagBtn = mkBtn('🔧 診斷', '#6a1b9a', () => this.actions.diagnose && this.actions.diagnose());
      const healthBtn = mkBtn('🩺 狀態檢查', '#00695c', () => this.actions.health && this.actions.health());
      const pauseBtn = PAUSED
        ? mkBtn('▶ 恢復擋廣告', '#2e7d32', () => setPaused(false))
        : mkBtn('⏸ 這個分頁先暫停擋廣告', '#8d6e00', () => {
          if (confirm('這個分頁先暫停擋廣告、擋彈窗嗎？\n\n網頁會重新整理。關掉這個分頁就會自動恢復，其他分頁不受影響。')) setPaused(true);
        });
      foot.append(copy, settingsBtn, healthBtn, diagBtn, pauseBtn, note);
      p.appendChild(foot);

      document.body.appendChild(p);
    },

    toast(msg, url) {
      const t = document.createElement('div');
      t.style.cssText = 'background:rgba(20,20,20,.95);color:#fff;padding:8px 12px;border-radius:8px;' +
        'font:13px/1.5 sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.4);word-break:break-all;';
      const line = document.createElement('div');
      line.textContent = '🛡 ' + msg;
      t.appendChild(line);
      if (url) {
        const host = document.createElement('div');
        host.style.cssText = 'opacity:.7;font-size:12px;';
        try { host.textContent = new URL(url, location.href).hostname; } catch (e) {}
        const go = document.createElement('span');
        go.textContent = '我確定要開啟';
        go.style.cssText = 'color:#8cf;text-decoration:underline;cursor:pointer;font-size:12px;';
        go.addEventListener('click', () => { t.remove(); if (ui.realOpen) ui.realOpen.call(PW, url, '_blank', 'noopener'); });
        t.append(host, go);
      }
      this.toasts.appendChild(t);
      setTimeout(() => t.remove(), 7000);
    },
  };

  /* ==================================================================
   * 第二層：網站轉接器
   * ================================================================== */

  const ALL = ['security', 'saveData', 'autoSkip', 'videoStatus', 'estimator', 'listLabels', 'download'];

  // 只擋廣告、不下載的網站共用的功能清單
  const ADBLOCK_ONLY = ['security', 'saveData', 'hideAds', 'autoSkip', 'videoStatus', 'estimator', 'listLabels'];
  const TUBE_LIMITS = ['不提供下載', '剛加入，還沒有這個網站專屬的擋廣告規則；有漏掉的廣告請回報'];

  const SITES = [
    {
      name: 'Pornhub',
      match: /(^|\.)pornhub\.com$/,
      features: ALL,
      // 影片前廣告清單、側邊廣告都是跟 Pornhub 自己的 /_xa/ads… 要的
      adPath: /^\/_xa\/ads/,
      // 網站自己的影片、圖片伺服器：封鎖了網站就壞掉
      ownDomains: ['phncdn.com'],
      // 以下參考 uBlock Origin 的規則庫（uBlockOrigin/uAssets），用自己的程式碼重新實作
      rules: {
        // uBlock：pornhub.*##.watchpageAd 等 → 藏起廣告區塊
        hide: ['.watchpageAd', '.middleAdContainer', '.mgp_overlayContainer', '#pb_block', '.js-adsRemoveButton',
          '.ad-tabSplit', '#js-abContainterMain', '.emptyBlockSpace', '#relatedVideosCenter > .wrapVideoBlock',
          'a[href^="http://ads.trafficjunky.net/"]', '.livesex', '.realsex', '.joinNowCPPBtn', '.js_joinNowCPPBtn',
          'div[id^="customSkin"]'],
        // uBlock：+js(set, abp1, 1) → 設定一個值，讓網站改走比較少廣告的版本
        setGlobals: { abp1: 1 },
        // uBlock：+js(acs, Object.defineProperty, trafficjunky)、+js(rmnt, script, /interstitial/)
        //   → 網頁裡的程式，同一組的字「全部都有」才不讓它執行（條件嚴格，避免誤傷正常程式）
        blockInlineScript: [[/trafficjunky/, /Object\.defineProperty/], [/\/interstitial\?/]],
        // uBlock：+js(nostif, adsbytrafficjunkycontext) → 取消「幾秒後載入廣告」的計時器
        blockTimeout: [/adsbytrafficjunkycontext/],
        // 影片前廣告的設定改名，播放器就找不到（概念同 uBlock 的 json-prune）
        renameKeys: ['adRollGlobalConfig'],
      },
      isVideoPage: () => location.pathname.includes('view_video'),
      findSources: findPhSources,
      limits: ['寫在網頁最開頭的廣告程式仍會被瀏覽器先下載（腳本只能讓它不執行）',
               'Pornhub 改版時，網站專屬規則可能失效（盾牌數字突然變很少就是徵兆）'],
    },
    {
      name: 'XVideos',
      match: /(^|\.)xvideos\.com$/,
      features: ALL,
      // uBlock：||xvideos.com/lvcsm/abck-banners/
      adPath: /^\/lvcsm\/abck-banners\//,
      ownDomains: ['xvideos-cdn.com'],
      rules: {
        // uBlock：xvideos.com##a[href*="/go.php?"][href*="&ad="]
        hide: ['a[href*="/go.php?"][href*="&ad="]'],
        // uBlock：+js(aeld, click, ShouldShow) → 不讓「點哪裡都開廣告」的監聽器裝上去
        blockListeners: [{ type: 'click', match: /ShouldShow/ }],
      },
      isVideoPage: () => /setVideo(UrlHigh|UrlLow|HLS)/.test(document.documentElement.innerHTML),
      findSources: findXvSources,
      limits: ['720p 以上是切片，在瀏覽器裡下載完要轉 MP4，大檔建議用「🖥 送到電腦版」'],
    },
    {
      name: 'XNXX',
      match: /(^|\.)xnxx\.com$/,
      features: ALL,
      // 跟 XVideos 同一家公司，播放器寫法一樣
      ownDomains: ['xnxx-cdn.com', 'xvideos-cdn.com'],
      rules: { blockListeners: [{ type: 'click', match: /ShouldShow/ }] },
      isVideoPage: () => /setVideo(UrlHigh|UrlLow|HLS)/.test(document.documentElement.innerHTML),
      findSources: findXvSources,
      limits: ['剛加入；跟 XVideos 用同一套方法，有問題請回報'],
    },
    {
      name: 'xHamster',
      match: /(^|\.)xhamster\d*\.(com|desi|one)$/,
      features: ALL,
      ownDomains: ['xhcdn.com'],
      isVideoPage: () => /\/videos\//.test(location.pathname) || !!(PW.initials && PW.initials.xplayerSettings),
      findSources: findXhSources,
      limits: ['剛加入下載；網址被加密時只能「交給電腦版」下載'],
    },
    {
      name: 'YouPorn',
      match: /(^|\.)youporn\.com$/,
      features: ALL,
      ownDomains: ['ypncdn.com'],
      isVideoPage: () => /mediaDefinition/.test(document.documentElement.innerHTML),
      findSources: findAyloSources,
      limits: ['剛加入；跟 Pornhub 同一家公司，有問題請回報'],
    },
    {
      name: 'RedTube',
      match: /(^|\.)redtube\.com$/,
      features: ALL,
      ownDomains: ['rdtcdn.com'],
      isVideoPage: () => /mediaDefinition/.test(document.documentElement.innerHTML),
      findSources: findAyloSources,
      limits: ['剛加入；跟 Pornhub 同一家公司，有問題請回報'],
    },
    // 盜版轉貼站：只開防護和隱藏廣告
    // 不開下載：內容多為片商作品的轉載
    { name: 'Jable', match: /(^|\.)jable\.tv$/,
      features: ['security', 'saveData', 'hideAds', 'autoSkip', 'videoStatus', 'estimator', 'listLabels'],
      limits: ['不提供下載（內容多為片商作品的轉載）'] },
    // MissAV：按播放時會開 /pop?url=廣告商 的新分頁
    // 不開下載：內容多為片商作品的轉載
    { name: 'MissAV', match: /(^|\.)missav[^.]*\.[a-z]+$/, adPath: /^\/pop/,
      features: ['security', 'saveData', 'hideAds', 'autoSkip', 'videoStatus', 'estimator', 'listLabels'],
      limits: ['不提供下載（內容多為片商作品的轉載）', 'MissAV 常換網址；換到不是 missav 開頭的網址時腳本不會啟動'] },
    // 其他影片網站（參考 ThePornDude 的影片網站排行）：只開擋廣告、預估時間，不開下載
    //   這些網站很多影片是片商作品的轉載；xHamster 之後測試穩定再考慮加下載
    { name: 'Eporner', match: /(^|\.)eporner\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    { name: 'HQporner', match: /(^|\.)hqporner\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    { name: 'Beeg', match: /(^|\.)beeg\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    { name: 'YourPorn', match: /(^|\.)sxyprn\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    { name: 'SpankBang', match: /(^|\.)spankbang\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    { name: 'XMoviesForYou', match: /(^|\.)xmoviesforyou\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    { name: 'PornTrex', match: /(^|\.)porntrex\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    { name: 'YouJizz', match: /(^|\.)youjizz\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    { name: 'PornOne', match: /(^|\.)(pornone|vporn)\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    { name: '3Movs', match: /(^|\.)3movs\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    { name: 'Tube8', match: /(^|\.)tube8\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    { name: 'PornDig', match: /(^|\.)porndig\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    { name: 'CumLouder', match: /(^|\.)cumlouder\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    { name: 'TXXX', match: /(^|\.)txxx\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    { name: 'PornDoe', match: /(^|\.)porndoe\.com$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },
    // Hanime1：動畫轉貼站
    // 不開下載：內容多為動畫公司作品的轉載
    { name: 'Hanime1', match: /(^|\.)hanime1\.me$/,
      features: ['security', 'saveData', 'hideAds', 'autoSkip', 'videoStatus', 'estimator', 'listLabels'],
      limits: ['不提供下載（內容多為動畫公司作品的轉載）', '剛加入，還沒有這個網站專屬的擋廣告規則；有漏掉的廣告請回報'] },
  ];

  const site = SITES.find(s => s.match.test(HOST));
  if (!site) return;
  siteAdPath = site.adPath || null;
  if (site.rules) site.features = ['siteRules', ...site.features];

  // ---- XVideos：影片網址藏在網頁原始碼 ----
  async function findXvSources() {
    const html = document.documentElement.innerHTML;
    const pick = re => { const m = html.match(re); return m ? m[1].replace(/\\\//g, '/') : null; };
    const title = pick(/setVideoTitle\(\s*'([^']*)'/) || document.title;
    const list = [];

    const hls = pick(/setVideoHLS\(\s*'([^']+)'/);
    if (hls) {
      try {
        for (const v of parseMaster(await getData(hls, 'text'), hls)) {
          list.push({ label: v.height + 'p', height: v.height, kind: 'hls', url: v.url, bw: v.bw });
        }
      } catch (e) {}
    }
    const high = pick(/setVideoUrlHigh\(\s*'([^']+)'/);
    const low = pick(/setVideoUrlLow\(\s*'([^']+)'/);
    if (high) list.push({ label: '高畫質（約 360～480p）', height: 480, kind: 'mp4', url: high });
    if (low) list.push({ label: '低畫質（約 240p）', height: 240, kind: 'mp4', url: low });

    list.sort((a, b) => b.height - a.height);
    return { title, list };
  }

  // ---- YouPorn、RedTube（跟 Pornhub 同一家 Aylo）：影片清單寫在網頁裡的 mediaDefinition(s) ----
  // 從網頁原始碼找出 "mediaDefinitions": [ … ] 這一段，括號配對後當 JSON 讀
  function readJsonArrayAfter(html, keyRe) {
    const m = keyRe.exec(html);
    if (!m) return null;
    let i = html.indexOf('[', m.index + m[0].length - 1), depth = 0, inStr = false, esc = false;
    if (i < 0) return null;
    for (let j = i; j < html.length && j < i + 200000; j++) {
      const c = html[j];
      if (inStr) { if (esc) esc = false; else if (c === '\\') esc = true; else if (c === '"') inStr = false; continue; }
      if (c === '"') inStr = true;
      else if (c === '[') depth++;
      else if (c === ']' && --depth === 0) {
        try { return JSON.parse(html.slice(i, j + 1)); } catch (e) { return null; }
      }
    }
    return null;
  }

  async function findAyloSources() {
    const html = document.documentElement.innerHTML;
    const og = document.querySelector('meta[property="og:title"]');
    const title = (og && og.content) || document.title;
    const defs = readJsonArrayAfter(html, /["']?mediaDefinitions?["']?\s*[:=]\s*/g) || [];
    const list = [];
    // MP4：清單裡的網址要再問一次，才拿到各畫質（跟 Pornhub 一樣）
    for (const d of defs.filter(d => d && d.format === 'mp4' && d.videoUrl)) {
      try {
        const arr = JSON.parse(await getData(new URL(d.videoUrl, location.href).href, 'text'));
        for (const x of Array.isArray(arr) ? arr : []) {
          const h = +x.quality;
          if (x.videoUrl && h && !list.some(y => y.height === h)) list.push({ label: h + 'p', height: h, kind: 'mp4', url: x.videoUrl });
        }
      } catch (e) {}
      break;
    }
    for (const d of defs) {
      if (!d || d.format !== 'hls' || !d.videoUrl) continue;
      const h = +(Array.isArray(d.quality) ? d.quality[0] : d.quality);
      if (!h || list.some(x => x.height === h)) continue;
      list.push({ label: h + 'p', height: h, kind: 'hls', url: new URL(d.videoUrl, location.href).href });
    }
    list.sort((a, b) => b.height - a.height);
    return { title, list };
  }

  // ---- xHamster：影片資訊在網頁的 window.initials 裡（xplayerSettings.sources）----
  // 網址有時會被網站加密；瀏覽器讀不出來時，就給一行「交給電腦版找畫質」（電腦版的 yt-dlp 會處理）
  async function findXhSources() {
    const ini = PW.initials || {};
    const vm = ini.videoModel || {};
    const title = vm.title || (document.querySelector('meta[property="og:title"]') || {}).content || document.title;
    const srcs = (ini.xplayerSettings && ini.xplayerSettings.sources) || {};
    const isUrl = u => typeof u === 'string' && /^(https?:)?\/\//.test(u);
    const list = [];
    const hls = srcs.hls && (srcs.hls.h264 || srcs.hls);
    const hlsUrl = hls && (hls.url || hls.fallback);
    if (isUrl(hlsUrl)) {
      try {
        const u = new URL(hlsUrl, location.href).href;
        for (const v of parseMaster(await getData(u, 'text'), u)) {
          if (v.height && !list.some(x => x.height === v.height)) list.push({ label: v.height + 'p', height: v.height, kind: 'hls', url: v.url, bw: v.bw });
        }
      } catch (e) {}
    }
    const std = (srcs.standard && (srcs.standard.h264 || srcs.standard.mp4)) || [];
    for (const x of Array.isArray(std) ? std : []) {
      const h = parseInt(x.quality || x.label, 10);
      const u = x.url || x.fallback;
      if (h && isUrl(u) && !list.some(y => y.height === h)) list.push({ label: h + 'p', height: h, kind: 'mp4', url: new URL(u, location.href).href });
    }
    list.sort((a, b) => b.height - a.height);
    if (!list.length && (vm.id || ini.xplayerSettings)) {
      list.push({ label: '交給電腦版找畫質', height: 0, kind: 'page', url: '' });
    }
    return { title, list };
  }

  // ---- Pornhub：影片網址在 flashvars 變數裡 ----
  async function findPhSources() {
    const key = Object.keys(PW).find(k => k.startsWith('flashvars_'));
    if (!key) return { title: document.title, list: [] };
    const fv = PW[key];
    const title = fv.video_title || document.title;
    const defs = fv.mediaDefinitions || [];
    const list = [];

    const mp4Def = defs.find(d => d.format === 'mp4' && d.videoUrl);
    if (mp4Def) {
      try {
        const arr = JSON.parse(await getData(mp4Def.videoUrl, 'text'));
        for (const d of arr) {
          if (d.videoUrl) list.push({ label: d.quality + 'p', height: +d.quality, kind: 'mp4', url: d.videoUrl });
        }
      } catch (e) {}
    }
    for (const d of defs) {
      if (d.format !== 'hls' || !d.videoUrl) continue;
      const h = +(Array.isArray(d.quality) ? d.quality[0] : d.quality);
      if (!h || list.some(x => x.height === h)) continue;
      list.push({ label: h + 'p', height: h, kind: 'hls', url: d.videoUrl });
    }

    list.sort((a, b) => b.height - a.height);
    return { title, list };
  }

  /* ==================================================================
   * 第三層：功能模組
   *   early: true → 網頁一開始就啟動（要比廣告程式早）
   *   其他的 → 網頁載入完成後才啟動，而且只在主頁面（不在框框裡）
   * ================================================================== */

  const shared = { adShowing: false };

  const MODULES = {

    /* ---------- 安全防護 ---------- */
    security: {
      label: '安全防護（擋彈窗、跳轉、危險下載）',
      early: true,
      run() {
        // 1. 擋彈出視窗、偷開新分頁
        try {
          ui.realOpen = PW.open;
          PW.open = function (url, ...rest) {
            const u = url ? String(url) : '';
            if (!u || u === 'about:blank' || isAdTarget(u)) { ui.report('擋下彈出視窗', u ? unwrap(u) : null); return null; }
            if (isDangerous(u)) { ui.report('擋下危險檔案下載', null); return null; }
            return ui.realOpen.call(PW, url, ...rest);
          };
        } catch (e) {}

        // 2. 擋廣告連結和危險檔案
        document.addEventListener('click', e => {
          const a = e.target && e.target.closest && e.target.closest('a[href]');
          if (!a || a.closest('.vh-ui')) return;
          const href = a.href;
          if (isDangerous(href)) {
            e.preventDefault(); e.stopImmediatePropagation();
            ui.report('擋下危險檔案下載：' + href.split('/').pop().split('?')[0], null);
          } else if ((a.target === '_blank' && isAdTarget(href)) || isAd(href)) {
            e.preventDefault(); e.stopImmediatePropagation();
            ui.report('擋下廣告連結（會開新分頁到別的網站）', unwrap(href));
          }
        }, true);

        // 2b. 「看不見的連結，用程式自己點一下」開彈窗的招數
        try {
          const realClick = PW.HTMLAnchorElement.prototype.click;
          PW.HTMLAnchorElement.prototype.click = function () {
            const href = this.href || '';
            if (!this.closest('.vh-ui') && !href.startsWith('blob:') &&
                ((this.target === '_blank' && isAdTarget(href)) || isAd(href))) {
              ui.report('擋下隱藏的彈窗連結', unwrap(href));
              return;
            }
            return realClick.call(this);
          };
        } catch (e) {}

        // 3. 擋自動跳轉
        try {
          const nav = PW.navigation;
          if (nav) nav.addEventListener('navigate', e => {
            const url = e.destination && e.destination.url;
            if (!url || !e.cancelable) return;
            if (isDangerous(url)) { e.preventDefault(); ui.report('擋下危險檔案下載', null); }
            else if (isAd(url) || (!e.userInitiated && isAdTarget(url))) { e.preventDefault(); ui.report('擋下自動跳轉', unwrap(url)); }
          });
        } catch (e) {}

        // 4. 擋「允許通知」
        try {
          if (PW.Notification) {
            PW.Notification.requestPermission = function () {
              ui.report('擋下通知權限請求', null);
              return Promise.resolve('denied');
            };
          }
        } catch (e) {}

        document.addEventListener('DOMContentLoaded', () => ui.flush());
      },
    },

    /* ---------- 網站專屬規則（參考 uBlock 規則庫）---------- */
    siteRules: {
      label: '網站專屬擋廣告規則（擋影片前廣告等）',
      early: true,
      run() {
        const R = site.rules;

        // 1. 藏起廣告區塊：網頁一開始就加上 CSS
        if (R.hide && R.hide.length) {
          const style = document.createElement('style');
          style.textContent = R.hide.join(',\n') + ' { display: none !important; }';
          // 網頁剛開始時可能連 <html> 都還沒有，等它出現再加
          const addStyle = () => {
            const root = document.head || document.documentElement;
            if (root) { root.appendChild(style); return true; }
            return false;
          };
          if (!addStyle()) {
            const mo = new MutationObserver(() => { if (addStyle()) mo.disconnect(); });
            mo.observe(document, { childList: true, subtree: true });
          }
        }

        // 2. 設定固定值（網站之後改不掉）
        for (const [k, v] of Object.entries(R.setGlobals || {})) {
          try { Object.defineProperty(PW, k, { get: () => v, set: () => {}, configurable: true }); } catch (e) {}
        }

        // 3. 取消特定的計時器
        if (R.blockTimeout && R.blockTimeout.length) {
          try {
            const realST = PW.setTimeout;
            PW.setTimeout = function (fn, ...rest) {
              let code = '';
              try { code = typeof fn === 'function' ? Function.prototype.toString.call(fn) : String(fn); } catch (e) {}
              if (R.blockTimeout.some(re => re.test(code))) return 0;
              return realST.call(this, fn, ...rest);
            };
          } catch (e) {}
        }

        // 4. 不讓特定的點擊監聽器裝上去
        if (R.blockListeners && R.blockListeners.length) {
          try {
            const realAEL = PW.EventTarget.prototype.addEventListener;
            PW.EventTarget.prototype.addEventListener = function (type, fn, ...rest) {
              if (fn && R.blockListeners.some(b => b.type === type)) {
                let code = '';
                try { code = Function.prototype.toString.call(typeof fn === 'function' ? fn : fn.handleEvent); } catch (e) {}
                if (R.blockListeners.some(b => b.type === type && b.match.test(code))) return;
              }
              return realAEL.call(this, type, fn, ...rest);
            };
          } catch (e) {}
        }

        // 5. 網頁裡直接寫的程式：執行前先檢查
        //    - 含影片設定（flashvars）的：絕對不擋，只把廣告設定改名
        //    - 含廣告關鍵字的：不讓它執行
        // 例："adRollGlobalConfig": → "vhOff_adRollGlobalConfig":
        const keyRes = (R.renameKeys || []).map(k => ({ key: k, re: new RegExp('(["\']?)' + k + '\\1(\\s*:)', 'g') }));
        const handleScript = sc => {
          if (sc.src || sc.dataset.vhChecked) return;
          sc.dataset.vhChecked = '1';
          const text = sc.textContent;
          if (!text) return;
          if (/flashvars_/.test(text)) {
            let t = text;
            for (const { key, re } of keyRes) t = t.replace(re, '$1vhOff_' + key + '$1$2');
            if (t !== text) {
              sc.textContent = t; ui.saved++; ui.blockedHosts['影片前廣告設定'] = 1;
              logEvent('拿掉影片前廣告設定', '播放器找不到廣告設定，就不會去要影片前廣告', '');
            }
            return;
          }
          if ((R.blockInlineScript || []).some(group => group.every(re => re.test(text)))) {
            sc.type = 'javascript/blocked';
            ui.saved++;
            ui.blockedHosts['網頁內的廣告程式'] = (ui.blockedHosts['網頁內的廣告程式'] || 0) + 1;
            logEvent('不讓網頁內的廣告程式執行', '符合這個網站的擋廣告規則（參考 uBlock Origin 規則庫）', '');
          }
        };
        new MutationObserver(muts => {
          for (const m of muts) for (const n of m.addedNodes) {
            if (n.tagName === 'SCRIPT') handleScript(n);
            else if (n.querySelectorAll) n.querySelectorAll('script:not([src])').forEach(handleScript);
          }
        }).observe(document.documentElement || document, { childList: true, subtree: true });

        // 6. 保險：影片設定如果是用別的方式出現的，也在前幾秒內把廣告設定拿掉
        if (keyRes.length) {
          const started = Date.now();
          const timer = setInterval(() => {
            for (const k of Object.keys(PW)) {
              if (!k.startsWith('flashvars_') || !PW[k] || typeof PW[k] !== 'object') continue;
              for (const key of R.renameKeys) {
                if (key in PW[k]) { try { delete PW[k][key]; ui.blockedHosts['影片前廣告設定'] = 1; } catch (e) {} }
              }
            }
            if (Date.now() - started > 5000) clearInterval(timer);
          }, 20);
        }
      },
    },

    /* ---------- 省流模式 ---------- */
    saveData: {
      label: '省流模式（廣告不下載，網速留給影片）',
      early: true,
      run() {
        // 盜版轉貼站：別的網站放進來的框框幾乎都是廣告，一律不載入
        const strictFrames = site.features.includes('hideAds');
        const KEEP = /(google\.com\/recaptcha|recaptcha\.net|hcaptcha\.com|challenges\.cloudflare\.com)/;
        let pendingUpdate = false;
        const saved = url => {
          try { const b = baseOf(new URL(url, location.href).hostname); ui.blockedHosts[b] = (ui.blockedHosts[b] || 0) + 1; } catch (e) {}
          let why = '已知的廣告網路';
          try {
            const u = new URL(url, location.href);
            if (userBlock.has(baseOf(u.hostname))) why = '在你的個人黑名單裡';
            else if (siteAdPath && baseOf(u.hostname) === HOME && siteAdPath.test(u.pathname)) why = '網站自己放廣告用的網址';
            else if (!AD_RE.test(u.hostname)) why = '別的網站放進來的框框（這個網站只留自己的框框）';
          } catch (e) {}
          logEvent('擋下廣告請求（還沒下載就擋掉）', why, url);
          ui.saved++;
          if (!pendingUpdate) { pendingUpdate = true; setTimeout(() => { pendingUpdate = false; ui.flush(); }, 500); }
        };

        const shouldBlock = (tag, url) =>
          isAd(url) || (tag === 'IFRAME' && strictFrames && isExternal(url) && !KEEP.test(url));

        // 1. 網頁程式設定廣告網址的那一刻就攔下（這時候還沒開始下載）
        const TAGS = ['IFRAME', 'SCRIPT', 'IMG', 'VIDEO', 'AUDIO', 'SOURCE', 'EMBED'];
        const srcsetIsAd = v => String(v).split(',').some(part => isAd(part.trim().split(/\s+/)[0]));
        const hookProp = (Ctor, prop, test) => {
          try {
            const d = Object.getOwnPropertyDescriptor(Ctor.prototype, prop);
            if (!d || !d.set) return;
            Object.defineProperty(Ctor.prototype, prop, {
              get: d.get, configurable: true, enumerable: d.enumerable,
              set(v) { if (test(this, String(v))) { saved(String(v)); return; } d.set.call(this, v); },
            });
          } catch (e) {}
        };
        hookProp(PW.HTMLIFrameElement, 'src', (el, v) => shouldBlock('IFRAME', v));
        hookProp(PW.HTMLScriptElement, 'src', (el, v) => shouldBlock('SCRIPT', v));
        hookProp(PW.HTMLImageElement, 'src', (el, v) => isAd(v));
        hookProp(PW.HTMLImageElement, 'srcset', (el, v) => srcsetIsAd(v));
        hookProp(PW.HTMLMediaElement, 'src', (el, v) => isAd(v));     // 影片前廣告常從這裡來
        hookProp(PW.HTMLSourceElement, 'src', (el, v) => isAd(v));
        hookProp(PW.HTMLSourceElement, 'srcset', (el, v) => srcsetIsAd(v));
        try {
          const realSetAttr = PW.Element.prototype.setAttribute;
          PW.Element.prototype.setAttribute = function (name, value) {
            const n = String(name).toLowerCase();
            if (TAGS.includes(this.tagName) &&
                ((n === 'src' && shouldBlock(this.tagName, String(value))) || (n === 'srcset' && srcsetIsAd(value)))) {
              saved(String(value)); return;
            }
            return realSetAttr.call(this, name, value);
          };
        } catch (e) {}

        // 1b. 網頁程式一次塞一整段 HTML 進來：先把裡面的廣告網址拿掉
        const AD_ATTR = /\s(src|srcset|data-src)\s*=\s*(["'])([^"']*)\2/gi;
        const cleanHtml = html => {
          if (typeof html !== 'string' || html.length > 500000) return html;
          return html.replace(AD_ATTR, (all, attr, q, url) => {
            if (url.split(',').some(part => isAd(part.trim().split(/\s+/)[0]))) { saved(url); return ''; }
            return all;
          });
        };
        try {
          const d = Object.getOwnPropertyDescriptor(PW.Element.prototype, 'innerHTML');
          Object.defineProperty(PW.Element.prototype, 'innerHTML', {
            get: d.get, configurable: true, enumerable: d.enumerable,
            set(v) { d.set.call(this, cleanHtml(v)); },
          });
          const realIAH = PW.Element.prototype.insertAdjacentHTML;
          PW.Element.prototype.insertAdjacentHTML = function (pos, html) { return realIAH.call(this, pos, cleanHtml(html)); };
          const realWrite = PW.document.write;
          PW.document.write = function (...a) { return realWrite.apply(this, a.map(cleanHtml)); };
        } catch (e) {}

        // 2. 網頁原始碼裡直接寫的廣告元素：一出現就拔掉
        const checkOne = el => {
          if (!el || el.nodeType !== 1 || el.closest && el.closest('.vh-ui')) return;
          const tag = el.tagName;
          if (!TAGS.includes(tag)) return;
          const url = el.getAttribute('src') || '';
          const set = el.getAttribute('srcset') || '';
          if (!(url && shouldBlock(tag, url)) && !(set && srcsetIsAd(set))) return;
          if (tag === 'SCRIPT') el.type = 'javascript/blocked';   // 不讓它執行
          el.removeAttribute('src');
          el.removeAttribute('srcset');
          el.remove();
          saved(url || set);
        };
        const observer = new MutationObserver(muts => {
          for (const m of muts) {
            if (m.type === 'attributes') { checkOne(m.target); continue; }
            for (const n of m.addedNodes) {
              checkOne(n);
              if (n.querySelectorAll) n.querySelectorAll('script[src], iframe[src], img[src], img[srcset], video[src], audio[src], source, embed[src]').forEach(checkOne);
            }
          }
        });
        observer.observe(document.documentElement || document, {
          childList: true, subtree: true, attributes: true, attributeFilter: ['src', 'srcset'],
        });

        // 3. 網頁程式偷偷跟廣告伺服器要資料：直接回空的
        try {
          const realFetch = PW.fetch;
          PW.fetch = function (input, ...rest) {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (isAd(url)) { saved(url); return Promise.reject(new TypeError('blocked by Video Helper')); }
            return realFetch.call(this, input, ...rest);
          };
        } catch (e) {}
        try {
          const proto = PW.XMLHttpRequest.prototype;
          const realOpen = proto.open;
          proto.open = function (method, url, ...rest) {
            if (isAd(String(url))) { saved(String(url)); return realOpen.call(this, 'GET', 'data:,', ...rest); }
            return realOpen.call(this, method, url, ...rest);
          };
        } catch (e) {}
        try {
          const realBeacon = PW.navigator.sendBeacon;
          PW.navigator.sendBeacon = function (url, ...rest) {
            if (isAd(String(url))) { saved(String(url)); return true; }
            return realBeacon.call(this, url, ...rest);
          };
        } catch (e) {}

        document.addEventListener('DOMContentLoaded', () => ui.flush());
      },
    },

    /* ---------- 隱藏廣告 ---------- */
    hideAds: {
      label: '隱藏廣告（別的網站放進來的框框、透明點擊層）',
      run() {
        // 驗證碼、登入用的框框不要藏
        const KEEP = /(google\.com\/recaptcha|recaptcha\.net|hcaptcha\.com|challenges\.cloudflare\.com)/;
        let hidden = 0;

        setInterval(() => {
          // 1. 別的網站放進來的框框（橫幅、浮動廣告）
          for (const f of document.querySelectorAll('iframe:not([data-vh])')) {
            f.dataset.vh = '1';
            const src = f.getAttribute('src') || '';
            if (!src || KEEP.test(src) || !isExternal(src)) continue;
            f.style.setProperty('display', 'none', 'important');
            hidden++;
          }

          // 2. 蓋在整個畫面上的透明層（點哪裡都會開廣告）
          for (const el of document.querySelectorAll('body > div, body > a')) {
            if (el.dataset.vh || el.classList.contains('vh-ui')) continue;
            const cs = getComputedStyle(el);
            if (cs.position !== 'fixed' && cs.position !== 'absolute') continue;
            const r = el.getBoundingClientRect();
            const covers = r.width >= innerWidth * 0.8 && r.height >= innerHeight * 0.8;
            const invisible = +cs.opacity < 0.1 || (!el.innerText.trim() && !el.querySelector('img,video,canvas'));
            if (covers && invisible && +cs.zIndex > 100) {
              el.dataset.vh = '1';
              el.style.setProperty('pointer-events', 'none', 'important');
              el.style.setProperty('display', 'none', 'important');
              ui.report('擋下透明廣告點擊層', null);
            }
          }

          if (hidden) { ui.count += hidden; hidden = 0; ui.flush(); }
        }, 1000);
      },
    },

    /* ---------- 自動略過廣告 ---------- */
    autoSkip: {
      label: '自動略過影片前廣告',
      run() {
        const skipWords = ['skip', '跳過', '跳过', '略過', '略过'];
        const adTexts = ['ad', '廣告', '广告'];

        const realClick = el => {
          ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(type => {
            el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true }));
          });
        };
        // 假的跳過按鈕：按下去會去別的網站
        const isFake = el => {
          const a = el.closest('a[href]');
          return !!(a && (isExternal(a.href) || isDangerous(a.href)));
        };

        setInterval(() => {
          let found = false;
          for (const el of document.querySelectorAll('button, div, span, a')) {
            if (el.offsetParent === null || el.closest('.vh-ui')) continue;
            const text = (el.innerText || '').trim().toLowerCase();
            if (!text || text.length > 15) continue;
            if (skipWords.some(w => text.includes(w)) && !/\d/.test(text) && !isFake(el)) realClick(el);
            if (el.children.length === 0 && adTexts.includes(text)) found = true;
          }
          shared.adShowing = found;

          if (found) {
            for (const v of document.querySelectorAll('video')) {
              if (v.duration && v.duration < 120 && v.currentTime < v.duration - 0.5) {
                v.muted = true;
                v.currentTime = v.duration;
              }
            }
          }
        }, 300);
      },
    },

    /* ---------- 影片狀態與載入品質紀錄 ----------
     * 聽播放器的事件，知道影片現在是：載入中、播放中、緩衝卡住、暫停、出錯
     * 「開始播放要多久」量的是：正片開始載入（loadstart）→ 可以開始播（canplay）
     *   這段時間跟你什麼時候按播放無關，也不含廣告
     * 量到後跟當時的預估比，記下比例（只存數字，依網站分開）；評估框用最近 20 次的中位數校正預估
     */
    videoStatus: {
      label: '影片狀態與載入品質紀錄（只存數字，可清除）',
      early: true,                 // 網頁一開始就聽，才量得到正片什麼時候開始載入
      run() {
        if (!IS_TOP) return;
        const vq = shared.vq = { state: 'searching', stalls: 0, stallMs: 0, startup: null, estAtStart: null, error: '' };
        const ERR = { 1: '被中斷', 2: '網路錯誤', 3: '解碼錯誤', 4: '格式不支援或網址失效' };
        const info = new WeakMap();          // 每個 <video>：最近一次開始載入的時間
        let stallFrom = 0;
        const isMain = v => isFinite(v.duration) && v.duration > 120;

        const on = (v, type) => {
          const now = performance.now();
          const inf = info.get(v);
          if (type === 'loadstart') { inf.loadstart = now; return; }
          if (!isMain(v)) return;                             // 廣告、預覽小影片不管
          if (type === 'canplay' && vq.startup === null) {
            vq.startup = (now - inf.loadstart) / 1000;
            vq.estAtStart = startupEstimate(v.videoHeight) || shared.startEstRaw || null;
            if (vq.estAtStart) recordStartup(vq.startup / vq.estAtStart);
            if (vq.state === 'searching' || vq.state === 'loading') vq.state = v.paused ? 'ready' : 'playing';
          } else if (type === 'playing') {
            if (stallFrom) { vq.stallMs += now - stallFrom; stallFrom = 0; }
            vq.state = 'playing';
          } else if (type === 'waiting') {
            if (vq.state !== 'playing' && vq.state !== 'buffering') return;   // 還沒開始播時的等待不算卡頓
            if (!stallFrom) { stallFrom = now; vq.stalls++; }
            vq.state = 'buffering';
          } else if (type === 'pause') {
            if (!v.ended && vq.state !== 'error') vq.state = 'paused';
          } else if (type === 'ended') {
            vq.state = 'ended';
          } else if (type === 'error') {
            vq.state = 'error';
            vq.error = ERR[v.error && v.error.code] || '未知錯誤';
          }
        };

        const attach = v => {
          if (info.has(v)) return;
          // 太晚才找到（網頁已經在播了）→ 這次不記錄開始時間，避免記到錯的數字
          const late = v.readyState >= 3;
          info.set(v, { loadstart: performance.now() });
          for (const t of ['loadstart', 'canplay', 'playing', 'waiting', 'pause', 'ended', 'error']) {
            v.addEventListener(t, () => on(v, t));
          }
          if (late && isMain(v)) {
            if (vq.startup === null) vq.startup = -1;
            vq.state = v.paused ? 'paused' : 'playing';
          }
        };

        const scan = () => {
          document.querySelectorAll('video').forEach(attach);
          if (vq.state === 'searching' && [...document.querySelectorAll('video')].some(isMain)) vq.state = 'loading';
        };
        new MutationObserver(scan).observe(document, { childList: true, subtree: true });
        scan();
        setInterval(scan, 1000);
      },
    },

    /* ---------- 影片頁評估框 ---------- */
    estimator: {
      label: '載入時間評估框（右上角）',
      run() {
        const getMain = () => {
          const v = [...document.querySelectorAll('video')].find(x => isFinite(x.duration) && x.duration > 120);
          if (v) return { sec: v.duration, height: v.videoHeight, fromPage: false };
          const meta = document.querySelector(
            'meta[property="video:duration"], meta[property="og:duration"], meta[itemprop="duration"]');
          if (meta) {
            const s = parseDur(meta.content);
            if (s > 120) return { sec: s, height: 0, fromPage: true };
          }
          for (const sc of document.querySelectorAll('script[type="application/ld+json"]')) {
            const m = sc.textContent.match(/"duration"\s*:\s*"([^"]+)"/);
            const s = m ? parseDur(m[1]) : 0;
            if (s > 120) return { sec: s, height: 0, fromPage: true };
          }
          return null;
        };

        let collapsed = false;
        const box = document.createElement('div');
        box.className = 'vh-ui';
        box.style.cssText = 'position:fixed;top:10px;right:20px;z-index:2147483647;background:rgba(0,0,0,.8);' +
          'color:#fff;padding:8px 12px;border-radius:8px;font:14px/1.6 sans-serif;cursor:move;' +
          'white-space:nowrap;display:none;touch-action:none;user-select:none;';
        box.title = '點一下收合 / 展開；按住可以拖到別的位置（雙擊還原位置）';
        document.body.appendChild(box);

        // 位置：使用者拖過就用拖的位置；沒拖過就自動放在網站「固定在上方的選單」下面，不擋到它
        let pos = store.get('estPos', null);             // { right, top }
        const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
        const headerBottom = () => {
          let bottom = 0;
          for (const x of [PW.innerWidth - 40, PW.innerWidth - 200]) {
            for (const el of document.elementsFromPoint(x, 15)) {
              if (el === box || el.closest('.vh-ui') || el === document.body || el === document.documentElement) continue;
              let e = el;
              while (e && e !== document.body) {
                const p = getComputedStyle(e).position;
                if (p === 'fixed' || p === 'sticky') {
                  const r = e.getBoundingClientRect();
                  if (r.top <= 5 && r.bottom < 220) bottom = Math.max(bottom, r.bottom);
                  break;
                }
                e = e.parentElement;
              }
            }
          }
          return bottom;
        };
        const place = () => {
          const w = box.offsetWidth || 200, h = box.offsetHeight || 40;
          const right = pos ? pos.right : 20;
          const top = pos ? pos.top : headerBottom() + 10;
          box.style.right = clamp(right, 0, PW.innerWidth - w) + 'px';
          box.style.top = clamp(top, 0, PW.innerHeight - h) + 'px';
        };
        let drag = null;
        box.addEventListener('pointerdown', e => {
          const r = box.getBoundingClientRect();
          drag = { x: e.clientX, y: e.clientY, right: PW.innerWidth - r.right, top: r.top, moved: false };
          try { box.setPointerCapture(e.pointerId); } catch (err) {}
        });
        box.addEventListener('pointermove', e => {
          if (!drag) return;
          const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
          if (!drag.moved && Math.hypot(dx, dy) < 5) return;
          drag.moved = true;
          pos = { right: drag.right - dx, top: drag.top + dy };
          place();
        });
        box.addEventListener('pointerup', () => {
          if (!drag) return;
          if (drag.moved) store.set('estPos', pos);
          else collapsed = !collapsed;                  // 沒有拖 → 當成點一下
          drag = null;
        });
        box.addEventListener('dblclick', () => { pos = null; store.set('estPos', null); place(); });

        setInterval(() => {
          const main = getMain();
          if (!main) { box.style.display = 'none'; return; }
          box.style.display = 'block';
          place();
          if (collapsed) { box.textContent = '⏱'; return; }

          const speed = measureSpeed();
          if (!speed) { box.textContent = '量測網速中…'; return; }

          const need = bitrateFor(main.height);
          const ratio = speed.mbps / need;
          const ratingText = ratio >= 2 ? '順暢' : ratio >= 1.2 ? '可能偶爾卡' : '會卡，建議降畫質';
          const header = shared.adShowing ? '📺 廣告中，以下為預估<br>' : main.fromPage ? '📄 影片還沒開始，以下為預估<br>' : '';

          // 開始播放的預估：原始值存起來給「影片狀態」記錄；有足夠紀錄就校正
          const raw = startupEstimate(main.height) || (need * 5 / speed.mbps + 0.5);
          shared.startEstRaw = raw;
          const [f, n] = startupFactor();
          const vq = shared.vq;
          let startLine = `開始播放：約 ${fmt(raw * (f || 1))}` + (f ? `（依你過去 ${n} 次校正）` : '');
          if (vq && vq.startup !== null && vq.startup >= 0) {
            startLine = `實際開始播放：${fmt(vq.startup)}` + (vq.estAtStart ? `（預估 ${fmt(vq.estAtStart * (f || 1))}）` : '');
          }

          box.innerHTML = statusLine(vq) + header +
            `網速：${speed.mbps.toFixed(1)} Mbps（${speed.source}）<br>` +
            `影片長度：${fmt(main.sec)}<br>` +
            `畫質：${main.height ? main.height + 'p' : '以 720p 預估'}<br>` +
            `${startLine}<br>` +
            `整部載完：約 ${fmt(need * main.sec / speed.mbps)}<br>` +
            `${ratingOf(ratio)} ${ratingText}｜建議最高 ${recommend(speed.mbps)}` +
            (ui.saved ? `<br>🚫 已擋 ${ui.saved} 個廣告請求，網速留給影片` : '');
        }, 1000);
      },
    },

    /* ---------- 列表頁縮圖標示 ---------- */
    listLabels: {
      label: '列表頁縮圖顯示載入時間',
      run() {
        const parseThumbTime = t => {
          let m = t.match(/^(?:(\d+):)?(\d{1,2}):(\d{2})$/);
          if (m) return (+m[1] || 0) * 3600 + (+m[2]) * 60 + (+m[3]);
          m = t.match(/^(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?$/i);
          if (m && (m[1] || m[2])) return (+m[1] || 0) * 3600 + (+m[2] || 0) * 60;
          m = t.match(/^(\d+)\s*sec$/i);
          return m ? +m[1] : 0;
        };

        setInterval(() => {
          const speed = measureSpeed();
          if (!speed) return;
          const need = BITRATE[720];
          const ratio = speed.mbps / need;

          for (const el of document.querySelectorAll('span, div, var')) {
            if (el.dataset.vhDone || el.children.length > 0) continue;
            if (el.closest('[class*="player"], [id*="player"], .vh-ui')) continue;
            const sec = parseThumbTime((el.innerText || '').trim());
            if (sec < 60) continue;

            el.dataset.vhDone = '1';
            const fullSec = need * sec / speed.mbps;
            const tag = document.createElement('span');
            tag.textContent = ` ${ratingOf(ratio)} ${fullSec < 60 ? Math.round(fullSec) + '秒' : Math.round(fullSec / 60) + '分'}`;
            tag.style.cssText = 'font-size:11px;margin-left:4px;';
            tag.title = `以 720p、${speed.mbps.toFixed(1)} Mbps 估算整部載完時間`;
            el.appendChild(tag);
          }
        }, 1500);
      },
    },

    /* ---------- 一鍵下載 ---------- */
    download: {
      label: '一鍵下載（右下角）',
      async run() {
        if (!site.isVideoPage || !site.isVideoPage()) return;
        const src = await site.findSources();
        if (src && src.list.length) buildDownloadUI(src);
      },
    },
  };

  /* ---------- 下載模組用到的工具 ---------- */

  function safeName(s) { return s.replace(/[\\/:*?"<>|]/g, '_').trim().slice(0, 100) || 'video'; }
  function absUrl(u, base) { return new URL(u, base).href; }

  // 讀「畫質總表」
  function parseMaster(text, base) {
    const lines = text.split(/\r?\n/);
    const out = [];
    for (let i = 0; i < lines.length; i++) {
      if (!lines[i].startsWith('#EXT-X-STREAM-INF')) continue;
      const res = lines[i].match(/RESOLUTION=\d+x(\d+)/);
      const bw = lines[i].match(/BANDWIDTH=(\d+)/);
      const name = lines[i].match(/NAME="?(\d+)p?/);
      let j = i + 1;
      while (j < lines.length && (!lines[j].trim() || lines[j].startsWith('#'))) j++;
      if (j < lines.length) {
        out.push({ url: absUrl(lines[j].trim(), base), height: res ? +res[1] : (name ? +name[1] : 0), bw: bw ? +bw[1] : 0 });
      }
    }
    return out;
  }

  // 讀「片段清單」
  function parseMedia(text, base) {
    const segs = [];
    let dur = 0, init = null, encrypted = false;
    for (const raw of text.split(/\r?\n/)) {
      const l = raw.trim();
      if (l.startsWith('#EXTINF:')) dur += parseFloat(l.slice(8)) || 0;
      else if (l.startsWith('#EXT-X-MAP')) { const m = l.match(/URI="([^"]+)"/); if (m) init = absUrl(m[1], base); }
      else if (l.startsWith('#EXT-X-KEY') && !/METHOD=NONE/.test(l)) encrypted = true;
      else if (l && !l.startsWith('#')) segs.push(absUrl(l, base));
    }
    return { segs, dur, init, encrypted };
  }

  async function resolveMedia(url, bw = 0) {
    const text = await getData(url, 'text');
    if (text.includes('#EXT-X-STREAM-INF')) {
      const v = parseMaster(text, url).sort((a, b) => b.height - a.height)[0];
      return resolveMedia(v.url, v.bw || bw);
    }
    return Object.assign(parseMedia(text, url), { bw });
  }

  async function estimateSize(s) {
    if (s.kind === 'page') return { bytes: 0, exact: false };
    if (s.kind === 'mp4') return { bytes: await getSize(s.url), exact: true };
    try {
      s.media = await resolveMedia(s.url, s.bw || 0);
      const bps = s.media.bw || bitrateFor(s.height) * 1e6;
      return { bytes: bps * s.media.dur / 8, exact: false };
    } catch (e) {
      return { bytes: 0, exact: false };
    }
  }

  // 用 mux.js 把 .ts 片段轉成 .mp4（失敗就回傳 null，改存 .ts）
  /* 把「分段式 MP4」（mux.js 轉出來的、或網站本身給的 fMP4 切片）整理成「一般的 MP4」
   *
   * 分段式 MP4：檔頭幾乎是空的，影像聲音一段一段放，每段自己帶小目錄（moof）。
   *   有些播放器（例如 Windows 媒體播放器）對這種檔案很挑：長度顯示 00:00:00、不能拖、甚至打不開。
   * 一般的 MP4：檔頭（moov）有完整的目錄：每一格畫面多大、多長、哪些是關鍵畫面、放在檔案哪裡。
   *   這就是 ffmpeg 平常輸出的格式，所有播放器都能開。
   *
   * 做法：讀每一段的小目錄 → 合成一份完整目錄 → 輸出 [檔頭, 目錄, 資料區標頭, 各段資料]。
   *   影片資料本身只是「指過去」，不會再複製一份，所以大檔也不會多吃一倍記憶體。
   */
  function defragmentMp4(parts) {
    const u32 = (b, i) => ((b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]) >>> 0;
    const s32 = (b, i) => (b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3];
    const u64 = (b, i) => u32(b, i) * 4294967296 + u32(b, i + 4);
    const typeAt = (b, i) => String.fromCharCode(b[i + 4], b[i + 5], b[i + 6], b[i + 7]);
    function list(b, start, end) {
      const out = [];
      for (let i = start; i + 8 <= end;) {
        let size = u32(b, i), hdr = 8;
        if (size === 1) { size = u64(b, i + 8); hdr = 16; } else if (size === 0) size = end - i;
        if (size < hdr || i + size > end) break;
        out.push({ t: typeAt(b, i), s: i, e: i + size, c: i + hdr });
        i += size;
      }
      return out;
    }
    const child = (b, box, t) => list(b, box.c, box.e).find(x => x.t === t) || null;
    const raw = (b, box) => b.subarray(box.s, box.e);

    // ---- 讀檔頭（init）：每條軌道的基本資料 ----
    const init = parts[0];
    const moov = list(init, 0, init.length).find(x => x.t === 'moov');
    if (!moov) throw new Error('找不到 moov');
    const mvhd = child(init, moov, 'mvhd');
    const movieTs = u32(init, mvhd.c + (init[mvhd.c] === 1 ? 20 : 12));
    const tracks = new Map();
    for (const trak of list(init, moov.c, moov.e).filter(x => x.t === 'trak')) {
      const tkhd = child(init, trak, 'tkhd'), mdia = child(init, trak, 'mdia');
      const mdhd = child(init, mdia, 'mdhd'), hdlr = child(init, mdia, 'hdlr'), minf = child(init, mdia, 'minf');
      const stbl = child(init, minf, 'stbl');
      const id = u32(init, tkhd.c + (init[tkhd.c] === 1 ? 20 : 12));
      // 原本的「影音對齊」設定（edit list）：例如「影像晚 66 毫秒開始」，要原樣帶過去
      let edits = null;
      const edts = child(init, trak, 'edts'), elst = edts && child(init, edts, 'elst');
      if (elst) {
        const v1 = init[elst.c] === 1, n = u32(init, elst.c + 4);
        edits = [];
        for (let k = 0, q = elst.c + 8; k < n; k++) {
          const dur = v1 ? u64(init, q) : u32(init, q);
          const mt = v1 ? (s32(init, q + 8) * 4294967296 + u32(init, q + 12)) : s32(init, q + 4);
          q += v1 ? 16 : 8;
          edits.push({ dur, mt, rate: init.subarray(q, q + 4) });
          q += 4;
        }
      }
      tracks.set(id, {
        edits,
        id, tkhd: raw(init, tkhd), mdhd: raw(init, mdhd), hdlr: raw(init, hdlr),
        mhd: raw(init, child(init, minf, 'vmhd') || child(init, minf, 'smhd') || child(init, minf, 'nmhd')),
        dinf: child(init, minf, 'dinf') ? raw(init, child(init, minf, 'dinf')) : null,
        stsd: raw(init, child(init, stbl, 'stsd')),
        ts: u32(init, mdhd.c + (init[mdhd.c] === 1 ? 20 : 12)),
        video: typeAt(init, hdlr.c + 4) === 'vide',
        def: { dur: 0, size: 0, flags: 0 },
        sizes: [], durs: [], ctos: [], sync: [], chunks: [], firstDts: null,
      });
    }
    const mvex = child(init, moov, 'mvex');
    if (mvex) for (const trex of list(init, mvex.c, mvex.e).filter(x => x.t === 'trex')) {
      const tr = tracks.get(u32(init, trex.c + 4));
      if (tr) tr.def = { dur: u32(init, trex.c + 12), size: u32(init, trex.c + 16), flags: u32(init, trex.c + 20) };
    }

    // ---- 讀每一段的小目錄（moof）----
    const chunks = [];                                   // 依出現順序：{ track, data }
    for (let pi = 1; pi < parts.length; pi++) {
      const b = parts[pi];
      for (const moof of list(b, 0, b.length).filter(x => x.t === 'moof')) {
        let prevEnd = moof.s;
        for (const traf of list(b, moof.c, moof.e).filter(x => x.t === 'traf')) {
          const tfhd = child(b, traf, 'tfhd');
          const fl = u32(b, tfhd.c) & 0xffffff;
          const tr = tracks.get(u32(b, tfhd.c + 4));
          if (!tr) continue;
          let q = tfhd.c + 8;
          let base = (fl & 0x20000) ? moof.s : prevEnd;
          if (fl & 0x1) { base = u64(b, q); q += 8; }
          if (fl & 0x2) q += 4;
          let dDur = tr.def.dur, dSize = tr.def.size, dFlags = tr.def.flags;
          if (fl & 0x8) { dDur = u32(b, q); q += 4; }
          if (fl & 0x10) { dSize = u32(b, q); q += 4; }
          if (fl & 0x20) { dFlags = u32(b, q); q += 4; }
          const tfdt = child(b, traf, 'tfdt');
          if (tfdt && tr.firstDts === null) tr.firstDts = b[tfdt.c] === 1 ? u64(b, tfdt.c + 4) : u32(b, tfdt.c + 4);
          for (const trun of list(b, traf.c, traf.e).filter(x => x.t === 'trun')) {
            const v1 = b[trun.c] === 1, tf = u32(b, trun.c) & 0xffffff, n = u32(b, trun.c + 4);
            let r = trun.c + 8, ptr = base;
            if (tf & 0x1) { ptr = base + s32(b, r); r += 4; }
            let firstFlags = null;
            if (tf & 0x4) { firstFlags = u32(b, r); r += 4; }
            const start = ptr;
            for (let k = 0; k < n; k++) {
              const d = (tf & 0x100) ? u32(b, r) : dDur; if (tf & 0x100) r += 4;
              const sz = (tf & 0x200) ? u32(b, r) : dSize; if (tf & 0x200) r += 4;
              let sf = (tf & 0x400) ? u32(b, r) : dFlags; if (tf & 0x400) r += 4;
              if (k === 0 && firstFlags !== null) sf = firstFlags;
              const cto = (tf & 0x800) ? (v1 ? s32(b, r) : u32(b, r)) : 0; if (tf & 0x800) r += 4;
              tr.durs.push(d); tr.sizes.push(sz); tr.ctos.push(cto); tr.sync.push(!(sf & 0x10000));
              ptr += sz;
            }
            if (ptr > b.length) throw new Error('片段資料不完整');
            if (n) { const c = { track: tr, data: b.subarray(start, ptr), count: n }; tr.chunks.push(c); chunks.push(c); }
            prevEnd = ptr;
          }
        }
      }
    }
    if (!chunks.length) throw new Error('沒有影片資料');

    // ---- 寫出一般的 MP4 ----
    const enc = s => [...s].map(c => c.charCodeAt(0));
    const cat = arrs => { const n = arrs.reduce((a, x) => a + x.length, 0); const o = new Uint8Array(n); let i = 0; for (const x of arrs) { o.set(x, i); i += x.length; } return o; };
    const be32 = v => [(v >>> 24) & 255, (v >>> 16) & 255, (v >>> 8) & 255, v & 255];
    const be64 = v => [...be32(Math.floor(v / 4294967296)), ...be32(v >>> 0)];
    const box = (t, ...kids) => { const body = cat(kids.map(k => k instanceof Uint8Array ? k : new Uint8Array(k))); return cat([new Uint8Array([...be32(body.length + 8), ...enc(t)]), body]); };
    const full = (t, v, f, ...kids) => box(t, [v, (f >>> 16) & 255, (f >>> 8) & 255, f & 255], ...kids);
    const rle = arr => { const out = []; for (const x of arr) { const l = out[out.length - 1]; if (l && l[1] === x) l[0]++; else out.push([1, x]); } return out; };
    const setDur = (src, off32, off64, value) => {            // 複製一個 box 並改掉它的長度欄位
      const b = src.slice(); const v1 = b[8] === 1;
      b.fill(0, 12, v1 ? 28 : 20);                            // 建立／修改時間歸零（mux.js 寫的是 1904 年）
      if (v1) b.set(be64(value), off64); else b.set(be32(Math.min(value, 0xffffffff)), off32);
      return b;
    };

    const mdatPayload = chunks.reduce((a, c) => a + c.data.length, 0);
    const big = mdatPayload > 0xfff00000;                     // 超過 4 GB：用 64 位元的位置
    let movieDur = 0;
    const buildMoov = offsets => {
      const traks = [];
      for (const tr of tracks.values()) {
        if (!tr.sizes.length) continue;
        const mediaDur = tr.durs.reduce((a, x) => a + x, 0);
        const lead = tr.firstDts ? Math.round(tr.firstDts * movieTs / tr.ts) : 0;   // 這條軌道晚開始的時間
        // 影音對齊：原本有 edit list 就沿用（重新算長度），沒有的話用「晚開始的時間」補一段空白
        let edits = null;
        if (tr.edits && tr.edits.length) {
          edits = tr.edits.map(x => ({ ...x }));
          for (const x of edits) {
            if (x.mt >= 0 && x.dur === 0) x.dur = Math.max(0, Math.round((mediaDur - x.mt) * movieTs / tr.ts));
          }
        } else if (lead > 0) {
          edits = [{ dur: lead, mt: -1, rate: new Uint8Array([0, 1, 0, 0]) },
                   { dur: Math.round(mediaDur * movieTs / tr.ts), mt: 0, rate: new Uint8Array([0, 1, 0, 0]) }];
        }
        const trackDur = edits ? edits.reduce((a, x) => a + x.dur, 0) : Math.round(mediaDur * movieTs / tr.ts);
        movieDur = Math.max(movieDur, trackDur);
        const stbl = [tr.stsd];
        const stts = rle(tr.durs);                                                    // 每格畫面的長度（連續相同的合併）
        stbl.push(full('stts', 0, 0, [...be32(stts.length), ...stts.flatMap(([n, d]) => [...be32(n), ...be32(d)])]));
        if (tr.ctos.some(x => x !== 0)) {
          const ct = rle(tr.ctos), neg = tr.ctos.some(x => x < 0);
          stbl.push(full('ctts', neg ? 1 : 0, 0, [...be32(ct.length), ...ct.flatMap(([n, o]) => [...be32(n), ...be32(o >>> 0)])]));
        }
        if (tr.video && tr.sync.some(x => !x)) {
          const idx = []; tr.sync.forEach((x, i) => { if (x) idx.push(i + 1); });
          stbl.push(full('stss', 0, 0, [...be32(idx.length), ...idx.flatMap(be32)]));
        }
        const sc = []; tr.chunks.forEach((c, i) => { const l = sc[sc.length - 1]; if (!l || l[1] !== c.count) sc.push([i + 1, c.count]); });
        stbl.push(full('stsc', 0, 0, [...be32(sc.length), ...sc.flatMap(([f, n]) => [...be32(f), ...be32(n), ...be32(1)])]));
        stbl.push(full('stsz', 0, 0, [...be32(0), ...be32(tr.sizes.length), ...tr.sizes.flatMap(be32)]));
        const offs = tr.chunks.map(c => offsets.get(c));
        stbl.push(big ? full('co64', 0, 0, [...be32(offs.length), ...offs.flatMap(be64)])
                      : full('stco', 0, 0, [...be32(offs.length), ...offs.flatMap(be32)]));
        const minf = box('minf', tr.mhd, ...(tr.dinf ? [tr.dinf] : []), box('stbl', ...stbl));
        const mdia = box('mdia', setDur(tr.mdhd, 24, 32, mediaDur), tr.hdlr, minf);
        const parts2 = [setDur(tr.tkhd, 28, 36, trackDur)];
        if (edits) {
          parts2.push(box('edts', full('elst', 0, 0, [...be32(edits.length),
            ...edits.flatMap(x => [...be32(Math.min(x.dur, 0xffffffff)), ...be32(x.mt >>> 0), ...x.rate])])));
        }
        traks.push(box('trak', ...parts2, mdia));
      }
      const mv = init.subarray(mvhd.s, mvhd.e);
      return box('moov', setDur(mv, 24, 32, movieDur), ...traks);
    };

    const ftyp = box('ftyp', enc('isom'), be32(512), enc('isom'), enc('iso2'), enc('avc1'), enc('mp41'));
    // 先用假的位置算出目錄多大，再填真正的位置（目錄放在資料前面，播放器一打開就讀得到）
    const fake = new Map(chunks.map(c => [c, 0]));
    const moovSize = buildMoov(fake).length;
    const mdatHdr = big ? new Uint8Array([...be32(1), ...enc('mdat'), ...be64(mdatPayload + 16)])
                        : new Uint8Array([...be32(mdatPayload + 8), ...enc('mdat')]);
    let pos = ftyp.length + moovSize + mdatHdr.length;
    const real = new Map();
    for (const c of chunks) { real.set(c, pos); pos += c.data.length; }
    const moovOut = buildMoov(real);
    if (moovOut.length !== moovSize) throw new Error('目錄大小不一致');
    return [ftyp, moovOut, mdatHdr, ...chunks.map(c => c.data)];
  }

  function tsToMp4(parts) {
    const mux = typeof muxjs !== 'undefined' ? muxjs : (window.muxjs || PW.muxjs);
    if (!mux) return null;
    try {
      const out = [];
      let gotInit = false;
      const tm = new mux.mp4.Transmuxer();
      tm.on('data', seg => {
        if (!gotInit) { out.push(new Uint8Array(seg.initSegment)); gotInit = true; }
        out.push(new Uint8Array(seg.data));
      });
      for (const p of parts) { tm.push(p); tm.flush(); }
      return gotInit ? out : null;
    } catch (e) {
      return null;
    }
  }

  // 整理成一般的 MP4；萬一失敗，就存原本的分段式 MP4（VLC 一定能播）
  function toNormalMp4(parts) {
    try { return defragmentMp4(parts); }
    catch (e) { console.warn('[Video Helper] 整理 MP4 失敗，改存分段式', e); return parts; }
  }

  function saveBlob(parts, type, fileName) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob(parts, { type }));
    a.download = fileName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  }

  // 切片下載：同時抓 6 段，失敗重試 3 次
  async function downloadHls(s, baseName, onProgress, onStage) {
    const media = s.media || await resolveMedia(s.url, s.bw || 0);
    if (media.encrypted) throw new Error('這部影片有加密，目前不支援');
    if (!media.segs.length) throw new Error('找不到影片片段');

    const total = media.segs.length;
    const parts = new Array(total);
    let done = 0, next = 0;

    async function fetchSeg(i) {
      for (let t = 0; t < 3; t++) {
        try { return new Uint8Array(await getData(media.segs[i], 'arraybuffer')); }
        catch (e) {
          if (t === 2) throw new Error(`第 ${i + 1} 段下載失敗`);
          await new Promise(r => setTimeout(r, 1000));
        }
      }
    }
    async function worker() {
      while (next < total) {
        const i = next++;
        parts[i] = await fetchSeg(i);
        onProgress(++done, total);
      }
    }
    await Promise.all(Array.from({ length: 6 }, worker));

    // 片段本身就是 MP4 格式 → 直接接起來
    if (media.init) {
      parts.unshift(new Uint8Array(await getData(media.init, 'arraybuffer')));
      saveBlob(toNormalMp4(parts), 'video/mp4', baseName + '.mp4');
      return baseName + '.mp4';
    }

    // .ts 片段 → 轉成 MP4
    onStage('轉換成 MP4 中…');
    await new Promise(r => setTimeout(r, 50));
    const mp4 = tsToMp4(parts);
    if (mp4) {
      saveBlob(toNormalMp4(mp4), 'video/mp4', baseName + '.mp4');
      return baseName + '.mp4';
    }
    saveBlob(parts, 'video/mp2t', baseName + '.ts');
    return baseName + '.ts（轉 MP4 失敗，請用 VLC 播放）';
  }

  /* ---------- 電腦版連動：把影片送進電腦版的下載佇列 ----------
   * 電腦版只聽 127.0.0.1；第一次要輸入電腦版顯示的 6 位數配對碼，之後用配對拿到的密碼。
   * 用暴力猴的 GM_xmlhttpRequest 送（一般網頁的 fetch 送不過去，電腦版也會拒絕一般網頁）。
   */
  const BRIDGE_PORTS = [47321, 47322, 47323, 47324, 47325];

  function bridgeReq(method, port, path, body, token) {
    return new Promise(resolve => {
      const headers = {};
      if (body) headers['Content-Type'] = 'application/json';
      if (token) headers['X-VH-Token'] = token;
      GM_xmlhttpRequest({
        method, url: `http://127.0.0.1:${port}${path}`, headers, timeout: 3000,
        data: body ? JSON.stringify(body) : undefined,
        onload: r => { let d = {}; try { d = JSON.parse(r.responseText || '{}'); } catch (e) {} resolve({ status: r.status, data: d }); },
        onerror: () => resolve({ status: 0, data: {} }),
        ontimeout: () => resolve({ status: 0, data: {} }),
      });
    });
  }

  async function findBridge() {
    const last = store.get('bridgePort', 0);
    for (const port of [last, ...BRIDGE_PORTS.filter(p => p !== last)].filter(Boolean)) {
      const r = await bridgeReq('GET', port, '/ping');
      if (r.status === 200 && r.data.app === 'VideoHelper') { store.set('bridgePort', port); return port; }
    }
    return 0;
  }

  // 送一個請求給電腦版；還沒配對（401）就請使用者輸入配對碼，配好再送一次
  async function bridgeCall(port, path, body) {
    let r = await bridgeReq('POST', port, path, body, store.get('bridgeToken', ''));
    if (r.status === 401) {
      const code = prompt('第一次使用，請輸入電腦版顯示的 6 位數配對碼：\n（電腦版 →「③ 設定」→「🔗 配對瀏覽器…」）');
      if (!code) throw new Error('已取消配對');
      const p = await bridgeReq('POST', port, '/pair', { code: code.replace(/\s/g, '') });
      if (p.status !== 200 || !p.data.token) throw new Error(p.data.error || '配對失敗');
      store.set('bridgeToken', p.data.token);
      r = await bridgeReq('POST', port, path, body, p.data.token);
    }
    return r;
  }

  // 判斷網域是不是廣告（電腦版先查免費的公開廣告清單，有設定金鑰才再問 AI）
  // 只送網域名稱和網站名稱。hosts：每個網域實際連到的完整網址。回傳 {網域: {label, reason, block}}
  async function askAi(domains, hosts) {
    const port = await findBridge();
    if (!port) throw new Error('要先打開電腦版（廣告清單在電腦版裡）');
    const r = await bridgeCall(port, '/classify', { site: site ? site.name : HOST, domains, hosts });
    if (r.status !== 200) throw new Error(r.data.error || `電腦版回應錯誤（${r.status}）`);
    return r.data.results || {};
  }

  // 回傳一個 ticket（之後拿來查進度），失敗就丟出錯誤訊息
  // s：畫質清單裡的一項（有影片真正的位址）。一起送過去，電腦版就不用自己再開一次網頁
  //    （有些網站會擋不是瀏覽器的程式開網頁，例如 Pornhub 回 410）
  async function sendToDesktop(s, title) {
    const url = location.href.split('#')[0];
    const height = s.height || null;                 // 0（交給電腦版找畫質）→ 用電腦版的預設畫質
    const port = await findBridge();
    if (!port) {
      try { GM_setClipboard(url); } catch (e) {}
      throw new Error('找不到電腦版。請先打開 Video Helper 電腦版，並在「③ 設定」按「🔗 配對瀏覽器…」。\n（網址已經幫你複製，切到電腦版也會自動偵測）');
    }
    const media = s.url ? new URL(s.url, location.href).href : '';
    const r = await bridgeCall(port, '/enqueue',
      { url, height, media, kind: s.kind, title: String(title || '').slice(0, 300) });
    if (r.status !== 200) throw new Error(r.data.error || `電腦版回應錯誤（${r.status}）`);
    return { port, ticket: r.data.ticket };
  }

  // 每 2 秒問一次進度，顯示在 el 裡，直到結束
  function watchDesktop(el, port, ticket) {
    const icon = { '讀取中': '🔎 讀取中', '等待中': '⏳ 排隊中', '下載中': '⬇ 下載中', '完成': '✅ 完成', '失敗': '❌ 失敗', '取消': '■ 已取消' };
    let misses = 0;
    const tick = async () => {
      const r = await bridgeReq('GET', port, `/status?ticket=${ticket}`, null, store.get('bridgeToken', ''));
      if (r.status !== 200) {
        // 剛送出時電腦版可能還沒記好，或電腦版正忙；多等幾次再下結論
        if (++misses <= 5) { el.textContent = '🖥 電腦版：已送出，等電腦版回應…'; setTimeout(tick, 2000); return; }
        el.textContent = r.status === 0 ? '🖥 電腦版：連不上了（可能已關閉）'
          : r.status === 401 ? '🖥 電腦版：需要重新配對'
          : '🖥 電腦版：已送出，請到電腦版「① 下載影片」看進度';
        return;
      }
      misses = 0;
      const d = r.data;
      const prog = d.status === '下載中' ? ` ${d.progress || 0}%` : '';
      el.textContent = `🖥 電腦版：${icon[d.status] || d.status}${prog}` + (d.error ? `（${d.error}）` : '');
      if (!['完成', '失敗', '取消'].includes(d.status)) setTimeout(tick, 2000);
    };
    tick();
  }

  function buildDownloadUI(src) {
    const wrap = document.createElement('div');
    wrap.className = 'vh-ui';
    document.body.appendChild(wrap);

    const btn = document.createElement('div');
    btn.textContent = '⬇ 下載';
    btn.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:2147483647;background:#e53935;color:#fff;' +
      'padding:10px 16px;border-radius:24px;font:bold 15px sans-serif;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.4);';
    const panel = document.createElement('div');
    panel.style.cssText = 'position:fixed;bottom:70px;right:20px;z-index:2147483647;background:rgba(20,20,20,.95);' +
      'color:#fff;padding:12px 14px;border-radius:10px;font:14px/1.7 sans-serif;min-width:260px;' +
      'max-height:70vh;overflow-y:auto;display:none;';
    wrap.append(btn, panel);

    let busy = false;
    const desk = document.createElement('div');     // 顯示電腦版那邊的進度

    btn.addEventListener('click', async () => {
      if (busy || panel.style.display === 'block') {
        panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
        return;
      }
      panel.style.display = 'block';
      panel.textContent = '讀取各畫質大小中…';

      const speed = measureSpeed();
      const mbps = speed ? speed.mbps : 0;
      const results = await Promise.all(src.list.map(estimateSize));

      panel.innerHTML = '<b>選擇畫質</b>';
      src.list.forEach((s, i) => {
        const { bytes, exact } = results[i];
        const sizeText = bytes ? (exact ? '' : '約 ') + fmtSize(bytes) : '大小未知';
        const timeText = bytes && mbps ? '約 ' + fmt(bytes * 8 / (mbps * 1e6)) : '時間未知';
        const typeText = s.kind === 'hls' ? '切片' : s.kind === 'page' ? '電腦版會自動選最高畫質' : 'MP4';

        const row = document.createElement('div');
        row.style.cssText = 'margin-top:6px;padding:6px 8px;background:#333;border-radius:6px;cursor:pointer;';
        row.innerHTML = `<b>${s.label}</b> <small>（${typeText}）</small><br><small>${sizeText}｜${timeText}</small>`;
        // 「🖥」：送到電腦版下載（不在瀏覽器裡下載）
        const toPc = document.createElement('span');
        toPc.textContent = '🖥 送到電腦版';
        toPc.title = '用電腦版下載：大檔不吃瀏覽器記憶體，可以關掉網頁、在背景下載';
        toPc.style.cssText = 'float:right;margin-left:8px;padding:1px 8px;border-radius:4px;background:#1565c0;font-size:12px;';
        toPc.addEventListener('click', async e => {
          e.stopPropagation();
          desk.textContent = '🖥 送到電腦版中…';
          try {
            const { port, ticket } = await sendToDesktop(s, src.title);
            watchDesktop(desk, port, ticket);
          } catch (err) {
            desk.textContent = '🖥 ' + err.message;
          }
        });
        row.prepend(toPc);
        row.addEventListener('click', () => {
          if (s.kind === 'page') { toPc.click(); return; }   // 這行只能交給電腦版
          const big = bytes > 2e9 ? '\n\n⚠ 檔案很大，下載完成前請不要關閉這個分頁' : '';
          if (!confirm(`確定要下載嗎？\n\n${s.label}（${typeText}）\n${sizeText}｜${timeText}${big}`)) return;
          startDownload(s, bytes);
        });
        panel.appendChild(row);
      });

      const note = document.createElement('div');
      note.style.cssText = 'margin-top:8px;font-size:12px;opacity:.7;';
      note.textContent = mbps ? `以 ${mbps.toFixed(1)} Mbps（${speed.source}）估算` : '尚未量到網速，先播一下影片會比較準';
      panel.appendChild(note);
      desk.style.cssText = 'margin-top:6px;font-size:12px;white-space:pre-line;color:#9cf;';
      panel.appendChild(desk);
    });

    async function startDownload(s, bytes) {
      busy = true;
      const baseName = safeName(src.title) + '_' + s.height + 'p';
      panel.innerHTML = `下載中（請勿關閉分頁）：<br><small>${baseName}</small><br><span class="vh-prog">準備中…</span>`;
      const prog = panel.querySelector('.vh-prog');
      const t0 = Date.now();

      const ok = name => { busy = false; panel.innerHTML = `✅ 下載完成<br><small>${name}<br>檔案在「下載」資料夾</small>`; };
      const fail = msg => { busy = false; panel.innerHTML = '❌ 下載失敗：' + msg; };
      const showFrac = (frac, extra) => {
        const el = (Date.now() - t0) / 1000;
        prog.textContent = `${(frac * 100).toFixed(0)}%${extra}｜剩約 ${fmt(el / frac - el)}`;
      };

      if (s.kind === 'mp4') {
        GM_download({
          url: s.url,
          name: baseName + '.mp4',
          onprogress: e => {
            const total = e.total || bytes;
            if (!e.loaded) return;
            if (!total) { prog.textContent = '已下載 ' + fmtSize(e.loaded); return; }
            showFrac(e.loaded / total, '');
          },
          onload: () => ok(baseName + '.mp4'),
          onerror: e => fail((e && e.error) || '未知原因'),
        });
      } else {
        try {
          const name = await downloadHls(s, baseName,
            (d, total) => showFrac(d / total, `｜${d}/${total} 段`),
            text => { prog.textContent = text; });
          ok(name);
        } catch (e) {
          fail(e.message);
        }
      }
    }
  }

  /* ==================================================================
   * 啟動
   * ================================================================== */

  // 這個分頁暫停時，擋廣告相關的功能先不開（評估框、下載等照常）
  const active = site.features.filter(isOn).filter(n => !(PAUSED && PAUSABLE.includes(n)));
  if (PAUSED && IS_TOP) document.addEventListener('DOMContentLoaded', () => ui.flush());

  // 暴力猴選單：每個功能一個開關
  if (IS_TOP) {
    try {
      for (const name of site.features) {
        const on = isOn(name);
        GM_registerMenuCommand(`${on ? '✅' : '⬜'} ${MODULES[name].label}`, () => {
          settings[name] = !on;
          store.set('settings', settings);
          location.reload();
        });
      }
    } catch (e) {}
  }

  // 🩺 狀態檢查：一眼看出腳本每個部分有沒有正常運作，以及這個網站的功能和已知限制
  ui.actions.health = () => {
    if (ui.panel) { ui.panel.remove(); ui.panel = null; }
    const p = ui.panel = document.createElement('div');
    p.className = 'vh-ui';
    p.style.cssText = 'position:fixed;bottom:60px;left:20px;z-index:2147483647;background:rgba(20,20,20,.97);color:#fff;' +
      'padding:12px 14px;border-radius:10px;font:13px/1.7 sans-serif;width:400px;max-height:75vh;overflow-y:auto;' +
      'box-shadow:0 2px 12px rgba(0,0,0,.5);';
    const add = (html) => { const d = document.createElement('div'); d.innerHTML = html; p.appendChild(d); return d; };
    const esc = t => String(t).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    const ok = (cond, good, bad, warn) => cond ? `✅ ${good}` : (warn ? `⚠ ${bad}` : `❌ ${bad}`);

    let storeOk = false;
    try { const t = String(Date.now()); store.set('vh_health', t); storeOk = store.get('vh_health') === t; } catch (e) {}
    const hooked = active.includes('saveData') ? !/native code/.test(String(PW.fetch)) : true;
    const hasMux = typeof muxjs !== 'undefined' || !!(window.muxjs || PW.muxjs);
    const sp = measureSpeed();
    const ver = (typeof GM_info !== 'undefined' && GM_info.script) ? GM_info.script.version : '?';

    add(`<b>🩺 狀態檢查（${esc(site.name)}）</b>`);
    add(`腳本版本：v${esc(ver)}`);
    add(ok(hooked, '在網頁本身執行（擋廣告的功能才有效）', '沒有在網頁本身執行，擋廣告可能無效（暴力猴設定或版本問題）'));
    add(ok(storeOk, '設定可以存檔', '設定存不了，功能開關和黑名單會不見'));
    if (active.includes('download')) add(ok(hasMux, '切片轉 MP4 的工具已載入', '切片轉 MP4 的工具沒載入，下載會存成 .ts', true));
    add(sp ? `${sp.source === '實測' ? '✅' : '⚠'} 網速：${sp.mbps.toFixed(1)} Mbps（${esc(sp.source)}）` : '⚠ 還沒量到網速（播一下影片就會有）');

    add('<br><b>這個網站開啟的功能</b>');
    for (const name of site.features) add(`${isOn(name) ? '✅' : '⬜'} ${esc(MODULES[name].label)}`);
    if (site.limits && site.limits.length) {
      add('<br><b>已知限制</b>');
      for (const l of site.limits) add(`・${esc(l)}`);
    }

    const n = startupSamples(HOME).length;
    const [f] = startupFactor();
    add('<br><b>載入品質紀錄</b>');
    const rec = add(n ? `記錄了 ${n} 次「實際開始播放」的時間` + (f ? `，實際約是預估的 ${f.toFixed(2)} 倍，評估框已自動校正` : '（滿 3 次才會開始校正）')
                      : '還沒有紀錄（看影片時會自動記錄，只存數字）');
    if (n) {
      const clr = document.createElement('span');
      clr.textContent = '清除紀錄';
      clr.style.cssText = 'margin-left:8px;color:#8cf;text-decoration:underline;cursor:pointer;';
      clr.addEventListener('click', () => {
        const all = store.get('vqSamples', {}) || {};
        delete all[HOME];
        store.set('vqSamples', all);
        rec.textContent = '已清除';
      });
      rec.appendChild(clr);
    }
    const close = add('<br><span style="color:#8cf;text-decoration:underline;cursor:pointer">關閉</span>');
    close.addEventListener('click', () => { p.remove(); ui.panel = null; });
    document.body.appendChild(p);
  };

  // 網頁上的「功能開關」面板（不依賴暴力猴選單，手機也能用）
  ui.actions.settings = () => {
    if (ui.panel) { ui.panel.remove(); ui.panel = null; }
    const p = ui.panel = document.createElement('div');
    p.className = 'vh-ui';
    p.style.cssText = 'position:fixed;bottom:60px;left:20px;z-index:2147483647;background:rgba(20,20,20,.97);color:#fff;' +
      'padding:12px 14px;border-radius:10px;font:14px/1.8 sans-serif;width:360px;box-shadow:0 2px 12px rgba(0,0,0,.5);';
    p.innerHTML = `<b>功能開關（${site.name}）</b><div style="opacity:.7;font-size:12px">改完會自動重新整理網頁</div>`;
    for (const name of site.features) {
      const row = document.createElement('label');
      row.style.cssText = 'display:flex;gap:8px;align-items:center;cursor:pointer;margin-top:4px;';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = isOn(name);
      cb.addEventListener('change', () => {
        settings[name] = cb.checked;
        store.set('settings', settings);
        location.reload();
      });
      const txt = document.createElement('span');
      txt.textContent = MODULES[name].label;
      row.append(cb, txt);
      p.appendChild(row);
    }
    const close = document.createElement('div');
    close.textContent = '關閉';
    close.style.cssText = 'margin-top:8px;cursor:pointer;color:#8cf;text-decoration:underline;font-size:13px;';
    close.addEventListener('click', () => { p.remove(); ui.panel = null; });
    p.appendChild(close);
    document.body.appendChild(p);
  };

  if (IS_TOP) {
    try { GM_registerMenuCommand('📋 查看這頁連到的其他網站（封鎖廣告）', () => { ui.flush(); ui.openPanel(); }); } catch (e) {}
  }

  // ---- 診斷：背景自動記錄，使用者點一下就複製 ----
  const diag = { videos: {}, adSeen: [], startedAt: Date.now() };
  if (IS_TOP) {
    const t = () => ((Date.now() - diag.startedAt) / 1000).toFixed(1) + 's';
    const sample = () => {
      document.querySelectorAll('video').forEach((v, i) => {
        const src = v.currentSrc || v.src || '(沒有網址)';
        const key = i + '|' + src.slice(0, 200);
        if (!diag.videos[key]) diag.videos[key] = { i, src: src.slice(0, 300), firstSeen: t(), durations: [] };
        const d = isFinite(v.duration) ? Math.round(v.duration) : String(v.duration);
        const rec = diag.videos[key];
        if (rec.durations[rec.durations.length - 1] !== d) rec.durations.push(d);
      });
      if (shared.adShowing && diag.adSeen.length < 5) diag.adSeen.push(t());
    };
    setInterval(sample, 1000);

    ui.actions.diagnose = () => {
        sample();
        const fvKey = Object.keys(PW).find(k => k.startsWith('flashvars_'));
        const fv = fvKey ? PW[fvKey] : null;
        const report = {
          version: GM_info && GM_info.script && GM_info.script.version,
          manager: GM_info && GM_info.scriptHandler,
          injectInto: GM_info && GM_info.injectInto,
          pageHooked: PW.fetch !== undefined && !/native code/.test(String(PW.fetch)),
          site: site.name, page: location.href.split('?')[0],
          features: active,
          videos: Object.values(diag.videos),
          adLabelSeenAt: diag.adSeen,
          iframes: [...document.querySelectorAll('iframe')].map(f => (f.getAttribute('src') || '(空)').slice(0, 150)),
          flashvarsAdKeys: fv ? Object.keys(fv).filter(k => /ad/i.test(k)) : null,
          adRollConfig: fv && fv.adRollGlobalConfig ? JSON.stringify(fv.adRollGlobalConfig).slice(0, 1500) : null,
          blocked: ui.blockedHosts,
          paused: PAUSED,
          recentEvents: events.slice(0, 20),
          userBlockStored: store.get('userBlock', null),
          storageTest: (() => { try { const t = Date.now(); GM_setValue('vh_test', t); return GM_getValue('vh_test') === t ? 'ok' : 'mismatch'; } catch (e) { return 'error: ' + e.message; } })(),
          otherHosts: [...new Set(performance.getEntriesByType('resource').map(e => {
            try { return baseOf(new URL(e.name).hostname); } catch (er) { return ''; }
          }))].filter(h => h && h !== HOME),
        };
        const text = JSON.stringify(report, null, 1);
        navigator.clipboard.writeText(text).then(
          () => alert('✅ 診斷資料已複製，直接貼給開發者就好'),
          () => prompt('請複製下面的內容：', text));
    };
    try { GM_registerMenuCommand('🔧 診斷（複製給開發者）', ui.actions.diagnose); } catch (e) {}
  }

  // 要早啟動的（安全防護）
  for (const name of active) if (MODULES[name].early) MODULES[name].run();

  // 網頁載入完成後啟動其他功能（只在主頁面）
  function startLate() {
    if (!IS_TOP) return;
    performance.setResourceTimingBufferSize(1000);
    performance.addEventListener('resourcetimingbufferfull', () => performance.clearResourceTimings());
    for (const name of active) {
      if (MODULES[name].early) continue;
      try { MODULES[name].run(); } catch (e) { console.warn('[Video Helper]', name, e); }
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', startLate);
  else startLate();
})();
