// S3 相容性：腳本 × 電腦版（真的 Bridge、真的配對）
// 用法：
//   node .github/compat_test.js browser <腳本檔> <電腦版原始碼資料夾> <標題>   → C1／C2：送到電腦版、查進度
//   node .github/compat_test.js legacy  <腳本檔> <電腦版原始碼資料夾>          → C4：舊電腦版功能對照表
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const { spawn } = require('child_process');

const [mode, scriptFile, bridgeSrc, label = mode] = process.argv.slice(2);
const src = fs.readFileSync(scriptFile, 'utf8');
const notice = (t, m) => console.log(`::notice title=${t}::${m}`);
const error = (t, m) => console.log(`::error title=${t}::${m}`);
const scriptVer = (src.match(/@version\s+(\S+)/) || [])[1];

function startBridge() {
  const log = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'vh-compat-')), 'bridge.log');
  const py = spawn(process.env.PYTHON || 'python3', ['-I', path.join(__dirname, 'compat_bridge.py'), bridgeSrc, log],
    { stdio: ['pipe', 'pipe', 'inherit'] });
  const ready = new Promise((res, rej) => {
    let buf = '';
    py.stdout.on('data', d => { buf += d; const i = buf.indexOf('\n'); if (i >= 0) res(JSON.parse(buf.slice(0, i))); });
    py.on('exit', c => rej(new Error('電腦版連線服務啟動失敗（' + c + '）')));
  });
  const read = () => fs.readFileSync(log, 'utf8').trim().split('\n').filter(Boolean).map(l => JSON.parse(l));
  return { py, ready, read };
}

function get(port, p) {
  return new Promise(res => {
    http.get({ host: '127.0.0.1', port, path: p, headers: { 'X-VH': '1', Origin: 'chrome-extension://violentmonkey' } }, r => {
      let b = ''; r.on('data', c => b += c); r.on('end', () => { try { res(JSON.parse(b)); } catch (e) { res({}); } });
    }).on('error', () => res({}));
  });
}

// ---------- C1／C2：真的瀏覽器流程 ----------
async function browser() {
  const { chromium } = require('playwright');
  const br = startBridge();
  const { port, code, version } = await br.ready;
  const ping = await get(port, '/ping');
  const seen = [];
  const extRequest = o => new Promise(resolve => {
    const u = new URL(o.url);
    if (u.hostname !== '127.0.0.1') return resolve({ status: 0, text: '' });
    seen.push(u.pathname);
    const req = http.request({ host: '127.0.0.1', port: u.port, path: u.pathname + u.search, method: o.method || 'GET',
      headers: { ...(o.headers || {}), Origin: 'chrome-extension://violentmonkey', ...(o.data ? { 'Content-Length': Buffer.byteLength(o.data) } : {}) } }, r => {
      let b = ''; r.on('data', c => b += c); r.on('end', () => resolve({ status: r.statusCode, text: b }));
    });
    req.on('error', () => resolve({ status: 0, text: '' }));
    if (o.data) req.write(o.data);
    req.end();
  });
  const wrapped = `(() => {
    const __net = window.__vhNet, __store = new Map();
    const GM_xmlhttpRequest = o => { __net(JSON.stringify({ url: o.url, method: o.method, headers: o.headers, data: o.data }))
      .then(t => { const r = JSON.parse(t); o.onload && o.onload({ status: r.status, responseText: r.text, response: r.text, responseHeaders: '' }); }); return { abort() {} }; };
    const GM_download = () => {};
    const GM_getValue = (k, d) => (__store.has(k) ? __store.get(k) : d);
    const GM_setValue = (k, v) => { __store.set(k, v); };
    const GM_registerMenuCommand = () => {};
    const GM_setClipboard = () => {};
    const GM_info = { script: { version: '${scriptVer}' }, scriptHandler: 'compat-test' };
    const unsafeWindow = window;
    window.muxjs = window.muxjs || {};
    ${src}
  })();`;
  const PAGE = `<!doctype html><html><head><title>compat test</title>
<script>var flashvars_1 = { video_title: 'compat test', mediaDefinitions: [
  { format: 'hls', videoUrl: 'https://ev-h.phncdn.com/hls/compat/master.m3u8', quality: '720' } ] };</script>
</head><body><div id="player"><video></video></div></body></html>`;

  const b = await chromium.launch();
  const ctx = await b.newContext({ locale: 'zh-TW' });
  const p = await ctx.newPage();
  const pageErrors = [];
  p.on('pageerror', e => pageErrors.push(String(e).slice(0, 200)));
  await p.exposeFunction('__vhNet', async t => JSON.stringify(await extRequest(JSON.parse(t))));
  await ctx.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.hostname === 'www.pornhub.com' && u.pathname === '/view_video.php') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: PAGE });
    return route.abort();
  });
  p.on('dialog', d => d.type() === 'prompt' ? d.accept(code) : d.dismiss());
  await p.addInitScript(wrapped);
  await p.goto('https://www.pornhub.com/view_video.php?viewkey=compat');
  await p.waitForTimeout(2000);
  let clicked = false;
  for (let i = 0; i < 8 && !clicked; i++) {
    const btn = p.getByText('🖥 送到電腦版', { exact: true }).first();
    if (await btn.count() && await btn.isVisible()) { await btn.click(); clicked = true; break; }
    for (const el of await p.$$('.vh-ui *')) { try { if (await el.isVisible()) await el.click({ timeout: 200, force: true }); } catch (e) {} }
    await p.waitForTimeout(500);
  }
  // 等「查進度」至少回一次
  for (let i = 0; i < 20 && !br.read().some(x => x.kind === 'status'); i++) await p.waitForTimeout(500);
  const recs = br.read(); if (process.env.VH_DEBUG) console.log(JSON.stringify(recs));
  const enq = recs.filter(x => x.kind === 'enqueue');
  const st = recs.filter(x => x.kind === 'status');
  const shown = await p.evaluate(() => [...document.querySelectorAll('.vh-ui')].map(e => e.innerText).join(' ').match(/🖥 電腦版[^\n]*/g) || []);
  br.py.stdin.end();
  await b.close();

  const problems = [];
  if (!ping.version) problems.push('/ping 沒回版本');
  if (!clicked) problems.push('找不到「🖥 送到電腦版」按鈕');
  if (!seen.includes('/pair')) problems.push('沒走配對');
  if (enq.length !== 1) problems.push(`電腦版收到 ${enq.length} 筆下載（應該 1 筆）`);
  else {
    if (!enq[0].url.includes('viewkey=compat')) problems.push('送過去的網址不對：' + enq[0].url);
    const wantMedia = !ping.features || ping.features.includes('media');
    const m = enq[0].media && typeof enq[0].media === 'object' ? enq[0].media.url : enq[0].media;
    if (wantMedia && !String(m || '').includes('ev-h.phncdn.com')) problems.push('電腦版會收影片位址，但腳本沒送');
  }
  if (!st.length) problems.push('腳本沒有來查進度');
  if (pageErrors.length) problems.push('網頁錯誤：' + pageErrors.join('｜'));
  const sum = `腳本 v${scriptVer} × 電腦版 v${version}（api ${ping.api ?? '—'}）：配對、送出 ${enq.length} 筆、查進度 ${st.length} 次；畫面：${shown.slice(-1)[0] || '—'}`;
  if (problems.length) { error(label, sum + '；❌ ' + problems.join('；')); process.exit(1); }
  notice(label, '✅ ' + sum);
}

// ---------- C4：舊電腦版功能對照表 ----------
async function legacy() {
  const a = src.indexOf('const DESKTOP_DL'), z = src.indexOf('const BRIDGE_API');
  if (a < 0 || z < 0) { error(label, '腳本裡找不到 desktopLacks 那一段'); process.exit(1); }
  const end = src.indexOf('\n', z);
  const mk = new Function(src.slice(a, end) + '\nreturn { set: c => { desktopCaps = c; }, lacks: desktopLacks, LEGACY_FEATURES, LEGACY_SITES, BRIDGE_API };');
  const L = mk();
  const check = (caps, feature, host) => { L.set(caps); return L.lacks(feature, host); };
  const old = v => ({ version: v, api: 0, features: null, sites: null });     // 1.23.0 以前：沒有 features／sites
  const lacks = s => s !== '';
  const cases = [];
  const want = (name, got, exp) => cases.push([name, got === exp, got, exp]);
  // 照版本推算的每一格：版本 < 表上的版本 → 不支援；≥ → 支援
  for (const [f, v] of Object.entries(L.LEGACY_FEATURES)) {
    const [M, m] = v.split('.').map(Number);
    if (M > 1 || m > 0) want(`${f}：電腦版 v${M}.${m - 1}.9 沒有`, lacks(check(old(`${M}.${m - 1}.9`), f)), true);
    want(`${f}：電腦版 v${v} 有`, lacks(check(old(v), f)), false);
  }
  for (const [d, v] of Object.entries(L.LEGACY_SITES)) {
    const [M, m] = v.split('.').map(Number);
    want(`${d}：電腦版 v${M}.${m - 1}.9 不收`, lacks(check(old(`${M}.${m - 1}.9`), 'enqueue', 'www.' + d)), true);
    want(`${d}：電腦版 v${v} 收`, lacks(check(old(v), 'enqueue', 'www.' + d)), false);
  }
  want('不在表上的新功能（node）：舊電腦版 v1.20.0 要說沒有', lacks(check(old('1.20.0'), 'node')), true);
  want('有 features 清單、沒有 node → 沒有', lacks(check({ version: '1.31.0', api: 1, features: ['enqueue', 'status', 'media', 'classify'], sites: ['pornhub.com'] }, 'node')), true);
  want('有 features 清單、有 node → 有', lacks(check({ version: '1.32.0', api: 1, features: ['enqueue', 'node'], sites: ['pornhub.com'] }, 'node')), false);
  want('sites 清單：子網域 www.pornhub.com 算 pornhub.com', lacks(check({ version: '1.40.0', api: 1, features: ['enqueue'], sites: ['pornhub.com'] }, 'enqueue', 'www.pornhub.com')), false);
  want('sites 清單：不在清單 → 不收', lacks(check({ version: '1.40.0', api: 1, features: ['enqueue'], sites: ['pornhub.com'] }, 'enqueue', 'www.xvideos.com')), true);
  want('sites 清單：notpornhub.com 不能冒充 pornhub.com', lacks(check({ version: '1.40.0', api: 1, features: ['enqueue'], sites: ['pornhub.com'] }, 'enqueue', 'notpornhub.com')), true);
  L.set({ version: '9.0.0', api: L.BRIDGE_API + 1, features: ['enqueue'], sites: ['pornhub.com'] });
  want('電腦版的 api 比腳本新 → 請腳本更新', /腳本比電腦版舊/.test(L.lacks('enqueue', 'www.pornhub.com')), true);
  want('還沒找到電腦版 → 不擋', check(null, 'enqueue', 'www.pornhub.com'), '');

  // 跟「現在的電腦版」對照：腳本會問的功能、表上的功能／網站，電腦版 /ping 都要有
  const br = startBridge();
  const { port, version } = await br.ready;
  const ping = await get(port, '/ping');
  br.py.stdin.end();
  const asked = new Set([...src.matchAll(/desktopLacks\(([^)]*)\)/g)].flatMap(m => [...m[1].matchAll(/'([a-z]+)'/g)].map(x => x[1])));
  const feats = new Set(ping.features || []);
  for (const f of new Set([...asked, ...Object.keys(L.LEGACY_FEATURES)])) want(`電腦版 v${version} 的 /ping 有 ${f}`, feats.has(f), true);
  for (const d of Object.keys(L.LEGACY_SITES)) want(`電腦版 v${version} 的 /ping 收 ${d}`, (ping.sites || []).includes(d), true);

  const bad = cases.filter(c => !c[1]);
  for (const c of bad) error(label, `❌ ${c[0]}（得到 ${JSON.stringify(c[2])}，應該 ${JSON.stringify(c[3])}）`);
  if (bad.length) process.exit(1);
  notice(label, `✅ ${cases.length} 個情況都對（腳本 v${scriptVer}；會問電腦版的功能：${[...asked].join('、')}）`);
}

(mode === 'legacy' ? legacy : browser)().then(() => process.exit(0)).catch(e => { error(label, String(e && e.stack || e)); process.exit(1); });
