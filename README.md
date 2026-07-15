# healthcare-opendata-mcp 🏥

> 官方開放資料 → 可查詢 MCP 介面，讓 AI agent 不必直接處理分散的政府資料來源。

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![FastMCP](https://img.shields.io/badge/built%20with-FastMCP-orange)](https://github.com/jlowin/fastmcp)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

`healthcare-opendata-mcp`（命令名稱：`hcmcp`）是一個自建、可部署的 MCP server：把政府電子採購網與健保署開放資料同步到 SQLite，再以穩定的 MCP tools 提供給 Claude 或其他 agent 查詢。

專案保留 Twinkle Hub `query_rows` 的 SQL 式查詢模式，但資料來源、同步流程與儲存層都由本專案自行掌握，不依賴第三方聚合服務。

[GitHub Pages 導覽](https://trionnemesis.github.io/healthcare-opendata-mcp/) · [GitHub repository](https://github.com/trionnemesis/healthcare-opendata-mcp)

## Contents

- [Why](#why)
- [How it works](#how-it-works)
- [Install](#install)
- [What it provides](#what-it-provides)
- [Querying](#querying)
- [Trust & security](#trust--security)
- [HTTP & GKE](#http--gke)
- [Development](#development)
- [Scope & limits](#scope--limits)
- [Related projects](#related-projects)
- [License](#license)

## Why

AI agent 要查政府資料時，真正的摩擦通常不在模型，而在資料入口：來源分散、格式不同、欄位缺漏，而且外部聚合服務的政策或可用性可能改變。

| Problem | What hcmcp does |
|---|---|
| 政府電子採購網與健保資料各自分散 | 以 `SourceAdapter` 統一 discover → fetch → normalize → upsert 流程 |
| 半月 XML、CSV API、標案明細頁格式不同 | 正規化成可查詢的 dataset 與 schema |
| 標案 open data 缺少截標、開標、預算 | 以 `get_tender_detail` 按需讀取官方明細頁補足資訊 |
| 第三方資料入口不可控 | 自行同步、儲存與提供 MCP 介面 |

## How it works

```mermaid
flowchart LR
    PCC[政府電子採購網<br/>半月 XML] --> SYNC[hcmcp-sync<br/>fetch / normalize / upsert]
    NHI[健保署開放平台<br/>CSV API] --> SYNC
    SYNC --> DB[(SQLite<br/>~/.hcmcp/hcmcp.db)]
    DB --> SERVER[hcmcp<br/>唯讀 MCP server]
    SERVER --> AGENT[Claude / Agent]
    AGENT -->|按需補查| DETAIL[get_tender_detail]
    DETAIL --> PCCDETAIL[政府採購網<br/>標案明細頁]
```

The project keeps ingestion and querying separate:

1. `hcmcp-sync` pulls official sources and writes the shared SQLite database.
2. `hcmcp` opens the same database in the query path and exposes MCP tools.
3. `list_datasets` → `get_dataset` → `query_rows` is the recommended discovery flow.
4. `get_tender_detail` performs an on-demand lookup when a tender needs deadline, opening time, or budget details.

## Install

```bash
git clone https://github.com/trionnemesis/healthcare-opendata-mcp.git
cd healthcare-opendata-mcp

python3.11 -m venv .venv
.venv/bin/python -m pip install -e .

# 建立或更新預設 DB：~/.hcmcp/hcmcp.db
.venv/bin/hcmcp-sync
```

加入 Claude Code：

```bash
claude mcp add hcmcp -- /absolute/path/to/healthcare-opendata-mcp/.venv/bin/hcmcp
```

### Database path

`hcmcp-sync` 與 `hcmcp` server 共用同一個預設 DB：`~/.hcmcp/hcmcp.db`。如果要改路徑，兩個 process 都必須使用相同的 `HCMCP_DB`；sync 也可以使用 `--db`：

```bash
HCMCP_DB=/path/to/hcmcp.db .venv/bin/hcmcp-sync
HCMCP_DB=/path/to/hcmcp.db .venv/bin/hcmcp
```

否則可能出現「同步成功，但 server 查不到資料」的路徑漂移問題。

## What it provides

### Datasets

目前 CLI 預設同步兩個資料集：

| Dataset | Scope | Official source | Update path |
|---|---|---|---|
| `pcc-tender` | 衛生福利部轄下機關的資訊勞務相關標案 | [政府電子採購網](https://web.pcc.gov.tw/) | 半月 XML；明細欄位按需 enrich |
| `nhi-clinic` | 健保特約醫事機構－診所 | [健保署資料開放平台](https://info.nhi.gov.tw/) | CSV API，每日更新 |

### MCP tools

| Tool | Purpose |
|---|---|
| `list_sources` | 列出資料來源、取得策略與最後抓取時間 |
| `list_datasets` | 列出可查詢資料集與欄位 |
| `get_dataset` | 取得 dataset metadata、schema 與可選的抽樣資料列 |
| `query_rows` | 對單一 dataset 做 SELECT-only 篩選、排序與聚合 |
| `search_records` | 跨資料集關鍵字搜尋 |
| `get_record` | 以 `(dataset_id, natural_key)` 取得單筆完整資料 |
| `get_vendor_stats` | 依得標次數與金額整理廠商排名 |
| `get_tender_detail` | 即時取得標案明細的截標、開標、預算與採購屬性 |

## Querying

先看資料集與 schema，再執行查詢：

```python
list_datasets()
get_dataset(dataset_id="pcc-tender", sample_rows=5)
```

`query_rows` 保留 Twinkle 相容的 SQL-style 查詢介面，支援欄位選取、`WHERE`、`GROUP BY`、排序與聚合：

```python
query_rows(
    dataset_id="pcc-tender",
    columns=[
        "agency",
        "COUNT(*) AS n",
        "SUM(CAST(award_price AS INTEGER)) AS total",
    ],
    where="announcement_type='決標公告' AND date >= '2025-01-01'",
    group_by=["agency"],
    order_by="total DESC",
    limit=50,
)
```

SQLite 使用 `LIKE`，不使用 PostgreSQL 的 `ILIKE`；金額欄位需要依資料內容使用 `CAST(... AS INTEGER)`。

## Trust & security

`query_rows` 接受 SQL 片段，因此實作了兩層防禦：

- **語法層**：只允許單一 `SELECT`；拒絕多語句、註解、`PRAGMA`、`ATTACH`、DML、DDL 與危險 keyword，並將 limit 硬上限設為 400。
- **執行層**：使用 SQLite read-only connection 與 authorizer allowlist，只允許讀取單一物化資料表；另有 VM 步數上限。

這是查詢執行安全邊界，不是使用者認證層。MCP server 本身沒有 authentication；HTTP/GKE 部署應放在內部網路，或在前方配置 IAP、service mesh mTLS 等存取控制。

## HTTP & GKE

本機或容器可使用 MCP streamable HTTP：

```bash
HCMCP_TRANSPORT=http HCMCP_PORT=8000 .venv/bin/hcmcp

curl http://localhost:8000/healthz
# {"status":"ok"}

claude mcp add --transport http hcmcp http://<host>:8000/mcp
```

`HCMCP_TRANSPORT=sse` 僅保留給既有部署相容；新網路部署使用 `http`。GKE 架構、Workload Identity、CronJob、GCS DB artifact 與 Kubernetes manifests 請見 [deploy/README.md](deploy/README.md)。

## Development

```bash
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
```

主要程式分層如下：

```text
src/health_opendata_mcp/
├── adapters/      官方來源 adapter 與 HTTP/CSV/PCC parser
├── domain/        query_guard 等純函式安全規則
├── ingestion/     discover → fetch → normalize → upsert pipeline
├── repository/    SQLite schema、物化表與唯讀 query executor
└── mcp_server/    FastMCP tools、transport 與 QueryService
```

新增健保資料集只需在 `cli.py` 的 `NHI_DATASETS` 登錄 `rId`；新增資料來源則實作 `SourceAdapter` 的 `discover`、`fetch`、`normalize`。

## Scope & limits

- 預設同步範圍刻意收斂為衛福部資訊勞務相關標案與健保診所，不是完整的政府採購或醫療資料目錄。
- `get_tender_detail` 依賴政府電子採購網即時明細頁；舊案下架、網站維護或限流時，工具可能回傳錯誤，應稍後重試。
- HTTP server 預設沒有 authentication；公開暴露前必須自行配置網路層存取控制。
- 資料依官方來源更新節奏而變動；本 repo 不把同步後的資料快照提交進 Git。

## Related projects

[g0VMCP](https://github.com/trionnemesis/g0VMCP) — 衛福部標案的生命週期與明細加值 MCP，處理招標 → 更正 → 決標狀態與深度標案情報。兩個專案刻意零耦合：本專案提供 Twinkle 相容的扁平列查詢，g0VMCP 提供深度標案資訊；PCC XML parser 以純函式方式 vendored 自 g0VMCP。

## License

[MIT](./LICENSE) — 資料依[政府資料開放授權條款](https://data.gov.tw/license)使用。
