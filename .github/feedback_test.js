// 意見回饋測試（v1.49.0，docs/infra/04-意見回饋與回饋獎勵規格.md）
//   ① 字數規則：腳本、新介面、Worker 三份 FB-RULE 跟 .github/feedback/rule.js 一模一樣，cases.json 全對（Python 由 feedback_test.py 比）
//   ② Worker：/feedback 收到 → 轉 Discord（不會 @ 到人）→ 回簽好的代碼；字數不夠、灌水、重複、太長都擋；/回饋者 給身分組、同一組代碼不能給兩個人
// 用法：node .github/feedback_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const ROOT = path.join(__dirname, '..');
let fails = 0;
const ok = (c, m) => { console.log((c ? '✅ ' : '❌ ') + m); if (!c) fails++; };

const block = (src) => {
  const a = src.indexOf('// FB-RULE-START'), b = src.indexOf('// FB-RULE-END');
  return a < 0 || b < 0 ? null : src.slice(a, b).split('\n').map((l) => l.trim()).join('\n');
};
const ref = fs.readFileSync(path.join(__dirname, 'feedback', 'rule.js'), 'utf8');
for (const f of ['video-helper.user.js', 'webui/app.js', 'cloudflare/worker.js']) {
  ok(block(fs.readFileSync(path.join(ROOT, f), 'utf8')) === block(ref), `字數規則：${f} 跟共用版一模一樣`);
}
const R = new Function(ref + '\nreturn { fbRule, fbMask };')();
for (const c of JSON.parse(fs.readFileSync(path.join(__dirname, 'feedback', 'cases.json'), 'utf8'))) {
  const t = c.text === 'LONG' ? c.unit.repeat(c.repeat) : c.text;
  const r = R.fbRule(t);
  ok(r.cjk === c.cjk && r.words === c.words && r.ok === c.ok && r.too_long === !!c.too_long,
    `規則：「${t.slice(0, 24)}」→ 中 ${r.cjk}、英 ${r.words}、${r.ok ? '可以送' : '不能送'}`);
}
ok(R.fbMask('看 https://x.com/a?b=1 和 www.y.com/z 這裡') === '看 [網址已移除] 和 [網址已移除] 這裡', '網址會被刪掉');

// ---- Worker ----
(async () => {
  const mod = await import(path.join(ROOT, 'cloudflare', 'worker.js'));
  const worker = mod.default;
  const kv = new Map();
  const KV = {
    get: async (k, type) => { const v = kv.get(k); return v == null ? null : type === 'json' ? JSON.parse(v) : v; },
    put: async (k, v) => { kv.set(k, v); },
  };
  const sent = [], roles = [];
  globalThis.fetch = async (url, init = {}) => {
    if (String(url).startsWith('https://discord.test/webhook')) { sent.push(JSON.parse(init.body)); return new Response(null, { status: 204 }); }
    if (String(url).includes('/roles/')) { roles.push(url); return new Response(null, { status: 204 }); }
    return new Response('{}', { status: 404 });
  };
  // Discord 互動要驗簽章：測試自己產生一組「假 Discord」金鑰
  const dk = crypto.generateKeyPairSync('ed25519');
  const dpub = dk.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32).toString('hex');
  const env = { FEEDBACK_KV: KV, DISCORD_FEEDBACK_WEBHOOK: 'https://discord.test/webhook/1', FEEDBACK_ROLE_ID: '999', GUILD_ID: 'g1',
    DISCORD_PUBLIC_KEY: dpub, DISCORD_BOT_TOKEN: 'x' };
  const post = (body, ip = '1.2.3.4') => worker.fetch(new Request('https://w.test/feedback', { method: 'POST', body: JSON.stringify(body),
    headers: { 'CF-Connecting-IP': ip } }), env, { waitUntil() {} }).then(async (r) => ({ status: r.status, ...(await r.json()) }));
  const good = (n) => ({ kind: 'suggest', text: `第${n}則：這個工具很好用但是希望可以多一點功能 @everyone https://evil.example/x`, app: 'desktop', ver: '1.49.0', lang: 'zh' });

  const r1 = await post(good(1));
  ok(r1.status === 200 && r1.ok && /^VH1\./.test(r1.token), '送出成功、拿到代碼');
  const m = sent[0] || {};
  ok(m.allowed_mentions && Array.isArray(m.allowed_mentions.parse) && m.allowed_mentions.parse.length === 0, 'Discord 不會 @ 到任何人（@everyone 沒效）');
  ok(m.embeds && !/evil\.example/.test(m.embeds[0].description) && /\[網址已移除\]/.test(m.embeds[0].description), '網址被刪掉才貼到 Discord');
  ok(m.embeds && !/1\.2\.3\.4/.test(JSON.stringify(m)), 'Discord 看不到 IP');
  ok(![...kv.keys()].some((k) => k.includes('1.2.3.4')) && ![...kv.values()].some((v) => String(v).includes('1.2.3.4')), 'KV 裡沒有原本的 IP（只存雜湊）');
  // 代碼要能用公鑰驗
  const pub = (await (await worker.fetch(new Request('https://w.test/feedback/pubkey'), env, {})).json()).pub;
  const [, p, s] = r1.token.split('.');
  const key = crypto.createPublicKey({ key: Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), Buffer.from(pub, 'base64url')]), format: 'der', type: 'spki' });
  ok(crypto.verify(null, Buffer.from(p), key, Buffer.from(s, 'base64url')), '代碼用公鑰驗證通過（電腦版、腳本可以離線驗）');
  ok(r1.pub === pub, '回應附上公鑰');

  ok((await post({ ...good(9), text: '太短了' })).status === 400, '字數不夠 → 400');
  ok((await post({ ...good(9), text: '好'.repeat(40) })).status === 400, '一直重複同一個字 → 400');
  ok((await post({ ...good(9), text: '好用'.repeat(600) })).status === 400, '超過 1000 字 → 400');
  ok((await post({ ...good(9), kind: 'hack' })).status === 400, '類型不對 → 400');
  ok((await post(good(1))).status === 409, '一模一樣的內容 → 409（不收）');
  ok((await post(good(2))).status === 200 && (await post(good(3))).status === 200, '同一個人第 2、3 則可以');
  const r4 = await post(good(4));
  ok(r4.status === 429, '同一個人一小時第 4 則 → 429（每小時最多 3 則）');
  ok((await post(good(5), '5.6.7.8')).status === 200, '別人不受影響');
  ok(sent.length === 4, `Discord 總共收到 4 則（${sent.length}）`);
  ok((await worker.fetch(new Request('https://w.test/feedback', { method: 'POST', body: 'x'.repeat(9000) }), env, {})).status === 413, '太大的請求 → 413');
  const noEnv = await worker.fetch(new Request('https://w.test/feedback', { method: 'POST', body: JSON.stringify(good(7)) }), {}, {});
  ok(noEnv.status === 503, '還沒設定好 → 503');

  // /回饋者
  const interact = async (code, uid) => {
    const body = JSON.stringify({ type: 2, guild_id: 'g1', member: { user: { id: uid } }, data: { name: 'feedback-role', options: [{ name: 'code', value: code }] } });
    const ts = '1700000000';
    const sig = crypto.sign(null, Buffer.from(ts + body), dk.privateKey).toString('hex');
    const r = await worker.fetch(new Request('https://w.test/', { method: 'POST', body, headers: { 'X-Signature-Ed25519': sig, 'X-Signature-Timestamp': ts } }), env, { waitUntil() {} });
    return (await r.json()).data.content;
  };
  ok(/拿到/.test(await interact(r1.token, 'u1')) && roles.length === 1 && roles[0].includes('/guilds/g1/members/u1/roles/999'), '/回饋者 真代碼 → 給身分組');
  ok(/拿到/.test(await interact(r1.token, 'u1')), '同一個人再用同一組 → 照樣成功（重複給沒關係）');
  ok(/別人用過/.test(await interact(r1.token, 'u2')), '同一組代碼給別人用 → 拒絕');
  ok(/代碼不對/.test(await interact(r1.token.slice(0, -3) + 'AAA', 'u3')), '改過的代碼 → 拒絕');
  ok(/代碼不對/.test(await interact('VH1.abc.def', 'u3')), '亂打 → 拒絕');

  // 電腦版（Python）也驗得過 Worker 簽的代碼
  const py = require('child_process').spawnSync(process.env.PYTHON || 'python3', ['-I', '-c',
    'import sys,json; sys.path.insert(0, sys.argv[1]); import vh_feedback as F; d=json.loads(sys.argv[2]); ' +
    'print("OK" if F.verify_token(d["t"], d["p"]) and F.rewards([d["t"]], d["p"])["level"] == "bronze" and not F.verify_token(d["t"][:-2] + "AA", d["p"]) else "BAD")',
    ROOT, JSON.stringify({ t: r1.token, p: pub })], { encoding: 'utf8' });
  ok(/OK/.test(py.stdout), '電腦版（Python）驗得過 Worker 的代碼、改過的不過' + (py.stderr ? '：' + py.stderr.slice(-200) : ''));

  console.log(fails ? `❌ ${fails} 項沒過` : '全部通過');
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.log('❌ ' + (e.stack || e)); process.exit(1); });
