"""CLI — hcmcp-sync:同步所有內建來源至本地 SQLite。"""
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
from health_opendata_mcp.ingestion.pipeline import run_source
from health_opendata_mcp.repository.sqlite_repo import SqliteRepository


def default_db_path() -> str:
    return os.environ.get(
        "HCMCP_DB", str(Path.home() / ".hcmcp" / "hcmcp.db")
    )


# 內建資料集註冊表 — rId 無命名規則,逐一實查登錄(來源:data.gov.tw 對應頁)
NHI_DATASETS = [
    NhiDatasetSpec(
        dataset_id="nhi-hospital-district",
        r_id="A21030000I-D21003-003",
        title="健保特約醫事機構-地區醫院",
    ),
    NhiDatasetSpec(
        dataset_id="nhi-hospital-regional",
        r_id="A21030000I-D21002-005",
        title="健保特約醫事機構-區域醫院",
    ),
    # data.gov.tw #9402 — 同為 info.nhi.gov.tw 一級 API;同機構多月份 → 複合鍵
    NhiDatasetSpec(
        dataset_id="nhi-hospital-bed-ratio",
        r_id="A21030000I-D02001-015",
        title="全民健保特約醫院之保險病床比率",
        natural_key_columns=("機構代碼", "統計年月"),
    ),
]

# data.gov.tw distribution 靜態 CSV(2026-06-10 實查 ld+json contentUrl)
STATIC_DATASETS = [
    # data.gov.tw #25842(退輔會彙整,月別時間序列)
    StaticCsvSpec(
        dataset_id="nhi-insured-population",
        title="全民健保人數",
        urls=("https://www.vac.gov.tw/files/b10全民健保人數.csv",),
        natural_key_columns=("民國年月",),
    ),
    # data.gov.tw #176510(縣市別系列 2 檔:97-104 / 105+;年齡別系列不納入)
    StaticCsvSpec(
        dataset_id="mohw-outpatient-rate",
        title="健保平均門診就診率",
        urls=(
            "https://www.mohw.gov.tw/dl-98683-66651e5d-fc2b-4e72-969f-d092f2249f62.html",
            "https://www.mohw.gov.tw/dl-98684-976060f4-6fcb-4029-b6bd-34978a99f822.html",
        ),
        natural_key_columns=("年度", "縣市別", "疾病別"),
        column_renames={"疾病別(第10版)": "疾病別"},
    ),
    # data.gov.tw #142696(國防部軍醫局)
    StaticCsvSpec(
        dataset_id="mnd-military-hospital-fee",
        title="國軍醫院健保不給付醫療項目收費標準",
        urls=(
            "https://www.mnd.gov.tw/opendata.aspx?f=國軍醫院健保不給付醫療項目收費標準563669",
        ),
        natural_key_columns=("Date", "Code"),
    ),
]


async def _sync(db_path: str, award_months: int, tender_months: int) -> int:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    repo = SqliteRepository(db_path)
    await repo.init()
    adapters = [
        NhiApiAdapter(NHI_DATASETS),
        StaticCsvAdapter(STATIC_DATASETS),
        PccTenderAdapter(award_months=award_months, tender_months=tender_months),
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
    parser = argparse.ArgumentParser(description="同步醫療健保開放資料至本地 DB")
    parser.add_argument("--db", default=default_db_path(), help="SQLite DB 路徑")
    parser.add_argument("--award-months", type=int, default=12, help="決標回溯月數")
    parser.add_argument("--tender-months", type=int, default=3, help="招標回溯月數")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_sync(args.db, args.award_months, args.tender_months)))
