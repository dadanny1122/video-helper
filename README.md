# Video Helper

影片網站輔助腳本（使用者腳本 / userscript）。

## 功能
- 安全防護：擋彈出視窗、自動跳轉、危險檔案下載（.exe / .apk 等）、通知權限請求，左下角顯示攔截次數
- 網站專屬規則：Pornhub 影片前廣告、XVideos「點哪裡都開廣告」等（參考 uBlock Origin 規則庫重新實作）
- 省流模式：已知的廣告網路在下載前就擋掉，網速留給影片（Pornhub、XVideos、Jable、MissAV）
- 自動略過影片前廣告
- 顯示目前網速、開始播放與整部載入的預估時間
- 列表頁顯示預估載入時間
- 廣告收集器：點左下角盾牌，列出這頁連到的其他網站，一鍵加入個人黑名單
- 隱藏廣告：隱藏別的網站放進來的廣告框框、透明點擊層
- 一鍵下載：選擇畫質、顯示檔案大小與預估時間後再下載，切片影片自動轉成 MP4（不支援加密影片）
- 設定：點暴力猴圖示，可以個別開關每個功能

## 支援網站
- Pornhub、XVideos：全部功能
- MissAV、Jable：全部功能（不含下載）

## 安裝
1. 安裝瀏覽器擴充功能 [Violentmonkey](https://violentmonkey.github.io/)（或 Tampermonkey）
2. 點這裡安裝腳本：[video-helper.user.js](https://raw.githubusercontent.com/dadanny1122/video-helper/main/video-helper.user.js)
3. 之後有新版本會自動更新

## 電腦版（下載工具）
適合下載高畫質、大檔案，下載結果直接是 MP4。

**一般使用者**：到 [Releases](https://github.com/dadanny1122/video-helper/releases/latest) 下載 `VideoHelper.exe`，雙擊執行。
- 分頁 ①「安裝擋廣告」：照著按 3 個按鈕，就能在 Chrome 裝好暴力猴和腳本
- 分頁 ②「下載影片」：貼網址、選畫質、下載成 MP4；可取消、失敗可重試
- 分頁 ③「下載紀錄」：完成／失敗／取消的紀錄，可重新下載、打開檔案位置
- 分頁 ④「設定」：下載資料夾、預設畫質、是否保存紀錄、錯誤日誌

**用 Python 執行**：
1. 安裝 Python 3
2. 安裝套件：`pip install yt-dlp imageio-ffmpeg`
3. 執行：`python video_helper_app.py`
4. 貼上影片網址 → 讀取 → 選畫質 → 下載

## 注意
下載的內容僅供個人使用，請勿轉傳或散布。

## 致謝
- 部分網站規則參考 [uBlock Origin 規則庫（uBlockOrigin/uAssets）](https://github.com/uBlockOrigin/uAssets)，以本專案自己的程式碼重新實作
- 切片轉 MP4 使用 [mux.js](https://github.com/videojs/mux.js)
- 電腦版下載使用 [yt-dlp](https://github.com/yt-dlp/yt-dlp)
