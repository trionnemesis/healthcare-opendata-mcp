"""安全回歸測試 — 鎖住已修正漏洞的觸發條件。

每個 class 對應一項已確認的問題;測試同時涵蓋「攻擊被擋下」與
「正常輸入行為不變」兩面,避免修正造成功能退化。
"""
from __future__ import annotations

import os

import pytest

from health_opendata_mcp.adapters.pcc_detail import (
    HttpResp,
    PccDetailEnricher,
    _resolve_detail_url,
)
from health_opendata_mcp.cli import ensure_db_dir
from health_opendata_mcp.contracts import (
    ColumnSpec,
    DatasetMeta,
    NormalizedBatch,
    Record,
)
from health_opendata_mcp.mcp_server.service import QueryService
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository


def _batch(rows: list[dict]) -> NormalizedBatch:
    dataset = DatasetMeta(
        id="ds-a",
        source_id="src-a",
        title="Security fixture",
        columns=(ColumnSpec("title"),),
        collection="healthcare",
    )
    return NormalizedBatch(
        dataset=dataset,
        records=tuple(
            Record(dataset_id="ds-a", natural_key=r["_nk"], payload={"title": r["title"]})
            for r in rows
        ),
    )


# ── 1. 明細連結 SSRF(CWE-918)────────────────────────────────────────────────


class TestDetailHrefSsrf:
    """搜尋結果頁的 href 屬不可信輸入,解析後必須仍落在 web.pcc.gov.tw。"""

    @pytest.mark.parametrize(
        "path",
        [
            "https://attacker.example/prkms/urlSelector/common/tpam?pk=AAA",
            "//attacker.example/readBulletion",
            "http://web.pcc.gov.tw/prkms/x",  # 降級為 http
            "https://web.pcc.gov.tw.evil.example/readBulletion",  # 後綴混淆
        ],
    )
    def test_offsite_href_rejected(self, path: str) -> None:
        with pytest.raises(RuntimeError, match="unexpected detail href"):
            _resolve_detail_url(path, "T-001")

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            (
                "/prkms/urlSelector/common/tpam?pk=AAA",
                "https://web.pcc.gov.tw/prkms/urlSelector/common/tpam?pk=AAA",
            ),
            (
                "https://web.pcc.gov.tw/prkms/x?pk=1",
                "https://web.pcc.gov.tw/prkms/x?pk=1",
            ),
        ],
    )
    def test_legitimate_href_unchanged(self, path: str, expected: str) -> None:
        assert _resolve_detail_url(path, "T-001") == expected

    async def test_enricher_never_requests_offsite_host(self) -> None:
        """端到端:惡意 href 必須在送出請求前就被擋下。"""
        requested: list[str] = []

        class FakeClient:
            async def get(self, url: str) -> HttpResp:
                requested.append(url)
                return HttpResp(200, "")

            async def post(self, url: str, data: dict[str, str]) -> HttpResp:
                requested.append(url)
                html = (
                    '<a href="https://attacker.example/prkms/urlSelector/'
                    'common/tpam?pk=AAA">detail</a>'
                )
                return HttpResp(200, html)

        enricher = PccDetailEnricher(FakeClient())
        with pytest.raises(RuntimeError, match="unexpected detail href"):
            await enricher.fetch_detail("T-001")
        assert not any("attacker.example" in url for url in requested)


# ── 2. LIKE 萬用字元跳脫(CWE-943)────────────────────────────────────────────


class TestSearchRecordsLikeEscaping:
    """keyword 中的 % / _ 必須以字面值比對,不得變成萬用字元。"""

    def test_escape_percent(self) -> None:
        assert SqliteRepository._escape_like("100%") == "100\\%"

    def test_escape_underscore(self) -> None:
        assert SqliteRepository._escape_like("a_b") == "a\\_b"

    def test_escape_backslash_first(self) -> None:
        assert SqliteRepository._escape_like("a\\b") == "a\\\\b"

    def test_plain_text_unchanged(self) -> None:
        assert SqliteRepository._escape_like("衛生福利部") == "衛生福利部"

    async def test_wildcard_keyword_does_not_dump_every_record(
        self, tmp_path
    ) -> None:
        repo = SqliteRepository(str(tmp_path / "t.db"))
        await repo.init()
        await repo.upsert_batch(
            _batch(
                [
                    {"_nk": "k1", "title": "一般標案"},
                    {"_nk": "k2", "title": "折扣 100% 標案"},
                ]
            )
        )

        wildcard = await repo.search_records("%")
        assert [r["natural_key"] for r in wildcard] == ["k2"]

        literal = await repo.search_records("一般")
        assert [r["natural_key"] for r in literal] == ["k1"]


# ── 3. 工具參數長度上限(CWE-20 / CWE-400)───────────────────────────────────


class TestInputLengthLimits:
    @pytest.fixture
    def service(self, tmp_path) -> QueryService:
        return QueryService(SqliteRepository(str(tmp_path / "t.db")))

    async def test_keyword_too_long_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="keyword too long"):
            await service.search_records("x" * 201)

    async def test_job_number_too_long_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="job_number too long"):
            await service.get_tender_detail("x" * 201)

    async def test_natural_key_too_long_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="natural_key too long"):
            await service.get_record("ds-a", "x" * 201)

    async def test_keyword_at_limit_still_queries(self, tmp_path) -> None:
        """上限之內的輸入不得被誤擋(避免修正造成功能退化)。"""
        repo = SqliteRepository(str(tmp_path / "t.db"))
        await repo.init()
        service = QueryService(repo)
        assert await service.search_records("x" * 200) == []


# ── 4. 資料目錄權限(CWE-276)─────────────────────────────────────────────────


class TestDataDirPermissions:
    """sync(寫)與 server(讀)都必須經同一個 ensure_db_dir 建立 0o700 目錄。"""

    def test_ensure_db_dir_is_owner_only(self, tmp_path) -> None:
        if os.name == "nt":
            pytest.skip("POSIX 權限位元不適用 Windows")
        parent = ensure_db_dir(str(tmp_path / "nested" / "hcmcp.db"))
        assert parent.stat().st_mode & 0o777 == 0o700

    def test_existing_dir_permissions_untouched(self, tmp_path) -> None:
        """已存在的目錄不改權限(不覆寫使用者刻意設定的部署權限)。"""
        if os.name == "nt":
            pytest.skip("POSIX 權限位元不適用 Windows")
        existing = tmp_path / "preset"
        existing.mkdir(mode=0o750)
        ensure_db_dir(str(existing / "hcmcp.db"))
        assert existing.stat().st_mode & 0o777 == 0o750

    def test_both_entrypoints_share_one_helper(self) -> None:
        """避免任一進入點日後 inline 回 mkdir() 而漏掉權限。"""
        from health_opendata_mcp.mcp_server import __main__ as server_main

        assert server_main.ensure_db_dir is ensure_db_dir


# ── 5. SQL 函式白名單:輸出放大型 DoS(CWE-770)──────────────────────────────


def _tender_batch() -> NormalizedBatch:
    """帶 companies / award_price 的標案樣本,供查詢面測試共用。"""
    dataset = DatasetMeta(
        id="pcc-tender",
        source_id="src-pcc",
        title="Tender fixture",
        columns=(
            ColumnSpec("title"),
            ColumnSpec("companies"),
            ColumnSpec("award_price"),
            ColumnSpec("announcement_type"),
        ),
        collection="procurement",
    )
    return NormalizedBatch(
        dataset=dataset,
        records=tuple(
            Record(
                dataset_id="pcc-tender",
                natural_key=f"k{i}",
                payload={
                    "title": f"案{i}",
                    "companies": "甲公司",
                    "award_price": "100",
                    "announcement_type": "決標公告",
                },
            )
            for i in range(3)
        ),
    )


class TestSqlFunctionAllowlist:
    """authorizer 的 progress handler 數 VM 步數,擋不住「少步數、大配置」。

    randomblob(1e9) 一步即配置 1GB;乘上 MAX_LIMIT=400 列,單次 query_rows
    就能耗盡行程記憶體(修正前實測 5.7GiB RSS)。函式改白名單後這類呼叫
    在執行層被 DENY,而對外承諾的分析用法必須完全不受影響。
    """

    @pytest.fixture
    async def service(self, tmp_path) -> QueryService:
        repo = SqliteRepository(str(tmp_path / "t.db"))
        await repo.init()
        await repo.upsert_batch(_tender_batch())
        return QueryService(repo)

    @pytest.mark.parametrize(
        "column",
        [
            "randomblob(100000000)",          # 配置放大
            "zeroblob(100000000)",            # 同上
            "printf('%100000000d', 1)",       # 格式字串放大
            "sqlite_version()",               # 建置資訊洩漏
            "sqlite_source_id()",
            "sqlite_compileoption_get(0)",
            "last_insert_rowid()",
            "total_changes()",
        ],
    )
    async def test_non_allowlisted_function_denied(
        self, service: QueryService, column: str
    ) -> None:
        with pytest.raises(ValueError, match="白名單外的函式"):
            await service.query_rows("pcc-tender", columns=[column])

    @pytest.mark.parametrize(
        "kwargs",
        [
            # server.py query_rows docstring 的範例查詢
            {
                "columns": ["title", "SUM(CAST(award_price AS INTEGER)) AS total"],
                "where": "announcement_type='決標公告'",
                "group_by": ["title"],
                "order_by": "total DESC",
            },
            {"columns": ["COUNT(*) AS n"], "where": "title LIKE '%案%'"},
            {"where": "companies GLOB '*公司*'"},
            {"columns": ["lower(trim(companies))", "length(title)", "substr(title,1,2)"]},
            {"columns": ["date('now')", "strftime('%Y', date('now'))"]},
            {"columns": ["COALESCE(companies,'-')", "IIF(award_price>'50','h','l')"]},
            {"columns": ["ROUND(AVG(CAST(award_price AS INTEGER)),2)", "MIN(title)"]},
            {"columns": ["group_concat(companies)"]},
            {"columns": ["title", "ROW_NUMBER() OVER (ORDER BY title) AS rn"]},
        ],
    )
    async def test_advertised_analytics_still_work(
        self, service: QueryService, kwargs: dict
    ) -> None:
        """白名單不得縮減 query_rows 對外承諾的聚合/字串/日期/window 用法。"""
        result = await service.query_rows("pcc-tender", **kwargs)
        assert result["rows"], f"查詢應回傳資料列: {kwargs}"

    async def test_load_extension_still_blocked(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="禁用語法"):
            await service.query_rows(
                "pcc-tender", columns=["load_extension('/tmp/x.so')"]
            )


# ── 6. SQL 片段長度/項數上限(CWE-400)───────────────────────────────────────


class TestQueryFragmentLimits:
    """query_rows 的 SQL 片段同為 MCP 自由文字參數,先前未納入長度上限。

    修正前 3MB 的 where 可直接通過 regex 掃描進到 SQLite parser。
    """

    @pytest.fixture
    async def service(self, tmp_path) -> QueryService:
        repo = SqliteRepository(str(tmp_path / "t.db"))
        await repo.init()
        await repo.upsert_batch(_tender_batch())
        return QueryService(repo)

    @pytest.mark.parametrize(
        "kwargs,what",
        [
            ({"where": "title = '" + "A" * 3_000_000 + "'"}, "where"),
            ({"columns": ["'" + "B" * 2_000_000 + "'"]}, "columns"),
            ({"order_by": "'" + "C" * 2_000_000 + "'"}, "order_by"),
            ({"group_by": ["'" + "D" * 2_000_000 + "'"]}, "group_by"),
            # parser stack overflow 的深層嵌套同樣先被長度上限擋下
            ({"where": "(" * 50_000 + "1" + ")" * 50_000}, "where"),
        ],
    )
    async def test_oversized_fragment_rejected(
        self, service: QueryService, kwargs: dict, what: str
    ) -> None:
        with pytest.raises(ValueError, match=f"{what} 過長"):
            await service.query_rows("pcc-tender", **kwargs)

    async def test_too_many_columns_rejected(self, service: QueryService) -> None:
        """逐項長度上限擋不住「很多項」,故另設項數上限。"""
        with pytest.raises(ValueError, match="columns 項數過多"):
            await service.query_rows(
                "pcc-tender", columns=[f"title AS c{i}" for i in range(500)]
            )

    async def test_realistic_fragment_unaffected(self, service: QueryService) -> None:
        """上限遠大於真實查詢:repo 內最長的 where 不到 100 字元。"""
        result = await service.query_rows(
            "pcc-tender",
            where="announcement_type='決標公告' AND companies LIKE '%公司%'",
            columns=[f"title AS c{i}" for i in range(64)],
        )
        assert len(result["columns"]) == 64


# ── 7. 查詢錯誤的例外映射(CWE-209)──────────────────────────────────────────


class TestQueryErrorMapping:
    """service 開頭宣告「領域/執行層例外統一轉 ValueError」。

    修正前 sqlite3.OperationalError(如 no such column)會原樣逸出對外介面,
    屬未映射的內部例外型別。
    """

    @pytest.fixture
    async def service(self, tmp_path) -> QueryService:
        repo = SqliteRepository(str(tmp_path / "t.db"))
        await repo.init()
        await repo.upsert_batch(_tender_batch())
        return QueryService(repo)

    async def test_unknown_column_maps_to_value_error(
        self, service: QueryService
    ) -> None:
        import sqlite3

        with pytest.raises(ValueError) as excinfo:
            await service.query_rows("pcc-tender", where="nosuchcolumn = 1")
        assert not isinstance(excinfo.value, sqlite3.DatabaseError)

    async def test_vendor_stats_on_incompatible_dataset(self, tmp_path) -> None:
        """缺 companies / award_price 的資料集要回可理解的錯誤,而非原始 SQL 例外。"""
        import sqlite3

        repo = SqliteRepository(str(tmp_path / "t.db"))
        await repo.init()
        await repo.upsert_batch(_batch([{"_nk": "a", "title": "T"}]))
        service = QueryService(repo)
        with pytest.raises(ValueError, match="不支援廠商統計") as excinfo:
            await service.get_vendor_stats("ds-a")
        assert not isinstance(excinfo.value, sqlite3.DatabaseError)
