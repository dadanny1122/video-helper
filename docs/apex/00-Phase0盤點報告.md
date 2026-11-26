# Project APEX Phase 0 盤點報告

- 基準：Video Helper v1.50.0（tag `v1.50.0`，commit `044a2ee182fab458fcc061015d6fee146daf1457`，2026-11-26）
- 範圍：`nexus/`（約 9,000 行程式＋測試）、`docs/nexus/`、`.github/workflows/nexus.yml`
- 方法：直接讀程式碼和測試、在 Linux 跑測試、另外寫一支小程式驗證網路檢查（不在 repo 裡）。**沒有改任何正式程式碼**，也沒有推送。
- 限制：這次的環境是 Linux。沙箱的 Windows 測試（24 個）在這裡會跳過，所以 Windows 上的行為是「讀程式碼＋CI 設定」判斷的，**不是我親眼跑過的**。

---

## 0. 摘要（先看這段）

**成熟度**：Nexus 在「一台 Master＋少數節點、只下載影片」這個範圍內做得很紮實。任務狀態機、取消、重試規則、mTLS、撤銷、防重放、分段傳回都有程式碼，也有整合測試。APEX 文件列的項目裡，大概三分之一已經有了，三分之一只有基礎，剩下的完全沒有。

**最重要的三個風險**
1. **沙箱子程序和節點用同一個 Windows 身分執行。** 子程序被攻破的話，它讀得到也解得開節點的 mTLS 私鑰，就能冒充這台節點。原因是私鑰用 DPAPI CurrentUser 加密，而且就放在沙箱資料夾往上三層的地方。使用者自己的檔案它也都讀得到。這不是新的漏洞，威脅模型早就寫了「不擋同帳號的惡意程式」。但 §6 那句「yt-dlp 有漏洞時影響範圍被上面幾層限制」說得太滿：上面幾層只限制得了資源，限制不了檔案和網路。
2. **網路允許清單只擋得住「正常的程式碼」。** 我實測過：檢查裝在 `socket.socket.connect` 上，但 `connect_ex` 和 `_socket` 的低階呼叫都繞得過去，連得到 127.0.0.1。ffmpeg 是原生程式，本來就不經過這層檢查。正常的 yt-dlp 不會走這些路，所以平常沒事；但只要子程序裡有惡意程式碼，這層就等於沒有。
3. **高可用、AI、插件、可觀測性這四塊幾乎是零。** 不過以你目前**只有一台家用電腦**的情況，這四塊大多沒有立刻做的必要（見 §6）。

**最值得先做的事**：Phase 1 先做**低成本**的兩步：補安全測試、更正威脅模型，再偵測這台 Windows 能不能用 AppContainer。之後才決定要不要做真正的 OS 級隔離（PR3 以後）。理由見 §7.1。

---

## 1. APEX 現有功能盤點矩陣（五級分類）

分級：**已完成**＝有程式碼也有測試；**部分完成**＝有實作，但有重要缺口；**僅有基礎**＝有初步架構，離 APEX 目標還很遠；**尚未實作**；**無法確認**。

### 1.1 Advanced Sandbox

| 項目 | 分級 | 證據 | 實際保護範圍／缺口 |
|---|---|---|---|
| Job Object 資源限制 | 已完成 | `nexus/sandbox/jobs.py` `Job._set_limits`：記憶體 2GB、CPU 50%（硬上限）、8 個程序、`KILL_ON_JOB_CLOSE`、`DIE_ON_UNHANDLED_EXCEPTION`；`Job.spawn` 先暫停建立、放進 Job 再恢復。測試：`integration/test_sandbox_job.py`（8 個，只在 Windows 跑） | 只管**資源和程序生命週期**。檔案權限、網路、程序的權限等級都不管。沒設 UI 限制（剪貼簿、桌面） |
| 整組結束、沒有殘留 | 已完成 | `Job.terminate`、`wait_empty`；`runner.py` 有殘留就回 `LEFTOVER_PROCESSES`。測試：`test_cancel_kills_grandchild_too`、`test_terminate_kills_whole_tree_no_orphans` | 透過 WMI／COM 請別的程序代開的程序不在 Job 裡（同帳號下做得到），這種情況砍不到 |
| 工作資料夾隔離 | 部分完成 | `nexus/sandbox/workspace.py` `Workspace`（隨機 32 hex 資料夾）、`deliver`（擋連結、擋跑出資料夾、副檔名白名單、不覆蓋）。測試：`unit/test_sandbox_workspace.py`、`test_symlink_escape_not_delivered` | **只管「搬出來的成果」**，不管子程序能讀什麼。資料夾 ACL 用預設（檔頭有誠實註明） |
| 環境變數白名單 | 已完成 | `workspace.safe_env`：APPDATA／USERPROFILE 等都指到 tmp。測試：`test_env_whitelist_inside_child` | 只改「預設位置」。子程序用絕對路徑一樣讀得到真正的使用者資料夾 |
| 網站允許清單 | 部分完成 | `nexus/net/guard.py`（`check_host`：https、443、清單網域、公開 IP、不准直接寫 IP）；`nexus/sandbox/video.py` `install_net_guard`、`block_bypass_libs`。測試：`unit/test_net.py`、`unit/test_sandbox_video.py` | Python 層的檢查。**我實測**：`connect_ex`、`_socket.socket.connect` 都繞得過（§3.2）。ffmpeg 不受管 |
| OS 層級權限限制（Restricted Token／AppContainer／低完整性） | 尚未實作 | `jobs.spawn` 用 `subprocess.Popen`，跟節點同一個 token。`probe()` 只檢查 `_is_admin`。ADR-6（`docs/nexus/02-架構決策.md`）寫了要偵測 Restricted Token，程式碼沒做 | 這是 APEX-01 和現況最大的差距 |
| 沙箱稽核紀錄 | 僅有基礎 | 任務的成功／失敗代碼會進 `task_events` 和 `audit_events` | 「哪個操作被擋」這種拒絕事件沒有單獨記錄，網路被擋只看得到錯誤碼 `NET_BLOCKED` |
| 沙箱能力偵測、不降級 | 已完成 | `jobs.probe()`→`isolation` basic／none；`executor.SANDBOXED`、`sandbox_status()`；`scheduler.REASONS["no_sandbox"]`。測試：`test_worker_advertises_video_download_only_with_sandbox` | 只有兩級。`capabilities.ISOLATION_LEVELS=("basic","none")`，而且長度上限 8 個字元（Phase 1 要注意，見 §7） |

### 1.2 Universal Task Engine

| 項目 | 分級 | 證據 | 缺口 |
|---|---|---|---|
| 19 狀態狀態機 | 已完成 | `nexus/tasks/states.py` `TRANSITIONS`（有 assert 確保每個狀態都列了）；唯一入口 `TaskService._transition`（樂觀鎖＋同一個交易寫稽核）。測試：`unit/test_tasks_model.py`、`unit/test_task_service.py` | `RECONCILING` **定義了但沒有程式碼會進入**（只在 `display.py` 有中文名稱）。`UNKNOWN` 只能人工 `mark_failed` |
| 冪等 | 已完成 | `TaskService.submit`：同一個 key、同樣內容 → 回傳原本那筆；內容不同 → `IDEMPOTENCY_CONFLICT` | 冪等只做在「建立任務」。副作用的冪等（同一部影片下載兩次）靠 `AdapterSpec.idempotent` 宣告，沒有實際保證 |
| 重試 | 已完成 | `should_retry`：`RETRYABLE_ERRORS`／`NON_RETRYABLE_ERRORS`、`retry_safe`、`max_attempts`、過期不重試；每次重試開新的 attempt | 沒有退避（backoff）時間 |
| 取消 | 已完成 | `TaskService.cancel`：還沒到 Worker → 直接 CANCELLED；在 Worker 上 → CANCELLING 等 Worker 確認 | — |
| 逾時／過期 | 已完成 | `Dispatcher.sweep` 第 3、4 點 | — |
| 通用任務格式 | 部分完成 | `nexus/tasks/manifest.py`（嚴格驗證、16KB、SHA-256）、`adapters.AdapterSpec`（參數型別、資源需求、副作用、版本） | Adapter 是寫死的清單，目前有 4 個 test.* 加 `video.download`。這是**刻意的設計**（ADR-10：永遠不做 shell／python Adapter） |
| 任務依賴（DAG）、拆子任務、合併結果 | 尚未實作 | `nexus/tasks`、`nexus/storage` 搜不到 depend／parent／dag | — |

### 1.3 Distributed Scheduler

| 項目 | 分級 | 證據 | 缺口 |
|---|---|---|---|
| 硬性篩選＋排除原因 | 已完成 | `scheduler.REASONS`（14 種）、`evaluate`（純函式）、決策存在 `schedule_decisions` 表。測試：`unit/test_scheduler.py` | — |
| 資源評分 | 已完成 | `WEIGHTS["w1"]`（cpu／memory／disk／queue／reliability，權重有版本號）；資源由 `worker/resources.py` 回報 | 資源數字是節點**自己報的**，只拿來挑節點、不拿來授權（這點設計正確）。沒有 GPU（`test_weights_sum_to_one_and_no_gpu_penalty`） |
| 負載管理 | 部分完成 | `BUSY`、同時任務上限、同分時看忙碌程度再看 device_id | 沒有公平性或多使用者配額（目前只有一個使用者，不需要） |
| 多台實機 | 無法確認 | `docs/nexus/05-V1交付報告.md`：跨電腦沒測過；V2～V4 是同一台電腦開兩個 profile 驗收 | 只有一台家用電腦 |

### 1.4 Data Integrity

| 項目 | 分級 | 證據 | 缺口 |
|---|---|---|---|
| 分段傳輸＋每段 SHA-256 | 已完成 | `master/incoming.py` `write_chunk`（`X-VH-Chunk-SHA256`）、`worker/uploader.py` | — |
| 整檔 SHA-256、續傳 | 已完成 | `incoming.complete`（錯一次清掉重傳，錯兩次放棄）、`have` 位元圖、`retry_return`。測試：`integration/test_upload_return.py`（AT-030～037，含節點重開、Master 重開） | — |
| 結果驗證 | 部分完成 | `Dispatcher.verify`：只檢查檔案清單的**格式**（名稱、大小範圍、雜湊格式）。雜湊在 Worker 主程序裡算（`workspace.deliver`，不在沙箱子程序裡），這點是對的 | SHA-256 只證明「收到的跟節點送的一樣」，**不證明內容是真的從那個網站下載的**。不傳回的話，Master 只能相信節點自己的說法 |

### 1.5 Security

| 項目 | 分級 | 證據 | 缺口 |
|---|---|---|---|
| mTLS、私有 CA | 已完成 | `identity/certs.py`、`identity/ca.py`、`core/tls.py`（避開 truststore 的問題）。測試：`integration/test_master_v1.py`、`test_truststore.py` | `make_server_cert` 沒有 AuthorityKeyIdentifier → Python 3.13 用嚴格驗證時會失敗（§3.3） |
| 憑證撤銷 | 已完成 | `registry.revoke`：裝置變 REVOKED、所有憑證撤銷、指紋進永久黑名單、斷掉連線；每個請求都檢查（`CREDENTIAL_REVOKED`） | — |
| 防重放 | 已完成 | `server.py:385` 用 `repo.nonce_seen`（存在 SQLite 的 `nonces` 表，Master 重開也還記得）＋時間偏移 5 分鐘 | `protocol/messages.py` 裡的 `ReplayGuard` 類別（存在記憶體）看起來正式程式沒有用到，是遺留程式碼 |
| 私鑰保護 | 部分完成 | `identity/keystore.py`：`CryptProtectData(..., flags=0)`＝CurrentUser，沒有額外的 entropy | 同一個帳號的任何程式（**包含沙箱子程序**）都解得開（§3.1） |
| 威脅模型 | 部分完成 | `docs/nexus/threat-model.md` §1～§7 | §6 對 yt-dlp 漏洞的影響範圍說得太樂觀。`nexus/tests/security/` **是空資料夾** |
| 供應鏈 | 已完成（不在本次重點） | `requirements.txt` 有 290 個 `--hash=sha256`、`yt-dlp==2026.11.23` 固定版本 | — |

### 1.6 High Availability

| 項目 | 分級 | 證據 | 缺口 |
|---|---|---|---|
| 狀態持久化 | 已完成 | SQLite WAL（`storage/db.py`），任務、attempt、事件、上傳都在資料庫 | — |
| Master 重開後恢復 | 已完成 | `sweep` 每秒從資料庫重新推算。測試：`test_at011_master_restart_mid_task`、`test_at032_master_restart_resumes`、`test_n03_...` | — |
| 單一 Master 內的 fencing | 部分完成 | `lease_id`／`current_attempt_id` 比對（`dispatch.ack`、`event`、`upload_open`），舊的 attempt 回報會被拒 | 只在「一台 Master」的前提下成立 |
| 備援 Master、Leader Election、跨 Master fencing、複寫 | 尚未實作 | 沒有相關程式碼 | — |
| 備份／還原 | 僅有基礎 | 只有資料庫升級前做備份（`db.migrate`：先 `wal_checkpoint(TRUNCATE)` 再複製） | 沒有定期備份，沒有還原演練 |

### 1.7 AI Runtime

| 項目 | 分級 | 證據 |
|---|---|---|
| 模型管理、ONNX、GPU 排程、跨節點推論 | 尚未實作 | `nexus/` 完全搜不到 onnx／gpu／cuda |
| 本機 AI（不屬於 Nexus） | 已完成（桌面端） | `video_helper_app.py` 約第 2947 行起：whisper.cpp 字幕，版本和指紋寫死（`WHISPER_ASSET`、whisper-pin.yml） |

whisper 的「固定版本＋指紋」做法以後可以直接當 Model Registry 的雛形。

### 1.8 Plugin Runtime

| 項目 | 分級 | 證據 |
|---|---|---|
| 版本化的擴充介面 | 僅有基礎 | `adapters.AdapterSpec`（version、`major` 相容檢查、參數 schema、副作用宣告）。這等於「內建插件介面」，但只能把程式碼寫進 repo 來擴充 |
| 外部插件載入、簽章、WASM／WASI | 尚未實作 | 搜不到 importlib、entry_points、wasm |

### 1.9 Observability

| 項目 | 分級 | 證據 |
|---|---|---|
| 日誌 | 已完成（基本） | `master/service.py:42`、`worker/tray.py:32` 用 RotatingFileHandler |
| 稽核＋關聯 ID | 部分完成 | `audit_events` 表、`correlation_id`（正式程式碼用了 45 處）、`task_events`、`schedule_decisions`；GUI 的 `TaskService.detail` 一次拿得到整個任務的歷程 |
| Metrics、Traces、OpenTelemetry | 尚未實作 | 沒有相關程式碼 |

---

## 2. Nexus V1～V4 與 APEX 六大領域對照

| APEX 領域 | 對應的 Nexus 版本 | 現況一句話 |
|---|---|---|
| APEX-01 沙箱 | V3（PR2～PR6）、V3.1 | 資源限制做好了；檔案和網路只在程式層檢查，**沒有 OS 權限隔離** |
| APEX-02 通用運算引擎 | V2（狀態機、排程、派送） | 引擎是通用的，Adapter 刻意只有下載；沒有 DAG |
| APEX-03 高可用 | V2 lease／sweep、V4 續傳 | 單一 Master 能撐過重開；沒有備援 |
| APEX-04 AI | 無（桌面端有 whisper） | 零 |
| APEX-05 跨專業 | 無 | 零。要跑「使用者自己的 Python 計算」就跟 ADR-10 衝突，**必須先有強沙箱** |
| APEX-06 插件／可觀測 | V1 稽核、V2 決策紀錄 | 稽核不錯；沒有外部插件、沒有 metrics／trace |

---

## 3. 本次實際驗證到的問題（有證據）

### 3.1 沙箱子程序拿得到節點私鑰（推論＋程式碼證據，Windows 未實測）

- 沙箱根目錄：`executor.run_video_download` → `SandboxRunner(os.path.join(base, "sandbox"), ...)`，`base`＝節點設定檔所在資料夾（預設 `mode_dir("worker")`）
- 子程序的工作資料夾：`<base>/sandbox/work/<32hex>`，而且路徑會透過命令列參數傳給子程序（`child_command`）
- 私鑰：`CredStore` → `<mode_dir("worker")>/identity/`（KeyStore），用 `CryptProtectData(..., 0)` 加密＝CurrentUser
- 子程序用跟節點同一個 token 跑（`jobs.spawn` → `subprocess.Popen`）

→ 子程序裡的程式只要往上三層讀 `identity/`，再呼叫 `CryptUnprotectData`，就能拿到私鑰。**要被利用，前提是子程序裡跑了惡意程式碼**，例如 ffmpeg 解析惡意影片時被打進去（RCE），或 yt-dlp 的供應鏈被汙染。

### 3.2 網路檢查繞得過（Linux 實測）

驗證程式 `/home/claude/apex_probe/guard_bypass.py`：先裝上 `install_net_guard`，再對 127.0.0.1 的本機埠測試：

| 呼叫方式 | 結果 |
|---|---|
| `socket.socket().connect` | 被擋 ✅ |
| `socket.create_connection` | 被擋 ✅ |
| `socket.socket().connect_ex` | **連得上** |
| `_socket.socket.connect` | **連得上** |

意義：yt-dlp 正常使用時不會用這些呼叫，所以**現況的實際風險低**。但這證明這層防護只擋得住「不小心」，擋不住「惡意」。順便發現：`block_bypass_libs` 對已經 import 過的套件不會生效（`video.py:48`）。子程序是全新的程序，所以現在沒問題，但這個寫法很容易壞。

### 3.3 測試結果（Linux、Python 3.13；CI 用的是 3.12）

`xvfb-run python3 -m pytest nexus/tests`（排除需要 tkinter 的 `test_gui_tab.py`）：**315 通過、2 失敗、24 跳過**（跳過的是 Windows 才能跑的沙箱測試和需要顯示器的測試）。

| 失敗 | 原因 | 影響正式程式嗎 |
|---|---|---|
| `test_upload_return.py::test_put_limits_and_replay` | 這個測試用 `ssl.create_default_context`，Python 3.13 預設開嚴格 X.509 驗證，要求伺服器憑證有 AKI，但 `certs.make_server_cert` 沒加 | 現在不影響（正式程式用 `ssl.SSLContext()`，沒開嚴格模式）。是個潛在問題：以後換 3.13，或改用預設 context 就會壞 |
| `test_sandbox_video.py::test_bypass_network_libs_blocked` | 跟測試執行順序有關（前面的測試已經 import 了 requests），單獨跑會過 | 不影響。是測試隔離的問題，也就是 §3.2 說的寫法容易壞 |

第一次全部跑時是 3 個失敗，第二次只剩 2 個。第 3 個失敗沒有重現，我也沒記到名稱，算**無法確認**的不穩定測試。

---

## 4. 安全威脅模型（只列跟 APEX 有關的新增部分；其餘沿用 `threat-model.md`）

| # | 威脅 | 現有對策 | 剩餘風險 | 分級 |
|---|---|---|---|---|
| A1 | 惡意網站讓 ffmpeg 或 yt-dlp 跑出惡意程式碼 | Job 限制資源、只能連清單網站、固定版本＋雜湊 | 程式碼一旦跑起來：讀得到使用者所有檔案、偷得到節點私鑰、連得到區網（包含 Master） | **高影響／低機率** |
| A2 | 子程序繞過網路清單 | Python socket 檢查 | `connect_ex`、`_socket`、原生程式都繞得過（已實測） | 中（要先有 A1 才能利用） |
| A3 | 子程序跳出 Job | 先暫停建立再放進 Job、不允許 breakaway | 同帳號用 WMI／COM 請別的程序代開，就不在 Job 裡 | 中（要先有 A1） |
| A4 | 偷到的節點憑證被拿去冒充 | 撤銷、90 天效期 | 要等使用者發現並撤銷；目前沒有自動偵測異常 | 中 |
| A5 | 節點謊報結果 | 不傳回時只檢查格式；傳回時檢查 SHA-256 | 節點能回傳任意 mp4（例如換掉內容）。來源限制在清單網站，影響有限 | 低 |
| A6 | 單一 Master 掛掉 | 重開後從 SQLite 恢復、有測試 | 硬碟壞掉＝任務歷程全沒（沒有定期備份） | 低（目前只有一台電腦） |

**剩餘風險的本質**：Nexus 的信任邊界是「Windows 帳號」，沙箱只在同一個帳號**裡面**限制資源。APEX-01 要的是「帳號裡面再切一道權限邊界」。

---

## 5. 技術債與優先改善項目

| 優先 | 項目 | 證據 | 成本 |
|---|---|---|---|
| 1 | 威脅模型 §6 的說法要更正（影響範圍是「資源」，不是「檔案／網路」） | `threat-model.md` 第 88 行 | 文件 |
| 2 | `nexus/tests/security/` 是空的，A1～A3 都沒有測試 | `ls nexus/tests/security` | 小 |
| 3 | `capabilities.ISOLATION_LEVELS` 寫死 basic／none，長度上限 8 字元；`scheduler.py:92` 寫死 `!= "basic"`。加新的等級時**舊 Master 會拒絕新節點的心跳**（要在 PR 裡驗證） | `capabilities.py:11-12,38`、`scheduler.py:92` | 小，但會牽涉協定相容 |
| 4 | `RECONCILING` 是空狀態；`service.py` 註解說「V4 才做自動核對」，V4 沒做 | `states.py`、`service.py:156` | 中 |
| 5 | `make_server_cert` 缺 AKI（Python 3.13 相容性） | `certs.py:58-75` | 小 |
| 6 | `block_bypass_libs` 對已 import 的套件不生效；相關測試跟執行順序有關 | `video.py:46-49` | 小 |
| 7 | ADR-6 說要偵測 Restricted Token，`probe()` 沒做 | `02-架構決策.md` 第 72 行 vs `jobs.probe` | 小 |
| 8 | 記憶體上限是用「尖峰 ≥ 95%」推測的 | `Job.memory_limit_hit` | 小，可以接受 |
| 9 | 沒有定期備份資料庫 | `db.py` 只在升級時備份 | 小 |
| 10 | `protocol/messages.py` 的 `ReplayGuard` 看起來沒用到 | `server.py` 用的是 `repo.nonce_seen` | 小（刪掉前要確認） |

---

## 6. 直接沿用／補強／全新開發

原則：**有技術必要性、而且有實際效益**才做。你目前只有一台家用電腦，學校電腦不建議當節點，這會大幅影響以下判斷。

| 項目 | 建議 | 必要性 | 現有替代方案 | 預期效益 |
|---|---|---|---|---|
| 狀態機、冪等、取消、重試 | **直接沿用** | — | 已經有了 | — |
| 排程器、排除原因 | **直接沿用** | — | 已經有了 | — |
| 分段傳回、SHA-256、續傳 | **直接沿用** | — | 已經有了 | — |
| mTLS、撤銷、防重放 | **直接沿用**（補 AKI） | — | 已經有了 | — |
| 沙箱：OS 權限隔離 | **補強**（Phase 1） | 中：A1 的影響很大；而且以後想跑任何「不是下載」的工作（APEX-05），一定要先有它 | 讓節點用**另一個 Windows 帳號**執行（不用寫程式，手動設定，隔離效果還更好） | 私鑰和使用者檔案跟子程序隔開；區網和本機在 OS 層被擋 |
| 沙箱：網路 | **補強**，跟上一項一起做 | 同上 | 現有的 Python 檢查（保留，當第二層） | OS 層擋掉區網和 127.0.0.1；**網域白名單還是只能在程式層做**（Windows 防火牆只認 IP，不認網域） |
| 沙箱稽核（拒絕事件） | **補強** | 低到中 | 錯誤碼 | 出事時查得到原因 |
| RECONCILING 自動核對 | 補強，**之後再說** | 低：一個人用，人工標記就夠了 | `mark_failed` | 少按一次按鈕 |
| 定期備份資料庫 | 補強（很小） | 低到中 | 無 | 硬碟出問題時保得住歷程 |
| 備援 Master／Leader Election／複寫 | **先不做** | **低**：只有一台電腦，沒有第二台能當備援；SQLite 加上重開恢復已經夠用 | 重開恢復（已有測試） | 現在幾乎沒有 |
| DAG／拆子任務 | **先不做** | 低：影片下載用不到；等真的有「參數掃描」之類的需求再做 | 一次建多個獨立任務 | 現在沒有 |
| AI Runtime、GPU | **先不做** | 低：沒有第二台有 GPU 的節點；whisper 在桌面端本機跑就好 | 桌面端 whisper.cpp | 現在沒有 |
| 外部插件／WASM | **先不做** | 低，而且違反 ADR-10 的「永不跑任意程式」，風險反而變高 | 在 repo 裡加 Adapter | 現在是負的 |
| OpenTelemetry | **先不做** | 低：一個人用，日誌加稽核就查得到 | 日誌、稽核、`TaskService.detail` | 小 |
| 跨專業（例如 PySAM／PyPSA 情境平行求解） | **列為候選** | 中，跟你的論文有關，但前提是有第二台電腦，**而且**要先有強沙箱 | 在自己電腦上用 multiprocessing | 等有兩台以上電腦才有效益 |

---

## 7. Phase 1：Advanced Sandbox PR 級規格

### 7.1 為什麼建議分兩段

- **PR1、PR2**：幾乎沒有風險（只補測試、文件、偵測），不管之後做不做都有價值。
- **PR3 以後**：AppContainer 牽涉很多 Windows 細節，包含 PyInstaller 解壓縮、ffmpeg、憑證庫、資料夾權限，相容風險高。PR2 的偵測結果出來，你決定要做，才繼續。
- 另一條路是**節點改用另一個 Windows 帳號執行**：不用寫程式，效果比 AppContainer 還完整（帳號完全分開），只要在說明書寫設定步驟。缺點是要手動設定。**這兩條路請你選。**

注意：以下 AppContainer 的行為是根據 Windows 的設計推出來的，**這次在 Linux 上沒辦法驗證**。PR2 的工作就是在你的 Windows 上實測確認。

### 7.2 PR 清單（照依賴順序）

**PR1　安全測試＋威脅模型更正（不改行為）**
- 檔案：`nexus/tests/security/test_sandbox_escape.py`（新）、`docs/nexus/threat-model.md` §6、`nexus/tests/unit/test_sandbox_video.py`（修跟執行順序有關的問題）
- 測試（Windows）：子程序讀得到 `identity/`、`connect_ex` 連得到 127.0.0.1、`_socket` 繞過。**全部標成 `xfail(strict=True)`**，等於寫下「目前已知做不到」，以後真的修好時，測試會提醒要把標記拿掉
- 驗收：CI 綠燈；威脅模型的說法跟測試一致
- 回滾：刪掉測試檔

**PR2　偵測 OS 隔離能力（只偵測，不改執行方式）**
- 檔案：`nexus/sandbox/jobs.py`（新增 `probe_strong()`：建一個 AppContainer profile → 跑一個小程序 → 確認它讀不到使用者資料夾、連不到 127.0.0.1 → 刪掉 profile）、`worker/executor.py`
- 協定：capabilities 新增欄位 `hardening`（例如 `"appcontainer"`／`"none"`＋原因），**`isolation` 照舊報 `basic`**，舊 Master 才不會拒絕心跳。協定升 1.4（只新增欄位）
- GUI：節點分頁的「沙箱」欄顯示偵測結果
- 驗收：你的 Windows 上偵測結果正確；舊 Master（v1.50.0）配新 Worker，心跳正常
- 回滾：欄位是新增的，舊版會忽略

**（審核點：看 PR2 在你電腦上的結果，再決定要不要做 PR3～PR6）**

**PR3　AppContainer 啟動器**
- 檔案：`nexus/sandbox/jobs.py`（用 ctypes 直接呼叫 `CreateProcessW`，加 `STARTUPINFOEX`＋`PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`，只給 `internetClient`；照舊先暫停建立、放進 Job 再恢復）、`workspace.py`（只對工作資料夾授權給 AppContainer SID）
- `subprocess.Popen` 沒辦法傳 attribute list，所以管線（stdout）要自己接，這是這個 PR 最大的風險
- 開關：設定 `sandbox.strong`，預設**關閉**
- 測試：PR1 的 xfail 測試在開關打開時要變成通過
- 回滾：關掉開關 → 回到現在的 basic

**PR4　打包後的相容性**
- 檔案：`.github/nexus_frozen_check.py`、`nexus.yml` 的 frozen job
- 重點：PyInstaller onefile 會解壓到 `%TEMP%`（子程序裡已經指到 `ws.tmp`，要確認有授權）；EXE 所在的資料夾要讓「ALL APPLICATION PACKAGES」能讀取和執行（裝在下載資料夾的話可能沒有）；yt-dlp 的 https 憑證（certifi）；ffmpeg 合併影音
- 驗收：打包後的 EXE 在 AppContainer 裡真的下載成功一部清單內的影片

**PR5　Job UI 限制＋拒絕事件稽核**
- 檔案：`jobs.py`（`JOB_OBJECT_UILIMIT_*`：剪貼簿、桌面、全域 atom）、`runner.py`（被擋的事件從子程序回報，再寫進 `audit_events`，action 用 `sandbox.denied`）
- 驗收：被擋的操作在任務詳情裡看得到

**PR6　排程認得新等級**
- 檔案：`capabilities.py`（`ISOLATION_LEVELS` 改成有順序的等級）、`scheduler.py:92`（改成「≥ 需要的等級」）、`manifest.py`（`requirements.isolation` 選填，預設 basic）
- 驗收：要求 strong 的任務只派給有 strong 的節點，排除原因寫得清楚；沒寫這個欄位的舊任務行為不變

**PR7　實機驗收＋文件**
- 新增 `docs/apex/` 的規格和交付報告、說明書、SECURITY.md
- 實機測試：T-06（下載）、T-07（取消後沒有殘留）、U-01（交給節點）全部重跑；打開和關閉開關各跑一次

### 7.3 Phase 1 完成後，可以誠實地說

- 「沙箱子程序讀不到節點私鑰和使用者資料夾、連不到區網和本機」，而且有測試證明。
- 還是**不能說**：「能擋 Windows 核心漏洞」、「網域白名單是 OS 層級」。

---

## 8. 需要你決定的事

1. Phase 1 要走 **AppContainer（PR1～PR7）**、**另一個 Windows 帳號（PR1＋說明書）**，還是**先只做 PR1＋PR2** 看結果？我建議先只做 PR1＋PR2。
2. 高可用、AI、插件、OpenTelemetry 照 §6 的建議先不做，你同意嗎？
3. 這份報告要不要放進 repo 的 `docs/apex/`？目前只在本地，沒有 commit。

---

## 9. 使用者決定（2026-11-26）

照建議：
1. Phase 1 **先只做 PR1＋PR2**；PR3 以後看 PR2 在使用者電腦上的偵測結果再決定（另一個選項：節點用另一個 Windows 帳號執行）
2. 高可用、AI、插件、OpenTelemetry **先不做**
3. 這份報告放進 repo 的 `docs/apex/`

實作（分支 `apex/phase1-pr1-pr2`）：
- PR1：`nexus/tests/security/test_sandbox_escape.py`、威脅模型 §6 更正＋§8
- PR2（v1.51.0、nexus 0.5.0、協定 1.4）：`nexus/sandbox/appcontainer.py`（只偵測）、心跳 `hardening` 欄位（只送給 1.4 以上的 Master）、節點分頁顯示結果

## 10. PR2 結果與決定（2026-11-27）

- GitHub Windows（nexus.yml frozen）：`appcontainer`
- 使用者自己的電腦（v1.51.0，`VideoHelper.exe --nexus-probe-strong`）：`appcontainer`；run ✅、讀節點資料夾被擋、連 127.0.0.1 被擋（curl 等到逾時，exit 28；測試埠沒收到任何連線）
- **決定：選 B，Phase 1 先停在 PR2**。理由：只有一台電腦、平常用「送到電腦版」不開節點，沙箱只保護節點下載 → 現在做 PR3～PR7 幾乎沒有實際效益。
- 什麼時候重新考慮 PR3：有第二台電腦當節點，或要讓節點跑「不只是下載」的工作（APEX-05）
