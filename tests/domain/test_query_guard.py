"""query_guard — SQL 安全驗證(BDD: spec/features/query-rows.feature)。"""
import pytest

from health_opendata_mcp.domain.query_guard import (
    MAX_LIMIT,
    QueryValidationError,
    build_select,
)


class TestBuildSelect:
    def test_basic_select_all(self):
        sql, eff = build_select("ds_demo")
        assert sql.startswith('SELECT * FROM "ds_demo"')
        assert eff == 50  # 預設 limit

    def test_aggregation_with_group_by_order_by(self):
        sql, eff = build_select(
            "ds_pcc_tender_mohw",
            columns=["agency", "SUM(CAST(award_price AS INTEGER)) AS total"],
            where="announcement_type='決標公告' AND date >= '2025-01-01'",
            group_by=["agency"],
            order_by="total DESC",
            limit=20,
        )
        assert 'FROM "ds_pcc_tender_mohw"' in sql
        assert "SUM(CAST(award_price AS INTEGER)) AS total" in sql
        assert "GROUP BY agency" in sql
        assert "ORDER BY total DESC" in sql
        assert eff == 20

    def test_limit_capped_at_max(self):
        _, eff = build_select("ds_demo", limit=10000)
        assert eff == MAX_LIMIT

    def test_limit_floor_at_one(self):
        _, eff = build_select("ds_demo", limit=0)
        assert eff == 1

    def test_sql_ends_with_limit_plus_one(self):
        # executor 以 eff+1 偵測截斷
        sql, eff = build_select("ds_demo", limit=10)
        assert sql.endswith(f"LIMIT {eff + 1}")


class TestInjectionRejection:
    def test_reject_multi_statement_semicolon(self):
        with pytest.raises(QueryValidationError):
            build_select("ds_demo", where="1=1; DROP TABLE records")

    def test_reject_sql_comment(self):
        with pytest.raises(QueryValidationError):
            build_select("ds_demo", where="1=1 --")

    def test_reject_block_comment(self):
        with pytest.raises(QueryValidationError):
            build_select("ds_demo", where="1=1 /* x */")

    def test_reject_pragma(self):
        with pytest.raises(QueryValidationError):
            build_select("ds_demo", where="pragma_table_info('x') IS NOT NULL")

    def test_reject_attach(self):
        with pytest.raises(QueryValidationError):
            build_select("ds_demo", where="ATTACH DATABASE '/tmp/x' AS x")

    def test_reject_dml_keyword_in_columns(self):
        with pytest.raises(QueryValidationError):
            build_select("ds_demo", columns=["(DELETE FROM records)"])

    def test_reject_injection_in_order_by(self):
        with pytest.raises(QueryValidationError):
            build_select("ds_demo", order_by="1; UPDATE records SET payload=''")

    def test_reject_injection_in_group_by(self):
        with pytest.raises(QueryValidationError):
            build_select("ds_demo", group_by=["agency; DROP TABLE datasets"])

    def test_chinese_values_pass(self):
        # 中文條件值是正常使用情境,不應誤殺
        sql, _ = build_select("ds_demo", where="agency LIKE '%衛生福利部%'")
        assert "衛生福利部" in sql
