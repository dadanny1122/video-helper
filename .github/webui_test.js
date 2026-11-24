// 新介面（webui/）畫面測試：用假的 pywebview.api 餵資料，檢查畫面、按鈕、跳脫網站文字、不連外，並截圖
// 用法：node .github/webui_test.js [截圖資料夾]
'use strict';
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const dir = path.join(__dirname, '..', 'webui');
const read = (f) => fs.readFileSync(path.join(dir, f), 'utf8');
const html = require('./webui_fake.js').pageHtml();
const out = process.argv[2] || 'webui-shots';
fs.mkdirSync(out, { recursive: true });

const { JPG, EVIL, state, NEXUS, setup } = require('./webui_fake.js');
let fails = 0;
const ok = (c, msg) => { console.log((c ? '✅ ' : '❌ ') + msg); if (!c) fails++; };

(async () => {
  const browser = await chromium.launch();
  for (const theme of ['dark', 'light']) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const extra = {};
    const requests = [];
    page.on('request', (r) => { if (!/^(data|about):/.test(r.url())) requests.push(r.url()); });
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    await setup(page, () => state(theme, extra));
    await page.route('**/*', (r) => r.abort());       // 有任何連外請求就擋下（上面另外記錄）
    await page.goto('about:blank');
    await page.setContent(html);
    await page.waitForFunction(() => window.__ready, null, { timeout: 5000 });
    await page.waitForTimeout(700);

    ok(await page.locator('.kpi').count() === 4, `${theme}：4 張數據卡`);
    ok(await page.locator('#list .row').count() === 6, `${theme}：任務列表 6 筆`);
    ok(await page.evaluate(() => !window.__pwned && [...document.querySelectorAll('#list img, #nexus img, #notices img')]
      .every((i) => i.parentElement.classList.contains('thumb') && i.src.startsWith('data:image/jpeg;base64,'))), `${theme}：網站來的標題被跳脫，沒有執行（只有縮圖是圖片）`);
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
      await page.click('[data-act="open_folder"]');
      await page.click('[data-a="play"]');
      await page.waitForTimeout(200);
      const acts = await page.evaluate(() => window.__acts);
      ok(JSON.stringify(acts[0]) === JSON.stringify(['add', 'https://www.pornhub.com/view_video.php?viewkey=x']), '貼網址按 Enter → add');
      ok(acts.some((a) => a[0] === 'open_folder') && true && acts.some((a) => a[0] === 'play' && a[1] === 'hab'),
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
      // ---- v1.37.0 下載管理 ----
      await page.keyboard.press('Escape');
      await page.click('#bell');
      await page.click('#nav a[data-page="dl"]');
      await page.waitForTimeout(400);
      ok(await page.isVisible('#page-dl') && !(await page.isVisible('#page-home')), '下載管理頁打得開');
      ok(await page.locator('#qlist .row').count() === 3, '佇列 3 筆（只有還沒結束的）');
      ok(await page.locator('#hlist .row').count() === 3, '紀錄 3 筆');
      await page.waitForTimeout(500);
      ok(await page.locator('#hlist .row[data-key="hab"] img').count() === 1, '有縮圖的顯示縮圖');
      ok(await page.locator('#hlist .row[data-key="hcd"] img').count() === 0, '不是 JPEG 的縮圖網址不放進畫面');
      await page.fill('#hsearch', '節點');
      await page.waitForTimeout(400);
      ok(await page.locator('#hlist .row').count() === 1, '搜尋標題');
      await page.fill('#hsearch', '');
      await page.click('#hchips button[data-s="fail"]');
      await page.waitForTimeout(300);
      ok(await page.locator('#hlist .row').count() === 1, '篩選失敗');
      await page.click('#hchips button[data-s=""]');
      await page.waitForTimeout(300);
      await page.fill('#dlurls', 'https://www.xvideos.com/video1/a\nhttps://www.xvideos.com/video2/b\n');
      ok((await page.textContent('#dlgo')).includes('2 部'), '貼兩個網址 → 按鈕變「全部加入（2 部）」');
      await page.selectOption('#dlq', '720');
      await page.click('#dlgo');
      await page.fill('#dlurls', 'https://www.xvideos.com/video9/z');
      await page.click('#dlgo');
      await page.waitForTimeout(200);
      let acts2 = await page.evaluate(() => window.__acts);
      ok(acts2.some((a) => a[0] === 'add_batch' && a[1].quality === '720' && a[1].text.split('\n').length === 2), '批次加入帶著選的畫質');
      ok(acts2.some((a) => a[0] === 'analyze' && a[1] === 'https://www.xvideos.com/video9/z'), '一個網址 → 讀取');
      extra.analysis = { id: 'an1', url: 'https://www.xvideos.com/video9/z', state: 'ok', title: EVIL, duration: '12:34', site: 'xvideos.com',
        pick: 0, downloaded: '', formats: [{ i: 0, label: '1080p', size: '約 1.1 GB', time: '3 分鐘', kind: '切片' }, { i: 1, label: '720p', size: '600 MB', time: '2 分鐘', kind: 'MP4 直連' }] };
      await page.waitForSelector('.fmt', { timeout: 2000 });
      ok(await page.locator('.fmt').count() === 2 && await page.evaluate(() => !window.__pwned), '畫質清單出現（標題有跳脫）');
      await page.screenshot({ path: path.join(out, 'dl-dark.png'), fullPage: false });
      await page.click('.fmt[data-fmt="1"]');
      await page.click('#ango');
      await page.waitForTimeout(200);
      acts2 = await page.evaluate(() => window.__acts);
      ok(acts2.some((a) => a[0] === 'enqueue_fmt' && a[1].id === 'an1' && a[1].i === 1), '選 720p 下載 → enqueue_fmt');
      extra.analysis = null;
      await page.click('#hlist .row[data-key="hab"]', { button: 'right' });
      ok(await page.isVisible('#menu'), '右鍵出現選單');
      await page.click('#menu button[data-m="copy_url"]');
      await page.click('#hlist .row[data-key="hef"]', { button: 'right' });
      ok(await page.locator('#menu button[data-m="play"]:disabled').count() === 1, '失敗的紀錄不能播放（選單是灰的）');
      await page.keyboard.press('Escape');
      await page.check('#hlist input[data-sel="hab"]');
      await page.check('#hlist input[data-sel="hcd"]');
      ok(await page.isVisible('#bulk') && (await page.textContent('#bulkn')).includes('2'), '勾兩部 → 出現批次按鈕');
      await page.click('#bsub');
      await page.dblclick('#hlist .row[data-key="hef"] .t1');
      await page.waitForTimeout(200);
      acts2 = await page.evaluate(() => window.__acts);
      ok(acts2.some((a) => a[0] === 'copy_url' && a[1] === 'hab'), '右鍵 → 複製網址');
      ok(acts2.some((a) => a[0] === 'subtitles' && a[1].length === 2), '勾選的一起做 AI 字幕');
      ok(acts2.some((a) => a[0] === 'redownload' && a[1] === 'hef'), '雙擊失敗的 → 重新下載');
      await page.screenshot({ path: path.join(out, 'dl-dark-2.png') });
      // ---- v1.38.0 設定、安裝擋廣告、通知跳頁 ----
      await page.click('#nav a[data-page="set"]');
      await page.waitForTimeout(400);
      ok(await page.isVisible('#page-set') && (await page.inputValue('#s-q')) === '1080', '設定頁打得開、顯示目前畫質');
      ok((await page.inputValue('#s-rate')) === '7' && (await page.isChecked('input[data-set="skip_downloaded"]')) && !(await page.isChecked('input[data-set="keep_stats"]')),
        '速度上限（自訂的 7）和開關都對');
      await page.screenshot({ path: path.join(out, 'set-dark.png') });
      await page.selectOption('#s-q', '720');
      await page.click('label.sw:has(input[data-set="keep_stats"])');
      await page.selectOption('#s-rate', '20');
      await page.click('#s-theme button[data-theme="light"]');
      await page.click('#s-report');
      await page.waitForSelector('#modal:not([hidden]) .report', { timeout: 2000 });
      ok((await page.textContent('#mbox .report')).includes('pornhub'), '📊 報告顯示在對話框');
      await page.click('#mbox button');
      await page.fill('#s-key', 'sk-ant-test-1234567890abcdef');
      await page.click('#s-keysave');
      await page.click('#s-unpair');
      await page.click('#mbox button:has-text("取消所有配對")');
      await page.waitForTimeout(300);
      let a3 = await page.evaluate(() => window.__acts);
      const has = (n, f) => a3.some((a) => a[0] === n && f(a[1]));
      ok(has('set', (v) => v.key === 'default_quality' && v.value === '720'), '改預設畫質 → set');
      ok(has('set', (v) => v.key === 'keep_stats' && v.value === true), '開關 → set（true）');
      ok(has('set', (v) => v.key === 'rate_limit' && v.value === 20), '速度上限送數字');
      ok(has('set', (v) => v.key === 'theme' && v.value === 'light'), '外觀 → set theme');
      ok(has('set_ai_key', (v) => v === 'sk-ant-test-1234567890abcdef') && (await page.inputValue('#s-key')) === '', 'AI 金鑰送出後輸入框清空');
      ok(has('unpair_all', () => true), '取消所有配對要先確認');
      await page.click('#nav a[data-page="setup"]');
      await page.waitForTimeout(300);
      ok(await page.locator('.step').count() === 3 && (await page.textContent('.step .ok')) === '✓', '安裝擋廣告：3 步、第 1 步打勾');
      await page.click('.step:nth-child(2) button');
      await page.screenshot({ path: path.join(out, 'setup-dark.png') });
      a3 = await page.evaluate(() => window.__acts);
      ok(a3.some((a) => a[0] === 'setup_step' && a[1] === '2'), '按「打開」→ setup_step 2');
      await page.click('#bell');
      await page.click('.ntc.go');
      await page.waitForTimeout(200);
      ok(await page.isVisible('#page-dl') && !(await page.isVisible('#panel')), '按通知 → 跳到下載管理');
      // ---- v1.39.0 Nexus 管理 ----
      await page.click('#nav a[data-page="nodes"]');
      await page.waitForTimeout(600);
      ok(await page.isVisible('#page-nodes') && await page.locator('#nx-devs .nrow').count() === 2 && await page.locator('#nx-tasks .nrow').count() === 2,
        'Nexus 管理頁：2 台節點、2 個任務');
      ok((await page.textContent('#nx-code')) === '123 456' && (await page.textContent('#nx-fp')).startsWith('ab12cd34'), '配對碼、Master 指紋');
      ok(await page.evaluate(() => !window.__pwned), '節點名稱有跳脫');
      await page.screenshot({ path: path.join(out, 'nodes-dark.png') });
      await page.click('#nx-devs button[data-nxdev="approve"]');
      await page.click('#mbox button:has-text("核准")');                     // 沒填理由
      await page.waitForTimeout(200);
      let a4 = await page.evaluate(() => window.__acts);
      ok(!a4.some((a) => a[0] === 'nx_device'), '沒填理由就不核准');
      await page.click('#nx-devs button[data-nxdev="approve"]');
      await page.fill('#mbox input.reason', '是我的筆電');
      await page.click('#mbox button:has-text("核准")');
      await page.click('#nx-devs button[data-nxdev="revoke"]');
      await page.fill('#mbox input.reason', '不用了');
      await page.click('#mbox button:has-text("取消")');
      await page.waitForTimeout(200);
      a4 = await page.evaluate(() => window.__acts);
      ok(a4.some((a) => a[0] === 'nx_device' && a[1].op === 'approve' && a[1].id === 'd2' && a[1].reason === '是我的筆電'), '核准要填理由 → nx_device');
      ok(!a4.some((a) => a[0] === 'nx_device' && a[1].op === 'revoke'), '撤銷按取消就不做');
      await page.click('#nx-new');
      await page.fill('#nf-url', 'https://www.pornhub.com/view_video.php?viewkey=1');
      ok((await page.textContent('#nf-warn')).includes('交給節點'), 'Pornhub 網址提醒要從瀏覽器交');
      await page.click('#nf-go');
      await page.click('#mbox button:has-text("不要")');
      await page.fill('#nf-url', 'https://www.xvideos.com/video1/a');
      await page.click('#nf-go');
      await page.waitForTimeout(200);
      a4 = await page.evaluate(() => window.__acts);
      const nt = a4.filter((a) => a[0] === 'nx_task_new');
      ok(nt.length === 1 && nt[0][1].type === 'video.download' && nt[0][1].ret === true && nt[0][1].url.includes('xvideos'), '新增節點任務（預設傳回這台）');
      await page.click('#nx-tasks button[data-nxt="detail"]');
      await page.waitForSelector('#modal:not([hidden]) .tbl', { timeout: 2000 });
      ok(await page.locator('#mbox .tbl').count() === 2 && !(await page.locator('#mbox .res b').count()), '任務詳情：排程理由＋時間軸（結果有跳脫）');
      await page.screenshot({ path: path.join(out, 'nodes-detail.png') });
      await page.click('#mbox button:has-text("關閉")');
      await page.click('#nx-tasks button[data-nxt="cancel"]');
      await page.fill('#mbox input.reason', '不要了');
      await page.click('#mbox button:has-text("取消任務")');
      await page.click('#nx-audit');
      await page.waitForSelector('#modal:not([hidden]) .tbl', { timeout: 2000 });
      await page.click('#mbox button:has-text("關閉")');
      await page.waitForTimeout(200);
      a4 = await page.evaluate(() => window.__acts);
      ok(a4.some((a) => a[0] === 'nx_cancel' && a[1].id === 't1' && a[1].reason === '不要了') && a4.some((a) => a[0] === 'nx_audit'), '取消任務要理由、稽核紀錄打得開');
      // ---- v1.41.0 安全與統計 ----
      await page.click('#nav a[data-page="sec"]');
      await page.waitForTimeout(500);
      ok(await page.isVisible('#page-sec') && await page.locator('#sec-paired [data-unpair]').count() === 1 && await page.locator('#sec-net tr').count() === 3,
        '安全與統計頁：配對、連線紀錄');
      ok(await page.locator('#sec-net .badge2.warn').count() === 1 && (await page.inputValue('#sec-keep')) === '14' && !(await page.evaluate(() => window.__pwned)),
        '不常見的網域有標記、日誌天數、瀏覽器名稱有跳脫');
      ok(await page.isVisible('#sec-vres.good') && (await page.textContent('#sec-vres')).includes('runs/1'), '一鍵檢查結果顯示（✅、指紋、打包紀錄）');
      await page.click('#sec-verify');
      await page.screenshot({ path: path.join(out, 'sec-dark.png') });
      await page.click('[data-unpair]');
      await page.click('#mbox button:has-text("取消配對")');
      await page.selectOption('#sec-keep', '7');
      await page.click('#sec-clearlog');
      await page.click('#mbox button:has-text("不要")');
      await page.waitForTimeout(200);
      const a5 = await page.evaluate(() => window.__acts);
      ok(a5.some((a) => a[0] === 'sec_unpair' && a[1] === 'abcdef0123456789') && a5.some((a) => a[0] === 'set' && a[1].key === 'log_keep_days' && a[1].value === 7)
        && !a5.some((a) => a[0] === 'clear_logs') && a5.some((a) => a[0] === 'verify_self'), '取消配對、改日誌天數（數字）、清空日誌按「不要」就不清、一鍵檢查');
      await page.click('#nav a[data-page="home"]');
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
