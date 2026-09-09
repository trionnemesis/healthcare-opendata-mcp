from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest

from export_board_data import (
    PUBLIC_COLUMNS,
    build_snapshot,
    export_snapshot,
    parse_amount,
    parse_generated_at,
    render_dashboard,
)
from health_opendata_mcp.repository.schema import BASE_SCHEMA

GENERATED_AT = dt.datetime(2026, 9, 1, 8, 0, tzinfo=dt.timezone.utc)


def make_db(path: Path, rows: list[dict] | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(BASE_SCHEMA)
    con.execute(
        "INSERT INTO data_sources"
        " (id, name, platform, access_strategy, config, enabled)"
        " VALUES ('pcc-opendata', 'PCC', 'web.pcc.gov.tw', 'STATIC_FILE', '{}', 1)"
    )
    con.execute(
        "INSERT INTO datasets"
        " (id, source_id, title, schema_json, collection, license, last_fetched_at)"
        " VALUES ('pcc-tender', 'pcc-opendata', 'PCC', '[]', 'procurement',"
        " '政府資料開放授權條款 1.0', '2026-09-01T07:00:00+00:00')"
    )
    con.execute(
        "INSERT INTO datasets"
        " (id, source_id, title, schema_json, collection, license, last_fetched_at)"
        " VALUES ('nhi-clinic', 'nhi-opendata', 'NHI', '[]', 'healthcare',"
        " '政府資料開放授權條款 1.0', '2026-09-01T06:00:00+00:00')"
    )
    all_columns = ("_nk",) + PUBLIC_COLUMNS + (
        "procurement_type",
        "procurement_attr",
        "award_way",
        "notice_date",
    )
    con.execute(
        'CREATE TABLE "ds_pcc_tender" ('
        + ", ".join(f'"{name}" TEXT' for name in all_columns)
        + ")"
    )
    con.execute('CREATE TABLE "ds_nhi_clinic" (_nk TEXT PRIMARY KEY, name TEXT)')
    con.executemany(
        'INSERT INTO "ds_nhi_clinic" VALUES (?, ?)',
        [("nhi-1", "診所 A"), ("nhi-2", "診所 B")],
    )
    for index, row in enumerate(rows or []):
        values = {name: None for name in all_columns}
        values.update(row)
        values["_nk"] = values["_nk"] or f"row-{index}"
        con.execute(
            f'INSERT INTO "ds_pcc_tender" ({", ".join(f"{name!r}" for name in all_columns)})'
            f' VALUES ({", ".join("?" for _ in all_columns)})',
            [values[name] for name in all_columns],
        )
    con.execute(
        "INSERT INTO ingestion_runs"
        " (source_id, started_at, finished_at, status, fetched_count)"
        " VALUES ('pcc-opendata', '2026-09-01T06:00:00+00:00',"
        " '2026-09-01T07:00:00+00:00', 'SUCCEEDED', ?)",
        (len(rows or []),),
    )
    con.execute(
        "INSERT INTO ingestion_runs"
        " (source_id, started_at, finished_at, status, fetched_count)"
        " VALUES ('nhi-opendata', '2026-09-01T05:00:00+00:00',"
        " '2026-09-01T06:00:00+00:00', 'SUCCEEDED', 2)"
    )
    con.commit()
    return con


def sample_rows() -> list[dict]:
    return [
        {
            "_nk": "older",
            "date": "2026-08-20",
            "announcement_type": "決標公告",
            "title": "系統維護",
            "agency": "衛生福利部 A",
            "job_number": "A-1",
            "budget": "",
            "award_price": "1,200",
            "companies": "得標公司",
        },
        {
            "_nk": "newer",
            "date": "2026-09-01",
            "announcement_type": "招標公告",
            "title": '<img src=x onerror="globalThis.pwned=1">',
            "agency": "衛生福利部 B",
            "job_number": "B-2",
            "budget": "2,500",
            "award_price": "unknown",
            "companies": None,
        },
    ]


def test_parse_generated_at_requires_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        parse_generated_at("2026-09-01T08:00:00")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("unknown", None), ("NT$ 1,234元", 1234), ("12.5", 12.5)],
)
def test_parse_amount_preserves_unknown(raw: object, expected: object) -> None:
    assert parse_amount(raw) == expected


def test_snapshot_is_deterministic_safe_and_null_aware(tmp_path: Path) -> None:
    con = make_db(tmp_path / "fixture.db", sample_rows())
    try:
        first = build_snapshot(con, generated_at=GENERATED_AT)
        second = build_snapshot(con, generated_at=GENERATED_AT)
    finally:
        con.close()

    assert first == second
    assert [row["job_number"] for row in first["rows"]] == ["B-2", "A-1"]
    assert first["rows"][0]["title"].startswith("<img")
    assert first["rows"][0]["award_price"] is None
    assert first["rows"][1]["budget"] is None
    assert first["summary"]["pcc_tender"]["budget"] == {
        "known_count": 1,
        "sum_twd": 2500,
    }
    assert first["summary"]["pcc_tender"]["award_amount"] == {
        "known_count": 1,
        "sum_twd": 1200,
    }
    assert first["datasets"]["nhi_clinic"]["row_count"] == 2
    assert first["status"]["state"] == "fresh"


def test_empty_snapshot_is_not_fresh(tmp_path: Path) -> None:
    con = make_db(tmp_path / "empty.db")
    try:
        payload = build_snapshot(con, generated_at=GENERATED_AT)
    finally:
        con.close()
    assert payload["status"]["state"] == "empty"
    assert payload["rows"] == []


def test_partial_source_run_is_degraded_without_publishing_error(tmp_path: Path) -> None:
    con = make_db(tmp_path / "degraded.db", sample_rows())
    con.execute(
        "UPDATE ingestion_runs SET error_detail = 'internal stack /secret/path'"
        " WHERE source_id = 'pcc-opendata'"
    )
    con.commit()
    try:
        payload = build_snapshot(con, generated_at=GENERATED_AT)
    finally:
        con.close()
    assert payload["status"]["state"] == "degraded"
    assert "PCC" in payload["status"]["message"]
    assert "/secret/path" not in json.dumps(payload, ensure_ascii=False)


def test_old_successful_sync_is_stale(tmp_path: Path) -> None:
    """過期判定的基準是 datasets.last_fetched_at,不是 ingestion_runs.finished_at。

    run 是 per-source 的,且 pipeline 對單一 ref 失敗有容錯 —— 一個 SUCCEEDED
    的 run 底下可能有某個 dataset 根本沒更新到。per-dataset 的 last_fetched_at
    才是「這份資料多舊」的答案,故兩者都往回撥才是一致的情境。
    """
    con = make_db(tmp_path / "stale.db", sample_rows())
    con.execute(
        "UPDATE ingestion_runs SET finished_at = '2026-07-01T00:00:00+00:00'"
    )
    con.execute("UPDATE datasets SET last_fetched_at = '2026-07-01T00:00:00+00:00'")
    con.commit()
    try:
        payload = build_snapshot(con, generated_at=GENERATED_AT, stale_after_days=21)
    finally:
        con.close()
    assert payload["status"]["state"] == "stale"
    # pcc-tender 非 catalog 驅動,沿用明列門檻
    assert payload["datasets"]["pcc_tender"]["stale_after_days"] == 21
    assert payload["datasets"]["pcc_tender"]["freshness"] == "stale"


def test_complete_projection_is_measured_before_rows_are_bounded(tmp_path: Path) -> None:
    rows = [
        {
            "date": f"2026-08-{(index % 28) + 1:02d}",
            "announcement_type": "招標公告",
            "title": "大型標題" * 80,
            "agency": "衛生福利部",
            "job_number": f"P-{index:04d}",
        }
        for index in range(80)
    ]
    con = make_db(tmp_path / "large.db", rows)
    try:
        payload = build_snapshot(
            con,
            generated_at=GENERATED_AT,
            detail_limit=20,
            max_bytes=12_000,
        )
    finally:
        con.close()
    assert payload["export"]["strategy"] == "latest_records"
    assert payload["export"]["full_row_count"] == 80
    assert payload["export"]["full_payload_bytes"] > 12_000
    assert payload["export"]["published_payload_bytes"] <= 12_000
    assert len(payload["rows"]) <= 20


def test_rendered_summary_escapes_upstream_aggregate_text(tmp_path: Path) -> None:
    rows = sample_rows()
    rows[0]["announcement_type"] = "<script>alert(1)</script>"
    con = make_db(tmp_path / "escape.db", rows)
    try:
        payload = build_snapshot(con, generated_at=GENERATED_AT)
    finally:
        con.close()
    rendered = render_dashboard(
        '<p>{{ANNOUNCEMENT_TYPE_SUMMARY}}</p><b>{{STATUS_STATE}}</b>', payload
    )
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_failed_build_preserves_existing_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "missing-table.db"
    con = sqlite3.connect(db_path)
    con.executescript(BASE_SCHEMA)
    con.close()
    out = tmp_path / "current.json"
    out.write_text('{"known":"good"}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="ds_pcc_tender"):
        export_snapshot(db_path, out, generated_at=GENERATED_AT)

    assert json.loads(out.read_text(encoding="utf-8")) == {"known": "good"}


class TestCatalogDrivenDatasets:
    """datasets 由 catalog(已啟用者)∪ DB 產生 —— 不硬編碼 dataset 名單。

    回歸重點:先前 build_snapshot 把 pcc_tender / nhi_clinic 寫死在 payload 裡,
    catalog 啟用新資料集後 Pages 看不到它,schema 也會拒收。
    """

    def _snapshot(self, tmp_path: Path, rows=None):
        con = make_db(tmp_path / "matrix.db", rows or sample_rows())
        return build_snapshot(con, generated_at=GENERATED_AT), con

    def test_enabled_catalog_dataset_carries_provenance(self, tmp_path):
        payload, _ = self._snapshot(tmp_path)
        nhi = payload["datasets"]["nhi_clinic"]
        assert nhi["title"] == "健保特約醫事機構-診所"
        assert nhi["collection"] == "healthcare"
        assert nhi["update_cadence"] == "daily"
        assert nhi["verified_at"] == "2026-06-10"
        # 出處來自 catalog,不再是本檔寫死的字串
        assert nhi["source_url"].startswith("https://info.nhi.gov.tw/")
        assert nhi["row_count"] == 2

    def test_disabled_candidates_are_not_published(self, tmp_path):
        from health_opendata_mcp.catalog import CATALOG
        from export_board_data import snapshot_key

        payload, _ = self._snapshot(tmp_path)
        for entry in CATALOG:
            if not entry.enabled:
                # 尚未實查的候選列出來,會讓訪客誤以為已涵蓋
                assert snapshot_key(entry.dataset_id) not in payload["datasets"]

    def test_pcc_is_published_without_catalog_provenance(self, tmp_path):
        payload, _ = self._snapshot(tmp_path)
        pcc = payload["datasets"]["pcc_tender"]
        assert pcc["source_url"] == "https://web.pcc.gov.tw/"
        # PCC 不是 catalog 驅動的,出處欄位留 null,不臆造
        assert pcc["title"] is None
        assert pcc["verified_at"] is None
        assert pcc["row_count"] == len(sample_rows())

    def test_dataset_without_materialized_table_reports_null_not_zero(self, tmp_path):
        con = make_db(tmp_path / "ghost.db", sample_rows())
        con.execute(
            "INSERT INTO datasets (id, source_id, title, schema_json)"
            " VALUES ('ghost-ds', 'nhi-opendata', 'Ghost', '[]')"
        )
        con.commit()
        payload = build_snapshot(con, generated_at=GENERATED_AT)
        ghost = payload["datasets"]["ghost_ds"]
        # 從未同步成功 ≠ 同步成功但 0 筆
        assert ghost["row_count"] is None
        assert ghost["last_fetched_at"] is None

    def test_dataset_keys_are_deterministically_ordered(self, tmp_path):
        payload, _ = self._snapshot(tmp_path)
        keys = list(payload["datasets"])
        assert keys == sorted(keys)

    def test_status_message_names_the_sources_actually_present(self, tmp_path):
        payload, _ = self._snapshot(tmp_path)
        assert payload["status"]["state"] == "fresh"
        # 來源標籤由 DB 反推;fixture 未登錄 nhi-opendata 的 data_sources 列,
        # 因此退回 source_id —— 退回本身也必須是可見的事實
        assert "nhi-opendata" in payload["status"]["message"]
        assert "PCC" in payload["status"]["message"]


class TestRenderDashboardTolerance:
    def test_missing_nhi_dataset_renders_no_data_instead_of_zero(self, tmp_path):
        con = make_db(tmp_path / "nonhi.db", sample_rows())
        payload = build_snapshot(con, generated_at=GENERATED_AT)
        payload["datasets"].pop("nhi_clinic")
        template = Path("scripts/templates/dashboard.html").read_text(encoding="utf-8")
        rendered = render_dashboard(template, payload)
        assert "無資料" in rendered
        assert "{{" not in rendered

    def test_dataset_matrix_container_exists_in_template(self):
        template = Path("scripts/templates/dashboard.html").read_text(encoding="utf-8")
        assert 'id="dataset-matrix-body"' in template


def test_committed_dashboard_matches_template_render() -> None:
    """委入的 docs/dashboard/index.html 必須就是 template + 委入快照的渲染結果。

    兩者是手改容易漂移的一對:template 加了區塊而 artifact 沒重新產生時,
    Pages 上看到的就不是 repo 裡宣稱的那個頁面。
    """
    payload = json.loads(Path("docs/data/current.json").read_text(encoding="utf-8"))
    template = Path("scripts/templates/dashboard.html").read_text(encoding="utf-8")
    committed = Path("docs/dashboard/index.html").read_text(encoding="utf-8")
    assert render_dashboard(template, payload) == committed


class TestFreshnessFromCadence:
    """過期門檻由 catalog 的 update_cadence 推導,不再是全域猜測值(#31 第 2 項)。

    #22 明文要求「不自行推論 healthy／stale 門檻⋯需先建立每個 source 的正式
    cadence 契約」。該契約自 #26 起存在,本組測試釘住它真的被拿來用。
    """

    def test_threshold_is_two_update_cycles(self):
        from export_board_data import CADENCE_STALE_AFTER_DAYS

        assert CADENCE_STALE_AFTER_DAYS["daily"] == 2
        assert CADENCE_STALE_AFTER_DAYS["weekly"] == 14
        assert CADENCE_STALE_AFTER_DAYS["monthly"] == 60
        assert CADENCE_STALE_AFTER_DAYS["quarterly"] == 182
        assert CADENCE_STALE_AFTER_DAYS["yearly"] == 730

    def test_cadence_without_a_cycle_has_no_threshold(self):
        from export_board_data import CADENCE_STALE_AFTER_DAYS

        assert CADENCE_STALE_AFTER_DAYS["irregular"] is None
        assert CADENCE_STALE_AFTER_DAYS["unknown"] is None

    def test_every_catalog_cadence_has_a_policy(self):
        """catalog 新增 cadence 而未定政策時,import 就該失敗而不是靜默放行。"""
        from export_board_data import CADENCE_STALE_AFTER_DAYS
        from health_opendata_mcp.catalog import UPDATE_CADENCES

        assert set(CADENCE_STALE_AFTER_DAYS) == UPDATE_CADENCES

    def test_non_catalog_dataset_keeps_its_explicit_threshold(self):
        from export_board_data import stale_after_days_for

        assert stale_after_days_for("pcc-tender", None) == 21
        # 旗標只影響非 catalog 驅動者
        assert stale_after_days_for("pcc-tender", None, non_catalog_default=5) == 5

    def test_catalog_dataset_ignores_the_non_catalog_flag(self):
        from export_board_data import stale_after_days_for

        assert stale_after_days_for("nhi-clinic", "daily", non_catalog_default=999) == 2

    def test_dataset_without_a_cadence_contract_has_no_threshold(self):
        from export_board_data import stale_after_days_for

        assert stale_after_days_for("whatever", None) is None
        assert stale_after_days_for("whatever", "unknown") is None
        assert stale_after_days_for("whatever", "irregular") is None


class TestFreshnessFor:
    def test_within_threshold_is_fresh(self):
        from export_board_data import freshness_for

        assert freshness_for("2026-09-01T00:00:00+00:00", 2, GENERATED_AT) == "fresh"

    def test_beyond_threshold_is_stale(self):
        from export_board_data import freshness_for

        assert freshness_for("2026-08-20T00:00:00+00:00", 2, GENERATED_AT) == "stale"

    def test_no_threshold_is_never_stale_however_old(self):
        """沒有 cadence 契約就沒有判定依據 —— 再舊也不得宣稱過期。"""
        from export_board_data import freshness_for

        assert freshness_for("2000-01-01T00:00:00+00:00", None, GENERATED_AT) == "unknown"

    def test_missing_fetch_time_is_unknown(self):
        from export_board_data import freshness_for

        assert freshness_for(None, 2, GENERATED_AT) == "unknown"

    def test_malformed_fetch_time_is_unknown_not_fresh(self):
        from export_board_data import freshness_for

        assert freshness_for("not-a-timestamp", 2, GENERATED_AT) == "unknown"
        # 無時區的時間戳無法比較,同樣沒有依據
        assert freshness_for("2026-09-01T00:00:00", 2, GENERATED_AT) == "unknown"


class TestStatusRollup:
    def _db(self, tmp_path, name="rollup.db"):
        return make_db(tmp_path / name, sample_rows())

    def test_catalog_dataset_past_its_own_threshold_makes_the_page_stale(self, tmp_path):
        con = self._db(tmp_path)
        # nhi-clinic 的 cadence 是 daily → 門檻 2 天;往回撥 5 天
        con.execute(
            "UPDATE datasets SET last_fetched_at = '2026-08-27T00:00:00+00:00'"
            " WHERE id = 'nhi-clinic'"
        )
        con.commit()
        payload = build_snapshot(con, generated_at=GENERATED_AT)
        assert payload["datasets"]["nhi_clinic"]["stale_after_days"] == 2
        assert payload["datasets"]["nhi_clinic"]["freshness"] == "stale"
        assert payload["status"]["state"] == "stale"
        # 訊息要指名是哪個資料集、依據什麼門檻
        assert "nhi_clinic" in payload["status"]["message"]
        assert "2 天" in payload["status"]["message"]
        # 全域門檻 21 天不再是判定依據:5 天 < 21 卻仍判為過期
        assert payload["datasets"]["pcc_tender"]["freshness"] == "fresh"

    def test_dataset_without_a_contract_never_drags_the_page_stale(self, tmp_path):
        con = self._db(tmp_path, "ghost.db")
        con.execute(
            "INSERT INTO datasets (id, source_id, title, schema_json, last_fetched_at)"
            " VALUES ('ghost-ds', 'nhi-opendata', 'Ghost', '[]',"
            " '2000-01-01T00:00:00+00:00')"
        )
        con.commit()
        payload = build_snapshot(con, generated_at=GENERATED_AT)
        ghost = payload["datasets"]["ghost_ds"]
        assert ghost["stale_after_days"] is None
        assert ghost["freshness"] == "unknown"
        assert payload["status"]["state"] == "fresh"

    def test_degraded_still_takes_precedence_over_stale(self, tmp_path):
        con = self._db(tmp_path, "degraded.db")
        con.execute("UPDATE ingestion_runs SET status = 'FAILED'")
        con.execute("UPDATE datasets SET last_fetched_at = '2026-01-01T00:00:00+00:00'")
        con.commit()
        payload = build_snapshot(con, generated_at=GENERATED_AT)
        assert payload["status"]["state"] == "degraded"

    def test_freshness_is_published_for_every_dataset(self, tmp_path):
        payload = build_snapshot(self._db(tmp_path, "all.db"), generated_at=GENERATED_AT)
        for key, meta in payload["datasets"].items():
            assert meta["freshness"] in {"fresh", "stale", "unknown"}, key
            assert "stale_after_days" in meta, key
