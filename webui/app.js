// Video Helper 新介面。只透過 window.pywebview.api 的 3 個函式跟電腦版講話：state()、static()、act(名稱, 參數)
// 網站來的文字（影片標題等）一律經過 esc() 才放進畫面
'use strict';
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
let api = null, S = null, filter = 'all', seenN = 0, lastSig = '';

const ST = { run: '下載中', wait: '等待中', retry: '等重試', pause: '已暫停', done: '完成', fail: '失敗', need: '需要處理', cancel: '已取消' };
const FILTERS = { all: () => true, active: (t) => ['run', 'wait', 'retry'].includes(t.st), done: (t) => t.st === 'done',
  paused: (t) => t.st === 'pause', fail: (t) => ['fail', 'need'].includes(t.st) };

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

function renderKpis(k) {
  $('#kpis').innerHTML = k.map((c) => `
    <div class="card kpi"><div class="lbl"><i>${esc(c.icon)}</i>${esc(c.label)}</div>
    <div class="val">${esc(c.value)}<small>${esc(c.unit || '')}</small></div><div class="sub">${esc(c.sub || '')}</div></div>`).join('');
}

function renderTasks(tasks) {
  const list = tasks.filter(FILTERS[filter]);
  if (!list.length) {
    $('#list').innerHTML = `<div class="empty">${filter === 'all' ? '還沒有下載任務：在上面貼網址，或在影片網頁按「🖥 送到電腦版」' : '這一類目前沒有'}</div>`;
    return;
  }
  $('#list').innerHTML = list.map((t) => {
    const meta = [t.site, t.quality, t.size, t.time].filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join('');
    const bar = ['run', 'pause', 'wait', 'retry'].includes(t.st) && t.progress > 0 ? `<div class="bar"><b style="width:${Math.min(100, +t.progress || 0)}%"></b></div>` : '';
    const btn = [];
    if (t.open) btn.push(`<button title="播放" data-a="play" data-k="${esc(t.key)}">▶</button><button title="打開檔案位置" data-a="reveal" data-k="${esc(t.key)}">📂</button>`);
    if (t.cancel) btn.push(`<button title="取消下載" data-a="cancel" data-k="${esc(t.key)}">✕</button>`);
    if (t.remove) btn.push(`<button title="從清單移除" data-a="remove" data-k="${esc(t.key)}">🗑</button>`);
    const label = t.st === 'run' ? `${Math.floor(t.progress || 0)}%` : (t.label || ST[t.st] || t.st);
    return `<div class="row">
      <div class="thumb" style="--h:${hue(t.site)}">${esc((t.site || '?').slice(0, 2).toUpperCase())}</div>
      <div style="min-width:0"><div class="t1" title="${esc(t.title)}">${t.node ? '🖧 ' : ''}${esc(t.title)}</div>
        <div class="t2">${meta}${t.speed ? `<span>${esc(t.speed)}</span>` : ''}${t.note ? `<span>${esc(t.note)}</span>` : ''}</div>${bar}</div>
      <div class="acts"><span class="st ${esc(t.st)}">${esc(label)}</span>${btn.join('')}</div></div>`;
  }).join('');
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

function render(s) {
  S = s;
  document.body.className = s.theme === 'light' ? 'light' : 'dark';
  $('#ver').textContent = 'v' + s.version;
  const br = $('#bridge'); br.textContent = s.bridge ? '● 瀏覽器已連動' : '瀏覽器未連動'; br.classList.toggle('ok', !!s.bridge);
  $('#pausebtn').textContent = s.paused ? '▶ 全部繼續' : '⏸ 全部暫停';
  const sig = JSON.stringify([s.kpis, s.tasks, s.nexus, s.update, s.notices.length, filter]);
  if (sig !== lastSig) {                   // 沒變就不重畫（不然捲動、hover 會一直被打斷）
    lastSig = sig;
    renderKpis(s.kpis); renderTasks(s.tasks); renderNexus(s.nexus); renderNews(window.NEWS, s.update); renderNotices(s.notices);
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
    if (p === 'home') return;
    if (p === 'about') {
      modal('關於 Video Helper', `版本 v${S ? S.version : ''}（新介面預覽）\n\n全部在你自己的電腦上跑，沒有伺服器。新介面只讀電腦版裡的資料、不連任何網站。\n下載管理、Nexus、設定這幾頁還在做，先用舊介面打開。`,
        [['說明書', 'manual', 'accent'], ['GitHub 發布頁', 'releases'], ['關閉', null]]).then((v) => v && act('link', v));
      return;
    }
    act('old', p);
  });
  document.addEventListener('click', async (e) => {
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
    if (r) {
      if (r.dataset.a === 'cancel' && !await modal('取消下載', '確定要取消嗎？已下載的部分會被刪除；排在後面的會繼續下載。', [['取消下載', 1, 'accent'], ['不要', 0]])) return;
      act(r.dataset.a, r.dataset.k);
    }
  });
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
  $('#bell').addEventListener('click', () => {
    const p = $('#panel'); p.hidden = !p.hidden;
    if (!p.hidden && S) seenN = S.notices.length;
    if (S) renderNotices(S.notices);
  });
  $('#clearn').addEventListener('click', () => { act('clear_notices'); seenN = 0; });
  document.addEventListener('contextmenu', (e) => { if (!e.target.closest('textarea')) e.preventDefault(); });
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
