// 「🐞 回報網站壞了」測試（腳本 v3.24.0）：按下去要開 GitHub 回報表單，
// 只帶網站名和版本，不能帶影片網址（viewkey）、封鎖清單；要用 noopener,noreferrer（不把網址當 Referer 傳過去）
// 用法：node .github/userscript_report_test.js video-helper.user.js
'use strict';
const fs = require('fs');
const { chromium } = require('playwright');
const src = fs.readFileSync(process.argv[2] || 'video-helper.user.js', 'utf8');
let fails = 0;
const ok = (c, m) => { console.log((c ? '✅ ' : '❌ ') + m); if (!c) fails++; };

const PAGE = `<!doctype html><html><head><title>t</title>
<script>var flashvars_1 = { video_title: 'secret title', mediaDefinitions: [
  { format: 'hls', videoUrl: 'https://ev-h.phncdn.com/hls/x/master.m3u8', quality: '720' } ] };</script></head>
<body><div id="player"><video></video></div></body></html>`;

(async () => {
  const b = await chromium.launch();
  for (const locale of ['zh-TW', 'en-US']) {
    const ctx = await b.newContext({ locale });
    const p = await ctx.newPage();
    const errors = [];
    p.on('pageerror', e => errors.push(String(e)));
    await ctx.route('**/*', r => new URL(r.request().url()).hostname === 'www.pornhub.com'
      ? r.fulfill({ contentType: 'text/html; charset=utf-8', body: PAGE }) : r.abort());
    await p.addInitScript(`(() => {
      window.__opened = []; window.__menu = {};
      window.open = (u, t, f) => { window.__opened.push([String(u), t, f]); return null; };
      const store = {};
      window.GM_getValue = (k, d) => (k in store ? store[k] : d);
      window.GM_setValue = (k, v) => { store[k] = v; };
      window.GM_registerMenuCommand = (n, fn) => { window.__menu[n] = fn; };
      window.GM_xmlhttpRequest = o => { setTimeout(() => o.onerror && o.onerror({}), 0); return { abort() {} }; };
      window.GM_download = () => {}; window.GM_setClipboard = () => {};
      window.GM_info = { script: { version: '9.9.9' }, scriptHandler: 'Violentmonkey' };
      window.unsafeWindow = window; window.muxjs = {};
      window.confirm = () => true; window.alert = () => {};
    })();\n` + src);
    await p.goto('https://www.pornhub.com/view_video.php?viewkey=SECRETKEY123');
    await p.waitForTimeout(1500);
    const names = await p.evaluate(() => Object.keys(window.__menu));
    const key = names.find(n => /回報網站壞了|Report a broken site/.test(n));
    ok(!!key, `${locale}：暴力猴選單有「回報網站壞了」（${key}）`);
    if (key) await p.evaluate(k => window.__menu[k](), key);
    const opened = await p.evaluate(() => window.__opened);
    ok(opened.length === 1, `${locale}：開了 1 個分頁`);
    if (opened.length) {
      const [u, , f] = opened[0];
      const url = new URL(u);
      ok(url.origin + url.pathname === 'https://github.com/dadanny1122/video-helper/issues/new', `${locale}：開的是 GitHub 回報表單`);
      ok(url.searchParams.get('template') === 'site-broken.yml', `${locale}：用 site-broken 表單`);
      ok(url.searchParams.get('site') === 'Pornhub' && url.searchParams.get('version') === '9.9.9', `${locale}：帶網站名和版本`);
      ok(!/SECRETKEY|viewkey|secret title|phncdn/i.test(u), `${locale}：沒有帶影片網址、標題、影片伺服器`);
      ok(/noopener/.test(f) && /noreferrer/.test(f), `${locale}：noopener,noreferrer（不傳 Referer）`);
    }
    ok(!errors.length, `${locale}：沒有網頁錯誤 ${errors.join(' ')}`);
    await ctx.close();
  }
  await b.close();
  if (!fs.existsSync('.github/ISSUE_TEMPLATE/site-broken.yml')) { console.log('❌ 少了 .github/ISSUE_TEMPLATE/site-broken.yml'); fails++; }
  console.log(fails ? `❌ ${fails} 項沒過` : '✅ 全部通過');
  process.exit(fails ? 1 : 0);
})();
