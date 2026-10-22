// ==UserScript==
// @name         Video Helper
// @namespace    https://github.com/dadanny1122/video-helper
// @version      2.6.0
// @description  安全防護（擋彈窗、跳轉、危險下載）、省流模式（廣告不下載）、隱藏廣告、自動略過廣告、載入時間評估、一鍵下載
// @author       dadanny1122
// @match        *://*.pornhub.com/*
// @match        *://*.xvideos.com/*
// @match        *://*.jable.tv/*
// @include      /^https?:\/\/([^\/]*\.)?missav[^\/]*\//
// @require      https://cdn.jsdelivr.net/npm/mux.js@6.3.0/dist/mux.min.js
// @grant        GM_download
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        GM_info
// @grant        unsafeWindow
// @connect      *
// @run-at       document-start
// @inject-into  page
// @updateURL    https://raw.githubusercontent.com/dadanny1122/video-helper/main/video-helper.user.js
// @downloadURL  https://raw.githubusercontent.com/dadanny1122/video-helper/main/video-helper.user.js
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
    'trafficjunky.net', 'trafficjunky.com', 'exoclick.com', 'exosrv.com', 'exdynsrv.com',
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

  // ---- 左下角盾牌 + 提示 ----
  const ui = {
    count: 0, saved: 0, blockedHosts: {}, actions: {}, shield: null, toasts: null, pending: [], realOpen: null,

    report(msg, url) {
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
      this.shield.textContent = `🛡 已攔截 ${this.count} 次` + (this.saved ? `｜省下 ${this.saved} 個廣告請求` : '');
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
        stat[b] = stat[b] || { n: 0, blocked: 0, how: {} };
        stat[b].n++;
        const how = { img: '圖片', script: '程式', xmlhttprequest: '資料', fetch: '資料', beacon: '追蹤',
          iframe: '框框', video: '影片', audio: '聲音', css: 'CSS', link: '預先載入', other: '其他' }[e.initiatorType] || e.initiatorType;
        stat[b].how[how] = 1;
      }
      for (const [b, n] of Object.entries(this.blockedHosts)) {
        stat[b] = stat[b] || { n: 0, blocked: 0, how: {} };
        stat[b].blocked += n;
      }
      const rows = Object.entries(stat).sort((a, b) => (b[1].n + b[1].blocked) - (a[1].n + a[1].blocked));

      const p = this.panel = document.createElement('div');
      p.className = 'vh-ui';
      p.dataset.kind = 'list';
      p.style.cssText = 'position:fixed;bottom:60px;left:20px;z-index:2147483647;background:rgba(20,20,20,.97);color:#fff;' +
        'padding:12px 14px;border-radius:10px;font:13px/1.6 sans-serif;width:380px;max-height:70vh;overflow-y:auto;' +
        'box-shadow:0 2px 12px rgba(0,0,0,.5);';
      p.innerHTML = '<b>這頁連到的其他網站</b><div style="opacity:.7;font-size:12px">' +
        '影片和圖片常放在網站自己的另一個網域（例如 phncdn.com），封鎖前先確認不是它</div>';

      if (!rows.length) p.insertAdjacentHTML('beforeend', '<div style="margin-top:8px">目前沒有</div>');

      for (const [b, st] of rows) {
        const builtIn = AD_RE.test(b);
        const mine = userBlock.has(b);
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:8px;margin-top:6px;padding:4px 8px;background:#333;border-radius:6px;';
        const name = document.createElement('div');
        name.style.cssText = 'flex:1;word-break:break-all;';
        name.textContent = b;
        const info = document.createElement('div');
        info.style.cssText = 'font-size:11px;opacity:.75;white-space:nowrap;';
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
          btn.style.background = mine ? '#555' : '#c62828';
          btn.addEventListener('click', () => {
            if (userBlock.has(b)) userBlock.delete(b); else userBlock.add(b);
            store.set('userBlock', [...userBlock]);
            btn.textContent = userBlock.has(b) ? '解除封鎖' : '封鎖';
            btn.style.background = userBlock.has(b) ? '#555' : '#c62828';
          });
        }
        row.append(name, info, btn);
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
      foot.append(copy, settingsBtn, diagBtn, note);
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

  const ALL = ['security', 'saveData', 'autoSkip', 'estimator', 'listLabels', 'download'];

  const SITES = [
    {
      name: 'Pornhub',
      match: /(^|\.)pornhub\.com$/,
      features: ALL,
      // 影片前廣告清單、側邊廣告都是跟 Pornhub 自己的 /_xa/ads… 要的
      adPath: /^\/_xa\/ads/,
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
    },
    {
      name: 'XVideos',
      match: /(^|\.)xvideos\.com$/,
      features: ALL,
      // uBlock：||xvideos.com/lvcsm/abck-banners/
      adPath: /^\/lvcsm\/abck-banners\//,
      rules: {
        // uBlock：xvideos.com##a[href*="/go.php?"][href*="&ad="]
        hide: ['a[href*="/go.php?"][href*="&ad="]'],
        // uBlock：+js(aeld, click, ShouldShow) → 不讓「點哪裡都開廣告」的監聽器裝上去
        blockListeners: [{ type: 'click', match: /ShouldShow/ }],
      },
      isVideoPage: () => /setVideo(UrlHigh|UrlLow|HLS)/.test(document.documentElement.innerHTML),
      findSources: findXvSources,
    },
    // 盜版轉貼站：只開防護和隱藏廣告
    { name: 'Jable', match: /(^|\.)jable\.tv$/, features: ['security', 'saveData', 'hideAds'] },
    // MissAV：按播放時會開 /pop?url=廣告商 的新分頁
    { name: 'MissAV', match: /(^|\.)missav[^.]*\.[a-z]+$/, features: ['security', 'saveData', 'hideAds'], adPath: /^\/pop/ },
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
            if (t !== text) { sc.textContent = t; ui.saved++; ui.blockedHosts['影片前廣告設定'] = 1; }
            return;
          }
          if ((R.blockInlineScript || []).some(group => group.every(re => re.test(text)))) {
            sc.type = 'javascript/blocked';
            ui.saved++;
            ui.blockedHosts['網頁內的廣告程式'] = (ui.blockedHosts['網頁內的廣告程式'] || 0) + 1;
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
          'color:#fff;padding:8px 12px;border-radius:8px;font:14px/1.6 sans-serif;cursor:pointer;' +
          'white-space:nowrap;display:none;';
        box.title = '點一下收合 / 展開';
        box.addEventListener('click', () => { collapsed = !collapsed; });
        document.body.appendChild(box);

        setInterval(() => {
          const main = getMain();
          if (!main) { box.style.display = 'none'; return; }
          box.style.display = 'block';
          if (collapsed) { box.textContent = '⏱'; return; }

          const speed = measureSpeed();
          if (!speed) { box.textContent = '量測網速中…'; return; }

          const need = bitrateFor(main.height);
          const ratio = speed.mbps / need;
          const ratingText = ratio >= 2 ? '順暢' : ratio >= 1.2 ? '可能偶爾卡' : '會卡，建議降畫質';
          const header = (shared.adShowing || main.fromPage) ? '📺 廣告中，以下為預估<br>' : '';

          box.innerHTML = header +
            `網速：${speed.mbps.toFixed(1)} Mbps（${speed.source}）<br>` +
            `影片長度：${fmt(main.sec)}<br>` +
            `畫質：${main.height ? main.height + 'p' : '以 720p 預估'}<br>` +
            `開始播放：約 ${fmt(need * 5 / speed.mbps + 0.5)}<br>` +
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
      saveBlob(parts, 'video/mp4', baseName + '.mp4');
      return baseName + '.mp4';
    }

    // .ts 片段 → 轉成 MP4
    onStage('轉換成 MP4 中…');
    await new Promise(r => setTimeout(r, 50));
    const mp4 = tsToMp4(parts);
    if (mp4) {
      saveBlob(mp4, 'video/mp4', baseName + '.mp4');
      return baseName + '.mp4';
    }
    saveBlob(parts, 'video/mp2t', baseName + '.ts');
    return baseName + '.ts（轉 MP4 失敗，請用 VLC 播放）';
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
        const typeText = s.kind === 'hls' ? '切片' : 'MP4';

        const row = document.createElement('div');
        row.style.cssText = 'margin-top:6px;padding:6px 8px;background:#333;border-radius:6px;cursor:pointer;';
        row.innerHTML = `<b>${s.label}</b> <small>（${typeText}）</small><br><small>${sizeText}｜${timeText}</small>`;
        row.addEventListener('click', () => {
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

  const active = site.features.filter(isOn);

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
