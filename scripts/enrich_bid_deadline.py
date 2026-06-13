"""enrich_bid_deadline — 為近期 IT 類招標案補截標/開標/預算(web.pcc 明細頁)。

半月 open data 招標檔無截標/開標/預算;本 script 對「最近、尚未決標、值還空」的
IT 類招標公告逐案抓明細頁補欄位,寫回 pcc-tender。

反爬倫理:限量(--limit)+ 逐案節流(--throttle)+ 被封鎖(BlockedError)立即停止
已抓部分照常寫回。看板讀 data.js 快照,故 enrich 後需重跑 export_board_data.py。

用法:
  enrich_bid_deadline.py --db <hcmcp.db> [--days 30] [--limit 40] [--throttle 3.0]
"""
from __future__ import annotations

import argparse
import asyncio

from health_opendata_mcp.adapters.pcc_detail import PccDetailEnricher, default_client
from health_opendata_mcp.contracts import BlockedError, NormalizedBatch, Record
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository

_DATASET = "pcc-tender"
# 與看板 / 半月排程同步維護的 IT 收緊條件
_IT_KW = ["資訊","系統","軟體","資安","網路","雲端","機房","數位","電腦","主機","資料庫","平台","伺服器","資通訊","程式","網站","鑑識","人工智慧","人工智能","ai整合","生成式"]
_EXCLUDE_KW = ["手術","醫材","椎","核酸","x光","攝影","空調","離心機","灌溉","變電","鍋爐","燈光","微影","機械手臂","不純物","心血管","放射","定序","電車","輸送","供電","模控","儲能","冷氣","消防","交通控制","環境監測","循環氣體","銜接"]


def _is_it(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in _IT_KW) and not any(k in t for k in _EXCLUDE_KW)


async def _candidates(repo: SqliteRepository, threshold: str) -> list[tuple[str, dict]]:
    """回 (natural_key, payload) — 招標公告、date>=threshold、bid_deadline 空、IT 類。"""
    import aiosqlite
    import json

    out: list[tuple[str, dict]] = []
    async with aiosqlite.connect(repo._db_path) as db:  # noqa: SLF001
        cur = await db.execute(
            "SELECT natural_key, payload FROM records WHERE dataset_id = ?",
            (_DATASET,),
        )
        for nk, payload_json in await cur.fetchall():
            p = json.loads(payload_json)
            if p.get("announcement_type") != "招標公告":
                continue
            if (p.get("date") or "") < threshold:
                continue
            if (p.get("bid_deadline") or "").strip():
                continue
            if not _is_it(p.get("title", "")):
                continue
            out.append((nk, p))
    out.sort(key=lambda x: x[1].get("date", ""), reverse=True)
    return out


async def _enrich(db_path: str, days: int, limit: int, throttle: float) -> int:
    from datetime import date, timedelta

    repo = SqliteRepository(db_path)
    await repo.init()
    meta = await repo.get_dataset(_DATASET)
    if meta is None:
        print(f"資料集 {_DATASET} 不存在,請先 hcmcp-sync")
        return 1
    threshold = (date.today() - timedelta(days=days)).isoformat()
    cands = (await _candidates(repo, threshold))[:limit]
    print(f"候選 {len(cands)} 案(招標公告 / date>={threshold} / IT / 未 enrich)")

    enricher = PccDetailEnricher(default_client())
    updated: list[Record] = []
    blocked = False
    for i, (nk, payload) in enumerate(cands, 1):
        job = payload.get("job_number", "")
        try:
            detail = await enricher.fetch_detail(job)
        except BlockedError as exc:
            print(f"  ! 被封鎖,停止:{exc}")
            blocked = True
            break
        except Exception as exc:  # 單案失敗不中斷整批
            print(f"  - {job}: 失敗 {type(exc).__name__}")
            detail = None
        if detail is not None and (detail.bid_deadline or detail.open_date or detail.budget):
            payload = {
                **payload,
                "bid_deadline": detail.bid_deadline,
                "open_date": detail.open_date,
                "budget": detail.budget,
            }
            updated.append(Record(dataset_id=_DATASET, natural_key=nk, payload=payload))
            print(f"  + {job}: 截標={detail.bid_deadline or '-'} 開標={detail.open_date or '-'} 預算={detail.budget or '-'}")
        if i < len(cands) and not blocked:
            await asyncio.sleep(throttle)

    if updated:
        await repo.upsert_batch(NormalizedBatch(dataset=meta, records=tuple(updated)))
    print(f"已 enrich {len(updated)} 案" + (" (因封鎖提前停止)" if blocked else ""))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="補 pcc-tender 截標/開標/預算")
    parser.add_argument("--db", required=True, help="hcmcp SQLite DB 路徑")
    parser.add_argument("--days", type=int, default=30, help="只處理近 N 天的招標公告")
    parser.add_argument("--limit", type=int, default=40, help="單次最多 enrich 案數")
    parser.add_argument("--throttle", type=float, default=3.0, help="逐案間隔秒數")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_enrich(args.db, args.days, args.limit, args.throttle)))


if __name__ == "__main__":
    main()
