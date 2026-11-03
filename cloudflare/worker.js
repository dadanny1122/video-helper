// Video Helper 的 Discord 指令機器人（放在 Cloudflare Workers，免費）
// 在 Discord 打 /快照 → 這裡收到 → 叫 GitHub 截一張「即時介面」→ 圖片會出現在你打指令的地方
//
// 要在 Cloudflare 設定的變數（Settings → Variables and Secrets，全部選 Secret）：
//   DISCORD_PUBLIC_KEY  Discord 開發者網站 → General Information → Public Key
//   DISCORD_APP_ID      Discord 開發者網站 → General Information → Application ID
//   DISCORD_BOT_TOKEN   Discord 開發者網站 → Bot → Reset Token（只在註冊指令時用）
//   GITHUB_TOKEN        GitHub 的 fine-grained token（只給 video-helper 的 Actions 讀寫）
//
// 第一次設定好之後，用瀏覽器打開「你的 Worker 網址/register」註冊 /快照 指令

const REPO = 'dadanny1122/video-helper';
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
        body: JSON.stringify([{
          name: 'snapshot', name_localizations: { 'zh-TW': '快照' }, type: 1,
          description: 'Take a snapshot of the live dashboard now',
          description_localizations: { 'zh-TW': '馬上截一張「即時介面」的圖' },
        }]),
      });
      return text(r.ok ? '✅ 指令註冊成功！回 Discord 打 /快照 試試看' : `❌ 註冊失敗（${r.status}）：${await r.text()}`);
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
    return json({ type: 4, data: { content: '不認得這個指令', flags: 64 } });
  },
};
