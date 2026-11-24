// Video Helper 新介面。只透過 window.pywebview.api 跟電腦版講話：
//   state()、static()、act(名稱, 參數)、ready()、history(查詢)、thumbs(紀錄編號)
// 網站來的文字（影片標題等）一律經過 esc() 才放進畫面；縮圖只收 data:image/jpeg
'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
let api = null, S = null, filter = 'all', seenN = 0, lastSig = '', page = 'home';

const ST = { run: '下載中', wait: '等待中', retry: '等重試', pause: '已暫停', done: '完成', fail: '失敗', need: '需要處理', cancel: '已取消' };
const FILTERS = { all: () => true, active: (t) => ['run', 'wait', 'retry'].includes(t.st), done: (t) => t.st === 'done',
  paused: (t) => t.st === 'pause', fail: (t) => ['fail', 'need'].includes(t.st) };
const TITLES = { home: ['首頁', '安全、乾淨、穩定地下載'], dl: ['下載管理', '新增下載、看佇列、找以前下載的影片'],
  nodes: ['Nexus 管理', '讓另一台電腦幫你下載、下載完傳回這台'],
  set: ['設定', '改了馬上存；原本視窗的 ③ 設定、⚙ 進階設定都在這裡'], setup: ['安裝擋廣告', '在 Chrome 裝暴力猴和 Video Helper 腳本'] };
const PAGES = Object.keys(TITLES);
const STEPS = [['安裝暴力猴（Violentmonkey）', '打開後按「加到 Chrome」→「新增擴充功能」'],
  ['打開暴力猴的必要設定', '把「允許使用者指令碼」打開；\n有用無痕視窗的話，「在無痕模式下允許」也打開'],
  ['安裝 Video Helper 腳本', '打開後按「安裝」，之後有新版本會自動更新']];

function hue(s) { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }

function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), 3200);
}

async function act(name, arg) {
  if (!api) return;
  try {
    const r = await api.act(name, arg ?? null);
    if (r && r.msg) toast(r.msg);
    setTimeout(tick, 150);
    return r;
  } catch (e) { toast('出錯了：' + e); }
}

function modal(title, text, buttons, cls = 'muted', opt = {}) {
  const box = $('#mbox');
  box.className = 'box' + (opt.wide ? ' wide' : '');
  box.innerHTML = `<h3>${esc(title)}</h3><p class="${cls}">${esc(text)}</p>${opt.html || ''}${opt.input ? `<input class="search reason" placeholder="${esc(opt.input)}">` : ''}<div class="btns"></div>`;
  const inp = $('input.reason', box);
  return new Promise((res) => {
    for (const [label, val, cls] of buttons) {
      const b = document.createElement('button'); b.textContent = label; if (cls) b.className = cls;
      b.onclick = () => { $('#modal').hidden = true; res(inp ? (val ? inp.value.trim() || '' : null) : val); };
      $('.btns', box).appendChild(b);
    }
    $('#modal').hidden = false;
    if (inp) inp.focus();
  });
}
// 要理由的操作（會寫進稽核紀錄）：按取消回 null；沒填理由回 ''（不執行）
async function askReason(title, text, ok) {
  const r = await modal(title, text, [[ok, 1, 'accent'], ['取消', 0]], 'muted', { input: '理由（會寫進稽核紀錄）' });
  if (r === '') toast('沒有理由就不執行');
  return r || null;
}

// ---------- 縮圖：沒有的先顯示網站色塊，做好再換 ----------
const THUMB = {};               // key → data URL；'' ＝做不出來
let thumbWant = new Set(), thumbTimer = null;
function thumbHtml(t) {
  const u = THUMB[t.key];
  if (u && /^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(u)) return `<div class="thumb img"><img alt="" src="${u}"></div>`;
  if (t.open && u === undefined) thumbWant.add(t.key);
  return `<div class="thumb" style="--h:${hue(t.site)}">${esc((t.site || '?').slice(0, 2).toUpperCase())}</div>`;
}
function askThumbs() {
  if (!thumbWant.size || thumbTimer || !api || !api.thumbs) return;
  thumbTimer = setTimeout(async () => {
    const keys = [...thumbWant].slice(0, 60); thumbWant = new Set();
    let got = {};
    try { got = await api.thumbs(keys) || {}; } catch (e) { got = {}; }
    let changed = false;
    for (const k of keys) {
      if (got[k] === null || got[k] === undefined) { thumbWant.add(k); continue; }   // 還在做：等一下再問
      THUMB[k] = got[k]; changed = true;
    }
    thumbTimer = null;
    if (changed) { lastSig = ''; if (S) render(S); if (page === 'dl') renderHist(); }
    if (thumbWant.size) { thumbTimer = setTimeout(() => { thumbTimer = null; askThumbs(); }, 1500); }
  }, 100);
}

// ---------- 一列任務 ----------
function rowHtml(t, opts = {}) {
  const meta = [t.site, t.quality, t.size, t.time].filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join('');
  const bar = ['run', 'pause', 'wait', 'retry'].includes(t.st) && t.progress > 0 ? `<div class="bar"><b style="width:${Math.min(100, +t.progress || 0)}%"></b></div>` : '';
  const k = esc(t.key);
  const btn = [];
  if (t.open) btn.push(`<button title="播放" data-a="play" data-k="${k}">▶</button><button title="打開檔案位置" data-a="reveal" data-k="${k}">📂</button>`);
  if (opts.hist && !t.open) btn.push(`<button title="重新下載" data-a="redownload" data-k="${k}">↻</button>`);
  if (t.cancel) btn.push(`<button title="取消下載" data-a="cancel" data-k="${k}">✕</button>`);
  if (t.remove) btn.push(`<button title="從清單移除" data-a="remove" data-k="${k}">🗑</button>`);
  const label = t.st === 'run' ? `${Math.floor(t.progress || 0)}%` : (t.label || ST[t.st] || t.st);
  const cb = opts.hist ? `<input type="checkbox" data-sel="${k}" ${SEL.has(t.key) ? 'checked' : ''} title="選取">` : '';
  return `<div class="row${opts.hist ? ' hrow' : ''}${SEL.has(t.key) && opts.hist ? ' sel' : ''}" data-key="${k}" data-open="${t.open ? 1 : 0}">${cb}
    ${thumbHtml(t)}
    <div style="min-width:0"><div class="t1" title="${esc(t.title)}">${t.node ? '🖧 ' : ''}${esc(t.title)}</div>
      <div class="t2">${meta}${t.speed ? `<span>${esc(t.speed)}</span>` : ''}${t.note ? `<span>${esc(t.note)}</span>` : ''}</div>${bar}</div>
    <div class="acts"><span class="st ${esc(t.st)}">${esc(label)}</span>${btn.join('')}</div></div>`;
}

function renderKpis(k) {
  $('#kpis').innerHTML = k.map((c) => `
    <div class="card kpi"><div class="lbl"><i>${esc(c.icon)}</i>${esc(c.label)}</div>
    <div class="val">${esc(c.value)}<small>${esc(c.unit || '')}</small></div><div class="sub">${esc(c.sub || '')}</div></div>`).join('');
}

function renderTasks(tasks) {
  const list = tasks.filter(FILTERS[filter]);
  $('#list').innerHTML = list.length ? list.map((t) => rowHtml(t)).join('')
    : `<div class="empty">${filter === 'all' ? '還沒有下載任務：在上面貼網址，或在影片網頁按「🖥 送到電腦版」' : '這一類目前沒有'}</div>`;
  askThumbs();
}

function renderNexus(n) {
  const el = $('#nexus');
  if (!n) { el.innerHTML = `<div class="card-h"><h3>Nexus 節點</h3></div><div class="muted small">節點功能無法載入</div>`; return; }
  const rows = (n.devices || []).slice(0, 4).map((d) => `<div class="nx-row"><span><span class="dot ${d.online ? 'ok' : ''}"></span>${esc(d.name)}</span><span class="muted">${esc(d.state)}</span></div>`).join('');
  el.innerHTML = `<div class="card-h"><h3>Nexus 節點</h3><div class="grow"></div><span class="pill ${n.running ? 'ok' : ''}">${n.running ? 'Master 執行中' : '未啟動'}</span></div>
    <div class="nx-row"><span>在線節點</span><b>${esc(n.online)} / ${esc(n.total)}</b></div>
    <div class="nx-row"><span>執行中的節點任務</span><b>${esc(n.tasks)}</b></div>${rows}
    <button class="ghost mt" data-go="nodes">管理節點</button>`;
}

function renderNews(news, upd) {
  const u = upd ? `<div class="news upd" data-act="link:update"><b>✨ 有新版本 v${esc(upd.tag)}</b><div class="muted">按這裡前往下載</div></div>` : '';
  $('#news').innerHTML = u + (news || []).map((n) => `<div class="news"><b>v${esc(n.ver)} ${esc(n.title)}</b>
    <div class="muted">${(n.items || []).map(esc).join('<br>')}</div></div>`).join('');
}

function renderNotices(ns) {
  $('#notices').innerHTML = ns.length ? ns.map((n) => `<div class="ntc${PAGES.includes(n.page) ? ' go' : ''}" data-gopage="${esc(n.page || '')}"><span>${esc(n.icon)}</span><span class="grow">${esc(n.text)}</span><time>${esc(n.time)}</time></div>`).join('')
    : '<div class="empty">沒有通知</div>';
  const unread = Math.max(0, ns.length - seenN);
  const b = $('#badge'); b.hidden = !unread || !$('#panel').hidden; b.textContent = unread;
}

// ---------- 下載管理：新增下載 ----------
let anPick = null, anId = null;
function renderAnalysis(a) {
  const el = $('#analysis');
  if (!a) { el.innerHTML = ''; anId = null; return; }
  if (a.id !== anId) { anId = a.id; anPick = a.pick ?? null; }
  if (a.state === 'loading') { el.innerHTML = `<div class="an"><span class="spin"></span>讀取中…<span class="muted small"> ${esc(a.url)}</span></div>`; return; }
  if (a.state === 'error') {
    el.innerHTML = `<div class="an"><div class="err">❌ ${esc(a.err)}</div><div class="muted small">${esc(a.url)}</div>
      <div class="btns" style="margin-top:10px"><button class="ghost" data-act="clear_analysis">關閉</button></div></div>`;
    return;
  }
  const f = (a.formats || []).map((x) => `<button class="fmt${x.i === anPick ? ' on' : ''}" data-fmt="${x.i}"><b>${esc(x.label)}</b>
    <div>${esc(x.size)}・${esc(x.time)}・${esc(x.kind)}</div></button>`).join('');
  el.innerHTML = `<div class="an"><h4>${esc(a.title)}</h4><div class="muted small">${esc(a.site)}・長度 ${esc(a.duration)}</div>
    ${a.downloaded ? `<div class="warn">⚠ 這支影片 ${esc(a.downloaded)}</div>` : ''}
    <div class="fmts">${f}</div>
    <div class="btns"><button class="accent" id="ango">⬇ 下載這個畫質</button><button class="ghost" data-act="clear_analysis">取消</button></div></div>`;
}

function dlLines() { return $('#dlurls').value.split(/\r?\n/).map((s) => s.trim()).filter((s) => /^https?:\/\//.test(s)); }
function updateGo() {
  const n = new Set(dlLines()).size;
  $('#dlgo').textContent = n > 1 ? `全部加入（${n} 部）` : '讀取';
}

// ---------- 下載管理：佇列 ----------
function renderQueue(s) {
  const q = s.tasks.filter((t) => t.key[0] === 'q');
  $('#qlist').innerHTML = q.length ? q.map((t) => rowHtml(t)).join('') : '<div class="empty">佇列是空的</div>';
  const n = (st) => q.filter((t) => st.includes(t.st)).length;
  $('#qsum').textContent = `下載中 ${n(['run'])}・排隊 ${n(['wait', 'retry'])}` + (n(['pause']) ? `・暫停 ${n(['pause'])}` : '')
    + (n(['fail', 'need']) ? `・失敗 ${n(['fail', 'need'])}` : '') + (s.rate_limit ? `・速度上限 ${s.rate_limit} Mbps` : '') + (s.batch ? '・讀取網址中…' : '');
}

// ---------- 下載管理：紀錄 ----------
const SEL = new Set();
const HQ = { text: '', status: '', site: '', limit: 100 };
let H = { items: [], total: 0, sites: [] }, histVer = null;
async function loadHist() {
  if (!api || !api.history) return;
  try { H = await api.history({ text: HQ.text, status: HQ.status, site: HQ.site, offset: 0, limit: HQ.limit }) || H; } catch (e) { return; }
  const sel = $('#hsite'), cur = sel.value;
  sel.innerHTML = '<option value="">全部網站</option>' + H.sites.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join('');
  sel.value = H.sites.includes(cur) ? cur : '';
  renderHist();
}
function renderHist() {
  $('#hlist').innerHTML = H.items.length ? H.items.map((t) => rowHtml(t, { hist: true })).join('')
    : `<div class="empty">${HQ.text || HQ.status || HQ.site ? '找不到符合的紀錄' : '還沒有下載紀錄'}</div>`;
  $('#hcount').textContent = H.total ? `共 ${H.total} 筆，顯示 ${H.items.length} 筆` : '';
  $('#hmore').hidden = H.items.length >= H.total;
  for (const k of [...SEL]) if (!H.items.some((t) => t.key === k)) SEL.delete(k);
  $('#bulk').hidden = !SEL.size;
  $('#bulkn').textContent = `已選 ${SEL.size} 部`;
  askThumbs();
}

// ---------- 設定 ----------
let setSig = '';
function opts(list, cur) {
  return list.map((x) => { const [v, l] = Array.isArray(x) ? x : [x, x]; return `<option value="${esc(v)}"${String(v) === String(cur) ? ' selected' : ''}>${esc(l)}</option>`; }).join('');
}
function renderSettings(c) {
  if (!c) return;
  const sig = JSON.stringify(c);
  if (sig === setSig) return;
  const focus = document.activeElement;
  if (focus && focus.closest('#page-set') && focus.matches('input:not([type=checkbox]), select')) return;   // 使用者正在改，先不要蓋掉
  setSig = sig;
  $('#s-dir').textContent = c.download_dir; $('#s-dirreset').hidden = c.default_dir;
  $('#s-q').innerHTML = opts((S.qualities || []).map((q) => [q, q === '最高' ? '最高' : q + 'p']), c.default_quality);
  const rate = $('#s-rate');
  if (![...rate.options].some((o) => +o.value === +c.rate_limit)) rate.insertAdjacentHTML('beforeend', `<option>${esc(c.rate_limit)}</option>`);
  rate.value = [...rate.options].find((o) => +o.value === +c.rate_limit).value;
  $('#s-smart').innerHTML = opts(c.smart_modes, c.smart_segments);
  $('#s-tpl').innerHTML = opts(c.templates, c.filename_template);
  $('#s-tplprev').textContent = '範例：' + c.tpl_preview;
  $('#s-theme').innerHTML = c.themes.map(([v, l]) => `<button data-theme="${esc(v)}" class="${v === c.theme ? 'on' : ''}">${esc(l)}</button>`).join('');
  $('#s-close').innerHTML = opts(c.close_choices, c.close_action);
  $('#s-tray').disabled = !c.tray_ok;
  for (const el of $$('#page-set input[type=checkbox][data-set]')) el.checked = !!c[el.dataset.set];
  $('#s-bridge').textContent = c.bridge || (c.bridge_on ? '執行中' : '關閉中') + `｜已配對 ${c.paired} 個瀏覽器`;
  $('#s-mbps').textContent = c.mbps ? `目前記得約 ${c.mbps} Mbps（換了網路、預估變很不準時清除）` : '還沒量過';
  $('#s-bl').textContent = c.blocklist;
  $('#s-ai').textContent = c.ai;
  $('#s-keyclear').hidden = !c.ai_has_key;
  $('#s-ver').textContent = 'v' + S.version;
  $('#s-data').textContent = '設定資料夾：' + c.data_dir;
}
function renderSetup(st) {
  if (!st) return;
  $('#steps').innerHTML = STEPS.map(([t, h], i) => `<div class="step"><div class="n">${i + 1}</div><div class="grow"><b>${esc(t)}</b><div>${esc(h)}</div></div>
    <span class="ok">${st.done.includes(String(i + 1)) ? '✓' : ''}</span><button class="accent small" data-act="setup_step:${i + 1}">打開</button></div>`).join('');
  $('#nochrome').hidden = st.chrome;
}

// ---------- Nexus 管理 ----------
let NX = null, nxSig = '', nxFilter = 'all';
const DEV_OPS = { approve: ['✔ 核准', '核准節點', '核准後這台節點會拿到憑證、可以跟 Master 心跳。\n請先核對節點畫面上的 Master 指紋前 8 碼。'],
  reject: ['✘ 拒絕', '拒絕節點', '拒絕後它要重新配對才能再申請。'],
  suspend: ['⏸ 暫停', '暫停節點', '暫停會立刻切斷它的連線，恢復前心跳都會被拒絕。'],
  resume: ['▶ 恢復', '恢復節點', '恢復後它會自己接回來。'],
  revoke: ['⛔ 撤銷', '撤銷節點', '⚠ 撤銷不能復原：連線立刻切斷、憑證永久作廢，就算它拿舊憑證也進不來；要再用得重新配對（新身分）。'] };
async function loadNexus() {
  if (!api || !api.nexus) return;
  try { NX = await api.nexus(); } catch (e) { return; }
  renderNexusPage();
}
function renderNexusPage() {
  const n = NX;
  $('#nx-err').hidden = !(n && !n.ok); if (n && !n.ok) $('#nx-err').textContent = n.err;
  if (!n || !n.ok) return;
  const sig = JSON.stringify([n, nxFilter]);
  if (sig === nxSig) return;
  const focus = document.activeElement;
  const editing = focus && focus.closest('#page-nodes') && focus.matches('input:not([type=checkbox]), select');
  nxSig = sig;
  const st = $('#nx-state');
  st.textContent = n.running ? `● 執行中 https://${n.host}:${n.port}` : (n.error ? '啟動失敗：' + n.error : '○ 未啟用');
  st.className = 'pill' + (n.running ? ' ok' : '');
  $('#nx-on').checked = n.running;
  if (!editing) {
    $('#nx-host').innerHTML = n.addrs.map(([a, l]) => `<option value="${esc(a)}"${a === n.cfg_host ? ' selected' : ''}>${esc(l)}</option>`).join('');
    $('#nx-port').value = n.cfg_port;
    if (!$('#nf-type').options.length) $('#nf-type').innerHTML = n.types.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
  }
  $('#nx-fp').textContent = n.fp ? n.fp + '…' : '尚未建立';
  $('#nx-code').textContent = n.pair ? n.pair.slice(0, 3) + ' ' + n.pair.slice(3) : '';
  $('#nx-pairnote').textContent = n.pair ? `${Math.floor(n.pair_left / 60)}:${String(n.pair_left % 60).padStart(2, '0')} 後失效，只能用一次；在節點那邊輸入` : (n.running ? '按右邊產生 6 位數，在節點那邊輸入' : '要先啟用 Master');
  $('#nx-pair').disabled = !n.running; $('#nx-new').disabled = !n.running;
  const online = n.devices.filter((d) => d.online).length, act = n.devices.filter((d) => d.status === 'ACTIVE').length;
  $('#nx-dsum').textContent = `已核准 ${act} 台・在線 ${online} 台` + (n.devices.some((d) => d.status === 'PENDING') ? '・有節點在等核准' : '');
  $('#nx-devs').innerHTML = n.devices.length ? n.devices.map((d) => `<div class="nrow"><div style="min-width:0">
      <div class="t1"><span class="dot ${d.online ? 'ok' : ''}"></span>${esc(d.name)}</div>
      <div class="t2"><span class="badge2 ${esc(d.status)}">${esc(d.status_name)}</span><span>${esc(d.conn)}</span><span>心跳 ${esc(d.seen)}</span><span>沙箱：${esc(d.iso)}</span></div>
      <div class="t2 mono"><span>配對 ${esc(d.created)}</span><span>${esc(d.ip)}</span><span>指紋 ${esc(d.fp)}</span></div></div>
      <div class="acts">${d.ops.map((o) => `<button data-nxdev="${esc(o)}" data-id="${esc(d.id)}">${esc(DEV_OPS[o][0])}</button>`).join('')}</div></div>`).join('')
    : `<div class="empty">還沒有節點。在另一台電腦執行 VideoHelper.exe --worker，輸入這裡的配對碼</div>`;
  const ts = n.tasks.filter((t) => nxFilter === 'all' || t.cancel || t.status === 'UNKNOWN');
  $('#nx-tasks').innerHTML = ts.length ? ts.map((t) => `<div class="nrow"><div style="min-width:0">
      <div class="t1">${esc(t.type)}　<span class="muted">${esc(t.what)}</span></div>
      <div class="t2"><span class="badge2 ${esc(t.tag)}">${esc(t.text)}</span><span>節點 ${esc(t.node)}</span><span>嘗試 ${esc(t.tries)}</span><span>${esc(t.created)}</span></div>
      ${t.progress !== null && t.progress !== undefined ? `<div class="bar"><b style="width:${Math.min(100, +t.progress)}%"></b></div>` : ''}</div>
      <div class="acts"><button data-nxt="detail" data-id="${esc(t.id)}">🔍 詳情</button>${t.cancel ? `<button data-nxt="cancel" data-id="${esc(t.id)}">⏹ 取消</button>` : ''}
      ${t.retry_return ? `<button data-nxt="retry" data-id="${esc(t.id)}">↩ 重新傳回</button>` : ''}${t.fail ? `<button data-nxt="fail" data-id="${esc(t.id)}">✘ 標記失敗</button>` : ''}</div></div>`).join('')
    : `<div class="empty">${nxFilter === 'all' ? '還沒有節點任務' : '沒有進行中的任務'}</div>`;
}
function nfSwitch() {
  const n = $('#nf-type').value;
  const t = NX && NX.types.find((x) => x[0] === n);
  $('#nf-desc').textContent = t ? t[2] : '';
  $('#nf-run').value = t ? Math.min(t[3], n === 'video.download' || n === 'test.file' ? 7200 : 900) : 60;
  for (const r of $$('#nx-form .nf')) r.hidden = !r.dataset.for.split(' ').includes(n);
  nfWarn();
}
function nfWarn() {
  let host = '';
  try { host = new URL($('#nf-url').value.trim()).hostname.toLowerCase(); } catch (e) { host = ''; }
  const hit = NX && NX.browser_only.find((h) => host === h || host.endsWith('.' + h));
  $('#nf-warn').textContent = hit ? `⚠ ${hit} 會擋程式直接開網頁：請改用瀏覽器影片頁「⬇ 下載」→「🖧 交給節點」` : '只支援能下載的 9 個網站';
  return hit;
}
async function nxTask(op, id) {
  if (op === 'detail') {
    const r = await act('nx_detail', id);
    if (!r || !r.ok) return;
    const d = r.detail;
    const tbl = (head, rows) => `<div class="tblwrap"><table class="tbl"><tr>${head.map((h) => `<th>${esc(h)}</th>`).join('')}</tr>${rows.map((row) => `<tr>${row.map((c) => `<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</table></div>`;
    const html = (d.result ? `<div class="res ${d.good ? 'good' : 'bad'}">${esc(d.result)}</div>` : '')
      + `<h4>為什麼選這台</h4><p class="muted small">${esc(d.note)}</p>` + (d.why.length ? tbl(['節點', '結果', '分數', '子分數／排除原因'], d.why) : '')
      + `<h4>時間軸</h4>` + tbl(['時間', '誰', '發生什麼', '說明'], d.timeline);
    modal('任務詳情', d.head, [['關閉', 0, 'accent']], 'muted', { wide: true, html });
    return;
  }
  if (op === 'retry') return act('nx_retry_return', id);
  const [title, text, ok, name] = op === 'cancel'
    ? ['取消任務', '會通知節點停止；節點確認停下後才會變成「已取消」。', '取消任務', 'nx_cancel']
    : ['標記失敗', '這個任務的節點失聯了，不知道有沒有跑完。標記成失敗後不會自動重跑。', '標記失敗', 'nx_fail'];
  const reason = await askReason(title, text, ok);
  if (reason) { nxSig = ''; await act(name, { id, reason }); loadNexus(); }
}

// ---------- 右鍵選單 ----------
function showMenu(x, y, key, open) {
  const items = [['▶ 播放', 'play', open], ['📂 打開檔案位置', 'reveal', open], ['📋 複製網址', 'copy_url', true],
    ['📝 AI 字幕', 'sub', open], ['↻ 重新下載', 'redownload', true], ['🗑 刪除紀錄', 'del', true]];
  const m = $('#menu');
  m.innerHTML = items.map(([l, a, on]) => `<button data-m="${a}" ${on ? '' : 'disabled'}>${esc(l)}</button>`).join('');
  m.hidden = false;
  m.style.left = Math.min(x, innerWidth - m.offsetWidth - 8) + 'px';
  m.style.top = Math.min(y, innerHeight - m.offsetHeight - 8) + 'px';
  m.onclick = async (e) => {
    const b = e.target.closest('button'); if (!b || b.disabled) return;
    m.hidden = true;
    await rowAction(b.dataset.m, key);
  };
}

async function rowAction(a, key) {
  if (a === 'cancel' && !await modal('取消下載', '確定要取消嗎？已下載的部分會被刪除；排在後面的會繼續下載。', [['取消下載', 1, 'accent'], ['不要', 0]])) return;
  if (a === 'sub') return act('subtitles', [key]);
  if (a === 'del') {
    if (await modal('刪除紀錄', '只刪下載紀錄，影片檔案不會刪。', [['刪除', 1, 'accent'], ['取消', 0]])) { SEL.delete(key); await act('delete_hist', [key]); }
    return;
  }
  if (a === 'redownload') { go('dl'); window.scrollTo(0, 0); $('main').scrollTop = 0; }
  return act(a, key);
}

function go(p) {
  if (!PAGES.includes(p)) return;
  page = p;
  for (const a of $$('#nav a')) a.classList.toggle('on', a.dataset.page === p);
  for (const x of PAGES) $('#page-' + x).hidden = x !== p;
  setSig = ''; nxSig = '';
  if (p === 'nodes') loadNexus();
  $('#ptitle').textContent = TITLES[p][0]; $('#sub').textContent = TITLES[p][1];
  $('main').scrollTop = 0;
  if (p === 'dl') { loadHist(); if (S) renderDl(S); }
}

function renderDl(s) {
  const qs = $('#dlq');
  if (!qs.options.length && s.qualities) {
    qs.innerHTML = s.qualities.map((q) => `<option value="${esc(q)}">${q === '最高' ? '最高' : esc(q) + 'p'}</option>`).join('');
    qs.value = s.default_quality;
  }
  renderQueue(s);
  renderAnalysis(s.analysis);
  if (s.hist_ver !== histVer) { histVer = s.hist_ver; loadHist(); }
}

function render(s) {
  S = s;
  document.body.className = s.theme === 'light' ? 'light' : 'dark';
  $('#ver').textContent = 'v' + s.version;
  const br = $('#bridge'); br.textContent = s.bridge ? '● 瀏覽器已連動' : '瀏覽器未連動'; br.classList.toggle('ok', !!s.bridge);
  for (const b of $$('.pausebtn')) b.textContent = s.paused ? '▶ 全部繼續' : '⏸ 全部暫停';
  const sig = JSON.stringify([s.kpis, s.tasks, s.nexus, s.update, s.notices.length, filter, s.analysis, s.hist_ver, s.batch, page]);
  if (sig !== lastSig) {                   // 沒變就不重畫（不然捲動、hover 會一直被打斷）
    lastSig = sig;
    if (page === 'home') { renderKpis(s.kpis); renderTasks(s.tasks); renderNexus(s.nexus); renderNews(window.NEWS, s.update); }
    else if (page === 'dl') renderDl(s);
    renderNotices(s.notices);
  }
  if (page === 'set') renderSettings(s.settings);
  if (page === 'nodes' && (tick.n = (tick.n || 0) + 1) % 2 === 0) loadNexus();
  if (page === 'setup') renderSetup(s.setup);
  if (s.ask && s.ask.id !== render.asked) {          // 電腦版要問你一件事（例如還在下載時關視窗）
    render.asked = s.ask.id;
    modal(s.ask.title, s.ask.text, s.ask.buttons.map((b, i) => [b, i, i === 0 ? 'accent' : ''])).then((i) => act('answer', { id: s.ask.id, choice: i }));
  }
}

async function tick() {
  if (!api) return;
  try { render(await api.state()); } catch (e) { /* 電腦版忙：下一次再試 */ }
}

function wire() {
  $('#nav').addEventListener('click', (e) => {
    const a = e.target.closest('a'); if (!a) return;
    const p = a.dataset.page;
    if (PAGES.includes(p)) { go(p); lastSig = ''; if (S) render(S); return; }
    if (p === 'about') {
      modal('關於 Video Helper', `版本 v${S ? S.version : ''}（新介面預覽）\n\n全部在你自己的電腦上跑，沒有伺服器。新介面只讀電腦版裡的資料、不連任何網站。\n🔒 安全中心、統計還沒搬過來，按了會打開原本的小視窗。`,
        [['說明書', 'manual', 'accent'], ['GitHub 發布頁', 'releases'], ['關閉', null]]).then((v) => v && act('link', v));
      return;
    }
    act('old', p);
  });
  document.addEventListener('click', async (e) => {
    if (!e.target.closest('#menu')) $('#menu').hidden = true;
    const g = e.target.closest('[data-go], [data-gopage]');
    if (g && (g.dataset.go || g.dataset.gopage)) { go(g.dataset.go || g.dataset.gopage); lastSig = ''; if (S) render(S); if (g.dataset.gopage) $('#panel').hidden = true; return; }
    const th = e.target.closest('[data-theme]');
    if (th) { act('set', { key: 'theme', value: th.dataset.theme }); return; }
    const b = e.target.closest('[data-act]');
    if (b) {
      const [name, arg] = b.dataset.act.split(':');
      if (name === 'set_ui_classic') {
        if (await modal('切回舊介面', '馬上換成原本的視窗；之後打開電腦版也是舊介面。\n想再用新介面：③ 設定 勾「使用新介面」。', [['切回舊介面', 1, 'accent'], ['取消', 0]])) act('set_ui', 'classic');
        return;
      }
      act(name, arg);
      return;
    }
    const r = e.target.closest('[data-a]');
    if (r) return rowAction(r.dataset.a, r.dataset.k);
    const f = e.target.closest('[data-fmt]');
    if (f) { anPick = +f.dataset.fmt; lastSig = ''; renderAnalysis(S && S.analysis); return; }
    if (e.target.id === 'ango' && S && S.analysis) {
      const a = S.analysis;
      if (a.downloaded && !await modal('已經下載過了', `這支影片 ${a.downloaded}，還要再下載一次嗎？`, [['再下載一次', 1, 'accent'], ['不要', 0]])) return;
      act('enqueue_fmt', { id: a.id, i: anPick ?? a.pick });
    }
  });
  document.addEventListener('change', (e) => {
    const k = e.target.closest('[data-set]');
    if (k) {
      const key = k.dataset.set;
      const value = k.type === 'checkbox' ? k.checked : key === 'rate_limit' ? +k.value : k.value;
      setSig = ''; act('set', { key, value }); k.blur();
      return;
    }
    const c = e.target.closest('[data-sel]'); if (!c) return;
    if (c.checked) SEL.add(c.dataset.sel); else SEL.delete(c.dataset.sel);
    renderHist();
  });
  document.addEventListener('dblclick', (e) => {
    const r = e.target.closest('.hrow'); if (!r || e.target.closest('button, input')) return;
    rowAction(r.dataset.open === '1' ? 'play' : 'redownload', r.dataset.key);
  });
  document.addEventListener('contextmenu', (e) => {
    const r = e.target.closest('.row');
    if (r && r.dataset.key[0] === 'h') { e.preventDefault(); showMenu(e.clientX, e.clientY, r.dataset.key, r.dataset.open === '1'); return; }
    if (!e.target.closest('textarea, input')) e.preventDefault();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('#menu').hidden = true; });
  $('#chips').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    filter = b.dataset.f; for (const x of $('#chips').children) x.classList.toggle('on', x === b);
    if (S) { lastSig = ''; render(S); }
  });
  const ta = $('#urls');
  ta.addEventListener('input', () => { ta.style.height = 'auto'; ta.style.height = Math.min(120, ta.scrollHeight) + 'px'; });
  ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('#addform').requestSubmit(); } });
  $('#addform').addEventListener('submit', async (e) => {
    e.preventDefault();
    const v = ta.value.trim(); if (!v) return;
    const r = await act('add', v);
    if (r && r.ok) { ta.value = ''; ta.style.height = ''; }
  });
  // 下載管理
  $('#dlurls').addEventListener('input', updateGo);
  $('#dlurls').addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.ctrlKey) $('#dlgo').click(); });
  $('#dlgo').addEventListener('click', async () => {
    const urls = [...new Set(dlLines())];
    if (!urls.length) { toast('沒有看到網址（一行一個，http 開頭）'); return; }
    const r = urls.length > 1 ? await act('add_batch', { text: urls.join('\n'), quality: $('#dlq').value }) : await act('analyze', urls[0]);
    if (r && r.ok) { $('#dlurls').value = ''; updateGo(); }
  });
  let st = null;
  $('#hsearch').addEventListener('input', (e) => { clearTimeout(st); st = setTimeout(() => { HQ.text = e.target.value; HQ.limit = 100; loadHist(); }, 200); });
  $('#hsite').addEventListener('change', (e) => { HQ.site = e.target.value; HQ.limit = 100; loadHist(); });
  $('#hchips').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    HQ.status = b.dataset.s; HQ.limit = 100; for (const x of $('#hchips').children) x.classList.toggle('on', x === b);
    loadHist();
  });
  $('#hmore').addEventListener('click', () => { HQ.limit = Math.min(HQ.limit + 100, 500); loadHist(); });
  $('#ball').addEventListener('click', () => { for (const t of H.items) SEL.add(t.key); renderHist(); });
  $('#bnone').addEventListener('click', () => { SEL.clear(); renderHist(); });
  $('#bsub').addEventListener('click', () => act('subtitles', [...SEL]));
  $('#bdel').addEventListener('click', async () => {
    if (!await modal('刪除紀錄', `刪除選取的 ${SEL.size} 筆紀錄？影片檔案不會刪。`, [['刪除', 1, 'accent'], ['取消', 0]])) return;
    const keys = [...SEL]; SEL.clear(); act('delete_hist', keys);
  });
  // Nexus 管理
  $('#nx-on').addEventListener('change', async (e) => {
    const on = e.target.checked;
    if (on && NX && NX.lan && !await modal('開放區域網路', `Master 會在 ${NX.cfg_host}:${NX.cfg_port} 聽同一個網路裡的電腦。\n只有你在這裡按「核准」的節點才能真的連上；其他人只會看到被拒絕。`, [['啟用', 1, 'accent'], ['取消', 0]])) { e.target.checked = false; return; }
    nxSig = ''; await act('nx_enable', on ? 'on' : 'off'); loadNexus();
  });
  $('#nx-bind').addEventListener('click', async () => { nxSig = ''; await act('nx_bind', { host: $('#nx-host').value, port: +$('#nx-port').value }); loadNexus(); });
  $('#nx-pair').addEventListener('click', async () => { nxSig = ''; await act('nx_pair'); loadNexus(); });
  $('#nx-audit').addEventListener('click', async () => {
    const r = await act('nx_audit'); if (!r || !r.ok) return;
    const rows = r.audit.map((x) => `<tr>${x.map((c) => `<td>${esc(c)}</td>`).join('')}</tr>`).join('');
    modal('節點稽核紀錄（最近 200 筆，只能看不能改）', '時間是 UTC。', [['關閉', 0, 'accent']], 'muted', { wide: true,
      html: `<div class="tblwrap"><table class="tbl"><tr><th>時間</th><th>動作</th><th>誰</th><th>裝置</th><th>結果</th><th>理由／說明</th></tr>${rows}</table></div>` });
  });
  $('#nx-chips').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    nxFilter = b.dataset.f; for (const x of $('#nx-chips').children) x.classList.toggle('on', x === b);
    nxSig = ''; renderNexusPage();
  });
  $('#nx-devs').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-nxdev]'); if (!b) return;
    const op = b.dataset.nxdev, d = NX.devices.find((x) => x.id === b.dataset.id); if (!d) return;
    let note = DEV_OPS[op][2];
    if (op === 'approve') {
      const same = NX.devices.filter((o) => o.id !== d.id && o.name === d.name && ['ACTIVE', 'SUSPENDED', 'PENDING'].includes(o.status));
      if (same.length) note += `\n\n⚠ 名單裡已經有 ${same.length} 筆也叫「${d.name}」、還沒撤銷的節點（通常是同一台重新配對留下的舊身分）。核准這筆之後，記得把舊的撤銷。`;
    }
    const reason = await askReason(DEV_OPS[op][1], `${d.name}\n指紋 ${d.fp}\n來源 ${d.ip}\n\n${note}`, DEV_OPS[op][0]);
    if (reason) { nxSig = ''; await act('nx_device', { op, id: d.id, reason }); loadNexus(); }
  });
  $('#nx-tasks').addEventListener('click', (e) => { const b = e.target.closest('[data-nxt]'); if (b) nxTask(b.dataset.nxt, b.dataset.id); });
  $('#nx-new').addEventListener('click', () => { $('#nx-form').hidden = false; nfSwitch(); });
  $('#nf-close').addEventListener('click', () => { $('#nx-form').hidden = true; });
  $('#nf-type').addEventListener('change', nfSwitch);
  $('#nf-url').addEventListener('input', nfWarn);
  $('#nf-go').addEventListener('click', async () => {
    const type = $('#nf-type').value;
    if (type === 'video.download') {
      if (!/^https?:\/\//.test($('#nf-url').value.trim())) { toast('請貼影片網址'); return; }
      if (nfWarn() && !await modal('這個網站要從瀏覽器交', '在這裡貼網址，節點會下載失敗。還是要照樣送出嗎？', [['照樣送出', 1], ['不要', 0, 'accent']])) return;
    }
    const r = await act('nx_task_new', { type, url: $('#nf-url').value.trim(), quality: $('#nf-q').value, ret: $('#nf-ret').checked,
      size_mb: +$('#nf-size').value, text: $('#nf-text').value, seconds: +$('#nf-sec').value, code: $('#nf-code').value,
      runtime: +$('#nf-run').value, tries: +$('#nf-tries').value });
    if (r && r.ok) { $('#nx-form').hidden = true; $('#nf-url').value = ''; nxSig = ''; loadNexus(); }
  });
  // 設定
  $('#s-report').addEventListener('click', async () => {
    const r = await act('smart_report');
    if (r && r.report) modal('智慧分段報告', r.report, [['關閉', 0, 'accent']], 'report');
  });
  $('#s-unpair').addEventListener('click', async () => {
    if (await modal('取消所有配對', '所有配對過的瀏覽器下次「送到電腦版」時都要重新輸入配對碼。', [['取消所有配對', 1, 'accent'], ['不要', 0]])) act('unpair_all');
  });
  $('#s-keysave').addEventListener('click', async () => {
    const v = $('#s-key').value.trim(); if (!v) { toast('先貼上金鑰'); return; }
    const r = await act('set_ai_key', v);
    if (r && r.ok) $('#s-key').value = '';
  });
  $('#s-keyclear').addEventListener('click', async () => {
    if (await modal('清除金鑰', '清除後只用免費的公開清單判斷廣告。', [['清除', 1, 'accent'], ['取消', 0]])) act('set_ai_key', '-');
  });
  $('#bell').addEventListener('click', () => {
    const p = $('#panel'); p.hidden = !p.hidden;
    if (!p.hidden && S) seenN = S.notices.length;
    if (S) renderNotices(S.notices);
  });
  $('#clearn').addEventListener('click', () => { act('clear_notices'); seenN = 0; });
  document.addEventListener('dragover', (e) => e.preventDefault());   // 拖檔案進來不要變成打開那個檔
  document.addEventListener('drop', (e) => e.preventDefault());
}

async function start() {
  api = window.pywebview.api;
  wire();
  try {
    const st = await api.static();
    window.NEWS = st.news || [];
  } catch (e) { window.NEWS = []; }
  await tick();
  setInterval(tick, 500);
  api.ready();
}
if (window.pywebview && window.pywebview.api) start(); else window.addEventListener('pywebviewready', start, { once: true });
