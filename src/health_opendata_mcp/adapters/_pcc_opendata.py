"""PCC 半月公開資料(open data)解析純函式,零網路 I/O。

Vendored from g0VMCP `src/g0vmcp/ingestion/opendata.py`(MIT)— 兩專案
刻意零耦合(g0VMCP 維持不動),故複製而非 import;上游異動不自動跟進。

招標半月檔 tender_YYYYMMNN.xml 根節點 <TENDER_LIST>,每筆 <TENDER>。
決標檔 award_*.xml 走同一 downloadFile?fileName= 路由。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

_DOWNLOAD_BASE = "https://web.pcc.gov.tw/tps/tp/OpenData/downloadFile"
_MAX_XML_CHARS = 20_000_000


@dataclass(frozen=True)
class OpenDataRow:
    """半月招標 XML 單筆。"""

    ann_date: str  # TENDER_SPDT,格式 2026/04/20
    org_name: str
    case_no: str
    title: str
    procurement_type: str
    procurement_attr: str


@dataclass(frozen=True)
class AwardRow:
    """半月決標 XML 單筆。winners 為得標廠商名(無統編)。"""

    award_date: str
    notice_date: str
    org_name: str
    case_no: str
    title: str
    procurement_type: str
    procurement_attr: str
    award_way: str
    award_price: str  # 字串,可能空
    winners: tuple[str, ...]


def _safe_fromstring(xml: str) -> ET.Element:
    """解析官方 XML 前套用基本資源護欄,拒絕 DTD/ENTITY 與過大輸入。"""
    if len(xml) > _MAX_XML_CHARS:
        raise ValueError("PCC XML 超過安全解析大小上限")
    prefix = xml[:1024].lower()
    if "<!doctype" in prefix or "<!entity" in prefix:
        raise ValueError("PCC XML 含 DTD/ENTITY,已拒絕解析")
    return ET.fromstring(xml)


def _text(node: ET.Element, tag: str) -> str:
    """缺欄位 / 空內容一律回空字串(容錯)。"""
    child = node.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def parse_tender_xml(xml: str) -> list[OpenDataRow]:
    """解析半月招標 XML。缺欄位以空字串填充。"""
    root = _safe_fromstring(xml)
    rows: list[OpenDataRow] = []
    for t in root.iter("TENDER"):
        rows.append(
            OpenDataRow(
                ann_date=_text(t, "TENDER_SPDT"),
                org_name=_text(t, "TENDER_ORG_NAME"),
                case_no=_text(t, "TENDER_CASE_NO"),
                title=_text(t, "TENDER_NAME"),
                procurement_type=_text(t, "PROCUREMENT_TYPE"),
                procurement_attr=_text(t, "PROCUREMENT_ATTR"),
            )
        )
    return rows


def parse_award_xml(xml: str) -> list[AwardRow]:
    """解析半月決標 XML。得標廠商在巢狀 <BIDDER_LIST>/<BIDDER_SUPP_NAME>。"""
    root = _safe_fromstring(xml)
    rows: list[AwardRow] = []
    for t in root.iter("TENDER"):
        bl = t.find("BIDDER_LIST")
        winners: tuple[str, ...] = ()
        if bl is not None:
            winners = tuple(
                e.text.strip()
                for e in bl.iter("BIDDER_SUPP_NAME")
                if e.text and e.text.strip()
            )
        rows.append(
            AwardRow(
                award_date=_text(t, "AWARD_DATE"),
                notice_date=_text(t, "AWARD_NOTICE_DATE"),
                org_name=_text(t, "TENDER_ORG_NAME"),
                case_no=_text(t, "TENDER_CASE_NO"),
                title=_text(t, "TENDER_NAME"),
                procurement_type=_text(t, "PROCUREMENT_TYPE"),
                procurement_attr=_text(t, "PROCUREMENT_ATTR"),
                award_way=_text(t, "TENDER_AWARD_WAY"),
                award_price=_text(t, "TENDER_AWARD_PRICE"),
                winners=winners,
            )
        )
    return rows


def _halfmonth_url(prefix: str, year: int, month: int, half: int) -> str:
    file_name = f"{prefix}_{year:04d}{month:02d}{half:02d}.xml"
    return f"{_DOWNLOAD_BASE}?fileName={file_name}"


def tender_xml_url(year: int, month: int, half: int) -> str:
    """半月招標檔下載 URL。half=1 上半月 / half=2 下半月。"""
    return _halfmonth_url("tender", year, month, half)


def award_xml_url(year: int, month: int, half: int) -> str:
    """半月決標檔下載 URL。"""
    return _halfmonth_url("award", year, month, half)
