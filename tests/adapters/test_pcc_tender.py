"""PccTenderAdapter — PCC 半月 XML → pcc-tender-mohw(BDD: ingestion.feature)。"""
from datetime import date

import pytest

from health_opendata_mcp.adapters.pcc_tender import PccTenderAdapter
from health_opendata_mcp.contracts import AccessStrategy, RawPayload

_AWARD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<TENDER_LIST>
  <TENDER>
    <AWARD_DATE>2026/04/15</AWARD_DATE>
    <AWARD_NOTICE_DATE>2026/04/18</AWARD_NOTICE_DATE>
    <TENDER_ORG_NAME>衛生福利部疾病管制署</TENDER_ORG_NAME>
    <TENDER_CASE_NO>CK115001</TENDER_CASE_NO>
    <TENDER_NAME>115年疫苗冷鏈資訊系統維運案</TENDER_NAME>
    <PROCUREMENT_TYPE>公開招標</PROCUREMENT_TYPE>
    <PROCUREMENT_ATTR>勞務類</PROCUREMENT_ATTR>
    <TENDER_AWARD_WAY>最有利標</TENDER_AWARD_WAY>
    <TENDER_AWARD_PRICE>12000000</TENDER_AWARD_PRICE>
    <BIDDER_LIST><BIDDER_SUPP_NAME>某資訊股份有限公司</BIDDER_SUPP_NAME></BIDDER_LIST>
  </TENDER>
  <TENDER>
    <AWARD_DATE>2026/04/16</AWARD_DATE>
    <AWARD_NOTICE_DATE>2026/04/17</AWARD_NOTICE_DATE>
    <TENDER_ORG_NAME>交通部公路局</TENDER_ORG_NAME>
    <TENDER_CASE_NO>RD115002</TENDER_CASE_NO>
    <TENDER_NAME>道路養護工程</TENDER_NAME>
    <PROCUREMENT_TYPE>公開招標</PROCUREMENT_TYPE>
    <PROCUREMENT_ATTR>工程類</PROCUREMENT_ATTR>
    <TENDER_AWARD_WAY>最低標</TENDER_AWARD_WAY>
    <TENDER_AWARD_PRICE>500000</TENDER_AWARD_PRICE>
    <BIDDER_LIST><BIDDER_SUPP_NAME>某工程公司</BIDDER_SUPP_NAME></BIDDER_LIST>
  </TENDER>
</TENDER_LIST>
"""

_TENDER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<TENDER_LIST>
  <TENDER>
    <TENDER_SPDT>2026/05/02</TENDER_SPDT>
    <TENDER_ORG_NAME>衛生福利部</TENDER_ORG_NAME>
    <TENDER_CASE_NO>M1601001</TENDER_CASE_NO>
    <TENDER_NAME>116年網站維運案</TENDER_NAME>
    <PROCUREMENT_TYPE>公開招標</PROCUREMENT_TYPE>
    <PROCUREMENT_ATTR>勞務類</PROCUREMENT_ATTR>
  </TENDER>
</TENDER_LIST>
"""


def _adapter(**kw) -> PccTenderAdapter:
    return PccTenderAdapter(today=date(2026, 6, 10), **kw)


def _ref(adapter: PccTenderAdapter, kind: str):
    return next(
        r for r in adapter._build_refs() if r.meta["kind"] == kind  # noqa: SLF001
    )


class TestDiscover:
    async def test_generates_halfmonth_urls_within_window(self):
        adapter = _adapter(award_months=2, tender_months=1)
        refs = await adapter.discover()
        award_urls = [r.url for r in refs if r.meta["kind"] == "award"]
        tender_urls = [r.url for r in refs if r.meta["kind"] == "tender"]
        assert any("award_20260402.xml" in u for u in award_urls)
        assert any("tender_20260601.xml" in u for u in tender_urls)
        # 未來期數不應出現
        assert not any("20260701" in u for u in award_urls + tender_urls)

    def test_access_strategy_is_static_file(self):
        assert _adapter().access_strategy is AccessStrategy.STATIC_FILE


class TestNormalize:
    def test_award_filters_to_mohw_and_maps_twinkle_columns(self):
        adapter = _adapter()
        ref = _ref(adapter, "award")
        batch = adapter.normalize(
            RawPayload(ref=ref, content=_AWARD_XML.encode("utf-8"))
        )
        assert len(batch.records) == 1  # 交通部被過濾
        rec = batch.records[0]
        assert rec.payload["agency"] == "衛生福利部疾病管制署"
        assert rec.payload["announcement_type"] == "決標公告"
        assert rec.payload["job_number"] == "CK115001"
        assert rec.payload["companies"] == "某資訊股份有限公司"
        assert rec.payload["award_price"] == "12000000"
        assert rec.payload["date"] == "2026-04-15"  # twinkle ISO 格式
        assert rec.payload["notice_date"] == "2026-04-18"
        assert rec.natural_key == "CK115001|決標公告|2026-04-15"

    def test_tender_announcement_type(self):
        adapter = _adapter()
        ref = _ref(adapter, "tender")
        batch = adapter.normalize(
            RawPayload(ref=ref, content=_TENDER_XML.encode("utf-8"))
        )
        assert len(batch.records) == 1
        rec = batch.records[0]
        assert rec.payload["announcement_type"] == "招標公告"
        assert rec.payload["award_price"] == ""
        assert rec.payload["date"] == "2026-05-02"

    def test_empty_file_tolerated(self):
        # 半月檔尚未發布(實測回 HTTP 200 + 0 bytes)→ 零筆且不報錯
        adapter = _adapter()
        ref = _ref(adapter, "award")
        batch = adapter.normalize(RawPayload(ref=ref, content=b""))
        assert batch.records == ()


class TestFullScopeDataset:
    """agency_prefix=""(全機關)— Cowork IT 標案看板的 twinkle-hub 替代資料源。"""

    def _full(self) -> PccTenderAdapter:
        return _adapter(
            agency_prefix="", dataset_id="pcc-tender", collection="procurement"
        )

    def test_empty_prefix_keeps_all_agencies(self):
        ref = _ref(self._full(), "award")
        batch = self._full().normalize(
            RawPayload(ref=ref, content=_AWARD_XML.encode("utf-8"))
        )
        agencies = {r.payload["agency"] for r in batch.records}
        assert agencies == {"衛生福利部疾病管制署", "交通部公路局"}

    def test_dataset_meta_reflects_full_scope(self):
        ref = _ref(self._full(), "tender")
        batch = self._full().normalize(
            RawPayload(ref=ref, content=_TENDER_XML.encode("utf-8"))
        )
        assert batch.dataset.id == "pcc-tender"
        assert batch.dataset.collection == "procurement"
        assert "全機關" in batch.dataset.title


class TestTopicFilter:
    """title_includes/excludes — 衛福部範圍內再篩「資訊勞務相關」(看板用)。"""

    def test_includes_keeps_matching_title(self):
        a = _adapter(agency_prefix="衛生福利部", title_includes=("資訊",))
        batch = a.normalize(
            RawPayload(ref=_ref(a, "award"), content=_AWARD_XML.encode("utf-8"))
        )
        # 疾管署「疫苗冷鏈資訊系統維運案」含資訊;交通部非衛福部已濾
        assert len(batch.records) == 1
        assert batch.records[0].payload["agency"] == "衛生福利部疾病管制署"

    def test_includes_drops_non_matching(self):
        a = _adapter(agency_prefix="衛生福利部", title_includes=("區塊鏈",))
        batch = a.normalize(
            RawPayload(ref=_ref(a, "award"), content=_AWARD_XML.encode("utf-8"))
        )
        assert len(batch.records) == 0

    def test_excludes_removes_matching(self):
        a = _adapter(
            agency_prefix="衛生福利部",
            title_includes=("資訊", "系統"),
            title_excludes=("疫苗",),
        )
        batch = a.normalize(
            RawPayload(ref=_ref(a, "award"), content=_AWARD_XML.encode("utf-8"))
        )
        assert len(batch.records) == 0  # 疫苗冷鏈被排除

    def test_empty_includes_keeps_all_in_agency(self):
        a = _adapter(agency_prefix="衛生福利部")  # 不傳 includes → 不篩主題
        batch = a.normalize(
            RawPayload(ref=_ref(a, "award"), content=_AWARD_XML.encode("utf-8"))
        )
        assert len(batch.records) == 1

    def test_tender_topic_filter(self):
        a = _adapter(agency_prefix="衛生福利部", title_includes=("網站",))
        batch = a.normalize(
            RawPayload(ref=_ref(a, "tender"), content=_TENDER_XML.encode("utf-8"))
        )
        assert len(batch.records) == 1  # 116年網站維運案

    def test_title_includes_marks_dataset_title(self):
        a = _adapter(
            agency_prefix="衛生福利部",
            dataset_id="pcc-tender",
            title_includes=("資訊",),
        )
        batch = a.normalize(
            RawPayload(ref=_ref(a, "tender"), content=_TENDER_XML.encode("utf-8"))
        )
        assert "資訊勞務" in batch.dataset.title


def test_rejects_xml_doctype_payload():
    from health_opendata_mcp.adapters import _pcc_opendata as pcc

    xml = """<!DOCTYPE x [<!ENTITY boom "boom">]><TENDER_LIST />"""

    with pytest.raises(ValueError):
        pcc.parse_tender_xml(xml)
