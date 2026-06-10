"""run_source — discover→fetch→normalize→upsert 全鏈路(BDD: ingestion.feature)。"""
import pytest

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
