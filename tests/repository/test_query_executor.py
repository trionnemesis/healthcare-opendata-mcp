"""query_rows 執行面 — 唯讀連線 + authorizer 白名單(BDD: query-rows.feature)。"""
import pytest

from health_opendata_mcp.contracts import (
    ColumnSpec,
    DatasetMeta,
    DatasetNotFoundError,
    NormalizedBatch,
    Record,
)
from health_opendata_mcp.domain.query_guard import QueryValidationError
from health_opendata_mcp.repository.query_executor import QueryDeniedError
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository

AWARDS = [
    {"agency": "衛生福利部疾病管制署", "award_price": "100", "announcement_type": "決標公告"},
    {"agency": "衛生福利部疾病管制署", "award_price": "200", "announcement_type": "決標公告"},
    {"agency": "衛生福利部中央健康保險署", "award_price": "50", "announcement_type": "決標公告"},
]


@pytest.fixture
async def repo(tmp_path):
    r = SqliteRepository(str(tmp_path / "test.db"))
    await r.init()
    dataset = DatasetMeta(
        id="pcc-tender-mohw",
        source_id="pcc",
        title="衛福部標案",
        columns=(
            ColumnSpec("agency"),
            ColumnSpec("award_price"),
            ColumnSpec("announcement_type"),
        ),
    )
    records = tuple(
        Record(dataset_id="pcc-tender-mohw", natural_key=str(i), payload=row)
        for i, row in enumerate(AWARDS)
    )
    await r.upsert_batch(NormalizedBatch(dataset=dataset, records=records))
    return r


class TestQueryRows:
    async def test_aggregation_group_by_sum(self, repo):
        # 對齊 twinkle query_rows 模式:按機關聚合決標金額
        result = await repo.query_rows(
            "pcc-tender-mohw",
            columns=["agency", "SUM(CAST(award_price AS INTEGER)) AS total"],
            where="announcement_type = '決標公告'",
            group_by=["agency"],
            order_by="total DESC",
        )
        assert result.columns == ("agency", "total")
        assert result.rows[0] == ("衛生福利部疾病管制署", 300)
        assert result.rows[1] == ("衛生福利部中央健康保險署", 50)

    async def test_unknown_dataset_raises(self, repo):
        with pytest.raises(DatasetNotFoundError):
            await repo.query_rows("nonexistent; DROP TABLE records")

    async def test_injection_rejected_before_execution(self, repo):
        with pytest.raises(QueryValidationError):
            await repo.query_rows("pcc-tender-mohw", where="1=1; DROP TABLE records")

    async def test_cross_table_subquery_denied(self, repo):
        # authorizer 白名單:物化表以外(如 records 基底表)一律拒讀
        with pytest.raises(QueryDeniedError):
            await repo.query_rows(
                "pcc-tender-mohw",
                where="agency IN (SELECT dataset_id FROM records)",
            )

    async def test_truncation_flag(self, repo):
        result = await repo.query_rows("pcc-tender-mohw", limit=2)
        assert len(result.rows) == 2
        assert result.truncated is True

    async def test_no_truncation_when_all_returned(self, repo):
        result = await repo.query_rows("pcc-tender-mohw", limit=200)
        assert len(result.rows) == 3
        assert result.truncated is False
