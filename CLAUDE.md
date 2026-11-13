# Video Helper — 給 AI 助手看的專案說明

新對話請先讀完這份，再開始工作。使用者是台灣的研究生，用**繁體中文**、喜歡**白話、簡短、一步一步**的說明。

## 這個專案是什麼

讓使用者和朋友看成人影片網站時「安全、乾淨、穩定」：擋廣告／彈窗／危險下載、預估載入時間、在支援的網站一鍵下載。
**全部在使用者自己的電腦上跑，沒有伺服器。** GitHub 只負責打包和 Discord 通知。

| 檔案 | 是什麼 |
|---|---|
| `video-helper.user.js` | 瀏覽器腳本（暴力猴／Violentmonkey）。約 2,200 行，**單一檔案**（Greasy Fork 規定） |
| `video_helper_app.py` | 電腦版（Tkinter + yt-dlp + 迷你 ffmpeg），GitHub Actions 打包成 `VideoHelper.exe`；v1.18.0 加「📝 AI 字幕」（② 下載紀錄的按鈕／右鍵）：`make_subtitles()`＝迷你 ffmpeg 轉 16kHz wav（build.sh 加了 pcm_s16le、wav）→ whisper.cpp（第一次用從 GitHub releases/latest 下載 whisper-bin-x64.zip 到設定資料夾/whisper；模型從 Hugging Face 下載，快＝small-q5_1、準＝large-v3-turbo-q5_0，指紋寫死在 `MODEL_SHA256`；whisper zip 比對 GitHub asset digest；AI 金鑰用 DPAPI 加密（`SECRET_KEYS`））→ 原文 `影片.語言.srt`；有 ai_key 就 `ai_translate()` 只送字幕文字翻成繁中 `影片.srt`；build.yml 有真的 whisper 測試（Windows 語音念英文、tiny 模型，失敗就不發布）；v1.19.0「⚙ 進階設定 → 🔒 安全」分頁（`build_security_tab`／`refresh_security`）：配對清單（`bridge_meta`：瀏覽器名、配對／最後使用時間、次數；`Bridge.unpair(h)` 單獨取消）、AI 使用（`AI_USAGE_HOOK`→`settings["ai_usage"]` 每月次數＋token）、連線紀錄（`_install_net_log` 在 socket.getaddrinfo 記 `NET_SEEN`，不存檔）、日誌（`log_keep_days`、`clean_old_logs`、清空）、`component_report()` 版本＋SHA-256；v1.20.0 智慧下載：`PAUSED` 狀態＋`toggle_pause()`（`stop_mode="pause"`→`keep_partials`→`run_download`／`parallel_download` 丟 `Paused` 不刪半成品；`parallel_download` 用 `dest.parts.json`（total、段數）判斷能不能接著下載、`.partN` 以 ab 續寫；yt-dlp `continuedl`；task["partials"] 移除或結束程式時刪）、`THROTTLE`（全域 token bucket，`rate_limit_mbps`，yt-dlp `ratelimit`）、`video_key()`／`find_downloaded()`（`VIDEO_ID_RULES` 各站影片編號；`skip_downloaded` 批次／瀏覽器自動跳過，手動下載會問）；v1.21.0 AI 字幕中心：下載紀錄 `selectmode="extended"` 多選 → `subtitle_dialog` 收集多部、`_subtitle_worker` 一部接一部（失敗的記下來繼續）、`has_subtitles()` 跳過已有字幕；`make_subtitles(fmts=, dual=)`：`SUB_FORMATS`（srt／vtt／兩種，`write_vtt`、`write_subs`）、`bilingual()` 寫 `影片.雙語.srt`（上中文下原文）；v1.22.0 自動化核心（使用者貼的規劃文件，先做「錯誤診斷＋狀態機＋自適應重試」；工作流程引擎／規則簽章判定價值低先不做；能力協商使用者要做 → v1.23.0）：`classify_error()`（`ERROR_RULES`：disk_full／429／403／404／invalid_url／5xx／timeout／network／ffmpeg，unknown 只試 1 次）、`retry_wait_seconds()`（Retry-After 優先，否則 base×2^(n-1)×0.8~1.2 抖動、cap 900s）、狀態 `RETRY_WAIT`／`NEEDS_ACTION`（retry_failed 兩種都收、due_retries 由 poll 每秒檢查）、`NET`＝NetHealth（timeout/network/5xx 連 3 次失敗 → pump 暫緩 60s×2^n）、disk_full → queue_paused＋任務留 WAITING、`decision_log()`→decisions.jsonl（只記網站名，400KB 砍半）；ci_smoke：404→需要處理、429 照 Retry-After 自動重試成功、磁碟滿→暫停；headless 測試另見對話（classify 10 案例）；v1.23.0 能力協商：`/ping` 回 `version`、`api`（`BRIDGE_API`）、`features`（enqueue／media／status／classify）、`sites`（`BRIDGE_HOSTS`）→ 腳本 v3.20.0 `desktopCaps`／`desktopLacks(feature, host)`（舊電腦版沒 features 就照 `LEGACY_FEATURES`／`LEGACY_SITES` 版本推算）太舊就說「請更新」附 releases 連結；api 比腳本新 → 請腳本檢查更新；v1.21.1 防幻聽（使用者回報一直出現同一個詞「小小」）：`run_whisper` 加 `-mc 0 -sns` 和 `--vad --vad-model`（Silero `ggml-silero-v5.1.2.bin`，`MODEL_SOURCES`／`model_path()`，指紋寫死），不認得就退回基本模式（`WHISPER_FALLBACKS`，build.yml 檢查要是 0）；`clean_hallucinations()`：「小小小小」縮成兩個、片尾字幕型句子（`HALLU_PHRASES`／`HALLU_EXACT`）刪、連續同句合併、某句佔 4 成以上全刪 |
| `docs/manual/index.html` | 說明書（GitHub Pages：https://dadanny1122.github.io/video-helper/manual/），改了會自動同步到 Discord |
| `docs/fx/index.html` | 匯率頻道說明書（https://dadanny1122.github.io/video-helper/fx/），匯率訊息都附這個連結；改了匯率的欄位要一起改 |
| `docs/fx/calc.html` | 換匯計算機（https://dadanny1122.github.io/video-helper/fx/calc.html）：讀 `docs/fx/rates.json`（fx 每次跑時 `export_rates()` 寫入並 commit，preview 模式也會）；可選「今天任一分鐘」的價格（`ticks()` 抓台銀「今日各時段牌價」每次掛牌，約每 2～3 分鐘一次、精確到秒；選的時間用最後一筆 ≤ 那一分鐘的掛牌；台銀頁面 CORS 只開給自己，所以要由 Actions 抓）；鬧鐘每 10 分鐘以 `inputs.export=true` 只更新 rates.json、不發 Discord；三種模式：買外幣（數量×銀行賣出）、用台幣換（台幣÷賣出）、賣外幣（數量×銀行買入），現金／即期，附算式、30 天平均比較、現金 vs 即期差價 |
| `docs/dash/index.html` | 「即時介面」（使用者要的名字；logo＝使用者提供的吉祥物圖 docs/dash/logo.png，含 Pornhub 標誌元素，已提醒商標風險）網頁看板（https://dadanny1122.github.io/video-helper/dash/）六格（2×3）：天氣（讀 .github/weather_state.json 的 dash：6 地點現在 3 小時降雨、今天最可能下雨時段、氣溫）、支援網站檢查（site_status.json）、匯率（讀 docs/fx/rates.json：prev_mid、pctl30）、排行榜（讀 .github/rank_state.json：rows、tranco、updated）、新聞（讀 .github/news_seen.json 的 recent，最近貼出 15 則）、系統狀態（GitHub API 各 workflow 最後一次執行，免登入每小時 60 次 → 5 分鐘問一次；site_status.json）。都從 raw.githubusercontent.com 讀，每分鐘刷新 |
| `docs/practice/` | 學 HLS 用的練習頁 |
| `CHANGELOG.md` | **電腦版**的更新紀錄（發布 EXE 時當作說明） |
| `.github/ci_smoke.py` | 電腦版的自動測試（在 Windows 上跑） |
| `SECURITY.md` | 安全說明：資料外傳清單、本機服務防護、第三方元件指紋、自動安全測試；**改了會連網的功能要同步更新它和 `.github/net_test.py` 的 ALLOWED** |
| `.github/net_test.py` | 網路流量測試（build.yml，Windows）：socket 層記錄所有連線，跑開視窗 15 秒、檢查更新、廣告清單、whisper、tiny 模型、AI（假金鑰），連到 ALLOWED 以外就失敗、擋發布 |
| `.github/security_test.js` | 腳本隱私測試（security.yml，改腳本自動跑）：每個 @match／@include 網站開假網頁，監視 Cookie、密碼欄位、非 vh_ 的 localStorage、IndexedDB、navigator.credentials、對外連線；也核對 mux.js 的 @require #sha256 跟 jsDelivr 一樣 |
| `.github/attack_test.js`＋`attack_bridge.py` | 配對密碼攻擊模擬（security.yml）：attack_bridge.py 只啟動真的 Bridge（印 port、配對碼，收到的 enqueue 寫檔）；Playwright 假 Pornhub 頁（flashvars）＋假暴力猴（GM_* 包在閉包、Map 存值、跨界資料用 NONCE XOR 加密，模擬擴充功能邊界）；網站埋陷阱（Object.prototype setter、換 JSON.parse/stringify/prompt/String.replace/Set.has）→ 腳本真配對送 1 筆 → 網站翻 storage/cookie/DOM/window/陷阱找密碼、三種方式冒充 → 要找不到且電腦版只收 1 筆。腳本處理密碼一律用 `SAFE`（啟動時存的原版 JSON/prompt）、headers 一次寫成物件、`isSecret()` 不用 Set |
| `requirements.txt`／`.in` | 打包套件寫死版本＋sha256（pip-compile --generate-hashes；在 Linux 產生，Windows 專用的 pefile!=2024.8.26、pywin32-ctypes 手動列在 .in）；build.yml：pip install --require-hashes → pip-audit（有 CVE 就不發布）→ cyclonedx SBOM → actions/attest-build-provenance＋attest-sbom（Sigstore 簽章）→ Release 附 sbom.cdx.json、audit.md；`.github/dependabot.yml` 每週 pip、每月 actions。改套件後要重產 requirements.txt |
| `.github/ffmpeg/build.sh` | 編迷你 ffmpeg（LGPL，只會接檔、換包裝） |

## 腳本的架構（`video-helper.user.js`）

- `AD_DOMAINS`：已知的廣告網路（所有網站共用）
- `SITES`：每個網站一筆 → 名稱、`match`（網域）、`features`（開哪些功能）、專屬規則、找影片的函式
  - `ALL`＝全部功能含下載；`ADBLOCK_ONLY`＝只擋廣告（沒有下載）
  - 專屬規則（`rules`）：`hide`（藏元素）、`blockInlineScript`、`blockTimeout`、`blockListeners`、`renameKeys`、`setGlobals`、`pruneCallArgs`（網站呼叫播放器函式時刪掉設定裡的廣告清單，例：YouJizz 的 fluidPlayer vastOptions）；參考 uBlock Origin 規則庫用自己的程式重寫
  - `adPath`：網站自己網域上的廣告路徑；`ownDomains`：網站自己的影片伺服器（不要封鎖）
- `MODULES`：功能本體（security、saveData、hideAds、autoSkip、videoStatus、estimator、listLabels、download、bossKey、disguise）；`defaultOff: true` 的預設關閉（`isOn`）；early 模組每個都包 try（一個出錯不拖垮安全防護）
  - v3.19.0 `bossKey`：Esc 700ms 內按兩下（全螢幕時 fullscreenchange 退出也算一下，因為那下 Esc 被瀏覽器吃掉） → 靜音暫停、`location.replace(store bossUrl，預設 Google)`；先設 `shared.bossUrl`，security 的 navigate 攔截看到就放行；early＝iframe 裡也有效（v3.19.2：iframe 裡不自己跳（沙盒播放器會被擋、改跳自己 → Google 不給嵌 → 播放器變黑），改 postMessage {vhBossKey:1} 給 top，top 收到才 location.replace）；v3.19.3：使用者回報 Pornhub 播放中按兩下 → 影片變黑但沒跳走（推測：Esc＝瀏覽器「停止載入」，取消了同一下開始的跳轉並停掉影片下載）→ 觸發那下 preventDefault、先蓋全白遮罩、setTimeout 0 才 location.replace、1 秒後還在就再 location.replace 一次（v3.19.4：原本用 href 會在上一頁留下影片；瀏覽器不准刪更早的紀錄，建議無痕視窗）；gmMenu 可改網址。`disguise`：標題「未命名文件」＋SVG 文件圖示，MutationObserver 觀察 `document`（document-start 時 documentElement 還是 null）
- 找影片：`findPhSources`（Pornhub：flashvars）、`findXvSources`（XVideos／XNXX：setVideoHLS）、`findAyloSources`（YouPorn／RedTube：mediaDefinitions）、`findYjSources`（YouJizz：dataEncodings）、`findSbSources`（SpankBang：stream_data，讀不到 → 交給電腦版）、Tube8 用 `findAyloSources`、`findXhSources`（xHamster：window.initials；網址被加密成 16 進位字，`xhDecodeHex` 解開＝第 1 位元組選算法 1～7、第 2～5 位元組是 seed、其餘 XOR，跟 yt-dlp 同法；網站換新算法時解不出來 → 退回「交給電腦版」）
- **相容層**（v3.17.0）：不要直接呼叫 GM_xxx，一律用 `gmXhr`、`gmDownload`、`gmClip`、`gmMenu`、`gmGetValue`、`gmSetValue`、`gmInfo`（先用暴力猴的 GM_xxx → 沒有就用新式 GM.xxx → 再沒有就用 fetch／下載連結／navigator.clipboard 頂替），讓 iPhone Safari 的 Userscripts 等其他管理器也能跑
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
| `fx.yml` | 平日台灣 9:00～17:00 每小時整點（cron :50 啟動、`wait_for_hour()` 等到整點；`state[slot]` 同一小時只發一次；勾 wait 可手動補） | **兩個頻道**（使用者要求）：#匯率＝每小時發一則新的價格表；#匯率分析（`DISCORD_FX_ANALYSIS_WEBHOOK`，沒設定就先發在 #匯率）＝每小時小方向表格（Now/Day%/1H%/Lo/Hi/1σ%/5D）＋⚡提醒、17:00 大方向總結。臺灣銀行牌告匯率（官方 CSV）：USD、EUR、JPY、CNY、GBP、HKD、CHF、AUD、KRW 的現金／即期買賣價、跟前一營業日比的漲跌；漲跌超過 0.5% 另發提醒（每種一天一次）；附 TradingView 走勢圖連結（不抓 TradingView 資料：沒免費 API、違反條款）；最上面有來源、台銀掛牌時間、各貨幣「今日各時段牌價」連結；每種貨幣有「1 萬台幣換現鈔得到多少（比 30 天平均）」和「30 天內便宜／貴」🟢🟡🔴（台銀 L3M 歷史 CSV，一天抓一次存 state）；17:00 那次發「今日匯率總結」＋ matplotlib 30 天走勢圖（勾 summary 可手動發）；總結含「未來 5 個營業日預報」（USD/EUR/JPY/CNY）：A＝範圍（2026-11-08 起用 `hist_range()` 歷史分位：FRED 過去 250 天每段 5 日漲跌的 16～84%／2.5～97.5%；讀不到 FRED 才退回 `vol_range()` 30 天波動度，標題會寫）、B＝類比預報（最近 10 天形狀比對 FRED 過去 3 年，取最像 20 段不重疊，看之後 5 天升貶）；預報存 state[fc]，5 個營業日後 `score()` 對答案並公開命中率（使用者的想法：forecast 不是 predict、歷史可能重演也可能不會 → 用準度記錄驗證）；⚡ 提醒＝盤中 |Day%|≥1σ、跑出 5 日 68% 區間、比前一營業日超過 0.5%（每種每天一次，`state[intra]`、`state[alerted]`）；總結格式＝投資人看的數據表格（程式碼區塊對齊）：行情 Mid/1D%/30D Lo/Hi/Pctl/年化 Vol%、5 日 68%/95% 區間、類比 Up/N/Mean/Med/P25/P75，解讀另放最後（使用者：數據和分析要分開，不要攏統的文字）；使用者要求只做自己的數據整理、不加新聞、不寫買賣建議 | `DISCORD_FX_WEBHOOK`、`DISCORD_FX_ANALYSIS_WEBHOOK` |
| `fx-arena.yml` | 每週六台灣約 10:00（GitHub cron，遲到沒關係） | `fx_backtest.py`「匯率預報擂台」→ #匯率分析：FRED 6 年資料、前 500 天當課本、每 5 天考一次（答案不重疊、只用考試日以前的資料）；方向 5 種（永遠猜漲＝基準線、動能、均值回歸、類比、機器學習＝自寫邏輯迴歸）比 Hit%、Wilson 95%CI，判讀用 99%（多重比較）；範圍 3 種（30天波動度、EWMA、歷史分位）比 Cover%（目標 68%）、Width%；結果存 docs/fx/arena.json（即時介面匯率格子最下面一行、Discord `/擂台`（arena）讀它）；勾 preview 只印；SYNTH=true 用隨機漫步測程式。2026-11-06 第一次真實結果：方向全部跟丟硬幣分不出來；30 天波動度太寬（73%）、歷史分位最準（69.6%）→ 使用者同意，2026-11-08 正式預報改用歷史分位 | `DISCORD_FX_ANALYSIS_WEBHOOK` |
| `fx-progress.yml` | 改了 `.github/fx_progress.md`／`fx_manual.md` | 匯率的進度、說明書摘要貼到 #匯率進度（使用者說「匯率進度上傳 DC」時改寫這兩個檔） | `DISCORD_FX_PROGRESS_WEBHOOK` |
| `ios-guide.yml` | 改了 `.github/ios_guide.md` | iPhone 安裝教學貼到 #video_ios（iPhone 從 raw GitHub 網址裝，Greasy Fork 成人腳本要登入才看得到） | `DISCORD_IOS_WEBHOOK` |
| `dash.yml` | 匯率／排行榜／新聞跑完（workflow_run）或手動 | `dash_snapshot.py` 用 Playwright 截「即時介面」深色版 → 修改 #看板 同一則訊息（`.github/dash_state.json` 存 message_id）；Discord 指令 `/快照`（命令名 snapshot、zh-TW 顯示快照）：`cloudflare/worker.js` 放在使用者的 Cloudflare Workers，驗 Discord 簽章 → dispatch dash.yml（inputs.interaction／app_id）→ `reply_command()` 把「截圖中…」換成圖；其他指令（Worker 直接讀 raw GitHub 的 JSON，1 秒內回覆，不叫 GitHub Actions）：`/換匯`（exchange：貨幣、數量、方式 buy/twd/sell、現金或即期）、`/匯率`（rate）、`/排行榜`（rank）、`/狀態`（status，用 Worker 的 GITHUB_TOKEN 讀 Actions runs）；指令清單在 `COMMANDS`，改了要請使用者重貼 Worker 程式並開 /register | `DISCORD_DASH_WEBHOOK` |
| `weather.yml` | 鬧鐘：每天 7:00 mode=daily、每小時 mode=check | `weather_feed.py` → #每日氣象。使用者要「大小方向」：🗓 大方向（daily）＝`SPOTS` 6 個地點（大園、青埔＝中壢區、土城、台北101＝信義區、竹科＝新竹市東區、竹北）今天每 3 小時降雨機率時間表＋最可能下雨時段（氣象署 F-D0047-xxx 鄉鎮 2 天預報，元素「3小時降雨機率」「天氣現象」「溫度」）＋全台 22 縣市今明總覽（F-C0032-001）；⚡ 小方向（check）＝接下來 3 小時（now+1h 所在區塊）從 <70% 變 ≥70% 才通知、下過雨後 ≤30% 通知雨停（`weather_state.json` 的 spots 記狀態）。⛈ 特報＝W-C0033-001，只看 `DS_COUNTY` 5 縣市，名稱含「雨／颱風」的新特報才通知、解除也通知（state[warn]），其他特報（如陸上強風）只顯示在即時介面；state[dash] 另存 blocks（未來 2 天每 3 小時）、temps（每小時）、warnings，給即時介面「🔎 查某個時間會不會下雨」和 Discord `/天氣`（weather：地點、幾點、哪天）用。氣象署沒有逐小時降雨機率，最細 3 小時；勾 preview 只印、mode=probe 看特報格式。🌏 mode=quake（鬧鐘每 5 分鐘，`*/5 * * * *`，body `{"ref":"main","inputs":{"mode":"quake"}}`）：`quake_main()` 讀 E-A0015-001（顯著有感，全部通知）＋E-A0016-001（小區域，`MY_COUNTIES` 震度 ≥3 才通知），`quake_seen` 記已發的（第一次跑只記不發），附震度圖、你那 5 縣市震度；發到 `DISCORD_QUAKE_WEBHOOK`（沒設就 #每日氣象）；state.dash.quake 給即時介面（check/daily 寫 dash 時要保留）。🌫 空氣品質：`aqi()` 讀環境部 aqx_p_432（要 `MOENV_API_KEY`，沒設就跳過），`AQI_SITES` 地點→最近測站（101→松山、竹北→湖口）；daily 附在時間表下面、check 時 ≥151 提醒／≤100 解除（state.aqi_alert）；state.dash.aqi 給即時介面 | `CWA_API_KEY`、`DISCORD_WEATHER_WEBHOOK`、（選用）`DISCORD_QUAKE_WEBHOOK`、`MOENV_API_KEY` |
| `life.yml` | GitHub cron 台灣 07:23、14:17（遲到沒關係）＋手動 | `life_feed.py` → ⛽ 中油油價（官方 XML `getCPCMainProdListPrice_XML`，92/95/98/超級柴油，`牌價生效日期` 民國年；日期變了才發、跟上週比）、🧾 統一發票（財政部 RSS invoice.xml，新一期才發）；狀態 `.github/life_state.json`（oil、invoice 最近 3 期）；Worker 指令 `/對獎`（invoice，number；最近兩期、末 N 碼規則同 `check_number()`，只有自己看得到）、`/油價`（oil）；勾 preview 只印、number 測對獎 | `DISCORD_LIFE_WEBHOOK`（沒設就 #每日氣象） |
| `announce.yml` | 改了 `.github/announce.md` | 把更新總結貼到 #進度（使用者說「上傳 DC 更新結果」時：改寫這個檔、推上去） | `DISCORD_WEBHOOK` |
| `rank.yml` | 每天 20:00 整（使用者要求固定 8 點；cron 18:31、19:13、19:47 提早啟動，程式 sleep 到 20:00 才貼；`last_day` 只有 8 點那次（schedule 或手動勾 wait）才記，今天貼過就跳過；排程被 GitHub 跳過時手動 Run workflow 勾 wait 補救；concurrency 一次只跑一個、checkout ref: main 拿最新狀態） | ThePornDude 前 20 名 → 排行榜頻道（修改同一則），標「可下載／只擋廣告」；每天另發一則跟昨天比的名次變動；並排 Tranco 全球流量排名（`tranco()` 下載 top-1m，`DOMAINS` 對照名稱→網域）、列出兩邊差最多的；附來源連結＋Similarweb 連結；勾 preview 只印在 annotations 不發 Discord；手動 Run workflow 勾 repost 會刪舊訊息重貼 | `DISCORD_RANK_WEBHOOK` |

其他：腳本也發布在 Greasy Fork（https://greasyfork.org/zh-TW/scripts/601901-video-helper），從 GitHub 自動同步。

## 外部鬧鐘（重要）

GitHub 自己的排程（cron）常延遲幾小時或直接跳過（排行榜的 cron 一次都沒自己跑過）。所以**匯率、排行榜、新聞改用 cron-job.org 當鬧鐘**：
時間到就 POST `https://api.github.com/repos/dadanny1122/video-helper/actions/workflows/{fx|rank}.yml/dispatches`，
body `{"ref":"main","inputs":{"wait":"true"}}`，Header 帶使用者的 fine-grained token（只有這個 repo 的 Actions 讀寫）。
匯率 `55 8-16 * * 1-5`、排行榜 `55 19 * * *`、新聞 `23 2,5,8,11,14,17,20,23 * * *`（body 只要 `{"ref":"main"}`，不用 wait）、天氣 `0 7 * * *`（mode daily）＋ `5 * * * *`（mode check）＋ `*/5 * * * *`（mode quake，地震）、計算機 `*/10 9-17 * * 1-5`（fx.yml，body `{"ref":"main","inputs":{"export":"true"}}`）（Asia/Taipei）。fx、rank 的 GitHub cron 已移除（會遲到好幾小時亂發，例如 16:50 的拖到 19:05 才發），只靠鬧鐘；程式也擋掉時段外或遲到超過 10～15 分鐘的執行。新聞、網站檢查的 cron 還留著（遲到沒關係）。
確認鬧鐘有效：看 runs 的 event＝workflow_dispatch、時間在 :55 附近。token 不要經過 AI 對話。

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
- 手機版：使用者之後打算換三星 Android（可開發性比較好）→ 到時用 Firefox＋暴力猴；目前只有 iPhone→ 先做 iPhone：Safari ＋ Userscripts App；v3.17.0 加了相容層；iPhone 已能裝、有盾牌和畫質清單，但 Pornhub 240p MP4 下載卡在「準備中」（Userscripts 沒 GM_download，備用法整檔抓進記憶體、沒進度）→ v3.18.0：沒 GM_download（`NATIVE_DL`）時 MP4 先給「① 開啟影片檔（Safari 分享→儲存到檔案）」或「② 在這頁下載」，並顯示等待秒數，使用者確認 iPhone 可以下載了（送到電腦版在手機上不能用：電腦版只聽 127.0.0.1）
