"""QueryService — MCP 工具的查詢服務(唯讀讀模型)。

錯誤映射策略:領域/執行層例外統一轉 ValueError,訊息對使用者安全
(不洩漏 SQL 細節之外的 schema 資訊),FastMCP 會轉為 tool error。
"""
from __future__ import annotations

from typing import Any

from health_opendata_mcp.contracts import DatasetNotFoundError, QueryResult
from health_opendata_mcp.domain.query_guard import DEFAULT_LIMIT, QueryValidationError
from health_opendata_mcp.repository.query_executor import QueryDeniedError
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository


def _result_dict(result: QueryResult) -> dict[str, Any]:
    return {
        "columns": list(result.columns),
        "rows": [list(r) for r in result.rows],
        "truncated": result.truncated,
    }


class QueryService:
    def __init__(self, repo: SqliteRepository) -> None:
        self._repo = repo

    async def list_sources(self) -> list[dict[str, Any]]:
        return [
            {
                "id": s.id,
                "name": s.name,
                "platform": s.platform,
                "access_strategy": s.access_strategy.value,
                "enabled": s.enabled,
                "last_fetched_at": (
                    s.last_fetched_at.isoformat() if s.last_fetched_at else None
                ),
            }
            for s in await self._repo.list_sources()
        ]

    async def list_datasets(self) -> list[dict[str, Any]]:
        return [
            {
                "id": d.id,
                "title": d.title,
                "source_id": d.source_id,
                "collection": d.collection,
                "columns": [c.name for c in d.columns],
            }
            for d in await self._repo.list_datasets()
        ]

    async def get_dataset(
        self, dataset_id: str, sample_rows: int = 0
    ) -> dict[str, Any]:
        meta = await self._repo.get_dataset(dataset_id)
        if meta is None:
            raise ValueError(f"dataset 不存在: {dataset_id}")
        info: dict[str, Any] = {
            "id": meta.id,
            "title": meta.title,
            "source_id": meta.source_id,
            "collection": meta.collection,
            "license": meta.license,
            "schema": [{"name": c.name, "type": c.type} for c in meta.columns],
        }
        if sample_rows > 0:
            info["sample"] = _result_dict(
                await self._repo.sample_rows(dataset_id, sample_rows)
            )
        return info

    async def query_rows(
        self,
        dataset_id: str,
        *,
        columns: list[str] | None = None,
        where: str | None = None,
        group_by: list[str] | None = None,
        order_by: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        try:
            result = await self._repo.query_rows(
                dataset_id,
                columns=columns,
                where=where,
                group_by=group_by,
                order_by=order_by,
                limit=limit,
            )
        except DatasetNotFoundError as exc:
            raise ValueError(
                f"dataset 不存在: {dataset_id}(可用 list_datasets 查詢)"
            ) from exc
        except (QueryValidationError, QueryDeniedError) as exc:
            raise ValueError(str(exc)) from exc
        return _result_dict(result)

    async def search_records(
        self, keyword: str, dataset_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        return await self._repo.search_records(keyword, dataset_id, limit)

    async def get_record(self, dataset_id: str, natural_key: str) -> dict[str, Any]:
        rec = await self._repo.get_record(dataset_id, natural_key)
        if rec is None:
            raise ValueError(f"record 不存在: {dataset_id}/{natural_key}")
        return rec
