# Healthcare OpenData MCP (hcmcp)

> 衛生福利部資訊勞務標案 × 健保診所開放資料 MCP — 對齊 Twinkle Hub `query_rows`,完全自主資料來源

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![FastMCP](https://img.shields.io/badge/built%20with-FastMCP-orange)](https://github.com/jlowin/fastmcp)

因 hub.twinkleai.tw 政策異動停用而自建的替代方案:以 [FastMCP](https://github.com/jlowin/fastmcp) 封裝,提供 Twinkle Hub 相容的 SQL 式查詢(`query_rows`),**不依賴任何第三方聚合服務**。

## 資料來源(全部一級官方來源)

範圍聚焦(2026-06):**衛生福利部轄下機關的資訊勞務相關標案** + **健保診所**——案量精簡到可逐案 enrich 標案截標/開標/預算。

| 來源 | 資料集 | 取得形式 |
|------|--------|---------|
| 政府電子採購網 `web.pcc.gov.tw` | `pcc-tender`(衛福部轄下機關 · 資訊勞務,twinkle pcc-tender 欄位相容,含 enrich 的 `bid_deadline`/`open_date`/`budget`) | 半月公開 XML + 明細頁 enrich |
| 健保署資料開放平台 `info.nhi.gov.tw` | `nhi-clinic`(健保特約診所) | CSV API,每日更新、免 key |

## 快速開始

```bash
# 安裝
git clone <repo-url> && cd healthcare-opendata-mcp
python3.11 -m venv .venv && .venv/bin/pip install -e .

# 同步資料(預設 ~/.hcmcp/hcmcp.db;決標 12 月、招標 12 月)
.venv/bin/hcmcp-sync

# 加入 Claude Code
claude mcp add hcmcp -- /path/to/.venv/bin/hcmcp
```

HTTP 模式(團隊共用 / 容器部署,MCP streamable HTTP):

```bash
HCMCP_TRANSPORT=http HCMCP_PORT=8000 hcmcp
# 連線:claude mcp add --transport http hcmcp http://<host>:8000/mcp
# probe:GET /healthz
```

(`HCMCP_TRANSPORT=sse` 保留給既有部署相容;MCP spec 已 deprecate SSE,新部署用 http)

GKE 部署(Dockerfile + K8s manifests):見 [deploy/README.md](deploy/README.md)。

## MCP 工具

| 工具 | 說明 |
|------|------|
| `list_datasets` | 列出可查詢資料集 |
| `get_dataset(dataset_id, sample_rows)` | metadata + schema + 抽樣資料列 |
| `query_rows(dataset_id, where, columns, group_by, order_by, limit)` | **SQL 式查詢與聚合**(對齊 twinkle) |
| `search_records(keyword, dataset_id)` | 跨資料集關鍵字搜尋 |
| `get_record(dataset_id, natural_key)` | 取單筆完整資料 |
| `get_tender_detail(job_number)` | 即時抓 web.pcc 標案明細,補半月 open data 缺的**截標/開標/預算**(招標評估用) |
| `list_sources` | 列出資料來源與最後抓取時間 |

`query_rows` 範例(twinkle 查詢模式直接沿用):

```python
query_rows(
    dataset_id="pcc-tender-mohw",
    columns=["agency", "COUNT(*) AS n", "SUM(CAST(award_price AS INTEGER)) AS total"],
    where="announcement_type='決標公告' AND date >= '2025-01-01'",
    group_by=["agency"],
    order_by="total DESC",
)
```

### SQL 安全(OWASP A03)

`query_rows` 接受原始 SQL 片段,以雙層防禦保護:

1. **語法層**(`query_guard`):僅單一 SELECT;拒絕 `;` 多語句、註解、PRAGMA/ATTACH/DML/DDL;limit 硬上限 400
2. **執行層**(`query_executor`):`mode=ro` 唯讀連線 + sqlite authorizer 白名單(僅允許讀單一物化表,跨表子查詢一律拒絕)+ VM 步數上限

## 架構

```
src/health_opendata_mcp/
├── contracts.py      跨層 DTO、Enum、Protocol(DI 邊界)
├── domain/           query_guard(SQL 安全驗證純函式)
├── adapters/         SourceAdapter 實作(可插拔多來源)
│   ├── nhi.py            健保署 CSV API
│   ├── pcc_tender.py     PCC 半月 XML(vendored 解析純函式)
│   └── _http.py          共用 async HTTP getter(DI 預設值)
├── ingestion/        pipeline(discover→fetch→normalize→upsert,容錯)
├── repository/       SQLite 基底表 + ds_{dataset_id} 物化表 + 唯讀 executor
└── mcp_server/       FastMCP 6 工具 + QueryService

spec/
├── erm.dbml          領域模型(DataSource/Dataset/Record/IngestionRun + 不變量)
└── features/         Gherkin BDD 行為規格(5 features)
```

**資料流**:`hcmcp-sync`(ETL,可排程)→ SQLite(records 正準 + 物化查詢表同交易)→ `hcmcp`(唯讀 MCP server)→ Claude/Agent。抓取與查詢分離,headless browser 無必要場景(介面保留、預設停用)。

**新增資料集**:健保署資料集只需在 `cli.py` 的 `NHI_DATASETS` 登錄 `rId`(從 data.gov.tw 對應資料集頁取得);新資料來源則實作 `SourceAdapter` Protocol(`discover/fetch/normalize` 三方法)。

## 開發

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest        # 75 tests
```

完整 SDD 規格(評估矩陣 / BDD / DDD / 架構 / Twinkle 功能對齊)維護於 Notion「醫療健保開放資料 MCP」條目。

## 相關專案

[g0VMCP](https://github.com/trionnemesis/g0VMCP) — 衛福部標案的**生命週期/明細加值** MCP(招標→更正→決標狀態機、開標時間/預算 enrich)。兩專案刻意零耦合:hcmcp 提供 twinkle 相容的扁平列查詢,g0VMCP 提供深度標案情報;PCC XML 解析純函式 vendored 自 g0VMCP。

## License

MIT — 資料依[政府資料開放授權條款](https://data.gov.tw/license)使用。
