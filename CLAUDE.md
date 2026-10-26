# Video Helper — 給 AI 助手看的專案說明

新對話請先讀完這份，再開始工作。使用者是台灣的研究生，用**繁體中文**、喜歡**白話、簡短、一步一步**的說明。

## 這個專案是什麼

讓使用者和朋友看成人影片網站時「安全、乾淨、穩定」：擋廣告／彈窗／危險下載、預估載入時間、在支援的網站一鍵下載。
**全部在使用者自己的電腦上跑，沒有伺服器。** GitHub 只負責打包和 Discord 通知。

| 檔案 | 是什麼 |
|---|---|
| `video-helper.user.js` | 瀏覽器腳本（暴力猴／Violentmonkey）。約 2,200 行，**單一檔案**（Greasy Fork 規定） |
| `video_helper_app.py` | 電腦版（Tkinter + yt-dlp + 迷你 ffmpeg），GitHub Actions 打包成 `VideoHelper.exe` |
| `docs/manual/index.html` | 說明書（GitHub Pages：https://dadanny1122.github.io/video-helper/manual/），改了會自動同步到 Discord |
| `docs/practice/` | 學 HLS 用的練習頁 |
| `CHANGELOG.md` | **電腦版**的更新紀錄（發布 EXE 時當作說明） |
| `.github/ci_smoke.py` | 電腦版的自動測試（在 Windows 上跑） |
| `.github/ffmpeg/build.sh` | 編迷你 ffmpeg（LGPL，只會接檔、換包裝） |

## 腳本的架構（`video-helper.user.js`）

- `AD_DOMAINS`：已知的廣告網路（所有網站共用）
- `SITES`：每個網站一筆 → 名稱、`match`（網域）、`features`（開哪些功能）、專屬規則、找影片的函式
  - `ALL`＝全部功能含下載；`ADBLOCK_ONLY`＝只擋廣告（沒有下載）
  - 專屬規則（`rules`）：`hide`（藏元素）、`blockInlineScript`、`blockTimeout`、`blockListeners`、`renameKeys`、`setGlobals`、`pruneCallArgs`（網站呼叫播放器函式時刪掉設定裡的廣告清單，例：YouJizz 的 fluidPlayer vastOptions）；參考 uBlock Origin 規則庫用自己的程式重寫
  - `adPath`：網站自己網域上的廣告路徑；`ownDomains`：網站自己的影片伺服器（不要封鎖）
- `MODULES`：功能本體（security、saveData、hideAds、autoSkip、videoStatus、estimator、listLabels、download）
- 找影片：`findPhSources`（Pornhub：flashvars）、`findXvSources`（XVideos／XNXX：setVideoHLS）、`findAyloSources`（YouPorn／RedTube：mediaDefinitions）、`findYjSources`（YouJizz：dataEncodings）、`findSbSources`（SpankBang：stream_data，讀不到 → 交給電腦版）、Tube8 用 `findAyloSources`、`findXhSources`（xHamster：window.initials；網址被加密成 16 進位字，`xhDecodeHex` 解開＝第 1 位元組選算法 1～7、第 2～5 位元組是 seed、其餘 XOR，跟 yt-dlp 同法；網站換新算法時解不出來 → 退回「交給電腦版」）
- 「🖥 送到電腦版」：`sendToDesktop` → 電腦版 `127.0.0.1:47321～47325`，要先 6 位數配對；會一起送影片真正的位址（網站會擋程式開網頁，例如 Pornhub 回 410）

## 支援的網站

- **全部功能（含下載）**：Pornhub、XVideos、XNXX、YouPorn、RedTube、xHamster、Tube8、YouJizz、SpankBang（標準：使用者上傳為主的平台；片商轉載站、片商自己的網站不開下載）
- **只擋廣告**：MissAV、Jable、Hanime1，以及 ThePornDude 排行前 20 的其他影片網站（Eporner、HQporner、Beeg、YourPorn、XMoviesForYou、PornTrex、PornOne、3Movs、PornDig、CumLouder、TXXX、PornDoe）

## 一定要遵守的規則

- **盜版轉載站、付費平台不做下載**（MissAV、Jable、Hanime1、FC2、OnlyFans、myfans、Brazzers 官網等），只擋廣告。使用者練習下載用 `docs/practice/`
- **不做**來源不明、可能有「外流」「偷拍」等無法確認當事人同意的網站
- 新聞、排行榜只貼**標題／名稱和連結**，不轉貼全文
- 改了腳本：`@version` 一定要 +1（暴力猴、Greasy Fork、Discord 通知都看它）
- 改了電腦版：`VERSION` +1，並在 `CHANGELOG.md` 最上面寫一段（GitHub 會自動打包發布）
- **commit 訊息用中文白話寫**（會直接貼到 Discord #進度 給朋友看）
- 說明書裡的分頁名稱要跟電腦版一致：① 下載影片 ② 下載紀錄 ③ 設定 ④ 安裝擋廣告；其他設定在「⚙ 進階設定…」
- 推之前先 `git pull --rebase`（GitHub 機器人會自己 commit 狀態檔）

## 自動化（GitHub Actions → Discord）

| Workflow | 什麼時候 | 做什麼 | Secret |
|---|---|---|---|
| `build.yml` | 改了電腦版 | 編 ffmpeg、測試、打包 EXE、發布 Release、通知 #進度 | `DISCORD_WEBHOOK` |
| `userscript.yml` | 腳本 `@version` 改了 | 通知 #進度 | `DISCORD_WEBHOOK` |
| `manual.yml` | 說明書改了 | 更新 #使用手冊（修改原本的訊息） | `DISCORD_MANUAL_WEBHOOK` |
| `site-check.yml` | 每天 9:17 | 檢查網站改版，狀態變了才通知 #進度 | `DISCORD_WEBHOOK` |
| `news.yml` | 每 3 小時（台灣 2:23、5:23…23:23） | 中英日新聞 → 新聞頻道（只收安全名單裡的媒體、確認原文日期、標題附中文翻譯）；除了各網站，也追「成人網站動態」（產業法規、封鎖）和英國 Online Safety Act；勾 diagnose 看被擋掉的來源 | `DISCORD_NEWS_WEBHOOK` |
| `fx.yml` | 平日台灣 9:07～16:37 每 30 分鐘 | 臺灣銀行牌告匯率（官方 CSV）→ 匯率頻道（修改同一則）：USD、EUR、JPY、CNY、GBP、HKD、CHF、AUD、KRW 的現金／即期買賣價、跟前一營業日比的漲跌；漲跌超過 0.5% 另發提醒（每種一天一次）；附 TradingView 走勢圖連結（不抓 TradingView 資料：沒免費 API、違反條款）；最上面有來源、台銀掛牌時間、各貨幣「今日各時段牌價」連結；每種貨幣有「1 萬台幣換現鈔得到多少（比 30 天平均）」和「30 天內便宜／貴」🟢🟡🔴（台銀 L3M 歷史 CSV，一天抓一次存 state）；16:30 後第一次跑發「今日匯率總結」＋ matplotlib 30 天走勢圖（勾 summary 可手動發）；總結含「未來 5 個營業日預報」（USD/EUR/JPY/CNY）：A＝30 天波動度推 68% 範圍、B＝類比預報（最近 10 天形狀比對 FRED 過去 3 年，取最像 20 段不重疊，看之後 5 天升貶）；預報存 state[fc]，5 個營業日後 `score()` 對答案並公開命中率（使用者的想法：forecast 不是 predict、歷史可能重演也可能不會 → 用準度記錄驗證）；總結格式＝投資人看的數據表格（程式碼區塊對齊）：行情 Mid/1D%/30D Lo/Hi/Pctl/年化 Vol%、5 日 68%/95% 區間、類比 Up/N/Mean/Med/P25/P75，解讀另放最後（使用者：數據和分析要分開，不要攏統的文字）；使用者要求只做自己的數據整理、不加新聞、不寫買賣建議 | `DISCORD_FX_WEBHOOK` |
| `announce.yml` | 改了 `.github/announce.md` | 把更新總結貼到 #進度（使用者說「上傳 DC 更新結果」時：改寫這個檔、推上去） | `DISCORD_WEBHOOK` |
| `rank.yml` | 每天 20:00 整（使用者要求固定 8 點；cron 18:31、19:13、19:47 提早啟動，程式 sleep 到 20:00 才貼；`last_day` 只有 8 點那次（schedule 或手動勾 wait）才記，今天貼過就跳過；排程被 GitHub 跳過時手動 Run workflow 勾 wait 補救；concurrency 一次只跑一個、checkout ref: main 拿最新狀態） | ThePornDude 前 20 名 → 排行榜頻道（修改同一則），標「可下載／只擋廣告」；每天另發一則跟昨天比的名次變動；並排 Tranco 全球流量排名（`tranco()` 下載 top-1m，`DOMAINS` 對照名稱→網域）、列出兩邊差最多的；附來源連結＋Similarweb 連結；勾 preview 只印在 annotations 不發 Discord；手動 Run workflow 勾 repost 會刪舊訊息重貼 | `DISCORD_RANK_WEBHOOK` |

其他：腳本也發布在 Greasy Fork（https://greasyfork.org/zh-TW/scripts/601901-video-helper），從 GitHub 自動同步。

## 常見任務

**加一個「只擋廣告」的網站**（3 個地方）
1. 檔頭加 `// @match        *://*.網域/*`
2. `SITES` 裡加 `{ name: '名稱', match: /(^|\.)網域$/, features: ADBLOCK_ONLY, limits: TUBE_LIMITS },`
3. `@version` +1；`README.md`、`docs/manual/index.html` 的支援清單補上；`.github/rank_feed.py` 的 `ADBLOCK` 也補上

**有廣告沒擋到**
請使用者：F12 →「Console」→ 把紀錄貼上（看得到廣告商網域，例如 XVideos 的 `pemsrv.com`），或盾牌 →「🔍 檢查」截圖。
修法：網域加進 `AD_DOMAINS`，或在該網站的 `rules` 加專屬規則。

**網站改版、抓不到影片**
看 `site-check.yml` 的結果（Actions 頁面的 annotations），對照該網站的找影片函式修。

## 測試方法

- 腳本：`node --check`；用 Playwright（Chromium 在 `/opt/pw-browsers/`）載入假網頁 + 假的 GM_* 函式測
- 電腦版：`xvfb-run python3.12 .github/ci_smoke.py`（要 `ffmpeg_bin/` 裡有 ffmpeg；系統匣那段只能在 Windows 過）
- 開發環境連不到成人網站、Google 新聞 → 要看真實網頁就寫成 GitHub Actions 的測試模式，用 `::notice` 印結果

## 還沒做的事

- xHamster：v3.12.0 真網站確認可選畫質（瀏覽器自己解密）；「交給電腦版」也可下載
- xHamster 影片前廣告會閃約 0.7 秒才被跳過（使用者說先不處理；要擋再請他貼 Console 紀錄）
- XNXX 首頁 2026-10 改成分類頁（沒影片連結），site_check 改去 /best、/hits 找；site-check.yml 可用 probe=xnxx 診斷；probe=tube8 會看 Tube8／SpankBang／YouJizz 影片頁結構
- YouPorn／RedTube 影片前廣告：v3.13.0 加了跟 Pornhub 一樣的 adPath /_xa/(ads|fla) + renameKeys adRollGlobalConfig，真網站確認沒廣告
- Tube8、YouJizz、SpankBang 下載 v3.14.0／電腦版 v1.17.0 加入，使用者說下載都沒問題
- YouJizz 全螢幕時推薦影片的預覽會閃一下（網站自己的功能，使用者說不用理）
- 新加的「只擋廣告」網站都還沒有專屬規則（有人回報再加）
- 手機版（Android 的 Firefox／Kiwi 可以裝暴力猴；iPhone 要另外的 App）
