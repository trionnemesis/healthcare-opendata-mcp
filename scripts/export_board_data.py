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

from health_opendata_mcp.catalog import UPDATE_CADENCES, enabled_entries

SCHEMA_VERSION = "1.0"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_DETAIL_LIMIT = 1_000
# 非 catalog 驅動的資料集門檻。PCC 是半月檔(每月兩次,約 15 天一輪),
# 21 天 ≈ 1.4 輪;此值沿用自 P0,刻意維持不變以免改動既有頁面的狀態判定。
DEFAULT_STALE_AFTER_DAYS = 21
NON_CATALOG_STALE_AFTER_DAYS = {"pcc-tender": DEFAULT_STALE_AFTER_DAYS}

# 每個 cadence 的名目更新週期(天)。這是對官方更新節奏的描述,不是政策。
_CADENCE_CYCLE_DAYS = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
    "quarterly": 91,
    "yearly": 365,
}
# 沒有週期可推導者 —— 沒有判定依據,永不判為 stale。
_CADENCE_WITHOUT_CYCLE = frozenset({"irregular", "unknown"})

# catalog 新增了 cadence 卻沒有同時決定它的門檻政策,在此就失敗。
# 若讓它靜默落到「無週期」,一個真的有節奏的資料集會永遠不被判為過期。
_uncovered = UPDATE_CADENCES - set(_CADENCE_CYCLE_DAYS) - _CADENCE_WITHOUT_CYCLE
if _uncovered:
    raise RuntimeError(
        f"catalog 新增了 update_cadence 但未定門檻政策: {sorted(_uncovered)}"
    )

# 「多久沒更新算過期」是**政策**,而 catalog 的 update_cadence 是**事實**。
# 規則:門檻 = 2 × 更新週期。漏掉一次更新是時序造成的常態;漏掉兩次代表出事了。
CADENCE_STALE_AFTER_DAYS: dict[str, int | None] = {
    **{c: days * 2 for c, days in _CADENCE_CYCLE_DAYS.items()},
    **{c: None for c in _CADENCE_WITHOUT_CYCLE},
}

FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
# 「沒有新鮮度契約」與「還很新」是兩件事。unknown 必須能在 UI 上被看見,
# 否則無契約的資料集會被誤讀成已驗證為最新。
FRESHNESS_UNKNOWN = "unknown"
VALID_FRESHNESS = {FRESHNESS_FRESH, FRESHNESS_STALE, FRESHNESS_UNKNOWN}
VALID_STATES = {"fresh", "stale", "degraded", "empty"}

# PCC 不是 catalog 驅動(半月 XML + 關鍵字過濾,見 catalog.py),故其出處在此明列。
# 其餘資料集的出處一律來自 catalog,不在本檔重複維護 —— 重複就會漂移。
NON_CATALOG_SOURCE_URLS = {"pcc-tender": "https://web.pcc.gov.tw/"}

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
        "SELECT last_fetched_at, license, source_id FROM datasets WHERE id = ?",
        (dataset_id,),
    ).fetchone()
    return {
        "last_fetched_at": row[0] if row else None,
        "license": row[1] if row else None,
        "source_id": row[2] if row else None,
    }


def snapshot_key(dataset_id: str) -> str:
    """dataset_id → snapshot 的 datasets key(與 ds_ 物化表同一套正規化)。"""
    return re.sub(r"[^a-z0-9_]", "_", dataset_id.lower())


def _row_count(con: sqlite3.Connection, dataset_id: str) -> int | None:
    """物化表不存在 → None(從未同步成功),不是 0。缺值不折成 0。"""
    table = "ds_" + snapshot_key(dataset_id)
    if not _table_exists(con, table):
        return None
    # 表名由 dataset_id 經 snapshot_key() 正規化而來,非使用者輸入;
    # SQLite 無法參數化表名。
    return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]  # nosec B608


def _source_label(con: sqlite3.Connection, source_id: str) -> str:
    row = con.execute(
        "SELECT name FROM data_sources WHERE id = ?", (source_id,)
    ).fetchone()
    return (row[0] if row and row[0] else source_id)


def published_dataset_ids(con: sqlite3.Connection) -> list[str]:
    """要發布的資料集 = catalog 已啟用者 ∪ DB 既有者 ∪ pcc-tender(非 catalog 驅動)。"""
    in_db = [r[0] for r in con.execute("SELECT id FROM datasets ORDER BY id")]
    enabled = [e.dataset_id for e in enabled_entries()]
    return sorted(set(in_db) | set(enabled) | {"pcc-tender"})


def stale_after_days_for(
    dataset_id: str,
    update_cadence: str | None,
    *,
    non_catalog_default: int = DEFAULT_STALE_AFTER_DAYS,
) -> int | None:
    """該資料集的過期門檻(天)。None = 沒有契約可依據,不得判為 stale。

    catalog 驅動者由 update_cadence 推導;非 catalog 驅動者(PCC)沿用明列的門檻。
    兩者都不在名單上時回 None —— 寧可說「不知道」也不要套一個沒有根據的數字。
    """
    if dataset_id in NON_CATALOG_STALE_AFTER_DAYS:
        return non_catalog_default
    if update_cadence is None:
        return None
    return CADENCE_STALE_AFTER_DAYS.get(update_cadence)


def freshness_for(
    last_fetched_at: str | None,
    stale_after_days: int | None,
    generated_at: dt.datetime,
) -> str:
    """逐資料集的新鮮度。無門檻或無同步時間一律 unknown,永不冒充 fresh。"""
    if stale_after_days is None or not last_fetched_at:
        return FRESHNESS_UNKNOWN
    try:
        fetched = dt.datetime.fromisoformat(last_fetched_at.replace("Z", "+00:00"))
        if fetched.tzinfo is None:
            raise ValueError("missing timezone")
    except (TypeError, ValueError):
        # 時間戳壞掉就是沒有依據,不猜。全域狀態另有 degraded 判定會抓到。
        return FRESHNESS_UNKNOWN
    age_days = (generated_at - fetched.astimezone(dt.timezone.utc)).days
    return FRESHNESS_STALE if age_days > stale_after_days else FRESHNESS_FRESH


def build_datasets(
    con: sqlite3.Connection,
    *,
    pcc_row_count: int,
    generated_at: dt.datetime,
    non_catalog_stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
) -> dict[str, dict[str, Any]]:
    """由 catalog(已啟用者)∪ DB 產生資料集矩陣 —— 不硬編碼 dataset 名單。

    停用的 catalog 候選**不**發布:它們尚未實查,列出來會讓訪客誤以為已涵蓋。
    已啟用但尚未同步的資料集會出現,且 row_count 為 null —— 那正是要讓人看見的
    狀態,與「同步成功但 0 筆」不同。
    """
    catalog = {e.dataset_id: e for e in enabled_entries()}
    dataset_ids = published_dataset_ids(con)

    datasets: dict[str, dict[str, Any]] = {}
    for dataset_id in dataset_ids:
        meta = _dataset_metadata(con, dataset_id)
        entry = catalog.get(dataset_id)
        source_id = meta["source_id"]
        run_status, run_finished_at, _ = (
            _latest_run(con, source_id) if source_id else (None, None, False)
        )
        source_url = NON_CATALOG_SOURCE_URLS.get(dataset_id)
        if source_url is None and entry is not None:
            source_url = entry.landing_url or entry.download_urls[0]
        datasets[snapshot_key(dataset_id)] = {
            "row_count": (
                pcc_row_count if dataset_id == "pcc-tender"
                else _row_count(con, dataset_id)
            ),
            "last_fetched_at": meta["last_fetched_at"],
            "latest_run_status": run_status,
            "latest_run_finished_at": run_finished_at,
            "source_url": source_url,
            "license": meta["license"],
            # catalog 出處。非 catalog 驅動的資料集(PCC)為 null,不臆造。
            "title": entry.title if entry else None,
            "collection": entry.collection if entry else None,
            "update_cadence": entry.update_cadence if entry else None,
            "verified_at": entry.verified_at if entry else None,
        }
        cadence = datasets[snapshot_key(dataset_id)]["update_cadence"]
        threshold = stale_after_days_for(
            dataset_id, cadence, non_catalog_default=non_catalog_stale_after_days
        )
        datasets[snapshot_key(dataset_id)]["stale_after_days"] = threshold
        datasets[snapshot_key(dataset_id)]["freshness"] = freshness_for(
            meta["last_fetched_at"], threshold, generated_at
        )
    return datasets


def status_sources(
    con: sqlite3.Connection, dataset_ids: Iterable[str]
) -> list[tuple[str, str | None, str | None, bool]]:
    """由實際發布的資料集反推要納入狀態判定的來源 —— 不硬編碼 PCC/NHI。

    尚未同步過的資料集沒有 source_id,因此不會拉低整頁狀態;它的 row_count=null
    本身已經是可見的訊號。
    """
    source_ids = sorted(
        {
            sid
            for dataset_id in dataset_ids
            if (sid := _dataset_metadata(con, dataset_id)["source_id"])
        }
    )
    return [(_source_label(con, sid), *_latest_run(con, sid)) for sid in source_ids]


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
    datasets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """全域狀態。stale 由逐資料集的 freshness 匯總,不再用單一全域門檻。

    freshness=unknown(無 cadence 契約或未同步)**不會**拉低狀態 —— 沒有依據
    就不該宣稱過期,那個狀態在資料集矩陣裡本來就看得見。
    """
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
        if finished_times and (generated_at - min(finished_times)).days < 0:
            return {
                "state": "degraded",
                "source_max_date": source_max_date,
                "message": "來源同步完成時間晚於快照時間，請檢查資料時鐘。",
            }
    except (TypeError, ValueError):
        return {
            "state": "degraded",
            "source_max_date": source_max_date,
            "message": "來源同步完成時間格式無法驗證；目前顯示可驗證的既有資料。",
        }

    stale = sorted(
        (key, meta)
        for key, meta in datasets.items()
        if meta.get("freshness") == FRESHNESS_STALE
    )
    if stale:
        detail = "、".join(
            f"{key}（逾 {meta['stale_after_days']} 天）" for key, meta in stale
        )
        return {
            "state": "stale",
            "source_max_date": source_max_date,
            "message": (
                f"下列資料集已超過依更新頻率推導的門檻：{detail}。"
                "門檻為 2 倍更新週期；未登錄更新頻率者不納入判定。"
            ),
        }
    labels = "／".join(name for name, *_ in source_runs) or "已登錄"
    return {
        "state": "fresh",
        "source_max_date": source_max_date,
        "message": (
            f"快照由最近一次成功同步的 {labels} 資料庫重建；"
            "公告日期依目前資料範圍呈現。"
        ),
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
    # columns is derived only from PUBLIC_COLUMNS; the table name is a literal.
    # This is a bounded build-time projection, so Bandit B608 is a false positive.
    full_rows = [
        _public_row(row)
        for row in con.execute(
            f'SELECT {columns} FROM "ds_pcc_tender"'  # nosec B608
            ' ORDER BY "date" DESC, "announcement_type" ASC,'
            ' "agency" ASC, "job_number" ASC, "_nk" ASC'
        ).fetchall()
    ]
    total_count = len(full_rows)
    dates = [row["date"] for row in full_rows if row["date"]]
    source_max_date = max(dates) if dates else None
    source_min_date = min(dates) if dates else None
    dataset_ids = published_dataset_ids(con)
    datasets = build_datasets(
        con,
        pcc_row_count=total_count,
        generated_at=generated_at,
        non_catalog_stale_after_days=stale_after_days,
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
        source_runs=status_sources(con, dataset_ids),
        generated_at=generated_at,
        datasets=datasets,
    )

    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "status": status,
        "datasets": datasets,
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


def _format_count(value: int | None) -> str:
    """None = 從未同步成功。顯示「無資料」而不是 0(0 代表同步成功但空表)。"""
    return "無資料" if value is None else f"{value:,}"


def render_dashboard(template: str, payload: dict[str, Any]) -> str:
    """Render the no-JS core summary; record rows remain only in current.json."""
    summary = payload["summary"]["pcc_tender"]
    datasets = payload["datasets"]
    nhi = datasets.get("nhi_clinic") or {}
    type_summary = " · ".join(
        f'{item["name"]} {item["count"]:,}'
        for item in summary["announcement_types"]
    ) or "沒有公告類型資料"
    replacements = {
        "STATUS_STATE": payload["status"]["state"],
        "STATUS_MESSAGE": payload["status"]["message"],
        "GENERATED_AT": _format_datetime(payload["generated_at"]),
        "SOURCE_MAX_DATE": payload["status"]["source_max_date"] or "無法確認",
        "PCC_ROW_COUNT": _format_count(datasets["pcc_tender"]["row_count"]),
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
        # nhi_clinic 是 catalog 驅動的,可能不在快照中(未啟用/未同步)。
        # 缺席時顯示「無資料」,不顯示 0 —— 0 會被讀成「同步成功但沒有診所」。
        "NHI_ROW_COUNT": _format_count(nhi.get("row_count")),
        "NHI_FETCHED_AT": _format_datetime(nhi.get("last_fetched_at")),
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
    parser.add_argument(
        "--stale-after-days",
        type=int,
        default=DEFAULT_STALE_AFTER_DAYS,
        help=(
            "非 catalog 驅動資料集(PCC)的過期門檻天數。catalog 驅動者一律由"
            " update_cadence 推導(2 倍更新週期),不受本旗標影響"
        ),
    )
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