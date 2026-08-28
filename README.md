# healthcare-opendata-mcp 🏥

> 官方開放資料 → 可查詢 MCP 介面，讓 AI agent 不必直接處理分散的政府資料來源。

Self-hosted MCP server that syncs Taiwan government procurement (PCC) and National Health Insurance (NHI) open data into a local SQLite database, then exposes it through read-only MCP tools. A SELECT-only query guard (syntax allowlist plus a read-only SQLite authorizer) keeps the Twinkle-compatible `query_rows` interface safe for agent-driven querying.

[![CI](https://github.com/trionnemesis/healthcare-opendata-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/trionnemesis/healthcare-opendata-mcp/actions/workflows/ci.yml)
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

### Sync options

```bash
.venv/bin/hcmcp-sync --db /path/to/hcmcp.db --tender-months 12 --award-months 12
```

| Flag | Default | Purpose |
|---|---|---|
| `--db` | `HCMCP_DB` 或 `~/.hcmcp/hcmcp.db` | 寫入的 SQLite 路徑 |
| `--tender-months` | `12` | 招標回溯月數（PCC 站上實際可回溯約 6 個月） |
| `--award-months` | `12` | 決標回溯月數 |

### Environment variables

| Variable | Default | Used by | Purpose |
|---|---|---|---|
| `HCMCP_DB` | `~/.hcmcp/hcmcp.db` | sync + server | SQLite 路徑；兩個 process 必須一致 |
| `HCMCP_TRANSPORT` | `stdio` | server | `stdio` / `http` / `sse`（僅相容既有部署） |
| `HCMCP_HOST` | `0.0.0.0` | server（http/sse） | 監聽位址；server 無 authentication，公開網段請改綁內網位址並在前方配置存取控制 |
| `HCMCP_PORT` | `8000` | server（http/sse） | 監聽 port |

`hcmcp-sync` 與 `hcmcp` server 共用同一個預設 DB：`~/.hcmcp/hcmcp.db`。如果要改路徑，兩個 process 都必須使用相同的 `HCMCP_DB`；sync 也可以使用 `--db`：

```bash
HCMCP_DB=/path/to/hcmcp.db .venv/bin/hcmcp-sync
HCMCP_DB=/path/to/hcmcp.db .venv/bin/hcmcp
```

否則可能出現「同步成功，但 server 查不到資料」的路徑漂移問題。server 啟動時若 DB 沒有任何資料集會直接以錯誤訊息結束，提醒先跑 `hcmcp-sync`。

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

寫入路徑（sync）另有一層 ingestion 防禦：PCC 半月 XML 一律以 `defusedxml` 解析，DTD 與 entity 在 parser 層就被拒絕（CWE-611/776，不使用可被 padding 繞過的字串前綴檢查），並保留 20M 字元的輸入上限。

這是查詢執行安全邊界，不是使用者認證層。MCP server 本身沒有 authentication；HTTP/GKE 部署應放在內部網路，或在前方配置 IAP、service mesh mTLS 等存取控制。

## HTTP & GKE

本機或容器可使用 MCP streamable HTTP：

```bash
HCMCP_TRANSPORT=http HCMCP_PORT=8000 .venv/bin/hcmcp

curl http://localhost:8000/healthz
# {"status":"ok"}

claude mcp add --transport http hcmcp http://<host>:8000/mcp
```

`HCMCP_TRANSPORT=sse` 僅保留給既有部署相容；新網路部署使用 `http`。預設監聽 `0.0.0.0:8000`，容器外執行時可用 `HCMCP_HOST` 收斂綁定位址。

GKE 架構、Workload Identity、CronJob、GCS DB artifact 與 Kubernetes manifests 請見 [deploy/README.md](deploy/README.md)。DB 以不可變唯讀 artifact 形式從 GCS 拉進各 pod 的 emptyDir，因此 replica 可自由水平擴展；manifests 依 Pod Security Standards *restricted* 設定 `runAsNonRoot`、`allowPrivilegeEscalation: false`、`capabilities.drop: ["ALL"]` 與 seccomp `RuntimeDefault`。

## Development

```bash
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m bandit -r src -ll        # SAST，與 CI 同門檻（Medium 以上失敗）
.venv/bin/python -m pip_audit --skip-editable # 依賴弱點掃描
```

CI（`.github/workflows/ci.yml`）在 push 與 pull request 跑相同三項：pytest（Python 3.11 / 3.12）、bandit、pip-audit。

主要程式分層如下：

```text
src/health_opendata_mcp/
├── adapters/      官方來源 adapter 與 HTTP/CSV/PCC parser
├── domain/        query_guard 等純函式安全規則
├── ingestion/     discover → fetch → normalize → upsert pipeline
├── repository/    SQLite schema、物化表與唯讀 query executor
└── mcp_server/    FastMCP tools、transport 與 QueryService
```

新增健保資料集只需在 `cli.py` 的 `NHI_DATASETS` 登錄 `rId`；新增資料來源則實作 `SourceAdapter` 的 `discover`、`fetch`、`normalize`。標案的資訊勞務主題篩選由 `cli.py` 的 `IT_INCLUDE` / `IT_EXCLUDE` 關鍵字決定。

行為契約以 Gherkin 記錄在 `spec/features/`（ingestion、query-rows、query-tools、source-registration、headless-fallback），資料模型見 `spec/erm.dbml`。

### Maintenance scripts

| Script | Purpose |
|---|---|
| `scripts/enrich_bid_deadline.py` | 對近期、IT 類、尚未 enrich 且尚未決標的招標公告逐案補截標/開標/預算（限量 `--limit` + 節流 `--throttle`，被封鎖即停） |
| `scripts/export_board_data.py` | 匯出看板用的 `data.js` 快照 |
| `scripts/prune_local_db.py` | 清除超出目前同步範圍的舊資料（預設 dry-run，`--apply` 才寫入） |

`enrich_bid_deadline.py` 的候選條件為：`announcement_type='招標公告'`、`date` 在區間內、`bid_deadline` 為空、標題屬 IT 類，且同 `job_number` 尚無決標公告。決標與招標是兩筆獨立 record，只看招標那筆看不出案子已結束，因此另行比對決標的 `job_number` 集合，避免已決標的舊案佔用有限的明細頁請求額度、排擠仍可投標的新案。

## Scope & limits

- 預設同步範圍刻意收斂為衛福部資訊勞務相關標案與健保診所，不是完整的政府採購或醫療資料目錄。
- `get_tender_detail` 依賴政府電子採購網即時明細頁；舊案下架、網站維護或限流時，工具可能回傳錯誤，應稍後重試。
- HTTP server 預設沒有 authentication；公開暴露前必須自行配置網路層存取控制。
- 資料依官方來源更新節奏而變動；本 repo 不把同步後的資料快照提交進 Git。

## Related projects

[g0VMCP](https://github.com/trionnemesis/g0VMCP) — 衛福部標案的生命週期與明細加值 MCP，處理招標 → 更正 → 決標狀態與深度標案情報。兩個專案刻意零耦合：本專案提供 Twinkle 相容的扁平列查詢，g0VMCP 提供深度標案資訊；PCC XML parser 以純函式方式 vendored 自 g0VMCP。

[opendataCampus-MCP](https://github.com/trionnemesis/opendataCampus-MCP) — 教育資源導航 MCP，以 TWCampus 為目錄入口路由至台灣官方教育平台。與本專案同屬「官方開放資料 → 可查詢 MCP 介面」系列，但服務網域為教育資源而非採購／健保。

## License

[MIT](./LICENSE) — 資料依[政府資料開放授權條款](https://data.gov.tw/license)使用。
