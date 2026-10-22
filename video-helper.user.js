// ==UserScript==
// @name         Video Helper
// @namespace    https://github.com/dadanny1122/video-helper
// @version      1.1.0
// @description  安全防護（擋彈窗、跳轉、危險下載）、自動略過廣告、載入時間評估、一鍵下載
// @author       dadanny1122
// @match        *://*.pornhub.com/*
// @match        *://*.xvideos.com/*
// @grant        GM_download
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      *
// @run-at       document-start
// @updateURL    https://raw.githubusercontent.com/dadanny1122/video-helper/main/video-helper.user.js
// @downloadURL  https://raw.githubusercontent.com/dadanny1122/video-helper/main/video-helper.user.js
// ==/UserScript==

(function () {
  /* ========== 第零部分：安全防護 ========== */
  // 網頁一開始就啟動，比廣告程式早一步

  const PW = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
  const baseOf = h => h.split('.').slice(-2).join('.');
  const HOME = baseOf(location.hostname);
  const DANGER_EXT = /\.(exe|msi|apk|bat|cmd|scr|vbs|ps1|jar|dmg|pkg|com)$/i;

  // 是不是別的網站
  function isExternal(url) {
    try {
      const u = new URL(url, location.href);
      return /^https?:$/.test(u.protocol) && baseOf(u.hostname) !== HOME;
    } catch (e) { return false; }
  }

  // 是不是危險的安裝檔
  function isDangerous(url) {
    try { return DANGER_EXT.test(new URL(url, location.href).pathname); }
    catch (e) { return false; }
  }

  // 假的跳過按鈕：按下去會帶你去別的網站或下載檔案
  function isFakeButton(el) {
    const a = el.closest('a[href]');
    return !!(a && (isExternal(a.href) || isDangerous(a.href)));
  }

  // ---- 左下角的盾牌和提示 ----
  let blockedCount = 0, shield = null, toastBox = null, realOpen = null;
  const pending = [];

  function report(msg, url) {
    blockedCount++;
    pending.push([msg, url]);
    flushUI();
  }

  function flushUI() {
    if (!document.body) return;
    if (!shield) {
      shield = document.createElement('div');
      shield.className = 'lte-ui';
      shield.style.cssText = 'position:fixed;bottom:20px;left:20px;z-index:999999;background:rgba(0,120,60,.9);' +
        'color:#fff;padding:6px 12px;border-radius:16px;font:bold 13px sans-serif;';
      toastBox = document.createElement('div');
      toastBox.className = 'lte-ui';
      toastBox.style.cssText = 'position:fixed;bottom:60px;left:20px;z-index:999999;display:flex;' +
        'flex-direction:column-reverse;gap:6px;max-width:360px;';
      document.body.append(shield, toastBox);
    }
    shield.textContent = `🛡 已攔截 ${blockedCount} 次`;
    while (pending.length) showToast(...pending.shift());
  }

  function showToast(msg, url) {
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
      t.appendChild(host);
      const go = document.createElement('span');
      go.textContent = '我確定要開啟';
      go.style.cssText = 'color:#8cf;text-decoration:underline;cursor:pointer;font-size:12px;';
      go.addEventListener('click', () => { t.remove(); if (realOpen) realOpen.call(PW, url, '_blank', 'noopener'); });
      t.appendChild(go);
    }
    toastBox.appendChild(t);
    setTimeout(() => t.remove(), 7000);
  }

  document.addEventListener('DOMContentLoaded', flushUI);

  // 1. 擋彈出視窗、偷開新分頁
  try {
    realOpen = PW.open;
    PW.open = function (url, ...rest) {
      const u = url ? String(url) : '';
      if (!u || u === 'about:blank' || isExternal(u)) { report('擋下彈出視窗', isExternal(u) ? u : null); return null; }
      if (isDangerous(u)) { report('擋下危險檔案下載', null); return null; }
      return realOpen.call(PW, url, ...rest);
    };
  } catch (e) {}

  // 2. 擋廣告連結和危險檔案（在網站的程式之前先檢查每次點擊）
  document.addEventListener('click', e => {
    const a = e.target && e.target.closest && e.target.closest('a[href]');
    if (!a || a.closest('.lte-ui')) return;
    const href = a.href;
    if (isDangerous(href)) {
      e.preventDefault(); e.stopImmediatePropagation();
      report('擋下危險檔案下載：' + href.split('/').pop().split('?')[0], null);
    } else if (isExternal(href) && a.target === '_blank') {
      e.preventDefault(); e.stopImmediatePropagation();
      report('擋下廣告連結（會開新分頁到別的網站）', href);
    }
  }, true);

  // 3. 擋自動跳轉：不是你點的、卻要把你帶去別的網站
  try {
    const nav = PW.navigation;
    if (nav) nav.addEventListener('navigate', e => {
      const url = e.destination && e.destination.url;
      if (!url || !e.cancelable) return;
      if (isDangerous(url)) { e.preventDefault(); report('擋下危險檔案下載', null); }
      else if (!e.userInitiated && isExternal(url)) { e.preventDefault(); report('擋下自動跳轉', url); }
    });
  } catch (e) {}

  // 4. 擋「允許通知」請求
  try {
    if (PW.Notification) {
      PW.Notification.requestPermission = function () {
        report('擋下通知權限請求', null);
        return Promise.resolve('denied');
      };
    }
  } catch (e) {}

  /* ========== 網頁載入完成後才啟動的功能 ========== */

  function main() {
  let adShowing = false;

  /* ========== 第一部分：自動略過廣告 ========== */

  const skipWords = ['skip', '跳過', '跳过', '略過', '略过'];
  const adTexts = ['ad', '廣告', '广告'];

  function realClick(el) {
    ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(type => {
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true }));
    });
  }

  setInterval(() => {
    let found = false;
    for (const el of document.querySelectorAll('button, div, span, a')) {
      if (el.offsetParent === null) continue;
      if (el.closest('.lte-ui')) continue;
      const text = (el.innerText || '').trim().toLowerCase();
      if (!text || text.length > 15) continue;
      if (skipWords.some(w => text.includes(w)) && !/\d/.test(text) && !isFakeButton(el)) realClick(el);
      if (el.children.length === 0 && adTexts.includes(text)) found = true;
    }
    adShowing = found;

    if (adShowing) {
      for (const v of document.querySelectorAll('video')) {
        if (v.duration && v.duration < 120 && v.currentTime < v.duration - 0.5) {
          v.muted = true;
          v.currentTime = v.duration;
        }
      }
    }
  }, 300);

  /* ========== 共用工具 ========== */

  const BITRATE = { 240: 0.5, 360: 0.8, 480: 1.5, 720: 3, 1080: 5.5, 1440: 9, 2160: 16 };

  performance.setResourceTimingBufferSize(1000);
  performance.addEventListener('resourcetimingbufferfull', () => performance.clearResourceTimings());

  function bitrateFor(h) {
    if (!h) return BITRATE[720];
    let best = 240;
    for (const k of Object.keys(BITRATE).map(Number)) if (h >= k) best = k;
    return BITRATE[best];
  }

  function parseDur(str) {
    if (!str) return 0;
    if (/^\d+$/.test(str)) return Number(str);
    const m = str.match(/PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?/);
    if (!m) return 0;
    return (+m[1] || 0) * 3600 + (+m[2] || 0) * 60 + (+m[3] || 0);
  }

  function saveSpeed(mbps) { try { localStorage.setItem('lte_speed', mbps); } catch (e) {} }
  function loadSpeed() { try { return Number(localStorage.getItem('lte_speed')) || 0; } catch (e) { return 0; } }

  function measureSpeed() {
    const entries = performance.getEntriesByType('resource')
      .filter(e => e.transferSize > 100000 && e.responseEnd - e.responseStart > 0)
      .slice(-10);
    if (entries.length) {
      let bytes = 0, ms = 0;
      for (const e of entries) { bytes += e.transferSize; ms += e.responseEnd - e.responseStart; }
      const mbps = bytes * 8 / (ms / 1000) / 1e6;
      saveSpeed(mbps);
      return { mbps, source: '實測' };
    }
    const saved = loadSpeed();
    if (saved) return { mbps: saved, source: '上次實測' };
    if (navigator.connection && navigator.connection.downlink) {
      return { mbps: navigator.connection.downlink, source: '粗估' };
    }
    return null;
  }

  function fmt(sec) {
    if (!isFinite(sec) || sec < 0) return '計算中';
    if (sec < 60) return sec.toFixed(1) + ' 秒';
    return Math.floor(sec / 60) + ' 分 ' + Math.round(sec % 60) + ' 秒';
  }

  function fmtSize(bytes) {
    if (bytes >= 1e9) return (bytes / 1e9).toFixed(2) + ' GB';
    return (bytes / 1e6).toFixed(0) + ' MB';
  }

  function ratingOf(ratio) {
    return ratio >= 2 ? '🟢' : ratio >= 1.2 ? '🟡' : '🔴';
  }

  function recommend(mbps) {
    const keys = Object.keys(BITRATE).map(Number).reverse();
    for (const k of keys) if (mbps / BITRATE[k] >= 1.2) return k + 'p';
    return '240p';
  }

  /* ========== 第二部分：影片頁評估框 ========== */

  function getMainVideo() {
    const v = [...document.querySelectorAll('video')]
      .find(x => isFinite(x.duration) && x.duration > 120);
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
  }

  let collapsed = false;
  const box = document.createElement('div');
  box.className = 'lte-ui';
  box.style.cssText = 'position:fixed;top:10px;right:20px;z-index:999999;background:rgba(0,0,0,.8);' +
    'color:#fff;padding:8px 12px;border-radius:8px;font:14px/1.6 sans-serif;cursor:pointer;' +
    'white-space:nowrap;display:none;';
  box.title = '點一下收合 / 展開';
  box.addEventListener('click', () => { collapsed = !collapsed; });
  document.body.appendChild(box);

  setInterval(() => {
    const main = getMainVideo();
    if (!main) { box.style.display = 'none'; return; }
    box.style.display = 'block';
    if (collapsed) { box.textContent = '⏱'; return; }

    const speed = measureSpeed();
    if (!speed) { box.textContent = '量測網速中…'; return; }

    const need = bitrateFor(main.height);
    const ratio = speed.mbps / need;
    const startSec = need * 5 / speed.mbps + 0.5;
    const fullSec = need * main.sec / speed.mbps;
    const ratingText = ratio >= 2 ? '順暢' : ratio >= 1.2 ? '可能偶爾卡' : '會卡，建議降畫質';
    const header = (adShowing || main.fromPage) ? '📺 廣告中，以下為預估<br>' : '';
    const quality = main.height ? main.height + 'p' : '以 720p 預估';

    box.innerHTML = header +
      `網速：${speed.mbps.toFixed(1)} Mbps（${speed.source}）<br>` +
      `影片長度：${fmt(main.sec)}<br>` +
      `畫質：${quality}<br>` +
      `開始播放：約 ${fmt(startSec)}<br>` +
      `整部載完：約 ${fmt(fullSec)}<br>` +
      `${ratingOf(ratio)} ${ratingText}｜建議最高 ${recommend(speed.mbps)}`;
  }, 1000);

  /* ========== 第三部分：列表頁縮圖標示 ========== */

  function parseThumbTime(t) {
    let m = t.match(/^(?:(\d+):)?(\d{1,2}):(\d{2})$/);
    if (m) return (+m[1] || 0) * 3600 + (+m[2]) * 60 + (+m[3]);
    m = t.match(/^(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?$/i);
    if (m && (m[1] || m[2])) return (+m[1] || 0) * 3600 + (+m[2] || 0) * 60;
    m = t.match(/^(\d+)\s*sec$/i);
    if (m) return +m[1];
    return 0;
  }

  setInterval(() => {
    const speed = measureSpeed();
    if (!speed) return;
    const need = BITRATE[720];
    const ratio = speed.mbps / need;

    for (const el of document.querySelectorAll('span, div, var')) {
      if (el.dataset.lteDone || el.children.length > 0) continue;
      if (el.closest('[class*="player"], [id*="player"], .lte-ui')) continue;
      const sec = parseThumbTime((el.innerText || '').trim());
      if (sec < 60) continue;

      el.dataset.lteDone = '1';
      const fullSec = need * sec / speed.mbps;
      const tag = document.createElement('span');
      tag.textContent = ` ${ratingOf(ratio)} ${fullSec < 60 ? Math.round(fullSec) + '秒' : Math.round(fullSec / 60) + '分'}`;
      tag.style.cssText = 'font-size:11px;margin-left:4px;';
      tag.title = `以 720p、${speed.mbps.toFixed(1)} Mbps 估算整部載完時間`;
      el.appendChild(tag);
    }
  }, 1500);

  /* ========== 第四部分：一鍵下載（XVideos + Pornhub）========== */

  const W = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;

  function safeName(s) {
    return s.replace(/[\\/:*?"<>|]/g, '_').trim().slice(0, 100) || 'video';
  }

  function absUrl(u, base) { return new URL(u, base).href; }

  // 用暴力猴抓資料（可以跨網域）
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

  // 先用瀏覽器一般方式抓（比較快），失敗再用暴力猴
  async function getData(url, type) {
    try {
      const r = await fetch(url);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return type === 'text' ? await r.text() : await r.arrayBuffer();
    } catch (e) {
      return gmGet(url, type);
    }
  }

  // 只問伺服器「檔案多大」，不會真的下載
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

  // 讀「畫質總表」：列出各畫質的片段清單網址
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
        out.push({
          url: absUrl(lines[j].trim(), base),
          height: res ? +res[1] : (name ? +name[1] : 0),
          bw: bw ? +bw[1] : 0,
        });
      }
    }
    return out;
  }

  // 讀「片段清單」：列出所有小片段網址和總長度
  function parseMedia(text, base) {
    const lines = text.split(/\r?\n/);
    const segs = [];
    let dur = 0, init = null, encrypted = false;
    for (const raw of lines) {
      const l = raw.trim();
      if (l.startsWith('#EXTINF:')) dur += parseFloat(l.slice(8)) || 0;
      else if (l.startsWith('#EXT-X-MAP')) { const m = l.match(/URI="([^"]+)"/); if (m) init = absUrl(m[1], base); }
      else if (l.startsWith('#EXT-X-KEY') && !/METHOD=NONE/.test(l)) encrypted = true;
      else if (l && !l.startsWith('#')) segs.push(absUrl(l, base));
    }
    return { segs, dur, init, encrypted };
  }

  // 如果拿到的是總表，就往下找到真正的片段清單
  async function resolveMedia(url, bw = 0) {
    const text = await getData(url, 'text');
    if (text.includes('#EXT-X-STREAM-INF')) {
      const v = parseMaster(text, url).sort((a, b) => b.height - a.height)[0];
      return resolveMedia(v.url, v.bw || bw);
    }
    return Object.assign(parseMedia(text, url), { bw });
  }

  // XVideos：MP4 直連 + 切片
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

  // Pornhub：優先用官方 MP4，沒有的畫質才用切片
  async function findPhSources() {
    const key = Object.keys(W).find(k => k.startsWith('flashvars_'));
    if (!key) return { title: document.title, list: [] };
    const fv = W[key];
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

  // 估算檔案大小：MP4 直接問伺服器，切片用「網速需求 × 長度」算
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

  // 切片下載：同時抓 6 段，失敗重試 3 次，全部接起來後存檔
  async function downloadHls(s, baseName, onProgress) {
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
        done++;
        onProgress(done, total);
      }
    }

    await Promise.all(Array.from({ length: 6 }, worker));
    if (media.init) parts.unshift(new Uint8Array(await getData(media.init, 'arraybuffer')));

    const isMp4 = !!media.init;
    const blob = new Blob(parts, { type: isMp4 ? 'video/mp4' : 'video/mp2t' });
    const fileName = baseName + (isMp4 ? '.mp4' : '.ts');

    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = fileName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
    return fileName;
  }

  function buildDownloadUI(src) {
    const wrap = document.createElement('div');
    wrap.className = 'lte-ui';
    document.body.appendChild(wrap);

    const btn = document.createElement('div');
    btn.textContent = '⬇ 下載';
    btn.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:999999;background:#e53935;color:#fff;' +
      'padding:10px 16px;border-radius:24px;font:bold 15px sans-serif;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.4);';
    wrap.appendChild(btn);

    const panel = document.createElement('div');
    panel.style.cssText = 'position:fixed;bottom:70px;right:20px;z-index:999999;background:rgba(20,20,20,.95);' +
      'color:#fff;padding:12px 14px;border-radius:10px;font:14px/1.7 sans-serif;min-width:260px;' +
      'max-height:70vh;overflow-y:auto;display:none;';
    wrap.appendChild(panel);

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
      panel.innerHTML = `下載中（請勿關閉分頁）：<br><small>${baseName}</small><br><span class="lte-prog">準備中…</span>`;
      const prog = panel.querySelector('.lte-prog');
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
          const name = await downloadHls(s, baseName, (d, total) => showFrac(d / total, `｜${d}/${total} 段`));
          ok(name);
        } catch (e) {
          fail(e.message);
        }
      }
    }
  }

  // 啟動：只在影片頁出現下載按鈕
  (async () => {
    const host = location.hostname;
    let src = null;
    if (host.includes('xvideos.com') && /setVideo(UrlHigh|UrlLow|HLS)/.test(document.documentElement.innerHTML)) {
      src = await findXvSources();
    } else if (host.includes('pornhub.com') && location.pathname.includes('view_video')) {
      src = await findPhSources();
    }
    if (src && src.list.length) buildDownloadUI(src);
  })();
  }

  if (document.body) main();
  else document.addEventListener('DOMContentLoaded', main);
})();
