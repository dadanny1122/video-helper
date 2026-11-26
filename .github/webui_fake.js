// 新介面測試共用的假資料（webui_test.js、webui_i18n_test.js）
'use strict';
const JPG = 'data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAJABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCvRRRQfZn/2Q==';
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
      { key: 'hef', title: '失敗的影片', site: 'spankbang.com', st: 'fail', progress: 0, note: '影片不存在（404）' },
    ],
    nexus: { enabled: true, running: true, online: 1, total: 2, tasks: 1, devices: [{ name: '書房電腦', online: true, state: '連線中' }, { name: EVIL, online: false, state: '離線' }] },
    notices: [{ icon: '✅', text: '下載完成：' + EVIL, time: '20:31', page: 'dl' }],
    update: { tag: '1.38.0' }, ask: null, hist_ver: 1, analysis: null, batch: false,
    qualities: ['最高', '2160', '1440', '1080', '720', '480', '360'], default_quality: '1080', rate_limit: 0,
    settings: { download_dir: 'C:\\Users\\me\\Downloads', default_dir: true, default_quality: '1080', rate_limit: 7, skip_downloaded: true,
      smart_segments: 'observe', smart_modes: [['observe', '只觀察（記錄建議，照舊 6 段）'], ['auto', '自動調整'], ['off', '關閉（固定 6 段）']], filename_template: '標題 [畫質]',
      templates: ['標題 [畫質]', '標題'], tpl_preview: '影片標題 [1080p].mp4', theme: 'auto', themes: [['auto', '跟系統'], ['dark', '深色'], ['light', '淺色']],
      lang: 'en', langs: [['auto', '跟系統'], ['zh', '繁體中文'], ['en', 'English']],
      close_action: '下載中才詢問', close_choices: ['下載中才詢問', '一律縮到系統匣', '直接結束'], tray_always: true, tray_ok: true,
      keep_history: true, keep_stats: false, bridge: '✅ 執行中（127.0.0.1:47321）｜已配對 1 個瀏覽器', bridge_on: true, paired: 1,
      blocklist: '71,234 筆｜今天更新', ai: '沒有設定金鑰（只用免費清單）｜AI 記住 0 個網域', ai_has_key: false, mbps: 85.2, data_dir: 'C:\\Users\\me\\AppData\\Roaming\\VideoHelper',
      lock_on: false, lock_idle_min: 15, lock_idles: [0, 5, 15, 30, 60], sched_mode: 'fast_night',
      sched_modes: [['off', '關閉'], ['window', '只在時段內下載'], ['fast_night', '時段內全速、其他時間限速']], sched_start: 1, sched_end: 7,
      sched_day_mbps: 5, sched_days: [2, 5, 10, 20], organize: 'off', organize_modes: [['off', '不整理'], ['site', '依網站分資料夾'], ['site_month', '依網站＋月份']],
      notify_mode: 'off', notify_modes: [['off', '關閉'], ['all_done', '佇列全部做完時']], notify_has: false, recycle_ok: true },
    lock_on: false, lock_idle_min: 15, later_ver: 1, later_n: 2, sched: '',
    setup: { chrome: true, done: ['1'] },
  }, extra);
}

const NEXUS = { ok: true, running: true, error: '', host: '192.168.1.5', port: 47400, cfg_host: '192.168.1.5', cfg_port: 47400, enabled: true,
  addrs: [['127.0.0.1', '127.0.0.1（只有這台電腦）'], ['192.168.1.5', '192.168.1.5（區域網路）']], lan: true, fp: 'ab12cd34ef56ab78',
  pair: '123456', pair_left: 245, browser_only: ['pornhub.com'],
  types: [['video.download', '下載影片（節點）', '在節點的沙箱裡下載影片，存在節點那台電腦（可勾選傳回這台）', 7200], ['test.echo', '回聲', '回傳原文＋SHA-256（測完整流程）', 30], ['test.file', '測試檔（測傳回）', '在節點寫一個 N MB 的測試檔（測傳回、續傳；不連網）', 600]],
  devices: [{ id: 'd1', name: '書房電腦', status: 'ACTIVE', status_name: '已核准', conn: '連線中', online: true, seen: '3 秒前', iso: '基本（可下載）', created: '11-20 10:00', ip: '192.168.1.8', fp: '0011223344556677', ops: ['suspend', 'revoke'] },
    { id: 'd2', name: EVIL, status: 'PENDING', status_name: '等待核准', conn: '—', online: false, seen: '—', iso: '—', created: '11-24 21:00', ip: '192.168.1.9', fp: '8899aabbccddeeff', ops: ['approve', 'reject'] }],
  tasks: [{ id: 't1', type: '下載影片（節點）', what: 'xvideos.com／abc（最高，傳回）', status: 'UPLOADING', text: '傳回中 42%（420 MB／1.0 GB）', tag: '', progress: 42, node: '書房電腦', tries: '1/2', created: '11-24 21:30:00', cancel: true, fail: false, retry_return: false },
    { id: 't2', type: '回聲', what: '哈囉', status: 'SUCCEEDED', text: '成功', tag: 'ok', progress: null, node: '書房電腦', tries: '1/2', created: '11-24 20:00:00', cancel: false, fail: false, retry_return: false }] };

// 假的 pywebview.api（真的電腦版在 webui/__init__.py 的 Api）
async function setup(page, getState) {
    await page.exposeFunction('__state', getState);
    await page.exposeFunction('__hist', (q) => {
      let rows = state('dark').tasks.filter((t) => t.key[0] === 'h');
      if (q.text) rows = rows.filter((t) => t.title.toLowerCase().includes(q.text.toLowerCase()));
      if (q.status === 'done') rows = rows.filter((t) => t.st === 'done');
      if (q.status === 'fail') rows = rows.filter((t) => t.st !== 'done');
      if (q.site) rows = rows.filter((t) => t.site === q.site);
      return { total: rows.length, items: rows, sites: ['redtube.com', 'spankbang.com', 'youporn.com'], counts: {} };
    });
    await page.exposeFunction('__nexus', () => NEXUS);
    await page.exposeFunction('__thumbs', (keys) => Object.fromEntries(keys.map((k) => [k, k === 'hab' ? JPG : k === 'hcd' ? 'javascript:alert(1)' : ''])));
    await page.addInitScript(() => {
      window.__acts = [];
      window.__ask = null;
      window.pywebview = { api: {
        state: async () => Object.assign(await window.__state(), window.__ask ? { ask: window.__ask } : {}),
        static: async () => ({ news: [{ ver: '1.36.0', title: '新介面預覽', items: ['左側選單', '數據卡'] }, { ver: '1.35.0', title: '系統匣', items: ['一直都在'] }] }),
        act: async (n, a) => { window.__acts.push([n, a]);
          if (n === 'smart_report') return { ok: true, report: '最近 30 天：完成或失敗的任務 12 個\n・固定 6 段（基準）：12 個，成功 11，平均速度 85.2 Mbps，平均重試 0.1 次\n・自動調整：還沒有資料\n・分段調整：實際調整 0 次，只觀察的建議 1 次\n　11-24 pornhub.com：6 → 4 段（分段一直出錯）［只觀察］' };
          if (n === 'nx_detail') return { ok: true, detail: { head: '下載影片（節點）｜傳回中 42%（420 MB／1.0 GB）｜節點：書房電腦｜嘗試 1/2｜11-24 21:30:00', result: '下載完成：' + '<b>x</b>', good: true, note: '這個任務一共排程過 1 次。', why: [['書房電腦', '✔ 選中', '88.0', 'CPU 0.90']], timeline: [['11-24 21:30', '你', '已建立', '']] } };
          if (n === 'sec_view') return { ok: true, sec: { paired: [{ id: 'abcdef0123456789', ua: '<img src=x onerror="window.__pwned=3">', paired: '2026-11-20 10:00', last: '2026-11-24 21:00', uses: 12 }],
            bridge: '開著（127.0.0.1:47321，只有這台電腦連得到）', ai: [], ai_key: false,
            net: [{ host: 'github.com', n: 3, last: '21:00:00', odd: false }, { host: 'weird.example', n: 1, last: '21:01:00', odd: true }],
            log_size: '120 KB', log_keep: 14, components: 'yt-dlp 2026.10.26  sha256 abc…', stats: [['pornhub.com', 12, '8.1 GB', '85.2 Mbps', '±8%', '±3%']], stats_sum: '共下載 12 部', verify: { state: 'ok', text: '✅ 這個 EXE 是 GitHub Actions 從 dadanny1122/video-helper 原始碼打包的（GitHub 有簽章紀錄，也跟發布頁的指紋一樣）', sha256: 'ab'.repeat(32), release_sha256: 'ab'.repeat(32), run: 'https://github.com/x/runs/1' } } };
          if (n === 'dup_scan') return { ok: true, can_delete: true, groups: [[{ key: 'h1', title: '<img src=x onerror="window.__pwned=6">', quality: '1080p', size: '1.2 GB', name: 'a [1080p].mp4', keep: true },
            { key: 'h2', title: 'x', quality: '480p', size: '300 MB', name: 'a [480p].mp4', keep: false }]] };
          if (n === 'unlock') return a === '2468' ? { ok: true, msg: '已解鎖' } : { ok: false, msg: '密碼不對（再錯 4 次要等一下）' };
          if (n === 'nx_audit') return { ok: true, audit: [['2026-11-24 13:00:00', 'device.approve', 'owner', '書房電腦', 'ok', '是我的']] };
          return { ok: true, msg: 'ok:' + n }; },
        ready: () => { window.__ready = true; return true; },
        history: (q) => window.__hist(q),
        thumbs: (k) => window.__thumbs(k),
        nexus: () => window.__nexus(),
        later: async () => [{ key: 'l1', title: '<img src=x onerror="window.__pwned=5">', site: 'Pornhub', time: '11-26 09:00', browser: true },
          { key: 'l2', title: '稍後的影片', site: 'XVideos', time: '11-26 08:00', browser: false }],
      } };
    });
}

// 跟 EXE 裡一模一樣的那一頁（webui/__init__.py 的 page_html：CSS、英文字典、JS 合成）
function pageHtml() {
  const { execFileSync } = require('child_process');
  const root = require('path').join(__dirname, '..');
  return execFileSync(process.env.PYTHON || 'python3', ['-I', '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import webui; sys.stdout.buffer.write(webui.page_html().encode())', root], { encoding: 'utf8', maxBuffer: 1 << 26 });
}

module.exports = { JPG, EVIL, state, NEXUS, setup, pageHtml };
