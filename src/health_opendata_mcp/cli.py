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
    StaticCsvAdapter,
    StaticCsvSpec,
)
from health_opendata_mcp.catalog import enabled_nhi_specs, enabled_static_specs
from health_opendata_mcp.contracts import SourceAdapter
from health_opendata_mcp.ingestion.pipeline import run_source
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository


def default_db_path() -> str:
    return os.environ.get(
        "HCMCP_DB", str(Path.home() / ".hcmcp" / "hcmcp.db")
    )


# DB 目錄權限的單一真實來源:sync(寫)與 server(讀)都經由此函式建目錄,
# 兩處必須一致才有意義 —— 誰先跑就由誰決定權限。
DB_DIR_MODE = 0o700


def ensure_db_dir(db_path: str) -> Path:
    """建立 DB 所在目錄,權限限制為 0o700 並回傳該目錄。

    DB 目錄存放已抓取的開放資料與抓取軌跡;預設 umask 會讓它成為
    0o755,多使用者主機上任何本機帳號都讀得到(CWE-276)。
    已存在的目錄不改動權限,避免覆寫使用者刻意設定的部署權限。
    """
    parent = Path(db_path).parent
    parent.mkdir(parents=True, exist_ok=True, mode=DB_DIR_MODE)
    return parent


# 要同步哪些開放資料由 catalog 決定(見 health_opendata_mcp/catalog.py):
# 新增資料集 = 加一筆帶出處的 entry,不必再改本檔。此處只保留投影後的
# spec 清單,維持既有 import 路徑不變。
NHI_DATASETS = enabled_nhi_specs()
STATIC_CSV_DATASETS = enabled_static_specs()

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


def build_adapters(
    award_months: int,
    tender_months: int,
    *,
    nhi_specs: list[NhiDatasetSpec] | None = None,
    static_specs: list[StaticCsvSpec] | None = None,
) -> list[SourceAdapter]:
    """依 catalog 組出本輪要跑的 adapter。

    只有 catalog 中 enabled 的 entry 會產生網路請求;某一 kind 沒有任何
    enabled entry 時就不建立對應 adapter —— 不註冊一個永遠抓 0 筆的來源。
    spec 可注入(DI),測試不必動模組層狀態。
    """
    nhi = NHI_DATASETS if nhi_specs is None else nhi_specs
    static = STATIC_CSV_DATASETS if static_specs is None else static_specs
    adapters: list[SourceAdapter] = []
    if nhi:
        adapters.append(NhiApiAdapter(nhi))
    if static:
        adapters.append(StaticCsvAdapter(static))
    # 衛福部轄下機關 + 資訊勞務(IT 關鍵字)標案 — 看板/排程資料源。
    # PCC 不是 CSV registry 驅動(半月 XML + 關鍵字過濾),故不進 catalog。
    adapters.append(
        PccTenderAdapter(
            award_months=award_months,
            tender_months=tender_months,
            agency_prefix="衛生福利部",
            dataset_id="pcc-tender",
            collection="procurement",
            title_includes=IT_INCLUDE,
            title_excludes=IT_EXCLUDE,
        )
    )
    return adapters


async def _sync(db_path: str, award_months: int, tender_months: int) -> int:
    ensure_db_dir(db_path)
    repo = SqliteRepository(db_path)
    await repo.init()
    adapters = build_adapters(award_months, tender_months)
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
