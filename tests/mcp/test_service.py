"""QueryService — MCP 工具背後的查詢服務(BDD: query-tools / query-rows.feature)。"""
import pytest

from health_opendata_mcp.contracts import (
    AccessStrategy,
    ColumnSpec,
    DatasetMeta,
    NormalizedBatch,
    Record,
    SourceInfo,
)
from health_opendata_mcp.mcp_server.service import QueryService
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository

ROWS = [
    {"agency": "衛生福利部疾病管制署", "award_price": "100", "title": "疫苗系統維運"},
    {"agency": "衛生福利部中央健康保險署", "award_price": "200", "title": "健保資訊系統"},
]


@pytest.fixture
async def service(tmp_path):
    repo = SqliteRepository(str(tmp_path / "t.db"))
    await repo.init()
    await repo.register_source(
        SourceInfo(
            id="pcc",
            name="PCC",
            platform="web.pcc.gov.tw",
            access_strategy=AccessStrategy.STATIC_FILE,
        )
    )
    dataset = DatasetMeta(
        id="pcc-tender-mohw",
        source_id="pcc",
        title="衛福部標案",
        columns=(ColumnSpec("agency"), ColumnSpec("award_price"), ColumnSpec("title")),
        collection="healthcare",
    )
    await repo.upsert_batch(
        NormalizedBatch(
            dataset=dataset,
            records=tuple(
                Record(dataset_id=dataset.id, natural_key=str(i), payload=r)
                for i, r in enumerate(ROWS)
            ),
        )
    )
    return QueryService(repo)


class _FakeEnricher:
    def __init__(self, detail=None, exc=None):
        self._detail = detail
        self._exc = exc

    async def fetch_detail(self, job_number):
        if self._exc:
            raise self._exc
        return self._detail


class TestGetTenderDetail:
    async def test_returns_enriched_fields(self, tmp_path):
        from health_opendata_mcp.adapters._pcc_detail import DetailFields

        repo = SqliteRepository(str(tmp_path / "t.db"))
        await repo.init()
        svc = QueryService(
            repo,
            _FakeEnricher(
                DetailFields(
                    bid_deadline="2026-06-20 17:00",
                    open_date="2026-06-21 10:00",
                    budget="5000000",
                    title="某資訊系統案",
                    agency="衛生福利部",
                )
            ),
        )
        out = await svc.get_tender_detail("ABC123")
        assert out["bid_deadline"] == "2026-06-20 17:00"
        assert out["open_date"] == "2026-06-21 10:00"
        assert out["budget"] == "5000000"
        assert out["job_number"] == "ABC123"

    async def test_blocked_maps_to_valueerror(self, tmp_path):
        from health_opendata_mcp.contracts import BlockedError

        repo = SqliteRepository(str(tmp_path / "t.db"))
        await repo.init()
        svc = QueryService(repo, _FakeEnricher(exc=BlockedError("429")))
        with pytest.raises(ValueError, match="稍後再試"):
            await svc.get_tender_detail("X")

    async def test_not_found_maps_to_valueerror(self, tmp_path):
        repo = SqliteRepository(str(tmp_path / "t.db"))
        await repo.init()
        svc = QueryService(repo, _FakeEnricher(detail=None))
        with pytest.raises(ValueError, match="查無此案"):
            await svc.get_tender_detail("X")

    async def test_empty_job_number_rejected(self, tmp_path):
        repo = SqliteRepository(str(tmp_path / "t.db"))
        await repo.init()
        with pytest.raises(ValueError):
            await QueryService(repo, _FakeEnricher()).get_tender_detail("  ")


class TestCatalog:
    async def test_list_sources(self, service):
        sources = await service.list_sources()
        assert sources[0]["id"] == "pcc"
        assert sources[0]["access_strategy"] == "STATIC_FILE"
        assert sources[0]["last_fetched_at"] is not None

    async def test_list_datasets(self, service):
        datasets = await service.list_datasets()
        assert [d["id"] for d in datasets] == ["pcc-tender-mohw"]
        assert datasets[0]["collection"] == "healthcare"

    async def test_get_dataset_with_sample_rows(self, service):
        info = await service.get_dataset("pcc-tender-mohw", sample_rows=1)
        assert info["title"] == "衛福部標案"
        assert [c["name"] for c in info["schema"]] == ["agency", "award_price", "title"]
        assert len(info["sample"]["rows"]) == 1

    async def test_get_dataset_unknown_raises_value_error(self, service):
        with pytest.raises(ValueError):
            await service.get_dataset("nope")


class TestQueryRows:
    async def test_aggregation(self, service):
        result = await service.query_rows(
            "pcc-tender-mohw",
            columns=["COUNT(*) AS n", "SUM(CAST(award_price AS INTEGER)) AS total"],
        )
        assert result["columns"] == ["n", "total"]
        assert result["rows"] == [[2, 300]]
        assert result["truncated"] is False

    async def test_injection_maps_to_value_error(self, service):
        with pytest.raises(ValueError):
            await service.query_rows("pcc-tender-mohw", where="1=1; DROP TABLE records")

    async def test_cross_table_maps_to_value_error(self, service):
        with pytest.raises(ValueError):
            await service.query_rows(
                "pcc-tender-mohw", where="agency IN (SELECT id FROM datasets)"
            )

    async def test_unknown_dataset_maps_to_value_error(self, service):
        with pytest.raises(ValueError):
            await service.query_rows("nope")


class TestRecords:
    async def test_search_records(self, service):
        hits = await service.search_records("健保資訊")
        assert len(hits) == 1
        assert hits[0]["dataset_id"] == "pcc-tender-mohw"

    async def test_get_record(self, service):
        rec = await service.get_record("pcc-tender-mohw", "0")
        assert rec["agency"] == "衛生福利部疾病管制署"

    async def test_get_record_missing_raises(self, service):
        with pytest.raises(ValueError):
            await service.get_record("pcc-tender-mohw", "zzz")


class TestServerWiring:
    async def test_all_tools_registered(self, tmp_path):
        from health_opendata_mcp.mcp_server.server import build_server

        repo = SqliteRepository(str(tmp_path / "t.db"))
        await repo.init()
        mcp = build_server(repo)
        import inspect

        tools = mcp.list_tools()
        if inspect.isawaitable(tools):
            tools = await tools
        assert {t.name for t in tools} >= {
            "query_rows",
            "get_dataset",
            "list_sources",
            "list_datasets",
            "search_records",
            "get_record",
        }
