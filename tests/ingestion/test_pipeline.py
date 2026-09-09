"""run_source — discover→fetch→normalize→upsert 全鏈路(BDD: ingestion.feature)。"""
import pytest

from health_opendata_mcp.adapters.nhi import (
    NhiDatasetSpec,
    NhiHealthcareFacilityAdapter,
)
from health_opendata_mcp.contracts import (
    AccessStrategy,
    BlockedError,
    ColumnSpec,
    DatasetMeta,
    NormalizedBatch,
    RawPayload,
    Record,
    ResourceRef,
    RunStatus,
)
from health_opendata_mcp.ingestion.pipeline import run_source
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository

_DS = DatasetMeta(
    id="fake-ds",
    source_id="fake",
    title="Fake",
    columns=(ColumnSpec("name"),),
)


class FakeAdapter:
    source_id = "fake"
    name = "Fake Source"
    platform = "test"
    access_strategy = AccessStrategy.STATIC_FILE

    def __init__(self, refs_payloads: list[tuple[str, bytes | Exception]]):
        self._data = refs_payloads

    async def discover(self):
        return [
            ResourceRef(dataset=_DS, url=url, fmt="csv", meta={})
            for url, _ in self._data
        ]

    async def fetch(self, ref):
        payload = dict(self._data)[ref.url]
        if isinstance(payload, Exception):
            raise payload
        return RawPayload(ref=ref, content=payload)

    def normalize(self, raw):
        names = [n for n in raw.content.decode().split(",") if n]
        return NormalizedBatch(
            dataset=_DS,
            records=tuple(
                Record(dataset_id=_DS.id, natural_key=n, payload={"name": n})
                for n in names
            ),
        )


@pytest.fixture
async def repo(tmp_path):
    r = SqliteRepository(str(tmp_path / "t.db"))
    await r.init()
    return r


class TestRunSource:
    async def test_happy_path_ingests_and_records_run(self, repo):
        summary = await run_source(FakeAdapter([("u1", b"a,b"), ("u2", b"c")]), repo)
        assert summary.status is RunStatus.SUCCEEDED
        assert summary.fetched_count == 3
        result = await repo.sample_rows("fake-ds", 10)
        assert len(result.rows) == 3
        sources = await repo.list_sources()
        assert sources[0].id == "fake"  # pipeline 自動註冊來源

    async def test_single_ref_failure_does_not_abort(self, repo):
        adapter = FakeAdapter([("u1", RuntimeError("boom")), ("u2", b"x")])
        summary = await run_source(adapter, repo)
        assert summary.status is RunStatus.SUCCEEDED
        assert summary.fetched_count == 1
        assert len(summary.errors) == 1

    async def test_all_refs_failed_marks_failed(self, repo):
        adapter = FakeAdapter([("u1", RuntimeError("a")), ("u2", RuntimeError("b"))])
        summary = await run_source(adapter, repo)
        assert summary.status is RunStatus.FAILED

    async def test_blocked_marks_blocked_and_stops(self, repo):
        adapter = FakeAdapter([("u1", BlockedError("429")), ("u2", b"x")])
        summary = await run_source(adapter, repo)
        assert summary.status is RunStatus.BLOCKED
        assert summary.fetched_count == 0  # 被擋即停,不再打後續 ref

    async def test_facility_schema_drift_fails_and_preserves_existing_data(self, repo):
        spec = NhiDatasetSpec(
            dataset_id="nhi-healthcare-facility",
            r_id="A21030000I-D2100G-001",
            title="健保特約醫療院所名冊-需求範圍",
        )
        valid_csv = (
            "醫事機構代碼,權屬別名稱,醫事機構名稱,特約類別,"
            "終止合約或歇業日期\n"
            "0131060029,部立及直轄市立醫院,衛生福利部臺北醫院,2,\n"
        ).encode()

        async def valid_get(_url: str) -> bytes:
            return valid_csv

        first = await run_source(
            NhiHealthcareFacilityAdapter([spec], http_get=valid_get), repo
        )
        assert first.status is RunStatus.SUCCEEDED
        before = await repo.get_record("nhi-healthcare-facility", "0131060029")
        assert before is not None

        async def drifted_get(_url: str) -> bytes:
            return b"<html><title>maintenance</title></html>"

        second = await run_source(
            NhiHealthcareFacilityAdapter([spec], http_get=drifted_get), repo
        )
        assert second.status is RunStatus.FAILED
        assert second.fetched_count == 0
        assert "schema drift" in second.errors[0]
        assert (
            await repo.get_record("nhi-healthcare-facility", "0131060029")
        ) == before
