"""匯出 pcc-tender 全量資料為看板 data.js 快照。

Cowork live artifact 的 callMcpTool 只能呼叫 claude.ai 註冊的 remote connector,
無法呼叫本機 stdio MCP — 看板(pcc-it-tender-board)因此改讀同目錄 data.js,
由半月排程在 hcmcp-sync 之後執行本腳本重新匯出。
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sqlite3
from collections import defaultdict

def _normalize_name(name: str) -> str:
    """Strip trailing English alias like '公司 (CORP NAME)'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()


def _vendor_stats(con: sqlite3.Connection) -> dict:
    """Aggregate award records by vendor: count, total amount, percentages."""
    rows = con.execute(
        'SELECT "companies", CAST("award_price" AS INTEGER)'
        ' FROM "ds_pcc_tender"'
        " WHERE \"announcement_type\" = '決標公告'"
        "   AND \"companies\" IS NOT NULL AND \"companies\" != ''"
    ).fetchall()

    counts: dict[str, int] = defaultdict(int)
    amounts: dict[str, int] = defaultdict(int)

    for companies_str, amount in rows:
        # split only on commas outside parentheses (EN company names contain commas)
        for raw in re.split(r",\s*(?![^(]*\))", companies_str or ""):
            name = _normalize_name(raw)
            if name:
                counts[name] += 1
                amounts[name] += amount or 0

    total_count = sum(counts.values())
    total_amount = sum(amounts.values())

    vendors = sorted(
        [
            {
                "name": name,
                "award_count": counts[name],
                "total_amount": amounts[name],
                "count_pct": (
                    round(counts[name] / total_count * 100, 1) if total_count else 0
                ),
                "amount_pct": (
                    round(amounts[name] / total_amount * 100, 1) if total_amount else 0
                ),
            }
            for name in counts
        ],
        key=lambda v: (-v["award_count"], -v["total_amount"]),
    )

    return {
        "total_award_records": total_count,
        "total_amount": total_amount,
        "vendors": vendors,
    }


COLUMNS = (
    "date",
    "announcement_type",
    "title",
    "agency",
    "procurement_attr",
    "award_way",
    "award_price",
    "companies",
    "job_number",
    "bid_deadline",  # 截止投標(enrich;招標案才有意義)
    "open_date",  # 開標時間 = 實際投標 deadline
    "budget",  # 預算金額
)


def export(db_path: str, out_path: str) -> int:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cols = ", ".join(f'"{c}"' for c in COLUMNS)
        rows = con.execute(
            f'SELECT {cols} FROM "ds_pcc_tender" ORDER BY "date" DESC'
        ).fetchall()
        vendor_stats = _vendor_stats(con)
    finally:
        con.close()
    payload = {
        "generated_at": datetime.datetime.now()
        .astimezone()
        .isoformat(timespec="seconds"),
        "max_date": rows[0][0] if rows else None,
        "columns": list(COLUMNS),
        "rows": [list(r) for r in rows],
        "vendor_stats": vendor_stats,
    }
    js = (
        "window.__PCC_DATA = "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + ";\n"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(js)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="匯出 pcc-tender 為看板 data.js")
    parser.add_argument("--db", required=True, help="hcmcp SQLite DB 路徑")
    parser.add_argument("--out", required=True, help="data.js 輸出路徑")
    args = parser.parse_args()
    count = export(args.db, args.out)
    print(f"exported {count} rows → {args.out}")


if __name__ == "__main__":
    main()
