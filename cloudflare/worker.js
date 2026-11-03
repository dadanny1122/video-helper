// Video Helper 的 Discord 指令機器人（放在 Cloudflare Workers，免費）
// 指令：
//   /快照   → 叫 GitHub 截一張「即時介面」（1～2 分鐘）
//   /換匯   → 用台銀牌價算「數量 × 價格」（1 秒內，直接讀 GitHub 上的 rates.json）
//   /匯率   → 某個貨幣的現價、漲跌、30 天位置
//   /排行榜 → 前 10 名＋可下載的網站
//   /狀態   → 各個機器人最後一次跑的時間、成功或失敗
//
// 要在 Cloudflare 設定的變數（Settings → Variables and Secrets，全部選 Secret）：
//   DISCORD_PUBLIC_KEY  Discord 開發者網站 → General Information → Public Key
//   DISCORD_APP_ID      Discord 開發者網站 → General Information → Application ID
//   DISCORD_BOT_TOKEN   Discord 開發者網站 → Bot → Reset Token（只在註冊指令時用）
//   GITHUB_TOKEN        GitHub 的 fine-grained token（只給 video-helper 的 Actions 讀寫）
//
// 第一次設定好、或改了指令之後，用瀏覽器打開「你的 Worker 網址/register」註冊指令

const REPO = 'dadanny1122/video-helper';
const RAW = `https://raw.githubusercontent.com/${REPO}/main/`;
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

// 指令清單（英文名字給程式用，zh-TW 是 Discord 上看到的名字）
const COMMANDS = [
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

// ---- /狀態 ----
async function cmdStatus(env) {
  const r = await fetch(`https://api.github.com/repos/${REPO}/actions/runs?per_page=60`, {
    headers: { Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: 'application/vnd.github+json', 'User-Agent': 'video-helper-discord-bot' } });
  if (!r.ok) return reply({ description: `讀不到 GitHub 狀態（${r.status}）` }, 64);
  const runs = (await r.json()).workflow_runs;
  const BOTS = [['Exchange rates to Discord', '💱 匯率'], ['Ranking to Discord', '🏆 排行榜'], ['News to Discord', '📰 新聞'],
                ['Site change alarm', '🔔 網站檢查'], ['Dashboard snapshot to Discord', '📊 即時介面']];
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
      return text(r.ok ? '✅ 指令註冊成功！回 Discord 打 / 看看：快照、換匯、匯率、排行榜、狀態' : `❌ 註冊失敗（${r.status}）：${await r.text()}`);
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
      if (i.type === 2 && i.data.name === 'status') return await cmdStatus(env);
    } catch (e) {
      return json({ type: 4, data: { content: `出錯了：${e.message}`, flags: 64 } });
    }
    return json({ type: 4, data: { content: '不認得這個指令', flags: 64 } });
  },
};
