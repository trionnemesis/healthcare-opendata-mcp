"""StaticCsvAdapter — 政府開放資料靜態 CSV(BDD: source-registration.feature 2026-06-10 擴充)。"""
from health_opendata_mcp.adapters.static_csv import StaticCsvAdapter, StaticCsvSpec
from health_opendata_mcp.contracts import AccessStrategy, RawPayload

# 實測 mohw.gov.tw 門診就診率:新舊年度檔欄位名不同(疾病別 vs 疾病別(第10版))
_CSV_OLD = "﻿年度,縣市別,疾病別,每十萬人口就診率\n97,總計,總計,90918\n"
_CSV_NEW = "﻿年度,縣市別,疾病別(第10版),每十萬人口就診率\n113,總計,總計,88123\n"
# 實測 9402:來源含完全重複列
_CSV_DUP = (
    "機構代碼,統計年月,機構名稱\n"
    "101090517,10810,臺北市立聯合醫院\n"
    "101090517,10810,臺北市立聯合醫院\n"
)

OUTPATIENT = StaticCsvSpec(
    dataset_id="mohw-outpatient-rate",
    title="健保平均門診就診率",
    urls=("https://example.test/old.csv", "https://example.test/new.csv"),
    natural_key_columns=("年度", "縣市別", "疾病別"),
    column_renames={"疾病別(第10版)": "疾病別"},
)
BED_RATIO = StaticCsvSpec(
    dataset_id="nhi-hospital-bed-ratio",
    title="全民健保特約醫院之保險病床比率",
    urls=("https://example.test/bed.csv",),
    natural_key_columns=("機構代碼", "統計年月"),
)


def _adapter(specs: list[StaticCsvSpec]) -> StaticCsvAdapter:
    return StaticCsvAdapter(specs)


class TestDiscover:
    def test_access_strategy_is_static_file(self):
        assert _adapter([OUTPATIENT]).access_strategy is AccessStrategy.STATIC_FILE

    async def test_multi_url_spec_yields_refs_sharing_dataset_id(self):
        refs = await _adapter([OUTPATIENT]).discover()
        assert len(refs) == 2
        assert {r.dataset.id for r in refs} == {"mohw-outpatient-rate"}
        assert [r.url for r in refs] == list(OUTPATIENT.urls)
        assert all(r.fmt == "csv" for r in refs)
        assert refs[0].dataset.collection == "healthcare"


class TestNormalize:
    async def test_column_rename_unifies_schema_across_files(self):
        adapter = _adapter([OUTPATIENT])
        old_ref, new_ref = await adapter.discover()
        old_batch = adapter.normalize(
            RawPayload(ref=old_ref, content=_CSV_OLD.encode("utf-8"))
        )
        new_batch = adapter.normalize(
            RawPayload(ref=new_ref, content=_CSV_NEW.encode("utf-8"))
        )
        cols = ["年度", "縣市別", "疾病別", "每十萬人口就診率"]
        assert [c.name for c in old_batch.dataset.columns] == cols
        assert [c.name for c in new_batch.dataset.columns] == cols
        assert new_batch.records[0].payload["疾病別"] == "總計"

    async def test_composite_natural_key_joined_with_pipe(self):
        adapter = _adapter([OUTPATIENT])
        ref = (await adapter.discover())[0]
        batch = adapter.normalize(RawPayload(ref=ref, content=_CSV_OLD.encode("utf-8")))
        assert batch.records[0].natural_key == "97|總計|總計"

    async def test_exact_duplicate_rows_share_natural_key(self):
        adapter = _adapter([BED_RATIO])
        ref = (await adapter.discover())[0]
        batch = adapter.normalize(RawPayload(ref=ref, content=_CSV_DUP.encode("utf-8")))
        # 兩列同 natural_key → repository upsert 去重為單筆
        assert [r.natural_key for r in batch.records] == [
            "101090517|10810", "101090517|10810",
        ]

    async def test_row_missing_key_column_is_skipped(self):
        adapter = _adapter([BED_RATIO])
        ref = (await adapter.discover())[0]
        csv = "機構代碼,統計年月,機構名稱\n,10810,缺代碼\n101090517,10810,正常\n"
        batch = adapter.normalize(RawPayload(ref=ref, content=csv.encode("utf-8")))
        assert len(batch.records) == 1
