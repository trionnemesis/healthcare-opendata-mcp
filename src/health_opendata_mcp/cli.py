"""CLI — hcmcp-sync:同步衛福部資訊勞務標案 + 診所資料至本地 SQLite。

範圍縮小(2026-06-13):由「全機關標案 × 全醫療健保開放資料」收斂為
衛生福利部轄下機關的「資訊勞務相關」標案 + 診所資料,案量小才能逐案
enrich 截標/開標/預算(見 scripts/enrich_bid_deadline.py)。
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from health_opendata_mcp.adapters import (
    NhiApiAdapter,
    NhiDatasetSpec,
    PccTenderAdapter,
)
from health_opendata_mcp.ingestion.pipeline import run_source
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository


def default_db_path() -> str:
    return os.environ.get(
        "HCMCP_DB", str(Path.home() / ".hcmcp" / "hcmcp.db")
    )


# 診所(NHI 一級 API,約 24.5k 筆/每日更新);實查 2026-06-10 resource = D21004-009
NHI_DATASETS = [
    NhiDatasetSpec(
        dataset_id="nhi-clinic",
        r_id="A21030000I-D21004-009",
        title="健保特約醫事機構-診所",
    ),
]

# 資訊勞務主題關鍵字 — 與看板 pcc-it-tender-board / 半月排程 SKILL 同步維護
IT_INCLUDE = (
    "資訊", "系統", "軟體", "資安", "網路", "雲端", "機房", "數位", "電腦", "主機",
    "資料庫", "平台", "伺服器", "資通訊", "程式", "網站", "鑑識", "人工智慧",
    "人工智能", "ai整合", "生成式",
)
IT_EXCLUDE = (
    "手術", "醫材", "椎", "核酸", "x光", "攝影", "空調", "離心機", "灌溉", "變電",
    "鍋爐", "燈光", "微影", "機械手臂", "不純物", "心血管", "放射", "定序", "電車",
    "輸送", "供電", "模控", "儲能", "冷氣", "消防", "交通控制", "環境監測",
    "循環氣體", "銜接",
)


async def _sync(db_path: str, award_months: int, tender_months: int) -> int:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    repo = SqliteRepository(db_path)
    await repo.init()
    adapters = [
        NhiApiAdapter(NHI_DATASETS),
        # 衛福部轄下機關 + 資訊勞務(IT 關鍵字)標案 — 看板/排程資料源
        PccTenderAdapter(
            award_months=award_months,
            tender_months=tender_months,
            agency_prefix="衛生福利部",
            dataset_id="pcc-tender",
            collection="procurement",
            title_includes=IT_INCLUDE,
            title_excludes=IT_EXCLUDE,
        ),
    ]
    exit_code = 0
    for adapter in adapters:
        summary = await run_source(adapter, repo)
        print(
            f"[{summary.status.value:<9}] {adapter.source_id:<14}"
            f" +{summary.fetched_count} 筆"
            + (f"  (errors: {len(summary.errors)})" if summary.errors else "")
        )
        for err in summary.errors[:3]:
            print(f"    ! {err}")
        if summary.status.value in ("FAILED", "BLOCKED"):
            exit_code = 1
    return exit_code


def sync_main() -> None:
    parser = argparse.ArgumentParser(
        description="同步衛福部資訊勞務標案 + 診所資料至本地 DB"
    )
    parser.add_argument("--db", default=default_db_path(), help="SQLite DB 路徑")
    parser.add_argument("--award-months", type=int, default=12, help="決標回溯月數")
    # 12 = 看板「近 1 年」招標視圖的累積上限(PCC 站上實際可回溯約 6 個月)
    parser.add_argument("--tender-months", type=int, default=12, help="招標回溯月數")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_sync(args.db, args.award_months, args.tender_months)))
