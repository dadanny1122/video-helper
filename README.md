# Video Helper

影片網站輔助腳本（使用者腳本 / userscript）。

## 功能
- 自動略過影片前廣告
- 顯示目前網速、開始播放與整部載入的預估時間
- 列表頁顯示預估載入時間
- 一鍵下載：選擇畫質、顯示檔案大小與預估時間後再下載（不支援加密影片）

## 安裝
1. 安裝瀏覽器擴充功能 [Violentmonkey](https://violentmonkey.github.io/)（或 Tampermonkey）
2. 點這裡安裝腳本：[video-helper.user.js](https://raw.githubusercontent.com/dadanny1122/video-helper/main/video-helper.user.js)
3. 之後有新版本會自動更新

## 電腦版（下載工具）
適合下載高畫質、大檔案，下載結果直接是 MP4。

**一般使用者**：到 [Releases](https://github.com/dadanny1122/video-helper/releases/latest) 下載 `VideoHelper.exe`，雙擊執行即可。

**用 Python 執行**：
1. 安裝 Python 3
2. 安裝套件：`pip install yt-dlp imageio-ffmpeg`
3. 執行：`python video_helper_app.py`
4. 貼上影片網址 → 讀取 → 選畫質 → 下載

## 注意
下載的內容僅供個人使用，請勿轉傳或散布。
