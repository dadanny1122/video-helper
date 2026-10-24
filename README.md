# Video Helper

影片網站輔助腳本（使用者腳本 / userscript）。

📖 **[說明書（第一次用請看這裡）](https://dadanny1122.github.io/video-helper/manual/)**

## 功能
- 安全防護：擋彈出視窗、自動跳轉、危險檔案下載（.exe / .apk 等）、通知權限請求，左下角顯示攔截次數
- 網站專屬規則：Pornhub 影片前廣告、XVideos「點哪裡都開廣告」等（參考 uBlock Origin 規則庫重新實作）
- 省流模式：已知的廣告網路在下載前就擋掉，網速留給影片（Pornhub、XVideos、Jable、MissAV）
- 影片狀態：評估框最上面顯示載入中／播放中／緩衝中（卡頓幾次）／載入失敗；記錄實際開始播放的時間來校正預估（只存數字）
- 自動略過影片前廣告
- 顯示目前網速、開始播放與整部載入的預估時間
- 列表頁顯示預估載入時間
- 🩺 狀態檢查：點盾牌 →「🩺 狀態檢查」，看腳本每個部分是否正常、這個網站開了哪些功能和已知限制
- 廣告收集器：點左下角盾牌，列出這頁連到的其他網站，一鍵加入個人黑名單
- 🤖 問 AI（選用）：透過電腦版問 AI 這些網域是不是廣告；只送網域名稱，金鑰只存在自己電腦，AI 只給建議
- 攔截原因：盾牌清單最上面列出最近擋了什麼、為什麼擋；擋錯了可以「這是誤擋？」一鍵回報，或「⏸ 這個分頁先暫停擋廣告」（關掉分頁就恢復）
- 隱藏廣告：隱藏別的網站放進來的廣告框框、透明點擊層
- 一鍵下載：選擇畫質、顯示檔案大小與預估時間後再下載，切片影片自動轉成 MP4（不支援加密影片）
- 送到電腦版：配對電腦版後，按「🖥 送到電腦版」直接交給電腦版下載（大檔、背景下載）
- 設定：點暴力猴圖示，可以個別開關每個功能

## 支援網站
- Pornhub、XVideos：全部功能
- MissAV、Jable：全部功能（不含下載）

## 安裝
1. 安裝瀏覽器擴充功能 [Violentmonkey](https://violentmonkey.github.io/)（或 Tampermonkey）
2. 點這裡安裝腳本：[Greasy Fork 頁面](https://greasyfork.org/zh-TW/scripts/601901-video-helper) → 按綠色「安裝此腳本」
   （或直接從 GitHub 裝：[video-helper.user.js](https://raw.githubusercontent.com/dadanny1122/video-helper/main/video-helper.user.js)）
3. 之後有新版本會自動更新

## 電腦版（下載工具）
適合下載高畫質、大檔案，下載結果直接是 MP4。

**一般使用者**：到 [Releases](https://github.com/dadanny1122/video-helper/releases/latest) 下載 `VideoHelper.exe`，雙擊執行。
- 分頁 ①「下載影片」：貼網址、選畫質、加入下載佇列（一次排很多部，或一次貼多個網址）；可取消、失敗可重試（按右鍵有更多選項）
- 分頁 ②「下載紀錄」：雙擊播放；打開檔案位置；按右鍵可重新下載、刪除紀錄
- 分頁 ③「設定」：下載資料夾、預設畫質、配對瀏覽器；其他（檔名格式、關閉視窗的行為、備份、檢查更新、錯誤日誌、統計）在「⚙ 進階設定…」
- 分頁 ④「安裝擋廣告」：照著按 3 個按鈕，就能在 Chrome 裝好暴力猴和腳本（第一次打開會自動帶你到這裡）
- 系統匣：關閉視窗時可以縮到右下角繼續下載，完成會跳通知
- 瀏覽器連動：「③ 設定」→「🔗 配對瀏覽器…」，之後在影片網頁按「🖥 送到電腦版」即可（只聽本機，需配對）
- 更新內容見 [CHANGELOG.md](CHANGELOG.md)

**用 Python 執行**：
1. 安裝 Python 3
2. 安裝套件：`pip install yt-dlp imageio-ffmpeg pystray pillow`
3. 執行：`python video_helper_app.py`
4. 貼上影片網址 → 讀取 → 選畫質 → 下載

## 注意
下載的內容僅供個人使用，請勿轉傳或散布。

## 關於內附的 ffmpeg
電腦版 EXE 內附的 ffmpeg 是用 `.github/ffmpeg/build.sh` 從 [FFmpeg](https://ffmpeg.org/)（n7.1）原始碼編出來的迷你版，
只保留「接檔、換成 MP4 包裝」需要的部分，不會重新壓縮影片。沒有啟用 GPL 元件，以 LGPL v2.1 授權，原始碼見 FFmpeg 官方網站。

## 致謝
- 部分網站規則參考 [uBlock Origin 規則庫（uBlockOrigin/uAssets）](https://github.com/uBlockOrigin/uAssets)，以本專案自己的程式碼重新實作
- 切片轉 MP4 使用 [mux.js](https://github.com/videojs/mux.js)
- 電腦版下載使用 [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- 合併與轉 MP4 使用 [FFmpeg](https://ffmpeg.org/)（LGPL）
