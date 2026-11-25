// 腳本「💬 意見回饋」測試（v3.25.0）：字數不夠不能送；送出去只有類型、內容（網址刪掉）、版本，沒有你在看的網址；
// 拿到代碼後徽章、限定配色出現；代碼只存在暴力猴裡（網站的 localStorage 拿不到）
// 用法：node .github/userscript_feedback_test.js video-helper.user.js
'use strict';
const fs = require('fs');
const { chromium } = require('playwright');
let src = fs.readFileSync(process.argv[2] || 'video-helper.user.js', 'utf8');
src = src.replace("const FEEDBACK_URL = '';", "const FEEDBACK_URL = 'https://fb.test';");
let fails = 0;
const ok = (c, m) => { console.log((c ? '✅ ' : '❌ ') + m); if (!c) fails++; };
const b64u = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
const TOKEN = `VH1.${b64u({ v: 1, id: '0123456789abcdef', t: 1 })}.c2ln`;
const PAGE = `<!doctype html><html><head><title>t</title>
<script>var flashvars_1 = { video_title: 'secret title', mediaDefinitions: [{ format: 'hls', videoUrl: 'https://ev-h.phncdn.com/hls/x/master.m3u8', quality: '720' }] };</script>
</head><body><div id="player"><video></video></div></body></html>`;

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ locale: 'zh-TW' });
  const p = await ctx.newPage();
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await ctx.route('**/*', (r) => new URL(r.request().url()).hostname === 'www.pornhub.com'
    ? r.fulfill({ contentType: 'text/html; charset=utf-8', body: PAGE }) : r.abort());
  await p.exposeFunction('__fbPost', async (o) => JSON.stringify({ status: 200, text: JSON.stringify({ ok: true, token: TOKEN, pub: '' }) }));
  await p.addInitScript(`(() => {
    const store = JSON.parse(sessionStorage.getItem('__gm') || '{}');
    window.__menu = {}; window.__reqs = [];
    window.GM_getValue = (k, d) => (k in store ? store[k] : d);
    window.GM_setValue = (k, v) => { store[k] = v; sessionStorage.setItem('__gm', JSON.stringify(store)); };
    window.GM_registerMenuCommand = (n, fn) => { window.__menu[n] = fn; };
    window.GM_xmlhttpRequest = (o) => {
      if (o.url.startsWith('https://fb.test')) { window.__reqs.push({ url: o.url, data: o.data });
        window.__fbPost(o).then((t) => { const r = JSON.parse(t); o.onload({ status: r.status, responseText: r.text }); }); }
      else setTimeout(() => o.onerror && o.onerror({}), 0);
      return { abort() {} };
    };
    window.GM_download = () => {}; window.GM_setClipboard = () => {};
    window.GM_info = { script: { version: '9.9.9' }, scriptHandler: 'Violentmonkey' };
    window.unsafeWindow = window; window.muxjs = {};
  })();\n` + src);
  await p.goto('https://www.pornhub.com/view_video.php?viewkey=SECRETKEY123');
  await p.waitForTimeout(1200);
  const key = (await p.evaluate(() => Object.keys(window.__menu))).find((n) => n.includes('意見回饋'));
  ok(!!key, '暴力猴選單有「💬 意見回饋」');
  await p.evaluate((k) => window.__menu[k](), key);
  const ta = p.locator('.vh-ui textarea');
  ok(await ta.count() === 1, '打開回饋視窗');
  await ta.fill('太短了');
  ok(/還差 13 個中文字/.test(await p.locator('.vh-ui').last().innerText()), '字數不夠：顯示還差幾個字');
  await p.locator('.vh-ui span', { hasText: /^送出$/ }).click();
  await p.waitForTimeout(200);
  ok((await p.evaluate(() => window.__reqs.length)) === 0, '字數不夠 → 按送出也不會送');
  await ta.fill('這個工具很好用但是希望可以多一點功能 https://www.pornhub.com/view_video.php?viewkey=SECRETKEY123');
  ok(/可以送出了/.test(await p.locator('.vh-ui').last().innerText()), '字數夠了：可以送出');
  await p.locator('.vh-ui span', { hasText: /^送出$/ }).click();
  await p.waitForTimeout(500);
  const reqs = await p.evaluate(() => window.__reqs);
  ok(reqs.length === 1 && reqs[0].url === 'https://fb.test/feedback', '送到 Worker 的 /feedback');
  const body = reqs.length ? JSON.parse(reqs[0].data) : {};
  ok(body.app === 'script' && body.ver === '9.9.9' && body.kind === 'suggest', '帶類型、腳本版本');
  ok(!/SECRETKEY|viewkey|secret title|phncdn/i.test(reqs.length ? reqs[0].data : 'x') && /\[網址已移除\]/.test(body.text || ''), '沒有送出網址、影片標題（內容裡的網址刪掉了）');
  const txt = await p.locator('.vh-ui').last().innerText();
  ok(txt.includes(TOKEN) && /銅牌支持者/.test(txt) && /極光/.test(txt), '顯示代碼、🥉 徽章、限定配色選項');
  ok(await p.evaluate(() => !Object.keys(localStorage).some((k) => k.includes('fb_tokens'))), '代碼沒有放在網站的 localStorage');
  ok(await p.evaluate((t) => JSON.parse(sessionStorage.getItem('__gm')).fb_tokens.includes(t), TOKEN), '代碼存在暴力猴裡');
  await Promise.all([p.waitForNavigation(), p.locator('.vh-ui span', { hasText: /^極光$/ }).click()]);
  await p.waitForTimeout(1200);
  ok(await p.evaluate(() => [...document.querySelectorAll('style')].some((s) => s.textContent.includes('#3ee6a8'))), '換成「極光」配色（重新整理後生效）');
  ok(!errors.length, '沒有網頁錯誤 ' + errors.join(' '));
  await b.close();
  console.log(fails ? `❌ ${fails} 項沒過` : '全部通過');
  process.exit(fails ? 1 : 0);
})();
