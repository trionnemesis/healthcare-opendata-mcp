# Changelog

## [0.9.0] - 2026-09-10

### Added
- **Pages 部署改為「同步 → 匯出 → 驗證 → 部署」(issue #31 第 1 項)**:先前
  `pages.yml` 只把已提交的 `docs/` 上傳,看板**可重建但不會自己重建**。
  - `_site` 一開始就以委入的快照當種子(**last-known-good**);sync/export 全程寫進
    `$RUNNER_TEMP/stage`,**通過驗證且非空**才整份取代 `_site`。中途任何一步失敗都
    不會動到 `_site` —— artifact 層級的原子性替換。
  - 新增 `verify_dashboard.py --require-non-empty`:`empty` 對契約而言是合法狀態
    (委入快照走一般驗證路徑),但在「用新快照覆蓋舊快照」那一刻,空資料等於用看起來
    成功的東西蓋掉真實資料。故閘門只掛在部署路徑上。
  - **同步失敗 → 部署 last-known-good,再讓 job 轉紅。** #31 同時要求「保留前一份
    有效快照」與「失敗則不部署」,字面上互斥;且「失敗就不部署」會讓一個只改文案的
    push 因為上游剛好掛掉而永遠上不了線。先部署再轉紅同時滿足四條要求,且不讓上游的
    健康狀況綁架我們自己的靜態內容。靜默成功等於用舊快照冒充今日資料。
  - `permissions` 維持 `contents: read` —— 評估過讓 workflow 把快照 commit 回
    `master`,**否決**:那需要 `contents: write`,等於為了更新資料而讓部署 workflow
    取得寫入 repo 的權限,且會產生 bot 對 master 的提交流。`_site` 是 ephemeral
    artifact,git 裡的委入快照本來就是 last-known-good。
  - 加上 job `timeout-minutes` 與 `concurrency` 群組(並行部署會讓兩份 artifact
    互相覆蓋)。
- **快照年紀的客戶端檢查**:`status.state` 是**匯出當下**的判定,被凍結在 JSON 裡。
  靜態頁可能被服務很久 —— 自動同步一旦停擺,三個月前的快照會永遠自稱 `fresh`。
  `dashboard.js` 改為載入時以「現在」重新檢查快照年紀,門檻取所有資料集
  `stale_after_days` 中**最嚴格的那個**(不另立憑空的數字),超過就把橫幅覆寫為 stale。
  **只覆寫 `fresh`**;`degraded` / `empty` / `stale` 比它嚴重,不得被降級。
- `docs/superpowers/specs/2026-09-10-pages-auto-sync.md`。
- `tests/scripts/test_pages_workflow.py`:以純文字解析(不引入 YAML 相依)釘住
  workflow 的**失敗行為** —— 驗證必須在上傳與部署之前、驗證步驟不得
  `continue-on-error`、staging 必須先驗證才准取代 `_site`、refresh 失敗必須讓 job
  轉紅且發生在部署之後、`permissions` 不得出現 `contents: write`。順序錯了會把未驗證
  的東西部署出去,或在同步失敗時靜默成功 —— 兩者都是無聲的錯誤。

### Verified
- 全套件 **283 tests 通過**(修改前 267,新增 16)。
- `bandit -r src scripts -ll`:Medium 0。
- `scripts/verify_dashboard.py --site docs` 通過;委入的 Pages artifact 未退化。

### 未處理(如實記錄)
- **每日排程未啟用**,`schedule:` 在 `pages.yml` 中維持註解。兩個理由:
  (1) 本次 session 的 egress 政策拒絕全部五個官方 host,**無法驗證 GitHub-hosted
  runner 能否穩定完成官方來源同步**,#31 明文要求實測證據,沒有就是沒有;
  (2) 「不得假設 DB 已存在於 runner」意味著每次全量同步,預設回溯 12 個月的 PCC
  半月檔,每日一次對政府站台是持續性負載,與 repo 既有的反爬倫理直接相關,屬維運決策。
  啟用方式:以 `workflow_dispatch` 成功跑過一次、確認耗時與負載可接受後取消註解。
  `tender_months` / `award_months` 已開為 workflow input,可先用較短視窗評估。
- `docs/data/current.json` 仍是委入的舊快照;第一次成功的自動同步會取代它,屆時矩陣的
  `freshness` / `stale_after_days` 欄位才會有值(目前顯示「—」)。

## [0.8.1] - 2026-09-09

### Changed
- **過期門檻改由 catalog 的 `update_cadence` 推導(issue #31 第 2 項)**:先前
  `export_board_data.py` 用 `DEFAULT_STALE_AFTER_DAYS = 21` 對所有來源一體適用 ——
  一份每日更新的資料集停更 20 天顯然出事了,一份年度更新的資料集停更 20 天完全正常,
  同一個門檻不可能同時對兩者成立。這與 #22 明文寫下的「不自行推論 healthy／stale
  門檻⋯需先建立每個 source 的正式 cadence 契約」直接牴觸。
  該契約自 #26 起存在(catalog 每筆 entry 的 `update_cadence`),本次讓它真的被用上。
  - **規則:門檻 = 2 × 更新週期**。漏掉一次更新是時序造成的常態;漏掉兩次代表出事了。
    `daily`→2、`weekly`→14、`monthly`→60、`quarterly`→182、`yearly`→730 天。
  - `irregular` / `unknown` **沒有週期可推導 → 沒有門檻,永遠不會被判為 stale**。
  - **catalog 新增 cadence 而未定政策時,`export_board_data` 在 import 時就失敗**。
    靜默落到「無週期」會讓一個真的有節奏的資料集永遠不被判為過期,比直接壞掉更糟。
  - 事實與政策分離:`update_cadence` 是事實(住 `catalog.py`),門檻天數是政策
    (住 `export_board_data.py`)。混在一起會讓資料集出處與看板呈現偏好綁死。
- **判定基準改為 per-dataset 的 `datasets.last_fetched_at`**,不再是 per-source 的
  `min(ingestion_runs.finished_at)`。`ingestion/pipeline.py` 對單一 ref 失敗有容錯,
  **一個 SUCCEEDED 的 run 底下可能有某個 dataset 根本沒更新到** —— 用 run 的時間會
  把這種情況掩蓋掉。`ingestion_runs` 的時間戳仍用於 degraded 判定。
- 全域 `stale` 改由逐資料集的 `freshness` 匯總:任一資料集過期則整頁過期,訊息指名
  是哪些資料集、依據什麼門檻。`freshness=unknown` **不參與**匯總。
- `--stale-after-days` 旗標的語意收斂為「非 catalog 驅動資料集(PCC)的門檻」,
  catalog 驅動者不受其影響;`--help` 已載明。

### Added
- 快照的每筆資料集新增兩個**選填**欄位(`schema_version` 維持 `"1.0"`,舊快照仍合法):
  - `stale_after_days`: `integer | null`
  - `freshness`: `enum ["fresh", "stale", "unknown"]`
- `NON_CATALOG_STALE_AFTER_DAYS` 明列非 catalog 驅動來源的門檻(目前只有 PCC 的
  21 天,沿用 P0),與既有的 `NON_CATALOG_SOURCE_URLS` 同一個模式。
- 看板資料集矩陣新增「新鮮度」欄:最新 / 過期(逾 N 天) / 無契約。
  **三種狀態都以文字呈現,不靠顏色區分**;「無契約」必須看得見 —— 渲染成空白或與
  「最新」同樣式,會讓沒有新鮮度契約的資料集被讀成已驗證為最新。
- `docs/superpowers/specs/2026-09-09-freshness-from-cadence.md`。

### Verified
- 全套件 **252 tests 通過**(修改前 237,新增 15)。
- `bandit -r src scripts -ll`:Medium 0。
- `scripts/verify_dashboard.py --site docs` 通過;委入的 Pages artifact 未退化。
- `test_old_successful_sync_is_stale` 的 fixture 一併修正:原本只往回撥
  `ingestion_runs.finished_at`、卻留著新的 `datasets.last_fetched_at`,在新語意下
  是不一致的情境。改為兩者都往回撥,並補上門檻與 freshness 的斷言。

### 未處理(如實記錄)
- `docs/data/current.json` 未重新產生(本環境無真實 DB)。新欄位為選填,舊快照仍
  合法;矩陣的新鮮度欄位會顯示「—」直到下一次真實 export。

## [0.8.0] - 2026-09-07

### Changed
- **Pages 快照的資料集矩陣改由 catalog 驅動(issue #25 Slice 3)**:`datasets` 先前是
  **固定兩個 key 的物件** —— `build_snapshot` 把 `pcc_tender` / `nhi_clinic` 連同
  `source_url` 字串寫死在 payload literal 裡,`schema-v1.json` 是
  `additionalProperties: false` + 兩者皆 required,`verify_dashboard` 逐一檢查那兩個
  名字,`_derive_status` 收硬編碼的兩元素 source 清單。
  結果是 #26 建立的 catalog 一旦啟用新資料集,Pages **看不到它**,schema 還會拒收 ——
  資料集名單有兩個真相來源,必然漂移。
  - 現在 `datasets` 由 **catalog(已啟用者)∪ 資料庫**產生。停用的候選**不發布**:
    它們尚未實查(#25 Slice 2),列在公開頁面會讓訪客誤以為已涵蓋。
  - `source_url` 只有 PCC 在 `NON_CATALOG_SOURCE_URLS` 明列(它不是 catalog 驅動的),
    其餘一律取自 catalog。同一件事不在兩個地方維護。
  - 每筆資料集另帶 catalog 出處:`title` / `collection` / `update_cadence` /
    `verified_at`。非 catalog 驅動者為 `null`,不臆造。
  - `_derive_status` 的來源清單改由「實際發布的資料集反推 `source_id`」產生,
    標籤取 `data_sources.name`(缺列則退回 `source_id`);`fresh` 訊息不再寫死「PCC／NHI」。
- **`schema-v1.json` 對資料集開放**(放寬,非破壞):`datasets` 改為
  `additionalProperties: {$ref dataset}` + `propertyNames: ^[a-z0-9_]+$`,
  `required` 由兩者縮為 `["pcc_tender"]`;`dataset.row_count` 與 `dataset.source_url`
  改為可為 null;新增選填的四個出處欄位。
  **`schema_version` 維持 `"1.0"`** —— 先前每一份合法快照仍然合法,只是多了一些
  也合法的快照。已評估過改陣列並切 v2,否決理由見設計文件。
  - 實測:委入的 `current.json` 對新 schema 0 error;加入一筆全新資料集後仍 0 error
    (即新增資料集**不需要**改 schema)。
- **`verify_dashboard.py` 只驗形狀不驗成員**:`pcc_tender` 仍必須存在且 `row_count`
  為整數(看板的摘要與表格都靠它),其餘資料集只檢查 key 命名、必填欄位齊全、
  `row_count` 為整數或 null。驗證器若知道資料集名單,每加一個資料集就要改部署閘門。
  維持 **stdlib-only**(`pages.yml` 不安裝套件)。

### Added
- 看板新增「資料集概覽」矩陣(`#dataset-matrix-body`):資料集、筆數、最後同步、
  最近一次同步狀態、更新頻率、出處驗證日、授權。
  以 `createElement` / `textContent` 渲染,不用 `innerHTML`(`verify_dashboard` 的
  靜態檢查未放寬);無 JS 時顯示明確說明列,上方 KPI 摘要仍為建置時預渲染。
- `docs/superpowers/specs/2026-09-07-pages-catalog-driven-datasets.md`:記錄本次對
  2026-09-01 P0 契約的增量調整與「放寬 v1 而非切 v2」的理由。

### Fixed
- `row_count` 缺值語意一致化:物化表不存在時快照輸出 `null`、前端顯示「—」,
  不再折成 0。「從未同步成功」與「同步成功但 0 筆」是兩件事。
- `render_dashboard` 不再假設 `nhi_clinic` 一定存在(它由 catalog 決定);
  缺席時 KPI 顯示「無資料」而非炸掉或顯示 0。

### Verified
- 全套件 **236 tests 通過**(修改前 220,新增 16:catalog 驅動的資料集 6、
  render 容錯 2、驗證器開放性 8)。
- `bandit -r src scripts -ll`:Medium 0(與修改前相同)。
- `scripts/verify_dashboard.py --site docs` 通過;委入的 Pages artifact 未退化。
- 以 `jsonschema` Draft 2020-12 實際驗證 schema 本身與委入快照(該套件只在本機
  檢查時使用,**未**加入專案相依)。

### 未處理(如實記錄)
- `docs/data/current.json` **未重新產生**。它是一次真實同步的產物,本環境沒有那個
  DB;用 fixture 重建等於用假資料覆蓋真資料。新增的出處欄位為選填,舊快照仍合法,
  矩陣的出處欄位會顯示「—」直到下一次真實 export。
- Pages 仍未自動同步(P1 範圍,見 #21)。

## [0.7.1] - 2026-09-07

### Added
- **`scripts/verify_catalog_sources.py`(issue #25 Slice 2)**:對 catalog entry 的官方端點
  實查,輸出可貼進 `catalog.py` 的 `verified_at` / `verified_note` 素材。
  把「實查」從人工看網頁寫心得,變成可重跑、只輸出**可觀察事實**的程序:
  HTTP status、content-type、byte size、列數、欄位名、natural key 欄位是否存在、
  相異鍵數、無鍵列數。
  - **只讀**:永遠不修改 `catalog.py`、不翻 `enabled`。啟用仍是人工 review 後的 PR 編輯。
  - **先驗證後請求**:entry 未通過 `validate_catalog()`(例如 host 不在
    `OFFICIAL_HOSTS`)時,連請求都不發出。
  - **欄位處理與 ingestion 一致**(strip + `column_renames`),否則報告出來的欄位名
    不是實際會用的那組。
  - **失敗一律明說**:非 200、空回應、只有 header、UTF-8 解不開、natural key 欄位不存在
    都是失敗並保留已觀察到的事實;離開碼非零。不以 `errors="replace"` 掩蓋解碼問題。
  - **回應大小上限**(預設 64 MiB):超過即中止,不截斷後假裝解析成功。
  - 抓取例外帶上例外類別:`ProxyError: 403 Forbidden`(本地 egress 政策)與上游限流的
    403 都顯示同一句話,分不出來就會誤判資料源已失效。
  - 第二個用途是**上游漂移檢查**:`--enabled-only` 對已啟用的資料集重跑,欄位或
    natural key 不再成立時離開碼非零。
- **`.github/workflows/verify-catalog.yml`**:`workflow_dispatch` 手動觸發上述 script。
  **刻意不加 `schedule`** —— 定期對官方端點發流量是持續性外部負載,屬維運決策,
  留給維護者拍板。

### Verified
- 全套件 **220 tests 通過**(修改前 203,新增 17:成功路徑事實 3、失敗路徑 6、
  白名單先於請求 1、大小上限 2、note 產生 2、entry 選取 3)。
- `bandit -r src scripts -ll`:Medium 0(與修改前相同)。
- 成功路徑以注入 fetcher 的 fixture 驗證;失敗路徑以本環境實際的 proxy 403 驗證
  (離開碼 1、三筆 entry 皆標示 `ProxyError: 403 Forbidden`)。
- **未驗證事項(如實記錄)**:本次 session 的 egress 政策拒絕全部五個官方 host
  (`info.nhi.gov.tw`、`data.gov.tw`、`www.mohw.gov.tw`、`dep.mohw.gov.tw`、
  `www.nhi.gov.tw`,CONNECT 皆回 403),因此 **Slice 2 的實查本身尚未執行**,
  candidate 仍為 `enabled=False`。本次交付的是讓實查可被執行且結果可稽核的工具,
  不是實查結果。

## [0.7.0] - 2026-09-07

### Added
- **資料集目錄 `catalog.py`(issue #25 Slice 1)**:把「要同步哪些政府開放資料」從
  `cli.py` 的模組層常數,改成帶出處的宣告式資料。
  每筆 `CatalogEntry` 攜帶 `update_cadence`、`landing_url`、`verified_at`、
  `verified_note`、`enabled`,擴充一個資料集從此是「加一筆資料 + 實查後翻一個
  flag」,而不是改 code。
  以 import 時執行的 `validate_catalog()` 強制四條不變量:
  - **驗證閘門**:`enabled=True` 必須有 `verified_at`(YYYY-MM-DD)與 `verified_note`。
    未實查的候選可以進目錄,但只能是 `enabled=False` —— 目錄有能力誠實表達
    「還沒驗證」,不需要靠猜測填空。
  - **官方網域白名單**:下載與說明頁 URL 的 host 必須在 `OFFICIAL_HOSTS`。entry 是
    資料;不設限等於「新增一筆資料 = 新增一個對任意主機發請求的能力」。
  - **kind 與欄位一致**:`nhi_api` 只能有 `r_id`、`static_csv` 只能有 `urls`。
  - **`r_id` 字元集**:`r_id` 會被字串插值進 query string
    (`NhiApiAdapter.discover`),限制為 `[A-Za-z0-9]+(-[A-Za-z0-9]+)+` 才能保證它不會
    挾帶 `&` / `?` / `#` / 空白去改寫 URL 的其他參數。
- `list_datasets` / `get_dataset` 新增 `last_fetched_at` 與 `row_count`(additive,
  既有欄位與參數簽章不變)。先前兩者都不回新鮮度,呼叫端無從分辨「查無資料」與
  「這個資料集根本沒同步成功」—— 這與專案自己在 #21 寫下的「資料新鮮度與失敗
  狀態也是資料」矛盾。
  - 讀模型以新的 `DatasetStatus` DTO 承載,刻意不塞進 ingestion 端的 `DatasetMeta`。
  - **物化表不存在時 `row_count` 為 `null`,不折成 0**:「從未同步成功」與
    「同步成功但 0 筆」是兩件事,不可混為一談。
- `SqliteRepository.dataset_status()` / `list_dataset_status()`。
- `NhiApiAdapter` 的 `NHI_API_BASE` 由私有改為公開常數,讓 catalog 能組出
  「實際會被抓取的 URL」並對它做白名單檢查。

### Fixed
- **`StaticCsvAdapter` 從未被實例化**:該 adapter 自 0.2.0 起已實作、已由
  `adapters/__init__.py` 匯出、有 5 項測試,但 `cli.py:_sync()` 只組
  `NhiApiAdapter` + `PccTenderAdapter` —— `gov-static` 來源永遠不會註冊,靜態 CSV
  實際貢獻 0 筆。現改由 `build_adapters()` 依 catalog 組裝並接上。
  - 目前 catalog 沒有任何 enabled 的 static entry(見下方「未驗證事項」),
    故本次仍不會發出靜態 CSV 請求;接線本身以注入 spec 的測試釘住。
  - `build_adapters()` 對沒有 enabled entry 的 kind 不建立 adapter —— 不註冊一個
    永遠抓 0 筆的來源。

### Verified
- 全套件 **203 tests 通過**(修改前 159,新增 44:catalog 不變量 28、
  來源組裝 6、dataset 新鮮度 6、service additive 欄位 4)。
- `bandit -r src scripts -ll`:Medium 0(與修改前逐項相同:Low 3 / Medium 0)。
- `pip-audit`:專案相依無弱點;僅回報執行環境 venv bootstrap 的 `pip 24.0` /
  `setuptools`,不屬任何專案相依(CI 在 3.12 且先升級 pip,不會出現)。
- **未驗證事項(如實記錄)**:本次 session 的 egress 政策拒絕
  `info.nhi.gov.tw:443`(proxy 回 403),因此**沒有**對任何官方端點做實查。
  - `nhi-clinic` 的 `verified_at=2026-06-10` 沿用 repo 既有的實查註記,非本次驗證。
  - `nhi-hospital-district`(D21003-003)、`nhi-hospital-bed-ratio`(D02001-015)
    的 rId 取自 `tests/adapters/test_nhi.py`,repo 內無實查日期,故以
    `enabled=False`、`verified_at=None` 進目錄,不會被同步。
  - `StaticCsvAdapter` 原本預計接的 `data.gov.tw` / `mohw.gov.tw` distribution URL
    在 repo 內只有 `example.test` 佔位值,無可驗證的真實 URL,故未加入目錄。

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
