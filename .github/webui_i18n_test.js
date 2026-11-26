// 英文版畫面測試（v1.44.0，docs/i18n/00-英文版規格.md）：語言設成 English，每一頁、每種對話框都打開，
// 看得到（或藏著）的字裡不能還有中文；網站來的標題（translate="no"）、語言選單的「繁體中文」除外。最後切回中文要還原。
// 用法：node .github/webui_i18n_test.js [截圖資料夾]
'use strict';
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');
const { state, setup, pageHtml } = require('./webui_fake.js');

const out = process.argv[2] || 'webui-shots-en';
fs.mkdirSync(out, { recursive: true });
const html = pageHtml();
let fails = 0;
const ok = (c, msg) => { console.log((c ? '✅ ' : '❌ ') + msg); if (!c) fails++; };
// 假資料裡當成「使用者自己取的名字」的中文（節點名稱等）
const NAMES = ['書房電腦', '哈囉', '是我的', '繁體中文', '.雙語.srt'];

async function leftovers(page) {
  return page.evaluate((names) => {
    const CJK = /[㐀-鿿豈-﫿]/;
    const clean = (s) => { for (const n of names) s = s.split(n).join(''); return s; };
    const bad = new Set();
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      const el = n.nodeType === 3 ? n.parentElement : n;
      if (!el || el.closest('[translate="no"], script, style')) continue;
      if (n.nodeType === 3) { const v = clean(n.nodeValue); if (CJK.test(v)) bad.add(v.trim()); }
      else for (const a of ['placeholder', 'title', 'aria-label']) { const v = n.getAttribute(a); if (v && CJK.test(clean(v))) bad.add(a + '=' + v); }
    }
    return [...bad];
  }, NAMES);
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const extra = { lang: 'en' };
  await setup(page, () => state('dark', extra));
  await page.route('**/*', (r) => r.abort());
  await page.goto('about:blank');
  await page.setContent(html);
  await page.waitForFunction(() => window.__ready, null, { timeout: 5000 });
  await page.waitForTimeout(700);
  const all = new Set();
  const check = async (where) => {
    await page.waitForTimeout(150);
    const b = await leftovers(page);
    b.forEach((x) => all.add(x));
    ok(!b.length, `英文：${where}沒有漏翻` + (b.length ? '：' + b.slice(0, 12).map((x) => JSON.stringify(x)).join('、') : ''));
  };
  const modalClose = async () => { if (await page.isVisible('#modal')) await page.click('#mbox .btns button:last-child'); };

  ok(await page.evaluate(() => document.documentElement.lang === 'en'), 'html lang＝en');
  ok((await page.textContent('#ptitle')) === 'Home' && (await page.textContent('#nav')).includes('Downloads'), '標題、選單是英文');
  await page.screenshot({ path: path.join(out, 'home-en.png') });
  await check('首頁');
  await page.click('#bell'); await check('通知中心'); await page.click('#bell');
  await page.click('[data-a="cancel"]'); await check('取消下載對話框'); await modalClose();
  await page.click('#nav a[data-page="about"]'); await check('關於'); await modalClose();
  await page.evaluate(() => { window.__ask = { id: 'a1', title: '還在下載', text: '還有影片在下載，要怎麼做？\n縮到右下角的系統匣會繼續下載，完成會通知你。', buttons: ['縮到系統匣，繼續下載', '取消下載並結束', '回到視窗'] }; });
  await page.waitForSelector('#modal:not([hidden])');
  await check('電腦版問問題');
  await page.click('#mbox button:last-child');
  await page.evaluate(() => { window.__ask = null; });

  await page.click('#nav a[data-page="dl"]');
  await page.waitForTimeout(400);
  await page.fill('#dlurls', 'https://www.xvideos.com/video1/a\nhttps://www.xvideos.com/video2/b');
  ok((await page.textContent('#dlgo')).includes('Add all (2'), '批次按鈕是英文');
  extra.analysis = { id: 'an1', url: 'https://www.xvideos.com/video9/z', state: 'ok', title: '中文標題不翻', duration: '12:34', site: 'xvideos.com',
    pick: 0, downloaded: '11-20 下載過（1080p）', formats: [{ i: 0, label: '1080p', size: '約 1.1 GB', time: '3 分 20 秒', kind: '切片' }, { i: 1, label: '720p', size: '600 MB', time: '2 分鐘', kind: 'MP4 直連' }] };
  await page.waitForSelector('.fmt');
  await page.screenshot({ path: path.join(out, 'dl-en.png') });
  await check('下載管理（含畫質清單）');
  await page.click('#ango'); await check('已經下載過的對話框'); await modalClose();
  extra.analysis = null;
  await page.click('#hlist .row[data-key="hab"]', { button: 'right' }); await check('右鍵選單'); await page.keyboard.press('Escape');
  await page.check('#hlist input[data-sel="hab"]'); await page.click('#bdel'); await check('刪除紀錄對話框'); await modalClose();

  await page.click('#nav a[data-page="set"]');
  await page.waitForTimeout(400);
  ok(await page.locator('#s-lang option').count() === 3 && (await page.inputValue('#s-lang')) === 'en', '設定頁有語言選單、目前是 English');
  await page.screenshot({ path: path.join(out, 'set-en.png'), fullPage: true });
  await check('設定');
  await page.click('#s-report'); await check('智慧分段報告'); await modalClose();
  await page.click('#s-unpair'); await check('取消所有配對對話框'); await modalClose();

  await page.click('#nav a[data-page="nodes"]');
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(out, 'nodes-en.png') });
  await check('Nexus 管理');
  for (const op of ['approve', 'suspend', 'revoke']) {
    const b = page.locator(`#nx-devs button[data-nxdev="${op}"]`);
    if (await b.count()) { await b.first().click(); await check(`節點操作：${op}`); await modalClose(); }
  }
  await page.click('#nx-tasks button[data-nxt="detail"]'); await page.waitForSelector('#modal:not([hidden]) .tbl'); await check('任務詳情'); await modalClose();
  await page.click('#nx-tasks button[data-nxt="cancel"]'); await check('取消任務對話框'); await modalClose();
  await page.click('#nx-audit'); await page.waitForSelector('#modal:not([hidden]) .tbl'); await check('稽核紀錄'); await modalClose();
  await page.click('#nx-new');
  await page.fill('#nf-url', 'https://www.pornhub.com/view_video.php?viewkey=1');
  await check('新增節點任務');
  await page.click('#nf-close');

  await page.click('#nav a[data-page="sec"]');
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(out, 'sec-en.png'), fullPage: true });
  await check('安全與統計');
  await page.click('#sec-clearstats'); await check('清除統計對話框'); await modalClose();
  await page.click('#nav a[data-page="setup"]');
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(out, 'setup-en.png') });
  await check('安裝擋廣告');
  // v1.48.0 稍後清單、找重複、密碼對話框、鎖住畫面
  await page.click('#nav a[data-page="dl"]'); await page.waitForTimeout(500); await check('稍後清單');
  await page.click('#dupbtn'); await page.waitForSelector('#modal:not([hidden]) .dupck'); await check('找重複'); await modalClose();
  await page.click('#nav a[data-page="set"]'); await page.waitForTimeout(400);
  await page.click('#x-lockset'); await check('設定密碼對話框'); await modalClose();
  extra.locked = true; extra.lock = { wait: 30 };
  await page.waitForTimeout(800);
  await page.screenshot({ path: path.join(out, 'locked-en.png') });
  await check('鎖住畫面');
  delete extra.locked; delete extra.lock;
  await page.waitForTimeout(800);

  // 切回中文：畫面上的字要回到中文
  extra.lang = 'zh';
  await page.click('#nav a[data-page="home"]');
  await page.waitForTimeout(800);
  ok((await page.textContent('#ptitle')) === '首頁' && (await page.textContent('#nav')).includes('下載管理') && (await page.getAttribute('#urls', 'placeholder')).includes('貼上')
    && await page.evaluate(() => document.documentElement.lang === 'zh-Hant'), '切回中文：選單、標題、提示字都還原');
  ok((await page.textContent('#list')).includes('等待中'), '切回中文：任務狀態是中文');
  extra.lang = 'en';
  await page.waitForTimeout(800);
  ok((await page.textContent('#ptitle')) === 'Home', '再切英文也可以');
  ok(errors.length === 0, '沒有 JavaScript 錯誤' + (errors.length ? '：' + errors.join(' | ') : ''));
  if (all.size) fs.writeFileSync(path.join(out, 'leftovers.json'), JSON.stringify([...all], null, 1));
  await browser.close();
  console.log(fails ? `${fails} 項失敗` : '全部通過');
  process.exit(fails ? 1 : 0);
})();
