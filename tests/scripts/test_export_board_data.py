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
    con = make_db(tmp_path / "stale.db", sample_rows())
    con.execute(
        "UPDATE ingestion_runs SET finished_at = '2026-07-01T00:00:00+00:00'"
    )
    con.commit()
    try:
        payload = build_snapshot(con, generated_at=GENERATED_AT, stale_after_days=21)
    finally:
        con.close()
    assert payload["status"]["state"] == "stale"


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
