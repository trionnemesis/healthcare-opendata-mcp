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


# ── 5. 回應 body 大小上限(CWE-400)───────────────────────────────────────────


def _mock_client(handler, *, follow_redirects: bool = False):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=follow_redirects
    )


class TestResponseSizeLimit:
    """`resp.content` / `resp.text` 會把整份 body 讀進記憶體才交給下游解析。

    上游站台(健保署、web.pcc.gov.tw)不在本專案控制範圍內;被指向大型檔案
    或上游異常回傳巨量 body 時,單次呼叫即可耗盡記憶體。`get_tender_detail`
    是 MCP 工具,此路徑由用戶端可達,不只是離線 ETL。
    """

    async def test_declared_oversize_is_rejected_before_download(self) -> None:
        import httpx

        from health_opendata_mcp.adapters._http import (
            ResponseTooLargeError,
            bounded_send,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"Content-Length": "999"}, content=b"x" * 999
            )

        async with _mock_client(handler) as client:
            with pytest.raises(ResponseTooLargeError, match="declared"):
                await bounded_send(
                    client, client.build_request("GET", "https://x.test/f"),
                    max_bytes=100,
                )

    async def test_streamed_oversize_is_rejected(self) -> None:
        """未宣告 Content-Length 時,累計超量仍須中止。"""
        import httpx

        from health_opendata_mcp.adapters._http import (
            ResponseTooLargeError,
            bounded_send,
        )

        async def body():
            for _ in range(20):
                yield b"y" * 64

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body())

        async with _mock_client(handler) as client:
            with pytest.raises(ResponseTooLargeError):
                await bounded_send(
                    client, client.build_request("GET", "https://x.test/f"),
                    max_bytes=100,
                )

    async def test_under_cap_response_api_is_unchanged(self) -> None:
        """上限之內的回應行為不得改變(避免修正造成功能退化)。"""
        import httpx

        from health_opendata_mcp.adapters._http import bounded_send

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201,
                headers={"Content-Type": "application/json; charset=utf-8"},
                json={"ok": True, "名稱": "衛福部"},
            )

        async with _mock_client(handler) as client:
            resp = await bounded_send(
                client, client.build_request("GET", "https://x.test/f"),
                max_bytes=1_000_000,
            )
        assert resp.status_code == 201
        assert resp.json() == {"ok": True, "名稱": "衛福部"}
        assert "衛福部" in resp.text
        assert resp.headers["Content-Type"].startswith("application/json")

    async def test_redirect_bodies_are_not_buffered_past_the_cap(self) -> None:
        """轉址中間回應的 body 不得被讀進記憶體,否則上限等於沒設。"""
        import httpx

        from health_opendata_mcp.adapters._http import bounded_send

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/a":
                return httpx.Response(
                    302, headers={"Location": "https://x.test/b"},
                    content=b"z" * 5_000,
                )
            return httpx.Response(200, content=b"done")

        async with _mock_client(handler, follow_redirects=True) as client:
            resp = await bounded_send(
                client, client.build_request("GET", "https://x.test/a"),
                max_bytes=1_000,
            )
        assert resp.content == b"done"

    async def test_default_http_get_is_capped(self, monkeypatch) -> None:
        import httpx

        from health_opendata_mcp.adapters import _http

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"q" * 4_096)

        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        monkeypatch.setattr(_http.httpx, "AsyncClient", factory)
        assert len(await _http.default_http_get("https://x.test/f")) == 4_096
        with pytest.raises(_http.ResponseTooLargeError):
            await _http.default_http_get("https://x.test/f", max_bytes=1_024)

    def test_too_large_is_a_blocked_error(self) -> None:
        """呼叫端既有的 BlockedError 分支必須接得住,否則變成未處理例外。"""
        from health_opendata_mcp.adapters._http import ResponseTooLargeError
        from health_opendata_mcp.contracts import BlockedError

        assert issubclass(ResponseTooLargeError, BlockedError)

    def test_caps_are_set_and_bounded(self) -> None:
        from health_opendata_mcp.adapters import _http

        assert 0 < _http.MAX_PAGE_BYTES <= 8 * 1024 * 1024
        assert 0 < _http.MAX_BULK_BYTES <= 100 * 1024 * 1024


class TestAllHttpPathsAreBounded:
    """避免日後改回 `self._c.get(...)`,讓某條路徑重新變成無上限。"""

    @pytest.mark.parametrize(
        "filename", ["_http.py", "pcc_detail.py"]
    )
    def test_no_unbounded_client_read(self, filename: str) -> None:
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "src/health_opendata_mcp/adapters"
            / filename
        ).read_text(encoding="utf-8")
        for banned in ("self._c.get(", "self._c.post(", "await client.get("):
            assert banned not in src, (
                f"{filename} 直接呼叫 {banned} —— 繞過 bounded_send 的大小上限"
            )

    def test_pcc_detail_client_uses_bounded_send(self) -> None:
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "src/health_opendata_mcp/adapters/pcc_detail.py"
        ).read_text(encoding="utf-8")
        assert "bounded_send" in src


# ── 6. query_rows 的例外映射與片段上限(CWE-755 / CWE-209 / CWE-20)──────────


class TestQueryRowsErrorMapping:
    """`conn.execute()` 失敗時 sqlite3 拋 OperationalError —— 它原本不在
    QueryService 的例外映射清單內,會原樣穿透 MCP 工具邊界,與 service
    文件宣告的「領域/執行層例外統一轉 ValueError」相矛盾(CWE-755),
    並把引擎內部錯誤字串交給呼叫端(CWE-209)。
    """

    @pytest.fixture
    async def service(self, tmp_path) -> QueryService:
        repo = SqliteRepository(str(tmp_path / "t.db"))
        await repo.init()
        await repo.upsert_batch(_batch([{"_nk": "k1", "title": "甲"}]))
        return QueryService(repo)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"where": "nosuchcol = 1"},
            {"where": "1 ==== 1"},
            {"where": "title LIKE '" + "a" * 1_500 + "'"},
            {"order_by": "nosuchcol"},
            {"columns": ["nosuchcol"]},
            {"group_by": ["nosuchcol"]},
        ],
    )
    async def test_sqlite_errors_surface_as_value_error(
        self, service: QueryService, kwargs: dict
    ) -> None:
        import sqlite3

        try:
            await service.query_rows("ds-a", **kwargs)
        except ValueError:
            pass
        except sqlite3.Error as exc:  # pragma: no cover - 迴歸時才會走到
            pytest.fail(f"raw sqlite3 error escaped the tool boundary: {exc!r}")

    def test_operational_detail_is_not_echoed(self, tmp_path) -> None:
        """執行層的環境問題(DB 檔案打不開)不得把路徑或引擎狀態轉述出去。

        直接測 execute_readonly:`query_rows` 會先經 `get_dataset`,那條
        aiosqlite 連線路徑不在本次修正範圍內(見報告「尚未自動修正」)。
        """
        from health_opendata_mcp.repository.query_executor import (
            QueryExecutionError,
            execute_readonly,
        )

        missing = str(tmp_path / "nonexistent" / "gone.db")
        with pytest.raises(QueryExecutionError) as err:
            execute_readonly(missing, "ds_a", 'SELECT * FROM "ds_a" LIMIT 1', 1)
        assert "unable to open" not in str(err.value)
        assert "nonexistent" not in str(err.value)
        assert str(tmp_path) not in str(err.value)

    async def test_valid_query_still_works(self, service: QueryService) -> None:
        """修正不得改變正常查詢的結果(避免功能退化)。"""
        out = await service.query_rows(
            "ds-a", columns=["title"], where="title = '甲'", order_by="title"
        )
        assert out == {"columns": ["title"], "rows": [["甲"]], "truncated": False}


class TestQueryRowsFragmentLimits:
    """where / columns / group_by / order_by 是自由文字且直接進 SQL 組裝。

    其餘工具參數(keyword、dataset_id、natural_key、job_number)早有 200
    字元上限,唯獨這幾個沒有 —— 呼叫端單次即可送進 MB 級片段或上千個
    column(CWE-20 / CWE-400),先前只由 SQLite 引擎內部限制兜底。
    """

    @pytest.fixture
    async def service(self, tmp_path) -> QueryService:
        repo = SqliteRepository(str(tmp_path / "t.db"))
        await repo.init()
        await repo.upsert_batch(_batch([{"_nk": "k1", "title": "甲"}]))
        return QueryService(repo)

    async def test_oversized_where_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="too long"):
            await service.query_rows("ds-a", where="title = '" + "a" * 3_000 + "'")

    async def test_oversized_order_by_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="too long"):
            await service.query_rows("ds-a", order_by="a" * 3_000)

    @pytest.mark.parametrize("field", ["columns", "group_by"])
    async def test_too_many_items_rejected(
        self, service: QueryService, field: str
    ) -> None:
        with pytest.raises(ValueError, match="too many items"):
            await service.query_rows("ds-a", **{field: ["title"] * 500})

    @pytest.mark.parametrize("field", ["columns", "group_by"])
    async def test_oversized_item_rejected(
        self, service: QueryService, field: str
    ) -> None:
        with pytest.raises(ValueError, match="too long"):
            await service.query_rows("ds-a", **{field: ["a" * 3_000]})

    async def test_oversized_dataset_id_rejected(
        self, service: QueryService
    ) -> None:
        with pytest.raises(ValueError, match="too long"):
            await service.query_rows("d" * 500)

    async def test_realistic_query_is_not_blocked(
        self, service: QueryService
    ) -> None:
        """repo 內最長的真實 where 為 65 字元、最多 2 個 column ——
        上限必須留足餘裕,不得誤擋既有看板查詢。"""
        from health_opendata_mcp.domain.query_guard import (
            MAX_FRAGMENT_ITEMS,
            MAX_FRAGMENT_LEN,
        )

        assert MAX_FRAGMENT_LEN >= 1_000
        assert MAX_FRAGMENT_ITEMS >= 16
        out = await service.query_rows(
            "ds-a",
            columns=["title", "COUNT(*) AS n"],
            where="title IS NOT NULL AND title != ''",
            group_by=["title"],
            order_by="n DESC",
        )
        assert out["rows"] == [["甲", 1]]

    async def test_get_vendor_stats_fixed_where_still_passes(self, tmp_path) -> None:
        """內部固定 where(get_vendor_stats)不得被新上限擋下。"""
        from health_opendata_mcp.domain.query_guard import build_select

        sql, _ = build_select(
            "ds_pcc_tender",
            columns=["companies", "award_price"],
            where=(
                "announcement_type='決標公告'"
                " AND companies IS NOT NULL AND companies != ''"
            ),
            limit=400,
        )
        assert "LIMIT 401" in sql


# ── 7. CI / 供應鏈(CWE-1357 / CWE-78)──────────────────────────────────────


class TestCiSupplyChain:
    """第三方 action 一律釘 commit SHA。

    浮動 tag(@v5)可被上游重新指向 —— 等同把 CI 執行權交給上游 tag 的
    可變性,而這些 job 看得到 GITHUB_TOKEN(pages.yml 另有 pages: write
    與 id-token: write)。姊妹專案 g0vmcp / opendataCampus-MCP 已採此作法
    並在註解中載明理由,本專案先前仍用浮動 tag,此測試鎖住對齊結果。
    """

    _WORKFLOWS = ("ci.yml", "pages.yml", "verify-catalog.yml")

    @staticmethod
    def _text(name: str) -> str:
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[1] / ".github/workflows" / name
        ).read_text(encoding="utf-8")

    @pytest.mark.parametrize("name", _WORKFLOWS)
    def test_actions_are_pinned_to_commit_sha(self, name: str) -> None:
        import re

        uses = re.findall(r"uses:\s*(\S+)", self._text(name))
        assert uses, f"{name} 沒有任何 uses: —— 測試可能找錯檔案"
        for ref in uses:
            assert "@" in ref, f"{name}: {ref} 未指定版本"
            rev = ref.split("@", 1)[1]
            assert re.fullmatch(r"[0-9a-f]{40}", rev), (
                f"{name}: {ref} 使用浮動 ref,必須釘 40 碼 commit SHA"
            )

    @pytest.mark.parametrize("name", _WORKFLOWS)
    def test_declares_least_privilege_token(self, name: str) -> None:
        """未宣告 permissions 時 GITHUB_TOKEN 會沿用 repository 預設。"""
        assert "permissions:" in self._text(name)
        assert "contents: read" in self._text(name)

    @pytest.mark.parametrize("name", _WORKFLOWS)
    def test_no_inputs_interpolated_into_run_blocks(self, name: str) -> None:
        """${{ }} 在 shell 執行前做文字替換;插進 run: 等於讓輸入變成 shell 語法。"""
        import re

        for block in re.findall(r"\n\s+run: \|[\s\S]*?(?=\n\s+- |\n\S|\Z)", self._text(name)):
            assert "${{" not in block, (
                f"{name}: run: 區塊直接內插 ${{{{ }}}},應改以 env: 傳入並加引號"
            )

