// 瀏覽器腳本英文版測試（v3.23.0，docs/i18n/00-英文版規格.md E2）：
// 假的 Pornhub 影片頁 + 假暴力猴，瀏覽器語言設英文 → 打開盾牌面板、功能開關、狀態檢查、下載清單，
// 腳本自己的畫面（.vh-ui）不能還有中文；網頁本身的中文不能被動到；瀏覽器是中文時腳本也是中文；選單、對話框也翻。
// 用法：node .github/userscript_i18n_test.js video-helper.user.js [截圖資料夾]
'use strict';
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const src = fs.readFileSync(process.argv[2] || 'video-helper.user.js', 'utf8');
const out = process.argv[3] || 'userscript-shots';
fs.mkdirSync(out, { recursive: true });
let fails = 0;
const ok = (c, msg) => { console.log((c ? '✅ ' : '❌ ') + msg); if (!c) fails++; };

const PAGE = `<!doctype html><html><head><title>test</title>
<script>var flashvars_1 = { video_title: 'test', video_duration: 600, mediaDefinitions: [
  { format: 'hls', videoUrl: 'https://ev-h.phncdn.com/hls/x/master.m3u8', quality: '720' },
  { format: 'hls', videoUrl: 'https://ev-h.phncdn.com/hls/x/480.m3u8', quality: '480' } ] };</script></head>
<body><h1 id="site-text">網站自己的中文標題</h1><div id="player"><video></video></div>
<iframe src="https://ads.exoclick.com/x"></iframe></body></html>`;

const FAKE_GM = (store) => `(() => {
  const store = ${JSON.stringify(store)};
  window.__menu = []; window.__dialogs = [];
  window.GM_getValue = (k, d) => (k in store ? store[k] : d);
  window.GM_setValue = (k, v) => { store[k] = v; };
  window.GM_registerMenuCommand = (n) => window.__menu.push(n);
  window.GM_xmlhttpRequest = (o) => { setTimeout(() => o.onerror && o.onerror({}), 0); return { abort() {} }; };
  window.GM_download = () => {};
  window.GM_setClipboard = () => {};
  window.GM_info = { script: { version: 'test' }, scriptHandler: 'Violentmonkey' };
  window.unsafeWindow = window;
  window.muxjs = {};
  const a = window.alert, c = window.confirm;
  window.alert = (m) => { window.__dialogs.push(String(m)); };
  window.confirm = (m) => { window.__dialogs.push(String(m)); return false; };
})();`;

async function open(browser, locale, store = {}) {
  const ctx = await browser.newContext({ locale, viewport: { width: 1280, height: 860 } });
  const p = await ctx.newPage();
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await ctx.route('**/*', (route) => {
    const u = new URL(route.request().url());
    if (u.hostname === 'www.pornhub.com') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: PAGE });
    return route.abort();
  });
  await p.addInitScript(FAKE_GM(store) + '\n' + src);
  await p.goto('https://www.pornhub.com/view_video.php?viewkey=i18n');
  await p.waitForTimeout(1500);
  return { ctx, p, errors };
}

async function leftovers(p) {
  return p.evaluate(() => {
    const CJK = /[㐀-鿿豈-﫿]/, bad = new Set();
    for (const root of document.querySelectorAll('.vh-ui')) {
      const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let n = w.nextNode(); n; n = w.nextNode()) if (CJK.test(n.nodeValue.replace('🌐 中文', ''))) bad.add(n.nodeValue.trim());
      for (const el of root.querySelectorAll('[title]')) if (CJK.test(el.title)) bad.add('title=' + el.title);
    }
    return [...bad];
  });
}

async function clickText(p, texts) {
  for (const t of texts) {
    const el = p.getByText(t, { exact: false }).first();
    if (await el.count()) { try { await el.click({ timeout: 1000 }); await p.waitForTimeout(400); return true; } catch (e) {} }
  }
  return false;
}

(async () => {
  const browser = await chromium.launch();
  // ---- 英文 ----
  const { ctx, p, errors } = await open(browser, 'en-US');
  const check = async (where) => {
    const b = await leftovers(p);
    ok(!b.length, `英文：${where}沒有漏翻` + (b.length ? '：' + b.slice(0, 10).map((x) => JSON.stringify(x)).join('、') : ''));
  };
  ok(await p.locator('.vh-ui').count() > 0, '腳本有畫面');
  await check('一打開');
  ok((await p.textContent('#site-text')) === '網站自己的中文標題', '網頁本身的中文沒被動到');
  ok(await clickText(p, ['🛡 Blocked']), '盾牌是英文（🛡 Blocked N）');
  await p.screenshot({ path: path.join(out, 'shield-en.png') });
  await check('盾牌面板');
  ok(await clickText(p, ['⚙ Features']), '打開功能開關');
  await p.screenshot({ path: path.join(out, 'features-en.png') });
  await check('功能開關');
  await clickText(p, ['Close']);
  await clickText(p, ['🛡 Blocked']);
  ok(await clickText(p, ['🩺 Health check']), '打開狀態檢查');
  await check('狀態檢查');
  await clickText(p, ['Close']);
  ok(await clickText(p, ['⬇ Download']), '下載按鈕是英文');
  await p.waitForTimeout(1500);
  await p.screenshot({ path: path.join(out, 'download-en.png') });
  await check('下載清單');
  const menu = await p.evaluate(() => window.__menu);
  ok(menu.length > 0 && menu.every((m) => !/[㐀-鿿]/.test(m.replace('🌐 中文', ''))) && menu.includes('🌐 中文'), '暴力猴選單是英文、有切回中文：' + menu.slice(0, 4).join('｜'));
  await clickText(p, ['🛡 Blocked']);
  await clickText(p, ['⏸ Pause ad blocking']);
  const dlg = await p.evaluate(() => window.__dialogs);
  ok(dlg.length > 0 && dlg.every((m) => !/[㐀-鿿]/.test(m)), '對話框是英文：' + JSON.stringify(dlg.slice(-1)));
  ok(errors.length === 0, '英文：沒有 JavaScript 錯誤' + (errors.length ? '：' + errors.join(' | ') : ''));
  await ctx.close();

  // ---- 中文瀏覽器 → 中文；英文瀏覽器但手動選中文 → 中文 ----
  for (const [locale, store, why] of [['zh-TW', {}, '瀏覽器是中文'], ['en-US', { lang: 'zh' }, '手動選了中文']]) {
    const r = await open(browser, locale, store);
    ok(await r.p.getByText('🛡 已攔截', { exact: false }).count() > 0 && (await leftovers(r.p)).length > 0, `${why} → 腳本是中文`);
    await r.ctx.close();
  }
  const r = await open(browser, 'zh-TW', { lang: 'en' });
  ok(await r.p.getByText('🛡 Blocked', { exact: false }).count() > 0, '中文瀏覽器但手動選英文 → 英文');
  await r.ctx.close();
  await browser.close();
  console.log(fails ? `${fails} 項失敗` : '全部通過');
  process.exit(fails ? 1 : 0);
})();
