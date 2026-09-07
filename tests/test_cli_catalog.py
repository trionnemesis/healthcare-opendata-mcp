"""hcmcp-sync 的來源組裝 — 由 catalog 驅動(BDD: source-registration.feature)。

回歸重點:`StaticCsvAdapter` 曾實作、匯出並有測試,卻從未被 `_sync()`
實例化 —— 靜態 CSV 來源實際貢獻 0 筆。本檔把「catalog 有 enabled 的
靜態資料集就必須組出該 adapter」釘成測試。
"""
from health_opendata_mcp.adapters import (
    NhiApiAdapter,
    NhiDatasetSpec,
    PccTenderAdapter,
    StaticCsvAdapter,
    StaticCsvSpec,
)
from health_opendata_mcp.catalog import CATALOG, enabled_nhi_specs
from health_opendata_mcp.cli import NHI_DATASETS, STATIC_CSV_DATASETS, build_adapters

NHI_SPEC = NhiDatasetSpec(
    dataset_id="demo-nhi", r_id="A21030000I-D21004-009", title="示範"
)
STATIC_SPEC = StaticCsvSpec(
    dataset_id="demo-static",
    title="示範靜態 CSV",
    urls=("https://data.gov.tw/demo.csv",),
    natural_key_columns=("年度",),
)


def _kinds(adapters) -> list[type]:
    return [type(a) for a in adapters]


class TestBuildAdapters:
    def test_static_csv_adapter_is_built_when_catalog_has_static_entry(self):
        adapters = build_adapters(
            12, 12, nhi_specs=[NHI_SPEC], static_specs=[STATIC_SPEC]
        )
        assert StaticCsvAdapter in _kinds(adapters)
        assert _kinds(adapters) == [NhiApiAdapter, StaticCsvAdapter, PccTenderAdapter]

    def test_no_adapter_for_a_kind_without_enabled_entries(self):
        adapters = build_adapters(12, 12, nhi_specs=[], static_specs=[])
        # 不註冊一個永遠抓 0 筆的來源
        assert _kinds(adapters) == [PccTenderAdapter]

    def test_pcc_is_always_built_and_keeps_its_dataset_id(self):
        adapters = build_adapters(12, 12, nhi_specs=[], static_specs=[])
        pcc = adapters[0]
        assert isinstance(pcc, PccTenderAdapter)
        assert pcc.source_id == "pcc-opendata"

    def test_defaults_come_from_the_catalog(self):
        adapters = build_adapters(12, 12)
        assert _kinds(adapters)[0] is NhiApiAdapter
        assert [s.dataset_id for s in NHI_DATASETS] == [
            s.dataset_id for s in enabled_nhi_specs()
        ]


class TestCatalogDerivedRegistry:
    def test_nhi_datasets_only_contains_enabled_catalog_entries(self):
        enabled = {e.dataset_id for e in CATALOG if e.enabled}
        assert {s.dataset_id for s in NHI_DATASETS} <= enabled
        assert "nhi-clinic" in {s.dataset_id for s in NHI_DATASETS}

    def test_disabled_candidates_are_not_synced(self):
        synced = {s.dataset_id for s in NHI_DATASETS} | {
            s.dataset_id for s in STATIC_CSV_DATASETS
        }
        for entry in CATALOG:
            if not entry.enabled:
                assert entry.dataset_id not in synced
