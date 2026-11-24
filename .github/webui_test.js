// 新介面（webui/）畫面測試：用假的 pywebview.api 餵資料，檢查畫面、按鈕、跳脫網站文字、不連外，並截圖
// 用法：node .github/webui_test.js [截圖資料夾]
'use strict';
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const dir = path.join(__dirname, '..', 'webui');
const read = (f) => fs.readFileSync(path.join(dir, f), 'utf8');
const html = read('index.html').replace('/*CSS*/', () => read('app.css')).replace('/*JS*/', () => read('app.js'));
const out = process.argv[2] || 'webui-shots';
fs.mkdirSync(out, { recursive: true });

const EVIL = '<img src=x onerror="window.__pwned=1">"><script>window.__pwned=2</script>';
function state(theme, extra = {}) {
  return Object.assign({
    version: '1.36.0', theme, paused: false, bridge: true,
    kpis: [
      { icon: '⬇', label: '進行中下載', value: 2, unit: '部', sub: '目前 48.3 Mbps' },
      { icon: '✓', label: '已完成下載', value: 128, unit: '部', sub: '今天 3 部' },
      { icon: '🖧', label: 'Nexus 節點在線', value: '1 / 2', unit: '', sub: 'Master 執行中' },
      { icon: '💾', label: '可用空間', value: '312', unit: 'GB', sub: '下載資料夾在 D:' },
    ],
    tasks: [
      { key: 'q1', title: '很長的影片標題 ' + 'A'.repeat(80), site: 'pornhub.com', quality: '1080p', size: '1.2 GB', st: 'run', progress: 45.2, speed: '48.3 Mbps', cancel: true, remove: false },
      { key: 'q2', title: EVIL, site: 'xvideos.com', quality: '720p', st: 'wait', progress: 0, remove: true },
      { key: 'q3', title: '暫停的影片', site: 'xnxx.com', quality: '480p', st: 'pause', progress: 30, remove: true },
      { key: 'hab', title: '已完成的影片', site: 'youporn.com', quality: '1080p', size: '800 MB', time: '11-24 20:31', st: 'done', progress: 100, open: true },
      { key: 'hcd', title: '節點傳回的影片', site: 'redtube.com', st: 'done', progress: 100, open: true, node: true },
      { key: 'hef', title: '失敗的影片', site: 'spankbang.com', st: 'fail', progress: 0, note: 'HTTP 404：影片不存在' },
    ],
    nexus: { enabled: true, running: true, online: 1, total: 2, tasks: 1, devices: [{ name: '書房電腦', online: true, state: '連線中' }, { name: EVIL, online: false, state: '離線' }] },
    notices: [{ icon: '✅', text: '下載完成：' + EVIL, time: '20:31' }],
    update: { tag: '1.37.0' }, ask: null,
  }, extra);
}

let fails = 0;
const ok = (c, msg) => { console.log((c ? '✅ ' : '❌ ') + msg); if (!c) fails++; };

(async () => {
  const browser = await chromium.launch();
  for (const theme of ['dark', 'light']) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const requests = [];
    page.on('request', (r) => { if (!/^(data|about):/.test(r.url())) requests.push(r.url()); });
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    await page.exposeFunction('__state', () => state(theme, globalThis.__extra || {}));
    await page.addInitScript(() => {
      window.__acts = [];
      window.__ask = null;
      window.pywebview = { api: {
        state: async () => Object.assign(await window.__state(), window.__ask ? { ask: window.__ask } : {}),
        static: async () => ({ news: [{ ver: '1.36.0', title: '新介面預覽', items: ['左側選單', '數據卡'] }, { ver: '1.35.0', title: '系統匣', items: ['一直都在'] }] }),
        act: async (n, a) => { window.__acts.push([n, a]); return { ok: true, msg: 'ok:' + n }; },
        ready: () => { window.__ready = true; return true; },
      } };
    });
    await page.route('**/*', (r) => r.abort());       // 有任何連外請求就擋下（上面另外記錄）
    await page.goto('about:blank');
    await page.setContent(html);
    await page.waitForFunction(() => window.__ready, null, { timeout: 5000 });
    await page.waitForTimeout(700);

    ok(await page.locator('.kpi').count() === 4, `${theme}：4 張數據卡`);
    ok(await page.locator('#list .row').count() === 6, `${theme}：任務列表 6 筆`);
    ok(await page.evaluate(() => !window.__pwned && !document.querySelector('#list img, #nexus img, #notices img')), `${theme}：網站來的標題被跳脫，沒有執行`);
    ok((await page.locator('.news').count()) === 3, `${theme}：最新消息 2 則＋有新版本`);
    ok(await page.evaluate((t) => document.body.className === t, theme), `${theme}：主題套用`);
    await page.screenshot({ path: path.join(out, `home-${theme}.png`) });

    if (theme === 'dark') {
      await page.click('#chips button[data-f="done"]');
      ok(await page.locator('#list .row').count() === 2, '篩選「已完成」剩 2 筆');
      await page.click('#chips button[data-f="active"]');
      ok(await page.locator('#list .row').count() === 2, '篩選「進行中」剩 2 筆（下載中＋等待中）');
      await page.click('#chips button[data-f="all"]');
      await page.fill('#urls', 'https://www.pornhub.com/view_video.php?viewkey=x');
      await page.keyboard.press('Enter');
      await page.waitForTimeout(200);
      await page.click('[data-act="open_folder"]');
      await page.click('#nav a[data-page="set"]');
      await page.click('[data-a="play"]');
      await page.waitForTimeout(200);
      const acts = await page.evaluate(() => window.__acts);
      ok(JSON.stringify(acts[0]) === JSON.stringify(['add', 'https://www.pornhub.com/view_video.php?viewkey=x']), '貼網址按 Enter → add');
      ok(acts.some((a) => a[0] === 'open_folder') && acts.some((a) => a[0] === 'old' && a[1] === 'set') && acts.some((a) => a[0] === 'play' && a[1] === 'hab'),
        '快速功能、選單、播放都送出正確動作');
      ok(await page.inputValue('#urls') === '', '送出後清空輸入框');
      // 取消要先確認
      await page.click('[data-a="cancel"]');
      ok(await page.isVisible('#modal'), '按 ✕ 先問要不要取消');
      await page.click('#mbox button:has-text("不要")');
      ok(!(await page.evaluate(() => window.__acts.some((a) => a[0] === 'cancel'))), '選「不要」就不取消');
      // 電腦版問問題（關視窗時還在下載）
      await page.evaluate(() => { window.__ask = { id: 'a1', title: '還在下載', text: '要怎麼做？', buttons: ['縮到系統匣，繼續下載', '取消下載並結束', '回到視窗'] }; });
      await page.waitForSelector('#modal:not([hidden])', { timeout: 2000 });
      await page.screenshot({ path: path.join(out, 'ask-close.png') });
      await page.click('#mbox button:has-text("回到視窗")');
      await page.waitForTimeout(200);
      ok(await page.evaluate(() => window.__acts.some((a) => a[0] === 'answer' && a[1].id === 'a1' && a[1].choice === 2)), '回答會送回電腦版');
      await page.click('#bell');
      ok(await page.isVisible('#panel'), '通知中心打得開');
      await page.screenshot({ path: path.join(out, 'notices.png') });
      await page.setViewportSize({ width: 1000, height: 760 });
      await page.waitForTimeout(200);
      await page.screenshot({ path: path.join(out, 'home-narrow.png') });
    }
    ok(requests.length === 0, `${theme}：沒有任何連外請求` + (requests.length ? '：' + requests.join(', ') : ''));
    ok(errors.length === 0, `${theme}：沒有 JavaScript 錯誤` + (errors.length ? '：' + errors.join(' | ') : ''));
    await page.close();
  }
  await browser.close();
  console.log(fails ? `${fails} 項失敗` : '全部通過');
  process.exit(fails ? 1 : 0);
})();
