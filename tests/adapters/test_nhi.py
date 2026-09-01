"""NhiApiAdapter — 健保署 CSV API(BDD: ingestion.feature / source-registration.feature)。"""
import pytest

from health_opendata_mcp.adapters.nhi import (
    NhiApiAdapter,
    NhiDatasetSpec,
    NhiHealthcareFacilityAdapter,
)
from health_opendata_mcp.contracts import AccessStrategy, RawPayload

# 實測 info.nhi.gov.tw 回應為 UTF-8 BOM CSV
_CSV = "﻿醫事機構代碼,醫事機構名稱,醫事機構種類,縣市別代碼\n" \
    "0102080017,高雄市立民生醫院,綜合醫院,2\n" \
    "0401180014,衛生福利部桃園醫院,綜合醫院,3\n"

_HEALTHCARE_CSV = (
    "﻿分區業務組別代碼,醫事機構代碼,權屬別名稱,醫事機構名稱,機構地址,電話區域號碼,電話號碼,"
    "特約類別,型態別代碼,醫事機構種類,終止合約或歇業日期,原始合約起始日期\n"
    "1,0131060029,部立及直轄市立醫院,衛生福利部臺北醫院,台北,02,25553000,2,01,A,,20200101\n"
    "1,0101090518,縣市立醫院,基隆市立醫院,基隆,02,24652141,3,02,A,,20200101\n"
    "1,0101090519,衛生所,桃園衛生所,台中,04,23123456,4,04,衛生所,,20200101\n"
    "1,0101090520,地方醫師公會,一般診所,高雄,07,27654321,4,04,診所,20230101,20200101\n"
    "1,0101090521,部立及直轄市立醫院,高雄市立聯合醫院,高雄,07,29829111,3,01,A,20230101,20180101\n"
    "1,0101090522,衛生福利部,不在需求範圍,嘉義,05,27500000,2,01,A,,20200101\n"
)

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


class TestNhiHealthcareFacilityAdapter:
    @staticmethod
    def _adapter() -> NhiHealthcareFacilityAdapter:
        return NhiHealthcareFacilityAdapter([
            NhiDatasetSpec(
                dataset_id="nhi-healthcare-facility",
                r_id="A21030000I-D2100G-001",
                title="健保特約醫事機構-醫療機構",
            )
        ])

    async def test_facility_dataset_filters_scope_and_enriches_fields(self):
        adapter = self._adapter()
        ref = (await adapter.discover())[0]
        batch = adapter.normalize(RawPayload(ref=ref, content=_HEALTHCARE_CSV.encode("utf-8")))
        assert len(batch.records) == 5
        assert [c.name for c in batch.dataset.columns][-4:] == [
            "facility_type",
            "governing_level",
            "classification_source",
            "is_active",
        ]

        records = {r.natural_key: r.payload for r in batch.records}

        mohw = records["0131060029"]
        assert mohw["facility_type"] == "hospital"
        assert mohw["governing_level"] == "mohw"
        assert mohw["classification_source"] == "official_mapping:MOHW_AFFILIATED_FACILITY_NAMES"
        assert mohw["is_active"] is True

        local_government = records["0101090518"]
        assert local_government["facility_type"] == "hospital"
        assert local_government["governing_level"] == "local_government"
        assert local_government["is_active"] is True

        health_center = records["0101090519"]
        assert health_center["facility_type"] == "health_center"
        assert health_center["governing_level"] == "local_government"

        clinic = records["0101090520"]
        assert clinic["facility_type"] == "clinic"
        assert clinic["governing_level"] == "unknown"
        assert clinic["is_active"] is False

        terminated = records["0101090521"]
        assert terminated["facility_type"] == "hospital"
        assert terminated["governing_level"] == "local_government"
        assert terminated["is_active"] is False

        assert batch.dataset.columns[-1].name == "is_active"
        assert batch.dataset.columns[-1].type == "INTEGER"

    async def test_out_of_scope_facility_rows_are_dropped(self):
        adapter = self._adapter()
        ref = (await adapter.discover())[0]
        batch = adapter.normalize(RawPayload(ref=ref, content=_HEALTHCARE_CSV.encode("utf-8")))
        ids = {r.natural_key for r in batch.records}
        assert "0101090522" not in ids

    async def test_duplicate_natural_key_is_normalized_once(self):
        adapter = self._adapter()
        ref = (await adapter.discover())[0]
        duplicate = _HEALTHCARE_CSV + (
            "1,0131060029,部立及直轄市立醫院,衛生福利部臺北醫院,台北,02,"
            "22765566,2,01,A,,20200101\n"
        )
        batch = adapter.normalize(RawPayload(ref=ref, content=duplicate.encode("utf-8")))
        matching = [r for r in batch.records if r.natural_key == "0131060029"]
        assert len(matching) == 1
        assert matching[0].payload["電話號碼"] == "22765566"

    async def test_exact_mapping_covers_mohw_branch_and_renamed_hospital(self):
        adapter = self._adapter()
        ref = (await adapter.discover())[0]
        csv = _HEALTHCARE_CSV.splitlines()[0] + "\n" + (
            "1,0131060010,部立及直轄市立醫院,衛生福利部樂生醫院,新北,02,"
            "82006600,3,02,A,,20200101\n"
            "1,0146020537,部立及直轄市立醫院,衛生福利部臺東醫院成功分院,"
            "臺東,089,851218,3,01,A,,20200101\n"
        )
        batch = adapter.normalize(RawPayload(ref=ref, content=csv.encode("utf-8")))
        assert {r.payload["governing_level"] for r in batch.records} == {"mohw"}

    @pytest.mark.parametrize(
        "content, message",
        [
            (b"", "schema drift"),
            (b"<html><title>maintenance</title></html>", "schema drift"),
            (
                "醫事機構代碼,權屬別名稱,醫事機構名稱,特約類別,"
                "終止合約或歇業日期\n".encode(),
                "沒有可識別的資料列",
            ),
        ],
    )
    async def test_invalid_or_empty_source_is_diagnosable(self, content, message):
        adapter = self._adapter()
        ref = (await adapter.discover())[0]
        with pytest.raises(ValueError, match=message):
            adapter.normalize(RawPayload(ref=ref, content=content))


class TestCompositeNaturalKey:
    """9402 保險病床比率:同機構多月份,natural key = 機構代碼|統計年月。"""

    _CSV = (
        "機構代碼,統計年月,機構名稱,急性比率\n"
        "1137010024,10408,彰化基督教醫院,74.26\n"
        "1137010024,10409,彰化基督教醫院,74.31\n"
    )

    async def test_composite_key_joined_with_pipe(self):
        adapter = NhiApiAdapter([
            NhiDatasetSpec(
                dataset_id="nhi-hospital-bed-ratio",
                r_id="A21030000I-D02001-015",
                title="全民健保特約醫院之保險病床比率",
                natural_key_columns=("機構代碼", "統計年月"),
            )
        ])
        ref = (await adapter.discover())[0]
        batch = adapter.normalize(RawPayload(ref=ref, content=self._CSV.encode("utf-8")))
        assert [r.natural_key for r in batch.records] == [
            "1137010024|10408", "1137010024|10409",
        ]


class TestBuiltinRegistry:
    """cli 內建註冊表 — 診所資料集(BDD: 註冊健保診所資料集)。"""

    async def test_clinic_dataset_registered_with_verified_rid(self):
        from health_opendata_mcp.cli import NHI_DATASETS
        clinic = {s.dataset_id: s for s in NHI_DATASETS}.get("nhi-clinic")
        assert clinic is not None
        # 實查 2026-06-10:D32001-001 查無資料,正確 rId 為 D21004-009
        assert clinic.r_id == "A21030000I-D21004-009"
        assert clinic.effective_key_columns == ("醫事機構代碼",)
        refs = await NhiApiAdapter([clinic]).discover()
        assert refs[0].dataset.id == "nhi-clinic"
        assert refs[0].fmt == "csv"

        facility = {s.dataset_id: s for s in NHI_DATASETS}.get("nhi-healthcare-facility")
        assert facility is not None
        assert facility.r_id == "A21030000I-D2100G-001"
