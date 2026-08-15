# Changelog

## [0.6.3] - 2026-08-14

### Fixed
- **`enrich_bid_deadline` 漏掉決標檢查**:module docstring 寫明處理「最近、尚未決標、值還空」的招標公告,
  但 `_candidates()` 只檢查 `announcement_type='招標公告'`、`date>=threshold`、`bid_deadline` 為空、標題屬 IT 類,
  從未比對同案是否已決標 —— 宣告的行為從一開始就沒被實作。
  - 招標與決標在半月檔是兩筆獨立 record(共用 `job_number`/`case_no`),只看招標那筆看不出案子已結束
  - 後果是額度排擠:明細頁逐案抓取受 `--limit` 與 `--throttle` 約束,已決標但欄位仍空的舊案會佔掉配額,
    排擠仍可投標的新案 —— 而看板要顯示的正是後者的剩餘天數
  - 修正:先掃出決標公告的 `job_number` 集合,再從招標候選中排除;`_TENDER` / `_AWARD` 提為常數
    (與 `adapters/pcc_tender.py` 同值),進度訊息一併補上「未決標」

### Changed
- `pyproject.toml` 的 pytest `pythonpath` 加入 `scripts`,讓維運腳本可被測試 import(`scripts/` 不是 package)。

### Verified
- 新增 `tests/scripts/test_enrich_bid_deadline.py` 8 項(決標排除 4 + 既有四項條件與排序不回歸 4);
  決標排除案例在修正前確實失敗(`['A-1'] != []`)、修正後通過,其餘 7 項前後皆過。
- 全套件 124 tests 通過;`bandit -r src -ll` 0 Medium;`pip-audit` 無弱點。

## [0.6.2] - 2026-07-25

### Added
- **真正的 CI**(`.github/workflows/ci.yml`,回應 issue #4 / #6):原本 `.github/workflows/` 只有 Pages 部署,
  README 的 `pytest` 全靠手動執行,故先前刻意不掛會誤導的 CI badge。現補三個 job:
  - `test`:`pytest` matrix Python 3.11 / 3.12(dev extras 含 `pytest-asyncio`,async 測試不會被靜默跳過)
  - `sast`:`bandit -r src -ll`(Medium 以上失敗)
  - `audit`:`pip-audit`(無 lockfile,故稽核實際解析安裝的版本樹;跑在 3.12 避免稽核到 ensurepip 夾帶的 bootstrap 套件)
  - README 掛上對應 CI badge,Development 章節補齊與 CI 同門檻的本機指令
- dev extras 新增 `bandit`、`pip-audit`,讓本機與 CI 檢測門檻一致。

### Changed
- 兩處 bandit Medium findings 加上 `# nosec` 與理由註記(非全域關閉規則,保留未來偵測能力):
  - `query_guard.py` B608:`table` 來自 dataset_id 白名單、欄位片段已過 `_validate`,非使用者原始輸入
  - `mcp_server/__main__.py` B104:容器/K8s 需綁 `0.0.0.0` 才收得到 Service 流量,且可由 `HCMCP_HOST` 覆寫
  - (原本還有第三處 `_pcc_opendata.py` B314「待人工核可 defusedxml」的 `# nosec`;合併 master 時
    採用已在 `ae72bb9` 落地的 `defusedxml`,B314 不再觸發,該註記隨之移除 —— 此處補正實際落地內容)

### Verified
- Python 3.11 / 3.12 各 112 tests 通過;`bandit -r src -ll` 0 issues;乾淨環境 `pip-audit` 回報 No known vulnerabilities。

## [0.6.1] - 2026-06-27

### Fixed
- **DB 路徑單一真實來源**:`hcmcp-sync` 與 `hcmcp` server 預設 DB 統一為 `~/.hcmcp/hcmcp.db`。
  `mcp_server/__main__.py` 移除冗餘的 `os.environ.get("HCMCP_DB", default_db_path())` 雙讀,
  改直接用 `cli.default_db_path()`(其本就讀 `HCMCP_DB`)為唯一來源,杜絕 server 與 sync 預設漂移。
  - 歷史地雷:本機看板半月排程(`~/.hcmcp/sync_board.sh`)、`~/.claude.json`、`.codex` 皆把
    `HCMCP_DB` 指向專案目錄 `hcmcp.db`,而文件化預設 `~/.hcmcp/hcmcp.db` 為空殼 →
    照 README 不帶 `--db` 執行 `hcmcp-sync` 會寫空殼,看板/MCP server 卻讀專案目錄,
    形成「同步了卻查不到」假象。已將真資料(58 衛福部資訊勞務標案 + 24,582 健保診所)
    遷至 `~/.hcmcp/hcmcp.db`,並把上述本機接線一律改用此正準 DB。
  - README 補「sync 與 server 共用預設、改路徑要兩邊一起設 `HCMCP_DB`」防雷註記。

### Verified
- 新增 `tests/test_db_path.py`(預設 `~/.hcmcp/hcmcp.db` + `HCMCP_DB` 覆寫 + 單一來源);全測試套件通過。

## [0.6.0] - 2026-06-13

### Changed
- **範圍縮小**:由「全機關標案 × 全醫療健保開放資料」收斂為 **衛生福利部轄下機關的資訊勞務相關標案 + 健保診所**(案量精簡到可逐案 enrich 截標/開標/預算)
  - `PccTenderAdapter` 新增 `title_includes` / `title_excludes`(衛福部範圍內再篩資訊勞務 IT 關鍵字);`_keep()` 統一機關前綴 + 主題篩選
  - `cli.py` 只註冊 `nhi-clinic` + 衛福部資訊勞務 `pcc-tender`(dataset_id 由 `pcc-tender-mohw` 改為 `pcc-tender`);移除全機關 pcc-tender、其他醫院/健保統計/靜態 CSV 資料集;IT 關鍵字提為 `IT_INCLUDE`/`IT_EXCLUDE` 常數(與看板/排程同步)
  - 看板 + 半月排程縮成「衛福部資訊勞務」(文案/快照/查詢同步)

### Removed
- local DB 清除超範圍資料(`scripts/prune_local_db.py`,dry-run 預設 + `--apply`):
  - 保留 `nhi-clinic`、`pcc-tender` 就地縮成衛福部資訊勞務(10,988 → 58 筆)
  - 移除 `pcc-tender-mohw`、`nhi-hospital-district`/`regional`、`nhi-hospital-bed-ratio`、`nhi-insured-population`、`mohw-outpatient-rate`、`mnd-military-hospital-fee`
  - VACUUM 後 DB 180MB → 50MB

### Fixed
- enricher 對齊當前 PCC(2026):持久 session(先 GET indexTenderBasic 取 JSESSIONID,否則搜尋只回表單頁)+ 明細連結改抓 `/prkms/urlSelector/common/tpam?pk=`(舊 readBulletion 保留容錯)。實證可取近期衛福部資訊勞務招標案的截標/開標/預算 — 看板「截標/開標」欄已有真實值(如 115-2-013 截標 2026-06-16、預算 761 萬);舊案截標後明細下架,抓不到屬正常

### Verified
- 106 tests 通過(新增 6 adapter 主題篩選 + 1 tpam 連結);prune dry-run/apply 筆數一致(58 筆全衛福部、6 機關、招標 24/決標 34);真實 enrich 5 近期招標案成功;看板 Playwright 截標欄渲染剩餘天數正確

## [0.5.0] - 2026-06-13

### Added
- 招標案截標/開標/預算 enrich(看板「截標/開標」欄需求):
  - `pcc-tender` / `pcc-tender-mohw` 新增欄位 `bid_deadline`(截止投標)、`open_date`(開標時間)、`budget`(預算金額)— 半月 open data 招標檔沒有這些,只能逐案爬 web.pcc 明細頁
  - `adapters/_pcc_detail.py`:明細頁解析純函式(stdlib html.parser,不引入 selectolax),擷取截標/開標/預算;th/td 與 td/td 雙模型、忽略 script/style 內文字。fixture 取自 g0VMCP(MIT)實戰頁驗證
  - `adapters/pcc_detail.py`:`PccDetailEnricher`(DI HTTP client)— POST readTenderBasic 搜尋 → readBulletion 明細頁 → 解析;403/429 raise BlockedError
  - MCP tool `get_tender_detail(job_number)`:即時抓單案明細(截標/開標/預算/採購屬性)
  - `scripts/enrich_bid_deadline.py`:對近期、未決標、未 enrich 的 IT 類招標案逐案補欄位;限量(--limit)+ 逐案節流(--throttle)+ 被封鎖即停。半月排程在 sync 後執行,再重匯 data.js
  - 看板新增「截標/開標」欄(剩餘天數 badge:剩 N 天 / 今天截止 / 已截止),招標案名稱下顯示預算金額

### Note
- 真實頁實證:**開標時間**在 web.pcc 明細頁是可靠的表格欄位(投標文件須在開標前送達,開標時間=實際投標 deadline);截止投標多數頁也可從表格抽到,抽不到時看板以開標時間為準
- enrich 走逐案爬明細頁(反爬風險),刻意限量+節流,僅覆蓋「看板會顯示、仍可投標」的近期 IT 招標案

### Verified
- 99 tests 通過(新增 24:明細解析 15 + enricher 5 + get_tender_detail 4)
- 看板 Playwright:8 欄表頭、剩餘天數/已截止/開標/決標各情境渲染正確、零 JS error

## [0.4.0] - 2026-06-13

### Added
- `pcc-tender`(全機關政府採購標案):`PccTenderAdapter` 第二實例(`agency_prefix=""`、`collection="procurement"`)— twinkle-hub 故障停用後,Cowork「政府採購 IT 標案看板」與半月排程 `pcc-it-tender-biweekly` 的替代資料源(dataset_id 與欄位與 twinkle 完全相容,查詢端僅 `ILIKE` 需改 `LIKE`)
- `PccTenderAdapter` 新增 `collection` 參數;`agency_prefix=""` 表全機關不過濾
- `scripts/export_board_data.py`:匯出 `pcc-tender` 全量為看板 `data.js` 快照 — Cowork artifact 的 `callMcpTool` 僅能呼叫 claude.ai remote connector(無法呼叫本機 stdio MCP),看板改讀快照,由半月排程在 sync 後重新匯出

### Changed
- `query_rows` / `search_records` limit 硬上限 200 → 400(對齊看板單次查詢量 400;executor 唯讀連線 + authorizer 白名單 + VM 步數護欄不變)
- `hcmcp-sync --tender-months` 預設 3 → 12(支撐看板「近 1 年」招標視圖;PCC 站上實際可回溯約 6 個月,歷史隨每次同步累積)

### Verified
- 75 tests 通過(新增 3:全機關不過濾、dataset meta、limit 400 釘規格)
- live sync(2026-06-13):pcc-opendata +12,270 筆(`pcc-tender` 全機關,招標 6,614/決標 4,374,回溯至 2025-06)+177 筆(mohw,招標回溯 12 月)
- 看板實際 WHERE(IT 關鍵字 + 近 90 天)經 QueryService 回 274 筆 / limit=400 未截斷(舊上限 200 會截斷此查詢)

## [0.3.0] - 2026-06-11

### Added
- GKE 部署支援:
  - `HCMCP_TRANSPORT=http`(MCP streamable HTTP,stateless 可多 replica;SSE 留作既有部署相容,spec 已 deprecated)
  - `/healthz` custom route(K8s readiness/liveness probe)
  - `Dockerfile`(單 image 雙 entrypoint `hcmcp`/`hcmcp-sync`,non-root uid 10001)
  - `deploy/k8s/`:Deployment(GCS artifact 模式,initContainer 拉 DB 至 emptyDir)+ CronJob(每日 sync → 上傳 GCS → rollout restart)+ Service + RBAC/ServiceAccount(Workload Identity)
  - `deploy/README.md`:架構圖、前置作業、bootstrap 與驗證步驟
- `resolve_transport()` 純函式抽取(transport/host/port 解析,便於測試)

### Verified
- 67 tests 通過(新增 5 tests:transport 解析 4 + healthz probe 1)
- 容器 E2E:docker build → 掛載 176MB DB 啟動 → `/healthz` 200 → MCP `initialize` over streamable HTTP 回應正常

## [0.2.2] - 2026-06-10

### Added
- 啟動護欄:`hcmcp` 啟動時偵測空 DB,直接以明確訊息退出(指出 DB 路徑、指引先跑 `hcmcp-sync`),避免 server 起來後查無資料的隱性失敗

### Verified
- 62 tests 通過(新增 2 tests:空 DB 報錯含路徑與指引 / 有資料正常放行)

## [0.2.1] - 2026-06-10

### Added
- `nhi-clinic`(健保特約醫事機構-診所):NHI 一級 API,約 24.5k 筆/每日更新
  - 實查更正:需求提供之 rId `A21030000I-D32001-001` 查無資料;經 openapi 目錄盤點,正確 resource ID 為 `A21030000I-D21004-009`

### Verified
- 60 tests 通過;live sync nhi-opendata +125,701 筆,`query_rows("nhi-clinic", limit=10)` 回傳 10 筆,縣市聚合正常

## [0.2.0] - 2026-06-10

### Added
- 接通 Twinkle healthcare collection 剩餘 4 資料集(05 矩陣 ③ 完整覆蓋):
  - `nhi-hospital-bed-ratio`(#9402,info.nhi.gov.tw 一級 API,複合鍵 機構代碼|統計年月)
  - `nhi-insured-population`(#25842,vac.gov.tw CSV)
  - `mohw-outpatient-rate`(#176510,mohw.gov.tw 縣市別系列 2 檔合併,欄位重命名統一 schema)
  - `mnd-military-hospital-fee`(#142696,mnd.gov.tw CSV)
- `StaticCsvAdapter`:多 URL 合併單一資料集、column_renames、複合 natural key
- `NhiDatasetSpec.natural_key_columns`:複合鍵支援;CSV 正規化抽出共用 `_csv.normalize_csv`

### Verified
- 59 tests 通過;live E2E:nhi-opendata 101,150 筆 + gov-static 168,552 筆 + pcc 9 筆,4 個新資料集 `query_rows limit=10` 各回傳 10 筆,GROUP BY 聚合正常

## [0.1.0] - 2026-06-10

### Added
- MVP:NHI 健保特約醫事機構(地區/區域醫院)+ pcc-tender-mohw 衛福部標案,`SourceAdapter → ETL → 物化表 → query_rows` 全鏈路
- MCP 工具:`list_datasets` / `get_dataset` / `query_rows` / `search_records` / `get_record` / `list_sources`(對齊 Twinkle Hub query_rows)
- SQL 安全護欄:唯讀連線、單一 SELECT 白名單、limit 硬上限 200

### Verified
- 52 unit/integration tests 全數通過
- Live E2E(2026-06-10):`hcmcp-sync` 實抓 nhi-opendata 457 筆 + pcc-opendata 9 筆;`query_rows(dataset_id='nhi-hospital-district', limit=10)` 成功回傳 10 筆
