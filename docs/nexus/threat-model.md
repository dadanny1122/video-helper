# Nexus V1 威脅模型（兩台電腦安全連線）

> 對象：`VideoHelper.exe`（Master，下載器裡的「🖧 節點」分頁）與 `VideoHelper.exe --worker`（Worker）。
> V1 只做：連線、註冊、核准、心跳、憑證更新、撤銷。**沒有任務派送、沒有遠端執行**，所以這份只談「身分與連線」的威脅。

## 1. 資產

| 資產 | 在哪 | 丟了會怎樣 |
|---|---|---|
| Master CA 私鑰 | `%APPDATA%\VideoHelper\nexus\master\pki\ca.key`（DPAPI 加密） | 任何人都能簽出被信任的節點憑證 → 最高資產 |
| Master 伺服器私鑰 | 同上 `server.key` | 可冒充 Master 騙 Worker |
| 節點私鑰 | `nexus\worker\device.key`（DPAPI） | 可冒充那台節點（直到被撤銷） |
| 註冊 token | SQLite `registrations`（只存 SHA-256） | 可冒領 Pending 中那張憑證 |
| 配對碼 | 記憶體，5 分鐘 | 可把陌生機器排進 Pending（還是要人按核准） |
| 稽核紀錄 | SQLite `audit_events`（trigger 禁改禁刪） | 事後查不出誰做了什麼 |

## 2. 攻擊者

- **A1 同網路的陌生人**（咖啡廳、宿舍 Wi-Fi）：能看到流量、能連 Master 的埠、能亂送封包。
- **A2 拿到舊憑證的人**（被撤銷的節點、被複製的檔案）。
- **A3 本機其他程式／使用者**（同一台電腦、低權限）。
- **A4 假 Master**（有人在網路上架一台假的，等 Worker 來連）。
- 不在範圍：拿到 Windows 管理員權限的人（DPAPI 對同帳號的程式沒有防護）、作業系統或 Python 本身的漏洞。

## 3. 威脅 → 對策 → 測試

| # | 威脅 | 對策（程式位置） | 驗證 |
|---|---|---|---|
| T1 | A1 偷看流量 | 只有 HTTPS（TLS 1.2+），`server.py` | N-01 明文被拒 |
| T2 | A1 直接呼叫心跳／更新端點 | mTLS：沒有 Master CA 簽的憑證 → 403 `CREDENTIAL_*`；路徑 id 必須＝憑證裡的 id（`CREDENTIAL_MISMATCH`） | AT-002、N-02 |
| T3 | A1 猜配對碼 | 6 位數、5 分鐘、一次、錯 5 次作廢；註冊 5 次/分/IP | AT-001、AT-023 |
| T4 | A1 排進 Pending 等人手滑 | 畫面上要先核對 Master 指紋，核准要確認＋理由；Pending 不給任何憑證 | AT-022 |
| T5 | A1 重放抓到的請求 | 每則訊息 `nonce` 存 10 分鐘、`sent_at` 偏移 > 5 分鐘拒絕 | N-03、N-04 |
| T6 | A1 洪水攻擊 | 每端點每 IP token bucket；payload 上限 64KB；連線上限 64 | AT-023 |
| T7 | A2 用舊憑證連 | 撤銷＝憑證指紋進 `revoked_fingerprints`（永久）＋ 裝置 REVOKED；請求層每次都查 DB，不只看 TLS 有沒有過 | AT-003、N-05 |
| T8 | A2 撤銷後既有連線繼續用 | `ConnectionRegistry`：撤銷／暫停／停用 Master 時主動關閉該裝置（或全部）的 socket | AT-003、N-06 |
| T9 | A2 被撤銷後「重新配對」拿回信任 | 重新配對一定產生新金鑰＝新身分；舊指紋永遠在黑名單 | N-05 |
| T10 | A3 偷私鑰檔 | DPAPI（同 Windows 帳號才解得開）；非 Windows 退回 0600 並在畫面提醒 | 單元 test_identity |
| T11 | A3 讀日誌找密碼 | 日誌遮罩（`masking.py`）：私鑰、token、配對碼、Authorization、X-VH-* | 單元 test_core |
| T12 | A4 假 Master | Worker 第一次連線釘住 CA 指紋（TOFU，畫面要人核對 8 碼）；核准回來的 CA 必須＝註冊時說的，而且要真的簽了這條連線的伺服器憑證；之後 CA 變了 → `MASTER_CHANGED` 停止、不自動信任 | 整合 test_worker_agent（Master 換 CA） |
| T13 | Master 重啟後狀態亂掉 | 全部狀態在 SQLite；重啟後 Worker 心跳照常、撤銷名單還在 | N-07 |
| T14 | Worker 離線很久 | 30 秒 STALE、5 分鐘 OFFLINE（只標記，不撤銷）；憑證 90 天、14 天前自動更新、全部過期才 EXPIRED | sweep 單元／整合 |
| T15 | 綁錯網卡暴露公網 | `BindPolicy`：公網位址直接拒絕、0.0.0.0 要設定檔明確確認、LAN 位址 GUI 再問一次 | 整合 test_bind_policy |
| T16 | Worker 繼承下載器的權限／設定 | `--worker` 在建 Tk 之前就分岔，不建 App、不讀 settings.json、不開 Bridge、不碰 AI 金鑰；各自 config、log | 程式碼審查、N-09 |
| T17 | 稽核被竄改 | `audit_events` 有 BEFORE UPDATE／DELETE trigger 直接 RAISE | AT-019 |

## 4. 已知殘餘風險（V1 接受）

1. **TOFU 的第一次**：Worker 第一次連線若網路上已經有假 Master，使用者沒核對指紋就會釘到假的。對策只有畫面提醒＋ Master 那邊看不到 Pending 就知道不對。
2. **DPAPI 範圍**：同一個 Windows 帳號跑的惡意程式可以解開私鑰。這是 Windows 使用者層級的信任模型，V1 不另加密碼。
3. **沒有 OS 級隔離**：Worker 是一般程序。V1 沒有任務，所以沒有「任務逃逸」問題；V3 沙箱（Job Object＋允許清單）文件已誠實標示不是 OS 級隔離。
4. **時間**：兩台時鐘差超過 5 分鐘就連不上（會顯示 `CLOCK_SKEW`），不自動校時。

## 5. V2（任務派送）新增的威脅

| # | 威脅 | 對策（程式位置） | 驗證 |
|---|---|---|---|
| T18 | 有人透過網路下任務 | 沒有任何建立／取消任務的網路端點；只在 Master 本機 GUI（`TaskService`） | 程式碼審查、`api-v1.md` |
| T19 | 惡意或寫錯的任務內容（未知欄位、超大、超範圍、不存在的類型） | `manifest.validate()` 嚴格驗證，排程前就 REJECTED；16KB 上限；Adapter 固定允許清單（沒有 shell／python 類） | AT-005、`test_tasks_model.py` |
| T20 | 派送途中任務內容被改 | Master 存 `manifest_sha256`；Worker 收到重算，不符拒收 | T-05、`test_worker_tasks.py` |
| T21 | 別台節點冒領或亂報別人的任務 | inbox／ACK／事件都要 mTLS；attempt 綁定 device_id，不是派給你的 → `TASK_NOT_ASSIGNED` | `test_events_from_other_device_refused` |
| T22 | 未核准／暫停／撤銷的節點拿任務 | 跟心跳同一套 `authenticate()`；撤銷時它手上的任務改 UNKNOWN、未開始的收回改派 | AT-001、AT-010 |
| T23 | 重送、亂序的事件讓任務重複完成或倒退 | `event_id` 去重、`(attempt, seq)` 遞增、狀態機只准合法轉移、終態不再改 | AT-012 |
| T24 | 派出去了、節點沒收到就重派 → 同一個任務跑兩次 | Worker 一定要等 ACK 回 `start: true` 才開始；沒 ACK 的才收回，晚到的 ACK 一律 `start: false` | AT-009 |
| T25 | 節點失聯後自動重跑有副作用的任務 | ACK 之後失聯 → UNKNOWN，**不自動重跑**，人工確認才標記失敗（V4 才做自動核對） | AT-010 |
| T26 | Worker 謊報成功 | `SUCCEEDED` 進 VERIFYING，Master 自己驗證結果 | `test_master_verifies_result_itself` |
| T27 | 節點謊報資源搶任務 | 資源數字只影響挑選，不影響授權；Worker 收到後自己再檢查一次 | 規格 25.3、程式碼審查 |
| T28 | 長輪詢佔滿 Master 連線 | 每 IP inbox 60 次/分、最多等 25 秒、連線上限 64；停用 Master 時先放掉所有長輪詢 | AT-023 |

V2 的測試 Adapter 不開子程序、不碰檔案、不連網，所以**還不需要沙箱**；任何會碰檔案或網路的 Adapter 都要等 V3 的沙箱。

## 6. V3（節點在沙箱裡下載影片，電腦版 v1.31.0）新增的威脅

隔離等級照實寫：**基本（程序限制＋程式自己檢查網路）**，不是 OS 級隔離（規格 2.2、ADR-6）。

| # | 威脅 | 對策（程式位置） | 驗證 |
|---|---|---|---|
| T29 | Master（或被冒用的 Master）叫節點連內網、雲端 metadata、任意網站 | 三層：建任務時 `guard.static_check`（https／443／`sites.json` 頁面網域／不准 IP、帳密）；子程序裡 `video.install_net_guard`：每次 getaddrinfo 只解析一次並檢查網域＋443＋全部是公開 IP（`is_global`、IPv4-mapped 還原），connect 只准連檢查過的 IP；影片伺服器網址下載前再檢查 | `test_net.py`、`test_sandbox_video.py`（含真的 yt-dlp：清單網域解析到 127.0.0.1 → NET_BLOCKED）、AT-007 |
| T30 | 繞過 Python socket 的網路套件（curl_cffi 等）或 ffmpeg 自己連網 | 子程序 `BYPASS_LIBS` 一律當作沒裝；yt-dlp 全部用 native 下載器，ffmpeg 只做本機合併；協定白名單（拒 rtmp、f4m…） | `test_bypass_network_libs_blocked`、`test_unsupported_protocols_refused` |
| T31 | 任務吃光記憶體／CPU、狂開程序、跑不停 | Job Object：記憶體 2GB、CPU 50% 硬上限、8 個程序、`KILL_ON_JOB_CLOSE`；時間由節點計時 → `TerminateJobObject` | `test_sandbox_job.py`（Windows CI）、AT-008 |
| T32 | 取消或當掉後留下下載程序（孫程序） | 暫停建立→放進 Job→才恢復（沒有空檔逃出）；結束後確認 Job 內 0 個程序，有殘留就算失敗；打包成 EXE 後也實測 | `test_cancel_kills_grandchild_too`、nexus.yml `frozen` job、AT-015、AT-026 |
| T33 | 任務寫到資料夾外、用連結偷搬使用者檔案、檔名帶磁碟機代號 | 隨機工作資料夾；`deliver` 只收真的在 output 裡、不是連結、副檔名 mp4/mkv/webm/m4a 的檔；`sanitize_filename`＋`is_inside`；一個不合格整批不搬；不覆蓋 | `test_sandbox_workspace.py`（含 `C:`、`x:`、UNC、符號連結）、AT-006 |
| T34 | 任務讀到節點的秘密（憑證、金鑰、瀏覽器 Cookie） | 環境變數白名單；APPDATA／USERPROFILE／HOME 都指到任務 tmp；yt-dlp 不用 Cookie、不用系統代理、不寫快取；子程序只讀 input/task.json | `test_env_whitelist_inside_child` |
| T35 | 清理失敗卻顯示成功 | 刪不掉工作資料夾 → CLEANUP_FAILED | `test_cleanup_reports_failure_when_file_locked`、AT-024 |
| T36 | 沙箱建不起來就偷偷不用沙箱照跑 | 節點啟動 `jobs.probe()`；不是 basic 就不宣稱支援 video.download，Master 排除原因 `no_sandbox` | `test_worker_without_sandbox_does_not_offer_video_download`、AT-025 |
| T37 | 子程序回報亂塞資料拖垮節點 | stdout 每行 64KB、最多 10 萬則事件，超過就整組結束；結果由 Master `verify` 檢查檔案清單格式 | 程式碼審查、`dispatch.verify` |

**V3 接受的殘餘風險**：同一個 Windows 帳號下的惡意程式不受沙箱限制；工作資料夾權限用 Windows 預設（沒另設 ACL）；網路限制在 Python 層，不是防火牆；yt-dlp 本身有漏洞時，影響範圍被上面幾層限制，但不是零。


## 7. V4（節點把影片傳回 Master，電腦版 v1.33.0）新增的威脅

| # | 威脅 | 對策（程式位置） | 驗證 |
|---|---|---|---|
| T38 | 別台節點（或被撤銷的節點）冒充上傳、塞檔案進 Master | 傳檔四個端點都要 mTLS＋sender_id 跟憑證一致；`upload_id` 綁「裝置＋任務」，任務一定要派給你、狀態 UPLOADING；撤銷 → 憑證失效，連線當場切斷 | `test_at034_other_device_cannot_upload`、AT-034 |
| T39 | 節點送惡意檔名（`..\`、`CON`、`C:x`、`.exe`）跳出下載資料夾或放執行檔 | 檔名只認 UPLOADING 時驗過的那份；搬檔前 `sanitize_filename`＋副檔名白名單（mp4/mkv/webm/m4a）＋`is_inside`＋`unique_target` 不覆蓋 | `test_incoming.py::test_malicious_names_stay_inside`、`test_extension_not_allowed`、`test_no_overwrite`、AT-035 |
| T40 | 塞超大資料吃光 Master 磁碟／記憶體 | 只有 `PUT .../chunks/{n}` 放寬到 8 MB，其他端點仍 64 KB；每段長度一定要等於依宣告大小算出的長度；宣告大小 ≤ `max_output_bytes`（8 GB）；body 串流寫檔（1 MB 一次讀）；開始前檢查空間（要多留 1 GB），不夠 → `MASTER_DISK_FULL` 不收 | `test_put_limits_and_replay`、`test_wrong_length_and_bad_index`、`test_disk_full_then_ok`、AT-036 |
| T41 | 傳輸中資料被改、或節點檔案壞掉卻被當成成功 | 每段 SHA-256（不對不算收到）；收齊後算整檔 SHA-256 跟 UPLOADING 時登記的比，不對就清掉重傳一次、再錯就放棄；半成品放 `incoming\`，驗證前不進下載資料夾 | `test_bad_chunk_hash_not_counted`、`test_whole_file_hash_mismatch_retry_then_give_up`、AT-033 |
| T42 | 重放舊的傳檔請求 | PUT 也帶 nonce＋時間，跟 POST 一樣 10 分鐘防重放 | `test_put_limits_and_replay` |
| T43 | Master（或被冒用的 Master）利用「接著傳」叫節點傳別的檔案（偷檔案） | 節點 `check_files`：只傳自己下載資料夾裡、不是連結、大小相符的檔案；Master 給別的路徑就回報 TRANSFER_FAILED，不傳 | `uploader.check_files`、程式碼審查 |
| T44 | 半成品或節點那份沒清掉、或節點太早刪掉 | 取消／失敗／24 小時沒進展 → 刪半成品（清查每秒補刪）；節點只在 Master 回 SUCCEEDED 之後才刪自己那份 | `test_at037_cancel_while_uploading`、`test_at030_return_ok_and_node_copy_deleted` |
| T45 | 節點關機就把傳回中的任務判成「狀態不明」、或節點關閉時偷偷再開傳回 | UPLOADING 離線不判 UNKNOWN（撤銷才判）；節點關閉後路上回來的 inbox 訊息不處理 | `test_at031_worker_restart_resumes`、`test_d7_expire_then_retry_return` |

**V4 接受的殘餘風險**：SHA-256 只保證「收到的跟節點送的一樣」，不保證影片內容安全（來源仍只限 `sites.json`）；檔案傳回時在兩台各存一份一段時間；同一台電腦測試測不出真實網路斷線（用程式模擬）。
