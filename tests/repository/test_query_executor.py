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


class TestResourceExhaustionGuards:
    """CWE-770/400:query_rows 的 SQL 片段可配置任意記憶體 → pod OOMKill。

    authorizer 只管「讀哪張表」,progress handler 以 VM 步數計量;
    `hex(zeroblob(N))` 兩者都不觸發,卻能在毫秒內配置數百 MB
    (實測 zeroblob(2e8) → RSS ≈ 570MB,deployment.yaml 的 limit 是 512Mi)。
    """

    async def test_single_value_allocation_bomb_denied(self, repo):
        # 單一值放大:length(hex(zeroblob(2e8))) 需配置約 400MB
        with pytest.raises(QueryDeniedError):
            await repo.query_rows(
                "pcc-tender-mohw",
                where="agency != '' AND length(hex(zeroblob(200000000))) > 0",
            )

    async def test_randomblob_allocation_bomb_denied(self, repo):
        with pytest.raises(QueryDeniedError):
            await repo.query_rows(
                "pcc-tender-mohw",
                where="agency != '' AND length(hex(randomblob(200000000))) > 0",
            )

    async def test_row_multiplied_allocation_denied(self, repo):
        # 每列都在單值上限之下,但列數相乘後超出結果集預算
        with pytest.raises(QueryDeniedError):
            await repo.query_rows(
                "pcc-tender-mohw",
                columns=["hex(zeroblob(2000000)) AS big"],
                limit=400,
            )

    async def test_oversized_sql_fragment_denied(self, repo):
        # where 片段先前無長度上限;MB 級 SQL 文字直接交給 parser
        giant = " AND agency != '" + ("x" * 400_000) + "'"
        with pytest.raises(QueryDeniedError):
            await repo.query_rows("pcc-tender-mohw", where="agency != ''" + giant)

    async def test_deeply_nested_expression_denied(self, repo):
        nested = "agency != ''" + (" AND (1=1" * 400) + ")" * 400
        with pytest.raises(QueryDeniedError):
            await repo.query_rows("pcc-tender-mohw", where=nested)

    async def test_normal_queries_unaffected(self, repo):
        """護欄不得改變正常查詢的結果、欄位名或截斷旗標。"""
        plain = await repo.query_rows("pcc-tender-mohw", limit=200)
        assert len(plain.rows) == 3
        assert plain.truncated is False

        agg = await repo.query_rows(
            "pcc-tender-mohw",
            columns=["agency", "SUM(CAST(award_price AS INTEGER)) AS total"],
            where="announcement_type='決標公告'",
            group_by=["agency"],
            order_by="total DESC",
        )
        assert agg.columns == ("agency", "total")
        assert agg.rows[0] == ("衛生福利部疾病管制署", 300)

    async def test_modest_sized_values_still_readable(self, repo):
        """低於單值上限的大字串仍須讀得出來 —— 上限不是把正常資料擋掉。"""
        result = await repo.query_rows(
            "pcc-tender-mohw",
            columns=["length(hex(zeroblob(100000))) AS n"],
            limit=1,
        )
        assert result.rows[0][0] == 200_000
