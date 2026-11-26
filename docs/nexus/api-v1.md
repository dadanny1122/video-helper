# Nexus API v1（實作版，對照 `nexus/master/server.py`、`nexus/worker/client.py`）

- 基底：`https://<master>:<port>/api/v1`，TLS 1.2+，Master 用自己的私有 CA 簽的伺服器憑證。
- 協定版本 `1.0`（`nexus.PROTOCOL_VERSION`）；主版號不同 → `426 PROTOCOL_INCOMPATIBLE`。
- 所有請求與回應都是 JSON，而且都有**訊息外框**：

```json
{
  "protocol_version": "1.0",
  "message_id": "uuid",
  "sent_at": "2026-11-15T10:00:00Z",
  "sender_id": "anonymous | worker:<device_id> | master",
  "correlation_id": "uuid",
  "nonce": "32 hex（只有請求要）",
  ...端點自己的欄位
}
```

- GET 沒有 body，外框放在標頭 `X-VH-Envelope`（JSON 字串）。
- Master 記 `(sender_id, nonce)` 10 分鐘，重複 → `409 REPLAY_DETECTED`；`sent_at` 與伺服器差 > 300 秒 → `400 CLOCK_SKEW`。
- 速率限制每來源 IP、每端點；超過 → `429 RATE_LIMITED` ＋ `Retry-After`。body > 64KB → `413`。
- 錯誤回應：`{"error": {"code", "message", "details"}}`（外框照帶），錯誤碼清單在 `nexus/core/errors.py`。

## 端點

### `POST /devices/register`　身分：無憑證＋配對碼　限制 5/分
請求：`pair_code`（6 位數）、`name`（≤32）、`platform`、`agent_version`、`csr_pem`（EC P-256 CSR）。
回應：`device_id`、`registration_token`（只給這一次；Master 只存雜湊）、`status: "PENDING"`、`fingerprint`（節點公鑰指紋）、`master_ca_fingerprint`（Worker 要釘住）。
失敗：`PAIR_CODE_INVALID`／`PAIR_CODE_EXPIRED`／`PAIR_CODE_BURNED`、`AGENT_VERSION_TOO_OLD`、`SCHEMA_INVALID`。

### `GET /devices/{id}/registration`　身分：`Authorization: Registration <token>`　限制 12/分
回應 `status`：`PENDING`（繼續等）／`ACTIVE`（附 `certificate_pem`、`ca_pem`、`expires_at`，**只給一次**，之後再問只回狀態）／`REJECTED`／`REVOKED`／`EXPIRED`。
token 不對或註冊 1 小時沒核准 → `404 REGISTRATION_NOT_FOUND`。

### `POST /devices/{id}/heartbeat`　身分：mTLS　限制 2/秒
請求：`sequence`（遞增）、`agent_version`、`status`（`IDLE`）。
回應：`status`（裝置狀態）、`server_time`、`heartbeat_seconds`（10）、`renew_recommended`（到期 ≤14 天為 true）。
失敗：`CREDENTIAL_REVOKED`／`CREDENTIAL_EXPIRED`／`CREDENTIAL_MISMATCH`（憑證裡的 id ≠ 路徑 id）／`DEVICE_SUSPENDED`／`DEVICE_NOT_ACTIVE`。

### `POST /devices/{id}/renew`　身分：mTLS　限制 2/分
請求：`csr_pem`（**新金鑰**的 CSR）。回應：`certificate_pem`、`expires_at`。
舊憑證標 `SUPERSEDED`，還能用 24 小時（讓換憑證過程不斷線），之後拒絕。

### `POST /devices/{id}/disconnect`　身分：mTLS　限制 6/分
正常下線：連線狀態改 `DISCONNECTED`，不改裝置狀態。

### `GET /system/info`　身分：mTLS　限制 30/分
回應：`nexus_version`、`protocol_version`、`min_peer_protocol`、`min_agent_version`、`server_time`。

## 沒有的端點（刻意）
核准、拒絕、暫停、恢復、撤銷、列表、稽核——**V1 沒有網路端點**，只能在 Master 那台電腦的「⚙ 進階設定 → 🖧 節點」操作（同程序呼叫 `DeviceRegistry`）。任何任務、命令、檔案傳輸端點也都沒有。

## 狀態與時間
- 裝置狀態：`PENDING → ACTIVE ⇄ SUSPENDED → REVOKED`，`ACTIVE → EXPIRED`，`PENDING → REJECTED`；`REVOKED`／`REJECTED`／`EXPIRED` 是終點，重新配對＝新 `device_id`。
- 連線狀態（只給人看）：`CONNECTED`（心跳 ≤30 秒）→ `STALE`（30 秒～5 分）→ `OFFLINE`（>5 分）；`DISCONNECTED`＝自己說再見。
- 憑證：裝置 90 天（到期前 14 天更新）、伺服器 1 年（到期前 30 天或綁定位址變了就自動換）、CA 10 年。

---

# 協定 1.1（V2：任務派送，電腦版 v1.30.0 起）

1.0 的 Worker 照樣能註冊、心跳（主版號相同），只是**不會被派任務**（排程排除原因：「節點版本太舊」）。

## 心跳多帶的欄位（`POST /devices/{id}/heartbeat`，都可省略）

| 欄位 | 內容 | 限制 |
|---|---|---|
| `resources` | `cpu_count`、`cpu_load`（0～1）、`memory_total_mb`、`memory_available_mb`、`disk_free_mb`、`platform`、`arch` | 不認得的欄位、超出範圍 → 整份能力資料丟掉（心跳照算）＋稽核 `capabilities.rejected` |
| `adapters` | `{"test.echo": "1.0", …}` | ≤32 個；名稱、版本格式固定 |
| `max_concurrency` | 1～4 | |
| `accepting` | 要不要接任務 | |
| `running` | 執行中任務 id | ≤64 個 UUID |

> 這些是 Worker 自己報的：**只拿來挑節點，不拿來授權**。

## `GET /workers/{id}/inbox?wait=25`　身分：mTLS　限制 60/分
長輪詢：有東西就馬上回，沒有就等到 `wait` 秒（最多 25）。回應 `messages`：
- `{"kind":"task", task_id, attempt_id, lease_id, lease_expires_at, manifest, manifest_sha256}`——**收到不代表可以開始**
- `{"kind":"cancel", task_id, attempt_id, reason}`——`reason`＝`cancelled`（使用者取消）或 `timed_out`（Master 判逾時）

同一份還沒 ACK 的派送，lease 到期前每次問都會再給一次（Worker 沒收到上一次回應也沒關係）。

## `POST /workers/{id}/task-ack`　身分：mTLS　限制 120/分
請求：`task_id`、`attempt_id`、`lease_id`、`accept`、`reason`（拒收原因，例如 `ADAPTER_UNSUPPORTED`、`MANIFEST_INVALID`、`FULL`）。
回應：`{"start": true}` 才可以開始；`false` 時有 `reason`（`lease_invalid`＝已取消或收回、`lease_expired`＝太晚）。重送同一個 ACK 回同樣結果。

## `POST /tasks/{task_id}/events`　身分：mTLS（裝置身分取自 `sender_id`＋憑證）　限制 600/分
請求：`event_id`（UUID，去重用）、`attempt_id`、`seq`（每個 attempt 從 1 遞增）、`type`、`data`（≤8KB）。
`type`：`PREPARING`／`RUNNING`／`PROGRESS`（`data.progress` 0～1）／`SUCCEEDED`（`data.result`）／`FAILED`（`data.error_code`、`data.message`）／`CANCELLED`／`TIMED_OUT`。
回應：`{"status": 任務狀態, "stop": true/false}`（`stop: true`＝任務已結束或被取消，Worker 該停）；同一個 `event_id` 再送 → `{"duplicate": true}`；序號倒退 → `409 EVENT_OUT_OF_ORDER`；舊 attempt 的事件 → `{"stale": true, "stop": true}`（記錄但不影響任務）。
`SUCCEEDED` 的結果由 **Master 自己驗證**（大小、回聲內容與 SHA-256、睡的秒數），不通過 → `RESULT_REJECTED`。

## 新錯誤碼
`TASK_NOT_FOUND`（404）、`TASK_NOT_ASSIGNED`（403，任務不是派給你的）、`LEASE_EXPIRED`（409）、`INVALID_TRANSITION`（409）、`CONCURRENT_UPDATE`（409，可重試）、`IDEMPOTENCY_CONFLICT`（409）、`MANIFEST_INVALID`（400）、`ADAPTER_UNSUPPORTED`（422）、`EVENT_OUT_OF_ORDER`（409）。

## 仍然沒有的端點（刻意）
建立／取消任務、看排程理由——只在 Master 那台的「⚙ 進階設定 → 📋 節點任務」（決策 D2）。

# 協定 1.2（V3：節點下載影片，電腦版 v1.31.0 起）

- 心跳 `resources` 多兩個欄位：`isolation`（`basic`／`none`）、`isolation_reason`（≤200 字）。舊版 Master（1.1）不認得 → 整份能力丟掉、不派任務，所以兩邊要一起更新。
- 新任務類型 `video.download`（1.0）：`params` = `url`（https、`nexus/net/sites.json` 的頁面網域、≤2048 字）＋`quality`（`best`／`1080`／`720`／`480`，可省略）；`limits` 多 `max_output_bytes`（只有這個類型帶，上限 8GB）；`max_runtime_seconds` 上限 7200。
- 只派給 `isolation == basic` 而且宣稱支援 `video.download` 的節點；排除原因 `no_sandbox`。
- 成功結果：`{"files": [{"name","size","sha256","path"}], "title", "site", "id", "height"}`；Master 驗證檔名不含路徑字元、大小合理、雜湊 64 位 16 進位。檔案留在節點（V4 才傳回）。
- 節點回報的新錯誤碼：可重試 `NETWORK`、`RATE_LIMITED`；不重試 `NET_BLOCKED`、`NOT_FOUND`、`DISK_FULL`、`SANDBOX_UNAVAILABLE`、`OUTPUT_REJECTED`、`TOO_BIG`、`CLEANUP_FAILED`、`LEFTOVER_PROCESSES`、`PROTOCOL_UNSUPPORTED`。
- 仍然沒有建立／取消任務的網路端點。


# 協定 1.3（V4：節點把檔案傳回 Master，電腦版 v1.33.0 起）

- `video.download`、`test.file` 的 `params` 可多 `return_to_master: true`。協定 < 1.3 的節點不會被派這種任務（排除原因 `no_return`）。
- 新任務類型 `test.file`（1.0）：`size_mb`（1～2048）＋`return_to_master`；節點在自己的下載資料夾寫固定內容的檔案（不連網、不需要沙箱）。
- 新事件 `UPLOADING`（`data.result` 同成功結果）：RUNNING → UPLOADING，Master 先驗結果格式、存下每個檔案的名稱／大小／SHA-256（之後只認這份）。全部收齊後節點送 `SUCCEEDED` → Master 檢查每個檔案都驗證通過 → VERIFYING → SUCCEEDED，結果改成 `{"files":[{"name","size","sha256","path"（Master 上）,"node_path"}], "returned": true, …}`。
- UPLOADING 時節點離線**不判 UNKNOWN**；24 小時沒進展 → FAILED `TRANSFER_EXPIRED`（檔案留在節點）。`FAILED → UPLOADING`（「重新傳回」，只限 `TRANSFER_FAILED`／`TRANSFER_EXPIRED`）。UPLOADING 可以取消。
- inbox 多一種訊息：`{"kind":"upload", task_id, attempt_id, next_seq, files:[{index,name,size,sha256,path}]}`——傳回中卻 60 秒沒動靜時提醒節點接著傳（節點重開、斷線回來、重新傳回）；節點只會傳自己下載資料夾裡、大小相符的檔案。

## `POST /tasks/{task_id}/uploads`　身分：mTLS（sender_id）　限制 30/分
請求：`attempt_id`、`file_index`。只有任務派給你、狀態 UPLOADING、attempt 是目前那次才行。
回應：`upload_id`、`chunk_size`（8 MB）、`chunks_total`、`size`、`have`（已收到的段號）、`done`、`rate_limit_mbps`（0＝不限）。
Master 空間不夠 → `507 MASTER_DISK_FULL`＋`Retry-After: 300`。

## `PUT /uploads/{upload_id}/chunks/{n}`　身分：mTLS（sender_id）　限制 600/分
body＝檔案內容（`application/octet-stream`），**≤ 8 MB**（只有這個端點放寬）；外框（含 nonce）放 `X-VH-Envelope`；`X-VH-Chunk-SHA256`＝這段的雜湊。
長度一定要等於依宣告大小算出的長度（`SIZE_MISMATCH`）、雜湊不對 → `CHUNK_MISMATCH`（不算收到，重傳這段）。出錯時 Master 一律 `Connection: close`。

## `GET /uploads/{upload_id}`　限制 60/分
回應：`have`、`chunks_total`、`status`（`open`／`done`／`failed`／`cancelled`）。

## `POST /uploads/{upload_id}/complete`　限制 30/分
Master 算整檔 SHA-256：對 → 搬進下載資料夾（檔名重新清理、副檔名白名單、不覆蓋）→ `{"ok": true, "master_path", "all_done"}`；重送回同樣結果。
還有缺 → `UPLOAD_INCOMPLETE`（`details.missing`）；整檔不對 → `HASH_MISMATCH`（第一次清掉重傳，`details.final=false`；第二次 `final=true`、放棄）。

## 新錯誤碼
`UPLOAD_NOT_ALLOWED`（403）、`UPLOAD_INCOMPLETE`（409）、`UPLOAD_EXPIRED`（410）、`CHUNK_MISMATCH`（400）、`SIZE_MISMATCH`（400）、`HASH_MISMATCH`（409）、`MASTER_DISK_FULL`（507）、`OUTPUT_REJECTED`（400）；節點回報 `TRANSFER_FAILED`（不重試，可重新傳回）。

# 協定 1.4（APEX Phase 1 PR2：強隔離偵測，電腦版 v1.51.0 起）

- 心跳 `resources` 可多兩個欄位：`hardening`（`appcontainer`／`none`）、`hardening_reason`（≤200 字）。**只是偵測結果，排程不看它**，下載還是用 1.2 的 basic 沙箱。
- 跟 1.2 不一樣的地方：節點從 Master 回覆裡的 `protocol_version` 知道對方版本，**只在 Master ≥ 1.4 時才送**這兩個欄位（`HARDENING_MIN_PROTOCOL`），所以舊版 Master 不用一起更新。剛連上的第一次心跳不知道版本，不會帶。
- 偵測在背景跑一次（`VideoHelper.exe --nexus-probe-strong <節點資料夾>`，印一行 JSON）；還沒跑完之前也不會帶。

# 協定 1.5（APEX：自訂資源，電腦版 v1.53.0 起）

- 心跳 `resources` 可多 `named`：節點自己宣告的資源 `{"gpu": 1, "pysam": 1}`（名字小寫英數 `. _ -`、最多 16 種、數量 1～1024；節點小視窗「自訂資源」設定，存在 `named_resources`）。跟 1.4 一樣**只送給 Master ≥ 1.5**（`NAMED_MIN_PROTOCOL`）。
- 任務 manifest `requirements.resources`（選填，同樣格式）：排程只派給「宣告數量 − 正在跑的任務佔掉的」還夠的節點，不夠的排除原因 `resource`。沒寫這個欄位的任務跟以前完全一樣（舊節點照樣收）。
