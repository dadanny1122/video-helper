// 「⭐ 稍後下載」測試（腳本 v3.25.0）：
//   1. 按「⭐ 稍後下載」→ 只送網頁網址（不含 #）和標題給電腦版 /later，不送影片位址
//   2. 從電腦版稍後清單打開（網址後面 #vh-later）→ 自動「送到電腦版」最高畫質，並把 #vh-later 拿掉
//   3. 舊電腦版（features 沒有 later）→ 說太舊、不送
// 用法：node .github/userscript_later_test.js video-helper.user.js
'use strict';
const fs = require('fs');
const { chromium } = require('playwright');
const src = fs.readFileSync(process.argv[2] || 'video-helper.user.js', 'utf8');
let fails = 0;
const ok = (c, m) => { console.log((c ? '✅ ' : '❌ ') + m); if (!c) fails++; };

const PAGE = `<!doctype html><html><head><title>t</title>
<script>var flashvars_1 = { video_title: 'later title', mediaDefinitions: [
  { format: 'hls', videoUrl: 'https://ev-h.phncdn.com/hls/480P/master.m3u8', quality: '480' },
  { format: 'hls', videoUrl: 'https://ev-h.phncdn.com/hls/1080P/master.m3u8', quality: '1080' } ] };</script></head>
<body><div id="player"><video></video></div></body></html>`;

async function run(b, feats, hash) {
  const ctx = await b.newContext({ locale: 'zh-TW' });
  const p = await ctx.newPage();
  const errors = [];
  p.on('pageerror', e => errors.push(String(e)));
  await ctx.route('**/*', r => new URL(r.request().url()).hostname === 'www.pornhub.com'
    ? r.fulfill({ contentType: 'text/html; charset=utf-8', body: PAGE }) : r.abort());
  await p.addInitScript(`(() => {
    window.__reqs = [];
    const FEATS = ${JSON.stringify(feats)};
    const store = { bridgeToken: 'tok', bridgePort: 47321 };
    window.GM_getValue = (k, d) => (k in store ? store[k] : d);
    window.GM_setValue = (k, v) => { store[k] = v; };
    window.GM_registerMenuCommand = () => {};
    window.GM_xmlhttpRequest = o => {
      const u = new URL(o.url);
      let body = {};
      if (u.hostname === '127.0.0.1') {
        window.__reqs.push([o.method, u.pathname, o.data ? JSON.parse(o.data) : null]);
        if (u.pathname === '/ping') body = { app: 'VideoHelper', version: '1.48.0', api: 1, features: FEATS, sites: ['pornhub.com'] };
        else if (u.pathname === '/later') body = { ok: true, added: true };
        else if (u.pathname === '/enqueue') body = { ok: true, ticket: 't1' };
        else if (u.pathname === '/status') body = { status: '等待中' };
        setTimeout(() => o.onload({ status: 200, responseText: JSON.stringify(body) }), 0);
      } else setTimeout(() => o.onerror && o.onerror({}), 0);
      return { abort() {} };
    };
    window.GM_download = () => {}; window.GM_setClipboard = () => {};
    window.GM_info = { script: { version: '9.9.9' }, scriptHandler: 'Violentmonkey' };
    window.unsafeWindow = window; window.muxjs = {};
    window.confirm = () => true; window.alert = () => {}; window.prompt = () => null;
  })();\n` + src);
  await p.goto('https://www.pornhub.com/view_video.php?viewkey=K1' + hash);
  await p.waitForTimeout(1500);
  return { ctx, p, errors };
}

(async () => {
  const b = await chromium.launch();
  const FEATS = ['enqueue', 'media', 'status', 'classify', 'node', 'later'];

  // 1. 按 ⭐
  let { ctx, p, errors } = await run(b, FEATS, '');
  await p.click('text=⬇ 下載');
  await p.waitForSelector('text=⭐ 稍後下載', { timeout: 5000 });
  await p.click('text=⭐ 稍後下載');
  await p.waitForTimeout(500);
  let reqs = await p.evaluate(() => window.__reqs);
  const later = reqs.filter(r => r[1] === '/later');
  ok(later.length === 1, '按 ⭐ 送出 1 次 /later');
  if (later.length) {
    const body = later[0][2];
    ok(body.url === 'https://www.pornhub.com/view_video.php?viewkey=K1' && body.title === 'later title', '送網頁網址和標題');
    ok(Object.keys(body).sort().join() === 'title,url' && !JSON.stringify(body).includes('phncdn'), '不送影片位址');
  }
  ok((await p.textContent('body')).includes('已加入電腦版的稍後下載清單'), '顯示「已加入」');
  ok(!reqs.some(r => r[1] === '/enqueue'), '沒有 #vh-later 不會自動送');
  ok(!errors.length, '沒有網頁錯誤 ' + errors.join(' '));
  await ctx.close();

  // 2. #vh-later 自動送最高畫質
  ({ ctx, p, errors } = await run(b, FEATS, '#vh-later'));
  await p.waitForTimeout(1000);
  reqs = await p.evaluate(() => window.__reqs);
  const enq = reqs.filter(r => r[1] === '/enqueue');
  ok(enq.length === 1 && enq[0][2].media.includes('1080P') && enq[0][2].url.endsWith('viewkey=K1'), '#vh-later：自動送最高畫質（網址不含 #）');
  ok(!(await p.evaluate(() => location.hash)), '#vh-later 拿掉了（重新整理不會再送）');
  ok(!errors.length, '沒有網頁錯誤 ' + errors.join(' '));
  await ctx.close();

  // 3. 舊電腦版
  ({ ctx, p, errors } = await run(b, ['enqueue', 'media', 'status'], ''));
  await p.click('text=⬇ 下載');
  await p.waitForSelector('text=⭐ 稍後下載', { timeout: 5000 });
  await p.click('text=⭐ 稍後下載');
  await p.waitForTimeout(500);
  reqs = await p.evaluate(() => window.__reqs);
  ok(!reqs.some(r => r[1] === '/later') && (await p.textContent('body')).includes('太舊'), '舊電腦版：說太舊、不送');
  await ctx.close();

  await b.close();
  console.log(fails ? `❌ ${fails} 項沒過` : '✅ 全部通過');
  process.exit(fails ? 1 : 0);
})();
