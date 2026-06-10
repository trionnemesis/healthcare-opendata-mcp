"""NhiApiAdapter — 健保署 CSV API(BDD: ingestion.feature / source-registration.feature)。"""
from health_opendata_mcp.adapters.nhi import NhiApiAdapter, NhiDatasetSpec
from health_opendata_mcp.contracts import AccessStrategy, RawPayload

# 實測 info.nhi.gov.tw 回應為 UTF-8 BOM CSV
_CSV = "﻿醫事機構代碼,醫事機構名稱,醫事機構種類,縣市別代碼\n" \
    "0102080017,高雄市立民生醫院,綜合醫院,2\n" \
    "0401180014,衛生福利部桃園醫院,綜合醫院,3\n"

SPECS = [
    NhiDatasetSpec(
        dataset_id="nhi-hospital-district",
        r_id="A21030000I-D21003-003",
        title="健保特約醫事機構-地區醫院",
    )
]


def _adapter() -> NhiApiAdapter:
    return NhiApiAdapter(SPECS)


class TestDiscover:
    async def test_refs_carry_rid_url_and_format(self):
        refs = await _adapter().discover()
        assert len(refs) == 1
        ref = refs[0]
        assert "rId=A21030000I-D21003-003" in ref.url
        assert ref.fmt == "csv"
        assert ref.dataset.id == "nhi-hospital-district"
        assert ref.dataset.collection == "healthcare"

    def test_access_strategy_is_platform_api(self):
        assert _adapter().access_strategy is AccessStrategy.PLATFORM_API


class TestNormalize:
    async def test_csv_with_bom_to_records(self):
        adapter = _adapter()
        ref = (await adapter.discover())[0]
        batch = adapter.normalize(RawPayload(ref=ref, content=_CSV.encode("utf-8")))
        assert [c.name for c in batch.dataset.columns] == [
            "醫事機構代碼", "醫事機構名稱", "醫事機構種類", "縣市別代碼",
        ]
        assert len(batch.records) == 2
        rec = batch.records[0]
        assert rec.natural_key == "0102080017"  # natural_key=醫事機構代碼
        assert rec.payload["醫事機構名稱"] == "高雄市立民生醫院"

    async def test_empty_content_yields_no_records(self):
        adapter = _adapter()
        ref = (await adapter.discover())[0]
        batch = adapter.normalize(RawPayload(ref=ref, content=b""))
        assert batch.records == ()
