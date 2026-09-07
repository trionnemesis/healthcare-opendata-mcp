"""dataset 新鮮度讀模型 — last_fetched_at / row_count。

核心語意:row_count 為 None 代表「物化表尚不存在」= 從未同步成功,
與「同步成功但 0 筆」不同。缺值不折成 0(loud failure,不靜默降級)。
"""
from datetime import datetime, timezone

import aiosqlite
import pytest

from health_opendata_mcp.contracts import (
    ColumnSpec,
    DatasetMeta,
    NormalizedBatch,
    Record,
)
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository


def _batch(dataset_id: str, names: list[str]) -> NormalizedBatch:
    dataset = DatasetMeta(
        id=dataset_id,
        source_id="demo-src",
        title=f"Demo {dataset_id}",
        columns=(ColumnSpec("name"),),
        collection="healthcare",
    )
    return NormalizedBatch(
        dataset=dataset,
        records=tuple(
            Record(dataset_id=dataset_id, natural_key=n, payload={"name": n})
            for n in names
        ),
    )


@pytest.fixture
async def repo(tmp_path):
    r = SqliteRepository(str(tmp_path / "status.db"))
    await r.init()
    return r


class TestDatasetStatus:
    async def test_reports_row_count_and_fetch_time_after_sync(self, repo):
        before = datetime.now(timezone.utc)
        await repo.upsert_batch(_batch("demo-ds", ["a", "b", "c"]))
        status = await repo.dataset_status("demo-ds")
        assert status is not None
        assert status.dataset_id == "demo-ds"
        assert status.row_count == 3
        assert status.last_fetched_at is not None
        assert status.last_fetched_at >= before

    async def test_upsert_is_idempotent_for_row_count(self, repo):
        await repo.upsert_batch(_batch("demo-ds", ["a", "b"]))
        await repo.upsert_batch(_batch("demo-ds", ["a", "b"]))
        status = await repo.dataset_status("demo-ds")
        assert status.row_count == 2

    async def test_unknown_dataset_returns_none(self, repo):
        assert await repo.dataset_status("does-not-exist") is None

    async def test_row_count_is_none_when_materialized_table_missing(self, tmp_path):
        """datasets 有列、物化表沒建起來 —— 不可回報 0 筆假成功。"""
        db_path = str(tmp_path / "ghost.db")
        repo = SqliteRepository(db_path)
        await repo.init()
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO datasets (id, source_id, title, schema_json)"
                " VALUES ('ghost-ds', 'demo-src', 'Ghost', '[]')"
            )
            await db.commit()
        status = await repo.dataset_status("ghost-ds")
        assert status is not None
        assert status.row_count is None
        assert status.last_fetched_at is None


class TestListDatasetStatus:
    async def test_lists_every_dataset_ordered_by_id(self, repo):
        await repo.upsert_batch(_batch("b-ds", ["x"]))
        await repo.upsert_batch(_batch("a-ds", ["x", "y"]))
        rows = await repo.list_dataset_status()
        assert [r.dataset_id for r in rows] == ["a-ds", "b-ds"]
        assert [r.row_count for r in rows] == [2, 1]

    async def test_empty_db_yields_empty_list(self, repo):
        assert await repo.list_dataset_status() == []
