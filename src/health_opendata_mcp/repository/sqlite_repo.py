"""SqliteRepository — 基底表 + 物化表的持久化(aiosqlite)。

不變量(spec/erm.dbml):
- #1 Record 以 (dataset_id, natural_key) 唯一,upsert 冪等
- #4 物化表名由 dataset_id 白名單映射(datasets 表存在 = 白名單成員)
- #5 物化表與 records 同一交易同步更新
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from health_opendata_mcp.contracts import (
    AccessStrategy,
    ColumnSpec,
    DatasetMeta,
    DatasetNotFoundError,
    NormalizedBatch,
    QueryResult,
    RunStatus,
    SourceInfo,
)
from health_opendata_mcp.domain.query_guard import (
    DEFAULT_LIMIT,
    build_select,
    normalize_limit,
)
from health_opendata_mcp.repository.query_executor import execute_readonly
from health_opendata_mcp.repository.schema import BASE_SCHEMA

_COLUMN_NAME_RE = re.compile(r'^[^"\x00-\x1f]+$')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteRepository:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(BASE_SCHEMA)
            await db.commit()

    # ------------------------------------------------------------------
    # 白名單映射
    # ------------------------------------------------------------------
    @staticmethod
    def materialized_table(dataset_id: str) -> str:
        """dataset_id → 物化表名。僅供已存在於 datasets 表的 id 呼叫。"""
        return "ds_" + re.sub(r"[^a-z0-9_]", "_", dataset_id.lower())

    # ------------------------------------------------------------------
    # 來源
    # ------------------------------------------------------------------
    async def register_source(
        self, info: SourceInfo, config: dict[str, Any] | None = None
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO data_sources"
                " (id, name, platform, access_strategy, config, enabled)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    info.id,
                    info.name,
                    info.platform,
                    info.access_strategy.value,
                    json.dumps(config or {}, ensure_ascii=False),
                    1 if info.enabled else 0,
                ),
            )
            await db.commit()

    async def list_sources(self) -> list[SourceInfo]:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT s.id, s.name, s.platform, s.access_strategy, s.enabled,"
                " (SELECT MAX(d.last_fetched_at) FROM datasets d"
                "  WHERE d.source_id = s.id)"
                " FROM data_sources s ORDER BY s.id"
            )
            rows = await cur.fetchall()
        return [
            SourceInfo(
                id=r[0],
                name=r[1],
                platform=r[2],
                access_strategy=AccessStrategy(r[3]),
                enabled=bool(r[4]),
                last_fetched_at=datetime.fromisoformat(r[5]) if r[5] else None,
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # upsert(不變量 #1 #5)
    # ------------------------------------------------------------------
    async def upsert_batch(self, batch: NormalizedBatch) -> int:
        ds = batch.dataset
        for col in ds.columns:
            if not _COLUMN_NAME_RE.match(col.name):
                raise ValueError(f"非法欄位名: {col.name!r}")
        table = self.materialized_table(ds.id)
        now = _now()
        schema_json = json.dumps(
            [{"name": c.name, "type": c.type} for c in ds.columns],
            ensure_ascii=False,
        )
        col_names = [c.name for c in ds.columns]
        quoted = ", ".join(f'"{n}"' for n in col_names)
        placeholders = ", ".join("?" * (len(col_names) + 1))

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO datasets"
                " (id, source_id, title, schema_json, collection, license,"
                "  last_fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET title=excluded.title,"
                "  schema_json=excluded.schema_json,"
                "  collection=excluded.collection, license=excluded.license,"
                "  last_fetched_at=excluded.last_fetched_at",
                (ds.id, ds.source_id, ds.title, schema_json, ds.collection,
                 ds.license, now),
            )
            col_defs = ", ".join(f'"{c.name}" {c.type}' for c in ds.columns)
            await db.execute(
                f'CREATE TABLE IF NOT EXISTS "{table}"'
                f" (_nk TEXT PRIMARY KEY, {col_defs})"
            )
            cur = await db.execute(f'PRAGMA table_info("{table}")')
            existing = {row[1] for row in await cur.fetchall()}
            for c in ds.columns:
                if c.name not in existing:
                    await db.execute(
                        f'ALTER TABLE "{table}" ADD COLUMN "{c.name}" {c.type}'
                    )
            for rec in batch.records:
                await db.execute(
                    "INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?)",
                    (rec.dataset_id, rec.natural_key,
                     json.dumps(rec.payload, ensure_ascii=False), now),
                )
                await db.execute(
                    f'INSERT OR REPLACE INTO "{table}" (_nk, {quoted})'
                    f" VALUES ({placeholders})",
                    [rec.natural_key] + [rec.payload.get(n) for n in col_names],
                )
            await db.commit()
        return len(batch.records)

    # ------------------------------------------------------------------
    # 讀模型
    # ------------------------------------------------------------------
    async def get_dataset(self, dataset_id: str) -> DatasetMeta | None:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, source_id, title, schema_json, collection, license"
                " FROM datasets WHERE id = ?",
                (dataset_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        columns = tuple(
            ColumnSpec(c["name"], c.get("type", "TEXT"))
            for c in json.loads(row[3] or "[]")
        )
        return DatasetMeta(
            id=row[0], source_id=row[1], title=row[2],
            columns=columns, collection=row[4], license=row[5],
        )

    async def list_datasets(self) -> list[DatasetMeta]:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT id FROM datasets ORDER BY id")
            ids = [r[0] for r in await cur.fetchall()]
        result = []
        for i in ids:
            meta = await self.get_dataset(i)
            if meta:
                result.append(meta)
        return result

    async def query_rows(
        self,
        dataset_id: str,
        *,
        columns: list[str] | None = None,
        where: str | None = None,
        group_by: list[str] | None = None,
        order_by: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> QueryResult:
        """SQL 式查詢單一資料集物化表(不變量 #4:先白名單,後組裝)。"""
        meta = await self.get_dataset(dataset_id)
        if meta is None:
            raise DatasetNotFoundError(dataset_id)
        table = self.materialized_table(meta.id)
        sql, effective = build_select(
            table, columns=columns, where=where,
            group_by=group_by, order_by=order_by, limit=limit,
        )
        return await asyncio.to_thread(
            execute_readonly, self._db_path, table, sql, effective
        )

    async def sample_rows(self, dataset_id: str, n: int) -> QueryResult:
        return await self.query_rows(dataset_id, limit=n)

    async def search_records(
        self, keyword: str, dataset_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        keyword = keyword.strip()
        if not keyword:
            raise ValueError("keyword 不可為空")
        effective_limit = normalize_limit(limit)
        sql = (
            "SELECT dataset_id, natural_key, payload FROM records"
            " WHERE payload LIKE ?"
        )
        params: list[Any] = [f"%{keyword}%"]
        if dataset_id:
            sql += " AND dataset_id = ?"
            params.append(dataset_id)
        sql += " LIMIT ?"
        params.append(effective_limit)
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(sql, params)
            rows = await cur.fetchall()
        return [
            {"dataset_id": r[0], "natural_key": r[1], "payload": json.loads(r[2])}
            for r in rows
        ]

    async def get_record(
        self, dataset_id: str, natural_key: str
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT payload FROM records WHERE dataset_id = ?"
                " AND natural_key = ?",
                (dataset_id, natural_key),
            )
            row = await cur.fetchone()
        return json.loads(row[0]) if row else None

    # ------------------------------------------------------------------
    # 抓取軌跡
    # ------------------------------------------------------------------
    async def start_run(self, source_id: str) -> int:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "INSERT INTO ingestion_runs (source_id, started_at, status)"
                " VALUES (?, ?, ?)",
                (source_id, _now(), RunStatus.RUNNING.value),
            )
            await db.commit()
            assert cur.lastrowid is not None
            return cur.lastrowid

    async def finish_run(
        self,
        run_id: int,
        status: RunStatus,
        fetched_count: int,
        error_detail: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE ingestion_runs SET finished_at = ?, status = ?,"
                " fetched_count = ?, error_detail = ? WHERE id = ?",
                (_now(), status.value, fetched_count, error_detail, run_id),
            )
            await db.commit()
