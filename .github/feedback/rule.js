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
