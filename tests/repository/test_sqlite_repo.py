"""SqliteRepository — 基底表 + 物化表同交易 upsert(BDD: ingestion.feature)。"""
from datetime import datetime

import pytest

from health_opendata_mcp.contracts import (
    AccessStrategy,
    ColumnSpec,
    DatasetMeta,
    NormalizedBatch,
    Record,
    SourceInfo,
)
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository


def _demo_batch(rows: list[dict], columns: tuple[ColumnSpec, ...] | None = None) -> NormalizedBatch:
    dataset = DatasetMeta(
        id="demo-ds",
        source_id="demo-src",
        title="Demo Dataset",
        columns=columns
        or (ColumnSpec("name"), ColumnSpec("city"), ColumnSpec("amount", "INTEGER")),
        collection="healthcare",
    )
    records = tuple(
        Record(dataset_id="demo-ds", natural_key=r["name"], payload=r) for r in rows
    )
    return NormalizedBatch(dataset=dataset, records=records)


@pytest.fixture
async def repo(tmp_path):
    r = SqliteRepository(str(tmp_path / "test.db"))
    await r.init()
    return r


class TestUpsertBatch:
    async def test_creates_dataset_and_materialized_rows(self, repo):
        n = await repo.upsert_batch(
            _demo_batch([{"name": "a", "city": "台北", "amount": 100}])
        )
        assert n == 1
        meta = await repo.get_dataset("demo-ds")
        assert meta is not None
        assert meta.title == "Demo Dataset"
        assert meta.collection == "healthcare"
        result = await repo.sample_rows("demo-ds", 5)
        assert len(result.rows) == 1

    async def test_idempotent_upsert(self, repo):
        batch = _demo_batch([{"name": "a", "city": "台北", "amount": 100}])
        await repo.upsert_batch(batch)
        await repo.upsert_batch(batch)
        result = await repo.sample_rows("demo-ds", 10)
        assert len(result.rows) == 1  # 同 natural_key 不重複

    async def test_upsert_overwrites_changed_payload(self, repo):
        await repo.upsert_batch(_demo_batch([{"name": "a", "city": "台北", "amount": 100}]))
        await repo.upsert_batch(_demo_batch([{"name": "a", "city": "高雄", "amount": 200}]))
        rec = await repo.get_record("demo-ds", "a")
        assert rec["city"] == "高雄"

    async def test_schema_evolution_adds_column(self, repo):
        await repo.upsert_batch(_demo_batch([{"name": "a", "city": "台北", "amount": 1}]))
        cols2 = (
            ColumnSpec("name"),
            ColumnSpec("city"),
            ColumnSpec("amount", "INTEGER"),
            ColumnSpec("phone"),
        )
        await repo.upsert_batch(
            _demo_batch([{"name": "b", "city": "台中", "amount": 2, "phone": "04"}], cols2)
        )
        result = await repo.query_rows("demo-ds", where="phone = '04'")
        assert len(result.rows) == 1


class TestSourcesAndSearch:
    async def test_register_and_list_sources(self, repo):
        await repo.register_source(
            SourceInfo(
                id="nhi",
                name="健保署資料開放平台",
                platform="info.nhi.gov.tw",
                access_strategy=AccessStrategy.PLATFORM_API,
            )
        )
        sources = await repo.list_sources()
        assert [s.id for s in sources] == ["nhi"]
        assert sources[0].access_strategy is AccessStrategy.PLATFORM_API

    async def test_last_fetched_updated_after_upsert(self, repo):
        await repo.register_source(
            SourceInfo(
                id="demo-src",
                name="demo",
                platform="test",
                access_strategy=AccessStrategy.STATIC_FILE,
            )
        )
        await repo.upsert_batch(_demo_batch([{"name": "a", "city": "x", "amount": 1}]))
        meta = await repo.get_dataset("demo-ds")
        assert meta is not None
        sources = await repo.list_sources()
        assert sources[0].last_fetched_at is not None

    async def test_search_records_by_keyword(self, repo):
        await repo.upsert_batch(
            _demo_batch(
                [
                    {"name": "高雄市立民生醫院", "city": "高雄", "amount": 1},
                    {"name": "台大醫院", "city": "台北", "amount": 2},
                ]
            )
        )
        hits = await repo.search_records("民生")
        assert len(hits) == 1
        assert hits[0]["dataset_id"] == "demo-ds"
        assert "民生" in hits[0]["payload"]["name"]
