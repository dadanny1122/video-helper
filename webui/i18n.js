// 英文版（v1.44.0，規格 docs/i18n/00-英文版規格.md）
// 中文原文當鑰匙、webui/en.json 是翻譯；找不到就照原文顯示（不會壞）。
// 英文模式下用 MutationObserver 把畫面上的中文整頁換掉 → app.js 和電腦版送來的訊息都不用改寫法。
// 規則跟 webui/i18n.py 的 tr() 一模一樣（webui_host_test 會核對）。
// 網站來的文字（影片標題、裝置名稱、更新紀錄）放在 translate="no" 裡面，不翻。
'use strict';
const I18N = (() => {
  const D = /*EN*/{ exact: {}, patterns: [] };
  const CJK = /[\u3400-\u9fff\uf900-\ufaff]/;
  const PUNCT = { '：': ': ', '（': ' (', '）': ')', '，': ', ', '。': '. ', '、': ', ', '「': '"', '」': '"', '『': '"', '』': '"',
    '！': '!', '？': '?', '；': '; ', '｜': ' | ', '・': ' · ', '～': '–', '　': ' ', '【': '[', '】': ']', '《': '"', '》': '"' };
  const SEPS = /(｜|・|；|。)/, SEP1 = new Set(['｜', '・', '；', '。']);
  const balanced = (x) => x.split('（').length === x.split('）').length && x.split('(').length === x.split(')').length;   // 括號要成對
  const edge = (c) => /[\s\p{P}\p{S}]/u.test(c);
  for (const [zh, en] of Object.entries(D.exact)) {     // 多行的句子也拆成一行一行（規則同 i18n.py _split_lines）
    const a = zh.split('\n'), b = en.split('\n');
    if (a.length > 1 && a.length === b.length) a.forEach((x, i) => { if (x.trim() && CJK.test(x) && !(x in D.exact)) D.exact[x] = b[i]; });
  }
  const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const PATS = (D.patterns || []).map(([zh, en]) => {
    const names = [];
    const body = zh.split(/(\{\w+\})/).map((p) => /^\{\w+\}$/.test(p) ? (names.push(p.slice(1, -1)), zh.includes('\n') ? '([\\s\\S]+?)' : '([^\\n]+?)') : reEsc(p)).join('');
    const lit = zh.replace(/\{\w+\}/g, '');
    return { re: new RegExp('^' + body + '$'), names, en, lit: lit.length, cjk: CJK.test(lit) };
  }).sort((a, b) => b.lit - a.lit);
  let lang = 'zh';

  const fixPunct = (s) => s.replace(/[：（），。、「」『』！？；｜・～　【】《》]/g, (c) => PUNCT[c]).replace(/ {2,}/g, ' ').replace(/\( /g, '(');
  function pattern(s, glue = false) {        // glue＝只有標點的樣板（例如「{a}：{b}」），最後才試
    for (const p of PATS) {
      if (p.cjk === glue) continue;
      const m = p.re.exec(s);
      if (!m) continue;
      const v = {}; p.names.forEach((n, i) => { v[n] = tr(m[i + 1]); });
      return p.en.replace(/\{(\w+)\}/g, (_, n) => v[n] ?? '');
    }
    return null;
  }
  function core(s) {
    if (Object.prototype.hasOwnProperty.call(D.exact, s)) return D.exact[s];
    return pattern(s);
  }
  // 一整段字 → 英文（翻不出來的部分保留中文）
  function tr(s) {
    s = String(s ?? '');
    if (!CJK.test(s)) return s;
    let r = core(s);
    if (r !== null) return r;
    const cs = [...s];
    let a = 0, b = cs.length;
    while (a < b && edge(cs[a])) a++;
    while (b > a && edge(cs[b - 1])) b--;
    if ((a > 0 || b < cs.length) && a < b) {
      for (const [x, y] of [[a, cs.length], [0, b], [a, b]]) {     // 前後的標點拿掉（只拿前面／只拿後面：括號要成對）
        r = (x === 0 && y === cs.length) || !balanced(cs.slice(x, y).join('')) ? null : core(cs.slice(x, y).join(''));
        if (r !== null) return fixPunct(cs.slice(0, x).join('')) + r + fixPunct(cs.slice(y).join('')).trimEnd();
      }
    }
    if (s.includes('\n')) return s.split('\n').map(tr).join('\n');      // 先一行一行翻
    if (SEPS.test(s)) {
      const parts = s.split(SEPS).filter((p) => p !== '');
      if (parts.length > 1) return parts.map((p) => (SEP1.has(p) ? fixPunct(p) : tr(p))).join('').trim();
    }
    r = pattern(s, true);
    if (r === null && (a > 0 || b < cs.length) && a < b) {
      r = pattern(cs.slice(a, b).join(''), true);
      if (r !== null) r = fixPunct(cs.slice(0, a).join('')) + r + fixPunct(cs.slice(b).join('')).trimEnd();
    }
    return r === null ? s : r;
  }

  // ---------- 畫面 ----------
  const ORIG = new WeakMap();      // 文字節點 → 原本的中文（切回中文用）
  const ATTRS = ['placeholder', 'title', 'aria-label'];
  const skip = (el) => el && el.closest && el.closest('[translate="no"], script, style, textarea');
  function doText(n) {
    const v = n.nodeValue;
    if (!v || !CJK.test(v) || skip(n.parentElement)) return;
    const t = v.trim(), out = tr(t);
    if (out === t) return;
    ORIG.set(n, v);
    n.nodeValue = v.slice(0, v.indexOf(t)) + out + v.slice(v.indexOf(t) + t.length);
  }
  function doEl(el) {
    if (el.closest('[translate="no"]')) return;
    for (const a of ATTRS) {
      const v = el.getAttribute(a);
      if (v && CJK.test(v)) { (el.__i18n = el.__i18n || {})[a] = v; el.setAttribute(a, tr(v)); }
    }
  }
  function walk(root) {
    if (root.nodeType === 3) { doText(root); return; }
    if (root.nodeType !== 1) return;
    doEl(root);
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let n = w.nextNode(); n; n = w.nextNode()) { if (n.nodeType === 3) doText(n); else doEl(n); }
  }
  const obs = typeof MutationObserver === 'undefined' ? null : new MutationObserver((list) => {
    for (const m of list) {
      if (m.type === 'characterData') doText(m.target);
      else if (m.type === 'attributes') doEl(m.target);
      else for (const n of m.addedNodes) walk(n);
    }
  });
  function restore() {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      if (n.nodeType === 3) { if (ORIG.has(n)) { n.nodeValue = ORIG.get(n); ORIG.delete(n); } }
      else if (n.__i18n) { for (const [a, v] of Object.entries(n.__i18n)) n.setAttribute(a, v); n.__i18n = null; }
    }
  }
  function set(l) {
    l = l === 'en' ? 'en' : 'zh';
    if (l === lang) return false;
    lang = l;
    document.documentElement.lang = l === 'en' ? 'en' : 'zh-Hant';
    document.body.classList.toggle('en', l === 'en');
    if (l === 'en') { walk(document.body); obs.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS }); }
    else { obs.disconnect(); restore(); }
    return true;
  }
  return { tr, set, get lang() { return lang; }, t: (s) => (lang === 'en' ? tr(s) : s), CJK };
})();
