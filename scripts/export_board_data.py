"""匯出 pcc-tender 全量資料為看板 data.js 快照。

Cowork live artifact 的 callMcpTool 只能呼叫 claude.ai 註冊的 remote connector,
無法呼叫本機 stdio MCP — 看板(pcc-it-tender-board)因此改讀同目錄 data.js,
由半月排程在 hcmcp-sync 之後執行本腳本重新匯出。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3

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
    finally:
        con.close()
    payload = {
        "generated_at": datetime.datetime.now()
        .astimezone()
        .isoformat(timespec="seconds"),
        "max_date": rows[0][0] if rows else None,
        "columns": list(COLUMNS),
        "rows": [list(r) for r in rows],
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
