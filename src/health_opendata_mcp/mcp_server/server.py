"""FastMCP server — 對外 6 工具,對齊 Twinkle Hub 查詢面。"""
from __future__ import annotations

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from health_opendata_mcp.adapters.pcc_detail import PccDetailEnricher
from health_opendata_mcp.mcp_server.service import QueryService
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository

_INSTRUCTIONS = """\
醫療健保開放資料 MCP — 健保署開放資料 × 政府採購標案(twinkle pcc-tender 相容)。
查詢入口:list_datasets 看可用資料集 → get_dataset(sample_rows=5) 看欄位與樣本
→ query_rows 做 SQL 式篩選/聚合(SELECT-only)。
標案資料集:pcc-tender(全機關)、pcc-tender-mohw(衛福部子集),欄位對齊
twinkle pcc-tender(date/announcement_type/title/agency/job_number/companies/
award_price 等;金額用 CAST(award_price AS INTEGER);SQLite 用 LIKE,無 ILIKE)。
招標案的截標/開標/預算半月 open data 沒有,需 get_tender_detail(job_number)
即時抓明細頁補(開標時間=實際投標 deadline)。
"""


def build_server(
    repo: SqliteRepository, enricher: PccDetailEnricher | None = None
) -> FastMCP:
    mcp = FastMCP("hcmcp", instructions=_INSTRUCTIONS)
    service = QueryService(repo, enricher)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        # K8s readiness/liveness probe;啟動護欄已保證 DB 非空,這裡只確認 process 活著
        return JSONResponse({"status": "ok"})

    @mcp.tool()
    async def list_sources() -> list[dict]:
        """列出已註冊資料來源(id、平台、取得策略、最後抓取時間)。"""
        return await service.list_sources()

    @mcp.tool()
    async def list_datasets() -> list[dict]:
        """列出可查詢的資料集(id、標題、collection、欄位名)。"""
        return await service.list_datasets()

    @mcp.tool()
    async def get_dataset(dataset_id: str, sample_rows: int = 0) -> dict:
        """取得資料集 metadata 與欄位 schema;sample_rows>0 時附抽樣資料列。"""
        return await service.get_dataset(dataset_id, sample_rows)

    @mcp.tool()
    async def query_rows(
        dataset_id: str,
        where: str | None = None,
        columns: list[str] | None = None,
        group_by: list[str] | None = None,
        order_by: str | None = None,
        limit: int = 50,
    ) -> dict:
        """SQL 式查詢單一資料集(SELECT-only,limit 上限 400)。

        columns 可含聚合,如 ["agency", "SUM(CAST(award_price AS INTEGER)) AS total"];
        where 為 SQL WHERE 片段,如 "announcement_type='決標公告' AND date >= '2025-01-01'"。
        """
        return await service.query_rows(
            dataset_id,
            columns=columns,
            where=where,
            group_by=group_by,
            order_by=order_by,
            limit=limit,
        )

    @mcp.tool()
    async def search_records(
        keyword: str, dataset_id: str | None = None, limit: int = 20
    ) -> list[dict]:
        """跨資料集關鍵字搜尋(payload 全文 LIKE),回傳標註資料集的記錄(limit 上限 400)。"""
        return await service.search_records(keyword, dataset_id, limit)

    @mcp.tool()
    async def get_record(dataset_id: str, natural_key: str) -> dict:
        """以 (dataset_id, natural_key) 取單筆完整資料。"""
        return await service.get_record(dataset_id, natural_key)

    @mcp.tool()
    async def get_tender_detail(job_number: str) -> dict:
        """即時抓政府電子採購網標案明細,補半月 open data 缺的加值欄位。

        以標案案號(job_number,即 pcc-tender 的 job_number)查 web.pcc 明細頁,
        回 bid_deadline(截止投標)、open_date(開標時間)、budget(預算金額)、
        title、agency、procurement_attr。招標階段評估投標時用;PCC 維護/限流
        時會回錯誤,稍後再試。
        """
        return await service.get_tender_detail(job_number)

    return mcp
