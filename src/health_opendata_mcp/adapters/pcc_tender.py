"""PccTenderAdapter — PCC 半月 XML → pcc-tender / pcc-tender-mohw 資料集。

對齊 twinkle-hub pcc-tender 欄位(英文欄名,既有查詢模式可直接沿用)。
agency_prefix 縮限機關範圍(預設「衛生福利部」含轄下機關與部立醫院);
傳空字串=全機關,供 twinkle-hub 停用後的看板/排程沿用原 pcc-tender 查詢。
解析純函式 vendored 自 g0VMCP(_pcc_opendata.py),兩專案零耦合。
"""
from __future__ import annotations

from datetime import date
from typing import Awaitable, Callable

from health_opendata_mcp.adapters import _pcc_opendata as pcc
from health_opendata_mcp.adapters._http import default_http_get
from health_opendata_mcp.contracts import (
    AccessStrategy,
    ColumnSpec,
    DatasetMeta,
    NormalizedBatch,
    RawPayload,
    Record,
    ResourceRef,
)

# twinkle pcc-tender 相容欄位(award_price 維持 TEXT — 空值常見,查詢端 CAST)
PCC_TENDER_COLUMNS = tuple(
    ColumnSpec(name)
    for name in (
        "date",
        "announcement_type",
        "title",
        "agency",
        "job_number",
        "companies",
        "procurement_type",
        "procurement_attr",
        "award_way",
        "award_price",
        "notice_date",
        # 明細頁加值欄位(初始空,由 enrich_bid_deadline 補;招標階段最關鍵)
        "bid_deadline",
        "open_date",
        "budget",
    )
)

_AWARD = "決標公告"
_TENDER = "招標公告"


def _iso(slash_date: str) -> str:
    """PCC 日期 2026/04/15 → 2026-04-15(twinkle 格式);空值原樣。"""
    return slash_date.replace("/", "-")


def _halfmonths(today: date, months_back: int):
    """產生 (year, month, half) — 從 months_back 個月前到本月,不含未來。"""
    year, month = today.year, today.month - months_back
    while month <= 0:
        year, month = year - 1, month + 12
    while (year, month) <= (today.year, today.month):
        for half in (1, 2):
            # 上半月檔涵蓋 1-15 日;當月份只納入已開始的期間,未發布回空檔由 normalize 容忍
            period_start = date(year, month, 1 if half == 1 else 16)
            if period_start <= today:
                yield year, month, half
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


class PccTenderAdapter:
    source_id = "pcc-opendata"
    name = "政府電子採購網半月公開資料"
    platform = "web.pcc.gov.tw"
    access_strategy = AccessStrategy.STATIC_FILE

    def __init__(
        self,
        *,
        award_months: int = 12,
        tender_months: int = 3,
        agency_prefix: str = "衛生福利部",
        dataset_id: str = "pcc-tender-mohw",
        collection: str = "healthcare",
        title_includes: tuple[str, ...] = (),
        title_excludes: tuple[str, ...] = (),
        today: date | None = None,
        http_get: Callable[[str], Awaitable[bytes]] | None = None,
    ) -> None:
        self._award_months = award_months
        self._tender_months = tender_months
        self._agency_prefix = agency_prefix
        # 主題關鍵字以小寫比對(title.lower()),中文不受影響、英文(AI/IT)大小寫無關
        self._title_includes = tuple(k.lower() for k in title_includes)
        self._title_excludes = tuple(k.lower() for k in title_excludes)
        self._today = today or date.today()
        self._http_get = http_get or default_http_get
        scope = f"{agency_prefix}範圍" if agency_prefix else "全機關"
        topic = "資訊勞務" if title_includes else ""
        self._dataset = DatasetMeta(
            id=dataset_id,
            source_id=self.source_id,
            title=f"政府採購標案({scope}{topic},twinkle pcc-tender 相容)",
            columns=PCC_TENDER_COLUMNS,
            collection=collection,
            license="政府資料開放授權條款 1.0",
        )

    def _keep(self, org_name: str, title: str) -> bool:
        """機關前綴 + 主題關鍵字篩選(includes 命中其一且不命中 excludes)。"""
        if not org_name.strip().startswith(self._agency_prefix):
            return False
        t = (title or "").lower()
        if self._title_includes and not any(k in t for k in self._title_includes):
            return False
        if any(k in t for k in self._title_excludes):
            return False
        return True

    def _build_refs(self) -> list[ResourceRef]:
        refs = [
            ResourceRef(
                dataset=self._dataset,
                url=pcc.award_xml_url(y, m, h),
                fmt="xml",
                meta={"kind": "award"},
            )
            for y, m, h in _halfmonths(self._today, self._award_months)
        ]
        refs += [
            ResourceRef(
                dataset=self._dataset,
                url=pcc.tender_xml_url(y, m, h),
                fmt="xml",
                meta={"kind": "tender"},
            )
            for y, m, h in _halfmonths(self._today, self._tender_months)
        ]
        return refs

    async def discover(self) -> list[ResourceRef]:
        return self._build_refs()

    async def fetch(self, ref: ResourceRef) -> RawPayload:
        return RawPayload(ref=ref, content=await self._http_get(ref.url))

    def normalize(self, raw: RawPayload) -> NormalizedBatch:
        text = raw.content.decode("utf-8", errors="replace").strip()
        if not text:  # 半月檔尚未發布:HTTP 200 + 空內容(實測)
            return NormalizedBatch(dataset=self._dataset, records=())
        if raw.ref.meta["kind"] == "award":
            records = tuple(
                self._award_record(row)
                for row in pcc.parse_award_xml(text)
                if self._keep(row.org_name, row.title)
            )
        else:
            records = tuple(
                self._tender_record(row)
                for row in pcc.parse_tender_xml(text)
                if self._keep(row.org_name, row.title)
            )
        return NormalizedBatch(dataset=self._dataset, records=records)

    def _award_record(self, row: pcc.AwardRow) -> Record:
        d = _iso(row.award_date)
        payload = {
            "date": d,
            "announcement_type": _AWARD,
            "title": row.title,
            "agency": row.org_name,
            "job_number": row.case_no,
            "companies": ",".join(row.winners),
            "procurement_type": row.procurement_type,
            "procurement_attr": row.procurement_attr,
            "award_way": row.award_way,
            "award_price": row.award_price,
            "notice_date": _iso(row.notice_date),
            "bid_deadline": "",
            "open_date": "",
            "budget": "",
        }
        return Record(
            dataset_id=self._dataset.id,
            natural_key=f"{row.case_no}|{_AWARD}|{d}",
            payload=payload,
        )

    def _tender_record(self, row: pcc.OpenDataRow) -> Record:
        d = _iso(row.ann_date)
        payload = {
            "date": d,
            "announcement_type": _TENDER,
            "title": row.title,
            "agency": row.org_name,
            "job_number": row.case_no,
            "companies": "",
            "procurement_type": row.procurement_type,
            "procurement_attr": row.procurement_attr,
            "award_way": "",
            "award_price": "",
            "notice_date": "",
            "bid_deadline": "",
            "open_date": "",
            "budget": "",
        }
        return Record(
            dataset_id=self._dataset.id,
            natural_key=f"{row.case_no}|{_TENDER}|{d}",
            payload=payload,
        )
