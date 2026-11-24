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
const TITLES = { home: ['首頁', '安全、乾淨、穩定地下載'], dl: ['下載管理', '新增下載、看佇列、找以前下載的影片'] };

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

function modal(title, text, buttons) {
  const box = $('#mbox');
  box.innerHTML = `<h3>${esc(title)}</h3><p class="muted">${esc(text)}</p><div class="btns"></div>`;
  return new Promise((res) => {
    for (const [label, val, cls] of buttons) {
      const b = document.createElement('button'); b.textContent = label; if (cls) b.className = cls;
      b.onclick = () => { $('#modal').hidden = true; res(val); };
      $('.btns', box).appendChild(b);
    }
    $('#modal').hidden = false;
  });
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
    <button class="ghost mt" data-act="old:nodes">管理節點（舊介面）</button>`;
}

function renderNews(news, upd) {
  const u = upd ? `<div class="news upd" data-act="link:update"><b>✨ 有新版本 v${esc(upd.tag)}</b><div class="muted">按這裡前往下載</div></div>` : '';
  $('#news').innerHTML = u + (news || []).map((n) => `<div class="news"><b>v${esc(n.ver)} ${esc(n.title)}</b>
    <div class="muted">${(n.items || []).map(esc).join('<br>')}</div></div>`).join('');
}

function renderNotices(ns) {
  $('#notices').innerHTML = ns.length ? ns.map((n) => `<div class="ntc"><span>${esc(n.icon)}</span><span class="grow">${esc(n.text)}</span><time>${esc(n.time)}</time></div>`).join('')
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
  page = p;
  for (const a of $$('#nav a')) a.classList.toggle('on', a.dataset.page === p);
  $('#page-home').hidden = p !== 'home';
  $('#page-dl').hidden = p !== 'dl';
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
    else renderDl(s);
    renderNotices(s.notices);
  }
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
    if (p === 'home' || p === 'dl') { go(p); lastSig = ''; if (S) render(S); return; }
    if (p === 'about') {
      modal('關於 Video Helper', `版本 v${S ? S.version : ''}（新介面預覽）\n\n全部在你自己的電腦上跑，沒有伺服器。新介面只讀電腦版裡的資料、不連任何網站。\nNexus、設定這兩頁還在做，先用原本的視窗打開。`,
        [['說明書', 'manual', 'accent'], ['GitHub 發布頁', 'releases'], ['關閉', null]]).then((v) => v && act('link', v));
      return;
    }
    act('old', p);
  });
  document.addEventListener('click', async (e) => {
    if (!e.target.closest('#menu')) $('#menu').hidden = true;
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
