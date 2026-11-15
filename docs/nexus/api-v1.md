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
