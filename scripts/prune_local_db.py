"""prune_local_db — 把既有 hcmcp.db 就地縮小到新範圍(衛福部資訊勞務標案 + 診所)。

範圍縮小後一次性清除超範圍資料,不依賴重 sync(PCC 半月檔維護時也能清):
- 保留 nhi-clinic(診所)
- pcc-tender 就地縮成「衛福部轄下機關 + 資訊勞務(IT 關鍵字)」(重用 cli 篩選邏輯)
- DROP 其餘 dataset(全機關 pcc-tender 多餘列、其他 NHI/static)
- VACUUM 釋放空間

預設 dry-run 只報告;加 --apply 才實際修改。
"""
from __future__ import annotations

import argparse
import json
import sqlite3

from health_opendata_mcp.cli import IT_EXCLUDE, IT_INCLUDE
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository

_KEEP = {"nhi-clinic", "pcc-tender"}
_PREFIX = "衛生福利部"
_NEW_TENDER_TITLE = "政府採購標案(衛生福利部範圍資訊勞務,twinkle pcc-tender 相容)"


def _keep_tender(agency: str, title: str) -> bool:
    if not (agency or "").strip().startswith(_PREFIX):
        return False
    t = (title or "").lower()
    if IT_INCLUDE and not any(k in t for k in IT_INCLUDE):
        return False
    if any(k in t for k in IT_EXCLUDE):
        return False
    return True


def prune(db_path: str, apply: bool) -> None:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    datasets = [r[0] for r in con.execute("SELECT id FROM datasets ORDER BY id")]

    print("=== 現有 dataset ===")
    drop_ds = [d for d in datasets if d not in _KEEP]
    for d in datasets:
        n = con.execute(
            "SELECT COUNT(*) FROM records WHERE dataset_id=?", (d,)
        ).fetchone()[0]
        mark = "保留" if d in _KEEP else "移除"
        print(f"  [{mark}] {d}: {n} 筆")

    # pcc-tender 就地縮小評估
    drop_nks: list[str] = []
    if "pcc-tender" in datasets:
        rows = con.execute(
            "SELECT natural_key, payload FROM records WHERE dataset_id='pcc-tender'"
        ).fetchall()
        for nk, payload in rows:
            p = json.loads(payload)
            if not _keep_tender(p.get("agency", ""), p.get("title", "")):
                drop_nks.append(nk)
        print(
            f"\n=== pcc-tender 就地縮小:{len(rows)} → {len(rows) - len(drop_nks)} 筆"
            f"(刪 {len(drop_nks)} 非衛福部資訊勞務)==="
        )

    if not apply:
        print("\n(dry-run,未修改;加 --apply 才執行)")
        con.close()
        return

    # 1) DROP 超範圍 dataset
    for d in drop_ds:
        table = SqliteRepository.materialized_table(d)
        con.execute(f'DROP TABLE IF EXISTS "{table}"')
        con.execute("DELETE FROM records WHERE dataset_id=?", (d,))
        con.execute("DELETE FROM datasets WHERE id=?", (d,))
    # 2) 就地縮小 pcc-tender
    for nk in drop_nks:
        con.execute(
            "DELETE FROM records WHERE dataset_id='pcc-tender' AND natural_key=?", (nk,)
        )
        con.execute('DELETE FROM ds_pcc_tender WHERE _nk=?', (nk,))
    # 3) 更新 pcc-tender 標題/collection 反映新範圍
    con.execute(
        "UPDATE datasets SET title=?, collection='procurement' WHERE id='pcc-tender'",
        (_NEW_TENDER_TITLE,),
    )
    con.commit()
    con.execute("VACUUM")
    con.commit()
    con.close()
    print("\n已套用。保留 dataset:", ", ".join(sorted(_KEEP)))


def main() -> None:
    parser = argparse.ArgumentParser(description="縮小 hcmcp.db 至新範圍")
    parser.add_argument("--db", required=True)
    parser.add_argument("--apply", action="store_true", help="實際修改(預設 dry-run)")
    args = parser.parse_args()
    prune(args.db, args.apply)


if __name__ == "__main__":
    main()
