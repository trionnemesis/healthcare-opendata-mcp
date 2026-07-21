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

## 2026-07-21 排程資安審查（無人值守）

範圍：全 repo（`src/`、`scripts/`、`Dockerfile`、`deploy/k8s/`、`.github/workflows/`）
的機密掃描、相依套件 CVE 查核、SAST（bandit）、認證/授權檢視。詳細方法與逐項
結果見對應的 PR 說明。摘要如下：

### 環境（本次可用，補上次記錄的缺口）

- `pip install -e ".[dev]"` 成功（PyPI 可連線）。
- `python -m pytest`：112 passed，0 failed（無 pre-existing failures）。
- `bandit -q -r src`：0 High、3 Medium、2 Low，全數複查後判定為既有防禦機制
  已涵蓋或屬設計已知取捨（見下）。
- `pip-audit`：對已安裝套件（fastmcp 3.4.4、httpx 0.28.1、starlette 1.3.1、
  mcp 1.28.1、aiosqlite 0.22.1 等）掃描，**0 個已知漏洞**；僅回報 venv 內建的
  `pip`/`setuptools` 版本本身有 CVE，與專案執行期依賴無關。
- 專案未設定 lint / type-check 工具（pyproject.toml 無 ruff/mypy 設定，
  dev extras 只有 pytest + pytest-asyncio）；因此本輪未執行對應步驟。

### Bandit 逐項複查

| 位置 | 規則 | 判定 |
|---|---|---|
| `adapters/_pcc_opendata.py:11,53` | B405/B314 `xml.etree.ElementTree` | 已有自訂護欄：解析前拒絕 `<!doctype`/`<!entity` 前綴與 20MB 上限（`_safe_fromstring`），可有效阻擋傳統 XXE/entity expansion；未換 `defusedxml` 屬可選強化，不視為現存漏洞。 |
| `domain/query_guard.py:59` | B608 SQL 組裝 | 誤報：`table` 來自 `SqliteRepository.materialized_table()` 白名單映射（`datasets` 表已存在的 id），非使用者原始輸入；`where`/`columns`/`order_by` 另有 `_validate()` 黑名單 + 執行層 authorizer 白名單雙重防禦。 |
| `mcp_server/__main__.py:34` | B104 bind `0.0.0.0` | 設計已知：README「Trust & security」與 `deploy/README.md` 已明文揭露 MCP server 本身無認證，HTTP/GKE 部署須侷限於叢集內網或加 IAP/mTLS；非本次新增風險。 |
| `repository/sqlite_repo.py:266` | B101 `assert` | Low、非安全性問題（`assert cur.lastrowid is not None` 為內部不變量檢查，`-O` 下會被跳過但無安全影響）；記錄為待強化項目，未達本輪修補門檻（僅修 Critical/High）。 |

### 相依套件 / 容器基底

- `pyproject.toml` 對 `fastmcp`/`httpx`/`aiosqlite` 僅設下限、無 lockfile；
  本次無鎖檔可稽核精確版本，改以「當前解析安裝結果」跑 pip-audit（見上，0 漏洞）。
  建議：評估加入 lockfile（如 `uv.lock`）以利未來稽核重現性（未在本輪變更，
  屬 build 流程調整，留待人工決定）。
- `Dockerfile` 基底 `python:3.11-slim` 為浮動 tag；未鎖 digest。屬供應鏈重現性
  建議事項，非本次可判定的現存漏洞；未變更 Dockerfile（浮動 tag 屬 deploy
  行為變更範疇，留待人工決定）。
- `deploy/k8s/cronjob-sync.yaml` 內的 `bitnami/kubectl:latest` 與
  `google-cloud-cli:slim` 同為浮動 tag，建議日後改鎖定版本；因會改變實際部署
  行為，本輪僅記錄、未修改。

### 機密掃描

- 對 tracked files（含 `Dockerfile`、`deploy/`）以常見金鑰/token/私鑰樣式與關鍵字
  掃描，**未發現任何硬編碼機密**。

### 認證 / 授權

- MCP server 本身無 caller 認證（README/deploy README 已明文揭露）；`query_rows`
  的兩層防禦（語法白名單 + SQLite authorizer + 唯讀連線）是**查詢執行安全邊界**，
  非使用者身分驗證層 —— 與 repo 文件描述一致，非新發現。
- 未見傳統 IDOR 樣態：專案為單租戶自建工具，`get_record`/`get_tender_detail`
  等工具不含使用者身分或租戶概念，屬設計範圍內（部署前需自行加網路層存取控制，
  文件已揭露）。

### 結論

本輪未發現需要修補的 Critical/High 漏洞；因此本次無程式碼變更，僅新增本節
稽核紀錄。detailed findings table 見 PR 說明。
