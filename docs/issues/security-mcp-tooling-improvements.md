# Issue: 資安檢測與 MCP tool 改進追蹤

## 背景

本次針對 Healthcare OpenData MCP 進行資安檢測與 MCP tool 行為檢視。專案已具備 `query_rows` SQL 雙層防禦（語法護欄、唯讀 SQLite 連線、authorizer 白名單、VM step 上限），但仍發現可補強的輸入資源限制與 XML 解析護欄。

## 已完成修補

- [x] 將 `limit` 正規化邏輯抽成共用函式，統一套用 `1..200` 的工具回傳上限。
- [x] 將 `search_records` 的 `limit` 套用同一上限，避免 `LIMIT -1` 造成無上限查詢。
- [x] `search_records` 拒絕空白關鍵字，避免空查詢掃描整張 `records`。
- [x] PCC XML 解析前拒絕 DTD/ENTITY 與過大 XML 字串，降低 XML entity expansion / 大檔案 DoS 風險。
- [x] 補上對應單元測試，涵蓋 limit floor/cap、空關鍵字、XML DOCTYPE 拒絕。

## 檢測紀錄

| 檢測項目 | 指令 | 結果 |
| --- | --- | --- |
| 單元測試 | `python -m pytest` | 環境缺少可用的 `pytest-asyncio` plugin，導致 async tests 未被處理；非同步測試在此容器無法完整驗證。 |
| 安裝 dev dependencies | `python -m pip install -e '.[dev]'` | 受 PyPI 連線 403 限制，無法下載 `hatchling` / dev dependencies。 |
| SAST | `python -m bandit -q -r src` | 此容器未安裝 `bandit`。 |
| Dependency audit | `python -m pip_audit` | 此容器未安裝 `pip-audit`。 |

## 後續建議

- [ ] 在 CI 固定安裝 `pytest-asyncio` 並跑完整 async test matrix（Python 3.11 / 3.12）。
- [ ] 在 CI 新增 SAST：`bandit -r src`。
- [ ] 在 CI 新增依賴弱點掃描：`pip-audit` 或 Dependabot。
- [ ] 評估改用 `defusedxml` 解析 PCC XML；目前已先以 DTD/ENTITY 與大小上限做輕量護欄，若允許新增依賴可再強化。
- [ ] 評估為 `search_records` 加上可設定的最小關鍵字長度（例如至少 2 個非空白字元），降低高頻模糊查詢負載。
- [ ] 評估新增 MCP tool：`describe_query_syntax`，讓 agent 在查詢前可取得欄位型別、limit 上限、範例與安全限制，減少錯誤查詢。

## 驗收條件

- `query_rows` 與 `search_records` 都不接受無限制輸出。
- 空白 `keyword` 不會觸發全表 LIKE 查詢。
- PCC XML 含 `DOCTYPE` / `ENTITY` 時會被拒絕。
- CI 可完整跑過 pytest、Bandit、依賴弱點掃描。
