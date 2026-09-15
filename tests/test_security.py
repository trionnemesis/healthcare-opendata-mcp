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


# ── 5. query_rows 輸入長度護欄(CWE-20 / CWE-1284)────────────────────────────


class TestQueryRowsInputBounds:
    """search_records / get_record 早有長度護欄,query_rows 先前沒有。

    where / order_by 是會被接進 SQL 文字的自由字串,columns / group_by 是
    識別符清單 —— 不設限時單一呼叫就能送進 MB 級片段給禁用語法掃描與
    sqlite parser。護欄擋在 SQL 組裝之前,authorizer 仍是最終防線。
    """

    @pytest.fixture
    async def service(self, tmp_path) -> QueryService:
        repo = SqliteRepository(str(tmp_path / "t.db"))
        await repo.init()
        return QueryService(repo)

    async def test_oversized_where_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="where too long"):
            await service.query_rows("pcc-tender", where="a=1 AND " + "x" * 2001)

    async def test_oversized_order_by_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="order_by too long"):
            await service.query_rows("pcc-tender", order_by="x" * 2001)

    async def test_oversized_column_name_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="columns too long"):
            await service.query_rows("pcc-tender", columns=["x" * 201])

    async def test_too_many_columns_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="columns too many entries"):
            await service.query_rows("pcc-tender", columns=[f"c{i}" for i in range(101)])

    async def test_too_many_group_by_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="group_by too many entries"):
            await service.query_rows(
                "pcc-tender", group_by=[f"c{i}" for i in range(101)]
            )

    async def test_oversized_dataset_id_rejected(self, service: QueryService) -> None:
        with pytest.raises(ValueError, match="dataset_id too long"):
            await service.query_rows("x" * 201)

    async def test_normal_query_shape_still_reaches_the_repository(
        self, service: QueryService
    ) -> None:
        """護欄不得誤殺正常查詢:此處應走到「dataset 不存在」而非長度錯誤。"""
        with pytest.raises(ValueError, match="dataset 不存在"):
            await service.query_rows(
                "no-such-dataset",
                columns=["case_no", "title"],
                where="announcement_type='決標公告' AND award_price > 0",
                order_by="case_no DESC",
                limit=50,
            )


# ── 6. GitHub Actions expression injection(CWE-78)──────────────────────────


class TestWorkflowExpressionInjection:
    """workflow_dispatch 的 inputs 不得直接展開進 run: 的腳本文字。

    先前 verify-catalog.yml 是
      `${{ inputs.only && format('--only {0}', inputs.only) || '' }}`,
    只要能觸發 workflow 就等於任意指令執行。修正為經 env 傳入並在 shell 內引號化。
    """

    @staticmethod
    def _run_blocks(text: str) -> list[str]:
        """抓出每個 `run:` 區塊的內容(以縮排判斷邊界)。"""
        import re

        blocks: list[str] = []
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*)run: \|", line)
            if not m:
                inline = re.match(r"^\s*run: (.+)$", line)
                if inline:
                    blocks.append(inline.group(1))
                continue
            indent = len(m.group(1))
            body: list[str] = []
            for nxt in lines[i + 1:]:
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                    break
                body.append(nxt)
            blocks.append("\n".join(body))
        return blocks

    @pytest.mark.parametrize(
        "workflow",
        ["ci.yml", "pages.yml", "verify-catalog.yml"],
    )
    def test_no_interpolation_inside_run_blocks(self, workflow: str) -> None:
        from pathlib import Path

        text = Path(".github/workflows") / workflow
        for block in self._run_blocks(text.read_text(encoding="utf-8")):
            assert "${{" not in block, (
                f"{workflow}: run: 區塊內出現 ${{{{ }}}} 展開,"
                "請改以 env: 傳入並在 shell 內引號化"
            )

    def test_verify_catalog_passes_inputs_through_env(self) -> None:
        from pathlib import Path

        text = (Path(".github/workflows") / "verify-catalog.yml").read_text(
            encoding="utf-8"
        )
        assert "ONLY: ${{ inputs.only }}" in text
        assert "ENABLED_ONLY: ${{ inputs.enabled_only }}" in text
        # 引號化後才不會被 word splitting 拆成多個參數
        assert '--only "$ONLY"' in text


# ── 7. Kubernetes 容器強化(CWE-250 / CWE-400)───────────────────────────────


class TestKubernetesHardening:
    """deploy/k8s 的護欄不可在日後編輯中被摘掉。"""

    @staticmethod
    def _load(name: str) -> list[dict]:
        import pathlib

        import yaml

        text = (pathlib.Path("deploy/k8s") / name).read_text(encoding="utf-8")
        return [d for d in yaml.safe_load_all(text) if d]

    @staticmethod
    def _pod_spec(doc: dict) -> dict:
        if doc["kind"] == "Deployment":
            return doc["spec"]["template"]["spec"]
        return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]

    @pytest.mark.parametrize(
        ("filename", "container"),
        [("deployment.yaml", "hcmcp"), ("cronjob-sync.yaml", "sync")],
    )
    def test_own_image_containers_have_readonly_rootfs(
        self, filename: str, container: str
    ) -> None:
        spec = self._pod_spec(self._load(filename)[0])
        named = {
            c["name"]: c
            for c in spec.get("containers", []) + spec.get("initContainers", [])
        }
        assert named[container]["securityContext"]["readOnlyRootFilesystem"] is True

    @pytest.mark.parametrize(
        "filename", ["deployment.yaml", "cronjob-sync.yaml"]
    )
    def test_every_container_has_memory_limits(self, filename: str) -> None:
        """沒有上限時單一 pod 可以吃光 node(CWE-400)。"""
        spec = self._pod_spec(self._load(filename)[0])
        for c in spec.get("containers", []) + spec.get("initContainers", []):
            limits = c.get("resources", {}).get("limits", {})
            assert "memory" in limits, f"{filename}:{c['name']} 缺 memory limit"

    @pytest.mark.parametrize(
        "filename", ["deployment.yaml", "cronjob-sync.yaml"]
    )
    def test_every_container_drops_all_capabilities(self, filename: str) -> None:
        spec = self._pod_spec(self._load(filename)[0])
        for c in spec.get("containers", []) + spec.get("initContainers", []):
            sc = c["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False, c["name"]
            assert sc["capabilities"]["drop"] == ["ALL"], c["name"]

    def test_readonly_rootfs_containers_get_a_writable_tmp(self) -> None:
        """唯讀 rootfs 若沒有可寫 /tmp,sqlite 暫存檔會讓容器在執行期才爆。"""
        for filename, container in (
            ("deployment.yaml", "hcmcp"),
            ("cronjob-sync.yaml", "sync"),
        ):
            spec = self._pod_spec(self._load(filename)[0])
            named = {
                c["name"]: c
                for c in spec.get("containers", []) + spec.get("initContainers", [])
            }
            mounts = {m["mountPath"] for m in named[container]["volumeMounts"]}
            assert "/tmp" in mounts, f"{filename}:{container} 缺可寫的 /tmp"
            assert any(v["name"] == "tmp" for v in spec["volumes"])

    def test_network_policy_scopes_ingress_to_the_server_port(self) -> None:
        """MCP server 本身無認證,ClusterIP 等於同叢集全開;網路層要補這道門。"""
        policy = self._load("networkpolicy.yaml")[0]
        assert policy["kind"] == "NetworkPolicy"
        assert policy["spec"]["podSelector"]["matchLabels"] == {"app": "hcmcp"}
        assert policy["spec"]["policyTypes"] == ["Ingress"]
        ports = policy["spec"]["ingress"][0]["ports"]
        assert ports == [{"protocol": "TCP", "port": 8000}]


# ── 8. query_rows 注入防線矩陣(CWE-89)──────────────────────────────────────


class TestQueryRowsInjectionMatrix:
    """query_rows 的 where/order_by 是自由 SQL 片段,防線分兩層:

      layer 1  query_guard 的禁用語法掃描(擋 ; -- pragma attach 等)
      layer 2  mode=ro 連線 + sqlite authorizer 白名單(只准讀那一張物化表)

    layer 1 是黑名單,本來就擋不住 UNION / 子查詢 —— 這正是 layer 2 存在的理由。
    本測試把「兩層加起來有擋住」釘死,避免日後有人以為 layer 1 夠用而拿掉 layer 2。
    """

    @pytest.fixture
    def db(self, tmp_path) -> str:
        import sqlite3

        path = str(tmp_path / "m.db")
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE ds_public(id INTEGER, title TEXT);"
            "INSERT INTO ds_public VALUES (1,'public row');"
            "CREATE TABLE secrets(token TEXT);"
            "INSERT INTO secrets VALUES ('SUPER-SECRET-TOKEN');"
        )
        con.commit()
        con.close()
        return path

    @pytest.mark.parametrize(
        "fragment",
        [
            pytest.param({"where": "1=1; DROP TABLE secrets"}, id="terminator"),
            pytest.param({"where": "1=1 --"}, id="comment-truncation"),
            pytest.param(
                {"where": "1=1 UNION SELECT token FROM secrets"}, id="union"
            ),
            pytest.param(
                {"where": "(SELECT token FROM secrets) IS NOT NULL"}, id="subquery"
            ),
            pytest.param(
                {"where": "1=1 UNION SELECT name FROM sqlite_master"},
                id="schema-enumeration",
            ),
            pytest.param(
                {"where": "1=1 AND pragma_table_info('secrets')"}, id="pragma"
            ),
            pytest.param(
                {"where": "load_extension('/tmp/x.so')"}, id="load-extension"
            ),
            pytest.param(
                {"where": "1=1 ATTACH DATABASE '/etc/passwd' AS p"}, id="attach"
            ),
            pytest.param(
                {"where": "1=1; UPDATE ds_public SET title='x'"}, id="write"
            ),
            pytest.param(
                {
                    "order_by": "(WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL "
                    "SELECT x+1 FROM c) SELECT count(*) FROM c)"
                },
                id="recursive-cte",
            ),
        ],
    )
    def test_attack_never_reaches_data_outside_the_allowed_table(
        self, db: str, fragment: dict
    ) -> None:
        import sqlite3

        from health_opendata_mcp.domain.query_guard import (
            QueryValidationError,
            build_select,
        )
        from health_opendata_mcp.repository.query_executor import (
            QueryDeniedError,
            execute_readonly,
        )

        try:
            sql, effective = build_select("ds_public", limit=5, **fragment)
        except QueryValidationError:
            return  # layer 1 擋下
        with pytest.raises((QueryDeniedError, sqlite3.DatabaseError)):
            execute_readonly(db, "ds_public", sql, effective)  # layer 2 擋下

    def test_legitimate_query_still_returns_rows(self, db: str) -> None:
        """防線不得誤殺正常查詢。"""
        from health_opendata_mcp.domain.query_guard import build_select
        from health_opendata_mcp.repository.query_executor import execute_readonly

        sql, effective = build_select(
            "ds_public", columns=["title"], where="id=1", limit=5
        )
        assert execute_readonly(db, "ds_public", sql, effective).rows == (
            ("public row",),
        )

    def test_secret_table_is_never_readable_even_directly(self, db: str) -> None:
        """authorizer 白名單是以「允許的那張表」為界,不是以語法為界。"""
        import sqlite3

        from health_opendata_mcp.domain.query_guard import build_select
        from health_opendata_mcp.repository.query_executor import (
            QueryDeniedError,
            execute_readonly,
        )

        sql, effective = build_select("secrets", limit=5)
        with pytest.raises((QueryDeniedError, sqlite3.DatabaseError)):
            execute_readonly(db, "ds_public", sql, effective)
