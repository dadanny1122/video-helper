// HLS（m3u8）解析測試：直接從腳本裡拿出 parseMaster／parseMedia／resolveMedia 來跑固定的假資料（.github/hls/cases.json）
// 用法：node .github/hls_test.js video-helper.user.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const src = fs.readFileSync(process.argv[2] || 'video-helper.user.js', 'utf8');

// 從腳本裡找出某個函式的完整原始碼（數大括號，跳過字串和正規表示式裡的括號）
function grab(name) {
  const m = new RegExp(`(?:async )?function ${name}\\s*\\(`).exec(src);
  if (!m) throw new Error('腳本裡找不到 ' + name);
  let i = src.indexOf('{', m.index), depth = 0, q = null;
  for (let j = i; j < src.length; j++) {
    const c = src[j];
    if (q) { if (c === '\\') j++; else if (c === q) q = null; continue; }
    if (c === "'" || c === '"' || c === '`') { q = c; continue; }
    if (c === '/' && src[j + 1] === '/') { j = src.indexOf('\n', j); continue; }
    if (c === '/' && /[(,=:!&|?{};]\s*$/.test(src.slice(Math.max(0, j - 20), j))) {   // 正規表示式
      for (j++; j < src.length && src[j] !== '/'; j++) { if (src[j] === '\\') j++; else if (src[j] === '[') { while (src[++j] !== ']') if (src[j] === '\\') j++; } }
      continue;
    }
    if (c === '{') depth++;
    if (c === '}' && --depth === 0) return src.slice(m.index, j + 1);
  }
  throw new Error(name + ' 的括號沒對上');
}

const files = {};
const ctx = { URL, files, getData: async (u) => { await new Promise(r => setImmediate(r)); if (!(u in files)) throw new Error('404 ' + u); return files[u]; } };
vm.createContext(ctx);
for (const f of ['absUrl', 'parseMaster', 'parseMedia', 'resolveMedia']) vm.runInContext(grab(f), ctx);

const cases = JSON.parse(fs.readFileSync(path.join(__dirname, 'hls', 'cases.json'), 'utf8'));
let bad = 0;
const plain = x => JSON.parse(JSON.stringify(x));
for (const c of cases) {
  try {
    if (c.kind === 'master') {
      const got = plain(ctx.parseMaster(c.text, c.base)).map(v => { const o = { height: v.height, bw: v.bw, url: v.url }; if (v.audio) o.audio = v.audio; return o; });
      assert.deepStrictEqual(got.sort((a, b) => a.height - b.height), c.expect);
    } else {
      const got = plain(ctx.parseMedia(c.text, c.base));
      assert.deepStrictEqual({ segs: got.segs, dur: got.dur, init: got.init, encrypted: got.encrypted, byterange: !!got.byterange }, c.expect);
    }
    console.log('✔', c.name);
  } catch (e) {
    bad++;
    console.log(`::error title=HLS 解析::${c.name}：${e.message.split('\n').slice(0, 6).join(' ')}`);
  }
}

// resolveMedia：選最高畫質、空的總表、自己指自己（不能無限繞）
(async () => {
  const B = 'https://cdn.example.com/m.m3u8';
  const checks = [
    ['選最高畫質、帶頻寬', async () => {
      files[B] = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1x360\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=9,RESOLUTION=1x1080\nhi.m3u8\n';
      files['https://cdn.example.com/hi.m3u8'] = '#EXTM3U\n#EXTINF:2,\n1.ts\n';
      const r = await ctx.resolveMedia(B);
      assert.strictEqual(r.segs[0], 'https://cdn.example.com/1.ts');
      assert.strictEqual(r.bw, 9);
    }],
    ['畫質總表是空的 → 清楚的錯誤（不是 TypeError）', async () => {
      files[B] = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n';
      await assert.rejects(ctx.resolveMedia(B), e => !(e instanceof TypeError) && /畫質/.test(e.message));
    }],
    ['總表指向自己 → 停下來（不會無限繞）', async () => {
      files[B] = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1x720\nm.m3u8\n';
      await assert.rejects(ctx.resolveMedia(B), e => /層|太多/.test(e.message));
    }],
    ['聲音另外放 → 標記出來', async () => {
      files[B] = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="au.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1x720,AUDIO="a"\nv.m3u8\n';
      files['https://cdn.example.com/v.m3u8'] = '#EXTM3U\n#EXTINF:2,\n1.ts\n';
      const r = await ctx.resolveMedia(B);
      assert.strictEqual(r.audio, 'https://cdn.example.com/au.m3u8');
    }],
  ];
  for (const [name, fn] of checks) {
    try { await Promise.race([fn(), new Promise((_, no) => setTimeout(() => no(new Error('超過 2 秒沒結束（可能無限繞圈）')), 2000))]); console.log('✔', name); } catch (e) { bad++; console.log(`::error title=HLS 解析::${name}：${e.message}`); }
  }
  console.log(bad ? `${bad} 個沒過` : `全部 ${cases.length + checks.length} 個都過`);
  process.exit(bad ? 1 : 0);
})();
