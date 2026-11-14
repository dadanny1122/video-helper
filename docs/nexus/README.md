# Video Helper Nexus — 規劃文件

來源：使用者提供的《Video Helper Nexus 技術開發總規格書 v1.0》（2026-11-15，25 章＋3 附錄；原檔不在 repo）。
依規格 §25.1 的順序：

| Phase | 文件 | 狀態 |
|---|---|---|
| 0＋1 Repository Discovery／Current-State Audit | [00-現況盤點.md](00-現況盤點.md) | 完成 |
| 2 Gap Analysis（需求追溯矩陣） | [01-差距分析.md](01-差距分析.md) | 完成（Existing 6／Partial 30／Missing 38／Blocked 2） |
| 3 Architecture Decision Records | [02-架構決策.md](02-架構決策.md) | 完成，3 項待使用者確認 |
| 4 Implementation Plan（V1） | [03-V1實作計畫.md](03-V1實作計畫.md)＋[04-V1技術規格.md](04-V1技術規格.md) | 完成；三項決策 2026-11-15 已確認（LAN 優先＋預留 VPN、加 cryptography、單 EXE 雙模式）＋額外 5 項強制要求 |
| 5 V1 Implementation | PR1 起 | 進行中 |

規格書的禁止事項摘要（每次動工前看）：不重寫整個專案、不用 TODO／mock 冒充完成、不做任意遠端執行、沙箱不足不靜默降級、沒跑過的測試不說通過、SHA-256／TLS／Job Object 不單獨宣稱為完整安全。
