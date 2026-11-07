// 配對密碼「攻擊模擬」：接上真的電腦版連線服務，讓腳本走真的配對流程，
// 同時讓「網站」（攻擊者）在同一個網頁裡用各種方法偷配對密碼，再拿偷到的東西冒充腳本叫電腦版下載。
// 預期：網站拿不到密碼、也冒充不了；只有腳本自己送的那一筆會被電腦版收下。
// 用法：node .github/attack_test.js video-helper.user.js
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const crypto = require('crypto');
const { spawn } = require('child_process');
const { chromium } = require('playwright');

const src = fs.readFileSync(process.argv[2] || 'video-helper.user.js', 'utf8');
const enqLog = path.join(os.tmpdir(), 'vh_enqueue.log');
const notice = (t, m) => console.log(`::notice title=${t}::${m}`);
const error = (t, m) => console.log(`::error title=${t}::${m}`);

// 1. 啟動真的電腦版連線服務
const py = spawn(process.env.PYTHON || 'python3', [path.join(__dirname, 'attack_bridge.py'), enqLog], { stdio: ['pipe', 'pipe', 'inherit'] });
const ready = new Promise(res => py.stdout.once('data', d => res(JSON.parse(String(d).split('\n')[0]))));

// 腳本的 GM_xmlhttpRequest＝「擴充功能」送的請求：在 Node 這邊真的送出去，帶擴充功能的 Origin
// 用一次性暗號擋住網站直接呼叫（真實世界裡網站也碰不到擴充功能的 GM_*）
const NONCE = crypto.randomBytes(16).toString('hex');
let realToken = '';
function extRequest(o) {
  return new Promise(resolve => {
    const u = new URL(o.url);
    if (u.hostname !== '127.0.0.1') return resolve({ status: 0, text: '' });
    const req = http.request({ host: '127.0.0.1', port: u.port, path: u.pathname + u.search, method: o.method || 'GET',
      headers: { ...(o.headers || {}), Origin: 'chrome-extension://violentmonkey', ...(o.data ? { 'Content-Length': Buffer.byteLength(o.data) } : {}) } }, r => {
      let b = ''; r.on('data', c => b += c); r.on('end', () => {
        if (u.pathname === '/pair') { try { realToken = JSON.parse(b).token || realToken; } catch (e) {} }
        resolve({ status: r.statusCode, text: b });
      });
    });
    req.on('error', () => resolve({ status: 0, text: '' }));
    if (o.data) req.write(o.data);
    req.end();
  });
}

// 腳本外面包一層（跟暴力猴一樣）：GM_* 只在這個範圍裡，網站拿不到
const wrapped = `(() => {
  // 模擬「擴充功能的邊界」：真的暴力猴裡，腳本和擴充功能之間的資料網站看不到。
  // 這裡用 Playwright 傳資料時會經過網頁的 JSON（網站換得掉），所以先用暗號 XOR 加密，網站只看得到亂碼
  const __net = window.__vhNet, __K = '${NONCE}';
  const __enc = t => { let o = ''; for (let i = 0; i < t.length; i++) o += (t.charCodeAt(i) ^ __K.charCodeAt(i % __K.length)).toString(16).padStart(4, '0'); return o; };
  const __dec = h => { let o = ''; for (let i = 0; i < h.length; i += 4) o += String.fromCharCode(parseInt(h.substr(i, 4), 16) ^ __K.charCodeAt((i / 4) % __K.length)); return o; };
  const __J = JSON.stringify, __P = JSON.parse, __store = new Map();   // 暴力猴把資料存在擴充功能裡，不是網頁的物件
  const GM_xmlhttpRequest = o => { __net('${NONCE}', __enc(__J({ url: o.url, method: o.method, headers: o.headers, data: o.data })))
      .then(h => { const r = __P(__dec(h)); o.onload && o.onload({ status: r.status, responseText: r.text, response: r.text, responseHeaders: '' }); }); return { abort() {} }; };
  const GM_download = () => {};
  const GM_getValue = (k, d) => (__store.has(k) ? __store.get(k) : d);
  const GM_setValue = (k, v) => { __store.set(k, v); };
  const GM_registerMenuCommand = () => {};
  const GM_setClipboard = () => {};
  const GM_info = { script: { version: 'attack-test' }, scriptHandler: 'test' };
  const unsafeWindow = window;
  window.muxjs = window.muxjs || {};
  ${src}
})();`;

// 攻擊者：網站自己的程式（在腳本之後、使用者按按鈕之前就埋好陷阱）
const ATTACK = `
window.__loot = [];
const take = (how, v) => { try { if (v != null) window.__loot.push([how, String(typeof v === 'string' ? v : JSON.stringify(v)).slice(0, 2000)]); } catch (e) {} };
// 陷阱 1：在所有物件的原型上埋 setter，腳本寫 headers['X-VH-Token'] = 密碼 就會被攔
for (const k of ['X-VH-Token', 'token', 'bridgeToken']) {
  Object.defineProperty(Object.prototype, k, { configurable: true, set(v) { take('原型 setter ' + k, v); Object.defineProperty(this, k, { value: v, writable: true, enumerable: true, configurable: true }); } });
}
// 陷阱 2～4：換掉 JSON、prompt、String.replace，看腳本有沒有把密碼或配對碼經過它們
const P = JSON.parse, S = JSON.stringify, R = String.prototype.replace, pr = window.prompt;
JSON.parse = function (t) { take('JSON.parse', t); return P.apply(this, arguments); };
JSON.stringify = function (v) { const r = S.apply(this, arguments); take('JSON.stringify', r); return r; };
String.prototype.replace = function () { take('String.replace', String(this)); return R.apply(this, arguments); };
window.prompt = function () { const r = pr.apply(this, arguments); take('prompt', r); return r; };
// 陷阱 5：讓 Set.has 永遠回 false（腳本如果用 Set 判斷「這是祕密不要存」，就會被騙去存到 localStorage）
Set.prototype.has = () => false;
`;

const PAGE = `<!doctype html><html><head><title>attack test</title>
<script>var flashvars_1 = { video_title: 'attack test', mediaDefinitions: [
  { format: 'hls', videoUrl: 'https://ev-h.phncdn.com/hls/attack/master.m3u8', quality: '720' } ] };</script>
<script>${ATTACK}</script></head><body><div id="player"><video></video></div></body></html>`;

(async () => {
  const { port, code } = await ready;
  notice('攻擊模擬', `電腦版連線服務在 127.0.0.1:${port}`);
  const browser = await chromium.launch();
  const ctx = await browser.newContext();
  const p = await ctx.newPage();
  const enc = t => { let o = ''; for (let i = 0; i < t.length; i++) o += (t.charCodeAt(i) ^ NONCE.charCodeAt(i % NONCE.length)).toString(16).padStart(4, '0'); return o; };
  const dec = h => { let o = ''; for (let i = 0; i < h.length; i += 4) o += String.fromCharCode(parseInt(h.substr(i, 4), 16) ^ NONCE.charCodeAt((i / 4) % NONCE.length)); return o; };
  await p.exposeFunction('__vhNet', async (nonce, h) => nonce === NONCE ? enc(JSON.stringify(await extRequest(JSON.parse(dec(h))))) : '');
  await ctx.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.hostname === 'www.pornhub.com' && u.pathname === '/view_video.php') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: PAGE });
    if (u.hostname === '127.0.0.1') return route.continue();                  // 攻擊者直接打電腦版：讓它真的打
    return route.abort();
  });
  p.on('dialog', d => d.type() === 'prompt' ? d.accept(code) : d.dismiss());   // 使用者輸入配對碼
  await p.addInitScript(wrapped);
  await p.goto('https://www.pornhub.com/view_video.php?viewkey=attack');
  await p.waitForTimeout(2000);

  // 2. 使用者操作：打開下載清單 → 按「🖥 送到電腦版」（腳本會跳出配對碼視窗 → 配對 → 送出）
  let sent = false;
  for (let i = 0; i < 6 && !sent; i++) {
    const pc = p.getByText('🖥 送到電腦版', { exact: true }).first();
    if (await pc.count() && await pc.isVisible()) { await pc.click(); sent = true; break; }
    for (const el of await p.$$('.vh-ui *')) { try { if (await el.isVisible()) await el.click({ timeout: 200, force: true }); } catch (e) {} }
    await p.waitForTimeout(500);
  }
  await p.waitForTimeout(2500);
  const legit = fs.readFileSync(enqLog, 'utf8').trim().split('\n').filter(Boolean).length;
  if (!sent || !realToken || legit !== 1) {
    error('攻擊模擬', `沒走到真的配對流程（按到按鈕 ${sent}、拿到密碼 ${!!realToken}、電腦版收到 ${legit} 筆）`);
    py.stdin.end(); await browser.close(); process.exit(1);
  }
  notice('攻擊模擬 步驟 1', '腳本用配對碼完成真的配對，電腦版收到 1 筆下載（正常流程沒問題）');

  // 3. 攻擊者翻遍網站碰得到的所有地方
  const found = await p.evaluate(tok => {
    const where = [];
    const has = v => { try { return String(v).includes(tok); } catch (e) { return false; } };
    for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); if (has(localStorage.getItem(k))) where.push('localStorage.' + k); }
    for (let i = 0; i < sessionStorage.length; i++) { const k = sessionStorage.key(i); if (has(sessionStorage.getItem(k))) where.push('sessionStorage.' + k); }
    if (has(document.cookie)) where.push('cookie');
    if (has(document.documentElement.outerHTML)) where.push('網頁內容');
    for (const k of Object.getOwnPropertyNames(window)) { try { const v = window[k]; if (typeof v === 'string' && has(v)) where.push('window.' + k); else if (v && typeof v === 'object' && !(v instanceof Node) && v !== window && has(JSON.stringify(v))) where.push('window.' + k); } catch (e) {} }
    for (const [how, v] of window.__loot) if (has(v)) where.push('陷阱：' + how);
    return where;
  }, realToken);
  const traps = await p.evaluate(() => [...new Set(window.__loot.map(x => x[0]))]);

  // 4. 攻擊者冒充腳本叫電腦版下載：用偷到的（或猜的）密碼，三種送法都試
  const forge = await p.evaluate(async ({ port, guess }) => {
    const url = `http://127.0.0.1:${port}/enqueue`;
    const body = JSON.stringify({ url: 'https://www.pornhub.com/view_video.php?viewkey=evil' });
    const out = [];
    const tries = [
      ['fetch＋自訂標頭', () => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-VH': '1', 'X-VH-Token': guess }, body })],
      ['fetch no-cors（純文字）', () => fetch(url, { method: 'POST', mode: 'no-cors', headers: { 'Content-Type': 'text/plain' }, body })],
      ['表單 POST', () => new Promise(r => { const f = document.createElement('form'); f.method = 'POST'; f.action = url; f.target = 'x'; const i = document.createElement('iframe'); i.name = 'x'; document.body.append(i, f); f.submit(); setTimeout(r, 800); })],
    ];
    for (const [name, fn] of tries) { try { const r = await fn(); out.push(name + '：' + (r && r.type === 'opaque' ? '送出（看不到回應）' : r && r.status ? '回應 ' + r.status : '送出')); } catch (e) { out.push(name + '：被瀏覽器擋下'); } }
    return out;
  }, { port, guess: found.length ? realToken : 'guess-' + Math.random() });
  await p.waitForTimeout(1500);
  const total = fs.readFileSync(enqLog, 'utf8').trim().split('\n').filter(Boolean).length;

  notice('攻擊模擬 步驟 2', `網站埋的陷阱有被觸發的：${traps.join('、') || '無'}`);
  notice('攻擊模擬 步驟 3', `翻遍 localStorage、sessionStorage、Cookie、網頁內容、window 上所有東西、陷阱紀錄：${found.length ? '❌ 找到密碼在 ' + found.join('、') : '找不到配對密碼'}`);
  notice('攻擊模擬 步驟 4', `冒充腳本叫電腦版下載：${forge.join('｜')}｜電腦版總共收到 ${total} 筆（應該只有腳本那 1 筆）`);
  py.stdin.end();
  await browser.close();
  const ok = found.length === 0 && total === 1;
  (ok ? notice : error)('攻擊模擬', ok ? '✅ 網站拿不到配對密碼，也冒充不了腳本' : '❌ 攻擊成功，要修');
  process.exit(ok ? 0 : 1);
})().catch(e => { error('攻擊模擬', String(e)); py.kill(); process.exit(1); });
