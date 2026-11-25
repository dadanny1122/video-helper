// Video Helper 的 Discord 指令機器人（放在 Cloudflare Workers，免費）
// 指令：
//   /快照   → 叫 GitHub 截一張「即時介面」（1～2 分鐘）
//   /換匯   → 用台銀牌價算「數量 × 價格」（1 秒內，直接讀 GitHub 上的 rates.json）
//   /匯率   → 某個貨幣的現價、漲跌、30 天位置
//   /排行榜 → 前 10 名＋可下載的網站
//   /狀態   → 各個機器人最後一次跑的時間、成功或失敗
//   /天氣   → 某個地點、某個時間會不會下雨（讀 GitHub 上的 weather_state.json）
//   /回饋者 → 輸入回饋代碼，拿「回饋者」身分組
//   POST /feedback（電腦版、腳本送意見回饋）、GET /feedback/pubkey（簽名公鑰）
//
// 要在 Cloudflare 設定的變數（Settings → Variables and Secrets，全部選 Secret）：
//   DISCORD_PUBLIC_KEY  Discord 開發者網站 → General Information → Public Key
//   DISCORD_APP_ID      Discord 開發者網站 → General Information → Application ID
//   DISCORD_BOT_TOKEN   Discord 開發者網站 → Bot → Reset Token（只在註冊指令時用）
//   GITHUB_TOKEN        GitHub 的 fine-grained token（只給 video-helper 的 Actions 讀寫）
//   ── 意見回饋（v1.48.0，docs/infra/04-意見回饋與回饋獎勵規格.md）──
//   DISCORD_FEEDBACK_WEBHOOK  Discord #意見回饋 頻道的 Webhook 網址
//   GUILD_ID / FEEDBACK_ROLE_ID  你的伺服器 ID、「回饋者」身分組 ID（/回饋者 指令給身分組用）
//   FEEDBACK_KV（KV 綁定，不是 Secret）：記防灌水次數、重複內容、簽名金鑰（第一次用自己產生，私鑰只在這裡）
//
// 第一次設定好、或改了指令之後，用瀏覽器打開「你的 Worker 網址/register」註冊指令

const REPO = 'dadanny1122/video-helper';
const RAW = `https://raw.githubusercontent.com/${REPO}/bot-data/`;   // S4：機器人的狀態檔在 bot-data 分支（不是 main）
const CALC = 'https://dadanny1122.github.io/video-helper/fx/calc.html';
const DASH = 'https://dadanny1122.github.io/video-helper/dash/';
const BOT_PAGE = 'https://rate.bot.com.tw/xrt?Lang=zh-TW';
const CCY = { USD: '美金', EUR: '歐元', JPY: '日圓', CNY: '人民幣', GBP: '英鎊', HKD: '港幣', CHF: '瑞士法郎', AUD: '澳幣', KRW: '韓元' };
const CCY_CHOICES = Object.entries(CCY).map(([v, zh]) => ({ name: `${v} ${zh}`, value: v }));
const DIG = c => (c === 'JPY' || c === 'KRW') ? 0 : 2;
const nf = (v, d) => Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const rf = v => v == null ? '—' : v < 1 ? v.toFixed(4) : v < 10 ? v.toFixed(3) : v.toFixed(2);
const getJson = p => fetch(RAW + p + '?t=' + Date.now(), { cf: { cacheTtl: 30 } }).then(r => { if (!r.ok) throw new Error('GitHub ' + r.status); return r.json(); });
const reply = (embed, flags) => json({ type: 4, data: { embeds: [{ color: 0xF57C00, ...embed }], ...(flags ? { flags } : {}) } });
const opt = (i, n) => ((i.data.options || []).find(o => o.name === n) || {}).value;
const midOf = r => r.spot_buy && r.spot_sell ? (r.spot_buy + r.spot_sell) / 2 : (r.cash_buy + r.cash_sell) / 2;

// FB-RULE-START 意見回饋字數規則（跟電腦版 vh_feedback.py、腳本、新介面、Worker 同一套；.github/feedback_test.* 會比對）
const FB_CJK = /[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]/;
const FB_URL = /(?:https?:\/\/|www\.)\S+/gi;
const FB_WORD = /[A-Za-z0-9]+(?:'[A-Za-z]+)?/g;
const FB_NEED_CJK = 16, FB_NEED_WORDS = 20, FB_MAX = 1000;
function fbRuns(items, keep) {
  let n = 0;
  for (let i = 0; i < items.length;) {
    let j = i;
    while (j < items.length && items[j] === items[i]) j++;
    if (!keep || keep(items[i])) n += j - i > 5 ? 1 : j - i;
    i = j;
  }
  return n;
}
function fbRule(text) {
  const raw = text == null ? '' : String(text);
  const clean = raw.replace(FB_URL, ' ');
  const cjk = fbRuns(Array.from(clean), (c) => FB_CJK.test(c));
  const words = fbRuns((clean.match(FB_WORD) || []).map((w) => w.toLowerCase()));
  const tooLong = Array.from(raw).length > FB_MAX;
  return { cjk, words, too_long: tooLong, ok: (cjk >= FB_NEED_CJK || words >= FB_NEED_WORDS) && !tooLong };
}
function fbMask(text) { return String(text).replace(FB_URL, '[網址已移除]'); }
// FB-RULE-END

// ---- 意見回饋 ----
const FB_KINDS = { suggest: '💡 建議', bug: '🐞 問題', praise: '👍 稱讚', other: '💬 其他' };
const FB_PER_HOUR = 3, FB_PER_DAY = 10, FB_DUP_DAYS = 30;           // F2
const b64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const unb64u = (s) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - s.length % 4) % 4)), (c) => c.charCodeAt(0));
const sha256hex = async (t) => [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(t)))].map((b) => b.toString(16).padStart(2, '0')).join('');
const fbJson = (o, status = 200) => new Response(JSON.stringify(o), { status, headers: { 'Content-Type': 'application/json; charset=utf-8' } });

async function fbKeys(env) {
  // 簽名金鑰：第一次用時自己產生、存在 KV（私鑰不離開 Cloudflare，也不會經過任何對話或 GitHub）
  let k = await env.FEEDBACK_KV.get('fb:key', 'json');
  if (!k) {
    const pair = await crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify']);
    k = { priv: b64u(await crypto.subtle.exportKey('pkcs8', pair.privateKey)), pub: b64u(await crypto.subtle.exportKey('raw', pair.publicKey)) };
    await env.FEEDBACK_KV.put('fb:key', JSON.stringify(k));
    k = (await env.FEEDBACK_KV.get('fb:key', 'json')) || k;          // 兩個請求同時產生時，以先存進去的為準
  }
  return k;
}

async function fbSign(env, payload) {
  const k = await fbKeys(env);
  const key = await crypto.subtle.importKey('pkcs8', unb64u(k.priv), { name: 'Ed25519' }, false, ['sign']);
  const p = b64u(new TextEncoder().encode(JSON.stringify(payload)));
  return `VH1.${p}.${b64u(await crypto.subtle.sign({ name: 'Ed25519' }, key, new TextEncoder().encode(p)))}`;
}

async function fbVerify(env, token) {
  try {
    const [tag, p, s] = String(token || '').trim().split('.');
    if (tag !== 'VH1' || !p || !s || token.length > 400) return null;
    const k = await fbKeys(env);
    const key = await crypto.subtle.importKey('raw', unb64u(k.pub), { name: 'Ed25519' }, false, ['verify']);
    if (!await crypto.subtle.verify({ name: 'Ed25519' }, key, unb64u(s), new TextEncoder().encode(p))) return null;
    const d = JSON.parse(new TextDecoder().decode(unb64u(p)));
    return d.v === 1 && /^[0-9a-f]{16}$/.test(d.id) ? d : null;
  } catch (e) { return null; }
}

async function handleFeedback(req, env) {
  if (!env.FEEDBACK_KV || !env.DISCORD_FEEDBACK_WEBHOOK) return fbJson({ ok: false, msg: '意見回饋還沒設定好，等開發者設定' }, 503);
  const raw = await req.text();
  if (raw.length > 8000) return fbJson({ ok: false, msg: '內容太長' }, 413);
  let b;
  try { b = JSON.parse(raw); } catch (e) { return fbJson({ ok: false, msg: '格式不對' }, 400); }
  const kind = String(b.kind || ''), text = typeof b.text === 'string' ? b.text : '';
  const app = b.app === 'script' ? '瀏覽器腳本' : b.app === 'desktop' ? '電腦版' : '';
  const ver = /^[\w.\-]{1,20}$/.test(String(b.ver || '')) ? String(b.ver) : '?';
  const lang = b.lang === 'en' ? 'en' : 'zh';
  if (!FB_KINDS[kind] || !app) return fbJson({ ok: false, msg: '類型不對' }, 400);
  const r = fbRule(text);
  if (!r.ok) return fbJson({ ok: false, msg: r.too_long ? `太長了（最多 ${FB_MAX} 字）` : `字數不夠：中文要 ${FB_NEED_CJK} 字以上，或英文 ${FB_NEED_WORDS} 個單字以上` }, 400);
  // 防灌水：同一個 IP 每小時 3 則、每天 10 則（只存雜湊，24～48 小時自動刪）；一樣的內容 30 天不收
  const now = new Date(), day = now.toISOString().slice(0, 10), hour = now.toISOString().slice(0, 13);
  const who = await sha256hex((req.headers.get('CF-Connecting-IP') || '') + '|' + day);
  const hKey = `rl:h:${who}:${hour}`, dKey = `rl:d:${who}:${day}`;
  const [h, d] = await Promise.all([env.FEEDBACK_KV.get(hKey), env.FEEDBACK_KV.get(dKey)]);
  if (+h >= FB_PER_HOUR || +d >= FB_PER_DAY) return fbJson({ ok: false, msg: `送太多次了：每小時最多 ${FB_PER_HOUR} 則、每天最多 ${FB_PER_DAY} 則，晚一點再寫` }, 429);
  const masked = fbMask(text);
  const dup = `dup:${await sha256hex(masked.replace(/\s+/g, ' ').trim().toLowerCase())}`;
  if (await env.FEEDBACK_KV.get(dup)) return fbJson({ ok: false, msg: '這段內容已經送過了' }, 409);
  const id = [...crypto.getRandomValues(new Uint8Array(8))].map((x) => x.toString(16).padStart(2, '0')).join('');
  const post = await fetch(env.DISCORD_FEEDBACK_WEBHOOK, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'Video Helper 意見回饋', allowed_mentions: { parse: [] },
      embeds: [{ title: FB_KINDS[kind], description: masked.slice(0, 3900), color: 0x8B7CFF,
                 footer: { text: `${app} v${ver}｜${lang === 'en' ? 'English' : '中文'}｜#${id.slice(0, 6)}` }, timestamp: now.toISOString() }] }),
  });
  if (!post.ok) return fbJson({ ok: false, msg: `Discord 沒收到（${post.status}），等一下再試` }, 502);
  await Promise.all([
    env.FEEDBACK_KV.put(hKey, String(+h + 1), { expirationTtl: 7200 }),
    env.FEEDBACK_KV.put(dKey, String(+d + 1), { expirationTtl: 172800 }),
    env.FEEDBACK_KV.put(dup, '1', { expirationTtl: FB_DUP_DAYS * 86400 }),
  ]);
  const k = await fbKeys(env);
  return fbJson({ ok: true, token: await fbSign(env, { v: 1, id, t: Math.floor(now.getTime() / 1000) }), pub: k.pub });
}

async function cmdFeedbackRole(i, env) {
  const eph = (content) => json({ type: 4, data: { content, flags: 64 } });
  if (!env.FEEDBACK_KV || !env.FEEDBACK_ROLE_ID) return eph('回饋者身分組還沒設定好，等開發者設定。');
  const user = (i.member && i.member.user) || i.user;
  if (!i.guild_id || !user) return eph('請在伺服器的頻道裡打這個指令（不能用私訊）。');
  if (env.GUILD_ID && i.guild_id !== env.GUILD_ID) return eph('這個伺服器不能用。');
  const d = await fbVerify(env, opt(i, 'code'));
  if (!d) return eph('❌ 代碼不對。請到電腦版「💬 意見回饋」或腳本盾牌面板複製完整的代碼（VH1. 開頭）。');
  const usedBy = await env.FEEDBACK_KV.get(`used:${d.id}`);
  if (usedBy && usedBy !== user.id) return eph('❌ 這組代碼已經被別人用過了。');
  const r = await fetch(`https://discord.com/api/v10/guilds/${i.guild_id}/members/${user.id}/roles/${env.FEEDBACK_ROLE_ID}`, {
    method: 'PUT', headers: { Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`, 'X-Audit-Log-Reason': 'Video Helper feedback reward' } });
  if (!r.ok) return eph(`給身分組失敗（${r.status}）：請開發者確認機器人的身分組排在「回饋者」上面、而且有「管理身分組」權限。`);
  await env.FEEDBACK_KV.put(`used:${d.id}`, user.id);
  return eph('🎭 你拿到「回饋者」身分組了，謝謝你的意見！');
}

// 指令清單（英文名字給程式用，zh-TW 是 Discord 上看到的名字）
const COMMANDS = [
  { name: 'feedback-role', name_localizations: { 'zh-TW': '回饋者' }, type: 1,
    description: 'Get the Feedback role with your feedback code', description_localizations: { 'zh-TW': '輸入意見回饋拿到的代碼，領「回饋者」身分組' },
    options: [{ type: 3, name: 'code', name_localizations: { 'zh-TW': '代碼' }, description: 'Your code (starts with VH1.)',
                description_localizations: { 'zh-TW': '回饋代碼（VH1. 開頭）' }, required: true, max_length: 400 }] },
  { name: 'snapshot', name_localizations: { 'zh-TW': '快照' }, type: 1,
    description: 'Take a snapshot of the live dashboard now', description_localizations: { 'zh-TW': '馬上截一張「即時介面」的圖（約 1～2 分鐘）' } },
  { name: 'exchange', name_localizations: { 'zh-TW': '換匯' }, type: 1,
    description: 'Calculate amount x Bank of Taiwan rate', description_localizations: { 'zh-TW': '用台銀牌價算：數量 × 價格' },
    options: [
      { type: 3, name: 'currency', name_localizations: { 'zh-TW': '貨幣' }, description: 'Currency', description_localizations: { 'zh-TW': '哪種貨幣' }, required: true, choices: CCY_CHOICES },
      { type: 10, name: 'amount', name_localizations: { 'zh-TW': '數量' }, description: 'Amount', description_localizations: { 'zh-TW': '數量（買／賣外幣＝外幣數量；用台幣換＝台幣金額）' }, required: true, min_value: 0 },
      { type: 3, name: 'mode', name_localizations: { 'zh-TW': '方式' }, description: 'What to do', description_localizations: { 'zh-TW': '要做什麼（不選＝買外幣）' }, choices: [
        { name: '買外幣：我要買這麼多外幣，要付多少台幣', value: 'buy' },
        { name: '用台幣換：我有這麼多台幣，換得到多少外幣', value: 'twd' },
        { name: '賣外幣：我要賣這麼多外幣，拿回多少台幣', value: 'sell' }] },
      { type: 3, name: 'kind', name_localizations: { 'zh-TW': '現金或即期' }, description: 'Cash or spot', description_localizations: { 'zh-TW': '現金＝換鈔票、即期＝外幣帳戶（不選＝現金）' }, choices: [
        { name: '現金（換鈔票）', value: 'cash' }, { name: '即期（外幣帳戶）', value: 'spot' }] }] },
  { name: 'rate', name_localizations: { 'zh-TW': '匯率' }, type: 1,
    description: 'Current rate of a currency', description_localizations: { 'zh-TW': '查某個貨幣的現價、漲跌、30 天位置' },
    options: [{ type: 3, name: 'currency', name_localizations: { 'zh-TW': '貨幣' }, description: 'Currency', description_localizations: { 'zh-TW': '哪種貨幣' }, required: true, choices: CCY_CHOICES }] },
  { name: 'rank', name_localizations: { 'zh-TW': '排行榜' }, type: 1,
    description: 'Top 10 video sites', description_localizations: { 'zh-TW': '影片網站排行前 10 名＋哪些可以下載' } },
  { name: 'weather', name_localizations: { 'zh-TW': '天氣' }, type: 1,
    description: 'Will it rain at that place and time?', description_localizations: { 'zh-TW': '查某個地點、某個時間會不會下雨' },
    options: [
      { type: 3, name: 'place', name_localizations: { 'zh-TW': '地點' }, description: 'Place', description_localizations: { 'zh-TW': '哪裡' }, required: true,
        choices: ['大園', '青埔（中壢）', '土城', '台北101（信義）', '竹科（新竹東區）', '竹北'].map(n => ({ name: n, value: n })) },
      { type: 4, name: 'hour', name_localizations: { 'zh-TW': '幾點' }, description: 'Hour (0-23)', description_localizations: { 'zh-TW': '幾點（0～23，不填＝現在）' }, min_value: 0, max_value: 23 },
      { type: 3, name: 'day', name_localizations: { 'zh-TW': '哪天' }, description: 'Day', description_localizations: { 'zh-TW': '哪一天（不填＝今天）' },
        choices: [{ name: '今天', value: '0' }, { name: '明天', value: '1' }, { name: '後天', value: '2' }] }] },
  { name: 'arena', name_localizations: { 'zh-TW': '擂台' }, type: 1,
    description: 'Which FX forecast method is most accurate', description_localizations: { 'zh-TW': '匯率預報擂台：哪種預報方法比較準（每週六更新）' } },
  { name: 'invoice', name_localizations: { 'zh-TW': '對獎' }, type: 1,
    description: 'Check a receipt lottery number', description_localizations: { 'zh-TW': '統一發票對獎（最近兩期）' },
    options: [{ type: 3, name: 'number', name_localizations: { 'zh-TW': '號碼' }, description: '8-digit number',
      description_localizations: { 'zh-TW': '發票號碼（8 碼數字，英文字母不用打；只打末 3 碼也可以先看）' }, required: true, min_length: 3, max_length: 12 }] },
  { name: 'oil', name_localizations: { 'zh-TW': '油價' }, type: 1,
    description: 'CPC fuel prices this week', description_localizations: { 'zh-TW': '這週中油油價、跟上週比' } },
  { name: 'status', name_localizations: { 'zh-TW': '狀態' }, type: 1,
    description: 'Are the bots running?', description_localizations: { 'zh-TW': '各個機器人有沒有正常在跑' } },
];

// ---- /換匯 ----
async function cmdExchange(i) {
  const c = opt(i, 'currency'), a = Number(opt(i, 'amount')), mode = opt(i, 'mode') || 'buy';
  let kind = opt(i, 'kind') || 'cash';
  const d = await getJson('docs/fx/rates.json'), r = d.rates[c];
  if (!r) return reply({ description: '讀不到這個貨幣的牌價' }, 64);
  let note = '';
  if (kind === 'spot' && !(r.spot_buy && r.spot_sell)) { kind = 'cash'; note = '（這個貨幣沒有即期報價，改用現金）'; }
  const kn = kind === 'cash' ? '現金' : '即期', zh = CCY[c];
  let title, formula, extra = '';
  if (mode === 'buy') {
    const p = r[kind + '_sell'], t = a * p;
    title = `買 ${nf(a, DIG(c))} ${zh}，要付 **NT$ ${nf(t, 0)}**`;
    formula = `${nf(a, DIG(c))} ${c} × ${rf(p)}（銀行${kn}賣出）= ${nf(t, 0)} 台幣`;
  } else if (mode === 'twd') {
    const p = r[kind + '_sell'], q = a / p;
    title = `NT$ ${nf(a, 0)} 換得到 **${nf(q, DIG(c))} ${zh}**`;
    formula = `${nf(a, 0)} 台幣 ÷ ${rf(p)}（銀行${kn}賣出）= ${nf(q, DIG(c))} ${c}`;
    if (kind === 'cash' && r.avg30_cash_sell) {
      const diff = q - a / r.avg30_cash_sell;
      extra = `\n跟近 30 天平均價比：${diff >= 0 ? '多換' : '少換'} ${nf(Math.abs(diff), DIG(c))} ${c}`;
    }
  } else {
    const p = r[kind + '_buy'], t = a * p;
    title = `賣 ${nf(a, DIG(c))} ${zh}，拿回 **NT$ ${nf(t, 0)}**`;
    formula = `${nf(a, DIG(c))} ${c} × ${rf(p)}（銀行${kn}買入）= ${nf(t, 0)} 台幣`;
  }
  return reply({ title: `🧮 換匯試算｜${r.flag} ${zh}`, url: CALC,
    description: `${title}${note}\n\`${formula}\`${extra}\n\n🏦 台銀掛牌：${d.quote_time || '—'}\n` +
                 `📎 [臺灣銀行牌告匯率](${BOT_PAGE})｜[🧮 網頁計算機（可選任一分鐘的價格）](${CALC})\n只是試算，實際成交可能有手續費；不是買賣建議` });
}

// ---- /匯率 ----
async function cmdRate(i) {
  const c = opt(i, 'currency'), d = await getJson('docs/fx/rates.json'), r = d.rates[c];
  if (!r) return reply({ description: '讀不到這個貨幣的牌價' }, 64);
  const m = midOf(r), pct = r.prev_mid ? (m / r.prev_mid - 1) * 100 : null;
  const pos = r.pctl30 == null ? '' : `${r.pctl30 <= 20 ? '🟢 偏便宜' : r.pctl30 >= 80 ? '🔴 偏貴' : '🟡 普通'}（30 天位置 ${r.pctl30}／100）`;
  const got = 10000 / r.cash_sell;
  return reply({ title: `💱 ${r.flag} ${CCY[c]} ${c}`, url: `https://rate.bot.com.tw/xrt/quote/day/${c}`,
    description: `**中間價 ${rf(m)}**　${pct == null ? '' : (pct > 0 ? '🔺 +' : pct < 0 ? '🔻 ' : '▫ ') + pct.toFixed(2) + '%（比前一營業日）'}\n${pos}\n\n` +
      `現金　買 ${rf(r.cash_buy)}／賣 ${rf(r.cash_sell)}\n即期　買 ${rf(r.spot_buy)}／賣 ${rf(r.spot_sell)}\n` +
      `💵 1 萬台幣換現鈔 → **${nf(got, DIG(c))} ${CCY[c]}**\n\n🏦 台銀掛牌：${d.quote_time || '—'}｜更新：${d.updated}\n` +
      `📎 [今日各時段牌價](https://rate.bot.com.tw/xrt/quote/day/${c})｜[🧮 計算機](${CALC})｜[📊 即時介面](${DASH})` });
}

// ---- /對獎、/油價（讀 .github/life_state.json）----
const PRIZE = { 8: '頭獎 20 萬', 7: '二獎 4 萬', 6: '三獎 1 萬', 5: '四獎 4 千', 4: '五獎 1 千', 3: '六獎 200 元' };
async function cmdInvoice(i) {
  const num = String(opt(i, 'number') || '').replace(/\D/g, '');
  if (num.length < 3) return reply({ title: '🧾 對獎', description: '請打發票號碼的數字（至少末 3 碼）' }, 64);
  const d = await getJson('.github/life_state.json');
  const periods = (d.invoice || []).slice(0, 2);                       // 能領的只有最近兩期
  const lines = [];
  for (const v of periods) {
    let hit = '';
    if (num.length === 8 && num === v.special) hit = '🏆 特別獎 1,000 萬！';
    else if (num.length === 8 && num === v.grand) hit = '🥇 特獎 200 萬！';
    else {
      let best = 0;
      for (const f of v.first) { let k = 0; while (k < Math.min(8, num.length) && num[num.length - 1 - k] === f[f.length - 1 - k]) k++; best = Math.max(best, k); }
      if (best >= 3) hit = num.length < 8 && best === num.length ? `🎫 末 ${best} 碼有中（打完整 8 碼才知道是幾獎）` : `🎉 ${PRIZE[best]}`;
      else if ((v.extra || []).includes(num.slice(-3))) hit = '🎉 增開六獎 200 元';
      else if (num.length < 8 && (num === v.special.slice(-num.length) || num === v.grand.slice(-num.length)))
        hit = '👀 末幾碼跟特別獎／特獎一樣，打完整 8 碼確認';
    }
    lines.push(`**${v.period}**：${hit || '沒有中'}`);
  }
  return reply({ title: `🧾 對獎：${num}`, url: periods[0] && periods[0].link,
    description: lines.join('\n') + '\n\n只對最近兩期（還能領獎的）。中了記得在期限內兌獎！\n資料：財政部（每單數月 25 日開獎）' }, 64);   // 只有你看得到
}

async function cmdOil() {
  const d = await getJson('.github/life_state.json');
  const o = d.oil || {};
  const rows = Object.entries(o.prices || {}).map(([n, p]) => {
    const q = (o.prev || {})[n];
    const diff = q == null ? '' : Math.abs(p - q) < 0.005 ? '　持平' : `　${p > q ? '🔺' : '🔻'} ${(p - q > 0 ? '+' : '') + (p - q).toFixed(1)}`;
    return `${n.padEnd(6, '　')} ${p.toFixed(1)} 元${diff}`;
  });
  return reply({ title: `⛽ 中油油價（${o.date || '?'} 起）`, url: 'https://www.cpc.com.tw/historyprice.aspx?n=2890',
    description: '```\n' + rows.join('\n') + '\n```每公升，中油直營站；跟上週比\n資料：台灣中油｜更新：' + (d.updated || '?') });
}

// ---- /擂台 ----
async function cmdArena() {
  const d = await getJson('docs/fx/arena.json');
  const dir = Object.entries(d.dir).filter(([, x]) => x.n).map(([m, x]) => [m, x.k / x.n * 100, x.n]).sort((a, b) => b[1] - a[1]);
  const rng = Object.entries(d.rng).filter(([, x]) => x.n).map(([m, x]) => [m, x.in / x.n * 100, x.w / x.n]).sort((a, b) => Math.abs(a[1] - 68) - Math.abs(b[1] - 68));
  const medal = ['🥇', '🥈', '🥉', '4.', '5.'];
  return reply({ title: '🏟 匯率預報擂台', url: 'https://dadanny1122.github.io/video-helper/fx/#arena',
    description: '**方向：猜 5 個營業日後會漲還是跌**（丟硬幣＝50%）\n' +
      dir.map(([m, p, n], k) => `${medal[k]} ${m}　**${p.toFixed(1)}%**（考 ${n} 次）`).join('\n') +
      '\n\n**範圍：說 68% 會落在裡面，實際幾 %**（越接近 68% 越好）\n' +
      rng.map(([m, c, w], k) => `${medal[k]} ${m}　**${c.toFixed(1)}%**（寬 ${w.toFixed(2)}%）`).join('\n') +
      `\n\n「永遠猜漲」是及格線：贏不了它就是沒學到東西。過去 6 年 USD／EUR／JPY／CNY 回測，每 5 天考一次、不偷看未來。\n更新：${d.updated}｜不是投資建議` });
}

// ---- /排行榜 ----
async function cmdRank() {
  const d = await getJson('.github/rank_state.json');
  const FULL = ['pornhub', 'xvideos', 'xnxx', 'redtube', 'youporn', 'xhamster', 'tube8', 'youjizz', 'spankbang'];
  const isDl = n => FULL.includes(n.replace(/\s*\(.*\)$/, '').trim().toLowerCase());
  const lines = (d.rows || []).slice(0, 10).map(([p, n, u]) => `\`${String(p).padStart(2)}.\` ${isDl(n) ? '✅' : '🛡'} [${n}](${u})`);
  const dl = (d.rows || []).filter(([, n]) => isDl(n)).map(([, n]) => n);
  return reply({ title: '🏆 影片網站排行 Top 10', url: 'https://theporndude.com/zh/top-porn-tube-sites',
    description: lines.join('\n') + `\n\n✅ 可下載＝擋廣告＋下載｜🛡 只擋廣告\n**可以下載的（前 20 名裡 ${dl.length} 個）：**${dl.join('、')}\n` +
                 `🕒 最後更新：${d.updated || '—'}（每天 20:00）｜[📊 即時介面](${DASH})` });
}

// ---- /天氣 ----
async function cmdWeather(i) {
  const d = (await getJson('.github/weather_state.json')).dash || {};
  const n = opt(i, 'place'), off = Number(opt(i, 'day') || 0);
  const tw = new Date(Date.now() + 8 * 3600e3);                     // 台灣時間（用 UTC 欄位讀）
  const h = opt(i, 'hour') ?? tw.getUTCHours();
  const day = new Date(Date.UTC(tw.getUTCFullYear(), tw.getUTCMonth(), tw.getUTCDate() + off));
  const ymd = day.toISOString().slice(0, 10);
  const t = new Date(`${ymd}T${String(h).padStart(2, '0')}:00:00+08:00`);
  const b = ((d.blocks || {})[n] || []).find(x => new Date(x[0]) <= t && t < new Date(x[1]));
  const label = `${['今天', '明天', '後天'][off]} ${String(h).padStart(2, '0')}:00`;
  if (!b || b[2] == null) return reply({ description: `${n} ${label}：超出預報範圍（只有未來約 2 天）` }, 64);
  const temps = (d.temps || {})[n] || [];
  const tp = temps.length ? temps.reduce((a, x) => Math.abs(new Date(x[0]) - t) < Math.abs(new Date(a[0]) - t) ? x : a)[1] : null;
  const hz = Object.entries(d.warnings || {}).flatMap(([c, hs]) => hs.map(x => `${c} ${x.phen}${x.sig}`));
  const icon = /雷/.test(b[3]) ? '⛈' : /雨/.test(b[3]) ? '🌧' : /陰/.test(b[3]) ? '☁' : /多雲/.test(b[3]) ? '⛅' : /晴/.test(b[3]) ? '☀' : '🌤';
  const hh = x => String(new Date(new Date(x).getTime() + 8 * 3600e3).getUTCHours()).padStart(2, '0');
  return reply({ title: `${icon} ${n}｜${label}`, url: DASH,
    description: `**${b[3]}**\n💧 降雨機率 **${b[2]}%**${b[2] >= 70 ? '　☔ 記得帶傘' : ''}\n🌡 約 ${tp ?? '—'}°C\n（${hh(b[0])}–${hh(b[1])} 點這一段的預報）` +
                 (hz.length ? `\n\n⛈ 目前特報：${hz.join('、')}` : '') +
                 `\n\n🕒 預報更新：${d.updated || '—'}｜資料：中央氣象署｜[📊 即時介面](${DASH})` });
}

// ---- /狀態 ----
async function cmdStatus(env) {
  const r = await fetch(`https://api.github.com/repos/${REPO}/actions/runs?per_page=60`, {
    headers: { Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: 'application/vnd.github+json', 'User-Agent': 'video-helper-discord-bot' } });
  if (!r.ok) return reply({ description: `讀不到 GitHub 狀態（${r.status}）` }, 64);
  const runs = (await r.json()).workflow_runs;
  const BOTS = [['Exchange rates to Discord', '💱 匯率'], ['Ranking to Discord', '🏆 排行榜'], ['News to Discord', '📰 新聞'],
                ['Site change alarm', '🔔 網站檢查'], ['Weather to Discord', '🌦 天氣'], ['Dashboard snapshot to Discord', '📊 即時介面']];
  const ago = t => { const m = Math.round((Date.now() - new Date(t)) / 60000); return m < 60 ? `${m} 分鐘前` : m < 1440 ? `${Math.round(m / 60)} 小時前` : `${Math.round(m / 1440)} 天前`; };
  const lines = BOTS.map(([n, zh]) => {
    const x = runs.find(w => w.name === n && w.status === 'completed');
    if (!x) return `⚪ ${zh}：最近沒有紀錄`;
    return `${x.conclusion === 'success' ? '🟢' : '🔴'} ${zh}：${ago(x.updated_at)}${x.conclusion === 'success' ? '' : '（失敗）'}`;
  });
  return reply({ title: '🩺 機器人狀態', url: `https://github.com/${REPO}/actions`,
    description: lines.join('\n') + `\n\n🟢 上次成功｜🔴 上次失敗｜[📊 即時介面（含網站檢查）](${DASH})` });
}
const text = (s, status = 200) => new Response(s, { status, headers: { 'content-type': 'text/plain; charset=utf-8' } });
const json = o => new Response(JSON.stringify(o), { headers: { 'content-type': 'application/json' } });
const hex = h => new Uint8Array(h.match(/../g).map(b => parseInt(b, 16)));

// Discord 規定：每個請求都要驗證簽章，確定真的是 Discord 送來的
async function verify(publicKey, signature, message) {
  for (const algo of ['Ed25519', { name: 'NODE-ED25519', namedCurve: 'NODE-ED25519' }]) {
    try {
      const key = await crypto.subtle.importKey('raw', hex(publicKey), algo, false, ['verify']);
      return await crypto.subtle.verify(algo, key, hex(signature), new TextEncoder().encode(message));
    } catch (e) { /* 換另一種寫法再試 */ }
  }
  return false;
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);

    // 註冊 /快照 指令（設定好之後打開一次就好；重複打開也沒關係）
    if (req.method === 'GET' && url.pathname === '/register') {
      const r = await fetch(`https://discord.com/api/v10/applications/${env.DISCORD_APP_ID}/commands`, {
        method: 'PUT',
        headers: { Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(COMMANDS),
      });
      return text(r.ok ? '✅ 指令註冊成功！回 Discord 打 / 看看：快照、換匯、匯率、排行榜、狀態、天氣、回饋者' : `❌ 註冊失敗（${r.status}）：${await r.text()}`);
    }
    // 意見回饋（電腦版、腳本送來的；不是 Discord，不驗 Discord 簽章）
    if (url.pathname === '/feedback/pubkey' && req.method === 'GET') {
      if (!env.FEEDBACK_KV) return fbJson({ ok: false }, 503);
      return fbJson({ ok: true, pub: (await fbKeys(env)).pub });
    }
    if (url.pathname === '/feedback') {
      if (req.method !== 'POST') return fbJson({ ok: false, msg: '要用 POST' }, 405);
      try { return await handleFeedback(req, env); } catch (e) { console.log('feedback error', e && e.stack); return fbJson({ ok: false, msg: '出錯了，等一下再試' }, 500); }
    }
    if (req.method !== 'POST') return text('🤖 Video Helper 指令機器人：運作中');

    // 驗證是 Discord 送來的
    const body = await req.text();
    const ok = await verify(env.DISCORD_PUBLIC_KEY, req.headers.get('X-Signature-Ed25519') || '',
                            (req.headers.get('X-Signature-Timestamp') || '') + body);
    if (!ok) return text('簽章不對', 401);
    const i = JSON.parse(body);

    if (i.type === 1) return json({ type: 1 });          // Discord 確認網址有效（PING）

    if (i.type === 2 && i.data.name === 'snapshot') {
      // 叫 GitHub 截圖（在背景做，先回覆 Discord，不然 Discord 等超過 3 秒會說失敗）
      ctx.waitUntil(fetch(`https://api.github.com/repos/${REPO}/actions/workflows/dash.yml/dispatches`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: 'application/vnd.github+json',
                   'User-Agent': 'video-helper-discord-bot' },
        body: JSON.stringify({ ref: 'main', inputs: { interaction: i.token, app_id: i.application_id } }),
      }));
      return json({ type: 4, data: { content: '📸 截圖中，大約 1～2 分鐘，好了會直接換成圖片…' } });
    }
    try {
      if (i.type === 2 && i.data.name === 'exchange') return await cmdExchange(i);
      if (i.type === 2 && i.data.name === 'rate') return await cmdRate(i);
      if (i.type === 2 && i.data.name === 'rank') return await cmdRank();
      if (i.type === 2 && i.data.name === 'arena') return await cmdArena();
      if (i.type === 2 && i.data.name === 'invoice') return await cmdInvoice(i);
      if (i.type === 2 && i.data.name === 'oil') return await cmdOil();
      if (i.type === 2 && i.data.name === 'status') return await cmdStatus(env);
      if (i.type === 2 && i.data.name === 'weather') return await cmdWeather(i);
      if (i.type === 2 && i.data.name === 'feedback-role') return await cmdFeedbackRole(i, env);
    } catch (e) {
      return json({ type: 4, data: { content: `出錯了：${e.message}`, flags: 64 } });
    }
    return json({ type: 4, data: { content: '不認得這個指令', flags: 64 } });
  },
};
