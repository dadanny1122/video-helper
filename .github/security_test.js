// 瀏覽器腳本的安全測試：在每個支援的網站開一個「假網頁」，腳本執行前先埋好監視器，
// 只要腳本讀了 Cookie、密碼欄位、不是自己的 localStorage、IndexedDB、瀏覽器存的帳密，
// 或連到「這個網站和電腦版（127.0.0.1）以外」的地方，就算失敗。
// 用法：node .github/security_test.js video-helper.user.js
const fs = require('fs');
const { chromium } = require('playwright');

const src = fs.readFileSync(process.argv[2] || 'video-helper.user.js', 'utf8');
const sites = [...src.matchAll(/^\/\/ @match\s+\*:\/\/\*\.([^/]+)\/\*/gm)].map(m => m[1]);
// @include 用正規表示式寫的網站（xHamster、MissAV）另外加
if (/@include.*xhamster/.test(src)) sites.push('xhamster.com');
if (/@include.*missav/.test(src)) sites.push('missav.ws');

// 腳本執行「之前」先跑：把敏感的東西都包一層，被碰到就記下來
const MONITOR = `
(() => {
  const hits = window.__vhHits = [];
  const hit = (what, detail) => hits.push(what + (detail ? '：' + detail : ''));
  const wrapGetter = (proto, prop, check) => {
    const d = Object.getOwnPropertyDescriptor(proto, prop);
    Object.defineProperty(proto, prop, { ...d, get() { check(this); return d.get.call(this); } });
  };
  wrapGetter(Document.prototype, 'cookie', () => hit('讀 Cookie'));
  wrapGetter(HTMLInputElement.prototype, 'value', el => { if (el.type === 'password') hit('讀密碼欄位'); });
  for (const st of [Storage.prototype]) {
    const gi = st.getItem, k = st.key;
    st.getItem = function (key) { if (!String(key).startsWith('vh_')) hit('讀網站的 localStorage', key); return gi.call(this, key); };
    st.key = function (i) { hit('列出 localStorage 所有項目'); return k.call(this, i); };
  }
  const idb = indexedDB.open.bind(indexedDB);
  indexedDB.open = (...a) => { hit('開 IndexedDB', a[0]); return idb(...a); };
  if (indexedDB.databases) { const dbs = indexedDB.databases.bind(indexedDB); indexedDB.databases = () => { hit('列出 IndexedDB'); return dbs(); }; }
  if (navigator.credentials) navigator.credentials.get = () => { hit('讀瀏覽器存的帳號密碼'); return Promise.resolve(null); };
  // 假的 GM_*：記下腳本要連到哪裡
  window.__vhXhr = [];
  window.GM_xmlhttpRequest = o => { window.__vhXhr.push(o.url); setTimeout(() => o.onerror && o.onerror({}), 0); return { abort() {} }; };
  window.GM_download = o => { window.__vhXhr.push(o.url); };
  const store = {};
  window.GM_getValue = (k, d) => (k in store ? store[k] : d);
  window.GM_setValue = (k, v) => { store[k] = v; };
  window.GM_registerMenuCommand = () => {};
  window.GM_setClipboard = () => {};
  window.GM_info = { script: { version: 'test' }, scriptHandler: 'test' };
  window.unsafeWindow = window;
  window.muxjs = {};
})();`;

// 假網頁：有 Cookie、有網站自己的 localStorage、有密碼欄位（看腳本會不會去碰）
const page = host => `<!doctype html><html><head><title>test video</title>
<script>localStorage.setItem('site_session','secret'); document.cookie = 'session=secret; path=/';</script></head>
<body><form><input type="text" name="user" value="me"><input type="password" name="pw" value="hunter2"></form>
<video id="player" src="https://${host}/v.mp4" controls></video>
<div class="video-wrapper"><a href="/view_video.php?viewkey=abc">video</a></div></body></html>`;

(async () => {
  const browser = await chromium.launch();
  let failed = 0;
  for (const domain of sites) {
    const host = 'www.' + domain;
    const ctx = await browser.newContext();
    const p = await ctx.newPage();
    const outside = [];
    await ctx.route('**/*', route => {
      const u = new URL(route.request().url());
      const own = u.hostname === domain || u.hostname.endsWith('.' + domain);
      if (u.hostname === host && u.pathname === '/test') return route.fulfill({ contentType: 'text/html', body: page(host) });
      if (!own && u.hostname !== '127.0.0.1') outside.push(u.hostname);
      return route.abort();
    });
    await p.addInitScript(MONITOR);
    await p.addInitScript(src);                       // 腳本本身（document-start，跟暴力猴一樣在網頁之前跑）
    const errors = [];
    p.on('pageerror', e => errors.push(String(e.message || e).slice(0, 120)));
    await p.goto(`https://${host}/test`);
    await p.waitForTimeout(2500);
    // 把介面上的按鈕都點一遍（打開盾牌、選單），多跑一點程式碼
    for (const el of await p.$$('.vh-ui button, .vh-ui [role=button]')) { try { await el.click({ timeout: 300 }); } catch (e) {} }
    await p.waitForTimeout(800);
    const hits = await p.evaluate(() => window.__vhHits);
    const xhr = (await p.evaluate(() => window.__vhXhr)).map(u => { try { return new URL(u, location.href).hostname; } catch (e) { return u; } })
      .filter(h => !(h === domain || h.endsWith('.' + domain) || h === '127.0.0.1'));
    const ui = await p.$$eval('.vh-ui', els => els.length);
    const bad = [...new Set([...hits, ...outside.map(h => '連到 ' + h), ...xhr.map(h => '腳本連到 ' + h)])];
    const ok = bad.length === 0;
    if (!ok) failed++;
    console.log(`${ok ? '✅' : '❌'} ${domain}｜介面元件 ${ui}${bad.length ? '｜' + bad.join('、') : ''}${errors.length ? '｜錯誤：' + errors[0] : ''}`);
    if (!ok) console.log(`::error title=腳本安全測試 ${domain}::${bad.join('、')}`);
    await ctx.close();
  }
  await browser.close();
  console.log(`::notice title=腳本安全測試::${sites.length} 個網站，${failed ? failed + ' 個失敗' : '全部通過'}（監視：Cookie、密碼欄位、網站的 localStorage、IndexedDB、瀏覽器帳密、對外連線）`);
  process.exit(failed ? 1 : 0);
})();
