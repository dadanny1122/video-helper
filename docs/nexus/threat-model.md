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
