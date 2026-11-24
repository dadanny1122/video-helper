// 說明書用的新介面截圖（示範資料，不連網）：node .github/webui_shots.js docs/manual/img
// 改了 webui/ 的畫面想更新說明書的圖就跑一次
'use strict';
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const dir = path.join(__dirname, '..', 'webui');
const read = (f) => fs.readFileSync(path.join(dir, f), 'utf8');
const html = read('index.html').replace('/*CSS*/', () => read('app.css')).replace('/*JS*/', () => read('app.js'));
const out = process.argv[2] || 'docs/manual/img';
fs.mkdirSync(out, { recursive: true });

const V = process.env.VH_VERSION || '1.42.0';
const hist = [
  { key: 'h1', title: '示範影片：週末旅行紀錄', site: 'xvideos.com', quality: '1080p', size: '1.2 GB', time: '11-24 21:10', st: 'done', progress: 100, open: true },
  { key: 'h2', title: '示範影片：城市夜景', site: 'pornhub.com', quality: '720p', size: '640 MB', time: '11-24 20:42', st: 'done', progress: 100, open: true },
  { key: 'h3', title: '示範影片：節點傳回的影片', site: 'youporn.com', quality: '1080p', size: '980 MB', time: '11-24 19:03', st: 'done', progress: 100, open: true, node: true },
  { key: 'h4', title: '示範影片：已經被刪掉的網址', site: 'redtube.com', st: 'fail', progress: 0, note: 'HTTP 404：影片不存在' },
];
const S = {
  version: V, theme: 'dark', paused: false, bridge: true,
  kpis: [
    { icon: '⬇', label: '進行中下載', value: 2, unit: '部', sub: '目前 52.4 Mbps' },
    { icon: '✓', label: '已完成下載', value: 86, unit: '部', sub: '今天 3 部' },
    { icon: '🖧', label: 'Nexus 節點在線', value: '1 / 1', unit: '', sub: 'Master 執行中' },
    { icon: '💾', label: '可用空間', value: '412', unit: 'GB', sub: '下載資料夾在 D:' },
  ],
  tasks: [
    { key: 'q1', title: '示範影片：海邊日落', site: 'xvideos.com', quality: '1080p', size: '1.4 GB', st: 'run', progress: 62, speed: '52.4 Mbps', cancel: true },
    { key: 'q2', title: '示範影片：山上的雲', site: 'xnxx.com', quality: '720p', st: 'wait', progress: 0, remove: true },
    ...hist,
  ],
  nexus: { enabled: true, running: true, online: 1, total: 1, tasks: 1, devices: [{ name: '書房電腦', online: true, state: '連線中' }] },
  notices: [{ icon: '✅', text: '下載完成：示範影片：週末旅行紀錄', time: '11-24 21:10', page: 'dl' }],
  update: null, ask: null, hist_ver: 1, analysis: null, batch: false,
  qualities: ['最高', '2160', '1440', '1080', '720', '480', '360'], default_quality: '1080', rate_limit: 0,
  settings: { download_dir: 'D:\\影片', default_dir: false, default_quality: '1080', rate_limit: 0, skip_downloaded: true,
    smart_segments: 'observe', smart_modes: [['observe', '只觀察（記錄建議，照舊 6 段）'], ['auto', '自動調整'], ['off', '關閉（固定 6 段）']],
    filename_template: '標題 [畫質]', templates: ['標題 [畫質]', '標題', '下載日期 標題 [畫質]'], tpl_preview: '影片標題 [1080p].mp4',
    theme: 'auto', themes: [['auto', '跟系統'], ['dark', '深色'], ['light', '淺色']], close_action: '下載中才詢問',
    close_choices: ['下載中才詢問', '一律縮到系統匣', '直接結束'], tray_always: true, tray_ok: true, keep_history: true, keep_stats: true,
    bridge: '✅ 執行中（127.0.0.1:47321）｜已配對 1 個瀏覽器', bridge_on: true, paired: 1, blocklist: '71,234 筆｜今天更新',
    ai: '沒有設定金鑰（只用免費清單）｜AI 記住 0 個網域', ai_has_key: false, mbps: 85.2, data_dir: 'C:\\Users\\你\\AppData\\Roaming\\VideoHelper' },
  setup: { chrome: true, done: ['1', '2'] },
};
const NEWS = [{ ver: V, title: '新介面', items: ['左側選單：首頁、下載管理、Nexus、設定', '安全與統計'] }];
const NEXUS = { ok: true, running: true, error: '', host: '192.168.1.5', port: 47400, cfg_host: '192.168.1.5', cfg_port: 47400, enabled: true,
  addrs: [['127.0.0.1', '127.0.0.1（只有這台電腦）'], ['192.168.1.5', '192.168.1.5（區域網路）']], lan: true, fp: '3f9a1c27b0d4e65a', pair: '', pair_left: 0,
  browser_only: ['pornhub.com'], types: [['video.download', '下載影片（節點）', '在節點的沙箱裡下載影片', 7200]],
  devices: [{ id: 'd1', name: '書房電腦', status: 'ACTIVE', status_name: '已核准', conn: '連線中', online: true, seen: '4 秒前', iso: '基本（可下載）',
    created: '11-20 10:00', ip: '192.168.1.8', fp: '0a1b2c3d4e5f6a7b', ops: ['suspend', 'revoke'] }],
  tasks: [{ id: 't1', type: '下載影片（節點）', what: 'xvideos.com／示範（最高，傳回）', status: 'UPLOADING', text: '傳回中 58%（580 MB／1.0 GB）', tag: '', progress: 58,
    node: '書房電腦', tries: '1/2', created: '11-24 21:30:00', cancel: true }] };
const SEC = { paired: [{ id: 'aaaabbbbccccdddd', ua: 'Chrome 142（Windows）', paired: '2026-11-20 10:00', last: '2026-11-24 21:00', uses: 24 }],
  bridge: '開著（127.0.0.1:47321，只有這台電腦連得到）', ai: [], ai_key: false,
  net: [{ host: 'github.com', n: 3, last: '21:00:00', odd: false }, { host: 'xvideos.com', n: 12, last: '21:10:00', odd: false }],
  log_size: '86 KB', log_keep: 14, components: `Video Helper ${V}\nyt-dlp 2026.10.26\nffmpeg n7.1（迷你版）`,
  stats: [['xvideos.com', 40, '38.2 GB', '86.0 Mbps', '±7%', '±2%'], ['pornhub.com', 31, '25.4 GB', '74.1 Mbps', '±9%', '±3%']], stats_sum: '共下載 71 部｜總量 63.6 GB' };

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
  await page.exposeFunction('__data', (k) => ({ S, NEWS, NEXUS, SEC })[k]);
  await page.addInitScript(() => {
    window.pywebview = { api: {
      state: () => window.__data('S'), static: async () => ({ news: await window.__data('NEWS') }),
      act: async (n) => (n === 'sec_view' ? { ok: true, sec: await window.__data('SEC') } : { ok: true }),
      ready: () => true, history: async () => { const s = await window.__data('S'); const it = s.tasks.filter((t) => t.key[0] === 'h'); return { total: it.length, items: it, sites: [], counts: {} }; },
      thumbs: async () => ({}), nexus: () => window.__data('NEXUS'),
    } };
  });
  await page.route('**/*', (r) => r.abort());
  await page.goto('about:blank');
  await page.setContent(html);
  await page.waitForTimeout(800);
  for (const p of ['home', 'dl', 'nodes', 'set', 'sec', 'setup']) {
    await page.click(`#nav a[data-page="${p}"]`);
    await page.waitForTimeout(900);
    await page.screenshot({ path: path.join(out, `ui-${p}.jpg`), type: 'jpeg', quality: 78 });
    console.log('✅', p);
  }
  await browser.close();
})();
