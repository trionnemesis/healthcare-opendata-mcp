"""Build the versioned public GitHub Pages snapshot from an hcmcp SQLite DB.

The database remains the source of truth. This module creates a bounded,
sanitized read model for the static dashboard; it never publishes SQLite or
raw ingestion errors. Pure build/render functions are separated from the
atomic file-writing boundary so the contract stays directly testable.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_DETAIL_LIMIT = 1_000
DEFAULT_STALE_AFTER_DAYS = 21
VALID_STATES = {"fresh", "stale", "degraded", "empty"}

PUBLIC_COLUMNS = (
    "date",
    "announcement_type",
    "title",
    "agency",
    "job_number",
    "bid_deadline",
    "open_date",
    "budget",
    "award_price",
    "companies",
)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def parse_generated_at(value: str | None) -> dt.datetime:
    """Return an aware UTC timestamp; reject ambiguous local timestamps."""
    if value is None:
        return _utc_now()
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--generated-at must include a timezone")
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0)


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_amount(value: Any) -> int | float | None:
    """Convert a known numeric amount while preserving missing/unknown as null."""
    text = _text_or_none(value)
    if text is None:
        return None
    normalized = (
        text.replace(",", "")
        .replace("NT$", "")
        .replace("nt$", "")
        .replace("$", "")
        .replace("元", "")
        .strip()
    )
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    integral = amount.to_integral_value()
    return int(integral) if amount == integral else float(amount)


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _dataset_metadata(con: sqlite3.Connection, dataset_id: str) -> dict[str, Any]:
    row = con.execute(
        "SELECT last_fetched_at, license FROM datasets WHERE id = ?", (dataset_id,)
    ).fetchone()
    return {
        "last_fetched_at": row[0] if row else None,
        "license": row[1] if row else None,
    }


def _latest_run(
    con: sqlite3.Connection, source_id: str
) -> tuple[str | None, str | None, bool]:
    row = con.execute(
        "SELECT status, finished_at, error_detail FROM ingestion_runs"
        " WHERE source_id = ? ORDER BY id DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    return (row[0], row[1], bool(row[2])) if row else (None, None, False)


def _public_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "date": _text_or_none(row["date"]),
        "announcement_type": _text_or_none(row["announcement_type"]),
        "title": _text_or_none(row["title"]),
        "agency": _text_or_none(row["agency"]),
        "job_number": _text_or_none(row["job_number"]),
        "bid_deadline": _text_or_none(row["bid_deadline"]),
        "open_date": _text_or_none(row["open_date"]),
        "budget": parse_amount(row["budget"]),
        "award_price": parse_amount(row["award_price"]),
        "companies": _text_or_none(row["companies"]),
    }


def _sum_known(rows: Iterable[dict[str, Any]], field: str) -> dict[str, Any]:
    known = [row[field] for row in rows if row[field] is not None]
    return {"known_count": len(known), "sum_twd": sum(known) if known else None}


def _derive_status(
    *,
    row_count: int,
    source_max_date: str | None,
    source_runs: list[tuple[str, str | None, str | None, bool]],
    generated_at: dt.datetime,
    stale_after_days: int,
) -> dict[str, Any]:
    if row_count == 0:
        return {
            "state": "empty",
            "source_max_date": None,
            "message": "快照沒有 PCC 資料；未以空資料呈現成功狀態。",
        }
    incomplete = [
        name
        for name, status, _finished_at, has_errors in source_runs
        if status != "SUCCEEDED" or has_errors
    ]
    if incomplete:
        return {
            "state": "degraded",
            "source_max_date": source_max_date,
            "message": (
                f"最近一次 {'／'.join(incomplete)} 同步不完整；"
                "目前顯示可驗證的既有資料。"
            ),
        }
    missing_finished = [
        name for name, _status, finished_at, _has_errors in source_runs if not finished_at
    ]
    if missing_finished:
        return {
            "state": "degraded",
            "source_max_date": source_max_date,
            "message": (
                f"{'／'.join(missing_finished)} 同步完成時間缺失；"
                "目前顯示可驗證的既有資料。"
            ),
        }
    try:
        finished_times = []
        for _name, _status, finished_at_text, _has_errors in source_runs:
            assert finished_at_text is not None
            finished_at = dt.datetime.fromisoformat(
                finished_at_text.replace("Z", "+00:00")
            )
            if finished_at.tzinfo is None:
                raise ValueError("missing timezone")
            finished_times.append(finished_at.astimezone(dt.timezone.utc))
        age_days = (generated_at - min(finished_times)).days
        if age_days < 0:
            return {
                "state": "degraded",
                "source_max_date": source_max_date,
                "message": "來源同步完成時間晚於快照時間，請檢查資料時鐘。",
            }
        if age_days > stale_after_days:
            return {
                "state": "stale",
                "source_max_date": source_max_date,
                "message": f"最近一次來源成功同步已超過 {stale_after_days} 天。",
            }
    except (TypeError, ValueError):
        return {
            "state": "degraded",
            "source_max_date": source_max_date,
            "message": "來源同步完成時間格式無法驗證；目前顯示可驗證的既有資料。",
        }
    return {
        "state": "fresh",
        "source_max_date": source_max_date,
        "message": "快照由最近一次成功同步的 PCC／NHI 資料庫重建；公告日期依目前資料範圍呈現。",
    }


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    ) + "\n"


def _payload_size(payload: dict[str, Any]) -> int:
    return len(_json_text(payload).encode("utf-8"))


def _stabilize_size_field(payload: dict[str, Any], field: str) -> int:
    """Set a byte-size field until the serialized size reaches a fixed point."""
    previous = -1
    for _ in range(8):
        current = _payload_size(payload)
        payload["export"][field] = current
        if current == previous:
            return current
        previous = current
    return _payload_size(payload)


def build_snapshot(
    con: sqlite3.Connection,
    *,
    generated_at: dt.datetime,
    detail_limit: int = DEFAULT_DETAIL_LIMIT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
) -> dict[str, Any]:
    """Build a deterministic public projection for a fixed ``generated_at``."""
    if detail_limit < 1:
        raise ValueError("detail_limit must be at least 1")
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if stale_after_days < 0:
        raise ValueError("stale_after_days cannot be negative")
    if not _table_exists(con, "ds_pcc_tender"):
        raise RuntimeError("required table ds_pcc_tender is missing")

    con.row_factory = sqlite3.Row
    columns = ", ".join(f'"{name}"' for name in PUBLIC_COLUMNS)
    full_rows = [
        _public_row(row)
        for row in con.execute(
            f'SELECT {columns} FROM "ds_pcc_tender"'
            ' ORDER BY "date" DESC, "announcement_type" ASC,'
            ' "agency" ASC, "job_number" ASC, "_nk" ASC'
        ).fetchall()
    ]
    total_count = len(full_rows)
    dates = [row["date"] for row in full_rows if row["date"]]
    source_max_date = max(dates) if dates else None
    source_min_date = min(dates) if dates else None
    pcc_meta = _dataset_metadata(con, "pcc-tender")
    pcc_run_status, pcc_run_finished_at, pcc_run_has_errors = _latest_run(
        con, "pcc-opendata"
    )

    nhi_count = 0
    if _table_exists(con, "ds_nhi_clinic"):
        nhi_count = con.execute('SELECT COUNT(*) FROM "ds_nhi_clinic"').fetchone()[0]
    nhi_meta = _dataset_metadata(con, "nhi-clinic")
    nhi_run_status, nhi_run_finished_at, nhi_run_has_errors = _latest_run(
        con, "nhi-opendata"
    )

    type_counts = Counter(
        row["announcement_type"]
        for row in full_rows
        if row["announcement_type"] is not None
    )
    agencies = sorted(
        {row["agency"] for row in full_rows if row["agency"] is not None}
    )
    status = _derive_status(
        row_count=total_count,
        source_max_date=source_max_date,
        source_runs=[
            ("PCC", pcc_run_status, pcc_run_finished_at, pcc_run_has_errors),
            ("NHI", nhi_run_status, nhi_run_finished_at, nhi_run_has_errors),
        ],
        generated_at=generated_at,
        stale_after_days=stale_after_days,
    )

    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "status": status,
        "datasets": {
            "pcc_tender": {
                "row_count": total_count,
                "last_fetched_at": pcc_meta["last_fetched_at"],
                "latest_run_status": pcc_run_status,
                "latest_run_finished_at": pcc_run_finished_at,
                "source_url": "https://web.pcc.gov.tw/",
                "license": pcc_meta["license"],
            },
            "nhi_clinic": {
                "row_count": nhi_count,
                "last_fetched_at": nhi_meta["last_fetched_at"],
                "latest_run_status": nhi_run_status,
                "latest_run_finished_at": nhi_run_finished_at,
                "source_url": "https://info.nhi.gov.tw/",
                "license": nhi_meta["license"],
            },
        },
        "summary": {
            "pcc_tender": {
                "snapshot_row_count": total_count,
                "date_range": {"min": source_min_date, "max": source_max_date},
                "announcement_types": [
                    {"name": name, "count": type_counts[name]}
                    for name in sorted(type_counts)
                ],
                "budget": _sum_known(full_rows, "budget"),
                "award_amount": _sum_known(full_rows, "award_price"),
            }
        },
        "filters": {
            "announcement_types": sorted(type_counts),
            "agencies": agencies,
        },
        "export": {
            "strategy": "all",
            "max_bytes": max_bytes,
            "detail_limit": detail_limit,
            "full_row_count": total_count,
            "full_payload_bytes": 0,
            "published_payload_bytes": 0,
        },
        "rows": full_rows,
    }

    # Measure the complete projection before applying any detail bound.
    full_size = _stabilize_size_field(base, "full_payload_bytes")
    if full_size > max_bytes:
        base["export"]["strategy"] = "latest_records"
        base["rows"] = full_rows[: min(detail_limit, total_count)]
        base["summary"]["pcc_tender"]["snapshot_row_count"] = len(base["rows"])
        while base["rows"] and _payload_size(base) > max_bytes:
            base["rows"] = base["rows"][: len(base["rows"]) // 2]
            base["summary"]["pcc_tender"]["snapshot_row_count"] = len(
                base["rows"]
            )

    _stabilize_size_field(base, "published_payload_bytes")
    if _payload_size(base) > max_bytes:
        raise ValueError("snapshot metadata exceeds max_bytes even without detail rows")
    return base


def _format_datetime(value: str | None) -> str:
    return value or "無法確認"


def _format_money(value: int | float | None) -> str:
    return "無可用金額" if value is None else f"NT$ {value:,.0f}"


def render_dashboard(template: str, payload: dict[str, Any]) -> str:
    """Render the no-JS core summary; record rows remain only in current.json."""
    summary = payload["summary"]["pcc_tender"]
    datasets = payload["datasets"]
    type_summary = " · ".join(
        f'{item["name"]} {item["count"]:,}'
        for item in summary["announcement_types"]
    ) or "沒有公告類型資料"
    replacements = {
        "STATUS_STATE": payload["status"]["state"],
        "STATUS_MESSAGE": payload["status"]["message"],
        "GENERATED_AT": _format_datetime(payload["generated_at"]),
        "SOURCE_MAX_DATE": payload["status"]["source_max_date"] or "無法確認",
        "PCC_ROW_COUNT": f'{datasets["pcc_tender"]["row_count"]:,}',
        "SNAPSHOT_ROW_COUNT": f'{summary["snapshot_row_count"]:,}',
        "DATE_RANGE": (
            f'{summary["date_range"]["min"] or "無法確認"} – '
            f'{summary["date_range"]["max"] or "無法確認"}'
        ),
        "ANNOUNCEMENT_TYPE_SUMMARY": type_summary,
        "BUDGET_SUMMARY": _format_money(summary["budget"]["sum_twd"]),
        "BUDGET_KNOWN_COUNT": f'{summary["budget"]["known_count"]:,}',
        "AWARD_SUMMARY": _format_money(summary["award_amount"]["sum_twd"]),
        "AWARD_KNOWN_COUNT": f'{summary["award_amount"]["known_count"]:,}',
        "NHI_ROW_COUNT": f'{datasets["nhi_clinic"]["row_count"]:,}',
        "NHI_FETCHED_AT": _format_datetime(datasets["nhi_clinic"]["last_fetched_at"]),
    }
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace("{{" + key + "}}", html.escape(str(value)))
    unresolved = re.findall(r"{{[A-Z0-9_]+}}", rendered)
    if unresolved:
        raise ValueError(f"unresolved dashboard placeholders: {sorted(set(unresolved))}")
    return rendered


def atomic_write_many(files: dict[Path, str]) -> None:
    """Fully materialize every temp file before replacing any destination."""
    temp_paths: dict[Path, Path] = {}
    try:
        for destination, content in files.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temp_paths[destination] = Path(handle.name)
        for destination, temporary in temp_paths.items():
            os.replace(temporary, destination)
        temp_paths.clear()
    finally:
        for temporary in temp_paths.values():
            temporary.unlink(missing_ok=True)


def export_snapshot(
    db_path: str | Path,
    out_path: str | Path,
    *,
    template_path: str | Path | None = None,
    dashboard_out: str | Path | None = None,
    generated_at: dt.datetime | None = None,
    detail_limit: int = DEFAULT_DETAIL_LIMIT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        payload = build_snapshot(
            con,
            generated_at=generated_at or _utc_now(),
            detail_limit=detail_limit,
            max_bytes=max_bytes,
            stale_after_days=stale_after_days,
        )
    finally:
        con.close()

    outputs = {Path(out_path): _json_text(payload)}
    if (template_path is None) != (dashboard_out is None):
        raise ValueError("template_path and dashboard_out must be provided together")
    if template_path is not None and dashboard_out is not None:
        template = Path(template_path).read_text(encoding="utf-8")
        outputs[Path(dashboard_out)] = render_dashboard(template, payload)
    atomic_write_many(outputs)
    return payload


def export(db_path: str, out_path: str) -> int:
    """Compatibility wrapper for callers that only need the JSON snapshot."""
    return len(export_snapshot(db_path, out_path)["rows"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="從 hcmcp SQLite 匯出 versioned GitHub Pages JSON 快照"
    )
    parser.add_argument("--db", required=True, help="hcmcp SQLite DB 路徑")
    parser.add_argument("--out", required=True, help="current.json 輸出路徑")
    parser.add_argument("--template", help="dashboard HTML template 路徑")
    parser.add_argument("--dashboard-out", help="產生的 dashboard HTML 路徑")
    parser.add_argument("--generated-at", help="可重建測試用 ISO-8601 時間（需時區）")
    parser.add_argument("--detail-limit", type=int, default=DEFAULT_DETAIL_LIMIT)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--stale-after-days", type=int, default=DEFAULT_STALE_AFTER_DAYS)
    args = parser.parse_args()

    payload = export_snapshot(
        args.db,
        args.out,
        template_path=args.template,
        dashboard_out=args.dashboard_out,
        generated_at=parse_generated_at(args.generated_at),
        detail_limit=args.detail_limit,
        max_bytes=args.max_bytes,
        stale_after_days=args.stale_after_days,
    )
    print(
        "exported "
        f'{payload["summary"]["pcc_tender"]["snapshot_row_count"]} '
        f'of {payload["datasets"]["pcc_tender"]["row_count"]} PCC rows '
        f'({payload["export"]["published_payload_bytes"]} bytes) → {args.out}'
    )


if __name__ == "__main__":
    main()
