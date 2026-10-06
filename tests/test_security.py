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


# ── 5. query_rows 資源耗用:SQLite blob 配置炸彈(CWE-400)──────────────────


class TestQueryValueSizeLimit:
    """單一 string/blob 值上限 —— 擋「一步配置很多記憶體」的查詢。

    progress handler 以 VM 步數計,擋不到 hex(zeroblob(4e8)):它通過
    query_guard 的語法黑名單、通過 authorizer(zeroblob/hex 都不是
    load_extension),只花數十個 VM 步就配置約 1.1 GiB。
    deploy/k8s/deployment.yaml 的 memory limit 是 512Mi,一次呼叫就足以
    讓 pod 被 OOM kill,而 query_rows 是對外公開的 MCP 工具。
    """

    @staticmethod
    def _db(tmp_path) -> str:
        import sqlite3

        path = str(tmp_path / "limit.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE ds_x (agency TEXT, award_price TEXT)")
        conn.executemany(
            "INSERT INTO ds_x VALUES (?, ?)",
            [(f"衛生福利部{i % 5}", str(i * 1000)) for i in range(120)],
        )
        conn.commit()
        conn.close()
        return path

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"where": "length(hex(zeroblob(400000000))) > 0"},
            {"where": "length(hex(randomblob(300000000))) > 0"},
            {"order_by": "length(hex(zeroblob(400000000)))"},
            {"columns": ["hex(zeroblob(400000000)) AS x"]},
        ],
        ids=["where-zeroblob", "where-randomblob", "order-by", "columns"],
    )
    def test_blob_allocation_bomb_denied(self, tmp_path, kwargs) -> None:
        from health_opendata_mcp.domain.query_guard import build_select
        from health_opendata_mcp.repository.query_executor import (
            QueryDeniedError,
            execute_readonly,
        )

        db = self._db(tmp_path)
        sql, effective = build_select("ds_x", **kwargs)
        with pytest.raises(QueryDeniedError, match="大小上限"):
            execute_readonly(db, "ds_x", sql, effective)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"where": "agency LIKE '%衛生%'"},
            {
                "columns": ["agency", "SUM(CAST(award_price AS INTEGER)) AS total"],
                "group_by": ["agency"],
                "order_by": "total DESC",
            },
            {"columns": ["group_concat(agency) AS all_agencies"]},
            {"columns": ["hex(agency) AS h"]},
        ],
        ids=["select-star", "like", "aggregate", "group-concat", "hex-of-column"],
    )
    def test_normal_queries_unaffected(self, tmp_path, kwargs) -> None:
        """上限是 8 MiB —— 真實資料列、聚合與 group_concat 都遠低於此。"""
        from health_opendata_mcp.domain.query_guard import build_select
        from health_opendata_mcp.repository.query_executor import execute_readonly

        db = self._db(tmp_path)
        sql, effective = build_select("ds_x", **kwargs)
        result = execute_readonly(db, "ds_x", sql, effective)
        assert len(result.rows) >= 1

    def test_cross_table_read_still_denied(self, tmp_path) -> None:
        """既有 authorizer 防護未被新上限取代。"""
        from health_opendata_mcp.domain.query_guard import build_select
        from health_opendata_mcp.repository.query_executor import (
            QueryDeniedError,
            execute_readonly,
        )

        db = self._db(tmp_path)
        sql, effective = build_select(
            "ds_x", where="agency IN (SELECT name FROM sqlite_master)"
        )
        with pytest.raises(QueryDeniedError, match="白名單外"):
            execute_readonly(db, "ds_x", sql, effective)


# ── 6. query_rows 片段長度護欄(CWE-20)──────────────────────────────────────


class TestQueryFragmentLengthLimits:
    """where/order_by/columns/group_by 必須有長度上限。

    片段會被 query_guard 的 regex 與 SQLite parser 各完整掃過一次;
    無上限時單次呼叫即可送進 MB 級字串。其餘工具早有長度檢查,
    query_rows 先前是唯一缺口。
    """

    @pytest.fixture
    def service(self, tmp_path) -> QueryService:
        return QueryService(SqliteRepository(str(tmp_path / "frag.db")))

    async def test_where_too_long_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="where too long"):
            await service.query_rows("ds-a", where="a=1 OR " + "x" * 2001)

    async def test_order_by_too_long_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="order_by too long"):
            await service.query_rows("ds-a", order_by="x" * 2001)

    async def test_columns_item_too_long_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match=r"columns\[1\] too long"):
            await service.query_rows("ds-a", columns=["ok", "x" * 2001])

    async def test_group_by_item_too_long_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match=r"group_by\[0\] too long"):
            await service.query_rows("ds-a", group_by=["x" * 2001])

    async def test_too_many_columns_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="columns has too many items"):
            await service.query_rows("ds-a", columns=["a"] * 51)

    async def test_dataset_id_too_long_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="dataset_id too long"):
            await service.query_rows("x" * 201)

    async def test_get_dataset_id_too_long_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="dataset_id too long"):
            await service.get_dataset("x" * 201)

    async def test_fragments_at_limit_pass_the_guard(
        self, service: QueryService
    ) -> None:
        """長度剛好在上限內的片段不被護欄攔下 —— 呼叫會往下走到 repository。"""
        with pytest.raises(Exception) as exc_info:
            await service.query_rows("ds-a", where="a=1 AND " + "b" * 1990)
        assert "too long" not in str(exc_info.value)


# ── 7. 外送 HTTP 回應 body 上限(CWE-400)───────────────────────────────────


class TestBoundedHttpResponse:
    """default_http_get 必須串流讀取並在超限時中止,而非 resp.content 全收。"""

    @staticmethod
    def _patched_client(handler):
        import httpx

        transport = httpx.MockTransport(handler)
        base = httpx.AsyncClient

        class Patched(base):  # type: ignore[misc,valid-type]
            def __init__(self, *args, **kwargs) -> None:
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        return base, Patched

    async def test_oversized_body_rejected(self, monkeypatch) -> None:
        import httpx

        from health_opendata_mcp.adapters import _http
        from health_opendata_mcp.contracts import BlockedError

        base, patched = self._patched_client(
            lambda request: httpx.Response(200, content=b"x" * 4096)
        )
        monkeypatch.setattr(_http.httpx, "AsyncClient", patched)
        monkeypatch.setattr(_http, "_MAX_RESPONSE_BYTES", 1024)
        with pytest.raises(BlockedError, match="exceeds"):
            await _http.default_http_get("https://data.nhi.gov.tw/big.csv")
        assert base is httpx.AsyncClient or True

    async def test_normal_body_returned_unchanged(self, monkeypatch) -> None:
        import httpx

        from health_opendata_mcp.adapters import _http

        body = "醫事機構代碼,名稱\n0101,測試\n".encode()
        _, patched = self._patched_client(
            lambda request: httpx.Response(200, content=body)
        )
        monkeypatch.setattr(_http.httpx, "AsyncClient", patched)
        assert await _http.default_http_get("https://data.nhi.gov.tw/ok.csv") == body

    @pytest.mark.parametrize("status", [403, 429])
    async def test_blocked_status_still_raises_blocked_error(
        self, monkeypatch, status: int
    ) -> None:
        import httpx

        from health_opendata_mcp.adapters import _http
        from health_opendata_mcp.contracts import BlockedError

        _, patched = self._patched_client(lambda request: httpx.Response(status))
        monkeypatch.setattr(_http.httpx, "AsyncClient", patched)
        with pytest.raises(BlockedError, match=str(status)):
            await _http.default_http_get("https://data.nhi.gov.tw/x.csv")

    async def test_server_error_still_raises_for_status(self, monkeypatch) -> None:
        import httpx

        from health_opendata_mcp.adapters import _http

        _, patched = self._patched_client(lambda request: httpx.Response(500))
        monkeypatch.setattr(_http.httpx, "AsyncClient", patched)
        with pytest.raises(httpx.HTTPStatusError):
            await _http.default_http_get("https://data.nhi.gov.tw/x.csv")


# ── 8. workflow 不得把 inputs 插進 run: 指令(CWE-78)───────────────────────


class TestWorkflowScriptInjection:
    """`${{ }}` 在 shell 執行前做文字替換;插進 run: 等於讓輸入變成 shell 語法。

    純文字檢查,與 tests/scripts/test_pages_workflow.py 一致,不引入 YAML 相依。
    """

    @pytest.mark.parametrize(
        "workflow",
        ["ci.yml", "pages.yml", "verify-catalog.yml"],
    )
    def test_no_input_expressions_inside_run_blocks(self, workflow: str) -> None:
        import re
        from pathlib import Path

        text = Path(".github/workflows") / workflow
        lines = text.read_text(encoding="utf-8").splitlines()

        offenders: list[str] = []
        in_run = False
        run_indent = 0
        for line in lines:
            stripped = line.strip()
            m = re.match(r"^(\s*)run:\s*\|?", line)
            if m and stripped.startswith("run:"):
                in_run = True
                run_indent = len(m.group(1))
                continue
            if in_run:
                indent = len(line) - len(line.lstrip())
                if stripped and indent <= run_indent:
                    in_run = False
                elif "${{" in line and (
                    "inputs." in line or "github.event" in line
                ):
                    offenders.append(line.strip())
        assert offenders == [], (
            f"{workflow}: inputs 直接插進 run: 指令(CWE-78),應改以 env 傳入:"
            f" {offenders}"
        )

    def test_verify_catalog_passes_only_via_env(self) -> None:
        from pathlib import Path

        text = Path(".github/workflows/verify-catalog.yml").read_text(encoding="utf-8")
        assert "ONLY: ${{ inputs.only }}" in text
        assert 'args+=(--only "$ONLY")' in text
